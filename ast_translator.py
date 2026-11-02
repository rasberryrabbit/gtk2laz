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
        # char* -> PChar
        if inner in ("Char", "PChar"):
            return "PChar"
        if inner == "Byte":
            return "PByte"
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
    if fields:
        body = f"  {pas_name} = record\n" + "\n".join(fields) + "\n  end;"
    else:
        body = f"  {pas_name} = record\n    (* empty struct *)\n  end;"
    return AstTypeDef(name=name, kind="struct", pascal_decl=body)


def _enum_typedef_to_pas(name: str, enum_node) -> AstTypeDef:
    pas_name = _pascalize_type(name)
    members: list[str] = []
    if enum_node.values:
        for v in enum_node.values.enumerators:
            mname = pascalify_name(v.name)
            members.append(mname)
    if members:
        body = f"  {pas_name} = ({', '.join(members)});"
    else:
        body = f"  (* empty enum {pas_name} *)"
    return AstTypeDef(name=name, kind="enum", pascal_decl=body)


def _union_typedef_to_pas(name: str, union_node) -> AstTypeDef:
    pas_name = _pascalize_type("union_" + name)
    # Unions become variant records in Pascal - too complex for stage 1.
    return AstTypeDef(name=name, kind="union",
                      pascal_decl=f"  (* TODO gtk2laz: union {pas_name} - "
                                  "convert to variant record by hand *)")


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
                 included_units: list[str] = None):
        self.c_path = c_path
        self.unit_name = unit_name
        self.fake_include_dir = fake_include_dir
        self.form_unit = form_unit
        self.included_units = list(included_units or [])
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
            return False
        text = None
        # ---- Try pcpp (pure Python) first ----
        try:
            from pcpp_preprocessor import preprocess, is_available
            if is_available():
                text = preprocess(self.c_path,
                                  fake_include_dir=self.fake_include_dir)
        except Exception:
            text = None
        # ---- Fall back to gcc -E if pcpp unavailable or failed ----
        if not text or not text.strip():
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
                if proc.returncode != 0:
                    # Preprocessor failed - check if we still got partial output
                    if not text.strip():
                        self.parse_error = proc.stderr.decode("utf-8",
                                                               errors="replace")
                        return False
            except Exception as e:
                self.parse_error = f"both pcpp and gcc failed: {e}"
                return False
        # ---- Parse the preprocessed text with pycparser ----
        try:
            from pycparser import CParser
            parser = CParser()
            ast = parser.parse(text, filename=self.c_path)
        except Exception as e:
            self.parse_error = str(e)
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
                   included_units: list[str] = None) -> Optional[AstToPascal]:
    """Parse ``c_path`` with pycparser and return an AstToPascal object.

    Returns None if pycparser is unavailable or the file cannot be parsed
    (the caller should fall back to the pattern-based translator in that
    case).
    """
    if not _HAS_PYCPARSER:
        return None
    a2p = AstToPascal(c_path, unit_name, fake_include_dir,
                      form_unit, included_units)
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
        return self.lines

    # ------------------------------------------------------------------
    def _pad(self, extra: int = 0) -> str:
        return " " * (self.indent + extra * 2)

    def _emit(self, text: str) -> None:
        self.lines.append(text)
        self._pas_line += 1
        self.translated_count += 1

    def _emit_todo(self, node, reason: str = "") -> None:
        """Emit a multi-line TODO comment for an AST node we can't
        translate. Uses the node's coord for accurate source line."""
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
        """
        pad = self._pad()
        op = node.op
        # Handle chained assignment: a = b = 0
        if op == "=" and isinstance(node.rvalue, c_ast.Assignment):
            flat = self._flatten_assignment(node)
            if flat:
                self._emit(f"{pad}{flat}")
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
        # If the label has a body (stmt), translate it after the label
        if node.stmts:
            self._emit(f"{pad}{label_name}:")
            self.indent += 1
            for stmt in node.stmts:
                self._translate_stmt(stmt)
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

    def _expr_ID(self, node) -> str:
        return pascalify_name(node.name)

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
        # Integer division needs special handling: C '/' for ints is div,
        # but for floats it's '/'. We default to div since most C code
        # here uses integer division.
        if op == "/":
            return f"({left}) div ({right})"
        return f"({left}) {pas_op} ({right})"

    def _expr_UnaryOp(self, node) -> str:
        operand = self._translate_expr(node.expr)
        op = node.op
        if op == "!":
            return f"not ({operand})"
        if op == "~":
            return f"not ({operand})"
        if op == "-":
            return f"-({operand})"
        if op == "+":
            return f"+({operand})"
        if op == "&":
            return f"@({operand})"
        if op == "*":
            return f"{operand}^"
        if op in ("p++", "p--", "++", "--"):
            # As expression, this is semantically wrong (pre/post
            # increment returns a value in C). Emit Inc/Dec call but
            # flag it - the user must verify.
            sub_op = "Inc" if "++" in op else "Dec"
            return f"{sub_op}({operand})"
        if op == "sizeof":
            return f"SizeOf({operand})"
        return f"({op} {operand})"

    def _expr_Assignment(self, node) -> str:
        # Assignment used as expression - emit as ':=' (Pascal allows
        # this in some contexts but not all; flag with comment).
        lhs = self._translate_expr(node.lvalue)
        rhs = self._translate_expr(node.rvalue)
        return f"({lhs} := {rhs})"

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
            return f"{struct}^{field}"
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
        # (defined above, but keep for safety)
        val = node.value
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
    return {
        "lines": lines,
        "locals": t.locals,
        "labels": getattr(t, '_labels', []),
        "todos": t.todos,
        "stats": {
            "translated_count": t.translated_count,
            "todo_count": t.todo_count,
        },
    }
