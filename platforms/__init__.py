"""Which OS this is, and which vendor backends that OS can offer.

`SensorManager` (metrics.py) has to keep working byte-for-byte on Windows while
gaining a Linux path, so the OS-specific imports, construction and note text
live here instead of as `if os.name == "nt"` branches threaded through the
manager. Everything platform-specific is reached through
`build_platform_backends()`; the manager only ever touches the returned bundle.

Why named fields instead of a dict
----------------------------------
On Linux the Windows-only fields are not missing: they hold an
`UnsupportedBackend` that explains itself, and the Linux-only fields do the same
on Windows. That is what keeps `gpumon.py --selftest`, the terminal view and the
web view able to ask any backend `available()` / `.error` on either OS without
an AttributeError and without a platform check of their own.

Import order (this is load-bearing)
-----------------------------------
`metrics.py` imports this package from inside `SensorManager.__init__`, and the
platform modules import the backend classes back out of `metrics.py`, so the
submodules are imported *inside* `build_platform_backends()` rather than at this
module's import time. Importing `platforms` itself stays cheap and free of OS
API calls, which is what lets a Windows machine import `platforms.linux` to
prove that module is import-safe on an OS with no /sys at all.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any


def current_platform() -> str:
    """`"windows"` or `"linux"`, from `sys.platform`.

    Anything else raises instead of guessing: both branches below are written
    against APIs that exist nowhere else, and one clear message beats an
    AttributeError from deep inside the probe path.
    """
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    raise RuntimeError(
        f"gpumon supports Windows and Linux; this is {sys.platform!r}")


class UnsupportedBackend:
    """Stand-in for a backend whose OS API does not exist here.

    Has the same shape as a real backend (`name`, `error`, `available()`,
    `poll()`, `close()`), so callers can iterate over backends unconditionally
    and report "unavailable: <reason>" instead of crashing on a missing
    attribute. `poll()` returns nothing rather than zeros, which is the contract
    metrics.py opens with: an unsupported sensor is absent, never fake.
    """

    def __init__(self, name: str, error: str) -> None:
        self.name = name
        self.error = error

    def available(self) -> bool:
        return False

    def poll(self) -> dict[str, float]:
        return {}

    def close(self) -> None:
        pass


@dataclass
class PlatformBackends:
    """Every backend this OS can offer, plus how to poll and explain them.

    `system` is built here although both platforms use it (psutil reads CPU and
    RAM identically), and `nvml` is deliberately absent: it is cross-platform
    and its library loading is already handled inside metrics.py.
    """

    #: "windows" or "linux".
    name: str
    #: metrics.SystemBackend: CPU %, clocks, RAM, pagefile.
    system: Any
    #: nvidia-smi. Cross-platform, but only ever used as NVML's fallback.
    nvidia_smi: Any = None
    #: Windows performance counters (`PdhGpuBackend`).
    pdh: Any = None
    #: AMD's ADL, the Windows driver interface (`AmdAdlBackend`).
    adl: Any = None
    #: LibreHardwareMonitor's WMI provider (`LibreHardwareMonitorBackend`).
    lhm: Any = None
    #: AMD telemetry from /sys/class/drm/card*/device (`SysfsAmdGpuBackend`).
    amd_sysfs: Any = None
    #: Optional `rocm-smi` CLI, used only to fill gaps sysfs leaves.
    amd_rocm: Any = None
    #: CPU temperature from /sys (`LinuxSystemBackend`).
    cpu_thermal: Any = None
    #: Intel GPUs (`IntelGpuBackend`), which reports itself unavailable.
    intel: Any = None
    #: (label, backend) for this OS's *own* sources, in precedence order: the
    #: manager merges them with `setdefault`, so the first source that has a
    #: value wins. Empty on Windows, whose sources are the named fields above
    #: and whose merge order is the PDH/ADL/LHM sequence in metrics.py.
    sources: list[tuple[str, Any]] = field(default_factory=list)

    def errors(self) -> dict[str, str]:
        """{label: reason} for this OS's own sources that are not usable.

        `UnsupportedBackend` entries are skipped on purpose, so a Linux user is
        never told that PDH, ADL or LibreHardwareMonitor is "broken" - those do
        not exist there, and `notes()` says so once instead of three times.
        """
        out: dict[str, str] = {}
        for label, backend in self.sources:
            if isinstance(backend, UnsupportedBackend):
                continue
            if not backend.available():
                out[label] = getattr(backend, "error", "") or "unavailable"
        return out

    def notes(self) -> list[str]:
        """How this OS sources its telemetry, for a user reading the notes.

        Windows returns nothing on purpose: metrics.py's `_build_notes()`
        already emits the Windows advice (LibreHardwareMonitor, ADL, PDH)
        verbatim, and its exact wording is pinned by the Windows test suite.
        """
        if self.name != "linux":
            return []
        from platforms import linux       # local: see the import-order note
        return linux.notes(self)


def build_platform_backends(per_core: bool = False) -> PlatformBackends:
    """The backend set for the OS this process is running on.

    `per_core` only reaches the psutil system backend, exactly as before.
    """
    name = current_platform()
    if name == "windows":
        from platforms import windows     # local: see the import-order note
        return windows.build_platform_backends(per_core)
    from platforms import linux
    return linux.build_platform_backends(per_core)
