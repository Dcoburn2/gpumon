"""Assemble the finished product, in the shapes a download can take.

Three artifacts, all built from the same source:

  `release/`          the folder build - fast to start, needs its `_internal`
                      folder kept beside the exe
  `release-onefile/`  the same program as a single executable that unpacks
                      itself at each start: easier to hand to somebody, slower to
                      open
  `*.zip`             the folder build zipped, which is what a download link wants

Nothing third-party is redistributed. The PawnIO driver and its signed sensor
modules are GPL-2.0 and this project is MIT, so `--setup-sensors` fetches them
from the vendor's own releases instead.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile

#: The repository root. This script lives in scripts/, one level down.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = os.path.join(HERE, "release")
SINGLE = os.path.join(HERE, "release-onefile")
BUILD = os.path.join(HERE, "dist", "gpumon")

# The program's modules are at the root, so the name in the archive comes from the
# program itself rather than from a string here that goes stale at every version.
if HERE not in sys.path:
    sys.path.insert(0, HERE)

#: Copied into the release folder, and nothing else.
RELEASE_SCRIPT = "start-sensors.cmd"


def set_store_mode(enabled: bool) -> None:
    """State in the source what kind of build this is, before PyInstaller runs.

    The flag is baked into the executable, so it has to be right *before* the
    build rather than adjusted afterwards. `--store` sets it; anything else clears
    it, which also protects against a leftover: the Store packaging clears it in a
    `finally`, and a killed process does not run a `finally`.

    Clearing it unconditionally was wrong in the other direction - it silently
    rebuilt the Store package as a portable one, and the test that asks the
    packaged build to refuse the sensor setup caught that.
    """
    path = os.path.join(HERE, "storemode.py")
    if not os.path.exists(path):
        return
    text = open(path, encoding="utf-8").read()
    wanted = "IS_STORE_BUILD = True" if enabled else "IS_STORE_BUILD = False"
    current = ("IS_STORE_BUILD = True" if "IS_STORE_BUILD = True" in text
               else "IS_STORE_BUILD = False")
    if current == wanted:
        return
    import re
    text = re.sub(r"IS_STORE_BUILD = (True|False)", wanted, text)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"  storemode.IS_STORE_BUILD = {enabled}")


def build_executable(store: bool = False) -> bool:
    """Run PyInstaller on the program, from the repository root."""
    set_store_mode(store)
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--windowed", "--onedir", "--name", "gpumon",
        # Keep PyInstaller's own files out of the repository root: without this it
        # writes gpumon.spec next to the source on every build, which is how a
        # "deleted" spec file kept coming back.
        "--specpath", "build",
        # Absolute paths, because --add-data is resolved relative to the spec file
        # once the spec lives in build/ rather than beside the source.
        "--icon", os.path.join(HERE, "gpumon.ico"),
        "--add-data", os.path.join(HERE, "gpumon.ico") + ";.",
        "--add-data", os.path.join(HERE, "gpumon.png") + ";.",
        "--hidden-import", "psutil", "--hidden-import", "wmi",
        "--exclude-module", "tkinter.test", "--exclude-module", "test",
        os.path.join(HERE, "gpumon.py"),
    ]
    print("building the executable...")
    result = subprocess.run(command, cwd=HERE, capture_output=True, text=True,
                            errors="replace")
    if result.returncode != 0:
        output = result.stderr or result.stdout
        tail = output.strip().splitlines()[-6:]
        print("build failed:")
        for line in tail:
            print(f"  {line}")
        if "Access is denied" in output and "_internal" in output:
            # The usual cause on a machine that is also using the program: the
            # scheduled task's sensor helper is running from this build, so its
            # files are open. Saying so saves hunting for it.
            print("\n  A sensor helper is running from this build. Stop it first:")
            print("    schtasks /end /tn gpumon-sensors-web")
            print("    schtasks /end /tn gpumon-sensors")
        return False
    return os.path.exists(os.path.join(BUILD, "gpumon.exe"))


def build_single_file() -> bool:
    """Build the one-file variant, so the two shapes can be compared.

    Same program, packed into a single executable that unpacks itself into a
    temporary directory at each start: nicer to hand to somebody, and slower to
    launch, which is the trade.
    """
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--windowed", "--onefile", "--name", "gpumon-portable",
        "--specpath", "build",
        # Absolute, for the same reason as the folder build above.
        "--icon", os.path.join(HERE, "gpumon.ico"),
        "--add-data", os.path.join(HERE, "gpumon.ico") + ";.",
        "--add-data", os.path.join(HERE, "gpumon.png") + ";.",
        "--hidden-import", "psutil", "--hidden-import", "wmi",
        "--exclude-module", "tkinter.test", "--exclude-module", "test",
        os.path.join(HERE, "gpumon.py"),
    ]
    print("building the single-file executable...")
    result = subprocess.run(command, cwd=HERE, capture_output=True, text=True,
                            errors="replace")
    if result.returncode != 0:
        print("build failed:")
        for line in (result.stderr or result.stdout).strip().splitlines()[-5:]:
            print(f"  {line}")
        return False
    return os.path.exists(os.path.join(HERE, "dist", "gpumon-portable.exe"))


def sign_artifacts(require: bool = False) -> int:
    """Sign what was built, before anything is checksummed or zipped.

    Signing is configuration, not code: with a signing identity in the
    environment the artifacts come out signed and timestamped, and without one
    they come out unsigned with a line saying so. `require` turns the second case
    into a failure, which is what a release pipeline wants.
    """
    import signing

    artifacts = [path for path in (os.path.join(RELEASE, "gpumon.exe"),
                                   os.path.join(SINGLE, "gpumon.exe"))
                 if os.path.exists(path)]
    print("\nsigning...")
    signed, message = signing.sign(artifacts)
    print(f"  {'+ ' if signed else '! '}{message}")
    if not signed:
        if require:
            print("  --sign was asked for, so this build stops here.")
            return 1
        print("  unsigned: no certificate is configured, so Windows will warn on")
        print("  first run. See the Signing section of README.md.")
    return 0


def write_checksums() -> int:
    """One checksum file beside each artifact, then the zip's.

    Written after signing, because signing changes the bytes. Each file names
    only what sits beside it: an earlier version listed both executables as
    "gpumon.exe" in one file, which told a reader nothing about which was which.
    """
    import signing

    for folder in (RELEASE, SINGLE):
        executable = os.path.join(folder, "gpumon.exe")
        if not os.path.exists(executable):
            continue
        target = os.path.join(folder, "SHA256SUMS.txt")
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(signing.checksums([executable]))
        print(f"  wrote {os.path.relpath(target, HERE)}")
    return 0


def archive_name() -> str:
    """The zip's name, taken from the program's own version.

    It used to be spelled out in three places, so a version bump meant finding all
    three or shipping a file named after the previous release.
    """
    import sampler
    return f"gpumon-{sampler.APP_VERSION}-windows-x64.zip"


def checksum_zip() -> int:
    """A `.sha256` beside the zip, in the form `sha256sum -c` reads."""
    import signing

    archive = os.path.join(HERE, archive_name())
    if not os.path.exists(archive):
        return 0
    target = archive + ".sha256"
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(signing.checksums([archive]))
    print(f"  wrote {os.path.basename(target)}")
    return 0


def assemble_single_file() -> int:
    """Put the single-file build in its own folder, with its own readme."""
    target = SINGLE
    source = os.path.join(HERE, "dist", "gpumon-portable.exe")
    if not os.path.exists(source):
        print(f"nothing to copy: {source} does not exist")
        return 1
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.makedirs(target)
    shutil.copy2(source, os.path.join(target, "gpumon.exe"))
    write_release_readme(target, single_file=True)
    # The licence travels with the program. MIT requires its notice to be included
    # in copies of the software, and somebody who downloads a zip has no other way
    # to see it.
    licence = os.path.join(HERE, "LICENSE")
    if os.path.exists(licence):
        shutil.copy2(licence, os.path.join(target, "LICENSE"))
    icon = os.path.join(HERE, "gpumon.ico")
    if os.path.exists(icon):
        shutil.copy2(icon, os.path.join(target, "gpumon.ico"))
    size = os.path.getsize(os.path.join(target, "gpumon.exe"))
    print(f"\nrelease-onefile/ assembled: {size / 1024 / 1024:.1f} MB in one file")
    for name in sorted(os.listdir(target)):
        path = os.path.join(target, name)
        print(f"  {name}  ({os.path.getsize(path) / 1024:.0f} KB)")
    return 0


def make_zip() -> int:
    """A zip of the folder build: what a download link wants."""
    target = os.path.join(HERE, archive_name())
    if os.path.exists(target):
        os.remove(target)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
        for root, _dirs, names in os.walk(RELEASE):
            for name in names:
                full = os.path.join(root, name)
                bundle.write(full, os.path.join(
                    "gpumon", os.path.relpath(full, RELEASE)))
    print(f"\nzipped: {os.path.basename(target)}  "
          f"({os.path.getsize(target) / 1024 / 1024:.1f} MB)")
    return 0


def write_release_scripts() -> None:
    """The one script a new user needs: the sensor setup.

    Written for the packaged build, so it calls the executable rather than
    `gpumon.py` - there is no `gpumon.py` in the release folder, and a script
    that assumed one would fail the first time anybody ran it.
    """
    target = os.path.join(RELEASE, RELEASE_SCRIPT)
    with open(target, "w", encoding="ascii", newline="\r\n") as handle:
        handle.write(r"""@echo off
REM Give gpumon the processor's temperature.
REM
REM Windows will not hand a CPU temperature to an ordinary program: it lives in a
REM privileged register, which takes a kernel driver. This sets that up - once.
REM Expect one Windows prompt; after it, nothing is ever asked again.
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo   Asking Windows for administrator rights - accept the prompt that follows.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process -FilePath '%~dp0gpumon.exe' -ArgumentList '--setup-sensors' -Verb RunAs -WorkingDirectory '%~dp0' -Wait"

echo.
echo   Done. Start gpumon and the CPU temperature is live.
echo.
pause
exit /b 0
""")
    print(f"  wrote {RELEASE_SCRIPT}")


def write_release_readme(directory: str | None = None,
                         single_file: bool = False) -> None:
    """The readme a downloader gets. Two shapes, so two sets of instructions."""
    import sampler

    target = os.path.join(directory or RELEASE, "README.md")
    running = (
        "Double-click **`gpumon.exe`**. It is one file: put it wherever you like,\n"
        "including straight on the desktop.\n\n"
        "It unpacks itself into a temporary folder each time it starts, which\n"
        "costs about half a second on a modern machine (measured: 3.7s to a\n"
        "window, against 3.3s for the folder build).\n"
        if single_file else
        "Double-click **`gpumon.exe`**.\n\n"
        "**Keep the whole folder together.** `gpumon.exe` needs the `_internal`\n"
        "folder beside it — copy the exe on its own and it will not start. If you\n"
        "want it somewhere else, move the entire folder.\n"
    )
    with open(target, "w", encoding="utf-8", newline="\r\n") as handle:
        handle.write(f"""# gpumon

A live monitor and logger for GPUs and the rest of the machine: utilisation,
temperature, hotspot, VRAM, clocks, power and fan for every graphics card, plus
CPU load, CPU temperature, per-core activity and memory, with graphs that scroll
back through history.

![gpumon](gpumon.png)

Version {sampler.APP_VERSION}.

## What you need

- Windows 10 or 11, 64-bit.
- Nothing else: no Python, no installer, no drivers.

## Running it

""")
        handle.write(running)
        handle.write("""
The first time it runs it offers to put a shortcut on your desktop, so after that
you can launch it from there.

Windows may show a "Windows protected your PC" notice the first time, because
this build is not code-signed. *More info → Run anyway*.

## CPU temperature (one optional setup step)

GPU sensors work immediately. The **CPU temperature** needs a kernel driver,
because that reading lives in a privileged register Windows will not expose to an
ordinary program — it is the one thing no monitor can do without one.

If you want it, click **START SENSORS** on the CPU card and accept the one
Windows prompt. It installs [PawnIO](https://pawnio.eu) (a signed, open-source
driver) and registers a helper that reads the processor's temperature from then
on. Nothing else is ever asked. The first time needs an internet connection, to
fetch the driver and its sensor modules from their own project.

Skip it and everything else still works — the CPU temperature simply shows as
unavailable rather than being guessed at.

Intel processors are read directly. AMD processors are supported through the same
driver, but that decoding has not been tried on AMD hardware yet — if the number
looks wrong there, that is where to look.

## Using it

All the numbers are on one screen. Each GPU gets a card with its own graphs; the
CPU and memory get one at the bottom.

| Key | What it does |
|-----|--------------|
| `L` | start or stop recording to the session log |
| `M` | drop a marker in the log, to find a moment later |
| `S` | open the summary — charts, statistics and alarms for the session |
| `A` | set the alarm thresholds |
| `G` | show or hide the graphs |
| `T` | cycle the colour themes |
| `C` | collapse or expand the GPU cards |
| `P` | resume live after scrolling back through history |
| `Q` | browse past sessions, or quit |

**Drag a graph** sideways to look back through history, or hold **shift** and use
the wheel. Double-click, or press `P`, to return to live. Recording continues
while you look back: panning changes what you see, not what is logged.

Click a card's title to collapse it.

## Where your data goes

Recordings are stored in `Documents\\gpumon\\sessions.db`, one database for the
whole machine. Every sample is kept; the summary can export them.

Settings live in `config.json` beside the program, and the sensor helper's
readings in `%LOCALAPPDATA%\\gpumon\\`.

If something goes wrong, `gpumon-error.log` beside the program holds the
traceback.

## Uninstalling

Delete this file or folder. If you set up the CPU sensors, also remove PawnIO from
*Add or remove programs* and the two `gpumon-sensors*` tasks from *Task
Scheduler*.

## Licence

gpumon is free software under the MIT licence — see `LICENSE` beside the program.
You may use it, change it and pass it on. It collects nothing about you: see the
privacy policy at https://github.com/Dcoburn2/gpumon/blob/main/PRIVACY.md
""")
    print(f"  wrote README.md{'' if not single_file else ' (single-file variant)'}")


#: Files the program writes at run time. A release folder must not carry them:
#: a config from the machine it was built on would hand a new user someone
#: else's settings, and the shortcut question would look already answered.
RUNTIME_FILES = ("config.json", "msr-sensors.json", "msr-heartbeat",
                 "gpumon-launch.log", "sensors-setup.log", "sessions.db",
                 "sessions.db-wal", "sessions.db-shm")


def clean_runtime_files() -> None:
    removed = []
    for name in os.listdir(RELEASE):
        if name in RUNTIME_FILES or name.startswith("diag"):
            try:
                os.remove(os.path.join(RELEASE, name))
                removed.append(name)
            except OSError:
                pass
    if removed:
        print(f"  cleared run-time files from the release: {', '.join(removed)}")


def assemble() -> int:
    if not os.path.isdir(BUILD):
        print(f"nothing to package: {BUILD} does not exist")
        return 1
    if os.path.isdir(RELEASE):
        shutil.rmtree(RELEASE)
    os.makedirs(RELEASE)

    # The executable first, where people look for it, then its support folder.
    shutil.copy2(os.path.join(BUILD, "gpumon.exe"),
                 os.path.join(RELEASE, "gpumon.exe"))
    shutil.copytree(os.path.join(BUILD, "_internal"),
                    os.path.join(RELEASE, "_internal"))
    # The icon, so the shortcut and the README have something to point at, and
    # the preview image the README shows.
    for name in ("gpumon.ico", "gpumon.png"):
        source = os.path.join(BUILD, "_internal", name)
        if os.path.exists(source):
            shutil.copy2(source, os.path.join(RELEASE, name))

    # The licence, which MIT requires to travel with copies of the program.
    licence = os.path.join(HERE, "LICENSE")
    if os.path.exists(licence):
        shutil.copy2(licence, os.path.join(RELEASE, "LICENSE"))

    write_release_scripts()
    write_release_readme()
    clean_runtime_files()

    # Anything that is not part of a release is a bug in this script, so say so
    # rather than shipping it.
    allowed_files = {"gpumon.exe", "README.md", RELEASE_SCRIPT, "gpumon.ico",
                     "gpumon.png", "SHA256SUMS.txt", "LICENSE"}
    unexpected = [name for name in os.listdir(RELEASE)
                  if name not in allowed_files and name != "_internal"
                  and not os.path.isdir(os.path.join(RELEASE, name))]
    if unexpected:
        print(f"unexpected files in the release folder: {unexpected}")
    tests = [name for name in os.listdir(RELEASE) if name.startswith("test")]
    if tests:
        print(f"tests leaked into the release folder: {tests}")
        return 1

    total = sum(os.path.getsize(os.path.join(root, name))
                for root, _dirs, names in os.walk(RELEASE)
                for name in names)
    print(f"\nrelease/ assembled: {total / 1024 / 1024:.1f} MB")
    for name in sorted(os.listdir(RELEASE)):
        path = os.path.join(RELEASE, name)
        if os.path.isdir(path):
            print(f"  {name}/  (support files)")
        else:
            print(f"  {name}  ({os.path.getsize(path) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    wanted_single = "--onefile" in sys.argv or "--all" in sys.argv
    # --store builds the program the Store package needs: identical except that it
    # refuses to install the sensor driver, which is the claim made to Microsoft.
    store = "--store" in sys.argv
    if store or "--build" in sys.argv or "--all" in sys.argv \
            or not os.path.isdir(BUILD):
        if not build_executable(store=store):
            raise SystemExit(1)
    code = assemble()
    if wanted_single and code == 0:
        if not build_single_file():
            raise SystemExit(1)
        code = assemble_single_file() or code
    # Sign first, then checksum, then zip: signing changes the bytes, and the
    # zip should contain the checksums it was built with.
    if code == 0:
        code = sign_artifacts(require="--sign" in sys.argv)
    if code == 0:
        code = write_checksums()
    if code == 0:
        code = make_zip()
    if code == 0:
        code = checksum_zip()
    raise SystemExit(code)
