# Submitting gpumon to the Microsoft Store

Everything Partner Center will ask for, what to answer, and the two facts about
this app that shape the submission. Written to be worked through top to bottom.

## Before anything: register

Individual developer accounts are **free** — the old $19 fee is waived. Start at
[storedeveloper.microsoft.com](https://storedeveloper.microsoft.com) and pick
*Individual developer*; identity verification is a government ID and a selfie.
Starting anywhere else (Partner Center directly, Visual Studio) gets you the
legacy paid flow, so use that URL.

## The two facts that shape this submission

**1. The package cannot install the CPU sensor driver, and must not try.**
Elevation from a packaged app needs the `allowElevation` restricted capability,
which Microsoft describes as unlikely to pass certification — and their own
guidance is to keep the interface in user mode and put admin work in a separate
component. gpumon is already built that way: the sensor helper is its own
process. So the packaged build ships *without* `--setup-sensors`, works fully for
GPU telemetry, CPU load, memory and per-core activity, and shows the CPU
temperature when the machine already has the helper installed (the portable
download installs it once; readings are published per user, not per copy).

Say this plainly in the certification notes. An app that quietly fails a feature
is a rejection; an app that documents the limitation is not.

**2. The install folder is read-only, and the app expects that.**
MSIX installs to `C:\Program Files\WindowsApps\`. `apppaths.state_path()` resolves
to the user's profile for an installed copy and to the program's own folder only
when a portable `config.json` is present beside it — so the package writes
nothing to its install folder. `test_storepackage.py` proves it by running the
packaged build from a scratch folder and checking that nothing appears beside the
executable.

## Drivers and NT services

Partner Center asks whether the product depends on non-Microsoft drivers or NT
services. Store policy **10.2.4.2** says: *"Generally, dependency on non-Microsoft
provided drivers or NT services is not allowed but may be considered case by case
for WHCP certified drivers. If your product has a dependency on non-Microsoft
provided driver(s) or NT service(s), you must disclose that dependency to
Microsoft in the certification notes."*

**The answer for the Store package: no NT services, and no driver it installs or
requires.** Audited from the source rather than from memory:

| Question | Answer |
|---|---|
| Does it create or install an NT service? | **No.** No service-creation call exists anywhere in the program. |
| What does it create instead? | A Windows **scheduled task** (`schtasks`), for the optional sensor helper — and only in the portable build. |
| Which Windows services does it talk to? | Microsoft's own, only when present: WMI for hardware identification, and the performance counters (PDH). |
| Non-Microsoft drivers | GPU readings go through NVIDIA's **NVML** and AMD's **ADL**: user-mode libraries that ship with the graphics card's own driver package, which must already be installed for the hardware to work at all. gpumon installs nothing. |
| The CPU temperature driver | **PawnIO**, a non-Microsoft signed kernel driver. The Store package neither installs nor requires it. |

The last row is the one to be careful about, so it is worth being exact.

**The Store package has no dependency on it.** It has no setup step, ships no
driver, downloads nothing, and is fully functional without one: GPU telemetry, CPU
load, memory and per-core activity all work. It reads a per-user file if a sensor
helper is *already* installed on that machine — which is a data file, not a
driver call. The packaged build never opens the driver itself: only the helper
process does, and that process is not in this package.

**The portable build, distributed outside the Store, offers it as an optional
one-time install.** The CPU package temperature is a register Windows does not
expose to user mode; reading it needs a kernel driver, which is why every monitor
that reports it installs one. That setup is user-initiated, explained, and
removable. It is not part of what the Store distributes.

### What to put in the certification notes

> gpumon reads GPU telemetry through NVIDIA's NVML and AMD's ADL — user-mode
> libraries that are part of the graphics card's own driver package, already
> present on any machine with that hardware. It creates no NT service; it uses
> Windows Task Scheduler for an optional helper, and that only in the separately
> distributed portable build.
>
> The CPU package temperature requires a kernel driver (PawnIO, a signed
> third-party driver). This Store package does not install, download, or require
> it: it has no setup step at all and every other reading works without it. The
> portable build, distributed outside the Store, offers that one-time install with
> the user's explicit consent. This package only reads a per-user data file if the
> helper is already present, and never opens the driver itself.

If a reviewer prefers the Store package to contain no sensor-setup code at all,
that is already how it is built: `python scripts/make_store_package.py` **disables the
setup path in the binary** by default. `storemode.IS_STORE_BUILD` becomes True for
that build, the command line refuses `--setup-sensors`, and the layout records it
in `STORE-BUILD.txt`. `test_storedrivers.py` runs the packaged executable with
`--setup-sensors` and asserts it refuses without downloading anything, so the claim
is checked rather than asserted. `--allow-sensor-setup` exists only for building a
local package that keeps the full feature set.



| Field | Value |
|---|---|
| App name | `gpumon` (reserve it; if taken, `gpumon telemetry`) |
| Category | Developer tools → *System monitoring*, or Utilities |
| Pricing | Free |
| Age rating | IARC questionnaire — no violence, no user-generated content; expect 3+ / everyone |
| Privacy policy | **Required.** See below. |
| Support contact | Your repository's issues URL |
| System requirements | See the measured table below |
| OS and architecture | Windows 10 version 1809 (build 17763) or later, **x64 only** — what the package manifest declares |

### System requirements, as measured

The Store turns these into the listing's promises, so they were measured rather
than estimated. Every number comes from the running program on the machine this
was built on (Windows 11, i9-7900X, three GPUs).

| Feature | Minimum | Recommended | Why |
|---|---|---|---|
| Touch screen | ☐ | ☐ | No touch API is used. It works on a touchscreen but never asks for one. |
| Keyboard | **☑** | **☑** | Shortcuts are a core part of it (`L`, `M`, `S`, `A`, `G`, `T`, `C`, `P`, `Q`), and the terminal view is keyboard-only. |
| Mouse | **☑** | **☑** | Dragging a graph to pan through history is the main way to explore it; the wheel is the other. |
| Camera | ☐ | ☐ | Not used. |
| NFC HCE / Proximity | ☐ | ☐ | Not used. |
| Bluetooth LE | ☐ | ☐ | Not used. |
| Telephony | ☐ | ☐ | Not used. |
| Microphone | ☐ | ☐ | Not used. |
| Memory | **2 GB** | **8 GB** | The program and its helper use about **116 MB** together (measured). 2 GB matches Windows' own 64-bit floor; 8 GB reflects that this is usually run *alongside* the workload being measured. |
| DirectX | Not specified | Not specified | No 3D API is used — the window is drawn with GDI through tkinter. |
| Dedicated GPU memory | Not specified | 1 GB | It runs without a discrete GPU and still reports CPU, memory and counters. A discrete GPU is what it is *for*, though. |
| Processor | `x64 processor (Intel or AMD)` | `Intel Core i5 / AMD Ryzen 5 or newer` | x64 only: there is no ARM64 build. CPU temperature is read on Intel, and on AMD through the same driver path. |
| Graphics | `Any Windows GPU with its vendor driver installed` | `Discrete NVIDIA or AMD GPU` | NVIDIA and AMD give the full set — temperature, hotspot, VRAM, clocks, power. Other GPUs fall back to Windows performance counters. |

Two corrections to the form as it was filled in: **Mouse belongs in Minimum**, not
only in Recommended, because panning through history is a drag. **Keyboard is
right as it is.**

Not asked for, but worth knowing: a recording uses **about 20 MB per hour** at the
default 1 Hz with per-core temperatures on (53–76 values per tick, about 111 bytes
each, measured from the session database). The recordings are the user's own files
in their Documents folder.


### The values the manifest needs

Partner Center assigns these when you reserve the name. **The manifest must match
them exactly or the package is rejected.** Set them in the environment, so a
package can be built for a reservation without editing code:

```powershell
$env:GPUMON_STORE_NAME = "…"            # Product identity -> Package/Identity/Name
$env:GPUMON_STORE_PUBLISHER = "CN=…"    # Product identity -> Package/Identity/Publisher
$env:GPUMON_STORE_DISPLAY_NAME = "…"    # your publisher display name
$env:GPUMON_STORE_VERSION = "1.0.0.0"   # x.y.z.0
python scripts/make_store_package.py
```

The package does not need signing first: the Store re-signs it after
certification, which is the whole reason this route costs nothing.

The defaults are at the top of `scripts/make_store_package.py`.

**`PublisherDisplayName` is not the same thing as `Publisher`, and getting it
wrong was the first validation error this package hit.** Partner Center checks it
against the publisher display name on the *account* — `Darrell Coburn` here — and a
mismatch stops the submission with:

> The PublisherDisplayName element in the app manifest of gpumon-1.0.0.0-x64.msix
> is gpumon, which doesn't match your publisher display name: Darrell Coburn.

### What Partner Center says when the package is uploaded

Three messages are normal, and one is a choice:

| Message | What it means |
|---|---|
| **Error:** `PublisherDisplayName` mismatch | Set `GPUMON_STORE_DISPLAY_NAME` to your account's publisher display name and rebuild. |
| **Warning:** restricted capability `runFullTrust` requires approval | Expected for a desktop app in a package. Justification below. |
| **Error:** "must provide a package that supports each selected device family" | Uncheck the families this is not for — **Xbox in particular**, since a hardware monitor has nothing to monitor there. Keep **Windows 10/11 Desktop**. |
| **Error:** "must upload at least one package" | A consequence of the failed upload, not a separate problem. |

Justification for the `runFullTrust` request, for whenever the form asks:

> gpumon is a desktop application: it draws its own window and reads hardware
> sensors through the graphics driver's own user-mode libraries (NVIDIA NVML, AMD
> ADL) and Windows performance counters. It needs full trust because a packaged app
> cannot reach those interfaces from inside a container. It installs no driver,
> creates no service, requires no elevation, and writes only to the user's own
> profile and Documents folder.

### Privacy policy

**It exists: `PRIVACY.md` in the repository root.** Partner Center wants a URL,
not an upload, so it needs to be reachable on the web before you submit. The
repository is at [github.com/Dcoburn2/gpumon](https://github.com/Dcoburn2/gpumon),
so the URL to give Partner Center is:

```
https://github.com/Dcoburn2/gpumon/blob/main/PRIVACY.md
```

These also work, if the form prefers them:

```
https://raw.githubusercontent.com/Dcoburn2/gpumon/main/PRIVACY.md
https://dcoburn2.github.io/gpumon/PRIVACY.md     (after enabling GitHub Pages)
```

A GitHub URL is accepted — it does not need to be a domain you own.

The policy says the app collects nothing, which is true and checkable: the only
outbound requests in the whole program are the two GitHub downloads in the
portable build's optional sensor setup, plus its own loopback web interface.
`test_privacy.py` asserts both claims against the source, so the policy cannot
drift away from the code.

### Screenshots

At least one, 1366×768 or larger, PNG. `python -m store_capture` takes them from a
running window at the required size; put them in `store/screenshots/`.

### Description

The Store listing wants a short description and a long one. Draft:

**Short:** Live GPU and system monitor: utilisation, temperatures, VRAM, clocks,
power and fan for every graphics card, with CPU load, memory and per-core
activity, graphs that scroll back through history, session logging and alarms.

**Long:** gpumon shows the whole machine on one screen. Every GPU gets a card
with its own graphs — utilisation, edge and hotspot temperature, VRAM, clocks,
power and fan, read through the vendor's own drivers with no kernel driver
needed. The CPU and memory get their own card, with per-core activity on demand.
Graphs scroll back through history: drag one sideways to see what was happening a
minute ago without interrupting the recording. Sessions are logged to a database
you own, with markers, alarm thresholds, and a summary that charts and exports
the run.

The CPU package temperature needs a kernel driver, because Windows does not
expose that reading to ordinary programs. The portable download sets that up once
with a single prompt; this packaged version shows the temperature when it is
already set up and works fully without it.

## Certification notes

Paste this into *Notes for certification* — it pre-empts the questions:

> gpumon is a hardware monitor. It reads GPU telemetry through NVML and ADL (the
> drivers already on the machine) and CPU/memory figures through normal Windows
> APIs. It requires no special permissions and runs entirely in user mode.
>
> CPU *package* temperature is the one reading Windows does not expose to user
> mode; it needs a kernel driver. A packaged app cannot install one, so this
> build does not attempt it: the CPU temperature appears only if the machine
> already has the helper installed, which the separately distributed portable
> build sets up once with the user's consent. Every other reading works without
> it. This is deliberate, and the app says so in its interface rather than
> failing silently.
>
> The app writes its settings, logs and session database to the user's profile
> and Documents folder. It has no network access of its own; the optional sensor
> setup in the portable build downloads a driver from its own project release.
>
> Test account: not required — the app has no accounts.

## After it is live

- The Store signs each package, so there is nothing to buy and no certificate to
  renew. Keep the version in the manifest increasing with each submission;
  Partner Center rejects a package whose version it has already seen.
- `scripts/make_store_package.py` regenerates the package from the same source tree as
  the zip release, so the two never drift.
- The portable download stays available for anyone who wants the CPU temperature
  setup, or no Store at all. Both read the same sensor helper and write the same
  session database.
