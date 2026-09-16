"""Read CPU sensors straight from the kernel, through the PawnIO driver.

Why this exists: gpumon's CPU temperature used to come from LibreHardwareMonitor
over WMI, then over its HTTP server, and both routes needed that whole
application - a GUI, a menu item nobody can automate reliably, a background
process. None of it was ever necessary. Every monitoring tool, LibreHardwareMonitor
included, does the same thing underneath: load a signed kernel driver that can
execute privileged instructions, then read the CPU's model-specific registers.

This module is that layer. It talks to the PawnIO driver - a signed, open-source
framework by namazso that LibreHardwareMonitor itself uses - loads one of its
signed modules, and calls a function in it. No LibreHardwareMonitor process, no
web server, no menu, no automation.

The protocol is small, and was read from LibreHardwareMonitor's own
`PawnIo/PawnIo.cs`:

  * open ``\\?\\GLOBALROOT\\Device\\PawnIO``
  * ``DeviceIoControl`` with the load-binary code and the module blob
  * ``DeviceIoControl`` with the execute code, input = 32-byte function name
    followed by ``long[]`` arguments, output = ``long[]`` results

Opening the device needs administrator rights, which is the one thing no
user-mode program can avoid: a kernel driver only loads for an administrator, and
without one there is no way to execute the instruction that reports the CPU's
temperature. gpumon reads through a helper that runs elevated; nothing else in
the program needs privileges.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

#: PawnIO's device interface. The type value is its own magic number, not
#: something we chose; the control codes are shifts of its two function codes.
DEVICE_PATH = "\\\\?\\GLOBALROOT\\Device\\PawnIO"
DEVICE_TYPE = 41394 << 16
IOCTL_PIO_LOAD_BINARY = 0x821 << 2
IOCTL_PIO_EXECUTE_FN = 0x841 << 2
FN_NAME_LENGTH = 32

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                 wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD,
                                 wintypes.HANDLE]
kernel32.DeviceIoControl.restype = wintypes.BOOL
kernel32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD),
                                     ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class PawnIO:
    """A loaded module inside the PawnIO driver."""

    def __init__(self) -> None:
        self.handle: int | None = None
        self.error = ""
        self.module_name = ""

    # -- lifecycle -------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return bool(self.handle) and self.handle != INVALID_HANDLE_VALUE

    def open(self) -> bool:
        """Open the driver device. Needs administrator rights."""
        if os.name != "nt":
            self.error = "Windows only"
            return False
        if self.is_open:
            return True
        handle = kernel32.CreateFileW(
            DEVICE_PATH, GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL, None)
        if not handle or handle == INVALID_HANDLE_VALUE:
            code = ctypes.get_last_error()
            self.error = (f"could not open {DEVICE_PATH} "
                          f"(Windows error {code}: "
                          f"{ctypes.FormatError(code).strip()})")
            if code == 5:
                self.error += " - this needs administrator rights"
            if code == 2:
                self.error += " - the PawnIO driver is not installed"
            self.handle = None
            return False
        self.handle = handle
        self.error = ""
        return True

    def load_module(self, blob: bytes, name: str = "") -> bool:
        """Load a signed PawnIO module. The driver checks the signature."""
        if not self.open():
            return False
        written = wintypes.DWORD(0)
        buffer = ctypes.create_string_buffer(blob, len(blob))
        ok = kernel32.DeviceIoControl(
            self.handle, DEVICE_TYPE | IOCTL_PIO_LOAD_BINARY,
            ctypes.cast(buffer, ctypes.c_void_p), len(blob),
            None, 0, ctypes.byref(written), None)
        if not ok:
            code = ctypes.get_last_error()
            self.error = (f"the driver refused the module {name or '(unnamed)'} "
                          f"(Windows error {code}: "
                          f"{ctypes.FormatError(code).strip()})")
            return False
        self.module_name = name
        self.error = ""
        return True

    def load_module_file(self, path: str) -> bool:
        try:
            with open(path, "rb") as handle:
                blob = handle.read()
        except OSError as exc:
            self.error = f"could not read {path}: {exc}"
            return False
        return self.load_module(blob, os.path.basename(path))

    def close(self) -> None:
        if self.is_open:
            kernel32.CloseHandle(self.handle)
        self.handle = None

    def __enter__(self) -> "PawnIO":
        return self

    def __exit__(self, *_exc) -> bool:
        self.close()
        return False

    # -- calling into the module ----------------------------------------
    def execute(self, name: str, args: list[int] | tuple[int, ...] = (),
                out_length: int = 1) -> list[int] | None:
        """Call one of the module's functions.

        Input is the function name in a fixed 32-byte field followed by the
        arguments as 64-bit integers; the result is 64-bit integers. Returns None
        (and sets `error`) when the call fails, rather than raising, because the
        caller is usually a poll loop where a failed read is not fatal.
        """
        if not self.is_open:
            self.error = "the driver is not open"
            return None
        if len(name.encode("ascii", "replace")) >= FN_NAME_LENGTH:
            self.error = f"function name too long: {name}"
            return None
        payload = bytearray(FN_NAME_LENGTH + len(args) * 8)
        payload[:len(name)] = name.encode("ascii")
        for index, value in enumerate(args):
            payload[FN_NAME_LENGTH + index * 8:
                    FN_NAME_LENGTH + index * 8 + 8] = int(value).to_bytes(
                        8, "little", signed=True)

        in_buffer = ctypes.create_string_buffer(bytes(payload), len(payload))
        out_buffer = ctypes.create_string_buffer(max(1, out_length * 8))
        written = wintypes.DWORD(0)
        ok = kernel32.DeviceIoControl(
            self.handle, DEVICE_TYPE | IOCTL_PIO_EXECUTE_FN,
            ctypes.cast(in_buffer, ctypes.c_void_p), len(payload),
            ctypes.cast(out_buffer, ctypes.c_void_p), max(1, out_length * 8),
            ctypes.byref(written), None)
        if not ok:
            code = ctypes.get_last_error()
            self.error = (f"{name} failed (Windows error {code}: "
                          f"{ctypes.FormatError(code).strip()})")
            return None
        count = written.value // 8
        raw = out_buffer.raw[:count * 8]
        return [int.from_bytes(raw[i * 8:i * 8 + 8], "little", signed=True)
                for i in range(count)]

    def read_msr(self, index: int) -> int | None:
        """One MSR as a 64-bit value: low half is EAX, high half is EDX."""
        result = self.execute("ioctl_read_msr", [index], 1)
        if not result:
            return None
        return result[0] & 0xFFFFFFFFFFFFFFFF

    def read_smn(self, address: int) -> int | None:
        """AMD's indirect register space (Family 17h and later)."""
        result = self.execute("ioctl_read_smn", [address], 1)
        if not result:
            return None
        return result[0] & 0xFFFFFFFFFFFFFFFF

    def get_arch(self) -> int | None:
        result = self.execute("get_arch", [], 1)
        return result[0] if result else None


def module_path(name: str) -> str:
    """Where gpumon keeps the module blobs it loads.

    Two places, in order: beside the program, which is where `--setup-sensors`
    puts a freshly downloaded module, and inside the bundle, which is where a
    packaged build carries them. A release therefore needs no download at all,
    and a source checkout picks up whatever setup fetched.
    """
    import apppaths
    beside = apppaths.app_path(os.path.join("pawnio-modules", name))
    if os.path.exists(beside):
        return beside
    return apppaths.resource_path(os.path.join("pawnio-modules", name))


def modules_directory() -> str:
    """The writable place modules are installed to."""
    import apppaths
    return apppaths.app_path("pawnio-modules")


def driver_installed() -> bool:
    """True when the PawnIO service exists, so a setup step can skip itself."""
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO"):
            return True
    except OSError:
        return False
