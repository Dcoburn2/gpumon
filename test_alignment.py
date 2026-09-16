"""Verify every card's columns line up, at several window widths.

The bug being tested: each card sized its own grid columns, so graphs started
and ended at different x positions. This measures the actual widget geometry.
"""
from __future__ import annotations

import os

# Building the window must not launch a sensor helper.
os.environ.setdefault("GPUMON_NO_AUTOSTART", "1")
import time
import tkinter as tk

import metrics as M
import sampler as SP
import store as S

DB = "diagalign.db"
for s in ("", "-wal", "-shm"):
    if os.path.exists(DB + s):
        os.remove(DB + s)

problems: list[str] = []
print("=" * 88)
print("COLUMN ALIGNMENT TEST")
print("=" * 88)

manager = M.SensorManager(per_core=True)
store = S.Store(DB)
sampler = SP.Sampler(manager, store, sample_hz=2.0, per_core=True)
root = tk.Tk()
from ui.monitor import MonitorApp

app = MonitorApp(root, manager, store, sampler)
print(f"\ninitial window size chosen by the app: {app._size}")


def pump(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


pump(3.0)


def columns_of(index: int) -> dict[str, tuple[int, int]]:
    """(x, width) for the value, stats and graph columns of one card."""
    panel = app.panels[index]
    out = {}
    for field, lbl in panel.value_labels.items():
        out["value"] = (lbl.winfo_rootx(), lbl.winfo_width())
        break
    for field, lbl in panel.stats_labels.items():
        if lbl.winfo_ismapped():
            out["stats"] = (lbl.winfo_rootx(), lbl.winfo_width())
            break
    for field, spark in panel.sparks.items():
        if spark.winfo_ismapped():
            out["graph"] = (spark.winfo_rootx(), spark.winfo_width())
            break
    return out


def check_alignment(label: str) -> None:
    print(f"\n--- {label} (window {root.winfo_width()}x{root.winfo_height()})")
    per_card = {i: columns_of(i) for i in sorted(app.panels)}
    for i, cols in per_card.items():
        graph = cols.get("graph")
        stats = cols.get("stats")
        print(f"    gpu{i}: value_x={cols['value'][0]}  "
              f"stats_x={stats[0] if stats else 'n/a'}  "
              f"graph_x={graph[0] if graph else 'n/a'}  "
              f"graph_w={graph[1] if graph else 'hidden'}")

    for column in ("value", "stats", "graph"):
        xs = [c[column][0] for c in per_card.values() if column in c]
        if len(xs) > 1 and max(xs) - min(xs) > 2:
            msg = f"{label}: {column} column misaligned by {max(xs) - min(xs)} px"
            print(f"    FAIL {msg}")
            problems.append(msg)
        elif xs:
            print(f"    OK   {column} column aligned across cards")
    # Graphs should also be the same width.
    widths = [c["graph"][1] for c in per_card.values() if "graph" in c]
    if len(widths) > 1 and max(widths) - min(widths) > 2:
        msg = f"{label}: graph widths differ by {max(widths) - min(widths)} px"
        print(f"    FAIL {msg}")
        problems.append(msg)
    elif widths:
        print(f"    OK   graph width equal at {widths[0]} px")


check_alignment("initial size")

for size in ("1400x900", "1100x700", "900x600", "1800x1000"):
    root.geometry(size)
    root.update()
    pump(1.2)
    check_alignment(f"resized to {size}")

print("\n[no text is ever cut off]")
# The bug being tested: the value and stats labels had fixed character widths
# smaller than their contents ("83.2%  25,554/30,704 MB", "min 104.0 avg 104.0
# max 104.0"), and Tk truncates text longer than a label's declared width. A
# label is only safe when the room the layout gave it is at least what its text
# needs, so every visible label is measured against its own required width.
def clipping_report(label: str) -> None:
    worst: list[str] = []
    for index, panel in sorted(app.panels.items()):
        groups = (("label", panel.sized.get("labels", [])),
                  ("value", panel.sized.get("values", [])),
                  ("stats", panel.sized.get("stats", [])))
        for name, widgets in groups:
            for widget in widgets:
                if not widget.winfo_ismapped():
                    continue
                need = widget.winfo_reqwidth()
                got = widget.winfo_width()
                if got + 1 < need:
                    worst.append(f"gpu{index} {name} {widget.cget('text')!r}: "
                                 f"{got} px for {need} px of text")
    if worst:
        problems.append(f"{label}: clipped text")
        for line in worst[:4]:
            print(f"    FAIL {line}")
    else:
        print(f"    OK   every visible label fits its column at "
              f"{root.winfo_width()}x{root.winfo_height()}")


for size in ("1400x900", "1180x880", "1000x700", "860x620", "760x560",
             "1800x1000"):
    root.geometry(size)
    root.update()
    pump(1.2)
    clipping_report(f"window {size}")

print("\n[the whole card scales, not just the graph]")
# Resizing used to move only the graph: the left block, the meter and the text
# columns were pinned to constants. They follow the window now.
left_widths = {}
graph_widths = {}
for size in ("1800x1000", "1180x880", "860x620"):
    root.geometry(size)
    root.update()
    pump(1.2)
    left_widths[size] = app._left_width
    graph_widths[size] = app._graph_width
print(f"    left block:  {left_widths}")
print(f"    graph width: {graph_widths}")
if len(set(left_widths.values())) > 1:
    print("    OK   the left block scales with the window")
else:
    problems.append("left block is pinned to one width")
if len(set(graph_widths.values())) > 1:
    print("    OK   the graph still takes the surplus")
else:
    problems.append("graph width never changed")
meter_widths = {size: 0 for size in graph_widths}
for size in graph_widths:
    root.geometry(size)
    root.update()
    pump(0.8)
    meter_widths[size] = app.panels[sorted(app.panels)[0]].meter.winfo_width()
print(f"    meter bar:   {meter_widths}")
if len(set(meter_widths.values())) > 1:
    print("    OK   the meter bar follows the left block")
else:
    problems.append("meter bar is pinned to one width")

print("\n[graphs off]")
app.toggle_graphs(force=False)
root.update()
pump(1.0)
per_card = {i: columns_of(i) for i in sorted(app.panels)}
for i, cols in per_card.items():
    print(f"    gpu{i}: value_x={cols['value'][0]} graph={'hidden' if 'graph' not in cols else cols['graph']}")
xs = [c["value"][0] for c in per_card.values()]
if max(xs) - min(xs) <= 2:
    print("    OK   value column still aligned with graphs hidden")
else:
    problems.append("graphs-off: value column misaligned")
app.toggle_graphs(force=True)
root.update()
pump(1.0)
print("\n[graphs back on]")
check_alignment("graphs restored")

print("\n[standard size sanity]")
print(f"    screen: {root.winfo_screenwidth()}x{root.winfo_screenheight()}")
print(f"    chosen: {app._size}")
w, h = (int(v) for v in app._size.split("x"))
if w > root.winfo_screenwidth() or h > root.winfo_screenheight():
    problems.append("window larger than the screen")
else:
    print("    OK   fits on screen")
if w < 700:
    problems.append("window too narrow for the data")
else:
    print(f"    OK   wide enough for the text columns ({w} px)")

app.on_close()
sampler.stop()
store.close()
manager.close()
for s in ("", "-wal", "-shm"):
    if os.path.exists(DB + s):
        try:
            os.remove(DB + s)
        except OSError:
            pass

print("\n" + "=" * 88)
if problems:
    print(f"ALIGNMENT TEST FAILED ({len(problems)})")
    for p in problems:
        print(f"  - {p}")
else:
    print("ALIGNMENT TEST PASSED")
print("=" * 88)
raise SystemExit(1 if problems else 0)
