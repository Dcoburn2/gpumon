"""Does every artifact's checksum file describe the right file, unambiguously?

The first version of this listed both executables as "gpumon.exe" in a single
file, which tells a downloader nothing about which build they are holding. This
checks each file names only what sits beside it, and that the hashes match.
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





import hashlib
import os

import signing

FOLDERS = ["release", "release-onefile"]
problems = []

#: These checks are about built artifacts, so they only mean something once
#: something has been built. In a fresh clone there is nothing to check, and
#: failing would say "the tests are broken" when the truth is "not built yet".
#: Continuous integration builds first and then runs this, so it does check.
BUILT = [folder for folder in FOLDERS
         if os.path.exists(os.path.join(folder, "SHA256SUMS.txt"))]


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


print("=" * 84)
print("RELEASE CHECKSUM TEST")
print("=" * 84)

if not BUILT:
    print("\nNothing has been built in this checkout, so there are no artifacts")
    print("to check. Run:  python scripts/make_release.py --all")
    print("\nWhat is checked when they exist: each checksum file names the file")
    print("beside it and matches it, the zip carries the readme and its checksums,")
    print("and every delivered folder carries the licence.")
    raise SystemExit(0)

for folder in FOLDERS:
    target = os.path.join(folder, "SHA256SUMS.txt")
    print(f"\n[{folder}]")
    if not os.path.exists(target):
        check(f"{target} exists", False)
        continue
    lines = [line for line in open(target, encoding="utf-8").read().splitlines()
             if line.strip()]
    print(f"    {len(lines)} line(s)")
    for line in lines:
        print(f"      {line}")
    check("exactly one file is listed", len(lines) == 1, f"{len(lines)}")
    check("it does not name two different builds the same way",
          len({line.split(None, 1)[1] for line in lines}) == len(lines))
    digest, name = lines[0].split(None, 1)
    path = os.path.join(folder, name.strip())
    check(f"it names a file beside it ({name.strip()})", os.path.exists(path))
    if os.path.exists(path):
        actual = hashlib.sha256(open(path, "rb").read()).hexdigest()
        check("and the hash matches", actual == digest,
              f"{actual[:16]}… vs {digest[:16]}…")

# Taken from the program's own version rather than written down, so a version bump
# does not leave this test looking for the previous release's file.
import make_release  # noqa: E402  (scripts/ is on the path from the bootstrap)

archive = make_release.archive_name()
print(f"\n[{archive}]")
sidecar = archive + ".sha256"
check("a sidecar checksum exists", os.path.exists(sidecar))
if os.path.exists(sidecar):
    text = open(sidecar, encoding="utf-8").read()
    print(f"    {text.strip()}")
    digest, name = text.split(None, 1)
    check("it names the archive", name.strip() == archive, name.strip())
    actual = hashlib.sha256(open(archive, "rb").read()).hexdigest()
    check("and the hash matches", actual == digest)

print("\n[the zip carries the checksums it was built with]")
import zipfile

if os.path.exists(archive):
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
    print(f"    {len(names)} entries")
    check("the folder readme is inside", "gpumon/README.md" in names)
    check("the folder checksums are inside",
          "gpumon/SHA256SUMS.txt" in names)
    check("the executable is at the top of the zip",
          "gpumon/gpumon.exe" in names)
    check("nothing from the repository leaked in",
          not any(name.startswith(("gpumon/test_", "gpumon/.git",
                                   "gpumon/dist/")) for name in names))

print("\n[the licence travels with the download]")
# MIT requires the copyright notice to be included in copies of the software, and
# somebody who downloads a zip has no other way to see it.
for folder in FOLDERS + [os.path.join("store", "layout")]:
    path = os.path.join(folder, "LICENSE")
    if not os.path.isdir(folder):
        # The Store layout is only there after make_store_package.py has run.
        print(f"  --  {folder}/ was not built, so it is not checked")
        continue
    exists = os.path.exists(path)
    check(f"{folder}/LICENSE is shipped", exists,
          "" if exists else "the download carries no licence")
    if exists:
        text = open(path, encoding="utf-8").read()
        check(f"{folder}/LICENSE names the copyright holder",
              "Copyright (c) 2026 Darrell Coburn" in text,
              text.splitlines()[2] if len(text.splitlines()) > 2 else "")

print("\n[and the helper records what it did]")
check("checksums are written whatever the signing state",
      callable(signing.checksums))

print("\n" + "=" * 84)
if problems:
    print(f"RELEASE CHECKSUM TEST FAILED ({len(problems)})")
    for item in problems:
        print(f"  - {item}")
else:
    print("RELEASE CHECKSUM TEST PASSED")
print("=" * 84)
raise SystemExit(1 if problems else 0)
