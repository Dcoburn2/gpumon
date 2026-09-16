"""Build the Microsoft Store package (MSIX) for gpumon.

Two things make this different from the zip release, and both come from how MSIX
works rather than from choice:

  * **The install folder is read-only at run time.** An MSIX package lives in
    `C:\\Program Files\\WindowsApps\\`, so nothing may be written beside the
    executable. `apppaths.state_path()` already resolves to the user's profile for
    an installed copy, which is what makes this packaging possible at all.
  * **A packaged app cannot install a kernel driver.** Elevation needs the
    `allowElevation` restricted capability, which Microsoft describes as unlikely
    to pass certification, and their own recommendation is to keep the UI in user
    mode and put admin work in a separate component - which is how gpumon is
    already built. So the Store package ships without the CPU-temperature setup,
    and reads the sensors if the machine already has them. The
    `--setup-sensors` path stays in the portable build.

The Store re-signs the package after certification, so nothing here needs a
certificate to *submit*. A self-signed one is only for installing locally to test.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "store")
LAYOUT = os.path.join(STORE, "layout")
BUILD = os.path.join(HERE, "dist", "gpumon")

#: Identity values. `Name` and `Publisher` have to match what Partner Center
#: assigns to the app; these are the placeholders a local build uses, and the
#: comment says where to change them.
IDENTITY_NAME = "gpumon.gpumon"
IDENTITY_PUBLISHER = "CN=gpumon"
VERSION = "1.0.0.0"

#: Tile sizes Microsoft requires, and the background the tiles are drawn on -
#: the default theme's panel colour, so the Store listing looks like the program.
REQUIRED_ASSETS = {
    "StoreLogo.png": (50, 50),
    "Square44x44Logo.png": (44, 44),
    "Square150x150Logo.png": (150, 150),
    "Wide310x150Logo.png": (310, 150),
}
TILE_BG = (0x17, 0x10, 0x2A)

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<Package
  xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
  xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10"
  xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
  IgnorableNamespaces="uap rescap">

  <Identity Name="{name}"
            Publisher="{publisher}"
            Version="{version}"
            ProcessorArchitecture="x64" />

  <Properties>
    <DisplayName>gpumon</DisplayName>
    <PublisherDisplayName>gpumon</PublisherDisplayName>
    <Description>Live GPU and system telemetry: utilisation, temperature, VRAM, clocks, power and fan for every graphics card, with CPU load, memory and per-core activity, graphs that scroll back through history, session logging and alarms.</Description>
    <Logo>Assets\\StoreLogo.png</Logo>
  </Properties>

  <Resources>
    <Resource Language="en-us" />
  </Resources>

  <Dependencies>
    <TargetDeviceFamily Name="Windows.Desktop" MinVersion="10.0.17763.0"
                        MaxVersionTested="10.0.26100.0" />
  </Dependencies>

  <Applications>
    <Application Id="gpumon" Executable="gpumon.exe"
                 EntryPoint="Windows.FullTrustApplication">
      <uap:VisualElements DisplayName="gpumon"
                          Description="Live GPU and system telemetry"
                          BackgroundColor="transparent"
                          Square150x150Logo="Assets\\Square150x150Logo.png"
                          Square44x44Logo="Assets\\Square44x44Logo.png">
        <uap:DefaultTile Wide310x150Logo="Assets\\Wide310x150Logo.png"
                         ShortName="gpumon" />
      </uap:VisualElements>
    </Application>
  </Applications>

  <Capabilities>
    <!-- A desktop app packaged for the Store needs full trust: it reads hardware
         sensors through vendor drivers and draws its own window. This is the
         ordinary capability for a Win32 app in a package, not an elevated one. -->
    <rescap:Capability Name="runFullTrust" />
  </Capabilities>
</Package>
"""


def find_tool(name: str) -> str:
    """Locate a Windows SDK tool, which is never on PATH by default."""
    root = os.path.join(os.environ.get("ProgramFiles(x86)",
                                       r"C:\Program Files (x86)"),
                        "Windows Kits", "10", "bin")
    if not os.path.isdir(root):
        return ""
    for version in sorted(os.listdir(root), reverse=True):
        candidate = os.path.join(root, version, "x64", name)
        if os.path.exists(candidate):
            return candidate
    return ""


def write_tiles() -> None:
    """Draw the Store tiles from the icon, so the listing matches the program."""
    import make_icon

    assets = os.path.join(LAYOUT, "Assets")
    os.makedirs(assets, exist_ok=True)
    for name, (width, height) in REQUIRED_ASSETS.items():
        rows = [[TILE_BG + (255,)] * width for _ in range(height)]
        # The mascot fills the square for the square tiles and is centred in the
        # wide one, which is a banner rather than a badge.
        side = min(width, height)
        sprite = make_icon.draw(side)
        sprite_rows = sprite.to_rgba_rows()
        left = (width - side) // 2
        top = (height - side) // 2
        for y in range(side):
            row = sprite_rows[y]
            for x in range(side):
                offset = x * 4
                pixel = tuple(row[offset:offset + 4])
                if len(pixel) == 4 and pixel[3] > 8:
                    rows[top + y][left + x] = pixel
        data = [bytes(b for px in row for b in px) for row in rows]
        with open(os.path.join(assets, name), "wb") as handle:
            handle.write(make_icon.encode_png(data, width, height))
        print(f"  Assets/{name}  {width}x{height}")


def build_layout() -> bool:
    """Copy the built program into the folder that becomes the package."""
    if not os.path.exists(os.path.join(BUILD, "gpumon.exe")):
        print(f"nothing to package: build it first "
              f"(python make_release.py --build)")
        return False
    if os.path.isdir(LAYOUT):
        shutil.rmtree(LAYOUT)
    os.makedirs(LAYOUT)

    shutil.copy2(os.path.join(BUILD, "gpumon.exe"),
                 os.path.join(LAYOUT, "gpumon.exe"))
    shutil.copytree(os.path.join(BUILD, "_internal"),
                    os.path.join(LAYOUT, "_internal"))
    for name in ("gpumon.ico", "gpumon.png"):
        source = os.path.join(BUILD, "_internal", name)
        if os.path.exists(source):
            shutil.copy2(source, os.path.join(LAYOUT, name))

    # The package carries no third-party binaries, and no run-time state: the
    # install folder is read-only, so a config.json in here would be a bug.
    for unwanted in ("config.json", "msr-sensors.json", "msr-heartbeat",
                     "pawnio-modules"):
        path = os.path.join(LAYOUT, unwanted)
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)

    with open(os.path.join(LAYOUT, "AppxManifest.xml"), "w",
              encoding="utf-8") as handle:
        handle.write(MANIFEST.format(name=IDENTITY_NAME, version=VERSION,
                                     publisher=IDENTITY_PUBLISHER))
    print("  AppxManifest.xml")
    write_tiles()

    # The Store build must not offer a setup it cannot perform.
    readme = os.path.join(LAYOUT, "README-STORE.txt")
    with open(readme, "w", encoding="utf-8", newline="\r\n") as handle:
        handle.write(
            "gpumon, packaged for the Microsoft Store.\n"
            "\n"
            "The program keeps its settings and logs in your user profile, "
            "because an installed package's own folder is read-only.\n"
            "\n"
            "CPU temperature needs a kernel driver, which a packaged app cannot "
            "install. If this machine already has the sensor helper set up "
            "(installed by the portable build), the temperature appears here as "
            "well; if not, everything else works and the CPU temperature shows "
            "as unavailable.\n")
    print("  README-STORE.txt")
    return True


def pack(sign: bool = False) -> int:
    makeappx = find_tool("makeappx.exe")
    if not makeappx:
        print("makeappx.exe was not found - install the Windows SDK")
        return 1
    target = os.path.join(STORE, f"gpumon-{VERSION}-x64.msix")
    if os.path.exists(target):
        os.remove(target)
    result = subprocess.run([makeappx, "pack", "/d", LAYOUT, "/p", target,
                             "/o"], capture_output=True, text=True,
                            errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print((result.stdout or result.stderr).strip()[-400:])
    if result.returncode != 0 or not os.path.exists(target):
        return 1
    print(f"\npackaged: {os.path.relpath(target, HERE)} "
          f"({os.path.getsize(target) / 1024 / 1024:.1f} MB)")

    if sign:
        return sign_local(target)
    print("  unsigned. The Store re-signs it after certification; for a local")
    print("  test install, run this again with --sign.")
    return 0


def sign_local(target: str) -> int:
    """Sign with a self-signed certificate, so the package can be installed here.

    Only for testing: Windows will not install a package whose publisher does not
    match a trusted certificate, and the Store replaces this signature with its
    own. The certificate has to match the manifest's Publisher exactly.
    """
    print("\ncreating a self-signed test certificate...")
    pfx = os.path.join(STORE, "gpumon-test.pfx")
    if os.path.exists(pfx):
        os.remove(pfx)
    script = (
        f"$cert = New-SelfSignedCertificate -Type Custom -Subject "
        f"'{IDENTITY_PUBLISHER}' -KeyUsage DigitalSignature "
        f"-FriendlyName 'gpumon test' -CertStoreLocation 'Cert:\\CurrentUser\\My' "
        f"-TextExtension @('2.5.29.37={{text}}1.3.6.1.5.5.7.3.3'); "
        f"$pw = ConvertTo-SecureString -String 'gpumon' -Force -AsPlainText; "
        f"Export-PfxCertificate -Cert $cert -FilePath '{pfx}' "
        f"-Password $pw | Out-Null; "
        f"Write-Output $cert.Thumbprint"
    )
    made = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                          capture_output=True, text=True, errors="replace",
                          creationflags=getattr(subprocess,
                                                "CREATE_NO_WINDOW", 0))
    thumbprint = made.stdout.strip().splitlines()[-1] if made.stdout.strip() else ""
    if not os.path.exists(pfx):
        print(f"  could not create the test certificate: "
              f"{(made.stderr or made.stdout).strip()[:200]}")
        return 1
    print(f"  thumbprint {thumbprint}")

    signtool = find_tool("signtool.exe")
    result = subprocess.run([signtool, "sign", "/fd", "sha256", "/f", pfx,
                             "/p", "gpumon", target],
                            capture_output=True, text=True, errors="replace",
                            creationflags=getattr(subprocess,
                                                  "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        print(f"  signing failed: {(result.stdout + result.stderr).strip()[-300:]}")
        return 1
    print(f"  signed with the test certificate: "
          f"{os.path.basename(pfx)} (password 'gpumon')")
    print("\n  To install it on this machine, trust the certificate first:")
    print(f"    Import-Certificate -FilePath <exported .cer> "
          f"-CertStoreLocation Cert:\\LocalMachine\\TrustedPeople")
    print("  That step needs administrator rights, and is for your own testing")
    print("  only - the Store signs the real package after certification.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the Store package")
    parser.add_argument("--sign", action="store_true",
                        help="sign it with a self-signed certificate, for a "
                             "local test install")
    parser.add_argument("--layout-only", action="store_true",
                        help="write the package folder and stop")
    options = parser.parse_args()
    if not build_layout():
        raise SystemExit(1)
    raise SystemExit(0 if options.layout_only else pack(options.sign))
