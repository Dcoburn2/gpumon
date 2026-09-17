"""Sensor backends for gpumon.

Contract: each backend exposes `available()` and `poll() -> {metric: float}`.
Any metric a backend cannot read is absent, so unsupported sensors show as
"unavailable" instead of fake zeros.

Backends
--------
NvidiaSmiBackend             full NVIDIA telemetry: temp, hotspot limit, clocks,
                             VRAM, power, fan, memory util.
PdhGpuBackend                cross-vendor GPU utilization + dedicated VRAM from
                             Windows performance counters. This is what covers
                             the AMD adapter, which nvidia-smi cannot see.
SystemBackend                CPU / RAM / pagefile via psutil.
LibreHardwareMonitorBackend  CPU and AMD GPU temperatures over WMI (requires
                             LibreHardwareMonitor running elevated with its WMI
                             provider enabled). Nothing else reads AMD sensors.

Which of those exist depends on the OS, so `SensorManager` no longer builds
them itself: `platforms.build_platform_backends()` returns the set for this
machine (see platforms/windows.py and platforms/linux.py). On Linux the AMD
telemetry comes from amdgpu's sysfs attributes and CPU temperature from hwmon
or the kernel thermal zones, while the Windows-only backends above are replaced
by stubs that report themselves unavailable with a reason. The metric
vocabulary below is shared by all of them.

Windows PDH notes (learned the hard way, worth keeping)
-------------------------------------------------------
* Every PDH function must have `argtypes` declared. Without them ctypes passes
  `LPCWSTR` as a truncated 32-bit pointer and every call fails with a bogus
  error code (0xC0000BBC / PDH_CSTATUS_NO_OBJECT).
* PdhAddCounterW rejects wildcards, so utilisation has to be read by expanding
  the wildcard path first and adding one counter per concrete instance.
* Per-adapter utilisation is the sum over competing processes, so we aggregate
  by LUID and clamp to 100.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

try:
    import ctypes.wintypes as wintypes
except (ImportError, ValueError):
    # ctypes.wintypes is a Windows-only module: it defines VARIANT_BOOL with
    # ctypes' "v" type code, which does not exist elsewhere, so importing it
    # raises ValueError on Linux and this module would not import at all. The
    # aliases below are the same types wintypes provides on Windows (DWORD is
    # c_ulong, 4 bytes under Windows' LLP64), and the only code that uses them
    # is the PDH backend, which is Windows-only anyway.
    class _FallbackWinTypes:
        DWORD = ctypes.c_uint32
        BOOL = ctypes.c_int32
        LPCWSTR = ctypes.c_wchar_p
        LPWSTR = ctypes.c_wchar_p
        HWND = ctypes.c_void_p

    wintypes = _FallbackWinTypes()  # type: ignore[assignment]

from asyncpoll import AsyncPoller
from gpuenum import (PhysicalGpu, bus_sort_key, classify_luids, distribute_luids,
                     enumerate_physical_gpus, measure_luid_activity,
                     order_luids_by_activity)
from nvml import NvmlBackend

import msrbackend


def card_vram_estimate(card: PhysicalGpu,
                       mem_totals: dict[str, float]) -> float | None:
    """VRAM total for a physical card.

    The registry's `HardwareInformation.qwMemorySize` is accurate on this machine
    (30704 MB for a 32 GB V620, 5120 MB for the P2000), so it is preferred over
    the display-class `MemorySize` value, which is capped at 4 GB.
    """
    return _registry_vram_for([card], card.name, card.vendor)


def _registry_vram_for(cards: list[PhysicalGpu], name: str,
                       vendor: str) -> float | None:
    """Registry-reported VRAM total for the card matching name+vendor.

    Names differ between sources for the same card - NVML reports "Quadro
    P2000" where the registry driver description is "NVIDIA Quadro P2000" - so
    match on either name containing the other after stripping vendor prefixes.
    """
    def normalise(text: str) -> str:
        lowered = text.strip().lower()
        for prefix in ("nvidia ", "amd ", "advanced micro devices, inc. ",
                       "(r)", "(tm)"):
            lowered = lowered.replace(prefix, "")
        return " ".join(lowered.split())

    wanted = normalise(name)
    rows = [r for r in enumerate_display_adapters() if r.vendor == vendor]
    for row in rows:
        candidate = normalise(row.name)
        if candidate == wanted or wanted in candidate or candidate in wanted:
            if row.vram_total_mb:
                return row.vram_total_mb
    # Fall back to the vendor's largest reported total when several identical
    # cards share a name.
    totals = [r.vram_total_mb for r in rows if r.vram_total_mb]
    return max(totals) if totals else None

# ==========================================================================
# Metric catalogue
# ==========================================================================


@dataclass(frozen=True)
class MetricDef:
    key: str
    label: str
    unit: str
    kind: str = "line"            # line | percent | temp | clock | bool
    higher_is_worse: bool = False
    precision: int = 0

    def fmt(self, value: float | None) -> str:
        if value is None:
            return "--"
        return f"{value:.{self.precision}f}"


GPU_FIELDS: dict[str, MetricDef] = {
    "temp":        MetricDef("temp", "Temperature", "C", "temp", True, 0),
    "hotspot":     MetricDef("hotspot", "Hotspot (junction)", "C", "temp", True, 0),
    "mem_temp":    MetricDef("mem_temp", "Memory Temperature", "C", "temp", True, 0),
    "util":        MetricDef("util", "GPU Utilization", "%", "percent", False, 1),
    "mem_util":    MetricDef("mem_util", "Memory Bandwidth Usage", "%", "percent", False, 1),
    "vram_used":   MetricDef("vram_used", "VRAM Used", "MB", "line", False, 0),
    "vram_total":  MetricDef("vram_total", "VRAM Total", "MB", "line", False, 0),
    "vram_percent": MetricDef("vram_percent", "VRAM Utilization", "%", "percent", False, 1),
    "clock_core":  MetricDef("clock_core", "Core Clock", "MHz", "clock", False, 0),
    "clock_sm":    MetricDef("clock_sm", "SM Clock", "MHz", "clock", False, 0),
    "clock_mem":   MetricDef("clock_mem", "Memory Clock", "MHz", "clock", False, 0),
    "power":       MetricDef("power", "Board Power", "W", "line", False, 1),
    "power_limit": MetricDef("power_limit", "Power Limit", "W", "line", False, 1),
    "fan":         MetricDef("fan", "Fan Speed", "%", "percent", False, 0),
    "temp_limit":  MetricDef("temp_limit", "Thermal Limit", "C", "temp", False, 0),
    "throttle":    MetricDef("throttle", "Thermal Throttling", "", "bool", False, 0),
    "power_percent": MetricDef("power_percent", "Power Utilization", "%", "percent", False, 1),
}

SYSTEM_METRICS: dict[str, MetricDef] = {
    "cpu_util":      MetricDef("cpu_util", "CPU Utilization", "%", "percent", False, 1),
    "cpu_temp":      MetricDef("cpu_temp", "CPU Temperature", "C", "temp", True, 1),
    "cpu_clock":     MetricDef("cpu_clock", "CPU Clock", "MHz", "clock", False, 0),
    "cpu_power":     MetricDef("cpu_power", "CPU Package Power", "W", "line", False, 1),
    "ram_used":      MetricDef("ram_used", "RAM Used", "MB", "line", False, 0),
    "ram_total":     MetricDef("ram_total", "RAM Total", "MB", "line", False, 0),
    "ram_percent":   MetricDef("ram_percent", "RAM Utilization", "%", "percent", False, 1),
    "pagefile_used": MetricDef("pagefile_used", "Pagefile Used", "MB", "line", False, 0),
}


def metric_def(key: str) -> MetricDef:
    if key.startswith("gpu"):
        head, _, tail = key.partition("_")
        if tail in GPU_FIELDS:
            d = GPU_FIELDS[tail]
            return MetricDef(key, d.label, d.unit, d.kind, d.higher_is_worse, d.precision)
    if key in SYSTEM_METRICS:
        return SYSTEM_METRICS[key]
    if key.startswith("core") and key.endswith("_util"):
        return MetricDef(key, f"Core {key[4:-5]}", "%", "percent", False, 1)
    if key.startswith("core") and key.endswith("_temp"):
        # Per-core CPU temperature, which only the Linux backend can read
        # (platforms/linux.py): the hwmon chip carries one sensor per core.
        return MetricDef(key, f"Core {key[4:-5]}", "C", "temp", True, 1)
    return MetricDef(key, key, "")


def is_gpu_key(key: str) -> bool:
    return key.startswith("gpu") and "_" in key


def gpu_index_of(key: str) -> int | None:
    if not key.startswith("gpu"):
        return None
    try:
        return int(key.partition("_")[0][3:])
    except ValueError:
        return None


def gpu_field_of(key: str) -> str:
    return key.partition("_")[2]


# ==========================================================================
# Devices and capabilities
# ==========================================================================


@dataclass
class GpuDevice:
    index: int
    name: str
    vendor: str
    vram_total_mb: float | None = None
    sources: list[str] = field(default_factory=list)
    luids: list[str] = field(default_factory=list)
    temp_limit: float | None = None
    note: str = ""
    registry_key: str = ""
    uuid: str = ""
    #: PCI bus:device.function, when known - a stable way to tell identical
    #: cards apart in the UI and in reports.
    pci: str = ""
    device_key: str = ""
    bus: int | None = None
    #: How confident the LUID->card attribution is: exact | pci-order |
    #: approximate | unattributed | unknown.
    luid_confidence: str = "unknown"

    @property
    def full_telemetry(self) -> bool:
        return bool({"nvml", "nvidia-smi"} & set(self.sources))

    @property
    def has_temperature(self) -> bool:
        return bool(_TEMP_SOURCES & set(self.sources))

    @property
    def has_hotspot(self) -> bool:
        """True when a backend for this card publishes a hotspot temperature.

        ADL and amdgpu sysfs report one per card, so those are conclusive.
        LibreHardwareMonitor publishes an AMD hotspot too, but it is added to
        every card's source list when it is running - including NVIDIA ones,
        where its tree has no hotspot for the card. Counting a bare "lhm" as
        proof was claiming a sensor the machine then reported as missing.
        """
        if {"adl", "sysfs"} & set(self.sources):
            return True
        return "lhm" in self.sources and self.vendor == "amd"

    @property
    def display_name(self) -> str:
        return f"{self.name} ({self.pci})" if self.pci else self.name

    @property
    def is_aggregate(self) -> bool:
        """True when one row covers several hardware functions."""
        return len(self.luids) > 1


#: Backends that publish at least one GPU temperature. This list is the whole
#: reason `has_temperature` exists, and it used to be only the NVIDIA trio: an
#: AMD card on Windows gets its 31 C from ADL and a Linux one from sysfs, so
#: both were labelled "no thermal sensor available" beside their own reading.
_TEMP_SOURCES = frozenset({"nvml", "nvidia-smi", "lhm", "adl", "sysfs",
                           "rocm-smi"})


def modernise_note(text: str) -> str:
    """Correct a note recorded by an older version before it is shown.

    Notes are written into the session file once and read for years afterwards, so
    anything actionable inside them goes stale - and a stale instruction is worse
    than no instruction. The one that matters is the CPU-temperature note from
    when gpumon read that through LibreHardwareMonitor: it told the reader to run
    a command and to start that program's web server from its menu, neither of
    which is how this works any more.

    The fact is kept and restated accurately for the run it belongs to; only the
    superseded advice goes. The stored record is untouched - this is a rendering
    step, so the session file stays exactly as it was written.
    """
    for marker in ("CPU package temperature needs a kernel driver",
                   "is only reachable through a kernel driver"):
        if marker in text:
            return ("CPU package temperature needs a kernel driver on Windows, "
                    "so it takes a single elevated setup - recorded as "
                    "unavailable for this run.")
    return text


@dataclass
class Capabilities:
    gpus: list[GpuDevice] = field(default_factory=list)
    cpu_temp: bool = False
    backend_errors: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# ==========================================================================
# Subprocess helper (no console flashing)
# ==========================================================================


def _run(cmd: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           text=True, errors="replace", **kwargs)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)


# ==========================================================================
# Availability caching
# ==========================================================================

# `available()` on the probing backends is expensive (it spawns nvidia-smi or
# enumerates wildcard counters). Cache a positive result for a while, but retry
# a negative one sooner so a backend that comes back can recover.
_AVAIL_OK_TTL = 60.0
_AVAIL_FAIL_TTL = 10.0
_avail_cache: dict[str, tuple[bool, float]] = {}


def cached_available(obj: Any) -> bool:
    key = getattr(obj, "name", type(obj).__name__)
    hit = _avail_cache.get(key)
    now = time.monotonic()
    if hit is not None:
        ok, when = hit
        ttl = _AVAIL_OK_TTL if ok else _AVAIL_FAIL_TTL
        if now - when < ttl:
            return ok
    ok = bool(obj.available())
    _avail_cache[key] = (ok, now)
    return ok


def invalidate_availability(name: str | None = None) -> None:
    """Drop the cached answer, so the next check really probes.

    Used after the user starts LibreHardwareMonitor from inside the app: a
    negative result is cached for ten seconds, and a sensor that came up two
    seconds ago would otherwise stay invisible for the rest of that window.
    """
    if name is None:
        _avail_cache.clear()
    else:
        _avail_cache.pop(name, None)


# ==========================================================================
# PDH: cross-vendor GPU utilisation and VRAM
# ==========================================================================

PDH_MORE_DATA = 0x800007D2
PDH_STATUS_OK = 0x00000000
PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000

_INST_BEGIN = "GPU Engine("
_MEM_INST_BEGIN = "GPU Adapter Memory("

# Utilization contributions below this (in percent) are not worth keeping as a
# steady PDH counter: reading idle per-process counters is what makes the query
# expensive under CPU load.
PDH_KEEP_THRESHOLD = 0.5

# Engine types whose utilisation is summed into "GPU Utilization", matching how
# Task Manager reports a single figure per adapter. Engine names carry numeric
# suffixes on some drivers ("Compute 0", "High Priority 3D"), so match by prefix.
_GRAPHICS_ENGINES = ("3d", "compute", "high priority 3d", "high priority compute")


def _is_graphics_engine(engine: str) -> bool:
    e = engine.strip().lower()
    if e.endswith(tuple("0123456789")):
        e = e.rstrip("0123456789").strip()
    return e in _GRAPHICS_ENGINES


class _PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class PdhGpuBackend:
    """GPU utilisation + dedicated VRAM for every adapter Windows exposes
    performance counters for.

    A single card can publish several LUIDs (SR-IOV virtual functions, or
    per-engine partitions), so callers group LUIDs into one logical device.
    """

    name = "pdh"

    def __init__(self) -> None:
        self.pdh = ctypes.WinDLL("pdh.dll") if os.name == "nt" else None
        self.error = ""
        self.query = ctypes.c_void_p()
        self._query_open = False
        self._counters: dict[str, tuple[ctypes.c_void_p, str, str]] = {}
        self._mem_counters: dict[str, tuple[ctypes.c_void_p, str]] = {}
        self._counter_lock = threading.RLock()
        self._last_refresh = 0.0
        self._last_mem_refresh = 0.0
        self._busy = False
        self._primed = False
        self._refresh_count = 0
        # Samples to collect before pruning idle counters, so a counter is never
        # dropped on the strength of a single unlucky read.
        self._warmup_samples = 3
        self._low_streak: dict[str, int] = {}

        # Background poller state.
        self._poller: AsyncPoller | None = None
        self._refresh_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._cached_util: dict[str, float] = {}
        self._cached_vram: dict[str, float] = {}
        self._cache_time = 0.0
        self._cache_error = ""
        self._declare()

    # -- declarations ----------------------------------------------------
    def _declare(self) -> None:
        if not self.pdh:
            return
        p = self.pdh
        p.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t,
                                    ctypes.POINTER(ctypes.c_void_p)]
        p.PdhOpenQueryW.restype = wintypes.DWORD
        p.PdhCloseQuery.argtypes = [ctypes.c_void_p]
        p.PdhCloseQuery.restype = wintypes.DWORD
        p.PdhAddCounterW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_size_t,
                                     ctypes.POINTER(ctypes.c_void_p)]
        p.PdhAddCounterW.restype = wintypes.DWORD
        p.PdhRemoveCounter.argtypes = [ctypes.c_void_p]
        p.PdhRemoveCounter.restype = wintypes.DWORD
        p.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
        p.PdhCollectQueryData.restype = wintypes.DWORD
        p.PdhExpandWildCardPathW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                            wintypes.LPWSTR,
                                            ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
        p.PdhExpandWildCardPathW.restype = wintypes.DWORD
        p.PdhGetFormattedCounterValue.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                 ctypes.POINTER(wintypes.DWORD),
                                                 ctypes.c_void_p]
        p.PdhGetFormattedCounterValue.restype = wintypes.DWORD

    def _open_query(self) -> bool:
        if self._query_open:
            return True
        if not self.pdh:
            return False
        rc = self.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.query))
        self._query_open = (rc == PDH_STATUS_OK)
        if not self._query_open:
            self.error = f"PdhOpenQuery failed (0x{rc & 0xFFFFFFFF:08X})"
        return self._query_open

    def collect(self) -> None:
        if self._open_query():
            self.pdh.PdhCollectQueryData(self.query)

    # -- wildcard helpers ------------------------------------------------
    def expand(self, path: str) -> list[str]:
        if not self.pdh:
            return []
        size = wintypes.DWORD(0)
        rc = self.pdh.PdhExpandWildCardPathW(None, path, None, ctypes.byref(size), 0)
        if rc not in (PDH_STATUS_OK, PDH_MORE_DATA) or not size.value:
            return []
        buf = ctypes.create_unicode_buffer(size.value)
        rc = self.pdh.PdhExpandWildCardPathW(None, path, buf, ctypes.byref(size), 0)
        if rc != PDH_STATUS_OK:
            return []
        return [b for b in buf[:size.value].split("\0") if b]

    @staticmethod
    def _instance_of(path: str, marker: str) -> str:
        i = path.find(marker)
        if i < 0:
            return ""
        return path[i + len(marker):path.rfind(")")]

    @staticmethod
    def parse_engine_instance(inst: str) -> tuple[str, str] | None:
        """`pid_123_luid_0x0_0x1A2B_phys_0_eng_0_engtype_3D` -> (luid, engine)."""
        if "luid_" not in inst:
            return None
        tail = inst.split("luid_", 1)[1].split("_")
        if len(tail) < 2:
            return None
        luid = f"{tail[0]}_{tail[1]}".lower()
        engine = inst.rsplit("engtype_", 1)[-1].lower() if "engtype_" in inst else ""
        return luid, engine

    @staticmethod
    def parse_memory_instance(inst: str) -> str | None:
        """`luid_0x00000000_0x0001639F_phys_0` -> `0x00000000_0x0001639f`."""
        if "luid_" not in inst:
            return None
        tail = inst.split("luid_", 1)[1].split("_")
        if len(tail) < 2:
            return None
        return f"{tail[0]}_{tail[1]}".lower()

    # -- lifecycle -------------------------------------------------------
    def available(self) -> bool:
        if not self.pdh:
            self.error = "pdh.dll unavailable"
            return False
        if not self._open_query():
            return False
        luids = self.enumerate_luids()
        if not luids:
            self.error = "no GPU utilisation counters exposed"
            return False
        return True

    def enumerate_luids(self) -> list[str]:
        out: list[str] = []
        for path in self.expand(r"\GPU Engine(*)\Utilization Percentage"):
            parsed = self.parse_engine_instance(self._instance_of(path, _INST_BEGIN))
            if parsed and parsed[0] not in out:
                out.append(parsed[0])
        return out

    def fingerprint(self) -> dict[str, dict[str, set[str]]]:
        """{luid: {engine_type: {pid,...}}} - lets us tell NVIDIA from AMD.

        NVIDIA exposes LegacyOverlay/VR engines; AMD exposes numbering engine
        groups plus True Audio. Also used to pick the LUID that has live work.
        """
        fp: dict[str, dict[str, set[str]]] = {}
        for path in self.expand(r"\GPU Engine(*)\Utilization Percentage"):
            inst = self._instance_of(path, _INST_BEGIN)
            parsed = self.parse_engine_instance(inst)
            if not parsed:
                continue
            luid, engine = parsed
            pid = inst.split("_")[1] if inst.startswith("pid_") else "?"
            fp.setdefault(luid, {}).setdefault(engine, set()).add(pid)
        return fp

    def read_memory_totals(self) -> dict[str, float]:
        """Dedicated VRAM currently committed per LUID, MB."""
        out: dict[str, float] = {}
        if not self._open_query():
            return out
        paths = self.expand(r"\GPU Adapter Memory(*)\Dedicated Usage")
        handles: list[tuple[ctypes.c_void_p, str]] = []
        for p in paths:
            luid = self.parse_memory_instance(self._instance_of(p, _MEM_INST_BEGIN))
            if not luid:
                continue
            h = ctypes.c_void_p()
            if self.pdh.PdhAddCounterW(self.query, p, 0, ctypes.byref(h)) == PDH_STATUS_OK:
                handles.append((h, luid))
        if not handles:
            return out
        # Rate counters need two samples to report; the elapsed time between
        # them is unavoidable, so keep this to the minimum useful window.
        self.collect()
        time.sleep(0.05)
        self.collect()
        for h, luid in handles:
            v = self._read(h)
            if v is not None:
                out[luid] = max(out.get(luid, 0.0), v / (1024 * 1024))
            self.pdh.PdhRemoveCounter(h)
        return out

    def _read(self, handle: ctypes.c_void_p) -> float | None:
        val = _PDH_FMT_COUNTERVALUE()
        rc = self.pdh.PdhGetFormattedCounterValue(
            handle, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, None, ctypes.byref(val))
        if rc != PDH_STATUS_OK or val.CStatus != PDH_STATUS_OK:
            return None
        return float(val.doubleValue)

    # -- async polling ---------------------------------------------------
    #
    # Measured on this machine (8 threads busy on a 20-thread CPU):
    #     idle            : 1 counter 19 ms, 298 counters 15 ms  (flat)
    #     under CPU load  : 1 counter 128 ms, 298 counters 11.5 s
    # So PDH cost is dominated by *system contention*, not counter count, and a
    # CPU-saturating benchmark (Cinebench, a solver, a build) makes a synchronous
    # PDH read take seconds. The sampler thread must never block on that, so PDH
    # runs on its own thread and the sampler reads this cache. Stale utilization
    # for a few seconds is a far better failure mode than a stalled sampler.
    POLL_INTERVAL = 1.0
    POLL_INTERVAL_BUSY = 0.5
    STALE_AFTER = 12.0

    def start_poller(self) -> None:
        """Begin background PDH sampling. Idempotent."""
        if self._poller is None:
            self._poller = AsyncPoller("pdh-poller", self._sample_once_flat,
                                       self.POLL_INTERVAL,
                                       error_fn=lambda: self.error)
        self._poller.prime_once()
        self._poller.start()

    def stop_poller(self) -> None:
        if self._poller is not None:
            self._poller.stop()
            self._poller = None

    def _sample_once_flat(self) -> dict[str, float]:
        """Wrap the two result maps into one dict for AsyncPoller."""
        util, vram = self._sample_once()
        flat = {f"util:{k}": v for k, v in util.items()}
        flat.update({f"vram:{k}": v for k, v in vram.items()})
        return flat

    def _collect_stamped(self) -> float:
        """Collect query data, returning our own monotonic timestamp.

        We timestamp locally rather than trusting the PDH counter's own clock so
        that staleness is measured on one consistent clock.
        """
        self.collect()
        return time.monotonic()

    def _sample_once(self) -> tuple[dict[str, float], dict[str, float]]:
        """One full PDH cycle: collect, read, prune, aggregate."""
        self._collect_stamped()
        util: dict[str, float] = {}
        with self._counter_lock:
            items = list(self._counters.items())
        # Read outside the lock so the refresher can still add counters; the
        # steady dict is only mutated by the refresher under _counter_lock.
        per_path: dict[str, float] = {}
        for path, (h, luid, engine) in items:
            v = self._read(h)
            if v is None:
                continue
            per_path[path] = v
            util[luid] = util.get(luid, 0.0) + v

        # Keep the steady set to counters that actually carry load. Re-reading a
        # few hundred idle per-process counters is what makes PDH expensive.
        self._prune_steady(per_path, items)
        self._refresh_counters()
        self.refresh_memory_counters_if_due()

        vram: dict[str, float] = {}
        with self._counter_lock:
            mem_items = list(self._mem_counters.items())
        for _path, (h, luid) in mem_items:
            v = self._read(h)
            if v is not None:
                vram[luid] = max(vram.get(luid, 0.0), v / (1024 * 1024))
        self._busy = any(v > 1.0 for v in util.values())
        return ({k: min(100.0, v) for k, v in util.items()}, vram)

    def _prune_steady(self, per_path: dict[str, float],
                      items: list[tuple[str, tuple]]) -> None:
        """Drop laggard counters, keeping at least one per LUID+engine group.

        Reading idle per-process counters is what makes PDH expensive under load
        (15 ms -> 11 s), so laggards get dropped. But an idle adapter reports 0
        for *every* instance, so pruning on value alone would remove the whole
        group and make that GPU disappear from the log. One sentinel counter per
        group therefore always survives.
        """
        if not items:
            return
        by_group: dict[tuple[str, str], list[str]] = {}
        for path, (_h, luid, engine) in items:
            by_group.setdefault((luid, engine), []).append(path)

        heavy = self._heavy_paths(per_path)
        # Elect a sentinel per group: the busiest instance, so a GPU that is
        # actually working keeps its dominant counter even if nothing is "heavy"
        # by the absolute threshold yet.
        sentinels: set[str] = set()
        for paths in by_group.values():
            best = max(paths, key=lambda p: per_path.get(p, 0.0))
            sentinels.add(best)

        with self._counter_lock:
            for path, (_h, _luid, _eng) in items:
                if path in heavy or path in sentinels:
                    self._low_streak.pop(path, None)
                    continue
                if self._refresh_count < self._warmup_samples:
                    self._low_streak[path] = 0
                    continue
                streak = self._low_streak.get(path, 0) + 1
                self._low_streak[path] = streak
                if streak >= 2:
                    self._low_streak.pop(path, None)
                    pinned, _, _ = self._counters.pop(path)
                    try:
                        self.pdh.PdhRemoveCounter(pinned)
                    except OSError:
                        pass
        self._refresh_count += 1

    @staticmethod
    def _heavy_paths(per_path: dict[str, float]) -> set[str]:
        """Top contributors plus anything non-trivial.

        GPU utilization is the sum over processes, so we keep the top few by
        value, then also keep every path above a floor to bound the count.
        """
        if not per_path:
            return set()
        ranked = sorted(per_path.items(), key=lambda kv: -kv[1])
        keep = {p for p, v in ranked[:3] if v > 0.0}
        keep |= {p for p, v in ranked if v >= PDH_KEEP_THRESHOLD}
        return keep

    def _refresh_counters(self, force: bool = False) -> None:
        """Add counters for graphics-engine instances not yet tracked."""
        wanted: dict[str, tuple[str, str]] = {}
        for path in self.expand(r"\GPU Engine(*)\Utilization Percentage"):
            parsed = self.parse_engine_instance(self._instance_of(path, _INST_BEGIN))
            if not parsed:
                continue
            luid, engine = parsed
            if not _is_graphics_engine(engine):
                continue
            wanted[path] = (luid, engine)

        with self._counter_lock:
            # Only drop instances that vanished; laggards are handled by pruning
            # so that a burst of activity is not missed by removing and re-adding.
            for path in list(self._counters):
                if path not in wanted:
                    h, _, _ = self._counters.pop(path)
                    self._low_streak.pop(path, None)
                    try:
                        self.pdh.PdhRemoveCounter(h)
                    except OSError:
                        pass
            added = 0
            for path, (luid, engine) in wanted.items():
                if path in self._counters:
                    continue
                h = ctypes.c_void_p()
                if self.pdh.PdhAddCounterW(self.query, path, 0,
                                           ctypes.byref(h)) == PDH_STATUS_OK:
                    self._counters[path] = (h, luid, engine)
                    added += 1
            if not self._counters and wanted:
                self.error = "no GPU engine counters could be added"
        self._last_refresh = time.monotonic()
        return None

    def refresh_memory_counters_if_due(self) -> None:
        if time.monotonic() - self._last_mem_refresh < 60.0:
            return
        self.refresh_memory_counters()

    def refresh_memory_counters(self) -> None:
        wanted: dict[str, tuple[str, str]] = {}
        for p in self.expand(r"\GPU Adapter Memory(*)\Dedicated Usage"):
            luid = self.parse_memory_instance(self._instance_of(p, _MEM_INST_BEGIN))
            if luid:
                wanted[p] = (luid, "")
        with self._counter_lock:
            for path in list(self._mem_counters):
                if path not in wanted:
                    h, _ = self._mem_counters.pop(path)
                    try:
                        self.pdh.PdhRemoveCounter(h)
                    except OSError:
                        pass
            for path, (luid, _e) in wanted.items():
                if path in self._mem_counters:
                    continue
                h = ctypes.c_void_p()
                if self.pdh.PdhAddCounterW(self.query, path, 0,
                                           ctypes.byref(h)) == PDH_STATUS_OK:
                    self._mem_counters[path] = (h, luid)
        self._last_mem_refresh = time.monotonic()

    def prime(self) -> None:
        if not self._open_query():
            return
        self._refresh_counters(force=True)
        self.refresh_memory_counters()
        self.collect()
        time.sleep(0.1)
        self.collect()
        self._primed = True
        # Prime the cache once synchronously so the first samples are populated,
        # then hand further polling to the background thread.
        self.start_poller()

    def cache_age(self) -> float:
        """Seconds since the last background PDH sample (inf if never)."""
        return self._poller.age() if self._poller is not None else float("inf")

    def poll(self) -> tuple[dict[str, float], dict[str, float]]:
        """Return the last background sample. Never blocks on PDH.

        A stale cache is returned as-is; callers can inspect `cache_age()` to
        decide whether to mark the figure as approximate.
        """
        if self._poller is None:
            return {}, {}
        flat = self._poller.snapshot()
        util = {k[5:]: v for k, v in flat.items() if k.startswith("util:")}
        vram = {k[5:]: v for k, v in flat.items() if k.startswith("vram:")}
        return util, vram

    def close(self) -> None:
        if not self.pdh:
            return
        self.stop_poller()
        with self._counter_lock:
            for h, _, _ in self._counters.values():
                try:
                    self.pdh.PdhRemoveCounter(h)
                except OSError:
                    pass
            for h, _ in self._mem_counters.values():
                try:
                    self.pdh.PdhRemoveCounter(h)
                except OSError:
                    pass
            self._counters.clear()
            self._mem_counters.clear()
        if self._query_open:
            try:
                self.pdh.PdhCloseQuery(self.query)
            except OSError:
                pass
            self._query_open = False


# ==========================================================================
# NVIDIA via nvidia-smi
# ==========================================================================


class NvidiaSmiBackend:
    name = "nvidia-smi"

    QUERY = [
        "index", "name", "temperature.gpu", "temperature.gpu.tlimit",
        "utilization.gpu", "utilization.memory", "memory.used", "memory.total",
        "clocks.current.graphics", "clocks.current.memory", "clocks.current.sm",
        "power.draw", "power.limit", "fan.speed",
    ]
    # Fields some GPUs/drivers do not implement.
    OPTIONAL = {
        "temperature.gpu.tlimit", "utilization.memory", "clocks.current.graphics",
        "clocks.current.sm", "fan.speed", "power.limit",
    }
    FIELD_MAP = {
        "temperature.gpu": "temp",
        "temperature.gpu.tlimit": "temp_limit",
        "utilization.gpu": "util",
        "utilization.memory": "mem_util",
        "memory.used": "vram_used",
        "memory.total": "vram_total",
        "clocks.current.graphics": "clock_core",
        "clocks.current.sm": "clock_sm",
        "clocks.current.memory": "clock_mem",
        "power.draw": "power",
        "power.limit": "power_limit",
        "fan.speed": "fan",
    }
    NULL_TOKENS = {"", "[N/A]", "N/A", "[Not Supported]", "[Unknown Error]", "[Insufficient Permissions]"}

    def __init__(self) -> None:
        self.exe = shutil.which("nvidia-smi")
        self.devices: list[GpuDevice] = []
        self.fields: list[str] = []
        self.error = ""
        self.consecutive_failures = 0

    def available(self) -> bool:
        if not self.exe:
            self.error = "nvidia-smi not found on PATH"
            return False
        rc, out, err = _run([self.exe, "--query-gpu=index,name,memory.total",
                             "--format=csv,noheader,nounits"], timeout=8)
        if rc != 0:
            self.error = (err or "nvidia-smi failed").strip().splitlines()[0] \
                if (err or "").strip() else "nvidia-smi failed"
            self.devices = []
            return False
        devices: list[GpuDevice] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [x.strip() for x in line.split(",")]
            if len(parts) < 2:
                continue
            try:
                idx = int(parts[0])
            except ValueError:
                continue
            total = None
            if len(parts) > 2:
                try:
                    total = float(parts[2])
                except ValueError:
                    total = None
            devices.append(GpuDevice(
                index=idx, name=parts[1], vendor="nvidia", vram_total_mb=total,
                sources=["nvidia-smi"], note="full telemetry"))
        self.devices = devices
        self._detect_fields()
        self.error = ""
        return bool(devices)

    def _detect_fields(self) -> None:
        rc, _, _ = _run([self.exe, "--query-gpu=" + ",".join(self.QUERY),
                         "--format=csv,noheader,nounits"], timeout=8)
        self.fields = list(self.QUERY) if rc == 0 else \
            [f for f in self.QUERY if f not in self.OPTIONAL]

    def poll(self) -> dict[str, float]:
        if not self.exe or not self.fields:
            return {}
        rc, out, _ = _run([self.exe, "--query-gpu=" + ",".join(self.fields),
                           "--format=csv,noheader,nounits"], timeout=5)
        if rc != 0:
            self.consecutive_failures += 1
            self.error = "nvidia-smi query failed"
            return {}
        self.consecutive_failures = 0
        self.error = ""
        result: dict[str, float] = {}
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [x.strip() for x in line.split(",")]
            if len(parts) < len(self.fields):
                parts += ["[N/A]"] * (len(self.fields) - len(parts))
            row = dict(zip(self.fields, parts))
            try:
                idx = int(float(row.get("index", "0")))
            except ValueError:
                continue
            for src, suffix in self.FIELD_MAP.items():
                if src not in self.fields:
                    continue
                raw = row.get(src, "")
                if raw in self.NULL_TOKENS:
                    continue
                try:
                    result[f"gpu{idx}_{suffix}"] = float(raw)
                except ValueError:
                    continue
        return result

    def close(self) -> None:
        pass


# ==========================================================================
# System CPU / RAM via psutil
# ==========================================================================


class SystemBackend:
    name = "psutil"

    def __init__(self, per_core: bool = False) -> None:
        self.per_core = per_core
        try:
            import psutil  # noqa: PLC0415
            self.psutil = psutil
        except ImportError:
            self.psutil = None
        self.error = "" if self.psutil else "psutil not installed"

    def available(self) -> bool:
        return self.psutil is not None

    def poll(self) -> dict[str, float]:
        if not self.psutil:
            return {}
        ps = self.psutil
        out: dict[str, float] = {}
        try:
            out["cpu_util"] = float(ps.cpu_percent(interval=None))
        except OSError:
            pass
        try:
            freq = ps.cpu_freq()
            if freq and freq.current:
                out["cpu_clock"] = float(freq.current)
        except (AttributeError, NotImplementedError, OSError):
            pass
        try:
            vm = ps.virtual_memory()
            out["ram_used"] = vm.used / (1024 * 1024)
            out["ram_total"] = vm.total / (1024 * 1024)
            out["ram_percent"] = float(vm.percent)
        except OSError:
            pass
        try:
            out["pagefile_used"] = ps.swap_memory().used / (1024 * 1024)
        except OSError:
            pass
        if self.per_core:
            try:
                for i, pct in enumerate(ps.cpu_percent(percpu=True, interval=None)):
                    out[f"core{i:02d}_util"] = float(pct)
            except OSError:
                pass
        return out

    def close(self) -> None:
        pass


# ==========================================================================
# LibreHardwareMonitor over WMI
# ==========================================================================


class AcpiThermalZoneBackend:
    """Last-resort CPU temperature from the firmware's own thermal zones.

    Windows exposes `MSAcpi_ThermalZoneTemperature` through WMI, and on many
    laptops the CPU's zone is one of them - readable with no driver and no
    elevation at all. On desktops it is often the board or VRM instead, and on
    some firmware (this machine's included) the class is not implemented and
    returns WBEM_E_NOT_SUPPORTED.

    So it is a *fallback*, used only when nothing better produced a CPU
    temperature, and it says what it is: a motherboard reading, not the CPU
    package sensor. Presenting it as the package sensor would be a lie the
    numbers could not back up.
    """

    name = "acpi"
    #: Plausible idle-to-hot range; anything outside it is a different sensor.
    FLOOR = 15.0
    CEILING = 115.0

    def __init__(self) -> None:
        self.error = ""
        self._checked = False
        self._zones = 0

    def available(self) -> bool:
        if self._checked:
            return self._zones > 0
        self._checked = True
        if os.name != "nt":
            self.error = "Windows only (Linux reads hwmon instead)"
            return False
        reading = self.poll()
        if not reading:
            self.error = self.error or "this firmware publishes no ACPI thermal zone"
        return self._zones > 0

    def poll(self) -> dict[str, float]:
        if os.name != "nt":
            # Say why, here as well as in available(): an unavailable backend that
            # answers with a silent empty dict cannot be told apart from one that
            # looked and found nothing, which is what a Linux run of the suite
            # reported.
            self.error = "Windows only (Linux reads hwmon instead)"
            return {}
        try:
            import wmi  # noqa: PLC0415
        except ImportError:
            self.error = "python 'wmi' package not installed (pip install wmi)"
            return {}
        try:
            conn = wmi.WMI(namespace="root/WMI")
            zones = conn.MSAcpi_ThermalZoneTemperature()
        except Exception as exc:  # noqa: BLE001 - COM raises many types
            text = str(exc)
            self.error = ("this firmware publishes no ACPI thermal zone"
                          if "8004100c" in text or "not supported" in text.lower()
                          else text.splitlines()[0][:120])
            return {}
        best: float | None = None
        for zone in zones:
            try:
                # Tenths of a kelvin, per the ACPI spec.
                celsius = float(zone.CurrentTemperature) / 10.0 - 273.15
            except (TypeError, ValueError):
                continue
            self._zones += 1
            if self.FLOOR <= celsius <= self.CEILING:
                best = celsius if best is None else max(best, celsius)
        self.error = ""
        return {"cpu_temp": best} if best is not None else {}


class LibreHardwareMonitorBackend:
    """CPU and AMD GPU temperatures, fans and power from LibreHardwareMonitor's
    WMI provider. LHM must run elevated with Options -> WMI Provider enabled;
    this is the only way to read AMD sensors on Windows.

    Identifier shapes:
      /intelcpu/0/temperature/0     CPU package
      /amdcpu/0/temperature/0
      /gpu-nvidia/0/temperature/0   GPU core
      /amdgpu/0/temperature/0       AMD edge
      /amdgpu/0/temperature/1       hotspot
      /amdgpu/0/temperature/2       memory
    """

    name = "lhm"
    NAMESPACE = "root/LibreHardwareMonitor"
    #: LibreHardwareMonitor's own web server. 0.9.6 removed the WMI provider
    #: altogether - its binaries contain no WMI code, so `root/LibreHardwareMonitor`
    #: never appears however the settings file is written - and this is the
    #: interface that replaced it.
    URL = "http://127.0.0.1:8085/data.json"
    HTTP_TIMEOUT = 2.5
    _NUMBER = re.compile(r"-?\d+(?:\.\d+)?")

    def __init__(self) -> None:
        self.conn = None
        self.error = ""
        self._probed = False
        self._gpu_map: dict[str, int] = {}
        self.consecutive_failures = 0
        #: "http" or "wmi": which interface this install answers on.
        self.source = ""

    def available(self) -> bool:
        if self._probed:
            return self.conn is not None or self.source == "http"
        self._probed = True
        if os.name != "nt":
            self.error = "Windows only"
            return False
        # The web server first. Reading it costs one local HTTP request, and it
        # is the only interface a current LibreHardwareMonitor has; WMI stays as
        # the fallback for installs old enough to still publish it.
        if self._http_readings() is not None:
            self.source = "http"
            self.error = ""
            return True
        try:
            import wmi  # noqa: PLC0415
        except ImportError:
            self.error = ("no sensor source: LibreHardwareMonitor is not "
                          "publishing (start it and use Web -> Run), and the "
                          "python 'wmi' package is not installed either")
            return False
        try:
            self.conn = wmi.WMI(namespace=self.NAMESPACE)
            _ = self.conn.Sensor()
        except Exception as exc:  # noqa: BLE001 - COM raises many error types
            self.error = self._shorten(exc)
            self.conn = None
            return False
        self.source = "wmi"
        self.error = ""
        return True

    @staticmethod
    def _shorten(exc: Exception) -> str:
        """Turn a COM error into something a user can act on."""
        text = str(exc)
        if "8004100e" in text or "Invalid namespace" in text:
            return ("LibreHardwareMonitor is not publishing sensors - start it, "
                    "or start its web server from the Web menu")
        if "80041003" in text or "Access denied" in text or "Access is denied" in text:
            return "WMI provider present but access denied (run LHM as admin)"
        if "class" in text.lower() and "invalid" in text.lower():
            return "WMI provider present but no Sensor class"
        return text.strip().splitlines()[0][:120] if text.strip() else "unavailable"

    # -- the HTTP interface ----------------------------------------------
    def _http_readings(self) -> list[tuple[str, str, str, float]] | None:
        """Every sensor the web server publishes, or None if it is not serving.

        The tree is nested (computer -> hardware -> sensor type -> sensor), and
        each sensor carries the same Identifier/SensorType/Value triple the WMI
        provider used, so the metric mapping below is shared between the two.
        """
        import json
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(self.URL,
                                        timeout=self.HTTP_TIMEOUT) as response:
                tree = json.loads(response.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
            return None
        out: list[tuple[str, str, str, float]] = []
        self._flatten(tree, out)
        return out

    def _flatten(self, node: dict, out: list[tuple[str, str, str, float]]) -> None:
        ident = (node.get("SensorId") or "").lower()
        raw = node.get("Value")
        if ident and raw not in (None, ""):
            match = self._NUMBER.search(str(raw))
            if match:
                out.append((ident, (node.get("Type") or "").lower(),
                            node.get("Text") or "", float(match.group())))
        for child in node.get("Children") or ():
            self._flatten(child, out)

    def set_gpu_map(self, mapping: dict[str, int]) -> None:
        self._gpu_map = {k.lower(): v for k, v in mapping.items()}

    def _index_for(self, ident: str) -> int | None:
        parts = ident.split("/")
        if len(parts) < 3:
            return None
        return self._gpu_map.get("/" + parts[1] + "/" + parts[2])

    @staticmethod
    def _cpu_temp_rank(ident: str, name: str) -> int:
        """How good a candidate for "the CPU temperature" a sensor is.

        LibreHardwareMonitor creates one sensor per core *before* the package
        sensor, so `/intelcpu/0/temperature/0` is the first core - gpumon read
        that index and labelled it the CPU temperature, which is a core reading
        and runs several degrees above the package. The package sensor is the one
        whose name says so, and it has to win whatever order the sensors arrive
        in, hence a rank rather than a first-match.
        """
        lowered = name.lower()
        if "package" in lowered:
            return 3
        if "core max" in lowered or "core average" in lowered:
            return 2
        if ident.endswith("/temperature/0"):
            return 1
        return 0

    def _metric_for(self, ident: str, stype: str, name: str) -> str | None:
        is_cpu = "/intelcpu/" in ident or "/amdcpu/" in ident
        is_gpu = any(k in ident for k in ("/gpu-nvidia/", "/gpu-amd/", "/amdgpu/",
                                          "/gpu-intel/"))
        if stype == "temperature":
            if is_cpu:
                return ("cpu_temp" if self._cpu_temp_rank(ident, name) else None)
            if is_gpu:
                idx = self._index_for(ident)
                if idx is None:
                    return None
                tail = ident.rsplit("/temperature/", 1)[-1]
                if tail == "0":
                    return f"gpu{idx}_temp"
                if tail == "1":
                    return f"gpu{idx}_hotspot"
                if tail in ("2", "3"):
                    return f"gpu{idx}_mem_temp"
        elif stype == "load":
            if is_gpu:
                idx = self._index_for(ident)
                return f"gpu{idx}_util" if idx is not None else None
        elif stype == "power":
            if is_cpu:
                return "cpu_power"
            if is_gpu:
                idx = self._index_for(ident)
                return f"gpu{idx}_power" if idx is not None else None
        elif stype == "clock":
            if is_cpu:
                return "cpu_clock"
        elif stype == "fan":
            if is_gpu:
                idx = self._index_for(ident)
                return f"gpu{idx}_fan" if idx is not None else None
        return None

    def poll(self) -> dict[str, float]:
        if self.source == "http":
            readings = self._http_readings()
            if readings is None:
                self.consecutive_failures += 1
                self.error = "LibreHardwareMonitor's web server stopped answering"
                if self.consecutive_failures > 5:
                    self.source = ""
                return {}
            self.consecutive_failures = 0
            return self._map_readings(readings)

        if not self.conn:
            return {}
        try:
            sensors = self.conn.Sensor()
        except Exception as exc:  # noqa: BLE001
            self.consecutive_failures += 1
            self.error = f"WMI read failed: {exc}"
            if self.consecutive_failures > 5:
                self.conn = None
            return {}
        self.consecutive_failures = 0
        readings = []
        for s in sensors:
            try:
                raw = getattr(s, "Value", None)
                if raw is None:
                    continue
                readings.append(((getattr(s, "Identifier", "") or "").lower(),
                                 (getattr(s, "SensorType", "") or "").lower(),
                                 getattr(s, "Name", "") or "",
                                 float(raw)))
            except (TypeError, ValueError, AttributeError):
                continue
        return self._map_readings(readings)

    def _map_readings(self, readings: list[tuple[str, str, str, float]]) \
            -> dict[str, float]:
        """Turn identifier/type/name/value rows into gpumon metric keys."""
        out: dict[str, float] = {}
        best_rank = 0
        for ident, stype, name, value in readings:
            key = self._metric_for(ident, stype, name)
            if not key:
                continue
            if key == "cpu_temp":
                # Several sensors qualify (a core, the package); the best one wins
                # regardless of the order they arrive in.
                rank = self._cpu_temp_rank(ident, name)
                if rank >= best_rank:
                    best_rank = rank
                    out[key] = value
                continue
            out[key] = value
        return out

    def close(self) -> None:
        self.conn = None


# ==========================================================================
# Registry-based adapter discovery
# ==========================================================================

_DISPLAY_CLASS = (r"SYSTEM\CurrentControlSet\Control\Class"
                  r"\{4d36e968-e325-11ce-bfc1-08002be10318}")


def _vendor_of(name: str) -> str:
    n = name.lower()
    if any(k in n for k in ("nvidia", "geforce", "quadro", "rtx", "tesla")):
        return "nvidia"
    if any(k in n for k in ("amd", "radeon", "firepro", "instinct")):
        return "amd"
    if any(k in n for k in ("intel", "iris", "uhd graphics", "arc ")):
        return "intel"
    return "unknown"


def _decode_str(value: Any) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-16-le", "ignore").rstrip("\x00")
        except (UnicodeDecodeError, LookupError):
            return value.decode("latin-1", "ignore").rstrip("\x00")
    return str(value)


@dataclass
class RawAdapter:
    registry_key: str
    name: str
    vendor: str
    vram_total_mb: float | None
    pnp_id: str = ""
    driver_version: str = ""


def enumerate_display_adapters() -> list[RawAdapter]:
    """Physical display adapters, deduplicated.

    A card appears several times in the registry (per-output entries and
    SR-IOV virtual functions), so dedup by (name, vendor, vram).
    """
    if os.name != "nt":
        return []
    import winreg  # noqa: PLC0415
    adapters: list[RawAdapter] = []
    seen: set[tuple[str, str, float | None]] = set()
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS) as root:
            keys: list[str] = []
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                    i += 1
                except OSError:
                    break
                if sub.isdigit():
                    keys.append(sub)
            for sub in sorted(keys):
                try:
                    with winreg.OpenKey(root, sub) as sk:
                        desc = _decode_str(winreg.QueryValueEx(sk, "DriverDesc")[0])
                        mem = None
                        pnp = ""
                        drv = ""
                        for value_name, target in (("HardwareInformation.qwMemorySize", "mem"),
                                                   ("MatchingDeviceId", "pnp"),
                                                   ("DriverVersion", "drv")):
                            try:
                                got = winreg.QueryValueEx(sk, value_name)[0]
                            except OSError:
                                continue
                            if target == "mem" and isinstance(got, int):
                                mem = got / (1024 * 1024)
                            elif target == "pnp":
                                pnp = _decode_str(got)
                            elif target == "drv":
                                drv = _decode_str(got)
                except OSError:
                    continue
                vendor = _vendor_of(desc)
                dedup = (desc.strip().lower(), vendor, mem)
                if dedup in seen:
                    continue
                seen.add(dedup)
                adapters.append(RawAdapter(
                    registry_key=sub, name=desc, vendor=vendor,
                    vram_total_mb=mem, pnp_id=pnp, driver_version=drv))
    except OSError:
        return []
    return adapters


# ==========================================================================
# Aggregating sensor manager
# ==========================================================================


class SensorManager:
    """Owns the backends, merges their samples, and reports what is available."""

    def __init__(self, per_core: bool = False) -> None:
        # Imported here rather than at module scope because platforms.windows
        # imports these backend classes back out of this module; keeping it
        # local means neither module depends on the other's import order.
        # Modules are cached, so this costs one dict lookup.
        from platforms import (PlatformBackends, build_platform_backends,
                               current_platform)  # noqa: PLC0415

        self.per_core = per_core
        #: "windows" or "linux", from platforms.current_platform().
        self.platform_name = current_platform()
        #: Every optional vendor backend this OS can offer (platforms/).
        self.platform_backends: PlatformBackends = \
            build_platform_backends(per_core)
        self.lock = threading.Lock()
        self.nvml = NvmlBackend()
        # The attribute names below are the ones the UI and --selftest have
        # always used. On Linux the Windows-only ones hold stubs that explain
        # why they are unavailable, so no caller needs a platform check.
        self.nvidia = self.platform_backends.nvidia_smi
        self.amd = self.platform_backends.adl
        self.system = self.platform_backends.system
        self.lhm = self.platform_backends.lhm
        #: gpumon's own CPU sensor path: an elevated helper reads the processor's
        #: registers through the PawnIO driver and publishes them to a file. It
        #: comes first because it is a direct reading with no third-party
        #: application in the way; LibreHardwareMonitor stays as a fallback for a
        #: machine where our helper has not been set up.
        self.msr = msrbackend.MsrBackend()
        #: The firmware's own thermal zone, used only if nothing better reports a
        #: CPU temperature. Cross-platform object; it declines on Linux, where
        #: hwmon already answers.
        self.acpi = AcpiThermalZoneBackend()
        self.pdh = self.platform_backends.pdh
        # Linux-only sources: poll() merges them in this order with setdefault,
        # so sysfs wins over rocm-smi. They are stubs (and unused) on Windows,
        # where the five backends above supply the same figures.
        self.amd_sysfs = self.platform_backends.amd_sysfs
        self.rocm = self.platform_backends.amd_rocm
        self.cpu_thermal = self.platform_backends.cpu_thermal
        self.intel = self.platform_backends.intel
        self.gpus: list[GpuDevice] = []
        self.notes: list[str] = []
        self.backend_errors: dict[str, str] = {}
        self.pdh_mem_totals: dict[str, float] = {}
        self.amd_luid_count = 0
        #: Utilisation per LUID, measured once during Windows discovery.
        self.luid_activity: dict[str, float] = {}
        #: PCI label -> GpuDevice, so ADL/LHM readings land on the right card.
        self._card_by_pci: dict[str, GpuDevice] = {}
        #: Fields measured by PDH this cycle, which outrank ADL's equivalents.
        self._pdh_measured: set[str] = set()
        #: Cards whose PDH VRAM counter reported an implausible figure.
        self._implausible_vram: set[int] = set()
        #: device_key -> LUIDs, from a previous --calibrate-gpus run.
        self.luid_calibration: dict[str, list[str]] = load_luid_calibration()
        self._probe()

    # -- discovery -------------------------------------------------------
    def _probe(self) -> None:
        """Build the logical GPU list.

        Discovery is deliberately vendor-centric rather than registry-centric,
        because this machine reports one physical AMD card as three registry
        adapter entries (two SR-IOV functions plus a duplicate), while PDH
        publishes two LUIDs for it.

        The discovery itself is per-OS: Windows walks the registry, the Setup
        API and the LUID performance counters, Linux reads NVML and
        /sys/class/drm. Both produce the same GpuDevice rows and the same metric
        keys, so everything downstream - sampler, UI, reports, store - is
        unchanged by which one ran.
        """
        self.notes.clear()
        self.backend_errors.clear()
        self._card_by_pci = {}
        if self.platform_name == "windows":
            self._probe_windows()
        else:
            self._probe_linux()
        self._build_notes()

    def _probe_windows(self) -> None:
        """Windows discovery: registry + Setup API + PDH LUID counters.

        Byte-for-byte the discovery this class did before the Linux port - it is
        kept as one block on purpose, because its exact outcome is pinned by the
        Windows test suite.
        """
        adapters = enumerate_display_adapters()
        reg_by_vendor: dict[str, RawAdapter] = {}
        for ad in adapters:                       # adapters are ordered by key
            reg_by_vendor.setdefault(ad.vendor, ad)

        # NVML is the preferred NVIDIA source: in-process, milliseconds, and it
        # cannot be starved by the very load we are measuring.
        nvidia_devices: list[GpuDevice] = []
        nvml_ok = self.nvml.available()
        if nvml_ok:
            for d in self.nvml.devices:
                nvidia_devices.append(GpuDevice(
                    index=d.index, name=d.name, vendor="nvidia",
                    sources=["nvml"], uuid=d.uuid, note="full telemetry (NVML)",
                    pci=d.pci, bus=d.bus))
        else:
            self.backend_errors["nvml"] = self.nvml.error
            # Fall back to nvidia-smi, accepting that it can stall under load.
            if self.nvidia.available():
                nvidia_devices = list(self.nvidia.devices)
                for d in nvidia_devices:
                    d.sources = ["nvidia-smi"]
                    d.note = "full telemetry (nvidia-smi fallback)"
            else:
                self.backend_errors["nvidia-smi"] = self.nvidia.error

        pdh_ok = self.pdh.available()
        if pdh_ok:
            fingerprint = self.pdh.fingerprint()
            mem_totals = self.pdh.read_memory_totals()
        else:
            self.backend_errors["pdh"] = self.pdh.error
            fingerprint, mem_totals = {}, {}

        buckets = classify_luids(fingerprint)
        self.luid_activity = self._measure_luid_activity() if pdh_ok else {}

        gpus: list[GpuDevice] = []
        next_index = 0

        # --- NVIDIA: one row per NVML device (exact, ordered) --------------
        # NVML reports utilisation, temperature and clocks but not the memory
        # total, so take that from the same registry value used for other cards.
        physical = enumerate_physical_gpus()
        for dev in nvidia_devices:
            total = dev.vram_total_mb
            if total is None:
                total = _registry_vram_for(physical, dev.name, "nvidia")
            gpus.append(GpuDevice(
                index=next_index, name=dev.name, vendor="nvidia",
                vram_total_mb=total, sources=list(dev.sources),
                note=dev.note, uuid=dev.uuid, bus=dev.bus, pci=dev.pci,
                luid_confidence="exact"))
            next_index += 1

        # Attach NVIDIA LUIDs. NVML's `pci.bus_id` lets us match exactly when the
        # counts line up; otherwise fall back to NVML's own device order, which
        # is the same order nvidia-smi and NVML enumerate in.
        nvidia_luids = order_luids_by_activity(buckets["nvidia"], self.luid_activity)
        nvidia_rows = [g for g in gpus if g.vendor == "nvidia"]
        if len(nvidia_rows) == len(nvidia_luids):
            for row, luid in zip(nvidia_rows, nvidia_luids):
                row.luids = [luid]
                row.luid_confidence = "exact"
        elif nvidia_rows:
            # One card, possibly several published functions.
            for row in nvidia_rows:
                row.luids = list(nvidia_luids)
            nvidia_rows[0].luid_confidence = "approximate"

        # --- Other vendors: one row per physical card from the Setup API ----
        # Cards whose vendor NVML already covers are skipped, so nothing is
        # listed twice.
        adl_ok = self.amd.available()
        if not adl_ok:
            self.backend_errors["adl"] = self.amd.error
        adl_by_pci = {a.pci_label: a for a in self.amd.adapters} if adl_ok else {}
        nvidia_covered = len(nvidia_rows)
        nvidia_seen = 0
        # Which LUIDs remain after NVIDIA's are claimed. LUIDs of one vendor can
        # be needed by several cards, so they are dealt out rather than assumed
        # to belong to a single device.
        unclaimed = order_luids_by_activity(
            buckets["amd"] + buckets["unknown"], self.luid_activity)
        # Work out which physical cards still need a LUID: every card except the
        # NVIDIA ones already represented by an NVML row.
        cards_needing_luids: list[PhysicalGpu] = []
        skipped_nvidia = 0
        for card in physical:
            if card.vendor == "nvidia" and skipped_nvidia < nvidia_covered:
                skipped_nvidia += 1
                continue
            cards_needing_luids.append(card)
        # Deal counters round-robin so each card gets a working one. A saved
        # calibration, if present, overrides this entirely.
        dealt = distribute_luids(len(cards_needing_luids), unclaimed)
        amd_luid_cursor = 0
        for card in physical:
            if card.vendor == "nvidia":
                nvidia_seen += 1
                if nvidia_seen <= nvidia_covered:
                    continue   # already represented by an NVML row
            total = card_vram_estimate(card, mem_totals)
            sources: list[str] = []
            luids: list[str] = []
            confidence = "unknown"
            note = ""
            card_position = cards_needing_luids.index(card) \
                if card in cards_needing_luids else -1
            if pdh_ok and card_position >= 0 and card_position < len(dealt):
                if self.luid_calibration.get(card.device_key):
                    luids = list(self.luid_calibration[card.device_key])
                    confidence = "calibrated"
                else:
                    luids = list(dealt[card_position])
                    confidence = ("exact" if len(unclaimed) == len(cards_needing_luids)
                                  else "pci-order")
                if luids:
                    sources.append("pdh")
            if sources:
                note = "utilisation + VRAM via PDH"
            else:
                note = "no telemetry backend available for this adapter"
            # ADL reports the PCI address of every AMD adapter, so match on it
            # instead of relying on enumeration order.
            if card.pci_label in adl_by_pci:
                sources.append("adl")
                note = "full AMD telemetry via ADL (temperature, fan, clocks, power)"
            gpus.append(GpuDevice(
                index=next_index, name=card.name, vendor=card.vendor,
                vram_total_mb=total, sources=sources, luids=luids, note=note,
                pci=card.pci_label, device_key=card.device_key, bus=card.bus,
                luid_confidence=confidence))
            next_index += 1

        # Any LUID not attributed to a card still gets monitored rather than
        # silently dropped, so utilisation is never lost.
        attributed = {l for g in gpus for l in g.luids}
        leftovers = [l for l in unclaimed if l not in attributed]
        if pdh_ok and leftovers:
            gpus.append(GpuDevice(
                index=next_index, name="Additional GPU function",
                vendor="unknown", sources=["pdh"], luids=leftovers,
                note=f"{len(leftovers)} unattributed hardware function(s) "
                     "reporting utilisation",
                luid_confidence="unattributed"))
            next_index += 1

        self.gpus = gpus
        self.pdh_mem_totals = mem_totals
        self._card_by_pci = {g.pci: g for g in gpus if g.pci}

        # --- LibreHardwareMonitor -----------------------------------------
        if self.lhm.available():
            self._map_lhm_gpus(gpus)
        else:
            self.backend_errors["lhm"] = self.lhm.error

        if not self.system.available():
            self.backend_errors["psutil"] = self.system.error

    def _probe_linux(self) -> None:
        """Linux discovery: NVML for NVIDIA, /sys/class/drm for everything else.

        There is no registry and no Setup API here, so identity comes from the
        card's own device directory: `/sys/class/drm/cardN/device` is the PCI
        function, and its `uevent` carries `PCI_SLOT_NAME` (0000:41:00.0). That
        address is what the UI and the reports use to tell identical cards
        apart, so it *is* the row's `pci`/`device_key`: unlike Windows, nothing
        here is inferred from enumeration order, which is why every row with a
        PCI address is marked "exact" rather than "pci-order".

        NVIDIA rows come first for the same reason they do on Windows: the
        nvidia-smi fallback numbers its devices from zero over NVIDIA alone, so
        numbering NVIDIA first is what lets that backend's `gpuN_*` keys line up
        with these rows too.
        """
        nvidia_devices: list[GpuDevice] = []
        if self.nvml.available():
            for d in self.nvml.devices:
                nvidia_devices.append(GpuDevice(
                    index=d.index, name=d.name, vendor="nvidia",
                    sources=["nvml"], uuid=d.uuid, note="full telemetry (NVML)",
                    pci=d.pci, bus=d.bus))
        else:
            self.backend_errors["nvml"] = self.nvml.error
            # Same fallback as Windows, including its caveat: each sample spawns
            # a process, so it can stall under the load we are measuring.
            if self.nvidia.available():
                nvidia_devices = list(self.nvidia.devices)
                for d in nvidia_devices:
                    d.sources = ["nvidia-smi"]
                    d.note = "full telemetry (nvidia-smi fallback)"
            else:
                self.backend_errors["nvidia-smi"] = self.nvidia.error

        # Why each of this platform's own sources is down (sysfs, rocm-smi,
        # /sys thermal). Windows-only stubs are skipped by `errors()` itself, so
        # nothing here claims PDH or ADL is "broken" on a machine that never had
        # them.
        self.backend_errors.update(self.platform_backends.errors())

        sysfs_ok = self.amd_sysfs.available()
        cards = list(self.amd_sysfs.cards)
        gpus: list[GpuDevice] = []
        next_index = 0

        # An NVIDIA card is covered by NVML whenever NVML is up: its sysfs
        # directory (nvidia-drm) exposes none of the amdgpu attributes anyway,
        # so listing it twice would only duplicate the row.
        nvidia_covered = bool(nvidia_devices)
        for dev in nvidia_devices:
            gpus.append(GpuDevice(
                index=next_index, name=dev.name, vendor="nvidia",
                vram_total_mb=dev.vram_total_mb, sources=list(dev.sources),
                note=dev.note, uuid=dev.uuid, bus=dev.bus, pci=dev.pci,
                luid_confidence="exact"))
            next_index += 1

        # NVML reports the VRAM total only from `poll()`, not during discovery,
        # so a card that also has a sysfs directory lends its total (bytes ->
        # MB) to the row: the same PCI address, so the match is exact.
        vram_by_pci = {card.pci: card.vram_total_mb for card in cards if card.pci}
        for row in gpus:
            if row.vram_total_mb is None and row.pci:
                row.vram_total_mb = vram_by_pci.get(row.pci.lower())

        bound: dict[str, int] = {}
        for card in cards:
            if card.vendor == "nvidia" and nvidia_covered:
                continue
            sources: list[str] = []
            if card.vendor == "amd" and sysfs_ok:
                sources.append("sysfs")
            if sources:
                note = ("AMD telemetry via sysfs (temperature, hotspot, memory "
                        "temperature, utilisation, VRAM, clocks, fan, power)")
            else:
                note = "no telemetry backend available for this adapter"
            gpus.append(GpuDevice(
                index=next_index, name=card.name, vendor=card.vendor,
                vram_total_mb=card.vram_total_mb, sources=sources,
                note=note, pci=card.pci_label, device_key=card.device_key,
                bus=card.bus, luid_confidence="exact" if card.pci else "unknown"))
            if sources:
                # Keyed by both namespaces: the PCI address is the identity, the
                # card id is what rocm-smi uses. See set_gpu_map() in
                # platforms/linux.py.
                bound[card.card] = next_index
                if card.pci:
                    bound[card.pci] = next_index
            next_index += 1
        self.amd_sysfs.set_gpu_map(bound)
        self.rocm.set_gpu_map(bound)

        if any(g.vendor == "intel" for g in gpus):
            # Only worth saying when the machine actually has one.
            self.backend_errors["intel"] = self.intel.error

        self.gpus = gpus
        self._card_by_pci = {g.pci: g for g in gpus if g.pci}

        if not self.system.available():
            self.backend_errors["psutil"] = self.system.error

    def _measure_luid_activity(self) -> dict[str, float]:
        """One utilisation sample per LUID, used to order LUIDs by importance."""
        try:
            self.pdh.collect()
            util, _vram = self.pdh.poll()
            time.sleep(0.25)
            util, _vram = self.pdh.poll()
            return util
        except Exception:  # noqa: BLE001
            return {}

    def _poll_amd(self) -> dict[str, float]:
        """Attach each ADL reading to the card with the matching PCI address."""
        out: dict[str, float] = {}
        readings = self.amd.poll()
        if not readings:
            return out
        # ADL is authoritative for VRAM on these cards, so any earlier
        # "implausible PDH reading" flag for a card ADL just measured is stale.
        for label in readings:
            card = self._card_by_pci.get(label)
            if card is not None and "vram_used" in readings[label]:
                self._implausible_vram.discard(card.index)
        # PMLog names mostly line up with gpumon's fields; these are the ones
        # that need mapping, and `power_board` is a fallback for `power`.
        rename = {"temp": "temp", "hotspot": "hotspot", "mem_temp": "mem_temp",
                  "fan": "fan", "fan_rpm": "fan_rpm", "util": "util",
                  "mem_util": "mem_util", "clock_core": "clock_core",
                  "clock_mem": "clock_mem", "clock_soc": "clock_soc",
                  "power": "power", "power_board": "power_board",
                  "voltage_core": "voltage_core", "voltage_mem": "voltage_mem",
                  "voltage_soc": "voltage_soc",
                  # ADL's dedicated-VRAM query is the only accurate source for
                  # these cards, so it overrides PDH rather than deferring to it.
                  "vram_used": "vram_used"}
        for label, fields in readings.items():
            card = self._card_by_pci.get(label)
            if card is None:
                continue
            i = card.index
            for src, dst in rename.items():
                value = fields.get(src)
                if value is None:
                    continue
                # PDH is the authority for utilisation; ADL's figure is only
                # used when PDH had nothing for this card.
                if dst in ("util", "mem_util") and \
                        f"gpu{i}_{dst}" in self._pdh_measured:
                    continue
                key = f"gpu{i}_{dst}"
                if dst == "power_board" and f"gpu{i}_power" in out:
                    continue
                out[key] = value
        return out

    def _map_lhm_gpus(self, gpus: list[GpuDevice]) -> None:
        """Map LibreHardwareMonitor's per-vendor GPU numbering onto our rows.

        LHM numbers GPUs per vendor (/gpu-nvidia/0, /amdgpu/1, ...), so the
        ordinal within each vendor is what has to line up. Our own rows are
        already in PCI order, and LHM enumerates in driver order, which matches
        on every machine checked so far.
        """
        mapping: dict[str, int] = {}
        for vendor, prefix in (("nvidia", "/gpu-nvidia"), ("amd", "/amdgpu"),
                               ("amd", "/gpu-amd"), ("intel", "/gpu-intel")):
            rows = [g for g in gpus if g.vendor == vendor]
            for n, row in enumerate(rows):
                mapping[f"{prefix}/{n}"] = row.index
        self.lhm.set_gpu_map(mapping)
        for g in gpus:
            if "lhm" not in g.sources:
                g.sources = list(g.sources) + ["lhm"]

    def _build_notes(self) -> None:
        """Assemble the user-facing notes for this machine.

        The platform supplies its own text: on Linux, platforms/linux.py
        explains that AMD telemetry comes from sysfs, that rocm-smi is optional
        and where CPU temperature is read from; on Windows it supplies nothing,
        because `_windows_notes` below is that text - word for word what this
        method emitted before the port, since its wording is pinned by the
        Windows test suite. The rest of the notes read the same on either OS.
        """
        notes: list[str] = []
        if self.platform_name == "windows":
            notes.extend(self._windows_notes())
        else:
            notes.extend(self.platform_backends.notes())
        notes.extend(self._shared_notes())
        self.notes = notes

    def _shared_notes(self) -> list[str]:
        """Notes about telemetry gaps that are worded identically per OS."""
        notes: list[str] = []
        if "nvml" in self.backend_errors and "nvidia-smi" in self.backend_errors:
            notes.append("No NVIDIA telemetry: NVML unavailable "
                         f"({self.backend_errors['nvml']}) and "
                         f"nvidia-smi unavailable ({self.backend_errors['nvidia-smi']}).")
        elif "nvml" in self.backend_errors:
            notes.append("NVML unavailable, falling back to nvidia-smi. "
                         "Sampling can stall under heavy CPU load because each "
                         "sample spawns a process.")
        if "pdh" in self.backend_errors:
            notes.append(f"GPU performance counters unavailable: "
                         f"{self.backend_errors['pdh']}")
        if self.platform_name == "linux":
            # Whatever is left (an Intel GPU with no telemetry source, a missing
            # psutil) gets named once with its reason. sysfs, rocm-smi and the
            # thermal backend were already explained in context above, and
            # nvidia-smi/NVML just above that.
            covered = {"nvml", "nvidia-smi", "sysfs", "rocm-smi", "thermal"}
            for label, reason in self.backend_errors.items():
                if label not in covered:
                    notes.append(f"{label} unavailable: {reason}")
            # A missing CPU temperature is explained per OS, because the reason
            # differs: Windows needs a kernel driver, where Linux needs a readable
            # sensor in sysfs and normally has one. This was written only for
            # Windows, so a Linux machine with no temperature said nothing about
            # it - which is what the first Linux run of the suite found.
            if not self.cpu_temp_source():
                notes.append(
                    "No CPU temperature: neither /sys/class/thermal nor "
                    "/sys/class/hwmon published a readable temperature on this "
                    "machine.")
        return notes

    def _windows_notes(self) -> list[str]:
        """The Windows-specific notes, word for word as before the Linux port."""
        notes: list[str] = []
        lhm_ok = self.lhm.available() and self.lhm.conn is not None

        amd_with_adl = [g for g in self.gpus if "adl" in g.sources]
        if amd_with_adl:
            names = ", ".join(g.pci or g.name for g in amd_with_adl)
            notes.append(
                f"AMD telemetry via ADL for {names} - temperature, hotspot, "
                "memory temperature, fan, clocks and power, with no kernel "
                "driver or elevation required.")

        if self._implausible_vram:
            idx = ", ".join(f"gpu{i}" for i in sorted(self._implausible_vram))
            notes.append(
                f"VRAM used is withheld for {idx}: the Windows 'GPU Adapter "
                "Memory' counter reports a memory budget rather than live usage "
                "on this driver (it claimed more than the card physically has), "
                "so showing it would read as a permanently full card.")

        source = self.cpu_temp_source()
        if source == "msr":
            # gpumon's own reader answered: nothing to explain, and saying
            # anything here would be noise on a machine that is working.
            pass
        elif source == "thermal":
            notes.append(
                "CPU temperature comes from the kernel's thermal zone "
                "(`/sys/class/thermal`), which is usually the package or the "
                "board sensor depending on the platform.")
        elif source == "acpi":
            notes.append(
                "CPU temperature comes from the firmware's ACPI thermal zone - "
                "a board or VRM reading on most desktops, not the CPU package "
                "sensor.")
        elif source == "lhm":
            notes.append(
                "CPU temperature was read through LibreHardwareMonitor rather "
                "than from the processor directly.")
        elif not source:
            # The detail is our own reader's, not the fallback's: telling someone
            # about LibreHardwareMonitor while explaining a missing CPU
            # temperature is how this whole detour started.
            detail = (self.msr.error
                      or "the sensor helper is not running")
            # A note is recorded into the session file and read long afterwards,
            # so it describes what was true and never tells anyone what to run:
            # the actionable version lives in the program, on the card beside the
            # missing number, where it cannot go stale in a database.
            notes.append(
                "CPU package temperature needs a kernel driver on Windows, so it "
                f"takes a single elevated setup - unavailable for this run "
                f"({detail}).")

        # Tell the user when identical cards force an inferred attribution.
        any_calibrated = any(g.luid_confidence == "calibrated" for g in self.gpus)
        inferred = [g for g in self.gpus
                    if g.luid_confidence in ("pci-order", "unattributed")]
        if inferred and any_calibrated:
            notes.append(
                f"{len(inferred)} card(s) use an inferred counter mapping; "
                "re-run `python gpumon.py --calibrate-gpus` while loading those "
                "cards to verify them.")
        elif inferred:
            names = ", ".join(g.display_name for g in inferred[:2])
            extra = "" if len(inferred) <= 2 else f" (+{len(inferred) - 2} more)"
            notes.append(
                f"Utilisation counters for {names}{extra} are matched by counter "
                "order, not verified: Windows publishes no way to ask which "
                "counter belongs to which card here. Each card has at least one "
                "counter. Run `python gpumon.py --calibrate-gpus` to confirm "
                "which is which by loading one card at a time.")
        for g in self.gpus:
            if g.luid_confidence == "unattributed":
                notes.append(
                    f"{len(g.luids)} GPU hardware function(s) could not be tied "
                    "to a physical card; they are monitored separately so their "
                    "utilisation is not lost.")
                break
        return notes

    # -- capabilities ----------------------------------------------------
    def backend_status(self) -> dict[str, bool]:
        """Which sources are currently usable, for a compact status display.

        The terminal view and the web GUI both show this; it is deliberately
        cheap so it can be called on every frame.
        """
        status = {
            "nvml": cached_available(self.nvml),
            "smi": cached_available(self.nvidia),
            "adl": self.amd.available(),
            "pdh": cached_available(self.pdh),
            "cpu": self.system.available(),
            "lhm": cached_available(self.lhm),
        }
        if self.platform_name == "linux":
            # Extra keys rather than replacements: the six above keep their
            # meaning on either OS (the Windows-only ones read False on Linux,
            # because their stubs say so), and the Windows map is unchanged.
            status["sysfs"] = cached_available(self.amd_sysfs)
            status["rocm"] = cached_available(self.rocm)
            status["thermal"] = cached_available(self.cpu_thermal)
        return status

    def capabilities(self) -> Capabilities:
        source = self.cpu_temp_source()
        # The notes are rebuilt here, not just once at construction. A sensor that
        # goes away while the program is running - the helper stops publishing, a
        # fallback disappears - used to leave the interface showing no CPU
        # temperature and saying nothing about why, because the notes still
        # described the machine as it was at startup. That is the state a user
        # spends most of their time in: the reading is there, then it is not.
        self._build_notes()
        return Capabilities(gpus=list(self.gpus), cpu_temp=bool(source),
                            backend_errors=dict(self.backend_errors),
                            notes=list(self.notes))

    def cpu_temp_source(self) -> str:
        """Which backend is producing a CPU temperature right now, if any.

        Asked as a question rather than assumed from the backend list, because
        the answer decides both what the UI says and whether the metric is worth
        recording. In order of preference: "msr" (gpumon's own helper reading the
        processor's registers), "lhm" (LibreHardwareMonitor, over HTTP or WMI),
        "thermal" (/sys on Linux) or "acpi" (the firmware's own zone, a last
        resort).
        """
        try:
            if "cpu_temp" in self.msr.poll():
                return "msr"
        except Exception:  # noqa: BLE001
            pass
        try:
            if "cpu_temp" in self.lhm.poll():
                return "lhm"
        except Exception:  # noqa: BLE001 - a broken backend is simply not the source
            pass
        if self.platform_name == "linux":
            if cached_available(self.cpu_thermal):
                try:
                    if "cpu_temp" in self.cpu_thermal.poll():
                        return "thermal"
                except Exception:  # noqa: BLE001
                    pass
            return ""
        try:
            if "cpu_temp" in self.acpi.poll():
                return "acpi"
        except Exception:  # noqa: BLE001
            pass
        return ""

    def metric_keys(self) -> list[str]:
        """Metric keys currently expected to produce data."""
        keys = ["cpu_util", "cpu_clock", "ram_used", "ram_percent"]
        if self.system.available():
            if self.capabilities().cpu_temp:
                keys.append("cpu_temp")
        for g in self.gpus:
            fields = ["util", "vram_used", "vram_total"]
            if g.full_telemetry:
                fields += ["temp", "clock_core", "clock_mem", "power", "fan"]
            if "adl" in g.sources:
                # AMD's driver reports all of this with no kernel driver, and it
                # is the source that actually answers on a machine without
                # LibreHardwareMonitor - which is why the list is built from what
                # the backend produces rather than from which sources are present:
                # the temperature keys used to disappear whenever LHM was down,
                # even though ADL was reporting temperatures all along.
                fields += ["temp", "hotspot", "mem_temp", "clock_core",
                           "clock_mem", "power", "fan"]
            if "lhm" in g.sources:
                fields.append("temp")
            if g.has_hotspot:
                # The same rule the capability uses, so the list of expected
                # metrics cannot promise a hotspot the card does not publish.
                fields.append("hotspot")
            if "sysfs" in g.sources:
                # Linux: everything the amdgpu sysfs ABI can offer. A board may
                # not expose all of it (temp2/temp3 and mem_busy_percent are the
                # common absences), but these are the fields to expect.
                fields += ["temp", "hotspot", "mem_temp", "mem_util",
                           "clock_core", "clock_mem", "power", "power_limit",
                           "fan"]
            keys += [f"gpu{g.index}_{f}" for f in fields]
        return list(dict.fromkeys(keys))

    # -- polling ---------------------------------------------------------
    def prime(self) -> None:
        # Both GPU backends run on their own threads: a driver call that costs
        # milliseconds idle takes seconds when every core is saturated, and the
        # sampler must not inherit that latency.
        if self.nvml.available():
            self.nvml.start_poller()
        if self.pdh.available():
            self.pdh.prime()

    def poll(self) -> dict[str, float]:
        merged: dict[str, float] = {}
        with self.lock:
            if cached_available(self.nvml):
                try:
                    merged.update(self.nvml.poll())
                except Exception as exc:  # noqa: BLE001
                    self.backend_errors["nvml"] = str(exc)
            elif cached_available(self.nvidia):
                try:
                    merged.update(self.nvidia.poll())
                except Exception as exc:  # noqa: BLE001
                    self.backend_errors["nvidia-smi"] = str(exc)
            try:
                merged.update(self.system.poll())
            except Exception as exc:  # noqa: BLE001
                self.backend_errors["psutil"] = str(exc)
            # Linux sources: sysfs (a file read) then rocm-smi (a subprocess),
            # merged first-wins, and the /sys CPU temperature. The list is empty
            # on Windows, where the backends above are the sources.
            for label, backend in self.platform_backends.sources:
                if not cached_available(backend):
                    continue
                try:
                    for key, value in backend.poll().items():
                        merged.setdefault(key, value)
                except Exception as exc:  # noqa: BLE001
                    self.backend_errors[label] = str(exc)
            # gpumon's own sensor helper first: a direct register read, with no
            # third-party application involved. Whatever it publishes wins over
            # the LibreHardwareMonitor fallback below.
            try:
                merged.update(self.msr.poll())
            except Exception as exc:  # noqa: BLE001
                self.backend_errors["msr"] = str(exc)
            if cached_available(self.lhm):
                try:
                    for key, value in self.lhm.poll().items():
                        merged.setdefault(key, value)
                except Exception as exc:  # noqa: BLE001
                    self.backend_errors["lhm"] = str(exc)
            # Last resort for the CPU temperature, and only when nothing else
            # produced one. See AcpiThermalZoneBackend for why it is never the
            # first choice and always labelled.
            if "cpu_temp" not in merged:
                try:
                    merged.update(self.acpi.poll())
                except Exception:  # noqa: BLE001
                    pass
            if self.amd.available():
                try:
                    merged.update(self._poll_amd())
                except Exception as exc:  # noqa: BLE001
                    self.backend_errors["adl"] = str(exc)
            if cached_available(self.pdh):
                try:
                    util, vram = self.pdh.poll()
                except Exception as exc:  # noqa: BLE001
                    self.backend_errors["pdh"] = str(exc)
                    util, vram = {}, {}
                measured: set[str] = set()
                for g in self.gpus:
                    if not g.luids:
                        continue
                    # A card can publish several LUIDs (SR-IOV functions). Take
                    # the busiest for utilisation and sum their VRAM.
                    utils = [util[l] for l in g.luids if l in util]
                    used = [vram[l] for l in g.luids if l in vram]
                    if utils and not g.full_telemetry:
                        merged[f"gpu{g.index}_util"] = min(100.0, max(utils))
                        measured.add(f"gpu{g.index}_util")
                    if used:
                        # Windows' "Dedicated Usage" counter is unreliable for
                        # these AMD cards: it double-counts one V620 (summing two
                        # LUIDs that describe the same memory) and reports 0 for
                        # the other. ADL supplies the accurate figure, and it
                        # runs first, so PDH only fills in what ADL left blank.
                        summed = sum(used)
                        total = g.vram_total_mb
                        plausible = not total or summed <= total * 1.02
                        if not plausible:
                            # Only a problem if nothing else measured it: ADL's
                            # figure takes precedence and is accurate.
                            if f"gpu{g.index}_vram_used" not in merged:
                                self._implausible_vram.add(g.index)
                        elif f"gpu{g.index}_vram_used" not in merged:
                            merged[f"gpu{g.index}_vram_used"] = summed
                            measured.add(f"gpu{g.index}_vram_used")
                    if g.vram_total_mb:
                        merged.setdefault(f"gpu{g.index}_vram_total", g.vram_total_mb)
                self._pdh_measured = measured

        # Derived metrics.
        for g in self.gpus:
            i = g.index
            used = merged.get(f"gpu{i}_vram_used")
            total = merged.get(f"gpu{i}_vram_total")
            if used is not None and total and used <= total * 1.02:
                merged[f"gpu{i}_vram_percent"] = min(100.0, used / total * 100.0)
            power = merged.get(f"gpu{i}_power")
            limit = merged.get(f"gpu{i}_power_limit")
            if power is not None and limit:
                merged[f"gpu{i}_power_percent"] = min(100.0, power / limit * 100.0)
            temp = merged.get(f"gpu{i}_temp")
            limit_t = merged.get(f"gpu{i}_temp_limit")
            hot = merged.get(f"gpu{i}_hotspot")
            if limit_t is not None and temp is not None:
                merged[f"gpu{i}_throttle"] = 1.0 if temp >= limit_t - 2 else 0.0
            elif temp is not None and hot is not None:
                merged[f"gpu{i}_throttle"] = 1.0 if hot >= 110 else 0.0
        return merged

    def close(self) -> None:
        backends = [self.nvml, self.nvidia, self.amd, self.system, self.lhm,
                    self.pdh]
        # The Linux sources hold no OS handles today, but rocm-smi's backend -
        # and anything added later - is released through the same path. Empty on
        # Windows, so the six backends above close exactly as they always did.
        backends += [backend for _label, backend
                     in self.platform_backends.sources]
        for bk in backends:
            try:
                bk.close()
            except Exception:  # noqa: BLE001
                pass


def load_luid_calibration() -> dict[str, list[str]]:
    """Read the saved LUID-to-card mapping, if a calibration has been run."""
    path = _calibration_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, value in raw.items():
        if isinstance(value, list):
            out[str(key)] = [str(v).lower() for v in value]
    return out


def save_luid_calibration(mapping: dict[str, list[str]]) -> str:
    path = _calibration_path()
    payload = {k: sorted(v) for k, v in mapping.items()}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    return path


def _calibration_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "luid_calibration.json")


def gpu_metric_keys(gpus: list[GpuDevice]) -> list[str]:
    """Every metric key a given GPU list can produce, for config and validation."""
    keys: list[str] = []
    for g in gpus:
        fields = ["util", "vram_used", "vram_total", "vram_percent"]
        if g.full_telemetry:
            fields += ["temp", "hotspot", "mem_temp", "clock_core", "clock_sm",
                       "clock_mem", "power", "power_limit", "power_percent",
                       "fan", "mem_util"]
        if "lhm" in g.sources:
            fields += ["temp", "hotspot", "mem_temp"]
        if "sysfs" in g.sources:
            # Linux: the amdgpu sysfs vocabulary (see platforms/linux.py).
            fields += ["temp", "hotspot", "mem_temp", "mem_util", "clock_core",
                       "clock_mem", "power", "power_limit", "fan"]
        keys += [f"gpu{g.index}_{f}" for f in dict.fromkeys(fields)]
    return keys

