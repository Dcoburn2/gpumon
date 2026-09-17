"""Verify the launchers actually work, from a shell, as a user would run them.

The .cmd launcher is easy to get subtly wrong. It once forwarded arguments as
`%2 %3 ... %9`, which duplicated `%1` and left the unset ones on the command
line as literal text, so `gpumon list` fell through to the desktop window and
looked like a hang. It then rewrote `%1` into a flag, which meant the shorthand
only worked in first position: `gpumon --db other.db report 3` left "report 3"
unparsed, and unparsed arguments started the desktop window - the same hang
again, one layer down.

Both are fixed by forwarding `%*` verbatim and letting gpumon.py resolve the
shorthand words in any position. Unrecognised arguments must now fail loudly
instead of quietly starting a window, which is what most of this file checks:
every case runs in a subprocess with a timeout, so a hang fails rather than
blocking the suite.
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





import os
import re
import subprocess
import sys
import time

problems: list[str] = []
HERE = _REPO          # the repository root, where the launchers are


def check(label: str, ok: bool, detail: str = "") -> None:
    text = f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}"
    print(text.encode("ascii", "replace").decode("ascii"))
    if not ok:
        problems.append(label)


def run_launcher(args: str, timeout: float = 60.0) -> tuple[bool, str, int]:
    """Run the launcher through cmd.exe. Returns (finished, output, exit code).

    Passed as a single command string: cmd strip-quotes a quoted first token, so
    handing it an argument list turns the path into a literal command name.
    """
    script = os.path.join(HERE, "gpumon.cmd")
    command = f'call "{script}" {args}'
    try:
        result = subprocess.run(command, shell=True, cwd=HERE, timeout=timeout,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True,
                                errors="replace")
        return True, result.stdout, result.returncode
    except subprocess.TimeoutExpired as exc:
        captured = exc.output or ""
        if isinstance(captured, bytes):
            captured = captured.decode("utf-8", "replace")
        return False, captured, -1


def run_python(args: str, timeout: float = 60.0) -> tuple[bool, str, int]:
    """Same, straight against gpumon.py: the launcher must not be the only path."""
    command = f'"{sys.executable}" gpumon.py {args}'
    try:
        result = subprocess.run(command, shell=True, cwd=HERE, timeout=timeout,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True,
                                errors="replace")
        return True, result.stdout, result.returncode
    except subprocess.TimeoutExpired as exc:
        captured = exc.output or ""
        if isinstance(captured, bytes):
            captured = captured.decode("utf-8", "replace")
        return False, captured, -1


print("=" * 84)
print("LAUNCHER TEST")
print("=" * 84)

print("\n[1] launcher files")
for name in ("gpumon.cmd", "run.cmd", "gpumon.sh"):
    check(f"{name} exists", os.path.exists(os.path.join(HERE, name)))
check("run.bat is gone", not os.path.exists(os.path.join(HERE, "run.bat")))

cmd_text = open(os.path.join(HERE, "gpumon.cmd"), encoding="utf-8",
                errors="replace").read()
run_text = open(os.path.join(HERE, "run.cmd"), encoding="utf-8",
                errors="replace").read()
sh_text = open(os.path.join(HERE, "gpumon.sh"), encoding="utf-8",
               errors="replace").read()
def code_lines(text: str) -> str:
    """The executable part of a batch file, with comments removed.

    The old bug is described in the header comments of these files, so a check
    for `%2 %3` has to look at code only or it flags its own documentation.
    """
    keep = [line for line in text.splitlines()
            if not line.strip().upper().startswith("REM")]
    return "\n".join(keep)


check("no %2..%9 argument forwarding (the original bug)",
      not re.search(r"%2\s+%3", code_lines(cmd_text)))
check("launcher forwards the whole argument list", "%*" in cmd_text)
check("launcher cd's to its own directory", 'cd /d "%~dp0"' in cmd_text)
check("launcher pauses on a Python-not-found error", "pause" in cmd_text)
check("launcher finds Python via py and python", "py -3" in cmd_text
      and "python --version" in cmd_text)
check("helper forwards the argument list unchanged", "%*" in code_lines(run_text))
check("helper pauses on a non-zero exit", "pause" in run_text)
check("shell launcher forwards its arguments", '"$@"' in sh_text)
check("the .cmd no longer rewrites arguments into flags",
      'set "ARGS=' not in cmd_text)
check("the .cmd ends in one plain forwarding call",
      'call "%~dp0run.cmd" %PY% gpumon.py %*' in cmd_text)

print("\n[2] modes that must finish on their own")
# The session listing prints a table whose header is "id started duration
# samples alarms label" and prints "No sessions logged yet" when there are none.
# Matching on the word "session" passed only while the database was empty - the
# moment a real session existed the check failed on correct output.
cases = [
    ("help", "gpumon - GPU and system telemetry"),
    ("list", "duration"),
    ("--list", "duration"),
    ("sessions", "duration"),
    ("--selftest", "gpumon self-test"),
    ("selftest", "gpumon self-test"),
]
for args, expected in cases:
    finished, output, code = run_launcher(args, timeout=90)
    if not finished:
        check(f"`gpumon {args}` finishes", False, "timed out (looks like a hang)")
        continue
    check(f"`gpumon {args}` finishes", True, f"exit {code}")
    ok = expected in output or "No sessions logged yet" in output \
        or "SELF-TEST PASSED" in output
    check(f"`gpumon {args}` produced expected output", ok,
          (output.strip().splitlines() or [""])[0][:60])

print("\n[3] an unknown argument must fail, not open a window")
finished, output, code = run_launcher("not-a-real-mode", timeout=30)
if not finished:
    check("`gpumon not-a-real-mode` fails instead of opening a window", False,
          "timed out - it started the desktop window instead")
else:
    check("`gpumon not-a-real-mode` fails instead of opening a window", True,
          f"exit {code} after {len(output)} bytes of output")
    check("`gpumon not-a-real-mode` exits non-zero", code != 0, f"exit {code}")
    check("`gpumon not-a-real-mode` says what was wrong",
          "unrecognised" in output.lower(),
          (output.strip().splitlines() or [""])[0][:60])
    check("`gpumon not-a-real-mode` does not claim to have started something",
          "Nothing was started" in output)

finished, output, code = run_python("nonsense-flag", timeout=30)
check("direct gpumon.py call rejects unknown words too", finished and code != 0,
      f"exit {code}")

print("\n[4] report needs an argument and says so")
finished, output, code = run_launcher("report", timeout=30)
check("`gpumon report` finishes", finished)
check("`gpumon report` asks for a session number",
      "NUMBER" in output or "number" in output, output.strip()[:70])
check("`gpumon report` exits non-zero", code != 0, f"exit {code}")

print("\n[5] report with a real session id, in every position")
import metrics as M
import sampler as SP
import store as S

DB = "launchertest.db"
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(os.path.join(HERE, DB + suffix)):
        os.remove(os.path.join(HERE, DB + suffix))
store = S.Store(os.path.join(HERE, DB))
manager = M.SensorManager(per_core=False)
sampler = SP.Sampler(manager, store, sample_hz=2.0)
sampler.start()
sampler.start_logging("launcher test")
time.sleep(4)
summary = sampler.stop_logging()
sampler.stop()
store.close()
manager.close()
session_id = summary.session_id if summary else 1
check("fixture session created", session_id >= 1, f"#{session_id}")

for args in (f"--db {DB} report {session_id}",
             f"--db {DB} --report {session_id}",
             f"report {session_id} --db {DB}"):
    finished, output, code = run_launcher(args, timeout=60)
    if not finished:
        check(f"`gpumon {args}` finishes", False, "timed out - it opened a window")
        continue
    check(f"`gpumon {args}` finishes", code == 0, f"exit {code}")
    check(f"`gpumon {args}` prints a report", "gpumon report" in output,
          (output.strip().splitlines() or [""])[0][:60])

print("\n[6] the same arguments reach gpumon.py with no duplication")
finished, output, code = run_launcher("--db launchertest.db --list", timeout=60)
check("`gpumon --db X --list` finishes", finished and code == 0, f"exit {code}")
check("forwarded args are not duplicated",
      output.count("usage:") == 0 and "No sessions" not in output,
      "listed the session with its own db"
      if "launcher test" in output else output.strip()[:60])
check("the named database was used", "launcher test" in output,
      output.strip().splitlines()[-1][:60] if output.strip() else "no output")

finished, output, code = run_launcher("--db launchertest.db list", timeout=60)
check("a shorthand after a flag still uses that flag",
      finished and "launcher test" in output, output.strip()[:60])

for suffix in ("", "-wal", "-shm"):
    path = os.path.join(HERE, DB + suffix)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass

print("\n[7] the desktop window is detached from the console")
# Two distinct requirements: the launcher must start the window in a process
# that outlives whatever console launched it, and it must NOT do that for a mode
# that needs the console.
#
# `start "" pythonw` was the first attempt and was not enough: it left the app a
# child of that console, so a shell that kills its process tree took gpumon with
# it, and the fallback to console python (when pythonw.exe was missing) kept a
# console window of its own. The spawn is done by gpumon.py now, with
# DETACHED_PROCESS and CREATE_BREAKAWAY_FROM_JOB.
gpumon_src = open(os.path.join(HERE, "gpumon.py"), encoding="utf-8",
                  errors="replace").read()
check("the launcher asks gpumon.py to spawn the window",
      "--spawn" in cmd_text)
check("the launcher no longer relies on `start`", 'start ""' not in cmd_text)
check("the spawn detaches the process from any console",
      "DETACHED_PROCESS" in gpumon_src)
check("the spawn attempts to break away from a job object",
      "CREATE_BREAKAWAY_FROM_JOB" in gpumon_src)
check("the spawn falls back rather than refusing to start",
      gpumon_src.count("DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP") >= 1)
check("the spawned app logs where a windowless launch has no console",
      "LAUNCH_LOG" in gpumon_src)
check("run.cmd is only used for console modes",
      "call \"%~dp0run.cmd\" %PY% gpumon.py %*" in cmd_text)

# Behaviour: no arguments must return at once and leave the app running. Output
# goes to DEVNULL rather than a pipe on purpose - a captured pipe stays open
# while the detached app holds it, which would make this look like a hang.
import subprocess as _sp


def windowless_pids() -> set[int]:
    """PIDs of running pythonw processes, which is what a detached launch is."""
    listing = _sp.run(["tasklist", "/FI", "IMAGENAME eq pythonw.exe", "/FO", "CSV"],
                      capture_output=True, text=True, errors="replace",
                      creationflags=0x08000000).stdout
    found = set()
    for line in listing.splitlines()[1:]:
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[1].isdigit():
            found.add(int(parts[1]))
    return found


before = windowless_pids()
try:
    _sp.run(f'call "{os.path.join(HERE, "gpumon.cmd")}"', shell=True, cwd=HERE,
            timeout=60, stdin=_sp.DEVNULL, stdout=_sp.DEVNULL,
            stderr=_sp.DEVNULL)
    finished = True
except _sp.TimeoutExpired:
    finished = False
check("`gpumon` returns without waiting for the window", finished,
      "timed out - it waited for the app" if not finished else "returned")
time.sleep(5)
started = windowless_pids() - before
print(f"    windowless processes started: {sorted(started)}")
check("the desktop window was started in the background", bool(started),
      str(sorted(started)))

# Closing the console must not take the app with it. The windowless interpreter
# has no console of any kind, which is the mechanism that makes this true; the
# check is that the process is still there after its launcher is gone (the
# launcher already exited above).
time.sleep(2)
check("the window outlives the console that launched it",
      bool(started & windowless_pids()), str(sorted(started)))

for pid in sorted(started):
    _sp.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
            creationflags=0x08000000)
time.sleep(0.5)
check("the test cleaned up the window it started",
      not (started & windowless_pids()), str(sorted(started & windowless_pids())))

print("\n" + "=" * 84)
if problems:
    print(f"LAUNCHER TEST FAILED ({len(problems)})")
    for p in problems:
        print(f"  - {p}")
else:
    print("LAUNCHER TEST PASSED")
print("=" * 84)
raise SystemExit(1 if problems else 0)
