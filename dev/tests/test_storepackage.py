"""Does the packaged build write anything beside itself?

An MSIX package installs to `C:\\Program Files\\WindowsApps\\`, which is read-only
at run time. Anything the program writes there fails silently in a package and
works perfectly on a development machine, which is the worst combination.

The layout built by `make_store_package.py` has no portable `config.json` in it -
that is what tells `apppaths` to use the user's profile. This copies that layout
to a scratch folder, runs the program from it, and checks that nothing appeared
beside the executable.
"""
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





import os
import shutil
import subprocess
import tempfile
import time

import apppaths

problems = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


print("=" * 84)
print("PACKAGED BUILD TEST")
print("=" * 84)

print("\n[1] how state resolves, portable versus installed")
print(f"    app dir   : {apppaths.app_dir()}")
print(f"    user dir  : {apppaths.user_dir()}")
portable = os.path.exists(apppaths.app_path(apppaths.PORTABLE_MARKER))
print(f"    a config.json sits beside this copy: {portable}")
print(f"    so state goes to: {apppaths.state_dir()}")
if portable:
    check("a portable copy keeps its state beside itself",
          apppaths.state_dir() == apppaths.app_dir())
else:
    check("an installed copy writes to the user's profile",
          apppaths.state_dir() == apppaths.user_dir())
check("the config path follows the same rule",
      apppaths.state_path("config.json") ==
      os.path.join(apppaths.state_dir(), "config.json"))
check("logs follow the same rule",
      apppaths.state_path("gpumon-error.log") ==
      os.path.join(apppaths.state_dir(), "gpumon-error.log"))

print("\n[2] the package layout carries no state to begin with")
layout = os.path.join(_DEV, "store", "layout")
if not os.path.isdir(layout):
    print("    (no layout built - run: python make_store_package.py --layout-only)")
else:
    contents = sorted(os.listdir(layout))
    print(f"    {contents}")
    for unwanted in ("config.json", "msr-sensors.json", "msr-heartbeat",
                     "gpumon-error.log", "sessions.db", "pawnio-modules"):
        check(f"no {unwanted} in the package", unwanted not in contents)
    manifest = open(os.path.join(layout, "AppxManifest.xml"),
                    encoding="utf-8").read()
    check("the manifest asks for full trust, not elevation",
          "runFullTrust" in manifest and "allowElevation" not in manifest)
    check("no third-party binaries are packaged",
          "pawnio-modules" not in contents,
          "PawnIO is GPL-2.0 and fetched by the portable build's setup")

print("\n[3] running it from a read-only-style folder writes nothing there")
scratch = tempfile.mkdtemp(prefix="gpumon-msix-")
if os.path.isdir(layout):
    for name in os.listdir(layout):
        source = os.path.join(layout, name)
        target = os.path.join(scratch, name)
        if os.path.isdir(source):
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    before = set(os.listdir(scratch))
    print(f"    copied the layout to {scratch}")
    process = subprocess.Popen([os.path.join(scratch, "gpumon.exe")])
    time.sleep(14)
    after = set(os.listdir(scratch))
    added = sorted(after - before)
    print(f"    files that appeared beside the exe: {added or 'none'}")
    check("nothing was written beside the executable", not added, str(added))
    state = apppaths.user_dir()
    published = [name for name in os.listdir(state)
                 if name.endswith((".log", ".json"))]
    print(f"    files in the user directory   : {published or 'none'}")
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
    time.sleep(1.0)
    shutil.rmtree(scratch, ignore_errors=True)

print("\n[4] what the packaged build says about the sensors")
print(f"    is_packaged() on this machine: {apppaths.is_packaged()}")
check("it knows whether it is packaged", isinstance(apppaths.is_packaged(), bool))
message = apppaths.packaged_sensor_message()
print(f"    its message: {message}")
check("the message says it cannot install the helper",
      "cannot install" in message, message)
check("and says where the helper does come from",
      "start-sensors.cmd" in message and "portable" in message, message)
source = open("ui/monitor.py", encoding="utf-8").read()
check("the button does not offer a prompt Windows would refuse",
      "is_packaged()" in source and "packaged_sensor_message" in source)

print("\n[5] the submission material is ready for Partner Center")
import make_store_package as pack  # noqa: E402

submission = os.path.join(_DEV, "store", "SUBMISSION.md")
check("there is a submission guide", os.path.exists(submission))
if os.path.exists(submission):
    guide = open(submission, encoding="utf-8").read()
    check("it says individual registration is free",
          "free" in guide and "storedeveloper.microsoft.com" in guide)
    check("it names the two facts that shape the submission",
          "cannot install" in guide and "read-only" in guide)
    check("it includes the certification notes",
          "Notes for certification" in guide or "certification notes" in guide)
    check("and a privacy policy draft, which the Store requires",
          "Privacy policy" in guide)
    check("it says the Store re-signs, so no certificate is needed",
          "re-signs" in guide or "signing first" in guide)

if os.path.isdir(layout):
    manifest = open(os.path.join(layout, "AppxManifest.xml"),
                    encoding="utf-8").read()
    check("the manifest uses the identity constants from the builder",
          f'Name="{pack.IDENTITY_NAME}"' in manifest
          and f'Publisher="{pack.IDENTITY_PUBLISHER}"' in manifest
          and f'Version="{pack.VERSION}"' in manifest,
          "Partner Center's values must match these exactly")

screenshots = os.path.join(_DEV, "store", "screenshots")
if os.path.isdir(screenshots):
    images = [name for name in os.listdir(screenshots) if name.endswith(".png")]
    print(f"    {len(images)} screenshot(s): {images}")
    check("at least one screenshot exists", bool(images))
    if images:
        import struct
        with open(os.path.join(screenshots, images[0]), "rb") as handle:
            header = handle.read(24)
        width, height = struct.unpack(">II", header[16:24])
        print(f"    {images[0]} is {width}x{height}")
        check("it meets the Store's minimum size",
              width >= 1366 and height >= 768, f"{width}x{height}")
else:
    print("    (no screenshots yet - run: python store_capture.py)")

print("\n[6] the packaged build and the zip come from one source tree")
# Only meaningful once something has been built; a fresh clone has no dist/.
if os.path.isdir(os.path.join(_DEV, "dist")):
    check("both are built by name from the same pyinstaller output",
          os.path.exists(os.path.join(_DEV, "dist", "gpumon", "gpumon.exe")))
else:
    print("    --  dist/ was not built, so the shared-output check is skipped")
check("the manifest is generated, not hand-edited",
      "MANIFEST = " in open(os.path.join(_DEV, "scripts", "make_store_package.py"), encoding="utf-8").read())

print("\n" + "=" * 84)
if problems:
    print(f"PACKAGED BUILD TEST FAILED ({len(problems)})")
    for item in problems:
        print(f"  - {item}")
else:
    print("PACKAGED BUILD TEST PASSED")
print("=" * 84)
raise SystemExit(1 if problems else 0)
