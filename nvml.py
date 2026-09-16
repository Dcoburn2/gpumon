"""NVIDIA telemetry through NVML directly, via ctypes.

Why not nvidia-smi
------------------
Spawning `nvidia-smi` costs ~130 ms *and* relies on the OS scheduler winning a
new process. Under the heavy CPU load this tool exists to measure (Cinebench,
FurMark, a training run), the child process gets starved, the sampler thread
blocks for seconds, and samples are silently lost. NVML is an in-process query
that costs microseconds and cannot be starved that way.

`nvidia-smi` stays as a fallback for drivers that do not ship nvml.dll.

Only `argtypes`/`restype` are declared here for the same reason PDH needed it:
without them ctypes truncates LPWSTR and pointer arguments to 32 bits.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from asyncpoll import AsyncPoller

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

NVML_SUCCESS = 0

# Temperature sensors (nvmlTemperatureSensors_t)
NVML_TEMPERATURE_GPU = 0
NVML_TEMPERATURE_MEMORY = 1

# Temperature thresholds (nvmlTemperatureThresholds_t). Index 0 is the maximum
# operating temperature; index 1 is where clocks start being reduced.
NVML_TEMPERATURE_THRESHOLD_SHUTDOWN = 0
NVML_TEMPERATURE_THRESHOLD_SLOWDOWN = 1

# Clock domains
NVML_CLOCK_GRAPHICS = 0
NVML_CLOCK_SM = 1
NVML_CLOCK_MEM = 2
NVML_CLOCK_VIDEO = 3

# Memory locations
NVML_MEMORY_LOCATION_L1_CACHE = 0
NVML_MEMORY_LOCATION_L2_CACHE = 1
NVML_MEMORY_LOCATION_DEVICE_MEMORY = 2

# Throttle reasons (nvmlClocksThrottleReason_t), a bitfield. Only the thermal and
# hardware-brake bits mean the GPU actually reduced clocks to protect itself;
# e.g. 0x1 (GpuIdle) and 0x4 are normal and must not be treated as throttling.
NVML_PERF_REASON_NONE = 0x0000000000000000
NVML_PERF_REASON_GPU_IDLE = 0x0000000000000001
NVML_PERF_REASON_APPLICATIONS_CLOCKS_SETTING = 0x0000000000000002
NVML_PERF_REASON_SW_POWER_CAP = 0x0000000000000004
NVML_PERF_REASON_HW_SLOWDOWN = 0x0000000000000008
NVML_PERF_REASON_SYNC_BOOST = 0x0000000000000010
NVML_PERF_REASON_SW_THERMAL_SLOWDOWN = 0x0000000000000020
NVML_PERF_REASON_HW_THERMAL_SLOWDOWN = 0x0000000000000040
NVML_PERF_REASON_HW_POWER_BRAKE_SLOWDOWN = 0x0000000000000080
NVML_PERF_REASON_DISPLAY_CLOCK_SETTING = 0x0000000000000100

# Bits that mean "clocks were reduced to protect the part".
THROTTLE_REASON_BITS = (
    NVML_PERF_REASON_HW_SLOWDOWN
    | NVML_PERF_REASON_SW_THERMAL_SLOWDOWN
    | NVML_PERF_REASON_HW_THERMAL_SLOWDOWN
    | NVML_PERF_REASON_HW_POWER_BRAKE_SLOWDOWN
)

# Bits that are normal operating states, not throttling.
BENIGN_REASON_BITS = (
    NVML_PERF_REASON_GPU_IDLE
    | NVML_PERF_REASON_APPLICATIONS_CLOCKS_SETTING
    | NVML_PERF_REASON_SYNC_BOOST
    | NVML_PERF_REASON_DISPLAY_CLOCK_SETTING
)

# --------------------------------------------------------------------------
# Structs
# --------------------------------------------------------------------------


class MemoryInfo(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong),
                ("free", ctypes.c_ulonglong),
                ("used", ctypes.c_ulonglong)]


class Utilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class UtilizationV2(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint),
                ("encoder", ctypes.c_uint), ("decoder", ctypes.c_uint),
                ("jpeg", ctypes.c_uint), ("ofa", ctypes.c_uint)]


def _make_string_struct(size: int = 96) -> type[ctypes.Structure]:
    return type("NvmlString", (ctypes.Structure,),
                {"_fields_": [("value", ctypes.c_char * size)]})


NVML_DEVICE_NAME = 96
NVML_DEVICE_NAME_V2 = 96
NVML_DRIVER_VERSION = 80
NVML_VBIOS_VERSION = 32

DriverVersion = _make_string_struct(NVML_DRIVER_VERSION)


# --------------------------------------------------------------------------
# Backend
# --------------------------------------------------------------------------


class PciInfo(ctypes.Structure):
    _fields_ = [("busIdLegacy", ctypes.c_char * 16),
                ("domain", ctypes.c_uint),
                ("bus", ctypes.c_uint),
                ("device", ctypes.c_uint),
                ("pciDeviceId", ctypes.c_uint),
                ("pciSubSystemId", ctypes.c_uint),
                ("busId", ctypes.c_char * 32)]


@dataclass
class NvmlDevice:
    handle: ctypes.c_void_p
    index: int
    uuid: str = ""
    name: str = ""
    pci_bus_id: str = ""
    bus: int | None = None
    pci: str = ""


class NvmlBackend:
    """In-process NVIDIA telemetry. Falls back cleanly when NVML is absent."""

    name = "nvml"

    CANDIDATES = ("nvml.dll", r"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll",
                  "libnvidia-ml.so.1", "libnvidia-ml.so")
    UtilsStruct = type("UtilizationTypes", (ctypes.Structure,),
                       {"_fields_": [(n, ctypes.c_uint) for n in
                                     ("gpu", "memory", "encoder", "decoder",
                                      "jpeg", "ofa")]})

    def __init__(self) -> None:
        self.lib: Any = None
        self.error = ""
        self.devices: list[NvmlDevice] = []
        self._util_struct: Any = Utilization
        self._util_fn = "nvmlDeviceGetUtilizationRates"
        self._mem_temp_supported = True
        self._init_done = False
        # Static per-device values (VRAM total, thermal limit) do not change;
        # caching them removes two driver calls from every poll.
        self._static: dict[int, dict[str, float]] = {}
        self._static_refresh = 0.0
        self.POLL_INTERVAL = 1.0
        self._poller: AsyncPoller | None = None
        self._load()

    # -- loading ---------------------------------------------------------
    def _load(self) -> None:
        for candidate in self.CANDIDATES:
            if os.path.sep in candidate and not os.path.exists(candidate):
                continue
            try:
                self.lib = ctypes.WinDLL(candidate) if sys.platform == "win32" \
                    else ctypes.CDLL(candidate)
                break
            except OSError:
                continue
        if self.lib is None:
            self.error = "nvml library not found"
            return
        self._declare()

    def _declare(self) -> None:
        lib = self.lib

        def sig(name: str, args: list[Any], restype: Any = ctypes.c_int) -> None:
            try:
                fn = getattr(lib, name)
            except AttributeError:
                return
            fn.argtypes = args
            fn.restype = restype

        D = ctypes.c_void_p
        U = ctypes.c_uint
        I = ctypes.c_int
        ULL = ctypes.c_ulonglong
        F = ctypes.c_float
        CS = ctypes.c_char_p

        sig("nvmlInit_v2", [])
        sig("nvmlInit", [])
        sig("nvmlShutdown", [])
        sig("nvmlErrorString", [I], CS)
        sig("nvmlDeviceGetCount_v2", [ctypes.POINTER(U)])
        sig("nvmlDeviceGetCount", [ctypes.POINTER(U)])
        sig("nvmlDeviceGetHandleByIndex_v2", [U, ctypes.POINTER(D)])
        sig("nvmlDeviceGetHandleByIndex", [U, ctypes.POINTER(D)])
        sig("nvmlDeviceGetTemperature", [D, I, ctypes.POINTER(U)])
        sig("nvmlDeviceGetTemperatureThreshold", [D, I, ctypes.POINTER(U)])
        sig("nvmlDeviceGetUtilizationRates", [D, ctypes.POINTER(Utilization)])
        sig("nvmlDeviceGetUtilizationRates_v2", [D, ctypes.POINTER(UtilizationV2)])
        sig("nvmlDeviceGetMemoryInfo", [D, ctypes.POINTER(MemoryInfo)])
        sig("nvmlDeviceGetClockInfo", [D, I, ctypes.POINTER(U)])
        sig("nvmlDeviceGetMaxClockInfo", [D, I, ctypes.POINTER(U)])
        sig("nvmlDeviceGetPowerUsage", [D, ctypes.POINTER(U)])           # mW
        sig("nvmlDeviceGetEnforcedPowerLimit", [D, ctypes.POINTER(U)])   # mW
        sig("nvmlDeviceGetPowerManagementLimit", [D, ctypes.POINTER(U)])
        sig("nvmlDeviceGetFanSpeed", [D, ctypes.POINTER(U)])
        sig("nvmlDeviceGetNumFans", [D, ctypes.POINTER(U)])
        sig("nvmlDeviceGetPerformanceState", [D, ctypes.POINTER(I)])
        sig("nvmlDeviceGetCurrentClocksThrottleReasons", [D, ctypes.POINTER(ctypes.c_ulonglong)])
        sig("nvmlDeviceGetName", [D, ctypes.c_char_p, U])
        sig("nvmlDeviceGetUUID", [D, ctypes.c_char_p, U])
        sig("nvmlDeviceGetPciInfo_v3", [D, ctypes.POINTER(PciInfo)])
        sig("nvmlDeviceGetPciInfo", [D, ctypes.POINTER(PciInfo)])
        sig("nvmlDeviceGetMemoryBusWidth", [D, ctypes.POINTER(U)])

    # -- availability ----------------------------------------------------
    def available(self) -> bool:
        if self._init_done:
            return bool(self.devices)
        self._init_done = True
        if self.lib is None:
            return False
        ok = False
        for init_name in ("nvmlInit_v2", "nvmlInit"):
            fn = getattr(self.lib, init_name, None)
            if fn is None:
                continue
            rc = fn()
            if rc == NVML_SUCCESS:
                ok = True
                break
        if not ok:
            self.error = "nvmlInit failed (driver/library version mismatch?)"
            return False

        count = ctypes.c_uint(0)
        getter = getattr(self.lib, "nvmlDeviceGetCount_v2", None) \
            or getattr(self.lib, "nvmlDeviceGetCount", None)
        if getter is None or getter(ctypes.byref(count)) != NVML_SUCCESS:
            self.error = "could not enumerate NVIDIA devices"
            return False

        handle_fn = getattr(self.lib, "nvmlDeviceGetHandleByIndex_v2", None) \
            or getattr(self.lib, "nvmlDeviceGetHandleByIndex", None)
        devices: list[NvmlDevice] = []
        for i in range(count.value):
            handle = ctypes.c_void_p()
            if handle_fn(ctypes.c_uint(i), ctypes.byref(handle)) != NVML_SUCCESS:
                continue
            pci_bus, bus_number, pci_label = self._read_pci(handle)
            devices.append(NvmlDevice(handle=handle, index=i,
                                      name=self._read_name(handle),
                                      uuid=self._read_uuid(handle),
                                      pci_bus_id=pci_bus, bus=bus_number,
                                      pci=pci_label))
        self.devices = devices
        self._detect_util_variant()
        if not devices:
            self.error = "no NVIDIA devices reported by NVML"
        return bool(devices)

    def _read_name(self, handle: ctypes.c_void_p) -> str:
        buf = ctypes.create_string_buffer(NVML_DEVICE_NAME_V2)
        try:
            if self.lib.nvmlDeviceGetName(handle, buf, len(buf)) == NVML_SUCCESS:
                return buf.value.decode("utf-8", "replace")
        except (AttributeError, OSError):
            pass
        return f"NVIDIA GPU"

    def _read_pci(self, handle: ctypes.c_void_p) -> tuple[str, int | None, str]:
        """(busId string, PCI bus number, 'bus:dev.fn') for LUID/card matching."""
        info = PciInfo()
        for fn_name in ("nvmlDeviceGetPciInfo_v3", "nvmlDeviceGetPciInfo"):
            fn = getattr(self.lib, fn_name, None)
            if fn is None:
                continue
            try:
                if fn(handle, ctypes.byref(info)) == NVML_SUCCESS:
                    bus_id = info.busId.decode("utf-8", "replace").strip("\x00")
                    label = f"PCI {info.bus:02x}:{info.device:02x}.0"
                    return bus_id, int(info.bus), label
            except (OSError, TypeError):
                continue
        return "", None, ""

    def _read_uuid(self, handle: ctypes.c_void_p) -> str:
        buf = ctypes.create_string_buffer(96)
        try:
            if self.lib.nvmlDeviceGetUUID(handle, buf, len(buf)) == NVML_SUCCESS:
                return buf.value.decode("utf-8", "replace")
        except (AttributeError, OSError):
            pass
        return ""

    def _detect_util_variant(self) -> None:
        """Some drivers only export the v2 utilization struct."""
        if hasattr(self.lib, "nvmlDeviceGetUtilizationRates"):
            self._util_fn = "nvmlDeviceGetUtilizationRates"
            self._util_struct = Utilization
        elif hasattr(self.lib, "nvmlDeviceGetUtilizationRates_v2"):
            self._util_fn = "nvmlDeviceGetUtilizationRates_v2"
            self._util_struct = UtilizationV2

    # -- querying --------------------------------------------------------
    def _u32(self, fn_name: str, handle: ctypes.c_void_p, arg: int | None = None) -> float | None:
        fn = getattr(self.lib, fn_name, None)
        if fn is None:
            return None
        out = ctypes.c_uint(0)
        try:
            # Pass the enum selector as a plain int: nvmlTemperatureSensors_t and
            # friends are enum types, and some builds reject a c_uint wrapper.
            if arg is None:
                rc = fn(handle, ctypes.byref(out))
            else:
                rc = fn(handle, arg, ctypes.byref(out))
        except (OSError, TypeError, ctypes.ArgumentError):
            return None
        return float(out.value) if rc == NVML_SUCCESS else None

    # -- polling ---------------------------------------------------------
    def start_poller(self) -> None:
        """Begin background sampling; `poll()` then returns cached values."""
        if self._poller is None:
            self._poller = AsyncPoller("nvml-poller", self._poll_live,
                                       self.POLL_INTERVAL)
        self._poller.prime_once()
        self._poller.start()

    def stop_poller(self) -> None:
        if self._poller is not None:
            self._poller.stop()
            self._poller = None

    def poll(self) -> dict[str, float]:
        """Cached values. Never touches the driver, so never stalls."""
        if self._poller is None:
            return self._poll_live()
        return self._poller.snapshot()

    def cache_age(self) -> float:
        return self._poller.age() if self._poller is not None else 0.0

    def _refresh_static(self) -> None:
        """Cache the per-device values that cannot change mid-session."""
        if self._static and (time.monotonic() - self._static_refresh) < 300.0:
            return
        for dev in self.devices:
            fields: dict[str, float] = {}
            i = dev.index
            h = dev.handle
            info = MemoryInfo()
            try:
                if self.lib.nvmlDeviceGetMemoryInfo(h, ctypes.byref(info)) == NVML_SUCCESS:
                    fields[f"gpu{i}_vram_total"] = info.total / (1024 * 1024)
            except (OSError, TypeError):
                pass            # Index 1 is where clocks begin to be reduced; index 0 is the max
            # operating temperature. Prefer the slowdown point, which is the
            # number users recognise as "the throttle limit".
            slowdown = self._u32("nvmlDeviceGetTemperatureThreshold", h,
                                 NVML_TEMPERATURE_THRESHOLD_SLOWDOWN)
            shutdown = self._u32("nvmlDeviceGetTemperatureThreshold", h,
                                 NVML_TEMPERATURE_THRESHOLD_SHUTDOWN)
            for candidate in (slowdown, shutdown):
                if candidate is not None and 40.0 < candidate < 115.0:
                    fields[f"gpu{i}_temp_limit"] = candidate
                    break
            limit = self._u32("nvmlDeviceGetEnforcedPowerLimit", h)
            if limit is None:
                limit = self._u32("nvmlDeviceGetPowerManagementLimit", h)
            if limit is not None:
                fields[f"gpu{i}_power_limit"] = limit / 1000.0
            fans = self._u32("nvmlDeviceGetNumFans", h)
            if fans is not None:
                fields[f"gpu{i}_num_fans"] = fans
            self._static[i] = fields
        self._static_refresh = time.monotonic()

    def _poll_live(self) -> dict[str, float]:
        """One full driver poll. Only ever called from the poller thread."""
        if not self.devices:
            return {}
        self._refresh_static()
        out: dict[str, float] = {}

        for dev in self.devices:
            i = dev.index
            h = dev.handle
            out.update(self._static.get(i, {}))

            temp = self._u32("nvmlDeviceGetTemperature", h, NVML_TEMPERATURE_GPU)
            if temp is not None:
                out[f"gpu{i}_temp"] = temp

            if self._mem_temp_supported:
                mem_temp = self._u32("nvmlDeviceGetTemperature", h,
                                     NVML_TEMPERATURE_MEMORY)
                if mem_temp is not None and mem_temp > 0:
                    out[f"gpu{i}_mem_temp"] = mem_temp
                elif mem_temp is None:
                    # Not supported on this board; stop asking every tick.
                    self._mem_temp_supported = False

            util = self._query_utilization(h)
            if util is not None:
                out[f"gpu{i}_util"] = float(util.gpu)
                out[f"gpu{i}_mem_util"] = float(util.memory)

            info = MemoryInfo()
            try:
                rc = self.lib.nvmlDeviceGetMemoryInfo(h, ctypes.byref(info))
            except (OSError, TypeError):
                rc = -1
            if rc == NVML_SUCCESS:
                out[f"gpu{i}_vram_used"] = info.used / (1024 * 1024)

            for domain, key in ((NVML_CLOCK_GRAPHICS, "clock_core"),
                                (NVML_CLOCK_SM, "clock_sm"),
                                (NVML_CLOCK_MEM, "clock_mem")):
                val = self._u32("nvmlDeviceGetClockInfo", h, domain)
                if val is not None:
                    out[f"gpu{i}_{key}"] = val

            power = self._u32("nvmlDeviceGetPowerUsage", h)
            if power is not None:
                out[f"gpu{i}_power"] = power / 1000.0      # mW -> W

            fan = self._u32("nvmlDeviceGetFanSpeed", h)
            if fan is not None and (out.get(f"gpu{i}_num_fans") or 1) >= 1:
                out[f"gpu{i}_fan"] = fan

            reasons = ctypes.c_ulonglong(0)
            try:
                rc = self.lib.nvmlDeviceGetCurrentClocksThrottleReasons(
                    h, ctypes.byref(reasons))
            except (OSError, TypeError):
                rc = -1
            if rc == NVML_SUCCESS:
                bits = reasons.value
                out[f"gpu{i}_throttle"] = 1.0 if (bits & THROTTLE_REASON_BITS) else 0.0
                # Expose the non-benign bits so the summary can explain *why*.
                out[f"gpu{i}_throttle_reasons"] = float(bits & ~BENIGN_REASON_BITS)
        return out

    def _query_utilization(self, handle: ctypes.c_void_p) -> Any | None:
        fn = getattr(self.lib, self._util_fn, None)
        if fn is None:
            return None
        struct = self._util_struct()
        try:
            rc = fn(handle, ctypes.byref(struct))
        except (OSError, TypeError):
            return None
        return struct if rc == NVML_SUCCESS else None

    def close(self) -> None:
        self.stop_poller()
        if self.lib is not None and self._init_done:
            try:
                self.lib.nvmlShutdown()
            except (AttributeError, OSError):
                pass
