"""The elevated helper that reads the CPU's registers and publishes them.

Reading model-specific registers needs a kernel driver, and opening that driver
needs administrator rights. A monitoring program should not run as
administrator - it polls a dozen other things and draws a window - so the
privileged part is split out into this small process:

    gpumon (ordinary user)  ---reads--->  msr-sensors.json  <---writes---  helper (elevated)

The helper is started by a scheduled task created once with highest privileges,
so starting it raises no prompt. It stops by itself: when the program that asked
for it disappears, or when nobody has asked for a reading for a while, there is
no reason to keep a process reading registers every second.

Everything here is written so it can be tested without a driver: the register
reader and the clock are passed in.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

import apppaths

#: Where the helper publishes and where the program says it is still watching.
#: In the per-user directory, not beside the program: the helper is started by a
#: scheduled task, and every copy of gpumon on the machine has to see the same
#: reading. Keeping these beside each executable meant a helper started from one
#: folder published where only that folder's copy could find it.
SENSORS_FILE = "msr-sensors.json"
HEARTBEAT_FILE = "msr-heartbeat"
DEFAULT_HZ = 1.0
#: Stop after this long with no heartbeat from a reader.
DEFAULT_IDLE_SECONDS = 45.0


def sensors_path() -> str:
    return apppaths.user_path(SENSORS_FILE)


def heartbeat_path() -> str:
    return apppaths.user_path(HEARTBEAT_FILE)


def write_atomically(path: str, payload: dict) -> None:
    """Write JSON so a reader never sees half a file.

    A plain rewrite can be read mid-flight, and a monitor reading a truncated
    JSON would report a sensor failure that never happened.
    """
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_sensors(path: str | None = None) -> dict:
    """The helper's last published reading, or an empty dict."""
    try:
        with open(path or sensors_path(), encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


# -- liveness --------------------------------------------------------------
def hide_console() -> bool:
    """Hide the console window this process owns, if it has one.

    The helper is started by a scheduled task, and a task running `python.exe`
    gets a console window: a black rectangle appears whenever the sensors are
    started, which looks like something going wrong. A GUI-subsystem interpreter
    (`pythonw.exe`) has no console at all and is preferred when the task is
    registered, but this covers the case where it is not - including tasks
    registered by an earlier version - without anyone having to re-approve
    anything.

    Returns whether a console was found and hidden.
    """
    if os.name != "nt":
        return False
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.GetConsoleWindow.restype = ctypes.c_void_p
    handle = kernel32.GetConsoleWindow()
    if not handle:
        return False
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindow(handle, 0)            # SW_HIDE
    return True


def _process_alive(pid: int) -> bool:
    """True when a process with this id is still running."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                     wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(wintypes.DWORD)]
    STILL_ACTIVE = 259
    handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED
    if not handle:
        return False
    try:
        code = wintypes.DWORD(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def touch_heartbeat(path: str | None = None) -> None:
    """A reader saying 'I am still here'."""
    target = path or heartbeat_path()
    try:
        with open(target, "a", encoding="utf-8"):
            os.utime(target, None)
    except OSError:
        pass


@dataclass
class HelperResult:
    iterations: int = 0
    published: int = 0
    reason: str = ""
    error: str = ""


def run(reader=None, parent_pid: int = 0, hz: float = DEFAULT_HZ,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        max_seconds: float | None = None,
        sensors_file: str | None = None,
        heartbeat: str | None = None,
        clock=time.monotonic, sleep=time.sleep, wall=time.time,
        on_publish=None) -> HelperResult:
    """Poll the CPU and publish readings until something says stop.

    `reader` returns a dict of metric keys to values; it defaults to the real
    register reader. Stopping conditions, in order of how often they fire:
    the parent process went away, nobody has asked for a reading recently, or the
    maximum lifetime expired.
    """
    if reader is None:
        reader = _default_reader
    # Nothing visible, whatever interpreter the task happens to use.
    hide_console()
    target = sensors_file or sensors_path()
    beat = heartbeat or heartbeat_path()
    started = clock()
    #: Wall clock, for the one comparison that has to agree with a file's mtime.
    started_wall = wall()
    result = HelperResult()
    period = 1.0 / hz if hz > 0 else 1.0
    # A reader that has never written a heartbeat is given the benefit of the
    # doubt for one idle period: the program may still be starting up.
    beat_reference = None

    while True:
        result.iterations += 1
        try:
            values = reader()
        except Exception as exc:  # noqa: BLE001 - a failed read is not fatal
            values = {}
            result.error = str(exc)
        if values:
            values = dict(values)
            values["t"] = wall()
            try:
                write_atomically(target, values)
                result.published += 1
                if on_publish:
                    on_publish(values)
            except OSError as exc:
                result.error = f"could not publish: {exc}"
        elif result.error:
            # Publish the reason rather than only the silence. Without this the
            # program's only evidence is an old reading, and the label it shows
            # says the helper is not running - when the helper is running
            # perfectly well and cannot read the processor.
            try:
                write_atomically(target, {"error": result.error, "t": wall()})
            except OSError as exc:
                result.error = f"could not publish: {exc}"

        # -- stopping conditions ----------------------------------------
        if parent_pid and not _process_alive(parent_pid):
            result.reason = "the program that started this helper has exited"
            return result
        try:
            if os.path.exists(beat):
                stamp = os.path.getmtime(beat)
                # Only a heartbeat touched while this helper was running is
                # evidence that a reader is out there. One left behind by an
                # earlier session is already older than the idle limit, so
                # trusting it stopped the helper on its first pass and reported a
                # reader that had gone away - a reader that never existed, and
                # after ten seconds while claiming forty-five.
                if stamp >= started_wall:
                    beat_reference = stamp
        except OSError:
            pass
        if beat_reference is not None:
            if wall() - beat_reference > idle_seconds:
                result.reason = ("nothing has read the sensors for "
                                 f"{idle_seconds:.0f}s")
                return result
        elif result.iterations * period > idle_seconds:
            result.reason = "no reader ever appeared"
            return result
        if max_seconds is not None and clock() - started >= max_seconds:
            result.reason = f"reached the {max_seconds:.0f}s lifetime"
            return result
        sleep(period)


def _default_reader() -> dict[str, float]:
    """The real thing: identify the CPU and read its registers."""
    import cpusensors

    readings = cpusensors.read_cpu()
    if readings.error:
        raise RuntimeError(readings.error)
    out: dict[str, float] = {}
    if readings.package is not None:
        out["cpu_temp"] = readings.package
    if readings.tjmax:
        out["cpu_tjmax"] = readings.tjmax
    if readings.frequency_mhz:
        out["cpu_clock"] = readings.frequency_mhz
    # AMD gets its package power from the energy counter rather than from a
    # register that holds watts, so it arrives the same way as the rest.
    if readings.power:
        out["cpu_power"] = readings.power
    cores = [c for c in readings.per_core if c is not None]
    if cores:
        out["cpu_core_max"] = max(cores)
    for index, value in enumerate(readings.per_core):
        if value is not None:
            out[f"core{index}_temp"] = value
    return out
