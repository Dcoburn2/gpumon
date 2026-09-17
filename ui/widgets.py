"""Tk widget toolkit for gpumon: nvtop-ish theme, sparklines and chart canvases.

Everything is drawn with plain Canvas items rather than a plotting library so
the app stays dependency-free and can render thousands of points per second.

Colours are not defined here. `ui/themes.py` owns them and this module resolves
them on access (module `__getattr__`, PEP 562), so `W.PANEL` always means "the
panel colour of the theme that is active now". Before this they were constants
frozen at import, which is exactly what made a runtime theme switch impossible.
"""
from __future__ import annotations

import math
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass, field
from typing import Callable, Sequence

from ui import themes as _themes

# --------------------------------------------------------------------------
# Theme
# --------------------------------------------------------------------------
#: Palette attribute name for every colour a widget may ask this module for.
_PALETTE_ATTRS = {
    "BG": "bg",
    "PANEL": "panel",
    "PANEL_ALT": "panel_alt",
    "BORDER": "border",
    "TEXT": "text",
    "TEXT_DIM": "text_dim",
    "TEXT_BRIGHT": "text_bright",
    "ACCENT": "accent",
    "OK": "ok",
    "WARN": "warn",
    "CRIT": "crit",
    "IDLE": "idle",
    "PLOT_BG": "plot_bg",
    "SERIES_COLORS": "series",
    "W_SELECTION": "selection",
}


def __getattr__(name: str):
    """Resolve theme colours at access time rather than at import time."""
    attribute = _PALETTE_ATTRS.get(name)
    if attribute is not None:
        value = getattr(_themes.current(), attribute)
        # Hand out a copy of the series list: callers index and sort it, and a
        # palette is shared by every front end.
        return list(value) if isinstance(value, tuple) else value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _LivePalette:
    """The active palette, resolved one attribute at a time.

    `C.panel` is the inside-the-module counterpart of `W.PANEL`. The module
    `__getattr__` above only serves attribute access from *other* modules; a
    bare name inside this file is an ordinary global lookup and never reaches it
    (which is how `NameError: name 'PANEL_ALT' is not defined` got as far as a
    real launch). Reading through this object costs one attribute lookup and
    means every widget built after a theme change gets the new colour.
    """

    def __getattr__(self, name: str) -> str:
        return getattr(_themes.current(), name)


C = _LivePalette()


def palette():
    """The active palette, for callers that want more than one colour."""
    return _themes.current()


#: The monospace candidates, in preference order.
FONT_CANDIDATES = ("Cascadia Mono", "Cascadia Code", "Consolas",
                   "DejaVu Sans Mono", "Courier New")
UI_FONT_CANDIDATES = ("Segoe UI", "Inter", "Helvetica", "Arial")


def pick_font(candidates: Sequence[str], fallback: str) -> str:
    try:
        available = set(tkfont.families())
    except tk.TclError:
        return fallback
    for name in candidates:
        if name in available:
            return name
    return fallback


class Theme:
    """Resolved fonts, created after a Tk root exists."""

    def __init__(self) -> None:
        self.mono_family = pick_font(FONT_CANDIDATES, "Courier New")
        self.ui_family = pick_font(UI_FONT_CANDIDATES, "Helvetica")
        self.body = (self.mono_family, 10)
        self.small = (self.mono_family, 9)
        self.tiny = (self.mono_family, 8)
        self.bold = (self.mono_family, 10, "bold")
        self.heading = (self.mono_family, 11, "bold")
        self.title = (self.mono_family, 13, "bold")
        self.big = (self.mono_family, 17, "bold")
        self.huge = (self.mono_family, 26, "bold")
        self.huge = (self.mono_family, 22, "bold")
        self.ui_body = (self.ui_family, 10)
        self.ui_small = (self.ui_family, 9)


def level_color(level: str) -> str:
    return {"ok": C.ok, "warning": C.warn, "critical": C.crit}.get(level, C.text_dim)


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------


def fmt_value(value: float | None, precision: int = 0, *, dash: str = "--") -> str:
    if value is None or (isinstance(value, float) and value != value):
        return dash
    return f"{value:.{precision}f}"


def fmt_pct(value: float | None, precision: int = 0) -> str:
    return "n/a" if value is None else f"{value:.{precision}f}%"


def build_braille(values: Sequence[float], width: int, height: int,
                  vmax: float = 100.0) -> list[str]:
    """Render numbers as Braille bar blocks, the way nvtop does its meters."""
    if width <= 0 or height <= 0:
        return [""] * max(0, height)
    dots = {(0, 0): 1, (0, 1): 2, (0, 2): 4, (0, 3): 64,
            (1, 0): 8, (1, 1): 16, (1, 2): 32, (1, 3): 128}
    cell_w, cell_h = width * 2, height * 4
    grid = [[0] * cell_w for _ in range(cell_h)]
    n = len(values)
    if n:
        for col in range(cell_w):
            src = values[min(n - 1, int(col * n / cell_w))]
            frac = 0.0 if vmax <= 0 else max(0.0, min(1.0, src / vmax))
            filled = int(round(frac * cell_h))
            for row in range(cell_h):
                if cell_h - row <= filled:
                    grid[row][col] = 1
    lines: list[str] = []
    for cy in range(height):
        chars = []
        for cx in range(width):
            code = 0x2800
            for (dx, dy), bit in dots.items():
                if grid[cy * 4 + dy][cx * 2 + dx]:
                    code |= bit
            chars.append(chr(code))
        lines.append("".join(chars))
    return lines


# --------------------------------------------------------------------------
# Sparkline
# --------------------------------------------------------------------------


@dataclass
class SparkSeries:
    key: str
    color: str
    values: list[float] = field(default_factory=list)
    capacity: int = 600
    #: The scale this series is drawn against. `None` means the chart's own,
    #: which is what every series uses until a chart carries two different
    #: units - a clock in MHz next to temperatures in degrees has to be plotted
    #: on a second axis or it is a flat line at the top of the plot.
    vmin: float | None = None
    vmax: float | None = None
    #: "left" or "right": which edge of the plot prints this scale's numbers.
    axis: str = "left"

    def range(self, chart_min: float, chart_max: float) -> tuple[float, float]:
        return (chart_min if self.vmin is None else self.vmin,
                chart_max if self.vmax is None else self.vmax)

    def append(self, value: float | None) -> None:
        # Non-finite and missing values are dropped rather than stored: Tk
        # rejects NaN coordinates, and an absent sensor should leave a gap in
        # the trace instead of breaking the render or reading as a zero.
        if value is None:
            return
        if value != value or value in (float("inf"), float("-inf")):
            return
        self.values.append(value)
        if len(self.values) > self.capacity:
            del self.values[:len(self.values) - self.capacity]


class Sparkline(tk.Canvas):
    """Scrolling chart for one or more series, drawn in the GPU-Z idiom.

    The reference for this look is the sensor graph in GPU-Z: an inset dark plot
    area with a hairline border, faint horizontal divisions with their values
    printed inside on the left, a translucent fill under the trace, a stepped
    (sample-and-hold) line rather than a smoothed one, a legend box in the top
    left with a colour swatch per series, and the newest value boxed against the
    right edge.

    Two behaviours come with that: the x axis is a *fixed* time window, so the
    newest sample sits at the right edge and history scrolls left out of view
    instead of the whole trace stretching to fit, and the line is stepped, so a
    utilisation reading that jumps 0 -> 100 -> 0 draws the square wave GPU-Z
    draws rather than a ramp.

    Item count stays constant regardless of history length: each series is one
    polygon and one line, so a minute of 1 Hz data costs the same as an hour.
    """

    #: Fractions of the value range that get a division line.
    TICKS = (0.0, 0.25, 0.5, 0.75, 1.0)
    #: Below this height only the outer divisions are labelled.
    SMALL_HEIGHT = 90
    #: Width reserved on the left for the scale numbers.
    LABEL_MARGIN = 22

    def __init__(self, master: tk.Misc, *, width: int = 150, height: int = 40,
                 vmin: float = 0.0, vmax: float = 100.0, capacity: int = 600,
                 fill_alpha: bool = True, bg: str | None = None,
                 grid: bool = True, series: Sequence[tuple[str, str]] = (),
                 unit: str = "", show_peak: bool = True,
                 show_current: bool = True, legend: bool = False,
                 value_badge: bool = True, stepped: bool = True,
                 series_scales: dict[str, tuple[float, float, str]] | None = None,
                 **kwargs) -> None:
        super().__init__(master, width=width, height=height,
                         bg=bg or C.panel, highlightthickness=0, bd=0, **kwargs)
        self.w, self.h = width, height
        self.vmin, self.vmax = vmin, vmax
        self.capacity = capacity
        self.fill = fill_alpha
        self.show_grid = grid
        self.unit = unit
        self.show_peak = show_peak
        self.show_current = show_current
        self.show_legend = legend
        self.show_badge = value_badge
        self.stepped = stepped
        self._series: list[SparkSeries] = [
            SparkSeries(key=k, color=c, capacity=capacity) for k, c in series]
        #: key -> (lo, hi, axis) for series drawn against their own range.
        scales = series_scales or {}
        for s in self._series:
            if s.key in scales:
                s.vmin, s.vmax, s.axis = scales[s.key]
        self._thresholds: list[tuple[float, str]] = []
        self._labels: dict[str, tuple[float, str, str]] = {}   # key -> (value, color, text)
        self._max_points = max(2, width)
        self._frame_color = C.border
        # Remembered so a band can be drawn behind the trace.
        self._bands: list[tuple[float, float, str]] = []
        #: Legend rows: (caption, colour), supplied by the caller each frame.
        self._legend: list[tuple[str, str]] = []
        self._legend_font = ("Consolas", 8)
        #: How many samples the right edge of the view sits behind the newest
        #: one. 0 means live; anything else means the trace is history and the
        #: frame is tinted to say so.
        self.offset = 0
        #: Where the pointer is, when it is over the plot: (x, y) or None. The
        #: guide and its readout follow this.
        self._hover: tuple[int, int] | None = None
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)

    # -- configuration ---------------------------------------------------
    def add_series(self, key: str, color: str) -> SparkSeries:
        s = SparkSeries(key=key, color=color, capacity=self.capacity)
        self._series.append(s)
        return s

    def set_series(self, series: list[tuple[str, str]]) -> None:
        self._series = [SparkSeries(key=k, color=c, capacity=self.capacity)
                        for k, c in series]

    def set_thresholds(self, thresholds: Sequence[tuple[float, str]]) -> None:
        self._thresholds = list(thresholds)

    def set_bands(self, bands: Sequence[tuple[float, float, str]]) -> None:
        """Shaded value ranges, e.g. a warning zone at the top of the scale."""
        self._bands = list(bands)

    def set_legend(self, rows: Sequence[tuple[str, str]]) -> None:
        """The GPU-Z legend box: one (caption, colour) row per series."""
        self._legend = list(rows)

    def set_range(self, vmin: float, vmax: float) -> None:
        self.vmin, self.vmax = vmin, vmax

    def set_offset(self, offset: int) -> None:
        """Slide the view back through the buffer (0 = follow the newest)."""
        self.offset = max(0, int(offset))

    def _on_motion(self, event: tk.Event) -> None:
        x0, _y0, x1, _y1 = self._plot_box()
        inside = x0 <= event.x <= x1 and 0 <= event.y <= self.h
        new_state = (event.x, event.y) if inside else None
        if new_state != self._hover:
            self._hover = new_state
            self.render()

    def _on_leave(self, _event: tk.Event) -> None:
        if self._hover is not None:
            self._hover = None
            self.render()

    def sample_at(self, series: SparkSeries,
                  x: float) -> tuple[float, float] | None:
        """(x, value) of the plotted sample nearest this x, or None.

        The same arithmetic the trace is drawn with, deliberately: pixels per
        sample come from capacity, and `offset` slides that window back through
        the buffer. Computed any other way, the guide would drift away from the
        line it is meant to be pointing at.
        """
        values = series.values
        if len(values) < 2:
            return None
        step = max(1, len(values) // self._max_points)
        sampled = values[::step]
        total = max(self.capacity, len(sampled))
        count = len(sampled)
        x0, _y0, x1, _y1 = self._plot_box()
        dx = (x1 - x0) / max(1, total - 1)
        index = int(round((x - x0) / dx + self.offset - (total - count)))
        index = max(0, min(count - 1, index))
        value = sampled[index]
        if value is None or value != value:
            return None                  # a gap in the trace is not a value
        return x0 + (total - count + index - self.offset) * dx, float(value)

    def _draw_cursor(self) -> None:
        """A vertical guide under the pointer, with each series' value beside it.

        The numbers sit just to the right of the guide, in the colour of the line
        they belong to, over a small panel-coloured backing so they stay readable
        where the trace runs underneath them. If there is no room to the right -
        the pointer near the live edge - they move to the left of the guide
        instead, rather than being clipped off the chart.
        """
        assert self._hover is not None
        x0, y0, x1, y1 = self._plot_box()
        hits = [(series, hit) for series in self._series
                if (hit := self.sample_at(series, self._hover[0])) is not None]
        if not hits:
            return

        guide_x = hits[0][1][0]
        self.create_line(guide_x, y0, guide_x, y1, fill=C.text_dim, dash=(2, 2))

        font = self._legend_font
        rows = []
        for series, (_px, value) in hits:
            name, unit = hover_label(series.key)
            rows.append((f"{name} {_axis_num(value)}{unit}", series.color))
        try:
            widest = max(self.tk.call("font", "measure", font, row[0])
                         for row in rows)
        except tk.TclError:
            # A font the measurement call does not understand is not worth
            # failing a redraw for; count characters instead.
            widest = max(len(row[0]) for row in rows) * 7
        box_w = int(widest) + 10
        box_h = 12 * len(rows) + 4
        right = guide_x + 5
        if right + box_w > x1:
            right = max(x0 + 1, guide_x - 5 - box_w)
        top = min(max(y0 + 2, y0 + 2), max(y0 + 2, y1 - box_h - 2))
        self.create_rectangle(right, top, right + box_w, top + box_h,
                              fill=C.plot_bg, outline=C.border)
        for index, (text, color) in enumerate(rows):
            self.create_text(right + 5, top + 3 + index * 12, text=text,
                             anchor="nw", fill=color, font=font)

    def visible_samples(self) -> int:
        """How many samples the view can show at once."""
        return max(1, int(self.capacity))

    def history_seconds(self, hz: float) -> float:
        """How far back the right edge is, in seconds."""
        return self.offset / hz if hz else 0.0

    def append(self, key: str, value: float | None) -> None:
        for s in self._series:
            if s.key == key:
                s.append(value)
                return

    def set_values(self, values: dict[str, float | None]) -> None:
        for s in self._series:
            v = values.get(s.key)
            if v is not None:
                s.append(v)

    def set_label(self, key: str, value: float | None, color: str, text: str) -> None:
        if value is None:
            self._labels.pop(key, None)
        else:
            self._labels[key] = (value, color, text)

    def clear(self) -> None:
        for s in self._series:
            s.values.clear()
        self._labels.clear()
        self.delete("all")

    # -- rendering -------------------------------------------------------
    def render(self) -> None:
        # Take the real widget size each time: a canvas can be resized (by the
        # caller or by a column being dragged) and the cached values from
        # __init__ would then be wrong, which is what made graphs draw at a
        # stale width.
        width = self.winfo_width()
        height = self.winfo_height()
        if width > 1 and height > 1:
            if (width, height) != (self.w, self.h):
                self.w, self.h = width, height
                self._max_points = max(2, width)
        self.delete("all")
        self._draw_plot_background()
        self._draw_bands()
        if self.show_grid:
            self._draw_grid()
        self._draw_thresholds()
        for s in self._series:
            self._draw_series(s)
        for key, (value, color, _text) in self._labels.items():
            self._draw_label(value, color)
        self._draw_annotations()
        if self.show_legend and self._legend:
            self._draw_legend()
        if self.show_badge:
            self._draw_badge()
        # The cursor guide goes on before the frame, so the frame stays the last
        # thing drawn and the chart still looks bounded while the pointer is on it.
        if self._hover is not None:
            self._draw_cursor()
        # Hairline frame last so it sits over the fill. Panned off the live edge
        # it changes colour, so a chart showing history never looks live.
        self.create_rectangle(0, 0, self.w - 1, self.h - 1,
                              outline=C.warn if self.offset else self._frame_color)

    def _plot_box(self) -> tuple[float, float, float, float]:
        """The inset plot area, leaving a margin for the axis numbers.

        GPU-Z prints its scale numbers in a margin to the left of the plot, and
        that is the layout this copies: it keeps the numbers clear of the legend
        box and of the trace, which drawing them inside the plot does not.
        """
        margin = self.LABEL_MARGIN if self.w >= 90 else 0.0
        return margin + 1.0, 1.0, self.w - 1.0, self.h - 1.0

    def _value_to_y(self, value: float, series: SparkSeries | None = None) -> float:
        _x0, y0, _x1, y1 = self._plot_box()
        lo, hi = (self.vmin, self.vmax) if series is None \
            else series.range(self.vmin, self.vmax)
        span = (hi - lo) or 1.0
        frac = (value - lo) / span
        frac = 0.0 if frac < 0 else (1.0 if frac > 1 else frac)
        return y1 - frac * (y1 - y0)

    def _draw_plot_background(self) -> None:
        """The inset panel every GPU-Z graph sits in."""
        x0, y0, x1, y1 = self._plot_box()
        self.create_rectangle(0, 0, self.w - 1, self.h - 1,
                              fill=C.plot_bg, outline="")
        self.create_rectangle(x0, y0, x1, y1, outline=_blend(C.border, C.plot_bg, 0.4))

    def _draw_bands(self) -> None:
        """Shade zones outside the interesting range, e.g. 'this is too hot'."""
        x0, _y0, x1, _y1 = self._plot_box()
        for lo, hi, color in self._bands:
            top = self._value_to_y(hi)
            bottom = self._value_to_y(lo)
            if bottom - top >= 1:
                self.create_rectangle(x0 + 1, top, x1, bottom,
                                      fill=_blend(color, C.plot_bg, 0.78),
                                      outline="")

    def _tick_values(self) -> list[float]:
        """Scale values that get a division line, bottom to top."""
        span = (self.vmax - self.vmin) or 1.0
        fractions = self.TICKS if self.h >= self.SMALL_HEIGHT else (0.0, 0.5, 1.0)
        return [self.vmin + frac * span for frac in fractions]

    def _draw_grid(self) -> None:
        """Divisions with their values in the left margin.

        A chart carrying a second unit - a clock in MHz beside temperatures in
        degrees - prints that scale down the right edge instead, against the same
        division lines. Two absolute scales on one plot is only honest if both
        are labelled.
        """
        x0, y0, x1, y1 = self._plot_box()
        font = ("Consolas", 7)
        for value in self._tick_values():
            y = self._value_to_y(value)
            if y0 + 1 <= y <= y1 - 1:
                self.create_line(x0 + 1, y, x1, y, fill=C.border, dash=(1, 3))
            label_y = min(max(y, y0 + 5), y1 - 5)
            self.create_text(x0 - 2, label_y, text=_spark_num(value),
                             anchor="e", fill=C.text_dim, font=font)
        secondary = next((s for s in self._series
                          if s.axis == "right" and s.vmax is not None), None)
        if secondary is None:
            return
        lo, hi = secondary.range(self.vmin, self.vmax)
        for frac in (0.0, 0.5, 1.0):
            value = lo + (hi - lo) * frac
            label_y = min(max(self._value_to_y(value), y0 + 5), y1 - 5)
            self.create_text(x1 + 2, label_y, text=_spark_num(value),
                             anchor="w", font=font,
                             fill=_blend(secondary.color, C.text_dim, 0.45))

    def _draw_series(self, series: SparkSeries) -> None:
        values = series.values
        if len(values) < 2:
            return
        step = max(1, len(values) // self._max_points)
        sampled = values[::step]
        if len(sampled) < 2:
            return
        x0, _y0, x1, y1 = self._plot_box()
        # Fixed time window: pixels per sample come from capacity, not from how
        # many samples happen to be buffered, so the trace scrolls in from the
        # right instead of being stretched across the whole width. `offset`
        # slides that window back through the buffer for looking at history.
        total = max(self.capacity, len(sampled))
        dx = (x1 - x0) / max(1, total - 1)
        n = len(sampled)
        points: list[float] = []
        for i, v in enumerate(sampled):
            if v is None or v != v or v in (float("inf"), float("-inf")):
                continue
            x = x0 + (total - n + i - self.offset) * dx
            if x < x0 - 0.5 or x > x1 + 0.5:
                continue                      # scrolled out of the view
            points.extend((x, self._value_to_y(v, series)))
        if len(points) < 4:
            return
        if self.fill:
            poly = points + [points[-2], y1, points[0], y1]
            self.create_polygon(poly, fill=_blend(series.color, C.plot_bg, 0.68),
                                outline="")
        if self.stepped:
            self.create_line(_step_points(points), fill=series.color, width=1)
        else:
            self.create_line(points, fill=series.color, width=1, smooth=False)
        self._draw_markers(series, points)

    def _draw_markers(self, series: SparkSeries, points: Sequence[float]) -> None:
        """Peak cross and current-value dot: context without needing a hover."""
        if len(points) < 4:
            return
        xs = points[0::2]
        ys = points[1::2]
        if self.show_peak and len(ys) > 1:
            peak_i = min(range(len(ys)), key=lambda i: ys[i])
            px, py = xs[peak_i], ys[peak_i]
            self.create_line(px - 2.5, py, px + 2.5, py, fill=series.color)
            self.create_line(px, py - 2.5, px, py + 2.5, fill=series.color)
        if self.show_current:
            self.create_oval(xs[-1] - 2, ys[-1] - 2, xs[-1] + 2, ys[-1] + 2,
                             fill=series.color, outline=C.plot_bg)

    def _draw_annotations(self) -> None:
        """Nothing: GPU-Z puts the numbers in the legend and the value badge."""
        return

    def _draw_label(self, value: float, color: str) -> None:
        if value is None or value != value:
            return
        span = (self.vmax - self.vmin) or 1.0
        frac = (value - self.vmin) / span
        frac = 0.0 if frac < 0 else (1.0 if frac > 1 else frac)
        y = self._value_to_y(value)
        x0, _y0, x1, _y1 = self._plot_box()
        self.create_line(x0 + 1, y, x1, y, fill=color, dash=(2, 2))

    def _draw_thresholds(self) -> None:
        x0, _y0, x1, _y1 = self._plot_box()
        for value, color in self._thresholds:
            span = (self.vmax - self.vmin) or 1.0
            frac = (value - self.vmin) / span
            if 0.0 < frac < 1.0:
                self.create_line(x0 + 1, self._value_to_y(value), x1,
                                 self._value_to_y(value), fill=color, dash=(3, 3))

    def _draw_legend(self) -> None:
        """A GPU-Z legend: swatch plus 'caption  value', boxed in the corner.

        Laid out in two columns once there are more than three series: a card
        that carries everything on one chart would otherwise spend most of the
        plot on its own legend.
        """
        font = self._legend_font
        x0, y0, x1, _y1 = self._plot_box()
        line_h = 11
        rows = list(self._legend)
        columns = 2 if len(rows) > 3 else 1
        per_column = -(-len(rows) // columns)
        widest = max((len(text) for text, _c in rows[:per_column]), default=0)
        col_w = 16 + int(widest * 5.4)
        box_w = col_w * columns + 4
        box_h = 4 + line_h * per_column
        if box_h + 4 > self.h or box_w > (x1 - x0) - 8:
            return
        self.create_rectangle(x0 + 3, y0 + 3, x0 + 3 + box_w, y0 + 3 + box_h,
                              fill=_blend(C.panel, C.plot_bg, 0.35),
                              outline=_blend(C.border, C.text_dim, 0.25))
        for position, (text, color) in enumerate(rows):
            column, row = divmod(position, per_column)
            left = x0 + 6 + column * col_w
            y = y0 + 3 + 2 + row * line_h
            self.create_rectangle(left, y + 2, left + 7, y + 9,
                                  fill=color, outline="")
            self.create_text(left + 11, y + 5, text=text, anchor="w", fill=color,
                             font=font)

    def _draw_badge(self) -> None:
        """The newest visible value of the row's primary series, boxed at the
        right edge like GPU-Z's.

        The *first* series, not the last: a chart carrying several (temperature,
        hotspot, load) is named for one of them, and the badge has to agree with
        the value printed beside it. While panned this is the reading at the
        right edge of the view, which is the number the reader is looking at.
        """
        newest: tuple[float, str] | None = None
        for s in self._series:
            if not s.values:
                continue
            index = len(s.values) - 1 - self.offset
            if index < 0:
                continue
            newest = (s.values[index], s.color)
            break
        if newest is None:
            return
        value, color = newest
        text = _spark_num(value)
        width = 12 + int(len(text) * 5.4)
        _x0, y0, x1, _y1 = self._plot_box()
        if width + 12 > (x1 - _x0):
            return
        bx1, by0 = x1 - 3, y0 + 3
        bx0, by1 = bx1 - width, by0 + 13
        self.create_rectangle(bx0, by0, bx1, by1,
                              fill=_blend(C.panel, C.plot_bg, 0.25),
                              outline=_blend(C.border, C.text_dim, 0.2))
        self.create_text((bx0 + bx1) / 2, (by0 + by1) / 2, text=text,
                         fill=color, font=self._legend_font)


def _step_points(points: Sequence[float]) -> list[float]:
    """Sample-and-hold polyline: the square wave GPU-Z draws.

    Linear interpolation turns a 0 -> 100 -> 0 utilisation burst into a
    triangle, which reads as a ramp that never happened. Repeating each y at the
    next sample's x keeps the value flat until the following sample.
    """
    out: list[float] = []
    for i in range(0, len(points) - 2, 2):
        x0, y0 = points[i], points[i + 1]
        x1 = points[i + 2]
        out.extend((x0, y0, x1, y0))
    out.extend(points[-2:])
    return out


class ThemedScrollbar(tk.Canvas):
    """A scrollbar drawn by us, so it can follow the theme.

    `tk.Scrollbar` is drawn by the window manager: on Windows it keeps the
    system trough and thumb colours whatever `troughcolor` says, which left a
    grey native bar beside a themed window. `ttk.Scrollbar` can be recoloured
    only under the `clam` theme, and switching ttk themes would restyle the
    summary window's notebook at the same time.

    Drawing it costs a canvas and about ten lines of geometry, and buys a
    scrollbar that is the same colour as everything around it. It presents the
    same interface as `tk.Scrollbar` - `set(first, last)` and a `command`
    callable - so call sites do not change.
    """

    #: Minimum thumb length in pixels, so a huge document still has a grip.
    MIN_THUMB = 24

    def __init__(self, master: tk.Misc, *, orient: str = "vertical",
                 command: Callable[..., None] | None = None, thickness: int = 11,
                 bg: str | None = None, **kwargs) -> None:
        self.orient = orient
        self.vertical = orient == "vertical"
        if self.vertical:
            super().__init__(master, width=thickness, height=40,
                             bg=bg or C.panel_alt, highlightthickness=0, bd=0,
                             **kwargs)
        else:
            super().__init__(master, width=40, height=thickness,
                             bg=bg or C.panel_alt, highlightthickness=0, bd=0,
                             **kwargs)
        self.command = command
        self.thickness = thickness
        self._first, self._last = 0.0, 1.0
        self._thumb = (0.0, 0.0, 0.0, 0.0)
        self._drag_grab: float | None = None
        self._hover = False
        self.bind("<Configure>", lambda _e: self._render())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    # -- interface compatible with tk.Scrollbar --------------------------
    def set(self, first: float | str, last: float | str) -> None:
        try:
            self._first, self._last = float(first), float(last)
        except (TypeError, ValueError):
            return
        if self._last - self._first >= 0.999:
            # Nothing to scroll: park the thumb over the whole track.
            self._first, self._last = 0.0, 1.0
        self._render()

    def get(self) -> tuple[float, float]:
        return self._first, self._last

    # -- geometry --------------------------------------------------------
    def _track(self) -> float:
        if self.vertical:
            return max(1.0, self.winfo_height() - 2)
        return max(1.0, self.winfo_width() - 2)

    def _thumb_box(self) -> tuple[float, float, float, float]:
        track = self._track()
        span = max(0.0, min(1.0, self._last - self._first))
        length = max(float(self.MIN_THUMB), span * track)
        if length >= track:
            length = track
        start = max(0.0, min(track - length, self._first * track))
        if self.vertical:
            x0, x1 = 1.0, self.winfo_width() - 1.0
            return x0, start + 1, x1, start + 1 + length
        y0, y1 = 1.0, self.winfo_height() - 1.0
        return start + 1, y0, start + 1 + length, y1

    def _render(self) -> None:
        self.delete("all")
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            return
        trough = _blend(C.panel_alt, C.bg, 0.35)
        self.create_rectangle(0, 0, width, height, fill=trough, outline="")
        self._thumb = self._thumb_box()
        length = (self._thumb[3] - self._thumb[1]) if self.vertical \
            else (self._thumb[2] - self._thumb[0])
        if length >= self._track() - 0.5:
            # Everything fits: a full-length grip would just read as a solid
            # bar, so leave the trough empty.
            return
        if self._drag_grab is not None:
            colour = C.accent
        elif self._hover:
            colour = _blend(C.idle, C.text_dim, 0.55)
        else:
            colour = C.idle
        self.create_rectangle(*self._thumb, fill=colour, outline="")

    # -- interaction -----------------------------------------------------
    def _on_enter(self, _event: tk.Event) -> None:
        self._hover = True
        self._render()

    def _on_leave(self, _event: tk.Event) -> None:
        self._hover = False
        self._render()

    def _position(self, event: tk.Event) -> float:
        return float(event.y if self.vertical else event.x)

    def _on_press(self, event: tk.Event) -> None:
        x0, y0, x1, y1 = self._thumb
        pos = self._position(event)
        low, high = (y0, y1) if self.vertical else (x0, x1)
        if low <= pos <= high:
            # Grab: remember where inside the thumb the pointer landed so the
            # thumb does not jump under the cursor.
            self._drag_grab = pos - low
        else:
            # Page: centre the thumb on the click, like a native scrollbar.
            self._drag_grab = (high - low) / 2
            self._move_to(pos - self._drag_grab)
        self._render()

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag_grab is None:
            return
        self._move_to(self._position(event) - self._drag_grab)

    def _on_release(self, _event: tk.Event) -> None:
        self._drag_grab = None
        self._render()

    def _move_to(self, top: float) -> None:
        track = self._track()
        x0, y0, x1, y1 = self._thumb
        length = (y1 - y0) if self.vertical else (x1 - x0)
        span = max(1.0, track - length)
        fraction = max(0.0, min(1.0, (top - 1.0) / span))
        if self.command is not None:
            self.command("moveto", fraction)


def _blend(color: str, other: str, amount: float) -> str:
    """Mix `color` toward `other` by `amount` (0 = color, 1 = other)."""
    try:
        r1, g1, b1 = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
        r2, g2, b2 = int(other[1:3], 16), int(other[3:5], 16), int(other[5:7], 16)
    except (ValueError, IndexError):
        return color
    r = int(r1 + (r2 - r1) * amount)
    g = int(g1 + (g2 - g1) * amount)
    b = int(b1 + (b2 - b1) * amount)
    return f"#{r:02x}{g:02x}{b:02x}"


def action_colours(kind: str = "ok") -> tuple[str, str]:
    """(background, foreground) for a positive or destructive action button.

    Derived from the palette instead of hardcoded. A dark green pill with green
    text reads well on every dark theme and becomes unreadable on the light one,
    where the same pair has to come out as a pale tint with dark text.
    """
    ink = C.ok if kind == "ok" else C.crit
    return _blend(C.panel_alt, ink, 0.22), ink


# --------------------------------------------------------------------------
# Stepped fill bar (nvtop-style meter)
# --------------------------------------------------------------------------


class MeterBar(tk.Canvas):
    """Horizontal segmented bar used for utilisation figures."""

    SEGMENTS = 20

    def __init__(self, master: tk.Misc, *, width: int = 160, height: int = 12,
                 color: str | None = None, bg: str | None = None,
                 **kwargs) -> None:
        super().__init__(master, width=width, height=height, bg=bg or C.panel,
                         highlightthickness=0, bd=0, **kwargs)
        self.w, self.h = width, height
        self.color = color or C.accent

    def render(self, fraction: float, color: str | None = None) -> None:
        self.delete("all")
        frac = 0.0 if fraction != fraction else max(0.0, min(1.0, fraction))
        seg_w = self.w / self.SEGMENTS
        gap = max(1.0, seg_w * 0.22)
        filled = int(round(frac * self.SEGMENTS))
        col = color or self.color
        for i in range(self.SEGMENTS):
            x0 = i * seg_w
            x1 = x0 + seg_w - gap
            on = i < filled
            self.create_rectangle(
                x0, 0, x1, self.h, outline="",
                fill=col if on else C.idle)


# --------------------------------------------------------------------------
# Big time-series chart for the summary window
# --------------------------------------------------------------------------


@dataclass
class ChartSeries:
    label: str
    color: str
    points: list[tuple[float, float]] = field(default_factory=list)
    dashed: bool = False
    width: int = 1
    axis: str = "left"          # left | right


@dataclass
class ChartMarker:
    x: float
    label: str
    color: str
    kind: str = "alarm"         # alarm | user


class TimeSeriesChart(tk.Canvas):
    """Line chart with two y-axes, threshold bands, event markers and a legend.

    Points are decimated to the pixel width before drawing, so a 4-hour log at
    1 Hz (14400 samples) renders as a single polyline per series.
    """

    PAD_LEFT = 54
    PAD_RIGHT = 56
    PAD_TOP = 26
    PAD_BOTTOM = 30

    def __init__(self, master: tk.Misc, *, width: int = 900, height: int = 260,
                 x_label: str = "time (s)", y_label: str = "",
                 resizable: bool = True, **kwargs) -> None:
        super().__init__(master, width=width, height=height, bg=C.panel,
                         highlightthickness=1, highlightbackground=C.border,
                         bd=0, **kwargs)
        self.w, self.h = width, height
        self.x_label, self.y_label = x_label, y_label
        self.series: list[ChartSeries] = []
        self.markers: list[ChartMarker] = []
        self.bands: list[tuple[float, float, str, str]] = []   # lo, hi, color, label
        self.left_range: tuple[float, float] | None = None
        self.right_range: tuple[float, float] | None = None
        self.right_label = ""
        self.left_label = ""
        self._x_range: tuple[float, float] = (0.0, 1.0)
        #: (mouse_x, mouse_y) while the pointer is over the plot, else None.
        self._hover: tuple[int, int] | None = None
        self._font_cache: tkfont.Font | None = None
        #: Drag-to-select a time window. `on_range_selected` receives (t0, t1).
        self.on_range_selected: Callable[[float, float], None] | None = None
        self._drag_start: float | None = None
        self._drag_now: float | None = None
        #: Shaded window currently applied elsewhere, drawn for context.
        self.selection: tuple[float, float] | None = None
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        if resizable:
            self.bind("<Configure>", self._on_resize)

    def _on_resize(self, event: tk.Event) -> None:
        """Follow the container so charts fill the window instead of clipping."""
        if event.width > 50 and event.height > 50:
            if (event.width, event.height) != (self.w, self.h):
                self.w, self.h = event.width, event.height
                self._max_points = max(2, self.w * 2)
                self.render()

    # -- range selection -------------------------------------------------
    def _x_to_time(self, x: float) -> float:
        x0, _y0, x1, _y1 = self._plot_box()
        ax0, ax1 = self._x_range
        frac = (x - x0) / max(1.0, x1 - x0)
        return ax0 + frac * (ax1 - ax0)

    def _on_press(self, event: tk.Event) -> None:
        x0, y0, x1, y1 = self._plot_box()
        if x0 <= event.x <= x1 and y0 <= event.y <= y1:
            self._drag_start = self._x_to_time(event.x)
            self._drag_now = self._drag_start

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag_start is None:
            return
        self._drag_now = self._x_to_time(event.x)
        self.render()

    def _on_release(self, event: tk.Event) -> None:
        if self._drag_start is None:
            return
        end = self._x_to_time(event.x)
        start, stop = sorted((self._drag_start, end))
        self._drag_start = None
        self._drag_now = None
        # Ignore an accidental click: a real drag spans a useful window.
        if stop - start < (self._x_range[1] - self._x_range[0]) * 0.005:
            self.render()
            return
        if self.on_range_selected is not None:
            self.on_range_selected(start, stop)
        self.render()

    # -- data ------------------------------------------------------------
    def set_data(self, series: Sequence[ChartSeries],
                 markers: Sequence[ChartMarker] = (),
                 bands: Sequence[tuple[float, float, str, str]] = ()) -> None:
        self.series = list(series)
        self.markers = list(markers)
        self.bands = list(bands)
        xs = [x for s in self.series for x, _ in s.points]
        if xs:
            self._x_range = (min(xs), max(xs))
            if self._x_range[0] == self._x_range[1]:
                self._x_range = (self._x_range[0], self._x_range[0] + 1.0)
        self.render()

    def set_ranges(self, left: tuple[float, float] | None,
                   right: tuple[float, float] | None) -> None:
        self.left_range, self.right_range = left, right

    # -- coordinates -----------------------------------------------------
    def _plot_box(self) -> tuple[float, float, float, float]:
        return (self.PAD_LEFT, self.PAD_TOP,
                self.w - self.PAD_RIGHT, self.h - self.PAD_BOTTOM)

    def _data_ranges(self) -> tuple[tuple[float, float], tuple[float, float] | None]:
        left = self.left_range
        right = self.right_range
        if left is None:
            vals = [v for s in self.series if s.axis == "left" for _, v in s.points]
            if vals:
                lo, hi = min(vals), max(vals)
                # Include threshold bands only where they are relevant. A band
                # far above the data (e.g. a 70-85 C warning band on a 35 C run)
                # would otherwise stretch the axis down to 0 and flatten the
                # trace into a straight line.
                span = (hi - lo) or 1.0
                for blo, bhi, _c, _lbl in self.bands:
                    if blo <= hi + span and bhi >= lo - span:
                        lo = min(lo, blo)
                        hi = max(hi, bhi)
                pad = (hi - lo) * 0.08 or 1.0
                left = (lo - pad, hi + pad)
            else:
                left = (0.0, 1.0)
        if right is None:
            vals = [v for s in self.series if s.axis == "right" for _, v in s.points]
            if vals:
                lo, hi = min(vals), max(vals)
                pad = (hi - lo) * 0.08 or 1.0
                right = (lo - pad, hi + pad)
            else:
                right = None
        return left, right

    @staticmethod
    def _scale(value: float, lo: float, hi: float, p0: float, p1: float) -> float:
        span = (hi - lo) or 1.0
        frac = (value - lo) / span
        return p0 + frac * (p1 - p0)

    # -- rendering -------------------------------------------------------
    def render(self) -> None:
        self.delete("all")
        x0, y0, x1, y1 = self._plot_box()
        (lx0, lx1), right = self._data_ranges()
        ax0, ax1 = self._x_range

        self._draw_bands(x0, y0, x1, y1, lx0, lx1)
        self._draw_grid(x0, y0, x1, y1, lx0, lx1, right)
        for s in self.series:
            self._draw_series(s, x0, y0, x1, y1, ax0, ax1, lx0, lx1, right)
        self._draw_selection(x0, y0, x1, y1, ax0, ax1)
        self._draw_markers(x0, y0, x1, y1, ax0, ax1)
        self._draw_legend(x0, y0, x1)
        self._draw_axes_labels(x0, y0, x1, y1, lx0, lx1, right)
        if self._hover is not None:
            self._draw_hover(x0, y0, x1, y1, ax0, ax1)

    def _draw_bands(self, x0: float, y0: float, x1: float, y1: float,
                    lx0: float, lx1: float) -> None:
        for lo, hi, color, label in self.bands:
            ya = self._scale(hi, lx0, lx1, y1, y0)
            yb = self._scale(lo, lx0, lx1, y1, y0)
            top, bottom = min(ya, yb), max(ya, yb)
            if bottom < y0 or top > y1:
                continue
            top = max(top, y0)
            bottom = min(bottom, y1)
            self.create_rectangle(x0, top, x1, bottom, fill=color, outline="")
            self.create_line(x0, top, x1, top, fill=_blend(color, "#ffffff", 0.25),
                             dash=(2, 4))
            if label:
                self.create_text(x1 - 6, top + 8, text=label, anchor="e",
                                 fill=C.text_dim, font=self._small())

    def _draw_grid(self, x0: float, y0: float, x1: float, y1: float,
                   lx0: float, lx1: float, right: tuple[float, float] | None) -> None:
        """Gridlines with labelled values, plus a shaded plot background."""
        self.create_rectangle(x0, y0, x1, y1, fill="#0d131c", outline="")
        for i in range(5):
            y = y0 + (y1 - y0) * i / 4
            self.create_line(x0, y, x1, y, fill=C.border, dash=(1, 3))
            val = lx1 - (lx1 - lx0) * i / 4
            self.create_text(x0 - 6, y, text=_axis_num(val), anchor="e",
                             fill=C.text_dim, font=self._small())
            if right is not None:
                rval = right[1] - (right[1] - right[0]) * i / 4
                self.create_text(x1 + 6, y, text=_axis_num(rval), anchor="w",
                                 fill=_blend(C.text_dim, C.accent, 0.25),
                                 font=self._small())
        # Vertical gridlines get time labels so the x-axis is readable without
        # hovering.
        ax0, ax1 = self._x_range
        for i in range(5):
            x = x0 + (x1 - x0) * i / 4
            self.create_line(x, y0, x, y1, fill=C.border, dash=(1, 3))
            tval = ax0 + (ax1 - ax0) * i / 4
            self.create_text(x, y1 + 6, text=_time_label(tval), anchor="n",
                             fill=C.text_dim, font=self._small())

    def _draw_series(self, s: ChartSeries, x0: float, y0: float, x1: float, y1: float,
                     ax0: float, ax1: float, lx0: float, lx1: float,
                     right: tuple[float, float] | None) -> None:
        if len(s.points) < 2:
            return
        span_x = (ax1 - ax0) or 1.0
        max_points = max(2, int(x1 - x0) * 2)
        step = max(1, len(s.points) // max_points)
        pts = s.points[::step]
        if s.points[-1] is not pts[-1]:
            pts = pts + [s.points[-1]]
        if s.axis == "right" and right is not None:
            lo, hi = right
        else:
            lo, hi = lx0, lx1
        coords: list[float] = []
        for x, v in pts:
            px = x0 + (x - ax0) / span_x * (x1 - x0)
            py = self._scale(v, lo, hi, y1, y0)
            py = max(y0, min(y1, py))
            coords.extend((px, py))
        if len(coords) >= 4:
            self.create_line(coords, fill=s.color, width=s.width,
                             dash=(4, 3) if s.dashed else ())

    def _draw_selection(self, x0: float, y0: float, x1: float, y1: float,
                        ax0: float, ax1: float) -> None:
        """Shade the chosen window, or the one being dragged out right now."""
        span_x = (ax1 - ax0) or 1.0

        def to_px(t: float) -> float:
            return x0 + (t - ax0) / span_x * (x1 - x0)

        if self.selection is not None:
            a, b = self.selection
            self.create_rectangle(to_px(a), y0, to_px(b), y1,
                                  fill=C.selection, outline="")
            for t in (a, b):
                self.create_line(to_px(t), y0, to_px(t), y1, fill=C.accent)
        if self._drag_start is not None and self._drag_now is not None:
            a, b = sorted((self._drag_start, self._drag_now))
            self.create_rectangle(to_px(a), y0, to_px(b), y1,
                                  fill=C.selection, outline="")
            self.create_line(to_px(a), y0, to_px(a), y1, fill=C.accent)
            self.create_line(to_px(b), y0, to_px(b), y1, fill=C.accent)
            # Live readout of the window being dragged, so the numbers are
            # visible before committing.
            self.create_text((to_px(a) + to_px(b)) / 2, y0 + 10,
                             text=f"{a:.1f}s - {b:.1f}s  ({b - a:.1f}s)",
                             fill=C.text_bright, font=self._small())

    def _draw_markers(self, x0: float, y0: float, x1: float, y1: float,
                      ax0: float, ax1: float) -> None:
        span_x = (ax1 - ax0) or 1.0
        for m in self.markers:
            if not (ax0 <= m.x <= ax1):
                continue
            px = x0 + (m.x - ax0) / span_x * (x1 - x0)
            self.create_line(px, y0, px, y1, fill=m.color,
                             dash=(3, 3) if m.kind == "user" else (1, 2))
            if m.kind == "user":
                self.create_text(px + 3, y0 + 10, text=m.label, anchor="nw",
                                 fill=m.color, font=self._small())

    def _draw_legend(self, x0: float, y0: float, x1: float) -> None:
        x = x0 + 4
        y = y0 - 12
        for s in self.series:
            if not s.points:
                continue
            self.create_line(x, y, x + 16, y, fill=s.color, width=2,
                             dash=(4, 3) if s.dashed else ())
            self.create_text(x + 20, y, text=s.label, anchor="w", fill=C.text,
                             font=self._small())
            x += 26 + len(s.label) * 6

    def _draw_axes_labels(self, x0: float, y0: float, x1: float, y1: float,
                          lx0: float, lx1: float,
                          right: tuple[float, float] | None) -> None:
        if self.left_label:
            self.create_text(x0 - 44, y0 - 12, text=self.left_label, anchor="w",
                             fill=C.text_dim, font=self._small())
        if right is not None and self.right_label:
            self.create_text(x1 + 44, y0 - 12, text=self.right_label, anchor="e",
                             fill=_blend(C.text_dim, C.accent, 0.25), font=self._small())
        if self.x_label:
            self.create_text((x0 + x1) / 2, y1 + 20, text=self.x_label,
                             fill=C.text_dim, font=self._small())

    def _on_motion(self, event: tk.Event) -> None:
        x0, _y0, x1, _y1 = self._plot_box()
        inside = x0 <= event.x <= x1
        # Mouse position plus the vertical position, so the readout can mark the
        # point on the line nearest the cursor rather than only the cursor's x.
        new_state = (event.x, event.y) if inside else None
        if new_state != self._hover:
            self._hover = new_state
            self.render()

    def _on_leave(self, _event: tk.Event) -> None:
        if self._hover is not None:
            self._hover = None
            self.render()

    def _draw_hover(self, x0: float, y0: float, x1: float, y1: float,
                    ax0: float, ax1: float) -> None:
        """Vertical guide plus a value for every series at that time.

        Values are taken from the sample nearest the guide, so the number shown
        is always a real recorded value rather than an interpolation.
        """
        assert self._hover is not None
        mouse_x, mouse_y = self._hover
        span_x = (ax1 - ax0) or 1.0
        t = ax0 + (mouse_x - x0) / max(1.0, x1 - x0) * span_x

        rows = self._hover_rows(t)
        if not rows:
            return

        # Guide line at the sample actually being reported, not the cursor.
        nearest_t = rows[0][3]
        sample_x = x0 + (nearest_t - ax0) / span_x * (x1 - x0)
        self.create_line(sample_x, y0, sample_x, y1, fill=C.text_dim)

        # Marker on the series whose point is closest to the cursor.
        best: tuple[float, int, float, float, str] | None = None
        lx0, lx1 = self._data_ranges()[0]
        right = self._data_ranges()[1]
        for idx, (label, color, value, _tt, axis) in enumerate(rows):
            lo, hi = (right if axis == "right" and right else (lx0, lx1))
            py = self._scale(value, lo, hi, y1, y0)
            py = max(y0, min(y1, py))
            px = sample_x
            dist = abs(py - mouse_y)
            if best is None or dist < best[0]:
                best = (dist, idx, px, py, color)
        if best is not None:
            _d, idx, px, py, color = best
            self.create_line(px - 5, py, px + 5, py, fill=color, width=2)
            self.create_line(px, py - 5, px, py + 5, fill=color, width=2)
            self.create_oval(px - 3, py - 3, px + 3, py + 3, outline=color,
                             width=1)

        # Readout box. Rows are ordered with the highlighted series first so the
        # answer to "what is this line" is the first thing read.
        if best is not None:
            hi_idx = best[1]
            rows = [rows[hi_idx]] + [r for i, r in enumerate(rows) if i != hi_idx]
        title = f"t = {nearest_t:.1f}s"
        width_units = max([len(title)] + [len(r[0]) + len(_fmt_hover(r[2])) + 2
                                          for r in rows])
        box_w = 10 + width_units * 7
        box_h = 20 + 15 * len(rows)
        box_x = sample_x + 12
        if box_x + box_w > x1:
            box_x = sample_x - 12 - box_w
        box_x = max(x0 + 2, box_x)
        box_y = max(y0 + 2, min(mouse_y - box_h / 2, y1 - box_h))
        self.create_rectangle(box_x, box_y, box_x + box_w, box_y + box_h,
                              fill=C.panel_alt, outline=C.border)
        self.create_text(box_x + 6, box_y + 9, text=title, anchor="w",
                         fill=C.text_bright, font=self._small())
        for i, (label, color, value, _tt, _axis) in enumerate(rows):
            row_y = box_y + 22 + 15 * i
            self.create_line(box_x + 6, row_y, box_x + 18, row_y, fill=color,
                             width=2)
            self.create_text(box_x + 22, row_y, text=label, anchor="w",
                             fill=C.text, font=self._small())
            self.create_text(box_x + box_w - 6, row_y,
                             text=_fmt_hover(value), anchor="e",
                             fill=C.text_bright, font=self._small())

    def _hover_rows(self, t: float) -> list[tuple[str, str, float, float, str]]:
        """(label, colour, value, sample time, axis) for each series near `t`."""
        rows: list[tuple[str, str, float, float, str]] = []
        for s in self.series:
            hit = _nearest(s.points, t)
            if hit is not None:
                rows.append((s.label, s.color, hit[1], hit[0], s.axis))
        return rows

    def _small(self) -> tuple[str, int]:
        return ("Consolas", 8)


def _nearest(points: Sequence[tuple[float, float]], t: float) -> tuple[float, float] | None:
    if not points:
        return None
    lo, hi = 0, len(points) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if points[mid][0] < t:
            lo = mid + 1
        else:
            hi = mid
    best = points[lo]
    if lo > 0 and abs(points[lo - 1][0] - t) < abs(best[0] - t):
        best = points[lo - 1]
    return best


def _axis_num(value: float) -> str:
    if abs(value) >= 10000:
        return f"{value / 1000:.0f}k"
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.0f}"
    return f"{value:.1f}"


#: How a metric reads in a hover readout: (name, unit). The keys are the program's
#: own, so anywhere a chart is drawn the guide says "GPU 42%" rather than
#: "gpu0_util 42" - which is a key, not an answer.
_HOVER_LABELS: dict[str, tuple[str, str]] = {
    "cpu_util": ("CPU", "%"),
    "cpu_temp": ("TEMP", "\u00b0C"),
    "cpu_clock": ("CPU", "MHz"),
    "ram_percent": ("RAM", "%"),
    "ram_used": ("RAM", "GB"),
    "temp": ("TEMP", "\u00b0C"),
    "hotspot": ("hotspot", "\u00b0C"),
    "mem_temp": ("mem", "\u00b0C"),
    "util": ("GPU", "%"),
    "mem_util": ("mem", "%"),
    "vram_percent": ("VRAM", "%"),
    "vram_used": ("VRAM", "MB"),
    "vram_total": ("VRAM", "MB"),
    "clock_core": ("CORE", "MHz"),
    "clock_mem": ("MEM", "MHz"),
    "power": ("PWR", "W"),
    "power_percent": ("PWR", "%"),
    "fan": ("FAN", "rpm"),
    "fan_percent": ("FAN", "%"),
    "temp_limit": ("limit", "\u00b0C"),
}


def hover_label(key: str) -> tuple[str, str]:
    """(name, unit) for a metric key, falling back to the key itself."""
    return _HOVER_LABELS.get(key, (key, ""))


def _fmt_hover(value: float) -> str:
    """Readable number for a hover readout: keeps detail at any magnitude."""
    magnitude = abs(value)
    if magnitude >= 1000:
        return f"{value:,.0f}"
    if magnitude >= 100:
        return f"{value:.1f}"
    if magnitude >= 1:
        return f"{value:.2f}"
    return f"{value:.3f}"


def _time_label(seconds: float) -> str:
    """Compact x-axis label: mm:ss once past a minute, else seconds."""
    if abs(seconds) < 60:
        return f"{seconds:.0f}s"
    minutes, sec = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}:{sec:02d}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}"


def _spark_num(value: float) -> str:
    """Very compact number for in-graph annotations, where space is scarce."""
    magnitude = abs(value)
    if magnitude >= 10000:
        return f"{value / 1000:.0f}k"
    if magnitude >= 1000:
        return f"{value / 1000:.1f}k"
    if magnitude >= 100:
        return f"{value:.0f}"
    if magnitude >= 10:
        return f"{value:.0f}"
    if magnitude >= 1:
        return f"{value:.1f}"
    # Zero is the bottom of most scales here, and "0.00" beside "100" and "50"
    # on the same axis reads like a different unit.
    if value == 0:
        return "0"
    return f"{value:.2f}"


# --------------------------------------------------------------------------
# Histogram
# --------------------------------------------------------------------------


class HistogramChart(tk.Canvas):
    """Distribution of one metric, with percentile markers."""

    PAD_LEFT = 42
    PAD_RIGHT = 12
    PAD_TOP = 20
    PAD_BOTTOM = 26

    def __init__(self, master: tk.Misc, *, width: int = 440, height: int = 180,
                 bins: int = 40, color: str | None = None, **kwargs) -> None:
        super().__init__(master, width=width, height=height, bg=C.panel,
                         highlightthickness=1, highlightbackground=C.border,
                         bd=0, **kwargs)
        self.color = color or C.accent
        self.w, self.h = width, height
        self.bins = bins
        self.title = ""
        self.unit = ""
        self.values: list[float] = []
        self.notes: list[tuple[float, str, str]] = []   # x, label, color

    def set_values(self, values: Sequence[float], title: str = "", unit: str = "",
                   notes: Sequence[tuple[float, str, str]] = ()) -> None:
        self.values = list(values)
        self.title = title
        self.unit = unit
        self.notes = list(notes)
        self.render()

    def render(self) -> None:
        self.delete("all")
        x0, y0 = self.PAD_LEFT, self.PAD_TOP
        x1, y1 = self.w - self.PAD_RIGHT, self.h - self.PAD_BOTTOM
        if self.title:
            self.create_text(x0, 8, text=self.title, anchor="w",
                             fill=C.text_bright, font=("Consolas", 9, "bold"))
        if not self.values:
            self.create_text((x0 + x1) / 2, (y0 + y1) / 2, text="no data",
                             fill=C.text_dim, font=("Consolas", 9))
            return
        lo, hi = min(self.values), max(self.values)
        if hi - lo < 1e-9:
            hi = lo + 1.0
        counts = [0] * self.bins
        for v in self.values:
            idx = int((v - lo) / (hi - lo) * self.bins)
            idx = max(0, min(self.bins - 1, idx))
            counts[idx] += 1
        peak = max(counts) or 1
        bar_w = (x1 - x0) / self.bins
        for i, c in enumerate(counts):
            if not c:
                continue
            bh = (c / peak) * (y1 - y0)
            bx0 = x0 + i * bar_w
            self.create_rectangle(bx0, y1 - bh, bx0 + bar_w - 1, y1,
                                  fill=_blend(self.color, C.bg, 0.45), outline="")
        # axis
        self.create_line(x0, y1, x1, y1, fill=C.border)
        self.create_line(x0, y0, x0, y1, fill=C.border)
        for i in range(5):
            gx = x0 + (x1 - x0) * i / 4
            val = lo + (hi - lo) * i / 4
            self.create_text(gx, y1 + 12, text=_axis_num(val), fill=C.text_dim,
                             font=("Consolas", 8))
        self.create_text(x0 - 6, y0, text=str(peak), anchor="e", fill=C.text_dim,
                         font=("Consolas", 8))
        self.create_text(x0 - 6, y1, text="0", anchor="e", fill=C.text_dim,
                         font=("Consolas", 8))
        for x, label, color in self.notes:
            if not (lo <= x <= hi):
                continue
            px = x0 + (x - lo) / (hi - lo) * (x1 - x0)
            self.create_line(px, y0, px, y1, fill=color, dash=(3, 3))
            self.create_text(px + 3, y0 + 6, text=label, anchor="nw", fill=color,
                             font=("Consolas", 8))
        if self.unit:
            self.create_text(x1, y1 + 12, text=self.unit, anchor="e",
                             fill=C.text_dim, font=("Consolas", 8))


# --------------------------------------------------------------------------
# Simple table
# --------------------------------------------------------------------------


class StatTable(tk.Frame):
    """A compact read-only table built from labels, for metric statistics."""

    def __init__(self, master: tk.Misc, columns: Sequence[tuple[str, int, str]],
                 *, theme: Theme, rows: int = 0, **kwargs) -> None:
        super().__init__(master, bg=C.panel, **kwargs)
        self.columns = list(columns)
        self.theme = theme
        for idx, (title, width, anchor) in enumerate(self.columns):
            lbl = tk.Label(self, text=title, bg=C.panel, fg=C.text_dim,
                           font=theme.small, width=width, anchor=anchor)
            lbl.grid(row=0, column=idx, sticky="ew", padx=(2, 0), pady=(0, 2))
        self._rows: list[list[tk.Label]] = []
        for r in range(rows):
            self._add_row(r + 1)
        self.grid_columnconfigure(tuple(range(len(self.columns))), weight=0)

    def _add_row(self, row: int) -> list[tk.Label]:
        cells: list[tk.Label] = []
        for idx, (_title, width, anchor) in enumerate(self.columns):
            bg = C.panel if row % 2 else C.panel_alt
            lbl = tk.Label(self, text="", bg=bg, fg=C.text, font=self.theme.small,
                           width=width, anchor=anchor)
            lbl.grid(row=row, column=idx, sticky="ew", pady=0)
            cells.append(lbl)
        self._rows.append(cells)
        return cells

    def set_rows(self, data: Sequence[Sequence[str]],
                 colors: Sequence[Sequence[str]] | None = None) -> None:
        while len(self._rows) < len(data):
            self._add_row(len(self._rows) + 1)
        for r, row in enumerate(data):
            for c, text in enumerate(row):
                if c >= len(self._rows[r]):
                    break
                lbl = self._rows[r][c]
                lbl.configure(text=str(text))
                if colors and r < len(colors) and c < len(colors[r]):
                    lbl.configure(fg=colors[r][c] or C.text)
        for r in range(len(data), len(self._rows)):
            for lbl in self._rows[r]:
                lbl.configure(text="")


# --------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------


class FlatButton(tk.Label):
    """Canvas-free flat button; tk.Button cannot be themed on Windows."""

    def __init__(self, master: tk.Misc, text: str, command: Callable[[], None],
                 *, bg: str | None = None, fg: str | None = None, theme: Theme,
                 padx: int = 10, pady: int = 4, **kwargs) -> None:
        bg = bg or C.panel_alt
        fg = fg or C.text
        super().__init__(master, text=text, bg=bg, fg=fg, font=theme.body,
                         padx=padx, pady=pady, cursor="hand2", **kwargs)
        self._command = command
        self._bg = bg
        self._fg = fg
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", lambda _e: self.configure(bg=_blend(self._bg, "#ffffff", 0.12)))
        self.bind("<Leave>", lambda _e: self.configure(bg=self._bg))

    def _on_click(self, _event: tk.Event) -> None:
        self._command()

    def set_style(self, bg: str | None = None, fg: str | None = None) -> None:
        if bg:
            self._bg = bg
            self.configure(bg=bg)
        if fg:
            self._fg = fg
            self.configure(fg=fg)

    def set_text(self, text: str) -> None:
        self.configure(text=text)
