"""Physical GPU discovery.

The problem this solves
-----------------------
Every easy source reports display *outputs* or driver adapters rather than GPUs:

* the display class registry key listed 3 AMD entries for 2 cards,
* `Win32_VideoController` listed 2 identical "AMD Radeon Pro V620" rows with no
  way to tell them apart,
* ADL reported 7 adapters (4 of them the single NVIDIA card),
* Windows GPU performance counters publish 4 LUIDs, one of which is a extra
  hardware function.

Deduplicating by name+VRAM therefore merged this machine's two Radeon Pro V620s
into a single row. The fix is to enumerate the display *device nodes* through the
Setup API, where each physical GPU appears exactly once with its PCI location,
and to key everything on the LUID that the performance counters actually use.

LUID <-> card mapping
---------------------
NVIDIA is exact: NVML reports `pciBusID` per device, so the LUID whose engine
fingerprint looks like NVIDIA is matched to the card with that PCI bus.

For AMD there is no unprivileged API here that maps a LUID to a PCI address
(DXGI is unavailable on this host and D3DKMTOpenAdapterFromLuid fails with
STATUS_INVALID_PARAMETER, both typical of a display-less VM). AMD LUIDs are
therefore attributed to AMD cards by PCI bus order, and every card reports how
confident that attribution is so the UI can say so rather than mislead.
"""
from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

try:
    import ctypes.wintypes as wintypes
except (ImportError, ValueError):
    # ctypes.wintypes defines VARIANT_BOOL with ctypes' Windows-only "v" type
    # code, so importing it raises ValueError off Windows - and this module has
    # to import cleanly there, because metrics.py imports it unconditionally and
    # only the Setup API calls below are Windows-only. These aliases are the
    # same types wintypes defines on Windows (DWORD is c_ulong, which is 4 bytes
    # under Windows' LLP64), so every struct keeps its exact layout. Nothing
    # here is reached on Linux anyway: `setupapi` is None, so _declare() and
    # enumerate_physical_gpus() return immediately.
    class _FallbackWinTypes:
        DWORD = ctypes.c_uint32
        BOOL = ctypes.c_int32
        LPCWSTR = ctypes.c_wchar_p
        LPWSTR = ctypes.c_wchar_p
        HWND = ctypes.c_void_p

    wintypes = _FallbackWinTypes()  # type: ignore[assignment]

setupapi = ctypes.WinDLL("setupapi") if os.name == "nt" else None

DIGCF_PRESENT = 0x00000002
MAX_DEVICE_ID_LEN = 200

GUID_DEVCLASS_DISPLAY = "{4d36e968-e325-11ce-bfc1-08002be10318}"

SPDRP_DEVICEDESC = 0x00000000
SPDRP_HARDWAREID = 0x00000001
SPDRP_LOCATION_INFORMATION = 0x0000000D
SPDRP_BUSNUMBER = 0x00000015
SPDRP_ADDRESS = 0x0000001C

VENDOR_NAMES = {0x1002: "amd", 0x1022: "amd", 0x10DE: "nvidia",
                0x8086: "intel", 0x1AE0: "google", 0x5143: "qualcomm"}

VENDOR_LABELS = {"amd": "AMD", "nvidia": "NVIDIA", "intel": "Intel"}


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text: str = "") -> None:
        super().__init__()
        if text:
            body = text.strip().strip("{}")
            d1, d2, d3, tail = body.split("-", 3)
            self.Data1 = int(d1, 16)
            self.Data2 = int(d2, 16)
            self.Data3 = int(d3, 16)
            for i, b in enumerate(bytes.fromhex(tail.replace("-", ""))[:8]):
                self.Data4[i] = b


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", GUID),
                ("DevInst", wintypes.DWORD), ("Reserved", ctypes.c_void_p)]


def _declare() -> None:
    if setupapi is None:
        return
    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR,
                                              wintypes.HWND, wintypes.DWORD]
    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.POINTER(SP_DEVINFO_DATA)]
    setupapi.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.LPWSTR,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    setupapi.SetupDiGetDeviceInstanceIdW.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    setupapi.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL


_declare()


@dataclass
class PhysicalGpu:
    """One physical graphics card, as reported by the display device node."""
    name: str
    vendor: str
    vendor_id: int
    device_id: int
    instance_id: str
    bus: int | None = None
    device: int | None = None
    function: int | None = None
    location: str = ""

    @property
    def pci_label(self) -> str:
        if self.bus is None:
            return "unknown PCI location"
        return f"PCI {self.bus:02x}:{self.device or 0:02x}.{self.function or 0}"

    @property
    def device_key(self) -> str:
        """Stable identity that survives reboots and driver updates."""
        return self.instance_id or f"{self.vendor}:{self.vendor_id:04x}:{self.device_id:04x}"


def _string_prop(dev_info: ctypes.c_void_p, data: SP_DEVINFO_DATA, prop: int) -> str:
    buf = ctypes.create_unicode_buffer(1024)
    needed = wintypes.DWORD(0)
    ok = setupapi.SetupDiGetDeviceRegistryPropertyW(
        dev_info, ctypes.byref(data), prop, None, buf, ctypes.sizeof(buf),
        ctypes.byref(needed))
    return buf.value if ok else ""


def _uint_prop(dev_info: ctypes.c_void_p, data: SP_DEVINFO_DATA,
               prop: int) -> int | None:
    value = wintypes.DWORD(0)
    needed = wintypes.DWORD(0)
    ok = setupapi.SetupDiGetDeviceRegistryPropertyW(
        dev_info, ctypes.byref(data), prop, None, ctypes.byref(value),
        ctypes.sizeof(value), ctypes.byref(needed))
    return int(value.value) if ok else None


_LOCATION_RE = None


def parse_pci_location(location: str) -> tuple[int | None, int | None, int | None]:
    """Parse 'PCI bus 23, device 0, function 0' -> (23, 0, 0).

    This string is authoritative. `SPDRP_BUSNUMBER` cannot be trusted for
    display devices: on this machine it reports 23 for the card whose location
    string says bus 17 (the parent bridge's bus, not the device's).
    """
    global _LOCATION_RE
    if not location:
        return None, None, None
    if _LOCATION_RE is None:
        import re
        _LOCATION_RE = re.compile(
            r"bus\s+(\d+).*?device\s+(\d+).*?function\s+(\d+)", re.IGNORECASE)
    match = _LOCATION_RE.search(location)
    if not match:
        return None, None, None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def enumerate_physical_gpus() -> list[PhysicalGpu]:
    """Every physical display adapter, once each, in a stable order."""
    if setupapi is None:
        return []
    guid = GUID(GUID_DEVCLASS_DISPLAY)
    dev_info = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None,
                                             DIGCF_PRESENT)
    if not dev_info or dev_info == ctypes.c_void_p(-1).value:
        return []
    gpus: list[PhysicalGpu] = []
    try:
        index = 0
        while True:
            data = SP_DEVINFO_DATA()
            data.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
            if not setupapi.SetupDiEnumDeviceInfo(dev_info, index, ctypes.byref(data)):
                break
            index += 1
            instance = ctypes.create_unicode_buffer(MAX_DEVICE_ID_LEN)
            setupapi.SetupDiGetDeviceInstanceIdW(
                dev_info, ctypes.byref(data), instance, MAX_DEVICE_ID_LEN, None)
            instance_id = instance.value
            if not instance_id:
                continue
            desc = _string_prop(dev_info, data, SPDRP_DEVICEDESC)
            hwids = _string_prop(dev_info, data, SPDRP_HARDWAREID)
            head = hwids.split("\x00")[0] if hwids else ""
            vendor_id = device_id = 0
            for token in head.split("&"):
                if token.startswith("VEN_"):
                    try:
                        vendor_id = int(token[4:], 16)
                    except ValueError:
                        pass
                elif token.startswith("DEV_"):
                    try:
                        device_id = int(token[4:], 16)
                    except ValueError:
                        pass
            # Fall back to the instance id when the hardware-id list is opaque.
            if not vendor_id:
                for token in instance_id.split("\\")[1:2]:
                    for part in token.split("&"):
                        if part.startswith("VEN_"):
                            try:
                                vendor_id = int(part[4:], 16)
                            except ValueError:
                                pass
                        elif part.startswith("DEV_"):
                            try:
                                device_id = int(part[4:], 16)
                            except ValueError:
                                pass
            address = _uint_prop(dev_info, data, SPDRP_ADDRESS)
            location = _string_prop(dev_info, data, SPDRP_LOCATION_INFORMATION)
            loc_bus, loc_dev, loc_fn = parse_pci_location(location)
            gpus.append(PhysicalGpu(
                name=desc or "Unknown display adapter",
                vendor=VENDOR_NAMES.get(vendor_id, "unknown"),
                vendor_id=vendor_id, device_id=device_id,
                instance_id=instance_id,
                # Prefer the location string; fall back to SPDRP_BUSNUMBER.
                bus=loc_bus if loc_bus is not None
                else _uint_prop(dev_info, data, SPDRP_BUSNUMBER),
                device=loc_dev if loc_dev is not None
                else ((address >> 16) & 0xFFFF if address is not None else None),
                function=loc_fn if loc_fn is not None
                else (address & 0xFFFF if address is not None else None),
                location=location))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(dev_info)
    # Sort by PCI bus so ordering is deterministic across runs.
    gpus.sort(key=lambda g: (g.bus if g.bus is not None else 999, g.name))
    return gpus


# --------------------------------------------------------------------------
# LUID classification
# --------------------------------------------------------------------------

# Engine types that are vendor-diagnostic. NVIDIA publishes overlay/VR engines;
# AMD publishes numbered compute groups plus True Audio.
_NVIDIA_ENGINE_MARKERS = {"legacyoverlay", "vr"}
_AMD_ENGINE_MARKERS = {"true audio 0", "video jpeg 0", "compute 3", "video codec 0"}


def classify_luids(fingerprint: dict[str, dict[str, set[str]]]) -> dict[str, list[str]]:
    """Bucket LUIDs by vendor from the engine types each one exposes.

    Returns {"nvidia": [...], "amd": [...], "unknown": [...]}. LUIDs that expose
    no distinguishing engine are left unknown rather than guessed at.
    """
    out: dict[str, list[str]] = {"nvidia": [], "amd": [], "unknown": []}
    for luid, engines in fingerprint.items():
        keys = set(engines)
        if keys & _NVIDIA_ENGINE_MARKERS:
            out["nvidia"].append(luid)
        elif keys & _AMD_ENGINE_MARKERS:
            out["amd"].append(luid)
        else:
            out["unknown"].append(luid)
    return out


def order_luids_by_activity(luids: Iterable[str],
                            activity: dict[str, float]) -> list[str]:
    """Most active LUID first; ties keep their original order."""
    return sorted(luids, key=lambda l: -activity.get(l, 0.0))


def distribute_luids(cards: int, luids: Sequence[str]) -> list[list[str]]:
    """Deal performance-counter LUIDs out to cards, one card at a time.

    Windows publishes one utilisation LUID per physical AMD GPU plus, on this
    machine, extra LUIDs that never report any activity. Summing them or dealing
    them in blocks therefore gives one card several counters and leaves another
    with a dead one. Round-robin means every card gets a working counter, and any
    surplus LUIDs end up on the first card rather than hiding a card entirely.
    """
    out: list[list[str]] = [[] for _ in range(max(0, cards))]
    if not out:
        return []
    for position, luid in enumerate(luids):
        out[position % len(out)].append(luid)
    return out


def measure_luid_activity(pdh, samples: int = 4, gap: float = 0.25) -> dict[str, float]:
    """Peak utilisation per LUID over a short window.

    Used to tell which counters carry real traffic, and to match counters to
    cards during calibration.
    """
    import time
    peak: dict[str, float] = {}
    for _ in range(max(1, samples)):
        try:
            util, _vram = pdh.poll()
        except Exception:  # noqa: BLE001
            break
        for luid, value in util.items():
            peak[luid] = max(peak.get(luid, 0.0), value or 0.0)
        time.sleep(gap)
    return peak


def bus_sort_key(bus: int | None) -> int:
    return bus if bus is not None else 10_000


if __name__ == "__main__":
    print("=" * 84)
    print("PHYSICAL GPU DISCOVERY")
    print("=" * 84)
    started = time.perf_counter()
    gpus = enumerate_physical_gpus()
    elapsed = (time.perf_counter() - started) * 1000
    print(f"\n{len(gpus)} physical GPU(s) in {elapsed:.1f} ms:\n")
    for g in gpus:
        print(f"  {VENDOR_LABELS.get(g.vendor, g.vendor):7} {g.name}")
        print(f"      {g.pci_label}   vendor=0x{g.vendor_id:04X} "
              f"device=0x{g.device_id:04X}")
        print(f"      key={g.device_key}")

    print("\n" + "=" * 84)
    print("LUID CLASSIFICATION AND ATTRIBUTION")
    print("=" * 84)
    import metrics as M
    pdh = M.PdhGpuBackend()
    if not pdh.available():
        print("  PDH unavailable")
        raise SystemExit(0)
    fingerprint = pdh.fingerprint()
    buckets = classify_luids(fingerprint)
    for vendor, luids in buckets.items():
        print(f"\n  {vendor}: {luids}")
        for luid in luids:
            print(f"      {sorted(fingerprint[luid])}")

    # Attribute NVIDIA LUIDs to cards by PCI bus (via NVML), AMD by order.
    try:
        from nvml import NvmlBackend
        nv = NvmlBackend()
        if nv.available():
            print(f"\n  NVML devices: "
                  f"{[(d.index, d.name, d.pci_bus_id) for d in nv.devices]}")
            nv.close()
    except Exception as exc:  # noqa: BLE001
        print(f"  NVML probe failed: {exc}")
    pdh.close()
