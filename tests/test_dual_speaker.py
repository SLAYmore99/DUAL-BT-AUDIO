"""Tests for dual_speaker.py that run without Windows, speakers or tkinter.

* A fake `soundcard` module stands in for the real one.
* Clock drift and calibration are tested with a simulated clock (fast, deterministic):
  a capture device and two speakers each run at their own rate, with their own
  output latency, and a simulated mic hears both speakers with echo and noise.
* The threaded engine is run for real against fake devices (including a device that
  fails and reconnects).
* The phone remote is tested over HTTP, and the window logic with a fake tkinter.

Run:  python -m pytest -q tests   (or: python tests/test_dual_speaker.py)
"""
import json
import os
import sys
import threading
import time
import types
import urllib.request

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


# ----------------------------------------------------------------- fake soundcard
class FakeRecorder:
    def __init__(self, dev):
        self.dev, self.t0, self.n = dev, None, 0

    def __enter__(self):
        if self.dev.fail_opens > 0:
            self.dev.fail_opens -= 1
            raise RuntimeError("device unplugged")
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *a):
        pass

    def record(self, numframes):
        # pace like a real device
        due = self.t0 + (self.n + numframes) / 48000
        time.sleep(max(0.0, due - time.perf_counter()))
        t = (np.arange(numframes) + self.n) / 48000
        self.n += numframes
        x = 0.3 * np.sin(2 * np.pi * 440 * t)
        return np.stack([x, -x], axis=1).astype(np.float32)


class FakePlayer:
    def __init__(self, dev, blocksize):
        self.dev, self.buf, self.fill, self.t = dev, blocksize, 0.0, None

    def __enter__(self):
        if self.dev.fail_opens > 0:
            self.dev.fail_opens -= 1
            raise RuntimeError("device unplugged")
        self.t = time.perf_counter()
        return self

    def __exit__(self, *a):
        pass

    def play(self, data):
        while True:  # block while the buffer is full, like WASAPI
            now = time.perf_counter()
            self.fill = max(0.0, self.fill - (now - self.t) * 48000)
            self.t = now
            if self.fill + len(data) <= self.buf:
                break
            time.sleep(0.002)
        self.fill += len(data)
        self.dev.played.append(np.array(data))


class FakeDevice:
    def __init__(self, name, isloopback=False, fail_opens=0):
        self.name, self.isloopback, self.fail_opens, self.played = name, isloopback, fail_opens, []
        self.channels = 2

    def recorder(self, samplerate, channels=None, blocksize=None):
        return FakeRecorder(self)

    def player(self, samplerate, channels=None, blocksize=None):
        return FakePlayer(self, blocksize)


DEVICES = {
    "mics": [FakeDevice("CABLE Output (VB-Audio Virtual Cable)"), FakeDevice("Microphone Array (Realtek)")],
    "loop": [FakeDevice("Speakers (Realtek)", True)],
    "spk": [FakeDevice("JBL Flip 5"), FakeDevice("boAt Stone 350"), FakeDevice("CABLE Input (VB-Audio Virtual Cable)"),
            FakeDevice("Speakers (Realtek)")],
}
fake_sc = types.ModuleType("soundcard")
fake_sc.SoundcardRuntimeWarning = RuntimeWarning
fake_sc.all_microphones = lambda include_loopback=False: (DEVICES["loop"] if include_loopback else []) + DEVICES["mics"]
fake_sc.all_speakers = lambda: DEVICES["spk"]
fake_sc.default_microphone = lambda: DEVICES["mics"][1]
sys.modules["soundcard"] = fake_sc

import dual_speaker as ds  # noqa: E402

SR, BLOCK = ds.SR, ds.BLOCK
REAL_SAVE = ds.save_settings


def reset_state():
    with ds.state_lock:
        ds.state.update(source="", mic="", master=100, running=False, status="Stopped", calib="",
                        pending=None, sync_test=False)
        for o in ds.state["outputs"]:
            o.update(device="", enabled=True, volume=100, mode="Stereo", delay=0, muted=False)


# ----------------------------------------------------------------- simulated clock
def simulate(seconds, cap_ppm=0.0, spk_ppm=(0.0, 0.0), latency=(0.0, 0.0), signal=None,
             calib_at=None, step_ms=1.0, stall=None, events=()):
    """Run the engine's buffer + two OutputWorkers on a simulated clock.
    Returns (engine, workers, outputs) where outputs[i] is a list of (arrival_time, block)."""
    eng = ds.Engine()
    eng.ring = ds.Ring()
    clock = {"t": 0.0}
    eng.ring.clock = lambda: clock["t"]
    workers = [ds.OutputWorker(i, None, eng) for i in range(2)]
    for w in workers:
        w.open_stream()
    rate_c = SR * (1 + cap_ppm * 1e-6)
    rates = [SR * (1 + p * 1e-6) for p in spk_ppm]
    fill = [0.0, 0.0]
    underrun = [0.0, 0.0]
    outputs = [[], []]
    next_cap, n_cap, t = 0.0, 0, 0.0
    dt = step_ms / 1000.0
    events = sorted(events)
    while t < seconds:
        while events and events[0][0] <= t:
            events.pop(0)[1]()
        if stall and stall[0] <= t < stall[0] + stall[1]:   # source stops delivering audio
            next_cap = t + dt
        clock["t"] = next_cap
        while next_cap <= t:
            clock["t"] = next_cap
            idx = np.arange(BLOCK) + n_cap
            x = signal(idx / SR) if signal else np.zeros(BLOCK)
            eng.ring.write(np.stack([x, x], axis=1).astype(np.float32))
            n_cap += BLOCK
            next_cap += BLOCK / rate_c
        clock["t"] = t
        if calib_at is not None and eng.calib_start is None and t >= calib_at:
            eng.calib_start = eng.ring.written + 1.0 * SR
        for i, w in enumerate(workers):
            drain = dt * rates[i]
            if t > 0.5 and fill[i] < drain:          # count underruns after startup
                underrun[i] += drain - fill[i]
            fill[i] = max(0.0, fill[i] - drain)
            while fill[i] + BLOCK <= ds.DEV_BUF:
                blk = w.next_block()
                outputs[i].append((t + fill[i] / rates[i] + latency[i], blk, rates[i]))
                fill[i] += BLOCK
        t += dt
    eng.underrun = underrun
    return eng, workers, outputs


def render_mic(outputs, seconds, gains=(1.0, 0.4), noise=0.01, echo=True, seed=1):
    rng = np.random.default_rng(seed)
    rec = np.zeros(int(seconds * SR) + SR)
    for i, outs in enumerate(outputs):
        for arr, blk, rate in outs:
            s = int(round(arr * SR))
            if 0 <= s < len(rec) - BLOCK:
                rec[s:s + BLOCK] += gains[i] * blk[:, 0]
    if echo:  # simple room: a few reflections
        out = rec.copy()
        for d_ms, g in ((7, 0.5), (19, 0.3), (41, 0.2)):
            k = int(d_ms * SR / 1000)
            out[k:] += g * rec[:-k]
        rec = out
    return rec + noise * rng.standard_normal(len(rec))


# ----------------------------------------------------------------- tests
def test_ring_wraps_and_interpolates():
    r = ds.Ring(seconds=1)
    ramp = np.arange(SR * 2.5, dtype=np.float32) / SR
    for k in range(0, len(ramp), 777):
        part = ramp[k:k + 777]
        r.write(np.stack([part, part], 1))
    out, missing = r.read_at(r.written - 1000.5, 1.0, 500)
    assert missing == 0
    expect = (np.arange(500) + r.written - 1000.5) / SR
    assert np.allclose(out[:, 0], expect, atol=1e-6)
    _, missing = r.read_at(r.written - 100, 1.0, 500)        # runs past the end
    assert missing > 0
    _, missing = r.read_at(0, 1.0, 10)                       # already overwritten
    assert missing == 10


def test_modes():
    x = np.array([[1.0, 0.0]], np.float32)
    assert ds.apply_mode(x, "Left channel only").tolist() == [[1, 1]]
    assert ds.apply_mode(x, "Right channel only").tolist() == [[0, 0]]
    assert ds.apply_mode(x, "Mono (L+R)").tolist() == [[0.5, 0.5]]
    assert ds.apply_mode(x, "Swap L/R").tolist() == [[0, 1]]
    assert ds.apply_mode(x, "Stereo").tolist() == [[1, 0]]


def test_corrected_delays():
    assert ds.corrected_delays(0, 0, 180) == (180, 0)      # speaker 2 later -> delay speaker 1
    assert ds.corrected_delays(0, 0, -95) == (0, 95)
    assert ds.corrected_delays(100, 0, -30) == (70, 0)     # partial fix keeps min at 0
    assert ds.corrected_delays(0, 0, 5000) == (1000, 0)    # clamped


def test_drift_is_corrected_without_dropouts():
    """Capture clock +300 ppm, speakers -250 / +400 ppm (worse than real hardware),
    10 simulated minutes: no underruns after startup, no resyncs, lag settles."""
    reset_state()
    sig = lambda t: 0.5 * np.sin(2 * np.pi * 220 * t)
    eng, ws, outs = simulate(600, cap_ppm=300, spk_ppm=(-250, 400), signal=sig, step_ms=2)
    for i, w in enumerate(ws):
        assert w.resyncs <= 2, (i, w.resyncs)               # only at startup
        assert eng.underrun[i] < BLOCK, (i, eng.underrun[i])
        assert abs(w.lag_ms - ds.LATENCY_MS) < 3, (i, w.lag_ms)
        # corrections match the clock mismatch (capture vs speaker)
        expect = 300 - (-250 if i == 0 else 400)
        assert abs(w.drift_ppm - expect) < 80, (i, w.drift_ppm, expect)
        # output is a smooth sine: no clicks after the first second
        y = np.concatenate([b[:, 0] for _, b, _ in outs[i]])[SR:]
        assert np.max(np.abs(np.diff(y))) < 0.02, i
    # the two speakers stay aligned to each other: compare where each is on the timeline
    assert abs(ws[0].lag_ms - ws[1].lag_ms) < 3


def test_stall_recovers():
    """Source stalls for 400 ms (e.g. the PC hiccups): both speakers re-anchor and end up
    aligned again, without staying offset from each other."""
    reset_state()
    sig = lambda t: 0.5 * np.sin(2 * np.pi * 220 * t)
    eng, ws, outs = simulate(40, spk_ppm=(-50, 80), signal=sig, stall=(5.0, 0.4), step_ms=2)
    for w in ws:
        assert 1 <= w.resyncs <= 3, w.resyncs
        assert abs(w.lag_ms - ds.LATENCY_MS) < 3, w.lag_ms
    assert abs(ws[0].lag_ms - ws[1].lag_ms) < 3


def test_delay_change_while_playing():
    reset_state()
    sig = lambda t: 0.5 * np.sin(2 * np.pi * 220 * t)

    def change():
        with ds.state_lock:
            ds.state["outputs"][0]["delay"] = 200
    eng, ws, outs = simulate(20, signal=sig, events=[(3.0, change)], step_ms=2)
    assert abs((ws[0].lag_ms - ws[1].lag_ms) - 200) < 3, (ws[0].lag_ms, ws[1].lag_ms)
    assert ws[0].resyncs <= 1


def test_phone_page_script_is_valid_js():
    import re
    import shutil
    import subprocess
    import tempfile
    node = shutil.which("node")
    if not node:
        return
    js = re.search(r"<script>(.*)</script>", ds.UI_PAGE, re.S).group(1)
    path = os.path.join(tempfile.mkdtemp(), "page.js")
    open(path, "w").write(js)
    r = subprocess.run([node, "--check", path], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_volume_mute_and_delay_change():
    reset_state()
    sig = lambda t: 0.5 * np.ones_like(t)
    with ds.state_lock:
        ds.state["master"] = 50
        ds.state["outputs"][0]["volume"] = 80
        ds.state["outputs"][1]["muted"] = True
    eng, ws, outs = simulate(3, signal=sig)
    a = outs[0][-1][1]
    b = outs[1][-1][1]
    assert np.allclose(a, 0.5 * 0.8 * 0.5, atol=1e-4)
    assert np.allclose(b, 0.0)


def test_calibration_end_to_end():
    """Speaker 1 has 250 ms output latency, speaker 2 has 70 ms (typical BT vs wired),
    plus echo and noise. Calibration should find -180 ms and fix it."""
    reset_state()
    lat = (0.250, 0.070)
    eng, ws, outs = simulate(12, latency=lat, calib_at=0.5)
    rec = render_mic(outs, 12)
    res = ds.measure_offset(rec)
    assert res["valid"], res
    assert abs(res["diff_ms"] - (-180)) < 2, res
    d1, d2 = ds.corrected_delays(0, 0, res["diff_ms"])
    assert (d1, d2) == (0, 180)
    # verify: with the new delays both arrive together
    with ds.state_lock:
        ds.state["outputs"][1]["delay"] = d2
    eng, ws, outs = simulate(12, latency=lat, calib_at=0.5)
    res2 = ds.measure_offset(render_mic(outs, 12, seed=7))
    assert res2["valid"] and abs(res2["diff_ms"]) < 2, res2


def test_calibration_rejects_silence_and_one_speaker():
    reset_state()
    rng = np.random.default_rng(3)
    assert not ds.measure_offset(0.01 * rng.standard_normal(SR * 10))["valid"]
    eng, ws, outs = simulate(12, latency=(0.1, 0.1), calib_at=0.5)
    rec = render_mic(outs, 12, gains=(1.0, 0.0), noise=0.02)    # speaker 2 not heard
    assert not ds.measure_offset(rec)["valid"]


def test_engine_calibrate_sets_delays():
    reset_state()
    eng = ds.Engine()
    eng.workers = [types.SimpleNamespace(idx=0), types.SimpleNamespace(idx=1)]
    _, _, outs = simulate(12, latency=(0.12, 0.30), calib_at=0.5)
    rec = render_mic(outs, 12)
    res = eng.calibrate(None, record_fn=lambda dur: rec)
    assert res["valid"], res
    assert res["delays"][1] == 0 and abs(res["delays"][0] - 180) <= 2, res
    assert eng.calib_start is None and not eng.calibrating


def test_threaded_engine_with_fake_devices():
    reset_state()
    old = ds.RETRY_S
    ds.RETRY_S = 0.2
    try:
        mic = DEVICES["mics"][0]
        a, b = FakeDevice("A", fail_opens=1), FakeDevice("B")      # A fails once, then reconnects
        eng = ds.Engine()
        eng.start(mic, [(0, a), (1, b)])
        time.sleep(2.0)
        assert eng.running
        assert not eng.errors(), eng.errors()
        for dev in (a, b):
            y = np.concatenate(dev.played)
            assert len(y) > SR * 0.8
            assert np.max(np.abs(y[-SR // 2:])) > 0.2               # music is coming through
        eng.stop()
        assert not eng.running
    finally:
        ds.RETRY_S = old


def test_web_remote():
    make_controller()
    srv = ds.start_web(port=0)
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        html = urllib.request.urlopen(base + "/").read().decode()
        assert "Dual Speaker" in html and "Auto-calibrate" in html

        def post(path, body):
            req = urllib.request.Request(base + path, json.dumps(body).encode(),
                                         {"Content-Type": "application/json"})
            return urllib.request.urlopen(req).status

        assert post("/set", {"path": ["outputs", 1, "volume"], "value": 999}) == 200
        assert post("/set", {"path": ["outputs", 0, "delay"], "value": 450}) == 200
        assert post("/set", {"path": ["outputs", 0, "mode"], "value": "Bogus"}) == 200
        assert post("/set", {"path": ["outputs", 7, "volume"], "value": 5}) == 200
        assert post("/set", {"path": ["master"], "value": "abc"}) == 200
        assert post("/set", {"path": ["outputs", 0, "device"], "value": "JBL Flip 5"}) == 200
        assert post("/cmd", {"cmd": "calibrate"}) == 200
        st = json.loads(urllib.request.urlopen(base + "/state").read())
        assert st["outputs"][1]["volume"] == 150
        assert st["outputs"][0]["delay"] == 450
        assert st["outputs"][0]["mode"] == "Stereo"
        assert st["outputs"][0]["device"] == "JBL Flip 5"
        assert "JBL Flip 5" in st["devices"]["speakers"] and st["local"] is True
        assert "Press Start" in st["calib"]
    finally:
        srv.shutdown()


def test_settings_roundtrip(tmp_path=None):
    import tempfile
    reset_state()
    path = os.path.join(tempfile.mkdtemp(), "s.json")
    with ds.state_lock:
        ds.state["outputs"][1].update(device="boAt Stone 350", delay=120, mode="Right channel only")
        ds.state["mic"] = "Microphone Array (Realtek)"
    REAL_SAVE(path)
    reset_state()
    ds.load_settings(path)
    assert ds.state["outputs"][1]["delay"] == 120
    assert ds.state["outputs"][1]["mode"] == "Right channel only"
    assert ds.state["mic"] == "Microphone Array (Realtek)"


# ----------------------------------------------------------------- controller (what the UI drives)
def make_controller():
    reset_state()
    ds.save_settings = lambda path=None: None
    c = ds.Controller()
    c.refresh_devices()
    ds.CTRL = c
    return c


def test_controller_flow():
    c = make_controller()
    try:
        # VB-CABLE picked automatically, laptop mic picked for calibration, cable not offered as a mic
        assert ds.state["source"] == "Input: CABLE Output (VB-Audio Virtual Cable)"
        assert ds.state["mic"] == "Microphone Array (Realtek)"
        assert "CABLE Output (VB-Audio Virtual Cable)" not in c.mics
        assert not c.public_state()["needs_cable"]
        c.command("start")
        assert ds.state["status"] == "Pick at least one speaker."
        # loop guards
        c.set(["outputs", 0, "device"], "CABLE Input (VB-Audio Virtual Cable)")
        c.command("start")
        assert "echo forever" in ds.state["status"]
        c.set(["source"], "System audio of: Speakers (Realtek)")
        c.set(["outputs", 0, "device"], "Speakers (Realtek)")
        c.command("start")
        assert "echo forever" in ds.state["status"]
        # unknown device names are ignored
        c.set(["outputs", 0, "device"], "Not a real speaker")
        assert ds.state["outputs"][0]["device"] == "Speakers (Realtek)"
        # proper start
        c.set(["source"], "Input: CABLE Output (VB-Audio Virtual Cable)")
        c.set(["outputs", 0, "device"], "JBL Flip 5")
        c.set(["outputs", 1, "device"], "boAt Stone 350")
        c.command("start")
        time.sleep(0.6)
        c.tick()
        assert c.engine.running and len(c.engine.workers) == 2
        assert ds.state["status"] == "Playing to JBL Flip 5 and boAt Stone 350"
        st = c.public_state()
        assert st["running"] and max(st["levels"]) > 0.05, st["levels"]   # meters move
        # switching a speaker while playing restarts on the new device
        c.set(["outputs", 1, "device"], "Speakers (Realtek)")
        time.sleep(0.3)
        assert c.engine.workers[1].speaker.name == "Speakers (Realtek)"
        c.command("stop")
        assert not c.engine.running and ds.state["status"] == "Stopped"
        c.command("calibrate")
        assert "Press Start" in ds.state["calib"]
    finally:
        c.shutdown()


def test_controller_no_cable_hint():
    saved = DEVICES["mics"]
    DEVICES["mics"] = [FakeDevice("Microphone Array (Realtek)")]
    try:
        c = make_controller()
        assert c.public_state()["needs_cable"]
        assert ds.state["source"] == ""
    finally:
        DEVICES["mics"] = saved


def test_window_close_quits_but_reload_does_not():
    c = make_controller()
    c.BYE_GRACE_S = 0.3
    c.command("bye", local=False)            # phones can't close the app
    assert c.bye_at is None
    c.command("quit", local=False)
    assert not c.quit_evt.is_set()
    c.command("bye", local=True)             # page reload: new poll arrives in time
    c.saw_local_ui()
    time.sleep(0.4)
    c.tick()
    assert not c.quit_evt.is_set()
    c.command("bye", local=True)             # window really closed
    time.sleep(0.4)
    c.tick()
    assert c.quit_evt.is_set()


def test_ui_in_headless_browser():
    """Load the real page in Chrome (if Playwright is available), click through it,
    and save screenshots for a visual check."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return
    c = make_controller()
    srv = ds.start_web(port=0)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    shots = os.path.join(HERE, "screenshots")
    os.makedirs(shots, exist_ok=True)
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page(viewport={"width": 1000, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(url)
            page.wait_for_selector("#dev1")
            page.select_option("#dev0", "JBL Flip 5")
            page.select_option("#dev1", "boAt Stone 350")
            page.click("#toggle")
            page.wait_for_function("document.getElementById('toggle').textContent==='Stop'")
            page.click("#mo1 button[data-v='Right channel only']")
            page.click("#mo0 button[data-v='Left channel only']")
            page.click("#mu1")
            page.wait_for_timeout(900)
            assert ds.state["outputs"][1]["mode"] == "Right channel only"
            assert ds.state["outputs"][1]["muted"] is True
            assert ds.state["outputs"][0]["device"] == "JBL Flip 5"
            page.click("#mu1")
            page.wait_for_timeout(700)
            page.screenshot(path=os.path.join(shots, "desktop.png"), full_page=True)
            m = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
            m.goto(url.replace("127.0.0.1", "localhost"))
            m.wait_for_selector("#dev1")
            m.wait_for_timeout(700)
            m.screenshot(path=os.path.join(shots, "phone.png"), full_page=True)
            b.close()
            assert not errors, errors
    finally:
        srv.shutdown()
        c.shutdown()


if __name__ == "__main__":
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
            print(f"PASS {name} ({time.time() - t0:.1f}s)")
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {e!r}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
