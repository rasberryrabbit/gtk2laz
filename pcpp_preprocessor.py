"""
pcpp_preprocessor.py
====================
Pure-Python C preprocessor wrapper around the ``pcpp`` package.

This module provides a drop-in replacement for ``gcc -E`` so that the
AST-based C translator works on machines without a C compiler installed
(notably Windows, where gcc/cpp is not available by default).

Usage::

    from pcpp_preprocessor import preprocess
    text = preprocess('/path/to/source.c',
                      fake_include_dir='/tmp/fake_libc_include')
    # ``text`` is the preprocessed C source, ready for pycparser.

The preprocessor:
  * Resolves ``#include <foo.h>`` and ``#include "foo.h"`` against the
    fake include directory (and the source file's own directory).
  * Defines away GNU extensions (``__attribute__``, ``__asm__``,
    ``__volatile__``, ...) so pycparser can parse the result.
  * Strips ``#line`` directives that pcpp emits (pycparser doesn't
    understand them).
  * Falls back gracefully: if pcpp is not installed, returns None so
    the caller can try gcc.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Optional

try:
    from pcpp import Preprocessor, OutputDirective, Action
    _HAS_PCPP = True
except ImportError:
    _HAS_PCPP = False
    # Provide stub base classes so the module-level class definitions
    # below don't raise ``NameError`` when pcpp is not installed.  The
    # stubs are never used at runtime because ``preprocess()`` checks
    # ``is_available()`` (which returns ``_HAS_PCPP``) before creating
    # any instance of ``FakeIncludePreprocessor``.
    class Preprocessor:  # type: ignore[no-redef]
        pass
    class OutputDirective:  # type: ignore[no-redef]
        pass
    class Action:  # type: ignore[no-redef]
        pass


class FakeIncludePreprocessor(Preprocessor):
    """A pcpp Preprocessor that resolves #include against a fake
    include directory.

    pcpp's default ``on_include_not_found`` raises an error and passes
    the directive through. We override it to look up the header in our
    fake include directory and return the resolved path so pcpp reads
    it instead.

    We also enable ``line_directive = '#line'`` so pcpp emits #line
    markers in the output. pycparser uses these to set coord.file on
    each AST node, which lets us filter out nodes that came from system
    headers (only keep nodes from the user's .c file).
    """

    def __init__(self, fake_include_dir: str, source_dir: str = ""):
        super().__init__()
        self.fake_dir = fake_include_dir
        self.source_dir = source_dir
        # Emit #line directives so pycparser can track which file each
        # node came from (needed to filter out system-header nodes).
        self.line_directive = "#line"
        # Suppress pcpp's own error output for missing includes
        self._errors: list[str] = []

    def on_include_not_found(self, is_malformed, is_system_include,
                              curdir, includepath):
        """Try to find the include file in our fake include dir."""
        if is_malformed:
            # Don't try to fix malformed includes
            raise OutputDirective(Action.IgnoreAndPassThrough)
        # includepath is like "stdio.h" or "sys/types.h" or "gtk/gtk.h"
        # Try the fake include directory first
        candidate = os.path.join(self.fake_dir, includepath)
        if os.path.isfile(candidate):
            return candidate
        # Try relative to the source file's directory
        if self.source_dir:
            candidate2 = os.path.join(self.source_dir, includepath)
            if os.path.isfile(candidate2):
                return candidate2
        # Try relative to curdir (the directory of the including file)
        if curdir:
            candidate3 = os.path.join(curdir, includepath)
            if os.path.isfile(candidate3):
                return candidate3
        # Not found - pass through (the include directive will be
        # removed from the output, which is what we want for system
        # headers we don't have stubs for)
        raise OutputDirective(Action.IgnoreAndRemove)

    def on_error(self, source, lineno, msg):
        """Collect errors instead of printing them. We don't want pcpp
        to abort on missing headers (we handle that in
        on_include_not_found)."""
        self._errors.append(f"{source}:{lineno}: {msg}")


def preprocess(c_path: str,
               fake_include_dir: str = "/tmp/fake_libc_include",
               extra_defines: Optional[list[str]] = None) -> Optional[str]:
    """Preprocess a C source file using pcpp (pure Python).

    Returns the preprocessed text, or None if pcpp is not available.

    Parameters
    ----------
    c_path
        Path to the .c source file.
    fake_include_dir
        Directory containing stub libc/GTK headers.
    extra_defines
        Additional macro definitions (e.g. ``["DEBUG=1"]``).
    """
    if not _HAS_PCPP:
        return None
    try:
        with open(c_path, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError:
        return None

    source_dir = os.path.dirname(os.path.abspath(c_path))
    pp = FakeIncludePreprocessor(fake_include_dir, source_dir)

    # Define away GNU extensions that pycparser cannot parse.
    # Each define maps the macro to empty (or a safe replacement).
    gnu_defines = [
        "__attribute__(x) ",
        "__extension__ ",
        "__asm__(x) ",
        "__asm(x) ",
        "__volatile__ ",
        "__inline ",
        "__inline__ ",
        "__restrict ",
        "__restrict__ ",
        "__signed__ ",
        "__signed ",
        "__typeof__(x) int",
        "__builtin_va_list void*",
        "__builtin_va_start(x,y) ",
        "__builtin_va_end(x) ",
        "__builtin_offsetof(t,m) 0",
        "__alignof__(x) 1",
        "__alignof(x) 1",
        "_Nonnull ",
        "_Nullable ",
        "_Null_unspecified ",
        "__null_unspecified ",
        "__deprecated ",
        "__deprecated_msg(x) ",
        "__ unavailable ",
        "__OSX_AVAILABLE_STARTING(x,y) ",
        "__OSX_AVAILABLE_BUT_DEPRECATED(x,y,z,w) ",
        "AVAILABLE_MAC_OS_X_VERSION_10_0_AND_LATER ",
        "AVAILABLE_MAC_OS_X_VERSION_10_0_AND_LATER_BUT_DEPRECATED_IN_MAC_OS_X_VERSION_10_12 ",
    ]
    for d in gnu_defines:
        try:
            pp.define(d)
        except Exception:
            pass

    # Apply extra defines
    if extra_defines:
        for d in extra_defines:
            try:
                pp.define(d)
            except Exception:
                pass

    # Add the fake include dir as a search path (helps pcpp resolve
    # includes even without on_include_not_found)
    pp.add_path(fake_include_dir)

    # Parse the source
    try:
        pp.parse(src, source=c_path)
    except Exception:
        return None

    # Write the preprocessed output
    import io
    out = io.StringIO()
    pp.write(out)
    text = out.getvalue()

    # ---- Auto-inject fake GTK/GDK header if the preprocessed text
    # references GTK/GDK types but the source did not include <gtk/gtk.h>
    # explicitly.  This is common in opgui: icd.h declares
    # ``extern GtkWidget *statusTxt;`` etc. without including gtk.h,
    # relying on the includer (opgui.c) to provide the types via the
    # common.h chain.  When pycparser parses the preprocessed text it
    # sees ``GtkWidget`` used as a type name with no typedef in scope
    # and aborts with "Invalid declaration", which causes the whole
    # .c file's AST parse to fail (and the converter falls back to the
    # pattern-based extractor, which only finds ~half the functions).
    #
    # We detect the situation by looking for known GTK/GDK type names
    # in the preprocessed text that did NOT come from the fake gtk.h
    # already (i.e. there is no ``typedef struct _GtkWidget GtkWidget;``
    # line in the output).  When that happens we prepend the contents
    # of the fake ``gtk/gtk.h`` so pycparser sees the typedefs.
    # ---- Always prepend ALL fake system headers so pycparser sees
    # every typedef/struct the user's code might reference, even if
    # the source did not #include the corresponding system header.
    #
    # Real C compilers resolve ``fd_set``, ``struct timeval``, etc.
    # through the user's include chain (``common.h`` -> ``deviceRW.h``
    # -> <sys/select.h> -> <sys/time.h> -> ...).  pycparser only sees
    # what the preprocessor expands, and the fake headers we ship
    # only get pulled in if the user explicitly includes them.  When
    # a header like opgui's icd.h uses ``GtkWidget`` without including
    # <gtk/gtk.h> (relying on common.h to provide it), pycparser
    # aborts with "Invalid declaration" and the whole AST parse fails.
    #
    # We work around this by concatenating EVERY fake header in the
    # fake include directory and prepending the result to the
    # preprocessed text.  This guarantees every typedef the user
    # might reference is in scope.  The cost is a larger text for
    # pycparser to parse, but fake headers are small (a few KB each)
    # so the overhead is negligible.
    #
    # ---- IMPORTANT: coord.file tracking ----
    # pycparser uses ``#line`` directives to set ``coord.file`` on
    # each AST node, which the converter uses to FILTER OUT nodes
    # that came from fake headers (so fake-header typedefs don't
    # pollute the user's .pas unit).  Without a ``#line`` directive,
    # pycparser attributes the prepended fake-header nodes to the
    # user's .c file, and they leak into the output.
    #
    # pycparser DOES support ``#line`` directives (unlike ``#if`` /
    # ``#define`` which it does not).  We emit a ``#line 1
    # "<fake_header>.h"`` marker BEFORE each fake header's content so
    # pycparser attributes every node from that header to a
    # "<fake_header>.h" coord.file, which the converter then filters
    # out (since the user's .c file basename != "<fake_header>.h").
    fake_header_blob = _read_all_fake_headers(fake_include_dir)
    if fake_header_blob:
        text = fake_header_blob + "\n" + text
    # ---- Auto-define any remaining Gtk*/Gdk* type names that the
    # fake gtk.h didn't already cover.  Even with all fake headers
    # prepended, a real GTK app may reference typedefs that aren't
    # in any of our stubs (GtkTreeModelForeachFunc, GtkAccelGroup,
    # ...).  We scan the preprocessed text for any identifier
    # starting with ``Gtk`` or ``Gdk`` (or GLib's ``G`` prefix
    # followed by an uppercase then lowercase letter) and emit a
    # placeholder typedef for each one that is not already defined.
    # This keeps pycparser happy so the AST parse succeeds and we
    # can extract all function bodies.
    text = _inject_missing_gtk_typedefs(text)

    # pcpp emits #line directives (since we set line_directive = "#line").
    # pycparser understands #line directives and uses them to set coord.file
    # on each AST node, which lets us filter out system-header nodes.
    # We DO NOT strip #line directives - they are needed for filtering.
    # However, we do strip empty # directives and normalize whitespace.
    cleaned_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        # Skip empty # directives
        if stripped == "#":
            continue
        cleaned_lines.append(line)

    return "\n".join(cleaned_lines)


# Known GTK/GDK type names that, if they appear in preprocessed C source
# without a corresponding typedef, indicate the source relies on a
# system GTK header that was not preprocessed in.  We use a conservative
# list of the most common GtkWidget-derived types so the auto-injection
# only triggers for genuine GTK-using files.
_GTK_TYPE_NAMES = (
    "GtkWidget", "GtkWindow", "GtkButton", "GtkLabel", "GtkEntry",
    "GtkBox", "GtkNotebook", "GtkComboBox", "GtkComboBoxText",
    "GtkSpinButton", "GtkTextView", "GtkTextBuffer", "GtkTextIter",
    "GtkListStore", "GtkTreeStore", "GtkTreeIter", "GtkTreePath",
    "GtkTreeModel", "GtkTreeSelection", "GtkCellRenderer",
    "GtkFileChooser", "GtkFileChooserDialog", "GtkDialog",
    "GtkCheckMenuItem", "GtkMenu", "GtkMenuBar", "GtkMenuItem",
    "GtkToolbar", "GtkToolButton", "GtkStatusbar", "GtkProgressBar",
    "GtkAdjustment", "GtkScrolledWindow", "GtkFrame", "GtkAspectFrame",
    "GtkPaned", "GtkHPaned", "GtkVPaned", "GtkSeparator",
    "GtkRadioButton", "GtkCheckButton", "GtkToggleButton",
    "GtkDrawingArea", "GtkScale", "GtkHScale", "GtkVScale",
    "GtkScrollbar", "GtkHScrollbar", "GtkVScrollbar",
    "GtkCellRendererText", "GtkCellRendererPixbuf",
    "GtkTreeIterCompareFunc", "GtkTreeViewColumn",
    "GdkEventButton", "GdkEventKey", "GdkPixbuf", "GdkColor",
    "GdkRectangle", "GdkDrawable", "GdkGC", "GdkPixmap",
)


def _needs_gtk_injection(text: str) -> bool:
    """Heuristic: return True if ``text`` uses GTK/GDK type names but
    does not already contain the typedefs that the fake ``gtk/gtk.h``
    would have provided.

    We look for:
      * any of the known GTK/GDK type names appearing as a whole word
        (so ``GtkWidget_xxx`` does NOT match), AND
      * absence of the marker line ``typedef struct _GtkWidget GtkWidget;``
        which is the first typedef in the fake gtk/gtk.h.  If that
        typedef is already present the user (or another header) has
        already pulled in the fake GTK definitions, so we don't need
        to inject it again.
    """
    # Quick reject: if the text already has the fake gtk.h typedefs,
    # we don't need to inject.
    if "typedef struct _GtkWidget GtkWidget;" in text:
        return False
    # Quick reject: text too small to plausibly use GTK.
    if len(text) < 100:
        return False
    # Look for any known GTK/GDK type name as a whole word.
    for name in _GTK_TYPE_NAMES:
        # Word-boundary check using a regex so that ``GtkWidgetFoo``
        # or ``myGtkWidget`` don't false-trigger.
        if re.search(rf"\b{re.escape(name)}\b", text):
            return True
    return False


def _inject_missing_gtk_typedefs(text: str) -> str:
    """Scan ``text`` for ``Gtk*`` / ``Gdk*`` identifiers used as type
    names and emit placeholder typedefs for any that are not already
    defined.

    The fake ``gtk/gtk.h`` only covers the most common widget types.
    Real GTK apps reference many more typedefs (GtkTreeModelForeachFunc,
    GtkTreeIterCompareFunc, GdkEventMotion, GtkAccelGroup, GtkCssSection,
    ...) that are too numerous to enumerate by hand.  Without them,
    pycparser fails with ``Invalid declaration`` at the first unknown
    type and the whole AST parse aborts, leaving the converter to fall
    back to the pattern-based extractor (which only finds ~half the
    functions in opgui.c).

    We work around this by:
      1. Collecting every ``typedef ... NAME ...`` line in ``text``
         and adding NAME to a "defined" set.
      2. Scanning for word-boundary identifiers starting with ``Gtk``
         or ``Gdk`` that are NOT in the defined set.
      3. Prepending a single ``typedef struct _<NAME> <NAME>;`` line
         for each one, so pycparser sees them as opaque struct types.

    Function-pointer typedefs (``GtkTreeModelForeachFunc``) are
    emitted as ``typedef int (*NAME)(void);`` so they can also appear
    in cast expressions like ``(GtkTreeModelForeachFunc)func``.

    The placeholder typedefs are wrapped in a comment block so a human
    reading the preprocessed text can see why they exist.
    """
    if not text:
        return text
    # Collect every name already typedef'd in the text so we don't
    # duplicate.  We look for both ``typedef struct _X X;`` (struct
    # typedefs) and ``typedef <stuff> NAME;`` (any other typedef).
    already_defined: set[str] = set()
    for m in re.finditer(
        r"typedef\s+(?:struct\s+\w+\s+|[^;{]*\s+)(\w+)\s*[;{]", text
    ):
        already_defined.add(m.group(1))
    # Also collect names from the simpler ``typedef <name>;`` form
    # (e.g. ``typedef int gboolean;``).
    for m in re.finditer(r"typedef\s+[^;{}]*\s+(\w+)\s*;", text):
        already_defined.add(m.group(1))
    # Find all Gtk*/Gdk*/G-prefixed identifiers used as whole words.
    # We match identifiers starting with:
    #   * ``Gtk`` or ``Gdk`` followed by an uppercase letter and then
    #     any mix of alphanumerics (GtkWidget, GtkTreeIter, GdkEventButton,
    #     GtkTreeModelForeachFunc, ...).  These are GTK/GDK type names.
    #   * ``G`` followed by an uppercase letter, then a *lowercase* letter
    #     (GObject, GList, GSList, GSourceFunc, GCallback, ...).  These
    #     are GLib type names.  We require the third character to be a
    #     lowercase letter so all-caps constants like ``GROUP_ALL`` or
    #     ``G_PI`` (math constant) are not mistaken for type names.
    candidates: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"\b(G[A-Za-z0-9_]+)\b", text):
        name = m.group(1)
        is_gtk_type = (
            name.startswith("Gtk")
            or name.startswith("Gdk")
            or (
                len(name) >= 3
                and name[0] == "G"
                and name[1].isupper()
                and name[2].islower()
            )
        )
        if not is_gtk_type:
            continue
        if name in already_defined or name in seen:
            continue
        seen.add(name)
        candidates.append(name)
    if not candidates:
        return text
    # Emit placeholder typedefs.  We use a single forward declaration
    # block prepended to the text.  Each entry is one of:
    #   typedef struct _GtkFoo GtkFoo;        (struct typedef)
    # or, for names ending in common function-pointer suffixes, a
    # function-pointer typedef so they can be used in casts.
    #
    # IMPORTANT: we must NOT wrap the typedefs in a ``/* ... */``
    # comment block.  pycparser does not support C comments in its
    # input (see https://github.com/eliben/pycparser#3using), so a
    # comment block at the start of the text would cause the whole
    # parse to abort with "Comments are not supported".  We use
    # ``//`` line comments instead, which pycparser also rejects,
    # so we simply omit any human-readable marker.
    func_ptr_suffixes = ("Func", "Callback", "Notify", "Destroy")
    lines: list[str] = []
    for name in candidates:
        if any(name.endswith(suf) for suf in func_ptr_suffixes):
            # Function-pointer typedef.  We use ``int (*)(void)`` as
            # a generic placeholder so the type can appear in both
            # variable declarations and cast expressions.
            lines.append(f"typedef int (*{name})(void);")
        else:
            # Struct typedef.  We use ``struct _<NAME>`` as the tag
            # so the typedef does not conflict with any later real
            # definition of the same struct.
            lines.append(f"typedef struct _{name} {name};")
    return "\n".join(lines) + "\n" + text


def is_available() -> bool:
    """Return True if pcpp is importable."""
    return _HAS_PCPP


def _read_all_fake_headers(fake_include_dir: str) -> str:
    """Concatenate every ``.h`` file under ``fake_include_dir`` into a
    single string so it can be prepended to the preprocessed text.

    This guarantees every typedef/struct the user's code might
    reference is in scope for pycparser, even if the source did not
    explicitly ``#include`` the corresponding system header.  We walk
    the directory recursively so subdirectories like ``gtk/`` and
    ``sys/`` are picked up too.

    Returns the concatenated text, or "" if the directory is empty or
    unreadable.  Each header's content is separated by a newline so
    the last line of one header doesn't run into the first line of
    the next.

    IMPORTANT — coord.file tracking:
    pycparser uses ``#line`` directives to set ``coord.file`` on each
    AST node.  The converter's AST filter
    (``ast_translator.AstToPascal.parse``) only keeps nodes whose
    ``coord.file`` matches the user's .c file basename, so fake-header
    typedefs don't pollute the user's .pas unit.

    To make that filter work, we MUST emit a ``#line 1 "<header>.h"``
    marker BEFORE each fake header's content.  That way pycparser
    attributes every typedef/struct/extern from the fake header to a
    "<header>.h" coord.file, which the filter then drops (since the
    user's .c file basename != "<header>.h").

    We also strip every OTHER ``#``-prefixed preprocessor directive
    (``#ifndef``, ``#define``, ``#endif``, ``#include``, ...) from
    each header's content.  pycparser does not support those in its
    input (it expects already-preprocessed text), but it DOES support
    ``#line`` directives — so we keep the ``#line`` markers we emit
    and strip everything else.
    """
    if not fake_include_dir or not os.path.isdir(fake_include_dir):
        return ""
    # Collect (header_name, content) pairs so we can emit a #line
    # marker before each one.  We use the basename of each .h file as
    # the coord.file so the converter's filter can identify them.
    header_pairs: list[tuple[str, str]] = []
    for root, _dirs, files in os.walk(fake_include_dir):
        for fname in sorted(files):
            if not fname.endswith(".h"):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8",
                          errors="replace") as f:
                    content = f.read()
            except OSError:
                continue
            header_pairs.append((fname, content))
    # Build the output: for each header, emit a ``#line 1 "<name>"``
    # marker followed by the header's content with non-#line
    # directives stripped.
    out_parts: list[str] = []
    for header_name, content in header_pairs:
        # Emit a #line directive so pycparser attributes all nodes
        # from this header to "<header_name>" coord.file.
        out_parts.append(f'#line 1 "{header_name}"')
        # Strip every # directive EXCEPT the #line we just emitted.
        # We do this by processing each line of the header's content:
        # any line starting with ``#`` that is NOT a ``#line`` we
        # just emitted gets blanked out.
        cleaned_lines = []
        for line in content.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#") and not stripped.startswith("#line"):
                # Strip #ifndef, #define, #endif, #include, etc. so
                # pycparser doesn't abort with "Directives not
                # supported yet".  Replace with a blank line to
                # preserve line numbering.
                cleaned_lines.append("")
            else:
                cleaned_lines.append(line)
        out_parts.append("\n".join(cleaned_lines))
        # Emit a trailing #line directive pointing back to a
        # sentinel "<end_fake_headers>" so any code after this header
        # (the next header, or the user's source) is correctly
        # attributed.  We use a sentinel that won't match the user's
        # .c file basename.
        out_parts.append(f'#line 1 "<end_fake_header_{header_name}>"')
    return "\n".join(out_parts)
