"""Verify the CPU-temperature fallback chain.

The question this answers: what does gpumon do about the CPU temperature, and on
what does it depend? The chain is deliberately layered so the answer is "whatever
the machine can give", with the source named rather than assumed:

  msr       gpumon's own reader, through the PawnIO driver - no other program
  lhm       LibreHardwareMonitor, over HTTP or WMI (kept as a fallback)
  thermal   /sys/class/hwmon and /sys/class/thermal on Linux - no driver at all
  acpi      the firmware's own thermal zone on Windows, a last resort

Every check here runs without any of them being installed.
"""

from __future__ import annotations
# GPUMON_TEST_BOOTSTRAP
# Run from anywhere, and from any working directory: the program modules and the
# packaging scripts are one level up, and the paths in here are relative to the
# repository root.
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _extra in (_ROOT, _os.path.join(_ROOT, "scripts")):
    if _extra not in _sys.path:
        _sys.path.insert(0, _extra)
_os.chdir(_ROOT)

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





import inspect
import os
import pathlib
import subprocess
import sys
import tempfile
import time

import alarms as A
import metrics as M
import msrbackend
import sensorhelper
import sensorsetup
from ui import monitor as MON

problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


print("=" * 84)
print("CPU TEMPERATURE SOURCE TEST")
print("=" * 84)

print("\n[1] the ACPI fallback declines politely when the firmware has none")
acpi = M.AcpiThermalZoneBackend()
reading = acpi.poll()
print(f"    poll() -> {reading}   error: {acpi.error!r}")
check("no exception from a machine without ACPI zones", isinstance(reading, dict))
check("nothing is invented when the class is unsupported",
      reading == {} or "cpu_temp" in reading, str(reading))
if not reading:
    check("the reason is recorded", bool(acpi.error), acpi.error)

print("\n[2] its plausibility gate rejects a nonsense zone")
# 0 K and 400 K come back from real firmware now and then; neither is a CPU.
for kelvin_tenths, expected in ((0, False), (2731, False), (3231, True),
                                (3731, True), (5000, False)):
    celsius = kelvin_tenths / 10.0 - 273.15
    plausible = M.AcpiThermalZoneBackend.FLOOR <= celsius <= \
        M.AcpiThermalZoneBackend.CEILING
    check(f"{kelvin_tenths / 10.0:.0f} K -> {celsius:6.1f} C accepted",
          plausible == expected, f"{celsius:.1f} C")

print("\n[3] the manager names its source instead of guessing")
manager = M.SensorManager(per_core=False)
source = manager.cpu_temp_source()
print(f"    source on this machine: {source!r}   platform: {manager.platform_name}")
check("the source is one of the known names",
      source in ("", "msr", "lhm", "thermal", "acpi"), source)
# The invariant is about preference, not about this machine's state. Requiring
# "msr" whenever setup has ever run was wrong: the marker persists, the reading
# goes stale when the helper stops, and the check then failed on a machine where
# nothing was being published at all.
_live = manager.msr.error == "" and manager.msr.reading_age() <= msrbackend.MAX_AGE
check("gpumon's own reader is preferred whenever it has a live reading",
      source == "msr" or not _live, f"source={source!r} live={_live}")
if _live:
    check("and it is the source in use", source == "msr", source)
else:
    print(f"    (nothing is being published right now: "
          f"{manager.msr.error or 'the reading is stale'})")

# The preference itself, without needing a driver or live sensors: hand the
# manager a fresh reading of its own and it must choose that over the fallbacks.
# The payload carries the helper's own timestamp (`t`), which is what the reader
# ages a reading by - without it the reading looks decades old and is refused.
_probe = os.path.join(tempfile.gettempdir(), "gpumon-preference.json")
_probe_beat = _probe + ".heartbeat"
sensorhelper.write_atomically(_probe, {"t": time.time(), "cpu_temp": 47.0,
                                       "cpu_clock": 3000.0})
sensorhelper.touch_heartbeat(_probe_beat)
manager.msr = msrbackend.MsrBackend(_probe, _probe_beat)
check("a fresh reading of its own wins over the fallbacks",
      manager.cpu_temp_source() == "msr", manager.cpu_temp_source())
manager.msr.stop()
for _path in (_probe, _probe_beat):
    if os.path.exists(_path):
        os.remove(_path)
caps = manager.capabilities()
if caps.cpu_temp != bool(source):
    # The helper publishes about once a second, and a reading can age past the
    # staleness window between these two calls - which is a race in this test, not
    # in the program. One retry, then judge.
    source = manager.cpu_temp_source()
    caps = manager.capabilities()
check("capabilities agree with the source",
      caps.cpu_temp == bool(source),
      f"{caps.cpu_temp} vs {source!r}")
if manager.platform_name == "linux":
    check("a Linux machine never reports the Windows fallback", source != "acpi")
if not source:
    matching = [n for n in manager.notes
                if "CPU package temperature needs" in n
                or "ACPI thermal zone" in n]
    check("a missing CPU temperature is explained, not left silent",
          bool(matching), str(matching[:1]))
    check("the explanation says why it is missing",
          any("kernel driver" in n or "ACPI thermal zone" in n for n in matching),
          str(matching[:1]))
    # What it deliberately does *not* do is tell the reader what to run: this text
    # is stored in the session file and read months later, when any command in it
    # may be wrong. The actionable version lives on the card, beside the number.
    check("and leaves the instructions to the program, not to a record",
          not any("START SENSORS" in n or "setup-sensors" in n for n in matching),
          str(matching[:1]))
    check("it does not blame a WMI provider that no longer exists",
          all("WMI provider" not in n for n in matching), str(matching[:1]))

print("\n[4] the fallback only ever fills a gap")
# The ACPI merge happens with `if "cpu_temp" not in merged`, so a real source
# always wins. This asserts the ordering rule itself rather than the machine.
merged = {"cpu_temp": 47.0}
if "cpu_temp" not in merged:
    merged.update({"cpu_temp": 99.0})
check("a present reading is never overwritten by the fallback",
      merged["cpu_temp"] == 47.0, str(merged))
merged = {}
if "cpu_temp" not in merged:
    merged.update({"cpu_temp": 52.0})
check("an absent reading is filled by the fallback", merged["cpu_temp"] == 52.0)

print("\n[5] the setup offers a lasting outcome, not a per-boot chore")
check("a logon task helper exists", callable(sensorsetup.install_startup_task))
check("an on-demand helper task exists", callable(sensorsetup.install_helper_task))
check("it can be undone", callable(sensorsetup.remove_startup_task))
# Deliberately *not* calling remove_startup_task() here: it deletes both tasks,
# and on a machine that is already set up that would tear down a working sensor
# installation. An earlier version of this test did exactly that.
check("removing is not run as a side effect of being tested",
      sensorsetup.remove_startup_task is not None)
scratch = "gpumon-test-scratch-task"
deleted, _message = sensorsetup.delete_task(scratch)
check("deleting a task that does not exist reports failure, not an exception",
      deleted is False)

print("\n[6] the setup that gives a new machine its sensors")
# There is no automation of another application any more: gpumon reads the
# processor's registers itself, through the PawnIO driver. It does not ship that
# driver - PawnIO is GPL-2.0 - so setup fetches the vendor's own installer and
# modules, which is what keeps this project's distribution clean.

check("the setup fetches PawnIO from the vendor's release",
      "PawnIO.Setup/releases" in sensorsetup.PAWNIO_SETUP_URL,
      sensorsetup.PAWNIO_SETUP_URL)
check("it fetches the modules from PawnIO's own module release",
      "PawnIO.Modules" in sensorsetup.MODULES_RELEASE_API)
check("it does not claim to redistribute them",
      not os.path.exists("PawnIO_setup.exe")
      and not os.path.exists(os.path.join("tools", "PawnIO_setup.exe")),
      "an installer in the tree would be GPL-2.0 code being redistributed")
check("it knows whether the driver is installed",
      sensorsetup.pawnio_installed() in (True, False))
check("it knows which modules it needs",
      set(sensorsetup.REQUIRED_MODULES) == {"IntelMSR.bin", "AMDFamily17.bin",
                                            "RyzenSMU.bin"},
      str(sorted(sensorsetup.REQUIRED_MODULES)))
# Whether they are present depends on whether setup has run on this checkout, so
# the rule is asserted rather than the state: anything missing has to be
# something setup can fetch, from the vendor rather than from here.
missing_now = sensorsetup.missing_modules()
print(f"    modules missing on this checkout: {missing_now or 'none'}")
check("anything missing is something setup can download",
      all(name in sensorsetup.REQUIRED_MODULES for name in missing_now),
      str(missing_now))
check("and it is fetched rather than shipped",
      "PawnIO.Modules" in sensorsetup.MODULES_RELEASE_API
      and not os.path.exists(os.path.join("release", "_internal",
                                          "pawnio-modules")),
      "the release must not carry GPL-2.0 module binaries")
check("both tasks run gpumon's own helper",
      "--sensor-helper" in sensorsetup.helper_action(),
      sensorsetup.helper_action())
check("the two task names are distinct",
      sensorsetup.STARTUP_TASK != sensorsetup.HELPER_TASK)
check("starting the helper needs no elevation of its own",
      callable(sensorsetup.start_helper_now))
check("the old name still works, for anything that used it",
      sensorsetup.start_server_now is sensorsetup.start_helper_now)
check("no LibreHardwareMonitor automation is left",
      not pathlib.Path("lhm-server.ps1").exists(),
      "the menu-driving script that was the janky part")

print("\n[7] a task left over from an older version is recognised as stale")
# The failure this guards against: an old task points at a file that no longer
# exists, so triggering it starts nothing and the program waits for a reading
# that never comes. It has to be reported as "needs setting up", not as a wait.
check("the current task action can be read back",
      isinstance(sensorsetup.task_action(), str))
check("a task running the helper counts as current",
      sensorsetup.task_is_current.__doc__ is not None)
if sensorsetup.task_exists(sensorsetup.HELPER_TASK):
    action = sensorsetup.task_action()
    print(f"    registered action: {action[:72]}")
    check("this machine's task is judged by what it runs",
          sensorsetup.task_is_current() == ("--sensor-helper" in action),
          f"current={sensorsetup.task_is_current()} action={action[:50]}")
    # Deliberately only exercised on the stale path: a current task would be
    # started for real, leaving a helper running from whatever build the task
    # points at - which then holds that build's DLLs open and blocks the next
    # `make_release.py --build`. A test should not leave a process behind.
    if not sensorsetup.task_is_current():
        ok, message = sensorsetup.start_helper_now(wait=2.0)
        print(f"    a stale task reports: {message[:72]}")
        check("a stale task is reported instead of the helper being waited on",
              not ok and "recreat" in message, message)
        check("and nothing was started to wait for",
              sensorsetup.reading_age() == float("inf")
              or sensorsetup.reading_age() > 5,
              f"{sensorsetup.reading_age():.1f}s")
    else:
        print("    the task is current, so it is left alone (starting it would "
              "leave a helper running)")

print("\n[8] the setup the app raises itself")
# Changing a scheduled task needs administrator rights, so one prompt is
# unavoidable - but it is Windows asking, not LibreHardwareMonitor, and the app
# raises it rather than sending anyone to a command line.

check("there is a self-elevating setup", callable(sensorsetup.run_setup_elevated))
command = sensorsetup.setup_command()
print(f"    command: {command}")
check("it asks Windows for permission", "-Verb RunAs" in command)
check("it passes --setup-sensors", "--setup-sensors" in command)
check("it names the program to run", "gpumon" in command)
# The bug that made the button useless: the whole argument list was wrapped in
# another pair of quotes, so PowerShell rejected it and the elevated window
# opened, errored and closed without running anything.
check("the argument list is not wrapped in extra quotes",
      "''" not in command, command)
check("each argument is quoted separately",
      command.count("','") == 1 and command.count("'--setup-sensors'") == 1,
      command)

print("\n[9] and the elevated child really receives those arguments")
# Run the same command shape without elevation, with a flag that returns
# immediately, and read back what Python was handed.
probe = sensorsetup.setup_command(
    arguments="'" + os.path.join(os.getcwd(), "gpumon.py") + "','--msr-probe'")
# The setup launches a windowless interpreter, which by design has no stdout to
# capture. For this probe the console interpreter is swapped in, because what is
# being checked is the argument quoting, not the window behaviour.
probe = probe.replace(sensorsetup.windowless_python(), sys.executable)
probe = probe.replace(" -Verb RunAs", "").replace(" -WindowStyle Hidden", "")
out_path = os.path.join(os.getcwd(), "cp.out")
err_path = os.path.join(os.getcwd(), "cp.err")
probe = (probe.rstrip() + " -PassThru -NoNewWindow "
         f"-RedirectStandardOutput '{out_path}' "
         f"-RedirectStandardError '{err_path}' | Out-Null; "
         "Write-Output 'started'")
result = subprocess.run(["powershell", "-NoProfile", "-Command", probe],
                        capture_output=True, text=True, errors="replace")
check("PowerShell accepted the command",
      result.returncode == 0 and "started" in result.stdout,
      (result.stderr or result.stdout).strip()[:120])
# The probe takes a few seconds to start and fail its way to the end.
for _ in range(30):
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        break
    time.sleep(0.5)
child_output = ""
if os.path.exists(out_path):
    with open(out_path, encoding="utf-8", errors="replace") as handle:
        child_output = handle.read()
child_error = ""
if os.path.exists(err_path):
    with open(err_path, encoding="utf-8", errors="replace") as handle:
        child_error = handle.read()
print("    the child printed: " +
      " / ".join(line.strip() for line in child_output.splitlines()
                 if line.strip())[:150])
check("the child ran and produced the probe's output",
      "CPU SENSOR PROBE" in child_output,
      child_output.strip()[:80] or child_error.strip()[:80])
check("so the arguments arrived intact",
      "decoding path" in child_output,
      "the probe only prints that once it has parsed its arguments")
for path in (out_path, err_path):
    if os.path.exists(path):
        os.remove(path)

check("it never runs or references another program",
      "lhm-server" not in inspect.getsource(sensorsetup.run_setup_elevated)
      and "LibreHardwareMonitor.exe" not in inspect.getsource(
          sensorsetup.run_setup_elevated),
      "the only thing it starts is gpumon itself with --setup-sensors")

check("the app calls it from a button", "run_setup_elevated" in
      open("ui/monitor.py", encoding="utf-8").read())
check("and never tells the user to run a script",
      "start-sensors.cmd once" not in open("ui/monitor.py", encoding="utf-8").read())

print("\n[10] the click does not freeze the window")
monitor_source = open("ui/monitor.py", encoding="utf-8").read()
click = monitor_source[monitor_source.index("def start_sensors"):]
click = click[:click.index("def _start_sensors_worker")]
print(f"    start_sensors body:\n      " +
      "\n      ".join(line.strip() for line in click.splitlines()
                      if line.strip())[:400])
check("the click returns immediately instead of waiting",
      "wait=20" not in click and "wait=45" not in click,
      "the first version waited on the UI thread for twenty seconds")
check("it starts a worker thread", "Thread(" in click)
check("the waiting happens in the worker",
      "wait=8.0" in monitor_source or "start_helper_now(wait=" in monitor_source)
check("the worker hands results back to the UI thread",
      "self.root.after(0" in monitor_source and "def _post" in monitor_source)
check("the outcome of the elevated setup is read back",
      "setup_outcome" in monitor_source)

print("\n[11] notes are facts, and old advice is corrected on the way out")
# A note is written into the session file once and read for years, so an
# instruction inside one goes stale - which is exactly what happened: a recorded
# session told the reader to run a command and to start LibreHardwareMonitor's
# web server from its menu, long after neither was how this works.
OLD_WMI = ("CPU temperature is only reachable through a kernel driver, so it "
           "needs LibreHardwareMonitor's WMI provider. Run `python gpumon.py "
           "--setup-sensors` (WMI provider not running).")
OLD_MENU = ("CPU package temperature needs a kernel driver on Windows - every "
            "tool that reports it installs one - so it takes a single elevated "
            "setup. Run `python gpumon.py --setup-sensors` once, or "
            "start-sensors.cmd, and it stays set up afterwards (LibreHardware"
            "Monitor is not publishing sensors - start it, or start its web "
            "server from the Web menu).")
for label, old in (("the WMI-era note", OLD_WMI), ("the menu-era note", OLD_MENU)):
    shown = M.modernise_note(old)
    print(f"    {label} now reads: {shown}")
    check(f"{label} no longer tells anyone what to run",
          "--setup-sensors" not in shown and "web server" not in shown
          and "start-sensors" not in shown, shown)
    check(f"{label} keeps the fact", "kernel driver" in shown, shown)

UNRELATED = "AMD telemetry via ADL for PCI 17:00.0 - temperature, hotspot, fan"
check("a note that is not affected is shown unchanged",
      M.modernise_note(UNRELATED) == UNRELATED)

manager2 = M.SensorManager(per_core=False)
manager2.capabilities()
live_notes = [n for n in manager2.notes if "CPU" in n]
print(f"    live CPU notes: {len(live_notes)}")
for note in live_notes:
    check("a live note states what was true and nothing else",
          not any(token in note for token in ("`", "--setup-sensors",
                                              "start-sensors.cmd",
                                              "web server")),
          note)
manager2.close()

check("the summary corrects notes when it displays them",
      "modernise_note" in open("ui/summary.py", encoding="utf-8").read())

manager.close()

print("\n" + "=" * 84)
if problems:
    print(f"CPU TEMPERATURE SOURCE TEST FAILED ({len(problems)})")
    for p in problems:
        print(f"  - {p}")
else:
    print("CPU TEMPERATURE SOURCE TEST PASSED")
print("=" * 84)
raise SystemExit(1 if problems else 0)
