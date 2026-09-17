"""Does starting the sensor helper put a window on screen?

The complaint: clicking START SENSORS flashed a console window. The task runs the
helper the same way this test does - `Start-Process python`, which gives the child
its own console - so if the window is hidden here, it is hidden when the task
starts it.

The console is looked for directly: enumerate the child's windows, find any of the
console class, and ask Windows whether it is visible.
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




import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.EnumWindows.restype = wintypes.BOOL
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

CONSOLE_CLASSES = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS",
                   "PseudoConsoleWindow"}


def console_windows(pid: int) -> list[tuple[int, str, bool]]:
    found: list[tuple[int, str, bool]] = []

    def callback(hwnd, _param):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, 256)
        name = buffer.value
        if name in CONSOLE_CLASSES:
            found.append((hwnd, name, bool(user32.IsWindowVisible(hwnd))))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


print("=" * 84)
print("NO WINDOW WHEN THE HELPER STARTS")
print("=" * 84)

print("\n[1] the launchers avoid a console in the first place")
import sensorsetup  # noqa: E402
import sensorhelper  # noqa: E402

print(f"    interpreter      : {sys.executable}")
print(f"    windowless form  : {sensorsetup.windowless_python()}")
check("a windowless interpreter is preferred when one exists",
      sensorsetup.windowless_python().lower().endswith("pythonw.exe")
      or not sys.executable.lower().endswith("python.exe"),
      sensorsetup.windowless_python())
action = sensorsetup.helper_action()
print(f"    task action      : {action}")
check("the task does not run a console interpreter",
      "python.exe" not in action.lower().replace("pythonw.exe", ""), action)
command = sensorsetup.setup_command()
check("the elevated setup asks to stay hidden", "-WindowStyle Hidden" in command,
      command[-90:])

print("\n[2] a windowless interpreter means no console is ever created")
# CREATE_NEW_CONSOLE, so the child gets its own console exactly as a scheduled
# task gives it one. With pythonw.exe there should be nothing to find at all.
script = os.path.join(os.getcwd(), "gpumon.py")
windowless = sensorsetup.windowless_python()
child = subprocess.Popen(
    [windowless, "-u", script, "--sensor-helper", "--hz", "2", "--seconds", "8"],
    creationflags=subprocess.CREATE_NEW_CONSOLE)
time.sleep(3.0)
windows = console_windows(child.pid)
print(f"    pid {child.pid} ({os.path.basename(windowless)}): {windows}")
check("running the windowless interpreter leaves no console window",
      windows == [], str(windows))
check("and it is publishing while invisible",
      bool(sensorhelper.read_sensors()),
      "no reading in the published file")
child.terminate()
time.sleep(1.0)

print("\n[3] the console interpreter used by an older task is hidden instead")
# A task registered before this change says python.exe. Re-registering it needs
# one elevation, so until that happens the helper hides its own console. Whether
# a console window is created at all depends on the session the task runs in, so
# this asks the helper directly rather than sampling a race from outside: a child
# started with its own console reports whether it managed to hide it.
code = ("import sensorhelper;"
        "print('hidden' if sensorhelper.hide_console() else 'no console');"
        "import ctypes;"
        "print('handle', bool(ctypes.windll.kernel32.GetConsoleWindow()))")
legacy = subprocess.Popen([sys.executable, "-c", code],
                          creationflags=subprocess.CREATE_NEW_CONSOLE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True)
try:
    out, err = legacy.communicate(timeout=30)
except subprocess.TimeoutExpired:
    legacy.kill()
    out, err = "", "timed out"
report = out.strip().replace("\n", " | ")
print(f"    the helper reports: {report or err.strip()[:120]}")
check("a console interpreter's window is hidden when the helper starts",
      report.startswith("hidden") and "handle True" in report,
      report or err.strip()[:120])
check("the helper hides it before doing anything else",
      "hide_console()" in open("sensorhelper.py", encoding="utf-8").read()
      and open("sensorhelper.py", encoding="utf-8").read().index("hide_console()")
      < open("sensorhelper.py", encoding="utf-8").read().index("while True"))

print("\n[4] the settings and the tasks agree on the windowless form")
check("a source checkout registers pythonw",
      "pythonw" in sensorsetup.helper_action()
      or not sys.executable.lower().endswith("python.exe"),
      sensorsetup.helper_action())
check("the elevated setup is hidden",
      "-WindowStyle Hidden" in sensorsetup.setup_command())

print("\n" + "=" * 84)
if problems:
    print(f"NO-WINDOW TEST FAILED ({len(problems)})")
    for item in problems:
        print(f"  - {item}")
else:
    print("NO-WINDOW TEST PASSED")
print("=" * 84)
raise SystemExit(1 if problems else 0)
