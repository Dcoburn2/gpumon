"""The nvtop-style live monitor window.

Layout
------
  header    title, backend status, logging indicator, action buttons
  devices   one panel per GPU: big temperature, utilisation meter, VRAM,
            clocks and power, each with its own scrolling sparkline
  system    CPU and RAM rows, plus per-core meters when enabled
  footer    sampler diagnostics, active alarms, hotkey hints

Sampler callbacks arrive on the sampler thread and only ever append to a queue;
all widget work happens in `_on_tick` on the Tk main thread, so there is no
cross-thread widget access and no lock contention on the hot path.
"""
from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import messagebox, simpledialog
from typing import Any, Sequence

import alarms as A
import apppaths
import metrics as M
import sampler as SP
import store as S
from metrics import cached_available
from ui import themes as TH
from ui import widgets as W
from ui.summary import SummaryWindow

SPARK_SECONDS = 60.0
MAX_ALARM_ROWS = 6

# Card metrics shared by every GPU card so the columns line up vertically.
# The character widths are the *widest string each column can ever hold*, not a
# guess: a tk.Label clips text longer than its declared width, and the value
# column holds "83.2%  25,554/30,704 MB" while the stats column holds
# "min 104.0 avg 104.0 max 104.0". Both were clipped by shorter guesses.
CARD_LABEL_CHARS = 5
CARD_VALUE_CHARS = 23
CARD_STATS_CHARS = 31
#: The left block is proportional to the window, between these bounds, so the
#: card as a whole scales instead of only the graph growing.
CARD_LEFT_MIN_PX = 150
CARD_LEFT_MAX_PX = 300
CARD_LEFT_FRACTION = 0.20
#: Tall enough for the GPU-Z-style legend and value badge to sit inside the
#: plot without covering the trace.
CARD_GRAPH_HEIGHT = 42
#: Wide enough for the longest value the CPU panel can print, which is the
#: explanation it shows when there is no CPU temperature sensor.
SYSTEM_VALUE_CHARS = 32
CARD_GAP_PX = 16
#: Below this the graphs are hidden: at that width they convey nothing.
CARD_GRAPH_MIN_PX = 200

# Which fields to show per GPU, with the chart range for each. `bands` shade the
# part of the graph that matters: temperature has a warning zone, the percentage
# rows have a "saturated" zone.
GPU_ROW_FIELDS: list[tuple[str, str, float, float, str]] = [
    ("temp", "TEMP", 0.0, 110.0, "C"),
    ("util", "GPU", 0.0, 100.0, "%"),
    ("vram_percent", "VRAM", 0.0, 100.0, "%"),
    ("clock_core", "CORE", 0.0, 2500.0, "MHz"),
    ("power_percent", "PWR", 0.0, 100.0, "%"),
]

#: Shaded regions per field, so a glance shows "this is the bad zone".
FIELD_BANDS: dict[str, list[tuple[float, float, str]]] = {
    "temp": [(80.0, 110.0, "#3a1d1d")],
    "util": [(90.0, 100.0, "#132a1c")],
    "vram_percent": [(90.0, 100.0, "#2a2413")],
}

#: Row label per field, for the legend chip drawn inside each graph.
ROW_LABELS: dict[str, str] = {field: label for field, label, _lo, _hi, _u
                              in GPU_ROW_FIELDS}
#: Short caption per series, for a chart carrying more than one.
SERIES_LABELS: dict[str, str] = {
    "temp": "temp", "hotspot": "hotspot", "util": "gpu",
    "vram_percent": "vram", "power_percent": "pwr", "clock_core": "core",
}


@dataclass(frozen=True)
class CardRow:
    """One chart on a GPU card."""

    key: str                      # the field the chart draws and reports
    label: str
    fields: tuple[str, ...]       # the series in the chart (one, by design)
    lo: float
    hi: float
    unit: str


def card_rows() -> list[CardRow]:
    """The charts one card shows: one per metric, always.

    This used to have a second, "combined" layout that put every series on a
    single chart with a shared axis. It was tried at the request of a reference
    screenshot and then rejected: five traces on one plot is harder to read than
    five small ones, and the legend ends up carrying values the number columns
    should have shown. It is gone rather than switched off.

    The hotspot is deliberately *not* among these. It is a number worth reading
    and a line worth omitting: on the temperature chart it doubles the trace count
    for a second reading of the same sensor cluster, and its own row costs height
    that the chart does not need either. It stays in the card's text, where it
    always was.
    """
    return [
        CardRow(field, label, (field,), lo, hi, unit)
        for field, label, lo, hi, unit in GPU_ROW_FIELDS
    ]


#: Default warning lines drawn on a graph, per field and unit.
#: Threshold reference line per field. The colour comes from the active palette
#: at call time: the amber that reads well on a dark plot disappears on the
#: light theme, where the warning tier is a much darker amber.
def field_thresholds(field: str) -> list[tuple[float, str]]:
    return {
        "temp": [(80.0, W.WARN)],
        "vram_percent": [(95.0, W.WARN)],
        "power_percent": [(95.0, W.WARN)],
    }.get(field, [])


def _series_caption(field: str, index: int, values: dict[str, float]) -> str:
    """One legend row: what the trace is, and what it currently reads.

    A chart can carry several series now, so each row names its own metric -
    "temp 45.0 C" and "hotspot 58.0 C" under one another, the way the GPU-Z
    reference draws it.
    """
    caption = SERIES_LABELS.get(field, ROW_LABELS.get(field, field))
    value = values.get(f"gpu{index}_{field}")
    if value is None:
        return f"{caption}  --"
    unit = M.metric_def(f"gpu{index}_{field}").unit
    if unit in ("%", "C", "W"):
        return f"{caption}  {value:.1f} {unit}"
    return f"{caption}  {value:,.0f} {unit}".strip()


@dataclass
class GpuPanel:
    index: int
    frame: tk.Frame
    temp_label: tk.Label
    name_label: tk.Label
    note_label: tk.Label
    sparks: dict[str, W.Sparkline]
    value_labels: dict[str, tk.Label]
    meter: W.MeterBar
    alarm_labels: list[tk.Label]
    #: Rows hidden when the card is collapsed.
    body: tk.Frame | None = None
    #: One-line summary shown only while collapsed.
    summary_label: tk.Label | None = None
    toggle: tk.Label | None = None
    collapsed: bool = False
    #: Rolling per-metric statistics for the window on screen.
    stats: dict[str, dict[str, float]] = field(default_factory=dict)
    #: min/avg/max readouts, one per metric row.
    stats_labels: dict[str, tk.Label] = field(default_factory=dict)
    #: Widgets whose width follows the window, so every card stays aligned.
    sized: dict[str, Any] = field(default_factory=dict)


def sensorsetup_describe() -> str:
    """The newest reading as a short phrase, for the status line."""
    import sensorsetup
    return sensorsetup.describe_reading()


def _cpu_temp_available(manager) -> bool:
    """Whether a CPU temperature is readable, without touching Tk.

    Called from the worker thread, so it uses the sensor manager directly rather
    than going through any widget.
    """
    try:
        return bool(manager.cpu_temp_source())
    except Exception:  # noqa: BLE001
        return False


def shortcut_exists_for_offer(config: dict) -> bool:
    """Whether there is anything worth offering a shortcut for.

    A packaged build has an executable of its own; a run from source does not,
    and offering to shortcut an interpreter is not an offer anyone wants. An
    existing desktop shortcut is respected rather than duplicated.
    """
    import sys

    import shortcut
    if not getattr(sys, "frozen", False):
        return False
    try:
        return not shortcut.shortcut_exists()
    except OSError:
        return True


class MonitorApp:
    """Owns the root window, the sampler, and the live rendering loop."""

    def __init__(self, root: tk.Tk, manager: M.SensorManager, store: S.Store,
                 sampler: SP.Sampler) -> None:
        self.root = root
        self.manager = manager
        self.store = store
        self.sampler = sampler
        self.theme = W.Theme()
        self.caps = manager.capabilities()
        self.panels: dict[int, GpuPanel] = {}
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=10000)
        self._alarm_log: list[A.AlarmUpdate] = []
        self._summary_windows: dict[int, SummaryWindow] = {}
        self._spark_hz = sampler.sample_hz
        self._last_render = 0.0
        self._blink_on = True
        self._closing = False
        #: Id of the pending redraw timer, so ticks never stack up.
        self._tick_pending: str | None = None
        #: Column layout state, shared by every card.
        self._graphs_hidden = False
        self._show_graphs = True
        self._graph_width = 0
        #: Whether the stats column is currently shown, and how wide the left
        #: block ended up: both follow the window, so they are state, not
        #: constants.
        self._show_stats = True
        self._left_width = CARD_LEFT_MAX_PX
        #: Samples the graphs are panned back from the live edge (0 = live).
        self._history = 0
        self._drag_from: tuple[int, int] | None = None
        self._pending_resize = False
        self._size = ""
        #: Position of the keyboard focus ring among the header buttons.
        self._focus_index: int | None = None

        self.root.title(f"gpumon {SP.APP_VERSION} - GPU & system telemetry")
        self.root.configure(bg=W.BG)
        self.root.minsize(760, 520)
        self._build_ui()
        self._wire_sampler()
        # Size to fit the data rather than to a fixed guess, then lay the
        # columns out before the first sample arrives.
        self._size = self._fit_window()
        self.root.update_idletasks()
        self._layout_columns()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.sampler.start()
        self._tick_pending = self.root.after(100, self._on_tick)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        t = self.theme
        outer = tk.Frame(self.root, bg=W.BG)
        outer.pack(fill="both", expand=True)

        # ---- header ----------------------------------------------------
        header = tk.Frame(outer, bg=W.PANEL_ALT, highlightthickness=1,
                          highlightbackground=W.BORDER)
        self.header = header
        header.pack(fill="x", side="top")

        title_box = tk.Frame(header, bg=W.PANEL_ALT)
        title_box.pack(side="left", padx=10, pady=6)
        tk.Label(title_box, text="gpumon", bg=W.PANEL_ALT, fg=W.ACCENT,
                 font=t.title).pack(side="left")
        tk.Label(title_box, text="  live telemetry", bg=W.PANEL_ALT,
                 fg=W.TEXT_DIM, font=t.small).pack(side="left")

        controls = tk.Frame(header, bg=W.PANEL_ALT)
        controls.pack(side="right", padx=10, pady=6)
        self.btn_log = W.FlatButton(controls, "  START LOGGING  ", self.toggle_logging,
                                    bg=W.action_colours("ok")[0], fg=W.OK, theme=t)
        self.btn_log.pack(side="left", padx=3)
        self.btn_mark = W.FlatButton(controls, " MARK ", self.add_marker, theme=t)
        self.btn_mark.pack(side="left", padx=3)
        self.btn_summary = W.FlatButton(controls, " SUMMARY ", self.open_summary, theme=t)
        self.btn_summary.pack(side="left", padx=3)
        # No COLLAPSE button: the chevron on each card does it, and the header
        # has more useful things to spend its width on (the layout button).
        self.btn_collapse = None
        self.btn_graphs = W.FlatButton(controls, " GRAPHS ON ",
                                       lambda: self.toggle_graphs(), theme=t)
        self.btn_graphs.pack(side="left", padx=3)
        self.btn_alarms = W.FlatButton(controls, " ALARMS ", self.open_alarm_config, theme=t)
        self.btn_alarms.pack(side="left", padx=3)
        self.btn_sessions = W.FlatButton(controls, " SESSIONS ", self.open_sessions, theme=t)
        self.btn_sessions.pack(side="left", padx=3)
        # The theme button names the theme it is on, so the state is readable
        # without opening anything.
        self.btn_theme = W.FlatButton(controls, f" {W.palette().label.upper()} ",
                                      self.open_theme_picker, theme=t)
        self.btn_theme.pack(side="left", padx=3)
        # Clicking a button moves the keyboard focus ring with it, so Tab
        # continues from wherever the mouse left off.
        for button in self._focusable_buttons():
            button.bind("<Button-1>",
                        lambda _e, b=button: self._sync_focus_from_click(b),
                        add="+")

        # ---- scrollable body -------------------------------------------
        # Everything below the header scrolls, so the wheel, arrows, Page
        # Up/Down, Home and End all reach the lower cards on a short window.
        body_wrap = tk.Frame(outer, bg=W.BG)
        body_wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body_wrap, bg=W.BG, highlightthickness=0, bd=0)
        self.vbar = W.ThemedScrollbar(body_wrap, orient="vertical",
                                      command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self._scroll_needed = False

        self.body = tk.Frame(self.canvas, bg=W.BG)
        self._body_window = self.canvas.create_window((0, 0), window=self.body,
                                                      anchor="nw")
        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        # Focus the canvas so arrow keys reach the scroll bindings immediately.
        self.canvas.configure(takefocus=True)

        # ---- recording status strip ------------------------------------
        strip = tk.Frame(self.body, bg=W.BG)
        strip.pack(fill="x", padx=10, pady=(6, 0))
        self.rec_label = tk.Label(strip, text=" IDLE ", bg=W.PANEL, fg=W.TEXT_DIM,
                                  font=t.bold, padx=8, pady=2)
        self.rec_label.pack(side="left")
        self.rec_detail = tk.Label(strip, text="", bg=W.BG, fg=W.TEXT, font=t.body)
        self.rec_detail.pack(side="left", padx=10)
        self.alarm_banner = tk.Label(strip, text="", bg=W.BG, fg=W.WARN, font=t.bold)
        self.alarm_banner.pack(side="right")
        # Shown only while the graphs are panned off the live edge, so a chart
        # showing history is never mistaken for a live one.
        self.history_label = tk.Label(strip, text="", bg=W.BG, fg=W.PLOT_BG,
                                      font=t.bold, padx=6, pady=2)
        self.history_label.pack(side="right", padx=8)

        # ---- device panels ---------------------------------------------
        self.devices_frame = tk.Frame(self.body, bg=W.BG)
        self.devices_frame.pack(fill="x", padx=10, pady=6)
        for gpu in self.caps.gpus:
            self._build_gpu_panel(self.devices_frame, gpu)

        # ---- system panel ----------------------------------------------
        self._build_system_panel(self.body)

        # ---- alarm timeline --------------------------------------------
        self._build_alarm_strip(self.body)

        self._build_scroll_indicator(highlight=W._blend(W.BORDER, W.ACCENT, 0.35))
        self._bind_graph_history()
        # The sensors button exists only when CPU temperature is missing, so its
        # state has to be settled once the card is built rather than on the
        # first click that would never come. The automatic attempt waits two
        # seconds so the window is on screen before anything is triggered.
        self._refresh_sensor_button()
        self.root.after(2000, self._auto_start_sensors)
        # The first-run offer comes after the window has settled and the sensors
        # have had their chance to start, so the question is not competing with a
        # dialog of our own.
        self.root.after(3000, self._offer_shortcut)

        # ---- footer ----------------------------------------------------
        footer = tk.Frame(outer, bg=W.PANEL_ALT, highlightthickness=1,
                          highlightbackground=W.BORDER)
        footer.pack(fill="x", side="bottom")
        self.footer_left = tk.Label(footer, text="", bg=W.PANEL_ALT, fg=W.TEXT_DIM,
                                    font=t.small, anchor="w", justify="left")
        self.footer_left.pack(side="left", padx=10, pady=4)
        tk.Label(footer, text="Tab focus  Enter activate  \u2191\u2193 PgUp/PgDn "
                              "Home/End scroll  drag a graph or shift+wheel to "
                              "pan history  |  L log  M mark  S summary  "
                              "A alarms  C collapse  G graphs  T theme  "
                              "V layout  P live  Q quit",
                 bg=W.PANEL_ALT, fg=W.TEXT_DIM, font=t.small).pack(side="right", padx=10)

        self._bind_keys()

    # ------------------------------------------------------------------
    # Scrolling and keyboard navigation
    # ------------------------------------------------------------------
    def _build_scroll_indicator(self, highlight: str = "") -> None:
        """A thin mark on the header showing how much is below the fold."""
        self.scroll_indicator = tk.Canvas(self.header, width=90, height=6,
                                          bg=W.PANEL_ALT, highlightthickness=0,
                                          bd=0)
        self.scroll_indicator.pack(side="right", padx=(4, 10))

    def _on_body_configure(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._update_scroll_indicator()

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._body_window, width=event.width)
        self._update_scroll_indicator()

    def _update_scroll_indicator(self) -> None:
        """Show, and hide, the scrollbar and the below-the-fold marker."""
        canvas = getattr(self, "canvas", None)
        if canvas is None:
            return
        region = canvas.bbox("all")
        if not region:
            return
        content_h = region[3] - region[1]
        view_h = canvas.winfo_height()
        needed = content_h > view_h + 2
        if needed and not self._scroll_needed:
            self.vbar.pack(side="right", fill="y")
            self._scroll_needed = True
        elif not needed and self._scroll_needed:
            self.vbar.pack_forget()
            self._scroll_needed = False

        marker = getattr(self, "scroll_indicator", None)
        if marker is None:
            return
        marker.delete("all")
        if not needed or content_h <= 0:
            return
        first, last = canvas.yview()
        marker.create_rectangle(0, 1, 90, 5, fill=W.BORDER, outline="")
        marker.create_rectangle(90 * first, 1, 90 * last, 5, fill=W.ACCENT,
                                outline="")

    def _scroll_by(self, units: int) -> None:
        self.canvas.yview_scroll(units, "units")
        self._update_scroll_indicator()

    def _scroll_page(self, direction: int) -> None:
        self.canvas.yview_scroll(direction, "pages")
        self._update_scroll_indicator()

    def _scroll_home(self) -> None:
        self.canvas.yview_moveto(0.0)
        self._update_scroll_indicator()

    def _scroll_end(self) -> None:
        self.canvas.yview_moveto(1.0)
        self._update_scroll_indicator()

    def _on_mousewheel(self, event: tk.Event) -> str:
        """Wheel scrolling, tolerating the three platform conventions.

        Windows and macOS send <MouseWheel> with a delta; X11 sends button 4
        and 5 events instead, which arrive here as Button-4/Button-5.
        """
        delta = getattr(event, "delta", 0)
        num = getattr(event, "num", 0)
        if num == 4:
            self._scroll_by(-3)
        elif num == 5:
            self._scroll_by(3)
        elif delta:
            self._scroll_by(-1 * max(1, abs(int(delta)) // 120) * (1 if delta > 0 else -1))
        return "break"

    def _bind_keys(self) -> None:
        """Wire scrolling, focus navigation and the action hotkeys."""
        for key, fn in (("l", self.toggle_logging), ("m", self.add_marker),
                        ("s", self.open_summary), ("a", self.open_alarm_config),
                        ("c", self.toggle_all_cards), ("g", self.toggle_graphs),
                        ("t", self.cycle_theme),
                        ("p", self.resume_live),
                        ("v", self.toggle_per_core),
                        ("q", self.on_close)):
            self.root.bind(f"<Key-{key}>", lambda _e, f=fn: f())

        # Scrolling: every key the request named, on the root and the canvas so
        # they work whether or not the canvas holds focus.
        scroll_keys = {
            "<Up>": lambda: self._scroll_by(-1),
            "<Down>": lambda: self._scroll_by(1),
            "<Prior>": lambda: self._scroll_page(-1),      # Page Up
            "<Next>": lambda: self._scroll_page(1),        # Page Down
            "<Home>": self._scroll_home,
            "<End>": self._scroll_end,
            "<Control-Home>": self._scroll_home,
            "<Control-End>": self._scroll_end,
        }
        for sequence, fn in scroll_keys.items():
            for widget in (self.root, self.canvas):
                widget.bind(sequence, self._as_handler(fn))

        # Mouse wheel: Windows/macOS deltas plus X11 buttons 4 and 5.
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            for widget in (self.root, self.canvas, self.body):
                widget.bind(sequence, self._on_mousewheel)

        # Tab cycles the action buttons; Enter or Space activates the focused one.
        for sequence in ("<Tab>", "<ISO_Left_Tab>", "<Shift-Tab>"):
            self.root.bind(sequence, self._as_handler(self._cycle_focus))
        for sequence in ("<Return>", "<KP_Enter>", "<space>"):
            self.root.bind(sequence, self._as_handler(self._activate_focus))
        self.root.bind("<Escape>", lambda _e: self.on_close())
        self.root.bind("<Configure>", self._on_window_resize)

    @staticmethod
    def _as_handler(fn) -> Any:
        def handler(_event: tk.Event) -> str:
            fn()
            return "break"
        return handler

    def _focusable_buttons(self) -> list[Any]:
        return [b for b in (self.btn_log, self.btn_mark, self.btn_summary,
                            self.btn_collapse, self.btn_graphs, self.btn_alarms,
                            self.btn_sessions, self.btn_theme)
                if b is not None and b.winfo_exists()]

    def _cycle_focus(self, backwards: bool = False) -> None:
        """Move the keyboard focus ring between the action buttons."""
        buttons = self._focusable_buttons()
        if not buttons:
            return
        current = self._focus_index
        if current is None:
            new_index = len(buttons) - 1 if backwards else 0
        else:
            step = -1 if backwards else 1
            new_index = (current + step) % len(buttons)
        self._focus_index = new_index
        self._paint_focus()
        # Keep the focused control visible.
        button = buttons[new_index]
        self.root.focus_set()

    def _activate_focus(self) -> None:
        buttons = self._focusable_buttons()
        if self._focus_index is None or not buttons:
            # Nothing focused: Enter starts or stops logging, the usual action.
            self.toggle_logging()
            return
        command = getattr(buttons[self._focus_index], "_command", None)
        if callable(command):
            command()

    def _paint_focus(self) -> None:
        """Outline the focused button, or clear it when nothing has focus."""
        buttons = self._focusable_buttons()
        for position, button in enumerate(buttons):
            focused = (self._focus_index == position)
            try:
                button.configure(highlightthickness=1,
                                 highlightbackground=W.ACCENT if focused else W.PANEL_ALT,
                                 highlightcolor=W.ACCENT if focused else W.PANEL_ALT,
                                 bd=1 if focused else 0)
            except tk.TclError:
                pass

    def _sync_focus_from_click(self, widget: Any) -> None:
        """Clicking a button should move the focus ring to it too."""
        buttons = self._focusable_buttons()
        for position, button in enumerate(buttons):
            if button is widget:
                self._focus_index = position
                self._paint_focus()
                return

    def _build_gpu_panel(self, parent: tk.Frame, gpu: M.GpuDevice) -> None:
        """One GPU card: header plus the metric rows, all in shared columns."""
        t = self.theme
        frame = tk.Frame(parent, bg=W.PANEL, highlightthickness=1,
                         highlightbackground=W.BORDER)
        frame.pack(fill="x", pady=3)
        frame.grid_columnconfigure(1, weight=1)

        # ---- header: always visible, doubles as the collapsed summary ----
        header = tk.Frame(frame, bg=W.PANEL)
        header.grid(row=0, column=0, columnspan=3, sticky="ew", padx=8, pady=(5, 2))
        header.grid_columnconfigure(2, weight=1)

        toggle = tk.Label(header, text="\u25be", bg=W.PANEL, fg=W.ACCENT,
                          font=t.bold, cursor="hand2", width=2)
        toggle.grid(row=0, column=0, sticky="w")
        name = tk.Label(header, text=f"gpu{gpu.index}  {gpu.name}", bg=W.PANEL,
                        fg=W.TEXT_BRIGHT, font=t.bold, anchor="w")
        name.grid(row=0, column=1, sticky="w")
        ident = gpu.pci or "PCI location unknown"
        tk.Label(header, text=f"{ident}   [{', '.join(gpu.sources) or 'no source'}]",
                 bg=W.PANEL, fg=W.ACCENT, font=t.tiny, anchor="w").grid(
            row=0, column=2, sticky="w", padx=8)
        # Collapsed-summary text: the vital signs, so collapsing loses nothing
        # critical.
        headline = tk.Label(header, text="", bg=W.PANEL, fg=W.TEXT, font=t.small,
                            anchor="e")
        headline.grid(row=0, column=3, sticky="e")

        # ---- body: hidden when collapsed ----
        # Four columns (label, value, stats, graph) whose widths are computed
        # once in _layout_columns and applied to every card, so the columns line
        # up across cards and nothing is clipped. Each card is a separate Frame,
        # so a grid weight inside one card cannot align it with another.
        body = tk.Frame(frame, bg=W.PANEL)
        body.grid(row=1, column=0, columnspan=3, sticky="ew")
        body.grid_columnconfigure(3, weight=1, minsize=160)

        left = tk.Frame(body, bg=W.PANEL)
        left.grid(row=0, column=0, rowspan=2, sticky="nsw", padx=(10, 6), pady=6)
        # The width comes from the column (see _layout_columns). A Frame cannot
        # pin its own width here: `grid_propagate(False)` does nothing for
        # pack-managed children, which is why this block used to size itself to
        # its text and shift every column after it, card by card.
        temp_label = tk.Label(left, text="-- C", bg=W.PANEL, fg=W.TEXT_DIM,
                              font=t.huge, anchor="w")
        temp_label.pack(anchor="w")
        sub = tk.Label(left, text="", bg=W.PANEL, fg=W.TEXT_DIM, font=t.tiny,
                       anchor="w", justify="left", wraplength=240)
        sub.pack(anchor="w")
        setattr(temp_label, "_sub", sub)
        notes: list[tk.Label] = []
        if gpu.is_aggregate or gpu.luid_confidence in ("pci-order", "unattributed"):
            detail = (f"{len(gpu.luids)} hardware functions combined"
                      if gpu.is_aggregate else "counter matched by counter order")
            note = tk.Label(left, text=detail, bg=W.PANEL, fg=W.WARN, font=t.tiny,
                            anchor="w", wraplength=240, justify="left")
            note.pack(anchor="w")
            notes.append(note)

        meter = W.MeterBar(body, width=CARD_LEFT_MAX_PX - 18, height=10, bg=W.PANEL)
        meter.grid(row=2, column=0, sticky="w", padx=(10, 6), pady=(0, 8))

        sparks: dict[str, W.Sparkline] = {}
        value_labels: dict[str, tk.Label] = {}
        stats_labels: dict[str, tk.Label] = {}
        label_widgets: list[tk.Label] = []
        rows = card_rows()
        for row, spec in enumerate(rows):
            name_lbl = tk.Label(body, text=spec.label, bg=W.PANEL, fg=W.TEXT_DIM,
                                font=t.small, width=CARD_LABEL_CHARS, anchor="w")
            name_lbl.grid(row=row, column=1, sticky="w")
            label_widgets.append(name_lbl)
            value = tk.Label(body, text="--", bg=W.PANEL, fg=W.TEXT, font=t.bold,
                             width=CARD_VALUE_CHARS, anchor="w")
            value.grid(row=row, column=2, sticky="w")
            # min/avg/max over the visible window: context the trace alone
            # cannot give at a glance.
            stats = tk.Label(body, text="", bg=W.PANEL, fg=W.TEXT_DIM,
                             font=t.tiny, width=CARD_STATS_CHARS, anchor="w")
            stats.grid(row=row, column=3, sticky="w", padx=(0, 8))
            spark = W.Sparkline(
                body, width=520, height=CARD_GRAPH_HEIGHT,
                vmin=spec.lo, vmax=spec.hi,
                capacity=int(SPARK_SECONDS * self._spark_hz) + 32,
                bg=W.PANEL, grid=True,
                series=[(field, field_color(field)) for field in spec.fields],
                unit=spec.unit, show_peak=True,
                show_current=True, legend=True, value_badge=True)
            spark.set_bands(FIELD_BANDS.get(spec.key, []))
            spark.set_thresholds(field_thresholds(spec.key))
            spark.grid(row=row, column=4, sticky="ew", padx=(0, 10), pady=1)
            # The graph column is the only one that grows.
            body.grid_columnconfigure(4, weight=1, minsize=120)
            sparks[spec.key] = spark
            value_labels[spec.key] = value
            stats_labels[spec.key] = stats

        alarm_labels = [tk.Label(body, text="", bg=W.PANEL, fg=W.CRIT,
                                 font=t.small, anchor="w")
                        for _ in range(2)]
        for i, lbl in enumerate(alarm_labels):
            lbl.grid(row=len(rows) + i, column=0, columnspan=5,
                     sticky="w", padx=10)

        panel = GpuPanel(
            index=gpu.index, frame=frame, temp_label=temp_label, name_label=name,
            note_label=headline, sparks=sparks, value_labels=value_labels,
            meter=meter, alarm_labels=alarm_labels, body=body,
            summary_label=headline, toggle=toggle, stats_labels=stats_labels,
            sized={"body": body, "left": left, "meter": meter, "rows": rows,
                   "labels": label_widgets, "values": list(value_labels.values()),
                   "stats": list(stats_labels.values()), "notes": notes})
        self.panels[gpu.index] = panel

        for widget in (toggle, name):
            widget.bind("<Button-1>",
                        lambda _e, idx=gpu.index: self.toggle_card(idx))

    def toggle_card(self, index: int, force: bool | None = None) -> None:
        """Collapse or expand one GPU card."""
        panel = self.panels.get(index)
        if panel is None or panel.body is None:
            return
        collapsed = (not panel.collapsed) if force is None else force
        panel.collapsed = collapsed
        if collapsed:
            panel.body.grid_remove()
        else:
            panel.body.grid()
        if panel.toggle is not None:
            panel.toggle.configure(text="\u25b8" if collapsed else "\u25be")

    def toggle_all_cards(self) -> None:
        """Collapse every card, or expand them all if any is collapsed."""
        any_open = any(not p.collapsed for p in self.panels.values())
        for index in self.panels:
            self.toggle_card(index, force=any_open)

    # ------------------------------------------------------------------
    # Column sizing
    # ------------------------------------------------------------------
    def toggle_graphs(self, force: bool | None = None) -> None:
        """Show or hide the graph column on every card at once.

        `force=True` shows them, `force=False` hides them, and omitting it flips
        the current state. Hiding leaves just the numbers, which is the compact
        view for a narrow window; the columns still line up because the widths
        are shared.
        """
        if force is None:
            show = self._graphs_hidden          # flip: hidden -> show
        else:
            show = bool(force)
        self._graphs_hidden = not show
        self.btn_graphs.set_text(" GRAPHS OFF " if not show else " GRAPHS ON ")
        self._layout_columns()

    def _column_needs(self) -> tuple[int, int, int]:
        """Pixel width each text column needs, measured from the widgets.

        Measured rather than assumed. The value labels carry strings like
        "83.2%  25,554/30,704 MB" and the stats labels "min 104.0 avg 104.0
        max 104.0"; guessing them short is exactly what used to clip both
        mid-number when the window was narrow.
        """
        label_px = value_px = stats_px = 0
        for panel in self.panels.values():
            for widget in panel.sized.get("labels", []):
                label_px = max(label_px, widget.winfo_reqwidth())
            for widget in panel.sized.get("values", []):
                value_px = max(value_px, widget.winfo_reqwidth())
            for widget in panel.sized.get("stats", []):
                stats_px = max(stats_px, widget.winfo_reqwidth())
        return label_px, value_px, stats_px

    def _layout_columns(self) -> None:
        """Give every card identical column widths, derived from window width.

        Each card is a separate Frame, so a grid weight inside one card cannot
        align it with another. The widths are therefore computed once here and
        applied to every card, which is what makes the graphs start and end at
        the same x across all cards.

        The whole card scales: the left block and the gaps follow the window,
        the text columns hold the width their content actually needs, and the
        graph takes what is left. When even that is too little the stats column
        goes first, then the graphs - never the numbers, which is what used to
        happen: a column narrower than its text is silently truncated by Tk.
        """
        total = 0
        # Flush pending geometry first: after a resize the body still reports
        # its old width for a moment, and laying out against a stale width made
        # the columns overflow, which Tk then resolved by shrinking every card
        # differently - the thing that misaligned the columns and clipped text.
        try:
            self.root.update_idletasks()
        except tk.TclError:
            pass
        for panel in self.panels.values():
            body = panel.sized.get("body")
            if body is not None and body.winfo_ismapped():
                total = body.winfo_width()
                break
        if total <= 1:
            total = max(700, self.root.winfo_width() - 40)

        # The left block scales with the window, so a wide window gives the
        # temperature and the meter room as well - previously the graph was the
        # only thing that grew.
        left_w = int(min(CARD_LEFT_MAX_PX,
                         max(CARD_LEFT_MIN_PX, total * CARD_LEFT_FRACTION)))
        label_px, value_px, stats_px = self._column_needs()

        # Exactly what the row costs, padding included: 10+6 around the left
        # block, 8 after the stats column, 10 after the graph. Under-counting
        # this is what let the row ask for more width than the card had.
        def room(show_stats: bool) -> int:
            used = 16 + left_w + label_px + value_px
            if show_stats:
                used += stats_px + 8
            return total - used - 10

        if self._graphs_hidden:
            show_graphs, show_stats = False, True
        elif room(True) >= CARD_GRAPH_MIN_PX:
            show_graphs, show_stats = True, True
        elif room(False) >= CARD_GRAPH_MIN_PX:
            # The stats column is the first thing to go: the graphs are the
            # reason the window is open.
            show_graphs, show_stats = True, False
        else:
            show_graphs = False
            show_stats = room(True) >= 0
        graph_w = max(0, room(show_stats)) if show_graphs else 0

        for panel in self.panels.values():
            body = panel.sized.get("body")
            if body is None:
                continue
            body.grid_columnconfigure(0, minsize=left_w + 16)
            body.grid_columnconfigure(1, minsize=label_px)
            body.grid_columnconfigure(2, minsize=value_px)
            body.grid_columnconfigure(3, minsize=stats_px if show_stats else 0)
            body.grid_columnconfigure(4, weight=1 if show_graphs else 0,
                                      minsize=graph_w if show_graphs else 0)
            for widget in panel.sized.get("stats", []):
                if show_stats:
                    widget.grid()
                else:
                    widget.grid_remove()
            left = panel.sized.get("left")
            if left is not None:
                left.configure(width=left_w)
            # Every wrapped label in the left block follows its width, or the
            # text wraps to the old width and pushes the columns out again.
            for widget in (getattr(panel.temp_label, "_sub", None),):
                if widget is not None:
                    widget.configure(wraplength=max(90, left_w - 12))
            for widget in panel.sized.get("notes", []):
                widget.configure(wraplength=max(90, left_w - 12))
            panel.meter.configure(width=max(80, left_w - 18))
            # The big temperature is part of the scaling too, so a narrow card
            # is not all heading.
            panel.temp_label.configure(font=self._temp_font(left_w))
            for row, spec in enumerate(panel.sized.get("rows", [])):
                spark = panel.sparks.get(spec.key)
                if spark is None:
                    continue
                if show_graphs:
                    spark.grid(row=row, column=4, sticky="ew", padx=(0, 10), pady=1)
                    try:
                        spark.configure(width=graph_w)
                    except tk.TclError:
                        pass
                else:
                    spark.grid_remove()
        # The CPU/RAM/TEMP charts are graphs too. Leaving them on made the G
        # toggle look broken: the card graphs vanished and three stayed.
        for spark in self.system_sparks.values():
            if show_graphs:
                spark.grid()
            else:
                spark.grid_remove()
        self._graph_width = graph_w
        self._show_graphs = show_graphs
        self._show_stats = show_stats
        self._left_width = left_w

    def _temp_font(self, left_w: int):
        """Pick the temperature font for the room the card has."""
        if left_w >= 240:
            return self.theme.huge
        if left_w >= 190:
            return self.theme.big
        return self.theme.heading

    # ------------------------------------------------------------------
    # Rebuilding the view
    # ------------------------------------------------------------------
    def _rebuild_window(self) -> None:
        """Recreate the whole view, keeping the graphs' history and the pan.

        Used by a theme change: Tk widgets keep the colours they were built with,
        so restyling in place would mean visiting every widget and knowing what
        role each plays. Rebuilding costs a few milliseconds and cannot miss one.
        """
        history = self._capture_history()
        for child in self.root.winfo_children():
            child.destroy()
        self.theme = W.Theme()
        self.panels.clear()
        self.system_values.clear()
        self.system_sparks.clear()
        self._graph_width = 0
        self._size = ""
        self.root.configure(bg=W.BG)
        self._build_ui()
        self._restore_history(history)
        self._set_history(self._history)
        if getattr(self, "btn_graphs", None) is not None:
            self.btn_graphs.set_text(" GRAPHS OFF " if self._graphs_hidden
                                     else " GRAPHS ON ")
        self._pending_resize = True
        self._last_render = 0.0

    # ------------------------------------------------------------------
    # History: panning the graphs back through the buffer
    # ------------------------------------------------------------------
    def pan_history(self, samples: int) -> None:
        """Slide every graph back (positive) or forward (negative) in time.

        All charts share one offset on purpose: the interesting question during
        a stress test is "what was everything doing when the temperature spiked",
        and a per-chart scroll would make that impossible to line up.
        """
        self._set_history(self._history + int(samples))

    def history_live(self) -> bool:
        return self._history == 0

    def resume_live(self) -> None:
        """Jump back to the newest sample (P, or a double-click on a graph)."""
        self._set_history(0)

    def toggle_history(self) -> None:
        self._set_history(0 if self._history else 30)

    def _history_limit(self) -> int:
        """How far back the buffer actually reaches."""
        for panel in self.panels.values():
            for spark in panel.sparks.values():
                if spark._series and spark._series[0].values:
                    return max(0, len(spark._series[0].values) - 2)
        return 0

    def _set_history(self, offset: int) -> None:
        self._history = max(0, min(int(offset), self._history_limit()))
        for panel in self.panels.values():
            for spark in panel.sparks.values():
                spark.set_offset(self._history)
        for spark in self.system_sparks.values():
            spark.set_offset(self._history)
        self._render_history_state()

    def _capture_history(self) -> dict:
        """Every chart's buffers, keyed by metric.

        Keyed by series rather than by chart so a theme change - which rebuilds
        every widget - keeps each trace exactly as it was on screen.
        """
        return {
            "gpu": {index: {series.key: list(series.values)
                            for spark in panel.sparks.values()
                            for series in spark._series}
                    for index, panel in self.panels.items()},
            "system": {key: list(spark._series[0].values)
                       for key, spark in self.system_sparks.items()
                       if spark._series},
        }

    def _restore_history(self, history: dict) -> None:
        """Put those buffers back into the freshly built charts."""
        for index, panel in self.panels.items():
            saved = history.get("gpu", {}).get(index, {})
            for spark in panel.sparks.values():
                for series in spark._series:
                    values = saved.get(series.key)
                    if values:
                        series.values[:] = values[-spark.capacity:]
        for key, values in history.get("system", {}).items():
            spark = self.system_sparks.get(key)
            if spark is not None and spark._series:
                spark._series[0].values[:] = values[-spark.capacity:]

    def _render_history_state(self) -> None:
        label = getattr(self, "history_label", None)
        if label is None or not label.winfo_exists():
            return
        if self._history:
            seconds = self._history / self._spark_hz if self._spark_hz else 0.0
            label.configure(
                text=f" HISTORY  -{seconds:,.1f}s  (drag or shift+wheel to "
                     f"pan, P to resume live) ",
                bg=W.WARN, fg=W.PLOT_BG)
        else:
            label.configure(text="", bg=W.BG)

    def _redraw_graphs(self) -> None:
        """Repaint the graphs now, rather than waiting for the next tick.

        Dragging is a direct manipulation: at the 5 Hz tick rate the trace would
        lag the pointer by a fifth of a second and feel broken.
        """
        for panel in self.panels.values():
            for spark in panel.sparks.values():
                try:
                    spark.render()
                except tk.TclError:
                    return
        for spark in self.system_sparks.values():
            try:
                spark.render()
            except tk.TclError:
                return

    def _on_graph_wheel(self, event: tk.Event) -> str | None:
        """Shift+wheel over a graph pans history; a plain wheel still scrolls."""
        if not (event.state & 0x0001):
            return None                     # let the page scroll handler run
        delta = int(getattr(event, "delta", 0) or 0)
        if delta == 0:                      # X11 sends button 4/5 instead
            delta = 120 if getattr(event, "num", 0) == 4 else -120
        self.pan_history(-delta / 120 * 5)
        self._redraw_graphs()
        return "break"

    def _on_graph_press(self, event: tk.Event) -> None:
        self._drag_from = (event.x_root, self._history)

    def _on_graph_drag(self, event: tk.Event) -> None:
        if self._drag_from is None:
            return
        start_x, start_offset = self._drag_from
        # The trace follows the cursor: drag right and the samples you are
        # holding move right, which brings *newer* data in from the right and
        # walks forward in time. The first version did the opposite - the offset
        # grew with the drag, so grabbing a spike and pulling right pushed it
        # away from the pointer and showed older data, which reads as the chart
        # fighting the mouse.
        try:
            canvas = event.widget
            per_sample = canvas.winfo_width() / max(1, canvas.capacity)
        except (AttributeError, tk.TclError):
            return
        if per_sample <= 0:
            return
        self._set_history(start_offset - (event.x_root - start_x) / per_sample)
        self._redraw_graphs()

    def _on_graph_release(self, _event: tk.Event) -> None:
        self._drag_from = None

    def _bind_graph_history(self) -> None:
        """Wire panning onto every live graph, system charts included."""
        self._drag_from: tuple[int, int] | None = None
        charts = [spark for panel in self.panels.values()
                  for spark in panel.sparks.values()]
        charts.extend(self.system_sparks.values())
        for spark in charts:
            spark.bind("<MouseWheel>", self._on_graph_wheel, add="+")
            spark.bind("<Button-4>", self._on_graph_wheel, add="+")
            spark.bind("<Button-5>", self._on_graph_wheel, add="+")
            spark.bind("<Button-1>", self._on_graph_press, add="+")
            spark.bind("<B1-Motion>", self._on_graph_drag, add="+")
            spark.bind("<ButtonRelease-1>", self._on_graph_release, add="+")
            spark.bind("<Double-Button-1>",
                       lambda _e: self.resume_live(), add="+")
            spark.configure(cursor="fleur")

    def _on_window_resize(self, event: tk.Event) -> None:
        """Re-lay the columns after a resize, in step with the redraw timer."""
        if event.widget is not self.root:
            return
        self._pending_resize = True

    def _apply_pending_resize(self) -> None:
        self._pending_resize = False
        self._layout_columns()

    def _fit_window(self) -> str:
        """A standard-ish window that still shows the data.

        Every column at once needs about 1080 px of card: the left block, the
        label, value and stats columns sized to their widest real strings
        (190 px and 192 px), and a graph worth reading. Narrower than that the
        stats column is dropped before anything is truncated - see
        _layout_columns. The width is capped to the work area so the window never
        opens larger than the screen.
        """
        width = min(1180, self.root.winfo_screenwidth() - 80)
        # Header + cards + system panel + strips, with room for the graphs.
        height = min(880, self.root.winfo_screenheight() - 120)
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 3)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        return f"{width}x{height}"

    def _build_system_panel(self, parent: tk.Frame) -> None:
        t = self.theme
        frame = tk.Frame(parent, bg=W.PANEL, highlightthickness=1,
                         highlightbackground=W.BORDER)
        frame.pack(fill="x", padx=10, pady=(0, 6))
        frame.grid_columnconfigure(3, weight=1)

        left = tk.Frame(frame, bg=W.PANEL, width=250)
        left.grid(row=0, column=0, rowspan=2, sticky="nsw", padx=(10, 6), pady=6)
        tk.Label(left, text="cpu / memory", bg=W.PANEL, fg=W.TEXT_BRIGHT,
                 font=t.bold, anchor="w").pack(anchor="w")
        self.cpu_info = tk.Label(left, text="", bg=W.PANEL, fg=W.TEXT_DIM,
                                 font=t.tiny, anchor="w", justify="left",
                                 wraplength=240)
        self.cpu_info.pack(anchor="w")
        # Per-core was a command-line flag only, which meant restarting the app
        # to look at the cores during a run. It is a live toggle now.
        self.btn_cores = W.FlatButton(left, " PER-CORE OFF ", self.toggle_per_core,
                                      theme=t, pady=2)
        self.btn_cores.pack(anchor="w", pady=(4, 2))
        # Shown only while CPU temperature is missing, because that is the one
        # number gpumon cannot produce by itself: it needs LibreHardwareMonitor's
        # kernel driver, which needs administrator rights, and the honest place
        # to offer that prompt is next to the reading it would fill in.
        self.btn_sensors = W.FlatButton(left, " START SENSORS ", self.start_sensors,
                                        theme=t, pady=2)
        self.sensors_status = tk.Label(left, text="", bg=W.PANEL, fg=W.TEXT_DIM,
                                       font=t.tiny, anchor="w", justify="left",
                                       wraplength=240)

        self.system_sparks: dict[str, W.Sparkline] = {}
        self.system_values: dict[str, tk.Label] = {}
        # CPU temperature above RAM, at the request of the person reading it:
        # processor load and processor heat belong next to each other, and memory
        # is the one of the three that is not about the CPU.
        rows = [("cpu_util", "CPU", 0.0, 100.0),
                ("cpu_temp", "TEMP", 0.0, 110.0),
                ("ram_percent", "RAM", 0.0, 100.0)]
        for row, (key, label, lo, hi) in enumerate(rows):
            tk.Label(frame, text=label, bg=W.PANEL, fg=W.TEXT_DIM, font=t.small,
                     width=6, anchor="w").grid(row=row, column=1, sticky="w")
            value = tk.Label(frame, text="--", bg=W.PANEL, fg=W.TEXT, font=t.bold,
                             width=SYSTEM_VALUE_CHARS, anchor="w")
            value.grid(row=row, column=2, sticky="w")
            spark = W.Sparkline(frame, width=520, height=22, vmin=lo, vmax=hi,
                                capacity=int(SPARK_SECONDS * self._spark_hz) + 32,
                                bg=W.PANEL, grid=False,
                                series=[(key, field_color(key))])
            spark.grid(row=row, column=3, sticky="e", padx=(6, 10), pady=1)
            self.system_sparks[key] = spark
            self.system_values[key] = value

        self.core_canvas = tk.Canvas(frame, height=24, bg=W.PANEL,
                                     highlightthickness=0, bd=0)
        self.core_canvas.grid(row=len(rows), column=0, columnspan=4, sticky="ew",
                              padx=10, pady=(2, 6))

    def _auto_start_sensors(self) -> None:
        """Bring the sensor helper up by itself when it can, and offer when it can't.

        With a task already registered this is silent: gpumon triggers it, the
        helper reads the registers, and the temperature appears. With nothing
        registered - or with a task left over from an older version - there is
        one Windows permission to ask for, and asking for it unprompted would be
        rude, so the card shows the button and the offer waits for a click.
        """
        import sensorsetup
        if os.name != "nt":
            return
        if os.environ.get("GPUMON_NO_AUTOSTART"):
            # Set by the test suite. Starting the sensors is a real action - it
            # launches a helper from whichever build the task points at - and a
            # test should not leave a process behind holding that build's files.
            return
        if self.manager.cpu_temp_source():
            return                              # already reading it
        if not sensorsetup.task_is_current():
            if sensorsetup.is_elevated():
                # Already running with the rights the setup needs: do it now
                # rather than asking for a prompt that would be redundant.
                self._set_sensor_status("setting up the CPU sensors...", ok=True)

                def set_up() -> None:
                    ok, message = sensorsetup.run_setup_here()
                    try:
                        self.root.after(0, lambda: self._auto_sensor_done(
                            ok, message))
                    except (RuntimeError, tk.TclError):
                        pass

                threading.Thread(target=set_up, daemon=True).start()
                return
            self._set_sensor_status(
                "CPU temperature is not set up yet - press START SENSORS",
                ok=False)
            return
        self._set_sensor_status("starting the sensor helper...", ok=True)

        def worker() -> None:
            ok, message = sensorsetup.start_helper_now(wait=40.0)
            try:
                self.root.after(0, lambda: self._auto_sensor_done(ok, message))
            except (RuntimeError, tk.TclError):
                pass                            # window closed while waiting

        threading.Thread(target=worker, daemon=True).start()

    def _auto_sensor_done(self, ok: bool, message: str) -> None:
        if not self.root.winfo_exists():
            return
        if ok:
            self._sensors_ready(message)
        else:
            self._set_sensor_status(message, ok=False)

    def _offer_shortcut(self) -> None:
        """On the first run, offer to put a shortcut on the desktop.

        Asked once ever, and only when there is something to point at: a
        packaged build's own executable. Running from source there is no
        installed program to shortcut to, so the question is skipped rather than
        answered on the user's behalf. The answer is remembered in the settings
        file, and a machine that already has the shortcut is not asked at all -
        including one where the user put it there themselves.
        """
        import shortcut
        config = A.load_config()
        if config.get("shortcut_offered"):
            return
        if not shortcut_exists_for_offer(config):
            self._remember_shortcut_offer()
            return
        self._remember_shortcut_offer()
        wanted = messagebox.askyesno(
            "gpumon",
            "Add a shortcut to the desktop?\n\n"
            f"It will point at:\n{shortcut.launch_target()[0]}",
            parent=self.root)
        if not wanted:
            return
        ok, detail = shortcut.create_shortcut()
        if ok:
            self._set_sensor_status(f"shortcut added: {detail}", ok=True)
        else:
            messagebox.showwarning("gpumon",
                                   f"The shortcut could not be created.\n\n{detail}",
                                   parent=self.root)

    def _remember_shortcut_offer(self) -> None:
        config = A.load_config()
        config["shortcut_offered"] = True
        try:
            A.save_config(config)
        except OSError:
            pass

    def start_sensors(self) -> None:
        """Get the CPU temperature working, without freezing the window.

        Everything that waits - triggering the task, waiting for Windows to be
        answered, waiting for the first reading - happens on a worker thread. The
        first version of this ran the wait on the UI thread, so clicking the
        button froze the program for twenty seconds, which reads as a hang rather
        than as work in progress.

        None of this involves LibreHardwareMonitor: the reading comes from
        gpumon's own reader, and the prompt is Windows Task Scheduler's, because
        changing a scheduled task requires administrator rights.
        """
        if self.manager.cpu_temp_source():
            self._sensors_ready(sensorsetup_describe())
            return
        if apppaths.is_packaged():
            # A packaged app cannot install a kernel driver, so offering to try
            # would be offering a prompt that Windows refuses.
            self._set_sensor_status(apppaths.packaged_sensor_message(), ok=False)
            return
        self._set_sensor_status("starting the sensor helper...", ok=True)
        threading.Thread(target=self._start_sensors_worker, daemon=True).start()

    def _start_sensors_worker(self) -> None:
        """Ask for the sensors, and keep the status line honest while it happens.

        Everything here runs on a worker thread, so the only way it may touch the
        window is by putting a message on the queue - see `_sensor_message`.
        """
        import sensorsetup

        ok, message = sensorsetup.start_helper_now(wait=8.0)
        if ok:
            self._sensor_message(message, True)
            return
        self._sensor_message(f"{message} - asking Windows...", True)
        raised, prompt = sensorsetup.run_setup_elevated()
        if not raised:
            self._sensor_message(prompt, False)
            return
        self._sensor_message(prompt, True)
        deadline = time.monotonic() + 240.0
        while time.monotonic() < deadline:
            if _cpu_temp_available(self.manager):
                self._sensor_message(f"CPU temperature is live "
                                     f"({sensorsetup_describe()})", True)
                return
            outcome = sensorsetup.setup_outcome()
            if outcome:
                good = outcome.startswith("the setup finished")
                self._sensor_message(outcome, good)
                return
            time.sleep(1.0)
        self._sensor_message("the setup did not finish in time - see "
                             "sensors-setup.log", False)

    def _post(self, action) -> None:
        """Deprecated shim: nothing should call Tk from a worker thread.

        Kept only so an older call site fails loudly in a test rather than
        silently doing nothing. Use `self._push("sensor", (text, ok))` instead.
        """
        raise RuntimeError("do not call Tk from a worker thread; use _push")

    def _sensor_message(self, text: str, ok: bool = True) -> None:
        """Send a status line to the UI thread through the queue."""
        self._push("sensor", (text, ok))

    def _sensors_ready(self, detail: str = "") -> None:
        """A reading arrived: drop the button and say where it came from."""
        M.invalidate_availability()
        source = self.manager.cpu_temp_source()
        labels = {"msr": "read directly from the processor",
                  "lhm": "read via LibreHardwareMonitor",
                  "thermal": "read from /sys",
                  "acpi": "from the firmware's thermal zone"}
        self._set_sensor_status(
            "CPU temperature is live"
            + (f" - {labels[source]}" if source in labels else "")
            + (f" ({detail})" if detail else ""), ok=True)
        self._refresh_sensor_button()

    def _set_sensor_status(self, text: str, *, ok: bool) -> None:
        label = getattr(self, "sensors_status", None)
        if label is None or not label.winfo_exists():
            return
        label.configure(text=text, fg=W.TEXT_DIM if ok else W.WARN)

    def _poll_for_sensors(self) -> None:
        """Watch for the helper's first reading, then re-read the CPU."""
        import sensorsetup
        self._sensor_attempts = getattr(self, "_sensor_attempts", 0) - 1
        if self.manager.cpu_temp_source():
            self._sensors_ready(sensorsetup.describe_reading())
            return
        if self._sensor_attempts <= 0:
            self._set_sensor_status(
                "the sensor helper still has not published anything - press "
                "START SENSORS to try again", ok=False)
            return
        self._set_sensor_status("waiting for the first reading...", ok=True)
        self.root.after(1000, self._poll_for_sensors)

    def _refresh_sensor_button(self) -> None:
        """Show the button only while CPU temperature is actually missing."""
        button = getattr(self, "btn_sensors", None)
        if button is None or not button.winfo_exists():
            return
        if self.manager.cpu_temp_source():
            button.pack_forget()
            if getattr(self, "sensors_status", None) is not None:
                self.sensors_status.pack_forget()
        else:
            button.pack(anchor="w", pady=(4, 2))
            self.sensors_status.pack(anchor="w")

    def toggle_per_core(self, force: bool | None = None) -> None:
        """Turn per-core CPU sampling on or off while the app is running.

        The flag used to be fixed at launch: seeing the cores meant restarting
        and losing the run in progress. `per_core` is only read at poll time, so
        flipping it here changes what the very next sample contains - and the
        sampler, the live buffer and the store all take whatever the poll
        returns, so a session that starts without cores and gains them halfway
        records both halves correctly.
        """
        backend = getattr(self.manager, "system", None)
        current = bool(getattr(backend, "per_core", False))
        enable = (not current) if force is None else bool(force)
        if backend is not None:
            backend.per_core = enable
        self.manager.per_core = enable
        self.sampler.per_core = enable
        if enable:
            # psutil reports 0% on the first per-cpu read because it has no
            # baseline yet; taking that read now keeps a bogus zero out of the
            # first sample after switching on.
            try:
                backend.psutil.cpu_percent(percpu=True, interval=None)
            except (AttributeError, OSError):
                pass
        self._save_per_core(enable)
        if getattr(self, "btn_cores", None) is not None:
            self.btn_cores.set_text(" PER-CORE ON " if enable
                                    else " PER-CORE OFF ")
        self.core_canvas.delete("all")
        self.core_canvas.configure(height=24)
        self.core_canvas.create_text(
            6, 12, anchor="w", fill=W.TEXT_DIM, font=self.theme.tiny,
            text=("per-core sampling on - first readings arrive with the next "
                  "sample" if enable else
                  "per-core utilisation off  (PER-CORE button, or --per-core)"))

    def _save_per_core(self, enable: bool) -> None:
        cfg = A.load_config()
        cfg["per_core"] = bool(enable)
        try:
            A.save_config(cfg)
        except OSError:
            pass

    def _build_alarm_strip(self, parent: tk.Frame) -> None:
        t = self.theme
        frame = tk.Frame(parent, bg=W.PANEL_ALT, highlightthickness=1,
                         highlightbackground=W.BORDER)
        frame.pack(fill="x", side="bottom")
        tk.Label(frame, text="ALARM TIMELINE", bg=W.PANEL_ALT, fg=W.TEXT_DIM,
                 font=t.small).pack(side="left", padx=(10, 8), pady=4)
        self.alarm_rows = [tk.Label(frame, text="", bg=W.PANEL_ALT, fg=W.TEXT,
                                    font=t.small, anchor="w")
                           for _ in range(1)]
        for lbl in self.alarm_rows:
            lbl.pack(side="left", padx=4)
    # ------------------------------------------------------------------
    # Sampler wiring
    # ------------------------------------------------------------------
    def _wire_sampler(self) -> None:
        self.sampler.on_sample = lambda wall, values: self._push("sample", (wall, values))
        self.sampler.on_alarm = lambda upd: self._push("alarm", upd)
        self.sampler.on_error = lambda msg: self._push("error", msg)

    def _push(self, kind: str, payload: Any) -> None:
        try:
            self._queue.put_nowait((kind, payload))
        except queue.Full:
            pass

    # ------------------------------------------------------------------
    # Main render loop
    # ------------------------------------------------------------------
    def _on_tick(self) -> None:
        if self._closing:
            return
        # Only ever one tick queued: rendering can be triggered directly (tests,
        # forced redraws) without stacking up a backlog of timers.
        self._tick_pending = None
        if self._pending_resize:
            self._apply_pending_resize()
        samples = 0
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            if kind == "sample":
                wall, values = payload
                self._apply_sample(values)
                samples += 1
            elif kind == "alarm":
                self._apply_alarm(payload)
            elif kind == "sensor":
                # A message from the sensor worker. It arrives through the queue
                # rather than by calling `after` from its own thread: Tk is not
                # thread-safe, and `after` from a worker raises "main thread is
                # not in main loop", which a try/except had been swallowing - the
                # status line simply never moved.
                text, ok = payload
                self._set_sensor_status(text, ok=ok)
                if ok and self.manager.cpu_temp_source() == "msr":
                    self._refresh_sensor_button()
            elif kind == "error":
                pass
        self._render_status()
        self._tick_pending = self.root.after(200, self._on_tick)

    def _apply_sample(self, values: dict[str, float]) -> None:
        for index, panel in self.panels.items():
            for field, spark in panel.sparks.items():
                # A chart can carry several series; each one is fed by its own
                # metric key, and a missing sensor yields a gap rather than a
                # fake zero.
                for series in spark._series:
                    spark.append(series.key, values.get(f"gpu{index}_{series.key}"))
        for key, spark in self.system_sparks.items():
            spark.append(key, values.get(key))
        # While panned, follow the buffer forward so the same history stays on
        # screen: without this the view would creep back to live one sample at a
        # time, which defeats the point of pausing it.
        if self._history:
            self._history = min(self._history + 1, self._history_limit())
            for panel in self.panels.values():
                for spark in panel.sparks.values():
                    spark.set_offset(self._history)
            for spark in self.system_sparks.values():
                spark.set_offset(self._history)
            self._render_history_state()
        self._last_values = values

    @staticmethod
    def _window_stats(series: Sequence[float]) -> dict[str, float]:
        """min / avg / last over the window currently on screen."""
        finite = [v for v in series if v == v and v not in (float("inf"),
                                                            float("-inf"))]
        if not finite:
            return {}
        return {"min": min(finite), "max": max(finite),
                "avg": sum(finite) / len(finite), "last": finite[-1]}

    def _apply_alarm(self, upd: A.AlarmUpdate) -> None:
        self._alarm_log.append(upd)
        self._refresh_alarm_strip()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _render_status(self) -> None:
        t = self.theme
        st = self.sampler.status()
        values = getattr(self, "_last_values", {})

        # recording strip
        if st.logging:
            self.rec_label.configure(text=" REC ", bg=W.CRIT, fg="#ffffff")
            session = self.store.get_session(st.session_id) if st.session_id else None
            label = f"  {session.label}" if session and session.label else ""
            self.rec_detail.configure(
                text=f"session #{st.session_id}{label}   "
                     f"elapsed {S.human_duration(st.elapsed)}   "
                     f"samples {st.samples_written}   "
                     f"{st.effective_hz:.1f} Hz")
        else:
            self.rec_label.configure(text=" IDLE ", bg=W.PANEL, fg=W.TEXT_DIM)
            self.rec_detail.configure(
                text=f"monitoring at {st.effective_hz:.1f} Hz   "
                     f"poll {st.poll_ms:.0f} ms   press L to start logging")

        # gpu panels
        for index, panel in self.panels.items():
            gpu = next((g for g in self.caps.gpus if g.index == index), None)
            temp = values.get(f"gpu{index}_temp")
            hotspot = values.get(f"gpu{index}_hotspot")
            mem_temp = values.get(f"gpu{index}_mem_temp")
            limit = values.get(f"gpu{index}_temp_limit")

            color = W.TEXT_DIM
            if temp is not None:
                color = _temp_color(temp, limit)
            panel.temp_label.configure(
                text=f"{temp:.0f} C" if temp is not None else "n/a",
                fg=color)
            subs = []
            if hotspot is not None:
                subs.append(f"hotspot {hotspot:.0f} C")
            if mem_temp is not None:
                subs.append(f"mem {mem_temp:.0f} C")
            if limit is not None:
                subs.append(f"limit {limit:.0f} C")
            # Only claim a card has no temperature sensor when no reading
            # actually arrived. Asking the backend list alone said this on both
            # V620s while printing "hotspot 34 C  mem 34 C" on the line above,
            # because ADL (Windows) and sysfs (Linux) were not counted as
            # temperature sources.
            if (temp is None and hotspot is None and mem_temp is None
                    and not (gpu and gpu.has_temperature)):
                subs.append("no thermal sensor available")
            getattr(panel.temp_label, "_sub").configure(text="   ".join(subs))

            for field, lbl in panel.value_labels.items():
                key = f"gpu{index}_{field}"
                d = M.metric_def(key)
                v = values.get(key)
                if field == "vram_percent":
                    used = values.get(f"gpu{index}_vram_used")
                    total = values.get(f"gpu{index}_vram_total")
                    if v is not None and used is not None and total:
                        lbl.configure(text=f"{v:4.1f}%  {used:,.0f}/{total:,.0f} MB",
                                      fg=_load_color(v))
                    elif total:
                        lbl.configure(text=f"n/a  of {total:,.0f} MB",
                                      fg=W.TEXT_DIM)
                    else:
                        lbl.configure(text="n/a", fg=W.TEXT_DIM)
                elif field == "power_percent":
                    power = values.get(f"gpu{index}_power")
                    plimit = values.get(f"gpu{index}_power_limit")
                    if power is not None:
                        text = f"{power:5.1f} W"
                        if plimit:
                            text += f" / {plimit:.0f} W"
                        pct = values.get(f"gpu{index}_power_percent")
                        if pct is not None:
                            text += f"  ({pct:.0f}%)"
                        lbl.configure(text=text, fg=_load_color(pct))
                    else:
                        lbl.configure(text="n/a", fg=W.TEXT_DIM)
                elif v is None:
                    lbl.configure(text="n/a", fg=W.TEXT_DIM)
                else:
                    unit = d.unit
                    if unit == "MHz":
                        text = f"{v:,.0f} MHz"
                    elif unit == "%":
                        text = f"{v:5.1f} %"
                    elif unit == "C":
                        text = f"{v:.0f} C"
                    else:
                        text = f"{v:,.0f} {unit}".strip()
                    lbl.configure(text=text,
                                  fg=_load_color(v) if unit == "%" else W.TEXT)

                # Legend chip inside the graph, GPU-Z style: one row per series,
                # so a chart carrying several still says what each trace is and
                # what it currently reads.
                spark = panel.sparks.get(field)
                if spark is not None:
                    spark.set_legend([
                        (_series_caption(series.key, index, values), series.color)
                        for series in spark._series])

            # Rolling min/avg/max for the window on screen. Reading "62 now,
            # peaked 78" is far more useful during a stress test than the
            # instantaneous number alone.
            stats_labels = panel.stats_labels
            for field, lbl in stats_labels.items():
                series = panel.sparks[field]._series
                data = series[0].values if series else []
                window = self._window_stats(data)
                panel.stats[field] = window
                if not window:
                    lbl.configure(text="")
                    continue
                unit = M.metric_def(f"gpu{index}_{field}").unit
                prec = 1 if unit in ("%", "W", "C") else 0
                lbl.configure(
                    text=f"min {window['min']:.{prec}f}  "
                         f"avg {window['avg']:.{prec}f}  "
                         f"max {window['max']:.{prec}f}")

            util = values.get(f"gpu{index}_util")
            panel.meter.render((util or 0.0) / 100.0,
                               color=_load_color(util) if util is not None else W.IDLE)

            # per-GPU alarm lines
            active = [a for a in self.sampler.alarms.active.values()
                      if a.gpu_index == index]
            for i, lbl in enumerate(panel.alarm_labels):
                if i < len(active):
                    a = active[i]
                    lbl.configure(
                        text=f"! {a.label} {a.level.upper()}  "
                             f"{a.peak:.0f} >= {a.threshold:.0f}  "
                             f"({S.human_duration(st.elapsed - a.started_t)})",
                        fg=W.CRIT if a.level == "critical" else W.WARN)
                else:
                    lbl.configure(text="")

            # Collapsed headline: the vital signs, so hiding the body loses
            # nothing you would want to glance at.
            if panel.summary_label is not None:
                util = values.get(f"gpu{index}_util")
                vram = values.get(f"gpu{index}_vram_percent")
                power = values.get(f"gpu{index}_power")
                bits = []
                if temp is not None:
                    bits.append(f"{temp:.0f} C")
                if util is not None:
                    bits.append(f"{util:.0f}% gpu")
                if vram is not None:
                    bits.append(f"{vram:.0f}% vram")
                if power is not None:
                    bits.append(f"{power:.0f} W")
                worst = panel.stats.get("temp", {})
                if worst:
                    bits.append(f"peak {worst['max']:.0f} C")
                text = "   ".join(bits)
                if not panel.collapsed:
                    text = ""      # the body already shows all of this
                panel.summary_label.configure(
                    text=text, fg=_temp_color(temp, limit))

        # system panel
        cpu_util = values.get("cpu_util")
        ram_pct = values.get("ram_percent")
        cpu_temp = values.get("cpu_temp")
        self.system_values["cpu_util"].configure(
            text=f"{cpu_util:5.1f} %" if cpu_util is not None else "n/a",
            fg=_load_color(cpu_util))
        ram_used = values.get("ram_used")
        ram_total = values.get("ram_total")
        if ram_pct is not None and ram_used is not None and ram_total:
            self.system_values["ram_percent"].configure(
                text=f"{ram_pct:4.1f}%  {ram_used / 1024:,.1f}/{ram_total / 1024:,.1f} GB",
                fg=_load_color(ram_pct))
        else:
            self.system_values["ram_percent"].configure(text="n/a", fg=W.TEXT_DIM)
        if cpu_temp is not None:
            self.system_values["cpu_temp"].configure(
                text=f"{cpu_temp:.1f} C", fg=_temp_color(cpu_temp, 100.0))
        else:
            # Name the missing piece rather than pointing at a notes list: the
            # answer is always the same one (LibreHardwareMonitor's WMI
            # provider), and "n/a" alone reads like gpumon cannot do it.
            self.system_values["cpu_temp"].configure(
                text="n/a - needs LibreHardwareMonitor", fg=W.TEXT_DIM)

        cpu_lines = [f"{self.manager.system.psutil.cpu_count(logical=False) if self.manager.system.psutil else '?'} cores / "
                     f"{self.manager.system.psutil.cpu_count() if self.manager.system.psutil else '?'} threads"]
        if values.get("cpu_clock"):
            cpu_lines.append(f"{values['cpu_clock']:,.0f} MHz")
        self.cpu_info.configure(text="   ".join(cpu_lines))

        # Sparklines only redraw when asked, so render once per UI tick rather
        # than per sample. Widths are re-applied here so a window resize is
        # reflected on the next frame.
        if self._show_graphs and self._graph_width > 0:
            for panel in self.panels.values():
                for spark in panel.sparks.values():
                    try:
                        if abs(spark.winfo_width() - self._graph_width) > 2:
                            spark.configure(width=self._graph_width)
                    except tk.TclError:
                        pass
        for spark in self.system_sparks.values():
            spark.render()
        for panel in self.panels.values():
            for spark in panel.sparks.values():
                spark.render()

        self._render_cores(values)

        # alarms
        active_all = list(self.sampler.alarms.active.values())
        if active_all:
            worst = self.sampler.alarms.worst_level
            self.alarm_banner.configure(
                text=f"{len(active_all)} ACTIVE ALARM(S) - {worst.upper()}",
                fg=W.CRIT if worst == "critical" else W.WARN)
        else:
            self.alarm_banner.configure(text="")

        # Footer diagnostics: keep this short. Full sensor-coverage notes live in
        # the summary window, which has the room to explain them.
        backend = []
        for label, bk in (("NVML", self.manager.nvml), ("PDH", self.manager.pdh),
                          ("psutil", self.manager.system), ("LHM", self.manager.lhm)):
            backend.append(f"{label} {'up' if cached_available(bk) else 'down'}")
        parts = [f"DB {os.path.basename(self.store.path)}"]
        parts.append(" ".join(backend))
        if st.logging and self.sampler.queue_depth():
            parts.append(f"queued {self.sampler.queue_depth()}")
        if st.gap_count:
            parts.append(f"gaps {st.gap_count}")
        if st.dropped:
            parts.append(f"dropped {st.dropped}")
        note = f"   |   {len(self.caps.notes)} sensor note(s) - see Summary > Session info" \
            if self.caps.notes else ""
        self.footer_left.configure(text="   ".join(parts) + note)

    def _render_cores(self, values: dict[str, float]) -> None:
        """Per-core bars, laid out so rows never overlap or overrun.

        Each core gets a fixed-width column: a Braille bar then its percentage,
        right-aligned in the column. Row spacing is derived from the font height
        rather than guessed, which is what made the old version draw its rows on
        top of each other and spill blue lines across the labels beside it.
        """
        canvas = self.core_canvas
        canvas.delete("all")
        core_keys = sorted(k for k in values
                           if k.startswith("core") and k.endswith("_util"))
        if not core_keys:
            canvas.configure(height=24)
            canvas.create_text(6, 12, anchor="w", fill=W.TEXT_DIM,
                               font=self.theme.tiny,
                               text="per-core utilisation off  "
                                    "(run with --per-core to record it)")
            return

        # Measured, not guessed: at font size 8 a Tk text item is 15 px tall, so
        # rows must be pitched at least that far apart. The previous 12 px pitch
        # made each row overlap the next, which drew lines through the glyphs.
        font_size = 8
        line_h = font_size + 8          # 16 px pitch for 15 px glyph boxes
        bar_cells = 12
        bar_px = bar_cells * 6
        label_px = 62
        gap = 12
        col_w = bar_px + 6 + label_px + gap

        canvas.update_idletasks()
        avail = max(320, canvas.winfo_width() - 12)
        columns = max(1, avail // col_w)
        columns = min(columns, len(core_keys))
        per_column = -(-len(core_keys) // columns)

        canvas.configure(height=per_column * line_h + 6)
        for position, key in enumerate(core_keys):
            col = position // per_column
            row = position % per_column
            value = values.get(key)
            label = key[4:-5].lstrip("0") or "0"
            x = 6 + col * col_w
            y = 4 + row * line_h

            bar = W.build_braille([value if value is not None else float("nan")],
                                  bar_cells, 1, vmax=100.0)[0]
            canvas.create_text(x, y, anchor="nw", text=bar,
                               fill=_load_color(value),
                               font=(self.theme.mono_family, font_size))
            text = "n/a" if value is None else f"{value:4.1f}%"
            canvas.create_text(x + bar_px + 5, y, anchor="nw",
                               text=f"c{label:>2} {text}", fill=W.TEXT_DIM,
                               font=(self.theme.mono_family, font_size))

    def _refresh_alarm_strip(self) -> None:
        recent = self._alarm_log[-3:]
        while len(self.alarm_rows) < len(recent):
            lbl = tk.Label(self.alarm_rows[0].master, text="", bg=W.PANEL_ALT,
                           fg=W.TEXT, font=self.theme.small, anchor="w")
            lbl.pack(side="left", padx=4)
            self.alarm_rows.append(lbl)
        if not recent:
            self.alarm_rows[0].configure(text="no threshold crossings yet",
                                         fg=W.TEXT_DIM)
            for lbl in self.alarm_rows[1:]:
                lbl.configure(text="")
            return
        for lbl, upd in zip(self.alarm_rows, recent):
            a = upd.alarm
            stamp = time.strftime("%H:%M:%S", time.localtime(upd.wall))
            verb = {"open": "TRIGGERED", "close": "CLEARED",
                    "escalate": "ESCALATED"}[upd.kind]
            lbl.configure(
                text=f"{stamp} {verb} {a.label} {a.level} "
                     f"(peak {a.peak:.0f}, thr {a.threshold:.0f})",
                fg=W.CRIT if a.level == "critical" else W.WARN)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def toggle_logging(self) -> None:
        if self.sampler.logging:
            self._stop_logging()
        else:
            self._start_logging()

    def _start_logging(self) -> None:
        label = simpledialog.askstring(
            "Start logging", "Label for this run (e.g. 'FurMark 1080p stress'):",
            parent=self.root, initialvalue="")
        if label is None:
            return
        session_id = self.sampler.start_logging(label=label or "")
        self.btn_log.set_style(bg=W.action_colours("crit")[0], fg=W.CRIT)
        self.btn_log.set_text("  STOP LOGGING  ")
        self._alarm_log.clear()
        self._refresh_alarm_strip()
        self.rec_detail.configure(text=f"logging to session #{session_id}")

    def _stop_logging(self) -> None:
        summary = self.sampler.stop_logging()
        self.btn_log.set_style(bg=W.action_colours("ok")[0], fg=W.OK)
        self.btn_log.set_text("  START LOGGING  ")
        if summary is None:
            return
        self.open_summary(session_id=summary.session_id)

    def add_marker(self) -> None:
        if not self.sampler.logging:
            messagebox.showinfo("Not logging",
                                "Start logging first, then marks can be added.",
                                parent=self.root)
            return
        label = simpledialog.askstring(
            "Add marker", "Marker label (e.g. 'Cinebench run 1'):",
            parent=self.root, initialvalue="")
        if label:
            self.sampler.mark(label)

    def open_summary(self, session_id: int | None = None) -> None:
        if session_id is None:
            session_id = self.sampler.session_id
        if session_id is None:
            recent = self.store.list_sessions(limit=1)
            if not recent:
                messagebox.showinfo("No sessions",
                                    "Nothing logged yet. Press L to start logging.",
                                    parent=self.root)
                return
            session_id = recent[0].id
        existing = self._summary_windows.get(session_id)
        if existing is not None and existing.alive:
            existing.focus()
            return
        win = SummaryWindow(self.root, self.store, session_id, self.theme,
                            devices=self.caps.gpus, on_close=self._forget_summary)
        self._summary_windows[session_id] = win

    def _forget_summary(self, session_id: int) -> None:
        self._summary_windows.pop(session_id, None)

    def open_alarm_config(self) -> None:
        from ui.alarms_dialog import AlarmConfigDialog
        AlarmConfigDialog(self.root, self.sampler.alarms, self.theme,
                          metrics=self.sampler.alarm_metrics,
                          on_change=self._save_alarms)

    def _save_alarms(self) -> None:
        cfg = A.load_config()
        cfg["alarms"] = A.rules_to_config(self.sampler.alarms)
        try:
            A.save_config(cfg)
        except OSError:
            pass

    def open_sessions(self) -> None:
        from ui.sessions_dialog import SessionsDialog
        SessionsDialog(self.root, self.store, self.theme,
                       on_open=lambda sid: self.open_summary(session_id=sid))

    # ------------------------------------------------------------------
    # Themes
    # ------------------------------------------------------------------
    def cycle_theme(self, step: int = 1) -> None:
        """Next theme in the catalogue (T, or shift for the previous one)."""
        keys = TH.keys()
        current = TH.current_key()
        try:
            index = keys.index(current)
        except ValueError:
            index = 0
        self.apply_theme(keys[(index + step) % len(keys)])

    def open_theme_picker(self) -> None:
        """A small list of themes, each row previewed in its own colours."""
        win = tk.Toplevel(self.root)
        win.title("Theme")
        win.configure(bg=W.BG)
        win.transient(self.root)
        tk.Label(win, text="  Theme", bg=W.PANEL_ALT, fg=W.ACCENT,
                 font=self.theme.heading, anchor="w").pack(fill="x")
        tk.Label(win, text="  Click one to apply it. T cycles, Shift+T goes back.",
                 bg=W.BG, fg=W.TEXT_DIM, font=self.theme.tiny,
                 anchor="w").pack(fill="x", padx=6, pady=(4, 2))
        for key, label, blurb in TH.catalog():
            palette = TH.get(key)
            row = tk.Frame(win, bg=W.BG, cursor="hand2")
            row.pack(fill="x", padx=6, pady=1)
            # Each row is drawn in the colours it offers: a preview beats a name.
            swatch = tk.Canvas(row, width=54, height=18, bg=W.BG,
                               highlightthickness=0, bd=0)
            swatch.pack(side="left", padx=(2, 8))
            for i, colour in enumerate(palette.series[:4]):
                swatch.create_rectangle(i * 13, 1, i * 13 + 11, 17,
                                        fill=colour, outline="")
            text = f"{label}"
            if key == TH.current_key():
                text += "   (current)"
            name = tk.Label(row, text=text, bg=W.BG, fg=palette.text,
                            font=self.theme.small, anchor="w", cursor="hand2")
            name.pack(side="left")
            tk.Label(row, text=blurb, bg=W.BG, fg=palette.text_dim,
                     font=self.theme.tiny, anchor="w").pack(side="left", padx=10)
            for widget in (row, name, swatch):
                widget.bind("<Button-1>",
                            lambda _e, k=key, w=win: (w.destroy(),
                                                      self.apply_theme(k)))
        win.bind("<Escape>", lambda _e: win.destroy())
        win.update_idletasks()
        x = self.root.winfo_rootx() + max(0, self.root.winfo_width() - 520)
        win.geometry(f"+{x}+{self.root.winfo_rooty() + 60}")

    def apply_theme(self, key: str) -> None:
        """Switch theme: persist it, then rebuild the window in the new colours.

        Tk widgets keep the colours they were built with, so restyling every
        widget in place would mean visiting the whole tree and knowing what role
        each one plays. Rebuilding costs a few milliseconds and cannot miss one.
        The sampler, the store and the recorded history are untouched: only the
        view is recreated - the same path the layout toggle takes.
        """
        palette = TH.set_theme(key)
        self._save_theme(palette.key)
        self._rebuild_window()
        if getattr(self, "footer_left", None) is not None:
            self.footer_left.configure(text=f"theme: {palette.label}")

    def _save_theme(self, key: str) -> None:
        cfg = A.load_config()
        cfg["theme"] = key
        try:
            A.save_config(cfg)
        except OSError:
            pass

    def on_close(self) -> None:
        if self._closing:
            return
        if self.sampler.logging:
            if not messagebox.askyesno(
                    "Stop logging?",
                    "Logging is active. Stop it and close gpumon?", parent=self.root):
                return
            self.sampler.stop_logging()
        self._closing = True
        try:
            self.sampler.stop()
        finally:
            try:
                self.manager.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.store.close()
            except Exception:  # noqa: BLE001
                pass
            self.root.destroy()


# --------------------------------------------------------------------------
# Colour helpers
# --------------------------------------------------------------------------


def field_color(field: str) -> str:
    """Chart colour per metric, chosen so the family is readable at a glance.

    The temperatures used to share one red, which was fine while every chart
    held a single series and wrong the moment hotspot joined the temperature
    chart: two identical reds on one plot are indistinguishable. Each series now
    has its own colour, and amber is avoided because the threshold reference
    line uses it.
    """
    if field in ("temp", "cpu_temp"):
        return W.CRIT
    if field == "hotspot":
        return W.SERIES_COLORS[3]          # orange, next to the red it belongs to
    if field == "mem_temp":
        return W.SERIES_COLORS[5]
    if field in ("util", "cpu_util"):
        return W.ACCENT
    if field in ("vram_percent", "ram_percent"):
        return W.SERIES_COLORS[2]
    if field in ("clock_core", "clock_sm", "clock_mem", "cpu_clock"):
        return W.SERIES_COLORS[1]
    if field == "power_percent":
        return W.SERIES_COLORS[6]
    return W.SERIES_COLORS[4]


def _load_color(value: float | None) -> str:
    if value is None or value != value:
        return W.TEXT_DIM
    if value >= 95:
        return W.CRIT
    if value >= 80:
        return W.WARN
    if value >= 40:
        return W.ACCENT
    return W.OK


def _temp_color(temp: float | None, limit: float | None = None) -> str:
    if temp is None or temp != temp:
        return W.TEXT_DIM
    if limit:
        if temp >= limit - 2:
            return W.CRIT
        if temp >= limit - 10:
            return W.WARN
    else:
        if temp >= 90:
            return W.CRIT
        if temp >= 80:
            return W.WARN
    if temp >= 60:
        return W.ACCENT
    return W.OK
