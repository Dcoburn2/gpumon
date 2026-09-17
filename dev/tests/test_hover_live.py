"""The hover readout on the live charts, and the processor in the summary.

The live window's charts are `Sparkline`, which had no hover at all: the guide and
the value box existed on another class, so hovering the CPU, temperature and RAM
charts did nothing. This checks a guide line appears with a readable value beside
it, that it follows the pointer, and that it goes away when the pointer does.

It also checks the row added to the summary's Session info, which is built from a
real recorded session.
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
    """Stop, with a note, when there is no desktop to draw a window on."""
    if _os.environ.get("GPUMON_SKIP_MACHINE_TESTS"):
        print("  --  skipped: this test needs a desktop session, and "
              "GPUMON_SKIP_MACHINE_TESTS is set")
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


_skip_machine_specific_test()

import os
import time
import tkinter as tk

os.environ["GPUMON_CONFIG"] = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "diaghover3-config.json")
os.environ["GPUMON_NO_AUTOSTART"] = "1"
DB = "diaghover3.db"
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.remove(DB + suffix)

import alarms as A
import metrics as M
import sampler as SP
import store as S
from ui import monitor as MON
from ui import themes
from ui import widgets as W

problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


def texts_of(spark) -> list[str]:
    return [spark.itemcget(item, "text") for item in spark.find_all()
            if spark.type(item) == "text"]


print("=" * 84)
print("LIVE HOVER AND SUMMARY PROCESSOR TEST")
print("=" * 84)

themes.set_theme("eva-01")
manager = M.SensorManager(per_core=False)
store = S.Store(DB)
sampler = SP.Sampler(manager, store, sample_hz=10.0)
root = tk.Tk()
app = MON.MonitorApp(root, manager, store, sampler)
root.geometry("1400x760")
end = time.monotonic() + 14
while time.monotonic() < end:
    root.update()
    time.sleep(0.02)

print("\n[1] a guide and a value appear over a live chart")
spark = app.system_sparks["cpu_util"]
before = len(spark.find_all())
spark.event_generate("<Motion>", x=int(spark.winfo_width() * 0.97),
                     y=int(spark.winfo_height() / 2))
root.update()
texts = texts_of(spark)
mine = [text for text in texts if text.startswith(("CPU", "RAM", "TEMP"))]
print(f"    items {before} -> {len(spark.find_all())}, text: {texts}")
check("hovering draws something", len(spark.find_all()) > before)
check("and it names the metric with a unit",
      any(text.startswith("CPU ") and text.endswith("%") for text in texts),
      str(mine))

print("\n[2] the guide follows the pointer, and it is a vertical line")
lines_before = [spark.coords(item) for item in spark.find_all()
                if spark.type(item) == "line"]
spark.event_generate("<Motion>", x=int(spark.winfo_width() * 0.80),
                     y=int(spark.winfo_height() / 2))
root.update()
lines_after = [spark.coords(item) for item in spark.find_all()
               if spark.type(item) == "line"]
vertical = [coords for coords in lines_after
            if len(coords) == 4 and abs(coords[0] - coords[2]) < 0.5]
print(f"    vertical lines drawn: {len(vertical)}")
check("a vertical guide is drawn", bool(vertical), str(lines_after[:2]))
check("and it moved with the pointer", lines_before != lines_after)

print("\n[3] and it clears when the pointer leaves")
hovered = len(spark.find_all())
hover_texts = texts_of(spark)
spark.event_generate("<Leave>")
root.update()
after_leave = len(spark.find_all())
# Counting after the fact on both sides compared the same number with itself,
# which is how the first version of this check failed. The count has to be taken
# before the pointer leaves.
check("the hover state is cleared", spark._hover is None, str(spark._hover))
check("the readout is gone", len(texts_of(spark)) < len(hover_texts),
      f"{hover_texts} -> {texts_of(spark)}")
check("and the canvas went back to its unpainted state",
      after_leave < hovered, f"{hovered} -> {after_leave} items")

print("\n[4] a card graph behaves the same way")
panel = next(iter(app.panels.values()))
card_spark = next(iter(panel.sparks.values()))
card_before = len(card_spark.find_all())
card_spark.event_generate("<Motion>", x=int(card_spark.winfo_width() * 0.9),
                          y=int(card_spark.winfo_height() / 2))
root.update()
card_texts = texts_of(card_spark)
check("the card chart draws a readout too",
      len(card_spark.find_all()) > card_before, str(card_texts))
check("with a unit on the value",
      any("\u00b0C" in text or "%" in text or "W" in text or "MHz" in text
          for text in card_texts), str(card_texts))

print("\n[5] the summary names the processor")
sampler.start()
sampler.start_logging("hover+summary test")
time.sleep(6)
summary = sampler.stop_logging()
from ui.summary import SummaryWindow  # noqa: E402

window = SummaryWindow(root, store, summary.session_id, W.Theme(),
                       devices=manager.gpus, on_close=lambda _s: None)
root.update()
labels: list[str] = []


def collect(widget) -> None:
    try:
        value = widget.cget("text")
        if value:
            labels.append(str(value))
    except tk.TclError:
        pass
    for child in widget.winfo_children():
        collect(child)


collect(window.win)
check("Session info has a Processor row", "Processor" in labels)
import cpusensors  # noqa: E402
expected = cpusensors.processor_name()
check("and it names this machine's processor", expected in labels,
      f"{expected!r} in {len(labels)} labels")

print("\n[6] the summary's charts hover as well")
charts = []


def find_charts(widget) -> None:
    if isinstance(widget, W.TimeSeriesChart):
        charts.append(widget)
    for child in widget.winfo_children():
        find_charts(child)


find_charts(window.win)
print(f"    charts: {len(charts)}")
if charts:
    chart = charts[0]
    chart_before = len(chart.find_all())
    chart.event_generate("<Motion>", x=int(chart.winfo_width() * 0.55),
                         y=int(chart.winfo_height() * 0.5))
    root.update()
    check("a summary chart draws a guide", len(chart.find_all()) > chart_before,
          f"{chart_before} -> {len(chart.find_all())}")
else:
    check("the summary has charts to hover", False)

window.close() if hasattr(window, "close") else None
app.on_close()
sampler.stop()
store.close()
manager.close()
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.remove(DB + suffix)
if os.path.exists("diaghover3-config.json"):
    os.remove("diaghover3-config.json")

print("\n" + "=" * 84)
if problems:
    print(f"LIVE HOVER AND SUMMARY PROCESSOR TEST FAILED ({len(problems)})")
    for item in problems:
        print(f"  - {item}")
else:
    print("LIVE HOVER AND SUMMARY PROCESSOR TEST PASSED")
print("=" * 84)
raise SystemExit(1 if problems else 0)
