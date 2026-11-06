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
    """Replace comments with whitespace, keeping newlines.

    Exception: ``/* Macro: ... */`` markers emitted by the macro
    expansion are PRESERVED — they document where each macro call was
    expanded and are converted to Pascal comments by the translator.
    """
    def repl(m: re.Match) -> str:
        text = m.group(0)
        if text.lstrip().startswith("/*") and "Macro:" in text:
            return text
        return "\n" * text.count("\n")
    return _COMMENT_RE.sub(repl, src)


def strip_preprocessor(src: str) -> tuple[str, list[str]]:
    """Pull out #define/#include lines so the parser can ignore them.

    Also evaluates PROVABLE conditional-compilation branches:
      * ``#ifdef X`` where X was explicitly ``#undef``'d in this file
        (and never ``#define``d) — the branch is blanked.  This is the
        ``#undef ENET_CONTEXT_EXCLUSION`` feature-disable idiom.
      * ``#if 0`` branches are blanked.
      * Unknown conditions keep BOTH branches (the previous behaviour)
        because the symbol may be defined by the real build command
        line or by headers we don't see here.

    Inactive lines are blanked (keeping line numbering) and their
    directives are NOT collected.
    """
    directives: list[str] = []
    out_lines: list[str] = []
    defined: set[str] = set()
    undefed: set[str] = set()   # explicitly #undef'd, never defined here
    # Conditional stack. Each entry:
    #   [parent_active, branch_active, any_branch_taken, both_mode]
    # ``both_mode`` entries keep every branch active (unknown
    # conditions — preserves the old keep-everything behaviour).
    stack: list[list] = []

    def act() -> bool:
        for parent, cur, _t, _b in stack:
            if not (parent and cur):
                return False
        return True

    for line in src.splitlines():
        m_dir = re.match(r"\s*#\s*(\w+)\s*(.*)$", line)
        if m_dir:
            kw = m_dir.group(1)
            rest = m_dir.group(2).strip()
            if kw in ("ifdef", "ifndef"):
                parts = rest.split()
                name = parts[0] if parts else ""
                if name in defined:
                    cond = (kw == "ifdef")
                    stack.append([act(), cond, cond, False])
                elif name in undefed:
                    cond = (kw == "ifndef")
                    stack.append([act(), cond, cond, False])
                else:
                    # Unknown symbol — keep both branches.
                    stack.append([act(), True, True, True])
            elif kw == "if":
                cond = _eval_pp_cond(rest, defined, undefed)
                if cond is None:
                    stack.append([act(), True, True, True])
                else:
                    stack.append([act(), cond, cond, False])
            elif kw == "elif":
                if stack:
                    cond = _eval_pp_cond(rest, defined, undefed)
                    entry = stack[-1]
                    if entry[3]:
                        entry[1] = True
                    else:
                        c = (not entry[2]) and (cond is None or cond)
                        entry[1] = c and entry[0]
                        entry[2] = entry[2] or c
                else:
                    stack.append([True, True, True, True])
            elif kw == "else":
                if stack:
                    entry = stack[-1]
                    if entry[3]:
                        entry[1] = True
                    else:
                        entry[1] = (not entry[2]) and entry[0]
            elif kw == "endif":
                if stack:
                    stack.pop()
            elif kw == "define":
                m_name = re.match(r"(\w+)", rest)
                if m_name and act():
                    defined.add(m_name.group(1))
                    undefed.discard(m_name.group(1))
                if act():
                    directives.append(line.strip())
                out_lines.append("")
                continue
            elif kw == "undef":
                m_name = re.match(r"(\w+)", rest)
                if m_name:
                    nm = m_name.group(1)
                    if nm in defined:
                        defined.discard(nm)
                    else:
                        undefed.add(nm)
                out_lines.append("")
                continue
            else:
                # include / pragma / error / warning / line ...
                if act():
                    directives.append(line.strip())
                out_lines.append("")
                continue
            # Conditional directives are blanked, never collected.
            out_lines.append("")
            continue
        if act():
            out_lines.append(line)
        else:
            out_lines.append("")
    return "\n".join(out_lines), directives


def _eval_pp_cond(
    expr: str,
    defined: set,
    undefed: set
):
    """Evaluate a ``#if`` expression.  Returns True/False, or ``None``
    when the expression references unknown symbols (caller keeps both
    branches in that case)."""
    expr = expr.strip()
    if not expr:
        return None
    if expr in ("0", "false"):
        return False
    if expr in ("1", "true"):
        return True
    resolved_known = True

    def repl_defined(m: re.Match) -> str:
        nonlocal resolved_known
        name = m.group(1)
        if name in defined:
            return "1"
        if name in undefed:
            return "0"
        resolved_known = False
        return "1"

    e = re.sub(r"defined\s*\(\s*(\w+)\s*\)", repl_defined, expr)
    e = re.sub(r"defined\s+(\w+)", repl_defined, e)
    if not resolved_known:
        return None
    # Any remaining identifiers (macros we don't know) → unknown
    if re.search(r"[A-Za-z_]\w*", e):
        return None
    e = e.replace("&&", " and ").replace("||", " or ")
    e = re.sub(r"(?<![><=!])!(?!=)", " not ", e)
    try:
        return bool(eval(e, {"__builtins__": {}}, {}))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Function-like macro extraction
# ---------------------------------------------------------------------------
def _split_top_level_brace_aware(s: str, sep: str = ",") -> list[str]:
    """Like _split_top_level, but also tracks brace depth ``{ }`` so
    that ``;`` inside a ``{ ... }`` block is NOT treated as a separator.

    This is used when splitting expanded macro bodies into statements:
    a macro like ``ENET_RANGE_CODER_ENCODE`` contains
    ``for (;;) { stmt1; stmt2; }`` where the ``;`` inside the ``{ }``
    should NOT be split (otherwise the ``for`` block's braces become
    unbalanced).

    Both ``()`` and ``{}`` nesting is tracked, and string/char
    literals are skipped.
    """
    parts: list[str] = []
    depth_paren = 0
    depth_brace = 0
    in_str = False
    in_char = False
    str_escape = False
    cur: list[str] = []
    for ch in s:
        if in_str:
            cur.append(ch)
            if str_escape:
                str_escape = False
            elif ch == "\\":
                str_escape = True
            elif ch == '"':
                in_str = False
            continue
        if in_char:
            cur.append(ch)
            if str_escape:
                str_escape = False
            elif ch == "\\":
                str_escape = True
            elif ch == "'":
                in_char = False
            continue
        if ch == '"':
            in_str = True
            cur.append(ch)
            continue
        if ch == "'":
            in_char = True
            cur.append(ch)
            continue
        if ch in "([{":
            if ch == "(":
                depth_paren += 1
            elif ch == "{":
                depth_brace += 1
            cur.append(ch)
            continue
        if ch in ")]}":
            if ch == ")":
                if depth_paren > 0:
                    depth_paren -= 1
            elif ch == "}":
                if depth_brace > 0:
                    depth_brace -= 1
            cur.append(ch)
            continue
        if ch == sep and depth_paren == 0 and depth_brace == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return parts


def _extract_func_macros(src: str) -> list[tuple[str, list[str], str]]:
    """Extract statement-like macros from C source.

    Three kinds of macros are extracted for manual (pre-pcpp) inline
    expansion:

      1. Function-like macros with a substantial body —
         ``#define NAME(a, b) { stmt1; stmt2; }`` where the body
         contains ``{`` or ``;``.  These are expanded as scope blocks.
      2. Function-like macros whose body is a single call to ANOTHER
         extracted macro — e.g.
         ``#define ENET_CONTEXT_TRY_DECODE(...) ENET_CONTEXT_DECODE(...)``.
         Without this, pcpp would expand the outer call into a line
         that still contains a raw call to the manually-managed inner
         macro (whose #define we rename away), and the call would
         never be expanded.
      3. Object-like macros with a brace/statement body —
         ``#define ENET_RANGE_CODER_FLUSH \\ { ... }`` (no parameter
         list).  Their bodies are real statements, so pcpp would just
         collapse them into one long line; we expand them as scope
         blocks instead.  Simple constant macros (``#define X 1``,
         ``#define NL "..."``) are NOT extracted — pcpp handles those.

    Returns a list of ``(name, params, body)`` tuples where:
      * ``name`` — the macro name (e.g. ``ENET_SYMBOL_CREATE``)
      * ``params`` — list of parameter names; ``[]`` for object-like
      * ``body`` — the macro body as a single string (line continuations
        collapsed to spaces, ``\\`` removed)
    """
    macros: list[tuple[str, list[str], str]] = []
    lines = src.splitlines()
    # ---- Pass A: collect all macro names (function-like AND
    # object-like) so "pure call" bodies can be detected.
    all_names: set[str] = set()
    for line in lines:
        s = line.strip()
        if not s.startswith("#"):
            continue
        m_any = re.match(r'#\s*define\s+(\w+)', s)
        if m_any:
            all_names.add(m_any.group(1))
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        # ---- Function-like: NAME immediately followed by ( ----
        m = re.match(
            r'#\s*define\s+(\w+)\(([^)]*)\)\s*(.*)',
            line
        )
        if not m:
            # ---- Object-like: NAME followed by whitespace + body ----
            # (#define ENET_RANGE_CODER_FLUSH \
            #  { ... })  — no parameter list.
            m_obj = re.match(r'#\s*define\s+(\w+)\s*([^\s(].*)', line)
            if not m_obj:
                i += 1
                continue
            obj_name = m_obj.group(1)
            obj_first = m_obj.group(2).strip()
            # Skip simple constant macros (string/char/number bodies)
            if obj_first.startswith('"') or obj_first.startswith("'"):
                i += 1
                continue
            body_parts = []
            if obj_first.endswith("\\"):
                body_parts.append(obj_first[:-1].strip())
            else:
                body_parts.append(obj_first)
            if obj_first.endswith("\\") or not obj_first:
                j = i + 1
                while j < n:
                    cont = lines[j].strip()
                    if cont.endswith("\\"):
                        body_parts.append(cont[:-1].strip())
                        j += 1
                    else:
                        body_parts.append(cont)
                        j += 1
                        break
                i = j
            else:
                i += 1
            obj_body = " ".join(body_parts).strip()
            # Only statement-like object-like bodies ({ or ; present)
            if obj_body and ("{" in obj_body or ";" in obj_body) \
                    and len(obj_body) > 10:
                macros.append((obj_name, [], obj_body))
            continue
        name = m.group(1)
        params_str = m.group(2).strip()
        first_body = m.group(3).strip()
        params = [p.strip() for p in params_str.split(",") if p.strip()] if params_str else []
        # Collect continuation lines (lines ending with \)
        body_parts = []
        if first_body:
            if first_body.endswith("\\"):
                body_parts.append(first_body[:-1].strip())
            else:
                body_parts.append(first_body)
                # Check if next lines continue
        else:
            # Body starts on next line
            pass
        # Follow continuation lines
        if first_body.endswith("\\") or not first_body:
            j = i + 1
            while j < n:
                cont = lines[j].strip()
                if cont.endswith("\\"):
                    body_parts.append(cont[:-1].strip())
                    j += 1
                else:
                    body_parts.append(cont)
                    j += 1
                    break
            i = j
        else:
            i += 1
        body = " ".join(body_parts).strip()
        if not body:
            # Empty-body macro (e.g. the ``#define ENET_CONTEXT_NOT_EXCLUDED(a, b)``)
            # no-op that enet uses for disabled features) — extract it
            # too so the manual expansion DELETES its call sites.
            if len(params) > 0:
                macros.append((name, params, ""))
            continue
        if len(body) <= 10:
            continue
        # Keep function-like macros with substantial bodies (contains
        # { or ;) — these expand as scope blocks.
        if ("{" in body or ";" in body) and len(params) > 0:
            macros.append((name, params, body))
            continue
        # Also keep macros whose body is a single call to ANOTHER
        # extracted macro (e.g. ENET_CONTEXT_TRY_DECODE expands to a
        # ENET_CONTEXT_DECODE call).  Without this, pcpp would expand
        # the outer call into a line that still contains a raw call to
        # the manually-managed inner macro, and it would never be
        # expanded.
        m_call = re.match(r'^\s*([A-Za-z_]\w*)\s*\(', body)
        if m_call and m_call.group(1) in all_names:
            macros.append((name, params, body))
    return macros


def _expand_func_macros_in_source(
    src: str,
    macros: list[tuple[str, list[str], str]]
) -> str:
    """Replace function-like macro calls in ``src`` with the macro
    body, inlined at the call site with parameter substitution, AND
    remove the ``#define`` lines so pcpp doesn't re-expand them.

    This is a manual macro expansion that runs BEFORE pcpp so the
    expanded code is split into individual statements (one per line)
    rather than collapsed into a single long line (which is what pcpp
    does and which the pattern-based translator can't parse).

    For each macro call ``MACRO_NAME(arg1, arg2, ...)`` found in the
    source (outside string/char literals and preprocessor directives),
    we:

      1. Substitute the macro parameters in the body with the actual
         argument values (simple text substitution — we don't handle
         token-pasting or stringification).
      2. Strip the outer ``{ }`` braces if present.
      3. Split the body on ``;`` into individual statements.
      4. Replace the macro call with the statements, each on its own
         line, with the first line preceded by a ``/* Macro: ... */``
         comment showing the original call.

    We also remove the ``#define MACRO_NAME(...)`` lines (and their
    ``\\`` continuation lines) so pcpp doesn't re-expand the macros.
    """
    if not macros:
        return src
    # Build a lookup: macro name -> (params, body, brace_count)
    # We track the net { } count of each macro's body so we can
    # preserve the brace balance when removing the #define lines.
    macro_map: dict[str, tuple[list[str], str]] = {}
    macro_names: set[str] = set()
    macro_brace_counts: dict[str, int] = {}
    for name, params, body in macros:
        macro_map[name] = (params, body)
        macro_names.add(name)
        # Count net braces in the body (should be 0 for balanced body)
        # but we use the total { count as a proxy for how many { }
        # pairs the definition contributes.
        macro_brace_counts[name] = body.count("{")

    # First pass: replace the #define for these macros with a
    # renamed version whose BODY IS EMPTIED.  We keep the #define
    # line (renamed to __SKIP_NAME) so pcpp sees a valid preprocessor
    # directive, but we replace all continuation lines (the body)
    # with empty lines.  This way:
    #   - pcpp doesn't expand NAME(...) (it only knows __SKIP_NAME)
    #   - The body's { } are NOT in the preprocessed output
    #   - Our manual expansion provides the { } for each call site
    #   - Total { } balance: each call adds body's { }, no definition
    #     { } to subtract → balanced (since we removed the body { }
    #     AND we're adding body { } per call)
    lines = src.splitlines()
    filtered_lines: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        m = re.match(
            r'#\s*define\s+(\w+)\(',
            stripped
        )
        m_obj = None
        if not m:
            # Object-like define: name NOT followed by '('
            m_obj = re.match(r'#\s*define\s+(\w+)\s*([^\s(].*)', stripped)
        if m and m.group(1) in macro_names:
            macro_name = m.group(1)
            # Rename the #define line
            renamed = re.sub(
                r'#\s*define\s+' + re.escape(macro_name) + r'\(',
                f'#define __SKIP_{macro_name}(',
                stripped,
                count=1
            )
            filtered_lines.append(renamed)
            i += 1
            # Replace continuation lines with empty lines to remove
            # the body's { } from the source
            while i < n and lines[i].strip().endswith("\\"):
                filtered_lines.append("")  # empty line replaces body
                i += 1
            if i < n:
                filtered_lines.append("")  # last continuation (no \)
                i += 1
            continue
        if m_obj and m_obj.group(1) in macro_names:
            macro_name = m_obj.group(1)
            # Object-like #define: keep the line (renamed) but empty
            # its continuation lines, exactly like the function-like
            # case.  The trailing ``\`` on the renamed line joins it
            # to the (now empty) continuation lines, so pcpp sees an
            # empty body.
            renamed = re.sub(
                r'#\s*define\s+' + re.escape(macro_name) + r'\b',
                f'#define __SKIP_{macro_name}',
                stripped,
                count=1
            )
            filtered_lines.append(renamed)
            i += 1
            while i < n and lines[i].strip().endswith("\\"):
                filtered_lines.append("")
                i += 1
            if i < n:
                filtered_lines.append("")
                i += 1
            continue
        filtered_lines.append(line)
        i += 1
    src_no_defines = "\n".join(filtered_lines)
    # Second pass: expand macro calls in the remaining source.
    # We run a SINGLE pass — nested macros inside expanded bodies
    # are handled by the suffix recursion in _expand_macros_in_line
    # and by the body splitting + per-statement expansion below.
    # Multiple passes caused { } explosion because each pass
    # re-expands already-expanded bodies.
    out_lines: list[str] = []
    src_lines = src_no_defines.splitlines()
    li = 0
    nl = len(src_lines)
    while li < nl:
        line = src_lines[li]
        stripped = line.strip()
        # Skip preprocessor directives and empty lines
        if stripped.startswith("#") or not stripped:
            out_lines.append(line)
            li += 1
            continue
        # Search for macro calls in this line.
        expanded = _expand_macros_in_line(line, macro_map)
        if expanded is not None:
            # None == no macro call.  Non-empty list == replacement
            # lines.  EMPTY list == an empty-body (no-op) macro — the
            # call disappears entirely, so nothing is emitted.
            if expanded:
                out_lines.extend(expanded)
            li += 1
            continue
        # Brace-aware multi-line call recovery: if the line contains
        # a known macro name whose argument parens do NOT balance on
        # this line (e.g. ``ENET_CONTEXT_WALK (context, { ... })``
        # spanning several source lines), join the following lines
        # until the parens balance, then expand the joined line.
        if _has_unbalanced_macro_call(line, macro_names):
            joined = line
            k = li + 1
            while k < nl and _paren_delta(joined) < 0 and k - li < 1000:
                nxt = src_lines[k]
                if nxt.rstrip().endswith("\\"):
                    nxt = nxt.rstrip()[:-1]
                joined += "\n" + nxt
                k += 1
            if _paren_delta(joined) >= 0:
                expanded = _expand_macros_in_line(joined, macro_map)
                if expanded:
                    out_lines.extend(expanded)
                    li = k
                    continue
        out_lines.append(line)
        li += 1
    return "\n".join(out_lines)


def _paren_delta(text: str) -> int:
    """Net paren count of ``text``: opens minus closes, ignoring
    string/char literals.  Negative means the text has unclosed ``(``.
    """
    delta = 0
    in_str = False
    in_char = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if in_char:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == "'":
                in_char = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "'":
            in_char = True
        elif ch == "(":
            delta += 1
        elif ch == ")":
            delta -= 1
    return delta


def _has_unbalanced_macro_call(
    line: str,
    macro_names: set[str]
) -> bool:
    """True if ``line`` contains ``NAME(`` for a known macro NAME and
    the parens opened by that call do not balance on this line
    (i.e. the call continues on following source lines)."""
    n = len(line)
    i = 0
    in_str = False
    in_char = False
    esc = False
    while i < n:
        ch = line[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if in_char:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == "'":
                in_char = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == "'":
            in_char = True
            i += 1
            continue
        for mname in macro_names:
            if line[i:i+len(mname)] == mname:
                before_ok = (i == 0 or not (
                    line[i-1].isalnum() or line[i-1] == "_"))
                j = i + len(mname)
                while j < n and line[j] in " \t":
                    j += 1
                if before_ok and j < n and line[j] == "(":
                    # Count parens from here to end of line
                    depth = 0
                    k = j
                    while k < n:
                        if line[k] == "(":
                            depth += 1
                        elif line[k] == ")":
                            depth -= 1
                        k += 1
                    if depth != 0:
                        return True
                break
        i += 1
    return False


def _expand_macros_in_line_recursive(
    line: str,
    macro_map: dict[str, tuple[list[str], str]]
) -> list[str] | None:
    """Like _expand_macros_in_line but expands macros recursively:
    if a macro's body contains calls to other macros, those are
    expanded too (up to 5 levels deep to prevent infinite loops)."""
    result = _expand_macros_in_line(line, macro_map)
    if result is None:
        return None
    # Recursively expand any macro calls in the result lines.
    final: list[str] = []
    for rline in result:
        # Don't expand the comment line
        if "/* Macro:" in rline:
            final.append(rline)
            continue
        sub = _expand_macros_in_line(rline, macro_map)
        if sub:
            final.extend(sub)
        else:
            final.append(rline)
    return final


def _expand_macros_in_line(
    line: str,
    macro_map: dict[str, tuple[list[str], str]],
    _depth: int = 0
) -> list[str] | None:
    """Find and expand macro calls in a single line.

    Returns a list of replacement lines (one or more), or ``None`` if
    no macro call was found (the caller should keep the original line).

    EMISSION MODEL — scope-unit, brace-aware (no prefix repetition):

    A macro call is replaced by a ``{ ... }`` scope block that is
    brace-balanced ON ITS OWN, so the surrounding code's brace depth
    is untouched:

        prefix MACRO(args);      ->      prefix{
                                         /* Macro: MACRO(args) */
                                         stmt1;
                                         ...
                                     }

    The call-site prefix (e.g. ``if (x) ``) is attached ONLY to the
    opening-brace line, never repeated per body line — repeating it
    used to open one unclosed ``{`` per line and destroy the brace
    balance.  Because the whole block becomes the if/for/while body,
    ``if (x) MACRO(...);`` correctly guards EVERY statement of the
    macro body (this was broken when the outer braces were stripped).

    The statement terminator ``;`` that follows the call is absorbed
    by the block (a block is already a complete statement), so
    ``if (x) MACRO(); else ...`` stays valid C.

    Nested macro calls inside the body statements are expanded FIRST
    (per statement, recursively) and their multi-line blocks are
    spliced in before the outer block is emitted — inner-first
    semantics, matching the C preprocessor's rescan behaviour.
    """
    if _depth > 20:
        # Safety guard against self-recursive macros — leave the text
        # untouched rather than looping forever.
        return None
    # Scan the line for macro calls, skipping string/char literals.
    n = len(line)
    i = 0
    in_str = False
    in_char = False
    escape = False
    while i < n:
        ch = line[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if in_char:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "'":
                in_char = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == "'":
            in_char = True
            i += 1
            continue
        # Check if this position starts a macro call
        for mname in macro_map:
            if line[i:i+len(mname)] == mname:
                # Check word boundary
                before_ok = (i == 0 or not (line[i-1].isalnum() or line[i-1] == "_"))
                after_pos = i + len(mname)
                # Skip whitespace
                j = after_pos
                while j < n and line[j] in " \t":
                    j += 1
                # Get macro params and body
                params, body = macro_map[mname]
                if not before_ok:
                    i += 1
                    continue
                args_str = ""
                args: list[str] = []
                if params:
                    # Function-like macro: the name must be followed
                    # by ``(args)``.
                    if j >= n or line[j] != "(":
                        i += 1
                        continue
                    # Found a macro call: MACRO_NAME( at position i
                    # Extract the argument list (balanced parens)
                    paren_start = j
                    depth = 1
                    j += 1
                    while j < n and depth > 0:
                        if line[j] == "(":
                            depth += 1
                        elif line[j] == ")":
                            depth -= 1
                        j += 1
                    if depth != 0:
                        i += 1
                        continue
                    args_str = line[paren_start+1:j-1]
                    # Split args on top-level commas (brace-aware — an
                    # argument may itself be a ``{ ... }`` code block).
                    args = _split_top_level(args_str, ",")
                    args = [a.strip() for a in args if a.strip()]
                else:
                    # Object-like macro (no parameter list): the call
                    # site has NO parens (``ENET_RANGE_CODER_SEED;``).
                    # Only require that the name is not a prefix of a
                    # longer identifier.
                    if j < n and (line[j].isalnum() or line[j] == "_"):
                        i += 1
                        continue
                # Substitute params with args in body
                expanded_body = body
                for pi, param in enumerate(params):
                    if pi < len(args):
                        # Simple text substitution.  We use word
                        # boundaries to avoid partial matches (e.g.
                        # ``value_`` should not match inside
                        # ``value_extra``).
                        expanded_body = re.sub(
                            r'\b' + re.escape(param) + r'\b',
                            args[pi],
                            expanded_body
                        )
                # Normalize spaced operators
                expanded_body = _normalise_spaced_operators(expanded_body)
                # Strip redundant parens around simple identifiers in
                # member-access positions: macro bodies write
                # ``(context) -> field`` defensively, and after
                # substitution that becomes ``(root) -> field`` which
                # the pattern translator's assignment regex cannot
                # match.  ``(root) -> field`` -> ``root -> field``.
                # (Only member access is touched — function calls and
                # casts keep their parens.)
                expanded_body = re.sub(
                    r'\(\s*(\w+)\s*\)(\s*(?:->|\.))',
                    r'\1\2',
                    expanded_body
                )
                # ---- Scope-unit emission ----
                inner = expanded_body.strip()
                if not inner:
                    # Empty-body macro (a no-op like enet's
                    # ``ENET_CONTEXT_NOT_EXCLUDED``).
                    prefix_txt = line[:i]
                    suffix_txt = line[j:]
                    if not prefix_txt.strip() and not (
                        suffix_txt.strip().rstrip(";").strip()
                    ):
                        # The call IS the whole statement
                        # (``NOOP(...);``) — it vanishes entirely.
                        # An EMPTY list (not None) tells the caller
                        # to drop the call text.
                        return []
                    # The call sits in the MIDDLE of the line —
                    # removing only the call must PRESERVE the rest
                    # (``} else { ...; NOOP(...); if ...`` — dropping
                    # the whole line here used to delete the entire
                    # else-branch!).  Re-join prefix + suffix (minus
                    # the absorbed ``;``) and keep expanding.
                    rest = prefix_txt + suffix_txt.lstrip()
                    if rest.lstrip().startswith(";"):
                        rest = rest.lstrip()[1:]
                    rest_expanded = _expand_macros_in_line(
                        rest, macro_map, _depth + 1
                    )
                    if rest_expanded is None:
                        return [rest]
                    return rest_expanded
                # If the body is a simple ``{ ... }`` block, drop the
                # outer braces here — we emit our own braces below.
                # (Braces are NEVER lost: the block we emit restores
                # them.  Body braces stay balanced overall.)
                if inner.startswith("{") and inner.endswith("}"):
                    depth_check = 0
                    wraps_all = True
                    for ci, ch in enumerate(inner):
                        if ch == "{":
                            depth_check += 1
                        elif ch == "}":
                            depth_check -= 1
                            if depth_check == 0 and ci < len(inner) - 1:
                                wraps_all = False
                                break
                    if wraps_all:
                        inner = inner[1:-1].strip()
                prefix = line[:i]
                suffix = line[j:]
                stmts = _split_top_level_brace_aware(inner, ";")
                stmts = [s.strip() for s in stmts if s.strip()]
                if not stmts:
                    # Empty body — leave the call untouched.
                    i = j
                    continue
                result_lines = []
                # Opening line: call-site prefix + ``{``.  For
                # ``if (cond) MACRO(...)`` the block becomes the
                # if-body; for a plain statement the prefix is just
                # leading whitespace/empty.
                result_lines.append(f"{prefix}{{")
                # Marker comment on its own line inside the block.
                param_list = ", ".join(params) if params else ""
                if param_list:
                    result_lines.append(
                        f"/* Macro: {mname}({param_list}) */"
                    )
                else:
                    result_lines.append(f"/* Macro: {mname} */")
                # Body statements — expand nested macro calls FIRST
                # (inner-first), then splice their multi-line scope
                # blocks in.  The call-site prefix is NOT repeated on
                # these lines: they live inside the block opened
                # above, so their brace depth is self-contained.
                for s in stmts:
                    stmt_expanded = _expand_macros_in_line(
                        s, macro_map, _depth + 1
                    )
                    if stmt_expanded is None:
                        # No macro in this statement — emit as-is.
                        if s.endswith("{") or s.endswith("}"):
                            result_lines.append(s)
                        else:
                            result_lines.append(f"{s};")
                    else:
                        # Nested expansion result (may be [] for a
                        # no-op macro — splices nothing).
                        result_lines.extend(stmt_expanded)
                # Closing brace of the scope block.
                result_lines.append("}")
                # Suffix — the ``;`` that terminated the macro call is
                # absorbed by the block (a block is a complete
                # statement), so drop exactly ONE leading ``;``.
                # Anything after it is emitted line by line, with
                # nested macro calls expanded, keeping braces
                # balanced across the emitted lines.
                suf = suffix.lstrip()
                if suf.startswith(";"):
                    suf = suf[1:]
                if suf.strip():
                    suffix_parts = _split_top_level_brace_aware(suf, ";")
                    for sp in suffix_parts:
                        sp = sp.strip()
                        if not sp:
                            continue
                        suf_expanded = _expand_macros_in_line(
                            sp, macro_map, _depth + 1
                        )
                        if suf_expanded is None:
                            if sp.endswith("{") or sp.endswith("}"):
                                result_lines.append(sp)
                            else:
                                result_lines.append(f"{sp};")
                        else:
                            result_lines.extend(suf_expanded)
                return result_lines
        i += 1
    return None


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
    # POSIX / libc time types
    "time_t", "clock_t", "useconds_t", "pid_t", "uid_t", "gid_t",
    "mode_t", "off_t", "dev_t", "ino_t", "nlink_t", "blkcnt_t",
    "blksize_t",
    # Windows
    "DWORD", "WORD", "BYTE", "BOOL", "HANDLE", "HWND", "HMODULE",
    "HINSTANCE", "HDC", "LRESULT", "WPARAM", "LPARAM", "UINT",
    "LPSTR", "LPCTSTR", "LPCSTR", "LPVOID", "PBYTE", "PDWORD",
    "PWORD", "HBRUSH", "HMENU", "WCHAR",
    "INT", "LONG", "SHORT", "ULONG", "USHORT", "BOOLEAN", "PVOID",
    "LPCWSTR", "LPWSTR", "LPCWCHAR", "SOCKET", "u_long", "u_short",
    "u_int", "u_char",
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
    "OVERLAPPED", "LPOVERLAPPED",
    "HDEVINFO", "PCSTR", "PCWSTR", "PULONG",
    "WSADATA", "LPWSADATA", "WSABUF", "LPWSABUF",
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
    (?:static\s+|extern\s+|inline\s+)*  # optional storage
    # Optional linkage-marker macros before the function. The pattern
    # translator's source path does NOT go through pcpp, so any
    # export/deprecated marker that the user's source used (e.g.
    # G_MODULE_EXPORT, GDK_AVAILABLE_IN_ALL, _GLIB_EXTERN,
    # SDL_FORCE_INLINE, ...) survives into src_clean and would block
    # the function-definition regex. We allow an arbitrary number
    # of ALL_UPPERCASE or _Init uppercase identifiers followed by
    # whitespace, then the real storage class.
    (?:[A-Z_][A-Z0-9_]*\s+)*
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
    """Split a string at top-level separator (respecting parens/brackets
    AND C string/char literals).

    Skipping string literals matters because a string can contain
    characters that look like the separator or like ``(`` / ``)``
    (e.g. ``sprintf(temp, "STATUS=0x%02X (", s)`` has a ``(`` inside
    the string that would otherwise bump the depth counter and
    swallow the closing ``)`` of the call).
    """
    parts: list[str] = []
    depth = 0
    cur = []
    in_str = False
    in_char = False
    str_escape = False
    for ch in s:
        if in_str:
            cur.append(ch)
            if str_escape:
                str_escape = False
            elif ch == "\\":
                str_escape = True
            elif ch == '"':
                in_str = False
            continue
        if in_char:
            cur.append(ch)
            if str_escape:
                str_escape = False
            elif ch == "\\":
                str_escape = True
            elif ch == "'":
                in_char = False
            continue
        if ch == '"':
            in_str = True
            cur.append(ch)
        elif ch == "'":
            in_char = True
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            cur.append(ch)
        elif ch == sep and depth == 0:
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

    Like ``_split_top_level`` we respect C string/char literals so a
    ``;`` inside a string literal in the init/cond/step expression
    is not mistaken for a slot boundary.
    """
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    in_str = False
    in_char = False
    str_escape = False
    for ch in s:
        if in_str:
            cur.append(ch)
            if str_escape:
                str_escape = False
            elif ch == "\\":
                str_escape = True
            elif ch == '"':
                in_str = False
            continue
        if in_char:
            cur.append(ch)
            if str_escape:
                str_escape = False
            elif ch == "\\":
                str_escape = True
            elif ch == "'":
                in_char = False
            continue
        if ch == '"':
            in_str = True
            cur.append(ch)
        elif ch == "'":
            in_char = True
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            cur.append(ch)
        elif ch == ";" and depth == 0:
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
    init_text: str = ""  # raw initializer text (e.g. ``{ "..." }``),
                         # recovered from the original source after
                         # the brace-stripping pass.


def extract_globals(residual: str) -> list[CGlobal]:
    """Find top-level variable declarations (after functions removed).

    Note: ``extern`` declarations are skipped — in Pascal, external
    variables are accessed via the ``uses`` clause (the unit that
    declares them in its interface section). Including ``extern int x;``
    as a local var would either duplicate the declaration or produce
    an invalid Pascal type like ``Textern int``.
    """
    globals_list: list[CGlobal] = []
    # Strip block contents that survived (struct/enum bodies).
    # We keep the ORIGINAL residual so we can recover initializers
    # that the brace-stripping pass would lose. The ``init_text``
    # field of each CGlobal is set to the raw initializer text
    # (e.g. ``{ "..." }``) by scanning the original residual for
    # ``varname = { ... }`` AFTER the regex match.
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
        # Recover the raw initializer text from the original residual
        # for each declared variable. We look for ``varname = {...};``
        # in the residual (the original, NOT the cleaned version).
        # This is needed because the brace-stripping pass removed
        # the ``{ ... }`` initializer from the cleaned text, so the
        # regex above sees ``varname = ;`` instead of
        # ``varname = { "..." };``. We use a brace-matching scan
        # (not a regex) because the initializer can contain nested
        # braces (e.g. for nested struct initializers).
        init_text = ""
        for name in names:
            base_name = re.match(r"(\w+)", name)
            if not base_name:
                continue
            var_name = base_name.group(1)
            # Find ``var_name`` followed by ``= {`` in the residual.
            # We use a simple regex with a lookahead for ``\s*=\s*{``.
            im = re.search(
                r'\b' + re.escape(var_name) + r'\s*=\s*\{',
                residual,
            )
            if not im:
                continue
            # Walk forward from ``{`` to find the matching ``}``.
            depth = 0
            i = im.end() - 1  # position of the opening ``{``
            j = i
            while j < len(residual):
                if residual[j] == '{':
                    depth += 1
                elif residual[j] == '}':
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j < len(residual):
                init_text = residual[im.end() - 1:j + 1]
                break  # only one initializer per global; use the first
        globals_list.append(CGlobal(type=type_str, names=names,
                                    raw=m.group(0), init_text=init_text))
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
        # Names declared via ``typedef`` earlier in this function body.
        # Used to distinguish *real* local typedefs (e.g.
        # ``typedef void (__stdcall*GETHIDGUID)(...);`` followed by
        # ``GETHIDGUID HidD_GetHidGuid=0;``) from ordinary ALL-CAPS
        # Windows types like ``DWORD``, ``HMODULE``, ``BOOL`` etc.
        # that are *already* in ``_KNOWN_TYPEDEFS`` and should be
        # emitted as normal local variable declarations, not as
        # "local typedef" comments.
        self._local_typedef_names: set[str] = set()

    def _pascalify(self, name: str) -> str:
        """Like pascalify_name, but also records the C→Pascal name
        mapping so references in expressions can be updated."""
        pas = pascalify_name(name)
        if pas != name:
            self._name_map[name] = pas
        return pas

    def _split_compound_block(self, src: str) -> list[str]:
        """Split the contents of a ``{ ... }`` compound block into
        statements, splitting on ``;`` at brace depth 0 (inside the
        compound block but NOT inside nested ``{ }`` blocks from
        if/for/while).

        This is used for expanded C macros where the outer ``{ }``
        wraps a sequence of statements.  We must NOT split inside
        ``if (x) { stmt1; stmt2; }`` because that would break the
        control flow structure.
        """
        out: list[str] = []
        paren_depth = 0
        brace_depth = 0
        in_str = False
        in_char = False
        str_escape = False
        cur: list[str] = []
        n = len(src)
        for idx, ch in enumerate(src):
            if in_str:
                cur.append(ch)
                if str_escape:
                    str_escape = False
                elif ch == "\\":
                    str_escape = True
                elif ch == '"':
                    in_str = False
                continue
            if in_char:
                cur.append(ch)
                if str_escape:
                    str_escape = False
                elif ch == "\\":
                    str_escape = True
                elif ch == "'":
                    in_char = False
                continue
            if ch == '"':
                in_str = True
                cur.append(ch)
                continue
            if ch == "'":
                in_char = True
                cur.append(ch)
                continue
            if ch == "(":
                paren_depth += 1
                cur.append(ch)
                continue
            if ch == ")":
                if paren_depth > 0:
                    paren_depth -= 1
                cur.append(ch)
                continue
            if ch == "{":
                brace_depth += 1
                cur.append(ch)
                continue
            if ch == "}":
                if brace_depth > 0:
                    brace_depth -= 1
                cur.append(ch)
                if brace_depth == 0 and paren_depth == 0:
                    # A nested compound statement completed — it is a
                    # statement boundary (unless an ``else`` /
                    # ``while`` continues it, e.g. if/else chains or
                    # do-while tails).
                    after = src[idx+1:].lstrip()
                    if not (after.startswith("else")
                            or after.startswith("while")):
                        stmt = "".join(cur).strip()
                        if stmt:
                            out.append(stmt)
                        cur = []
                continue
            if ch == ";" and paren_depth == 0 and brace_depth == 0:
                stmt = "".join(cur).strip()
                if stmt:
                    out.append(stmt)
                cur = []
                continue
            if ch == "\n":
                cur.append(" ")
            else:
                cur.append(ch)
        if cur:
            tail = "".join(cur).strip()
            if tail:
                out.append(tail)
        return out

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
          * ';' inside C string/char literals is NOT a separator
            (so ``sprintf(buf, "a;b");`` is treated as one statement,
            not split on the ``;`` inside the string).
          * Parens/brackets inside C string/char literals are NOT counted
            (so ``sprintf(buf, "abc(def)", x);`` has the ``(`` and ``)``
            inside the string ignored by the depth tracker, and the
            statement splits correctly on the trailing ``;``).
        """
        out: list[str] = []
        depth = 0          # brace depth {}
        paren_depth = 0    # paren depth ()
        in_str = False     # inside "..."
        in_char = False    # inside '...'
        str_escape = False
        cur: list[str] = []
        i = 0
        n = len(src)
        while i < n:
            ch = src[i]
            # ---- String/char literal handling ----
            # We must track these so a ``;`` or ``(`/``)`` inside a
            # literal does not influence the depth counters.
            if in_str:
                cur.append(ch)
                if str_escape:
                    str_escape = False
                elif ch == "\\":
                    str_escape = True
                elif ch == '"':
                    in_str = False
                i += 1
                continue
            if in_char:
                cur.append(ch)
                if str_escape:
                    str_escape = False
                elif ch == "\\":
                    str_escape = True
                elif ch == "'":
                    in_char = False
                i += 1
                continue
            if ch == '"':
                in_str = True
                cur.append(ch)
                i += 1
                continue
            if ch == "'":
                in_char = True
                cur.append(ch)
                i += 1
                continue
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
                if depth == 0 and paren_depth == 0:
                    # A compound statement just completed at the top
                    # level of the function body — the statement ENDS
                    # here per C grammar (``{ ... }`` is a complete
                    # statement).  Without this, an expanded macro
                    # scope block, the following ``for`` loop and any
                    # later blocks all merge into one mega statement
                    # that the translator can't parse.
                    # Exception: ``} else ...`` and ``} while ...``
                    # continue the current statement.
                    # Exception: ``} NAME, *PNAME;`` — this is the
                    # closing brace of a ``typedef struct { ... } NAME;``
                    # The NAME and ``;`` are PART of the typedef statement,
                    # so we must NOT split here.
                    after = src[i+1:].lstrip()
                    if not (after.startswith("else")
                            or after.startswith("while")
                            or re.match(r'\w+\s*[,;]', after)
                            or re.match(r'\*\w+\s*[,;]', after)):
                        out.append("".join(cur))
                        cur = []
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
        # --- Macro-expansion marker comment ---
        # ``/* Macro: NAME(args) */`` lines emitted by the macro
        # expansion are converted to Pascal ``// Macro: ...`` comments
        # so the user can see where each macro call was expanded.  The
        # marker may be a statement of its own or prefix the first
        # statement of the expanded body.
        m_macro = re.match(
            r"^/\*\s*Macro:\s*(.*?)\s*\*/\s*(.*)$", s, re.DOTALL
        )
        if m_macro:
            self._emit(f"{pad}// Macro: {m_macro.group(1)}")
            rest = m_macro.group(2).strip()
            if rest:
                self._translate_statement(rest)
            return
        # --- Bare scope braces ---
        # A ``{`` / ``}`` on its own (from an expanded macro scope
        # block) maps to a Pascal ``begin`` / ``end;`` pair.
        if s == "{":
            self.indent += 1
            self._emit(f"{pad}begin")
            return
        if s == "}":
            self.indent -= 1
            self._emit(f"{self._pad()}end;")
            return
        # Normalise spaced multi-char operators (e.g. ``peer - > field``)
        # to their unspaced forms so the downstream _OP_REPLACE table can
        # match them.  This must happen at the very top of
        # _translate_statement (before any pattern matching) because
        # ``peer - > field`` would otherwise confuse the comparison /
        # assignment / call-statement pattern matchers.  We skip
        # control-flow keywords (if/for/while/do/switch/return) which
        # are handled by their own translators — those translators
        # call _translate_expr internally, and _translate_expr already
        # normalises its input.  We only normalise here for the
        # expression-statement paths (assignments, calls, etc.).
        # We DO skip string/char literals so the spacing inside a
        # string is preserved verbatim.
        if not (s.startswith("if") or s.startswith("for")
                or s.startswith("while") or s.startswith("do")
                or s.startswith("switch") or s.startswith("return")
                or s.startswith("break") or s.startswith("continue")
                or s.startswith("goto") or s.startswith("{")
                or s.startswith("case") or s.startswith("default")
                or s.startswith("label")):
            s = _normalise_spaced_operators(s)

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
        if s.startswith("do") and (s[2:3] in (" ", "\t", "\n", "{") or len(s) == 2):
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
        # --- Compound block ``{ ... }`` ---
        # C treats ``{ stmt1; stmt2; }`` as a single compound statement.
        # When the splitter (or the switch/case handler that appends a
        # trailing ``;`` for non-compound case bodies) hands us a
        # statement like ``{ ... };`` we must strip the trailing ``;``
        # *before* checking whether the statement is a compound block.
        # Otherwise a multi-statement case body such as
        #   ``case SDT_STRING: { char *nameA, *nameB; ... break; }``
        # would fail the ``endswith("}")`` check (because it ends with
        # ``};``) and fall through to the TODO emitter — losing every
        # statement inside the case body, including the local
        # ``char *nameA, *nameB;`` multi-pointer declaration.
        s_compound = s
        if s_compound.endswith(";"):
            # Strip trailing ``;`` for the compound-block check below.
            # ``{ ... };`` -> ``{ ... }``. We keep the original ``s``
            # untouched for the rest of the function.
            s_compound = s_compound[:-1].rstrip()
        if s_compound.startswith("{") and s_compound.endswith("}"):
            # Compound block: split the inner content into statements
            # and recurse on each.  We use a dedicated splitter that
            # splits on ``;`` even inside ``{ }`` blocks (unlike
            # ``_split_statements`` which only splits at brace depth 0).
            # This is necessary because C preprocessor macros that use
            # the ``do { stmt1; stmt2; } while(0)`` idiom (or plain
            # ``{ stmt1; stmt2; }`` blocks) expand into a single
            # compound block that ``_split_statements`` treats as one
            # statement.  By splitting inside the compound block we
            # can translate each statement individually.
            inner = s_compound[1:-1]
            for sub in self._split_compound_block(inner):
                if sub.strip():
                    self._translate_statement(sub.strip())
            return

        # --- Local typedef declarations ---
        # C allows typedef inside function bodies (local typedefs).
        # These create local type aliases. In Pascal, there's no exact
        # equivalent, but we can emit them as comments so the user
        # can add them by hand if needed. We handle:
        #   1. typedef struct { ... } NAME, *PNAME;
        #   2. typedef RET_TYPE (*NAME)(params);
        #   3. typedef RET_TYPE (CALLBACK *NAME)(params);
        #   4. } NAME, *PNAME; (closing brace of typedef struct)
        #   5. TYPE_NAME var = 0; (local var using a local typedef)
        if s.startswith("typedef"):
            # typedef struct/union { ... } NAME;
            m_td_struct = re.match(
                r"^typedef\s+(?:struct|union)\s+\w*\s*\{.*?\}"
                r"\s+([A-Za-z_]\w*(?:\s*,\s*\*[A-Za-z_]\w*)?)\s*;?\s*$",
                s, re.DOTALL,
            )
            if m_td_struct:
                names = m_td_struct.group(1)
                # Record the declared typedef names so subsequent
                # ``NAME var = init;`` statements can be recognised
                # as using a *local* typedef (and emitted as comment)
                # rather than being mistaken for an ALL-CAPS Windows
                # type that happens to be in ``_KNOWN_TYPEDEFS``.
                # Strip leading ``*`` from each name (the pointer
                # alias form ``*PNAME`` declares both ``NAME`` and
                # ``PNAME``).
                for nm in re.findall(r"\*?([A-Za-z_]\w*)", names):
                    if nm:
                        self._local_typedef_names.add(nm)
                self._emit(f"{pad}// local typedef: {names}")
                return
            # typedef RET (CALLBACK *NAME)(params);
            m_td_fptr = re.match(
                r"^typedef\s+.*?\(.*?\*\s*(\w+)\s*\)\s*\(.*?\)\s*;?\s*$",
                s, re.DOTALL,
            )
            if m_td_fptr:
                name = m_td_fptr.group(1)
                self._local_typedef_names.add(name)
                self._emit(f"{pad}// local typedef: {name}")
                return
            # Generic typedef — extract any identifier that looks
            # like a typedef name (last identifier before the trailing
            # ``;``). This catches less-common forms such as
            # ``typedef enum { ... } NAME;`` or ``typedef int NAME;``.
            m_td_simple = re.search(
                r"\b([A-Za-z_]\w*)\s*;?\s*$", s,
            )
            if m_td_simple:
                self._local_typedef_names.add(m_td_simple.group(1))
            self._emit(f"{pad}// local typedef: {s[:100]}")
            return
        # } NAME, *PNAME; (closing of typedef struct)
        if re.match(r"^\}\s+\w+(?:\s*,\s*\*\w+)?\s*;?\s*$", s):
            # Extract the NAME(s) from ``} NAME, *PNAME;`` so subsequent
            # variable declarations using these names are recognised.
            for nm in re.findall(r"\*?([A-Za-z_]\w*)", s):
                if nm and nm not in ("struct", "union", "enum"):
                    self._local_typedef_names.add(nm)
            self._emit(f"{pad}// typedef closing brace")
            return

        # --- Local variable declarations using local typedefs ---
        # After local typedefs like ``typedef void (__stdcall*GETHIDGUID)(...);``
        # the code declares variables like ``GETHIDGUID HidD_GetHidGuid=0;``
        # These are local type names that are NOT in _KNOWN_TYPEDEFS. We
        # detect them by checking if the first word is ALL_CAPS (a
        # convention for Windows API function-pointer typedefs) and is
        # NOT already a known global typedef.  Names that ARE in
        # ``_KNOWN_TYPEDEFS`` (e.g. ``DWORD``, ``HMODULE``, ``BOOL``,
        # ``UINT``, ``HANDLE``, ``HWND``, ``LPSTR`` ...) must fall
        # through to the regular local-variable handler below so the
        # user gets a real ``var x: DWORD;`` declaration instead of a
        # dropped comment.
        m_ltd = re.match(r"^([A-Z][A-Z_0-9]+)\s+(\w+)\s*=", s)
        if m_ltd:
            td_name = m_ltd.group(1)
            # Only treat as a local-typedef-based declaration when the
            # name was actually declared via ``typedef`` earlier in
            # this function body — OR it's an ALL-CAPS name we've never
            # heard of (so it likely IS a local Windows function-pointer
            # typedef we failed to capture, e.g. one declared in a
            # sibling branch that the pattern walker didn't reach).
            # In both cases fall through if the name is a known global
            # typedef or C keyword; those are real types, not local
            # typedefs.
            is_known = (
                td_name in _KNOWN_TYPEDEFS
                or td_name in _C_TYPE_KEYWORDS
                or td_name in self._extra_known_types
            )
            if not is_known:
                self._emit(f"{pad}// local var (local typedef): {s[:100]}")
                return
            # Otherwise fall through to the regular local-variable
            # declaration handler below.

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
            # ``looks_like_assignment`` is True when type_str had only
            # one word and rest starts with ``=`` — in that case the
            # statement is an *assignment* (e.g.
            # ``MyDeviceDetected = TRUE;``), not a declaration, and we
            # must NOT let the "looks-like-a-typedef" heuristic below
            # misfire on it.
            looks_like_assignment = False
            if rest.startswith("="):
                parts = type_str.rsplit(None, 1)
                if len(parts) == 2:
                    var_name = parts[1]
                    type_str = parts[0]
                    rest = var_name + " " + rest
                else:
                    # type_str has only one word and rest starts with
                    # ``=`` — this is NOT a declaration (e.g.
                    # ``MyDeviceDetected = TRUE;`` is an assignment to
                    # an already-declared variable, not a declaration
                    # of a variable named ``= TRUE`` with type
                    # ``MyDeviceDetected``).  Don't treat it as a
                    # declaration; let the assignment handler below
                    # process it as ``MyDeviceDetected := True;``.
                    # Without this guard, the heuristic below would
                    # match mixed-case ``MyDeviceDetected`` as a
                    # "looks-like-a-typedef" type and emit a
                    # nonsensical ``__TRUE: TMyDeviceDetected;`` var
                    # declaration.
                    looks_like_assignment = True
            # Check if the first word is a C type keyword or known typedef.
            # Strip trailing ``*`` (pointer) so that ``GtkTextTag*`` is
            # recognised as the typedef ``GtkTextTag``.
            first_word = type_str.split()[0] if type_str.split() else ""
            first_word_stripped = first_word.rstrip("*")
            if not looks_like_assignment and (
                    first_word in _C_TYPE_KEYWORDS
                    or first_word in _KNOWN_TYPEDEFS
                    or first_word_stripped in _C_TYPE_KEYWORDS
                    or first_word_stripped in _KNOWN_TYPEDEFS
                    or first_word in self._extra_known_types
                    or first_word_stripped in self._extra_known_types
                    or first_word_stripped in self._local_typedef_names):
                self._translate_local_decl(type_str, rest)
                return
            # Heuristic fallback: many C libraries use typedef names that
            # follow predictable naming conventions but are NOT in our
            # fixed ``_KNOWN_TYPEDEFS`` set (e.g. enet's ``ENetRangeCoder``,
            # ``ENetSymbol``, ``enet_uint8``, ``enet_uint16``; or the user's
            # own ``MyStruct``, ``MyType``, ``device_handle_t``). If the
            # first word "looks like a type" we treat the statement as a
            # local declaration. This is conservative — it never misfires
            # on common expression statements like ``sum += i`` (``sum``
            # is all-lowercase and short) because such names start with a
            # lowercase letter and do not look like typedefs.
            #
            # The same heuristic is already used by ``_extract_prototypes``
            # for parameter type detection, so this keeps the two paths
            # consistent.
            if not looks_like_assignment and first_word_stripped and (
                # Mixed-case typedef name: starts with uppercase, contains
                # at least one lowercase letter (e.g. ``GtkWidget``,
                # ``ENetSymbol``, ``GtkWindow``).
                (first_word_stripped[0].isupper()
                 and not first_word_stripped.isupper()
                 and any(c.islower() for c in first_word_stripped))
                # ``_t`` suffix (POSIX typedef convention):
                # ``time_t``, ``pid_t``, ``enet_uint16_t``, etc.
                or first_word_stripped.endswith("_t")
                # Numeric-width typedef convention:
                # ``enet_uint8``, ``g_uint16``, ``my_int32``, etc.
                or first_word_stripped.endswith(
                    ("_uint8", "_uint16", "_uint32", "_uint64",
                     "_int8", "_int16", "_int32", "_int64"))
            ):
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
        # Dereference + increment/decrement: ``(*err)++`` → ``Inc(err^)``
        m_deref_inc = re.match(r"^\(\s*\*\s*(\w+)\s*\)\s*\+\+\s*;?$", s)
        if m_deref_inc:
            self._emit(f"{pad}Inc({m_deref_inc.group(1)}^);")
            return
        m_deref_dec = re.match(r"^\(\s*\*\s*(\w+)\s*\)\s*--\s*;?$", s)
        if m_deref_dec:
            self._emit(f"{pad}Dec({m_deref_dec.group(1)}^);")
            return
        # Pointer member access: ``packet->ref--`` → ``Dec(packet^.ref)``
        m_ptr_inc = re.match(
            r"^([\w\->\.\[\]\s]+?)\s*\+\+\s*;?$", s)
        if m_ptr_inc:
            target = m_ptr_inc.group(1).strip()
            pas_target = self._translate_expr(target)
            self._emit(f"{pad}Inc({pas_target});")
            return
        m_ptr_dec = re.match(
            r"^([\w\->\.\[\]\s]+?)\s*--\s*;?$", s)
        if m_ptr_dec:
            target = m_ptr_dec.group(1).strip()
            pas_target = self._translate_expr(target)
            self._emit(f"{pad}Dec({pas_target});")
            return
        m_ptr_pre_inc = re.match(
            r"^\+\+\s*([\w\->\.\[\]\s]+?)\s*;?$", s)
        if m_ptr_pre_inc:
            target = m_ptr_pre_inc.group(1).strip()
            pas_target = self._translate_expr(target)
            self._emit(f"{pad}Inc({pas_target});")
            return
        m_ptr_pre_dec = re.match(
            r"^--\s*([\w\->\.\[\]\s]+?)\s*;?$", s)
        if m_ptr_pre_dec:
            target = m_ptr_pre_dec.group(1).strip()
            pas_target = self._translate_expr(target)
            self._emit(f"{pad}Dec({pas_target});")
            return
        # Function-pointer call: ``(* ptr->member)(args)`` → ``ptr^.member^(args)``
        m_funcptr = re.match(r"^\(\s*\*\s*([\w\->\.\[\]\s]+?)\s*\)\s*\((.*)\)\s*;?\s*$", s, re.S)
        if m_funcptr:
            func_expr = m_funcptr.group(1).strip()
            args = m_funcptr.group(2)
            pas_func = self._translate_expr(func_expr)
            pas_args = self._translate_args(args)
            self._emit(f"{pad}{pas_func}^({pas_args});")
            return
        # Cast + function-pointer call: ``((CAST)(EXPR))(ARGS);``
        m_cast_call = re.match(
            r"^(.*?)\(([\w\->\.\[\]\s]+?)\)\s*\)\s*\((.*)\)\s*;?\s*$",
            s, re.S)
        if m_cast_call:
            func_expr = m_cast_call.group(2).strip()
            args = m_cast_call.group(3)
            pas_func = self._translate_expr(func_expr)
            pas_args = self._translate_args(args)
            self._emit(f"{pad}{pas_func}^({pas_args});")
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
                    self._emit_for_step(step)
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
                    self._emit_for_step(step)
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
                        self._emit_for_step(step)
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
                        self._emit_for_step(step)
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
                            self._emit_for_step(step)
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

        # ---- Fallback: while loop ----
        # For non-standard for loops that don't match any of the
        # patterns above, we emit a Pascal while loop that faithfully
        # reproduces the C semantics:
        #   1. Translate ``init`` as a statement BEFORE the while
        #      (this is where the C ``init`` clause runs, exactly once).
        #   2. Translate ``test`` as the while condition.
        #   3. Translate the body inside the while loop.
        #   4. Translate ``step`` as the LAST statement inside the
        #      while body (this is where the C ``step`` clause runs,
        #      after each iteration).
        # We no longer emit a TODO comment for this — the conversion
        # is semantically correct even though it uses a while loop
        # instead of a Pascal for-to-do loop.
        if init:
            # Emit the init clause as a statement before the while.
            # The init may be a simple assignment (``i = 0``) or a
            # declaration (``int i = 0``).  We translate it as a
            # statement so the pattern-based translator handles both
            # forms.
            self._translate_statement(init + ";")
        cond_pas = self._translate_expr(test) if test else "True"
        self._emit(f"{pad}while {cond_pas} do")
        body, tail = self._extract_block(after.lstrip())
        self._emit(f"{pad}begin")
        self.indent += 1
        for sub in self._split_statements(body):
            if sub.strip():
                self._translate_statement(sub.strip())
        if step:
            # Emit the step clause as the last statement in the
            # while body.  This is where C runs the step clause —
            # after each iteration of the body.
            self._emit_for_step(step)
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
                self._emit_for_step(step)
        self.indent -= 1
        self._emit(f"{pad}end;")

    # ------------------------------------------------------------------
    def _emit_for_step(self, step: str) -> None:
        """Emit a C for-loop ``step`` clause as one or more Pascal statements.

        C's for-loop step is a comma-separated expression list (e.g.
        ``i++, j++`` or ``i = i+2, k--``). Pascal has no equivalent
        comma operator, so we split the step on top-level commas and
        translate each expression as its own statement.

        Each piece can be:
          * a simple increment/decrement (``i++``, ``--j``) → ``Inc(i)`` / ``Dec(j)``
          * an augmented assignment (``i += 2``) → ``Inc(i, 2)``
          * any other assignment expression → translate as a statement
        """
        if not step or not step.strip():
            return
        # Split on top-level commas (respecting parens/brackets/strings)
        pieces = _split_top_level(step, ",")
        for piece in pieces:
            piece = piece.strip()
            if not piece:
                continue
            pad = self._pad()
            # Try simple increment / decrement
            m_inc = re.match(r"^(?:\+\+)?(\w+)\s*\+\+\s*$", piece) or \
                    re.match(r"^\+\+(\w+)\s*$", piece)
            m_dec = re.match(r"^(\w+)\s*--\s*$", piece) or \
                    re.match(r"^--(\w+)\s*$", piece)
            m_inc_k = re.match(r"^(\w+)\s*\+=\s*(.+)$", piece)
            m_dec_k = re.match(r"^(\w+)\s*-=\s*(.+)$", piece)
            if m_inc:
                self._emit(f"{pad}Inc({m_inc.group(1)});")
            elif m_dec:
                self._emit(f"{pad}Dec({m_dec.group(1)});")
            elif m_inc_k:
                self._emit(f"{pad}Inc({m_inc_k.group(1)}, "
                           f"{self._translate_expr(m_inc_k.group(2).strip())});")
            elif m_dec_k:
                self._emit(f"{pad}Dec({m_dec_k.group(1)}, "
                           f"{self._translate_expr(m_dec_k.group(2).strip())});")
            else:
                # Fall back to general statement translation. This
                # handles regular assignments (``i = i + 2``), function
                # calls, etc.
                self._translate_statement(piece + ";")

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
        m = re.match(r"\s*while\s*\(", tail, re.DOTALL)
        if not m:
            self._emit_todo(s, "do-while without while")
            return
        # Extract the parenthesized condition with proper nesting
        # (a non-greedy `(.*?)` would stop at the first `)` inside
        # conditions like ``(a == 1) && (b == 2)``). We use
        # ``_extract_paren`` instead which tracks paren depth.
        open_idx = m.end() - 1   # position of '('
        cond, after_cond = self._extract_paren(tail, open_idx)
        if not cond and not after_cond:
            # _extract_paren hit end-of-string without finding the
            # closing ')' — fall back to the old regex behaviour so we
            # at least emit *something* (the regex is non-greedy and
            # may not capture the full condition, but a partial
            # translation is better than a TODO).
            m2 = re.match(r"\s*while\s*\((.*?)\)\s*;?", tail, re.DOTALL)
            if not m2:
                self._emit_todo(s, "do-while without while")
                return
            cond = m2.group(1)
        cond = self._translate_cond(cond)
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
          \\n  -> NL                  (newline constant — see notes below)
          \\t  -> #9
          \\r  -> #13
          \\0  -> #0
          \\"  -> "
          \\'  -> ''
          \\\\ -> \\

        Returns a Pascal string expression like ``'hello'`` or
        ``'hello' + NL + ''``.

        Note on the newline escape: the original implementation mapped ``\\n``
        to ``LineEnding`` (the FPC platform-line-ending constant).  That
        works but it produces a lot of ``' + LineEnding + '`` noise for
        strings that simply contain an embedded newline, which is the
        common case in opgui's strings.c (e.g.
        ``strings_en[S_dev_det]="Device detected: ..." NL "Path: %s" NL;``).
        We now split on ``\\n`` and rejoin with ``' + NL + '``, producing
        the cleaner ``'Device detected: ...' + NL + 'Path: %s' + NL + ''``
        form expected by the user.  The generated unit always defines a
        ``const NL = #10;`` constant alongside the converted code, so
        ``NL`` resolves to the same value as ``LineEnding`` on Unix/macOS
        and remains a single ``\n`` character on Windows (matching the
        original C behaviour, where ``#define NL "\n"`` always produced
        a single LF regardless of platform).

        ``inner`` here is the *contents* of the C string (no surrounding
        double quotes).  A standalone ``\\n`` (i.e. ``inner == "\\n"``)
        is collapsed to the bare ``NL`` symbol so ``strings_en[S_NL] := NL;``
        matches the original C intent of ``strings_en[S_NL] = NL;``.
        """
        # Split on \n so each piece becomes a single-quoted Pascal
        # fragment joined by the NL constant.
        parts = inner.split('\\n')
        if len(parts) == 1:
            # No newline — single Pascal string with other escapes processed
            s = parts[0]
            s = s.replace('\\t', "' + #9 + '")
            s = s.replace('\\r', "' + #13 + '")
            s = s.replace('\\0', "' + #0 + '")
            s = s.replace('\\"', '"')
            s = s.replace("\\'", "''")
            s = s.replace("\\\\", "\\")
            return f"'{s}'"
        if len(parts) == 2 and parts[0] == "" and parts[1] == "":
            # Standalone "\n" — collapse to the NL constant
            return "NL"
        pas_parts = []
        for p in parts:
            p = p.replace('\\t', "' + #9 + '")
            p = p.replace('\\r', "' + #13 + '")
            p = p.replace('\\0', "' + #0 + '")
            p = p.replace('\\"', '"')
            p = p.replace("\\'", "''")
            p = p.replace("\\\\", "\\")
            pas_parts.append(f"'{p}'")
        return " + NL + ".join(pas_parts)

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
          * Pointer-deref with post-increment: ``*p++ = expr`` ->
            ``p^ := expr; Inc(p);`` (C idiom used heavily in stream /
            buffer code, e.g. enet's ``*outData++ = encodeLow >> 24;``)
        """
        pad = self._pad()
        # Strip trailing semicolon
        s_clean = s.rstrip(";").rstrip()
        if not s_clean:
            return False

        # ---- Pointer-deref with post-increment / post-decrement on LHS ----
        # C idiom: ``*p++ = expr;`` writes ``expr`` to ``*p`` and then
        # advances ``p`` by one element. Pascal has no built-in syntax
        # for this, so we split it into two statements:
        #     p^ := expr;
        #     Inc(p);
        # (or ``Dec(p)`` for ``*p-- = expr;``).
        # This pattern is very common in the enet range coder
        # (``* outData ++ = encodeLow >> 24;`` inside ENET_RANGE_CODER_OUTPUT,
        # ``* inData ++`` reads, etc.) so handling it here keeps the
        # pattern-based fallback translator on par with the AST translator.
        #
        # We match the following shapes (allowing arbitrary whitespace):
        #   *p ++ = expr
        #   *p-- = expr
        #   *(*p).field ++ = expr    (rare; we don't handle this complex form)
        # We deliberately keep this regex simple — anything more complex
        # (e.g. ``*p[i]++ = expr``) falls through to the generic
        # assignment handler, which may emit a TODO.
        m_post = re.match(
            r"^\*\s*(?P<ptr>\w+(?:\s*\[[^\]]*\]?"
            r"|\s*\.\s*\w+"
            r"|\s*->\s*\w+)*)"
            r"\s*(?P<op>\+\+|--)\s*=\s*(?P<rhs>.+)$",
            s_clean, re.DOTALL,
        )
        if m_post:
            ptr = self._translate_expr(m_post.group("ptr"))
            rhs = self._translate_expr(m_post.group("rhs").strip())
            self._emit(f"{pad}{ptr}^ := {rhs};")
            if m_post.group("op") == "++":
                self._emit(f"{pad}Inc({ptr});")
            else:
                self._emit(f"{pad}Dec({ptr});")
            return True

        # ---- Symmetric case: post-increment on a deref-write with the
        # increment AFTER the rhs (very rare; some macros write
        # ``*(p++) = expr``). Handle it for completeness. ----
        m_post2 = re.match(
            r"^\*\s*\(\s*(?P<ptr>\w+)\s*(?P<op>\+\+|--)\s*\)"
            r"\s*=\s*(?P<rhs>.+)$",
            s_clean, re.DOTALL,
        )
        if m_post2:
            ptr = self._translate_expr(m_post2.group("ptr"))
            rhs = self._translate_expr(m_post2.group("rhs").strip())
            self._emit(f"{pad}{ptr}^ := {rhs};")
            if m_post2.group("op") == "++":
                self._emit(f"{pad}Inc({ptr});")
            else:
                self._emit(f"{pad}Dec({ptr});")
            return True

        # ---- Read with post-increment: ``value = *ptr ++;`` ----
        # C reads ``*ptr`` into ``value`` and then advances ``ptr``.
        # Pascal: ``value := ptr^; Inc(ptr);``.
        # We only handle the SIMPLE form (single identifier LHS, RHS is
        # exactly ``*ptr++`` or ``*ptr--``) — composite expressions like
        # ``decodeCode |= *inData++ << 24`` are handled separately below.
        m_read_post = re.match(
            r"^(?P<lhs>\w+)\s*=\s*\*\s*(?P<ptr>\w+)\s*(?P<op>\+\+|--)\s*$",
            s_clean, re.DOTALL,
        )
        if m_read_post:
            lhs = self._translate_expr(m_read_post.group("lhs"))
            ptr = self._translate_expr(m_read_post.group("ptr"))
            self._emit(f"{pad}{lhs} := {ptr}^;")
            if m_read_post.group("op") == "++":
                self._emit(f"{pad}Inc({ptr});")
            else:
                self._emit(f"{pad}Dec({ptr});")
            return True

        # ---- Compound-expression read with post-increment ----
        # Pattern: ``lhs op= *ptr ++ [<< shift]`` or ``lhs = ... *ptr++ ...``
        # The most common case in enet is:
        #     decodeCode |= *inData ++ << 24;
        # which is:
        #     decodeCode := decodeCode or (inData^) shl 24; Inc(inData);
        # We handle the general case: scan s_clean for ``* <ident> ++``
        # or ``* <ident> --``. For each occurrence, replace it with
        # ``<ident>^`` in the expression and emit an ``Inc(<ident>);``
        # statement AFTER the assignment. (If both ``++`` and ``--`` are
        # present, we emit one Inc/Dec per occurrence.)
        # NOTE: C semantics is post-increment — the value of ``*p++``
        # as an expression is the value of ``*p`` BEFORE the increment.
        # Our rewrite uses ``p^`` (current value) and emits ``Inc(p)``
        # AFTER the assignment, which matches the C semantics for
        # ``decodeCode |= *inData++ << 24`` (the read happens first, then
        # the increment). For chained uses (e.g. ``a = *p++ + *p++``) the
        # order of increments is implementation-defined in C anyway, so
        # emitting them after the statement is acceptable.
        if re.search(r"\*\s*\w+\s*(?:\+\+|--)", s_clean):
            # Find all ``*ident++`` / ``*ident--`` occurrences at the
            # expression level (NOT inside string/char literals — but
            # s_clean here is the statement, with strings preserved;
            # we skip occurrences inside quotes by tracking quote
            # state while scanning).
            incr_list: list[tuple[str, str]] = []  # (ptr, "++"/"--")
            parts: list[str] = []
            i = 0
            in_str = False
            in_char = False
            esc = False
            while i < len(s_clean):
                ch = s_clean[i]
                if in_str:
                    parts.append(ch)
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    i += 1
                    continue
                if in_char:
                    parts.append(ch)
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == "'":
                        in_char = False
                    i += 1
                    continue
                if ch == '"':
                    in_str = True
                    parts.append(ch)
                    i += 1
                    continue
                if ch == "'":
                    in_char = True
                    parts.append(ch)
                    i += 1
                    continue
                # Try to match ``*ident++`` or ``*ident--`` at this position
                m = re.match(
                    r"\*\s*(\w+)\s*(\+\+|--)",
                    s_clean[i:],
                )
                if m:
                    ptr_name = m.group(1)
                    op_kind = m.group(2)
                    incr_list.append((ptr_name, op_kind))
                    # Replace ``*ident++`` with ``ident^`` in the output
                    parts.append(f"{ptr_name}^")
                    i += m.end()
                    continue
                parts.append(ch)
                i += 1
            # If we found at least one occurrence, rebuild the statement
            # and emit Inc/Dec after the assignment.
            if incr_list:
                rewritten = "".join(parts)
                # Translate the rewritten expression (which now has no
                # ``*p++`` — just ``p^``) using the normal assignment
                # logic. We recursively call _try_assignment with the
                # rewritten statement so augmented ops, chained
                # assignment, etc. still work.
                # Strip trailing semicolon first (we'll re-add it).
                ok = self._try_assignment(rewritten.rstrip(";").rstrip())
                if ok:
                    # Emit the Inc/Dec statements AFTER the assignment.
                    # This matches C post-increment semantics for the
                    # common case (the value is read, then the pointer
                    # is advanced).
                    for ptr_name, op_kind in incr_list:
                        if op_kind == "++":
                            self._emit(f"{pad}Inc({ptr_name});")
                        else:
                            self._emit(f"{pad}Dec({ptr_name});")
                    return True
                # If the recursive call didn't handle it, fall through to
                # the generic handlers below.

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

    def _split_ternary(self, expr: str):
        """Split a C ternary expression ``cond ? a : b`` into (cond, a, b).

        Returns ``None`` if ``expr`` is not a ternary at the top level.

        We scan left-to-right tracking:
          * paren depth (``(`` / ``)``)
          * square-bracket depth (``[`` / ``]``)
          * whether we are inside a C string literal (``"..."``) or a
            char literal (``'...'``)

        Only a ``?`` at the outermost level (paren depth 0, not inside any
        string/char literal) is treated as the ternary operator. The
        matching ``:`` is then searched at the same depth, taking into
        account that ``a`` may itself contain nested ternaries (so we
        greedily consume balanced ``? :`` pairs while scanning for the
        final ``:`` at depth 0).

        This avoids the previous bug where a non-greedy regex matched
        the FIRST ``?`` even when nested inside parentheses, e.g.
        ``(break_addr>>8)+(freeze?0x40:0)`` was split as
        ``c="(break_addr>>8)+(freeze"`` instead of correctly recognising
        the inner ternary ``freeze?0x40:0``.
        """
        depth_paren = 0
        depth_bracket = 0
        in_str = False
        in_char = False
        str_escape = False
        ternary_q_pos = -1
        for i, ch in enumerate(expr):
            if in_str:
                if str_escape:
                    str_escape = False
                elif ch == "\\":
                    str_escape = True
                elif ch == '"':
                    in_str = False
                continue
            if in_char:
                if str_escape:
                    str_escape = False
                elif ch == "\\":
                    str_escape = True
                elif ch == "'":
                    in_char = False
                continue
            if ch == '"':
                in_str = True
                continue
            if ch == "'":
                in_char = True
                continue
            if ch == "(":
                depth_paren += 1
                continue
            if ch == ")":
                depth_paren -= 1
                continue
            if ch == "[":
                depth_bracket += 1
                continue
            if ch == "]":
                depth_bracket -= 1
                continue
            if ch == "?" and depth_paren == 0 and depth_bracket == 0:
                ternary_q_pos = i
                break
        if ternary_q_pos < 0:
            return None
        # We have a top-level '?'.  Now scan from there to find the matching
        # ':' at the same depth.  We must skip over any nested ternaries:
        # ``a ? b : c ? d : e`` should split as cond="a", a="b", b="c ? d : e"
        # (right-associative).  For our use here, we want the FIRST ':' that
        # is at depth 0 (paren/bracket) AND at ternary depth 0.  Whenever we
        # see another '?' at depth 0, we increment a ternary counter so we
        # skip over the matching ':'.
        i = ternary_q_pos + 1
        ternary_depth = 0
        colon_pos = -1
        while i < len(expr):
            ch = expr[i]
            if in_str:
                if str_escape:
                    str_escape = False
                elif ch == "\\":
                    str_escape = True
                elif ch == '"':
                    in_str = False
            elif in_char:
                if str_escape:
                    str_escape = False
                elif ch == "\\":
                    str_escape = True
                elif ch == "'":
                    in_char = False
            elif ch == '"':
                in_str = True
            elif ch == "'":
                in_char = True
            elif ch == "(":
                depth_paren += 1
            elif ch == ")":
                depth_paren -= 1
            elif ch == "[":
                depth_bracket += 1
            elif ch == "]":
                depth_bracket -= 1
            elif depth_paren == 0 and depth_bracket == 0:
                if ch == "?":
                    ternary_depth += 1
                elif ch == ":":
                    if ternary_depth == 0:
                        colon_pos = i
                        break
                    ternary_depth -= 1
            i += 1
        if colon_pos < 0:
            return None
        c_raw = expr[:ternary_q_pos].strip()
        a_raw = expr[ternary_q_pos + 1:colon_pos].strip()
        b_raw = expr[colon_pos + 1:].strip()
        return (c_raw, a_raw, b_raw)

    def _expr_contains_top_level_question(self, expr: str) -> bool:
        """Return True if ``expr`` contains a ``?`` character anywhere
        outside of string/char literals (i.e. it is or contains a C
        ternary operator).  Used to decide whether to recurse into the
        expression looking for nested ternaries that the top-level
        split missed.
        """
        in_str = False
        in_char = False
        str_escape = False
        for ch in expr:
            if in_str:
                if str_escape:
                    str_escape = False
                elif ch == "\\":
                    str_escape = True
                elif ch == '"':
                    in_str = False
                continue
            if in_char:
                if str_escape:
                    str_escape = False
                elif ch == "\\":
                    str_escape = True
                elif ch == "'":
                    in_char = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "'":
                in_char = True
            elif ch == "?":
                return True
        return False

    def _translate_nested_ternaries(self, expr: str) -> str:
        """Recursively translate any ``cond ? a : b`` ternaries that
        appear *inside* parenthesised sub-expressions of ``expr``.

        The top-level ternary (if any) was already handled by the caller.
        Here we look for parenthesised groups that contain a ``?`` and
        recursively translate the contents of those groups, then
        re-assemble the expression with the translated groups in place.

        We do this by scanning ``expr`` once, identifying each top-level
        parenthesised run (``(...)``, ``[...]``), and whenever a run
        contains a ``?`` we recursively call ``_translate_expr`` on its
        inner text (which will itself handle any deeper-nested ternary
        or apply the OP_REPLACE / cast / etc. fallbacks).  String and
        char literals are skipped so a ``?`` inside a string does not
        trigger recursion.

        Example: ``(break_addr>>8)+(freeze?0x40:0)``
          ->  ``(break_addr>>8)+(IfThen(freeze, $40, 0))``
        (after the inner ternary is recursively translated; the caller
        then applies OP_REPLACE / hex conversion in the usual way).
        """
        if not expr:
            return expr
        out: list[str] = []
        i = 0
        n = len(expr)
        in_str = False
        in_char = False
        str_escape = False
        while i < n:
            ch = expr[i]
            if in_str:
                out.append(ch)
                if str_escape:
                    str_escape = False
                elif ch == "\\":
                    str_escape = True
                elif ch == '"':
                    in_str = False
                i += 1
                continue
            if in_char:
                out.append(ch)
                if str_escape:
                    str_escape = False
                elif ch == "\\":
                    str_escape = True
                elif ch == "'":
                    in_char = False
                i += 1
                continue
            if ch == '"':
                in_str = True
                out.append(ch)
                i += 1
                continue
            if ch == "'":
                in_char = True
                out.append(ch)
                i += 1
                continue
            if ch in "([":
                # Find the matching close paren/bracket at the same depth
                open_ch = ch
                close_ch = ")" if ch == "(" else "]"
                depth = 1
                j = i + 1
                while j < n and depth > 0:
                    cj = expr[j]
                    if cj == '"' or cj == "'":
                        # Skip the string/char literal entirely so any
                        # paren-like characters inside it are ignored.
                        quote = cj
                        j += 1
                        while j < n:
                            if expr[j] == "\\" and j + 1 < n:
                                j += 2
                                continue
                            if expr[j] == quote:
                                j += 1
                                break
                            j += 1
                        continue
                    if cj == open_ch:
                        depth += 1
                    elif cj == close_ch:
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                if depth != 0:
                    # Unbalanced — leave the rest verbatim
                    out.append(expr[i:])
                    break
                inner = expr[i + 1:j]
                # If the inner text contains a ``?`` (outside strings),
                # translate it recursively; otherwise leave it alone so
                # the caller's normal handling can apply.
                if self._expr_contains_top_level_question(inner):
                    translated = self._translate_expr(inner)
                    out.append(open_ch + translated + close_ch)
                else:
                    out.append(expr[i:j + 1])
                i = j + 1
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    # ------------------------------------------------------------------
    # Expression translator
    # ------------------------------------------------------------------
    def _translate_expr(self, expr: str) -> str:
        expr = expr.strip()
        if not expr:
            return ""
        # Normalise spaced multi-char operators (e.g. ``peer - > field``)
        # to their unspaced forms BEFORE any pattern matching.  This
        # must happen at the very top of _translate_expr because the
        # downstream splitters (_split_comparison, _split_logical,
        # _split_ternary, etc.) look for unspaced tokens like ``->``,
        # ``<<``, ``>>``, ``&&``, ``||``.  If the input has spaces
        # inside these operators (e.g. ``peer - > field``), the
        # splitters would not recognise them and the expression would
        # fall through to the generic operator-replacement path which
        # also only matches unspaced tokens.
        expr = _normalise_spaced_operators(expr)
        # Ternary  (depth-aware: only split on top-level ?: operators)
        # The previous simple non-greedy regex matched the FIRST '?' in the
        # expression even when nested inside parentheses. For example,
        # ``(break_addr>>8)+(freeze?0x40:0)`` was split as
        #   c = "(break_addr>>8)+(freeze", a = "0x40", b = "0)"
        # which produced malformed Pascal ``IfThen((break_addr shr 8)+(freeze, $40, 0))``.
        # We now scan the expression left-to-right tracking paren/string
        # depth, and only split on a ``?`` that is at depth 0.
        ternary = self._split_ternary(expr)
        if ternary:
            c_raw, a_raw, b_raw = ternary
            c = self._translate_expr(c_raw)
            a = self._translate_expr(a_raw)
            b = self._translate_expr(b_raw)
            return f"IfThen({c}, {a}, {b})"
        # If the whole expression is wrapped in a single pair of parens
        # AND there is a top-level binary operator inside those parens,
        # we still want to honour any ternary nested inside (e.g.
        # ``(break_addr>>8)+(freeze?0x40:0)`` — the ternary is inside the
        # second paren group, not at the top level of the whole
        # expression).  Walk the expression looking for parenthesised
        # sub-groups and recursively translate any that contain a ``?``.
        # This preserves the structure (we still emit the surrounding
        # parens / operators verbatim) but lets the inner ternary be
        # converted to ``IfThen(...)``.
        if self._expr_contains_top_level_question(expr):
            expr = self._translate_nested_ternaries(expr)
        # GTK/libc call expression
        # Match: NAME(args) where args is balanced (handles nested parens
        # and multiple function calls in one expression like
        # ``strlen("") + strlen("hello")`` — only the FIRST call is matched)
        m = re.match(r"^(?P<fn>[A-Za-z_]\w*)\s*\(", expr)
        if m:
            # Find the matching closing paren
            fn = m.group("fn")
            paren_start = m.end() - 1  # position of '('
            depth = 1
            j = paren_start + 1
            while j < len(expr) and depth > 0:
                if expr[j] == "(":
                    depth += 1
                elif expr[j] == ")":
                    depth -= 1
                j += 1
            if depth == 0:
                args_raw = expr[paren_start + 1:j - 1]
                trailing = expr[j:].strip()
                args = self._split_args(args_raw)
                # If there's trailing text (e.g. ``strlen("") + strlen("x")``),
                # this is NOT a simple function call — it's a compound
                # expression. Fall through to operator handling.
                if not trailing:
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
                # Has trailing — not a simple call, fall through
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
            # Strip the surrounding quotes and reuse the shared helper so
            # ``\n`` becomes a ``' + NL + '`` joiner (and ``"\n"`` collapses
            # to ``NL``).  Other escapes (``\t``, ``\r``, ``\0``, ``\"``,
            # ``\'``, ``\\``) are processed too.
            inner = expr[1:-1]
            return self._translate_c_string(inner)
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
        if re.match(r"^\w+$", expr) and expr in self._name_map:
            return self._name_map[expr]
        # ---- Calling convention placeholders ----
        # ``__GTK2LAZ_CALLCONV_cdecl`` etc. are placeholders inserted
        # by pcpp_preprocessor.preprocess() to preserve the C calling
        # convention through pycparser (which doesn't know ``__cdecl``).
        # In expression context these placeholders are meaningless —
        # they only matter in function/procedure declarations, which
        # are handled by the signature translator.  In expressions
        # we just strip them (they appear as type qualifiers that
        # Pascal doesn't need in expression position).
        if expr.startswith("__GTK2LAZ_CALLCONV_"):
            return ""
        # ---- Logical operators (&&, ||) — wrap each side in parens ----
        # Pascal's ``and``/``or`` are bitwise by default and have higher
        # precedence than comparison operators (unlike C, where ``&&`` has
        # lower precedence than ``<``, ``==`` etc.). Without parentheses,
        # ``a<b and c<>d`` would parse in Pascal as ``a < (b and c) <> d``,
        # which is wrong. So we always wrap each operand of &&/|| in parens.
        # Also note: we must split on && / || at top level (depth 0) to
        # avoid breaking expressions inside parentheses, function calls,
        # or array subscripts.
        #
        # IMPORTANT: this MUST run BEFORE ``_split_comparison``.  In C,
        # ``&&`` / ``||`` have LOWER precedence than ``<`` / ``==`` /
        # ``!=``, so for an expression like ``z<DIMBUF-1 && bufferI[z]!=READ_PINS``
        # we must split on ``&&`` first and then recurse into each
        # comparison operand.  The previous code called ``_split_comparison``
        # first, which happily split on the very first ``<`` it saw at
        # paren depth 0 and then handed the *rest* (``DIMBUF-1 && bufferI[z]!=READ_PINS``)
        # off as the right-hand side — producing broken output like
        # ``z < (DIMBUF-1) and (bufferI[z]) <> READ_PINS`` where the
        # right-hand side ``bufferI[z]`` was parenthesised incorrectly.
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
        # ---- Comparison operators (==, !=, <, >, <=, >=) ----
        # Split on top-level comparison operators so each side is
        # translated independently. This ensures string literals inside
        # function call arguments (e.g. ``strcmp(s, "") == 0``) are
        # properly converted to Pascal string literals.
        comp = self._split_comparison(expr)
        if comp:
            op, left, right = comp
            pas_left = self._translate_expr(left)
            pas_right = self._translate_expr(right)
            pas_op = {"==": "=", "!=": "<>", "<": "<", ">": ">",
                      "<=": "<=", ">=": ">="}[op]
            return f"{pas_left} {pas_op} {pas_right}"
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
        # ---- Normalise spaced multi-char operators so _OP_REPLACE can
        # match them.  Some C code writes ``peer -> field`` (with spaces
        # around ``->``), or ``a < < b`` (with spaces inside ``<<``).
        # C treats these the same as the unspaced forms, but our
        # _OP_REPLACE table only matches the unspaced tokens.  We
        # collapse the spaced forms to their unspaced equivalents
        # OUTSIDE string/char literals so the replacement table can
        # find them.
        result = _normalise_spaced_operators(result)
        # ---- Convert C string literals ("...") -> Pascal ('...') FIRST ----
        # This must happen before _OP_REPLACE because operators like ``&``
        # could appear inside strings and must not be replaced.
        # Also handles escape sequences.
        result = self._convert_string_literals(result)
        # ---- Convert hex literals (0x...) -> $... throughout the expression ----
        # This must happen BEFORE operator replacement because ``&`` etc.
        # might appear adjacent to hex literals.
        # Match: 0x followed by hex digits, with optional U/L suffixes.
        # Don't match inside string literals (single/double quoted).
        result = self._convert_hex_literals(result)
        # Apply C->Pascal operator replacements.  CRITICAL: we must NOT
        # touch anything inside a single-quoted Pascal string literal —
        # characters like ``%`` are valid in Pascal format strings (used
        # by sscanf/sprintf/Format) and must be preserved verbatim.  The
        # naive ``result.replace(old, new)`` would corrupt them (e.g.
        # ``'break %x'`` would become ``'break  mod x'``).  We split the
        # result into string-literal / non-string segments and apply the
        # replacements only to the non-string segments.
        result = self._apply_op_replace_outside_strings(result)
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
    def _convert_string_literals(s: str) -> str:
        """Convert all C double-quoted string literals in ``s`` to Pascal
        single-quoted strings, handling escape sequences.

        ``"hello\\n"`` -> ``'hello' + NL + ''``
        ``"\\n"`` -> ``NL``            (bare constant reference)
        ``""`` -> ``''``
        ``"plain"`` -> ``'plain'``

        Skips single-quoted char literals.

        Newlines (``\\n``) are split out as ``' + NL + '`` rather than
        ``' + LineEnding + '`` so that the generated source reads the way
        opgui's strings.c defines them (``#define NL "\\n"``); the unit
        emits a matching ``const NL = #10;`` so the symbol resolves
        correctly.  Other escape sequences (``\\t``, ``\\r``, ``\\0``)
        still expand to their Pascal control-char forms.
        """
        out: list[str] = []
        i = 0
        n = len(s)
        while i < n:
            ch = s[i]
            if ch == '"':
                # Find the closing quote (handle escape \")
                j = i + 1
                while j < n:
                    if s[j] == '\\' and j + 1 < n:
                        j += 2
                        continue
                    if s[j] == '"':
                        break
                    j += 1
                if j >= n:
                    # No closing quote — take rest of string
                    out.append(s[i:])
                    break
                inner = s[i+1:j]
                # Reuse _translate_c_string so the escape-sequence and
                # ``' + NL + '`` handling stays consistent with the
                # _translate_expr path.
                out.append(BodyTranslator._translate_c_string(inner))
                i = j + 1
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    def _apply_op_replace_outside_strings(self, s: str) -> str:
        """Apply every ``_OP_REPLACE`` substitution (e.g. ``%`` -> `` mod ``,
        ``&&`` -> `` and ``, ``>>`` -> `` shr ``, ``&`` -> `` and ``) to
        ``s`` while leaving characters inside Pascal string/char literals
        untouched.

        The original implementation used a plain ``result.replace(old,
        new)`` loop, which corrupted format strings such as
        ``'break %x'`` -> ``'break  mod x'`` because the ``%`` inside the
        Pascal string literal was treated as a modulo operator.  We now
        split the input into ``string-literal`` and ``non-string``
        segments and apply the operator replacements only to the
        non-string segments.

        Both Pascal single-quoted strings (``'...'``) and any leftover
        C double-quoted strings (``"..."``) are skipped; the latter can
        still appear here for unusual inputs that did not go through
        ``_convert_string_literals`` first.
        """
        # Walk through ``s`` collecting (segment, is_string) pairs, then
        # apply the OP_REPLACE table only to the non-string segments.
        out: list[str] = []
        i = 0
        n = len(s)
        in_str = False
        in_char = False
        str_escape = False
        cur = []
        cur_is_string = False
        segs: list[tuple[str, bool]] = []
        while i < n:
            ch = s[i]
            if in_str:
                cur.append(ch)
                if str_escape:
                    str_escape = False
                elif ch == "\\":
                    str_escape = True
                elif ch == "'":
                    in_str = False
                i += 1
                continue
            if in_char:
                cur.append(ch)
                if str_escape:
                    str_escape = False
                elif ch == "\\":
                    str_escape = True
                elif ch == "'":
                    in_char = False
                i += 1
                continue
            if ch == "'":
                # Flush non-string segment, start a string segment.
                if cur:
                    segs.append(("".join(cur), False))
                    cur = []
                cur.append(ch)
                in_str = True
                i += 1
                continue
            if ch == '"':
                if cur:
                    segs.append(("".join(cur), False))
                    cur = []
                cur.append(ch)
                # Find the matching closing quote (honouring backslash
                # escapes) so the whole C string is treated as one segment.
                j = i + 1
                while j < n:
                    if s[j] == "\\" and j + 1 < n:
                        cur.append(s[j])
                        cur.append(s[j + 1])
                        j += 2
                        continue
                    cur.append(s[j])
                    if s[j] == '"':
                        j += 1
                        break
                    j += 1
                segs.append(("".join(cur), True))
                cur = []
                i = j
                continue
            cur.append(ch)
            i += 1
        if cur:
            segs.append(("".join(cur), False))
        # Apply _OP_REPLACE to non-string segments only.
        for text, is_string in segs:
            if is_string:
                out.append(text)
                continue
            # First, normalise spaced multi-char operators (e.g.
            # ``- >`` -> ``->``) so the _OP_REPLACE table can match
            # them.  This handles C code that writes ``peer -> field``
            # with spaces around ``->``.
            text = _normalise_spaced_operators(text)
            for old, new in self._OP_REPLACE:
                text = text.replace(old, new)
            out.append(text)
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
                # Skip whitespace after -> (e.g. ``-> symbols``)
                while i < n and expr[i] in " \t":
                    i += 1
                # Read the field name after ->
                while i < n and (expr[i].isalnum() or expr[i] == "_"):
                    i += 1
                # After the field name, there may be an array index
                # (e.g. ``->symbols[predicted]``) or another member
                # access (e.g. ``->field->subfield``).  We need to
                # consume the array index HERE, as part of the same
                # operand, so that ``&rangeCoder->symbols[predicted]``
                # is extracted as the full operand rather than
                # splitting at ``symbols``.  We do this by NOT
                # returning to the outer while loop's whitespace skip
                # immediately — instead we check for ``[`` right after
                # the field name.
                k = i
                while k < n and expr[k] in " \t":
                    k += 1
                if k < n and expr[k] == "[":
                    # Consume the array index as part of this operand
                    depth = 1
                    i = k + 1
                    while i < n and depth > 0:
                        if expr[i] == "[":
                            depth += 1
                        elif expr[i] == "]":
                            depth -= 1
                        i += 1
                # Continue the outer loop to check for more member
                # accesses after the array index (e.g. ``->arr[i].field``).
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

    def _split_comparison(self, expr: str):
        """Split an expression on a top-level comparison operator.

        Returns ``(op, left, right)`` if a top-level comparison is found,
        or ``None`` if not. The split respects parentheses and string
        literals so that ``strcmp(s, "") == 0`` is split on ``==`` (not
        on any ``=`` inside the string ``""``).

        We also avoid splitting on ``<`` or ``>`` that are part of a
        shift operator (``<<``, ``>>``): the previous implementation
        skipped only the *first* char of a shift pair, so ``2000>>8``
        was matched at the *second* ``>`` and split as
        ``('>', '2000>', '8')``, corrupting the shift into ``> >``.
        We now skip the entire ``<<`` / ``>>`` token before resuming the
        scan, so the second ``<`` / ``>`` is not mistaken for a
        comparison operator.
        """
        depth = 0
        paren_depth = 0
        in_str = False
        in_char = False
        str_char = ""
        str_escape = False
        i = 0
        n = len(expr)
        # Two-char operators first: ==, !=, <=, >=
        while i < n:
            ch = expr[i]
            # ---- Skip string/char literal contents ----
            if in_str:
                if str_escape:
                    str_escape = False
                elif ch == "\\":
                    str_escape = True
                elif ch == str_char:
                    in_str = False
                i += 1
                continue
            if in_char:
                if str_escape:
                    str_escape = False
                elif ch == "\\":
                    str_escape = True
                elif ch == str_char:
                    in_char = False
                i += 1
                continue
            if ch in ('"', "'"):
                in_str = ch == '"'
                in_char = ch == "'"
                str_char = ch
                i += 1
                continue
            # ---- Track paren depth ----
            if ch == "(":
                paren_depth += 1
                i += 1
                continue
            if ch == ")":
                paren_depth -= 1
                i += 1
                continue
            # ---- Only consider top-level comparison operators ----
            if paren_depth == 0:
                two = expr[i:i+2]
                # ==, !=, <=, >= (and bitwise-or-assign | = etc. are
                # handled elsewhere, not here).
                if two in ("==", "!=", "<=", ">="):
                    return (two, expr[:i].strip(), expr[i+2:].strip())
                # Shift operators: << / >>.  Skip the whole token so
                # neither ``<`` nor ``>`` is mistaken for a comparison.
                if two in ("<<", ">>"):
                    i += 2
                    continue
                # Pointer member access: ->.  Skip the whole token so
                # the ``>`` in ``->`` is not mistaken for a comparison.
                # This is critical for expressions like
                # ``channel->outgoingUnreliableSequenceNumber < $FFFF``
                # where the ``>`` in ``->`` would otherwise be matched
                # as a comparison operator, splitting the expression
                # as ``('>', 'channel-', 'outgoingUnreliableSequenceNumber...')``
                # and corrupting the output.
                if two == "->":
                    i += 2
                    continue
                # Single-char < or > (but not << or >> — already
                # handled above).
                if ch in "<>" and i + 1 < n and expr[i+1] not in "<>":
                    return (ch, expr[:i].strip(), expr[i+1:].strip())
                if ch in "<>" and i + 1 >= n:
                    return (ch, expr[:i].strip(), "")
            i += 1
        return None

    def _split_logical(self, expr: str):
        """Split a top-level ``&&`` / ``||`` chain into operands.

        Returns ``(op, [parts])`` if the expression contains a top-level
        logical operator, or ``None`` if it does not. The split respects
        paren/bracket nesting so that ``f(a && b)`` is not split.

        The returned operator is the *lowest-precedence* logical operator
        found at top level (``||`` wins over ``&&``), mirroring C
        precedence so that ``a || b && c`` is split on ``||`` first and
        ``b && c`` is recursed into on the next call.

        String and char literals are skipped so that a ``&`` or ``|``
        inside a string literal does not falsely trigger a split on
        ``&&`` / ``||`` (e.g. ``strcmp(s, "&&") == 0`` would otherwise
        be split on the ``&&`` inside the string).
        """
        # First pass: find top-level || (lowest precedence in C)
        # If found, split there. Otherwise split on &&.
        for target_op in ("||", "&&"):
            parts: list[str] = []
            cur: list[str] = []
            i = 0
            depth = 0
            in_str = False
            in_char = False
            str_escape = False
            found = False
            n = len(expr)
            while i < n:
                ch = expr[i]
                if in_str:
                    cur.append(ch)
                    if str_escape:
                        str_escape = False
                    elif ch == "\\":
                        str_escape = True
                    elif ch == '"':
                        in_str = False
                    i += 1
                    continue
                if in_char:
                    cur.append(ch)
                    if str_escape:
                        str_escape = False
                    elif ch == "\\":
                        str_escape = True
                    elif ch == "'":
                        in_char = False
                    i += 1
                    continue
                if ch == '"':
                    in_str = True
                    cur.append(ch)
                    i += 1
                    continue
                if ch == "'":
                    in_char = True
                    cur.append(ch)
                    i += 1
                    continue
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
                 header_externs: list[tuple[str, str]] = None,
                 target_platform: str = "auto"):
        self.c_path = c_path
        self.unit_name = unit_name
        self.form_unit = form_unit  # name of the unit that owns form fields
        self.included_units = list(included_units or [])
        self.fake_include_dir = fake_include_dir
        self.use_ast = use_ast
        self.target_platform = target_platform or "auto"
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

    @staticmethod
    def _parse_struct_fields_c(body: str) -> list[tuple[str, str]]:
        """Parse struct body into (field, type) tuples.
        Same logic as HeaderTranslator._parse_struct_fields."""
        out: list[tuple[str, str]] = []
        for decl in _split_top_level(body, ";"):
            decl = decl.strip()
            if not decl:
                continue
            decl = re.sub(r":\s*\d+", "", decl).strip()  # bitfields
            if not decl:
                continue
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

    # ------------------------------------------------------------------
    def translate(self) -> str:
        with open(self.c_path, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
        self.stats["lines_in"] = src.count("\n") + 1
        src_clean = strip_comments(src)
        # Skip macro extraction/expansion if this translator is reading
        # an already-expanded temp file (set by convert_c_file for the
        # second pass).  The temp file already has macros inlined and
        # #define lines removed, so re-running _extract_func_macros
        # would find nothing and the expansion would be a no-op —
        # BUT it would still write a NEW temp file (of the already-
        # expanded source) and set c_path to it, which breaks the
        # 2-pass logic.
        if not hasattr(self, '_skip_macro_expansion'):
            func_macros = _extract_func_macros(src_clean)
            if func_macros:
                print(f"gtk2laz: [{os.path.basename(self.c_path)}] "
                      f"extracted {len(func_macros)} function-like "
                      f"macros for inlining", file=sys.stderr)
                src_clean = _expand_func_macros_in_source(src_clean, func_macros)
                import tempfile
                # Use the original .c file basename in the temp file
                # name so the user can easily identify which source
                # file the expanded version came from.
                # e.g. ``gtk2laz_expanded_compress_a1b2c3.c``
                # instead of the opaque ``gtk2laz_expanded_a1b2c3.c``.
                _orig_base = os.path.splitext(
                    os.path.basename(self.c_path))[0]
                _prefix = f"gtk2laz_expanded_{_orig_base}_"
                tmp_fd, tmp_path = tempfile.mkstemp(
                    suffix=".c", prefix=_prefix,
                    dir=os.path.dirname(self.c_path)
                )
                try:
                    import os as _os
                    _os.write(tmp_fd, src_clean.encode("utf-8"))
                    _os.close(tmp_fd)
                    self._orig_c_path = self.c_path
                    self.c_path = tmp_path
                    print(f"gtk2laz: [{os.path.basename(self._orig_c_path)}] "
                          f"wrote expanded source to {tmp_path} "
                          f"({len(src_clean)} bytes)", file=sys.stderr)
                except Exception as _e:
                    _os.close(tmp_fd)
                    print(f"gtk2laz: [{os.path.basename(self.c_path)}] "
                          f"failed to write expanded temp file: "
                          f"{type(_e).__name__}: {_e}", file=sys.stderr)
            else:
                print(f"gtk2laz: [{os.path.basename(self.c_path)}] "
                      f"no function-like macros extracted — using "
                      f"original source for AST parse", file=sys.stderr)
        src_clean, directives = strip_preprocessor(src_clean)
        # Apply the same linkage-marker stripping that the AST path
        # gets via pcpp_preprocessor.preprocess(). The pattern path
        # does NOT go through pcpp, so any export/deprecated marker
        # that the user's source used (G_MODULE_EXPORT,
        # GDK_AVAILABLE_IN_ALL, _GLIB_EXTERN, SDL_FORCE_INLINE, ...)
        # would survive into src_clean and break the function-definition
        # regex. We import the function lazily so we don't take a
        # circular-import hit on module load.
        # We also use the usage-based identifier classifier so we
        # don't accidentally strip a real type (e.g. GtkWidget) that
        # is used as the return type of a function.
        try:
            from pcpp_preprocessor import (
                _strip_unknown_linkage_markers,
                _classify_identifier_usages,
            )
            usage_map = _classify_identifier_usages(src_clean)
            src_clean = _strip_unknown_linkage_markers(src_clean, usage_map)
        except Exception:
            pass
        # Normalise spaced multi-char operators (e.g. ``peer -> field``
        # with spaces around ``->``) to their unspaced forms so the
        # downstream pattern-based translator and _OP_REPLACE table can
        # match them.  This handles C code that writes ``a - > b``,
        # ``a < < b``, ``a > > b``, ``a & & b``, etc. — C treats these
        # the same as the unspaced forms, but our pattern matcher only
        # recognises the unspaced tokens.
        src_clean = _normalise_spaced_operators(src_clean)
        # Strip ``__attribute__((...))`` GCC annotations so the
        # pattern extractor's regex can match cleanly. Without this,
        # ``const guint8 x[] __attribute__((aligned(4))) = { ... };``
        # would not be matched as a single declaration because the
        # regex doesn't understand ``__attribute__`` between the name
        # and the ``=``. The AST path doesn't need this strip because
        # pcpp's gnu_defines already expands ``__attribute__`` to
        # nothing, but the pattern path sees the raw source text.
        # We use a balanced-paren scan (not a regex) because
        # ``__attribute__((aligned(4)))`` has nested parens that a
        # regex can't reliably match. The function scans for
        # ``__attribute__`` followed by ``(...)`` (with balanced
        # parens) and replaces it with a space.
        def _strip_attr(text):
            out = []
            i = 0
            n = len(text)
            while i < n:
                # Look for ``__attribute__`` at position i.
                if (text[i:i+13] == '__attribute__'
                        and i + 13 < n):
                    # Skip whitespace.
                    j = i + 13
                    while j < n and text[j].isspace():
                        j += 1
                    if j < n and text[j] == '(':
                        # Walk to matching ``)`` (balanced parens).
                        depth = 0
                        k = j
                        while k < n:
                            if text[k] == '(':
                                depth += 1
                            elif text[k] == ')':
                                depth -= 1
                                if depth == 0:
                                    break
                            k += 1
                        if k < n:
                            # Skip ``__attribute__(...)`` — replace
                            # with a space (preserves token
                            # separation).
                            out.append(' ')
                            i = k + 1
                            continue
                out.append(text[i])
                i += 1
            return ''.join(out)
        src_clean = _strip_attr(src_clean)

        # ---- Try AST-based analysis first ----
        if self.use_ast:
            try:
                from ast_translator import parse_with_ast
                self._ast_result = parse_with_ast(
                    self.c_path, self.unit_name,
                    fake_include_dir=self.fake_include_dir,
                    form_unit=self.form_unit,
                    included_units=self.included_units,
                    target_platform=self.target_platform,
                )
                if self._ast_result is not None:
                    self.stats["ast_used"] = True
                else:
                    # parse_with_ast() returned None — AST-based
                    # translation is unavailable for this file. We
                    # will fall back to the pattern-based translator
                    # below, which produces significantly more
                    # TODOs (the pattern matcher cannot recognise
                    # many C constructs that pycparser handles
                    # natively). Tell the user so they understand why
                    # this file may have more TODOs than expected.
                    # The underlying parse_error (if any) is already
                    # stored on the AstToPascal instance — but we
                    # never see it here because parse_with_ast()
                    # returns None instead of the partial object.
                    import sys as _sys
                    print(
                        f"gtk2laz: AST parse failed for "
                        f"{os.path.basename(self.c_path)} — falling "
                        f"back to pattern-based translator "
                        f"(expect more TODOs in this unit).",
                        file=_sys.stderr)
            except Exception as _e:
                self._ast_result = None
                # Print a concise summary and fall back to pattern.
                # The detailed diagnostic (source line, context,
                # binary search) was already printed by
                # ast_translator.parse() before re-raising.
                import sys as _sys
                print(
                    f"gtk2laz: [{os.path.basename(self.c_path)}] "
                    f"AST parse failed ({type(_e).__name__}: "
                    f"{str(_e)[:200]}) — falling back to "
                    f"pattern-based translator.",
                    file=_sys.stderr)

        # ---- Pattern-based fallback (always run for body translation) ----
        # ---- Extract inline struct global declarations ----
        # Pattern: struct TAG { fields... } NAME[SIZE];  (no typedef, no extern)
        # This is a global variable with an inline struct definition.
        # We extract the struct as a Pascal record type and the variable
        # as an array of that type.
        inline_type_decls: list[str] = []
        inline_var_decls: list[tuple[str, str]] = []
        for m in re.finditer(
            r"^(?:\s*)struct\s+(\w+)\s*\{([^}]*)\}\s*"
            r"(\w+)\s*(?:\[([^\]]*)\])?\s*;",
            src_clean, re.MULTILINE | re.DOTALL,
        ):
            tag = m.group(1)
            body = m.group(2)
            var_name = m.group(3)
            arr_size = m.group(4)
            # Create Pascal record type
            pas_type_name = "T" + tag if not tag.startswith("T") else tag
            fields = self._parse_struct_fields_c(body)
            if fields:
                inline_type_decls.append(f"  {pas_type_name} = record")
                seen_f = set()
                for fname, ftype in fields:
                    if fname in seen_f:
                        continue
                    seen_f.add(fname)
                    inline_type_decls.append(
                        f"    {pascalify_name(fname)}: {ftype};"
                    )
                inline_type_decls.append("  end;")
            # Variable declaration
            if arr_size:
                arr_size_pas = arr_size
                m_hex = re.match(r"^0[xX]([0-9A-Fa-f]+)$", arr_size.strip())
                if m_hex:
                    arr_size_pas = "$" + m_hex.group(1)
                inline_var_decls.append(
                    (var_name, f"array[0..{arr_size_pas}-1] of {pas_type_name}")
                )
            else:
                inline_var_decls.append((var_name, pas_type_name))
            # Remove this from residual so it doesn't appear as TODO
            src_clean = src_clean[:m.start()] + src_clean[m.end():]

        # Re-extract globals from the modified residual
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
        # Add the generated fake-type units (gtk_types, glib_types,
        # libc_types, sys_types) so this unit can reference the
        # fake-header typedefs (GtkWidget, gint, gpointer, fd_set,
        # ...) that are emitted in those dedicated units instead of
        # being duplicated in every .pas file.
        for fake_unit in _fake_type_unit_names(self.fake_include_dir):
            if fake_unit not in unit_list and fake_unit != self.unit_name:
                unit_list.append(fake_unit)
        if self.form_unit and self.form_unit != self.unit_name \
                and self.form_unit not in unit_list:
            unit_list.append(self.form_unit)
        for i, u in enumerate(unit_list):
            sep = "," if i < len(unit_list) - 1 else ";"
            out.append(f"  {u}{sep}")
        out.append("")
        # Constants from #define
        defines = self._extract_defines(directives)
        # Always emit a NL constant so that multi-line C string literals
        # (which we translate to ``'text' + NL + 'more'``) resolve to the
        # same single-LF value the original C ``#define NL "\n"`` produced.
        # We skip emitting it if a #define in this very unit already
        # provides NL (e.g. opgui's strings.c does ``#define NL "\n"``),
        # so the user's own definition wins.
        define_names = {name for name, _ in defines}
        if "NL" not in define_names:
            defines.insert(0, ("NL", "#10"))
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

        # Inline struct types from extern declarations
        # (e.g. ``extern struct var { ... } variables[0x200];``)
        if hasattr(self, '_inline_types') and self._inline_types:
            for t in self._inline_types:
                out.append(t)
            out.append("")

        # Inline struct types from C file global declarations
        # (e.g. ``struct var { ... } variables[0x200];`` in icd.c)
        if inline_type_decls:
            for t in inline_type_decls:
                out.append(t)
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
                # Byte-array initializer: emit as a typed Pascal array
                # constant (icons.c ``const guint8 write_icon[] = ...``).
                # The initializer string starts with ``($`` — see
                # ``_byte_array_init_to_pascal`` in ast_translator.
                # Pascal's typed array constant requires known bounds,
                # so we count the bytes and emit
                # ``array[0..N-1] of Byte = ($XX, $XX, ...)``.
                if g.init and g.init.startswith('('):
                    n_bytes = g.init.count('$')
                    if g.pascal_type == 'array of Byte':
                        pas_type = f'array[0..{n_bytes}-1] of Byte'
                    else:
                        pas_type = g.pascal_type
                    out.append(f"  {g.name}: {pas_type} = {g.init};")
                else:
                    out.append(f"  {g.name}: {g.pascal_type};")
            out.append("")
            self.stats["globals"] = len(seen_names)
        elif globals_list:
            out.append("var")
            seen_names: set[str] = set()
            for g in globals_list:
                # Strip leading storage-class keywords (static, extern,
                # const, volatile) from the type string. The pattern
                # extractor's regex includes them in ``type``, but
                # Pascal doesn't want them as part of the type name.
                # Example: ``static const SECTION union`` → ``union``,
                # ``static GStaticResource`` → ``GStaticResource``.
                type_str = g.type
                # Strip leading storage-class keywords one at a time.
                while True:
                    stripped = type_str.lstrip()
                    parts = stripped.split(None, 1)
                    if not parts:
                        break
                    if parts[0] in ('static', 'extern', 'const',
                                    'volatile', 'register', 'auto',
                                    'inline', 'restrict'):
                        type_str = parts[1] if len(parts) > 1 else ''
                        continue
                    break
                pas_type = c_type_to_pascal(type_str)
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
                    # ---- Byte-array initializer support for the
                    # pattern path. When the initializer looks like
                    # a C byte-array initializer (a sequence of
                    # string literals in braces), we expand it into
                    # a Pascal typed array constant. This handles
                    # ``const guint8 x[] = { "...", "..." };`` which
                    # the pattern path encounters when the AST path
                    # is unavailable (e.g. for icons.c if pycparser
                    # fails).
                    # We delegate to the AST translator's helper
                    # since the byte parsing logic is the same.
                    # We use ``g.init_text`` (recovered from the
                    # original source via brace matching) because the
                    # ``init`` from ``_split_name_init`` had its
                    # braces stripped.
                    raw_init = g.init_text or init
                    if raw_init:
                        # Try to convert C byte-array init to Pascal.
                        from ast_translator import (
                            _pattern_byte_array_init_to_pascal,
                        )
                        byte_init = _pattern_byte_array_init_to_pascal(raw_init)
                        if byte_init is not None:
                            n_bytes = byte_init.count('$')
                            if pas_type in ('Byte', 'guint8',
                                            'unsigned char'):
                                emit_type = (f'array[0..{n_bytes}-1] '
                                             f'of Byte')
                            else:
                                emit_type = pas_type
                            out.append(f"  {pascalify_name(name)}: "
                                        f"{emit_type} = {byte_init};")
                            continue
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

        # Inline struct global variables (from C file)
        if inline_var_decls:
            if not (self._ast_result is not None and self._ast_result.globals) \
                    and not globals_list:
                out.append("var")
            for name, ptype in inline_var_decls:
                out.append(f"  {pascalify_name(name)}: {ptype};")
            out.append("")

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
                except Exception as _body_err:
                    # Fall back to pattern-based body if AST translation
                    # fails for any reason. Common causes:
                    #   * the function body uses a C construct that the
                    #     AST translator doesn't handle yet (e.g. a
                    #     goto inside a nested compound, a label
                    #     attribute the AST walker doesn't recognise,
                    #     etc.) — this used to silently kill the whole
                    #     function body, leaving the user with a .pas
                    #     file that had "begin // empty body end;"
                    #   * pycparser's AST node type we don't have a
                    #     handler for (raises AttributeError when we
                    #     access a missing attribute).
                    # We surface the underlying error so the user can
                    # see WHY this particular function fell back — and
                    # the body_offset for the pattern-based translator
                    # is still computed so the TODO line numbers come
                    # out right.
                    import sys as _sys
                    print(
                        f"gtk2laz: AST body translation failed for "
                        f"{ast_fn.name}() in "
                        f"{os.path.basename(self.c_path)}: "
                        f"{type(_body_err).__name__}: {_body_err} — "
                        f"falling back to pattern-based body.",
                        file=_sys.stderr)
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
        # Restore the original c_path but DON'T delete the temp file
        # yet — the caller (convert_c_file) may need it for a second
        # translate() pass.  The caller is responsible for deleting
        # the temp file after both passes are done.
        # We keep ``self._orig_c_path`` set (NOT deleted) so the
        # caller can find the temp path in ``self.c_path`` and pass
        # it to the second translator.  The caller will set
        # ``_orig_c_path = None`` after it has copied the path.
        if hasattr(self, '_orig_c_path') and self._orig_c_path:
            # Save the temp path for the caller, then restore c_path.
            self._temp_expanded_path = self.c_path
            self.c_path = self._orig_c_path
            # Keep _orig_c_path so caller can detect it.
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
            # Skip our renamed-away macro definitions (their bodies were
            # manually inlined at every call site) and multi-line
            # statement macros whose value is just a line continuation
            # marker or contains braces/statements — these are NOT
            # Pascal constants (``#define X \ ... { ... }``).
            if name.startswith("__SKIP_"):
                continue
            if val.startswith("\\") or "{" in val or "}" in val or ";" in val:
                continue
            # Skip if value contains a function call or undefined identifier
            # we can't safely translate
            if re.search(r"\b[a-z_]\w*\s*\(", val):
                continue
            # Translate common C literal forms
            val = re.sub(r"\b0x([0-9A-Fa-f]+)\b", r"$\1", val)
            val = val.replace("TRUE", "True").replace("FALSE", "False")
            # Convert C string literals to Pascal string expressions.
            # We must handle escape sequences properly: a C literal like
            # ``"\n"`` should become Pascal ``#10`` (a 1-char string), not
            # ``'\n'`` (which in Pascal is the 2-char string ``\``+``n``).
            # Likewise ``"\t"`` -> ``#9``, ``"\r"`` -> ``#13`` and longer
            # strings such as ``"hello\n"`` -> ``'hello' + NL + ''``.
            # The unit emits ``const NL = #10;`` up-front (or the user's
            # source defines it themselves, as opgui/strings.c does) so
            # references to ``NL`` resolve to the same single-LF value.
            val = self._convert_c_string_in_define(val)
            # Skip values that still contain operators we don't handle
            if any(op in val for op in ["->", "<<", ">>"]):
                continue
            if name in seen:
                continue
            seen.add(name)
            out.append((name, val))
        return out

    @staticmethod
    def _convert_c_string_in_define(val: str) -> str:
        """Replace every double-quoted C string literal in ``val`` with
        the equivalent Pascal string expression, processing C escape
        sequences.  Non-string portions of ``val`` (numbers, identifiers,
        operators) are left untouched.

        Used by ``_extract_defines`` to translate ``#define`` values such
        as ``"\\n"`` or ``"hello\\nworld"`` into Pascal.

        Single newline escapes become ``' + NL + '`` joiners so the
        generated code reads naturally; a standalone ``"\\n"`` becomes
        the bare constant reference ``NL`` (rather than ``'' + NL + ''``)
        which matches how opgui/strings.c declares the ``NL`` macro.
        """
        out: list[str] = []
        i = 0
        n = len(val)
        while i < n:
            ch = val[i]
            if ch != '"':
                out.append(ch)
                i += 1
                continue
            # Find the closing quote (handle \" escapes)
            j = i + 1
            while j < n:
                if val[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if val[j] == '"':
                    break
                j += 1
            if j >= n:
                # Unterminated — append the rest verbatim
                out.append(val[i:])
                break
            inner = val[i + 1:j]
            i = j + 1
            # Split on \n so each piece becomes a single-quoted Pascal
            # fragment and the joiner is the NL constant.
            parts = inner.split("\\n")
            if len(parts) == 1 and parts[0] == "":
                # Empty C string "" -> '' in Pascal
                out.append("''")
                continue
            if len(parts) == 2 and parts[0] == "" and parts[1] == "":
                # Special case: standalone "\n" — translate to Pascal
                # char literal ``#10`` rather than ``NL``.  Using ``NL``
                # here would create a self-referential constant when the
                # very macro being defined IS ``NL`` (i.e. ``#define NL
                # "\n"`` -> ``const NL = NL;``).  ``#10`` is the same
                # single-LF value and is the form Pascal users expect for
                # a newline character constant.
                out.append("#10")
                continue
            # For each piece, process the remaining escapes and wrap in
            # single quotes.  Empty pieces become '' (which combines
            # harmlessly with the NL joiner).
            pas_parts = []
            for p in parts:
                p = p.replace("\\t", "' + #9 + '")
                p = p.replace("\\r", "' + #13 + '")
                p = p.replace("\\0", "' + #0 + '")
                p = p.replace('\\"', '"')
                p = p.replace("\\'", "''")
                p = p.replace("\\\\", "\\")
                pas_parts.append(f"'{p}'")
            out.append(" + NL + ".join(pas_parts))
        return "".join(out)

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
                   header_externs: list = None,
                   target_platform: str = "auto") -> dict:
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
    target_platform
        Target platform for ``#ifdef`` branch selection. One of
        ``"auto"``, ``"windows"``, ``"linux"``, ``"macos"``. See
        ``pcpp_preprocessor.PLATFORM_PRESETS`` for the exact macros
        each platform defines.
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
                                  header_externs=header_externs,
                                  target_platform=target_platform)
    text = translator.translate()
    # Compute header line count: count newlines up to "implementation\n"
    impl_idx = text.find("\nimplementation\n")
    if impl_idx >= 0:
        translator._header_line_count = text[:impl_idx].count("\n") + 2
    # Re-translate for accurate output_line in TodoItems
    # If the first translator created a temp expanded-source file
    # (for function-like macro inlining), reuse that same temp
    # file path so the second pass reads the same expanded source.
    translator2 = CFileTranslator(c_path, unit_name, form_unit,
                                   included_units=included_units,
                                   fake_include_dir=fake_include_dir,
                                   use_ast=use_ast,
                                   header_types=header_types,
                                   header_defines=header_defines,
                                   header_externs=header_externs,
                                   target_platform=target_platform)
    # Inherit the temp file path from the first translator so the
    # second pass reads the expanded source instead of the original.
    temp_path = getattr(translator, '_temp_expanded_path', None)
    if temp_path and os.path.isfile(temp_path):
        translator2.c_path = temp_path
        # Skip macro expansion on the second pass — the temp file is
        # already expanded, so _extract_func_macros would find nothing
        # and the expansion logic would be a no-op (but would still
        # create a new temp file and mess up the c_path).
        translator2._skip_macro_expansion = True
    translator2._header_line_count = translator._header_line_count
    text = translator2.translate()
    # Clean up the temp file after both passes are done.
    temp_path2 = getattr(translator2, '_temp_expanded_path', None) or temp_path
    if temp_path2:
        try:
            os.unlink(temp_path2)
        except OSError:
            pass
    os.makedirs(out_dir, exist_ok=True)
    # Emit the generated fake-type units (gtk_types, glib_types,
    # libc_types, sys_types) which collect all fake-header typedefs/
    # externs into a small number of shared units.  This avoids
    # duplicating the same typedefs in every converted .pas file.
    _emit_fake_type_units(out_dir, fake_include_dir)
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
            for t in typedefs:
                out.append(t)
            out.append("")
            self.stats["typedefs"] = len(typedefs)
        else:
            out.append("")

        # 3) extern globals -> var
        #    Call _extract_externs BEFORE emitting inline types so that
        #    _inline_types (from inline struct externs) is populated.
        externs = self._extract_externs(src_no_pp)
        self.extracted_externs = externs

        # Inline struct types from extern declarations
        # (e.g. ``extern struct var { ... } variables[0x200];``)
        if hasattr(self, '_inline_types') and self._inline_types:
            for t in self._inline_types:
                out.append(t)
            out.append("")

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

        def _find_typedef_block(text: str, keyword: str
                                ) -> list[tuple[int, int, str, str, str]]:
            """Find all ``typedef <keyword> [TAG] { ... } [ATTR] NAME;`` blocks.

            Returns a list of (start, end, body, name, attrs) tuples where:
              * start/end are character offsets into ``text`` (end is
                past the final ``;``)
              * body is the text between the outermost ``{`` and ``}``
              * name is the typedef name (e.g. ``filehdr_t``)
              * attrs is the attribute-macro text found between ``}``
                and the name (e.g. ``ENET_PACKED``), or ``""`` if none.
                The caller can use this to emit a comment so the user
                knows to add ``packed record`` in Pascal.

            Uses a custom scanner that handles nested braces (unlike
            ``re`` with ``[^}]*`` which stops at the first ``}``).
            """
            results: list[tuple[int, int, str, str, str]] = []
            # Find each ``typedef <keyword>`` occurrence.
            # We use ``[A-Za-z_]\w*`` (C identifier rule: first char
            # must be a letter or underscore, not a digit) instead of
            # ``\w+`` for the optional TAG name, because ``\w+`` would
            # also match numbers-only tokens which are not valid C
            # identifiers.
            for m in re.finditer(
                rf"\btypedef\s+{keyword}\s+(?:[A-Za-z_]\w*\s+)?",
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
                # The text after ``}`` may contain:
                #   1. Just the typedef name: ``} Name;``
                #   2. An attribute macro: ``} ENET_PACKED Name;``
                #      (ENET_PACKED is ``__attribute__((packed))`` on GCC)
                #   3. Multiple names: ``} A, B;`` (rare for structs)
                # We extract any leading attribute macros (ENET_PACKED,
                # __attribute__, __packed, etc.) so the regex below can
                # match the real typedef name. We ALSO return the
                # attribute text so the caller can emit a comment
                # (e.g. ``// ENET_PACKED — consider using packed record``)
                # so the user knows to add ``packed`` in Pascal.
                rest = text[body_end:].lstrip()
                # Collect attribute macros between ``}`` and the name.
                attr_re = re.compile(
                    r"^(?:"
                    r"ENET_PACKED"          # enet's packed macro
                    r"|__attribute__\s*\([^)]*\)"  # GCC attribute
                    r"|__packed"             # some compilers
                    r"|__declspec\s*\([^)]*\)"     # MSVC
                    r"|[A-Z_][A-Z_0-9]*_PACKED"    # *_PACKED macros
                    r"|[A-Z_][A-Z_0-9]*_ALIGNED"   # *_ALIGNED macros
                    r")\s+"
                )
                attrs_collected: list[str] = []
                while True:
                    m_attr = attr_re.match(rest)
                    if not m_attr:
                        break
                    attrs_collected.append(m_attr.group(0).strip())
                    rest = rest[m_attr.end():]
                # Match the typedef name using C identifier rules:
                # first char must be [A-Za-z_], not a digit. ``\w+``
                # would match ``123`` which is not a valid C identifier.
                name_match = re.match(r"([A-Za-z_]\w*)\s*;", rest)
                if not name_match:
                    continue
                name = name_match.group(1)
                block_end = body_end + (len(text[body_end:]) - len(rest)) + name_match.end()
                attrs_str = " ".join(attrs_collected) if attrs_collected else ""
                results.append((block_start, block_end, body, name, attrs_str))
            return results

        def _find_plain_block(text: str, keyword: str) -> list[tuple[int, int, str, str]]:
            """Find all ``<keyword> TAG { ... };`` blocks (no typedef).

            Returns a list of (start, end, body, tag) tuples.
            """
            results: list[tuple[int, int, str, str]] = []
            for m in re.finditer(
                rf"\b{keyword}\s+(?P<tag>[A-Za-z_]\w*)\s*",
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
                # After the closing brace, expect ``;`` (possibly after
                # an attribute macro like ``ENET_PACKED``).
                rest = text[body_end:].lstrip()
                # Skip attribute macros (same as in _find_typedef_block)
                attr_re = re.compile(
                    r"^(?:"
                    r"ENET_PACKED"
                    r"|__attribute__\s*\([^)]*\)"
                    r"|__packed"
                    r"|__declspec\s*\([^)]*\)"
                    r"|[A-Z_][A-Z_0-9]*_PACKED"
                    r"|[A-Z_][A-Z_0-9]*_ALIGNED"
                    r")\s+"
                )
                while True:
                    m_attr = attr_re.match(rest)
                    if not m_attr:
                        break
                    rest = rest[m_attr.end():]
                if not rest.startswith(";"):
                    continue
                tag = m.group("tag")
                block_end = body_end + (len(text[body_end:]) - len(rest)) + 1
                results.append((block_start, block_end, body, tag))
            return results

        # ---- typedef struct TAG? { ... } [ATTR] NAME; ----
        for start, end, body, name, attrs in _find_typedef_block(src, "struct"):
            if _is_consumed(start, end):
                continue
            pas_name = self._pascalize_type_name(name)
            if pas_name in declared_names:
                continue
            declared_names.add(pas_name)
            consumed_spans.append((start, end))
            # If the C struct had an attribute macro like ENET_PACKED,
            # emit a comment before the record so the user knows to
            # consider using ``packed record`` in Pascal. Pascal/FPC
            # supports ``packed record`` which disables compiler
            # alignment padding — equivalent to C's
            # ``__attribute__((packed))``. We DON'T automatically add
            # ``packed`` because it changes field offsets and the user
            # should decide whether the Pascal code needs it.
            if attrs:
                out.append(f"  // C attribute: {attrs}")
                out.append(f"  // Consider using 'packed record' if "
                            f"field layout must match the C struct.")
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
            # ---- Auto-generate a pointer-type alias (P-prefix).
            if pas_name.startswith("T") and len(pas_name) > 1 \
                    and pas_name[1].isupper():
                ptr_name = "P" + pas_name[1:]
            else:
                ptr_name = "P" + pas_name
            if ptr_name not in declared_names:
                declared_names.add(ptr_name)
                out.append(f"  {ptr_name} = ^{pas_name};")

        # ---- typedef enum TAG? { ... } [ATTR] NAME; ----
        for start, end, body, name, attrs in _find_typedef_block(src, "enum"):
            if _is_consumed(start, end):
                continue
            pas_name = self._pascalize_type_name(name)
            if pas_name in declared_names:
                continue
            declared_names.add(pas_name)
            consumed_spans.append((start, end))
            if attrs:
                out.append(f"  // C attribute: {attrs}")
            members = self._parse_enum_members(body)
            if members:
                out.append(f"  {pas_name} = ({', '.join(members)});")
            else:
                out.append(f"  (* TODO gtk2laz: empty enum {pas_name} *)")
            # ---- Auto-generate a pointer-type alias for enums too.
            # (Same convention as for structs — see above.)
            if pas_name.startswith("T") and len(pas_name) > 1 \
                    and pas_name[1].isupper():
                ptr_name = "P" + pas_name[1:]
            else:
                ptr_name = "P" + pas_name
            if ptr_name not in declared_names:
                declared_names.add(ptr_name)
                out.append(f"  {ptr_name} = ^{pas_name};")

        # ---- typedef union TAG? { ... } [ATTR] NAME; ----
        # C unions map to Pascal variant records, but variant records
        # require a case tag which the C union does not have. We
        # convert the union to a Pascal record (all fields at offset 0
        # via ``case`` of a dummy tag) so the field names are at least
        # available. The user should review and convert to a proper
        # variant record by hand if needed.
        for start, end, body, name, attrs in _find_typedef_block(src, "union"):
            if _is_consumed(start, end):
                continue
            pas_name = self._pascalize_type_name(name)
            if pas_name in declared_names:
                continue
            declared_names.add(pas_name)
            consumed_spans.append((start, end))
            # Emit attribute comment (ENET_PACKED etc.)
            if attrs:
                out.append(f"  // C attribute: {attrs}")
                out.append(f"  // Consider using 'packed record' if "
                            f"field layout must match the C union.")
            # Emit a comment that this was a C union — Pascal variant
            # record conversion is left to the user.
            out.append(f"  // C union — consider converting to Pascal "
                        f"variant record (case of).")
            fields = self._parse_struct_fields(body)
            if not fields:
                out.append(f"  {pas_name} = record")
                out.append(f"    (* TODO gtk2laz: empty union body *)")
                out.append(f"  end;")
            else:
                # Emit as a record with all fields. In Pascal, a union
                # is best expressed as a variant record:
                #   TName = record
                #     case Integer of
                #       0: (field1: Type1);
                #       1: (field2: Type2);
                #     end;
                # but that requires the user to pick a tag type. We
                # emit a simple record with all fields overlaid (which
                # is NOT correct Pascal — fields in a record are
                # sequential, not overlapping). The user must convert
                # this to a variant record by hand.
                out.append(f"  {pas_name} = record")
                seen = set()
                for fname, ftype in fields:
                    if fname in seen:
                        continue
                    seen.add(fname)
                    out.append(f"    {pascalify_name(fname)}: {ftype};")
                out.append(f"  end;")
            # ---- Auto-generate a pointer-type alias (P-prefix).
            if pas_name.startswith("T") and len(pas_name) > 1 \
                    and pas_name[1].isupper():
                ptr_name = "P" + pas_name[1:]
            else:
                ptr_name = "P" + pas_name
            if ptr_name not in declared_names:
                declared_names.add(ptr_name)
                out.append(f"  {ptr_name} = ^{pas_name};")

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
            # ---- Auto-generate a pointer-type alias (same as typedef struct).
            if pas_name.startswith("T") and len(pas_name) > 1 \
                    and pas_name[1].isupper():
                ptr_name = "P" + pas_name[1:]
            else:
                ptr_name = "P" + pas_name
            if ptr_name not in declared_names:
                declared_names.add(ptr_name)
                out.append(f"  {ptr_name} = ^{pas_name};")

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

        # ---- ANONYMOUS enum { ... }; ----
        # Many C headers use anonymous enums to define constants:
        #   enum { FOO = 1, BAR = 2, BAZ = 4 };
        # These are NOT typedef'd and have no tag name. We extract each
        # member as a Pascal ``const`` so the user can reference the
        # constants by name. Without this, every member would become a
        # separate "header line not translated" TODO.
        for m in re.finditer(
            r'\benum\s*\{',
            src, re.DOTALL,
        ):
            if _is_consumed(m.start(), m.end()):
                continue
            block_start = m.start()
            brace_pos = m.end() - 1  # position of the `{`
            body_end = _match_braced_block(src, brace_pos)
            if body_end < 0:
                continue
            body = src[brace_pos + 1:body_end - 1]
            # After the closing brace, expect ``;`` (it's a statement)
            rest = src[body_end:].lstrip()
            if not rest.startswith(";"):
                continue
            block_end = body_end + (len(src[body_end:]) - len(rest)) + 1
            consumed_spans.append((block_start, block_end))
            # Parse the enum members and emit them as Pascal consts.
            # Each member is ``NAME = value,`` or just ``NAME,``.
            members = self._parse_enum_members(body)
            for member in members:
                # ``member`` is just the name (e.g. ``FOO``). We need
                # to find the value from the body to emit it as a
                # const. For anonymous enums, we emit the member names
                # as a Pascal enumerated type so they are at least
                # available — the user can convert them to consts by
                # hand if needed.
                pass
            if members:
                # Emit as a Pascal enumerated type with a unique name.
                # We use a counter to avoid name collisions.
                anon_name = f"_AnonymousEnum_{len(declared_names)}"
                declared_names.add(anon_name)
                out.append(f"  {anon_name} = ({', '.join(members)});")
            # No else — if the body is empty, we just consume the block
            # silently (no TODO needed for an empty anonymous enum).

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

            # ---- Function pointer fields ----
            # C pattern: ``RET_TYPE (* NAME)(PARAMS)``
            # The calling convention is encoded in the field name as
            # a ``__callconv_<conv>`` suffix (e.g. ``malloc__callconv_cdecl``).
            # This was inserted by pcpp_preprocessor.preprocess() to
            # preserve the calling convention through pycparser.
            # Pascal: function pointer → procedural type with calling
            # convention modifier.
            # e.g. ``malloc: function(size: PtrUInt): Pointer; cdecl``
            m_fp = re.match(
                r"^(?P<ret_type>[\w\s\*]+?)\s*"
                r"\(\s*\*\s*(?P<name>\w+)(__callconv_(?P<callconv>\w+))?\s*\)"
                r"\s*\(\s*(?P<params>.*?)\s*\)\s*$",
                decl, re.DOTALL,
            )
            if m_fp:
                ret_type = m_fp.group("ret_type").strip()
                # Strip the __callconv_ suffix from the field name
                name = m_fp.group("name")
                callconv = m_fp.group("callconv") or ""
                params_raw = m_fp.group("params").strip()
                # Convert return type
                pas_ret = c_type_to_pascal(ret_type)
                # Convert params: "size_t size" → "size: PtrUInt"
                pas_params = ""
                if params_raw and params_raw != "void":
                    param_list = []
                    for p in _split_top_level(params_raw, ","):
                        p = p.strip()
                        if not p:
                            continue
                        # "size_t size" → type="size_t", name="size"
                        # "void * memory" → type="void *", name="memory"
                        pm = re.match(
                            r"^(?P<ptype>[\w\s\*]+?)\s+(?P<pname>\w+)\s*$",
                            p,
                        )
                        if pm:
                            pt = c_type_to_pascal(pm.group("ptype").strip())
                            pn = pascalify_name(pm.group("pname"))
                            param_list.append(f"{pn}: {pt}")
                        else:
                            # Type-only param (no name)
                            pt = c_type_to_pascal(p)
                            param_list.append(f"arg: {pt}")
                    pas_params = "; ".join(param_list)
                # Build Pascal procedural type.
                # C ``void`` return → Pascal ``procedure`` (no return).
                # C ``void *`` return → Pascal ``function(...): Pointer``.
                # C ``int`` return → Pascal ``function(...): Integer``.
                # Calling convention (cdecl, stdcall, etc.) is appended
                # after the declaration so Pascal knows how to call it.
                callconv_suffix = f" {callconv}" if callconv else ""
                if ret_type.strip() == "void":
                    field_type = f"procedure({pas_params}){callconv_suffix}"
                else:
                    pas_ret = c_type_to_pascal(ret_type)
                    field_type = f"function({pas_params}): {pas_ret}{callconv_suffix}"
                out.append((name, field_type))
                continue

            # ---- Regular TYPE NAME pattern ----
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

        Also handles inline struct definitions:
        ``extern struct TAG { fields... } NAME[SIZE];``

        Returns list of (name, pascal_type).
        """
        out: list[tuple[str, str]] = []

        # ---- First pass: handle inline struct extern declarations ----
        # Pattern: extern struct TAG { ... } NAME[SIZE];  or
        #          extern struct TAG { ... } NAME;
        # We extract the struct as a Pascal record type and the variable
        # as an array/variable of that type.
        for m in re.finditer(
            r"extern\s+struct\s+(\w+)\s*\{([^}]*)\}\s*"
            r"(\w+)\s*(?:\[([^\]]*)\])?\s*;",
            src, re.DOTALL,
        ):
            tag = m.group(1)
            body = m.group(2)
            var_name = m.group(3)
            arr_size = m.group(4)
            # Create a Pascal record type for this inline struct
            pas_type_name = self._pascalize_type_name(tag)
            # Emit the type declaration via _extract_typedefs-like logic
            # by adding it to the output (we piggyback on the typedef
            # infrastructure by emitting the record here).
            # We store it as a special extern with a type prefix.
            fields = self._parse_struct_fields(body)
            if fields:
                # Build the record type string
                type_decl = [f"  {pas_type_name} = record"]
                seen = set()
                for fname, ftype in fields:
                    if fname in seen:
                        continue
                    seen.add(fname)
                    type_decl.append(
                        f"    {pascalify_name(fname)}: {ftype};"
                    )
                type_decl.append("  end;")
                # Store the type declaration so it can be emitted
                if not hasattr(self, '_inline_types'):
                    self._inline_types = []
                self._inline_types.extend(type_decl)
            # Determine the variable's Pascal type
            if arr_size:
                # Convert hex literal in array size
                arr_size_pas = arr_size
                m_hex = re.match(r"^0[xX]([0-9A-Fa-f]+)$", arr_size.strip())
                if m_hex:
                    arr_size_pas = "$" + m_hex.group(1)
                out.append((var_name, f"array[0..{arr_size_pas}-1] of {pas_type_name}"))
            else:
                out.append((var_name, pas_type_name))
            # Mark this span as consumed so _leftover_lines skips it
            if not hasattr(self, '_inline_consumed'):
                self._inline_consumed = []
            self._inline_consumed.append((m.start(), m.end()))

        # ---- Second pass: regular extern declarations ----
        # extern TYPE NAME;  or  extern TYPE *NAME;  or  extern TYPE NAME[N];
        for m in re.finditer(
            r"extern\s+(?P<type>(?:const\s+|volatile\s+)*"
            r"[\w]+(?:\s+\w+)*\s*\**)\s+"
            r"(?P<decl>[^;]+);",
            src,
        ):
            # Skip if already consumed by inline struct pass
            if hasattr(self, '_inline_consumed'):
                if any(m.start() >= cs and m.end() <= ce
                       for cs, ce in self._inline_consumed):
                    continue
            # Skip if this is an inline struct (handled above)
            if 'struct' in m.group(0).split('{')[0] and '{' in m.group(0):
                continue
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
            # Strip storage-class specifiers (extern, static, inline,
            # volatile, const) from the return type so they don't
            # become part of the Pascal type name.  For example,
            # ``extern int enet_initialize(void)`` has ret = "extern int"
            # which would produce ``Textern int`` if we don't strip
            # the ``extern``.  We only keep the actual type words.
            ret_words = ret.split()
            storage_classes = {"extern", "static", "inline", "volatile",
                               "const", "register", "auto", "restrict"}
            ret_words = [w for w in ret_words if w not in storage_classes]
            # Also strip API-export macros that the preprocessor would
            # normally expand (ENET_API, API, EXPORT, DLLEXPORT, etc.).
            # When the HeaderTranslator processes the raw (un-preprocessed)
            # source, these macros appear as identifiers in the return-type
            # position (e.g. ``ENET_API int enet_initialize(void)`` -> ret =
            # "ENET_API int").  Without stripping them, c_type_to_pascal would
            # produce ``TENET_API int`` as the Pascal return type.  We strip
            # any token that is ALL-CAPS and looks like a macro name (starts
            # with an underscore or uppercase letter, contains only
            # uppercase letters/digits/underscores, and is NOT a known C type
            # keyword like ``INT``, ``VOID``, etc. — though in practice C
            # type keywords are lowercase so this is safe).
            known_type_keywords = {
                "int", "char", "short", "long", "float", "double",
                "void", "unsigned", "signed", "size_t", "ssize_t",
                "FILE", "time_t", "clock_t",
            }
            ret_words = [w for w in ret_words
                         if w in known_type_keywords
                         or not (re.match(r"^[A-Z_][A-Z0-9_]*$", w)
                                 and w not in known_type_keywords
                                 and len(w) > 1)]
            ret = " ".join(ret_words)
            if not ret:
                continue
            if ret.split()[-1] in _C_KEYWORDS:
                continue
            # Map merged offsets back to original src offsets
            orig_start, orig_end = _merged_to_orig(m.start(), m.end())
            consumed_spans.append((orig_start, orig_end))
            params_raw = m.group("params").strip()
            params = []
            # Counter for generating automatic parameter names when
            # the C prototype omits them (e.g. ``enet_socket_create(ENetSocketType)``
            # — here ``ENetSocketType`` is a type with no parameter name).
            # We generate ``arg1``, ``arg2``, ... so the Pascal
            # declaration has a valid name in each parameter slot.
            auto_arg_idx = 0
            for p in _split_top_level(params_raw):
                p = p.strip()
                if not p or p == "void":
                    continue
                ptype, pname, _ = _parse_param(p)
                # Determine if this is a type-only parameter (name
                # omitted in the C prototype).  When ``_parse_param``
                # returns a non-empty ``pname`` but the original
                # parameter string is a single token (no spaces, no
                # ``*``), the ``pname`` is actually the type name, not
                # a parameter name.  For example:
                #   enet_time_set(enet_uint32) -> _parse_param returns
                #   type='', name='enet_uint32' — but 'enet_uint32' is
                # the TYPE, not a name.  We detect this case by
                # checking if the raw param is a single identifier
                # (no whitespace, no ``*``) AND the identifier looks
                # like a type name (starts with an uppercase letter
                # for typedef'd types, or is a known C type keyword,
                # or ends with ``_t`` / ``_uint`` / ``_int`` suffixes
                # common in C99 and GLib typedefs).
                raw_stripped = p.strip()
                is_single_token = (
                    len(raw_stripped.split()) == 1
                    and "*" not in raw_stripped
                )
                looks_like_type = False
                if is_single_token and pname:
                    # Check if the "name" looks like a type name
                    # rather than a parameter name.  Type names tend
                    # to start with an uppercase letter (typedef'd
                    # types like ``ENetSocket``, ``GtkWidget``), be all
                    # lowercase C keywords (``int``, ``char``, ``void``),
                    # or end with ``_t`` / ``_uint32`` / ``_int16`` etc.
                    C_TYPE_KEYWORDS_LOCAL = {
                        "int", "char", "short", "long", "float",
                        "double", "void", "unsigned", "signed",
                        "size_t", "ssize_t", "time_t", "clock_t",
                        "FILE", "wchar_t", "wint_t",
                    }
                    looks_like_type = (
                        pname in C_TYPE_KEYWORDS_LOCAL
                        or (pname[0].isupper() and not pname.islower())
                        or pname.endswith(("_t", "_uint8", "_uint16",
                                          "_uint32", "_uint64",
                                          "_int8", "_int16",
                                          "_int32", "_int64"))
                    )
                if looks_like_type:
                    # The parameter is type-only (name omitted in C).
                    # Move the "name" back to the type and generate
                    # an automatic parameter name.
                    ptype = pname
                    pname = ""
                if not ptype:
                    # _parse_param couldn't parse anything useful;
                    # skip this parameter.
                    continue
                if not pname:
                    # Generate an automatic name (arg1, arg2, ...).
                    auto_arg_idx += 1
                    pname = f"arg{auto_arg_idx}"
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
            # Skip standalone braces — these are typically the
            # opening/closing braces of ``enum { ... };``,
            # ``struct { ... };``, or ``extern "C" { ... }`` blocks.
            # The content inside the block is handled by _extract_typedefs
            # (for named typedefs) or by the enum-constant extraction
            # below. Leaving bare ``{`` or ``}`` as leftover would
            # produce spurious TODOs for every enum/struct block.
            if stripped in ("{", "}", "};", "{ }", "{}"):
                continue
            # Skip ``extern "C"`` linkage specifiers (C++ only, but
            # some C headers include them unconditionally).
            if 'extern "C"' in stripped or "extern 'C'" in stripped:
                continue
            # Skip attribute macros like ``ENET_PACKED`` that appear
            # between ``}`` and the typedef name (or between ``}`` and
            # ``;`` for plain struct blocks). These are C compiler
            # attributes (e.g. ``__attribute__((packed))``) wrapped in
            # a macro; Pascal has no equivalent so we drop them. The
            # struct/enum typedef itself is handled by
            # _extract_typedefs, which now skips these macros too.
            if re.match(r"^[A-Z_][A-Z_0-9]*_PACKED\s*;?\s*$", stripped):
                continue
            if re.match(r"^[A-Z_][A-Z_0-9]*_ALIGNED\s*;?\s*$", stripped):
                continue
            if stripped.startswith("__attribute__") or \
                    stripped.startswith("__packed") or \
                    stripped.startswith("__declspec"):
                continue
            if re.match(r"^[\w\s\*]+\s+\w+(\s*\[[^\]]*\])?\s*;", stripped):
                continue
            # Skip enum member lines like ``ENET_HOST_RECEIVE_BUFFER_SIZE = 256 * 1024,``
            # These are the individual constants inside an anonymous
            # ``enum { ... };`` block. They should have been extracted
            # as Pascal constants by the enum-constant handler; if they
            # survived as leftover, it's because the anonymous enum was
            # not recognised. We skip them here to avoid producing a
            # TODO for every single enum member.
            if re.match(r"^\w+\s*=\s*.+,\s*$", stripped):
                continue
            # Also skip the last enum member (no trailing comma):
            # ``ENET_PROTOCOL_MAXIMUM_FRAGMENT_COUNT = 1024 * 1024``
            if re.match(r"^\w+\s*=\s*[^;]+$", stripped) \
                    and "=" in stripped and not "(" in stripped:
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

    NOTE: The on-disk fake_libc_include directory is only used as a
    FALLBACK for pcpp's ``on_include_not_found`` (which needs a file
    path). The primary source of fake-header content is the in-memory
    ``gen_fake_includes.HEADERS`` dict, which is always up to date with
    the running gtk2laz version (see ``_read_all_fake_headers`` in
    ``pcpp_preprocessor.py``). This means we don't need to regenerate
    the on-disk directory every time gtk2laz is updated — if the dict
    has the headers, AST parsing works even if the on-disk directory
    is stale or missing newer headers.

    We still generate the on-disk directory once (on first use) so
    that pcpp's include resolver has real files to return for
    ``#include <stdio.h>`` etc. — but after that we don't check for
    staleness, because the dict is the authoritative source.
    """
    global _FAKE_INCLUDES_READY
    if _FAKE_INCLUDES_READY:
        return _FAKE_INCLUDE_DIR
    # ALWAYS regenerate the on-disk fake headers. We used to check for
    # a sentinel file (gtk/gtk.h) and skip regeneration if it existed,
    # but that caused a critical bug: stale on-disk headers from an
    # older version (with \\r\\n line endings or ``long long`` typedefs)
    # would be read by pcpp and passed to pycparser, causing "Invalid
    # declaration" or "Illegal character" errors. By always
    # regenerating, we ensure the on-disk files always match the
    # current gen_fake_includes.py (which uses ``newline=""`` and
    # ``long`` instead of ``long long``).
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


# ---------------------------------------------------------------------------
# Fake-header type units
# ---------------------------------------------------------------------------
# The fake libc/GTK stub headers declare many typedefs (GtkWidget,
# gint, gpointer, fd_set, ...) that the user's C code references.
# Rather than emitting these declarations in EVERY converted .pas
# unit (which produces massive duplication), we emit them in a small
# number of dedicated units and add those units to every .pas file's
# ``uses`` clause.
#
# Naming: to avoid clashing with the user's own header-derived units
# (which are named after the user's .h files — e.g. ``common``,
# ``icd``, ``strings``), we group the fake headers into a few
# well-known categories and emit one unit per category with a
# ``_types`` suffix:
#
#   gtk/gtk.h                       ->  gtk_types
#   glib.h, glib/*, gio/*           ->  glib_types
#   sys/* (ioctl, select, stat, ...) ->  sys_types
#   everything else (stdio, string, math, ...) -> libc_types
#
# These names never collide with the user's headers because the
# user's headers produce units named after the .h basename (e.g.
# ``common`` from common.h, ``icd`` from icd.h) — none of which end
# in ``_types``.  The ``_types`` suffix is the convention we use to
# mark generated type-only units.

# Cache the generated unit contents so we only build them once per
# session (the fake headers don't change between files).
_FAKE_TYPE_UNITS_CACHE: Optional[dict[str, str]] = None


# Map: fake header file path (relative to fake_include_dir) -> unit
# name.  This is how we decide which generated unit a given fake
# header's declarations belong to.
def _classify_fake_header(rel_path: str) -> str:
    """Return the Pascal unit name that should hold the declarations
    from the fake header at ``rel_path`` (relative to
    ``fake_include_dir``).

    The classification is purely path-based so it is deterministic
    and easy to reason about:

      * ``gtk/gtk.h``                       -> ``gtk_types``
      * ``glib.h`` / ``glib/*`` / ``gio/*`` -> ``glib_types``
      * ``sys/*``                           -> ``sys_types``
      * everything else                     -> ``libc_types``
    """
    # Normalize to forward slashes.
    p = rel_path.replace("\\", "/").lstrip("/")
    if p == "gtk/gtk.h" or p.startswith("gtk/"):
        return "gtk_types"
    if p == "glib.h" or p.startswith("glib/") or p.startswith("gio/"):
        return "glib_types"
    if p.startswith("sys/"):
        return "sys_types"
    return "libc_types"


def _build_fake_type_units(fake_include_dir: str) -> dict[str, str]:
    """Build the contents of all generated fake-type units.

    Returns a dict mapping unit name -> Pascal unit text.  The unit
    text does NOT include a trailing newline; the caller adds one
    when writing.
    """
    global _FAKE_TYPE_UNITS_CACHE
    if _FAKE_TYPE_UNITS_CACHE is not None:
        return _FAKE_TYPE_UNITS_CACHE
    if not fake_include_dir or not os.path.isdir(fake_include_dir):
        _FAKE_TYPE_UNITS_CACHE = {}
        return {}
    # Walk the fake-include dir and group each .h file by its
    # target unit name.  We keep the per-header content so we can
    # emit ``#line 1 "<header>.h"`` markers in the concatenated text
    # (helps debugging, though the generated units do not use
    # pycparser's coord filtering).
    groups: dict[str, list[tuple[str, str]]] = {}
    for root, _dirs, files in os.walk(fake_include_dir):
        for fname in sorted(files):
            if not fname.endswith(".h"):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, fake_include_dir)
            unit_name = _classify_fake_header(rel)
            try:
                with open(fpath, "r", encoding="utf-8",
                          errors="replace") as f:
                    content = f.read()
            except OSError:
                continue
            groups.setdefault(unit_name, []).append((fname, content))
    # Build one Pascal unit per group.
    result: dict[str, str] = {}
    for unit_name, headers in sorted(groups.items()):
        text = _build_one_fake_type_unit(unit_name, headers)
        if text:
            result[unit_name] = text
    _FAKE_TYPE_UNITS_CACHE = result
    return result


def _build_one_fake_type_unit(unit_name: str,
                              headers: list[tuple[str, str]]) -> str:
    """Build the Pascal source text for a single fake-type unit.

    ``headers`` is a list of ``(header_basename, content)`` pairs
    whose declarations should all go into this unit.
    """
    # Concatenate all headers' content, stripping preprocessor
    # directives and comments so pycparser can parse the result.
    import re as _re_ft
    cleaned_parts: list[str] = []
    for _hdr_name, content in headers:
        lines = content.splitlines()
        kept = []
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("#"):
                # Drop #ifndef / #define / #endif / #include so
                # pycparser doesn't abort with "Directives not
                # supported yet".  We replace with a blank line to
                # preserve line numbering.
                kept.append("")
            else:
                kept.append(line)
        cleaned_parts.append("\n".join(kept))
    cleaned = "\n".join(cleaned_parts)
    # Strip block comments and line comments.
    cleaned = _re_ft.sub(r"/\*.*?\*/", "", cleaned, flags=_re_ft.DOTALL)
    cleaned = _re_ft.sub(r"//[^\n]*", "", cleaned)
    # Parse with pycparser and collect typedef/struct/enum/extern/func.
    types_decls: list[str] = []
    var_decls: list[str] = []
    func_decls: list[str] = []
    # Build a typedef-name → Pascal-type map so later typedefs in
    # the SAME fake-header unit can flatten typedef-to-typedef
    # chains. For example, in windows.h we have:
    #     typedef int wchar_t;  (from wchar.h, prepended by _read_all_fake_headers)
    #     typedef wchar_t WCHAR;
    # Without flattening, the emitted Pascal would be:
    #     Twchar_t = Integer;
    #     TWCHAR = Twchar_t;        <- references Twchar_t
    # With flattening, the second typedef uses the resolved base
    # type directly:
    #     TWCHAR = Integer;         <- flattened chain
    # The chain is local to this generated unit — typedefs from
    # OTHER units (e.g. user's .c file) are not in scope and stay
    # as Twchar_t-style references.
    typedef_chain: dict[str, str] = {}
    try:
        from ast_translator import (
            _type_to_pascal, _typedef_to_pas,
            _decl_to_func_signature, _pascalize_type,
        )
        from pycparser import CParser, c_ast
        parser = CParser()
        ast = parser.parse(cleaned, filename=f"<fake:{unit_name}>")
        for ext in ast.ext:
            if isinstance(ext, c_ast.Typedef):
                if ext.name and ext.name.startswith("_"):
                    continue  # skip internal tags
                td = _typedef_to_pas(ext, typedef_chain)
                if td and td.pascal_decl:
                    types_decls.append(td.pascal_decl)
                    # Record the resolved Pascal type so later
                    # typedefs in the same unit can flatten to it.
                    decl_str = td.pascal_decl.strip()
                    if "=" in decl_str:
                        rhs = (decl_str.split("=", 1)[1]
                               .strip().rstrip(";").strip())
                        if td.kind == "typedef":
                            typedef_chain[td.name] = rhs
                        else:
                            typedef_chain[td.name] = _pascalize_type(td.name)
                else:
                    pas_type = _type_to_pascal(ext.type)
                    name = ext.name
                    if name and pas_type:
                        decl_line = f"  T{name} = {pas_type};"
                        types_decls.append(decl_line)
                        typedef_chain[name] = pas_type
            elif isinstance(ext, c_ast.Decl):
                if ext.storage and "extern" in ext.storage:
                    pas_type = _type_to_pascal(ext.type)
                    if pas_type and ext.name:
                        var_decls.append(f"  {ext.name}: {pas_type};")
                elif ext.type and hasattr(ext.type, "args"):
                    fn_name = ext.name or ""
                    if not fn_name:
                        continue
                    fn = _decl_to_func_signature(ext)
                    if fn:
                        ret = fn.return_type.strip()
                        is_func = ret and ret != "void"
                        params = []
                        for ptype, pname, _ in fn.params:
                            if not pname:
                                continue
                            try:
                                if isinstance(ptype, str):
                                    pas_p_type = _type_to_pascal(
                                        c_ast.IdentifierType(names=[ptype]))
                                else:
                                    pas_p_type = _type_to_pascal(ptype)
                                if pas_p_type:
                                    params.append(f"{pname}: {pas_p_type}")
                            except Exception:
                                pass
                        params_str = "; ".join(params)
                        prefix = "function" if is_func else "procedure"
                        sig = f"{prefix} {fn_name}"
                        if params_str:
                            sig += f"({params_str})"
                        if is_func:
                            try:
                                sig += f": {_type_to_pascal(c_ast.IdentifierType(names=[ret]))}"
                            except Exception:
                                sig += ": Integer"
                        func_decls.append(f"  {sig};")
    except Exception:
        pass
    # Build the Pascal unit text.
    out: list[str] = []
    out.append(f"unit {unit_name};")
    out.append("")
    out.append("{$mode objfpc}{$H+}")
    out.append("")
    out.append("interface")
    out.append("")
    out.append("uses")
    out.append("  Classes, SysUtils;")
    out.append("")
    if types_decls:
        out.append("type")
        seen: set[str] = set()
        for decl in types_decls:
            m_name = re.match(r"\s*T?(\w+)\s*=", decl)
            if m_name and m_name.group(1) in seen:
                continue
            if m_name:
                seen.add(m_name.group(1))
            out.append(decl)
        out.append("")
    if var_decls:
        out.append("var")
        seen_v: set[str] = set()
        for decl in var_decls:
            m_name = re.match(r"\s*(\w+)\s*:", decl)
            if m_name and m_name.group(1) in seen_v:
                continue
            if m_name:
                seen_v.add(m_name.group(1))
            out.append(decl)
        out.append("")
    if func_decls:
        out.append("// --- function declarations ---")
        seen_f: set[str] = set()
        for decl in func_decls:
            m_name = re.match(r"\s*(?:function|procedure)\s+(\w+)", decl)
            if m_name and m_name.group(1) in seen_f:
                continue
            if m_name:
                seen_f.add(m_name.group(1))
            out.append(decl)
        out.append("")
    out.append("implementation")
    out.append("")
    out.append("end.")
    out.append("")
    return "\n".join(out)


def _emit_fake_type_units(out_dir: str,
                          fake_include_dir: str) -> list[str]:
    """Write all generated fake-type units to ``out_dir``.

    Returns the list of unit names that were written (e.g.
    ``["gtk_types", "glib_types", "libc_types", "sys_types"]``).
    The caller adds these to every converted .pas file's ``uses``
    clause.
    """
    units = _build_fake_type_units(fake_include_dir)
    if not units:
        return []
    written: list[str] = []
    for unit_name, content in units.items():
        path = os.path.join(out_dir, unit_name + ".pas")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            written.append(unit_name)
        except OSError:
            continue
    return written


def _fake_type_unit_names(fake_include_dir: str) -> list[str]:
    """Return the list of generated fake-type unit names (without
    writing them to disk).  Used by ``CFileTranslator.translate()`` to
    add the units to the ``uses`` clause even on the first pass
    (before ``_emit_fake_type_units`` has been called).
    """
    units = _build_fake_type_units(fake_include_dir)
    return sorted(units.keys())


# ---------------------------------------------------------------------------
# Spaced-operator normalisation
# ---------------------------------------------------------------------------
# Some C code writes multi-char operators with spaces inside them:
#   peer -> field      (instead of peer->field)
#   a < < b            (instead of a << b)
#   a > > b            (instead of a >> b)
#   a & & b            (instead of a && b)
#   a | | b            (instead of a || b)
#   a = = b            (instead of a == b)
#   a ! = b            (instead of a != b)
#   a < = b            (instead of a <= b)
#   a > = b            (instead of a >= b)
#
# C treats these the same as the unspaced forms, but our _OP_REPLACE
# table only matches the unspaced tokens.  This helper collapses the
# spaced forms to their unspaced equivalents OUTSIDE string/char
# literals so _OP_REPLACE can find them.

# Map of spaced-operator patterns to their unspaced equivalents.
# We use a regex per operator so we can match the exact spaced form
# (including the required whitespace) without false-matching on
# expressions like ``a < b`` (single ``<`` is a comparison, not part
# of ``<<``).
_SPACED_OP_PATTERNS: list[tuple[str, str]] = [
    # Two-char operators with a space between the two chars.
    # We require at least one whitespace char between the two
    # halves so we don't accidentally match the unspaced form (which
    # is already correct).
    (r"-\s+>", "->"),        # - >  ->  ->
    (r"<\s+<", "<<"),        # < <  ->  <<
    (r">\s+>", ">>"),        # > >  ->  >>
    (r"&\s+&", "&&"),        # & &  ->  &&
    (r"\|\s+\|", "||"),      # | |  ->  ||
    (r"=\s+=", "=="),        # = =  ->  ==
    (r"!\s+=", "!="),        # ! =  ->  !=
    (r"<\s+=", "<="),        # < =  ->  <=
    (r">\s+=", ">="),        # > =  ->  >=
]


def _normalise_spaced_operators(s: str) -> str:
    """Collapse spaced multi-char operators to their unspaced forms,
    skipping string and char literals.

    For example, ``peer - > field`` becomes ``peer->field`` so the
    downstream ``_OP_REPLACE`` table can match the ``->`` token and
    convert it to ``^.``.
    """
    if not s:
        return s
    out: list[str] = []
    i = 0
    n = len(s)
    in_str = False
    in_char = False
    str_escape = False
    while i < n:
        ch = s[i]
        if in_str:
            out.append(ch)
            if str_escape:
                str_escape = False
            elif ch == "\\":
                str_escape = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if in_char:
            out.append(ch)
            if str_escape:
                str_escape = False
            elif ch == "\\":
                str_escape = True
            elif ch == "'":
                in_char = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "'":
            in_char = True
            out.append(ch)
            i += 1
            continue
        # Try to match a spaced operator at the current position.
        matched = False
        for pattern, replacement in _SPACED_OP_PATTERNS:
            m = re.match(pattern, s[i:])
            if m:
                out.append(replacement)
                i += m.end()
                matched = True
                break
        if matched:
            continue
        out.append(ch)
        i += 1
    return "".join(out)
