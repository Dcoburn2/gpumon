@echo off
REM gpumon launcher (Windows).
REM
REM Usage:
REM   gpumon              live desktop window (detached: this console closes)
REM   gpumon tui          terminal view
REM   gpumon web          browser GUI on http://127.0.0.1:8080
REM   gpumon report 3     text report for session 3
REM   gpumon help         usage
REM   gpumon <flags>      passed straight through to gpumon.py
REM
REM Two things this file has learned the hard way.
REM
REM 1. Arguments are forwarded verbatim as %*. An earlier version rewrote %1
REM    into a flag and forwarded the rest as `%2 %3 ... %9`, which duplicated %1
REM    and left the unset ones on the command line as literal text, so
REM    `gpumon list` fell through to the desktop window and looked like a hang.
REM    The shorthand words are understood by gpumon.py itself now, in any
REM    position, on both platforms.
REM
REM 2. The desktop window is detached. Started normally it was a child of this
REM    console, so closing the black box killed the app. GUI launches now go
REM    through pythonw.exe via `start`, which leaves the app running with no
REM    console at all. Modes that need a console to talk to you stay attached,
REM    and so does anything this script does not positively recognise - an
REM    unknown argument must fail where you can see it, not in a log file.
setlocal EnableExtensions
cd /d "%~dp0"

REM --- decide: detach, or stay attached? -----------------------------------
REM Only the flags that configure the desktop window are accepted here. A value
REM following --hz/--db/--theme is skipped rather than matched, so `--db list`
REM is not mistaken for the `list` command.
set "DETACH="
if "%~1"=="" set "DETACH=1"
set "EXPECT_VALUE="
for %%A in (%*) do (
    if defined EXPECT_VALUE (
        set "EXPECT_VALUE="
    ) else (
        set "KNOWN="
        if /I "%%~A"=="gui" set "KNOWN=1"
        if /I "%%~A"=="desktop" set "KNOWN=1"
        if /I "%%~A"=="window" set "KNOWN=1"
        if /I "%%~A"=="--per-core" set "KNOWN=1"
        if /I "%%~A"=="--hz" set "KNOWN=1" & set "EXPECT_VALUE=1"
        if /I "%%~A"=="--db" set "KNOWN=1" & set "EXPECT_VALUE=1"
        if /I "%%~A"=="--theme" set "KNOWN=1" & set "EXPECT_VALUE=1"
        if not defined KNOWN set "DETACH="
    )
)

REM --- locate Python -------------------------------------------------------
REM `py` is the Windows launcher and is present even when python.exe is only
REM reachable through the Microsoft Store alias, so try it first.
set "PY="
py -3 --version >nul 2>&1
if not errorlevel 1 (
    set "PY=py -3"
) else (
    python --version >nul 2>&1
    if not errorlevel 1 set "PY=python"
)

if not defined PY (
    echo.
    echo   gpumon could not find Python.
    echo.
    echo   Install Python 3.10 or newer from https://www.python.org/downloads/
    echo   and tick "Add python.exe to PATH" during setup.
    echo.
    echo   If Python is already installed, run this from a terminal to see why:
    echo       py -3 --version
    echo       python --version
    echo.
    pause
    exit /b 1
)

REM --- psutil is the only dependency ---------------------------------------
%PY% -c "import psutil" >nul 2>&1
if errorlevel 1 (
    echo   Installing the psutil dependency ^(needed for CPU and RAM metrics^)...
    %PY% -m pip install --quiet psutil
    %PY% -c "import psutil" >nul 2>&1
    if errorlevel 1 (
        echo.
        echo   Could not install psutil automatically.
        echo   Run this by hand and try again:
        echo       %PY% -m pip install psutil
        echo.
        pause
        exit /b 1
    )
)

if defined DETACH goto :detach

REM --- attached: modes that need this console ------------------------------
REM run.cmd owns the exit code and pauses if it is non-zero, so a
REM double-clicked console stays open long enough to be read.
call "%~dp0run.cmd" %PY% gpumon.py %*
exit /b %ERRORLEVEL%

:detach
REM --- detached: the desktop window ---------------------------------------
REM gpumon.py does the spawning itself, through CreateProcess with
REM DETACHED_PROCESS and CREATE_BREAKAWAY_FROM_JOB. `start` alone was not
REM enough: it left the app as a child of this console, so a shell that kills
REM its process tree - Windows Terminal, an IDE terminal, a job object - took
REM gpumon with it; and falling back to console python when pythonw.exe could
REM not be found kept a console window of its own. This path gives the app no
REM console at all, whichever interpreter ends up running it.
echo.
%PY% "%~dp0gpumon.py" --spawn %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo   gpumon could not be started ^(code %CODE%^).
    echo.
    pause
)
exit /b %CODE%
