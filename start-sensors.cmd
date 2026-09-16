@echo off
REM Give gpumon the CPU temperature, which needs a kernel driver.
REM
REM Runs the sensor setup with administrator rights - one Windows prompt. The
REM elevation cannot be avoided: the driver that reads the CPU's thermal MSRs
REM only loads for an administrator, and gpumon deliberately installs no driver
REM of its own. LibreHardwareMonitor ends up minimised in the notification area.
REM
REM The elevated window stays open when it finishes, so its output can be read,
REM and everything it printed is also saved to sensors-setup.log.
setlocal EnableExtensions
cd /d "%~dp0"

set "PY="
py -3 --version >nul 2>&1
if not errorlevel 1 (set "PY=py -3") else (
    python --version >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo   Python was not found on PATH.
    pause
    exit /b 1
)

echo.
echo   Asking Windows for administrator rights - accept the prompt that follows.
echo   The elevated window does the work and then waits for a key press, so you
echo   can read what it did. The same text is saved to sensors-setup.log.
echo.

if "%PY%"=="py -3" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "Start-Process -FilePath 'cmd.exe' -ArgumentList '/c','py -3 gpumon.py --setup-sensors ^& echo. ^& pause' -Verb RunAs -WorkingDirectory '%CD%' -Wait"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "Start-Process -FilePath 'cmd.exe' -ArgumentList '/c','python gpumon.py --setup-sensors ^& echo. ^& pause' -Verb RunAs -WorkingDirectory '%CD%' -Wait"
)

echo.
if exist "%~dp0sensors-setup.log" (
    echo   Last lines of sensors-setup.log:
    echo   ------------------------------------------------------------
    powershell -NoProfile -Command "Get-Content '%~dp0sensors-setup.log' -Tail 12"
    echo   ------------------------------------------------------------
)
echo.
pause
exit /b 0
