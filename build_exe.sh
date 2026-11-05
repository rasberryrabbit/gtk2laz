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
#   pip install pyinstaller
#
set -euo pipefail

cd "$(dirname "$0")"

# Ensure pyinstaller is available
if ! command -v pyinstaller >/dev/null 2>&1; then
    echo "PyInstaller not found. Installing..."
    python3 -m pip install --user pyinstaller
fi

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
