"""Verify GPU identity, per-GPU metrics and that nothing got merged."""
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

import metrics as M

sm = M.SensorManager()
print("discovered:")
for g in sm.gpus:
    print(f"  gpu{g.index} {g.vendor:7} {g.display_name:42} "
          f"vram={g.vram_total_mb} conf={g.luid_confidence} "
          f"luids={len(g.luids)} sources={g.sources}")

print("\npolling (after prime):")
sm.prime()
time.sleep(0.6)
for cycle in range(3):
    values = sm.poll()
    line = []
    for g in sm.gpus:
        i = g.index
        line.append(
            f"gpu{i}: util={values.get(f'gpu{i}_util')} "
            f"temp={values.get(f'gpu{i}_temp')} "
            f"vram={values.get(f'gpu{i}_vram_used')}/{values.get(f'gpu{i}_vram_total')}")
    print(f"  cycle {cycle}:")
    for entry in line:
        print(f"    {entry}")
    time.sleep(1)

print("\nuniqueness checks:")
names = [g.display_name for g in sm.gpus]
print(f"  {len(names)} rows, {len(set(names))} unique display names")
assert len(names) == len(set(names)), "duplicate GPU rows"
amds = [g for g in sm.gpus if g.vendor == "amd"]
print(f"  AMD rows: {len(amds)} (expected 2)")
assert len(amds) == 2
for g in amds:
    assert g.luids, f"{g.display_name} has no utilisation counter"
print("  every AMD card has at least one utilisation counter: OK")
nv = [g for g in sm.gpus if g.vendor == "nvidia"]
for g in nv:
    assert g.pci, "NVIDIA card is missing its PCI location"
    assert g.vram_total_mb, "NVIDIA card is missing its VRAM total"
print("  NVIDIA card has PCI location and VRAM total: OK")

# A card that reports a temperature must not also be described as having no
# temperature sensor: the desktop window derived that sentence from the backend
# list, which knew only about NVML/LHM, so both V620s read
# "no thermal sensor available" directly under "hotspot 34 C  mem 34 C".
print("\ntemperature reporting:")
for g in sm.gpus:
    live = [values.get(f"gpu{g.index}_{field}")
            for field in ("temp", "hotspot", "mem_temp")]
    reading = [v for v in live if v is not None]
    contradicting = bool(reading) and not g.has_temperature
    print(f"  gpu{g.index} sources={g.sources} readings={reading} "
          f"has_temperature={g.has_temperature}")
    assert not contradicting, (
        f"{g.display_name} reports {reading} but has_temperature is False "
        f"(sources={g.sources}): the UI would call it sensorless")
print("  no card contradicts itself: OK")
sm.close()
print("\nALL CHECKS PASSED")
