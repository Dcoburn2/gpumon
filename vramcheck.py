"""Measure real VRAM usage from a workload's load/unload delta.

Why this exists
---------------
gpumon reads AMD VRAM from `ADL2_Adapter_DedicatedVRAMUsage_Get`. Against a known
workload - Qwen3.8-27B UD-Q6_K_XL, 24.42 GB of weights on disk, reported by LM
Studio as ~25.88 GB in use - that figure came out about 1.93x higher than
expected. A ratio cannot distinguish a scale error from a fixed offset, from the
model not being split the way we assumed, or from partial CPU offload.

A delta can. Whatever the counter reads with the workload loaded, minus what it
reads with the workload unloaded, is what that workload actually occupies -
independent of unit, offset, or driver quirk.

Two ways to run it
------------------
Interactive (walks you through it):

    python gpumon.py --vram-check

Two passes, if you would rather load and unload on your own schedule:

    python gpumon.py --vram-check --phase=idle     # workload unloaded
    python gpumon.py --vram-check --phase=loaded   # workload loaded
"""
from __future__ import annotations

import json
import os
import time

import metrics as M

#: Reference workload used for the sanity comparison. Only used for the printed
#: verdict; the measurement itself needs no assumptions about the workload.
REFERENCE_WEIGHTS_GB = 23.56 + 0.86
REFERENCE_TOTAL_GB = 25.88
_SAVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "vram_phase.json")


def read_all(manager: M.SensorManager) -> dict[str, float]:
    """ADL dedicated VRAM in MB, per adapter."""
    out: dict[str, float] = {}
    for adapter in manager.amd.adapters:
        mb = manager.amd._dedicated_vram(adapter.index)
        if mb is not None:
            out[adapter.pci_label] = mb
    return out


def sample(manager: M.SensorManager, seconds: float, label: str) -> dict[str, float]:
    print(f"  sampling for {seconds:.0f}s ({label})")
    peaks: dict[str, float] = {}
    deadline = time.time() + seconds
    while time.time() < deadline:
        for key, mb in read_all(manager).items():
            peaks[key] = max(peaks.get(key, 0.0), mb)
        time.sleep(0.5)
    for key in sorted(peaks):
        print(f"    {key}: {peaks[key] / 1024:.2f} GB")
    return peaks


def _save(phase: str, values: dict[str, float]) -> None:
    payload: dict[str, dict[str, float]] = {}
    if os.path.exists(_SAVE_PATH):
        try:
            with open(_SAVE_PATH, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, json.JSONDecodeError):
            payload = {}
    payload[phase] = values
    with open(_SAVE_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    print(f"  saved '{phase}' to {os.path.basename(_SAVE_PATH)}")


def _load_saved() -> dict[str, dict[str, float]]:
    try:
        with open(_SAVE_PATH, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def report(idle: dict[str, float], loaded: dict[str, float]) -> None:
    print("\n" + "=" * 84)
    print("RESULT")
    print("=" * 84)
    print(f"  {'adapter':>14}  {'idle':>10}  {'loaded':>10}  {'delta':>10}")
    total_delta = 0.0
    for key in sorted(set(idle) | set(loaded)):
        i = idle.get(key, 0.0)
        l = loaded.get(key, 0.0)
        delta = l - i
        if "2d:00.0" not in key:
            total_delta += max(0.0, delta)
        print(f"  {key:>14}  {i / 1024:9.2f}G  {l / 1024:9.2f}G  "
              f"{delta / 1024:+9.2f}G")

    delta_gb = total_delta / 1024
    print(f"\n  AMD cards: total increase from loading the workload = "
          f"{delta_gb:.2f} GB")
    print(f"  reference model weights on disk : {REFERENCE_WEIGHTS_GB:.2f} GB")
    print(f"  reference reported in use       : {REFERENCE_TOTAL_GB:.2f} GB")

    print("\n" + "=" * 84)
    print("VERDICT")
    print("=" * 84)
    if delta_gb <= 0.05:
        print("  No increase measured. Either the workload did not load onto these")
        print("  cards, or both passes were taken in the same state.")
        return
    ratio_weights = delta_gb / REFERENCE_WEIGHTS_GB
    ratio_total = delta_gb / REFERENCE_TOTAL_GB
    print(f"  delta / weights = {ratio_weights:.2f}x    "
          f"delta / reported-total = {ratio_total:.2f}x")
    if 0.85 <= ratio_total <= 1.2:
        print("  The delta matches the workload, so gpumon's per-card VRAM figure")
        print("  is correct as an absolute measurement.")
    elif ratio_total > 1.5:
        print("  The delta is larger than the workload, so gpumon's AMD VRAM")
        print("  reading is inflated. Note the delta above and the figure can be")
        print("  rescaled or the source replaced.")
    else:
        print("  The delta is smaller than the model on disk, which normally means")
        print("  part of the model lives in system memory. The reading is fine.")
    print("\n  The delta is immune to unit, offset and scaling errors: it is only")
    print("  the difference between two readings of the same counter.")


def main(argv: list[str] | None = None) -> int:
    argv = argv or []
    phase = ""
    seconds = 6.0
    for arg in argv:
        if arg.startswith("--phase="):
            phase = arg.split("=", 1)[1].lower()
        elif arg.startswith("--seconds="):
            seconds = float(arg.split("=", 1)[1])

    manager = M.SensorManager()
    if not manager.amd.available():
        print("ADL is unavailable, so AMD VRAM cannot be measured.")
        manager.close()
        return 1

    print("=" * 84)
    print("VRAM DELTA CHECK")
    print("=" * 84)

    if phase in ("idle", "loaded"):
        label = ("workload UNLOADED" if phase == "idle" else "workload LOADED")
        print(f"\nPass: {phase} ({label})")
        values = sample(manager, seconds, label)
        _save(phase, values)
        saved = _load_saved()
        if "idle" in saved and "loaded" in saved:
            report(saved["idle"], saved["loaded"])
        else:
            other = "loaded" if phase == "idle" else "idle"
            print(f"\n  Now run the other pass:")
            print(f"    python gpumon.py --vram-check --phase={other}")
        manager.close()
        return 0

    print(f"""
This measures what the loaded workload actually occupies, by subtracting the idle
reading from the loaded reading. Reference workload: Qwen3.8-27B UD-Q6_K_XL,
{REFERENCE_WEIGHTS_GB:.2f} GB of weights on disk.

Load the workload in LM Studio when asked, and unload it when asked. Answer each
prompt only after the change has taken effect.""")

    try:
        input("\n[1/2] UNLOAD the workload, then press Enter... ")
    except (EOFError, KeyboardInterrupt):
        print("\n  (no console input available - use --phase=idle / --phase=loaded)")
        manager.close()
        return 1
    idle = sample(manager, seconds, "workload unloaded")

    try:
        input("\n[2/2] LOAD the workload again and wait until it is ready, then "
              "press Enter... ")
    except (EOFError, KeyboardInterrupt):
        _save("idle", idle)
        print(f"\n  Saved the idle pass. Re-run with --phase=loaded when ready.")
        manager.close()
        return 1
    loaded = sample(manager, seconds, "workload loaded")

    _save("idle", idle)
    _save("loaded", loaded)
    report(idle, loaded)
    manager.close()
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
