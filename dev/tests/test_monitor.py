"""Verify the reworked live monitor: collapsible cards, stats, richer graphs."""

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


_skip_machine_specific_test()




import os
import time
import tkinter as tk

import metrics as M
import sampler as SP
import store as S
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

# Building the window must not launch a sensor helper.
os.environ.setdefault("GPUMON_NO_AUTOSTART", "1")

DB = "diagmon.db"
for s in ("", "-wal", "-shm"):
    if os.path.exists(DB + s):
        os.remove(DB + s)

problems: list[str] = []
print("=" * 88)
print("LIVE MONITOR TEST")
print("=" * 88)

manager = M.SensorManager(per_core=True)
store = S.Store(DB)
sampler = SP.Sampler(manager, store, sample_hz=2.0, per_core=True)
root = tk.Tk()
from ui.monitor import MonitorApp

app = MonitorApp(root, manager, store, sampler)


def pump(seconds: float) -> None:
    """Let Tk run normally; the app schedules its own redraws."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


pump(10.0)


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


print(f"\npanels: {sorted(app.panels)}")
panel = app.panels[0]

print("\n[1] per-row context: value + min/avg/max + graph")
for field in panel.value_labels:
    value = panel.value_labels[field].cget("text")
    stats = panel.stats_labels[field].cget("text")
    items = len(panel.sparks[field].find_all())
    print(f"    {field:14} value={value!r:26} stats={stats!r:42} items={items}")
    check(f"{field} has a value", value not in ("", "--"), repr(value))
    check(f"{field} has min/avg/max", "min" in stats and "max" in stats, repr(stats))
    check(f"{field} graph drew", items > 0, f"{items} items")

print("\n[2] graphs carry bands and thresholds")
spark = panel.sparks["temp"]
print(f"    temp bands={spark._bands} thresholds={spark._thresholds}")
check("temperature graph has a warning zone", bool(spark._bands))
check("temperature graph has a threshold line", bool(spark._thresholds))
vram_spark = panel.sparks["vram_percent"]
check("vram graph has a threshold line", bool(vram_spark._thresholds))

print("\n[3] collapse / expand")
body_visible = bool(panel.body.winfo_ismapped())
app.toggle_card(0)
root.update()
check("card collapses", panel.collapsed and not panel.body.winfo_ismapped())
pump(0.5)   # let the app's own redraw fill in the headline
check("collapsed card shows a summary line",
      any(ch.isdigit() for ch in panel.summary_label.cget("text")),
      repr(panel.summary_label.cget("text")))
app.toggle_card(0)
root.update()
pump(0.5)
check("card expands again", not panel.collapsed and bool(panel.body.winfo_ismapped()))
check("headline cleared when expanded", panel.summary_label.cget("text") == "",
      repr(panel.summary_label.cget("text")))

print("\n[4] collapse-all toggles every card")
app.toggle_all_cards()
root.update()
all_collapsed = all(p.collapsed for p in app.panels.values())
print(f"    all collapsed: {all_collapsed}")
check("collapse all works", all_collapsed)
app.toggle_all_cards()
root.update()
check("expand all works", all(not p.collapsed for p in app.panels.values()))

print("\n[5] button and hotkey are wired")
labels = [w.cget("text") for w in app.btn_log.master.winfo_children()
          if hasattr(w, "cget")]
print(f"    header buttons: {labels}")
check("the header keeps its action buttons",
      any("SUMMARY" in str(t) for t in labels)
      and any("GRAPHS" in str(t) for t in labels))
check("no COLLAPSE button in the header",
      not any("COLLAPSE" in str(t) for t in labels), str(labels))
check("collapsing still works from the card and the C hotkey",
      bool(root.bind("<Key-c>")) and app.btn_collapse is None)

print("\n[6] window resize keeps graphs usable")
root.geometry("1500x900")
root.update()
pump(1.0)
widths = {f: s.winfo_width() for f, s in panel.sparks.items()}
print(f"    graph widths after resize: {widths}")
check("graphs have non-zero width", all(w > 50 for w in widths.values()))

print("\n[7] the card never calls itself sensorless while printing a temperature")
# The desktop window used to derive "no thermal sensor available" from the
# backend list, which knew only about NVML and LibreHardwareMonitor. Both V620s
# read their temperature through ADL, so each card claimed to have no sensor on
# the line directly under its own "hotspot 34 C  mem 34 C".
for index, panel_i in sorted(app.panels.items()):
    gpu = next((g for g in app.caps.gpus if g.index == index), None)
    sub = panel_i.temp_label._sub.cget("text")
    reading = panel_i.temp_label.cget("text")
    print(f"    gpu{index} {reading!r:9} sub={sub!r} "
          f"sources={gpu.sources if gpu else None}")
    claimed_sensorless = "no thermal sensor" in sub
    has_number = any(ch.isdigit() for ch in reading)
    check(f"gpu{index} does not contradict its own reading",
          not (claimed_sensorless and has_number), repr(sub))
    if gpu and reading not in ("", "--", "n/a"):
        check(f"gpu{index} is credited with a temperature source",
              gpu.has_temperature, f"sources={gpu.sources}")

print("\n[8] switching theme rebuilds the window in the new colours")
# The window is rebuilt rather than restyled in place, so this proves the
# rebuild path: the theme is persisted, the widgets come back in the new
# palette, and the sampler keeps feeding the rebuilt panels.
from ui import themes as TH
from ui.monitor import field_color

import alarms as A

# Keep every setting this test writes out of the user's real config.json.
os.environ.setdefault(
    "GPUMON_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "diagmon-config.json"))

saved_config: dict = {}
_real_load, _real_save = A.load_config, A.save_config
A.load_config = lambda path=None: {}                      # never touch the user's file
A.save_config = lambda cfg, path=None: saved_config.update(cfg)
config_before = os.path.exists(A.config_path())
before_labels = app.panels[0].temp_label
before_bg = app.root.cget("bg")
target = "eva-02" if TH.current_key() != "eva-02" else "nord"
app.apply_theme(target)
pump(3.0)

palette = TH.get(target)
print(f"    {before_bg} -> {app.root.cget('bg')} (expected {palette.bg})")
check("the root window took the new background", app.root.cget("bg") == palette.bg)
check("every card was rebuilt", len(app.panels) == len(app.caps.gpus),
      f"{len(app.panels)} panels for {len(app.caps.gpus)} devices")
check("the panels are new widgets",
      app.panels[0].temp_label is not before_labels)
spark = app.panels[0].sparks["temp"]
check("graphs draw on the theme's plot background",
      spark.cget("bg") == palette.panel)
check("the legend chip names the row with a theme colour",
      bool(spark._legend) and spark._legend[0][1] == field_color("temp"),
      str(spark._legend[:1]))
items = len(spark.find_all())
check("the graph redrew after the rebuild", items > 0, f"{items} items")
check("the theme was persisted", saved_config.get("theme") == target,
      str(saved_config))
value = app.panels[0].value_labels.get("temp")
check("live values still update after the rebuild",
      value is not None and value.cget("text") not in ("", "--"),
      value.cget("text") if value is not None else "no label")
app.apply_theme("nvtop")
pump(1.0)
check("switching back works", app.root.cget("bg") == TH.get("nvtop").bg)

# The real save function is restored only now: an earlier version restored it
# before switching back, which wrote a config.json into the project directory
# from a test run. Assert the file is untouched rather than trusting the patch.
A.load_config, A.save_config = _real_load, _real_save
check("the test did not create or modify config.json",
      os.path.exists(A.config_path()) == config_before,
      A.config_path())

print("\n[9] in-graph numbers read cleanly")
# The scale numbers, the legend chip and the value badge all share one compact
# formatter. It printed the bottom of every axis as "0.00" next to "100" and
# "50", which reads like a different unit, so the axis labels are inspected.
panel = app.panels[0]
labels: list[str] = []
for field, spark in panel.sparks.items():
    texts = [spark.itemcget(item, "text") for item in spark.find_all()
             if spark.type(item) == "text" and spark.itemcget(item, "text")]
    labels.extend(texts)
    print(f"    {field:14} {texts}")
    check(f"{field}: the axis has readable numbers",
          any(t.replace("k", "").replace(".", "").isdigit() for t in texts),
          str(texts))
check("no axis label is written as a decimal zero",
      not any(t in ("0.00", "0.0") for t in labels), str(labels))
check("the legend chip carries a value",
      any(" " in t for t in labels), str(labels[:2]))

print("\n[10] the CPU card's per-core toggle")
# Per-core used to be a launch flag, so looking at the cores meant restarting
# and losing the run. The button flips the backend, which is read at poll time,
# so the very next sample carries the cores - and the sampler, buffer and store
# take whatever the poll returns, so a session that gains them halfway records
# both halves.
backend = app.manager.system
saved_per_core: dict = {}
_real_load2, _real_save2 = A.load_config, A.save_config
A.load_config = lambda path=None: {}
A.save_config = lambda cfg, path=None: saved_per_core.update(cfg)
before_cores = [k for k in (app._last_values or {})
                if k.startswith("core") and k.endswith("_util")]
print(f"    cores before: {len(before_cores)}   button: "
      f"{app.btn_cores.cget('text')!r}   backend.per_core={backend.per_core}")
check("per-core starts off unless --per-core was passed",
      len(before_cores) == 0 or backend.per_core,
      f"{len(before_cores)} core metrics, flag {backend.per_core}")
app.toggle_per_core(force=True)
pump(2.5)
cores = [k for k in (app._last_values or {})
         if k.startswith("core") and k.endswith("_util")]
print(f"    cores after : {len(cores)}   button: {app.btn_cores.cget('text')!r}")
check("turning it on produces one metric per logical CPU", len(cores) >= 2,
      f"{len(cores)} core metrics")
check("the flag reached every layer",
      backend.per_core and app.manager.per_core and app.sampler.per_core)
check("the button says which way it is set",
      "ON" in app.btn_cores.cget("text"), app.btn_cores.cget("text"))
check("the cores are drawn", len(app.core_canvas.find_all()) > 0,
      f"{len(app.core_canvas.find_all())} canvas items")
app.toggle_per_core(force=False)
pump(2.0)
cores_after = [k for k in (app._last_values or {})
               if k.startswith("core") and k.endswith("_util")]
check("turning it off stops the metrics", not cores_after, str(cores_after[:3]))
check("the choice was saved for the next run",
      saved_per_core.get("per_core") is False, str(saved_per_core))
A.load_config, A.save_config = _real_load2, _real_save2
app.toggle_per_core(force=False)          # leave it off for the rest of the test
pump(0.4)

print("\n[11] G hides the CPU and RAM graphs too")
# Leaving them on is what made the toggle look broken: the card graphs vanished
# and the CPU/RAM/TEMP charts stayed.
app.toggle_graphs(force=False)
pump(1.2)
system_mapped = {k: bool(s.winfo_ismapped()) for k, s in app.system_sparks.items()}
card_mapped = any(s.winfo_ismapped() for p in app.panels.values()
                  for s in p.sparks.values())
print(f"    graphs off: system={system_mapped} cards={card_mapped}")
check("no card graph is left showing", not card_mapped)
check("no CPU/RAM graph is left showing", not any(system_mapped.values()),
      str(system_mapped))
app.toggle_graphs(force=True)
pump(1.2)
check("both come back",
      all(s.winfo_ismapped() for s in app.system_sparks.values())
      and any(s.winfo_ismapped() for p in app.panels.values()
              for s in p.sparks.values()))

print("\n[12] hotspot shares the temperature chart")
# The reference the request came from - the GPU-Z panel inside FurMark - draws
# temperature, hotspot and utilisation as three series in one chart with a
# three-row legend. This is that grouping.
for index, panel_i in sorted(app.panels.items()):
    gpu = next(g for g in app.caps.gpus if g.index == index)
    series = [s.key for s in panel_i.sparks["temp"]._series]
    legend = panel_i.sparks["temp"]._legend
    sub = panel_i.temp_label._sub.cget("text") if hasattr(
        panel_i.temp_label, "_sub") else ""
    print(f"    gpu{index} {gpu.display_name}: series={series} "
          f"legend={[t for t, _c in legend]} sub={sub!r} sources={gpu.sources}")
    check(f"gpu{index}: the temperature chart draws the temperature and nothing "
          f"else", series == ["temp"], str(series))
    check(f"gpu{index}: no hotspot trace is drawn",
          not any("hotspot" in text for text, _c in legend), str(legend))
    if gpu.has_hotspot:
        check(f"gpu{index}: the hotspot is still reported as text",
              "hotspot" in sub.lower(), sub)

print("\n[13] there is no second card layout any more")
# "Combined" was added to match a reference screenshot and then rejected: five
# traces on one plot is harder to read than five small ones. Removed, not
# switched off, so nothing should offer it.
check("the module no longer defines a combined layout",
      not [name for name in ("COMBINED_LAYOUT", "ROWS_LAYOUT", "LAYOUTS")
           if hasattr(__import__("ui.monitor", fromlist=["x"]), name)])
check("no layout button in the header",
      not hasattr(app, "btn_layout"), "app.btn_layout")
check("the app has no layout toggle", not hasattr(app, "toggle_layout"))
from ui.monitor import GPU_ROW_FIELDS as _CARD_ROWS  # noqa: E402
check("cards show one chart per metric, and nothing else",
      all([r.key for r in p.sized["rows"]] == [f[0] for f in _CARD_ROWS]
          for p in app.panels.values()),
      str({i: [r.key for r in p.sized["rows"]]
           for i, p in app.panels.items()}))
check("every chart carries exactly one series",
      all(len(s._series) == 1 for p in app.panels.values()
          for s in p.sparks.values()))
check("V does not switch layouts", not hasattr(app, "_layout"))
labels = [w.cget("text") for w in app.btn_log.master.winfo_children()
          if hasattr(w, "cget")]
check("the header does not advertise a layout",
      not any("ROWS" in str(t) or "COMBINED" in str(t) for t in labels),
      str(labels))

print("\n[14] the CPU card reads CPU, TEMP, RAM")
# Processor load and processor heat belong beside each other; memory is the one
# of the three that is not about the CPU. Requested after using it the other way
# round.
order = list(app.system_sparks)
print(f"    rows in order: {order}")
check("temperature sits between the CPU and the memory rows",
      order == ["cpu_util", "cpu_temp", "ram_percent"], str(order))
grid_rows = {key: int(app.system_sparks[key].grid_info().get("row", -1))
             for key in order}
check("and it is drawn in that order on the grid",
      grid_rows["cpu_util"] < grid_rows["cpu_temp"] < grid_rows["ram_percent"],
      str(grid_rows))

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
scratch = os.environ.get("GPUMON_CONFIG", "")
if scratch and os.path.exists(scratch):
    os.remove(scratch)
check("no scratch config is left behind",
      not scratch or not os.path.exists(scratch), scratch)

print("\n" + "=" * 88)
if problems:
    print(f"MONITOR TEST FAILED ({len(problems)})")
    for p in problems:
        print(f"  - {p}")
else:
    print("MONITOR TEST PASSED")
print("=" * 88)
raise SystemExit(1 if problems else 0)
