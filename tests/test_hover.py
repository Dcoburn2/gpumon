"""Test hover readouts and the generalised GPU discovery together."""

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


_skip_machine_specific_test()




import math
import os
import time
import tkinter as tk

import metrics as M
from ui import widgets as W

# Keep every setting this test writes out of the user's real config.json.
os.environ.setdefault(
    "GPUMON_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "diagmon-config.json"))
# Start from a clean slate: `layout` is read when the window is built, so
# a scratch file left behind by an earlier run would change the geometry
# this test measures.
if os.path.exists(os.environ["GPUMON_CONFIG"]):
    os.remove(os.environ["GPUMON_CONFIG"])

problems: list[str] = []

print("=" * 78)
print("HOVER + DISCOVERY TEST")
print("=" * 78)

print("\n[1] multi-GPU discovery")
manager = M.SensorManager()
caps = manager.capabilities()
print(f"    {len(caps.gpus)} GPU(s):")
for g in caps.gpus:
    print(f"      gpu{g.index} {g.vendor:7} {g.display_name:38} "
          f"sources={','.join(g.sources):10} conf={g.luid_confidence}")
if len(caps.gpus) < 3:
    problems.append(f"expected the 2 V620s plus the P2000, got {len(caps.gpus)}")
same_name = [g for g in caps.gpus if g.vendor == "amd"]
if len(same_name) < 2:
    problems.append("the two identical AMD cards were not kept separate")

print("\n[2] hover readout on a chart")
root = tk.Tk()
root.withdraw()

chart = W.TimeSeriesChart(root, width=900, height=240)
util = [(float(i), 40 + 35 * math.sin(i / 40.0)) for i in range(600)]
temp = [(float(i), 55 + 20 * math.sin(i / 40.0 + 0.6)) for i in range(600)]
chart.set_data([
    W.ChartSeries(label="GPU utilisation (%)", color=W.SERIES_COLORS[0], points=util),
    W.ChartSeries(label="GPU temp (C)", color=W.CRIT, points=temp),
])
root.update()
base_items = len(chart.find_all())
print(f"    rendered with {base_items} canvas items")

# Simulate the pointer moving over the plot.
x0, y0, x1, y1 = chart._plot_box()
mid = (x0 + x1) / 2
chart._hover = (int(mid), int((y0 + y1) / 2))
chart.render()
texts = []
for item in chart.find_all():
    if chart.type(item) == "text":
        texts.append(chart.itemcget(item, "text"))
print(f"    after hover: {len(chart.find_all())} items, {len(texts)} text labels")

def readout_rows(chart: W.TimeSeriesChart) -> list[str]:
    """Series labels inside the hover readout box, in display order.

    The legend carries the same labels, so rows are identified by position: the
    readout box is the only place a series label appears below the plot top.
    """
    labels = {s.label for s in chart.series}
    _x0, y0, _x1, _y1 = chart._plot_box()
    rows = []
    for item in chart.find_all():
        if chart.type(item) != "text":
            continue
        text = chart.itemcget(item, "text")
        if text in labels and chart.coords(item)[1] > y0:
            rows.append((chart.coords(item)[1], text))
    rows.sort()
    return [text for _y, text in rows]


joined = " | ".join(texts)
print(f"    labels: {joined[:190]}")

rows_shown = readout_rows(chart)
print(f"    readout series rows: {rows_shown}")
checks = {
    "time readout": any(t.startswith("t = ") for t in texts),
    "a numeric value is shown": any(
        t.replace(",", "").replace(".", "").isdigit() for t in texts),
    "both series listed": len(rows_shown) == 2,
}
for name, ok in checks.items():
    print(f"    {'OK  ' if ok else 'FAIL'} {name}")
    if not ok:
        problems.append(f"hover missing: {name}")

# The tooltip must report a real sample, not an interpolation.
rows = chart._hover_rows(300.0)
print(f"\n    rows at t=300: "
      f"{[(lbl, round(val, 2), round(tt, 2)) for lbl, _c, val, tt, _a in rows]}")
for label, _c, value, sample_t, _axis in rows:
    expected = 40 + 35 * math.sin(sample_t / 40.0) if "util" in label else \
        55 + 20 * math.sin(sample_t / 40.0 + 0.6)
    if abs(value - expected) > 0.01:
        problems.append(f"hover value for {label} is not the recorded sample")
        break
else:
    print("    OK   every hover value equals a recorded sample")

# Moving the mouse near the other line should highlight that one.
top = int(y0 + 8)
bottom = int(y1 - 8)
chart._hover = (int(mid), top)
chart.render()
top_rows = readout_rows(chart)
chart._hover = (int(mid), bottom)
chart.render()
bottom_rows = readout_rows(chart)
print(f"\n    hover near top    -> readout order: {top_rows}")
print(f"    hover near bottom -> readout order: {bottom_rows}")
if not top_rows or not bottom_rows:
    problems.append("hover readout rows missing")
elif top_rows[0] == bottom_rows[0]:
    problems.append("hover does not track which line the pointer is near")
else:
    print("    OK   the readout leads with the line nearest the pointer")

print("\n[3] hover clears on leave")
chart._on_leave(None)
after = [chart.itemcget(i, "text") for i in chart.find_all()
         if chart.type(i) == "text"]
if any(t.startswith("t = ") for t in after):
    problems.append("hover readout did not clear on mouse leave")
else:
    print("    OK   readout cleared")

print("\n[4] empty-data chart must not crash on hover")
empty = W.TimeSeriesChart(root, width=400, height=150)
empty.set_data([])
empty._hover = (100, 60)
empty.render()
print("    OK   empty chart handled")

root.destroy()
manager.close()

print("\n" + "=" * 78)
if problems:
    print(f"TEST FAILED ({len(problems)})")
    for p in problems:
        print(f"  - {p}")
else:
    print("HOVER + DISCOVERY TEST PASSED")
print("=" * 78)
raise SystemExit(1 if problems else 0)
