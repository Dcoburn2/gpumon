"""Read CPU sensors ourselves: identify the processor, then read its registers.

This is the replacement for going through LibreHardwareMonitor. The pieces:

  * identify the CPU from the registry's processor identifier, which needs no
    driver at all ("Intel64 Family 6 Model 85 Stepping 4");
  * open the PawnIO driver and load the signed module that matches the vendor;
  * read the registers, pinning the thread to each core for the per-core sensors
    (a core's MSR can only be read from that core);
  * decode them with msr.py, which holds the bit layouts.

Nothing here needs administrator rights *by itself* - every read goes through the
driver, and opening that is what needs them, once.
"""
from __future__ import annotations

import ctypes
import os
import re
import time
from dataclasses import dataclass, field

import msr
import pawnio

#: The module each CPU vendor needs. These are PawnIO's own signed modules,
#: fetched from its release by sensorsetup - not copies lifted out of
#: LibreHardwareMonitor, which pins an older version of them.
INTEL_MODULE = "IntelMSR.bin"
AMD_MODULES = ("AMDFamily17.bin", "RyzenSMU.bin")

_IDENTIFIER = re.compile(r"Family (\d+) Model (\d+) Stepping (\d+)")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetCurrentThread.restype = ctypes.c_void_p
kernel32.SetThreadAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
kernel32.SetThreadAffinityMask.restype = ctypes.c_size_t


@dataclass
class CpuIdentity:
    vendor: str = ""
    family: int = 0
    model: int = 0
    stepping: int = 0
    name: str = ""
    cores: int = 0

    @property
    def supported_by_us(self) -> bool:
        """Whether we have a decoding path for this processor."""
        if self.vendor == "intel":
            return self.model in msr.INTEL_MODELS_WITH_MSR_TJMAX
        return self.vendor == "amd"

    def describe(self) -> str:
        vendor = self.vendor.capitalize() or "unknown"
        return (f"{vendor} family {self.family} model {self.model} "
                f"stepping {self.stepping}")


def identify() -> CpuIdentity:
    """Read what Windows already knows about the processor.

    The registry's `Identifier` value carries the family/model/stepping, so this
    costs a registry read rather than a driver load - which matters because it
    decides whether a driver is worth loading at all.
    """
    identity = CpuIdentity()
    if os.name != "nt":
        return identity
    import winreg
    try:
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            name = winreg.QueryValueEx(key, "ProcessorNameString")[0]
            ident = winreg.QueryValueEx(key, "Identifier")[0]
    except OSError:
        return identity
    identity.name = str(name).strip()
    match = _IDENTIFIER.search(str(ident))
    if match:
        identity.family, identity.model, identity.stepping = (
            int(value) for value in match.groups())
    lowered = identity.name.lower()
    if "intel" in lowered:
        identity.vendor = "intel"
    elif "amd" in lowered:
        identity.vendor = "amd"
    identity.cores = os.cpu_count() or 0
    return identity


@dataclass
class CpuReadings:
    """One poll of the processor's own sensors."""

    temperatures: msr.IntelTemperatures | None = None
    #: AMD's readings, which do not fit the Intel shape (no TjMax, no per-core
    #: registers - the SMU reports the package and, on some models, one value per
    #: CCD).
    amd: msr.AmdTemperatures | None = None
    per_core: list[float | None] = field(default_factory=list)
    tjmax: float = 0.0
    frequency_mhz: float | None = None
    voltage: float | None = None
    error: str = ""
    #: Set by the AMD path, which knows its own package and hottest figures.
    package_override: float | None = None
    hottest_override: float | None = None

    @property
    def package(self) -> float | None:
        if self.package_override is not None:
            return self.package_override
        if self.temperatures is None:
            return None
        return self.temperatures.package

    def hottest(self) -> float | None:
        if self.hottest_override is not None:
            return self.hottest_override
        cores = [c for c in self.per_core if c is not None]
        if cores:
            return max(cores)
        return self.package

    @property
    def power(self) -> float | None:
        """Package power in watts, when the backend can compute it."""
        return self.amd.package_power if self.amd is not None else None


def logical_core_count() -> int:
    return os.cpu_count() or 1


def read_with_affinity(pawn: pawnio.PawnIO, index: int, mask: int) -> int | None:
    """Read an MSR from a specific logical processor.

    A core's thermal status register reports that core, so it has to be read by a
    thread running on it. The previous affinity is restored afterwards: leaving
    the sampling thread pinned to core 0 would skew every utilisation figure
    gpumon reports.
    """
    thread = kernel32.GetCurrentThread()
    previous = kernel32.SetThreadAffinityMask(thread, mask)
    try:
        return pawn.read_msr(index)
    finally:
        if previous:
            kernel32.SetThreadAffinityMask(thread, previous)


def read_intel(pawn: pawnio.PawnIO) -> CpuReadings:
    """Package and per-core temperatures from an Intel processor."""
    readings = CpuReadings()
    target = pawn.read_msr(msr.IA32_TEMPERATURE_TARGET)
    package = pawn.read_msr(msr.IA32_PACKAGE_THERM_STATUS)
    if target is None and package is None:
        readings.error = pawn.error or "no answer from the driver"
        return readings

    count = logical_core_count()
    core_status: list[int] = []
    for index in range(count):
        value = read_with_affinity(pawn, msr.IA32_THERM_STATUS, 1 << index)
        core_status.append(value if value is not None else 0)
    decoded = msr.decode_intel(package, core_status, target)
    readings.temperatures = decoded
    readings.tjmax = decoded.tjmax
    readings.per_core = list(decoded.cores or [])

    perf = pawn.read_msr(msr.IA32_PERF_STATUS)
    platform = pawn.read_msr(msr.MSR_PLATFORM_INFO)
    if perf is not None and platform is not None:
        multiplier = msr.tsc_multiplier(platform)
        if multiplier:
            # The base clock is 100 MHz on everything modern; deriving it from the
            # TSC would need a timer, and being 0.1 % out on a clock reading is not
            # worth a calibration loop.
            readings.frequency_mhz = msr.frequency_ratio(perf) * 100.0
        readings.voltage = msr.core_voltage(perf)
    return readings


def read_amd(pawn: pawnio.PawnIO, identity: CpuIdentity | None = None,
             state: dict | None = None) -> CpuReadings:
    """AMD CPU temperature and package power, through the SMU's register space.

    What `read_intel` does with MSRs, this does with indirect SMN reads: the
    control temperature sits at a fixed address, the per-CCD temperatures follow
    a table of their own, and the package power comes from an energy counter that
    has to be differenced over time.

    Written from LibreHardwareMonitor's Amd17Cpu.cs and AMD's register
    documentation, and tested against those rules - but **not yet run on an AMD
    machine**, so it reports what it finds rather than assuming it worked.
    """
    identity = identity or identify()
    readings = CpuReadings()
    state = state if state is not None else {}

    raw = pawn.read_smn(msr.AMD_THM_TCON_CUR_TMP)
    if raw is None:
        readings.error = (pawn.error
                          or "the SMU did not answer the temperature register")
        return readings
    control = msr.parse_amd_temperature(raw)
    if control is None:
        readings.error = f"the temperature register read as {raw:#010x}"
        return readings

    offset = msr.amd_tctl_offset(identity.name)
    decoded = msr.AmdTemperatures(control=control,
                                  die=control + offset if offset else control,
                                  offset=offset)

    # Per-CCD temperatures, on the models that publish them.
    flavour = msr.AMD_CCD_MODELS.get(identity.model)
    if flavour:
        base = (msr.AMD_CCD_TEMP_ZEN4 if flavour == "zen4"
                else msr.AMD_CCD_TEMP_ZEN2)
        ccds: list[float] = []
        for index in range(8):
            value = msr.parse_amd_ccd_temperature(
                pawn.read_smn(base + index * msr.AMD_CCD_STRIDE))
            if value is not None:
                ccds.append(value)
        if ccds:
            decoded.ccds = ccds

    # Package power, from the energy counter's growth since the last poll.
    unit_msr = pawn.read_msr(msr.AMD_MSR_PWR_UNIT)
    energy = pawn.read_msr(msr.AMD_MSR_PKG_ENERGY_STAT)
    if unit_msr is not None and energy is not None:
        unit = msr.amd_energy_unit(unit_msr)
        current = energy & 0xFFFFFFFF
        previous = state.get("energy")
        then = state.get("time")
        now = time.monotonic()
        if previous is not None and then is not None:
            decoded.package_power = msr.amd_package_power(
                unit, previous, current, now - then)
        state["energy"] = current
        state["time"] = now

    readings.temperatures = None
    readings.amd = decoded
    readings.per_core = []
    hottest = decoded.package
    if decoded.ccds:
        hottest = max(decoded.ccds)
    readings.tjmax = 0.0
    readings.voltage = None
    if decoded.package is not None:
        readings.package_override = decoded.package
    if hottest is not None:
        readings.hottest_override = hottest
    return readings


def read_cpu(pawn: pawnio.PawnIO | None = None) -> CpuReadings:
    """Identify the CPU and read its sensors, opening the driver if needed."""
    identity = identify()
    own_driver = pawn is None
    if own_driver:
        pawn = pawnio.PawnIO()
    try:
        if identity.vendor == "intel":
            module = pawnio.module_path(INTEL_MODULE)
            if not pawn.load_module_file(module):
                return CpuReadings(error=pawn.error)
            return read_intel(pawn)
        if identity.vendor == "amd":
            for name in AMD_MODULES:
                module = pawnio.module_path(name)
                if os.path.exists(module) and pawn.load_module_file(module):
                    return read_amd(pawn)
            return CpuReadings(error="no AMD module is available")
        return CpuReadings(error=f"unsupported processor: {identity.name}")
    finally:
        if own_driver and pawn is not None:
            pawn.close()
