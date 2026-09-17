"""The Store build cannot install a driver: is that a fact or a promise?

Partner Center asks whether the product depends on non-Microsoft drivers or NT
services, and Store policy 10.2.4.2 asks for disclosure if it does. The answer for
the Store package is no, and this test holds that claim to three things:

  1. nothing in the shipped program creates an NT service;
  2. the packaged build refuses the sensor setup, so it cannot install a driver
     even if its run-time package check were unavailable;
  3. the executable in the package layout actually refuses, verified by running
     it rather than by reading the source.
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

_windows_only_test()





import os
import re
import subprocess
import sys

import storemode

problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


def shipped() -> list[str]:
    files = []
    for root, dirs, names in os.walk("."):
        dirs[:] = [d for d in dirs if d not in (
            ".git", "__pycache__", "dist", "build", "release", "release-onefile",
            "tools", "store", "pawnio-modules")]
        for name in names:
            if name.endswith(".py") and not name.startswith(("test_", "_")):
                files.append(os.path.normpath(os.path.join(root, name)))
    return files


print("=" * 84)
print("STORE DRIVER AND SERVICE TEST")
print("=" * 84)

print("\n[1] no NT service is created or installed")
creators = ("win32service", "CreateService", "OpenSCManager", "New-Service",
            "sc create", "sc.exe create", "InstallUtil", "ChangeServiceConfig")
for path in shipped():
    body = open(path, encoding="utf-8").read()
    for token in creators:
        if token in body:
            check(f"{path} does not create a service", False, token)
print(f"    {len(shipped())} shipping module(s) checked, no service creation")
check("what it creates instead is a scheduled task",
      "schtasks" in open("sensorsetup.py", encoding="utf-8").read(),
      "a task is not a service, which is what the policy asks about")

print("\n[2] the driver is opened by the helper, never by the interface")
interface = open("metrics.py", encoding="utf-8").read()
check("the sensor layer does not construct a driver client",
      "pawnio.PawnIO()" not in interface)
check("it reads a published file instead",
      "MsrBackend" in interface or "msrbackend" in interface)
openers = {}
for path in shipped():
    body = open(path, encoding="utf-8").read()
    for match in re.finditer(r"PawnIO\(\)", body):
        start = body.rfind("def ", 0, match.start())
        name = body[start:body.find("(", start)].replace("def ", "").strip()
        openers.setdefault(path, set()).add(name)
print(f"    driver clients: { {k: sorted(v) for k, v in openers.items()} }")
check("the interface is not among the openers",
      "metrics.py" not in openers and "ui/monitor.py" not in openers)
check("the helper is", any("spin" in names for names in openers.values()),
      "the privileged side is a separate process")

print("\n[3] a Store build refuses the sensor setup")
source = open("storemode.py", encoding="utf-8").read()
check("the flag exists and defaults to off for a source checkout",
      "IS_STORE_BUILD = False" in source)
check("the command line consults it",
      "storemode.IS_STORE_BUILD" in open("gpumon.py", encoding="utf-8").read())

original = source
try:
    with open("storemode.py", "w", encoding="utf-8") as handle:
        handle.write(source.replace("IS_STORE_BUILD = False",
                                    "IS_STORE_BUILD = True"))
    result = subprocess.run([sys.executable, "gpumon.py", "--setup-sensors"],
                            capture_output=True, text=True, errors="replace")
    print(f"    exit code {result.returncode}: "
          f"{(result.stdout or result.stderr).strip().splitlines()[0][:88]}")
    check("it refuses instead of installing", result.returncode != 0)
    check("and says where the feature does live",
          "portable" in result.stdout.lower())
finally:
    with open("storemode.py", "w", encoding="utf-8") as handle:
        handle.write(original)
check("the flag is restored afterwards, so the portable build still works",
      "IS_STORE_BUILD = False" in open("storemode.py", encoding="utf-8").read())

print("\n[4] the executable in the package layout behaves the same way")
exe = os.path.join("store", "layout", "gpumon.exe")
marker = os.path.join("store", "layout", "STORE-BUILD.txt")
if not os.path.exists(exe) or not os.path.exists(marker):
    print("    (no layout built - run: python make_store_package.py)")
else:
    state = open(marker, encoding="utf-8").read()
    print(f"    the package says about itself: {state.strip().replace(chr(10), '; ')}")
    disabled = "sensor setup disabled: True" in state
    check("the Store package was built without the sensor setup path", disabled,
          "run: python make_store_package.py (the disable is now the default)")
    if disabled:
        # Safe to run now, and that is the point of the marker: asking a build
        # *with* the setup path to set the sensors up would install a driver on
        # whatever machine runs the tests. It did, once, while this was written.
        result = subprocess.run([exe, "--setup-sensors"], capture_output=True,
                                text=True, errors="replace", timeout=180)
        text = ((result.stdout or "") + (result.stderr or "")).strip()
        print(f"    exit code {result.returncode}: "
              f"{text.splitlines()[0][:80] if text else '(no output)'}")
        check("the packaged build refuses when asked directly",
              result.returncode != 0 and "cannot install" in text)
        check("it does not download anything on the way",
              "github.com" not in text and "ownloading" not in text)
    else:
        print("    not running it: a build with the setup path would try to")
        print("    install a driver on this machine.")

print("\n[5] the submission guide answers the question with policy wording")
guide = open(os.path.join(_DEV, "store", "SUBMISSION.md"), encoding="utf-8").read()
check("it quotes the policy requirement", "10.2.4.2" in guide)
check("it answers about NT services", "NT service" in guide)
check("it answers about drivers", "PawnIO" in guide and "NVML" in guide)
check("and gives certification notes to paste",
      "certification notes" in guide.lower())
check("it says the setup path is disabled in the package by default",
      "disables the" in guide and "by default" in guide)
check("and names the escape hatch for a local build",
      "--allow-sensor-setup" in guide)

print("\n" + "=" * 84)
if problems:
    print(f"STORE DRIVER AND SERVICE TEST FAILED ({len(problems)})")
    for item in problems:
        print(f"  - {item}")
else:
    print("STORE DRIVER AND SERVICE TEST PASSED")
print("=" * 84)
raise SystemExit(1 if problems else 0)
