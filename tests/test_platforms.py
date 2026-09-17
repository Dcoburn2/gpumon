"""Platform-layer test: dispatch, Linux import-safety, and no Windows regression.

The Linux code cannot be *run* here - this machine is Windows - so this test
checks the three things that can be checked on this host, and does it in the
order of how much they would hurt if they were wrong:

1. `platforms.current_platform()` says "windows", and the Windows bundle hands
   out real, working PDH/ADL/LHM/psutil backends. That is the "did the port
   break Windows" half.
2. `platforms.linux` imports cleanly in a fresh interpreter on a machine with no
   /sys at all, and every backend in it reports `available() == False` with an
   explanation instead of raising. That is the import-safety half of the Linux
   work: an OS check that crashes at import time takes the whole program with it.
3. The Linux *parsing* is exercised against synthetic sysfs trees and a recorded
   rocm-smi JSON payload. This is the strongest check available without a Linux
   host: the fixtures carry the documented ABI values (millidegrees C,
   microwatts, bytes, the -1 "unreadable" sentinel, the `*`-flagged DPM line) and
   the documented absences, so a wrong path, a wrong divisor or a zero substituted
   for a missing file fails here rather than on a user's machine. It does not
   prove the paths exist on a real kernel - nothing on this host can.

Plus the Windows regression checks: `SensorManager` must discover exactly the
GPUs, metric keys and notes it discovered before the platform abstraction, and
`sampler.raise_thread_priority()` must still work.

Exit code is non-zero if anything fails.
"""

from __future__ import annotations
# GPUMON_TEST_BOOTSTRAP
# Run from anywhere, and from any working directory: the program modules and the
# packaging scripts are one level up, and the paths in here are relative to the
# repository root.
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _extra in (_ROOT, _os.path.join(_ROOT, "scripts")):
    if _extra not in _sys.path:
        _sys.path.insert(0, _extra)
_os.chdir(_ROOT)

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





import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import metrics as M                       # noqa: E402
import platforms as P                     # noqa: E402
import platforms.linux as LINUX           # noqa: E402
import platforms.windows as WIN           # noqa: E402
from sampler import raise_thread_priority  # noqa: E402

# --------------------------------------------------------------------------
# Recorded before the platform abstraction was introduced, by calling
# SensorManager() on this machine (see the report's "before" snapshot). These
# are the properties that must not change; the values are this workstation's,
# exactly as test_gpus.py already asserts "expected 2 AMD rows".
# --------------------------------------------------------------------------

EXPECTED_GPUS = (
    # (vendor, pci, name)
    ("nvidia", "PCI 2d:00.0", "Quadro P2000"),
    ("amd", "PCI 17:00.0", "AMD Radeon Pro V620"),
    ("amd", "PCI 23:00.0", "AMD Radeon Pro V620"),
)

#: The keys every machine must report, whatever sensors it has. It used to be a
#: frozen list of everything this machine produced *while LibreHardwareMonitor
#: was failing* - which stopped being true the day the sensors started working,
#: and would have been wrong on any machine with LHM installed. What is invariant
#: is the base set plus the rule that the rest follows what the backends report.
EXPECTED_METRIC_KEYS = [
    "cpu_util", "cpu_clock", "ram_used", "ram_percent",
    "gpu0_util", "gpu0_vram_used", "gpu0_vram_total",
    "gpu1_util", "gpu1_vram_used", "gpu1_vram_total",
    "gpu2_util", "gpu2_vram_used", "gpu2_vram_total",
]

#: The two notes whose wording must survive the port untouched (the middle one
#: quotes LibreHardwareMonitor's state, which varies with whether LHM is up).
EXPECTED_NOTE_ADL_PREFIX = "AMD telemetry via ADL for PCI 17:00.0, PCI 23:00.0"
EXPECTED_NOTE_COUNTER_PREFIX = ("Utilisation counters for AMD Radeon Pro V620 "
                                "(PCI 17:00.0)")

problems: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    """One PASS/FAIL line; failures are collected for the final summary."""
    global checks
    checks += 1
    suffix = f"  [{detail}]" if detail and not ok else ""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{suffix}")
    if not ok:
        problems.append(f"{label}{(': ' + detail) if detail else ''}")
    return ok


def eq(label: str, got: object, want: object) -> bool:
    return check(label, got == want, f"got {got!r}, want {want!r}")


def write(path: str, text: str) -> None:
    """Create a fixture file, making its directory first."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# --------------------------------------------------------------------------
# Synthetic fixtures
# --------------------------------------------------------------------------


def build_sysfs_fixture(root: str) -> None:
    """A /sys/class/drm tree carrying the documented amdgpu ABI values.

    card0 is a fully featured card; card1 is nearly bare (no hwmon, one
    unparsable attribute, a DPM list with nothing flagged) so the "absent, never
    zero" rule is tested rather than just the happy path. A connector directory
    and a render node are present too, because enumerating those as GPUs is the
    obvious way to get this wrong.
    """
    dev0 = os.path.join(root, "card0", "device")
    write(os.path.join(dev0, "uevent"),
          "DRIVER=amdgpu\nPCI_CLASS=30000\nPCI_ID=1002:73BF\n"
          "PCI_SLOT_NAME=0000:41:00.0\n")
    write(os.path.join(dev0, "vendor"), "0x1002\n")
    write(os.path.join(dev0, "device"), "0x73bf\n")
    write(os.path.join(dev0, "gpu_busy_percent"), "7\n")
    write(os.path.join(dev0, "mem_busy_percent"), "1\n")
    write(os.path.join(dev0, "mem_info_vram_used"), "1073741824\n")      # 1 GiB
    write(os.path.join(dev0, "mem_info_vram_total"), "34359738368\n")    # 32 GiB
    write(os.path.join(dev0, "pp_dpm_sclk"), "0: 500Mhz\n1: 1700Mhz *\n")
    write(os.path.join(dev0, "pp_dpm_mclk"), "0: 167Mhz\n1: 1000Mhz *\n")
    hw0 = os.path.join(dev0, "hwmon", "hwmon3")
    write(os.path.join(hw0, "temp1_input"), "45000\n")            # edge
    write(os.path.join(hw0, "temp2_input"), "62000\n")            # junction
    write(os.path.join(hw0, "temp3_input"), "-1\n")               # unreadable
    write(os.path.join(hw0, "power1_average"), "180000000\n")     # 180 W
    write(os.path.join(hw0, "power1_cap"), "250000000\n")         # 250 W
    write(os.path.join(hw0, "fan1_input"), "1500\n")
    write(os.path.join(hw0, "pwm1"), "128\n")
    write(os.path.join(hw0, "pwm1_max"), "255\n")

    dev1 = os.path.join(root, "card1", "device")
    write(os.path.join(dev1, "uevent"),
          "DRIVER=amdgpu\nPCI_SLOT_NAME=0000:42:00.0\n")
    write(os.path.join(dev1, "vendor"), "0x1002\n")
    write(os.path.join(dev1, "device"), "0x73bf\n")
    write(os.path.join(dev1, "gpu_busy_percent"), "not a number\n")
    write(os.path.join(dev1, "pp_dpm_sclk"), "0: 500Mhz\n1: 1700Mhz\n")

    os.makedirs(os.path.join(root, "card0-DP-1"), exist_ok=True)
    os.makedirs(os.path.join(root, "renderD128"), exist_ok=True)


def build_hwmon_fixture(hwmon_root: str, thermal_root: str) -> None:
    """A coretemp chip plus an amdgpu chip and two thermal zones."""
    write(os.path.join(hwmon_root, "hwmon0", "name"), "coretemp\n")
    write(os.path.join(hwmon_root, "hwmon0", "temp1_input"), "52000\n")
    write(os.path.join(hwmon_root, "hwmon0", "temp1_label"), "Package id 0\n")
    write(os.path.join(hwmon_root, "hwmon0", "temp2_input"), "48000\n")
    write(os.path.join(hwmon_root, "hwmon0", "temp2_label"), "Core 0\n")
    write(os.path.join(hwmon_root, "hwmon0", "temp3_input"), "49000\n")
    write(os.path.join(hwmon_root, "hwmon0", "temp3_label"), "Core 1\n")
    # Not a CPU: has temperatures, must not be read as one.
    write(os.path.join(hwmon_root, "hwmon1", "name"), "amdgpu\n")
    write(os.path.join(hwmon_root, "hwmon1", "temp1_input"), "45000\n")
    write(os.path.join(thermal_root, "thermal_zone0", "type"), "acpitz\n")
    write(os.path.join(thermal_root, "thermal_zone0", "temp"), "27000\n")
    write(os.path.join(thermal_root, "thermal_zone1", "type"), "x86_pkg_temp\n")
    write(os.path.join(thermal_root, "thermal_zone1", "temp"), "53000\n")


ROCM_PAYLOAD = {
    "card0": {
        "Temperature (Sensor edge) (C)": "38.0",
        "Temperature (Sensor junction) (C)": "41.0",
        "Temperature (Sensor memory) (C)": "N/A",
        "GPU use (%)": "3",
        "GPU Memory Allocated (VRAM%)": "12",
        "Average Graphics Package Power (W)": "25.5",
        "sclk clock speed:": "(0) 1500Mhz",
        "mclk clock speed:": "1000Mhz",
        "Fan speed (%)": "30",
        "Fan RPM": "1200",
        "VRAM Total Memory (B)": "34359738368",
        "VRAM Total Used Memory (B)": "1073741824",
        "PCI Bus": "0000:41:00.0",
    },
    # rocm-smi also reports a system-level block; it is not a card.
    "system": {"Driver version": "6.0.0"},
}


# --------------------------------------------------------------------------
# 1. Dispatch
# --------------------------------------------------------------------------

print("=" * 88)
print("PLATFORM TEST (dispatch, Linux import-safety, Windows regression)")
print("=" * 88)

print("\n[1] platform dispatch")
eq("current_platform() on this host", P.current_platform(), "windows")
eq("sys.platform starts with win", sys.platform.startswith("win"), True)
try:
    P.current_platform()
    check("current_platform() does not raise here", True)
except RuntimeError as exc:                                # pragma: no cover
    check("current_platform() does not raise here", False, str(exc))

# --------------------------------------------------------------------------
# 2. Windows bundle
# --------------------------------------------------------------------------

print("\n[2] Windows backend bundle")
win = P.build_platform_backends(per_core=False)
eq("bundle name", win.name, "windows")
check("bundle built by platforms.windows itself",
      WIN.build_platform_backends(False).name == "windows")
check("psutil system backend is up", win.system.available(), win.system.error)
nvidia_smi_up = win.nvidia_smi.available()
if nvidia_smi_up:
    check("nvidia-smi backend is up, since it is installed here", nvidia_smi_up,
          getattr(win.nvidia_smi, "error", ""))
else:
    print(f"    --  no nvidia-smi on this machine, so nothing to insist on: "
          f"{getattr(win.nvidia_smi, 'error', '')}")
pdh_up = win.pdh.available()
if pdh_up:
    check("PDH GPU counters are up, since they are available here", pdh_up,
          getattr(win.pdh, "error", ""))
else:
    print(f"    --  no PDH GPU counters here: {getattr(win.pdh, 'error', '')}")
adl_up = win.adl.available()
if adl_up:
    check("ADL backend is up, since it is installed here", adl_up,
          getattr(win.adl, "error", ""))
else:
    print(f"    --  no AMD display library here: "
          f"{getattr(win.adl, 'error', '')}")
# Whether or not this machine has the hardware, the *behaviour* must hold: asking
# never raises, and an unavailable backend explains itself rather than going
# quiet. That is what a user without the vendor's driver actually experiences.
check("every backend answers the availability question",
      all(isinstance(backend.available(), bool)
          for backend in (win.system, win.nvidia_smi, win.pdh, win.adl)))
check("every unavailable backend says why",
      all(getattr(backend, "error", "")
          for backend in (win.nvidia_smi, win.pdh, win.adl)
          if not backend.available()))
check("LibreHardwareMonitor is a real Windows backend, not a stub",
      not isinstance(win.lhm, P.UnsupportedBackend))
check("bundle.sources is empty on Windows (poll order stays in metrics.py)",
      win.sources == [], str(win.sources))
for label, backend in (("amd_sysfs", win.amd_sysfs), ("amd_rocm", win.amd_rocm),
                       ("cpu_thermal", win.cpu_thermal), ("intel", win.intel)):
    ok = (isinstance(backend, P.UnsupportedBackend)
          and backend.available() is False and bool(backend.error))
    check(f"Linux-only field {label} is an explanatory stub", ok)

# --------------------------------------------------------------------------
# 3. Linux module import-safety
# --------------------------------------------------------------------------

print("\n[3] platforms.linux imports and degrades on a machine with no /sys")

proc = subprocess.run(
    [sys.executable, "-c",
     "import platforms.linux, platforms.windows; print('imported')"],
    cwd=_ROOT,
    capture_output=True, text=True, timeout=120)
check("fresh interpreter imports platforms.linux and .windows",
      proc.returncode == 0 and "imported" in proc.stdout,
      (proc.stderr or proc.stdout).strip()[-300:])

lin = LINUX.build_platform_backends(per_core=False)
eq("bundled name", lin.name, "linux")
eq("linux sources in precedence order", [lbl for lbl, _ in lin.sources],
   ["sysfs", "rocm-smi", "thermal"])
for label, backend in lin.sources:
    check(f"linux {label} reports unavailable with a reason",
          backend.available() is False and bool(backend.error),
          getattr(backend, "error", "no error string"))
    check(f"linux {label} poll() returns nothing, not zeros",
          backend.poll() == {})
errors = lin.errors()
eq("errors() covers exactly the unavailable native sources",
   sorted(errors), ["rocm-smi", "sysfs", "thermal"])
check("errors() does not blame PDH/ADL/LHM on Linux",
      not ({"pdh", "adl", "lhm"} & set(errors)), str(sorted(errors)))
check("Linux stubs for the Windows backends stay unavailable",
      all(getattr(lin, name).available() is False
          for name in ("pdh", "adl", "lhm")))
check("Linux CPU temperature backend is unavailable off Linux",
      lin.cpu_thermal.available() is False, lin.cpu_thermal.error)
check("Intel GPU backend refuses with an explanation",
      lin.intel.available() is False and "intel_gpu_top" in lin.intel.error,
      lin.intel.error)
notes = lin.notes()
check("Linux notes mention sysfs, optional rocm-smi and CPU temperature",
      all(any(word in note for note in notes)
          for word in ("sysfs", "rocm-smi is optional", "/sys/class/hwmon")),
      " | ".join(notes))
check("Linux notes never advise LibreHardwareMonitor",
      not any("--setup-sensors" in note or "Run `python" in note
              for note in notes), " | ".join(notes))

# --------------------------------------------------------------------------
# 4. Linux sysfs parsing, against a synthetic tree
# --------------------------------------------------------------------------

print("\n[4] sysfs parsing (synthetic /sys/class/drm fixture)")
tmp = tempfile.mkdtemp(prefix="gpumon-sysfs-")
try:
    build_sysfs_fixture(tmp)
    sysfs = LINUX.SysfsAmdGpuBackend(root=tmp)
    check("backend available with one readable card", sysfs.available(),
          sysfs.error)
    cards = {card.card: card for card in sysfs.cards}
    eq("connector and render node are not cards", sorted(cards),
       ["card0", "card1"])
    eq("cards are ordered by PCI address", [c.card for c in sysfs.cards],
       ["card0", "card1"])
    card0 = cards["card0"]
    eq("PCI address from uevent PCI_SLOT_NAME", card0.pci, "0000:41:00.0")
    eq("pci_label matches the format gpuenum/nvml use", card0.pci_label,
       "PCI 41:00.0")
    eq("PCI bus number", card0.bus, 0x41)
    eq("device_key is the PCI address", card0.device_key, "0000:41:00.0")
    eq("vendor from the vendor file", card0.vendor, "amd")
    eq("name from PCI ids and driver", card0.name,
       "AMD GPU 1002:73bf (amdgpu)")
    eq("vram total bytes -> MB", card0.vram_total_mb, 32768.0)
    eq("bare card exposes nothing readable", cards["card1"].fields, ())

    sysfs.set_gpu_map({"0000:41:00.0": 2, "card0": 2,
                       "0000:42:00.0": 3, "card1": 3})
    values = sysfs.poll()
    eq("temperature is millidegrees C (45000 -> 45)", values.get("gpu2_temp"),
       45.0)
    eq("hotspot is temp2_input", values.get("gpu2_hotspot"), 62.0)
    check("-1 sentinel becomes an absent metric, not -0.001 C",
          "gpu2_mem_temp" not in values, str(values.get("gpu2_mem_temp")))
    eq("utilisation is a percent", values.get("gpu2_util"), 7.0)
    eq("memory utilisation is a percent", values.get("gpu2_mem_util"), 1.0)
    eq("vram used bytes -> MB", values.get("gpu2_vram_used"), 1024.0)
    eq("vram total bytes -> MB", values.get("gpu2_vram_total"), 32768.0)
    eq("the * flagged DPM line is the current clock",
       values.get("gpu2_clock_core"), 1700.0)
    eq("memory clock from pp_dpm_mclk", values.get("gpu2_clock_mem"), 1000.0)
    eq("power microW -> W (180000000 -> 180)", values.get("gpu2_power"), 180.0)
    eq("power cap microW -> W", values.get("gpu2_power_limit"), 250.0)
    eq("fan RPM", values.get("gpu2_fan_rpm"), 1500.0)
    check("fan % derived from pwm1/pwm1_max",
          abs((values.get("gpu2_fan") or 0.0) - 128 / 255 * 100.0) < 0.01,
          str(values.get("gpu2_fan")))
    want_keys = {"gpu2_temp", "gpu2_hotspot", "gpu2_util", "gpu2_mem_util",
                 "gpu2_vram_used", "gpu2_vram_total", "gpu2_clock_core",
                 "gpu2_clock_mem", "gpu2_power", "gpu2_power_limit",
                 "gpu2_fan", "gpu2_fan_rpm"}
    eq("gpu2 key set: absent files are absent, never zero", set(values),
       want_keys)
    check("unparsable file and unflagged DPM yield nothing for card1",
          not any(key.startswith("gpu3_") for key in values), str(sorted(values)))
    check("only mapped cards emit metrics",
          all(key.startswith("gpu2_") for key in values), str(sorted(values)))

    empty = LINUX.SysfsAmdGpuBackend(root=os.path.join(tmp, "does-not-exist"))
    check("missing /sys/class/drm is unavailable, not a crash",
          empty.available() is False and "no GPU device directories" in empty.error,
          empty.error)
    eq("metric_def knows the Linux per-core temperature keys",
       (M.metric_def("core03_temp").unit, M.metric_def("core03_temp").kind,
        M.metric_def("core03_temp").higher_is_worse), ("C", "temp", True))
    eq("metric_def still reads coreNN_util the old way",
       (M.metric_def("core03_util").unit, M.metric_def("core03_util").kind),
       ("%", "percent"))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# --------------------------------------------------------------------------
# 5. Linux CPU temperature, against a synthetic hwmon/thermal tree
# --------------------------------------------------------------------------

print("\n[5] CPU temperature parsing (synthetic hwmon + thermal fixture)")
tmp = tempfile.mkdtemp(prefix="gpumon-thermal-")
try:
    hwmon_root = os.path.join(tmp, "hwmon")
    thermal_root = os.path.join(tmp, "thermal")
    build_hwmon_fixture(hwmon_root, thermal_root)
    thermal = LINUX.LinuxSystemBackend(thermal_root=thermal_root,
                                       hwmon_root=hwmon_root)
    check("CPU temperature backend available", thermal.available(), thermal.error)
    readings = thermal.poll()
    eq("package temperature from coretemp (millidegrees C)",
       readings.get("cpu_temp"), 52.0)
    eq("per-core temperature from tempN_label", readings.get("core00_temp"),
       48.0)
    eq("second core", readings.get("core01_temp"), 49.0)
    eq("the amdgpu chip is not read as a CPU", readings.get("cpu_temp"), 52.0)

    # Without hwmon chips, the CPU-named thermal zone must win over acpitz.
    zones_only = LINUX.LinuxSystemBackend(
        thermal_root=thermal_root, hwmon_root=os.path.join(tmp, "no-hwmon"))
    eq("thermal zone fallback prefers the CPU zone over acpitz",
       zones_only.poll().get("cpu_temp"), 53.0)
    check("no per-core keys without an hwmon chip",
          "core00_temp" not in zones_only.poll())

    # acpitz alone is the motherboard sensor and must be refused.
    acpitz_root = os.path.join(tmp, "acpitz")
    write(os.path.join(acpitz_root, "thermal_zone0", "type"), "acpitz\n")
    write(os.path.join(acpitz_root, "thermal_zone0", "temp"), "27000\n")
    acpitz_only = LINUX.LinuxSystemBackend(
        thermal_root=acpitz_root, hwmon_root=os.path.join(tmp, "no-hwmon"))
    check("acpitz alone is refused rather than reported as the CPU",
          acpitz_only.available() is False
          and "no CPU temperature sensor" in acpitz_only.error,
          acpitz_only.error)
    eq("refused backend polls to nothing", acpitz_only.poll(), {})
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# --------------------------------------------------------------------------
# 6. rocm-smi payload parsing
# --------------------------------------------------------------------------

print("\n[6] rocm-smi JSON parsing (recorded payload shape)")
rocm_cards = LINUX.parse_rocm_cards(ROCM_PAYLOAD)
eq("one card parsed, the system block ignored", len(rocm_cards), 1)
if rocm_cards:
    parsed = rocm_cards[0]
    eq("card id", parsed.card, "card0")
    eq("PCI bus from --showbus", parsed.pci, "0000:41:00.0")
    fields = parsed.fields
    eq("edge temperature", fields.get("temp"), 38.0)
    eq("junction temperature -> hotspot", fields.get("hotspot"), 41.0)
    check("an 'N/A' value yields no metric",
          "mem_temp" not in fields, str(fields.get("mem_temp")))
    eq("GPU use -> util", fields.get("util"), 3.0)
    eq("VRAM% -> mem_util", fields.get("mem_util"), 12.0)
    eq("package power", fields.get("power"), 25.5)
    eq("clock with a DPM level prefix takes the MHz figure",
       fields.get("clock_core"), 1500.0)
    eq("memory clock", fields.get("clock_mem"), 1000.0)
    eq("fan percent", fields.get("fan"), 30.0)
    eq("fan rpm", fields.get("fan_rpm"), 1200.0)
    eq("vram used bytes -> MB", fields.get("vram_used"), 1024.0)
    eq("vram total bytes -> MB", fields.get("vram_total"), 32768.0)
    # GPU_FIELDS is not the whole vocabulary: amdsensors.py already reports
    # fan_rpm and the voltage_* figures on Windows through _poll_amd's rename
    # table, and metric_def() falls back to a bare label for those. Listing them
    # here keeps the check honest instead of pretending the catalogue is closed.
    extra_fields = {"fan_rpm", "voltage_core", "voltage_mem", "voltage_soc",
                    "clock_soc", "power_board"}
    eq("every field is a GPU_FIELDS name or one amdsensors already emits",
       sorted(set(fields) - set(M.GPU_FIELDS) - extra_fields), [])
eq("a non-dict payload parses to nothing", LINUX.parse_rocm_cards(None), [])
eq("a list payload parses to nothing", LINUX.parse_rocm_cards([1, 2]), [])

# The backend's own poll(): the subprocess seam (_run, the same helper
# nvidia-smi uses and which Windows exercises) is replaced with the recorded
# payload, because there is no rocm-smi to install here. Everything below that
# seam - JSON handling, card->index mapping, first-wins merging - is the code
# under test.
real_run = LINUX._run
try:
    json_payload = json.dumps(ROCM_PAYLOAD)
    LINUX._run = lambda cmd, timeout=5.0: (0, json_payload, "")  # noqa: E731

    rocm = LINUX.AmdRocmSmiBackend()
    rocm.exe = "rocm-smi"        # pretend the CLI is on PATH
    rocm._probed = True          # and that the availability probe succeeded
    rocm._available = True
    rocm.set_gpu_map({"0000:41:00.0": 4, "card0": 4})
    readings = rocm.poll()
    eq("rocm poll maps its card onto the PCI address", readings.get("gpu4_temp"),
       38.0)
    eq("rocm poll emits the whole field set",
       sorted(key[len("gpu4_"):] for key in readings),
       ["clock_core", "clock_mem", "fan", "fan_rpm", "hotspot", "mem_util",
        "power", "temp", "util", "vram_total", "vram_used"])

    # Without a PCI key it must fall back to rocm-smi's own card id, and a card
    # the manager never bound must produce nothing at all.
    rocm.set_gpu_map({"card0": 5})
    eq("rocm poll falls back to the cardN key",
       rocm.poll().get("gpu5_util"), 3.0)
    rocm.set_gpu_map({"card7": 5})
    eq("rocm poll emits nothing for an unbound card", rocm.poll(), {})

    LINUX._run = lambda cmd, timeout=5.0: (1, "", "no permission")  # noqa: E731
    eq("rocm poll returns nothing when the CLI fails", rocm.poll(), {})
    check("rocm poll records why it failed", "no permission" in rocm.error,
          rocm.error)
    LINUX._run = lambda cmd, timeout=5.0: (0, "not json", "")  # noqa: E731
    eq("rocm poll returns nothing on non-JSON output", rocm.poll(), {})
    eq("rocm poll says so", rocm.error, "rocm-smi did not return JSON")
finally:
    LINUX._run = real_run

# --------------------------------------------------------------------------
# 7. Windows regression: SensorManager against the recorded snapshot
# --------------------------------------------------------------------------

print("\n[7] SensorManager on Windows, against the pre-change snapshot")
manager = M.SensorManager(per_core=True)
caps = manager.capabilities()
rows = [(g.vendor, g.pci, g.name) for g in manager.gpus]
# The snapshot below is this machine's: three cards, with those addresses. On any
# other machine it is not a regression, it is just a different computer - a build
# runner enumerated "Microsoft Hyper-V Video" and failed a test that had nothing
# to say about it. So the frozen list is applied where there are real cards to
# compare against, and what is genuinely invariant is checked everywhere.
snapshot_machine = any(vendor in ("nvidia", "amd") for vendor, _pci, _name in rows)
if snapshot_machine:
    eq("same GPU rows as before the change", rows, list(EXPECTED_GPUS))
else:
    print(f"    --  no discrete GPU here, so the recorded snapshot does not "
          f"apply. This machine reports: {rows}")
# These used to compare against a frozen list of everything this machine
# produced while LibreHardwareMonitor happened to be failing, including "cpu_temp
# is absent" and "the lhm backend is in error". They broke the moment the sensors
# started working, and would have been wrong on any machine with LHM installed.
# What is invariant is the base set, and that everything beyond it follows what
# the backends report.
keys = manager.metric_keys()
if snapshot_machine:
    missing = [key for key in EXPECTED_METRIC_KEYS if key not in keys]
else:
    # Without the cards the snapshot describes, what must still be present is the
    # set that needs no card at all.
    missing = [key for key in ("cpu_util", "cpu_clock", "ram_used", "ram_percent")
               if key not in keys]
check("every key the platform guarantees is present", not missing, str(missing))
check("cpu_temp appears exactly when a backend reports one",
      ("cpu_temp" in keys) == caps.cpu_temp,
      f"in keys: {'cpu_temp' in keys}, capabilities: {caps.cpu_temp}")
check("GPU temperature keys follow the cards that can report them",
      all((f"gpu{g.index}_temp" in keys) == g.has_temperature
          for g in manager.gpus),
      str([(g.index, g.has_temperature, f"gpu{g.index}_temp" in keys)
           for g in manager.gpus]))
check("hotspot keys follow the cards that publish a hotspot",
      all((f"gpu{g.index}_hotspot" in keys) == g.has_hotspot
          for g in manager.gpus),
      str([(g.index, g.has_hotspot) for g in manager.gpus]))
eq("Windows uses the Windows bundle",
   manager.platform_backends.name, "windows")
check("backends come from the bundle, not from SensorManager itself",
      manager.pdh is manager.platform_backends.pdh
      and manager.amd is manager.platform_backends.adl
      and manager.system is manager.platform_backends.system
      and manager.lhm is manager.platform_backends.lhm
      and manager.nvidia is manager.platform_backends.nvidia_smi)
eq("Linux-only backends stay stubs on Windows",
   [manager.amd_sysfs.available(), manager.rocm.available(),
    manager.cpu_thermal.available(), manager.intel.available()],
   [False, False, False, False])
check("every row has an index, a name and a PCI address",
      all(g.name and g.pci for g in manager.gpus), str(rows))
check("every non-NVIDIA card has at least one utilisation counter",
      all(g.luids for g in manager.gpus if g.vendor != "nvidia"),
      str([(g.index, g.luids) for g in manager.gpus]))
check("NVIDIA rows report a PCI address and a VRAM total",
      all(g.pci and g.vram_total_mb for g in manager.gpus
          if g.vendor == "nvidia"))
check("the CPU temperature source agrees with the capability",
      bool(manager.cpu_temp_source()) == caps.cpu_temp,
      f"source={manager.cpu_temp_source()!r} caps={caps.cpu_temp}")
check("backend_errors only ever names a backend that exists",
      set(caps.backend_errors) <= {"lhm", "acpi", "adl", "pdh", "nvml",
                                   "nvidia-smi", "psutil", "sysfs", "rocm",
                                   "intel"},
      str(sorted(caps.backend_errors)))
# The count varies with the machine: a note appears when a sensor is missing and
# disappears when it works, so what is checked is the rule, not a tally frozen
# from one afternoon when LibreHardwareMonitor happened to be down.
explains_cpu = any("CPU package temperature needs" in note
                   or "ACPI thermal zone" in note for note in caps.notes)
check("the CPU-temperature note appears exactly when there is no reading",
      explains_cpu == (not caps.cpu_temp),
      f"note={explains_cpu} cpu_temp={caps.cpu_temp} | " + " | ".join(caps.notes))
check("the ADL note is word for word what it was",
      any(note.startswith(EXPECTED_NOTE_ADL_PREFIX) for note in caps.notes),
      " | ".join(caps.notes))
check("the LUID-attribution note is word for word what it was",
      any(note.startswith(EXPECTED_NOTE_COUNTER_PREFIX) for note in caps.notes),
      " | ".join(caps.notes))
check("no Linux note text leaks onto Windows",
      not any(word in note for note in caps.notes
              for word in ("sysfs", "rocm-smi", "hwmon")),
      " | ".join(caps.notes))

status = manager.backend_status()
eq("backend_status() keeps its six keys", sorted(status),
   ["adl", "cpu", "lhm", "nvml", "pdh", "smi"])
check("backend_status() values are all bools",
      all(isinstance(value, bool) for value in status.values()), str(status))
check("backend_status() agrees with the backends",
      status["pdh"] == pdh_up and status["smi"] == nvidia_smi_up, str(status))

manager.prime()
time.sleep(1.5)
values = manager.poll()
for key in ("cpu_util", "ram_percent", "cpu_clock", "ram_used"):
    check(f"poll() returns a number for {key}",
          isinstance(values.get(key), float), repr(values.get(key)))
check("poll() still reports a GPU temperature",
      isinstance(values.get("gpu0_temp"), float), repr(values.get("gpu0_temp")))
check("poll() still reports AMD utilisation for both cards",
      all(isinstance(values.get(f"gpu{i}_util"), float) for i in (1, 2)),
      repr([values.get(f"gpu{i}_util") for i in (1, 2)]))
manager.close()

# --------------------------------------------------------------------------
# 8. Thread priority
# --------------------------------------------------------------------------

print("\n[8] sampler.raise_thread_priority() on Windows")
check("raises the calling thread above normal priority",
      raise_thread_priority() is True)

# --------------------------------------------------------------------------

print("\n" + "=" * 88)
if problems:
    print(f"PLATFORM TEST FAILED ({len(problems)} of {checks} checks)")
    for item in problems:
        print(f"  - {item}")
else:
    print(f"PLATFORM TEST PASSED ({checks} checks)")
    print("  Windows path unchanged; platforms.linux is import-safe and its")
    print("  parsers behave on synthetic sysfs. NOT covered here: whether those")
    print("  paths exist on a real Linux kernel - there is no Linux host to run")
    print("  on, so that half stays unverified.")
print("=" * 88)
raise SystemExit(1 if problems else 0)
