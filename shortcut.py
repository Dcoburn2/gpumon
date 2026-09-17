"""Create a desktop shortcut, for the first-run offer.

Windows shortcuts are COM objects, so this asks the shell to make one through
PowerShell's `WScript.Shell` - the same object the shell itself uses. There is no
standard-library way to write a `.lnk`, and adding a dependency for one file would
cost more than the call does.

Everything here is written so it can be tested: the desktop directory and the
target are passed in, and nothing is created unless it is asked for.
"""
from __future__ import annotations

import os
import subprocess
import sys

#: What the shortcut is called on the desktop.
SHORTCUT_NAME = "gpumon.lnk"

_SCRIPT = """
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut({link})
$link.TargetPath = {target}
$link.WorkingDirectory = {workdir}
$link.Description = {description}
$link.IconLocation = {icon}
$link.Save()
"""




def _no_window() -> dict[str, int]:
    """Subprocess arguments that keep a console from flashing up, on Windows.

    An empty dict elsewhere: passing `creationflags=0` is not the same as leaving
    the keyword out, and subprocess refuses the keyword on POSIX entirely.
    """
    return {"creationflags": 0x08000000} if os.name == "nt" else {}

def desktop_directory() -> str:
    """The user's desktop, wherever Windows has put it.

    Read from the shell rather than assumed to be `~/Desktop`: it is redirected
    on machines synced to OneDrive, and a shortcut written to the wrong place
    simply never appears.
    """
    if os.name != "nt":
        return os.path.join(os.path.expanduser("~"), "Desktop")
    try:
        import winreg
        key = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
               r"\User Shell Folders")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            value = str(winreg.QueryValueEx(handle, "Desktop")[0])
    except OSError:
        value = ""
    value = os.path.expandvars(value) if value else ""
    return value if value and os.path.isdir(value) else os.path.join(
        os.path.expanduser("~"), "Desktop")


def launch_target() -> tuple[str, str]:
    """What the shortcut should point at: (target, working directory).

    A packaged build points at its own executable. Running from source points at
    the interpreter with the script as an argument, which is what someone
    developing on it would want from a shortcut.
    """
    if getattr(sys, "frozen", False):
        executable = os.path.abspath(sys.executable)
        return executable, os.path.dirname(executable)
    script = os.path.abspath(os.path.join(os.path.dirname(__file__), "gpumon.py"))
    return sys.executable, os.path.dirname(script)


def create_shortcut(directory: str | None = None, name: str = SHORTCUT_NAME,
                    target: str | None = None, workdir: str | None = None,
                    icon: str | None = None) -> tuple[bool, str]:
    """Write the shortcut. Returns (created, path-or-reason)."""
    if os.name != "nt":
        return False, "shortcuts are a Windows thing"
    folder = directory or desktop_directory()
    if not os.path.isdir(folder):
        return False, f"there is no such folder: {folder}"
    path = os.path.join(folder, name)
    if target is None or workdir is None:
        target, workdir = launch_target()
    if icon is None:
        # The executable carries the icon in a packaged build; from source the
        # .ico beside the script does.
        import apppaths
        ico = apppaths.resource_path("gpumon.ico")
        icon = f"{ico},0" if os.path.exists(ico) else f"{target},0"

    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    script = _SCRIPT.format(link=quote(path), target=quote(target),
                            workdir=quote(workdir),
                            description=quote("gpumon - GPU and system telemetry"),
                            icon=quote(icon))
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            capture_output=True, text=True, errors="replace",
            **_no_window())
    except OSError as exc:
        return False, f"could not run PowerShell: {exc}"
    if result.returncode != 0 or not os.path.exists(path):
        detail = (result.stderr or result.stdout).strip().splitlines()
        return False, detail[-1] if detail else "the shortcut was not created"
    return True, path


def shortcut_exists(directory: str | None = None,
                    name: str = SHORTCUT_NAME) -> bool:
    return os.path.exists(os.path.join(directory or desktop_directory(), name))
