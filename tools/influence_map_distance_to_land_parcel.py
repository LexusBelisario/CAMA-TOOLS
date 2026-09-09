"""
tools/influence_map_distance_to_land_parcel.py

PURPOSE:
    CAMA Tools tool ("INFLUENCE MAP DISTANCE TO LAND PARCEL" in MAIN.py's dispatch table):
    for each Land Parcel, finds the single nearest feature in an
    Influence Map source (Point, LineString, or Polygon geometry all
    supported) and writes a dynamically-named distance column
    (CAMA_DISTANCE_TO_{layername}), plus one dynamically-named column
    per user-checked source column (CAMA_{columnname}_{layername}) --
    see _compute_output_column_targets() and process_parcels()'s own
    docstring for the full naming rule. The nearest feature is always
    selected by true geometric proximity (never a centroid/
    representative-point approximation); only the reported distance's
    measurement method varies by the winning feature's geometry type --
    see process_parcels()'s own docstring for the full
    Point/LineString-vs-Polygon distinction.

DISPATCH:
    Run as an isolated subprocess by MAIN.py via its `--tool` dispatch
    mechanism (see system context). Entry point is main(), triggered via
    the `if __name__ == "__main__":` guard at the bottom of this file.

INPUTS:
    Land Parcel source: one or more local vector files or PostGIS
    tables.
    Influence Map source: a single local vector file (with explicit
    layer disambiguation for an ambiguous multi-layer GeoPackage -- see
    resolve_local_fault_layer()) or a single PostGIS table. Its
    non-geometry columns are read in the background immediately after
    selection so the GUI can offer them as an optional "Copy Other
    Column(s)" checklist -- see _refresh_fault_columns() below.
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
    utils.progress_framework.

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
import math
import threading
import queue
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox, ttk
from tkinter import font as tkfont

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
from utils.gpkg_io import write_gpkg_atomic as _write_gpkg

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

# Dynamic output-column naming.
#
# This tool's output columns are no longer a fixed tuple -- see the
# Task Prompt's Exact Required Changes #2/#3 and the resolved Open
# Design Question (confirmed with the requester): the DEFAULT output
# is exactly one column, CAMA_DISTANCE_TO_{layername}, and each
# user-checked source column additionally produces
# CAMA_{columnname}_{layername}. {layername} is placed LAST in both
# patterns (a deliberate requester decision, to avoid any output-name
# collision with the sibling tool influence_map_to_land_parcel.py,
# whose own convention places the source name FIRST as
# CAMA_{layername}_{columnname}).
#
# Both {layername} and {columnname} go through the identical
# sanitization rule (see _sanitize_output_name_fragment() below) --
# the same rule, applied independently to each portion, mirroring
# influence_map_to_land_parcel.py's own _sanitize_influence_name_to_
# suffix() convention (two call sites, same function).
def _sanitize_output_name_fragment(raw_name: str) -> str:
    """
    Converts one raw name -- the selected Influence Map source's own
    display name, OR one of that source's column names -- into a valid
    CAMA_ column-name fragment: any run of non-alphanumeric characters
    becomes a single underscore, repeated underscores collapse to one,
    leading/trailing underscores are stripped, and the result is
    uppercased. Direct adaptation of influence_map_to_land_parcel.py's
    _sanitize_influence_name_to_suffix() -- same rule, ported here
    rather than imported, per this project's Rule of Three convention
    (see governing instructions Section G.5).

    The result is guaranteed non-empty: if sanitization leaves nothing
    (the raw value was purely punctuation/whitespace/symbols), the
    fixed placeholder "UNNAMED" is used instead -- same placeholder as
    influence_map_to_land_parcel.py's own fallback, reused deliberately
    (confirmed with the requester) since this tool now follows that
    file's naming convention directly.
    """
    suffix = re.sub(r"[^0-9A-Za-z]+", "_", raw_name)
    suffix = re.sub(r"_+", "_", suffix).strip("_")
    suffix = suffix.upper()
    if not suffix:
        suffix = "UNNAMED"
    return suffix


def _influence_source_display_name(source_type: str, path_or_table: str, layer=None) -> str:
    """
    Returns the raw (UN-sanitized) display name for the selected
    Influence Map source -- the {layername} portion of every output
    column name this tool produces. Direct adaptation of
    influence_map_to_land_parcel.py's _influence_source_display_stem()
    (same three cases, same rule):

      - Local file, single-layer (layer is None -- a .shp file, or any
        .gpkg with exactly one layer): the filename with its extension
        stripped.
      - Local file, multi-layer .gpkg (layer is a real layer-name
        string): the LAYER NAME itself, NOT the filename -- the layer
        is what actually identifies which data the checked columns
        came from.
      - Database table: the table name itself, unchanged.
    """
    if source_type == "local":
        if layer is not None:
            return layer
        return os.path.splitext(os.path.basename(path_or_table))[0]
    return path_or_table


def _compute_output_column_targets(source_display_name: str, checked_raw_columns):
    """
    Computes this run's full, ordered set of output column names:
    CAMA_DISTANCE_TO_{layername} ALWAYS first, followed by
    CAMA_{columnname}_{layername} for each entry in checked_raw_columns
    (already in checked/UI order -- this function does not reorder
    them). Column order in the output is a hard requirement (Task
    Prompt Section E) -- this function is the single place that
    ordering is decided, so every caller (the Run-time conflict check
    and process_parcels() by way of _process_one_source()) computes
    the same ordered target list from the same inputs, never cached
    across calls per this file's established "always fresh" convention.

    Args:
        source_display_name: the RAW (un-sanitized) Influence Map
        source display name, from _influence_source_display_name().
        checked_raw_columns: ordered iterable of raw (un-sanitized)
        column names actually checked in the GUI checklist (empty if
        the "Add Other Column(s)" master checkbox is unchecked, or if
        nothing is checked underneath it).

    Returns:
        tuple[str, ...] -- (dist_col, extra_col_1, extra_col_2, ...).
        Always at least length 1.
    """
    layer_suffix = _sanitize_output_name_fragment(source_display_name)
    dist_col = f"CAMA_DISTANCE_TO_{layer_suffix}"
    extra_cols = tuple(
        f"CAMA_{_sanitize_output_name_fragment(col)}_{layer_suffix}"
        for col in checked_raw_columns
    )
    return (dist_col, *extra_cols)


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
    dlg.title("Select Influence Map Layer")
    dlg.resizable(False, False)
    dlg.grab_set()
    dlg.attributes("-topmost", True)

    tk.Label(
        dlg,
        text=(
            f"'{os.path.basename(path)}' contains {len(layers)} layers.\n"
            "Select the ONE layer to use as the Influence Map source:"
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
# Dynamic target list -- targets is now computed fresh at Run time from
# _compute_output_column_targets() (the selected Influence Map source's
# display name + whatever columns are checked), matching
# influence_to_barangay.py's own dynamic-per-source-target-list
# pattern, NOT the fixed-tuple pattern this tool used before this task
# (see the deleted OUTPUT_COLUMN_TARGETS constant's old comment).
#
# Called synchronously from on_run() only (Run-time-only conflict
# detection, per confirmed design decision -- reading the Land Parcel
# source at selection time would be actively inaccurate here, since
# the target column set isn't even known until the Influence Map
# checklist has been resolved). No longer called from a background
# thread; the self-contained DB-credential loading below is kept
# anyway since on_run() itself has no already-open engine handy at the
# point this is called.
def _check_parcel_output_conflicts(sources, source_type, targets):
    """
    Checks each selected Land Parcel source for pre-existing columns
    matching `targets` (the dynamic, this-run's output column names --
    see _compute_output_column_targets()).

    Self-contained: loads its own DB credentials/engine internally for
    the "db" case.

    Returns a list of (path_or_table, {target: existing_col_name}) on a
    SUCCESSFUL read/check -- an empty list means the check succeeded and
    found no conflict. Returns None if credentials could not be loaded,
    or if ANY source failed to read -- this is a REQUIRED distinction,
    not cosmetic: an empty list means "verified, no conflict", while
    None means "could not verify at all".
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
        found = detect_existing_output_columns(gdf, targets)
        if found:
            conflicts.append((path_or_table, found))
    return conflicts


# parcel_output_column_overrides: {path_or_table: {final_column_name:
# actual_existing_column_name, ...}} -- populated at Run time when a
# Land Parcel source already has a pre-existing matching column and
# the user confirms proceeding. Keys are now this run's dynamically-
# computed column names (see _compute_output_column_targets()), not a
# fixed pair. Same convention as influence_to_barangay.py's own
# override map.
parcel_output_column_overrides = {}


# ========================================
# MEASUREMENT ENGINE
# ========================================
def process_parcels(parcel_gdf, fault_gdf, dist_col, extra_column_specs, source_name,
                     progress_cb=None, output_column_map=None):
    """
    Core measurement engine. For each parcel: representative_point() ->
    single nearest-feature STRtree lookup against the Influence Map
    layer -> the distance column, every checked extra column, and the
    VM line all derived from that SAME lookup result (single-nearest-
    feature-query rule -- see Task Prompt / Instructions Section E;
    this is a hard architectural requirement, not a style preference).

    Selection metric vs. measurement metric (approved design):
    The nearest feature is ALWAYS selected by true geometric proximity
    -- shapely's STRtree.nearest() already ranks candidates by real
    .distance() (point-to-point, point-to-line, or point-to-polygon,
    whichever applies), never by a centroid/representative-point
    approximation, and this holds even when Point, LineString, and
    Polygon features are mixed within the same Influence Map layer.
    This selection step is NEVER changed by geometry type -- "nearest
    feature" means the same thing regardless of what wins. UNCHANGED
    by this task.

    Only AFTER a winner is chosen does geometry type affect anything,
    and only the REPORTED distance value / VM line, never which
    feature won -- UNCHANGED by this task:
      - Point / LineString (and Multi- variants): the distance column
        is the true geometric distance from parcel_point to the
        feature, and the VM line runs to the exact nearest_points()
        endpoint on that feature -- this is this tool's original,
        LineString-validated behavior.
      - Polygon / MultiPolygon: the distance column is instead the
        distance from parcel_point to the winning feature's OWN
        representative_point() (center-to-center), and the VM line
        runs to that same representative_point() -- NOT standard
        boundary distance (which would read 0 whenever the parcel
        falls inside a fault/influence zone polygon, an unhelpful
        value for a hazard-distance metric).
    See the geometry-type switch in the per-parcel loop below for the
    exact implementation.

    Duplicate feature attribute values are acceptable and require no
    special handling: the nearest feature is selected purely by
    spatial proximity, never by uniqueness of any attribute, and every
    checked column's value is copied verbatim from the SAME winning
    feature.

    Args:
        dist_col: this run's final distance column name (already
        resolved via output_column_map, if any override applies -- see
        below).
        extra_column_specs: ordered list of (raw_column_name,
        final_column_name) tuples, one per checked checklist column,
        in checked/UI order -- this exact order is also the output
        column order (Task Prompt Section E hard requirement). Empty
        list when the "Add Other Column(s)" master checkbox is
        unchecked or nothing underneath it is checked -- in that case
        the distance column is the ONLY output column produced.

    Returns (parcel_gdf, vm_gdf) -- both still in the CALLER's current
    (projected) CRS; CRS restoration to the original CRS is the
    caller's responsibility (mirrors road_width.py's process()).
    """
    output_column_map = output_column_map or {}
    dist_col = output_column_map.get(dist_col, dist_col)
    # Resolve each extra column's OWN possible override independently --
    # a Land Parcel source may already have some, but not all, of this
    # run's checked-column output names as pre-existing columns.
    resolved_extra_specs = [
        (raw_col, output_column_map.get(final_col, final_col))
        for raw_col, final_col in extra_column_specs
    ]

    id_col = next(
        (c for c in parcel_gdf.columns
         if c.upper() in ("PIN", "ARP_NO", "TD_NO", "PARCEL_ID")),
        None
    )

    vm_columns = (["PIN"] if id_col else []) + \
        [dist_col] + [final_col for _, final_col in resolved_extra_specs] + ["geometry"]

    # ------------------------------------------------------------------
    # Influence Map geometry cleanup + spatial index.
    #
    # PERFORMANCE RULE (explicit, per review): the STRtree is built
    # EXACTLY ONCE per Influence Map source for this whole
    # process_parcels() call -- never rebuilt inside the per-parcel loop
    # below. Every parcel's nearest-feature lookup reuses this same
    # tree. UNCHANGED by this task.
    # ------------------------------------------------------------------
    fault_geoms = []
    # fault_attrs[i] is a dict of {raw_column_name: value} for
    # fault_geoms[i] -- carries EVERY checked column's value per
    # feature now (previously a single scalar name value only). Built
    # once here, before the per-parcel loop, exactly mirroring the
    # single-value pattern this replaced, just generalized to an
    # arbitrary set of columns.
    fault_attrs = []
    for fault_idx, (_, row) in enumerate(fault_gdf.iterrows()):
        fault_label = f"fault feature row {fault_idx}"
        g = fix_geometry(row.geometry, context_label=fault_label)
        if g is None:
            continue
        fault_geoms.append(g)
        fault_attrs.append({
            raw_col: (row.get(raw_col) if raw_col in fault_gdf.columns else None)
            for raw_col, _ in resolved_extra_specs
        })

    all_target_cols = [dist_col] + [final_col for _, final_col in resolved_extra_specs]

    if not fault_geoms:
        print(f"⚠️ [{source_name}] No usable Influence Map geometry -- "
              f"every parcel will receive NULL output values.")
        if progress_cb:
            for _ in range(len(parcel_gdf)):
                progress_cb(1)
        for col in all_target_cols:
            parcel_gdf[col] = None
        vm_gdf = gpd.GeoDataFrame(columns=vm_columns, geometry="geometry", crs=parcel_gdf.crs)
        return parcel_gdf, vm_gdf

    tree = STRtree(fault_geoms)

    dists_out = []
    # extras_out: {final_col: [value, value, ...]} -- one output list
    # per checked column, built in the same checked order as
    # resolved_extra_specs so downstream assignment stays deterministic.
    extras_out = {final_col: [] for _, final_col in resolved_extra_specs}
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
            dists_out.append(None)
            for _, final_col in resolved_extra_specs:
                extras_out[final_col].append(None)
            continue

        parcel_point = poly.representative_point()

        # ---- SINGLE nearest-feature lookup for this parcel ----
        # UNCHANGED selection logic -- only the attribute-carrying
        # shape (a dict of many values instead of one scalar) differs.
        res = tree.nearest(parcel_point)
        if isinstance(res, (int, np.integer)):
            nearest_geom = fault_geoms[int(res)]
            nearest_attrs = fault_attrs[int(res)]
        else:
            # Newer shapely returns the geometry directly; recover its
            # index for the parallel attribute lookup.
            try:
                nearest_idx = fault_geoms.index(res)
            except ValueError:
                nearest_idx = None
            nearest_geom = res
            nearest_attrs = fault_attrs[nearest_idx] if nearest_idx is not None else {}

        # ---- Measurement metric (decided AFTER the winner is chosen --
        # see process_parcels()'s docstring for the full rationale). The
        # winning feature itself is never re-selected here; only how its
        # distance/VM-line are computed changes. UNCHANGED by this task. ----
        if nearest_geom.geom_type in ("Polygon", "MultiPolygon"):
            # Polygon Influence Map feature (e.g. a fault hazard zone):
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

        dists_out.append(distance)
        for raw_col, final_col in resolved_extra_specs:
            extras_out[final_col].append(nearest_attrs.get(raw_col))

        record = {dist_col: distance, "geometry": vm_line}
        for raw_col, final_col in resolved_extra_specs:
            record[final_col] = nearest_attrs.get(raw_col)
        if id_col:
            record["PIN"] = parcel_gdf.iloc[idx][id_col]
        vm_records.append(record)

    parcel_gdf[dist_col] = dists_out
    for _, final_col in resolved_extra_specs:
        parcel_gdf[final_col] = extras_out[final_col]

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
from utils.progress_framework import (
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
    source_id, is_db_source, fault_gdf, dist_col, extra_column_specs, engine, schema,
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

    Args:
        dist_col: this run's distance output column name (see
        _compute_output_column_targets()).
        extra_column_specs: ordered (raw_column_name, final_column_name)
        pairs for the checked columns -- see process_parcels().
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
    zone_epsg = detect_prs92_zone([("Land Parcel", parcel_gdf), ("Influence Map Source", fault_gdf)])
    print(f"🌍 [{source_label}] Reprojecting to EPSG:{zone_epsg}...")
    parcel_gdf_proj = parcel_gdf.to_crs(epsg=zone_epsg)
    fault_gdf_proj = fault_gdf.to_crs(epsg=zone_epsg)

    status_cb(f"Measuring influence map distance: {source_label}...")
    parcel_gdf_proj, vm_gdf = process_parcels(
        parcel_gdf_proj, fault_gdf_proj, dist_col, extra_column_specs, source_label,
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
def run_processing(app_root, dist_col, extra_column_specs, overwrite_mode=None, per_source_resolution=None):
    """
    Orchestrates the full run on a background thread (worker(), defined
    below): loads DB credentials unconditionally (even for an all-local
    run -- see Args), loads the Influence Map source once, then for
    each selected Land Parcel source runs process_parcels() (via
    _process_one_source()) and saves the result either locally (.gpkg,
    optionally opened in Global Mapper) or to PostGIS.

    Args:
        app_root: parent Tk window for the ProgressWindow and any
        dialogs.
        dist_col: this run's resolved distance output column name --
        computed once in on_run() via _compute_output_column_targets()
        and _check_parcel_output_conflicts(), passed down rather than
        recomputed here so the exact same name (including any
        existing-column override) is used for both the conflict check
        the user already confirmed and the actual write.
        extra_column_specs: ordered (raw_column_name, final_column_name)
        pairs for the checked "Add Other Column(s)" checklist items --
        see process_parcels(). Empty list if none are checked.
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
        messagebox.showerror("Error", "Influence Map source not selected properly.")
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
        Background-thread body: loads the Influence Map source once,
        then for each selected Land Parcel source calls
        _process_one_source() (which itself calls process_parcels()
        and writes output), posting progress/status/completion/error
        events onto q for the main thread's queue-polling loop to
        consume. Never touches Tkinter widgets directly.
        """
        try:
            q.put(("update", "Loading Influence Map source...", None, None))
            try:
                fault_gdf = read_fault_line_source(fault_source, engine, schema)
                fault_gdf = ensure_geometry_column(fault_gdf)
            except Exception as e:
                q.put(("fatal_error", f"Could not load Influence Map source: {e}", None, None))
                return

            if len(fault_gdf) == 0:
                q.put(("fatal_error", "The selected Influence Map source has no features.", None, None))
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
                q.put(("update", f"Measuring influence map distance... "
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
                        source_id, is_db_source, fault_gdf, dist_col, extra_column_specs, engine, schema,
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
    Land Parcel and Influence Map Source pickers (each with a
    Local-file/Database-table radio toggle), an Output destination
    picker, and a Run button gated by _update_run_button_state().

    The Influence Map Source picker runs a background column-discovery
    read (_refresh_fault_columns(), via a daemon thread +
    win.after()-polled queue.Queue) immediately after a file/table is
    selected or the Local/Database toggle changes -- this populates the
    optional "Add Other Column(s)" checklist. Land Parcel, by
    contrast, runs NO background read at all -- its existing-output-
    column conflict check is Run-time-only now (see on_run()'s
    PRIORITY 1), since the actual target column set can't be known
    until the Influence Map Source's checklist has been resolved, and
    checking against a column set that might still change would be
    actively inaccurate (confirmed design decision).

    Args:
        root: the parent Tk root this window is opened under.
    """
    from tkinter import ttk

    win = tk.Toplevel(root)
    apply_icon(win, "distancefactor.ico")
    win.title("Influence Map Distance to Land Parcel Tool")
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

    # Single-selection architecture for BOTH Land Parcel and Influence
    # Map Source (Influence Map Source is single-select by explicit
    # task requirement; Land Parcel is single-select matching
    # influence_to_barangay.py's OWN actual picker -- see the note on
    # parcel_local_path below).
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

    # Influence Map Source column discovery: detect-on-select, read
    # immediately after a Browse/Select or a Local<->Database toggle --
    # this is what populates the "Add Other Column(s)" checklist.
    # Deliberately does NOT cache the result across calls -- every
    # selection AND every toggle triggers a fresh read, matching this
    # codebase's established detect-on-select convention elsewhere.
    fault_is_reading = False
    # fault_source_columns: {raw_column_name: [preview_value_str, ...]}
    # from the most recent successful read -- EXCLUDING the geometry
    # column, keyed exactly as cased/named on the source. The checklist
    # label uses the raw column name verbatim (no sanitization on
    # display text, only on the eventual output column name); the
    # value list is up to 10 distinct non-null/non-blank values, for
    # the hover-preview tooltip (see _show_fault_preview()).
    fault_source_columns = {}
    # copy_other_columns_var: the master "Add Other Column(s)"
    # checkbox. Only ever shown once a read has resolved AND found at
    # least one eligible column -- hidden entirely (not just disabled)
    # otherwise, per confirmed design.
    copy_other_columns_var = tk.BooleanVar(master=win, value=False)
    # fault_column_check_vars: {raw_col_name: tk.BooleanVar} -- one
    # entry per currently-available column. Rebuilt from scratch on
    # every fresh read (see _rebuild_fault_column_checklist()); never
    # merged with a previous read's state.
    fault_column_check_vars = {}

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

    def _reflow_window():
        """
        Safety net for dynamic-height content -- the Influence Map
        Source checklist growing/shrinking or showing/hiding -- combined
        with win.resizable(False, False) above. Direct port of
        road_width.py's own _reflow_window() (same rationale: measuring
        winfo_reqwidth()/reqheight() after update_idletasks() and
        setting minsize/maxsize/geometry to that exact value avoids
        both the "window gets stuck too small" bug and the window-
        decoration flicker that toggling resizable() causes on
        Windows). This tool previously had no dynamic-height content at
        all, so this function did not exist here before this task.

        Guarded against a REDUNDANT geometry/minsize/maxsize call: this
        function now runs on every Influence Map Source read cycle
        (even ones that end up changing nothing -- e.g. a source with
        zero eligible columns, same as the last one), and re-applying
        minsize()/maxsize()/geometry() to the SAME size on Windows can
        still trigger a full window repaint -- a plausible source of a
        reported brief black-flash artifact on the lower part of the
        window on every Influence Map Source selection. Comparing
        against the window's own last-applied size and skipping the
        three calls entirely when nothing changed removes that
        redundant repaint trigger without changing the function's
        actual resize behavior for genuine size changes.
        """
        win.update_idletasks()
        req_w = win.winfo_reqwidth()
        req_h = win.winfo_reqheight()
        if (req_w, req_h) == _reflow_window.last_applied_size:
            return
        # Clear both constraints FIRST, before applying the new
        # geometry, rather than setting the new minsize()/maxsize()
        # directly against the OLD, still-active constraints. When the
        # window is GROWING, calling minsize(req_w, req_h) with the new
        # (larger) size while maxsize() is still pinned to the OLD
        # (smaller) size means minsize briefly EXCEEDS maxsize -- an
        # invalid constraint pair for the brief window before maxsize()
        # is updated on the next line. On Windows this transient
        # invalid state is a plausible trigger for the reported brief
        # black-flash artifact specifically on GROW/SHRINK transitions
        # (never observed once content is stable, matching the
        # confirmed report). Resetting to the unconstrained default (0
        # minimum, a very large maximum) before applying geometry()
        # removes that transient invalid pairing entirely; the exact
        # pinned min==max==new-size is then reapplied right after.
        win.minsize(1, 1)
        win.maxsize(10000, 10000)
        win.geometry(f"{req_w}x{req_h}")
        win.minsize(req_w, req_h)
        win.maxsize(req_w, req_h)
        _reflow_window.last_applied_size = (req_w, req_h)
    _reflow_window.last_applied_size = (None, None)

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

    def browse_parcel_files():
        nonlocal parcel_local_path
        file = filedialog.askopenfilename(
            title="Select Land Parcel file",
            filetypes=VECTOR_FILETYPES)
        # Cancel returns "" -- do not assign, preserving previous selection.
        if file:
            parcel_local_path = file
            parcel_files_var.set(os.path.basename(file))
        _update_run_button_state()

    def _on_parcel_db_selected(sel):
        nonlocal parcel_db_table
        parcel_db_table = sel[0]
        parcel_db_label.set(sel[0])
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
        # remembered selection -- pre-existing behavior, left untouched.
        _update_run_button_state()

    # ── SECTION 2: INFLUENCE MAP SOURCE ──────────────────────────
    section_label(win, "Influence Map Source")

    fault_frame = tk.Frame(win)
    fault_frame.pack(fill="x", padx=18, pady=2)

    fault_radio_row = tk.Frame(fault_frame)
    fault_radio_row.pack(fill="x")
    fault_radio_local = tk.Radiobutton(fault_radio_row, text="Local File",
                   variable=fault_source_type, value="local",
                   command=lambda: _on_fault_toggle())
    fault_radio_local.pack(side="left")
    fault_radio_db = tk.Radiobutton(fault_radio_row, text="Database Table",
                   variable=fault_source_type, value="db",
                   command=lambda: _on_fault_toggle())
    fault_radio_db.pack(side="left", padx=(12, 0))

    fault_files_var = tk.StringVar(master=win, value="No file selected")
    fault_db_label = tk.StringVar(master=win, value="No table selected")

    fault_action_row = tk.Frame(fault_frame)
    fault_action_row.pack(fill="x", pady=2)

    fault_lbl = tk.Label(fault_action_row, textvariable=fault_files_var,
                         fg="gray", anchor="w", width=42)
    fault_lbl.pack(side="left")

    fault_btn = tk.Button(fault_action_row, text="Browse…", width=10)
    fault_btn.pack(side="left", **PAD)

    # "Add Other Column(s)" master checkbox -- created once, only
    # packed/unpacked (never destroyed) by
    # _update_fault_checklist_visibility(). Hidden entirely (not just
    # disabled) whenever the currently selected source has zero
    # eligible (non-geometry) columns -- per confirmed design, the
    # DISTANCE-only default output never depends on this checkbox, so
    # Run stays available either way.
    #
    # fault_checklist_header_row: holds BOTH the checkbox above and the
    # "Check All | Uncheck All" links, on the SAME row, links
    # right-aligned -- matching influence_map_to_land_parcel.py's own
    # header_row/links_frame pattern exactly (per explicit follow-up
    # request), not a separate button row below the checklist.
    fault_checklist_header_row = tk.Frame(fault_frame)

    copy_other_columns_checkbox = tk.Checkbutton(
        fault_checklist_header_row, text="Add Other Column(s)",
        variable=copy_other_columns_var)
    copy_other_columns_checkbox.pack(side="left")

    def _check_all_fault_columns():
        """Checks every currently-available column's BooleanVar. Mirrors
        influence_map_to_land_parcel.py's own _check_all_source()."""
        for var in fault_column_check_vars.values():
            var.set(True)

    def _uncheck_all_fault_columns():
        """Unchecks every currently-available column's BooleanVar.
        Mirrors influence_map_to_land_parcel.py's own
        _uncheck_all_source()."""
        for var in fault_column_check_vars.values():
            var.set(False)

    fault_checklist_links_frame = tk.Frame(fault_checklist_header_row)
    # NOT packed here -- _update_fault_checklist_visibility() packs/
    # unpacks it conditionally on copy_other_columns_var's state (only
    # visible while the master checkbox is checked; see that function's
    # docstring).
    fault_check_all_link = tk.Label(
        fault_checklist_links_frame, text="Check All", fg="#1a73e8", cursor="hand2",
        font=("Segoe UI", 8, "underline"))
    fault_check_all_link.pack(side="left")
    fault_check_all_link.bind("<Button-1>", lambda e: _check_all_fault_columns())
    tk.Label(fault_checklist_links_frame, text=" | ", fg="gray",
             font=("Segoe UI", 8)).pack(side="left")
    fault_uncheck_all_link = tk.Label(
        fault_checklist_links_frame, text="Uncheck All", fg="#1a73e8", cursor="hand2",
        font=("Segoe UI", 8, "underline"))
    fault_uncheck_all_link.pack(side="left")
    fault_uncheck_all_link.bind("<Button-1>", lambda e: _uncheck_all_fault_columns())

    # Holds one Checkbutton (+ hover-preview "i" icon) per eligible
    # (non-geometry) column found in the currently selected Influence
    # Map source. Only packed while the checkbox above is checked AND
    # at least one eligible column was found. Item-count-threshold
    # vertical scrollbar (matching
    # INFLUENCE_CHECKLIST_MAX_ITEMS_BEFORE_VSCROLL's own threshold),
    # dual-scroll (vertical + horizontal) when needed, and a WIDTH
    # PINNED to a fixed value every resize (never left to auto-grow to
    # fit wide content) -- direct port of
    # influence_map_to_land_parcel.py's own
    # _resize_influence_source_checklist_box() sizing logic (per
    # explicit follow-up request: the previous pixel-height-cap +
    # auto-width version let the whole window widen whenever a
    # scrollbar appeared, which this pinned-width approach prevents).
    FAULT_CHECKLIST_MAX_ITEMS_BEFORE_VSCROLL = 8
    FAULT_CHECKLIST_LEFT_INDENT = 20
    FAULT_CHECKLIST_RIGHT_MARGIN = 8

    # fault_checklist_outer/canvas/vscroll/hscroll/container: REBUILT
    # from scratch (destroy + recreate) on every content change, never
    # merely reconfigured in place -- see _build_fault_checklist_widgets()
    # below for the full root-cause reasoning (a live-verified Tkinter
    # bug, documented in landmarks_within_meters.py's own
    # _create_category_section(): Canvas.configure(height=N) leaves a
    # STICKY stale winfo_reqheight() on the Canvas's own parent Frame
    # that survives pack_forget(), destroying only the canvas's
    # children, update_idletasks(), and even a full win.withdraw()/
    # win.deiconify() cycle -- the ONLY thing that clears it is
    # destroying the Frame that directly holds the canvas and creating
    # a fresh replacement). This is the confirmed root cause of the
    # reported GUI-grow/shrink flicker: the stale reqheight left the
    # window's own size computation briefly wrong on every transition,
    # which is what a brief black-flash redraw was actually showing.
    # Declared here as None; _build_fault_checklist_widgets() (called
    # once at init, and again at the top of every
    # _rebuild_fault_column_checklist() call) is what actually creates
    # them.
    fault_checklist_outer = None
    fault_checklist_canvas = None
    fault_checklist_vscroll = None
    fault_checklist_hscroll = None
    fault_checklist_container = None
    _fault_checklist_canvas_window = None

    def _build_fault_checklist_widgets():
        """
        Destroys the CURRENT fault_checklist_outer (if any) and builds
        a fresh one -- outer Frame, Canvas, both Scrollbars, and the
        inner content container -- from scratch. This is the ONLY
        place any of these widgets are constructed. See the module-
        level comment above (where these names are first declared as
        None) for the full root-cause history this exists to fix.

        Called once at initial window construction (see the bottom of
        open_main_window()), and again at the top of EVERY
        _rebuild_fault_column_checklist() call -- i.e. every time the
        checklist's content is about to change, before any new
        Checkbutton/icon rows are added and before
        _resize_fault_checklist_box() runs. Never called on its own
        without an immediate _rebuild_fault_column_checklist() to
        follow, since a freshly built container always starts empty.

        Does NOT touch fault_column_check_vars, fault_preview_expanded,
        or fault_source_columns -- those are plain Python state, not
        tied to any widget's lifetime; _rebuild_fault_column_checklist()
        is still the only thing that repopulates them.

        Leaves the new fault_checklist_outer UNPACKED -- exactly like
        the very first construction did -- the caller
        (_update_fault_checklist_visibility()) remains responsible for
        packing/unpacking it, unchanged from before.
        """
        nonlocal fault_checklist_outer, fault_checklist_canvas
        nonlocal fault_checklist_vscroll, fault_checklist_hscroll
        nonlocal fault_checklist_container, _fault_checklist_canvas_window

        if fault_checklist_outer is not None and fault_checklist_outer.winfo_exists():
            fault_checklist_outer.destroy()

        fault_checklist_outer = tk.Frame(fault_frame)
        fault_checklist_canvas = tk.Canvas(
            fault_checklist_outer, highlightthickness=0, bd=0)
        fault_checklist_vscroll = tk.Scrollbar(
            fault_checklist_outer, orient="vertical",
            command=fault_checklist_canvas.yview, width=9)
        fault_checklist_hscroll = tk.Scrollbar(
            fault_checklist_outer, orient="horizontal",
            command=fault_checklist_canvas.xview)
        fault_checklist_canvas.configure(
            yscrollcommand=fault_checklist_vscroll.set,
            xscrollcommand=fault_checklist_hscroll.set)
        fault_checklist_canvas.pack(side="left", fill="both", expand=True)
        # Both scrollbars are packed/unpacked dynamically by
        # _resize_fault_checklist_box() below -- only shown when
        # content actually exceeds the box in that direction.

        # fault_checklist_container: the actual content frame drawn
        # INSIDE the canvas -- this is what
        # _rebuild_fault_column_checklist() populates with fresh rows
        # right after calling this function.
        fault_checklist_container = tk.Frame(fault_checklist_canvas)
        _fault_checklist_canvas_window = fault_checklist_canvas.create_window(
            (0, 0), window=fault_checklist_container, anchor="nw")

        def _on_fault_checklist_content_configure(_event=None):
            fault_checklist_canvas.configure(
                scrollregion=fault_checklist_canvas.bbox("all"))
        fault_checklist_container.bind(
            "<Configure>", _on_fault_checklist_content_configure)

        def _on_fault_checklist_mousewheel(event):
            fault_checklist_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        fault_checklist_canvas.bind(
            "<Enter>", lambda e: fault_checklist_canvas.bind_all(
                "<MouseWheel>", _on_fault_checklist_mousewheel))
        fault_checklist_canvas.bind(
            "<Leave>", lambda e: fault_checklist_canvas.unbind_all("<MouseWheel>"))

    def _resize_fault_checklist_box():
        """
        Recomputes fault_checklist_canvas's own height AND width to fit
        fault_checklist_container's CURRENT content -- direct port of
        influence_map_to_land_parcel.py's own
        _resize_influence_source_checklist_box() sizing logic.

        Vertical scrollbar trigger: item count >
        FAULT_CHECKLIST_MAX_ITEMS_BEFORE_VSCROLL (8), with the per-row
        pixel height derived from this content's own current height
        (content_height / n_items) so the cap translates into an
        accurate pixel height regardless of font/theme.

        Horizontal overflow: the canvas width is explicitly PINNED to a
        FIXED value on every call -- derived from fault_action_row's
        own already-established requested width (a stable sibling row
        that never itself changes size from checklist content, so this
        anchor can never self-referentially inflate the way anchoring
        off fault_frame directly could), minus the vertical scrollbar's
        width (whenever shown) and
        FAULT_CHECKLIST_LEFT_INDENT/FAULT_CHECKLIST_RIGHT_MARGIN --
        never left to grow to match wide column-name content. This is
        the fix for the reported bug where the whole window widened
        whenever the vertical scrollbar appeared: a column name wider
        than this pinned width now triggers the horizontal scrollbar
        instead of ever widening the canvas (and therefore win)
        itself.

        Called once per content change, never in a tight loop. Always
        operates on a FRESHLY BUILT canvas (see
        _build_fault_checklist_widgets()) -- never on one that already
        had a previous .configure(height=...) applied to it, which is
        what the stale-reqheight bug this whole rebuild discipline
        exists to avoid required.
        """
        fault_checklist_container.update_idletasks()
        n_items = len(fault_column_check_vars)
        content_height = fault_checklist_container.winfo_reqheight()
        content_width = fault_checklist_container.winfo_reqwidth()

        show_vscroll = n_items > FAULT_CHECKLIST_MAX_ITEMS_BEFORE_VSCROLL and n_items > 0
        vscroll_width = fault_checklist_vscroll.winfo_reqwidth() if show_vscroll else 0

        fixed_row_width = fault_action_row.winfo_reqwidth()
        canvas_width = max(
            fixed_row_width - vscroll_width - FAULT_CHECKLIST_LEFT_INDENT
            - FAULT_CHECKLIST_RIGHT_MARGIN, 1)
        fault_checklist_canvas.configure(width=canvas_width)

        if n_items <= FAULT_CHECKLIST_MAX_ITEMS_BEFORE_VSCROLL or n_items == 0:
            fault_checklist_canvas.configure(height=content_height)
            fault_checklist_vscroll.pack_forget()
        else:
            row_height = content_height / n_items
            capped_height = int(round(row_height * FAULT_CHECKLIST_MAX_ITEMS_BEFORE_VSCROLL))
            fault_checklist_canvas.configure(height=capped_height)
            fault_checklist_vscroll.pack(side="right", fill="y")

        if content_width > canvas_width:
            fault_checklist_canvas.itemconfig(_fault_checklist_canvas_window, width=content_width)
            fault_checklist_hscroll.pack(side="bottom", fill="x")
        else:
            fault_checklist_canvas.itemconfig(_fault_checklist_canvas_window, width=canvas_width)
            fault_checklist_hscroll.pack_forget()
    # fault_checklist_outer starts as None (see declaration above);
    # _build_fault_checklist_widgets() is called once at init (bottom
    # of open_main_window()) to create the first real instance, then
    # _update_fault_checklist_visibility() (via _refresh_fault_columns())
    # decides what to show.

    # ── hover-preview tooltip (Toplevel-based, single persistent
    # window reused for every hover) -- direct port of
    # influence_map_to_land_parcel.py's own
    # _show_influence_preview()/_hide_influence_preview()/
    # _toggle_influence_preview() subsystem. A single, PERSISTENT
    # Toplevel/Frame/Text, created ONCE here and reused for every hover
    # (hidden via withdraw(), shown via deiconify()) -- never destroyed
    # and recreated per hover, since the reference implementation's own
    # design history found that destroy/recreate caused stray visual
    # artifacts and hidden content on real Windows rendering.
    FAULT_PREVIEW_WRAP_PIXELS = 420

    # fault_preview_expanded: {raw_column_name: bool} -- per-column
    # sticky "see more"/"see less" toggle state (this tool has only one
    # Influence Map source at a time, so no source_key is needed here,
    # unlike the multi-source reference this was ported from). Reset to
    # empty only when the checklist itself is rebuilt from a fresh
    # discovery.
    fault_preview_expanded = {}

    _fault_preview_tip = tk.Toplevel(win)
    _fault_preview_tip.wm_overrideredirect(True)
    _fault_preview_tip.attributes("-topmost", True)
    _fault_preview_tip.withdraw()
    _fault_preview_frame = tk.Frame(
        _fault_preview_tip, bg="#333333", bd=1, relief="solid")
    _fault_preview_frame.pack()
    _fault_preview_font = tkfont.Font(family="Segoe UI", size=8)
    _fault_preview_hint_font = tkfont.Font(family="Segoe UI", size=8, slant="italic")
    _fault_preview_text = tk.Text(
        _fault_preview_frame, bg="#333333", fg="white",
        font=_fault_preview_font, wrap="char", bd=0,
        highlightthickness=0, padx=6, pady=4, cursor="hand2")
    _fault_preview_text.tag_configure(
        "hanging", lmargin1=0, lmargin2=_fault_preview_font.measure("\u2022 "))
    _fault_preview_text.tag_configure("hint", font=_fault_preview_hint_font)
    _fault_preview_text.pack()

    _fault_tooltip_ref = {"icon": None, "col": None, "values": None}

    def _on_fault_preview_click(_e=None):
        ref = _fault_tooltip_ref
        if ref["col"] is not None:
            _toggle_fault_preview(ref["col"])
    _fault_preview_text.bind("<Button-1>", _on_fault_preview_click)

    def _hide_fault_preview():
        _fault_preview_tip.withdraw()
        _fault_tooltip_ref["col"] = None

    def _show_fault_preview(icon_widget, col, values):
        """
        Shows a small borderless Toplevel to the right of icon_widget
        (the per-row " ℹ️" icon), listing `values` -- already the exact
        up-to-10 sorted unique non-null values discovered for that
        column during the background read -- never a fresh per-hover
        re-read. Pixel-accurate truncation/wrapping and adaptive
        width/height -- direct port of
        influence_map_to_land_parcel.py's own
        _show_influence_preview(); see that function's own docstring
        (this file's earlier reading) for the full rationale behind
        every measurement below.
        """
        preview_font = _fault_preview_font
        bullet_prefix = "\u2022 "

        def _pixel_truncate_value(v):
            if preview_font.measure(f"{bullet_prefix}{v}") <= FAULT_PREVIEW_WRAP_PIXELS:
                return v, False
            truncated = ""
            for ch in v:
                candidate = truncated + ch
                if preview_font.measure(f"{bullet_prefix}{candidate}…") <= FAULT_PREVIEW_WRAP_PIXELS:
                    truncated = candidate
                else:
                    break
            return truncated + "…", True

        def _pixel_wrap_line(text, budget_px):
            if not text:
                return [""]
            lines = []
            current = ""
            for ch in text:
                candidate = current + ch
                if preview_font.measure(candidate) <= budget_px or not current:
                    current = candidate
                else:
                    lines.append(current)
                    current = ch
            lines.append(current)
            return lines

        is_expanded = fault_preview_expanded.get(col, False)
        any_truncated = False
        show_hint = False
        hint = ""

        if not values:
            display_lines = ["(no values)"]
            all_wrapped_lines = ["(no values)"]
        else:
            display_lines = []
            all_wrapped_lines = []
            for v in values:
                if is_expanded:
                    display_v = v
                else:
                    display_v, was_truncated = _pixel_truncate_value(v)
                    if was_truncated:
                        any_truncated = True
                display_lines.append(display_v)
                wrapped = _pixel_wrap_line(
                    f"{bullet_prefix}{display_v}", FAULT_PREVIEW_WRAP_PIXELS)
                all_wrapped_lines.extend(wrapped)

            show_hint = any_truncated or (is_expanded and any(
                preview_font.measure(f"{bullet_prefix}{v}") > FAULT_PREVIEW_WRAP_PIXELS
                for v in values))
            if show_hint:
                hint = "to see less click ℹ️" if is_expanded else "to see more click ℹ️"
                all_wrapped_lines.append(hint)

        total_display_line_count = len(all_wrapped_lines)
        zero_char_pixel_width = max(preview_font.measure("0"), 1)
        width_ceiling_chars = max(
            math.ceil(FAULT_PREVIEW_WRAP_PIXELS / zero_char_pixel_width), 20)
        bullet_lines_only = all_wrapped_lines[:-1] if show_hint else all_wrapped_lines
        max_line_pixel_width = max(
            (preview_font.measure(l) for l in bullet_lines_only),
            default=zero_char_pixel_width * 10)
        if show_hint:
            max_line_pixel_width = max(
                max_line_pixel_width, _fault_preview_hint_font.measure(hint))
        actual_width_chars = min(
            max(math.ceil(max_line_pixel_width / zero_char_pixel_width) + 1, 1),
            width_ceiling_chars)

        _fault_preview_text.configure(state="normal")
        _fault_preview_text.delete("1.0", "end")
        _fault_preview_text.configure(
            width=actual_width_chars, height=total_display_line_count)

        if not values:
            _fault_preview_text.insert("end", "(no values)")
        else:
            for display_v in display_lines:
                _fault_preview_text.insert("end", f"\u2022 {display_v}\n", "hanging")
            if show_hint:
                _fault_preview_text.insert("end", hint, "hint")
            else:
                _fault_preview_text.delete("end-2c", "end-1c")

        _fault_preview_text.configure(state="disabled")

        x = icon_widget.winfo_rootx() + icon_widget.winfo_width() + 4
        y = icon_widget.winfo_rooty()
        _fault_preview_tip.geometry(f"+{x}+{y}")
        _fault_preview_tip.deiconify()
        _fault_preview_tip.lift()
        _fault_preview_tip.update_idletasks()

        _fault_tooltip_ref["icon"] = icon_widget
        _fault_tooltip_ref["col"] = col
        _fault_tooltip_ref["values"] = values

    def _toggle_fault_preview(col):
        """
        Flips the sticky truncate/expand state for exactly one column
        -- persists across hover leave/enter cycles until toggled again
        or the checklist is rebuilt from a fresh discovery.
        """
        fault_preview_expanded[col] = not fault_preview_expanded.get(col, False)

        ref = _fault_tooltip_ref
        if (_fault_preview_tip.state() != "withdrawn"
                and ref.get("col") == col):
            _show_fault_preview(ref["icon"], col, ref["values"])

    def _rebuild_fault_column_checklist():
        """
        Rebuilds the "Add Other Column(s)" checklist from
        fault_source_columns: one Checkbutton + hover-preview " ℹ️" icon
        per eligible column, labeled with its EXACT raw name/casing as
        found on the source (no sanitization applied to the checklist
        display text -- confirmed design; sanitization only ever
        applies to the eventual OUTPUT column name, computed separately
        at Run time).

        Calls _build_fault_checklist_widgets() FIRST -- a fresh
        fault_checklist_outer/canvas/container, never the previous
        cycle's already-.configure()'d canvas -- see that function's
        own docstring (and the declaration comment above it) for the
        full root-cause history of why reusing the same canvas across
        cycles is unsafe. Also rebuilds fault_column_check_vars and
        fault_preview_expanded from scratch. Called directly by the
        caller (_poll_fault_columns_queue() / _handle_fault_columns_
        failure() / _clear_fault_checklist_state()) before
        _update_fault_checklist_visibility() -- never by that function
        itself.
        """
        nonlocal fault_column_check_vars, fault_preview_expanded
        _build_fault_checklist_widgets()
        fault_column_check_vars = {}
        fault_preview_expanded = {}
        for col in sorted(fault_source_columns.keys()):
            var = tk.BooleanVar(master=win, value=False)
            fault_column_check_vars[col] = var

            row = tk.Frame(fault_checklist_container)
            row.pack(fill="x", anchor="w")
            tk.Checkbutton(row, text=col, variable=var).pack(side="left")
            icon = tk.Label(row, text=" ℹ️", cursor="hand2",
                             font=("Segoe UI", 8))
            icon.pack(side="left")
            preview_values = fault_source_columns[col]
            icon.bind("<Enter>", lambda e, c=col, vals=preview_values, w=icon:
                      _show_fault_preview(w, c, vals))
            icon.bind("<Leave>", lambda e: _hide_fault_preview())
            icon.bind("<Button-1>", lambda e, c=col: _toggle_fault_preview(c))

    def _update_fault_checklist_visibility():
        """
        Shows the "Add Other Column(s)" header row (checkbox always
        visible whenever there's at least one eligible column) plus,
        ONLY when the checkbox is checked, the right-aligned "Check
        All | Uncheck All" links on that same row AND the checklist
        itself below it. The links are hidden -- not just the
        checklist -- while the checkbox is unchecked, since "check/
        uncheck all" has no meaningful target until there's a visible
        checklist to act on (confirmed follow-up requirement).

        Hides everything -- header row included -- when there are no
        eligible columns at all, since the DISTANCE-only default
        output never needs a checklist.

        Deliberately a no-op while fault_is_reading -- the "Reading
        Influence Map source..." indicator lives elsewhere (see
        _set_fault_reading_state()'s docstring: it reuses the existing
        filename/table-name label via text swap, needing zero
        _reflow_window() calls of its own), and this function's own
        checkbox/checklist state is left completely untouched for the
        entire duration of a background read. Only ever invoked once a
        read's actual result is known, so it triggers at most ONE
        resize per call.
        """
        if fault_is_reading:
            return
        if fault_source_columns:
            fault_checklist_header_row.pack(fill="x", pady=(2, 0))
            if copy_other_columns_var.get():
                if not fault_checklist_links_frame.winfo_ismapped():
                    fault_checklist_links_frame.pack(side="right", padx=(0, 6))
                # Pack BEFORE resizing -- winfo_width() is meaningless
                # until the canvas has actually been packed into the
                # window at least once (same reasoning as
                # road_width.py's own equivalent function).
                if not fault_checklist_outer.winfo_ismapped():
                    fault_checklist_outer.pack(
                        fill="x", padx=(20, 0), pady=(2, 0),
                        after=fault_checklist_header_row)
                _resize_fault_checklist_box()
            else:
                fault_checklist_links_frame.pack_forget()
                fault_checklist_outer.pack_forget()
        else:
            fault_checklist_header_row.pack_forget()
            fault_checklist_links_frame.pack_forget()
            fault_checklist_outer.pack_forget()
        _reflow_window()

    def _on_copy_other_columns_changed(*_args):
        _update_fault_checklist_visibility()
        _update_run_button_state()

    copy_other_columns_var.trace_add("write", _on_copy_other_columns_changed)

    def _clear_fault_checklist_state():
        """
        Fully resets the "Add Other Column(s)" checkbox/checklist state
        back to empty -- used when the currently active mode
        (Local File / Database Table) has no selection at all (e.g.
        immediately after toggling to a mode where nothing has been
        browsed/selected yet). Without this, the PREVIOUS mode's
        checklist/checked-columns/master-checkbox state stayed visibly
        stuck on screen even though there is no longer a real selection
        behind it -- a confirmed reported bug, along with the visible
        window-size distortion that came from the stale checklist
        content not being cleared before the (now-empty) header row
        was re-evaluated.

        Mirrors _handle_fault_columns_failure()'s own cleanup lines,
        minus the error dialog -- this path is not an error, just "no
        selection yet."
        """
        nonlocal fault_source_columns
        fault_source_columns = {}
        copy_other_columns_var.set(False)
        _rebuild_fault_column_checklist()
        _update_fault_checklist_visibility()

    def _set_fault_reading_state(reading):
        """
        Disables fault_btn and both fault_radio_local/fault_radio_db
        while the background column-discovery read is in progress --
        prevents starting a second, overlapping read of the same
        selection. Matches road_width.py's verified
        _set_road_reading_state() pattern exactly.

        Reuses the EXISTING "No file selected"/filename/"No table
        selected" label (fault_lbl, bound to fault_files_var/
        fault_db_label) via a temporary text swap -- no new widget --
        restoring it from the authority variables
        (fault_local_path/fault_local_layer/fault_db_table) once done.

        Does NOT itself touch fault_is_reading -- that flag is owned
        and reset directly by _refresh_fault_columns()/
        _poll_fault_columns_queue()/_handle_fault_columns_failure(),
        matching road_width.py's own explicitly-documented convention.
        """
        state = "disabled" if reading else "normal"
        fault_btn.config(state=state)
        fault_radio_local.config(state=state)
        fault_radio_db.config(state=state)

        if reading:
            fault_files_var.set("⏳ Reading Influence Map source...")
            fault_db_label.set("⏳ Reading Influence Map source...")
            fault_lbl.config(fg="#b36b00")
        else:
            if fault_source_type.get() == "local":
                fault_files_var.set(
                    (os.path.basename(fault_local_path) + (f"  (layer: {fault_local_layer})" if fault_local_layer else ""))
                    if fault_local_path else "No file selected"
                )
            else:
                fault_db_label.set(
                    fault_db_table if fault_db_table
                    else "No table selected"
                )
            fault_lbl.config(fg="gray")
        _update_run_button_state()

    def _handle_fault_columns_failure(source_type, reason):
        """
        Shared cleanup for both outcomes of a FAILED Influence Map
        Source column-discovery read: a read that never completed
        within 60 seconds ("timeout"), or one that completed with an
        actual read error ("failure"). Mirrors the Land Parcel side's
        former _handle_parcel_check_failure() pattern (that mechanism
        was removed from the Land Parcel side per confirmed design --
        see open_main_window()'s docstring -- but this Influence Map
        Source read still needs the same discipline, since it still
        genuinely runs at selection time).

        Captures the failed source's display name BEFORE clearing the
        authority variable, then clears ONLY the authority variable(s)
        for source_type -- fault_local_path/fault_local_layer if
        "local", fault_db_table if "db" -- forcing the existing "no
        source selected" display path to handle recovery.

        _set_fault_reading_state(False) is called BEFORE the dialog is
        shown, not after -- messagebox.showerror() is modal and blocks
        here until dismissed, so showing it first would leave the
        "⏳ Reading Influence Map source..." indicator frozen on screen
        for the entire time the dialog is up.
        """
        nonlocal fault_is_reading, fault_local_path, fault_local_layer, fault_db_table
        nonlocal fault_source_columns

        # CRITICAL: fault_is_reading is owned by _refresh_fault_columns()/
        # _poll_fault_columns_queue()/this function -- NOT by
        # _set_fault_reading_state() (see that function's own docstring:
        # it only manages widget state). Must be explicitly reset here or
        # every subsequent _refresh_fault_columns() call would silently
        # no-op forever.
        fault_is_reading = False

        if source_type == "local":
            failed_name = (os.path.basename(fault_local_path)
                           if fault_local_path else "the selected file")
            fault_local_path = None
            fault_local_layer = None
        else:
            failed_name = fault_db_table if fault_db_table else "the selected table"
            fault_db_table = None

        fault_source_columns = {}
        _set_fault_reading_state(False)
        copy_other_columns_var.set(False)
        _rebuild_fault_column_checklist()
        _update_fault_checklist_visibility()

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

        messagebox.showerror(title, message, parent=win)

    def _poll_fault_columns_queue(result_queue, source_type, deadline):
        """
        Runs on the main thread via win.after() polling. Picks up the
        (columns, error) tuple placed on the queue by the background
        worker, or detects a timeout if 60 seconds have elapsed with no
        result. Ordering matters: the queue is ALWAYS checked before
        the deadline (single-threaded Tkinter main loop, fresh
        queue.Queue() per call, no generation counter needed -- same
        reasoning as road_density.py's identical pattern elsewhere in
        this project).
        """
        nonlocal fault_source_columns, fault_is_reading
        if not win.winfo_exists():
            return
        try:
            columns, error = result_queue.get_nowait()
        except queue.Empty:
            if time.time() >= deadline:
                _handle_fault_columns_failure(source_type, "timeout")
            else:
                win.after(100, lambda: _poll_fault_columns_queue(
                    result_queue, source_type, deadline))
            return

        if error is not None:
            _handle_fault_columns_failure(source_type, "failure")
            return

        fault_is_reading = False
        fault_source_columns = columns
        _set_fault_reading_state(False)
        copy_other_columns_var.set(False)
        _rebuild_fault_column_checklist()
        _update_fault_checklist_visibility()

    def _refresh_fault_columns():
        """
        Background-reads the currently selected Influence Map source's
        non-geometry columns, immediately after a Browse/Select or a
        Local<->Database toggle -- populates the "Add Other Column(s)"
        checklist. For each eligible column, also collects up to its
        first 10 distinct non-null, non-blank values (sorted, string-
        cast) for the hover-preview tooltip -- same discovery rule as
        influence_map_to_land_parcel.py's own
        _read_influence_source_columns_worker(), ported here rather
        than imported (Rule of Three). Gives up after 60 seconds with
        no result (see _poll_fault_columns_queue()) -- a hung read must
        not leave the tool waiting indefinitely.

        Deliberately does NOT cache the result across calls -- every
        call, whether triggered by a fresh Browse/Select or by toggling
        Local <-> Database, always performs a real read (confirmed
        design decision), never a cached result.
        """
        nonlocal fault_is_reading
        if fault_is_reading:
            return

        source_type = fault_source_type.get()
        if source_type == "local":
            if not fault_local_path:
                # Nothing selected for this mode -- nothing to read.
                # Clear any stale checklist/checkbox state left over
                # from the PREVIOUS mode's selection (e.g. toggling
                # from a Local File that had columns checked over to
                # an as-yet-empty Database Table) -- otherwise the old
                # checkbox/checklist stays visibly stuck on screen even
                # though there is no longer a real selection behind it
                # (confirmed reported bug). No background read is
                # involved here, so this is an immediate, synchronous
                # clear -- not the transient "reading" state at all.
                _clear_fault_checklist_state()
                return
            path_or_table = fault_local_path
            layer = fault_local_layer
        else:
            if not fault_db_table:
                _clear_fault_checklist_state()
                return
            path_or_table = fault_db_table
            layer = None

        result_queue = queue.Queue()

        def worker():
            try:
                if source_type == "local":
                    ext = os.path.splitext(path_or_table)[1].lower()
                    if ext == ".gpkg" and layer:
                        gdf = gpd.read_file(path_or_table, layer=layer)
                    else:
                        gdf = gpd.read_file(path_or_table)
                else:
                    creds = load_db_credentials()
                    if not creds:
                        result_queue.put((None, "Could not load DB credentials."))
                        return
                    engine = create_engine(
                        f"postgresql://{creds['username']}:{creds['password']}@"
                        f"{creds['host']}:{creds['port']}/{creds['database']}"
                    )
                    gdf = read_postgis_clean(path_or_table, engine, creds["schema"])
                gdf = ensure_geometry_column(gdf)
                eligible = {}
                for col in gdf.columns:
                    if col.lower() in ("geometry", "geom"):
                        continue
                    series = gdf[col].dropna()
                    # Blank/whitespace-only strings are a distinct "no
                    # real value" case from NaN/None -- same reasoning
                    # as influence_map_to_land_parcel.py's own
                    # discovery worker.
                    series = series[~series.map(
                        lambda v: isinstance(v, str) and v.strip() == "")]
                    if series.empty:
                        continue
                    unique_vals = sorted({str(v) for v in series.unique()})[:10]
                    eligible[col] = unique_vals
                result_queue.put((eligible, None))
            except Exception as e:
                result_queue.put((None, str(e)))

        deadline = time.time() + 60  # see _poll_fault_columns_queue()
        fault_is_reading = True
        _set_fault_reading_state(True)
        threading.Thread(target=worker, daemon=True).start()
        win.after(100, lambda: _poll_fault_columns_queue(
            result_queue, source_type, deadline))

    def browse_fault_file():
        nonlocal fault_local_path, fault_local_layer
        file = filedialog.askopenfilename(
            title="Select Influence Map file",
            filetypes=VECTOR_FILETYPES)
        if not file:
            return
        try:
            layer = resolve_local_fault_layer(win, file)
        except Exception as e:
            messagebox.showerror("Error", f"Could not read Influence Map source: {e}", parent=win)
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
        # Always checks fresh -- see _refresh_fault_columns() docstring:
        # no result is ever cached across calls.
        _refresh_fault_columns()

    def _on_fault_db_selected(sel):
        nonlocal fault_db_table
        fault_db_table = sel[0]
        fault_db_label.set(sel[0])
        _update_run_button_state()
        _refresh_fault_columns()

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
        """
        Pure label/command setup for the Local File <-> Database Table
        switch -- no background read triggered here. Mirrors
        road_width.py's own toggle_parcel()/toggle_road() functions,
        which are DELIBERATELY separate from that file's
        _update_parcel_classification_visibility()/
        _update_road_classification_visibility() (the checklist show/
        hide + resize functions) -- both are called independently, once
        each, at window-init time (see the bottom of
        open_main_window()). Mixing the two into one function (as an
        earlier version of this file did) meant window construction
        ended up firing an extra, redundant background-read-triggered
        resize cycle beyond the one the reference design actually
        needs -- a confirmed source of the reported visual distortion.
        See _on_fault_toggle() below for the user-facing Radiobutton
        command, which calls this AND starts a fresh read.
        """
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

    def _on_fault_toggle():
        """
        The actual Radiobutton command= target for the Local File /
        Database Table switch -- runs the pure label/command setup
        above, THEN triggers a fresh background read (confirmed
        design: every toggle re-reads, never a cached result). Kept
        separate from _toggle_fault() itself so window-init can call
        the pure setup alone, without also kicking off a read/resize
        cycle during construction -- see _toggle_fault()'s own
        docstring for the full rationale.
        """
        _toggle_fault()
        _refresh_fault_columns()

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
        Run button handler: validates Land Parcel + Influence Map
        Source + Output selections are present, computes this run's
        dynamic output column set from the selected Influence Map
        source's display name plus whatever "Add Other Column(s)"
        checklist items are checked, runs the Land Parcel existing-
        output-column conflict check SYNCHRONOUSLY against that dynamic
        set (PRIORITY 1 -- confirmed Run-time-only design; this is no
        longer a detect-on-select background check), runs the local
        output-file conflict check (PRIORITY 2), and per-source
        DB-output table resolution (PRIORITY 3) -- each able to cancel
        the whole run -- then destroys this window and hands off to
        run_processing(). Sets the module-level parcel_source,
        fault_source, output_mode, and parcel_output_column_overrides
        globals on success.
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
                messagebox.showerror("Error", "Please select an Influence Map file.", parent=win)
                return
            fault_source = ("local", fault_local_path, fault_local_layer)
        else:
            if not fault_db_table:
                messagebox.showerror("Error", "Please select an Influence Map table.", parent=win)
                return
            fault_source = ("db", fault_db_table)

        output_mode = (
            ("local", output_local_dir.get()) if output_dest_type.get() == "local"
            else ("db", None)
        )

        if output_mode[0] == "local" and not output_mode[1]:
            messagebox.showerror("Error", "Please select an output folder.", parent=win)
            return

        # ---- Compute this run's dynamic output column set ----
        # checked_raw_columns: ordered per the checklist's own UI
        # order (dict insertion order == the order columns were
        # populated in _rebuild_fault_column_checklist(), itself the
        # order the source's own columns were returned in) -- this
        # exact order becomes the output column order (Task Prompt
        # Section E hard requirement).
        checked_raw_columns = (
            [col for col, var in fault_column_check_vars.items() if var.get()]
            if copy_other_columns_var.get() else []
        )
        fault_display_name = _influence_source_display_name(
            fault_source[0],
            fault_source[1],
            fault_source[2] if fault_source[0] == "local" else None,
        )
        targets = _compute_output_column_targets(fault_display_name, checked_raw_columns)
        dist_col, *extra_final_cols = targets
        extra_column_specs = list(zip(checked_raw_columns, extra_final_cols))

        # PRIORITY 1: column conflict check -- warn if the selected Land
        # Parcel source already has any of this run's dynamic target
        # column names. Shown before the file-conflict dialog so the
        # user can decide whether to proceed at all before being asked
        # about filename conflicts. Declining cancels the run entirely;
        # main window stays open.
        #
        # Confirmed Run-time-only design: this check can only run now,
        # here, since the target column set (dist_col/extra_column_specs
        # above) isn't known until the Influence Map Source checklist
        # has been resolved -- reading the Land Parcel source any
        # earlier (e.g. at selection time, as this tool used to) would
        # be checking against a column set that might still change,
        # which would be actively inaccurate.
        conflicts = _check_parcel_output_conflicts(
            parcel_source[1], parcel_source[0], targets)
        if conflicts is None:
            messagebox.showerror(
                "Error",
                "Could not read the selected Land Parcel source to check for "
                "existing output column(s). Please try again.",
                parent=win,
            )
            return
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
        run_processing(root, dist_col, extra_column_specs, overwrite_mode, per_source_resolution)

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

        if not has_parcel:
            run_status_var.set("Please select a Land Parcel source.")
            ready = False
        elif not has_fault:
            run_status_var.set("Please select an Influence Map source.")
            ready = False
        elif not has_output:
            run_status_var.set("Please select an Output destination.")
            ready = False
        elif fault_is_reading:
            # Influence Map Source column-discovery read is still in
            # flight -- the checklist state used to build this run's
            # dynamic output column set is not yet current. Zero-checked
            # columns is still a perfectly valid Run state once reading
            # finishes (DISTANCE-only output) -- this branch only blocks
            # Run WHILE the read itself is actively in progress.
            checking_name = (
                os.path.basename(fault_local_path) if fault_source_type.get() == "local"
                else fault_db_table
            ) or "source"
            run_status_var.set(f'⏳ Reading "{checking_name}" columns…')
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
    _build_fault_checklist_widgets()
    _update_fault_checklist_visibility()
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