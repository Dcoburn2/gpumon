"""The sensor helper and the backend that reads it.

The helper is the privileged half of gpumon: it opens the kernel driver, reads
the processor's registers and publishes them. Nothing here needs a driver or
administrator rights, because the reader and the clock are injected - which is
also the only way to test an exit condition that otherwise takes 45 seconds.
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





import json
import os
import time

import msrbackend
import sensorhelper

problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diaghelper")
os.makedirs(WORK, exist_ok=True)
SENSORS = os.path.join(WORK, "sensors.json")
BEAT = os.path.join(WORK, "heartbeat")
for path in (SENSORS, BEAT):
    if os.path.exists(path):
        os.remove(path)


class FakeClock:
    """A clock that only moves when the helper sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        # Real time in the JSON is used for age, so keep them consistent.
        self.wall = getattr(self, "wall", time.time()) + seconds


print("=" * 84)
print("SENSOR HELPER TEST")
print("=" * 84)

print("\n[1] readings are published atomically and read back")
sensorhelper.write_atomically(SENSORS, {"cpu_temp": 47.0, "t": time.time()})
check("the file holds what was written",
      sensorhelper.read_sensors(SENSORS).get("cpu_temp") == 47.0)
check("no temporary file is left behind",
      not os.path.exists(SENSORS + ".tmp"))
check("a missing file reads as empty, not as an exception",
      sensorhelper.read_sensors(os.path.join(WORK, "nope.json")) == {})
with open(SENSORS, "w", encoding="utf-8") as handle:
    handle.write("{ this is not json")
check("a corrupt file reads as empty too", sensorhelper.read_sensors(SENSORS) == {})

print("\n[2] a short run publishes and stops on its own")
clock = FakeClock()
published: list[dict] = []
result = sensorhelper.run(
    reader=lambda: {"cpu_temp": 42.0, "core0_temp": 45.0},
    hz=1.0, idle_seconds=5.0, max_seconds=3.0,
    sensors_file=SENSORS, heartbeat=BEAT,
    clock=clock.monotonic, sleep=clock.sleep, on_publish=published.append)
print(f"    iterations={result.iterations} published={result.published} "
      f"reason={result.reason!r}")
check("it read the registers repeatedly", result.iterations >= 3,
      str(result.iterations))
check("it published every reading", result.published == result.iterations)
check("it stopped at its lifetime", "lifetime" in result.reason, result.reason)
check("the published payload carries the values", published[0]["cpu_temp"] == 42.0)

print("\n[3] a failing reader does not kill the helper")
calls = {"n": 0}


def flaky():
    calls["n"] += 1
    if calls["n"] == 1:
        raise RuntimeError("driver said no")
    return {"cpu_temp": 50.0}


clock = FakeClock()
result = sensorhelper.run(reader=flaky, hz=1.0, idle_seconds=5.0, max_seconds=2.0,
                          sensors_file=SENSORS, heartbeat=BEAT,
                          clock=clock.monotonic, sleep=clock.sleep)
check("it kept going after the failure", result.published >= 1,
      f"published={result.published}")
check("the failure is recorded for the log", bool(result.error), result.error)

print("\n[4] it stops when the program that wanted it goes away")
clock = FakeClock()
result = sensorhelper.run(reader=lambda: {"cpu_temp": 40.0}, hz=1.0,
                          parent_pid=999999, idle_seconds=60.0,
                          sensors_file=SENSORS, heartbeat=BEAT,
                          clock=clock.monotonic, sleep=clock.sleep)
print(f"    reason: {result.reason!r}")
check("a dead parent stops it immediately", "exited" in result.reason,
      result.reason)

print("\n[5] it stops when nobody is reading any more")
# The heartbeat file is what a reader touches; make it look old.
sensorhelper.write_atomically(SENSORS, {"cpu_temp": 40.0, "t": time.time()})
with open(BEAT, "w", encoding="utf-8"):
    pass
os.utime(BEAT, (time.time() - 120, time.time() - 120))
clock = sensorhelper.run(reader=lambda: {"cpu_temp": 40.0}, hz=1.0,
                         idle_seconds=5.0, max_seconds=30.0,
                         sensors_file=SENSORS, heartbeat=BEAT)
print(f"    reason: {clock.reason!r}")
check("a stale heartbeat stops it", "read the sensors" in clock.reason,
      clock.reason)

print("\n[6] the backend only trusts fresh readings")
backend = msrbackend.MsrBackend(path=SENSORS, heartbeat=BEAT)
sensorhelper.write_atomically(SENSORS, {"cpu_temp": 47.0, "cpu_core_max": 52.0,
                                        "t": time.time()})
check("a fresh reading is available", backend.available(), backend.error)
values = backend.poll()
check("it produces the metric keys",
      values.get("cpu_temp") == 47.0 and values.get("cpu_core_max") == 52.0,
      str(values))
check("the timestamp is not mistaken for a metric", "t" not in values, str(values))
check("polling touches the heartbeat, which is what keeps the helper alive",
      os.path.exists(BEAT) and time.time() - os.path.getmtime(BEAT) < 5.0)

sensorhelper.write_atomically(SENSORS, {"cpu_temp": 99.0, "t": time.time() - 120})
stale = msrbackend.MsrBackend(path=SENSORS, heartbeat=BEAT)
check("a stale reading is refused rather than reported as current",
      not stale.available())
check("and it says how old it is", "stopped publishing" in stale.error, stale.error)
check("poll returns nothing rather than an old number", stale.poll() == {})
check("the age is exposed for the UI", stale.reading_age() > 60,
      f"{stale.reading_age():.0f}s")

print("\n[7] stopping removes what would otherwise go stale")
stale.stop()
check("the published file is gone", not os.path.exists(SENSORS))
check("so is the heartbeat", not os.path.exists(BEAT))

for path in (SENSORS, BEAT):
    if os.path.exists(path):
        os.remove(path)
try:
    os.rmdir(WORK)
except OSError:
    pass

print("\n" + "=" * 84)
if problems:
    print(f"SENSOR HELPER TEST FAILED ({len(problems)})")
    for item in problems:
        print(f"  - {item}")
else:
    print("SENSOR HELPER TEST PASSED")
print("=" * 84)
raise SystemExit(1 if problems else 0)
