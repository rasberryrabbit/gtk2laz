"""
gtk2laz.py
==========
GTK (C + Glade XML) -> FreePascal / Lazarus LCL converter.

Usage
-----
::

    python gtk2laz.py <input_dir> <output_dir> [--form-unit NAME]
                                                 [--form-class NAME]
                                                 [--glade FILENAME]
                                                 [--report PATH]

``input_dir``  : directory containing the GTK source tree
                 (.glade file plus .c/.h files).
``output_dir`` : Lazarus project directory to create.

The converter writes:

  - ``<output_dir>/<project>.lpr``   main program
  - ``<output_dir>/<project>.lpi``   Lazarus project info
  - ``<output_dir>/<unit>.lfm``      one form resource per glade file
  - ``<output_dir>/<unit>.pas``      Pascal unit per .c file plus the form unit
  - ``<output_dir>/CONVERSION_REPORT.md``
  - ``<output_dir>/resources/``      copies of any *.png referenced by the glade

Limitations
-----------
See ``CONVERSION_REPORT.md`` generated next to the project; it lists every
TODO marker the converter inserted.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from typing import Optional

# Make sibling modules importable when run as a script
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from glade_parser import convert_glade          # noqa: E402
from c_parser import (convert_c_file,           # noqa: E402
                      convert_header_file,
                      scan_local_includes,
                      header_to_unit_name)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
@dataclass
class Project:
    input_dir: str
    output_dir: str
    glade_files: list[str] = field(default_factory=list)
    c_files: list[str] = field(default_factory=list)
    h_files: list[str] = field(default_factory=list)
    resource_files: list[str] = field(default_factory=list)


def _order_headers(h_files: list[str],
                    h_index: dict[str, str]) -> list[str]:
    """Topologically sort headers so that a header A which #includes
    header B comes after B in the output list.

    Cycles (rare but possible with mutual includes via include guards)
    are broken arbitrarily so the sort always terminates.
    """
    visited: set[str] = set()
    on_stack: set[str] = set()
    order: list[str] = []

    def visit(path: str) -> None:
        if path in visited:
            return
        if path in on_stack:
            # cycle: just stop
            return
        on_stack.add(path)
        for inc_name in scan_local_includes(path):
            base = os.path.basename(inc_name)
            if base in h_index and h_index[base] != path:
                visit(h_index[base])
        on_stack.discard(path)
        visited.add(path)
        order.append(path)

    for h in sorted(h_files):
        visit(h)
    return order


# ---------------------------------------------------------------------------
# Header-aware C file conversion
# ---------------------------------------------------------------------------
def convert_c_file_with_includes(c_path: str, out_dir: str,
                                  unit_name: str,
                                  form_unit: Optional[str],
                                  included_units: list[str]) -> dict:
    """Convert a .c file and add the given header-derived unit names to
    the generated unit's ``uses`` clause.

    This is now a thin wrapper around ``c_parser.convert_c_file`` which
    itself accepts ``included_units`` directly (and also tries the
    pycparser-based AST translator for accurate signatures).
    """
    return convert_c_file(c_path, out_dir,
                           unit_name=unit_name,
                           form_unit=form_unit,
                           included_units=included_units)


def discover(root: str) -> Project:
    glade, c, h, res = [], [], [], []
    for dirpath, _dirs, files in os.walk(root):
        # skip vendor / hidden dirs
        if any(part.startswith(".") for part in dirpath.split(os.sep)):
            continue
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            full = os.path.join(dirpath, f)
            if ext == ".glade":
                glade.append(full)
            elif ext == ".c":
                c.append(full)
            elif ext == ".h":
                h.append(full)
            elif ext in (".png", ".jpg", ".jpeg", ".svg", ".css", ".xml"):
                res.append(full)
    return Project(input_dir=root, output_dir="", glade_files=glade,
                   c_files=c, h_files=h, resource_files=res)


# ---------------------------------------------------------------------------
# LPR / LPI emitter
# ---------------------------------------------------------------------------
LPR_TEMPLATE = """\
program {project};

{{$mode objfpc}}{{$H+}}

uses
  {{$IFDEF UNIX}}{{$IFDEF UseCThreads}}
  cthreads,
  {{$ENDIF}}{{$ENDIF}}
  Interfaces, // this includes the LCL widget set
  Forms
{unit_uses};

{{$R *.res}}

begin
  RequireDerivedFormResource := True;
  Application.Scaled := True;
  Application.Initialize;
  Application.CreateForm({form_class}, {form_var});
  Application.Run;
end.
"""

LPI_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<CONFIG>
  <ProjectOptions>
    <Version Value="12"/>
    <General>
      <SessionStorage Value="InProjectDir"/>
      <Title Value="{project}"/>
      <ResourceType Value="res"/>
    </General>
    <BuildModes>
      <Item Name="Default" Default="True"/>
    </BuildModes>
    <PublishOptions>
      <Version Value="2"/>
      <IncludeFileFilter Value="*.(pas|pp|inc|lfm|lpr|lpi|lrres|lrs)"/>
    </PublishOptions>
    <RunParams>
      <FormatVersion Value="2"/>
    </RunParams>
    <RequiredPackages>
      <Item>
        <PackageName Value="LCL"/>
      </Item>
    </RequiredPackages>
    <Units>
{unit_units}
    </Units>
  </ProjectOptions>
  <CompilerOptions>
    <Version Value="11"/>
    <Target>
      <Filename Value="{project}"/>
    </Target>
    <SearchPaths>
      <IncludeFiles Value="$(ProjOutDir)"/>
      <OtherUnitFiles Value="."/>
      <UnitOutputDirectory Value="lib/$(TargetCPU)-$(TargetOS)"/>
    </SearchPaths>
    <Parsing>
      <SyntaxOptions>
        <SyntaxMode Value="ObjFPC"/>
        <CStyleOperator Value="True"/>
        <AllowLabel Value="False"/>
        <CPPInline Value="True"/>
        <UseAnsiStrings Value="True"/>
      </SyntaxOptions>
    </Parsing>
    <Linking>
      <Debugging>
        <UseExternalDbgSyms Value="True"/>
      </Debugging>
      <Options>
        <Win32>
          <GraphicApplication Value="True"/>
        </Win32>
      </Options>
    </Linking>
  </CompilerOptions>
  <CompilerMessages>
    <IgnoredMessages>
      <Code>5024</Code>
    </IgnoredMessages>
  </CompilerMessages>
</CONFIG>
"""


def emit_lpr(project_name: str, form_class: str, units: list[str]) -> str:
    # Form instance variable name = class minus leading 'T'.
    form_var = (form_class[1:] if form_class.startswith("T") else form_class)
    uses_lines = []
    # Put the form unit first
    if units:
        uses_lines.append(f"  , {units[0]}")
    for u in units[1:]:
        uses_lines.append(f"  , {u}")
    return LPR_TEMPLATE.format(
        project=project_name,
        form_class=form_class,
        form_var=form_var,
        unit_uses="\n".join(uses_lines) or "",
    )


def emit_lpi(project_name: str, form_unit: str, units: list[str]) -> str:
    unit_lines = []
    for i, u in enumerate(units):
        is_form = (u == form_unit)
        unit_lines.append(
            "      <Unit>\n"
            f"        <Filename Value=\"{u}.pas\"/>\n"
            f"        <IsPartOfProject Value=\"True\"/>\n"
            f"        <ComponentName Value=\"{u}\"/>\n"
            f"        <HasResources Value=\"{is_form}\"/>\n"
            f"        <ResourcePath Value=\".lfm\"/>\n"
            "      </Unit>"
        )
    return LPI_TEMPLATE.format(
        project=project_name,
        unit_units="\n".join(unit_lines),
    )


# ---------------------------------------------------------------------------
# Main conversion driver
# ---------------------------------------------------------------------------
def convert(input_dir: str, output_dir: str,
            form_unit: str = "mainform",
            form_class: str = "TMainForm",
            glade_name: Optional[str] = None,
            report_path: Optional[str] = None,
            c_only: bool = False) -> dict:
    """Convert a GTK source tree into a Lazarus project.

    When ``c_only`` is True, the Glade form is skipped and only the C
    files are translated. Useful when the user wants pure C -> Pascal
    translation without involving the GUI definition.
    """
    proj = discover(input_dir)
    proj.output_dir = output_dir
    os.makedirs(output_dir, exist_ok=True)

    glade_result = None
    if not c_only:
        # Pick the glade file
        glade_path = None
        if glade_name:
            for g in proj.glade_files:
                if os.path.basename(g) == glade_name:
                    glade_path = g
                    break
        if glade_path is None and proj.glade_files:
            glade_path = proj.glade_files[0]
        if glade_path is None:
            raise SystemExit("No .glade file found in input directory "
                             "(use --c-only to skip GUI conversion)")

        # Convert glade -> LFM + PAS
        glade_result = convert_glade(glade_path, output_dir,
                                      unit_name=form_unit,
                                      form_class_name=form_class)

    # Copy resources
    res_dir = os.path.join(output_dir, "resources")
    os.makedirs(res_dir, exist_ok=True)
    copied_res: list[str] = []
    for r in proj.resource_files:
        try:
            shutil.copy2(r, res_dir)
            copied_res.append(os.path.basename(r))
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Pass 1: convert all local header files first.
    # Each header becomes its own .pas unit. Headers that #include other
    # local headers get those units added to their `uses` clause so the
    # Pascal dependency graph mirrors the C #include graph.
    # ------------------------------------------------------------------
    # Map: header basename -> absolute path (so we can resolve includes)
    h_index: dict[str, str] = {}
    for h in proj.h_files:
        h_index[os.path.basename(h)] = h

    # Map: header basename -> pascal unit name (lowercase)
    h_unit_map: dict[str, str] = {}
    for h in proj.h_files:
        h_unit_map[os.path.basename(h)] = header_to_unit_name(h)

    header_results: list[dict] = []
    # Convert headers in dependency order: a header that #includes another
    # local header must come after it. We do a simple topological sort.
    header_order = _order_headers(proj.h_files, h_index)
    for h_path in header_order:
        # Resolve this header's local includes to Pascal unit names
        inc_units = []
        for inc_name in scan_local_includes(h_path):
            # inc_name might be relative; resolve via h_index
            base = os.path.basename(inc_name)
            if base in h_unit_map and h_unit_map[base] != header_to_unit_name(h_path):
                inc_units.append(h_unit_map[base])
        result = convert_header_file(h_path, output_dir,
                                      included_units=inc_units)
        header_results.append(result)

    # ------------------------------------------------------------------
    # Pass 2: convert C files. For each C file, scan its #include lines
    # and add the corresponding already-converted header unit names to
    # the generated .pas unit's `uses` clause so symbols declared in
    # headers are visible.
    # ------------------------------------------------------------------
    c_results = []
    c_files_sorted = sorted(proj.c_files)
    # Put opgui.c first if present so the form_unit name aligns
    opgui_c = next((f for f in c_files_sorted
                    if os.path.basename(f) == "opgui.c"), None)
    if opgui_c:
        c_files_sorted.remove(opgui_c)
        c_files_sorted.insert(0, opgui_c)

    unit_names: list[str] = []
    if not c_only:
        unit_names.append(form_unit)  # form unit first
    # Header-derived units come before C-derived units so C files can
    # reference header symbols.
    for r in header_results:
        unit_names.append(r["unit_name"])
    for c_path in c_files_sorted:
        base = os.path.splitext(os.path.basename(c_path))[0].lower()
        if base == "opgui" and not c_only:
            # Already represented by the form unit; skip duplicate
            continue
        if base[0].isdigit():
            base = "u" + base
        # Collect header units this C file #includes.
        inc_units = []
        for inc_name in scan_local_includes(c_path):
            base_inc = os.path.basename(inc_name)
            if base_inc in h_unit_map:
                inc_units.append(h_unit_map[base_inc])
        # Avoid self-reference (e.g. foo.c including foo.h would produce
        # both `foo` units; skip the same-named one).
        inc_units = [u for u in inc_units if u != base]
        result = convert_c_file_with_includes(
            c_path, output_dir,
            unit_name=base,
            form_unit=form_unit if not c_only else None,
            included_units=inc_units,
        )
        c_results.append(result)
        unit_names.append(result["unit_name"])

    # Generate .lpr and .lpi (skip in c_only mode - no form to anchor to)
    project_name = os.path.basename(output_dir.rstrip(os.sep)) or "gtk_project"
    if not c_only:
        lpr_text = emit_lpr(project_name, form_class, unit_names)
        lpi_text = emit_lpi(project_name, form_unit, unit_names)

        with open(os.path.join(output_dir, project_name + ".lpr"),
                  "w", encoding="utf-8") as f:
            f.write(lpr_text)
        with open(os.path.join(output_dir, project_name + ".lpi"),
                  "w", encoding="utf-8") as f:
            f.write(lpi_text)

    # Build conversion report
    report = build_report(proj, glade_result, c_results, header_results,
                          form_unit, form_class, project_name,
                          copied_res, c_only=c_only)
    if report_path is None:
        report_path = os.path.join(output_dir, "CONVERSION_REPORT.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    # Aggregate all TodoItems across all converted C and header files.
    all_todos = []
    for r in c_results:
        for todo in r.get("todos", []):
            all_todos.append(_todo_to_dict(todo))
    for r in header_results:
        for todo in r.get("todos", []):
            all_todos.append(_todo_to_dict(todo))

    # Persist the TODO list as JSON so the GUI (and the list-todos
    # subcommand) can resolve source-file and source-line for each marker
    # without re-parsing the .pas output.
    todos_json_path = os.path.join(output_dir, "todos.json")
    import json as _json
    with open(todos_json_path, "w", encoding="utf-8") as f:
        _json.dump([_todo_to_dict(t) for t in all_todos], f,
                    indent=2, ensure_ascii=False)

    return {
        "project_name": project_name,
        "output_dir": output_dir,
        "form_unit": form_unit,
        "form_class": form_class,
        "glade_result": glade_result,
        "c_results": c_results,
        "header_results": header_results,
        "report_path": report_path,
        "units": unit_names,
        "todos": all_todos,
        "c_only": c_only,
    }


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------
def build_report(proj: Project, glade_result: dict, c_results: list[dict],
                 header_results: list[dict],
                 form_unit: str, form_class: str, project_name: str,
                 copied_res: list[str], c_only: bool = False) -> str:
    total_funcs = sum(r["stats"]["functions"] for r in c_results)
    total_globals = sum(r["stats"]["globals"] for r in c_results)
    total_translated = sum(r["stats"]["translated_stmts"] for r in c_results)
    total_todos = sum(r["stats"]["todo_stmts"] for r in c_results)
    # Header stats
    h_defines = sum(r["stats"].get("defines", 0) for r in header_results)
    h_typedefs = sum(r["stats"].get("typedefs", 0) for r in header_results)
    h_externs = sum(r["stats"].get("globals", 0) for r in header_results)
    h_protos = sum(r["stats"].get("prototypes", 0) for r in header_results)
    h_todos = sum(r["stats"].get("todo_stmts", 0) for r in header_results)
    total_todos += h_todos

    lines: list[str] = []
    lines.append(f"# Conversion Report — {project_name}")
    lines.append("")
    lines.append("Generated by **gtk2laz**.")
    if c_only:
        lines.append("")
        lines.append("_Mode: C → Pascal only (Glade GUI skipped)._")
    lines.append("")
    lines.append("## Input")
    lines.append("")
    lines.append(f"- Source directory: `{proj.input_dir}`")
    if glade_result is not None:
        lines.append(f"- Glade file: `{os.path.basename(proj.glade_files[0]) if proj.glade_files else '?'}`")
    lines.append(f"- C source files: {len(proj.c_files)}")
    lines.append(f"- Header files (.h): {len(proj.h_files)}")
    lines.append(f"- Resources copied: {len(copied_res)}")
    lines.append("")
    lines.append("## Output")
    lines.append("")
    lines.append(f"- Output directory: `{proj.output_dir}`")
    if not c_only:
        lines.append(f"- Lazarus project: `{project_name}.lpr` / `{project_name}.lpi`")
        lines.append(f"- Main form unit: `{form_unit}.pas` / `{form_unit}.lfm`")
        lines.append(f"- Form class: `{form_class}`")
    lines.append(f"- Header-derived units: {len(header_results)}")
    lines.append(f"- C-derived units: {len(c_results)}")
    lines.append("")
    lines.append("## Statistics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    if glade_result is not None:
        lines.append(f"| Widgets converted (LFM) | {glade_result['widget_count']} |")
    lines.append(f"| Header #defines converted | {h_defines} |")
    lines.append(f"| Header typedefs converted | {h_typedefs} |")
    lines.append(f"| Header extern globals | {h_externs} |")
    lines.append(f"| Header function prototypes | {h_protos} |")
    lines.append(f"| C functions translated | {total_funcs} |")
    lines.append(f"| C global variables emitted | {total_globals} |")
    lines.append(f"| C statements auto-translated | {total_translated} |")
    lines.append(f"| Statements marked TODO (C + headers) | {total_todos} |")
    if total_translated + total_todos > 0:
        pct = 100.0 * total_translated / (total_translated + total_todos)
        lines.append(f"| Auto-translation rate | {pct:.1f}% |")
    lines.append("")
    lines.append("## Per-file breakdown")
    lines.append("")
    lines.append("### Header files (.h → .pas)")
    lines.append("")
    lines.append("| Unit | Source | Defines | Typedefs | Externs | Protos | TODO |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in header_results:
        s = r["stats"]
        lines.append(f"| `{r['unit_name']}` | `{os.path.basename(r['source'])}` | "
                     f"{s.get('defines', 0)} | {s.get('typedefs', 0)} | "
                     f"{s.get('globals', 0)} | {s.get('prototypes', 0)} | "
                     f"{s.get('todo_stmts', 0)} |")
    lines.append("")
    lines.append("### C source files (.c → .pas)")
    lines.append("")
    lines.append("| Unit | Source | Functions | Globals | Translated | TODO |")
    lines.append("|---|---|---|---|---|---|")
    for r in c_results:
        s = r["stats"]
        lines.append(f"| `{r['unit_name']}` | `{os.path.basename(r['source'])}` | "
                     f"{s['functions']} | {s['globals']} | "
                     f"{s['translated_stmts']} | {s['todo_stmts']} |")
    lines.append("")
    lines.append("## Conversion approach")
    lines.append("")
    lines.append("### GUI (Glade → LFM)")
    lines.append("")
    lines.append("Each GTK widget class is mapped to its closest LCL counterpart:")
    lines.append("")
    lines.append("- `GtkWindow` → `TForm`")
    lines.append("- `GtkBox` (vertical/horizontal) → `TPanel` with `Align`/`ChildSizing`")
    lines.append("- `GtkButton` → `TButton`; `GtkToolButton` → `TToolButton`")
    lines.append("- `GtkLabel` → `TLabel`; `GtkEntry` → `TEdit`; `GtkSpinButton` → `TSpinEdit`")
    lines.append("- `GtkComboBoxText` → `TComboBox`")
    lines.append("- `GtkTextView` → `TMemo` (inside `GtkScrolledWindow` → `TScrollBox`)")
    lines.append("- `GtkNotebook` → `TPageControl` (children become `TTabSheet`s)")
    lines.append("- `GtkCheckButton`/`GtkRadioButton`/`GtkToggleButton` → `TCheckBox`/`TRadioButton`/`TSpeedButton`")
    lines.append("- `GtkStatusbar` → `TStatusBar`")
    lines.append("- `GtkTreeView` → `TListView` (columns added at runtime)")
    lines.append("- `GtkAdjustment` objects are inlined into their owning `TSpinEdit`/`TTrackBar`")
    lines.append("")
    lines.append("### Signals → Events")
    lines.append("")
    lines.append("GTK signal handlers are connected to LCL events in the LFM:")
    lines.append("")
    lines.append("- `clicked` → `OnClick`")
    lines.append("- `toggled` → `OnClick` (for `TSpeedButton`)")
    lines.append("- `changed` → `OnChange`")
    lines.append("- `value-changed` → `OnChange`")
    lines.append("- `destroy` → `OnClose`")
    lines.append("- `key-press-event` → `OnKeyDown`")
    lines.append("- `button-press-event` → `OnMouseDown`")
    lines.append("")
    lines.append("Handler names are preserved verbatim so the C source bodies can be ported 1:1.")
    lines.append("")
    lines.append("### C → Pascal")
    lines.append("")
    lines.append("Each `.c` file becomes one `.pas` unit. The translator:")
    lines.append("")
    lines.append("1. Extracts function definitions and emits Pascal `procedure`/`function` signatures.")
    lines.append("2. Translates common GTK calls (`gtk_button_set_label`, `gtk_spin_button_get_value_as_int`, ...) "
                 "to LCL equivalents (`Caption :=`, `Value`, ...).")
    lines.append("3. Translates libc helpers (`sprintf`→`Format`, `strcmp`→`AnsiCompareStr`, `strlen`→`Length`, ...).")
    lines.append("4. Translates control flow (`if`/`for`/`while`/`switch`/`return`).")
    lines.append("5. Anything it cannot confidently translate is wrapped in a `(* TODO gtk2laz: ... *)` "
                 "comment block so the project still compiles once the offending statement is replaced by a stub.")
    lines.append("")
    lines.append("## Manual work required")
    lines.append("")
    lines.append("Search the project for `TODO gtk2laz` to find every spot needing attention. Typical items:")
    lines.append("")
    lines.append("- **HID/USB I/O**: `ioctl`, `hidraw`, `ReadFile`/`WriteFile`, `SetupDi*` calls. "
                 "Replace with `TFileStream` on `/dev/hidraw*` (Linux) or a JEDI-HID library.")
    lines.append("- **Tree/List model manipulation**: GTK's `GtkTreeIter`/`GtkListStore` API has no 1:1 LCL equivalent. "
                 "Rewrite using `TListView.Items.Add`.")
    lines.append("- **Custom drawing**: `cairo_t` from `GtkDrawingArea` `draw` signal → `TCanvas` methods in `OnPaint`.")
    lines.append("- **CSS styling**: GTK themes have no LCL counterpart; restyle with `Color`/`Font` properties.")
    lines.append("- **Resource paths**: `/res/foo.png` from `glib-compile-resources` → load via `TResourceStream` or "
                 "copy into the executable directory and `TImage.Picture.LoadFromFile`.")
    lines.append("")
    lines.append("## Building the project")
    lines.append("")
    lines.append("```bash")
    lines.append(f"# Open {project_name}.lpi in the Lazarus IDE,")
    lines.append("# or build from the command line:")
    lines.append(f"lazbuild {project_name}.lpi")
    lines.append("```")
    lines.append("")
    lines.append("If `lazbuild` reports errors, fix the first error and rebuild; many TODO markers will "
                 "manifest as syntax errors and resolve one-by-one as you replace them with real Pascal.")
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--form-unit", default="mainform",
                   help="Pascal unit name for the main form (default: mainform)")
    p.add_argument("--form-class", default="TMainForm",
                   help="Pascal class name for the main form (default: TMainForm)")
    p.add_argument("--glade", default=None,
                   help="Specific .glade file to use (default: first found)")
    p.add_argument("--report", default=None,
                   help="Path to write the conversion report "
                        "(default: <output_dir>/CONVERSION_REPORT.md)")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gtk2laz",
        description="Convert a GTK (C + Glade) project to a FreePascal / "
                    "Lazarus LCL project.",
    )
    parser.add_argument("--version", action="version",
                        version="gtk2laz 2.0")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # convert: full GTK -> Lazarus conversion
    p_conv = sub.add_parser("convert",
                             help="Full GTK -> Lazarus conversion "
                                  "(Glade + C source)")
    p_conv.add_argument("input_dir", help="GTK source tree directory")
    p_conv.add_argument("output_dir", help="Output directory")
    _add_common_args(p_conv)
    p_conv.add_argument("--c-only", action="store_true",
                        help="Only translate C -> Pascal; skip Glade form")

    # c2pas: standalone C -> Pascal translation (no Glade)
    p_c2p = sub.add_parser("c2pas",
                            help="Standalone C -> Pascal translation "
                                 "(skip Glade form)")
    p_c2p.add_argument("input_dir", help="Directory containing .c files")
    p_c2p.add_argument("output_dir", help="Output directory for .pas units")
    p_c2p.add_argument("--form-unit", default=None,
                        help="Optional form unit name to add to uses clauses")
    p_c2p.add_argument("--report", default=None,
                        help="Path to write the conversion report")

    # gui: launch the interactive GUI
    p_gui = sub.add_parser("gui",
                            help="Launch the interactive GUI front-end")

    # list-todos: print all TODOs from a previous conversion as JSON
    p_lt = sub.add_parser("list-todos",
                           help="List all TODO markers in a converted project")
    p_lt.add_argument("output_dir", help="Converted project directory")

    raw_argv = sys.argv[1:] if argv is None else list(argv)
    KNOWN_SUBS = {"convert", "c2pas", "gui", "list-todos", "-h", "--help"}
    if raw_argv and raw_argv[0] not in KNOWN_SUBS \
            and not raw_argv[0].startswith("-"):
        raw_argv = ["convert"] + raw_argv

    args = parser.parse_args(raw_argv)

    # ---- Subcommand: gui ----
    if args.command == "gui":
        from gtk2laz_gui import launch_gui
        launch_gui()
        return 0

    # ---- Subcommand: list-todos ----
    if args.command == "list-todos":
        import json
        todos = scan_todos_in_project(args.output_dir)
        print(json.dumps(todos, indent=2, default=str))
        return 0

    # ---- Subcommand: c2pas ----
    if args.command == "c2pas":
        if not args.input_dir or not os.path.isdir(args.input_dir):
            print(f"error: input directory not found: {args.input_dir!r}",
                  file=sys.stderr)
            return 2
        if not args.output_dir:
            print("error: output_dir is required", file=sys.stderr)
            return 2
        result = convert(args.input_dir, args.output_dir,
                         form_unit=args.form_unit or "mainform",
                         form_class="TMainForm",
                         report_path=args.report,
                         c_only=True)
        print(f"C -> Pascal conversion complete: {result['output_dir']}")
        print(f"  Units:  {len(result['units'])}")
        print(f"  TODOs:  {len(result['todos'])}")
        print(f"  Report: {result['report_path']}")
        return 0

    # ---- Subcommand: convert ----
    if args.command == "convert":
        if not args.input_dir or not os.path.isdir(args.input_dir):
            print(f"error: input directory not found: {args.input_dir!r}",
                  file=sys.stderr)
            return 2
        if not args.output_dir:
            print("error: output_dir is required", file=sys.stderr)
            return 2
        c_only = getattr(args, "c_only", False)
        result = convert(args.input_dir, args.output_dir,
                         form_unit=args.form_unit,
                         form_class=args.form_class,
                         glade_name=args.glade,
                         report_path=args.report,
                         c_only=c_only)
        print(f"Converted {result['project_name']} -> {result['output_dir']}")
        if not c_only:
            print(f"  Form:   {result['form_unit']} ({result['form_class']})")
        print(f"  Units:  {len(result['units'])}")
        print(f"  TODOs:  {len(result['todos'])}")
        print(f"  Report: {result['report_path']}")
        return 0

    # No subcommand: print help.
    parser.print_help()
    return 0


def _todo_to_dict(todo) -> dict:
    """Normalise a TodoItem (now a plain dict) for JSON serialisation.

    Accepts both dict and dataclass-style objects so the function is
    robust to refactors.
    """
    if isinstance(todo, dict):
        return {
            "source_file":  todo.get("source_file", "") or "",
            "source_line":  todo.get("source_line", 0) or 0,
            "source_text":  todo.get("source_text", "") or "",
            "output_file":  todo.get("output_file", "") or "",
            "output_line":  todo.get("output_line", 0) or 0,
            "reason":       todo.get("reason", "") or "",
            "function":     todo.get("function", "") or "",
        }
    # Legacy dataclass path
    def _get(name, default):
        try:
            v = getattr(todo, name)
            return v if v else default
        except AttributeError:
            return default
    return {
        "source_file":  _get("source_file", ""),
        "source_line":  _get("source_line", 0),
        "source_text":  _get("source_text", ""),
        "output_file":  _get("output_file", ""),
        "output_line":  _get("output_line", 0),
        "reason":       _get("reason", ""),
        "function":     _get("function", ""),
    }


def scan_todos_in_project(project_dir: str) -> list[dict]:
    """Return all TODO markers in a converted project.

    Prefers the ``todos.json`` sidecar produced during conversion (it has
    accurate source-file and source-line info). Falls back to scanning
    the .pas files for ``(* TODO gtk2laz: ... *)`` markers when the
    sidecar is missing.

    The new multi-line TODO format is::

        (* TODO gtk2laz: <reason>
           Source: <file>:<line>
           Original C:
             <code>
        *)

    The scanner recognises both the new multi-line format and the
    legacy single-line format for backward compatibility.
    """
    import json
    import re
    sidecar = os.path.join(project_dir, "todos.json")
    if os.path.isfile(sidecar):
        try:
            with open(sidecar, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    # Fallback: scan .pas files for TODO markers.
    # New multi-line format: (* TODO gtk2laz: <reason>\n ... Source: ... \n ... Original C: ... *)
    # Old single-line format: (* TODO gtk2laz: <reason>: <text> *)
    multi_re = re.compile(
        r"\(\* TODO gtk2laz: (?P<reason>[^\n*]+)\n"
        r"\s*Source:\s*(?P<src>[^:\n]+):(?P<line>\d+)\n"
        r"(?:\s*Original C:\n(?P<code>(?:\s+.+\n)+))?"
        r"\s*\*\)",
        re.MULTILINE,
    )
    single_re = re.compile(
        r"\(\* TODO gtk2laz: (?P<reason>[^:]+):\s*(?P<text>.*?)\s*\*\)"
    )
    out: list[dict] = []
    for fname in sorted(os.listdir(project_dir)):
        if not fname.endswith(".pas"):
            continue
        path = os.path.join(project_dir, fname)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            continue
        lines = content.split("\n")
        # Multi-line format
        for m in multi_re.finditer(content):
            # Compute the line number where the TODO starts
            start_pos = m.start()
            line_num = content[:start_pos].count("\n") + 1
            code_block = ""
            if m.group("code"):
                # Strip the leading indentation from each code line
                code_lines = []
                for cl in m.group("code").split("\n"):
                    code_lines.append(cl.strip())
                code_block = "\n".join(l for l in code_lines if l)
            out.append({
                "source_file":  m.group("src").strip(),
                "source_line":  int(m.group("line") or 0),
                "source_text":  code_block or m.group("reason").strip(),
                "output_file":  path,
                "output_line":  line_num,
                "reason":       m.group("reason").strip(),
                "function":     "",
            })
        # Single-line (legacy) format - only if not already matched
        for i, line in enumerate(lines, start=1):
            m = single_re.search(line)
            if not m:
                continue
            # Skip if this line is part of a multi-line TODO we already
            # captured (heuristic: check if line_num is within any
            # multi-line match range)
            already = False
            for existing in out:
                if existing["output_file"] == path:
                    if existing["output_line"] <= i <= existing["output_line"] + 10:
                        already = True
                        break
            if already:
                continue
            out.append({
                "source_file":  "",
                "source_line":  0,
                "source_text":  m.group("text").strip(),
                "output_file":  path,
                "output_line":  i,
                "reason":       m.group("reason").strip(),
                "function":     "",
            })
    return out


if __name__ == "__main__":
    raise SystemExit(main())
