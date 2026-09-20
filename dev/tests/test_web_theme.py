"""Theme plumbing of the browser GUI: rendered CSS, the picker and /api/theme.

Same shape as `test_web.py` - the real server on an ephemeral port, driven with
`urllib` - but pointed at a config.json inside a temporary directory.
`POST /api/theme` persists a preference, and the project's config.json is also
where the user's alarm rules live, so a test must never be able to aim that
write at the real file. The last section proves it did not, by comparing the
real file's bytes before and after.
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





import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request

import alarms as A
import metrics as M
import sampler as SP
import store as S
import webgui
import webserver as WEB
from ui import themes

#: A config.json shaped like a real one: alarm rules plus unrelated settings.
#: Nothing in this test parses them, they are the cargo the read-modify-write
#: has to carry across the theme switch intact.
FIXTURE: dict = {
    "alarms": {
        "cpu_util": {"warning": 91.0, "critical": 99.0, "hysteresis": 2.0,
                     "min_duration": 5.0, "enabled": True},
        "gpu0_temp": {"warning": 80.0, "critical": 90.0, "hysteresis": 4.0,
                      "min_duration": 10.0, "enabled": True},
    },
    "sample_hz": 2.0,
    "per_core": False,
    "db_path": "C:/Users/example/Documents/gpumon/sessions.db",
}


def read_bytes(path: str) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


REAL_CONFIG = A.config_path()
REAL_BEFORE = read_bytes(REAL_CONFIG)

problems: list[str] = []
print("=" * 88)
print("WEB GUI THEME TEST")
print("=" * 88)


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)
    return ok


def request(path: str, method: str = "GET", payload: dict | None = None
            ) -> tuple[int, str]:
    """One HTTP round trip; HTTP errors come back as (status, body)."""
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=body, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def get_json(path: str) -> tuple[int, dict]:
    status, body = request(path)
    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return status, {}


def post_theme(name: object) -> tuple[int, dict]:
    status, body = request("/api/theme", "POST", {"theme": name})
    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return status, {}


# --------------------------------------------------------------------------
# Everything below happens in a scratch directory, never in the project.
# --------------------------------------------------------------------------
WORK = tempfile.mkdtemp(prefix="gpumon-theme-")
CONFIG = os.path.join(WORK, "config.json")
EXPECTED = os.path.join(WORK, "expected.json")
DB = os.path.join(WORK, "theme.db")
# Seeded through alarms.save_config so the file on disk already has the
# canonical formatting: the byte comparison at the end is then meaningful.
A.save_config(dict(FIXTURE), CONFIG)

manager = M.SensorManager(per_core=False)
store = S.Store(DB)
sampler = SP.Sampler(manager, store, sample_hz=1.0)
web = WEB.create_server(manager, store, sampler, host="127.0.0.1", port=0,
                        config_path=CONFIG)
web.start()
BASE = f"http://127.0.0.1:{web.port}"
print(f"\nserver on {BASE} (port 0 -> {web.port}), config {CONFIG}")

try:
    print("\n[1] the served CSS is rendered from the active theme")
    for key in themes.keys():
        themes.set_theme(key)
        palette = themes.current()
        page = request("/")[1]
        values = [palette.bg, palette.panel, palette.panel_alt, palette.border,
                  palette.text, palette.text_dim, palette.text_bright,
                  palette.accent, palette.ok, palette.warn, palette.crit,
                  palette.idle, palette.selection, palette.plot_bg]
        values.extend(palette.series)
        absent = [v for v in values if v not in page]
        check(f"{key}: every palette colour reaches the page", not absent,
              ", ".join(absent))
        leaks = [themes.get(o).bg for o in themes.keys()
                 if o != key and themes.get(o).bg in page]
        check(f"{key}: no other theme's background leaks in", not leaks,
              ", ".join(leaks))
        check(f"{key}: the picker shows it as the selected option",
              f'value="{key}" selected' in page, key)
        check(f"{key}: {len(palette.series)} series variables emitted",
              all(f"--series-{i}: {colour};" in page
                  for i, colour in enumerate(palette.series)),
              f"--series-0..{len(palette.series) - 1}")

    print("\n[2] the JavaScript owns no colours of its own")
    themes.set_theme(themes.DEFAULT_KEY)
    page = request("/")[1]
    script = page.split("<script>", 1)[1]
    hexes = sorted({h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}", script)})
    check("no hex literal survives in the script", not hexes, ", ".join(hexes))
    check("the script reads its palette back out of the CSS variables",
          "getComputedStyle" in script and "cssVar(" in script)
    check("the picker is real markup, not built by script",
          'id="theme-select"' in page.split("<script>", 1)[0]
          and "theme-select" in script)
    check("the server-side placeholder was filled in",
          webgui.PICKER_SLOT not in page, webgui.PICKER_SLOT)
    check("nothing is fetched from off the machine",
          'src="http' not in page and "@import" not in page
          and "cdn." not in page)

    print("\n[3] POST /api/theme switches the running server")
    status, payload = post_theme("shinji")
    check("status 200", status == 200, str(status))
    check("the answer carries ok, theme and label",
          payload.get("ok") is True and payload.get("theme") == "shinji"
          and payload.get("label") == "Shinji",
          json.dumps(payload)[:90])
    check("the active theme is now shinji", themes.current_key() == "shinji",
          themes.current_key())
    page = request("/")[1]
    check("the next page is served in shinji",
          "#0a0710" in page and "#9a6bff" in page)
    check("and no longer in the previous theme", "#0b0f14" not in page)
    check("the picker moved to the new option",
          page.count('value="shinji" selected') == 1
          and 'value="nvtop" selected' not in page)
    status, state = get_json("/api/state")
    check("/api/state reports theme and theme_label",
          state.get("theme") == "shinji"
          and state.get("theme_label") == "Shinji",
          f"{state.get('theme')!r} {state.get('theme_label')!r}")

    print("\n[4] keys, aliases and rejections")
    status, payload = post_theme("02")
    check("a relaxed spelling is normalized onto a key",
          status == 200 and payload.get("theme") == "asuka",
          json.dumps(payload)[:70])
    check("the alias really switched the palette",
          themes.current_key() == "asuka", themes.current_key())
    for bad in ("no-such-theme", "", None, 17):
        status, payload = post_theme(bad)
        check(f"{bad!r} is a 400 carrying a JSON error",
              status == 400 and isinstance(payload.get("error"), str),
              json.dumps(payload)[:70])
    status, body = request("/api/theme", "POST", {})
    check("a body with no theme key is a 400", status == 400, str(status))
    status, _ = request("/api/theme")
    check("GET /api/theme is a 405", status == 405, str(status))
    check("every rejection left the active theme alone",
          themes.current_key() == "asuka", themes.current_key())

    print("\n[5] the choice is persisted without disturbing the rest")
    status, payload = post_theme("nord")
    check("the switch was persisted", payload.get("persisted") is True,
          json.dumps(payload)[:70])
    # What the file must contain: the fixture plus one key. Writing that through
    # save_config gives the canonical bytes to compare against, so "otherwise
    # unchanged" is checked byte for byte rather than field by field.
    wanted = dict(FIXTURE)
    wanted["theme"] = "nord"
    A.save_config(dict(wanted), EXPECTED)
    got_bytes, want_bytes = read_bytes(CONFIG), read_bytes(EXPECTED)
    check("config.json is the original file plus one theme line",
          got_bytes == want_bytes,
          f"{len(got_bytes or b'')} bytes vs {len(want_bytes or b'')}")
    stored = json.loads((got_bytes or b"{}").decode("utf-8"))
    check("the theme is stored under the key \"theme\"",
          stored.get("theme") == "nord", repr(stored.get("theme")))
    check("the alarm rules survived the write",
          stored.get("alarms") == FIXTURE["alarms"])
    check("no other setting changed",
          {k: v for k, v in stored.items() if k != "theme"} == FIXTURE,
          ", ".join(sorted(stored)))
    post_theme("gruvbox")
    again = json.loads((read_bytes(CONFIG) or b"{}").decode("utf-8"))
    check("a second switch keeps the rules too",
          again.get("theme") == "gruvbox"
          and again.get("alarms") == FIXTURE["alarms"],
          repr(again.get("theme")))

    print("\n[6] a config.json that cannot be written still switches the theme")
    # A path whose directory does not exist: the theme must still change and the
    # page must still follow it, with only `persisted` turning false. Refusing
    # the switch would make a read-only checkout unusable.
    web.config_path = os.path.join(WORK, "no-such-dir", "config.json")
    status, payload = post_theme("matrix")
    check("the theme switched anyway", themes.current_key() == "matrix",
          themes.current_key())
    check("the answer reports persisted false",
          status == 200 and payload.get("persisted") is False,
          json.dumps(payload)[:70])
    check("the served page follows it",
          "#000700" in request("/")[1])
    check("nothing was created at the unwritable path",
          not os.path.exists(web.config_path), str(web.config_path))
    web.config_path = CONFIG

    print("\n[7] the user's own config.json was never the target")
    check("the server persists to the injected path",
          os.path.abspath(web.config_path or "") == os.path.abspath(CONFIG),
          str(web.config_path))
    check("the real config.json is byte-identical (or still absent)",
          read_bytes(REAL_CONFIG) == REAL_BEFORE,
          f"{REAL_CONFIG}: "
          + ("absent" if REAL_BEFORE is None else f"{len(REAL_BEFORE)} bytes"))
finally:
    print("\n[8] shutting down")
    web.stop()
    sampler.stop()
    manager.close()
    store.close()
    shutil.rmtree(WORK, ignore_errors=True)
    themes.set_theme(themes.DEFAULT_KEY)
    check("the scratch directory is gone", not os.path.exists(WORK), WORK)
    closed = True
    try:
        urllib.request.urlopen(BASE + "/api/state", timeout=2)
        closed = False
    except Exception:  # noqa: BLE001 - any failure here means the port is gone
        pass
    check("server stopped listening", closed)

print("\n" + "=" * 88)
if problems:
    print(f"WEB GUI THEME TEST FAILED ({len(problems)})")
    for item in problems:
        print(f"  - {item}")
else:
    print("WEB GUI THEME TEST PASSED")
print("=" * 88)
raise SystemExit(1 if problems else 0)
