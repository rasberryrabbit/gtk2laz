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

# ---------------------------------------------------------------------------
# Version and build timestamp
# ---------------------------------------------------------------------------
# The build timestamp is written by ``build_win_exe.bat`` /
# ``build_exe.sh`` into ``_build_timestamp.txt`` next to gtk2laz.py at
# build time. We read it at import time so every run of the converter
# (CLI or GUI) knows exactly which source version it was built from.
# This is critical for diagnosing "the user is running an old build"
# situations: the timestamp appears at the top of ``conversion.log``
# and in the GUI title bar, so the user can tell at a glance whether
# they have the latest fixes.
VERSION = "1.4"

# Directory containing this file. Used to locate the source modules
# and to add them to ``sys.path``. Computed early so the
# source-timestamp reader below can use it.
HERE = os.path.dirname(os.path.abspath(__file__))


def _read_source_timestamp() -> str:
    """Compute the timestamp of the source code.

    Scans every ``.py`` file in the same directory as ``gtk2laz.py``
    and returns the NEWEST (most recently modified) file's mtime,
    formatted as a human-readable UTC string. This lets the user tell
    at a glance which version of the source they are running — even
    without a build step.

    Why the newest of all .py files (not just gtk2laz.py):
    A typical gtk2laz update touches ``pcpp_preprocessor.py`` or
    ``c_parser.py`` but leaves ``gtk2laz.py`` unchanged. If we only
    looked at gtk2laz.py's mtime, the timestamp would not reflect
    the update. By scanning all .py files, the timestamp always
    reflects the latest modification across the entire source tree.

    We also include the ``ply/`` subdirectory so changes to the
    bundled ply package are reflected too.

    Returns a string like ``"source:2026-11-04 02:07:19 UTC"``.
    The ``source:`` prefix distinguishes this from a build timestamp
    (which would be written by ``build_win_exe.bat`` / ``build_exe.sh``
    into ``_build_timestamp.txt`` — but we no longer use that file;
    the source mtime is always accurate and requires no build step).
    """
    import datetime as _dt
    newest_mtime = 0.0
    try:
        # Scan all .py files in the same directory as gtk2laz.py
        for fname in os.listdir(HERE):
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(HERE, fname)
            try:
                m = os.path.getmtime(fpath)
                if m > newest_mtime:
                    newest_mtime = m
            except OSError:
                continue
        # Also scan the ply/ subdirectory if it exists
        ply_dir = os.path.join(HERE, "ply")
        if os.path.isdir(ply_dir):
            for fname in os.listdir(ply_dir):
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(ply_dir, fname)
                try:
                    m = os.path.getmtime(fpath)
                    if m > newest_mtime:
                        newest_mtime = m
                except OSError:
                    continue
    except OSError:
        pass
    if newest_mtime <= 0:
        return "unknown"
    dt = _dt.datetime.fromtimestamp(newest_mtime, tz=_dt.timezone.utc)
    return f"source:{dt.strftime('%Y-%m-%d %H:%M:%S UTC')}"


BUILD_TIMESTAMP = _read_source_timestamp()


def get_version_string() -> str:
    """Return a full version string suitable for display.

    Format: ``"gtk2laz 1.4 (source:2026-11-04 02:07:19 UTC)"``

    The ``source:`` prefix and UTC timestamp tell the user exactly
    which version of the source code they are running. This is more
    useful than a build timestamp because it works even without a
    build step (e.g. running ``python gtk2laz.py`` directly from a
    clone), and it always reflects the latest modification across
    ALL source files — not just gtk2laz.py.
    """
    return f"gtk2laz {VERSION} ({BUILD_TIMESTAMP})"


# Make sibling modules importable when run as a script
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
# Conversion-log capture (CLI mode)
# ---------------------------------------------------------------------------
def _begin_log_capture(out_dir: str) -> str:
    """Begin capturing stderr to ``<out_dir>/conversion.log`` in addition
    to the user's console.

    Returns the path to the log file (which is created immediately so the
    directory exists). When the caller is done, it must call
    ``_end_log_capture(log_path)`` to restore stderr and close the file.

    Why we capture stderr (not stdout): all the gtk2laz diagnostic
    messages (``gtk2laz: AST parse failed for ...``, ``[info] ...``,
    traceback dumps, etc.) are printed to stderr. By teeing stderr to a
    file we get a full transcript of what happened during the conversion,
    which the user can review or share for troubleshooting — without
    having to scroll back through a long console window.
    """
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception:
        pass
    log_path = os.path.join(out_dir, "conversion.log")
    # Open the log file and write a header so it is self-documenting.
    # We keep the file handle in a module-global so ``_end_log_capture``
    # can close it without the caller having to pass it back in.
    import datetime as _dt
    global _LOG_FILE, _OLD_STDERR
    try:
        _LOG_FILE = open(log_path, "w", encoding="utf-8")
        _LOG_FILE.write(
            f"gtk2laz conversion log\n"
            f"Version: {get_version_string()}\n"
            f"Date: {_dt.datetime.now().isoformat(timespec='seconds')}\n"
            f"Output directory: {out_dir}\n"
            f"{'=' * 60}\n"
        )
        _LOG_FILE.flush()
    except Exception:
        _LOG_FILE = None
        return log_path
    # Replace sys.stderr with a tee that writes to both the original
    # stderr and the log file. We use a small wrapper class for this so
    # the file gets every line as it is printed (not just on flush).
    _OLD_STDERR = sys.stderr

    class _TeeStderr:
        def __init__(self, original, log_file):
            self._orig = original
            self._log = log_file

        def write(self, s):
            try:
                self._orig.write(s)
            except Exception:
                pass
            try:
                self._log.write(s)
                # Flush after every write so a crash mid-conversion
                # still leaves a complete log on disk.
                self._log.flush()
            except Exception:
                pass
            return len(s) if s else 0

        def flush(self):
            try:
                self._orig.flush()
            except Exception:
                pass
            try:
                self._log.flush()
            except Exception:
                pass

        # Some libraries check .isatty() to decide whether to use
        # colour codes etc. Forward to the real stderr so behaviour
        # matches the user's terminal.
        def isatty(self):
            try:
                return self._orig.isatty()
            except Exception:
                return False

        def fileno(self):
            return self._orig.fileno()

        def __getattr__(self, name):
            return getattr(self._orig, name)

    sys.stderr = _TeeStderr(_OLD_STDERR, _LOG_FILE)
    return log_path


def _end_log_capture(log_path: str) -> None:
    """Stop capturing stderr and close the log file.

    Restores the original ``sys.stderr`` and writes a final footer to
    the log file (with a note that this is the end of the conversion
    output) so the user can tell at a glance whether the log was
    truncated by a crash or contains the full run.
    """
    global _LOG_FILE, _OLD_STDERR
    # Restore original stderr FIRST so any error message we print
    # while closing the file goes to the real terminal.
    if "_OLD_STDERR" in globals() and _OLD_STDERR is not None:
        sys.stderr = _OLD_STDERR
        _OLD_STDERR = None
    if "_LOG_FILE" in globals() and _LOG_FILE is not None:
        try:
            _LOG_FILE.write(f"\n{'=' * 60}\nEnd of conversion log\n")
            _LOG_FILE.close()
        except Exception:
            pass
        _LOG_FILE = None


# Module-level state for the log-capture helpers above. We use globals
# because the helpers are called from the top-level CLI dispatcher and
# need to share the file handle / original stderr between begin() and
# end() without forcing the caller to thread the state through.
_LOG_FILE = None
_OLD_STDERR = None


# ---------------------------------------------------------------------------
# Header-aware C file conversion
# ---------------------------------------------------------------------------
def convert_c_file_with_includes(c_path: str, out_dir: str,
                                  unit_name: str,
                                  form_unit: Optional[str],
                                  included_units: list[str],
                                  header_types: list[str] = None,
                                  header_defines: list = None,
                                  header_externs: list = None,
                                  target_platform: str = "auto") -> dict:
    """Convert a .c file and add the given header-derived unit names to
    the generated unit's ``uses`` clause.

    ``header_types``, ``header_defines``, ``header_externs``: declarations
    extracted from the matching .h file (same basename as the .c file).
    These are merged into the .c file's output so that struct/enum/typedef
    declarations from the header are preserved (otherwise the .c output
    would overwrite the .h output since they share the same unit name).

    This is now a thin wrapper around ``c_parser.convert_c_file`` which
    itself accepts ``included_units`` directly (and also tries the
    pycparser-based AST translator for accurate signatures).

    ``target_platform`` is forwarded to ``convert_c_file`` so that the
    correct platform preset macros are defined during preprocessing.
    """
    return convert_c_file(c_path, out_dir,
                           unit_name=unit_name,
                           form_unit=form_unit,
                           included_units=included_units,
                           header_types=header_types,
                           header_defines=header_defines,
                           header_externs=header_externs,
                           target_platform=target_platform)


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
                # Skip our own temporary expanded-source files (they
                # live next to the real sources while a conversion is
                # running and must never be converted themselves).
                if f.startswith("gtk2laz_expanded_"):
                    continue
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
            c_only: bool = False,
            progress_callback=None,
            target_platform: str = "auto",
            extra_defines: Optional[list[str]] = None) -> dict:
    """Convert a GTK source tree into a Lazarus project.

    When ``c_only`` is True, the Glade form is skipped and only the C
    files are translated. Useful when the user wants pure C -> Pascal
    translation without involving the GUI definition.

    ``progress_callback`` is an optional callable invoked as
    ``progress_callback(message: str)`` at various stages of the
    conversion (e.g. "Converting coff.h…"). The GUI uses this to
    display the currently-processed file.

    ``target_platform`` selects which platform's preset macros
    (``_WIN32``, ``__linux__``, ``__APPLE__`` etc.) are defined in
    pcpp before preprocessing each .c/.h file. This controls which
    branch of every ``#ifdef`` guard is taken. ``"auto"`` uses the
    running interpreter's platform; the user can override by passing
    ``--platform=windows`` / ``linux`` / ``macos`` on the CLI.

    ``extra_defines`` is an optional list of additional macro
    definitions the user passed via ``--define`` on the CLI. Each
    entry is a string like ``"NAME=value"`` or ``"NAME"`` (which
    defines NAME to 1). These are passed straight through to pcpp
    on top of the platform preset.
    """

    def _progress(msg: str) -> None:
        if progress_callback:
            try:
                progress_callback(msg)
            except Exception:
                pass  # don't let callback errors break conversion

    # Set the module-level extra defines so every subsequent
    # ``preprocess()`` call (one per .c/.h file) picks them up. We do
    # this ONCE here at the start of the conversion so we don't have
    # to thread the list through every function in the call chain.
    try:
        from pcpp_preprocessor import set_extra_defines
        set_extra_defines(extra_defines)
    except Exception:
        pass

    proj = discover(input_dir)
    proj.output_dir = output_dir
    os.makedirs(output_dir, exist_ok=True)

    # ---- Auto-fall-back to C-only mode when there are no .glade files.
    #
    # The user might invoke ``gtk2laz convert <src> <out>`` on a C
    # source tree that has no .glade file (e.g. enet, opgui_src, or any
    # pure C library). Previously this raised ``SystemExit("No .glade
    # file found...")`` and the user had to re-run with the ``c2pas``
    # subcommand (or the ``--c-only`` flag) instead. To make the tool
    # Just Work, we silently switch to C-only mode when no .glade file
    # is present — this matches the user's mental model: ``convert``
    # does whatever the input tree needs. Users who explicitly want
    # Glade processing can still force it by passing a ``--glade``
    # argument (which would then fail loudly if the named file does
    # not exist, as before).
    if not c_only and not proj.glade_files and not glade_name:
        c_only = True
        _progress("No .glade file found in input — using C-only mode "
                  "(same as `c2pas`).")

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
        _progress(f"Converting Glade: {os.path.basename(glade_path)}")
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
        _progress(f"Converting header: {os.path.basename(h_path)}")
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
        _progress(f"Converting C source: {os.path.basename(c_path)}")
        base = os.path.splitext(os.path.basename(c_path))[0].lower()
        # ---- opgui.c handling ----
        # The previous code SKIPPED opgui.c in GUI (non-c_only) mode on
        # the assumption that "the form unit (mainform.pas) already
        # represents opgui.c".  This is wrong: opgui.c contains dozens
        # of regular (non-GUI) functions that the form unit does NOT
        # contain, so skipping opgui.c meant those functions were
        # silently dropped from the conversion.  The user observed this
        # as "opgui.pas is generated in CLI mode but not in GUI mode".
        #
        # We now ALWAYS convert opgui.c.  In GUI mode, the C-derived
        # unit is named ``opgui`` (same as the .c file), which is
        # distinct from the form unit ``mainform``.  Both units coexist
        # in the project: ``mainform`` holds the LCL form definition
        # (generated from the .glade file), and ``opgui`` holds the
        # translated C functions.  This matches how the CLI ``c2pas``
        # mode already works.
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

        # If a header with the same basename was converted (e.g. coff.h
        # for coff.c), merge its type/define/extern declarations into
        # this C file's output. Otherwise the C file's output would
        # overwrite the header's output (same unit name) and the struct/
        # enum/typedef declarations would be lost.
        h_basename = os.path.splitext(os.path.basename(c_path))[0] + ".h"
        matching_header = None
        for hr in header_results:
            if os.path.basename(hr["source"]) == h_basename:
                matching_header = hr
                break
        h_types = matching_header["header_types"] if matching_header else None
        h_defines = matching_header["header_defines"] if matching_header else None
        h_externs = matching_header["header_externs"] if matching_header else None

        result = convert_c_file_with_includes(
            c_path, output_dir,
            unit_name=base,
            form_unit=form_unit if not c_only else None,
            included_units=inc_units,
            header_types=h_types,
            header_defines=h_defines,
            header_externs=h_externs,
            target_platform=target_platform,
        )
        c_results.append(result)
        unit_names.append(result["unit_name"])

    # Generate .lpr and .lpi (skip in c_only mode - no form to anchor to)
    _progress("Generating project files…")
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
    _progress("Building conversion report…")
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

    # Also write a human-readable TODO list as a Markdown file. The
    # JSON file (todos.json) is for machine consumption (the GUI's
    # TODO tree, the ``list-todos`` subcommand); this Markdown file is
    # for the USER — they can open it in any text editor, read through
    # every TODO, click through to the source line, and check items
    # off as they fix them. Each TODO is grouped by its output .pas
    # unit so the user can work through one unit at a time.
    _write_todo_markdown(output_dir, all_todos)

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
# TODO markers file writer
# ---------------------------------------------------------------------------
def _write_todo_markdown(output_dir: str, todos: list[dict]) -> None:
    """Write a human-readable ``TODO_MARKERS.md`` file to the output
    directory.

    The file lists every TODO marker the converter produced, grouped
    by the output ``.pas`` unit. For each TODO it shows:
      - the source file and line (so the user can jump to the original
        C code in their editor)
      - the output file and line (so the user can jump to the Pascal
        code that needs fixing)
      - the reason (e.g. "unrecognised statement", "empty struct body")
      - the original C snippet (truncated to keep lines readable)
      - the enclosing function name (if known)

    The file is Markdown so it renders nicely in GitHub / GitLab /
    VS Code preview, but it is also perfectly readable as plain text
    in any editor (``notepad``, ``vim``, ``cat``, etc.).

    The file is OVERWRITTEN on each conversion so it always reflects
    the most recent run.
    """
    if not todos:
        # No TODOs at all — still write the file (with a "no TODOs"
        # message) so the user knows the conversion completed
        # successfully and there is nothing to fix.
        md_path = os.path.join(output_dir, "TODO_MARKERS.md")
        try:
            with open(md_path, "w", encoding="utf-8") as f:
                f.write("# TODO Markers\n\n")
                f.write(f"Generated by {get_version_string()}\n\n")
                f.write("No TODO markers were produced during this "
                         "conversion. All C statements were translated "
                         "successfully.\n")
        except OSError:
            pass
        return

    # Group TODOs by output file (the .pas unit they appear in).
    # This lets the user work through one unit at a time.
    by_unit: dict[str, list[dict]] = {}
    for t in todos:
        unit = t.get("output_file", "") or "(unknown unit)"
        # Use just the basename so the grouping is clean even if the
        # path is absolute or relative.
        unit_base = os.path.basename(unit)
        by_unit.setdefault(unit_base, []).append(t)

    md_path = os.path.join(output_dir, "TODO_MARKERS.md")
    try:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("# TODO Markers\n\n")
            f.write(f"Generated by {get_version_string()}\n\n")
            f.write(f"Total TODOs: **{len(todos)}** in "
                    f"**{len(by_unit)}** unit(s)\n\n")
            f.write("---\n\n")
            # Table of contents — one entry per unit
            f.write("## Units with TODOs\n\n")
            for unit in sorted(by_unit.keys()):
                count = len(by_unit[unit])
                # Anchor link (Markdown auto-generates anchors from
                # headings; we just lowercase and replace spaces).
                anchor = unit.lower().replace(" ", "-")
                f.write(f"- [{unit}](#{anchor}) — {count} TODO(s)\n")
            f.write("\n---\n\n")
            # Detailed listing per unit
            for unit in sorted(by_unit.keys()):
                f.write(f"## {unit}\n\n")
                unit_todos = by_unit[unit]
                # Sort by source line so the user can read top-to-bottom
                unit_todos.sort(key=lambda t: (t.get("source_line", 0),
                                                t.get("output_line", 0)))
                for i, t in enumerate(unit_todos, 1):
                    src_file = t.get("source_file", "?")
                    src_line = t.get("source_line", 0)
                    out_line = t.get("output_line", 0)
                    reason = t.get("reason", "")
                    func = t.get("function", "")
                    snippet = t.get("source_text", "")
                    # Truncate snippet to keep the file readable
                    if len(snippet) > 120:
                        snippet = snippet[:117] + "..."
                    # Replace newlines in snippet so it fits on one line
                    snippet = snippet.replace("\n", " ").replace("\r", "")
                    f.write(f"### {i}. {reason}")
                    if func:
                        f.write(f" (in `{func}()`)")
                    f.write("\n\n")
                    f.write(f"- **Source**: `{src_file}`:{src_line}\n")
                    if out_line:
                        f.write(f"- **Output**: `{unit}`:{out_line}\n")
                    if snippet:
                        f.write(f"- **C snippet**: `{snippet}`\n")
                    f.write("\n")
    except OSError:
        pass


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
    # Target platform for #ifdef branch selection. This is the
    # single most important knob for cross-platform C libraries: it
    # decides which branch of ``#ifdef _WIN32`` / ``#ifdef __linux__``
    # / ``#ifdef __APPLE__`` is taken in every header. ``auto`` (the
    # default) mirrors the running interpreter's platform — so on a
    # Linux build host the default is ``linux``; on a Windows build
    # host it is ``windows``. The user can override to convert code
    # for a different target platform (e.g. ``--platform=windows`` on
    # a Linux host to convert Windows-only source code like enet's
    # win32.c).
    p.add_argument("--platform", default="auto",
                   choices=["auto", "windows", "linux", "macos"],
                   help="Target platform for #ifdef branch selection "
                        "(default: auto = host platform)")
    # Extra --define options, like gcc's -D. The user can pass any
    # number of these; each one is forwarded to pcpp on top of the
    # platform preset. This is the escape hatch for unusual #ifdef
    # guards that are not covered by the platform preset (e.g.
    # ``--define HAVE_EPOLL=1`` to enable a Linux-specific code path
    # that is gated by ``#ifdef HAVE_EPOLL``).
    # Format: ``NAME`` (defines to 1), ``NAME=value`` (defines to
    # value), or ``NAME=`` (defines to empty).
    p.add_argument("--define", action="append", default=[],
                   metavar="NAME[=VALUE]",
                   help="Pre-define a macro before preprocessing "
                        "(repeatable; like gcc's -D)")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gtk2laz",
        description="Convert a GTK (C + Glade) project to a FreePascal / "
                    "Lazarus LCL project.",
    )
    parser.add_argument("--version", action="version",
                        version=get_version_string())
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
    # c2pas also accepts --platform and --define so the user can
    # cross-convert the same way as with ``convert``.
    p_c2p.add_argument("--platform", default="auto",
                       choices=["auto", "windows", "linux", "macos"],
                       help="Target platform for #ifdef branch selection "
                            "(default: auto = host platform)")
    p_c2p.add_argument("--define", action="append", default=[],
                       metavar="NAME[=VALUE]",
                       help="Pre-define a macro before preprocessing "
                            "(repeatable; like gcc's -D)")

    # gui: launch the interactive GUI
    p_gui = sub.add_parser("gui",
                            help="Launch the interactive GUI front-end")

    # list-todos: print all TODOs from a previous conversion as JSON
    p_lt = sub.add_parser("list-todos",
                           help="List all TODO markers in a converted project")
    p_lt.add_argument("output_dir", help="Converted project directory")

    # diagnose: run AST diagnostics
    sub.add_parser("diagnose",
                    help="Run AST diagnostics to verify pycparser/ply/pcpp")

    raw_argv = sys.argv[1:] if argv is None else list(argv)
    KNOWN_SUBS = {"convert", "c2pas", "gui", "list-todos", "diagnose",
                  "-h", "--help"}
    if raw_argv and raw_argv[0] not in KNOWN_SUBS \
            and not raw_argv[0].startswith("-"):
        raw_argv = ["convert"] + raw_argv

    args = parser.parse_args(raw_argv)

    # Print the version + build timestamp at the very start of every
    # CLI run (except --version which already printed it via argparse).
    # This goes to stderr so it appears in the console and is captured
    # into conversion.log by _begin_log_capture(). The user can then
    # tell at a glance which build they are running.
    if args.command in ("c2pas", "convert"):
        print(get_version_string(), file=sys.stderr)

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

    # ---- Subcommand: diagnose ----
    if args.command == "diagnose":
        import diagnose
        return diagnose.main()

    # ---- Subcommand: c2pas ----
    if args.command == "c2pas":
        if not args.input_dir or not os.path.isdir(args.input_dir):
            print(f"error: input directory not found: {args.input_dir!r}",
                  file=sys.stderr)
            return 2
        if not args.output_dir:
            print("error: output_dir is required", file=sys.stderr)
            return 2
        # Tee stderr into a conversion.log file in the output directory
        # so the user has a persistent record of every diagnostic message
        # (AST parse failures, pcpp errors, etc.). The on-screen output
        # is still printed as usual; this just adds a file copy.
        _log_path = _begin_log_capture(args.output_dir)
        try:
            result = convert(args.input_dir, args.output_dir,
                             form_unit=args.form_unit or "mainform",
                             form_class="TMainForm",
                             report_path=args.report,
                             c_only=True,
                             target_platform=getattr(args, "platform", "auto"),
                             extra_defines=getattr(args, "define", None))
        finally:
            _end_log_capture(_log_path)
            print(f"  Log:    {_log_path}")
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
        c_only_arg = getattr(args, "c_only", False)
        # Same log capture as c2pas: write ``conversion.log`` to the
        # output directory so the user has a persistent record of all
        # diagnostic messages even after the console window is closed.
        _log_path = _begin_log_capture(args.output_dir)
        try:
            result = convert(args.input_dir, args.output_dir,
                             form_unit=args.form_unit,
                             form_class=args.form_class,
                             glade_name=args.glade,
                             report_path=args.report,
                             c_only=c_only_arg,
                             target_platform=getattr(args, "platform", "auto"),
                             extra_defines=getattr(args, "define", None))
        finally:
            _end_log_capture(_log_path)
            print(f"  Log:    {_log_path}")
        # Use the c_only flag from the result dict, NOT the argument:
        # ``convert()`` may have auto-switched to C-only mode when no
        # .glade file was found in the input directory. In that case we
        # must NOT print the "Form:" line, because no Glade form was
        # processed.
        effective_c_only = result.get("c_only", c_only_arg)
        print(f"Converted {result['project_name']} -> {result['output_dir']}")
        if not effective_c_only:
            print(f"  Form:   {result['form_unit']} ({result['form_class']})")
        else:
            print(f"  Mode:   C -> Pascal (no .glade file)")
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
