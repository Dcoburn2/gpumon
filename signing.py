"""Sign the release artifacts, when there is something to sign them with.

The honest position first: **an unsigned executable cannot be made trusted.** A
signature says who published a file, and Windows decides how much to trust that
publisher from a certificate chaining to a CA in its Trusted Root Program.
Nothing in this program can substitute for that, and a self-signed certificate is
worse than none for public distribution - Windows blocks it outright for anyone
who has not installed the certificate by hand.

What this module does is make signing a matter of configuration rather than a
rewrite: point it at a signing tool and an identity and every artifact is signed
and timestamped; leave it unconfigured and the build says plainly that it is
producing unsigned binaries. That is the state a laptop build should be in, and
it is the state continuous integration moves out of.

Three ways to sign, in the order the environment variables are checked:

  * a certificate in the Windows store, by thumbprint  (GPUMON_SIGN_CERT)
  * a .pfx file with its password                      (GPUMON_SIGN_PFX)
  * anything else, through extra arguments             (GPUMON_SIGN_ARGS)

The third exists for services that sign through their own library - Microsoft's
Azure Artifact Signing, for instance, which passes `dlib` and a metadata file
rather than a certificate path - so this file does not have to know about each
one.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess

#: Where the signing tool is. The Windows SDK installs it under a versioned
#: directory, so a bare `signtool` is often not on PATH.
SIGNTOOL_ENV = "GPUMON_SIGNTOOL"
CERT_ENV = "GPUMON_SIGN_CERT"          # thumbprint of a certificate in the store
PFX_ENV = "GPUMON_SIGN_PFX"            # path to a .pfx or .p12
PFX_PASSWORD_ENV = "GPUMON_SIGN_PFX_PASSWORD"
ARGS_ENV = "GPUMON_SIGN_ARGS"          # extra arguments, for a signing service
TIMESTAMP_ENV = "GPUMON_SIGN_TIMESTAMP"

#: Default timestamp server. Without one, every signature stops being valid the
#: day the certificate expires - including for files downloaded years earlier.
DEFAULT_TIMESTAMP = "http://timestamp.digicert.com"


def find_signtool() -> str:
    """Locate signtool.exe, from the environment or the SDK's usual places."""
    from_env = os.environ.get(SIGNTOOL_ENV)
    if from_env and os.path.exists(from_env):
        return from_env
    on_path = shutil.which("signtool")
    if on_path:
        return on_path
    candidates = []
    for root in (os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("ProgramFiles", r"C:\Program Files")):
        kits = os.path.join(root, "Windows Kits", "10", "bin")
        if not os.path.isdir(kits):
            continue
        for version in sorted(os.listdir(kits), reverse=True):
            for arch in ("x64", "x86"):
                candidates.append(os.path.join(kits, version, arch, "signtool.exe"))
    return next((path for path in candidates if os.path.exists(path)), "")


def identity() -> tuple[str, str]:
    """The signing identity from the environment, as (kind, value) or ("", "")."""
    for kind, name in (("cert", CERT_ENV), ("pfx", PFX_ENV), ("args", ARGS_ENV)):
        value = os.environ.get(name)
        if value:
            return kind, value
    return "", ""


def sign_command(files: list[str]) -> tuple[list[str] | None, str]:
    """The signtool command line for these files, or why there is not one."""
    tool = find_signtool()
    if not tool:
        return None, (f"no signtool found - set {SIGNTOOL_ENV} to its path, or "
                      f"install the Windows SDK")
    kind, value = identity()
    if not kind:
        return None, (f"no signing identity configured - set {CERT_ENV} to a "
                      f"certificate thumbprint, {PFX_ENV} to a .pfx, or "
                      f"{ARGS_ENV} for a signing service")
    timestamp = os.environ.get(TIMESTAMP_ENV, DEFAULT_TIMESTAMP)
    command = [tool, "sign", "/fd", "sha256", "/td", "sha256",
               "/tr", timestamp]
    if kind == "cert":
        command += ["/sha1", value]
    elif kind == "pfx":
        command += ["/f", value]
        password = os.environ.get(PFX_PASSWORD_ENV)
        if password:
            command += ["/p", password]
    else:
        # A signing service supplies its own selection arguments, which can
        # contain spaces, so they are split rather than passed as one value.
        command += value.split()
    return command + list(files), ""


def sign(files: list[str]) -> tuple[bool, str]:
    """Sign every file, or explain why nothing was signed.

    Returns (signed, message). A build that cannot sign is not a failed build:
    the artifacts are still usable, they simply carry no publisher.
    """
    present = [path for path in files if os.path.exists(path)]
    if not present:
        return False, "there is nothing to sign"
    command, why = sign_command(present)
    if command is None:
        return False, why
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                errors="replace",
                                creationflags=getattr(subprocess,
                                                      "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        return False, f"could not run the signing tool: {exc}"
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip().splitlines()
        return False, (f"signing failed: "
                       f"{detail[-1] if detail else 'no output'}")
    return True, f"signed {len(present)} file(s)"


def verify(path: str) -> tuple[bool, str]:
    """Ask signtool what it makes of a file's signature."""
    tool = find_signtool()
    if not tool:
        return False, "no signtool available to check with"
    result = subprocess.run([tool, "verify", "/pa", "/v", path],
                            capture_output=True, text=True, errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    output = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        return True, "signed, and the signature verifies"
    if "No signature found" in output:
        return False, "no signature"
    return False, output.splitlines()[-1] if output else "could not verify"


def checksums(paths: list[str]) -> str:
    """A SHA-256 line per file, in the format `sha256sum -c` understands.

    Worth shipping whether or not the build is signed: it is how somebody checks
    that a download arrived intact, and how a package manager pins a version.
    """
    lines = []
    for path in sorted(paths):
        if not os.path.exists(path):
            continue
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        lines.append(f"{digest.hexdigest()}  {os.path.basename(path)}")
    return "\n".join(lines) + ("\n" if lines else "")
