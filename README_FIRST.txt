gtk2laz v2.2 - GTK to Lazarus converter (Stage 2 AST translation)
==================================================================

NEW in v2.2:
  * AST-based body translation (Stage 2) - 47% fewer TODOs!
  * TODO markers now include the original C source as a comment block

This package contains:

  linux-x86_64/gtk2laz      Pre-built single-file executable for Linux x86-64
                            (includes pycparser + Tcl/Tk runtime; ~16 MB)
  windows/gtk2laz.pyz       Python zipapp for Windows (Python 3.8+ required)
  windows/gtk2laz_win.bat   Launcher batch file for Windows
  *.py                      Source code (run with any Python 3.8+)
  ast_translator.py         pycparser-based AST translator (Stage 1 + 2)
  gen_fake_includes.py      Generates stub libc/GTK headers for pycparser

Quick start
-----------
  Linux:    ./linux-x86_64/gtk2laz gui
  Windows:  windows\gtk2laz_win.bat gui
  Source:   python3 gtk2laz.py gui

Conversion stats (opgui-0.12.4 demo):
  Stage 1 (signatures only):  1128 C-file TODOs
  Stage 2 (full AST bodies):   598 C-file TODOs  (-47%)
  Header TODOs:                 ~69
  Total:                       ~667

TODO marker format (NEW):
  Each TODO now contains the original C source as a multi-line comment:

    (* TODO gtk2laz: for loop with non-standard form
       Source: /path/to/file.c:82
       Original C:
         for (; j < 64; j++)
           bufferU[j] = 0x0;
    *)

  This lets you see exactly what C code needs manual translation,
  right next to the TODO marker in the .pas file.

The opgui_lazarus/ directory is a demo output.
