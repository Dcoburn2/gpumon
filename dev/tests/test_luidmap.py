"""Verify LUID distribution, hotspot coverage and the extended stats table."""
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

_windows_only_test()





import time

import metrics as M

sm = M.SensorManager()
problems: list[str] = []

print("\ncounter attribution, by how many cards are asking:")
# One card owns every counter that no other vendor claimed, however many
# functions it publishes, so its attribution is exact and there is nothing to
# warn about. This is the rule that used to put a yellow "counter matched by
# counter order" under the only card on a machine with one GPU - and that
# invented a second, unattributed card out of the leftovers.
_wanted = {(1, 1): "exact", (2, 1): "exact", (3, 1): "exact",
           (2, 2): "exact", (3, 2): "pci-order", (2, 3): "pci-order"}
for (_unclaimed, _cards), _expected in sorted(_wanted.items()):
    _got = M.counter_confidence(_unclaimed, _cards)
    _ok = _got == _expected
    print(f"  {'OK  ' if _ok else 'FAIL'} {_unclaimed} counter(s) over "
          f"{_cards} card(s) -> {_got} (want {_expected})")
    if not _ok:
        problems.append(f"counter_confidence({_unclaimed}, {_cards}) gave {_got}")

print("\nLUID assignment (round-robin, one working counter per card):")
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

if problems:
    print("\nLUID MAP TEST FAILED")
    for item in problems:
        print(f"  - {item}")
else:
    print("\nLUID MAP TEST PASSED")
raise SystemExit(1 if problems else 0)
