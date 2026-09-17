"""Test the ported ADL backend: does it read the V620 sensors?"""
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




import time

from amdsensors import (AmdAdlBackend, status_name, SENSOR_IDS)

print("=" * 84)
print("ADL BACKEND TEST (ported from LibreHardwareMonitor)")
print("=" * 84)

backend = AmdAdlBackend()
ok = backend.available()
print(f"\navailable: {ok}")
print(f"error    : {backend.error!r}")
print(f"context  : {backend.context.value}")
# Two quirks of this driver, both found by measurement and both load-bearing:
# the adapter list is only returned when the buffer comes from ADL's own
# allocator, and the first call after Main_Control_Create fails so the retry is
# what actually provides the data. Reporting where the list came from makes a
# future driver change visible here instead of as "no AMD telemetry".
print(f"adapter list came from: {backend.adapter_info_source!r}")

if not ok:
    print("\n  AMD telemetry is unavailable. On driver 32.0.12033.5037 the")
    print("  adapter list needs a buffer from ADL's allocator and a retry of")
    print("  the first call; see AmdAdlBackend._adapter_info_buffer.")
    raise SystemExit(1)

if not backend.adapter_info_source:
    print("  FAIL: the adapter list arrived without recording its source")
    raise SystemExit(1)

print(f"\n{len(backend.adapters)} AMD adapter(s):")
for a in backend.adapters:
    print(f"  idx={a.index:3} {a.name}")
    print(f"      {a.pci_label}  vendor=0x{a.vendor_id:04X}")
    print(f"      overdrive_level={backend._od_level.get(a.index)} "
          f"gcn_family={backend._family.get(a.index)} "
          f"pmlog_started={a.index in backend._pmlog_ready}")

print("\n" + "=" * 84)
print("POLLING (3 cycles, 1 s apart)")
print("=" * 84)
for cycle in range(3):
    print(f"\n--- cycle {cycle} ---")
    for a in backend.adapters:
        reading = backend.poll_adapter(a.index)
        print(f"  {a.pci_label}  methods={reading.methods}")
        if reading.errors:
            print(f"      errors: {reading.errors}")
        if reading.fields:
            for key in sorted(reading.fields):
                print(f"      {key:16} = {reading.fields[key]:.2f}")
        else:
            print("      (no values)")
    time.sleep(1.0)

print("\n" + "=" * 84)
print("LIVE CHECK: do temperatures move when the GPU is loaded?")
print("=" * 84)
adapter = backend.adapters[0]
print(f"  watching {adapter.pci_label} for 10 s")
for i in range(10):
    reading = backend.poll_adapter(adapter.index)
    keys = ("temp", "hotspot", "mem_temp", "fan", "fan_rpm", "power",
            "clock_core", "util")
    shown = {k: round(reading.fields[k], 1) for k in keys if k in reading.fields}
    print(f"    t={i:2}s  {shown}")
    time.sleep(1.0)

backend.close()
print("\nclosed")
