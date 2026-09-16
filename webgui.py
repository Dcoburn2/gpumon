"""Browser front-end for the gpumon web mode, as plain strings.

Kept apart from `webserver.py` so the server stays readable: this module is
assets, not logic. There is no build step, no CDN and no charting library - the
traces are drawn with hand-written JavaScript on `<canvas>`, and the page has to
work on an air-gapped benchmark machine.

The colours are not constants here. `ui/themes.py` owns the palette and the
`:root` block below is rendered from `themes.current()` on every request, so the
browser follows `--theme` like the desktop window and the terminal do - one
colour list, three front ends. The JavaScript reads the same variables back out
of the document (`cssVar`), which is what lets the canvas traces follow too.
"""
from __future__ import annotations

import html

from ui import themes

CSS = """
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0; padding: 12px; background: var(--bg); color: var(--text);
  font: 13px/1.45 var(--mono);
}
h1 { font-size: 16px; margin: 0; color: var(--bright); }
h2 { font-size: 14px; margin: 0; color: var(--bright); }
h3 { font-size: 12px; margin: 14px 0 6px; color: var(--dim);
     text-transform: uppercase; letter-spacing: .08em; }
a { color: var(--accent); }
.dim { color: var(--dim); }
.skip {
  position: absolute; left: -9999px; top: 0; background: var(--accent);
  color: #06121c; padding: 6px 10px; z-index: 10;
}
.skip:focus { left: 8px; }
.panel {
  background: var(--panel); border: 1px solid var(--border); border-radius: 4px;
  padding: 10px 12px; margin-bottom: 12px;
}
.bar { display: flex; align-items: center; gap: 10px; }
.bar.wrap { flex-wrap: wrap; }
.bar + .bar { margin-top: 8px; }
.lbl { color: var(--dim); }
button, select {
  font: 12px var(--mono); color: var(--text); background: var(--panel-alt);
  border: 1px solid var(--border); border-radius: 3px; padding: 5px 10px;
}
button { cursor: pointer; }
button:hover, select:hover { border-color: var(--accent); color: var(--bright); }
button:active { background: #172436; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.badge {
  padding: 2px 8px; border-radius: 10px; border: 1px solid var(--border);
  font-size: 11px;
}
.badge.ok { color: var(--ok); border-color: #1d4a30; }
.badge.warn { color: var(--warn); border-color: #4a3a1d; }
.badge.crit { color: var(--crit); border-color: #4a1d1d; }
.badge.dim { color: var(--dim); }
.card {
  background: var(--panel); border: 1px solid var(--border); border-radius: 4px;
  margin-bottom: 10px;
}
.card > header {
  display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
  padding: 8px 12px; border-bottom: 1px solid var(--border);
  background: var(--panel-alt); border-radius: 4px 4px 0 0;
}
.card .name { color: var(--bright); font-weight: bold; }
.card .meta { color: var(--dim); font-size: 11px; }
.card .body { padding: 6px 12px 10px; }
.row {
  display: grid; grid-template-columns: 56px 122px minmax(120px, 1fr);
  gap: 8px; align-items: center; padding: 2px 0;
}
.rlabel { color: var(--dim); font-size: 11px; }
.rvalue { color: var(--text); font-size: 13px; }
.chipset { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 12px 8px; }
.chip { font-size: 11px; padding: 2px 8px; border-radius: 10px;
        border: 1px solid var(--border); }
.chip.warning { color: var(--warn); border-color: #4a3a1d; }
.chip.critical { color: var(--crit); border-color: #4a1d1d; }
/* The live/history indicator and its banner. While the live charts are panned
   they take the palette's warning colour - chip, banner and every graph frame -
   which is the signal the desktop gives by tinting the Sparkline frame
   (ui/widgets.py) and colouring its own strip label. */
button.chip { font: 11px var(--mono); cursor: pointer; }
.chip.live { color: var(--ok); }
.chip.panned { color: var(--warn); border-color: var(--warn); }
.banner {
  font-size: 11px; padding: 2px 8px; border-radius: 3px;
  background: var(--warn); color: var(--plot-bg);
}
.banner[hidden] { display: none; }
/* Sticky so the indicator is still on screen while a chart further down the page
   is panned; the panel background keeps the charts from showing through it. */
#historybar { position: sticky; top: 0; z-index: 6; }
canvas.graph {
  display: block; width: 100%; height: 48px; background: var(--panel-alt);
  border: 1px solid var(--border); border-radius: 3px; cursor: grab;
}
canvas.graph:focus { border-color: var(--accent); }
/* Panned, a graph frame is tinted whether or not it holds focus: a chart showing
   history must never look like a live one. Summary charts are left alone - they
   drag out a time range instead of panning. */
body.panned canvas.graph, body.panned canvas.graph:focus {
  border-color: var(--warn);
}
canvas.chart {
  display: block; width: 100%; height: 190px; background: var(--panel-alt);
  border: 1px solid var(--border); border-radius: 3px; cursor: crosshair;
}
canvas.chart:focus { border-color: var(--accent); }
.chartwrap { margin: 10px 0 4px; }
.chartwrap h4 { margin: 0 0 4px; font-size: 12px; color: var(--text); }
.legend { display: flex; flex-wrap: wrap; gap: 12px; font-size: 11px;
          color: var(--dim); margin-top: 3px; }
.legend i { display: inline-block; width: 9px; height: 9px; border-radius: 2px;
            margin-right: 4px; }
.tablewrap { overflow-x: auto; }
table { border-collapse: collapse; font-size: 12px; }
th, td { padding: 3px 10px 3px 0; text-align: right; white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
thead th { color: var(--dim); border-bottom: 1px solid var(--border);
           font-weight: normal; text-transform: uppercase; font-size: 11px; }
tbody tr:hover { background: var(--panel-alt); }
.list { font-size: 12px; }
.list div { padding: 2px 0; border-bottom: 1px solid #16202c; }
.hint { color: var(--dim); font-size: 11px; margin: 6px 0 0; }
footer#footer {
  display: flex; flex-wrap: wrap; gap: 16px; color: var(--dim); font-size: 11px;
}
.toast {
  position: fixed; right: 14px; bottom: 14px; max-width: 380px;
  background: var(--panel); border: 1px solid var(--accent); color: var(--text);
  padding: 8px 12px; border-radius: 4px; font-size: 12px; opacity: 0;
  transition: opacity .15s ease; pointer-events: none;
}
.toast.show { opacity: 1; }
.toast.error { border-color: var(--crit); }
"""

#: The variable block, filled in per request. Only the variables are generated;
#: every rule above stays as written, because they were already written against
#: these names.
_ROOT_VARS = """
:root {{
  --bg: {bg}; --panel: {panel}; --panel-alt: {panel_alt}; --border: {border};
  --text: {text}; --dim: {dim}; --bright: {bright};
  --accent: {accent}; --ok: {ok}; --warn: {warn}; --crit: {crit};
  --idle: {idle}; --selection: {selection}; --plot-bg: {plot_bg};
  --grid: {grid};
{series}
  --mono: ui-monospace, "Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace;
}}
"""


def _blend(first: str, second: str, weight: float) -> str:
    """`weight` of `second` mixed into `first`; both "#rrggbb".

    Only used for the chart gridlines. No palette carries a grid entry - the
    desktop draws its grid by dimming the border - so the browser derives one
    the same way, half-way between the border and the panel it is drawn on.
    """
    try:
        a = [int(first[1:][i:i + 2], 16) for i in (0, 2, 4)]
        b = [int(second[1:][i:i + 2], 16) for i in (0, 2, 4)]
    except (ValueError, IndexError):
        return first
    return "#" + "".join(f"{round(x + (y - x) * weight):02x}"
                         for x, y in zip(a, b))


def _root_block(palette: themes.Palette) -> str:
    """The `:root` custom properties for `palette`.

    `--series-N` is what the charts colour their traces with; the count varies
    with the theme, and the JavaScript stops reading at the first missing one.
    """
    series = "".join(f"  --series-{i}: {colour};\n"
                     for i, colour in enumerate(palette.series))
    return _ROOT_VARS.format(
        bg=palette.bg, panel=palette.panel, panel_alt=palette.panel_alt,
        border=palette.border, text=palette.text, dim=palette.text_dim,
        bright=palette.text_bright, accent=palette.accent, ok=palette.ok,
        warn=palette.warn, crit=palette.crit, idle=palette.idle,
        selection=palette.selection, plot_bg=palette.plot_bg,
        grid=_blend(palette.border, palette.panel_alt, 0.5),
        series=series)


def stylesheet() -> str:
    """The whole stylesheet: the active palette, then the fixed rules."""
    return _root_block(themes.current()) + CSS


#: BODY carries this marker where the live theme menu belongs. `page()` swaps it
#: for `theme_picker()`, because which option is selected changes at runtime.
PICKER_SLOT = "<!--theme-picker-->"


def theme_picker() -> str:
    """The theme menu, rendered server-side from `themes.catalog()`.

    Rendered rather than fetched so the control exists - and already shows the
    active theme - in the first paint, before any script has run.
    """
    palette = themes.current()
    options = []
    for key, label, blurb in themes.catalog():
        chosen = " selected" if key == palette.key else ""
        options.append(
            f'<option value="{html.escape(key, quote=True)}"{chosen} '
            f'title="{html.escape(blurb, quote=True)}">'
            f"{html.escape(label)}</option>")
    return (
        '<div class="bar wrap">\n'
        '    <label class="lbl" for="theme-select">Theme</label>\n'
        '    <select id="theme-select">'
        f'{"".join(options)}</select>\n'
        f'    <span class="dim">{html.escape(palette.blurb)}</span>\n'
        "  </div>"
    )


BODY = """
<a class="skip" href="#live">Skip to live data</a>
<header class="panel">
  <div class="bar">
    <h1>gpumon <span class="dim" id="version"></span></h1>
    <span id="conn" class="badge dim">connecting</span>
  </div>
  <div class="bar wrap">
    <button id="btn-log" type="button">Start logging</button>
    <button id="btn-mark" type="button">Mark</button>
    <label class="lbl" for="session-select">Session</label>
    <select id="session-select"><option value="">newest session</option></select>
    <button id="btn-reload" type="button">Reload summary</button>
  </div>
  <div class="bar wrap"><span id="logging-state" class="dim"></span></div>
  <!--theme-picker-->
  <p id="headline" class="dim"></p>
  <div id="alarmbar" class="chipset"></div>
</header>
<!-- The live/history indicator, sticky under the controls: a panned chart three
     screens down must still show which way back to live is, the way the desktop
     window's status strip always does. Rendered as live here - offset 0, banner
     hidden - so the first paint is already correct, and the script below only
     changes it when a chart is actually panned. -->
<div id="historybar" class="bar wrap panel">
  <button id="hist-chip" class="chip live" type="button"
          title="following the newest samples">LIVE</button>
  <span id="hist-banner" class="banner" role="status" hidden></span>
  <span class="dim">drag or shift+wheel to pan, P to resume live</span>
</div>
<main>
  <section id="live" aria-label="Live telemetry">
    <!-- Placeholder replaced by the first /api/state response. It exists so the
         page has real markup (and a real canvas) before any script runs. -->
    <section class="card">
      <header><span class="name">connecting to the sampler</span></header>
      <div class="body"><div class="row">
        <span class="rlabel">--</span><span class="rvalue">--</span>
        <canvas class="graph" width="400" height="60"
                aria-label="waiting for samples"></canvas>
      </div></div>
    </section>
  </section>
  <section id="summary" class="panel" aria-label="Session summary">
    <div class="bar wrap">
      <h2>Summary</h2>
      <button id="preset-50" type="button">LAST 50%</button>
      <button id="preset-25" type="button">LAST 25%</button>
      <button id="preset-full" type="button">FULL RUN</button>
      <span id="range-label" class="dim">whole run</span>
    </div>
    <div id="tabs" class="bar wrap" role="tablist"></div>
    <p class="hint">Drag left to right across any chart to select that time
      range; the table below follows the selection. With the keyboard, Tab to a
      preset above and press Enter.</p>
    <div id="charts"></div>
    <h3>Statistics</h3>
    <div class="tablewrap"><table id="statstable"></table></div>
    <h3>Alarms</h3>
    <div id="alarm-list" class="list"></div>
    <h3>Markers</h3>
    <div id="marker-list" class="list"></div>
    <p id="summary-note" class="dim"></p>
  </section>
</main>
<footer id="footer" class="panel">
  <span id="f-rate">rate --</span>
  <span id="f-poll">poll --</span>
  <span id="f-dropped">dropped --</span>
  <span id="f-queue">queue --</span>
  <span id="f-errors"></span>
  <span id="f-db"></span>
</footer>
<div id="toast" class="toast" role="status" aria-live="polite"></div>
"""

JS = """
/* gpumon web front end: hand-written, no framework, no build step, no CDN.
   The browser draws every trace itself on <canvas> and never touches hardware:
   /api/sample is the sampler's own latest merged sample, /api/history is its
   live ring buffer, and everything else comes out of SQLite. */
(function () {
'use strict';

var POLL_MS = 500;          /* matches the desktop render cadence */
var RESYNC_EVERY = 4;       /* refetch /api/history every 4th poll (2 s) */
var LIVE_SECONDS = 60;      /* ui/monitor.py SPARK_SECONDS */
var WHEEL_SAMPLES = 5;      /* samples per shift+wheel notch, as on the desktop */
var MAX_LIVE_POINTS = 400;
var FONT = '11px ui-monospace, Consolas, monospace';

/* Every colour below is read out of the stylesheet's custom properties at boot,
   which is how the traces follow the active theme: `ui/themes.py` writes the
   `:root` block, the canvas reads it back. Nothing here may hardcode a hex - a
   theme with its own palette would otherwise leave nvtop-blue graphs on it. */
var C = {};
var SERIES = [];
var BAND_COLOR = {};

function cssVar(name) {
  var value = getComputedStyle(document.documentElement)
    .getPropertyValue(name);
  return (value || '').trim();
}

/* Canvas needs rgba() for the translucent fills; the palette is #rrggbb. */
function alpha(color, opacity) {
  var hex = String(color || '').replace('#', '');
  if (hex.length === 3) {
    hex = hex.charAt(0) + hex.charAt(0) + hex.charAt(1) + hex.charAt(1) +
          hex.charAt(2) + hex.charAt(2);
  }
  var n = parseInt(hex, 16);
  if (hex.length !== 6 || isNaN(n)) { return color; }
  return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' +
         (n & 255) + ',' + opacity + ')';
}

function readTheme() {
  C = {bg: cssVar('--bg'), panel: cssVar('--panel'),
       alt: cssVar('--panel-alt'), border: cssVar('--border'),
       text: cssVar('--text'), dim: cssVar('--dim'), bright: cssVar('--bright'),
       accent: cssVar('--accent'), ok: cssVar('--ok'), warn: cssVar('--warn'),
       crit: cssVar('--crit'), grid: cssVar('--grid'),
       selection: cssVar('--selection'), plotBg: cssVar('--plot-bg')};
  SERIES = [];
  for (var i = 0; i < 32; i++) {
    var colour = cssVar('--series-' + i);
    if (!colour) { break; }
    SERIES.push(colour);
  }
  if (!SERIES.length) { SERIES = [C.accent]; }
  /* The warning zones behind the graphs. Same meanings as BAND_COLOR in
     ui/monitor.py, tinted from this theme's own alarm colours. */
  BAND_COLOR = {temp: alpha(C.crit, 0.10), util: alpha(C.ok, 0.10),
                vram_percent: alpha(C.warn, 0.10),
                ram_percent: alpha(C.warn, 0.10)};
}

/* Mirror of ui/monitor.py: GPU_ROW_FIELDS / FIELD_BANDS / FIELD_THRESHOLDS.
   Same ranges, same warning zones, same threshold lines as the desktop cards,
   so a glance at either front end reads the same. */
var GPU_ROWS = [
  {field: 'temp', label: 'TEMP', min: 0, max: 110, unit: 'C', band: [80, 110],
   thresh: [80]},
  {field: 'util', label: 'GPU', min: 0, max: 100, unit: '%', band: [90, 100],
   thresh: []},
  {field: 'vram_percent', label: 'VRAM', min: 0, max: 100, unit: '%',
   band: [90, 100], thresh: [95]},
  {field: 'clock_core', label: 'CORE', min: 0, max: 2500, unit: 'MHz',
   band: null, thresh: []},
  {field: 'power_percent', label: 'PWR', min: 0, max: 100, unit: '%',
   band: null, thresh: [95]}
];
var CPU_ROWS = [
  {key: 'cpu_util', label: 'CPU', min: 0, max: 100, unit: '%', band: [90, 100],
   thresh: []},
  {key: 'cpu_temp', label: 'TEMP', min: 0, max: 110, unit: 'C', band: [80, 110],
   thresh: [85]},
  {key: 'ram_percent', label: 'RAM', min: 0, max: 100, unit: '%',
   band: [90, 100], thresh: [90]},
  {key: 'cpu_clock', label: 'CLOCK', min: 0, max: 5000, unit: 'MHz',
   band: null, thresh: []},
  {key: 'cpu_power', label: 'PWR', min: 0, max: 250, unit: 'W', band: null,
   thresh: []}
];
/* Rows always shown even before their metric has produced a sample: without
   them an idle machine would render a card with no graphs at all. */
var PRIMARY = {temp: 1, util: 1, vram_percent: 1, cpu_util: 1, ram_percent: 1};

var state = {catalogue: {}, devices: [], status: null, wallOffset: 0,
             logging: false, sessionId: null, db: '', theme: ''};
/* The shared pan position of the live charts: how many samples the right edge of
   the view sits behind the newest one (0 = live). One offset for every chart, as
   in the desktop window - the question being answered is "what was everything
   doing when the temperature spiked", and a per-chart scroll could not line that
   up. It is view state and nothing else: no request carries it, so it cannot
   change what is recorded or what /api/sample answers. `newestT` is the anchor
   that keeps the view on the same history while samples keep arriving. */
var hist = {offset: 0, newestT: null};
var liveSeries = {};
var summary = {data: null, tabs: [], tab: 0, window: null, span: null,
               series: {}, chartSpecs: [], fullStats: null, full: null};
var pollTimer = null;
var pollCount = 0;
var failures = 0;
var drawPending = false;

/* ------------------------------------------------------------------ dom */
function el(tag, cls, text) {
  var node = document.createElement(tag);
  if (cls) { node.className = cls; }
  if (text !== undefined && text !== null) { node.textContent = String(text); }
  return node;
}
function byId(id) { return document.getElementById(id); }
function setText(id, text) {
  var node = byId(id);
  if (node && node.textContent !== text) { node.textContent = text; }
}
function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }

/* ------------------------------------------------------------ formatting */
function fmt(value, precision) {
  if (value === null || value === undefined || isNaN(value)) { return '--'; }
  return Number(value).toFixed(precision || 0);
}
function duration(seconds) {
  if (!isFinite(seconds)) { return '--'; }
  seconds = Math.max(0, seconds);
  if (seconds < 60) { return seconds.toFixed(1) + 's'; }
  var m = Math.floor(seconds / 60), s = seconds - m * 60;
  if (m < 60) { return m + 'm ' + (s < 10 ? '0' : '') + s.toFixed(0) + 's'; }
  var h = Math.floor(m / 60); m = m - h * 60;
  return h + 'h ' + (m < 10 ? '0' : '') + m + 'm';
}
function clock(unix) {
  try { return new Date(unix * 1000).toLocaleTimeString(); }
  catch (err) { return '--'; }
}

/* Colour semantics copied from ui/monitor.py so a value that is amber on the
   desktop is amber in the browser too. */
function fieldColor(field) {
  if (field === 'temp' || field === 'hotspot' || field === 'mem_temp' ||
      field === 'cpu_temp') { return C.crit; }
  if (field === 'util' || field === 'cpu_util') { return C.accent; }
  if (field === 'vram_percent' || field === 'ram_percent') { return SERIES[2]; }
  if (field === 'clock_core' || field === 'clock_sm' || field === 'clock_mem' ||
      field === 'cpu_clock') { return SERIES[1]; }
  if (field === 'power_percent') { return SERIES[3]; }
  return SERIES[4];
}
function loadColor(value) {
  if (value === null || value === undefined || isNaN(value)) { return C.dim; }
  if (value >= 95) { return C.crit; }
  if (value >= 80) { return C.warn; }
  if (value >= 40) { return C.accent; }
  return C.ok;
}
function tempColor(value, limit) {
  if (value === null || value === undefined || isNaN(value)) { return C.dim; }
  if (limit) {
    if (value >= limit - 2) { return C.crit; }
    if (value >= limit - 10) { return C.warn; }
  } else {
    if (value >= 90) { return C.crit; }
    if (value >= 80) { return C.warn; }
  }
  if (value >= 60) { return C.accent; }
  return C.ok;
}
function valueColor(key, value, meta) {
  if (!meta) { return C.text; }
  if (meta.kind === 'temp') { return tempColor(value, meta.limit); }
  if (meta.kind === 'percent' || meta.key === 'power_percent') {
    return loadColor(value);
  }
  return C.text;
}

/* --------------------------------------------------------------- network */
function api(path, options) {
  return fetch(path, options).then(function (res) {
    if (!res.ok) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        throw new Error(body.error || (res.status + ' ' + res.statusText));
      });
    }
    return res.json();
  });
}
function postJSON(path, payload) {
  return api(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload || {})});
}

var toastTimer = null;
function toast(message, isError) {
  var node = byId('toast');
  node.textContent = message;
  node.className = 'toast show' + (isError ? ' error' : '');
  if (toastTimer) { clearTimeout(toastTimer); }
  toastTimer = setTimeout(function () { node.className = 'toast'; }, 4000);
}

function setConn(ok, detail) {
  var node = byId('conn');
  if (ok) {
    node.className = 'badge ok';
    node.textContent = 'connected';
  } else {
    node.className = 'badge crit';
    node.textContent = 'connection lost' + (detail ? ': ' + detail : '');
  }
}

/* ----------------------------------------------------------------- charts */
function ensureSize(canvas) {
  var dpr = window.devicePixelRatio || 1;
  var w = Math.max(60, Math.round(canvas.clientWidth || 300));
  var h = Math.max(40, Math.round(canvas.clientHeight || 48));
  if (canvas.width !== Math.round(w * dpr) ||
      canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  var ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return {ctx: ctx, w: w, h: h};
}

function nearestIndex(points, t) {
  if (!points.length) { return -1; }
  var lo = 0, hi = points.length - 1;
  while (hi - lo > 1) {
    var mid = (lo + hi) >> 1;
    if (points[mid][0] < t) { lo = mid; } else { hi = mid; }
  }
  return Math.abs(points[lo][0] - t) <= Math.abs(points[hi][0] - t) ? lo : hi;
}

/* Min/max decimation: a bucket keeps both its smallest and its largest sample
   so a one-sample thermal spike survives being drawn into a 1000 px canvas. */
function decimate(points, width) {
  if (points.length <= width * 2) { return points; }
  var out = [], bucket = points.length / width;
  for (var i = 0; i < width; i++) {
    var a = Math.floor(i * bucket);
    var b = Math.min(points.length, Math.floor((i + 1) * bucket));
    if (b <= a) { continue; }
    var lo = a, hi = a;
    for (var j = a; j < b; j++) {
      if (points[j][1] < points[lo][1]) { lo = j; }
      if (points[j][1] > points[hi][1]) { hi = j; }
    }
    var first = Math.min(lo, hi), second = Math.max(lo, hi);
    out.push(points[first]);
    if (second !== first) { out.push(points[second]); }
  }
  return out;
}

/* The samples that fall inside the drawn window. The desktop skips samples
   outside the view the same way (Sparkline._draw_series), and while a chart is
   panned this is what stops the newer samples - the ones the view has scrolled
   away from - from being drawn across the plot. */
function windowPoints(points, tmin, tmax) {
  /* The slack is for the summary charts, whose sample times come back from the
     server rounded to milliseconds while the window edges are not: without it a
     sample sitting exactly on an edge could be dropped. It is far below a pixel
     on any window this page draws. */
  var slack = (tmax - tmin) * 1e-6 + 0.0005;
  var out = [];
  for (var i = 0; i < points.length; i++) {
    if (points[i][0] >= tmin - slack && points[i][0] <= tmax + slack) {
      out.push(points[i]);
    }
  }
  return out;
}

function timeLabel(spec, t) {
  var parts = [];
  if (spec.clockBase !== null && spec.clockBase !== undefined) {
    parts.push(clock(spec.clockBase + t));
  }
  if (spec.relative) { parts.push('+' + t.toFixed(1) + 's'); }
  return parts.length ? parts.join('  ') : t.toFixed(1) + 's';
}

function drawTooltip(ctx, w, h, lines, px, py) {
  ctx.font = FONT;
  var wid = 0;
  for (var i = 0; i < lines.length; i++) {
    wid = Math.max(wid, ctx.measureText(lines[i]).width);
  }
  var bw = wid + 14, bh = lines.length * 14 + 8;
  var bx = px + 12;
  if (bx + bw > w - 2) { bx = px - bw - 12; }
  bx = Math.max(2, Math.min(bx, w - bw - 2));
  var by = Math.max(2, Math.min(h - bh - 2, py - bh - 10));
  ctx.fillStyle = alpha(C.plotBg, 0.95);
  ctx.strokeStyle = C.border;
  ctx.lineWidth = 1;
  ctx.fillRect(bx, by, bw, bh);
  ctx.strokeRect(bx + 0.5, by + 0.5, bw - 1, bh - 1);
  ctx.textAlign = 'left';
  ctx.textBaseline = 'top';
  for (var k = 0; k < lines.length; k++) {
    ctx.fillStyle = k === 0 ? C.dim : C.text;
    ctx.fillText(lines[k], bx + 7, by + 5 + k * 14);
  }
}

/* The one renderer behind both the live rows and the summary charts.
   spec: {lines: [{label, color, points}], vmin, vmax, tmin, tmax, axis, live,
          unit, precision, band, thresh, selection, alarms, markers,
          clockBase, relative, hoverT, pinned, showPeak, showCurrent} */
function drawChart(canvas) {
  var spec = canvas.__spec;
  if (!spec) { return; }
  var g = ensureSize(canvas);
  var ctx = g.ctx, w = g.w, h = g.h;
  ctx.clearRect(0, 0, w, h);
  var axis = !!spec.axis;
  var left = axis ? 54 : 4, right = axis ? 6 : 4;
  var top = 5, bottom = axis ? 17 : 4;
  var x0 = left, y0 = top, x1 = w - right, y1 = h - bottom;
  var bw = Math.max(8, x1 - x0), bh = Math.max(8, y1 - y0);
  var tmin = spec.tmin, tmax = spec.tmax;
  if (!(tmax > tmin)) { tmax = tmin + 1; }
  /* Only the window is drawn, and everything below - the scale, the peak, the
     current dot, the hover snap - reads the window rather than the whole buffer,
     so a panned chart describes the history on screen and not the live edge. */
  var vis = [];
  for (var vi = 0; vi < spec.lines.length; vi++) {
    vis.push(windowPoints(spec.lines[vi].points, tmin, tmax));
  }
  var vmin = spec.vmin === null || spec.vmin === undefined ? 0 : spec.vmin;
  var vmax = spec.vmax;
  if (vmax === null || vmax === undefined) {
    var hi = -Infinity;
    for (var n = 0; n < vis.length; n++) {
      for (var p = 0; p < vis[n].length; p++) {
        if (vis[n][p][1] > hi) { hi = vis[n][p][1]; }
      }
    }
    if (!isFinite(hi)) { hi = 1; }
    vmax = Math.max(vmin + 1, hi * 1.12);
  }
  if (!(vmax > vmin)) { vmax = vmin + 1; }
  function xOf(t) { return x0 + (t - tmin) / (tmax - tmin) * bw; }
  function yOf(v) { return y1 - (v - vmin) / (vmax - vmin) * bh; }

  /* shading first: bands, alarm spans and the selected window sit behind
     everything else so the trace always stays readable. */
  if (spec.band) {
    ctx.fillStyle = BAND_COLOR[spec.field] || alpha(C.warn, 0.10);
    var byTop = yOf(spec.band[1]), byBottom = yOf(spec.band[0]);
    ctx.fillRect(x0, byTop, bw, byBottom - byTop);
  }
  if (spec.alarms && spec.alarms.length) {
    for (var a = 0; a < spec.alarms.length; a++) {
      var alarm = spec.alarms[a];
      var ax0 = Math.max(x0, xOf(alarm.started_t));
      var endT = alarm.ended_t === null || alarm.ended_t === undefined
        ? tmax : alarm.ended_t;
      var ax1 = Math.min(x1, xOf(endT));
      if (ax1 <= ax0) { continue; }
      ctx.fillStyle = alarm.level === 'critical'
        ? alpha(C.crit, 0.16) : alpha(C.warn, 0.12);
      ctx.fillRect(ax0, y0, ax1 - ax0, bh);
    }
  }
  if (spec.selection) {
    var sx0 = Math.max(x0, xOf(Math.min(spec.selection[0], spec.selection[1])));
    var sx1 = Math.min(x1, xOf(Math.max(spec.selection[0], spec.selection[1])));
    if (sx1 > sx0) {
      ctx.fillStyle = C.selection;
      ctx.fillRect(sx0, y0, sx1 - sx0, bh);
      ctx.strokeStyle = C.accent;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(sx0 + 0.5, y0);
      ctx.lineTo(sx0 + 0.5, y1);
      ctx.moveTo(sx1 - 0.5, y0);
      ctx.lineTo(sx1 - 0.5, y1);
      ctx.stroke();
    }
  }

  /* grid and axis labels */
  ctx.strokeStyle = C.grid;
  ctx.lineWidth = 1;
  ctx.font = FONT;
  ctx.fillStyle = C.dim;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  var steps = axis ? 4 : 2;
  for (var i = 0; i <= steps; i++) {
    var value = vmin + (vmax - vmin) * (i / steps);
    var y = Math.round(yOf(value)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(x0, y);
    ctx.lineTo(x1, y);
    ctx.stroke();
    if (axis) {
      var digits = (vmax - vmin) >= 100 ? 0 : 1;
      ctx.fillText(value.toFixed(digits), x0 - 5, y);
    }
  }
  if (axis) {
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    var span = tmax - tmin;
    for (var k = 0; k <= 4; k++) {
      var tt = tmin + span * (k / 4);
      ctx.fillText(timeLabel(spec, tt).split('  ').pop(), xOf(tt), y1 + 4);
    }
  }

  /* dotted alarm thresholds (FIELD_THRESHOLDS on the desktop) */
  if (spec.thresh && spec.thresh.length) {
    ctx.save();
    ctx.setLineDash([4, 3]);
    ctx.strokeStyle = C.warn;
    for (var s = 0; s < spec.thresh.length; s++) {
      var th = spec.thresh[s];
      if (th < vmin || th > vmax) { continue; }
      var ty = Math.round(yOf(th)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(x0, ty);
      ctx.lineTo(x1, ty);
      ctx.stroke();
    }
    ctx.restore();
  }

  /* markers dropped during the run */
  if (spec.markers && spec.markers.length) {
    ctx.save();
    ctx.setLineDash([2, 3]);
    ctx.strokeStyle = C.dim;
    for (var m = 0; m < spec.markers.length; m++) {
      var mt = spec.markers[m].t;
      if (mt < tmin || mt > tmax) { continue; }
      var mx = Math.round(xOf(mt)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(mx, y0);
      ctx.lineTo(mx, y1);
      ctx.stroke();
      if (axis) {
        ctx.fillStyle = C.dim;
        ctx.textAlign = 'left';
        ctx.textBaseline = 'top';
        ctx.fillText(spec.markers[m].label.slice(0, 14), mx + 3, y0 + 2);
      }
    }
    ctx.restore();
  }

  /* the traces */
  var pxPerPoint = bw / Math.max(1, tmax - tmin);
  var decimateWidth = Math.max(32, Math.round(bw));
  for (var li = 0; li < spec.lines.length; li++) {
    var line = spec.lines[li];
    var data = decimate(vis[li], decimateWidth);
    if (data.length < 1) { continue; }
    ctx.strokeStyle = line.color;
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    for (var di = 0; di < data.length; di++) {
      var dx = xOf(data[di][0]), dy = yOf(data[di][1]);
      if (di === 0) { ctx.moveTo(dx, dy); } else { ctx.lineTo(dx, dy); }
    }
    ctx.stroke();
    if (axis && data.length > 1) {
      var grad = ctx.createLinearGradient(0, y0, 0, y1);
      grad.addColorStop(0, line.color + '33');
      grad.addColorStop(1, line.color + '05');
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.moveTo(xOf(data[0][0]), y1);
      for (var fi = 0; fi < data.length; fi++) {
        ctx.lineTo(xOf(data[fi][0]), yOf(data[fi][1]));
      }
      ctx.lineTo(xOf(data[data.length - 1][0]), y1);
      ctx.closePath();
      ctx.fill();
    }
    /* peak marker */
    if (spec.showPeak && vis[li].length > 1) {
      var peak = vis[li][0];
      for (var pi = 1; pi < vis[li].length; pi++) {
        if (vis[li][pi][1] > peak[1]) { peak = vis[li][pi]; }
      }
      var pkx = xOf(peak[0]), pky = yOf(peak[1]);
      ctx.fillStyle = line.color;
      ctx.beginPath();
      ctx.moveTo(pkx, pky - 5);
      ctx.lineTo(pkx - 3.5, pky - 0.5);
      ctx.lineTo(pkx + 3.5, pky - 0.5);
      ctx.closePath();
      ctx.fill();
      if (w > 260) {
        ctx.font = FONT;
        ctx.fillStyle = C.dim;
        ctx.textAlign = pkx > w / 2 ? 'right' : 'left';
        ctx.textBaseline = 'bottom';
        ctx.fillText(fmt(peak[1], spec.precision) + ' peak',
                     pkx + (pkx > w / 2 ? -6 : 6), pky - 6);
      }
    }
    /* current-value dot: the newest sample in the window, which while panned is
       the right edge of the view rather than the live sample */
    if (spec.showCurrent && vis[li].length) {
      var last = vis[li][vis[li].length - 1];
      ctx.fillStyle = line.color;
      ctx.beginPath();
      ctx.arc(xOf(last[0]), yOf(last[1]), 2.6, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  /* hover: crosshair plus a tooltip snapped to the nearest real sample */
  var hoverT = spec.hoverT;
  if (hoverT !== null && hoverT !== undefined && spec.lines.length) {
    var lines = [timeLabel(spec, hoverT)];
    var first = vis[0];
    var idx = nearestIndex(first, hoverT);
    var snapT = hoverT;
    if (idx >= 0) {
      snapT = first[idx][0];
      lines[0] = timeLabel(spec, snapT);
    }
    var crossX = Math.round(xOf(snapT)) + 0.5;
    ctx.save();
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = C.accent;
    ctx.beginPath();
    ctx.moveTo(crossX, y0);
    ctx.lineTo(crossX, y1);
    ctx.stroke();
    ctx.restore();
    var markerY = y1;
    for (var hi2 = 0; hi2 < spec.lines.length; hi2++) {
      var l2 = spec.lines[hi2];
      var jdx = nearestIndex(vis[hi2], snapT);
      if (jdx < 0) { lines.push(l2.label + ': --'); continue; }
      var pt = vis[hi2][jdx];
      var py = yOf(pt[1]);
      markerY = Math.min(markerY, py);
      ctx.fillStyle = l2.color;
      ctx.beginPath();
      ctx.arc(xOf(pt[0]), py, 3, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = C.bg;
      ctx.lineWidth = 1;
      ctx.stroke();
      lines.push(l2.label + ': ' + fmt(pt[1], spec.precision) +
                 (spec.unit ? ' ' + spec.unit : ''));
    }
    drawTooltip(ctx, w, h, lines, crossX, markerY);
  } else if (!vis.length || !vis[0].length) {
    ctx.font = FONT;
    ctx.fillStyle = C.dim;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    /* Panned off the end of the buffer there really is nothing in the window;
       "waiting for samples" would blame the sampler for the view's position. */
    ctx.fillText(spec.live && hist.offset ? 'no samples in this view'
                                          : 'waiting for samples', w / 2, h / 2);
  }
}

/* --------------------------------------------------------- chart plumbing */
function tAtX(canvas, clientX) {
  var spec = canvas.__spec;
  var rect = canvas.getBoundingClientRect();
  var axis = !!spec.axis;
  var x0 = axis ? 54 : 4, x1 = rect.width - (axis ? 6 : 4);
  var frac = (clientX - rect.left - x0) / Math.max(1, x1 - x0);
  frac = Math.max(0, Math.min(1, frac));
  return spec.tmin + frac * (spec.tmax - spec.tmin);
}

function attachHover(canvas) {
  canvas.addEventListener('mousemove', function (ev) {
    var spec = canvas.__spec;
    /* A drag owns the pointer: panning must not also drag the readout along. */
    if (!spec || canvas.__dragging || canvas.__panning) { return; }
    spec.hoverT = tAtX(canvas, ev.clientX);
    drawChart(canvas);
  });
  canvas.addEventListener('mouseleave', function () {
    var spec = canvas.__spec;
    if (!spec) { return; }
    if (!spec.pinned) { spec.hoverT = null; }
    drawChart(canvas);
  });
  /* Enter/Space "activate" a focused graph the same way they activate a
     button: they pin the readout so a keyboard user can keep it on screen.
     Arrow keys are deliberately left alone so they keep scrolling the page. */
  canvas.addEventListener('keydown', function (ev) {
    var spec = canvas.__spec;
    if (!spec) { return; }
    if (ev.key === 'Enter' || ev.key === ' ' || ev.key === 'Spacebar') {
      ev.preventDefault();
      spec.pinned = !spec.pinned;
      if (!spec.pinned) { spec.hoverT = null; }
      else if (spec.hoverT === null || spec.hoverT === undefined) {
        spec.hoverT = spec.tmax;
      }
      drawChart(canvas);
    }
  });
}

function scheduleDraw() {
  if (drawPending) { return; }
  drawPending = true;
  window.requestAnimationFrame(function () {
    drawPending = false;
    var key;
    for (key in liveSeries) {
      if (liveSeries.hasOwnProperty(key)) { drawChart(liveSeries[key].canvas); }
    }
    for (var i = 0; i < summary.chartSpecs.length; i++) {
      drawChart(summary.chartSpecs[i].canvas);
    }
  });
}

/* --------------------------------------------------------- live history */
/* Panning the live charts back through the ring buffer. `ui/monitor.py` does
   this in the desktop window (MonitorApp.pan_history and friends): one offset
   shared by every chart, drag right to walk back in time, shift+wheel to step,
   double-click or P to resume - and while panned the offset follows the buffer,
   so the history on screen does not creep back to live under the reader. */
function sampleHz() {
  /* The sampler's own rate, which is the rate the desktop divides by: the offset
     counts samples, while the banner and the window edges are times. */
  var hz = state.status && state.status.sample_hz;
  return hz > 0 ? hz : 0.0;
}
function historySeconds() {
  /* How far back the right edge sits, in seconds (Sparkline.history_seconds). */
  var hz = sampleHz();
  return hz ? hist.offset / hz : 0.0;
}
function historyLimit() {
  /* How far back the buffers reach, in samples - the desktop clamps to its own
     ring buffer the same way (MonitorApp._history_limit). The deepest series
     wins because one offset drives every chart. */
  var limit = 0;
  for (var key in liveSeries) {
    if (!liveSeries.hasOwnProperty(key)) { continue; }
    var count = liveSeries[key].points.length - 2;
    if (count > limit) { limit = count; }
  }
  return Math.max(0, limit);
}
function setOffset(offset) {
  /* Rounded rather than truncated: half a sample of drag should land on the
     nearer sample. The clamp is the buffer's end - there is no history beyond
     it to show. */
  var next = Math.max(0, Math.min(Math.round(offset), historyLimit()));
  if (next === hist.offset) { return; }
  hist.offset = next;
  applyHistoryView();
}
function panSamples(samples) { setOffset(hist.offset + samples); }
function resumeLive() { setOffset(0); }

function applyHistoryView() {
  /* Chip, banner and every graph frame follow the offset, so a view showing
     history can never be mistaken for a live one. The desktop's version of this
     is MonitorApp._render_history_state, which says the same words. */
  var panned = hist.offset > 0;
  var text = 'HISTORY  -' + historySeconds().toFixed(1) + 's';
  var chip = byId('hist-chip');
  var banner = byId('hist-banner');
  if (chip) {
    chip.textContent = panned ? text : 'LIVE';
    chip.className = 'chip ' + (panned ? 'panned' : 'live');
    chip.title = panned ? 'showing history: click to resume live'
                        : 'following the newest samples';
  }
  if (banner) {
    banner.textContent = text +
      '  (drag or shift+wheel to pan, P to resume live)';
    banner.hidden = !panned;
  }
  if (document.body) {
    if (panned) { document.body.classList.add('panned'); }
    else { document.body.classList.remove('panned'); }
  }
  updateLiveWindows();
  renderLiveValues();
  scheduleDraw();
}

function updateLiveWindows() {
  /* A fixed LIVE_SECONDS window per chart whose right edge is slid back by the
     shared offset: live, the newest sample sits on that edge. Indexing each
     series by the shared offset (rather than by time) is what the desktop does -
     every spark is told the same offset and slides its own buffer by it. */
  for (var key in liveSeries) {
    if (!liveSeries.hasOwnProperty(key)) { continue; }
    var entry = liveSeries[key];
    var points = entry.points;
    if (!points.length) { continue; }
    var index = points.length - 1 - hist.offset;
    /* A series shorter than the pan (a metric that stopped reporting) still gets
       a window moved the same distance, so the shared offset stays honest. */
    var edge = index >= 0 ? points[index][0]
                          : points[points.length - 1][0] - windowShift();
    entry.spec.tmax = edge;
    entry.spec.tmin = edge - LIVE_SECONDS;
  }
}

function windowShift() {
  return hist.offset / (sampleHz() || 1);
}

function renderLiveValues() {
  /* The row badge shows the value at the right edge of the view. Panned, that is
     not the live value but the sample the reader is looking at, picked exactly
     as Sparkline._draw_badge picks it: offset samples back from the newest. */
  for (var key in liveSeries) {
    if (!liveSeries.hasOwnProperty(key)) { continue; }
    var entry = liveSeries[key];
    var index = entry.points.length - 1 - hist.offset;
    setRowValue(entry, index >= 0 ? entry.points[index][1] : null);
  }
}

function viewNewestT() {
  /* The newest timestamp any live chart holds, or null before the first sample:
     the anchor the pan is measured against. */
  var newest = null;
  for (var key in liveSeries) {
    if (!liveSeries.hasOwnProperty(key)) { continue; }
    var points = liveSeries[key].points;
    if (!points.length) { continue; }
    var last = points[points.length - 1][0];
    if (newest === null || last > newest) { newest = last; }
  }
  return newest;
}

function followBuffer(newest) {
  /* While panned, grow the offset by as many samples as arrived, so the same
     history stays on screen - MonitorApp._apply_sample does this one sample at a
     time. Counted from the timestamps rather than "one per poll" because the
     page polls at a fixed 2 Hz while the sampler may run faster or slower: either
     mismatch would otherwise walk the window off the history the user paused on. */
  var previous = hist.newestT;
  hist.newestT = newest;
  if (!hist.offset || previous === null || newest === null ||
      newest <= previous) {
    return;
  }
  var gained = Math.round((newest - previous) * (sampleHz() || 1));
  if (gained > 0) { panSamples(gained); }
}

/* ------------------------------------------------------------ live view */
function makeCard(title, metaLines, chips) {
  var root = el('section', 'card');
  var head = el('header');
  head.appendChild(el('span', 'name', title));
  for (var i = 0; i < metaLines.length; i++) {
    head.appendChild(el('span', 'meta', metaLines[i]));
  }
  root.appendChild(head);
  if (chips && chips.length) {
    var chipset = el('div', 'chipset');
    for (var c = 0; c < chips.length; c++) {
      chipset.appendChild(el('span', 'chip ' + (chips[c].level || ''),
                             chips[c].text));
    }
    root.appendChild(chipset);
  }
  var body = el('div', 'body');
  root.appendChild(body);
  return {root: root, body: body};
}

function addLiveRow(body, key, label, spec) {
  var row = el('div', 'row');
  row.appendChild(el('span', 'rlabel', label));
  var value = el('span', 'rvalue', '--');
  row.appendChild(value);
  var canvas = el('canvas', 'graph');
  canvas.width = 400;
  canvas.height = 60;
  canvas.tabIndex = 0;
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label',
                      label + ' history, 60 seconds - drag to pan');
  var entry = {key: key, points: [], canvas: canvas, spec: spec, value: value,
               meta: spec.meta};
  spec.lines = [{label: label, color: spec.color, points: entry.points}];
  /* `live` marks this spec as one of the pannable graph rows: only they follow
     the shared offset and only they show "no samples in this view". */
  spec.live = true;
  canvas.__spec = spec;
  attachHover(canvas);
  attachHistoryPan(canvas);
  row.appendChild(canvas);
  body.appendChild(row);
  liveSeries[key] = entry;
  return entry;
}

/* The row badge. Split out because the live view writes it from two places: the
   arriving sample when live, and the right edge of the view when panned. */
function setRowValue(entry, value) {
  var meta = entry.meta || {};
  entry.value.textContent = fmt(value, meta.precision) +
    (meta.unit ? ' ' + meta.unit : '');
  entry.value.style.color = valueColor(entry.key, value, meta);
}

/* Horizontal drag pans every chart at once: dragging right walks back in time,
   the way a map drags, and with the same sign as MonitorApp._on_graph_drag.
   Only the live graphs get this - the summary charts drag out a time range. */
function attachHistoryPan(canvas) {
  var dragFrom = null;

  function samplesPerPixel() {
    /* The view holds LIVE_SECONDS of samples, so that many fit across the plot;
       the desktop divides by its capacity in exactly this way. */
    var width = Math.max(60, canvas.clientWidth || 300);
    return Math.max(1, LIVE_SECONDS * (sampleHz() || 1)) / width;
  }

  canvas.addEventListener('mousedown', function (ev) {
    if (ev.button !== 0) { return; }
    ev.preventDefault();
    canvas.focus();
    dragFrom = {x: ev.clientX, offset: hist.offset};
    canvas.__panning = true;
  });
  canvas.addEventListener('mousemove', function (ev) {
    if (!dragFrom) { return; }
    /* Absolute, not relative: the drag is measured from where the press landed,
       so a slow drag cannot accumulate rounding the way adding deltas would.
       Subtracted so the trace follows the pointer: dragging right brings newer
       samples in from the right. Adding it - the first version - pushed the
       chart away from the cursor, which reads as the graph fighting the mouse.
       ui/monitor.py::_on_graph_drag does the same subtraction. */
    setOffset(dragFrom.offset - (ev.clientX - dragFrom.x) * samplesPerPixel());
  });
  function finish() {
    /* A drag that moved nothing is just a click on a graph: the view stays where
       it was, so a stray click cannot throw the history away. */
    if (!dragFrom) { return; }
    dragFrom = null;
    canvas.__panning = false;
  }
  canvas.addEventListener('mouseup', finish);
  canvas.addEventListener('mouseleave', finish);
  canvas.addEventListener('dblclick', function () { resumeLive(); });
  canvas.addEventListener('wheel', function (ev) {
    /* Shift+wheel pans; a plain wheel is left alone so the page still scrolls
       (ui/monitor.py takes the same decision in _on_graph_wheel). */
    if (!ev.shiftKey) { return; }
    ev.preventDefault();
    /* One notch is five samples, as on the desktop (-delta / 120 * 5). Stepping
       by sign keeps trackpads, which send small deltas, at that same step. */
    panSamples(ev.deltaY > 0 ? WHEEL_SAMPLES : -WHEEL_SAMPLES);
  }, {passive: false});
}

function buildLive(data) {
  var host = byId('live');
  clear(host);
  liveSeries = {};
  var catalogue = data.metrics || {};
  var keys = Object.keys(catalogue);
  state.catalogue = catalogue;

  function wanted(key, field) {
    return keys.length === 0 || catalogue[key] !== undefined || PRIMARY[field];
  }

  var sysCard = makeCard('CPU & MEMORY', systemMeta(data), []);
  for (var i = 0; i < CPU_ROWS.length; i++) {
    var row = CPU_ROWS[i];
    if (!wanted(row.key, row.key)) { continue; }
    var meta = catalogue[row.key] || {label: row.label, unit: row.unit,
                                      precision: 0, kind: 'line'};
    addLiveRow(sysCard.body, row.key, row.label, {
      field: row.key, vmin: row.min, vmax: row.max, unit: meta.unit || row.unit,
      precision: meta.precision || 0, color: fieldColor(row.key), band: row.band,
      thresh: row.thresh, tmin: 0, tmax: 1, axis: false, showPeak: true,
      showCurrent: true, meta: meta, hoverT: null, pinned: false});
  }
  host.appendChild(sysCard.root);

  for (var d = 0; d < state.devices.length; d++) {
    var dev = state.devices[d];
    var meta2 = [dev.vendor || 'unknown'];
    if (dev.pci) { meta2.push(dev.pci); }
    if (dev.vram_total_mb) { meta2.push(fmt(dev.vram_total_mb, 0) + ' MB VRAM'); }
    if (dev.sources && dev.sources.length) {
      meta2.push('via ' + dev.sources.join('+'));
    }
    if (dev.luid_confidence && dev.luid_confidence !== 'exact') {
      meta2.push('counter ' + dev.luid_confidence);
    }
    var chipsetInput = [];
    if (dev.note) { chipsetInput.push({level: '', text: dev.note}); }
    var card = makeCard('GPU' + dev.index + ' ' + dev.name, meta2, chipsetInput);
    for (var r = 0; r < GPU_ROWS.length; r++) {
      var def = GPU_ROWS[r];
      var key2 = 'gpu' + dev.index + '_' + def.field;
      if (!wanted(key2, def.field)) { continue; }
      var info = catalogue[key2] || {label: def.label, unit: def.unit,
                                     precision: 0, kind: 'line'};
      info.limit = dev.temp_limit;
      info.key = key2;
      addLiveRow(card.body, key2, def.label, {
        field: def.field, vmin: def.min, vmax: def.max,
        unit: info.unit || def.unit, precision: info.precision || 0,
        color: fieldColor(def.field), band: def.band, thresh: def.thresh,
        tmin: 0, tmax: 1, axis: false, showPeak: true, showCurrent: true,
        meta: info, hoverT: null, pinned: false});
    }
    host.appendChild(card.root);
  }
  /* The rows exist now, so the indicator can be settled from the shared offset
     (0 on a fresh page): the markup already says LIVE, this keeps the two in
     step even if the script is reloaded into a page that was panned. */
  applyHistoryView();
}

function systemMeta(data) {
  var caps = data.capabilities || {};
  var lines = [];
  if (!caps.cpu_temp) { lines.push('CPU temperature sensor not available'); }
  var errors = caps.backend_errors || {};
  for (var name in errors) {
    if (errors.hasOwnProperty(name)) {
      lines.push(name + ': ' + String(errors[name]).slice(0, 90));
    }
  }
  if (!lines.length) { lines.push(''); }
  return lines;
}

function applySample(data) {
  state.status = data.status;
  state.wallOffset = data.wall_offset || 0;
  var t = data.t;
  var values = data.values || {};
  var key;
  for (key in values) {
    if (!values.hasOwnProperty(key)) { continue; }
    var entry = liveSeries[key];
    if (!entry || t === null || t === undefined) { continue; }
    var points = entry.points;
    if (points.length && t <= points[points.length - 1][0]) {
      /* The browser polls faster than the sampler: overwrite the last point
         instead of stacking duplicate timestamps. */
      points[points.length - 1] = [t, values[key]];
    } else {
      points.push([t, values[key]]);
    }
    while (points.length > MAX_LIVE_POINTS ||
           (points.length && points[0][0] < t - LIVE_SECONDS - 5)) {
      points.shift();
    }
  }
  /* The view then follows the buffer forward: while panned, the extra samples
     must not creep it back towards live. Windows and badges are rebuilt from
     wherever the view now sits, so the badges show the right edge of the view
     rather than the arriving value. */
  followBuffer(viewNewestT());
  updateLiveWindows();
  renderLiveValues();
  renderStatus(data.status);
  renderAlarms(data.active_alarms || [], data.status);
  var wasLogging = state.logging;
  state.logging = !!(data.status && data.status.logging);
  state.sessionId = data.status ? data.status.session_id : null;
  if (wasLogging && !state.logging) {
    /* A run just ended (here or from the desktop window): refresh the list so
       the finished session can be picked for the summary. */
    refreshSessions();
    if (!summary.window) { loadSummary(true); }
  }
  scheduleDraw();
}

function renderStatus(st) {
  if (!st) { return; }
  setText('f-rate', 'rate ' + fmt(st.effective_hz, 2) + ' Hz (target ' +
                    fmt(st.sample_hz, 2) + ')');
  setText('f-poll', 'poll cost ' + fmt(st.poll_ms, 1) + ' ms');
  setText('f-dropped', 'dropped ' + st.dropped + ' empty ticks, ' +
                       st.writer_dropped + ' unwritten samples');
  setText('f-queue', 'queue ' + st.queue_depth +
                     (st.gap_count ? ', ' + st.gap_count + ' gaps' : ''));
  setText('f-errors', st.last_error ? 'last error: ' + st.last_error : '');
  setText('f-db', state.db ? 'db ' + state.db : '');
  var btn = byId('btn-log');
  btn.textContent = st.logging ? 'Stop logging' : 'Start logging';
  setText('logging-state', st.logging
    ? 'logging session ' + st.session_id + ' - ' + duration(st.elapsed) +
      ' - ' + st.samples_written + ' samples written'
    : 'not logging - live view only');
}

function renderAlarms(alarms, st) {
  var bar = byId('alarmbar');
  clear(bar);
  if (!alarms.length) {
    if (st && st.logging) {
      bar.appendChild(el('span', 'badge ok', 'no active alarms'));
    }
    return;
  }
  for (var i = 0; i < alarms.length; i++) {
    var a = alarms[i];
    bar.appendChild(el('span', 'chip ' + a.level,
      a.label + ' ' + a.level + ' >= ' + fmt(a.threshold, 0) +
      ' (peak ' + fmt(a.peak, 0) + ')'));
  }
}

/* ------------------------------------------------------------- controls */
function startLogging() {
  var suggested = 'web ' + new Date().toLocaleString();
  var label = window.prompt('Label for this logging session:', suggested);
  /* A dismissed prompt still starts the run: refusing to log because a dialog
     was closed would lose the measurement the user was about to take. */
  if (label === null) { label = ''; }
  postJSON('/api/logging/start', {label: label}).then(function (data) {
    toast('logging started: session ' + data.session_id);
    refreshSessions();
    if (!summary.window) { loadSummary(true); }
  }).catch(function (err) { toast('could not start logging: ' + err.message, true); });
}

function stopLogging() {
  postJSON('/api/logging/stop', {}).then(function (data) {
    if (!data.stopped) { toast('no session was logging'); return; }
    toast('session ' + data.session_id + ' stopped: ' + data.samples +
          ' samples, ' + data.alarms + ' alarms');
    refreshSessions();
    byId('session-select').value = String(data.session_id);
    loadSummary(true);
  }).catch(function (err) { toast('could not stop logging: ' + err.message, true); });
}

function addMarker() {
  var label = window.prompt('Marker label:', 'mark');
  if (label === null) { label = 'mark'; }
  postJSON('/api/marker', {label: label}).then(function (data) {
    if (data.ok) {
      toast('marker added: ' + data.label);
    } else {
      toast('no session is logging, so the marker was not stored', true);
    }
  }).catch(function (err) { toast('could not add marker: ' + err.message, true); });
}

function applyTheme() {
  var select = byId('theme-select');
  var previous = state.theme;
  var chosen = select.value;
  postJSON('/api/theme', {theme: chosen}).then(function () {
    /* The `:root` block is rendered per request from the active palette, so a
       reload is what repaints the page - including the canvases, which read
       their colours from those same variables. */
    window.location.reload();
  }).catch(function (err) {
    if (previous) { select.value = previous; }
    toast('could not switch theme: ' + err.message, true);
  });
}

function refreshSessions() {
  return api('/api/sessions?limit=50').then(function (data) {
    var select = byId('session-select');
    var keep = select.value;
    clear(select);
    var live = el('option', null, 'live / newest session');
    live.value = '';
    select.appendChild(live);
    var sessions = data.sessions || [];
    for (var i = 0; i < sessions.length; i++) {
      var s = sessions[i];
      var text = '#' + s.id + ' ' + (s.label || '(unlabelled)') + ' - ' +
                 duration(s.duration) + ' - ' + s.sample_count + ' samples' +
                 (s.ongoing ? ' - ongoing' : '');
      var option = el('option', null, text);
      option.value = String(s.id);
      select.appendChild(option);
    }
    if (keep) { select.value = keep; }
    return data;
  }).catch(function () { return {sessions: []}; });
}

/* --------------------------------------------------------------- summary */
function unitCharts(tab) {
  if (tab.id === 'system') {
    return [
      {title: 'Utilisation over time', unit: '%', min: 0, max: 100, series: [
        ['cpu_util', 'CPU utilisation', SERIES[0]],
        ['ram_percent', 'RAM utilisation', SERIES[2]]]},
      {title: 'Temperature over time', unit: 'C', min: 0, max: 110,
       band: [80, 110], field: 'cpu_temp', series: [
        ['cpu_temp', 'CPU temperature', C.warn]]},
      {title: 'Memory over time', unit: 'MB', min: 0, max: null, series: [
        ['ram_used', 'RAM used', SERIES[2]],
        ['ram_total', 'RAM total', SERIES[7]]]},
      {title: 'Clocks over time', unit: 'MHz', min: 0, max: null, series: [
        ['cpu_clock', 'CPU clock', SERIES[1]]]},
      {title: 'Power over time', unit: 'W', min: 0, max: null, series: [
        ['cpu_power', 'CPU package power', SERIES[3]]]}
    ];
  }
  var i = tab.index;
  return [
    {title: 'Utilisation over time', unit: '%', min: 0, max: 100, series: [
      ['gpu' + i + '_util', 'GPU utilisation', SERIES[0]],
      ['gpu' + i + '_mem_util', 'memory bandwidth', SERIES[2]],
      ['gpu' + i + '_vram_percent', 'VRAM utilisation', SERIES[3]]]},
    /* Hotspot first: it is the number that actually limits a card, and edge
       temperature alone hides it (same ordering as ui/summary.py). */
    {title: 'Temperature over time', unit: 'C', min: 0, max: 110,
     band: [80, 110], field: 'temp', series: [
      ['gpu' + i + '_hotspot', 'hotspot (junction)', C.warn],
      ['gpu' + i + '_temp', 'GPU core', C.crit],
      ['gpu' + i + '_mem_temp', 'memory', SERIES[3]]]},
    {title: 'VRAM usage over time', unit: 'MB', min: 0, max: null, series: [
      ['gpu' + i + '_vram_used', 'VRAM used', SERIES[2]],
      ['gpu' + i + '_vram_total', 'VRAM total', SERIES[7]]]},
    {title: 'Clocks over time', unit: 'MHz', min: 0, max: null, series: [
      ['gpu' + i + '_clock_core', 'core clock', SERIES[1]],
      ['gpu' + i + '_clock_mem', 'memory clock', SERIES[4]],
      ['gpu' + i + '_clock_sm', 'SM clock', SERIES[5]]]},
    {title: 'Power over time', unit: 'W', min: 0, max: null, series: [
      ['gpu' + i + '_power', 'board power', SERIES[3]],
      ['gpu' + i + '_power_limit', 'power limit', SERIES[6]]]}
  ];
}

function ensureSeries(sessionId, metric) {
  var key = sessionId + '|' + metric;
  if (summary.series[key]) { return Promise.resolve(summary.series[key]); }
  var url = '/api/series?session_id=' + encodeURIComponent(sessionId) +
            '&metric=' + encodeURIComponent(metric);
  return api(url).then(function (data) {
    summary.series[key] = data.series || [];
    return summary.series[key];
  }).catch(function () {
    summary.series[key] = [];
    return [];
  });
}

function renderTabs() {
  var host = byId('tabs');
  clear(host);
  for (var i = 0; i < summary.tabs.length; i++) {
    var tab = summary.tabs[i];
    var button = el('button', null, tab.label);
    button.type = 'button';
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-selected', i === summary.tab ? 'true' : 'false');
    button.dataset.index = String(i);
    if (i === summary.tab) { button.style.borderColor = C.accent; }
    host.appendChild(button);
  }
}

function renderSummary() {
  var data = summary.data;
  if (!data) { return; }
  var devices = data.devices || [];
  var tabs = [];
  for (var d = 0; d < devices.length; d++) {
    tabs.push({id: 'gpu' + devices[d].index, label: 'gpu' + devices[d].index,
               index: devices[d].index, device: devices[d]});
  }
  tabs.push({id: 'system', label: 'system', index: -1, device: null});
  summary.tabs = tabs;
  if (summary.tab >= tabs.length) { summary.tab = 0; }
  renderTabs();
  renderCharts();
  renderTables();
  renderSessionNote();
}

function renderCharts() {
  var data = summary.data;
  var host = byId('charts');
  clear(host);
  summary.chartSpecs = [];
  if (!data) { return; }
  var tab = summary.tabs[summary.tab];
  if (!tab) { return; }
  var present = {};
  var metrics = data.metrics || [];
  for (var m = 0; m < metrics.length; m++) { present[metrics[m]] = true; }
  var span = (data.window && data.window.span) || summary.span || [0, 1];
  summary.span = span;
  var definitions = unitCharts(tab);
  var pending = [];
  for (var c = 0; c < definitions.length; c++) {
    var def = definitions[c];
    var available = [];
    for (var s = 0; s < def.series.length; s++) {
      if (present[def.series[s][0]]) { available.push(def.series[s]); }
    }
    if (!available.length) { continue; }
    var wrap = el('div', 'chartwrap');
    wrap.appendChild(el('h4', null, def.title + ' (' + def.unit + ')'));
    var canvas = el('canvas', 'chart');
    canvas.width = 900;
    canvas.height = 380;
    canvas.tabIndex = 0;
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label',
                        def.title + ' for ' + tab.label + ' over the selected range');
    wrap.appendChild(canvas);
    var legend = el('div', 'legend');
    for (var l = 0; l < available.length; l++) {
      var item = el('span');
      var swatch = el('i');
      swatch.style.background = available[l][2];
      item.appendChild(swatch);
      item.appendChild(document.createTextNode(available[l][1]));
      legend.appendChild(item);
    }
    wrap.appendChild(legend);
    host.appendChild(wrap);
    var spec = {
      lines: [], vmin: def.min, vmax: def.max, tmin: span[0], tmax: span[1],
      axis: true, unit: def.unit, precision: def.unit === 'MHz' ? 0 : 1,
      band: def.band || null, field: def.field || null, thresh: [],
      selection: summary.window, alarms: (summary.full || {}).alarms || [],
      markers: (summary.full || {}).markers || [], clockBase: null,
      relative: true, hoverT: null, pinned: false, showPeak: true,
      showCurrent: false, label: def.title
    };
    if (def.field === 'temp') { spec.thresh = [80]; }
    if (def.field === 'cpu_temp') { spec.thresh = [85]; }
    canvas.__spec = spec;
    attachHover(canvas);
    attachRangeDrag(canvas);
    summary.chartSpecs.push({canvas: canvas, spec: spec});
    pending.push({def: def, available: available, spec: spec, canvas: canvas});
  }
  /* Series are fetched one metric at a time; they are the whole run, not the
     selected window, so the shaded selection always has its context around it. */
  var chain = Promise.resolve();
  pending.forEach(function (item) {
    chain = chain.then(function () {
      var loads = item.available.map(function (seriesDef) {
        return ensureSeries(data.session.id, seriesDef[0]).then(function (points) {
          item.spec.lines.push({label: seriesDef[1], color: seriesDef[2],
                                points: points});
        });
      });
      return Promise.all(loads).then(function () { drawChart(item.canvas); });
    });
  });
}

function attachRangeDrag(canvas) {
  var dragFrom = null;
  canvas.addEventListener('mousedown', function (ev) {
    if (ev.button !== 0) { return; }
    ev.preventDefault();
    canvas.focus();
    dragFrom = tAtX(canvas, ev.clientX);
    canvas.__dragging = true;
    var spec = canvas.__spec;
    spec.selection = [dragFrom, dragFrom];
    drawChart(canvas);
  });
  canvas.addEventListener('mousemove', function (ev) {
    if (dragFrom === null) { return; }
    var spec = canvas.__spec;
    spec.selection = [dragFrom, tAtX(canvas, ev.clientX)];
    drawChart(canvas);
  });
  function finish(ev) {
    if (dragFrom === null) { return; }
    canvas.__dragging = false;
    var spec = canvas.__spec;
    var to = tAtX(canvas, ev.clientX);
    var lo = Math.min(dragFrom, to), hi = Math.max(dragFrom, to);
    dragFrom = null;
    var span = spec.tmax - spec.tmin;
    if (hi - lo < span * 0.01) {
      /* A plain click is not a range: treat it as "clear the selection". */
      spec.selection = summary.window;
      drawChart(canvas);
      return;
    }
    setWindow([lo, hi]);
  }
  canvas.addEventListener('mouseup', finish);
  canvas.addEventListener('mouseleave', function (ev) {
    if (dragFrom !== null) { finish(ev); }
  });
  canvas.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && dragFrom !== null) {
      dragFrom = null;
      canvas.__dragging = false;
      canvas.__spec.selection = summary.window;
      drawChart(canvas);
    }
  });
}

function metricsForTab(tab) {
  if (!summary.data) { return []; }
  var all = summary.data.metrics || [];
  var out = [];
  for (var i = 0; i < all.length; i++) {
    var key = all[i];
    var isGpu = key.indexOf('gpu') === 0;
    var index = isGpu ? parseInt(key.slice(3).split('_')[0], 10) : -1;
    if (tab.id === 'system') {
      if (!isGpu) { out.push(key); }
    } else if (index === tab.index) {
      out.push(key);
    }
  }
  return out;
}

function renderTables() {
  var data = summary.data;
  var table = byId('statstable');
  clear(table);
  if (!data) { return; }
  var tab = summary.tabs[summary.tab];
  if (!tab) { return; }
  var keys = metricsForTab(tab);
  var columns = ['metric', 'n', 'low', 'avg', 'high', 'max (run)', 'median',
                 'p95', 'p99', 'unit'];
  var head = el('thead');
  var headRow = el('tr');
  for (var c = 0; c < columns.length; c++) {
    headRow.appendChild(el('th', null, columns[c]));
  }
  head.appendChild(headRow);
  table.appendChild(head);
  var body = el('tbody');
  for (var k = 0; k < keys.length; k++) {
    var key = keys[k];
    var st = data.stats[key];
    if (!st) { continue; }
    var precision = st.unit === 'MHz' || st.unit === 'C' ? 0 : 1;
    /* "max (run)" keeps the whole-run peak visible while a sub-window is
       selected - the windowed high would otherwise hide it. */
    var runStats = (summary.fullStats || {})[key];
    var runMax = summary.window && runStats ? runStats.maximum : st.maximum;
    var row = el('tr');
    row.appendChild(el('td', null, st.label + (tab.id === 'system' ? '' :
                                               ' (' + key + ')')));
    row.appendChild(el('td', null, st.count));
    row.appendChild(el('td', null, fmt(st.minimum, precision)));
    row.appendChild(el('td', null, fmt(st.mean, precision)));
    row.appendChild(el('td', null, fmt(st.maximum, precision)));
    row.appendChild(el('td', null, fmt(runMax, precision)));
    row.appendChild(el('td', null, fmt(st.median, precision)));
    row.appendChild(el('td', null, fmt(st.p95, precision)));
    row.appendChild(el('td', null, fmt(st.p99, precision)));
    row.appendChild(el('td', null, st.unit || ''));
    body.appendChild(row);
  }
  table.appendChild(body);
  renderAlarmList();
  renderMarkerList();
  renderRangeLabel();
}

function renderAlarmList() {
  var host = byId('alarm-list');
  clear(host);
  var data = summary.data;
  var alarms = (data && data.alarms) || [];
  if (!alarms.length) {
    host.appendChild(el('div', 'dim', 'no alarms in this window'));
    return;
  }
  for (var i = 0; i < alarms.length; i++) {
    var a = alarms[i];
    var text = a.level.toUpperCase() + '  ' + a.label +
      ' >= ' + fmt(a.threshold, 0) +
      '  at +' + a.started_t.toFixed(1) + 's' +
      (a.ended_t === null || a.ended_t === undefined
        ? ' (ongoing)' : ' for ' + (a.ended_t - a.started_t).toFixed(1) + 's') +
      '  peak ' + fmt(a.peak, 0);
    var row = el('div', a.level, text);
    host.appendChild(row);
  }
}

function renderMarkerList() {
  var host = byId('marker-list');
  clear(host);
  var data = summary.data;
  var markers = (data && data.markers) || [];
  if (!markers.length) {
    host.appendChild(el('div', 'dim', 'no markers in this window'));
    return;
  }
  for (var i = 0; i < markers.length; i++) {
    var m = markers[i];
    host.appendChild(el('div', null,
      '+' + m.t.toFixed(1) + 's  ' + m.label + '  (' + clock(m.wall) + ')'));
  }
}

function renderSessionNote() {
  var data = summary.data;
  var note = byId('summary-note');
  if (!data) { note.textContent = ''; return; }
  var s = data.session;
  var parts = ['session #' + s.id + ' "' + (s.label || 'unlabelled') + '"',
               'started ' + clock(s.started_at),
               duration(s.duration),
               s.sample_count + ' samples at ' + fmt(s.sample_hz, 2) + ' Hz'];
  var span = data.window && data.window.span;
  if (span) { parts.push('span ' + span[0].toFixed(1) + 's to ' +
                         span[1].toFixed(1) + 's'); }
  var devices = data.devices || [];
  for (var i = 0; i < devices.length; i++) {
    var d = devices[i];
    parts.push('gpu' + d.index + ' ' + d.name + (d.pci ? ' (' + d.pci + ')' : ''));
  }
  note.textContent = parts.join('  -  ');
}

function renderRangeLabel() {
  var data = summary.data;
  var span = (data && data.window && data.window.span) || summary.span;
  var label;
  if (summary.window && span) {
    var wide = summary.window[1] - summary.window[0];
    label = 'selected ' + summary.window[0].toFixed(1) + 's to ' +
            summary.window[1].toFixed(1) + 's (' + wide.toFixed(1) + 's of ' +
            (span[1] - span[0]).toFixed(1) + 's)';
  } else {
    label = 'whole run' + (span
      ? ' (' + (span[1] - span[0]).toFixed(1) + 's)' : '');
  }
  setText('range-label', label);
}

function applySelection() {
  for (var i = 0; i < summary.chartSpecs.length; i++) {
    summary.chartSpecs[i].spec.selection = summary.window;
    drawChart(summary.chartSpecs[i].canvas);
  }
}

function setWindow(windowRange) {
  summary.window = windowRange;
  applySelection();
  renderRangeLabel();
  loadSummary(false);
}

function useFraction(fraction) {
  var span = (summary.data && summary.data.window && summary.data.window.span) ||
             summary.span;
  if (!span) { return; }
  var width = span[1] - span[0];
  if (width <= 0) { return; }
  setWindow([span[1] - width * fraction, span[1]]);
}

function loadSummary(rebuild) {
  var select = byId('session-select');
  var params = [];
  if (select.value) { params.push('session_id=' + encodeURIComponent(select.value)); }
  if (summary.window) {
    params.push('t0=' + summary.window[0].toFixed(3));
    params.push('t1=' + summary.window[1].toFixed(3));
  }
  var url = '/api/summary' + (params.length ? '?' + params.join('&') : '');
  return api(url).then(function (data) {
    summary.data = data;
    if (!summary.window) {
      summary.fullStats = data.stats;
      /* Decorations (alarm spans, markers) always describe the whole run: the
         shaded window is a reading aid, not a different timeline. */
      summary.full = {alarms: data.alarms, markers: data.markers};
    }
    if (rebuild) { renderSummary(); } else { renderTables(); }
  }).catch(function (err) {
    setText('summary-note', 'summary unavailable: ' + err.message);
  });
}

function selectTab(index) {
  summary.tab = index;
  renderTabs();
  renderCharts();
  renderTables();
}

function newSessionSelected() {
  summary.window = null;
  summary.series = {};
  summary.fullStats = null;
  summary.full = null;
  summary.data = null;
  loadSummary(true);
}

/* ------------------------------------------------------------------ poll */
function applyHistory(series) {
  var key;
  for (key in series) {
    if (!series.hasOwnProperty(key)) { continue; }
    var entry = liveSeries[key];
    if (!entry) { continue; }
    entry.points = series[key];
    entry.spec.lines[0].points = entry.points;
  }
  /* The ring buffer is authoritative and may already hold samples this page
     never saw, so re-anchor the pan to what really arrived: the offset keeps its
     value, and the creep is measured from the buffer's own newest sample. */
  hist.newestT = viewNewestT();
  updateLiveWindows();
  renderLiveValues();
}

function poll() {
  api('/api/sample').then(function (data) {
    failures = 0;
    setConn(true);
    applySample(data);
    pollCount++;
    if (pollCount % RESYNC_EVERY === 1) {
      /* The locally appended samples can drift the moment a tab sleeps or a
         sample is dropped; the ring buffer is authoritative, so re-read it. */
      return api('/api/history?seconds=' + LIVE_SECONDS).then(function (payload) {
        applyHistory(payload.series || {});
        scheduleDraw();
      }).catch(function () { return null; });
    }
    return null;
  }).catch(function (err) {
    failures++;
    setConn(false, failures > 2 ? err.message : '');
  }).then(function () {
    pollTimer = setTimeout(poll, POLL_MS);
  });
}

/* ------------------------------------------------------------ keyboard */
document.addEventListener('keydown', function (ev) {
  var node = ev.target;
  var tag = node && node.tagName ? node.tagName : '';
  /* Never steal keys from a text field or a select: those own their arrows. */
  if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA' ||
      (node && node.isContentEditable)) {
    return;
  }
  var step = ev.shiftKey ? 260 : 90;
  var key = ev.key;
  if (key === 'ArrowDown') { window.scrollBy(0, step); }
  else if (key === 'ArrowUp') { window.scrollBy(0, -step); }
  else if (key === 'PageDown') { window.scrollBy(0, window.innerHeight * 0.9); }
  else if (key === 'PageUp') { window.scrollBy(0, -window.innerHeight * 0.9); }
  else if (key === 'Home') { window.scrollTo(0, 0); }
  else if (key === 'End') { window.scrollTo(0, document.body.scrollHeight); }
  /* P resumes live, the same key and the same meaning as the desktop window. */
  else if (key === 'p' || key === 'P') { resumeLive(); }
  else { return; }
  ev.preventDefault();
});

function wireControls() {
  byId('btn-log').addEventListener('click', function () {
    if (state.logging) { stopLogging(); } else { startLogging(); }
  });
  byId('btn-mark').addEventListener('click', addMarker);
  byId('btn-reload').addEventListener('click', function () { loadSummary(false); });
  byId('session-select').addEventListener('change', newSessionSelected);
  byId('theme-select').addEventListener('change', applyTheme);
  /* The chip is the mouse-only way back to live: click it, or double-click a
     chart, or shift+wheel/drag the other way. */
  byId('hist-chip').addEventListener('click', resumeLive);
  byId('preset-50').addEventListener('click', function () { useFraction(0.5); });
  byId('preset-25').addEventListener('click', function () { useFraction(0.25); });
  byId('preset-full').addEventListener('click', function () {
    summary.window = null;
    applySelection();
    loadSummary(false);
  });
  byId('tabs').addEventListener('click', function (ev) {
    var index = ev.target && ev.target.dataset ? ev.target.dataset.index : null;
    if (index !== null && index !== undefined && index !== '') {
      selectTab(parseInt(index, 10));
    }
  });
  window.addEventListener('resize', function () {
    scheduleDraw();
    applySelection();
  });
}

/* ----------------------------------------------------------------- boot */
function boot() {
  readTheme();
  api('/api/state').then(function (data) {
    state.devices = data.devices || [];
    state.db = (data.server && data.server.db) || '';
    state.theme = data.theme || '';
    var exposed = data.server && data.server.exposed;
    setText('version', 'v' + data.app_version +
                       (exposed ? ' (network exposed)' : ''));
    buildLive(data);
    renderStatus(data.status);
    renderAlarms(data.alarms || [], data.status);
    var headline = byId('headline');
    headline.style.whiteSpace = 'pre-line';
    headline.textContent = (data.capabilities && data.capabilities.notes || [])
      .join('\\n');
    wireControls();
    refreshSessions().then(function () { return loadSummary(true); });
    poll();
  }).catch(function (err) {
    setConn(false, err.message);
    setTimeout(boot, 1000);
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
})();
"""


def page() -> str:
    """The whole single-page application, ready to serve.

    Assembled per request rather than cached: the cost is a few string joins,
    and it means editing this file and reloading the browser is the entire
    development loop - no build step to forget. It is also what lets a theme
    switch take effect: the stylesheet and the theme menu are both rendered from
    `themes.current()` here.
    """
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>gpumon - GPU &amp; system telemetry</title>\n"
        f"<style>{stylesheet()}</style>\n</head>\n<body>\n"
        f"{BODY.replace(PICKER_SLOT, theme_picker())}\n"
        f"<script>{JS}</script>\n</body>\n</html>\n"
    )
