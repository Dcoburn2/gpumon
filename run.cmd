@echo off
REM Internal helper: runs gpumon.py and reports a non-zero exit.
REM
REM Exists so the launcher can forward an argument list of unknown length. A
REM batch file cannot shift its own %1 away and then use %* (shift does not
REM affect %*), and spelling out %2 %3 ... %9 duplicates %1 and leaves the
REM unset ones on the command line as literal text - which made the launcher
REM appear to hang, because gpumon.py then received junk arguments and tried to
REM start the desktop window.
setlocal
cd /d "%~dp0"
%*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo   gpumon exited with code %CODE%.
    echo.
    pause
)
endlocal & exit /b %CODE%
