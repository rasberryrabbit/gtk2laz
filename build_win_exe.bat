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
REM    - pycparser:    pip install pycparser
REM    - pcpp:         pip install pcpp
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

echo ============================================================
echo  gtk2laz Windows Build Script
echo ============================================================
echo.

REM Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found on PATH.
    echo Please install Python 3.8+ from https://www.python.org/downloads/
    echo and make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

echo [1/5] Checking Python...
python --version
echo.

REM Install required packages
echo [2/5] Installing dependencies...
python -m pip install --upgrade pip pyinstaller pycparser ply pcpp 2>nul
if errorlevel 1 (
    echo WARNING: Some packages may not have installed correctly.
    echo Trying to continue anyway...
)
echo.

REM Verify ply is actually installed (critical for AST)
echo --- Verifying ply installation ---
python -c "import ply; import ply.lex; import ply.yacc; print('ply OK:', ply.__file__)" 2>nul
if errorlevel 1 (
    echo ============================================================
    echo  ERROR: ply package is NOT installed!
    echo ============================================================
    echo  ply is required for AST-based C parsing.
    echo  Without it, you will get 1136 TODOs instead of ~144.
    echo.
    echo  Install it manually:
    echo    pip install ply
    echo.
    echo  Then re-run this script.
    echo ============================================================
    pause
    exit /b 1
)
echo.

REM Clean previous build artifacts completely
echo [3/5] Cleaning previous build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist __pycache__ rmdir /s /q __pycache__
REM Clear PyInstaller cache to avoid stale builds
if exist "%APPDATA%\pyinstaller" rmdir /s /q "%APPDATA%\pyinstaller"
echo Cleaned.
echo.

REM Build using the spec file
echo [4/5] Building gtk2laz.exe with PyInstaller...
python -m PyInstaller gtk2laz.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo ============================================================
    echo  BUILD FAILED
    echo ============================================================
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Build OK: dist\gtk2laz.exe
echo ============================================================
echo.

REM Verify the build
echo [5/5] Verifying build...
echo.
echo --- Version ---
dist\gtk2laz.exe --version
if errorlevel 1 (
    echo ERROR: gtk2laz.exe failed to run.
    pause
    exit /b 1
)

echo.
echo --- Checking AST modules are bundled ---
dist\gtk2laz.exe c2pas --help 2>nul
echo.

REM Verify AST translator is working by testing a small file
echo --- AST verification test ---
set "TMPDIR2=%TEMP%\gtk2laz_ast_test"
if exist "%TMPDIR2%" rmdir /s /q "%TMPDIR2%"
mkdir "%TMPDIR2%\src" "%TMPDIR2%\out"
(
    echo int add(int a, int b^) {
    echo     int sum = a + b;
    echo     for (int i = 0; i ^< 10; i++^) {
    echo         sum += i;
    echo     }
    echo     return sum;
    echo }
) > "%TMPDIR2%\src\test.c"

dist\gtk2laz.exe c2pas "%TMPDIR2%\src" "%TMPDIR2%\out" 2>nul
echo.
echo --- Generated test.pas ---
type "%TMPDIR2%\out\test.pas"
echo.

REM Check if AST was actually used (look for proper for-loop translation)
echo --- Checking if AST was used ---
findstr /C:"for i" "%TMPDIR2%\out\test.pas" >nul 2>&1
if errorlevel 1 (
    findstr /C:"while" "%TMPDIR2%\out\test.pas" >nul 2>&1
    if errorlevel 1 (
        echo ============================================================
        echo  ERROR: AST translation is NOT working!
        echo ============================================================
        echo The output does not contain for/while loops.
        echo This means pycparser or ply is not bundled correctly.
        echo.
        echo Try:
        echo   pip install --force-reinstall pycparser ply pcpp
        echo   rmdir /s /q build dist __pycache__
        echo   rmdir /s /q "%%APPDATA%%\pyinstaller"
        echo   build_win_exe.bat
        echo ============================================================
        rmdir /s /q "%TMPDIR2%"
        pause
        exit /b 1
    )
)
echo AST translation: OK
rmdir /s /q "%TMPDIR2%"
echo.

echo --- CLI help ---
dist\gtk2laz.exe --help
echo.

REM Smoke test - c2pas on a tiny C file
echo --- Smoke test: c2pas on a tiny C file ---
set "TMPDIR=%TEMP%\gtk2laz_smoke"
if exist "%TMPDIR%" rmdir /s /q "%TMPDIR%"
mkdir "%TMPDIR%\src" "%TMPDIR%\out"

REM Create a test C file
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
if errorlevel 1 (
    echo ERROR: Smoke test failed.
    rmdir /s /q "%TMPDIR%"
    pause
    exit /b 1
)

echo.
echo Output files:
dir /b "%TMPDIR%\out"
echo.
echo --- hello.pas (first 20 lines) ---
powershell -Command "Get-Content -Path '%TMPDIR%\out\hello.pas' -TotalCount 20"

rmdir /s /q "%TMPDIR%"
echo.
echo ============================================================
echo  Build ^& smoke test complete!
echo.
echo  The executable is at: dist\gtk2laz.exe
echo  Run it with: dist\gtk2laz.exe gui
echo ============================================================
endlocal
