@echo off
REM Put gpumon on the desktop, and optionally in the Start menu.
REM
REM Usage:  make_shortcut.cmd [start]
REM
REM The shortcut points at dist\gpumon\gpumon.exe and carries the monster icon,
REM so Windows shows the app the way it shows any other application. It is a
REM plain .lnk: delete it and nothing else changes.
setlocal EnableExtensions
cd /d "%~dp0"

set "EXE=%~dp0dist\gpumon\gpumon.exe"
if not exist "%EXE%" (
    echo.
    echo   dist\gpumon\gpumon.exe does not exist yet.
    echo   Build it first:  build_exe.cmd
    echo.
    pause
    exit /b 1
)

set "WHERE=Desktop"
if /I "%~1"=="start" set "WHERE=StartMenu"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$exe = '%EXE%'; $ico = '%~dp0gpumon.ico';" ^
  "$dir = if ('%WHERE%' -eq 'StartMenu') { Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs' } else { [Environment]::GetFolderPath('Desktop') };" ^
  "$lnk = Join-Path $dir 'gpumon.lnk';" ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk);" ^
  "$s.TargetPath = $exe; $s.WorkingDirectory = (Split-Path $exe);" ^
  "$s.IconLocation = \"$ico,0\";" ^
  "$s.Description = 'gpumon - GPU and system telemetry';" ^
  "$s.Save(); Write-Host \"  Created $lnk\""

if errorlevel 1 (
    echo.
    echo   Could not create the shortcut.
    echo.
    pause
    exit /b 1
)
echo.
exit /b 0
