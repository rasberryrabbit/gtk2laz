"""
gtk2laz_gui.py
==============
Tkinter front-end for the gtk2laz converter.

Features
--------
* Pick an input directory (the GTK source tree) and an output directory
  (where the Lazarus project will be written).
* Choose between three conversion modes:
    - "Full (Glade + C)"  : run the complete GTK -> Lazarus conversion.
    - "C -> Pascal only"  : skip the Glade form; just translate C files.
    - "Reload TODOs"      : re-scan an already-converted output directory
                            for TODO markers and refresh the list.
* Run the conversion in a background thread so the UI stays responsive.
* Show every TODO marker in a tree view. Each row shows:
      file:line   function   reason   snippet
* Click a row (or press Enter) to open the **source viewer** panel that
  displays the original C file with the offending line highlighted.
  The corresponding Pascal output line is shown beneath it.
* Double-click a row to launch the system editor on the source file
  (``$EDITOR`` on Linux/macOS, ``notepad`` on Windows).

The GUI module is imported lazily by ``gtk2laz.py`` so that the CLI
builds (PyInstaller) do not require tkinter to be importable at startup.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _utf8_open(path: str):
    return open(path, "r", encoding="utf-8", errors="replace")


def _read_lines(path: str) -> list[str]:
    try:
        with _utf8_open(path) as f:
            return f.readlines()
    except OSError:
        return []


def _system_open(path: str) -> None:
    """Open ``path`` in the user's preferred editor."""
    editor = (os.environ.get("EDITOR")
              or os.environ.get("VISUAL"))
    try:
        if editor:
            subprocess.Popen([editor, path])
        elif sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------
class Gtk2LazApp:
    """Controller object that owns the Tk root and all widgets."""

    def __init__(self, root: tk.Tk):
        self.root = root
        # Show the version + build timestamp in the window title bar
        # so the user can tell at a glance which build they are
        # running. The ``get_version_string()`` function lives in
        # gtk2laz.py and reads ``_build_timestamp.txt`` (written by
        # the build scripts) or falls back to gtk2laz.py's mtime.
        try:
            from gtk2laz import get_version_string
            _version_str = get_version_string()
        except Exception:
            _version_str = "gtk2laz (version unknown)"
        root.title(f"gtk2laz — GTK to Lazarus converter  [{_version_str}]")
        root.geometry("1200x900")
        root.minsize(900, 700)

        # State
        self.input_dir = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.mode = tk.StringVar(value="full")  # full | c_only | reload
        self.form_unit = tk.StringVar(value="mainform")
        self.form_class = tk.StringVar(value="TMainForm")
        # Target platform for #ifdef branch selection. ``"auto"``
        # mirrors the running interpreter's platform. The user can
        # override via the dropdown to convert Windows-only source
        # code on a Linux host (or vice versa).
        self.target_platform = tk.StringVar(value="auto")
        # Extra ``--define`` macros the user wants to predefine. We
        # store these as a single comma-separated string in the entry
        # field and split on commas before passing them to
        # ``convert()``. The user types e.g.
        # ``HAVE_EPOLL=1,DEBUG=1`` to define both macros.
        self.extra_defines_str = tk.StringVar(value="")
        self.status_text = tk.StringVar(value="Ready.")
        self.progress = tk.IntVar(value=0)
        self.current_file = tk.StringVar(value="")  # currently-processed file

        # Background-thread result queue
        self._result_q: queue.Queue = queue.Queue()
        self._worker: Optional[threading.Thread] = None

        # All todos currently shown in the tree
        self.todos: list[dict] = []

        # Log lines collected from stderr (see _run_conversion_thread).
        # We append to this list as the worker thread prints to its
        # captured stderr; the UI polls the result queue for new lines.
        self._log_lines: list[str] = []

        self._build_ui()
        self._poll_result_queue()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        # ---- Top: input/output selection ----
        top = ttk.LabelFrame(self.root, text="Conversion")
        top.pack(side=tk.TOP, fill=tk.X, padx=8, pady=6)

        grid = ttk.Frame(top)
        grid.pack(fill=tk.X, padx=8, pady=8)

        ttk.Label(grid, text="Input dir:").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(grid, textvariable=self.input_dir, width=70).grid(
            row=0, column=1, sticky=tk.EW, padx=4)
        ttk.Button(grid, text="Browse…",
                   command=self._browse_input).grid(row=0, column=2)

        ttk.Label(grid, text="Output dir:").grid(row=1, column=0, sticky=tk.W)
        ttk.Entry(grid, textvariable=self.output_dir, width=70).grid(
            row=1, column=1, sticky=tk.EW, padx=4)
        ttk.Button(grid, text="Browse…",
                   command=self._browse_output).grid(row=1, column=2)

        ttk.Label(grid, text="Mode:").grid(row=2, column=0, sticky=tk.W)
        mode_row = ttk.Frame(grid)
        mode_row.grid(row=2, column=1, sticky=tk.W, padx=4, pady=4)
        ttk.Radiobutton(mode_row, text="Full (Glade + C)",
                        variable=self.mode, value="full").pack(side=tk.LEFT)
        ttk.Radiobutton(mode_row, text="C -> Pascal only",
                        variable=self.mode, value="c_only").pack(side=tk.LEFT)
        ttk.Radiobutton(mode_row, text="Reload TODOs from output",
                        variable=self.mode, value="reload").pack(side=tk.LEFT)

        opt_row = ttk.Frame(grid)
        opt_row.grid(row=3, column=1, sticky=tk.W, padx=4, pady=2)
        ttk.Label(opt_row, text="Form unit:").pack(side=tk.LEFT)
        ttk.Entry(opt_row, textvariable=self.form_unit, width=14).pack(
            side=tk.LEFT, padx=(2, 12))
        ttk.Label(opt_row, text="Form class:").pack(side=tk.LEFT)
        ttk.Entry(opt_row, textvariable=self.form_class, width=14).pack(
            side=tk.LEFT, padx=2)

        # Platform + defines row — lets the user select which platform's
        # preset macros (_WIN32 / __linux__ / __APPLE__) are defined
        # during preprocessing, and add extra --define macros on top.
        plat_row = ttk.Frame(grid)
        plat_row.grid(row=4, column=1, sticky=tk.W, padx=4, pady=2)
        ttk.Label(plat_row, text="Platform:").pack(side=tk.LEFT)
        # ttk.Combobox gives a dropdown the user can also type into,
        # but we restrict the choices to the four valid values.
        ttk.Combobox(plat_row, textvariable=self.target_platform,
                     values=["auto", "windows", "linux", "macos"],
                     state="readonly", width=10).pack(
            side=tk.LEFT, padx=(2, 12))
        ttk.Label(plat_row, text="Defines:").pack(side=tk.LEFT)
        ttk.Entry(plat_row, textvariable=self.extra_defines_str,
                  width=30).pack(side=tk.LEFT, padx=2)
        # Tooltip-like hint (we can't easily do real tooltips in plain
        # Tkinter, so we use a small grey label).
        ttk.Label(plat_row,
                  text="(comma-separated, e.g. HAVE_EPOLL=1,DEBUG)",
                  foreground="#888888").pack(side=tk.LEFT, padx=4)

        run_row = ttk.Frame(grid)
        run_row.grid(row=5, column=1, sticky=tk.W, padx=4, pady=6)
        self.run_btn = ttk.Button(run_row, text="Run conversion",
                                   command=self._start_conversion)
        self.run_btn.pack(side=tk.LEFT)
        ttk.Button(run_row, text="Open output in file manager",
                   command=self._open_output).pack(side=tk.LEFT, padx=8)

        grid.columnconfigure(1, weight=1)

        # ---- Middle: TODO list (left) + source viewer (right) ----
        middle = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        middle.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=4)

        # Left pane: TODO tree
        todo_frame = ttk.LabelFrame(middle, text="TODO markers")
        tree_wrap = ttk.Frame(todo_frame)
        tree_wrap.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        columns = ("location", "function", "reason", "snippet")
        # Use grid layout so the vertical scrollbar ALWAYS spans the
        # full height of the tree (with ``rowconfigure(weight=1)`` on
        # the tree's row) and the horizontal scrollbar ALWAYS spans the
        # full width below it. The previous ``pack`` layout had a
        # subtle bug where, when the tree shrank vertically (e.g.
        # because the window was resized), the vertical scrollbar
        # stayed at the old height and ended up either too short or
        # hanging outside the tree — making it look like there was no
        # vertical scrollbar at all.
        self.tree = ttk.Treeview(tree_wrap, columns=columns,
                                  show="headings", selectmode="browse")
        self.tree.heading("location", text="File:Line")
        self.tree.heading("function", text="Function")
        self.tree.heading("reason", text="Reason")
        self.tree.heading("snippet", text="C snippet")
        self.tree.column("location", width=180, anchor=tk.W)
        self.tree.column("function", width=120, anchor=tk.W)
        self.tree.column("reason", width=200, anchor=tk.W)
        self.tree.column("snippet", width=400, anchor=tk.W)

        # Vertical scrollbar — placed in column 1, spanning row 0 only
        # (same row as the tree). Always visible because the grid row
        # has weight=1, so it stretches with the tree.
        vsb = ttk.Scrollbar(tree_wrap, orient=tk.VERTICAL,
                             command=self.tree.yview)
        # Horizontal scrollbar — placed in row 1 (below the tree),
        # spanning column 0 only (so it doesn't overlap the vsb).
        hsb = ttk.Scrollbar(tree_wrap, orient=tk.HORIZONTAL,
                             command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        # Grid: tree in (0,0), vsb in (0,1), hsb in (1,0)
        self.tree.grid(row=0, column=0, sticky=tk.NSEW)
        vsb.grid(row=0, column=1, sticky=tk.NS)
        hsb.grid(row=1, column=0, sticky=tk.EW)
        tree_wrap.rowconfigure(0, weight=1)
        tree_wrap.columnconfigure(0, weight=1)
        # Row 1 (horizontal scrollbar) should NOT grow vertically.
        tree_wrap.rowconfigure(1, weight=0)
        # Column 1 (vertical scrollbar) should NOT grow horizontally.
        tree_wrap.columnconfigure(1, weight=0)

        self.tree.bind("<<TreeviewSelect>>", self._on_todo_selected)
        self.tree.bind("<Double-1>", self._on_todo_double_click)
        self.tree.bind("<Return>", self._on_todo_double_click)

        # Filter row
        filt = ttk.Frame(todo_frame)
        filt.pack(fill=tk.X, padx=4, pady=4)
        ttk.Label(filt, text="Filter:").pack(side=tk.LEFT)
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", self._apply_filter)
        ttk.Entry(filt, textvariable=self.filter_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        ttk.Button(filt, text="Open source in editor",
                   command=self._open_selected_source).pack(side=tk.RIGHT)

        middle.add(todo_frame, weight=2)

        # Right pane: source viewer
        viewer_frame = ttk.LabelFrame(middle, text="Source viewer")
        viewer_wrap = ttk.Frame(viewer_frame)
        viewer_wrap.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.src_label = ttk.Label(viewer_wrap,
                                    text="(select a TODO to view source)",
                                    anchor=tk.W, font=("TkFixedFont", 10))
        self.src_label.pack(fill=tk.X, anchor=tk.W)

        # Inner frame for the text widget + scrollbars (uses grid)
        text_wrap = ttk.Frame(viewer_wrap)
        text_wrap.pack(fill=tk.BOTH, expand=True)

        self.src_text = tk.Text(text_wrap, wrap=tk.NONE,
                                 font=("TkFixedFont", 10),
                                 state=tk.DISABLED,
                                 background="#fafafa")
        src_y = ttk.Scrollbar(text_wrap, orient=tk.VERTICAL,
                               command=self.src_text.yview)
        src_x = ttk.Scrollbar(text_wrap, orient=tk.HORIZONTAL,
                               command=self.src_text.xview)
        self.src_text.configure(yscrollcommand=src_y.set,
                                 xscrollcommand=src_x.set)
        self.src_text.grid(row=0, column=0, sticky=tk.NSEW)
        src_y.grid(row=0, column=1, sticky=tk.NS)
        src_x.grid(row=1, column=0, sticky=tk.EW)
        text_wrap.columnconfigure(0, weight=1)
        text_wrap.rowconfigure(0, weight=1)

        # Highlight tags
        self.src_text.tag_configure("highlight",
                                     background="#ffe08a",
                                     foreground="#000000")
        self.src_text.tag_configure("line_num",
                                     foreground="#888888",
                                     background="#f0f0f0")

        middle.add(viewer_frame, weight=3)

        # ---- Diagnostics log (bottom, collapsible-ish) ----
        # Captures stderr output from pcpp / pycparser / AST translator
        # so the user can see WHY conversion produced many TODOs (e.g.
        # "pcpp preprocessing failed for compress.c: PermissionError: ..."
        # on Windows when ply cannot write its parser tables inside the
        # read-only PyInstaller _MEIPASS bundle).
        log_frame = ttk.LabelFrame(self.root, text="Diagnostics log")
        log_frame.pack(side=tk.BOTTOM, fill=tk.BOTH, expand=False,
                       padx=8, pady=(4, 2))
        log_wrap = ttk.Frame(log_frame)
        log_wrap.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.log_text = tk.Text(log_wrap, wrap=tk.NONE, height=8,
                                font=("TkFixedFont", 9),
                                state=tk.DISABLED,
                                background="#1e1e1e", foreground="#d4d4d4",
                                insertbackground="#d4d4d4")
        log_y = ttk.Scrollbar(log_wrap, orient=tk.VERTICAL,
                              command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_y.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_y.pack(side=tk.RIGHT, fill=tk.Y)

        # Tag colors for the dark log panel
        self.log_text.tag_configure("error",
                                    foreground="#ff6b6b",
                                    background="#1e1e1e")
        self.log_text.tag_configure("warn",
                                    foreground="#ffd866",
                                    background="#1e1e1e")
        self.log_text.tag_configure("info",
                                    foreground="#78dce8",
                                    background="#1e1e1e")
        self.log_text.tag_configure("ok",
                                    foreground="#a9dc76",
                                    background="#1e1e1e")
        # Progress lines: dimmer blue-grey so they don't drown out
        # actual errors/warnings, but still readable. This is the
        # colour used for "[progress] Converting coff.h…" etc. —
        # the trail of which files were processed during conversion.
        self.log_text.tag_configure("progress",
                                    foreground="#6a9955",
                                    background="#1e1e1e")

        # ---- Bottom: status + current file + progress ----
        bottom = ttk.Frame(self.root)
        bottom.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=4)
        # Status text (left)
        ttk.Label(bottom, textvariable=self.status_text,
                  anchor=tk.W).pack(side=tk.LEFT)
        # Current file indicator (center, highlighted)
        self.file_label = ttk.Label(bottom, textvariable=self.current_file,
                                     anchor=tk.W, foreground="#0066cc",
                                     font=("TkDefaultFont", 9, "bold"))
        self.file_label.pack(side=tk.LEFT, padx=12, fill=tk.X, expand=True)
        # Progress bar (right)
        self.progress_bar = ttk.Progressbar(bottom, variable=self.progress,
                                             maximum=100, length=300)
        self.progress_bar.pack(side=tk.RIGHT, padx=8)

    # ------------------------------------------------------------------
    # Browse handlers
    # ------------------------------------------------------------------
    def _browse_input(self) -> None:
        d = filedialog.askdirectory(title="Select GTK source directory")
        if d:
            self.input_dir.set(d)
            # Default output dir = sibling "<input>_lazarus"
            base = os.path.basename(d.rstrip(os.sep))
            if base and not self.output_dir.get():
                self.output_dir.set(
                    os.path.join(os.path.dirname(d.rstrip(os.sep)),
                                 base + "_lazarus"))

    def _browse_output(self) -> None:
        d = filedialog.askdirectory(title="Select output directory")
        if d:
            self.output_dir.set(d)

    def _open_output(self) -> None:
        d = self.output_dir.get()
        if d and os.path.isdir(d):
            _system_open(d)
        else:
            messagebox.showwarning("Open output",
                                   "Output directory does not exist yet.")

    # ------------------------------------------------------------------
    # Conversion
    # ------------------------------------------------------------------
    def _start_conversion(self) -> None:
        in_dir = self.input_dir.get().strip()
        out_dir = self.output_dir.get().strip()
        mode = self.mode.get()

        if mode != "reload":
            if not in_dir or not os.path.isdir(in_dir):
                messagebox.showerror("Input directory",
                                     "Please choose a valid input directory.")
                return
            if not out_dir:
                messagebox.showerror("Output directory",
                                     "Please choose an output directory.")
                return
            # Warn the user if the input and output directories are the
            # same. Writing the converted .pas files, conversion.log
            # and CONVERSION_REPORT.md into the source tree is usually
            # a mistake — it pollutes the C source directory with
            # Pascal output that the user then has to clean up by
            # hand, and makes it easy to accidentally re-convert the
            # .pas files on a second run. We allow it (the user may
            # have a good reason) but warn loudly.
            try:
                _in_abs = os.path.abspath(in_dir)
                _out_abs = os.path.abspath(out_dir)
                if os.path.normcase(_in_abs) == os.path.normcase(_out_abs):
                    if not messagebox.askyesno(
                            "Input == Output",
                            "The input and output directories are the "
                            "same.\n\n"
                            "This will write .pas files, conversion.log, "
                            "and CONVERSION_REPORT.md directly into your "
                            "C source directory, which is usually NOT "
                            "what you want (the converted files will "
                            "mix with the originals).\n\n"
                            "Do you want to continue anyway?"):
                        return
            except Exception:
                pass  # don't let the comparison crash conversion
        else:
            if not out_dir or not os.path.isdir(out_dir):
                messagebox.showerror("Output directory",
                                     "Please choose a valid converted "
                                     "output directory to reload.")
                return

        # Disable button, reset progress
        self.run_btn.configure(state=tk.DISABLED)
        self.progress.set(5)
        self.status_text.set("Working…")
        self.current_file.set("")  # clear file indicator
        # Clear current tree
        self.tree.delete(*self.tree.get_children())
        self.todos = []

        # Clear the diagnostics log panel so the user sees only the
        # output of THIS conversion run (not the accumulated output of
        # previous runs).
        if hasattr(self, "log_text"):
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.delete("1.0", tk.END)
            self.log_text.configure(state=tk.DISABLED)
            # Print an immediate "starting" line so the log panel is
            # never empty while the conversion is in progress. Without
            # this, there's a brief window where the user clicks "Run
            # conversion" and nothing happens in the log until the
            # first progress callback fires (which can be a second or
            # two later for large projects).
            self._append_log_line(
                f"[info] Starting conversion: mode={mode} "
                f"input={in_dir} output={out_dir}")

        # Make gtk2laz modules importable when frozen.
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)

        self._worker = threading.Thread(
            target=self._run_conversion_thread,
            args=(mode, in_dir, out_dir),
            daemon=True,
        )
        self._worker.start()

    def _run_conversion_thread(self, mode: str,
                                in_dir: str, out_dir: str) -> None:
        """Worker: run the conversion in a background thread.

        We redirect ``sys.stderr`` to a queue-feeding wrapper for the
        duration of the conversion so that diagnostic messages emitted
        by pcpp / pycparser / the AST translator (e.g.
        ``"gtk2laz: pcpp preprocessing failed for compress.c:
        PermissionError: ..."``) appear in the GUI's diagnostics log
        panel instead of being silently dropped (which is what happened
        on Windows — the user saw ~1100 TODOs with no clue why).
        """
        # Capture stderr from the worker thread.
        # We use a per-thread proxy that forwards writes to the result
        # queue as ("log", line, level) tuples; the UI thread drains the
        # queue and appends to the diagnostics log.
        import io
        import re

        class _QueueWriter(io.TextIOBase):
            """Writes text lines to the result queue as log events."""

            def __init__(self, q):
                self._q = q
                self._buf = ""

            def write(self, s):
                if not s:
                    return 0
                self._buf += s
                # Split on newlines; each complete line becomes a log
                # event. Trailing partial line stays in the buffer.
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    self._q.put(("log", line.rstrip()))
                return len(s)

            def flush(self):
                if self._buf:
                    self._q.put(("log", self._buf.rstrip()))
                    self._buf = ""

        old_stderr = sys.stderr
        sys.stderr = _QueueWriter(self._result_q)
        try:
            if mode == "reload":
                from gtk2laz import scan_todos_in_project
                todos = scan_todos_in_project(out_dir)
                self._result_q.put(("ok", {"todos": todos,
                                            "output_dir": out_dir,
                                            "c_only": True}))
                return

            from gtk2laz import convert
            c_only = (mode == "c_only")

            # Progress callback: push file messages to the result queue
            # so the main thread can update the GUI.
            def _on_progress(msg: str) -> None:
                self._result_q.put(("progress", msg))

            # Read the platform selection from the Combobox and the
            # defines from the Entry field. The defines string is
            # comma-separated; we split on commas and strip whitespace.
            # An empty string yields no defines.
            _platform = self.target_platform.get() or "auto"
            _defines_str = self.extra_defines_str.get() or ""
            _extra_defines = [
                d.strip() for d in _defines_str.split(",") if d.strip()
            ]

            # Emit a "start" log line so the user can see something is
            # happening even before any progress callback fires.
            # Include the version + build timestamp so the log file
            # records exactly which build produced this conversion.
            try:
                from gtk2laz import get_version_string as _gvs
                _vstr = _gvs()
            except Exception:
                _vstr = "gtk2laz (version unknown)"
            print(f"[info] Starting conversion: version={_vstr} "
                  f"mode={mode} "
                  f"platform={_platform} defines={_extra_defines} "
                  f"input={in_dir} output={out_dir}")

            result = convert(in_dir, out_dir,
                              form_unit=self.form_unit.get() or "mainform",
                              form_class=self.form_class.get() or "TMainForm",
                              c_only=c_only,
                              progress_callback=_on_progress,
                              target_platform=_platform,
                              extra_defines=_extra_defines)
            self._result_q.put(("ok", result))
        except Exception as e:  # noqa: BLE001
            # Print the full traceback to our captured stderr so the
            # user can see the underlying error in the log panel.
            # (sys.stderr is still our _QueueWriter at this point.)
            import traceback
            traceback.print_exc()
            self._result_q.put(("error", str(e)))
        finally:
            # Flush any pending stderr output and restore the original.
            try:
                sys.stderr.flush()
            except Exception:
                pass
            sys.stderr = old_stderr
            # Mark end-of-log so the UI can do any post-processing.
            self._result_q.put(("log_end", None))

    def _poll_result_queue(self) -> None:
        try:
            while True:
                kind, payload = self._result_q.get_nowait()
                if kind == "progress":
                    # Update the current-file indicator (status bar)
                    self.current_file.set(payload)
                    # ALSO append the progress message to the
                    # diagnostics log so the user has a visible trail
                    # of which files were processed (and in what order).
                    # Without this, the log panel only shows stderr
                    # output, so when conversion runs cleanly the log
                    # appears empty and the user has no feedback that
                    # anything is happening at all.
                    self._append_log_line(f"[progress] {payload}")
                    # Bump progress slightly so the bar looks alive
                    cur = self.progress.get()
                    if cur < 90:
                        self.progress.set(cur + 2)
                elif kind == "log":
                    # Append a captured stderr line to the diagnostics
                    # log panel.
                    self._append_log_line(payload)
                elif kind == "log_end":
                    # Worker thread finished emitting log output.
                    # We could add a visual marker here; for now we just
                    # ignore it (the panel scrolls automatically).
                    pass
                elif kind == "ok":
                    self._on_conversion_done(payload)
                elif kind == "error":
                    self._on_conversion_error(payload)
        except queue.Empty:
            pass
        self.root.after(150, self._poll_result_queue)

    def _append_log_line(self, line: str) -> None:
        """Append one captured stderr line to the diagnostics log panel,
        tagging it by severity so it gets the right colour.

        Also appends the line to ``self._log_lines`` so it can be
        written out to ``conversion.log`` in the output directory when
        the conversion finishes. Without this in-memory trail, the
        log panel's text widget would be the only place the log
        exists — and the user cannot copy from a Tk ``Text`` widget
        that is configured as ``state=tk.DISABLED`` (the standard
        keyboard shortcuts are intercepted).
        """
        if not hasattr(self, "log_text"):
            return
        # Keep the in-memory trail so we can write it to a file later.
        # We keep the timestamp prefix too so the saved file has the
        # same content the user saw in the panel.
        self._log_lines.append(line)
        # Classify the line by prefix so we can colour it.
        # Recognised prefixes (emitted by pcpp_preprocessor.py and
        # ast_translator.py):
        #   "gtk2laz: pcpp preprocessing failed ..." -> error
        #   "gtk2laz: pcpp produced no output ..."  -> warn
        #   "gtk2laz: pcpp preprocess() raised ..." -> error
        #   "[info] ..."                              -> info
        #   "[progress] ..."                          -> progress
        #   "[ok] ..."                                 -> ok
        #   "[error] ..."                              -> error
        #   "Traceback (most recent call last):"    -> error
        #   Lines beginning with whitespace+File/line are traceback
        #   continuation lines -> error
        tag = "info"
        if line.startswith("gtk2laz:"):
            if "failed" in line or "raised" in line or "error" in line.lower():
                tag = "error"
            elif "no output" in line or "fall" in line.lower():
                tag = "warn"
            else:
                tag = "info"
        elif line.startswith("Traceback"):
            tag = "error"
        elif line.lstrip().startswith("File ") \
                or "Error" in line or "Exception" in line:
            tag = "error"
        elif line.startswith("[info]"):
            tag = "info"
        elif line.startswith("[progress]"):
            # Progress messages from the worker thread — these tell the
            # user which file is being processed. We use a distinct
            # colour (light blue) so they are visually separable from
            # informational / error lines.
            tag = "progress"
        elif line.startswith("[ok]"):
            tag = "ok"
        elif line.startswith("[error]"):
            tag = "error"
        elif "warning" in line.lower():
            tag = "warn"
        elif "ok" in line.lower() and "fail" not in line.lower():
            tag = "ok"

        self.log_text.configure(state=tk.NORMAL)
        try:
            self.log_text.insert(tk.END, line + "\n", tag)
            # Auto-scroll to bottom
            self.log_text.see(tk.END)
        finally:
            self.log_text.configure(state=tk.DISABLED)

    def _on_conversion_done(self, result: dict) -> None:
        self.progress.set(100)
        todos = result.get("todos", [])
        if not todos and result.get("output_dir"):
            # Fallback: scan output for TODO markers.
            from gtk2laz import scan_todos_in_project
            todos = scan_todos_in_project(result["output_dir"])
        self.todos = todos
        self._populate_tree(todos)
        out_dir = result.get("output_dir", "")
        c_only = result.get("c_only", False)
        n_units = len(result.get("units", []))
        msg = (f"Done — {len(todos)} TODO(s) in {n_units} unit(s). "
               f"Output: {out_dir}"
               + ("  (C-only mode)" if c_only else ""))
        self.status_text.set(msg)
        self.current_file.set("")  # clear file indicator on completion
        self.run_btn.configure(state=tk.NORMAL)
        # Also log the completion line so the user can see in the log
        # panel what the final state was (TODO count + output dir).
        self._append_log_line(
            f"[ok] {msg}")
        # Save the conversion log to a file in the output directory so
        # the user can copy/paste from it (the on-screen log panel is a
        # disabled Tk Text widget, which blocks Ctrl+C in many
        # environments). The log file always has the same name
        # (``conversion.log``) so the user knows where to find it.
        # We also save it in the temp output dir if conversion failed
        # before writing any files (so the log is never lost).
        self._save_conversion_log(out_dir)
        # Slowly decay progress bar
        self.root.after(2000, lambda: self.progress.set(0))

    def _on_conversion_error(self, msg: str) -> None:
        self.progress.set(0)
        self.status_text.set("Conversion failed.")
        self.current_file.set("")  # clear file indicator on error
        self.run_btn.configure(state=tk.NORMAL)
        # Log the error too so the user has a record of it in the log
        # panel (alongside any stderr/traceback lines that were
        # captured during the conversion).
        self._append_log_line(f"[error] Conversion failed: {msg}")
        # Even on error, save the conversion log so the user can
        # inspect what happened. The output_dir might not be set yet
        # (conversion may have failed before any files were written),
        # so we fall back to the input directory or a temp location.
        out_dir = self.output_dir.get().strip()
        if not out_dir:
            out_dir = os.path.dirname(self.input_dir.get().strip()) or "."
        self._save_conversion_log(out_dir)
        messagebox.showerror("Conversion failed", msg)

    def _save_conversion_log(self, out_dir: str) -> None:
        """Write the accumulated diagnostics log (``self._log_lines``)
        to ``<out_dir>/conversion.log``. The file is overwritten on
        each conversion so it always reflects the most recent run.

        The log contains every line that appeared in the on-screen
        diagnostics log panel, in order: progress messages ("[progress]
        Converting coff.h…"), info lines ("[info] Starting
        conversion…"), error messages ("gtk2laz: AST parse failed for
        compress.c …"), and the final "[ok] Done — N TODO(s)…" line.

        The user can open this file in any text editor and copy/paste
        freely, which is not possible from the disabled Tk Text widget
        in the GUI.
        """
        if not out_dir:
            return
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception:
            return
        log_path = os.path.join(out_dir, "conversion.log")
        try:
            # Write with a small header so the file is self-documenting
            # when the user opens it weeks later.
            import datetime as _dt
            header = (
                f"gtk2laz conversion log\n"
                f"Date: {_dt.datetime.now().isoformat(timespec='seconds')}\n"
                f"Output directory: {out_dir}\n"
                f"Lines: {len(self._log_lines)}\n"
                f"{'=' * 60}\n"
            )
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(header)
                for line in self._log_lines:
                    f.write(line + "\n")
            # Tell the user where the log was saved so they can find
            # it without having to look around.
            self._append_log_line(
                f"[info] Conversion log saved to: {log_path}")
        except Exception as _e:
            # Don't let a failure to save the log mask the actual
            # conversion result — just note it in the log panel.
            self._append_log_line(
                f"[warn] Could not save conversion log to {log_path}: "
                f"{type(_e).__name__}: {_e}")

    # ------------------------------------------------------------------
    # TODO tree
    # ------------------------------------------------------------------
    def _populate_tree(self, todos: list[dict]) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, t in enumerate(todos):
            src_file = t.get("source_file") or t.get("output_file") or ""
            src_line = t.get("source_line", 0)
            short = os.path.basename(src_file) if src_file else "?"
            location = f"{short}:{src_line}" if src_line else short
            func = t.get("function", "") or ""
            reason = t.get("reason", "") or ""
            snippet = (t.get("source_text", "") or "")[:200]
            self.tree.insert("", tk.END, iid=str(i),
                              values=(location, func, reason, snippet))

    def _apply_filter(self, *_args) -> None:
        needle = self.filter_var.get().lower().strip()
        for iid in self.tree.get_children():
            values = self.tree.item(iid, "values")
            joined = " ".join(str(v) for v in values).lower()
            visible = (needle in joined) if needle else True
            try:
                self.tree.reattach(iid, "", tk.END) if visible else \
                    self.tree.detach(iid)
            except tk.TclError:
                pass

    def _selected_todo(self) -> Optional[dict]:
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            idx = int(sel[0])
        except ValueError:
            return None
        if 0 <= idx < len(self.todos):
            return self.todos[idx]
        return None

    def _on_todo_selected(self, _event=None) -> None:
        todo = self._selected_todo()
        if not todo:
            return
        self._show_source(todo)

    def _on_todo_double_click(self, _event=None) -> None:
        todo = self._selected_todo()
        if not todo:
            return
        src_file = todo.get("source_file") or ""
        if src_file and os.path.isfile(src_file):
            _system_open(src_file)
        else:
            out_file = todo.get("output_file") or ""
            if out_file and os.path.isfile(out_file):
                _system_open(out_file)

    def _open_selected_source(self) -> None:
        todo = self._selected_todo()
        if not todo:
            messagebox.showinfo("Open source",
                                "Select a TODO from the list first.")
            return
        src_file = todo.get("source_file") or todo.get("output_file") or ""
        if src_file and os.path.isfile(src_file):
            _system_open(src_file)
        else:
            messagebox.showwarning("Open source",
                                    f"File not found: {src_file}")

    # ------------------------------------------------------------------
    # Source viewer
    # ------------------------------------------------------------------
    def _show_source(self, todo: dict) -> None:
        src_file = todo.get("source_file") or ""
        src_line = todo.get("source_line", 0) or 0
        out_file = todo.get("output_file") or ""
        out_line = todo.get("output_line", 0) or 0
        snippet = todo.get("source_text", "") or ""
        reason = todo.get("reason", "") or ""
        func = todo.get("function", "") or ""

        # Update header label
        if src_file:
            label = (f"Source: {src_file}:{src_line}"
                     f"   |   Pascal: {os.path.basename(out_file)}:{out_line}"
                     f"   |   function: {func or '(unknown)'}"
                     f"   |   reason: {reason}")
        else:
            label = (f"Pascal: {out_file}:{out_line}"
                     f"   |   reason: {reason}"
                     f"   |   snippet: {snippet[:80]}")
        self.src_label.configure(text=label)

        # Clear text
        self.src_text.configure(state=tk.NORMAL)
        self.src_text.delete("1.0", tk.END)

        # If we have a real C file, display it with line numbers and
        # highlight the offending line.
        if src_file and os.path.isfile(src_file):
            lines = _read_lines(src_file)
            # Show a window of ±40 lines around the target if the file is
            # very large; otherwise show the whole file.
            target = max(1, src_line)
            if len(lines) > 200:
                start = max(1, target - 40)
                end = min(len(lines), target + 40)
            else:
                start, end = 1, len(lines)
            for i in range(start, end + 1):
                line_text = lines[i - 1].rstrip("\n")
                # Line number gutter
                self.src_text.insert(tk.END, f"{i:5d} ", "line_num")
                if i == target:
                    self.src_text.insert(tk.END, line_text + "\n",
                                          "highlight")
                else:
                    self.src_text.insert(tk.END, line_text + "\n")
            # Scroll the highlight into view
            self.src_text.see(f"{target + (target - start) + 1}.0")
            # Try a more precise see
            try:
                # Find the line in the displayed text by counting
                self.src_text.tag_remove("highlight", "1.0", tk.END)
                # Re-apply: we re-inserted with line numbers, so the
                # highlighted line is at display-row (target - start + 1).
                row = target - start + 1
                self.src_text.tag_add("highlight", f"{row}.0", f"{row}.end")
                self.src_text.see(f"{row}.0")
            except tk.TclError:
                pass
        else:
            # Just show the snippet and the output line context.
            self.src_text.insert(tk.END,
                                  "Original C source file not available.\n\n")
            self.src_text.insert(tk.END, f"Reason: {reason}\n")
            self.src_text.insert(tk.END, f"Function: {func}\n")
            self.src_text.insert(tk.END, f"Snippet:\n  {snippet}\n\n")
            if out_file and os.path.isfile(out_file):
                self.src_text.insert(tk.END,
                                      f"Pascal output: {out_file}:{out_line}\n")
                out_lines = _read_lines(out_file)
                lo = max(1, out_line - 10)
                hi = min(len(out_lines), out_line + 10)
                for i in range(lo, hi + 1):
                    self.src_text.insert(tk.END, f"{i:5d} ", "line_num")
                    text = out_lines[i - 1].rstrip("\n")
                    if i == out_line:
                        self.src_text.insert(tk.END, text + "\n",
                                              "highlight")
                    else:
                        self.src_text.insert(tk.END, text + "\n")

        self.src_text.configure(state=tk.DISABLED)


# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------
def launch_gui() -> None:
    """Create the Tk root and start the main loop."""
    root = tk.Tk()
    # Try to apply a slightly nicer theme on Windows / macOS.
    try:
        style = ttk.Style()
        if sys.platform.startswith("win"):
            style.theme_use("vista")
        elif sys.platform == "darwin":
            style.theme_use("aqua")
        else:
            # clam is the most portable ttk theme on Linux
            style.theme_use("clam")
    except tk.TclError:
        pass

    Gtk2LazApp(root)
    root.mainloop()


if __name__ == "__main__":
    launch_gui()
