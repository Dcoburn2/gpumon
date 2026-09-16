#!/usr/bin/env bash
# gpumon launcher (Linux / macOS).
#
# Usage:
#   ./gpumon            live desktop window
#   ./gpumon tui        terminal view
#   ./gpumon web        browser GUI on http://127.0.0.1:8080
#   ./gpumon report 3   text report for session 3
#   ./gpumon help       usage
#   ./gpumon <flags>    passed straight through to gpumon.py
#
# The shorthand words are understood by gpumon.py itself, so they work in any
# position and behave identically to the Windows launcher. Nothing is rewritten
# here: an argument this script mangles is an argument the user cannot debug.
set -u
cd "$(dirname "$(readlink -f "$0")")"

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
if [ -z "$PY" ]; then
    echo "Python 3.10+ was not found on PATH." >&2
    exit 1
fi

# psutil is the only hard dependency.
if ! "$PY" -c "import psutil" >/dev/null 2>&1; then
    echo "Installing psutil (needed for CPU and RAM metrics)..."
    if ! "$PY" -m pip install --quiet psutil; then
        echo "Could not install psutil. Run: $PY -m pip install psutil" >&2
        exit 1
    fi
fi

# The desktop window needs tkinter, which some distributions ship separately.
exec "$PY" gpumon.py "$@"
