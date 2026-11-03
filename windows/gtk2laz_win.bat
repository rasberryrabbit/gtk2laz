@echo off
REM ============================================================
REM  gtk2laz Windows launcher (Option C — uses Python sources directly)
REM  --------------------------------------------------------
REM  This batch file launches gtk2laz using the Python source
REM  modules shipped alongside it (gtk2laz.py + the *.py modules
REM  in the parent folder), so it always picks up the LATEST
REM  bug-fixed converter code.  A system Python 3.8+ install is
REM  required (Python must be on PATH).
REM
REM  This launcher does NOT depend on gtk2laz.pyz — it runs
REM  ``python gtk2laz.py`` directly so any source-level fix you
REM  drop into the parent folder is effective immediately without
REM  needing to rebuild the .pyz bundle.
REM
REM  If Python is not installed, download it from:
REM    https://www.python.org/downloads/
REM  (Make sure to check "Add Python to PATH" during install.)
REM
REM  Folder layout expected by this launcher:
REM    gtk2laz_fixed/
REM      gtk2laz.py            <-- main entry point (run by this .bat)
REM      c_parser.py           <-- pattern-based translator
REM      ast_translator.py     <-- AST-based translator
REM      widget_map.py
REM      glade_parser.py
REM      gtk2laz_gui.py
REM      pcpp_preprocessor.py
REM      gen_fake_includes.py
REM      diagnose.py
REM      ply/                  <-- bundled ply (no pip install needed)
REM      windows/
REM        gtk2laz_win.bat     <-- this file
REM
REM  Usage (run from any folder; the .bat changes to its own
REM  parent folder automatically):
REM    gtk2laz_win.bat gui                          - launch GUI
REM    gtk2laz_win.bat c2pas INPUT_DIR OUTPUT_DIR   - C->Pascal only
REM    gtk2laz_win.bat convert INPUT_DIR OUTPUT_DIR - full GTK->Lazarus
REM    gtk2laz_win.bat list-todos OUTPUT_DIR        - dump TODOs as JSON
REM    gtk2laz_win.bat diagnose                     - run diagnostics
REM    gtk2laz_win.bat                              - show help
REM ============================================================
setlocal

REM ---- Resolve the parent directory of this .bat (the gtk2laz_fixed folder)
REM  %~dp0 expands to the directory of the current batch file, with a
REM  trailing backslash.  We use ``pushd`` so the cwd is restored later.
pushd "%~dp0\.."
set "GTK2LAZ_HOME=%CD%"
popd

REM ---- Find Python on PATH (try python, then py -3, then python3)
set "PY="
where python >nul 2>&1 && set "PY=python"
if "%PY%"=="" where py >nul 2>&1 && set "PY=py -3"
if "%PY%"=="" where python3 >nul 2>&1 && set "PY=python3"

if "%PY%"=="" (
    echo ERROR: Python 3 was not found on PATH.
    echo Please install Python 3.8+ from https://www.python.org/downloads/
    echo and make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

REM ---- Sanity check: make sure gtk2laz.py is present in the home folder.
if not exist "%GTK2LAZ_HOME%\gtk2laz.py" (
    echo ERROR: gtk2laz.py not found in "%GTK2LAZ_HOME%".
    echo This launcher expects to sit in a "windows\" subfolder of the
    echo gtk2laz distribution.  Please move gtk2laz_win.bat back into
    echo the windows\ subfolder and try again.
    pause
    exit /b 1
)

REM ---- Run the converter using the Python sources directly.
REM  We pass ``%*`` so all command-line arguments are forwarded as-is.
REM  The python -X utf8 flag ensures UTF-8 stdout/stderr on Windows
REM  (avoids mojibake for non-ASCII identifiers in the input source).
%PY% -X utf8 "%GTK2LAZ_HOME%\gtk2laz.py" %*
set "EXITCODE=%ERRORLEVEL%"

endlocal & exit /b %EXITCODE%
