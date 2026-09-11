"""
tools/land_shape.py

PURPOSE:
    CAMA Tools tool ("LAND SHAPE" in MAIN.py's dispatch table): for each
    Land Parcel, computes the Polsby-Popper compactness ratio
    (4*pi*area/perimeter^2) and classifies the lot's shape based on its
    interior vertex angles (TRIANGLE, RECTANGLE, L_SHAPED, or OTHERS).
    Writes eight CAMA_-prefixed output columns: CAMA_PP_RATIO,
    CAMA_VTX_COUNT, CAMA_ANGS_TXT, CAMA_TRIANGLE, CAMA_RECTANGLE,
    CAMA_L_SHAPED, CAMA_OTHERS (one-hot columns for the four shape
    classes), and CAMA_LOT_SHAPE (the CAMA_-prefixed shape label itself).

DISPATCH:
    Run as an isolated subprocess by MAIN.py via its `--tool` dispatch
    mechanism (see system context). Entry point is main(), triggered via
    the `if __name__ == "__main__":` guard at the bottom of this file.

INPUTS:
    Land Parcel source: a single local file (.shp, .gpkg, or any file
    type via the "All" filter) or a single PostGIS table.
    pg_credentials.json (via load_db_credentials(), from
    utils/db_discovery.py) for any DB source or DB output.

OUTPUTS:
    Local output mode: writes one atomically-written .gpkg
    (_write_gpkg()), then attempts to open it in Global Mapper
    (load_in_global_mapper()).
    DB output mode: writes/replaces one PostGIS table via
    _write_db_output_safely()'s staging-table-write -> verify ->
    atomic-rename-swap -> backup-drop sequence, targeting a destination
    resolved via resolve_db_output_table() -- an exact-match replace for
    a DB Land Parcel source, or a fuzzy-match-with-confirmation flow
    (confirm_db_overwrite_dialog() / choose_db_overwrite_dialog()) for a
    local-file Land Parcel source. resolve_db_output_table() also
    surfaces (and offers to clean up) any leftover staging/backup table
    from a previously interrupted run before its own matching logic
    runs.

DEPENDENCIES:
    stdlib: os, re, math, subprocess, json, secrets, threading, queue,
    time, ctypes, sys, tkinter.
    third-party: geopandas, numpy, shapely (geometry + validation),
    psycopg2, sqlalchemy.
    local: utils.table_name_matching, utils.resource_path,
    utils.db_discovery, utils.column_detection, utils.window_icon,
    utils.progress_framework (imported mid-file, directly above the
    class/function that uses it -- see the Progress Event Protocol v9
    comment block further below for why this file's progress dialog was
    migrated to that shared framework).

SIDE EFFECTS:
    File reads/writes (.shp/.gpkg). PostGIS reads/writes. A live
    PostgreSQL connection. Tkinter GUI windows throughout, including a
    background thread + queue.Queue-based polling loop for both the
    Land-Parcel existing-column check (detect-on-select) and the main
    processing run itself. A subprocess launch to Global Mapper
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
import secrets
import threading
import queue
import time
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox

import geopandas as gpd
import numpy as np
from shapely.geometry import Polygon, MultiPolygon
from shapely.validation import make_valid
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
output_mode = None

# ========================================
# OUTPUT-COLUMN CONFLICT DETECTION
# ========================================
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

# ========================================
# GEOMETRY FIX
# ========================================
def fix_geometry(geom):
    """Repairs an invalid geometry via buffer(0), falling back to
    make_valid() if that isn't enough. Returns None for a None, empty,
    or unrepairable geometry."""
    if geom is None or geom.is_empty: return None
    try:
        if not geom.is_valid:
            geom = geom.buffer(0)
        if not geom.is_valid:
            geom = make_valid(geom)
        return geom if not geom.is_empty else None
    except:
        return None

# ========================================
# HELPERS
# ========================================
def angle_between(p1, p2, p3):
    """Returns the interior angle at p2 (in degrees, 0-360) formed by
    the rays p2->p1 and p2->p3."""
    v1 = (p1[0] - p2[0], p1[1] - p2[1])
    v2 = (p3[0] - p2[0], p3[1] - p2[1])
    dot = v1[0]*v2[0] + v1[1]*v2[1]
    det = v1[0]*v2[1] - v1[1]*v2[0]
    angle_rad = math.atan2(det, dot)
    angle_deg = math.degrees(angle_rad)
    return round(angle_deg + 360 if angle_deg < 0 else angle_deg, 2)

def vertex_angles(polygon: Polygon):
    """
    Computes the interior angle at each vertex of a polygon's exterior
    ring, after deduplicating consecutive coincident points.

    Args:
        polygon (shapely.Polygon): the polygon to measure.

    Returns:
        list[float]: one angle (degrees) per vertex, in ring order.
        Empty list if fewer than 3 distinct vertices remain.
    """
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
    """
    Classifies a polygon's shape from its vertex angles (see
    vertex_angles()) into one of four bare internal labels: "TRIANGLE",
    "RECTANGLE", "L_SHAPED", or "OTHERS" (the fallback/default).
    Angle-bucket thresholds (<=169 "low"/near-straight,
    170-190 "rightish", 190-260 "obtuse", 260-280 "L-shaped candidate")
    encode the actual classification rules; see the branches below for
    exactly how each shape is distinguished.

    Args:
        angles (list[float]): vertex angles in degrees, from
        vertex_angles().

    Returns:
        str: one of "TRIANGLE", "RECTANGLE", "L_SHAPED", "OTHERS".
    """
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
    """Returns geom itself if it's a Polygon, the largest-area member if
    it's a MultiPolygon, or None otherwise/if empty."""
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

# ========================================
# PARCEL COLUMN-CONFLICT CHECK
# ========================================
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
# ATOMIC DB WRITE (D-Cancel)
# ========================================
# Sixth tool to receive this mechanism, after landmarks_within_meters.py
# (pilot), lot_location.py, road_frontage.py, terrain.py, road_surface.py,
# and road_density.py. Ported from road_density.py -- the closest
# structural precedent, since this file shares its single-loop,
# in-place-cell-mutation processing shape (compute_ppr_and_lot_shape_gdf()
# has one loop, same as process_density()). Nothing here is
# source-agnostic-generalized into a shared utils/ module -- Rule of
# Three (Section G.5): this is the sixth occurrence, still not yet its
# own separate decision. Each tool keeps its own copy.
#
# STAGING_WRITE -> VERIFY -> FINAL_SWAP -> (DISCARDING on failure) ->
# post-swap backup drop -- the real destination table (resolved_table_name)
# is never touched before FINAL_SWAP, and FINAL_SWAP is a single
# PostgreSQL transaction: either both renames inside it commit, or
# PostgreSQL rolls back both and the destination is exactly as it was
# before the call. A run that is hard-killed (process kill, computer
# shutdown/power loss, lost DB connection) between STAGING_WRITE and the
# post-swap backup drop leaves a `_camastg_*`/`_camabak_*` table behind;
# _scan_orphaned_cama_tables() below surfaces it to a FUTURE run via
# resolve_db_output_table(), since normal completion, normal Cancel, and
# normal failure all clean up after themselves here and never reach that
# scan.
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
    `result.to_postgis(..., if_exists="replace")` calls this file's
    worker() previously used at both call sites. Implements
    STAGING_WRITE -> VERIFY -> FINAL_SWAP -> (DISCARDING on failure) ->
    post-swap backup cleanup. The real destination table
    (resolved_table_name) is never touched before FINAL_SWAP, and
    FINAL_SWAP is a single PostgreSQL transaction -- either both renames
    inside it commit, or PostgreSQL rolls back both and the destination
    is exactly as it was before this call. This function is only ever
    called from worker() AFTER the run's Cancel checkpoint (inside
    compute_ppr_and_lot_shape_gdf()) has already passed and returned a
    real result -- nothing inside this function is cancelable, by design.

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
    apply_icon(win, "landshape.ico")
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
    dialog.title("LAND SHAPE TOOL")
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
    dialog.title("LAND SHAPE TOOL")
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
# CORE COMPUTATION
# ========================================
def compute_ppr_and_lot_shape_gdf(gdf,
        pp_ratio_col="CAMA_PP_RATIO", vtx_count_col="CAMA_VTX_COUNT",
        angs_txt_col="CAMA_ANGS_TXT", triangle_col="CAMA_TRIANGLE",
        rectangle_col="CAMA_RECTANGLE", l_shaped_col="CAMA_L_SHAPED",
        others_col="CAMA_OTHERS", lot_shape_col="CAMA_LOT_SHAPE",
        progress=None, cancel_flag=None):
    """
    progress : optional callable progress(message, value=None, maximum=None),
    called from inside the per-feature classification loop below (never
    from anywhere else in this function). Optional and defaults to None
    so this function's existing signature/behavior is unchanged for any
    call site that doesn't pass it -- added as part of this tool's
    Progress Event Protocol v9 migration (see run_processing() below).

    cancel_flag : optional dict from utils.progress_framework.
    new_cancel_flag() (added for D-Cancel). Checked directly, once per
    iteration, at the TOP of the per-feature loop below -- an
    explicit-True-only check (`cancel_flag["stop"] is True`), matching
    the explicit-False-only contract this function's own progress
    callback already uses elsewhere, so a cancel_flag of None (the
    default) or {"stop": False} never trips this. Per this task's
    resolved decision (full-discard, matching landmarks_within_meters.py's/
    lot_location.py's/road_frontage.py's/terrain.py's/road_surface.py's/
    road_density.py's own precedent): on Cancel, this function returns
    None immediately -- nothing computed so far (including any per-row
    .at[] mutations already applied to gdf in a partially-completed
    pass) is kept, and the caller (worker(), in run_processing()) never
    reaches the save step for this source. This function has only ONE
    loop, so there is only one checkpoint, not a "which loop(s)"
    question.

    None (the default) preserves this function's exact pre-D-Cancel
    behavior: the loop never checks anything, and this function always
    returns a real (non-None) result -- matching this parameter's
    optional, backward-compatible shape.

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
    # UI-accuracy fix (this task): progress(...) below posts one event onto
    # worker()'s queue per call. Timestamped diagnostic testing on a real
    # 11911-feature run showed the compute loop can generate progress
    # events far faster than poll_queue()'s 100ms polling interval can
    # drain them -- observed: ~305 queued "Classifying feature..." events
    # per single poll_queue() tick. The result: the on-screen progress
    # number visibly lags several seconds behind the loop's TRUE current
    # position, and can even lag behind the point where the loop has
    # already fully finished (compute + save both complete) while the
    # displayed text still reads an old "Classifying feature N/total".
    # This does not affect correctness of the underlying Cancel mechanism
    # -- cancel_flag is still checked on every iteration below, at full
    # frequency, un-throttled -- it only affects how often the DISPLAY is
    # updated. Throttling the display to roughly match poll_queue()'s own
    # ~100ms cadence keeps the queue from ever accumulating a large
    # backlog, so what's on screen stays close to the loop's true current
    # position. _last_progress_t is local to this call (reset to 0 so the
    # very first iteration always posts immediately); the final iteration
    # (i == total) always posts as well, regardless of timing, so the
    # last number seen is always the true final count, right before the
    # "Saving <table>" message that follows.
    _last_progress_t = 0.0
    for i, (idx, geom) in enumerate(fixed_geoms.items(), start=1):
        # D-Cancel: single checkpoint (this task's resolved decision:
        # full discard). Checked at the TOP of each iteration, before
        # any work for this feature happens -- explicit-True-only, so a
        # cancel_flag of None (the no-Cancel-support default) or
        # {"stop": False} never trips this. Full discard: nothing
        # assigned so far is kept -- the in-place .at[] mutations
        # already applied to gdf are simply abandoned along with the
        # whole gdf object once this function returns None; the caller
        # never uses a None return for anything. Deliberately NOT
        # throttled -- always checked at full per-iteration frequency,
        # independent of the display-throttling below.
        if cancel_flag is not None and cancel_flag["stop"] is True:
            return None
        if progress:
            _now = time.time()
            if i == total or (_now - _last_progress_t) >= 0.1:
                progress(f"Classifying feature {i}/{total}", i, total)
                _last_progress_t = _now
        # D-Cancel: explicit GIL-release yield -- not a real wait, just a
        # hint to CPython to consider switching threads here. Investigated
        # as a possible fix for a reported "Cancel not caught near the end
        # of a run" case; timestamped diagnostic testing traced that case
        # to the run genuinely finishing (compute + save both complete)
        # before the physical click occurred, not to any GIL-scheduling
        # delay -- so this does not change Cancel's observable behavior.
        # Kept as harmless, zero-risk insurance for slower machines/larger
        # datasets, per explicit decision after that investigation.
        time.sleep(0)
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
    """Opens path in Global Mapper (subprocess), if both GM_EXE_PATH and
    path exist. Simpler than load_in_global_mapper() further below (no
    EnumWindows focus-existing-window step) -- appears unused by
    run_processing(), which calls load_in_global_mapper() instead; kept
    as-is, not removed or consolidated (see Section 3.E.7 of the
    governing instructions)."""
    if os.path.exists(GM_EXE_PATH) and os.path.exists(path):
        subprocess.Popen([GM_EXE_PATH, path], shell=True)

# ========================================
# DB HELPERS
# ========================================
def get_geometry_column(table, engine, schema):
    """
    Looks up the geometry column name for a PostGIS table via the
    geometry_columns system view.

    Args:
        table (str): the table to look up.
        engine: a SQLAlchemy engine.
        schema (str): the schema the table lives in.

    Returns:
        str | None: the geometry column name, or None if not found.
    """
    with engine.connect() as conn:
        row=conn.execute(text("""
            SELECT f_geometry_column FROM geometry_columns
            WHERE f_table_schema=:schema AND f_table_name=:table
        """),{"schema":schema,"table":table}).fetchone()
        return row[0] if row else None

def read_postgis_clean(table,engine,schema):
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
    geom_col=get_geometry_column(table,engine,schema)
    insp=inspect(engine)
    cols=[c['name'] for c in insp.get_columns(table,schema=schema) if c['name']!=geom_col]
    col_str=", ".join([f'"{c}"' for c in cols]) if cols else ""
    q=f'SELECT {col_str+", " if col_str else ""}"{geom_col}" AS geometry FROM "{schema}"."{table}"'
    return gpd.read_postgis(q,engine,geom_col="geometry")

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


# ========================================
# MAIN WINDOW
# ========================================
def open_main_window(root):
    """
    Builds and shows the tool's single unified configuration window: a
    Land Parcel source picker (Local-file/Database-table radio toggle),
    an Output destination picker, and a Run button gated by
    _update_run_button_state().

    The Land Parcel picker additionally runs a background,
    detect-on-select check (_refresh_parcel_output_check(), via a
    daemon thread + win.after()-polled queue.Queue) for any of the 8
    OUTPUT_COLUMN_TARGETS already existing on the source, the moment a
    file/table is selected or the Local/Database toggle changes -- not
    only when Run is clicked. See _refresh_parcel_output_check()'s own
    docstring for the full rationale and its deliberate no-caching
    behavior.

    Args:
        root: the parent Tk root this window is opened under.
    """
    from tkinter import ttk
    win = tk.Toplevel(root)
    apply_icon(win, "landshape.ico")
    win.title("Land Shape Tool")
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
        """
        Run button handler: validates Land Parcel + Output selections
        are present, consults the already-known background column-
        conflict result (PRIORITY 1 -- see
        _refresh_parcel_output_check()), runs the local output-file
        conflict check (PRIORITY 2), and DB-output table resolution
        (PRIORITY 3) -- each able to cancel the whole run -- then
        destroys this window and hands off to run_processing(). Sets
        the module-level barangay_source, output_mode, and
        parcel_output_column_overrides globals on success.
        """
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
        # D-Cancel: resolved_outcome is now threaded through to
        # run_processing() -- previously discarded here as
        # _resolved_outcome (the same bug already fixed in the five
        # prior tools before their own fixes): _write_db_output_safely(),
        # called from worker(), needs it to know whether to stage a
        # backup-and-swap (overwrite) or a plain create.
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


# ========================================
# DB OUTPUT RESOLUTION
# ========================================
def resolve_db_output_table(root, schema, barangay_source, creds):
    """
    Determines the DB-output destination table for the Land Parcel
    source, BEFORE any processing or writing starts -- same "resolve
    everything up front" philosophy as ask_overwrite_dialog() (see
    run_processing()). land_shape.py has no background
    worker thread -- this function is still called once, up front, for
    separation of responsibilities: this function owns ALL user
    interaction and overwrite decisions, so the processing/write logic
    further below never has to ask any UI or overwrite question of its
    own.

    Runs the orphaned staging/backup table scan FIRST (creds param,
    added for this), before any of the matching logic below -- same
    "surface leftovers from an interrupted run before asking anything
    else" ordering as road_density.py/road_surface.py/road_frontage.py/
    landmarks_within_meters.py. This is orthogonal to the two cases
    below: it always runs, regardless of barangay_source[0].

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
#
# D-Cancel (separate, later task, sixth tool after
# landmarks_within_meters.py's pilot, lot_location.py's/road_frontage.py's/
# terrain.py's/road_surface.py's/road_density.py's own adoptions): added
# Cancel-during-processing (compute_ppr_and_lot_shape_gdf()'s own single
# checkpoint, full discard) and the atomic DB-write mechanism
# (_write_db_output_safely() and its helpers, above) on top of this
# already-existing Progress Event Protocol v9 migration -- see each
# function's own docstring for details. Nothing in this section's
# original migration is altered by that later task.
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
    thread. Same shape as lot_location.py's/road_frontage.py's own
    ProgressWindow -- status label + determinate progress bar. Progress
    Event Protocol v9 role: ProgressWindow is the host, not the
    decision-maker (see ProgressPresentationPolicy / TkinterProgressView,
    imported from progress_framework.py).

    D-Cancel (this task, sixth tool after landmarks_within_meters.py's
    pilot, lot_location.py's/road_frontage.py's/terrain.py's/
    road_surface.py's/road_density.py's own progress_framework.py-based
    adoptions): an OPTIONAL cancel_flag, passed straight through to
    TkinterProgressView, wires the title-bar X as Cancel -- see
    __init__'s own docstring. A ProgressWindow constructed WITHOUT
    cancel_flag has no Cancel wiring at all. See run_processing() for
    where the cancel_flag actually comes from
    (utils.progress_framework.new_cancel_flag(), one per run) and for
    this tool's actual Cancel checkpoint -- compute_ppr_and_lot_shape_gdf()'s
    own single per-feature-loop checkpoint, full discard on Cancel (see
    that function's own progress-contract docstring). Cancel is disabled
    (cancelable=False) once a real, non-cancelled result exists,
    immediately before the save that follows.

    Used by (progress_framework.py, current as of this task): shared
    with lot_location.py, road_frontage.py, road_surface.py,
    influence_map_distance_to_land_parcel.py, terrain.py,
    road_density.py, and influence_map_to_land_parcel.py -- 8 tools
    total, this one included.
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
        # Event Protocol v9), via progress_framework.py. Constructed
        # after the widgets they render into already exist.
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
    queue.Queue polled by poll_queue() on the main thread: for each
    selected Land Parcel file/table, runs compute_ppr_and_lot_shape_gdf()
    and saves the result either locally (.gpkg, optionally opened in
    Global Mapper) or to PostGIS.

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
    cancel_flag = new_cancel_flag()
    progress = ProgressWindow(root, "Land Shape Progress", cancel_flag=cancel_flag)
    q = queue.Queue()

    def worker():
        """
        Background-thread body: for each selected Land Parcel source,
        runs compute_ppr_and_lot_shape_gdf() and saves the result
        (local .gpkg or PostGIS via _write_db_output_safely()'s
        staging/verify/swap sequence), posting progress/completion/
        error/cancelled events onto q for poll_queue() to consume on
        the main thread. Never touches Tkinter widgets directly (all UI
        updates happen via progress_cb -> q, consumed by poll_queue()).

        D-Cancel (this task, DB-write half): acquires the DB run lock
        (only for DB output mode) before either loop, releases it in a
        finally: block regardless of how this function exits (clean
        completion, a Cancel, or an exception) -- see the "ATOMIC DB
        WRITE" header comment above for the full rationale. D-Cancel
        (Cancel-checkpoint half): cancel_flag is threaded into every
        compute_ppr_and_lot_shape_gdf() call; a None result means
        Cancel fired inside that call (full discard, per this task's
        resolved decision) -- the save step for that source is skipped
        entirely and the run ends via a "cancelled" event, matching the
        resolved decision.
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
            # lot_location.py's/road_frontage.py's/terrain.py's/
            # road_surface.py's/road_density.py's own identical
            # placement.
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

            if barangay_source[0] == "local":
                for path in barangay_source[1]:
                    q.put(("update", f"Loading {os.path.basename(path)}", None, None, None))
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
                        cancel_flag=cancel_flag,
                    )
                    if result is None:
                        # D-Cancel: Cancel fired inside
                        # compute_ppr_and_lot_shape_gdf() -- full discard,
                        # per this task's resolved decision. Nothing for
                        # this source is saved; the DB is never touched
                        # (the save step below is never reached), and no
                        # further Land Parcel source is attempted.
                        # Same-tick "flash" of this message before the
                        # terminal "cancelled" dialog, no artificial delay.
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
                        # happened there (see that function's docstring). This
                        # just uses the result. Falls back to the old
                        # filename-lowercased behavior only if
                        # resolved_table_name is somehow None here
                        # (output_mode[0] != "db" can't reach this branch, so
                        # this is just a defensive fallback).
                        local_name = os.path.splitext(os.path.basename(path))[0]
                        table = resolved_table_name if resolved_table_name is not None else local_name.lower()
                        # D-Cancel: staging-write / verify / atomic rename-
                        # swap, replacing the previous direct
                        # to_postgis(..., if_exists="replace") call -- see
                        # "ATOMIC DB WRITE" above.
                        _write_db_output_safely(engine, schema, result,
                                                 table, resolved_outcome,
                                                 run_id, run_started_at)
                        print(f"🔄 Saved to DB: {table}")
            else:
                # Database Land Parcel sources: extended (Fix 3) to
                # respect parcel_output_column_overrides, same as the
                # LOCAL branch above -- preserves the exact existing
                # column casing(s) detected in on_run()'s PRIORITY 1
                # check instead of always defaulting to the eight
                # hardcoded CAMA_-prefixed names.
                for table in barangay_source[1]:
                    q.put(("update", f"Loading DB table {table}", None, None, None))
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
                        cancel_flag=cancel_flag,
                    )
                    if result is None:
                        # D-Cancel: Cancel fired inside
                        # compute_ppr_and_lot_shape_gdf() -- full discard,
                        # per this task's resolved decision. Nothing for
                        # this source is saved; the DB is never touched
                        # (the save step below is never reached), and no
                        # further DB-source table is attempted.
                        # Same-tick "flash" of this message before the
                        # terminal "cancelled" dialog, no artificial delay.
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
                        # table it read from -- no matching, no dialog.
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
            # already-migrated tools.
            q.put(("error", str(e), None, None))
        finally:
            # D-Cancel: released here regardless of how the try block
            # above exits -- clean success, a Cancel (the `return`
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

        D-Cancel: "update" events now carry a 4th (cancelable) field --
        passed straight through to progress.update(), which forwards
        it to ProgressPresentationPolicy.compute() (None means "leave
        the title-bar X's state as-is", per progress_framework.py's
        own convention). A new "cancelled" event kind is handled the
        same way as "done"/"error": closes the progress window, shows
        a terminal dialog, and stops polling.

        UI-accuracy fix (this task): previously this drained the ENTIRE
        queue unconditionally in one call (`while True: ... q.get_nowait()`).
        On a fast run, the background thread can queue hundreds of
        ordinary "Classifying feature N/total" updates (cancelable=None)
        followed by the "Saving <table>" milestone (cancelable=False)
        and then "done", all within a single 100ms polling interval.
        Draining every one of those in one Python call meant the
        "Saving..." state -- though genuinely reached and genuinely
        rendered via TkinterProgressView.render()'s own win.update() --
        was immediately overwritten by "done" in the very same tick,
        before the OS ever painted that intermediate frame. Confirmed
        via timestamped diagnostic testing: an entire compute+save could
        finish in ~2.4s, making "Saving <table>" visible for only a
        fraction of a frame.

        Fix: an ordinary progress tick (cancelable is None -- ordinary
        "Classifying feature..." updates) keeps draining in the same
        tight loop as before, so run speed and overall completion time
        are completely unaffected. But the moment an event carries
        cancelable=False (this file's own signal for "Saving <table>",
        the one real milestone between processing and completion), this
        loop renders it and STOPS draining for this tick -- deferring
        anything queued after it (including "done") to the next
        root.after(100, ...) tick, so that milestone gets its own
        genuine ~100ms on screen before being replaced. No artificial
        delay is added anywhere in the run itself; this only changes how
        many already-queued events get flushed together on the UI side.
        """
        if not root.winfo_exists():
            return
        try:
            while True:
                kind, *rest = q.get_nowait()
                if kind == "update":
                    progress.update(rest[0], rest[1], rest[2], rest[3])
                    if rest[3] is False:
                        # A milestone update ("Saving <table>") was just
                        # rendered -- give it this tick to actually be
                        # seen instead of immediately draining further
                        # (possibly straight into "done").
                        break
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
                elif kind == "cancelled":
                    progress.close()
                    messagebox.showinfo("Cancelled", "The run was cancelled. No data was changed.")
                    return
        except queue.Empty:
            pass
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
        apply_icon(root, "landshape.ico")
        root.withdraw()
        open_main_window(root)
        root.mainloop()


if __name__ == "__main__":
    main()