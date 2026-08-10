import geopandas as gpd
import numpy as np
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox
from shapely.geometry import Polygon, MultiPolygon
import math
import os
import subprocess
import json
import psycopg2
from sqlalchemy import create_engine, inspect, text
from shapely.validation import make_valid
import re
import threading
import queue
import time

from utils.table_name_matching import normalize_name, find_matching_tables
from utils.resource_path import resource_path
from utils.db_discovery import load_db_credentials, fetch_tables
from utils.column_detection import detect_existing_output_columns
from utils.window_icon import apply_icon

# ============================
# FORCE WINDOWS APP ICON
# ============================
import ctypes
import sys

def set_app_user_model_id():
    appid = u"BLGF.CAMA.Tools.2025"
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(appid)

set_app_user_model_id()


# === Global Mapper EXE and Icon Paths
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"

barangay_source = None
output_mode = None

# ========================= EXISTING OUTPUT-COLUMN CONFLICT DETECTION =========================
# OUTPUT_COLUMN_TARGETS: this tool's eight output column names, checked
# for pre-existing conflicts in a selected LOCAL Land Parcel source (see
# _check_parcel_shape_conflicts() below, and the combined dialog in
# on_run()). Mirrors road_frontage.py's / terrain.py's
# OUTPUT_COLUMN_TARGETS exactly: ALL eight are checked, not just
# PP_RATIO/LOT_SHAPE -- they are one feature set computed together in
# the same run (confirmed decision: all 8 get the CAMA_ prefix, not
# just the two "headline" columns), so a source with (for example) an
# existing CAMA_TRIANGLE column but no existing CAMA_PP_RATIO column
# still needs a conflict warning, to avoid ending up with an old
# CAMA_TRIANGLE value sitting alongside a freshly-computed CAMA_PP_RATIO
# from a DIFFERENT run/computation -- an inconsistent, misleading
# combination.
#
# Cross-tool CAMA_ prefix standard: every column this tool CREATES gets
# a "CAMA_" prefix -- matches road_width.py's own CAMA_ROAD_WIDTH
# convention. These targets check for the NEW, prefixed names ONLY --
# never the OLD, non-prefixed names (e.g. a plain "LOT_SHAPE" column
# left over from a pre-CAMA_-prefix version of this tool). This tool
# never auto-detects, auto-removes, or auto-overwrites an old,
# non-prefixed column -- if one exists, it is simply left alone,
# untouched, and a NEW CAMA_-prefixed column is created alongside it.
# Only conflicts against the NEW naming scheme are ever surfaced to the
# user.
#
# Matching is EXACT (case-insensitive) -- "CAMA_LOT_SHAPE" vs
# "LOT_SHAPE_OLD" is not a match; only "cama_lot_shape"/"CAMA_LOT_SHAPE"/
# "Cama_Lot_Shape"/etc. (same letters, any casing) count as the same
# column.
OUTPUT_COLUMN_TARGETS = (
    "CAMA_PP_RATIO", "CAMA_VTX_COUNT", "CAMA_ANGS_TXT",
    "CAMA_TRIANGLE", "CAMA_RECTANGLE", "CAMA_L_SHAPED",
    "CAMA_OTHERS", "CAMA_LOT_SHAPE",
)

# parcel_output_column_overrides: {path_or_table: {"CAMA_PP_RATIO":
# name, ...}} -- for any Land Parcel source (Local file OR Database
# table) where one or more pre-existing CAMA_-prefixed output columns
# were detected (see _check_parcel_shape_conflicts() below) and the
# user confirmed proceeding at Run time. Read by run_processing() and
# resolved into the eight individual *_col keyword arguments passed to
# compute_ppr_and_lot_shape_gdf() -- matches the exact same
# override-storage-as-dict / function-signature-as-individual-kwargs
# split already established in terrain.py and road_frontage.py, so the
# tool writes back into the EXACT existing column(s) (preserving
# original casing) instead of always writing hardcoded "CAMA_*" names.
# A source with no entry here (or a target missing from its entry) uses
# that target's default CAMA_ name.
parcel_output_column_overrides = {}

# ---------------- Geometry Fix ----------------
def fix_geometry(geom):
    if geom is None or geom.is_empty: return None
    try:
        if not geom.is_valid:
            geom = geom.buffer(0)
        if not geom.is_valid:
            geom = make_valid(geom)
        return geom if not geom.is_empty else None
    except:
        return None

# ---------------- Helpers ----------------
def angle_between(p1, p2, p3):
    v1 = (p1[0] - p2[0], p1[1] - p2[1])
    v2 = (p3[0] - p2[0], p3[1] - p2[1])
    dot = v1[0]*v2[0] + v1[1]*v2[1]
    det = v1[0]*v2[1] - v1[1]*v2[0]
    angle_rad = math.atan2(det, dot)
    angle_deg = math.degrees(angle_rad)
    return round(angle_deg + 360 if angle_deg < 0 else angle_deg, 2)

def vertex_angles(polygon: Polygon):
    coords = list(polygon.exterior.coords)
    if coords[0] == coords[-1]: coords = coords[:-1]
    cleaned = [coords[0]]
    for pt in coords[1:]:
        if math.dist(pt, cleaned[-1]) != 0:
            cleaned.append(pt)
    angles = []
    n = len(cleaned)
    if n < 3: return angles
    for i in range(n):
        p1, p2, p3 = cleaned[i-1], cleaned[i], cleaned[(i+1) % n]
        if math.dist(p2,p3)==0: continue
        angles.append(angle_between(p1,p2,p3))
    return angles

def classify_lot_shape(angles):
    low_angles = [a for a in angles if a <= 169]
    rightish   = [a for a in angles if 170 <= a <= 190]
    obtuse     = [a for a in angles if 190 < a < 260]
    l_cands    = [a for a in angles if 260 <= a <= 280]
    if len(l_cands)==1: return "L_SHAPED"
    elif len(l_cands)>1: return "OTHERS"
    if len(angles)==3 and len(low_angles)==3: return "TRIANGLE"
    if len(low_angles)==3 and len(obtuse)==0: return "TRIANGLE"
    if len(angles)==4 and len(low_angles)==4: return "RECTANGLE"
    elif len(angles)>4:
        if len(low_angles)==4 and all(170<=a<=190 for a in rightish) and len(obtuse)==0:
            return "RECTANGLE"
    return "OTHERS"

def largest_polygon(geom):
    if isinstance(geom, Polygon): return geom
    if isinstance(geom, MultiPolygon) and len(geom.geoms)>0:
        return max(geom.geoms, key=lambda g:g.area)
    return None

def auto_utm_epsg_from_gdf(gdf):
    """
    Choose the UTM zone EPSG from the bbox-midpoint longitude of the
    input GeoDataFrame. UTM (not PRS92) is intentionally kept here --
    the Polsby-Popper ratio computed downstream is largely invariant
    under the locally uniform scale distortions expected for ordinary
    cadastral parcels (both UTM and PRS92 are Transverse Mercator
    family, locally conformal projections -- area and perimeter scale
    by k^2 and k respectively under a locally uniform scale factor k,
    which cancels out in the area/perimeter^2 ratio), so switching CRS
    systems has no established accuracy benefit for this computation
    and was not pursued.

    Uses total_bounds, not a unioned-geometry centroid -- unary_union.centroid
    is a known source of GEOS TopologyExceptions on real-world cadastral
    data with invalid geometries.
    """
    if gdf is None or gdf.empty or not gdf.geometry.notna().any():
        raise ValueError("No valid (non-empty) GeoDataFrame provided for UTM zone detection.")

    g = gdf if gdf.crs is not None else gdf.set_crs(epsg=4326, allow_override=True)
    epsg = g.crs.to_epsg()
    g_wgs84 = g.to_crs(epsg=4326) if epsg != 4326 else g

    bounds = g_wgs84.total_bounds
    if np.isnan(bounds).any():
        raise ValueError("Cannot determine UTM zone because the Land Parcel layer contains no valid geometry.")

    lon = (bounds[0] + bounds[2]) / 2
    zone = int((lon + 180) // 6) + 1
    return 32600 + zone

# ---------------- Core ----------------
# ========================= PARCEL COLUMN-CONFLICT CHECK =========================
# _check_parcel_shape_conflicts(): checks the selected Land Parcel
# source -- Local file OR Database table (extended to cover both as
# part of Fix 3; previously LOCAL-only) -- for pre-existing columns
# matching any of OUTPUT_COLUMN_TARGETS -- this tool is about to write
# its eight computed shape/compactness columns into those columns, and
# on_run() below shows a combined confirmation dialog before
# proceeding, regardless of which source type was selected.
#
# Unlike road_frontage.py/road_width.py, this tool has no background
# worker thread -- run_processing() runs synchronously on the main
# thread (on_run() validates, destroys the window, then calls
# run_processing() directly). So this check also runs synchronously,
# called directly from on_run() right before Run actually starts --
# same adaptation already applied in road_density.py, road_surface.py,
# and terrain.py. Adding threading here would be a separate,
# out-of-scope architectural change.
#
# Read approach: plain gpd.read_file(path) for a Local source, matching
# road_width.py's own canonical _read_gdf_worker() exactly -- no
# partial/schema-only read trick. For a Database source,
# read_postgis_clean() is used instead, loading its own creds/schema/
# engine (self-contained, matching the pattern already used by
# on_run()'s PRIORITY 3 block).
#
# A read failure here is NEVER treated as a column-conflict failure --
# it only skips the conflict check for that one source (logged to
# console). The real read inside run_processing() further below remains
# solely responsible for surfacing any genuine read error to the user.
def _check_parcel_shape_conflicts(sources, source_type):
    """
    Returns a list of (path_or_table, existing_output_cols) tuples on a
    SUCCESSFUL read/check -- one entry only for sources where at least
    one OUTPUT_COLUMN_TARGETS match was found; an empty list means the
    check succeeded and found no conflict. existing_output_cols is the
    dict returned by detect_existing_output_columns() for that source
    (target name -> actual existing column name, original casing
    preserved). Returns None if credentials could not be loaded, or if
    ANY source failed to read -- this is a REQUIRED distinction, not
    cosmetic: an empty list means "verified, no conflict", while None
    means "could not verify at all".

    source_type: "local" or "db" -- dispatches to gpd.read_file() or
    read_postgis_clean() respectively.
    """
    conflicts = []
    engine = None
    schema = None
    if source_type == "db":
        creds = load_db_credentials()
        if not creds:
            print("⚠️ Could not load DB credentials to check for existing "
                  "output column(s).")
            return None
        schema = creds["schema"]
        engine = create_engine(
            f"postgresql://{creds['username']}:{creds['password']}@"
            f"{creds['host']}:{creds['port']}/{creds['database']}"
        )
    for path_or_table in sources:
        try:
            if source_type == "local":
                gdf = gpd.read_file(path_or_table)
            else:
                gdf = read_postgis_clean(path_or_table, engine, schema)
        except Exception as e:
            print(f"⚠️ Could not read parcel layer to check for existing "
                  f"output column(s): {path_or_table}: {e}")
            return None
        existing_output_cols = detect_existing_output_columns(gdf, OUTPUT_COLUMN_TARGETS)
        if existing_output_cols:
            conflicts.append((path_or_table, existing_output_cols))
    return conflicts


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


def _write_gpkg(gdf, path):
    """
    Writes a GeoDataFrame to a .gpkg file, atomically.

    Why atomicity is necessary here specifically: the previous version
    of this function deleted any pre-existing file at `path` FIRST,
    then wrote the new content -- necessary because GeoPackage is a
    SQLite-based container that can hold multiple named layers, and
    calling gdf.to_file(path, driver="GPKG") when `path` already exists
    does NOT simply replace its contents; pyogrio/GDAL tries to create
    a new layer inside the existing file and fails with "Layer <name>
    already exists, CreateLayer failed" if a layer of that name is
    already there (confirmed reproduced when a user chose "Overwrite"
    in ask_overwrite_dialog() -- crashed the whole run with no success
    dialog and no clear message, just a console traceback invisible in
    the compiled EXE).

    But delete-then-write has its own, worse failure mode: if anything
    interrupts the process between the delete and the write completing
    (a crash, the machine losing power, disk full mid-write), the
    result isn't a corrupted file -- there is NO FILE AT ALL at `path`
    anymore, having deleted the original with nothing to show for it.

    This version instead writes to a temporary file first, VERIFIES
    that file is actually readable back (a write that raised no
    exception but produced something GDAL itself can't re-open is
    exactly the failure this guards against), and only then atomically
    replaces the destination via os.replace() -- which is atomic on
    the same filesystem on both Windows and POSIX, unlike
    os.remove()+os.rename(): there is no window where `path` doesn't
    exist. If ANY step before the final os.replace() fails, `path` is
    left completely untouched, exactly as if this call never happened.
    """
    tmp_path = f"{os.path.splitext(path)[0]}.tmp{os.path.splitext(path)[1]}"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    gdf.to_file(tmp_path, driver="GPKG")

    try:
        verify_gdf = gpd.read_file(tmp_path)
        if len(verify_gdf) != len(gdf):
            raise ValueError(
                f"Row count mismatch after write: expected {len(gdf)}, "
                f"got {len(verify_gdf)}."
            )
    except Exception as e:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise RuntimeError(
            f"Could not verify the written file before replacing the "
            f"destination -- destination left unchanged. Details: {e}"
        )

    os.replace(tmp_path, path)


def ask_overwrite_dialog(parent, conflicting_names):
    result = {"choice": "cancel"}
    dialog = tk.Toplevel(parent)
    apply_icon(dialog, "landshape.ico")
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


def confirm_db_overwrite_dialog(parent, table_name):
    """
    Shown when find_matching_tables() returns EXACTLY ONE candidate for
    the DB-output destination table. Asks the user to confirm before
    overwriting that specific table -- fuzzy matching only PROPOSES a
    candidate (see find_matching_tables()'s own docstring); this dialog
    is the actual safety check before anything is overwritten.

    Returns True (Yes -- proceed with overwriting table_name) or False
    (No, or the dialog was closed -- caller must treat this as a full
    cancel, not "create new" -- there is no "create new" for DB output).
    """
    result = {"confirmed": False}

    dialog = tk.Toplevel(parent)
    apply_icon(dialog, "landshape.ico")
    dialog.title("LAND SHAPE COMPACTNESS TOOL")
    dialog.resizable(False, False)
    dialog.grab_set()
    dialog.deiconify()
    dialog.lift()
    dialog.focus_force()
    dialog.attributes("-topmost", True)
    dialog.after(100, lambda: dialog.attributes("-topmost", False))

    def choose(confirmed):
        result["confirmed"] = confirmed
        dialog.destroy()

    dialog.protocol("WM_DELETE_WINDOW", lambda: choose(False))

    btn_frame = tk.Frame(dialog)
    btn_frame.pack(side="bottom", fill="x", pady=(4, 12))
    tk.Button(btn_frame, text="Yes", width=14, cursor="hand2",
              command=lambda: choose(True)).pack(side="left", padx=(16, 4))
    tk.Button(btn_frame, text="No", width=14, cursor="hand2",
              command=lambda: choose(False)).pack(side="left", padx=(4, 16))

    tk.Label(
        dialog, text="Found existing table:",
        font=("Segoe UI", 10, "bold"), anchor="w"
    ).pack(fill="x", padx=16, pady=(16, 4))

    tk.Label(
        dialog, text=table_name, anchor="w", font=("Segoe UI", 9)
    ).pack(fill="x", padx=16, pady=(0, 12))

    tk.Label(dialog, text="Overwrite this table?", anchor="w"
             ).pack(fill="x", padx=16, pady=(0, 16))

    dialog.update_idletasks()
    req_w = max(dialog.winfo_reqwidth(), 360)
    req_h = dialog.winfo_reqheight()
    sw = dialog.winfo_screenwidth()
    sh = dialog.winfo_screenheight()
    x = (sw - req_w) // 2
    y = (sh - req_h) // 2
    dialog.geometry(f"{req_w}x{req_h}+{x}+{y}")

    dialog.wait_window()
    return result["confirmed"]


def choose_db_overwrite_dialog(parent, candidates):
    """
    Shown when find_matching_tables() returns MORE THAN ONE candidate
    for the DB-output destination table -- e.g. both "landparcel_draft"
    and "landparcel_final" exist and both fuzzy-match the incoming
    filename. Lets the user pick exactly which one to overwrite via
    radio buttons; the FIRST candidate in the list is pre-selected by
    default.

    Returns the chosen table name, or None if the user cancelled (must
    be treated as a full cancel by the caller -- there is no "create
    new" for DB output).
    """
    result = {"chosen": None}
    selected = tk.StringVar(value=candidates[0])

    dialog = tk.Toplevel(parent)
    apply_icon(dialog, "landshape.ico")
    dialog.title("LAND SHAPE COMPACTNESS TOOL")
    dialog.resizable(False, False)
    dialog.grab_set()
    dialog.deiconify()
    dialog.lift()
    dialog.focus_force()
    dialog.attributes("-topmost", True)
    dialog.after(100, lambda: dialog.attributes("-topmost", False))

    def choose(confirm):
        result["chosen"] = selected.get() if confirm else None
        dialog.destroy()

    dialog.protocol("WM_DELETE_WINDOW", lambda: choose(False))

    btn_frame = tk.Frame(dialog)
    btn_frame.pack(side="bottom", fill="x", pady=(4, 12))
    tk.Button(btn_frame, text="Confirm", width=14, cursor="hand2",
              command=lambda: choose(True)).pack(side="left", padx=(16, 4))
    tk.Button(btn_frame, text="Cancel", width=14, cursor="hand2",
              command=lambda: choose(False)).pack(side="left", padx=(4, 16))

    tk.Label(
        dialog, text="Multiple possible matches found.",
        font=("Segoe UI", 10, "bold"), anchor="w"
    ).pack(fill="x", padx=16, pady=(16, 4))

    tk.Label(
        dialog, text="Select the table to overwrite:", anchor="w"
    ).pack(fill="x", padx=16, pady=(0, 8))

    radio_frame = tk.Frame(dialog)
    radio_frame.pack(fill="x", padx=16, pady=(0, 16))
    for name in candidates:
        tk.Radiobutton(
            radio_frame, text=name, variable=selected, value=name,
            anchor="w"
        ).pack(fill="x", anchor="w")

    dialog.update_idletasks()
    req_w = max(dialog.winfo_reqwidth(), 360)
    req_h = dialog.winfo_reqheight()
    sw = dialog.winfo_screenwidth()
    sh = dialog.winfo_screenheight()
    x = (sw - req_w) // 2
    y = (sh - req_h) // 2
    dialog.geometry(f"{req_w}x{req_h}+{x}+{y}")

    dialog.wait_window()
    return result["chosen"]



def compute_ppr_and_lot_shape_gdf(gdf,
        pp_ratio_col="CAMA_PP_RATIO", vtx_count_col="CAMA_VTX_COUNT",
        angs_txt_col="CAMA_ANGS_TXT", triangle_col="CAMA_TRIANGLE",
        rectangle_col="CAMA_RECTANGLE", l_shaped_col="CAMA_L_SHAPED",
        others_col="CAMA_OTHERS", lot_shape_col="CAMA_LOT_SHAPE",
        progress=None):
    """
    progress : optional callable progress(message, value=None, maximum=None),
    called from inside the per-feature classification loop below (never
    from anywhere else in this function). Optional and defaults to None
    so this function's existing signature/behavior is unchanged for any
    call site that doesn't pass it -- added as part of this tool's
    Progress Event Protocol v9 migration (see run_processing() below).
    pp_ratio_col, vtx_count_col, angs_txt_col, triangle_col,
    rectangle_col, l_shaped_col, others_col, lot_shape_col : str -- the
    column names this tool's eight computed outputs are written to.
    Each defaults to its standard CAMA_-prefixed name (this tool's
    normal output, matching road_width.py's own ROAD_WIDTH ->
    CAMA_ROAD_WIDTH convention). The GUI overrides these per-source when
    the selected LOCAL parcel layer already has existing matching
    columns (see OUTPUT_COLUMN_TARGETS / _detect_existing_output_columns())
    -- the exact existing name/casing is passed here so processing
    writes back into that same column instead of creating a hardcoded
    CAMA_-prefixed duplicate.

    classify_lot_shape() itself is UNCHANGED -- it still returns the
    bare internal labels "TRIANGLE"/"RECTANGLE"/"L_SHAPED"/"OTHERS".
    Those bare labels are used here only as an internal lookup key
    (shape_col_map below) to pick which of the four one-hot *_col
    columns gets set to 1 -- they are never written to any column
    as-is. The lot_shape_col VALUE (not just the column NAME) is
    deliberately prefixed too, per explicit project decision: it is
    written as "CAMA_TRIANGLE"/"CAMA_RECTANGLE"/"CAMA_L_SHAPED"/
    "CAMA_OTHERS", not the bare label -- confirmed: no other tool in
    this project reads/depends on this tool's LOT_SHAPE values, so this
    is a safe, isolated change.
    """
    # Internal label (from classify_lot_shape()/the invalid-geometry
    # fallback) -> which one-hot *_col column to set to 1. Keys are the
    # bare internal labels this function has always used internally;
    # values are this call's resolved, possibly-overridden column names.
    shape_col_map = {
        "TRIANGLE": triangle_col,
        "RECTANGLE": rectangle_col,
        "L_SHAPED": l_shaped_col,
        "OTHERS": others_col,
    }

    # Save the original CRS
    original_crs = gdf.crs

    # Ensure we work in projected CRS
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326, allow_override=True)
    if gdf.crs.is_geographic:
        epsg = auto_utm_epsg_from_gdf(gdf)
        gdf = gdf.to_crs(epsg=epsg)

    # Geometry repair is scoped to this local Series only -- used below
    # for area/perimeter/vertex-angle computation, never written back
    # into gdf's own geometry column. The exported output keeps each
    # parcel's original, untouched shape, even if invalid.
    #
    # Repair is genuinely needed here (unlike, say, a centroid-only
    # computation elsewhere in this project) -- confirmed empirically:
    # a self-intersecting rectangle-with-digitizing-fold test case
    # classified as OTHERS with PP_RATIO 0.27 on the raw geometry, vs
    # RECTANGLE with PP_RATIO 0.96 after repair, because vertex_angles()
    # reads the exterior ring's raw coordinate sequence directly.
    fixed_geoms = gdf.geometry.apply(fix_geometry)

    # ---- Do calculations in projected CRS, using the repaired geometry ----
    area = fixed_geoms.area
    perimeter = fixed_geoms.length
    gdf[pp_ratio_col] = ((4 * np.pi * area) / (perimeter ** 2)).round(2)

    gdf[vtx_count_col] = 0
    gdf[angs_txt_col] = ""

    for col in [triangle_col, rectangle_col, l_shaped_col, others_col]:
        if col not in gdf.columns:
            gdf[col] = 0
    gdf[lot_shape_col] = ""

    # .items() yields gdf's actual index LABELS (not positions), which
    # gdf.at[] requires. enumerate() gives positional 0..N-1 instead --
    # if gdf's index has any gaps (e.g. from upstream row filtering),
    # gdf.at[label, ...] with a positional number that isn't an actual
    # label silently creates a brand-new row full of NaNs rather than
    # raising an error. Confirmed empirically. Independent of the
    # geometry-repair change above -- this indexing correctness issue
    # applies regardless of what fixed_geoms contains.
    #
    # total/enumerate(..., start=1): added for the optional progress
    # callback below only. idx/geom themselves are completely unaffected
    # -- enumerate() here just wraps fixed_geoms.items() to also yield a
    # 1-based running count `i`; it does not change what idx/geom bind
    # to on each iteration, so the gdf.at[idx, ...] label-based indexing
    # this loop depends on (see comment above) is untouched.
    total = len(fixed_geoms)
    for i, (idx, geom) in enumerate(fixed_geoms.items(), start=1):
        if progress:
            progress(f"Classifying feature {i}/{total}", i, total)
        poly = largest_polygon(geom)
        if poly is None:
            # Temporary behavior: parcels whose geometry cannot be
            # repaired are retained in the output (never dropped --
            # every input row must appear exactly once in the output)
            # with LOT_SHAPE="CAMA_OTHERS" and PP_RATIO=NaN (area/perimeter
            # above are NaN for a None entry in fixed_geoms, so
            # PP_RATIO is already NaN for this row without extra code
            # here) until the business rule for a dedicated
            # "INVALID_GEOMETRY" classification is finalized with the
            # team lead. The parcel's OWN geometry in the output stays
            # exactly as originally read -- only this repaired local
            # `poly` failed, not gdf's own geometry column.
            gdf.at[idx, triangle_col] = 0
            gdf.at[idx, rectangle_col] = 0
            gdf.at[idx, l_shaped_col] = 0
            gdf.at[idx, others_col] = 1
            gdf.at[idx, lot_shape_col] = "CAMA_OTHERS"
            continue

        angles = vertex_angles(poly)
        shape_type = classify_lot_shape(angles)

        gdf.at[idx, triangle_col] = 0
        gdf.at[idx, rectangle_col] = 0
        gdf.at[idx, l_shaped_col] = 0
        gdf.at[idx, others_col] = 0
        gdf.at[idx, shape_col_map[shape_type]] = 1
        gdf.at[idx, lot_shape_col] = f"CAMA_{shape_type}"
        gdf.at[idx, vtx_count_col] = len(angles)
        gdf.at[idx, angs_txt_col] = ",".join(map(str, angles))

    # ✅ Reproject back to original CRS before returning
    if original_crs:
        gdf = gdf.to_crs(original_crs)

    return gdf

def open_in_global_mapper(path):
    if os.path.exists(GM_EXE_PATH) and os.path.exists(path):
        subprocess.Popen([GM_EXE_PATH, path], shell=True)

# ---------------- DB Helpers ----------------
def get_geometry_column(table, engine, schema):
    with engine.connect() as conn:
        row=conn.execute(text("""
            SELECT f_geometry_column FROM geometry_columns
            WHERE f_table_schema=:schema AND f_table_name=:table
        """),{"schema":schema,"table":table}).fetchone()
        return row[0] if row else None

def read_postgis_clean(table,engine,schema):
    geom_col=get_geometry_column(table,engine,schema)
    insp=inspect(engine)
    cols=[c['name'] for c in insp.get_columns(table,schema=schema) if c['name']!=geom_col]
    col_str=", ".join([f'"{c}"' for c in cols]) if cols else ""
    q=f'SELECT {col_str+", " if col_str else ""}"{geom_col}" AS geometry FROM "{schema}"."{table}"'
    return gpd.read_postgis(q,engine,geom_col="geometry")

# ---------------- Tkinter Windows ----------------
def _pick_db_tables(parent, tables, multi, on_select):
    from tkinter import ttk
    picker = tk.Toplevel(parent)
    apply_icon(picker, "landshape.ico")
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


def load_in_global_mapper(filepath):
    try:
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

        import ctypes.wintypes
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        ctypes.windll.user32.EnumWindows(EnumWindowsProc(enum_callback), 0)

        subprocess.Popen([GM_EXE_PATH, filepath])
        print(f"🗺️ Sent to Global Mapper: {filepath}")

    except Exception as e:
        print(f"⚠️ Could not open in Global Mapper: {e}")


def open_main_window(root):
    from tkinter import ttk
    win = tk.Toplevel(root)
    apply_icon(win, "landshape.ico")
    win.title("Lot Shape Tool")
    win.resizable(False, False)
    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))

    # ── state ────────────────────────────────────────────────────
    parcel_source_type = tk.StringVar(master=win, value="local")
    output_dest_type   = tk.StringVar(master=win, value="local")
    # Single-selection architecture: one local file and one DB table
    # may exist in memory at any time. Authority variables -- all GUI
    # labels and run-button state are derived from them, never the reverse.
    parcel_local_path = None   # authority: single local file path
    parcel_db_table   = None   # authority: single DB table name
    output_local_dir   = tk.StringVar(master=win)

    # Land Parcel existing-output-column check: detect-on-select,
    # matching the pattern established in lot_location.py/road_width.py/
    # road_frontage.py/road_density.py/road_surface.py/
    # influence_to_map.py. Deliberately does NOT cache the result across
    # calls -- every selection AND every Local/Database toggle triggers
    # a fresh read (see group-05-cache-removal-analysis.md). What IS
    # still remembered per mode is only WHICH file/table is selected
    # (parcel_local_path / parcel_db_table above), a separate concern.
    # Multi-target (8 targets, OUTPUT_COLUMN_TARGETS): each conflict
    # entry is (path_or_table, {target: existing_col_name}), a dict.
    parcel_is_reading = False
    parcel_existing_output_conflicts = []   # [(path_or_table, {target: col}), ...]

    # run_status_var: drives the always-visible status label under the
    # Run button ("Please select ..." / "Ready to run.") and mirrors
    # whether the Run button itself is enabled. Updated by
    # _update_run_button_state() below.
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
    parcel_radio_local = tk.Radiobutton(radio_row, text="Local File",
                   variable=parcel_source_type, value="local",
                   command=lambda: _toggle_parcel())
    parcel_radio_local.pack(side="left")
    parcel_radio_db = tk.Radiobutton(radio_row, text="Database Table",
                   variable=parcel_source_type, value="db",
                   command=lambda: _toggle_parcel())
    parcel_radio_db.pack(side="left", padx=(12, 0))

    parcel_files_var = tk.StringVar(master=win, value="No file selected")
    parcel_db_label  = tk.StringVar(master=win, value="No table selected")

    parcel_action_row = tk.Frame(parcel_frame)
    parcel_action_row.pack(fill="x", pady=2)

    parcel_lbl = tk.Label(parcel_action_row, textvariable=parcel_files_var,
                          fg="gray", anchor="w", width=42)
    parcel_lbl.pack(side="left")

    parcel_btn = tk.Button(parcel_action_row, text="Browse…", width=10)
    parcel_btn.pack(side="left", **PAD)

    def _set_parcel_reading_state(is_reading):
        """
        Toggle GUI responsiveness while the Land Parcel existing-output-
        column check is in progress. Disables the parcel Browse/Select
        button and the Local/Database radio buttons for the duration of
        the read, preventing a second, concurrent read of the same
        selection.

        The "Reading..." indicator reuses the EXISTING label (parcel_lbl)
        in place -- via whichever StringVar is currently bound to it
        (parcel_files_var for Local, parcel_db_label for Database, per
        _toggle_parcel()'s textvariable swap below) -- rather than
        packing/unpacking a separate status widget, which would reflow
        every widget below it and cause a visible layout jump. Matches
        the corrected pattern already used by lot_location.py/
        road_width.py/road_frontage.py/road_density.py/road_surface.py/
        influence_to_map.py.
        """
        nonlocal parcel_is_reading
        parcel_is_reading = is_reading
        if is_reading:
            if parcel_source_type.get() == "local":
                parcel_files_var.set("⏳ Reading Land Parcel…")
            else:
                parcel_db_label.set("⏳ Reading Land Parcel…")
            parcel_lbl.config(fg="#b36b00")
            parcel_btn.config(state="disabled")
            parcel_radio_local.config(state="disabled")
            parcel_radio_db.config(state="disabled")
        else:
            # Restore from authority variables -- never from StringVar
            # state -- same pattern _toggle_parcel() already uses below.
            if parcel_source_type.get() == "local":
                parcel_files_var.set(
                    os.path.basename(parcel_local_path) if parcel_local_path
                    else "No file selected"
                )
            else:
                parcel_db_label.set(
                    parcel_db_table if parcel_db_table
                    else "No table selected"
                )
            parcel_lbl.config(fg="gray")
            parcel_btn.config(state="normal")
            parcel_radio_local.config(state="normal")
            parcel_radio_db.config(state="normal")
        _update_run_button_state()

    def _handle_parcel_check_failure(source_type, reason):
        """
        Shared cleanup for both outcomes of a FAILED Land Parcel
        existing-output-column check: a read that never completed
        within 60 seconds ("timeout"), or one that completed with an
        actual read error ("failure" -- see
        _check_parcel_shape_conflicts()'s docstring on why this is
        signaled as None, not an empty list).

        Captures the failed source's display name BEFORE clearing the
        authority variable (needed for the dialog text below), then
        clears ONLY the authority variable for source_type (the mode
        that was actually being read) -- parcel_local_path if source_type
        is "local", parcel_db_table if "db".

        Clearing the authority variable is the entire recovery
        mechanism -- no new "check failed" state is introduced. This
        forces the EXISTING "no source selected -> Run disabled" path
        (_update_run_button_state(), invoked via
        _set_parcel_reading_state(False) below) to handle recovery: the
        display reverts to "No file selected" / "No table selected",
        and the user must select a source again.

        _set_parcel_reading_state(False) is called BEFORE the dialog is
        shown, not after -- messagebox.showerror() is modal and blocks
        here until dismissed, so showing it first would leave the
        "⏳ Reading Land Parcel…" indicator frozen on screen for the
        entire time the dialog is up.
        """
        nonlocal parcel_local_path, parcel_db_table, parcel_existing_output_conflicts

        if source_type == "local":
            failed_name = (os.path.basename(parcel_local_path)
                           if parcel_local_path else "the selected file")
            parcel_local_path = None
        else:
            failed_name = parcel_db_table if parcel_db_table else "the selected table"
            parcel_db_table = None

        parcel_existing_output_conflicts = []

        if reason == "timeout":
            title = "Read Timeout"
            if source_type == "local":
                message = (f'Could not read the selected file "{failed_name}" '
                           f'within 60 seconds.\n\n'
                           f'Please try again or choose a different file.')
            else:
                message = (f'Could not read the selected table "{failed_name}" '
                           f'within 60 seconds.\n\n'
                           f'Please check your database connection and try again.')
        else:  # "failure"
            title = "Read Error"
            if source_type == "local":
                message = (f'Could not read the selected file "{failed_name}".\n\n'
                           f'Please try again or choose a different file.')
            else:
                message = (f'Could not read the selected table "{failed_name}".\n\n'
                           f'Please check your database connection and try again.')

        _set_parcel_reading_state(False)
        messagebox.showerror(title, message, parent=win)

    def _poll_parcel_output_queue(result_queue, source_type, deadline):
        """
        Runs on the main thread via win.after() polling. Picks up the
        conflict list placed on the queue by the background worker, or
        detects a timeout if 60 seconds have elapsed with no result.

        Ordering matters: the queue is ALWAYS checked before the
        deadline -- see road_density.py's identical function for the
        full reasoning (single-threaded Tkinter main loop, fresh
        queue.Queue() per call, no generation counter needed).
        """
        nonlocal parcel_existing_output_conflicts
        if not win.winfo_exists():
            return
        try:
            conflicts = result_queue.get_nowait()
        except queue.Empty:
            if time.time() >= deadline:
                _handle_parcel_check_failure(source_type, "timeout")
            else:
                win.after(100, lambda: _poll_parcel_output_queue(
                    result_queue, source_type, deadline))
            return

        if conflicts is None:
            # Worker signaled a read failure (see
            # _check_parcel_shape_conflicts()'s docstring) -- distinct
            # from an empty list, which means "verified, no conflict".
            _handle_parcel_check_failure(source_type, "failure")
            return

        parcel_existing_output_conflicts = conflicts
        _set_parcel_reading_state(False)

    def _refresh_parcel_output_check():
        """
        Background-checks the currently selected Land Parcel file/table
        for existing OUTPUT_COLUMN_TARGETS columns -- moved here from
        on_run() (Phase A of Group 5's detect-on-select generalization)
        so the check happens immediately on selection/toggle, not only
        when Run Processing is clicked. Reuses
        _check_parcel_shape_conflicts() (defined above, already
        self-contained -- loads its own DB credentials internally) as
        the actual worker logic, just now called on a background thread.
        Gives up after 60 seconds with no result (see
        _poll_parcel_output_queue()) -- a hung read must not leave the
        tool waiting indefinitely.

        Deliberately does NOT cache the result across calls -- every
        call, whether triggered by a fresh Browse/Select or by toggling
        Local <-> Database, always performs a real read. See
        group-05-cache-removal-analysis.md for the full reasoning. What
        IS still remembered across calls is only WHICH file/table is
        selected per mode (parcel_local_path / parcel_db_table) -- a
        separate concern, untouched by this function.
        """
        nonlocal parcel_existing_output_conflicts
        if parcel_is_reading:
            # A check is already in flight — do not start a second,
            # overlapping one (controls are disabled while reading, but
            # this guard is the actual enforcement).
            return

        source_type = parcel_source_type.get()
        sources = (
            [parcel_local_path] if source_type == "local" and parcel_local_path
            else [parcel_db_table] if source_type == "db" and parcel_db_table
            else []
        )

        if not sources:
            # Nothing selected for this mode — nothing to check.
            parcel_existing_output_conflicts = []
            _update_run_button_state()
            return

        result_queue = queue.Queue()

        def worker():
            conflicts = _check_parcel_shape_conflicts(sources, source_type)
            result_queue.put(conflicts)

        deadline = time.time() + 60  # see _poll_parcel_output_queue()
        _set_parcel_reading_state(True)
        threading.Thread(target=worker, daemon=True).start()
        win.after(100, lambda: _poll_parcel_output_queue(
            result_queue, source_type, deadline))

    def browse_parcel_files():
        file = filedialog.askopenfilename(filetypes=[
            ("Shapefiles", "*.shp"), ("GeoPackage", "*.gpkg"), ("All", "*.*")])
        # Cancel returns "" -- do not assign, preserving previous selection.
        if file:
            nonlocal parcel_local_path
            parcel_local_path = file
            parcel_files_var.set(os.path.basename(file))
            # Always checks fresh -- see _refresh_parcel_output_check()
            # docstring: no result is ever cached across calls.
            _refresh_parcel_output_check()
        _update_run_button_state()

    def _on_parcel_db_selected(sel):
        # Only called on confirmed selection -- Cancel never calls on_select,
        # so parcel_db_table retains its previous value automatically.
        nonlocal parcel_db_table
        parcel_db_table = sel[0]
        parcel_db_label.set(sel[0])
        _refresh_parcel_output_check()
        _update_run_button_state()

    def browse_parcel_db():
        creds = load_db_credentials()
        if not creds:
            messagebox.showerror("Error", "Could not load DB credentials.")
            return
        tables = fetch_tables(creds["schema"])
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
        # Switching Local <-> Database does NOT clear the other mode's
        # remembered selection -- that's pre-existing behavior, left
        # untouched. Always re-checks fresh for whichever mode is now
        # active -- no cached result is ever restored (see
        # group-05-cache-removal-analysis.md).
        _refresh_parcel_output_check()
        _update_run_button_state()

    # ── SECTION 2: OUTPUT ────────────────────────────────────────
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
        global barangay_source, output_mode

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
        # Parcel source already has any of the 8 output columns. Shown
        # before the file-conflict dialog so the user can decide whether
        # to proceed at all before being asked about filename conflicts.
        # Declining cancels the run entirely; main window stays open
        # (this block runs before win.destroy() further below).
        #
        # Phase A (Group 5 detect-on-select generalization): this no
        # longer calls _check_parcel_shape_conflicts() synchronously
        # here -- the check already ran in the background the moment the
        # Land Parcel source was selected/toggled (see
        # _refresh_parcel_output_check()). This just consults the
        # already-known result, parcel_existing_output_conflicts.
        # _update_run_button_state() already guarantees Run cannot be
        # reached while parcel_is_reading is True, so this value is
        # guaranteed current for the actively selected source at this
        # point.
        # ------------------------------------------------------------------
        global parcel_output_column_overrides
        conflicts = parcel_existing_output_conflicts
        if conflicts:
            lines = "\n\n".join(
                f"'{os.path.basename(path)}' already has the following column(s):\n"
                + "\n".join(f"  • {existing_name}" for existing_name in existing_output_cols.values())
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
            # exactly -- e.g. a detected "caMA_PP_RATIO" is written
            # back to "caMA_PP_RATIO", not a hardcoded
            # "CAMA_PP_RATIO" -- so no duplicate column is ever
            # created regardless of the existing casing. A source
            # with no entry here (no conflict was found) simply
            # uses the default names in
            # compute_ppr_and_lot_shape_gdf() below.
            parcel_output_column_overrides = dict(conflicts)
        else:
            parcel_output_column_overrides = {}

        # ------------------------------------------------------------------
        # PRIORITY 2: file conflict check -- warn if an output file with
        # the same name already exists in the chosen output folder.
        # Root cause of bug fixed here: overwrite_mode was previously
        # local to on_run() and never reached run_processing(), causing
        # a NameError at runtime whenever a file conflict existed.
        # Fix: pass overwrite_mode explicitly as a parameter.
        # ------------------------------------------------------------------
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

        # PRIORITY 3: DB-output destination table resolution — mirrors
        # PRIORITY 2 above. Resolved here on the main thread, before
        # win.destroy(), so confirm_db_overwrite_dialog() /
        # choose_db_overwrite_dialog() (invoked inside
        # resolve_db_output_table()) still have a live parent window,
        # and a Cancel here leaves the fully-configured win intact
        # instead of forcing a from-scratch reopen. Previously this
        # resolution happened inside run_processing(), which is only
        # ever invoked AFTER win.destroy() -- see Fix 1 root cause.
        # resolve_db_output_table()'s own matching/decision logic is
        # untouched; only the call site moved here. resolved_table_name
        # is handed to run_processing() as an already-validated value —
        # run_processing() no longer re-resolves or re-validates it.
        # resolved_outcome is not threaded through here (same as
        # road_surface.py/road_density.py) because nothing downstream
        # in this file's worker() consumes it -- only resolved_table_name
        # is read (see the table fallback near "Falls back to the old...").
        resolved_table_name = None
        if output_mode[0] == "db":
            _resolve_creds = load_db_credentials()
            if not _resolve_creds:
                return
            _resolve_schema = _resolve_creds["schema"]
            resolved_table_name, _resolved_outcome = resolve_db_output_table(
                win, _resolve_schema, barangay_source
            )
            if resolved_table_name is None:
                print("Run cancelled by user (database output table not confirmed).")
                return

        win.destroy()
        run_processing(root, overwrite_mode, resolved_table_name)

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
        Land Parcel source and an Output destination are both selected.

        Explicit bg/fg/cursor toggling (not just state=) is required:
        Tkinter does NOT automatically gray out a classic tk.Button's
        custom bg/fg when state="disabled", and does not suppress a
        widget's assigned cursor either -- both must be set explicitly
        for each state.
        """
        has_parcel = bool(parcel_local_path) if parcel_source_type.get() == "local" else bool(parcel_db_table)
        has_output = bool(output_local_dir.get()) if output_dest_type.get() == "local" else True

        if parcel_is_reading:
            # Land Parcel existing-column check is still in flight --
            # never allow Run while its result is not yet known (see
            # Section 6's read-outcome invariant, group-05-FINAL-PLAN.md
            # -- an in-progress check must never be silently treated as
            # "no conflict").
            checking_name = (
                os.path.basename(parcel_local_path) if parcel_source_type.get() == "local"
                else parcel_db_table
            ) or "source"
            run_status_var.set(f'Checking "{checking_name}" columns…')
            ready = False
        elif not has_parcel:
            run_status_var.set("Please select a Land Parcel source.")
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
    _toggle_output()
    _update_run_button_state()


# ---------------- Processing ----------------
def resolve_db_output_table(root, schema, barangay_source):
    """
    Determines the DB-output destination table for the Land Parcel
    source, BEFORE any processing or writing starts -- same "resolve
    everything up front" philosophy as ask_overwrite_dialog() (see
    run_processing()). land_shape_compactness.py has no background
    worker thread -- this function is still called once, up front, for
    separation of responsibilities: this function owns ALL user
    interaction and overwrite decisions, so the processing/write logic
    further below never has to ask any UI or overwrite question of its
    own.

    Two cases:
      - DB-source Land Parcel (barangay_source[0] == "db"): always
        writes back to the exact same table it was read from -- no
        matching, no dialog, matches run_processing()'s own pre-
        existing db-source branch (table = table, unchanged).
      - Local-file Land Parcel: fuzzy-matches the filename against
        existing tables via find_matching_tables() (which already
        excludes CAMA_Table, CAMA_Transaction_Log, and any "_VM"
        table), then requires user confirmation before treating a
        match as an overwrite target -- zero candidates skips the
        dialog entirely and creates a new table under the filename.

    Returns (resolved_table_name, resolved_outcome), or (None, None) if
    the user cancelled -- caller must abort the entire run in that
    case, matching ask_overwrite_dialog()'s existing
    cancel-aborts-everything semantics (there is no "create new" choice
    for DB output).
    """
    if barangay_source[0] == "db":
        return barangay_source[1][0], "overwritten"

    desired_name = os.path.splitext(os.path.basename(barangay_source[1][0]))[0]
    all_tables = fetch_tables(schema)
    candidates = find_matching_tables(desired_name, all_tables)

    if len(candidates) == 0:
        return desired_name, "created"
    elif len(candidates) == 1:
        if not confirm_db_overwrite_dialog(root, candidates[0]):
            return None, None
        return candidates[0], "overwritten"
    else:
        chosen = choose_db_overwrite_dialog(root, candidates)
        if chosen is None:
            return None, None
        return chosen, "overwritten"


# ============================================================
# Progress Event Protocol v9 -- this tool's migration.
# ============================================================
# This tool previously had NO background worker thread and NO progress
# dialog at all -- run_processing() ran entirely synchronously on the
# main thread (see run_processing()'s own comments further below).
# Unlike lot_location.py's/road_frontage.py's migrations (pure
# extractions of an EXISTING ProgressWindow into shared code, zero
# behavior change), this is new functionality: a progress dialog is
# added where none existed before. It reuses progress_framework.py's
# PresentationState/ProgressPresentationPolicy/TkinterProgressView
# directly -- no tool-local copies, no new abstraction, same shared
# classes already validated by lot_location.py and road_frontage.py.
#
#   Worker (worker(), inside run_processing())      -> new
#   Main-thread Message Handler (poll_queue())       -> new
#   ProgressWindow                                   -> new (this class)
#
# Deliberately NOT done in this task (see conversation record):
#   - No per-source failure isolation added -- this tool's existing
#     all-or-nothing failure behavior (one exception aborts the whole
#     run) is preserved exactly. Only the ERROR REPORTING changed (an
#     uncaught exception now surfaces as a graceful "error" dialog via
#     the Progress Event Protocol, instead of the previous silent
#     crash with no dialog at all -- unavoidable side effect of moving
#     work onto a background thread, since an uncaught exception on a
#     non-main thread that nobody catches is otherwise simply lost).
#   - The 3 overwrite dialogs in this file (ask_overwrite_dialog,
#     confirm_db_overwrite_dialog, choose_db_overwrite_dialog) are
#     untouched -- any topmost/hiding fix for them is a separate,
#     dedicated follow-up task, not bundled into this migration.
# ============================================================
from tools.progress_framework import (
    PresentationState,
    ProgressPresentationPolicy,
    TkinterProgressView,
)


class ProgressWindow:
    """
    Progress dialog shown while run_processing() works on a background
    thread. Same shape as lot_location.py's/road_frontage.py's own
    ProgressWindow -- status label + determinate progress bar, no
    cancel/stop_flag support. Progress Event Protocol v9 role:
    ProgressWindow is the host, not the decision-maker (see
    ProgressPresentationPolicy / TkinterProgressView, imported from
    progress_framework.py, shared with the other two migrated tools).
    """
    def __init__(self, root, title="Processing"):
        from tkinter import ttk
        self.win = tk.Toplevel(root)
        apply_icon(self.win, "landshape.ico")
        self.win.title(title)
        self.win.minsize(400, 120)
        self.win.resizable(False, False)
        self.status_var = tk.StringVar(master=self.win)
        self.status_var.set("Starting...")
        tk.Label(
            self.win, textvariable=self.status_var, anchor="center",
            justify="left", wraplength=380,
        ).pack(pady=10, padx=10, fill="x")
        self.progress = ttk.Progressbar(self.win, orient="horizontal", mode="determinate", length=350)
        self.progress.pack(pady=10)
        self.win.attributes("-topmost", True)
        self.win.update()

        self.win.focus_force()
        self.win.lift()
        self.win.attributes("-topmost", True)
        self.win.after(100, lambda: self.win.attributes("-topmost", False))

        # Presentation Policy + Tkinter View collaborators (Progress
        # Event Protocol v9), shared with lot_location.py/road_frontage.py
        # via progress_framework.py. Constructed after the widgets they
        # render into already exist.
        self._policy = ProgressPresentationPolicy()
        self._view = TkinterProgressView(self.win, self.status_var, self.progress)

    def update(self, message, value=None, maximum=None):
        state = self._policy.compute(message, value, maximum)
        self._view.render(state)

    def close(self):
        self._view.destroy()


def run_processing(root, overwrite_mode=None, resolved_table_name=None):
    # root: the live top-level window (passed from on_run(); NOT
    # `win`, which is destroyed before run_processing() is ever
    # called -- see on_run()'s win.destroy() immediately before this
    # function's call site). Used as the parent for any dialogs
    # created in this function (currently just
    # resolve_db_output_table()'s DB confirmation dialogs).
    # overwrite_mode: passed from on_run(). Root cause of original bug:
    # no parameter existed, so overwrite_mode was unbound inside this
    # function, causing a NameError whenever a file conflict existed.
    global barangay_source, output_mode
    if not barangay_source or not output_mode:
        messagebox.showerror("Error", "Selections incomplete (Barangay + Output required).")
        return

    creds = load_db_credentials()
    schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    )

    # resolved_table_name: the DB-output destination table. Resolution
    # responsibility now belongs to on_run() (PRIORITY 3), on the main
    # thread, BEFORE win.destroy() -- see Fix 1. By the time it reaches
    # this function it is treated as an already-validated value: either
    # None (local output, or output_mode[0] != "db") or a confirmed
    # table name (DB output, user already had the chance to cancel in
    # on_run()). No re-resolution or re-validation happens here.

    # ============================================================
    # Progress Event Protocol v9 -- this tool's migration.
    # ============================================================
    # Everything ABOVE this point (validation, credential loading,
    # resolve_db_output_table() + its confirmation dialog(s)) is
    # unchanged and stays on the main thread, exactly as before --
    # matches lot_location.py's/road_frontage.py's own convention:
    # Tkinter dialogs must never be shown from a background thread, so
    # anything that can pop one up is resolved here, BEFORE worker()
    # below is ever started.
    #
    # Everything BELOW this point is the exact same two-loop body this
    # function always had (local-source loop, then the separate
    # DB-source loop -- deliberately NOT merged into one loop, per
    # explicit instruction), now wrapped inside a background worker()
    # thread instead of running inline on the main thread. No business
    # logic, read/write logic, or naming/output behavior is changed --
    # only WHERE this code runs and how its progress/completion is
    # reported.
    progress = ProgressWindow(root, "Land Shape Progress")
    q = queue.Queue()

    def worker():
        try:
            def progress_cb(msg, val=None, maxv=None):
                q.put(("update", msg, val, maxv))

            if barangay_source[0] == "local":
                for path in barangay_source[1]:
                    q.put(("update", f"Loading {os.path.basename(path)}", None, None))
                    gdf = gpd.read_file(path)
                    # Row-dropping REMOVED (Phase 1B decision, approved): every
                    # input parcel must appear exactly once in the output. A
                    # parcel whose geometry can't be repaired is no longer
                    # dropped -- compute_ppr_and_lot_shape_gdf() below keeps it
                    # in the output with its ORIGINAL geometry, PP_RATIO=NaN,
                    # and LOT_SHAPE="CAMA_OTHERS" as a temporary placeholder
                    # pending a dedicated "INVALID_GEOMETRY" classification once
                    # that business rule is finalized with the team lead.
                    #
                    # output_col_overrides: preserves each source's existing
                    # output column name(s)/casing exactly, if a conflict was
                    # detected and confirmed in on_run() -- e.g. a detected
                    # "caMA_PP_RATIO" is written back to "caMA_PP_RATIO", not a
                    # hardcoded "CAMA_PP_RATIO". Defaults to the standard
                    # CAMA_-prefixed name for any output this source has no
                    # override for.
                    output_col_overrides = parcel_output_column_overrides.get(path, {})
                    result = compute_ppr_and_lot_shape_gdf(
                        gdf,
                        pp_ratio_col=output_col_overrides.get("CAMA_PP_RATIO", "CAMA_PP_RATIO"),
                        vtx_count_col=output_col_overrides.get("CAMA_VTX_COUNT", "CAMA_VTX_COUNT"),
                        angs_txt_col=output_col_overrides.get("CAMA_ANGS_TXT", "CAMA_ANGS_TXT"),
                        triangle_col=output_col_overrides.get("CAMA_TRIANGLE", "CAMA_TRIANGLE"),
                        rectangle_col=output_col_overrides.get("CAMA_RECTANGLE", "CAMA_RECTANGLE"),
                        l_shaped_col=output_col_overrides.get("CAMA_L_SHAPED", "CAMA_L_SHAPED"),
                        others_col=output_col_overrides.get("CAMA_OTHERS", "CAMA_OTHERS"),
                        lot_shape_col=output_col_overrides.get("CAMA_LOT_SHAPE", "CAMA_LOT_SHAPE"),
                        progress=progress_cb,
                    )
                    if output_mode[0] == "local":
                        desired_base_name = os.path.splitext(os.path.basename(path))[0]
                        candidate_path = os.path.join(output_mode[1], f"{desired_base_name}.gpkg")
                        had_conflict = os.path.exists(candidate_path)
                        if had_conflict and overwrite_mode == "new":
                            base_name = resolve_output_base_name(output_mode[1], desired_base_name)
                        else:
                            base_name = desired_base_name
                        out = os.path.join(output_mode[1], f"{base_name}.gpkg")
                        _write_gpkg(result, out)
                        print(f"✅ Saved {out}")
                        q.put(("open_gm", out, None, None))
                    else:
                        # The actual destination table was already decided by
                        # resolve_db_output_table(), BEFORE this loop even
                        # started -- fuzzy matching + user confirmation already
                        # happened there (see that function's docstring). This
                        # just uses the result. Falls back to the old
                        # filename-lowercased behavior only if
                        # resolved_table_name is somehow None here
                        # (output_mode[0] != "db" can't reach this branch, so
                        # this is just a defensive fallback).
                        local_name = os.path.splitext(os.path.basename(path))[0]
                        table = resolved_table_name if resolved_table_name is not None else local_name.lower()
                        with engine.begin() as conn:
                            result.to_postgis(table, conn, schema=schema, if_exists="replace", index=False)
                        print(f"🔄 Saved to DB: {table}")
            else:
                # Database Land Parcel sources: extended (Fix 3) to
                # respect parcel_output_column_overrides, same as the
                # LOCAL branch above -- preserves the exact existing
                # column casing(s) detected in on_run()'s PRIORITY 1
                # check instead of always defaulting to the eight
                # hardcoded CAMA_-prefixed names.
                for table in barangay_source[1]:
                    q.put(("update", f"Loading DB table {table}", None, None))
                    gdf = read_postgis_clean(table, engine, schema)
                    # Row-dropping REMOVED -- same reasoning as the local-source
                    # branch above.
                    output_col_overrides = parcel_output_column_overrides.get(table, {})
                    result = compute_ppr_and_lot_shape_gdf(
                        gdf,
                        pp_ratio_col=output_col_overrides.get("CAMA_PP_RATIO", "CAMA_PP_RATIO"),
                        vtx_count_col=output_col_overrides.get("CAMA_VTX_COUNT", "CAMA_VTX_COUNT"),
                        angs_txt_col=output_col_overrides.get("CAMA_ANGS_TXT", "CAMA_ANGS_TXT"),
                        triangle_col=output_col_overrides.get("CAMA_TRIANGLE", "CAMA_TRIANGLE"),
                        rectangle_col=output_col_overrides.get("CAMA_RECTANGLE", "CAMA_RECTANGLE"),
                        l_shaped_col=output_col_overrides.get("CAMA_L_SHAPED", "CAMA_L_SHAPED"),
                        others_col=output_col_overrides.get("CAMA_OTHERS", "CAMA_OTHERS"),
                        lot_shape_col=output_col_overrides.get("CAMA_LOT_SHAPE", "CAMA_LOT_SHAPE"),
                        progress=progress_cb,
                    )
                    if output_mode[0] == "local":
                        desired_base_name = table
                        candidate_path = os.path.join(output_mode[1], f"{desired_base_name}.gpkg")
                        had_conflict = os.path.exists(candidate_path)
                        if had_conflict and overwrite_mode == "new":
                            base_name = resolve_output_base_name(output_mode[1], desired_base_name)
                        else:
                            base_name = desired_base_name
                        out = os.path.join(output_mode[1], f"{base_name}.gpkg")
                        _write_gpkg(result, out)
                        print(f"✅ Saved {out}")
                        q.put(("open_gm", out, None, None))
                    else:
                        with engine.begin() as conn:
                            result.to_postgis(table, conn, schema=schema, if_exists="replace", index=False)
                        print(f"🔄 Updated DB table: {table}")

            q.put(("done", "Processing done!", None, None))

        except Exception as e:
            # New: this function had no top-level try/except before --
            # an uncaught exception here previously propagated silently
            # (no graceful dialog). Required by moving to a background
            # thread: an exception on a non-main thread that nobody
            # catches is otherwise simply lost, with no way for the
            # user to ever learn the run failed. This is the "error"
            # kind of the Progress Event Protocol, same as the other
            # three already-migrated tools.
            q.put(("error", str(e), None, None))

    def poll_queue():
        if not root.winfo_exists():
            return
        try:
            while True:
                kind, *rest = q.get_nowait()
                if kind == "update":
                    progress.update(rest[0], rest[1], rest[2])
                elif kind == "open_gm":
                    load_in_global_mapper(rest[0])
                elif kind == "done":
                    progress.close()
                    messagebox.showinfo("Success", rest[0])
                    return
                elif kind == "error":
                    progress.close()
                    messagebox.showerror("Error", rest[0])
                    return
        except queue.Empty:
            pass
        root.after(100, poll_queue)

    threading.Thread(target=worker, daemon=True).start()
    poll_queue()


# ---------------- Main ----------------
def main(parent=None):
    if parent is not None:
        open_main_window(parent)
    else:
        root = tk.Tk()
        apply_icon(root, "landshape.ico")
        root.withdraw()
        open_main_window(root)
        root.mainloop()


if __name__ == "__main__":
    main()