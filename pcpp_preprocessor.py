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


# Module-level extra defines set by the CLI's ``--define`` option.
# The ``convert()`` function in ``gtk2laz.py`` calls
# ``set_extra_defines(args.define)`` once at the start of a conversion
# so every subsequent ``preprocess()`` call picks them up. This avoids
# threading the extra-defines list through every function in the call
# chain (convert → convert_c_file_with_includes → convert_c_file →
# CFileTranslator → parse_with_ast → AstToPascal.parse → preprocess).
_GLOBAL_EXTRA_DEFINES: list[str] = []


def _inline_local_includes_only(c_path: str) -> str:
    """Pre-inline every ``#include "..."`` header BEFORE pcpp runs.

    Unlike ``ast_translator._read_and_inline_local_headers`` (which
    strips ALL preprocessor directives), this function KEEPS every
    ``#define``, ``#ifdef``, ``#if``, ``#endif``, ``#pragma``, etc. so
    pcpp can still do macro expansion and conditional compilation. We
    ONLY replace ``#include "name.h"`` lines with the actual contents
    of ``name.h`` (recursively).

    This is needed because pcpp's include resolution is unreliable on
    Windows — it sometimes silently drops local includes, producing a
    tiny pcpp output with no typedefs from the project's own headers.
    By pre-inlining the local includes ourselves, pcpp never has to
    resolve a local include — it just sees the inlined content and
    handles the ``#ifdef`` guards and ``#define`` macros as usual.

    System includes (``#include <name.h>``) are LEFT INTACT — pcpp
    resolves those against the fake include directory, and the fake
    header blob (prepended later) provides the typedefs.

    The function resolves local includes relative to:
      1. The current file's directory (base_dir) — for nested includes
         like "win32.h" inside enet/enet.h
      2. The top-level source directory (src_dir) — for top-level
         includes like "enet/enet.h" from callbacks.c

    Circular includes are handled by tracking already-inlined files in
    a ``seen`` set.

    WINDOWS PATH HANDLING:
    C ``#include`` directives use forward slashes (``"enet/enet.h"``)
    even on Windows. ``os.path.join`` on Windows produces backslash
    paths (``src_dir\\enet/enet.h``), which usually works but can fail
    in edge cases. We normalise all paths to use ``os.sep`` so the
    join is consistent, and we also try the raw join (which may mix
    separators) as a fallback.
    """
    src_dir = os.path.dirname(os.path.abspath(c_path))
    seen_includes: set[str] = set()
    import re as _re_ili

    def _try_resolve(base: str, inc_name: str) -> str:
        """Resolve ``inc_name`` relative to ``base`` directory.

        ``inc_name`` may use forward slashes (C convention) even on
        Windows. We try several combinations of path separators to
        maximise the chance of finding the file on all platforms.
        Returns the resolved path if the file exists, else "".
        """
        # Normalise the include path to the OS separator.
        inc_norm = inc_name.replace("/", os.sep)
        candidates = [
            os.path.join(base, inc_norm),
            os.path.join(base, inc_name),       # raw (mixed separators)
            os.path.join(src_dir, inc_norm),
            os.path.join(src_dir, inc_name),
        ]
        for c in candidates:
            if os.path.isfile(c):
                return os.path.abspath(c)
        return ""

    def _read_with_local_includes(path: str, base_dir: str,
                                  depth: int = 0) -> str:
        if depth > 15:
            return ""  # avoid infinite recursion on circular includes
        try:
            with open(path, "r", encoding="utf-8",
                      errors="replace") as f:
                content = f.read()
        except OSError:
            return ""
        out_lines: list[str] = []
        for line in content.splitlines():
            stripped = line.lstrip()
            # Local include: #include "name.h"
            m = _re_ili.match(r'#\s*include\s+"([^"]+)"', stripped)
            if m:
                inc_name = m.group(1)
                inc_abs = _try_resolve(base_dir, inc_name)
                if not inc_abs:
                    # Header not found — leave the #include line intact
                    # so pcpp can try to resolve it (or
                    # on_include_not_found can warn). This is important:
                    # if we silently drop the include, the typedefs from
                    # that header are missing and pycparser will fail
                    # with "Missing type" errors. By keeping the line,
                    # at least pcpp has a chance to find it via its
                    # own path resolution.
                    out_lines.append(line)
                    continue
                if inc_abs in seen_includes:
                    # Already inlined — skip (preserve line numbering
                    # with an empty line so #line markers stay aligned).
                    out_lines.append("")
                    continue
                seen_includes.add(inc_abs)
                # Recursively inline. The base_dir for nested includes
                # is the directory of THIS header.
                nested_dir = os.path.dirname(inc_abs)
                inlined = _read_with_local_includes(
                    inc_abs, nested_dir, depth + 1
                )
                out_lines.append(inlined)
                continue
            # System include: #include <name.h> — leave it for pcpp.
            # Any other preprocessor directive — leave it for pcpp.
            # Regular code line — leave it.
            out_lines.append(line)
        return "\n".join(out_lines)

    return _read_with_local_includes(c_path, src_dir, 0)


def set_extra_defines(defines: Optional[list[str]]) -> None:
    """Set the module-level extra defines for all subsequent
    ``preprocess()`` calls.

    Called once by ``gtk2laz.convert()`` at the start of a conversion
    so every .c/.h file processed during that conversion sees the same
    user-supplied macros. Pass ``None`` or ``[]`` to clear.
    """
    global _GLOBAL_EXTRA_DEFINES
    _GLOBAL_EXTRA_DEFINES = list(defines or [])


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
        """Try to find the include file in our fake include dir.

        Resolution order:
          1. Fake include directory (for system headers like stdio.h)
          2. Source file's directory (for local headers like enet/enet.h)
          3. curdir — the directory of the file that did the #include

        If none of these work, we print a diagnostic to stderr so the
        user can see WHICH include failed (and thus which typedefs are
        missing from the pcpp output). Without this diagnostic, a
        missing local header silently produces a tiny pcpp output
        with no enet typedefs, which cascades into "Missing type in
        declaration" errors on every function that uses those types.
        """
        if is_malformed:
            # Don't try to fix malformed includes
            raise OutputDirective(Action.IgnoreAndPassThrough)
        # includepath is like "stdio.h" or "sys/types.h" or "gtk/gtk.h"
        # or "enet/enet.h" (a local header relative to the source dir).
        # Try the fake include directory first (for system headers).
        candidate = os.path.join(self.fake_dir, includepath)
        if os.path.isfile(candidate):
            return candidate
        # Try relative to the source file's directory (for local
        # headers like "enet/enet.h" when the source is in the parent
        # directory of enet/).
        if self.source_dir:
            candidate2 = os.path.join(self.source_dir, includepath)
            if os.path.isfile(candidate2):
                return candidate2
        # Try relative to curdir (the directory of the including file).
        # This handles the case where enet.h includes "enet/win32.h" —
        # curdir would be the enet/ subdirectory, so the join resolves
        # to enet/enet/win32.h which is wrong... but pcpp actually
        # passes curdir as the directory of the file being preprocessed,
        # so for a top-level include like "enet/enet.h" from callbacks.c,
        # curdir is the source directory. For a nested include like
        # "win32.h" from inside enet.h, curdir is the enet/ directory.
        if curdir:
            candidate3 = os.path.join(curdir, includepath)
            if os.path.isfile(candidate3):
                return candidate3
        # Not found. Print a diagnostic so the user can see which
        # include failed — this is critical for debugging "Missing
        # type in declaration" cascade errors. Without this message,
        # a missing local header (e.g. because the source directory
        # layout is non-standard) silently produces a pcpp output
        # with no typedefs from that header, and pycparser then fails
        # on every line that references those types.
        # We distinguish between system includes (angle-bracket, e.g.
        # <stdio.h>) and local includes (quoted, e.g. "enet/enet.h")
        # because system includes are expected to be in the fake
        # include dir and their absence is usually harmless (the fake
        # header blob already has the typedefs), while local includes
        # are critical — their absence means the .c file's own headers
        # are missing, which breaks the whole AST parse.
        if not is_system_include:
            print(f"gtk2laz: WARNING — local include not found: "
                  f"{includepath!r} (searched: {self.fake_dir!r}, "
                  f"{self.source_dir!r}, {curdir!r})",
                  file=sys.stderr)
        # Remove the include directive from the output. For system
        # headers this is correct (the fake header blob already has
        # the typedefs). For local headers this means the typedefs
        # from that header are missing — the user will see the
        # WARNING above and can investigate.
        raise OutputDirective(Action.IgnoreAndRemove)

    def on_error(self, source, lineno, msg):
        """Collect errors instead of printing them. We don't want pcpp
        to abort on missing headers (we handle that in
        on_include_not_found)."""
        self._errors.append(f"{source}:{lineno}: {msg}")


# ---------------------------------------------------------------------------
# Target-platform presets
# ---------------------------------------------------------------------------
# When converting C code that uses ``#ifdef _WIN32`` / ``#ifndef _WIN32``
# / ``#ifdef __APPLE__`` / ``#ifdef __linux__`` guards, the converter
# needs to know which platform the converted Pascal code is intended for.
# Real C compilers predefine a standard set of macros per platform:
#
#   Windows (_WIN32):  _WIN32, _WIN32_WINNT, WIN32, _WINDOWS, __WIN32__
#   Linux:             __linux__, __unix__, __linux, __GNUC__
#   macOS:             __APPLE__, __MACH__, __unix__, TARGET_OS_MAC
#
# Without these defines, pcpp would skip BOTH branches of the
# ``#ifdef`` guards (because the macro is undefined), which leaves
# typedefs like ``ENET_API`` undefined and causes pycparser to abort
# on every function declaration that uses it.
#
# The user can override the auto-detected platform by passing
# ``--platform=windows`` (or ``linux`` / ``macos`` / ``auto``) on the
# gtk2laz command line, or by selecting a platform in the GUI. The
# selected platform's preset macros are then fed to pcpp via
# ``pp.define()`` so the correct ``#ifdef`` branch is taken in every
# header.
#
# Format: each preset is a list of strings that ``pp.define()`` accepts
# (e.g. ``"_WIN32 1"``, ``"_WIN32_WINNT 0x0601"``).
PLATFORM_PRESETS: dict[str, list[str]] = {
    "auto": [],   # auto-detect from sys.platform (filled in below)
    "windows": [
        "_WIN32 1",
        "_WIN32_WINNT 0x0601",
        "WIN32 1",
        "_WINDOWS 1",
        "__WIN32__ 1",
        "NOMINMAX 1",
        # MSVC inline keyword equivalents
        "__inline inline",
    ],
    "linux": [
        "__linux__ 1",
        "__linux 1",
        "__unix__ 1",
        "__unix 1",
        "__GNUC__ 1",
        # GLib headers reference these on Linux
        "GLIB_VERSION_MIN_REQUIRED 2",
    ],
    "macos": [
        "__APPLE__ 1",
        "__MACH__ 1",
        "__unix__ 1",
        "__unix 1",
        "__GNUC__ 1",
        "TARGET_OS_MAC 1",
    ],
}


def _auto_platform() -> str:
    """Return the platform name that matches the running interpreter.

    Used when the user does not explicitly pass ``--platform``. This
    mirrors what a real C compiler on the user's machine would
    predefine: on Windows ``_WIN32`` is set, on Linux ``__linux__``,
    on macOS ``__APPLE__``.
    """
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    # Everything else (linux, freebsd, ...) gets the linux preset
    # because the unix/BSD family all share the same core macros
    # (``__unix__``, ``__GNUC__``). If a user wants a specific BSD
    # preset they can use ``--define`` to add their own macros.
    return "linux"


def preprocess(c_path: str,
               fake_include_dir: str = "/tmp/fake_libc_include",
               extra_defines: Optional[list[str]] = None,
               target_platform: str = "auto") -> Optional[str]:
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
    target_platform
        Target platform for ``#ifdef`` branch selection. One of:
        ``"auto"`` (use ``sys.platform``), ``"windows"``, ``"linux"``,
        ``"macos"``. The platform's standard preprocessor macros
        (e.g. ``_WIN32`` on Windows, ``__linux__`` on Linux) are
        defined before preprocessing so the correct branch of every
        ``#ifdef`` guard is taken in every header.
    """
    if not _HAS_PCPP:
        return None
    try:
        with open(c_path, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError:
        return None

    # ---- Pre-inline local headers BEFORE pcpp.
    #
    # pcpp's include resolution is unreliable on Windows (backslash
    # paths, missing curdir, etc.) — even with our ``add_path()`` calls
    # and ``on_include_not_found`` callback, pcpp on Windows sometimes
    # silently drops local ``#include "enet/enet.h"`` directives,
    # producing a tiny pcpp output with no enet typedefs. pycparser
    # then fails with "Missing type in declaration" on every line that
    # references those types (ENetCallbacks, ENetHost, enet_uint8, etc.),
    # and the whole file falls back to the pattern-based translator.
    #
    # To make AST parsing reliable on ALL platforms, we pre-inline
    # every local ``#include "..."`` header ourselves BEFORE handing
    # the source to pcpp. pcpp then only needs to handle macro
    # expansion and ``#ifdef`` guards — it never has to resolve a
    # local include.
    #
    # We inline ONLY local includes (``#include "..."``) — system
    # includes (``#include <...>``) are left for pcpp to handle (the
    # fake include dir has stubs for those). We keep ALL preprocessor
    # directives (``#define``, ``#ifdef``, ``#if``, ``#endif``, etc.)
    # intact so pcpp can still do macro expansion and conditional
    # compilation.
    try:
        # Log that we're ATTEMPTING pre-inline, even before calling
        # the function. This way, if the function raises an exception
        # or returns an empty string, the user still sees in the log
        # that pre-inline was tried. Without this, the only way to
        # tell whether pre-inline ran is by the ABSENCE of a message,
        # which is hard to spot in a long log.
        print(f"gtk2laz: [{os.path.basename(c_path)}] attempting "
              f"pre-inline of local headers...",
              file=sys.stderr)
        inlined_src = _inline_local_includes_only(c_path)
        if inlined_src and inlined_src.strip() \
                and len(inlined_src) > len(src):
            # Only use the inlined source if it's actually bigger
            # (i.e. at least one header was inlined). If no headers
            # were found (e.g. all includes are system includes), the
            # inlined source would be the same size as the original
            # and there's no point replacing it.
            src = inlined_src
            print(f"gtk2laz: [{os.path.basename(c_path)}] pre-inlined "
                  f"local headers ({len(src)} bytes, "
                  f"{len(src) - len(open(c_path, encoding='utf-8', errors='replace').read())} bytes added)",
                  file=sys.stderr)
        else:
            # pre-inline produced no output or output not bigger than
            # the original — log this so the user knows why no
            # "pre-inlined" message appeared.
            print(f"gtk2laz: [{os.path.basename(c_path)}] pre-inline "
                  f"found no local headers to inline "
                  f"(inlined={len(inlined_src) if inlined_src else 0} "
                  f"bytes, original={len(src)} bytes)",
                  file=sys.stderr)
    except Exception as _e:
        # If inlining fails, fall back to the original source — pcpp
        # will try to handle includes itself (which may not work on
        # Windows, but at least we tried).
        print(f"gtk2laz: [{os.path.basename(c_path)}] pre-inline "
              f"failed ({type(_e).__name__}: {_e}), using original "
              f"source for pcpp", file=sys.stderr)

    # ---- Windows / PyInstaller onefile ply-table workaround ----
    #
    # pcpp's ``parser.py`` calls ``lex.lex(optimize=in_production)`` where
    # ``in_production = 1``. When ``optimize=1``, ply tries to cache its
    # lexer/parser tables (``lextab.py`` / ``parsetab.py``) to disk —
    # usually in the directory of the calling module (``pcpp/``) or in
    # the current working directory.
    #
    # On Windows, when gtk2laz is distributed as a PyInstaller onefile
    # executable, the ``pcpp/`` package lives inside a read-only
    # ``_MEIPASS`` extraction directory. ply's attempt to write
    # ``parser.out`` / ``lextab.py`` there fails with ``PermissionError``
    # or ``OSError``, which propagates up as a generic ``Exception``
    # from ``pp.parse()``. The AST translator then silently falls back
    # to the pattern-based translator (which produces ~1100 TODOs
    # instead of ~0) and the user has no idea why their build broke.
    #
    # We fix this by:
    #   1. Monkey-patching ``pcpp.parser.in_production = 0`` BEFORE we
    #      instantiate the Preprocessor. With ``optimize=0``, ply keeps
    #      its tables in memory and never touches disk.
    #   2. Setting ``ply.yacc.yacc(errorlog=...)`` to suppress the
    #      noisy "Cannot create parser.out" warnings that would
    #      otherwise spam stderr on every conversion.
    #
    # The same fix is applied on Linux because there is no downside —
    # in-memory tables are slightly slower to build on the first run
    # but are rebuilt from scratch every time anyway, so the difference
    # is invisible. The big win is that Windows users no longer need
    # a writable ``pcpp/`` directory.
    try:
        import pcpp.parser as _pcpp_parser
        # Force ``optimize=0`` so ply never tries to write tables to
        # the read-only _MEIPASS directory. This is the main fix.
        _pcpp_parser.in_production = 0
    except Exception:
        # If pcpp.parser is not importable (older pcpp versions), skip
        # the patch — the user will see the original error in stderr.
        pass

    source_dir = os.path.dirname(os.path.abspath(c_path))
    pp = FakeIncludePreprocessor(fake_include_dir, source_dir)

    # ---- Apply the target-platform preset.
    #
    # The user can choose ``--platform=auto/windows/linux/macos`` on
    # the gtk2laz CLI (or in the GUI). ``"auto"`` resolves to the
    # running interpreter's platform (so on a Linux build host the
    # default is ``"linux"``; on a Windows build host it is
    # ``"windows"``). The chosen platform's preset macros are then
    # defined in pcpp BEFORE preprocessing starts, so the correct
    # branch of every ``#ifdef _WIN32`` / ``#ifdef __linux__`` /
    # ``#ifdef __APPLE__`` guard is taken in every header the source
    # includes.
    #
    # This replaces the previous heuristic that scanned the source
    # (and its sibling headers) for ``#ifdef _WIN32`` patterns. That
    # heuristic was fragile — it missed files that include enet.h
    # (which has the guard inside the header, not the .c file) and
    # required special-casing ``sys.platform == "win32"``. The
    # platform-preset approach is the same thing real C compilers do
    # (``gcc`` on Linux predefines ``__linux__``; ``cl`` on Windows
    # predefines ``_WIN32``), so it matches the user's mental model
    # exactly.
    #
    # ``target_platform="auto"`` uses ``_auto_platform()`` which
    # mirrors ``sys.platform``. If the user explicitly passes a
    # platform, that platform's preset is used regardless of the
    # host — so a Linux user can convert Windows-only source code
    # (e.g. enet's win32.c) by passing ``--platform=windows``.
    resolved_platform = target_platform or "auto"
    if resolved_platform == "auto":
        resolved_platform = _auto_platform()
    platform_defines = PLATFORM_PRESETS.get(resolved_platform, [])
    if platform_defines:
        print(f"gtk2laz: [{os.path.basename(c_path)}] using "
              f"target platform '{resolved_platform}' "
              f"({len(platform_defines)} preset defines)",
              file=sys.stderr)
    for d in platform_defines:
        try:
            pp.define(d)
        except Exception:
            pass

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
        # Common API-export macros that C libraries use to mark
        # public functions.  When the platform-specific header that
        # defines them (e.g. win32.h ``#define ENET_API __declspec(...)``
        # or unix.h ``#define ENET_API extern``) is not included (or
        # its #ifdef branch is not taken), the macro remains undefined
        # and pycparser sees ``ENET_API int foo()`` as a declaration
        # with type ``ENET_API`` and name ``int``.  We define these
        # macros to ``extern`` so they resolve to the standard C
        # linkage specifier and the function prototype parses cleanly.
        "ENET_API extern",
        "API extern",
        "EXPORT extern",
        "EXTERN extern",
        "DLLEXPORT extern",
        "DLLIMPORT extern",
        "WINAPI extern",
        "STDCALL extern",
        "CDECL extern",
        "CALLBACK extern",
        "IN ",
        "OUT ",
        "OPTIONAL ",
        "CONST const",
        "__int64 long long",
        "__int32 long",
        "__int16 short",
        "__int8 char",
        "__uint64 unsigned long long",
        "__uint32 unsigned long",
        "__uint16 unsigned short",
        "__uint8 unsigned char",
    ]
    for d in gnu_defines:
        try:
            pp.define(d)
        except Exception:
            pass

    # Apply extra defines passed by the user via ``--define`` on the
    # gtk2laz CLI. These are stored in the module-level
    # ``_GLOBAL_EXTRA_DEFINES`` list by ``set_extra_defines()`` so that
    # every ``preprocess()`` call picks them up without having to
    # thread the list through every function in the call chain
    # (convert → convert_c_file_with_includes → convert_c_file →
    # CFileTranslator → parse_with_ast → AstToPascal.parse → preprocess).
    all_extra = list(extra_defines or []) + list(_GLOBAL_EXTRA_DEFINES)
    if all_extra:
        for d in all_extra:
            try:
                pp.define(d)
            except Exception:
                pass

    # Add search paths so pcpp can resolve includes directly (in
    # addition to our on_include_not_found callback). We add BOTH the
    # fake include dir AND the source file's directory — the source dir
    # is needed for local headers like ``#include "enet/enet.h"`` where
    # the enet/ subdirectory is a child of the source dir.
    # Without the source dir on the search path, pcpp on Windows
    # sometimes fails to resolve local includes even when
    # on_include_not_found would succeed — pcpp's internal path
    # resolution has edge cases with backslash paths. Adding the dir
    # explicitly lets pcpp's own machinery handle it.
    pp.add_path(fake_include_dir)
    if source_dir:
        pp.add_path(source_dir)
        # Also add subdirectories one level deep (e.g. enet/ inside
        # the source dir) so pcpp can resolve includes like
        # ``#include "win32.h"`` from inside enet/enet.h. pcpp should
        # handle this via curdir, but on Windows this is sometimes
        # unreliable, so we pre-add the subdirectory.
        try:
            for entry in os.listdir(source_dir):
                subdir = os.path.join(source_dir, entry)
                if os.path.isdir(subdir):
                    pp.add_path(subdir)
        except OSError:
            pass

    # Parse the source
    try:
        pp.parse(src, source=c_path)
    except Exception as _e:
        # pcpp failed to parse the source. Common Windows-specific causes:
        #   * pcpp's embedded ply tries to write its parser table to a
        #     read-only location inside the PyInstaller _MEIPASS bundle.
        #   * The fake include directory does not exist yet (the caller
        #     should have called ``_ensure_fake_includes()`` first, but
        #     we still log the error here so the user can see what
        #     happened).
        # We print the failure reason to stderr so it is visible in the
        # console. Without this, the AST translator silently falls back
        # to the pattern-based translator (which produces ~1100 TODOs
        # instead of ~0) and the user has no idea why.
        # We still return ``None`` so the AST translator can try the
        # gcc/raw-source fallbacks, but at least the error is now
        # diagnosable.
        import sys as _sys
        print(f"gtk2laz: pcpp preprocessing failed for "
              f"{os.path.basename(c_path)}: {type(_e).__name__}: {_e}",
              file=_sys.stderr)
        return None

    # Write the preprocessed output
    import io
    out = io.StringIO()
    pp.write(out)
    text = out.getvalue()

    # ---- Strip \\r characters from the pcpp output.
    #
    # On Windows, the on-disk fake headers (in
    # ``%TEMP%\\fake_libc_include\\``) may have been written with
    # \\r\\n line endings — either by an older version of
    # gen_fake_includes.py (which used ``open(path, "w")`` without
    # ``newline=""``) or by the current version if the user's Python
    # installation has unusual defaults. When pcpp reads those files
    # (via our ``on_include_not_found`` callback which returns the
    # full path), it passes the \\r characters through to its output.
    # pycparser then rejects \\r with ``"Illegal character '\\r'"`` or
    # ``"Invalid declaration"``, causing the ENTIRE AST parse to fail
    # on every file that includes a system header.
    #
    # We strip \\r here (converting \\r\\n to \\n, and standalone \\r
    # to \\n) so pycparser always sees clean LF-only text regardless
    # of the host platform or the on-disk file's line endings.
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")

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

    # ---- Strip ``extern "C" { ... }`` blocks from the preprocessed text.
    #
    # C library headers (enet's enet.h, callbacks.h, list.h, and many
    # third-party C headers) wrap their public API in
    #   #ifdef __cplusplus
    #   extern "C" {
    #   #endif
    #   ... declarations ...
    #   #ifdef __cplusplus
    #   }
    #   #endif
    # so C++ callers get C linkage. When pcpp preprocesses with
    # ``__cplusplus`` UNDEFINED (the C path), the ``#ifdef __cplusplus``
    # blocks are removed entirely and ``extern "C"`` never appears in
    # the output — which is what we want for pycparser (a C parser
    # that cannot parse the C++ ``extern "C"`` syntax).
    #
    # However, there are two real-world cases where ``extern "C"``
    # STILL leaks into the pcpp output even with ``__cplusplus``
    # undefined:
    #
    #   1. The user explicitly defines ``__cplusplus`` (e.g. via
    #      ``--define __cplusplus=1``) — they might do this if they
    #      are converting C++ code that uses the ``extern "C"`` idiom
    #      to mark C-linkage functions.
    #
    #   2. A header writes ``extern "C"`` WITHOUT the ``#ifdef
    #      __cplusplus`` guard (some older headers do this when they
    #      are only ever included from C++ TUs). In that case pcpp
    #      passes the line through unchanged because there is no
    #      ``#ifdef`` to strip.
    #
    # In both cases pycparser aborts with ``Invalid declaration`` as
    # soon as it sees ``extern "C"``, which cascades into AST fallback
    # for the whole file. To be robust against these cases we strip
    # ``extern "C" { ... }`` blocks from the preprocessed text BEFORE
    # handing it to pycparser.
    #
    # We do this by scanning the text line-by-line:
    #   * A line matching ``extern "C"`` followed by optional ``{``
    #     opens a block — we remove that line.
    #   * The matching closing ``}`` for that block is removed too.
    #   * Lines INSIDE the block are kept (they are the actual C
    #     declarations we want pycparser to see).
    # This is safe because ``extern "C" { ... }`` is purely a linkage
    # specifier — removing it has no effect on the declarations inside.
    text = _strip_extern_c_blocks(text)

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


def _strip_extern_c_blocks(text: str) -> str:
    """Remove ``extern "C" { ... }`` wrappers and globalize their content.

    pycparser is a C parser and cannot handle the C++ ``extern "C"``
    linkage specifier. When a header wraps its public API in
    ``extern "C" { ... }`` (which is standard practice for C headers
    that want to be usable from C++), the specifier leaks into pcpp's
    output and breaks the AST parse.

    In C, ``extern "C" { ... }`` is purely a LINKAGE SPECIFIER — it does
    NOT introduce a new scope. Every declaration inside the block is
    a GLOBAL (file-scope) declaration, exactly as if the
    ``extern "C"`` and braces were not there. So the correct rewrite
    is to:
      1. Remove the ``extern "C"`` line.
      2. Remove the matching opening ``{``.
      3. Remove the matching closing ``}``.
      4. KEEP every line inside the block, but DE-INDENT it to column 0
         so it sits at the global scope (matching the surrounding
         declarations). This is important for the pattern where the
         header author indented the block's body for readability:
            extern "C" {
                void foo();    // 4-space indent
            }
         After stripping, ``void foo();`` should be at column 0, not
         still indented by 4 spaces. pycparser would parse either form
         correctly (it ignores leading whitespace), but the resulting
         coord.line numbers and the visual alignment of the preprocessed
         text should reflect the global scope.
      5. Lines that are NOT part of any ``extern "C"`` block are kept
         verbatim (including their original indentation).

    The function is careful NOT to touch ``extern "C"`` inside string
    or character literals (though this is extremely rare in real
    code). We track basic string/char state while scanning.

    The function handles the case where ``extern "C"`` and ``{`` are
    on the SAME line (e.g. ``extern "C" {``) and the case where they
    are on SEPARATE lines (e.g. line 1: ``extern "C"``, line 2: ``{``).

    ``#line`` directives inside the block are NOT de-indented (they
    start with ``#`` and pycparser uses them for coord.file tracking,
    so we must keep them exactly as pcpp emitted them).
    """
    if 'extern "C"' not in text:
        # Fast path: most preprocessed C text has no ``extern "C"``
        # at all (because ``__cplusplus`` is undefined, so the
        # ``#ifdef __cplusplus`` block is stripped by pcpp). Skip the
        # scan entirely.
        return text
    import re as _re_ec
    out_lines: list[str] = []
    # Stack of brace depths at which an ``extern "C"`` block was
    # opened. Each entry is the brace depth BEFORE the block's opening
    # ``{``. When we encounter a ``}`` that returns to that depth, we
    # know it's the block's closing brace and we drop it.
    extern_c_open_depths: list[int] = []
    brace_depth = 0
    # Track string/char literal state so we don't miscount braces
    # inside string literals (e.g. ``"foo { bar"``).
    in_string = False
    in_char = False
    escape = False
    # Pre-compile the regex that matches ``extern "C"`` (with
    # optional trailing ``{`` and whitespace). We match at the start
    # of the stripped line so we don't accidentally match inside a
    # longer identifier (e.g. ``my_extern "C"`` would not match).
    ec_re = _re_ec.compile(
        r'^\s*extern\s*"C"\s*(\{)?\s*$'
    )
    for line in text.splitlines():
        # Keep a copy of the line so we can scan it character-by-
        # character for brace counting even if we end up removing
        # the line from the output (because it's an ``extern "C"``
        # opener or a matching closer).
        line_for_scan = line
        # Check if this line opens an ``extern "C"`` block. We check
        # BEFORE updating the brace depth so the opener line itself
        # doesn't contribute to the count.
        m = ec_re.match(line)
        if m:
            # This line is an ``extern "C"`` opener. Remove it from
            # the output. If the ``{`` is on the SAME line, we need
            # to increment brace_depth AND record the depth so we
            # can find the matching ``}``.
            if m.group(1) == "{":
                # ``{`` is on the same line as ``extern "C"``.
                # The block's body starts at the NEXT line. Record
                # the depth so when we see a ``}`` that returns to
                # this depth, we know it's the block's closer.
                extern_c_open_depths.append(brace_depth)
                brace_depth += 1
            else:
                # ``{`` is NOT on this line — it should be on the
                # NEXT line. We'll handle it when we see it.
                # For now, just remember that we're expecting a
                # ``{`` next.
                # We use a sentinel depth of -1 to mean "expecting
                # ``{`` on the next non-empty line".
                extern_c_open_depths.append(-1)
            # Skip appending this line — it's the ``extern "C"``
            # opener and should not appear in pycparser's input.
            continue
        # If we're expecting a ``{`` (sentinel depth -1), check if
        # this line is just a ``{``.
        if extern_c_open_depths and extern_c_open_depths[-1] == -1:
            if line.strip() == "{":
                # This is the ``{`` that opens the ``extern "C"``
                # block. Replace the sentinel with the current depth
                # and increment the brace depth.
                extern_c_open_depths[-1] = brace_depth
                brace_depth += 1
                # Skip appending this line — it's the block opener.
                continue
            else:
                # The line after ``extern "C"`` was NOT a ``{``. The
                # header probably uses a non-standard form (e.g.
                # ``extern "C" void foo();`` without braces). In
                # that case, just strip the ``extern "C"`` prefix
                # from the line and keep the rest. Pop the sentinel
                # so we don't keep expecting a ``{``.
                extern_c_open_depths.pop()
                # Strip ``extern "C"`` prefix from the line (the
                # regex below matches the prefix at the start of
                # the line, leaving the rest intact).
                stripped_line = _re_ec.sub(
                    r'^\s*extern\s*"C"\s*', '', line, count=1
                )
                out_lines.append(stripped_line)
                # Still need to scan this line for braces.
                line_for_scan = stripped_line
        # ---- De-indent lines that are inside an extern "C" block.
        # In C, ``extern "C" { ... }`` does NOT introduce a new scope —
        # every declaration inside it is a global (file-scope)
        # declaration. So after removing the wrapper, the body should
        # be at column 0, matching the surrounding global declarations.
        # We de-indent ONLY when we are currently inside an extern "C"
        # block (i.e. there is at least one entry on the open-depths
        # stack with a non-negative depth). Lines outside any block
        # are kept verbatim with their original indentation.
        # We DO de-indent ``#line`` directives too — pcpp emits them
        # at column 0, but if the original header had them indented
        # inside the block, the indent would survive the pcpp pass and
        # end up in our text. pycparser accepts ``#`` at any column, so
        # functionally it does not matter, but normalizing to column 0
        # keeps the preprocessed text consistent.
        currently_inside = (
            extern_c_open_depths
            and extern_c_open_depths[-1] >= 0
        )
        if currently_inside:
            # De-indent: remove leading whitespace. We use lstrip()
            # which removes all leading whitespace (spaces + tabs).
            # This is correct because the block body was indented
            # RELATIVE to the global scope, so removing all indent
            # puts it back at column 0.
            deindented = line.lstrip()
            line_for_scan = deindented
            line = deindented
        # Scan the line character-by-character to update brace depth
        # and detect closing braces for open ``extern "C"`` blocks.
        # We also update string/char literal state so braces inside
        # string literals don't affect the count.
        i = 0
        n = len(line_for_scan)
        while i < n:
            ch = line_for_scan[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            elif in_char:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == "'":
                    in_char = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "'":
                    in_char = True
                elif ch == "{":
                    brace_depth += 1
                elif ch == "}":
                    brace_depth -= 1
                    # If this closing brace matches an open
                    # ``extern "C"`` block, drop this line.
                    if extern_c_open_depths \
                            and extern_c_open_depths[-1] == brace_depth:
                        extern_c_open_depths.pop()
                        # This line is the block's closing ``}``. If
                        # the line is JUST ``}`` (or whitespace +
                        # ``}``), we skip it entirely. If the line
                        # has other content after ``}`` (rare but
                        # possible), we keep the rest.
                        rest_after = line_for_scan[i + 1:].strip()
                        if not rest_after:
                            # Don't append this line — it's the
                            # closing ``}`` of the extern "C" block.
                            break
                        else:
                            # There's content after ``}``. Append
                            # just the content (drop the ``}``).
                            out_lines.append(rest_after)
                            break
            i += 1
        else:
            # The while loop completed without ``break`` — the line
            # was not a closing brace for an extern "C" block, so we
            # append it normally.
            out_lines.append(line)
            continue
        # If we broke out of the while loop (because the line WAS a
        # closing brace for an extern "C" block), we already handled
        # appending above. Don't append again.
        continue
    return "\n".join(out_lines)


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


# ---- Auto-injection of placeholder typedefs for *any* undefined
# identifier that starts with an uppercase letter.
#
# The previous implementation kept a hand-maintained ``WIN_TYPES`` set
# inside ``_inject_missing_gtk_typedefs`` listing the Windows-only
# types the fake ``windows.h`` might not cover (OVERLAPPED, PCSTR,
# PCWSTR, ...).  Every time a new third-party header introduced a new
# all-caps type, gtk2laz had to be edited to add the name to that set
# — otherwise pycparser aborted with "Missing type in declaration"
# and the whole file fell back to the pattern-based extractor.
#
# The current implementation drops that set and instead auto-injects a
# placeholder typedef for EVERY undefined identifier that starts with
# an uppercase letter, except for a small denylist of names that are
# known *not* to be types.  This way new Windows types (or any other
# all-caps type from a third-party header) are handled automatically,
# with no source edit required.
#
# ``_AUTO_INJECT_ALL_UPPERCASE`` is a feature flag so the new behavior
# can be turned off (set to ``False``) if it ever causes a regression
# — for example if a real type name collides with an enum constant
# that the enum-constant extraction misses.  Default: ``True``.
_AUTO_INJECT_ALL_UPPERCASE: bool = True


# ---------------------------------------------------------------------------
# Brace-depth-aware helpers
# ---------------------------------------------------------------------------
#
# All the regex-based passes below (Pass 1-7) used to be one-line regexes
# that scanned the entire preprocessed text.  That worked OK in the common
# case, but it broke spectacularly on real-world source like opgui's
# ``resources.c`` which contains a 14,000-line ``static const SECTION
# union { const guint8 data[111593]; ... } resources_resource_data = {
# { ... };`` declaration.  The regex would happily match identifiers
# *inside* the union body (``data``, ``alignment``, ``ptr``) and add them
# to ``already_defined``, which then suppressed the auto-inject pass
# where it shouldn't have.  Worse, multi-line declarations that span
# several lines (a function body containing a typedef, or a struct
# definition containing a function-pointer typedef) were not handled at
# all.
#
# The helpers below do a proper depth-aware walk of the C source:
#
#   * ``_strip_brace_bodies(text)`` returns a copy of ``text`` where
#     every ``{ ... }`` block (at any nesting depth) is replaced by
#     a single space.  This is what most passes want: they need to
#     scan the *top-level* declarations only, not the contents of
#     struct/union/enum bodies or function bodies.
#
#   * ``_iter_top_level_decls(text)`` walks ``text`` and yields each
#     top-level declaration as a string, where a "top-level
#     declaration" is everything from a ``;``, ``}``, ``{`` boundary
#     at depth 0 up to the next ``;`` at depth 0.  This handles
#     multi-line typedefs and variable declarations correctly.
#
#   * ``_iter_top_level_typedefs(text)`` is a thin wrapper that
#     yields only the declarations that start with ``typedef``.
#
# All three helpers preserve the *positions* of the kept characters so
# the original text can be re-referenced if needed.  They are also
# string/comment-aware (they do not split on ``;`` or ``{`` inside
# string literals or char literals), because the preprocessed text
# may still contain string literals (e.g. ``"..."`` from
# ``fprintf(stderr, "...")``).
#
# IMPORTANT: these helpers do NOT need to handle ``#if`` / ``#define``
# / ``#include`` directives — pcpp has already expanded those away
# before we get the text.  They also do NOT need to handle C++-style
# ``//`` comments — pycparser's input has already been
# comment-stripped by the time we run.

import re as _re_mod  # noqa: E402  (avoid circular-import issues)


def _strip_brace_bodies(text: str) -> str:
    """Return a copy of ``text`` with every ``{ ... }`` block (at any
    nesting depth), every comment (``/* ... */`` and ``// ...``),
    AND every string / char literal (``"..."`` and ``'...'``)
    replaced by a single space.

    Braces, semicolons and quotes inside comments / string literals /
    char literals do NOT count toward the depth counter or affect the
    literal state.

    Comments and string / char literals are removed (replaced with a
    single space) because subsequent identifier scans (Pass 1-7 and
    the auto-inject pass) should NOT extract identifiers from them.
    This is critical:
      * A comment like ``/* This is a Foo struct */`` would otherwise
        cause ``This`` and ``Foo`` to be added to ``already_defined``,
        suppressing the auto-inject pass where it shouldn't.
      * A string literal like ``"DS1820"`` in opgui's resources.c
        would otherwise cause ``DS1820`` to be auto-injected as a
        typedef, which is wrong (it's a string identifier, not a
        type name).

    This is used by all the regex-based passes (Pass 1, 2, 4, 6, 7)
    so that they only scan *top-level* declarations, never the
    contents of struct/union/enum bodies, function bodies, comments,
    or string / char literals.
    """
    if not text:
        return text
    out: list[str] = []
    depth = 0
    in_str = False        # inside a "..." string literal
    in_chr = False        # inside a '...' char literal
    in_block_comment = False  # inside a /* ... */ comment
    in_line_comment = False   # inside a // ... comment
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # ---- Line comment: skip until end-of-line ---------------
        # We replace the comment with a single space (so identifiers
        # in it are NOT picked up by subsequent regex scans).
        if in_line_comment:
            if ch == "\n":
                # The newline terminates the comment but is itself a
                # whitespace char, so we keep it (don't drop it).
                in_line_comment = False
                out.append(ch)
            i += 1
            continue
        # ---- Block comment: skip until closing ``*/`` -----------
        # We replace the comment with a single space.
        if in_block_comment:
            if ch == "*" and i + 1 < n and text[i + 1] == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        # ---- String literal -------------------------------------
        # We replace the string literal with a single space, so
        # identifiers inside string literals (e.g. ``"DS1820"`` in
        # opgui's resources.c) are NOT picked up by the auto-inject
        # pass's identifier scan.
        if in_str:
            if ch == "\\":
                # skip the next char (it's escaped)
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        # ---- Char literal ----------------------------------------
        # Same treatment as string literals: replace with a single
        # space so identifiers inside them are not picked up.
        if in_chr:
            if ch == "\\":
                i += 2
                continue
            if ch == "'":
                in_chr = False
            i += 1
            continue
        # ---- Not inside any literal/comment: look for starters --
        # Line comment ``//`` — check before ``/`` so a single ``/``
        # in ``a / b`` doesn't trigger it.
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            in_line_comment = True
            out.append(" ")  # replace comment with single space
            i += 2
            continue
        # Block comment ``/*``
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            in_block_comment = True
            out.append(" ")  # replace comment with single space
            i += 2
            continue
        if ch == '"':
            in_str = True
            out.append(" ")  # replace string with single space
            i += 1
            continue
        if ch == "'":
            in_chr = True
            out.append(" ")  # replace char literal with single space
            i += 1
            continue
        if ch == "{":
            depth += 1
            # Replace the entire brace body with a single space.
            # We do NOT emit the ``{`` — instead we emit a space so
            # the surrounding tokens (e.g. ``struct Foo`` and the
            # declarator ``x`` in ``struct Foo { ... } x;``) stay
            # separated by whitespace.
            out.append(" ")
            i += 1
            continue
        if ch == "}":
            if depth > 0:
                depth -= 1
            # Drop the ``}`` — its matching ``{`` was already
            # replaced by a space.
            i += 1
            continue
        if depth > 0:
            # We're inside a brace body — drop this char.
            i += 1
            continue
        # Top level — keep the char.
        out.append(ch)
        i += 1
    return "".join(out)


def _iter_top_level_decls(text: str):
    """Yield each top-level declaration from ``text``.

    A "top-level declaration" is the slice of text from the end of the
    previous top-level declaration up to the next ``;`` OR ``}`` at
    brace-depth 0.  Treating ``}`` as a declaration boundary is
    critical for function definitions, which end with ``}`` rather
    than ``;``.  Without this, a function definition like
    ``void f() { ... }`` followed by another function definition
    ``void g() { ... }`` would be merged into a single declaration,
    and the per-declaration scans (Pass 5, Pass 7) would not be able
    to inspect them separately.

    String literals, char literals, block comments and line
    comments are all respected — braces, semicolons and quotes inside
    them do NOT count toward the depth counter or terminate a
    declaration.  This matters for real C source where a string
    like ``"hello; world"`` would otherwise truncate the declaration.

    The yielded strings are NOT trimmed of leading/trailing whitespace
    — the caller can do that if needed.
    """
    if not text:
        return
    start = 0
    depth = 0
    in_str = False
    in_chr = False
    in_block_comment = False
    in_line_comment = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # ---- Line comment: skip until end-of-line ---------------
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        # ---- Block comment: skip until closing ``*/`` -----------
        if in_block_comment:
            if ch == "*" and i + 1 < n and text[i + 1] == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        # ---- String literal -------------------------------------
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        # ---- Char literal ----------------------------------------
        if in_chr:
            if ch == "\\":
                i += 2
                continue
            if ch == "'":
                in_chr = False
            i += 1
            continue
        # ---- Not inside any literal/comment: look for starters --
        # Line comment ``//``
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            in_line_comment = True
            i += 2
            continue
        # Block comment ``/*``
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            in_block_comment = True
            i += 2
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == "'":
            in_chr = True
            i += 1
            continue
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            if depth > 0:
                depth -= 1
            # If this ``}`` closes a top-level block (function body,
            # struct/union/enum body with a declarator, etc.), it MAY
            # terminate a declaration.  But it might also be the end
            # of a struct/union/enum body that is immediately followed
            # by a declarator (e.g. ``struct Foo { ... } var = ...;``
            # or ``struct Foo { ... } arr[N];``).  We need to peek
            # ahead to decide:
            #   - If the next non-whitespace char is ``;`` or another
            #     decl starter (e.g. a keyword or ``}`` itself or
            #     EOF), this ``}`` terminates a function definition
            #     or a pure struct/union/enum tag declaration.
            #   - If the next non-whitespace char is ``=``, ``[``,
            #     ``,``, or an identifier (continuation of the
            #     declarator), this ``}`` is the end of the
            #     struct/union/enum body but NOT the end of the
            #     declaration — keep accumulating.
            if depth == 0:
                # Peek ahead for the next non-whitespace char.
                j = i + 1
                while j < n and text[j] in " \t\n\r":
                    j += 1
                next_ch = text[j] if j < n else ""
                # If the next char is one of these, the ``}`` is the
                # end of a struct/union/enum body that continues into
                # a declarator (e.g. ``struct Foo { ... } var = ...;``).
                # We do NOT yield — the declaration continues.
                if next_ch in {"=", "[", ",", "(", "*"}:
                    i += 1
                    continue
                # If the next char is an identifier character, it
                # could be either:
                #   (a) a declarator continuing the struct/union/enum
                #       declaration (e.g. ``struct Foo { ... } var;`` or
                #       ``struct Foo { ... } *ptr;``), OR
                #   (b) a new declaration starting after a function
                #       definition (e.g. ``void f() { ... } int bar;``).
                # We distinguish these by reading the full identifier
                # and checking if it is a C storage/type keyword.  If
                # it IS a keyword (int, void, char, struct, extern,
                # static, etc.), the ``}`` terminates a function
                # definition and the identifier starts a new
                # declaration.  If it is NOT a keyword, it's a
                # declarator name continuing the current declaration.
                if next_ch.isalpha() or next_ch == "_":
                    # Read the full identifier starting at j.
                    k = j
                    while k < n and (text[k].isalnum() or text[k] == "_"):
                        k += 1
                    ident = text[j:k]
                    # C storage-class and type keywords that can start
                    # a new top-level declaration.  If the identifier
                    # is one of these, the ``}`` terminates the
                    # current declaration (function definition or
                    # struct/union/enum with body).
                    _C_DECL_START_KEYWORDS = {
                        "int", "char", "short", "long", "float",
                        "double", "void", "unsigned", "signed",
                        "struct", "union", "enum",
                        "extern", "static", "auto", "register",
                        "volatile", "const",
                        "typedef",
                        # Common typedef'd types from our fake headers
                        # that appear at the start of declarations.  We
                        # include these so a function definition
                        # followed by ``GtkWidget *foo;`` (which starts
                        # with the typedef name ``GtkWidget``) is
                        # correctly split.  This is conservative — if
                        # we miss a typedef name, the worst case is
                        # that two decls are merged into one (which
                        # Pass 5/7 can still handle).
                        "size_t", "ssize_t", "gboolean",
                        "GtkWidget", "GtkWindow", "GObject", "GList",
                        "DWORD", "HANDLE", "HWND", "HMODULE", "HDC",
                        "BOOL", "BYTE", "WORD", "ULONG", "LONG",
                        "INT", "UINT", "FILE",
                        # ``inline`` and ``restrict`` can also start
                        # a declaration in C99.
                        "inline", "restrict",
                    }
                    if ident in _C_DECL_START_KEYWORDS:
                        # The identifier is a C keyword — this ``}``
                        # terminates a function definition.  Yield it.
                        yield text[start:i + 1]
                        start = i + 1
                        i += 1
                        continue
                    # Not a keyword — the declarator continues
                    # (e.g. ``struct Foo { ... } var;``).
                    i += 1
                    continue
                # If the next char is ``;``, this ``}`` is the end of
                # a struct/union/enum tag declaration
                # (``struct Foo { ... };``) or a function definition
                # (``void f() { ... };`` — rare but valid C).  In this
                # case we do NOT yield at the ``}`` — we let the ``;``
                # yield the declaration, so the ``;`` is included in
                # the yielded text.  This avoids splitting
                # ``struct Foo { ... };`` into two decls
                # (``struct Foo { ... }`` + ``;``).
                if next_ch == ";":
                    i += 1
                    continue
                # Otherwise (``}`` (another function/struct following),
                # EOF, or any other char), this ``}`` terminates the
                # declaration.  Yield it.  This handles function
                # definitions like ``void f() { ... }`` followed by
                # another function ``void g() { ... }`` (no ``;``
                # between them).
                yield text[start:i + 1]
                start = i + 1
            i += 1
            continue
        if ch == ";" and depth == 0:
            yield text[start:i + 1]
            start = i + 1
            i += 1
            continue
        i += 1
    # Trailing text without a terminating ``;`` or ``}`` (rare, but
    # defensive).
    if start < n:
        trailing = text[start:].strip()
        if trailing:
            yield text[start:]


def _iter_top_level_typedefs(text: str):
    """Yield only the top-level declarations that start with ``typedef``.

    The yielded strings are the full declaration text (including the
    leading ``typedef`` and trailing ``;``).

    ``#line N "file"`` directives emitted by pcpp/pycparser between
    fake-header chunks are stripped before the typedef check, so a
    declaration like ``#line 1 "gtk/gtk.h"\\ntypedef struct _GtkWidget
    GtkWidget;`` is correctly recognized as a typedef.
    """
    for decl in _iter_top_level_decls(text):
        # Strip leading ``#line N "file"`` directives (and any other
        # ``#``-prefixed lines) before the typedef check.
        s = decl.lstrip()
        while True:
            nl = s.find("\n")
            if nl == -1:
                # Single-line declaration.  If it starts with ``#``,
                # the whole thing is a preprocessor line — not a typedef.
                if s.startswith("#"):
                    s = ""
                break
            first_line = s[:nl]
            if first_line.lstrip().startswith("#"):
                s = s[nl + 1:].lstrip()
                continue
            break
        if s.startswith("typedef"):
            yield s


# Identifiers that look like type names (start with an uppercase
# letter) but should NOT be auto-injected as placeholder typedefs
# because they are C keywords, common enum/macro constants, or
# preprocessor macros that should have been expanded away by pcpp.
#
# The set is intentionally small: we only list names that we are
# confident are NEVER type names in real C code.  Anything not in
# this set falls through to auto-injection, which is the safe default
# (a stray ``typedef struct _Foo Foo;`` is harmless; a stray typedef
# for ``TRUE`` would break ``enum { TRUE, FALSE };``).
#
# Order of categories:
#   1. C keywords (uppercase variants) — these appear in macro
#      contexts (e.g. ``#define IF(x) if(x)``) and would be nonsense
#      as type names.
#   2. C standard library constants — usually ``#define``'d but may
#      survive as enum constants when the source uses ``enum``.
#   3. Microsoft Win32 macros — usually ``#define``'d to nothing in
#      the fake ``windows.h``.  Listed defensively in case the fake
#      header is stale or absent.
#   4. Common GLib/GTK macros — defensive, same reason.
_NON_TYPE_DENYLIST: frozenset[str] = frozenset({
    # ---- C keywords (uppercase variants) ----
    "AUTO", "BREAK", "CASE", "CHAR", "CONST", "CONTINUE", "DEFAULT",
    "DO", "DOUBLE", "ELSE", "ENUM", "EXTERN", "FLOAT", "FOR", "GOTO",
    "IF", "INLINE", "INT", "LONG", "REGISTER", "RESTRICT", "RETURN",
    "SHORT", "SIGNED", "SIZEOF", "STATIC", "STRUCT", "SWITCH",
    "TYPEDEF", "UNION", "UNSIGNED", "VOID", "VOLATILE", "WHILE",
    "BOOL",
    # ---- C standard library constants (common enum/macro form) ----
    "NULL", "TRUE", "FALSE", "EOF", "BUFSIZ", "SEEK_SET", "SEEK_CUR",
    "SEEK_END", "EXIT_SUCCESS", "EXIT_FAILURE", "STDIN", "STDOUT",
    "STDERR", "M_PI", "M_E", "M_SQRT2", "MAXFLOAT", "HUGE_VAL",
    "INFINITY", "NAN", "DBL_MAX", "DBL_MIN", "FLT_MAX", "FLT_MIN",
    "INT_MAX", "INT_MIN", "UINT_MAX", "LONG_MAX", "LONG_MIN",
    "ULONG_MAX", "SHRT_MAX", "SHRT_MIN", "USHRT_MAX", "CHAR_MAX",
    "CHAR_MIN", "UCHAR_MAX", "SCHAR_MAX", "SCHAR_MIN",
    "PATH_MAX", "MAX_PATH", "NAME_MAX", "FILENAME_MAX", "FOPEN_MAX",
    "TMP_MAX", "L_TMPNAME",
    # ---- Microsoft Win32 macros ----
    "WINAPI", "STDCALL", "CALLBACK", "APIENTRY", "APIPRIVATE",
    "WINAPIV", "WSAAPI", "IN", "OUT", "OPTIONAL", "FAR", "NEAR",
    "DECLSPEC_IMPORT", "DECLSPEC_EXPORT",
    # ---- Common GLib / GTK macros ----
    # (Most GLib/GTK macros start with ``G_`` and thus do not match
    # the ``[A-Z][A-Za-z0-9_]+`` regex; we still list the few that
    # would, defensively.)
})


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
    """Scan ``text`` for type-name identifiers and emit placeholder
    typedefs for any that are not already defined.

    Without placeholder typedefs, pycparser fails with ``Invalid
    declaration`` at the first unknown type and the whole AST parse
    aborts, leaving the converter to fall back to the pattern-based
    extractor (which only finds ~half the functions in a typical GTK
    app's source).  This function keeps pycparser happy so the AST
    parse succeeds and every function body is extracted.

    Two classes of identifiers are handled:

      1. **GTK / GDK / GLib type names** — every word-boundary
         identifier starting with ``Gtk`` or ``Gdk`` (e.g.
         ``GtkWidget``, ``GdkEventButton``), or with the GLib prefix
         ``G`` followed by an uppercase letter then a lowercase letter
         (e.g. ``GObject``, ``GList``, ``GSourceFunc``).  These are
         the most common cases in real GTK apps.

      2. **ANY other undefined identifier starting with an uppercase
         letter** — handled by the auto-inject pass.  This is a strict
         superset of the previous hand-maintained ``WIN_TYPES`` set:
         every new Windows-only type (or any other all-caps type from a
         third-party header) is handled automatically, with no source
         edit required.

    The function:

      1. Collects every name already typedef'd in ``text`` and adds
         it to an ``already_defined`` set.
      2. Collects every name declared as an **enum constant** inside
         ``enum { ... }`` blocks so we don't accidentally turn it into
         a struct typedef and break the enum declaration.
      3. Scans for word-boundary identifiers starting with ``Gtk``,
         ``Gdk`` or ``G<Uppercase><lowercase>`` and adds each that is
         not in ``already_defined`` to the candidates list.
      4. Scans for word-boundary identifiers starting with an
         uppercase letter and adds each that is not in
         ``already_defined``, not in ``_NON_TYPE_DENYLIST``, and not
         in the enum-constant set to the candidates list.
      5. Prepends one placeholder typedef per candidate:
           ``typedef struct _<NAME> <NAME>;`` for struct types, or
           ``typedef int (*<NAME>)(void);`` for names ending in
           ``Func``/``Callback``/``Notify``/``Destroy`` so they can
           appear in cast expressions like ``(GtkTreeModelForeachFunc)fn``.

    The feature flag ``_AUTO_INJECT_ALL_UPPERCASE`` (default:
    ``True``) controls step 4.  Setting it to ``False`` reverts to
    the legacy behaviour where only Gtk/Gdk/GLib-prefixed types and
    the explicit ``WIN_TYPES`` set are injected — useful as a
    fallback if the auto-inject ever causes a regression.
    """
    if not text:
        return text
    # Collect every name already typedef'd in the text so we don't
    # duplicate.  We look for several common typedef forms:
    #   1. ``typedef struct _X X;`` (struct typedef, no body)  — first regex
    #   2. ``typedef <stuff> NAME;`` (simple typedef, e.g. ``typedef int gboolean;``) — second regex
    #   3. ``typedef struct|union|enum [tag] { ... } NAME [, *NAME2, ...];`` (typedef with body) — third regex (NEW)
    #   4. ``typedef int (*NAME)(...);`` (function-pointer typedef) — fourth regex (NEW)
    #
    # The third and fourth regexes are needed because the previous
    # implementation only handled cases 1 and 2.  With the new
    # auto-inject pass (which injects *any* undefined uppercase-initial
    # identifier as a placeholder typedef), cases 3 and 4 became
    # important: if ``typedef struct _Foo { int x; } Foo;`` is in the
    # text but we miss it, the auto-inject will emit a duplicate
    # ``typedef struct _Foo Foo;`` and pycparser will abort with
    # "redefinition of typedef 'Foo'".
    already_defined: set[str] = set()
    # ---- Brace-aware extraction of typedef'd names -----------------
    #
    # We iterate over top-level declarations only (using
    # ``_iter_top_level_typedefs``) so typedefs hidden inside function
    # bodies or struct bodies do not pollute ``already_defined``.
    # This is the key fix: the previous regex-based passes (Pass 1-4)
    # would happily match ``typedef`` inside a struct body or function
    # body, causing false positives.
    #
    # For each top-level typedef declaration, we extract the typedef
    # name(s) using a small state machine that walks the declaration
    # text:
    #   - Skip over ``{ ... }`` bodies (struct/union/enum field lists).
    #   - Skip over ``(...)`` parenthesised groups (function-pointer
    #     typedefs).
    #   - Capture the last identifier before each comma at depth 0,
    #     and the last identifier before the terminating ``;``.
    # This handles ALL the forms the previous Passes 1-4 covered
    # (simple typedefs, struct typedefs with body, function-pointer
    # typedefs, multi-alias typedefs) in one unified, depth-aware
    # pass.
    for decl in _iter_top_level_typedefs(text):
        # Walk the declaration, skipping over { ... } and ( ... ),
        # collecting the LAST identifier in each comma-separated
        # declarator.  We also collect every identifier that appears
        # after a closing ``}`` (these are the typedef alias names in
        # ``typedef struct _X { ... } X, *PX;``).
        decl_stripped = _strip_brace_bodies(decl)
        # ``decl_stripped`` now has all ``{ ... }`` bodies replaced
        # by a single space.  We can split on top-level commas and
        # take the last identifier in each piece.
        # Drop the leading ``typedef`` keyword (we don't want it as
        # a typedef name).
        decl_no_kw = _re_mod.sub(r"^\s*typedef\s+", "", decl_stripped)
        # Walk decl_no_kw, splitting on top-level commas (depth 0
        # of parens — there should be no braces left after
        # _strip_brace_bodies).
        pieces: list[str] = []
        depth = 0
        cur = ""
        for ch in decl_no_kw:
            if ch == "(":
                depth += 1
                cur += ch
            elif ch == ")":
                depth = max(0, depth - 1)
                cur += ch
            elif ch == "," and depth == 0:
                pieces.append(cur)
                cur = ""
            else:
                cur += ch
        if cur:
            pieces.append(cur)
        for piece in pieces:
            # Strip array subscripts (``[...]``) so we don't
            # capture array size identifiers.
            piece = _re_mod.sub(r"\[[^\]]*\]", "", piece)
            # Strip trailing ``;``.
            piece = piece.rstrip("; \t\n")
            if not piece:
                continue
            # Take the LAST identifier in the piece.
            idents = _re_mod.findall(r"\b([A-Za-z_]\w*)\b", piece)
            if not idents:
                continue
            already_defined.add(idents[-1])
    # Pass 5 (NEW): variable declarations of the form
    # ``[extern|static|...] <type> NAME [,...];`` or
    # ``<type> *NAME[N] = ...;`` — these declare NAME as a
    # VARIABLE, not a type.  Without this pass, the auto-inject
    # path would inject ``typedef struct _STR_ID STR_ID;`` for
    # ``extern char *STR_ID[64];`` (a real opgui.c case) and
    # pycparser would then abort with
    # "Non-typedef 'STR_ID' previously declared as typedef in this scope".
    #
    # This pass is critical for source that does not follow the C
    # convention of lowercase variable names — for example opgui
    # declares ``char *STR_ID[DIM];`` and ``struct DEVICES DEVLIST[];``
    # as global variables with all-uppercase names, which the
    # auto-inject regex would otherwise mistake for undefined types.
    #
    # STRATEGY (brace-aware):
    # We walk every top-level declaration (using
    # ``_iter_top_level_decls``) and check whether it looks like a
    # variable declaration.  We use ``_strip_brace_bodies`` to
    # remove struct/union/enum bodies and function bodies BEFORE
    # inspecting the declaration, so identifiers inside those bodies
    # (e.g. ``data`` in ``union { guint8 data[N]; }``) do NOT get
    # added to ``already_defined``.
    #
    # We skip declarations that start with ``typedef`` (already
    # handled by the typedef pass above) and declarations that look
    # like function definitions / declarations (those are handled
    # separately by Pass 7).
    #
    # For each surviving variable declaration we capture the LAST
    # identifier in each comma-separated declarator.
    _STORAGE_KW_RE = _re_mod.compile(
        r"(?:extern|static|auto|register|volatile|const|"
        r"signed|unsigned|"
        r"void|char|short|int|long|float|double|"
        r"struct|union|enum)\b"
    )
    _FUNC_DECL_RE = _re_mod.compile(
        # Function declaration: IDENT (...) ; or { ... }
        r"\b[A-Za-z_]\w*\s*\([^;{}]*\)\s*[;{]"
    )
    _INIT_EQ_RE = _re_mod.compile(r"(?<![<>=!])=")

    for decl in _iter_top_level_decls(text):
        # Strip leading/trailing whitespace for easier matching.
        decl_strip = decl.strip()
        if not decl_strip:
            continue
        # Strip leading ``#line N "file"`` directives (pycparser
        # emits these between fake-header chunks and the user
        # source).  Without this strip, a declaration like
        # ``#line 1 "<fake_header_end>"\nGtkWidget *ICD_check;``
        # would be skipped by the ``startswith("#")`` check below,
        # and ``ICD_check`` would not be added to ``already_defined``,
        # causing the auto-inject pass to wrongly inject
        # ``typedef struct _ICD_check ICD_check;``.
        # We strip every leading line that starts with ``#``.
        while True:
            nl = decl_strip.find("\n")
            if nl == -1:
                # Single-line declaration.  If it starts with ``#``,
                # the whole thing is a preprocessor line — skip it.
                if decl_strip.startswith("#"):
                    decl_strip = ""
                break
            first_line = decl_strip[:nl]
            if first_line.lstrip().startswith("#"):
                decl_strip = decl_strip[nl + 1:].lstrip()
                continue
            break
        if not decl_strip:
            continue
        # Skip typedefs (handled by the typedef pass above).
        if decl_strip.startswith("typedef"):
            continue
        # Skip remaining preprocessor lines.
        if decl_strip.startswith("#"):
            continue
        # Skip if the declaration does not start with a storage/type
        # keyword AND does not start with a previously-typedef'd name
        # (the typedef'd-name case is handled by Scan (b) below).
        first_token = decl_strip.split(None, 1)[0] if decl_strip else ""
        starts_with_storage = bool(_STORAGE_KW_RE.match(decl_strip))
        starts_with_typedef_name = (
            first_token in already_defined
            and first_token not in {
                "extern", "static", "auto", "register", "volatile",
                "const", "signed", "unsigned", "void", "char", "short",
                "int", "long", "float", "double", "struct", "union",
                "enum",
            }
        )
        if not starts_with_storage and not starts_with_typedef_name:
            continue
        # Brace-aware strip: remove { ... } bodies so identifiers
        # inside struct/union/enum bodies don't pollute already_defined.
        # This is the key fix: a line like
        #   static const SECTION union { const guint8 data[111593]; ... }
        #   resources_resource_data = { ... };
        # becomes (after stripping):
        #   static const SECTION union   resources_resource_data =  ;
        # which lets us correctly capture ``resources_resource_data``
        # as the variable name (not ``data``).
        decl_no_braces = _strip_brace_bodies(decl_strip)
        # Quick reject: if the declaration looks like a function
        # declaration (``IDENT(...) ;`` or ``IDENT(...) { ... }``),
        # skip it — function names are handled by Pass 7.
        # We strip the initializer (everything from the first ``=`` to
        # the end of the declaration) before checking, because an
        # initializer expression can contain ``sizeof(...)`` calls
        # that would false-trigger the function-decl pattern.
        pre_init = _INIT_EQ_RE.split(decl_no_braces, maxsplit=1)[0]
        if _FUNC_DECL_RE.search(pre_init):
            continue
        # Now extract the variable name(s).  We split on top-level
        # commas (depth 0 of parens — there should be no braces left
        # after _strip_brace_bodies) and take the LAST identifier in
        # each comma-separated declarator.
        #
        # CRITICAL: we must split on commas BEFORE stripping the array
        # subscript.  Otherwise a line like
        # ``char* Slabel[LMAX],*Sulabel[ULMAX];`` (a real icd.c case
        # from opgui) would have its first ``[`` (in ``Slabel[LMAX]``)
        # cut the line at ``char* Slabel``, losing the second
        # declarator ``*Sulabel[ULMAX]`` entirely.  This would cause
        # the auto-inject pass to inject
        # ``typedef struct _Sulabel Sulabel;`` — exactly the icd.c
        # failure we're trying to fix.
        # So: we split on top-level commas FIRST, then for each piece
        # we strip the array subscript and take the last identifier.
        # Strip the initializer (everything from the first ``=`` to
        # the end) BEFORE splitting on commas, so commas inside an
        # initializer expression (e.g. ``int x = (a, b);`` — rare but
        # possible) don't split the declarator.
        decl_before_init = _INIT_EQ_RE.split(decl_no_braces, maxsplit=1)[0]
        # Split on top-level commas (depth 0 of parens).
        pieces: list[str] = []
        depth = 0
        cur = ""
        for ch in decl_before_init:
            if ch == "(":
                depth += 1
                cur += ch
            elif ch == ")":
                depth = max(0, depth - 1)
                cur += ch
            elif ch == "," and depth == 0:
                pieces.append(cur)
                cur = ""
            else:
                cur += ch
        if cur:
            pieces.append(cur)
        # SPECIAL CASE: ``enum|struct|union TAG { ... };`` (with NO
        # declarator after the closing ``}``) is a *tag declaration*,
        # not a variable declaration.  The TAG is the enum/struct/union
        # tag name, not a variable.  We must NOT add TAG to
        # ``already_defined`` because the user's source may then use
        # ``TAG x;`` (without the ``enum`` prefix) — pycparser will
        # reject that unless TAG is also typedef'd.  The auto-inject
        # pass should therefore be allowed to inject
        # ``typedef struct _TAG TAG;`` for these cases.
        #
        # HOWEVER: when there IS a separate declarator after the tag
        # (e.g. ``struct ID_OW { ... } OW_LIST[] = {...};``), the TAG
        # is already declared as a struct/union/enum tag by the source
        # itself, so we MUST add it to ``already_defined`` so the
        # auto-inject pass does NOT inject a duplicate typedef for it
        # (which would clash with the struct tag and cause pycparser
        # to abort with "Non-typedef 'ID_OW' previously declared as
        # typedef in this scope" — the exact opgui progEEPROM.c case).
        # We detect this by looking at the FIRST piece only (which
        # contains the struct/union/enum keyword and tag).
        if pieces:
            # Strip trailing ``;`` from the first piece before
            # tokenizing, so ``enum Color  ;`` (after brace stripping)
            # becomes ``enum Color`` and has only 2 tokens.  Without
            # this strip, the ``;`` would be counted as a third
            # token, making ``has_var`` incorrectly True, and the
            # tag ``Color`` would be added to ``already_defined``,
            # suppressing the auto-inject that the user's source needs
            # (e.g. ``Color c;`` without the ``enum`` prefix).
            first_piece_clean = pieces[0].rstrip(";").strip()
            first_piece_tokens = first_piece_clean.split()
            if (len(first_piece_tokens) >= 2
                    and first_piece_tokens[0] in ("struct", "union", "enum")
                    and first_piece_tokens[1] not in ("struct", "union", "enum")):
                tag_name = first_piece_tokens[1]
                # Check if there's a variable-name token AFTER the tag
                # in the first piece, or if there are additional
                # comma-separated declarators.
                has_var = (
                    len(first_piece_tokens) > 2
                    or len(pieces) > 1
                )
                if not has_var:
                    # Pure tag declaration like ``enum Color { ... };``.
                    # Do NOT add the tag to already_defined — the
                    # auto-inject may need to typedef it so the user
                    # can write ``Color c;`` (without the ``enum``
                    # prefix).
                    continue
                # Has a separate declarator — the TAG is declared by
                # this very statement, so add it to already_defined.
                # This prevents the auto-inject pass from injecting a
                # duplicate typedef for the tag (which would clash
                # with the struct tag and abort pycparser).
                already_defined.add(tag_name)
                # Drop the ``struct|union|enum TAG`` prefix from the
                # first piece, leaving only the declarator(s).
                pieces[0] = " ".join(first_piece_tokens[2:])
                # Filter out empty pieces.
                pieces = [p for p in pieces if p.strip()]
        # Now for each piece, strip array subscripts and take the
        # LAST identifier.
        for piece in pieces:
            # Strip array subscripts (``[...]``) so we don't capture
            # array size identifiers.
            piece = _re_mod.sub(r"\[[^\]]*\]", "", piece)
            idents = _re_mod.findall(r"\b([A-Za-z_]\w*)\b", piece)
            if not idents:
                continue
            already_defined.add(idents[-1])
    # Pass 6 (NEW): ``#define NAME ...`` object-like and function-like
    # macros.  After pcpp preprocessing, ``#define MinDly 0`` expands
    # away, but pycparser may still see references to ``MinDly`` if
    # the source also uses it in a context that pcpp didn't expand
    # (e.g. inside a comment-stripped region or in the user's .c
    # file before the corresponding ``#define`` is reached).  The
    # auto-inject pass would then inject
    # ``typedef struct _MinDly MinDly;`` and pycparser would abort
    # with "Non-typedef 'MinDly' previously declared as typedef in
    # this scope" — exactly the opgui.c case.
    # We capture every ``#define NAME`` or ``#define NAME(...)`` and
    # add NAME to ``already_defined`` so the auto-inject skips it.
    # This is safe: a #define name is never a type name in C.
    #
    # BRACE-AWARE: #define directives are always at the top level
    # (the C preprocessor does not allow them inside { ... }), so we
    # don't need _strip_brace_bodies here.  We use the original text.
    for m in _re_mod.finditer(
        r"(?m)^\s*#\s*define\s+([A-Za-z_]\w*)", text
    ):
        already_defined.add(m.group(1))
    # Pass 7 (NEW): function declarations of the form
    # ``TYPE * NAME(...)`` or ``TYPE NAME(...)`` followed by ``;`` or
    # ``{``.  Here ``TYPE`` is the return type.  If ``TYPE`` is an
    # identifier (not a C keyword like ``int`` or ``void``) AND it
    # appears in the text as a function's return type, we know the
    # user's source intends it to be a type — but if pycparser
    # rejects it, injecting ``typedef struct _TYPE TYPE;`` here would
    # immediately clash with the function declaration, producing the
    # "Non-typedef 'TYPE' previously declared as typedef in this scope"
    # error and forcing the ast_translator's retry loop to spend all
    # 10 retries before giving up (a real opgui.c resources.c case
    # with ``GResource *resources_get_resource(void);``).
    #
    # BRACE-AWARE: We walk every top-level declaration (using
    # ``_iter_top_level_decls``) so that we only inspect TOP-LEVEL
    # function declarations — never function declarations nested
    # inside a function body.  This avoids false positives where,
    # for example, a local function-pointer variable inside a
    # function body would have its return type added to
    # ``already_defined``.
    for decl in _iter_top_level_decls(text):
        decl_strip = decl.strip()
        if not decl_strip:
            continue
        # Strip leading ``#line N "file"`` directives (same as
        # Pass 5 — see the comment there for details).
        while True:
            nl = decl_strip.find("\n")
            if nl == -1:
                if decl_strip.startswith("#"):
                    decl_strip = ""
                break
            first_line = decl_strip[:nl]
            if first_line.lstrip().startswith("#"):
                decl_strip = decl_strip[nl + 1:].lstrip()
                continue
            break
        if not decl_strip:
            continue
        # Skip preprocessor lines.
        if decl_strip.startswith("#"):
            continue
        # Skip typedefs (handled above).
        if decl_strip.startswith("typedef"):
            continue
        # Check if this is a function declaration: ends with ``;``
        # or ``{`` and contains ``IDENT(...)`` somewhere.
        # We use _strip_brace_bodies so a function body containing
        # another function call doesn't confuse us.
        decl_no_braces = _strip_brace_bodies(decl_strip)
        # Find the first ``IDENT(...)`` pattern.
        m = _re_mod.search(
            r"\b([A-Za-z_]\w*)\s*\(([^;{}]*)\)\s*[;{]",
            decl_no_braces,
        )
        if not m:
            continue
        # The function name is the IDENT just before the ``(``.
        func_name = m.group(1)
        # Add the function name to ``already_defined`` so the
        # auto-inject pass does NOT inject a typedef for it.  This
        # is the fix for opgui's
        # ``(GtkTreeModelForeachFunc)selectDev_ForeachFunc`` case
        # where ``selectDev_ForeachFunc`` is a function name (with a
        # function definition earlier in the file) that gets used in
        # a cast expression.  Without this addition, the auto-inject
        # pass would inject
        # ``typedef struct _selectDev_ForeachFunc selectDev_ForeachFunc;``
        # (because the name starts with lowercase 's' but happens to
        # not match any existing typedef) — and even though it
        # starts lowercase, pycparser would still abort because
        # ``selectDev_ForeachFunc`` was just declared as a function
        # and cannot be redeclared as a typedef.
        # By adding the function name here, we ensure the auto-inject
        # pass skips it.  We add the name regardless of case
        # (lowercase function names are not auto-inject candidates
        # anyway since the auto-inject regex requires ``[A-Z]`` start,
        # so this is harmless for lowercase names — but for functions
        # with uppercase-initial names like ``ICD_check`` in opgui's
        # icd.c, this is the critical fix that prevents a wrong
        # typedef injection).
        already_defined.add(func_name)
        # The return type is everything BEFORE the function name.
        # Walk backwards from the ``(`` to find the return type.
        func_name_end = m.start() + len(m.group(1))
        return_type_part = decl_no_braces[:m.start()].rstrip()
        if not return_type_part:
            continue
        # The return type may be ``TYPE`` or ``TYPE *``.  Take the
        # last identifier in the return type.
        # Skip storage keywords at the start of the return type.
        rt_idents = _re_mod.findall(r"\b([A-Za-z_]\w*)\b", return_type_part)
        if not rt_idents:
            continue
        # The last identifier is the return type (could be a typedef
        # name, or a struct/union/enum tag).
        ret_name = rt_idents[-1]
        # Skip C built-in return types — those are not undefined
        # type names, so adding them to already_defined is harmless
        # but pointless.  We skip them for clarity.
        if ret_name in {"int", "void", "char", "short", "long",
                        "float", "double", "unsigned", "signed",
                        "size_t", "ssize_t", "const", "static",
                        "extern", "volatile", "register", "inline",
                        "struct", "union", "enum"}:
            continue
        already_defined.add(ret_name)
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
    #
    # BRACE-AWARE: we use the ORIGINAL text (not brace-stripped)
    # for the GTK scan.  This is critical: GTK callback types like
    # ``GSourceFunc``, ``GtkTreeModelForeachFunc``, ``GCallback`` are
    # often used ONLY inside function bodies as cast targets
    # (e.g. ``(GSourceFunc)icdCheck`` in opgui's icd.c).  If we
    # strip brace bodies first, those identifiers disappear and
    # never become auto-inject candidates, leaving pycparser with
    # an undefined type and aborting the parse.
    # The brace-aware passes (Pass 5, 7) have already added all
    # top-level variable names and function names to
    # ``already_defined``, so identifiers from inside function
    # bodies that happen to collide with those will be correctly
    # skipped.  Identifiers that ONLY appear inside function bodies
    # and are NOT in ``already_defined`` are exactly the ones we
    # want to auto-inject (they're types used in cast expressions,
    # not local variables).
    candidates: list[str] = []
    seen: set[str] = set()
    for m in _re_mod.finditer(r"\b(G[A-Za-z0-9_]+)\b", text):
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
    # ---- Auto-inject ANY remaining uppercase-initial identifier
    # that is undefined.  This replaces the previous hand-maintained
    # ``WIN_TYPES`` set: instead of listing every Windows-only type
    # we might encounter, we inject a placeholder for every
    # undefined identifier that starts with an uppercase letter and
    # is not in the non-type denylist.  This way new Windows types
    # (PCSTR, PCWSTR, OVERLAPPED, SP_DEVICE_INTERFACE_DATA, ...) and
    # any other all-caps types from third-party headers are handled
    # automatically — no gtk2laz source edit required.
    #
    # We still ALWAYS run this scan regardless of whether the Gtk
    # scan above found candidates.  The previous code had a bug where
    # a non-empty ``candidates`` list (from the Gtk scan) caused the
    # WIN_TYPES scan to be skipped, leaving OVERLAPPED undefined and
    # aborting the whole AST parse.  We never want to regress there.
    if _AUTO_INJECT_ALL_UPPERCASE:
        # ---- Brace-aware extraction of enum constants and function
        # names.  We use ``_strip_brace_bodies`` so we only scan the
        # TOP-LEVEL declarations, never the contents of nested
        # struct/union/enum bodies or function bodies.
        # This prevents a false positive where, for example, a
        # local ``static void MyCallback(...)`` inside a function body
        # would have its name added to ``function_names``, suppressing
        # the auto-inject pass even though the user's source clearly
        # intends ``MyCallback`` to be a callback type that may be
        # undefined at the top level.
        # First, harvest names already declared as enum constants so
        # we don't accidentally promote them to typedefs.  A line like
        # ``enum Color { RED, GREEN, BLUE };`` declares RED/GREEN/BLUE
        # as enum constants, and if we prepend
        # ``typedef struct _RED RED;`` *before* that line, pycparser
        # sees a redeclaration of ``RED`` and aborts with
        # "redefinition of typedef 'RED'".  By extracting the enum
        # constants first we avoid the collision.
        # BRACE-AWARE: We walk top-level declarations only, and for
        # each one that contains ``enum ... { ... }``, we extract
        # the enum-body identifiers as enum constants.  This avoids
        # false-matching an ``enum {...}`` declared INSIDE a
        # function body or struct initializer.
        enum_constants: set[str] = set()
        for decl in _iter_top_level_decls(text):
            # Walk the original decl (not the brace-stripped version)
            # and find every ``enum ... { ... }`` pattern, then
            # extract the enum body.  We use a depth-aware,
            # string/char/comment-aware walker that's the same
            # logic as ``_iter_top_level_decls`` and
            # ``_strip_brace_bodies`` use, so braces inside comments
            # or string literals do not confuse the matching.
            decl_text = decl
            decl_len = len(decl_text)
            for em in _re_mod.finditer(
                r"enum\s*(?:\w+\s*)?\{", decl_text
            ):
                # Find the matching closing brace, skipping over
                # string literals, char literals, block comments and
                # line comments (those may contain ``{`` or ``}``).
                body_start = em.end()  # position right after ``{``
                body_depth = 1
                body_end = body_start
                _in_str = False
                _in_chr = False
                _in_bc = False   # block comment
                _in_lc = False   # line comment
                while body_end < decl_len and body_depth > 0:
                    c = decl_text[body_end]
                    if _in_lc:
                        if c == "\n":
                            _in_lc = False
                        body_end += 1
                        continue
                    if _in_bc:
                        if c == "*" and body_end + 1 < decl_len \
                                and decl_text[body_end + 1] == "/":
                            _in_bc = False
                            body_end += 2
                            continue
                        body_end += 1
                        continue
                    if _in_str:
                        if c == "\\":
                            body_end += 2
                            continue
                        if c == '"':
                            _in_str = False
                        body_end += 1
                        continue
                    if _in_chr:
                        if c == "\\":
                            body_end += 2
                            continue
                        if c == "'":
                            _in_chr = False
                        body_end += 1
                        continue
                    # Not in any literal/comment.
                    if c == "/" and body_end + 1 < decl_len:
                        nxt = decl_text[body_end + 1]
                        if nxt == "/":
                            _in_lc = True
                            body_end += 2
                            continue
                        if nxt == "*":
                            _in_bc = True
                            body_end += 2
                            continue
                    if c == '"':
                        _in_str = True
                        body_end += 1
                        continue
                    if c == "'":
                        _in_chr = True
                        body_end += 1
                        continue
                    if c == "{":
                        body_depth += 1
                    elif c == "}":
                        body_depth -= 1
                        if body_depth == 0:
                            break
                    body_end += 1
                if body_depth != 0:
                    continue
                # body_text = decl[body_start:body_end] (excluding
                # the closing ``}``).
                body = decl_text[body_start:body_end]
                # Strip the body of string/char/comment content so
                # we don't accidentally capture identifiers from
                # strings inside the enum body (rare but possible).
                body_clean = _strip_brace_bodies(body)
                # Remove string and char literals from body_clean.
                # _strip_brace_bodies preserved them verbatim, so
                # we manually remove them now.
                # We use a small inline regex.
                body_no_str = _re_mod.sub(
                    r'"(\\.|[^"\\])*"', " ", body_clean
                )
                body_no_str = _re_mod.sub(
                    r"'(\\.|[^'\\])*'", " ", body_no_str
                )
                # Remove block and line comments.
                body_no_str = _re_mod.sub(
                    r"/\*.*?\*/", " ", body_no_str, flags=_re_mod.S
                )
                body_no_str = _re_mod.sub(
                    r"//[^\n]*", " ", body_no_str
                )
                for im in _re_mod.finditer(
                    r"\b([A-Za-z_]\w*)\b", body_no_str
                ):
                    enum_constants.add(im.group(1))
        # Second, harvest names that are already declared as
        # FUNCTIONS — ``extern void FD_ZERO(fd_set *);`` or any
        # ``NAME(...)`` call/declaration.  If we inject
        # ``typedef struct _FD_ZERO FD_ZERO;`` *before* the function
        # declaration, pycparser aborts with
        # "Non-typedef 'FD_ZERO' previously declared as typedef in
        # this scope".  We capture any uppercase-initial identifier
        # immediately followed by ``(`` (function call/declaration)
        # or wrapped in ``(*NAME)`` (function-pointer variable),
        # and skip them from auto-inject.
        # We use the ORIGINAL text (not brace-stripped) so that
        # function calls inside function bodies (e.g.
        # ``FD_ZERO(&set)`` inside a function) are also captured
        # as function names.  This prevents the auto-inject pass
        # from injecting a typedef for a function name that appears
        # only as a call inside a function body.
        function_names: set[str] = set()
        for m in _re_mod.finditer(r"\b([A-Z][A-Za-z0-9_]+)\s*\(", text):
            function_names.add(m.group(1))
        for m in _re_mod.finditer(r"\(\s*\*\s*([A-Z][A-Za-z0-9_]+)\s*\)", text):
            function_names.add(m.group(1))
        # Now scan every uppercase-initial identifier in the text.
        # We use ``_text_no_braces`` (brace-stripped) so that
        # identifiers declared inside function bodies (e.g. local
        # typedefs like ``typedef int Local;`` or local variables
        # like ``int LocalVar = 42;``) do NOT get picked up as
        # auto-inject candidates.  Those local declarations would
        # clash with an injected typedef and cause pycparser to
        # abort.  GTK callback types like ``GSourceFunc`` that
        # appear ONLY inside function bodies are already captured
        # by the GTK scan above (which uses the original text), so
        # they're in ``candidates`` already and don't need to be
        # re-discovered by this scan.
        _text_no_braces = _strip_brace_bodies(text)
        for m in _re_mod.finditer(r"\b([A-Z][A-Za-z0-9_]+)\b", _text_no_braces):
            name = m.group(1)
            if name in already_defined or name in seen:
                continue
            if name in _NON_TYPE_DENYLIST:
                continue
            if name in enum_constants:
                # Already declared as an enum constant — injecting
                # a typedef here would clash with the enum, so skip.
                continue
            if name in function_names:
                # Already declared as a function — injecting a
                # typedef here would clash with the function decl.
                continue
            seen.add(name)
            candidates.append(name)
    else:
        # Legacy fallback: only inject from the explicit WIN_TYPES
        # set.  Kept so the new behaviour can be disabled with
        # ``_AUTO_INJECT_ALL_UPPERCASE = False`` if it ever causes a
        # regression.  This is the original behaviour pre-auto-inject.
        WIN_TYPES = {
            "OVERLAPPED", "LPOVERLAPPED", "PCSTR", "PCWSTR",
            "PULONG", "HDEVINFO", "SP_DEVICE_INTERFACE_DATA",
            "PSP_DEVICE_INTERFACE_DATA", "HIDD_ATTRIBUTES",
            "PHIDD_ATTRIBUTES", "SP_DEVINFO_DATA",
            "PSP_DEVINFO_DATA", "SP_DEVICE_INTERFACE_DETAIL_DATA",
            "PSP_DEVICE_INTERFACE_DETAIL_DATA",
            "GETHIDGUID", "GETATTRIBUTES", "SETNUMINPUTBUFFERS",
            "GETNUMINPUTBUFFERS", "GETFEATURE", "SETFEATURE",
            "GETREPORT", "SETREPORT", "GETMANUFACTURERSTRING",
            "GETPRODUCTSTRING",
            "SETUPDIGETCLASSDEVS", "SETUPDIENUMDEVICEINTERFACES",
            "SETUPDIGETDEVICEINTERFACEDETAIL",
            "SETUPDIDESTROYDEVICEINFOLIST",
        }
        win_types: set[str] = set()
        for m in re.finditer(r"\b([A-Z][A-Za-z0-9_]+)\b", text):
            name = m.group(1)
            if name in already_defined or name in seen:
                continue
            if name in WIN_TYPES and name not in win_types:
                win_types.add(name)
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
            # Function-pointer typedef.  We use ``typedef void *NAME;``
            # (an opaque pointer type) rather than
            # ``typedef int (*NAME)(void);`` (a specific function-pointer
            # type) because the source may cast functions with
            # DIFFERENT signatures to this type — for example opgui's
            # ``(GtkTreeModelForeachFunc)selectDev_ForeachFunc`` where
            # ``selectDev_ForeachFunc`` has the signature
            # ``gboolean (*)(GtkTreeModel *, GtkTreePath *, GtkTreeIter *, char *)``
            # which is NOT compatible with ``int (*)(void)``.  pycparser
            # rejects such casts when the typedef is a specific
            # function-pointer type, but accepts them when the typedef
            # is an opaque pointer (``void *``) — the cast just becomes
            # ``((void *)func)`` which pycparser treats as a generic
            # pointer-to-pointer cast.  This is the fix for the
            # ``before: selectDev_ForeachFunc`` parse error in opgui.c.
            lines.append(f"typedef void *{name};")
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
    # Collect (header_name, content) pairs so we can emit a #line
    # marker before each one.  We use the basename of each .h file as
    # the coord.file so the converter's filter can identify them.
    header_pairs: list[tuple[str, str]] = []
    # ---- Pass 1: build (header_name, content) pairs from the in-memory
    # HEADERS dict. This is the preferred source because it is always
    # up to date with the running gtk2laz version, regardless of what
    # is (or isn't) on disk in fake_include_dir. This avoids the need
    # to regenerate the on-disk directory whenever gtk2laz is updated —
    # the dict is the source of truth. Users who previously ran an
    # older version of gtk2laz may have a stale fake_libc_include on
    # disk missing newer headers (windows.h, winsock2.h, mmsystem.h),
    # which would silently break AST parsing; reading from the dict
    # sidesteps the problem entirely.
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import gen_fake_includes
        for hname, hcontent in gen_fake_includes.HEADERS.items():
            header_pairs.append((hname, hcontent))
    except Exception:
        header_pairs = []
    # ---- Pass 2 (fallback): if the in-memory dict was not available,
    # read the on-disk directory instead. This handles the rare case
    # where gen_fake_includes.py is missing or broken (e.g. running
    # from an old PyInstaller bundle that didn't include it).
    if not header_pairs and fake_include_dir \
            and os.path.isdir(fake_include_dir):
        for root, _dirs, files in os.walk(fake_include_dir):
            for fname in sorted(files):
                if not fname.endswith(".h"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8",
                              errors="replace", newline="") as f:
                        content = f.read()
                    # Strip any stray \\r characters that might have
                    # been written by an older version of
                    # gen_fake_includes.py (which used the default
                    # text-mode open() on Windows, converting \\n to
                    # \\r\\n). pycparser rejects \\r with
                    # "Illegal character '\\r'", so we must remove them.
                    content = content.replace("\r\n", "\n").replace("\r", "\n")
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
        # Strip trailing empty lines from the cleaned content.
        # These empty lines are left over from stripped #ifndef/
        # #define/#endif directives and cause pycparser to report
        # "Missing type in declaration" when it tries to parse them
        # as declarations belonging to the current header.
        while cleaned_lines and not cleaned_lines[-1].strip():
            cleaned_lines.pop()
        out_parts.append("\n".join(cleaned_lines))
        # Emit a trailing #line directive pointing to a sentinel file
        # that won't match the user's .c file. This ensures pycparser
        # switches coord.file away from the current header so any
        # remaining empty lines (between this header and the next)
        # are attributed to the sentinel, not to the current header.
        out_parts.append(f'#line 1 "<fake_header_end>"')
    return "\n".join(out_parts)
