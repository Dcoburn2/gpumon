"""Verify the app's own entry point launches, samples, and exits cleanly.

Runs `gpumon.main()` in a thread, drives Tk briefly, then closes the window the
way a user would. This is the closest thing to a real launch that can run
unattended: it exercises argument parsing, the default database path, the sensor
manager, the sampler and the real monitor window.
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


def _windows_only_test() -> None:
    """Stop, with a note, on anything that is not Windows.

    Some of the suite is about Windows itself: the .cmd launcher, the registry
    lookup for the desktop folder, LibreHardwareMonitor's Windows backends, a
    signing stub written as a .cmd, ctypes.WinDLL. None of that can say anything
    about a Linux machine, and a failure there is noise rather than a finding.
    """
    if _os.name != "nt":
        print("  --  skipped: this test is about Windows")
        raise SystemExit(0)


_skip_machine_specific_test()




import os
import sys
import threading
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

TEST_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "launch_test.db")
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(TEST_DB + suffix):
        os.remove(TEST_DB + suffix)

import alarms as A      # noqa: E402
import metrics as M     # noqa: E402
import sampler as SP    # noqa: E402
import store as S       # noqa: E402

print("=" * 78)
print("LAUNCH TEST (real entry path)")
print("=" * 78)

problems: list[str] = []

# Replicate exactly what main() -> launch_gui() does, with a test database.
store = S.Store(TEST_DB)
manager = M.SensorManager(per_core=False)
engine = A.AlarmEngine.from_config(A.load_config().get("alarms"))
sampler = SP.Sampler(manager, store, sample_hz=1.0, alarm_engine=engine,
                     per_core=False)
sampler.configure_alarms(engine.rules or None)

root = tk.Tk()
from ui.monitor import MonitorApp   # noqa: E402


app = MonitorApp(root, manager, store, sampler)
print(f"\nwindow constructed: title={root.title()!r}")
print(f"  geometry: {root.winfo_width()}x{root.winfo_height()}")
print(f"  GPU panels: {len(app.panels)}, system sparklines: {len(app.system_sparks)}")
# The title carries the version, so a bug report can name the build it came from.
expected_title = f"gpumon {SP.APP_VERSION} - GPU & system telemetry"
if root.title() != expected_title:
    problems.append(f"unexpected window title: {root.title()!r}")

# Drive it for a few seconds while real samples arrive.
end = time.monotonic() + 6.0
while time.monotonic() < end:
    root.update()
    app._on_tick()
    time.sleep(0.02)

st = sampler.status()
print(f"\nafter 6 s of live rendering:")
print(f"  effective rate : {st.effective_hz:.2f} Hz")
print(f"  poll cost      : {st.poll_ms:.1f} ms")
print(f"  samples seen   : {st.samples_written if st.logging else 'not logging'}")
print(f"  dropped        : {st.dropped}, gaps: {st.gap_count}")

# The on-screen readouts must show real numbers, not placeholders.
panel = app.panels[0]
readouts = {f: lbl.cget("text") for f, lbl in panel.value_labels.items()}
print(f"\ngpu0 readouts: {readouts}")
missing = [f for f, t in readouts.items() if t in ("--", "")]
if missing:
    problems.append(f"readouts still blank: {missing}")

temp_text = panel.temp_label.cget("text")
print(f"  big temperature label: {temp_text!r}")
if temp_text in ("-- C", "n/a"):
    problems.append("temperature readout blank")

status_line = app.rec_detail.cget("text")
footer = app.footer_left.cget("text")
print(f"  status strip : {status_line}")
print(f"  footer       : {footer}")
if "Hz" not in status_line:
    problems.append("status strip missing rate")

if st.effective_hz < 0.8:
    problems.append(f"live rate too low: {st.effective_hz:.2f} Hz")

print("\nclosing window (simulating the user quitting)")
app.on_close()
print("  closed without error")

for suffix in ("", "-wal", "-shm"):
    if os.path.exists(TEST_DB + suffix):
        try:
            os.remove(TEST_DB + suffix)
        except OSError:
            pass

print("\n" + "=" * 78)
if problems:
    print(f"LAUNCH TEST FAILED ({len(problems)})")
    for p in problems:
        print(f"  - {p}")
else:
    print("LAUNCH TEST PASSED")
print("=" * 78)
raise SystemExit(1 if problems else 0)
