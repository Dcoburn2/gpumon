"""Pin each GPU's utilisation counter by watching which one a load lands on.

Windows gives no unprivileged way to ask "which performance-counter LUID is PCI
bus 17?". Rather than guess from enumeration order, this walks the user through
loading one card at a time and records which counter actually moves. The result
is written to luid_calibration.json and used by every later run.

Without calibration gpumon deals counters to cards round-robin, which is usually
right but cannot be verified from inside the process.
"""
from __future__ import annotations

import time

import metrics as M
from gpuenum import measure_luid_activity


def _cards_needing_luids(manager: M.SensorManager) -> list[M.GpuDevice]:
    return [g for g in manager.gpus if g.vendor != "nvidia" and g.pci]


def _activity_snapshot(manager: M.SensorManager, seconds: float = 6.0) -> dict[str, float]:
    """Peak utilisation per LUID over a short window."""
    peak: dict[str, float] = {}
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            util, _vram = manager.pdh.poll()
        except Exception:  # noqa: BLE001
            util = {}
        for luid, value in util.items():
            peak[luid] = max(peak.get(luid, 0.0), value or 0.0)
        time.sleep(0.25)
    return peak


def _pick_busiest(peak: dict[str, float], already: set[str],
                  threshold: float = 25.0) -> str | None:
    candidates = [(luid, value) for luid, value in peak.items()
                  if luid not in already and value >= threshold]
    if not candidates:
        return None
    return max(candidates, key=lambda kv: kv[1])[0]


def calibrate(argv: list[str] | None = None) -> int:
    argv = argv or []
    quiet_seconds = 8.0
    load_seconds = 8.0
    for arg in argv:
        if arg.startswith("--load-seconds="):
            load_seconds = float(arg.split("=", 1)[1])

    print("=" * 84)
    print("GPU counter calibration")
    print("=" * 84)
    print("""
Windows does not let a normal process ask which performance counter belongs to
which physical card, so gpumon has to infer it. This walks through the cards one
at a time and records which counter moves while each is loaded.

You will be asked to load ONE card at a time. Any GPU workload will do: a
benchmark, a compute job, or an LLM inference run pinned to that card.""")

    manager = M.SensorManager()
    manager.prime()
    cards = _cards_needing_luids(manager)
    if not cards:
        print("\nNo non-NVIDIA cards found; nothing to calibrate.")
        manager.close()
        return 0
    luids = manager.pdh.enumerate_luids()
    print(f"\nFound {len(cards)} card(s) and {len(luids)} utilisation counter(s):")
    for card in cards:
        print(f"    gpu{card.index}  {card.display_name}  "
              f"key={card.device_key[:40]}")
    for luid in luids:
        print(f"    counter {luid}")

    if len(cards) < 2:
        print("\nOnly one card; no ambiguity to resolve.")
        manager.close()
        return 0

    print(f"\nWatching for {len(cards)} x ({quiet_seconds:.0f}s quiet + "
          f"{load_seconds:.0f}s loaded)")

    mapping: dict[str, list[str]] = {}
    claimed: set[str] = set()
    for step, card in enumerate(cards, start=1):
        print(f"\n{'-' * 84}")
        print(f"STEP {step} of {len(cards)}: card gpu{card.index} "
              f"({card.display_name})")
        print(f"{'-' * 84}")
        print(f"  Make sure NOTHING is loading any GPU. Measuring the quiet "
              f"baseline for {quiet_seconds:.0f}s...")
        baseline = _activity_snapshot(manager, quiet_seconds)
        print(f"    baseline peaks: "
              f"{ {k[-6:]: round(v, 1) for k, v in sorted(baseline.items())} }")

        print(f"\n  Now LOAD gpu{card.index} ({card.display_name}) and keep it "
              f"busy for {load_seconds:.0f}s.")
        print("  (A GPU benchmark, or any compute job pinned to that card.)")
        input("  Press Enter when the load is running... ")
        loaded = _activity_snapshot(manager, load_seconds)
        print(f"    loaded peaks: "
              f"{ {k[-6:]: round(v, 1) for k, v in sorted(loaded.items())} }")

        # The counter that rose is this card's.
        delta = {luid: loaded.get(luid, 0.0) - baseline.get(luid, 0.0)
                 for luid in set(loaded) | set(baseline)}
        busy = _pick_busiest({k: v for k, v in delta.items()}, claimed)
        if busy is None:
            print("    No counter rose. Either the load was not on this card, "
                  "or it did not use the GPU.")
            print("    Skipping this card; it will keep its inferred counter.")
            continue
        claimed.add(busy)
        mapping[card.device_key] = [busy]
        print(f"    -> gpu{card.index} uses counter {busy} "
              f"(rose by {delta[busy]:.0f} percentage points)")

    manager.close()

    if not mapping:
        print("\nNothing could be measured; leaving the inferred mapping in place.")
        return 1

    path = M.save_luid_calibration(mapping)
    print(f"\nSaved calibration to {path}")
    print("Verified mapping:")
    for key, values in sorted(mapping.items()):
        name = next((c.display_name for c in cards if c.device_key == key), key[:40])
        print(f"    {name}: {values}")
    unclaimed = [l for l in luids if l not in claimed]
    if unclaimed:
        print(f"\nCounters that never rose (expected: idle hardware functions): "
              f"{unclaimed}")
    print("\nRestart gpumon to use this mapping.")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(calibrate(sys.argv[1:]))
