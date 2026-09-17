"""Verify LUID distribution, hotspot coverage and the extended stats table."""
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





import time

import metrics as M

sm = M.SensorManager()
print("LUID assignment (round-robin, one working counter per card):")
for g in sm.gpus:
    print(f"  gpu{g.index} {g.pci or '-':12} conf={g.luid_confidence:10} "
          f"luids={g.luids}")

# Every non-NVIDIA card must have at least one counter, and no counter may be
# shared between two cards.
seen: dict[str, int] = {}
duplicates = []
for g in sm.gpus:
    for luid in g.luids:
        if luid in seen:
            duplicates.append((luid, seen[luid], g.index))
        seen[luid] = g.index
print(f"\nLUIDs shared between cards: {duplicates or 'none'}")
missing = [g.index for g in sm.gpus if g.vendor != "nvidia" and not g.luids]
print(f"Cards without any counter: {missing or 'none'}")

sm.prime()
time.sleep(1.2)
values = sm.poll()
print("\nper-card readings:")
for g in sm.gpus:
    i = g.index
    print(f"  gpu{i} {g.display_name}")
    print(f"      temp={values.get(f'gpu{i}_temp')} "
          f"hotspot={values.get(f'gpu{i}_hotspot')} "
          f"mem_temp={values.get(f'gpu{i}_mem_temp')}")
    print(f"      util={values.get(f'gpu{i}_util')} "
          f"vram={values.get(f'gpu{i}_vram_used')}/"
          f"{values.get(f'gpu{i}_vram_total')} "
          f"power={values.get(f'gpu{i}_power')}")

print("\nnotes:")
for note in sm.capabilities().notes:
    print(f"  * {note}")
sm.close()
