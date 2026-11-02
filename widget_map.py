"""
widget_map.py
=============
Static mapping tables that drive the GTK -> Lazarus LCL conversion.

Three concerns live here:

1. ``WIDGET_MAP``         : GTK widget class -> LCL class + default parent.
2. ``PROPERTY_MAP``       : GTK Glade property -> LFM property writer.
3. ``SIGNAL_MAP``         : GTK signal -> LCL event name + signature kind.

Keeping these tables in one module makes the rest of the converter short
and lets users tweak the mapping without touching the parser logic.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 1. Widget class mapping
# ---------------------------------------------------------------------------
# Each entry: gtk_class -> (lcl_class, default_container, pas_unit, kind)
#   - default_container : LCL class used when this widget is a container
#                         (e.g. GtkBox -> TPanel). Empty string for leaves.
#   - pas_unit           : FreePascal unit that declares the LCL class.
#   - kind               : 'form' | 'container' | 'leaf' | 'menu' | 'misc'
#                           Used by the LFM emitter to decide indentation.
WIDGET_MAP = {
    # Top level
    "GtkWindow":              ("TForm",          "",                   "Forms",          "form"),

    # Containers
    "GtkBox":                 ("TPanel",         "TPanel",             "ExtCtrls",       "container"),
    "GtkGrid":                ("TPanel",         "TPanel",             "ExtCtrls",       "container"),
    "GtkFrame":               ("TGroupBox",      "TGroupBox",          "StdCtrls",       "container"),
    "GtkAspectFrame":         ("TGroupBox",      "TGroupBox",          "StdCtrls",       "container"),
    "GtkScrolledWindow":      ("TScrollBox",     "TScrollBox",         "Forms",          "container"),
    "GtkNotebook":            ("TPageControl",   "TPageControl",       "ComCtrls",       "container"),
    "GtkToolbar":             ("TToolBar",       "TToolBar",           "ComCtrls",       "container"),
    "GtkMenuBar":             ("TMainMenu",      "",                   "Menus",          "menu"),
    "GtkMenu":                ("TPopupMenu",     "",                   "Menus",          "menu"),
    "GtkMenuItem":            ("TMenuItem",      "",                   "Menus",          "menu"),
    "GtkCheckMenuItem":       ("TMenuItem",      "",                   "Menus",          "menu"),
    "GtkSeparatorMenuItem":   ("TMenuItem",      "",                   "Menus",          "menu"),

    # Buttons / toggles
    "GtkButton":              ("TButton",        "",                   "StdCtrls",       "leaf"),
    "GtkToggleButton":        ("TSpeedButton",   "",                   "Buttons",        "leaf"),
    "GtkCheckButton":         ("TCheckBox",      "",                   "StdCtrls",       "leaf"),
    "GtkRadioButton":         ("TRadioButton",   "",                   "StdCtrls",       "leaf"),
    "GtkLinkButton":          ("TButton",        "",                   "StdCtrls",       "leaf"),
    "GtkSwitch":              ("TCheckBox",      "",                   "StdCtrls",       "leaf"),

    # Toolbar items
    "GtkToolButton":          ("TToolButton",    "",                   "ComCtrls",       "leaf"),
    "GtkToggleToolButton":    ("TToolButton",    "",                   "ComCtrls",       "leaf"),
    "GtkRadioToolButton":     ("TToolButton",    "",                   "ComCtrls",       "leaf"),
    "GtkSeparatorToolItem":   ("TToolButton",    "",                   "ComCtrls",       "leaf"),
    "GtkToolItem":            ("TToolButton",    "",                   "ComCtrls",       "leaf"),

    # Inputs
    "GtkEntry":               ("TEdit",          "",                   "StdCtrls",       "leaf"),
    "GtkSearchEntry":         ("TEdit",          "",                   "StdCtrls",       "leaf"),
    "GtkSpinButton":          ("TSpinEdit",      "",                   "Spin",           "leaf"),
    "GtkComboBox":            ("TComboBox",      "",                   "StdCtrls",       "leaf"),
    "GtkComboBoxText":        ("TComboBox",      "",                   "StdCtrls",       "leaf"),
    "GtkScale":               ("TTrackBar",      "",                   "ComCtrls",       "leaf"),
    "GtkScaleButton":         ("TSpinEdit",      "",                   "Spin",           "leaf"),
    "GtkVolumeButton":        ("TSpinEdit",      "",                   "Spin",           "leaf"),
    "GtkProgressBar":         ("TProgressBar",   "",                   "ComCtrls",       "leaf"),
    "GtkLevelBar":            ("TProgressBar",   "",                   "ComCtrls",       "leaf"),

    # Display
    "GtkLabel":               ("TLabel",         "",                   "StdCtrls",       "leaf"),
    "GtkImage":               ("TImage",         "",                   "ExtCtrls",       "leaf"),
    "GtkTextView":            ("TMemo",          "",                   "StdCtrls",       "leaf"),
    "GtkTreeView":            ("TListView",      "",                   "ComCtrls",       "leaf"),
    "GtkIconView":            ("TListView",      "",                   "ComCtrls",       "leaf"),
    "GtkStatusbar":           ("TStatusBar",     "TStatusBar",         "ComCtrls",       "container"),
    "GtkCalendar":            ("TCalendar",      "",                   "Calendar",       "leaf"),
    "GtkSpinner":             ("TActivityIndicator", "",               "ComCtrls",       "leaf"),

    # Separator / misc
    "GtkSeparator":           ("TBevel",         "",                   "ExtCtrls",       "leaf"),
    "GtkDrawingArea":         ("TPaintBox",      "",                   "ExtCtrls",       "leaf"),
    "GtkEventBox":            ("TPanel",         "TPanel",             "ExtCtrls",       "container"),
    "GtkAlignment":           ("TPanel",         "TPanel",             "ExtCtrls",       "container"),
    "GtkOverlay":             ("TPanel",         "TPanel",             "ExtCtrls",       "container"),
    "GtkRevealer":            ("TPanel",         "TPanel",             "ExtCtrls",       "container"),
    "GtkStack":               ("TPageControl",   "TPageControl",       "ComCtrls",       "container"),
    "GtkStackSwitcher":       ("TRadioGroup",    "",                   "ExtCtrls",       "leaf"),

    # Tree/Selection helpers (not directly represented in LCL; ignored or noted)
    "GtkTreeSelection":       (None,             "",                   "",               "misc"),
    "GtkAdjustment":          (None,             "",                   "",               "misc"),
    "GtkListStore":           (None,             "",                   "",               "misc"),
    "GtkTreeStore":           (None,             "",                   "",               "misc"),
    "GtkCellRendererText":    (None,             "",                   "",               "misc"),
    "GtkCellRendererToggle":  (None,             "",                   "",               "misc"),

    # Dialog windows
    "GtkDialog":              ("TForm",          "",                   "Forms",          "form"),
    "GtkMessageDialog":       ("TForm",          "",                   "Forms",          "form"),
    "GtkAboutDialog":         ("TForm",          "",                   "Forms",          "form"),
    "GtkFileChooserDialog":   ("TOpenDialog",    "",                   "Dialogs",        "leaf"),
    "GtkFontChooserDialog":   ("TFontDialog",    "",                   "Dialogs",        "leaf"),
    "GtkColorChooserDialog":  ("TColorDialog",   "",                   "Dialogs",        "leaf"),

    # Misc non-widget objects that appear in <interface> root
    "GtkSizeGroup":           (None,             "",                   "",               "misc"),
    "GtkTextBuffer":          (None,             "",                   "",               "misc"),
    "GtkCssProvider":         (None,             "",                   "",               "misc"),
    "GtkEntryBuffer":         (None,             "",                   "",               "misc"),
    "GtkEntryCompletion":     (None,             "",                   "",               "misc"),
}


def lookup_widget(gtk_class: str):
    """Return (lcl_class, container_lcl, pas_unit, kind) or None."""
    return WIDGET_MAP.get(gtk_class)


# ---------------------------------------------------------------------------
# 2. Property mapping
# ---------------------------------------------------------------------------
# Glade property name -> LFM property (or skip if value is None).
# Each writer is a function(prop_value) -> (lfm_key, lfm_value_str | None)
# ``None`` means: do not emit.

def _bool(v: str) -> str:
    return "True" if v in ("True", "true", "1", "yes") else "False"

def _int(v: str) -> str:
    try:
        return str(int(v))
    except (TypeError, ValueError):
        return "0"

def _float(v: str) -> str:
    try:
        return str(float(v))
    except (TypeError, ValueError):
        return "0"

def _str(v: str) -> str:
    # Pascal string literal: escape single quotes
    return "'" + v.replace("'", "''") + "'"

PROPERTY_MAP = {
    # Window-level
    "default-width":   lambda v: ("ClientWidth",  _int(v)),
    "default-height":  lambda v: ("ClientHeight", _int(v)),
    "width-request":   lambda v: ("Width",  _int(v)),
    "height-request":  lambda v: ("Height", _int(v)),
    "window-position": lambda v: ("Position", "poScreenCenter" if v in ("center", "center-always") else "poDesigned"),
    "title":           lambda v: ("Caption", _str(v)),
    "resizable":       lambda v: ("BorderIcons", "[biSystemMenu, biMinimize, biMaximize]" if _bool(v) == "True" else "[biSystemMenu]"),
    "icon-name":       lambda v: None,  # applied later via icon resource

    # Common
    "visible":         lambda v: ("Visible", _bool(v)),
    "sensitive":       lambda v: ("Enabled", _bool(v)),
    "can-focus":       lambda v: None,  # no direct LFM equivalent
    "has-focus":       lambda v: None,
    "tooltip-text":    lambda v: ("Hint", _str(v)),
    "tooltip-markup":  lambda v: ("Hint", _str(v)),
    "name":            lambda v: ("Name", _str(v)) if v else None,
    "opacity":         lambda v: None,
    "halign":          lambda v: ("Anchors",
                                  "[akLeft, akRight]" if v == "fill" else
                                  "[akLeft]" if v == "start" else
                                  "[akRight]" if v == "end" else
                                  "[akLeft, akRight]"),
    "valign":          lambda v: ("Anchors",
                                  "[akTop, akBottom]" if v == "fill" else
                                  "[akTop]" if v == "start" else
                                  "[akBottom]" if v == "end" else
                                  "[akTop, akBottom]"),
    "hexpand":         lambda v: ("Anchors", "[akLeft, akRight]" if _bool(v) == "True" else "[akLeft]"),
    "vexpand":         lambda v: ("Anchors", "[akTop, akBottom]" if _bool(v) == "True" else "[akTop]"),
    "margin-start":    lambda v: ("BorderSpacing.Around", _int(v)),
    "margin-end":      lambda v: None,
    "margin-top":      lambda v: ("BorderSpacing.Top", _int(v)),
    "margin-bottom":   lambda v: ("BorderSpacing.Bottom", _int(v)),

    # Box / container
    "orientation":     lambda v: None,  # applied to layout, see glade_parser
    "spacing":         lambda v: ("ChildSizing.VerticalSpacing" if False else "BorderSpacing.Around", _int(v)),
    "homogeneous":     lambda v: None,
    "border-width":    lambda v: ("BorderSpacing.Around", _int(v)),

    # Label
    "label":           lambda v: ("Caption", _str(v)),
    "use-markup":      lambda v: None,  # markup is lost; caption is plain text
    "use-underline":   lambda v: ("ShowAccelChar", _bool(v)),
    "xalign":          lambda v: ("Alignment", _float(v)),
    "yalign":          lambda v: ("Layout", _float(v)),
    "wrap":            lambda v: ("WordWrap", _bool(v)),
    "justify":         lambda v: ("Layout",
                                  "tlCenter" if v == "center" else
                                  "tlRight"  if v == "right" else
                                  "tlLeft"),
    "selectable":      lambda v: None,

    # Button / Toggle
    "use-underline":   lambda v: None,  # also handled in label
    "icon-name":       lambda v: None,  # mapped to ImageList later
    "image":           lambda v: None,
    "label":           lambda v: ("Caption", _str(v)),
    "active":          lambda v: ("Checked", _bool(v)),
    "draw-indicator":  lambda v: None,
    "relief":          lambda v: ("Flat", _bool("True") if v == "none" else _bool("False")),

    # Entry / Spin
    "text":            lambda v: ("Text", _str(v)),
    "max-length":      lambda v: ("MaxLength", _int(v)),
    "visibility":      lambda v: ("EchoMode", "emPassword" if _bool(v) != "True" else "emNormal"),
    "editable":        lambda v: ("ReadOnly", _bool("False") if _bool(v) == "True" else _bool("True")),
    "placeholder-text":lambda v: ("TextHint", _str(v)),
    "xalign":          lambda v: None,
    "caps-lock-warning":lambda v: None,
    "input-purpose":   lambda v: None,
    "numeric":         lambda v: None,
    "wrap":            lambda v: ("WordWrap", _bool(v)),

    # SpinButton / Adjustment
    "adjustment":      lambda v: None,  # applied later via MinValue/MaxValue
    "climb-rate":      lambda v: None,
    "digits":          lambda v: ("Decimal", _int(v)),
    "snap-to-ticks":   lambda v: None,
    "update-policy":   lambda v: None,
    "value":           lambda v: ("Value", _float(v)),
    "lower":           lambda v: ("MinValue", _int(v) if _int(v).lstrip('-').isdigit() else _float(v)),
    "upper":           lambda v: ("MaxValue", _int(v) if _int(v).lstrip('-').isdigit() else _float(v)),
    "step-increment":  lambda v: ("Increment", _int(v)),
    "page-increment":  lambda v: None,

    # ComboBox
    "active":          lambda v: ("ItemIndex", _int(v)),
    "has-entry":       lambda v: ("Style",
                                  "csDropDownList" if _bool(v) != "True" else "csDropDown"),
    "entry-text-column":lambda v: None,
    "id-column":       lambda v: None,

    # Notebook
    "scrollable":      lambda v: None,
    "show-border":     lambda v: None,
    "show-tabs":       lambda v: ("ShowTabs", _bool(v)),
    "tab-pos":         lambda v: ("TabOrder", "0"),
    "page":            lambda v: ("PageIndex", _int(v)),

    # TextView
    "monospace":       lambda v: ("Font.Name", "'Courier New'" if _bool(v) == "True" else None),
    "accepts-tab":     lambda v: ("WantTabs", _bool(v)),
    "cursor-visible":  lambda v: None,

    # ProgressBar
    "fraction":        lambda v: ("Position", str(int(float(v) * 100))),
    "inverted":        lambda v: ("Smooth", _bool(v)),

    # TreeView
    "headers-visible": lambda v: ("ShowColumnHeaders", _bool(v)),
    "enable-search":   lambda v: None,
    "search-column":   lambda v: None,

    # Image
    "resource":        lambda v: ("Picture", _str(v.split('/')[-1])),
    "icon-name":       lambda v: None,
    "pixbuf":          lambda v: ("Picture", _str(v)),

    # Separator
    "orientation":     lambda v: ("Shape", "bsBox") if False else None,  # set via Shape/Style

    # Scale / TrackBar
    "draw-value":      lambda v: None,
    "digits":          lambda v: ("Frequency", _int(v)),

    # StatusBar
    "margin-start":    lambda v: None,
}


def apply_property(gtk_class: str, prop_name: str, prop_value: str):
    """Return (lfm_key, lfm_value_str) or None to skip."""
    fn = PROPERTY_MAP.get(prop_name)
    if fn is None:
        return None
    try:
        out = fn(prop_value)
    except Exception:
        return None
    return out


# ---------------------------------------------------------------------------
# 3. Signal mapping
# ---------------------------------------------------------------------------
# GTK signal -> (LCL event name, pascal_handler_signature_kind)
#   kind: 'click'     -> procedure Handler(Sender: TObject);
#        'change'    -> procedure Handler(Sender: TObject);
#        'toggle'    -> procedure Handler(Sender: TObject);
#        'keypress'  -> procedure Handler(Sender: TObject; var Key: Word; Shift: TShiftState);
#        'mouse'     -> procedure Handler(Sender: TObject; Button: TMouseButton; Shift: TShiftState; X, Y: Integer);
#        'select'    -> procedure Handler(Sender: TObject; User: boolean);
#        'destroy'   -> procedure Handler(Sender: TObject; var CloseAction: TCloseAction);
#        'valuechg'  -> procedure Handler(Sender: TObject);
SIGNAL_MAP = {
    # Buttons
    "clicked":            ("OnClick",       "click"),
    "toggled":            ("OnClick",       "toggle"),    # TSpeedButton.OnClick ~= toggled
    "pressed":            ("OnMouseDown",   "mouse"),
    "released":           ("OnMouseUp",     "mouse"),
    "enter":              ("OnMouseEnter",  "mouse"),
    "leave":              ("OnMouseLeave",  "mouse"),

    # Entries / Text
    "changed":            ("OnChange",      "change"),
    "insert-text":        ("OnChange",      "change"),
    "delete-text":        ("OnChange",      "change"),
    "backspace":          ("OnKeyDown",     "keypress"),
    "activate":           ("OnKeyPress",    "keypress"),

    # Spin / Scale / Combo
    "value-changed":      ("OnChange",      "valuechg"),
    "changed":            ("OnChange",      "change"),

    # Tree / Selection
    "changed":            ("OnSelectionChange", "select"),

    # Notebook
    "switch-page":        ("OnChange",      "change"),

    # Window
    "destroy":            ("OnClose",       "destroy"),
    "delete-event":       ("OnCloseQuery",  "destroy"),
    "key-press-event":    ("OnKeyDown",     "keypress"),
    "key-release-event":  ("OnKeyUp",       "keypress"),
    "button-press-event": ("OnMouseDown",   "mouse"),
    "button-release-event":("OnMouseUp",    "mouse"),
    "configure-event":    ("OnResize",      "click"),
    "realize":            ("OnShow",        "click"),
    "map":                ("OnShow",        "click"),
    "unmap":              ("OnHide",        "click"),

    # Drawing
    "draw":               ("OnPaint",       "click"),
}


def lookup_signal(gtk_signal: str):
    """Return (lcl_event, signature_kind) or None if unsupported."""
    return SIGNAL_MAP.get(gtk_signal)


# ---------------------------------------------------------------------------
# 4. C -> Pascal helpers
# ---------------------------------------------------------------------------
# Type mapping used by the C-to-Pascal translator.

C_TYPE_MAP = {
    "void":           "",
    "int":            "Integer",
    "signed":         "Integer",
    "signed int":     "Integer",
    "unsigned":       "Cardinal",
    "unsigned int":   "Cardinal",
    "long":           "LongInt",
    "long int":       "LongInt",
    "signed long":    "LongInt",
    "signed long int":"LongInt",
    "unsigned long":  "DWORD",
    "unsigned long int":"DWORD",
    "long long":      "Int64",
    "long long int":  "Int64",
    "unsigned long long": "QWord",
    "short":          "SmallInt",
    "short int":      "SmallInt",
    "signed short":   "SmallInt",
    "signed short int":"SmallInt",
    "unsigned short": "Word",
    "unsigned short int":"Word",
    "char":           "Char",
    "unsigned char":  "Byte",
    "signed char":    "ShortInt",
    "float":          "Single",
    "double":         "Double",
    "long double":    "Extended",
    "size_t":         "PtrUInt",
    "ssize_t":        "PtrInt",
    "gboolean":       "Boolean",
    "gint":           "Integer",
    "guint":          "Cardinal",
    "glong":          "LongInt",
    "gulong":         "DWORD",
    "gdouble":        "Double",
    "gchar":          "Char",
    "guchar":         "Byte",
    "gpointer":       "Pointer",
    "gconstpointer":  "Pointer",
    "gsize":          "PtrUInt",
    "gssize":         "PtrInt",
    "guint32":        "Cardinal",
    "guint16":        "Word",
    "guint8":         "Byte",
    "gint32":         "LongInt",
    "gint16":         "SmallInt",
    "gint8":          "ShortInt",
    "gint64":         "Int64",
    "guint64":        "QWord",
    "FILE":           "TextFile",
    "GtkWidget":      "TControl",   # generic fallback
    "GtkWindow":      "TForm",
    "GtkButton":      "TButton",
    "GtkLabel":       "TLabel",
    "GtkEntry":       "TEdit",
    "GtkSpinButton":  "TSpinEdit",
    "GtkComboBox":    "TComboBox",
    "GtkComboBoxText":"TComboBox",
    "GtkTextView":    "TMemo",
    "GtkTextBuffer":  "TMemo",
    "GtkNotebook":    "TPageControl",
    "GtkStatusbar":   "TStatusBar",
    "GtkToggleButton":"TSpeedButton",
    "GtkCheckButton": "TCheckBox",
    "GtkRadioButton": "TRadioButton",
    "GtkTreeView":    "TListView",
    "GtkTreeSelection":"TObject",
    "GtkAdjustment":  "TObject",
    "GtkToolButton":  "TToolButton",
    "GtkToolbar":     "TToolBar",
    "GtkBuilder":     "TObject",
    "GtkCssProvider": "TObject",
    "GObject":        "TObject",
    "GCallback":      "TMethod",
    "GdkPixbuf":      "TPicture",
    "GdkScreen":      "TObject",
    # Lowercase aliases for common typedef-style types
    "byte":           "Byte",
    "word":           "Word",
    "dword":          "DWORD",
    "boolean":        "Boolean",
    "boolean_t":      "Boolean",
}


# Built-in Pascal types that should never get a 'T' prefix when synthesised.
_PASCAL_BUILTINS = {
    "Integer", "Cardinal", "LongInt", "LongWord", "Int64", "QWord",
    "SmallInt", "Word", "ShortInt", "Byte", "Char", "Boolean",
    "Single", "Double", "Extended", "Real", "Pointer", "string",
    "TObject", "TForm", "TButton", "TLabel", "TEdit", "TMemo",
    "TCheckBox", "TRadioButton", "TComboBox", "TSpinEdit", "TToolButton",
    "TToolBar", "TPageControl", "TTabSheet", "TStatusBar", "TListView",
    "TMainMenu", "TPopupMenu", "TMenuItem", "TPanel", "TGroupBox",
    "TScrollBox", "TImage", "TBevel", "TTrackBar", "TProgressBar",
    "TSpeedButton", "TControl",
}


def c_type_to_pascal(c_type: str) -> str:
    """Translate a C type to its Pascal equivalent."""
    if not c_type:
        return ""
    t = c_type.strip()
    # Pointer?
    is_ptr = t.endswith("*") or t.startswith("*") or " *" in t
    t = t.replace("*", "").strip()
    # const modifier
    t = t.replace("const ", "").replace("const", "").strip()
    pas = C_TYPE_MAP.get(t)
    if pas is None:
        # Windows-style typedefs that are already Pascal-compatible.
        if t in ("BYTE", "WORD", "DWORD", "BOOL", "LONG", "ULONG",
                 "INT", "UINT", "LPSTR", "LPWSTR", "HANDLE", "HMODULE",
                 "HINSTANCE", "HDC", "HWND"):
            pas = {"BYTE": "Byte", "WORD": "Word", "DWORD": "DWORD",
                   "BOOL": "Boolean", "LONG": "LongInt", "ULONG": "DWORD",
                   "INT": "Integer", "UINT": "Cardinal", "LPSTR": "PChar",
                   "LPWSTR": "PWideChar", "HANDLE": "THandle",
                   "HMODULE": "THandle", "HINSTANCE": "THandle",
                   "HDC": "THandle", "HWND": "HWND"}[t]
        elif t in _PASCAL_BUILTINS:
            pas = t
        else:
            # custom struct - Pascalize name. Avoid double 'T' prefix.
            if t.startswith("T") and len(t) > 1 and t[1].isupper():
                pas = t
            else:
                pas = "T" + t
    if is_ptr:
        # Use Pascal pointer-type conventions (P-prefix) instead of ``^T``
        # for built-in types. Pascal/FPC defines these pointer types:
        #   PInteger, PCardinal, PLongInt, PWord, PByte, PChar, etc.
        # For custom types, use ``^T<Type>`` (pointer to record).
        _PTR_TYPE_MAP = {
            "Char":          "PChar",
            "Byte":          "PByte",
            "Integer":       "PInteger",
            "Cardinal":      "PCardinal",
            "LongInt":       "PLongInt",
            "Word":          "PWord",
            "SmallInt":      "PSmallInt",
            "ShortInt":      "PShortInt",
            "Int64":         "PInt64",
            "QWord":         "PQWord",
            "Double":        "PDouble",
            "Single":        "PSingle",
            "Extended":      "PExtended",
            "Boolean":       "PBoolean",
            "DWORD":         "PDWORD",
            "Pointer":       "Pointer",
            "TextFile":      "^TextFile",
        }
        if pas in _PTR_TYPE_MAP:
            return _PTR_TYPE_MAP[pas]
        return "^" + pas
    return pas


# ---------------------------------------------------------------------------
# Pascal reserved word handling
# ---------------------------------------------------------------------------
# FreePascal / Object Pascal reserved words and directives that cannot be
# used as identifiers. When a C variable/function/field name collides
# with one of these, we append an underscore so the resulting Pascal
# compiles.
#
# Source: FreePascal reference manual, "Reserved words" section, plus
# common directives that are context-sensitive but still problematic as
# plain identifiers.

PASCAL_RESERVED_WORDS = {
    # Strict reserved words (always reserved)
    "absolute", "abstract", "and", "array", "as", "asm", "assembler",
    "begin", "case", "cdecl", "class", "const", "constructor",
    "destructor", "dispinterface", "dispid", "div", "do", "downto",
    "dynamic", "else", "end", "except", "export", "exports", "far",
    "file", "final", "finalization", "finally", "for", "forward",
    "function", "goto", "if", "implementation", "implements", "in",
    "index", "inherited", "initialization", "inline", "interface",
    "is", "label", "library", "message", "mod", "name", "near",
    "nil", "nodefault", "not", "object", "of", "oldfpccall", "on",
    "or", "out", "override", "package", "packed", "pascal", "private",
    "procedure", "program", "property", "protected", "public",
    "published", "raise", "read", "readonly", "record", "register",
    "reintroduce", "repeat", "resourcestring", "safecall", "sealed",
    "set", "shl", "shr", "static", "stdcall", "stored", "string",
    "then", "threadvar", "to", "try", "type", "unit", "until",
    "uses", "var", "virtual", "while", "with", "write", "writeonly",
    "xor",
    # Boolean literals (contextually reserved)
    "true", "false",
    # Self is special inside methods
    "self",
    # Result is a built-in in functions
    "result",
    # Common FreePascal modes add these
    "default", "operator", "reference", "specialize", "generic",
    "exit", "break", "continue", "halt", "runerror",
    # Turbo Pascal compatibility
    "inherited", "object",
}

# Reverse mapping so we can recover the original C name if needed.
# (Used for diagnostics; the Pascal identifier is the source of truth.)
_PASCAL_TO_C_NAME: dict[str, str] = {}


def pascalify_name(name: str) -> str:
    """Convert a C identifier to a Pascal-safe identifier.

    If ``name`` is a Pascal reserved word (case-insensitive), append an
    underscore so it compiles. For example ``string`` -> ``string_``,
    ``file`` -> ``file_``, ``name`` -> ``name_``.

    Non-identifier characters are also replaced with underscores, and
    a leading digit is prefixed with ``_``.
    """
    if not name:
        return name
    # Replace non-identifier characters with underscores
    import re
    safe = re.sub(r"[^\w]", "_", name)
    # Prefix leading digit
    if safe and safe[0].isdigit():
        safe = "_" + safe
    # Check reserved word (case-insensitive)
    if safe.lower() in PASCAL_RESERVED_WORDS:
        # Record the mapping for diagnostics
        original = safe
        safe = safe + "_"
        _PASCAL_TO_C_NAME[safe] = original
    return safe


def is_pascal_reserved(name: str) -> bool:
    """Return True if ``name`` (case-insensitive) is a Pascal reserved
    word or directive that cannot be used as an identifier."""
    return name.lower() in PASCAL_RESERVED_WORDS


# Backwards-compatible alias used in glade_parser
def pascalize_identifier(name: str) -> str:
    """Alias for :func:`pascalify_name`."""
    return pascalify_name(name)


# C standard library functions that map to Pascal idioms
C_LIB_MAP = {
    "printf":   ("WriteLn",  "varargs"),
    "fprintf":  ("WriteLn",  "varargs"),
    "sprintf":  ("Format",   "fmt"),
    "snprintf": ("Format",   "fmt"),
    "strcpy":   ("{dst} := {src}",        "assign"),
    "strncpy":  ("{dst} := Copy({src}, 1, {n})", "assign"),
    "strcat":   ("{dst} := {dst} + {src}", "assign"),
    "strcmp":   ("AnsiCompareStr({a}, {b})", "expr"),
    "strncmp":  ("AnsiCompareStr(Copy({a},1,{n}), Copy({b},1,{n}))", "expr"),
    "strcasecmp": ("AnsiCompareText({a}, {b})", "expr"),
    "strlen":   ("Length({s})",           "expr"),
    "strchr":   ("Pos({ch}, {s})",         "expr"),
    "strstr":   ("Pos({needle}, {s})",     "expr"),
    "strtol":   ("StrToInt({s})",          "expr"),
    "atoi":     ("StrToInt({s})",          "expr"),
    "atof":     ("StrToFloat({s})",        "expr"),
    "malloc":   ("GetMem({p}, {n})",       "stmt"),
    "calloc":   ("GetMem({p}, {n})",       "stmt"),
    "free":     ("FreeMem({p})",           "stmt"),
    "memcpy":   ("Move({src}, {dst}, {n})", "stmt"),
    "memcmp":   ("CompareMem({a}, {b}, {n})", "expr"),
    "memset":   ("FillChar({dst}, {n}, {val})", "stmt"),
    "exit":     ("Halt",                   "stmt"),
    "fopen":    ("AssignFile({f}, {name}); Reset({f})", "stmt"),
    "fclose":   ("CloseFile({f})",         "stmt"),
    "fread":    ("BlockRead({f}, {buf}, {n})", "stmt"),
    "fwrite":   ("BlockWrite({f}, {buf}, {n})", "stmt"),
    "fseek":    ("Seek({f}, {pos})",       "stmt"),
    "ftell":    ("FilePos({f})",           "expr"),
    "feof":     ("EOF({f})",               "expr"),
    "tolower":  ("ToLower({c})",           "expr"),
    "toupper":  ("ToUpper({c})",           "expr"),
    "isdigit":  ("IsDigit({c})",           "expr"),
    "isalpha":  ("IsAlpha({c})",           "expr"),
    "isspace":  ("IsSpace({c})",           "expr"),
    "getopt_long": ("{cmdline handled in main}", "stmt"),
}


# GTK function call patterns -> Pascal LCL equivalent
# Each entry: gtk_function_name -> (pascal_template, kind)
#   kind: 'expr'  -> inline expression
#        'stmt'  -> statement (must end with ;)
GTK_CALL_MAP = {
    # Toggle accessors
    "gtk_toggle_button_get_active":      ("{w}.Checked",                          "expr"),
    "gtk_toggle_button_set_active":      ("{w}.Checked := {v}",                   "stmt"),
    "gtk_toggle_tool_button_get_active": ("{w}.Down",                             "expr"),
    "gtk_toggle_tool_button_set_active": ("{w}.Down := {v}",                      "stmt"),

    # Spin
    "gtk_spin_button_get_value":         ("{w}.Value",                            "expr"),
    "gtk_spin_button_get_value_as_int":  ("{w}.Value",                            "expr"),
    "gtk_spin_button_set_value":         ("{w}.Value := {v}",                     "stmt"),
    "gtk_spin_button_set_range":         ("{w}.MinValue := {lo}; {w}.MaxValue := {hi}", "stmt"),
    "gtk_spin_button_set_increments":    ("{w}.Increment := {step}",              "stmt"),

    # Range / Scale
    "gtk_range_get_value":               ("{w}.Position",                         "expr"),
    "gtk_range_set_value":               ("{w}.Position := {v}",                  "stmt"),
    "gtk_scale_set_digits":              ("{w}.Frequency := {d}",                 "stmt"),

    # Entry
    "gtk_entry_get_text":                ("{w}.Text",                             "expr"),
    "gtk_entry_set_text":                ("{w}.Text := {t}",                      "stmt"),
    "gtk_editable_get_editable":         ("not {w}.ReadOnly",                     "expr"),
    "gtk_editable_set_editable":         ("{w}.ReadOnly := not ({e})",            "stmt"),
    "gtk_entry_set_max_length":          ("{w}.MaxLength := {n}",                 "stmt"),
    "gtk_entry_set_visibility":          ("{w}.EchoMode := IfThen({v}, emNormal, emPassword)", "stmt"),

    # Label
    "gtk_label_get_text":                ("{w}.Caption",                          "expr"),
    "gtk_label_set_text":                ("{w}.Caption := {t}",                   "stmt"),
    "gtk_label_set_markup":              ("{w}.Caption := {t}",                   "stmt"),
    "gtk_label_set_label":               ("{w}.Caption := {t}",                   "stmt"),

    # Button
    "gtk_button_get_label":              ("{w}.Caption",                          "expr"),
    "gtk_button_set_label":              ("{w}.Caption := {t}",                   "stmt"),
    "gtk_button_set_image":              ("{w}.Glyph.LoadFromFile({p})",          "stmt"),
    "gtk_widget_set_tooltip_text":       ("{w}.Hint := {t}; {w}.ShowHint := True","stmt"),
    "gtk_widget_set_tooltip_markup":     ("{w}.Hint := {t}; {w}.ShowHint := True","stmt"),

    # Widget visibility / sensitivity
    "gtk_widget_show":                   ("{w}.Visible := True",                  "stmt"),
    "gtk_widget_show_now":               ("{w}.Visible := True",                  "stmt"),
    "gtk_widget_show_all":               ("{w}.Visible := True",                  "stmt"),
    "gtk_widget_hide":                   ("{w}.Visible := False",                 "stmt"),
    "gtk_widget_set_sensitive":          ("{w}.Enabled := {b}",                   "stmt"),
    "gtk_widget_set_visible":            ("{w}.Visible := {b}",                   "stmt"),
    "gtk_widget_grab_focus":             ("{w}.SetFocus",                         "stmt"),
    "gtk_widget_destroy":                ("{w}.Free",                             "stmt"),
    "gtk_widget_queue_draw":             ("{w}.Invalidate",                       "stmt"),

    # Window
    "gtk_window_set_title":              ("{w}.Caption := {t}",                   "stmt"),
    "gtk_window_set_default_size":       ("{w}.SetClientSize({w}, {h})",          "stmt"),
    "gtk_window_set_modal":              ("{w}.FormStyle := fsStayOnTop",         "stmt"),
    "gtk_window_close":                  ("{w}.Close",                            "stmt"),
    "gtk_window_present":                ("{w}.Show",                             "stmt"),
    "gtk_window_resize":                 ("{w}.SetBounds({x}, {y}, {w_}, {h_})",  "stmt"),

    # ComboBox
    "gtk_combo_box_get_active":          ("{w}.ItemIndex",                        "expr"),
    "gtk_combo_box_set_active":          ("{w}.ItemIndex := {i}",                 "stmt"),
    "gtk_combo_box_text_get_active_text":("{w}.Text",                             "expr"),
    "gtk_combo_box_text_append":         ("{w}.Items.Add({t})",                   "stmt"),
    "gtk_combo_box_text_remove":         ("{w}.Items.Delete({i})",                "stmt"),
    "gtk_combo_box_text_remove_all":     ("{w}.Items.Clear",                      "stmt"),

    # Notebook
    "gtk_notebook_get_current_page":     ("{w}.PageIndex",                        "expr"),
    "gtk_notebook_set_current_page":     ("{w}.PageIndex := {i}",                 "stmt"),
    "gtk_notebook_append_page":          ("{w}.AddPage({p}, {t})",                "stmt"),
    "gtk_notebook_remove_page":          ("{w}.Pages[{i}].Free",                  "stmt"),

    # TextView / Buffer
    "gtk_text_view_get_buffer":          ("{w}",                                  "expr"),
    "gtk_text_buffer_set_text":          ("{w}.Lines.Text := {t}",                "stmt"),
    "gtk_text_buffer_get_text":          ("{w}.Lines.Text",                       "expr"),
    "gtk_text_buffer_get_start_iter":    ("{w}.SelStart := 0",                    "stmt"),
    "gtk_text_buffer_get_end_iter":      ("{w}.SelEnd := Length({w}.Text)",       "stmt"),
    "gtk_text_buffer_insert":            ("{w}.SelText := {t}",                   "stmt"),
    "gtk_text_buffer_delete":            ("{w}.Clear",                            "stmt"),
    "gtk_text_buffer_get_line_count":    ("{w}.Lines.Count",                      "expr"),
    "gtk_text_view_scroll_to_iter":      ("{w}.SelStart := Length({w}.Text)",     "stmt"),

    # Statusbar
    "gtk_statusbar_push":                ("{w}.SimpleText := {t}",                "stmt"),
    "gtk_statusbar_pop":                 ("{w}.SimpleText := ''",                 "stmt"),
    "gtk_statusbar_remove_all":          ("{w}.SimpleText := ''",                 "stmt"),

    # ProgressBar
    "gtk_progress_bar_set_fraction":     ("{w}.Position := Round({f} * 100)",     "stmt"),
    "gtk_progress_bar_set_text":         ("{w}.Hint := {t}",                      "stmt"),
    "gtk_progress_bar_pulse":            ("{w}.StepIt",                           "stmt"),

    # TreeView (rough mapping; ListView model differs significantly)
    "gtk_tree_view_get_selection":       ("{w}.Selected",                         "expr"),
    "gtk_tree_selection_get_selected":   ("({w}.Selected <> nil)",                "expr"),
    "gtk_tree_selection_select_iter":    ("if {i} < {w}.Items.Count then {w}.Items[{i}].Selected := True", "stmt"),
    "gtk_tree_selection_unselect_all":   ("{w}.ClearSelection",                   "stmt"),
    "gtk_tree_selection_set_mode":       ("{w}.MultiSelect := ({m} = 2)",         "stmt"),

    # Builder (no-op in Pascal; components are form fields)
    "gtk_builder_new":                   ("nil",                                  "expr"),
    "gtk_builder_new_from_file":         ("nil",                                  "expr"),
    "gtk_builder_new_from_resource":     ("nil",                                  "expr"),
    "gtk_builder_get_object":            ("{name}",                               "expr"),
    "gtk_builder_connect_signals":       ("",                                     "stmt"),

    # Misc GTK helpers
    "g_object_unref":                    ("",                                     "stmt"),
    "g_object_get_data":                 ("nil",                                  "expr"),
    "g_object_set_data":                 ("",                                     "stmt"),
    "g_signal_connect":                  ("",                                     "stmt"),
    "g_signal_connect_swapped":          ("",                                     "stmt"),
    "g_signal_handlers_disconnect_by_func":("",                                  "stmt"),
    "g_timeout_add":                     ("Application.ProcessMessages",          "stmt"),
    "g_idle_add":                        ("Application.ProcessMessages",          "stmt"),
    "gtk_main":                          ("Application.Run",                      "stmt"),
    "gtk_main_quit":                     ("Application.Terminate",                "stmt"),
    "gtk_init":                          ("",                                     "stmt"),
    "gtk_events_pending":                ("Application.ProcessMessages; False",   "expr"),
    "gtk_main_iteration":                ("Application.ProcessMessages",          "stmt"),
    "gtk_main_iteration_do":             ("Application.HandleMessage",            "stmt"),
}


def lookup_gtk_call(fn_name: str):
    """Return (template_str, kind) or None."""
    return GTK_CALL_MAP.get(fn_name)


def lookup_c_lib(fn_name: str):
    """Return (template_str, kind) or None."""
    return C_LIB_MAP.get(fn_name)
