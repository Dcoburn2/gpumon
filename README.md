# gpumon

[![release](https://github.com/Dcoburn2/gpumon/actions/workflows/release.yml/badge.svg)](https://github.com/Dcoburn2/gpumon/actions/workflows/release.yml)
[![licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![privacy](https://img.shields.io/badge/privacy-nothing%20collected-brightgreen.svg)](PRIVACY.md)

A GPU / system performance logger, in four flavours that all read the same
sensors and write the same database:

| | What it is | Launch |
|---|---|---|
| **Desktop** | nvtop-style live window with graphs, buttons and tabs | `gpumon` |
| **Terminal** | full-screen TUI, arrow-key navigable, no dependencies | `gpumon tui` |
| **Browser** | local web server + GUI, open from any device on the network | `gpumon web` |
| **Reports** | self-contained HTML / text summaries and CSV export | from any of the above |

Pick whichever suits the machine. The desktop window needs tkinter; the terminal
view needs nothing but a terminal; the browser view needs only a browser.

---

## Quick start

```powershell
gpumon              # desktop window  (or: python gpumon.py)
gpumon tui          # terminal view
gpumon web          # browser GUI at http://127.0.0.1:8080
```

The live view starts sampling immediately. Press **L** (or click START LOGGING),
give the run a label, then launch your benchmark. Press **L** again to stop; the
summary opens automatically.

### Building and shipping

```powershell
python scripts/make_release.py --all      # every artifact, from one source tree
```

| Artifact | Size | Start-up | For |
|---|---|---|---|
| `release/` | 30.1 MB folder | 3.3 s to a window | the normal download |
| `gpumon-1.0.0-windows-x64.zip` | 14.9 MB | — | a download link |
| `release-onefile/gpumon.exe` | 14.6 MB, one file | 3.7 s to a window | handing somebody a single file |

Measured on this machine, best of three, from launch to a visible window. The
single file unpacks itself into a temporary folder at each start, which costs
about 0.4 s — worth knowing before choosing, and worth measuring rather than
assuming: the first attempt at that comparison watched the process it launched,
found no window and reported sixty seconds, because the one-file bootloader runs
the real program as a child. It was the measurement that was wrong.

**What a release never contains.** `scripts/make_release.py` refuses to leave tests or
run-time files in the folder — a `config.json` from the build machine would hand
a new user somebody else's settings, and would make the first-run shortcut
question look already answered. It also carries no third-party binaries: PawnIO's
driver and its signed modules are GPL-2.0 and this project is MIT, so
`--setup-sensors` fetches them from the vendor's own releases instead of putting
them in the distribution.

| In `release/` | What it is |
|---|---|
| `gpumon.exe` | the program, at the top level so nobody has to hunt for it |
| `_internal/` | PyInstaller's support files, which the build needs |
| `start-sensors.cmd` | the one-time CPU sensor setup, for anyone who prefers a script |
| `README.md` | installation, running and using it — no development notes |
| `gpumon.ico`, `gpumon.png` | the icon and the preview image |

No tests, no source, no build files. The script refuses to leave run-time files
behind (`config.json` and the like), because a settings file from the build
machine would hand a new user someone else's configuration — and make the
first-run shortcut question look already answered.

**On first run the program offers to add a desktop shortcut**, and remembers the
answer: asked once ever, only in a packaged build, and never when one is already
there.

### Hotkeys / buttons

| Key | Button | Action |
|-----|--------|--------|
| `L` | START / STOP LOGGING | Begin or end a logged session (summary opens on stop) |
| `M` | MARK | Drop a labelled marker into the current run ("FurMark phase 2") |
| `S` | SUMMARY | Open the report for the running or most recent session |
| `A` | ALARMS | Edit warning / critical thresholds |
| `C` | — | Collapse or expand every GPU card (the chevron on a card does one) |
| `G` | GRAPHS ON/OFF | Show or hide the graph column — every card, plus the CPU and RAM charts |
| `T` | the theme button | Cycle the colour themes (the button opens a picker) |
| `P` | — | Resume live after panning the graphs back through history |
| `Q` | SESSIONS | Browse, open or delete past sessions |

The CPU card carries one more button: **PER-CORE** starts or stops per-core CPU
sampling while the app runs. The flag used to be fixed at launch, so looking at
the cores meant restarting and losing the run; it is read at poll time now, so
the next sample carries them — and because the sampler, the live buffer and the
store all take whatever the poll returns, a session that gains cores halfway
records both halves correctly. The choice is saved for the next run.

Its three rows read CPU, TEMP, RAM in that order: processor load and processor
heat belong beside each other, and memory is the one of the three that is not
about the CPU.

### What was tried and removed

**A second card layout called "combined".** One chart per card carrying every
series on a shared axis, modelled on the GPU-Z panel inside FurMark: temperature,
hotspot, load, VRAM, power, and the clock on a right-hand scale. It was measured,
screenshotted and shipped — and rejected on use. Five traces on one plot is
harder to read than five small ones, and the legend ends up carrying values the
number columns should have shown. The code is **gone**, not switched off: no
button, no `V` key, no setting, and a test asserts none of it comes back.

**The hotspot as a graph line.** It is a number worth reading and a line worth
omitting — a second trace over the same sensor cluster, for a reading that fits
in the card's text next to the temperature. It still appears there (`hotspot 35 C
mem 34 C`), and no longer on the plot.



The live graphs follow the GPU-Z idiom: an inset plot with a hairline border,
faint divisions labelled down the left, a translucent fill under the trace, a
**stepped** line (sample-and-hold, so a 0→100→0 utilisation burst is the square
wave it really was rather than a ramp), a legend listing every series on the
chart with its current value, and the row's primary reading boxed against the
right edge. The x axis is a fixed time window, so history scrolls off the left
instead of the trace stretching to fit.

The hotspot is not plotted. It is a number worth reading and a line worth
omitting: it reads the same sensor cluster as the edge temperature, so a second
trace doubles the ink for one more figure — a figure that fits comfortably in the
card's text (`hotspot 35 C   mem 34 C`). It stays there.

### Scrolling back through history

Sampling never stops, but the view can. **Drag a graph** sideways (drag right to
go back in time) or **shift+wheel** over one to pan, and **double-click** — or
`P`, or the `LIVE` chip in the browser GUI — to snap back to live. Every chart
moves together: the question during a stress test is "what was *everything* doing
when the temperature spiked", which a per-chart scroll could not answer.

While panned the whole view says so — an amber `HISTORY  -12.0s` banner, an amber
frame on every graph, and the in-chart value badge showing the reading at the
right edge of the *view* rather than the live one. The offset grows with the
incoming samples so a moment you are reading stays put instead of creeping back
to live one sample at a time. Recording is untouched: panning is a view, not a
filter, and the logged session keeps every sample.

### Window size and columns

Eleven palettes, switchable from the desktop button, the `T` key, the browser
GUI's picker, the terminal view's `T`, or `--theme NAME`; the choice is saved to
`config.json`.

| Key | Name | |
|-----|------|---|
| `nvtop` | nvtop | the original: cold blue on near-black |
| `eva-00` | EVA-00 (Rei) | the light one: white-grey armour, orange trim |
| `eva-01` | EVA-01 (Shinji) | violet armour, fluorescent green trim — **the default** |
| `eva-02` | EVA-02 (Asuka) | production red, black and orange |
| `eva-03` | EVA-03 (Touji) | charcoal and silver, red core |
| `eva-08` | EVA-08 (Mari) | magenta and pink, white trim |
| `eva-13` | EVA-13 (Kaworu) | black and violet, dual green cores |
| `nord` | Nord | cool arctic blue-grey |
| `gruvbox` | Gruvbox | warm retro terminal |
| `amber` | Amber CRT | monochrome amber phosphor |
| `matrix` | Matrix | green phosphor on black |

Unit-01 is what the program looks like out of the box; the palette this started
from (`nvtop`) is still there, one click away. Palettes register in source order
and the first one is the default, so `test_themes.py` asserts both halves of that:
Unit-01 leads, and the other units stay in numeric order behind it.

`python gpumon.py --list-themes` prints them with their series colours.
Green/amber/red keep their alarm meaning in every theme, including the
monochrome ones — `test_themes.py` asserts the contrast ratios and the hues so a
palette edit cannot quietly make a warning unreadable.

`eva-00` is the only light theme, and it is not the dark palettes inverted: text,
grid, series and alarm colours are all darker so they read on a light page, and
the parts of the UI that were hardcoded dark (the log button, the scroll
indicator, the chart threshold line) are derived from the palette instead. Every
palette colour is also checked against the xterm-256 cube the terminal view falls
back to when truecolor is unavailable: the worst of the eleven is 51 RGB units
off, which is why `eva-00`'s red and green are cube-exact.

### Navigation, in every version

The same model is implemented three times, so the keys mean the same thing
wherever you are:

| Key | Action |
|-----|--------|
| `Tab` / `Shift-Tab` | move the focus between the action buttons |
| `Enter` / `Space` | activate the focused button |
| `↑` `↓` | scroll one line |
| `PgUp` / `PgDn` | scroll a screenful |
| `Home` / `End` | jump to the top or bottom |
| `←` `→` | switch tab (terminal / browser), or move along the buttons |

The desktop window also scrolls with the **mouse wheel** (Windows, macOS and X11
conventions all handled), and its scrollbar only appears when the content
actually overflows. That scrollbar is drawn by gpumon rather than by Tk:
`tk.Scrollbar` keeps the system trough and thumb on Windows whatever you set, and
`ttk.Scrollbar` can only be recoloured under the `clam` theme, which would
restyle the summary window's tabs at the same time.

Click a card's **▾** (or its name) to collapse just that one. A collapsed card
keeps a one-line summary — `35 C  18% gpu  17% vram  6 W  peak 35 C` — so folding
it away costs you nothing you would want to glance at.

### Window size and columns

The window opens at **1180x880**, centred, capped to your screen's work area, and
is freely resizable — every card's columns are recomputed from the window width
so the label, value, stats and graph columns stay aligned vertically across all
cards at any size.

Because a per-card grid cannot align itself with the cards above it, the column
widths are computed once in `MonitorApp._layout_columns()` and applied to every
card. **The whole card scales**, not just the graph: the left block (and with it
the temperature, its caption and the meter bar) is a fraction of the window
between 150 and 300 px, and the graph takes whatever is left.

The text columns are sized to what their content actually needs, measured from
the widgets rather than guessed: the value column holds `83.2%  25,554/30,704 MB`
and the stats column `min 104.0 avg 104.0 max 104.0`, and a `tk.Label` silently
truncates text longer than its declared width — which is exactly what used to
happen to both when the window was narrow. When the cards genuinely run out of
room the stats column is dropped first; if even that will not fit, the graphs are
hidden automatically. **`G`** hides them explicitly to leave just the numbers.

### Reading the live view

Each metric row carries three things, so you can judge what is happening without
hovering or doing arithmetic:

* **current value**, colour-coded by severity
* **min / avg / max** over the window on screen — "62 now, peaked 78" is far more
  useful during a stress test than the instantaneous number
* a **scrolling graph** with the window's peak marked, a dot on the latest
  sample, a shaded warning zone, and a dashed threshold line where one applies

**Hover any chart** to read values off the lines. The readout shows the time, a
value for every series at that point, and marks the point on the line nearest
your cursor — and it leads with that line's value, so the answer to "what is
this line doing here" is the first thing you read. Values are always real
recorded samples, never interpolated.

### Fine-tuning the summary

The summary window can be **minimised, maximised and resized** like any normal
window, and its charts fill the width rather than clipping.

To analyse part of a run — drop a warm-up phase, or isolate one benchmark step —
**drag across any chart**. The selected window is shaded, and the statistics,
histograms and tables immediately report on that window only. `LAST 50%` and
`LAST 25%` are one-click presets, and `FULL RUN` clears the selection. Charts keep
showing the whole run with the window shaded, so you can keep adjusting it.

Scrollbars now appear only on tabs that actually overflow.

**Numbers, not just graphs.** The Overview gives each GPU a table of average /
low / high / p95 for temperature, hotspot, memory temperature, utilisation, VRAM,
clock, power and fan. Each GPU tab has a full table per metric: low (5th
percentile), average, high (95th), true maximum, median, p95, p99, standard
deviation, observed range and sample count. Percentiles are used for "low" and
"high" so a single spike does not define the row, while `max` still carries the
real peak.

**Temperature charts plot hotspot as a separate line**, drawn first: junction
temperature is what actually limits a GPU, and an edge-temperature-only chart
hides it. Memory temperature is plotted alongside where the driver reports it.

Charts have labelled axes on both sides, a time axis in `mm:ss`, a shaded hot
zone above 80 °C, and hover readouts.

### Command line

```powershell
gpumon                           # desktop window  (python gpumon.py)
gpumon tui                       # terminal view
gpumon web                       # browser GUI on http://127.0.0.1:8080
gpumon help                      # launcher help

python gpumon.py --hz 2          # sample at 2 Hz (default 1 Hz)
python gpumon.py --per-core      # also record per-core CPU utilisation (or the PER-CORE button)
python gpumon.py --setup-sensors # enable CPU temperature (see below)
python gpumon.py --calibrate-gpus # verify which counter belongs to which GPU
python gpumon.py --vram-report   # compare every VRAM source side by side
python gpumon.py --vram-check    # measure real VRAM usage from a load/unload delta
python gpumon.py --web --port 9000 --host 0.0.0.0   # serve on the LAN
python gpumon.py --selftest      # headless check of every layer
python gpumon.py --list          # list logged sessions
python gpumon.py --report 3      # print a text report for session 3
python gpumon.py --db D:\gpu.db  # use a different database
```

`gpumon.cmd` (Windows) and `gpumon.sh` (Linux/macOS) are launchers: they resolve
the Python interpreter, install `psutil` once if it is missing, and forward
everything through. `gpumon help` lists the shortcuts.

The words `tui`, `web`, `list`, `sessions`, `selftest`, `report N` and `help` are
understood by `gpumon.py` itself, in **any** position, so
`gpumon --db other.db report 3` means what it says and behaves the same on both
platforms. A word nobody recognises is refused with a message rather than
quietly starting the desktop window — `gpumon listt` used to open a window, which
on a double-clicked launcher is indistinguishable from a hang. The launchers do
no argument rewriting at all.

**Double-clicking `gpumon.cmd` starts the desktop window detached.** It used to
be a child of that console, so closing the black box killed the app — which is
what a double-clicked launcher does the moment the batch file ends, and what a
shell that kills its process tree (Windows Terminal, an IDE terminal) does to
everything inside it. The first fix used `start "" pythonw.exe`, which was not
enough: it still left the app as a child of the console, and when `pythonw.exe`
could not be found the launcher fell back to console `python`, which kept a
console window of its own.

The spawn now happens in `gpumon.py --spawn`, through `CreateProcess` with
**`DETACHED_PROCESS`** (no console is allocated at all, whatever interpreter runs
it), **`CREATE_NEW_PROCESS_GROUP`** and **`CREATE_BREAKAWAY_FROM_JOB`** — the last
of those is what escapes a terminal's job object. Breakaway is attempted first
and dropped if the job forbids it, because refusing to start would be worse than
starting inside it. Modes that need a console stay attached, and so does anything
the launcher does not positively recognise: an unexpected argument has to fail
where you can see it.

A windowless launch has no stdout at all, so `gpumon.py` points both streams at
`gpumon-launch.log` beside the app, and shows a Tk error dialog if it fails
before the window exists.

> **Security note for `gpumon web`:** the server has no authentication. It binds
> to `127.0.0.1` by default; use `--host 0.0.0.0` only on a network you trust.

Sessions live in `%USERPROFILE%\Documents\gpumon\sessions.db` (SQLite).

### Installing it as an application

```powershell
python scripts/make_release.py --build     # -> release\gpumon.exe, ready to run
python scripts/make_release.py --all       # + the zip and the single-file build
```

`scripts/make_release.py` installs nothing: PyInstaller must already be present
(`pip install pyinstaller`). It bundles the interpreter with the program, so
`gpumon.exe` runs on a machine with no Python at all. It is a one-folder build on
purpose — one-file mode unpacks ~20 MB into a temporary directory on every launch
and costs about 0.4 s each time, which is measured rather than guessed.

The program puts the desktop shortcut on offer itself on first run, so there is no
script for that any more. `gpumon.cmd` remains the way to run the terminal and
browser modes, which need a console to talk to you.

Paths that belong to the app (the settings file, the logs, the sensor readings)
resolve through `apppaths.py`: beside the executable for a portable copy — the
rule is the presence of a `config.json` next to it — and in the user's profile
otherwise, which is what an installed package needs since its own folder is
read-only. A frozen build unpacks into a temporary directory, so anything derived
from `__file__` would be written somewhere that is deleted on exit.

### Repository layout

```
gpumon.py, metrics.py, ...      the program: flat modules, imported by name
ui/                             the desktop interface (tkinter)
platforms/                      one module per OS, dispatched from __init__.py
tests/                          36 test files, each runnable on its own
scripts/                        packaging, signing, icons, Store screenshots
store/                          the Microsoft Store submission material
```

The program's modules stay at the top level on purpose: they are imported flat
(`import metrics`), which keeps the frozen build and every entry point simple.
What sits in folders is everything a *user* never runs.

### The icon

![gpumon](gpumon.png)

A horned monster eating a graphics card: black board, two fans, gold PCIe edge
fingers, held across its mouth with two fangs in it. Drawn by `scripts/make_icon.py` with
nothing but the standard library — a coverage-sampling rasteriser, a PNG encoder
on top of `zlib`, and a hand-written ICO container (PNG frames at 128 px and
above, BMP frames below, which is what Explorer and the taskbar expect).

It is built from circles and rounded rectangles only, so the silhouette still
reads at 16 px: `test_icon.py` decodes the frames and asserts each part of the
mascot is present — violet body, tan horns, white eyes and fangs, the black card
and its gold edge — rather than trusting that it drew something.

The window icon on Windows comes from the `.ico` alone (`iconbitmap`), which
hands Windows the file so it can pick the 16 px frame for the title bar and the
256 px one for the taskbar. `iconphoto` is deliberately **not** called there: Tk
rescales the 256 px photo itself, and the title bar ended up showing a mangled
grey patch while every frame in the .ico was provably correct. On X11 and Wayland
it is the other way round, and `iconphoto` is the call that sticks.

---

## What it measures

Live view and logged metrics, per GPU unless noted:

* **Temperature** — core, hotspot/junction, memory (where the driver exposes it)
* **Utilisation** — GPU and memory-bandwidth %
* **VRAM** — used / total, plus %
* **Clocks** — core, SM, memory (MHz)
* **Power** — board power and power limit (W)
* **Fan** — %
* **Throttle flag** — whether the driver reports a thermal/power clock limit
* **CPU** — utilisation, clock, package temperature and power (see below), per-core if enabled
* **RAM** — used, total, %, pagefile

A sensor that cannot be read is shown as `n/a` and simply absent from the log.
Nothing is ever reported as a fake zero.

---

## Platforms

|  | Windows | Linux |
|---|---|---|
| GPU telemetry | NVML (NVIDIA), ADL (AMD), PDH counters | NVML (NVIDIA), **sysfs** (AMD), `rocm-smi` if present |
| CPU temperature | **gpumon's own reader** (Intel, via PawnIO); LibreHardwareMonitor or the ACPI zone as fallbacks | `/sys/class/thermal` and `/sys/class/hwmon` |
| Physical GPU list | Setup API device nodes + PCI location | `/sys/class/drm/card*/device` + PCI slot |
| Desktop window | tkinter | tkinter |
| Terminal view | ANSI + `msvcrt` raw keys | ANSI + `termios` raw keys |
| Browser GUI | yes | yes |

### What has actually been run, and where

Worth stating plainly, because "supports Linux" and "has been run on Linux" are
different claims:

| | Status |
|---|---|
| Windows, Intel CPU, NVIDIA + AMD GPUs | **verified on one machine** (HP Z4 G4, i9-7900X, 2× Radeon Pro V620, Quadro P2000) |
| AMD CPU temperature | **not implemented** — the registers differ and are not decoded yet |
| Linux | **written, never run on a real kernel** — the sysfs and thermal readers are exercised only against fixtures |
| Other GPU generations | untested; the backends are the vendors' own APIs, but nothing here has seen an RX 7000 or an RTX 40-series |
| macOS | not supported |

`platforms/` holds the dispatch layer and one module per OS, so the sensor logic
above it stays platform-neutral. Anything Windows-only is imported lazily, so the
package imports cleanly on Linux and the Windows-only tools
(`--setup-sensors`, `--calibrate-gpus`, `--vram-report`, `--vram-check`) refuse
with an explanation instead of a traceback.

On Linux, AMD telemetry comes from sysfs — `gpu_busy_percent`,
`mem_info_vram_used`, `hwmon/temp1_input`, `pp_dpm_sclk` and friends — which
needs no driver, no elevation and no `rocm-smi`. `rocm-smi` is used only if sysfs
cannot supply a value.

The desktop window icon is generated from source by `scripts/make_icon.py` (stdlib only —
a small anti-aliased rasteriser, a PNG encoder and the ICO container). Re-run it
after editing the artwork: `python scripts/make_icon.py`.

---

## Sensor coverage on this machine

Detected here: **two AMD Radeon Pro V620** (PCI 17:00.0 and 23:00.0) + **NVIDIA
Quadro P2000** (PCI 2d:00.0) + i9-7900X. Each GPU is listed separately, even
though the two V620s are identical.

| Source | Covers | Needs |
|--------|--------|-------|
| **NVML** (`nvml.dll`) | NVIDIA temps, clocks, VRAM, power, fan, throttle reasons | nothing |
| **ADL** (`atiadlxx.dll`) | **AMD temperature, hotspot, memory temp, fan, clocks, power** | nothing |
| **Windows PDH** (`pdh.dll`) | Utilisation + dedicated VRAM for every adapter | nothing |
| **psutil** | CPU %, clock, RAM, pagefile | nothing |
| **Setup API** | Physical GPU list and each card's PCI location | nothing |
| **LibreHardwareMonitor** (WMI) | **CPU package temperature** | `--setup-sensors` |

So: both GPUs are fully instrumented out of the box — no kernel driver, no
elevation, no helper process.

### AMD sensors: what actually works, and two mistakes worth recording

An earlier version of this tool reported that AMD temperatures were unreachable,
because every ADL call returned `-5`. That conclusion was wrong, for three
reasons that are worth writing down because each one silently produced a
plausible-looking "unsupported" result:

1. **`-5` is `ADL_ERR_INVALID_ADL_IDX`** — "bad adapter index", not "not
   supported". Wrong indices made a working API look dead.
2. **The calling convention is `Cdecl`**, while ctypes defaults to `stdcall` on
   Windows. `ADL2_Main_Control_Create` faulted instead of returning a context,
   so no ADL call could ever succeed.
3. **The PMLog sensor ids were guessed.** `ADL_PMLOG_TEMPERATURE_EDGE` is **8**,
   not the 19 assumed from a documentation enum — reading the wrong slots yields
   plausible-looking numbers (33, 32) that are actually unrelated fields.

The fix was to stop reconstructing the interface and port it faithfully from
LibreHardwareMonitor's own ADL interop (`amdsensors.py`): struct layouts, enum
values, the Cdecl convention, the `PMLog_Support_Get` → `PMLog_Start` sequence
that makes the driver publish a rolling sensor block, and the `supported` flag
that says whether a given card has a given sensor at all.

Verified by loading one V620 with an OpenCL compute kernel for 20 seconds:

| | PCI 17:00.0 (loaded) | PCI 23:00.0 (idle) |
|---|---|---|
| temperature | 32 → **56 °C** | 33 °C, flat |
| hotspot | 34 → **74 °C** | 34 °C, flat |
| board power | 12 → **250 W** | 8 W, flat |
| core clock | 0 → **2345 MHz** | 0, flat |
| utilisation | 0 → **100 %** | 0 %, flat |

Every quantity responds to real load on exactly one card, which also proves the
two identical cards are read independently and correctly attributed.

### VRAM: why it does not come from Windows

Windows' own `GPU Adapter Memory` counter is unusable for these cards. Loading an
LLM split across both V620s exposed it: one card's counter summed two LUIDs that
describe the same memory while the other reported **0 MB**.

ADL's `ADL2_Adapter_DedicatedVRAMUsage_Get` replaced it and now agrees with the
per-LUID counters, so both cards report independently. It also agrees with
**GPU-Z** on this machine, card for card, which retires the two ways a reader
like this usually lies: a wrong unit (MB vs MiB) and a fixed offset. Agreement
does not prove ADL and GPU-Z are independent — both read AMD's driver, possibly
through the same call — but two implementations of the same number is still the
strongest check available without a kernel driver of our own.

**If a VRAM figure does not match your workload**, settle it with a measurement
rather than a guess:

```powershell
python gpumon.py --vram-check
```

It takes one reading with your workload loaded and one with it unloaded. The
**difference** is what the workload actually occupies — immune to unit, offset and
scaling errors, because it is only the gap between two readings of the same
counter. You can also run it in two passes on your own schedule:

```powershell
python gpumon.py --vram-check --phase=idle     # workload unloaded
python gpumon.py --vram-check --phase=loaded   # workload loaded
```

`python gpumon.py --vram-report` is the companion: it prints every source side by
side (gpumon's value, ADL per adapter, and each raw per-LUID counter) so you can
see them together. `metrics.py: _poll_amd()` is the single place that decides
which source gpumon uses.

#### Where a loaded model's VRAM actually goes

Measured here with a 27B Q6_K model on the two V620s at a 262144-token context:

| | |
|---|---|
| weights + vision tower (`*.gguf`) | 24.4 GiB |
| KV cache, 17 attention blocks at 262144 tokens, f16 | 17.0 GiB |
| runtime compute buffers, driver | ~5.4 GiB |
| **measured total** (`--vram-check`) | **46.8 GiB** (25,392 + 22,545 MiB) |

The KV cache is the part people misjudge, and for hybrid models the usual
arithmetic is badly wrong. This one interleaves three linear-attention blocks —
a fixed-size recurrent state, no cache at all — with one full-attention block
that owns a cache growing with the context. Counting a cache on all 65 blocks
would claim 65 GiB instead of 17.

`ggufinfo.py` reads the geometry out of the GGUF itself rather than trusting the
model card, and counts the blocks that really own a cache from the tensor names:

```powershell
python ggufinfo.py model.gguf --ctx 131072
```

```
blocks      : 65 (17 with a KV cache, 48 with a recurrent state)
per attention block at 262,144 tokens: 1.00 GiB

context        KV cache      weights + KV
  32,768        2.12 GiB       25.69 GiB
 131,072        8.50 GiB       32.06 GiB
 262,144       17.00 GiB       40.56 GiB
```

Two things worth knowing from this machine:

- **LM Studio's own load estimate is not usable for this model.** It reports
  *Model 27.01 GB / Context 45.03 GB / Total 72.04 GB*. The real context cost is
  18.25 GB, and the whole model runs in 46.8 GiB — their planner would have you
  believe it does not fit in the 60 GiB these cards have. Measure, don't plan.
- **Use the same MiB as the hardware.** gpumon's `MB` is a mebibyte
  (1 MiB = 1,048,576 B), confirmed against `HardwareInformation.qwMemorySize` in
  the display class registry key: 32,195,477,504 bytes = 30,704 MiB per V620.

**Other AMD tools on Windows.** `rocm-smi` does not exist here — ROCm is
Linux-only. `amd-smi.exe` ships in `System32` but its Windows build shells out to
`wmic`, which Windows 11 no longer includes, so it fails with *Access is denied*.
That leaves ADL, the Windows performance counters, and whatever your workload
itself reports.

### Utilisation counters and `--calibrate-gpus`

Windows publishes one utilisation counter per GPU, plus extra counters that never
report anything, and gives no way to ask which counter belongs to which card.
gpumon deals them out round-robin so **every card is guaranteed at least one
working counter** — before this, both AMD counters landed on one card and the
other had only a silent one, which is why one V620 showed no utilisation.

To turn that inference into a measurement:

```powershell
python gpumon.py --calibrate-gpus
```

It walks you through loading one card at a time and records which counter
actually moves, saving the verified mapping to `luid_calibration.json`. Every
later run uses it. Cards with a verified mapping are labelled `calibrated`; the
rest say `pci-order` so you always know which figures are inferred.

### CPU temperature

**Short answer: on Linux nothing is needed at all; on Windows one elevation,
once, is unavoidable — and this is why.**

CPU *package* temperature is not a Windows API. Reading it means reading the
processor's thermal MSR, which is a privileged instruction, so it needs a kernel
driver, and loading a kernel driver needs an administrator. Every program that
shows you a CPU temperature — LibreHardwareMonitor, HWiNFO, GPU-Z, Intel Power
Gadget — ships exactly such a driver. There is no user-mode route to it.

Measured on this machine, the built-in Windows sources are all empty:

| Source | Result |
|---|---|
| `Win32_Processor.CurrentTemperature` (SMBIOS type 4) | empty |
| `Win32_TemperatureProbe` (SMBIOS type 26) | no instances |
| `\Thermal Zone Information(*)\Temperature` perfmon counters | counter set absent |
| `MSAcpi_ThermalZoneTemperature` (root\WMI) | `WBEM_E_NOT_SUPPORTED` |

So gpumon asks every source it can, in order, and says which one answered:

| Order | Source | OS | Needs |
|---|---|---|---|
| 1 | **gpumon's own reader**, via the PawnIO driver | Windows | one elevation, once |
| 2 | `/sys/class/hwmon`, `/sys/class/thermal` | Linux | **nothing** — a file read, no driver, no elevation |
| 3 | LibreHardwareMonitor, HTTP or WMI | Windows | that whole application; kept only as a fallback |
| 4 | ACPI thermal zone | Windows | nothing, when the firmware has one |

The first source that produces a reading wins, and `SensorManager.cpu_temp_source()`
names it — `msr`, `lhm`, `thermal` or `acpi`. Nothing is inferred from the backend
list, so the UI never claims a sensor it does not have.

### Reading the processor ourselves

The `/temperature/0` route through LibreHardwareMonitor is no longer the first
choice, because none of it was ever necessary. Its source says exactly what it
does, and it is arithmetic over registers:

```
TjMax        = (MSR 0x1A2 >> 16) & 0xFF
package temp = TjMax - ((MSR 0x1B1 >> 16) & 0x7F)     if bit 31 is set
core temp    = TjMax - ((MSR 0x19C >> 16) & 0x7F)     if bit 31 is set
```

`msr.py` holds those layouts, `pawnio.py` talks to the driver, `cpusensors.py`
identifies the processor and reads the registers (pinning the thread to each core,
since a core's register reports that core), and `sensorhelper.py` runs the whole
thing elevated and publishes the result to a file. The program itself stays an
ordinary user process.

Verified against LibreHardwareMonitor on this machine, under sustained load —
**per-core readings agree to the degree**:

```
core 1:  ours 40.0 C   theirs 40.0 C
core 1:  ours 40.0 C   theirs 41.0 C
package: ours 57.0 C   theirs 59.0 C     (their server serves values ~1s stale)
```

That comparison also caught a real bug: gpumon had been reading
`/intelcpu/0/temperature/0` and calling it the CPU temperature, but
LibreHardwareMonitor creates its core sensors *before* the package sensor, so that
index is core #1 — several degrees hot. It now prefers the sensor named
"CPU package", with `test_directcpu.py` asserting the rule.

### The driver, and why gpumon does not ship it

Reading a register needs a kernel driver that can execute privileged
instructions. gpumon uses **[PawnIO](https://pawnio.eu)** — signed, open source,
by namazso, used by several monitoring tools — and **does not redistribute it**:
PawnIO is GPL-2.0, so bundling its installer or modules inside this project would
put its licence on this project's distribution, which is not a decision to make
quietly on someone's behalf. `sensorsetup.py` fetches the vendor's own installer
and the signed modules from the projects' own release channels instead, and uses
a task, not a bundled binary:

| Step | Where it comes from |
|---|---|
| the driver | `github.com/namazso/PawnIO.Setup/releases/latest/download/PawnIO_setup.exe` |
| the modules | `github.com/namazso/PawnIO.Modules` releases (signed) |
| the elevation | a scheduled task, created once, triggered with `schtasks /run` |

That is the whole cost: **one prompt, once**. Afterwards the helper starts at
logon and on demand, and gpumon repairs itself if it stops — verified by killing
the helper outright and watching an ordinary unelevated process bring it back in
about fifteen seconds.

### What was tried and removed here

For the record, because it was a lot of work to arrive at "none of this was
needed":

- **LibreHardwareMonitor's WMI provider.** Never existed in any release from
  0.8.8 to 0.9.6 — the binaries contain no WMI code at all.
- **Its web server.** The only interface 0.9.6 has, and it can only be started
  from its own menu, which is not exposed to UI Automation. Driving it with
  keystrokes worked (Options → Remote Web Server → Run) and is gone.
- **Copying their code.** Their C# is MPL-2.0, but the driver is the privileged
  part, so a port would still have needed the same elevation for no gain. Reading
  the source to learn the registers, though, was exactly the right move.

**The ACPI fallback is labelled, not disguised.** On many laptops the CPU zone is
one of the firmware's thermal zones and this is a genuine no-driver reading; on
desktops it is usually the board or the VRM. gpumon says so in the notes when it
falls back, rather than passing a motherboard sensor off as the CPU package.

### Setting up the Windows driver, once — then nothing, ever

```powershell
start-sensors.cmd                    # or: python gpumon.py --setup-sensors
```

Fetches [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
into `tools\` if it is missing, launches it elevated, and registers two tasks:

| Task | Trigger | What it does |
|---|---|---|
| `gpumon-sensors` | at every logon | starts the driver and the sensor server |
| `gpumon-sensors-web` | on demand, by gpumon | the same, for when the server is not answering |

Both run `lhm-server.ps1`. **One elevation, once**: after that the driver and the
sensor server come back by themselves, and gpumon triggers the helper itself
whenever the sensors are missing — a task created with `/rl highest` can be
started with `schtasks /run` without any prompt. The live window's **START
SENSORS** button does the same thing, and only falls back to a Windows prompt on
a machine that has never been set up.

The logon task is a convenience rather than a requirement: **gpumon repairs
itself**. Verified by stopping LibreHardwareMonitor outright and then calling
`sensorsetup.start_server_now()` from an ordinary unelevated Python process:

```
stop requested: 0 process(es) left
starting LibreHardwareMonitor
LibreHardwareMonitor is running: pid 7080
the window appeared after 6 attempts
sent Alt+O, End, Right, Home, Enter (Options -> Remote Web Server -> Run)
the sensor server is answering after 1s
repair -> True after 15.7s   cpu_temp: 45.0 C
```

That is the whole point of the arrangement: from a cold machine with nothing
running, an unelevated gpumon gets a CPU temperature in about fifteen seconds
without a prompt or a click.

`--no-startup-task` skips the tasks; `schtasks /delete /f /tn gpumon-sensors`
and `... /tn gpumon-sensors-web` remove them.

`lhm-server.ps1` also understands two marker files next to it, for when a future
LibreHardwareMonitor moves its menus or for testing this path:

| Marker | Effect |
|---|---|
| `lhm-list.flag` | dump the whole UI Automation tree to `lhm-server.log` |
| `lhm-probe.flag` | open every menu in turn and photograph it |
| `lhm-stop.flag` | stop LibreHardwareMonitor, so recovery can be tested |


### Why the helper drives a menu with the keyboard

This is the part that took the longest to get right, and it is worth recording so
nobody repeats it.

LibreHardwareMonitor's **web server is the only interface it has**. Its WMI
provider — the interface gpumon was originally written against — does not exist
in any recent release: checking the binaries of nine versions from 0.8.8 to 0.9.6
finds **zero** occurrences of `WmiProviderEnabled` or the `root\LibreHardwareMonitor`
namespace. And the web server cannot be started by any setting or switch: its
settings file holds only window geometry, the listener port and the theme, and
its command-line options are `-debuginfo`, `-install` and `-unrestricted`, none
of which touch it.

So it has to be started through its menu, which means automating another
application. Two things make that harder than it sounds:

1. **The menu is not in the accessibility tree.** The window reports five generic
   panes and no menu bar at all, so there is nothing for UI Automation to
   invoke. The helper therefore sends keystrokes — the same route a person uses —
   after restoring and activating the window.
2. **The menu is not where the documentation implies.** In 0.9.6 there is no
   "Web" menu. The path, read off the screen by opening each menu and
   photographing it, is:

   ```
   Options                     Alt+O
   Remote Web Server           last item in that menu        End
   Run                         first item of its submenu     Home, Enter
   ```

   (Not `Run On Windows Startup`, which is a different item in the same Options
   menu.)

Run `powershell -File lhm-server.ps1 -List` to dump the tree, or create
`lhm-probe.flag` next to it to photograph every menu, if a future version moves
things. `lhm-server.log` records what the helper found and did on every run.

For the record, the two things that do *not* work on this machine: the ACPI
thermal zone (`MSAcpi_ThermalZoneTemperature`) is not implemented by the
firmware at all — it returns `WBEM_E_NOT_SUPPORTED` — and the old advice to
enable LHM's WMI provider is a no-op on any version you can download.


```powershell
python gpumon.py --setup-sensors
```

It downloads [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
from the project's official GitHub release into `tools\` if it is not already
there, turns its sensor output on, restarts it so the settings take effect,
launches it elevated (one Windows prompt), then waits and prints the sensors it
can now see. You can equally install and run LibreHardwareMonitor yourself —
gpumon detects it either way, and records which sources were live for every
logged session.

> **If CPU temperature shows `n/a`:** that is this gap, not a bug. The CPU card
> says `n/a - needs LibreHardwareMonitor`, and while it does it also offers a
> **START SENSORS** button. The footer reads `LHM down` and the summary's
> Session info tab explains it. If a previous run already downloaded
> LibreHardwareMonitor, `start-sensors.cmd` will restart it with the right
> settings.

### Why identical cards used to be merged

Every convenient source reports display *outputs* rather than GPUs: the display
class registry key listed 3 AMD entries for 2 cards, `Win32_VideoController`
listed 2 rows with nothing to tell them apart, ADL listed 7 adapters for 3 cards,
and the Windows performance counters publish 4 LUIDs for 3 cards. Deduplicating
by name+VRAM therefore collapsed the two V620s into one row.

`gpuenum.py` enumerates the display **device nodes** through the Setup API, where
each physical card appears exactly once. Two details were needed:

* `SPDRP_BUSNUMBER` is wrong for display devices — it reported bus 23 for the
  card whose location string says bus 17. The location string is parsed instead.
* ADL reports each adapter's PCI bus/device, so AMD readings are matched to cards
  by PCI address — an exact mapping, not an ordering guess.

---

## Logging, alarms and the summary

**Logging** writes every metric to SQLite (`sample` table, one row per metric per
tick). Writes are batched and performed on a background thread, so recording
never disturbs the measurement. Stop with `L` and the summary opens.

**Alarms** have a warning and a critical level per metric, with two guards that
matter for thermal testing:

* **hysteresis** — a value must fall this far *below* the threshold before the
  alarm clears, so a value hovering on the line does not flap
* **minimum duration** — ignore spikes shorter than this, so a one-sample hotspot
  blip does not raise an alarm

Defaults are conservative (GPU temp: warn 80 °C / crit 88 °C; hotspot: 95/105;
CPU: 85/95). Edit them with `A`. Escalation is recorded: crossing warning then
critical produces two timeline entries.

Every crossing is persisted with its start time, end time, peak value and
duration, and drawn on the report charts as a vertical line — plus a full table
in the **Alarms** tab.

**The summary window** gives you, per session:

* a verdict banner (how many crossings, highest readings)
* per-GPU cards: average/peak temperature, utilisation, VRAM, power
* charts per unit — utilisation, **temperature** (with warm/threshold band),
  VRAM, clocks, power — each with alarm and marker overlays
* temperature and utilisation **histograms** with median/p95/p99 markers
* a full statistics table (min, avg, max, median, p95, p99, stdev)
* session info, the alarm rules that were active, and which backends were silent

**Exports:** `EXPORT REPORT` writes a **self-contained HTML** report (inline SVG
charts, no CDN, works offline — good for archiving or sending) or a plain text
report. `EXPORT CSV` writes every sample as a wide time-series table for
pandas/Excel. The raw SQLite file is also yours to query.

---

## Notes on measurement quality

A benchmark that saturates the machine is exactly when a naive monitor starts
losing data. Three things here are deliberate; each was found by measurement on
this hardware, not by assumption:

1. **GPU sources are polled on their own threads** (`asyncpoll.py`). A single
   NVML call costs ~0.05–1.7 ms idle but **16–116 ms with 600 ms outliers** when
   every core is busy; a PDH read goes from 15 ms to **11 seconds**. The sampler
   reads cached snapshots instead, so it cannot inherit that latency. Under
   extreme load a GPU figure may be a second or two old; the sample cadence
   stays intact.
2. **Disk writes are asynchronous** (`SampleWriter`). With the power plan's 5 %
   minimum processor state, a 0.7 ms SQLite insert becomes **~10 s** under full
   load. Samples are queued and written by a second thread; if the queue ever
   fills, the oldest are dropped and reported rather than stalling sampling.
3. **The sampler runs at above-normal thread priority**, so it keeps its cadence
   when every core is busy.

Measured result with 6 external CPU load processes on a 20-thread CPU:
**1.00 Hz effective, 0 dropped samples, zero gaps.**

If you want maximum measurement fidelity for a benchmark run, setting the power
plan to **High performance** removes the CPU sleep-state latency that causes
most of the above:

```powershell
powercfg /setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c   # High performance
```

Other behaviours worth knowing:

* A **gap counter** in the footer flags if the sampler ever falls behind, and a
  `gaps` count is stored with the session — a report tells you if its own data
  is suspect.
* The AMD V620 publishes several hardware functions; their utilisation is
  aggregated (busiest wins) and VRAM summed, and the UI says so.
* `metric_def()` in `metrics.py` is the single source of truth for every metric's
  label, unit and precision, so the live view, summary and reports never drift
  apart.

---

## Configuration

`config.json` next to the scripts (created when you save alarm settings):

```json
{
  "sample_hz": 1.0,
  "per_core": false,
  "db_path": "",
  "alarms": {
    "gpu0_temp": { "warning": 78.0, "critical": 85.0, "hysteresis": 2.0, "min_duration": 1.0 }
  }
}
```

Only values you actually change from the defaults are written. See
`config.example.json`.

---

## Architecture

```
gpumon.py          entry point, CLI; dispatches to the desktop / TUI / web front end
platforms/         per-OS sensor backends: __init__ (dispatch), windows.py, linux.py
metrics.py         sensor vocabulary + SensorManager (merging, capabilities, notes)
gpuenum.py         physical GPU discovery + LUID classification
nvml.py            NVIDIA via nvml.dll / libnvidia-ml.so (ctypes), starvation-proof
amdsensors.py      AMD via atiadlxx.dll (ctypes) - ADL/PMLog, ported from LHM
asyncpoll.py       background poller used by the NVML and PDH sources
sampler.py         sampling thread, LiveBuffer, alarm wiring, async SampleWriter
alarms.py          rules, hysteresis + debounce state machine, config persistence
store.py           SQLite schema, batched commits, time-windowed statistics
report.py          self-contained HTML report (inline SVG) + text report
sensorsetup.py     obtains/launches LibreHardwareMonitor for CPU temperature
gpucalibrate.py    verifies which utilisation counter belongs to which GPU
vramcheck.py       measures real VRAM usage from a load/unload delta
ggufinfo.py        predicts a GGUF's weights + KV cache footprint from the file
termlib.py         ANSI drawing + raw keyboard input (Windows and POSIX)
tui.py             the terminal interface
webserver.py       stdlib HTTP server for the browser GUI
webgui.py          the browser front end (HTML/CSS/JS as string constants)
make_icon.py       generates gpumon.ico / gpumon.png with the standard library
ui/widgets.py      theme, sparklines, meters, time-series chart (hover), histograms
ui/themes.py       the colour palettes, shared by all four front ends
ui/monitor.py      the nvtop-style desktop window
ui/summary.py      post-run report window
gpumon.cmd / .sh   launchers
ansi_screen.py     ANSI emulator used by tests to verify terminal rendering
```

Threads: **sampler** (cadence), **writer** (disk), **nvml-poller** and
**pdh-poller** (slow GPU sources), plus the UI thread. Only the sampler touches
the cadence; every front end renders from the same buffers and never blocks it.

## Publishing to the Microsoft Store

The Store signs packages itself, so this is the one route that needs no
certificate at all — and it comes with two constraints that shape the build rather
than decorate it.

**The install folder is read-only.** An MSIX package lives in
`C:\Program Files\WindowsApps\`, so nothing may be written beside the executable.
`apppaths.state_path()` handles that: settings, logs and the session database go
to the user's profile, and only a copy that already has a `config.json` beside it
keeps writing there — which is what makes the zip portable. `test_storepackage.py`
runs the packaged build from a scratch folder and asserts that nothing appears
beside the executable.

**A packaged app cannot install a kernel driver.** Elevation needs the
`allowElevation` restricted capability, which Microsoft describes as unlikely to
pass certification, and their own guidance is to keep the interface in user mode
and put admin work in a separate component. gpumon is already built that way, so
the packaged build:

* works fully for GPU telemetry, CPU load, memory and per-core activity;
* shows the CPU temperature when the machine already has the sensor helper — the
  portable download sets it up once, and readings are published per user rather
  than per copy of the program;
* says so in the window instead of offering a prompt Windows would refuse.

```powershell
python scripts/make_store_package.py                 # build store/gpumon-<version>-x64.msix
python scripts/make_store_package.py --layout-only   # just the package folder
python scripts/make_store_package.py --sign          # + a self-signed cert, for local testing
python scripts/store_capture.py                      # the 1366x768 screenshots the Store wants
```

`store/SUBMISSION.md` is the rest of it: what Partner Center asks for, the three
identity values that must match the manifest exactly, a privacy policy draft, the
listing text, and the certification notes that pre-empt the driver question.
**Individual developer registration is free** — start at
[storedeveloper.microsoft.com](https://storedeveloper.microsoft.com), because
going through Partner Center directly gets the legacy paid flow.

### Drivers and NT services

The Store asks this directly, and policy
[10.2.4.2](https://learn.microsoft.com/en-us/windows/apps/publish/store-policies)
requires disclosing any dependency on non-Microsoft drivers or NT services.

**gpumon creates no NT service.** There is no service-creation call in the
program. It uses Windows Task Scheduler for the optional sensor helper — a task,
not a service — and it *reads* Microsoft's own services when they are there (WMI
for hardware identification, the performance counters for GPU engine utilisation).

**GPU readings use the vendor's drivers, which are already installed.** NVIDIA's
NVML and AMD's ADL are user-mode libraries that ship inside the graphics card's
own driver package; without that package the hardware would not work at all.
gpumon installs nothing and ships no driver.

**The CPU package temperature needs a kernel driver, and the Store package does
not use one.** That register is not exposed to user mode, which is why every
monitor that reports it installs a driver. In the packaged build the sensor-setup
path is **disabled at build time** — `scripts/make_store_package.py` sets
`storemode.IS_STORE_BUILD`, and the command line then refuses `--setup-sensors`
instead of raising a prompt Windows would refuse. The packaged build reads a
per-user data file if a helper is already installed on that machine; it never
opens the driver itself, because only the helper process does, and that process is
not part of the package. The portable download offers that install once, with the
user's consent.

`test_storedrivers.py` checks all of this against the source and by running the
packaged executable, so the answer to that Store question stays true.

## Signing

**An unsigned executable cannot be made trusted by anything inside it.** A
signature says who published a file, and Windows decides how much to trust that
publisher from a certificate chaining to a CA in Microsoft's Trusted Root
Program. Without one, every downloader sees *"Windows protected your PC"* and has
to click through it. A self-signed certificate is **worse than none** for public
distribution: Windows treats it as an untrusted publisher and blocks the file
outright for anyone who has not installed the certificate by hand.

So this is a thing to buy or earn, not a thing to code around. The options, as
Microsoft currently describes them:

| Option | Cost | Notes |
|---|---|---|
| **[SignPath Foundation](https://signpath.org)** | free | Code signing for qualifying **open-source** projects through a managed pipeline. The obvious first stop for this repository. |
| **Azure Artifact Signing** (formerly Trusted Signing) | ~$9.99/month | Microsoft's recommendation for non-Store distribution. Integrates with a pipeline, **no hardware token**. Organizations in the USA, Canada, the EU and the UK; individual developers only in the USA and Canada. |
| **OV certificate** (DigiCert, Sectigo, …) | $150–300/year | Works anywhere. Since June 2023 the private key must live on a hardware token or HSM, which most CAs supply. |
| **EV certificate** | $400+/year | **No longer worth it for SmartScreen**: the instant-trust bypass was removed in 2024, so an EV-signed file now builds reputation exactly like an OV one. |
| **Microsoft Store (MSIX)** | free | Microsoft re-signs Store packages, so users never see a warning — a different distribution channel rather than a fix for a standalone exe. |
| **Self-signed** | free | Development and managed enterprise machines only. |

**Signing is not the same as being trusted.** A newly signed file can still warn
until its publisher accumulates reputation, and signing every release with the
same identity is what makes that reputation carry forward. Microsoft's
[code signing options](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options)
and [SmartScreen reputation](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation)
pages are the source for the table above.

### What this repository does about it

`signing.py` makes signing a matter of configuration rather than a rewrite, so the
day there is a certificate nothing here has to change:

```powershell
$env:GPUMON_SIGN_CERT = "ABCDEF…"      # a thumbprint in the Windows store
python scripts/make_release.py --all           # signs what it built, then checksums it
python scripts/make_release.py --sign          # or require signing: fail if it cannot
```

| Variable | For |
|---|---|
| `GPUMON_SIGNTOOL` | where `signtool.exe` is, if it is not on `PATH` |
| `GPUMON_SIGN_CERT` | a certificate thumbprint |
| `GPUMON_SIGN_PFX` / `GPUMON_SIGN_PFX_PASSWORD` | a `.pfx` file and its password |
| `GPUMON_SIGN_ARGS` | a signing service's own arguments, e.g. Azure's `/dlib … /dmdf …` |
| `GPUMON_SIGN_TIMESTAMP` | the timestamp server (defaults to DigiCert's) |

Every file is timestamped (RFC 3161), without which every signature would stop
being valid the day the certificate expires — including for files downloaded years
earlier.

Without a configured identity a build is **not** a failure: the artifacts come out
unsigned with a line saying so, because that is the honest state of a laptop
build. `.github/workflows/release.yml` is where it stops being
honest-by-accident: it builds on a version tag, runs the suite, signs if Azure
Artifact Signing secrets are present, warns loudly if they are not, and attaches
the artifacts and their `SHA256SUMS.txt` to the release. Keys belong in a
pipeline's secrets or an HSM, never in the repository — `test_workflow.py`
asserts none are committed.

**Realistically, for this project:** apply to SignPath Foundation first — it is
free and exists precisely for open-source projects — and fall back to Azure
Artifact Signing at ~$10/month if that application does not fit. Until then,
publish `SHA256SUMS.txt` with the download: it does not remove the warning, but
it lets anyone verify that what they got is what was built.

## Requirements

* Windows 10/11, Python 3.10+ (tested on 3.14.7)
* `pip install psutil` — required for CPU/RAM
* `pip install wmi` — optional, only for the LibreHardwareMonitor fallback
  (gpumon reads the processor's registers itself, through the PawnIO driver)
* No plotting library needed; all charts are drawn on Tk canvases and emitted as
  inline SVG.
* GPU sensors need nothing extra: NVML ships with the NVIDIA driver, ADL with the
  AMD driver, and the Windows performance counters are part of the OS.

Verify the installation at any time:

```powershell
python gpumon.py --selftest
```

That checks sensor discovery, polling, storage round-trip, the alarm state
machine (crossing, escalation, hysteresis release, debounce) and report
generation.

### Test suite

| Command | What it proves |
|---------|----------------|
| `python gpumon.py --selftest` | Sensor discovery, polling, store round-trip, alarm state machine, report generation |
| `python test_gpus.py` | Every physical GPU is listed exactly once, with distinct PCI identity and at least one utilisation counter |
| `python test_amdsensors.py` | AMD sensors are readable through ADL, per adapter, with the method that produced them |
| `python test_amdopencl.py` | Loads a V620 with a real compute kernel and shows temperature/power/clocks responding on that card only |
| `python test_hover.py` | Chart hover readouts report real recorded samples, track the nearest line, and clear on leave |
| `python test_icon.py` | The generated .ico is structurally valid: 7 frames, 16→256 px, correct palette |
| `python test_modes.py` | All four front ends are wired: CLI flags, launchers, imports, platform dispatch, icon, TUI loop, web routes |
| `python test_tui.py` | Terminal primitives, key decoding, tab/scroll/focus navigation, every tab renders |
| `python test_tui_serve.py` | The real `tui.serve()` loop, driven by a scripted keyboard, renders each tab and logs a session |
| `python test_navigation.py` | Desktop scrolling: wheel (Windows and X11), arrows, PgUp/PgDn, Home/End, Tab focus ring |
| `python test_web.py` | Every HTTP route, logging start/stop, sub-window summary, error codes, clean shutdown |
| `python test_monitor.py` | Live view: values plus min/avg/max per row, bands and thresholds, collapse/expand, resize |
| `python test_alignment.py` | Every card's label/value/stats/graph columns line up at six window widths, no visible label is ever truncated, and the card as a whole scales |
| `python test_history.py` | Panning the live graphs: one shared offset, the view holding still as samples arrive, readouts following the view, the panned state being visible, and the way back to live |
| `python test_summary.py` | Summary: drag-to-select range recomputes statistics, tabs do not duplicate, window minimises, scrollbars hidden when not needed |
| `python test_launch.py` | The real window builds, live readouts populate with real values, closes cleanly |
| `python test_themes.py` | Every palette's colours parse, reach their contrast ratios, keep green/amber/red meaning, and differ from each other |
| `python test_web_theme.py` | The browser GUI renders the active palette, `POST /api/theme` switches it, unknown keys are refused, config keeps its other contents |
| `python test_web_history.py` | The browser GUI's history panning: the served state machine is executed under Node, so panning, the creep, the clamp and resume are observed rather than grepped |
| `python test_tui_theme.py` | The terminal view draws the active palette in truecolor, falls back to the nearest of the 256 colours, and `T` persists the choice |
| `python test_launcher.py` | `gpumon.cmd` driven through `cmd.exe`: every mode finishes, shorthand words work in any position, an unknown argument fails instead of opening a window, and the window is detached from the console |
| `python test_platforms.py` | Platform dispatch plus the Linux readers against synthetic sysfs fixtures |
| `python test_luidmap.py` | Which LUID counter is attributed to which card, and how confidently |
| `python test_acceptance.py` | Full run: 20 s logged under external CPU load, cadence and per-metric coverage checked, alarms persisted, summary tabs built, reports generated |

`test_acceptance.py` is the one to run after any change to sampling, storage or
alarms — it asserts the sampling rate stays within 20 % of target and that every
metric is present in at least 80 % of samples, which is how the load-related
regressions in `asyncpoll.py` and `SampleWriter` were caught.
