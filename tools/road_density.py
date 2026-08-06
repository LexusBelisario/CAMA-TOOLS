import os
import re
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox
import geopandas as gpd
from shapely.geometry import Point
import subprocess
import math
import json
import psycopg2
from sqlalchemy import create_engine, inspect, text
from shapely.validation import make_valid
import threading
import queue

from utils.table_name_matching import normalize_name, find_matching_tables
from utils.resource_path import resource_path
from utils.db_discovery import load_db_credentials, fetch_tables

# ============================
# FORCE WINDOWS APP ICON
# ============================
import ctypes
import sys

def set_app_user_model_id():
    appid = u"BLGF.CAMA.Tools.2025"
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(appid)

set_app_user_model_id()


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


# Paths to icon and Global Mapper EXE
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"

barangay_source = None
road_source = None
output_mode = None
buffer_size = None  # 🔹 global buffer size (meters)

# density_column_overrides: {path_or_table: existing_col_name} -- for
# any Land Parcel source (Local file OR Database table) where a
# pre-existing "cama_dens_road"-like column was detected (see
# _check_parcel_density_conflicts() below) and the user confirmed
# proceeding at Run time. Read by run_processing() and passed into
# process_density() as output_column_name, so the tool writes back
# into the EXACT existing column (preserving its original casing)
# instead of always writing a hardcoded "CAMA_DENS_ROAD" -- the latter
# would silently create a confusing duplicate column whenever the
# existing one used different casing (e.g. a detected
# "caMA_dens_ROAD" alongside a new "CAMA_DENS_ROAD"). A source with no
# entry here uses the default "CAMA_DENS_ROAD" name.
density_column_overrides = {}

# ---------------- CRS Utility ----------------
def get_prs92_zone(labeled_gdfs):
    """
    Choose PRS92 zone EPSG from the combined bbox-midpoint longitude of
    one or more input GeoDataFrames.

    labeled_gdfs: list of (label, gdf) tuples, e.g.
        [("Land Parcel", brgy_gdf), ("Road Network", road_gdf)]
    The label is used only for diagnostics. It has no effect on CRS
    detection.

    Uses total_bounds, not a unioned-geometry centroid -- unary_union.centroid
    is a known source of GEOS TopologyExceptions on real-world cadastral
    data with invalid geometries (confirmed empirically: total_bounds
    stays fine on a mix of valid + self-intersecting polygons, while
    unary_union.centroid raises GEOSException/TopologyException on the
    exact same input). Same zone-boundary thresholds as before; only
    the longitude used to evaluate them has changed.

    total_bounds itself does not invoke GEOS topology operations, so it
    is not expected to raise the same TopologyException produced by
    unary_union.centroid -- but it is NOT immune to bad input in
    general: a GeoDataFrame with no usable geometry (all None, or all
    empty-but-non-null Polygon() shapes) still yields NaN bounds
    instead of crashing, which would otherwise silently fall through
    every "lon < ..." comparison below (NaN comparisons are always
    False) into the final "else" branch -- an incorrect zone returned
    with no warning at all.

    Two layers of defense against that:
      1. Pre-filter: skip any gdf that's None, has zero rows, or has
         no non-null geometry at all (geometry.notna().any()). Note
         notna() alone does NOT catch empty-but-non-null geometries
         (confirmed empirically -- Shapely's empty Polygon() passes
         notna() but still produces NaN bounds), so this filter is a
         cheap first pass, not a complete guarantee.
      2. Per-gdf post-check: after computing each gdf's total_bounds,
         explicitly verify it's not NaN and raise immediately, naming
         the specific layer -- BEFORE appending to all_bounds. This has
         to happen here and not after combining: the combination step
         below uses Python's built-in min()/max(), not a NaN-aware
         aggregation, so a single NaN slipping into all_bounds would
         silently propagate or vanish depending on its position in the
         list (confirmed empirically) rather than raising anything.
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

# ---------------- Geometry Fix ----------------
# NOTE (Part A3 investigation, resolved as NOT a confirmed bug): the two
# call sites below overwrite brgy_gdf["geometry"] directly with this
# function's repaired output, before process_density() runs -- the same
# "writes the fix into the output column" pattern flagged as a bug in
# other tools. Investigated specifically for THIS tool and found to be
# architecturally different:
#   1. process_density() only reads row.geometry.centroid from each
#      parcel -- never buffers/intersects the parcel polygon itself.
#   2. Empirically confirmed .centroid is safe on invalid geometry (no
#      crash, unlike unary_union) and the resulting centroid shift after
#      repair is negligible (~0.09m in a stress test, vs. a typical
#      1000m search radius) -- i.e. no evidence of a materially wrong
#      DENS_ROAD result.
#   3. This function does NOT have the historical "keep only the
#      largest MultiPolygon piece" defect that the older road_width.py
#      version had (confirmed by reading its body -- it returns
#      whatever buffer(0)/make_valid() produces, dropping nothing).
# What remains is a genuine but different question: should this tool
# persist repaired parcel geometry to its output at all, vs. keeping
# the original shape (matching the convention used elsewhere in this
# project)? That's a data-management POLICY decision, not a computation
# bug.
#
# RESOLVED: policy decision made -- keep the original, untouched shape.
# Both call sites below no longer write fix_geometry()'s result back into
# brgy_gdf["geometry"]; see each call site's own comment for the specific
# rationale (point 2 above -- negligible centroid shift on invalid
# geometry -- is what made this safe to resolve as a pure removal rather
# than a local-scope-only repair).
def fix_geometry(geom):
    if geom is None or geom.is_empty: 
        return None
    try:
        if not geom.is_valid:
            geom = geom.buffer(0)
        if not geom.is_valid:
            geom = make_valid(geom)
        return geom if not geom.is_empty else None
    except:
        return None

# ---------------- DB Helpers ----------------
def get_geometry_column(table_name, engine, schema):
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT f_geometry_column FROM geometry_columns
                WHERE f_table_schema=:schema AND f_table_name=:table
            """),{"schema":schema,"table":table_name}).fetchone()
            return row[0] if row else None
    except: 
        return None

def read_postgis_clean(table, engine, schema):
    geom_col = get_geometry_column(table,engine,schema)
    insp = inspect(engine)
    cols = [c["name"] for c in insp.get_columns(table,schema=schema) if c["name"]!=geom_col]
    col_str = ", ".join([f'"{c}"' for c in cols]) if cols else ""
    q = f'SELECT {col_str+", " if col_str else ""}"{geom_col}" AS geometry FROM "{schema}"."{table}"'
    return gpd.read_postgis(q, engine, geom_col="geometry")

# ---------------- Core Processing ----------------
def process_density(brgy_gdf, road_gdf, source_name="", output_column_name="CAMA_DENS_ROAD", progress=None):
    """Compute road density (m/m²) for each barangay polygon.

    output_column_name : str -- the column name the computed density is
        written to. Defaults to "CAMA_DENS_ROAD" (this tool's normal
        output, CAMA_-prefixed per project-wide column naming convention
        -- see road_width.py's own ROAD_WIDTH -> CAMA_ROAD_WIDTH). The
        GUI overrides this per-source when the selected LOCAL parcel
        layer already has an existing "cama_dens_road"-like column (any
        casing) -- the exact existing name/casing is passed here so
        processing writes back into that same column instead of
        creating a hardcoded "CAMA_DENS_ROAD" alongside it as a
        confusing duplicate.

    progress : optional callable progress(message, value=None, maximum=None),
    called from inside the per-parcel loop below (never from anywhere
    else in this function). Optional and defaults to None so this
    function's existing signature is unchanged for any call site that
    doesn't pass it -- added as part of this tool's Progress Event
    Protocol v9 migration (see run_processing() below).
    """
    global buffer_size
    orig_crs = brgy_gdf.crs

    # ✅ Project both to correct PRS92 zone (combined parcel + road extent)
    zone_epsg = get_prs92_zone([("Land Parcel", brgy_gdf), ("Road Network", road_gdf)])
    print(f"🌍 [{source_name}] Using PRS92 EPSG:{zone_epsg}")
    brgy_proj = brgy_gdf.to_crs(epsg=zone_epsg)
    road_proj = road_gdf.to_crs(epsg=zone_epsg)

    brgy_proj = brgy_proj[brgy_proj.geometry.type.isin(["Polygon", "MultiPolygon"])]
    road_proj = road_proj[road_proj.geometry.type.isin(["LineString", "MultiLineString"])]
    print(f"ℹ️ [{source_name}] Parcels after filter: {len(brgy_proj)}, Roads after filter: {len(road_proj)}")
    if road_proj.empty:
        print(f"⚠️ [{source_name}] No road features remain after geometry filter — check road layer geometry type.")

    brgy_proj[output_column_name] = 0.0
    radius = buffer_size if buffer_size else 1000  # meters
    buffer_area = math.pi * (radius ** 2)

    total = len(brgy_proj)
    for i, (idx, row) in enumerate(brgy_proj.iterrows(), start=1):
        if progress:
            progress(f"[{source_name}] Computing density: {i}/{total}", i, total)
        centroid = row.geometry.centroid
        buffer = centroid.buffer(radius)
        intersecting = road_proj[road_proj.geometry.intersects(buffer)]
        if intersecting.empty:
            continue
        clipped = intersecting.geometry.intersection(buffer)
        total_length = clipped.length.sum()
        dens = round(total_length / buffer_area, 6)
        brgy_proj.at[idx, output_column_name] = dens

        print(f"🟡 Feature {idx}: Length={round(total_length,2)} m, Density={dens}")

    # ✅ Reproject back to original CRS
    if orig_crs:
        brgy_proj = brgy_proj.to_crs(orig_crs)

    return brgy_proj


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
    apply_icon(dialog)
    dialog.title("ROAD DENSITY TOOL")
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
    apply_icon(dialog)
    dialog.title("ROAD DENSITY TOOL")
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


# ========================= PARCEL COLUMN-CONFLICT CHECK =========================
# _check_parcel_density_conflicts(): checks the selected Land Parcel
# source -- Local file OR Database table (extended to cover both as
# part of Fix 3; previously LOCAL-only) -- for an existing column
# matching "cama_dens_road" (case-insensitive exact match) -- this
# tool is about to write its computed road density into that column,
# and on_run() below shows a combined confirmation dialog before
# proceeding, regardless of which source type was selected.
#
# Unlike road_width.py/lot_location.py, this tool has no background
# worker thread / progress window / queue-polling architecture, so this
# runs synchronously on the main thread, called directly from on_run()
# right before Run actually starts. This is deliberate -- adding
# threading here would be a separate, out-of-scope architectural change
# (see project notes on background-processing not yet being scoped for
# the other tools), not part of this column-conflict task.
#
# Read approach: plain gpd.read_file(path) for a Local source, matching
# road_width.py's own canonical _read_gdf_worker() exactly -- no
# partial/schema-only read trick (e.g. rows=0) is used, since that is
# not confirmed consistently supported across the GeoPandas/Fiona/
# pyogrio versions in this project's environment. For a Database
# source, read_postgis_clean() is used instead, loading its own
# creds/schema/engine (self-contained, matching the pattern already
# used by on_run()'s PRIORITY 3 block).
#
# A read failure here is NEVER treated as a column-conflict failure --
# it only skips the conflict check for that one source (logged to
# console). The real read inside run_processing() further below remains
# solely responsible for surfacing any genuine read error to the user.
def _check_parcel_density_conflicts(sources, source_type):
    """
    Returns a list of (path_or_table, existing_col_name) tuples -- one
    entry only for sources where a column matching "cama_dens_road"
    (case-insensitive) was actually found. existing_col_name preserves
    the exact casing found in the source (e.g. a column literally named
    "caMA_dens_ROAD" is returned as-is, not normalized), so the
    confirmation dialog and the eventual write-back both show/use the
    real casing.

    source_type: "local" or "db" -- dispatches to gpd.read_file() or
    read_postgis_clean() respectively.
    """
    conflicts = []
    engine = None
    schema = None
    if source_type == "db":
        creds = load_db_credentials()
        if not creds:
            print("⚠️ Could not load DB credentials to check for an "
                  "existing CAMA_DENS_ROAD column.")
            return conflicts
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
            print(f"⚠️ Could not read parcel layer to check for an "
                  f"existing CAMA_DENS_ROAD column: {path_or_table}: {e}")
            continue
        existing_col = next(
            (c for c in gdf.columns if c.lower() == "cama_dens_road"), None
        )
        if existing_col:
            conflicts.append((path_or_table, existing_col))
    return conflicts


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
    win.title("Road Density Tool")
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
    # may exist in memory at any time (never a list). These are the
    # authority variables -- all GUI labels and run-button state are
    # derived from them, never the reverse. Reset to None when the
    # window closes (natural Python closure behavior).
    parcel_local_path = None   # authority: single local file path
    parcel_db_table   = None   # authority: single DB table name
    road_local_path    = tk.StringVar(master=win)
    road_db_table      = tk.StringVar(master=win)
    output_local_dir   = tk.StringVar(master=win)
    buffer_var         = tk.StringVar(master=win, value="1000")

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
        # Cancel returns "" -- do not assign, so parcel_local_path retains
        # its previous value (invariant: cancel never clears a valid selection).
        if file:
            nonlocal parcel_local_path
            parcel_local_path = file
            parcel_files_var.set(os.path.basename(file))
        # Always call _update_run_button_state(): if file was selected,
        # state may now be "Ready to run."; if cancelled, authority variable
        # is unchanged so run button state is unchanged -- but the call is
        # still correct and consistent.
        _update_run_button_state()

    def _on_parcel_db_selected(sel):
        # on_select is only called on confirmed selection (Confirm button)
        # -- Cancel in the table picker never calls on_select, so
        # parcel_db_table retains its previous value automatically.
        nonlocal parcel_db_table
        parcel_db_table = sel[0]
        parcel_db_label.set(sel[0])
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
        # Always render from authority variables -- never from the
        # current StringVar value. This guarantees that switching
        # Local → DB → Local always restores the original Local
        # selection, even if the StringVar was written by a previous
        # browse call in a different mode.
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
            messagebox.showerror("Error", "Could not load DB credentials.")
            return
        tables = fetch_tables(creds["schema"])
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

    # ── SECTION 3: BUFFER RADIUS ─────────────────────────────────
    section_label(win, "Buffer Radius")

    buffer_frame = tk.Frame(win)
    buffer_frame.pack(fill="x", padx=18, pady=2)
    tk.Label(buffer_frame, text="Radius (meters):",
             anchor="w").pack(side="left")
    tk.Entry(buffer_frame, textvariable=buffer_var,
             width=10).pack(side="left", **PAD)

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
        global barangay_source, road_source, output_mode, buffer_size

        # validate parcel
        if parcel_source_type.get() == "local":
            if not parcel_local_path:
                messagebox.showerror("Missing Input",
                    "Please select a Land Parcel file.")
                return
            # Validation guarantees parcel_local_path is not None here --
            # barangay_source never contains None (see Phase 1 invariant 3).
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

        # validate buffer
        try:
            buffer_size = float(buffer_var.get())
            if buffer_size <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid Input",
                "Please enter a valid positive number for the buffer radius.")
            return

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
        # Parcel source already has a CAMA_DENS_ROAD column. Shown
        # before the file-conflict dialog so the user can decide whether
        # to proceed at all before being asked about filename conflicts.
        # Declining cancels the run entirely; main window stays open
        # (this block runs before win.destroy() further below).
        # Extended (Fix 3) to also cover Database Land Parcel sources --
        # previously LOCAL-only (see _check_parcel_density_conflicts()'s
        # own docstring). Matches lot_location.py/road_width.py/
        # road_frontage.py's coverage. Reuses barangay_source, already
        # built and validated just above.
        global density_column_overrides
        conflicts = _check_parcel_density_conflicts(list(barangay_source[1]), barangay_source[0])
        if conflicts:
            lines = "\n\n".join(
                f"'{os.path.basename(path)}' already has the following column(s):\n"
                f"  • {existing_col}"
                for path, existing_col in conflicts
            )
            proceed = messagebox.askyesno(
                "Existing CAMA_DENS_ROAD column found",
                f"{lines}\n\n"
                "Processing will overwrite the existing column(s) with the "
                "newly computed values.\n\nProceed?"
            )
            if not proceed:
                print("Run cancelled by user (existing CAMA_DENS_ROAD column(s) found).")
                return
            # Preserve each source's existing column name/casing
            # exactly -- e.g. a detected "caMA_dens_ROAD" is written
            # back to "caMA_dens_ROAD", not a hardcoded
            # "CAMA_DENS_ROAD" -- so no duplicate column is ever
            # created regardless of the existing casing. A source
            # with no entry here (no conflict was found) simply
            # uses the default name in process_density() below.
            density_column_overrides = dict(conflicts)
        else:
            density_column_overrides = {}

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
        # road_surface.py) because nothing downstream in this file's
        # worker() consumes it -- only resolved_table_name is read (see
        # the table fallback near "Falls back to the old...").
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

    def _is_valid_buffer(value):
        """
        Same acceptance rule on_run() already applies (float, > 0) --
        used here only to gate the Run button, not to clamp or
        auto-correct buffer_var itself.
        """
        try:
            r = float(value)
        except (TypeError, ValueError):
            return False
        return r > 0

    def _update_run_button_state():
        """
        Single source of truth for whether the Run button may be
        pressed. Disabled (with an explanatory status message) until a
        Land Parcel source, a Road Network source, a valid positive
        buffer radius, and an Output destination are all present.

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
        buffer_ok = _is_valid_buffer(buffer_var.get())

        if not has_parcel:
            run_status_var.set("Please select a Land Parcel source.")
            ready = False
        elif not has_road:
            run_status_var.set("Please select a Road Network source.")
            ready = False
        elif not buffer_ok:
            run_status_var.set("Please enter a valid buffer radius.")
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

    # Live-updates the Run button as the user types in the buffer
    # radius field, without requiring focus-out or Enter.
    buffer_var.trace_add("write", lambda *_: _update_run_button_state())

    _toggle_parcel()
    _toggle_road()
    _toggle_output()
    _update_run_button_state()


def resolve_db_output_table(root, schema, barangay_source):
    """
    Determines the DB-output destination table for the Land Parcel
    source, BEFORE any processing or writing starts -- same
    "resolve everything up front" philosophy as
    ask_overwrite_dialog() (see run_processing()). road_density.py has
    no background worker thread (unlike road_width.py/lot_location.py/
    road_frontage.py) -- this function is still called once, up front,
    not because of any threading concern, but for separation of
    responsibilities: this function owns ALL user interaction and
    overwrite decisions, so the processing/write logic further below
    never has to ask any UI or overwrite question of its own.

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
# Same shape of migration as land_shape_compactness.py's and
# road_surface.py's: this tool had NO background worker thread and NO
# progress dialog at all -- run_processing() ran entirely synchronously
# on the main thread. Reuses progress_framework.py's
# PresentationState/ProgressPresentationPolicy/TkinterProgressView
# directly -- no tool-local copies, no new abstraction.
#
# Deliberately NOT done in this task:
#   - No per-source failure isolation added.
#   - The 3 overwrite dialogs in this file are untouched -- any
#     topmost/hiding fix for them is a separate, dedicated follow-up.
# ============================================================
from tools.progress_framework import (
    PresentationState,
    ProgressPresentationPolicy,
    TkinterProgressView,
)


class ProgressWindow:
    """
    Progress dialog shown while run_processing() works on a background
    thread. Same shape as the other migrated tools' ProgressWindow --
    status label + determinate progress bar, no cancel/stop_flag
    support. Progress Event Protocol v9 role: ProgressWindow is the
    host, not the decision-maker (see ProgressPresentationPolicy /
    TkinterProgressView, imported from progress_framework.py, shared
    with lot_location.py/road_frontage.py/land_shape_compactness.py/
    road_surface.py).
    """
    def __init__(self, root, title="Processing"):
        from tkinter import ttk
        self.win = tk.Toplevel(root)
        apply_icon(self.win)
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

        self._policy = ProgressPresentationPolicy()
        self._view = TkinterProgressView(self.win, self.status_var, self.progress)

    def update(self, message, value=None, maximum=None):
        state = self._policy.compute(message, value, maximum)
        self._view.render(state)

    def close(self):
        self._view.destroy()


# ---------------- Main Processing ----------------
def run_processing(root, overwrite_mode=None, resolved_table_name=None):
    # overwrite_mode: passed from on_run(). See PRIORITY 2 block there.
    # root: the live top-level window (passed from on_run(); NOT `win`,
    # which is destroyed before run_processing() is ever called -- see
    # on_run()'s win.destroy() immediately before this function's call
    # site). Used as the parent for any dialogs created in this
    # function (currently just resolve_db_output_table()'s DB
    # confirmation dialogs).
    global barangay_source, road_source, output_mode, buffer_size
    if not barangay_source or not road_source or not output_mode or buffer_size is None:
        messagebox.showerror("Error", "Selections incomplete.")
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
    # unchanged and stays on the main thread, exactly as before.
    #
    # road_gdf loading moves into worker() below -- matches
    # lot_location.py's/road_frontage.py's own established convention.
    #
    # Everything else below is the exact same two-loop body this
    # function always had (local-source loop, then the separate
    # DB-source loop -- NOT merged), including the fix_geometry() calls
    # that already ran here (unchanged, just relocated along with the
    # rest of the loop body), now wrapped inside a background worker()
    # thread instead of running inline on the main thread.
    progress = ProgressWindow(root, "Road Density Progress")
    q = queue.Queue()

    def worker():
        try:
            def progress_cb(msg, val=None, maxv=None):
                q.put(("update", msg, val, maxv))

            q.put(("update", "Loading road network...", None, None))
            road_gdf = (
                gpd.read_file(road_source[1][0]) if road_source[0] == "local"
                else read_postgis_clean(road_source[1][0], engine, schema)
            )

            if barangay_source[0] == "local":
                for path in barangay_source[1]:
                    q.put(("update", f"Loading {os.path.basename(path)}", None, None))
                    brgy_gdf = gpd.read_file(path)
                    # Deliberately NOT writing fix_geometry()'s repaired result
                    # back into brgy_gdf["geometry"] here (previously:
                    # brgy_gdf["geometry"] = brgy_gdf["geometry"].apply(fix_geometry)).
                    # Resolves the policy question left open in the fix_geometry()
                    # investigation note above (see that note's own final paragraph):
                    # this tool only reads row.geometry.centroid inside
                    # process_density() -- already confirmed there to be safe on
                    # invalid geometry with a negligible (~0.09m) measurement shift --
                    # so the repair brought no measurement benefit here, only a side
                    # effect of silently altering the SAVED output geometry, which
                    # broke from the documented convention followed elsewhere in this
                    # project (road_width.py, land_shape_compactness.py,
                    # lot_location.py, influence_to_map.py): output geometry must stay
                    # the parcel's original, untouched shape, even if invalid.
                    # output_column_name: preserves the exact existing column
                    # name/casing this LOCAL source's parcel layer already had
                    # (if the user confirmed overwriting one at Run time -- see
                    # on_run()'s confirmation dialog). A source with no entry
                    # here falls back to process_density()'s own default
                    # ("CAMA_DENS_ROAD").
                    output_column_name = density_column_overrides.get(path, "CAMA_DENS_ROAD")
                    result = process_density(
                        brgy_gdf, road_gdf, os.path.basename(path),
                        output_column_name=output_column_name,
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
                            result.to_postgis(table, conn, schema=schema,
                                              if_exists="replace", index=False)
                        print(f"🔄 Saved to DB: {table}")
            else:
                # Database Land Parcel sources: extended (Fix 3) to
                # respect density_column_overrides, same as the LOCAL
                # branch above -- preserves the exact existing column
                # casing detected in on_run()'s PRIORITY 1 check instead
                # of always defaulting to "CAMA_DENS_ROAD".
                for table in barangay_source[1]:
                    q.put(("update", f"Loading DB table {table}", None, None))
                    brgy_gdf = read_postgis_clean(table, engine, schema)
                    # Same rationale as the local-source loop above -- see that
                    # comment block for the full explanation. Not duplicating the
                    # full note here per Rule of Three (Section G.5): both call
                    # sites keep their own short pointer rather than sharing a helper.
                    output_column_name = density_column_overrides.get(table, "CAMA_DENS_ROAD")
                    result = process_density(
                        brgy_gdf, road_gdf, table,
                        output_column_name=output_column_name,
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
                            result.to_postgis(table, conn, schema=schema,
                                              if_exists="replace", index=False)
                        print(f"🔄 Updated DB table: {table}")

            q.put(("done", "Processing done!", None, None))

        except Exception as e:
            # New: this function had no top-level try/except before --
            # an uncaught exception here previously propagated silently
            # (no graceful dialog). Required by moving to a background
            # thread: an exception on a non-main thread that nobody
            # catches is otherwise simply lost.
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
        apply_icon(root)
        root.withdraw()
        open_main_window(root)
        root.mainloop()


if __name__ == "__main__":
    main()