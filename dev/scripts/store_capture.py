"""Capture the screenshots a Store listing needs.

The Store wants at least one image 1366x768 or larger. Rather than asking for one
to be taken by hand, this starts the program, waits for it to have real numbers on
it, and captures the window into `store/screenshots/`.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

WIDTH, HEIGHT = 1366, 768
#: The repository root. This script lives in scripts/, one level down.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(HERE, "store", "screenshots")

user32 = ctypes.WinDLL("user32", use_last_error=True)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.SetWindowPos.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_uint]


def find_window() -> int:
    """The program's own window, whichever process owns it."""
    found = {"hwnd": 0}

    def callback(hwnd, _param):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        if title.value.startswith("gpumon "):
            found["hwnd"] = hwnd
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found["hwnd"]


def capture(hwnd: int, path: str) -> None:
    rect = (ctypes.c_long * 4)()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    left, top, right, bottom = rect
    # The window is sized to exactly the Store's minimum, so nothing is scaled.
    user32.SetWindowPos(hwnd, ctypes.c_void_p(-1), 40, 40, WIDTH, HEIGHT, 0x0040)
    time.sleep(2.5)
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    script = (
        "Add-Type -AssemblyName System.Drawing; "
        f"$bmp = New-Object System.Drawing.Bitmap {width}, {height}; "
        "$g = [System.Drawing.Graphics]::FromImage($bmp); "
        f"$g.CopyFromScreen({left}, {top}, 0, 0, $bmp.Size); $g.Dispose(); "
        f"$bmp.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png); "
        "$bmp.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", script],
                   capture_output=True, text=True, errors="replace",
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print(f"  {os.path.relpath(path, HERE)}  {width}x{height}")


def main() -> int:
    os.makedirs(TARGET, exist_ok=True)
    exe = os.path.join(HERE, "release", "gpumon.exe")
    if not os.path.exists(exe):
        print(f"build it first: {exe} does not exist")
        return 1
    print("starting gpumon for the screenshots...")
    process = subprocess.Popen([exe])
    hwnd = 0
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and not hwnd:
        time.sleep(0.5)
        hwnd = find_window()
    if not hwnd:
        process.terminate()
        print("no window appeared")
        return 1

    # Let the graphs fill: an empty chart is a poor advertisement, and the Store
    # review team should see what the app looks like in use.
    print("letting the graphs fill (30s)...")
    time.sleep(30)
    capture(hwnd, os.path.join(TARGET, "01-overview.png"))

    # A second one with the recording running, so the log panel is visible.
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"$w = New-Object -ComObject WScript.Shell; "
                    f"$w.AppActivate('gpumon')"],
                   capture_output=True, text=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    time.sleep(1.0)
    print("capturing the recording view...")
    capture(hwnd, os.path.join(TARGET, "02-window.png"))

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
    print(f"\n{len(os.listdir(TARGET))} screenshot(s) in "
          f"{os.path.relpath(TARGET, HERE)}")
    print("The Store requires at least one at 1366x768 or larger.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
