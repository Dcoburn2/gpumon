"""Verify the LibreHardwareMonitor HTTP reader.

0.9.6 removed the WMI provider - its binaries contain no WMI code - so the
namespace gpumon used to read never appears however the settings file is
written. The web server is the interface that replaced it, and this exercises
the reader against a payload shaped exactly like the one LHM serves, without
needing LHM to be running.
"""
from __future__ import annotations

import json

import metrics as M

problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


def sensor(sensor_id: str, kind: str, text: str, value: str) -> dict:
    return {"SensorId": sensor_id, "Type": kind, "Text": text, "Value": value,
            "Children": []}


def node(text: str, children: list) -> dict:
    return {"Text": text, "Children": children}


# A payload shaped like /data.json: computer -> hardware -> group -> sensor.
PAYLOAD = node("Computer", [
    node("Intel Core i9-7900X", [
        node("Temperatures", [
            sensor("/intelcpu/0/temperature/0", "Temperature", "CPU Package", "47.0 °C"),
            sensor("/intelcpu/0/temperature/1", "Temperature", "CPU Core #1", "45.0 °C"),
        ]),
        node("Powers", [
            sensor("/intelcpu/0/power/0", "Power", "CPU Package", "89.5 W"),
        ]),
        node("Clocks", [
            sensor("/intelcpu/0/clock/0", "Clock", "CPU Core #1", "3312.0 MHz"),
        ]),
    ]),
    node("AMD Radeon Pro V620", [
        node("Temperatures", [
            sensor("/amdgpu/0/temperature/0", "Temperature", "GPU Core", "38.0 °C"),
            sensor("/amdgpu/0/temperature/1", "Temperature", "GPU Hot Spot", "52.0 °C"),
            sensor("/amdgpu/0/temperature/2", "Temperature", "GPU Memory", "44.0 °C"),
        ]),
        node("Load", [
            sensor("/amdgpu/0/load/0", "Load", "GPU Core", "73.0 %"),
        ]),
    ]),
    node("NVIDIA Quadro P2000", [
        node("Temperatures", [
            sensor("/gpu-nvidia/0/temperature/0", "Temperature", "GPU Core", "35.0 °C"),
        ]),
    ]),
])


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def serve(payload: dict | None):
    """Point urllib at a synthetic LHM, or at nothing at all."""
    import urllib.error
    import urllib.request

    def fake_urlopen(url, timeout=None):
        if payload is None:
            raise urllib.error.URLError("connection refused")
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    urllib.request.urlopen = fake_urlopen


print("=" * 84)
print("LIBREHARDWAREMONITOR HTTP TEST")
print("=" * 84)

print("\n[1] a serving LHM is detected as available")
backend = M.LibreHardwareMonitorBackend()
serve(PAYLOAD)
check("available() with the server answering", backend.available())
check("the source is recorded as http", backend.source == "http", backend.source)
check("no error is reported", backend.error == "", backend.error)

print("\n[2] the readings map onto gpumon's metrics")
# The keys are exactly what _map_lhm_gpus() builds: LHM numbers GPUs per vendor
# and _index_for() looks the "/vendor/n" prefix up, leading slash included.
backend.set_gpu_map({"/amdgpu/0": 1, "/gpu-nvidia/0": 0})
values = backend.poll()
for key in sorted(values):
    print(f"    {key:20} = {values[key]}")
check("CPU package temperature is read", values.get("cpu_temp") == 47.0,
      str(values.get("cpu_temp")))
check("CPU power is read", values.get("cpu_power") == 89.5,
      str(values.get("cpu_power")))
check("AMD edge temperature is read", values.get("gpu1_temp") == 38.0,
      str(values.get("gpu1_temp")))
check("AMD hotspot is read", values.get("gpu1_hotspot") == 52.0,
      str(values.get("gpu1_hotspot")))
check("AMD memory temperature is read", values.get("gpu1_mem_temp") == 44.0,
      str(values.get("gpu1_mem_temp")))
check("AMD utilisation is read", values.get("gpu1_util") == 73.0,
      str(values.get("gpu1_util")))
check("NVIDIA temperature is read", values.get("gpu0_temp") == 35.0,
      str(values.get("gpu0_temp")))
check("units do not leak into the numbers",
      all(isinstance(v, float) for v in values.values()), str(values))

print("\n[3] a server that is not answering says so")
dead = M.LibreHardwareMonitorBackend()
serve(None)
check("available() is False when nothing answers", not dead.available())
print(f"    error: {dead.error!r}")
check("the message names the fix, not a WMI namespace",
      "web server" in dead.error.lower() or "not publishing" in dead.error.lower(),
      dead.error)
check("it does not claim a WMI provider is the answer",
      "wmi provider not running" not in dead.error.lower(), dead.error)

print("\n[4] the tree walk copes with what the server actually sends")
weird = node("Computer", [
    node("Hardware", [
        node("Group", [
            {"SensorId": "/intelcpu/0/temperature/0", "Type": "Temperature",
             "Text": "CPU Package", "Value": "0.0 °C", "Children": []},
            {"SensorId": "/intelcpu/0/temperature/1", "Type": "Temperature",
             "Text": "CPU Core #1", "Value": None, "Children": []},
            {"SensorId": "", "Type": "Temperature", "Text": "junk",
             "Value": "5.0 °C", "Children": []},
            {"SensorId": "/intelcpu/0/load/0", "Type": "Load",
             "Text": "CPU Total", "Value": "12.5 %", "Children": []},
        ]),
    ]),
])
backend2 = M.LibreHardwareMonitorBackend()
serve(weird)
check("available() on an odd but valid tree", backend2.available())
odd = backend2.poll()
print(f"    {odd}")
check("a zero reading is kept", odd.get("cpu_temp") == 0.0, str(odd.get("cpu_temp")))
check("a null value is skipped rather than crashing", "cpu_temp" in odd)
check("a sensor with no id is ignored", len(odd) <= 2, str(odd))

print("\n" + "=" * 84)
if problems:
    print(f"LHM HTTP TEST FAILED ({len(problems)})")
    for p in problems:
        print(f"  - {p}")
else:
    print("LHM HTTP TEST PASSED")
print("=" * 84)
raise SystemExit(1 if problems else 0)
