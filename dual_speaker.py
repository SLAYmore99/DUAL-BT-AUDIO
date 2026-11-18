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
* Simple UI: opens as its own window on the PC (Edge app mode, built into Windows)
  and the same page works as a phone remote on your Wi-Fi.

Windows 10/11. Run DualSpeaker.exe, or with Python 3.9+:  pip install soundcard numpy
Ideas credited in README.md (erdongshuiyang/DualSpeaker, MIT).
"""
__version__ = "0.4.0"

import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
import warnings
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

try:
    import soundcard as sc
except ImportError:
    print("Missing package. Run:  python -m pip install soundcard numpy")
    sys.exit(1)

warnings.filterwarnings("ignore", category=getattr(sc, "SoundcardRuntimeWarning", RuntimeWarning))

SR = 48000            # sample rate used everywhere
BLOCK = 960           # 20 ms per block
DEV_BUF = SR // 10    # 100 ms device buffer: absorbs Python timing hiccups without crackles
RING_S = 12           # seconds kept in the shared buffer
LATENCY_MS = 150      # how far behind the newest captured audio each speaker reads (safety margin
                      # against PC hiccups; 60 ms was too tight on a real laptop)
MAX_DELAY = 1000      # ms, per-speaker delay range
MAX_CORR = 0.002      # max speed change for drift correction (0.2 %)
KP = 0.05             # drift correction: proportional gain (per second of error)
KI = KP * KP / 4      # integral gain (critically damped), removes steady drift fully
ERR_SMOOTH_S = 0.5    # smooth the buffer error so capture jitter doesn't wobble the pitch
REANCHOR_S = 0.25     # error bigger than this (e.g. after a stall): jump instead of slewing
RETRY_S = 2.0         # reconnect delay when a device drops (e.g. Bluetooth speaker turned off)
WEB_PORT = 8765
N_OUT = 2
# Settings live next to the .exe (or the script), not in PyInstaller's temp folder.
HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
SETTINGS = os.path.join(HERE, "dual_speaker_settings.json")
MODES = ["Stereo", "Left channel only", "Right channel only", "Mono (L+R)", "Swap L/R"]


def com_init():
    """Each audio thread on Windows needs COM initialised. Also ask Windows to schedule
    it as a "Pro Audio" thread (MMCSS), so busy moments don't starve the audio."""
    if sys.platform == "win32":
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0)
        except Exception:
            pass
        try:
            task = ctypes.c_ulong(0)
            ctypes.windll.avrt.AvSetMmThreadCharacteristicsW("Pro Audio", ctypes.byref(task))
        except Exception:
            pass


BT_MIC_WORDS = ("headset", "hands-free", "hands free", "ag audio", "cable")


def usable_mic(name):
    """Bluetooth speaker/headphone mics are excluded: opening one switches that device into
    low-quality hands-free (call) mode, which garbles its sound and breaks calibration."""
    n = name.lower()
    return not any(w in n for w in BT_MIC_WORDS)


def log(line):
    """Diagnostics log next to the app (dual_speaker_log.txt), rotated at 2 MB."""
    path = os.path.join(HERE, "dual_speaker_log.txt")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 2_000_000:
            os.replace(path, path + ".old")
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
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


# Sync-test tick: short and sharp (easy to hear whether it's one tick or two).
TICK_LEN = int(0.004 * SR)
_tt = np.arange(TICK_LEN) / SR
TICK = (0.7 * np.exp(-_tt / 0.0008) * (np.sin(2 * np.pi * 3000 * _tt) + 0.6 * np.sin(2 * np.pi * 5200 * _tt)) / 1.6
        ).astype(np.float32)

# Calibration chirps: each speaker gets its own frequency band so one mic recording
# can tell them apart; irregular gaps so the pattern can't line up with itself.
CHIRP_LEN = 0.09
CHIRP_OFFSETS = (0.0, 0.73, 1.61, 2.67, 3.91, 5.23)
CHIRP_BANDS = ((850.0, 1850.0), (2300.0, 3700.0))
CHIRP_LEVEL = 0.6


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
    heard = [sc_ > 4.0 for sc_ in scores]
    valid = all(heard) and spread < 4.0
    if valid:
        msg = f"Speaker 2 is {diff:+.0f} ms vs speaker 1 (spread {spread:.1f} ms)"
    elif not any(heard):
        msg = "The mic didn't hear either speaker. Turn them up and put the laptop between them, or use Line up by ear."
    elif not all(heard):
        msg = (f"The mic couldn't hear speaker {heard.index(False) + 1}. Turn it up or move it closer to the laptop, "
               "or use Line up by ear.")
    else:
        msg = "Too much echo or noise to measure reliably. Try again in a quieter spot, or use Line up by ear."
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
        self.max_gap = 0.0
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
            now = self.clock()
            if self.last_t is not None:
                self.max_gap = max(self.max_gap, now - self.last_t)
            self.last_t = now

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
        self.level = 0.0
        self.glitches = 0        # blocks with a hole in them (would click)
        self.late = 0            # calls that came so late the device buffer likely ran dry
        self.max_call_gap = 0.0
        self.last_call = None

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
        self.last_call = None
        self.starving = False
        self.err_f = 0.0

    @property
    def err_ms(self):
        return self.err_f * 1000.0

    def next_block(self):
        """Next BLOCK frames for this speaker. play() blocks while the device buffer is
        full, so this is called at the speaker's own clock rate."""
        ring = self.engine.ring
        now = time.perf_counter()
        if self.last_call is not None and self.prefill <= 0:
            gap = now - self.last_call
            self.max_call_gap = max(self.max_call_gap, gap)
            if gap > 0.9 * DEV_BUF / SR:
                self.late += 1
        self.last_call = now
        if self.prefill > 0:
            self.prefill -= BLOCK
            self.level = 0.0
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
            self.level = 0.0
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
        if missing:
            self.glitches += 1
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
        out = np.clip(out * gain, -1.0, 1.0).astype(np.float32)
        self.level = float(np.abs(out).max())
        return out


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
            data = np.zeros_like(data)
            ph = idx % (SR // 2)
            mask = ph < TICK_LEN
            if mask.any():
                data[mask] = TICK[ph[mask]][:, None]
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


# ----------------------------------------------------------------- user interface (desktop window + phone)
UI_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Dual Speaker</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--card2:#20242d;--line:#2a2f3a;--text:#e9ecf2;--dim:#8b93a3;--acc:#4f8cff;--ok:#33c27f;--warn:#f0b03d;--bad:#ef5a5a}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);font:15px/1.4 "Segoe UI",system-ui,-apple-system,Roboto,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:18px 18px 28px}
header{display:flex;align-items:center;gap:14px;margin-bottom:6px}
.logo{width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,#4f8cff,#7a5cff);display:grid;place-items:center;font-weight:700}
h1{font-size:19px;margin:0;font-weight:650;letter-spacing:.2px}
.grow{flex:1}
.status{color:var(--dim);font-size:13px;margin:0 0 16px 48px;min-height:18px}
.status.bad{color:var(--bad)}
button{font:inherit;color:var(--text);background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:8px 14px;cursor:pointer;transition:background .15s,transform .1s}
button:hover{background:#262b36}button:active{transform:scale(.97)}
button.primary{background:var(--acc);border-color:var(--acc);font-weight:600;padding:10px 22px;border-radius:999px}
button.primary.stop{background:#2b3140;border-color:#3a4252}
button.on{background:rgba(79,140,255,.18);border-color:var(--acc);color:#cfe0ff}
button.mute.on{background:rgba(239,90,90,.16);border-color:var(--bad);color:#ffd2d2}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:680px){.grid{grid-template-columns:1fr}.status{margin-left:0}}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px}
.card.off{opacity:.55}
.row{display:flex;align-items:center;gap:12px;margin:12px 0}
.row label{width:66px;color:var(--dim);font-size:13px}
.val{width:58px;text-align:right;font-variant-numeric:tabular-nums;color:var(--dim);font-size:13px}
.title{display:flex;align-items:center;gap:10px;margin-bottom:4px}
.title b{font-size:15px}
select{font:inherit;width:100%;background:var(--card2);color:var(--text);border:1px solid var(--line);border-radius:10px;padding:8px 10px}
input[type=range]{flex:1;-webkit-appearance:none;appearance:none;height:6px;border-radius:6px;background:var(--line);outline:none}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;background:#fff;border:3px solid var(--acc);cursor:pointer}
input[type=range]::-moz-range-thumb{width:14px;height:14px;border-radius:50%;background:#fff;border:3px solid var(--acc)}
.meter{height:6px;border-radius:6px;background:var(--card2);overflow:hidden;margin:10px 0 2px}
.meter i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--ok),var(--warn) 80%,var(--bad));transition:width .12s linear}
.seg{display:flex;background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:3px;flex:1}
.seg button{flex:1;border:0;background:none;padding:6px 4px;font-size:13px;border-radius:8px;color:var(--dim)}
.seg button.sel{background:#2f3646;color:var(--text)}
.bar{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.msg{color:var(--dim);font-size:13px;margin-top:8px;min-height:18px}
details{margin-top:14px}summary{cursor:pointer;color:var(--dim);font-size:13px;user-select:none}
.hint{background:rgba(240,176,61,.1);border:1px solid rgba(240,176,61,.35);border-radius:12px;padding:12px 14px;font-size:13px;margin-bottom:14px}
.hint ol{margin:6px 0 0 18px;padding:0}
.small{font-size:12px;color:var(--dim)}
.sw{position:relative;width:42px;height:24px;border-radius:24px;background:var(--line);border:0;padding:0;flex:none}
.sw::after{content:"";position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;transition:left .15s}
.sw.on{background:var(--acc)}.sw.on::after{left:21px}
.earbox{background:var(--card2);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-top:12px}
.earbox .bar button{padding:6px 10px}
h2{font-size:14px;margin:4px 0 10px;font-weight:600}
</style></head><body><div class="wrap">
<header><div class="logo">2</div><h1>Dual Speaker <span class="small" id="ver"></span></h1><div class="grow"></div>
<button id="quit" class="small" style="display:none" title="Stop audio and close the app">Quit</button>
<button id="toggle" class="primary">Start</button></header>
<div id="status" class="status"></div>
<div id="hint" class="hint" style="display:none"><b>One-time setup:</b> install VB-CABLE (free, vb-audio.com/Cable), restart the PC, then
<ol><li>Windows Sound settings &rarr; Output &rarr; <b>CABLE Input</b></li><li>Press <b>Refresh devices</b> below</li></ol></div>
<div class="grid" id="outs"></div>
<div class="card" style="margin-top:14px">
<div class="row"><label>Master</label><input id="master" type="range" min="0" max="100"><span class="val" id="masterv"></span></div>
<h2>Line the speakers up</h2>
<div class="bar"><button id="ear">Line up by ear</button><button id="cal">Auto (laptop mic)</button>
<span class="small">By ear works everywhere; Auto needs the laptop's own mic to hear both speakers.</span></div>
<div id="earbox" class="earbox" style="display:none">
<p class="small" style="margin:0 0 4px">Both speakers now tick twice a second (music is muted). Move the slider until you hear
<b>one</b> tick instead of two, then fine-tune with the small buttons.</p>
<div class="row"><label>Offset</label><input id="off" type="range" min="-1000" max="1000" step="1"><span class="val" id="offv"></span></div>
<div class="bar"><span class="small" id="offtxt"></span><span class="grow"></span>
<button data-d="-10">&minus;10</button><button data-d="-1">&minus;1</button><button data-d="1">+1</button><button data-d="10">+10</button>
<button id="earok" class="primary" style="padding:8px 18px">Done</button></div></div>
<div class="msg" id="calmsg"></div>
<details id="setup"><summary>Setup</summary>
<div class="row"><label>Play</label><select id="source"></select></div>
<div class="row"><label>Mic</label><select id="mic"></select></div>
<div class="bar"><button id="refresh">Refresh devices</button></div>
<p class="small" id="diag"></p>
<p class="small" id="remote"></p>
</details></div>
</div>
<script>
const MODES=[["Stereo","Stereo"],["Left channel only","Left"],["Right channel only","Right"],["Mono (L+R)","Mono"],["Swap L/R","Swap"]];
let S=null,busy=0,lev=[0,0];
const $=id=>document.getElementById(id);
function post(u,b){return fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)}).then(poll)}
function set(path,value){return post("/set",{path,value})}
function cmd(c){return post("/cmd",{cmd:c})}
function paint(el){const p=(el.value-el.min)/(el.max-el.min)*100;
 if(+el.min<0){const a=Math.min(p,50),b=Math.max(p,50);el.style.background=`linear-gradient(90deg,var(--line) ${a}%,var(--acc) ${a}%,var(--acc) ${b}%,var(--line) ${b}%)`;return}
 el.style.background=`linear-gradient(90deg,var(--acc) ${p}%,var(--line) ${p}%)`}
document.addEventListener("input",e=>{if(e.target.type==="range")paint(e.target)});
function hold(el){el.addEventListener("pointerdown",()=>busy++);el.addEventListener("pointerup",()=>setTimeout(()=>busy=Math.max(0,busy-1),300));}
function fill(sel,items,value,blank){const want=JSON.stringify([items,value]);if(sel.dataset.k===want||document.activeElement===sel)return;sel.dataset.k=want;
 sel.innerHTML=(blank?`<option value="">${blank}</option>`:"")+items.map(n=>`<option>${n.replace(/</g,"&lt;")}</option>`).join("");sel.value=value||"";}
function build(){
 const box=$("outs");box.innerHTML="";
 S.outputs.forEach((o,i)=>{const c=document.createElement("div");c.className="card";c.id="card"+i;c.innerHTML=`
 <div class="title"><b>Speaker ${i+1}</b><span class="grow"></span><button class="mute" id="mu${i}">Mute</button><button class="sw" id="en${i}" aria-label="On"></button></div>
 <select id="dev${i}"></select>
 <div class="meter"><i id="lv${i}"></i></div>
 <div class="row"><label>Volume</label><input id="vol${i}" type="range" min="0" max="150"><span class="val" id="volv${i}"></span></div>
 <div class="row"><label>Delay</label><input id="del${i}" type="range" min="0" max="1000" step="5"><span class="val" id="delv${i}"></span></div>
 <div class="row"><label>Channel</label><div class="seg" id="mo${i}">${MODES.map(([v,t])=>`<button data-v="${v}">${t}</button>`).join("")}</div></div>`;
 box.appendChild(c);
 const vol=$("vol"+i),del=$("del"+i);hold(vol);hold(del);
 vol.oninput=()=>{$("volv"+i).textContent=vol.value+"%";set(["outputs",i,"volume"],+vol.value)};
 del.oninput=()=>{$("delv"+i).textContent=del.value+" ms";set(["outputs",i,"delay"],+del.value)};
 $("dev"+i).onchange=e=>set(["outputs",i,"device"],e.target.value);
 $("en"+i).onclick=()=>set(["outputs",i,"enabled"],!S.outputs[i].enabled);
 $("mu"+i).onclick=()=>set(["outputs",i,"muted"],!S.outputs[i].muted);
 $("mo"+i).querySelectorAll("button").forEach(b=>b.onclick=()=>set(["outputs",i,"mode"],b.dataset.v));});}
function render(){
 const t=$("toggle");t.textContent=S.running?"Stop":"Start";t.classList.toggle("stop",S.running);
 const st=$("status");st.textContent=S.status;st.classList.toggle("bad",/^Problem|echo|Pick/.test(S.status));
 $("quit").style.display=S.local?"":"none";$("ver").textContent=S.version?("v"+S.version):"";
 $("hint").style.display=S.needs_cable?"":"none";
 $("calmsg").textContent=S.calib;$("cal").disabled=S.calibrating;$("cal").textContent=S.calibrating?"Listening…":"Auto (laptop mic)";
 $("earbox").style.display=S.sync_test?"":"none";$("ear").classList.toggle("on",S.sync_test);
 const off=S.outputs[0].delay-S.outputs[1].delay;if(!busy)$("off").value=off;paint($("off"));offLabel(off);
 $("diag").textContent=S.running?("Sound check: "+S.glitches.map((g,i)=>g===null?null:`Speaker ${i+1} ${g} glitch${g===1?"":"es"}`).filter(Boolean).join(" · ")+" (details: dual_speaker_log.txt next to the app)"):"";
 if(!busy)$("master").value=S.master;paint($("master"));$("masterv").textContent=S.master+"%";
 fill($("source"),S.devices.sources,S.source,"Choose what to play…");fill($("mic"),S.devices.mics,S.mic,"Choose a mic…");
 $("remote").textContent=S.remote?("Phone remote (same Wi-Fi): "+S.remote):"";
 S.outputs.forEach((o,i)=>{
  fill($("dev"+i),S.devices.speakers,o.device,"Choose a speaker…");
  $("card"+i).classList.toggle("off",!o.enabled);
  $("en"+i).classList.toggle("on",o.enabled);
  const mu=$("mu"+i);mu.classList.toggle("on",o.muted);mu.textContent=o.muted?"Muted":"Mute";
  if(!busy){$("vol"+i).value=o.volume;$("del"+i).value=o.delay}
  paint($("vol"+i));paint($("del"+i));
  $("volv"+i).textContent=o.volume+"%";$("delv"+i).textContent=o.delay+" ms";
  $("mo"+i).querySelectorAll("button").forEach(b=>b.classList.toggle("sel",b.dataset.v===o.mode));
  lev[i]=Math.max((S.levels[i]||0),lev[i]*0.6);$("lv"+i).style.width=Math.min(100,Math.sqrt(lev[i])*100)+"%";});}
async function poll(){try{const r=await fetch("/state",{cache:"no-store"});const n=await r.json();const first=!S;S=n;if(first)build();render()}catch(e){$("status").textContent="Can't reach the app. Is it still running on the PC?"}}
const m=$("master");hold(m);m.oninput=()=>{$("masterv").textContent=m.value+"%";set(["master"],+m.value)};
$("toggle").onclick=()=>cmd(S&&S.running?"stop":"start");
$("cal").onclick=()=>cmd("calibrate");
$("refresh").onclick=()=>cmd("refresh");
$("quit").onclick=()=>{cmd("quit");document.body.innerHTML='<div class="wrap"><h1>Dual Speaker closed</h1><p class="small">You can close this window.</p></div>'};
function offLabel(v){$("offv").textContent=(v>0?"+":"")+v+" ms";
 $("offtxt").textContent=v>0?`Speaker 1 waits ${v} ms`:v<0?`Speaker 2 waits ${-v} ms`:"No delay on either speaker";}
const off=$("off");hold(off);off.oninput=()=>{offLabel(+off.value);set(["offset"],+off.value)};
document.querySelectorAll("#earbox button[data-d]").forEach(b=>b.onclick=()=>{
 const v=Math.max(-1000,Math.min(1000,(S.outputs[0].delay-S.outputs[1].delay)+(+b.dataset.d)));set(["offset"],v)});
$("ear").onclick=()=>set(["sync_test"],!S.sync_test);
$("earok").onclick=()=>set(["sync_test"],false);
$("source").onchange=e=>set(["source"],e.target.value);
$("mic").onchange=e=>set(["mic"],e.target.value);
addEventListener("pagehide",()=>navigator.sendBeacon("/cmd",JSON.stringify({cmd:"bye"})));
poll();setInterval(poll,500);
</script></body></html>"""


def apply_set(path, value):
    """Validate and apply a numeric/flag change (from the UI or phone)."""
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


class Controller:
    """Everything the UI can do: devices, start/stop, calibrate, settings, quitting."""

    BYE_GRACE_S = 4.0   # window closed (not just reloaded) -> quit after this

    def __init__(self):
        self.engine = Engine()
        self.sources, self.speakers, self.mics = {}, {}, {}
        self.dirty = False
        self.quit_evt = threading.Event()
        self.bye_at = None
        self.remote = ""
        self.cal_thread = None
        self.next_log = 0.0
        self.last_written = (0, time.perf_counter())
        self.last_err = ""
        self.prev = {}

    # --- devices
    def refresh_devices(self):
        sources, mics, speakers = {}, {}, {}
        try:
            for m in sc.all_microphones(include_loopback=True):
                if getattr(m, "isloopback", False):
                    sources["System audio of: " + m.name] = m
                else:
                    sources["Input: " + m.name] = m
                    if usable_mic(m.name):
                        mics[m.name] = m
            speakers = {s.name: s for s in sc.all_speakers()}
        except Exception as e:
            self._status(f"Problem: couldn't list audio devices ({e})")
        self.sources, self.mics, self.speakers = sources, mics, speakers
        with state_lock:
            if not state["source"] or state["source"] not in sources:
                cable = [k for k in sources if "CABLE Output" in k]
                if cable:
                    state["source"] = cable[0]
            if not state["mic"] or state["mic"] not in mics:
                try:
                    name = sc.default_microphone().name
                except Exception:
                    name = ""
                built_in = [k for k in mics if any(w in k.lower() for w in ("array", "realtek", "intel", "internal"))]
                state["mic"] = name if name in mics else (built_in or list(mics) or [""])[0]
        self.dirty = True

    def public_state(self, local=False):
        with state_lock:
            pub = {k: state[k] for k in ("master", "running", "status", "sync_test", "calib", "source", "mic")}
            pub["outputs"] = [dict(o) for o in state["outputs"]]
        levels, glitches = [0.0] * N_OUT, [None] * N_OUT
        for w in self.engine.workers:
            levels[w.idx] = round(float(getattr(w, "level", 0.0)), 3)
            glitches[w.idx] = int(w.glitches + w.late)
        pub.update(version=__version__, levels=levels, glitches=glitches, local=local, remote=self.remote, calibrating=self.engine.calibrating,
                   needs_cable=not any("CABLE Output" in k for k in self.sources),
                   devices={"sources": list(self.sources), "speakers": list(self.speakers), "mics": list(self.mics)})
        return pub

    # --- changes from the UI
    def set(self, path, value):
        restart = False
        if path == ["offset"]:   # "Line up by ear": >0 delays speaker 1, <0 delays speaker 2
            try:
                v = int(max(-MAX_DELAY, min(MAX_DELAY, int(value))))
            except (TypeError, ValueError):
                return
            with state_lock:
                state["outputs"][0]["delay"], state["outputs"][1]["delay"] = max(v, 0), max(-v, 0)
            self.dirty = True
            return
        if path == ["sync_test"] and value and not self.engine.running:
            self._calib("Press Start first, then Line up by ear.")
            return
        if path in (["source"], ["mic"]) and isinstance(value, str):
            pool = self.sources if path == ["source"] else self.mics
            if value in pool or value == "":
                with state_lock:
                    state[path[0]] = value
                restart = path == ["source"]
        elif len(path) == 3 and path[0] == "outputs" and path[2] == "device":
            try:
                i = int(path[1])
            except (TypeError, ValueError):
                return
            if i in range(N_OUT) and isinstance(value, str) and (value in self.speakers or value == ""):
                with state_lock:
                    state["outputs"][i]["device"] = value
                restart = True
        else:
            apply_set(path, value)
        self.dirty = True
        if restart and self.engine.running:
            self.start()

    def command(self, cmd, local=False):
        if cmd == "start" and not self.engine.running:
            self.start()
        elif cmd == "stop" and self.engine.running:
            self.stop()
        elif cmd == "calibrate":
            self.calibrate()
        elif cmd == "refresh":
            self.refresh_devices()
        elif cmd == "quit" and local:
            self.quit_evt.set()
        elif cmd == "bye" and local:
            self.bye_at = time.time() + self.BYE_GRACE_S

    def saw_local_ui(self):
        self.bye_at = None

    # --- engine
    def start(self):
        with state_lock:
            src = state["source"]
            outs = [dict(o) for o in state["outputs"]]
        mic = self.sources.get(src)
        if mic is None:
            return self._status("Pick what to play first (Setup, below).")
        targets, used = [], set()
        for i, o in enumerate(outs):
            spk = self.speakers.get(o["device"])
            if spk is None or o["device"] in used:
                continue
            if (getattr(mic, "isloopback", False) and mic.name == spk.name) or (
                    "CABLE Input" in spk.name and "CABLE Output" in mic.name):
                return self._status(f"Speaker {i + 1} is the device you're playing from - that would echo forever. "
                                    "Pick your real speaker instead.")
            used.add(o["device"])
            targets.append((i, spk))
        if not targets:
            return self._status("Pick at least one speaker.")
        self.engine.start(mic, targets)
        with state_lock:
            state["running"] = True
        log(f"START v{__version__} source={src!r} speakers={[ (i, s.name) for i, s in targets]} "
            f"mic={state['mic']!r} outputs={outs}")
        self.next_log = time.time() + 5
        self.last_written = (0, time.perf_counter())
        self.prev = {}
        self.tick()

    def stop(self):
        if self.engine.running:
            self.write_stats()
            log("STOP")
        self.engine.stop()
        with state_lock:
            state["running"] = False
        self._status("Stopped")

    def calibrate(self):
        with state_lock:
            mic = self.mics.get(state["mic"])
        if self.engine.calibrating:
            return
        if mic is None:
            return self._calib("Pick a calibration mic in Setup (the laptop's own mic, not a Bluetooth one).")
        if not self.engine.running or len(self.engine.workers) < 2:
            return self._calib("Press Start with both speakers picked first.")
        self._calib("Calibrating... you'll hear chirps for about 10 seconds. Keep the room quiet.")

        def job():
            res = self.engine.calibrate(mic)
            log(f"CALIBRATE mic={mic.name!r} valid={res.get('valid')} diff_ms={res.get('diff_ms')} "
                f"spread_ms={res.get('spread_ms')} scores={res.get('scores')} -> {res.get('msg')}")
            self._calib(res["msg"])
            self.dirty = True
        self.cal_thread = threading.Thread(target=job, daemon=True)
        self.cal_thread.start()

    def _calib(self, text):
        with state_lock:
            state["calib"] = text

    def _status(self, text):
        with state_lock:
            state["status"] = text

    # --- background loop
    def tick(self):
        errs = self.engine.errors()
        with state_lock:
            if state["running"] and not self.engine.running:
                state["running"] = False
            running = state["running"]
            outs = [dict(o) for o in state["outputs"]]
            status = state["status"]
        err_text = " | ".join(errs)
        if err_text and err_text != self.last_err:
            log("ERROR " + err_text)
        self.last_err = err_text
        if running and time.time() >= self.next_log:
            self.next_log = time.time() + 5
            self.write_stats()
        if errs:
            self._status("Problem: " + " | ".join(errs))
        elif running:
            names = [outs[w.idx]["device"] for w in self.engine.workers]
            self._status("Playing to " + " and ".join(names))
        elif status.startswith(("Playing", "Problem")):
            self._status("Stopped")
        if self.dirty:
            self.dirty = False
            save_settings()
        if self.bye_at is not None and time.time() > self.bye_at:
            self.quit_evt.set()

    def write_stats(self):
        """One log line: capture health plus each speaker's health since the last line."""
        ring = self.engine.ring
        w0, t0 = self.last_written
        now = time.perf_counter()
        rate = (ring.written - w0) / max(1e-6, now - t0)
        self.last_written = (ring.written, now)
        parts = [f"capture {rate:.0f} Hz maxgap {ring.max_gap * 1000:.0f} ms"]
        ring.max_gap = 0.0
        with state_lock:
            outs = [dict(o) for o in state["outputs"]]
        for w in self.engine.workers:
            p = self.prev.get(w.idx, (0, 0, 0))
            parts.append(f"S{w.idx + 1} {outs[w.idx]['device']!r}: glitch +{w.glitches - p[0]} late +{w.late - p[1]} "
                         f"missing +{w.missing - p[2]} maxcall {w.max_call_gap * 1000:.0f} ms lag {w.lag_ms:.0f} ms "
                         f"drift {w.drift_ppm:+.0f} ppm resyncs {w.resyncs} level {w.level:.2f} "
                         f"vol {outs[w.idx]['volume']} delay {outs[w.idx]['delay']}"
                         f"{' OFF' if not outs[w.idx]['enabled'] else ''}{' MUTED' if outs[w.idx]['muted'] else ''}")
            self.prev[w.idx] = (w.glitches, w.late, w.missing)
            w.max_call_gap = 0.0
        log("STATS " + " | ".join(parts))

    def run_loop(self):
        while not self.quit_evt.wait(0.25):
            self.tick()

    def shutdown(self):
        self.engine.stop()
        save_settings()


CTRL = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _local(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

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
            self._send(200, UI_PAGE, "text/html; charset=utf-8")
        elif self.path == "/state":
            local = self._local()
            if local:
                CTRL.saw_local_ui()
            self._send(200, json.dumps(CTRL.public_state(local)))
        else:
            self._send(404, "{}")

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(min(n, 10000)) or b"{}")
        except Exception:
            return self._send(400, "{}")
        if self.path == "/set" and isinstance(data.get("path"), list):
            CTRL.set(data["path"], data.get("value"))
        elif self.path == "/cmd" and isinstance(data.get("cmd"), str):
            CTRL.command(data["cmd"], local=self._local())
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


def open_window(url):
    """Open the UI as its own app window (Microsoft Edge app mode, built into Windows);
    fall back to the default browser. Uses the person's existing Edge profile: a fresh
    profile makes Edge auto sign-in to the Microsoft account and pop up a sync notice."""
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", "")):
            edge = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            if base and os.path.exists(edge):
                try:
                    subprocess.Popen([edge, f"--app={url}", "--window-size=1000,900"])
                    return
                except OSError:
                    pass
    webbrowser.open(url)


def fatal(msg):
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(None, msg, "Dual Speaker", 0x10)
            return
        except Exception:
            pass
    print(msg)


def main():
    global CTRL
    load_settings()
    CTRL = Controller()
    url = f"http://127.0.0.1:{WEB_PORT}"
    try:
        srv = start_web()
    except OSError:
        open_window(url)   # already running: just show its window
        return
    CTRL.remote = f"http://{local_ip()}:{WEB_PORT}"
    if sys.platform == "win32":
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)   # 1 ms timer: steadier audio thread wake-ups
        except Exception:
            pass
    CTRL.refresh_devices()
    log(f"APP v{__version__} sources={list(CTRL.sources)} speakers={list(CTRL.speakers)} mics={list(CTRL.mics)}")
    open_window(url)
    try:
        CTRL.run_loop()
    except KeyboardInterrupt:
        pass
    finally:
        CTRL.shutdown()
        srv.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # windowed exe has no console: show the error
        fatal(f"Dual Speaker stopped because of an error:\n\n{e!r}")
