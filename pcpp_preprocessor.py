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
    OutputDirective = None
    Action = None


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


def is_available() -> bool:
    """Return True if pcpp is importable."""
    return _HAS_PCPP
