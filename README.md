# gpumon

A live monitor and logger for GPUs and the rest of the machine: utilisation,
temperature, hotspot, VRAM, clocks, power and fan for every graphics card, plus
CPU load, CPU memory, per-core activity and system memory, with graphs that scroll
back through history.

![gpumon](dev/gpumon.png)

## Download

**[The latest release](https://github.com/Dcoburn2/gpumon/releases/latest)** has two
downloads, the same program either way:

| Download | What it is |
|---|---|
| `gpumon-<version>-windows-x64.zip` | Unzip it and run `gpumon.exe`. Starts immediately; keep the folder together. |
| `gpumon-<version>-windows-x64.exe` | One file, put it anywhere and run it. Unpacks itself at each start, which costs about half a second. |

Each has a `.sha256` beside it holding that file's checksum, if you want to verify
the download.

Windows may show a "Windows protected your PC" notice the first time, because the
build is not code-signed. *More info → Run anyway*.

## What it does

All the numbers are on one screen. Each graphics card gets a panel with its own
graphs — utilisation, edge and hotspot temperature, VRAM, core and memory clocks,
power draw and fan speed — read through the driver you already have, with nothing
else to install.

The processor and memory sit below, with CPU load, per-core activity and memory
use, so a bottleneck is visible rather than guessed at.

Graphs scroll back through history: drag one sideways, or hold shift and use the
wheel, to see what was happening a minute ago. Recording continues while you look,
so you never lose the moment you were trying to catch. Hover any graph and a guide
line follows the pointer with the value at that instant beside it.

Sessions are logged to a database you own in `Documents\gpumon`. Drop a marker when
something interesting happens, set alarm thresholds and see them marked on the
graph, then open the summary for statistics, charts and CSV export.

**No accounts, no telemetry, no cloud.** gpumon reads sensors on your own machine
and sends nothing anywhere.

## Keys

| Key | What it does |
|-----|--------------|
| `L` | start or stop recording |
| `M` | drop a marker in the log |
| `S` | open the summary for this session |
| `A` | alarm thresholds |
| `G` | show or hide the graphs |
| `T` | change the colour theme |
| `C` | collapse or expand the cards |
| `V` | per-core sampling on or off |
| `P` | back to live after scrolling through history |
| `Q` | browse past sessions, or quit |

## CPU temperature

The CPU package temperature needs a kernel driver: Windows does not expose that
reading to ordinary programs, which is why every monitor that reports it installs
one. gpumon does it with a single prompt, from the **START SENSORS** button on the
CPU card. Everything else works without it, and the CPU temperature simply shows
as unavailable rather than being guessed at.

## Building it yourself, and the rest

The program is here, at the top of the repository: `gpumon.py`, the modules it
imports, and the interface in `ui/`. Run it with `python gpumon.py`, or build the
executable with `python dev/scripts/make_release.py --all`.

Everything that exists only to build, test or package it lives in
**[`dev/`](dev/)** — the test suite, the packaging scripts, the Microsoft Store
submission material, and the build output.

- **[`README-advanced.md`](README-advanced.md)** — the technical record: how each
  sensor is read, which registers are involved, and what has been measured rather
  than assumed
- `python dev/tests/test_release.py`, or any file in `dev/tests/` — the test suite,
  39 files, each runnable on its own

## Licence and privacy

Free software under the [MIT licence](LICENSE). It collects nothing about you —
the [privacy policy](PRIVACY.md) says exactly what it stores and where.
