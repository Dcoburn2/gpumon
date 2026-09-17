"""Verify the graphs can be panned back through history.

The live charts draw a fixed time window with the newest sample at the right
edge. Panning slides that window back so a spike seen a minute ago can be read
again - the sampling never stops, the view just stops following it.

What this checks: one shared offset across every chart, the view holding still
while samples keep arriving, the readouts following the *view* rather than the
live edge, the visible "you are looking at history" state, and the way back to
live.
"""

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


_skip_machine_specific_test()




import os
import time
import tkinter as tk
from types import SimpleNamespace

# Keep every setting this test writes out of the user's real config.json.
os.environ.setdefault(
    "GPUMON_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "diaghist-config.json"))

import metrics as M
import sampler as SP
import store as S

DB = "diaghist.db"
for s in ("", "-wal", "-shm"):
    if os.path.exists(DB + s):
        os.remove(DB + s)

problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


print("=" * 88)
print("GRAPH HISTORY TEST")
print("=" * 88)

import hashlib

REAL_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "config.json")


def digest(path: str) -> str:
    if not os.path.exists(path):
        return "(absent)"
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


real_config_before = digest(REAL_CONFIG)

manager = M.SensorManager(per_core=False)
store = S.Store(DB)
sampler = SP.Sampler(manager, store, sample_hz=4.0)
root = tk.Tk()
from ui.monitor import MonitorApp

app = MonitorApp(root, manager, store, sampler)


def pump(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


pump(6.0)                       # collect something to look back at


def every_spark():
    for panel in app.panels.values():
        for spark in panel.sparks.values():
            yield spark
    yield from app.system_sparks.values()


def offsets() -> set[int]:
    return {spark.offset for spark in every_spark()}


def temp_series() -> list[float]:
    panel = app.panels[sorted(app.panels)[0]]
    return panel.sparks["temp"]._series[0].values


print("\n[1] the charts start live")
check("every chart is at the live edge", offsets() == {0}, str(offsets()))
check("the history banner is hidden", app.history_label.cget("text") == "",
      repr(app.history_label.cget("text")))
check("history_live() agrees", app.history_live())

print("\n[2] panning moves every chart by the same amount")
samples = len(temp_series())
print(f"    {samples} samples buffered")
app.pan_history(10)
root.update()
# Read the offset back rather than assuming 10: sampling continues while the
# test runs, and each new sample advances a panned view by one to hold it still.
asked = app._history
check("panning moved the charts back", asked >= 10, f"asked for 10, got {asked}")
check("all charts share the offset", offsets() == {asked}, str(offsets()))
check("history_live() is false", not app.history_live())
banner = app.history_label.cget("text")
seconds = asked / sampler.sample_hz
print(f"    banner: {banner!r} (offset {asked} = {seconds:.1f}s)")
check("the banner says how far back the view is",
      f"-{seconds:.1f}s" in banner or f"-{asked / sampler.sample_hz + 0.05:.1f}s" in banner,
      banner)
check("the banner explains how to resume", "resume live" in banner.lower(), banner)
check("the banner is visible rather than an empty label",
      app.history_label.cget("bg") not in ("", None)
      and app.history_label.cget("bg") != ".")

print("\n[3] the view holds still while samples keep arriving")
# The point of freezing the view: the reading at the right edge of the window
# (and at the left) is the same sample before and after new ones land. Comparing
# the newest values instead would prove nothing - they are *meant* to change.
before_count = len(temp_series())
before_offset = app._history
before_right = temp_series()[-(1 + before_offset)]
before_left = temp_series()[-(1 + before_offset) - 10]
pump(3.0)
after_count = len(temp_series())
after_offset = app._history
after_right = temp_series()[-(1 + after_offset)]
after_left = temp_series()[-(1 + after_offset) - 10]
print(f"    offset {before_offset} -> {after_offset} for "
      f"{after_count - before_count} new samples")
check("the offset grew with the incoming samples",
      after_offset > before_offset, f"{before_offset} -> {after_offset}")
check("one sample of creep per arriving sample",
      after_offset - before_offset == after_count - before_count,
      f"offset +{after_offset - before_offset}, samples +{after_count - before_count}")
check("the right edge of the view is the same sample",
      before_right == after_right, f"{before_right} vs {after_right}")
check("the left edge of the view is the same sample",
      before_left == after_left, f"{before_left} vs {after_left}")
check("every chart still agrees", offsets() == {after_offset}, str(offsets()))

print("\n[4] the readout follows the view, not the live edge")
spark = app.panels[sorted(app.panels)[0]].sparks["temp"]
app.pan_history(8)
# Sampling is stopped for this check so the buffer is frozen: a value that
# could only have come from the live edge is appended, and the badge must still
# report the right edge of the *view* rather than the newest sample.
app.sampler.stop()
pump(0.6)
spark._series[0].append(99.0)
spark.render()
root.update()
badge_texts = [spark.itemcget(item, "text") for item in spark.find_all()
               if spark.type(item) == "text" and spark.itemcget(item, "text")]
series = spark._series[0].values
view_value = series[len(series) - 1 - spark.offset]
print(f"    live={series[-1]}  view={view_value}  texts={badge_texts}")
check("the appended live sample really is the newest", series[-1] == 99.0,
      str(series[-3:]))
check("the view is behind it", view_value != 99.0, str(view_value))
check("the badge shows the value at the right edge of the view",
      f"{view_value:.0f}" in badge_texts,
      f"wanted {view_value:.0f} in {badge_texts}")
check("the badge ignores the newest sample while panned",
      "99" not in badge_texts, str(badge_texts))
series.pop()                      # leave the buffer as it was
app.resume_live()

print("\n[5] the panned chart is drawn in the warning colour")
frames = {spark.itemcget(item, "outline")
          for item in spark.find_all() if spark.type(item) == "rectangle"}
from ui import widgets as W

print(f"    rectangle outlines: {frames}, warn={W.WARN!r}")
check("the frame is tinted while panned", W.WARN in frames, str(frames))

print("\n[6] the way back to live")
app.resume_live()
root.update()
check("every chart is back at the live edge", offsets() == {0}, str(offsets()))
check("the banner is cleared", app.history_label.cget("text") == "",
      repr(app.history_label.cget("text")))
pump(0.6)
check("no stray offset reappears while live", offsets() == {0}, str(offsets()))

print("\n[7] the drag and wheel gestures")
app.pan_history(0)
# The trace follows the pointer: drag left and the samples move left, which
# walks back in time. The first version had this the other way round, so
# grabbing a spike and pulling right pushed it away from the cursor.
app._on_graph_press(SimpleNamespace(x_root=560))
app._on_graph_drag(SimpleNamespace(x_root=500, widget=spark))
root.update()
dragged = app._history
print(f"    dragging 60 px left gave offset {dragged}")
check("dragging left walks back in time", dragged > 0, str(dragged))
app._on_graph_release(None)
app._on_graph_press(SimpleNamespace(x_root=500))
app._on_graph_drag(SimpleNamespace(x_root=560, widget=spark))
root.update()
check("dragging right comes forward again", app._history < dragged,
      f"{dragged} -> {app._history}")

app.resume_live()
wheel = SimpleNamespace(state=0x0001, delta=-120, num=0)
result = app._on_graph_wheel(wheel)
check("shift+wheel pans", app._history > 0, str(app._history))
check("shift+wheel swallows the event so the page does not scroll",
      result == "break", repr(result))
app.resume_live()
plain = SimpleNamespace(state=0, delta=-120, num=0)
check("a plain wheel is left to the page scroller",
      app._on_graph_wheel(plain) is None)
check("a plain wheel did not pan", app._history == 0, str(app._history))

# The CPU and RAM charts sit in a different panel and were easy to forget.
system_spark = next(iter(app.system_sparks.values()))
for sequence in ("<Button-1>", "<B1-Motion>", "<Double-Button-1>",
                 "<MouseWheel>"):
    check(f"the system charts are bound for {sequence}",
          bool(system_spark.bind(sequence)), str(system_spark.bind(sequence))[:40])
check("every graph shows the pan cursor",
      all(spark.cget("cursor") == "fleur" for spark in every_spark()))

print("\n[8] the offset is clamped to what is buffered")
app.pan_history(10 ** 6)
root.update()
limit = app._history_limit()
check("panning past the start stops at the oldest sample",
      app._history == limit, f"{app._history} vs limit {limit}")
check("every chart is clamped together", offsets() == {limit}, str(offsets()))
app.pan_history(-10 ** 6)
check("panning forward past live stops at live", app._history == 0,
      str(app._history))

print("\n[9] a new theme does not lose the pan or the history")
before_history = len(temp_series())
app.pan_history(5)
root.update()
panned = app._history
app.apply_theme("nord")
pump(1.0)
print(f"    {before_history} samples kept across the rebuild, "
      f"offset {panned} -> {app._history}")
check("panning survives a theme rebuild",
      offsets() == {app._history} and app._history > 0, str(offsets()))
check("the graph buffers survive a theme rebuild",
      len(temp_series()) >= before_history - 2,
      f"{before_history} -> {len(temp_series())}")
check("the banner is still shown after the rebuild",
      "HISTORY" in app.history_label.cget("text"),
      repr(app.history_label.cget("text")))
app.resume_live()
app.apply_theme("nvtop")
pump(0.5)

# Every theme switch above wrote settings. They must have gone to the scratch
# file this test pointed GPUMON_CONFIG at, never to the user's config.json -
# which is compared by content rather than by existence, since a user who has
# picked a theme has one and asserting it is absent was only true by accident.
import hashlib

REAL_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "config.json")


def digest(path: str) -> str:
    if not os.path.exists(path):
        return "(absent)"
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


check("the real config.json is unchanged by the theme switches",
      digest(REAL_CONFIG) == real_config_before, REAL_CONFIG)

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
if os.path.exists(os.environ["GPUMON_CONFIG"]):
    os.remove(os.environ["GPUMON_CONFIG"])

# Building the window must not launch a sensor helper.
os.environ.setdefault("GPUMON_NO_AUTOSTART", "1")
check("the scratch config was removed",
      not os.path.exists(os.environ["GPUMON_CONFIG"]))

print("\n" + "=" * 88)
if problems:
    print(f"GRAPH HISTORY TEST FAILED ({len(problems)})")
    for p in problems:
        print(f"  - {p}")
else:
    print("GRAPH HISTORY TEST PASSED")
print("=" * 88)
raise SystemExit(1 if problems else 0)
