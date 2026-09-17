"""Does every module of the program import?

On Windows this is a weak check - everything imports anyway. On Linux it is the
one that matters, which is why the same question is asked in the Linux job: a
module-level `ctypes.WinDLL` makes a file unimportable there, and the failure
surfaces as an AttributeError from whichever test happened to import it first.
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

import importlib
import pathlib
import sys
import traceback

# The program is at the repository root, two levels above this file: dev/tests/ ->
# dev/ -> root.

program_modules = sorted(
    path.stem for path in pathlib.Path(".").glob("*.py")
    if path.stem not in ("gpumon",) and not path.stem.startswith("_"))
program_modules += ["gpumon", "platforms", "platforms.linux", "platforms.windows",
                    "ui", "ui.monitor", "ui.summary", "ui.widgets", "ui.themes"]

failures = []
for name in program_modules:
    try:
        importlib.import_module(name)
    except Exception:                                  # noqa: BLE001
        failures.append((name, traceback.format_exc().strip().splitlines()[-1]))

print(f"  {len(program_modules)} modules checked")
for name, why in failures:
    print(f"    FAILED {name}: {why}")
if not failures:
    print("  every module imports")
