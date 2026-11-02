"""
c_parser.py
===========
Pattern-based C -> FreePascal translator focused on GTK applications.

This is *not* a full C compiler. It is a pragmatic translator that:

  1. Recognises top-level function definitions and emits a Pascal
     ``procedure``/``function`` with the equivalent signature.
  2. Translates the body line-by-line using pattern tables for the
     most common GTK / GLib / libc calls (see ``widget_map.GTK_CALL_MAP``
     and ``widget_map.C_LIB_MAP``).
  3. Translates simple control-flow constructs (if/else, for, while,
     return, break, continue).
  4. Anything it cannot translate is wrapped in a ``(* TODO: ... *)``
     comment block so the file still compiles when the offending
     statement is replaced by a stub.

The translator is intentionally conservative: prefer a TODO comment
over a syntactically plausible but semantically wrong translation.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

from widget_map import (
    c_type_to_pascal, lookup_gtk_call, lookup_c_lib,
    pascalify_name, is_pascal_reserved,
)


# ---------------------------------------------------------------------------
# Tokenization helpers
# ---------------------------------------------------------------------------
# Strip C and C++ comments while preserving line structure for diagnostics.
_COMMENT_RE = re.compile(
    r"/\*.*?\*/|//[^\n]*",
    re.DOTALL,
)

# Strip preprocessor directives (we re-emit the relevant ones as Pascal).
_PREPROC_RE = re.compile(r"^\s*#\s*[a-z]+\s*[^\n]*", re.MULTILINE | re.IGNORECASE)


def strip_comments(src: str) -> str:
    """Replace comments with whitespace, keeping newlines."""
    def repl(m: re.Match) -> str:
        text = m.group(0)
        return "\n" * text.count("\n")
    return _COMMENT_RE.sub(repl, src)


def strip_preprocessor(src: str) -> tuple[str, list[str]]:
    """Pull out #define/#include lines so the parser can ignore them."""
    directives: list[str] = []
    out_lines = []
    for line in src.splitlines():
        if re.match(r"\s*#\s*\w+", line):
            directives.append(line.strip())
            out_lines.append("")  # keep line numbering
        else:
            out_lines.append(line)
    return "\n".join(out_lines), directives


# ---------------------------------------------------------------------------
# Function definition extractor
# ---------------------------------------------------------------------------
# A C function definition roughly:
#   [storage] return_type [*] name ( param_list ) { body }
# where param_list may span multiple lines and contain nested parens.

# C keywords that must not be matched as function names.
_C_KEYWORDS = {
    "if", "else", "for", "while", "do", "switch", "case", "default",
    "return", "break", "continue", "goto", "sizeof", "typedef",
    "struct", "union", "enum",
}

# Type-like keywords that ARE valid return types and should not cause a match
# to be rejected. (Distinguished from statement keywords like if/for/while.)
_C_TYPE_KEYWORDS = {
    "void", "char", "short", "int", "long",
    "float", "double", "signed", "unsigned", "const", "volatile",
    "static", "extern", "register", "auto", "inline", "restrict",
    # struct/union/enum are type keywords that introduce a tagged type
    # (e.g. ``struct symbol s;``). Without these, ``struct symbol s``
    # would not be recognised as a declaration.
    "struct", "union", "enum",
}

# Common C typedefs (Windows + GLib + stdint) that may appear as the
# first word of a local variable declaration. Used by
# ``_translate_statement`` to distinguish real declarations (``int x``)
# from expression statements (``sum += i``) whose first word happens to
# match the declaration regex.
_KNOWN_TYPEDEFS = {
    # stdint
    "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "int8_t", "int16_t", "int32_t", "int64_t",
    "size_t", "ssize_t", "ptrdiff_t", "wchar_t",
    "intptr_t", "uintptr_t",
    # Windows
    "DWORD", "WORD", "BYTE", "BOOL", "HANDLE", "HWND", "HMODULE",
    "HINSTANCE", "HDC", "LRESULT", "WPARAM", "LPARAM", "UINT",
    "LPSTR", "LPCTSTR", "LPCSTR", "LPVOID", "PBYTE", "PDWORD",
    "PWORD", "HBRUSH", "HMENU", "WCHAR",
    # GLib
    "gint", "guint", "gchar", "guchar", "gshort", "gushort",
    "glong", "gulong", "gfloat", "gdouble", "gboolean", "gpointer",
    "gconstpointer", "gsize", "gssize", "goffset", "gint8", "guint8",
    "gint16", "guint16", "gint32", "guint32", "gint64", "guint64",
    "gintptr", "guintptr",
    # GTK forward decls commonly used in signatures
    "GtkWidget", "GtkWindow", "GtkButton", "GtkLabel", "GtkEntry",
    "GtkBox", "GtkGrid", "GtkNotebook", "GtkComboBox", "GtkComboBoxText",
    "GtkTextView", "GtkTextBuffer", "GtkTextIter", "GtkListStore",
    "GtkTreeStore", "GtkTreeIter", "GtkTreePath", "GtkCellRenderer",
    "GtkTreeView", "GtkTreeViewColumn", "GtkToggleButton", "GtkCheckButton",
    "GtkRadioButton", "GtkSpinButton", "GtkAdjustment", "GtkStatusbar",
    "GtkProgressBar", "GtkImage", "GtkMenu", "GtkMenuBar", "GtkMenuItem",
    "GtkToolbar", "GtkToolButton", "GtkSeparator", "GtkPaned", "GtkExpander",
    "GtkScrolledWindow", "GtkViewport", "GtkFrame", "GtkAspectFrame",
    "GtkCalendar", "GtkScale", "GtkScrollbar", "GtkDrawingArea",
    "GtkEventBox", "GtkFixed", "GtkLayout", "GtkOverlay",
    "GdkEvent", "GdkEventButton", "GdkEventKey", "GdkColor", "GdkRGBA",
    "GdkPixbuf", "GdkDrawable", "GdkGC", "GdkPixmap", "GdkWindow",
    "cairo_t", "cairo_surface_t",
    # FILE from stdio
    "FILE",
    # gtk2laz-generated Pascal types that may leak through
    "gchar", "guchar",
    # Additional GTK/GDK types commonly used in local declarations
    "GtkTextTag", "GtkTextMark", "GtkTextChildAnchor", "GtkTextTagTable",
    "GtkClipboard", "GtkSelectionData", "GtkTargetList", "GtkTargetEntry",
    "GtkIconSet", "GtkIconSource", "GtkIconSize", "GtkSettings",
    "GtkStyle", "GtkRcStyle", "GtkCssProvider", "GtkStyleContext",
    "GtkBuilder", "GtkApplication", "GtkWindowGroup",
    "GtkFileChooser", "GtkFileChooserDialog", "GtkFileChooserWidget",
    "GtkFileChooserAction", "GtkFileFilter", "GtkRecentFilter",
    "GtkDialog", "GtkMessageDialog", "GtkAboutDialog",
    "GtkAssistant", "GtkColorButton", "GtkColorSelectionDialog",
    "GtkFontButton", "GtkFontSelectionDialog", "GtkRecentChooserDialog",
    "GtkLinkButton", "GtkVolumeButton", "GtkScaleButton",
    "GtkToolItem", "GtkToolPalette", "GtkToolItemGroup",
    "GtkAction", "GtkActionGroup", "GtkUIManager", "GtkAccelGroup",
    "GtkAccelMap", "GtkEntryBuffer", "GtkEntryCompletion",
    "GtkTextMark", "GtkTextChildAnchor",
    "GMainLoop", "GMainContext", "GSource", "GIOChannel",
    "GError", "GString", "GList", "GSList", "GHashTable", "GArray",
    "GPtrArray", "GByteArray", "GBytes",
    "GTimer", "GDate", "GDateTime", "GTimeZone",
    "GValue", "GParamSpec", "GClosure",
    "GObject", "GInitiallyUnowned", "GType", "GTypeClass", "GTypeInstance",
    "GdkDisplay", "GdkScreen", "GdkCursor", "GdkDevice", "GdkKeymap",
    "GdkDisplayManager", "GdkAppLaunchContext",
    # ---- Windows-specific types used in opgui/FindDevice ----
    # These appear in local variable declarations inside Windows HID/USB
    # code paths. Without them, ``__int64 stop;`` would be mis-parsed as
    # an expression statement and silently dropped.
    "__int64", "__int32", "__int16", "__int8",
    "__uint64", "__uint32", "__uint16", "__uint8",
    "ULONG", "PULONG", "LONG", "PLONG", "SHORT", "USHORT",
    "BOOLEAN", "PVOID", "LPCWSTR", "LPWSTR", "LPCWCHAR",
    "GUID", "LPGUID", "HDEVINFO",
    "SP_DEVICE_INTERFACE_DATA", "PSP_DEVICE_INTERFACE_DATA",
    "SP_DEVICE_INTERFACE_DETAIL_DATA", "PSP_DEVICE_INTERFACE_DETAIL_DATA",
    "SP_DEVINFO_DATA", "PSP_DEVINFO_DATA",
    "HIDD_ATTRIBUTES", "PHIDD_ATTRIBUTES",
    "fd_set", "ssize_t",
    # GtkTreeModel and related (used in opgui.c)
    "GtkTreeModel", "GtkTreeSelection", "GtkCellRendererText",
    "GtkCellRendererToggle", "GtkListStore", "GtkTreeStore",
    # GDK display/screen types
    "GdkDisplay", "GdkScreen", "GdkCursor", "GdkDevice",
    # Misc
    "GtkTextIter", "GtkTextMark",
}

_FUNC_DEF_RE = re.compile(
    r"""
    ^\s*
    (?:static\s+|extern\s+|inline\s+|G_MODULE_EXPORT\s+)*  # optional storage
    (?:const\s+)?                                          # optional const
    (?P<ret>[\w][\w\s\*]*?)                                # return type (lazy)
    \s+
    (?P<name>[A-Za-z_]\w*)                                # function name
    \s*
    \(
        (?P<params>.*?)
    \)
    \s*
    \{
    """,
    re.MULTILINE | re.DOTALL | re.VERBOSE,
)


@dataclass
class CFunction:
    name: str
    return_type: str
    params: list[tuple[str, str, str]]   # list of (type, name, raw)
    body: str
    start_line: int
    end_line: int
    is_static: bool = False


@dataclass
class TodoItem:
    """A single TODO marker emitted by the converter.

    Fields:
        source_file : absolute path of the original .c file
        source_line : 1-based line number in the .c file where the
                      untranslated C statement starts
        source_text : the offending C snippet (truncated to ~200 chars)
        output_file : absolute path of the .pas file containing the TODO
        output_line : 1-based line number of the ``(* TODO gtk2laz ... *)``
                      marker in the .pas file
        reason      : human-readable reason string
        function    : name of the enclosing C function (or "" if unknown)
    """
    source_file: str
    source_line: int
    source_text: str
    output_file: str
    output_line: int
    reason: str
    function: str = ""


def _split_top_level(s: str, sep: str = ",") -> list[str]:
    """Split a string at top-level separator (respecting parens/brackets)."""
    parts: list[str] = []
    depth = 0
    cur = []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return parts


def _split_for_header(s: str) -> list[str]:
    """Split a for-loop header into exactly 3 parts at top-level semicolons.

    Unlike ``_split_top_level``, this preserves empty slots so that
    ``for(;;)``, ``for(;i<N;i++)`` and ``for(i=0;;i++)`` all produce
    exactly three parts (with empty strings where the slot was empty).
    The for-loop header is well-formed: it always has exactly two
    semicolons at top level, so the result is always length 3.
    """
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == ";" and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    # Always return exactly 3 parts (pad with empty strings if needed)
    while len(parts) < 3:
        parts.append("")
    return parts[:3]


def _parse_param(p: str) -> tuple[str, str, str]:
    """Return (type, name, raw) for a C parameter string."""
    p = p.strip()
    if not p or p == "void":
        return ("", "", p)
    # Try to find the trailing identifier (the param name)
    m = re.match(r"(?P<type>.*?)(?P<name>[A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*$",
                 p, re.DOTALL)
    if not m:
        return (p, "", p)
    ptype = m.group("type").strip()
    pname = m.group("name").strip()
    # If there's a "*" attached to the name (e.g. "char *name"), move it to type
    if pname.startswith("*"):
        ptype = (ptype + " *").strip()
        pname = pname.lstrip("*").strip()
    return (ptype, pname, p)


def extract_functions(src: str) -> tuple[list[CFunction], str]:
    """Find all function definitions; return (list, residual_text)."""
    funcs: list[CFunction] = []
    consumed_spans: list[tuple[int, int]] = []

    for m in _FUNC_DEF_RE.finditer(src):
        name = m.group("name")
        # Skip C keywords masquerading as function names
        if name in _C_KEYWORDS:
            continue
        # Skip if the "return type" looks like it ends in a statement keyword
        # (means the regex matched something like `if (...) {`)
        ret_check = m.group("ret").strip()
        if not ret_check:
            continue
        ret_last = ret_check.split()[-1]
        if ret_last in _C_KEYWORDS:
            continue
        # Find the matching closing brace
        start = m.end() - 1   # position of "{"
        depth = 0
        i = start
        while i < len(src):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if i >= len(src):
            continue
        end = i + 1
        body = src[start + 1: i]
        ret = ret_check
        params_raw = m.group("params").strip()
        params = [_parse_param(p) for p in _split_top_level(params_raw) if p]
        is_static = bool(re.search(r"\bstatic\b", m.group(0)))
        # Compute line numbers
        start_line = src.count("\n", 0, m.start()) + 1
        end_line = src.count("\n", 0, end) + 1
        funcs.append(CFunction(
            name=name,
            return_type=ret,
            params=params,
            body=body,
            start_line=start_line,
            end_line=end_line,
            is_static=is_static,
        ))
        consumed_spans.append((m.start(), end))

    # Build residual text (function bodies removed; keeps file structure
    # for global variable scanning).
    residual_parts: list[str] = []
    last = 0
    for s, e in consumed_spans:
        residual_parts.append(src[last:s])
        residual_parts.append(" " * (e - s))   # blank out the function
        last = e
    residual_parts.append(src[last:])
    residual = "".join(residual_parts)
    return funcs, residual


# ---------------------------------------------------------------------------
# Global variable extractor
# ---------------------------------------------------------------------------
_GLOBAL_VAR_RE = re.compile(
    r"""
    ^\s*
    (?P<type>(?:const\s+|static\s+|extern\s+|volatile\s+|unsigned\s+|signed\s+)*
              [\w]+(?:\s+\w+)*\s*\**)
    \s+
    (?P<names>[A-Za-z_][\w\s,=\[\]*\(\)\.\d'"]*?)
    \s*;
    """,
    re.MULTILINE | re.VERBOSE,
)


@dataclass
class CGlobal:
    type: str
    names: list[str]   # may include initializers like "x = 5"
    raw: str


def extract_globals(residual: str) -> list[CGlobal]:
    """Find top-level variable declarations (after functions removed).

    Note: ``extern`` declarations are skipped — in Pascal, external
    variables are accessed via the ``uses`` clause (the unit that
    declares them in its interface section). Including ``extern int x;``
    as a local var would either duplicate the declaration or produce
    an invalid Pascal type like ``Textern int``.
    """
    globals_list: list[CGlobal] = []
    # Strip block contents that survived (struct/enum bodies)
    cleaned = re.sub(r"\{[^{}]*\}", " ", residual)
    for m in _GLOBAL_VAR_RE.finditer(cleaned):
        type_str = m.group("type").strip()
        if type_str in ("if", "for", "while", "switch", "return", "else",
                        "struct", "enum", "union", "typedef"):
            continue
        if type_str.startswith("#"):
            continue
        # Skip extern declarations — they are resolved via the uses clause
        # in Pascal (the declaring unit's interface section makes the
        # variable visible). Including them here would produce invalid
        # Pascal like ``x: Textern int;``.
        if "extern" in type_str.split():
            continue
        names_raw = m.group("names").strip()
        # Split multiple declarators
        parts = _split_top_level(names_raw)
        names = [p.strip() for p in parts if p.strip()]
        if not names:
            continue
        globals_list.append(CGlobal(type=type_str, names=names, raw=m.group(0)))
    return globals_list


# ---------------------------------------------------------------------------
# Body translator
# ---------------------------------------------------------------------------
class BodyTranslator:
    """Translate the inside of a C function to Pascal statements."""

    # Operators that need to be replaced in expressions.
    # Order matters: longer operators must come first so that, e.g.,
    # ``!=`` is matched before ``!``. ``&&`` and ``||`` are handled
    # separately by ``_split_logical`` (which wraps operands in parens),
    # so they are NOT in this list — but we keep them here as a safety
    # net for any logical operator that slips past ``_split_logical``
    # (e.g. inside a string literal). They are placed first so that
    # they are replaced before ``!`` (which is part of ``!=``).
    _OP_REPLACE = [
        # Two-char operators first (so they win over their one-char prefix)
        ("&&", " and "), ("||", " or "),
        ("==", "="), ("!=", "<>"),
        ("<<", " shl "), (">>", " shr "),
        # C pointer member access -> Pascal pointer dereference + member
        # ``ptr->field`` becomes ``ptr^.field`` in Pascal
        ("->", "^."),
        # One-char operators last.
        # C ``&`` (bitwise AND) -> Pascal ``and`` (Pascal's ``and`` serves
        # as both bitwise and logical AND, distinguished by operand types).
        # C ``|`` (bitwise OR) -> Pascal ``or``.
        # C ``%`` (modulo) -> Pascal ``mod``.
        # NOTE: ``&`` must come AFTER ``&&`` (already handled above) so
        # that ``&&`` is replaced before ``&`` would match the first ``&``.
        # NOTE: ``%`` must come after ``%=`` is handled (``%=`` is handled
        # separately by the augmented-assignment regex in _try_assignment,
        # NOT by _OP_REPLACE, so there's no conflict here).
        # NOTE: ``^`` (C XOR) is NOT here because Pascal uses ``^`` for
        # pointer dereference (and ``->`` already becomes ``^.`` above).
        # C XOR is rare in this codebase; if needed it can be added as a
        # separate pass that checks for ``^`` not followed by ``.``.
        ("&", " and "), ("|", " or "), ("%", " mod "),
        ("!", " not "),
    ]

    def __init__(self, func: CFunction, indent: int = 2,
                 source_file: str = ""):
        self.func = func
        self.indent = indent
        self.source_file = source_file
        self.lines: list[str] = []
        self.locals: list[str] = []      # collected local var declarations
        self.todo_count = 0
        self.translated_count = 0
        self.skipped_count = 0
        # Track the current Pascal line number so each TODO can record
        # its output_line. 1-based: the function signature will occupy
        # lines 1..N before us; we update _pas_line as we go.
        self._pas_line = 1
        self.todos: list[TodoItem] = []
        # Name mapping: C identifier -> Pascal identifier.
        # When a C variable/function name is a Pascal reserved word,
        # pascalify_name appends ``_``. We record this mapping so that
        # references to the variable in expressions can be updated too.
        self._name_map: dict[str, str] = {}
        # Extra type names that should be treated as valid C types when
        # deciding whether a statement is a local declaration.
        self._extra_known_types: set[str] = set()

    def _pascalify(self, name: str) -> str:
        """Like pascalify_name, but also records the C→Pascal name
        mapping so references in expressions can be updated."""
        pas = pascalify_name(name)
        if pas != name:
            self._name_map[name] = pas
        return pas

    # ------------------------------------------------------------------
    def translate(self) -> list[str]:
        body = self.func.body
        # Split body into statements by ;, respecting nesting
        statements = self._split_statements(body)
        # Reset labels list (collected during translation)
        self._labels: list[str] = []
        for stmt in statements:
            stmt = stmt.strip()
            if not stmt:
                continue
            self._translate_statement(stmt)
        # Labels are NOT emitted here — they are returned via self._labels
        # and emitted by the caller (CFileTranslator) BEFORE the var block,
        # because Pascal requires ``label`` to come before ``var`` and
        # both must be before ``begin``.
        return self.lines

    # ------------------------------------------------------------------
    def _split_statements(self, src: str) -> list[str]:
        """Split a function body into statements, respecting braces.

        Special handling:
          * ';' inside ``(...)`` (for-headers, function calls, casts) is
            NOT a separator.
          * ';' immediately followed by 'else' is NOT a separator.
        """
        out: list[str] = []
        depth = 0          # brace depth {}
        paren_depth = 0    # paren depth ()
        cur: list[str] = []
        i = 0
        n = len(src)
        while i < n:
            ch = src[i]
            # Check for 'do' keyword — do-while must be kept as one
            # statement so the 'while' part isn't split off by the ';'
            # inside or after the do-body.
            if ch == "d" and src[i:i+2] == "do" and \
                    i + 2 < n and src[i+2] in " \t\n{" and \
                    (i == 0 or not (src[i-1].isalnum() or src[i-1] == "_")):
                # Found 'do' keyword — find the matching 'while(...);' to
                # determine the end of the do-while statement.
                # Strategy: find 'while' at the top level (depth 0), then
                # find its matching ')' and the trailing ';'.
                j = i + 2  # skip "do"
                d_depth = 0
                while_end = -1
                while j < n:
                    c2 = src[j]
                    if c2 in "{(":
                        d_depth += 1
                    elif c2 in "})":
                        d_depth -= 1
                    if d_depth == 0 and src[j:j+5] == "while" and \
                            (j == 0 or not (src[j-1].isalnum() or src[j-1] == "_")):
                        while_end = j
                        break
                    j += 1
                if while_end < 0:
                    # No 'while' found — treat as normal text
                    cur.append(ch)
                    i += 1
                    continue
                # Found 'while' — find its matching ')'
                wparen = src.find("(", while_end + 5)
                if wparen < 0:
                    cur.append(ch)
                    i += 1
                    continue
                wdepth = 1
                k = wparen + 1
                while k < n and wdepth > 0:
                    if src[k] == "(":
                        wdepth += 1
                    elif src[k] == ")":
                        wdepth -= 1
                    k += 1
                if wdepth != 0:
                    cur.append(ch)
                    i += 1
                    continue
                # k is past the ')' — look for ';'
                while k < n and src[k] in " \t\n":
                    k += 1
                if k < n and src[k] == ";":
                    # Found the end of do-while — append everything
                    # from 'do' to ';' (inclusive) as one statement.
                    cur.append(src[i:k+1])
                    i = k + 1
                    out.append("".join(cur))
                    cur = []
                    continue
                else:
                    cur.append(ch)
                    i += 1
                    continue
            if ch == "{":
                depth += 1
                cur.append(ch)
            elif ch == "}":
                depth -= 1
                cur.append(ch)
            elif ch == "(":
                paren_depth += 1
                cur.append(ch)
            elif ch == ")":
                if paren_depth > 0:
                    paren_depth -= 1
                cur.append(ch)
            elif ch == ";" and depth == 0 and paren_depth == 0:
                # ';' immediately followed by 'else' is not a separator
                after_semi = src[i+1:].lstrip()
                if after_semi.startswith("else"):
                    cur.append(ch)
                else:
                    out.append("".join(cur))
                    cur = []
            elif ch == "\n":
                cur.append(" ")
            else:
                cur.append(ch)
            i += 1
        if cur:
            tail = "".join(cur).strip()
            if tail:
                out.append(tail)
        return out

    def _extract_single_stmt(self, s: str) -> str:
        """Extract the first single statement from ``s`` up to the first
        top-level ``;`` (respecting parens/braces). Used to extract a
        for-loop's single-statement body (e.g. ``for(...) sum += i;`` ->
        body = ``sum += i``).

        Returns the statement text without the trailing ``;``, or "" if
        no statement is found.
        """
        depth = 0
        paren_depth = 0
        i = 0
        n = len(s)
        while i < n:
            ch = s[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif ch == "(":
                paren_depth += 1
            elif ch == ")":
                if paren_depth > 0:
                    paren_depth -= 1
            elif ch == ";" and depth == 0 and paren_depth == 0:
                return s[:i].strip()
            i += 1
        # No ';' found — return the whole string if non-empty
        return s.strip()

    # ------------------------------------------------------------------
    def _pad(self, n: int = 0) -> str:
        return " " * (self.indent + n * 2)

    def _emit(self, text: str) -> None:
        self.lines.append(text)
        self._pas_line += 1
        self.translated_count += 1

    def _emit_todo(self, code: str, reason: str = "",
                    c_line: int = 0) -> None:
        self.todo_count += 1
        pad = self._pad()
        # Determine the source line for this snippet
        if c_line == 0:
            c_line = self._guess_c_line(code)
        # Build a multi-line comment block containing:
        #   - the TODO marker + reason
        #   - the source file:line
        #   - the full original C snippet (preserving newlines/indent)
        first_line = code.strip().split("\n", 1)[0][:200]
        # Read the full source context (a few lines around c_line) so the
        # user sees the original C code exactly as written.
        source_block = self._read_source_block(c_line, code)
        # Emit as a multi-line Pascal comment. Pascal supports both
        #   (* ... *)  and  { ... }  style block comments. We use (* *)
        # because { } can be ambiguous with set literals in some Pascal
        # flavours. The comment is structured so that grep can find it:
        #
        #   (* TODO gtk2laz: <reason>
        #      Source: <file>:<line>
        #      Original C:
        #        <line 1>
        #        <line 2>
        #        ...
        #   *)
        reason_str = reason or "translate manually"
        src_file = self.source_file or "?"
        # Indent for the comment body (one level deeper than pad)
        body_pad = pad + "  "
        lines_out = []
        lines_out.append(f"{pad}(* TODO gtk2laz: {reason_str}")
        lines_out.append(f"{body_pad}Source: {src_file}:{c_line}")
        if source_block:
            lines_out.append(f"{body_pad}Original C:")
            for src_line in source_block.splitlines():
                # Preserve indentation, escape any '*)' sequences inside
                # the source so we don't accidentally close the comment.
                safe = src_line.replace("*)", "* )")
                lines_out.append(f"{body_pad}  {safe}")
        lines_out.append(f"{pad}*)")
        for line in lines_out:
            self.lines.append(line)
            self._pas_line += 1
        out_line = self._pas_line - len(lines_out)
        self.skipped_count += 1
        # Record a TodoItem as a plain dict (rather than a dataclass) so
        # that JSON serialisation works uniformly under PyInstaller.
        # The full source block is stored in source_text for the GUI.
        self.todos.append({
            "source_file": self.source_file,
            "source_line": c_line,
            "source_text": source_block or first_line,
            "output_file": "",   # filled in by the caller after writing
            "output_line": out_line,
            "reason": reason_str,
            "function": self.func.name,
        })

    def _read_source_block(self, c_line: int, code: str) -> str:
        """Read the original C source lines corresponding to ``code``.

        Returns the source text (possibly multi-line) that matches the
        snippet, with original indentation preserved. Falls back to the
        raw ``code`` argument if the source file is unavailable or the
        line number is unreliable.
        """
        # If we couldn't determine a source line (c_line == 0 or 1 when
        # the function starts at line 1 due to comment-stripping), just
        # return the code snippet itself rather than reading the whole
        # file from line 1.
        if not self.source_file or c_line <= 0:
            return code.strip()
        # Heuristic: if c_line is 1 and the code snippet doesn't look
        # like it belongs on line 1 (e.g. it's an assignment, not a
        # comment or #include), the line number is probably wrong.
        # In that case, return the code snippet instead of reading
        # from line 1 (which would dump the entire file header).
        snippet_first = code.strip().split("\n", 1)[0].strip()
        if c_line == 1:
            try:
                with open(self.source_file, "r",
                          encoding="utf-8", errors="replace") as f:
                    first_line = f.readline()
            except OSError:
                return code.strip()
            # If the snippet is not found on line 1 of the source file,
            # the line number is wrong - return the snippet as-is.
            if snippet_first and snippet_first[:20] not in first_line:
                return code.strip()
        try:
            with open(self.source_file, "r",
                      encoding="utf-8", errors="replace") as f:
                src_lines = f.readlines()
        except OSError:
            return code.strip()
        if c_line > len(src_lines):
            return code.strip()
        # Determine how many lines the snippet spans.
        snippet_lines = code.strip().split("\n")
        n_lines = len(snippet_lines)
        # If the snippet is a single line, try to extend to capture the
        # full statement (up to ~20 lines for safety, but stop at
        # blank lines or comment blocks that indicate a new construct).
        if n_lines == 1:
            depth = 0
            collected = []
            for i in range(c_line - 1, min(c_line + 20, len(src_lines))):
                line = src_lines[i].rstrip("\n")
                stripped = line.strip()
                # Stop if we hit a preprocessor directive or a new
                # function/struct definition (likely end of statement)
                if i > c_line - 1 and (stripped.startswith("#")
                                        or stripped.startswith("typedef")
                                        or stripped.startswith("struct ")):
                    break
                collected.append(line)
                depth += line.count("{") - line.count("}")
                depth += line.count("(") - line.count(")")
                if depth <= 0 and (";" in line or "}" in line):
                    if ";" in line or line.strip().endswith("}"):
                        break
            if len(collected) > 1:
                return "\n".join(collected)
        # Multi-line snippet: read the corresponding number of source lines
        end_line = min(c_line + n_lines - 1, len(src_lines))
        block = []
        for i in range(c_line - 1, end_line):
            block.append(src_lines[i].rstrip("\n"))
        return "\n".join(block) if block else code.strip()

    def _guess_c_line(self, code: str) -> int:
        """Best-effort: locate ``code`` inside the function body and
        return its absolute line number in the source file.

        The pattern translator works on comment-stripped text, so the
        line numbers it reports (``self.func.start_line`` etc.) may not
        match the original source file. To recover the true source line,
        we search the **original** (un-stripped) file for distinctive
        substrings of ``code``.
        """
        if not self.source_file:
            return 0
        # Build a list of candidate search strings from the code snippet.
        # Skip comment-only snippets (which would match line 1).
        snippet = code.strip().split("\n", 1)[0][:80]
        if not snippet:
            return self.func.start_line
        # Skip if the snippet is just a comment or whitespace
        clean = snippet.strip()
        if not clean or clean.startswith("//") or clean.startswith("/*"):
            # Try to extract a real code token from the snippet
            import re as _re
            # Find the first non-comment, non-whitespace token
            real_code = _re.sub(r"//.*|/\*.*?\*/", "", snippet).strip()
            if not real_code:
                return self.func.start_line
            snippet = real_code
        try:
            with open(self.source_file, "r",
                      encoding="utf-8", errors="replace") as f:
                src_lines = f.readlines()
        except OSError:
            return self.func.start_line
        # Build candidate search strings: try progressively shorter
        # substrings of the cleaned snippet.
        candidates = []
        clean_snippet = snippet.strip()
        # Remove leading/trailing punctuation that's too common
        for prefix in ("{", "}", "(", ")", ";", "\t", " "):
            clean_snippet = clean_snippet.lstrip(prefix)
        if len(clean_snippet) >= 6:
            candidates.append(clean_snippet)
        for length in [60, 40, 20, 10, 6]:
            if len(clean_snippet) >= length:
                cand = clean_snippet[:length]
                # Skip candidates that are too generic (just whitespace/punct)
                stripped_cand = cand.strip(" \t{}();")
                if len(stripped_cand) >= 4:
                    candidates.append(cand)
        # Search within the function body range first (using the
        # pattern translator's best guess for start/end line).
        search_start = max(0, self.func.start_line - 1)
        search_end = min(self.func.end_line if self.func.end_line > 0
                         else len(src_lines), len(src_lines))
        for cand in candidates:
            for i in range(search_start, search_end):
                if cand in src_lines[i]:
                    return i + 1
        # Fall back to whole-file scan (but skip comment-only lines).
        for cand in candidates:
            for i, line in enumerate(src_lines):
                stripped = line.strip()
                # Skip pure comment lines
                if stripped.startswith("//") or stripped.startswith("/*"):
                    continue
                if cand in line:
                    return i + 1
        # Last resort: function start line (not 1)
        return self.func.start_line

    def _emit_local(self, decl: str) -> None:
        """Collect a local var declaration; emitted in var block by caller."""
        self.locals.append(decl)

    # ------------------------------------------------------------------
    def _translate_statement(self, stmt: str) -> None:
        pad = self._pad()
        s = stmt.strip()

        # --- Control flow ---
        if s.startswith("if") and s[2:3] in (" ", "("):
            self._translate_if(s)
            return
        if s.startswith("for") and (s[3:4] == "(" or s[3:4].isspace()):
            self._translate_for(s)
            return
        if s.startswith("while") and (s[5:6] == "(" or s[5:6].isspace()):
            self._translate_while(s)
            return
        if s.startswith("do") and (s[2:3] in (" ", "\t", "{") or len(s) == 2):
            self._translate_do(s)
            return
        if s.startswith("switch"):
            self._translate_switch(s)
            return
        if s.startswith("return"):
            self._translate_return(s)
            return
        if s in ("break;", "break"):
            self._emit(f"{pad}Break;")
            return
        if s in ("continue;", "continue"):
            self._emit(f"{pad}Continue;")
            return
        # --- goto statement ---
        # C: ``goto label;``  ->  Pascal: ``goto label;``
        # (Pascal uses the same ``goto`` keyword, but the label must be
        # declared in a ``label`` block. We collect labels and emit them
        # in the function's var/label section.)
        m_goto = re.match(r"^goto\s+(\w+)\s*;?\s*$", s)
        if m_goto:
            label_name = m_goto.group(1)
            self._emit(f"{pad}goto {label_name};")
            return
        # --- label definition ---
        # C: ``label:`` or ``label: statement``
        # Pascal: ``label:`` (a label is just an identifier followed by
        # a colon, same as C). We emit the label and then translate any
        # trailing statement on the same line.
        # A label is an identifier followed by ``:`` at the start of a
        # statement. We must distinguish this from a case label
        # (``case X:``) which is handled in _translate_switch.
        m_label = re.match(r"^(\w+)\s*:\s*(.*)$", s, re.DOTALL)
        if m_label:
            label_name = m_label.group(1)
            rest = m_label.group(2).strip()
            # Heuristic: if the identifier is followed by ``:`` and the
            # rest is empty or starts with a statement (not a type decl),
            # treat it as a label.
            # Common C labels: ``done:``, ``error:``, ``loop:``, etc.
            # Avoid matching ``default:`` (handled by switch) or
            # ``case X:`` (handled by switch).
            if label_name not in ("default", "case"):
                # Record the label so it can be emitted in the label block
                if not hasattr(self, "_labels"):
                    self._labels = []
                if label_name not in self._labels:
                    self._labels.append(label_name)
                self._emit(f"{pad}{label_name}:")
                # Translate any trailing statement after the label
                if rest:
                    self._translate_statement(rest)
                return
        if s.startswith("{") and s.endswith("}"):
            # Compound block: just recurse
            inner = s[1:-1]
            for sub in self._split_statements(inner):
                if sub.strip():
                    self._translate_statement(sub.strip())
            return

        # --- Local variable declarations ---
        # Only treat as a declaration if the first word is a known C type
        # keyword (int, char, etc.) or a known typedef. This prevents
        # statements like ``sum += i`` from being mis-parsed as a
        # declaration of variable ``+= i`` with type ``sum``.
        #
        # First, strip leading C storage class specifiers (auto, register,
        # static, extern, inline, restrict, volatile, __restrict, etc.)
        # so the actual type keyword becomes the first word.
        # ``auto int b = 5;`` -> ``int b = 5;`` (then parsed normally)
        s_decl = re.sub(
            r"^(?:const\s+|static\s+|auto\s+|register\s+|extern\s+|"
            r"inline\s+|restrict\s+|volatile\s+|__restrict\s+|"
            r"__inline\s+|__restrict__\s+|__inline__\s+|"
            r"__attribute__\s*\([^)]*\)\s+)*",
            "",
            s,
        ).strip()
        m = re.match(
            r"^(?P<type>(?:const\s+|unsigned\s+|signed\s+|volatile\s+)*"
            r"[\w]+(?:\s+\w+)*\s*\**)\s+(?P<rest>.+)$",
            s_decl, re.DOTALL,
        )
        if m and not s.startswith("("):
            type_str = m.group("type").strip()
            rest = m.group("rest").strip()
            # If rest starts with '=' (e.g. ``int b = 5`` was parsed as
            # type="int b", rest="= 5"), the last word of type_str is
            # actually the variable name. Move it to rest.
            if rest.startswith("="):
                parts = type_str.rsplit(None, 1)
                if len(parts) == 2:
                    var_name = parts[1]
                    type_str = parts[0]
                    rest = var_name + " " + rest
            # Check if the first word is a C type keyword or known typedef.
            # Strip trailing ``*`` (pointer) so that ``GtkTextTag*`` is
            # recognised as the typedef ``GtkTextTag``.
            first_word = type_str.split()[0] if type_str.split() else ""
            first_word_stripped = first_word.rstrip("*")
            if first_word in _C_TYPE_KEYWORDS or first_word in _KNOWN_TYPEDEFS \
                    or first_word_stripped in _C_TYPE_KEYWORDS \
                    or first_word_stripped in _KNOWN_TYPEDEFS \
                    or first_word in self._extra_known_types \
                    or first_word_stripped in self._extra_known_types:
                self._translate_local_decl(type_str, rest)
                return
            # If not a known type, fall through to assignment/call handling.
            # This is important so that ``sum += i`` is handled as an
            # assignment, not silently dropped as a bad declaration.

        # --- GTK / GLib / libc function call statements ---
        if self._try_call_statement(s):
            return

        # --- Simple assignment ---
        if self._try_assignment(s):
            return

        # --- Increment/decrement as statement (nwatch++; j--; etc.) ---
        m_inc = re.match(r"^(\w+)\s*\+\+\s*;?$", s)
        if m_inc:
            self._emit(f"{pad}Inc({m_inc.group(1)});")
            return
        m_dec = re.match(r"^(\w+)\s*--\s*;?$", s)
        if m_dec:
            self._emit(f"{pad}Dec({m_dec.group(1)});")
            return
        m_pre_inc = re.match(r"^\+\+\s*(\w+)\s*;?$", s)
        if m_pre_inc:
            self._emit(f"{pad}Inc({m_pre_inc.group(1)});")
            return
        m_pre_dec = re.match(r"^--\s*(\w+)\s*;?$", s)
        if m_pre_dec:
            self._emit(f"{pad}Dec({m_pre_dec.group(1)});")
            return

        # --- Fallback ---
        self._emit_todo(s, "unrecognised statement")

    # ------------------------------------------------------------------
    def _translate_if(self, s: str) -> None:
        pad = self._pad()
        # Find the condition between first ( and matching )
        open_idx = s.find("(")
        if open_idx < 0:
            self._emit_todo(s, "malformed if")
            return
        cond, after = self._extract_paren(s, open_idx)
        cond_pas = self._translate_cond(cond)
        self._emit(f"{pad}if {cond_pas} then")
        rest = self._translate_branch(after, expect_else=True)
        # If no else follows, patch the trailing 'end' to 'end;'
        if not rest.startswith("else"):
            self._patch_last_end_semicolon()
        # Handle else / else if chain
        while rest.startswith("else"):
            rest = rest[4:].lstrip()
            # Remove trailing ';' from the then-branch statement
            # (Pascal: "if cond then stmt else stmt;" - no ';' before else)
            self._strip_last_semicolon()
            if rest.startswith("if") and (rest[2:3] in (" ", "(")):
                # else if -> recurse on the if as a single statement
                if_open = rest.find("(")
                if if_open < 0:
                    self._emit_todo(rest, "else if malformed")
                    return
                cond2, if_after = self._extract_paren(rest, if_open)
                cond_pas2 = self._translate_cond(cond2)
                self._emit(f"{pad}else if {cond_pas2} then")
                rest = self._translate_branch(if_after, expect_else=True)
                if not rest.startswith("else"):
                    self._patch_last_end_semicolon()
            else:
                self._emit(f"{pad}else")
                rest = self._translate_branch(rest, expect_else=False)
        # After the if-else chain, there may be trailing statements that
        # were not separated by a ';' because the if-body was a brace
        # block ``{...}`` whose closing ``}`` ended the statement. These
        # trailing statements need to be translated as separate statements.
        # e.g. ``if(c){...} for(...);`` — after processing the if, ``rest``
        # is ``for(...);`` which we must now translate.
        rest = rest.strip()
        if rest:
            # The remaining text could be multiple statements; split and
            # translate each one. This is important for the case where an
            # if-block ``{...}`` is immediately followed by another
            # statement without a separating ';' (which is valid C since
            # the ``}`` ends the if-statement).
            for sub in self._split_statements(rest):
                sub = sub.strip()
                if sub:
                    self._translate_statement(sub)

    def _patch_last_end_semicolon(self) -> None:
        """If the last emitted line is 'end' (no semicolon), append ';'.
        Used after _translate_branch when no else follows."""
        if not self.lines:
            return
        last = self.lines[-1].rstrip()
        # Strip indent
        stripped = last.lstrip()
        if stripped == "end":
            indent = last[:len(last) - len(stripped)]
            self.lines[-1] = f"{indent}end;"

    def _strip_last_semicolon(self) -> None:
        """Remove trailing ';' from the last emitted line.
        Used before 'else' in Pascal: "if cond then stmt;" -> "if cond then stmt"
        (Pascal doesn't allow ';' before 'else')."""
        if not self.lines:
            return
        last = self.lines[-1].rstrip()
        if last.endswith(";"):
            self.lines[-1] = last[:-1]

    def _translate_branch(self, after: str, expect_else: bool) -> str:
        """Translate an if/else body. Return the residual text after the body.

        If ``expect_else`` is True, the trailing ``;`` after ``end`` is omitted
        so a following ``else`` parses correctly.
        """
        pad = self._pad()
        rest = after.lstrip()
        end_token = "end" if expect_else else "end;"
        if rest.startswith("{"):
            body, tail = self._extract_block(rest)
            self._emit(f"{pad}begin")
            self.indent += 1
            for sub in self._split_statements(body):
                if sub.strip():
                    self._translate_statement(sub.strip())
            self.indent -= 1
            self._emit(f"{pad}{end_token}")
            rest = tail.lstrip()
        else:
            # Single statement - extract until ';'
            # BUT: if 'else' follows on the same line, we need to split
            # at the ';' before 'else', not at a later ';'
            # e.g. "running=0; else running=1;" -> stmt="running=0", rest="else running=1;"
            # Find the first ';' that is followed by 'else' or end of statement
            stmt = ""
            depth = 0
            i = 0
            found_semi = -1
            while i < len(rest):
                ch = rest[i]
                if ch in "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif ch == ";" and depth == 0:
                    found_semi = i
                    break
                elif ch == "e" and depth == 0 and i > 0:
                    # Check for 'else' keyword at this position
                    if rest[i:i+4] == "else" and (i == 0 or rest[i-1] in " \t\n}"):
                        # 'else' found before any ';' - the statement is
                        # everything before 'else'
                        # This shouldn't happen in valid C (if without body
                        # before else), but handle it
                        break
                i += 1
            if found_semi >= 0:
                stmt = rest[:found_semi].strip()
                rest = rest[found_semi + 1:].lstrip()
            else:
                stmt = rest.strip()
                rest = ""
            self.indent += 1
            if stmt:
                self._translate_statement(stmt.strip())
            else:
                # Empty body (e.g. ``if (a) ;``) — emit an empty
                # begin/end block so Pascal has a valid statement
                # after ``then`` / ``else``.
                self._emit(f"{pad}begin")
                self._emit(f"{pad}{end_token}")
            self.indent -= 1
            # In Pascal, single-statement if body doesn't need begin/end,
            # but we need to handle the semicolon correctly for else.
            # If expect_else and rest starts with 'else', don't emit ';'.
            # The _translate_if caller handles the else.
        return rest

    def _translate_for(self, s: str) -> None:
        pad = self._pad()
        open_idx = s.find("(")
        cond, after = self._extract_paren(s, open_idx)
        # for (init; test; step) — split keeping empty parts.
        # ``_split_top_level`` skips empty fields like str.split, but a
        # for-loop's three slots can each be empty (``for(;;)``,
        # ``for(;i<N;i++)``, ``for(i=0;;i++)``), so we use a custom splitter
        # that preserves empty parts.
        parts = _split_for_header(cond)
        if len(parts) != 3:
            self._emit_todo(s, "complex for loop")
            return
        init, test, step = parts
        init = init.strip()
        test = test.strip()
        step = step.strip()

        # ---- Pattern 0: bodyless for-loop -> while loop ----
        # C: for(init; cond; step);   (body is just ';' or empty braces)
        # Common linear-search idiom, e.g.:
        #   for(z=0; z<DIMBUF-5 && bufferI[z]!=RX16; z++);
        # Pascal's for-to-do iterates over a fixed range and cannot express
        # "advance until condition fails", so we always emit a while-loop.
        # This must run BEFORE Pattern 1, because Pattern 1 would otherwise
        # match the simple bodyless form ``for(i=0; i<N; i++);`` and emit
        # an empty-body ``for i := 0 to N-1 do begin end;``.
        body_str, tail_str = self._extract_block(after.lstrip())
        body_is_empty = self._is_empty_for_body(body_str, tail_str)
        if body_is_empty:
            self._translate_bodyless_for(init, test, step)
            self._process_for_trailing(tail_str, pad)
            return

        # ---- Pattern 1: for (var=start; var<N or var<=N; var++) ----
        # Handles the common form:
        #   for (i=0; i<N; i++) body
        #   for (i=0; i<=N; i++) body
        # Also handles ExprList inits like:
        #   for (i=0, count=sum=0; i<N; i++) body
        # — the first assignment is the loop variable, the rest are emitted
        # as extra init statements before the for loop.
        m = re.match(r"(?:int\s+|unsigned\s+)?(\w+)\s*=\s*([^,;]+)", init)
        if m:
            var = m.group(1)
            start = self._translate_expr(m.group(2).strip())
            # Collect any extra comma-separated inits (e.g. "count=sum=0")
            extra_inits: list[str] = []
            if "," in init:
                init_parts = _split_top_level(init, ",")
                # Skip the first part (the loop var assignment); emit the
                # rest as separate statements before the for loop.
                for extra in init_parts[1:]:
                    extra = extra.strip()
                    if extra:
                        extra_inits.append(extra)
            # Check condition: var<N or var<=N
            # IMPORTANT: the ``<`` pattern must NOT match ``<=`` — we use
            # a negative lookahead ``(?!=)`` after ``<`` to ensure the
            # operator is strictly ``<`` and not ``<=``. Without this,
            # ``i<=N`` would match the ``<`` pattern with end_expr ``=N``
            # and produce the wrong ``for i := 0 to (=N) - 1 do`` output.
            # ALSO IMPORTANT: the end expression must NOT contain ``&&`` or
            # ``||`` — a compound condition like ``i<N && i<M`` cannot be
            # expressed as a Pascal for-to-do (which iterates over a fixed
            # range). Such loops must be translated as while loops instead.
            test_match_lt = re.match(
                rf"\s*{re.escape(var)}\s*<(?!=)\s*(.+)$", test
            )
            test_match_le = re.match(
                rf"\s*{re.escape(var)}\s*<=\s*(.+)$", test
            )
            # Reject compound conditions (containing && or ||) — they need
            # while loops, not for-to-do.
            if test_match_lt and ("&&" in test_match_lt.group(1) or
                                   "||" in test_match_lt.group(1)):
                test_match_lt = None
            if test_match_le and ("&&" in test_match_le.group(1) or
                                   "||" in test_match_le.group(1)):
                test_match_le = None
            # Check step: var++ or ++var or var = var+1
            is_simple_inc = (
                step == f"{var}++" or step == f"++{var}" or
                (step.startswith(f"{var} = ") and
                 (step.endswith("+ 1") or step.endswith("+1")))
            )
            if (test_match_lt or test_match_le) and is_simple_inc:
                # Extract the loop body. C for-loop bodies come in two
                # forms: a brace block ``{...}`` or a single statement
                # followed by ``;``. We must handle BOTH — previously
                # only the brace-block form was handled, which caused
                # single-statement bodies like ``for(...) sum += i;`` to
                # be silently dropped (emitted as an empty for-to-do).
                body_str, tail_str = self._extract_block(after.lstrip())
                body_stmts: list[str] = []
                if body_str:
                    # Brace block: split into statements
                    body_stmts = self._split_statements(body_str)
                elif tail_str:
                    # Single statement: extract until ';' (respecting
                    # parens so function-call args aren't split).
                    stmt_text = self._extract_single_stmt(tail_str)
                    if stmt_text:
                        body_stmts = [stmt_text]
                # Emit extra inits first (use _translate_statement so
                # chained assignments like ``count=sum=0`` are handled)
                for extra in extra_inits:
                    self._translate_statement(extra + ";")
                if test_match_lt:
                    end_pas = self._translate_expr(
                        test_match_lt.group(1).strip()
                    )
                    self._emit(
                        f"{pad}for {var} := {start} to ({end_pas}) - 1 do"
                    )
                else:  # test_match_le
                    end_pas = self._translate_expr(
                        test_match_le.group(1).strip()
                    )
                    self._emit(
                        f"{pad}for {var} := {start} to {end_pas} do"
                    )
                self._emit(f"{pad}begin")
                self.indent += 1
                for sub in body_stmts:
                    if sub.strip():
                        self._translate_statement(sub.strip())
                # Handle non-standard steps (defensive; the simple-inc
                # check above should already cover var++ / ++var / var=var+1)
                if step and step != f"{var}++" and step != f"++{var}" \
                        and not (step.startswith(f"{var} = ") and
                                 (step.endswith("+ 1") or
                                  step.endswith("+1"))):
                    self._translate_statement(step + ";")
                self.indent -= 1
                self._emit(f"{pad}end;")
                self._process_for_trailing(tail_str, pad)
                return

            # ---- Pattern 1c: for (var=start; compound_cond; var++) ----
            # A compound condition (containing ``&&`` or ``||``) cannot be
            # expressed as a Pascal for-to-do. Translate as a while loop:
            #   var := start;
            #   while <cond> do begin body; Inc(var); end;
            # This handles e.g. ``for(i=0; i<N && i<M; i++) body``
            if is_simple_inc and test and ("&&" in test or "||" in test):
                body_str, tail_str = self._extract_block(after.lstrip())
                body_stmts = []
                if body_str:
                    body_stmts = self._split_statements(body_str)
                elif tail_str:
                    stmt_text = self._extract_single_stmt(tail_str)
                    if stmt_text:
                        body_stmts = [stmt_text]
                for extra in extra_inits:
                    self._translate_statement(extra + ";")
                cond_pas = self._translate_expr(test)
                self._emit(f"{pad}{var} := {start};")
                self._emit(f"{pad}while {cond_pas} do")
                self._emit(f"{pad}begin")
                self.indent += 1
                for sub in body_stmts:
                    if sub.strip():
                        self._translate_statement(sub.strip())
                self._emit(f"{self._pad()}Inc({var});")
                self.indent -= 1
                self._emit(f"{pad}end;")
                self._process_for_trailing(tail_str, pad)
                return

        # ---- Pattern 1b: for (var=start; ; var++) -> while True ----
        # for (i=0; ; i++) body  is an infinite loop with an init.
        # Emit the init, then ``while True do begin body; step; end;``.
        m = re.match(r"(?:int\s+|unsigned\s+)?(\w+)\s*=\s*([^,;]+)", init)
        if m and not test:
            var = m.group(1)
            start = self._translate_expr(m.group(2).strip())
            extra_inits: list[str] = []
            if "," in init:
                init_parts = _split_top_level(init, ",")
                for extra in init_parts[1:]:
                    extra = extra.strip()
                    if extra:
                        extra_inits.append(extra + ";")
            body_str, tail_str = self._extract_block(after.lstrip())
            body_stmts = []
            if body_str:
                body_stmts = self._split_statements(body_str)
            elif tail_str:
                stmt_text = self._extract_single_stmt(tail_str)
                if stmt_text:
                    body_stmts = [stmt_text]
            for extra in extra_inits:
                self._emit(f"{pad}{extra}")
            self._emit(f"{pad}{var} := {start};")
            self._emit(f"{pad}while True do")
            self._emit(f"{pad}begin")
            self.indent += 1
            for sub in body_stmts:
                if sub.strip():
                    self._translate_statement(sub.strip())
            if step:
                # Emit step as Inc/Dec for simple forms, else as statement
                if step == f"{var}++" or step == f"++{var}":
                    self._emit(f"{self._pad()}Inc({var});")
                elif step == f"{var}--" or step == f"--{var}":
                    self._emit(f"{self._pad()}Dec({var});")
                else:
                    self._translate_statement(step + ";")
            self.indent -= 1
            self._emit(f"{pad}end;")
            self._process_for_trailing(tail_str, pad)
            return

        # ---- Pattern 2: empty init, e.g. for(;j<N;j++) ----
        # Extract the loop variable from the step (j++ -> var=j)
        # or from the condition (j<N -> var=j)
        if not init:
            step_var = None
            # Try to extract from step: j++ or ++j or j-- or --j
            m_step = re.match(r"\s*(?:\+\+|p\+\+)?(\w+)\s*\+\+", step)
            if not m_step:
                m_step = re.match(r"\s*\+\+(\w+)", step)
            if not m_step:
                m_step = re.match(r"\s*(\w+)\s*\+\+", step)
            if m_step:
                step_var = m_step.group(1)
            # Try to extract from condition: j<N or j<=N
            if not step_var:
                m_cond = re.match(r"\s*(\w+)\s*[<<=]", test)
                if m_cond:
                    step_var = m_cond.group(1)
            if step_var and test:
                # Translate as while loop (no TODO needed)
                cond_pas = self._translate_expr(test)
                body, tail = self._extract_block(after.lstrip())
                # If body is empty (single statement, not a block),
                # extract the statement from tail
                body_stmts = []
                if body:
                    body_stmts = self._split_statements(body)
                elif tail:
                    # Single statement: extract until ';'
                    stmt_text = tail.split(";", 1)[0].strip()
                    if stmt_text:
                        body_stmts = [stmt_text]
                self._emit(f"{pad}while {cond_pas} do")
                self._emit(f"{pad}begin")
                self.indent += 1
                for sub in body_stmts:
                    if sub.strip():
                        self._translate_statement(sub.strip())
                # Emit step
                if step:
                    if re.match(rf"\s*{re.escape(step_var)}\s*\+\+", step):
                        self._emit(f"{self._pad()}Inc({step_var});")
                    elif re.match(rf"\s*{re.escape(step_var)}\s*--", step):
                        self._emit(f"{self._pad()}Dec({step_var});")
                    elif re.match(rf"\s*\+\+{re.escape(step_var)}", step):
                        self._emit(f"{self._pad()}Inc({step_var});")
                    elif re.match(rf"\s*--{re.escape(step_var)}", step):
                        self._emit(f"{self._pad()}Dec({step_var});")
                    else:
                        self._translate_statement(step + ";")
                self.indent -= 1
                self._emit(f"{pad}end;")
                self._process_for_trailing(tail_str, pad)
                return

        # ---- Pattern 3: init present, handle various forms ----
        if init:
            # 3a: Augmented assignment init (z+=10, z-=5, z*=2, etc.)
            m_aug = re.match(r"(?:int\s+|unsigned\s+)?(\w+)\s*([+\-*/%&|^])=\s*(.+)", init)
            if m_aug:
                var = m_aug.group(1)
                op = m_aug.group(2)
                rhs = self._translate_expr(m_aug.group(3).strip())
                op_map = {"+": "+", "-": "-", "*": "*", "/": "div",
                          "%": "mod", "&": "and", "|": "or", "^": "xor"}
                pas_op = op_map.get(op, "+")
                # Emit init as augmented assignment
                self._emit(f"{pad}{var} := {var} {pas_op} {rhs};")
                # Now handle as while loop
                cond_pas = self._translate_expr(test)
                body, tail = self._extract_block(after.lstrip())
                body_stmts = []
                if body:
                    body_stmts = self._split_statements(body)
                elif tail:
                    stmt_text = tail.split(";", 1)[0].strip()
                    if stmt_text:
                        body_stmts = [stmt_text]
                self._emit(f"{pad}while {cond_pas} do")
                self._emit(f"{pad}begin")
                self.indent += 1
                for sub in body_stmts:
                    if sub.strip():
                        self._translate_statement(sub.strip())
                # Emit step
                if step:
                    if re.match(rf"\s*{re.escape(var)}\s*\+\+", step):
                        self._emit(f"{self._pad()}Inc({var});")
                    elif re.match(rf"\s*{re.escape(var)}\s*--", step):
                        self._emit(f"{self._pad()}Dec({var});")
                    elif re.match(rf"\s*\+\+{re.escape(var)}", step):
                        self._emit(f"{self._pad()}Inc({var});")
                    elif re.match(rf"\s*--{re.escape(var)}", step):
                        self._emit(f"{self._pad()}Dec({var});")
                    elif re.match(rf"\s*{re.escape(var)}\s*\+=\s*(.+)", step):
                        m_s = re.match(rf"\s*{re.escape(var)}\s*\+=\s*(.+)", step)
                        self._emit(f"{self._pad()}Inc({var}, {self._translate_expr(m_s.group(1).strip())});")
                    elif re.match(rf"\s*{re.escape(var)}\s*-=\s*(.+)", step):
                        m_s = re.match(rf"\s*{re.escape(var)}\s*-=\s*(.+)", step)
                        self._emit(f"{self._pad()}Dec({var}, {self._translate_expr(m_s.group(1).strip())});")
                    else:
                        self._translate_statement(step + ";")
                self.indent -= 1
                self._emit(f"{pad}end;")
                self._process_for_trailing(tail_str, pad)
                return

            # 3b: Comma-separated init (a=0, b=func()) - first is loop var
            if "," in init:
                init_parts = _split_top_level(init, ",")
                if init_parts:
                    # First part: loop variable init
                    first = init_parts[0].strip()
                    m_first = re.match(r"(?:int\s+|unsigned\s+)?(\w+)\s*=\s*(.+)", first)
                    if m_first:
                        var = m_first.group(1)
                        start = self._translate_expr(m_first.group(2).strip())
                        # Emit extra inits
                        for extra in init_parts[1:]:
                            extra = extra.strip()
                            if extra:
                                self._translate_statement(extra + ";")
                        # Now handle as while loop
                        cond_pas = self._translate_expr(test)
                        body, tail = self._extract_block(after.lstrip())
                        body_stmts = []
                        if body:
                            body_stmts = self._split_statements(body)
                        elif tail:
                            stmt_text = tail.split(";", 1)[0].strip()
                            if stmt_text:
                                body_stmts = [stmt_text]
                        self._emit(f"{pad}while {cond_pas} do")
                        self._emit(f"{pad}begin")
                        self.indent += 1
                        for sub in body_stmts:
                            if sub.strip():
                                self._translate_statement(sub.strip())
                        # Emit step
                        if step:
                            self._translate_statement(step + ";")
                        self.indent -= 1
                        self._emit(f"{pad}end;")
                        self._process_for_trailing(tail_str, pad)
                        return

            # 3c: Simple assignment init (i=0) with i+=N step
            m_init = re.match(r"(?:int\s+|unsigned\s+)?(\w+)\s*=\s*(.+)", init)
            if m_init:
                var = m_init.group(1)
                start = self._translate_expr(m_init.group(2).strip())
                # Check if step is var += N
                m_step = re.match(rf"\s*{re.escape(var)}\s*\+=\s*(.+)", step)
                if m_step:
                    step_amount = self._translate_expr(m_step.group(1).strip())
                    cond_pas = self._translate_expr(test)
                    body, tail = self._extract_block(after.lstrip())
                    body_stmts = []
                    if body:
                        body_stmts = self._split_statements(body)
                    elif tail:
                        stmt_text = tail.split(";", 1)[0].strip()
                        if stmt_text:
                            body_stmts = [stmt_text]
                    self._emit(f"{pad}{var} := {start};")
                    self._emit(f"{pad}while {cond_pas} do")
                    self._emit(f"{pad}begin")
                    self.indent += 1
                    for sub in body_stmts:
                        if sub.strip():
                            self._translate_statement(sub.strip())
                    self._emit(f"{self._pad()}Inc({var}, {step_amount});")
                    self.indent -= 1
                    self._emit(f"{pad}end;")
                    self._process_for_trailing(tail_str, pad)
                    return

        # ---- Fallback: while loop with TODO ----
        self._emit_todo(s, "for loop with non-standard form; rewritten as while")
        self._emit(f"{pad}// init: {init}")
        cond_pas = self._translate_expr(test) if test else "True"
        self._emit(f"{pad}while {cond_pas} do")
        body, tail = self._extract_block(after.lstrip())
        self._emit(f"{pad}begin")
        self.indent += 1
        for sub in self._split_statements(body):
            if sub.strip():
                self._translate_statement(sub.strip())
        if step:
            self._translate_statement(step + ";")
        self.indent -= 1
        self._emit(f"{pad}end;")

    def _process_for_trailing(self, tail_str: str, pad: str) -> None:
        """Process any statements that follow a for-loop's body.

        When ``_split_statements`` fails to separate a for-loop from the
        following statement (e.g. ``for(...){...} for(...);``), the
        trailing text ends up in ``tail_str``. This method splits and
        translates those trailing statements so they are not silently
        dropped.

        For brace-block bodies, ``tail_str`` is whatever follows the ``}``.
        For single-statement bodies, ``tail_str`` still contains the body
        statement followed by any trailing statements — we skip the body
        statement (already processed by the caller) and process the rest.
        """
        if not tail_str:
            return
        trailing = tail_str.strip()
        if not trailing:
            return
        # Split into statements
        stmts = self._split_statements(trailing)
        if not stmts:
            return
        # Heuristic: if the first statement starts with a control-flow
        # keyword or a declaration, it's a NEW statement (trailing content
        # from a brace-block body). Otherwise, it may be the body statement
        # that was already processed (single-statement body) — skip it.
        first = stmts[0].strip()
        first_word = first.split()[0] if first.split() else ""
        is_new_stmt = (
            (first.startswith("for") and (first[3:4] == "(" or first[3:4].isspace())) or
            (first.startswith("while") and (first[5:6] == "(" or first[5:6].isspace())) or
            (first.startswith("if") and (first[2:3] == "(" or first[2:3].isspace())) or
            first.startswith("switch") or
            first.startswith("return") or
            first.startswith("break") or
            first.startswith("continue") or
            first.startswith("do") or
            first_word in _C_TYPE_KEYWORDS or
            first_word in _KNOWN_TYPEDEFS
        )
        start_idx = 0 if is_new_stmt else 1
        for i in range(start_idx, len(stmts)):
            sub = stmts[i].strip()
            if sub:
                self._translate_statement(sub)

    def _is_empty_for_body(self, body_str: str, tail_str: str) -> bool:
        """Return True if the for-loop body is empty.

        A bodyless C for-loop has the form ``for(init; cond; step);`` (note
        the trailing semicolon). The pattern-based parser leaves ``after``
        starting with ``;`` (so ``_extract_block`` returns ``("", ";...")``)
        or with ``{}`` / ``{ ; }`` (an empty Compound).

        We treat as empty:
          * No block AND the tail starts with ``;`` (i.e. body is just ``;``)
            — this is the ``for(...);`` form. We must NOT treat the case where
            the tail starts with a real statement (``for(...) stmt;``) as
            empty, even though body_str is also "" in that case.
          * Block was present (``{}``) AND body_str is empty or only
            whitespace — this is the ``for(...) {}`` form. We distinguish
            this from the single-statement case by checking that the tail
            is empty (i.e. ``_extract_block`` consumed the whole body).
          * Block containing only empty statements (only ``;`` characters)
        """
        # Case 1: body is just a stray semicolon (most common form).
        # This is the only case where body_str is "" and the body is empty;
        # the other case where body_str is "" is a single-statement body
        # like ``for(...) stmt;`` where _extract_block returns ("", "stmt;").
        if not body_str and tail_str.lstrip().startswith(";"):
            return True
        # Case 2: empty brace block. body_str is "" AND the tail is empty
        # (meaning _extract_block consumed ``{}`` and there's nothing left).
        # If tail_str still has content, body_str="" means single-statement.
        if body_str == "" and not tail_str.strip():
            return True
        # Case 3: body_str non-empty but only contains empty statements
        if body_str:
            cleaned = body_str.replace(" ", "").replace("\t", "")
            cleaned = cleaned.replace("\n", "").replace("\r", "")
            if cleaned and all(ch == ";" for ch in cleaned):
                return True
            # Also handle truly empty block (e.g. "{}" gave body_str="")
            if not cleaned:
                return True
        return False

    def _translate_bodyless_for(self, init: str, test: str, step: str) -> None:
        """Translate a bodyless C for-loop into a Pascal while-loop.

        C:        for(init; cond; step);
        Pascal:
          <init>;
          while <cond> do
          begin
            <step>;
          end;

        A bodyless for-loop is a common C idiom for "advance the loop
        variable until the condition fails" (e.g. linear search). Pascal's
        for-to-do cannot express this naturally (it iterates over a fixed
        range, not until a condition fails), so we always emit a while-loop.

        If the condition is missing (e.g. ``for(init;;step);``), we emit
        ``while True do`` to preserve the original (infinite-loop) semantics.
        If the step is also missing (e.g. ``for(;;);``), the body is empty.
        """
        pad = self._pad()
        # ---- Emit init statement(s) before the loop ----
        # The init may be a comma-separated list (a=0, b=10) or a single
        # assignment / declaration. We use the same comma-splitting logic
        # as Pattern 3b so chained inits are emitted as separate statements.
        if init:
            if "," in init:
                # Comma-separated init: emit each as a separate statement
                init_parts = _split_top_level(init, ",")
                for part in init_parts:
                    part = part.strip()
                    if part:
                        self._translate_statement(part + ";")
            else:
                # Single init: strip any leading type (e.g. "int z=0" -> "z=0")
                # so it becomes a plain assignment.
                m = re.match(r"(?:int\s+|unsigned\s+|char\s+|long\s+|short\s+)+(.+)", init)
                if m:
                    self._translate_statement(m.group(1).strip() + ";")
                else:
                    self._translate_statement(init + ";")
        # ---- Determine the condition ----
        if not test:
            # for(init;;step); -> infinite loop -> while True do
            cond_pas = "True"
        else:
            cond_pas = self._translate_expr(test)
        # ---- Emit the while loop with the step as its body ----
        self._emit(f"{pad}while {cond_pas} do")
        self._emit(f"{pad}begin")
        self.indent += 1
        if step:
            # Try to extract the step variable so we can emit Inc()/Dec()
            # for the common i++/i--/i+=K forms (more idiomatic Pascal).
            m_inc = re.match(r"\s*(\w+)\s*\+\+\s*$", step) or \
                    re.match(r"\s*\+\+(\w+)\s*$", step)
            m_dec = re.match(r"\s*(\w+)\s*--\s*$", step) or \
                    re.match(r"\s*--(\w+)\s*$", step)
            m_inc_k = re.match(r"\s*(\w+)\s*\+=\s*(.+)$", step)
            m_dec_k = re.match(r"\s*(\w+)\s*-=\s*(.+)$", step)
            if m_inc:
                self._emit(f"{self._pad()}Inc({m_inc.group(1)});")
            elif m_dec:
                self._emit(f"{self._pad()}Dec({m_dec.group(1)});")
            elif m_inc_k:
                self._emit(f"{self._pad()}Inc({m_inc_k.group(1)}, "
                           f"{self._translate_expr(m_inc_k.group(2).strip())});")
            elif m_dec_k:
                self._emit(f"{self._pad()}Dec({m_dec_k.group(1)}, "
                           f"{self._translate_expr(m_dec_k.group(2).strip())});")
            else:
                self._translate_statement(step + ";")
        self.indent -= 1
        self._emit(f"{pad}end;")

    def _translate_while(self, s: str) -> None:
        pad = self._pad()
        open_idx = s.find("(")
        cond, after = self._extract_paren(s, open_idx)
        cond_pas = self._translate_cond(cond)
        body, tail = self._extract_block(after.lstrip())
        self._emit(f"{pad}while {cond_pas} do")
        self._emit(f"{pad}begin")
        self.indent += 1
        for sub in self._split_statements(body):
            if sub.strip():
                self._translate_statement(sub.strip())
        self.indent -= 1
        self._emit(f"{pad}end;")

    def _translate_do(self, s: str) -> None:
        """C do-while: ``do body while (cond);`` -> Pascal ``repeat body until not (cond);``

        Handles:
          * Brace body: ``do { ... } while (cond);``
          * Single-statement body: ``do b++; while (cond);``
          * Empty body: ``do ; while (cond);`` or ``do {} while (cond);``
        """
        pad = self._pad()
        rest = s[2:].lstrip()  # skip "do"
        # Try brace body first
        if rest.startswith("{"):
            body, tail = self._extract_block(rest)
            body = body.strip()
        else:
            # Single-statement or empty body: extract until "while"
            # Find the "while" keyword that ends the do-body
            # We need to find "while" that is at the top level (not inside
            # a nested expression). Walk through the string tracking
            # paren/brace depth.
            depth = 0
            i = 0
            while_idx = -1
            while i < len(rest):
                ch = rest[i]
                if ch in "{(":
                    depth += 1
                elif ch in "})":
                    depth -= 1
                elif depth == 0 and rest[i:i+5] == "while" and \
                        (i == 0 or rest[i-1] in " \t\n}"):
                    while_idx = i
                    break
                i += 1
            if while_idx < 0:
                self._emit_todo(s, "do-while without while")
                return
            body = rest[:while_idx].strip()
            tail = rest[while_idx:]
        m = re.match(r"\s*while\s*\((.*?)\)\s*;?", tail, re.DOTALL)
        if not m:
            self._emit_todo(s, "do-while without while")
            return
        cond = self._translate_cond(m.group(1))
        self._emit(f"{pad}repeat")
        self.indent += 1
        if body:
            for sub in self._split_statements(body):
                if sub.strip():
                    self._translate_statement(sub.strip())
        # Empty body is fine for Pascal repeat/until — it just loops
        # with no statements, which is valid.
        self.indent -= 1
        self._emit(f"{pad}until not ({cond});")

    def _translate_switch(self, s: str) -> None:
        pad = self._pad()
        open_idx = s.find("(")
        cond, after = self._extract_paren(s, open_idx)
        body, tail = self._extract_block(after.lstrip())
        cond_pas = self._translate_expr(cond)
        self._emit(f"{pad}case {cond_pas} of")
        self.indent += 1

        # ---- Extract the ``default:`` section first ----
        # ``default:`` may appear anywhere in the switch body (typically at
        # the end). We split the body at the ``default:`` keyword so the
        # case-splitting below doesn't accidentally include the default
        # body in the last case.
        default_body = ""
        # Use word-boundary match so ``default`` is a whole word, and
        # require a ``:`` after it (the C syntax is ``default:``).
        m_default = re.search(r"\bdefault\s*:", body)
        if m_default:
            default_body = body[m_default.end():]
            body = body[:m_default.start()]

        # ---- Split on ``case`` labels ----
        # Each part is ``<label> : <body until next case or end>``
        case_parts = re.split(r"\bcase\s+", body)
        for cp in case_parts[1:]:  # skip text before first 'case'
            label, _, cbody = cp.partition(":")
            label = label.strip()
            if not label:
                continue
            # Pascal label range (e.g. ``1`` or ``1..5`` or ``'a'``)
            label_pas = self._translate_expr(label)
            stmts = self._split_statements(cbody)
            # Strip 'break;' at end (Pascal case blocks don't need break)
            stmts = [st for st in stmts if st.strip() not in ("break",)]
            self._emit(f"{self._pad()}{label_pas}:")
            self._emit(f"{self._pad()}begin")
            self.indent += 1
            for st in stmts:
                if st.strip():
                    self._translate_statement(st.strip() + ";")
            self.indent -= 1
            self._emit(f"{self._pad()}end;")

        # ---- Emit the ``default:`` section as Pascal ``else`` ----
        if default_body or m_default:
            stmts = [st for st in self._split_statements(default_body)
                     if st.strip() not in ("break",)]
            self._emit(f"{self._pad()}else")
            self._emit(f"{self._pad()}begin")
            self.indent += 1
            for st in stmts:
                if st.strip():
                    self._translate_statement(st.strip() + ";")
            self.indent -= 1
            self._emit(f"{self._pad()}end;")

        self.indent -= 1
        self._emit(f"{pad}end;")

    def _translate_return(self, s: str) -> None:
        pad = self._pad()
        m = re.match(r"return\s*(.*?)\s*;?\s*$", s, re.DOTALL)
        if not m:
            self._emit_todo(s, "return")
            return
        expr = m.group(1).strip()
        if not expr:
            self._emit(f"{pad}Exit;")
            return
        expr_pas = self._translate_expr(expr)
        if self.func.return_type.strip() in ("void", ""):
            self._emit(f"{pad}Exit;")
        else:
            self._emit(f"{pad}Exit({expr_pas});")

    # ------------------------------------------------------------------
    def _translate_local_decl(self, type_str: str, rest: str) -> None:
        # Strip C storage class specifiers (auto, register, static, extern,
        # inline, restrict, volatile, __restrict, etc.) — Pascal doesn't
        # have these; they are C-only concepts.
        type_clean = type_str.strip()
        type_clean = re.sub(
            r"\b(auto|register|static|extern|inline|restrict|volatile"
            r"|__restrict|__restrict__|__inline|__inline__|__attribute__"
            r"\s*\([^)]*\))\s+",
            "",
            type_clean,
        ).strip()
        # In C, ``*`` can appear on the type or on individual declarators:
        #   ``int* a, b;``  — a is ``int*``, b is ``int`` (NOT ``int*``)
        #   ``int *c, d;``  — c is ``int*``, d is ``int``
        #   ``int *e, *f;`` — both are ``int*``
        # C's rule: the ``*`` on the type applies to the FIRST declarator
        # only; subsequent declarators need their own ``*``.
        base_type = type_clean.replace("*", "").strip()
        type_had_star = "*" in type_clean
        pas_type_base = c_type_to_pascal(base_type)
        pas_type_ptr = c_type_to_pascal(base_type + " *")
        # rest may be: "x = 5, y = 6" or "x[10]" etc.
        declarators = _split_top_level(rest.rstrip(";"))
        first_decl = True
        for d in declarators:
            d = d.strip()
            if not d:
                continue
            # Determine if this declarator is a pointer.
            # A declarator is a pointer if:
            #   - it starts with its own ``*`` (e.g. ``*c``), OR
            #   - the type had ``*`` AND this is the first declarator
            #     AND the declarator doesn't start with ``*``
            #     (e.g. ``int* a`` — a is pointer because of the type's ``*``)
            is_ptr = d.startswith("*")
            if not is_ptr and type_had_star and first_decl:
                is_ptr = True
            first_decl = False
            # Determine the Pascal type for this declarator
            pas_type = pas_type_ptr if is_ptr else pas_type_base
            # Handle array
            m_arr = re.match(r"\*?\s*(\w+)\s*\[([^\]]*)\]\s*(?:=\s*(.*))?$", d)
            if m_arr:
                name = self._pascalify(m_arr.group(1))
                size = m_arr.group(2).strip()
                init = m_arr.group(3)
                if not size:
                    self._emit_local(f"  {name}: array of {pas_type};")
                else:
                    self._emit_local(
                        f"  {name}: array[0..{size}-1] of {pas_type};"
                    )
                if init:
                    # ---- Translate array initializers ----
                    # C: ``char cmd[32]="";``  ->  Pascal: ``cmd[0] := #0;``
                    # C: ``char cmd[32]="hi";`` ->  Pascal: ``StrPCopy(cmd, 'hi');``
                    # C: ``int arr[5]={0};``   ->  Pascal: ``FillChar(arr, SizeOf(arr), 0);``
                    # C: ``int arr[5]={1,2,3};`` -> Pascal: ``arr[0] := 1; arr[1] := 2; arr[2] := 3;``
                    pad = self._pad()
                    init = init.strip()
                    # Case 1: string literal initializer (char arrays)
                    if init.startswith('"') and init.endswith('"'):
                        inner = init[1:-1]
                        if not inner:
                            # Empty string: just null-terminate
                            self._emit(f"{pad}{name}[0] := #0;")
                        else:
                            # Non-empty string: use StrPCopy to copy the
                            # string into the char array (Pascal idiom).
                            # Translate C escape sequences.
                            pas_str = self._translate_c_string(inner)
                            self._emit(f"{pad}StrPCopy({name}, {pas_str});")
                    # Case 2: brace initializer {a, b, c, ...}
                    elif init.startswith("{") and init.endswith("}"):
                        inner = init[1:-1].strip()
                        # Split on top-level commas
                        elems = _split_top_level(inner, ",")
                        elems = [e.strip() for e in elems if e.strip()]
                        # Special case: {0} or {0,0,0,...} -> FillChar
                        if elems and all(e == "0" for e in elems):
                            self._emit(
                                f"{pad}FillChar({name}, SizeOf({name}), 0);"
                            )
                        else:
                            # Emit individual element assignments
                            for idx, elem in enumerate(elems):
                                elem_pas = self._translate_expr(elem)
                                self._emit(
                                    f"{pad}{name}[{idx}] := {elem_pas};"
                                )
                    else:
                        # Unrecognised initializer form — emit TODO
                        self._emit_todo(d + ";", "array initializer")
                continue
            m_init = re.match(r"(\w+)\s*=\s*(.+)$", d, re.DOTALL)
            if m_init:
                name = self._pascalify(m_init.group(1))
                val = self._translate_expr(m_init.group(2).strip())
                self._emit_local(f"  {name}: {pas_type};")
                pad = self._pad()
                self._emit(f"{pad}{name} := {val};")
                continue
            # Pointer with optional initializer: ``*pd = &a``
            m_ptr_init = re.match(r"\*\s*(\w+)\s*=\s*(.+)$", d, re.DOTALL)
            if m_ptr_init:
                name = self._pascalify(m_ptr_init.group(1))
                val = self._translate_expr(m_ptr_init.group(2).strip())
                self._emit_local(f"  {name}: {pas_type};")
                pad = self._pad()
                self._emit(f"{pad}{name} := {val};")
                continue
            # Pointer (no initializer)
            if d.startswith("*"):
                name = self._pascalify(d.lstrip("*").strip())
                self._emit_local(f"  {name}: {pas_type};")
                continue
            self._emit_local(f"  {self._pascalify(d)}: {pas_type};")

    @staticmethod
    def _translate_c_string(inner: str) -> str:
        """Translate a C string literal's inner content (without surrounding
        quotes) to a Pascal string expression.

        Handles common C escape sequences:
          \\n  -> LineEnding
          \\t  -> #9
          \\r  -> #13
          \\0  -> #0
          \\"  -> "
          \\'  -> ''
          \\\\ -> \\

        Returns a Pascal string expression like ``'hello'`` or
        ``'hello' + LineEnding + ''``.
        """
        # Order matters: replace longer sequences first
        result = inner
        # Replace escape sequences with Pascal equivalents. We use a
        # placeholder pattern to avoid double-replacement issues.
        # Use the same approach as _translate_expr's string handling.
        result = result.replace('\\n', "' + LineEnding + '")
        result = result.replace('\\t', "' + #9 + '")
        result = result.replace('\\r', "' + #13 + '")
        result = result.replace('\\0', "' + #0 + '")
        result = result.replace('\\"', '"')
        result = result.replace("\\'", "''")
        result = result.replace("\\\\", "\\")
        return f"'{result}'"

    # ------------------------------------------------------------------
    def _try_call_statement(self, s: str) -> bool:
        """If s is a function call statement, translate and emit. Return True if handled.

        Handles:
          * Simple call: ``foo(args)``
          * Function pointer call via struct member: ``ptr->func(args)``,
            ``obj->callback(a, b)`` — the ``->`` is converted to ``^.``
            so the Pascal output is ``ptr^.func(args)``.
        """
        m = re.match(
            r"^(?P<fn>[A-Za-z_]\w*(?:\s*->\s*\w+)*)\s*\((?P<args>.*)\)\s*;?\s*$",
            s, re.DOTALL,
        )
        if not m:
            return False
        fn_raw = m.group("fn")
        # Convert -> to ^. for function pointer calls
        fn = fn_raw.replace("->", "^.")
        args_raw = m.group("args").strip()
        args = self._split_args(args_raw)
        pad = self._pad()
        # Function pointer calls (ptr->func()) are not GTK/libc functions;
        # skip the lookup and emit directly.
        is_func_ptr = "->" in fn_raw
        info = None
        lib = None
        if not is_func_ptr:
            # GTK call?
            info = lookup_gtk_call(fn)
        if info:
            template, kind = info
            if kind == "stmt":
                stmt = self._instantiate_template(template, fn, args)
                if stmt:
                    self._emit(f"{pad}{stmt};")
                else:
                    # empty template -> no-op
                    self._emit(f"{pad}// {fn}() -> no-op in LCL")
                return True
            elif kind == "expr":
                # standalone expression call -> just emit with semicolon
                expr = self._instantiate_template(template, fn, args)
                if expr:
                    self._emit(f"{pad}{expr};")
                else:
                    self._emit(f"{pad}// {fn}() -> no-op")
                return True
        # libc call?
        if not is_func_ptr:
            lib = lookup_c_lib(fn)
        if lib:
            template, kind = lib
            if kind == "stmt":
                stmt = self._instantiate_lib_template(template, args)
                if stmt:
                    self._emit(f"{pad}{stmt};")
                else:
                    self._emit(f"{pad}// {fn}()")
                return True
            elif kind == "expr":
                expr = self._instantiate_lib_template(template, args)
                if expr:
                    self._emit(f"{pad}{expr};")
                return True
        # Application-specific call: just translate args and emit as procedure call
        # with TODO comment if return type is non-void
        if self.func.return_type.strip() in ("void", ""):
            self._emit(f"{pad}{fn}({', '.join(self._translate_expr(a) for a in args)});")
            return True
        else:
            self._emit(f"{pad}// return value of {fn}() ignored")
            self._emit(f"{pad}{fn}({', '.join(self._translate_expr(a) for a in args)});")
            return True

    def _try_assignment(self, s: str) -> bool:
        """Try to translate simple and augmented assignments.

        Handles:
          * Augmented assignments: ``a += b``, ``a -= b``, ``a *= b``,
            ``a /= b``, ``a %= b``, ``a &= b``, ``a |= b``, ``a ^= b``,
            ``a <<= b``, ``a >>= b``
          * Chained assignments: ``a = b = c = 5`` -> ``c := 5; b := 5; a := 5;``
            (Pascal doesn't support chained assignment as an expression,
            so we emit multiple statements)
          * Simple assignment: ``a = b``
          * Augmented assignment with array/struct LHS:
            ``buf[j] += 1``, ``struct.field += 1``
        """
        pad = self._pad()
        # Strip trailing semicolon
        s_clean = s.rstrip(";").rstrip()
        if not s_clean:
            return False

        # ---- Augmented assignments (check FIRST, before plain '=') ----
        # We must check augmented ops before the plain ``=`` regex because
        # the plain ``=`` regex uses ``[^=<>!]+?`` for the LHS, which
        # would fail to match ``a <<= b`` (the ``<`` is excluded from the
        # LHS character class). By checking augmented ops first, we ensure
        # ``<<=``, ``>>=``, ``+=``, etc. are all recognised.
        # The LHS can be: a simple identifier, an array element (a[i]),
        # a struct field (a.b), a pointer deref (a->b -> a^.b in Pascal),
        # or a pointer dereference (*p, *(*p).field, etc.).
        # LVALUE_RE: optional * prefix + identifier + optional member access
        _LVALUE_RE = (
            r"(?:\*\s*)*"  # zero or more * (pointer dereference)
            r"\w+"
            r"(?:\s*\[[^\]]*\]?"
            r"|\s*\.\s*\w+"
            r"|\s*->\s*\w+)*"
        )
        aug = re.match(
            rf"^(?P<l>{_LVALUE_RE})"
            r"\s*(?P<op>\+=|-=|\*=|/=|%=|<<=|>>=|&=|\|=|\^=)"
            r"\s*(?P<r>.+)$",
            s_clean, re.DOTALL,
        )
        if aug:
            l_raw = aug.group("l").strip()
            l = self._translate_expr(l_raw)
            op = aug.group("op")
            r_raw = aug.group("r").strip()
            r = self._translate_expr(r_raw)
            # Special case: pointer arithmetic with += / -=
            # In Pascal, you can't add an integer to a pointer with ``+``.
            # Use ``Inc(ptr, n)`` / ``Dec(ptr, n)`` instead.
            # We detect this heuristically: if the LHS is a simple
            # identifier (likely a pointer) and the op is += or -= with
            # a numeric/sizeof operand, emit Inc/Dec.
            # This is not perfect (we don't know the type) but works
            # for the common pattern ``p += 4;`` -> ``Inc(p, 4);``.
            if op in ("+=", "-=") and re.match(r"^\w+$", l_raw):
                # Simple identifier LHS — could be a pointer.
                # Use Inc/Dec which works for both pointers and integers
                # in Pascal (Inc increments by element size for pointers).
                if r.strip():
                    if op == "+=":
                        self._emit(f"{pad}Inc({l}, {r});")
                    else:
                        self._emit(f"{pad}Dec({l}, {r});")
                else:
                    if op == "+=":
                        self._emit(f"{pad}Inc({l});")
                    else:
                        self._emit(f"{pad}Dec({l});")
                return True
            pas_op = {
                "+=": "+", "-=": "-", "*=": "*", "/=": "/",
                "%=": "mod", "<<=": "shl", ">>=": "shr",
                "&=": "and", "|=": "or", "^=": "xor",
            }[op]
            self._emit(f"{pad}{l} := {l} {pas_op} {r};")
            return True

        # ---- Plain assignment (including chained: a = b = c = 5) ----
        # Match: lhs = rhs   (but not == or <= or >=)
        # The LHS pattern must allow ``->`` (pointer member access) and
        # ``.`` (struct member) and ``[]`` (array index). The old pattern
        # ``[^=<>!]+?`` excluded ``>`` which meant ``ptr->field = value``
        # was never matched (``->`` contains ``>``). We now use a proper
        # lvalue pattern that matches identifiers with optional member
        # access chains.
        # LVALUE_RE: optional * prefix (pointer dereference) + identifier
        #   + optional ([index] | .field | ->field)*
        # Allow optional whitespace around ``->`` and ``.`` so that
        # ``detailData -> cbSize`` (with spaces) also matches.
        # ``*p`` (pointer dereference) is also matched so that
        # ``*p = 0`` becomes ``p^ := 0;``.
        _LVALUE_RE_PLAIN = (
            r"(?:\*\s*)*"  # zero or more * (pointer dereference)
            r"\w+"
            r"(?:\s*\[[^\]]*\]?"
            r"|\s*\.\s*\w+"
            r"|\s*->\s*\w+)*"
        )
        m = re.match(
            rf"^(?P<lhs>{_LVALUE_RE_PLAIN})\s*=\s*(?P<rhs>.+)$",
            s_clean, re.DOTALL,
        )
        if not m:
            return False
        lhs = m.group("lhs").strip()
        rhs = m.group("rhs").strip()

        # Check for chained assignment: rhs itself contains a top-level '='
        # e.g. ``a = b = c = 5`` -> rhs = ``b = c = 5``
        chain = self._split_chained_assignment(s_clean)
        if chain and len(chain) >= 2:
            # chain is a list of (lhs, rhs_value) pairs, innermost first.
            # e.g. ``a = b = c = 5`` -> [("c","5"), ("b","c"), ("a","b")]
            # C semantics: assign right-to-left, so all get the final value.
            final_value = self._translate_expr(chain[0][1])
            stmts = []
            for lhs_name, _ in chain:
                lhs_pas = self._translate_expr(lhs_name)
                stmts.append(f"{lhs_pas} := {final_value}")
            self._emit(f"{pad}{' ; '.join(stmts)};")
            return True

        # Pattern: gtk_xxx_get_*(GTK_XXX(widget)) -> lhs := widget.Prop
        # We delegate to expr translator which handles gtk_* calls.
        lhs_pas = self._translate_expr(lhs)
        rhs_pas = self._translate_expr(rhs)
        self._emit(f"{pad}{lhs_pas} := {rhs_pas};")
        return True

    @staticmethod
    def _split_chained_assignment(s: str) -> list[tuple[str, str]]:
        """Split a chained C assignment into (lhs, rhs_value) pairs.

        C: ``a = b = c = 5`` assigns right-to-left: c=5, b=5, a=5.
        We return pairs innermost-first: [("c","5"), ("b","c"), ("a","b")].
        The caller can then emit ``c := 5; b := 5; a := 5;``.

        Returns an empty list if ``s`` is not a chained assignment (i.e.
        has only one ``=``).
        """
        parts: list[str] = []
        cur: list[str] = []
        depth = 0
        paren_depth = 0
        i = 0
        n = len(s)
        while i < n:
            ch = s[i]
            if ch == "{":
                depth += 1
                cur.append(ch)
            elif ch == "}":
                depth -= 1
                cur.append(ch)
            elif ch == "(":
                paren_depth += 1
                cur.append(ch)
            elif ch == ")":
                if paren_depth > 0:
                    paren_depth -= 1
                cur.append(ch)
            elif ch == "=" and depth == 0 and paren_depth == 0:
                # Check this is a plain ``=`` and not part of ``==``,
                # ``<=``, ``>=``, ``!=``, ``+=``, ``-=``, etc.
                prev = s[i-1] if i > 0 else ""
                nxt = s[i+1] if i+1 < n else ""
                if prev in "<>!+-*/%&|^=":
                    # Part of a multi-char operator — skip
                    cur.append(ch)
                elif nxt == "=":
                    # ``==`` — skip both chars
                    cur.append(ch)
                    i += 1
                    if i < n:
                        cur.append(s[i])
                else:
                    # Plain ``=`` — split here
                    parts.append("".join(cur).strip())
                    cur = []
            else:
                cur.append(ch)
            i += 1
        parts.append("".join(cur).strip())
        # parts is now [a, b, c, 5] for ``a = b = c = 5``
        if len(parts) < 3:
            # Not a chained assignment (need at least 2 '=' signs -> 3 parts)
            return []
        # Build (lhs, rhs_value) pairs innermost-first
        pairs = []
        for i in range(len(parts) - 2, -1, -1):
            pairs.append((parts[i], parts[i + 1]))
        return pairs

    # ------------------------------------------------------------------
    # Expression translator
    # ------------------------------------------------------------------
    def _translate_expr(self, expr: str) -> str:
        expr = expr.strip()
        if not expr:
            return ""
        # Ternary
        m = re.match(r"^(?P<c>.+?)\s*\?\s*(?P<a>[^:]+?)\s*:\s*(?P<b>.+)$",
                     expr, re.DOTALL)
        if m:
            c = self._translate_expr(m.group("c"))
            a = self._translate_expr(m.group("a"))
            b = self._translate_expr(m.group("b"))
            return f"IfThen({c}, {a}, {b})"
        # GTK/libc call expression
        m = re.match(r"^(?P<fn>[A-Za-z_]\w*)\s*\((?P<args>.*)\)$", expr, re.DOTALL)
        if m:
            fn = m.group("fn")
            args_raw = m.group("args")
            args = self._split_args(args_raw)
            info = lookup_gtk_call(fn)
            if info:
                template, kind = info
                return self._instantiate_template(template, fn, args) or "nil"
            lib = lookup_c_lib(fn)
            if lib:
                template, kind = lib
                if kind == "expr":
                    return self._instantiate_lib_template(template, args) or "0"
            # Generic: translate args and rebuild call
            arg_strs = [self._translate_expr(a) for a in args]
            return f"{fn}({', '.join(arg_strs)})"
        # Cast: (type)expr
        m = re.match(r"^\((?P<type>[\w\s\*]+)\)\s*(?P<expr>.+)$", expr)
        if m:
            inner = self._translate_expr(m.group("expr"))
            # We don't strictly need the cast type in Pascal
            return inner
        # NULL -> nil
        if expr in ("NULL", "nullptr"):
            return "nil"
        if expr in ("TRUE", "true"):
            return "True"
        if expr in ("FALSE", "false"):
            return "False"
        # String literal
        if expr.startswith('"') and expr.endswith('"'):
            # C escape sequences
            inner = expr[1:-1]
            inner = inner.replace('\\n', "' + LineEnding + '")
            inner = inner.replace('\\t', "' + #9 + '")
            inner = inner.replace('\\"', '"')
            inner = inner.replace("\\'", "''")
            return f"'{inner}'"
        # Char literal
        if expr.startswith("'") and expr.endswith("'") and len(expr) >= 3:
            return f"#{ord(expr[1])}" if len(expr) == 3 else f"'{expr[1:-1]}'"
        # Numeric: hex 0x..  -> $..
        # Allow optional C suffixes: U, L, UL, LU, ull, ull, etc.
        # (Pascal doesn't need these suffixes — Integer/Int64 is inferred.)
        m = re.match(r"^0[xX]([0-9A-Fa-f]+)[uUlL]*$", expr)
        if m:
            return f"${m.group(1)}"
        # ---- Identifier name remapping ----
        # If the expression is a simple identifier that was remapped
        # (because it's a Pascal reserved word), return the remapped name.
        # e.g. C variable ``type`` -> Pascal ``type_``
        if re.match(r"^\w+$", expr) and expr in self._name_map:
            return self._name_map[expr]
        # ---- Logical operators (&&, ||) — wrap each side in parens ----
        # Pascal's ``and``/``or`` are bitwise by default and have higher
        # precedence than comparison operators (unlike C, where ``&&`` has
        # lower precedence than ``<``, ``==`` etc.). Without parentheses,
        # ``a<b and c<>d`` would parse in Pascal as ``a < (b and c) <> d``,
        # which is wrong. So we always wrap each operand of &&/|| in parens.
        # Also note: we must split on && / || at top level (depth 0) to
        # avoid breaking expressions inside parentheses, function calls,
        # or array subscripts.
        logical = self._split_logical(expr)
        if logical:
            op, parts = logical
            pas_op = " and " if op == "&&" else " or "
            pas_parts = [self._translate_expr(p) for p in parts]
            # Wrap each part in parens (but skip if already wrapped or
            # is a simple identifier/constant)
            wrapped = []
            for p in pas_parts:
                p = p.strip()
                if (p.startswith("(") and p.endswith(")") and
                        self._is_balanced(p[1:-1])):
                    wrapped.append(p)
                else:
                    wrapped.append(f"({p})")
            return pas_op.join(wrapped)
        # ---- Address-of operator: &var -> @var ----
        # C: ``&a`` means "address of a".
        # Pascal: ``@a`` means the same thing.
        # We translate a leading ``&`` (unary address-of, NOT bitwise AND)
        # to ``@``. The operand is an identifier (with optional -> / . /
        # [] access) or a parenthesised sub-expression.
        # This must run BEFORE _OP_REPLACE which converts ``&`` to ``and``.
        m_addr = re.match(r"^&(?!&)", expr)  # single &, not &&
        if m_addr:
            rest = expr[1:].lstrip()
            operand, trailing = self._extract_deref_operand(rest)
            if operand:
                operand_pas = self._translate_expr(operand)
                result = f"@( {operand_pas} )"
                if trailing:
                    trailing_pas = self._translate_expr(trailing)
                    result = f"{result} {trailing_pas}"
                return result
        # ---- Pointer dereference: *ptr -> ptr^ ----
        # C: ``*p`` means "the value pointed to by p".
        # Pascal: ``p^`` means the same thing.
        # We translate a leading ``*`` (unary dereference, NOT multiplication)
        # by extracting the dereferenced operand and appending ``^``.
        #
        # The operand of ``*`` is one of:
        #   * an identifier (possibly with -> / . member access):
        #       ``*p``      -> ``p^``
        #       ``*ptr->f`` -> ``ptr^.f^``  (actually ``ptr^.f`` then ``^``)
        #   * a parenthesised sub-expression:
        #       ``*(p+1)``  -> ``(p+1)^``
        #       ``*(*pp)``  -> ``(pp^^)``  (recursive)
        #
        # After the operand, there may be a trailing operator + expression:
        #   ``*p + 1``   -> ``(p^) + 1``
        #   ``*p * 2``   -> ``(p^) * 2``
        #
        # We must be careful: ``*`` in the middle of an expression
        # (``a * b``) is multiplication and is NOT handled here — this
        # branch only fires when ``*`` is the very first character.
        m_deref = re.match(r"^\*(?!\*)", expr)  # single *, not **
        if m_deref:
            rest = expr[1:].lstrip()
            # Extract the operand: identifier (+ optional -> / . / [] access),
            # or a parenthesised sub-expression.
            operand, trailing = self._extract_deref_operand(rest)
            if operand:
                operand_pas = self._translate_expr(operand)
                # Wrap in parens before appending ``^`` so that
                # ``*p + 1`` becomes ``(p^) + 1`` (correct precedence).
                result = f"({operand_pas}^)"
                if trailing:
                    # Translate the trailing part. The trailing starts
                    # with a binary operator (e.g. ``+ 1``, ``* 2``,
                    # ``+ *q``). We extract the binary operator, then
                    # recursively translate the rest — so that ``+ *q``
                    # correctly treats ``*q`` as a new dereference.
                    trailing_pas = self._translate_trailing(trailing)
                    result = f"{result} {trailing_pas}"
                return result
        # Multiple dereference: **pp
        m_deref2 = re.match(r"^\*\*(?!\*)", expr)
        if m_deref2:
            rest = expr[2:].lstrip()
            operand, trailing = self._extract_deref_operand(rest)
            if operand:
                operand_pas = self._translate_expr(operand)
                result = f"({operand_pas}^^)"
                if trailing:
                    trailing_pas = self._translate_trailing(trailing)
                    result = f"{result} {trailing_pas}"
                return result
        # Operators
        result = expr
        # ---- Convert hex literals (0x...) -> $... throughout the expression ----
        # This must happen BEFORE operator replacement because ``&`` etc.
        # might appear adjacent to hex literals.
        # Match: 0x followed by hex digits, with optional U/L suffixes.
        # Don't match inside string literals (single/double quoted).
        result = self._convert_hex_literals(result)
        for old, new in self._OP_REPLACE:
            result = result.replace(old, new)
        # Collapse multiple spaces (introduced by operator replacement
        # when the source already had spaces around operators) into one.
        # But preserve spaces inside string literals — we do a simple
        # pass that only collapses runs of 2+ spaces outside quotes.
        result = self._collapse_spaces(result)
        # Pre/post increment/decrement: convert to function-call style.
        # NOTE: Pascal has no post-increment expression; using Inc/Dec as
        # inline expressions is technically wrong but compiles (Inc returns
        # nothing in Pascal; this is a known TODO the user must fix).
        # We translate `x++` to `Inc(x)` so it at least surfaces as a
        # procedure call that the user can spot and rewrite.
        result = re.sub(r"\+\+(\w+)", r"Inc(\1)", result)
        result = re.sub(r"--(\w+)", r"Dec(\1)", result)
        result = re.sub(r"(\w+)\+\+", r"Inc(\1)", result)
        result = re.sub(r"(\w+)--", r"Dec(\1)", result)
        # ---- Apply identifier name remapping ----
        # Replace any C identifier that was remapped (because it's a Pascal
        # reserved word) with its Pascal-safe equivalent.
        # e.g. ``type`` -> ``type_``, ``file`` -> ``file_``
        # We use word boundaries so we don't replace substrings.
        # Skip string literals (single/double quoted).
        if self._name_map:
            result = self._apply_name_map(result)
        return result

    def _apply_name_map(self, s: str) -> str:
        """Replace C identifiers with their Pascal-safe equivalents,
        skipping string literals."""
        # Build a regex that matches any of the remapped names as whole words
        names = sorted(self._name_map.keys(), key=len, reverse=True)
        if not names:
            return s
        pattern = re.compile(
            r"\b(" + "|".join(re.escape(n) for n in names) + r")\b"
        )
        out: list[str] = []
        i = 0
        n = len(s)
        in_str = False
        str_char = ""
        while i < n:
            ch = s[i]
            if in_str:
                out.append(ch)
                if ch == str_char and (i == 0 or s[i-1] != "\\"):
                    in_str = False
                i += 1
                continue
            if ch in ('"', "'"):
                in_str = True
                str_char = ch
                out.append(ch)
                i += 1
                continue
            # Try to match a remapped name at this position
            m = pattern.match(s, i)
            if m:
                out.append(self._name_map[m.group(1)])
                i = m.end()
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    @staticmethod
    def _convert_hex_literals(s: str) -> str:
        """Convert all C hex literals (``0x...``) in ``s`` to Pascal (``$...``).

        Handles optional C integer suffixes (U, L, UL, LL, ULL, etc.)
        by stripping them — Pascal infers the integer type automatically.

        Skips hex literals inside string/char literals (``"..."``, ``'...'``).
        """
        # We walk through the string, tracking whether we're inside a
        # string literal, and replace 0x... outside of strings.
        out: list[str] = []
        i = 0
        n = len(s)
        in_str = False
        str_char = ""
        while i < n:
            ch = s[i]
            if in_str:
                out.append(ch)
                if ch == str_char and (i == 0 or s[i-1] != "\\"):
                    in_str = False
                i += 1
                continue
            if ch in ('"', "'"):
                in_str = True
                str_char = ch
                out.append(ch)
                i += 1
                continue
            # Check for 0x or 0X
            if ch == "0" and i + 1 < n and s[i+1] in "xX":
                # Find the end of the hex literal
                j = i + 2
                while j < n and s[j] in "0123456789abcdefABCDEF":
                    j += 1
                # Check for C integer suffixes: U, L, UL, LU, LL, ULL, etc.
                while j < n and s[j] in "uUlL":
                    j += 1
                # Extract the hex digits (without the 0x prefix and suffix)
                hex_digits = s[i+2:j]
                # Strip trailing suffix chars
                hex_digits = re.sub(r"[uUlL]+$", "", hex_digits)
                if hex_digits:
                    out.append("$" + hex_digits)
                else:
                    out.append(s[i:j])  # fallback
                i = j
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    @staticmethod
    def _collapse_spaces(s: str) -> str:
        """Collapse runs of 2+ spaces into a single space, preserving
        spaces inside string literals (single and double quoted)."""
        out: list[str] = []
        i = 0
        n = len(s)
        in_str = False
        str_char = ""
        while i < n:
            ch = s[i]
            if in_str:
                out.append(ch)
                if ch == str_char and (i == 0 or s[i-1] != "\\"):
                    in_str = False
                i += 1
                continue
            if ch in ("'", '"'):
                in_str = True
                str_char = ch
                out.append(ch)
                i += 1
                continue
            if ch == " " and i + 1 < n and s[i+1] == " ":
                # Skip run of spaces
                while i < n and s[i] == " ":
                    i += 1
                out.append(" ")
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    def _translate_cond(self, expr: str) -> str:
        """Translate a C condition (used in if/while/do-while) to a Pascal
        boolean expression.

        C conditions can be:
          * Comparison expressions: ``a < b``, ``a == b``, ``a != b`` etc.
            These translate directly (Pascal ``<``, ``=``, ``<>`` etc.).
          * Logical combinations: ``a && b``, ``a || b``, ``!a``
            These translate to Pascal ``and``/``or``/``not`` (handled by
            ``_split_logical`` which wraps operands in parens).
          * Bitwise operations used as boolean: ``a & b``, ``a | b``
            In C, ``if(a & b)`` means ``if((a & b) != 0)``. Pascal requires
            a boolean, so we must add ``<> 0`` to convert the integer
            bitwise result to a boolean.
          * Plain integer/pointer used as boolean: ``if(count)``, ``if(ptr)``
            In C, ``if(count)`` means ``if(count != 0)``. We add ``<> 0``.

        Strategy:
          1. Check if the C expression contains a top-level comparison
             operator (``<``, ``>``, ``==``, ``!=``, ``<=``, ``>=``). If
             so, the result is already boolean — translate and return.
          2. Check if the C expression is a logical combination (``&&``,
             ``||``). If so, ``_split_logical`` handles it and the result
             is boolean — translate and return.
          3. Check if the C expression starts with ``!`` (logical NOT).
             The result is boolean — translate and return.
          4. Otherwise, the expression is an integer (bitwise op result,
             plain variable, function call returning int). Translate and
             add ``<> 0`` to convert to boolean.

        The key distinction from a naive approach: we check the **C**
        expression for ``&&``/``||``/``!``/comparisons BEFORE translation,
        because after translation both ``&`` and ``&&`` become ``and``,
        making them indistinguishable.
        """
        if not expr or not expr.strip():
            return "True"
        c_expr = expr.strip()
        # Check for top-level comparison operators in the C expression.
        # We need to respect parentheses so that ``f(a < b)`` is not
        # treated as a comparison condition.
        has_comparison = self._has_top_level_comparison(c_expr)
        # Check for top-level logical operators (&& ||)
        has_logical = self._has_top_level_op(c_expr, ("&&", "||"))
        # Check for leading ``!`` (logical NOT) — but not ``!=``
        # Strip leading whitespace and check
        stripped = c_expr.lstrip()
        starts_with_not = stripped.startswith("!") and not stripped.startswith("!=")
        # Check for boolean constants
        is_bool_const = c_expr in ("TRUE", "true", "FALSE", "false")
        if has_comparison or has_logical or starts_with_not or is_bool_const:
            return self._translate_expr(c_expr)
        # Translate the expression. If it's a single function call, we
        # assume it returns boolean (common in GTK code). Otherwise, it's
        # an integer expression and we add ``<> 0``.
        pas = self._translate_expr(c_expr).strip()
        # Check for function call pattern: name(args)
        import re as _re
        is_func_call = bool(_re.match(r'^[A-Za-z_]\w*\s*\(.*\)$', pas))
        if is_func_call:
            return pas
        # Integer expression — add ``<> 0``
        if pas.startswith("(") and pas.endswith(")"):
            return f"{pas} <> 0"
        return f"({pas}) <> 0"

    @staticmethod
    def _has_top_level_comparison(expr: str) -> bool:
        """Return True if ``expr`` contains a top-level comparison operator
        (``<``, ``>``, ``==``, ``!=``, ``<=``, ``>=``) outside of any
        parentheses."""
        depth = 0
        i = 0
        n = len(expr)
        while i < n:
            ch = expr[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif depth == 0:
                # Check for two-char operators first
                two = expr[i:i+2]
                if two in ("==", "!=", "<=", ">="):
                    return True
                # Single-char < or > (but not << or >>)
                if ch in "<>" and i + 1 < n and expr[i+1] not in "<>":
                    return True
                if ch in "<>" and i + 1 >= n:
                    return True
            i += 1
        return False

    @staticmethod
    def _has_top_level_op(expr: str, ops: tuple) -> bool:
        """Return True if ``expr`` contains any of the given two-char
        operators (e.g. ``("&&", "||")``) at the top level (outside of
        any parentheses)."""
        depth = 0
        i = 0
        n = len(expr)
        while i < n:
            ch = expr[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif depth == 0 and expr[i:i+2] in ops:
                return True
            i += 1
        return False

    def _translate_trailing(self, trailing: str) -> str:
        """Translate the trailing part of a dereference expression.

        ``trailing`` is what follows the dereferenced operand, e.g.:
          ``+ 1``     for ``*p + 1``
          ``* 2``     for ``*p * 2``
          ``+ *q``    for ``*p + *q``

        The trailing always starts with a binary operator. We extract
        the operator, then recursively translate the rest (which may
        contain another dereference like ``*q``).

        Binary operators we recognise: ``+``, ``-``, ``*``, ``/``,
        ``%``, ``<<``, ``>>``, ``==``, ``!=``, ``<``, ``>``, ``<=``,
        ``>=``, ``&&``, ``||``, ``&``, ``|``, ``^``, ``,``, ``?``
        (ternary).
        """
        trailing = trailing.strip()
        if not trailing:
            return ""
        # Extract the binary operator at the start
        # Two-char operators first, then one-char
        m_op = re.match(
            r"^(\+\+|--|<<|>>|==|!=|<=|>=|&&|\|\||[-+*/%<>&|^,?])",
            trailing,
        )
        if not m_op:
            # No operator found — just translate the whole thing
            return self._translate_expr(trailing)
        op = m_op.group(1)
        rest = trailing[m_op.end():].strip()
        # Translate the operator to Pascal
        op_pas = {
            "<<": "shl", ">>": "shr", "==": "=", "!=": "<>",
            "&&": "and", "||": "or", "&": "and", "|": "or", "^": "xor",
            "%": "mod",
        }.get(op, op)  # default: keep as-is (+, -, *, /, <, >, <=, >=, etc.)
        # Recursively translate the rest (may contain more dereferences)
        if rest:
            rest_pas = self._translate_expr(rest)
            return f"{op_pas} {rest_pas}"
        return op_pas

    def _extract_deref_operand(self, expr: str) -> tuple[str, str]:
        """Extract the operand of a unary ``*`` or ``&`` operator.

        Given an expression like ``p + 1`` (after the ``*`` has been
        consumed), return ``(operand, trailing)`` where:
          * ``operand`` is the dereferenced/addressed expression:
              - ``p``         for ``p + 1``      (operand = "p", trailing = "+ 1")
              - ``p->f``      for ``p->f + 1``   (operand = "p->f", ...)
              - ``(p+1)``     for ``(p+1)``       (operand = "(p+1)", trailing = "")
              - ``arr[i]``    for ``arr[i]``      (operand = "arr[i]", trailing = "")
          * ``trailing`` is whatever follows the operand (operators +
            more expression), or "" if nothing follows.

        The operand is one of:
          * A parenthesised sub-expression: ``(...)``
          * An identifier followed by optional ``->`` / ``.`` / ``[]``
            member accesses, up to the first binary operator
            (``+``, ``-``, ``*``, ``/``, ``<``, ``>``, ``==``, etc.)
        """
        expr = expr.strip()
        if not expr:
            return ("", "")
        # Case 1: parenthesised sub-expression
        if expr.startswith("("):
            depth = 0
            for i, ch in enumerate(expr):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        operand = expr[:i+1]
                        trailing = expr[i+1:].strip()
                        return (operand, trailing)
            # Unbalanced - take everything
            return (expr, "")
        # Case 2: identifier + optional member accesses
        # Match: identifier, then any sequence of:
        #   ->field, .field, [index], (function call args)
        # Stop at binary operators (+, -, *, /, <, >, ==, !=, <=, >=, &&, ||, etc.)
        i = 0
        n = len(expr)
        # First, match the initial identifier
        while i < n and (expr[i].isalnum() or expr[i] == "_"):
            i += 1
        if i == 0:
            # No identifier found — return empty
            return ("", expr)
        # Now consume member access chains: ->, ., [], ()
        while i < n:
            # Skip whitespace
            j = i
            while j < n and expr[j] in " \t":
                j += 1
            if j >= n:
                break
            ch = expr[j]
            if ch == "-" and j + 1 < n and expr[j+1] == ">":
                # -> member access
                i = j + 2
                while i < n and (expr[i].isalnum() or expr[i] == "_"):
                    i += 1
            elif ch == ".":
                # . member access
                i = j + 1
                while i < n and (expr[i].isalnum() or expr[i] == "_"):
                    i += 1
            elif ch == "[":
                # Array index — find matching ]
                depth = 1
                i = j + 1
                while i < n and depth > 0:
                    if expr[i] == "[":
                        depth += 1
                    elif expr[i] == "]":
                        depth -= 1
                    i += 1
            elif ch == "(":
                # Function call — find matching )
                depth = 1
                i = j + 1
                while i < n and depth > 0:
                    if expr[i] == "(":
                        depth += 1
                    elif expr[i] == ")":
                        depth -= 1
                    i += 1
            else:
                # Binary operator or end — stop
                break
        operand = expr[:i].strip()
        trailing = expr[i:].strip()
        return (operand, trailing)

    def _split_logical(self, expr: str):
        """Split a top-level ``&&`` / ``||`` chain into operands.

        Returns ``(op, [parts])`` if the expression contains a top-level
        logical operator, or ``None`` if it does not. The split respects
        paren/bracket nesting so that ``f(a && b)`` is not split.

        The returned operator is the *lowest-precedence* logical operator
        found at top level (``||`` wins over ``&&``), mirroring C
        precedence so that ``a || b && c`` is split on ``||`` first and
        ``b && c`` is recursed into on the next call.
        """
        depth = 0
        # First pass: find top-level || (lowest precedence in C)
        # If found, split there. Otherwise split on &&.
        for target_op in ("||", "&&"):
            parts: list[str] = []
            cur: list[str] = []
            i = 0
            found = False
            while i < len(expr):
                ch = expr[i]
                if ch in "([{":
                    depth += 1
                    cur.append(ch)
                    i += 1
                    continue
                if ch in ")]}":
                    depth -= 1
                    cur.append(ch)
                    i += 1
                    continue
                if depth == 0 and expr[i:i+2] == target_op:
                    parts.append("".join(cur))
                    cur = []
                    i += 2
                    found = True
                    continue
                cur.append(ch)
                i += 1
            if found:
                parts.append("".join(cur))
                return (target_op, parts)
        return None

    @staticmethod
    def _is_balanced(s: str) -> bool:
        """Return True if parens/brackets in ``s`` are balanced and the
        outermost pair wraps the entire string (i.e. removing them leaves
        a still-balanced inner string)."""
        depth = 0
        for i, ch in enumerate(s):
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth == 0 and i != len(s) - 1:
                    # Closed before end - not fully wrapped
                    return False
        return depth == 0

    # ------------------------------------------------------------------
    def _split_args(self, args_raw: str) -> list[str]:
        if not args_raw.strip():
            return []
        return [a.strip() for a in _split_top_level(args_raw)]

    # ------------------------------------------------------------------
    def _instantiate_template(self, template: str, fn: str,
                              args: list[str]) -> str:
        """Instantiate a GTK_CALL_MAP template using positional args.

        Templates use placeholders:
          {w}     - first arg (usually the widget)
          {v}     - second arg (value)
          {t}     - string arg
          {b}     - boolean arg
          {i}     - integer arg
          {name}  - the string id passed to gtk_builder_get_object
          {p}     - path arg
          {e}     - editable
          {lo},{hi} - low/high of range
          {step}  - increment step
          {d}     - digits
          {f}     - fraction
          {m}     - mode
        Generic {0}, {1}, ... also accepted.
        Also: G_CALLBACK(handler) is recognised in g_signal_connect args.
        """
        if not template:
            return ""
        # Translate each arg to Pascal
        pas_args = [self._translate_expr(a) for a in args]
        # gtk_builder_get_object(builder, "NAME") -> "NAME" becomes the component
        if fn == "gtk_builder_get_object" and len(pas_args) >= 2:
            # The second arg is a string literal "NAME"; strip quotes
            name = args[1].strip().strip('"')
            return name  # treat as a form field reference
        # g_signal_connect: drop entirely
        if fn in ("g_signal_connect", "g_signal_connect_swapped",
                  "g_signal_handlers_disconnect_by_func",
                  "gtk_builder_connect_signals"):
            return ""
        # Range: gtk_spin_button_set_range(w, lo, hi)
        if fn == "gtk_spin_button_set_range" and len(pas_args) >= 3:
            return template.format(w=pas_args[0], lo=pas_args[1], hi=pas_args[2])
        if fn == "gtk_spin_button_set_increments" and len(pas_args) >= 3:
            return template.format(w=pas_args[0], step=pas_args[1])
        if fn == "gtk_scale_set_digits" and len(pas_args) >= 2:
            return template.format(w=pas_args[0], d=pas_args[1])
        if fn == "gtk_progress_bar_set_fraction" and len(pas_args) >= 2:
            return template.format(w=pas_args[0], f=pas_args[1])
        if fn == "gtk_tree_selection_set_mode" and len(pas_args) >= 2:
            return template.format(w=pas_args[0], m=pas_args[1])
        if fn == "gtk_widget_set_tooltip_text" and len(pas_args) >= 2:
            return template.format(w=pas_args[0], t=pas_args[1])
        if fn == "gtk_widget_set_sensitive" and len(pas_args) >= 2:
            return template.format(w=pas_args[0], b=pas_args[1])
        if fn == "gtk_widget_set_visible" and len(pas_args) >= 2:
            return template.format(w=pas_args[0], b=pas_args[1])
        if fn == "gtk_window_set_default_size" and len(pas_args) >= 3:
            return template.format(w=pas_args[0], w_=pas_args[1], h_=pas_args[2])
        if fn == "gtk_window_resize" and len(pas_args) >= 4:
            return template.format(w=pas_args[0], x=pas_args[1],
                                   y=pas_args[2], w_=pas_args[3], h_=pas_args[4])
        if fn == "gtk_button_set_image" and len(pas_args) >= 2:
            return template.format(w=pas_args[0], p=pas_args[1])
        if fn == "gtk_notebook_append_page" and len(pas_args) >= 3:
            return template.format(w=pas_args[0], p=pas_args[1], t=pas_args[2])
        if fn == "gtk_combo_box_text_append" and len(pas_args) >= 3:
            return template.format(w=pas_args[0], t=pas_args[2])
        # Default: substitute the most common placeholders
        subs = {
            "w": pas_args[0] if len(pas_args) >= 1 else "nil",
            "v": pas_args[1] if len(pas_args) >= 2 else "0",
            "t": pas_args[1] if len(pas_args) >= 2 else "''",
            "b": pas_args[1] if len(pas_args) >= 2 else "True",
            "i": pas_args[1] if len(pas_args) >= 2 else "0",
            "n": pas_args[1] if len(pas_args) >= 2 else "0",
            "e": pas_args[1] if len(pas_args) >= 2 else "True",
            "p": pas_args[1] if len(pas_args) >= 2 else "''",
            "f": pas_args[1] if len(pas_args) >= 2 else "0.0",
        }
        try:
            return template.format(**subs)
        except (KeyError, IndexError):
            return template

    def _instantiate_lib_template(self, template: str, args: list[str]) -> str:
        """Instantiate a C_LIB_MAP template.

        Templates use named placeholders {dst}, {src}, {n}, {s}, {a}, {b},
        {ch}, {needle}, {p}, {val}, {name}, {f}, {buf}, {pos}, {c}.
        """
        if not template:
            return ""
        pas_args = [self._translate_expr(a) for a in args]
        # Map by name based on common conventions
        named = {}
        for i, a in enumerate(pas_args):
            named[str(i)] = a
        # Heuristic name mapping
        if len(pas_args) >= 1: named["s"] = pas_args[0]
        if len(pas_args) >= 2: named["dst"] = pas_args[0]; named["src"] = pas_args[1]
        if len(pas_args) >= 3: named["n"] = pas_args[2]
        named.setdefault("a", pas_args[0] if pas_args else "''")
        named.setdefault("b", pas_args[1] if len(pas_args) >= 2 else "''")
        named.setdefault("ch", pas_args[0] if pas_args else "''")
        named.setdefault("needle", pas_args[0] if pas_args else "''")
        named.setdefault("p", pas_args[0] if pas_args else "nil")
        named.setdefault("val", pas_args[1] if len(pas_args) >= 2 else "0")
        named.setdefault("name", pas_args[1] if len(pas_args) >= 2 else "''")
        named.setdefault("f", pas_args[0] if pas_args else "")
        named.setdefault("buf", pas_args[1] if len(pas_args) >= 2 else "nil")
        named.setdefault("pos", pas_args[1] if len(pas_args) >= 2 else "0")
        named.setdefault("c", pas_args[0] if pas_args else "''")
        try:
            return template.format(**named)
        except (KeyError, IndexError):
            return template

    # ------------------------------------------------------------------
    def _extract_paren(self, s: str, open_idx: int) -> tuple[str, str]:
        """Return (inner, rest_after_closing_paren)."""
        depth = 0
        i = open_idx
        while i < len(s):
            if s[i] == "(":
                depth += 1
            elif s[i] == ")":
                depth -= 1
                if depth == 0:
                    return s[open_idx + 1: i], s[i + 1:]
            i += 1
        return s[open_idx + 1:], ""

    def _extract_block(self, s: str) -> tuple[str, str]:
        """If s starts with '{', return (block_inner, rest)."""
        s = s.lstrip()
        if not s.startswith("{"):
            return "", s
        depth = 0
        i = 0
        while i < len(s):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    return s[1:i], s[i + 1:]
            i += 1
        return s[1:], ""


# ---------------------------------------------------------------------------
# Pascal unit emitter for a C source file
# ---------------------------------------------------------------------------
class CFileTranslator:
    """Translate one .c file into one .pas unit.

    Two-pass hybrid:
      1. Try pycparser (AST-based) for accurate function signatures,
         typedefs, and globals. If it succeeds, use those exact
         declarations and only fall back to the pattern-based
         ``BodyTranslator`` for the function bodies.
      2. If pycparser fails (missing dependency, GNU extension, etc.),
         fall back entirely to the pattern-based
         ``extract_functions`` + ``BodyTranslator`` pipeline.
    """

    def __init__(self, c_path: str, unit_name: str,
                 form_unit: Optional[str] = None,
                 included_units: list[str] = None,
                 fake_include_dir: str = "/tmp/fake_libc_include",
                 use_ast: bool = True,
                 header_types: list[str] = None,
                 header_defines: list[tuple[str, str]] = None,
                 header_externs: list[tuple[str, str]] = None):
        self.c_path = c_path
        self.unit_name = unit_name
        self.form_unit = form_unit  # name of the unit that owns form fields
        self.included_units = list(included_units or [])
        self.fake_include_dir = fake_include_dir
        self.use_ast = use_ast
        # Types/defines/externs extracted from the matching .h file (if any).
        # When a .c file and its .h file share the same basename (e.g.
        # coff.c + coff.h), the .c file's output would overwrite the .h
        # file's output. To preserve the header's type declarations, we
        # merge them into the .c file's output.
        self.header_types = list(header_types or [])
        self.header_defines = list(header_defines or [])
        self.header_externs = list(header_externs or [])
        self.stats = {
            "functions": 0, "globals": 0, "translated_stmts": 0,
            "todo_stmts": 0, "lines_in": 0, "lines_out": 0,
            "ast_used": False,
        }
        # Collected TodoItems (as plain dicts) across all functions in
        # this file. Using dicts instead of dataclasses so JSON
        # serialisation works uniformly under PyInstaller.
        self.todos: list[dict] = []
        # Number of lines emitted before the function bodies begin.
        # Updated as the header is built; used to compute absolute output
        # line numbers for each TODO.
        self._header_line_count = 0
        # AST analysis result (None if pycparser not used or failed)
        self._ast_result = None

    # ------------------------------------------------------------------
    def translate(self) -> str:
        with open(self.c_path, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
        self.stats["lines_in"] = src.count("\n") + 1
        src_clean = strip_comments(src)
        src_clean, directives = strip_preprocessor(src_clean)

        # ---- Try AST-based analysis first ----
        if self.use_ast:
            try:
                from ast_translator import parse_with_ast
                self._ast_result = parse_with_ast(
                    self.c_path, self.unit_name,
                    fake_include_dir=self.fake_include_dir,
                    form_unit=self.form_unit,
                    included_units=self.included_units,
                )
                if self._ast_result is not None:
                    self.stats["ast_used"] = True
            except Exception:
                self._ast_result = None

        # ---- Pattern-based fallback (always run for body translation) ----
        funcs, residual = extract_functions(src_clean)
        globals_list = extract_globals(residual)

        out: list[str] = []
        out.append(f"unit {self.unit_name};")
        out.append("")
        out.append("{$mode objfpc}{$H+}")
        out.append("")
        out.append("interface")
        out.append("")
        out.append("uses")
        base_units = ["Classes", "SysUtils", "Forms", "Controls",
                      "Graphics", "Dialogs", "StdCtrls", "ComCtrls",
                      "ExtCtrls", "Buttons", "Spin", "Menus", "LCLType"]
        unit_list = base_units + self.included_units
        if self.form_unit and self.form_unit != self.unit_name \
                and self.form_unit not in unit_list:
            unit_list.append(self.form_unit)
        for i, u in enumerate(unit_list):
            sep = "," if i < len(unit_list) - 1 else ";"
            out.append(f"  {u}{sep}")
        out.append("")
        # Constants from #define
        defines = self._extract_defines(directives)
        if defines:
            out.append("const")
            for name, val in defines:
                out.append(f"  {name} = {val};")
            out.append("")
        # Constants from the matching .h file (if any)
        if self.header_defines:
            out.append("const")
            for name, val in self.header_defines:
                out.append(f"  {name} = {val};")
            out.append("")
        # Types
        out.append("type")
        out.append("  DWORD = Cardinal;")
        out.append("  WORD  = Word;")
        out.append("  BYTE  = Byte;")

        # Types from the matching .h file (if any).
        # When a .c file shares its basename with a .h file (e.g. coff.c +
        # coff.h), the .c output overwrites the .h output. We merge the
        # header's struct/enum/typedef declarations here so they survive.
        if self.header_types:
            for t in self.header_types:
                out.append(t)
            out.append("")
        else:
            out.append("")

        # AST-derived typedefs (more accurate than pattern-based)
        if self._ast_result is not None and self._ast_result.typedefs:
            out.append("type")
            for td in self._ast_result.typedefs:
                out.append(td.pascal_decl)
            out.append("")

        # Globals: prefer AST-derived ones (more accurate types), fall
        # back to pattern-extracted ones.
        if self._ast_result is not None and self._ast_result.globals:
            out.append("var")
            seen_names: set[str] = set()
            for g in self._ast_result.globals:
                if g.name in seen_names:
                    continue
                seen_names.add(g.name)
                out.append(f"  {g.name}: {g.pascal_type};")
            out.append("")
            self.stats["globals"] = len(seen_names)
        elif globals_list:
            out.append("var")
            seen_names: set[str] = set()
            for g in globals_list:
                pas_type = c_type_to_pascal(g.type)
                for name_init in g.names:
                    name, init = self._split_name_init(name_init)
                    if not name:
                        continue
                    base_name = re.match(r"(\w+)", name)
                    if not base_name:
                        continue
                    base = base_name.group(1)
                    if base in seen_names:
                        continue
                    seen_names.add(base)
                    if "[" in name:
                        m = re.match(r"(\w+)\s*\[([^\]]*)\]", name)
                        if m:
                            n = pascalify_name(m.group(1))
                            sz = m.group(2).strip()
                            if sz:
                                out.append(f"  {n}: array[0..{sz}-1] of {pas_type};")
                            else:
                                out.append(f"  {n}: array of {pas_type};")
                            continue
                    out.append(f"  {pascalify_name(name)}: {pas_type};")
            out.append("")
            self.stats["globals"] = len(seen_names)

        # Function prototypes: prefer AST-derived signatures
        out.append("// --- function declarations ---")
        if self._ast_result is not None and self._ast_result.functions:
            for fn in self._ast_result.functions:
                sig = self._ast_result._pascal_signature(fn, prototype=True)
                if sig:
                    out.append(sig + ";")
            out.append("")
            self.stats["functions"] = len(self._ast_result.functions)
        else:
            for fn in funcs:
                sig = self._pascal_signature(fn, prototype=True)
                if sig:
                    out.append(sig + ";")
            out.append("")
            self.stats["functions"] = len(funcs)
        out.append("implementation")
        out.append("")
        # Function bodies
        if self._ast_result is not None and self._ast_result.functions:
            # Use AST function list with pattern-based bodies (Stage 1)
            pattern_funcs_by_name = {fn.name: fn for fn in funcs}
            for ast_fn in self._ast_result.functions:
                if ast_fn.body_node is None:
                    continue  # prototype only
                sig = self._ast_result._pascal_signature(ast_fn, prototype=False)
                if not sig:
                    continue
                out.append(sig + ";")
                pat_fn = pattern_funcs_by_name.get(ast_fn.name)
                body_translated: list[str] = []
                local_decls: list[str] = []
                # Use AST-based body translator (Stage 2) since we have
                # the AST function with its body_node. This is more
                # accurate than the pattern-based BodyTranslator.
                try:
                    from ast_translator import translate_body_with_ast
                    body_offset = self._header_line_count + len(out) + 1
                    ast_result = translate_body_with_ast(
                        ast_fn, source_file=self.c_path, indent=2,
                    )
                    body_translated = ast_result["lines"]
                    local_decls = ast_result["locals"]
                    # Patch output_line in each todo with the correct offset
                    for todo in ast_result["todos"]:
                        todo["output_line"] += body_offset
                    self.stats["translated_stmts"] += \
                        ast_result["stats"]["translated_count"]
                    self.stats["todo_stmts"] += \
                        ast_result["stats"]["todo_count"]
                    self.todos.extend(ast_result["todos"])
                except Exception:
                    # Fall back to pattern-based body if AST translation
                    # fails for any reason.
                    if pat_fn:
                        body_offset = self._header_line_count + len(out) + 1
                        translator = BodyTranslator(pat_fn, indent=2,
                                                     source_file=self.c_path)
                        translator._pas_line = body_offset
                        body_translated = translator.translate()
                        local_decls = translator.locals
                        self.stats["translated_stmts"] += translator.translated_count
                        self.stats["todo_stmts"] += translator.todo_count
                        self.todos.extend(translator.todos)
                # Collect labels from whichever translator was used.
                # AST path stores labels in ast_result["labels"];
                # pattern path stores them in translator._labels.
                func_labels = []
                if 'ast_result' in dir() and ast_result and "labels" in ast_result:
                    func_labels = ast_result.get("labels", [])
                elif hasattr(translator, '_labels'):
                    func_labels = translator._labels
                # Emit label block first (if any), then var block.
                # Pascal requires: label ...; var ...; begin ... end;
                if func_labels:
                    out.append("label")
                    for i, lbl in enumerate(func_labels):
                        sep = "," if i < len(func_labels) - 1 else ";"
                        out.append(f"  {lbl}{sep}")
                if local_decls:
                    out.append("var")
                    out.extend(local_decls)
                else:
                    out.append("var")
                    out.append("  // (no local variables detected)")
                out.append("begin")
                out.extend(body_translated)
                if not body_translated:
                    out.append("  // empty body")
                out.append("end;")
                out.append("")
        else:
            # Pattern-based bodies
            for fn in funcs:
                body_lines = self._translate_function(fn)
                out.extend(body_lines)
                out.append("")
        out.append("end.")
        text = "\n".join(out) + "\n"
        self.stats["lines_out"] = text.count("\n") + 1
        return text

    # ------------------------------------------------------------------
    def _extract_defines(self, directives: list[str]) -> list[tuple[str, str]]:
        """Convert #define lines into Pascal constants where possible."""
        out: list[tuple[str, str]] = []
        seen = set()
        for d in directives:
            m = re.match(r"#\s*define\s+(\w+)\s+(.+)", d)
            if not m:
                continue
            name = m.group(1)
            val = m.group(2).strip()
            # Skip macro-style defines (with parameters or multi-line continuations)
            if "(" in name:
                continue
            # Skip if value contains a function call or undefined identifier
            # we can't safely translate
            if re.search(r"\b[a-z_]\w*\s*\(", val):
                continue
            # Translate common C literal forms
            val = re.sub(r"\b0x([0-9A-Fa-f]+)\b", r"$\1", val)
            val = val.replace("TRUE", "True").replace("FALSE", "False")
            val = re.sub(r'"([^"]*)"', r"'\1'", val)
            # Skip values that still contain operators we don't handle
            if any(op in val for op in ["->", "<<", ">>"]):
                continue
            if name in seen:
                continue
            seen.add(name)
            out.append((name, val))
        return out

    # ------------------------------------------------------------------
    def _split_name_init(self, name_init: str) -> tuple[str, str]:
        """Split 'x = 5' into ('x', '5'). For pointer decls strip leading *."""
        s = name_init.strip()
        init = ""
        if "=" in s:
            name_part, _, init = s.partition("=")
            s = name_part.strip()
            init = init.strip()
        if s.startswith("*"):
            s = s.lstrip("*").strip()
        # Strip array brackets for scalar globals
        return s, init

    # ------------------------------------------------------------------
    def _pascal_signature(self, fn: CFunction, prototype: bool = False) -> str:
        """Build a Pascal procedure/function signature.

        All identifiers (function name, parameter names) are passed
        through ``pascalify_name`` so they don't clash with Pascal
        reserved words.
        """
        ret = fn.return_type.strip()
        is_func = ret and ret != "void"
        # Build parameter list
        params: list[str] = []
        for ptype, pname, _ in fn.params:
            if not pname:
                continue
            pas_type = c_type_to_pascal(ptype)
            if not pas_type:
                continue
            if pas_type == "Char":
                pas_type = "PChar"
            params.append(f"{pascalify_name(pname)}: {pas_type}")
        params_str = "; ".join(params)
        prefix = "function" if is_func else "procedure"
        sig = f"{prefix} {pascalify_name(fn.name)}"
        if params_str:
            sig += f"({params_str})"
        if is_func:
            ret_pas = c_type_to_pascal(ret)
            if not ret_pas:
                ret_pas = "TObject"
            sig += f": {ret_pas}"
        return sig

    # ------------------------------------------------------------------
    def _translate_function(self, fn: CFunction) -> list[str]:
        sig = self._pascal_signature(fn, prototype=False)
        if not sig:
            return [f"// skipped function {fn.name}"]
        out: list[str] = []
        out.append(sig + ";")
        # Track the line offset where this function's body will start,
        # so the BodyTranslator can compute absolute output line numbers
        # for its TodoItems.
        body_offset = self._header_line_count + len(out) + 1  # +1 for "var"
        translator = BodyTranslator(fn, indent=2,
                                     source_file=self.c_path)
        translator._pas_line = body_offset
        body_lines = translator.translate()
        # Emit label block first (if any), then var block.
        # Pascal requires: label ...; var ...; begin ... end;
        if hasattr(translator, '_labels') and translator._labels:
            out.append("label")
            for i, lbl in enumerate(translator._labels):
                sep = "," if i < len(translator._labels) - 1 else ";"
                out.append(f"  {lbl}{sep}")
        # Emit collected local var declarations in a var block.
        if translator.locals:
            out.append("var")
            for decl in translator.locals:
                out.append(decl)
        else:
            out.append("var")
            out.append("  // (no local variables detected)")
        out.append("begin")
        out.extend(body_lines)
        if translator.todo_count == 0 and translator.translated_count == 0:
            out.append("  // empty body")
        out.append("end;")
        self.stats["translated_stmts"] += translator.translated_count
        self.stats["todo_stmts"] += translator.todo_count
        # Collect this function's TODOs (output_file is patched later).
        self.todos.extend(translator.todos)
        return out


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------
def convert_c_file(c_path: str, out_dir: str,
                   unit_name: Optional[str] = None,
                   form_unit: Optional[str] = None,
                   included_units: list[str] = None,
                   fake_include_dir: Optional[str] = None,
                   use_ast: bool = True,
                   header_types: list[str] = None,
                   header_defines: list = None,
                   header_externs: list = None) -> dict:
    """Convert one .c file to a .pas unit under ``out_dir``.

    Parameters
    ----------
    included_units
        Pascal unit names (from local headers) to add to the uses clause.
    fake_include_dir
        Directory of stub libc/GTK headers for pycparser. If None, the
        converter looks for ``/tmp/fake_libc_include`` and creates it
        on first use by running ``gen_fake_includes.py``.
    use_ast
        If True (default), try pycparser for accurate signatures and
        fall back to pattern-based translation on failure.
    """
    if unit_name is None:
        unit_name = os.path.splitext(os.path.basename(c_path))[0].lower()
        if unit_name[0].isdigit():
            unit_name = "u" + unit_name
    # Auto-create fake includes if needed
    if fake_include_dir is None:
        fake_include_dir = _ensure_fake_includes()
    translator = CFileTranslator(c_path, unit_name, form_unit,
                                  included_units=included_units,
                                  fake_include_dir=fake_include_dir,
                                  use_ast=use_ast,
                                  header_types=header_types,
                                  header_defines=header_defines,
                                  header_externs=header_externs)
    text = translator.translate()
    # Compute header line count: count newlines up to "implementation\n"
    impl_idx = text.find("\nimplementation\n")
    if impl_idx >= 0:
        translator._header_line_count = text[:impl_idx].count("\n") + 2
    # Re-translate for accurate output_line in TodoItems
    translator2 = CFileTranslator(c_path, unit_name, form_unit,
                                   included_units=included_units,
                                   fake_include_dir=fake_include_dir,
                                   use_ast=use_ast,
                                   header_types=header_types,
                                   header_defines=header_defines,
                                   header_externs=header_externs)
    translator2._header_line_count = translator._header_line_count
    text = translator2.translate()
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, unit_name + ".pas")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    # Patch output_file in each todo now that we know the path.
    for todo in translator2.todos:
        todo["output_file"] = out_path
    return {
        "out_path": out_path,
        "unit_name": unit_name,
        "source": c_path,
        "stats": translator2.stats,
        "todos": translator2.todos,
    }


# ---------------------------------------------------------------------------
# Include scanning
# ---------------------------------------------------------------------------
# Regex matching a #include "..." (local header) directive.
_LOCAL_INCLUDE_RE = re.compile(
    r'^\s*#\s*include\s+"([^"]+)"',
    re.MULTILINE,
)


def scan_local_includes(c_path: str) -> list[str]:
    """Return the list of locally-included header file *names* (basename
    only) from ``c_path``.

    Only ``#include "foo.h"`` (quotes) counts; system headers with
    ``<...>`` are ignored.
    """
    try:
        with open(c_path, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError:
        return []
    # Strip C comments first so commented-out includes are not picked up.
    src = strip_comments(src)
    return _LOCAL_INCLUDE_RE.findall(src)


def header_to_unit_name(h_path: str) -> str:
    """Convert a .h file path to a Pascal unit name.

    e.g. ``strings.h`` -> ``strings``, ``common.h`` -> ``common``,
    ``progAVR.h`` -> ``progavr`` (Pascal unit identifiers are
    case-insensitive).
    """
    base = os.path.splitext(os.path.basename(h_path))[0].lower()
    if base and base[0].isdigit():
        base = "u" + base
    return base


# ---------------------------------------------------------------------------
# Header file translator  (.h -> .pas)
# ---------------------------------------------------------------------------
class HeaderTranslator:
    """Translate a single .h file into a Pascal unit (.pas).

    Headers contain declarations only (no function bodies), so the
    generated unit is interface-only:

      * ``#define X Y``   ->  ``const X = Y;``
      * ``typedef struct {...} T;`` -> ``type T = record ... end;``
      * ``typedef enum {A, B, C} E;`` -> ``type E = (A, B, C);``
      * ``extern int x;``  -> ``var x: Integer;``
      * ``int foo(int a);`` -> ``function foo(a: Integer): Integer;``

    Anything unrecognised becomes a TODO comment so the user can finish
    the header by hand. The resulting unit name follows the same
    convention as for .c files (lowercase basename), so the ``uses``
    clause emitted by ``CFileTranslator`` will resolve.
    """

    def __init__(self, h_path: str, unit_name: str,
                 included_units: list[str] = None):
        self.h_path = h_path
        self.unit_name = unit_name
        self.included_units = list(included_units or [])
        self.todos: list[dict] = []
        self.stats = {
            "defines": 0, "typedefs": 0, "globals": 0,
            "prototypes": 0, "todo_stmts": 0, "lines_in": 0,
            "lines_out": 0,
        }
        # Store extracted declarations so the caller (convert_header_file)
        # can pass them to the matching .c file's CFileTranslator.
        self.extracted_types: list[str] = []
        self.extracted_defines: list[tuple[str, str]] = []
        self.extracted_externs: list[tuple[str, str]] = []

    # ------------------------------------------------------------------
    def translate(self) -> str:
        try:
            with open(self.h_path, "r",
                      encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            return (f"unit {self.unit_name};\n\n"
                    "interface\n\nimplementation\nend.\n")
        self.stats["lines_in"] = src.count("\n") + 1
        src_clean = strip_comments(src)
        # Pull out preprocessor directives (we will inspect them ourselves)
        src_no_pp, directives = strip_preprocessor(src_clean)
        # Also strip string literals so simple regex doesn't get confused.
        # (We keep them in the body for typedef parsing, though.)

        out: list[str] = []
        out.append(f"unit {self.unit_name};")
        out.append("")
        out.append("{$mode objfpc}{$H+}")
        out.append("")
        out.append("interface")
        out.append("")
        out.append("uses")
        # Always include common LCL units so type references resolve.
        if self.included_units:
            unit_list = (["Classes", "SysUtils", "Forms", "Controls",
                          "Graphics", "Dialogs", "StdCtrls", "ComCtrls",
                          "ExtCtrls", "Buttons", "Spin", "Menus", "LCLType"]
                         + self.included_units)
        else:
            unit_list = ["Classes", "SysUtils", "Forms", "Controls",
                         "Graphics", "Dialogs", "StdCtrls", "ComCtrls",
                         "ExtCtrls", "Buttons", "Spin", "Menus", "LCLType"]
        for i, u in enumerate(unit_list):
            sep = "," if i < len(unit_list) - 1 else ";"
            out.append(f"  {u}{sep}")
        out.append("")
        out.append("type")
        out.append("  DWORD = Cardinal;")
        out.append("  WORD  = Word;")
        out.append("  BYTE  = Byte;")

        # 1) #define -> const
        defines = self._extract_defines(directives)
        self.extracted_defines = defines
        if defines:
            out.append("const")
            for name, val in defines:
                out.append(f"  {name} = {val};")
            out.append("")
            self.stats["defines"] = len(defines)

        # 2) typedefs (struct/enum) -> type
        #    Append to the existing ``type`` block opened above.
        #    Also get the consumed spans so _leftover_lines_with_lines
        #    can skip lines that were part of a typedef/struct/enum block.
        typedefs, consumed_spans = self._extract_typedefs(src_no_pp)
        self.extracted_types = typedefs
        if typedefs:
            # The ``type`` keyword was already emitted above (with DWORD,
            # WORD, BYTE). We append the struct/enum/typedef declarations
            # directly into that same type block.
            for t in typedefs:
                out.append(t)
            out.append("")
            self.stats["typedefs"] = len(typedefs)
        else:
            out.append("")

        # 3) extern globals -> var
        externs = self._extract_externs(src_no_pp)
        self.extracted_externs = externs
        if externs:
            out.append("var")
            seen = set()
            for name, ptype in externs:
                if name in seen:
                    continue
                seen.add(name)
                out.append(f"  {pascalify_name(name)}: {ptype};")
            out.append("")
            self.stats["globals"] = len(externs)

        # 4) function prototypes
        protos, proto_spans = self._extract_prototypes(src_no_pp)
        consumed_spans.extend(proto_spans)
        if protos:
            out.append("// --- function declarations ---")
            for sig in protos:
                out.append(sig + ";")
            out.append("")
            self.stats["prototypes"] = len(protos)

        # Anything else: emit as TODO comments so the user can see what
        # was not handled. We collect a few leftover lines as a hint.
        # Try to find the source line for each leftover line so we can
        # include the original C context.
        leftover = self._leftover_lines_with_lines(src_no_pp, consumed_spans)
        for line_text, src_line in leftover[:30]:
            # Multi-line TODO comment with original C source
            out.append(f"  (* TODO gtk2laz: header line not translated")
            out.append(f"    Source: {self.h_path}:{src_line}")
            out.append(f"    Original C:")
            out.append(f"      {line_text.strip()[:200]}")
            out.append(f"  *)")
            self.todos.append({
                "source_file": self.h_path,
                "source_line": src_line,
                "source_text": line_text.strip()[:200],
                "output_file": "",
                "output_line": 0,
                "reason": "header line not translated",
                "function": "",
            })
            self.stats["todo_stmts"] += 1

        out.append("implementation")
        out.append("")
        out.append("end.")
        text = "\n".join(out) + "\n"
        self.stats["lines_out"] = text.count("\n") + 1
        return text

    # ------------------------------------------------------------------
    def _extract_defines(self, directives: list[str]) -> list[tuple[str, str]]:
        """Convert #define lines to Pascal constants."""
        out: list[tuple[str, str]] = []
        seen = set()
        for d in directives:
            m = re.match(r"#\s*define\s+(\w+)\s+(.+)", d)
            if not m:
                continue
            name = m.group(1)
            val = m.group(2).strip()
            if "(" in name:
                continue  # macro with params - skip
            if re.search(r"\b[a-z_]\w*\s*\(", val):
                continue  # value contains a function call
            val = re.sub(r"\b0x([0-9A-Fa-f]+)\b", r"$\1", val)
            val = val.replace("TRUE", "True").replace("FALSE", "False")
            val = re.sub(r'"([^"]*)"', r"'\1'", val)
            if any(op in val for op in ["->", "<<", ">>"]):
                continue
            if name in seen:
                continue
            seen.add(name)
            out.append((name, val))
        return out

    # ------------------------------------------------------------------
    def _extract_typedefs(self, src: str) -> tuple[list[str], list[tuple[int, int]]]:
        """Extract struct/enum/typedef declarations from a header.

        Handles these C patterns:
          * ``typedef struct { ... } NAME;``
          * ``typedef struct TAG { ... } NAME;``  (with tag name)
          * ``typedef enum { ... } NAME;``
          * ``typedef enum TAG { ... } NAME;``  (with tag name)
          * ``struct TAG { ... };``  (no typedef — TAG becomes the type)
          * ``enum TAG { ... };``  (no typedef — TAG becomes the type)
          * ``typedef OLD NEW;``  (simple alias)

        Returns ``(declarations, consumed_spans)`` where ``declarations``
        is a list of Pascal type declaration strings and ``consumed_spans``
        is a list of (start, end) character offsets that were consumed
        (used by ``_leftover_lines_with_lines`` to skip processed lines).
        """
        out: list[str] = []
        # Track which names we've already declared so we don't duplicate
        declared_names: set[str] = set()
        # Track spans of text we've consumed so later passes don't re-match
        consumed_spans: list[tuple[int, int]] = []

        def _is_consumed(start: int, end: int) -> bool:
            for cs, ce in consumed_spans:
                if start >= cs and end <= ce:
                    return True
            return False

        def _match_braced_block(text: str, start: int) -> int:
            """Given ``text`` and the index of an opening ``{`` at
            ``start``, return the index just past the matching ``}``.
            Handles nested braces. Returns -1 if no match found."""
            depth = 0
            i = start
            while i < len(text):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        return i + 1
                i += 1
            return -1

        def _find_typedef_block(text: str, keyword: str) -> list[tuple[int, int, str, str]]:
            """Find all ``typedef <keyword> [TAG] { ... } NAME;`` blocks.

            Returns a list of (start, end, body, name) tuples where:
              * start/end are character offsets into ``text`` (end is
                past the final ``;``)
              * body is the text between the outermost ``{`` and ``}``
              * name is the typedef name (e.g. ``filehdr_t``)

            Uses a custom scanner that handles nested braces (unlike
            ``re`` with ``[^}]*`` which stops at the first ``}``).
            """
            results: list[tuple[int, int, str, str]] = []
            # Find each ``typedef <keyword>`` occurrence
            for m in re.finditer(
                rf"\btypedef\s+{keyword}\s+(?:\w+\s+)?",
                text, re.DOTALL,
            ):
                block_start = m.start()
                # Find the opening brace after the keyword
                brace_pos = text.find("{", m.end())
                if brace_pos < 0:
                    continue
                # Find the matching closing brace
                body_end = _match_braced_block(text, brace_pos)
                if body_end < 0:
                    continue
                body = text[brace_pos + 1:body_end - 1]
                # After the closing brace, find the name and ``;``
                rest = text[body_end:].lstrip()
                name_match = re.match(r"(\w+)\s*;", rest)
                if not name_match:
                    continue
                name = name_match.group(1)
                block_end = body_end + (len(text[body_end:]) - len(rest)) + name_match.end()
                results.append((block_start, block_end, body, name))
            return results

        def _find_plain_block(text: str, keyword: str) -> list[tuple[int, int, str, str]]:
            """Find all ``<keyword> TAG { ... };`` blocks (no typedef).

            Returns a list of (start, end, body, tag) tuples.
            """
            results: list[tuple[int, int, str, str]] = []
            for m in re.finditer(
                rf"\b{keyword}\s+(?P<tag>\w+)\s*",
                text, re.DOTALL,
            ):
                # Make sure this isn't part of a typedef (which is
                # handled separately)
                # Check if 'typedef' appears immediately before
                before = text[:m.start()].rstrip()
                if before.endswith("typedef"):
                    continue
                block_start = m.start()
                brace_pos = text.find("{", m.end())
                if brace_pos < 0:
                    continue
                body_end = _match_braced_block(text, brace_pos)
                if body_end < 0:
                    continue
                body = text[brace_pos + 1:body_end - 1]
                # After the closing brace, expect ``;``
                rest = text[body_end:].lstrip()
                if not rest.startswith(";"):
                    continue
                tag = m.group("tag")
                block_end = body_end + (len(text[body_end:]) - len(rest)) + 1
                results.append((block_start, block_end, body, tag))
            return results

        # ---- typedef struct TAG? { ... } NAME; ----
        for start, end, body, name in _find_typedef_block(src, "struct"):
            if _is_consumed(start, end):
                continue
            pas_name = self._pascalize_type_name(name)
            if pas_name in declared_names:
                continue
            declared_names.add(pas_name)
            consumed_spans.append((start, end))
            fields = self._parse_struct_fields(body)
            if not fields:
                out.append(f"  {pas_name} = record")
                out.append(f"    (* TODO gtk2laz: empty struct body *)")
                out.append(f"  end;")
            else:
                out.append(f"  {pas_name} = record")
                seen = set()
                for fname, ftype in fields:
                    if fname in seen:
                        continue
                    seen.add(fname)
                    out.append(f"    {pascalify_name(fname)}: {ftype};")
                out.append(f"  end;")

        # ---- typedef enum TAG? { ... } NAME; ----
        for start, end, body, name in _find_typedef_block(src, "enum"):
            if _is_consumed(start, end):
                continue
            pas_name = self._pascalize_type_name(name)
            if pas_name in declared_names:
                continue
            declared_names.add(pas_name)
            consumed_spans.append((start, end))
            members = self._parse_enum_members(body)
            if members:
                out.append(f"  {pas_name} = ({', '.join(members)});")
            else:
                out.append(f"  (* TODO gtk2laz: empty enum {pas_name} *)")

        # ---- struct TAG { ... };  (no typedef) ----
        for start, end, body, tag in _find_plain_block(src, "struct"):
            if _is_consumed(start, end):
                continue
            pas_name = self._pascalize_type_name(tag)
            if pas_name in declared_names:
                continue
            declared_names.add(pas_name)
            consumed_spans.append((start, end))
            fields = self._parse_struct_fields(body)
            if not fields:
                out.append(f"  {pas_name} = record")
                out.append(f"    (* TODO gtk2laz: empty struct body *)")
                out.append(f"  end;")
            else:
                out.append(f"  {pas_name} = record")
                seen = set()
                for fname, ftype in fields:
                    if fname in seen:
                        continue
                    seen.add(fname)
                    out.append(f"    {pascalify_name(fname)}: {ftype};")
                out.append(f"  end;")

        # ---- enum TAG { ... };  (no typedef) ----
        for start, end, body, tag in _find_plain_block(src, "enum"):
            if _is_consumed(start, end):
                continue
            pas_name = self._pascalize_type_name(tag)
            if pas_name in declared_names:
                continue
            declared_names.add(pas_name)
            consumed_spans.append((start, end))
            members = self._parse_enum_members(body)
            if members:
                out.append(f"  {pas_name} = ({', '.join(members)});")
            else:
                out.append(f"  (* TODO gtk2laz: empty enum {pas_name} *)")

        # ---- Simple typedef: typedef OLD NEW; ----
        for m in re.finditer(
            r"typedef\s+(?P<old>[\w\s]+?)\s+(?P<new>\w+)\s*;",
            src,
        ):
            if _is_consumed(m.start(), m.end()):
                continue
            old = m.group("old").strip()
            new = m.group("new").strip()
            if old in ("struct", "enum", "union"):
                continue
            pas_old = c_type_to_pascal(old)
            pas_new = self._pascalize_type_name(new)
            if pas_new in declared_names:
                continue
            declared_names.add(pas_new)
            out.append(f"  {pas_new} = {pas_old};")
        return out, consumed_spans

    def _pascalize_type_name(self, name: str) -> str:
        """C type name -> Pascal type name. Avoid 'T' double-prefix."""
        if name.startswith("T") and len(name) > 1 and name[1].isupper():
            return name
        return "T" + name

    def _parse_struct_fields(self, body: str) -> list[tuple[str, str]]:
        """Parse the inside of a struct body into (field, type) tuples."""
        out: list[tuple[str, str]] = []
        # Split by ;, but be careful of nested braces (rare in structs).
        for decl in _split_top_level(body, ";"):
            decl = decl.strip()
            if not decl:
                continue
            # Skip bitfields "int x : 4;"
            decl = re.sub(r":\s*\d+", "", decl).strip()
            if not decl:
                continue
            # Match TYPE NAME pattern (NAME may have array suffix)
            m = re.match(
                r"^(?P<type>(?:const\s+|unsigned\s+|signed\s+|volatile\s+)*"
                r"[\w]+(?:\s+\w+)*\s*\**)\s+(?P<name>\w+)\s*"
                r"(?:\[(?P<arr>[^\]]*)\])?\s*$",
                decl, re.DOTALL,
            )
            if not m:
                continue
            ptype = c_type_to_pascal(m.group("type").strip())
            name = m.group("name")
            arr = m.group("arr")
            if arr:
                out.append((name, f"array[0..{arr}-1] of {ptype}"))
            else:
                out.append((name, ptype))
        return out

    def _parse_enum_members(self, body: str) -> list[str]:
        """Parse the inside of an enum body into Pascal identifiers."""
        out: list[str] = []
        for part in _split_top_level(body, ","):
            part = part.strip()
            if not part:
                continue
            # Strip "= value" - Pascal enum doesn't support explicit values
            # without typed enum tricks; emit as plain identifier.
            name = part.split("=", 1)[0].strip()
            if re.match(r"^\w+$", name):
                # Use pascalify_name to handle all reserved words uniformly
                out.append(pascalify_name(name))
        return out

    # ------------------------------------------------------------------
    def _extract_externs(self, src: str) -> list[tuple[str, str]]:
        """Extract ``extern TYPE NAME;`` declarations.

        Returns list of (name, pascal_type).
        """
        out: list[tuple[str, str]] = []
        # extern TYPE NAME;  or  extern TYPE *NAME;  or  extern TYPE NAME[N];
        for m in re.finditer(
            r"extern\s+(?P<type>(?:const\s+|volatile\s+)*"
            r"[\w]+(?:\s+\w+)*\s*\**)\s+"
            r"(?P<decl>[^;]+);",
            src,
        ):
            ptype = c_type_to_pascal(m.group("type").strip())
            decl = m.group("decl").strip()
            # Multiple declarators separated by commas
            for d in _split_top_level(decl, ","):
                d = d.strip()
                if not d:
                    continue
                # Array?
                m_arr = re.match(r"(\w+)\s*\[([^\]]*)\]", d)
                if m_arr:
                    name = m_arr.group(1)
                    size = m_arr.group(2).strip()
                    if size:
                        out.append((name, f"array[0..{size}-1] of {ptype}"))
                    else:
                        out.append((name, f"array of {ptype}"))
                    continue
                # Pointer
                if d.startswith("*"):
                    name = d.lstrip("*").strip()
                    out.append((name, ptype))
                    continue
                out.append((d, ptype))
        return out

    # ------------------------------------------------------------------
    def _extract_prototypes(self, src: str) -> tuple[list[str], list[tuple[int, int]]]:
        """Extract function prototypes and emit Pascal signatures.

        Returns ``(signatures, consumed_spans)`` where ``consumed_spans``
        is the list of (start, end) character offsets that were matched
        as prototypes (used by ``_leftover_lines_with_lines`` to skip
        processed lines).
        """
        out: list[str] = []
        consumed_spans: list[tuple[int, int]] = []
        # Merge line-continuation backslashes so multi-line prototypes
        # (``int foo(int a,\\n  int b);``) are treated as one line.
        src_merged = re.sub(r"\\\s*\n\s*", " ", src)
        # Build a mapping from merged-text offsets back to original-text
        # offsets so we can report consumed spans in the original src.
        # We do this by tracking how many chars were removed by the merge.
        offset_map: list[tuple[int, int]] = []  # (merged_off, orig_off)
        mi = 0  # merged index
        oi = 0  # original index
        i = 0
        while i < len(src):
            # Check for line-continuation: backslash + optional ws + newline + ws
            m_cont = re.match(r"\\\s*\n\s*", src[i:])
            if m_cont:
                # In merged text, this becomes a single space
                offset_map.append((mi, oi))
                mi += 1
                oi += m_cont.end()
                i += m_cont.end()
            else:
                offset_map.append((mi, oi))
                mi += 1
                oi += 1
                i += 1
        offset_map.append((mi, oi))  # sentinel

        def _merged_to_orig(merged_start: int, merged_end: int) -> tuple[int, int]:
            """Convert merged-text offsets to original-text offsets."""
            # Binary search for the original offset at each merged offset
            import bisect
            merged_offs = [m for m, _ in offset_map]
            orig_offs = [o for _, o in offset_map]
            s_idx = bisect.bisect_right(merged_offs, merged_start) - 1
            e_idx = bisect.bisect_right(merged_offs, merged_end) - 1
            return (orig_offs[s_idx], orig_offs[e_idx])

        # A prototype looks like:  TYPE NAME(params);
        # We avoid matching control-flow keywords.
        proto_re = re.compile(
            r"(?P<ret>(?:const\s+|static\s+|inline\s+|extern\s+|volatile\s+)*"
            r"[\w][\w\s\*]*?)\s+"
            r"(?P<name>[A-Za-z_]\w*)\s*"
            r"\((?P<params>[^;]*)\)\s*;",
            re.DOTALL,
        )
        for m in proto_re.finditer(src_merged):
            name = m.group("name")
            if name in _C_KEYWORDS:
                continue
            ret = m.group("ret").strip()
            if not ret:
                continue
            if ret.split()[-1] in _C_KEYWORDS:
                continue
            # Map merged offsets back to original src offsets
            orig_start, orig_end = _merged_to_orig(m.start(), m.end())
            consumed_spans.append((orig_start, orig_end))
            params_raw = m.group("params").strip()
            params = []
            for p in _split_top_level(params_raw):
                p = p.strip()
                if not p or p == "void":
                    continue
                ptype, pname, _ = _parse_param(p)
                if not pname:
                    continue
                pas_type = c_type_to_pascal(ptype)
                if pas_type == "Char":
                    pas_type = "PChar"
                params.append(f"{pascalify_name(pname)}: {pas_type}")
            params_str = "; ".join(params)
            is_func = ret and ret != "void"
            prefix = "function" if is_func else "procedure"
            sig = f"{prefix} {pascalify_name(name)}"
            if params_str:
                sig += f"({params_str})"
            if is_func:
                ret_pas = c_type_to_pascal(ret)
                if not ret_pas:
                    ret_pas = "TObject"
                sig += f": {ret_pas}"
            out.append(sig)
        return out, consumed_spans

    # ------------------------------------------------------------------
    def _leftover_lines(self, src: str) -> list[str]:
        """Return lines that are not typedef/extern/prototype, for TODO hints.

        We strip preprocessor lines and known declarations to surface
        anything else that might need attention (macros with params,
        inline function bodies, etc.).
        """
        out: list[str] = []
        for line in src.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                continue
            if stripped.startswith("typedef"):
                continue
            if stripped.startswith("extern"):
                continue
            # Heuristic: looks like a prototype?
            if re.match(r"^[\w\s\*]+\s+\w+\s*\([^;]*\)\s*;", stripped):
                continue
            # Closing brace of a struct/enum typedef
            if stripped in ("}", "};"):
                continue
            # Member declaration inside a struct (TYPE NAME;)
            if re.match(r"^[\w\s\*]+\s+\w+(\s*\[[^\]]*\])?\s*;", stripped):
                continue
            out.append(stripped)
        return out

    def _leftover_lines_with_lines(self, src: str,
                                    consumed_spans: list[tuple[int, int]] = None
                                    ) -> list[tuple[str, int]]:
        """Like ``_leftover_lines`` but also returns the 1-based source
        line number for each leftover line, so we can emit accurate
        ``Source: <file>:<line>`` references in TODO comments.

        ``consumed_spans`` is a list of (start, end) character offsets into
        ``src`` that were already consumed by ``_extract_typedefs`` (e.g.
        the entire ``typedef struct { ... } NAME;`` block). Lines that fall
        within any consumed span are skipped so they don't appear as
        leftover TODOs.
        """
        if consumed_spans is None:
            consumed_spans = []
        # Build a set of line numbers that fall within any consumed span.
        # We do this by walking through ``src`` and tracking character
        # offset → line number.
        consumed_lines: set[int] = set()
        if consumed_spans:
            line_starts = [0]  # char offset of start of each line
            for i, ch in enumerate(src):
                if ch == "\n":
                    line_starts.append(i + 1)
            for span_start, span_end in consumed_spans:
                # Find the line numbers that overlap this span
                for line_idx, line_start in enumerate(line_starts):
                    line_end = (line_starts[line_idx + 1] - 1
                                if line_idx + 1 < len(line_starts)
                                else len(src))
                    # Line is (line_idx + 1) 1-based
                    if line_start < span_end and line_end >= span_start:
                        consumed_lines.add(line_idx + 1)

        out: list[tuple[str, int]] = []
        for i, line in enumerate(src.splitlines(), start=1):
            # Skip lines that were part of a consumed typedef/struct/enum
            if i in consumed_lines:
                continue
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                continue
            if stripped.startswith("typedef"):
                continue
            if stripped.startswith("extern"):
                continue
            if re.match(r"^[\w\s\*]+\s+\w+\s*\([^;]*\)\s*;", stripped):
                continue
            if stripped in ("}", "};"):
                continue
            if re.match(r"^[\w\s\*]+\s+\w+(\s*\[[^\]]*\])?\s*;", stripped):
                continue
            out.append((stripped, i))
        return out


# ---------------------------------------------------------------------------
# Top-level entry for headers
# ---------------------------------------------------------------------------
def convert_header_file(h_path: str, out_dir: str,
                         unit_name: Optional[str] = None,
                         included_units: list[str] = None) -> dict:
    """Convert one .h file to a .pas unit under ``out_dir``.

    ``included_units`` is the list of Pascal unit names (lowercase) that
    should be added to the generated unit's ``uses`` clause - typically
    the units produced from headers that this header itself #includes.
    """
    if unit_name is None:
        unit_name = header_to_unit_name(h_path)
    translator = HeaderTranslator(h_path, unit_name, included_units)
    text = translator.translate()
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, unit_name + ".pas")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    for todo in translator.todos:
        todo["output_file"] = out_path
    return {
        "out_path": out_path,
        "unit_name": unit_name,
        "source": h_path,
        "stats": translator.stats,
        "todos": translator.todos,
        # Pass extracted declarations to the caller so they can be merged
        # into the matching .c file's output (when .c and .h share the
        # same basename, the .c output would otherwise overwrite the .h
        # output and lose these declarations).
        "header_types": translator.extracted_types,
        "header_defines": translator.extracted_defines,
        "header_externs": translator.extracted_externs,
    }


# ---------------------------------------------------------------------------
# Fake include management
# ---------------------------------------------------------------------------
# Use a platform-appropriate temp directory for the fake headers.
import tempfile as _tempfile
if sys.platform == "win32":
    _FAKE_INCLUDE_DIR = os.path.join(_tempfile.gettempdir(), "fake_libc_include")
else:
    _FAKE_INCLUDE_DIR = "/tmp/fake_libc_include"
_FAKE_INCLUDES_READY = False


def _ensure_fake_includes() -> str:
    """Make sure the fake libc/GTK stub headers exist; return their dir.

    On first call, runs the gen_fake_includes.py script (which is
    shipped alongside the gtk2laz modules) to generate stub headers in
    the platform-appropriate temp directory. Subsequent calls return
    immediately.
    """
    global _FAKE_INCLUDES_READY
    if _FAKE_INCLUDES_READY:
        return _FAKE_INCLUDE_DIR
    # Check if dir already populated
    sentinel = os.path.join(_FAKE_INCLUDE_DIR, "gtk", "gtk.h")
    if os.path.isfile(sentinel):
        _FAKE_INCLUDES_READY = True
        return _FAKE_INCLUDE_DIR
    # Generate by importing the gen_fake_includes module (works both in
    # source layout and under PyInstaller, since gen_fake_includes.py is
    # bundled as a data file).
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        meipass = getattr(sys, "_MEIPASS", None)
        if here not in sys.path:
            sys.path.insert(0, here)
        if meipass and meipass not in sys.path:
            sys.path.insert(0, meipass)
        import gen_fake_includes
        # generate() uses the platform-appropriate default dir
        gen_fake_includes.generate()
        if os.path.isfile(sentinel):
            _FAKE_INCLUDES_READY = True
        else:
            # Try explicit path
            gen_fake_includes.generate(_FAKE_INCLUDE_DIR)
            if os.path.isfile(sentinel):
                _FAKE_INCLUDES_READY = True
    except Exception:
        _FAKE_INCLUDES_READY = False
    return _FAKE_INCLUDE_DIR
