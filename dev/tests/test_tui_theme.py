"""Verify that the terminal view follows the application's colour theme.

Four claims are checked here, none of which needs an interactive terminal or a
colour-capable one:

  * `termlib` turns a palette into escape sequences: 24-bit when the terminal
    says it understands them, and otherwise the nearest colour of the xterm-256
    cube, whose error is measured against a stated tolerance rather than
    eyeballed
  * the frames `tui.py` draws really carry those sequences, which is checked in
    the raw escape stream and again in the grid reconstructed by
    `ansi_screen.VirtualScreen`
  * colours are read while a frame is drawn rather than captured at import, so
    a theme switch needs no rebuild
  * pressing T switches the theme, saves it to config.json, and leaves the rest
    of that file - in particular the alarm rules - exactly as it was

Safety: the TUI's config path is injectable, so every write in this script goes
to a scratch file. The real config.json is hashed before and after and compared,
because a test must never be the reason someone loses their alarm rules.
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





import io
import json
import math
import os
import re
import threading
import time

import alarms as A
import ansi_screen as ANSI
import metrics as M
import sampler as SP
import store as S
import termlib as T
import tui
from termlib import Key
from ui import themes

DB = "diagthemetui.db"
TEMP_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "diagtheme_config.json")

#: The cube's levels are 0, 95, 135, 175, 215, 255, so its widest gap is the
#: 0..95 one and no channel can end up more than 47.5 units out. 47.5*sqrt(3) is
#: therefore the worst error the cube can show for a colour it cannot reach at
#: all, and every palette field of every theme is measured against it below.
CUBE_WORST_CASE = 47.5 * math.sqrt(3)

#: The shipped palettes sit well inside that: the worst of them is about 51 RGB
#: units (Matrix's red #ff3b3b, which the cube can only reach as #ff5f5f). A
#: palette colour landing further away than this is a regression, so the bound
#: is asserted as well as printed.
FALLBACK_TOLERANCE = 56.0

#: Palette field -> the `termlib` constant that has to carry it, foreground.
ROLE_CONSTANTS = {
    "text": "FG_DEFAULT",
    "text_dim": "FG_DIM",
    "idle": "FG_FAINT",
    "accent": "FG_ACCENT",
    "ok": "FG_OK",
    "warn": "FG_WARN",
    "crit": "FG_CRIT",
    "text_bright": "FG_BRIGHT",
}

#: Palette field -> the `termlib` constant that has to carry it, background.
BG_CONSTANTS = {
    "panel": "BG_BAR",
    "panel_alt": "BG_ALT",
    "selection": "BG_SELECT",
    "bg": "BG_BG",
}

problems: list[str] = []


def ascii_safe(text: str) -> str:
    """Console output here is cp1252, which cannot encode Braille or blocks."""
    return text.encode("ascii", "replace").decode("ascii")


def check(label: str, ok: bool, detail: str = "") -> None:
    print(ascii_safe(f"  {'OK  ' if ok else 'FAIL'} {label}"
                     f"{'  ' + detail if detail else ''}"))
    if not ok:
        problems.append(label)


def snapshot(path: str) -> bytes | None:
    """The bytes of `path`, or None when it is not there."""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def real_config_detail() -> str:
    """How the real config.json stands now, so a mismatch is diagnosable.

    A failure here means something wrote the user's file during this run - it
    was not this script, whose every write went to the scratch path - and the
    first thing worth knowing is whether it is still there and how big it is.
    """
    after = snapshot(REAL_CONFIG)
    if real_config_before is None:
        return ("absent before and after" if after is None
                else f"appeared during the run: {len(after)} bytes")
    if after is None:
        return f"disappeared during the run: was {len(real_config_before)} bytes"
    if after == real_config_before:
        return f"{len(after)} bytes unchanged"
    return (f"changed during the run: {len(real_config_before)} -> "
            f"{len(after)} bytes")


def expect_constant(problems: list[str], theme: str, name: str,
                    expected: str) -> None:
    """Record `name` unless it carries exactly `expected`."""
    actual = getattr(T, name)
    if actual != expected:
        problems.append(f"{theme}: {name} is {actual!r}, want {expected!r}")


class environment:
    """Temporarily set environment variables, for the 24-bit colour hints.

    The app reads os.environ itself when it applies a palette, so the capability
    has to be present in the process for this to exercise the real path instead
    of a stub. Everything is restored on the way out.
    """

    def __init__(self, **values: str) -> None:
        self.values = values
        self.saved: dict[str, str | None] = {}

    def __enter__(self) -> "environment":
        for name, value in self.values.items():
            self.saved[name] = os.environ.get(name)
            os.environ[name] = value
        return self

    def __exit__(self, *_exc) -> None:
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def sgr_rgb(sequence: str) -> tuple[int, int, int] | None:
    """The colour a foreground SGR selects, whichever of the two forms it is."""
    match = re.fullmatch(r"\x1b\[38;2;(\d+);(\d+);(\d+)m", sequence)
    if match:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    match = re.fullmatch(r"\x1b\[38;5;(\d+)m", sequence)
    if match:
        return T.XTERM256_RGB[int(match.group(1))]
    return None


def brute_force_nearest(rgb: tuple[int, int, int]) -> int:
    """Recompute the argmin here, so the cached lookup is checked, not trusted.

    `min` and `termlib` both keep the lowest index on a tie, which is what makes
    the two comparable directly.
    """
    return min(range(len(T.XTERM256_RGB)),
               key=lambda i: sum((a - b) ** 2
                                 for a, b in zip(rgb, T.XTERM256_RGB[i])))


print("=" * 88)
print("TERMINAL THEME TEST")
print("=" * 88)

REAL_CONFIG = A.config_path()
real_config_before = snapshot(REAL_CONFIG)

for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.remove(DB + suffix)

# A config that looks like a real one: the theme write has to survive every
# other key in the file, not just the ones it knows about.
ORIGINAL_CONFIG = {
    "_comment": "placeholder: the theme write must not disturb anything else",
    "sample_hz": 2.0,
    "per_core": True,
    "db_path": DB,
    "theme": "nvtop",
    "alarms": {
        "gpu0_temp": {"warning": 78.0, "critical": 85.0, "hysteresis": 2.0,
                      "min_duration": 1.0, "enabled": True},
        "cpu_temp": {"warning": 85.0, "critical": 95.0, "hysteresis": 2.0,
                     "min_duration": 2.0, "enabled": True},
    },
}

print("\n[1] 24-bit capability detection")
check("COLORTERM=truecolor is recognised",
      T.supports_truecolor({"COLORTERM": "truecolor"}))
check("COLORTERM=24bit is recognised",
      T.supports_truecolor({"COLORTERM": "24bit"}))
check("the hint is matched case-insensitively",
      T.supports_truecolor({"COLORTERM": "TrueColor"}))
check("TERM=...-direct is recognised",
      T.supports_truecolor({"TERM": "xterm-direct"}))
check("a 256-colour TERM does not claim 24-bit",
      not T.supports_truecolor({"TERM": "xterm-256color"}))
check("an empty environment falls back to 256", not T.supports_truecolor({}))
check("COLORTERM decides even when TERM is plain",
      T.supports_truecolor({"COLORTERM": "truecolor", "TERM": "xterm-256color"}))

print("\n[2] the xterm-256 fallback")
check("the table holds all 256 colours", len(T.XTERM256_RGB) == 256,
      str(len(T.XTERM256_RGB)))
# A table that is subtly wrong still produces plausible colours, so the entries
# that anchor it - cube corners and both ends of the grey ramp - are checked.
check("index 16 is black", T.XTERM256_RGB[16] == (0, 0, 0),
      str(T.XTERM256_RGB[16]))
check("index 231 is white", T.XTERM256_RGB[231] == (255, 255, 255),
      str(T.XTERM256_RGB[231]))
check("index 196 is pure red", T.XTERM256_RGB[196] == (255, 0, 0),
      str(T.XTERM256_RGB[196]))
check("index 21 is pure blue", T.XTERM256_RGB[21] == (0, 0, 255),
      str(T.XTERM256_RGB[21]))
check("the grey ramp starts at 8", T.XTERM256_RGB[232] == (8, 8, 8),
      str(T.XTERM256_RGB[232]))
check("the grey ramp ends at 238", T.XTERM256_RGB[255] == (238, 238, 238),
      str(T.XTERM256_RGB[255]))

check("hex colours parse", T.parse_colour("#3ea6ff") == (62, 166, 255),
      str(T.parse_colour("#3ea6ff")))
check("the # is optional and #rgb is expanded",
      T.parse_colour("0af") == (0, 170, 255), str(T.parse_colour("0af")))
check("a colour name is refused", T.parse_colour("blue") is None,
      repr(T.parse_colour("blue")))

# White and black are reachable exactly - by index 15/231 and index 0/16
# respectively - and the search keeps the lowest of the two, so what is asserted
# is that the error is zero rather than which of the pair wins.
check("white is matched exactly",
      T.colour_distance((255, 255, 255), T.nearest_rgb((255, 255, 255))) == 0.0,
      f"index {T.nearest_index((255, 255, 255))}")
check("black is matched exactly",
      T.colour_distance((0, 0, 0), T.nearest_rgb((0, 0, 0))) == 0.0,
      f"index {T.nearest_index((0, 0, 0))}")
check("a grey between two ramp steps takes the nearer one",
      T.nearest_index((46, 46, 46)) == 236,
      f"index {T.nearest_index((46, 46, 46))}")

T.nearest_index.cache_clear()
T.nearest_index((46, 46, 46))
T.nearest_index((46, 46, 46))
check("repeat lookups are served from the cache",
      T.nearest_index.cache_info().hits >= 1, str(T.nearest_index.cache_info()))

worst_error, worst_where = 0.0, ""
fallback_problems: list[str] = []
with environment(COLORTERM="", TERM="xterm-256color"):
    for key, _label, _blurb in themes.catalog():
        palette = themes.get(key)
        mode = T.apply_palette(palette)
        if mode != "256":
            fallback_problems.append(f"{key}: mode was {mode!r}")
            continue
        for role, constant in ROLE_CONSTANTS.items():
            sequence = getattr(T, constant)
            if not sequence.startswith("\x1b[38;5;"):
                fallback_problems.append(f"{key}/{role}: {sequence!r} is not 38;5")
                continue
            index = int(sequence[len("\x1b[38;5;"):-1])
            wanted = T.parse_colour(getattr(palette, role))
            if index != brute_force_nearest(wanted):
                fallback_problems.append(
                    f"{key}/{role}: index {index} is not the nearest")
            error = T.colour_distance(wanted, T.XTERM256_RGB[index])
            if error > worst_error:
                worst_error, worst_where = error, f"{key}/{role}"
            if error > CUBE_WORST_CASE:
                fallback_problems.append(
                    f"{key}/{role}: off by {error:.1f}, past the cube's "
                    f"{CUBE_WORST_CASE:.1f} worst case")

check("every theme colour falls back to the nearest of the 256",
      not fallback_problems,
      f"worst was {worst_where} off by {worst_error:.1f}, "
      f"cube worst case {CUBE_WORST_CASE:.1f}")
for detail in fallback_problems[:6]:
    print(f"       {detail}")

check(f"the shipped palettes stay within {FALLBACK_TOLERANCE:.0f} RGB units "
      f"of what they ask for",
      worst_error <= FALLBACK_TOLERANCE,
      f"worst was {worst_where} off by {worst_error:.1f}")

check("backgrounds fall back to 48;5 as well",
      all(getattr(T, name).startswith("\x1b[48;5;")
          for name in ("BG_BAR", "BG_ALT", "BG_SELECT", "BG_BG")),
      f"{T.BG_BAR!r} {T.BG_SELECT!r} {T.BG_BG!r}")

print("\n[3] every palette field lands on its constant")
mapping_problems: list[str] = []
with environment(COLORTERM="truecolor", TERM="xterm-256color"):
    check("a truecolor terminal is detected", T.supports_truecolor(),
          os.environ.get("COLORTERM", ""))
    for key, _label, _blurb in themes.catalog():
        palette = themes.get(key)
        mode = T.apply_palette(palette)
        if mode != "truecolor":
            mapping_problems.append(f"{key}: mode was {mode!r}")
        for role, constant in ROLE_CONSTANTS.items():
            expect_constant(mapping_problems, key, constant,
                            T.fg(getattr(palette, role), True))
        for role, constant in BG_CONSTANTS.items():
            expect_constant(mapping_problems, key, constant,
                            T.bg(getattr(palette, role), True))
        # The heading has no field of its own; it is derived, so the derivation
        # is what gets checked.
        heading = T.mix(palette.accent, palette.text_bright, 0.5)
        expect_constant(mapping_problems, key, "FG_HEAD", T.fg(heading, True))
check(f"all {len(themes.keys())} themes map every colour role",
      not mapping_problems, f"{len(themes.keys())} themes checked")
for detail in mapping_problems[:6]:
    print(f"       {detail}")

check("bg is emitted as a background, not a foreground",
      T.BG_BG.startswith("\x1b[48;2;") and not T.BG_BG.startswith("\x1b[38;"))


class PartialPalette:
    """A palette that defines some fields and not others."""

    text = "#123456"


with environment(COLORTERM="truecolor", TERM="xterm-256color"):
    before_crit, before_bar = T.FG_CRIT, T.BG_BAR
    T.apply_palette(PartialPalette())
    check("a defined field is used", T.FG_DEFAULT == T.fg("#123456", True),
          repr(T.FG_DEFAULT))
    check("fields the palette does not define keep the previous colour",
          T.FG_CRIT == before_crit and T.BG_BAR == before_bar,
          f"{T.FG_CRIT!r} {T.BG_BAR!r}")

print("\n[4] build the app against real sensors")
manager = M.SensorManager(per_core=True)
store = S.Store(DB)
sampler = SP.Sampler(manager, store, sample_hz=2.0, per_core=True)
BASE_SAMPLE = dict(manager.poll())
# A hot synthetic sample, so the warn and crit rows are drawn on any machine:
# `_value_colour` puts 85% at warn and 99% at crit whatever the hardware turns
# out to be, and this frame must not depend on the hardware.
HOT_SAMPLE = dict(BASE_SAMPLE, cpu_util=85.0, ram_percent=99.0, cpu_temp=45.0)


def render_frame(tab: str = "live", width: int = 118, height: int = 60,
                 values: dict[str, float] | None = None) -> tuple[str, str]:
    """Draw one frame, returning the raw escapes and the emulated grid.

    A fresh app per call on purpose: what is being tested is what a frame looks
    like under whichever theme is active at that moment, so each frame has to be
    built after its theme was applied rather than reused.
    """
    buffer = io.StringIO()
    screen = T.Screen(stream=buffer)
    screen.width, screen.height = width, height
    app = tui.TuiApp(manager=manager, store=store, sampler=sampler, screen=screen,
                     config_path=TEMP_CONFIG)
    app._on_sample(time.time(), HOT_SAMPLE if values is None else values)
    app.set_tab(tab)
    app.render()
    raw = buffer.getvalue()
    ansi = ANSI.VirtualScreen(width, height)
    ansi.feed(raw)
    return raw, ansi.text()


with environment(COLORTERM="truecolor", TERM="xterm-256color"):
    print("\n[5] a drawn frame carries the active palette")
    frame_problems: list[str] = []
    for key, _label, _blurb in themes.catalog():
        themes.set_theme(key)
        palette = themes.current()
        live_raw, live_text = render_frame("live")
        # Every one of these appears in a live frame by construction: the body
        # text and the status row, the dim labels, the bright values, the accent
        # sparkline, the warn/crit bars fed by the hot sample, and the page
        # colour the frame is cleared through.
        wanted = {"FG_DEFAULT": T.fg(palette.text, True),
                  "FG_DIM": T.fg(palette.text_dim, True),
                  "FG_BRIGHT": T.fg(palette.text_bright, True),
                  "FG_ACCENT": T.fg(palette.accent, True),
                  "FG_WARN": T.fg(palette.warn, True),
                  "FG_CRIT": T.fg(palette.crit, True),
                  "BG_BG": T.bg(palette.bg, True)}
        for name, sequence in wanted.items():
            if sequence not in live_raw:
                frame_problems.append(f"{key}: {name} {sequence!r} not in the frame")
        # Read one sequence back out of the frame: the claim is that the bytes
        # on the wire carry the palette's RGB, not merely that an escape arrived.
        if sgr_rgb(T.fg(palette.accent, True)) != T.parse_colour(palette.accent):
            frame_problems.append(f"{key}: the accent sequence does not decode "
                                  f"back to {palette.accent}")
        if "gpumon" not in live_text or "LIVE" not in live_text:
            frame_problems.append(f"{key}: the frame no longer reads as a frame")
        if f"theme: {palette.label}" not in live_text:
            frame_problems.append(f"{key}: the status row does not name the theme")

        # The roles the live tab does not draw are checked on the tabs that do:
        # with no alarm open the alarms tab writes "none" in the ok colour, and
        # both it and the sessions tab draw rules in the faint colour.
        alarms_raw, _ = render_frame("alarms")
        if T.fg(palette.ok, True) not in alarms_raw:
            frame_problems.append(f"{key}: FG_OK missing from the alarms tab")
        if T.fg(palette.idle, True) not in alarms_raw:
            frame_problems.append(f"{key}: FG_FAINT missing from the alarms tab")
        sessions_raw, _ = render_frame("sessions")
        heading = T.mix(palette.accent, palette.text_bright, 0.5)
        if T.fg(heading, True) not in sessions_raw:
            frame_problems.append(f"{key}: the heading colour is missing from sessions")

    check(f"all {len(themes.keys())} themes are drawn in their own colours",
          not frame_problems, f"{len(themes.keys())} themes checked")
    for detail in frame_problems[:6]:
        print(f"       {detail}")

    themes.set_theme("mari")
    palette = themes.current()
    raw, text = render_frame("live", width=40, height=20)
    check("a 40-column frame keeps its colours",
          T.fg(palette.accent, True) in raw)
    check("a 40-column frame does not lose the status row",
          "IDLE" in text, ascii_safe(text.splitlines()[1] if text else ""))

    print("\n[6] the help tab names the theme in force")
    themes.set_theme("gruvbox")
    palette = themes.current()
    raw, text = render_frame("help", height=60)
    check("help lists the T key", "next colour theme" in text)
    check("help names the theme", palette.label in text, palette.label)
    check("help describes it", palette.blurb in text)
    check("help draws the heading colour",
          T.fg(T.mix(palette.accent, palette.text_bright, 0.5), True) in raw)

    print("\n[7] colours are read per frame, not captured at import")
    themes.set_theme("nvtop")
    nvtop_raw, _ = render_frame("live")
    themes.set_theme("matrix")
    matrix_raw, matrix_text = render_frame("live")
    check("two themes produce different escape streams", nvtop_raw != matrix_raw)
    check("the first frame carries the first theme's accent",
          T.fg(themes.get("nvtop").accent, True) in nvtop_raw)
    check("the first theme's accent is absent from the second frame",
          T.fg(themes.get("nvtop").accent, True) not in matrix_raw)
    check("the second frame carries the second theme's accent",
          T.fg(themes.get("matrix").accent, True) in matrix_raw)
    check("the second frame still renders as a frame",
          "gpumon" in matrix_text and "LIVE" in matrix_text)
    check("the status row followed the switch",
          f"theme: {themes.get('matrix').label}" in matrix_text)

print("\n[8] T cycles the theme and saves it to config.json")
with open(TEMP_CONFIG, "w", encoding="utf-8") as fh:
    json.dump(ORIGINAL_CONFIG, fh, indent=2, sort_keys=True)

themes.set_theme("nvtop")
app = tui.TuiApp(manager=manager, store=store, sampler=sampler,
                 screen=T.Screen(stream=io.StringIO()), config_path=TEMP_CONFIG)
check("the app starts on the palette already active",
      themes.current_key() == "nvtop", themes.current_key())
check("the status row names it before any key",
      app.theme_text() == f"theme: {themes.get('nvtop').label}", app.theme_text())

catalog = themes.keys()
expected_next = catalog[(catalog.index("nvtop") + 1) % len(catalog)]
app.handle(Key(name="t"))
check("T moved to the next theme in the catalogue",
      themes.current_key() == expected_next,
      f"{themes.current_key()} (expected {expected_next})")
check("the terminal now carries the new theme's colour",
      T.FG_ACCENT == T.fg(themes.current().accent, T.supports_truecolor()),
      repr(T.FG_ACCENT))

saved_raw = snapshot(TEMP_CONFIG) or b"{}"
saved = json.loads(saved_raw.decode("utf-8"))
check("the choice was written to config.json",
      saved.get("theme") == expected_next, str(saved.get("theme")))
check("the alarm rules in that file are untouched",
      saved.get("alarms") == ORIGINAL_CONFIG["alarms"],
      f"{len(saved.get('alarms', {}))} rules kept")
check("the rest of the file is untouched",
      saved.get("db_path") == DB and saved.get("per_core") is True
      and saved.get("_comment") == ORIGINAL_CONFIG["_comment"])
check("no stray temporary file was left behind",
      not os.path.exists(TEMP_CONFIG + ".tmp"))

for _ in range(len(catalog) - 1):
    app.handle(Key(name="t"))
check("a full cycle comes back round to the start",
      themes.current_key() == "nvtop", themes.current_key())
saved = json.loads((snapshot(TEMP_CONFIG) or b"{}").decode("utf-8"))
check("the wrap-around was saved too", saved.get("theme") == "nvtop",
      str(saved.get("theme")))
check("the real config.json was not written by any of this",
      snapshot(REAL_CONFIG) == real_config_before, real_config_detail())

# A config that cannot be written - a read-only install, or a path that is not
# there - must cost the user the saved preference and nothing else.
MISSING_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "diagtheme_missing")
check("the unwritable path really is unwritable", not os.path.exists(MISSING_DIR))

unwritable = tui.TuiApp(manager=manager, store=store, sampler=sampler,
                        screen=T.Screen(stream=io.StringIO()),
                        config_path=os.path.join(MISSING_DIR, "config.json"))
before_key = themes.current_key()
raised = ""
try:
    unwritable.handle(Key(name="t"))
except Exception as exc:  # noqa: BLE001
    raised = repr(exc)
check("an unwritable config does not end the session", not raised, raised)
check("the theme still changed for this run",
      themes.current_key() != before_key,
      f"{before_key} -> {themes.current_key()}")
check("the failure was reported rather than swallowed",
      "not saved" in unwritable.status_message, unwritable.status_message)
check("nothing appeared on disk for the failed write",
      not os.path.exists(MISSING_DIR))

print("\n[9] the real entry path applies the theme")
# serve() is what `gpumon.py --tui` calls: the theme has to reach the terminal
# through it, and its T key has to save to the path it was handed.
script = [(1.2, ["t"]), (1.0, ["q"])]


class ScriptedKeyboard:
    """A Keyboard that hands out a fixed, timed list of keys."""

    def __init__(self, steps: list[tuple[float, list[str]]]) -> None:
        self.steps = steps
        self.started = time.monotonic()
        self.index = 0
        self.pending: list[str] = []

    def enter(self) -> None:
        pass

    def exit(self) -> None:
        pass

    def read(self, timeout: float = 0.1) -> Key | None:
        while self.index < len(self.steps) and not self.pending:
            delay, keys = self.steps[self.index]
            due = self.started + delay
            if time.monotonic() < due:
                time.sleep(min(0.02, max(0.0, due - time.monotonic())))
                continue
            self.pending.extend(keys)
            self.index += 1
        if self.pending:
            return Key(name=self.pending.pop(0))
        return None


themes.set_theme("nvtop")
buffer = io.StringIO()
screen = T.Screen(stream=buffer)
screen.width, screen.height = 118, 36
result: dict[str, object] = {}
original_isatty = tui.sys_isatty
tui.sys_isatty = lambda: True


def run_serve() -> None:
    try:
        result["code"] = tui.serve(manager, store, sampler, screen=screen,
                                   keyboard=ScriptedKeyboard(script),
                                   config_path=TEMP_CONFIG)
    except Exception as exc:  # noqa: BLE001
        result["error"] = repr(exc)
    finally:
        result["done"] = True


with environment(COLORTERM="truecolor", TERM="xterm-256color"):
    worker = threading.Thread(target=run_serve, daemon=True)
    worker.start()
    deadline = time.time() + 30
    while time.time() < deadline and not result.get("done"):
        time.sleep(0.1)
    served_raw = buffer.getvalue()
    tui.sys_isatty = original_isatty

check("serve() ran and returned", result.get("done") is True)
check("serve() returned 0", result.get("code") == 0, str(result.get("code")))
check("no exception", "error" not in result, str(result.get("error", "")))
if "error" in result:
    print(f"       serve() raised: {result['error']}")

# Which theme `T` moves to is read from the catalogue rather than named here:
# the catalogue grows, and this assertion is about the key reaching the loop,
# not about which palette happens to sit next to nvtop.
_catalogue = themes.keys()
_expected_next = _catalogue[(_catalogue.index("nvtop") + 1) % len(_catalogue)]
check("serve() drew in the active palette",
      T.fg(themes.get(_expected_next).accent, True) in served_raw)
check("T reached the loop and moved the theme",
      themes.current_key() == _expected_next, themes.current_key())
saved = json.loads((snapshot(TEMP_CONFIG) or b"{}").decode("utf-8"))
check("the key press was saved through the injected path",
      saved.get("theme") == _expected_next, str(saved.get("theme")))
check("the alarm rules survived the entry path too",
      saved.get("alarms") == ORIGINAL_CONFIG["alarms"])
served_ansi = ANSI.VirtualScreen(118, 36)
served_ansi.feed(served_raw)
check("the served session rendered a readable frame",
      "gpumon" in served_ansi.full_text())
check("the served session entered and left the alternate screen",
      "?1049h" in served_raw and "?1049l" in served_raw)

print("\n[10] cleanup")
check("the real config.json is still byte-identical",
      snapshot(REAL_CONFIG) == real_config_before, real_config_detail())
for path in (TEMP_CONFIG, TEMP_CONFIG + ".tmp"):
    if os.path.exists(path):
        os.remove(path)
check("the scratch config was removed", not os.path.exists(TEMP_CONFIG))

themes.set_theme(themes.DEFAULT_KEY)
sampler.stop()
store.close()
manager.close()
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        try:
            os.remove(DB + suffix)
        except OSError:
            pass

print("\n" + "=" * 88)
if problems:
    print(f"THEME TEST FAILED ({len(problems)})")
    for name in problems:
        print(f"  - {name}")
else:
    print("THEME TEST PASSED")
print("=" * 88)
raise SystemExit(1 if problems else 0)
