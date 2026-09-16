"""CPU sensors decoded from model-specific registers.

The register numbers and the bit layouts below are not guessed: they are what
LibreHardwareMonitor does, read from its `Hardware/Cpu/IntelCpu.cs` (v0.9.6).
Intel's digital thermal sensor reports the *distance* to TjMax rather than a
temperature, so the arithmetic needs the target as well:

    TjMax        = (MSR 0x1A2 >> 16) & 0xFF
    package temp = TjMax - ((MSR 0x1B1 >> 16) & 0x7F)     when bit 31 is set
    core temp    = TjMax - ((MSR 0x19C >> 16) & 0x7F)     when bit 31 is set

Bit 31 of the two status registers means "this reading is valid"; when it is
clear the sensor is not reporting and the honest answer is no reading at all,
which is why these return None instead of a number.

Everything here is pure: it takes register values and returns temperatures, so it
can be tested without a driver, a CPU, or administrator rights.
"""
from __future__ import annotations

from dataclasses import dataclass

# -- Intel registers -------------------------------------------------------
IA32_THERM_STATUS = 0x019C          # per core, 0 = core 0
IA32_PACKAGE_THERM_STATUS = 0x01B1  # whole package
IA32_TEMPERATURE_TARGET = 0x01A2    # TjMax in bits 23:16
IA32_PERF_STATUS = 0x0198           # current ratio and core voltage
MSR_RAPL_POWER_UNIT = 0x0606
MSR_PKG_ENERGY_STATUS = 0x0611
MSR_PLATFORM_INFO = 0x00CE

#: Intel CPUID families/models whose temperature reading works with the TjMax
#: from MSR 0x1A2. The list is LibreHardwareMonitor's, minus the parts old enough
#: to need hard-coded per-stepping values that a 2026 machine will never be.
INTEL_MODELS_WITH_MSR_TJMAX = {
    0x1A, 0x1E, 0x1F, 0x25, 0x2C, 0x2E, 0x2F,      # Nehalem
    0x2A, 0x2D,                                     # Sandy Bridge
    0x3A, 0x3E,                                     # Ivy Bridge
    0x3C, 0x3F, 0x45, 0x46,                         # Haswell
    0x3D, 0x47, 0x4F, 0x56,                         # Broadwell
    0x36,                                           # Atom
    0x37, 0x4A, 0x4D, 0x5A, 0x5D,                   # Silvermont
    0x4E, 0x5E, 0x55,                               # Skylake (0x55 = Skylake-X)
    0x4C,                                           # Airmont
    0x8E, 0x9E,                                     # Kaby Lake, Coffee Lake
    0x5C, 0x5F, 0x7A,                               # Goldmont
    0x66,                                           # Cannon Lake
    0x7D, 0x7E, 0x6A, 0x6C,                         # Ice Lake
    0xA5, 0xA6,                                     # Comet Lake
    0x86,                                           # Tremont
    0x8C, 0x8D,                                     # Tiger Lake
    0x97, 0x9A, 0xBE,                               # Alder Lake
    0xB7, 0xBA, 0xBF,                               # Raptor Lake
    0xAC, 0xAA,                                     # Meteor Lake
    0x9C,                                           # Jasper Lake
    0xA7,                                           # Rocket Lake
    0xC5, 0xC6,                                     # Arrow Lake
    0xBD,                                           # Lunar Lake
    0x8F,                                           # Sapphire Rapids
    0x96,                                           # Elkhart Lake
}

#: Plausibility window. A CPU outside this range is an unimplemented sensor, a
#: decoding mistake or a dead machine - in all three cases a number would be a
#: lie, and the ACPI-zone fallback in metrics.py makes the same judgement.
MIN_CELSIUS = 5.0
MAX_CELSIUS = 125.0

# -- AMD registers ---------------------------------------------------------
# AMD does not expose a thermal status MSR the way Intel does. The control
# temperature lives in the SMU's register space, reached through indirect PCI
# accesses, which is what the AMDFamily17 module's `ioctl_read_smn` does. These
# addresses and bit layouts are from LibreHardwareMonitor's Amd17Cpu.cs, which in
# turn documents them from AMD's PPRs; the offset table is the one Linux's
# k10temp driver uses.
AMD_THM_TCON_CUR_TMP = 0x00059800     # CUR_TEMP in bits 31:21, 0.125 C each
AMD_TEMP_OFFSET_FLAG = 0x00080000     # bit 19: subtract a further 49 C
AMD_CCD_TEMP_ZEN2 = 0x00059954        # F17H_M70H_CCD1_TEMP
AMD_CCD_TEMP_ZEN4 = 0x00059B08        # F17H_M61H_CCD1_TEMP
AMD_CCD_STRIDE = 0x4
AMD_MSR_PWR_UNIT = 0xC0010299
AMD_MSR_PKG_ENERGY_STAT = 0xC001029B

#: Models with per-CCD temperature registers, and which base address they use.
AMD_CCD_MODELS = {
    0x31: "zen2",     # Threadripper 3000
    0x71: "zen2",     # Zen 2
    0x21: "zen3",     # Zen 3
    0x61: "zen4",     # Zen 4 (Raphael)
    0x44: "zen4",     # Zen 5 (Granite Ridge)
}

#: The Tctl/Tdie offset, by processor name - the same table Linux uses, because
#: the firmware applies it inconsistently across the early Ryzen parts.
AMD_TCTL_OFFSETS = (
    (("1600X", "1700X", "1800X"), -20.0),
    (("Threadripper 19", "Threadripper 29"), -27.0),
    (("2700X",), -10.0),
)


def amd_tctl_offset(name: str) -> float:
    """The firmware's offset between the reported control temperature and the die.

    Early Ryzen reports Tctl with a deliberate offset added so that all parts
    would ramp their fans at the same reported number; Tdie is what the silicon
    actually is. Newer parts report them identically, which is the zero here.
    """
    for names, offset in AMD_TCTL_OFFSETS:
        if any(token in name for token in names):
            return offset
    return 0.0


def parse_amd_temperature(raw: int) -> float | None:
    """Decode THM_TCON_CUR_TMP into degrees Celsius.

    Bits 31:21 hold the temperature in units of 0.125 C. Bit 19 says the part
    applies a further -49 C correction, which the firmware sets on some models.
    """
    if raw is None:
        return None
    celsius = (raw >> 21) * 0.125
    if raw & AMD_TEMP_OFFSET_FLAG:
        celsius -= 49.0
    if not MIN_CELSIUS <= celsius <= MAX_CELSIUS:
        return None
    return celsius


def parse_amd_ccd_temperature(raw: int) -> float | None:
    """Decode one CCD temperature register: 12 bits, in 0.125 C, offset by 305.

    The documented formula is `(value * 125 - 305000) / 1000`, which is the same
    arithmetic written differently. A zero register means that CCD is not
    populated, and a result above the ceiling means the same thing.
    """
    if raw is None:
        return None
    value = raw & 0xFFF
    if value == 0:
        return None
    celsius = (value * 125 - 305000) * 0.001
    if not MIN_CELSIUS <= celsius <= MAX_CELSIUS:
        return None
    return celsius


def amd_energy_unit(power_unit_msr: int) -> float:
    """Joules per energy-status increment: 0.5 to the power of the ESU field."""
    return 0.5 ** ((power_unit_msr >> 8) & 0x1F)


def amd_package_power(unit: float, previous: int, current: int,
                      seconds: float) -> float | None:
    """Watts from two energy-status readings and the time between them.

    The counter is 32-bit and wraps, so the difference is taken modulo 2^32
    rather than as a plain subtraction - which is what made an early version
    report a spike of several kilowatts whenever it rolled over.
    """
    if unit <= 0 or seconds <= 0 or previous is None or current is None:
        return None
    delta = (current - previous) & 0xFFFFFFFF
    watts = unit * delta / seconds
    if not 0.0 <= watts <= 1500.0:
        return None
    return watts


@dataclass
class AmdTemperatures:
    """What one poll of an AMD CPU produced."""

    control: float | None = None      # Tctl, what the firmware reports
    die: float | None = None          # Tdie, after the model's offset
    ccds: list[float] | None = None
    offset: float = 0.0
    package_power: float | None = None

    @property
    def package(self) -> float | None:
        """The number to show: the die temperature when it is known.

        Tdie is the silicon; Tctl is the same reading with the firmware's fan
        offset added, so showing Tctl as "the CPU temperature" overstates it by
        up to 27 degrees on the parts that use the offset.
        """
        return self.die if self.die is not None else self.control


def _valid(status: int) -> bool:
    """Bit 31 of IA32_(PACKAGE_)THERM_STATUS: 'this reading is meaningful'."""
    return bool(status & 0x80000000)


def delta_to_tjmax(status: int) -> int:
    """Bits 22:16 of a thermal status register: degrees below TjMax."""
    return (status & 0x007F0000) >> 16


def tjmax_from_target(msr_value: int) -> int:
    """MSR 0x1A2 bits 23:16: the target junction temperature in degrees C."""
    return (msr_value >> 16) & 0xFF


def temperature(status: int, tjmax: float) -> float | None:
    """Turn a thermal status register into a temperature, or None if invalid."""
    if not _valid(status):
        return None
    celsius = tjmax - delta_to_tjmax(status)
    if not MIN_CELSIUS <= celsius <= MAX_CELSIUS:
        return None
    return celsius


def frequency_ratio(perf_status: int) -> int:
    """MSR 0x198 bits 15:8: the current multiplier (Skylake and later)."""
    return (perf_status >> 8) & 0xFF


def core_voltage(perf_status: int) -> float | None:
    """MSR 0x198 bits 47:32, in units of 1/8192 V; zero means unsupported."""
    raw = (perf_status >> 32) & 0xFFFF
    if raw == 0:
        return None
    return raw / (1 << 13)


def energy_unit(power_unit_msr: int) -> float:
    """MSR 0x606 bits 12:8: joules per energy-status unit."""
    exponent = (power_unit_msr >> 8) & 0x1F
    return 1.0 / (1 << exponent)


def tsc_multiplier(platform_info_msr: int) -> int:
    """MSR 0xCE bits 15:8: the base clock multiplier."""
    return (platform_info_msr >> 8) & 0xFF


@dataclass
class IntelTemperatures:
    """What one poll of an Intel CPU produced."""

    package: float | None = None
    cores: list[float] | None = None
    tjmax: float = 0.0
    #: Set when the CPU reports a package sensor at all.
    has_package: bool = False

    @property
    def hottest_core(self) -> float | None:
        cores = [c for c in (self.cores or []) if c is not None]
        return max(cores) if cores else None


def decode_intel(package_status: int | None, core_status: list[int],
                 target_msr: int | None) -> IntelTemperatures:
    """Decode a poll taken from an Intel CPU.

    Kept separate from the register reads so it can be tested with numbers
    captured from a real machine, and so a wrong answer here is provably a
    decoding bug rather than a driver one.
    """
    tjmax = float(tjmax_from_target(target_msr)) if target_msr else 100.0
    if not 50.0 <= tjmax <= 125.0:
        # TjMax outside this range means the register is not what we think it is;
        # 100 C is Intel's usual target and the best available assumption.
        tjmax = 100.0
    cores = [temperature(status, tjmax) for status in core_status]
    return IntelTemperatures(
        package=temperature(package_status, tjmax)
        if package_status is not None else None,
        cores=cores,
        tjmax=tjmax,
        has_package=package_status is not None)
