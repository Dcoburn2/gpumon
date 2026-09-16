@echo off
REM Build gpumon.exe - a real application, not a script wrapper.
REM
REM PyInstaller bundles the interpreter, psutil and gpumon's own modules into
REM dist\gpumon\gpumon.exe, with the monster icon baked in. It is a one-folder
REM build on purpose: one-file mode unpacks ~20 MB to a temporary directory on
REM every launch, which costs seconds each time for no benefit here.
REM
REM PyInstaller is a build tool only. The app itself still needs nothing but
REM psutil at run time, and this script installs PyInstaller into the user's
REM Python if it is missing.
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

%PY% -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo   Installing PyInstaller ^(build-time only^)...
    %PY% -m pip install --quiet pyinstaller
    %PY% -c "import PyInstaller" >nul 2>&1
    if errorlevel 1 (
        echo   Could not install PyInstaller. Run: %PY% -m pip install pyinstaller
        pause
        exit /b 1
    )
)

echo   Regenerating the icon...
%PY% make_icon.py || goto :failed

echo   Building dist\gpumon\gpumon.exe ...
REM --windowed drops the console window; the app logs to gpumon-launch.log
REM beside the exe when something goes wrong before the window exists.
%PY% -m PyInstaller --noconfirm --clean --windowed --onedir --name gpumon ^
    --icon gpumon.ico ^
    --add-data "gpumon.ico;." ^
    --add-data "gpumon.png;." ^
    --hidden-import psutil ^
    --hidden-import wmi ^
    --exclude-module tkinter.test ^
    --exclude-module test ^
    gpumon.py || goto :failed

echo.
echo   Built %~dp0dist\gpumon\gpumon.exe
echo   Double-click it, or run make_shortcut.cmd to put it on the desktop.
echo.
exit /b 0

:failed
echo.
echo   Build failed. The output above says why.
pause
exit /b 1
