"""Terminal layer: raw keyboard input and ANSI drawing, on Windows and Linux.

Why hand-rolled
---------------
`curses` is not available in the standard Windows Python build (there is no
`_curses` module), and pulling in `windows-curses` would break the "no
dependencies" property the rest of the program has. Everything here uses ANSI
escape sequences, which Windows Terminal, modern conhost, and every Linux
terminal understand, plus per-platform raw key reading:

  Windows  msvcrt.getwch()   - wide characters, so arrow keys arrive as two
                               reads: a NUL/0xE0 prefix then a scan code
  POSIX    termios + tty     - switch the tty to non-canonical, no-echo mode
                               and read one byte at a time

The public surface is deliberately small: a Screen that knows its size, moves
the cursor, prints styled runs, and yields KeyEvents.
"""
from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterator, Mapping, Sequence

IS_WINDOWS = os.name == "nt"

# --------------------------------------------------------------------------
# Escape sequences
# --------------------------------------------------------------------------

ESC = "\x1b"
CSI = f"{ESC}["

RESET = f"{CSI}0m"
BOLD = f"{CSI}1m"
DIM = f"{CSI}2m"
UNDERLINE = f"{CSI}4m"
REVERSE = f"{CSI}7m"

CLEAR = f"{CSI}2J"
HOME = f"{CSI}H"
HIDE_CURSOR = f"{CSI}?25l"
SHOW_CURSOR = f"{CSI}?25h"
ALT_SCREEN_ON = f"{CSI}?1049h"
ALT_SCREEN_OFF = f"{CSI}?1049l"

# Colour constants. They start out as the 256-colour codes that match the
# default (nvtop) palette, and `apply_palette` rebinds them from whichever theme
# is active. Module globals rather than a dict on purpose: `tui.py` names them
# (`T.FG_ACCENT`, `T.BG_BAR`) while drawing, so rebinding the names here is what
# switching a theme amounts to - nothing has to thread a palette through the
# renderer, and a colour is never captured at import time.
FG_DEFAULT = f"{CSI}38;5;252m"
FG_DIM = f"{CSI}38;5;245m"
FG_FAINT = f"{CSI}38;5;240m"
FG_ACCENT = f"{CSI}38;5;39m"
FG_OK = f"{CSI}38;5;78m"
FG_WARN = f"{CSI}38;5;214m"
FG_CRIT = f"{CSI}38;5;203m"
FG_BRIGHT = f"{CSI}38;5;231m"
FG_HEAD = f"{CSI}38;5;146m"

BG_BAR = f"{CSI}48;5;236m"
BG_ALT = f"{CSI}48;5;235m"
BG_SELECT = f"{CSI}48;5;24m"
#: The page colour, painted behind a whole frame. Empty until a palette is
#: applied: filling the screen is opt-in, so an unthemed caller is unchanged.
BG_BG = ""


# --------------------------------------------------------------------------
# Palette -> escape sequences
# --------------------------------------------------------------------------

#: The xterm-256 cube levels, and the 16 system colours in their standard order.
_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)
_SYSTEM_RGB = (
    (0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0),
    (0, 0, 128), (128, 0, 128), (0, 128, 128), (192, 192, 192),
    (128, 128, 128), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
)


def _build_xterm256() -> tuple[tuple[int, int, int], ...]:
    """The RGB of every colour `38;5;N` can select.

    The fallback path measures a requested colour against all 256 entries, so
    the table is built once here rather than per lookup: a frame maps the same
    dozen palette colours over and over.
    """
    table = list(_SYSTEM_RGB)
    for red in _CUBE_LEVELS:
        for green in _CUBE_LEVELS:
            for blue in _CUBE_LEVELS:
                table.append((red, green, blue))
    for step in range(24):
        level = 8 + step * 10                  # 232..255: the grey ramp
        table.append((level, level, level))
    return tuple(table)


XTERM256_RGB = _build_xterm256()


def parse_colour(value: str) -> tuple[int, int, int] | None:
    """`#rgb` or `#rrggbb` (the `#` optional) as RGB; None if it is neither."""
    if not isinstance(value, str):
        return None
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        return None
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return None


def colour_distance(first: tuple[int, int, int],
                    second: tuple[int, int, int]) -> float:
    """Euclidean RGB distance - the error the 256-colour fallback leaves."""
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))


@lru_cache(maxsize=None)
def nearest_index(rgb: tuple[int, int, int]) -> int:
    """The xterm-256 index closest to `rgb`, by smallest squared RGB distance.

    Squared distance is enough to rank candidates and avoids a square root per
    entry; the value is cached because the fallback is a 256-entry scan.
    """
    best_index = 0
    best_distance: int | None = None
    for index, candidate in enumerate(XTERM256_RGB):
        distance = sum((a - b) ** 2 for a, b in zip(rgb, candidate))
        if best_distance is None or distance < best_distance:
            best_index, best_distance = index, distance
    return best_index


def nearest_rgb(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """The RGB `nearest_index` settles on, for callers that need the error."""
    return XTERM256_RGB[nearest_index(rgb)]


def supports_truecolor(env: Mapping[str, str] | None = None) -> bool:
    """Whether the terminal understands 24-bit SGR colour.

    Two hints are honoured, both of them conventions terminals set for
    themselves: COLORTERM says outright that it does 24-bit (`truecolor`, or
    `24bit` from the older spelling), and terminfo's `direct` colour mode
    reaches the program as TERM matching `...-direct`. Nothing else is guessed
    at - an unset environment means the 256-colour path, which draws on every
    terminal this program supports.

    `env` is the mapping to read; os.environ by default. It is a parameter so a
    test can exercise both paths without editing the process environment.
    """
    environ = os.environ if env is None else env
    colorterm = str(environ.get("COLORTERM", "")).lower()
    if "truecolor" in colorterm or "24bit" in colorterm:
        return True
    return "direct" in str(environ.get("TERM", "")).lower()


@lru_cache(maxsize=None)
def _sgr(colour: str, background: bool, truecolor: bool) -> str:
    """The escape sequence for one colour, or "" when it cannot be parsed."""
    rgb = parse_colour(colour)
    if rgb is None:
        return ""
    plane = 48 if background else 38
    if truecolor:
        return f"{CSI}{plane};2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    return f"{CSI}{plane};5;{nearest_index(rgb)}m"


def fg(colour: str, truecolor: bool | None = None) -> str:
    """Foreground escape for `colour`, "" if it is not a hex colour."""
    mode = supports_truecolor() if truecolor is None else truecolor
    return _sgr(colour, False, bool(mode))


def bg(colour: str, truecolor: bool | None = None) -> str:
    """Background escape for `colour`, "" if it is not a hex colour."""
    mode = supports_truecolor() if truecolor is None else truecolor
    return _sgr(colour, True, bool(mode))


def mix(first: str, second: str, ratio: float) -> str:
    """Blend two hex colours; `ratio` is how much of `second` to take.

    Used for the one colour role no palette carries: see `apply_palette`.
    """
    a, b = parse_colour(first), parse_colour(second)
    if a is None:
        return second if b is not None else first
    if b is None:
        return first
    parts = (min(255, max(0, round(x + (y - x) * ratio))) for x, y in zip(a, b))
    return "#" + "".join(f"{part:02x}" for part in parts)


def apply_palette(palette, env: Mapping[str, str] | None = None) -> str:
    """Rebind this module's colour constants from `palette`.

    A palette is anything with the attribute names of `ui.themes.Palette`;
    nothing is imported from `ui` here, so the terminal layer stays usable on a
    machine with no display and no tkinter. Returns the mode chosen,
    "truecolor" or "256", which callers may show or log.

    Each role takes the palette field that means the same thing, and keeps its
    previous value when the palette is missing that field, so a partial palette
    still draws something readable rather than losing the screen.

    `BG_BG` is the page colour: `Screen.begin` can paint a whole frame with it,
    which is what makes the lighter themes (Nord, Gruvbox) look like themselves
    instead of like the user's default terminal background.
    """
    global FG_DEFAULT, FG_DIM, FG_FAINT, FG_ACCENT, FG_OK, FG_WARN, FG_CRIT
    global FG_BRIGHT, FG_HEAD, BG_BAR, BG_ALT, BG_SELECT, BG_BG

    truecolor = supports_truecolor(env)

    def field(*names: str) -> str:
        """The first of `names` this palette actually defines."""
        for name in names:
            value = getattr(palette, name, None)
            if isinstance(value, str) and value:
                return value
        return ""

    def paint(*names: str, background: bool = False, keep: str = "") -> str:
        colour = field(*names)
        sequence = bg(colour, truecolor) if background else fg(colour, truecolor)
        return sequence or keep

    FG_DEFAULT = paint("text", keep=FG_DEFAULT)
    FG_DIM = paint("text_dim", keep=FG_DIM)
    # `idle` before `border`: both are quiet, but this name draws rules, notes
    # and the scroll gutter, and a theme's border colour alone is dark enough to
    # vanish against its own background.
    FG_FAINT = paint("idle", "border", keep=FG_FAINT)
    FG_ACCENT = paint("accent", keep=FG_ACCENT)
    FG_OK = paint("ok", keep=FG_OK)
    FG_WARN = paint("warn", keep=FG_WARN)
    FG_CRIT = paint("crit", keep=FG_CRIT)
    FG_BRIGHT = paint("text_bright", keep=FG_BRIGHT)

    # No palette carries a heading colour, so one is derived: the accent pulled
    # halfway towards the brightest text. Headings then belong to the theme
    # without competing with the accent for attention.
    accent, bright = field("accent"), field("text_bright")
    if accent and bright:
        heading = mix(accent, bright, 0.5)
    else:
        heading = accent or bright
    FG_HEAD = fg(heading, truecolor) or FG_HEAD

    BG_BAR = paint("panel", "panel_alt", background=True, keep=BG_BAR)
    BG_ALT = paint("panel_alt", "panel", background=True, keep=BG_ALT)
    BG_SELECT = paint("selection", background=True, keep=BG_SELECT)
    BG_BG = paint("bg", background=True, keep=BG_BG)
    return "truecolor" if truecolor else "256"


# Braille, for the same block-drawing nvtop uses.
BRAILLE_BASE = 0x2800
_BRAILLE_DOTS = {(0, 0): 1, (0, 1): 2, (0, 2): 4, (0, 3): 64,
                 (1, 0): 8, (1, 1): 16, (1, 2): 32, (1, 3): 128}


def strip_ansi(text: str) -> str:
    """Visible width helper: drop escape sequences before measuring."""
    out = []
    i = 0
    while i < len(text):
        if text[i] == ESC:
            j = i + 1
            while j < len(text) and not (text[j].isalpha() or text[j] == "m"):
                j += 1
            i = j + 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def visible_width(text: str) -> int:
    return len(strip_ansi(text))


def braille_bar(fraction: float, cells: int = 12) -> str:
    """A horizontal bar as Braille cells: 2 dot-columns per character cell."""
    if fraction != fraction:                      # NaN
        return " " * cells
    fraction = 0.0 if fraction < 0 else (1.0 if fraction > 1 else fraction)
    filled = int(round(fraction * cells * 2))
    chars = []
    for cell in range(cells):
        code = BRAILLE_BASE
        left = cell * 2
        right = left + 1
        if left < filled:
            code |= _BRAILLE_DOTS[(0, 0)] | _BRAILLE_DOTS[(0, 1)] \
                | _BRAILLE_DOTS[(0, 2)] | _BRAILLE_DOTS[(0, 3)]
        if right < filled:
            code |= _BRAILLE_DOTS[(1, 0)] | _BRAILLE_DOTS[(1, 1)] \
                | _BRAILLE_DOTS[(1, 2)] | _BRAILLE_DOTS[(1, 3)]
        chars.append(chr(code))
    return "".join(chars)


def sparkline(values: Sequence[float], cells: int, vmin: float = 0.0,
              vmax: float = 100.0) -> str:
    """Block-character sparkline, one cell per sample (newest last)."""
    blocks = " ▁▂▃▄▅▆▇█"
    if not values or cells <= 0:
        return " " * cells
    step = max(1, len(values) // cells)
    sampled = list(values)[::step][-cells:]
    span = (vmax - vmin) or 1.0
    out = []
    for value in sampled:
        if value != value:
            out.append(" ")
            continue
        frac = (value - vmin) / span
        frac = 0.0 if frac < 0 else (1.0 if frac > 1 else frac)
        out.append(blocks[int(round(frac * (len(blocks) - 1)))])
    return " " * (cells - len(out)) + "".join(out)


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Key:
    """One decoded key press. `name` is canonical, lower case."""
    name: str
    char: str = ""
    shift: bool = False


#: Windows scan codes -> canonical names. Arrow keys arrive after a 0x00/0xE0
#: prefix; the same scan codes are reused by Page/Home/End.
_WIN_SCAN_CODES = {
    "H": "up", "P": "down", "K": "left", "M": "right",
    "I": "pageup", "Q": "pagedown", "G": "home", "O": "end",
    "R": "insert", "S": "delete", "k": "end", "q": "pagedown",
}


class Keyboard:
    """Raw, non-blocking key reading for the current platform."""

    def __init__(self) -> None:
        self._saved_termios = None
        self._fd: int | None = None

    def __enter__(self) -> "Keyboard":
        self.enter()
        return self

    def __exit__(self, *_exc) -> None:
        self.exit()

    def enter(self) -> None:
        if IS_WINDOWS:
            return
        try:
            import termios
            import tty
        except ImportError:
            return
        self._fd = sys.stdin.fileno()
        try:
            self._saved_termios = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd, termios.TCSANOW)
        except (OSError, ValueError):
            self._saved_termios = None

    def exit(self) -> None:
        if IS_WINDOWS or self._saved_termios is None or self._fd is None:
            return
        try:
            import termios
            termios.tcsetattr(self._fd, termios.TCSANOW, self._saved_termios)
        except (ImportError, OSError, ValueError):
            pass
        self._saved_termios = None

    def read(self, timeout: float = 0.1) -> Key | None:
        """One key press, or None if nothing arrived within `timeout`."""
        if IS_WINDOWS:
            return self._read_windows(timeout)
        return self._read_posix(timeout)

    # -- Windows ---------------------------------------------------------
    @staticmethod
    def _read_windows(timeout: float) -> Key | None:
        import msvcrt
        import time
        deadline = time.monotonic() + timeout
        while True:
            if msvcrt.kbhit():
                char = msvcrt.getwch()
                if char in ("\x00", "\xe0"):
                    if not msvcrt.kbhit():
                        time.sleep(0.001)
                    code = msvcrt.getwch()
                    name = _WIN_SCAN_CODES.get(code, f"scan_{ord(code):02x}")
                    return Key(name=name)
                return _decode_char(char)
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)

    # -- POSIX -----------------------------------------------------------
    def _read_posix(self, timeout: float) -> Key | None:
        import select
        if self._fd is None:
            return None
        try:
            ready, _w, _x = select.select([self._fd], [], [], timeout)
        except (OSError, ValueError):
            return None
        if not ready:
            return None
        try:
            data = os.read(self._fd, 8)
        except OSError:
            return None
        if not data:
            return None
        text = data.decode("utf-8", "replace")
        if text.startswith(ESC):
            # CSI sequences: ESC [ A .. and the xterm variants with modifiers.
            body = text[1:]
            if body.startswith("["):
                final = body[1:2]
                mapping = {"A": "up", "B": "down", "C": "right", "D": "left",
                           "H": "home", "F": "end", "Z": "backtab"}
                if final in mapping:
                    name = mapping[final]
                    shift = ";2" in body
                    return Key(name=name, shift=shift)
                if body[1:2] == "5":
                    return Key(name="pageup")
                if body[1:2] == "6":
                    return Key(name="pagedown")
                if body[1:2] == "1" and body.endswith("~"):
                    return Key(name="home")
                if body[1:2] == "4" and body.endswith("~"):
                    return Key(name="end")
            if body in ("O", "OP"):
                return None
            return Key(name="escape")
        return _decode_char(text[0] if text else "")


def _decode_char(char: str) -> Key | None:
    if not char:
        return None
    if char == "\r":
        return Key(name="enter")
    if char in ("\n",):
        return Key(name="enter")
    if char == "\t":
        return Key(name="tab")
    if char in ("\x7f", "\b"):
        return Key(name="backspace")
    if char == "\x03":
        return Key(name="ctrl-c")
    if char == "\x1b":
        return Key(name="escape")
    return Key(name=char.lower(), char=char, shift=char.isupper())


# --------------------------------------------------------------------------
# Screen
# --------------------------------------------------------------------------


class Screen:
    """Double-buffered ANSI screen with a known size."""

    def __init__(self, stream=None) -> None:
        self.stream = stream or sys.stdout
        self.width = 80
        self.height = 24
        self._buffer: list[str] = []
        self._raw_written = False

    # -- lifecycle -------------------------------------------------------
    def open(self) -> None:
        self._write(ALT_SCREEN_ON + HIDE_CURSOR + CLEAR)
        self.measure()

    def close(self) -> None:
        self._write(RESET + SHOW_CURSOR + ALT_SCREEN_OFF)

    def _write(self, text: str) -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
            self._raw_written = True
        except (OSError, ValueError):
            pass

    def measure(self) -> tuple[int, int]:
        """Terminal size, preferring the ioctl over the environment."""
        size = None
        try:
            size = os.get_terminal_size()
        except (OSError, ValueError, AttributeError):
            cols = os.environ.get("COLUMNS")
            rows = os.environ.get("LINES")
            if cols and rows:
                try:
                    size = os.terminal_size((int(cols), int(rows)))
                except ValueError:
                    size = None
        if size is not None:
            self.width = max(40, size.columns)
            self.height = max(12, size.lines)
        self._last_size = (self.width, self.height)
        return self.width, self.height

    @property
    def resized(self) -> bool:
        return getattr(self, "_last_size", None) != (self.width, self.height)

    # -- drawing ---------------------------------------------------------
    def begin(self, bg_colour: str = "") -> None:
        """Start a frame: reposition and clear.

        `bg_colour` is emitted before the erase so that terminals implementing
        background-colour-erase fill the cleared cells with it - that is how a
        theme's page colour reaches the parts of the screen nothing is drawn
        on. Terminals without it simply erase to their own background.
        """
        self._buffer = [bg_colour + CLEAR] if bg_colour else [CLEAR]

    def flush(self) -> None:
        self._write("".join(self._buffer))

    def at(self, row: int, col: int) -> None:
        """Move the cursor (1-based, as ANSI expects)."""
        self._buffer.append(f"{CSI}{max(1, row)};{max(1, col)}H")

    def text(self, value: str, colour: str = "", bg: str = "") -> None:
        prefix = (colour or "") + (bg or "")
        if prefix:
            self._buffer.append(prefix)
        self._buffer.append(value)
        if prefix:
            self._buffer.append(RESET)

    def line(self, row: int, col: int, value: str, colour: str = "",
             bg: str = "", width: int | None = None) -> None:
        """Write one line, clipped/padded to `width` visible columns."""
        if width is not None:
            visible = visible_width(value)
            if visible > width:
                # Clip on visible width, keeping the escape prefix intact.
                value = _clip_ansi(value, width)
            elif visible < width:
                value += " " * (width - visible)
        self.at(row, col)
        self.text(value, colour, bg)


def _clip_ansi(value: str, width: int) -> str:
    """Trim a styled string to `width` visible characters."""
    out: list[str] = []
    visible = 0
    i = 0
    while i < len(value) and visible < width:
        if value[i] == ESC:
            j = i + 1
            while j < len(value) and not (value[j].isalpha() or value[j] == "m"):
                j += 1
            out.append(value[i:j + 1])
            i = j + 1
            continue
        out.append(value[i])
        visible += 1
        i += 1
    out.append(RESET)
    return "".join(out)


def hrule(width: int, char: str = "\u2500") -> str:
    return char * max(0, width)
