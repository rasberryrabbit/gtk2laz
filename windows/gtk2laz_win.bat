@echo off
REM ============================================================
REM  gtk2laz Windows launcher
REM  --------------------------------------------------------
REM  This batch file launches the bundled gtk2laz.pyz using a
REM  system Python 3.8+ install (Python must be on PATH).
REM
REM  If Python is not installed, download it from:
REM    https://www.python.org/downloads/
REM  (Make sure to check "Add Python to PATH" during install.)
REM
REM  Usage:
REM    gtk2laz_win.bat gui                          - launch GUI
REM    gtk2laz_win.bat c2pas INPUT_DIR OUTPUT_DIR   - C->Pascal only
REM    gtk2laz_win.bat convert INPUT_DIR OUTPUT_DIR - full GTK->Lazarus
REM    gtk2laz_win.bat list-todos OUTPUT_DIR        - dump TODOs as JSON
REM    gtk2laz_win.bat                              - show help
REM ============================================================
setlocal
cd /d "%~dp0"

REM Try python, then py launcher, then python3
where python >nul 2>&1 && set PY=python
if "%PY%"=="" where py >nul 2>&1 && set PY=py -3
if "%PY%"=="" where python3 >nul 2>&1 && set PY=python3

if "%PY%"=="" (
    echo ERROR: Python 3 was not found on PATH.
    echo Please install Python 3.8+ from https://www.python.org/downloads/
    echo and make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

%PY% gtk2laz.pyz %*
endlocal
