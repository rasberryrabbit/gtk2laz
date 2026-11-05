# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for gtk2laz.

Builds a single-file executable that bundles:
  * the gtk2laz CLI (gtk2laz.py)
  * the GTK/Glade -> LCL converter (glade_parser.py, c_parser.py, widget_map.py)
  * the Tkinter GUI front-end (gtk2laz_gui.py)
  * the AST translator (ast_translator.py, pcpp_preprocessor.py)
  * pycparser + ply (for AST-based C parsing)
  * pcpp (pure-Python C preprocessor)
  * the Tcl/Tk runtime libraries so the GUI works without a system Tk install.

The resulting binary is a console application: it prints help on `--help`
and launches the GUI on `gtk2laz gui`.

Requirements (must be installed before building):
  pip install pyinstaller pycparser ply pcpp
"""

import os
import pycparser

block_cipher = None

# pycparser stores its AST definition as a .cfg data file that must be
# bundled alongside the Python modules.
pycparser_dir = os.path.dirname(pycparser.__file__)

# ply is a separate package used by pycparser. We need to find it and
# bundle it as a data directory because PyInstaller's hiddenimports
# sometimes fails to locate it (especially on Windows with the
# Microsoft Store Python).
ply_dir = None
try:
    import ply
    ply_dir = os.path.dirname(ply.__file__)
except ImportError:
    pass

# pcpp has its own embedded ply at pcpp/ply/ply/
pcpp_dir = os.path.dirname(__import__('pcpp').__file__)

datas_list = [
    # fake libc/GTK stub headers generator
    ('gen_fake_includes.py', '.'),
    ('pcpp_preprocessor.py', '.'),
    ('diagnose.py', '.'),
    # pycparser's _c_ast.cfg is needed at runtime
    (os.path.join(pycparser_dir, '_c_ast.cfg'), 'pycparser'),
    # Local ply/ directory (bundled source - always available)
    ('ply/*.py', 'ply'),
]

# Bundle the entire ply package as data files if found
if ply_dir:
    datas_list.append((os.path.join(ply_dir, '*.py'), 'ply'))

# Bundle pcpp's embedded ply
pcpp_ply_dir = os.path.join(pcpp_dir, 'ply', 'ply')
if os.path.isdir(pcpp_ply_dir):
    datas_list.append((os.path.join(pcpp_ply_dir, '*.py'), 'pcpp/ply/ply'))

# Bundle pcpp's lextab/parsetab if they exist
for tab_file in ['lextab.py', 'parsetab.py']:
    tab_path = os.path.join(pcpp_dir, tab_file)
    if os.path.isfile(tab_path):
        datas_list.append((tab_path, 'pcpp'))

a = Analysis(
    ['gtk2laz.py'],
    pathex=[],
    binaries=[],
    datas=datas_list,
    hiddenimports=[
        # --- gtk2laz sibling modules ---
        'glade_parser',
        'c_parser',
        'widget_map',
        'gtk2laz_gui',
        'ast_translator',
        'gen_fake_includes',
        'pcpp_preprocessor',

        # --- pcpp (pure-Python C preprocessor) ---
        # pcpp ships its own copy of ply inside pcpp/ply/ply/
        'pcpp',
        'pcpp.preprocessor',
        'pcpp.parser',
        'pcpp.evaluator',
        'pcpp.pcmd',
        'pcpp.ply',
        'pcpp.ply.ply',
        'pcpp.ply.ply.lex',
        'pcpp.ply.ply.yacc',
        'pcpp.ply.ply.cpp',
        'pcpp.ply.ply.ctokens',
        'pcpp.ply.ply.ygen',
        'pcpp.lextab',
        'pcpp.parsetab',

        # --- ply (top-level package used by pycparser) ---
        # pycparser imports 'ply.lex' and 'ply.yacc' from the top-level
        # ply package, NOT from pcpp.ply.
        'ply',
        'ply.lex',
        'ply.yacc',
        'ply.cpp',
        'ply.ctokens',
        'ply.ygen',

        # --- pycparser ---
        'pycparser',
        'pycparser.c_ast',
        'pycparser.c_parser',
        'pycparser.c_generator',
        'pycparser.c_lexer',
        'pycparser._ast_gen',
        'pycparser.ast_transforms',
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
