"""Live-history panning in the browser GUI: one offset, drag, shift+wheel, resume.

The offset is browser view state on purpose. Nothing sends it to the server, so
the server cannot be asked what it is, and this test cannot ask a browser either:
there is no JavaScript engine in the standard library and no browser on a
benchmark host. What it can do is drive the real server and inspect - and run -
exactly what that server serves:

  * the markup and stylesheet: the LIVE/HISTORY control, its banner and the
    warning-coloured frame that says a chart is showing history;
  * the script: the handlers, the wording, and the state machine. When Node is
    on PATH the state machine itself is sliced out of the served page and
    executed against stub DOM nodes, so "the offset starts at 0", "it grows by
    one per arriving sample while panned", "resuming returns it to 0" and the
    gestures are observed rather than grepped for. Without Node those checks are
    replaced by source-level ones and the run says so.
  * the data plane: /api/sample and /api/history answer exactly what they
    answered before, no route was added for the view state, and samples keep
    arriving at the sampler's own rate - the view cannot pause or filter them.

What is *not* proven here: pixels, real browser events, and anything a browser
does with the markup. The report says so too.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

import metrics as M
import sampler as SP
import store as S
import webserver as WEB
from ui import themes

HERE = os.path.dirname(os.path.abspath(__file__))
#: The desktop window this behaviour has to match, read as text: importing it
#: would drag tkinter into a headless test, and the wording is the thing worth
#: comparing.
DESKTOP_SOURCE = os.path.join(HERE, "ui", "monitor.py")
#: The gesture the desktop banner teaches, quoted here as well so a reworded
#: desktop banner fails this test instead of drifting silently.
HINT = "drag or shift+wheel to pan, P to resume live"
#: Every route the server had before this feature. Nothing about the view state
#: may need one of its own.
ROUTES = {
    ("GET", "/"), ("GET", "/favicon.ico"), ("GET", "/api/state"),
    ("GET", "/api/sample"), ("GET", "/api/history"), ("GET", "/api/sessions"),
    ("GET", "/api/summary"), ("GET", "/api/series"),
    ("POST", "/api/logging/start"), ("POST", "/api/logging/stop"),
    ("POST", "/api/marker"), ("POST", "/api/theme"),
}

WORK = tempfile.mkdtemp(prefix="gpumon-history-")
DB = os.path.join(WORK, "history.db")
CONFIG = os.path.join(WORK, "config.json")
HARNESS = os.path.join(WORK, "history-harness.js")

problems: list[str] = []
print("=" * 88)
print("WEB GUI LIVE HISTORY TEST")
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


def line_source(script: str, marker: str) -> str:
    """One `var X = ...;` declaration out of the served script.

    The script is served as text and the test has to hand parts of it back to a
    JavaScript engine, so declarations are lifted verbatim - the run below then
    proves things about the code the browser will run, not about a copy of it.
    """
    start = script.find(marker)
    if start < 0:
        return ""
    end = script.find(";", start)
    return script[start:end + 1] if end >= 0 else ""


def block_source(script: str, marker: str) -> str:
    """A whole function (or call statement) from `marker` to its closing brace.

    Brace counting is enough here: the script is hand-formatted, contains no
    braces inside its string literals or comments, and a piece that ran past its
    own function is caught by the extraction check below.
    """
    start = script.find(marker)
    if start < 0:
        return ""
    depth = 0
    opened = False
    for index in range(start, len(script)):
        char = script[index]
        if char == "{":
            depth += 1
            opened = True
        elif char == "}":
            depth -= 1
            if opened and depth == 0:
                end = index + 1
                if ")" in script[end:script.find("\n", end)]:
                    stop = script.find(";", end)
                    if 0 <= stop - end < 8:
                        end = stop + 1
                return script[start:end]
    return ""


#: The stub page the extracted state machine runs against: the nodes the
#: indicator writes to, a canvas that records the handlers the gestures arrive
#: on, and no-ops for the parts of the page this test is not about.
STUBS = r"""
/* Node harness written by test_web_history.py. The functions below it are the
   served page's own code; everything above is the least a browser would have to
   provide for that code to run. If the served code stops creeping the offset,
   stops tinting a panned view or stops resuming, this prints FAIL. */
var failures = 0;
function check(label, ok, detail) {
  if (!ok) { failures = failures + 1; }
  console.log('  ' + (ok ? 'OK  ' : 'FAIL') + ' ' + label +
              (detail === undefined || detail === '' ? '' : '  ' + detail));
}
var nodes = {};
function stubNode(id) {
  return {id: id, textContent: '', className: '', title: '', hidden: false,
          style: {}, handlers: {}, focus: function () {},
          setAttribute: function () {},
          addEventListener: function (type, fn) { this.handlers[type] = fn; }};
}
nodes['hist-chip'] = stubNode('hist-chip');
nodes['hist-banner'] = stubNode('hist-banner');
function byId(id) {
  if (!nodes[id]) { nodes[id] = stubNode(id); }
  return nodes[id];
}
var bodyClasses = {};
var document = {
  body: {classList: {
    add: function (name) { bodyClasses[name] = true; },
    remove: function (name) { delete bodyClasses[name]; },
    contains: function (name) { return bodyClasses[name] === true; }}},
  listeners: {},
  addEventListener: function (type, fn) { document.listeners[type] = fn; }
};
var window = {innerHeight: 800, addEventListener: function () {}};
var summary = {window: null};
function scheduleDraw() {}
function renderStatus() {}
function renderAlarms() {}
function refreshSessions() {}
function loadSummary() {}
function setRowValue(entry, value) { entry.shown = value; }
/* Handlers wireControls passes around as values; only the chip's is exercised. */
function addMarker() {}
function newSessionSelected() {}
function applyTheme() {}
"""

SETUP = r"""
/* The rate the running server reported, and the words the desktop banner uses. */
var HZ = @@HZ@@;
var HINT = '@@HINT@@';
state.status = {sample_hz: HZ, logging: false, session_id: null};
var chipNode = byId('hist-chip');
var bannerNode = byId('hist-banner');
function makeRow(key) {
  var row = {key: key, points: [], value: {style: {}}, meta: {},
             spec: {tmin: 0, tmax: 1}};
  row.spec.lines = [{label: key, points: row.points}];
  liveSeries[key] = row;
  return row;
}
var rowA = makeRow('a');
var rowB = makeRow('b');
function stubCanvas() {
  return {clientWidth: 60 * HZ, handlers: {}, focus: function () {},
          addEventListener: function (type, fn) { this.handlers[type] = fn; }};
}
var canvas = stubCanvas();
attachHistoryPan(canvas);
wireControls();
/* One arriving sample, 1/hz apart in time exactly as the sampler produces them,
   so the offset-to-seconds conversion is exercised at a real rate. */
function feed(t, value) {
  var values = {};
  for (var key in liveSeries) {
    if (liveSeries.hasOwnProperty(key)) { values[key] = value; }
  }
  applySample({wall: t, t: t, wall_offset: 0, values: values,
               status: state.status, active_alarms: []});
}
function arrive(index, value) { feed(index / HZ, value); }
function bufferPoints(first, last) {
  var points = [];
  for (var index = first; index <= last; index++) {
    points.push([index / HZ, 100 + index]);
  }
  return points;
}
"""

CHECKS = r"""
/* [7a] a fresh view is live */
check('the offset starts at 0', hist.offset === 0, 'offset=' + hist.offset);
applyHistoryView();
check('the control reads LIVE before anything is panned',
      chipNode.textContent === 'LIVE', chipNode.textContent);
check('the banner is hidden before anything is panned', bannerNode.hidden === true);
check('no chart is tinted before anything is panned',
      document.body.classList.contains('panned') === false);

/* [7b] live: a fixed window with the newest sample on the right edge */
var index;
for (index = 0; index < 40; index++) { arrive(index, 100 + index); }
check('live, the newest sample sits at the right edge of a ' + LIVE_SECONDS +
      ' s window',
      Math.abs(rowA.spec.tmax - 39 / HZ) < 1e-9 &&
      Math.abs(rowA.spec.tmax - rowA.spec.tmin - LIVE_SECONDS) < 1e-9,
      'window ' + rowA.spec.tmin.toFixed(2) + '..' + rowA.spec.tmax.toFixed(2));
check('live, the badge is the arriving value', rowA.shown === 139,
      'shown=' + rowA.shown);

/* [7c] panning moves the one offset every chart shares */
panSamples(12);
check('panning moves the offset', hist.offset === 12, 'offset=' + hist.offset);
check('the control counts the pan in seconds at the sampler rate',
      chipNode.textContent === 'HISTORY  -' + (12 / HZ).toFixed(1) + 's',
      chipNode.textContent);
check('the control is styled as history while panned',
      chipNode.className === 'chip panned', chipNode.className);
check('the banner appears, in the desktop window\'s words',
      bannerNode.hidden === false &&
      bannerNode.textContent === 'HISTORY  -' + (12 / HZ).toFixed(1) + 's' +
                                 '  (' + HINT + ')',
      bannerNode.textContent);
check('the frames take the class the stylesheet tints',
      document.body.classList.contains('panned') === true);
check('every chart panned by the same 12 samples',
      Math.abs(rowA.spec.tmax - 27 / HZ) < 1e-9 &&
      Math.abs(rowB.spec.tmax - rowA.spec.tmax) < 1e-9,
      'right edges ' + rowA.spec.tmax + ' and ' + rowB.spec.tmax);
check('the badge shows the value at the right edge of the view, not the live one',
      rowA.shown === 127, 'shown=' + rowA.shown);

/* [7d] samples keep arriving: the view must not creep back to live */
var keptEdge = rowA.spec.tmax;
var keptBadge = rowA.shown;
for (index = 40; index < 45; index++) { arrive(index, 100 + index); }
check('the offset grows by one per arriving sample',
      hist.offset === 17, 'offset=' + hist.offset);
check('so the same history stays on screen',
      Math.abs(rowA.spec.tmax - keptEdge) < 1e-9 && rowA.shown === keptBadge,
      'right edge ' + rowA.spec.tmax + ', badge ' + rowA.shown);
feed(44 / HZ, 999);
check('a repeated sample (the page polls faster than the sampler) does not creep',
      hist.offset === 17, 'offset=' + hist.offset);
check('and it replaced the last point instead of stacking a duplicate',
      rowA.points.length === 45, rowA.points.length + ' points');

/* [7e] the ring buffer is authoritative when the page has missed samples */
applyHistory({a: bufferPoints(3, 47), b: bufferPoints(3, 47)});
check('a resync leaves the view where it is', hist.offset === 17,
      'offset=' + hist.offset);
arrive(48, 148);
check('after a resync the creep counts from the buffer, not from the last poll',
      hist.offset === 18, 'offset=' + hist.offset);

/* [7f] the clamp is the buffer's own end */
setOffset(9999);
check('the offset cannot run past the oldest buffered sample',
      hist.offset === 44 && hist.offset === rowA.points.length - 2,
      'offset=' + hist.offset + ' of ' + rowA.points.length + ' points');
check('and the view still reports itself as panned',
      bannerNode.hidden === false &&
      document.body.classList.contains('panned') === true);

/* [7g] back to live */
resumeLive();
check('resuming sets the offset back to 0', hist.offset === 0);
check('the control reads LIVE again',
      chipNode.textContent === 'LIVE' && chipNode.className === 'chip live');
check('the banner is hidden again', bannerNode.hidden === true);
check('the tint is gone',
      document.body.classList.contains('panned') === false);
check('the badge is the live value again', rowA.shown === 148,
      'shown=' + rowA.shown);

/* [7h] the gestures, driven through the handlers the page registers.
   The trace follows the pointer, so dragging left walks back in time and
   dragging right returns towards live - the same arithmetic as
   ui/monitor.py::_on_graph_drag. */
canvas.handlers.mousemove({clientX: 300});
check('a move with no press is not a drag', hist.offset === 0,
      'offset=' + hist.offset);
canvas.handlers.mousedown({button: 0, clientX: 100,
                           preventDefault: function () {}});
canvas.handlers.mousemove({clientX: 92});
canvas.handlers.mouseup({clientX: 92});
check('dragging left walks back in time, following the pointer',
      hist.offset === 8, 'offset=' + hist.offset);
canvas.handlers.mousedown({button: 0, clientX: 100,
                           preventDefault: function () {}});
canvas.handlers.mousemove({clientX: 108});
canvas.handlers.mouseup({clientX: 108});
check('dragging right comes forward again', hist.offset === 0,
      'offset=' + hist.offset);
var prevented = false;
canvas.handlers.wheel({shiftKey: true, deltaY: 120,
                       preventDefault: function () { prevented = true; }});
check('shift+wheel pans one notch of ' + WHEEL_SAMPLES + ' samples',
      hist.offset === WHEEL_SAMPLES && prevented === true,
      'offset=' + hist.offset);
canvas.handlers.wheel({shiftKey: true, deltaY: -120,
                       preventDefault: function () { prevented = true; }});
check('shift+wheel the other way comes forward again', hist.offset === 0,
      'offset=' + hist.offset);
prevented = false;
canvas.handlers.wheel({shiftKey: false, deltaY: 120,
                       preventDefault: function () { prevented = true; }});
check('a plain wheel is left to scroll the page',
      hist.offset === 0 && prevented === false, 'offset=' + hist.offset);
panSamples(5);
canvas.handlers.dblclick({});
check('double-click resumes live', hist.offset === 0, 'offset=' + hist.offset);
panSamples(7);
chipNode.handlers.click({});
check('the LIVE chip resumes on a click alone',
      hist.offset === 0 && chipNode.textContent === 'LIVE',
      chipNode.textContent);
panSamples(9);
document.listeners.keydown({key: 'p', target: {tagName: 'BODY'},
                            preventDefault: function () {}});
check('P resumes live', hist.offset === 0, 'offset=' + hist.offset);
panSamples(9);
document.listeners.keydown({key: 'p', target: {tagName: 'INPUT'},
                            preventDefault: function () {}});
check('P inside a text field is left to the text field', hist.offset === 9,
      'offset=' + hist.offset);
resumeLive();

/* [7i] the exact wording, at the rate the desktop draws its sparks at */
state.status.sample_hz = 1.0;
setOffset(12);
check('the banner reads exactly as the desktop window writes it',
      bannerNode.textContent === 'HISTORY  -12.0s  (' + HINT + ')',
      bannerNode.textContent);
check('and the control shows the same distance',
      chipNode.textContent === 'HISTORY  -12.0s', chipNode.textContent);
state.status.sample_hz = HZ;

console.log('NODE FAILURES ' + failures);
process.exitCode = failures ? 1 : 0;
"""

# --------------------------------------------------------------------------
# Bring up exactly what `webserver.serve()` builds, on an ephemeral port.
# --------------------------------------------------------------------------
manager = M.SensorManager(per_core=False)
store = S.Store(DB)
sampler = SP.Sampler(manager, store, sample_hz=4.0)
web = WEB.create_server(manager, store, sampler, host="127.0.0.1", port=0,
                        config_path=CONFIG)
web.start()
sampler.start()
BASE = f"http://127.0.0.1:{web.port}"
print(f"\nserver on {BASE} (port 0 -> {web.port})")

try:
    status, page = request("/")
    markup = page.split("<script>", 1)[0]
    script = page.split("<script>", 1)[1]
    style = markup.split("<style>", 1)[1].split("</style>", 1)[0]

    print("\n[1] the page starts live, with a control and a banner for the offset")
    check("status 200 and the page is served", status == 200 and "<canvas" in page,
          str(status))
    chip = re.search(r"<button id=\"hist-chip\"[^>]*>LIVE</button>", markup)
    check("the LIVE chip is a real, clickable button in the markup",
          bool(chip) and 'class="chip live"' in chip.group(0),
          chip.group(0) if chip else "not found")
    check("the chip is wired to resume in the script",
          "byId('hist-chip').addEventListener('click', resumeLive)" in script)
    banner = re.search(r"<span id=\"hist-banner\"[^>]*hidden>", markup)
    check("the HISTORY banner exists and starts hidden (offset 0)",
          bool(banner), banner.group(0) if banner else "not found")
    check("the markup teaches the gesture before anything is panned",
          HINT in markup and "shift+wheel" in markup)
    check("nothing is fetched from off the machine",
          'src="http' not in page and "@import" not in page
          and "cdn." not in page)

    print("\n[2] a panned view is tinted from the palette, never hardcoded")
    panned_rule = re.search(r"body\.panned canvas\.graph[^{]*\{[^}]*\}", style)
    rule = panned_rule.group(0) if panned_rule else ""
    check("the panned frame rule exists and uses the theme's warning colour",
          bool(rule) and "var(--warn)" in rule and "#" not in rule,
          " ".join(rule.split()))
    check("it covers a focused graph too, so focus cannot hide the state",
          ":focus" in rule)
    check("only the live graphs are tinted: summary charts keep their own drag",
          "body.panned canvas.chart" not in style)
    banner_rule = re.search(r"\.banner \{[^}]*\}", style)
    brow = banner_rule.group(0) if banner_rule else ""
    check("the banner is drawn like the desktop's: warning on plot background",
          "var(--warn)" in brow and "var(--plot-bg)" in brow and "#" not in brow,
          " ".join(brow.split()))
    check("the hidden banner is actually hidden", ".banner[hidden]" in style)
    sticky = re.search(r"#historybar \{[^}]*\}", style)
    check("the indicator stays on screen while a chart further down is panned",
          bool(sticky) and "position: sticky" in sticky.group(0),
          " ".join(sticky.group(0).split()) if sticky else "no rule")
    check("the warning colour is this theme's own",
          f"--warn: {themes.current().warn};" in style, themes.current().warn)

    print("\n[3] the script keeps one shared offset, starting at 0")
    check("the offset starts at 0 in the served script",
          "var hist = {offset: 0" in script)
    check("no chart carries an offset of its own", "__offset" not in script)
    check("the depth is the ring buffer's, as on the desktop",
          "points.length - 2" in script)
    live_row = block_source(script, "function addLiveRow(")
    check("every live row subscribes to the pan gesture",
          "attachHistoryPan(canvas);" in live_row)
    check("the rows are marked as the pannable ones", "spec.live = true;" in live_row)

    print("\n[4] the handlers the gestures arrive on")
    check("drag is wired: press, move, release and leave",
          all(f"addEventListener('{event}'" in script
              for event in ("mousedown", "mousemove", "mouseup", "mouseleave")))
    check("double-click is wired", "addEventListener('dblclick'" in script)
    check("the wheel listener asks for a non-passive listener so it can stop "
          "the page scroll", "{passive: false}" in script)
    check("a plain wheel is left to the page", "if (!ev.shiftKey) { return; }" in script)
    check("P resumes live from the document key handler",
          "(key === 'p' || key === 'P') { resumeLive(); }" in script)
    check("the panel's own hint says the same thing as the desktop",
          HINT in markup)

    print("\n[5] the wording and the badge match the desktop window")
    check("the banner is built with the desktop's wording",
          "'HISTORY  -' + historySeconds().toFixed(1) + 's'" in script
          and "(drag or shift+wheel to pan, P to resume live)" in script)
    check("the badge is the value at the right edge of the view",
          "entry.points.length - 1 - hist.offset" in script)
    check("the charts draw only the window, like Sparkline._draw_series",
          "windowPoints(spec.lines[vi].points, tmin, tmax)" in script)
    try:
        with open(DESKTOP_SOURCE, "r", encoding="utf-8") as fh:
            desktop = fh.read()
    except OSError as exc:
        desktop = ""
        print(f"    note: could not read {DESKTOP_SOURCE} ({exc}); the wording "
              f"was not compared with the desktop window")
    if desktop:
        # The desktop writes its banner as two adjacent f-strings so a line-length
        # limit does not reword it; joining them here is what makes this about the
        # words the reader is taught rather than about where the source wraps. The
        # only difference that survives is the thousands separator, which cannot
        # occur in a 60 s browser buffer.
        found = re.search(r'text=f"( HISTORY.*?)",\n', desktop, re.S)
        banner = re.sub(r'"\s*f"', "", found.group(1)).strip() if found else ""
        check("the desktop banner says exactly what the browser's says",
              banner.replace("{seconds:,.1f}", "SECONDS")
              == "HISTORY  -SECONDSs  (" + HINT + ")",
              banner or "no banner found in ui/monitor.py")

    # ----------------------------------------------------------------------
    # Execute the served state machine. Everything below runs the slices taken
    # out of the page the server just handed out.
    # ----------------------------------------------------------------------
    pieces = {
        "state": line_source(script, "var state = "),
        "liveSeries": line_source(script, "var liveSeries = "),
        "hist": line_source(script, "var hist = "),
        "LIVE_SECONDS": line_source(script, "var LIVE_SECONDS = "),
        "MAX_LIVE_POINTS": line_source(script, "var MAX_LIVE_POINTS = "),
        "WHEEL_SAMPLES": line_source(script, "var WHEEL_SAMPLES = "),
        "sampleHz": block_source(script, "function sampleHz("),
        "historySeconds": block_source(script, "function historySeconds("),
        "historyLimit": block_source(script, "function historyLimit("),
        "setOffset": block_source(script, "function setOffset("),
        "panSamples": block_source(script, "function panSamples("),
        "resumeLive": block_source(script, "function resumeLive("),
        "applyHistoryView": block_source(script, "function applyHistoryView("),
        "updateLiveWindows": block_source(script, "function updateLiveWindows("),
        "windowShift": block_source(script, "function windowShift("),
        "renderLiveValues": block_source(script, "function renderLiveValues("),
        "viewNewestT": block_source(script, "function viewNewestT("),
        "followBuffer": block_source(script, "function followBuffer("),
        "attachHistoryPan": block_source(script, "function attachHistoryPan("),
        "applySample": block_source(script, "function applySample("),
        "applyHistory": block_source(script, "function applyHistory("),
        "wireControls": block_source(script, "function wireControls("),
        "keydown": block_source(script, "document.addEventListener('keydown',"),
    }
    print("\n[6] the served script, sliced up ready to run")
    broken = [name for name, text in pieces.items()
              if not text or "\nfunction " in text]
    check("every piece of the state machine came out of the page intact",
          not broken, ", ".join(broken))
    check("the slices are the served text, not a transcription",
          pieces["followBuffer"] in script
          and pieces["applySample"] in script)

    node = shutil.which("node")
    if not node or broken:
        print("    note: node is not on PATH, so the extracted state machine was")
        print("          not executed; the checks above are source-level only.")
    else:
        status, sample = get_json("/api/sample")
        hz = (sample.get("status") or {}).get("sample_hz")
        check("the server reports the sampler rate the view divides by",
              isinstance(hz, (int, float)) and hz > 0, f"sample_hz={hz!r}")
        build = [
            STUBS,
            pieces["state"], pieces["liveSeries"], pieces["hist"],
            pieces["LIVE_SECONDS"], pieces["MAX_LIVE_POINTS"],
            pieces["WHEEL_SAMPLES"],
            pieces["sampleHz"], pieces["historySeconds"], pieces["historyLimit"],
            pieces["setOffset"], pieces["panSamples"], pieces["resumeLive"],
            pieces["applyHistoryView"], pieces["updateLiveWindows"],
            pieces["windowShift"], pieces["renderLiveValues"],
            pieces["viewNewestT"], pieces["followBuffer"],
            pieces["attachHistoryPan"], pieces["applySample"],
            pieces["applyHistory"], pieces["wireControls"], pieces["keydown"],
            SETUP.replace("@@HZ@@", repr(float(hz))).replace("@@HINT@@", HINT),
            CHECKS,
        ]
        with open(HARNESS, "w", encoding="utf-8") as fh:
            fh.write("\n".join(build))
        print(f"\n[7] executing it (node {HARNESS})")
        try:
            run = subprocess.run([node, HARNESS], capture_output=True, text=True,
                                 timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            run = None
            print(f"    note: could not run node: {exc!r}")
            check("node ran the extracted state machine", False, repr(exc))
        if run is not None:
            for out_line in run.stdout.splitlines():
                print(out_line)
            if run.stderr.strip():
                print(f"    node stderr: {run.stderr.strip()[:400]}")
            check("the executed state machine behaved exactly as the desktop does",
                  run.returncode == 0 and "NODE FAILURES 0" in run.stdout,
                  f"exit {run.returncode}, "
                  f"{'no failure line' if 'NODE FAILURES' not in run.stdout else 'see above'}")

    print("\n[8] the offset is view state: nothing about it crosses the wire")
    status, sample = get_json("/api/sample")
    check("the sample payload carries no view state",
          set(sample) == {"wall", "t", "wall_offset", "values", "status",
                          "active_alarms"},
          ", ".join(sorted(sample)))
    check("no request in the script carries an offset",
          "offset=" not in script and "/api/history?seconds=" in script)
    check("no route was added for the view state",
          set(WEB._ROUTES) == ROUTES,
          f"{len(WEB._ROUTES)} routes")
    first = get_json("/api/history?seconds=60")[1].get("series") or {}
    first_t = (first.get("cpu_util") or [[0, 0]])[-1][0]
    time.sleep(2.5)
    second = get_json("/api/history?seconds=60")[1].get("series") or {}
    second_points = second.get("cpu_util") or []
    check("the sampler keeps producing while a client sits panned",
          bool(second_points) and second_points[-1][0] > first_t,
          f"t {first_t} -> {second_points[-1][0] if second_points else None}")
    check("and /api/history keeps serving the whole window",
          len(second_points) > len(first.get("cpu_util") or []),
          f"{len(first.get('cpu_util') or [])} -> {len(second_points)} points")
    status, other = get_json("/api/history?seconds=60&offset=99")
    check("an offset parameter is not a thing the server accepts",
          status == 200 and set(other) == {"seconds", "series"},
          f"status {status}, keys {sorted(other)}")
finally:
    print("\n[9] shutting down")
    web.stop()
    sampler.stop()
    manager.close()
    store.close()
    shutil.rmtree(WORK, ignore_errors=True)
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
    print(f"WEB GUI LIVE HISTORY TEST FAILED ({len(problems)})")
    for item in problems:
        print(f"  - {item}")
else:
    print("WEB GUI LIVE HISTORY TEST PASSED")
print("=" * 88)
raise SystemExit(1 if problems else 0)
