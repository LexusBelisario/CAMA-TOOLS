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

# ========================= EXISTING OUTPUT-COLUMN CONFLICT DETECTION =========================
# OUTPUT_COLUMN_TARGETS: this tool's six output column names, checked
# for pre-existing conflicts in a selected LOCAL Land Parcel source (see
# _check_parcel_terrain_conflicts() below, and the combined dialog in
# on_run()). Mirrors road_frontage.py's OUTPUT_COLUMN_TARGETS exactly:
# ALL six are checked, not just one -- they are one feature set computed
# together in the same run, so a source with (for example) an existing
# CAMA_SLOPE column but no existing CAMA_TERRAIN column still needs a
# conflict warning, to avoid ending up with an old CAMA_TERRAIN value
# sitting alongside a freshly-computed CAMA_SLOPE from a DIFFERENT
# run/computation -- an inconsistent, misleading combination.
#
# Cross-tool CAMA_ prefix standard: every column this tool CREATES gets
# a "CAMA_" prefix -- matches road_width.py's own CAMA_ROAD_WIDTH
# convention. These targets check for the NEW, prefixed names ONLY --
# never the OLD, non-prefixed names (e.g. a plain "SLOPE" column left
# over from a pre-CAMA_-prefix version of this tool). This tool never
# auto-detects, auto-removes, or auto-overwrites an old, non-prefixed
# column -- if one exists, it is simply left alone, untouched, and a NEW
# CAMA_-prefixed column is created alongside it. Only conflicts against
# the NEW naming scheme are ever surfaced to the user.
#
# Matching is EXACT (case-insensitive) -- "CAMA_SLOPE" vs "SLOPE_PCT" is
# not a match; only "cama_slope"/"CAMA_SLOPE"/"Cama_Slope"/etc. (same
# letters, any casing) count as the same column.
OUTPUT_COLUMN_TARGETS = (
    "CAMA_SLOPE", "CAMA_TERRAIN", "CAMA_PRCL_ELEV",
    "CAMA_ROAD_ELEV", "CAMA_PRCL_ROAD", "CAMA_TOPO_LVL",
)

# parcel_output_column_overrides: {path: {"CAMA_SLOPE": name, ...}} -- for
# any LOCAL Land Parcel source where one or more pre-existing
# CAMA_-prefixed output columns were detected (see
# _check_parcel_terrain_conflicts() below) and the user confirmed
# proceeding at Run time. Read by run_processing() and passed into
# process_parcels_fast() as the six *_col keyword arguments, so the tool
# writes back into the EXACT existing column(s) (preserving original
# casing) instead of always writing hardcoded "CAMA_*" names -- the
# latter would silently create confusing duplicate columns whenever an
# existing one used different casing. A source with no entry here (or a
# target missing from its entry) uses that target's default CAMA_ name.
# Scope: LOCAL sources only -- Database Land Parcel sources are
# explicitly out of scope for this check.
parcel_output_column_overrides = {}


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


def _detect_existing_output_columns(gdf):
    """
    Checks a parcel GeoDataFrame for pre-existing columns matching any of
    OUTPUT_COLUMN_TARGETS (CAMA_SLOPE, CAMA_TERRAIN, CAMA_PRCL_ELEV,
    CAMA_ROAD_ELEV, CAMA_PRCL_ROAD, CAMA_TOPO_LVL), exact match
    (case-insensitive) -- "cama_slope" matches "CAMA_SLOPE", but a
    column like "CAMA_SLOPE_PCT" or "SLOPE" does NOT match (no
    substring/partial matching, and no matching against the old,
    unprefixed names -- see OUTPUT_COLUMN_TARGETS' own docstring).

    Mirrors road_frontage.py's _detect_existing_output_columns() exactly.

    Returns a dict {target_name: actual_existing_column_name}, containing
    ONLY the targets that actually have a match -- e.g.
    {"CAMA_SLOPE": "caMA_SLOPE"} if only a differently-cased CAMA_SLOPE
    column exists and the other five targets have no match at all. Empty
    dict if none of the six targets have any existing column. The
    actual column's ORIGINAL casing is preserved in the returned value
    (e.g. "caMA_SLOPE", not "CAMA_SLOPE") -- this is what gets shown to
    the user in the confirmation dialog and what process_parcels_fast()
    writes back into, so an existing differently-cased column is reused
    exactly as found rather than renamed or duplicated.
    """
    found = {}
    for target in OUTPUT_COLUMN_TARGETS:
        match = next((c for c in gdf.columns if c.lower() == target.lower()), None)
        if match is not None:
            found[target] = match
    return found


# ========================= PARCEL COLUMN-CONFLICT CHECK =========================
# _check_parcel_terrain_conflicts(): checks LOCAL Land Parcel source(s)
# for pre-existing columns matching any of OUTPUT_COLUMN_TARGETS -- this
# tool is about to write its six computed terrain columns into those
# columns, and on_run() below shows a combined confirmation dialog
# before proceeding.
#
# Unlike road_frontage.py/road_width.py, this tool has no background
# worker thread -- run_processing() itself already runs synchronously
# on the main thread with a simple modal progress window (see
# open_progress_window()/update_progress()), not a queue-polling
# pattern. So this check also runs synchronously, called directly from
# on_run() right before Run actually starts -- same adaptation already
# applied in road_density.py's _check_parcel_density_conflicts() and
# road_surface.py's _check_parcel_surface_conflicts(). Adding threading
# here would be a separate, out-of-scope architectural change.
#
# Read approach: plain gpd.read_file(path), matching road_width.py's own
# canonical _read_gdf_worker() exactly -- no partial/schema-only read
# trick.
#
# A read failure here is NEVER treated as a column-conflict failure --
# it only skips the conflict check for that one source (logged to
# console). The real read inside run_processing() further below remains
# solely responsible for surfacing any genuine read error to the user.
#
# Scope: LOCAL sources only. Database Land Parcel sources are
# explicitly out of scope for this check per the project task
# definition -- callers must not invoke this for "db"-mode sources.
def _check_parcel_terrain_conflicts(local_paths):
    """
    Returns a list of (path, existing_output_cols) tuples -- one entry
    only for local sources where at least one OUTPUT_COLUMN_TARGETS
    match was found. existing_output_cols is the dict returned by
    _detect_existing_output_columns() for that source (target name ->
    actual existing column name, original casing preserved).
    """
    conflicts = []
    for path in local_paths:
        try:
            gdf = gpd.read_file(path)
        except Exception as e:
            print(f"⚠️ Could not read parcel layer to check for existing "
                  f"output column(s): {path}: {e}")
            continue
        existing_output_cols = _detect_existing_output_columns(gdf)
        if existing_output_cols:
            conflicts.append((path, existing_output_cols))
    return conflicts


def process_parcels_fast(parcels, roads, dtm, parcels_crs,
                          slope_col="CAMA_SLOPE", terrain_col="CAMA_TERRAIN",
                          prcl_elev_col="CAMA_PRCL_ELEV", road_elev_col="CAMA_ROAD_ELEV",
                          prcl_road_col="CAMA_PRCL_ROAD", topo_lvl_col="CAMA_TOPO_LVL"):
    """
    slope_col, terrain_col, prcl_elev_col, road_elev_col, prcl_road_col,
    topo_lvl_col : str -- the column names this tool's six computed
        outputs are written to. Each defaults to its standard
        CAMA_-prefixed name (this tool's normal output, matching
        road_width.py's own ROAD_WIDTH -> CAMA_ROAD_WIDTH convention).
        The GUI overrides these per-source when the selected LOCAL
        parcel layer already has existing matching columns (see
        OUTPUT_COLUMN_TARGETS / _detect_existing_output_columns()) --
        the exact existing name/casing is passed here so processing
        writes back into that same column instead of creating a
        hardcoded CAMA_-prefixed duplicate.
    """
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

    parcels[slope_col] = slopes
    parcels[terrain_col] = terrains
    parcels[prcl_elev_col] = prcl_elevs
    parcels[road_elev_col] = road_elevs
    parcels[prcl_road_col] = diffs
    parcels[topo_lvl_col] = topos

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

    # Single-selection architecture: one local file and one DB table
    # may exist in memory at any time. Authority variables -- all GUI
    # labels and run-button state are derived from them, never the reverse.
    parcel_local_path = None   # authority: single local file path
    parcel_db_table   = None   # authority: single DB table name
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
        from sqlalchemy import create_engine as ce
        eng = ce(f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}")
        tables = inspect(eng).get_table_names(schema=creds["schema"])
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


        # ------------------------------------------------------------------
        # PRIORITY 1: column conflict check -- warn if the selected Land
        # Parcel source already has any of the 6 output columns
        # (CAMA_SLOPE, CAMA_TERRAIN, CAMA_PRCL_ELEV, CAMA_ROAD_ELEV,
        # CAMA_PRCL_ROAD, CAMA_TOPO_LVL). Shown before the file-conflict
        # dialog so the user can decide whether to proceed at all before
        # being asked about filename conflicts. Declining cancels the
        # run entirely; main window stays open. LOCAL sources only --
        # Database Land Parcel sources are explicitly out of scope.
        # ------------------------------------------------------------------
        global parcel_output_column_overrides
        if parcel_source_type.get() == "local":
            # parcel_local_path is guaranteed non-None here -- validation
            # above already returned if it was falsy.
            conflicts = _check_parcel_terrain_conflicts([parcel_local_path])
            if conflicts:
                lines = "\n".join(
                    f"- '{os.path.basename(path)}': found "
                    + ", ".join(
                        f"'{existing_name}' column (for {target})"
                        for target, existing_name in existing_output_cols.items()
                    )
                    for path, existing_output_cols in conflicts
                )
                proceed = messagebox.askyesno(
                    "Existing output column(s) found",
                    f"{lines}\n\n"
                    "Processing will overwrite the existing column(s) with the "
                    "newly computed values. The column name(s) will not change.\n\n"
                    "Proceed?"
                )
                if not proceed:
                    print("Run cancelled by user (existing output column(s) found).")
                    return
                # Preserve each source's existing column name(s)/casing
                # exactly -- e.g. a detected "caMA_SLOPE" is written back
                # to "caMA_SLOPE", not a hardcoded "CAMA_SLOPE" -- so no
                # duplicate column is ever created regardless of the
                # existing casing. A source with no entry here (no
                # conflict was found) simply uses the default names in
                # process_parcels_fast() below.
                parcel_output_column_overrides = dict(conflicts)
            else:
                parcel_output_column_overrides = {}
        else:
            parcel_output_column_overrides = {}

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
        run_processing(root, overwrite_mode)

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
        has_parcel = bool(parcel_local_path) if parcel_source_type.get() == "local" else bool(parcel_db_table)
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
def run_processing(app_root, overwrite_mode=None):
    # overwrite_mode: passed from on_run(). Root cause of original bug:
    # no parameter existed, so overwrite_mode was unbound inside this
    # function, causing a NameError whenever a file conflict existed.
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

            # Preserves each source's existing output column name(s)/
            # casing exactly, if a conflict was detected and confirmed
            # in on_run() -- e.g. a detected "caMA_SLOPE" is written
            # back to "caMA_SLOPE", not a hardcoded "CAMA_SLOPE".
            # Defaults to the standard CAMA_-prefixed name for any
            # output this source has no override for. Always {} for
            # DB-sourced parcels (column-conflict check is LOCAL-only),
            # so this naturally falls back to every default in that
            # case.
            output_col_overrides = parcel_output_column_overrides.get(src, {})
            slope_col = output_col_overrides.get("CAMA_SLOPE", "CAMA_SLOPE")
            terrain_col = output_col_overrides.get("CAMA_TERRAIN", "CAMA_TERRAIN")
            prcl_elev_col = output_col_overrides.get("CAMA_PRCL_ELEV", "CAMA_PRCL_ELEV")
            road_elev_col = output_col_overrides.get("CAMA_ROAD_ELEV", "CAMA_ROAD_ELEV")
            prcl_road_col = output_col_overrides.get("CAMA_PRCL_ROAD", "CAMA_PRCL_ROAD")
            topo_lvl_col = output_col_overrides.get("CAMA_TOPO_LVL", "CAMA_TOPO_LVL")

            result = process_parcels_fast(
                parcels, roads, dtm, parcels_crs,
                slope_col=slope_col, terrain_col=terrain_col,
                prcl_elev_col=prcl_elev_col, road_elev_col=road_elev_col,
                prcl_road_col=prcl_road_col, topo_lvl_col=topo_lvl_col
            )

            update_progress("Saving output...")
            if output_mode[0] == "local":
                desired_base_name = name
                candidate_path = os.path.join(output_mode[1], f"{desired_base_name}.gpkg")
                had_conflict = os.path.exists(candidate_path)
                if had_conflict and overwrite_mode == "new":
                    base_name = resolve_output_base_name(output_mode[1], desired_base_name)
                else:
                    base_name = desired_base_name
                out = os.path.join(output_mode[1], f"{base_name}.gpkg")
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
        messagebox.showinfo("Success", "Terrain processing complete!")

    except Exception as e:
        close_progress_window()
        messagebox.showerror("Error", f"Processing failed:\n{str(e)}")


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