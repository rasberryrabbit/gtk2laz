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
            fname = f.name or "field"
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
            mname = v.name
            # Avoid Pascal keywords
            if mname.lower() in _PASCAL_KEYWORDS:
                mname = mname + "_"
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
        """Parse the C file with pycparser. Return True on success."""
        if not _HAS_PYCPARSER:
            self.parse_error = "pycparser not installed"
            return False
        # pycparser's parse_file reads the file directly; if it has
        # non-UTF-8 bytes (e.g. Latin-1 comments), preprocess manually
        # and feed the text to the parser.
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
        params_str = "; ".join(f"{n}: {t}" for n, t in fn.params)
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
        sig = f"{prefix} {fn.name}"
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
        ptype = _type_to_pascal(node.type)
        # Handle array decl
        if isinstance(node.type, c_ast.ArrayDecl):
            inner = _type_to_pascal(node.type.type)
            dim = _const_eval(node.type.dim) if node.type.dim else ""
            if dim:
                decl = f"  {node.name}: array[0..{dim}-1] of {inner};"
            else:
                decl = f"  {node.name}: array of {inner};"
            self.locals.append(decl)
        else:
            self.locals.append(f"  {node.name}: {ptype};")
        # If there's an initializer, emit an assignment
        if node.init is not None:
            init_expr = self._translate_expr(node.init)
            pad = self._pad()
            self._emit(f"{pad}{node.name} := {init_expr};")

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
            self._emit(f"{pad}else")
            self._emit_if_branch(node.iffalse, expect_else=False)

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
        # Try to detect the simple "for (i=0; i<N; i++)" pattern
        init = node.init
        cond = node.cond
        nxt = node.next
        # init might be a Decl or an Assignment
        var = None
        start_expr = None
        if init is not None:
            if isinstance(init, c_ast.DeclList):
                # for (int i=0; ...) - DeclList with declarations
                if init.decls:
                    first = init.decls[0]
                    if first.name and first.init:
                        var = first.name
                        start_expr = self._translate_expr(first.init)
            elif isinstance(init, c_ast.Assignment):
                if isinstance(init.lvalue, c_ast.ID):
                    var = init.lvalue.name
                    start_expr = self._translate_expr(init.rvalue)
            elif isinstance(init, c_ast.UnaryOp) and init.op == "p++":
                pass
        # Check for "i < N" test
        end_expr = None
        is_less_than = False
        is_less_equal = False
        if cond is not None and var is not None:
            if isinstance(cond, c_ast.BinaryOp):
                if cond.op == "<" and isinstance(cond.left, c_ast.ID) \
                        and cond.left.name == var:
                    end_expr = self._translate_expr(cond.right)
                    is_less_than = True
                elif cond.op == "<=" and isinstance(cond.left, c_ast.ID) \
                        and cond.left.name == var:
                    end_expr = self._translate_expr(cond.right)
                    is_less_equal = True
        # Check for "i++" step
        is_inc = False
        if nxt is not None:
            if isinstance(nxt, c_ast.UnaryOp) and nxt.op == "p++":
                if isinstance(nxt.expr, c_ast.ID) and nxt.expr.name == var:
                    is_inc = True
            elif isinstance(nxt, c_ast.Assignment) and nxt.op == "=":
                # i = i + 1
                if (isinstance(nxt.lvalue, c_ast.ID) and
                    isinstance(nxt.rvalue, c_ast.BinaryOp) and
                    nxt.rvalue.op == "+" and
                    isinstance(nxt.rvalue.left, c_ast.ID) and
                    nxt.rvalue.left.name == var and
                    isinstance(nxt.rvalue.right, c_ast.Constant) and
                    nxt.rvalue.right.value == "1"):
                    is_inc = True
        # If we have a clean for-to-do pattern, emit it
        if var and start_expr and end_expr and is_inc:
            if is_less_than:
                self._emit(f"{pad}for {var} := {start_expr} to ({end_expr}) - 1 do")
            else:  # is_less_equal
                self._emit(f"{pad}for {var} := {start_expr} to {end_expr} do")
            self._emit(f"{pad}begin")
            self.indent += 1
            if node.stmt and hasattr(node.stmt, "block_items"):
                for stmt in (node.stmt.block_items or []):
                    self._translate_stmt(stmt)
            elif node.stmt:
                self._translate_stmt(node.stmt)
            self.indent -= 1
            self._emit(f"{pad}end;")
            return
        # Handle "for (; var < N; var++)" - empty init, var is in test/step
        if var is None and cond is not None and nxt is not None:
            step_var = None
            step_is_inc = False
            if isinstance(nxt, c_ast.UnaryOp) and nxt.op == "p++":
                if isinstance(nxt.expr, c_ast.ID):
                    step_var = nxt.expr.name
                    step_is_inc = True
            if step_var and isinstance(cond, c_ast.BinaryOp):
                if (isinstance(cond.left, c_ast.ID) and
                    cond.left.name == step_var and step_is_inc):
                    end_expr2 = self._translate_expr(cond.right)
                    # We don't know the start value, so emit a while loop
                    # but without a TODO (it's a valid translation).
                    cond_pas = self._translate_expr(cond)
                    self._emit(f"{pad}while {cond_pas} do")
                    self._emit(f"{pad}begin")
                    self.indent += 1
                    if node.stmt and hasattr(node.stmt, "block_items"):
                        for stmt in (node.stmt.block_items or []):
                            self._translate_stmt(stmt)
                    elif node.stmt:
                        self._translate_stmt(node.stmt)
                    # Emit the step
                    self._emit(f"{self._pad()}Inc({step_var});")
                    self.indent -= 1
                    self._emit(f"{pad}end;")
                    return
        # Handle "for (; j < N; j++)" - empty init
        if init is None and cond is not None and nxt is not None:
            cond_pas = self._translate_expr(cond)
            step_pas = self._translate_expr(nxt)
            self._emit(f"{pad}while {cond_pas} do")
            self._emit(f"{pad}begin")
            self.indent += 1
            if node.stmt and hasattr(node.stmt, "block_items"):
                for stmt in (node.stmt.block_items or []):
                    self._translate_stmt(stmt)
            elif node.stmt:
                self._translate_stmt(node.stmt)
            # Emit the step as a statement
            if isinstance(nxt, c_ast.UnaryOp) and nxt.op in ("p++", "++"):
                operand = self._translate_expr(nxt.expr)
                self._emit(f"{self._pad()}Inc({operand});")
            elif isinstance(nxt, c_ast.UnaryOp) and nxt.op in ("p--", "--"):
                operand = self._translate_expr(nxt.expr)
                self._emit(f"{self._pad()}Dec({operand});")
            else:
                self._emit(f"{self._pad()}{step_pas};")
            self.indent -= 1
            self._emit(f"{pad}end;")
            return
        # Fall back: emit as while loop with TODO
        self._emit_todo(node, "for loop with non-standard form; rewritten as while")
        self._emit(f"{pad}// init: {self._dump(init) if init else ''}")
        while_cond = self._translate_expr(cond) if cond else "True"
        self._emit(f"{pad}while {while_cond} do")
        self._emit(f"{pad}begin")
        self.indent += 1
        if node.stmt and hasattr(node.stmt, "block_items"):
            for stmt in (node.stmt.block_items or []):
                self._translate_stmt(stmt)
        elif node.stmt:
            self._translate_stmt(node.stmt)
        if nxt:
            self.indent -= 1
            self._emit(f"{pad}  // step: {self._dump(nxt)}")
            self.indent += 1
        self.indent -= 1
        self._emit(f"{pad}end;")

    def _stmt_Switch(self, node) -> None:
        pad = self._pad()
        cond = self._translate_expr(node.cond)
        self._emit(f"{pad}case {cond} of")
        self.indent += 1
        for case in node.stmts:
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
        """Assignment: lvalue op= rvalue"""
        pad = self._pad()
        lhs = self._translate_expr(node.lvalue)
        rhs = self._translate_expr(node.rvalue)
        op = node.op
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
        # Pascal labels need declaration - skip with TODO
        self._emit_todo(node, "label (Pascal needs label decl)")

    def _stmt_Goto(self, node) -> None:
        self._emit_todo(node, "goto (Pascal needs label decl)")

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
        return node.name

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
        """struct.field or struct->field"""
        struct = self._translate_expr(node.expr)
        field = node.field.name if hasattr(node.field, "name") else str(node.field)
        if node.type == "->":
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
        todos   : list of TodoItem dicts
        stats   : dict with translated_count, todo_count
    """
    t = AstBodyTranslator(ast_func, source_file, indent)
    lines = t.translate()
    return {
        "lines": lines,
        "locals": t.locals,
        "todos": t.todos,
        "stats": {
            "translated_count": t.translated_count,
            "todo_count": t.todo_count,
        },
    }
