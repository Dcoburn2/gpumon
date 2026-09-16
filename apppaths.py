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


#: A file whose presence beside the executable means "keep my settings here".
#: That is what a portable copy does, and it is how the packaged build is told
#: apart from one: an MSIX install lives in `C:\Program Files\WindowsApps\`,
#: which is read-only at run time, so state has to go to the user's profile.
PORTABLE_MARKER = "config.json"


def state_dir() -> str:
    """Where writable state belongs, portable copy or installed package.

    A copy that already keeps its settings beside itself keeps doing so - that is
    what makes the zip portable, and moving somebody's config out from under them
    would lose their theme and their alarms. Anything else, including a packaged
    install, writes to the per-user directory.
    """
    if os.path.exists(app_path(PORTABLE_MARKER)):
        return app_dir()
    return user_dir()


def state_path(name: str) -> str:
    """A path for a file gpumon writes: settings, logs, published readings."""
    return os.path.join(state_dir(), name)


def is_packaged() -> bool:
    """True when running from an MSIX package, which is how the Store ships it.

    A packaged app has a package identity, and knowing that matters for one
    behaviour only: it cannot install a kernel driver or register a scheduled
    task, because elevation needs a restricted capability Microsoft will not
    certify for an app that asks for it up front. So the packaged build says
    where the sensor helper comes from rather than offering a button that cannot
    work.
    """
    if os.name != "nt":
        return False
    import ctypes
    try:
        length = ctypes.c_uint32(0)
        result = ctypes.windll.kernel32.GetCurrentPackageFullName(
            ctypes.byref(length), None)
    except (AttributeError, OSError):
        return False
    # APPMODEL_ERROR_NO_PACKAGE means unpackaged; anything else means it has an
    # identity, including the "buffer too small" that a real package returns.
    return result != 15700


def packaged_sensor_message() -> str:
    """What to say when the packaged build cannot set the CPU sensors up.

    A Store build cannot install a kernel driver: elevation needs a restricted
    capability Microsoft will not certify for an app that asks for it, and their
    own guidance is to keep the interface unprivileged and put the administrative
    work in a separate component. That is how gpumon is built - the sensor helper
    is its own process - so the honest answer is where the helper comes from
    rather than a button that cannot work.
    """
    return ("This packaged build cannot install the CPU sensor helper. Run "
            "start-sensors.cmd from the portable download once and this app will "
            "show the CPU temperature too - the readings are published per user, "
            "not per copy of the program.")
