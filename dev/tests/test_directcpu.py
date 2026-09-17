"""The direct CPU sensor path: PawnIO driver, registers, decoding.

Everything here is testable without a driver, a CPU, or administrator rights,
because the decoding is a pure function of register values. The register numbers
and bit layouts come from LibreHardwareMonitor's own IntelCpu.cs, which is the
point: this is the same thing it does, without running it.
"""

from __future__ import annotations
# GPUMON_TEST_BOOTSTRAP
# Run from anywhere, and from any working directory: the program modules and the
# packaging scripts are one level up, and the paths in here are relative to the
# repository root.
import os as _os
import sys as _sys

#: dev/, where this test lives: the packaging scripts, and the build output.
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
#: The repository root, one level above. The program is here, so this is where the
#: tests run from and the paths they use are relative to.
_REPO = _os.path.dirname(_ROOT)
#: Anything under dev/ is written as a path from the root - "dev/release/..." -
#: because that is where it is from here.
_DEV = "dev"
for _extra in (_REPO, _ROOT, _os.path.join(_ROOT, "scripts")):
    if _extra not in _sys.path:
        _sys.path.insert(0, _extra)
_os.chdir(_REPO)

def _skip_machine_specific_test() -> None:
    """Stop, with a note, when there is no hardware or desktop to test against.

    Set by continuous integration. A test that measures a window or asserts that
    a vendor's library is installed cannot say anything useful on a machine that
    has neither, and reporting a failure there trains everybody to ignore red.
    """
    if _os.environ.get("GPUMON_SKIP_MACHINE_TESTS"):
        print("  --  skipped: this test needs a GPU, a vendor driver, a desktop "
              "session or the sensor driver, and GPUMON_SKIP_MACHINE_TESTS is set")
        raise SystemExit(0)


_skip_machine_specific_test()




import os
import struct
import sys

import cpusensors
import msr
import pawnio

problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


print("=" * 84)
print("DIRECT CPU SENSOR TEST")
print("=" * 84)

print("\n[1] the Intel thermal decoding")
# A real reading from the i9-7900X in this machine: TjMax 95 C, package 16 degrees
# below it, core 1 five degrees below.
check("TjMax comes from bits 23:16 of 0x1A2",
      msr.tjmax_from_target(0x005F0000) == 95, str(msr.tjmax_from_target(0x005F0000)))
check("an invalid status register yields no reading, not a temperature",
      msr.temperature(0x00000000, 95.0) is None,
      str(msr.temperature(0x00000000, 95.0)))
check("a valid one decodes to TjMax minus the distance",
      msr.temperature(0x80000000 | (16 << 16), 95.0) == 79.0,
      str(msr.temperature(0x80000000 | (16 << 16), 95.0)))
check("the distance is read from bits 22:16",
      msr.delta_to_tjmax(0x80000000 | (0x7F << 16)) == 127,
      str(msr.delta_to_tjmax(0x80000000 | (0x7F << 16))))

print("\n[2] implausible readings are refused")
# Firmware that reports nonsense, or a decoding mistake, must not become a
# number on a graph: both look identical from here. The distance field is only
# seven bits wide, so an out-of-range temperature needs a TjMax and a distance
# that really do produce one.
check("a reading below freezing is refused",
      msr.temperature(0x80000000 | (40 << 16), 10.0) is None,
      str(msr.temperature(0x80000000 | (40 << 16), 10.0)))
check("a reading above 125 C is refused",
      msr.temperature(0x80000000 | (0 << 16), 200.0) is None)
check("a nonsense TjMax falls back to Intel's usual 100 C",
      msr.decode_intel(0x80000000 | (10 << 16), [], 0x00000000).tjmax == 100.0,
      str(msr.decode_intel(None, [], 0).tjmax))

print("\n[3] one poll decodes package and cores together")
decoded = msr.decode_intel(0x80000000 | (16 << 16),
                           [0x80000000 | (5 << 16), 0x80000000 | (9 << 16)],
                           0x005F0000)
print(f"    package {decoded.package} C, cores {decoded.cores}, "
      f"hottest {decoded.hottest_core} C, TjMax {decoded.tjmax:.0f}")
check("package temperature", decoded.package == 79.0, str(decoded.package))
check("core temperatures", decoded.cores == [90.0, 86.0], str(decoded.cores))
check("the hottest core is reported", decoded.hottest_core == 90.0)

print("\n[4] the other registers decode as LibreHardwareMonitor does")
check("core ratio from bits 15:8 of 0x198",
      msr.frequency_ratio(0x00000E00) == 14, str(msr.frequency_ratio(0x00000E00)))
check("core voltage from bits 47:32, in 1/8192 V",
      abs((msr.core_voltage(0x000017BE00000000) or 0) - 0.742) < 0.002,
      str(msr.core_voltage(0x000017BE00000000)))
check("a zero voltage field means 'not supported', not 0 V",
      msr.core_voltage(0x00000000000) is None)
check("the energy unit is a negative power of two",
      abs(msr.energy_unit(0x00000803) - 1.0 / 256) < 1e-12
      and abs(msr.energy_unit(0x00001003) - 1.0 / 65536) < 1e-12,
      f"{msr.energy_unit(0x00000803)} / {msr.energy_unit(0x00001003)}")
check("the base clock multiplier comes from MSR 0xCE",
      msr.tsc_multiplier(0x00006400) == 100, str(msr.tsc_multiplier(0x00006400)))

print("\n[5] the PawnIO client is shaped like the protocol")
check("the device path is PawnIO's own",
      pawnio.DEVICE_PATH.endswith("Device\\PawnIO"), pawnio.DEVICE_PATH)
check("the load-binary control code is the documented one",
      (pawnio.DEVICE_TYPE | pawnio.IOCTL_PIO_LOAD_BINARY) == (41394 << 16 | 0x2084),
      hex(pawnio.DEVICE_TYPE | pawnio.IOCTL_PIO_LOAD_BINARY))
check("the execute control code is the documented one",
      (pawnio.DEVICE_TYPE | pawnio.IOCTL_PIO_EXECUTE_FN) == (41394 << 16 | 0x2104),
      hex(pawnio.DEVICE_TYPE | pawnio.IOCTL_PIO_EXECUTE_FN))
check("function names live in a 32-byte field", pawnio.FN_NAME_LENGTH == 32)
check("an unopened client reports an error instead of raising",
      pawnio.PawnIO().read_msr(0x1A2) is None)
client = pawnio.PawnIO()
check("reading without a driver explains what is wrong",
      client.read_msr(msr.IA32_TEMPERATURE_TARGET) is None
      and "not open" in client.error, client.error)

print("\n[6] the modules are present and carry the functions we call")
module = pawnio.module_path(cpusensors.INTEL_MODULE)
check("the Intel MSR module is bundled", os.path.exists(module), module)
if os.path.exists(module):
    blob = open(module, "rb").read()
    print(f"    {os.path.basename(module)}: {len(blob):,} bytes")
    check("it advertises ioctl_read_msr", b"ioctl_read_msr" in blob)
    check("it is a PawnIO module, not a stray file",
          blob[:4] == b"\x00\x02\x00\x00", blob[:4].hex())
amd = pawnio.module_path(cpusensors.AMD_MODULES[0])
check("an AMD module is bundled too", os.path.exists(amd), amd)
if os.path.exists(amd):
    amd_blob = open(amd, "rb").read()
    check("it advertises the SMN read used for AMD temperatures",
          b"ioctl_read_smn" in amd_blob)

print("\n[7] identification decides whether a driver is worth loading")
identity = cpusensors.identify()
print(f"    {identity.name or 'unknown'}  ->  {identity.describe()}")
print(f"    decoding path available: {identity.supported_by_us}")
if os.name == "nt":
    check("this machine identifies a known Intel model",
          identity.vendor == "intel" and identity.model in
          msr.INTEL_MODELS_WITH_MSR_TJMAX,
          f"vendor={identity.vendor} model={identity.model}")
    check("Skylake-X (model 0x55) is in the supported list",
          0x55 in msr.INTEL_MODELS_WITH_MSR_TJMAX)

print("\n[8] opening the driver needs elevation, and says so")
if os.name == "nt":
    elevated = False
    try:
        import ctypes
        elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        pass
    print(f"    this process is elevated: {elevated}")
    with pawnio.PawnIO() as probe:
        opened = probe.open()
        print(f"    device opened: {opened}" + ("" if opened else f"  ({probe.error})"))
        if elevated:
            check("an elevated process can open the driver", opened, probe.error)
        else:
            check("an unelevated process is refused, with an actionable message",
                  not opened and ("administrator" in probe.error
                                  or "not installed" in probe.error),
                  probe.error)

print("\n[9] the AMD decoding, from the same documentation path")
# AMD has no thermal status MSR: the control temperature lives in the SMU's
# register space and is reached by indirect PCI access, which is what the
# AMDFamily17 module's ioctl_read_smn does. These rules are LibreHardwareMonitor's
# Amd17Cpu.cs and Linux's k10temp, and - unlike the Intel ones - they have not
# been checked against a real AMD machine, so what is tested here is the
# arithmetic, not the hardware.
def amd_raw(celsius: float, extra: int = 0) -> int:
    """A THM_TCON_CUR_TMP value that encodes this temperature."""
    return (int(round(celsius * 8)) << 21) | extra


check("the control temperature is bits 31:21 at 0.125 C each",
      msr.parse_amd_temperature(amd_raw(45.0)) == 45.0,
      str(msr.parse_amd_temperature(amd_raw(45.0))))
check("a fractional reading survives the decode",
      msr.parse_amd_temperature(amd_raw(45.125)) == 45.125,
      str(msr.parse_amd_temperature(amd_raw(45.125))))
check("bit 19 subtracts a further 49 C",
      msr.parse_amd_temperature(amd_raw(60.0, msr.AMD_TEMP_OFFSET_FLAG)) == 11.0,
      str(msr.parse_amd_temperature(amd_raw(60.0, msr.AMD_TEMP_OFFSET_FLAG))))
check("a nonsense reading is refused",
      msr.parse_amd_temperature(0) is None)

check("the CCD formula is (value * 125 - 305000) / 1000",
      msr.parse_amd_ccd_temperature(2840) == 50.0,
      str(msr.parse_amd_ccd_temperature(2840)))
check("a zero CCD register means that CCD is absent",
      msr.parse_amd_ccd_temperature(0) is None)
check("a CCD above 125 C is refused",
      msr.parse_amd_ccd_temperature(0xFFF) is None,
      str(msr.parse_amd_ccd_temperature(0xFFF)))

print("\n[10] the model offsets, and why Tdie is not Tctl")
for name, expected in (("AMD Ryzen 7 1700X Eight-Core Processor", -20.0),
                       ("AMD Ryzen 7 1800X", -20.0),
                       ("AMD Ryzen Threadripper 1950X", -27.0),
                       ("AMD Ryzen 7 2700X", -10.0),
                       ("AMD Ryzen 9 7950X", 0.0),
                       ("Intel(R) Core(TM) i9-7900X", 0.0)):
    got = msr.amd_tctl_offset(name)
    check(f"{name[:34]:34} -> {got:+.0f} C", got == expected, f"{got} vs {expected}")
reading = msr.AmdTemperatures(control=80.0, die=80.0 - 27.0, offset=-27.0)
check("the package reading is the die temperature, not the control one",
      reading.package == 53.0, str(reading.package))
check("with no offset they are the same number",
      msr.AmdTemperatures(control=60.0, die=60.0, offset=0.0).package == 60.0)

print("\n[11] package power, from an energy counter that wraps")
# 0.5 ** ESU joules per increment: ESU 8 is 1/256 J, ESU 0 is 1 J.
check("the energy unit is 0.5 to the power of ESU",
      abs(msr.amd_energy_unit(8 << 8) - 1.0 / 256) < 1e-15
      and msr.amd_energy_unit(0) == 1.0,
      f"{msr.amd_energy_unit(8 << 8)} / {msr.amd_energy_unit(0)}")
check("power is increments times joules divided by time",
      abs((msr.amd_package_power(1.0 / 256, 0, 25600, 1.0) or 0) - 100.0) < 0.01,
      str(msr.amd_package_power(1.0 / 256, 0, 25600, 1.0)))
check("a counter that wrapped is differenced modulo 2^32",
      abs((msr.amd_package_power(1.0 / 256, 0xFFFFFF00, 0x00000100, 1.0) or 0)
          - 2.0) < 0.01,
      str(msr.amd_package_power(1.0 / 256, 0xFFFFFF00, 0x00000100, 1.0)))
check("an impossible wattage is refused rather than reported",
      msr.amd_package_power(1.0, 0, 0xFFFFFFFF, 0.001) is None)
check("no previous reading means no power yet",
      msr.amd_package_power(1.0, None, 100, 1.0) is None)

print("\n[12] and the reader is wired to them")
source = open("cpusensors.py", encoding="utf-8").read()
check("the AMD path reads the SMN temperature register",
      "AMD_THM_TCON_CUR_TMP" in source and "read_smn" in source)
check("it applies the model offset", "amd_tctl_offset" in source)
check("it can produce package power", "amd_package_power" in source)
check("it is honest that it has not run on that hardware",
      "not yet run on an AMD" in source or "not been run on an AMD" in source,
      "the docstring should say so")
check("the setup says the same to an AMD user",
      "has not been run on an AMD machine" in
      open("sensorsetup.py", encoding="utf-8").read())
check("the published reading prefers the die temperature",
      "package_override" in source and "die" in source)

print("\n" + "=" * 84)
if problems:
    print(f"DIRECT CPU SENSOR TEST FAILED ({len(problems)})")
    for item in problems:
        print(f"  - {item}")
else:
    print("DIRECT CPU SENSOR TEST PASSED")
print("=" * 84)
raise SystemExit(1 if problems else 0)
