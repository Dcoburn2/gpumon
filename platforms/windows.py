"""The Windows backend set: the existing implementations, handed out.

Nothing is reimplemented here. `NvidiaSmiBackend`, `PdhGpuBackend`,
`LibreHardwareMonitorBackend` and `SystemBackend` live in metrics.py,
`AmdAdlBackend` in amdsensors.py, and `enumerate_display_adapters` in
metrics.py; this module only decides which of them to construct for a given
`per_core` and labels the Windows-only ones for the manager.

That is deliberate: the Windows behaviour of the program is pinned by a test
suite, and the cheapest way to keep it byte-for-byte identical is to hand out
the very same objects the manager used to build inline. The Linux-only fields
hold an `UnsupportedBackend`, so GPU discovery in metrics.py can stay
platform-neutral and `gpumon.py --selftest` can still print every backend on
either OS.

The names below are re-exported so callers can reach the Windows
implementations through the platform layer rather than importing metrics.py
directly.
"""
from __future__ import annotations

import metrics as M
from amdsensors import AmdAdlBackend
from platforms import PlatformBackends, UnsupportedBackend

#: Re-exports of the Windows implementations this platform provides.
NvidiaSmiBackend = M.NvidiaSmiBackend
PdhGpuBackend = M.PdhGpuBackend
LibreHardwareMonitorBackend = M.LibreHardwareMonitorBackend
SystemBackend = M.SystemBackend
enumerate_display_adapters = M.enumerate_display_adapters


def build_platform_backends(per_core: bool = False) -> PlatformBackends:
    """Windows backends: NVML/nvidia-smi, PDH, ADL and LibreHardwareMonitor."""
    return PlatformBackends(
        name="windows",
        system=M.SystemBackend(per_core=per_core),
        nvidia_smi=M.NvidiaSmiBackend(),
        pdh=M.PdhGpuBackend(),
        adl=AmdAdlBackend(),
        lhm=M.LibreHardwareMonitorBackend(),
        # Linux-only sources, present so every call site can address them.
        amd_sysfs=UnsupportedBackend(
            "sysfs",
            "amdgpu's /sys/class/drm telemetry is Linux-only"),
        amd_rocm=UnsupportedBackend(
            "rocm-smi",
            "rocm-smi ships with the ROCm stack on Linux; AMD telemetry on "
            "Windows comes from ADL"),
        cpu_thermal=UnsupportedBackend(
            "thermal",
            "CPU temperature on Windows needs a kernel driver, so it comes "
            "from LibreHardwareMonitor's WMI provider instead"),
        intel=UnsupportedBackend(
            "intel",
            "Intel GPU telemetry on Windows would need a vendor library, and "
            "gpumon has none"),
    )
