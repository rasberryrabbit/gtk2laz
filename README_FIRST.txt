gtk2laz - GTK to Lazarus converter
====================================

This package contains:

  linux-x86_64/gtk2laz      - Pre-built single-file executable for Linux x86-64
                              (no Python installation needed; ~16 MB)

  windows/gtk2laz.pyz       - Python zipapp for Windows (Python 3.8+ required)
  windows/gtk2laz_win.bat   - Launcher batch file for Windows
                              (Python must be on PATH; download from
                              https://www.python.org/downloads/ if missing)

  *.py                      - Source code (run with any Python 3.8+)
  *.spec, build_exe.sh      - PyInstaller build scripts (Linux)
  build_win_exe.bat         - PyInstaller build script (Windows; produces
                              a single gtk2laz.exe)

Quick start
-----------
  Linux:    ./linux-x86_64/gtk2laz gui
  Windows:  windows\gtk2laz_win.bat gui
  Source:   python3 gtk2laz.py gui

Subcommands
-----------
  gtk2laz gui                            Launch the interactive GUI
  gtk2laz convert INPUT OUTPUT           Full GTK -> Lazarus conversion
  gtk2laz c2pas INPUT OUTPUT             C -> Pascal only (skip Glade)
  gtk2laz list-todos OUTPUT              Print all TODO markers as JSON

See README.md for full documentation.

The opgui_lazarus/ directory (next to this package) is a demo output
produced by running:
  gtk2laz convert /path/to/opgui-0.12.4 opgui_lazarus

It contains 31 Pascal units (15 from headers + 15 from C files + 1 form)
and a todos.json listing all 1136 TODO markers that need manual review.
