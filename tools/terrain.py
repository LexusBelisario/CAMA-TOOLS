import os
import math
import pyproj
os.environ["PROJ_LIB"] = pyproj.datadir.get_data_dir()

import geopandas as gpd
import rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling
from shapely.geometry import LineString, MultiLineString, Point
from shapely.strtree import STRtree
import numpy as np
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox
import math
import json
from sqlalchemy import create_engine, inspect, text
from scipy.ndimage import sobel

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
            win._icon_ref = img  # prevent garbage collection
        except Exception:
            pass


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
dtm_source = None
output_mode = None
progress_window = None
progress_label = None


# ---------------- Progress Window ----------------
def open_progress_window(parent=None):
    global progress_window, progress_label
    progress_window = tk.Toplevel(parent)
    apply_icon(progress_window)
    progress_window.title("Processing Progress")
    progress_window.geometry("320x100")
    progress_window.resizable(False, False)
    progress_label = tk.Label(progress_window, text="Starting...", wraplength=300)
    progress_label.pack(pady=20)
    progress_window.update()


def update_progress(msg):
    global progress_label, progress_window
    if progress_window and progress_label:
        progress_label.config(text=msg)
        progress_window.update()


def close_progress_window():
    global progress_window
    if progress_window:
        progress_window.destroy()
        progress_window = None


# ---------------- DB Helpers ----------------
def load_db_credentials():
    path = _get_credentials_path()
    if not os.path.exists(path):
        messagebox.showerror("Error", "Database credentials not found.")
        return None
    with open(path, "r") as f:
        return json.load(f)


def get_geometry_column(table_name, engine, schema):
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT f_geometry_column
            FROM geometry_columns
            WHERE f_table_schema = :schema AND f_table_name = :table
        """), {"schema": schema, "table": table_name}).fetchone()
        return result[0] if result else None


def get_columns_except_geom(table_name, engine, schema, geom_col):
    insp = inspect(engine)
    return [c['name'] for c in insp.get_columns(table_name, schema=schema) if c['name'] != geom_col]


def get_raster_srid(engine, schema, table):
    with engine.connect() as conn:
        return conn.execute(text(
            f'SELECT ST_SRID(rast) FROM "{schema}"."{table}" LIMIT 1'
        )).scalar()


def read_postgis_clean(table, engine, schema):
    geom_col = get_geometry_column(table, engine, schema)
    cols = get_columns_except_geom(table, engine, schema, geom_col)
    col_str = ", ".join([f'"{c}"' for c in cols]) if cols else ""
    query = f'SELECT {col_str + "," if col_str else ""}"{geom_col}" AS geometry FROM "{schema}"."{table}"'
    return gpd.read_postgis(query, engine, geom_col="geometry")


# ---------------- CRS Helpers ----------------
def detect_prs92_zone(labeled_gdfs):
    """
    Choose PRS92 zone EPSG from the combined bbox-midpoint longitude of
    one or more input GeoDataFrames.

    labeled_gdfs: list of (label, gdf) tuples, e.g.
        [("Land Parcel", parcels), ("Road Network", roads)]
    The label is used only for diagnostics. It has no effect on CRS
    detection.

    Auxiliary layers without usable geometry are ignored for CRS zone
    determination. Downstream processing may still validate required
    layers independently.

    Replaces the previous single-centroid, first-parcel-only version,
    which had a real off-by-one bug in its zone-boundary mapping (every
    threshold shifted one zone from the correct PRS92 EPSG codes) and
    used only feature 0's centroid rather than the dataset's extent.
    Uses total_bounds, not a unioned-geometry centroid -- unary_union.centroid
    is a known source of GEOS TopologyExceptions on real-world cadastral
    data with invalid geometries.
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


def reproject_raster_to_prs92(dtm, target_epsg):
    dst_crs = f"EPSG:{target_epsg}"
    transform, width, height = calculate_default_transform(
        dtm.crs, dst_crs, dtm.width, dtm.height, *dtm.bounds
    )
    kwargs = dtm.meta.copy()
    kwargs.update({"crs": dst_crs, "transform": transform, "width": width, "height": height})
    memfile = rasterio.io.MemoryFile()
    with memfile.open(**kwargs) as dst:
        for i in range(1, dtm.count + 1):
            reproject(
                source=rasterio.band(dtm, i),
                destination=rasterio.band(dst, i),
                src_transform=dtm.transform,
                src_crs=dtm.crs,
                dst_transform=transform,
                dst_crs=dst_crs,
                resampling=Resampling.bilinear
            )
    return memfile.open()


# ---------------- Fast Terrain Processing ----------------
def compute_slope_array(dtm):
    """Compute slope in degrees for entire DTM using Sobel gradient."""
    update_progress("Precomputing slope from DTM...")
    arr = dtm.read(1)
    arr[arr == dtm.nodata] = np.nan
    xres, yres = dtm.res
    dzdx = sobel(arr, axis=1, mode="nearest") / (8 * xres)
    dzdy = sobel(arr, axis=0, mode="nearest") / (8 * yres)
    slope = np.degrees(np.arctan(np.sqrt(dzdx ** 2 + dzdy ** 2)))
    return slope


def get_raster_values_batch(raster, points):
    """Efficiently sample raster at multiple coordinates."""
    coords = [(p.x, p.y) for p in points]
    values = [v[0] if v[0] is not None else np.nan for v in raster.sample(coords)]
    return values


def process_parcels_fast(parcels, roads, dtm, parcels_crs):
    # NOTE (Part A3 investigation, resolved as NOT needed): like
    # road_density.py, this function only reads parcels.geometry.centroid
    # from each parcel (line below) -- never buffers/intersects/unions
    # the parcel polygon itself. Road geometry is split into raw
    # LineString segments via direct coordinate-list slicing (no
    # buffer/union either) purely for STRtree nearest-neighbor lookup.
    # Centroid computation is already confirmed safe on invalid geometry
    # elsewhere in this project (no crash, unlike unary_union). No
    # fix_geometry() added.
    update_progress("Building road spatial index...")
    # Split roads into segments and index them
    segments = []
    for geom in roads.geometry:
        if geom.geom_type == "LineString":
            coords = list(geom.coords)
            segments += [LineString([coords[i], coords[i + 1]]) for i in range(len(coords) - 1)]
        elif geom.geom_type == "MultiLineString":
            for g in geom.geoms:
                coords = list(g.coords)
                segments += [LineString([coords[i], coords[i + 1]]) for i in range(len(coords) - 1)]

    tree = STRtree(segments)

    # Compute slope array
    slope_arr = compute_slope_array(dtm)
    band1 = dtm.read(1)
    transform = dtm.transform

    update_progress("Sampling parcel elevations...")
    centroids = parcels.geometry.centroid
    coords = [(p.x, p.y) for p in centroids]
    prcl_elevs = [v[0] for v in dtm.sample(coords)]

    update_progress("Finding nearest road and road elevations...")
    nearest_segments = []
    for p in centroids:
        res = tree.nearest(p)
        if isinstance(res, (int, np.integer)):
            nearest_segments.append(segments[int(res)])
        else:
            nearest_segments.append(res)

    road_points = [seg.interpolate(0.5, normalized=True) for seg in nearest_segments]
    road_elevs = [v[0] for v in dtm.sample([(p.x, p.y) for p in road_points])]

    update_progress("Calculating slope and elevation differences...")
    diffs, slopes, terrains, topos = [], [], [], []

    for i, c in enumerate(centroids):
        elev = prcl_elevs[i]
        road = road_elevs[i]
        diff = elev - road if elev and road else None
        diffs.append(round(diff, 2) if diff else None)

        # Extract slope from slope raster using coordinates
        row, col = ~transform * (c.x, c.y)
        row, col = int(row), int(col)
        slope_val = (
            float(slope_arr[row, col]) if 0 <= row < slope_arr.shape[0] and 0 <= col < slope_arr.shape[1] else None
        )
        slopes.append(round(slope_val, 2) if slope_val else None)

        # Terrain classification
        if slope_val is None:
            terrains.append(None)
        elif slope_val < 3:
            terrains.append("FLAT")
        else:
            terrains.append("SLOPING")

        if diff is None:
            topos.append(None)
        elif abs(diff) < 0.01:
            topos.append("At Street Level")
        elif diff < 0:
            topos.append("Below Street Level 0.5m" if abs(diff) < 0.5 else "Below Street Level >= 0.5m")
        else:
            topos.append("Above Street Level" if diff < 0.5 else "Above Street Level >= 0.5m")

    parcels["SLOPE"] = slopes
    parcels["TERRAIN"] = terrains
    parcels["PRCL_ELEV"] = prcl_elevs
    parcels["ROAD_ELEV"] = road_elevs
    parcels["PRCL_ROAD"] = diffs
    parcels["TOPO_LVL"] = topos

    return parcels.to_crs(parcels_crs)


# NOTE: An earlier, unreachable run_processing() (no-argument signature)
# used to live here. It was permanently shadowed by the run_processing(app_root)
# definition further below (Python keeps only the last function bound to a
# given name at module level) and was never callable -- open_main_window()'s
# on_run() always called run_processing(root), which only matches the
# surviving definition's signature. Verified unreachable via: (1) no
# external references anywhere in the project (tools are dispatched as
# isolated subprocesses via importlib, never imported directly by name),
# (2) the only call site in this file passes one positional argument,
# matching only the surviving definition. Removed rather than left in
# place to avoid a future fix being silently applied to the dead copy
# instead of the live one.

# ========================= GLOBAL MAPPER =========================
def load_in_global_mapper(filepath):
    try:
        import ctypes.wintypes
        import subprocess
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

        GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"
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
    win.title("Terrain Analysis Tool")
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
    dtm_source_type    = tk.StringVar(master=win, value="local")
    output_dest_type   = tk.StringVar(master=win, value="local")

    parcel_local_paths = []
    parcel_db_tables   = []
    road_local_path    = tk.StringVar(master=win)
    road_db_table      = tk.StringVar(master=win)
    dtm_local_path     = tk.StringVar(master=win)
    dtm_db_table       = tk.StringVar(master=win)
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
    tk.Radiobutton(radio_row, text="Local File(s)",
                   variable=parcel_source_type, value="local",
                   command=lambda: _toggle_parcel()).pack(side="left")
    tk.Radiobutton(radio_row, text="Database Table(s)",
                   variable=parcel_source_type, value="db",
                   command=lambda: _toggle_parcel()).pack(side="left", padx=(12, 0))

    parcel_files_var = tk.StringVar(master=win, value="No file(s) selected")
    parcel_db_label  = tk.StringVar(master=win, value="No table(s) selected")

    parcel_action_row = tk.Frame(parcel_frame)
    parcel_action_row.pack(fill="x", pady=2)

    parcel_lbl = tk.Label(parcel_action_row, textvariable=parcel_files_var,
                          fg="gray", anchor="w", width=42)
    parcel_lbl.pack(side="left")

    parcel_btn = tk.Button(parcel_action_row, text="Browse…", width=10)
    parcel_btn.pack(side="left", **PAD)

    def browse_parcel_files():
        files = filedialog.askopenfilenames(filetypes=[
            ("Shapefiles", "*.shp"), ("GeoPackage", "*.gpkg"), ("All", "*.*")])
        if files:
            parcel_local_paths.clear()
            parcel_local_paths.extend(files)
            parcel_files_var.set(f"{len(files)} file(s) selected")
            _update_run_button_state()

    def _on_parcel_db_selected(sel):
        parcel_db_tables.clear()
        parcel_db_tables.extend(sel)
        parcel_db_label.set(f"{len(sel)} table(s) selected")
        _update_run_button_state()

    def browse_parcel_db():
        creds = load_db_credentials()
        if not creds:
            return
        from sqlalchemy import create_engine as ce
        eng = ce(f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}")
        tables = inspect(eng).get_table_names(schema=creds["schema"])
        if not tables:
            messagebox.showwarning("No Tables", "No tables found in the database schema.")
            return
        _pick_db_tables(win, tables, multi=True, on_select=_on_parcel_db_selected)

    def _toggle_parcel():
        if parcel_source_type.get() == "local":
            parcel_lbl.config(textvariable=parcel_files_var)
            parcel_btn.config(text="Browse…", command=browse_parcel_files)
        else:
            parcel_lbl.config(textvariable=parcel_db_label)
            parcel_btn.config(text="Select…", command=browse_parcel_db)
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
        from sqlalchemy import create_engine as ce
        eng = ce(f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}")
        tables = inspect(eng).get_table_names(schema=creds["schema"])
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

    # ── SECTION 3: DTM ───────────────────────────────────────────
    section_label(win, "DTM Source")

    dtm_frame = tk.Frame(win)
    dtm_frame.pack(fill="x", padx=18, pady=2)

    dtm_radio_row = tk.Frame(dtm_frame)
    dtm_radio_row.pack(fill="x")
    tk.Radiobutton(dtm_radio_row, text="Local File (.tif)",
                   variable=dtm_source_type, value="local",
                   command=lambda: _toggle_dtm()).pack(side="left")
    tk.Radiobutton(dtm_radio_row, text="Database Table",
                   variable=dtm_source_type, value="db",
                   command=lambda: _toggle_dtm()).pack(side="left", padx=(12, 0))

    dtm_file_var = tk.StringVar(master=win, value="No file selected")
    dtm_db_var   = tk.StringVar(master=win, value="No table selected")

    dtm_action_row = tk.Frame(dtm_frame)
    dtm_action_row.pack(fill="x", pady=2)

    dtm_lbl = tk.Label(dtm_action_row, textvariable=dtm_file_var,
                       fg="gray", anchor="w", width=42)
    dtm_lbl.pack(side="left")

    dtm_btn = tk.Button(dtm_action_row, text="Browse…", width=10)
    dtm_btn.pack(side="left", **PAD)

    def browse_dtm_file():
        f = filedialog.askopenfilename(filetypes=[
            ("GeoTIFF", "*.tif"), ("All", "*.*")])
        if f:
            dtm_local_path.set(f)
            dtm_file_var.set(os.path.basename(f))
            _update_run_button_state()

    def _on_dtm_db_selected(sel):
        # Same note as _on_road_db_selected() above: _pick_db_tables()
        # only calls on_select with a confirmed, non-empty selection.
        dtm_db_table.set(sel[0])
        dtm_db_var.set(sel[0])
        _update_run_button_state()

    def browse_dtm_db():
        creds = load_db_credentials()
        if not creds:
            return
        from sqlalchemy import create_engine as ce
        eng = ce(f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}")
        tables = inspect(eng).get_table_names(schema=creds["schema"])
        if not tables:
            messagebox.showwarning("No Tables", "No tables found in the database schema.")
            return
        _pick_db_tables(win, tables, multi=False, on_select=_on_dtm_db_selected)

    def _toggle_dtm():
        if dtm_source_type.get() == "local":
            dtm_lbl.config(textvariable=dtm_file_var)
            dtm_btn.config(text="Browse…", command=browse_dtm_file)
        else:
            dtm_lbl.config(textvariable=dtm_db_var)
            dtm_btn.config(text="Select…", command=browse_dtm_db)
        _update_run_button_state()

    # ── SECTION 4: OUTPUT ────────────────────────────────────────
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
        global barangay_source, road_source, dtm_source, output_mode

        # validate parcel
        if parcel_source_type.get() == "local":
            if not parcel_local_paths:
                messagebox.showerror("Missing Input",
                    "Please select at least one Land Parcel file.")
                return
            barangay_source = ("local", tuple(parcel_local_paths))
        else:
            if not parcel_db_tables:
                messagebox.showerror("Missing Input",
                    "Please select at least one Land Parcel table.")
                return
            barangay_source = ("db", parcel_db_tables)

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

        # validate dtm
        if dtm_source_type.get() == "local":
            if not dtm_local_path.get():
                messagebox.showerror("Missing Input",
                    "Please select a DTM file.")
                return
            dtm_source = ("local", dtm_local_path.get())
        else:
            if not dtm_db_table.get():
                messagebox.showerror("Missing Input",
                    "Please select a DTM table.")
                return
            dtm_source = ("db", dtm_db_table.get())

        # validate output
        if output_dest_type.get() == "local":
            if not output_local_dir.get():
                messagebox.showerror("Missing Input",
                    "Please select an output folder.")
                return
            output_mode = ("local", output_local_dir.get())
        else:
            output_mode = ("db", None)

        win.destroy()
        run_processing(root)

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
        Land Parcel source, a Road Network source, a DTM source, and an
        Output destination are all present.

        The has_parcel / has_road / has_dtm / has_output cascade below
        intentionally mirrors on_run()'s own validation order further
        down -- this is a conscious duplication for a minimal-risk,
        additive gating layer, not a refactor of on_run() itself. Keep
        the two in sync if this tool's required inputs ever change.

        Explicit bg/fg/cursor toggling (not just state=) is required:
        Tkinter does NOT automatically gray out a classic tk.Button's
        custom bg/fg when state="disabled", and does not suppress a
        widget's assigned cursor either -- both must be set explicitly
        for each state.
        """
        has_parcel = bool(parcel_local_paths) if parcel_source_type.get() == "local" else bool(parcel_db_tables)
        has_road = bool(road_local_path.get()) if road_source_type.get() == "local" else bool(road_db_table.get())
        has_dtm = bool(dtm_local_path.get()) if dtm_source_type.get() == "local" else bool(dtm_db_table.get())
        has_output = bool(output_local_dir.get()) if output_dest_type.get() == "local" else True

        if not has_parcel:
            run_status_var.set("Please select a Land Parcel source.")
            ready = False
        elif not has_road:
            run_status_var.set("Please select a Road Network source.")
            ready = False
        elif not has_dtm:
            run_status_var.set("Please select a DTM source.")
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
    _toggle_dtm()
    _toggle_output()
    _update_run_button_state()


# ========================= RUN =========================
def run_processing(app_root):
    open_progress_window(app_root)
    try:
        global barangay_source, road_source, dtm_source, output_mode
        creds = load_db_credentials()
        if not creds:
            close_progress_window()
            return
        schema = creds["schema"]
        engine = create_engine(
            f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
        )

        update_progress("Loading road data...")
        road_gdf = (
            gpd.read_file(road_source[1][0]) if road_source[0] == "local"
            else read_postgis_clean(road_source[1][0], engine, schema)
        )

        barangay_list = barangay_source[1]
        for idx, src in enumerate(barangay_list, 1):
            update_progress(f"Loading parcel {idx}/{len(barangay_list)}...")
            if barangay_source[0] == "local":
                parcels = gpd.read_file(src)
                name = os.path.splitext(os.path.basename(src))[0]
            else:
                parcels = read_postgis_clean(src, engine, schema)
                name = src

            target_epsg = detect_prs92_zone([("Land Parcel", parcels), ("Road Network", road_gdf)])
            parcels_crs = parcels.crs
            parcels = parcels.to_crs(epsg=target_epsg)
            roads = road_gdf.to_crs(epsg=target_epsg)

            update_progress("Loading DTM...")
            if dtm_source[0] == "local":
                dtm_raw = rasterio.open(dtm_source[1])
                dtm = reproject_raster_to_prs92(dtm_raw, target_epsg)
            else:
                dtm_table = dtm_source[1]
                dtm_raw = rasterio.open(
                    f"PG:dbname={creds['database']} host={creds['host']} "
                    f"user={creds['username']} password={creds['password']} "
                    f"schema={schema} table={dtm_table} column=rast"
                )
                srid = get_raster_srid(engine, schema, dtm_table)
                dtm = reproject_raster_to_prs92(dtm_raw, target_epsg) \
                    if srid != target_epsg else dtm_raw

            result = process_parcels_fast(parcels, roads, dtm, parcels_crs)

            update_progress("Saving output...")
            if output_mode[0] == "local":
                out = os.path.join(output_mode[1], f"{name}_terrain.gpkg")
                result.to_file(out, driver="GPKG")
                print(f"✅ Saved: {out}")
                load_in_global_mapper(out)
            else:
                existing = [
                    t for t in inspect(engine).get_table_names(schema=schema)
                    if name.lower() in t.lower()
                ]
                out_table = existing[0] if existing else name + "_terrain"
                result.to_postgis(out_table, engine, schema=schema,
                                  if_exists="replace", index=False)
                print(f"✅ Saved to DB: {out_table}")

            update_progress(f"✅ Completed {name}")

        close_progress_window()
        messagebox.showinfo("Success", "✅ Terrain processing complete!")

    except Exception as e:
        close_progress_window()
        messagebox.showerror("Error", f"❌ Processing failed:\n{str(e)}")


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