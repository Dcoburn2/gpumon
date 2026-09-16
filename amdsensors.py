"""AMD GPU sensors through ADL, ported from LibreHardwareMonitor.

Why this module exists
----------------------
An earlier attempt concluded AMD's driver API was unusable here because every
call returned -5. That was wrong twice over, and both mistakes came from guessing
at the interface instead of reading it:

1. **-5 is `ADL_ERR_INVALID_ADL_IDX`**, not "not supported". The adapter indices
   being passed were wrong, which made a perfectly working API look dead.
2. **The calling convention is Cdecl**, while ctypes defaults to stdcall on
   Windows. That is why `ADL2_Main_Control_Create` faulted instead of returning a
   context.
3. The PMLog sensor ids were guessed from a documentation enum; the real ones put
   `ADL_PMLOG_TEMPERATURE_EDGE` at **8**, not 19. Reading the wrong slots yielded
   plausible-looking noise.

The fix is to port LibreHardwareMonitor's own ADL interop rather than
reconstruct it: struct layouts, enum values, the Cdecl convention, and the
`supported` flag on each PMLog sensor. LHM is MPL-2.0; the layouts below are
interface definitions from AMD's public ADL SDK, reproduced so the numbers match
what the driver actually returns.

This gives AMD GPU temperature, hotspot, fan, clocks and power with no kernel
driver, no elevation and no external process.
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from dataclasses import dataclass, field

# `ctypes.wintypes` used to be imported here although nothing in this module
# uses it. It is a Windows-only module - it defines VARIANT_BOOL with ctypes'
# "v" type code, which does not exist elsewhere - so importing it made this file
# impossible to import on Linux, where the platform layer now needs it to load
# cleanly. Every ADL type below is built from plain ctypes, so nothing is lost.

ADL_MAX_PATH = 256
ADL_PMLOG_MAX_SENSORS = 256
ADL_OK = 0

# ADLStatus values that matter. ADL_ERR_INVALID_ADL_IDX is the one that used to
# masquerade as "unsupported".
ADL_ERR = -1
ADL_ERR_NOT_INIT = -2
ADL_ERR_INVALID_PARAM = -3
ADL_ERR_INVALID_PARAM_SIZE = -4
ADL_ERR_INVALID_ADL_IDX = -5
ADL_ERR_INVALID_CONTROLLER_IDX = -6
ADL_ERR_NOT_SUPPORTED = -8
ADL_ERR_NULL_POINTER = -9
ADL_ERR_DISABLED_ADAPTER = -10

STATUS_NAMES = {
    ADL_OK: "ADL_OK",
    ADL_ERR: "ADL_ERR",
    ADL_ERR_NOT_INIT: "ADL_ERR_NOT_INIT",
    ADL_ERR_INVALID_PARAM: "ADL_ERR_INVALID_PARAM",
    ADL_ERR_INVALID_PARAM_SIZE: "ADL_ERR_INVALID_PARAM_SIZE",
    ADL_ERR_INVALID_ADL_IDX: "ADL_ERR_INVALID_ADL_IDX",
    ADL_ERR_INVALID_CONTROLLER_IDX: "ADL_ERR_INVALID_CONTROLLER_IDX",
    ADL_ERR_NOT_SUPPORTED: "ADL_ERR_NOT_SUPPORTED",
    ADL_ERR_NULL_POINTER: "ADL_ERR_NULL_POINTER",
    ADL_ERR_DISABLED_ADAPTER: "ADL_ERR_DISABLED_ADAPTER",
}


def status_name(code: int) -> str:
    return STATUS_NAMES.get(code, f"ADL status {code}")


# --------------------------------------------------------------------------
# PMLog sensor ids (AMD's ADL_PMLOG_SENSORS), copied value-for-value
# --------------------------------------------------------------------------

SENSOR_IDS = {
    "CLK_GFXCLK": 1,
    "CLK_MEMCLK": 2,
    "CLK_SOCCLK": 3,
    "CLK_UVDCLK1": 4,
    "CLK_UVDCLK2": 5,
    "CLK_VCECLK": 6,
    "CLK_VCNCLK": 7,
    "TEMPERATURE_EDGE": 8,
    "TEMPERATURE_MEM": 9,
    "TEMPERATURE_VRVDDC": 10,
    "TEMPERATURE_VRMVDD": 11,
    "TEMPERATURE_LIQUID": 12,
    "TEMPERATURE_PLX": 13,
    "FAN_RPM": 14,
    "FAN_PERCENTAGE": 15,
    "SOC_VOLTAGE": 16,
    "SOC_POWER": 17,
    "SOC_CURRENT": 18,
    "INFO_ACTIVITY_GFX": 19,
    "INFO_ACTIVITY_MEM": 20,
    "GFX_VOLTAGE": 21,
    "MEM_VOLTAGE": 22,
    "ASIC_POWER": 23,
    "TEMPERATURE_VRSOC": 24,
    "TEMPERATURE_VRMVDD0": 25,
    "TEMPERATURE_VRMVDD1": 26,
    "TEMPERATURE_HOTSPOT": 27,
    "TEMPERATURE_GFX": 28,
    "TEMPERATURE_SOC": 29,
    "GFX_POWER": 30,
    "GFX_CURRENT": 31,
    "CLK_VCN1CLK1": 36,
    "CLK_VCN1CLK2": 37,
    "BUS_SPEED": 40,
    "BUS_LANES": 41,
    "TEMPERATURE_LIQUID0": 42,
    "TEMPERATURE_LIQUID1": 43,
    "CLK_FCLK": 44,
    "BOARD_POWER": 73,
}

ADL_SENSOR_MAXTYPES = 0

# GCN families; PMLog is the modern path for Vega and later.
FAMILY_AI = 141
FAMILY_NV = 143
FAMILY_NV3 = 145


# --------------------------------------------------------------------------
# Structs
# --------------------------------------------------------------------------


class ADLAdapterInfo(ctypes.Structure):
    _fields_ = [
        ("Size", ctypes.c_int),
        ("AdapterIndex", ctypes.c_int),
        ("UDID", ctypes.c_char * ADL_MAX_PATH),
        ("BusNumber", ctypes.c_int),
        ("DeviceNumber", ctypes.c_int),
        ("FunctionNumber", ctypes.c_int),
        ("VendorID", ctypes.c_int),
        ("AdapterName", ctypes.c_char * ADL_MAX_PATH),
        ("DisplayName", ctypes.c_char * ADL_MAX_PATH),
        ("Present", ctypes.c_int),
        ("Exist", ctypes.c_int),
        ("DriverPath", ctypes.c_char * ADL_MAX_PATH),
        ("DriverPathExt", ctypes.c_char * ADL_MAX_PATH),
        ("PNPString", ctypes.c_char * ADL_MAX_PATH),
        ("OSDisplayIndex", ctypes.c_int),
    ]


class ADLSingleSensorData(ctypes.Structure):
    _fields_ = [("supported", ctypes.c_int), ("value", ctypes.c_int)]


class ADLPMLogDataOutput(ctypes.Structure):
    """ADL2_New_QueryPMLogData_Get's output: size + 256 {supported, value} slots."""
    _fields_ = [("size", ctypes.c_int),
                ("sensors", ADLSingleSensorData * ADL_PMLOG_MAX_SENSORS)]


class ADLPMLogSupportInfo(ctypes.Structure):
    _fields_ = [("usSensors", ctypes.c_ushort * ADL_PMLOG_MAX_SENSORS),
                ("iReserved", ctypes.c_int * 16)]


class ADLPMLogStartInput(ctypes.Structure):
    _fields_ = [("usSensors", ctypes.c_ushort * ADL_PMLOG_MAX_SENSORS),
                ("ulSampleRate", ctypes.c_uint),
                ("iReserved", ctypes.c_int * 15)]


class ADLPMLogStartOutput(ctypes.Structure):
    _fields_ = [("pLoggingAddress", ctypes.c_void_p)]


class ADLPMLogData(ctypes.Structure):
    """The block the driver writes into the address from PMLog_Start."""
    _fields_ = [("ulVersion", ctypes.c_uint),
                ("ulActiveSampleRate", ctypes.c_uint),
                ("ulLastUpdated", ctypes.c_ulonglong),
                ("ulValues", ctypes.c_uint * (ADL_PMLOG_MAX_SENSORS * 2)),
                ("ulReserved", ctypes.c_uint * 256)]


class ADLGcnInfo(ctypes.Structure):
    _fields_ = [("CuCount", ctypes.c_int), ("TexCount", ctypes.c_int),
                ("RopCount", ctypes.c_int), ("ASICFamilyId", ctypes.c_int),
                ("ASICRevisionId", ctypes.c_int)]


class ADLTemperature(ctypes.Structure):
    _fields_ = [("iSize", ctypes.c_int), ("iTemperature", ctypes.c_int)]


class ADLFanSpeedValue(ctypes.Structure):
    _fields_ = [("iSize", ctypes.c_int), ("iSpeedType", ctypes.c_int),
                ("iFanSpeed", ctypes.c_int), ("iFlags", ctypes.c_int)]


class ADLPMActivity(ctypes.Structure):
    _fields_ = [("iSize", ctypes.c_int), ("iEngineClock", ctypes.c_int),
                ("iMemoryClock", ctypes.c_int), ("iVddc", ctypes.c_int),
                ("iActivityPercent", ctypes.c_int),
                ("iCurrentPerformanceLevel", ctypes.c_int),
                ("iCurrentBusSpeed", ctypes.c_int),
                ("iCurrentBusLanes", ctypes.c_int),
                ("iMaximumBusLanes", ctypes.c_int),
                ("iReserved", ctypes.c_int * 2)]


class ADLODNPerformanceStatus(ctypes.Structure):
    _fields_ = [("iCoreClock", ctypes.c_int), ("iMemoryClock", ctypes.c_int),
                ("iDCEFClock", ctypes.c_int), ("iGFXClock", ctypes.c_int),
                ("iUVDClock", ctypes.c_int), ("iVCEClock", ctypes.c_int),
                ("iGPUActivityPercent", ctypes.c_int),
                ("iCurrentCorePerformanceLevel", ctypes.c_int),
                ("iCurrentMemoryPerformanceLevel", ctypes.c_int),
                ("iCurrentDCEFPerformanceLevel", ctypes.c_int),
                ("iCurrentGFXPerformanceLevel", ctypes.c_int),
                ("iUVDPerformanceLevel", ctypes.c_int),
                ("iVCEPerformanceLevel", ctypes.c_int),
                ("iCurrentBusSpeed", ctypes.c_int),
                ("iCurrentBusLanes", ctypes.c_int),
                ("iMaximumBusLanes", ctypes.c_int),
                ("iVDDC", ctypes.c_int), ("iVDDCI", ctypes.c_int)]


ADL_DL_FANCTRL_SPEED_TYPE_PERCENT = 1
ADL_DL_FANCTRL_SPEED_TYPE_RPM = 2

# ADLODNTemperatureType
ODN_EDGE = 1
ODN_MEM = 2
ODN_VRVDDC = 3
ODN_VRMVDD = 4
ODN_LIQUID = 5
ODN_PLX = 6
ODN_HOTSPOT = 7

ADL_MAIN_MALLOC_CALLBACK = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_int)


@dataclass
class AmdAdapter:
    index: int
    name: str
    bus: int
    device: int
    function: int
    vendor_id: int
    pnp_string: str
    display_name: str

    @property
    def pci_label(self) -> str:
        return f"PCI {self.bus:02x}:{self.device:02x}.{self.function}"


@dataclass
class AmdSensorReading:
    """One poll of an AMD adapter, in gpumon metric-field terms."""
    fields: dict[str, float] = field(default_factory=dict)
    methods: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class AmdAdlBackend:
    """Reads AMD GPU sensors through ADL, the way LibreHardwareMonitor does."""

    name = "adl"
    DLL_CANDIDATES = (
        r"C:\Windows\System32\atiadlxx.dll",
        "atiadlxx.dll",
        "atiadlxy.dll",
    )

    def __init__(self) -> None:
        self.dll = None
        self.error = ""
        self.context = ctypes.c_void_p()
        self.adapters: list[AmdAdapter] = []
        #: Which allocation the driver accepted for the adapter list, printed by
        #: `--vram-report` and the self-test: it is the difference between
        #: working AMD telemetry and none at all on this driver.
        self.adapter_info_source = ""
        self._malloc = None
        self._allocations: list[int] = []
        self._pmlog_ready: set[int] = set()
        self._pmlog_buffers: dict[int, ctypes.Array] = {}
        self._pmlog_device: dict[int, ctypes.c_uint] = {}
        self._od_level: dict[int, int] = {}
        self._family: dict[int, int] = {}
        self._samplerate_ms = 1000
        self._lock = threading.RLock()
        self._load()

    # -- loading ---------------------------------------------------------
    def _load(self) -> None:
        if os.name != "nt":
            self.error = "Windows only"
            return
        for candidate in self.DLL_CANDIDATES:
            if os.path.sep in candidate and not os.path.exists(candidate):
                continue
            try:
                # Cdecl: LHM declares these with CallingConvention.Cdecl, and a
                # stdcall call here faults instead of returning a status.
                self.dll = ctypes.CDLL(candidate)
                break
            except OSError:
                continue
        if self.dll is None:
            self.error = "atiadlxx.dll not found"

    def _declare(self) -> None:
        d = self.dll
        P = ctypes.POINTER
        I = ctypes.c_int
        V = ctypes.c_void_p

        def sig(name: str, args: list, restype=I):
            fn = getattr(d, name, None)
            if fn is not None:
                fn.argtypes = args
                fn.restype = restype
            return fn

        self._f_create = sig("ADL2_Main_Control_Create",
                             [ADL_MAIN_MALLOC_CALLBACK, I, P(V)])
        self._f_destroy = sig("ADL2_Main_Control_Destroy", [V])
        self._f_num = sig("ADL2_Adapter_NumberOfAdapters_Get", [V, P(I)])
        self._f_info = sig("ADL2_Adapter_AdapterInfo_Get", [V, V])
        self._f_caps = sig("ADL2_Overdrive_Caps", [V, I, P(I), P(I), P(I)])
        self._f_gcn = sig("ADL2_Adapter_GcnInfo_Get", [V, I, P(ADLGcnInfo)])
        self._f_pmlog_out = sig("ADL2_New_QueryPMLogData_Get",
                                [V, I, P(ADLPMLogDataOutput)])
        # Authoritative per-adapter VRAM usage, in MB. Windows' own
        # "GPU Adapter Memory" counter is unusable for these cards: it reports a
        # budget rather than live usage, double-counts one V620 and returns 0 for
        # the other.
        self._f_vram = sig("ADL2_Adapter_DedicatedVRAMUsage_Get",
                           [V, I, P(I)])
        self._f_pmlog_caps = sig("ADL2_Adapter_PMLog_Support_Get",
                                 [V, I, P(ADLPMLogSupportInfo)])
        self._f_pmlog_dev_create = sig("ADL2_Device_PMLog_Device_Create",
                                       [V, I, P(ctypes.c_uint)])
        self._f_pmlog_dev_destroy = sig("ADL2_Device_PMLog_Device_Destroy",
                                        [V, ctypes.c_uint])
        self._f_pmlog_start = sig("ADL2_Adapter_PMLog_Start",
                                  [V, I, P(ADLPMLogStartInput),
                                   P(ADLPMLogStartOutput), ctypes.c_uint])
        self._f_pmlog_stop = sig("ADL2_Adapter_PMLog_Stop",
                                 [V, I, ctypes.c_uint])
        self._f_od5_temp = sig("ADL2_Overdrive5_Temperature_Get",
                               [V, I, I, P(ADLTemperature)])
        self._f_od5_fan = sig("ADL2_Overdrive5_FanSpeed_Get",
                              [V, I, I, P(ADLFanSpeedValue)])
        self._f_od5_activity = sig("ADL2_Overdrive5_CurrentActivity_Get",
                                   [V, I, P(ADLPMActivity)])
        self._f_odn_temp = sig("ADL2_OverdriveN_Temperature_Get",
                               [V, I, I, P(I)])
        self._f_odn_perf = sig("ADL2_OverdriveN_PerformanceStatus_Get",
                               [V, I, P(ADLODNPerformanceStatus)])

    # -- lifecycle -------------------------------------------------------
    def _adapter_info_buffer(self, count: int):
        """Fill an adapter-info array, the way this driver wants it called.

        Two things had to be discovered by measurement on driver
        32.0.12033.5037, and both are recorded here because neither is in the
        ADL documentation:

        * The driver validates that the buffer came from the allocator it was
          handed in Main_Control_Create. A ctypes-owned buffer returns
          ADL_ERR_INVALID_PARAM for *every* entry count and every `Size` value,
          while the identical call through ADL's own malloc succeeds. Older
          drivers accepted either, so both are tried, ADL's first.
        * The first call after Main_Control_Create returns
          ADL_ERR_INVALID_PARAM and the *immediate retry* returns the adapter
          list, every time, across runs. The first call evidently primes the
          driver's enumeration. Three attempts cost nothing when the first one
          works.

        Returns the array, or None when nothing is accepted.
        """
        size = ctypes.sizeof(ADLAdapterInfo)
        plans: list[tuple[str, bool]] = []
        if self._malloc is not None:
            plans.append(("ADL's allocator", True))
        plans.append(("ctypes", False))

        for label, from_adl in plans:
            for attempt in range(1, 4):
                if from_adl:
                    raw = self._malloc(size * count)
                    if not raw:
                        break
                    ctypes.memset(raw, 0, size * count)
                    buffer = (ADLAdapterInfo * count).from_address(raw)
                    for i in range(count):
                        buffer[i].Size = size
                    rc = self._f_info(self.context, ctypes.c_void_p(raw))
                else:
                    buffer = (ADLAdapterInfo * count)()
                    for i in range(count):
                        buffer[i].Size = size
                    rc = self._f_info(self.context,
                                      ctypes.cast(buffer, ctypes.c_void_p))
                if rc == ADL_OK:
                    self.adapter_info_source = (label if attempt == 1
                                                else f"{label} (attempt {attempt})")
                    # The first attempt's failure is expected and already
                    # handled; leaving it in `error` would report a working
                    # backend as broken in the notes and the reports.
                    self.error = ""
                    return buffer
                self.error = (f"AdapterInfo_Get returned {status_name(rc)} "
                              f"({label}, attempt {attempt})")
                time.sleep(0.02)
        return None

    def available(self) -> bool:
        if self.dll is None:
            return False
        if self.adapters:
            return True
        self._declare()
        if self._f_create is None:
            self.error = "ADL2_Main_Control_Create not exported"
            return False

        # The allocator must outlive ADL's use of it, and the memory it returns
        # must never be freed by us.
        libc = ctypes.CDLL("msvcrt.dll")
        libc.malloc.restype = ctypes.c_void_p
        libc.malloc.argtypes = [ctypes.c_size_t]

        def alloc(size: int) -> int:
            pointer = libc.malloc(size)
            if pointer:
                self._allocations.append(pointer)
            return pointer or 0

        self._malloc = ADL_MAIN_MALLOC_CALLBACK(alloc)
        ctx = ctypes.c_void_p(0)
        rc = self._f_create(self._malloc, 1, ctypes.byref(ctx))
        if rc != ADL_OK or not ctx.value:
            self.error = (f"ADL2_Main_Control_Create returned "
                          f"{status_name(rc)}, context={ctx.value}")
            return False
        self.context = ctx

        count = ctypes.c_int(0)
        rc = self._f_num(self.context, ctypes.byref(count))
        if rc != ADL_OK or count.value <= 0:
            self.error = f"NumberOfAdapters returned {status_name(rc)}"
            return False

        buffer = self._adapter_info_buffer(count.value)
        if buffer is None:
            self.error = "AdapterInfo_Get returned ADL_ERR_INVALID_PARAM"
            return False

        self.adapters = []
        for i in range(count.value):
            info = buffer[i]
            if info.Present != 1 or info.Exist != 1:
                continue
            # A card can be listed once per display output; keep unique PCI
            # addresses so each physical GPU appears once. ADL lists non-AMD
            # adapters too (the NVIDIA card shows up here), and those simply
            # return no sensor data later.
            key = (info.BusNumber, info.DeviceNumber, info.FunctionNumber)
            if any((a.bus, a.device, a.function) == key for a in self.adapters):
                continue
            self.adapters.append(AmdAdapter(
                index=info.AdapterIndex,
                name=info.AdapterName.decode("latin-1", "replace").strip("\x00 "),
                bus=info.BusNumber, device=info.DeviceNumber,
                function=info.FunctionNumber, vendor_id=info.VendorID,
                pnp_string=info.PNPString.decode("latin-1", "replace").strip("\x00 "),
                display_name=info.DisplayName.decode("latin-1", "replace").strip("\x00 ")))
        self._probe_capabilities()
        return bool(self.adapters)

    def poll(self) -> dict[str, dict[str, float]]:
        """Read every AMD adapter once, merged for the sampler.

        Returns {pci_label: {field: value}} using gpumon metric field names, so
        the caller can attach each reading to the physical card whose PCI
        address matches. ADL reports the bus/device itself, which makes this an
        exact mapping rather than an ordering guess.
        """
        out: dict[str, dict[str, float]] = {}
        if not self.context:
            return out
        for adapter in self.adapters:
            # No vendor filter: ADL reports its own wrapper vendor id (0x03EA on
            # this machine) rather than the PCI vendor 0x1002, and every adapter
            # ADL lists here is reachable through its API. Non-AMD cards simply
            # return no data and are skipped below.
            reading = self.poll_adapter(adapter.index)
            fields = dict(reading.fields)
            self._normalise_units(fields)
            vram_used = self._dedicated_vram(adapter.index)
            if vram_used is not None:
                fields["vram_used"] = vram_used
            if fields:
                fields["_adl_index"] = float(adapter.index)
                fields["_adl_bus"] = float(adapter.bus)
                fields["_adl_device"] = float(adapter.device)
                out[adapter.pci_label] = fields
        return out

    def _dedicated_vram(self, idx: int) -> float | None:
        """Live VRAM usage in MB for one adapter, straight from the driver."""
        if self._f_vram is None:
            return None
        used = ctypes.c_int(0)
        if self._f_vram(self.context, idx, ctypes.byref(used)) != ADL_OK:
            return None
        if used.value < 0:
            return None
        return float(used.value)

    @staticmethod
    def _normalise_units(fields: dict[str, float]) -> None:
        """Convert ADL's mixed units into the ones gpumon records.

        PMLog reports millivolts; anything that looks like a core clock below a
        few MHz is really the 10 kHz unit Overdrive5 uses. Doing this here keeps
        the conversion next to the source that produced it.
        """
        for key in ("voltage_core", "voltage_mem", "voltage_soc"):
            value = fields.get(key)
            if value and value > 100:
                fields[key] = value / 1000.0        # mV -> V
        if fields.get("clock_core", 0) and fields["clock_core"] < 100:
            # PMLog already reports MHz on the cards seen so far, so only rescale
            # when the value is implausibly small for a live GPU clock.
            candidate = fields["clock_core"]
            if candidate < 10:
                fields["clock_core"] = candidate * 10.0

    def _probe_capabilities(self) -> None:
        """Learn each adapter's Overdrive level and PMLog support once."""
        for adapter in self.adapters:
            idx = adapter.index
            if self._f_gcn is not None:
                gcn = ADLGcnInfo()
                if self._f_gcn(self.context, idx, ctypes.byref(gcn)) == ADL_OK:
                    self._family[idx] = gcn.ASICFamilyId
            level = -1
            if self._f_caps is not None:
                supported = ctypes.c_int(0)
                enabled = ctypes.c_int(0)
                version = ctypes.c_int(0)
                rc = self._f_caps(self.context, idx, ctypes.byref(supported),
                                  ctypes.byref(enabled), ctypes.byref(version))
                if rc == ADL_OK and supported.value == 1:
                    level = version.value
            self._od_level[idx] = level
            self._start_pmlog(idx, level)

    def _start_pmlog(self, idx: int, od_level: int) -> None:
        """Start PMLog so the driver keeps a rolling sensor block for us.

        PMLog is the path that works on Vega and newer; it needs a device handle
        plus a start call whose output points at memory the *caller* provides.
        """
        family = self._family.get(idx, 0)
        if od_level >= 0 and od_level < 8 and family < FAMILY_AI:
            return
        if self._f_pmlog_caps is None or self._f_pmlog_start is None:
            return
        support = ADLPMLogSupportInfo()
        if self._f_pmlog_caps(self.context, idx, ctypes.byref(support)) != ADL_OK:
            return
        device = ctypes.c_uint(0)
        if self._f_pmlog_dev_create is None:
            return
        if self._f_pmlog_dev_create(self.context, idx,
                                    ctypes.byref(device)) != ADL_OK:
            return
        sensors = [s for s in support.usSensors if s not in (0,)]
        if not sensors:
            return
        start_in = ADLPMLogStartInput()
        for i, sensor in enumerate(sensors[:ADL_PMLOG_MAX_SENSORS - 1]):
            start_in.usSensors[i] = sensor
        start_in.usSensors[min(len(sensors), ADL_PMLOG_MAX_SENSORS - 1)] = \
            ADL_SENSOR_MAXTYPES
        start_in.ulSampleRate = self._samplerate_ms
        start_out = ADLPMLogStartOutput()
        rc = self._f_pmlog_start(self.context, idx, ctypes.byref(start_in),
                                 ctypes.byref(start_out), device)
        if rc != ADL_OK or not start_out.pLoggingAddress:
            return
        self._pmlog_device[idx] = device
        self._pmlog_ready.add(idx)
        # Keep a reference: ADL writes into this block continuously.
        self._pmlog_buffers[idx] = ctypes.cast(
            start_out.pLoggingAddress, ctypes.POINTER(ADLPMLogData)).contents

    def close(self) -> None:
        if self.dll is None:
            return
        for idx, device in list(self._pmlog_device.items()):
            try:
                if self._f_pmlog_stop is not None:
                    self._f_pmlog_stop(self.context, idx, device)
                if self._f_pmlog_dev_destroy is not None:
                    self._f_pmlog_dev_destroy(self.context, device)
            except OSError:
                pass
        self._pmlog_device.clear()
        self._pmlog_ready.clear()
        self._pmlog_buffers.clear()
        if self.context and self._f_destroy is not None:
            try:
                self._f_destroy(self.context)
            except OSError:
                pass
        self.context = ctypes.c_void_p()

    # -- polling ---------------------------------------------------------
    def poll_adapter(self, idx: int) -> AmdSensorReading:
        """Read one adapter. PMLog first, then Overdrive8, then OD5/ODN."""
        reading = AmdSensorReading()
        with self._lock:
            if not self.context:
                reading.errors.append("no ADL context")
                return reading

            values = self._read_pmlog(idx, reading)
            if not values:
                values = self._read_overdrive8(idx, reading)
            if not values:
                values = self._read_overdrive5(idx, reading)

            level = self._od_level.get(idx, -1)
            if level >= 7:
                self._read_odn_temperatures(idx, values)
            if not values:
                reading.errors.append(
                    f"no sensor method returned data "
                    f"(overdrive level {level})")
            reading.fields = values
            return reading

    def _read_pmlog(self, idx: int, reading: AmdSensorReading) -> dict[str, float]:
        """The rolling block the driver fills after PMLog_Start."""
        block = self._pmlog_buffers.get(idx)
        if block is None:
            return {}
        now = time.time()
        # ulLastUpdated is a FILETIME; reject a block that has gone stale.
        if block.ulLastUpdated:
            updated = block.ulLastUpdated / 10_000_000 - 11_644_473_600
            if abs(now - updated) > 48 * 3600:
                return {}
        if block.ulActiveSampleRate == 0 or block.ulActiveSampleRate > 86_400_000:
            return {}
        out: dict[str, float] = {}
        pairs = block.ulValues
        for k in range(0, len(pairs) - 1, 2):
            sensor = pairs[k]
            if sensor == ADL_SENSOR_MAXTYPES:
                break
            out[_pmlog_field(sensor)] = float(pairs[k + 1])
        out.pop("", None)
        if out:
            reading.methods.append("PMLog")
        return out

    def _read_overdrive8(self, idx: int,
                         reading: AmdSensorReading) -> dict[str, float]:
        """ADL2_New_QueryPMLogData_Get: 256 {supported, value} slots."""
        if self._f_pmlog_out is None:
            return {}
        data = ADLPMLogDataOutput()
        data.size = ctypes.sizeof(ADLPMLogDataOutput)
        rc = self._f_pmlog_out(self.context, idx, ctypes.byref(data))
        if rc != ADL_OK:
            return {}
        out: dict[str, float] = {}
        for sensor_id, name in _PMLOG_ID_TO_FIELD.items():
            if 0 <= sensor_id < ADL_PMLOG_MAX_SENSORS:
                slot = data.sensors[sensor_id]
                # `supported` is the flag that was missing before: a slot with
                # supported == 0 means "this card has no such sensor", and its
                # value is meaningless.
                if slot.supported != 0:
                    out[name] = float(slot.value)
        if out:
            reading.methods.append("Overdrive8/PMLog")
        return out

    def _read_overdrive5(self, idx: int,
                         reading: AmdSensorReading) -> dict[str, float]:
        out: dict[str, float] = {}
        if self._f_od5_temp is not None:
            temp = ADLTemperature()
            temp.iSize = ctypes.sizeof(ADLTemperature)
            if self._f_od5_temp(self.context, idx, 0, ctypes.byref(temp)) == ADL_OK:
                value = temp.iTemperature * 0.001
                if 0 < value < 256:
                    out["temp"] = value
        if self._f_od5_fan is not None:
            fan = ADLFanSpeedValue()
            fan.iSize = ctypes.sizeof(ADLFanSpeedValue)
            fan.iSpeedType = ADL_DL_FANCTRL_SPEED_TYPE_RPM
            if self._f_od5_fan(self.context, idx, 0, ctypes.byref(fan)) == ADL_OK:
                if fan.iFanSpeed > 0:
                    out["fan_rpm"] = float(fan.iFanSpeed)
            fan.iSpeedType = ADL_DL_FANCTRL_SPEED_TYPE_PERCENT
            if self._f_od5_fan(self.context, idx, 0, ctypes.byref(fan)) == ADL_OK:
                if 0 <= fan.iFanSpeed <= 100:
                    out["fan"] = float(fan.iFanSpeed)
        if self._f_od5_activity is not None:
            activity = ADLPMActivity()
            activity.iSize = ctypes.sizeof(ADLPMActivity)
            if self._f_od5_activity(self.context, idx,
                                    ctypes.byref(activity)) == ADL_OK:
                if activity.iEngineClock > 0:
                    out["clock_core"] = activity.iEngineClock * 0.01
                if activity.iMemoryClock > 0:
                    out["clock_mem"] = activity.iMemoryClock * 0.01
                if 0 <= activity.iActivityPercent <= 100:
                    out["util"] = float(min(activity.iActivityPercent, 100))
        if self._f_odn_perf is not None:
            perf = ADLODNPerformanceStatus()
            if self._f_odn_perf(self.context, idx, ctypes.byref(perf)) == ADL_OK:
                if perf.iGFXClock > 0:
                    out.setdefault("clock_core", float(perf.iGFXClock))
                if 0 <= perf.iGPUActivityPercent <= 100:
                    out.setdefault("util", float(perf.iGPUActivityPercent))
        if out:
            reading.methods.append("Overdrive5/N")
        return out

    def _read_odn_temperatures(self, idx: int, out: dict[str, float]) -> None:
        if self._f_odn_temp is None:
            return
        mapping = ((ODN_EDGE, "temp"), (ODN_MEM, "mem_temp"),
                   (ODN_HOTSPOT, "hotspot"), (ODN_VRVDDC, None),
                   (ODN_VRMVDD, None), (ODN_LIQUID, None), (ODN_PLX, None))
        for sensor_type, field_name in mapping:
            value = ctypes.c_int(0)
            rc = self._f_odn_temp(self.context, idx, sensor_type,
                                  ctypes.byref(value))
            if rc != ADL_OK:
                continue
            # Some cards report 54000 for sensors they do not have.
            if value.value <= -256_000 or value.value >= 256_000:
                continue
            scaled = value.value * 0.001
            if not (0 < scaled < 256):
                continue
            if field_name and field_name not in out:
                out[field_name] = scaled


def _pmlog_field(sensor_id: int) -> str:
    return _PMLOG_ID_TO_FIELD.get(sensor_id, "")


# Only the ids this program records; the rest of the block is ignored.
_PMLOG_ID_TO_FIELD: dict[int, str] = {
    1: "clock_core",            # CLK_GFXCLK
    2: "clock_mem",             # CLK_MEMCLK
    3: "clock_soc",             # CLK_SOCCLK
    8: "temp",                  # TEMPERATURE_EDGE
    9: "mem_temp",              # TEMPERATURE_MEM
    10: "temp_vrvddc",          # TEMPERATURE_VRVDDC
    11: "temp_vrmvdd",          # TEMPERATURE_VRMVDD
    12: "temp_liquid",
    13: "temp_plx",
    14: "fan_rpm",              # FAN_RPM
    15: "fan",                  # FAN_PERCENTAGE
    16: "voltage_soc",
    17: "power_soc",
    19: "util",                 # INFO_ACTIVITY_GFX
    20: "mem_util",             # INFO_ACTIVITY_MEM
    21: "voltage_core",         # GFX_VOLTAGE (mV)
    22: "voltage_mem",
    23: "power",                # ASIC_POWER
    24: "temp_vrsoc",
    25: "temp_vrmvdd0",
    26: "temp_vrmvdd1",
    27: "hotspot",              # TEMPERATURE_HOTSPOT
    28: "temp_gfx",
    29: "temp_soc",
    30: "power_gfx",
    36: "clock_vcn1",
    37: "clock_vcn2",
    40: "bus_speed",
    41: "bus_lanes",
    42: "temp_liquid0",
    43: "temp_liquid1",
    44: "clock_fclk",
    73: "power_board",          # BOARD_POWER
}
