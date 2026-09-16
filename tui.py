"""The terminal (command-line) interface.

Layout mirrors the desktop window: a header with actionable buttons, a tab bar,
a scrolling body, and a footer of key hints. Everything is reachable from the
keyboard alone, with no clicks required:

  Tab / Shift-Tab   move between the header buttons
  Enter / Space     activate the focused button
  Left / Right      switch tab (or move the selection within a tab)
  Up / Down         scroll the body one line
  PgUp / PgDn       scroll a screenful
  Home / End        jump to the top or bottom
  T                 cycle the colour theme
  Esc / Q           quit (with a confirmation if logging)

The same navigation model is implemented three times across this project - here,
in the desktop window (`ui/monitor.py`) and in the browser GUI - so the keys mean
the same thing wherever you are.

Colours come from `ui/themes.py` by way of `termlib.apply_palette`, which
rebinds the `termlib` colour constants from the active palette. Nothing here
captures a colour at import time: every `T.FG_*` is read while a frame is built,
which is what lets T switch the theme without rebuilding the app.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import alarms as A
import metrics as M
import sampler as SP
import store as S
import termlib as T
from termlib import Key, Keyboard, Screen
from ui import themes as TH

#: Tabs, in order. Left/Right cycles these.
TABS = ("live", "stats", "alarms", "sessions", "help")

TAB_TITLES = {
    "live": "LIVE",
    "stats": "STATS",
    "alarms": "ALARMS",
    "sessions": "SESSIONS",
    "help": "HELP",
}

#: Which metric fields to show per GPU, and the scale for their bar/sparkline.
GPU_FIELDS: list[tuple[str, str, float, float, str]] = [
    ("temp", "TEMP", 0.0, 110.0, "C"),
    ("util", "GPU", 0.0, 100.0, "%"),
    ("vram_percent", "VRAM", 0.0, 100.0, "%"),
    ("clock_core", "CORE", 0.0, 2500.0, "MHz"),
    ("power_percent", "PWR", 0.0, 100.0, "%"),
]

FIELD_THRESHOLDS = {"temp": 80.0, "vram_percent": 95.0, "power_percent": 95.0}


@dataclass
class Button:
    label: str
    action: Callable[[], None]
    key_hint: str = ""


@dataclass
class TuiApp:
    """Owns the screen, the key loop and the three views."""

    manager: M.SensorManager
    store: S.Store
    sampler: SP.Sampler
    screen: Screen = field(default_factory=Screen)
    #: config.json to write the chosen theme to. Injectable for the same reason
    #: `screen` is: a test must be able to press T without touching the file
    #: that holds the user's alarm rules.
    config_path: str | None = None

    def __post_init__(self) -> None:
        self.caps = self.manager.capabilities()
        self.tab = "live"
        self.scroll = 0
        self.max_scroll = 0
        self.focus = 0
        self.running = True
        self.status_message = ""
        self.status_until = 0.0
        self._last_sample: dict[str, float] = {}
        self._alarms_seen: list[A.AlarmUpdate] = []
        self.buttons: list[Button] = [
            Button("START LOGGING", self.toggle_logging, "L"),
            Button("MARK", self.add_marker, "M"),
            Button("GRAPHS", lambda: None, "G"),
            Button("SUMMARY", lambda: None, "S"),
            Button("SESSIONS", lambda: self.set_tab("sessions"), "V"),
            Button("QUIT", self.quit, "Q"),
        ]
        self.sampler.on_sample = self._on_sample
        self.sampler.on_alarm = self._on_alarm
        # gpumon.py has already resolved --theme / config.json and called
        # themes.set_theme(), so current() is authoritative here; all this does
        # is put those colours on the terminal.
        self._apply_theme(TH.current())

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def _on_sample(self, _wall: float, values: dict[str, float]) -> None:
        # Called on the sampler thread: only copy, never draw.
        self._last_sample = dict(values)

    def _on_alarm(self, update: A.AlarmUpdate) -> None:
        self._alarms_seen.append(update)
        del self._alarms_seen[:-40]

    def values(self) -> dict[str, float]:
        return self._last_sample

    def series(self, metric: str, seconds: float = 60.0) -> list[float]:
        return [v for _t, v in self.sampler.live.window(metric, seconds)]

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def set_tab(self, tab: str) -> None:
        if tab in TABS:
            self.tab = tab
            self.scroll = 0
            self.focus = 0

    def cycle_tab(self, step: int) -> None:
        index = (TABS.index(self.tab) + step) % len(TABS)
        self.set_tab(TABS[index])

    def toggle_logging(self) -> None:
        if self.sampler.logging:
            summary = self.sampler.stop_logging()
            if summary is not None:
                self.notify(f"stopped session #{summary.session_id} "
                            f"({summary.samples} samples)")
            self.set_tab("stats")
        else:
            session_id = self.sampler.start_logging(label="tui session")
            self.notify(f"logging to session #{session_id}")

    def add_marker(self) -> None:
        if not self.sampler.logging:
            self.notify("start logging first", warn=True)
            return
        self.sampler.mark(f"marker at {time.strftime('%H:%M:%S')}")
        self.notify("marker added")

    def quit(self) -> None:
        self.running = False

    def notify(self, message: str, warn: bool = False) -> None:
        self.status_message = message
        self.status_until = time.monotonic() + (4.0 if warn else 2.5)

    def activate_focused(self) -> None:
        if 0 <= self.focus < len(self.buttons):
            self.buttons[self.focus].action()

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------
    def cycle_theme(self, step: int = 1) -> None:
        """Switch to the next theme in the catalogue, then persist it.

        The repaint costs nothing extra: every colour is read from `termlib`
        while a frame is being built, so the next frame already comes out in
        the new palette.
        """
        keys = TH.keys()
        try:
            index = keys.index(TH.current_key())
        except ValueError:
            index = 0
        palette = TH.set_theme(keys[(index + step) % len(keys)])
        self._apply_theme(palette)
        if self._save_theme(palette.key):
            self.notify(f"theme: {palette.label}")
        else:
            self.notify(f"theme: {palette.label} (not saved)", warn=True)

    def _apply_theme(self, palette) -> None:
        """Put `palette` on the terminal, replacing the previous one.

        Rebinding is the whole switch: `termlib`'s colour constants are what
        every draw call names, so there is no second copy of the colours to
        keep in step.
        """
        T.apply_palette(palette)

    def _save_theme(self, key: str) -> bool:
        """Record the theme in config.json, keeping the rest of the file.

        Read-modify-write, never a fresh document: that file also holds the
        user's alarm rules, and `save_config` replaces it wholesale. A
        read-only install is not a reason to end the session, so a failed write
        is reported rather than raised.
        """
        path = self.config_path or A.config_path()
        config = A.load_config(path)
        config["theme"] = key
        try:
            A.save_config(config, path)
        except OSError:
            return False
        return True

    def theme_text(self) -> str:
        """The theme in force, named for the status line (read as drawn)."""
        return f"theme: {TH.current().label}"

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------
    def handle(self, key: Key) -> None:
        name = key.name
        if name in ("q", "escape", "ctrl-c"):
            self.quit()
        elif name == "tab":
            self.focus = (self.focus + 1) % len(self.buttons)
        elif name in ("backtab",):
            self.focus = (self.focus - 1) % len(self.buttons)
        elif name in ("enter", " "):
            self.activate_focused()
        elif name == "left":
            if self.tab == "live" and self.focus:
                self.focus = (self.focus - 1) % len(self.buttons)
            else:
                self.cycle_tab(-1)
        elif name == "right":
            if self.tab == "live" and self.focus:
                self.focus = (self.focus + 1) % len(self.buttons)
            else:
                self.cycle_tab(1)
        elif name == "up":
            self.scroll = max(0, self.scroll - 1)
        elif name == "down":
            self.scroll = min(self.max_scroll, self.scroll + 1)
        elif name == "pageup":
            self.scroll = max(0, self.scroll - self.page_size())
        elif name == "pagedown":
            self.scroll = min(self.max_scroll, self.scroll + self.page_size())
        elif name == "home":
            self.scroll = 0
        elif name == "end":
            self.scroll = self.max_scroll
        elif name == "l":
            self.toggle_logging()
        elif name == "m":
            self.add_marker()
        elif name == "s":
            self.set_tab("stats")
        elif name == "a":
            self.set_tab("alarms")
        elif name == "t":
            self.cycle_theme()
        elif name == "1":
            self.set_tab("live")
        elif name == "2":
            self.set_tab("stats")
        elif name == "3":
            self.set_tab("alarms")
        elif name == "4":
            self.set_tab("sessions")
        elif name == "5":
            self.set_tab("help")

    def page_size(self) -> int:
        return max(1, self.screen.height - 6)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def render(self) -> None:
        screen = self.screen
        # The page colour is part of a theme - Nord's whole character is its
        # blue-grey ground - so the frame is cleared through it rather than
        # leaving the user's own terminal background showing through.
        screen.begin(T.BG_BG)
        width, height = screen.width, screen.height
        row = 1

        row = self._draw_header(row, width)
        row = self._draw_tabs(row, width)

        footer_rows = 2
        body_height = max(1, height - row - footer_rows + 1)
        lines = self._body_lines(width)
        self.max_scroll = max(0, len(lines) - body_height)
        self.scroll = min(self.scroll, self.max_scroll)
        for offset in range(body_height):
            index = self.scroll + offset
            if index >= len(lines):
                break
            colour, text = lines[index]
            screen.line(row + offset, 1, text, colour, width=width)

        self._draw_footer(height - footer_rows + 1, width)
        screen.flush()

    def _draw_header(self, row: int, width: int) -> int:
        screen = self.screen
        status = self.sampler.status()
        # Row 1: title, backend health, and the buttons.
        screen.line(row, 1, "", width=width, bg=T.BG_BAR)
        title = f" {T.BOLD}gpumon{T.RESET}{T.FG_DIM} terminal{T.RESET}"
        screen.at(row, 1)
        screen.text(title, T.FG_ACCENT, T.BG_BAR)

        backend = []
        for name, backend_obj in sorted(self.manager.backend_status().items()):
            mark = f"{T.FG_OK}up{T.RESET}" if backend_obj else f"{T.FG_CRIT}down{T.RESET}"
            backend.append(f"{T.FG_DIM}{name}={mark}")
        screen.at(row, min(30, max(12, width // 3)))
        screen.text(" ".join(backend), "", T.BG_BAR)

        # Buttons, right-aligned, with the focused one highlighted.
        positions: list[tuple[int, int]] = []
        cursor = width - 2
        for index in range(len(self.buttons) - 1, -1, -1):
            label = f" {self.buttons[index].label} "
            cursor -= len(label)
            positions.append((index, cursor + 1))
        for index, col in reversed(positions):
            label = f" {self.buttons[index].label} "
            focused = (index == self.focus and self.tab == "live")
            colour = T.FG_BRIGHT if focused else T.FG_DIM
            bg = T.BG_SELECT if focused else T.BG_BAR
            screen.line(row, col, label, colour, bg=bg)
        row += 1

        # Row 2: recording state and sampler health.
        if status.logging:
            session = self.store.get_session(status.session_id) if status.session_id else None
            label = f" {session.label}" if session and session.label else ""
            rec = (f" {T.FG_CRIT}{T.BOLD}REC{T.RESET}{T.FG_DEFAULT} "
                   f"session #{status.session_id}{label}  "
                   f"elapsed {S.human_duration(status.elapsed)}  "
                   f"samples {status.samples_written}")
        else:
            rec = (f" {T.FG_DIM}IDLE{T.RESET}{T.FG_DEFAULT}  "
                   f"{status.effective_hz:.1f} Hz  poll {status.poll_ms:.0f} ms  "
                   f"press L to start logging")
        # The theme is named on this row rather than in the footer: the key
        # hints fill the footer at any width that matters, and here there is
        # room to say which one is on without stealing a column from them.
        screen.line(row, 1, _right_aligned(rec, self.theme_text(), width),
                    T.FG_DEFAULT, width=width)
        row += 1
        if self.status_message and time.monotonic() < self.status_until:
            colour = T.FG_WARN if "start logging" in self.status_message else T.FG_OK
            screen.line(row, 1, f" {self.status_message}", colour, width=width)
            row += 1
        return row

    def _draw_tabs(self, row: int, width: int) -> int:
        screen = self.screen
        col = 1
        for tab in TABS:
            label = f" {TAB_TITLES[tab]} "
            active = (tab == self.tab)
            colour = T.FG_BRIGHT if active else T.FG_DIM
            bg = T.BG_SELECT if active else ""
            screen.line(row, col, label, colour, bg=bg)
            col += len(label) + 1
        screen.line(row, col, "\u2500" * max(0, width - col + 1), T.FG_FAINT,
                    width=max(0, width - col + 1))
        return row + 1

    def _draw_footer(self, row: int, width: int) -> None:
        screen = self.screen
        # The footer takes `panel_alt` where the header takes `panel`, so the two
        # bars keep the relationship the theme's own palette gives them instead
        # of both being flattened onto one colour.
        screen.line(row, 1, "", width=width, bg=T.BG_ALT)
        keys = ("\u2190\u2192 tab/select  \u2191\u2193 scroll  PgUp/PgDn  "
                "Home/End  Tab focus  Enter activate  L log  M mark  "
                "T theme  Q quit")
        screen.line(row, 1, f" {keys}", T.FG_DIM, bg=T.BG_ALT, width=width)
        first, last = self._scroll_fraction()
        bar_width = max(10, width - 20)
        filled = int(bar_width * first)
        end = max(filled, int(bar_width * last))
        bar = ("\u2588" * filled + "\u2592" * max(0, end - filled)
               + "\u2591" * max(0, bar_width - end))
        screen.line(row + 1, 1, f" {T.FG_FAINT}{bar}{T.RESET}", T.FG_FAINT,
                    width=width)

    def _scroll_fraction(self) -> tuple[float, float]:
        total = self.max_scroll + self.page_size()
        if total <= 0:
            return 0.0, 1.0
        first = self.scroll / total
        last = min(1.0, (self.scroll + self.page_size()) / total)
        return first, last

    # ------------------------------------------------------------------
    # Body content: (colour, text) lines
    # ------------------------------------------------------------------
    def _body_lines(self, width: int) -> list[tuple[str, str]]:
        if self.tab == "live":
            return self._live_lines(width)
        if self.tab == "stats":
            return self._stats_lines(width)
        if self.tab == "alarms":
            return self._alarm_lines(width)
        if self.tab == "sessions":
            return self._session_lines(width)
        return self._help_lines(width)

    def _live_lines(self, width: int) -> list[tuple[str, str]]:
        # `values` shadows the method of the same name inside this scope, which
        # is what the per-core section below must read from - reading the method
        # object there silently skipped the whole block.
        values = self.values()
        lines: list[tuple[str, str]] = []
        reset = T.FG_DEFAULT

        for gpu in self.caps.gpus:
            i = gpu.index
            temp = values.get(f"gpu{i}_temp")
            hotspot = values.get(f"gpu{i}_hotspot")
            ident = gpu.pci or "PCI ?"
            head = (f" {T.BOLD}gpu{i}{T.RESET}  {gpu.name}  "
                    f"{T.FG_DIM}{ident}  [{','.join(gpu.sources) or 'none'}]")
            lines.append((reset, head))

            big = _temp_colour(temp, values.get(f"gpu{i}_temp_limit"))
            extras = []
            if hotspot is not None:
                extras.append(f"hotspot {hotspot:.0f}C")
            if values.get(f"gpu{i}_mem_temp") is not None:
                extras.append(f"mem {values[f'gpu{i}_mem_temp']:.0f}C")
            if temp is None:
                extras.append("no thermal sensor")
            lines.append((reset, f"   {big}{T.BOLD}{_fmt(temp, 0)} C{T.RESET}"
                                 f"   {T.FG_DIM}{'  '.join(extras)}"))

            for field, label, lo, hi, unit in GPU_FIELDS:
                key = f"gpu{i}_{field}"
                value = values.get(key)
                series = self.series(key)
                bar = T.braille_bar((value or 0.0) / (hi or 1.0), 14) \
                    if value is not None else " " * 14
                spark = T.sparkline(series, 40, lo, hi)
                colour = _value_colour(field, value)
                shown = _field_text(field, values, i, unit)
                threshold = FIELD_THRESHOLDS.get(field)
                flag = ""
                if threshold is not None and value is not None and value >= threshold:
                    flag = f" {T.FG_WARN}!{T.RESET}"
                lines.append((reset,
                              f"   {T.FG_DIM}{label:<5}{T.RESET}"
                              f"{colour}{bar}{T.RESET} "
                              f"{T.FG_BRIGHT}{shown:>20}{T.RESET}{flag}  "
                              f"{T.FG_ACCENT}{spark}{T.RESET}"))

            active = [a for a in self.sampler.alarms.active.values()
                      if a.gpu_index == i]
            for alarm in active[:2]:
                lines.append((T.FG_CRIT if alarm.level == "critical" else T.FG_WARN,
                              f"   ! {alarm.label} {alarm.level.upper()} "
                              f"{alarm.peak:.0f} >= {alarm.threshold:.0f}"))
            lines.append((reset, ""))

        # CPU / memory card.
        lines.append((reset, f" {T.BOLD}cpu / memory{T.RESET}"))
        cpu = values.get("cpu_util")
        cpu_temp = values.get("cpu_temp")
        ram = values.get("ram_percent")
        ram_used = values.get("ram_used")
        ram_total = values.get("ram_total")
        lines.append((reset,
                      f"   {T.FG_DIM}CPU {T.RESET}{_value_colour('util', cpu)}"
                      f"{T.braille_bar((cpu or 0) / 100, 14)}{T.RESET} "
                      f"{T.FG_BRIGHT}{_fmt(cpu, 1):>5}%{T.RESET}  "
                      f"{T.FG_ACCENT}{T.sparkline(self.series('cpu_util'), 40)}{T.RESET}"))
        lines.append((reset,
                      f"   {T.FG_DIM}RAM {T.RESET}"
                      f"{_value_colour('util', ram)}{T.braille_bar((ram or 0) / 100, 14)}"
                      f"{T.RESET} {T.FG_BRIGHT}{_fmt(ram, 1):>5}%{T.RESET}  "
                      f"{T.FG_DIM}{_fmt(ram_used, 0)}/"
                      f"{_fmt(ram_total, 0)} MB{T.RESET}"))
        cpu_temp_text = (f"{cpu_temp:.1f} C" if cpu_temp is not None
                         else "n/a (see notes)")
        lines.append((reset,
                      f"   {T.FG_DIM}TEMP{T.RESET} "
                      f"{_temp_colour(cpu_temp, 100.0)}{cpu_temp_text:>7}{T.RESET}"))

        cores = sorted(k for k in values if k.startswith("core")
                       and k.endswith("_util"))
        if cores:
            lines.append((reset, ""))
            lines.append((reset, f" {T.BOLD}per-core{T.RESET}"))
            per_line = max(1, (width - 4) // 22)
            for start in range(0, len(cores), per_line):
                chunk = cores[start:start + per_line]
                cells = []
                for key in chunk:
                    value = values.get(key)
                    label = key[4:-5].lstrip("0") or "0"
                    cells.append(f"{T.FG_DIM}c{label:>2}{T.RESET}"
                                 f"{_value_colour('util', value)}"
                                 f"{T.braille_bar((value or 0) / 100, 6)}{T.RESET}"
                                 f"{_fmt(value, 0):>3}")
                lines.append((reset, "   " + " ".join(cells)))

        for note in self.caps.notes:
            lines.append((T.FG_FAINT, f" note: {note}"))
        return lines

    def _stats_lines(self, width: int) -> list[tuple[str, str]]:
        session_id = self.sampler.session_id
        if session_id is None:
            recent = self.store.list_sessions(limit=1)
            if not recent:
                return [(T.FG_DIM, " Nothing logged yet. Press L to start logging.")]
            session_id = recent[0].id
        session = self.store.get_session(session_id)
        stats = self.store.stats_many(session_id)
        if session is None or not stats:
            return [(T.FG_DIM, f" Session #{session_id} has no samples.")]

        lines: list[tuple[str, str]] = []
        lines.append((T.FG_BRIGHT,
                      f" {T.BOLD}session #{session_id}{T.RESET}"
                      f"  {session.label or '(unlabelled)'}  "
                      f"duration {S.human_duration(session.duration)}  "
                      f"samples {session.sample_count}"))
        lines.append((T.FG_FAINT, " " + T.hrule(width - 2)))
        header = (f" {'metric':<22}{'unit':>6}{'low':>9}{'avg':>9}{'high':>9}"
                  f"{'max':>9}{'p95':>9}{'p99':>9}{'n':>7}")
        lines.append((T.FG_HEAD, header))
        for key in sorted(stats):
            st = stats[key]
            d = M.metric_def(key)
            gpu = M.gpu_index_of(key)
            label = f"gpu{gpu} {d.label}" if gpu is not None else d.label
            prec = 1 if d.unit in ("%", "C", "W") else 0
            values = [v for _t, v in self.store.series(session_id, key)]
            low = S._percentile(values, 5.0) if values else st.minimum
            high = S._percentile(values, 95.0) if values else st.maximum
            colour = _value_colour(M.gpu_field_of(key), st.maximum)
            lines.append((colour,
                          f" {label[:21]:<22}{d.unit:>6}"
                          f"{low:>9.{prec}f}{st.mean:>9.{prec}f}"
                          f"{high:>9.{prec}f}{st.maximum:>9.{prec}f}"
                          f"{st.p95:>9.{prec}f}{st.p99:>9.{prec}f}{st.count:>7}"))
        lines.append((T.FG_FAINT,
                      "  low/high are the 5th/95th percentile; max is the true peak"))
        return lines

    def _alarm_lines(self, width: int) -> list[tuple[str, str]]:
        lines: list[tuple[str, str]] = []
        active = list(self.sampler.alarms.active.values())
        lines.append((T.FG_BRIGHT, f" {T.BOLD}active alarms ({len(active)}){T.RESET}"))
        if not active:
            lines.append((T.FG_OK, "   none"))
        for alarm in active:
            colour = T.FG_CRIT if alarm.level == "critical" else T.FG_WARN
            lines.append((colour,
                          f"   {alarm.label} {alarm.level.upper():8} "
                          f"peak {alarm.peak:.0f} >= {alarm.threshold:.0f}  "
                          f"for {S.human_duration(self.sampler.logging_elapsed() - alarm.started_t)}"))
        lines.append((T.FG_FAINT, " " + T.hrule(width - 2)))
        lines.append((T.FG_BRIGHT, f" {T.BOLD}recent crossings{T.RESET}"))
        recent = self._alarms_seen[-14:]
        if not recent:
            lines.append((T.FG_DIM, "   none yet"))
        for update in reversed(recent):
            alarm = update.alarm
            colour = T.FG_CRIT if alarm.level == "critical" else T.FG_WARN
            stamp = time.strftime("%H:%M:%S", time.localtime(update.wall))
            verb = {"open": "TRIGGERED", "close": "CLEARED",
                    "escalate": "ESCALATED"}[update.kind]
            lines.append((colour,
                          f"   {stamp}  {verb:<10} {alarm.label} {alarm.level} "
                          f"(peak {alarm.peak:.0f}, thr {alarm.threshold:.0f})"))
        lines.append((T.FG_FAINT, " " + T.hrule(width - 2)))
        lines.append((T.FG_DIM, "  thresholds are the built-in defaults; edit them "
                                "in the desktop app's ALARMS dialog"))
        return lines

    def _session_lines(self, width: int) -> list[tuple[str, str]]:
        lines: list[tuple[str, str]] = []
        lines.append((T.FG_BRIGHT, f" {T.BOLD}logged sessions{T.RESET}"))
        lines.append((T.FG_FAINT, " " + self.store.path))
        lines.append((T.FG_HEAD,
                      f" {'id':>4}  {'started':<20}{'duration':>12}"
                      f"{'samples':>9}{'alarms':>8}  label"))
        for session in self.store.list_sessions(limit=60):
            alarms = self.store.alarms(session.id)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(session.started_at))
            colour = T.FG_DEFAULT if alarms else T.FG_DIM
            lines.append((colour,
                          f" {session.id:>4}  {stamp:<20}"
                          f"{S.human_duration(session.duration):>12}"
                          f"{session.sample_count:>9}{len(alarms):>8}  "
                          f"{session.label or '(unlabelled)'}"))
        if not self.store.list_sessions(limit=1):
            lines.append((T.FG_DIM, "   none yet - press L to start logging"))
        return lines

    def _help_lines(self, width: int) -> list[tuple[str, str]]:
        palette = TH.current()
        rows = [
            ("", ""),
            (T.FG_BRIGHT, "  NAVIGATION"),
            (T.FG_DEFAULT, "  Left / Right      switch tab, or move between buttons"),
            (T.FG_DEFAULT, "  Up / Down         scroll one line"),
            (T.FG_DEFAULT, "  PgUp / PgDn       scroll a screenful"),
            (T.FG_DEFAULT, "  Home / End        jump to top or bottom"),
            (T.FG_DEFAULT, "  Tab / Shift-Tab   move the focus between buttons"),
            (T.FG_DEFAULT, "  Enter / Space     activate the focused button"),
            ("", ""),
            (T.FG_BRIGHT, "  ACTIONS"),
            (T.FG_DEFAULT, "  L                 start / stop logging"),
            (T.FG_DEFAULT, "  M                 drop a marker in the current run"),
            (T.FG_DEFAULT, "  S                 jump to stats"),
            (T.FG_DEFAULT, "  A                 jump to alarms"),
            (T.FG_DEFAULT, "  T                 next colour theme"),
            (T.FG_DEFAULT, "  1..5              jump straight to a tab"),
            (T.FG_DEFAULT, "  Q / Esc           quit"),
            ("", ""),
            (T.FG_BRIGHT, "  TABS"),
            (T.FG_DEFAULT, "  LIVE     current readings with bars and sparklines"),
            (T.FG_DEFAULT, "  STATS    statistics for the running or latest session"),
            (T.FG_DEFAULT, "  ALARMS   active alarms and the recent crossings"),
            (T.FG_DEFAULT, "  SESSIONS everything logged so far"),
            ("", ""),
            (T.FG_BRIGHT, "  THEME"),
            (T.FG_HEAD, f"  {palette.label} - {palette.blurb}"),
            (T.FG_DEFAULT, f"  T cycles the {len(TH.keys())} themes; the choice is "
                           f"saved to config.json and used again next time."),
            ("", ""),
            (T.FG_DIM, "  This is the terminal view. The desktop window, the browser"),
            (T.FG_DIM, "  GUI and this all read the same sensors and the same database."),
        ]
        return rows


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------


def _fmt(value: float | None, precision: int) -> str:
    if value is None or value != value:
        return "n/a"
    return f"{value:.{precision}f}"


def _right_aligned(prefix: str, suffix: str, width: int, gap: int = 2) -> str:
    """Push `suffix` flush with `width`, or leave `prefix` alone if it will not fit.

    `prefix` may carry escape sequences, so it is measured on visible width; a
    narrow terminal must not end up with the two runs written over each other.
    `suffix` is plain text - the theme labels are.
    """
    spare = width - T.visible_width(prefix) - len(suffix)
    if spare < gap:
        return prefix
    return prefix + " " * spare + suffix


def _value_colour(field: str, value: float | None) -> str:
    if value is None or value != value:
        return T.FG_DIM
    if field in ("temp", "hotspot", "mem_temp", "cpu_temp"):
        return _temp_colour(value, None)
    if field in ("vram_percent", "util", "cpu_util", "ram_percent",
                 "power_percent"):
        if value >= 95:
            return T.FG_CRIT
        if value >= 80:
            return T.FG_WARN
        if value >= 40:
            return T.FG_ACCENT
        return T.FG_OK
    return T.FG_DEFAULT


def _temp_colour(temp: float | None, limit: float | None) -> str:
    if temp is None or temp != temp:
        return T.FG_DIM
    if limit:
        if temp >= limit - 2:
            return T.FG_CRIT
        if temp >= limit - 10:
            return T.FG_WARN
    else:
        if temp >= 90:
            return T.FG_CRIT
        if temp >= 80:
            return T.FG_WARN
    return T.FG_ACCENT if temp >= 60 else T.FG_OK


def _field_text(field: str, values: dict[str, float], index: int,
                unit: str) -> str:
    """Human text for one metric row, matching the desktop window's formatting."""
    if field == "vram_percent":
        used = values.get(f"gpu{index}_vram_used")
        total = values.get(f"gpu{index}_vram_total")
        percent = values.get(f"gpu{index}_vram_percent")
        if percent is None:
            return "n/a"
        if used is not None and total:
            return f"{percent:.0f}% {used:,.0f}/{total:,.0f}MB"
        return f"{percent:.0f}%"
    if field == "power_percent":
        power = values.get(f"gpu{index}_power")
        limit = values.get(f"gpu{index}_power_limit")
        if power is None:
            return "n/a"
        text = f"{power:.0f} W"
        if limit:
            text += f" / {limit:.0f} W"
        return text
    value = values.get(f"gpu{index}_{field}")
    if value is None:
        return "n/a"
    if unit == "MHz":
        return f"{value:,.0f} MHz"
    if unit == "%":
        return f"{value:.1f} %"
    if unit == "C":
        return f"{value:.0f} C"
    return f"{value:,.0f} {unit}".strip()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def serve(manager: M.SensorManager, store: S.Store, sampler: SP.Sampler,
          screen: Screen | None = None,
          keyboard: "Keyboard | None" = None,
          config_path: str | None = None) -> int:
    """Run the TUI until the user quits.

    `screen` and `keyboard` are injectable so the loop can be driven by a test
    with an in-memory screen and a scripted keyboard: the terminal view is
    otherwise only reachable from a real interactive terminal. `config_path` is
    injectable for the same reason - a test that presses T must be able to do so
    without writing the config.json that holds the user's alarm rules.
    """
    keyboard = keyboard if keyboard is not None else Keyboard()
    app = TuiApp(manager=manager, store=store, sampler=sampler,
                 config_path=config_path)
    if screen is not None:
        app.screen = screen
    screen = app.screen
    if not sys_isatty():
        print("The terminal view needs an interactive terminal "
              "(no TTY detected).", file=__import__("sys").stderr)
        return 1
    sampler.start()
    try:
        screen.open()
        keyboard.enter()
        next_frame = time.monotonic()
        while app.running:
            # Drain every pending key so held arrows feel responsive.
            key = keyboard.read(timeout=0.02)
            while key is not None:
                app.handle(key)
                if not app.running:
                    break
                key = keyboard.read(timeout=0.0)
            now = time.monotonic()
            if now >= next_frame:
                screen.measure()
                app.render()
                next_frame = now + 0.25
    finally:
        keyboard.exit()
        screen.close()
        sampler.stop()
    return 0


def sys_isatty() -> bool:
    import sys
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False
