"""
tools/road_surface.py

PURPOSE:
    CAMA Tools tool ("ROAD SURFACE" in MAIN.py's dispatch table): for
    each Land Parcel, determines the road surface type(s) (e.g. paved,
    gravel, dirt) of intersecting roads within a fixed 10-unit buffer,
    falling back to the nearest road's surface for parcels with no
    intersection. Writes a slash-separated string of all surface types
    found (e.g. "asphalt/gravel") into CAMA_RD_SURFACE (or an existing
    differently-cased column, if one was detected and confirmed at Run
    time -- see surface_column_overrides below).

DISPATCH:
    Run as an isolated subprocess by MAIN.py via its `--tool` dispatch
    mechanism (see system context). Entry point is main(), triggered via
    the `if __name__ == "__main__":` guard at the bottom of this file.

INPUTS:
    Land Parcel source: a single local file (.shp, .gpkg, or any file
    type via the "All" filter) or a single PostGIS table.
    Road Network source: a single local file or PostGIS table with a
    `surface`/`surf`/`road_surf`/`rd_surface`/`pavement` column
    (case-insensitive, first match wins).
    pg_credentials.json (via load_db_credentials(), from
    utils/db_discovery.py) for any DB source or DB output.

OUTPUTS:
    Local output mode: writes one atomically-written .gpkg per Land
    Parcel source processed (_write_gpkg()), then attempts to open it in
    Global Mapper (load_in_global_mapper()).
    DB output mode: writes/replaces one PostGIS table per source,
    resolved via resolve_db_output_table() -- an exact-match replace for
    a DB Land Parcel source, or a fuzzy-match-with-confirmation flow
    (confirm_db_overwrite_dialog() / choose_db_overwrite_dialog()) for a
    local-file Land Parcel source.

DEPENDENCIES:
    stdlib: os, re, math, subprocess, json, threading, queue, time,
    ctypes, sys, tkinter.
    third-party: geopandas, shapely, psycopg2, sqlalchemy.
    local: utils.table_name_matching, utils.resource_path,
    utils.db_discovery, utils.column_detection, utils.window_icon,
    utils.progress_framework (PresentationState,
    ProgressPresentationPolicy, TkinterProgressView -- imported mid-file,
    directly above the class/function that uses it; see the Progress
    Event Protocol v9 comment block further below for why this file's
    progress dialog was migrated to that shared framework).

SIDE EFFECTS:
    File reads/writes (.shp/.gpkg). PostGIS reads/writes. A live
    PostgreSQL connection. Tkinter GUI windows throughout, including a
    background thread + queue.Queue-based polling loop for the
    Land-Parcel existing-column check (detect-on-select) and for the
    main processing run itself. A subprocess launch to Global Mapper
    (load_in_global_mapper()) on local-output saves, plus a Win32
    EnumWindows call to find/focus an already-open Global Mapper window
    first.

    IMPORTANT -- this module has a genuine import-time side effect: the
    module-level call to set_app_user_model_id() (see the "FORCE WINDOWS
    APP ICON" section below) invokes the Win32
    SetCurrentProcessExplicitAppUserModelID API the moment this file is
    imported or run -- not lazily, not inside main(). This affects how
    Windows groups/identifies this process's taskbar icon. Preserved
    exactly as found -- not moved, deferred, or wrapped in a function --
    since doing so would change when this Windows-level identification
    happens, which is out of scope for a documentation/reorganization
    task (see Section C of the governing instructions: no behavior
    changes).

    KNOWN FOLLOW-UP (documented, not implemented here): GM_EXE_PATH
    below is currently a hardcoded absolute path
    ("C:\\Program Files\\GlobalMapper26.1_64bit\\global_mapper.exe").
    The planned improvement is dynamic Global Mapper executable-path
    discovery instead of a hardcoded constant. That discovery logic
    (search locations, missing-executable handling, installation-
    variant handling, fallback behavior) is a separate, deliberately-
    scoped future task -- not implemented as part of this
    documentation/reorganization pass, since it would change runtime
    behavior.
"""
import os
import re
import math
import subprocess
import json
import threading
import queue
import time
import secrets
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox

import geopandas as gpd
from shapely.geometry import Point
import psycopg2
from sqlalchemy import create_engine, inspect, text

from utils.table_name_matching import normalize_name, find_matching_tables
from utils.resource_path import resource_path
from utils.db_discovery import load_db_credentials, fetch_tables
from utils.column_detection import detect_existing_output_columns
from utils.window_icon import apply_icon
from utils.gpkg_io import write_gpkg_atomic as _write_gpkg

# ============================
# FORCE WINDOWS APP ICON
# ============================
import ctypes
import sys

# NOTE: import-time side effect -- this call executes the moment this
# module is loaded, before main() runs (see module docstring SIDE
# EFFECTS). Not moved or deferred; see module docstring for why.
def set_app_user_model_id():
    appid = u"BLGF.CAMA.Tools.2025"
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(appid)

set_app_user_model_id()


# ========================================
# CONFIGURATION
# ========================================
# Hardcoded (current behavior). Planned improvement: dynamic Global
# Mapper executable discovery. Actual implementation: separate future
# task -- see module docstring SIDE EFFECTS for the full note.
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"

# ========================================
# RUNTIME STATE
# ========================================
barangay_source = None
road_source = None
output_mode = None

# surface_column_overrides: {path_or_table: existing_col_name} -- for
# any Land Parcel source (Local file OR Database table) where a
# pre-existing "cama_rd_surface"-like column was detected (see
# _check_parcel_surface_conflicts() below) and the user confirmed
# proceeding at Run time. Read by run_processing() and passed into
# process_surface() as output_column_name, so the tool writes back
# into the EXACT existing column (preserving its original casing)
# instead of always writing a hardcoded "CAMA_RD_SURFACE" -- the
# latter would silently create a confusing duplicate column whenever
# the existing one used different casing. A source with no entry here
# uses the default "CAMA_RD_SURFACE" name.
surface_column_overrides = {}

# ========================================
# CRS HELPER
# ========================================
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

# ========================================
# DB HELPERS
# ========================================
def get_geometry_column(table_name, engine, schema):
    """
    Looks up the geometry column name for a PostGIS table via the
    geometry_columns system view.

    Args:
        table_name (str): the table to look up.
        engine: a SQLAlchemy engine.
        schema (str): the schema the table lives in.

    Returns:
        str | None: the geometry column name, or None if not found or
        on any query error (errors are swallowed, not raised).
    """
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
    """
    Reads a PostGIS table into a GeoDataFrame with a single, consistently
    named "geometry" column, regardless of what the table's actual
    geometry column is called.

    Args:
        table (str): table name to read.
        engine: a SQLAlchemy engine.
        schema (str): the schema the table lives in.

    Returns:
        geopandas.GeoDataFrame: the table's contents, with the geometry
        column renamed to "geometry".
    """
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
    """Opens path in Global Mapper (subprocess), if both GM_EXE_PATH and
    path exist. Simpler than load_in_global_mapper() further below (no
    EnumWindows focus-existing-window step) -- currently unused by
    run_processing(), which calls load_in_global_mapper() instead; kept
    as-is, not consolidated (see Section 3.E.7 of the governing
    instructions)."""
    if os.path.exists(GM_EXE_PATH) and os.path.exists(path):
        subprocess.Popen([GM_EXE_PATH, path], shell=True)


# ========================================
# PARCEL COLUMN-CONFLICT CHECK
# ========================================
# _check_parcel_surface_conflicts(): checks the selected Land Parcel
# source -- Local file OR Database table (extended to cover both as
# part of Fix 3; previously LOCAL-only) -- for an existing column
# matching "cama_rd_surface" (case-insensitive exact match) -- this
# tool is about to write its computed road surface(s) into that
# column, and on_run() below shows a combined confirmation dialog
# before proceeding, regardless of which source type was selected.
#
# Runs synchronously on the main thread (this tool has no background
# worker thread / progress window / queue-polling architecture, and
# adding one is out of scope for this task): reads via plain
# gpd.read_file(path) for a Local source, or read_postgis_clean() for
# a Database source (loading its own creds/schema/engine, self-
# contained). A read failure here is NEVER treated as a column-
# conflict failure -- it only skips the conflict check for that one
# source (logged to console); the real read inside run_processing()
# further below remains solely responsible for surfacing any genuine
# read error to the user.
def _check_parcel_surface_conflicts(sources, source_type):
    """
    Returns a list of (path_or_table, existing_col_name) tuples on a
    SUCCESSFUL read/check -- one entry only for sources where a column
    matching "cama_rd_surface" (case-insensitive) was actually found;
    an empty list means the check succeeded and found no conflict.
    Returns None if credentials could not be loaded, or if ANY source
    failed to read -- this is a REQUIRED distinction, not cosmetic: an
    empty list means "verified, no conflict", while None means "could
    not verify at all". existing_col_name preserves the exact casing
    found in the source, so the confirmation dialog and the eventual
    write-back both show/use the real casing.

    source_type: "local" or "db" -- dispatches to gpd.read_file() or
    read_postgis_clean() respectively. Extended (Fix 3) to cover
    Database Land Parcel sources -- previously LOCAL-only. Loads its
    own creds/schema/engine for the "db" case, self-contained, matching
    the pattern already used by on_run()'s PRIORITY 3 block and
    lot_location.py's own dual-mode checker.
    """
    conflicts = []
    engine = None
    schema = None
    if source_type == "db":
        creds = load_db_credentials()
        if not creds:
            print("⚠️ Could not load DB credentials to check for an "
                  "existing CAMA_RD_SURFACE column.")
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
            print(f"⚠️ Could not read parcel layer to check for an "
                  f"existing CAMA_RD_SURFACE column: {path_or_table}: {e}")
            return None
        found = detect_existing_output_columns(gdf, ("CAMA_RD_SURFACE",))
        existing_col = found.get("CAMA_RD_SURFACE")
        if existing_col:
            conflicts.append((path_or_table, existing_col))
    return conflicts


# ========================================
# PROCESSING
# ========================================
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
def process_surface(brgy_gdf, road_gdf, output_column_name="CAMA_RD_SURFACE", progress=None, cancel_flag=None):
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

    progress : optional callable progress(message, value=None, maximum=None),
    called from inside the two loops below (never from anywhere else in
    this function). Optional and defaults to None so this function's
    existing signature is unchanged for any call site that doesn't pass
    it -- added as part of this tool's Progress Event Protocol v9
    migration (see run_processing() below).

    cancel_flag : optional dict from utils.progress_framework.
    new_cancel_flag() (added for D-Cancel). Checked directly, once per
    iteration, at the TOP of BOTH Loop 1 (over road_buffer -- assigning
    surfaces from intersecting roads) and Loop 2 (over unmatched -- the
    nearest-road fallback) -- an explicit-True-only check
    (`cancel_flag["stop"] is True`), matching the explicit-False-only
    contract this file's progress callback itself uses elsewhere, so
    neither loop's Cancel checkpoint is ever accidentally tripped by a
    falsy-but-not-True value. Per Blocker #1's resolved decision
    (full-discard, matching the three prior tools' precedent): on
    Cancel, at EITHER loop, this function returns None immediately --
    nothing computed so far (including any surfaces already assigned
    in a partially-completed Loop 1) is kept, and the caller (worker(),
    in run_processing()) never reaches the save step for this source.
    Per Blocker #2's resolved decision, BOTH loops get their own
    checkpoint, not just Loop 1 -- Loop 2's total_unmatched, while
    usually much smaller than Loop 1's total_roads, is not assumed
    negligible.

    None (the default) preserves this function's exact pre-D-Cancel
    behavior: neither loop ever checks anything, and this function
    always returns a real (non-None) result -- matching this
    parameter's optional, backward-compatible shape.
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
        # BUG FIX (deliberate, not a preserved behavior -- see
        # run_processing()'s Progress Event Protocol v9 migration
        # comment for the full record): the previous version of this
        # branch called messagebox.showerror(...) and then
        # `return brgy_gdf` -- the caller had no way to know a fatal
        # validation error had occurred, so run_processing() continued
        # on to write this INCOMPLETE gdf (missing the output column
        # entirely) to the destination, and still showed the final
        # "Success" dialog afterward. Raising here instead makes this a
        # real fatal error: the caller (worker(), in run_processing())
        # already has a try/except that turns any raised exception into
        # the Progress Event Protocol's "error" event -- which shows
        # the modal error on the MAIN thread (never from here, the
        # worker thread), writes nothing, and never reaches "done"/the
        # Success dialog. Also fixes the pre-existing Tkinter-call-from
        # -background-thread hazard this line would otherwise have had.
        raise ValueError(
            f"Road layer has no 'surface' column.\n\n"
            f"Available columns: {', '.join(road_gdf.columns.tolist())}"
        )

    print(f"ℹ️ Using road surface column: '{surface_col}'")

    # Buffer the roads
    road_buffer = road_gdf.copy()
    road_buffer["geometry"] = road_gdf.buffer(10)

    brgy_gdf[output_column_name] = [[] for _ in range(len(brgy_gdf))]

    # Assign surfaces from intersecting roads
    total_roads = len(road_buffer)
    for i, (_, road) in enumerate(road_buffer.iterrows(), start=1):
        # D-Cancel: Loop 1 checkpoint (Blocker #2: both loops get one).
        # Checked at the TOP of each iteration, before any work for
        # this road happens -- explicit-True-only, so a cancel_flag of
        # None (the no-Cancel-support default) or {"stop": False} never
        # trips this. Full discard (Blocker #1): nothing assigned so
        # far in this loop is kept -- the in-place .at[] mutations
        # already applied to brgy_gdf are simply abandoned along with
        # the whole brgy_gdf object once this function returns None;
        # the caller never uses a None return for anything.
        if cancel_flag is not None and cancel_flag["stop"] is True:
            return None
        if progress:
            progress(f"Assigning road surfaces: {i}/{total_roads}", i, total_roads)
        surface_val = str(road.get(surface_col, "")).strip()
        if not surface_val:
            continue
        intersect_mask = brgy_gdf.geometry.intersects(road.geometry)
        for idx in brgy_gdf[intersect_mask].index:
            if surface_val not in brgy_gdf.at[idx, output_column_name]:
                brgy_gdf.at[idx, output_column_name].append(surface_val)

    # Nearest road for those with no intersections
    no_surface_mask = brgy_gdf[output_column_name].apply(lambda x: len(x) == 0)
    unmatched = brgy_gdf[no_surface_mask]
    total_unmatched = len(unmatched)
    for i, (idx, row) in enumerate(unmatched.iterrows(), start=1):
        # D-Cancel: Loop 2 checkpoint (Blocker #2: both loops get one,
        # not just Loop 1 -- total_unmatched is not assumed negligible).
        # Same explicit-True-only / full-discard contract as Loop 1's
        # checkpoint above -- a Cancel here discards Loop 1's completed
        # work too, since this function returns None either way.
        if cancel_flag is not None and cancel_flag["stop"] is True:
            return None
        if progress:
            progress(f"Resolving unmatched parcels: {i}/{total_unmatched}", i, total_unmatched)
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


# ========================================
# OUTPUT FILENAME HELPERS
# ========================================
def _split_trailing_number(base_name: str):
    """Splits a trailing '_N' suffix off base_name, if present. Returns
    (root, N) or (base_name, None) if there's no trailing number."""
    m = re.match(r'^(.*)_(\d+)$', base_name)
    if m:
        return m.group(1), int(m.group(2))
    return base_name, None


def resolve_output_base_name(folder: str, desired_base_name: str, ext: str = "gpkg") -> str:
    """
    Returns desired_base_name unchanged if no file of that name already
    exists in folder. Otherwise, finds the highest existing "_N" suffix
    among files matching the same root name in folder and returns the
    root with N+1 appended, so a "Create New File" choice never
    collides with an existing file.

    Args:
        folder (str): directory to check.
        desired_base_name (str): the name that would ideally be used.
        ext (str): file extension to check for (without the dot).

    Returns:
        str: a base name (no extension) guaranteed not to collide with
        an existing file in folder at the time of the call.
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
        pass
    return f"{root}_{max_n + 1}"


# ========================================
# OVERWRITE DIALOGS
# ========================================
def ask_overwrite_dialog(parent, conflicting_names):
    """
    Modal dialog shown when one or more local output files already
    exist. Lets the user choose to overwrite all of them, save all
    under new (non-colliding) names instead, or cancel the run
    entirely -- one choice applies to every listed file.

    Args:
        parent: parent Tk window.
        conflicting_names (list[str]): filenames (with extension)
        already present in the output folder.

    Returns:
        str: "overwrite", "new", or "cancel" (also returned if the
        dialog is closed via the window's X button).
    """
    result = {"choice": "cancel"}
    dialog = tk.Toplevel(parent)
    apply_icon(dialog, "roadsurface.ico")
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
    apply_icon(dialog, "roadsurface.ico")
    dialog.title("ROAD SURFACE TOOL")
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
    apply_icon(dialog, "roadsurface.ico")
    dialog.title("ROAD SURFACE TOOL")
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


# ========================================
# GLOBAL MAPPER
# ========================================
def load_in_global_mapper(filepath):
    """
    Opens filepath in Global Mapper. First tries to find an already-open
    Global Mapper window (via a Win32 EnumWindows title-text scan) so a
    running instance can pick up the new file, then launches
    GM_EXE_PATH as a subprocess regardless of whether an existing
    window was found. Any failure is caught and only printed, never
    raised or shown to the user.

    Args:
        filepath (str): path to open in Global Mapper.

    Notes:
        GM_EXE_PATH is currently a hardcoded absolute path (see
        CONFIGURATION section above and the module docstring's SIDE
        EFFECTS note) -- dynamic executable discovery is a planned,
        separately-scoped future improvement, not implemented here.
    """
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


# ========================================
# DB TABLE PICKER
# ========================================
def _pick_db_tables(parent, tables, multi, on_select):
    """
    Simple modal listbox dialog for picking one (multi=False) or more
    (multi=True) table names from `tables`. Calls on_select(selection)
    and closes itself once the user confirms a non-empty selection.

    Args:
        parent: parent Tk window.
        tables (list[str]): table names to list.
        multi (bool): whether multiple selection is allowed.
        on_select (callable): called with the list of selected names.
    """
    from tkinter import ttk
    picker = tk.Toplevel(parent)
    apply_icon(picker, "roadsurface.ico")
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


# ========================================
# MAIN WINDOW
# ========================================
def open_main_window(root):
    """
    Builds and shows the tool's single unified configuration window:
    Land Parcel and Road Network source pickers (each with a
    Local-file/Database-table radio toggle), an Output destination
    picker, and a Run button gated by _update_run_button_state().

    The Land Parcel picker additionally runs a background,
    detect-on-select check (_refresh_parcel_surface_check(), via a
    daemon thread + win.after()-polled queue.Queue) for an existing
    CAMA_RD_SURFACE-like column, the moment a file/table is selected or
    the Local/Database toggle changes -- not only when Run is clicked.
    See _refresh_parcel_surface_check()'s own docstring for the full
    rationale and its deliberate no-caching behavior.

    Args:
        root: the parent Tk root this window is opened under.
    """
    from tkinter import ttk

    win = tk.Toplevel(root)
    apply_icon(win, "roadsurface.ico")
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

    # Land Parcel existing-CAMA_RD_SURFACE-column check: detect-on-select,
    # matching the pattern established in lot_location.py/road_width.py/
    # road_frontage.py/road_density.py. Deliberately does NOT cache the
    # result across calls -- every selection AND every Local/Database
    # toggle triggers a fresh read (see group-05-cache-removal-
    # analysis.md). What IS still remembered per mode is only WHICH
    # file/table is selected (parcel_local_path / parcel_db_table
    # above), a separate concern.
    parcel_is_reading = False
    parcel_existing_surface_conflicts = []   # [(path_or_table, existing_col_name)]

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
        Toggle GUI responsiveness while the Land Parcel existing-
        CAMA_RD_SURFACE-column check is in progress. Disables the parcel
        Browse/Select button and the Local/Database radio buttons for
        the duration of the read, preventing a second, concurrent read
        of the same selection.

        The "Reading..." indicator reuses the EXISTING label (parcel_lbl)
        in place -- via whichever StringVar is currently bound to it
        (parcel_files_var for Local, parcel_db_label for Database, per
        _toggle_parcel()'s textvariable swap below) -- rather than
        packing/unpacking a separate status widget, which would reflow
        every widget below it and cause a visible layout jump. Matches
        the corrected pattern already used by lot_location.py/
        road_width.py/road_frontage.py/road_density.py.
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
        existing-CAMA_RD_SURFACE-column check: a read that never
        completed within 60 seconds ("timeout"), or one that completed
        with an actual read error ("failure" -- see
        _check_parcel_surface_conflicts()'s docstring on why this is
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
        nonlocal parcel_local_path, parcel_db_table, parcel_existing_surface_conflicts

        if source_type == "local":
            failed_name = (os.path.basename(parcel_local_path)
                           if parcel_local_path else "the selected file")
            parcel_local_path = None
        else:
            failed_name = parcel_db_table if parcel_db_table else "the selected table"
            parcel_db_table = None

        parcel_existing_surface_conflicts = []

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

    def _poll_parcel_surface_queue(result_queue, source_type, deadline):
        """
        Runs on the main thread via win.after() polling. Picks up the
        conflict list placed on the queue by the background worker, or
        detects a timeout if 60 seconds have elapsed with no result.

        Ordering matters: the queue is ALWAYS checked before the
        deadline -- see road_density.py's identical function for the
        full reasoning (single-threaded Tkinter main loop, fresh
        queue.Queue() per call, no generation counter needed).
        """
        nonlocal parcel_existing_surface_conflicts
        if not win.winfo_exists():
            return
        try:
            conflicts = result_queue.get_nowait()
        except queue.Empty:
            if time.time() >= deadline:
                _handle_parcel_check_failure(source_type, "timeout")
            else:
                win.after(100, lambda: _poll_parcel_surface_queue(
                    result_queue, source_type, deadline))
            return

        if conflicts is None:
            # Worker signaled a read failure (see
            # _check_parcel_surface_conflicts()'s docstring) -- distinct
            # from an empty list, which means "verified, no conflict".
            _handle_parcel_check_failure(source_type, "failure")
            return

        parcel_existing_surface_conflicts = conflicts
        _set_parcel_reading_state(False)

    def _refresh_parcel_surface_check():
        """
        Background-checks the currently selected Land Parcel file/table
        for an existing column matching "cama_rd_surface" -- moved here
        from on_run() (Phase A of Group 5's detect-on-select
        generalization) so the check happens immediately on selection/
        toggle, not only when Run Processing is clicked. Reuses
        _check_parcel_surface_conflicts() (defined above) as the actual
        worker logic -- unchanged from its original synchronous form,
        just now called on a background thread. Gives up after 60
        seconds with no result (see _poll_parcel_surface_queue()) -- a
        hung read must not leave the tool waiting indefinitely.

        Deliberately does NOT cache the result across calls -- every
        call, whether triggered by a fresh Browse/Select or by toggling
        Local <-> Database, always performs a real read. See
        group-05-cache-removal-analysis.md for the full reasoning. What
        IS still remembered across calls is only WHICH file/table is
        selected per mode (parcel_local_path / parcel_db_table) -- a
        separate concern, untouched by this function.
        """
        nonlocal parcel_existing_surface_conflicts
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
            parcel_existing_surface_conflicts = []
            _update_run_button_state()
            return

        result_queue = queue.Queue()

        def worker():
            conflicts = _check_parcel_surface_conflicts(sources, source_type)
            result_queue.put(conflicts)

        deadline = time.time() + 60  # see _poll_parcel_surface_queue()
        _set_parcel_reading_state(True)
        threading.Thread(target=worker, daemon=True).start()
        win.after(100, lambda: _poll_parcel_surface_queue(
            result_queue, source_type, deadline))

    def browse_parcel_files():
        file = filedialog.askopenfilename(filetypes=[
            ("Shapefiles", "*.shp"), ("GeoPackage", "*.gpkg"), ("All", "*.*")])
        # Cancel returns "" -- do not assign, preserving previous selection.
        if file:
            nonlocal parcel_local_path
            parcel_local_path = file
            parcel_files_var.set(os.path.basename(file))
            # Always checks fresh -- see _refresh_parcel_surface_check()
            # docstring: no result is ever cached across calls.
            _refresh_parcel_surface_check()
        _update_run_button_state()

    def _on_parcel_db_selected(sel):
        # Only called on confirmed selection -- Cancel never calls on_select,
        # so parcel_db_table retains its previous value automatically.
        nonlocal parcel_db_table
        parcel_db_table = sel[0]
        parcel_db_label.set(sel[0])
        _refresh_parcel_surface_check()
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
        # Switching Local <-> Database does NOT clear the other mode's
        # remembered selection -- that's pre-existing behavior, left
        # untouched. Always re-checks fresh for whichever mode is now
        # active -- no cached result is ever restored (see
        # group-05-cache-removal-analysis.md).
        _refresh_parcel_surface_check()
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
        """
        Run button handler: validates Land Parcel + Road Network +
        Output selections are present, consults the already-known
        background column-conflict result (PRIORITY 1 -- see
        _refresh_parcel_surface_check()), runs the local output-file
        conflict check (PRIORITY 2), and DB-output table resolution
        (PRIORITY 3) -- each able to cancel the whole run -- then
        destroys this window and hands off to run_processing(). Sets
        the module-level barangay_source, road_source, output_mode, and
        surface_column_overrides globals on success.
        """
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
        # Declining cancels the run entirely; main window stays open
        # (this block runs before win.destroy() further below).
        #
        # Phase A (Group 5 detect-on-select generalization): this no
        # longer calls _check_parcel_surface_conflicts() synchronously
        # here -- the check already ran in the background the moment the
        # Land Parcel source was selected/toggled (see
        # _refresh_parcel_surface_check()). This just consults the
        # already-known result, parcel_existing_surface_conflicts.
        # _update_run_button_state() already guarantees Run cannot be
        # reached while parcel_is_reading is True, so this value is
        # guaranteed current for the actively selected source at this
        # point.
        global surface_column_overrides
        conflicts = parcel_existing_surface_conflicts
        if conflicts:
            lines = "\n\n".join(
                f"'{os.path.basename(path)}' already has the following column(s):\n"
                f"  • {existing_col}"
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
            # Preserve each source's existing column name/casing exactly
            # -- e.g. a detected "caMA_rd_SURFACE" is written back to
            # "caMA_rd_SURFACE", not a hardcoded "CAMA_RD_SURFACE" -- so
            # no duplicate column is ever created regardless of the
            # existing casing. A source with no entry here (no conflict
            # was found) simply uses the default name in
            # process_surface() below.
            surface_column_overrides = dict(conflicts)
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
        # resolved_outcome IS now threaded through (previously discarded
        # as _resolved_outcome -- same bug as the three prior tools
        # before their own fixes): _write_db_output_safely(), called
        # from worker()'s local-source branch, needs it to know whether
        # to stage a backup-and-swap (overwrite) or a plain create.
        resolved_table_name = None
        resolved_outcome = None
        if output_mode[0] == "db":
            _resolve_creds = load_db_credentials()
            if not _resolve_creds:
                return
            _resolve_schema = _resolve_creds["schema"]
            resolved_table_name, resolved_outcome = resolve_db_output_table(
                win, _resolve_schema, barangay_source, _resolve_creds
            )
            if resolved_table_name is None:
                print("Run cancelled by user (database output table not confirmed).")
                return

        win.destroy()
        run_processing(root, overwrite_mode, resolved_table_name, resolved_outcome)

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


# ========================================
# ATOMIC DB WRITE (staging -> verify -> swap -> backup drop)
# ========================================
# Everything from here down to _prompt_orphaned_cama_tables() is
# the post-processing DB-write half of this task and is itself
# NOT cancelable -- gdf.to_postgis() (and the DB-only steps that
# follow it) is a single blocking library call with no hook a
# background thread could check a flag inside of, not a stricter
# design choice specific to this file. See worker() below for the
# actual Cancel checkpoint sequence (pre-processing check, then
# process_surface()'s own internal per-loop checkpoints, then
# disabled once a real result exists).


def _gen_id():
    """
    12 lowercase hex characters (48 bits) of cryptographically random
    entropy, used for both staging and backup table names. Independently
    generated each time it's called -- a staging table and its paired
    backup table get two SEPARATE calls to this function, not one value
    reused with different prefixes.
    """
    return secrets.token_hex(6)


def _advisory_lock_key(run_id):
    """
    Deterministic signed-bigint PostgreSQL advisory-lock key derived from
    a run id. Must produce the IDENTICAL key from the same run_id every
    time: the owning run derives it once (_acquire_run_lock()) to acquire
    the lock, and _scan_orphaned_cama_tables() independently re-derives it
    later from only the run_id text recorded in a table's own COMMENT --
    there is no other channel between the two. run_id is 12 hex chars (48
    bits), always well under pg_try_advisory_lock()'s signed-64-bit range,
    so no overflow/wraparound handling is needed at the current id length.
    """
    return int(run_id, 16)


def _table_exists(conn, schema, table_name):
    """
    True if schema.table_name currently exists as a real relation, via
    PostgreSQL's own to_regclass() -- authoritative against the live
    catalog, not any caller-side cache or an earlier information_schema
    snapshot. Used both for FINAL_SWAP's create-new race re-check and
    inside _write_db_output_safely()'s own outcome-detection fallback.
    """
    result = conn.execute(
        text("SELECT to_regclass(:qualified) IS NOT NULL"),
        {"qualified": f'"{schema}"."{table_name}"'}
    ).scalar()
    return bool(result)


def _get_spatial_index_name(conn, schema, table_name, column_name="geometry"):
    """
    Looks up the actual name of the GIST spatial index attached to a
    column via PostgreSQL's own catalogs (pg_class, pg_index, pg_am,
    pg_attribute), never assuming any naming convention such as
    "idx_<table>_geometry". column_name defaults to "geometry" -- this
    file's own to_postgis() writes never rename the geometry column
    (confirmed: read_postgis_clean() always renames the source geometry
    column to "geometry" before any further processing), so this default
    matches this file's own convention out of the box.

    Returns None if no spatial index exists on this column (e.g. a
    brand-new staging table before to_postgis() has created one). Raises
    RuntimeError if more than one GIST index is found on the same column
    -- this should never happen in this pipeline's normal single-index
    case, so silently picking one would hide a genuinely unexpected schema
    state instead of surfacing it.
    """
    rows = conn.execute(text(
        """
        SELECT ix.relname AS index_name
        FROM pg_class t
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN pg_index idx ON idx.indrelid = t.oid
        JOIN pg_class ix ON ix.oid = idx.indexrelid
        JOIN pg_am am ON am.oid = ix.relam
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(idx.indkey)
        WHERE n.nspname = :schema AND t.relname = :table_name
          AND a.attname = :column_name AND am.amname = 'gist';
        """
    ), {"schema": schema, "table_name": table_name,
        "column_name": column_name}).fetchall()

    if len(rows) == 0:
        return None
    elif len(rows) == 1:
        return rows[0][0]
    else:
        raise RuntimeError(
            f"Expected at most one GIST spatial index on "
            f"\"{schema}\".\"{table_name}\".\"{column_name}\", but found "
            f"{len(rows)}: {[r[0] for r in rows]}. Schema is in an "
            f"unexpected state -- aborting rather than guessing which "
            f"index to rename."
        )


def _index_exists(conn, schema, index_name):
    """True if an index of this exact name already exists in the given
    schema -- pre-flight check before a rename, so a collision produces a
    clear diagnostic instead of a generic PostgreSQL duplicate-object
    error."""
    result = conn.execute(
        text("SELECT 1 FROM pg_indexes WHERE schemaname = :schema "
             "AND indexname = :index_name"),
        {"schema": schema, "index_name": index_name}
    ).fetchone()
    return result is not None


def _rename_spatial_index(conn, schema, table_name, desired_name,
                           column_name="geometry"):
    """
    Finds the actual spatial index on table_name/column_name via
    _get_spatial_index_name() and renames it to desired_name. No-op if no
    spatial index exists yet. Raises a clear diagnostic (rather than
    letting PostgreSQL fail with a generic duplicate-object error) if
    desired_name is already taken by something else in the schema.

    This step exists because PostgreSQL does NOT rename a table's index
    just because the table itself was renamed. Without it, a table
    promoted by FINAL_SWAP below would keep carrying its staging table's
    auto-generated index name indefinitely.
    """
    current_name = _get_spatial_index_name(conn, schema, table_name, column_name)
    if current_name is None:
        return
    if current_name == desired_name:
        return
    if _index_exists(conn, schema, desired_name):
        raise RuntimeError(
            f"Cannot rename spatial index on \"{schema}\".\"{table_name}\" "
            f"to \"{desired_name}\" -- an index with that name already "
            f"exists in schema \"{schema}\". Aborting rather than "
            f"colliding with an unrelated index."
        )
    conn.execute(text(
        f'ALTER INDEX "{schema}"."{current_name}" RENAME TO "{desired_name}";'
    ))


def _drop_table_best_effort(engine, schema, table_name):
    """
    Drops table_name (schema-qualified) without raising. Used for staging
    cleanup after a failed/cancelled write and for the post-swap backup
    drop -- in both cases the property being protected (the real
    destination table's integrity) is already settled by the time this
    runs, so a failure here only means a leftover table for a future run
    to surface. table_name is always a caller-supplied staging_name or
    backup_name here -- never resolved_table_name.
    """
    try:
        with engine.begin() as conn:
            conn.execute(text(
                f'DROP TABLE IF EXISTS "{schema}"."{table_name}" CASCADE;'
            ))
    except Exception as drop_err:
        print(f"  ⚠ Could not drop '{schema}.{table_name}': {drop_err}. "
              f"This does not affect the live table -- it will be surfaced "
              f"as a recoverable leftover item on a future run.")


def _comment_cama_table(conn, schema, table_name, role, dest, run_id,
                         started_at_iso):
    """
    Attaches recovery metadata to a staging or backup table via
    COMMENT ON TABLE. This is metadata ONLY: it plays no role in
    collision safety (the random id in the table name is the entire
    collision-safety mechanism) and no role in proving ownership by
    itself (the advisory lock, re-derived from the run_id recorded here,
    is what _scan_orphaned_cama_tables() actually uses to decide whether
    a run is still alive).

    Format (parsed back by _parse_cama_comment()):
        "cama-run-id: <12-hex>; role: staging|backup; dest: <schema>.<table>; started: <iso8601>"
    """
    comment = (
        f"cama-run-id: {run_id}; role: {role}; "
        f"dest: {schema}.{dest}; started: {started_at_iso}"
    )
    conn.execute(text(f'COMMENT ON TABLE "{schema}"."{table_name}" IS :c'),
                 {"c": comment})


def _parse_cama_comment(comment):
    """
    Parses _comment_cama_table()'s own format back out. Returns
    (run_id, dest), or (None, None) if comment is missing or doesn't match
    the expected format -- e.g. a table that predates this mechanism, or
    one whose comment step never ran because the process died before it
    (STAGING_WRITE succeeded, but the connection was lost before VERIFY's
    own _comment_cama_table() call). _scan_orphaned_cama_tables() treats
    that case as unverifiable and always surfaces it, rather than silently
    skipping a table it can't positively identify.
    """
    if not comment:
        return None, None
    m = re.search(r"cama-run-id:\s*([0-9a-f]{12});.*?dest:\s*([^;]+)", comment)
    if not m:
        return None, None
    return m.group(1).strip(), m.group(2).strip()


def _acquire_run_lock(creds, run_id):
    """
    Opens ONE dedicated, non-pooled psycopg2 connection -- deliberately
    NOT taken from the SQLAlchemy engine's connection pool, see this
    section's own header comment for why -- and acquires a session-level
    advisory lock on it, keyed by _advisory_lock_key(run_id). The
    connection must be kept open, unused for anything else, for the
    entire DB-output portion of this run, and released via
    _release_run_lock() exactly once -- see run_processing()'s worker(),
    which does this in a try/finally so the lock is always released
    (success, failure, or Cancel).

    Returns the open connection (holding the lock) on success, or None if
    the connection itself could not be opened or the lock could not be
    acquired for any reason -- the caller treats None as a hard abort: no
    staging write is ever attempted without a held lock, since the lock is
    what lets a future run's orphan scan tell this run's own artifacts
    apart from a genuinely abandoned one.
    """
    try:
        conn = psycopg2.connect(
            host=creds["host"], port=creds["port"],
            dbname=creds["database"], user=creds["username"],
            password=creds["password"],
        )
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_lock(%s);", (_advisory_lock_key(run_id),))
        conn.commit()
        cur.close()
        return conn
    except Exception as lock_err:
        print(f"  ⚠ Could not acquire DB run lock: {lock_err}")
        return None


def _release_run_lock(lock_conn):
    """
    Releases the advisory lock held by _acquire_run_lock() and closes that
    dedicated connection. Closing the connection is sufficient by itself
    -- PostgreSQL releases all of a session's advisory locks automatically
    when that session ends, so no explicit pg_advisory_unlock() call is
    needed first. Best-effort: a failure here does not affect any
    already-committed table data -- it only means this run's advisory
    lock lingers until PostgreSQL notices the connection is actually gone,
    so a subsequent _scan_orphaned_cama_tables() correctly continues to
    treat this run's own staging/backup artifacts as still "live" until
    then, rather than as newly-orphaned.
    """
    try:
        lock_conn.close()
    except Exception:
        pass


def _write_db_output_safely(engine, schema, gdf, resolved_table_name,
                             resolved_outcome, run_id, started_at_iso):
    """
    The D-Cancel replacement for the direct
    `brgy_gdf.to_postgis(..., if_exists="replace")` call this file's
    worker() previously used. Implements STAGING_WRITE -> VERIFY ->
    FINAL_SWAP -> (DISCARDING on failure) -> post-swap backup cleanup.
    The real destination table (resolved_table_name) is never touched
    before FINAL_SWAP, and FINAL_SWAP is a single PostgreSQL transaction
    -- either both renames inside it commit, or PostgreSQL rolls back
    both and the destination is exactly as it was before this call. This
    function is only ever called from worker() AFTER the run's Cancel
    checkpoints have already passed (see this section's own header
    comment on this file's Cancel granularity) -- nothing inside this
    function is cancelable, by design.

    Args:
        engine: SQLAlchemy engine.
        schema (str): destination schema.
        gdf (GeoDataFrame): the processed result to write.
        resolved_table_name (str): destination table name, from
            resolve_db_output_table() (via on_run()/run_processing()).
        resolved_outcome (str | None): "overwritten" or "created", from
            the same source. If it's anything else (the documented
            defensive-fallback case in worker(), where resolved_table_name
            itself was somehow None), the real outcome is determined here
            directly via _table_exists() rather than guessed.
        run_id (str): this run's _gen_id() value, recorded in the staging/
            backup tables' comments for _scan_orphaned_cama_tables().
        started_at_iso (str): human-readable run start time, for the same
            comment metadata.

    Raises:
        Exception: on any failure at any step. The caller (worker()'s own
        try/except) turns this into the run's error dialog. By the time
        any exception reaches the caller, the staging table has already
        been dropped (best-effort) here, and the destination table is
        guaranteed untouched if the failure happened before FINAL_SWAP's
        transaction committed.
    """
    if resolved_outcome not in ("overwritten", "created"):
        with engine.begin() as conn:
            resolved_outcome = (
                "overwritten" if _table_exists(conn, schema, resolved_table_name)
                else "created"
            )

    is_overwrite = (resolved_outcome == "overwritten")
    staging_name = f"_camastg_{_gen_id()}"
    backup_name = f"_camabak_{_gen_id()}" if is_overwrite else None

    # STAGING_WRITE -- the real destination table is completely untouched
    # at this point. Uses `conn` (not the bare engine), matching this
    # file's own pre-existing to_postgis()-inside-engine.begin() pattern
    # -- a failure partway through this call rolls back cleanly rather
    # than leaving a half-written staging table.
    with engine.begin() as conn:
        gdf.to_postgis(staging_name, conn, schema=schema,
                        if_exists="replace", index=False)

    # VERIFY -- confirm the staging write actually landed, and that its
    # row count matches what we intended to write, before this table is
    # ever eligible for promotion.
    with engine.begin() as conn:
        try:
            actual_count = conn.execute(text(
                f'SELECT COUNT(*) FROM "{schema}"."{staging_name}"'
            )).scalar()
        except Exception as verify_err:
            raise RuntimeError(
                f"Staging table '{staging_name}' could not be verified "
                f"after write -- it may not have been created. Existing "
                f"table '{resolved_table_name}' was not modified. "
                f"Cause: {verify_err}"
            ) from verify_err

        if actual_count != len(gdf):
            _drop_table_best_effort(engine, schema, staging_name)
            raise RuntimeError(
                f"Staging table '{staging_name}' row count "
                f"({actual_count}) does not match the processed result "
                f"({len(gdf)}). Aborting before promotion -- existing "
                f"table '{resolved_table_name}' was not modified."
            )

        _comment_cama_table(conn, schema, staging_name, "staging",
                             resolved_table_name, run_id, started_at_iso)

    # FINAL_SWAP -- one transaction, never cancelable. Either both renames
    # commit, or PostgreSQL rolls back both and resolved_table_name is
    # exactly as it was before this call.
    try:
        with engine.begin() as conn:
            exists_now = _table_exists(conn, schema, resolved_table_name)
            if is_overwrite:
                if not exists_now:
                    raise RuntimeError(
                        f"Expected an existing table '{resolved_table_name}' "
                        f"to overwrite, but it no longer exists. Aborting -- "
                        f"nothing was promoted. The processed data is still "
                        f"available in staging table '{staging_name}'."
                    )
                conn.execute(text(
                    f'ALTER TABLE "{schema}"."{resolved_table_name}" '
                    f'RENAME TO "{backup_name}";'
                ))
                _rename_spatial_index(conn, schema, backup_name,
                                       f"idx_{backup_name}_geometry")
                _comment_cama_table(conn, schema, backup_name, "backup",
                                     resolved_table_name, run_id,
                                     started_at_iso)
            else:
                if exists_now:
                    raise RuntimeError(
                        f"A table named '{resolved_table_name}' now exists, "
                        f"but this run was resolved as a new table. "
                        f"Aborting -- nothing was promoted. The processed "
                        f"data is still available in staging table "
                        f"'{staging_name}'."
                    )
            conn.execute(text(
                f'ALTER TABLE "{schema}"."{staging_name}" '
                f'RENAME TO "{resolved_table_name}";'
            ))
            _rename_spatial_index(conn, schema, resolved_table_name,
                                   f"idx_{resolved_table_name}_geometry")
    except Exception:
        # DISCARDING -- staging was never promoted. This DROP only ever
        # targets staging_name, per this section's own invariant -- never
        # resolved_table_name or backup_name.
        _drop_table_best_effort(engine, schema, staging_name)
        raise

    # Backup is now redundant -- the swap committed, so the new data is
    # confirmed live. Drop immediately, best-effort: never treated as a
    # long-term retention artifact. Normally transient; retained only if
    # this post-commit cleanup itself fails, or the process crashes
    # before it runs -- either way, _scan_orphaned_cama_tables() surfaces
    # it on a future run. A failed drop here does not affect the live
    # table, which is already safely promoted.
    if is_overwrite:
        _drop_table_best_effort(engine, schema, backup_name)


def _scan_orphaned_cama_tables(schema, creds):
    """
    Looks for every _camastg_*/_camabak_* table currently in `schema` and
    returns the ones whose owning run is NOT currently alive. Relies on
    pg_try_advisory_lock() on the SAME key the owning run acquired (see
    _advisory_lock_key()/_acquire_run_lock()), re-derived here from the
    run_id recorded in each table's own COMMENT ON TABLE -- succeeding
    means no live session currently holds that key, i.e. the run that
    created this table is provably gone (a hard process kill, a computer
    shutdown/power loss, or a lost DB connection -- normal completion,
    normal Cancel, and normal failure all clean up after themselves via
    _write_db_output_safely()/_drop_table_best_effort() and never reach
    this scan). A table whose comment is missing or doesn't match the
    expected format (something failed before the comment step itself ever
    ran) is treated as unverifiable and always included, rather than
    silently skipped -- see _parse_cama_comment().

    Read-only with respect to table data: this function only tests locks
    (releasing each one immediately after testing -- it must never itself
    end up holding one) and reads catalog metadata. It never drops
    anything -- see _prompt_orphaned_cama_tables() for the user-driven
    removal step, called from resolve_db_output_table() before its
    existing fuzzy-match logic runs.
    """
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@"
        f"{creds['host']}:{creds['port']}/{creds['database']}"
    )
    orphans = []
    with engine.begin() as conn:
        rows = conn.execute(text(
            """
            SELECT c.relname, obj_description(c.oid, 'pg_class')
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = :schema AND c.relkind = 'r'
              AND (c.relname LIKE '\\_camastg\\_%' ESCAPE '\\'
                   OR c.relname LIKE '\\_camabak\\_%' ESCAPE '\\')
            """
        ), {"schema": schema}).fetchall()

        for relname, comment in rows:
            role = "staging" if relname.startswith("_camastg_") else "backup"
            run_id, dest = _parse_cama_comment(comment)
            if run_id is None:
                orphans.append({"table": relname, "role": role,
                                 "run_id": None, "dest": dest})
                continue
            key = _advisory_lock_key(run_id)
            acquired = conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": key}
            ).scalar()
            if acquired:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                orphans.append({"table": relname, "role": role,
                                 "run_id": run_id, "dest": dest})
            # else: a live run holds this lock -- not orphaned, skip.
    return orphans


def _prompt_orphaned_cama_tables(root, orphans, schema, creds):
    """
    Shown once, from resolve_db_output_table(), before its own normal
    fuzzy-match flow. Presents each orphan (its destination table, role,
    and run start time when available) and offers a per-item checkbox
    choice: remove now, or leave for later manual review. Never deletes
    anything the user hasn't explicitly checked -- declining to check
    anything, or closing this dialog, is the same as choosing "leave"
    for every item found; nothing is ever auto-deleted.
    """
    if not orphans:
        return

    win = tk.Toplevel(root)
    win.title("Recovered Items From an Interrupted Run")
    apply_icon(win, "roadsurface.ico")
    win.resizable(False, False)
    win.transient(root)
    win.grab_set()

    intro = (
        "The following leftover database table(s) were found. These are "
        "normally cleaned up automatically -- they were likely left "
        "behind by a run that was interrupted (e.g. the app or computer "
        "was closed unexpectedly) while writing to the database. Your "
        "existing data was never affected by this."
    )
    tk.Label(win, text=intro, justify="left", wraplength=460,
             padx=14, pady=10).pack()

    list_frame = tk.Frame(win)
    list_frame.pack(fill="x", padx=14)
    vars_by_table = {}
    for o in orphans:
        dest_display = o["dest"] or "unknown (no recovery metadata found)"
        label = f'{o["table"]}   (role: {o["role"]}, destination: {dest_display})'
        var = tk.BooleanVar(value=False)
        vars_by_table[o["table"]] = var
        tk.Checkbutton(list_frame, text=label, variable=var, anchor="w",
                        justify="left", wraplength=440).pack(fill="x", anchor="w")

    tk.Label(
        win,
        text=("Check any items you want removed now. Unchecked items are\n"
              "left in place for manual review and will be shown again."),
        justify="left", padx=14, pady=(6, 10)
    ).pack()

    def _on_remove_selected():
        engine = create_engine(
            f"postgresql://{creds['username']}:{creds['password']}@"
            f"{creds['host']}:{creds['port']}/{creds['database']}"
        )
        for o in orphans:
            if vars_by_table[o["table"]].get():
                _drop_table_best_effort(engine, schema, o["table"])
        win.destroy()

    btn_frame = tk.Frame(win)
    btn_frame.pack(pady=(0, 12))
    tk.Button(btn_frame, text="Remove Checked", command=_on_remove_selected,
              width=16).pack(side="left", padx=6)
    tk.Button(btn_frame, text="Leave All For Now", command=win.destroy,
              width=16).pack(side="left", padx=6)

    win.update_idletasks()
    sw = win.winfo_screenwidth()
    win.geometry(f"+{(sw // 2) - 250}+120")
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))
    win.focus_force()
    win.wait_window()


# ========================================
# DB OUTPUT RESOLUTION
# ========================================
def resolve_db_output_table(root, schema, barangay_source, creds):
    """
    Determines the DB-output destination table for the Land Parcel
    source, BEFORE any processing or writing starts -- same "resolve
    everything up front" philosophy as ask_overwrite_dialog() (see
    run_processing()). road_surface.py has no background worker thread
    -- this function is still called once, up front, for separation of
    responsibilities: this function owns ALL user interaction and
    overwrite decisions, so the processing/write logic further below
    never has to ask any UI or overwrite question of its own.

    Runs the orphaned staging/backup table scan FIRST (creds param,
    added for this), before any of the matching logic below -- same
    "surface leftovers from an interrupted run before asking anything
    else" ordering as road_frontage.py/landmarks_within_meters.py. This
    is orthogonal to the two cases below: it always runs, regardless of
    barangay_source[0].

    Two cases (existing matching logic, unchanged by this task):
      - DB-source Land Parcel (barangay_source[0] == "db"): always
        writes back to the exact same table it was read from -- no
        matching, no dialog. This corrects a confirmed regression in
        the PREVIOUS matching logic, which ran the same fuzzy
        `table.lower() in t.lower()` check regardless of source type,
        meaning even a DB-source run wasn't guaranteed to write back
        to its own source table if a differently-named table in the
        schema happened to substring-match. That behavior is
        intentionally NOT preserved here.
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
    orphans = _scan_orphaned_cama_tables(schema, creds)
    if orphans:
        _prompt_orphaned_cama_tables(root, orphans, schema, creds)

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
# main thread. Same shape of migration as land_shape_compactness.py's:
# new functionality (a progress dialog where none existed), reusing
# progress_framework.py's PresentationState/ProgressPresentationPolicy/
# TkinterProgressView directly -- no tool-local copies, no new
# abstraction.
#
# One additional fix beyond land_shape_compactness.py's migration: this
# tool's process_surface() had a pre-existing bug where a missing
# 'surface' column on the road layer showed an error dialog but then
# CONTINUED -- writing an incomplete output and still reaching the
# final "Success" dialog afterward. That is not preserved -- see
# process_surface()'s own comment at the raise ValueError(...) for the
# full record. It's now a genuine fatal error, routed through the same
# "error" event every other exception already uses -- no new event
# kind was needed.
#
# Deliberately NOT done in this task:
#   - No per-source failure isolation added.
#   - The 3 overwrite dialogs in this file are untouched -- any
#     topmost/hiding fix for them is a separate, dedicated follow-up.
# ============================================================
from utils.progress_framework import (
    PresentationState,
    ProgressPresentationPolicy,
    TkinterProgressView,
    new_cancel_flag,
)


class ProgressWindow:
    """
    Progress dialog shown while run_processing() works on a background
    thread. Same shape as the other migrated tools' ProgressWindow --
    status label + determinate progress bar. Progress Event Protocol
    v9 role: ProgressWindow is the host, not the decision-maker (see
    ProgressPresentationPolicy / TkinterProgressView, imported from
    progress_framework.py, shared with lot_location.py/road_frontage.py/
    terrain.py/land_shape.py/influence_map_distance_to_land_parcel.py/
    road_density.py/influence_map_to_land_parcel.py).

    D-Cancel (this task, fourth tool after landmarks_within_meters.py's
    pilot, lot_location.py's/road_frontage.py's/terrain.py's own
    progress_framework.py-based adoptions): an OPTIONAL cancel_flag,
    passed straight through to TkinterProgressView, wires the title-bar
    X as Cancel -- see __init__'s own docstring. A ProgressWindow
    constructed WITHOUT cancel_flag has no Cancel wiring at all. See
    run_processing() for where the cancel_flag actually comes from
    (utils.progress_framework.new_cancel_flag(), one per run) and for
    this tool's actual Cancel checkpoints -- process_surface()'s own
    per-loop checkpoints in BOTH Loop 1 (roads) and Loop 2 (unmatched
    parcels), full discard on Cancel (see that function's own
    progress-contract docstring). Cancel is disabled (cancelable=False)
    once a real, non-cancelled result exists, immediately before the
    save that follows.
    """
    def __init__(self, root, title="Processing", cancel_flag=None):
        """
        Creates and immediately shows the progress dialog.

        Args:
            root: the parent Tk/Toplevel window.
            title (str): window title. Defaults to "Processing".
            cancel_flag: optional, from utils.progress_framework.
                new_cancel_flag() -- when provided, wires the title-bar
                X to Cancel (see class docstring). None (the default)
                preserves the exact pre-Cancel-support behavior: no
                Cancel wiring at all.
        """
        from tkinter import ttk
        self.win = tk.Toplevel(root)
        apply_icon(self.win, "roadsurface.ico")
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
        self._view = TkinterProgressView(self.win, self.status_var, self.progress,
                                          cancel_flag=cancel_flag)

    def update(self, message, value=None, maximum=None, cancelable=None):
        """Updates the progress display via the shared
        ProgressPresentationPolicy/TkinterProgressView (see class
        docstring). cancelable is optional and, per
        progress_framework.py's own convention, None means "leave the
        title-bar X's enabled/disabled state as it currently is" --
        only has any effect on a window constructed WITH cancel_flag."""
        state = self._policy.compute(message, value, maximum, cancelable)
        self._view.render(state)

    def close(self):
        """Closes the progress window."""
        self._view.destroy()


# ========================================
# RUN
# ========================================
def run_processing(root, overwrite_mode=None, resolved_table_name=None,
                    resolved_outcome=None):
    """
    Orchestrates the full run on a background thread (worker(), started
    at the bottom of this function) with progress reported via a
    queue.Queue polled by poll_queue() on the main thread: loads the
    Road Network layer once, then for each selected Land Parcel
    file/table, runs process_surface() and saves the result either
    locally (.gpkg, optionally opened in Global Mapper) or to PostGIS.

    Args:
        root: the live top-level window, used as the parent for any
        dialogs created here (currently none directly -- resolution
        already happened in on_run() before this was called).
        overwrite_mode (str | None): "overwrite" or "new", from
        ask_overwrite_dialog() in on_run() -- only relevant for local
        output mode.
        resolved_table_name (str | None): the already-confirmed DB
        output table name from resolve_db_output_table() in on_run() --
        only relevant for DB output mode.
        resolved_outcome (str | None): "overwritten" or "created", from
        the same resolve_db_output_table() call in on_run() -- read by
        worker()'s local-source branch when calling
        _write_db_output_safely() (Step 3). Only relevant for DB output
        mode; unused for the DB-source branch, which always resolves
        its own outcome as "overwritten" (writes back to its own
        already-existing source table -- see worker()'s own comment).
    """
    # root: the live top-level window (passed from on_run(); NOT
    # `win`, which is destroyed before run_processing() is ever
    # called -- see on_run()'s win.destroy() immediately before this
    # function's call site). Used as the parent for any dialogs
    # created in this function (currently just
    # resolve_db_output_table()'s DB confirmation dialogs).
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
    # road_gdf loading moves into worker() below (it previously ran
    # inline here, before either loop) -- matches lot_location.py's/
    # road_frontage.py's own established convention of loading the
    # shared road layer inside the background thread, with a status
    # message, rather than blocking the GUI on the main thread first.
    #
    # Everything else below is the exact same two-loop body this
    # function always had (local-source loop, then the separate
    # DB-source loop -- NOT merged), now wrapped inside a background
    # worker() thread instead of running inline on the main thread.
    cancel_flag = new_cancel_flag()
    progress = ProgressWindow(root, "Road Surface Progress", cancel_flag=cancel_flag)
    q = queue.Queue()

    def worker():
        """
        Background-thread body: loads the Road Network layer, then for
        each selected Land Parcel source runs process_surface() and
        saves the result (local .gpkg or PostGIS via
        _write_db_output_safely()'s staging/verify/swap sequence),
        posting progress/completion/error events onto q for
        poll_queue() to consume on the main thread. Never touches
        Tkinter widgets directly (all UI updates happen via
        progress_cb -> q, consumed by poll_queue()).

        D-Cancel (this task, DB-write half): acquires the DB run lock
        (only for DB output mode) before either loop, releases it in a
        finally: block regardless of how this function exits (clean
        completion or an exception) -- see the "ATOMIC DB WRITE" header
        comment above for the full rationale. Cancel-checkpoint wiring
        itself (cancel_flag, per-loop checkpoints inside
        process_surface()) is added separately.
        """
        lock_conn = None
        try:
            def progress_cb(msg, val=None, maxv=None, cancelable=None):
                q.put(("update", msg, val, maxv, cancelable))

            # D-Cancel: one run_id/run_started_at for the whole run --
            # cheap to always generate, even for local output mode,
            # which doesn't use it. The advisory lock itself is only
            # ever acquired for DB output mode, since it exists purely
            # to let a FUTURE run's _scan_orphaned_cama_tables() tell
            # this run's staging/backup artifacts apart from a
            # genuinely abandoned one. Matches landmarks_within_meters.py's/
            # lot_location.py's/road_frontage.py's/terrain.py's own
            # identical placement.
            run_id = _gen_id()
            run_started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            if output_mode[0] == "db":
                lock_conn = _acquire_run_lock(creds, run_id)
                if lock_conn is None:
                    raise RuntimeError(
                        "Could not acquire the database run lock. Another "
                        "CAMA Tools database write may already be in "
                        "progress, or the database connection failed. "
                        "Aborting before any write -- no data was changed."
                    )

            q.put(("update", "Loading road network...", None, None, None))
            if road_source[0] == "local":
                road_gdf = gpd.read_file(road_source[1][0])
            else:
                road_gdf = read_postgis_clean(road_source[1][0], engine, schema)

            if barangay_source[0] == "local":
                for path in barangay_source[1]:
                    q.put(("update", f"Loading {os.path.basename(path)}", None, None, None))
                    brgy_gdf = gpd.read_file(path)
                    # output_column_name: preserves the exact existing column
                    # name/casing this LOCAL source's parcel layer already had
                    # (if the user confirmed overwriting one at Run time -- see
                    # on_run()'s confirmation dialog). A source with no entry
                    # here falls back to process_surface()'s own default
                    # ("CAMA_RD_SURFACE").
                    output_column_name = surface_column_overrides.get(path, "CAMA_RD_SURFACE")
                    result = process_surface(
                        brgy_gdf, road_gdf,
                        output_column_name=output_column_name,
                        progress=progress_cb,
                        cancel_flag=cancel_flag,
                    )
                    if result is None:
                        # D-Cancel: Cancel fired inside process_surface()
                        # (Loop 1 or Loop 2) -- full discard, per
                        # Blocker #1's resolved decision. Nothing for
                        # this source is saved; output_mode[1]/the DB
                        # is never touched (the save step below is
                        # never reached), and no further source (in
                        # this loop or the DB-source loop below) is
                        # attempted. Same-tick "flash" of this message
                        # before the terminal "cancelled" dialog, no
                        # artificial delay.
                        q.put(("update", "Discarding run... Please wait.", None, None, False))
                        q.put(("cancelled", None, None, None))
                        return
                    # D-Cancel: from here through the save, Cancel is
                    # disabled -- a real result now exists and nothing
                    # past this point can be safely interrupted (the
                    # save itself is not cancelable -- see
                    # _write_db_output_safely()).
                    progress_cb(f"Saving {os.path.basename(path)}", cancelable=False)
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
                        # happened there (see that function's docstring).
                        # Falls back to the old "base + _roadsurface" behavior
                        # only if resolved_table_name is somehow None here
                        # (output_mode[0] != "db" can't reach this branch, so
                        # this is just a defensive fallback).
                        base = os.path.splitext(os.path.basename(path))[0]
                        out_table = resolved_table_name if resolved_table_name is not None else base + "_roadsurface"
                        # D-Cancel: staging-write / verify / atomic rename-
                        # swap, replacing the previous direct
                        # to_postgis(..., if_exists="replace") call -- see
                        # "ATOMIC DB WRITE" above.
                        _write_db_output_safely(engine, schema, result,
                                                 out_table, resolved_outcome,
                                                 run_id, run_started_at)
                        print(f"🔄 Saved to DB: {out_table}")
            else:
                # Database Land Parcel sources: extended (Fix 3) to
                # respect surface_column_overrides, same as the LOCAL
                # branch above -- preserves the exact existing column
                # casing detected in on_run()'s PRIORITY 1 check instead
                # of always defaulting to "CAMA_RD_SURFACE".
                for table in barangay_source[1]:
                    q.put(("update", f"Loading DB table {table}", None, None, None))
                    brgy_gdf = read_postgis_clean(table, engine, schema)
                    output_column_name = surface_column_overrides.get(table, "CAMA_RD_SURFACE")
                    result = process_surface(
                        brgy_gdf, road_gdf,
                        output_column_name=output_column_name,
                        progress=progress_cb,
                        cancel_flag=cancel_flag,
                    )
                    if result is None:
                        # D-Cancel: Cancel fired inside process_surface()
                        # (Loop 1 or Loop 2) -- full discard, per
                        # Blocker #1's resolved decision. Nothing for
                        # this source is saved; the DB is never touched
                        # (the save step below is never reached), and
                        # no further DB-source table is attempted.
                        # Same-tick "flash" of this message before the
                        # terminal "cancelled" dialog, no artificial
                        # delay.
                        q.put(("update", "Discarding run... Please wait.", None, None, False))
                        q.put(("cancelled", None, None, None))
                        return
                    # D-Cancel: from here through the save, Cancel is
                    # disabled -- a real result now exists and nothing
                    # past this point can be safely interrupted (the
                    # save itself is not cancelable -- see
                    # _write_db_output_safely()).
                    progress_cb(f"Saving {table}", cancelable=False)
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
                        # DB-source -> DB-output: writes back to the exact SAME
                        # table it read from -- no matching, no dialog. This
                        # replaces the previous inline `table.lower() in
                        # t.lower()` check, which was a confirmed regression:
                        # even a DB-source run wasn't guaranteed to write back
                        # to its own source table if a differently-named table
                        # in the schema happened to substring-match.
                        # resolved_table_name/resolved_outcome (from on_run()'s
                        # resolve_db_output_table() call) are NOT used here --
                        # only relevant to the LOCAL-source branch above. This
                        # branch's outcome is always "overwritten": `table`
                        # was just read from at the top of this loop (see
                        # read_postgis_clean(table, ...) above), so it is
                        # unconditionally an existing table.
                        # D-Cancel: staging-write / verify / atomic rename-
                        # swap, replacing the previous direct
                        # to_postgis(..., if_exists="replace") call -- see
                        # "ATOMIC DB WRITE" above.
                        _write_db_output_safely(engine, schema, result,
                                                 table, "overwritten",
                                                 run_id, run_started_at)
                        print(f"🔄 Saved to DB: {table}")

            q.put(("done", "Processing complete!", None, None))

        except Exception as e:
            # New: this function had no top-level try/except before --
            # an uncaught exception here previously propagated silently
            # (no graceful dialog), EXCEPT for the missing-surface-column
            # case, which previously showed messagebox.showerror() then
            # incorrectly continued (see process_surface()'s own
            # comment) -- that bug is fixed by this same try/except now
            # catching the raise it performs instead, and routing it
            # through the identical "error" event as every other
            # exception. No new event kind was needed for that fix.
            q.put(("error", str(e), None, None))
        finally:
            # D-Cancel: released here regardless of how the try block
            # above exits -- clean success, a Cancel (either `return`
            # right after a "cancelled" event above), or an exception
            # (the `except` clause's own fallthrough). A lingering
            # advisory lock past this point would make a FUTURE run's
            # _scan_orphaned_cama_tables() wrongly treat this run's own
            # staging/backup tables as still "live" even after this run
            # has fully ended.
            if lock_conn is not None:
                _release_run_lock(lock_conn)

    def poll_queue():
        """
        Main-thread poller (scheduled via root.after(100, ...)): drains
        q and updates the progress dialog, opens the result in Global
        Mapper, or shows the final success/error/cancelled dialog and
        stops polling, depending on the event kind. All Tkinter calls
        happen here, never inside worker() itself.

        D-Cancel: a new "cancelled" event kind is handled the same way
        as "done"/"error": closes the progress window, shows a
        terminal dialog, and stops polling.

        Backlog / terminal-event-starvation fix (D-Cancel): both of
        process_surface()'s loops are deliberately unthrottled (fire
        progress() on every road/every unmatched parcel, no i % N
        throttle -- see that function's own docstring), so a large
        road or parcel set can enqueue thousands of "update" events
        before this poller gets a turn. Draining every queued item
        with an unbounded `while True:` was previously harmless, but
        becomes a real problem now that a Cancel click needs the
        mainloop free to be processed promptly, and q is strict FIFO
        -- a naive fixed-size-cap drain would leave "cancelled" stuck
        behind however many "update" events preceded it. Since
        progress updates are pure display state (not transactional
        events -- superseded by whatever the next one says), it is
        safe to coalesce consecutive "update" payloads, keeping only
        the LATEST one before a terminal/action event (or before q
        goes empty); "open_gm"/"done"/"error"/"cancelled" are never
        coalesced or deferred. Net effect: "cancelled" is reached in
        the same tick it was queued in, regardless of how many stale
        "update" events precede it, because reaching it only requires
        cheap get_nowait() calls, not a render per item. Matches
        terrain.py's own identical fix.
        """
        if not root.winfo_exists():
            return
        try:
            pending_update = None
            while True:
                kind, *rest = q.get_nowait()
                if kind == "update":
                    # Coalesce: only the latest "update" payload seen
                    # before the next terminal/action event (or before
                    # q goes empty) is kept -- superseded ones are
                    # discarded unrendered.
                    pending_update = rest
                    continue
                # A non-"update" event follows: flush whatever the
                # latest pending "update" was (if any) BEFORE acting on
                # this event, so the displayed status text/bar never
                # goes stale relative to what's about to happen (e.g.
                # the "Discarding run..." update queued right before
                # "cancelled" is still shown, not skipped).
                if pending_update is not None:
                    progress.update(pending_update[0], pending_update[1], pending_update[2], pending_update[3])
                    pending_update = None
                if kind == "open_gm":
                    load_in_global_mapper(rest[0])
                elif kind == "done":
                    progress.close()
                    messagebox.showinfo("Success", rest[0])
                    return
                elif kind == "error":
                    progress.close()
                    messagebox.showerror("Error", rest[0])
                    return
                elif kind == "cancelled":
                    progress.close()
                    messagebox.showinfo("Cancelled", "The run was cancelled. No data was changed.")
                    return
        except queue.Empty:
            # q is empty for now -- render whatever the latest pending
            # "update" was (if any) so the dialog reflects the most
            # recent state, then wait for the next tick.
            if pending_update is not None:
                progress.update(pending_update[0], pending_update[1], pending_update[2], pending_update[3])
        root.after(100, poll_queue)

    threading.Thread(target=worker, daemon=True).start()
    poll_queue()


# ========================================
# MAIN / ENTRYPOINT
# ========================================
def main(parent=None):
    """
    Tool entry point. If parent is given (invoked from within another
    running Tk app), reuses it and just opens this tool's window.
    Otherwise creates and hides a new Tk root, applies this tool's icon,
    and enters its own mainloop -- the standalone-subprocess dispatch
    path.

    Args:
        parent: an existing Tk root to reuse, or None to create one.
    """
    if parent is not None:
        open_main_window(parent)
    else:
        root = tk.Tk()
        apply_icon(root, "roadsurface.ico")
        root.withdraw()
        open_main_window(root)
        root.mainloop()


if __name__ == "__main__":
    main()