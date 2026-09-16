"""Verify every interface mode is wired up and starts.

The four front ends share one sensor layer and one database, so this checks the
plumbing between them: the CLI flags exist, the launcher resolves, each entry
point is importable, the desktop window builds, the terminal view starts, the web
server answers, and the icon is generated and applied.
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




import io
import os
import subprocess
import sys
import time
import urllib.request

# Keep every setting this test writes out of the user's real config.json.
os.environ.setdefault(
    "GPUMON_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "diagmon-config.json"))
# Start from a clean slate: `layout` is read when the window is built, so
# a scratch file left behind by an earlier run would change the geometry
# this test measures.
if os.path.exists(os.environ["GPUMON_CONFIG"]):
    os.remove(os.environ["GPUMON_CONFIG"])

problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    text = f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}"
    print(text.encode("ascii", "replace").decode("ascii"))
    if not ok:
        problems.append(label)


def run_cli(args: list[str], timeout: float = 60.0) -> tuple[int, str]:
    """Run the CLI and capture output without a shell pipeline."""
    result = subprocess.run([sys.executable, "gpumon.py", *args],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=timeout, errors="replace")
    return result.returncode, result.stdout


print("=" * 84)
print("INTERFACE MODES TEST")
print("=" * 84)

print("\n[1] command line surface")
code, out = run_cli(["--help"])
check("--help exits 0", code == 0, str(code))
for flag in ("--tui", "--web", "--selftest", "--list", "--report", "--host",
             "--port", "--hz", "--per-core"):
    check(f"{flag} documented", flag in out)
check("help mentions the terminal view", "terminal" in out.lower())
check("help mentions the browser", "browser" in out.lower())

print("\n[2] launchers")
for name in ("gpumon.cmd", "gpumon.sh", "gpumon.ico", "gpumon.png"):
    check(f"{name} present", os.path.exists(name))
if os.path.exists("gpumon.cmd"):
    text = open("gpumon.cmd", encoding="utf-8", errors="replace").read()
    check("launcher handles 'tui'", "tui" in text.lower())
    check("launcher handles 'web'", "web" in text.lower())
    check("launcher resolves python", "python" in text.lower())
    check("launcher installs psutil if missing", "psutil" in text)
    check("launcher is named after the program", "run.bat" not in text.lower())
if os.path.exists("gpumon.sh"):
    text = open("gpumon.sh", encoding="utf-8", errors="replace").read()
    check("shell launcher handles 'tui'", "tui" in text)
    check("shell launcher handles 'web'", "web" in text)
    check("shell launcher has a shebang", text.startswith("#!/usr/bin/env bash"))

print("\n[3] every front end imports")
for module in ("ui.monitor", "ui.summary", "tui", "termlib", "webserver",
               "webgui", "ansi_screen", "platforms"):
    try:
        __import__(module)
        check(f"{module} imports", True)
    except Exception as exc:  # noqa: BLE001
        check(f"{module} imports", False, repr(exc))

print("\n[4] platform dispatch")
import platforms as PL
check("current_platform reports windows here",
      PL.current_platform() == "windows", PL.current_platform())
try:
    import platforms.linux as LX
    check("platforms.linux imports on Windows", True)
except Exception as exc:  # noqa: BLE001
    check("platforms.linux imports on Windows", False, repr(exc))
    LX = None
if LX is not None:
    backend = LX.SysfsAmdGpuBackend()
    check("linux sysfs backend reports unavailable off Linux",
          backend.available() is False, backend.error[:50])

print("\n[5] desktop window builds with the icon")
import tkinter as tk
import gpumon

root = tk.Tk()
root.withdraw()
applied = gpumon._apply_window_icon(root)
root.update()
check("icon applied", bool(applied), os.path.basename(applied) if applied else "")
check("icon is the multi-resolution .ico", applied.endswith(".ico"),
      os.path.basename(applied))
root.destroy()

print("\n[6] terminal view starts and stops cleanly")
import metrics as M
import sampler as SP
import store as S
import termlib
import tui

DB = "diagmodes.db"
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.remove(DB + suffix)
manager = M.SensorManager(per_core=False)
store = S.Store(DB)
sampler = SP.Sampler(manager, store, sample_hz=2.0)


class ScriptedKeyboard:
    """Quits after a moment, so serve() runs its real loop then exits."""

    def __init__(self) -> None:
        self.start = time.monotonic()
        self.done = False

    def enter(self) -> None:
        pass

    def exit(self) -> None:
        pass

    def read(self, timeout: float = 0.1) -> termlib.Key | None:
        if not self.done and time.monotonic() - self.start > 2.0:
            self.done = True
            return termlib.Key(name="q")
        time.sleep(0.01)
        return None


buffer = io.StringIO()
screen = termlib.Screen(stream=buffer)
screen.width, screen.height = 100, 30
original_isatty = tui.sys_isatty
tui.sys_isatty = lambda: True
try:
    code = tui.serve(manager, store, sampler, screen=screen,
                     keyboard=ScriptedKeyboard())
finally:
    tui.sys_isatty = original_isatty
output = buffer.getvalue()
check("tui.serve returned 0", code == 0, str(code))
check("tui drew a frame", len(output) > 200, f"{len(output)} bytes")
check("tui used the alternate screen", "?1049h" in output)
sampler.stop()
store.close()
manager.close()
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        try:
            os.remove(DB + suffix)
        except OSError:
            pass

print("\n[7] web server answers")
import webserver


DB_WEB = "diagweb.db"
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB_WEB + suffix):
        os.remove(DB_WEB + suffix)
web_store = S.Store(DB_WEB)
web_manager = M.SensorManager(per_core=False)
web_sampler = SP.Sampler(web_manager, web_store, sample_hz=2.0)
web_sampler.start()


def probe() -> None:
    # WebServer takes the same manager/store/sampler stack the other front ends
    # use, so the browser sees identical data.
    server = webserver.WebServer(web_manager, web_store, web_sampler,
                                 host="127.0.0.1", port=0)
    server.start()
    try:
        port = getattr(server, "port", None) or server.address[1]
        base = f"http://127.0.0.1:{port}"
        for route in ("/", "/api/state", "/api/sample"):
            with urllib.request.urlopen(f"{base}{route}", timeout=10) as response:
                body = response.read()
                check(f"GET {route} returns content", len(body) > 20,
                      f"{len(body)} bytes")
        with urllib.request.urlopen(f"{base}/", timeout=10) as response:
            page = response.read().decode("utf-8", "replace")
        check("page contains a canvas", "<canvas" in page)
        check("page has no external asset URLs",
              "cdn." not in page and "googleapis" not in page)
        check("page implements keyboard navigation", "keydown" in page)
    finally:
        server.stop()


try:
    probe()
except Exception as exc:  # noqa: BLE001
    check("web server probe", False, repr(exc))
finally:
    web_sampler.stop()
    web_store.close()
    web_manager.close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(DB_WEB + suffix):
            try:
                os.remove(DB_WEB + suffix)
            except OSError:
                pass

print("\n" + "=" * 84)
if problems:
    print(f"INTERFACE MODES TEST FAILED ({len(problems)})")
    for p in problems:
        print(f"  - {p}")
else:
    print("INTERFACE MODES TEST PASSED")
print("=" * 84)
raise SystemExit(1 if problems else 0)
