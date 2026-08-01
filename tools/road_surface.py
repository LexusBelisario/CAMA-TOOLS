import os
import math
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox
import geopandas as gpd
from shapely.geometry import Point
import subprocess
import json
from sqlalchemy import create_engine, inspect, text

# ============================
# FORCE WINDOWS APP ICON
# ============================
import ctypes
import sys

def set_app_user_model_id():
    appid = u"BLGF.CAMA.Tools.2025"
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(appid)

set_app_user_model_id()


def resource_path(relative_path):
    """ PyInstaller-safe resource path """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def apply_icon(win):
    ico = resource_path("BLGF.ico")
    png = resource_path("BLGF.png")

    # Taskbar / Alt-Tab icon
    if os.path.exists(ico):
        try:
            win.iconbitmap(ico)
        except Exception:
            pass

    # Tk titlebar fallback
    if os.path.exists(png):
        try:
            img = tk.PhotoImage(file=png)
            win.iconphoto(True, img)
            win._icon_ref = img  # prevent GC
        except Exception:
            pass


# === Paths ===
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"
def _get_credentials_path():
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "pg_credentials.json")
    else:
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "pg_credentials.json"
        )

CREDENTIALS_FILE = _get_credentials_path()

# Globals
barangay_source = None
road_source = None
output_mode = None

# surface_column_overrides: {path: existing_col_name} -- for any LOCAL
# Land Parcel source where a pre-existing "cama_rd_surface"-like column
# was detected (see _check_parcel_surface_conflicts() below) and the
# user confirmed proceeding at Run time. Read by run_processing() and
# passed into process_surface() as output_column_name, so the tool
# writes back into the EXACT existing column (preserving its original
# casing) instead of always writing a hardcoded "CAMA_RD_SURFACE" --
# the latter would silently create a confusing duplicate column
# whenever the existing one used different casing. A source with no
# entry here uses the default "CAMA_RD_SURFACE" name. Scope: LOCAL
# sources only -- Database Land Parcel sources are explicitly out of
# scope for this check (see _check_parcel_surface_conflicts()).
surface_column_overrides = {}

# ---------------- CRS Helper ----------------
def get_prs92_zone(labeled_gdfs):
    """
    Choose PRS92 zone EPSG from the combined bbox-midpoint longitude of
    one or more input GeoDataFrames.

    labeled_gdfs: list of (label, gdf) tuples, e.g.
        [("Land Parcel", brgy_gdf), ("Road Network", road_gdf)]
    The label is used only for diagnostics. It has no effect on CRS
    detection.

    Auxiliary layers without usable geometry are ignored for CRS zone
    determination. Downstream processing may still validate required
    layers independently.

    Uses total_bounds, not a unioned-geometry centroid -- unary_union.centroid
    is a known source of GEOS TopologyExceptions on real-world cadastral
    data with invalid geometries. Same zone-boundary thresholds as
    before; only the longitude used to evaluate them has changed.
    """
    valid = [
        (label, g) for label, g in labeled_gdfs
        if g is not None and not g.empty and g.geometry.notna().any()
    ]
    if not valid:
        raise ValueError("No valid (non-empty) GeoDataFrames provided for PRS92 zone detection.")

    all_bounds = []
    for label, g in valid:
        if g.crs is None:
            g = g.set_crs(epsg=4326)
        epsg = g.crs.to_epsg()
        if epsg != 4326:
            g_wgs84 = g.to_crs(epsg=4326)
        else:
            g_wgs84 = g

        bounds = g_wgs84.total_bounds
        if any(math.isnan(v) for v in bounds):
            raise ValueError(
                f"Cannot determine PRS92 zone because the '{label}' layer "
                f"contains no valid geometry."
            )
        all_bounds.append(bounds)

    minx = min(b[0] for b in all_bounds)
    maxx = max(b[2] for b in all_bounds)
    lon = (minx + maxx) / 2
    if lon < 118: return 3121
    elif lon < 120: return 3122
    elif lon < 122: return 3123
    elif lon < 124: return 3124
    else: return 3125

# ---------------- DB Helpers ----------------
def load_db_credentials():
    path = _get_credentials_path()
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        messagebox.showerror("Error", "Database credentials not found.")
        return None

def get_geometry_column(table_name, engine, schema):
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT f_geometry_column
                FROM geometry_columns
                WHERE f_table_schema = :schema AND f_table_name = :table
            """), {"schema": schema, "table": table_name}).fetchone()
            return result[0] if result else None
    except:
        return None

def read_postgis_clean(table, engine, schema):
    geom_col = get_geometry_column(table, engine, schema)
    insp = inspect(engine)
    cols = [c['name'] for c in insp.get_columns(table, schema=schema) if c['name'] != geom_col]
    col_str = ", ".join([f'"{c}"' for c in cols]) if cols else ""
    if col_str:
        query = f'SELECT {col_str}, "{geom_col}" AS geometry FROM "{schema}"."{table}"'
    else:
        query = f'SELECT "{geom_col}" AS geometry FROM "{schema}"."{table}"'
    return gpd.read_postgis(query, engine, geom_col="geometry")

def open_in_global_mapper(path):
    if os.path.exists(GM_EXE_PATH) and os.path.exists(path):
        subprocess.Popen([GM_EXE_PATH, path], shell=True)


# ========================= PARCEL COLUMN-CONFLICT CHECK =========================
# _check_parcel_surface_conflicts(): checks LOCAL Land Parcel source(s)
# for an existing column matching "cama_rd_surface" (case-insensitive
# exact match) -- this tool is about to write its computed road
# surface(s) into that column, and on_run() below shows a combined
# confirmation dialog before proceeding.
#
# Same pattern as road_density.py's _check_parcel_density_conflicts()
# and road_width.py's canonical _read_gdf_worker(): runs synchronously
# on the main thread (this tool has no background worker thread /
# progress window / queue-polling architecture, and adding one is out
# of scope for this task), reads via plain gpd.read_file(path) (no
# partial/schema-only read trick), and a read failure here is NEVER
# treated as a column-conflict failure -- it only skips the conflict
# check for that one source (logged to console); the real read inside
# run_processing() further below remains solely responsible for
# surfacing any genuine read error to the user.
#
# Scope: LOCAL sources only. Database Land Parcel sources are
# explicitly out of scope for this check per the project task
# definition -- callers must not invoke this for "db"-mode sources.
def _check_parcel_surface_conflicts(local_paths):
    """
    Returns a list of (path, existing_col_name) tuples -- one entry
    only for local sources where a column matching "cama_rd_surface"
    (case-insensitive) was actually found. existing_col_name preserves
    the exact casing found in the source, so the confirmation dialog
    and the eventual write-back both show/use the real casing.
    """
    conflicts = []
    for path in local_paths:
        try:
            gdf = gpd.read_file(path)
        except Exception as e:
            print(f"⚠️ Could not read parcel layer to check for an "
                  f"existing CAMA_RD_SURFACE column: {path}: {e}")
            continue
        existing_col = next(
            (c for c in gdf.columns if c.lower() == "cama_rd_surface"), None
        )
        if existing_col:
            conflicts.append((path, existing_col))
    return conflicts


# ---------------- Processing ----------------
# NOTE (Part A3 investigation, resolved as NOT needed): unlike
# land_shape_compactness.py, this tool has no fix_geometry() helper at
# all. Investigated whether it should -- the operations below are
# geometry.intersects() (against a positive-distance road buffer, not
# buffer(0)), geometry.distance() from a parcel centroid, and .centroid
# itself. None of these require a full boolean set operation (union,
# intersection) across a whole collection -- the operation class
# responsible for the confirmed unary_union.centroid crash risk found
# elsewhere in this project. Empirically confirmed .intersects() and
# .distance() both return without crashing on a self-intersecting test
# polygon, including at GeoSeries batch level. No fix_geometry() added.
def process_surface(brgy_gdf, road_gdf, output_column_name="CAMA_RD_SURFACE"):
    """
    output_column_name : str -- the column name the computed road
        surface(s) are written to. Defaults to "CAMA_RD_SURFACE" (this
        tool's normal output, CAMA_-prefixed per project-wide column
        naming convention -- see road_width.py's own ROAD_WIDTH ->
        CAMA_ROAD_WIDTH). The GUI overrides this per-source when the
        selected LOCAL parcel layer already has an existing
        "cama_rd_surface"-like column (any casing) -- the exact
        existing name/casing is passed here so processing writes back
        into that same column instead of creating a hardcoded
        "CAMA_RD_SURFACE" alongside it as a confusing duplicate.
    """
    # Save original CRS
    orig_crs = brgy_gdf.crs

    # Temporary reproject to PRS92 (combined parcel + road extent)
    zone_epsg = get_prs92_zone([("Land Parcel", brgy_gdf), ("Road Network", road_gdf)])
    print(f"🌍 Reprojecting layers to EPSG:{zone_epsg} for processing...")
    brgy_gdf = brgy_gdf.to_crs(epsg=zone_epsg)
    road_gdf = road_gdf.to_crs(epsg=zone_epsg)

    # Auto-detect surface column (case-insensitive)
    surface_col = next(
        (c for c in road_gdf.columns if c.lower() in ("surface", "surf", "road_surf", "rd_surface", "pavement")),
        None
    )
    if surface_col is None:
        messagebox.showerror(
            "Missing Column",
            f"Road layer has no 'surface' column.\n\n"
            f"Available columns: {', '.join(road_gdf.columns.tolist())}"
        )
        return brgy_gdf

    print(f"ℹ️ Using road surface column: '{surface_col}'")

    # Buffer the roads
    road_buffer = road_gdf.copy()
    road_buffer["geometry"] = road_gdf.buffer(10)

    brgy_gdf[output_column_name] = [[] for _ in range(len(brgy_gdf))]

    # Assign surfaces from intersecting roads
    for _, road in road_buffer.iterrows():
        surface_val = str(road.get(surface_col, "")).strip()
        if not surface_val:
            continue
        intersect_mask = brgy_gdf.geometry.intersects(road.geometry)
        for idx in brgy_gdf[intersect_mask].index:
            if surface_val not in brgy_gdf.at[idx, output_column_name]:
                brgy_gdf.at[idx, output_column_name].append(surface_val)

    # Nearest road for those with no intersections
    no_surface_mask = brgy_gdf[output_column_name].apply(lambda x: len(x) == 0)
    for idx, row in brgy_gdf[no_surface_mask].iterrows():
        centroid: Point = row.geometry.centroid
        distances = road_gdf.distance(centroid)
        nearest_idx = distances.idxmin()
        nearest_surface = str(road_gdf.at[nearest_idx, surface_col]).strip()
        if nearest_surface:
            brgy_gdf.at[idx, output_column_name] = [nearest_surface]

    # Convert list → slash-separated string
    brgy_gdf[output_column_name] = brgy_gdf[output_column_name].apply(
        lambda surfaces: "/".join(sorted(set(surfaces))) if surfaces else None
    )

    # Reproject back to original CRS
    if orig_crs:
        brgy_gdf = brgy_gdf.to_crs(orig_crs)

    return brgy_gdf


# ---------------- Output filename helpers ----------------
def _split_trailing_number(base_name: str):
    m = re.match(r'^(.*)_(\d+)$', base_name)
    if m:
        return m.group(1), int(m.group(2))
    return base_name, None


def resolve_output_base_name(folder: str, desired_base_name: str, ext: str = "gpkg") -> str:
    candidate_path = os.path.join(folder, f"{desired_base_name}.{ext}")
    if not os.path.exists(candidate_path):
        return desired_base_name
    root, _existing_number = _split_trailing_number(desired_base_name)
    pattern = re.compile(rf'^{re.escape(root)}_(\d+)\.{re.escape(ext)}$', re.IGNORECASE)
    max_n = 0
    try:
        for fname in os.listdir(folder):
            m = pattern.match(fname)
            if m:
                max_n = max(max_n, int(m.group(1)))
    except OSError:
        pass
    return f"{root}_{max_n + 1}"


def ask_overwrite_dialog(parent, conflicting_names):
    result = {"choice": "cancel"}
    dialog = tk.Toplevel(parent)
    apply_icon(dialog)
    dialog.title("File(s) Already Exist")
    dialog.resizable(False, False)
    dialog.grab_set()
    dialog.deiconify()
    dialog.lift()
    dialog.focus_force()
    dialog.attributes("-topmost", True)
    dialog.after(100, lambda: dialog.attributes("-topmost", False))

    def choose(value):
        result["choice"] = value
        dialog.destroy()

    dialog.protocol("WM_DELETE_WINDOW", lambda: choose("cancel"))

    btn_frame = tk.Frame(dialog)
    btn_frame.pack(side="bottom", fill="x", pady=(4, 12))
    tk.Button(btn_frame, text="Overwrite", width=14, cursor="hand2",
              command=lambda: choose("overwrite")).pack(side="left", padx=(16, 4))
    tk.Button(btn_frame, text="Create New File", width=16, cursor="hand2",
              command=lambda: choose("new")).pack(side="left", padx=4)
    tk.Button(btn_frame, text="Cancel", width=10, cursor="hand2",
              command=lambda: choose("cancel")).pack(side="left", padx=(4, 16))

    tk.Label(dialog, text="The following output file(s) already exist:",
             font=("Segoe UI", 10, "bold"), anchor="w"
             ).pack(fill="x", padx=16, pady=(16, 4))

    MAX_LIST_LINES = 10
    TEXT_WIDTH_CHARS = 55
    list_frame = tk.Frame(dialog)
    list_frame.pack(fill="both", expand=True, padx=16, pady=(0, 4))
    vscroll = tk.Scrollbar(list_frame, orient="vertical")
    hscroll = tk.Scrollbar(list_frame, orient="horizontal")
    text = tk.Text(
        list_frame, wrap="none", height=min(len(conflicting_names), MAX_LIST_LINES),
        width=TEXT_WIDTH_CHARS, yscrollcommand=vscroll.set, xscrollcommand=hscroll.set,
        relief="flat", bg=dialog.cget("bg"), font=("Segoe UI", 9))
    vscroll.config(command=text.yview)
    hscroll.config(command=text.xview)
    if len(conflicting_names) > MAX_LIST_LINES:
        vscroll.pack(side="right", fill="y")
    needs_hscroll = any(len(f"\u2022 {name}") > TEXT_WIDTH_CHARS for name in conflicting_names)
    if needs_hscroll:
        hscroll.pack(side="bottom", fill="x")
    text.pack(side="left", fill="both", expand=True)
    for name in conflicting_names:
        text.insert("end", f"\u2022 {name}\n")
    text.config(state="disabled")

    tk.Label(dialog, text=(
        "Overwrite will replace these files. Create New File will save "
        "them under a new name instead, leaving the existing files "
        "untouched. This choice applies to all files listed above."
    ), wraplength=380, justify="left", anchor="w"
    ).pack(fill="x", padx=16, pady=(4, 8))

    dialog.update_idletasks()
    req_w = max(dialog.winfo_reqwidth(), 420)
    req_h = dialog.winfo_reqheight()
    sw = dialog.winfo_screenwidth()
    sh = dialog.winfo_screenheight()
    x = (sw - req_w) // 2
    y = (sh - req_h) // 2
    dialog.geometry(f"{req_w}x{req_h}+{x}+{y}")

    dialog.wait_window()
    return result["choice"]


# ========================= GLOBAL MAPPER =========================
def load_in_global_mapper(filepath):
    try:
        import ctypes.wintypes
        gm_hwnd = None

        def enum_callback(hwnd, _):
            nonlocal gm_hwnd
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            if "Global Mapper" in buf.value:
                gm_hwnd = hwnd
                return False
            return True

        EnumWindowsProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        ctypes.windll.user32.EnumWindows(EnumWindowsProc(enum_callback), 0)

        subprocess.Popen([GM_EXE_PATH, filepath])
        print(f"🗺️ Sent to Global Mapper: {filepath}")
    except Exception as e:
        print(f"⚠️ Could not open in Global Mapper: {e}")


# ========================= DB TABLE PICKER =========================
def _pick_db_tables(parent, tables, multi, on_select):
    from tkinter import ttk
    picker = tk.Toplevel(parent)
    apply_icon(picker)
    picker.title("Select Table(s)")
    picker.resizable(False, False)
    picker.grab_set()

    mode = tk.MULTIPLE if multi else tk.SINGLE
    lb = Listbox(picker, selectmode=mode, width=55, height=15)
    for t in tables:
        lb.insert(tk.END, t)
    lb.pack(padx=10, pady=10)

    def submit():
        sel = [lb.get(i) for i in lb.curselection()]
        if sel:
            on_select(sel)
            picker.destroy()

    tk.Button(picker, text="Confirm Selection", command=submit,
              width=20).pack(pady=(0, 10))


# ========================= MAIN WINDOW =========================
def open_main_window(root):
    from tkinter import ttk

    win = tk.Toplevel(root)
    apply_icon(win)
    win.title("Road Surface Tool")
    win.resizable(False, False)
    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))

    # ── state ────────────────────────────────────────────────────
    parcel_source_type = tk.StringVar(master=win, value="local")
    road_source_type   = tk.StringVar(master=win, value="local")
    output_dest_type   = tk.StringVar(master=win, value="local")

    # Single-selection architecture: one local file and one DB table
    # may exist in memory at any time. Authority variables -- all GUI
    # labels and run-button state are derived from them, never the reverse.
    parcel_local_path = None   # authority: single local file path
    parcel_db_table   = None   # authority: single DB table name
    road_local_path    = tk.StringVar(master=win)
    road_db_table      = tk.StringVar(master=win)
    output_local_dir   = tk.StringVar(master=win)

    # run_status_var: drives the always-visible status label under the
    # Run button ("Please select ..." / "Ready to run.") and mirrors
    # whether the Run button itself is enabled. Updated by
    # _update_run_button_state() below. Its validation-order cascade
    # intentionally mirrors on_run()'s own validation order below --
    # conscious duplication for a minimal-risk, additive gating layer,
    # not a refactor of on_run() itself; keep the two in sync if this
    # tool's required inputs ever change.
    run_status_var = tk.StringVar(master=win, value="Preparing…")

    PAD = dict(padx=8, pady=4)

    def section_label(parent, text):
        frm = tk.Frame(parent)
        frm.pack(fill="x", padx=10, pady=(10, 2))
        tk.Label(frm, text=text,
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        ttk.Separator(frm, orient="horizontal").pack(
            side="left", fill="x", expand=True, padx=(6, 0), pady=4)

    # ── SECTION 1: LAND PARCEL ───────────────────────────────────
    section_label(win, "Land Parcel Source")

    parcel_frame = tk.Frame(win)
    parcel_frame.pack(fill="x", padx=18, pady=2)

    radio_row = tk.Frame(parcel_frame)
    radio_row.pack(fill="x")
    tk.Radiobutton(radio_row, text="Local File",
                   variable=parcel_source_type, value="local",
                   command=lambda: _toggle_parcel()).pack(side="left")
    tk.Radiobutton(radio_row, text="Database Table",
                   variable=parcel_source_type, value="db",
                   command=lambda: _toggle_parcel()).pack(side="left", padx=(12, 0))

    parcel_files_var = tk.StringVar(master=win, value="No file selected")
    parcel_db_label  = tk.StringVar(master=win, value="No table selected")

    parcel_action_row = tk.Frame(parcel_frame)
    parcel_action_row.pack(fill="x", pady=2)

    parcel_lbl = tk.Label(parcel_action_row, textvariable=parcel_files_var,
                          fg="gray", anchor="w", width=42)
    parcel_lbl.pack(side="left")

    parcel_btn = tk.Button(parcel_action_row, text="Browse…", width=10)
    parcel_btn.pack(side="left", **PAD)

    def browse_parcel_files():
        file = filedialog.askopenfilename(filetypes=[
            ("Shapefiles", "*.shp"), ("GeoPackage", "*.gpkg"), ("All", "*.*")])
        # Cancel returns "" -- do not assign, preserving previous selection.
        if file:
            nonlocal parcel_local_path
            parcel_local_path = file
            parcel_files_var.set(os.path.basename(file))
        _update_run_button_state()

    def _on_parcel_db_selected(sel):
        # Only called on confirmed selection -- Cancel never calls on_select,
        # so parcel_db_table retains its previous value automatically.
        nonlocal parcel_db_table
        parcel_db_table = sel[0]
        parcel_db_label.set(sel[0])
        _update_run_button_state()

    def browse_parcel_db():
        creds = load_db_credentials()
        if not creds:
            return
        engine = create_engine(
            f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
        )
        tables = inspect(engine).get_table_names(schema=creds["schema"])
        if not tables:
            messagebox.showwarning("No Tables", "No tables found in the database schema.")
            return
        _pick_db_tables(win, tables, multi=False, on_select=_on_parcel_db_selected)

    def _toggle_parcel():
        # Always render from authority variables -- never from StringVar state.
        # Guarantees Local → DB → Local always restores the original selection.
        if parcel_source_type.get() == "local":
            parcel_lbl.config(textvariable=parcel_files_var)
            parcel_btn.config(text="Browse…", command=browse_parcel_files)
            parcel_files_var.set(
                os.path.basename(parcel_local_path) if parcel_local_path
                else "No file selected"
            )
        else:
            parcel_lbl.config(textvariable=parcel_db_label)
            parcel_btn.config(text="Select…", command=browse_parcel_db)
            parcel_db_label.set(
                parcel_db_table if parcel_db_table
                else "No table selected"
            )
        _update_run_button_state()

    # ── SECTION 2: ROAD NETWORK ──────────────────────────────────
    section_label(win, "Road Network Source")

    road_frame = tk.Frame(win)
    road_frame.pack(fill="x", padx=18, pady=2)

    road_radio_row = tk.Frame(road_frame)
    road_radio_row.pack(fill="x")
    tk.Radiobutton(road_radio_row, text="Local File",
                   variable=road_source_type, value="local",
                   command=lambda: _toggle_road()).pack(side="left")
    tk.Radiobutton(road_radio_row, text="Database Table",
                   variable=road_source_type, value="db",
                   command=lambda: _toggle_road()).pack(side="left", padx=(12, 0))

    road_file_var = tk.StringVar(master=win, value="No file selected")
    road_db_var   = tk.StringVar(master=win, value="No table selected")

    road_action_row = tk.Frame(road_frame)
    road_action_row.pack(fill="x", pady=2)

    road_lbl = tk.Label(road_action_row, textvariable=road_file_var,
                        fg="gray", anchor="w", width=42)
    road_lbl.pack(side="left")

    road_btn = tk.Button(road_action_row, text="Browse…", width=10)
    road_btn.pack(side="left", **PAD)

    def browse_road_file():
        f = filedialog.askopenfilename(filetypes=[
            ("Shapefiles", "*.shp"), ("GeoPackage", "*.gpkg"), ("All", "*.*")])
        if f:
            road_local_path.set(f)
            road_file_var.set(os.path.basename(f))
            _update_run_button_state()

    def _on_road_db_selected(sel):
        # _pick_db_tables() only invokes on_select after a confirmed
        # selection, so sel is never empty here -- the original
        # lambda's "if sel else None" branch was a redundant
        # conditional. Switching to a named callback is a readability
        # change only; no behavior change.
        road_db_table.set(sel[0])
        road_db_var.set(sel[0])
        _update_run_button_state()

    def browse_road_db():
        creds = load_db_credentials()
        if not creds:
            return
        engine = create_engine(
            f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
        )
        tables = inspect(engine).get_table_names(schema=creds["schema"])
        if not tables:
            messagebox.showwarning("No Tables", "No tables found in the database schema.")
            return
        _pick_db_tables(win, tables, multi=False, on_select=_on_road_db_selected)

    def _toggle_road():
        if road_source_type.get() == "local":
            road_lbl.config(textvariable=road_file_var)
            road_btn.config(text="Browse…", command=browse_road_file)
        else:
            road_lbl.config(textvariable=road_db_var)
            road_btn.config(text="Select…", command=browse_road_db)
        _update_run_button_state()

    # ── SECTION 3: OUTPUT ────────────────────────────────────────
    section_label(win, "Output Destination")

    output_frame = tk.Frame(win)
    output_frame.pack(fill="x", padx=18, pady=2)

    out_radio_row = tk.Frame(output_frame)
    out_radio_row.pack(fill="x")
    tk.Radiobutton(out_radio_row, text="Save to Local Folder",
                   variable=output_dest_type, value="local",
                   command=lambda: _toggle_output()).pack(side="left")
    tk.Radiobutton(out_radio_row, text="Save to Database",
                   variable=output_dest_type, value="db",
                   command=lambda: _toggle_output()).pack(side="left", padx=(12, 0))

    output_dir_var = tk.StringVar(master=win, value="No folder selected")
    output_db_var  = tk.StringVar(master=win,
                                  value="Will write back to the connected PostGIS schema.")

    out_action_row = tk.Frame(output_frame)
    out_action_row.pack(fill="x", pady=2)

    out_lbl = tk.Label(out_action_row, textvariable=output_dir_var,
                       fg="gray", anchor="w", width=42)
    out_lbl.pack(side="left")

    out_btn = tk.Button(out_action_row, text="Browse…", width=10)
    out_btn.pack(side="left", **PAD)

    def browse_output_dir():
        d = filedialog.askdirectory()
        if d:
            output_local_dir.set(d)
            output_dir_var.set(d)
            _update_run_button_state()

    def _toggle_output():
        if output_dest_type.get() == "local":
            out_lbl.config(textvariable=output_dir_var,
                           font=("Segoe UI", 9), fg="gray")
            out_btn.config(text="Browse…", command=browse_output_dir)
            out_btn.pack(side="left", **PAD)
        else:
            out_lbl.config(textvariable=output_db_var,
                           font=("Segoe UI", 8, "italic"), fg="gray")
            out_btn.pack_forget()
        _update_run_button_state()

    # ── RUN BUTTON ───────────────────────────────────────────────
    ttk.Separator(win, orient="horizontal").pack(
        fill="x", padx=10, pady=(12, 4))

    def on_run():
        global barangay_source, road_source, output_mode

        # validate parcel
        if parcel_source_type.get() == "local":
            if not parcel_local_path:
                messagebox.showerror("Missing Input",
                    "Please select a Land Parcel file.")
                return
            # Validation guarantees parcel_local_path is not None here --
            # barangay_source never contains None (Phase 1 invariant 3).
            barangay_source = ("local", (parcel_local_path,))
        else:
            if not parcel_db_table:
                messagebox.showerror("Missing Input",
                    "Please select a Land Parcel table.")
                return
            barangay_source = ("db", (parcel_db_table,))

        # validate road
        if road_source_type.get() == "local":
            if not road_local_path.get():
                messagebox.showerror("Missing Input",
                    "Please select a Road Network file.")
                return
            road_source = ("local", [road_local_path.get()])
        else:
            if not road_db_table.get():
                messagebox.showerror("Missing Input",
                    "Please select a Road Network table.")
                return
            road_source = ("db", [road_db_table.get()])

        # validate output
        if output_dest_type.get() == "local":
            if not output_local_dir.get():
                messagebox.showerror("Missing Input",
                    "Please select an output folder.")
                return
            output_mode = ("local", output_local_dir.get())
        else:
            output_mode = ("db", None)


        # PRIORITY 1: column conflict check -- warn if the selected Land
        # Parcel source already has a CAMA_RD_SURFACE column. Shown
        # before the file-conflict dialog so the user can decide whether
        # to proceed at all before being asked about filename conflicts.
        # Declining cancels the run entirely; main window stays open.
        # Database Land Parcel sources are explicitly out of scope for
        # this check (see _check_parcel_surface_conflicts()).
        global surface_column_overrides
        if parcel_source_type.get() == "local":
            # parcel_local_path is guaranteed non-None here -- validation
            # above already returned if it was falsy.
            conflicts = _check_parcel_surface_conflicts([parcel_local_path])
            if conflicts:
                lines = "\n".join(
                    f"- '{os.path.basename(path)}' already has a '{existing_col}' column"
                    for path, existing_col in conflicts
                )
                proceed = messagebox.askyesno(
                    "Existing CAMA_RD_SURFACE column found",
                    f"{lines}\n\n"
                    "Processing will overwrite the existing column(s) with the "
                    "newly computed values.\n\nProceed?"
                )
                if not proceed:
                    print("Run cancelled by user (existing CAMA_RD_SURFACE column(s) found).")
                    return
                # Preserve each source's existing column name/casing
                # exactly -- e.g. a detected "caMA_rd_SURFACE" is
                # written back to "caMA_rd_SURFACE", not a hardcoded
                # "CAMA_RD_SURFACE" -- so no duplicate column is ever
                # created regardless of the existing casing. A source
                # with no entry here (no conflict was found) simply
                # uses the default name in process_surface() below.
                surface_column_overrides = dict(conflicts)
            else:
                surface_column_overrides = {}
        else:
            surface_column_overrides = {}

        # PRIORITY 2: file conflict check -- warn if an output file with
        # the same name already exists in the chosen output folder.
        # Root cause of bug fixed here: overwrite_mode was previously
        # local to on_run() and never reached run_processing(), causing
        # a NameError at runtime whenever a file conflict existed.
        # Fix: pass overwrite_mode explicitly as a parameter.
        overwrite_mode = None
        if output_mode[0] == "local":
            desired_names = (
                [os.path.splitext(os.path.basename(p))[0] for p in barangay_source[1]]
                if barangay_source[0] == "local"
                else list(barangay_source[1])
            )
            conflicting_names = [
                f"{name}.gpkg" for name in desired_names
                if os.path.exists(os.path.join(output_mode[1], f"{name}.gpkg"))
            ]
            if conflicting_names:
                overwrite_mode = ask_overwrite_dialog(win, conflicting_names)
                if overwrite_mode == "cancel":
                    print("Run cancelled by user (existing output file(s) found).")
                    return

        win.destroy()
        run_processing(overwrite_mode)

    # Single source of truth for the Run button's enabled/disabled
    # colors -- used both at button creation and inside
    # _update_run_button_state() below, so there's only one place to
    # change if the theme changes later.
    RUN_BTN_BG_ENABLED  = "#2e7d32"
    RUN_BTN_FG_ENABLED  = "white"
    RUN_BTN_BG_DISABLED = "#e0e0e0"
    RUN_BTN_FG_DISABLED = "#888888"

    def _update_run_button_state():
        """
        Single source of truth for whether the Run button may be
        pressed. Disabled (with an explanatory status message) until a
        Land Parcel source, a Road Network source, and an Output
        destination are all present.

        The cascade below intentionally mirrors on_run()'s own
        validation order further down -- conscious duplication for a
        minimal-risk, additive gating layer, not a refactor of on_run()
        itself. Keep the two in sync if this tool's required inputs
        ever change.

        Explicit bg/fg/cursor toggling (not just state=) is required:
        Tkinter does NOT automatically gray out a classic tk.Button's
        custom bg/fg when state="disabled", and does not suppress a
        widget's assigned cursor either -- both must be set explicitly
        for each state.
        """
        has_parcel = bool(parcel_local_path) if parcel_source_type.get() == "local" else bool(parcel_db_table)
        has_road = bool(road_local_path.get()) if road_source_type.get() == "local" else bool(road_db_table.get())
        has_output = bool(output_local_dir.get()) if output_dest_type.get() == "local" else True

        if not has_parcel:
            run_status_var.set("Please select a Land Parcel source.")
            ready = False
        elif not has_road:
            run_status_var.set("Please select a Road Network source.")
            ready = False
        elif not has_output:
            run_status_var.set("Please select an Output destination.")
            ready = False
        else:
            run_status_var.set("Ready to run.")
            ready = True

        if ready:
            run_btn.config(state="normal", cursor="hand2",
                            bg=RUN_BTN_BG_ENABLED, fg=RUN_BTN_FG_ENABLED)
        else:
            run_btn.config(state="disabled", cursor="no",
                            bg=RUN_BTN_BG_DISABLED, fg=RUN_BTN_FG_DISABLED,
                            disabledforeground=RUN_BTN_FG_DISABLED)

    run_btn = tk.Button(win, text="▶  Run Processing", command=on_run,
              bg=RUN_BTN_BG_ENABLED, fg=RUN_BTN_FG_ENABLED,
              font=("Segoe UI", 10, "bold"),
              relief="flat", padx=16, pady=6)
    run_btn.pack(pady=(4, 4))

    # Permanent status line UNDER the Run button -- always visible, no
    # hover required.
    run_status_lbl = tk.Label(win, textvariable=run_status_var,
                              font=("Segoe UI", 8), fg="gray")
    run_status_lbl.pack(pady=(0, 12))

    _toggle_parcel()
    _toggle_road()
    _toggle_output()
    _update_run_button_state()


# ---------------- Run ----------------
def run_processing(overwrite_mode=None):
    # overwrite_mode: passed from on_run(). Root cause of original bug:
    # no parameter existed, so overwrite_mode was unbound inside this
    # function, causing a NameError whenever a file conflict existed.
    global barangay_source, road_source, output_mode
    if not barangay_source or not road_source or not output_mode:
        messagebox.showerror("Error", "Selections incomplete.")
        return

    creds = load_db_credentials()
    schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    )

    if road_source[0] == "local":
        road_gdf = gpd.read_file(road_source[1][0])
    else:
        road_gdf = read_postgis_clean(road_source[1][0], engine, schema)

    if barangay_source[0] == "local":
        for path in barangay_source[1]:
            brgy_gdf = gpd.read_file(path)
            # output_column_name: preserves the exact existing column
            # name/casing this LOCAL source's parcel layer already had
            # (if the user confirmed overwriting one at Run time -- see
            # on_run()'s confirmation dialog). A source with no entry
            # here falls back to process_surface()'s own default
            # ("CAMA_RD_SURFACE").
            output_column_name = surface_column_overrides.get(path, "CAMA_RD_SURFACE")
            result = process_surface(brgy_gdf, road_gdf, output_column_name=output_column_name)
            if output_mode[0] == "local":
                desired_base_name = os.path.splitext(os.path.basename(path))[0]
                candidate_path = os.path.join(output_mode[1], f"{desired_base_name}.gpkg")
                had_conflict = os.path.exists(candidate_path)
                if had_conflict and overwrite_mode == "new":
                    base_name = resolve_output_base_name(output_mode[1], desired_base_name)
                else:
                    base_name = desired_base_name
                out = os.path.join(output_mode[1], f"{base_name}.gpkg")
                result.to_file(out, driver="GPKG")
                print(f"✅ Saved {out}")
                load_in_global_mapper(out)
            else:
                base = os.path.splitext(os.path.basename(path))[0]
                existing = [t for t in inspect(engine).get_table_names(schema=schema)
                            if base.lower() in t.lower()]
                out_table = existing[0] if existing else base + "_roadsurface"
                result.to_postgis(out_table, engine, schema=schema,
                                  if_exists="replace", index=False)
                print(f"🔄 Saved to DB: {out_table}")
    else:
        # Database Land Parcel sources: column-conflict check is out of
        # scope (see _check_parcel_surface_conflicts()) -- always uses
        # process_surface()'s default "CAMA_RD_SURFACE" name.
        for table in barangay_source[1]:
            brgy_gdf = read_postgis_clean(table, engine, schema)
            result = process_surface(brgy_gdf, road_gdf)
            if output_mode[0] == "local":
                desired_base_name = table
                candidate_path = os.path.join(output_mode[1], f"{desired_base_name}.gpkg")
                had_conflict = os.path.exists(candidate_path)
                if had_conflict and overwrite_mode == "new":
                    base_name = resolve_output_base_name(output_mode[1], desired_base_name)
                else:
                    base_name = desired_base_name
                out = os.path.join(output_mode[1], f"{base_name}.gpkg")
                result.to_file(out, driver="GPKG")
                print(f"✅ Saved {out}")
                load_in_global_mapper(out)
            else:
                existing = [t for t in inspect(engine).get_table_names(schema=schema)
                            if table.lower() in t.lower()]
                out_table = existing[0] if existing else table + "_roadsurface"
                result.to_postgis(out_table, engine, schema=schema,
                                  if_exists="replace", index=False)
                print(f"🔄 Saved to DB: {out_table}")

    messagebox.showinfo("Success", "Processing complete!")


# ---------------- Main ----------------
def main(parent=None):
    if parent is not None:
        open_main_window(parent)
    else:
        root = tk.Tk()
        apply_icon(root)
        root.withdraw()
        open_main_window(root)
        root.mainloop()


if __name__ == "__main__":
    main()