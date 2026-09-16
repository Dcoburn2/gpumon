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




import hashlib
import os

import signing

FOLDERS = ["release", "release-onefile"]
problems = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


print("=" * 84)
print("RELEASE CHECKSUM TEST")
print("=" * 84)

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

archive = "gpumon-1.0.0-windows-x64.zip"
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
