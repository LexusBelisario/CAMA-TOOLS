"""
tools/influence_map_distance_to_land_parcel.py

PURPOSE:
    CAMA Tools tool ("INFLUENCE MAP DISTANCE TO LAND PARCEL" in MAIN.py's dispatch table):
    for each Land Parcel, finds the single nearest feature in a Fault
    Line Map layer (Point, LineString, or Polygon geometry all
    supported) and writes CAMA_FAULT_NAME and CAMA_FAULT_DISTANCE. The
    nearest feature is always selected by true geometric proximity
    (never a centroid/representative-point approximation); only the
    reported distance's measurement method varies by the winning
    feature's geometry type -- see process_parcels()'s own docstring
    for the full Point/LineString-vs-Polygon distinction.

DISPATCH:
    Run as an isolated subprocess by MAIN.py via its `--tool` dispatch
    mechanism (see system context). Entry point is main(), triggered via
    the `if __name__ == "__main__":` guard at the bottom of this file.

INPUTS:
    Land Parcel source: one or more local vector files or PostGIS
    tables.
    Fault Line Map source: a single local vector file (with explicit
    layer disambiguation for an ambiguous multi-layer GeoPackage -- see
    resolve_local_fault_layer()) or a single PostGIS table.
    pg_credentials.json (via load_db_credentials(), from
    utils/db_discovery.py) -- always loaded up front by
    run_processing(), even for an all-local run.

OUTPUTS:
    Local output mode: writes one atomically-written .gpkg per
    processed Land Parcel source (_write_gpkg()), then attempts to open
    it in Global Mapper (load_in_global_mapper()). A companion Visual
    Measurement (VM) layer is computed but its write is currently
    disabled -- see the "Visual Measurement (VM) layer write --
    DISABLED" comment inside _process_one_source() for the full,
    deliberately-preserved reasoning.
    DB output mode: writes/replaces one PostGIS table per source,
    resolved via resolve_db_output_table(). A companion CAMA_Table
    write is also computed but currently disabled -- see the
    "CAMA_Table write -- TEMPORARILY DISABLED" comment in the same
    function.

DEPENDENCIES:
    stdlib: os, re, sys, time, json, threading, queue, subprocess,
    ctypes, tkinter (+ ttk).
    third-party: geopandas, numpy, psycopg2, sqlalchemy, shapely
    (geometry, ops, strtree, validation), fiona (imported locally,
    only where GeoPackage layer inspection is needed).
    local: utils.table_name_matching, utils.resource_path,
    utils.db_discovery, utils.column_detection, utils.window_icon,
    tools.progress_framework.

SIDE EFFECTS:
    File reads/writes (.shp/.gpkg). PostGIS reads/writes. A live
    PostgreSQL connection (loaded unconditionally by run_processing(),
    even for an all-local run -- see that function's own docstring).
    Tkinter GUI windows throughout, including a background thread +
    queue.Queue-based polling loop for the main processing run. A
    subprocess launch to Global Mapper (load_in_global_mapper()) on
    local-output saves.

    IMPORTANT -- this module has a genuine import-time side effect: the
    module-level call to set_app_user_model_id() (see the "FORCE
    WINDOWS APP ICON" section below) invokes the Win32
    SetCurrentProcessExplicitAppUserModelID API the moment this file is
    imported or run -- not lazily, not inside main(). This affects how
    Windows groups/identifies this process's taskbar icon. Preserved
    exactly as found -- not moved, deferred, or wrapped in a function --
    since doing so would change when this Windows-level identification
    happens, which is out of scope for a documentation/reorganization
    task (see Section C of the governing instructions: no behavior
    changes). Note this file's version wraps the API call in its own
    try/except (unlike the bare call used in some other tool files) --
    an existing, harmless difference, not something this pass changes.

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
from __future__ import annotations
import os
import re
import sys
import time
import json
import threading
import queue
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox, ttk

import geopandas as gpd
import numpy as np
import psycopg2
from sqlalchemy import create_engine, text
from shapely.geometry import Point, LineString
from shapely.ops import nearest_points
from shapely.strtree import STRtree
from shapely.validation import make_valid

from utils.table_name_matching import normalize_name, find_matching_tables
from utils.resource_path import resource_path
from utils.db_discovery import load_db_credentials, fetch_tables
from utils.column_detection import detect_existing_output_columns
from utils.window_icon import apply_icon

# ============================
# FORCE WINDOWS APP ICON
# ============================
import ctypes


# NOTE: import-time side effect -- this call executes the moment this
# module is loaded, before main() runs (see module docstring SIDE
# EFFECTS). Not moved or deferred; see module docstring for why.
def set_app_user_model_id():
    appid = u"BLGF.CAMA.Tools.2025"
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(appid)
    except Exception:
        pass


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
# ------------------------------------------------------------------
# Module-level selection state.
#
# parcel_source : ("local", [path, ...]) or ("db", [table, ...])
#     MULTI-select, mirroring influence_to_barangay.py's barangay_source
#     exactly -- Land Parcel is processed per-source in a loop, same as
#     every other tool in this project.
#
# fault_source : ("local", path, layer_name) or ("db", table)
#     SINGLE-select, deliberately different shape from parcel_source --
#     this tool takes exactly ONE Fault Line Map input (see Task spec).
#     layer_name is only meaningful for a local .gpkg source; it is the
#     user-confirmed (never auto-defaulted) layer chosen at file-
#     selection time -- see _prompt_select_layer() below.
#
# output_mode : ("local", dir) or ("db", None)
# ------------------------------------------------------------------
parcel_source = None
fault_source = None
output_mode = None

VECTOR_FILETYPES = [
    ("Vector files", "*.shp *.gpkg"),
    ("Shapefiles", "*.shp"),
    ("GeoPackage", "*.gpkg"),
    ("All files", "*.*"),
]

# Fixed output columns for this tool. NOT dynamic/per-layer -- see
# Known Open Question #1 in the Task Prompt: this tool is permanently
# fault-line-specific for the current implementation.
#
# Renamed from CAMA_FAULT_LINE_NAME/NEAREST_FAULT_LINE: (1) the missing
# CAMA_ prefix on the distance column was a bug -- every other output
# column in this tool and across CAMA Tools is CAMA_-prefixed; (2) the
# "_LINE" wording no longer fits now that Polygon and Point Fault Line
# features are formally supported (see process_parcels()'s geometry-
# type switch below) -- these column names describe the FAULT domain/
# dataset the value came from, not a geometry shape, matching the
# project's existing convention (e.g. ROAD_WIDTH doesn't imply roads
# are always drawn a particular way either).
OUTPUT_COLUMN_TARGETS = ("CAMA_FAULT_NAME", "CAMA_FAULT_DISTANCE")


# ========================================
# DB HELPERS
# ========================================
def get_geom_column(engine, schema, table):
    """Detect the geometry column name from PostGIS system catalogs."""
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                SELECT f_geometry_column
                FROM geometry_columns
                WHERE f_table_schema = :schema AND f_table_name = :table;
            """
                ),
                {"schema": schema, "table": table},
            ).fetchone()
            if result:
                return result[0]
    except Exception:
        pass
    return "geometry"


def read_postgis_clean(table, engine, schema):
    """Reads a PostGIS table, always exposing the geometry column as
    'geometry' regardless of its actual name in the DB. Ported verbatim
    from terrain.py's own helper -- self-contained duplication per this
    project's no-shared-module convention (Rule of Three)."""
    geom_col = get_geom_column(engine, schema, table)
    query = f'SELECT * FROM "{schema}"."{table}"'
    gdf = gpd.read_postgis(query, engine, geom_col=geom_col)
    if geom_col != "geometry":
        gdf = gdf.rename(columns={geom_col: "geometry"}).set_geometry("geometry")
    return gdf


# ========================================
# FILE READING (PARCEL)
# ========================================
def read_vector_file(path: str) -> gpd.GeoDataFrame:
    """
    Reads a Land Parcel vector file (SHP or GPKG). Ported verbatim from
    influence_to_barangay.py -- used ONLY for the Land Parcel source,
    which is not the layer where this task's multi-layer-GPKG ambiguity
    concern applies (Land Parcel layers in this project are always
    single-layer in practice, same assumption every other tool already
    makes). The Fault Line Map source uses its OWN, stricter reader --
    see read_fault_line_source() below -- which never silently
    defaults on an ambiguous multi-layer GeoPackage.
    """
    ext = os.path.splitext(path)[1].lower()

    if ext == ".gpkg":
        import fiona
        layers = fiona.listlayers(path)
        if not layers:
            raise ValueError(f"No layers found in GeoPackage: {path}")
        stem = os.path.splitext(os.path.basename(path))[0].lower()
        matched_layer = next((l for l in layers if l.lower() == stem), layers[0])
        if len(layers) > 1:
            print(f"ℹ️ GPKG has {len(layers)} layers: {layers}. Using: '{matched_layer}'")
        return gpd.read_file(path, layer=matched_layer)

    return gpd.read_file(path)


def get_local_name(path: str) -> str:
    """Clean layer/table name from a file path. Ported verbatim from
    influence_to_barangay.py."""
    ext = os.path.splitext(path)[1].lower()
    stem = os.path.splitext(os.path.basename(path))[0]
    if ext == ".gpkg":
        try:
            import fiona
            layers = fiona.listlayers(path)
            if layers:
                matched = next((l for l in layers if l.lower() == stem.lower()), layers[0])
                return matched
        except Exception:
            pass
    return stem


# ========================================
# FILE READING (FAULT LINE MAP)
# ========================================
def _list_gpkg_layers(path):
    import fiona
    return fiona.listlayers(path)


def _prompt_select_layer(parent, path, layers):
    """
    Explicit, blocking layer-selection dialog for an ambiguous
    multi-layer GeoPackage. Shown ONCE, at file-selection time (not at
    Run time, and not silently defaulted) -- resolves Phase 1 Blocker
    #2: the actual PH_FAULT_LINES.gpkg sample file has two layers
    ("PH_Fault_Line", "gem_active_faults_harmonized"), neither of which
    matches the file stem "PH_FAULT_LINES", so
    influence_to_barangay.py's stem-match-then-layers[0] convention
    would silently pick the wrong (or an arbitrary) layer here.

    Returns the chosen layer name, or None if the user cancelled.
    """
    result = {"layer": None}

    dlg = tk.Toplevel(parent)
    apply_icon(dlg, "distancefactor.ico")
    dlg.title("Select Fault Line Layer")
    dlg.resizable(False, False)
    dlg.grab_set()
    dlg.attributes("-topmost", True)

    tk.Label(
        dlg,
        text=(
            f"'{os.path.basename(path)}' contains {len(layers)} layers.\n"
            "Select the ONE layer to use as the Fault Line Map:"
        ),
        justify="left", wraplength=360, padx=12, pady=10,
    ).pack()

    frame = tk.Frame(dlg)
    frame.pack(padx=12, pady=(0, 8), fill="both", expand=True)
    lb = Listbox(frame, selectmode="browse", height=min(8, max(3, len(layers))), width=50)
    lb.pack(side="left", fill="both", expand=True)
    sb = tk.Scrollbar(frame, command=lb.yview)
    sb.pack(side="right", fill="y")
    lb.config(yscrollcommand=sb.set)
    for l in layers:
        lb.insert("end", l)
    lb.selection_set(0)

    def on_ok():
        sel = lb.curselection()
        if not sel:
            messagebox.showwarning("No Selection", "Please select a layer.", parent=dlg)
            return
        result["layer"] = layers[sel[0]]
        dlg.destroy()

    def on_cancel():
        result["layer"] = None
        dlg.destroy()

    dlg.protocol("WM_DELETE_WINDOW", on_cancel)

    btn_frame = tk.Frame(dlg)
    btn_frame.pack(pady=(0, 12))
    tk.Button(btn_frame, text="OK", width=10, command=on_ok).pack(side="left", padx=6)
    tk.Button(btn_frame, text="Cancel", width=10, command=on_cancel).pack(side="left", padx=6)

    dlg.update_idletasks()
    dlg.lift()
    dlg.focus_force()
    dlg.wait_window()
    return result["layer"]


def resolve_local_fault_layer(parent, path):
    """
    Called once, at file-selection time (inside the GUI's Browse
    handler), for a local Fault Line Map file. For a non-GPKG file,
    returns None (no layer concept). For a GPKG:
      - 1 layer  -> used directly, no prompt.
      - >1 layers, exactly one matches the file stem -> used directly
        (same convention every other tool already relies on, kept for
        the common case where it IS unambiguous).
      - >1 layers, no unambiguous stem match -> explicit
        _prompt_select_layer() dialog; returns None only if the user
        cancels (caller must then treat the whole file selection as
        cancelled, not silently fall back to a first-layer guess).
    """
    ext = os.path.splitext(path)[1].lower()
    if ext != ".gpkg":
        return None  # not applicable; single-layer format
    layers = _list_gpkg_layers(path)
    if not layers:
        raise ValueError(f"No layers found in GeoPackage: {path}")
    if len(layers) == 1:
        return layers[0]
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    stem_matches = [l for l in layers if l.lower() == stem]
    if len(stem_matches) == 1:
        return stem_matches[0]
    return _prompt_select_layer(parent, path, layers)


def read_fault_line_source(fault_source_tuple, engine=None, schema=None) -> gpd.GeoDataFrame:
    """
    Reads the single Fault Line Map source, given the already-resolved
    (path, layer_name) or (table,) selection from fault_source. Never
    performs its own layer disambiguation -- that already happened at
    selection time (resolve_local_fault_layer()); this function only
    consumes the confirmed result.
    """
    kind = fault_source_tuple[0]
    if kind == "local":
        path, layer_name = fault_source_tuple[1], fault_source_tuple[2]
        ext = os.path.splitext(path)[1].lower()
        if ext == ".gpkg" and layer_name:
            return gpd.read_file(path, layer=layer_name)
        return gpd.read_file(path)
    else:
        table = fault_source_tuple[1]
        return read_postgis_clean(table, engine, schema)


# ========================================
# GEOMETRY
# ========================================
def ensure_geometry_column(gdf):
    """Renames gdf's geometry column to "geometry" if it isn't already
    (handles a "geom"-named column, or any other active geometry column
    name), so downstream code can always assume the column is literally
    called "geometry"."""
    if "geometry" not in gdf.columns and "geom" in gdf.columns:
        gdf = gdf.rename(columns={"geom": "geometry"}).set_geometry("geometry")
    elif gdf.geometry.name != "geometry":
        gdf = gdf.set_geometry(gdf.geometry.name)
        gdf = gdf.rename_geometry("geometry")
    return gdf


def fix_geometry(geom, context_label=None):
    """
    Ported verbatim from road_frontage.py. Geometry-type-aware repair:
    buffer(0) is a polygon-repair technique that can silently collapse
    a LineString into an empty polygon, so lines are repaired with
    make_valid() instead. Correct for this tool's LineString fault
    data today, and forward-compatible for Point/Polygon Fault Line
    layers per Section E's scoping.

    context_label: optional identifier (e.g. "parcel PIN=123-45-6" or
    "fault feature row 7") logged to the console whenever repair is
    actually needed -- audit trail for which specific records trigger
    shapely's make_valid() (the source of the
    "RuntimeWarning: invalid value encountered in make_valid" GEOS
    warning seen on some real-world shapefiles). Purely additive: when
    omitted (the default), behavior is 100% unchanged from before --
    no logging, same repair logic.
    """
    if geom is None or geom.is_empty:
        return None
    try:
        if not geom.is_valid:
            if geom.geom_type in {"Polygon", "MultiPolygon"}:
                geom = geom.buffer(0)
            if not geom.is_valid:
                if context_label:
                    print(f"ℹ️ Repairing invalid geometry via make_valid() for {context_label}")
                geom = make_valid(geom)
        if geom.is_empty:
            if context_label:
                print(f"⚠️ Geometry for {context_label} is empty after repair -- will be skipped.")
            return None
        return geom
    except Exception as e:
        if context_label:
            print(f"⚠️ Geometry repair failed for {context_label}: {type(e).__name__}: {e}")
        return None


# ========================================
# FAULT LINE NAME-FIELD DETECTION
# ========================================
# Deliberately NOT a call into influence_to_barangay.py's
# detect_attr_name() -- that function's fallback chain ends in an
# "ELEVATION" rule (designed for terrain/elevation influence layers)
# and, failing that, silently returns the FIRST non-geometry column.
# Verified against the actual PH_FAULT_LINES.gpkg sample data: both
# layers have a "name" column AND an "ELEVATION" column, so
# detect_attr_name() would silently pick elevation values instead of
# fault names for this exact production dataset -- unacceptable for
# this tool. This detector is bounded and deterministic: a short list
# of exact candidate names, then (only as a last resort) a normalized
# substring match against the layer/table name -- no ELEVATION rule,
# no "first column" fallback. If nothing matches, the caller must show
# an explicit error and abort; this function never guesses.
_FAULT_NAME_CANDIDATES = ("name", "fault_name", "fault", "feature_name")


def detect_fault_name_field(gdf, name_guess: str):
    """
    Returns the column name to use for CAMA_FAULT_NAME, or None if
    no suitable column could be determined (caller must show an
    explicit error and abort -- see PRIORITY 1 conflict-check read and
    run_processing() below).
    """
    cols = [c for c in gdf.columns if c.lower() not in ("geometry", "geom")]
    lower_map = {c.lower(): c for c in cols}

    for candidate in _FAULT_NAME_CANDIDATES:
        if candidate in lower_map:
            return lower_map[candidate]

    norm_layer = normalize_name(name_guess)
    for col in cols:
        ncol = normalize_name(col)
        if ncol and (ncol in norm_layer or norm_layer in ncol):
            return col

    return None


# ========================================
# PRS92 CRS ZONE DETECTION
# ========================================
# Ported verbatim (table + function) from road_width.py.
PRS92_ZONE_BOUNDS = [
    (-180.0, 118.0, 3121, "Zone I"),
    (118.0,  120.0, 3122, "Zone II"),
    (120.0,  122.0, 3123, "Zone III"),
    (122.0,  124.0, 3124, "Zone IV"),
    (124.0,  180.0, 3125, "Zone V"),
]


def detect_prs92_zone(labeled_gdfs):
    valid = [
        (label, g) for label, g in labeled_gdfs
        if g is not None and not g.empty and g.geometry.notna().any()
    ]
    if not valid:
        raise ValueError("No valid (non-empty) GeoDataFrames provided for PRS92 zone detection.")

    all_bounds = []
    for label, gdf in valid:
        g = gdf
        if g.crs is None:
            g = g.set_crs(epsg=4326)
            print(f"⚠️ No CRS found in the '{label}' layer -- assuming "
                  "WGS84. Measurements may be incorrect if the actual CRS "
                  "is different.")
        epsg = g.crs.to_epsg()
        g_wgs84 = g.to_crs(epsg=4326) if epsg != 4326 else g

        bounds = g_wgs84.total_bounds
        if np.isnan(bounds).any():
            raise ValueError(
                f"Cannot determine PRS92 zone because the '{label}' layer "
                f"contains no valid geometry."
            )
        all_bounds.append(bounds)

    minx = min(b[0] for b in all_bounds)
    maxx = max(b[2] for b in all_bounds)
    center_lon = (minx + maxx) / 2

    for lon_min, lon_max, epsg, zone_label in PRS92_ZONE_BOUNDS:
        if lon_min <= center_lon < lon_max:
            if not (lon_min <= minx and maxx < lon_max):
                print(f"⚠️ Dataset longitude range ({minx:.4f}°E to {maxx:.4f}°E) "
                      f"extends outside the detected {zone_label} bounds "
                      f"({lon_min}°E-{lon_max}°E). Features near the dataset "
                      f"edge may be very slightly less accurate.")
            return epsg

    raise ValueError(f"Could not determine PRS92 zone for longitude {center_lon}")


# ========================================
# OUTPUT-COLUMN CONFLICT DETECTION
# ========================================
# Fixed target list -- OUTPUT_COLUMN_TARGETS -- same static-tuple
# pattern as terrain.py's own OUTPUT_COLUMN_TARGETS, NOT
# influence_to_barangay.py's dynamic per-source target list (that
# tool's targets vary per Influence Map layer; ours are fixed by
# explicit direction -- Section E).
def _check_parcel_output_conflicts(sources, source_type):
    """
    Checks each selected Land Parcel source for pre-existing
    OUTPUT_COLUMN_TARGETS columns.

    Self-contained: loads its own DB credentials/engine internally for
    the "db" case, rather than requiring the caller to pre-load and pass
    them in (as this function's signature originally required). Changed
    (Phase A, Group 5 detect-on-select generalization) because this is
    now called from a background thread triggered by Land Parcel
    selection/toggle, independent of on_run() -- matches the
    self-contained pattern already used by every other tool's equivalent
    worker (e.g. road_density.py's _check_parcel_density_conflicts()).

    Returns a list of (path_or_table, {target: existing_col_name}) on a
    SUCCESSFUL read/check -- an empty list means the check succeeded and
    found no conflict. Returns None if credentials could not be loaded,
    or if ANY source failed to read -- this is a REQUIRED distinction,
    not cosmetic: an empty list means "verified, no conflict", while
    None means "could not verify at all". A previous version of this
    function treated read failure as skip-only/non-blocking ("mirrors
    influence_to_barangay.py's own... never a conflict-check failure")
    -- no longer safe now that the check runs at selection time,
    potentially long before Run is ever clicked (see the timeout/
    failure-handling design notes for this change -- the check must
    always reflect a fresh read, never a cached result).
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
                gdf = read_vector_file(path_or_table)
            else:
                gdf = read_postgis_clean(path_or_table, engine, schema)
        except Exception as e:
            print(f"⚠️ Could not read parcel layer to check for existing "
                  f"output column(s): {path_or_table}: {e}")
            return None
        found = detect_existing_output_columns(gdf, OUTPUT_COLUMN_TARGETS)
        if found:
            conflicts.append((path_or_table, found))
    return conflicts


# parcel_output_column_overrides: {path_or_table: {"CAMA_FAULT_NAME":
# actual_name, "CAMA_FAULT_DISTANCE": actual_name}} -- populated at Run
# time when a Land Parcel source already has a pre-existing matching
# column and the user confirms proceeding. Same convention as
# influence_to_barangay.py's own override map.
parcel_output_column_overrides = {}


# ========================================
# MEASUREMENT ENGINE
# ========================================
def process_parcels(parcel_gdf, fault_gdf, name_field, source_name,
                     progress_cb=None, output_column_map=None):
    """
    Core measurement engine. For each parcel: representative_point() ->
    single nearest-feature STRtree lookup against the Fault Line layer
    -> CAMA_FAULT_NAME, CAMA_FAULT_DISTANCE, and the VM line all
    derived from that SAME lookup result (single-nearest-feature-query
    rule -- see Task Prompt / Instructions Section E; this is a hard
    architectural requirement, not a style preference).

    Selection metric vs. measurement metric (approved design):
    The nearest feature is ALWAYS selected by true geometric proximity
    -- shapely's STRtree.nearest() already ranks candidates by real
    .distance() (point-to-point, point-to-line, or point-to-polygon,
    whichever applies), never by a centroid/representative-point
    approximation, and this holds even when Point, LineString, and
    Polygon features are mixed within the same Fault Line layer. This
    selection step is NEVER changed by geometry type -- "nearest
    feature" means the same thing regardless of what wins.

    Only AFTER a winner is chosen does geometry type affect anything,
    and only the REPORTED value / VM line, never which feature won:
      - Point / LineString (and Multi- variants): CAMA_FAULT_DISTANCE
        is the true geometric distance from parcel_point to the
        feature, and the VM line runs to the exact nearest_points()
        endpoint on that feature -- this is this tool's original,
        LineString-validated behavior, unchanged.
      - Polygon / MultiPolygon: CAMA_FAULT_DISTANCE is instead the
        distance from parcel_point to the fault feature's OWN
        representative_point() (center-to-center), and the VM line
        runs to that same representative_point() -- NOT standard
        boundary distance (which would read 0 whenever the parcel
        falls inside a fault zone polygon, an unhelpful value for a
        hazard-distance metric). This supersedes Known Open Question
        #2's original boundary-distance default, per confirmed Team
        Lead direction.
    See the geometry-type switch in the per-parcel loop below for the
    exact implementation.

    Duplicate feature names are acceptable and require no special
    handling: the nearest feature is selected purely by spatial
    proximity, never by uniqueness of its NAME attribute, and that
    NAME is copied verbatim onto CAMA_FAULT_NAME.

    Returns (parcel_gdf, vm_gdf) -- both still in the CALLER's current
    (projected) CRS; CRS restoration to the original CRS is the
    caller's responsibility (mirrors road_width.py's process()).
    """
    output_column_map = output_column_map or {}
    name_col = output_column_map.get("CAMA_FAULT_NAME", "CAMA_FAULT_NAME")
    dist_col = output_column_map.get("CAMA_FAULT_DISTANCE", "CAMA_FAULT_DISTANCE")

    id_col = next(
        (c for c in parcel_gdf.columns
         if c.upper() in ("PIN", "ARP_NO", "TD_NO", "PARCEL_ID")),
        None
    )

    vm_columns = (["PIN"] if id_col else []) + [name_col, dist_col, "geometry"]

    # ------------------------------------------------------------------
    # Fault Line geometry cleanup + spatial index.
    #
    # PERFORMANCE RULE (explicit, per review): the STRtree is built
    # EXACTLY ONCE per Fault Line source for this whole process_parcels()
    # call -- never rebuilt inside the per-parcel loop below. Every
    # parcel's nearest-feature lookup reuses this same tree.
    # ------------------------------------------------------------------
    fault_geoms = []
    fault_attrs = []  # parallel list -- fault_attrs[i] is the name value for fault_geoms[i]
    for fault_idx, (_, row) in enumerate(fault_gdf.iterrows()):
        fault_name_for_log = row.get(name_field) if name_field in fault_gdf.columns else None
        fault_label = f"fault feature '{fault_name_for_log}' (row {fault_idx})" if fault_name_for_log else f"fault feature row {fault_idx}"
        g = fix_geometry(row.geometry, context_label=fault_label)
        if g is None:
            continue
        fault_geoms.append(g)
        fault_attrs.append(row[name_field] if name_field in fault_gdf.columns else None)

    if not fault_geoms:
        print(f"⚠️ [{source_name}] No usable Fault Line geometry -- "
              f"every parcel will receive NULL output values.")
        if progress_cb:
            for _ in range(len(parcel_gdf)):
                progress_cb(1)
        parcel_gdf[name_col] = None
        parcel_gdf[dist_col] = None
        vm_gdf = gpd.GeoDataFrame(columns=vm_columns, geometry="geometry", crs=parcel_gdf.crs)
        return parcel_gdf, vm_gdf

    tree = STRtree(fault_geoms)

    names_out = []
    dists_out = []
    vm_records = []

    for idx, poly in enumerate(parcel_gdf.geometry):
        if progress_cb:
            progress_cb(1)

        parcel_label = (
            f"parcel {id_col}={parcel_gdf.iloc[idx][id_col]}" if id_col
            else f"parcel row {idx}"
        )
        poly = fix_geometry(poly, context_label=parcel_label)
        if poly is None:
            print(f"⚠️ [{source_name}] Skipping parcel at row {idx}: null/empty/unrepairable geometry.")
            names_out.append(None)
            dists_out.append(None)
            continue

        parcel_point = poly.representative_point()

        # ---- SINGLE nearest-feature lookup for this parcel ----
        res = tree.nearest(parcel_point)
        if isinstance(res, (int, np.integer)):
            nearest_geom = fault_geoms[int(res)]
            nearest_name = fault_attrs[int(res)]
        else:
            # Newer shapely returns the geometry directly; recover its
            # index for the parallel attribute lookup.
            try:
                nearest_idx = fault_geoms.index(res)
            except ValueError:
                nearest_idx = None
            nearest_geom = res
            nearest_name = fault_attrs[nearest_idx] if nearest_idx is not None else None

        # ---- Measurement metric (decided AFTER the winner is chosen --
        # see process_parcels()'s docstring for the full Option A
        # rationale). The winning feature itself is never re-selected
        # here; only how its distance/VM-line are computed changes. ----
        if nearest_geom.geom_type in ("Polygon", "MultiPolygon"):
            # Polygon Fault Line feature (e.g. a fault hazard zone):
            # center-to-center, NOT boundary distance -- boundary
            # distance would read 0 whenever the parcel falls inside
            # the zone, which is not a useful hazard-distance value.
            fault_ref_point = nearest_geom.representative_point()
            distance = round(float(parcel_point.distance(fault_ref_point)), 4)
            vm_line = LineString([parcel_point, fault_ref_point])
        else:
            # Point / LineString (and Multi- variants): unchanged --
            # true geometric distance, VM endpoint is the exact
            # nearest_points() point ON the feature, matching the
            # distance value exactly. This is this tool's original,
            # LineString-validated behavior.
            distance = round(float(parcel_point.distance(nearest_geom)), 4)
            _, pt_on_fault = nearest_points(parcel_point, nearest_geom)
            vm_line = LineString([parcel_point, pt_on_fault])

        names_out.append(nearest_name)
        dists_out.append(distance)

        record = {name_col: nearest_name, dist_col: distance, "geometry": vm_line}
        if id_col:
            record["PIN"] = parcel_gdf.iloc[idx][id_col]
        vm_records.append(record)

    parcel_gdf[name_col] = names_out
    parcel_gdf[dist_col] = dists_out

    if vm_records:
        vm_gdf = gpd.GeoDataFrame(vm_records, geometry="geometry", crs=parcel_gdf.crs)
        vm_gdf = vm_gdf[vm_columns]
    else:
        vm_gdf = gpd.GeoDataFrame(columns=vm_columns, geometry="geometry", crs=parcel_gdf.crs)

    return parcel_gdf, vm_gdf


# ========================================
# DB OUTPUT TABLE RESOLUTION
# ========================================
def resolve_db_output_table(root, schema, parcel_src, desired_name):
    """
    Ported from influence_to_barangay.py's resolve_db_output_table(),
    generalized to accept a single desired_name (this function is
    called once PER Land Parcel source in the batch, not once for the
    whole run -- see run_processing()). Same two cases:
      - DB-source Land Parcel: always overwrites the exact same table
        -- but the user must still explicitly confirm this, same as
        every other DB-overwrite path in this tool.
      - Local-file Land Parcel: fuzzy match + user confirmation.
    Returns (resolved_table_name, resolved_outcome), or (None, None) if
    the user cancelled. A (None, None) return here propagates straight
    back through on_run()'s PRIORITY 3 loop, which returns immediately
    without calling win.destroy() -- the configuration window stays
    open and the user can reconfigure and try again, same as every
    other cancellable step in this tool's Run flow.
    """
    if parcel_src[0] == "db":
        # Land Parcel source IS a DB table -- output necessarily
        # overwrites that exact same table. Previously this returned
        # "overwritten" immediately with NO confirmation at all --
        # confirmed gap, now fixed by reusing the same
        # confirm_db_overwrite_dialog() used everywhere else in this
        # tool for a DB-table overwrite decision.
        if not confirm_db_overwrite_dialog(root, desired_name):
            return None, None
        return desired_name, "overwritten"

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


# ========================================
# PROGRESS WINDOW
# ========================================
from tools.progress_framework import (
    PresentationState,
    ProgressPresentationPolicy,
    TkinterProgressView,
)


class ProgressWindow:
    """Same shape as every other migrated tool's ProgressWindow."""
    def __init__(self, root, title="Processing"):
        """
        Creates and immediately shows the progress dialog.

        Args:
            root: the parent Tk/Toplevel window.
            title (str): window title. Defaults to "Processing".
        """
        self.win = tk.Toplevel(root)
        apply_icon(self.win, "distancefactor.ico")
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
        self.win.after(100, lambda: self.win.attributes("-topmost", False))

        self._policy = ProgressPresentationPolicy()
        self._view = TkinterProgressView(self.win, self.status_var, self.progress)

    def switch_to_determinate(self, maximum):
        """Switches the progress bar to determinate mode with the given
        maximum, resetting its current value to 0."""
        self.progress.config(mode="determinate", maximum=maximum, value=0)

    def update(self, message, value=None, maximum=None):
        """Updates the progress display via the shared
        ProgressPresentationPolicy/TkinterProgressView (see class
        docstring)."""
        state = self._policy.compute(message, value, maximum)
        self._view.render(state)

    def close(self):
        """Closes the progress window."""
        self._view.destroy()


# ========================================
# LOCAL FILE WRITE HELPERS
# ========================================
def _write_gpkg(gdf, out_path):
    """Atomic write: temp file, verified readable, then os.replace()."""
    tmp_path = out_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    gdf.to_file(tmp_path, driver="GPKG")
    # Verify readability before committing.
    gpd.read_file(tmp_path, rows=1)
    if os.path.exists(out_path):
        os.remove(out_path)
    os.replace(tmp_path, out_path)


def resolve_output_base_name(out_dir, desired_base_name):
    """Finds the next available '<name>_<n>' base name in out_dir."""
    n = 1
    candidate = f"{desired_base_name}_{n}"
    while os.path.exists(os.path.join(out_dir, f"{candidate}.gpkg")):
        n += 1
        candidate = f"{desired_base_name}_{n}"
    return candidate


def with_vm_suffix(main_base_name: str) -> str:
    """Derives the VM output's base name from the already-finalized
    main output base name. Ported/renamed from road_width.py's
    with_qa_suffix() -- same pairing guarantee."""
    return f"{main_base_name}_VM"


# ========================================
# DIALOGS
# ========================================
def ask_overwrite_dialog(parent, conflicting_names):
    """
    Combined dialog shown ONCE, before any processing starts, when the
    Land Parcel source's desired local output filename already exists
    in the chosen output folder. Ported to match road_frontage.py's/
    road_width.py's own ask_overwrite_dialog() exactly -- a custom
    Toplevel (not messagebox), screen-centered, persistently
    topmost, with the tool's own name as the title bar text -- rather
    than the plain OS-styled messagebox this file previously used,
    which looked visibly inconsistent against every other CAMA Tools
    dialog (confirmed by direct screenshot comparison).

    Returns "overwrite", "new", or "cancel" (also returned if the
    dialog's own titlebar close button is used).
    """
    result = {"choice": "cancel"}

    dialog = tk.Toplevel(parent)
    apply_icon(dialog, "distancefactor.ico")
    dialog.title("INFLUENCE MAP DISTANCE TO LAND PARCEL TOOL")
    dialog.resizable(False, False)
    dialog.grab_set()

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
    ), anchor="w", justify="left", wraplength=420
             ).pack(fill="x", padx=16, pady=(4, 16))

    dialog.update_idletasks()
    req_w = max(dialog.winfo_reqwidth(), 460)
    req_h = dialog.winfo_reqheight()
    # Centered on the SCREEN, not on `parent` -- matches
    # road_frontage.py's own rationale exactly: `parent` here can be
    # a deliberately invisible off-screen anchor window, so screen
    # dimensions are the only always-meaningful reference to center
    # against.
    screen_w = dialog.winfo_screenwidth()
    screen_h = dialog.winfo_screenheight()
    x = (screen_w - req_w) // 2
    y = (screen_h - req_h) // 2
    dialog.geometry(f"{req_w}x{req_h}+{max(x,0)}+{max(y,0)}")

    dialog.deiconify()
    dialog.lift()
    dialog.focus_force()
    dialog.attributes("-topmost", True)

    # Persistent re-assertion for the dialog's whole lifetime -- the
    # CAMA Tools floating panel is itself persistently topmost (a
    # separate process), so a one-time lift() at creation isn't
    # enough. Self-cancels once dialog.destroy() runs in choose().
    def _keep_dialog_on_top():
        if dialog.winfo_exists():
            dialog.lift()
            dialog.attributes("-topmost", True)
            dialog.after(250, _keep_dialog_on_top)
    dialog.after(250, _keep_dialog_on_top)

    dialog.wait_window()
    return result["choice"]


def confirm_db_overwrite_dialog(parent, table_name):
    """
    Shown when find_matching_tables() returns EXACTLY ONE candidate for
    the DB-output destination table. Ported to match road_frontage.py's
    own confirm_db_overwrite_dialog() exactly -- previously this file
    used a plain messagebox.askyesno(), which shows the OS's generic
    dialog chrome (default icon, no tool-specific title bar) instead of
    this app's own custom-styled dialog convention.

    Returns True (Yes) or False (No / dialog closed).
    """
    result = {"confirmed": False}

    dialog = tk.Toplevel(parent)
    apply_icon(dialog, "distancefactor.ico")
    dialog.title("INFLUENCE MAP DISTANCE TO LAND PARCEL TOOL")
    dialog.resizable(False, False)
    dialog.grab_set()

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
    screen_w = dialog.winfo_screenwidth()
    screen_h = dialog.winfo_screenheight()
    x = (screen_w - req_w) // 2
    y = (screen_h - req_h) // 2
    dialog.geometry(f"{req_w}x{req_h}+{max(x,0)}+{max(y,0)}")

    dialog.deiconify()
    dialog.lift()
    dialog.focus_force()
    dialog.attributes("-topmost", True)

    def _keep_dialog_on_top():
        if dialog.winfo_exists():
            dialog.lift()
            dialog.attributes("-topmost", True)
            dialog.after(250, _keep_dialog_on_top)
    dialog.after(250, _keep_dialog_on_top)

    dialog.wait_window()
    return result["confirmed"]


def choose_db_overwrite_dialog(parent, candidates):
    """
    Shown when find_matching_tables() returns MORE THAN ONE candidate.
    Ported to match road_frontage.py's own choose_db_overwrite_dialog()
    exactly -- radio-button selection (first candidate pre-selected)
    inside the same custom-styled Toplevel convention, instead of this
    file's previous ad-hoc Listbox-based version.

    Returns the chosen table name, or None if cancelled.
    """
    result = {"chosen": None}
    selected = tk.StringVar(value=candidates[0])

    dialog = tk.Toplevel(parent)
    apply_icon(dialog, "distancefactor.ico")
    dialog.title("INFLUENCE MAP DISTANCE TO LAND PARCEL TOOL")
    dialog.resizable(False, False)
    dialog.grab_set()

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
    screen_w = dialog.winfo_screenwidth()
    screen_h = dialog.winfo_screenheight()
    x = (screen_w - req_w) // 2
    y = (screen_h - req_h) // 2
    dialog.geometry(f"{req_w}x{req_h}+{max(x,0)}+{max(y,0)}")

    dialog.deiconify()
    dialog.lift()
    dialog.focus_force()
    dialog.attributes("-topmost", True)

    def _keep_dialog_on_top():
        if dialog.winfo_exists():
            dialog.lift()
            dialog.attributes("-topmost", True)
            dialog.after(250, _keep_dialog_on_top)
    dialog.after(250, _keep_dialog_on_top)

    dialog.wait_window()
    return result["chosen"]


def show_success_dialog(root, total, failed_sources, single_success_detail):
    """
    Shows the final outcome dialog after all Land Parcel sources have
    been processed: a warning listing per-source failures if any
    occurred, otherwise a plain success message (using
    single_success_detail's more specific text when exactly one source
    was processed, or a generic "all N processed" message otherwise).

    Args:
        root: unused directly (messagebox calls use no explicit parent
        here); kept for signature consistency with this file's other
        dialog functions.
        total (int): total number of sources attempted.
        failed_sources (list[tuple[str, str]]): (source_label, reason)
        pairs for any source that raised during processing.
        single_success_detail (str | None): a more specific success
        message to show when total == 1 and it succeeded; ignored
        otherwise.
    """
    if failed_sources:
        lines = "\n".join(f"  • {name}: {reason}" for name, reason in failed_sources)
        messagebox.showwarning(
            "Completed With Errors",
            f"Processed {total} source(s).\n\n"
            f"{len(failed_sources)} source(s) failed:\n{lines}",
        )
    elif single_success_detail:
        messagebox.showinfo("Success", single_success_detail)
    else:
        messagebox.showinfo("Success", f"All {total} source(s) processed successfully.")


# ========================================
# GLOBAL MAPPER
# ========================================
def load_in_global_mapper(path):
    """Best-effort launch. Never blocks or raises -- a failure here
    must never be treated as a processing failure.

    Notes:
        GM_EXE_PATH is currently a hardcoded absolute path (see
        CONFIGURATION section above and the module docstring's SIDE
        EFFECTS note) -- dynamic executable discovery is a planned,
        separately-scoped future improvement, not implemented here.
        Unlike other tool files' load_in_global_mapper(), this version
        does not attempt to find/focus an already-open Global Mapper
        window first -- it always launches a new subprocess.
    """
    try:
        if os.path.exists(GM_EXE_PATH):
            subprocess.Popen([GM_EXE_PATH, path], shell=False)
        else:
            print(f"ℹ️ Global Mapper not found at {GM_EXE_PATH}; skipping auto-open for {path}")
    except Exception as e:
        print(f"⚠️ Could not open '{path}' in Global Mapper: {e}")


def _translate_exception(e, source_label):
    """Formats an exception as "ExceptionType: message" for display in
    the failed-sources list (source_label is accepted for signature
    consistency but not currently used in the formatted text)."""
    return f"{type(e).__name__}: {e}"


# ========================================
# SINGLE-SOURCE PROCESSING
# ========================================
def _process_one_source(
    source_id, is_db_source, fault_gdf, name_field, engine, schema,
    output_mode, overwrite_mode, output_column_overrides,
    progress_cb, status_cb, on_total=None,
    resolved_table_name=None, resolved_outcome=None,
):
    """
    Fully processes ONE Land Parcel source: load, measure (single
    STRtree nearest-feature lookup per parcel against the ALREADY-
    LOADED fault_gdf -- loaded once for the whole batch by the caller,
    not reloaded per source), write main output + VM output. Mirrors
    road_width.py's _process_one_source() atomicity guarantees:
      - Local output: _write_gpkg() is atomic.
      - Database output: main table + CAMA_Table share one transaction.
      - VM output: best-effort, own try/except, never blocks the main
        output on failure.
    """
    if is_db_source:
        parcel_gdf = read_postgis_clean(source_id, engine, schema)
        source_label = source_id
    else:
        parcel_gdf = gpd.read_file(source_id)
        source_label = os.path.basename(source_id)

    if len(parcel_gdf) == 0:
        raise ValueError(f"No parcels found in {source_label}")

    if on_total:
        on_total(len(parcel_gdf))

    output_column_map = output_column_overrides.get(source_id, {})

    original_crs = parcel_gdf.crs
    zone_epsg = detect_prs92_zone([("Land Parcel", parcel_gdf), ("Fault Line Map", fault_gdf)])
    print(f"🌍 [{source_label}] Reprojecting to EPSG:{zone_epsg}...")
    parcel_gdf_proj = parcel_gdf.to_crs(epsg=zone_epsg)
    fault_gdf_proj = fault_gdf.to_crs(epsg=zone_epsg)

    status_cb(f"Measuring fault line distance: {source_label}...")
    parcel_gdf_proj, vm_gdf = process_parcels(
        parcel_gdf_proj, fault_gdf_proj, name_field, source_label,
        progress_cb=progress_cb, output_column_map=output_column_map,
    )

    # Reproject BOTH outputs back to the parcel's original CRS before
    # saving -- applies to the main output and the VM output equally.
    # vm_gdf always carries a defined CRS from process_parcels() (set at
    # construction time in both the empty and non-empty branches), so
    # .to_crs() is safe here without special-casing the empty case.
    if original_crs is not None:
        parcel_gdf_out = parcel_gdf_proj.to_crs(original_crs)
        vm_gdf_out = vm_gdf.to_crs(original_crs)
    else:
        parcel_gdf_out = parcel_gdf_proj.to_crs(epsg=4326)
        vm_gdf_out = vm_gdf.to_crs(epsg=4326)

    # Deliberately NOT applying any geometry repair (e.g. buffer(0))
    # to parcel_gdf_out here. This tool only MEASURES -- it must never
    # alter a parcel's digitized shape, even if that shape happens to
    # be technically invalid. This matches the explicitly documented
    # convention in road_width.py ("safe, LOCAL-SCOPE geometry repair
    # ... that never touches [output]"), land_shape_compactness.py
    # ("the exported output keeps each parcel's original, untouched
    # shape, even if invalid"), and lot_location.py ("never mutating
    # brgy_gdf's geometry column, so the exported output stays
    # faithful to the original source shapes"). Any geometry repair
    # needed for the MEASUREMENT itself already happened earlier,
    # strictly local-scope, inside process_parcels() -- see that
    # function's own per-parcel loop (poly = fix_geometry(poly), never
    # written back into parcel_gdf's geometry column).
    #
    # NOTE: influence_to_barangay.py and road_density.py do apply a
    # buffer(0)/fix_geometry pass directly onto their own saved output
    # geometry before writing -- confirmed, on inspection, to be an
    # inconsistency against the dominant convention above, not a
    # pattern to replicate here. Out of scope to correct in those
    # files as part of this task (Do-Not-Touch / Architecture
    # Preservation Rules) -- flagged separately as a follow-up task.

    if output_mode[0] == "local":
        desired_base_name = (
            source_id if is_db_source
            else os.path.splitext(os.path.basename(source_id))[0]
        )
        candidate_path = os.path.join(output_mode[1], f"{desired_base_name}.gpkg")
        had_conflict = os.path.exists(candidate_path)
        base_name = (
            resolve_output_base_name(output_mode[1], desired_base_name)
            if had_conflict and overwrite_mode == "new"
            else desired_base_name
        )
        outcome = "overwritten" if (had_conflict and overwrite_mode == "overwrite") else "created"
        out = os.path.join(output_mode[1], f"{base_name}.gpkg")

        status_cb(f"Writing output file: {source_label}...")
        _write_gpkg(parcel_gdf_out, out)

        vm_out = None
        # ------------------------------------------------------------------
        # Visual Measurement (VM) layer write -- DISABLED (commented out, not
        # removed). Per-task decision to suppress the secondary/diagnostic
        # output so a successful Run Processing always produces exactly ONE
        # main output file per parcel source. vm_gdf is still computed inside
        # process_parcels() (unchanged) since vm_line is a byproduct of the
        # same tree.nearest() lookup that produces the main CAMA_FAULT_NAME/
        # CAMA_FAULT_DISTANCE values -- only this write (and the DB-output
        # equivalent below, in the `else:` branch of this same function) is
        # disabled. vm_out stays initialized to None above, so the return
        # tuple's shape is unchanged and the existing `if vm_ref:` guard in
        # run_processing()'s worker() continues to work exactly as before,
        # just always taking the "no VM layer" path.
        # ------------------------------------------------------------------
        # if not vm_gdf_out.empty:
            # try:
                # status_cb("Writing Visual Measurement layer...")
                # vm_base_name = with_vm_suffix(base_name)
                # vm_path = os.path.join(output_mode[1], f"{vm_base_name}.gpkg")
                # _write_gpkg(vm_gdf_out, vm_path)
                # vm_out = vm_path
            # except Exception as e:
                # print(f"⚠️ Could not write Visual Measurement layer for '{source_label}': {type(e).__name__}: {e}")

        return source_label, out, vm_out, outcome

    else:
        if is_db_source:
            table = source_id
            outcome = "overwritten"
        else:
            if resolved_table_name is not None:
                table = resolved_table_name
                outcome = resolved_outcome
            else:
                table = os.path.splitext(os.path.basename(source_id))[0]
                outcome = "created"

        status_cb(
            "Updating database records..." if outcome == "overwritten"
            else "Creating new table in database..."
        )
        with engine.begin() as conn:
            parcel_gdf_out.to_postgis(table, conn, schema=schema, if_exists="replace", index=False)

            # ------------------------------------------------------------
            # CAMA_Table write -- TEMPORARILY DISABLED (commented out, not
            # removed). CAMA_Table itself is a real, established cross-
            # tool convention (also used by road_width.py and
            # influence_to_barangay.py) -- this is NOT the same situation
            # as CAMA_Transaction_Log, which road_width.py confirmed is
            # genuinely unused and removed outright. This block is kept
            # in place, disabled only, because:
            #   1. It is not a required deliverable for this tool yet.
            #   2. The row-by-row UPSERT loop below (one conn.execute()
            #      per parcel, via total_rows = len(parcel_gdf_out)) is
            #      the actual performance bottleneck on large Land Parcel
            #      sources (e.g. LandParcel.shp's 11,911 features) -- NOT
            #      the to_postgis() write above, which stays enabled.
            #      A better batched/bulk-UPSERT algorithm for this loop
            #      is still undecided; re-enable only once that's solved.
            # ------------------------------------------------------------
            # conn.execute(text(f"""
            #     CREATE TABLE IF NOT EXISTS "{schema}"."CAMA_Table" (
            #         id SERIAL PRIMARY KEY,
            #         PIN TEXT UNIQUE NOT NULL
            #     );
            # """))
            # for col in ("cama_fault_name", "cama_fault_distance"):
            #     col_type = "TEXT" if col == "cama_fault_name" else "NUMERIC"
            #     conn.execute(text(f"""
            #         DO $$
            #         BEGIN
            #             IF NOT EXISTS (
            #                 SELECT 1 FROM information_schema.columns
            #                 WHERE table_schema='{schema}'
            #                   AND table_name='CAMA_Table'
            #                   AND column_name='{col}'
            #             ) THEN
            #                 EXECUTE 'ALTER TABLE "{schema}"."CAMA_Table" ADD COLUMN "{col}" {col_type}';
            #             END IF;
            #         END $$;
            #     """))
            #
            # pin_field = next((c for c in parcel_gdf_out.columns if c.lower() == "pin"), None)
            # if pin_field:
            #     name_col = output_column_map.get("CAMA_FAULT_NAME", "CAMA_FAULT_NAME")
            #     dist_col = output_column_map.get("CAMA_FAULT_DISTANCE", "CAMA_FAULT_DISTANCE")
            #     sql = text(f"""
            #         INSERT INTO "{schema}"."CAMA_Table" (PIN, cama_fault_name, cama_fault_distance)
            #         VALUES (:pin, :fname, :fdist)
            #         ON CONFLICT (PIN) DO UPDATE
            #         SET cama_fault_name = EXCLUDED.cama_fault_name,
            #             cama_fault_distance = EXCLUDED.cama_fault_distance;
            #     """)
            #     total_rows = len(parcel_gdf_out)
            #     for row_i, (_, row) in enumerate(parcel_gdf_out.iterrows(), start=1):
            #         status_cb(f"Updating CAMA_Table: {row_i}/{total_rows}", row_i, total_rows)
            #         conn.execute(sql, {
            #             "pin": str(row[pin_field]),
            #             "fname": row.get(name_col),
            #             "fdist": float(row[dist_col]) if row.get(dist_col) is not None else None,
            #         })

        vm_table = None
        # ------------------------------------------------------------------
        # Visual Measurement (VM) layer write -- DISABLED (commented out, not
        # removed). Same per-task decision as the local-output equivalent
        # above (see that block's comment for full reasoning): a successful
        # Run Processing should always produce exactly ONE main output per
        # parcel source. vm_gdf itself is still computed inside
        # process_parcels() (unchanged) -- only this write is disabled.
        # vm_table stays initialized to None above, so the return tuple's
        # shape is unchanged and the existing `if vm_ref:` guard in
        # run_processing()'s worker() continues to work exactly as before,
        # just always taking the "no VM layer" path. This block was, and
        # remains, deliberately AFTER the `with engine.begin() as conn:`
        # transaction above (best-effort, own separate write) -- untouched
        # by this change; nothing about the CAMA_Table block immediately
        # above this one is modified.
        # ------------------------------------------------------------------
        # if not vm_gdf_out.empty:
            # try:
                # status_cb("Writing Visual Measurement layer...")
                # vm_table = f"{table}_VM"
                # vm_gdf_out.to_postgis(vm_table, engine, schema=schema, if_exists="replace", index=False)
            # except Exception as e:
                # print(f"⚠️ Could not write Visual Measurement layer to DB for '{source_label}': {type(e).__name__}: {e}")

        return source_label, table, vm_table, outcome


# ========================================
# RUN PROCESSING
# ========================================
def run_processing(app_root, overwrite_mode=None, per_source_resolution=None):
    """
    Orchestrates the full run on a background thread (worker(), defined
    below): loads DB credentials unconditionally (even for an all-local
    run -- see Args), loads the Fault Line Map once, then for each
    selected Land Parcel source runs process_parcels() (via
    _process_one_source()) and saves the result either locally (.gpkg,
    optionally opened in Global Mapper) or to PostGIS.

    Args:
        app_root: parent Tk window for the ProgressWindow and any
        dialogs.
        overwrite_mode (str | None): "overwrite" or "new", from
        ask_overwrite_dialog() in on_run() -- only relevant for local
        output mode.
        per_source_resolution: {source_id: (resolved_table_name, resolved_outcome)}
        -- resolved once, up front, per Land Parcel source, on the main
        thread, BEFORE win.destroy() -- see on_run()'s PRIORITY 3.

    Notes:
        DB credentials are loaded and an engine created unconditionally
        at the top of this function, even when output_mode is "local"
        -- this appears to be because read_postgis_clean() and a
        DB-sourced parcel/fault read both need a live engine regardless
        of where output ultimately goes. If credentials are missing,
        this function returns early (showing whatever error
        load_db_credentials() itself raises) even for an otherwise
        all-local run.
    """
    global parcel_source, fault_source, output_mode, parcel_output_column_overrides

    if not parcel_source or not parcel_source[1]:
        messagebox.showerror("Error", "Land Parcel source not selected properly.")
        return
    if not fault_source:
        messagebox.showerror("Error", "Fault Line Map source not selected properly.")
        return
    if not output_mode:
        messagebox.showerror("Error", "Output destination not selected.")
        return

    creds = load_db_credentials()
    if not creds:
        return
    schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    )

    progress = ProgressWindow(app_root, title="Influence Map Distance to Land Parcel — Processing")

    q = queue.Queue()

    def worker():
        """
        Background-thread body: loads the Fault Line Map once, then for
        each selected Land Parcel source calls _process_one_source()
        (which itself calls process_parcels() and writes output),
        posting progress/status/completion/error events onto q for the
        main thread's queue-polling loop to consume. Never touches
        Tkinter widgets directly.
        """
        try:
            q.put(("update", "Loading Fault Line Map...", None, None))
            try:
                fault_gdf = read_fault_line_source(fault_source, engine, schema)
                fault_gdf = ensure_geometry_column(fault_gdf)
            except Exception as e:
                q.put(("fatal_error", f"Could not load Fault Line Map: {e}", None, None))
                return

            if len(fault_gdf) == 0:
                q.put(("fatal_error", "The selected Fault Line Map has no features.", None, None))
                return

            fault_name_guess = (
                fault_source[2] if fault_source[0] == "local" and fault_source[2]
                else (os.path.splitext(os.path.basename(fault_source[1]))[0]
                      if fault_source[0] == "local" else fault_source[1])
            )
            name_field = detect_fault_name_field(fault_gdf, fault_name_guess)
            if name_field is None:
                q.put(("fatal_error",
                       "Could not determine which column in the Fault Line Map "
                       "holds the fault name. Expected a column named 'name', "
                       "'fault_name', 'fault', or 'feature_name'.", None, None))
                return

            sources = (
                [(p, False) for p in parcel_source[1]]
                if parcel_source[0] == "local"
                else [(t, True) for t in parcel_source[1]]
            )

            failed_sources = []
            success_count = 0
            single_success_detail = None
            current_step = [0]
            current_total = [None]

            def progress_cb(_):
                current_step[0] += 1
                q.put(("update", f"Measuring fault line distance... "
                                  f"Parcel {current_step[0]}"
                                  + (f" / {current_total[0]}" if current_total[0] else ""),
                       current_step[0], current_total[0]))

            def status_cb(message, value=None, total=None):
                q.put(("update", message, value if value is not None else current_step[0],
                       total if total is not None else current_total[0]))

            def on_total(n):
                current_step[0] = 0
                current_total[0] = n
                q.put(("found_total", f"Found {n} parcel(s).", n, None))

            for source_id, is_db_source in sources:
                source_label = source_id if is_db_source else os.path.basename(source_id)
                q.put(("update", f"Loading parcel source: {source_label}...", None, None))
                try:
                    resolved_table_name, resolved_outcome = (None, None)
                    if per_source_resolution and source_id in per_source_resolution:
                        resolved_table_name, resolved_outcome = per_source_resolution[source_id]

                    label, out_ref, vm_ref, outcome = _process_one_source(
                        source_id, is_db_source, fault_gdf, name_field, engine, schema,
                        output_mode, overwrite_mode, parcel_output_column_overrides,
                        progress_cb, status_cb, on_total=on_total,
                        resolved_table_name=resolved_table_name,
                        resolved_outcome=resolved_outcome,
                    )
                    success_count += 1

                    if len(sources) == 1:
                        display_name = os.path.basename(out_ref) if output_mode[0] == "local" else out_ref
                        single_success_detail = f"'{display_name}' {outcome} successfully."

                    if output_mode[0] == "local":
                        q.put(("open_gm", out_ref, None, None))
                        if vm_ref:
                            q.put(("open_gm", vm_ref, None, None))

                except Exception as e:
                    reason = _translate_exception(e, source_label)
                    failed_sources.append((source_label, reason))
                    print(f"⚠️ Skipped '{source_label}': {type(e).__name__}: {e}")

            q.put(("done", success_count + len(failed_sources), failed_sources, single_success_detail))

        except Exception as e:
            q.put(("fatal_error", str(e), None, None))
        finally:
            try:
                engine.dispose()
            except Exception as e:
                print(f"⚠️ Could not cleanly dispose of the database engine: {e}")

    def poll_queue():
        """
        Main-thread poller (scheduled via app_root.after(100, ...)):
        drains q and updates the progress dialog, opens the result in
        Global Mapper, or shows the final success/error dialog and
        stops polling, depending on the event kind. All Tkinter calls
        happen here, never inside worker() itself.
        """
        try:
            while True:
                msg = q.get_nowait()
                kind = msg[0]
                if kind == "update":
                    progress.update(msg[1], msg[2], msg[3])
                elif kind == "found_total":
                    progress.switch_to_determinate(msg[2])
                    progress.update(msg[1], 0, msg[2])
                elif kind == "open_gm":
                    load_in_global_mapper(msg[1])
                elif kind == "done":
                    progress.close()
                    show_success_dialog(app_root, msg[1], msg[2], msg[3])
                    return
                elif kind == "fatal_error":
                    progress.close()
                    messagebox.showerror("Error", f"Could not complete processing: {msg[1]}")
                    return
        except queue.Empty:
            pass
        except tk.TclError as e:
            print(f"⚠️ poll_queue() stopped early (widget no longer exists): {e}")
            return
        app_root.after(100, poll_queue)

    threading.Thread(target=worker, daemon=True).start()
    poll_queue()


# ========================================
# MAIN WINDOW
# ========================================
def _pick_db_tables(parent, tables, multi, on_select):
    """
    Modal single/multi table picker, ported verbatim (structure and
    behavior) from influence_to_barangay.py's own _pick_db_tables().
    Used so DB-mode selection stays a compact single action_row in the
    main configuration window (label + Browse button), exactly like
    Local-mode -- never an inline Listbox embedded in the main window,
    which is what previously made this tool's window visibly wider/
    taller than influence_to_barangay.py's "Influence to Parcel Tool".
    """
    picker = tk.Toplevel(parent)
    apply_icon(picker, "distancefactor.ico")
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


def open_main_window(root):
    """
    Builds and shows the tool's single unified configuration window:
    Land Parcel and Fault Line Map source pickers (each with a
    Local-file/Database-table radio toggle), an Output destination
    picker, and a Run button gated by _update_run_button_state().

    The Land Parcel picker additionally runs a background,
    detect-on-select check (_refresh_parcel_output_check(), via a
    daemon thread + win.after()-polled queue.Queue) for existing
    OUTPUT_COLUMN_TARGETS columns, the moment a file/table is selected
    or the Local/Database toggle changes -- not only when Run is
    clicked.

    Args:
        root: the parent Tk root this window is opened under.
    """
    from tkinter import ttk

    win = tk.Toplevel(root)
    apply_icon(win, "distancefactor.ico")
    win.title("Influence Map Distance to Land Parcel")
    win.resizable(False, False)
    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))

    # ── state ────────────────────────────────────────────────────
    parcel_source_type = tk.StringVar(master=win, value="local")
    fault_source_type = tk.StringVar(master=win, value="local")
    output_dest_type = tk.StringVar(master=win, value="local")

    # Single-selection architecture for BOTH Land Parcel and Fault Line
    # Map (Fault Line Map is single-select by explicit task requirement;
    # Land Parcel is single-select matching influence_to_barangay.py's
    # OWN actual picker -- see the note on parcel_local_path below).
    # Authority variables -- all GUI labels and Run-button state are
    # derived from them, never the reverse. Confirmed by direct
    # screenshot comparison against the running "Influence to Parcel
    # Tool": its Land Parcel Source section shows a single "Browse..."
    # button and "No file selected" (singular); only ITS SEPARATE
    # "Influence Map Source" section is genuinely multi-select. The
    # internal parcel_source tuple still wraps the single selection
    # into a 1-item list in on_run() below, purely so the existing
    # loop-based per-source processing code needs no restructuring --
    # same technique influence_to_barangay.py itself uses:
    # barangay_source = ("local", (parcel_local_path,)).
    parcel_local_path = None    # authority: single local file path
    parcel_db_table = None      # authority: single DB table name
    fault_local_path = None     # authority: single local file path
    fault_local_layer = None    # authority: layer name, local .gpkg only
    fault_db_table = None       # authority: single DB table name
    output_local_dir = tk.StringVar(master=win)

    # Land Parcel existing-output-column check: detect-on-select,
    # matching the pattern established in lot_location.py/road_width.py/
    # road_frontage.py/road_density.py/road_surface.py. Deliberately does
    # NOT cache the result across calls -- every selection AND every
    # Local/Database toggle triggers a fresh read -- never a cached
    # result. What IS still remembered per
    # mode is only WHICH file/table is selected (parcel_local_path /
    # parcel_db_table above), a separate concern. Multi-target (2
    # targets, OUTPUT_COLUMN_TARGETS): each conflict entry is
    # (path_or_table, {target: existing_col_name}), a dict, not a single
    # column name -- see _detect_existing_output_columns()'s docstring.
    parcel_is_reading = False
    parcel_existing_output_conflicts = []   # [(path_or_table, {target: col}), ...]

    # run_status_var: drives the always-visible status label under the
    # Run button, same as influence_to_barangay.py's own pattern.
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

    parcel_radio_row = tk.Frame(parcel_frame)
    parcel_radio_row.pack(fill="x")
    parcel_radio_local = tk.Radiobutton(parcel_radio_row, text="Local File",
                   variable=parcel_source_type, value="local",
                   command=lambda: _toggle_parcel())
    parcel_radio_local.pack(side="left")
    parcel_radio_db = tk.Radiobutton(parcel_radio_row, text="Database Table",
                   variable=parcel_source_type, value="db",
                   command=lambda: _toggle_parcel())
    parcel_radio_db.pack(side="left", padx=(12, 0))

    parcel_files_var = tk.StringVar(master=win, value="No file selected")
    parcel_db_label = tk.StringVar(master=win, value="No table selected")

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
        road_width.py/road_frontage.py/road_density.py/road_surface.py.
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
        _check_parcel_output_conflicts()'s docstring on why this is
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
            # _check_parcel_output_conflicts()'s docstring) -- distinct
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
        _check_parcel_output_conflicts() (defined above, now
        self-contained) as the actual worker logic, just now called on a
        background thread. Gives up after 60 seconds with no result
        (see _poll_parcel_output_queue()) -- a hung read must not leave
        the tool waiting indefinitely.

        Deliberately does NOT cache the result across calls -- every
        call, whether triggered by a fresh Browse/Select or by toggling
        Local <-> Database, always performs a real read, never a
        cached result. What
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
            conflicts = _check_parcel_output_conflicts(sources, source_type)
            result_queue.put(conflicts)

        deadline = time.time() + 60  # see _poll_parcel_output_queue()
        _set_parcel_reading_state(True)
        threading.Thread(target=worker, daemon=True).start()
        win.after(100, lambda: _poll_parcel_output_queue(
            result_queue, source_type, deadline))

    def browse_parcel_files():
        nonlocal parcel_local_path
        file = filedialog.askopenfilename(
            title="Select Land Parcel file",
            filetypes=VECTOR_FILETYPES)
        # Cancel returns "" -- do not assign, preserving previous selection.
        if file:
            parcel_local_path = file
            parcel_files_var.set(os.path.basename(file))
            # Always checks fresh -- see _refresh_parcel_output_check()
            # docstring: no result is ever cached across calls.
            _refresh_parcel_output_check()
        _update_run_button_state()

    def _on_parcel_db_selected(sel):
        nonlocal parcel_db_table
        parcel_db_table = sel[0]
        parcel_db_label.set(sel[0])
        _refresh_parcel_output_check()
        _update_run_button_state()

    def browse_parcel_db():
        creds = load_db_credentials()
        if not creds:
            return
        tables = fetch_tables(creds["schema"])
        if not tables:
            messagebox.showwarning("No Tables", "No tables found in the database schema.")
            return
        _pick_db_tables(win, tables, multi=False, on_select=_on_parcel_db_selected)

    def _toggle_parcel():
        # Always render from authority variables -- never from StringVar state.
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
        # active -- no cached result is ever restored.
        _refresh_parcel_output_check()
        _update_run_button_state()

    # ── SECTION 2: FAULT LINE MAP ────────────────────────────────
    section_label(win, "Fault Line Map")

    fault_frame = tk.Frame(win)
    fault_frame.pack(fill="x", padx=18, pady=2)

    fault_radio_row = tk.Frame(fault_frame)
    fault_radio_row.pack(fill="x")
    tk.Radiobutton(fault_radio_row, text="Local File",
                   variable=fault_source_type, value="local",
                   command=lambda: _toggle_fault()).pack(side="left")
    tk.Radiobutton(fault_radio_row, text="Database Table",
                   variable=fault_source_type, value="db",
                   command=lambda: _toggle_fault()).pack(side="left", padx=(12, 0))

    fault_files_var = tk.StringVar(master=win, value="No file selected")
    fault_db_label = tk.StringVar(master=win, value="No table selected")

    fault_action_row = tk.Frame(fault_frame)
    fault_action_row.pack(fill="x", pady=2)

    fault_lbl = tk.Label(fault_action_row, textvariable=fault_files_var,
                         fg="gray", anchor="w", width=42)
    fault_lbl.pack(side="left")

    fault_btn = tk.Button(fault_action_row, text="Browse…", width=10)
    fault_btn.pack(side="left", **PAD)

    def browse_fault_file():
        nonlocal fault_local_path, fault_local_layer
        file = filedialog.askopenfilename(
            title="Select Fault Line Map file",
            filetypes=VECTOR_FILETYPES)
        if not file:
            return
        try:
            layer = resolve_local_fault_layer(win, file)
        except Exception as e:
            messagebox.showerror("Error", f"Could not read Fault Line Map: {e}", parent=win)
            return
        ext = os.path.splitext(file)[1].lower()
        if ext == ".gpkg" and layer is None:
            # User cancelled the layer-selection dialog -- treat the
            # whole file selection as cancelled, never silently fall
            # back to an arbitrary layer.
            return
        fault_local_path = file
        fault_local_layer = layer
        label = os.path.basename(file) + (f"  (layer: {layer})" if layer else "")
        fault_files_var.set(label)
        _update_run_button_state()

    def _on_fault_db_selected(sel):
        nonlocal fault_db_table
        fault_db_table = sel[0]
        fault_db_label.set(sel[0])
        _update_run_button_state()

    def browse_fault_db():
        creds = load_db_credentials()
        if not creds:
            return
        tables = fetch_tables(creds["schema"])
        if not tables:
            messagebox.showwarning("No Tables", "No tables found in the database schema.")
            return
        _pick_db_tables(win, tables, multi=False, on_select=_on_fault_db_selected)

    def _toggle_fault():
        if fault_source_type.get() == "local":
            fault_lbl.config(textvariable=fault_files_var)
            fault_btn.config(text="Browse…", command=browse_fault_file)
            fault_files_var.set(
                (os.path.basename(fault_local_path) + (f"  (layer: {fault_local_layer})" if fault_local_layer else ""))
                if fault_local_path else "No file selected"
            )
        else:
            fault_lbl.config(textvariable=fault_db_label)
            fault_btn.config(text="Select…", command=browse_fault_db)
            fault_db_label.set(
                fault_db_table if fault_db_table
                else "No table selected"
            )
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
    output_db_var = tk.StringVar(master=win,
                                  value="Output will be written to the connected database.")

    out_action_row = tk.Frame(output_frame)
    out_action_row.pack(fill="x", pady=2)

    out_lbl = tk.Label(out_action_row, textvariable=output_dir_var,
                       fg="gray", anchor="w", width=42)
    out_lbl.pack(side="left")

    out_btn = tk.Button(out_action_row, text="Browse…", width=10)
    out_btn.pack(side="left", **PAD)

    def browse_output_dir():
        d = filedialog.askdirectory(title="Select Output Folder")
        if d:
            output_local_dir.set(d)
            output_dir_var.set(d)
            _update_run_button_state()

    def _toggle_output():
        if output_dest_type.get() == "local":
            out_lbl.config(textvariable=output_dir_var, fg="gray")
            out_btn.config(text="Browse…", command=browse_output_dir)
            out_btn.pack(side="left", **PAD)
        else:
            out_lbl.config(textvariable=output_db_var, fg="gray")
            out_btn.pack_forget()
        _update_run_button_state()

    # ---------------- Run ----------------
    def on_run():
        """
        Run button handler: validates Land Parcel + Fault Line Map +
        Output selections are present, consults the already-known
        background column-conflict result (PRIORITY 1 -- see
        _refresh_parcel_output_check()), runs the local output-file
        conflict check (PRIORITY 2), and per-source DB-output table
        resolution (PRIORITY 3) -- each able to cancel the whole run --
        then destroys this window and hands off to run_processing().
        Sets the module-level parcel_source, fault_source, output_mode,
        and parcel_output_column_overrides globals on success.
        """
        nonlocal parcel_local_path, parcel_db_table, fault_local_path, fault_local_layer, fault_db_table
        global parcel_source, fault_source, output_mode, parcel_output_column_overrides

        if parcel_source_type.get() == "local":
            if not parcel_local_path:
                messagebox.showerror("Error", "Please select a Land Parcel file.", parent=win)
                return
            parcel_source = ("local", [parcel_local_path])
        else:
            if not parcel_db_table:
                messagebox.showerror("Error", "Please select a Land Parcel table.", parent=win)
                return
            parcel_source = ("db", [parcel_db_table])

        if fault_source_type.get() == "local":
            if not fault_local_path:
                messagebox.showerror("Error", "Please select a Fault Line Map file.", parent=win)
                return
            fault_source = ("local", fault_local_path, fault_local_layer)
        else:
            if not fault_db_table:
                messagebox.showerror("Error", "Please select a Fault Line Map table.", parent=win)
                return
            fault_source = ("db", fault_db_table)

        output_mode = (
            ("local", output_local_dir.get()) if output_dest_type.get() == "local"
            else ("db", None)
        )

        if output_mode[0] == "local" and not output_mode[1]:
            messagebox.showerror("Error", "Please select an output folder.", parent=win)
            return

        # PRIORITY 1: column conflict check -- warn if the selected Land
        # Parcel source already has any of OUTPUT_COLUMN_TARGETS. Shown
        # before the file-conflict dialog so the user can decide whether
        # to proceed at all before being asked about filename conflicts.
        # Declining cancels the run entirely; main window stays open.
        #
        # Phase A (Group 5 detect-on-select generalization): this no
        # longer calls _check_parcel_output_conflicts() synchronously
        # here (and no longer needs to pre-load DB credentials/engine
        # for that call, either -- the check function is now
        # self-contained) -- the check already ran in the background the
        # moment the Land Parcel source was selected/toggled (see
        # _refresh_parcel_output_check()). This just consults the
        # already-known result, parcel_existing_output_conflicts.
        # _update_run_button_state() already guarantees Run cannot be
        # reached while parcel_is_reading is True, so this value is
        # guaranteed current for the actively selected source at this
        # point.
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
                "Proceed?",
                parent=win,
            )
            if not proceed:
                print("Run cancelled by user (existing output column(s) found).")
                return
            parcel_output_column_overrides = dict(conflicts)
        else:
            parcel_output_column_overrides = {}

        # ---------------- PRIORITY 2: local output-file overwrite ----------------
        overwrite_mode = None
        if output_mode[0] == "local":
            desired_names = (
                [os.path.splitext(os.path.basename(p))[0] for p in parcel_source[1]]
                if parcel_source[0] == "local" else list(parcel_source[1])
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

        # ---------------- PRIORITY 3: DB output-table resolution ----------------
        per_source_resolution = {}
        if output_mode[0] == "db":
            _resolve_creds = load_db_credentials()
            if not _resolve_creds:
                return
            _resolve_schema = _resolve_creds["schema"]
            for source_id in parcel_source[1]:
                desired_name = (
                    os.path.splitext(os.path.basename(source_id))[0]
                    if parcel_source[0] == "local" else source_id
                )
                table_name, outcome = resolve_db_output_table(win, _resolve_schema, parcel_source, desired_name)
                if table_name is None:
                    print("Run cancelled by user (database output table not confirmed).")
                    return
                per_source_resolution[source_id] = (table_name, outcome)

        win.destroy()
        run_processing(root, overwrite_mode, per_source_resolution)

    # Single source of truth for the Run button's enabled/disabled
    # colors -- same convention as influence_to_barangay.py.
    RUN_BTN_BG_ENABLED = "#2e7d32"
    RUN_BTN_FG_ENABLED = "white"
    RUN_BTN_BG_DISABLED = "#e0e0e0"
    RUN_BTN_FG_DISABLED = "#888888"

    def _update_run_button_state():
        has_parcel = bool(parcel_local_path) if parcel_source_type.get() == "local" else bool(parcel_db_table)
        has_fault = bool(fault_local_path) if fault_source_type.get() == "local" else bool(fault_db_table)
        has_output = bool(output_local_dir.get()) if output_dest_type.get() == "local" else True

        if parcel_is_reading:
            # Land Parcel existing-column check is still in flight --
            # never allow Run while its result is not yet known -- an
            # in-progress check must never be silently treated as
            # "no conflict".
            checking_name = (
                os.path.basename(parcel_local_path) if parcel_source_type.get() == "local"
                else parcel_db_table
            ) or "source"
            run_status_var.set(f'Checking "{checking_name}" columns…')
            ready = False
        elif not has_parcel:
            run_status_var.set("Please select a Land Parcel source.")
            ready = False
        elif not has_fault:
            run_status_var.set("Please select a Fault Line Map source.")
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
    _toggle_fault()
    _toggle_output()
    _update_run_button_state()


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
        apply_icon(root, "distancefactor.ico")
        root.withdraw()
        open_main_window(root)
        root.mainloop()


if __name__ == "__main__":
    main()