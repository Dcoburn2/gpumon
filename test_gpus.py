"""Verify GPU identity, per-GPU metrics and that nothing got merged."""
import time

import metrics as M

sm = M.SensorManager()
print("discovered:")
for g in sm.gpus:
    print(f"  gpu{g.index} {g.vendor:7} {g.display_name:42} "
          f"vram={g.vram_total_mb} conf={g.luid_confidence} "
          f"luids={len(g.luids)} sources={g.sources}")

print("\npolling (after prime):")
sm.prime()
time.sleep(0.6)
for cycle in range(3):
    values = sm.poll()
    line = []
    for g in sm.gpus:
        i = g.index
        line.append(
            f"gpu{i}: util={values.get(f'gpu{i}_util')} "
            f"temp={values.get(f'gpu{i}_temp')} "
            f"vram={values.get(f'gpu{i}_vram_used')}/{values.get(f'gpu{i}_vram_total')}")
    print(f"  cycle {cycle}:")
    for entry in line:
        print(f"    {entry}")
    time.sleep(1)

print("\nuniqueness checks:")
names = [g.display_name for g in sm.gpus]
print(f"  {len(names)} rows, {len(set(names))} unique display names")
assert len(names) == len(set(names)), "duplicate GPU rows"
amds = [g for g in sm.gpus if g.vendor == "amd"]
print(f"  AMD rows: {len(amds)} (expected 2)")
assert len(amds) == 2
for g in amds:
    assert g.luids, f"{g.display_name} has no utilisation counter"
print("  every AMD card has at least one utilisation counter: OK")
nv = [g for g in sm.gpus if g.vendor == "nvidia"]
for g in nv:
    assert g.pci, "NVIDIA card is missing its PCI location"
    assert g.vram_total_mb, "NVIDIA card is missing its VRAM total"
print("  NVIDIA card has PCI location and VRAM total: OK")

# A card that reports a temperature must not also be described as having no
# temperature sensor: the desktop window derived that sentence from the backend
# list, which knew only about NVML/LHM, so both V620s read
# "no thermal sensor available" directly under "hotspot 34 C  mem 34 C".
print("\ntemperature reporting:")
for g in sm.gpus:
    live = [values.get(f"gpu{g.index}_{field}")
            for field in ("temp", "hotspot", "mem_temp")]
    reading = [v for v in live if v is not None]
    contradicting = bool(reading) and not g.has_temperature
    print(f"  gpu{g.index} sources={g.sources} readings={reading} "
          f"has_temperature={g.has_temperature}")
    assert not contradicting, (
        f"{g.display_name} reports {reading} but has_temperature is False "
        f"(sources={g.sources}): the UI would call it sensorless")
print("  no card contradicts itself: OK")
sm.close()
print("\nALL CHECKS PASSED")
