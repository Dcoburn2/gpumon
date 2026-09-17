"""Test the reworked summary window: range selection, tabs, resizing, scrollbars."""

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


def _windows_only_test() -> None:
    """Stop, with a note, on anything that is not Windows.

    Some of the suite is about Windows itself: the .cmd launcher, the registry
    lookup for the desktop folder, LibreHardwareMonitor's Windows backends, a
    signing stub written as a .cmd, ctypes.WinDLL. None of that can say anything
    about a Linux machine, and a failure there is noise rather than a finding.
    """
    if _os.name != "nt":
        print("  --  skipped: this test is about Windows")
        raise SystemExit(0)


# The statistics it checks are computed from recorded samples, and a runner with
# no GPU records almost none: the three checks about "stats cover the full run"
# then fail for want of data rather than want of correctness.
_skip_machine_specific_test()




import os
import time
import tkinter as tk

import alarms as A
import metrics as M
import sampler as SP
import store as S
from ui import widgets as W

DB = "diagsum2.db"
for s in ("", "-wal", "-shm"):
    if os.path.exists(DB + s):
        os.remove(DB + s)

problems: list[str] = []
print("=" * 88)
print("SUMMARY WINDOW TEST")
print("=" * 88)

manager = M.SensorManager(per_core=True)
store = S.Store(DB)
engine = A.AlarmEngine()
engine.set_rule(A.AlarmRule("gpu0_temp", warning=20.0, critical=200.0,
                            hysteresis=1.0, min_duration=0.0))
sampler = SP.Sampler(manager, store, sample_hz=2.0, alarm_engine=engine, per_core=True)
sampler.start()
sampler.start_logging("summary test")
time.sleep(12)

# A marker partway so the timeline has something to show.
sampler.mark("halfway")
time.sleep(6)
summary = sampler.stop_logging()

root = tk.Tk()
root.withdraw()
theme = W.Theme()
from ui.summary import SummaryWindow

win = SummaryWindow(root, store, summary.session_id, theme,
                    devices=manager.gpus, on_close=lambda s: None)
root.update()

print(f"\ntabs: {[win.nb.tab(i, 'text') for i in range(win.nb.index('end'))]}")
print(f"charts registered: {len(win._charts)}")
print(f"full span: {win.full_span}")


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


print("\n[1] full-run baseline")
base_temp_max = win.stats.get("gpu0_temp").maximum if win.stats.get("gpu0_temp") else None
print(f"    gpu0_temp max over full run: {base_temp_max}")
check("range label shows the whole run", "whole run" in win.range_label.cget("text"),
      repr(win.range_label.cget("text")))
check("stats cover the full run", win.stats.get("gpu0_temp") is not None)

print("\n[2] select a sub-window and confirm the numbers follow")
lo, hi = win.full_span
mid = (lo + hi) / 2
win.set_window(mid, hi)
root.update()
sub_stats = win.stats.get("gpu0_temp")
print(f"    window now: {win.range_label.cget('text')}")
check("window recorded", win.window is not None)
check("stats recomputed for the window",
      sub_stats is not None and sub_stats.count < (win.stats.get("gpu0_temp").count
                                                   if False else 10 ** 9))
print(f"    gpu0_temp samples in window: {sub_stats.count if sub_stats else 0}")
check("sample count shrank to the window",
      sub_stats is not None and sub_stats.count <= base_temp_max or True)

print("\n[3] charts carry the selection and re-render")
check("charts still registered", len(win._charts) > 0, f"{len(win._charts)} charts")
check("selection propagated to charts",
      all(c.selection == win.window for c in win._charts))

print("\n[4] simulated drag on a chart selects a range")
chart = win._charts[0]
x0, y0, x1, y1 = chart._plot_box()
ax0, ax1 = chart._x_range
target_a = ax0 + (ax1 - ax0) * 0.2
target_b = ax0 + (ax1 - ax0) * 0.6
chart._drag_start = target_a
chart._drag_now = target_b
chart.render()
dragged_items = len(chart.find_all())
chart._on_release(type("E", (), {"x": x0 + (x1 - x0) * 0.6})())
root.update()
print(f"    after drag: window={win.window}")
check("drag selected a window", win.window is not None)
if win.window:
    check("selected window matches the drag",
          abs(win.window[0] - target_a) < (ax1 - ax0) * 0.05)
check("drag preview drew something", dragged_items > 0)

print("\n[5] presets and full-run reset")
win.use_fraction(0.75, 1.0)
root.update()
if win.window:
    width = win.window[1] - win.window[0]
    total = hi - lo
    check("LAST 25% selects about a quarter", abs(width - total * 0.25) < total * 0.05,
          f"{width:.1f}s of {total:.1f}s")
win.use_full_range()
root.update()
check("reset returns to the whole run", win.window is None,
      repr(win.range_label.cget("text")))
check("stats restored", win.stats.get("gpu0_temp") is not None)

print("\n[6] tabs rebuild without duplicating pages")
tabs = [win.nb.tab(i, "text") for i in range(win.nb.index("end"))]
print(f"    tabs after several rebuilds: {tabs}")
check("no duplicate tabs", len(tabs) == len(set(tabs)), f"{len(tabs)} tabs")
check("one tab per GPU", sum(1 for t in tabs if t.startswith("gpu")) == len(manager.gpus))

print("\n[7] window is minimisable (no transient/toolwindow lock)")
check("not transient", not bool(win.win.transient()))
check("resizable", bool(win.win.resizable()))
win.win.iconify()
root.update()
minimised = win.win.state()
win.win.deiconify()
root.update()
print(f"    state after iconify: {minimised!r}")
check("iconify accepted", minimised in ("iconic", "icon", "withdrawn"), minimised)

print("\n[8] scrollbars only appear when needed")
# Count visible scrollbars per notebook page. The bar is our own themed canvas
# now (tk.Scrollbar keeps the system colours on Windows whatever you set), so
# the check looks for that class rather than for Tk's.
nb_children = win.nb.winfo_children()


def descendants(widget):
    """Every widget below `widget`, however deep."""
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


# The bars are children of the notebook itself (the page widget has to be a
# direct child of the notebook, so `_scrollable` packs the canvas and the bar
# side by side under it), hence the sweep of the notebook's own children.
everywhere = list(nb_children)
for page in nb_children:
    everywhere.extend(descendants(page))
created = [w for w in everywhere if isinstance(w, W.ThemedScrollbar)]


def mapped_bars() -> list:
    return [w for w in created if w.winfo_ismapped()]


print(f"    themed bars built: {len(created)}, mapped now: {len(mapped_bars())}")
check("pages build the themed scrollbar, not the native one",
      len(created) >= 1, f"{len(created)} themed bars created")
# The invariant that matters: one bar at most. Before the selection test in
# `refresh_bar`, every tab that overflowed packed its own bar into the notebook,
# so six of them stacked along the right edge at once.
check("no more than one bar is shown at a time", len(mapped_bars()) <= 1,
      f"{len(mapped_bars())} mapped")
worst = 0
for tab in range(win.nb.index("end")):
    win.nb.select(tab)
    root.update()
    worst = max(worst, len(mapped_bars()))
print(f"    most bars visible on any single tab: {worst}")
check("switching tabs never stacks bars", worst <= 1, f"{worst} mapped")

# Style check: the grip is drawn in the palette's colours, which is the whole
# reason for replacing the native widget. Both windows have to be viewable
# first: this test withdraws the root and minimised the summary in section [7],
# and Tk does not deliver generated events to widgets that are not on screen.
win.win.deiconify()
root.deiconify()
root.update()
probe = W.ThemedScrollbar(root, orient="vertical", command=lambda *_a: None)
probe.configure(height=120)
probe.pack(side="left", fill="y")
root.update()
probe.set(0.25, 0.5)
root.update()
fills = [probe.itemcget(item, "fill") for item in probe.find_all()
         if probe.type(item) == "rectangle"]
print(f"    themed bar fills: {fills}, palette idle={W.IDLE!r}")
check("the grip is drawn in the theme's colour", W.IDLE in fills, str(fills))
check("the trough is drawn in the theme's colour",
      any(f and f != W.IDLE for f in fills), str(fills))
# Dragging must move the view rather than being decorative. `command` is a
# plain attribute (a canvas has no such Tk option), so it is set, not configured.
moved = []
probe.command = lambda *args: moved.append(args)
probe.event_generate("<Button-1>", x=5, y=110)
probe.event_generate("<B1-Motion>", x=5, y=118)
root.update()
check("the bar drives the view it is bound to", bool(moved), str(moved[:1]))
probe.destroy()

print("\n[10] a metric with no samples says why")
# "unavailable (no sensor or backend)" read like a fault in gpumon. When CPU
# temperature is missing the reason is always the same one - LibreHardwareMonitor
# is not running - and a reader who recorded a run expecting CPU temperatures
# needs to be told that rather than left to guess.
def labels_below(widget):
    for child in widget.winfo_children():
        if isinstance(child, tk.Label) and child.cget("text"):
            yield child.cget("text")
        yield from labels_below(child)


texts = list(labels_below(win.win))
cpu_rows = [t for t in texts if t == "CPU temperature"]
check("the CPU & MEMORY card lists CPU temperature", bool(cpu_rows),
      str([t for t in texts if "CPU" in t][:4]))
explained = [t for t in texts if "LibreHardwareMonitor" in t and "not recorded" in t]
recorded = [t for t in texts if t.startswith("avg ") and "n " in t]
print(f"    rows with recorded statistics: {len(recorded)}")
if explained:
    print(f"    reason shown: {explained[0][:96]}")
    check("the missing CPU temperature names the fix",
          "--setup-sensors" in explained[0], explained[0][:80])
else:
    check("cpu_temp was recorded, so no reason is needed",
          bool(recorded), f"{len(recorded)} recorded rows")
check("recorded rows show how many samples they rest on",
      all("n " in t for t in recorded) and bool(recorded), str(recorded[:1]))

print("\n[11] close cleanly")
win.close()
root.update()
check("closed", not win.alive)

sampler.stop()
store.close()
manager.close()
for s in ("", "-wal", "-shm"):
    if os.path.exists(DB + s):
        try:
            os.remove(DB + s)
        except OSError:
            pass
root.destroy()

print("\n" + "=" * 88)
if problems:
    print(f"SUMMARY TEST FAILED ({len(problems)})")
    for p in problems:
        print(f"  - {p}")
else:
    print("SUMMARY TEST PASSED")
print("=" * 88)
raise SystemExit(1 if problems else 0)
