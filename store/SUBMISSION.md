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

## What to fill in

| Field | Value |
|---|---|
| App name | `gpumon` (reserve it; if taken, `gpumon telemetry`) |
| Category | Developer tools → *System monitoring*, or Utilities |
| Pricing | Free |
| Age rating | IARC questionnaire — no violence, no user-generated content; expect 3+ / everyone |
| Privacy policy | **Required.** See below. |
| Support contact | Your repository's issues URL |
| System requirements | Windows 10 version 1809 (17763) or later, x64; a GPU from NVIDIA, AMD or Intel for the GPU half |

### The values the manifest needs

Partner Center assigns three of these when you reserve the name. **The manifest
must match them exactly or the package is rejected.** `make_store_package.py`
holds them at the top of the file:

```python
IDENTITY_NAME = "gpumon.gpumon"     # -> Package/Identity/Name
IDENTITY_PUBLISHER = "CN=gpumon"    # -> Package/Identity/Publisher
VERSION = "1.0.0.0"                 # -> Package/Identity/Version (x.y.z.0)
```

Copy the three from Partner Center → *Product identity* into those constants,
rebuild with `python make_store_package.py`, and upload
`store/gpumon-<version>-x64.msix`. It does **not** need signing first: the Store
re-signs it after certification.

### Privacy policy

Required even though the app collects nothing. A short honest one is enough, and
the repository is a fine place for it (`PRIVACY.md`), since the Store needs a URL:

> gpumon reads hardware sensors on your own machine — GPU and CPU utilisation,
> temperatures, clocks, power, memory — and stores session recordings in a SQLite
> database in your Documents folder. It sends nothing anywhere and has no
> telemetry, no accounts and no network access of its own. The one exception is
> the portable build's optional sensor setup, which downloads the PawnIO driver
> and its modules from their own project releases when you ask it to.

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
- `make_store_package.py` regenerates the package from the same source tree as
  the zip release, so the two never drift.
- The portable download stays available for anyone who wants the CPU temperature
  setup, or no Store at all. Both read the same sensor helper and write the same
  session database.
