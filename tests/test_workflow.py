"""Is the release workflow well-formed?

A broken workflow is only discovered when a tag is pushed, which is the worst
moment to find out. This parses it and checks the parts that matter: the
triggers, the steps, and that the signing step is conditional rather than
required.
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





import pathlib

path = pathlib.Path(".github/workflows/release.yml")
text = path.read_text(encoding="utf-8")

problems = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


print("=" * 84)
print("RELEASE WORKFLOW TEST")
print("=" * 84)

print(f"\n[1] the file is where GitHub looks for it")
check("the workflow exists", path.exists(), str(path))

try:
    import yaml
except ImportError:
    yaml = None

if yaml is None:
    print("\n[2] pyyaml is not installed, so the structure is checked as text")
    check("it declares a tag trigger", "tags:" in text)
    check("it has a windows job", "runs-on: windows-latest" in text)
    names = [line.strip().removeprefix("- name: ").strip()
             for line in text.splitlines() if line.strip().startswith("- name: ")]
else:
    parsed = yaml.safe_load(text)
    print("\n[2] it parses as YAML")
    check("it parses", isinstance(parsed, dict))
    # PyYAML reads the bare word `on` as the boolean True, which is a trap worth
    # knowing about when checking triggers.
    triggers = parsed.get("on", parsed.get(True, {}))
    print(f"    triggers: {triggers}")
    check("it runs on version tags", "push" in triggers
          and "tags" in triggers["push"], str(triggers))
    check("and can be started by hand", "workflow_dispatch" in triggers)
    steps = parsed["jobs"]["windows"]["steps"]
    names = [step.get("name", "") for step in steps]
    print(f"    {len(steps)} steps:")
    for name in names:
        print(f"      - {name}")

print("\n[3] the steps that matter are there")
check("it runs the test suite before building",
      any("test" in name.lower() for name in names))
check("it builds the three artifacts",
      any("artifact" in name.lower() and "build" in name.lower()
          for name in names))
check("it signs", any(name.lower().startswith("sign") for name in names))
check("it uploads", any("upload" in name.lower() for name in names))
check("it attaches files to a release",
      any("attach" in name.lower() for name in names))

print("\n[4] signing is configured, not required")
check("the signing step is conditional", "if: env." in text,
      "a missing certificate must not fail the build")
check("an unsigned build is announced", "::warning::" in text)
check("and the release body says so",
      "not code-signed" in text or "unsigned" in text.lower())
check("the checksums are published", "sha256" in text.lower())

print("\n[4b] and the published checksum describes the published file")
# It published release/SHA256SUMS.txt, which is the checksum of gpumon.exe *inside*
# the archive. Anyone verifying the download against it got a mismatch and
# concluded the download was corrupt, which is worse than publishing nothing.
published = []
if parsed is not None:
    for step in parsed["jobs"]["windows"]["steps"]:
        files = (step.get("with") or {}).get("files")
        if files:
            published = [line.strip() for line in files.splitlines()
                         if line.strip()]
print(f"    release files: {published}")
check("the zip is published", any("zip" in name for name in published))
check("the zip's own checksum is published",
      any(name.endswith(".zip.sha256") for name in published), str(published))
check("the single-file build is published",
      any(name.endswith(".exe") for name in published), str(published))
check("and its checksum as well",
      any(name.endswith(".exe.sha256") for name in published), str(published))
check("the checksum of a file inside the archive is not published as the "
      "release's checksum",
      not any(name.endswith("SHA256SUMS.txt") for name in published),
      "that file describes gpumon.exe, not the zip")
check("the release body describes both downloads",
      "one file" in text.lower() or "single" in text.lower())

print("\n[5] it does not sign with a key from the repository")
check("no certificate or password is committed",
      "BEGIN PRIVATE KEY" not in text and "PFX_PASSWORD:" not in text,
      "keys belong in secrets, never in the workflow file")
check("the signing identity comes from secrets",
      "secrets.AZURE_SIGNING" in text)

print("\n[5b] and it is a workflow GitHub will actually accept")
# A class of error that only appears on the server: GitHub rejects certain
# contexts inside an `if:` condition. A workflow using `secrets` there is invalid
# and fails with *no jobs at all*, which reads like mysterious infrastructure
# trouble rather than a mistake in the file. That cost a failed run on every push
# until it was found, including the first tag.
conditions = [line.strip() for line in text.splitlines()
              if line.strip().startswith("if:")]
print(f"    {len(conditions)} condition(s):")
for condition in conditions:
    print(f"      {condition}")
    check(f"does not use the secrets context: {condition[:46]}",
          "secrets." not in condition,
          "secrets cannot be used in if: - pass it through env instead")
check("the signing secret reaches a condition through env",
      "SIGNING_METADATA: ${{ secrets." in text,
      "env is allowed in if:, and only a job or step env can carry it")

print("\n[6] and the local build says the same thing")
import signing  # noqa: E402
check("no signing identity is configured on this machine",
      signing.identity() == ("", ""), str(signing.identity()))
print(f"    signtool available: {bool(signing.find_signtool())}")

print("\n" + "=" * 84)
if problems:
    print(f"RELEASE WORKFLOW TEST FAILED ({len(problems)})")
    for item in problems:
        print(f"  - {item}")
else:
    print("RELEASE WORKFLOW TEST PASSED")
print("=" * 84)
raise SystemExit(1 if problems else 0)
