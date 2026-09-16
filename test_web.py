"""Headless test of the web GUI's HTTP surface.

Runs the real server on an ephemeral port (port 0) and drives every route with
`urllib`, so the whole path - sampler, live buffer, SQLite summary and the
handler - is exercised on a machine with no browser, and without launching one.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

import alarms as A
import metrics as M
import sampler as SP
import store as S
import webserver as WEB
from ui import themes

DB = "webdiag.db"
#: The theme route persists to config.json, so this test gets its own file.
#: Writing the project's real config.json would rewrite the user's alarm rules.
CONFIG = "webdiag-config.json"
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.remove(DB + suffix)
if os.path.exists(CONFIG):
    os.remove(CONFIG)

problems: list[str] = []
print("=" * 88)
print("WEB GUI TEST")
print("=" * 88)


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)
    return ok


def request(path: str, method: str = "GET", payload: dict | None = None,
            raw: bytes | None = None) -> tuple[int, str]:
    """One HTTP round trip; HTTP errors come back as (status, body)."""
    body = raw
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


# --------------------------------------------------------------------------
# Bring up exactly what `webserver.serve()` builds, on an ephemeral port.
# --------------------------------------------------------------------------
manager = M.SensorManager(per_core=False)
store = S.Store(DB)
engine = A.AlarmEngine()
# A threshold every machine crosses, so the summary has a real alarm event to
# return rather than an empty list that would pass vacuously.
engine.set_rule(A.AlarmRule("cpu_util", warning=0.0, critical=None,
                            hysteresis=0.0, min_duration=0.0))
sampler = SP.Sampler(manager, store, sample_hz=4.0, alarm_engine=engine)
sampler.configure_alarms(engine.rules or None)
web = WEB.create_server(manager, store, sampler, host="127.0.0.1", port=0,
                        config_path=CONFIG)
web.start()
sampler.start()
BASE = f"http://127.0.0.1:{web.port}"
print(f"\nserver on {BASE} (port 0 -> {web.port})")
print(f"devices: {[(g.index, g.name, g.pci) for g in manager.gpus]}")

try:
    print("\n[1] GET / serves the page")
    status, html = request("/")
    check("status 200", status == 200, str(status))
    check("is HTML", html.lstrip().lower().startswith("<!doctype html>"))
    check("contains a canvas element", "<canvas" in html)
    check("contains the script and the active theme's background",
          # Read from the palette rather than a literal: this should test the
          # rendering, not one theme's colour, which is what it did until the
          # default theme changed and took this assertion with it.
          "<script>" in html and themes.current().bg in html,
          themes.current_key())
    palette = themes.current()
    check("the CSS is rendered from the active palette",
          f"--bg: {palette.bg};" in html and f"--accent: {palette.accent};" in html
          and "--plot-bg:" in html and "--series-0:" in html,
          themes.current_key())
    check("theme picker rendered with the active theme selected",
          'id="theme-select"' in html
          and f'value="{palette.key}" selected' in html
          and html.count("<option") >= len(themes.keys()),
          f"{len(themes.keys())} themes")
    check("no third-party asset is fetched",
          'src="http' not in html and 'href="http' not in html
          and "@import" not in html)

    print("\n[2] GET /api/state")
    status, state = get_json("/api/state")
    check("status 200", status == 200, str(status))
    devices = state.get("devices") or []
    check("devices listed", bool(devices), f"{len(devices)} device(s)")
    check("every device carries index/name/pci",
          all(isinstance(d.get("index"), int) and isinstance(d.get("name"), str)
              and "pci" in d for d in devices))
    caps = state.get("capabilities") or {}
    check("capabilities report notes/backend_errors/cpu_temp",
          isinstance(caps.get("notes"), list)
          and isinstance(caps.get("backend_errors"), dict)
          and isinstance(caps.get("cpu_temp"), bool))
    metrics_meta = state.get("metrics") or {}
    check("metric catalogue is non-empty", bool(metrics_meta),
          f"{len(metrics_meta)} metrics")
    check("catalogue entries have label/unit/precision/kind",
          all(set(("label", "unit", "precision", "kind")) <= set(m)
              for m in metrics_meta.values()))
    check("status block present", isinstance(state.get("status"), dict))
    check("active alarms listed", isinstance(state.get("alarms"), list))

    print("\n[3] GET /api/sample (latest merged sample, no hardware poll)")
    sample: dict = {}
    deadline = time.monotonic() + 25.0
    while time.monotonic() < deadline:
        status, sample = get_json("/api/sample")
        if status == 200 and isinstance(
                (sample.get("values") or {}).get("cpu_util"), (int, float)):
            break
        time.sleep(0.4)
    check("status 200", status == 200, str(status))
    values = sample.get("values") or {}
    check("cpu_util is a number", isinstance(values.get("cpu_util"), (int, float)),
          f"cpu_util={values.get('cpu_util')!r}")
    check("values are flat metric->number",
          all(isinstance(v, (int, float)) for v in values.values()),
          f"{len(values)} metrics")
    check("status block repeated for the footer",
          isinstance(sample.get("status"), dict)
          and "poll_ms" in sample["status"] and "dropped" in sample["status"])
    check("active_alarms present", isinstance(sample.get("active_alarms"), list),
          f"{len(sample.get('active_alarms') or [])} open")
    started = time.perf_counter()
    for _ in range(5):
        request("/api/sample")
    per_request = (time.perf_counter() - started) / 5 * 1000.0
    print(f"    /api/sample served in {per_request:.1f} ms on average")
    check("sample route stays cheap", per_request < 250.0,
          f"{per_request:.1f} ms")

    print("\n[4] GET /api/history")
    status, history = get_json("/api/history?seconds=60")
    check("status 200", status == 200, str(status))
    series = history.get("series") or {}
    check("series returned", bool(series), f"{len(series)} series")
    points = series.get("cpu_util") or []
    check("cpu_util series has [t, value] pairs",
          bool(points) and all(isinstance(p, list) and len(p) == 2 for p in points),
          f"{len(points)} points")
    check("timestamps increase",
          all(points[i][0] <= points[i + 1][0] for i in range(len(points) - 1)))
    status, bad = get_json("/api/history?seconds=nonsense")
    check("a non-numeric seconds is a 400", status == 400, str(status))

    print("\n[5] logging start / marker / stop / sessions")
    status, body = request("/api/logging/start", "POST", {"label": "web test"})
    started_body = json.loads(body)
    check("POST /api/logging/start returns a session id",
          status == 200 and isinstance(started_body.get("session_id"), int),
          body.strip())
    session_id = started_body["session_id"]
    status, again = request("/api/logging/start", "POST", {"label": "ignored"})
    check("starting twice keeps the same session",
          status == 200 and json.loads(again).get("session_id") == session_id,
          again.strip())
    status, marker_body = request("/api/marker", "POST", {"label": "halfway"})
    marker = json.loads(marker_body)
    check("POST /api/marker records the marker",
          status == 200 and marker.get("ok") is True, marker_body.strip())

    status, sessions = get_json("/api/sessions")
    ids = [s.get("id") for s in (sessions.get("sessions") or [])]
    check("the running session is listed", session_id in ids, f"ids={ids}")

    print("    collecting samples...")
    time.sleep(3.0)

    status, stopped_body = request("/api/logging/stop", "POST", {})
    stopped = json.loads(stopped_body)
    check("POST /api/logging/stop returns the same session id",
          status == 200 and stopped.get("session_id") == session_id,
          stopped_body.strip())
    check("samples were written", (stopped.get("samples") or 0) > 0,
          f"{stopped.get('samples')} samples, {stopped.get('alarms')} alarms")

    status, sessions = get_json("/api/sessions")
    listed = {s.get("id"): s for s in (sessions.get("sessions") or [])}
    check("the finished session appears in /api/sessions",
          session_id in listed)
    if session_id in listed:
        print(f"    session {session_id}: label={listed[session_id]['label']!r} "
              f"samples={listed[session_id]['sample_count']} "
              f"alarms={listed[session_id]['alarm_count']}")

    print("\n[6] GET /api/summary")
    status, summary = get_json(f"/api/summary?session_id={session_id}")
    check("status 200", status == 200, str(status))
    stats = summary.get("stats") or {}
    check("statistics returned", bool(stats), f"{len(stats)} metrics")
    st = stats.get("cpu_util") or {}
    check("cpu_util stats carry the full set",
          all(k in st for k in ("count", "minimum", "maximum", "mean", "median",
                                "p95", "p99", "stdev", "first", "last")),
          ", ".join(sorted(st)) if st else "none")
    check("stats are sane", (st.get("minimum") or 0) <= (st.get("mean") or 0)
          <= (st.get("maximum") or 0) <= 100.5,
          f"min={st.get('minimum')} mean={st.get('mean')} max={st.get('maximum')}")
    check("alarm events included", isinstance(summary.get("alarms"), list)
          and len(summary["alarms"]) > 0,
          f"{len(summary.get('alarms') or [])} event(s)")
    check("markers included",
          any(m.get("label") == "halfway"
              for m in (summary.get("markers") or [])))
    check("device records included from the session",
          bool(summary.get("devices")))
    window = summary.get("window") or {}
    check("span reported for the range selector",
          isinstance(window.get("span"), list) and len(window["span"]) == 2,
          str(window.get("span")))

    span = window["span"]
    mid = span[0] + (span[1] - span[0]) * 0.5
    status, sub = get_json(f"/api/summary?session_id={session_id}"
                           f"&t0={span[0]:.3f}&t1={mid:.3f}")
    sub_stats = (sub.get("stats") or {}).get("cpu_util") or {}
    check("a sub-window is honoured",
          status == 200 and 0 < (sub_stats.get("count") or 0) < (st.get("count") or 1),
          f"{sub_stats.get('count')} of {st.get('count')} samples")
    check("the window is echoed back",
          abs(((sub.get("window") or {}).get("t1") or -1)
              - float(f"{mid:.3f}")) < 1e-9,
          str((sub.get("window") or {}).get("t1")))

    status, _ = get_json("/api/summary?session_id=999999")
    check("an unknown session is a 404", status == 404, str(status))

    print("\n[7] GET /api/series")
    status, one = get_json(f"/api/series?session_id={session_id}&metric=cpu_util")
    check("status 200", status == 200, str(status))
    check("series points returned", bool(one.get("series")),
          f"{len(one.get('series') or [])} points")
    check("metric metadata returned with it",
          one.get("label") == "CPU Utilization" and one.get("unit") == "%",
          f"{one.get('label')!r} {one.get('unit')!r}")
    check("t0/t1 accepted",
          get_json(f"/api/series?session_id={session_id}&metric=cpu_util"
                   "&t0=0&t1=1")[0] == 200)
    status, _ = get_json(f"/api/series?session_id={session_id}&metric=not_a_metric")
    check("an unknown metric is a 404", status == 404, str(status))
    status, _ = get_json("/api/series?session_id=" + str(session_id))
    check("a missing metric is a 400", status == 400, str(status))

    print("\n[8] error handling")
    status, body = request("/api/does-not-exist")
    check("an unknown route is a 404", status == 404, str(status))
    check("the 404 carries a JSON error",
          "error" in json.loads(body), body.strip()[:80])
    status, _ = request("/api/sample", "POST", {})
    check("the wrong method is a 405", status == 405, str(status))
    status, _ = request("/api/marker", "POST", raw=b"{not json}")
    check("a malformed JSON body is a 400", status == 400, str(status))
    status, _ = request("/favicon.ico")
    check("the favicon probe is answered without an error", status in (204, 200),
          str(status))
    status, _ = request("/", "HEAD")
    check("HEAD / works", status == 200, str(status))

    print("\n[9] theme: the page follows the palette, POST /api/theme switches it")
    status, html = request("/")
    check("the served CSS uses the active palette's colours",
          themes.current().bg in html and themes.current().accent in html,
          themes.current_key())
    status, switched = request("/api/theme", "POST", {"theme": "gruvbox"})
    body = json.loads(switched)
    check("POST /api/theme answers with key, label and persistence",
          status == 200 and body.get("theme") == "gruvbox"
          and body.get("label") == "Gruvbox" and body.get("persisted") is True,
          switched.strip()[:100])
    _, html = request("/")
    check("the next page is served in the new theme",
          "#1d2021" in html and "#d79921" in html and "#0b0f14" not in html)
    status, state = get_json("/api/state")
    check("/api/state reports theme and theme_label",
          state.get("theme") == "gruvbox"
          and state.get("theme_label") == "Gruvbox",
          f"{state.get('theme')!r} {state.get('theme_label')!r}")
    status, body = request("/api/theme", "POST", {"theme": "no-such-theme"})
    check("an unknown theme key is a 400 carrying a JSON error",
          status == 400 and "error" in json.loads(body), body.strip()[:90])
    check("a rejected switch leaves the active theme alone",
          themes.current_key() == "gruvbox", themes.current_key())
    with open(CONFIG, "r", encoding="utf-8") as fh:
        saved = json.load(fh)
    check("the choice was persisted to this test's config file",
          saved.get("theme") == "gruvbox", str(sorted(saved)))
    request("/api/theme", "POST", {"theme": "nvtop"})

    print("\n[10] the server is still alive after all of that")
    status, sample = get_json("/api/sample")
    check("still answering", status == 200, str(status))
    check("sampler is still sampling",
          isinstance((sample.get("status") or {}).get("effective_hz"), float)
          and (sample["status"]["effective_hz"] or 0) > 0.5,
          f"effective {sample.get('status', {}).get('effective_hz')} Hz")
    check("no empty ticks recorded", (sample["status"].get("dropped") or 0) == 0,
          f"dropped={sample['status'].get('dropped')}")
finally:
    print("\n[11] shutting down")
    web.stop()
    sampler.stop()
    manager.close()
    store.close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(DB + suffix):
            try:
                os.remove(DB + suffix)
            except OSError:
                pass
    if os.path.exists(CONFIG):
        try:
            os.remove(CONFIG)
        except OSError:
            pass
    closed = True
    try:
        urllib.request.urlopen(BASE + "/api/state", timeout=2)
        closed = False
    except Exception:  # noqa: BLE001 - any failure here means the port is gone
        pass
    check("server stopped listening", closed)

print("\n" + "=" * 88)
if problems:
    print(f"WEB GUI TEST FAILED ({len(problems)})")
    for item in problems:
        print(f"  - {item}")
else:
    print("WEB GUI TEST PASSED")
print("=" * 88)
raise SystemExit(1 if problems else 0)
