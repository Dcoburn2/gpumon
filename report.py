"""Standalone report exports (no external libraries, charts as inline SVG).

The HTML report is fully self-contained: no CDN, no external assets, so it can
be emailed or archived and still renders offline.
"""
from __future__ import annotations

import html
import time
from typing import Sequence

import metrics as M
import store as S

REPORT_CSS = """
:root { color-scheme: dark; }
body { background:#0b0f14; color:#c8d4e0; font:13px/1.5 "Segoe UI",system-ui,sans-serif;
       margin:0; padding:24px 28px 60px; }
h1 { font-size:22px; color:#eaf2fa; margin:0 0 4px; }
h2 { font-size:15px; color:#eaf2fa; margin:26px 0 8px; border-bottom:1px solid #1e2a38;
     padding-bottom:4px; }
.sub { color:#6b7d90; font-size:12px; margin-bottom:18px; }
table { border-collapse:collapse; font-size:12px; margin:6px 0 18px; }
th { text-align:left; color:#6b7d90; font-weight:600; padding:4px 10px 4px 0;
     border-bottom:1px solid #1e2a38; white-space:nowrap; }
td { padding:3px 10px 3px 0; border-bottom:1px solid #131b25; white-space:nowrap; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
.ok { color:#3ddc84; } .warn { color:#ffb020; } .crit { color:#ff4d4f; }
.dim { color:#6b7d90; }
.card { background:#111823; border:1px solid #1e2a38; border-radius:6px;
        padding:12px 16px; margin:0 0 14px; display:inline-block; vertical-align:top;
        margin-right:12px; }
.card h3 { margin:0 0 6px; font-size:13px; color:#eaf2fa; }
.card .note { color:#6b7d90; font-size:11px; margin-bottom:8px; max-width:320px; }
.chart { background:#111823; border:1px solid #1e2a38; border-radius:6px;
         margin:6px 0 18px; display:block; }
.legend { font-size:11px; color:#6b7d90; margin:-12px 0 18px; }
.legend span { margin-right:14px; }
.legend i { display:inline-block; width:10px; height:10px; border-radius:2px;
            margin-right:4px; vertical-align:middle; }
.empty { color:#3ddc84; }
code { background:#0e141d; padding:1px 5px; border-radius:3px; font-size:12px; }
"""

PALETTE = ["#3ea6ff", "#b388ff", "#00d4c8", "#ffa657", "#7ee787",
           "#ff7eb6", "#d2a8ff", "#79c0ff"]


# --------------------------------------------------------------------------
# SVG chart builder
# --------------------------------------------------------------------------


def _svg_line_chart(series: Sequence[tuple[str, str, list[tuple[float, float]]]],
                    *, width: int = 1000, height: int = 240,
                    y_label: str = "", bands: Sequence[tuple[float, float, str, str]] = (),
                    markers: Sequence[tuple[float, str, str]] = ()) -> str:
    """Render a line chart as inline SVG. `series` is (label, color, points)."""
    pad_l, pad_r, pad_t, pad_b = 56, 20, 26, 34
    x0, y0 = pad_l, pad_t
    x1, y1 = width - pad_r, height - pad_b

    usable = [(lbl, col, pts) for lbl, col, pts in series if len(pts) >= 2]
    if not usable:
        return (f'<svg class="chart" width="{width}" height="{height}">'
                f'<text x="{width / 2}" y="{height / 2}" fill="#6b7d90" '
                f'font-size="12" text-anchor="middle">no data recorded</text></svg>')

    xs = [p[0] for _l, _c, pts in usable for p in pts]
    ys = [p[1] for _l, _c, pts in usable for p in pts]
    ax0, ax1 = min(xs), max(xs)
    if ax1 - ax0 < 1e-9:
        ax1 = ax0 + 1.0
    ay0, ay1 = min(ys), max(ys)
    # Include a threshold band only when it sits near the data. A 70-85 C band on
    # a 35 C run would otherwise stretch the axis to 0 and flatten the trace.
    span = (ay1 - ay0) or 1.0
    for lo, hi, _c, _lbl in bands:
        if lo <= ay1 + span and hi >= ay0 - span:
            ay0 = min(ay0, lo)
            ay1 = max(ay1, hi)
    pad = (ay1 - ay0) * 0.08 or 1.0
    ay0 -= pad
    ay1 += pad

    def sx(v: float) -> float:
        return x0 + (v - ax0) / (ax1 - ax0) * (x1 - x0)

    def sy(v: float) -> float:
        return y1 - (v - ay0) / (ay1 - ay0) * (y1 - y0)

    parts = [f'<svg class="chart" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">']
    for lo, hi, color, label in bands:
        top, bottom = min(sy(hi), sy(lo)), max(sy(hi), sy(lo))
        top_c, bottom_c = max(top, y0), min(bottom, y1)
        if bottom_c > top_c:
            parts.append(f'<rect x="{x0:.1f}" y="{top_c:.1f}" '
                         f'width="{x1 - x0:.1f}" height="{bottom_c - top_c:.1f}" '
                         f'fill="{color}" opacity="0.35"/>')
            if label:
                parts.append(f'<text x="{x1 - 4:.1f}" y="{top_c + 12:.1f}" '
                             f'fill="#6b7d90" font-size="10" text-anchor="end">'
                             f'{html.escape(label)}</text>')

    for i in range(5):
        gy = y0 + (y1 - y0) * i / 4
        val = ay1 - (ay1 - ay0) * i / 4
        parts.append(f'<line x1="{x0:.1f}" y1="{gy:.1f}" x2="{x1:.1f}" y2="{gy:.1f}" '
                     f'stroke="#1e2a38" stroke-dasharray="2 4"/>')
        parts.append(f'<text x="{x0 - 6:.1f}" y="{gy + 4:.1f}" fill="#6b7d90" '
                     f'font-size="10" text-anchor="end">{val:.0f}</text>')
    for i in range(5):
        gx = x0 + (x1 - x0) * i / 4
        tval = ax0 + (ax1 - ax0) * i / 4
        parts.append(f'<text x="{gx:.1f}" y="{y1 + 14:.1f}" fill="#6b7d90" '
                     f'font-size="10" text-anchor="middle">{tval:.0f}s</text>')

    for label, color, pts in usable:
        # Decimate to at most 2 points per pixel.
        step = max(1, len(pts) // max(2, int(x1 - x0) * 2))
        sampled = pts[::step]
        if sampled[-1] != pts[-1]:
            sampled = list(sampled) + [pts[-1]]
        d = " ".join(f"{sx(px):.1f},{sy(py):.1f}" for px, py in sampled)
        parts.append(f'<polyline points="{d}" fill="none" stroke="{color}" '
                     f'stroke-width="1.6" stroke-linejoin="round"/>')

    for mx, mlabel, mcolor in markers:
        if not (ax0 <= mx <= ax1):
            continue
        px = sx(mx)
        parts.append(f'<line x1="{px:.1f}" y1="{y0:.1f}" x2="{px:.1f}" y2="{y1:.1f}" '
                     f'stroke="{mcolor}" stroke-width="1" stroke-dasharray="3 3"/>')

    if y_label:
        parts.append(f'<text x="{x0 - 46:.1f}" y="{y0 - 10:.1f}" fill="#6b7d90" '
                     f'font-size="11">{html.escape(y_label)}</text>')
    parts.append("</svg>")

    legend = "".join(
        f'<span><i style="background:{col}"></i>{html.escape(lbl)}</span>'
        for lbl, col, _pts in usable)
    return "".join(parts) + f'<div class="legend">{legend}</div>'


def _svg_histogram(values: Sequence[float], *, width: int = 490, height: int = 170,
                   color: str = "#ff4d4f", title: str = "",
                   unit: str = "", bins: int = 36) -> str:
    pad_l, pad_r, pad_t, pad_b = 40, 12, 22, 28
    x0, y0 = pad_l, pad_t
    x1, y1 = width - pad_r, height - pad_b
    parts = [f'<svg class="chart" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">']
    if title:
        parts.append(f'<text x="{x0 - 30:.0f}" y="12" fill="#eaf2fa" font-size="11" '
                     f'font-weight="600">{html.escape(title)}</text>')
    if not values:
        parts.append(f'<text x="{width / 2}" y="{height / 2}" fill="#6b7d90" '
                     f'font-size="12" text-anchor="middle">no data</text></svg>')
        return "".join(parts)
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        hi = lo + 1.0
    counts = [0] * bins
    for v in values:
        idx = min(bins - 1, max(0, int((v - lo) / (hi - lo) * bins)))
        counts[idx] += 1
    peak = max(counts) or 1
    bar_w = (x1 - x0) / bins
    for i, c in enumerate(counts):
        if not c:
            continue
        bh = (c / peak) * (y1 - y0)
        parts.append(f'<rect x="{x0 + i * bar_w:.2f}" y="{y1 - bh:.2f}" '
                     f'width="{max(1.0, bar_w - 1):.2f}" height="{bh:.2f}" '
                     f'fill="{color}" opacity="0.75"/>')
    parts.append(f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" stroke="#1e2a38"/>')
    for i in range(5):
        gx = x0 + (x1 - x0) * i / 4
        val = lo + (hi - lo) * i / 4
        parts.append(f'<text x="{gx:.1f}" y="{y1 + 14:.1f}" fill="#6b7d90" '
                     f'font-size="10" text-anchor="middle">{val:.0f}</text>')
    parts.append(f'<text x="{x0 - 6:.1f}" y="{y0 + 4:.1f}" fill="#6b7d90" '
                 f'font-size="10" text-anchor="end">{peak}</text>')
    parts.append(f'<text x="{x1:.1f}" y="{y1 + 24:.1f}" fill="#6b7d90" '
                 f'font-size="10" text-anchor="end">{html.escape(unit)}</text>')
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------
# HTML report
# --------------------------------------------------------------------------


def _fmt(value: float | None, precision: int = 1) -> str:
    if value is None:
        return "--"
    return f"{value:.{precision}f}"


def build_html_report(store: S.Store, session_id: int,
                      devices: Sequence[M.GpuDevice] = ()) -> str:
    session = store.get_session(session_id)
    if session is None:
        return "<html><body>Session not found.</body></html>"
    stats = store.stats_many(session_id)
    alarm_events = store.alarms(session_id)
    markers = store.markers(session_id)
    devs = list(devices) or [
        M.GpuDevice(index=d.index, name=d.name, vendor=d.vendor,
                    vram_total_mb=d.vram_total_mb, sources=d.sources, note=d.note,
                    pci=getattr(d, "pci", ""))
        for d in session.devices]

    started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(session.started_at))
    ended = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(session.ended_at))
             if session.ended_at else "in progress")

    out: list[str] = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>",
        f"<title>gpumon report - session {session_id}</title>",
        f"<style>{REPORT_CSS}</style></head><body>",
        f"<h1>gpumon report &mdash; session #{session_id}"
        + (f" &mdash; {html.escape(session.label)}" if session.label else "")
        + "</h1>",
        f"<div class='sub'>started {started} &middot; ended {ended} &middot; "
        f"duration {S.human_duration(session.duration)} &middot; "
        f"{session.sample_count} samples @ {session.sample_hz:.2f} Hz target &middot; "
        f"generated {time.strftime('%Y-%m-%d %H:%M:%S')}</div>",
    ]

    # ---- verdict ------------------------------------------------------
    if alarm_events:
        crit = [e for e in alarm_events if e.level == "critical"]
        cls = "crit" if crit else "warn"
        worst = max(alarm_events, key=lambda e: e.peak)
        out.append(
            f"<div class='card'><h3 class='{cls}'>"
            f"{len(alarm_events)} threshold crossing(s)</h3>"
            f"<div class='note'>{len(crit)} critical, "
            f"{len(alarm_events) - len(crit)} warning. Highest reading: "
            f"{html.escape(M.metric_def(worst.metric).label)} at "
            f"{worst.peak:.0f} {M.metric_def(worst.metric).unit} "
            f"(threshold {worst.threshold:.0f}).</div></div>")
    else:
        out.append("<div class='card'><h3 class='ok'>No alarm thresholds "
                   "crossed</h3><div class='note'>Every monitored metric stayed "
                   "below its configured warning level for the whole run."
                   "</div></div>")

    # ---- per-device summary cards -------------------------------------
    out.append("<h2>Devices</h2>")
    for dev in devs:
        i = dev.index
        out.append("<div class='card'><h3>"
                   f"gpu{i} &mdash; {html.escape(dev.name)}</h3>"
                   f"<div class='note'>{html.escape(dev.pci or 'PCI location unknown')}"
                   f" &middot; sources: {html.escape(', '.join(dev.sources) or 'none')}"
                   + (f" &middot; {dev.vram_total_mb:.0f} MB VRAM"
                      if dev.vram_total_mb else "")
                   + "</div><table>")
        rows = [("Max temperature", f"gpu{i}_temp", "C", 0),
                ("Average temperature", f"gpu{i}_temp", "C", 1),
                ("Peak utilisation", f"gpu{i}_util", "%", 0),
                ("Average utilisation", f"gpu{i}_util", "%", 1),
                ("Peak VRAM", f"gpu{i}_vram_percent", "%", 1),
                ("Peak power", f"gpu{i}_power", "W", 1),
                ("Peak core clock", f"gpu{i}_clock_core", "MHz", 0)]
        for label, key, unit, mode in rows:
            st = stats.get(key)
            if st is None:
                continue
            val = st.maximum if mode == 0 else st.mean
            out.append(f"<tr><td>{html.escape(label)}</td>"
                       f"<td class='num'>{val:.{1 if unit in ('C', '%', 'W') else 0}f} "
                       f"{unit}</td></tr>")
        out.append("</table></div>")
    out.append("<div style='clear:both'></div>")

    # ---- charts per device --------------------------------------------
    chart_markers = [(e.started_t, f"{M.metric_def(e.metric).label} {e.level}",
                      "#ff4d4f" if e.level == "critical" else "#ffb020")
                     for e in alarm_events]
    chart_markers += [(t, lbl, "#3ea6ff") for t, _w, lbl, _k in markers]

    for dev in devs:
        i = dev.index
        out.append(f"<h2>gpu{i} &mdash; {html.escape(dev.name)} performance</h2>")
        # One chart per unit: mixing °C with MHz on a shared axis flattens the
        # temperature trace, which is the single most important signal in a
        # thermal stress test.
        charts = [
            ("GPU utilisation", "%",
             [(f"gpu{i}_util", "utilisation", PALETTE[0]),
              (f"gpu{i}_mem_util", "memory bandwidth", PALETTE[2])], []),
            ("Temperature", "C",
             [(f"gpu{i}_hotspot", "hotspot (junction)", "#ffb020"),
              (f"gpu{i}_temp", "GPU core", "#ff4d4f"),
              (f"gpu{i}_mem_temp", "memory temp", PALETTE[3])],
             [(80.0, 110.0, "#2a1616", "hot zone")]),
            ("VRAM usage", "MB",
             [(f"gpu{i}_vram_used", "VRAM used", PALETTE[2])], []),
            ("Clocks", "MHz",
             [(f"gpu{i}_clock_core", "core clock", PALETTE[1]),
              (f"gpu{i}_clock_mem", "memory clock", PALETTE[4]),
              (f"gpu{i}_clock_sm", "SM clock", PALETTE[5])], []),
            ("Power", "W",
             [(f"gpu{i}_power", "board power", PALETTE[3]),
              (f"gpu{i}_power_limit", "power limit", PALETTE[6])], []),
        ]
        for title, unit, defs, bands in charts:
            series = []
            for key, label, color in defs:
                pts = store.series(session_id, key)
                if len(pts) >= 2:
                    series.append((f"{label} ({unit})", color, pts))
            if not series:
                continue
            out.append(f"<h3 style='font-size:13px;color:#c8d4e0'>{title}</h3>")
            out.append(_svg_line_chart(series, y_label=unit, bands=bands,
                                       markers=chart_markers))
        temps = [v for _t, v in store.series(session_id, f"gpu{i}_temp")]
        utils = [v for _t, v in store.series(session_id, f"gpu{i}_util")]
        if temps or utils:
            out.append("<div>" +
                       _svg_histogram(temps, title="Temperature distribution",
                                      unit="C", color="#ff4d4f") +
                       " " +
                       _svg_histogram(utils, title="Utilisation distribution",
                                      unit="%", color="#3ea6ff") +
                       "</div>")

    # ---- alarms -------------------------------------------------------
    out.append("<h2>Alarm timeline</h2>")
    if alarm_events:
        out.append("<table><tr><th>metric</th><th>level</th><th>threshold</th>"
                   "<th>peak</th><th>crossed at</th><th>cleared at</th>"
                   "<th>duration</th><th>samples</th></tr>")
        for ev in alarm_events:
            d = M.metric_def(ev.metric)
            prefix = f"gpu{ev.gpu_index} " if ev.gpu_index is not None else ""
            cls = "crit" if ev.level == "critical" else "warn"
            out.append(
                f"<tr><td>{html.escape(prefix + d.label)}</td>"
                f"<td class='{cls}'>{ev.level}</td>"
                f"<td class='num'>{ev.threshold:.0f}</td>"
                f"<td class='num {cls}'>{ev.peak:.0f}</td>"
                f"<td class='num'>{ev.started_t:.1f}s</td>"
                f"<td class='num'>"
                + (f"{ev.ended_t:.1f}s" if ev.ended_t is not None else "&mdash;")
                + f"</td><td class='num'>"
                + (S.human_duration(ev.duration) if ev.duration is not None
                   else "to end of run")
                + f"</td><td class='num'>{ev.samples}</td></tr>")
        out.append("</table>")
    else:
        out.append("<p class='empty'>No thresholds were crossed during this run.</p>")

    if markers:
        out.append("<h3 style='font-size:13px'>User markers</h3><table>"
                   "<tr><th>time</th><th>label</th></tr>")
        for t, _wall, label, _kind in markers:
            out.append(f"<tr><td class='num'>{t:.1f}s</td>"
                       f"<td>{html.escape(label)}</td></tr>")
        out.append("</table>")

    # ---- statistics ---------------------------------------------------
    out.append("<h2>All statistics</h2><table><tr><th>metric</th><th>unit</th>"
               "<th>min</th><th>avg</th><th>max</th><th>median</th><th>p95</th>"
               "<th>p99</th><th>stdev</th><th>samples</th></tr>")
    for key in sorted(stats):
        st = stats[key]
        d = M.metric_def(key)
        gpu = M.gpu_index_of(key)
        label = f"gpu{gpu} {d.label}" if gpu is not None else d.label
        prec = 1 if d.unit in ("%", "C", "W") else 0
        out.append(
            f"<tr><td>{html.escape(label)}</td><td class='dim'>{html.escape(d.unit)}</td>"
            f"<td class='num'>{st.minimum:.{prec}f}</td>"
            f"<td class='num'>{st.mean:.{prec}f}</td>"
            f"<td class='num'>{st.maximum:.{prec}f}</td>"
            f"<td class='num'>{st.median:.{prec}f}</td>"
            f"<td class='num'>{st.p95:.{prec}f}</td>"
            f"<td class='num'>{st.p99:.{prec}f}</td>"
            f"<td class='num'>{st.stdev:.{prec}f}</td>"
            f"<td class='num'>{st.count}</td></tr>")
    out.append("</table>")

    # ---- notes --------------------------------------------------------
    notes = (session.capabilities or {}).get("notes") or []
    errs = (session.capabilities or {}).get("backend_errors") or {}
    if notes or errs:
        out.append("<h2>Sensor coverage</h2><ul>")
        for note in notes:
            out.append(f"<li>{html.escape(note)}</li>")
        for key, val in errs.items():
            out.append(f"<li><code>{html.escape(key)}</code>: {html.escape(str(val))}"
                       "</li>")
        out.append("</ul>")

    out.append(f"<p class='dim'>gpumon {html.escape(session.app_version or '')} "
               f"&middot; database <code>{html.escape(store.path)}</code></p>")
    out.append("</body></html>")
    return "".join(out)


# --------------------------------------------------------------------------
# Plain text report
# --------------------------------------------------------------------------


def build_text_report(store: S.Store, session_id: int,
                      devices: Sequence[M.GpuDevice] = ()) -> str:
    session = store.get_session(session_id)
    if session is None:
        return f"Session {session_id} not found."
    stats = store.stats_many(session_id)
    alarm_events = store.alarms(session_id)
    devs = list(devices) or [
        M.GpuDevice(index=d.index, name=d.name, vendor=d.vendor,
                    vram_total_mb=d.vram_total_mb, sources=d.sources, note=d.note,
                    pci=getattr(d, "pci", ""))
        for d in session.devices]
    line = "=" * 78
    out: list[str] = [
        line,
        f"gpumon report - session #{session_id}"
        + (f" - {session.label}" if session.label else ""),
        line,
        f"started  : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(session.started_at))}",
        f"ended    : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(session.ended_at))}"
        if session.ended_at else "ended    : in progress",
        f"duration : {S.human_duration(session.duration)}",
        f"samples  : {session.sample_count} at {session.sample_hz:.2f} Hz target",
        f"database : {store.path}",
        "",
    ]
    out.append("DEVICES")
    out.append("-" * 78)
    for dev in devs:
        vram = f"{dev.vram_total_mb:.0f} MB" if dev.vram_total_mb else "unknown"
        out.append(f"  gpu{dev.index}  {dev.name}")
        out.append(f"         {getattr(dev, 'pci', '') or 'PCI location unknown'}"
                   f"  vendor={dev.vendor}  vram={vram}")
        out.append(f"         sources={','.join(dev.sources) or 'none'}")
        if dev.note:
            out.append(f"         note: {dev.note}")
    out.append("")
    out.append("KEY NUMBERS")
    out.append("-" * 78)
    for dev in devs:
        i = dev.index
        out.append(f"  gpu{i} {dev.name}")
        for label, key, unit, mode in (
                ("max temp", f"gpu{i}_temp", "C", 0),
                ("avg temp", f"gpu{i}_temp", "C", 1),
                ("max util", f"gpu{i}_util", "%", 0),
                ("avg util", f"gpu{i}_util", "%", 1),
                ("max vram", f"gpu{i}_vram_percent", "%", 1),
                ("max power", f"gpu{i}_power", "W", 1)):
            st = stats.get(key)
            if st is None:
                continue
            val = st.maximum if mode == 0 else st.mean
            prec = 1 if unit in ("C", "%", "W") else 0
            out.append(f"    {label:<12} {val:>10.{prec}f} {unit}")
    for label, key, unit in (("cpu max util", "cpu_util", "%"),
                             ("cpu max temp", "cpu_temp", "C"),
                             ("ram max util", "ram_percent", "%")):
        st = stats.get(key)
        if st:
            out.append(f"  {label:<16} {st.maximum:>10.1f} {unit}")
    out.append("")
    out.append("ALARMS")
    out.append("-" * 78)
    if not alarm_events:
        out.append("  none - no threshold was crossed during this run")
    for ev in alarm_events:
        d = M.metric_def(ev.metric)
        prefix = f"gpu{ev.gpu_index} " if ev.gpu_index is not None else ""
        out.append(f"  [{ev.level.upper():8}] {prefix}{d.label}: peak {ev.peak:.0f} "
                   f"{d.unit} vs threshold {ev.threshold:.0f} {d.unit}, "
                   f"crossed at {ev.started_t:.1f}s"
                   + (f", cleared at {ev.ended_t:.1f}s "
                      f"({S.human_duration(ev.duration)})"
                      if ev.ended_t is not None else ", still open at stop"))
    out.append("")
    out.append("ALL STATISTICS")
    out.append("-" * 78)
    out.append(f"  {'metric':<30}{'unit':<7}{'min':>9}{'avg':>9}{'max':>9}"
               f"{'p95':>9}{'p99':>9}{'n':>7}")
    for key in sorted(stats):
        st = stats[key]
        d = M.metric_def(key)
        gpu = M.gpu_index_of(key)
        label = f"gpu{gpu} {d.label}" if gpu is not None else d.label
        prec = 1 if d.unit in ("%", "C", "W") else 0
        out.append(f"  {label[:29]:<30}{d.unit:<7}"
                   f"{st.minimum:>9.{prec}f}{st.mean:>9.{prec}f}"
                   f"{st.maximum:>9.{prec}f}{st.p95:>9.{prec}f}"
                   f"{st.p99:>9.{prec}f}{st.count:>7}")
    notes = (session.capabilities or {}).get("notes") or []
    if notes:
        out.append("")
        out.append("SENSOR COVERAGE NOTES")
        out.append("-" * 78)
        for note in notes:
            out.append(f"  - {note}")
    out.append("")
    return "\n".join(out)
