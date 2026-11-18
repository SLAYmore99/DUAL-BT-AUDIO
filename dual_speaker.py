"""
Dual Speaker Mixer
------------------
Play one audio source on two speakers at the same time - even different brands
(Bluetooth, USB, 3.5mm, HDMI ...) - with separate controls for each speaker:
volume, mute, on/off, channel (stereo / left / right / mono / swap) and delay.

* Drift correction: each speaker reads a shared buffer at its own pace and is
  nudged by a tiny speed change (max 0.2 %), so two speakers with slightly
  different clocks stay in sync without clicks.
* Auto-calibrate: each speaker plays short chirps in its own frequency band,
  the laptop mic hears both, and the delay of the faster speaker is set for you.
* Phone remote on your Wi-Fi (address shown in the window).

Windows 10/11. Needs Python 3.9+ and:  pip install soundcard numpy
Ideas credited in README.md (erdongshuiyang/DualSpeaker, MIT).
"""
import ctypes
import json
import os
import socket
import sys
import threading
import time
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

try:
    import soundcard as sc
except ImportError:
    print("Missing package. Run:  python -m pip install soundcard numpy")
    input("Press Enter to close...")
    sys.exit(1)

warnings.filterwarnings("ignore", category=getattr(sc, "SoundcardRuntimeWarning", RuntimeWarning))

SR = 48000            # sample rate used everywhere
BLOCK = 960           # 20 ms per block
DEV_BUF = SR // 10    # 100 ms device buffer: absorbs Python timing hiccups without crackles
RING_S = 12           # seconds kept in the shared buffer
LATENCY_MS = 60       # how far behind the newest captured audio each speaker reads
MAX_DELAY = 1000      # ms, per-speaker delay range
MAX_CORR = 0.002      # max speed change for drift correction (0.2 %)
KP = 0.05             # drift correction: proportional gain (per second of error)
KI = KP * KP / 4      # integral gain (critically damped), removes steady drift fully
ERR_SMOOTH_S = 0.5    # smooth the buffer error so capture jitter doesn't wobble the pitch
REANCHOR_S = 0.25     # error bigger than this (e.g. after a stall): jump instead of slewing
RETRY_S = 2.0         # reconnect delay when a device drops (e.g. Bluetooth speaker turned off)
WEB_PORT = 8765
N_OUT = 2
HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(HERE, "dual_speaker_settings.json")
MODES = ["Stereo", "Left channel only", "Right channel only", "Mono (L+R)", "Swap L/R"]


def com_init():
    """Each audio thread on Windows needs COM initialised."""
    if sys.platform == "win32":
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0)
        except Exception:
            pass


# ----------------------------------------------------------------- shared state
state_lock = threading.Lock()
state = {
    "source": "",
    "mic": "",
    "master": 80,
    "running": False,
    "status": "Stopped",
    "calib": "",             # last calibration message
    "pending": None,         # command from phone remote: "start" / "stop" / "calibrate"
    "sync_test": False,
    "outputs": [
        {"device": "", "enabled": True, "volume": 100, "mode": "Stereo", "delay": 0, "muted": False}
        for _ in range(N_OUT)
    ],
}
SAVED_KEYS = ("source", "mic", "master")
OUT_KEYS = ("device", "enabled", "volume", "mode", "delay", "muted")


def load_settings(path=None):
    try:
        with open(path or SETTINGS, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    for k in SAVED_KEYS:
        if k in data:
            state[k] = data[k]
    for i, o in enumerate(data.get("outputs", [])[:N_OUT]):
        for k in OUT_KEYS:
            if k in o:
                state["outputs"][i][k] = o[k]


def save_settings(path=None):
    with state_lock:
        data = {k: state[k] for k in SAVED_KEYS}
        data["outputs"] = [{k: o[k] for k in OUT_KEYS} for o in state["outputs"]]
    try:
        with open(path or SETTINGS, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ----------------------------------------------------------------- signal helpers
def apply_mode(x, mode):
    if mode == "Left channel only":
        return np.repeat(x[:, :1], 2, axis=1)
    if mode == "Right channel only":
        return np.repeat(x[:, 1:2], 2, axis=1)
    if mode == "Mono (L+R)":
        return np.repeat(x.mean(axis=1, keepdims=True), 2, axis=1)
    if mode == "Swap L/R":
        return x[:, ::-1]
    return x


# Calibration chirps: each speaker gets its own frequency band so one mic recording
# can tell them apart; irregular gaps so the pattern can't line up with itself.
CHIRP_LEN = 0.09
CHIRP_OFFSETS = (0.0, 0.73, 1.61, 2.67, 3.91, 5.23)
CHIRP_BANDS = ((850.0, 1850.0), (2300.0, 3700.0))
CHIRP_LEVEL = 0.35


def chirp(idx, t):
    """One windowed chirp for speaker idx at times t (seconds, 0..CHIRP_LEN)."""
    f0, f1 = CHIRP_BANDS[idx % len(CHIRP_BANDS)]
    w = np.sin(np.pi * t / CHIRP_LEN) ** 2
    return np.sin(2 * np.pi * (f0 * t + 0.5 * (f1 - f0) * t * t / CHIRP_LEN)) * w


def test_signal(idx, t):
    """Full calibration pattern for speaker idx at times t (seconds from pattern start)."""
    out = np.zeros(len(t), np.float32)
    for off in CHIRP_OFFSETS:
        x = t - off
        m = (x >= 0) & (x < CHIRP_LEN)
        if m.any():
            out[m] += chirp(idx, x[m]).astype(np.float32)
    return out * CHIRP_LEVEL


def _envelope(rec, idx):
    """Matched-filter envelope of a mic recording against speaker idx's chirp."""
    tmpl = chirp(idx, np.arange(int(CHIRP_LEN * SR)) / SR)
    n = len(rec) + len(tmpl)
    nfft = 1 << (n - 1).bit_length()
    c = np.fft.irfft(np.fft.rfft(rec, nfft) * np.conj(np.fft.rfft(tmpl, nfft)), nfft)[:len(rec)]
    env = np.abs(c)
    k = int(0.0005 * SR)  # smooth over 1 ms so phase wiggles don't matter
    return np.convolve(env, np.ones(2 * k + 1) / (2 * k + 1), mode="same")


def measure_offset(rec):
    """Find both speakers' chirp patterns in a mic recording.
    Returns dict(valid, diff_ms = speaker2 - speaker1 arrival, spread_ms, scores, msg)."""
    rec = np.asarray(rec, np.float64)
    rec = rec - rec.mean()
    offs = np.array([int(round(o * SR)) for o in CHIRP_OFFSETS])
    span = offs[-1]
    peaks, scores = [], []
    for idx in (0, 1):
        env = _envelope(rec, idx)
        if len(env) <= span + 1:
            return {"valid": False, "msg": "Recording too short."}
        L = len(env) - span
        pattern = sum(env[o:o + L] for o in offs)          # score of each possible start
        t0 = int(np.argmax(pattern))
        noise = np.median(env) + 1e-12
        win = int(0.005 * SR)
        pk = []
        for o in offs:
            a, b = max(0, t0 + o - win), min(len(env), t0 + o + win)
            pk.append(a + int(np.argmax(env[a:b])))
        peaks.append(np.array(pk))
        scores.append(float(np.median([env[p] for p in pk]) / noise))
    deltas = (peaks[1] - peaks[0]) * 1000.0 / SR
    diff = float(np.median(deltas))
    spread = float(np.median(np.abs(deltas - diff)))
    valid = min(scores) > 6.0 and spread < 3.0
    msg = (f"Speaker 2 is {diff:+.0f} ms vs speaker 1 (spread {spread:.1f} ms)" if valid else
           "Couldn't hear both speakers clearly. Turn them up, put the mic between them, keep the room quiet.")
    return {"valid": valid, "diff_ms": diff, "spread_ms": spread, "scores": scores, "msg": msg}


def corrected_delays(d1, d2, diff_ms):
    """New delays so both arrive together. diff_ms = speaker2 arrival - speaker1 arrival."""
    d1, d2 = d1 + max(diff_ms, 0), d2 + max(-diff_ms, 0)
    base = min(d1, d2)
    d1, d2 = d1 - base, d2 - base
    return int(round(min(MAX_DELAY, d1))), int(round(min(MAX_DELAY, d2)))


# ----------------------------------------------------------------- audio engine
class Ring:
    """Shared capture buffer with an absolute frame timeline. Speakers read from it
    at fractional positions (linear interpolation), so each can run at its own speed."""

    def __init__(self, seconds=RING_S):
        self.cap = SR * seconds
        self.buf = np.zeros((self.cap, 2), np.float32)
        self.written = 0
        self.last_t = None
        self.clock = time.perf_counter
        self.lock = threading.Lock()

    def now_written(self):
        """Frames written, plus how much the source has produced since its last chunk.
        Captured audio arrives in ~20 ms chunks; without this the fill level looks like a
        sawtooth and the drift correction would chase it."""
        with self.lock:
            if self.last_t is None:
                return float(self.written)
            return self.written + min(1.5 * BLOCK, max(0.0, (self.clock() - self.last_t) * SR))

    def write(self, data):
        n = len(data)
        if n == 0:
            return
        if n > self.cap:
            data, n = data[-self.cap:], self.cap
        with self.lock:
            i = self.written % self.cap
            first = min(n, self.cap - i)
            self.buf[i:i + first] = data[:first]
            if first < n:
                self.buf[:n - first] = data[first:]
            self.written += n
            self.last_t = self.clock()

    def read_at(self, pos, step, frames):
        p = pos + np.arange(frames) * step
        i0 = np.floor(p).astype(np.int64)
        f = (p - i0).astype(np.float32)[:, None]
        with self.lock:
            end = self.written
            ok = (i0 >= 0) & (i0 + 1 < end) & (i0 >= end - self.cap)
            a = self.buf[i0 % self.cap]
            b = self.buf[(i0 + 1) % self.cap]
        out = a + (b - a) * f
        out[~ok] = 0.0
        return out, int(np.count_nonzero(~ok))


class OutputWorker(threading.Thread):
    def __init__(self, idx, speaker, engine):
        super().__init__(daemon=True)
        self.idx = idx
        self.speaker = speaker
        self.engine = engine
        self.stop_evt = threading.Event()
        self.error = None
        self.position = None
        self.prefill = 0
        self.starving = False
        self.last_delay = 0
        self.err_f = 0.0
        self.integ = 0.0
        self.drift_ppm = 0.0
        self.lag_ms = 0.0
        self.missing = 0
        self.resyncs = 0
        self.played = 0

    def run(self):
        com_init()
        while not self.stop_evt.is_set():
            try:
                with self.speaker.player(samplerate=SR, channels=2, blocksize=DEV_BUF) as p:
                    self.error = None
                    self.open_stream()
                    while not self.stop_evt.is_set():
                        p.play(self.next_block())
            except Exception as e:
                self.error = f"Speaker {self.idx + 1}: {e} (retrying)"
                self.stop_evt.wait(RETRY_S)

    def open_stream(self):
        """Fresh stream: fill the device buffer with silence first, and only start
        the timeline once play() starts pacing us at the speaker's real rate."""
        self.position = None
        self.prefill = DEV_BUF
        self.starving = False
        self.err_f = 0.0

    @property
    def err_ms(self):
        return self.err_f * 1000.0

    def next_block(self):
        """Next BLOCK frames for this speaker. play() blocks while the device buffer is
        full, so this is called at the speaker's own clock rate."""
        ring = self.engine.ring
        if self.prefill > 0:
            self.prefill -= BLOCK
            return np.zeros((BLOCK, 2), np.float32)
        with state_lock:
            o = dict(state["outputs"][self.idx])
            master = state["master"]
        delay = o["delay"]
        target = (LATENCY_MS + delay) * SR / 1000.0
        written = ring.now_written()
        if self.position is None:
            self.position, self.last_delay = written - target, delay
        if delay != self.last_delay:  # delay changed: jump the read position
            self.position -= (delay - self.last_delay) * SR / 1000.0
            self.last_delay = delay
        lag = written - self.position
        err = lag - target
        if lag < BLOCK:  # source stalled: play silence and wait, don't loop old audio
            self.starving = True
            self.missing += BLOCK
            self.played += BLOCK
            return np.zeros((BLOCK, 2), np.float32)
        if self.starving or abs(err) > REANCHOR_S * SR:  # back after a stall, or far off: jump once
            self.starving = False
            self.position = written - target
            self.resyncs += 1
            lag, err = target, 0.0
            self.err_f = 0.0
        dt = BLOCK / SR
        self.err_f += (err / SR - self.err_f) * min(1.0, dt / ERR_SMOOTH_S)
        self.integ = float(np.clip(self.integ + self.err_f * dt, -MAX_CORR / KI, MAX_CORR / KI))
        corr = float(np.clip(KP * self.err_f + KI * self.integ, -MAX_CORR, MAX_CORR))
        step = 1.0 + corr
        self.drift_ppm, self.lag_ms = corr * 1e6, lag * 1000.0 / SR
        out, missing = ring.read_at(self.position, step, BLOCK)
        self.missing += missing
        cs = self.engine.calib_start
        if cs is not None:  # calibration: chirps on the same timeline, music muted
            t = (self.position + np.arange(BLOCK) * step - cs) / SR
            out = np.repeat(test_signal(self.idx, t)[:, None], 2, axis=1)
            gain = 1.0
        else:
            out = apply_mode(out, o["mode"])
            gain = 0.0 if (o["muted"] or not o["enabled"]) else (o["volume"] / 100) * (master / 100)
        self.position += BLOCK * step
        self.played += BLOCK
        return np.clip(out * gain, -1.0, 1.0).astype(np.float32)


class Engine:
    def __init__(self):
        self.workers = []
        self.cap_thread = None
        self.stop_evt = threading.Event()
        self.error = None
        self.ring = Ring()
        self.calib_start = None
        self.calibrating = False
        self.n = 0

    @property
    def running(self):
        return self.cap_thread is not None and self.cap_thread.is_alive()

    def start(self, mic, targets):
        self.stop()
        self.error = None
        self.ring = Ring()
        self.n = 0
        self.stop_evt = threading.Event()
        self.workers = [OutputWorker(i, spk, self) for i, spk in targets]
        self.cap_thread = threading.Thread(target=self._capture, args=(mic,), daemon=True)
        self.cap_thread.start()
        for w in self.workers:
            w.start()

    def stop(self):
        self.stop_evt.set()
        for w in self.workers:
            w.stop_evt.set()
        for t in [self.cap_thread] + self.workers:
            if t is not None:
                t.join(timeout=1.5)
        self.workers, self.cap_thread = [], None
        self.calib_start = None

    def _capture(self, mic):
        com_init()
        while not self.stop_evt.is_set():
            try:
                with mic.recorder(samplerate=SR, blocksize=DEV_BUF) as r:
                    self.error = None
                    while not self.stop_evt.is_set():
                        data = np.asarray(r.record(numframes=BLOCK), dtype=np.float32)
                        if data.ndim == 1:
                            data = data[:, None]
                        if data.shape[1] == 1:
                            data = np.repeat(data, 2, axis=1)
                        elif data.shape[1] > 2:
                            data = data[:, :2]
                        self.ring.write(self._sync_clicks(data))
            except Exception as e:
                self.error = f"Source: {e} (retrying)"
                self.stop_evt.wait(RETRY_S)

    def _sync_clicks(self, data):
        """Short beeps twice a second while 'Sync test' is on (music muted), to compare by ear."""
        idx = np.arange(len(data)) + self.n
        self.n += len(data)
        with state_lock:
            on = state["sync_test"]
        if on:
            mask = (idx % (SR // 2)) < 480
            data = np.zeros_like(data)
            if mask.any():
                data[mask] = (0.5 * np.sin(2 * np.pi * 1000 * idx[mask] / SR))[:, None]
        return data

    def calibrate(self, mic, record_fn=None):
        """Play the chirp pattern on both speakers, record with `mic`, measure, and
        set the delays. Blocking (run it in a thread). Returns the measure dict."""
        if self.calibrating:
            return {"valid": False, "msg": "Already calibrating."}
        if len([w for w in self.workers if w.idx < 2]) < 2:
            return {"valid": False, "msg": "Start playback on both speakers first."}
        self.calibrating = True
        try:
            com_init()
            t_end = time.time() + 5.0  # let both speakers settle on the timeline first
            while time.time() < t_end and any(abs(getattr(w, "err_ms", 0.0)) > 5.0 for w in self.workers):
                time.sleep(0.1)
            lead = 1.0
            dur = lead + LATENCY_MS / 1000 + MAX_DELAY / 1000 + CHIRP_OFFSETS[-1] + 2.0
            self.calib_start = self.ring.written + lead * SR
            if record_fn is None:
                with mic.recorder(samplerate=SR, blocksize=DEV_BUF) as r:
                    rec = np.asarray(r.record(numframes=int(dur * SR)), np.float32)
            else:
                rec = record_fn(dur)
            self.calib_start = None
            if rec.ndim == 2:
                rec = rec.mean(axis=1)
            res = measure_offset(rec)
            if res["valid"]:
                with state_lock:
                    o1, o2 = state["outputs"][0], state["outputs"][1]
                    o1["delay"], o2["delay"] = corrected_delays(o1["delay"], o2["delay"], res["diff_ms"])
                    res["delays"] = (o1["delay"], o2["delay"])
                res["msg"] += f" -> delays set to {res['delays'][0]} / {res['delays'][1]} ms"
            return res
        except Exception as e:
            return {"valid": False, "msg": f"Calibration failed: {e}"}
        finally:
            self.calib_start = None
            self.calibrating = False

    def errors(self):
        errs = [self.error] if self.error else []
        errs += [w.error for w in self.workers if w.error]
        return errs


# ----------------------------------------------------------------- phone remote
WEB_PAGE = r"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Room Speakers</title><style>
body{font-family:system-ui,sans-serif;background:#111;color:#eee;margin:0;padding:16px;max-width:520px}
h1{font-size:20px;margin:0 0 12px}.card{background:#1d1d1d;border-radius:14px;padding:14px;margin-bottom:12px}
.row{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:8px 0}
input[type=range]{flex:1}button{background:#2d6cdf;color:#fff;border:0;border-radius:10px;padding:10px 14px;font-size:15px}
button.off{background:#444}select{background:#2a2a2a;color:#eee;border:0;border-radius:8px;padding:6px}
.name{font-weight:600}.muted{opacity:.6;font-size:13px}.val{width:60px;text-align:right}
</style></head><body><h1>Room Speakers</h1>
<div class="card"><div class="row"><button id="toggle">Start</button><span id="status" class="muted"></span></div>
<div class="row">Master<input id="master" type="range" min="0" max="100"><span class="val" id="masterv"></span></div>
<div class="row"><button id="sync" class="off">Sync test: off</button><button id="cal">Auto-calibrate</button></div>
<div class="muted" id="calmsg"></div></div>
<div id="outs"></div>
<script>
const MODES=["Stereo","Left channel only","Right channel only","Mono (L+R)","Swap L/R"];
let S=null,busy=false;
function post(u,b){return fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)})}
function set(path,value){post("/set",{path,value})}
function build(){
 const box=document.getElementById("outs");box.innerHTML="";
 S.outputs.forEach((o,i)=>{const d=document.createElement("div");d.className="card";d.innerHTML=`
 <div class="row"><span class="name">Speaker ${i+1}</span><span class="muted" id="dev${i}"></span></div>
 <div class="row"><button id="en${i}">On</button><button id="mu${i}">Mute</button></div>
 <div class="row">Volume<input id="vol${i}" type="range" min="0" max="150"><span class="val" id="volv${i}"></span></div>
 <div class="row">Delay<input id="del${i}" type="range" min="0" max="1000"><span class="val" id="delv${i}"></span></div>
 <div class="row">Channel<select id="mo${i}">${MODES.map(m=>`<option>${m}</option>`).join("")}</select></div>`;
 box.appendChild(d);
 const vol=d.querySelector("#vol"+i),del=d.querySelector("#del"+i);
 vol.oninput=()=>{d.querySelector("#volv"+i).textContent=vol.value+"%";set(["outputs",i,"volume"],+vol.value)};
 del.oninput=()=>{d.querySelector("#delv"+i).textContent=del.value+"ms";set(["outputs",i,"delay"],+del.value)};
 d.querySelector("#mo"+i).onchange=e=>set(["outputs",i,"mode"],e.target.value);
 d.querySelector("#en"+i).onclick=()=>set(["outputs",i,"enabled"],!S.outputs[i].enabled);
 d.querySelector("#mu"+i).onclick=()=>set(["outputs",i,"muted"],!S.outputs[i].muted);
 [vol,del].forEach(el=>{el.onpointerdown=()=>busy=true;el.onpointerup=()=>busy=false});});}
function render(){
 const t=document.getElementById("toggle");t.textContent=S.running?"Stop":"Start";t.className=S.running?"":"off";
 document.getElementById("status").textContent=S.status;
 document.getElementById("calmsg").textContent=S.calib;
 const sy=document.getElementById("sync");sy.textContent="Sync test: "+(S.sync_test?"on":"off");sy.className=S.sync_test?"":"off";
 if(!busy){document.getElementById("master").value=S.master}
 document.getElementById("masterv").textContent=S.master+"%";
 S.outputs.forEach((o,i)=>{
  document.getElementById("dev"+i).textContent=o.device||"(no speaker picked)";
  const en=document.getElementById("en"+i);en.textContent=o.enabled?"On":"Off";en.className=o.enabled?"":"off";
  const mu=document.getElementById("mu"+i);mu.textContent=o.muted?"Muted":"Mute";mu.className=o.muted?"":"off";
  if(!busy){document.getElementById("vol"+i).value=o.volume;document.getElementById("del"+i).value=o.delay}
  document.getElementById("volv"+i).textContent=o.volume+"%";document.getElementById("delv"+i).textContent=o.delay+"ms";
  const mo=document.getElementById("mo"+i);if(document.activeElement!==mo)mo.value=o.mode;});}
async function poll(){try{const r=await fetch("/state");const n=await r.json();const first=!S;S=n;if(first)build();render()}catch(e){}}
const m=document.getElementById("master");m.oninput=()=>{document.getElementById("masterv").textContent=m.value+"%";set(["master"],+m.value)};
m.onpointerdown=()=>busy=true;m.onpointerup=()=>busy=false;
document.getElementById("toggle").onclick=()=>post("/cmd",{cmd:S&&S.running?"stop":"start"});
document.getElementById("sync").onclick=()=>set(["sync_test"],!S.sync_test);
document.getElementById("cal").onclick=()=>post("/cmd",{cmd:"calibrate"});
poll();setInterval(poll,700);
</script></body></html>"""


def apply_set(path, value):
    """Validate and apply a change coming from the phone remote."""
    try:
        with state_lock:
            if path == ["master"]:
                state["master"] = int(max(0, min(100, int(value))))
            elif path == ["sync_test"]:
                state["sync_test"] = bool(value)
            elif len(path) == 3 and path[0] == "outputs" and int(path[1]) in range(N_OUT):
                o = state["outputs"][int(path[1])]
                key = path[2]
                if key == "volume":
                    o[key] = int(max(0, min(150, int(value))))
                elif key == "delay":
                    o[key] = int(max(0, min(MAX_DELAY, int(value))))
                elif key in ("enabled", "muted"):
                    o[key] = bool(value)
                elif key == "mode" and value in MODES:
                    o[key] = value
    except (ValueError, TypeError):
        pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/":
            self._send(200, WEB_PAGE, "text/html; charset=utf-8")
        elif self.path == "/state":
            with state_lock:
                pub = {k: state[k] for k in ("master", "running", "status", "sync_test", "calib")}
                pub["outputs"] = [dict(o) for o in state["outputs"]]
            self._send(200, json.dumps(pub))
        else:
            self._send(404, "{}")

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(min(n, 10000)) or b"{}")
        except Exception:
            return self._send(400, "{}")
        if self.path == "/set" and isinstance(data.get("path"), list):
            apply_set(data["path"], data.get("value"))
        elif self.path == "/cmd" and data.get("cmd") in ("start", "stop", "calibrate"):
            with state_lock:
                state["pending"] = data["cmd"]
        self._send(200, "{}")


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def start_web(port=WEB_PORT):
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ----------------------------------------------------------------- desktop window
class App:
    def __init__(self, root, tk, ttk, messagebox):
        self.tk, self.ttk, self.messagebox = tk, ttk, messagebox
        self.root = root
        self.engine = Engine()
        self.sources, self.speakers, self.mics = {}, {}, {}
        self.vars = []
        self.dirty = False
        root.title("Dual Speaker Mixer")
        root.resizable(False, False)

        top = ttk.Frame(root, padding=12)
        top.grid(sticky="nsew")

        src = ttk.LabelFrame(top, text="What to play", padding=10)
        src.grid(row=0, column=0, columnspan=N_OUT, sticky="ew")
        self.src_var = tk.StringVar(value=state["source"])
        self.src_box = ttk.Combobox(src, textvariable=self.src_var, width=62, state="readonly")
        self.src_box.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.src_box.bind("<<ComboboxSelected>>", lambda e: self._set_top("source", self.src_var.get(), restart=True))
        ttk.Button(src, text="Refresh devices", command=self.refresh_devices).grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.run_btn = ttk.Button(src, text="Start", command=self.toggle)
        self.run_btn.grid(row=1, column=1, pady=(8, 0))
        self.sync_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(src, text="Sync test (beeps)", variable=self.sync_var,
                        command=lambda: self._set_top("sync_test", self.sync_var.get())).grid(row=1, column=2, sticky="e", pady=(8, 0))

        ttk.Label(src, text="Master volume").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.master_var = tk.IntVar(value=state["master"])
        tk.Scale(src, from_=0, to=100, orient="horizontal", length=330, variable=self.master_var,
                 command=lambda v: self._set_top("master", int(float(v)))).grid(row=2, column=1, columnspan=2, sticky="ew")

        ttk.Label(src, text="Calibration mic").grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.mic_var = tk.StringVar(value=state["mic"])
        self.mic_box = ttk.Combobox(src, textvariable=self.mic_var, width=36, state="readonly")
        self.mic_box.grid(row=3, column=1, sticky="w", pady=(10, 0))
        self.mic_box.bind("<<ComboboxSelected>>", lambda e: self._set_top("mic", self.mic_var.get()))
        self.cal_btn = ttk.Button(src, text="Auto-calibrate delay", command=self.calibrate)
        self.cal_btn.grid(row=3, column=2, sticky="e", pady=(10, 0))
        self.cal_label = ttk.Label(src, text="", foreground="#555", wraplength=600)
        self.cal_label.grid(row=4, column=0, columnspan=3, sticky="w")

        for i in range(N_OUT):
            self._build_output(top, i)

        self.status = ttk.Label(top, text="", foreground="#555", wraplength=640, justify="left")
        self.status.grid(row=2, column=0, columnspan=N_OUT, sticky="w", pady=(10, 0))
        self.web_label = ttk.Label(top, text="", foreground="#2d6cdf")
        self.web_label.grid(row=3, column=0, columnspan=N_OUT, sticky="w")

        self.refresh_devices()
        self._start_web()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(250, self.poll)

    def _build_output(self, parent, i):
        tk, ttk = self.tk, self.ttk
        o = state["outputs"][i]
        f = ttk.LabelFrame(parent, text=f"Speaker {i + 1}", padding=10)
        f.grid(row=1, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0), pady=(10, 0))
        v = {
            "device": tk.StringVar(value=o["device"]),
            "enabled": tk.BooleanVar(value=o["enabled"]),
            "volume": tk.IntVar(value=o["volume"]),
            "mode": tk.StringVar(value=o["mode"]),
            "delay": tk.IntVar(value=o["delay"]),
            "muted": tk.BooleanVar(value=o["muted"]),
        }
        self.vars.append(v)
        box = ttk.Combobox(f, textvariable=v["device"], width=30, state="readonly")
        box.grid(row=0, column=0, columnspan=2, sticky="ew")
        box.bind("<<ComboboxSelected>>", lambda e: self._set_out(i, "device", v["device"].get(), restart=True))
        v["box"] = box
        ttk.Checkbutton(f, text="On", variable=v["enabled"],
                        command=lambda: self._set_out(i, "enabled", v["enabled"].get())).grid(row=1, column=0, sticky="w", pady=6)
        ttk.Checkbutton(f, text="Mute", variable=v["muted"],
                        command=lambda: self._set_out(i, "muted", v["muted"].get())).grid(row=1, column=1, sticky="w", pady=6)
        ttk.Label(f, text="Volume %").grid(row=2, column=0, sticky="w")
        tk.Scale(f, from_=0, to=150, orient="horizontal", length=200, variable=v["volume"],
                 command=lambda x: self._set_out(i, "volume", int(float(x)))).grid(row=2, column=1)
        ttk.Label(f, text="Delay ms").grid(row=3, column=0, sticky="w")
        tk.Scale(f, from_=0, to=MAX_DELAY, orient="horizontal", length=200, variable=v["delay"],
                 command=lambda x: self._set_out(i, "delay", int(float(x)))).grid(row=3, column=1)
        ttk.Label(f, text="Channel").grid(row=4, column=0, sticky="w")
        mo = ttk.Combobox(f, textvariable=v["mode"], values=MODES, state="readonly", width=20)
        mo.grid(row=4, column=1, sticky="w", pady=4)
        mo.bind("<<ComboboxSelected>>", lambda e: self._set_out(i, "mode", v["mode"].get()))

    # --- state changes from the window
    def _set_top(self, key, value, restart=False):
        with state_lock:
            state[key] = value
        self.dirty = True
        if restart and self.engine.running:
            self.start()

    def _set_out(self, i, key, value, restart=False):
        with state_lock:
            state["outputs"][i][key] = value
        self.dirty = True
        if restart and self.engine.running:
            self.start()

    # --- devices
    def refresh_devices(self):
        self.sources, self.mics = {}, {}
        try:
            for m in sc.all_microphones(include_loopback=True):
                if getattr(m, "isloopback", False):
                    self.sources["System audio of: " + m.name] = m
                else:
                    self.sources["Input: " + m.name] = m
                    if "CABLE" not in m.name:
                        self.mics[m.name] = m
            self.speakers = {s.name: s for s in sc.all_speakers()}
        except Exception as e:
            self.messagebox.showerror("Device error", str(e))
        self.src_box["values"] = list(self.sources)
        self.mic_box["values"] = list(self.mics)
        for v in self.vars:
            v["box"]["values"] = list(self.speakers)
        if not self.src_var.get():
            cable = [k for k in self.sources if "CABLE Output" in k]
            if cable:
                self.src_var.set(cable[0])
                self._set_top("source", cable[0])
        if not self.mic_var.get() and self.mics:
            try:
                name = sc.default_microphone().name
            except Exception:
                name = ""
            pick = name if name in self.mics else list(self.mics)[0]
            self.mic_var.set(pick)
            self._set_top("mic", pick)

    # --- engine control
    def toggle(self):
        if self.engine.running:
            self.stop()
        else:
            self.start()

    def start(self):
        with state_lock:
            src = state["source"]
            outs = [dict(o) for o in state["outputs"]]
        mic = self.sources.get(src)
        if mic is None:
            self._status("Pick what to play first (top box).")
            return
        targets, used = [], set()
        for i, o in enumerate(outs):
            spk = self.speakers.get(o["device"])
            if spk is None or o["device"] in used:
                continue
            if (getattr(mic, "isloopback", False) and mic.name == spk.name) or (
                    "CABLE Input" in spk.name and "CABLE Output" in mic.name):
                self._status(f"Speaker {i + 1} is the same device you're capturing from - that would echo forever. "
                             "Pick a different source (see the README).")
                return
            used.add(o["device"])
            targets.append((i, spk))
        if not targets:
            self._status("Pick at least one speaker.")
            return
        self.engine.start(mic, targets)
        with state_lock:
            state["running"] = True
        self.run_btn.config(text="Stop")

    def stop(self):
        self.engine.stop()
        with state_lock:
            state["running"] = False
        self.run_btn.config(text="Start")

    def calibrate(self):
        with state_lock:
            mic = self.mics.get(state["mic"])
        if mic is None:
            return self._calib_msg("Pick a calibration mic first (use the laptop's own mic, not a Bluetooth one).")
        if not self.engine.running:
            return self._calib_msg("Press Start first, with both speakers picked.")
        self._calib_msg("Calibrating... you'll hear chirps for about 10 seconds. Keep the room quiet.")

        def job():
            res = self.engine.calibrate(mic)
            self._calib_msg(res["msg"])
            self.dirty = True
        threading.Thread(target=job, daemon=True).start()

    def _calib_msg(self, text):
        with state_lock:
            state["calib"] = text

    def _status(self, text):
        with state_lock:
            state["status"] = text
        self.status.config(text=text)

    # --- web remote
    def _start_web(self):
        try:
            start_web()
            self.web_label.config(text=f"Phone remote (same Wi-Fi): http://{local_ip()}:{WEB_PORT}")
        except OSError as e:
            self.web_label.config(text=f"Phone remote off: {e}")

    # --- keep window, phone and engine in step
    def poll(self):
        with state_lock:
            pending, state["pending"] = state["pending"], None
            snap = {k: state[k] for k in ("master", "sync_test", "calib")}
            outs = [dict(o) for o in state["outputs"]]
        if pending == "start" and not self.engine.running:
            self.start()
        elif pending == "stop" and self.engine.running:
            self.stop()
        elif pending == "calibrate":
            self.calibrate()

        if self.master_var.get() != snap["master"]:
            self.master_var.set(snap["master"])
        if self.sync_var.get() != snap["sync_test"]:
            self.sync_var.set(snap["sync_test"])
        self.cal_label.config(text=snap["calib"])
        for v, o in zip(self.vars, outs):
            for k in ("enabled", "volume", "mode", "delay", "muted"):
                if v[k].get() != o[k]:
                    v[k].set(o[k])

        errs = self.engine.errors()
        was_running = state["running"]
        if was_running and not self.engine.running:
            with state_lock:
                state["running"] = False
            self.run_btn.config(text="Start")
        if errs:
            self._status("Problem: " + " | ".join(errs))
        elif self.engine.running:
            parts = []
            for w in self.engine.workers:
                parts.append(f"{outs[w.idx]['device']} (drift fix {w.drift_ppm:+.0f} ppm)")
            self._status("Playing to: " + ", ".join(parts))
        elif not was_running and not state["status"].startswith(("Pick", "Speaker")):
            self._status("Stopped")

        if self.dirty:
            save_settings()
            self.dirty = False
        self.root.after(250, self.poll)

    def on_close(self):
        save_settings()
        self.engine.stop()
        self.root.destroy()


def main():
    import tkinter as tk
    from tkinter import ttk, messagebox
    load_settings()
    root = tk.Tk()
    App(root, tk, ttk, messagebox)
    root.mainloop()


if __name__ == "__main__":
    main()
