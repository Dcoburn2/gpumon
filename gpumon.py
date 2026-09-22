"""gpumon - entry point.

Usage:
    python gpumon.py                 launch the live monitor GUI
    python gpumon.py --per-core      also record per-CPU-core utilisation
    python gpumon.py --hz 2          sample at 2 Hz (default 1 Hz)
    python gpumon.py --selftest      headless check of sensors + storage
    python gpumon.py --report N      print a text report for session N
    python gpumon.py --list          list logged sessions

Word forms of the same subcommands are accepted too, in any position, so
`gpumon --db other.db report 3` behaves as written rather than being silently
ignored: see _resolve_subcommands().
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import traceback

# Allow running from any working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import apppaths        # noqa: E402
import alarms as A      # noqa: E402
import metrics as M     # noqa: E402
import platforms as PL  # noqa: E402
import sampler as SP    # noqa: E402
import store as S       # noqa: E402
from ui import themes    # noqa: E402

#: Where a detached launch writes its output. See _capture_output().
LAUNCH_LOG = "gpumon-launch.log"
#: Truncate the log once it passes this, so a log nobody reads cannot grow.
LOG_LIMIT = 256 * 1024


def _capture_output() -> str:
    """Give a windowless launch somewhere to print.

    The desktop window is started through `pythonw.exe` so that closing the
    console that launched it does not kill the app. A windowed interpreter has
    no stdout or stderr at all - they are None - so anything printed, including
    a traceback from a failure before the window exists, would vanish silently.
    Pointing both at a log file keeps those failures findable; the Tk error
    dialog covers the rest.

    The launcher also sets GPUMON_DETACHED=1 and points the child's standard
    handles at NUL (a detached app holding the caller's pipe open would make
    the caller wait for it). A handle to NUL is a usable stream, so the
    environment variable is what decides in that case.

    Returns the log path, or "" when the streams were already usable.
    """
    if os.environ.get("GPUMON_DETACHED") != "1" \
            and sys.stdout is not None and sys.stderr is not None:
        return ""
    path = apppaths.state_path(LAUNCH_LOG)
    try:
        if os.path.exists(path) and os.path.getsize(path) > LOG_LIMIT:
            os.remove(path)
        stream = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        # No log is not a reason to refuse to start.
        class _Discard:
            def write(self, _text: str) -> int:
                return 0

            def flush(self) -> None:
                return None

            def isatty(self) -> bool:
                return False
        sys.stdout = sys.stderr = _Discard()   # type: ignore[assignment]
        return ""
    import datetime
    print(f"\n=== gpumon {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ===",
          file=stream)
    sys.stdout = sys.stderr = stream           # type: ignore[assignment]
    return path


CAPTURED_TO = _capture_output()


def spawn_detached(argv: list[str]) -> int:
    """Start the desktop window in a process that outlives this console.

    `start` in the batch launcher was not enough on its own. It leaves the app
    as a child of that console, so a shell that kills its process tree - Windows
    Terminal, an IDE terminal, a job object - takes gpumon with it; and when
    `pythonw.exe` could not be found the launcher fell back to console `python`,
    which keeps a console window of its own. Either way the user closes a black
    box and the app disappears, which is the bug this exists to kill.

    So the child is created with DETACHED_PROCESS (no console is allocated at
    all, whatever interpreter runs it), CREATE_NEW_PROCESS_GROUP, and
    CREATE_BREAKAWAY_FROM_JOB - the last of those is what escapes a terminal's
    job object. Breakaway is attempted first but not insisted on: a job can
    forbid it, and refusing to start would be worse than starting inside it.

    Returns 0 when something was started, 2 when nothing could be.
    """
    if os.name != "nt":
        print("--spawn is Windows-only; the shell launcher handles POSIX.",
              file=sys.stderr)
        return 2
    import ctypes
    from ctypes import wintypes

    class STARTUPINFO(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                    ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                    ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                    ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD),
                    ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD),
                    ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                    ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE),
                    ("hStdError", wintypes.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD),
                    ("dwThreadId", wintypes.DWORD)]

    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    interpreter = sys.executable or "python"
    if getattr(sys, "frozen", False):
        # A frozen build has no script to point at: re-running the exe with the
        # same arguments is the equivalent launch.
        command = subprocess.list2cmdline([interpreter] + list(argv))
    else:
        windowless = os.path.join(os.path.dirname(interpreter), "pythonw.exe")
        if os.path.exists(windowless):
            interpreter = windowless
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "gpumon.py")
        # list2cmdline quotes the way CreateProcess expects: without it this
        # path, which lives under Downloads, would split into two arguments.
        command = subprocess.list2cmdline([interpreter, script] + list(argv))
    attempts = (
        DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB,
        DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
    )
    for flags in attempts:
        info = PROCESS_INFORMATION()
        startup = STARTUPINFO()
        startup.cb = ctypes.sizeof(startup)
        started = kernel32.CreateProcessW(
            None, ctypes.c_wchar_p(command), None, None, False, flags, None,
            apppaths.app_dir(), ctypes.byref(startup), ctypes.byref(info))
        if started:
            kernel32.CloseHandle(info.hProcess)
            kernel32.CloseHandle(info.hThread)
            print(f"Started gpumon ({interpreter})")
            print(f"Its output goes to {LAUNCH_LOG} next to this script.")
            return 0
    print(f"Could not start gpumon: Windows error {ctypes.get_last_error()}",
          file=sys.stderr)
    return 2


def print_themes() -> int:
    """`--list-themes`: the catalogue, with each theme's own colours as a guide."""
    print("Colour themes (--theme NAME, or press T in the desktop window)\n")
    for key, label, blurb in themes.catalog():
        marker = "*" if key == themes.current_key() else " "
        palette = themes.get(key)
        print(f" {marker} {key:<9} {label:<18} {blurb}")
        print(f"     series: {' '.join(palette.series[:6])}")
    print("\n * = the theme in config.json")
    return 0


def _config() -> dict:
    return A.load_config()


def _refuse_off_windows(tool: str, reason: str) -> bool:
    """True when a Windows-only helper tool was refused on another OS.

    `--setup-sensors` drives LibreHardwareMonitor, `--calibrate-gpus` reads the
    LUID performance counters and `--vram-report` / `--vram-check` query AMD's
    ADL: all Windows APIs with no Linux counterpart, where the same telemetry
    comes from sysfs instead. Saying so plainly beats a traceback from deep
    inside a backend stub. Returns False (and prints nothing) on Windows, so the
    Windows behaviour of every one of those tools is untouched.
    """
    if PL.current_platform() == "windows":
        return False
    print(f"{tool} is Windows-only: {reason}. "
          "On Linux the equivalent data comes from /sys (see the notes printed "
          "by `python gpumon.py --selftest`).")
    return True


USAGE = """gpumon - GPU and system telemetry

  gpumon              live desktop window
  gpumon tui          terminal view
  gpumon web          browser GUI on http://127.0.0.1:8080
  gpumon report N     text report for session N
  gpumon list         list logged sessions
  gpumon selftest     verify sensors, storage and alarms

Options: --db PATH  --hz N  --per-core  --host ADDR  --port N  --theme NAME
  gpumon --list-themes     the colour themes
Anything else is passed through to gpumon.py; run "gpumon --help" for the
full flag list."""


def usage() -> None:
    print(USAGE)


def _resolve_subcommands(args: argparse.Namespace,
                         extra: list[str]) -> tuple[int | None, list[str]]:
    """Fold word-form subcommands from `extra` into `args`.

    `gpumon report 3` and `gpumon list` are friendlier than `--report 3` and
    `--list`, so the launchers advertise them. Handling them here rather than in
    the .cmd file means they work in any position and on every platform:
    `gpumon --db other.db report 3` used to leave "report 3" unparsed, and an
    unparsed argument silently started the desktop window - which looks exactly
    like a hang.

    Nothing is rejected here. The leftover tokens come back so that only the
    default GUI path has to care about them: the specialist tools
    (`--vram-check --phase loaded`, `--setup-sensors`, `--calibrate-gpus`) parse
    their own trailing arguments, and eating them would break those instead.

    Returns (exit code to stop at or None, unclaimed tokens).
    """
    rest: list[str] = []
    index = 0
    while index < len(extra):
        word = extra[index]
        lowered = word.lower()
        if lowered in ("tui", "terminal"):
            args.tui = True
        elif lowered in ("web", "server", "serve"):
            args.web = True
        elif lowered in ("list", "sessions", "ls"):
            args.list = True
        elif lowered in ("selftest", "self-test", "check"):
            args.selftest = True
        elif lowered in ("gui", "desktop", "window"):
            pass
        elif lowered in ("help", "-h", "?", "/?"):
            usage()
            return 0, []
        elif lowered == "report":
            if index + 1 >= len(extra):
                print("Which session?  Usage:  gpumon report NUMBER", file=sys.stderr)
                print('Run "gpumon list" to see the session ids.', file=sys.stderr)
                return finish(1), []
            try:
                args.report = int(extra[index + 1])
            except ValueError:
                print(f"report needs a session number, not {extra[index + 1]!r}",
                      file=sys.stderr)
                return finish(1), []
            index += 1
        else:
            rest.append(word)
        index += 1

    return None, rest


def _reject_stray_arguments(rest: list[str]) -> int | None:
    """Refuse to open the desktop window over arguments nobody understood.

    Called only on the default path. `gpumon listt` used to become "start the
    GUI with one mystery argument", which on a double-clicked launcher is
    indistinguishable from a hang; saying so is the whole point.
    """
    if not rest:
        return None
    print(f"gpumon: unrecognised argument(s): {' '.join(rest)}", file=sys.stderr)
    print('Nothing was started. Run "gpumon help" for the list of modes.',
          file=sys.stderr)
    return 2



def selftest() -> int:
    """Headless verification of every layer; returns a process exit code."""
    print("=" * 78)
    print("gpumon self-test")
    print("=" * 78)

    ok = True
    print("\n[1] sensors")
    manager = M.SensorManager(per_core=False)
    caps = manager.capabilities()
    for g in caps.gpus:
        print(f"    gpu{g.index}  {g.vendor:<7} {g.name}")
        print(f"           sources={g.sources} vram={g.vram_total_mb} luids={g.luids}")
    for name, backend in (("nvidia-smi", manager.nvidia), ("psutil", manager.system),
                          ("pdh", manager.pdh), ("lhm", manager.lhm)):
        state = "up" if backend.available() else "down"
        detail = getattr(backend, "error", "") or ""
        print(f"    backend {name:<11} {state:<5} {detail[:60]}")
    for note in caps.notes:
        print(f"    note: {note}")

    manager.prime()
    print("\n[2] polling three times")
    for i in range(3):
        values = manager.poll()
        interesting = {k: round(v, 1) for k, v in sorted(values.items())
                       if M.gpu_field_of(k) in ("temp", "util", "vram_used", "power")
                       or k in ("cpu_util", "cpu_temp", "ram_percent")}
        print(f"    #{i}: {len(values)} metrics  {interesting}")
    if not values:
        print("    FAIL: no metrics returned")
        ok = False

    print("\n[3] store round-trip")
    db = apppaths.app_path("selftest.db")
    if os.path.exists(db):
        os.remove(db)
    st = S.Store(db)
    sid = st.create_session(label="selftest", sample_hz=1.0,
                            devices=[S.DeviceRecord(index=0, name="test")],
                            capabilities={}, config={}, app_version="test")
    for i in range(10):
        st.write_samples(sid, float(i), 1000.0 + i,
                         {"gpu0_temp": 40.0 + i, "gpu0_util": 50.0 + i})
    st.finish_session(sid)
    stats = st.stats(sid, "gpu0_temp")
    print(f"    session {sid}: {stats.count} samples, min {stats.minimum}, "
          f"max {stats.maximum}, mean {stats.mean:.1f}, p95 {stats.p95:.1f}")
    assert stats.count == 10 and stats.maximum == 49.0, "statistics wrong"
    print("    OK")

    print("\n[4] alarm engine (crossing, hysteresis, debounce)")
    eng = A.AlarmEngine()
    eng.set_rule(A.AlarmRule("gpu0_temp", warning=50.0, critical=60.0,
                             hysteresis=2.0, min_duration=0.0))
    eng.set_rule(A.AlarmRule("gpu1_temp", warning=50.0, hysteresis=2.0,
                             min_duration=1.5))
    events: list[str] = []
    script = [
        (0.0, {"gpu0_temp": 40.0, "gpu1_temp": 40.0}),
        (1.0, {"gpu0_temp": 52.0, "gpu1_temp": 52.0}),
        (2.0, {"gpu0_temp": 61.0, "gpu1_temp": 52.0}),
        (3.0, {"gpu0_temp": 55.0, "gpu1_temp": 53.0}),
        (4.0, {"gpu0_temp": 50.0, "gpu1_temp": 53.0}),
        (5.0, {"gpu0_temp": 47.0, "gpu1_temp": 53.0}),
        (6.0, {"gpu0_temp": 47.0, "gpu1_temp": 44.0}),
    ]
    for t, vals in script:
        for upd in eng.evaluate(t, 1000.0 + t, vals):
            events.append(f"t={t:.0f} {upd.kind} {upd.alarm.metric} "
                          f"{upd.alarm.level} peak={upd.alarm.peak:.0f}")
    for e in events:
        print(f"    {e}")
    expected = [
        "t=1 open gpu0_temp warning",
        "t=2 close gpu0_temp warning",
        "t=2 open gpu0_temp critical",
        "t=3 open gpu1_temp warning",
        "t=5 close gpu0_temp critical",
        "t=6 close gpu1_temp warning",
    ]
    got = [e.split(" peak")[0] for e in events]
    if got != expected:
        print(f"    FAIL: expected {expected}")
        ok = False
    else:
        print("    OK (escalation, hysteresis release and debounce all correct)")
        # Document what these transitions prove, so a future reader trusts them.
        print("       t=1 warning opens; t=2 escalates to critical; hysteresis keeps")
        print("       critical open at 55 C (release is 58 C) and clears it at 50 C;")
        print("       gpu1's alarm is debounced until it has persisted 1.5 s.")

    print("\n[5] report generation")
    import report
    text = report.build_text_report(st, sid)
    html_doc = report.build_html_report(st, sid)
    print(f"    text report {len(text)} bytes, html report {len(html_doc)} bytes")
    if "gpumon report" not in text or "<svg" not in html_doc:
        print("    FAIL: report content looks wrong")
        ok = False
    else:
        print("    OK")

    st.close()
    manager.close()
    os.remove(db)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db + suffix):
            os.remove(db + suffix)
    print("\n" + "=" * 78)
    print("SELF-TEST PASSED" if ok else "SELF-TEST FAILED")
    print("=" * 78)
    return 0 if ok else 1


def list_sessions(db_path: str) -> int:
    store = S.Store(db_path)
    sessions = store.list_sessions()
    if not sessions:
        print("No sessions logged yet.")
        return 0
    print(f"{'id':>4}  {'started':<20} {'duration':>12} {'samples':>8} "
          f"{'alarms':>7}  label")
    for s in sessions:
        alarms = store.alarms(s.id)
        print(f"{s.id:>4}  "
              f"{__import__('time').strftime('%Y-%m-%d %H:%M:%S', __import__('time').localtime(s.started_at)):<20} "
              f"{S.human_duration(s.duration):>12} {s.sample_count:>8} "
              f"{len(alarms):>7}  {s.label or '(unlabelled)'}")
    store.close()
    return 0


def vram_report() -> int:
    """Print every VRAM figure each backend offers, side by side.

    Windows, ADL and the performance counters disagree about AMD VRAM on some
    drivers, so this exists to compare them against whatever the workload itself
    reports (rocm-smi, nvidia-smi, the application's own console).
    """
    import time

    manager = M.SensorManager(per_core=False)
    manager.prime()
    time.sleep(1.0)

    print("=" * 84)
    print("VRAM REPORT - every source, per card")
    print("=" * 84)

    print("\nCards:")
    for g in manager.gpus:
        print(f"  gpu{g.index}  {g.display_name}")
        print(f"      sources={g.sources}  luid_confidence={g.luid_confidence}")
        print(f"      total (registry) = "
              f"{f'{g.vram_total_mb:,.0f} MB' if g.vram_total_mb else 'unknown'}")

    print("\n1. gpumon's recorded value (what the UI shows)")
    values = manager.poll()
    for g in manager.gpus:
        i = g.index
        used = values.get(f"gpu{i}_vram_used")
        total = values.get(f"gpu{i}_vram_total")
        pct = values.get(f"gpu{i}_vram_percent")
        used_txt = f"{used:,.0f} MB" if isinstance(used, float) else "n/a"
        print(f"  gpu{i}: used={used_txt}  total="
              f"{f'{total:,.0f} MB' if total else 'n/a'}  "
              f"pct={f'{pct:.1f}%' if isinstance(pct, float) else 'n/a'}")

    print("\n2. ADL: ADL2_Adapter_DedicatedVRAMUsage_Get, per adapter")
    for line in manager.amd.report():
        print(f"  {line}")
    if manager.amd.available():
        for a in manager.amd.adapters:
            mb = manager.amd._dedicated_vram(a.index)
            print(f"  ADL adapter {a.index} ({a.pci_label}, {a.name}): "
                  f"{f'{mb:,.0f} MB' if mb is not None else 'n/a'}")
    else:
        print(f"  ADL unavailable: {manager.amd.error}")

    print("\n3. Windows performance counters, per LUID (raw)")
    manager.pdh._open_query()
    manager.pdh.refresh_memory_counters()
    manager.pdh.collect()
    time.sleep(0.3)
    manager.pdh.collect()
    with manager.pdh._counter_lock:
        for _path, (handle, luid) in manager.pdh._mem_counters.items():
            raw = manager.pdh._read(handle)
            owner = next((f"gpu{g.index}" for g in manager.gpus if luid in g.luids),
                         "(unassigned)")
            mb = raw / (1024 * 1024) if raw is not None else None
            print(f"  {luid}  owner={owner:6}  "
                  f"{f'{mb:,.0f} MB' if mb is not None else 'n/a'}")

    print("\n4. Sum of the per-LUID counters above, by owner")
    sums: dict[str, float] = {}
    with manager.pdh._counter_lock:
        for _path, (handle, luid) in manager.pdh._mem_counters.items():
            raw = manager.pdh._read(handle)
            if raw is None:
                continue
            owner = next((f"gpu{g.index}" for g in manager.gpus if luid in g.luids),
                         "unassigned")
            sums[owner] = sums.get(owner, 0.0) + raw / (1024 * 1024)
    for owner, mb in sorted(sums.items()):
        print(f"  {owner}: {mb:,.0f} MB")

    print("\n" + "=" * 84)
    print("How to read this")
    print("=" * 84)
    print("""
While your workload is loaded, compare the figures above against what the
workload itself reports (rocm-smi, nvidia-smi, or its own console). Whichever
source matches is the one to trust.

gpumon prefers ADL for AMD cards because Windows' own counter was measured
returning a card's worth of memory twice over on one V620 and zero on the other.
If ADL is the one that disagrees with your workload, say so and the preference
can be swapped - metrics.py: _poll_amd() is the only place that decides.""")
    manager.close()
    return 0


def print_report(db_path: str, session_id: int) -> int:
    store = S.Store(db_path)
    if store.get_session(session_id) is None:
        print(f"Session {session_id} not found in {db_path}")
        return 1
    print(__import__("report").build_text_report(store, session_id))
    store.close()
    return 0


def _install_crash_logging(root) -> str:
    """Record unhandled failures in a file, because a windowed build has no console.

    Tk's own handler prints a traceback to stderr, and a packaged build has no
    stderr to print to: an exception inside a callback would make a graph stop
    updating with nothing anywhere to say why. Worse, `threading.excepthook` does
    the same for the sampler and sensor-worker threads, and those are where the
    driver calls and the polling live.

    Everything goes to one log beside the program, so a bug report can contain the
    actual traceback rather than "it froze".
    """
    import threading
    import time
    import traceback

    path = apppaths.state_path("gpumon-error.log")

    def record(kind: str, exc: BaseException) -> None:
        try:
            with open(path, "a", encoding="utf-8", errors="replace") as handle:
                handle.write(f"\n=== {kind} at "
                             f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                             f"(gpumon {SP.APP_VERSION}) ===\n")
                traceback.print_exception(type(exc), exc, exc.__traceback__,
                                          file=handle)
        except OSError:
            pass

    def tk_hook(exc_type, exc, tb) -> None:
        record("interface error", exc)
        traceback.print_exception(exc_type, exc, tb)      # stderr, when there is one

    def thread_hook(argument) -> None:
        record(f"error in the {getattr(argument.thread, 'name', 'worker')} thread",
               argument.exc_value or RuntimeError("unknown"))

    def plain_hook(exc_type, exc, tb) -> None:
        record("error", exc)
        traceback.print_exception(exc_type, exc, tb)

    root.report_callback_exception = tk_hook
    threading.excepthook = thread_hook
    sys.excepthook = plain_hook
    return path


def launch_gui(args: argparse.Namespace, config: dict) -> int:
    import tkinter as tk

    per_core = args.per_core or bool(config.get("per_core", False))
    store = S.Store(args.db)
    manager = M.SensorManager(per_core=per_core)
    engine = A.AlarmEngine.from_config(config.get("alarms"))
    sampler = SP.Sampler(manager, store, sample_hz=args.hz, alarm_engine=engine,
                         per_core=per_core)
    # Apply any per-metric rules from config.json on top of the defaults.
    sampler.configure_alarms(engine.rules or None)

    root = tk.Tk()
    # Before anything else: a failure anywhere should leave a traceback behind.
    _install_crash_logging(root)
    _apply_window_icon(root)
    from ui.monitor import MonitorApp
    # Where the AMD loader got to, before the window opens. A windowed launch
    # sends stdout to the launch log, so a packaged build that has lost ADL says
    # so there rather than only showing a card with no temperature.
    for line in manager.amd.report():
        print(line)
    MonitorApp(root, manager, store, sampler)
    root.mainloop()
    return 0


def launch_tui(args: argparse.Namespace, config: dict) -> int:
    """Run the terminal interface."""
    per_core = args.per_core or bool(config.get("per_core", False))
    store = S.Store(args.db)
    manager = M.SensorManager(per_core=per_core)
    engine = A.AlarmEngine.from_config(config.get("alarms"))
    sampler = SP.Sampler(manager, store, sample_hz=args.hz, alarm_engine=engine,
                         per_core=per_core)
    sampler.configure_alarms(engine.rules or None)
    import tui
    return tui.serve(manager, store, sampler)


def _apply_window_icon(root) -> str:
    """Give the window the application icon. Returns the file that was applied.

    On Windows the multi-resolution `.ico` is the only path that works properly:
    `iconbitmap` hands Windows the file and it picks the 16 px frame for the
    title bar and the 256 px one for the taskbar. `iconphoto` was being called
    as well, and Tk rescales the 256 px photo itself for the title bar - the
    result was a mangled grey patch where the logo should be, while the .ico
    frames themselves are all correct (verified frame by frame). So Windows gets
    `iconbitmap` only, and `iconphoto` is left to X11 and Wayland, where it is
    the call that sticks.

    `iconbitmap(default=...)` alone was not enough either: `default` applies to
    windows created *after* the call, and the Tk root already exists by then.

    Reading the icon back cannot distinguish success from a silent no-op
    (`iconbitmap()` returns an empty string either way on this Tk), so the return
    value reports which file was handed to Tk.
    """
    import tkinter as tk
    # resource_path, not app_path: a frozen build carries the icon inside its
    # bundle, which is a different directory from the exe during a run.
    applied = ""
    ico = apppaths.resource_path("gpumon.ico")
    if os.path.exists(ico):
        try:
            root.iconbitmap(ico)
            applied = ico
        except tk.TclError:
            pass
        try:
            root.iconbitmap(default=ico)
        except tk.TclError:
            pass
    if os.name == "nt":
        return applied
    png = apppaths.resource_path("gpumon.png")
    if os.path.exists(png):
        try:
            # Keep a reference: Tk discards the image if Python collects it.
            root._gpumon_icon = tk.PhotoImage(file=png)
            root.iconphoto(True, root._gpumon_icon)
            applied = applied or png
        except tk.TclError:
            pass
    return applied


def _msr_probe() -> int:
    """Read the CPU's own sensors through the driver, and say what happened.

    This is the direct path: identify the processor, load the signed module into
    the PawnIO driver, read the registers. Run it elevated - opening a kernel
    driver is the one step no user-mode process can do for itself.

    Everything it prints is also written to `msr-probe.txt` beside the settings,
    because the packaged build is a windowed program with no console: running it
    from a prompt appears to do nothing at all, and the report is the whole point
    of the command. On a machine whose processor is not being read - an AMD one,
    say - that file is what to send.
    """
    import cpusensors

    lines: list[str] = []

    def say(text: str = "") -> None:
        # This one really is a print: everything else in the probe goes through
        # say(), and saying it here would be the shortest infinite loop going.
        print(text)
        lines.append(text)

    def finish(code: int) -> int:
        """Write the report and return, on every path out of here.

        The two failure paths are the ones a person actually hits - not elevated,
        or the driver not installed - and they were the two that returned without
        writing anything. The report exists precisely for those cases.
        """
        try:
            import apppaths
            report = apppaths.state_path("msr-probe.txt")
            with open(report, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            print(f"\n  written to {report}")
        except OSError as exc:
            print(f"\n  could not write the report: {exc}")
        return code

    identity = cpusensors.identify()
    say("=" * 78)
    say("CPU SENSOR PROBE (direct register access)")
    say("=" * 78)
    say(f"  processor     : {identity.name or 'unknown'}")
    say(f"  {identity.describe()}")
    say(f"  logical cores : {identity.cores}")
    say(f"  decoding path : {'yes' if identity.supported_by_us else 'no'}")

    import pawnio
    say(f"  PawnIO driver : {'installed' if pawnio.driver_installed() else 'not installed'}")
    with pawnio.PawnIO() as pawn:
        if not pawn.open():
            say(f"\n  {pawn.error}")
            say("\n  Run this from an elevated prompt: the driver device can only "
                  "be opened by an administrator.")
            return finish(1)
        say("  device opened : yes")
        if identity.vendor == "amd":
            say("  AMD modules   : " + ", ".join(
                f"{name}{' present' if os.path.exists(pawnio.module_path(name)) else ' MISSING'}"
                for name in cpusensors.AMD_MODULES))
        readings = cpusensors.read_cpu(pawn)
        if identity.vendor == "amd":
            # The raw register, before any decoding: if this is the failure, the
            # value says so at a glance.
            import msr
            raw = pawn.read_smn(msr.AMD_THM_TCON_CUR_TMP)
            say(f"  SMN {msr.AMD_THM_TCON_CUR_TMP:#010x} : "
                f"{'no answer' if raw is None else hex(raw)}"
                f"{'' if raw is None else f'  -> {msr.parse_amd_temperature(raw)} C'}")
            say(f"  AMD offset    : {msr.amd_tctl_offset(identity.name):+.0f} C"
                f"  (for {identity.name!r})")
            if raw is None:
                say(f"  SMN error     : {pawn.error or 'the module returned nothing'}")

    if readings.error:
        say(f"\n  {readings.error}")
        return 1
    say(f"\n  TjMax         : {readings.tjmax:.0f} C")
    say(f"  CPU package   : {readings.package} C")
    cores = readings.per_core
    if cores:
        shown = ", ".join("n/a" if c is None else f"{c:.0f}"
                          for c in cores[:12])
        say(f"  per-core      : {shown}")
        say(f"  hottest core  : {readings.hottest()} C")
    if readings.frequency_mhz:
        say(f"  frequency     : {readings.frequency_mhz:.0f} MHz")
    if readings.voltage:
        say(f"  core voltage  : {readings.voltage:.3f} V")

    # Cross-check against whatever else can report a CPU temperature, so a
    # decoding mistake shows up as a disagreement rather than as a number nobody
    # can question. Sampled in pairs, at the same instant: comparing a reading
    # taken now with one taken a second ago proves nothing on a CPU whose
    # package temperature moves several degrees in that time.
    try:
        import time

        import cpusensors as CS
        import metrics as M
        manager = M.SensorManager(per_core=False)
        manager.prime()
        time.sleep(0.5)
        say("\n  paired samples (ours vs the sensor server, same moment):")

        def server_temperatures() -> dict[str, float]:
            """LibreHardwareMonitor's temperature sensors, by name.

            By name, not by index: it creates one sensor per core before the
            package sensor, so `/intelcpu/0/temperature/0` is the *first core*,
            not the package. gpumon had been reading that index and calling it the
            CPU temperature, which is why our package reading looked 6 degrees
            cooler than "the same" sensor.
            """
            import json
            import urllib.request
            out: dict[str, float] = {}
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:8085/data.json", timeout=2.5) as response:
                    tree = json.loads(response.read().decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                return out

            def walk(node: dict) -> None:
                if (node.get("Type") == "Temperature"
                        and node.get("SensorId", "").startswith("/intelcpu")
                        and str(node.get("Value", "")).strip()):
                    text = str(node.get("Value")).split()[0]
                    try:
                        out[node.get("Text", "?")] = float(text)
                    except ValueError:
                        pass
                for child in node.get("Children") or ():
                    walk(child)

            walk(tree)
            return out
        # Idle is the worst case for this comparison: the package temperature
        # swings several degrees between turbo bursts, and the sensor server hands
        # out whatever it computed up to a second ago. Holding the CPU at a steady
        # load flattens both curves, so a disagreement means decoding rather than
        # timing.
        import threading
        stop = time.monotonic() + 26.0

        def spin() -> None:
            while time.monotonic() < stop:
                sum(i * i for i in range(20000))

        workers = [threading.Thread(target=spin, daemon=True)
                   for _ in range(max(2, (os.cpu_count() or 4) // 2))]
        for worker in workers:
            worker.start()
        say(f"    holding {len(workers)} threads busy for 26s...")
        time.sleep(9.0)                     # let the temperature plateau
        with pawnio.PawnIO() as pawn:
            # The module and the reader both follow the processor, not the other
            # way round: hardcoding Intel's here meant an AMD machine ended the
            # comparison with "the driver refused the module IntelMSR.bin".
            module, reader = "", None
            if identity.vendor == "amd":
                for name in CS.AMD_MODULES:
                    if pawn.load_module_file(pawnio.module_path(name)):
                        module, reader = name, CS.read_amd
                        break
            elif pawn.load_module_file(pawnio.module_path(CS.INTEL_MODULE)):
                module, reader = CS.INTEL_MODULE, CS.read_intel
            if reader is None:
                say(f"    could not reopen the module: {pawn.error}")
            else:
                say(f"    comparing with {module}")
                others = (("CPU Package", "Core (Tctl/Tdie)", "CPU Core"),
                          ("Core #1", "CCD1 (Tdie)")) if identity.vendor == "amd" \
                    else (("CPU Package",), ("P-Core #1", "CPU Core #1"))
                deltas = []
                for _ in range(6):
                    reading = reader(pawn)
                    theirs = server_temperatures()
                    package = next((theirs[n] for n in others[0] if n in theirs), None)
                    first_core = next((theirs[n] for n in others[1] if n in theirs), None)
                    core_zero = reading.per_core[0] if reading.per_core else None
                    if reading.package is not None and package is not None:
                        deltas.append(reading.package - package)
                    say(f"    package: ours {reading.package} C   "
                          f"theirs {package} C    |    core 1: ours {core_zero} C"
                          f"   theirs {first_core} C")
                    time.sleep(1.0)
                if deltas:
                    worst = max(abs(d) for d in deltas)
                    average = sum(deltas) / len(deltas)
                    say(f"    package: mean difference {average:+.1f} C, "
                          f"largest {worst:.1f} C")
                    say("    -> " + ("they agree" if worst <= 2.0 else
                                       "THEY DISAGREE - the decoding needs a look"))
        stop = time.monotonic()           # release the spinners
        for worker in workers:
            worker.join(timeout=1.0)
        manager.close()
    except Exception as exc:  # noqa: BLE001
        say(f"\n  (no comparison available: {exc})")
    return finish(0)


def _sensor_helper(argv: list[str]) -> int:
    """Run the elevated sensor reader.

    This is what the scheduled task starts: it opens the PawnIO driver, loads the
    signed module for this processor, and publishes the readings it decodes to a
    file the main program reads. It needs administrator rights, which the task
    supplies, and it stops by itself when the program that wanted it goes away.
    """
    import sensorhelper

    parent = 0
    hz = sensorhelper.DEFAULT_HZ
    seconds = None
    for index, item in enumerate(argv):
        if item == "--parent-pid" and index + 1 < len(argv):
            parent = int(argv[index + 1])
        elif item.startswith("--parent-pid="):
            parent = int(item.split("=", 1)[1])
        elif item == "--hz" and index + 1 < len(argv):
            hz = float(argv[index + 1])
        elif item.startswith("--hz="):
            hz = float(item.split("=", 1)[1])
        elif item == "--seconds" and index + 1 < len(argv):
            seconds = float(argv[index + 1])
        elif item.startswith("--seconds="):
            seconds = float(item.split("=", 1)[1])

    print(f"gpumon sensor helper: publishing to {sensorhelper.sensors_path()}")
    print(f"  {hz:g} Hz, stopping when no reader is left"
          + (f", parent process {parent}" if parent else "")
          + (f", lifetime {seconds:g}s" if seconds else ""))
    result = sensorhelper.run(parent_pid=parent, hz=hz, max_seconds=seconds)
    print(f"stopped after {result.iterations} readings "
          f"({result.published} published): {result.reason}")
    if result.error:
        print(f"last error: {result.error}")
    return 0 if result.published else 1


def _setup_sensors(argv: list[str]) -> int:
    """Run the sensor setup, leaving a log behind whatever happens.

    The setup runs in an elevated console that closes the moment it finishes, so
    a failure used to disappear with it - which is exactly how a setup that
    never happened looks identical to one that worked. Every line goes to
    sensors-setup.log as well as to the screen.
    """
    import sensorsetup

    log_path = apppaths.state_path("sensors-setup.log")
    log = open(log_path, "w", encoding="utf-8", errors="replace")

    class Tee:
        def __init__(self, stream, mirror) -> None:
            self.stream = stream
            self.mirror = mirror

        def write(self, text: str) -> int:
            self.mirror.write(text)
            self.mirror.flush()
            return self.stream.write(text)

        def flush(self) -> None:
            self.stream.flush()
            self.mirror.flush()

        def isatty(self) -> bool:
            return False

    original = sys.stdout
    sys.stdout = Tee(original, log)
    try:
        code = sensorsetup.setup(argv)
    finally:
        sys.stdout = original
        print(f"\n(transcript: {log_path})")
        log.close()
        # Leave the outcome where the program that raised this can read it: the
        # elevated window closes immediately, and in a packaged build there is no
        # console to print to at all.
        try:
            with open(sensorsetup.setup_marker(), "w", encoding="utf-8") as fh:
                fh.write(str(code))
        except OSError:
            pass
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gpumon",
        description="GPU and system performance logger with alarms and summaries.")
    parser.add_argument("--db", default=None,
                        help="SQLite database path (default: Documents\\gpumon\\sessions.db)")
    parser.add_argument("--hz", type=float, default=None,
                        help="samples per second (default 1.0)")
    parser.add_argument("--per-core", action="store_true",
                        help="also record per-core CPU utilisation")
    parser.add_argument("--version", action="version",
                        version=f"gpumon {SP.APP_VERSION}")
    parser.add_argument("--selftest", action="store_true",
                        help="run a headless verification of every layer")
    parser.add_argument("--setup-sensors", action="store_true",
                        help="fetch/launch LibreHardwareMonitor so CPU and AMD GPU "
                             "temperatures become readable")
    parser.add_argument("--msr-probe", action="store_true",
                        help="read the CPU's registers directly through the PawnIO "
                             "driver and report what they say (needs elevation)")
    parser.add_argument("--sensor-helper", action="store_true",
                        help="run the elevated sensor reader that publishes CPU "
                             "temperatures for the main program (used by its task)")
    parser.add_argument("--calibrate-gpus", action="store_true",
                        help="verify which utilisation counter belongs to which GPU "
                             "by loading one card at a time")
    parser.add_argument("--vram-report", action="store_true",
                        help="print every VRAM figure available from each backend")
    parser.add_argument("--vram-check", action="store_true",
                        help="measure real VRAM usage from the load/unload delta of "
                             "a model or workload")
    parser.add_argument("--web", action="store_true",
                        help="serve the browser GUI instead of the desktop window")
    parser.add_argument("--tui", action="store_true",
                        help="run the terminal interface instead of the desktop window")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address for --web (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080,
                        help="port for --web (default: 8080)")
    parser.add_argument("--list", action="store_true", help="list logged sessions")
    parser.add_argument("--report", type=int, metavar="SESSION_ID",
                        help="print a text report for one session")
    parser.add_argument("--theme", default=None, metavar="NAME",
                        help="colour theme (see --list-themes); saved to config.json")
    parser.add_argument("--list-themes", action="store_true",
                        help="list the available colour themes")
    parser.add_argument("--spawn", action="store_true",
                        help="start the desktop window detached and return; the "
                             "launcher uses this so closing its console cannot "
                             "close gpumon")
    args, extra = parser.parse_known_args(argv)

    # Word-form subcommands first: they set the same flags the parser does, so
    # every branch below stays unaware of them.
    stop, unclaimed = _resolve_subcommands(args, extra)
    if stop is not None:
        return stop

    if args.spawn:
        return spawn_detached(extra)

    if args.list_themes:
        return print_themes()

    # Resolved before any front end starts, so the desktop window, the terminal
    # view, the web GUI and the reports all see the same palette.
    if args.theme and not themes.normalize(args.theme):
        print(f"gpumon: unknown theme {args.theme!r}. Themes: "
              f"{', '.join(themes.keys())}", file=sys.stderr)
        return 2
    themes.set_theme(args.theme or _config().get("theme") or themes.DEFAULT_KEY)

    if args.sensor_helper:
        if _refuse_off_windows("--sensor-helper",
                               "it reads model-specific registers"):
            return 2
        return _sensor_helper(extra)

    if args.msr_probe:
        if _refuse_off_windows("--msr-probe",
                               "it reads model-specific registers"):
            return 2
        return _msr_probe()

    if args.setup_sensors:
        if _refuse_off_windows("--setup-sensors",
                              "it installs a sensor driver"):
            return 2
        # A Store build stops here rather than raising a prompt Windows would
        # refuse. The packaging script disables this path in the binary, which is
        # what the answer to Partner Center's driver question rests on.
        import storemode
        if storemode.IS_STORE_BUILD or apppaths.is_packaged():
            print(storemode.REFUSAL)
            return 2
        return _setup_sensors(extra)

    if args.calibrate_gpus:
        if _refuse_off_windows("--calibrate-gpus",
                              "it matches LUID performance counters to cards"):
            return 2
        import gpucalibrate
        return gpucalibrate.calibrate(extra)

    if args.vram_report:
        if _refuse_off_windows("--vram-report",
                              "it compares the Windows VRAM counters with ADL"):
            return 2
        return vram_report()

    if args.vram_check:
        if _refuse_off_windows("--vram-check",
                              "it measures VRAM through AMD's ADL"):
            return 2
        import vramcheck
        return vramcheck.main(extra)

    if args.web:
        import webserver
        return webserver.serve(args, extra)

    if args.selftest:
        return selftest()

    config = _config()
    if args.db is None:
        args.db = config.get("db_path") or S.default_db_path()
    if args.hz is None:
        args.hz = float(config.get("sample_hz", 1.0))

    # After the config load: the terminal view needs the same resolved database
    # path and sample rate as the desktop window.
    if args.tui:
        return launch_tui(args, config)

    if args.list:
        return list_sessions(args.db)
    if args.report is not None:
        return print_report(args.db, args.report)

    # Last stop before the desktop window: a window opened over an argument
    # nobody understood is the failure mode this guards.
    refusal = _reject_stray_arguments(unclaimed)
    if refusal is not None:
        return refusal

    try:
        return launch_gui(args, config)
    except Exception:  # noqa: BLE001 - surface the traceback in a dialog if possible
        traceback.print_exc()
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("gpumon failed to start",
                                 traceback.format_exc()[-2000:])
        except Exception:  # noqa: BLE001
            pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
