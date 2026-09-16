# gpumon privacy policy

**gpumon collects nothing about you and sends nothing anywhere.** It has no
telemetry, no analytics, no accounts, no advertising and no cloud service. There
is no server to send anything to.

This page says exactly what the program does with data, so you can check it
against the source — everything below is visible in the code in this repository.

## What gpumon reads

Sensor readings from your own machine, and nothing else:

* **Graphics cards** — utilisation, temperatures, VRAM, clocks, power and fan
  speed, through the drivers already installed (NVIDIA's NVML, AMD's ADL, and
  Windows performance counters).
* **Processor and memory** — load, clocks, per-core activity, memory use, and the
  CPU package temperature where a sensor driver is available.
* **Hardware identification** — the names of your graphics cards, their PCI
  addresses and their Windows device paths. This is how the program tells one
  card from another; it is machine information, not personal information.

gpumon does not read your documents, your browser history, your files, your
location, your contacts, your clipboard, or anything you have typed. It has no
access to any account of yours.

## What gpumon stores, and where

Everything stays on your computer, in files you own:

| What | Where |
|---|---|
| Session recordings — timestamps, sensor samples, alarms, markers, and the hardware list for that run | `Documents\gpumon\sessions.db` |
| Your settings — theme, alarm thresholds, per-core preference | `config.json`, beside the program or in `%LOCALAPPDATA%\gpumon\` |
| Logs, for when something goes wrong | `gpumon-error.log` and `gpumon-launch.log`, in the same place as the settings |
| The latest CPU sensor reading, used to pass values between the program and its sensor helper | `%LOCALAPPDATA%\gpumon\` |

**Deleting it:** delete `Documents\gpumon\` and `%LOCALAPPDATA%\gpumon\`, and the
`config.json` beside the program if there is one. That is all of it. Uninstalling
gpumon removes the program but leaves your recordings, because they are yours.

## Network access

gpumon talks to the network in exactly two situations, and neither sends
information about you:

1. **Its own browser interface**, if you start it with `gpumon web`. That server
   binds to `127.0.0.1` — your own machine — by default and has no authentication.
   Binding it to another address is an explicit opt-in (`--host`), and the program
   warns you in plain terms when you do, because anyone on that network could then
   read your sensor data.
2. **The optional CPU sensor setup**, in the portable (non-Store) build. If you
   choose to set the CPU temperature up, it downloads two things from GitHub:
   the PawnIO driver installer, and PawnIO's signed sensor modules. Those requests
   go to `github.com` and `api.github.com` and carry nothing but the request
   itself — no identifiers, no usage data, no hardware information.

The Microsoft Store build never downloads anything: it has no sensor setup step
at all.

The program also reads a sensor value from `127.0.0.1:8085` **if** you happen to be
running LibreHardwareMonitor yourself. That is a request to your own machine.

## Sharing

Nothing is shared, sold, or transmitted to anyone. There is no third party
involved in this program at all.

## Children

gpumon is a hardware monitoring tool. It is not directed at children and collects
no personal information from anyone, of any age.

## Changes

If a future version ever collects or transmits anything, this page will say so
before that version is released, and the change will be visible in the
repository's history.

## Contact

Questions, corrections or doubts: open an issue in this repository.

---

*Last updated: 2026-09-16. gpumon is free software under the MIT licence; this
policy describes the program's behaviour, and the source is the authority on it.*
