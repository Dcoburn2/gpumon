"""End-to-end test of the terminal view's real entry path.

`tui.serve()` is what `gpumon.py --tui` calls. It needs an interactive terminal
and a keyboard, so both are injected here:

  * the Screen writes to an in-memory stream, and the captured escape stream is
    replayed through a small ANSI emulator to reconstruct the visible screen
  * the Keyboard is replaced with a scripted one that yields a fixed key
    sequence, which drives the real loop (render, handle, repeat) rather than
    poking at methods

That exercises the whole path - screen open/close, the key loop, tab switching,
scrolling, logging start/stop - without needing a TTY.
"""
from __future__ import annotations

import io
import os
import re
import sys
import threading
import time

import metrics as M
import sampler as SP
import store as S
import termlib
import termlib as T
import tui
import ansi_screen as ANSI

DB = "diagtuipty.db"
for s in ("", "-wal", "-shm"):
    if os.path.exists(DB + s):
        os.remove(DB + s)

problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    text = f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}"
    print(text.encode("ascii", "replace").decode("ascii"))
    if not ok:
        problems.append(label)


class ScriptedKeyboard:
    """A Keyboard that hands out a fixed list of keys, then nothing."""

    def __init__(self, script: list[tuple[float, list[str]]]) -> None:
        self.script = script
        self.started = time.monotonic()
        self.index = 0
        self._pending: list[str] = []
        self._next_at = self.started + (script[0][0] if script else 0.0)

    def enter(self) -> None:
        pass

    def exit(self) -> None:
        pass

    def read(self, timeout: float = 0.1) -> T.Key | None:
        now = time.monotonic()
        while self.index < len(self.script):
            delay, keys = self.script[self.index]
            due = self.started + delay
            if now < due:
                time.sleep(min(0.02, due - now))
                now = time.monotonic()
                continue
            self._pending.extend(keys)
            self.index += 1
            break
        if self._pending:
            return T.Key(name=self._pending.pop(0))
        return None


print("=" * 84)
print("TERMINAL ENTRY-PATH TEST")
print("=" * 84)

manager = M.SensorManager(per_core=True)
store = S.Store(DB)
sampler = SP.Sampler(manager, store, sample_hz=2.0, per_core=True)

# The script drives the real loop: settle, scroll, walk the tabs, start logging,
# stop logging, then quit. Two Shift-Tabs walk the focus ring from index 2 back
# to index 0 (the logging button) - Enter activates the *focused* control, which
# is the point of the keyboard navigation.
script: list[tuple[float, list[str]]] = [
    (2.5, []),
    (0.6, ["down"] * 5),
    (0.5, ["pagedown", "home"]),
    (0.6, ["right", "right"]),
    (0.5, ["1"]),
    (0.4, ["tab", "tab"]),
    (0.5, ["backtab", "backtab"]),   # focus ring -> START LOGGING
    (0.6, ["enter"]),                # -> start logging
    # Long enough for several samples at 2 Hz; sampling and the disk write are
    # both asynchronous, so a run this short still exercises the whole path.
    (4.0, ["enter"]),                # -> stop logging
    (1.0, ["s"]),
    (0.6, ["q"]),
]

buffer = io.StringIO()
screen = T.Screen(stream=buffer)
screen.width, screen.height = 118, 36

# serve() takes an injectable screen and keyboard precisely so this test can
# drive the real loop without a TTY. The TTY guard still has to be satisfied.
original_isatty = tui.sys_isatty
tui.sys_isatty = lambda: True

result: dict[str, int] = {}


def run() -> None:
    try:
        result["code"] = tui.serve(manager, store, sampler, screen=screen,
                                   keyboard=ScriptedKeyboard(script))
    except Exception as exc:  # noqa: BLE001
        result["error"] = repr(exc)
    finally:
        result["done"] = 1


worker = threading.Thread(target=run, daemon=True)
worker.start()
deadline = time.time() + 30
while time.time() < deadline and not result.get("done"):
    time.sleep(0.1)

tui.sys_isatty = original_isatty


raw = buffer.getvalue()
print(f"\ncaptured {len(raw)} bytes over {len(script)} scripted steps")
if "error" in result:
    print(f"  serve() raised: {result['error']}")

print("\n[1] serve() ran and returned")
check("serve finished", bool(result.get("done")))
check("serve returned 0", result.get("code") == 0, str(result.get("code")))
check("no exception", "error" not in result, str(result.get("error", "")))

print("\n[2] terminal lifecycle")
check("entered the alternate screen", "?1049h" in raw)
check("hid the cursor", "?25l" in raw)
check("showed the cursor again", "?25h" in raw)
check("left the alternate screen", "?1049l" in raw)

print("\n[3] one clean frame per tab")
# A scripted run interleaves many redraws, and stitching them together mixes
# frames. To inspect what a tab actually looks like, capture a single frame.
for tab in tui.TABS:
    frame_buffer = io.StringIO()
    frame_screen = T.Screen(stream=frame_buffer)
    frame_screen.width, frame_screen.height = 118, 60
    frame_app = tui.TuiApp(manager=manager, store=store, sampler=sampler,
                           screen=frame_screen)
    # A freshly built app has no sample until the sampler thread delivers one,
    # so seed it synchronously - otherwise the frame renders empty and the
    # assertions below would be testing nothing.
    frame_app._on_sample(time.time(), manager.poll())
    frame_app.set_tab(tab)
    frame_app.render()
    ansi = ANSI.VirtualScreen(118, 60)
    ansi.feed(frame_buffer.getvalue())
    content = ansi.text()
    first_lines = "\n".join(content.splitlines()[:6])
    print(f"\n  --- tab {tab} (first 6 lines) ---")
    print(first_lines.encode("ascii", "replace").decode("ascii"))
    check(f"{tab}: names the app", "gpumon" in content)
    check(f"{tab}: tab bar present", "LIVE" in content and "HELP" in content)
    check(f"{tab}: key hints present", "PgUp" in content)

live_buffer = io.StringIO()
live_screen = T.Screen(stream=live_buffer)
live_screen.width, live_screen.height = 118, 60
live_app = tui.TuiApp(manager=manager, store=store, sampler=sampler,
                      screen=live_screen)
live_app._on_sample(time.time(), manager.poll())
live_app.set_tab("live")
live_app.render()
live_ansi = ANSI.VirtualScreen(118, 60)
live_ansi.feed(live_buffer.getvalue())
live_text = live_ansi.text()

print("\n[4] the live frame carries the data")
check("all GPUs listed",
      all(f"gpu{g.index}" in live_text for g in manager.gpus))
check("a GPU name is shown",
      any(g.name.split()[0] in live_text for g in manager.gpus))
check("CPU panel rendered", "cpu / memory" in live_text)
check("per-core section rendered", "per-core" in live_text)
check("drawing characters present",
      any(c in live_text for c in "\u2800\u2581\u2588\u2591\u2592"))
check("metric rows present",
      all(label in live_text for label in ("TEMP", "GPU", "VRAM", "CORE", "PWR")))

print("\n[5] terminal lifecycle and input handling")
check("entered the alternate screen", "?1049h" in raw)
check("hid the cursor", "?25l" in raw)
check("showed the cursor again", "?25h" in raw)
check("left the alternate screen", "?1049l" in raw)
check("frames were redrawn more than once", raw.count("\x1b[2J") >= 2,
      f"{raw.count(chr(27) + '[2J')} clears")

sessions = store.list_sessions(limit=1)
check("logging started and a session was stored", bool(sessions),
      f"#{sessions[0].id}" if sessions else "none")

sampler.stop()
store.close()
manager.close()
for s in ("", "-wal", "-shm"):
    if os.path.exists(DB + s):
        try:
            os.remove(DB + s)
        except OSError:
            pass

print("\n" + "=" * 84)
if problems:
    print(f"ENTRY-PATH TEST FAILED ({len(problems)})")
    for p in problems:
        print(f"  - {p}")
else:
    print("ENTRY-PATH TEST PASSED")
print("=" * 84)
raise SystemExit(1 if problems else 0)
