"""Linux backends: amdgpu sysfs, optional rocm-smi, and CPU temperature.

Units come straight from the kernel ABI and are the easiest thing to get wrong
here, so they are recorded once, in one place:

==============================  ============================  ===============
sysfs attribute                 unit                          gpumon field
==============================  ============================  ===============
hwmon/hwmonN/temp1_input        millidegrees C                temp (edge)
hwmon/hwmonN/temp2_input        millidegrees C                hotspot
hwmon/hwmonN/temp3_input        millidegrees C                mem_temp
gpu_busy_percent                percent (0-100)               util
mem_busy_percent                percent (0-100)               mem_util
mem_info_vram_used              bytes                         vram_used (MB)
mem_info_vram_total             bytes                         vram_total (MB)
pp_dpm_sclk / pp_dpm_mclk       MHz on the line marked `*`    clock_core/mem
hwmon/hwmonN/power1_average     microwatts                    power (W)
hwmon/hwmonN/power1_cap         microwatts                    power_limit (W)
hwmon/hwmonN/fan1_input         RPM                           fan_rpm
hwmon/hwmonN/pwm1 vs pwm1_max   0..pwm1_max (255 on every      fan (%)
                                board seen, *not* 0..100)
==============================  ============================  ===============

`temp1/2/3` follow amdgpu's own sensor order (edge, junction, memory) and the
hwmon files follow the generic hwmon ABI; `power1_input` is accepted as a
fallback for `power1_average`, which only exists on drivers new enough to
expose the averaged register.

Every one of those files is optional - which ones exist depends on the driver,
the kernel version and the board - so each read is guarded and a missing file
yields an *absent* metric, never 0.0. That is the rule metrics.py opens with: a
fake zero reads as "idle". Two traps are worth naming:

* amdgpu writes **-1** into a sensor attribute it cannot read (and
  `gpu_busy_percent` reads -1 while the GPU is in reset). -1 is dropped rather
  than charted as "-1 %".
* `pp_dpm_*` lists every DPM state, one per line, with the active one flagged
  (`0: 500Mhz`, `1: 1700Mhz *`). Using the maximum would report the boost clock
  as the current clock, so only the flagged line is used, and an unflagged list
  yields nothing at all.

None of this has ever been executed on a Linux host: this workstation is
Windows. The mitigation is that every parser is total - garbage, empty, missing
and -1 all produce "absent" - and that the parsing runs against a synthetic
sysfs tree in test_platforms.py, which is the strongest check available here.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any

import metrics as M
from metrics import _run          # the shared subprocess helper (no console)
from platforms import PlatformBackends, UnsupportedBackend

#: /sys/class/drm holds one `cardN` directory per DRM device, one entry per
#: connector (`card0-DP-1`) and one `renderD*` node; only `cardN` is a GPU.
DRM_ROOT = "/sys/class/drm"
THERMAL_ROOT = "/sys/class/thermal"
HWMON_ROOT = "/sys/class/hwmon"

#: hwmon chip names that are a CPU: coretemp (Intel), k10temp/zenpower (AMD),
#: cpu_thermal (ARM/SoC).
CPU_HWMON_CHIPS = ("coretemp", "k10temp", "zenpower", "cpu_thermal",
                   "cpu-thermal")

#: thermal zone types that name a CPU package. `acpitz` is deliberately absent:
#: it is the motherboard sensor and sits at a constant temperature on most
#: desktops, so charting it as the CPU would be worse than charting nothing.
CPU_ZONE_HINTS = ("pkg", "cpu", "soc_thermal", "tctl", "tdie")

_VENDOR_NAMES = {0x1002: "amd", 0x1022: "amd", 0x10DE: "nvidia",
                 0x8086: "intel", 0x1AE0: "google", 0x5143: "qualcomm"}
_VENDOR_LABELS = {"amd": "AMD", "nvidia": "NVIDIA", "intel": "Intel"}

_DPM_RE = re.compile(r"(\d+):\s*([0-9.]+)\s*mhz", re.IGNORECASE)
_TEMP_ATTR_RE = re.compile(r"^temp(\d+)_input$")
_CORE_LABEL_RE = re.compile(r"core\s*(\d+)", re.IGNORECASE)
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


# ==========================================================================
# sysfs readers
# ==========================================================================


def _read_text(path: str | None) -> str | None:
    """File contents stripped, or None when missing or unreadable."""
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _read_int(path: str | None) -> float | None:
    """One sysfs integer, dropping amdgpu's -1 "sensor unreadable" sentinel.

    Strict on purpose: a file that is missing, empty, or holds anything that is
    not a number yields None, which callers turn into an absent metric.
    """
    text = _read_text(path)
    if not text:
        return None
    try:
        value = float(text.split()[0])
    except (ValueError, IndexError):
        return None
    return None if value == -1.0 else value


def _read_hex(path: str | None) -> int | None:
    """`vendor` / `device` files print their id as 0x1002."""
    text = _read_text(path)
    if not text:
        return None
    try:
        return int(text.split()[0], 16)
    except (ValueError, IndexError):
        return None


def _milli_celsius(path: str | None) -> float | None:
    """millidegrees C -> C, with a sanity window.

    The window is not decoration: if a driver ever published a temperature in
    degrees instead of millidegrees, an unchecked read would chart 45000 C for
    a 45 C card. An out-of-window value is treated as unreadable instead.
    """
    milli = _read_int(path)
    if milli is None:
        return None
    celsius = milli / 1000.0
    return celsius if -50.0 <= celsius <= 200.0 else None


def _microwatts(path: str | None) -> float | None:
    """microwatts -> W. Zero is "no reading": a live GPU never draws 0 W."""
    micro = _read_int(path)
    if micro is None or micro <= 0:
        return None
    watts = micro / 1_000_000.0
    return watts if watts <= 2000.0 else None


def _bytes_to_mb(value: float | None) -> float | None:
    """VRAM totals/usage are bytes in sysfs; every other VRAM figure in gpumon
    is MB (nvml.py and psutil's RAM figures do the same 1024*1024 division)."""
    if value is None or value < 0:
        return None
    return value / (1024 * 1024)


def _percent(path: str | None) -> float | None:
    value = _read_int(path)
    if value is None or not 0.0 <= value <= 100.0:
        return None
    return value


def _dpm_active_mhz(path: str | None) -> float | None:
    """The active DPM state's clock in MHz (`pp_dpm_sclk` / `pp_dpm_mclk`).

    amdgpu prints one state per line and flags the active one, e.g.
    `0: 500Mhz`, `1: 1700Mhz *`. If nothing is flagged the reading is dropped:
    the only alternative is the highest state, which would report the boost
    clock as the current clock - a plausible-looking wrong number is worse than
    an absent one.
    """
    text = _read_text(path)
    if not text:
        return None
    for line in text.splitlines():
        if "*" not in line:
            continue
        match = _DPM_RE.search(line)
        if match:
            try:
                return float(match.group(2))
            except ValueError:
                return None
    return None


def _read_uevent(path: str) -> dict[str, str]:
    """`KEY=VALUE` lines from a device's `uevent` file."""
    text = _read_text(path)
    if not text:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _pci_parts(pci: str) -> tuple[int | None, int, int]:
    """`0000:41:00.0` -> (0x41, 0x00, 0x0). The domain is dropped, as on
    Windows, because gpumon's labels are bus:device.function everywhere."""
    if not pci:
        return None, 0, 0
    tail = pci.split(":", 1)[1] if pci.count(":") == 2 else pci
    try:
        bus_text, rest = tail.split(":", 1)
        device_text, function_text = rest.split(".", 1)
        return int(bus_text, 16), int(device_text, 16), int(function_text, 16)
    except (ValueError, IndexError):
        return None, 0, 0


# ==========================================================================
# Card discovery
# ==========================================================================


@dataclass
class SysfsCard:
    """One `/sys/class/drm/cardN/device` directory - a GPU, not a connector."""

    card: str                    # "card0"
    path: str                    # /sys/class/drm/card0/device
    pci: str                     # "0000:41:00.0" (uevent PCI_SLOT_NAME)
    vendor: str                  # amd | nvidia | intel | unknown
    vendor_id: int
    device_id: int
    driver: str                  # "amdgpu", from uevent DRIVER
    name: str
    hwmon: str                   # the card's hwmon directory, "" when absent
    vram_total_mb: float | None
    #: GPU_FIELDS suffixes this card was seen to expose, filled at probe time.
    fields: tuple[str, ...] = ()

    @property
    def pci_label(self) -> str:
        """The `PCI bb:dd.f` label the rest of gpumon uses (gpuenum.py and
        nvml.py both build the same string)."""
        bus, device, function = _pci_parts(self.pci)
        if bus is None:
            return "unknown PCI location"
        return f"PCI {bus:02x}:{device:02x}.{function}"

    @property
    def bus(self) -> int | None:
        return _pci_parts(self.pci)[0]

    @property
    def device_key(self) -> str:
        """Stable identity for the calibration file: the PCI address survives
        reboots and driver reloads, `cardN` does not."""
        return self.pci or self.card

    @property
    def display_name(self) -> str:
        return f"{self.name} ({self.pci_label})" if self.pci else self.name


def _is_card_entry(name: str) -> bool:
    """True for `card0` / `card12`, false for `card0-DP-1` and `renderD128`."""
    return name.startswith("card") and name[4:].isdigit()


def _first_hwmon(device_dir: str) -> str:
    """The card's hwmon directory (`device/hwmon/hwmon3`), "" when absent.

    amdgpu registers exactly one chip, so the first match is the card's own.
    """
    parent = os.path.join(device_dir, "hwmon")
    try:
        entries = sorted(n for n in os.listdir(parent) if n.startswith("hwmon"))
    except OSError:
        return ""
    return os.path.join(parent, entries[0]) if entries else ""


def _card_name(vendor: str, vendor_id: int, device_id: int,
               driver: str) -> str:
    """A name built from what sysfs actually carries.

    sysfs has no marketing name for a GPU (there is no `product_name` file; the
    human-readable string only exists in lspci's database), so the vendor, the
    PCI ids and the driver are the honest answer - and the driver is worth
    including, because it is what tells identical PCI ids apart on a box with
    both the open and the proprietary module loaded.
    """
    label = _VENDOR_LABELS.get(vendor)
    if label and vendor_id:
        name = f"{label} GPU {vendor_id:04x}:{device_id:04x}"
    elif vendor_id:
        name = f"PCI device {vendor_id:04x}:{device_id:04x}"
    else:
        name = "Unknown display adapter"
    return f"{name} ({driver})" if driver else name


def _describe_card(entry: str, device_dir: str) -> SysfsCard:
    """Read one card directory's static identity."""
    uevent = _read_uevent(os.path.join(device_dir, "uevent"))
    vendor_id = _read_hex(os.path.join(device_dir, "vendor")) or 0
    device_id = _read_hex(os.path.join(device_dir, "device")) or 0
    driver = uevent.get("DRIVER", "")
    vendor = _VENDOR_NAMES.get(vendor_id, "unknown")
    # `PCI_SLOT_NAME` is the card's own address. A platform/SoC GPU has no PCI
    # address at all, which is why every consumer treats "" as "unknown".
    pci = uevent.get("PCI_SLOT_NAME", "").lower()
    return SysfsCard(
        card=entry, path=device_dir, pci=pci, vendor=vendor,
        vendor_id=vendor_id, device_id=device_id, driver=driver,
        name=_card_name(vendor, vendor_id, device_id, driver),
        hwmon=_first_hwmon(device_dir),
        vram_total_mb=_bytes_to_mb(
            _read_int(os.path.join(device_dir, "mem_info_vram_total"))))


def enumerate_cards(root: str = DRM_ROOT) -> list[SysfsCard]:
    """Every GPU under /sys/class/drm, in a stable order.

    Ordered by PCI address rather than by `cardN`: the kernel numbers cards in
    probe order, so `card0` today can be `card1` after a reboot or a module
    reload, and a row order that changes between runs makes logged sessions
    incomparable. Cards without a PCI address sort last, by name.
    """
    cards: list[SysfsCard] = []
    if not os.path.isdir(root):
        return cards
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return cards
    for entry in entries:
        if not _is_card_entry(entry):
            continue
        device_dir = os.path.join(root, entry, "device")
        if not os.path.isdir(device_dir):
            continue
        cards.append(_describe_card(entry, device_dir))
    cards.sort(key=lambda c: (c.pci or "~", c.card))
    return cards


def read_card(card: SysfsCard) -> dict[str, float]:
    """Every metric this card exposes right now, keyed by GPU_FIELDS suffix.

    Keys are field names ("temp", "util", ...) rather than full metric keys:
    which `gpuN` they belong to is the manager's numbering, and the backend only
    learns that from `set_gpu_map`. Values that are absent stay absent.
    """
    device_dir = card.path
    hwmon_dir = card.hwmon

    def hwmon_file(name: str) -> str | None:
        return os.path.join(hwmon_dir, name) if hwmon_dir else None

    out: dict[str, float] = {}

    # Temperatures: temp1=edge, temp2=junction/hotspot, temp3=memory. amdgpu
    # exposes temp1 on every card, the other two only where the board has the
    # sensor, so each is optional.
    for suffix, attribute in (("temp", "temp1_input"),
                              ("hotspot", "temp2_input"),
                              ("mem_temp", "temp3_input")):
        value = _milli_celsius(hwmon_file(attribute))
        if value is not None:
            out[suffix] = value

    for suffix, attribute in (("util", "gpu_busy_percent"),
                              ("mem_util", "mem_busy_percent")):
        value = _percent(os.path.join(device_dir, attribute))
        if value is not None:
            out[suffix] = value

    for suffix, attribute in (("vram_used", "mem_info_vram_used"),
                              ("vram_total", "mem_info_vram_total")):
        value = _bytes_to_mb(_read_int(os.path.join(device_dir, attribute)))
        if value is not None:
            out[suffix] = value

    for suffix, attribute in (("clock_core", "pp_dpm_sclk"),
                              ("clock_mem", "pp_dpm_mclk")):
        value = _dpm_active_mhz(os.path.join(device_dir, attribute))
        if value is not None:
            out[suffix] = value

    # `power1_average` is the averaged register and the one to prefer;
    # `power1_input` is the instantaneous reading and exists on more drivers.
    power = _microwatts(hwmon_file("power1_average"))
    if power is None:
        power = _microwatts(hwmon_file("power1_input"))
    if power is not None:
        out["power"] = power
    limit = _microwatts(hwmon_file("power1_cap"))
    if limit is not None:
        out["power_limit"] = limit

    # fan1_input is RPM, which gpumon has no MetricDef for; it is reported under
    # `fan_rpm` because that is the vocabulary amdsensors.py already uses for
    # the same figure on Windows.
    rpm = _read_int(hwmon_file("fan1_input"))
    if rpm is not None and rpm >= 0:
        out["fan_rpm"] = rpm
    # pwm1 is 0..pwm1_max (255 on every board seen), not 0..100, so the
    # percentage the rest of gpumon speaks has to be derived - and only against
    # a non-zero maximum, which is also the guard against dividing by zero.
    pwm = _read_int(hwmon_file("pwm1"))
    pwm_max = _read_int(hwmon_file("pwm1_max"))
    if pwm is not None and pwm_max:
        out["fan"] = min(100.0, max(0.0, pwm / pwm_max * 100.0))

    return out


# ==========================================================================
# AMD GPU telemetry
# ==========================================================================


class SysfsAmdGpuBackend:
    """AMD GPU telemetry from /sys/class/drm/card*/device (the amdgpu ABI).

    Preferred over rocm-smi whenever both work: this is a handful of file reads
    - tens of microseconds - against a Python program that loads the ROCm stack
    and spawns a process, and unlike a subprocess it cannot be starved by the
    very load gpumon exists to measure. See the module docstring for units.

    `poll()` returns `gpuN_*` keys, so the manager has to tell it how the cards
    were numbered: `set_gpu_map({card_id_or_pci: index})`. A card the manager
    did not create a row for (an NVIDIA card NVML already covers) must not emit
    metrics for a row that does not exist, hence the map rather than a guess.
    """

    name = "sysfs"

    def __init__(self, root: str = DRM_ROOT) -> None:
        self.root = root
        self.cards: list[SysfsCard] = []
        self.error = ""
        self._probed = False
        self._gpu_map: dict[str, int] = {}

    # -- lifecycle -------------------------------------------------------
    def available(self) -> bool:
        self._ensure_probe()
        return any(card.fields for card in self.cards)

    def _ensure_probe(self) -> None:
        """Find the cards once, and record what each one can be read for."""
        if self._probed:
            return
        self._probed = True
        self.cards = enumerate_cards(self.root)
        for card in self.cards:
            card.fields = tuple(read_card(card))
        if not self.cards:
            self.error = (f"no GPU device directories under {self.root} "
                          "(is the amdgpu or nvidia module loaded?)")
        elif not any(card.fields for card in self.cards):
            self.error = ("GPU device directories found, but none exposes the "
                          "amdgpu sysfs attributes (gpu_busy_percent, hwmon "
                          "temperatures)")
        else:
            self.error = ""

    def set_gpu_map(self, mapping: dict[str, int]) -> None:
        """Map cards onto gpumon's row indexes.

        Mirrors `LibreHardwareMonitorBackend.set_gpu_map()`: the backend has no
        idea how the manager numbered the cards, so it is told. Keys may be the
        PCI address (`0000:41:00.0`) or the card id (`card0`); PCI is tried
        first because it is the identity that cannot drift.
        """
        self._gpu_map = {str(k).lower(): int(v) for k, v in mapping.items()}

    def _index_for(self, card: SysfsCard) -> int | None:
        for key in (card.pci, card.card):
            if key and key in self._gpu_map:
                return self._gpu_map[key]
        return None

    def poll(self) -> dict[str, float]:
        self._ensure_probe()
        out: dict[str, float] = {}
        if not self._gpu_map:
            return out
        for card in self.cards:
            index = self._index_for(card)
            if index is None:
                continue
            for suffix, value in read_card(card).items():
                out[f"gpu{index}_{suffix}"] = value
        return out

    def close(self) -> None:
        """Nothing to release: every read is an ordinary file read."""


class AmdRocmSmiBackend:
    """Optional second opinion from the `rocm-smi` CLI, when it is installed.

    Deliberately the second choice: `rocm-smi` is a Python program that loads
    the ROCm stack and prints a table, so one poll costs a process spawn plus
    tens to hundreds of milliseconds, and the sampler thread is the one thing
    that must never stall. sysfs carries most of the same figures for the price
    of a file read, so this backend only fills in what sysfs could not (some
    kernels do not expose `mem_info_vram_used` at all), and the manager merges
    it with `setdefault` for exactly that reason.

    `--json` is requested because the human-readable output is column-aligned
    text that changes shape between releases. The JSON keys still differ
    between rocm-smi versions, so keys are matched by substring rather than
    exactly - see `_ROCM_KEYS` - and the parsing is exercised against a
    recorded payload in test_platforms.py rather than being trusted.
    """

    name = "rocm-smi"

    #: Sources of VRAM and PCI identity, plus the metrics sysfs can miss.
    QUERY = ("--showtemp", "--showuse", "--showmemuse", "--showpower",
             "--showclocks", "--showfan", "--showbus", "--showmeminfo", "vram",
             "--json")

    def __init__(self) -> None:
        self.exe: str | None = None
        self.error = ""
        self._probed = False
        self._available = False
        self._gpu_map: dict[str, int] = {}

    # -- lifecycle -------------------------------------------------------
    def available(self) -> bool:
        if not self._probed:
            self._probed = True
            self.exe = shutil.which("rocm-smi")
            if not self.exe:
                self.error = ("rocm-smi is not on PATH - optional, sysfs "
                              "already provides AMD telemetry")
                return False
            rc, _out, err = _run([self.exe, "--showtemp", "--json"], timeout=8.0)
            if rc != 0:
                detail = (err or "").strip().splitlines()
                self.error = detail[0] if detail else "rocm-smi failed"
                return False
            self.error = ""
            self._available = True
        return self._available

    def set_gpu_map(self, mapping: dict[str, int]) -> None:
        """Map rocm-smi's cards onto gpumon's row indexes.

        Keys are accepted in either namespace rocm-smi uses: the PCI address
        from `--showbus` (“0000:41:00.0”) or its own card id (“card0”). PCI is
        tried first, because rocm-smi's numbering is its own and can differ from
        /sys/class/drm's - attributing one card's temperature to another card
        would be worse than showing nothing.
        """
        self._gpu_map = {str(k).lower(): int(v) for k, v in mapping.items()}

    def poll(self) -> dict[str, float]:
        if not self.available() or not self.exe:
            return {}
        rc, out, err = _run([self.exe, *self.QUERY], timeout=5.0)
        if rc != 0 or not out.strip():
            # A failed poll is not fatal: the manager keeps whatever sysfs gave.
            detail = (err or "").strip().splitlines()
            self.error = detail[0] if detail else "rocm-smi returned nothing"
            return {}
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            self.error = "rocm-smi did not return JSON"
            return {}
        self.error = ""
        out_values: dict[str, float] = {}
        for card in parse_rocm_cards(payload):
            index = self._gpu_map.get(card.pci) if card.pci else None
            if index is None:
                index = self._gpu_map.get(card.card)
            if index is None:
                continue
            for suffix, value in card.fields.items():
                out_values.setdefault(f"gpu{index}_{suffix}", value)
        return out_values

    def close(self) -> None:
        """Nothing to release: each poll is a short-lived subprocess."""


#: rocm-smi JSON key substring -> (GPU_FIELDS suffix, unit). Checked in order,
#: so the more specific patterns come first: "VRAM Total Used Memory (B)"
#: contains "vram total memory", and "Temperature (Sensor memory) (C)" contains
#: "memory". Key names differ between rocm-smi releases, which is why this
#: matches on substrings instead of exact keys.
_ROCM_KEYS: tuple[tuple[str, str, str], ...] = (
    ("junction", "hotspot", "celsius"),
    ("sensor memory", "mem_temp", "celsius"),
    ("sensor edge", "temp", "celsius"),
    ("gpu use", "util", "percent"),
    ("memory use", "mem_util", "percent"),
    ("vram%", "mem_util", "percent"),
    ("graphics package power", "power", "watts"),
    ("average power", "power", "watts"),
    ("sclk", "clock_core", "mhz"),
    ("mclk", "clock_mem", "mhz"),
    ("fan speed", "fan", "percent"),
    ("fan rpm", "fan_rpm", "rpm"),
    ("vram total used memory", "vram_used", "bytes"),
    ("vram total memory", "vram_total", "bytes"),
)


@dataclass
class RocmCard:
    """One card from a rocm-smi JSON payload."""

    card: str                                  # "card0", rocm-smi's own key
    pci: str = ""                              # "0000:41:00.0", from --showbus
    fields: dict[str, float] = field(default_factory=dict)


def _rocm_number(raw: Any) -> float | None:
    """The first number in a rocm-smi value.

    Values arrive as strings (`"38.0"`, `"1500Mhz"`, `"12.345"`) and as
    placeholders (`"N/A"`, `"Unsupported"`, `"unknown"`), the same shape
    nvidia-smi's CSV output has and metrics.py's NvidiaSmiBackend also has to
    cope with.
    """
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str):
        return None
    match = _NUMBER_RE.search(raw)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


_CLOCK_RE = re.compile(r"(\d+(?:\.\d+)?)\s*mhz", re.IGNORECASE)


def _rocm_clock_mhz(raw: Any) -> float | None:
    """The MHz figure in a rocm-smi clock value.

    Some rocm-smi versions print clocks as `(0) 1500Mhz`, where the bracketed
    number is the DPM level and not the clock, so the number attached to "Mhz"
    is the one to take. Without a unit suffix the first number is used.
    """
    if isinstance(raw, str):
        match = _CLOCK_RE.search(raw)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
    return _rocm_number(raw)


def _scale_rocm(value: float, unit: str) -> float | None:
    """Convert one rocm-smi figure to the unit gpumon speaks, or drop it.

    The unit is known from the key, so the conversions are applied here rather
    than at the call site, and each has the same sanity window the sysfs readers
    use: a figure outside it is treated as unreadable, not charted.
    """
    if unit == "celsius":
        return value if -50.0 <= value <= 200.0 else None
    if unit == "percent":
        return value if 0.0 <= value <= 100.0 else None
    if unit == "watts":
        return value if 0.0 < value <= 2000.0 else None
    if unit == "rpm":
        return value if 0.0 <= value <= 100_000.0 else None
    if unit == "mhz":
        # rocm-smi prints MHz, but a clock in Hz (1_500_000_000) is one
        # version-dependent formatting change away; the magnitude says which.
        mhz = value / 1_000_000.0 if value > 100_000.0 else value
        return mhz if 0.0 < mhz <= 10_000.0 else None
    if unit == "bytes":
        # "VRAM Total Memory (B)" is bytes; a value far too small to be bytes is
        # a version that prints MB, which gpumon already wants.
        return value / (1024 * 1024) if value > 10_000_000.0 else value
    return None


def parse_rocm_cards(payload: Any) -> list[RocmCard]:
    """Turn a rocm-smi `--json` payload into cards, defensively.

    Shape (ROCm 4-6): `{"card0": {"Temperature (Sensor edge) (C)": "38.0", ...},
    ...}`. Anything unexpected - a list, a system-level key, a value that is not
    a number - is skipped rather than raising, because this is the optional
    path and a rocm-smi upgrade must not take the sampler down with it.
    """
    cards: list[RocmCard] = []
    if not isinstance(payload, dict):
        return cards
    for card_id, entries in payload.items():
        if not isinstance(entries, dict) or not str(card_id).startswith("card"):
            continue
        card = RocmCard(card=str(card_id).lower())
        for key, raw in entries.items():
            lowered = str(key).lower()
            if "pci bus" in lowered:
                card.pci = str(raw).strip().lower()
                continue
            for pattern, suffix, unit in _ROCM_KEYS:
                if pattern not in lowered or suffix in card.fields:
                    continue
                number = (_rocm_clock_mhz(raw) if unit == "mhz"
                          else _rocm_number(raw))
                if number is None:
                    break
                value = _scale_rocm(number, unit)
                if value is not None:
                    card.fields[suffix] = value
                break
        cards.append(card)
    return cards


# ==========================================================================
# CPU temperature
# ==========================================================================


class LinuxSystemBackend:
    """CPU temperature from /sys, which psutil does not do reliably on Linux.

    `psutil.sensors_temperatures()` exists on Linux, but it only returns
    whatever hwmon chips the kernel happened to label as sensors and returns {}
    on plenty of hosts while the same numbers sit in /sys. Reading the two
    documented sources directly is predictable, and it also gives the per-core
    readings psutil never surfaces:

    * `/sys/class/thermal/thermal_zone*/{type,temp}` - kernel/ACPI zones
      (x86_pkg_temp and friends), `temp` in millidegrees C.
    * `/sys/class/hwmon/hwmon*/{name,tempN_input,tempN_label}` - the chip
      drivers; coretemp/k10temp/zenpower/cpu_thermal carry the package sensor
      and, on most desktop chips, one sensor per core. Millidegrees C.

    Only CPU temperature lives here: CPU %, clocks, RAM and per-core
    utilisation already come from `metrics.SystemBackend`, which works on Linux
    through psutil and is reused rather than rewritten.
    """

    name = "thermal"

    def __init__(self, thermal_root: str = THERMAL_ROOT,
                 hwmon_root: str = HWMON_ROOT) -> None:
        self.thermal_root = thermal_root
        self.hwmon_root = hwmon_root
        self.error = ""
        self._probed = False
        self._package: list[str] = []          # candidate package sensor paths
        self._cores: list[tuple[str, str]] = []  # (core number, input path)

    # -- lifecycle -------------------------------------------------------
    def available(self) -> bool:
        self._probe()
        return bool(self._package)

    def _probe(self) -> None:
        """Locate the package sensor and any per-core sensors, once."""
        if self._probed:
            return
        self._probed = True
        # The chip drivers come first: they carry the per-core sensors and are
        # the numbers users compare against `sensors(1)`, while an ACPI thermal
        # zone is a second-hand reading of the same package.
        for chip in self._cpu_hwmon_chips():
            self._scan_hwmon(chip)
            if self._package:
                break
        if not self._package:
            self._scan_thermal_zones()
        if not self._package:
            self.error = (
                f"no CPU temperature sensor in {self.hwmon_root} "
                "(coretemp/k10temp/zenpower) or in the CPU thermal zones under "
                f"{self.thermal_root} - load the chip driver, e.g. "
                "`modprobe coretemp`")

    def _cpu_hwmon_chips(self) -> list[str]:
        """Directory of each CPU hwmon chip found under /sys/class/hwmon."""
        chips: list[str] = []
        try:
            entries = sorted(os.listdir(self.hwmon_root))
        except OSError:
            return chips
        for entry in entries:
            path = os.path.join(self.hwmon_root, entry)
            name = (_read_text(os.path.join(path, "name")) or "").lower()
            if name in CPU_HWMON_CHIPS:
                chips.append(path)
        return chips

    def _scan_hwmon(self, chip: str) -> None:
        """Collect the package sensor and the per-core sensors of one chip."""
        try:
            entries = sorted(os.listdir(chip))
        except OSError:
            return
        best: tuple[int, str] | None = None
        cores: list[tuple[str, str]] = []
        for entry in entries:
            match = _TEMP_ATTR_RE.match(entry)
            if not match:
                continue
            number = match.group(1)
            path = os.path.join(chip, entry)
            label = (_read_text(os.path.join(chip, f"temp{number}_label"))
                     or "").lower()
            core = _CORE_LABEL_RE.search(label)
            if core:
                # coretemp labels these "Core 0".."Core N"; they are *physical*
                # cores, while psutil's coreNN_util counts logical CPUs. The
                # key is built from the label so the two numberings stay
                # distinguishable instead of silently disagreeing.
                cores.append((core.group(1), path))
                continue
            # "Package id 0" (coretemp); k10temp has no package label at all and
            # calls its package sensor "Tctl" (or "Tdie" on some parts).
            if any(word in label for word in ("package", "tctl", "tdie")):
                score = 2
            elif number == "1":
                score = 1          # the conventional package sensor
            else:
                continue
            if best is None or score > best[0]:
                best = (score, path)
        if best is not None:
            self._package = [best[1]]
        if cores:
            self._cores = sorted(cores, key=lambda item: int(item[0]))

    def _scan_thermal_zones(self) -> None:
        """Fall back to the kernel's thermal zones.

        A zone whose type names a CPU (x86_pkg_temp, cpu-thermal, ...) beats
        whichever zone happens to be zone0. `acpitz` is refused outright: it is
        the motherboard sensor and would be charted as the CPU temperature.
        """
        zones: list[tuple[str, str]] = []
        try:
            entries = sorted(os.listdir(self.thermal_root))
        except OSError:
            return
        for entry in entries:
            if not entry.startswith("thermal_zone"):
                continue
            path = os.path.join(self.thermal_root, entry)
            zone_type = (_read_text(os.path.join(path, "type")) or "").lower()
            zones.append((zone_type, os.path.join(path, "temp")))
        for zone_type, path in zones:
            if any(hint in zone_type for hint in CPU_ZONE_HINTS):
                self._package = [path]
                return
        for zone_type, path in zones:
            if "acpitz" not in zone_type:
                self._package = [path]
                return

    def poll(self) -> dict[str, float]:
        self._probe()
        out: dict[str, float] = {}
        for path in self._package:
            value = _milli_celsius(path)
            if value is not None:
                out["cpu_temp"] = value
                break
        for number, path in self._cores:
            value = _milli_celsius(path)
            if value is not None:
                out[f"core{int(number):02d}_temp"] = value
        return out

    def close(self) -> None:
        """Nothing to release: every read is an ordinary file read."""


# ==========================================================================
# Intel GPUs
# ==========================================================================


class IntelGpuBackend:
    """Intel GPUs on Linux, deliberately unavailable.

    i915/xe expose no vendor-neutral telemetry in sysfs: there is no
    `gpu_busy_percent` (that attribute is amdgpu's own), no `mem_busy_percent`
    and no temperature file. Engine utilisation is only reachable through the
    perf PMU or `intel_gpu_top`, which needs CAP_PERFMON and a running process,
    and the GPU's hwmon chip is not registered on most systems to begin with.

    Reporting unavailable with the reason is the honest answer: an Intel-only
    box then says "GPU detected, no telemetry source" instead of showing an
    empty panel that looks like a bug in gpumon.
    """

    name = "intel"

    def __init__(self) -> None:
        self.error = ("Intel GPUs expose no standard sysfs telemetry (no "
                      "gpu_busy_percent, no mem_busy_percent, no hwmon "
                      "temperature); engine utilisation needs intel-gpu-tools "
                      "(`intel_gpu_top -J`, which requires CAP_PERFMON)")

    def available(self) -> bool:
        return False

    def poll(self) -> dict[str, float]:
        return {}

    def close(self) -> None:
        pass


# ==========================================================================
# Platform notes and bundle
# ==========================================================================


def notes(backends: PlatformBackends) -> list[str]:
    """Linux-specific note text for `Capabilities.notes`.

    Written for a user whose telemetry is incomplete: what Linux uses as the
    source, what is optional, and what simply does not exist here - so nobody
    goes hunting for LibreHardwareMonitor, AMD's ADL or the Windows performance
    counters on a Linux box, and nobody installs rocm-smi expecting gpumon to
    need it.
    """
    out: list[str] = []

    sysfs = backends.amd_sysfs
    if sysfs.available():
        measured = [card for card in sysfs.cards if card.fields]
        names = ", ".join(card.pci_label or card.card for card in measured)
        out.append(
            f"AMD telemetry comes from sysfs for {names} - temperature, "
            "hotspot, memory temperature, utilisation, VRAM, clocks, fan and "
            "power, read straight out of the kernel driver with no subprocess "
            "and no extra software.")
        missing = [card.card for card in sysfs.cards if not card.fields]
        if missing:
            out.append(
                f"{', '.join(missing)} exposes none of the amdgpu sysfs "
                "attributes, so that card has no telemetry on this kernel "
                "(an out-of-tree or very old driver does this).")
    else:
        out.append(f"No AMD GPU telemetry from sysfs: {sysfs.error}.")

    rocm = backends.amd_rocm
    if rocm.available():
        out.append(
            "rocm-smi is installed and is used only to fill in figures sysfs "
            "does not expose; sysfs stays the preferred source because it "
            "needs no subprocess and cannot be starved under load.")
    else:
        out.append(
            "rocm-smi is optional: nothing in gpumon requires it, because AMD "
            "telemetry is read from sysfs here. Install the ROCm stack only if "
            "you want a second opinion on VRAM and clocks.")

    thermal = backends.cpu_thermal
    if thermal.available():
        out.append(
            "CPU temperature comes from /sys/class/hwmon (coretemp / k10temp / "
            "zenpower) or /sys/class/thermal, because psutil cannot read it on "
            "Linux. Per-core temperatures appear as coreNN_temp when the chip "
            "driver exposes one sensor per core.")
    else:
        out.append(f"CPU temperature is unavailable: {thermal.error}.")

    out.append(
        "The Windows-only sources (LibreHardwareMonitor's WMI provider, the "
        "Windows GPU performance counters and AMD's ADL) do not exist on "
        "Linux, which is why gpumon reads sysfs and /sys/class/thermal here "
        "instead.")
    return out


def build_platform_backends(per_core: bool = False) -> PlatformBackends:
    """Linux backends: sysfs for AMD, NVML/nvidia-smi for NVIDIA, /sys for CPU.

    `sources` is in precedence order - the manager merges it with `setdefault`,
    so sysfs (cheap file reads) wins over rocm-smi (a subprocess) whenever both
    have a value.
    """
    sysfs = SysfsAmdGpuBackend()
    rocm = AmdRocmSmiBackend()
    thermal = LinuxSystemBackend()
    return PlatformBackends(
        name="linux",
        system=M.SystemBackend(per_core=per_core),
        nvidia_smi=M.NvidiaSmiBackend(),
        # Windows-only sources, present so every call site can address them.
        pdh=UnsupportedBackend(
            "pdh",
            "the Windows GPU performance counters (PDH) do not exist on Linux; "
            "utilisation comes from amdgpu's gpu_busy_percent instead"),
        adl=UnsupportedBackend(
            "adl",
            "AMD's ADL is a Windows driver interface; Linux AMD telemetry "
            "comes from sysfs"),
        lhm=UnsupportedBackend(
            "lhm",
            "LibreHardwareMonitor's WMI provider is Windows-only; CPU "
            "temperature comes from /sys/class/hwmon here"),
        amd_sysfs=sysfs,
        amd_rocm=rocm,
        cpu_thermal=thermal,
        intel=IntelGpuBackend(),
        sources=[("sysfs", sysfs), ("rocm-smi", rocm), ("thermal", thermal)],
    )
