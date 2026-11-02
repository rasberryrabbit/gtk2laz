# gtk2laz

**GTK (C + Glade + local headers) → FreePascal / Lazarus LCL converter.**

`gtk2laz` is a command-line tool **and interactive GUI** that takes a GTK
source tree (C source, Glade `.glade` XML, and local `.h` headers) and
produces a Lazarus project that uses the LCL widget set. It is intended
to do the bulk of a porting job automatically so that a human can finish
the remaining 5–10 % by hand.

The converter is conservative: anything it cannot confidently translate
is emitted as a Pascal comment of the form

```pascal
(* TODO gtk2laz: <reason>: <original C snippet> *)
```

so the project still loads in Lazarus and the manual work is fully
discoverable through the GUI's **TODO list** view (which links each
marker back to the original C source line) or a simple
`grep "TODO gtk2laz"`.

---

## Quick start

The release ships three runnable forms:

| Platform | Path                                   | Requirement                  |
| -------- | -------------------------------------- | ---------------------------- |
| Linux    | `linux-x86_64/gtk2laz`                 | None (self-contained)        |
| Windows  | `windows\gtk2laz_win.bat`              | Python 3.8+ on PATH          |
| Any      | `python3 gtk2laz.py` (or `gtk2laz.pyz`) | Python 3.8+ (tkinter for GUI) |

```bash
# Linux (pre-built binary, no Python needed):
./linux-x86_64/gtk2laz gui
./linux-x86_64/gtk2laz c2pas /path/to/src /path/to/out
./linux-x86_64/gtk2laz convert /path/to/src /path/to/out
./linux-x86_64/gtk2laz list-todos /path/to/out

# Windows (Python 3.8+ required - get it from python.org):
windows\gtk2laz_win.bat gui
windows\gtk2laz_win.bat c2pas C:\path\to\src C:\path\to\out

# Build a Windows single-file .exe on a Windows machine:
build_win_exe.bat        # produces dist\gtk2laz.exe

# Or run from source on any platform with Python 3.8+:
python3 gtk2laz.py gui
python3 gtk2laz.pyz gui
```

The output directory will contain:

```
myproject/
├── myproject.lpr              ← main program (skipped in c2pas mode)
├── myproject.lpi              ← Lazarus project info (skipped in c2pas mode)
├── mainform.pas               ← form unit (Glade → LFM)
├── mainform.lfm               ← form resource
├── <header>.pas               ← one per local .h file (NEW)
├── <cfile>.pas                ← one per .c file (uses local headers)
├── todos.json                 ← machine-readable TODO list
├── resources/                 ← .png / .css copied from the source tree
└── CONVERSION_REPORT.md       ← per-file stats + manual work checklist
```

Open `myproject.lpi` in the Lazarus IDE, or build headlessly with:

```bash
lazbuild myproject.lpi
```

---

## Header file handling (NEW)

The converter now scans every `.h` file in the source tree and converts
each one to its own `.pas` unit, mirroring the C `#include` graph:

| C header construct                     | Pascal output                          |
| -------------------------------------- | -------------------------------------- |
| `#define X 5`                          | `const X = 5;`                         |
| `#define X 0x10`                       | `const X = $10;`                       |
| `typedef struct { int a; char b; } T;` | `type T = record a: Integer; b: Char; end;` |
| `typedef enum { A, B, C } E;`          | `type E = (A, B, C);`                  |
| `typedef int WORD;`                    | `type WORD = Integer;`                 |
| `extern int x;`                        | `var x: Integer;`                      |
| `int foo(int a);`                      | `function foo(a: Integer): Integer;`   |

When a C file `foo.c` does `#include "bar.h"`, the generated `foo.pas`
automatically adds `bar` to its `uses` clause — so symbols declared in
the header are visible. Likewise, headers that include other headers
(`common.h` includes `strings.h` and `instructions.h`) get the
corresponding units in their `uses` clause.

Headers are converted in topological order (a header is processed only
after every header it depends on), so the dependency graph is always
satisfied.

---

## Subcommands

| Command                          | Purpose                                                   |
| -------------------------------- | --------------------------------------------------------- |
| `gtk2laz gui`                    | Launch the interactive Tkinter GUI front-end.             |
| `gtk2laz convert INPUT OUTPUT`   | Full conversion: Glade form + all `.c` + all `.h` files.  |
| `gtk2laz c2pas INPUT OUTPUT`     | **Standalone C → Pascal translation** (skip Glade form).  |
| `gtk2laz list-todos OUTPUT`      | Print every TODO marker in a converted project as JSON.   |

For backward compatibility, `gtk2laz INPUT OUTPUT` (no subcommand) is
treated as `gtk2laz convert INPUT OUTPUT`.

### Common options for `convert`

| Option           | Default     | Description                                |
| ---------------- | ----------- | ------------------------------------------ |
| `--form-unit`    | `mainform`  | Pascal unit name for the main form.        |
| `--form-class`   | `TMainForm` | Pascal class name for the main form.       |
| `--glade`        | (auto)      | Specific `.glade` file to use.             |
| `--report`       | (auto)      | Path to write the conversion report.       |
| `--c-only`       | off         | Skip the Glade form (alias for `c2pas`).   |

### Options for `c2pas`

| Option        | Default | Description                                       |
| ------------- | ------- | ------------------------------------------------- |
| `--form-unit` | (none)  | Optional form unit to add to each unit's `uses`.  |
| `--report`    | (auto)  | Path to write the conversion report.              |

---

## The GUI front-end

`gtk2laz gui` opens a 1200×780 window with three regions:

1. **Conversion panel (top)** — input/output directory pickers, a
   mode selector (Full / C-only / Reload TODOs), and a "Run conversion"
   button. Conversion runs in a background thread so the UI stays
   responsive.
2. **TODO list (left)** — a tree view showing every `(* TODO gtk2laz … *)`
   marker produced by the conversion (including those from header
   files). Columns: `File:Line | Function | Reason | C snippet`.
   A free-text filter narrows the list live. Double-click (or Enter)
   opens the source file in your `$EDITOR`.
3. **Source viewer (right)** — when a TODO is selected, this panel
   shows the original C file with line numbers and the offending line
   highlighted in yellow. The header summarises
   `source_file:line | pascal_file:line | function | reason`.

This lets you walk through every spot the converter could not handle
and jump straight to the original C source for context.

---

## What gets converted automatically

### Glade XML → LFM (form resource)

| GTK widget                | LCL component        |
| ------------------------- | -------------------- |
| `GtkWindow` / `GtkDialog` | `TForm`              |
| `GtkBox` (vert/horiz)     | `TPanel`             |
| `GtkFrame`                | `TGroupBox`          |
| `GtkNotebook`             | `TPageControl` (children become `TTabSheet`s; tab labels extracted from `<child type="tab">`) |
| `GtkToolbar`              | `TToolBar`           |
| `GtkToolButton`           | `TToolButton`        |
| `GtkSeparatorToolItem`    | `TToolButton` (`Style = tbsSeparator`) |
| `GtkButton`               | `TButton`            |
| `GtkToggleButton`         | `TSpeedButton` (`AllowAllUp`, `GroupIndex = 0`) |
| `GtkCheckButton`          | `TCheckBox`          |
| `GtkRadioButton`          | `TRadioButton`       |
| `GtkLabel`                | `TLabel`             |
| `GtkEntry`                | `TEdit`              |
| `GtkSpinButton` + `GtkAdjustment` | `TSpinEdit` (lower/upper/value inlined) |
| `GtkComboBoxText`         | `TComboBox`          |
| `GtkTextView` (inside `GtkScrolledWindow`) | `TMemo` (inside `TScrollBox`) |
| `GtkTreeView`             | `TListView` (`vsReport`) |
| `GtkStatusbar`            | `TStatusBar`         |
| `GtkScale`                | `TTrackBar`          |
| `GtkImage`                | `TImage`             |
| `GtkSeparator`            | `TBevel`             |
| `GtkMenuBar` / `GtkMenu` / `GtkMenuItem` | `TMainMenu` / `TPopupMenu` / `TMenuItem` |

Layout heuristics:

* A `GtkBox` becomes a `TPanel` whose children get `Align = alTop`/`alLeft`/`alClient`
  depending on the box orientation and the `expand` packing flag.
* `GtkNotebook` children become `TTabSheet` objects inside the `TPageControl`.
* `GtkScrolledWindow` becomes `TScrollBox`; the inner widget gets `Align = alClient`.
* `GtkStatusbar` gets `Align = alBottom`; `GtkToolbar`/`GtkMenuBar` get `Align = alTop`.

### Signals → LCL events

| GTK signal              | LCL event              |
| ----------------------- | ---------------------- |
| `clicked`               | `OnClick`              |
| `toggled`               | `OnClick` (speed buttons) |
| `changed`               | `OnChange`             |
| `value-changed`         | `OnChange`             |
| `destroy`               | `OnClose`              |
| `delete-event`          | `OnCloseQuery`         |
| `key-press-event`       | `OnKeyDown`            |
| `button-press-event`    | `OnMouseDown`          |
| `switch-page`           | `OnChange`             |

Handler names are preserved verbatim. The form's `.pas` unit declares each
handler as an empty procedure stub (`// TODO: port original C handler ...`),
so the LFM-to-Pascal linkage works out of the box.

### C source → Pascal unit (per `.c` file)

The translator walks each C file and emits a `.pas` unit containing:

* `#define` constants → Pascal `const` block (with `0x..` → `$..` translation).
* `typedef`s and `extern` globals → Pascal `type`/`var` blocks. Common Windows
  typedefs (`DWORD`, `WORD`, `BYTE`, `HANDLE`, ...) are mapped to LCL types.
* Function definitions → Pascal `procedure`/`function` with translated
  signatures (`int → Integer`, `char* → PChar`, `BYTE* → PByte`, ...).
* Function bodies are translated statement-by-statement:
  * GTK calls (`gtk_button_set_label`, `gtk_toggle_button_get_active`,
    `gtk_statusbar_push`, ...) → LCL equivalents (`Caption :=`,
    `Checked`, `SimpleText :=`, ...). See `widget_map.GTK_CALL_MAP` for the
    full table.
  * libc helpers (`sprintf` → `Format`, `strcmp` → `AnsiCompareStr`,
    `strlen` → `Length`, `malloc` → `GetMem`, ...). See `widget_map.C_LIB_MAP`.
  * Control flow: `if/else`, `for` (with `i++` step → `for i := 0 to N-1 do`),
    `while`, `do/while` → `repeat/until not (...)`, `switch` → `case/of`,
    `return` → `Exit` / `Exit(value)`.
  * Operators: `&&`, `||`, `!`, `==`, `!=`, `<<`, `>>` → Pascal equivalents.
  * Post-increment `x++` → `Inc(x)` (semantic approximation; the user must
    verify expressions that depend on the pre-increment value).
* `#include "foo.h"` → `foo` is added to the unit's `uses` clause
  (the corresponding `foo.pas` was generated from `foo.h` in pass 1).

---

## What requires manual work

The converter inserts `(* TODO gtk2laz: ... *)` markers for everything it
cannot confidently translate. Each marker is also recorded in
`todos.json` and surfaced in the GUI's TODO list. Typical items:

* **HID/USB I/O** — `ioctl`, `hidraw`, `ReadFile`/`WriteFile`, `SetupDi*`
  calls. Replace with `TFileStream` on `/dev/hidraw*` (Linux) or a JEDI-HID
  library (Windows).
* **Tree/List models** — GTK's `GtkTreeIter` / `GtkListStore` API has no
  1:1 LCL equivalent. Rewrite using `TListView.Items.Add`.
* **Custom drawing** — `cairo_t` from a `GtkDrawingArea` `draw` signal →
  `TCanvas` methods in `OnPaint`.
* **CSS styling** — GTK themes have no LCL counterpart; restyle with
  `Color`/`Font` properties.
* **Resource paths** — `/res/foo.png` from `glib-compile-resources` →
  load via `TResourceStream` or copy into the executable directory and
  `TImage.Picture.LoadFromFile`.
* **Post-increment in expressions** — `arr[i++] = x;` becomes
  `arr[Inc(i)] := x;` which compiles but does not return the pre-increment
  value. Rewrite as `arr[i] := x; Inc(i);`.
* **Header macros with parameters** — `#define MAX(a,b) ((a)>(b)?(a):(b))`
  is not translated; rewrite as a Pascal `function Max(a, b: Integer): Integer;`.

---

## Module layout

```
gtk2laz/
├── gtk2laz.py         ← CLI entry point + project file generator
├── gtk2laz_gui.py     ← Tkinter GUI front-end (TODO browser + source viewer)
├── glade_parser.py    ← Glade XML → LFM/LPR/PAS form emitter
├── c_parser.py        ← C source + .h header → Pascal unit translator
├── widget_map.py      ← GTK→LCL widget/signal/call mapping tables
├── gtk2laz.pyz        ← Python zipapp (single-file, cross-platform)
├── gtk2laz_win.bat    ← Windows launcher (calls system Python)
├── README.md          ← this file
├── build_exe.sh       ← PyInstaller single-file build script (Linux)
├── build_win_exe.bat  ← PyInstaller single-file build script (Windows)
└── gtk2laz.spec       ← PyInstaller spec
```

The mapping tables in `widget_map.py` are deliberately data-driven so the
converter can be retuned for a different GTK application by editing one
file.

---

## Building the single-file executable

### Linux x86-64 (pre-built binary included)

```bash
bash build_exe.sh
# produces dist/gtk2laz (~16 MB single-file ELF executable)
```

### Windows (build on a Windows machine)

```bat
build_win_exe.bat
:: produces dist\gtk2laz.exe (~16 MB single-file PE executable)
```

Requirements for the Windows build:

* Python 3.8+ from https://www.python.org/downloads/
  (the default installer includes Tcl/Tk, which the GUI needs).
* PyInstaller: `pip install pyinstaller`

### Run without building

The `gtk2laz.pyz` zipapp runs on any Python 3.8+ without installation:

```bash
python3 gtk2laz.pyz gui                # Linux/macOS
python gtk2laz.pyz gui                # Windows (with Python on PATH)
```

---

## Demo: converting the OPGUI project

The repository's demo target is the Open Programmer GUI
(`opgui-0.12.4`, https://sourceforge.net/projects/openprogrammer/), a
~42 000-line GTK3 application that drives a USB programmer for PIC/AVR
microcontrollers. Running

```bash
gtk2laz convert /path/to/opgui-0.12.4 opgui_lazarus
```

produces 31 Pascal units (1 form + 15 headers + 15 C files). Typical
conversion stats:

| Metric                              | Value   |
| ----------------------------------- | ------- |
| GTK widgets converted (LFM)         | ~296    |
| Header #defines converted           | ~130    |
| Header typedefs converted           | ~3      |
| Header extern globals               | ~76     |
| Header function prototypes          | ~127    |
| C functions translated              | ~117    |
| C global variables emitted          | ~81     |
| C statements auto-translated        | ~15 000 |
| Statements marked TODO (C + headers)| ~1 100  |
| Auto-translation rate               | ~93 %   |

Open the GUI (`gtk2laz gui`) and pick the converted output directory
in "Reload TODOs from output" mode to walk through every TODO with
the source viewer.

See `CONVERSION_REPORT.md` in the output directory for the per-file
breakdown.

---

## Limitations and design notes

* The C parser is **pattern-based**, not a full C compiler. It handles
  the common cases (function definitions, simple control flow, GTK
  accessor calls, header declarations) and falls back to TODO comments
  for anything else. This is a deliberate trade-off: a fully accurate C
  parser would be several times larger and would still produce
  semantically wrong Pascal for non-trivial pointer arithmetic, function
  pointers, and macro expansions.
* Header files are translated **declaratively**: only `#define`,
  `typedef struct/enum`, `extern`, and function prototypes are emitted.
  Macros with parameters, inline functions, and `#ifdef` blocks become
  TODO markers so the user can decide what to keep.
* The converter does **not** attempt to translate the GTK theme/CSS.
  Per-widget colors and fonts can be added by hand in the LFM.
* `GtkTreeView` columns are added at runtime in the original C; the
  generated `TListView` is created with `vsReport` and one placeholder
  column. You will need to add real columns in code.
* `GtkAdjustment` objects (separate top-level objects in Glade) are
  inlined into the owning `TSpinEdit` / `TTrackBar` via `MinValue`,
  `MaxValue`, `Value` and `Increment`.

---

## License

`gtk2laz` itself is released under the MIT license. The demo
`opgui_lazarus/` output is derived from OPGUI which is GPL-2.0; any
derivative work must respect that license.

---

## Quick start

```bash
# Run directly with Python 3.8+ (no third-party deps required):
python3 gtk2laz.py gui                              # launch the GUI
python3 gtk2laz.py convert  src/  out/              # full conversion
python3 gtk2laz.py c2pas     src/  out/              # C -> Pascal only

# Or build a single-file x86-64 executable (see "Building the .exe"):
./dist/gtk2laz gui
./dist/gtk2laz c2pas /path/to/gtk_source  /path/to/out
./dist/gtk2laz list-todos /path/to/out   # JSON dump of every TODO
```

The output directory will contain:

```
myproject/
├── myproject.lpr              ← main program (skipped in c2pas mode)
├── myproject.lpi              ← Lazarus project info (skipped in c2pas mode)
├── mainform.pas               ← form unit (Glade → LFM)
├── mainform.lfm               ← form resource
├── <otherunit>.pas            ← one per .c file
├── todos.json                 ← machine-readable TODO list
├── resources/                 ← .png / .css copied from the source tree
└── CONVERSION_REPORT.md       ← per-file stats + manual work checklist
```

Open `myproject.lpi` in the Lazarus IDE, or build headlessly with:

```bash
lazbuild myproject.lpi
```

---

## Subcommands

| Command                          | Purpose                                                   |
| -------------------------------- | --------------------------------------------------------- |
| `gtk2laz gui`                    | Launch the interactive Tkinter GUI front-end.             |
| `gtk2laz convert INPUT OUTPUT`   | Full conversion: Glade form + all `.c` files.             |
| `gtk2laz c2pas INPUT OUTPUT`     | **Standalone C → Pascal translation** (skip Glade form).  |
| `gtk2laz list-todos OUTPUT`      | Print every TODO marker in a converted project as JSON.   |

For backward compatibility, `gtk2laz INPUT OUTPUT` (no subcommand) is
treated as `gtk2laz convert INPUT OUTPUT`.

### Common options for `convert`

| Option           | Default     | Description                                |
| ---------------- | ----------- | ------------------------------------------ |
| `--form-unit`    | `mainform`  | Pascal unit name for the main form.        |
| `--form-class`   | `TMainForm` | Pascal class name for the main form.       |
| `--glade`        | (auto)      | Specific `.glade` file to use.             |
| `--report`       | (auto)      | Path to write the conversion report.       |
| `--c-only`       | off         | Skip the Glade form (alias for `c2pas`).   |

### Options for `c2pas`

| Option        | Default | Description                                       |
| ------------- | ------- | ------------------------------------------------- |
| `--form-unit` | (none)  | Optional form unit to add to each unit's `uses`.  |
| `--report`    | (auto)  | Path to write the conversion report.              |

---

## The GUI front-end

`gtk2laz gui` opens a 1200×780 window with three regions:

1. **Conversion panel (top)** — input/output directory pickers, a
   mode selector (Full / C-only / Reload TODOs), and a "Run conversion"
   button. Conversion runs in a background thread so the UI stays
   responsive.
2. **TODO list (left)** — a tree view showing every `(* TODO gtk2laz … *)`
   marker produced by the conversion. Columns:
   `File:Line | Function | Reason | C snippet`. A free-text filter
   narrows the list live. Double-click (or Enter) opens the source file
   in your `$EDITOR`.
3. **Source viewer (right)** — when a TODO is selected, this panel
   shows the original C file with line numbers and the offending line
   highlighted in yellow. The header summarises
   `source_file:line | pascal_file:line | function | reason`.

This lets you walk through every spot the converter could not handle
and jump straight to the original C source for context.

---

## What gets converted automatically

### Glade XML → LFM (form resource)

| GTK widget                | LCL component        |
| ------------------------- | -------------------- |
| `GtkWindow` / `GtkDialog` | `TForm`              |
| `GtkBox` (vert/horiz)     | `TPanel`             |
| `GtkFrame`                | `TGroupBox`          |
| `GtkNotebook`             | `TPageControl` (children become `TTabSheet`s; tab labels extracted from `<child type="tab">`) |
| `GtkToolbar`              | `TToolBar`           |
| `GtkToolButton`           | `TToolButton`        |
| `GtkSeparatorToolItem`    | `TToolButton` (`Style = tbsSeparator`) |
| `GtkButton`               | `TButton`            |
| `GtkToggleButton`         | `TSpeedButton` (`AllowAllUp`, `GroupIndex = 0`) |
| `GtkCheckButton`          | `TCheckBox`          |
| `GtkRadioButton`          | `TRadioButton`       |
| `GtkLabel`                | `TLabel`             |
| `GtkEntry`                | `TEdit`              |
| `GtkSpinButton` + `GtkAdjustment` | `TSpinEdit` (lower/upper/value inlined) |
| `GtkComboBoxText`         | `TComboBox`          |
| `GtkTextView` (inside `GtkScrolledWindow`) | `TMemo` (inside `TScrollBox`) |
| `GtkTreeView`             | `TListView` (`vsReport`) |
| `GtkStatusbar`            | `TStatusBar`         |
| `GtkScale`                | `TTrackBar`          |
| `GtkImage`                | `TImage`             |
| `GtkSeparator`            | `TBevel`             |
| `GtkMenuBar` / `GtkMenu` / `GtkMenuItem` | `TMainMenu` / `TPopupMenu` / `TMenuItem` |

Layout heuristics:

* A `GtkBox` becomes a `TPanel` whose children get `Align = alTop`/`alLeft`/`alClient`
  depending on the box orientation and the `expand` packing flag.
* `GtkNotebook` children become `TTabSheet` objects inside the `TPageControl`.
* `GtkScrolledWindow` becomes `TScrollBox`; the inner widget gets `Align = alClient`.
* `GtkStatusbar` gets `Align = alBottom`; `GtkToolbar`/`GtkMenuBar` get `Align = alTop`.

### Signals → LCL events

| GTK signal              | LCL event              |
| ----------------------- | ---------------------- |
| `clicked`               | `OnClick`              |
| `toggled`               | `OnClick` (speed buttons) |
| `changed`               | `OnChange`             |
| `value-changed`         | `OnChange`             |
| `destroy`               | `OnClose`              |
| `delete-event`          | `OnCloseQuery`         |
| `key-press-event`       | `OnKeyDown`            |
| `button-press-event`    | `OnMouseDown`          |
| `switch-page`           | `OnChange`             |

Handler names are preserved verbatim. The form's `.pas` unit declares each
handler as an empty procedure stub (`// TODO: port original C handler ...`),
so the LFM-to-Pascal linkage works out of the box.

### C source → Pascal unit (per `.c` file)

The translator walks each C file and emits a `.pas` unit containing:

* `#define` constants → Pascal `const` block (with `0x..` → `$..` translation).
* `typedef`s and `extern` globals → Pascal `type`/`var` blocks. Common Windows
  typedefs (`DWORD`, `WORD`, `BYTE`, `HANDLE`, ...) are mapped to LCL types.
* Function definitions → Pascal `procedure`/`function` with translated
  signatures (`int → Integer`, `char* → PChar`, `BYTE* → PByte`, ...).
* Function bodies are translated statement-by-statement:
  * GTK calls (`gtk_button_set_label`, `gtk_toggle_button_get_active`,
    `gtk_statusbar_push`, ...) → LCL equivalents (`Caption :=`,
    `Checked`, `SimpleText :=`, ...). See `widget_map.GTK_CALL_MAP` for the
    full table.
  * libc helpers (`sprintf` → `Format`, `strcmp` → `AnsiCompareStr`,
    `strlen` → `Length`, `malloc` → `GetMem`, ...). See `widget_map.C_LIB_MAP`.
  * Control flow: `if/else`, `for` (with `i++` step → `for i := 0 to N-1 do`),
    `while`, `do/while` → `repeat/until not (...)`, `switch` → `case/of`,
    `return` → `Exit` / `Exit(value)`.
  * Operators: `&&`, `||`, `!`, `==`, `!=`, `<<`, `>>` → Pascal equivalents.
  * Post-increment `x++` → `Inc(x)` (semantic approximation; the user must
    verify expressions that depend on the pre-increment value).

---

## What requires manual work

The converter inserts `(* TODO gtk2laz: ... *)` markers for everything it
cannot confidently translate. Each marker is also recorded in
`todos.json` and surfaced in the GUI's TODO list. Typical items:

* **HID/USB I/O** — `ioctl`, `hidraw`, `ReadFile`/`WriteFile`, `SetupDi*`
  calls. Replace with `TFileStream` on `/dev/hidraw*` (Linux) or a JEDI-HID
  library (Windows).
* **Tree/List models** — GTK's `GtkTreeIter` / `GtkListStore` API has no
  1:1 LCL equivalent. Rewrite using `TListView.Items.Add`.
* **Custom drawing** — `cairo_t` from a `GtkDrawingArea` `draw` signal →
  `TCanvas` methods in `OnPaint`.
* **CSS styling** — GTK themes have no LCL counterpart; restyle with
  `Color`/`Font` properties.
* **Resource paths** — `/res/foo.png` from `glib-compile-resources` →
  load via `TResourceStream` or copy into the executable directory and
  `TImage.Picture.LoadFromFile`.
* **Post-increment in expressions** — `arr[i++] = x;` becomes
  `arr[Inc(i)] := x;` which compiles but does not return the pre-increment
  value. Rewrite as `arr[i] := x; Inc(i);`.

---

## Module layout

```
gtk2laz/
├── gtk2laz.py         ← CLI entry point + project file generator
├── gtk2laz_gui.py     ← Tkinter GUI front-end (TODO browser + source viewer)
├── glade_parser.py    ← Glade XML → LFM/LPR/PAS form emitter
├── c_parser.py        ← C source → Pascal unit translator
├── widget_map.py      ← GTK→LCL widget/signal/call mapping tables
├── README.md          ← this file
├── build_exe.sh       ← PyInstaller single-file build script
└── gtk2laz.spec       ← PyInstaller spec
```

The mapping tables in `widget_map.py` are deliberately data-driven so the
converter can be retuned for a different GTK application by editing one
file.

---

## Building the single-file executable

`gtk2laz` ships as pure Python with no third-party dependencies, but the
project includes a PyInstaller spec to bundle it into a single
self-contained **x86-64** executable that includes the Tcl/Tk runtime
(so the GUI works on a stock Linux without a system Tk install).

```bash
# Install PyInstaller (one-time):
pip install pyinstaller

# Build the .exe:
bash build_exe.sh

# The result is dist/gtk2laz (Linux) or dist/gtk2laz.exe (Windows).
./dist/gtk2laz gui                                  # launch the GUI
./dist/gtk2laz c2pas /path/to/src /path/to/out      # standalone C->Pascal
./dist/gtk2laz convert /path/to/src /path/to/out    # full GTK->Lazarus
./dist/gtk2laz list-todos /path/to/out              # JSON dump of TODOs
```

The build script invokes PyInstaller with `--onefile` so the entire
Python runtime, the parser modules, the mapping tables, and the
Tcl/Tk libraries are packed into one binary that can be shipped
without requiring Python on the target machine.

---

## Demo: converting the OPGUI project

The repository's demo target is the Open Programmer GUI
(`opgui-0.12.4`, https://sourceforge.net/projects/openprogrammer/), a
~42 000-line GTK3 application that drives a USB programmer for PIC/AVR
microcontrollers. Running

```bash
python3 gtk2laz.py  c2pas  /path/to/opgui-0.12.4  opgui_lazarus
```

produces 16 Pascal units. Typical conversion stats:

| Metric                       | Value   |
| ---------------------------- | ------- |
| C functions translated       | ~117    |
| Global variables emitted     | ~81     |
| Statements auto-translated   | ~15 000 |
| Statements marked TODO       | ~1 100  |
| Auto-translation rate        | ~93 %   |

Open the GUI (`gtk2laz gui`) and pick the converted output directory
in "Reload TODOs from output" mode to walk through every TODO with
the source viewer.

See `CONVERSION_REPORT.md` in the output directory for the per-file
breakdown.

---

## Limitations and design notes

* The C parser is **pattern-based**, not a full C compiler. It handles
  the common cases (function definitions, simple control flow, GTK
  accessor calls) and falls back to TODO comments for anything else.
  This is a deliberate trade-off: a fully accurate C parser would be
  several times larger and would still produce semantically wrong Pascal
  for non-trivial pointer arithmetic, function pointers, and macro
  expansions.
* The converter does **not** attempt to translate the GTK theme/CSS.
  Per-widget colors and fonts can be added by hand in the LFM.
* `GtkTreeView` columns are added at runtime in the original C; the
  generated `TListView` is created with `vsReport` and one placeholder
  column. You will need to add real columns in code.
* `GtkAdjustment` objects (separate top-level objects in Glade) are
  inlined into the owning `TSpinEdit` / `TTrackBar` via `MinValue`,
  `MaxValue`, `Value` and `Increment`.

---

## License

`gtk2laz` itself is released under the MIT license. The demo
`opgui_lazarus/` output is derived from OPGUI which is GPL-2.0; any
derivative work must respect that license.
