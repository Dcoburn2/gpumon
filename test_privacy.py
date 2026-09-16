"""Does the privacy policy still describe what the program does?

A privacy policy is a promise about behaviour, and promises rot. The two claims
that matter here are checkable in the source, so they are checked:

  * "gpumon collects nothing and sends nothing anywhere" - the only outbound
    requests in the whole program go to two GitHub URLs, and only during the
    portable build's optional sensor setup.
  * "its own browser interface binds to 127.0.0.1 by default" - binding wider is
    an explicit opt-in.

If somebody adds an analytics call or a telemetry upload, this fails.
"""
from __future__ import annotations

import os
import re

POLICY = "PRIVACY.md"
problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


#: Build-time tooling and developer utilities. These are not in the shipped
#: program: `signing.py` talks to a timestamp server when somebody *builds* a
#: release, and the rest are packaging scripts. Auditing them would be auditing
#: the wrong thing, so they are excluded by name rather than by accident.
DEVELOPER_ONLY = {
    "make_release.py", "make_store_package.py", "store_capture.py",
    "make_icon.py", "signing.py", "build_exe.cmd", "make_shortcut.cmd",
}


def sources() -> list[str]:
    """Every Python file that ships, excluding tests and build tooling."""
    files = []
    for root, dirs, names in os.walk("."):
        dirs[:] = [d for d in dirs if d not in (
            ".git", "__pycache__", "dist", "build", "release", "release-onefile",
            "tools", "store", "pawnio-modules")]
        for name in names:
            if not name.endswith(".py"):
                continue
            if name.startswith(("test_", "_")) or name in DEVELOPER_ONLY:
                continue
            # Normalised, so ".\gpumon.py" and "gpumon.py" compare equal - the
            # first version of this test flagged the very files it allowed.
            files.append(os.path.normpath(os.path.join(root, name)))
    return files


def requests_made() -> dict[str, list[str]]:
    """Hosts a shipping module could contact, by reading the calls.

    Two traps to avoid. Scanning for URL literals finds ones the browser
    interface prints for a user to click and the XML namespaces in a manifest,
    neither of which is a request. And the calls that matter most pass a *named
    constant*, sometimes through a helper's parameter - `urlopen` lives in
    `download()`, which is handed `PAWNIO_SETUP_URL` - so following the value
    exactly would miss it and the audit would pass vacuously, which is worse than
    no audit.

    So: find the modules that make requests, and then count every URL constant
    they define. Deliberately over-inclusive - a telemetry URL added to a file
    that already talks to the network would be caught even if nothing called it
    yet.
    """
    import re

    hosts: dict[str, list[str]] = {}
    for path in sources():
        body = open(path, encoding="utf-8").read()
        lines = body.splitlines()
        call_lines = [number for number, line in enumerate(lines, 1)
                      if ("urlopen(" in line or "Request(" in line)
                      and not line.lstrip().startswith("#")]
        if not call_lines:
            continue
        where = f"{path}:{call_lines[0]}"
        for url in re.findall(r'https?://[^\s"\')\\,]+', body):
            host = url.split("//")[1].split("/")[0].split(":")[0]
            if host.startswith("schemas.") or host == "www.w3.org":
                continue                    # XML namespaces in generated markup
            hosts.setdefault(host, []).append(where)
    return hosts


print("=" * 84)
print("PRIVACY POLICY TEST")
print("=" * 84)

print("\n[1] the policy exists and is reachable")
check("PRIVACY.md is in the repository root", os.path.exists(POLICY))
text = open(POLICY, encoding="utf-8").read() if os.path.exists(POLICY) else ""
print(f"    {len(text.splitlines())} lines, {len(text):,} characters")
check("it says plainly that nothing is collected",
      "collects nothing" in text and "telemetry" in text)
for section in ("What gpumon reads", "What gpumon stores", "Network access",
                "Sharing", "Contact"):
    check(f"it covers {section!r}", section in text)

print("\n[2] 'sends nothing anywhere' is true of the code that ships")
hosts = requests_made()
print(f"    hosts contacted: {sorted(hosts) or 'none'}")
for host, where in sorted(hosts.items()):
    print(f"      {host} <- {', '.join(sorted(set(where)))}")
outbound = {host: where for host, where in hosts.items()
            if host not in ("127.0.0.1", "localhost")}
check("the sensor setup's two download hosts are found at all",
      {"github.com", "api.github.com"} <= set(hosts),
      "a vacuous audit is worse than none: constants must be resolved")
check("and they are the only outbound hosts",
      set(outbound) <= {"github.com", "api.github.com"}, str(sorted(outbound)))
check("every outbound request is in the sensor setup",
      all(all("sensorsetup.py" in where for where in where_list)
          for where_list in outbound.values()), str(outbound))
check("everything else is loopback, which the policy describes",
      all(host in ("127.0.0.1", "localhost") for host in hosts
          if host not in ("github.com", "api.github.com")), str(sorted(hosts)))
check("the policy names both download hosts",
      "github.com" in text and "api.github.com" in text)

print("\n[3] nothing phones home")
shipping = sources()
print(f"    {len(shipping)} shipping module(s) audited")
for token in ("analytics", "sentry_sdk", "posthog", "mixpanel",
              "google_analytics", "amplitude", "telemetry_client"):
    hits = [path for path in shipping
            if token in open(path, encoding="utf-8").read()]
    check(f"no {token}", not hits, str(hits))
check("no HTTP client beyond urllib is imported",
      not any(any(f"import {module}" in open(path, encoding="utf-8").read()
                  for module in ("requests", "httpx", "aiohttp", "socket"))
              for path in shipping))

print("\n[4] the browser interface binds to loopback, as claimed")
import webserver  # noqa: E402
print(f"    DEFAULT_HOST = {webserver.DEFAULT_HOST}")
check("the default is loopback", webserver.DEFAULT_HOST in ("127.0.0.1",
                                                            "localhost", "::1"))
check("and the policy says so", "127.0.0.1" in text and "opt-in" in text)
check("binding wider is a deliberate choice, not an accident",
      "--host" in open("gpumon.py", encoding="utf-8").read())

print("\n[5] what it says it stores is what it stores")
import apppaths  # noqa: E402
import alarms  # noqa: E402
import store as S  # noqa: E402
locations = {
    "sessions": S.default_db_path(),
    "settings": alarms.config_path(),
    "readings": apppaths.user_path("msr-sensors.json"),
}
for what, path in locations.items():
    print(f"    {what:9} -> {path}")
check("the policy names the session database",
      "sessions.db" in text and "Documents" in text)
check("and the user directory", "%LOCALAPPDATA%" in text)
check("and says how to delete everything",
      "Deleting it" in text or "delete" in text.lower())

print("\n[6] the submission guide points at it")
guide = open(os.path.join("store", "SUBMISSION.md"), encoding="utf-8").read()
check("the guide names the file", "PRIVACY.md" in guide)
check("and explains that Partner Center wants a URL",
      "URL" in guide, "a file upload is not what the form asks for")
check("and gives the GitHub form of that URL",
      "github.com/" in guide and "blob/main" in guide)

print("\n" + "=" * 84)
if problems:
    print(f"PRIVACY POLICY TEST FAILED ({len(problems)})")
    for item in problems:
        print(f"  - {item}")
else:
    print("PRIVACY POLICY TEST PASSED")
print("=" * 84)
raise SystemExit(1 if problems else 0)
