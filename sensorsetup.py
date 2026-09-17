"""Set up gpumon's own CPU sensors: the driver, the modules, and the helper task.

What a new machine needs, and why each step is unavoidable:

 1. **A kernel driver.** Reading a processor's thermal register is a privileged
    instruction, so it needs one. gpumon uses PawnIO - signed, open source, by
    namazso - and does not ship it, because PawnIO is GPL-2.0 and bundling it
    into this project would be a licensing decision for the whole project. The
    official installer is downloaded from the vendor's own release and run
    elevated: one prompt.
 2. **The signed modules** that do the actual register reads. Those come from
    PawnIO's own module release, again rather than being redistributed here.
 3. **A task** to start the helper with the rights the driver needs, so nobody
    is prompted again - at logon, and on demand when the program wants it.

That is the whole cost: one elevation, once.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(HERE, "tools")

#: Modules are read from the bundle and written beside the program, so a
#: packaged release carries them and a source checkout can fetch what it lacks.
import apppaths  # noqa: E402

MODULES_DIR = apppaths.user_path("pawnio-modules")

#: The vendor's own installer, not a copy: PawnIO is GPL-2.0, so redistributing
#: it inside gpumon would put its licence on this project's distribution. For
#: the same reason the modules are fetched from their release rather than
#: committed here.
PAWNIO_SETUP_URL = ("https://github.com/namazso/PawnIO.Setup/releases/latest/"
                    "download/PawnIO_setup.exe")
MODULES_RELEASE_API = ("https://api.github.com/repos/namazso/PawnIO.Modules/"
                       "releases/latest")

#: The modules gpumon loads, and what each is for.
REQUIRED_MODULES = {
    "IntelMSR.bin": "Intel CPU temperature, clocks and voltage",
    "AMDFamily17.bin": "AMD CPU temperature (Family 17h and later)",
    "RyzenSMU.bin": "AMD Ryzen power and thermal tables",
}

STARTUP_TASK = "gpumon-sensors"
HELPER_TASK = "gpumon-sensors-web"


class _Step:
    """Collects the outcome of the setup steps so it can be summarised."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def ok(self, text: str) -> None:
        print(f"  + {text}")
        self.lines.append(("+", text))

    def warn(self, text: str) -> None:
        print(f"  ! {text}")
        self.lines.append(("!", text))

    def head(self, text: str) -> None:
        print(f"\n== {text}")




def _no_window() -> dict[str, int]:
    """Subprocess arguments that keep a console from flashing up, on Windows.

    An empty dict elsewhere: passing `creationflags=0` is not the same as leaving
    the keyword out, and subprocess refuses the keyword on POSIX entirely.
    """
    return {"creationflags": 0x08000000} if os.name == "nt" else {}

def _print_step(text: str) -> None:
    print(f"\n== {text}")


# --------------------------------------------------------------------------
# The driver
# --------------------------------------------------------------------------
def pawnio_installed() -> bool:
    """True when PawnIO is installed, which is all we need to use it."""
    if os.name != "nt":
        return False
    import winreg
    for hive, path in (
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion"
             r"\Uninstall\PawnIO")):
        try:
            with winreg.OpenKey(hive, path):
                return True
        except OSError:
            continue
    return False


def pawnio_version() -> str:
    if os.name != "nt":
        return ""
    import winreg
    try:
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO"
        ) as key:
            return str(winreg.QueryValueEx(key, "DisplayVersion")[0])
    except OSError:
        return ""


def download(url: str, destination: str, timeout: int = 180) -> str | None:
    """Fetch a file, with a browser-ish User-Agent (GitHub rejects the default)."""
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    request = urllib.request.Request(
        url, headers={"User-Agent": "gpumon-setup",
                      "Accept": "application/octet-stream"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except (urllib.error.URLError, OSError) as exc:
        print(f"    download failed: {exc}")
        return None
    if not payload:
        print("    download was empty")
        return None
    with open(destination, "wb") as handle:
        handle.write(payload)
    return destination


def download_pawnio_installer() -> str | None:
    target = os.path.join(TOOLS_DIR, "PawnIO_setup.exe")
    print(f"    fetching {PAWNIO_SETUP_URL}")
    path = download(PAWNIO_SETUP_URL, target)
    if path:
        print(f"    {os.path.getsize(path):,} bytes -> {path}")
    return path


def install_pawnio(installer: str) -> bool:
    """Run the vendor's installer.

    Silent first: the installer accepts `/S`, and a setup step that needs no
    clicks beyond the one elevation is the point. If it is not installed
    afterwards the user is told to run it themselves rather than left guessing.
    """
    if os.name != "nt":
        return False
    try:
        process = subprocess.Popen([installer, "/S"], **_no_window())
        process.wait(timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"    the installer did not finish: {exc}")
        return False
    for _ in range(20):
        if pawnio_installed():
            return True
        time.sleep(0.5)
    return False


# --------------------------------------------------------------------------
# The modules
# --------------------------------------------------------------------------
def missing_modules() -> list[str]:
    """Modules still needed: one is present if it is bundled *or* installed."""
    import pawnio
    return [name for name in REQUIRED_MODULES
            if not os.path.exists(pawnio.module_path(name))]


def install_modules_from_zip(archive: str) -> list[str]:
    """Extract the modules we need from PawnIO's module release."""
    wanted = {name.lower(): name for name in REQUIRED_MODULES}
    installed: list[str] = []
    os.makedirs(MODULES_DIR, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.namelist():
                base = os.path.basename(member).lower()
                if base in wanted:
                    with bundle.open(member) as source, \
                            open(os.path.join(MODULES_DIR, wanted[base]),
                                 "wb") as target:
                        shutil.copyfileobj(source, target)
                    installed.append(wanted[base])
    except (OSError, zipfile.BadZipFile) as exc:
        print(f"    could not read the module archive: {exc}")
    return installed


def download_modules() -> tuple[bool, str]:
    """Fetch PawnIO's signed modules from the project's own release."""
    print(f"    querying {MODULES_RELEASE_API}")
    request = urllib.request.Request(
        MODULES_RELEASE_API, headers={"User-Agent": "gpumon-setup"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            release = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"could not reach the module release: {exc}"
    asset = next((item for item in release.get("assets", [])
                  if item["name"].lower().endswith(".zip")), None)
    if not asset:
        return False, "the module release has no zip asset"
    archive = download(asset["browser_download_url"],
                       os.path.join(TOOLS_DIR, asset["name"]))
    if not archive:
        return False, "the module archive could not be downloaded"
    installed = install_modules_from_zip(archive)
    try:
        os.remove(archive)
    except OSError:
        pass
    if not installed:
        return False, f"the archive held none of {', '.join(REQUIRED_MODULES)}"
    return True, f"installed {', '.join(installed)}"


def modules_available() -> bool:
    return not missing_modules()


# --------------------------------------------------------------------------
# The tasks that supply the elevation
# --------------------------------------------------------------------------
def windowless_python(executable: str | None = None) -> str:
    """A GUI-subsystem interpreter, so nothing opens a console window.

    `python.exe` is a console program: any task or elevated child started with it
    gets a black window, which appears and disappears while the user is looking at
    gpumon. `pythonw.exe` is the same interpreter built for windows, and the
    program's output already goes to a log file rather than to a console, so
    nothing is lost.
    """
    executable = executable or sys.executable
    if executable.lower().endswith("python.exe"):
        candidate = executable[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(candidate):
            return candidate
    return executable


def helper_action() -> str:
    """What both tasks run: the sensor helper, with the rights the driver needs.

    A packaged build is its own executable, so the task runs `gpumon.exe
    --sensor-helper`; a source checkout runs the script through a windowless
    interpreter. Either way, nothing appears on screen when the sensors start.
    """
    if getattr(sys, "frozen", False):
        return f'"{os.path.abspath(sys.executable)}" --sensor-helper'
    return (f'"{windowless_python()}" "{os.path.join(HERE, "gpumon.py")}" '
            f'--sensor-helper')


def _schtasks(args: list[str]) -> tuple[int, str]:
    result = subprocess.run(["schtasks"] + args, capture_output=True, text=True,
                            errors="replace",
                            **_no_window())
    output = (result.stdout + result.stderr).strip().splitlines()
    return result.returncode, output[-1].strip() if output else ""


def create_task(name: str, trigger: list[str]) -> tuple[bool, str]:
    code, detail = _schtasks(["/create", "/f", "/tn", name, "/tr",
                              helper_action(), "/rl", "highest"] + trigger)
    if code != 0:
        return False, f"could not register '{name}': {detail}"
    return True, f"registered '{name}'"


def delete_task(name: str) -> tuple[bool, str]:
    code, detail = _schtasks(["/delete", "/f", "/tn", name])
    return code == 0, detail


def task_exists(name: str) -> bool:
    code, _ = _schtasks(["/query", "/tn", name])
    return code == 0


def task_action(name: str = HELPER_TASK) -> str:
    """What a registered task actually runs, or an empty string."""
    code, _ = _schtasks(["/query", "/tn", name, "/fo", "LIST", "/v"])
    if code != 0:
        return ""
    result = subprocess.run(["schtasks", "/query", "/tn", name, "/fo", "LIST",
                             "/v"], capture_output=True, text=True,
                            errors="replace",
                            **_no_window())
    for line in result.stdout.splitlines():
        if "Task To Run" in line:
            return line.split(":", 1)[-1].strip()
    return ""


def task_is_current(name: str = HELPER_TASK) -> bool:
    """Whether the task runs the sensor helper this build actually has.

    A task left over from an earlier version points at a file that no longer
    exists: running it starts nothing and the program waits for a reading that
    will never come. Checking the recorded action turns that silent wait into an
    answer - the setup needs doing again.
    """
    if not task_exists(name):
        return False
    return "--sensor-helper" in task_action(name)


def is_elevated() -> bool:
    """True when this process already has administrator rights."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def run_setup_here() -> tuple[bool, str]:
    """Run the whole setup in this process, for when it is already elevated.

    Someone who launched gpumon as administrator has already granted what the
    setup needs, so asking again would be a prompt for nothing.
    """
    try:
        code = setup([])
    except Exception as exc:  # noqa: BLE001
        return False, f"the setup failed: {exc}"
    return code == 0, "setup finished" if code == 0 else "setup did not finish"


def setup_marker() -> str:
    """Where the elevated setup records how it went."""
    import apppaths
    return apppaths.state_path("sensors-setup.exit")


def begin_setup() -> None:
    """Clear any previous outcome, so the next one cannot be mistaken for it."""
    try:
        os.remove(setup_marker())
    except OSError:
        pass


def setup_outcome() -> str:
    """How the last elevated setup finished, or "" while it is still running.

    The elevated child may be a windowed program with nowhere to print, and its
    console closes the moment it ends, so the outcome is written to a file
    instead. Without this the only visible sign of a failure is a window that
    appeared and went away - which is exactly how this looked the first time.
    """
    marker = setup_marker()
    if not os.path.exists(marker):
        return ""
    try:
        with open(marker, encoding="utf-8", errors="replace") as handle:
            code = handle.read().strip()
    except OSError:
        return ""
    tail = ""
    try:
        with open(os.path.join(HERE, "sensors-setup.log"), encoding="utf-8",
                  errors="replace") as handle:
            lines = [line.strip() for line in handle if line.strip()]
        tail = lines[-1] if lines else ""
    except OSError:
        pass
    if code == "0":
        return "the setup finished" + (f" - {tail}" if tail else "")
    return (f"the setup failed (exit {code})" + (f" - {tail}" if tail else ""))


def setup_launch() -> tuple[str, str, str]:
    """The three parts of the elevated launch: executable, arguments, directory.

    Split out so the command can be built and checked without elevating anything.
    `Start-Process -ArgumentList` takes a list, and wrapping that list in another
    pair of quotes produced `''script','--setup-sensors''`, which PowerShell
    rejects - the elevated window opened, errored and closed before anything ran.
    """
    if getattr(sys, "frozen", False):
        executable = os.path.abspath(sys.executable)
        workdir = os.path.dirname(executable)
        return executable, "'--setup-sensors'", workdir
    script = os.path.join(HERE, "gpumon.py")
    # PowerShell single-quoted strings escape a quote by doubling it.
    quoted = "'" + script.replace("'", "''") + "'"
    return windowless_python(), f"{quoted},'--setup-sensors'", HERE


def setup_command(arguments: str | None = None) -> str:
    """The PowerShell that raises the prompt and runs the setup.

    `arguments` exists so this can be exercised without running the real setup:
    the same shape is used with a flag that returns immediately.

    `-WindowStyle Hidden` is belt and braces alongside `pythonw.exe`: the elevated
    child must not put a console on screen while the user is watching gpumon.
    """
    executable, default_arguments, workdir = setup_launch()
    return (f"Start-Process -FilePath '{executable}' -ArgumentList "
            f"{arguments or default_arguments} -Verb RunAs "
            f"-WindowStyle Hidden -WorkingDirectory '{workdir}'")


def run_setup_elevated() -> tuple[bool, str]:
    """Ask Windows to run the sensor setup elevated. One prompt, once.

    The app does this itself rather than telling the user to find a script:
    changing a scheduled task needs administrator rights, so *some* prompt is
    unavoidable, and the least confusing place for it is next to the reading it
    fixes. Nothing here involves LibreHardwareMonitor - the prompt is Windows
    Task Scheduler's.
    """
    if os.name != "nt":
        return False, "this is Windows only"
    begin_setup()
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", setup_command()],
            capture_output=True, text=True, errors="replace",
            **_no_window())
    except OSError as exc:
        return False, f"could not raise the prompt: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return False, (f"Windows refused to start the setup: "
                       f"{detail[-1] if detail else 'unknown reason'}")
    return True, "asked Windows for permission - accept the prompt"


def install_startup_task() -> tuple[bool, str]:
    """Start the helper at every logon."""
    ok, message = create_task(STARTUP_TASK, ["/sc", "onlogon"])
    return (ok, f"{message} to read the CPU at every logon") if ok else (ok, message)


def install_helper_task() -> tuple[bool, str]:
    """Register the on-demand trigger, which gpumon starts without a prompt."""
    ok, message = create_task(HELPER_TASK, ["/sc", "once", "/st", "00:01"])
    return (ok, f"{message}, to be triggered on demand") if ok else (ok, message)


def remove_startup_task() -> tuple[bool, str]:
    results = [delete_task(name) for name in (STARTUP_TASK, HELPER_TASK)]
    if any(ok for ok, _ in results):
        return True, "removed gpumon's sensor tasks"
    return False, results[0][1] or "no task to remove"


# --------------------------------------------------------------------------
# Starting the helper, and checking it worked
# --------------------------------------------------------------------------
def reading_age() -> float:
    """Seconds since the helper last published, or infinity."""
    import msrbackend
    return msrbackend.MsrBackend().reading_age()


def start_helper_now(wait: float = 30.0) -> tuple[bool, str]:
    """Trigger the helper task and wait for it to publish a reading.

    No prompt: the task was created once with highest privileges, and starting it
    does not ask again. This is the whole trick that keeps a privileged operation
    away from the program the user actually runs.
    """
    import msrbackend
    if reading_age() <= msrbackend.MAX_AGE:
        return True, "the sensor helper is already publishing"
    if not task_exists(HELPER_TASK):
        return False, ("the sensor helper is not set up yet - it takes one "
                       "Windows prompt")
    if not task_is_current():
        # Left over from an earlier version: it runs a file that is not there any
        # more, so triggering it would start nothing and this would sit waiting.
        return False, ("the sensor task needs recreating (it still points at an "
                       "older version's script) - one Windows prompt")
    code, message = _schtasks(["/run", "/tn", HELPER_TASK])
    if code != 0:
        return False, f"could not start the sensor helper: {message}"
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(1.0)
        if reading_age() <= msrbackend.MAX_AGE:
            import sensorhelper
            payload = sensorhelper.read_sensors()
            temperature = payload.get("cpu_temp")
            return True, (f"CPU temperature is live"
                          + (f" ({temperature:g} C)" if temperature else ""))
    return False, "the helper started but published nothing"


# Compatibility: the app used to ask for the LibreHardwareMonitor server.
start_server_now = start_helper_now


def describe_reading() -> str:
    """A short phrase about the newest reading, for the status line."""
    import sensorhelper
    payload = sensorhelper.read_sensors()
    temperature = payload.get("cpu_temp")
    if isinstance(temperature, (int, float)):
        return f"{temperature:g} C"
    return ""


def summarise() -> None:
    """Print what the sensors are currently reporting."""
    import sensorhelper
    payload = sensorhelper.read_sensors()
    if not payload:
        print("  nothing published yet")
        return
    age = reading_age()
    keys = ("cpu_temp", "cpu_core_max", "cpu_tjmax", "cpu_clock")
    shown = ", ".join(f"{key}={payload[key]:g}" for key in keys
                      if isinstance(payload.get(key), (int, float)))
    cores = sorted((k for k in payload if k.startswith("core")),
                   key=lambda k: int(k[4:].split("_")[0]))
    print(f"  {shown}")
    if cores:
        print(f"  {len(cores)} cores: "
              + ", ".join(f"{payload[c]:g}" for c in cores))
    print(f"  published {age:.1f}s ago")


# --------------------------------------------------------------------------
# The flow
# --------------------------------------------------------------------------
def setup(argv: list[str] | None = None) -> int:
    """Entry point for `gpumon.py --setup-sensors`."""
    argv = argv or []
    no_tasks = "--no-startup-task" in argv
    force = "--download" in argv

    print("=" * 78)
    print("gpumon sensor setup")
    print("=" * 78)
    print("""
This gives gpumon the one reading Windows will not hand to an ordinary program:
the processor's own temperature. It is a privileged register, so it takes a
kernel driver, and this sets that up - once.

  * PawnIO (signed, open source, by namazso) is downloaded from its own release
    and installed with one Windows prompt, if it is not already present.
  * Its signed sensor modules are fetched the same way, if the bundle does not
    already carry them.
  * A scheduled task is registered so gpumon's own reader starts with the rights
    it needs without asking again, at logon and on demand.

No other monitoring program is involved: gpumon reads the registers itself.
""")

    steps = _Step()
    steps.head("Checking what is already in place")
    import pawnio
    have_driver = pawnio_installed()
    print(f"  PawnIO driver : "
          + (f"installed (version {pawnio_version()})" if have_driver
             else "not installed"))
    missing = missing_modules()
    print(f"  sensor modules: "
          + ("all present" if not missing else f"missing {', '.join(missing)}"))

    if not have_driver or missing or force:
        steps.head("Downloading what is missing")
        if not have_driver or force:
            installer = download_pawnio_installer()
            if installer:
                print("  running the PawnIO installer (silent)")
                if install_pawnio(installer):
                    steps.ok(f"PawnIO {pawnio_version() or ''} installed".strip())
                    have_driver = True
                else:
                    steps.warn("the installer did not report success - run "
                               f"{installer} by hand if the next step fails")
        if missing or force:
            ok, message = download_modules()
            if ok:
                steps.ok(message)
            else:
                steps.warn(message)
    else:
        steps.ok("nothing to download")

    steps.head("Registering the helper task")
    if no_tasks:
        print("  skipped (--no-startup-task)")
    else:
        ok, message = install_startup_task()
        steps.ok(message) if ok else steps.warn(message)
        ok, message = install_helper_task()
        steps.ok(message) if ok else steps.warn(message)

    steps.head("Reading the CPU through the driver")
    import cpusensors
    identity = cpusensors.identify()
    print(f"  {identity.name or 'unknown processor'}")
    print(f"  {identity.describe()}")
    if identity.vendor == "amd":
        steps.warn("AMD processors are supported through the SMU's register "
                   "space, but this code has not been run on an AMD machine "
                   "yet - if the reading looks wrong, that is where to look")
    elif not identity.supported_by_us:
        steps.warn("this processor model has no decoding path yet")

    ok = False
    if task_exists(HELPER_TASK):
        ok, message = start_helper_now(wait=30.0)
        steps.ok(message) if ok else steps.warn(message)
    else:
        with pawnio.PawnIO() as driver:
            if driver.open():
                readings = cpusensors.read_cpu(driver)
                if readings.error:
                    steps.warn(readings.error)
                else:
                    steps.ok(f"CPU package {readings.package} C, "
                             f"hottest core {readings.hottest()} C")
                    ok = True
            else:
                steps.warn(driver.error)

    if ok:
        steps.head("Sensors now available")
        summarise()

    print("\n" + "=" * 78)
    if ok:
        print("Setup complete. Restart gpumon and the CPU temperature is live.")
    else:
        print("Setup did not finish. The steps above say which one.")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(setup(sys.argv[1:]))
