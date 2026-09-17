"""Do the CPU and RAM charts follow the window the way the card graphs do?

The report was that they do not: they were built at a fixed width and right
aligned, so they stayed put while everything around them moved. Measuring the
widgets says whether that is fixed, and says it in pixels rather than in
opinions.

The program is built in-process at two window widths, and the widths of a card
graph and of the CPU, temperature and RAM charts are read at each size.
"""
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

import os
import time
import tkinter as tk

os.environ["GPUMON_CONFIG"] = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "diagresize-config.json")
os.environ["GPUMON_NO_AUTOSTART"] = "1"
DB = "diagresize.db"
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.remove(DB + suffix)

import alarms as A
import metrics as M
import sampler as SP
import store as S
from ui import monitor as MON

problems = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


def widths(app) -> dict[str, int]:
    out = {"window": app.root.winfo_width()}
    for key, spark in app.system_sparks.items():
        out[f"system:{key}"] = spark.winfo_width()
    for index, panel in app.panels.items():
        for row, spec in enumerate(panel.sized.get("rows", [])):
            spark = panel.sparks.get(spec.key)
            if spark is not None and spark.winfo_ismapped():
                out[f"gpu{index}:{spec.key}"] = spark.winfo_width()
                break
    return out


def right_edge(app, key: str) -> int:
    """Where a chart ends on screen, so two of them can be compared.

    Width alone will not do: a card row carries a stats column and the CPU/memory
    panel does not, so its chart starts further left and is wider by exactly that
    column. What should line up is where they end.
    """
    if key.startswith("system:"):
        spark = app.system_sparks.get(key.split(":", 1)[1])
        return spark.winfo_rootx() + spark.winfo_width() if spark else 0
    index, field = key.split(":", 1)
    panel = app.panels.get(int(index.lstrip("gpu")))
    if panel is None:
        return 0
    spark = panel.sparks.get(field)
    return spark.winfo_rootx() + spark.winfo_width() if spark else 0


print("=" * 84)
print("CHART RESIZING TEST")
print("=" * 84)

manager = M.SensorManager(per_core=False)
store = S.Store(DB)
sampler = SP.Sampler(manager, store, sample_hz=4.0)
root = tk.Tk()
app = MON.MonitorApp(root, manager, store, sampler)

sizes = {}
for width in (900, 1500):
    root.geometry(f"{width}x760")
    for _ in range(20):
        root.update()
        time.sleep(0.02)
    root.update_idletasks()
    time.sleep(0.4)
    for _ in range(10):
        root.update()
        time.sleep(0.02)
    sizes[width] = widths(app)
    print(f"\n  window {width} px")
    for name, value in sizes[width].items():
        print(f"    {name:22} {value:5} px")

narrow, wide = sizes[900], sizes[1500]
print()
for key in ("system:cpu_util", "system:cpu_temp", "system:ram_percent"):
    grew = wide.get(key, 0) - narrow.get(key, 0)
    check(f"{key.split(':')[1]} chart grew with the window", grew > 50,
          f"{narrow.get(key)} -> {wide.get(key)} px ({grew:+d})")

card_keys = [key for key in narrow if key.startswith("gpu")]
if card_keys:
    key = card_keys[0]
    grew = wide.get(key, 0) - narrow.get(key, 0)
    check(f"the card graph grew too ({key}), for comparison", grew > 50,
          f"{narrow.get(key)} -> {wide.get(key)} px ({grew:+d})")
    # Width alone is not what to compare: a card row carries a stats column and
    # this panel does not, so its chart starts further left and is wider by exactly
    # that column. What has to line up is where the charts *end*, which is the
    # card's right padding in both cases.
    card_right = right_edge(app, key)
    for name in ("system:cpu_util", "system:ram_percent"):
        difference = abs(right_edge(app, name) - card_right)
        check(f"{name.split(':')[1]} ends level with the card graph",
              difference <= 4, f"right edges differ by {difference} px")
else:
    print("  --  no GPU card here, so there is nothing to compare against")

app.on_close()
sampler.stop()
store.close()
manager.close()
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.remove(DB + suffix)
if os.path.exists("diagresize-config.json"):
    os.remove("diagresize-config.json")

print("\n" + "=" * 84)
if problems:
    print(f"CHART RESIZING TEST FAILED ({len(problems)})")
    for item in problems:
        print(f"  - {item}")
else:
    print("CHART RESIZING TEST PASSED")
print("=" * 84)
raise SystemExit(1 if problems else 0)
