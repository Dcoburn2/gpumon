"""Verify the terminal view: key decoding, navigation and rendering.

The TUI cannot be driven by a real terminal here, so the app is exercised
directly: keys are fed to `handle()`, and the rendered frame is captured by
pointing the Screen at an in-memory stream and inspecting the escape output.
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
import os
import re
import time

import metrics as M
import sampler as SP
import store as S
import termlib as T
import tui

DB = "diagtui.db"
for s in ("", "-wal", "-shm"):
    if os.path.exists(DB + s):
        os.remove(DB + s)

problems: list[str] = []


def ascii_safe(text: str) -> str:
    """Console output here is cp1252, which cannot encode Braille or blocks."""
    return text.encode("ascii", "replace").decode("ascii")


def check(label: str, ok: bool, detail: str = "") -> None:
    print(ascii_safe(f"  {'OK  ' if ok else 'FAIL'} {label}"
                     f"{'  ' + detail if detail else ''}"))
    if not ok:
        problems.append(label)


print("=" * 88)
print("TERMINAL VIEW TEST")
print("=" * 88)

print("\n[1] termlib primitives")
check("ansi stripping measures visible width",
      T.visible_width(f"{T.FG_OK}abc{T.RESET}") == 3,
      str(T.visible_width(f"{T.FG_OK}abc{T.RESET}")))
bar = T.braille_bar(0.5, 8)
check("braille bar has the requested cell count", len(bar) == 8,
      f"{len(bar)} cells")
check("braille bar is all braille codepoints",
      all(0x2800 <= ord(c) <= 0x28FF for c in bar))
check("braille bar grows with the fraction",
      T.braille_bar(0.9, 8) != T.braille_bar(0.1, 8))
spark = T.sparkline([0, 25, 50, 75, 100], 5, 0, 100)
check("sparkline length matches", len(spark) == 5, repr(spark))
check("sparkline ends at the highest block", spark.endswith("\u2588"), repr(spark))
check("sparkline ignores NaN", T.sparkline([float("nan")] * 5, 5).strip() == "")
check("clip keeps escape prefixes",
      T.visible_width(T._clip_ansi(f"{T.FG_OK}abcdef{T.RESET}", 3)) == 3)

print("\n[2] key decoding")
from termlib import Key
app_keys = []
for char, expected in (("q", "q"), ("\r", "enter"), ("\t", "tab"),
                       ("\x1b", "escape"), ("\x03", "ctrl-c")):
    key = T._decode_char(char)
    app_keys.append((char, key.name, expected))
for char, got, expected in app_keys:
    check(f"decode {char!r} -> {expected}", got == expected, f"got {got!r}")
check("uppercase carries shift", T._decode_char("L").shift is True)

print("\n[3] build the app against real sensors")
manager = M.SensorManager(per_core=True)
store = S.Store(DB)
sampler = SP.Sampler(manager, store, sample_hz=2.0, per_core=True)
buffer = io.StringIO()
screen = T.Screen(stream=buffer)
screen.width, screen.height = 120, 40
app = tui.TuiApp(manager=manager, store=store, sampler=sampler, screen=screen)
check("app built", app.tab == "live")
check("buttons present", len(app.buttons) == 6, f"{len(app.buttons)} buttons")

sampler.start()
time.sleep(3.0)
sample = app.values()
check("samples arrive on the app", bool(sample), f"{len(sample)} metrics")

print("\n[4] navigation")
app.handle(Key(name="right"))
check("right arrow switches tab", app.tab == "stats", app.tab)
app.handle(Key(name="right"))
check("right again", app.tab == "alarms", app.tab)
app.handle(Key(name="left"))
check("left goes back", app.tab == "stats", app.tab)
app.handle(Key(name="1"))
check("digit jumps to a tab", app.tab == "live", app.tab)

app.handle(Key(name="tab"))
check("tab moves the focus ring", app.focus == 1, str(app.focus))
app.handle(Key(name="backtab"))
check("shift-tab moves it back", app.focus == 0, str(app.focus))

app.max_scroll = 10
app.handle(Key(name="down"))
check("down scrolls", app.scroll == 1, str(app.scroll))
app.handle(Key(name="pageup"))
check("page up clamps at the top", app.scroll == 0, str(app.scroll))
app.handle(Key(name="end"))
check("end jumps to the bottom", app.scroll == app.max_scroll, str(app.scroll))
app.handle(Key(name="home"))
check("home jumps to the top", app.scroll == 0, str(app.scroll))

print("\n[5] every tab renders without error")
for tab in tui.TABS:
    app.set_tab(tab)
    buffer.truncate(0)
    buffer.seek(0)
    app.render()
    output = buffer.getvalue()
    lines = app._body_lines(120)
    check(f"tab {tab} renders", len(output) > 50 and len(lines) > 0,
          f"{len(lines)} lines, {len(output)} bytes")

app.set_tab("live")
buffer.truncate(0)
buffer.seek(0)
app.render()
frame = buffer.getvalue()
plain = T.strip_ansi(frame)
check("frame names the app", "gpumon" in plain)
check("frame shows the tab bar", "LIVE" in plain and "SESSIONS" in plain)
check("frame shows key hints", "PgUp" in plain and "Home/End" in plain)
check("frame contains drawing characters",
      any(ch in plain for ch in "\u2588\u2591\u2800\u2581"))
gpu_names = [g.name for g in manager.gpus]
check("frame lists every GPU", all(n in plain for n in gpu_names),
      f"{len(gpu_names)} GPUs")

print("\n[6] buttons activate from the keyboard")
app.focus = 0
app.handle(Key(name="enter"))
time.sleep(1.2)
check("Enter started logging", sampler.logging)
app.focus = 0
app.handle(Key(name="enter"))
time.sleep(1.2)
check("Enter stopped logging", not sampler.logging)
sessions = store.list_sessions(limit=1)
check("session was stored", bool(sessions),
      f"session #{sessions[0].id} with {sessions[0].sample_count} samples"
      if sessions else "none")

print("\n[7] stats tab reads the stored session")
app.set_tab("stats")
lines = app._body_lines(120)
text = T.strip_ansi(" ".join(t for _c, t in lines))
check("stats tab shows a session", "session #" in text)
check("stats tab has a header row", "avg" in text and "p95" in text)

print("\n[8] live tab reflects the alarm state")
app.set_tab("alarms")
alarm_text = T.strip_ansi(" ".join(t for _c, t in app._body_lines(120)))
check("alarms tab renders", "active alarms" in alarm_text)

print("\n[9] resize is tolerated")
for width, height in ((60, 20), (200, 60), (40, 12)):
    screen.width, screen.height = width, height
    buffer.truncate(0)
    buffer.seek(0)
    app.render()
    check(f"renders at {width}x{height}", len(buffer.getvalue()) > 20)

print("\n[10] quit key stops the loop")
app.handle(Key(name="q"))
check("q requests exit", app.running is False)

sampler.stop()
store.close()
manager.close()
for s in ("", "-wal", "-shm"):
    if os.path.exists(DB + s):
        try:
            os.remove(DB + s)
        except OSError:
            pass

print("\n" + "=" * 88)
if problems:
    print(f"TERMINAL TEST FAILED ({len(problems)})")
    for p in problems:
        print(f"  - {p}")
else:
    print("TERMINAL TEST PASSED")
print("=" * 88)
raise SystemExit(1 if problems else 0)
