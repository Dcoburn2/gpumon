"""Does an unhandled failure leave a traceback behind?

A packaged build is a windowed program: it has no stderr, so Tk's default handler
and `threading.excepthook` both write into nothing. A bug would then look like a
graph that quietly stopped updating.

This raises in a Tk callback and in a worker thread - the two places the app's
real work runs - and checks the log catches both.
"""
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

# Building the window must not launch a sensor helper.
os.environ.setdefault("GPUMON_NO_AUTOSTART", "1")
import threading
import time
import tkinter as tk

os.environ["GPUMON_CONFIG"] = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "diagcrash-config.json")
DB = "diagcrash.db"
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.remove(DB + suffix)

import gpumon
import metrics as M
import sampler as SP
import store as S
from ui.monitor import MonitorApp

manager = M.SensorManager(per_core=False)
store = S.Store(DB)
sampler = SP.Sampler(manager, store, sample_hz=2.0)
root = tk.Tk()
log_path = gpumon._install_crash_logging(root)
app = MonitorApp(root, manager, store, sampler)


def pump(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


def boom():
    raise ValueError("simulated failure")


def worker_boom():
    raise RuntimeError("simulated worker failure")


root.after(100, boom)
pump(1.5)
threading.Thread(target=worker_boom, name="sensor-worker").start()
pump(1.5)

text = ""
if os.path.exists(log_path):
    with open(log_path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
print("the log contains:")
for line in text.splitlines()[:20]:
    print("   ", line)

problems = []
if "interface error" not in text:
    problems.append("a failure in a Tk callback was not recorded")
if "simulated failure" not in text:
    problems.append("the traceback of the Tk failure is missing")
if "sensor-worker" not in text or "simulated worker failure" not in text:
    problems.append("a failure in a worker thread was not recorded")
if "gpumon " not in text:
    problems.append("the version is not recorded with the failure")

app.on_close()
sampler.stop()
store.close()
manager.close()
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.remove(DB + suffix)
if os.path.exists("diagcrash-config.json"):
    os.remove("diagcrash-config.json")
if os.path.exists(log_path):
    os.remove(log_path)

print()
if problems:
    print("CRASH LOGGING TEST FAILED")
    for item in problems:
        print("  -", item)
    raise SystemExit(1)
print("CRASH LOGGING TEST PASSED: both kinds of failure leave a traceback.")
