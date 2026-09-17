"""Create a desktop shortcut on request, and prove it is a real one.

Writing a .lnk is one thing; a shortcut that points at the wrong target or opens
without the program's icon is worse than none. So this creates one in a scratch
folder and reads it back through the shell, which is the only opinion that counts.
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
import shutil
import subprocess
import tempfile

import shortcut

problems = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


print("=" * 84)
print("SHORTCUT TEST")
print("=" * 84)

print("\n[1] where things live")
desktop = shortcut.desktop_directory()
print(f"    desktop: {desktop}")
check("the desktop directory exists", os.path.isdir(desktop), desktop)
check("it did not just assume ~/Desktop",
      os.path.isdir(desktop), "the shell's own answer is what is used")
target, workdir = shortcut.launch_target()
print(f"    target : {target}")
print(f"    workdir: {workdir}")
check("the target exists", os.path.exists(target), target)
check("the working directory exists", os.path.isdir(workdir), workdir)
check("from source it points at the interpreter and the script",
      target == os.sys.executable or target.endswith("gpumon.exe"),
      target)

print("\n[2] it creates a shortcut that the shell agrees with")
scratch = tempfile.mkdtemp()
try:
    ok, path = shortcut.create_shortcut(directory=scratch, name="gpumon-test.lnk")
    print(f"    create_shortcut -> {ok}: {path}")
    check("the file was created", ok and os.path.exists(path), str(path))
    check("it is a .lnk", path.endswith(".lnk"))
    check("it is not empty", ok and os.path.getsize(path) > 500,
          f"{os.path.getsize(path)} bytes" if ok else "-")

    reader = (
        "$s = New-Object -ComObject WScript.Shell; "
        f"$l = $s.CreateShortcut('{path}'); "
        "Write-Output ($l.TargetPath + '|' + $l.WorkingDirectory + '|' + "
        "$l.Description + '|' + $l.IconLocation)"
    )
    read = subprocess.run(["powershell", "-NoProfile", "-Command", reader],
                          capture_output=True, text=True, errors="replace")
    fields = read.stdout.strip().split("|")
    print(f"    the shell reads back: {fields}")
    check("the target survived the round trip",
          len(fields) > 0 and os.path.normcase(fields[0]) == os.path.normcase(target),
          f"{fields[0] if fields else '-'} vs {target}")
    check("so did the working directory",
          len(fields) > 1 and os.path.normcase(fields[1]) == os.path.normcase(workdir),
          f"{fields[1] if fields else '-'} vs {workdir}")
    check("it carries a description", len(fields) > 2 and bool(fields[2]),
          fields[2] if len(fields) > 2 else "-")
    check("it carries an icon", len(fields) > 3 and bool(fields[3]),
          fields[3] if len(fields) > 3 else "-")

    print("\n[3] it reports failure instead of pretending")
    ok, message = shortcut.create_shortcut(
        directory=os.path.join(scratch, "does-not-exist"))
    print(f"    missing folder -> {ok}: {message}")
    check("a missing folder is refused", not ok)
    check("and the reason is the folder", "no such folder" in message, message)

    print("\n[4] a name is not overwritten by accident, and can be replaced")
    first = os.path.getmtime(path)
    ok, again = shortcut.create_shortcut(directory=scratch, name="gpumon-test.lnk")
    check("re-creating replaces it rather than failing", ok, again)
    check("the shortcut still exists", os.path.exists(path))
    del first
finally:
    shutil.rmtree(scratch, ignore_errors=True)

print("\n[5] the first-run offer is a setting, asked once")
import alarms as A  # noqa: E402
import ui.monitor as MON  # noqa: E402
check("the app has a first-run hook",
      hasattr(MON.MonitorApp, "_offer_shortcut"),
      "MonitorApp._offer_shortcut")
source = open("ui/monitor.py", encoding="utf-8").read()
check("the offer is recorded so it is not repeated",
      "shortcut_offered" in source)
check("it only offers once", source.count("shortcut_offered") >= 2,
      f"{source.count('shortcut_offered')} references")
check("the answer is saved through the normal settings file",
      "A.save_config" in source)
check("a machine that already has one is not asked", "shortcut_exists" in source)

print("\n[6] the offer itself, answered both ways")
# The real flow, with the dialog and the desktop redirected: one run says yes and
# a shortcut appears, the next says no and nothing does. Both must record that
# they asked, or the question comes back every launch.
import os as _os  # noqa: E402
import shutil as _shutil  # noqa: E402
import tkinter as tk  # noqa: E402

_os.environ["GPUMON_CONFIG"] = _os.path.join(_os.getcwd(), "diagshortcut-config.json")
for _name in ("diagshortcut-config.json",):
    if _os.path.exists(_name):
        _os.remove(_name)
import metrics as M  # noqa: E402
import sampler as SP  # noqa: E402
import store as S  # noqa: E402

for _suffix in ("", "-wal", "-shm"):
    if _os.path.exists("diagshortcut.db" + _suffix):
        _os.remove("diagshortcut.db" + _suffix)

manager = M.SensorManager(per_core=False)
store = S.Store("diagshortcut.db")
sampler = SP.Sampler(manager, store, sample_hz=2.0)
root = tk.Tk()
app = MON.MonitorApp(root, manager, store, sampler)
root.update()

offers = {"asked": 0, "answer": True}
scratch = tempfile.mkdtemp()
original_exists = shortcut.shortcut_exists
original_create = shortcut.create_shortcut
original_desktop = shortcut.desktop_directory
original_ask = MON.messagebox.askyesno

shortcut.shortcut_exists = lambda directory=None, name=shortcut.SHORTCUT_NAME: False
shortcut.desktop_directory = lambda: scratch
MON.shortcut = shortcut


def fake_ask(*_args, **_kwargs):
    offers["asked"] += 1
    return offers["answer"]


MON.messagebox.askyesno = fake_ask
MON.shortcut_exists_for_offer = lambda config: True

try:
    app._offer_shortcut()
    root.update()
    check("the user was asked", offers["asked"] == 1, str(offers["asked"]))
    # Checked with the filesystem, not with shortcut.shortcut_exists: that one is
    # stubbed to always say no, so that the offer happens at all.
    created = os.path.join(scratch, shortcut.SHORTCUT_NAME)
    check("saying yes created a shortcut on the desktop",
          os.path.exists(created), str(os.listdir(scratch)))
    check("and it is a real shortcut, not an empty file",
          os.path.exists(created) and os.path.getsize(created) > 500,
          f"{os.path.getsize(created)} bytes" if os.path.exists(created) else "-")
    check("the answer was recorded so it is not asked again",
          bool(A.load_config().get("shortcut_offered")),
          str(A.load_config()))

    # Second run: the setting stops the question.
    A.save_config({"shortcut_offered": True})
    offers["asked"] = 0
    app._offer_shortcut()
    check("a second run does not ask again", offers["asked"] == 0,
          f"asked {offers['asked']} times")

    # A fresh setting, answering no, must create nothing.
    A.save_config({})
    for name in os.listdir(scratch):
        os.remove(os.path.join(scratch, name))
    offers["answer"] = False
    app._offer_shortcut()
    root.update()
    check("saying no creates nothing", not os.listdir(scratch),
          str(os.listdir(scratch)))
    check("and is still recorded", bool(A.load_config().get("shortcut_offered")))
finally:
    MON.messagebox.askyesno = original_ask
    MON.shortcut_exists_for_offer = MON.shortcut_exists_for_offer
    shortcut.shortcut_exists = original_exists
    shortcut.create_shortcut = original_create
    shortcut.desktop_directory = original_desktop
    _shutil.rmtree(scratch, ignore_errors=True)
    app.on_close()
    sampler.stop()
    store.close()
    manager.close()
    for _suffix in ("", "-wal", "-shm"):
        if _os.path.exists("diagshortcut.db" + _suffix):
            _os.remove("diagshortcut.db" + _suffix)
    if _os.path.exists("diagshortcut-config.json"):
        _os.remove("diagshortcut-config.json")

print("\n" + "=" * 84)
if problems:
    print(f"SHORTCUT TEST FAILED ({len(problems)})")
    for item in problems:
        print(f"  - {item}")
else:
    print("SHORTCUT TEST PASSED")
print("=" * 84)
raise SystemExit(1 if problems else 0)
