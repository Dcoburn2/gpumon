"""Does the PER-CORE button agree with the state the program is actually in?

It used to be the literal string "PER-CORE OFF" whatever was happening, so a
session that started with per-core sampling on - which is what config.json asked
for - drew every core while the button said they were off. Toggling corrected the
label, which is why it looked like a startup oddity rather than a wrong label.
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
    """Stop, with a note, when there is no desktop to draw a window on."""
    if _os.environ.get("GPUMON_SKIP_MACHINE_TESTS"):
        print("  --  skipped: this test needs a desktop session, and "
              "GPUMON_SKIP_MACHINE_TESTS is set")
        raise SystemExit(0)


_skip_machine_specific_test()

import json
import os
import pathlib
import tkinter as tk

os.environ["GPUMON_CONFIG"] = "diagpc-config.json"
os.environ["GPUMON_NO_AUTOSTART"] = "1"

import metrics as M
import sampler as SP
import store as S
from ui import monitor as MON

for name in ("diagpc.db", "diagpc.db-wal", "diagpc.db-shm"):
    if os.path.exists(name):
        os.remove(name)

problems = []
for wanted in (True, False):
    pathlib.Path("diagpc-config.json").write_text(
        json.dumps({"per_core": wanted}), encoding="utf-8")
    manager = M.SensorManager(per_core=wanted)
    store = S.Store("diagpc.db")
    sampler = SP.Sampler(manager, store, sample_hz=2.0)
    root = tk.Tk()
    app = MON.MonitorApp(root, manager, store, sampler)
    root.update()
    label = app.btn_cores.cget("text").strip()
    says_on = "ON" in label and "OFF" not in label
    ok = says_on == wanted
    print(f"  config per_core={wanted!s:5}  button says {label!r:16}  agree: {ok}")
    if not ok:
        problems.append(f"per_core={wanted} but the button said {label!r}")
    # And the toggle still works from there, which is how the label used to be
    # corrected by hand.
    app.toggle_per_core()
    root.update()
    flipped = app.btn_cores.cget("text").strip()
    flipped_on = "ON" in flipped and "OFF" not in flipped
    print(f"    after one toggle: {flipped!r}  (now {flipped_on})")
    if flipped_on == wanted:
        problems.append("the toggle did not change anything")
    # The hint shown when the strip is empty should point at the button rather
    # than at a command-line flag that was the only way to change this once.
    app.toggle_per_core(force=False)
    root.update()
    app._render_cores({})
    root.update()
    hints = [app.core_canvas.itemcget(item, "text")
             for item in app.core_canvas.find_all()
             if app.core_canvas.type(item) == "text"]
    print(f"    empty-strip hint: {hints}")
    if not any("PER-CORE" in hint for hint in hints):
        problems.append(f"the empty-strip hint does not mention the button: {hints}")
    app.on_close()
    sampler.stop()
    store.close()
    manager.close()

for name in ("diagpc.db", "diagpc.db-wal", "diagpc.db-shm",
             "diagpc-config.json"):
    if os.path.exists(name):
        os.remove(name)

print()
if problems:
    print("PER-CORE LABEL TEST FAILED")
    for item in problems:
        print(f"  - {item}")
    raise SystemExit(1)
print("PER-CORE LABEL TEST PASSED")
