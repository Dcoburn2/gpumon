"""Post-run summary window: statistics, charts and the alarm timeline.

Everything is derived from the SQLite session, so the same window works for a
just-finished run and for a session loaded from a previous day.
"""
from __future__ import annotations

import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Sequence

import metrics as M
import store as S
from store import _percentile
from ui import widgets as W


class SummaryWindow:
    """Top-level report window for one logged session."""

    def __init__(self, parent: tk.Tk, store: S.Store, session_id: int,
                 theme: W.Theme, *, devices: Sequence[M.GpuDevice] = (),
                 on_close: Callable[[int], None] | None = None) -> None:
        self.store = store
        self.session_id = session_id
        self.theme = theme
        self._on_close = on_close
        self.session = store.get_session(session_id)
        if self.session is None:
            messagebox.showerror("Missing session", f"Session {session_id} not found.")
            return
        # Full extent of the recording, used whenever no window is selected.
        self.full_span = store.sample_span(session_id) or (0.0, 1.0)
        #: Chosen sub-window (t0, t1). None means the whole run.
        self.window: tuple[float, float] | None = None
        self._charts: list[W.TimeSeriesChart] = []
        #: Overview page body, rebuilt in place when the window changes.
        self.overview_body: tk.Frame | None = None
        #: Per-GPU page bodies, keyed by GPU index.
        self._gpu_bodies: dict[int, tk.Frame] = {}
        #: Histograms and statistics tables, updated in place.
        self._histograms: dict[tuple[int, str], W.HistogramChart] = {}
        self._tables: dict[str, W.StatTable] = {}
        self.metrics = store.metrics_for_session(session_id)
        self.stats = store.stats_many(session_id)
        self.alarm_events = store.alarms(session_id)
        self.markers = store.markers(session_id)

        # Prefer live device info; fall back to what was recorded at log time.
        self.devices = list(devices) or [
            M.GpuDevice(index=d.index, name=d.name, vendor=d.vendor,
                        vram_total_mb=d.vram_total_mb, sources=d.sources,
                        note=d.note, pci=getattr(d, "pci", ""))
            for d in self.session.devices]

        self.win = tk.Toplevel(parent)
        self.win.title(f"gpumon summary - session #{session_id}"
                       + (f" - {self.session.label}" if self.session.label else ""))
        self.win.configure(bg=W.BG)
        self.win.geometry("1240x840")
        self.win.minsize(900, 560)
        # Maximise/minimise work because this is a real top-level window, not a
        # transient dialog.
        self.win.resizable(True, True)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.attributes("-toolwindow", False)
        self._build()
        self.focus()

    # ------------------------------------------------------------------
    @property
    def alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def focus(self) -> None:
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def close(self) -> None:
        if self._on_close:
            self._on_close(self.session_id)
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build(self) -> None:
        t = self.theme
        header = tk.Frame(self.win, bg=W.PANEL_ALT, highlightthickness=1,
                          highlightbackground=W.BORDER)
        header.pack(fill="x")
        left = tk.Frame(header, bg=W.PANEL_ALT)
        left.pack(side="left", padx=12, pady=8)
        title = f"Session #{self.session_id}"
        if self.session.label:
            title += f"  -  {self.session.label}"
        tk.Label(left, text=title, bg=W.PANEL_ALT, fg=W.TEXT_BRIGHT,
                 font=t.title).pack(anchor="w")
        started = time.strftime("%Y-%m-%d %H:%M:%S",
                                time.localtime(self.session.started_at))
        tk.Label(left, text=f"started {started}    duration "
                            f"{S.human_duration(self.session.duration)}    "
                            f"{len(self.metrics)} metrics    "
                            f"{self.session.sample_count} samples",
                 bg=W.PANEL_ALT, fg=W.TEXT_DIM, font=t.small).pack(anchor="w")
        right = tk.Frame(header, bg=W.PANEL_ALT)
        right.pack(side="right", padx=12, pady=8)
        W.FlatButton(right, " EXPORT REPORT ", self.export_report, theme=t
                     ).pack(side="left", padx=3)
        W.FlatButton(right, " EXPORT CSV ", self.export_csv, theme=t
                     ).pack(side="left", padx=3)
        W.FlatButton(right, " CLOSE ", self.close, theme=t).pack(side="left", padx=3)

        self.alarm_banner = tk.Label(self.win, text="", bg=W.BG, font=t.bold,
                                     anchor="w", justify="left", wraplength=1180)
        self.alarm_banner.pack(fill="x", padx=12, pady=(8, 0))

        # ---- analysis window selector ----------------------------------
        # Drag across any chart to pick a sub-range; every tab then reports on
        # that window only, which is what you want when a warm-up phase or a
        # benchmark step should be excluded.
        range_bar = tk.Frame(self.win, bg=W.PANEL_ALT, highlightthickness=1,
                             highlightbackground=W.BORDER)
        range_bar.pack(fill="x", padx=12, pady=(6, 0))
        tk.Label(range_bar, text="ANALYSIS WINDOW", bg=W.PANEL_ALT,
                 fg=W.TEXT_DIM, font=t.small).pack(side="left", padx=(8, 6), pady=5)
        self.range_label = tk.Label(range_bar, text="", bg=W.PANEL_ALT,
                                    fg=W.TEXT, font=t.small, anchor="w")
        self.range_label.pack(side="left")
        W.FlatButton(range_bar, " FULL RUN ", self.use_full_range, theme=t
                     ).pack(side="right", padx=6, pady=3)
        self.btn_last_quarter = W.FlatButton(
            range_bar, " LAST 25% ", lambda: self.use_fraction(0.75, 1.0), theme=t)
        self.btn_last_quarter.pack(side="right", padx=3, pady=3)
        self.btn_last_half = W.FlatButton(
            range_bar, " LAST 50% ", lambda: self.use_fraction(0.5, 1.0), theme=t)
        self.btn_last_half.pack(side="right", padx=3, pady=3)
        tk.Label(range_bar, text="drag on any chart to select",
                 bg=W.PANEL_ALT, fg=W.TEXT_DIM, font=t.tiny).pack(
            side="right", padx=8)
        self._update_range_label()

        style = ttk.Style(self.win)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TNotebook", background=W.BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=W.PANEL, foreground=W.TEXT,
                        padding=(14, 6), font=t.ui_body)
        style.map("TNotebook.Tab",
                  background=[("selected", W.PANEL_ALT)],
                  foreground=[("selected", W.ACCENT)])

        self.nb = ttk.Notebook(self.win)
        self.nb.pack(fill="both", expand=True, padx=10, pady=8)
        #: One callback per scrollable page, re-run when the tab changes: only
        #: the selected page may show a scrollbar (see _scrollable).
        self._bar_refreshers: list = []
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._build_overview()
        for dev in self.devices:
            self._build_gpu_tab(dev)
        self._build_alarms_tab()
        self._build_config_tab()

        self._render_alarm_banner()

    # ------------------------------------------------------------------
    # Analysis window
    # ------------------------------------------------------------------
    def _on_tab_changed(self, _event: tk.Event) -> None:
        """Let each page decide again whether it needs a scrollbar."""
        for refresh in getattr(self, "_bar_refreshers", []):
            refresh()

    def _update_range_label(self) -> None:
        if self.window is None:
            lo, hi = self.full_span
            self.range_label.configure(
                text=f"whole run   0.0s - {hi:.1f}s  ({hi - lo:.1f}s)",
                fg=W.TEXT)
            return
        lo, hi = self.window
        self.range_label.configure(
            text=f"{lo:.1f}s - {hi:.1f}s  ({hi - lo:.1f}s of "
                 f"{self.full_span[1] - self.full_span[0]:.1f}s)",
            fg=W.ACCENT)

    def use_full_range(self) -> None:
        self.window = None
        self._refresh_all()

    def use_fraction(self, start: float, end: float) -> None:
        """Select a fraction of the run, e.g. skip a warm-up phase."""
        lo, hi = self.full_span
        span = hi - lo
        self.window = (lo + span * start, lo + span * end)
        self._refresh_all()

    def set_window(self, t0: float, t1: float) -> None:
        """Called when the user drags out a range on a chart."""
        lo, hi = self.full_span
        t0 = max(lo, min(t0, hi))
        t1 = max(lo, min(t1, hi))
        if t1 - t0 < 0.1:
            return
        self.window = (t0, t1)
        self._refresh_all()

    def _refresh_all(self) -> None:
        """Recompute statistics and update every chart for the chosen window.

        Charts are updated **in place** rather than rebuilt. Rebuilding would
        destroy the very widget whose event handler is running (the drag that
        selected the window), and it would also throw away the user's scroll
        position.

        Charts keep showing the whole run with the chosen window shaded, so the
        selection stays visible and can be adjusted by dragging again. The
        statistics, histograms and tables report the window only.
        """
        t0, t1 = self.window if self.window else (None, None)
        self.stats = self.store.stats_many(self.session_id, t0, t1)
        self._update_range_label()
        for chart in self._charts:
            if not chart.winfo_exists():
                continue
            chart.selection = self.window
            chart.render()
        self._refresh_histograms()
        self._refresh_stat_tables()
        self._render_alarm_banner()

    def _refresh_histograms(self) -> None:
        t0, t1 = self.window if self.window else (None, None)
        for index, device in enumerate(self.devices):
            i = device.index
            temps = [v for _t, v in
                     self.store.series(self.session_id, f"gpu{i}_temp", t0, t1)]
            utils = [v for _t, v in
                     self.store.series(self.session_id, f"gpu{i}_util", t0, t1)]
            st = self.stats.get(f"gpu{i}_temp")
            notes = []
            if st:
                notes = [(st.median, "median", W.TEXT_DIM),
                         (st.p95, "p95", W.WARN), (st.p99, "p99", W.CRIT)]
            for kind, values in (("temp", temps), ("util", utils)):
                chart = self._histograms.get((i, kind))
                if chart is not None and chart.winfo_exists():
                    chart.set_values(
                        values,
                        title=("Temperature distribution" if kind == "temp"
                               else "Utilisation distribution"),
                        unit=("C" if kind == "temp" else "%"),
                        notes=notes if kind == "temp" else [])

    def _refresh_stat_tables(self) -> None:
        for prefix, table in self._tables.items():
            if not table.winfo_exists():
                continue
            keys = [m for m in self.metrics if m.startswith(prefix)]
            table.set_rows(self._stat_rows_extended(keys))

    def _stat_rows(self, keys: Sequence[str]) -> list[list[str]]:
        rows = []
        for key in keys:
            st = self.stats.get(key)
            d = M.metric_def(key)
            if st is None:
                rows.append([d.label, d.unit] + ["--"] * 8)
                continue
            prec = 1 if d.unit in ("%", "W", "C") else 0
            rows.append([d.label, d.unit,
                         f"{st.minimum:.{prec}f}", f"{st.mean:.{prec}f}",
                         f"{st.maximum:.{prec}f}", f"{st.median:.{prec}f}",
                         f"{st.p95:.{prec}f}", f"{st.p99:.{prec}f}",
                         f"{st.stdev:.{prec}f}", str(st.count)])
        return rows

    def _scrollable(self, parent: tk.Misc) -> tuple[tk.Canvas, tk.Frame]:
        """Create a scrollable tab page.

        Returns (page, body): pass `page` to Notebook.add (the widget must be a
        direct child of the notebook) and fill `body`.

        The scrollbar is hidden while the content fits, so tabs that do not
        overflow do not carry a dead scrollbar next to the text.
        """
        canvas = tk.Canvas(parent, bg=W.BG, highlightthickness=0, bd=0)
        vbar = W.ThemedScrollbar(parent, orient="vertical",
                                 command=canvas.yview)
        inner = tk.Frame(canvas, bg=W.BG)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vbar.set)
        canvas.pack(side="left", fill="both", expand=True)

        state = {"outer": 0, "inner": 0, "bar_visible": False}

        def refresh_bar() -> None:
            # Every bar is a child of the notebook rather than of its own tab
            # (a tab page has to be added directly to the notebook, which leaves
            # the bar as its sibling). Without the selection test, a tab whose
            # content overflows packs its bar while another tab is on screen -
            # and four overflowing tabs stacked four bars along the right edge.
            visible = self.nb.select() == str(canvas)
            needed = (state["inner"] > state["outer"] + 2) and visible
            if needed and not state["bar_visible"]:
                vbar.pack(side="right", fill="y")
                state["bar_visible"] = True
            elif not needed and state["bar_visible"]:
                vbar.pack_forget()
                state["bar_visible"] = False

        # Re-evaluated when the user switches tab, since which bar may show now
        # depends on the selection.
        self._bar_refreshers.append(refresh_bar)

        def _resize(event: tk.Event) -> None:
            canvas.itemconfigure(window, width=event.width)
            state["outer"] = event.height
            refresh_bar()

        def _configure(_event: tk.Event) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            state["inner"] = inner.winfo_reqheight()
            refresh_bar()

        def _wheel(event: tk.Event) -> None:
            canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind("<Configure>", _resize)
        inner.bind("<Configure>", _configure)
        # Bind the wheel only while the pointer is over this page, otherwise
        # every page would scroll together.
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        return canvas, inner

    # ------------------------------------------------------------------
    # Overview tab
    # ------------------------------------------------------------------
    def _build_overview(self) -> None:
        t = self.theme
        # Keep a handle so the content can be rebuilt when the analysis window
        # changes, without creating duplicate tab pages.
        if self.overview_body is not None and self.overview_body.winfo_exists():
            for child in self.overview_body.winfo_children():
                child.destroy()
            page = self.overview_body
        else:
            page_widget, page = self._scrollable(self.nb)
            self.nb.add(page_widget, text="Overview")
            self.overview_body = page

        # headline metrics, per GPU
        cards = tk.Frame(page, bg=W.BG)
        cards.pack(fill="x", padx=6, pady=(6, 2))
        for dev in self.devices:
            i = dev.index
            frame = tk.Frame(cards, bg=W.PANEL, highlightthickness=1,
                             highlightbackground=W.BORDER)
            frame.pack(side="left", fill="both", expand=True, padx=4, pady=4)
            tk.Label(frame, text=f"gpu{i}  {dev.name}", bg=W.PANEL,
                     fg=W.TEXT_BRIGHT, font=t.bold, anchor="w").pack(
                anchor="w", padx=8, pady=(6, 0))
            ident = getattr(dev, "pci", "") or "PCI location unknown"
            tk.Label(frame, text=f"{ident}   [{', '.join(dev.sources) or 'no source'}]",
                     bg=W.PANEL, fg=W.ACCENT, font=t.tiny, anchor="w",
                     wraplength=280, justify="left").pack(anchor="w", padx=8)
            if dev.note:
                tk.Label(frame, text=dev.note, bg=W.PANEL, fg=W.TEXT_DIM,
                         font=t.tiny, anchor="w", wraplength=280,
                         justify="left").pack(anchor="w", padx=8)
            # A small table reads better than a wall of prose for this.
            body = tk.Frame(frame, bg=W.PANEL)
            body.pack(anchor="w", fill="x", padx=8, pady=(4, 8))
            rows = []
            for field, label, unit in (("temp", "temperature", "C"),
                                       ("hotspot", "hotspot", "C"),
                                       ("mem_temp", "memory temp", "C"),
                                       ("util", "utilisation", "%"),
                                       ("vram_percent", "VRAM used", "%"),
                                       ("clock_core", "core clock", "MHz"),
                                       ("power", "board power", "W"),
                                       ("fan", "fan", "%")):
                st = self.stats.get(f"gpu{i}_{field}")
                if st is None:
                    continue
                prec = 0 if unit in ("MHz", "C") else 1
                rows.append([label,
                             f"{st.mean:.{prec}f}",
                             f"{st.minimum:.{prec}f}",
                             f"{st.maximum:.{prec}f}",
                             f"{st.p95:.{prec}f}", unit])
            if rows:
                table = W.StatTable(
                    body,
                    [("metric", 13, "w"), ("avg", 7, "e"), ("low", 7, "e"),
                     ("high", 7, "e"), ("p95", 7, "e"), ("unit", 5, "w")],
                    theme=t, rows=len(rows))
                table.pack(anchor="w")
                table.set_rows(rows)

        # system card
        sysframe = tk.Frame(page, bg=W.PANEL, highlightthickness=1,
                            highlightbackground=W.BORDER)
        sysframe.pack(fill="x", padx=10, pady=6)
        tk.Label(sysframe, text="CPU & MEMORY", bg=W.PANEL, fg=W.TEXT_BRIGHT,
                 font=t.bold, anchor="w").pack(anchor="w", padx=8, pady=(6, 2))
        body = tk.Frame(sysframe, bg=W.PANEL)
        body.pack(anchor="w", padx=8, pady=(0, 8))
        for key, label, unit in (("cpu_util", "CPU utilisation", "%"),
                                 ("cpu_temp", "CPU temperature", "C"),
                                 ("ram_percent", "RAM utilisation", "%"),
                                 ("ram_used", "RAM used", "MB")):
            st = self.stats.get(key)
            row = tk.Frame(body, bg=W.PANEL)
            row.pack(anchor="w")
            tk.Label(row, text=label, bg=W.PANEL, fg=W.TEXT_DIM, font=t.small,
                     width=17, anchor="w").pack(side="left")
            if st is None:
                # "no sensor or backend" read like a glitch in the app. The
                # session recorded why, so say why - a reader who expected CPU
                # temperatures needs to know it is LibreHardwareMonitor that is
                # missing, not the data.
                tk.Label(row, text=self._missing_reason(key),
                         bg=W.PANEL, fg=W.WARN, font=t.small,
                         anchor="w").pack(side="left")
                continue
            tk.Label(row, text=f"avg {st.mean:8.1f} {unit}   "
                               f"min {st.minimum:8.1f}   max {st.maximum:8.1f}   "
                               f"p95 {st.p95:8.1f}   p99 {st.p99:8.1f}   "
                               f"n {st.count}",
                     bg=W.PANEL, fg=W.TEXT, font=t.small, anchor="w").pack(side="left")

        # alarm roll-up
        alarm_frame = tk.Frame(page, bg=W.PANEL, highlightthickness=1,
                               highlightbackground=W.BORDER)
        alarm_frame.pack(fill="x", padx=10, pady=6)
        tk.Label(alarm_frame, text="ALARM ROLL-UP", bg=W.PANEL, fg=W.TEXT_BRIGHT,
                 font=t.bold, anchor="w").pack(anchor="w", padx=8, pady=(6, 2))
        if not self.alarm_events:
            tk.Label(alarm_frame, text="No thresholds were crossed during this run.",
                     bg=W.PANEL, fg=W.OK, font=t.body, anchor="w").pack(
                anchor="w", padx=8, pady=(0, 8))
        else:
            table = W.StatTable(
                alarm_frame,
                [("metric", 24, "w"), ("level", 9, "w"), ("threshold", 10, "e"),
                 ("peak", 8, "e"), ("crossed at", 10, "e"),
                 ("duration", 10, "e"), ("status", 10, "w")],
                theme=t, rows=len(self.alarm_events))
            table.pack(anchor="w", padx=8, pady=(0, 8))
            rows, colors = [], []
            for ev in self.alarm_events:
                d = M.metric_def(ev.metric)
                prefix = f"gpu{ev.gpu_index} " if ev.gpu_index is not None else ""
                col = W.CRIT if ev.level == "critical" else W.WARN
                rows.append([
                    f"{prefix}{d.label}", ev.level.upper(), f"{ev.threshold:.0f}",
                    f"{ev.peak:.0f}", f"{ev.started_t:.1f}s",
                    S.human_duration(ev.duration) if ev.duration is not None
                    else "to end of run",
                    "still open at stop" if ev.ongoing else "cleared"])
                colors.append([W.TEXT, col, W.TEXT_DIM, col, W.TEXT, W.TEXT, W.TEXT_DIM])
            table.set_rows(rows, colors)

        notes = (self.session.capabilities or {}).get("notes") or []
        if notes:
            nf = tk.Frame(page, bg=W.PANEL_ALT, highlightthickness=1,
                          highlightbackground=W.BORDER)
            nf.pack(fill="x", padx=10, pady=(2, 10))
            tk.Label(nf, text="SENSOR COVERAGE NOTES", bg=W.PANEL_ALT,
                     fg=W.TEXT_BRIGHT, font=t.bold, anchor="w").pack(
                anchor="w", padx=8, pady=(6, 0))
            for note in notes:
                # A note from an older run may carry advice that has since been
                # superseded; the fact is kept and the stale instruction dropped.
                tk.Label(nf, text=f"- {M.modernise_note(note)}", bg=W.PANEL_ALT,
                         fg=W.TEXT_DIM, font=t.small, anchor="w", wraplength=1140,
                         justify="left").pack(anchor="w", padx=8)
            tk.Label(nf, text="", bg=W.PANEL_ALT).pack(pady=2)

    # ------------------------------------------------------------------
    # Per-GPU tabs
    # ------------------------------------------------------------------
    def _build_gpu_tab(self, dev: M.GpuDevice) -> None:
        t = self.theme
        tab_label = f"gpu{dev.index}"
        body = self._gpu_bodies.get(dev.index)
        if body is not None and body.winfo_exists():
            for child in body.winfo_children():
                child.destroy()
            page = body
        else:
            page_widget, page = self._scrollable(self.nb)
            self.nb.add(page_widget, text=tab_label)
            self._gpu_bodies[dev.index] = page
        i = dev.index
        span = self.window if self.window else self.full_span
        t0, t1 = self.window if self.window else (None, None)

        # One chart per unit. Mixing °C with MHz on a shared axis flattens the
        # temperature trace into a line, which defeats the point of the report.
        chart_defs = [
            ("Utilisation over time", "%",
             [(f"gpu{i}_util", "GPU utilisation", W.SERIES_COLORS[0]),
              (f"gpu{i}_mem_util", "memory bandwidth", W.SERIES_COLORS[2])], []),
            # Hotspot first on the temperature chart: it is the number that
            # actually limits a GPU, and edge temperature alone hides it.
            ("Temperature over time", "C",
             [(f"gpu{i}_hotspot", "hotspot (junction)", W.WARN),
              (f"gpu{i}_temp", "GPU core", W.CRIT),
              (f"gpu{i}_mem_temp", "memory", W.SERIES_COLORS[3])],
             [(80.0, 110.0, "#2a1616", "hot zone")]),
            ("VRAM usage over time", "MB",
             [(f"gpu{i}_vram_used", "VRAM used (MB)", W.SERIES_COLORS[2])], []),
            ("Clocks over time", "MHz",
             [(f"gpu{i}_clock_core", "core clock", W.SERIES_COLORS[1]),
              (f"gpu{i}_clock_mem", "memory clock", W.SERIES_COLORS[4]),
              (f"gpu{i}_clock_sm", "SM clock", W.SERIES_COLORS[5])], []),
            ("Power over time", "W",
             [(f"gpu{i}_power", "board power (W)", W.SERIES_COLORS[3]),
              (f"gpu{i}_power_limit", "power limit (W)", W.SERIES_COLORS[6])], []),
        ]

        for title, unit, series_defs, bands in chart_defs:
            present = [(k, lbl, col) for k, lbl, col in series_defs if k in self.stats]
            if not present:
                continue
            frame = tk.Frame(page, bg=W.BG)
            frame.pack(fill="x", padx=10, pady=(8, 0))
            tk.Label(frame, text=title, bg=W.BG, fg=W.TEXT_BRIGHT, font=t.bold,
                     anchor="w").pack(anchor="w")
            chart = W.TimeSeriesChart(frame, width=1120, height=210,
                                      x_label="seconds since logging started",
                                      y_label=unit)
            chart.pack(fill="both", expand=True)
            chart.on_range_selected = self.set_window
            chart.selection = self.window
            self._charts.append(chart)
            series = []
            for key, label, color in present:
                pts = self.store.series(self.session_id, key, t0, t1)
                if not pts:
                    continue
                series.append(W.ChartSeries(label=f"{label} ({unit})", color=color,
                                            points=pts))
            chart.set_data(series, markers=self._chart_markers(),
                           bands=[(lo, hi, c, lbl) for lo, hi, c, lbl in bands])

        # histogram of temperature
        hist_row = tk.Frame(page, bg=W.BG)
        hist_row.pack(fill="x", padx=10, pady=8)
        temps = [v for _t, v in self.store.series(self.session_id, f"gpu{i}_temp", t0, t1)]
        tchart = W.HistogramChart(hist_row, width=545, height=190, color=W.CRIT)
        tchart.pack(side="left", padx=(0, 8))
        st = self.stats.get(f"gpu{i}_temp")
        notes = []
        if st:
            notes = [(st.median, "median", W.TEXT_DIM), (st.p95, "p95", W.WARN),
                     (st.p99, "p99", W.CRIT)]
        tchart.set_values(temps, title="Temperature distribution", unit="C",
                          notes=notes)
        self._histograms[(i, "temp")] = tchart
        uchart = W.HistogramChart(hist_row, width=545, height=190, color=W.ACCENT)
        uchart.pack(side="left")
        utils = [v for _t, v in self.store.series(self.session_id, f"gpu{i}_util", t0, t1)]
        uchart.set_values(utils, title="Utilisation distribution", unit="%",
                          notes=[])
        self._histograms[(i, "util")] = uchart

        # statistics table
        self._stats_table(page, prefix=f"gpu{i}_")

    def _chart_markers(self) -> list[W.ChartMarker]:
        out: list[W.ChartMarker] = []
        for ev in self.alarm_events:
            color = W.CRIT if ev.level == "critical" else W.WARN
            d = M.metric_def(ev.metric)
            out.append(W.ChartMarker(x=ev.started_t,
                                     label=f"{d.label} {ev.level}",
                                     color=color, kind="alarm"))
        for t, _wall, label, _kind in self.markers:
            out.append(W.ChartMarker(x=t, label=label, color=W.ACCENT, kind="user"))
        return out

    def _stats_table(self, page: tk.Frame, prefix: str = "") -> None:
        t = self.theme
        keys = [m for m in self.metrics if m.startswith(prefix)]
        if not keys:
            return
        frame = tk.Frame(page, bg=W.BG)
        frame.pack(fill="x", padx=10, pady=(4, 12))
        tk.Label(frame, text="Statistics", bg=W.BG, fg=W.TEXT_BRIGHT, font=t.bold,
                 anchor="w").pack(anchor="w", pady=(0, 2))
        cols = [("metric", 20, "w"), ("unit", 5, "w"),
                ("low", 8, "e"), ("avg", 8, "e"), ("high", 8, "e"),
                ("max", 8, "e"), ("median", 8, "e"),
                ("p95", 8, "e"), ("p99", 8, "e"),
                ("stdev", 8, "e"), ("range", 8, "e"), ("samples", 8, "e")]
        table = W.StatTable(frame, cols, theme=t, rows=len(keys))
        table.pack(anchor="w")
        self._tables[prefix] = table
        table.set_rows(self._stat_rows_extended(keys))

    def _stat_rows_extended(self, keys: Sequence[str]) -> list[list[str]]:
        """One row per metric: spread, central tendency and tail behaviour.

        'low' and 'high' are the 5th and 95th percentiles rather than the
        extremes, so a single spike does not define the row; 'max' carries the
        true peak and 'range' the observed swing.
        """
        rows = []
        for key in keys:
            st = self.stats.get(key)
            d = M.metric_def(key)
            if st is None:
                rows.append([d.label, d.unit] + ["--"] * 10)
                continue
            prec = 1 if d.unit in ("%", "W", "C") else 0
            values = [v for _t, v in self.store.series(self.session_id, key)]
            low = _percentile(values, 5.0) if values else st.minimum
            high = _percentile(values, 95.0) if values else st.maximum
            rows.append([
                d.label, d.unit,
                f"{low:.{prec}f}", f"{st.mean:.{prec}f}", f"{high:.{prec}f}",
                f"{st.maximum:.{prec}f}", f"{st.median:.{prec}f}",
                f"{st.p95:.{prec}f}", f"{st.p99:.{prec}f}",
                f"{st.stdev:.{prec}f}",
                f"{st.maximum - st.minimum:.{prec}f}",
                str(st.count)])
        return rows

    # ------------------------------------------------------------------
    # Alarms tab
    # ------------------------------------------------------------------
    def _build_alarms_tab(self) -> None:
        t = self.theme
        page_widget, page = self._scrollable(self.nb)
        self.nb.add(page_widget, text=f"Alarms ({len(self.alarm_events)})")
        tk.Label(page, text="Threshold crossings during this run", bg=W.BG,
                 fg=W.TEXT_BRIGHT, font=t.bold, anchor="w").pack(
            anchor="w", padx=10, pady=(8, 2))

        if not self.alarm_events:
            tk.Label(page, text="No alarm thresholds were crossed.",
                     bg=W.BG, fg=W.OK, font=t.body, anchor="w").pack(
                anchor="w", padx=10)
            return

        span = self.store.sample_span(self.session_id) or (0.0, 1.0)
        timeline = W.TimeSeriesChart(
            page, width=1120, height=150,
            x_label="seconds since logging started")
        timeline.pack(fill="x", padx=10, pady=6)
        # A flat reference line so the markers have an axis to sit on.
        timeline.set_data(
            [W.ChartSeries(label="session", color=W.IDLE,
                           points=[(span[0], 0.0), (span[1], 0.0)])],
            markers=self._chart_markers())
        timeline.set_ranges((-1.0, 1.0), None)

        cols = [("metric", 22, "w"), ("level", 9, "w"), ("threshold", 10, "e"),
                ("peak", 9, "e"), ("crossed at", 11, "e"), ("cleared at", 11, "e"),
                ("duration", 11, "e"), ("samples", 8, "e"), ("status", 18, "w")]
        table = W.StatTable(page, cols, theme=t, rows=len(self.alarm_events))
        table.pack(anchor="w", padx=10, pady=6)
        rows, colors = [], []
        for ev in self.alarm_events:
            d = M.metric_def(ev.metric)
            prefix = f"gpu{ev.gpu_index} " if ev.gpu_index is not None else ""
            col = W.CRIT if ev.level == "critical" else W.WARN
            rows.append([
                f"{prefix}{d.label}", ev.level.upper(), f"{ev.threshold:.0f}",
                f"{ev.peak:.0f}", f"{ev.started_t:.1f}s",
                f"{ev.ended_t:.1f}s" if ev.ended_t is not None else "--",
                S.human_duration(ev.duration) if ev.duration is not None
                else "to end of run",
                str(ev.samples),
                "open at stop" if ev.ongoing else "cleared"])
            colors.append([W.TEXT, col, W.TEXT_DIM, col, W.TEXT, W.TEXT,
                           W.TEXT, W.TEXT_DIM, W.TEXT_DIM])
        table.set_rows(rows, colors)

        tk.Label(page, text="Red lines are warning/critical crossings; blue lines "
                            "are your manual markers.",
                 bg=W.BG, fg=W.TEXT_DIM, font=t.small, anchor="w").pack(
            anchor="w", padx=10, pady=(2, 10))

    # ------------------------------------------------------------------
    # Configuration tab
    # ------------------------------------------------------------------
    def _build_config_tab(self) -> None:
        t = self.theme
        page_widget, page = self._scrollable(self.nb)
        self.nb.add(page_widget, text="Session info")

        info = [
            ("Session ID", str(self.session_id)),
            ("Label", self.session.label or "(none)"),
            ("Started", time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(self.session.started_at))),
            ("Ended", time.strftime("%Y-%m-%d %H:%M:%S",
                                    time.localtime(self.session.ended_at))
             if self.session.ended_at else "in progress"),
            ("Duration", S.human_duration(self.session.duration)),
            ("Target rate", f"{self.session.sample_hz:.2f} Hz"),
            ("Samples", str(self.session.sample_count)),
            ("Database", self.store.path),
            ("gpumon version", self.session.app_version or "unknown"),
        ]
        box = tk.Frame(page, bg=W.PANEL, highlightthickness=1,
                       highlightbackground=W.BORDER)
        box.pack(fill="x", padx=10, pady=8)
        for key, val in info:
            row = tk.Frame(box, bg=W.PANEL)
            row.pack(anchor="w", fill="x", padx=8, pady=1)
            tk.Label(row, text=key, bg=W.PANEL, fg=W.TEXT_DIM, font=t.small,
                     width=18, anchor="w").pack(side="left")
            tk.Label(row, text=val, bg=W.PANEL, fg=W.TEXT, font=t.small,
                     anchor="w").pack(side="left")

        cfg = self.session.config or {}
        alarms_cfg = cfg.get("alarms") or {}
        if alarms_cfg:
            af = tk.Frame(page, bg=W.PANEL, highlightthickness=1,
                          highlightbackground=W.BORDER)
            af.pack(fill="x", padx=10, pady=8)
            tk.Label(af, text="ALARM RULES ACTIVE DURING THIS RUN", bg=W.PANEL,
                     fg=W.TEXT_BRIGHT, font=t.bold, anchor="w").pack(
                anchor="w", padx=8, pady=(6, 2))
            table = W.StatTable(af, [("threshold", 10, "e"), ("metric", 26, "w"),
                                     ("hysteresis", 12, "e"),
                                     ("min duration", 14, "e")],
                                theme=t, rows=len(alarms_cfg))
            table.pack(anchor="w", padx=8, pady=(0, 8))
            rows = []
            for metric, rule in sorted(alarms_cfg.items()):
                thr = []
                if rule.get("warning") is not None:
                    thr.append(f"w {rule['warning']:.0f}")
                if rule.get("critical") is not None:
                    thr.append(f"c {rule['critical']:.0f}")
                rows.append([" / ".join(thr) or "disabled",
                             M.metric_def(metric).label,
                             f"{rule.get('hysteresis', 0):.1f}",
                             f"{rule.get('min_duration', 0):.1f}s"])
            table.set_rows(rows)

        notes = (self.session.capabilities or {})
        errs = notes.get("backend_errors") or {}
        if errs:
            ef = tk.Frame(page, bg=W.PANEL_ALT, highlightthickness=1,
                          highlightbackground=W.BORDER)
            ef.pack(fill="x", padx=10, pady=8)
            tk.Label(ef, text="BACKENDS THAT RETURNED NO DATA", bg=W.PANEL_ALT,
                     fg=W.WARN, font=t.bold, anchor="w").pack(
                anchor="w", padx=8, pady=(6, 2))
            for key, val in errs.items():
                tk.Label(ef, text=f"- {key}: {val}", bg=W.PANEL_ALT, fg=W.TEXT_DIM,
                         font=t.small, anchor="w", wraplength=1100,
                         justify="left").pack(anchor="w", padx=8)
            tk.Label(ef, text="", bg=W.PANEL_ALT).pack(pady=2)

    def _missing_reason(self, key: str) -> str:
        """Why this session holds no samples for one metric.

        A metric with no rows is not the same as a metric this machine cannot
        read, and the difference matters: CPU temperature is readable on this
        hardware, but only through LibreHardwareMonitor's WMI provider, which
        gpumon deliberately does not install by itself. The session stored that
        fact in its capabilities when it started, so the summary can say it
        instead of shrugging.
        """
        caps = self.session.capabilities or {}
        errors = caps.get("backend_errors") or {}
        if key == "cpu_temp" and not caps.get("cpu_temp", False):
            detail = errors.get("lhm") or "LibreHardwareMonitor not running"
            return (f"not recorded this run - CPU temperature needs "
                    f"LibreHardwareMonitor ({detail}); "
                    f"run: python gpumon.py --setup-sensors")
        if not caps.get("cpu_temp", False) and key in ("cpu_power",):
            return f"not recorded this run - no CPU power sensor ({key})"
        return "not recorded this run - no samples for this metric"

    def _render_alarm_banner(self) -> None:
        if not self.alarm_events:
            self.alarm_banner.configure(
                text="No temperature or utilisation threshold was crossed "
                     "during this run.", fg=W.OK)
            return
        crit = [e for e in self.alarm_events if e.level == "critical"]
        warn = [e for e in self.alarm_events if e.level == "warning"]
        worst_peak = max(e.peak for e in self.alarm_events)
        peaks = ", ".join(
            f"{M.metric_def(e.metric).label} {e.peak:.0f}"
            for e in sorted(self.alarm_events, key=lambda e: -e.peak)[:3])
        text = (f"{len(self.alarm_events)} threshold crossing(s): "
                f"{len(crit)} critical, {len(warn)} warning. "
                f"Highest readings: {peaks}.")
        self.alarm_banner.configure(text=text,
                                    fg=W.CRIT if crit else W.WARN)

    # ------------------------------------------------------------------
    # Exports
    # ------------------------------------------------------------------
    def export_csv(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.win, title="Export samples as CSV",
            defaultextension=".csv", initialfile=f"gpumon_session{self.session_id}.csv",
            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            columns = self.metrics
            rows = {m: self.store.series(self.session_id, m) for m in columns}
            times = sorted({t for series in rows.values() for t, _ in series})
            lookup = {m: dict(series) for m, series in rows.items()}
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write("t_seconds," + ",".join(columns) + "\n")
                for t in times:
                    vals = []
                    for m in columns:
                        v = lookup[m].get(t)
                        vals.append("" if v is None else f"{v:.4f}")
                    fh.write(f"{t:.3f}," + ",".join(vals) + "\n")
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc), parent=self.win)
            return
        messagebox.showinfo("Exported", f"Wrote {len(times)} rows to\n{path}",
                            parent=self.win)

    def export_report(self) -> None:
        from report import build_text_report, build_html_report
        path = filedialog.asksaveasfilename(
            parent=self.win, title="Export summary report",
            defaultextension=".html",
            initialfile=f"gpumon_session{self.session_id}.html",
            filetypes=[("HTML report", "*.html"), ("Text report", "*.txt")])
        if not path:
            return
        try:
            if path.lower().endswith(".txt"):
                content = build_text_report(self.store, self.session_id, self.devices)
            else:
                content = build_html_report(self.store, self.session_id, self.devices)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc), parent=self.win)
            return
        if messagebox.askyesno("Exported", f"Report written to\n{path}\n\nOpen it now?",
                               parent=self.win):
            try:
                os.startfile(path)  # noqa: S606 - user-initiated open
            except OSError:
                pass
