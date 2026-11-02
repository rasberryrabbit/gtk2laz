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
        root.title("gtk2laz — GTK to Lazarus converter")
        root.geometry("1200x780")
        root.minsize(900, 600)

        # State
        self.input_dir = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.mode = tk.StringVar(value="full")  # full | c_only | reload
        self.form_unit = tk.StringVar(value="mainform")
        self.form_class = tk.StringVar(value="TMainForm")
        self.status_text = tk.StringVar(value="Ready.")
        self.progress = tk.IntVar(value=0)
        self.current_file = tk.StringVar(value="")  # currently-processed file

        # Background-thread result queue
        self._result_q: queue.Queue = queue.Queue()
        self._worker: Optional[threading.Thread] = None

        # All todos currently shown in the tree
        self.todos: list[dict] = []

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

        run_row = ttk.Frame(grid)
        run_row.grid(row=4, column=1, sticky=tk.W, padx=4, pady=6)
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

        vsb = ttk.Scrollbar(tree_wrap, orient=tk.VERTICAL,
                             command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_wrap, orient=tk.HORIZONTAL,
                             command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        # horizontal scrollbar below tree (separate frame)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)

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
        """Worker: run the conversion in a background thread."""
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

            result = convert(in_dir, out_dir,
                              form_unit=self.form_unit.get() or "mainform",
                              form_class=self.form_class.get() or "TMainForm",
                              c_only=c_only,
                              progress_callback=_on_progress)
            self._result_q.put(("ok", result))
        except Exception as e:  # noqa: BLE001
            self._result_q.put(("error", str(e)))

    def _poll_result_queue(self) -> None:
        try:
            while True:
                kind, payload = self._result_q.get_nowait()
                if kind == "progress":
                    # Update the current-file indicator
                    self.current_file.set(payload)
                    # Bump progress slightly so the bar looks alive
                    cur = self.progress.get()
                    if cur < 90:
                        self.progress.set(cur + 2)
                elif kind == "ok":
                    self._on_conversion_done(payload)
                else:
                    self._on_conversion_error(payload)
        except queue.Empty:
            pass
        self.root.after(150, self._poll_result_queue)

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
        # Slowly decay progress bar
        self.root.after(2000, lambda: self.progress.set(0))

    def _on_conversion_error(self, msg: str) -> None:
        self.progress.set(0)
        self.status_text.set("Conversion failed.")
        self.current_file.set("")  # clear file indicator on error
        self.run_btn.configure(state=tk.NORMAL)
        messagebox.showerror("Conversion failed", msg)

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
