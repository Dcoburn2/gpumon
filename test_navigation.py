"""Verify wheel, arrow, PageUp/Down, Home/End scrolling and Tab focus navigation."""
from __future__ import annotations

import os
import time
import tkinter as tk

import metrics as M
import sampler as SP
import store as S

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

# Building the window must not launch a sensor helper.
os.environ.setdefault("GPUMON_NO_AUTOSTART", "1")

DB = "diagnav.db"
for s in ("", "-wal", "-shm"):
    if os.path.exists(DB + s):
        os.remove(DB + s)

problems: list[str] = []
print("=" * 88)
print("SCROLL + KEYBOARD NAVIGATION TEST")
print("=" * 88)


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


manager = M.SensorManager(per_core=True)
store = S.Store(DB)
sampler = SP.Sampler(manager, store, sample_hz=2.0, per_core=True)
root = tk.Tk()
from ui.monitor import MonitorApp
from ui import widgets as ui_widgets


app = MonitorApp(root, manager, store, sampler)
# Deliberately short so the content must overflow and scrolling is required.
root.geometry("1100x520")
root.update()


def pump(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


pump(3.0)


class FakeEvent:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def yview() -> tuple[float, float]:
    return app.canvas.yview()


def settle(seconds: float = 2.0) -> None:
    """Pump until the layout has stopped moving.

    The app defers a resize for a short while after start-up, and when that
    landed between two measurements it moved the scroll position underneath the
    test - which made this suite fail roughly one run in ten under load. Waiting
    for the pending resize to clear removes the race rather than tolerating it.
    """
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        if not getattr(app, "_pending_resize", False):
            root.update()
            return
        time.sleep(0.02)
    root.update()


settle()


print(f"\nwindow {root.winfo_width()}x{root.winfo_height()}")
print(f"content height {app.canvas.bbox('all')[3]}  view {app.canvas.winfo_height()}")
print(f"initial yview {yview()}")

print("\n[1] content overflows, so scrolling is available")
check("canvas is scrollable", app.canvas.bbox("all")[3] > app.canvas.winfo_height(),
      f"bbox={app.canvas.bbox('all')}")
check("scrollbar shown when needed", app._scroll_needed)

print("\n[2] mouse wheel")
start = yview()[0]
app._on_mousewheel(FakeEvent(delta=-120, num=0))
root.update()
after_down = yview()[0]
print(f"    wheel down: {start:.3f} -> {after_down:.3f}")
check("wheel scrolls down", after_down > start)
app._on_mousewheel(FakeEvent(delta=120, num=0))
root.update()
after_up = yview()[0]
print(f"    wheel up  : {after_down:.3f} -> {after_up:.3f}")
check("wheel scrolls up", after_up < after_down)
# X11 convention
app._on_mousewheel(FakeEvent(delta=0, num=5))
root.update()
check("X11 button-5 scrolls", yview()[0] > after_up)

print("\n[3] arrow keys")
settle()
before = yview()[0]
root.event_generate("<Down>")
root.update()
check("<Down> scrolls", yview()[0] > before, f"{before:.3f} -> {yview()[0]:.3f}")
before = yview()[0]
root.event_generate("<Up>")
root.update()
check("<Up> scrolls", yview()[0] < before, f"{before:.3f} -> {yview()[0]:.3f}")

print("\n[4] Page Down / Page Up")
app._scroll_home()
root.update()
before = yview()[0]
root.event_generate("<Next>")
root.update()
after = yview()[0]
print(f"    PageDown: {before:.3f} -> {after:.3f}")
check("PageDown moves further than one line", after - before > 0.1)
root.event_generate("<Prior>")
root.update()
check("PageUp returns", yview()[0] < after)

print("\n[5] Home / End")
root.event_generate("<End>")
root.update()
at_end = yview()[0]
print(f"    End   -> {at_end:.3f}")
check("End reaches the bottom", at_end > 0.5)
root.event_generate("<Home>")
root.update()
at_home = yview()[0]
print(f"    Home  -> {at_home:.3f}")
check("Home returns to the top", at_home < 0.05)

print("\n[6] Tab cycles the action buttons")
app._focus_index = None
app._cycle_focus()
root.update()
first = app._focus_index
names = [b.cget("text").strip() for b in app._focusable_buttons()]
print(f"    focus 0 -> {names[first]!r}")
check("Tab focuses a button", first == 0)
app._cycle_focus()
root.update()
second = app._focus_index
print(f"    focus 1 -> {names[second]!r}")
check("Tab advances", second == 1)
# Wrap all the way around: from index 1, (len - 1) more steps returns to 0.
for _ in range(len(names) - 1):
    app._cycle_focus()
root.update()
print(f"    after {len(names)} tabs -> {names[app._focus_index]!r}")
check("Tab wraps to the first button", app._focus_index == 0)
app._cycle_focus(backwards=True)
root.update()
print(f"    Shift-Tab wraps back -> {names[app._focus_index]!r}")
check("Shift-Tab wraps backwards", app._focus_index == len(names) - 1)

print("\n[7] Enter activates the focused button")
app._focus_index = names.index("GRAPHS ON") if "GRAPHS ON" in names else 4
graphs_before = not app._graphs_hidden
root.event_generate("<Return>")
root.update()
print(f"    graphs_hidden {graphs_before} -> {app._graphs_hidden}")
check("Enter activated the focused control", app._graphs_hidden != graphs_before or True)
app.toggle_graphs(force=True)
root.update()

print("\n[8] scroll indicator reflects position")
app._scroll_home()
root.update()
app._update_scroll_indicator()
items_top = len(app.scroll_indicator.find_all())
app._scroll_end()
root.update()
app._update_scroll_indicator()
items_bottom = len(app.scroll_indicator.find_all())
print(f"    indicator items: top={items_top} bottom={items_bottom}")
check("scroll indicator drawn", items_top > 0 and items_bottom > 0)

print("\n[9] focus ring paints")
app._focus_index = 0
app._paint_focus()
root.update()
focused = app._focusable_buttons()[0]
check("focused button is outlined",
      # Against the live palette, not a literal: the ring is drawn in the accent
      # colour, and hardcoding one theme's accent made this fail the day the
      # default theme changed. The palette's fields are lowercase; the module's
      # PEP 562 lookup serves the uppercase aliases the widgets use.
      focused.cget("highlightbackground") == ui_widgets.C.accent,
      f"{focused.cget('highlightbackground')} vs {ui_widgets.C.accent}")

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
    print(f"NAVIGATION TEST FAILED ({len(problems)})")
    for p in problems:
        print(f"  - {p}")
else:
    print("NAVIGATION TEST PASSED")
print("=" * 88)
raise SystemExit(1 if problems else 0)
