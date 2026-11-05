"""
ast_translator.py
=================
AST-based C → Pascal translator using pycparser.

Stage 1 of the performance upgrade plan:
  * Parse the C source with pycparser (real C99 AST, accurate types).
  * Generate accurate Pascal signatures (function prototypes, parameters,
    local variables, types) from the AST.
  * Function bodies are still translated by the existing pattern-based
    ``BodyTranslator`` (Stage 1 only rewrites signatures and types;
    Stage 2 will rewrite bodies too).

Why this is more accurate than the pattern-based approach:

  * pycparser is a real recursive-descent C99 parser. It correctly
    distinguishes function definitions from ``if/for/while`` statements
    (the pattern-based regex occasionally confuses them).
  * Parameter types come straight from the AST, so ``char *argv[]``
    becomes ``argv: array of PChar`` instead of a guess.
  * Struct / enum / typedef are emitted with their full field lists,
    including nested arrays and pointers.
  * Function-pointer typedefs are detected and emitted as Pascal
    procedure types.

Fallback: if pycparser fails to parse a file (e.g. it uses GNU
extensions pycparser does not support), the caller can fall back to
the pattern-based ``convert_c_file``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

# Import pascalify_name from widget_map so all generated identifiers
# (variables, functions, fields, parameters) avoid Pascal reserved words.
from widget_map import pascalify_name, is_pascal_reserved

# ---------------------------------------------------------------------------
# ply compatibility shim
# ---------------------------------------------------------------------------
# pycparser depends on the `ply` package. We bundle ply's source code
# directly in a `ply/` subdirectory so it's always available, even under
# PyInstaller or when the system ply package is missing.
#
# This shim must run BEFORE pycparser is imported.
import sys
import os

def _ensure_ply():
    """Ensure the `ply` package is importable.

    Priority:
      1. Local ply/ directory bundled with gtk2laz (always available)
      2. System-installed ply package
      3. pcpp's embedded ply (pcpp.ply.ply)
    """
    # Already loaded?
    if 'ply' in sys.modules and 'ply.lex' in sys.modules:
        return

    # Try local ply/ directory first
    here = os.path.dirname(os.path.abspath(__file__))
    meipass = getattr(sys, '_MEIPASS', None)
    for base in [here, meipass]:
        if base and os.path.isdir(os.path.join(base, 'ply')):
            if base not in sys.path:
                sys.path.insert(0, base)
            break

    # Try normal import
    try:
        import ply
        import ply.lex
        import ply.yacc
        return
    except ImportError:
        pass

    # Try pcpp's embedded ply as fallback
    try:
        import pcpp.ply.ply as _pcpp_ply
        sys.modules['ply'] = _pcpp_ply
        try:
            import pcpp.ply.ply.lex as _lex
            sys.modules['ply.lex'] = _lex
        except ImportError:
            pass
        try:
            import pcpp.ply.ply.yacc as _yacc
            sys.modules['ply.yacc'] = _yacc
        except ImportError:
            pass
    except ImportError:
        pass

_ensure_ply()

# pycparser is an optional dependency. The module imports cleanly even
# if pycparser is missing; the public ``parse_with_ast`` function raises
# a clear error in that case.
try:
    from pycparser import c_ast, c_parser, parse_file
    _HAS_PYCPARSER = True
except ImportError:
    _HAS_PYCPARSER = False


import re as _re


def _extract_missing_type_from_error(err_msg: str) -> str:
    """Extract the undefined type name from a pycparser ParseError
    message so we can emit a placeholder typedef and retry.

    pycparser's ParseError messages come in these flavours:

      1. ``"<file>:line:col: before: <ident>"`` — pycparser hit an
         identifier it doesn't know and is telling us what it was
         looking at when it bailed.  The identifier is the LAST
         whitespace-separated token after ``before:``.

      2. ``"<file>:line:col: Missing type in declaration"`` — the
         parser hit a declaration whose type specifier was a macro
         that got stripped (because we're on the raw-source fallback
         path).  pycparser doesn't name the identifier in this case,
         so we have to fall back to "guess and retry" — return a
         sentinel so the caller knows to emit an ``int`` typedef.

      3. ``"<file>:line: Non-typedef 'X' previously declared..."``
         — the error names a specific identifier.  We return it.

    Returns the extracted identifier (stripped of punctuation) or "".
    """
    if not err_msg:
        return ""
    # Pattern 1: "before: <ident>"
    m = _re.search(r"before:\s+(\w+)", err_msg)
    if m:
        return m.group(1)
    # Pattern 2: "Non-typedef 'X' ..."
    m = _re.search(r"Non-typedef '(\w+)'", err_msg)
    if m:
        return m.group(1)
    # Pattern 3: "Missing type in declaration" — we don't know the
    # name, but the caller will look at the error line in the source
    # to find the offending variable.  We return a sentinel so the
    # caller's retry loop continues.
    if "Missing type" in err_msg:
        # Try to extract the variable name from the error line number.
        # Format: "<file>:line:col: Missing type in declaration"
        m = _re.search(r":(\d+):(\d+):\s*Missing type", err_msg)
        if m:
            return f"__MISSING_TYPE_LINE_{m.group(1)}__"
    return ""


def _read_and_inline_local_headers(c_path: str) -> str:
    """Read a .c file and inline every local ``#include "..."`` header
    so pycparser can parse the result without a real preprocessor.

    This is the last-resort preprocessing path used when both pcpp
    and gcc are unavailable (the common case on Windows).  We:

      1. Read the .c source.
      2. Walk every line; when we see ``#include "name.h"`` (note the
         double quotes — that signals a LOCAL include, not a system
         include), we look for ``name.h`` in the same directory as
         the .c file and recursively inline its contents.  System
         includes (``#include <name.h>``) are dropped — the fake
         headers prepended later provide the typedefs.
      3. Strip every other preprocessor directive (``#define``,
         ``#ifdef``, ``#if``, ``#endif``, ``#pragma``, ...).
      4. Strip C block comments (``/* ... */``) and line comments
         (``// ...``) so pycparser doesn't abort with "Comments are
         not supported".

    Macro expansion (#define X Y) is NOT performed — we simply drop
    the directives.  This means code that relies on macro expansion
    will not parse correctly, but for the opgui codebase (which
    mostly uses #define for constants and includes) this is good
    enough to extract all function signatures and bodies.

    The result is a single string ready to be passed to pycparser
    (after the fake-header blob is prepended by the caller).
    """
    src_dir = os.path.dirname(os.path.abspath(c_path))
    seen_includes: set[str] = set()

    def _read_with_includes(path: str, depth: int = 0) -> str:
        if depth > 10:
            return ""  # avoid infinite recursion on circular includes
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            return ""
        out_lines: list[str] = []
        for line in content.splitlines():
            stripped = line.lstrip()
            # Local include: #include "name.h"
            m = _re.match(r'#\s*include\s+"([^"]+)"', stripped)
            if m:
                inc_name = m.group(1)
                inc_path = os.path.join(src_dir, inc_name)
                # Avoid inlining the same header twice
                inc_abs = os.path.abspath(inc_path)
                if inc_abs in seen_includes:
                    out_lines.append("")  # preserve line numbering
                    continue
                seen_includes.add(inc_abs)
                if os.path.isfile(inc_path):
                    inlined = _read_with_includes(inc_path, depth + 1)
                    out_lines.append(inlined)
                else:
                    out_lines.append("")  # header not found — skip
                continue
            # System include: #include <name.h> — drop it; fake headers
            # provide the typedefs.
            if _re.match(r'#\s*include\s+<[^>]+>', stripped):
                out_lines.append("")
                continue
            # Any other preprocessor directive — drop it.
            if stripped.startswith("#"):
                out_lines.append("")
                continue
            out_lines.append(line)
        raw = "\n".join(out_lines)
        # Strip block comments (/* ... */) across multiple lines.
        raw = _re.sub(r"/\*.*?\*/", "", raw, flags=_re.DOTALL)
        # Strip line comments (// ...).
        raw = _re.sub(r"//[^\n]*", "", raw)
        return raw

    return _read_with_includes(c_path)


# ---------------------------------------------------------------------------
# Type translation: pycparser AST type nodes → Pascal type strings
# ---------------------------------------------------------------------------
def _type_to_pascal(typ) -> str:
    """Translate a pycparser type node to a Pascal type string."""
    if typ is None:
        return "TObject"
    # IdentifierType (e.g. "int", "char", "GtkWidget")
    if isinstance(typ, c_ast.IdentifierType):
        if not typ.names:
            return "TObject"
        name = " ".join(typ.names)
        return _C_TYPE_TO_PASCAL.get(name, _pascalize_type(name))
    # TypeDecl: the actual declaration with a name; we want its type
    if isinstance(typ, c_ast.TypeDecl):
        return _type_to_pascal(typ.type)
    # PtrDecl: pointer to something
    if isinstance(typ, c_ast.PtrDecl):
        inner = _type_to_pascal(typ.type)
        # void* → Pointer (not ^Pointer). In Pascal, ``Pointer`` is
        # an untyped pointer, which is the direct equivalent of C's
        # ``void *``. Using ``^Pointer`` would be syntactically valid
        # but semantically wrong — it implies a pointer TO a pointer.
        if inner == "Pointer":
            return "Pointer"
        # char* -> PChar
        if inner in ("Char", "PChar"):
            return "PChar"
        if inner == "Byte":
            return "PByte"
        # Use P-prefix convention for pointer types (same as
        # widget_map.c_type_to_pascal and HeaderTranslator):
        #   ^TSomeRecord  → PSomeRecord
        #   ^TENetSymbol  → PENetSymbol
        #   ^TENetHost    → PENetHost
        # This matches the pointer-type aliases we auto-generate
        # for every struct/enum/union typedef.
        if inner.startswith("T") and len(inner) > 1 and inner[1].isupper():
            return _make_pointer_name(inner)
        # For non-T types (Integer, Pointer, etc.), keep ^Inner
        # since there is no P-prefix alias defined for them.
        return f"^{inner}"
    # ArrayDecl: array of something
    if isinstance(typ, c_ast.ArrayDecl):
        inner = _type_to_pascal(typ.type)
        dim = _const_eval(typ.dim) if typ.dim else ""
        if dim:
            return f"array[0..{dim}-1] of {inner}"
        return f"array of {inner}"
    # Typename (used in casts and sizeof)
    if isinstance(typ, c_ast.Typename):
        return _type_to_pascal(typ.type)
    # Struct
    if isinstance(typ, c_ast.Struct):
        if typ.name:
            return _pascalize_type(typ.name)
        return "TRecord"
    # Union
    if isinstance(typ, c_ast.Union):
        if typ.name:
            return _pascalize_type("union_" + typ.name)
        return "TUnionRecord"
    # Enum
    if isinstance(typ, c_ast.Enum):
        if typ.name:
            return _pascalize_type(typ.name)
        return "TEnum"
    # FuncDecl (function pointer type)
    if isinstance(typ, c_ast.FuncDecl):
        return "TProcedure"
    return "TObject"


_C_TYPE_TO_PASCAL = {
    "void": "Pointer",
    "char": "Char",
    "signed char": "ShortInt",
    "unsigned char": "Byte",
    "short": "SmallInt",
    "short int": "SmallInt",
    "unsigned short": "Word",
    "unsigned short int": "Word",
    "int": "Integer",
    "signed": "Integer",
    "signed int": "Integer",
    "unsigned": "Cardinal",
    "unsigned int": "Cardinal",
    "long": "LongInt",
    "long int": "LongInt",
    "unsigned long": "Cardinal",
    "unsigned long int": "Cardinal",
    "long long": "Int64",
    "long long int": "Int64",
    "unsigned long long": "QWord",
    "unsigned long long int": "QWord",
    "float": "Single",
    "double": "Double",
    "long double": "Extended",
    "size_t": "PtrUInt",
    "ssize_t": "PtrInt",
    "FILE": "TextFile",
    # GTK/GLib types (handled by typedefs but also mapped for safety)
    "gboolean": "Boolean",
    "gint": "Integer",
    "guint": "Cardinal",
    "glong": "LongInt",
    "gulong": "Cardinal",
    "gdouble": "Double",
    "gchar": "Char",
    "guchar": "Byte",
    "gpointer": "Pointer",
    "gconstpointer": "Pointer",
    "gsize": "PtrUInt",
    "gssize": "PtrInt",
    "guint8": "Byte",
    "guint16": "Word",
    "guint32": "Cardinal",
    "guint64": "QWord",
    "gint8": "ShortInt",
    "gint16": "SmallInt",
    "gint32": "LongInt",
    "gint64": "Int64",
    "GtkWidget": "TControl",
    "GtkWindow": "TForm",
    "GtkButton": "TButton",
    "GtkLabel": "TLabel",
    "GtkEntry": "TEdit",
    "GtkSpinButton": "TSpinEdit",
    "GtkComboBox": "TComboBox",
    "GtkComboBoxText": "TComboBox",
    "GtkTextView": "TMemo",
    "GtkTextBuffer": "TMemo",
    "GtkNotebook": "TPageControl",
    "GtkStatusbar": "TStatusBar",
    "GtkToggleButton": "TSpeedButton",
    "GtkCheckButton": "TCheckBox",
    "GtkRadioButton": "TRadioButton",
    "GtkTreeView": "TListView",
    "GtkTreeSelection": "TObject",
    "GtkAdjustment": "TObject",
    "GtkToolButton": "TToolButton",
    "GtkToolbar": "TToolBar",
    "GtkBuilder": "TObject",
    "GtkCssProvider": "TObject",
    "GObject": "TObject",
    "GCallback": "TMethod",
    "GdkPixbuf": "TPicture",
    "GdkScreen": "TObject",
}


def _pascalize_type(name: str) -> str:
    """C type name -> Pascal type name. Avoid 'T' double-prefix."""
    # Strip "struct " / "enum " / "union " prefixes
    for kw in ("struct ", "enum ", "union "):
        if name.startswith(kw):
            name = name[len(kw):]
            break
    if not name:
        return "TObject"
    if name in _PASCAL_BUILTINS:
        return name
    if name.startswith("T") and len(name) > 1 and name[1].isupper():
        return name
    return "T" + name


_PASCAL_BUILTINS = {
    "Integer", "Cardinal", "LongInt", "LongWord", "Int64", "QWord",
    "SmallInt", "Word", "ShortInt", "Byte", "Char", "Boolean",
    "Single", "Double", "Extended", "Real", "Pointer", "string",
    "PChar", "PByte", "TextFile", "TObject", "THandle",
}


def _const_eval(expr) -> str:
    """Evaluate a constant expression node to its textual value.

    For simple cases (integers, identifiers, simple binary ops) we
    return a string suitable for an array dimension.
    """
    if expr is None:
        return ""
    if isinstance(expr, c_ast.Constant):
        return expr.value
    if isinstance(expr, c_ast.ID):
        return expr.name
    if isinstance(expr, c_ast.BinaryOp):
        return f"({_const_eval(expr.left)} {expr.op} {_const_eval(expr.right)})"
    if isinstance(expr, c_ast.UnaryOp):
        return f"{expr.op}{_const_eval(expr.expr)}"
    # Fall back to a textual dump
    return _dump(expr)


def _dump(node) -> str:
    """One-line textual dump of a node, used for fallback."""
    if node is None:
        return ""
    try:
        gen = c_generator.CGenerator()
        return gen.visit(node).strip()
    except Exception:
        return type(node).__name__


# Lazy import to avoid hard dependency at module load time.
try:
    from pycparser import c_generator
except ImportError:
    c_generator = None


# ---------------------------------------------------------------------------
# Function extraction
# ---------------------------------------------------------------------------
@dataclass
class AstFunction:
    name: str
    return_type: str
    params: list[tuple[str, str]]   # list of (name, pascal_type)
    is_static: bool
    body_node: Optional[object]     # c_ast.Compound or None for prototypes


@dataclass
class AstGlobal:
    name: str
    pascal_type: str
    init: str = ""


@dataclass
class AstTypeDef:
    name: str
    kind: str     # 'typedef', 'struct', 'enum'
    pascal_decl: str


def _decl_to_func_signature(decl) -> Optional[AstFunction]:
    """If ``decl`` is a function declaration/definition, return an
    AstFunction. Otherwise return None."""
    # Function definitions are wrapped in FuncDef nodes; prototypes are
    # Decl nodes whose .type is FuncDecl.
    type_node = decl
    if hasattr(decl, "type") and isinstance(decl.type, c_ast.FuncDecl):
        type_node = decl.type
    elif isinstance(decl, c_ast.FuncDef) and hasattr(decl.decl, "type") \
            and isinstance(decl.decl.type, c_ast.FuncDecl):
        type_node = decl.decl.type
    else:
        # Check if it's a Decl wrapping a function pointer
        return None
    if not isinstance(type_node, c_ast.FuncDecl):
        return None
    # Return type
    ret_type = _type_to_pascal(type_node.args) if False else "Pointer"
    if type_node.type is not None:
        ret_type = _type_to_pascal(type_node.type)
    # Parameters
    params: list[tuple[str, str]] = []
    if type_node.args:
        for p in type_node.args.params:
            if isinstance(p, c_ast.Typename):
                # Typename in func decl - this is unusual; skip
                continue
            if isinstance(p, c_ast.ID):
                # K&R style declaration, just a name
                params.append((p.name, "TObject"))
                continue
            if isinstance(p, c_ast.Decl):
                pname = p.name or f"arg{len(params)+1}"
                ptype = _type_to_pascal(p.type)
                params.append((pname, ptype))
                continue
    # Name
    if isinstance(decl, c_ast.FuncDef):
        name = decl.decl.name
        is_static = "static" in (decl.decl.storage or [])
        body = decl.body
    else:
        name = decl.name or ""
        is_static = "static" in (decl.storage or [])
        body = None
    return AstFunction(
        name=name,
        return_type=ret_type,
        params=params,
        is_static=is_static,
        body_node=body,
    )


def _decl_to_global(decl) -> Optional[AstGlobal]:
    """If ``decl`` is a non-function global variable, return AstGlobal."""
    # Skip function decls (handled by _decl_to_func_signature)
    if hasattr(decl, "type") and isinstance(decl.type, c_ast.FuncDecl):
        return None
    if isinstance(decl, c_ast.FuncDef):
        return None
    if not isinstance(decl, c_ast.Decl):
        return None
    name = decl.name
    if not name:
        return None
    # Apply pascalify_name so globals like 'string', 'file', 'name' get
    # the trailing underscore and don't clash with Pascal reserved words.
    name = pascalify_name(name)
    # Skip local declarations inside function bodies
    # (top-level Decls in ast.ext are globals; this is checked by caller)
    ptype = _type_to_pascal(decl.type)
    init = ""
    if decl.init is not None:
        init = _dump(decl.init)
    return AstGlobal(name=name, pascal_type=ptype, init=init)


def _typedef_to_pas(typedef) -> Optional[AstTypeDef]:
    """Convert a c_ast.Typedef to a Pascal type declaration."""
    name = typedef.name
    if not name:
        return None
    # If the inner type is a struct/enum with a body, emit a record/enum
    inner = typedef.type
    # Drill down through TypeDecl to find Struct/Enum/Union
    if isinstance(inner, c_ast.TypeDecl):
        if isinstance(inner.type, c_ast.Struct):
            return _struct_typedef_to_pas(name, inner.type)
        if isinstance(inner.type, c_ast.Enum):
            return _enum_typedef_to_pas(name, inner.type)
        if isinstance(inner.type, c_ast.Union):
            return _union_typedef_to_pas(name, inner.type)
    # Simple typedef: typedef OLD NEW;
    old_type = _type_to_pascal(inner)
    return AstTypeDef(name=name, kind="typedef",
                      pascal_decl=f"  {_pascalize_type(name)} = {old_type};")


def _struct_typedef_to_pas(name: str, struct_node) -> AstTypeDef:
    pas_name = _pascalize_type(name)
    fields: list[str] = []
    if struct_node.decls:
        for f in struct_node.decls:
            if not isinstance(f, c_ast.Decl):
                continue
            fname = pascalify_name(f.name or "field")
            ftype = _type_to_pascal(f.type)
            fields.append(f"    {fname}: {ftype};")
    # Build the record declaration AND an auto-generated pointer-type
    # alias (``P<Name> = ^T<Name>``). The pointer alias lets the
    # converter use ``PName`` instead of ``^TName`` for pointer-to-
    # struct types, which is the idiomatic Pascal convention. This
    # also matches what the HeaderTranslator does for .h files.
    lines: list[str] = []
    if fields:
        lines.append(f"  {pas_name} = record")
        lines.extend(fields)
        lines.append("  end;")
    else:
        lines.append(f"  {pas_name} = record")
        lines.append("    (* empty struct *)")
        lines.append("  end;")
    # Auto-generate pointer alias: TSomeRecord → PSomeRecord,
    # TENetSymbol → PENetSymbol.
    ptr_name = _make_pointer_name(pas_name)
    lines.append(f"  {ptr_name} = ^{pas_name};")
    body = "\n".join(lines)
    return AstTypeDef(name=name, kind="struct", pascal_decl=body)


def _make_pointer_name(pas_name: str) -> str:
    """Convert a Pascal type name to its pointer-type alias name.

    Convention:
      TSomeRecord → PSomeRecord  (strip T, prepend P)
      TENetSymbol → PENetSymbol  (strip T, prepend P)
      SomeType    → PSomeType    (no T to strip, just prepend P)

    This matches the convention used by FreePascal/LCL's own
    pointer types (PInteger, PByte, PChar, etc.).
    """
    if pas_name.startswith("T") and len(pas_name) > 1 \
            and pas_name[1].isupper():
        return "P" + pas_name[1:]
    return "P" + pas_name


def _enum_typedef_to_pas(name: str, enum_node) -> AstTypeDef:
    pas_name = _pascalize_type(name)
    members: list[str] = []
    if enum_node.values:
        for v in enum_node.values.enumerators:
            mname = pascalify_name(v.name)
            members.append(mname)
    lines: list[str] = []
    if members:
        lines.append(f"  {pas_name} = ({', '.join(members)});")
    else:
        lines.append(f"  (* empty enum {pas_name} *)")
    # Auto-generate pointer alias for enums too (same convention).
    ptr_name = _make_pointer_name(pas_name)
    lines.append(f"  {ptr_name} = ^{pas_name};")
    body = "\n".join(lines)
    return AstTypeDef(name=name, kind="enum", pascal_decl=body)


def _union_typedef_to_pas(name: str, union_node) -> AstTypeDef:
    pas_name = _pascalize_type(name)
    # C unions map to Pascal variant records, but variant records
    # require a case tag which the C union does not have. We convert
    # the union to a Pascal record with all fields listed (same as
    # the HeaderTranslator does for .h files) and emit a comment
    # so the user knows to convert it to a proper variant record by
    # hand. This matches the HeaderTranslator's behaviour so the
    # .h and .c outputs are consistent.
    lines: list[str] = []
    lines.append(f"  // C union — consider converting to Pascal "
                 f"variant record (case of).")
    fields: list[str] = []
    if union_node.decls:
        for f in union_node.decls:
            if not isinstance(f, c_ast.Decl):
                continue
            fname = pascalify_name(f.name or "field")
            ftype = _type_to_pascal(f.type)
            fields.append(f"    {fname}: {ftype};")
    if fields:
        lines.append(f"  {pas_name} = record")
        lines.extend(fields)
        lines.append("  end;")
    else:
        lines.append(f"  {pas_name} = record")
        lines.append("    (* empty union *)")
        lines.append("  end;")
    # Auto-generate pointer alias (same as struct/enum).
    ptr_name = _make_pointer_name(pas_name)
    lines.append(f"  {ptr_name} = ^{pas_name};")
    body = "\n".join(lines)
    return AstTypeDef(name=name, kind="union", pascal_decl=body)


_PASCAL_KEYWORDS = {
    "begin", "end", "if", "then", "else", "for", "to", "downto", "do",
    "while", "repeat", "until", "case", "of", "with", "function",
    "procedure", "type", "var", "const", "label", "array", "record",
    "set", "file", "class", "object", "interface", "implementation",
    "uses", "unit", "program", "library", "package", "and", "or",
    "not", "xor", "shl", "shr", "div", "mod", "in", "is", "as", "nil",
    "true", "false", "self", "name", "result",
}


# ---------------------------------------------------------------------------
# Top-level: AST → Pascal unit (signatures only, body uses pattern translator)
# ---------------------------------------------------------------------------
class AstToPascal:
    """Build the interface section of a Pascal unit from a pycparser AST.

    The body of each function is filled in by the existing pattern-based
    BodyTranslator (Stage 1). Stage 2 will replace the body translator
    with an AST-based one.
    """

    def __init__(self, c_path: str, unit_name: str,
                 fake_include_dir: str = "/tmp/fake_libc_include",
                 form_unit: Optional[str] = None,
                 included_units: list[str] = None,
                 target_platform: str = "auto"):
        self.c_path = c_path
        self.unit_name = unit_name
        self.fake_include_dir = fake_include_dir
        self.form_unit = form_unit
        self.included_units = list(included_units or [])
        self.target_platform = target_platform or "auto"
        self.parse_error: Optional[str] = None
        self.functions: list[AstFunction] = []
        self.globals: list[AstGlobal] = []
        self.typedefs: list[AstTypeDef] = []
        # Track line numbers for diagnostics
        self.func_lines: dict[str, tuple[int, int]] = {}

    # ------------------------------------------------------------------
    def parse(self) -> bool:
        """Parse the C file with pycparser. Return True on success.

        Preprocessing strategy (in order of preference):
          1. pcpp (pure-Python C preprocessor) - works everywhere, no
             external dependencies. Best for Windows where gcc is not
             installed by default.
          2. gcc -E - traditional C preprocessor. Slightly faster and
             more accurate, but requires gcc/cpp to be installed.
          3. Fall back to pattern-based translation if both fail.
        """
        if not _HAS_PYCPARSER:
            self.parse_error = "pycparser not installed"
            print(f"gtk2laz: pycparser NOT installed — cannot parse "
                  f"{os.path.basename(self.c_path)}", file=sys.stderr)
            return False
        text = None
        _src_basename = os.path.basename(self.c_path)
        # ---- Try pcpp (pure Python) first ----
        # pcpp is REQUIRED for AST parsing on Windows (where gcc is not
        # installed by default).  If pcpp is not installed, we try to
        # install it automatically via pip so the user does not need to
        # manually run ``pip install pcpp`` before the first conversion.
        # This auto-install only happens once per session.
        try:
            from pcpp_preprocessor import preprocess, is_available
            if not is_available():
                # pcpp is not installed. On a PyInstaller-frozen build
                # we CANNOT ``pip install`` at runtime (the bundle is
                # read-only), so the auto-install attempt below is
                # effectively a no-op. We still try it for the
                # source-distribution case (running from a clone).
                print(f"gtk2laz: pcpp not installed — attempting "
                      f"auto-install (will fail in frozen build)",
                      file=sys.stderr)
                import subprocess as _subprocess
                import sys as _sys
                try:
                    _subprocess.run(
                        [_sys.executable, "-m", "pip", "install",
                         "pcpp", "--quiet"],
                        check=False, timeout=120,
                        capture_output=True,
                    )
                    # Force re-import after install
                    import importlib
                    import pcpp_preprocessor as _pp
                    importlib.reload(_pp)
                except Exception:
                    pass
            if is_available():
                print(f"gtk2laz: [{_src_basename}] trying pcpp "
                      f"preprocessing...", file=sys.stderr)
                text = preprocess(self.c_path,
                                  fake_include_dir=self.fake_include_dir,
                                  target_platform=self.target_platform)
                if text and text.strip():
                    print(f"gtk2laz: [{_src_basename}] pcpp OK — "
                          f"{len(text)} bytes of preprocessed text",
                          file=sys.stderr)
                else:
                    # pcpp returned empty output. This usually means
                    # pcpp silently failed inside ``preprocess()`` —
                    # the inner function already printed a diagnostic
                    # to stderr, so here we just note it.
                    print(f"gtk2laz: [{_src_basename}] pcpp produced "
                          f"no output — falling back to gcc/raw-source "
                          f"path", file=sys.stderr)
            else:
                print(f"gtk2laz: [{_src_basename}] pcpp still not "
                      f"available after auto-install attempt",
                      file=sys.stderr)
        except Exception as _e:
            print(f"gtk2laz: [{_src_basename}] pcpp preprocess() "
                  f"raised {type(_e).__name__}: {_e}", file=sys.stderr)
            text = None
        # ---- Fall back to gcc -E if pcpp unavailable or failed ----
        if not text or not text.strip():
            print(f"gtk2laz: [{_src_basename}] trying gcc -E "
                  f"fallback...", file=sys.stderr)
            import subprocess
            cpp_args = ["gcc", "-E", "-nostdinc",
                        f"-I{self.fake_include_dir}",
                        # Neutralise GNU extensions that pycparser cannot parse.
                        "-D__attribute__(x)=",
                        "-D__extension__=",
                        "-D__asm__(x)=",
                        "-D__volatile__=",
                        "-D__inline=",
                        "-D__inline__=",
                        "-D__restrict=",
                        "-D__restrict__=",
                        "-D__builtin_va_list=void*",
                        "-D__builtin_va_start(x,y)=",
                        "-D__builtin_va_end(x)=",
                        "-D__signed__=",
                        "-D__typeof__(x)=int",
                        self.c_path]
            try:
                proc = subprocess.run(cpp_args, capture_output=True,
                                       check=False, timeout=60)
                # Decode with errors='replace' so non-UTF-8 bytes don't crash
                text = proc.stdout.decode("utf-8", errors="replace")
                if proc.returncode == 0 and text.strip():
                    print(f"gtk2laz: [{_src_basename}] gcc -E OK — "
                          f"{len(text)} bytes", file=sys.stderr)
                else:
                    # Preprocessor failed - check if we still got partial output
                    if not text.strip():
                        self.parse_error = proc.stderr.decode("utf-8",
                                                               errors="replace")
                        print(f"gtk2laz: [{_src_basename}] gcc -E "
                              f"failed (rc={proc.returncode}): "
                              f"{self.parse_error[:200]}", file=sys.stderr)
                        # Don't return False yet — fall through to the
                        # "raw source" fallback below so we still get a
                        # best-effort AST parse with fake headers injected.
                        text = None
                    else:
                        print(f"gtk2laz: [{_src_basename}] gcc -E "
                              f"partial output (rc={proc.returncode}) — "
                              f"{len(text)} bytes", file=sys.stderr)
            except FileNotFoundError:
                print(f"gtk2laz: [{_src_basename}] gcc not found on "
                      f"PATH — trying raw-source fallback",
                      file=sys.stderr)
                self.parse_error = "gcc not found"
                text = None
            except Exception as e:
                print(f"gtk2laz: [{_src_basename}] gcc -E raised "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
                self.parse_error = f"both pcpp and gcc failed: {e}"
                # Don't return False yet — fall through to the
                # "raw source" fallback below so we still get a
                # best-effort AST parse with fake headers injected.
                text = None
        # ---- Last-resort fallback: use the raw C source text if both
        # pcpp and gcc failed (or gcc is not installed, which is the
        # common case on Windows).  We strip preprocessor directives
        # (#include, #define, #ifdef, ...) ourselves, INLINES every
        # local #include "..." header (so typedefs/structs declared in
        # sibling .h files are visible to pycparser), and rely on the
        # fake-header injection below to provide every system typedef
        # the source might reference.  This is less accurate than a
        # real preprocessor pass (no macro expansion) but it still
        # lets pycparser extract function signatures and bodies from
        # the .c file, which is enough for the converter to produce a
        # useful .pas unit.  Without this fallback the whole .c file's
        # AST parse returns None and the converter falls back to the
        # pattern-based extractor, which only finds ~half the
        # functions in opgui.c.
        if not text or not text.strip():
            print(f"gtk2laz: [{_src_basename}] using raw-source "
                  f"fallback (no macro expansion)", file=sys.stderr)
            try:
                text = _read_and_inline_local_headers(self.c_path)
                if text:
                    print(f"gtk2laz: [{_src_basename}] raw-source OK "
                          f"— {len(text)} bytes "
                          f"(local #includes inlined)", file=sys.stderr)
                else:
                    print(f"gtk2laz: [{_src_basename}] raw-source "
                          f"returned empty", file=sys.stderr)
                if not self.parse_error:
                    self.parse_error = (
                        "used raw source (pcpp and gcc both unavailable)"
                    )
            except Exception as e:
                print(f"gtk2laz: [{_src_basename}] raw-source read "
                      f"failed: {type(e).__name__}: {e}", file=sys.stderr)
                self.parse_error = f"failed to read source: {e}"
                return False
        # ---- Always prepend ALL fake system headers so pycparser sees
        # every typedef/struct the user's code might reference, even
        # if the source did not #include the corresponding system header.
        # This mirrors the logic in pcpp_preprocessor.preprocess() so
        # the gcc -E path is also covered.
        try:
            from pcpp_preprocessor import (
                _read_all_fake_headers, _inject_missing_gtk_typedefs,
            )
        except Exception:
            _read_all_fake_headers = None
            _inject_missing_gtk_typedefs = None
        if text and _read_all_fake_headers:
            fake_blob = _read_all_fake_headers(self.fake_include_dir)
            if fake_blob:
                text = fake_blob + "\n" + text
                print(f"gtk2laz: [{_src_basename}] injected "
                      f"{len(fake_blob)} bytes of fake system headers",
                      file=sys.stderr)
        # Always run the placeholder-typedef injection so any Gtk*/Gdk*
        # names not covered by the fake headers still resolve to stubs.
        if text and _inject_missing_gtk_typedefs:
            text = _inject_missing_gtk_typedefs(text)
        # ---- Parse the preprocessed text with pycparser ----
        print(f"gtk2laz: [{_src_basename}] parsing with pycparser "
              f"({len(text) if text else 0} bytes)...", file=sys.stderr)
        try:
            from pycparser import CParser
            parser = CParser()
            ast = parser.parse(text, filename=self.c_path)
            print(f"gtk2laz: [{_src_basename}] pycparser OK",
                  file=sys.stderr)
        except Exception as e:
            # pycparser raises ParseError when it hits an undefined type
            # name (e.g. ``fd_set``, ``struct timespec``, ...) or a
            # missing type in a declaration (e.g. when a #define that
            # expands to a type was stripped because we're on the raw
            # source fallback path).  We try to recover by parsing
            # the error message for the offending identifier, emitting
            # a placeholder typedef for it, and re-parsing.  We retry
            # up to 10 times so a chain of missing types
            # (``fd_set`` -> ``timeval`` -> ``port`` -> ...) can be
            # resolved in one go.
            print(f"gtk2laz: [{_src_basename}] pycparser initial "
                  f"parse failed: {str(e)[:300]}", file=sys.stderr)
            ast = None
            err_msg = str(e)
            self.parse_error = err_msg
            for _retry in range(10):
                missing = _extract_missing_type_from_error(err_msg)
                if not missing:
                    print(f"gtk2laz: [{_src_basename}] retry "
                          f"{_retry}: no missing type extracted, "
                          f"giving up", file=sys.stderr)
                    break
                # "Missing type in declaration" — the missing name is a
                # sentinel like ``__MISSING_TYPE_LINE_<N>__``.  We look
                # up line <N> in the source text and extract the first
                # identifier that looks like a variable name, then emit
                # an ``int`` typedef for it.
                if missing.startswith("__MISSING_TYPE_LINE_"):
                    line_num_str = missing[len("__MISSING_TYPE_LINE_"):-2]
                    try:
                        line_num = int(line_num_str)
                        src_lines = text.splitlines()
                        if 0 < line_num <= len(src_lines):
                            err_line = src_lines[line_num - 1]
                            # Extract the first identifier that could be
                            # a variable name (alphanumeric, starts with
                            # a letter or underscore).
                            m_id = _re.search(
                                r"\b([A-Za-z_]\w*)\s*[=;\[,\)]", err_line
                            )
                            if m_id:
                                var_name = m_id.group(1)
                                text = f"typedef int {var_name};\n" + text
                                print(f"gtk2laz: [{_src_basename}] retry "
                                      f"{_retry}: injecting "
                                      f"typedef int {var_name}; "
                                      f"(from line {line_num})",
                                      file=sys.stderr)
                                # Re-parse and continue the retry loop.
                                try:
                                    parser2 = CParser()
                                    ast = parser2.parse(text,
                                                        filename=self.c_path)
                                    self.parse_error = ""
                                    print(f"gtk2laz: [{_src_basename}] "
                                          f"retry {_retry}: pycparser OK",
                                          file=sys.stderr)
                                    break
                                except Exception as e2:
                                    err_msg = str(e2)
                                    self.parse_error = err_msg
                                    print(f"gtk2laz: [{_src_basename}] "
                                          f"retry {_retry}: still "
                                          f"failing: "
                                          f"{err_msg[:200]}",
                                          file=sys.stderr)
                                    continue
                            else:
                                print(f"gtk2laz: [{_src_basename}] "
                                      f"retry {_retry}: no identifier "
                                      f"found on line {line_num} "
                                      f"({err_line[:80]!r})",
                                      file=sys.stderr)
                    except (ValueError, IndexError):
                        pass
                    # If we couldn't extract a variable name, stop
                    # retrying to avoid an infinite loop.
                    break
                # Normal case: ``before: <ident>`` or ``Non-typedef 'X'``
                # Prepend a placeholder typedef for the missing type so
                # pycparser can resolve it on the next attempt.
                if "Missing type" in err_msg:
                    text = f"typedef int {missing};\n" + text
                    print(f"gtk2laz: [{_src_basename}] retry {_retry}: "
                          f"injecting typedef int {missing};",
                          file=sys.stderr)
                else:
                    text = f"typedef struct _{missing} {missing};\n" + text
                    print(f"gtk2laz: [{_src_basename}] retry {_retry}: "
                          f"injecting typedef struct _{missing} {missing};",
                          file=sys.stderr)
                try:
                    parser2 = CParser()
                    ast = parser2.parse(text, filename=self.c_path)
                    self.parse_error = ""
                    print(f"gtk2laz: [{_src_basename}] retry {_retry}: "
                          f"pycparser OK", file=sys.stderr)
                    break
                except Exception as e2:
                    err_msg = str(e2)
                    self.parse_error = err_msg
                    print(f"gtk2laz: [{_src_basename}] retry {_retry}: "
                          f"still failing: {err_msg[:200]}",
                          file=sys.stderr)
                    continue
            if ast is None:
                print(f"gtk2laz: [{_src_basename}] pycparser parse "
                      f"FAILED after all retries — returning False",
                      file=sys.stderr)
                return False
        # Filter: only keep top-level nodes whose coord.file matches our
        # source file (skip nodes coming from fake headers).
        target = os.path.basename(self.c_path)
        for ext in ast.ext:
            coord_file = ""
            if ext.coord and ext.coord.file:
                coord_file = os.path.basename(ext.coord.file)
            if coord_file != target:
                continue
            # Track line range
            start_line = ext.coord.line if ext.coord else 0
            end_line = start_line
            # FuncDef
            if isinstance(ext, c_ast.FuncDef):
                fn = _decl_to_func_signature(ext)
                if fn:
                    self.functions.append(fn)
                    if ext.body and ext.body.coord:
                        end_line = ext.body.coord.line + 50  # rough
                    self.func_lines[fn.name] = (start_line, end_line)
                continue
            # Typedef
            if isinstance(ext, c_ast.Typedef):
                td = _typedef_to_pas(ext)
                if td:
                    self.typedefs.append(td)
                continue
            # Decl (global var or function prototype)
            if isinstance(ext, c_ast.Decl):
                fn = _decl_to_func_signature(ext)
                if fn:
                    self.functions.append(fn)
                    self.func_lines[fn.name] = (start_line, start_line)
                else:
                    g = _decl_to_global(ext)
                    if g:
                        self.globals.append(g)
                continue
        return True

    # ------------------------------------------------------------------
    def emit_interface(self) -> str:
        """Emit the full Pascal unit text (interface + implementation).

        Bodies are left as TODO comments; the caller should patch them
        with the pattern-based translator's output."""
        out: list[str] = []
        out.append(f"unit {self.unit_name};")
        out.append("")
        out.append("{$mode objfpc}{$H+}")
        out.append("")
        out.append("interface")
        out.append("")
        out.append("uses")
        unit_list = (["Classes", "SysUtils", "Forms", "Controls",
                      "Graphics", "Dialogs", "StdCtrls", "ComCtrls",
                      "ExtCtrls", "Buttons", "Spin", "Menus", "LCLType"]
                     + self.included_units)
        if self.form_unit and self.form_unit != self.unit_name:
            unit_list.append(self.form_unit)
        for i, u in enumerate(unit_list):
            sep = "," if i < len(unit_list) - 1 else ";"
            out.append(f"  {u}{sep}")
        out.append("")
        out.append("type")
        out.append("  DWORD = Cardinal;")
        out.append("  WORD  = Word;")
        out.append("  BYTE  = Byte;")
        out.append("")
        # Typedefs
        if self.typedefs:
            out.append("type")
            for td in self.typedefs:
                out.append(td.pascal_decl)
            out.append("")
        # Globals
        if self.globals:
            out.append("var")
            seen = set()
            for g in self.globals:
                if g.name in seen:
                    continue
                seen.add(g.name)
                out.append(f"  {g.name}: {g.pascal_type};")
            out.append("")
        # Function prototypes
        if self.functions:
            out.append("// --- function declarations ---")
            for fn in self.functions:
                sig = self._pascal_signature(fn, prototype=True)
                if sig:
                    out.append(sig + ";")
            out.append("")
        out.append("implementation")
        out.append("")
        # Function bodies (Stage 1: TODO; caller patches with pattern output)
        for fn in self.functions:
            if fn.body_node is None:
                continue  # prototype only, no body needed
            sig = self._pascal_signature(fn, prototype=False)
            if not sig:
                continue
            out.append(sig + ";")
            out.append("begin")
            out.append(f"  (* TODO gtk2laz: body for {fn.name} - "
                       "patched by pattern translator *)")
            out.append("end;")
            out.append("")
        out.append("end.")
        return "\n".join(out) + "\n"

    # ------------------------------------------------------------------
    def _pascal_signature(self, fn: AstFunction,
                          prototype: bool = False) -> str:
        # Apply pascalify_name to all parameter names so they don't clash
        # with Pascal reserved words (e.g. 'string', 'file', 'name').
        params_str = "; ".join(
            f"{pascalify_name(n)}: {t}" for n, t in fn.params
        )
        is_func = fn.return_type and fn.return_type != "Pointer" \
            or (fn.return_type and fn.return_type != "void")
        # void return -> procedure; everything else -> function
        if fn.return_type in ("void", "Pointer") and not fn.params:
            # Heuristic: if the AST said void, use procedure.
            # If it said Pointer (which we use as void fallback), assume
            # procedure too.
            prefix = "procedure"
            ret = None
        elif fn.return_type == "void":
            prefix = "procedure"
            ret = None
        else:
            prefix = "function"
            ret = fn.return_type
        sig = f"{prefix} {pascalify_name(fn.name)}"
        if params_str:
            sig += f"({params_str})"
        if ret:
            sig += f": {ret}"
        return sig

    # ------------------------------------------------------------------
    def get_function_line_range(self, name: str) -> tuple[int, int]:
        return self.func_lines.get(name, (0, 0))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def parse_with_ast(c_path: str, unit_name: str,
                   fake_include_dir: str = "/tmp/fake_libc_include",
                   form_unit: Optional[str] = None,
                   included_units: list[str] = None,
                   target_platform: str = "auto") -> Optional[AstToPascal]:
    """Parse ``c_path`` with pycparser and return an AstToPascal object.

    Returns None if pycparser is unavailable or the file cannot be parsed
    (the caller should fall back to the pattern-based translator in that
    case).

    ``target_platform`` selects which platform's preset macros
    (``_WIN32``, ``__linux__``, ``__APPLE__`` etc.) are defined in
    pcpp before preprocessing. This controls which ``#ifdef`` branch
    is taken in platform-guarded headers. ``"auto"`` uses the running
    interpreter's platform.
    """
    if not _HAS_PYCPARSER:
        return None
    a2p = AstToPascal(c_path, unit_name, fake_include_dir,
                      form_unit, included_units,
                      target_platform=target_platform)
    if not a2p.parse():
        return None
    return a2p


def is_available() -> bool:
    """Return True if pycparser is importable."""
    return _HAS_PYCPARSER


# ===========================================================================
# Stage 2: AST-based body translator
# ===========================================================================
# Walks a c_ast.Compound (function body) and emits Pascal statements
# with accurate control flow (if/for/while/switch/return) and expression
# translation (binary/unary/calls/struct access/array index).
#
# Anything we cannot confidently translate becomes a multi-line TODO
# comment containing the original C source (re-using the same format
# as the pattern-based _emit_todo).

class AstBodyTranslator:
    """Translate a c_ast function body into Pascal statements.

    The translator is intentionally conservative: any node type it
    does not handle becomes a TODO comment so the project still
    compiles once the offending statement is replaced by a stub.
    """

    def __init__(self, ast_func, source_file: str = "",
                 indent: int = 2):
        self.func = ast_func
        self.source_file = source_file
        self.indent = indent
        self.lines: list[str] = []
        self.locals: list[str] = []
        self.todos: list[dict] = []
        self.todo_count = 0
        self.translated_count = 0
        self._pas_line = 1
        # Pre-statements collected during the translation of the current
        # top-level statement. When a C assignment is used as a sub-
        # expression (e.g. ``decodeRange /= total`` inside
        # ``code = (decodeCode - decodeLow) / (decodeRange /= total)``),
        # Pascal cannot keep the assignment inline because ``:=`` is
        # not an expression in Pascal. We split such assignments into
        # separate ``:=`` statements that are emitted *before* the
        # surrounding expression statement, and the assignment's lvalue
        # is used in place of the original sub-expression.
        # ``_emit_pre`` adds a line to this buffer; ``_emit`` flushes
        # the buffer (with the current pad) before appending its own
        # line, so the pre-statements appear in the right place.
        self._pre_lines: list[str] = []
        # Post-statements: similar to pre-statements but emitted AFTER
        # the current statement. Used for C post-increment in read
        # context (``*ptr++`` reads ptr^ then advances ptr — the Inc
        # must happen AFTER the value is used, not before).
        self._post_lines: list[str] = []

    # ------------------------------------------------------------------
    def translate(self) -> list[str]:
        """Translate the function body. Returns the list of Pascal
        statement lines (without the surrounding begin/end)."""
        self.lines = []
        if self.func.body_node is None:
            return self.lines
        # body_node is a c_ast.Compound with .block_items
        if hasattr(self.func.body_node, "block_items") and \
                self.func.body_node.block_items:
            for stmt in self.func.body_node.block_items:
                self._translate_stmt(stmt)
        # Flush any trailing pre-statements that were collected during
        # the last top-level statement but never emitted (e.g. when the
        # final statement is an expression-statement that produced
        # pre-statements but had no trailing ``_emit`` call of its own).
        self._flush_pre()
        self._flush_post()
        return self.lines

    # ------------------------------------------------------------------
    def _pad(self, extra: int = 0) -> str:
        return " " * (self.indent + extra * 2)

    def _emit_pre(self, text: str) -> None:
        """Record a side-effect statement (e.g. ``decodeRange := ...``)
        that must run *before* the surrounding expression statement.
        Flush happens automatically the next time ``_emit`` is called."""
        self._pre_lines.append(text)

    def _emit_post(self, text: str) -> None:
        """Record a side-effect statement (e.g. ``Inc(ptr)`` for
        ``*ptr++``) that must run *after* the surrounding expression
        statement. Flush happens automatically the next time ``_emit``
        or ``_emit_todo`` is called."""
        self._post_lines.append(text)

    def _flush_pre(self) -> None:
        """Emit any pending pre-statements at the current indent.
        Called by ``_emit`` and ``_emit_todo`` before they append."""
        if not self._pre_lines:
            return
        pad = self._pad()
        for line in self._pre_lines:
            self.lines.append(f"{pad}{line}")
            self._pas_line += 1
            self.translated_count += 1
        self._pre_lines = []

    def _flush_post(self) -> None:
        """Emit any pending post-statements at the current indent.
        Called by ``_emit`` and ``_emit_todo`` after they append."""
        if not self._post_lines:
            return
        pad = self._pad()
        for line in self._post_lines:
            self.lines.append(f"{pad}{line}")
            self._pas_line += 1
            self.translated_count += 1
        self._post_lines = []

    def _emit(self, text: str) -> None:
        self._flush_pre()
        self.lines.append(text)
        self._pas_line += 1
        self.translated_count += 1
        self._flush_post()

    def _emit_todo(self, node, reason: str = "") -> None:
        """Emit a multi-line TODO comment for an AST node we can't
        translate. Uses the node's coord for accurate source line."""
        self._flush_pre()
        self._flush_post()
        self.todo_count += 1
        pad = self._pad()
        body_pad = pad + "  "
        # Get source location
        c_line = 0
        src_file = self.source_file or "?"
        if node and hasattr(node, "coord") and node.coord:
            c_line = node.coord.line or 0
            if node.coord.file:
                src_file = node.coord.file
        # Get the original C text via c_generator
        from pycparser import c_generator
        try:
            gen = c_generator.CGenerator()
            code_text = gen.visit(node).strip()
        except Exception:
            code_text = str(type(node).__name__)
        # Limit to 30 lines / 600 chars to keep TODOs readable
        code_lines = code_text.split("\n")
        if len(code_lines) > 30:
            code_lines = code_lines[:30] + ["... (truncated)"]
        code_text = "\n".join(code_lines)
        if len(code_text) > 600:
            code_text = code_text[:600] + "..."
        reason_str = reason or "translate manually"
        lines_out = [
            f"{pad}(* TODO gtk2laz: {reason_str}",
            f"{body_pad}Source: {src_file}:{c_line}",
            f"{body_pad}Original C:",
        ]
        for cl in code_text.split("\n"):
            safe = cl.replace("*)", "* )")
            lines_out.append(f"{body_pad}  {safe}")
        lines_out.append(f"{pad}*)")
        for line in lines_out:
            self.lines.append(line)
            self._pas_line += 1
        out_line = self._pas_line - len(lines_out)
        self.todos.append({
            "source_file": self.source_file,
            "source_line": c_line,
            "source_text": code_text,
            "output_file": "",
            "output_line": out_line,
            "reason": reason_str,
            "function": self.func.name,
        })

    # ------------------------------------------------------------------
    # Statement translation
    # ------------------------------------------------------------------
    def _translate_stmt(self, node) -> None:
        if node is None:
            return
        # Dispatch on node type
        handler = getattr(self, f"_stmt_{type(node).__name__}", None)
        if handler:
            handler(node)
        else:
            self._emit_todo(node, f"unhandled statement: {type(node).__name__}")

    def _stmt_Decl(self, node) -> None:
        """Local variable declaration."""
        # node.name, node.type (TypeDecl/PtrDecl/ArrayDecl), node.init
        if not node.name:
            return
        pas_name = pascalify_name(node.name)
        ptype = _type_to_pascal(node.type)
        # Handle array decl
        if isinstance(node.type, c_ast.ArrayDecl):
            inner = _type_to_pascal(node.type.type)
            dim = _const_eval(node.type.dim) if node.type.dim else ""
            if dim:
                decl = f"  {pas_name}: array[0..{dim}-1] of {inner};"
            else:
                decl = f"  {pas_name}: array of {inner};"
            self.locals.append(decl)
        else:
            self.locals.append(f"  {pas_name}: {ptype};")
        # If there's an initializer, emit an assignment
        if node.init is not None:
            init_expr = self._translate_expr(node.init)
            pad = self._pad()
            self._emit(f"{pad}{pas_name} := {init_expr};")

    def _stmt_Return(self, node) -> None:
        pad = self._pad()
        if node.expr is None:
            self._emit(f"{pad}Exit;")
        else:
            expr = self._translate_expr(node.expr)
            self._emit(f"{pad}Exit({expr});")

    def _stmt_Break(self, node) -> None:
        self._emit(f"{self._pad()}Break;")

    def _stmt_Continue(self, node) -> None:
        self._emit(f"{self._pad()}Continue;")

    def _stmt_EmptyStatement(self, node) -> None:
        pass  # nothing to emit

    def _stmt_Compound(self, node) -> None:
        """A brace-enclosed block. In Pascal, we just emit the inner
        statements (Pascal uses begin/end for blocks, but a Compound
        inside an if/for body is already handled by the parent)."""
        if node.block_items:
            for stmt in node.block_items:
                self._translate_stmt(stmt)

    def _stmt_If(self, node) -> None:
        """if (cond) then-stmt else else-stmt"""
        pad = self._pad()
        cond = self._translate_expr(node.cond)
        self._emit(f"{pad}if {cond} then")
        self._emit_if_branch(node.iftrue, expect_else=node.iffalse is not None)
        if node.iffalse is not None:
            # Remove trailing ';' from the then-branch before 'else'
            # (Pascal: "if cond then stmt else stmt;" - no ';' before else)
            self._strip_last_semicolon()
            self._emit(f"{pad}else")
            self._emit_if_branch(node.iffalse, expect_else=False)

    def _strip_last_semicolon(self) -> None:
        """Remove trailing ';' from the last emitted line.
        Used before 'else' in Pascal."""
        if not self.lines:
            return
        last = self.lines[-1].rstrip()
        if last.endswith(";"):
            self.lines[-1] = last[:-1]

    def _emit_if_branch(self, branch, expect_else: bool) -> None:
        """Emit the body of an if/else branch. If the branch is a
        Compound with multiple statements, wrap in begin/end. If it's
        a single statement, emit it inline."""
        pad = self._pad()
        if branch is None:
            # empty branch
            if not expect_else:
                self._emit(f"{pad};  // empty then")
            return
        if isinstance(branch, c_ast.Compound):
            self._emit(f"{pad}begin")
            self.indent += 1
            if branch.block_items:
                for stmt in branch.block_items:
                    self._translate_stmt(stmt)
            self.indent -= 1
            end_token = "end;" if not expect_else else "end"
            self._emit(f"{pad}{end_token}")
        elif isinstance(branch, c_ast.If):
            # else if -> nested if
            self.indent += 1
            self._stmt_If(branch)
            self.indent -= 1
            if expect_else:
                # need to remove the trailing ; from the nested if
                # (handled by _stmt_If emitting end;)
                # For simplicity, we accept a stray ; here.
                pass
        else:
            # Single statement
            self.indent += 1
            self._translate_stmt(branch)
            self.indent -= 1

    def _stmt_While(self, node) -> None:
        pad = self._pad()
        cond = self._translate_expr(node.cond)
        self._emit(f"{pad}while {cond} do")
        self._emit(f"{pad}begin")
        self.indent += 1
        if node.stmt and hasattr(node.stmt, "block_items"):
            for stmt in (node.stmt.block_items or []):
                self._translate_stmt(stmt)
        elif node.stmt:
            self._translate_stmt(node.stmt)
        self.indent -= 1
        self._emit(f"{pad}end;")

    def _stmt_DoWhile(self, node) -> None:
        pad = self._pad()
        cond = self._translate_expr(node.cond)
        self._emit(f"{pad}repeat")
        self.indent += 1
        if node.stmt and hasattr(node.stmt, "block_items"):
            for stmt in (node.stmt.block_items or []):
                self._translate_stmt(stmt)
        elif node.stmt:
            self._translate_stmt(node.stmt)
        self.indent -= 1
        self._emit(f"{pad}until not ({cond});")

    def _stmt_For(self, node) -> None:
        """C for-loop. Translate common patterns:
            for (init; test; step) body
          -> for var := start to end do begin ... end;
          or  while test do begin ... init/step ... end;
        """
        pad = self._pad()
        init = node.init
        cond = node.cond
        nxt = node.next

        # ---- Pattern 0: bodyless for-loop -> while loop ----
        # C: for(init; cond; step);   (body is just ';' or empty braces)
        # This is a common idiom for "advance the loop variable until the
        # condition fails" (e.g. linear search:
        # ``for(z=0; z<DIMBUF-5 && bufferI[z]!=RX16; z++);``).
        # Pascal's for-to-do iterates over a fixed range and cannot express
        # this naturally (the loop variable's final value depends on when
        # the condition becomes false, not on a fixed end value), so we
        # always emit a while-loop. This also avoids emitting an empty-body
        # ``for ... to ... do begin end;`` for the simple case
        # ``for(i=0; i<N; i++);`` which would otherwise match Pattern 1.
        if self._is_empty_for_body(node.stmt):
            self._translate_bodyless_for(node, init, cond, nxt)
            return

        # ---- Pattern 1: for-to-do with i++ step ----
        # for (i=0; i<N; i++)  or  for (i=0; i<=N; i++)
        # Also handles ExprList init like for(i=0, count=sum=0; i<N; i++)
        var, start_expr, end_expr, is_less_than, is_less_equal, extra_inits = \
            self._match_for_to_do(init, cond, nxt)
        if var and start_expr and end_expr:
            pas_var = pascalify_name(var)
            # Emit extra init statements before the for loop
            for extra in extra_inits:
                self._emit(f"{pad}{extra}")
            if is_less_than:
                self._emit(f"{pad}for {pas_var} := {start_expr} to ({end_expr}) - 1 do")
            else:
                self._emit(f"{pad}for {pas_var} := {start_expr} to {end_expr} do")
            self._emit(f"{pad}begin")
            self.indent += 1
            self._emit_loop_body(node.stmt)
            self.indent -= 1
            self._emit(f"{pad}end;")
            return

        # ---- Pattern 2: for-to-do with i+=N step ----
        # for (i=0; i<N; i+=2)  ->  while loop with Inc(i, 2)
        var2, start_expr2, cond_pas2, step_var2, step_amount2, extra_inits2 = \
            self._match_for_with_step(init, cond, nxt)
        if var2 and start_expr2 and cond_pas2:
            pas_var2 = pascalify_name(var2)
            # Emit extra init statements
            for extra in extra_inits2:
                self._emit(f"{pad}{extra}")
            self._emit(f"{pad}{pas_var2} := {start_expr2};")
            self._emit(f"{pad}while {cond_pas2} do")
            self._emit(f"{pad}begin")
            self.indent += 1
            self._emit_loop_body(node.stmt)
            if step_amount2:
                self._emit(f"{self._pad()}Inc({pas_var2}, {step_amount2});")
            elif step_var2:
                self._emit(f"{self._pad()}Inc({pas_var2});")
            self.indent -= 1
            self._emit(f"{pad}end;")
            return

        # ---- Pattern 3: empty init with FuncCall/ID cond (e.g. for(;fgets(...);)) ----
        if init is None and cond is not None:
            cond_pas = self._translate_expr(cond)
            self._emit(f"{pad}while {cond_pas} do")
            self._emit(f"{pad}begin")
            self.indent += 1
            self._emit_loop_body(node.stmt)
            if nxt is not None:
                self._translate_for_step(nxt)
            self.indent -= 1
            self._emit(f"{pad}end;")
            return

        # ---- Pattern 4: init + cond + step, complex ----
        self._translate_complex_for(node, init, cond, nxt)

    def _is_empty_for_body(self, stmt) -> bool:
        """Return True if the for-loop body is empty.

        A bodyless C for-loop has the form ``for(init; cond; step);`` (note
        the trailing semicolon). pycparser represents the empty body as a
        ``c_ast.EmptyStatement``. We also treat an empty Compound
        (``for(init; cond; step){}``) and a Compound containing only empty
        statements as bodyless, since they are semantically equivalent.
        """
        if stmt is None:
            return True
        if isinstance(stmt, c_ast.EmptyStatement):
            return True
        if isinstance(stmt, c_ast.Compound):
            items = stmt.block_items or []
            if not items:
                return True
            if all(isinstance(it, c_ast.EmptyStatement) for it in items):
                return True
        return False

    def _translate_bodyless_for(self, node, init, cond, nxt) -> None:
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
        # ---- Emit init statements before the loop ----
        if init is not None:
            self._translate_for_init(init)
        # ---- Determine the condition ----
        if cond is None:
            # for(init;;step); -> infinite loop -> while True do
            cond_pas = "True"
        else:
            cond_pas = self._translate_expr(cond)
        # ---- Emit the while loop with the step as its body ----
        self._emit(f"{pad}while {cond_pas} do")
        self._emit(f"{pad}begin")
        self.indent += 1
        if nxt is not None:
            self._translate_for_step(nxt)
        self.indent -= 1
        self._emit(f"{pad}end;")

    def _match_for_to_do(self, init, cond, nxt):
        """Match the simple for(i=0; i<N or i<=N; i++) pattern.

        Also handles ExprList inits like ``for(i=0, count=sum=0; i<N; i++)``
        - the first assignment is the loop variable, the rest are emitted
        as extra init statements before the for loop.

        Returns (var, start_expr, end_expr, is_less_than, is_less_equal,
                 extra_inits) or (None, None, None, False, False, [])
        """
        var = None
        start_expr = None
        extra_inits = []
        if init is not None:
            if isinstance(init, c_ast.DeclList) and init.decls:
                first = init.decls[0]
                if first.name and first.init:
                    var = first.name
                    start_expr = self._translate_expr(first.init)
            elif isinstance(init, c_ast.Assignment):
                if isinstance(init.lvalue, c_ast.ID):
                    var = init.lvalue.name
                    start_expr = self._translate_expr(init.rvalue)
            elif isinstance(init, c_ast.ExprList):
                # for(i=0, count=sum=0; ...) - ExprList with multiple
                # comma-separated assignments
                if init.exprs:
                    first = init.exprs[0]
                    if isinstance(first, c_ast.Assignment) and \
                            isinstance(first.lvalue, c_ast.ID):
                        var = first.lvalue.name
                        start_expr = self._translate_expr(first.rvalue)
                    # Remaining expressions are extra inits
                    for extra_expr in init.exprs[1:]:
                        extra_str = self._flatten_assignment(extra_expr)
                        if extra_str:
                            extra_inits.append(extra_str)
        # Check condition: i < N or i <= N
        end_expr = None
        is_less_than = False
        is_less_equal = False
        if cond is not None and var is not None and isinstance(cond, c_ast.BinaryOp):
            if isinstance(cond.left, c_ast.ID) and cond.left.name == var:
                if cond.op == "<":
                    end_expr = self._translate_expr(cond.right)
                    is_less_than = True
                elif cond.op == "<=":
                    end_expr = self._translate_expr(cond.right)
                    is_less_equal = True
        # Check step: i++ or i = i+1
        is_inc = False
        if nxt is not None and var is not None:
            if isinstance(nxt, c_ast.UnaryOp) and nxt.op in ("p++", "++"):
                if isinstance(nxt.expr, c_ast.ID) and nxt.expr.name == var:
                    is_inc = True
            elif (isinstance(nxt, c_ast.Assignment) and nxt.op == "=" and
                  isinstance(nxt.lvalue, c_ast.ID) and nxt.lvalue.name == var and
                  isinstance(nxt.rvalue, c_ast.BinaryOp) and nxt.rvalue.op == "+" and
                  isinstance(nxt.rvalue.left, c_ast.ID) and
                  nxt.rvalue.left.name == var and
                  isinstance(nxt.rvalue.right, c_ast.Constant) and
                  nxt.rvalue.right.value == "1"):
                is_inc = True
        if var and start_expr and end_expr and is_inc:
            return (var, start_expr, end_expr, is_less_than,
                    is_less_equal, extra_inits)
        return (None, None, None, False, False, [])

    def _flatten_assignment(self, node) -> str:
        """Flatten a C assignment (possibly chained) to Pascal statement(s).

        Handles:
          count = 0          -> "count := 0;"
          count = sum = 0    -> "sum := 0; count := 0;"
          a = b = c = 5      -> "c := 5; b := 5; a := 5;"
        """
        if not isinstance(node, c_ast.Assignment):
            # Not an assignment - translate as expression statement
            expr = self._translate_expr(node)
            return f"{expr};"
        # Collect all (lhs, rhs_value) pairs by unwrapping chained assignments
        pairs = []
        current = node
        rhs_value = None
        while isinstance(current, c_ast.Assignment):
            lhs_name = self._translate_expr(current.lvalue)
            if isinstance(current.rvalue, c_ast.Assignment):
                # Chained: a = (b = ...) - keep unwrapping
                pairs.append(lhs_name)
                current = current.rvalue
            else:
                # Terminal: a = value
                rhs_value = self._translate_expr(current.rvalue)
                pairs.append(lhs_name)
                break
        # Emit in reverse order (innermost first): sum := 0; count := 0;
        if rhs_value is not None:
            stmts = []
            for name in reversed(pairs):
                stmts.append(f"{name} := {rhs_value};")
            return " ".join(stmts)
        return ""

    def _match_for_with_step(self, init, cond, nxt):
        """Match for(i=0; i<N; i+=K) or for(i=0; i<=N; i+=K) pattern.

        Also handles ExprList inits (same as _match_for_to_do).

        Returns (var, start_expr, cond_pas, step_var, step_amount,
                 extra_inits) or (None, None, None, None, None, [])
        """
        var = None
        start_expr = None
        extra_inits = []
        if init is not None:
            if isinstance(init, c_ast.DeclList) and init.decls:
                first = init.decls[0]
                if first.name and first.init:
                    var = first.name
                    start_expr = self._translate_expr(first.init)
            elif isinstance(init, c_ast.Assignment):
                if isinstance(init.lvalue, c_ast.ID):
                    var = init.lvalue.name
                    start_expr = self._translate_expr(init.rvalue)
            elif isinstance(init, c_ast.ExprList):
                if init.exprs:
                    first = init.exprs[0]
                    if isinstance(first, c_ast.Assignment) and \
                            isinstance(first.lvalue, c_ast.ID):
                        var = first.lvalue.name
                        start_expr = self._translate_expr(first.rvalue)
                    for extra_expr in init.exprs[1:]:
                        extra_str = self._flatten_assignment(extra_expr)
                        if extra_str:
                            extra_inits.append(extra_str)
        # Check condition is a BinaryOp involving var
        cond_pas = None
        if cond is not None and var is not None:
            if isinstance(cond, c_ast.BinaryOp):
                if (isinstance(cond.left, c_ast.ID) and
                    cond.left.name == var):
                    cond_pas = self._translate_expr(cond)
                elif (isinstance(cond.right, c_ast.ID) and
                      cond.right.name == var):
                    cond_pas = self._translate_expr(cond)
        # Check step: i += K
        step_var = None
        step_amount = None
        if nxt is not None and var is not None:
            if isinstance(nxt, c_ast.Assignment) and nxt.op == "+=":
                if isinstance(nxt.lvalue, c_ast.ID) and nxt.lvalue.name == var:
                    step_var = var
                    step_amount = self._translate_expr(nxt.rvalue)
            elif isinstance(nxt, c_ast.Assignment) and nxt.op == "-=":
                if isinstance(nxt.lvalue, c_ast.ID) and nxt.lvalue.name == var:
                    step_var = var
                    step_amount = "-" + self._translate_expr(nxt.rvalue)
        if var and start_expr and cond_pas and step_amount:
            return (var, start_expr, cond_pas, step_var, step_amount, extra_inits)
        return (None, None, None, None, None, [])

    def _emit_loop_body(self, stmt) -> None:
        """Emit the body of a loop (For/While/DoWhile)."""
        if stmt and hasattr(stmt, "block_items"):
            for s in (stmt.block_items or []):
                self._translate_stmt(s)
        elif stmt:
            self._translate_stmt(stmt)

    def _translate_complex_for(self, node, init, cond, nxt) -> None:
        """Translate a C for-loop that doesn't match the simple
        ``for (i=0; i<N; i++)`` pattern.

        This handler deals with:
          * Comma operators in init/step: ``for (a=0, b=10; a<b; a++, b--)``
          * Multiple declarations in init: ``for (int i=0, j=10; ...)``
          * Complex conditions: ``for (; i<n && flag; ...)``
          * Non-constant step: ``for (; i<n; i+=2)``
          * Empty parts: ``for (;;)`` (infinite loop)

        Strategy: emit the init as statements before the loop, translate
        the loop as a ``while`` with the condition, and emit the step
        at the end of the loop body. This is always semantically correct
        (though slightly more verbose than a for-to-do).
        """
        pad = self._pad()
        # ---- Emit init statements ----
        if init is not None:
            self._translate_for_init(init)
        # ---- Determine the condition ----
        if cond is None:
            # for (;;) -> infinite loop -> while True do
            cond_pas = "True"
        else:
            cond_pas = self._translate_expr(cond)
        # ---- Emit the while loop ----
        self._emit(f"{pad}while {cond_pas} do")
        self._emit(f"{pad}begin")
        self.indent += 1
        # Loop body
        if node.stmt and hasattr(node.stmt, "block_items"):
            for stmt in (node.stmt.block_items or []):
                self._translate_stmt(stmt)
        elif node.stmt:
            self._translate_stmt(node.stmt)
        # ---- Emit the step at the end of the body ----
        if nxt is not None:
            self._translate_for_step(nxt)
        self.indent -= 1
        self._emit(f"{pad}end;")

    def _translate_for_init(self, init) -> None:
        """Translate the init part of a for-loop.

        Handles:
          * DeclList with one or more declarations
          * Assignment (single, including chained: a = b = 0)
          * Comma-separated assignments (ExprList)
          * UnaryOp (e.g. i++)
        """
        pad = self._pad()
        if init is None:
            return
        # DeclList: for (int i=0, j=10; ...)
        if isinstance(init, c_ast.DeclList):
            for decl in (init.decls or []):
                if isinstance(decl, c_ast.Decl):
                    self._stmt_Decl(decl)
            return
        # ExprList: for (a=0, b=10; ...) - comma-separated expressions
        if isinstance(init, c_ast.ExprList):
            for expr in init.exprs:
                # Use _flatten_assignment for chained assignments
                # like count = sum = 0
                flat = self._flatten_assignment(expr)
                if flat:
                    self._emit(f"{pad}{flat}")
                else:
                    self._translate_for_step(expr)
            return
        # Single Assignment (including chained: a = b = 0)
        if isinstance(init, c_ast.Assignment):
            flat = self._flatten_assignment(init)
            if flat:
                self._emit(f"{pad}{flat}")
            else:
                self._stmt_Assignment(init)
            return
        # Single Decl
        if isinstance(init, c_ast.Decl):
            self._stmt_Decl(init)
            return
        # Single UnaryOp (e.g. for (i++; ...))
        if isinstance(init, c_ast.UnaryOp):
            self._stmt_UnaryOp(init)
            return
        # Fall back: emit as expression statement
        expr = self._translate_expr(init)
        self._emit(f"{pad}{expr};")

    def _translate_for_step(self, step) -> None:
        """Translate the step (next) part of a for-loop.

        Handles:
          * UnaryOp: i++, i--, ++i, --i
          * Assignment: i = i+1, i += 2, etc.
            - ``i += K`` and ``i -= K`` are emitted as ``Inc(i, K)`` /
              ``Dec(i, K)`` for readability (matching the simple ``i++``
              case), since these are almost always used as for-loop steps.
              The plain-assignment form is still produced when the same
              operator appears as a real statement (via ``_stmt_Assignment``).
          * Comma-separated: ExprList of the above
          * FuncCall: e.g. iter = next(iter)
        """
        pad = self._pad()
        if step is None:
            return
        # ExprList: comma-separated steps (a++, b--)
        if isinstance(step, c_ast.ExprList):
            for expr in step.exprs:
                self._translate_for_step(expr)
            return
        # UnaryOp: i++ -> Inc(i);  i-- -> Dec(i)
        if isinstance(step, c_ast.UnaryOp):
            self._stmt_UnaryOp(step)
            return
        # Assignment: i += 2, i = i+1, etc.
        if isinstance(step, c_ast.Assignment):
            # i += K  ->  Inc(i, K);   i -= K  ->  Dec(i, K);
            # This is more idiomatic Pascal than "i := i + K;" and matches
            # how we translate i++ (Inc(i)) in the simple for-loop case.
            if step.op in ("+=", "-=") and isinstance(step.lvalue, c_ast.ID):
                var_name = pascalify_name(step.lvalue.name)
                amount = self._translate_expr(step.rvalue)
                fn = "Inc" if step.op == "+=" else "Dec"
                self._emit(f"{pad}{fn}({var_name}, {amount});")
                return
            self._stmt_Assignment(step)
            return
        # FuncCall as step
        if isinstance(step, c_ast.FuncCall):
            self._stmt_FuncCall(step)
            return
        # Fall back: emit as expression statement
        expr = self._translate_expr(step)
        self._emit(f"{pad}{expr};")

    def _stmt_Switch(self, node) -> None:
        pad = self._pad()
        cond = self._translate_expr(node.cond)
        self._emit(f"{pad}case {cond} of")
        self.indent += 1
        # pycparser's Switch uses node.stmt (a Compound) not node.stmts
        stmt = node.stmt
        if stmt is None:
            stmt = type('X', (), {'block_items': []})()
        cases = getattr(stmt, 'block_items', []) or []
        for case in cases:
            if isinstance(case, c_ast.Case):
                val = self._translate_expr(case.expr)
                self._emit(f"{self._pad()}{val}:")
                self._emit(f"{self._pad()}begin")
                self.indent += 1
                for stmt in (case.stmts or []):
                    if not isinstance(stmt, c_ast.Break):
                        self._translate_stmt(stmt)
                self.indent -= 1
                self._emit(f"{self._pad()}end;")
            elif isinstance(case, c_ast.Default):
                self._emit(f"{self._pad()}else")
                self._emit(f"{self._pad()}begin")
                self.indent += 1
                for stmt in (case.stmts or []):
                    if not isinstance(stmt, c_ast.Break):
                        self._translate_stmt(stmt)
                self.indent -= 1
                self._emit(f"{self._pad()}end;")
        self.indent -= 1
        self._emit(f"{pad}end;")

    def _stmt_Assignment(self, node) -> None:
        """Assignment: lvalue op= rvalue

        For chained assignments like ``count = sum = 0``, flattens to
        ``sum := 0; count := 0;`` using _flatten_assignment.

        Special handling for ``*ptr++ = value`` (C idiom: write to
        current position, then advance pointer):
            ptr^ := value;
            Inc(ptr);
        """
        pad = self._pad()
        op = node.op
        # Handle chained assignment: a = b = 0
        if op == "=" and isinstance(node.rvalue, c_ast.Assignment):
            flat = self._flatten_assignment(node)
            if flat:
                self._emit(f"{pad}{flat}")
                return

        # ---- Special case: *ptr++ = value  (and *ptr-- = value)
        # C: ``*(ptr++) = value`` writes value to *ptr, then advances
        # ptr. In Pascal this must be split into two statements:
        #   ptr^ := value;
        #   Inc(ptr);
        # If we naively translate the lvalue with _translate_expr,
        # we get ``Inc(ptr)^ := value`` which is invalid Pascal
        # (``Inc`` doesn't return a value).
        if op == "=" and isinstance(node.lvalue, c_ast.UnaryOp) \
                and node.lvalue.op == "*":
            inner = node.lvalue.expr
            if isinstance(inner, c_ast.UnaryOp) \
                    and inner.op in ("p++", "p--", "++", "--"):
                ptr = self._translate_expr(inner.expr)
                rhs = self._translate_expr(node.rvalue)
                self._emit(f"{pad}{ptr}^ := {rhs};")
                if "++" in inner.op:
                    self._emit(f"{pad}Inc({ptr});")
                else:
                    self._emit(f"{pad}Dec({ptr});")
                return

        lhs = self._translate_expr(node.lvalue)
        rhs = self._translate_expr(node.rvalue)
        if op == "=":
            self._emit(f"{pad}{lhs} := {rhs};")
        elif op == "+=":
            self._emit(f"{pad}{lhs} := {lhs} + {rhs};")
        elif op == "-=":
            self._emit(f"{pad}{lhs} := {lhs} - {rhs};")
        elif op == "*=":
            self._emit(f"{pad}{lhs} := {lhs} * {rhs};")
        elif op == "/=":
            self._emit(f"{pad}{lhs} := {lhs} div {rhs};")
        elif op == "%=":
            self._emit(f"{pad}{lhs} := {lhs} mod {rhs};")
        elif op == "<<=":
            self._emit(f"{pad}{lhs} := {lhs} shl {rhs};")
        elif op == ">>=":
            self._emit(f"{pad}{lhs} := {lhs} shr {rhs};")
        elif op == "&=":
            self._emit(f"{pad}{lhs} := {lhs} and {rhs};")
        elif op == "|=":
            self._emit(f"{pad}{lhs} := {lhs} or {rhs};")
        elif op == "^=":
            self._emit(f"{pad}{lhs} := {lhs} xor {rhs};")
        else:
            self._emit_todo(node, f"assignment op {op}")

    def _stmt_UnaryOp(self, node) -> None:
        """++/-- as statement (not expression)"""
        pad = self._pad()
        if node.op in ("p++", "p--"):
            operand = self._translate_expr(node.expr)
            op = "Inc" if node.op == "p++" else "Dec"
            self._emit(f"{pad}{op}({operand});")
        elif node.op in ("++", "--"):
            operand = self._translate_expr(node.expr)
            op = "Inc" if node.op == "++" else "Dec"
            self._emit(f"{pad}{op}({operand});")
        else:
            # Other unary ops as expression statement
            expr = self._translate_expr(node)
            self._emit(f"{pad}{expr};")

    def _stmt_FuncCall(self, node) -> None:
        """Function call as a statement."""
        pad = self._pad()
        expr = self._translate_expr(node)
        self._emit(f"{pad}{expr};")

    def _stmt_Typedef(self, node) -> None:
        """Local typedef inside a function body.

        C allows typedef inside function bodies (local typedefs). In
        Pascal there's no exact equivalent, so we emit them as comments
        so the user can add them by hand if needed. This matches the
        pattern-based translator's behaviour.
        """
        pad = self._pad()
        # Extract the typedef name (node.name holds it)
        name = getattr(node, 'name', '') or ''
        if name:
            # Also record it so subsequent variable declarations using
            # this local typedef can be recognised by other AST
            # handlers (e.g. _stmt_Decl).
            if not hasattr(self, '_local_typedef_names'):
                self._local_typedef_names = set()
            self._local_typedef_names.add(name)
            self._emit(f"{pad}// local typedef: {name}")
        else:
            # Fall back to a generic comment with the node type
            self._emit(f"{pad}// local typedef (unrecognised form)")

    def _stmt_Pragma(self, node) -> None:
        pass  # ignore pragmas

    def _stmt_Label(self, node) -> None:
        """C label: ``label_name:`` — emit as Pascal label.
        Also record the label so it can be declared in a ``label`` block."""
        pad = self._pad()
        label_name = node.name
        # Record the label for later declaration in the label block
        if not hasattr(self, '_labels'):
            self._labels = []
        if label_name not in self._labels:
            self._labels.append(label_name)
        # If the label has a body (stmt), translate it after the label.
        # pycparser's Label node uses ``.stmt`` (singular) — it is either
        # a single statement node or None. Older code mistakenly used the
        # plural ``.stmts``, which raises AttributeError and aborts the
        # entire function body translation (causing the caller to fall
        # back to the pattern-based translator, which then loses all
        # local variable declarations for unknown typedefs).
        if node.stmt is not None:
            self._emit(f"{pad}{label_name}:")
            self.indent += 1
            self._translate_stmt(node.stmt)
            self.indent -= 1
        else:
            self._emit(f"{pad}{label_name}:")

    def _stmt_Goto(self, node) -> None:
        """C goto: ``goto label;`` — emit as Pascal goto."""
        pad = self._pad()
        label_name = node.name
        # Record the label for declaration
        if not hasattr(self, '_labels'):
            self._labels = []
        if label_name not in self._labels:
            self._labels.append(label_name)
        self._emit(f"{pad}goto {label_name};")

    # ------------------------------------------------------------------
    # Expression translation
    # ------------------------------------------------------------------
    def _translate_expr(self, node) -> str:
        if node is None:
            return "nil"
        handler = getattr(self, f"_expr_{type(node).__name__}", None)
        if handler:
            return handler(node)
        # Fall back to c_generator dump
        return self._dump(node)

    def _expr_Constant(self, node) -> str:
        val = node.value
        # String literal: pycparser tags string constants with type='string'
        # and value still wrapped in the original double quotes.  We strip
        # the quotes and process C escape sequences:
        #   \n  -> ' + NL + '   (single LF, matches the C ``#define NL "\n"``
        #                        macro used throughout opgui/strings.c)
        #   \t  -> ' + #9 + '
        #   \r  -> ' + #13 + '
        #   \0  -> ' + #0 + '
        #   \"  -> "
        #   \'  -> ''
        #   \\  -> \
        # Note: this MUST be done before the hex/char/float branches below
        # because a string literal's first character can be ``'`` (when the
        # C source uses ``"'"``) and would otherwise be mistaken for a char.
        if getattr(node, "type", None) == "string":
            return self._convert_c_string_literal(val)
        # Hex literal
        if val.startswith("0x") or val.startswith("0X"):
            return f"${val[2:]}"
        # Char literal 'a' -> #97
        if val.startswith("'") and val.endswith("'") and len(val) >= 3:
            if len(val) == 3:
                return f"#{ord(val[1])}"
            # escape sequences
            inner = val[1:-1]
            if inner == "\\n":
                return "#10"
            if inner == "\\t":
                return "#9"
            if inner == "\\0":
                return "#0"
            if inner == "\\\\":
                return "#92"
            return f"'{inner}'"
        # Float
        if "." in val and ("e" in val.lower() or "." in val):
            return val
        # Integer
        if val.endswith("L") or val.endswith("l"):
            val = val[:-1]
        if val.endswith("U") or val.endswith("u"):
            val = val[:-1]
        return val

    @staticmethod
    def _convert_c_string_literal(val: str) -> str:
        """Translate a C double-quoted string literal (as exposed by
        pycparser's ``Constant.value``) into a Pascal string expression.

        Embedded ``\\n`` is split out as ``' + NL + '`` so the generated
        source reads the way opgui's strings.c declares multi-line
        strings (the converted unit emits ``const NL = #10;`` up front).
        Standalone ``"\\n"`` collapses to the bare ``NL`` symbol rather
        than ``'' + NL + ''`` so that ``strings_en[S_NL] := NL;`` matches
        the original C intent of ``strings_en[S_NL] = NL;``.
        """
        # pycparser stores the value with the surrounding double quotes
        # intact, and the escape sequences as raw backslash-pairs (e.g.
        # the Python string ``"hello\\nworld"`` — note the double backslash
        # — represents the C source ``"hello\nworld"``).
        if not (val.startswith('"') and val.endswith('"') and len(val) >= 2):
            # Defensive: shouldn't happen for type='string', but be safe.
            inner = val
        else:
            inner = val[1:-1]
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

    def _expr_ID(self, node) -> str:
        name = node.name
        # C's NULL macro → Pascal's nil. pycparser treats NULL as an
        # identifier (not a constant), so we intercept it here.
        if name == "NULL":
            return "nil"
        return pascalify_name(name)

    def _expr_BinaryOp(self, node) -> str:
        left = self._translate_expr(node.left)
        right = self._translate_expr(node.right)
        op = node.op
        # C -> Pascal operator mapping
        op_map = {
            "+": "+", "-": "-", "*": "*", "/": "div",
            "%": "mod",
            "<<": "shl", ">>": "shr",
            "&": "and", "|": "or", "^": "xor",
            "&&": "and", "||": "or",
            "==": "=", "!=": "<>",
            "<": "<", ">": ">", "<=": "<=", ">=": ">=",
        }
        pas_op = op_map.get(op, op)

        # ---- Parenthesisation based on PASCAL operator precedence.
        #
        # C and Pascal have DIFFERENT operator precedence tables:
        #
        #   C:  * / %  >  + -  >  << >>  >  < > <= >=  >  == !=
        #       >  &  >  ^  >  |  >  &&  >  ||
        #
        #   Pascal:  * / div mod and shl shr  (ALL same level)
        #            >  + - or xor  (ALL same level)
        #            >  = <> < > <= >=  (ALL same level)
        #
        # This means C expressions like ``a + b << c`` (C: ``a + (b << c)``)
        # would parse WRONG in Pascal as ``(a + b) shl c`` because
        # ``+`` and ``shl`` are at the SAME precedence level in Pascal.
        #
        # Solution: use PASCAL precedence (not C precedence) to decide
        # whether child sub-expressions need parentheses. A child
        # needs parens when its Pascal-precedence is LOWER than the
        # parent's Pascal-precedence. When they are at the SAME level
        # but are DIFFERENT operators, we also parenthesise to preserve
        # the C parse order (because Pascal evaluates same-level
        # operators left-to-right, but C's parse tree may have grouped
        # them differently due to C's finer-grained precedence).
        #
        # Pascal precedence levels (higher = binds tighter):
        #   3:  * / div mod and shl shr
        #   2:  + - or xor
        #   1:  = <> < > <= >=
        PASCAL_PREC = {
            # Level 3
            "*": 3, "/": 3, "div": 3, "mod": 3, "and": 3,
            "shl": 3, "shr": 3,
            # Level 2
            "+": 2, "-": 2, "or": 2, "xor": 2,
            # Level 1
            "=": 1, "<>": 1, "<": 1, ">": 1, "<=": 1, ">=": 1,
        }

        def _needs_parens(child_node, parent_pas_op):
            """Return True if the child expression needs parentheses
            when used as an operand of ``parent_pas_op``.

            We use PASCAL precedence (not C precedence) because the
            output is Pascal code that must parse correctly under
            Pascal's precedence rules.

            Two cases need parens:
            1. Child's Pascal precedence < parent's → always parenthesise
               (e.g. ``a + b`` inside ``c * ...`` → ``(a + b) * c``).
            2. Child's Pascal precedence == parent's BUT different
               operator → parenthesise to preserve C's grouping.
               Pascal evaluates same-level ops left-to-right, but the
               C AST may have grouped them differently.

            3. EXTRA RULE for shl/shr: even though Pascal's precedence
               makes ``1 shl 16 or 3 shl 8`` parse correctly (shl is
               level 3, or is level 2), the user wants explicit
               parentheses around shift results for readability:
               ``(1 shl 16) or (3 shl 8) or 0``. So we always
               parenthesise a shl/shr child when the parent is NOT
               also shl/shr.
            """
            if not isinstance(child_node, c_ast.BinaryOp):
                return False
            child_pas_op = op_map.get(child_node.op, child_node.op)
            child_prec = PASCAL_PREC.get(child_pas_op, 0)
            parent_prec = PASCAL_PREC.get(parent_pas_op, 0)
            # Rule 1: lower precedence needs parens
            if child_prec < parent_prec:
                return True
            # Rule 2: same precedence, different operator needs parens
            if child_prec == parent_prec and child_pas_op != parent_pas_op:
                return True
            # Rule 3: shl/shr results always parenthesised for
            # readability when parent is not also shl/shr.
            if child_pas_op in ("shl", "shr") \
                    and parent_pas_op not in ("shl", "shr"):
                return True
            return False

        left_node = node.left
        right_node = node.right
        # Integer division needs special handling: C '/' for ints is div,
        # but for floats it's '/'. We default to div since most C code
        # here uses integer division.
        if op == "/":
            l = f"({left})" if _needs_parens(left_node, "div") else left
            r = f"({right})" if _needs_parens(right_node, "div") else right
            return f"{l} div {r}"
        l = f"({left})" if _needs_parens(left_node, pas_op) else left
        r = f"({right})" if _needs_parens(right_node, pas_op) else right
        return f"{l} {pas_op} {r}"

    def _expr_UnaryOp(self, node) -> str:
        operand = self._translate_expr(node.expr)
        op = node.op
        if op == "!":
            return f"not ({operand})"
        if op == "~":
            return f"not ({operand})"
        if op == "-":
            # Negation: only parenthesise when the operand is a
            # complex expression (BinaryOp, etc.). For simple
            # operands (constants, identifiers, function calls),
            # no parens are needed:
            #   -(1)  → -1
            #   -(a)  → -a
            #   -(a + b) → -(a + b)
            if isinstance(node.expr, (c_ast.Constant, c_ast.ID,
                                      c_ast.FuncCall)):
                return f"-{operand}"
            return f"-({operand})"
        if op == "+":
            if isinstance(node.expr, (c_ast.Constant, c_ast.ID,
                                      c_ast.FuncCall)):
                return f"+{operand}"
            return f"+({operand})"
        if op == "&":
            # Address-of: only parenthesise complex expressions.
            if isinstance(node.expr, (c_ast.Constant, c_ast.ID,
                                      c_ast.FuncCall)):
                return f"@{operand}"
            return f"@({operand})"
        if op == "*":
            # Special case: *(ptr++) — dereference then increment.
            # C: ``*ptr++`` reads *ptr (current value), THEN advances
            # ptr. Post-increment means the increment happens AFTER
            # the value is used.
            # Pascal: we return ``ptr^`` (the current value) and emit
            # ``Inc(ptr)`` as a POST-statement so it runs after the
            # surrounding expression statement.
            # Without this, ``Inc(ptr)^`` is invalid Pascal, and using
            # _emit_pre would increment BEFORE reading (wrong order).
            if isinstance(node.expr, c_ast.UnaryOp) \
                    and node.expr.op in ("p++", "p--"):
                ptr = self._translate_expr(node.expr.expr)
                if "++" in node.expr.op:
                    self._emit_post(f"Inc({ptr});")
                else:
                    self._emit_post(f"Dec({ptr});")
                return f"{ptr}^"
            return f"{operand}^"
        if op in ("p++", "p--", "++", "--"):
            # Post-increment (p++) / pre-increment (++) as expression.
            # C: ``x = y++`` assigns the current value of y to x, then
            # increments y. ``x = ++y`` increments y first, then
            # assigns the new value.
            # Pascal: neither Inc nor Dec returns a value, so we
            # cannot use them as expressions. We use _emit_post to
            # emit the Inc/Dec AFTER the surrounding statement (for
            # post-increment) or _emit_pre to emit it BEFORE (for
            # pre-increment), and return the operand itself as the
            # "value" of the expression.
            sub_op = "Inc" if "++" in op else "Dec"
            # post-increment (p++): Inc/Dec AFTER the statement
            if op in ("p++", "p--"):
                self._emit_post(f"{sub_op}({operand});")
                return operand
            # pre-increment (++): Inc/Dec BEFORE the statement
            else:
                self._emit_pre(f"{sub_op}({operand});")
                return operand
        if op == "sizeof":
            return f"SizeOf({operand})"
        return f"({op} {operand})"

    def _expr_Assignment(self, node) -> str:
        # Assignment used as expression (C allows this; Pascal does not).
        # We emit a Pascal ``:=`` assignment as a *side effect* before
        # the surrounding expression, then return the lvalue as the
        # "value" of the expression. This works because the parent
        # emitter collects statements via ``self._emit_pre`` and inserts
        # them just before the current statement.
        #
        # Compound operators (``/=``, ``+=`` etc.) are rewritten to the
        # equivalent ``lhs := lhs <op> rhs`` form so that pycparser's
        # ``Assignment`` node with ``op="/="`` is handled correctly
        # instead of being silently dropped (which previously produced
        # ``decodeRange := total`` for ``decodeRange /= total``,
        # discarding the divide).
        lhs = self._translate_expr(node.lvalue)
        rhs = self._translate_expr(node.rvalue)
        op = node.op
        # Compound-assignment operator → equivalent Pascal binary op.
        # ``/=`` → ``div``, ``%=`` → ``mod``, ``<<=`` → ``shl``, etc.
        compound_to_pas = {
            "+=": "+", "-=": "-",
            "*=": "*", "/=": "div", "%=": "mod",
            "<<=": "shl", ">>=": "shr",
            "&=": "and", "|=": "or", "^=": "xor",
        }
        if op == "=":
            # Plain assignment: emit ``lhs := rhs;`` as a pre-statement
            # and return ``lhs`` as the expression's value.
            self._emit_pre(f"{lhs} := {rhs};")
            return lhs
        if op in compound_to_pas:
            pas_op = compound_to_pas[op]
            self._emit_pre(f"{lhs} := {lhs} {pas_op} {rhs};")
            return lhs
        # Unknown operator — fall back to the old behaviour with a
        # marker so the user can review.
        return f"({lhs} := {rhs})  /* TODO: unsupported assign-as-expr op {op} */"

    def _expr_FuncCall(self, node) -> str:
        # node.name is the function reference, node.args is ExprList
        if node.name is None:
            return ""
        name = self._translate_expr(node.name)
        args = []
        if node.args:
            for arg in node.args.exprs:
                args.append(self._translate_expr(arg))
        return f"{name}({', '.join(args)})"

    def _expr_ArrayRef(self, node) -> str:
        arr = self._translate_expr(node.name)
        idx = self._translate_expr(node.subscript)
        return f"{arr}[{idx}]"

    def _expr_StructRef(self, node) -> str:
        """struct.field or struct->field

        pycparser's StructRef uses:
          node.name  - the struct expression (e.g. ``s`` in ``s.field``)
          node.type  - '.' or '->'
          node.field - the field name (an ID node)
        """
        struct = self._translate_expr(node.name)
        field = node.field.name if hasattr(node.field, "name") else str(node.field)
        field = pascalify_name(field)
        if node.type == "->":
            # ptr->field  ->  ptr^.field  (deref then field access)
            # Pascal pointer dereference uses ``^`` and the field
            # access uses ``.``, so the full expression is
            # ``ptr^.field`` (NOT ``ptr^field`` — that would be a
            # pointer dereference with no field access, which Pascal
            # does not accept).  The previous code emitted
            # ``ptr^field`` (missing the dot), producing invalid
            # Pascal like ``info^family`` instead of ``info^.family``.
            return f"{struct}^.{field}"
        return f"{struct}.{field}"

    def _expr_Cast(self, node) -> str:
        # C cast: (type)expr -> in Pascal we mostly drop the cast
        inner = self._translate_expr(node.expr)
        return inner

    def _expr_TernaryOp(self, node) -> str:
        """cond ? iftrue : iffalse -> IfThen(...)"""
        cond = self._translate_expr(node.cond)
        iftrue = self._translate_expr(node.iftrue)
        iffalse = self._translate_expr(node.iffalse)
        return f"IfThen({cond}, {iftrue}, {iffalse})"

    def _expr_Constant(self, node) -> str:
        # Duplicate handler kept for safety; the primary _expr_Constant
        # above is what the dispatcher will actually call (Python class
        # attribute lookup uses the first definition).  Still route string
        # constants through _convert_c_string_literal so any caller that
        # happens to invoke this version gets correct behaviour too.
        val = node.value
        if getattr(node, "type", None) == "string":
            return self._convert_c_string_literal(val)
        if val.startswith("0x") or val.startswith("0X"):
            return f"${val[2:]}"
        if val.startswith("'") and val.endswith("'") and len(val) >= 3:
            if len(val) == 3:
                return f"#{ord(val[1])}"
            inner = val[1:-1]
            if inner == "\\n":
                return "#10"
            if inner == "\\t":
                return "#9"
            return f"'{inner}'"
        if val.endswith("L") or val.endswith("l"):
            val = val[:-1]
        if val.endswith("U") or val.endswith("u"):
            val = val[:-1]
        return val

    def _expr_ExprList(self, node) -> str:
        """Comma-separated expressions (rare as a value)."""
        parts = [self._translate_expr(e) for e in node.exprs]
        return ", ".join(parts)

    def _expr_InitList(self, node) -> str:
        """Array initializer {a, b, c} - Pascal uses [a, b, c]."""
        parts = [self._translate_expr(e) for e in (node.exprs or [])]
        return f"[{', '.join(parts)}]"

    def _expr_NamedInitializer(self, node) -> str:
        """Struct designated initializer .field = value."""
        return self._translate_expr(node.expr)

    def _dump(self, node) -> str:
        if node is None:
            return ""
        try:
            from pycparser import c_generator
            gen = c_generator.CGenerator()
            return gen.visit(node).strip()
        except Exception:
            return type(node).__name__


# ---------------------------------------------------------------------------
# Convenience: translate a function body using AST
# ---------------------------------------------------------------------------
def translate_body_with_ast(ast_func, source_file: str = "",
                             indent: int = 2) -> dict:
    """Translate an AST function's body into Pascal statements.

    Returns a dict with:
        lines   : list of Pascal statement strings
        locals  : list of local var declaration strings
        labels  : list of label names (for the Pascal ``label`` block)
        todos   : list of TodoItem dicts
        stats   : dict with translated_count, todo_count
    """
    t = AstBodyTranslator(ast_func, source_file, indent)
    lines = t.translate()
    # Deduplicate local variable declarations.
    #
    # C allows the same variable name to be declared in multiple nested
    # scopes (e.g. inside different branches of an if/else chain, or
    # inside different invocations of the same macro that each declare
    # ``ENetSymbol * node = ...;``). The AST flattens all these Decl
    # nodes into a single ``self.locals`` list, producing duplicate
    # entries like:
    #     node: ^TENetSymbol;
    #     node: ^TENetSymbol;
    # Pascal forbids duplicate names in a single ``var`` block, so we
    # dedupe by variable name, keeping the first occurrence. If a later
    # scope uses the same name with a different type, the user must
    # rename by hand (we keep the first type seen — that is the most
    # common case in macro-heavy C like enet's compress.c).
    seen_names: dict[str, str] = {}
    deduped: list[str] = []
    for decl in t.locals:
        m = re.match(r"^\s*(\w+)\s*:", decl)
        if m:
            name = m.group(1)
            if name in seen_names:
                # Skip duplicate. If the type differs from the first
                # occurrence, annotate the first decl with a comment so
                # the user knows there is a type mismatch to review.
                if seen_names[name] != decl.strip():
                    # Find and annotate the first decl if not yet annotated.
                    for i, d in enumerate(deduped):
                        if re.match(rf"^\s*{re.escape(name)}\s*:", d) \
                                and "// also declared as" not in d:
                            deduped[i] = (
                                d.rstrip().rstrip(";")
                                + f"  // also declared as: {decl.strip()};"
                            )
                            break
                continue
            seen_names[name] = decl.strip()
        deduped.append(decl)
    return {
        "lines": lines,
        "locals": deduped,
        "labels": getattr(t, '_labels', []),
        "todos": t.todos,
        "stats": {
            "translated_count": t.translated_count,
            "todo_count": t.todo_count,
        },
    }
