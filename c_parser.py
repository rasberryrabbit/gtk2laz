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
from dataclasses import dataclass, field
from typing import Optional

from widget_map import (
    c_type_to_pascal, lookup_gtk_call, lookup_c_lib,
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
    """Find top-level variable declarations (after functions removed)."""
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

    # Operators that need to be replaced in expressions
    _OP_REPLACE = [
        ("&&", " and "), ("||", " or "), ("!", " not "),
        ("==", " = "), ("!=", " <> "),
        ("<<", " shl "), (">>", " shr "),
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

    # ------------------------------------------------------------------
    def translate(self) -> list[str]:
        body = self.func.body
        # Split body into statements by ;, respecting nesting
        statements = self._split_statements(body)
        for stmt in statements:
            stmt = stmt.strip()
            if not stmt:
                continue
            self._translate_statement(stmt)
        return self.lines

    # ------------------------------------------------------------------
    def _split_statements(self, src: str) -> list[str]:
        """Split a function body into statements, respecting braces."""
        out: list[str] = []
        depth = 0
        cur: list[str] = []
        i = 0
        while i < len(src):
            ch = src[i]
            if ch in "{(":
                depth += 1
                cur.append(ch)
            elif ch in "})":
                depth -= 1
                cur.append(ch)
            elif ch == ";" and depth == 0:
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
        first_line = code.strip().split("\n", 1)[0][:200]
        msg = f"(* TODO gtk2laz: {reason or 'translate manually'}: {first_line} *)"
        self.lines.append(f"{pad}{msg}")
        out_line = self._pas_line
        self._pas_line += 1
        self.skipped_count += 1
        # Record a TodoItem as a plain dict (rather than a dataclass) so
        # that JSON serialisation works uniformly under PyInstaller.
        # c_line is the 1-based line number in the original C source.
        # If unknown (0), try to derive from the function body offset.
        if c_line == 0:
            c_line = self._guess_c_line(code)
        self.todos.append({
            "source_file": self.source_file,
            "source_line": c_line,
            "source_text": first_line,
            "output_file": "",   # filled in by the caller after writing
            "output_line": out_line,
            "reason": reason or "translate manually",
            "function": self.func.name,
        })

    def _guess_c_line(self, code: str) -> int:
        """Best-effort: locate ``code`` inside the function body and
        return its absolute line number in the source file."""
        if not self.source_file:
            return 0
        snippet = code.strip().split("\n", 1)[0][:80]
        if not snippet:
            return self.func.start_line
        try:
            with open(self.source_file, "r",
                      encoding="utf-8", errors="replace") as f:
                src_lines = f.readlines()
        except OSError:
            return self.func.start_line
        # Search within the function body range first.
        for i in range(self.func.start_line - 1,
                        min(self.func.end_line, len(src_lines))):
            if snippet in src_lines[i]:
                return i + 1
        # Fall back to whole-file scan.
        for i, line in enumerate(src_lines):
            if snippet in line:
                return i + 1
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
        if s.startswith("for") and s[3:4] == "(":
            self._translate_for(s)
            return
        if s.startswith("while") and s[5:6] == "(":
            self._translate_while(s)
            return
        if s.startswith("do"):
            self._translate_do(s)
            return
        if s.startswith("switch"):
            self._translate_switch(s)
            return
        if s.startswith("return"):
            self._translate_return(s)
            return
        if s in ("break;",):
            self._emit(f"{pad}Break;")
            return
        if s in ("continue;",):
            self._emit(f"{pad}Continue;")
            return
        if s.startswith("{") and s.endswith("}"):
            # Compound block: just recurse
            inner = s[1:-1]
            for sub in self._split_statements(inner):
                if sub.strip():
                    self._translate_statement(sub.strip())
            return

        # --- Local variable declarations ---
        m = re.match(
            r"^(?P<type>(?:const\s+|static\s+|unsigned\s+|signed\s+|volatile\s+)*"
            r"[\w]+(?:\s+\w+)*\s*\**)\s+(?P<rest>.+)$",
            s, re.DOTALL,
        )
        if m and not s.startswith("("):
            self._translate_local_decl(m.group("type"), m.group("rest"))
            return

        # --- GTK / GLib / libc function call statements ---
        if self._try_call_statement(s):
            return

        # --- Simple assignment ---
        if self._try_assignment(s):
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
        cond_pas = self._translate_expr(cond)
        self._emit(f"{pad}if {cond_pas} then")
        rest = self._translate_branch(after, expect_else=True)
        # If no else follows, patch the trailing 'end' to 'end;'
        if not rest.startswith("else"):
            self._patch_last_end_semicolon()
        # Handle else / else if chain
        while rest.startswith("else"):
            rest = rest[4:].lstrip()
            if rest.startswith("if") and (rest[2:3] in (" ", "(")):
                # else if -> recurse on the if as a single statement
                if_open = rest.find("(")
                if if_open < 0:
                    self._emit_todo(rest, "else if malformed")
                    return
                cond2, if_after = self._extract_paren(rest, if_open)
                cond_pas2 = self._translate_expr(cond2)
                self._emit(f"{pad}else if {cond_pas2} then")
                rest = self._translate_branch(if_after, expect_else=True)
                if not rest.startswith("else"):
                    self._patch_last_end_semicolon()
            else:
                self._emit(f"{pad}else")
                rest = self._translate_branch(rest, expect_else=False)

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
            # Single statement
            stmt, sep, rest = rest.partition(";")
            self.indent += 1
            self._translate_statement(stmt.strip())
            self.indent -= 1
            rest = rest.lstrip()
        return rest

    def _translate_for(self, s: str) -> None:
        pad = self._pad()
        open_idx = s.find("(")
        cond, after = self._extract_paren(s, open_idx)
        # for (init; test; step)
        parts = _split_top_level(cond, ";")
        if len(parts) != 3:
            self._emit_todo(s, "complex for loop")
            return
        init, test, step = parts
        # Try to translate as for-to-do
        # Patterns: int i=0 ; i<n ; i++    -> for i := 0 to n-1 do
        m = re.match(r"\s*(?:int\s+|unsigned\s+)?(\w+)\s*=\s*([^;]+)", init)
        if m:
            var = m.group(1)
            start = self._translate_expr(m.group(2).strip())
            test_match = re.match(rf"\s*{re.escape(var)}\s*<\s*(.+)", test)
            if test_match:
                end_expr = test_match.group(1).strip()
                end_pas = self._translate_expr(end_expr)
                body, tail = self._extract_block(after.lstrip())
                self._emit(f"{pad}for {var} := {start} to ({end_pas}) - 1 do")
                self._emit(f"{pad}begin")
                self.indent += 1
                for sub in self._split_statements(body):
                    if sub.strip():
                        self._translate_statement(sub.strip())
                # If step is not i++, emit it as last statement
                step = step.strip()
                if step and step != f"{var}++" and step != f"++{var}":
                    # Emit step
                    self._translate_statement(step + ";")
                self.indent -= 1
                self._emit(f"{pad}end;")
                return
        # Generic: emit as while loop with TODO
        self._emit_todo(s, "for loop with non-standard form; rewritten as while")
        self._emit(f"{pad}// init: {init.strip()}")
        self._emit(f"{pad}while {self._translate_expr(test)} do")
        body, tail = self._extract_block(after.lstrip())
        self._emit(f"{pad}begin")
        self.indent += 1
        for sub in self._split_statements(body):
            if sub.strip():
                self._translate_statement(sub.strip())
        if step.strip():
            self._translate_statement(step.strip() + ";")
        self.indent -= 1
        self._emit(f"{pad}end;")

    def _translate_while(self, s: str) -> None:
        pad = self._pad()
        open_idx = s.find("(")
        cond, after = self._extract_paren(s, open_idx)
        cond_pas = self._translate_expr(cond)
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
        pad = self._pad()
        body, tail = self._extract_block(s[2:].lstrip())
        m = re.match(r"\s*while\s*\((.*?)\)\s*;?", tail, re.DOTALL)
        if m:
            cond = self._translate_expr(m.group(1))
            self._emit(f"{pad}repeat")
            self.indent += 1
            for sub in self._split_statements(body):
                if sub.strip():
                    self._translate_statement(sub.strip())
            self.indent -= 1
            self._emit(f"{pad}until not ({cond});")
        else:
            self._emit_todo(s, "do-while without while")

    def _translate_switch(self, s: str) -> None:
        pad = self._pad()
        open_idx = s.find("(")
        cond, after = self._extract_paren(s, open_idx)
        body, tail = self._extract_block(after.lstrip())
        cond_pas = self._translate_expr(cond)
        self._emit(f"{pad}case {cond_pas} of")
        self.indent += 1
        # Split on case labels
        case_parts = re.split(r"\bcase\s+", body)
        for cp in case_parts[1:]:
            label, _, cbody = cp.partition(":")
            label = label.strip()
            # Pascal label range
            label_pas = self._translate_expr(label)
            # body until next 'case' or 'default' or end
            # (split by ';' - rough but ok for typical switches)
            stmts = self._split_statements(cbody)
            # Strip 'break;' at end
            stmts = [st for st in stmts if st.strip() not in ("break",)]
            self._emit(f"{self._pad()}{label_pas}:")
            self._emit(f"{self._pad()}begin")
            self.indent += 1
            for st in stmts:
                if st.strip():
                    self._translate_statement(st.strip() + ";")
            self.indent -= 1
            self._emit(f"{self._pad()}end;")
        if "default:" in body:
            default_body = body.split("default:", 1)[1]
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
        pas_type = c_type_to_pascal(type_str.strip())
        # rest may be: "x = 5, y = 6" or "x[10]" etc.
        declarators = _split_top_level(rest.rstrip(";"))
        for d in declarators:
            d = d.strip()
            if not d:
                continue
            # Handle array
            m_arr = re.match(r"(\w+)\s*\[([^\]]*)\]\s*(?:=\s*(.*))?$", d)
            if m_arr:
                name = m_arr.group(1)
                size = m_arr.group(2).strip()
                init = m_arr.group(3)
                if not size:
                    self._emit_local(f"  {name}: array of {pas_type};")
                else:
                    self._emit_local(f"  {name}: array[0..{size}-1] of {pas_type};")
                if init:
                    self._emit_todo(d + ";", "array initializer")
                continue
            m_init = re.match(r"(\w+)\s*=\s*(.+)$", d, re.DOTALL)
            if m_init:
                name = m_init.group(1)
                val = self._translate_expr(m_init.group(2).strip())
                self._emit_local(f"  {name}: {pas_type};")
                pad = self._pad()
                self._emit(f"{pad}{name} := {val};")
                continue
            # Pointer
            if d.startswith("*"):
                name = d.lstrip("*").strip()
                self._emit_local(f"  {name}: {pas_type};")
                continue
            self._emit_local(f"  {d}: {pas_type};")

    # ------------------------------------------------------------------
    def _try_call_statement(self, s: str) -> bool:
        """If s is a function call statement, translate and emit. Return True if handled."""
        m = re.match(r"^(?P<fn>[A-Za-z_]\w*)\s*\((?P<args>.*)\)\s*;?\s*$",
                     s, re.DOTALL)
        if not m:
            return False
        fn = m.group("fn")
        args_raw = m.group("args").strip()
        args = self._split_args(args_raw)
        pad = self._pad()
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
        """Try to translate simple assignments."""
        pad = self._pad()
        # Strip trailing semicolon
        s_clean = s.rstrip(";").rstrip()
        # Match: lhs = rhs   (but not == or <=)
        m = re.match(r"^(?P<lhs>[^=<>!]+?)\s*=\s*(?P<rhs>.+)$", s_clean, re.DOTALL)
        if not m:
            return False
        lhs = m.group("lhs").strip()
        rhs = m.group("rhs").strip()
        # Augmented assignments
        aug = re.match(r"^(?P<l>\w+(?:\[[^\]]*\])?(?:\.\w+)?)\s*(?P<op>\+=|-=|\*=|/=|%=|<<=|>>=|&=|\|=|\^=)\s*(?P<r>.+)$",
                       s_clean, re.DOTALL)
        if aug:
            l = aug.group("l")
            op = aug.group("op")
            r = self._translate_expr(aug.group("r").strip())
            pas_op = {
                "+=": "+", "-=": "-", "*=": "*", "/=": "/",
                "%=": "mod", "<<=": "shl", ">>=": "shr",
                "&=": "and", "|=": "or", "^=": "xor",
            }[op]
            self._emit(f"{pad}{l} := {l} {pas_op} {r};")
            return True
        # Pattern: gtk_xxx_get_*(GTK_XXX(widget)) -> lhs := widget.Prop
        # We delegate to expr translator which handles gtk_* calls.
        lhs_pas = self._translate_expr(lhs)
        rhs_pas = self._translate_expr(rhs)
        self._emit(f"{pad}{lhs_pas} := {rhs_pas};")
        return True

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
        m = re.match(r"^0[xX]([0-9A-Fa-f]+)$", expr)
        if m:
            return f"${m.group(1)}"
        # Operators
        result = expr
        for old, new in self._OP_REPLACE:
            result = result.replace(old, new)
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
        return result

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
    """Translate one .c file into one .pas unit."""

    def __init__(self, c_path: str, unit_name: str,
                 form_unit: Optional[str] = None):
        self.c_path = c_path
        self.unit_name = unit_name
        self.form_unit = form_unit  # name of the unit that owns form fields
        self.stats = {
            "functions": 0, "globals": 0, "translated_stmts": 0,
            "todo_stmts": 0, "lines_in": 0, "lines_out": 0,
        }
        # Collected TodoItems (as plain dicts) across all functions in
        # this file. Using dicts instead of dataclasses so JSON
        # serialisation works uniformly under PyInstaller.
        self.todos: list[dict] = []
        # Number of lines emitted before the function bodies begin.
        # Updated as the header is built; used to compute absolute output
        # line numbers for each TODO.
        self._header_line_count = 0

    # ------------------------------------------------------------------
    def translate(self) -> str:
        with open(self.c_path, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
        self.stats["lines_in"] = src.count("\n") + 1
        src_clean = strip_comments(src)
        src_clean, directives = strip_preprocessor(src_clean)
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
        if self.form_unit and self.form_unit != self.unit_name:
            out.append("  Classes, SysUtils, Forms, Controls, Graphics, Dialogs,")
            out.append("  StdCtrls, ComCtrls, ExtCtrls, Buttons, Spin, Menus, LCLType,")
            out.append(f"  {self.form_unit};")
        else:
            out.append("  Classes, SysUtils, Forms, Controls, Graphics, Dialogs,")
            out.append("  StdCtrls, ComCtrls, ExtCtrls, Buttons, Spin, Menus, LCLType;")
        out.append("")
        # Constants from #define
        defines = self._extract_defines(directives)
        if defines:
            out.append("const")
            for name, val in defines:
                out.append(f"  {name} = {val};")
            out.append("")
        # Types
        out.append("type")
        out.append("  DWORD = Cardinal;")
        out.append("  WORD  = Word;")
        out.append("  BYTE  = Byte;")
        out.append("")
        # Globals as var block (interface section).
        # Deduplicate by base name (without array brackets) to avoid
        # "duplicate var" Pascal compile errors when the C source declares
        # the same extern in multiple places (possibly with different
        # array sizes - we keep the first declaration).
        if globals_list:
            out.append("var")
            seen_names: set[str] = set()
            for g in globals_list:
                pas_type = c_type_to_pascal(g.type)
                for name_init in g.names:
                    name, init = self._split_name_init(name_init)
                    if not name:
                        continue
                    # Compute base name (strip array suffix) for dedup
                    base_name = re.match(r"(\w+)", name)
                    if not base_name:
                        continue
                    base = base_name.group(1)
                    if base in seen_names:
                        continue
                    seen_names.add(base)
                    if "[" in name:
                        # array global
                        m = re.match(r"(\w+)\s*\[([^\]]*)\]", name)
                        if m:
                            n = m.group(1)
                            sz = m.group(2).strip()
                            if sz:
                                out.append(f"  {n}: array[0..{sz}-1] of {pas_type};")
                            else:
                                out.append(f"  {n}: array of {pas_type};")
                            continue
                    out.append(f"  {name}: {pas_type};")
            out.append("")
        # Function prototypes
        out.append("// --- function declarations ---")
        for fn in funcs:
            sig = self._pascal_signature(fn, prototype=True)
            if sig:
                out.append(sig + ";")
        out.append("")
        out.append("implementation")
        out.append("")
        # Function bodies
        for fn in funcs:
            body_lines = self._translate_function(fn)
            out.extend(body_lines)
            out.append("")
        out.append("end.")
        text = "\n".join(out) + "\n"
        self.stats["lines_out"] = text.count("\n") + 1
        self.stats["functions"] = len(funcs)
        self.stats["globals"] = len(globals_list)
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
        """Build a Pascal procedure/function signature."""
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
            params.append(f"{pname}: {pas_type}")
        params_str = "; ".join(params)
        prefix = "function" if is_func else "procedure"
        sig = f"{prefix} {fn.name}"
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
                   form_unit: Optional[str] = None) -> dict:
    """Convert one .c file to a .pas unit under ``out_dir``."""
    if unit_name is None:
        unit_name = os.path.splitext(os.path.basename(c_path))[0].lower()
        # Pascal unit identifiers cannot start with a digit
        if unit_name[0].isdigit():
            unit_name = "u" + unit_name
    translator = CFileTranslator(c_path, unit_name, form_unit)
    text = translator.translate()
    # Compute header line count: count newlines up to "implementation\n"
    # and add 2 (the "implementation" line itself + blank after).
    impl_idx = text.find("\nimplementation\n")
    if impl_idx >= 0:
        translator._header_line_count = text[:impl_idx].count("\n") + 2
    # Re-translate now that we know the header line count, so the
    # TodoItem output_line fields are correct.
    translator2 = CFileTranslator(c_path, unit_name, form_unit)
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
        out.append("")

        # 1) #define -> const
        defines = self._extract_defines(directives)
        if defines:
            out.append("const")
            for name, val in defines:
                out.append(f"  {name} = {val};")
            out.append("")
            self.stats["defines"] = len(defines)

        # 2) typedefs (struct/enum) -> type
        typedefs = self._extract_typedefs(src_no_pp)
        if typedefs:
            out.append("type")
            for t in typedefs:
                out.append(t)
            out.append("")
            self.stats["typedefs"] = len(typedefs)

        # 3) extern globals -> var
        externs = self._extract_externs(src_no_pp)
        if externs:
            out.append("var")
            seen = set()
            for name, ptype in externs:
                if name in seen:
                    continue
                seen.add(name)
                out.append(f"  {name}: {ptype};")
            out.append("")
            self.stats["globals"] = len(externs)

        # 4) function prototypes
        protos = self._extract_prototypes(src_no_pp)
        if protos:
            out.append("// --- function declarations ---")
            for sig in protos:
                out.append(sig + ";")
            out.append("")
            self.stats["prototypes"] = len(protos)

        # Anything else: emit as TODO comments so the user can see what
        # was not handled. We collect a few leftover lines as a hint.
        leftover = self._leftover_lines(src_no_pp)
        for line in leftover[:30]:
            out.append(f"  (* TODO gtk2laz: header line not translated: "
                       f"{line.strip()[:120]} *)")
            self.todos.append({
                "source_file": self.h_path,
                "source_line": 0,
                "source_text": line.strip()[:200],
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
    def _extract_typedefs(self, src: str) -> list[str]:
        """Extract ``typedef struct {...} T;`` and ``typedef enum {...} E;``.

        Returns a list of Pascal type declarations (each ending with ``;``).
        """
        out: list[str] = []
        # typedef struct { ... } NAME;
        for m in re.finditer(
            r"typedef\s+struct\s*\{(?P<body>[^}]*)\}\s*(?P<name>\w+)\s*;",
            src, re.DOTALL,
        ):
            body = m.group("body")
            name = m.group("name")
            pas_name = self._pascalize_type_name(name)
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
                    out.append(f"    {fname}: {ftype};")
                out.append(f"  end;")
        # typedef enum { A, B, C = 5, D } NAME;
        for m in re.finditer(
            r"typedef\s+enum\s*\{(?P<body>[^}]*)\}\s*(?P<name>\w+)\s*;",
            src, re.DOTALL,
        ):
            body = m.group("body")
            name = m.group("name")
            pas_name = self._pascalize_type_name(name)
            members = self._parse_enum_members(body)
            if members:
                out.append(f"  {pas_name} = ({', '.join(members)});")
            else:
                out.append(f"  (* TODO gtk2laz: empty enum {pas_name} *)")
        # Simple typedef: typedef OLD NEW;
        for m in re.finditer(
            r"typedef\s+(?P<old>[\w\s]+?)\s+(?P<new>\w+)\s*;",
            src,
        ):
            old = m.group("old").strip()
            new = m.group("new").strip()
            if old in ("struct", "enum", "union"):
                continue
            pas_old = c_type_to_pascal(old)
            pas_new = self._pascalize_type_name(new)
            # Skip if already declared by a struct/enum typedef above
            if any(pas_new in line for line in out):
                continue
            out.append(f"  {pas_new} = {pas_old};")
        return out

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
                # Avoid Pascal keywords
                if name.lower() in {"begin", "end", "if", "then", "else",
                                    "for", "to", "do", "while", "repeat",
                                    "until", "case", "of", "with", "function",
                                    "procedure", "type", "var", "const",
                                    "and", "or", "not", "xor", "div", "mod",
                                    "in", "is", "as", "nil", "true", "false",
                                    "shl", "shr", "self", "name", "result"}:
                    name = name + "_"
                out.append(name)
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
    def _extract_prototypes(self, src: str) -> list[str]:
        """Extract function prototypes and emit Pascal signatures."""
        out: list[str] = []
        # A prototype looks like:  TYPE NAME(params);
        # We avoid matching control-flow keywords.
        proto_re = re.compile(
            r"^(?P<ret>(?:const\s+|static\s+|inline\s+|extern\s+|volatile\s+)*"
            r"[\w][\w\s\*]*?)\s+"
            r"(?P<name>[A-Za-z_]\w*)\s*"
            r"\((?P<params>[^;]*)\)\s*;",
            re.MULTILINE,
        )
        for m in proto_re.finditer(src):
            name = m.group("name")
            if name in _C_KEYWORDS:
                continue
            ret = m.group("ret").strip()
            if not ret:
                continue
            if ret.split()[-1] in _C_KEYWORDS:
                continue
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
                params.append(f"{pname}: {pas_type}")
            params_str = "; ".join(params)
            is_func = ret and ret != "void"
            prefix = "function" if is_func else "procedure"
            sig = f"{prefix} {name}"
            if params_str:
                sig += f"({params_str})"
            if is_func:
                ret_pas = c_type_to_pascal(ret)
                if not ret_pas:
                    ret_pas = "TObject"
                sig += f": {ret_pas}"
            out.append(sig)
        return out

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
    }
