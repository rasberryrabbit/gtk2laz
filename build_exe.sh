#!/usr/bin/env bash
#
# build_exe.sh
# ------------
# Build a single-file gtk2laz executable (x86-64) using PyInstaller.
#
# Output:
#   dist/gtk2laz      (Linux / macOS)
#   dist/gtk2laz.exe  (Windows)
#
# The executable bundles:
#   * the gtk2laz CLI (gtk2laz.py)
#   * the converter modules (glade_parser.py, c_parser.py, widget_map.py)
#   * the Tkinter GUI front-end (gtk2laz_gui.py)
#   * the Tcl/Tk runtime libraries
#
# Requirements:
#   pip install pyinstaller pycparser ply pcpp
#
set -euo pipefail

cd "$(dirname "$0")"

# Ensure all required packages are available.
# The PyInstaller spec file (gtk2laz.spec) imports pycparser and pcpp
# at spec-evaluation time, so they MUST be installed before building.
# ply is required by pycparser for AST parsing.
# pcpp is the pure-Python C preprocessor used for #include expansion.
echo "Checking build dependencies..."
MISSING=""
python3 -c "import PyInstaller"  2>/dev/null || MISSING="$MISSING pyinstaller"
python3 -c "import pycparser"    2>/dev/null || MISSING="$MISSING pycparser"
python3 -c "import ply"          2>/dev/null || MISSING="$MISSING ply"
python3 -c "import pcpp"         2>/dev/null || MISSING="$MISSING pcpp"
if [ -n "$MISSING" ]; then
    echo "Installing missing packages:$MISSING"
    python3 -m pip install $MISSING
fi

# Verify ply is actually installed (critical for AST translation)
echo "--- Verifying ply installation ---"
python3 -c "import ply; import ply.lex; import ply.yacc; print('ply OK:', ply.__file__)" || {
    echo "============================================================" >&2
    echo "  ERROR: ply package is NOT installed!" >&2
    echo "============================================================" >&2
    echo "  ply is required for AST-based C parsing." >&2
    echo "  Without it, you will get many more TODOs in the output." >&2
    echo "" >&2
    echo "  Install it manually:" >&2
    echo "    pip install ply" >&2
    echo "" >&2
    echo "  Then re-run this script." >&2
    echo "============================================================" >&2
    exit 1
}

# Clean previous build artifacts
rm -rf build dist

# Build using the spec file (single-file, console app, includes GUI)
python3 -m PyInstaller gtk2laz.spec --noconfirm --clean

# Verify the binary exists and runs
if [ -f dist/gtk2laz ]; then
    BIN=dist/gtk2laz
elif [ -f dist/gtk2laz.exe ]; then
    BIN=dist/gtk2laz.exe
else
    echo "ERROR: build did not produce an executable in dist/" >&2
    exit 1
fi

echo ""
echo "Build OK: $BIN"
file "$BIN"
"$BIN" --version

echo ""
echo "CLI help:"
"$BIN" --help | head -15

echo ""
echo "Smoke test - c2pas on a tiny C file:"
TMPDIR=$(mktemp -d)
mkdir -p "$TMPDIR/src" "$TMPDIR/out"
cat > "$TMPDIR/src/hello.c" <<'EOF'
#include <stdio.h>
int add(int a, int b) {
    return a + b;
}
void greet(const char *name) {
    printf("Hello, %s!\n", name);
}
EOF
"$BIN" c2pas "$TMPDIR/src" "$TMPDIR/out"
echo "Output dir contents:"
ls "$TMPDIR/out"
echo "First 20 lines of hello.pas:"
head -20 "$TMPDIR/out/hello.pas"
rm -rf "$TMPDIR"
echo ""
echo "Build & smoke test complete."
