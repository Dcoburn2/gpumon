"""Where gpumon's own files live, frozen or not.

PyInstaller unpacks a frozen application into a temporary directory and clears
it afterwards, so `__file__` points at somewhere that will not exist next run.
Settings, logs, generated tools and the saved VRAM baseline all have to sit next
to the executable the user actually launched, which is what this returns.

Nothing imports this at the top of `ui.themes`-style leaf modules; it has no
dependencies at all, so anything may use it.
"""
from __future__ import annotations

import os
import sys


def app_dir() -> str:
    """The directory holding gpumon: the exe's folder when frozen, else here."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def app_path(name: str) -> str:
    """A path inside `app_dir()`."""
    return os.path.join(app_dir(), name)


def resource_path(name: str) -> str:
    """A bundled read-only file: the icon, when frozen, travels in the bundle.

    PyInstaller sets `sys._MEIPASS` to the unpack directory, which is the only
    place bundled data files exist. Writable files must use `app_path()`.
    """
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        candidate = os.path.join(bundled, name)
        if os.path.exists(candidate):
            return candidate
    return app_path(name)


def user_dir() -> str:
    """A per-user directory shared by every copy of gpumon on the machine.

    Some state belongs to the user rather than to one copy of the program. The
    sensor helper is the clear case: it is started by a scheduled task, and if
    each copy kept its readings beside its own executable, a helper started from
    one folder would publish into that folder while the copy you are looking at
    waited for a reading that never arrived - which is exactly what happened
    while the task pointed at a build in `dist/`.

    Falls back to the program's own directory if the environment has no such
    place, so nothing depends on this succeeding.
    """
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
            or os.path.expanduser("~"))
    if not base:
        return app_dir()
    target = os.path.join(base, "gpumon")
    try:
        os.makedirs(target, exist_ok=True)
    except OSError:
        return app_dir()
    return target


def user_path(name: str) -> str:
    """A path inside `user_dir()`."""
    return os.path.join(user_dir(), name)
