@echo off
REM ============================================================
REM  build_win_exe.bat
REM  --------------------------------------------------------
REM  Build a single-file gtk2laz.exe for Windows using PyInstaller.
REM
REM  Requirements:
REM    - Python 3.8+ from https://www.python.org/downloads/
REM      (Make sure "tcl/tk and IDLE" is checked in the installer -
REM      it is by default - so the GUI works.)
REM    - PyInstaller:  pip install pyinstaller
REM
REM  Output:
REM    dist\gtk2laz.exe   (single-file, ~16 MB)
REM
REM  After building, you can run:
REM    dist\gtk2laz.exe gui
REM    dist\gtk2laz.exe c2pas input_dir output_dir
REM    dist\gtk2laz.exe convert input_dir output_dir
REM    dist\gtk2laz.exe list-todos output_dir
REM ============================================================
setlocal
cd /d "%~dp0"

REM Ensure pyinstaller is available
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found. Installing...
    python -m pip install pyinstaller
)

REM Clean previous build artifacts
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

REM Build using the spec file
python -m PyInstaller gtk2laz.spec --noconfirm --clean
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

echo.
echo Build OK: dist\gtk2laz.exe
dist\gtk2laz.exe --version

echo.
echo CLI help:
dist\gtk2laz.exe --help

echo.
echo Smoke test - c2pas on a tiny C file:
set TMPDIR=%TEMP%\gtk2laz_smoke
if exist "%TMPDIR%" rmdir /s /q "%TMPDIR%"
mkdir "%TMPDIR%\src" "%TMPDIR%\out"
(
    echo #include ^<stdio.h^>
    echo int add(int a, int b^) {
    echo     return a + b;
    echo }
    echo void greet(const char *name^) {
    echo     printf("Hello, %%s!\n", name^);
    echo }
) > "%TMPDIR%\src\hello.c"

dist\gtk2laz.exe c2pas "%TMPDIR%\src" "%TMPDIR%\out"
echo Output dir contents:
dir /b "%TMPDIR%\out"
echo First 20 lines of hello.pas:
powershell -Command "Get-Content -Path '%TMPDIR%\out\hello.pas' -TotalCount 20"

rmdir /s /q "%TMPDIR%"
echo.
echo Build ^& smoke test complete.
endlocal
