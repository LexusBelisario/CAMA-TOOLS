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


# === Global Mapper EXE and Icon Paths
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

# parcel_output_column_overrides: {path: {"CAMA_PP_RATIO": name, ...}} --
# for any LOCAL Land Parcel source where one or more pre-existing
# CAMA_-prefixed output columns were detected (see
# _check_parcel_shape_conflicts() below) and the user confirmed
# proceeding at Run time. Read by run_processing() and resolved into
# the eight individual *_col keyword arguments passed to
# compute_ppr_and_lot_shape_gdf() -- matches the exact same
# override-storage-as-dict / function-signature-as-individual-kwargs
# split already established in terrain.py and road_frontage.py, so the
# tool writes back into the EXACT existing column(s) (preserving
# original casing) instead of always writing hardcoded "CAMA_*" names.
# A source with no entry here (or a target missing from its entry) uses
# that target's default CAMA_ name. Scope: LOCAL sources only --
# Database Land Parcel sources are explicitly out of scope for this
# check.
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
def _detect_existing_output_columns(gdf):
    """
    Checks a parcel GeoDataFrame for pre-existing columns matching any of
    OUTPUT_COLUMN_TARGETS (CAMA_PP_RATIO, CAMA_VTX_COUNT, CAMA_ANGS_TXT,
    CAMA_TRIANGLE, CAMA_RECTANGLE, CAMA_L_SHAPED, CAMA_OTHERS,
    CAMA_LOT_SHAPE), exact match (case-insensitive) -- "cama_pp_ratio"
    matches "CAMA_PP_RATIO", but a column like "CAMA_PP_RATIO_OLD" or
    "PP_RATIO" does NOT match (no substring/partial matching, and no
    matching against the old, unprefixed names -- see
    OUTPUT_COLUMN_TARGETS' own docstring).

    Mirrors road_frontage.py's / terrain.py's
    _detect_existing_output_columns() exactly.

    Returns a dict {target_name: actual_existing_column_name}, containing
    ONLY the targets that actually have a match. Empty dict if none of
    the eight targets have any existing column. The actual column's
    ORIGINAL casing is preserved in the returned value -- this is what
    gets shown to the user in the confirmation dialog and what
    compute_ppr_and_lot_shape_gdf() writes back into, so an existing
    differently-cased column is reused exactly as found rather than
    renamed or duplicated.
    """
    found = {}
    for target in OUTPUT_COLUMN_TARGETS:
        match = next((c for c in gdf.columns if c.lower() == target.lower()), None)
        if match is not None:
            found[target] = match
    return found


# ========================= PARCEL COLUMN-CONFLICT CHECK =========================
# _check_parcel_shape_conflicts(): checks LOCAL Land Parcel source(s)
# for pre-existing columns matching any of OUTPUT_COLUMN_TARGETS -- this
# tool is about to write its eight computed shape/compactness columns
# into those columns, and on_run() below shows a combined confirmation
# dialog before proceeding.
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
def _check_parcel_shape_conflicts(local_paths):
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


# ---------------- Output filename helpers ----------------
# Ported from road_width.py's validated pattern, already successfully
# adapted in road_frontage.py, lot_location.py, road_density.py,
# road_surface.py, and terrain.py. Implementations are identical across
# all ports -- do not introduce variations.

def _split_trailing_number(base_name: str):
    """
    Splits a base name into (root, existing_number) if it ends with
    "_<digits>" (e.g. "landparcel_1" -> ("landparcel", 1)), else returns
    (base_name, None) unchanged.
    """
    m = re.match(r'^(.*)_(\d+)$', base_name)
    if m:
        return m.group(1), int(m.group(2))
    return base_name, None


def resolve_output_base_name(folder: str, desired_base_name: str, ext: str = "gpkg") -> str:
    """
    Determines the actual output base name (no extension) to use for a
    NEW file in `folder`, given the DESIRED name -- normally the Land
    Parcel source's own filename, unchanged, with no tool-name suffix
    appended.

    Rule: reuse the desired name exactly if nothing of that name exists
    yet in `folder`. If it already exists, strip any existing trailing
    "_<N>" from the desired name to get a root, scan `folder` for every
    file matching "<root>_<N>.<ext>", and use "<root>_<max(N)+1>" --
    the highest N found ANYWHERE in the folder, not just "the source
    file's own N + 1".

    This tool has no companion/QA outputs, so there is no
    with_output_suffix call needed here.
    """
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
        pass  # folder unreadable -- fall through with max_n=0, worst case uses N=1

    return f"{root}_{max_n + 1}"


def ask_overwrite_dialog(parent, conflicting_names):
    """
    Combined dialog shown ONCE, before any processing starts, when one or
    more Land Parcel sources' desired local output filename already
    exists in the chosen output folder. Not a per-file prompt -- every
    conflicting name in the batch is listed together, and the chosen
    action applies to ALL of them:

      - "Overwrite": every conflicting file is replaced in place, using
        its plain desired name (no numbering).
      - "Create New File": every conflicting file is instead saved under
        a new, non-colliding name via resolve_output_base_name()'s
        auto-numbering -- the existing files are left untouched.
      - "Cancel": aborts the ENTIRE run. Nothing is written, including
        sources that had no conflict at all.

    Returns "overwrite", "new", or "cancel" (also returned if the
    dialog's own titlebar close button is used).

    Ported from road_width.py's validated implementation, already
    adapted in road_frontage.py, lot_location.py, road_density.py,
    road_surface.py, and terrain.py. Deliberately does NOT call
    dialog.transient(parent): this app's root is permanently withdrawn
    (see main()), and transient() on a withdrawn parent is a known
    source of window-manager-dependent "dialog never becomes viewable"
    behavior. grab_set()+deiconify()+lift()+focus_force()+topmost is
    used instead, matching this file's own existing dialog pattern
    (see _pick_db_tables()).
    """
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

    # Buttons packed first, at the bottom -- guaranteed visible/reachable
    # regardless of how long the scrollable list above them ends up being.
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
    needs_hscroll = any(len(f"• {name}") > TEXT_WIDTH_CHARS for name in conflicting_names)
    if needs_hscroll:
        hscroll.pack(side="bottom", fill="x")
    text.pack(side="left", fill="both", expand=True)
    for name in conflicting_names:
        text.insert("end", f"• {name}\n")
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


def compute_ppr_and_lot_shape_gdf(gdf,
        pp_ratio_col="CAMA_PP_RATIO", vtx_count_col="CAMA_VTX_COUNT",
        angs_txt_col="CAMA_ANGS_TXT", triangle_col="CAMA_TRIANGLE",
        rectangle_col="CAMA_RECTANGLE", l_shaped_col="CAMA_L_SHAPED",
        others_col="CAMA_OTHERS", lot_shape_col="CAMA_LOT_SHAPE"):
    """
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
    for idx, geom in fixed_geoms.items():
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
def load_db_credentials():
    path = _get_credentials_path()
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None

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

def normalize_name(name): return re.sub(r'[^a-z]','',name.lower())

def fetch_tables(schema):
    creds=load_db_credentials()
    if not creds: return []
    try:
        conn=psycopg2.connect(
            host=creds["host"],port=creds["port"],
            dbname=creds["database"],user=creds["username"],password=creds["password"]
        )
        cur=conn.cursor()
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s;",(schema,))
        return [r[0] for r in cur.fetchall()]
    except: return []

def find_matching_table(local_name,schema):
    lname=normalize_name(local_name)
    for t in fetch_tables(schema):
        if lname in normalize_name(t) or normalize_name(t) in lname:
            return t
    return None


# ---------------- Tkinter Windows ----------------
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
    apply_icon(win)
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
    parcel_local_paths = []
    parcel_db_tables   = []
    output_local_dir   = tk.StringVar(master=win)

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
            messagebox.showerror("Error", "Could not load DB credentials.")
            return
        tables = fetch_tables(creds["schema"])
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

        if output_dest_type.get() == "local":
            if not output_local_dir.get():
                messagebox.showerror("Missing Input",
                    "Please select an output folder.")
                return
            output_mode = ("local", output_local_dir.get())
        else:
            output_mode = ("db", None)

        # Existing OUTPUT-COLUMN conflict warning. Checks all eight output
        # columns (CAMA_PP_RATIO, CAMA_VTX_COUNT, CAMA_ANGS_TXT,
        # CAMA_TRIANGLE, CAMA_RECTANGLE, CAMA_L_SHAPED, CAMA_OTHERS,
        # CAMA_LOT_SHAPE) -- not just PP_RATIO/LOT_SHAPE -- per the same
        # project-lead decision already applied in road_frontage.py and
        # terrain.py: they are one feature set computed together, so a
        # conflict on ANY of them warrants one combined warning covering
        # all affected sources and columns, shown once here (never
        # per-file mid-processing, never only at Browse time). Declining
        # cancels the run entirely rather than skipping just the
        # affected source(s). Column names are shown with their EXACT
        # existing casing, and that exact casing/name is what
        # compute_ppr_and_lot_shape_gdf() will write into later -- never
        # renamed to the standard casing. LOCAL sources only -- Database
        # Land Parcel sources are explicitly out of scope for this check.
        # ------------------------------------------------------------------
        global parcel_output_column_overrides
        if parcel_source_type.get() == "local":
            conflicts = _check_parcel_shape_conflicts(parcel_local_paths)
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
        else:
            parcel_output_column_overrides = {}
        # PRIORITY 2: existing OUTPUT-FILE conflict check (local output only).
        # Resolved ONCE, up front, on the main thread -- before win.destroy()
        # so the dialog has a live parent window. The chosen action applies
        # to ALL conflicting sources in the batch (one combined dialog, not
        # per-file). Cancel aborts the entire run; nothing is written.
        # Ported from road_width.py / road_frontage.py's validated pattern.
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

        # ------------------------------------------------------------------

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
        Land Parcel source and an Output destination are both selected.

        Explicit bg/fg/cursor toggling (not just state=) is required:
        Tkinter does NOT automatically gray out a classic tk.Button's
        custom bg/fg when state="disabled", and does not suppress a
        widget's assigned cursor either -- both must be set explicitly
        for each state.
        """
        has_parcel = bool(parcel_local_paths) if parcel_source_type.get() == "local" else bool(parcel_db_tables)
        has_output = bool(output_local_dir.get()) if output_dest_type.get() == "local" else True

        if not has_parcel:
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
def run_processing(overwrite_mode=None):
    # overwrite_mode: "overwrite", "new", or None (no conflict existed).
    # Resolved ONCE, up front, on the main thread in on_run() before
    # win.destroy() -- passed here as a parameter (not a global).
    # See ask_overwrite_dialog() for the full behavior contract.
    global barangay_source, output_mode
    if not barangay_source or not output_mode:
        messagebox.showerror("Error", "Selections incomplete (Barangay + Output required).")
        return

    creds = load_db_credentials()
    schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    )

    if barangay_source[0] == "local":
        for path in barangay_source[1]:
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
            )
            if output_mode[0] == "local":
                # Desired output filename = the Land Parcel source's own
                # name, unchanged -- no tool-name suffix appended (matching
                # road_width.py / road_frontage.py's established convention).
                # overwrite_mode was resolved ONCE, up front in on_run(),
                # for the whole batch -- no per-file prompt here.
                base = os.path.splitext(os.path.basename(path))[0]
                desired_base_name = base
                candidate_path = os.path.join(output_mode[1], f"{desired_base_name}.gpkg")
                had_conflict = os.path.exists(candidate_path)
                if had_conflict and overwrite_mode == "new":
                    base_name = resolve_output_base_name(output_mode[1], desired_base_name)
                else:
                    # No conflict, or user chose "Overwrite" --
                    # both cases use the plain desired name.
                    base_name = desired_base_name
                out = os.path.join(output_mode[1], f"{base_name}.gpkg")
                result.to_file(out, driver="GPKG")
                print(f"✅ Saved {out}")
                load_in_global_mapper(out)
            else:
                local_name = os.path.splitext(os.path.basename(path))[0]
                match = find_matching_table(local_name, schema)
                table = match if match else local_name.lower()
                result.to_postgis(table, engine, schema=schema, if_exists="replace", index=False)
                print(f"🔄 Saved to DB: {table}")
    else:
        # Database Land Parcel sources: column-conflict check is out of
        # scope (see _check_parcel_shape_conflicts()) -- always uses
        # compute_ppr_and_lot_shape_gdf()'s eight default CAMA_-prefixed
        # names.
        for table in barangay_source[1]:
            gdf = read_postgis_clean(table, engine, schema)
            # Row-dropping REMOVED -- same reasoning as the local-source
            # branch above.
            result = compute_ppr_and_lot_shape_gdf(gdf)
            if output_mode[0] == "local":
                # DB parcel source: table name used as desired base name
                # directly (no .splitext() needed). Same overwrite/create-new
                # logic as the local parcel source branch above.
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
                result.to_postgis(table, engine, schema=schema, if_exists="replace", index=False)
                print(f"🔄 Updated DB table: {table}")

    messagebox.showinfo("Success", "Processing done!")


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