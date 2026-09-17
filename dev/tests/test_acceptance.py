"""Final acceptance test: launch the real app exactly as a user would.

Runs `gpumon.py`'s own launch path (arg parsing, store, sensor manager, sampler,
MonitorApp), exercises logging with a live benchmark-like load, writes a summary,
then shuts down. Exits non-zero on any failure.
"""

from __future__ import annotations
# GPUMON_TEST_BOOTSTRAP
# Run from anywhere, and from any working directory: the program modules and the
# packaging scripts are one level up, and the paths in here are relative to the
# repository root.
import os as _os
import sys as _sys

#: dev/, where this test lives: the packaging scripts, and the build output.
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
#: The repository root, one level above. The program is here, so this is where the
#: tests run from and the paths they use are relative to.
_REPO = _os.path.dirname(_ROOT)
#: Anything under dev/ is written as a path from the root - "dev/release/..." -
#: because that is where it is from here.
_DEV = "dev"
for _extra in (_REPO, _ROOT, _os.path.join(_ROOT, "scripts")):
    if _extra not in _sys.path:
        _sys.path.insert(0, _extra)
_os.chdir(_REPO)

def _skip_machine_specific_test() -> None:
    """Stop, with a note, when there is no hardware or desktop to test against.

    Set by continuous integration. A test that measures a window or asserts that
    a vendor's library is installed cannot say anything useful on a machine that
    has neither, and reporting a failure there trains everybody to ignore red.
    """
    if _os.environ.get("GPUMON_SKIP_MACHINE_TESTS"):
        print("  --  skipped: this test needs a GPU, a vendor driver, a desktop "
              "session or the sensor driver, and GPUMON_SKIP_MACHINE_TESTS is set")
        raise SystemExit(0)





import os
import subprocess
import sys
import time
import tkinter as tk

# Keep every setting this test writes out of the user's real config.json.
os.environ.setdefault(
    "GPUMON_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "diagmon-config.json"))
# Start from a clean slate: `layout` is read when the window is built, so
# a scratch file left behind by an earlier run would change the geometry
# this test measures.
if os.path.exists(os.environ["GPUMON_CONFIG"]):
    os.remove(os.environ["GPUMON_CONFIG"])

# Building the window must not launch a sensor helper.
os.environ.setdefault("GPUMON_NO_AUTOSTART", "1")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import alarms as A
import metrics as M
import sampler as SP
import store as S

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "acceptance.db")
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.remove(DB + suffix)

problems: list[str] = []
print("=" * 78)
print("ACCEPTANCE TEST")
print("=" * 78)

# ---- 1. sensor layer -------------------------------------------------
manager = M.SensorManager(per_core=True)
caps = manager.capabilities()
print(f"\n[1] sensors: {len(caps.gpus)} GPU(s)")
for g in caps.gpus:
    print(f"    gpu{g.index} {g.vendor:7} {g.name:26} sources={g.sources}")
if not caps.gpus:
    problems.append("no GPUs detected")

# ---- 2. app construction (the real entry path) -----------------------
store = S.Store(DB)
engine = A.AlarmEngine()
# A deliberately low threshold so this test exercises the alarm path end to end:
# crossing -> esca/close -> persisted timeline entry -> report section.
engine.set_rule(A.AlarmRule("gpu0_temp", warning=20.0, critical=200.0,
                            hysteresis=1.0, min_duration=2.0))
engine.set_rule(A.AlarmRule("cpu_util", warning=5.0, hysteresis=1.0,
                            min_duration=2.0))
sampler = SP.Sampler(manager, store, sample_hz=1.0, alarm_engine=engine,
                     per_core=True)
root = tk.Tk()
from ui.monitor import MonitorApp  # noqa: E402
app = MonitorApp(root, manager, store, sampler)
print(f"\n[2] app built: {len(app.panels)} panel(s), "
      f"{len(app.system_sparks)} system sparklines")


def pump(seconds: float) -> None:
    """Let Tk run normally; the app schedules its own redraws."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


pump(3.0)
print(f"    after warmup: samples_seen={sampler.status().samples_written} "
      f"eff={sampler.status().effective_hz:.2f}Hz")

# ---- 3. live rendering actually drew something -----------------------
drawn = sum(len(sp.find_all()) for p in app.panels.values() for sp in p.sparks.values())
drawn += sum(len(sp.find_all()) for sp in app.system_sparks.values())
print(f"    sparkline canvas items drawn: {drawn}")
if drawn == 0:
    problems.append("live sparklines drew nothing")

# ---- 4. log a run while external load is applied ---------------------
print("\n[3] logging a 20 s run under external CPU load")
sid = sampler.start_logging(label="acceptance run")
sampler.mark("load started")

ps_cmd = ("$end=(Get-Date).AddSeconds(24); $x=0.0; "
          "while((Get-Date) -lt $end){ $x += [math]::Sqrt([math]::PI) * 1.000001 }")
procs = [subprocess.Popen(["powershell", "-NoProfile", "-Command", ps_cmd],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=0x08000000) for _ in range(6)]

max_gap = 0.0
last = time.monotonic()
n_samples = 0


def counting_sample(_wall, _values):
    global last, max_gap, n_samples
    now = time.monotonic()
    max_gap = max(max_gap, now - last)
    last = now
    n_samples += 1


sampler.on_sample = counting_sample
pump(20.0)

for p in procs:
    p.terminate()
for p in procs:
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()

sampler.mark("load stopped")
pump(2.0)
summary = sampler.stop_logging()
st = sampler.status()
print(f"    session #{summary.session_id}: duration={summary.duration:.1f}s "
      f"samples={summary.samples} dropped={sampler.dropped_samples()}")
print(f"    cadence: {st.effective_hz:.2f} Hz effective, max gap {max_gap:.2f}s, "
      f"gaps={st.gap_count}, poll={st.poll_ms:.1f}ms")

if summary.samples < summary.duration * 0.8:
    problems.append(f"only {summary.samples} samples over {summary.duration:.1f}s")
if max_gap > 3.0:
    problems.append(f"sampling gap of {max_gap:.1f}s")

# ---- 5. data completeness per metric ---------------------------------
metrics_stored = store.metrics_for_session(summary.session_id)
stats = store.stats_many(summary.session_id)
expected = max(1, int(summary.duration))
print(f"\n[4] stored {len(metrics_stored)} metrics; sample counts vs "
      f"~{expected} expected:")
short = []
for key in sorted(metrics_stored):
    n = stats[key].count
    if n < expected * 0.8:
        short.append((key, n))
print(f"    metrics with <80% coverage: {len(short)}")
for key, n in short:
    print(f"      {key}: {n}")
if short:
    problems.append(f"{len(short)} metrics had poor coverage: "
                    f"{[k for k, _ in short][:6]}")

# ---- 6. alarms recorded ----------------------------------------------
alarms = store.alarms(summary.session_id)
print(f"\n[5] alarm events recorded: {len(alarms)}")
for ev in alarms:
    print(f"    {ev.metric:16} {ev.level:8} peak={ev.peak:7.1f} "
          f"thr={ev.threshold:6.1f} {ev.started_t:5.1f}s -> "
          f"{'open' if ev.ended_t is None else f'{ev.ended_t:.1f}s'} "
          f"({ev.samples} samples)")
if not alarms:
    problems.append("no alarm events were persisted despite low thresholds")
else:
    with_start = [e for e in alarms if e.started_t >= 0]
    with_peak = [e for e in alarms if e.peak > 0]
    print(f"    all have a start time: {len(with_start) == len(alarms)}; "
          f"all recorded a peak: {len(with_peak) == len(alarms)}")
    if len(with_start) != len(alarms) or len(with_peak) != len(alarms):
        problems.append("alarm events missing start time or peak")

# ---- 7. summary window + exports -------------------------------------
print("\n[6] summary window and exports")
from ui.summary import SummaryWindow  # noqa: E402
win = SummaryWindow(root, store, summary.session_id, app.theme,
                    devices=manager.gpus, on_close=lambda s: None)
tabs = [win.nb.tab(i, "text") for i in range(win.nb.index("end"))]
print(f"    tabs: {tabs}")
for i in range(len(tabs)):
    win.nb.select(i)
    root.update()
if len(tabs) < 4:
    problems.append(f"summary missing tabs: {tabs}")

import report as R  # noqa: E402

html_doc = R.build_html_report(store, summary.session_id)
txt = R.build_text_report(store, summary.session_id)
with open("acceptance_report.html", "w", encoding="utf-8") as fh:
    fh.write(html_doc)
print(f"    html report {len(html_doc)} bytes, "
      f"{html_doc.count('<svg')} charts, text {len(txt)} bytes")
if html_doc.count("<svg") < 4:
    problems.append("report has too few charts")

# ---- 8. shutdown ------------------------------------------------------
print("\n[7] shutdown")
win.close()
app.on_close()
print("    closed cleanly")

store2 = S.Store(DB)
final = store2.get_session(summary.session_id)
print(f"    session persisted: label={final.label!r} "
      f"sample_count={final.sample_count} duration={final.duration:.1f}s")
store2.close()
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        try:
            os.remove(DB + suffix)
        except OSError:
            pass

print("\n" + "=" * 78)
if problems:
    print(f"ACCEPTANCE FAILED ({len(problems)} problem(s))")
    for p in problems:
        print(f"  - {p}")
else:
    print("ACCEPTANCE PASSED")
print("=" * 78)
raise SystemExit(1 if problems else 0)
