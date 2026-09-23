"""
tools/batchProcessing/pickers.py

PURPOSE:
    The two GLOBAL, batch-wide pickers shown OUTSIDE the tool-
    selection checklist (see checklist.py): Land Parcel Source and
    Output Destination, packed side by side in one row, above the
    checklist (see window.py's own layout spec) -- the two framed
    boxes in this task's own UI sketch.
    Both are built from the SAME shared SourcePicker helper below
    (identical Local File / Database Table convention, differing only
    in dialog type and labels), rather than duplicated twice, per this
    codebase's own Rule of Three (Instructions Section C / G.5).

    Purely a UI-state capture in this task (see contract.py's own
    TASK 1 SCOPE note) -- neither picker's value is consumed by any
    real processing yet; that is Task 2's own batch execution engine.

    Also owns pick_db_table() / _pick_db_tables(), the shared single-
    selection modal table picker -- mirrors the SAME pattern already
    established, identically, in every one of the 11 tool files' own
    local _pick_db_tables() helper (e.g. road_surface.py). Duplicated
    here rather than imported, matching that established per-file
    convention: this helper is not currently a shared utils/ module in
    this codebase.

INPUTS:
    Each SourcePicker instance is built directly inside a caller-
    supplied container widget (window.py owns overall layout) and a
    `win` reference (the Batch Processing Toplevel itself) used ONLY as
    the parent for its own table-picker dialog -- never `root`, so
    that dialog never appears in core/tool_exclusivity.py's own
    root-children diff (see window.py's own docstring SIDE EFFECTS).

OUTPUTS:
    SourcePicker.get_mode() -> "local" | "db"
    SourcePicker.get_value() -> str | None (the selected local path or
        database table name for whichever mode is currently active)

DEPENDENCIES:
    stdlib: os, tkinter (ttk, filedialog, messagebox, Listbox).
    local: utils.window_icon (apply_icon), utils.db_gate_ui
    (disable_db_radio, attach_no_db_tooltip), utils.db_discovery
    (load_db_credentials, fetch_tables) -- three already-proven,
    shared modules reused as-is (Rule of Three).

SIDE EFFECTS:
    Builds Tkinter widgets inside the container passed to
    SourcePicker.__init__. pick_db_table() builds one additional
    tk.Toplevel, parented on `win` (see INPUTS above).
"""
import os
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox

from utils.window_icon import apply_icon
from utils.db_gate_ui import disable_db_radio, attach_no_db_tooltip
from utils.db_discovery import load_db_credentials, fetch_tables


def pick_db_table(win, on_select):
    """
    Fetches this session's DB tables (via utils.db_discovery, showing
    its own error dialogs on failure) and, if any are found, opens
    _pick_db_tables() for a single-selection modal picker. Shared
    entry point for both SourcePicker instances below -- the
    credential-loading / empty-schema handling is identical for Land
    Parcel Source and Output Destination, so it is written once here
    rather than inside SourcePicker itself (which stays a pure UI
    class, with no DB-fetch logic of its own).
    """
    creds = load_db_credentials()
    if not creds:
        return
    tables = fetch_tables(creds["schema"])
    if not tables:
        messagebox.showwarning(
            "No Tables", "No tables found in the database schema.",
            parent=win)
        return
    _pick_db_tables(win, tables, on_select)


def _pick_db_tables(parent, tables, on_select):
    """
    Single-selection modal table picker. See module docstring for why
    this is duplicated from, rather than shared with, the 11 tool
    files' own identical local helper.

    Args:
        parent: parent Tk window (the Batch Processing `win`, never the
            app root -- see module docstring SIDE EFFECTS).
        tables (list[str]): table names to list.
        on_select (callable): called with the single selected name.
    """
    picker = tk.Toplevel(parent)
    apply_icon(picker, "batchvaluation.ico")
    picker.title("Select Table")
    picker.resizable(False, False)
    picker.grab_set()

    lb = Listbox(picker, selectmode=tk.SINGLE, width=55, height=15)
    for t in tables:
        lb.insert(tk.END, t)
    lb.pack(padx=10, pady=10)

    def submit():
        sel = [lb.get(i) for i in lb.curselection()]
        if sel:
            on_select(sel[0])
            picker.destroy()

    tk.Button(picker, text="Confirm Selection", command=submit,
              width=20).pack(pady=(0, 10))


class SourcePicker:
    """
    One framed Local File / Database Table picker section -- shared
    between the global Land Parcel Source and Output Destination
    pickers, parameterized rather than duplicated twice. Both are
    packed side by side, in the SAME row, above the tool-selection
    checklist (see window.py's own layout spec) -- each SourcePicker
    keeps its own distinct title ("Land Parcel Source" / "Output
    Destination") as its own box header; there is no combined outer
    title wrapping both.
    """

    def __init__(self, container, win, db_verified, *, title,
                 local_button_text, empty_local_text, empty_db_text,
                 local_dialog):
        """
        Args:
            container: parent widget this picker's own framed box is
                packed into (window.py owns the outer layout).
            win: the Batch Processing Toplevel -- passed through only
                as the parent for pick_db_table()'s own dialog.
            db_verified: bool, gates the Database Table radio exactly
                the way every individual tool's own such radio already
                is (via utils.db_gate_ui, unchanged).
            title: this box's own heading text (e.g. "Land Parcel
                Source").
            local_button_text: label for the local-mode action button
                (e.g. "Browse…" or "Save As…").
            empty_local_text / empty_db_text: placeholder display text
                before anything is selected in that mode.
            local_dialog: zero-arg callable returning a selected local
                path, or ""/None if cancelled -- the ONE thing that
                actually differs between Land Parcel Source
                (filedialog.askopenfilename) and Output Destination
                (filedialog.asksaveasfilename); see the two builder
                functions below for the concrete callables passed in.
        """
        self._win = win
        self._empty_local_text = empty_local_text
        self._empty_db_text = empty_db_text
        self._local_dialog = local_dialog
        self._local_path = None
        self._db_table = None
        self._local_button_text = local_button_text

        self.mode = tk.StringVar(master=win, value="local")
        self._display_var = tk.StringVar(master=win, value=empty_local_text)

        outer = tk.Frame(container, relief="groove", borderwidth=2, bg="white")
        outer.pack(side="left", fill="both", expand=True,
                   padx=5, pady=(6, 4))

        tk.Label(outer, text=title, font=("Segoe UI", 9, "bold"),
                 bg="white", anchor="w").pack(fill="x", padx=8, pady=(6, 2))

        radio_row = tk.Frame(outer, bg="white")
        radio_row.pack(fill="x", padx=8)
        tk.Radiobutton(radio_row, text="Local File", variable=self.mode,
                       value="local", bg="white",
                       command=self._refresh).pack(side="left")
        db_radio = tk.Radiobutton(radio_row, text="Database Table",
                                  variable=self.mode, value="db", bg="white",
                                  command=self._refresh)
        db_radio.pack(side="left", padx=(12, 0))
        if not db_verified:
            disable_db_radio(db_radio)
            attach_no_db_tooltip(db_radio)

        action_row = tk.Frame(outer, bg="white")
        action_row.pack(fill="x", padx=8, pady=(2, 8))
        tk.Label(action_row, textvariable=self._display_var, fg="gray",
                 bg="white", anchor="w", width=38).pack(side="left")
        self._action_btn = tk.Button(action_row, text=local_button_text,
                                     width=10, command=self._on_local_click)
        self._action_btn.pack(side="left", padx=(6, 0))

    def _on_local_click(self):
        path = self._local_dialog()
        if path:
            self._local_path = path
            self._display_var.set(os.path.basename(path))

    def _on_db_click(self):
        def _on_select(name):
            self._db_table = name
            self._display_var.set(name)
        pick_db_table(self._win, _on_select)

    def _refresh(self):
        if self.mode.get() == "local":
            self._action_btn.config(text=self._local_button_text,
                                    command=self._on_local_click)
            self._display_var.set(
                os.path.basename(self._local_path) if self._local_path
                else self._empty_local_text)
        else:
            self._action_btn.config(text="Select…", command=self._on_db_click)
            self._display_var.set(
                self._db_table if self._db_table else self._empty_db_text)

    def get_mode(self):
        """Returns "local" or "db" -- whichever mode is currently
        selected."""
        return self.mode.get()

    def get_value(self):
        """Returns the currently selected local path or database table
        name for whichever mode is currently active, or None if
        nothing has been selected in that mode yet."""
        return self._local_path if self.get_mode() == "local" else self._db_table


def build_land_parcel_source_picker(container, win, db_verified):
    """
    Builds and returns the global Land Parcel Source SourcePicker.
    Local File uses filedialog.askopenfilename with the same
    shapefile/GeoPackage filetypes every individual tool's own Land
    Parcel Source section already uses (e.g. road_surface.py).
    """
    def _dialog():
        return filedialog.askopenfilename(filetypes=[
            ("Shapefiles", "*.shp"), ("GeoPackage", "*.gpkg"), ("All", "*.*")])

    return SourcePicker(
        container, win, db_verified,
        title="Land Parcel Source",
        local_button_text="Browse…",
        empty_local_text="No file selected",
        empty_db_text="No table selected",
        local_dialog=_dialog,
    )


def build_output_destination_picker(container, win, db_verified):
    """
    Builds and returns the global Output Destination SourcePicker.

    Local File uses filedialog.asksaveasfilename (folder AND filename
    together, per the Prompt's own explicit spec for this picker),
    relying on that dialog's own built-in overwrite-conflict prompt
    rather than any custom conflict-handling UI -- no existing
    project-own wrapper around asksaveasfilename was found (grepped
    the current codebase before writing this).

    Database Table branch: two-way Local File / Database Table,
    matching every individual tool's own existing Output Destination
    convention and the Land Parcel Source picker's own explicit
    two-way spec -- the Prompt's own wording for THIS specific picker
    only spelled out the local-file mechanism, so this branch mirrors
    the 11 tools' own established DB-output convention for parity (see
    this task's own Phase 1 analysis for the explicit note on that
    resolution).
    """
    def _dialog():
        return filedialog.asksaveasfilename(
            defaultextension=".gpkg",
            filetypes=[("GeoPackage", "*.gpkg"), ("All", "*.*")])

    return SourcePicker(
        container, win, db_verified,
        title="Output Destination",
        local_button_text="Save As…",
        empty_local_text="No output file selected",
        empty_db_text="No table selected",
        local_dialog=_dialog,
    )