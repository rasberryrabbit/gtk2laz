# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for gtk2laz.

Builds a single-file executable that bundles:
  * the gtk2laz CLI (gtk2laz.py)
  * the GTK/Glade -> LCL converter (glade_parser.py, c_parser.py, widget_map.py)
  * the Tkinter GUI front-end (gtk2laz_gui.py)
  * the Tcl/Tk runtime libraries so the GUI works on a stock x86-64 Linux
    without a system Tk install.

The resulting binary is a console application: it prints help on `--help`
and launches the GUI on `gtk2laz gui`.
"""

block_cipher = None

a = Analysis(
    ['gtk2laz.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # ensure sibling modules are pulled in even though they are
        # imported via sys.path manipulation rather than a package.
        'glade_parser',
        'c_parser',
        'widget_map',
        'gtk2laz_gui',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim unused stdlib modules to keep the binary small.
        'unittest', 'test', 'pydoc', 'doctest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='gtk2laz',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
