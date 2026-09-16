"""Code signing: what is configured, what gets invoked, what is written.

No certificate is needed to test any of this. The tool that would be run is
replaced by a stub that records the arguments it was handed, which is the part
that has to be right: the difference between a signed release and an unsigned one
is entirely in those arguments.
"""

from __future__ import annotations
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
import stat
import tempfile

import signing

problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


def clear_environment() -> None:
    for name in ("GPUMON_SIGNTOOL", "GPUMON_SIGN_CERT", "GPUMON_SIGN_PFX",
                 "GPUMON_SIGN_PFX_PASSWORD", "GPUMON_SIGN_ARGS",
                 "GPUMON_SIGN_TIMESTAMP"):
        os.environ.pop(name, None)


def make_stub(directory: str) -> str:
    """A fake signtool: writes its arguments to a file and exits 0."""
    record = os.path.join(directory, "args.txt")
    stub = os.path.join(directory, "signtool-stub.cmd")
    with open(stub, "w", encoding="ascii", newline="\r\n") as handle:
        handle.write("@echo off\n")
        handle.write(f'echo %* >> "{record}"\n')
        handle.write("exit /b 0\n")
    return stub


print("=" * 84)
print("SIGNING TEST")
print("=" * 84)

work = tempfile.mkdtemp()
artifact = os.path.join(work, "gpumon.exe")
with open(artifact, "wb") as handle:
    handle.write(b"pretend this is a program")
stub = make_stub(work)
record = os.path.join(work, "args.txt")
clear_environment()

print("\n[1] with nothing configured, nothing is signed and the reason is clear")
signed, message = signing.sign([artifact])
print(f"    sign() -> {signed}: {message}")
check("signing reports failure rather than pretending", not signed)
check("the message names the variables to set",
      "GPUMON_SIGN_CERT" in message or "GPUMON_SIGN_PFX" in message, message)
check("and no file was touched",
      open(artifact, "rb").read() == b"pretend this is a program")
check("an empty signing identity is not an error",
      signing.identity() == ("", ""))

print("\n[2] a thumbprint produces the right signtool command")
os.environ["GPUMON_SIGNTOOL"] = stub
os.environ["GPUMON_SIGN_CERT"] = "ABCDEF0123456789"
command, why = signing.sign_command([artifact])
print(f"    {command}")
check("signtool is invoked", bool(command), why)
check("with the certificate's thumbprint", "/sha1" in command
      and "ABCDEF0123456789" in command)
check("and SHA-256 for both the file and the timestamp",
      command[command.index("/fd") + 1] == "sha256"
      and command[command.index("/td") + 1] == "sha256")
check("it is timestamped, so the signature outlives the certificate",
      "/tr" in command and command[command.index("/tr") + 1].startswith("http"))
check("the file is last, where signtool expects it", command[-1] == artifact)

print("\n[3] a .pfx adds the file and its password")
os.environ.pop("GPUMON_SIGN_CERT", None)
os.environ["GPUMON_SIGN_PFX"] = os.path.join(work, "cert.pfx")
os.environ["GPUMON_SIGN_PFX_PASSWORD"] = "hunter2"
command, why = signing.sign_command([artifact])
check("the pfx path is passed", "/f" in command
      and command[command.index("/f") + 1].endswith("cert.pfx"), why)
check("the password is passed", "/p" in command
      and command[command.index("/p") + 1] == "hunter2")

print("\n[4] a signing service supplies its own selection arguments")
clear_environment()
os.environ["GPUMON_SIGNTOOL"] = stub
os.environ["GPUMON_SIGN_ARGS"] = ("/dlib Azure.CodeSigning.Dlib.dll "
                                  "/dmdf metadata.json")
command, why = signing.sign_command([artifact])
print(f"    {command}")
check("the service's arguments arrive split, not as one quoted blob",
      "/dlib" in command and "Azure.CodeSigning.Dlib.dll" in command
      and "/dmdf" in command and "metadata.json" in command, why)

print("\n[5] the command actually runs, and its failure is reported")
os.environ["GPUMON_SIGN_ARGS"] = "/dlib thing.dll"
if os.path.exists(record):
    os.remove(record)
signed, message = signing.sign([artifact])
print(f"    sign() -> {signed}: {message}")
check("the stub was invoked", os.path.exists(record))
if os.path.exists(record):
    print(f"    it was called with: {open(record).read().strip()}")
    check("with the arguments we built",
          "/dlib" in open(record).read() and artifact in open(record).read())

failing = os.path.join(work, "signtool-fail.cmd")
with open(failing, "w", encoding="ascii", newline="\r\n") as handle:
    handle.write("@echo off\necho signing refused 1>&2\nexit /b 1\n")
os.environ["GPUMON_SIGNTOOL"] = failing
signed, message = signing.sign([artifact])
print(f"    a failing tool -> {signed}: {message}")
check("a refusal is reported, not swallowed", not signed)
check("the tool's own words are passed on", "refused" in message, message)

print("\n[6] checksums are written whether or not anything was signed")
clear_environment()
text = signing.checksums([artifact])
expected = hashlib.sha256(open(artifact, "rb").read()).hexdigest()
print(f"    {text.strip()}")
check("the format is the one sha256sum -c reads",
      text.split()[0] == expected and text.split()[1] == "gpumon.exe", text)
check("a missing file is skipped rather than crashing",
      signing.checksums([os.path.join(work, "nope.exe")]) == "")
check("no files means no checksum lines", signing.checksums([]) == "")

print("\n[7] verification follows the tool's own answer")
# The permissive stub exits 0 for everything, so the meaningful direction is the
# other one: a tool that refuses must not be reported as a verified signature.
os.environ["GPUMON_SIGNTOOL"] = stub
ok_with_stub, message = signing.verify(artifact)
print(f"    a permissive tool -> {ok_with_stub}: {message}")
check("a tool that says yes is believed", ok_with_stub, message)
os.environ["GPUMON_SIGNTOOL"] = failing
ok_with_failure, message = signing.verify(artifact)
print(f"    a refusing tool  -> {ok_with_failure}: {message}")
check("a tool that says no is believed too", not ok_with_failure, message)
check("and the failure is described", bool(message))
clear_environment()
ok_unconfigured, message = signing.verify(artifact)
print(f"    the real tool, on an unsigned file -> {ok_unconfigured}: {message}")
check("an unsigned file is never reported as signed", not ok_unconfigured)
check("and the reason comes from the tool itself", bool(message))

for name in os.listdir(work):
    os.remove(os.path.join(work, name))
os.rmdir(work)

print("\n" + "=" * 84)
if problems:
    print(f"SIGNING TEST FAILED ({len(problems)})")
    for item in problems:
        print(f"  - {item}")
else:
    print("SIGNING TEST PASSED")
print("=" * 84)
raise SystemExit(1 if problems else 0)
