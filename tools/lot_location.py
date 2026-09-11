"""
tools/lot_location.py

PURPOSE:
    CAMA Tools tool ("LOT LOCATION" in MAIN.py's dispatch table): for
    each Land Parcel, classifies its road-frontage situation as
    "Inner Lot" (no road contact), "Road Lot" (touches exactly one
    road, or 2+ roads that don't form a corner), or "Corner Lot"
    (touches 2+ roads whose intersection is physically at/near the
    parcel, via an Intersection Buffer Test -- see _is_corner_lot()'s
    docstring), writing the result into CAMA_LOT_LOCATION (or an
    existing differently-cased column, if one was detected and
    confirmed at Run time -- see lot_location_column_overrides below).
    A user-optional Road Type filter (Section 2 of the main window) can
    exclude specific ROAD_TYPE values from consideration entirely.

DISPATCH:
    Run as an isolated subprocess by MAIN.py via its `--tool` dispatch
    mechanism (see system context). Entry point is main(), triggered via
    the `if __name__ == "__main__":` guard at the bottom of this file.

INPUTS:
    Land Parcel source: one or more local files or PostGIS tables.
    Road Network source: a single local file or PostGIS table,
    optionally with a ROAD_TYPE-like column (see
    ROAD_TYPE_COLUMN_CANDIDATES) for the optional exclusion filter.
    pg_credentials.json (via load_db_credentials(), from
    utils/db_discovery.py) for any DB source or DB output.

OUTPUTS:
    Local output mode: writes one atomically-written .gpkg per
    processed Land Parcel source (_write_gpkg()), then attempts to open
    it in Global Mapper (load_in_global_mapper()).
    DB output mode: writes/replaces one PostGIS table per source,
    resolved via resolve_db_output_table() -- an exact-match replace for
    a DB Land Parcel source, or a fuzzy-match-with-confirmation flow
    (confirm_db_overwrite_dialog() / choose_db_overwrite_dialog()) for a
    local-file Land Parcel source. As of the D-Cancel rollout (ported
    from landmarks_within_meters.py's own D4 -- see "DB ATOMIC WRITE /
    CANCEL-SAFE STAGING" below), the actual DB write never touches the
    real destination table directly: _write_db_output_safely() writes
    to a randomly-named staging table first, verifies it, then promotes
    it via a single atomic rename-swap transaction. See that section,
    and ProgressWindow/run_processing() below, for how Cancel is
    supported through the classification loop itself (mid-
    classification Cancel, full discard on trigger -- see
    process_lot_location()'s own progress-contract docstring), and
    disabled only once a real (non-cancelled) result exists, through
    the save that follows.

DEPENDENCIES:
    stdlib: os, re, subprocess, json, threading, queue, time, secrets,
    itertools (combinations), ctypes, sys, tkinter (+ ttk).
    third-party: geopandas, pandas, numpy, psycopg2, shapely (geometry +
    validation), sqlalchemy.
    local: utils.table_name_matching, utils.resource_path,
    utils.db_discovery, utils.column_detection, utils.window_icon,
    utils.progress_framework (imported mid-file, directly above the
    class/function that uses it -- see the Progress Event Protocol v9
    comment block further below for why this file's progress dialog was
    migrated to that shared framework).

SIDE EFFECTS:
    File reads/writes (.shp/.gpkg). PostGIS reads/writes. A live
    PostgreSQL connection. Tkinter GUI windows throughout, including
    TWO independent background-thread + queue.Queue-based detect-on-
    select systems in open_main_window() -- one for the Land Parcel
    existing-output-column check, one for reading the Road Network
    layer to populate the Road Type filter checklist (whose result is
    cached in _road_gdf_cache and reused by run_processing() if the
    same source is still selected) -- plus a third background thread +
    queue.Queue for the main processing run itself. A subprocess launch
    to Global Mapper (load_in_global_mapper()) on local-output saves,
    plus a Win32 EnumWindows call to find/focus an already-open Global
    Mapper window first. DB-output runs additionally open one dedicated,
    non-pooled psycopg2 connection for the run's PostgreSQL advisory
    lock (_acquire_run_lock()/_release_run_lock() -- see "DB ATOMIC
    WRITE / CANCEL-SAFE STAGING" below), held for the duration of the
    DB-output portion of the run and released in worker()'s own
    finally: block.

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
import subprocess
import json
import threading
import queue
import time
import secrets
from itertools import combinations
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox, ttk

import geopandas as gpd
import pandas as pd
import numpy as np
import psycopg2
from shapely.validation import make_valid
from shapely.geometry import Point
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

# road_type_excluded_values: list[str] of ROAD_TYPE values (exact,
# case-sensitive) that the user unchecked in the Section 2 "Filter by
# Road Type" checklist. Set by on_run() inside open_main_window(), read
# by run_processing() and threaded into process_lot_location(). Empty
# list => no filtering (default / backward-compatible behavior).
road_type_excluded_values = []

# lot_location_column_overrides: {path_or_table: existing_col_name} — for
# any Land Parcel source where a pre-existing "cama_lot_location"-like
# column was detected (see the GUI's _check_parcel_lot_location_worker())
# and the user confirmed proceeding. Set by on_run(), read by
# run_processing() and threaded into process_lot_location() as
# output_column_name, so the tool writes back into the EXACT existing
# column (preserving its original casing) instead of always writing a
# hardcoded "CAMA_LOT_LOCATION" — the latter would silently create a
# confusing duplicate column whenever the existing one used different
# casing (e.g. "cAMA_lot_location" alongside a new "CAMA_LOT_LOCATION").
# A source with no entry here uses the default "CAMA_LOT_LOCATION" name.
# NOTE (CAMA_ prefix rollout): this check only recognizes the NEW
# "cama_lot_location" name. Pre-rollout files that still have the old,
# unprefixed "lot_location" column will NOT be flagged as a conflict —
# accepted tradeoff per project decision, since this tool has not yet
# been run against any pre-rollout output. Revisit if that changes.
lot_location_column_overrides = {}

# _road_gdf_cache: holds the most recently, successfully read road layer
# so run_processing() can reuse it instead of re-reading the same file/DB
# table that was already read to populate the Section 2 checklist.
# Keyed by (source_type, path_or_table) so a stale cache from a different
# selection is never silently reused. See _clear_road_type_filter() and
# _poll_road_read_queue() inside open_main_window() for the write side.
_road_gdf_cache = {"key": None, "gdf": None}

# ========================================
# CRS UTILITY
# ========================================
# PRS92 zones are non-overlapping 2-degree longitude bands (EPSG registry):
#   Zone I   (3121): west of 118°E
#   Zone II  (3122): 118°E – 120°E  (Palawan, Calamian Islands)
#   Zone III (3123): 120°E – 122°E  (Luzon west of 122°E, Mindoro)
#   Zone IV  (3124): 122°E – 124°E  (SE Luzon, Panay, Cebu, Negros, west Mindanao)
#   Zone V   (3125): east of 124°E  (east Mindanao, east Visayas)
PRS92_ZONE_BOUNDS = [
    (-180.0, 118.0, 3121, "Zone I"),
    (118.0,  120.0, 3122, "Zone II"),
    (120.0,  122.0, 3123, "Zone III"),
    (122.0,  124.0, 3124, "Zone IV"),
    (124.0,  180.0, 3125, "Zone V"),
]


def detect_prs92_zone(labeled_gdfs):
    """
    Detect the appropriate PRS92 zone from the combined geographic
    extent of one or more GeoDataFrames.

    Parameters
    ----------
    labeled_gdfs
        List of (label, GeoDataFrame) tuples, e.g.
        [("Land Parcel", barangay_gdf), ("Road Network", road_gdf)].
        The label is used only for diagnostics -- it has no effect on
        CRS detection.

    Returns
    -------
    int
        The EPSG code of the detected PRS92 zone.

    Notes
    -----
    Uses bounding-box midpoint (total_bounds) instead of
    unary_union.centroid to avoid GEOS TopologyExceptions caused by
    invalid geometries. Reprojects each input to EPSG:4326 first when
    its CRS isn't already WGS84. Uses the COMBINED extent of every
    GeoDataFrame passed in (this tool reprojects both the parcel and
    road layers together).

    Optional layers that are absent (None) or contain no usable
    features are skipped. Layers that survive that initial validation
    but still produce invalid geographic bounds (NaN) raise a
    ValueError identifying the offending layer.

    Integration note (lot_location-specific): this tool has no progress
    callback, so unlike road_frontage.py's (epsg, warning) tuple
    return, this returns only the EPSG code -- any warning is printed
    directly.
    """
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
            print(f"ℹ️ Auto-detected PRS92 {zone_label} (EPSG:{epsg}) "
                  f"from combined bbox-midpoint longitude {center_lon:.4f}°E")
            return epsg

    raise ValueError(f"Could not determine PRS92 zone for longitude {center_lon}")


# ========================================
# GEOMETRY FIX
# ========================================
def fix_geometry(geom):
    """Repairs an invalid geometry via buffer(0), falling back to
    make_valid() if that isn't enough. Returns None for a None, empty,
    or unrepairable geometry."""
    if geom is None or geom.is_empty: return None
    try:
        if not geom.is_valid: geom = geom.buffer(0)
        if not geom.is_valid: geom = make_valid(geom)
        return geom if not geom.is_empty else None
    except: return None

# ========================================
# LOT LOCATION LOGIC
# ========================================

# CORNER_TOLERANCE_METERS: radius of the buffer applied to the road intersection
# point. Must be >= half the typical ROW width plus a digitization margin.
# Default 15m per industry standard (Gemini / mass appraisal reference).
# QA team may adjust this value when final threshold parameters are confirmed.
CORNER_TOLERANCE_METERS = 15.0

def _is_corner_lot(parcel_geom, road_geom_list, tolerance=CORNER_TOLERANCE_METERS):
    """
    Intersection Buffer Test (Option C — industry standard for mass appraisal).

    For every unique pair of road geometries associated with the parcel:
      1. Test if the two roads intersect anywhere.
      2. If yes, extract the intersection point (junction).
      3. Buffer that junction by `tolerance` meters.
      4. If the parcel intersects that buffer → parcel is physically at the
         corner → Corner Lot.

    If no pair passes the buffer test, the parcel spans between two roads
    that either don't intersect near it, or don't intersect at all
    → Through-Lot / Road Lot.

    All geometries must already be in a projected metric CRS (PRS92)
    before this function is called.

    Parameters
    ----------
    parcel_geom   : Shapely geometry  — the parcel polygon (projected)
    road_geom_list: list of Shapely geometries — road centerlines (projected)
    tolerance     : float — buffer radius in meters (default 15.0)

    Returns
    -------
    True  → Corner Lot
    False → Road Lot (through-lot or single-frontage)
    """
    for road_a, road_b in combinations(road_geom_list, 2):
        # Skip if either geometry is missing
        if road_a is None or road_b is None:
            continue
        if road_a.is_empty or road_b.is_empty:
            continue

        # Step 1: Do these two roads intersect at all?
        if not road_a.intersects(road_b):
            continue  # No intersection → not a corner candidate for this pair

        # Step 2: Get the intersection geometry (point, multipoint, or segment)
        intersection = road_a.intersection(road_b)
        if intersection is None or intersection.is_empty:
            continue

        # Step 3: Reduce intersection to one or more junction points
        geom_type = intersection.geom_type
        if geom_type == "Point":
            junction_points = [intersection]
        elif geom_type == "MultiPoint":
            junction_points = list(intersection.geoms)
        else:
            # Overlapping segments (T-junction digitized as shared segment),
            # or GeometryCollection — use centroid as representative point
            junction_points = [intersection.centroid]

        # Step 4: Intersection Buffer Test
        # Is the parcel physically located at this junction?
        for jpt in junction_points:
            if parcel_geom.intersects(jpt.buffer(tolerance)):
                return True  # Parcel is at the corner → Corner Lot

    return False  # No corner condition found → Road Lot / Through-Lot


def _deduplicate_road_ids(id_list, road_name_map):
    """
    Optional deduplication: group road IDs by road_name so that two segments
    of the same named road are treated as one road.

    If road_name_map is empty (column absent or all NULL), returns id_list
    unchanged — geometry-only path is used.

    Returns a list of representative IDs (one per unique road name group).
    For unnamed roads (NULL / empty name), each ID is kept as its own group.
    """
    if not road_name_map:
        return id_list  # No name data → use all IDs as-is

    seen_names = set()
    deduped = []
    for rid in id_list:
        name = road_name_map.get(rid, "").strip() if road_name_map.get(rid) else ""
        if name:
            if name not in seen_names:
                seen_names.add(name)
                deduped.append(rid)
            # else: same named road already represented → skip this ID
        else:
            # NULL or empty name → treat as its own unique road
            deduped.append(rid)
    return deduped


# ROAD_TYPE_COLUMN_CANDIDATES: case-insensitive column-name aliases used to
# locate a road-classification column in a user-supplied road layer. Shared
# between process_lot_location() (server-side filtering) and the GUI's
# road-type checklist (Section 2 of open_main_window()) so both agree on
# what counts as a "ROAD_TYPE-like" column.
#
# NOTE: "highway" was already part of the pre-existing PUBLIC_ROAD_TYPES
# detection logic before this feature — kept as-is for backward
# compatibility with any dataset that uses OSM-style column naming. Do not
# remove without confirming no dataset depends on it; that decision is out
# of scope for this feature.
ROAD_TYPE_COLUMN_CANDIDATES = ("road_type", "roadtype", "highway")


def _detect_road_type_column(gdf):
    """
    Case-insensitive lookup of a ROAD_TYPE-like column in a GeoDataFrame.
    Returns the actual column name (original casing preserved) or None.
    """
    if gdf is None:
        return None
    return next(
        (c for c in gdf.columns if c.lower() in ROAD_TYPE_COLUMN_CANDIDATES),
        None
    )


def label_lot_location(val):
    """Maps the internal classification code (0/1/2) to its
    human-readable label ("Inner Lot"/"Road Lot"/"Corner Lot"),
    defaulting to "Unknown" for any other value."""
    return {0: "Inner Lot", 1: "Road Lot", 2: "Corner Lot"}.get(val, "Unknown")


def process_lot_location(barangay_gdf, road_gdf, source_name="", excluded_road_types=None, progress=None, output_column_name="CAMA_LOT_LOCATION"):
    """
    Core classification engine. Unchanged from the prior "Double Frontage"
    fix (Intersection Buffer Test / road-name deduplication below) except
    for the road-type filtering step, which replaces the old hardcoded
    PUBLIC_ROAD_TYPES allowlist with a fully optional, user-driven filter.

    Parameters
    ----------
    excluded_road_types : optional list/set of str — ROAD_TYPE values
        (exact, case-sensitive match) to exclude from the road layer
        before the 10m buffer / Intersection Buffer Test / classification
        steps run. None or empty => no filtering, every road feature in
        the layer is used. This is the default and is backward-compatible
        with any existing caller that doesn't pass this argument.
    progress : optional callable progress(message, value=None, maximum=None)
        — called at coarse milestones and, throttled, during the
        classification loop. None (default) disables progress reporting
        entirely — every call site below is guarded, so this remains
        backward-compatible with any existing caller that doesn't pass it.

        Return-value contract (mid-classification Cancel): ONLY the
        classification loop's own throttled call site (below) checks the
        return value, and only stops on an EXPLICIT `False` -- any other
        return (None, True, or no return at all -- the shape every
        caller before this contract existed already produces) is treated
        as "continue," so an old-style caller that never returns
        anything keeps working exactly as before. The other four call
        sites (reprojection, geometry cleaning, spatial join, finished)
        do not check the return value at all -- they are each a single
        blocking operation with no loop to break out of, so there is no
        meaningful place to stop mid-call; the classification loop's own
        first checkpoint (i == 1) is reached quickly enough after those
        phases that a Cancel clicked during them is still honored
        promptly, without needing a separate checkpoint of their own.
        On a mid-loop Cancel this function returns None instead of a
        GeoDataFrame -- see the classification loop below.
    output_column_name : str — the column name the classification is
        written to. Defaults to "CAMA_LOT_LOCATION" (this tool's normal
        output, CAMA_-prefixed per project-wide column naming convention
        — see road_width.py's own ROAD_WIDTH -> CAMA_ROAD_WIDTH). The GUI
        overrides this per-source when the selected parcel layer already
        has an existing "cama_lot_location"-like column (any casing) —
        the exact existing name/casing is passed here so processing
        writes back into that same column instead of creating a
        hardcoded "CAMA_LOT_LOCATION" alongside it as a confusing
        duplicate.
    """
    orig_crs = barangay_gdf.crs
    zone_epsg = detect_prs92_zone([("Land Parcel", barangay_gdf), ("Road Network", road_gdf)])
    print(f"🌍 [{source_name}] Using EPSG:{zone_epsg}")
    if progress:
        progress(f"Reprojecting {source_name} to EPSG:{zone_epsg}")
    brgy_proj = barangay_gdf.to_crs(epsg=zone_epsg)
    road_proj = road_gdf.to_crs(epsg=zone_epsg)

    # ------------------------------------------------------------------
    # Optional, user-driven road-type filter (replaces the old hardcoded
    # PUBLIC_ROAD_TYPES allowlist). There is no built-in list of "public"
    # road types anymore — ROAD_TYPE label conventions vary by LGU, so
    # the GUI (Section 2, "Filter by Road Type") lets the user exclude
    # whichever values exist in their own dataset, if they choose to.
    #
    # Default behavior (excluded_road_types is None/empty): no filtering,
    # every road feature is used for classification.
    #
    # Safety net: since the hardcoded filter is gone, the Intersection
    # Buffer Test (_is_corner_lot, 15m tolerance, above) is the sole
    # automatic defense against false Corner Lot classification. This
    # filter is an optional refinement on top of it, not a required
    # safety mechanism.
    # ------------------------------------------------------------------
    road_type_col = _detect_road_type_column(road_proj)
    if road_type_col and excluded_road_types:
        original_count = len(road_proj)
        road_proj = road_proj[
            ~road_proj[road_type_col].isin(excluded_road_types)
        ].copy()
        filtered_count = len(road_proj)
        print(f"ℹ️  [{source_name}] Road type filter: {filtered_count}/{original_count} "
              f"roads retained after excluding {len(excluded_road_types)} type(s) "
              f"(column: '{road_type_col}')")
        if filtered_count == 0:
            print(f"⚠️  [{source_name}] All roads excluded by filter — "
                  f"falling back to full road layer.")
            road_proj = road_gdf.to_crs(epsg=zone_epsg)
    else:
        print(f"ℹ️  [{source_name}] No road type filtering applied — "
              f"using full road layer ({len(road_proj)} features).")

    # Assign a row-level integer ROAD_ID if not already present
    if "ROAD_ID" not in road_proj.columns:
        road_proj = road_proj.copy()
        road_proj["ROAD_ID"] = range(len(road_proj))

    # ------------------------------------------------------------------
    # Build lookup dicts from road_proj (all in projected metric CRS)
    # ------------------------------------------------------------------

    # road_id (int) → road geometry (Shapely, projected)
    road_geom_map = {
        int(row["ROAD_ID"]): row["geometry"]
        for _, row in road_proj.iterrows()
        if row["geometry"] is not None and not row["geometry"].is_empty
    }

    # road_id (int) → road_name (str or "")
    # Only populated if a non-trivial road_name column exists
    road_name_col = next(
        (c for c in road_proj.columns
         if c.lower() in ("road_name", "roadname", "name", "street", "road_no")),
        None
    )
    road_name_map = {}
    if road_name_col:
        non_null_count = road_proj[road_name_col].notna().sum()
        if non_null_count > 0:
            road_name_map = {
                int(row["ROAD_ID"]): (str(row[road_name_col]).strip()
                                      if pd.notna(row[road_name_col]) else "")
                for _, row in road_proj.iterrows()
            }
            print(f"ℹ️  [{source_name}] road_name column '{road_name_col}' found "
                  f"({non_null_count} non-null values) — segment deduplication enabled.")
        else:
            print(f"ℹ️  [{source_name}] road_name column '{road_name_col}' is all NULL "
                  f"— geometry-only classification.")
    else:
        print(f"ℹ️  [{source_name}] No road_name column found "
              f"— geometry-only classification.")

    # ------------------------------------------------------------------
    # Fixed geometry — computed ONCE per parcel, used ONLY for topological
    # operations below (spatial join, Intersection Buffer Test). The
    # ORIGINAL geometry in brgy_proj/result is never modified — this
    # keeps the exported output faithful to the source data, matching
    # the pattern already used in road_frontage.py's
    # process_frontage_single(). A parcel whose geometry cannot be
    # repaired (fix_geometry returns None) simply can't participate in
    # the spatial join below, which naturally falls back to an empty
    # ROAD_ID (-> Inner Lot) for that row — the same conservative
    # default already used elsewhere in this function for missing
    # geometry, not a new failure mode.
    # ------------------------------------------------------------------
    if progress:
        progress("Cleaning parcel geometries...")
    brgy_fixed_geom = brgy_proj.geometry.apply(fix_geometry)

    _PIN_CANDIDATES = ["PIN", "pin", "Pin", "ARP_NO", "TD_NO", "PARCEL_ID"]
    _pin_col = next((c for c in _PIN_CANDIDATES if c in brgy_proj.columns), None)
    unfixable_mask = brgy_fixed_geom.isna() & brgy_proj.geometry.notna()
    if unfixable_mask.any():
        ids = (brgy_proj.loc[unfixable_mask, _pin_col].astype(str).tolist()
               if _pin_col else [str(i) for i in brgy_proj.index[unfixable_mask]])
        print(f"⚠️  [{source_name}] {unfixable_mask.sum()} parcel(s) have "
              f"unfixable geometry — kept as original shape in output, "
              f"excluded from road-touch testing: {ids[:20]}"
              f"{' ...' if len(ids) > 20 else ''}")

    # ------------------------------------------------------------------
    # Spatial join: which roads does each parcel's 10m buffer touch?
    # Uses the fixed geometry (sjoin_input) so intersects() runs on valid
    # topology; the output (result) is built from brgy_proj separately,
    # below, keeping the original geometry untouched.
    # ------------------------------------------------------------------
    sjoin_input = brgy_proj.copy()
    sjoin_input["geometry"] = brgy_fixed_geom

    if progress:
        progress("Running spatial join...")
    road_buffer = road_proj.copy()
    road_buffer["geometry"] = road_proj.geometry.buffer(10, cap_style=2)
    joined = gpd.sjoin(sjoin_input, road_buffer[["ROAD_ID", "geometry"]],
                       how="left", predicate="intersects")
    grouped = joined.groupby(joined.index).agg({
        "ROAD_ID": lambda x: ",".join(
            sorted(set(str(int(v)) for v in x if pd.notna(v)))
        )
    })

    result = brgy_proj.copy()   # ORIGINAL geometry — never overwritten

    # ROAD_ID (comma-separated touched-road-feature-index string per
    # parcel) is working data for the classification loop immediately
    # below ONLY -- it drives the Inner/Road/Corner Lot decision (how
    # many distinct roads a parcel touches, and which ones, for the
    # Intersection Buffer Test), but has no use once classification is
    # done: neither road_frontage.py nor road_width.py ever reads it,
    # and a raw internal road-feature-index string has little diagnostic
    # value to a human reading the attribute table either. Kept as a
    # plain dict (index label -> ROAD_ID string), never written as a
    # column on `result` -- it is never saved to the exported GPKG/DB
    # table.
    road_id_by_idx = grouped["ROAD_ID"].to_dict()

    # ------------------------------------------------------------------
    # Classification — replaces the old compute_lot_location() string check
    # ------------------------------------------------------------------
    lot_location_codes = []
    total = len(result)
    cancelled_mid_loop = False
    for i, (idx, row) in enumerate(result.iterrows(), start=1):
        if progress and (i % 200 == 0 or i == 1 or i == total):
            if progress(f"Classifying {source_name}: {i}/{total}", i, total) is False:
                # Mid-classification Cancel (full discard, per explicit
                # decision -- matches landmarks_within_meters.py's own
                # full-discard behavior for its PROCESSING phase). Stop
                # immediately; lot_location_codes so far is thrown away
                # below rather than padded/truncated into a partial
                # result -- there is no use for a half-classified output.
                cancelled_mid_loop = True
                break

        road_id_str = road_id_by_idx.get(idx, "")

        # --- Inner Lot: no road contact ---
        if not road_id_str or not road_id_str.strip():
            lot_location_codes.append(0)
            continue

        id_list = [int(x) for x in road_id_str.split(",") if x.strip()]

        # --- Road Lot: only one road feature touched ---
        if len(id_list) == 1:
            lot_location_codes.append(1)
            continue

        # --- 2+ road features touched: need to determine Corner vs Road ---

        # Step 1: Optional deduplication by road_name
        # (reduces same-road multi-segment false positives)
        deduped_ids = _deduplicate_road_ids(id_list, road_name_map)

        # After deduplication, if only one unique road remains → Road Lot
        if len(deduped_ids) == 1:
            lot_location_codes.append(1)
            continue

        # Step 2: Intersection Buffer Test (Option C)
        # Retrieve the actual road geometries for the deduped IDs
        road_geoms = [road_geom_map[rid] for rid in deduped_ids if rid in road_geom_map]

        # Uses the FIXED geometry (not row["geometry"], which is now the
        # original/possibly-invalid shape) — .loc[idx] rather than .get():
        # brgy_fixed_geom and result both derive from brgy_proj with no
        # filtering/reindexing in between, so their indices are guaranteed
        # aligned. A KeyError here would mean that invariant was broken by
        # a future change — better to fail loudly than silently fall back.
        parcel_geom = brgy_fixed_geom.loc[idx]
        if parcel_geom is None or parcel_geom.is_empty:
            lot_location_codes.append(1)  # Can't test → default Road Lot (conservative)
            continue

        if _is_corner_lot(parcel_geom, road_geoms, tolerance=CORNER_TOLERANCE_METERS):
            lot_location_codes.append(2)  # Corner Lot
        else:
            lot_location_codes.append(1)  # Road Lot (through-lot / double frontage)

    if cancelled_mid_loop:
        return None

    # output_column_name (default "CAMA_LOT_LOCATION") holds the
    # human-readable classification directly ("Inner Lot" / "Road Lot" /
    # "Corner Lot") -- per project lead decision, this column should
    # contain the actual classification, not an internal numeric code,
    # and an end user has no reason to see a code column in the
    # attribute table. lot_location_codes (0/1/2) stays purely internal
    # to this function; label_lot_location() is applied here, once, to
    # produce the only classification column that ends up in the output.
    result[output_column_name] = [label_lot_location(v) for v in lot_location_codes]

    if progress:
        progress(f"Finished {source_name}", total, total)

    if orig_crs:
        result = result.to_crs(orig_crs)
    return result

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
        str | None: the geometry column name, or None if not found.
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT f_geometry_column FROM geometry_columns
                WHERE f_table_schema=:schema AND f_table_name=:table
            """),{"schema":schema,"table":table_name}).fetchone()
            return row[0] if row else None
    except: return None

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
    geom_col = get_geometry_column(table,engine,schema)
    insp = inspect(engine)
    cols = [c["name"] for c in insp.get_columns(table,schema=schema) if c["name"]!=geom_col]
    col_str = ", ".join([f'"{c}"' for c in cols]) if cols else ""
    q = f'SELECT {col_str+", " if col_str else ""}"{geom_col}" AS geometry FROM "{schema}"."{table}"'
    return gpd.read_postgis(q, engine, geom_col="geometry")

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
    apply_icon(dialog, "lotlocation.ico")
    dialog.title("File(s) Already Exist")
    dialog.resizable(False, False)
    dialog.grab_set()

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

    # deiconify/lift/focus_force/topmost are called LAST -- after
    # content and geometry() are finalized, and topmost is never reset
    # back to False -- see road_frontage.py's matching dialogs for the
    # full rationale (repositioning can perturb stacking order against
    # another always-on-top window from a separate process, e.g. the
    # CAMA Tools floating panel; grab_set() alone cannot protect this
    # indefinite-duration dialog from being covered by it). The
    # periodic re-assert loop below keeps winning that z-order fight
    # for the dialog's whole lifetime, not just at creation -- confirmed
    # necessary in testing, a single lift() at creation was not enough.
    # Self-cancels via the winfo_exists() guard once dialog.destroy()
    # runs.
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
    apply_icon(dialog, "lotlocation.ico")
    dialog.title("LOT LOCATION TOOL")
    dialog.resizable(False, False)
    dialog.grab_set()

    def choose(confirmed):
        result["confirmed"] = confirmed
        dialog.destroy()

    dialog.protocol("WM_DELETE_WINDOW", lambda: choose(False))

    # Buttons packed first, at the bottom -- same reasoning as
    # ask_overwrite_dialog() above.
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

    # deiconify/lift/focus_force/topmost are called LAST -- after
    # content and geometry() are finalized, and topmost is never reset
    # back to False -- see road_frontage.py's matching dialogs for the
    # full rationale (repositioning can perturb stacking order against
    # another always-on-top window from a separate process, e.g. the
    # CAMA Tools floating panel; grab_set() alone cannot protect this
    # indefinite-duration dialog from being covered by it). The
    # periodic re-assert loop below keeps winning that z-order fight
    # for the dialog's whole lifetime, not just at creation -- confirmed
    # necessary in testing, a single lift() at creation was not enough.
    # Self-cancels via the winfo_exists() guard once dialog.destroy()
    # runs.
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
    apply_icon(dialog, "lotlocation.ico")
    dialog.title("LOT LOCATION TOOL")
    dialog.resizable(False, False)
    dialog.grab_set()

    def choose(confirm):
        result["chosen"] = selected.get() if confirm else None
        dialog.destroy()

    dialog.protocol("WM_DELETE_WINDOW", lambda: choose(False))

    # Buttons packed first, at the bottom -- same reasoning as
    # ask_overwrite_dialog() above.
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

    # deiconify/lift/focus_force/topmost are called LAST -- after
    # content and geometry() are finalized, and topmost is never reset
    # back to False -- see road_frontage.py's matching dialogs for the
    # full rationale (repositioning can perturb stacking order against
    # another always-on-top window from a separate process, e.g. the
    # CAMA Tools floating panel; grab_set() alone cannot protect this
    # indefinite-duration dialog from being covered by it). The
    # periodic re-assert loop below keeps winning that z-order fight
    # for the dialog's whole lifetime, not just at creation -- confirmed
    # necessary in testing, a single lift() at creation was not enough.
    # Self-cancels via the winfo_exists() guard once dialog.destroy()
    # runs.
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
        import ctypes
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

        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
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
    picker = tk.Toplevel(parent)
    apply_icon(picker, "lotlocation.ico")
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
    Local-file/Database-table radio toggle), a Road Type exclusion
    checklist (Section 2, populated after the Road Network is read), an
    Output destination picker, and a Run button gated by
    _update_run_button_state().

    Runs TWO independent background detect-on-select systems (each its
    own daemon thread + win.after()-polled queue.Queue):
      - Land Parcel: checks for an existing CAMA_LOT_LOCATION-like
        column the moment a file/table is selected or toggled -- see
        _refresh_parcel_lot_location_check(), _set_parcel_reading_state(),
        _handle_parcel_check_failure().
      - Road Network: reads the road layer to populate the Road Type
        checklist, and caches the result in the module-level
        _road_gdf_cache so run_processing() can reuse it instead of
        reading the same source twice -- see _refresh_road_type_filter(),
        _set_road_reading_state(), _handle_road_check_failure().
    Both systems are independent of each other and of Run being clicked.

    Args:
        root: the parent Tk root this window is opened under.
    """
    from tkinter import ttk
    win = tk.Toplevel(root)
    apply_icon(win, "lotlocation.ico")
    win.title("Lot Location Tool")
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

    # Road-type filter state (Section 2). road_is_reading / road_read_ok
    # are plain closure locals (not Tkinter variables) mutated via
    # `nonlocal` from the nested functions below.
    road_type_filter_check_var = tk.BooleanVar(master=win, value=False)
    road_type_value_vars = {}   # {str(value): tk.BooleanVar(value=True)}
    road_is_reading = False     # True while the background read thread is active
    road_read_ok = False        # True once the current road source has been read successfully

    # Land Parcel existing-CAMA_LOT_LOCATION-column check (Section 1). Mirrors
    # the Road Network background-read shape above. Deliberately does NOT
    # cache the detection result (see group-05-cache-removal-analysis.md):
    # a cache keyed only on "which file/table was selected" cannot detect
    # that the file/table's CONTENTS changed externally (e.g. another
    # CAMA tool, QGIS, or Global Mapper modifying it) between one
    # selection and the next -- serving a stale "no conflict" result would
    # defeat the whole purpose of this check. Every selection AND every
    # Local/Database toggle triggers a fresh read instead. What IS still
    # remembered per mode is only WHICH file/table was last selected
    # (parcel_local_path / parcel_db_table below) -- that's a completely
    # separate concern from the detection result and is unaffected by
    # this decision.
    parcel_is_reading = False
    parcel_existing_lot_location = []   # [(source_label, existing_col_name), ...]

    PAD = dict(padx=8, pady=4)

    def section_label(parent, text):
        frm = tk.Frame(parent)
        frm.pack(fill="x", padx=10, pady=(10, 2))
        tk.Label(frm, text=text,
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        ttk.Separator(frm, orient="horizontal").pack(
            side="left", fill="x", expand=True, padx=(6, 0), pady=4)

    def _run_status_message():
        """
        Returns the current status text for the permanent label under
        the Run button — always a string, including the "ready" case
        (unlike the earlier hover-tooltip design, this has no "None"
        state since something is always shown).

        Priority order (checked top to bottom, first match wins):
          1. Land Parcel not selected
          2. Road Network not selected
          3. Output Destination not selected
          4. Land Parcel currently being checked for an existing
             CAMA_LOT_LOCATION column (background thread)
          5. Road Network currently being read (background thread)
          6. Road Network selected but not yet successfully read
             (covers: read failed, or hasn't completed for any other
             reason) — same corrective action as case 2, since a
             working road source isn't actually available yet either
             way, so it reuses that message rather than introducing a
             separate "something went wrong" message here.
          7. Ready.
        Selection-completeness (1-3) is checked before either read status
        (4-5) so the more actionable "please select X" messages take
        priority over the passive "reading..." status — the user can go
        pick an output folder while the road is still loading, for
        instance, rather than stare at a status that gives them nothing
        to do.
        """
        parcel_ok = (
            bool(parcel_local_path) if parcel_source_type.get() == "local"
            else bool(parcel_db_table)
        )
        road_selected = (
            bool(road_local_path.get()) if road_source_type.get() == "local"
            else bool(road_db_table.get())
        )
        output_ok = (
            bool(output_local_dir.get()) if output_dest_type.get() == "local"
            else True
        )
        if not parcel_ok:
            return "Please select a Land Parcel source."
        if not road_selected:
            return "Please select a Road Network source."
        if not output_ok:
            return "Please select an Output Destination."
        if parcel_is_reading:
            checking_name = (
                os.path.basename(parcel_local_path) if parcel_source_type.get() == "local"
                else parcel_db_table
            ) or "source"
            return f'Checking "{checking_name}" columns…'
        if road_is_reading:
            checking_name = (
                os.path.basename(road_local_path.get()) if road_source_type.get() == "local"
                else road_db_table.get()
            ) or "source"
            return f'Checking "{checking_name}" columns…'
        if not road_read_ok:
            return "Please select a Road Network source."
        return "Ready to run."

    def _update_run_button_state():
        """
        Called after every relevant input change (parcel/road/output
        selection, road read start/finish/failure). Uses Tkinter's real
        state="disabled"/"normal", with explicit colors for both states
        (Tkinter does NOT automatically gray out a classic tk.Button's
        background when disabled — only `disabledforeground` gets a
        built-in default, and it doesn't coordinate with a custom `bg`,
        which is what produced the dark-text-on-dark-green-background
        readability problem before this fix).

        Cursor is also toggled explicitly here: confirmed empirically
        that Tkinter does NOT suppress a widget's assigned `cursor` just
        because state="disabled" — the last-assigned cursor keeps
        showing regardless, so "no" (Windows "not-allowed" cursor) must
        be set for the disabled state the same deliberate way "hand2"
        is set for the enabled one, rather than assuming one "reverts"
        automatically.

        This also drives the permanent status label under the button,
        so the reason is always visible without needing to hover.
        """
        message = _run_status_message()
        run_status_var.set(message)
        if message == "Ready to run.":
            run_btn.config(state="normal", cursor="hand2",
                            bg="#2e7d32", fg="white")
        else:
            run_btn.config(state="disabled", cursor="no",
                            bg="#e0e0e0", fg="#888888", disabledforeground="#888888")

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

    parcel_btn = tk.Button(parcel_action_row, text="Browse…", width=10, cursor="hand2")
    parcel_btn.pack(side="left", **PAD)

    def _check_parcel_lot_location_worker(sources_list, source_type):
        """
        Runs on a background thread. Reads each currently selected Land
        Parcel source and checks for an existing column matching
        "cama_lot_location" (case-insensitive exact match) — this tool
        is about to write its own Inner/Road/Corner Lot classification
        into that column, and the on_run() confirmation dialog below
        lets the user confirm before it does. Reuses
        _read_road_layer_worker (defined below) — despite the name,
        it's a generic file/DB reader, not road-specific, and never
        touches any Tkinter widget/variable.

        CAMA_ prefix rollout note: this only matches "cama_lot_location"
        (case-insensitive), not the old, unprefixed "lot_location". Files
        produced before the CAMA_ prefix rollout will not be flagged
        here — accepted per project decision, since this tool has not
        yet been run against any pre-rollout output.

        Returns a list of (path_or_table, existing_col_name) tuples on a
        SUCCESSFUL read/check — one entry only for sources where a
        conflicting column was actually found; an empty list means the
        check succeeded and found no conflict. Returns None if ANY
        source failed to read -- this is a REQUIRED distinction, not
        cosmetic: an empty list means "verified, no conflict", while
        None means "could not verify at all". Silently treating a read
        failure as "no conflict" (returning [] either way) would let
        Run proceed as if this safety check had actually passed, when
        in fact it never ran at all -- exactly the same class of risk
        this refactor already eliminated for stale caching (see
        group-05-cache-removal-analysis.md). A previous version of this
        function treated read failure as skip-only/non-blocking, relying
        on "the real read, at Run time, will surface it anyway" -- no
        longer safe to assume now that the check runs at selection time,
        potentially long before Run is ever clicked (see the timeout/
        failure-handling design notes for this change).

        existing_col_name preserves the exact casing found in the
        source (e.g. a column literally named "caMA_Lot_locaTION" is
        returned as-is, not normalized), so the confirmation dialog and
        the eventual write-back both show/use the real casing. The raw
        path/table (not a display-friendly basename) is returned here so
        it can be used directly as a lookup key by run_processing()
        later — the basename is derived separately, only where actually
        needed for display (see on_run() below).
        """
        conflicts = []
        for path_or_table in sources_list:
            gdf, error = _read_road_layer_worker(source_type, path_or_table)
            if error is not None or gdf is None:
                print(f"⚠️ Could not read parcel layer to check for an "
                      f"existing CAMA_LOT_LOCATION column: {path_or_table}: {error}")
                return None
            found = detect_existing_output_columns(gdf, ("CAMA_LOT_LOCATION",))
            existing_col = found.get("CAMA_LOT_LOCATION")
            if existing_col:
                conflicts.append((path_or_table, existing_col))
        return conflicts

    def _set_parcel_reading_state(is_reading):
        """
        Toggle GUI responsiveness while the Land Parcel existing-
        CAMA_LOT_LOCATION-column check is in progress. Mirrors
        _set_road_reading_state() below exactly — disables the parcel
        Browse/Select button and the Local/Database radio buttons for
        the duration of the read, preventing a second, concurrent read
        of the same selection.

        The "Reading..." indicator reuses the EXISTING label
        (parcel_lbl) in place -- via whichever StringVar is currently
        bound to it (parcel_files_var for Local, parcel_db_label for
        Database, per _toggle_parcel()'s textvariable swap below) --
        rather than packing/unpacking a separate status widget. This
        replaces an earlier design that used a second, separate label
        (packed/unpacked via pack()/pack_forget()) to show the reading
        message: that caused a real, reported layout-distortion bug --
        every widget below it (Road Network, DTM Source, Output
        Destination, Run button) visibly shifted position each time the
        status label appeared or disappeared, since packing/forgetting
        a widget reflows all subsequently-packed siblings. Reusing the
        existing label in place adds no new row to the layout, so
        nothing below it ever moves. Matches the pattern already used
        correctly by road_width.py / road_frontage.py.
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
        existing-CAMA_LOT_LOCATION-column check: a read that never
        completed within 60 seconds ("timeout"), or one that completed
        with an actual read error ("failure" -- see
        _check_parcel_lot_location_worker()'s docstring on why this is
        signaled as None, not an empty list).

        Captures the failed source's display name BEFORE clearing the
        authority variable (needed for the dialog text below), then
        clears ONLY the authority variable for source_type (the mode
        that was actually being read) -- parcel_local_path if source_type
        is "local", parcel_db_table if "db". The OTHER mode's selection,
        if any, is left completely untouched -- a Land Parcel failure
        must never affect anything about Road Network, and vice versa
        (see _handle_road_check_failure() below for the Road Network
        equivalent -- the two are entirely independent).

        Clearing the authority variable is the entire recovery
        mechanism -- no new "check failed" state is introduced. This
        forces the EXISTING "no source selected -> Run disabled" path
        (_update_run_button_state(), invoked via
        _set_parcel_reading_state(False) below) to handle recovery: the
        display reverts to "No file selected" / "No table selected",
        matching a genuinely-nothing-selected state exactly, and the
        user must select a source again -- rather than leaving a
        visually-selected-but-unverified source that only an internal
        flag distinguishes from a valid one.

        _set_parcel_reading_state(False) is called BEFORE the dialog is
        shown, not after -- messagebox.showerror() is modal and blocks
        here until dismissed, so showing it first would leave the
        "⏳ Reading Land Parcel…" indicator frozen on screen for the
        entire time the dialog is up. Resetting the display and
        re-enabling controls first means the correct "No file/table
        selected" state is already visible in the background the moment
        the dialog appears.
        """
        nonlocal parcel_local_path, parcel_db_table, parcel_existing_lot_location

        if source_type == "local":
            failed_name = (os.path.basename(parcel_local_path)
                           if parcel_local_path else "the selected file")
            parcel_local_path = None
        else:
            failed_name = parcel_db_table if parcel_db_table else "the selected table"
            parcel_db_table = None

        parcel_existing_lot_location = []

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

    def _poll_parcel_lot_location_queue(result_queue, source_type, deadline):
        """
        Runs on the main thread via win.after() polling. Picks up the
        conflict list placed on the queue by the background worker, or
        detects a timeout if 60 seconds have elapsed with no result.

        Ordering matters: the queue is ALWAYS checked before the
        deadline. This callback only ever runs on the single-threaded
        Tkinter main loop -- two poll cycles for the same queue can
        never run concurrently -- so a result that happens to arrive at
        or right around the deadline is still accepted as a genuine
        success; "timeout" only means NO result had arrived by the time
        THIS poll cycle actually ran. No separate synchronization
        mechanism (e.g. a generation counter) is needed to prevent a
        stale/late result from an abandoned prior read from being
        accepted here -- each call to _refresh_parcel_lot_location_check()
        creates its own brand-new, independent queue.Queue() instance
        (see that function below), so an old, timed-out read's result,
        whenever it eventually arrives, lands in a queue object nothing
        is polling anymore and is simply never read by anyone.
        """
        nonlocal parcel_existing_lot_location
        if not win.winfo_exists():
            return
        try:
            conflicts = result_queue.get_nowait()
        except queue.Empty:
            if time.time() >= deadline:
                _handle_parcel_check_failure(source_type, "timeout")
            else:
                win.after(100, lambda: _poll_parcel_lot_location_queue(
                    result_queue, source_type, deadline))
            return

        if conflicts is None:
            # Worker signaled a read failure (see
            # _check_parcel_lot_location_worker()'s docstring) --
            # distinct from an empty list, which means "verified, no
            # conflict".
            _handle_parcel_check_failure(source_type, "failure")
            return

        parcel_existing_lot_location = conflicts
        _set_parcel_reading_state(False)

    def _refresh_parcel_lot_location_check():
        """
        Background-checks every currently selected Land Parcel
        file/table for an existing column that would collide with the
        CAMA_LOT_LOCATION column this tool is about to write. Gives up
        after 60 seconds with no result (see
        _poll_parcel_lot_location_queue()) -- a hung read must not leave
        the tool waiting indefinitely.

        Deliberately does NOT cache the result across calls -- every
        call, whether triggered by a fresh Browse/Select or by toggling
        Local <-> Database, always performs a real read. A cache keyed
        only on "which file/table was selected" cannot detect that the
        file/table's CONTENTS changed externally (e.g. another CAMA
        tool, QGIS, or Global Mapper modifying it) since it was last
        read here -- serving a stale "no conflict" result would defeat
        the purpose of this check. See group-05-cache-removal-
        analysis.md for the full reasoning. What IS still remembered
        across calls is only WHICH file/table is selected per mode
        (parcel_local_path / parcel_db_table) -- a separate concern,
        untouched by this function.
        """
        nonlocal parcel_existing_lot_location
        if parcel_is_reading:
            # A check is already in flight — do not start a second,
            # overlapping one (controls are disabled while reading, but
            # this guard is the actual enforcement).
            return

        source_type = parcel_source_type.get()
        # Single-selection: build a one-element list from the authority
        # variable, or empty list if nothing selected. The early-return
        # on "if not sources:" below is completely unchanged -- only the
        # list construction changes, not when or whether the refresh fires.
        sources = (
            [parcel_local_path] if source_type == "local" and parcel_local_path
            else [parcel_db_table] if source_type == "db" and parcel_db_table
            else []
        )

        if not sources:
            # Nothing selected for this mode — nothing to check.
            parcel_existing_lot_location = []
            _update_run_button_state()
            return

        result_queue = queue.Queue()

        def worker():
            conflicts = _check_parcel_lot_location_worker(sources, source_type)
            result_queue.put(conflicts)

        deadline = time.time() + 60  # see _poll_parcel_lot_location_queue()
        _set_parcel_reading_state(True)
        threading.Thread(target=worker, daemon=True).start()
        win.after(100, lambda: _poll_parcel_lot_location_queue(
            result_queue, source_type, deadline))

    def browse_parcel_files():
        file = filedialog.askopenfilename(filetypes=[
            ("Shapefiles", "*.shp"), ("GeoPackage", "*.gpkg"), ("All", "*.*")])
        # Cancel returns "" -- do not assign, preserving previous selection.
        if file:
            nonlocal parcel_local_path
            parcel_local_path = file
            parcel_files_var.set(os.path.basename(file))
            # Always checks fresh -- see _refresh_parcel_lot_location_check()
            # docstring: no result is ever cached across calls.
            _refresh_parcel_lot_location_check()
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

        def _on_parcel_tables_selected(sel):
            # Only called on confirmed selection -- Cancel never calls
            # on_select, so parcel_db_table retains its previous value.
            if sel:
                nonlocal parcel_db_table
                parcel_db_table = sel[0]
                parcel_db_label.set(sel[0])
                _refresh_parcel_lot_location_check()
            _update_run_button_state()

        _pick_db_tables(win, tables, multi=False, on_select=_on_parcel_tables_selected)

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
        # remembered selection — that's pre-existing behavior, left
        # untouched. Always re-checks fresh for whichever mode is now
        # active -- no cached result is ever restored (see
        # group-05-cache-removal-analysis.md).
        _refresh_parcel_lot_location_check()
        _update_run_button_state()

    # ── SECTION 2: ROAD NETWORK ──────────────────────────────────
    section_label(win, "Road Network Source")

    road_frame = tk.Frame(win)
    road_frame.pack(fill="x", padx=18, pady=2)

    road_radio_row = tk.Frame(road_frame)
    road_radio_row.pack(fill="x")
    road_radio_local = tk.Radiobutton(road_radio_row, text="Local File",
                   variable=road_source_type, value="local",
                   command=lambda: _toggle_road())
    road_radio_local.pack(side="left")
    road_radio_db = tk.Radiobutton(road_radio_row, text="Database Table",
                   variable=road_source_type, value="db",
                   command=lambda: _toggle_road())
    road_radio_db.pack(side="left", padx=(12, 0))

    road_file_var = tk.StringVar(master=win, value="No file selected")
    road_db_var   = tk.StringVar(master=win, value="No table selected")

    road_action_row = tk.Frame(road_frame)
    road_action_row.pack(fill="x", pady=2)

    road_lbl = tk.Label(road_action_row, textvariable=road_file_var,
                        fg="gray", anchor="w", width=42)
    road_lbl.pack(side="left")

    road_btn = tk.Button(road_action_row, text="Browse…", width=10, cursor="hand2")
    road_btn.pack(side="left", **PAD)

    # --- Road-type filter UI (new) -----------------------------------
    # road_filter_frame is only packed when the currently selected road
    # source has a ROAD_TYPE-like column (see _detect_road_type_column).
    # Until then, Section 2 looks and behaves exactly as it did before
    # this feature existed.
    road_filter_frame = tk.Frame(road_frame)

    # road_type_header_row: shared row holding "Filter by Road Type"
    # (left) AND the "Check All" / "Uncheck All" hyperlink-style links
    # (right, grouped in road_type_links_frame) -- no extra row, no
    # extra vertical space. Packed once, unconditionally, at
    # construction: road_filter_frame's own pack()/pack_forget() (see
    # _poll_road_read_queue()/_clear_road_type_filter()) already gates
    # whether this whole section is visible at all, so this row itself
    # never needs its own separate content-gate the way meters_from_
    # school_shop_transport_church.py's other_landmarks_header_row
    # does. road_type_links_frame is NOT gated by this row's own
    # visibility alone -- it has its own independent, narrower pack/
    # pack_forget (checked AND has content -- the exact same condition
    # the checklist itself uses), since "Check All"/"Uncheck All" are
    # only meaningful once the checklist they operate on is actually
    # visible. See _toggle_road_type_checklist() for both toggles.
    road_type_header_row = tk.Frame(road_filter_frame)
    road_type_header_row.pack(fill="x")

    road_type_filter_checkbox = tk.Checkbutton(
        road_type_header_row, text="Filter by Road Type",
        variable=road_type_filter_check_var,
        command=lambda: _toggle_road_type_checklist())
    road_type_filter_checkbox.pack(side="left")

    # road_type_links_frame: groups "Check All" / "|" / "Uncheck All"
    # together and packs the GROUP to the far right edge of the header
    # row (side="right") -- rather than appending them after the
    # checkbox text, which would grow the row (and therefore the
    # window) wider than intended. Hyperlink-style labels: plain
    # tk.Label styled to look clickable (blue, underlined, hand
    # cursor), bound to <Button-1> -- there is no native Tkinter
    # "link" widget. Text is static Title Case in both states (never
    # toggles to reflect current selection) -- "Check All" always
    # checks every ROAD_TYPE value, "Uncheck All" always unchecks
    # every value, regardless of the checklist's current state.
    road_type_links_frame = tk.Frame(road_type_header_row)

    check_all_road_type_link = tk.Label(
        road_type_links_frame, text="Check All",
        fg="#1a73e8", cursor="hand2", font=("Segoe UI", 8, "underline"))
    check_all_road_type_link.pack(side="left")
    check_all_road_type_link.bind("<Button-1>", lambda e: _check_all_road_types())

    road_type_links_separator = tk.Label(
        road_type_links_frame, text=" | ", fg="gray", font=("Segoe UI", 8))
    road_type_links_separator.pack(side="left")

    uncheck_all_road_type_link = tk.Label(
        road_type_links_frame, text="Uncheck All",
        fg="#1a73e8", cursor="hand2", font=("Segoe UI", 8, "underline"))
    uncheck_all_road_type_link.pack(side="left")
    uncheck_all_road_type_link.bind("<Button-1>", lambda e: _uncheck_all_road_types())

    # Content-adaptive height with a vertical scrollbar ONLY once more
    # than 8 distinct ROAD_TYPE values are found -- matching
    # meters_from_school_shop_transport_church.py's own
    # OTHER_LANDMARKS_MAX_ITEMS_BEFORE_VSCROLL threshold (the reference
    # implementation this Canvas/Scrollbar/mousewheel/bbox architecture
    # is ported from), for consistency across tools rather than the 7
    # originally proposed for this task. A horizontal scrollbar appears
    # only when a label is wider than the box -- never truncated or
    # wrapped, only ever scrolled into view.
    ROAD_TYPE_MAX_ITEMS_BEFORE_VSCROLL = 8
    # Left indent used when packing road_type_checklist_outer below
    # (visually nests the checklist under the "Filter by Road Type"
    # checkbox -- matches this checklist's own pre-existing
    # padx=(20, 0)). Named here so _resize_road_type_checklist_box() can
    # subtract this exact same value from its available-width budget --
    # see that function's own docstring for why omitting it would let
    # the checklist's canvas+vertical-scrollbar combined width extend
    # past road_action_row's own right edge.
    ROAD_TYPE_CHECKLIST_LEFT_INDENT = 20

    # Holds one Checkbutton per unique ROAD_TYPE value found in the
    # currently selected road layer. Rebuilt from scratch on every new
    # successful read (see _poll_road_read_queue). Only packed while
    # road_type_filter_check_var is True. road_type_checklist_container
    # (the actual Checkbutton parent) lives INSIDE
    # road_type_checklist_canvas, which itself lives inside
    # road_type_checklist_outer alongside the vertical/horizontal
    # scrollbars -- road_type_checklist_outer is what
    # _toggle_road_type_checklist()/_clear_road_type_filter() actually
    # pack()/pack_forget() now; road_type_checklist_container's own
    # pack()/pack_forget() calls from before this fix are gone, since
    # its visibility is now entirely governed by its parent outer
    # frame's visibility.
    road_type_checklist_outer = tk.Frame(road_filter_frame)
    road_type_checklist_canvas = tk.Canvas(
        road_type_checklist_outer, highlightthickness=0, bd=0)
    road_type_vscroll = tk.Scrollbar(
        road_type_checklist_outer, orient="vertical",
        command=road_type_checklist_canvas.yview)
    road_type_hscroll = tk.Scrollbar(
        road_type_checklist_outer, orient="horizontal",
        command=road_type_checklist_canvas.xview)
    road_type_checklist_canvas.configure(
        yscrollcommand=road_type_vscroll.set,
        xscrollcommand=road_type_hscroll.set)
    road_type_checklist_canvas.pack(side="left", fill="both", expand=True)
    # Both scrollbars packed/unpacked dynamically by
    # _resize_road_type_checklist_box() below -- only shown when content
    # actually exceeds the box in that direction.

    road_type_checklist_container = tk.Frame(road_type_checklist_canvas)
    _road_type_canvas_window = road_type_checklist_canvas.create_window(
        (0, 0), window=road_type_checklist_container, anchor="nw")

    def _on_road_type_content_configure(_event=None):
        road_type_checklist_canvas.configure(
            scrollregion=road_type_checklist_canvas.bbox("all"))
    road_type_checklist_container.bind(
        "<Configure>", _on_road_type_content_configure)

    def _on_road_type_mousewheel(event):
        road_type_checklist_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    road_type_checklist_canvas.bind(
        "<Enter>", lambda e: road_type_checklist_canvas.bind_all(
            "<MouseWheel>", _on_road_type_mousewheel))
    road_type_checklist_canvas.bind(
        "<Leave>", lambda e: road_type_checklist_canvas.unbind_all("<MouseWheel>"))

    def _resize_road_type_checklist_box():
        """
        Recomputes road_type_checklist_canvas's own height/width handling
        to fit its current content. Ported from
        meters_from_school_shop_transport_church.py's
        _resize_other_landmarks_checklist_box() -- see that function's
        own docstring for the full rationale; only the widget names and
        the reference row (road_action_row instead of poi_action_row,
        re-verified against this file's own actual widget tree -- a
        Label(width=42) + a Browse button, same shape) differ here.

        Vertical scrollbar trigger is an ITEM COUNT (> 8 distinct
        ROAD_TYPE values), not a fixed-pixel cap -- the per-row pixel
        height is measured from the container's actual current content
        (content_height / n_items) so the 8-item cap is translated into
        an accurate pixel height regardless of font/theme.

        Horizontal overflow handling: the canvas's own displayed WIDTH
        is explicitly pinned here to a FIXED value on every call --
        derived from road_action_row's own already-established requested
        width, MINUS both the vertical scrollbar's width (whenever
        shown) AND ROAD_TYPE_CHECKLIST_LEFT_INDENT (the same left indent
        already applied when road_type_checklist_outer itself is packed
        below). Pinning the canvas's width here means overflow is
        handled ENTIRELY by the horizontal scrollbar appearing -- this
        configuration window itself only ever grows taller (to fit new
        checklist rows, up to the 8-item cap), never wider.
        """
        road_type_checklist_container.update_idletasks()
        n_items = len(road_type_value_vars)
        content_height = road_type_checklist_container.winfo_reqheight()
        content_width = road_type_checklist_container.winfo_reqwidth()

        show_vscroll = n_items > ROAD_TYPE_MAX_ITEMS_BEFORE_VSCROLL and n_items > 0
        vscroll_width = road_type_vscroll.winfo_reqwidth() if show_vscroll else 0
        fixed_row_width = road_action_row.winfo_reqwidth()
        canvas_width = max(
            fixed_row_width - vscroll_width - ROAD_TYPE_CHECKLIST_LEFT_INDENT, 1)
        road_type_checklist_canvas.configure(width=canvas_width)

        if n_items <= ROAD_TYPE_MAX_ITEMS_BEFORE_VSCROLL or n_items == 0:
            road_type_checklist_canvas.configure(height=content_height)
            road_type_vscroll.pack_forget()
        else:
            row_height = content_height / n_items
            capped_height = int(round(row_height * ROAD_TYPE_MAX_ITEMS_BEFORE_VSCROLL))
            road_type_checklist_canvas.configure(height=capped_height)
            road_type_vscroll.pack(side="right", fill="y")

        if content_width > canvas_width:
            road_type_checklist_canvas.itemconfig(_road_type_canvas_window, width=content_width)
            road_type_hscroll.pack(side="bottom", fill="x")
        else:
            road_type_checklist_canvas.itemconfig(_road_type_canvas_window, width=canvas_width)
            road_type_hscroll.pack_forget()

    def _reflow_window():
        """
        Ported from meters_from_school_shop_transport_church.py's own
        _reflow_window() (itself adapted from road_width.py's) -- forces
        ONE clean, complete window repaint after this checklist's
        visibility/size changes, instead of an incremental resize that
        can leave a stale/unpainted region behind. Measures
        win.winfo_reqwidth()/reqheight() after update_idletasks() and
        explicitly sets minsize/maxsize/geometry to that exact value.

        Adaptation note -- this window is NOT single-dynamic-section the
        way meters_from_school_shop_transport_church.py's is (see that
        tool's own _reflow_window() docstring, which explicitly relies
        on having exactly one dynamic-content section). This window ALSO
        has road_filter_frame's own show/hide (this checklist's direct
        parent -- see _poll_road_read_queue()'s road_filter_frame.pack()
        call, now reflowed immediately after it -- a prior version of
        this note incorrectly claimed this was already the case, which
        it wasn't; that gap produced a real regression, since fixed:
        toggling Road Network source when a road source with a
        ROAD_TYPE column was already selected would lock the window at
        its smaller size via _clear_road_type_filter()'s own reflow,
        then a subsequent fresh read re-showing road_filter_frame here
        had nowhere to grow into, clipping the window's bottom content)
        and, separately, Section 3 Output Destination's out_btn
        pack()/pack_forget() toggle (_toggle_output(), still genuinely
        untouched -- out of scope for this task per the governing
        instructions' explicit restriction to
        "_toggle_road_type_checklist() and its surrounding widget
        construction only"). Once this function has pinned win's
        minsize/maxsize from a checklist-triggered call, a LATER
        Output-section toggle is not itself reflowed and could still be
        visually clipped or leave dead space against that pinned size --
        a real, disclosed residual risk of this window having more than
        one dynamic-content section, not something this task's scope
        permits fixing without touching Section 3's own code.
        """
        win.update_idletasks()
        req_w = win.winfo_reqwidth()
        req_h = win.winfo_reqheight()
        win.minsize(req_w, req_h)
        win.maxsize(req_w, req_h)
        win.geometry(f"{req_w}x{req_h}")

    def _read_road_layer_worker(source_type, path_or_table):
        """
        Runs on a background thread. Reads the road layer for the given
        selection and returns (gdf, error) — never touches any Tkinter
        widget or variable, since Tkinter is not thread-safe. The caller
        places the result on a queue for the main thread to pick up.
        """
        try:
            if source_type == "local":
                gdf = gpd.read_file(path_or_table)
            else:
                creds = load_db_credentials()
                if not creds:
                    return None, "Could not load DB credentials."
                engine = create_engine(
                    f"postgresql://{creds['username']}:{creds['password']}@"
                    f"{creds['host']}:{creds['port']}/{creds['database']}"
                )
                gdf = read_postgis_clean(path_or_table, engine, creds["schema"])
            return gdf, None
        except Exception as e:
            return None, str(e)

    def _set_road_reading_state(is_reading):
        """
        Toggle GUI responsiveness while a road-layer read is in progress.

        Disables the road Browse/Select button and the Local/Database
        radio buttons for the duration of the read. This prevents the
        user from starting a second, concurrent read — it removes the
        only realistic concurrent-read scenario in this workflow (the
        user re-triggering a read through this UI), not every
        conceivable race; the window itself (e.g. its close button)
        is intentionally left interactive.

        The "Reading..." indicator reuses the EXISTING label (road_lbl)
        in place -- via whichever display StringVar is currently bound
        to it (road_file_var for Local, road_db_var for Database, per
        _toggle_road()'s textvariable swap below) -- rather than
        packing/unpacking a separate status widget. Matches the
        corrected pattern already applied to the Land Parcel section's
        _set_parcel_reading_state() above -- see that function's
        docstring for the full rationale on why the earlier two-widget
        design caused a real, reported layout-distortion bug (every
        widget below it visibly shifting position each time the status
        label appeared/disappeared).
        """
        nonlocal road_is_reading
        road_is_reading = is_reading
        if is_reading:
            if road_source_type.get() == "local":
                road_file_var.set("⏳ Reading road network…")
            else:
                road_db_var.set("⏳ Reading road network…")
            road_lbl.config(fg="#b36b00")
            road_btn.config(state="disabled")
            road_radio_local.config(state="disabled")
            road_radio_db.config(state="disabled")
        else:
            # Restore from authority StringVars (road_local_path /
            # road_db_table) -- never from the display StringVar's
            # mid-read state -- same pattern _toggle_road() uses below.
            if road_source_type.get() == "local":
                road_file_var.set(
                    os.path.basename(road_local_path.get()) if road_local_path.get()
                    else "No file selected"
                )
            else:
                road_db_var.set(
                    road_db_table.get() if road_db_table.get()
                    else "No table selected"
                )
            road_lbl.config(fg="gray")
            road_btn.config(state="normal")
            road_radio_local.config(state="normal")
            road_radio_db.config(state="normal")
        _update_run_button_state()

    def _toggle_road_type_checklist():
        """
        Shows/hides the Road Type checklist AND, together with it, the
        "Check All" / "Uncheck All" links group -- both gated on the
        exact same condition (road_type_filter_check_var checked; the
        checklist itself is meaningless to act on while hidden).

        road_type_header_row WIDTH: pinned every call, via
        pack_propagate(False), to road_action_row's own established
        width (the same reference _resize_road_type_checklist_box()
        already uses for the canvas). This row now holds THREE pieces
        of text on one line when checked (checkbox label + the Check
        All / Uncheck All group) -- without this pin, that combined
        text's natural/requested width can exceed the window's
        established width and grow the whole configuration window.
        Pinning here means the row (and therefore the window) stays at
        a constant width regardless of link content; road_type_links_
        frame is packed side="right" within this fixed-width row so it
        sits flush against the far right edge, with the checkbox flush
        against the far left. Harmless to also pin while unchecked
        (the checkbox alone is already narrower than road_action_row).
        """
        win.update_idletasks()
        fixed_row_width = road_action_row.winfo_reqwidth()
        row_height = road_type_filter_checkbox.winfo_reqheight()
        road_type_header_row.configure(width=fixed_row_width, height=row_height)
        road_type_header_row.pack_propagate(False)

        if road_type_filter_check_var.get():
            for display_text, (real_value, var) in road_type_value_vars.items():
                tk.Checkbutton(road_type_checklist_container, text=display_text,
                                variable=var).pack(anchor="w")
            road_type_links_frame.pack(side="right")
            road_type_checklist_outer.pack(
                fill="x", padx=(ROAD_TYPE_CHECKLIST_LEFT_INDENT, 0))
            win.update_idletasks()
            _resize_road_type_checklist_box()
        else:
            for child in road_type_checklist_container.winfo_children():
                child.destroy()
            road_type_links_frame.pack_forget()
            road_type_checklist_outer.pack_forget()
        _reflow_window()

    def _check_all_road_types():
        """
        Sets every discovered ROAD_TYPE value's BooleanVar to True --
        iterates road_type_value_vars directly (the full dictionary
        populated by _poll_road_read_queue() for every distinct value
        this read found), never the Tkinter Checkbutton widgets
        currently rendered inside the Canvas viewport -- so every value
        is checked regardless of whether its row is currently scrolled
        into view. Each Checkbutton is bound to its own BooleanVar, so
        this automatically and correctly updates every widget's
        displayed check-state too, including off-screen ones, the
        moment they're scrolled into view. Mirrors meters_from_school_
        shop_transport_church.py's _check_all_other_landmarks() exactly.

        Checking/unchecking individual ROAD_TYPE values never gates the
        Run button (see _run_status_message(), which does not read
        road_type_value_vars at all) -- so, unlike that reference
        function, there is no _update_run_button_state() call needed
        here.
        """
        for real_value, var in road_type_value_vars.values():
            var.set(True)

    def _uncheck_all_road_types():
        """Mirror of _check_all_road_types() -- sets every discovered
        value's BooleanVar to False, same off-screen-safe approach
        (iterates the variable dict directly, never the currently-
        rendered widgets)."""
        for real_value, var in road_type_value_vars.values():
            var.set(False)

    def _clear_road_type_filter():
        """
        Reset the road-type filter UI and its backing cache/state to
        "no valid source selected". Called whenever the previously
        selected road source is no longer trustworthy: switching
        Local <-> Database, or immediately before starting a new read
        for a newly selected file/table.

        The cache is cleared here, before any new read starts, so a
        stale road layer from a previous selection can never be reused
        by run_processing() — whether or not the new read succeeds.
        """
        nonlocal road_read_ok
        global _road_gdf_cache

        road_type_filter_check_var.set(False)
        for child in road_type_checklist_container.winfo_children():
            child.destroy()
        road_type_value_vars.clear()
        road_type_links_frame.pack_forget()
        road_type_checklist_outer.pack_forget()
        # Only reflow if road_filter_frame was actually visible before
        # this call -- this function also runs once, unconditionally,
        # during open_main_window()'s own initial-setup sync
        # (_toggle_road(), before any road source has ever been
        # browsed), when nothing is visible yet and there is nothing to
        # shrink back from. Reflowing THEN would prematurely lock the
        # window at its smallest possible height, before
        # road_filter_frame's own later, out-of-scope pack() call (see
        # _poll_road_read_queue()) ever gets a chance to be accounted
        # for -- clipping the window's bottom content (the Run button's
        # status line) the first time a road source with a ROAD_TYPE
        # column is actually loaded. This guard is what keeps that
        # initial no-op call from locking anything.
        was_visible = road_filter_frame.winfo_ismapped()
        road_filter_frame.pack_forget()
        if was_visible:
            _reflow_window()

        road_read_ok = False
        _road_gdf_cache = {"key": None, "gdf": None}
        _update_run_button_state()

    def _handle_road_check_failure(source_type, reason):
        """
        Shared cleanup for both outcomes of a FAILED Road Network read:
        one that never completed within 60 seconds ("timeout"), or one
        that completed with an actual read error ("failure").

        Entirely independent from _handle_parcel_check_failure() above
        -- no shared queue, worker, deadline, or state between the two.
        A Road Network failure must never clear or affect the Land
        Parcel selection, and vice versa.

        Captures the failed source's display name BEFORE clearing the
        authority variable (needed for the dialog text below), then
        clears ONLY road_local_path or road_db_table (whichever
        source_type was actually being read) -- road_local_path/
        road_db_table are tk.StringVar here (unlike Land Parcel's plain-
        variable parcel_local_path/parcel_db_table), so clearing means
        .set("") rather than assigning None. Reuses the EXISTING
        _clear_road_type_filter() to reset the checklist/road_read_ok/
        cache state -- this already existed for the Local<->Database
        toggle case; a failed/timed-out read is conceptually the same
        "this source is no longer trustworthy" situation, so no new
        reset logic is introduced here.

        _set_road_reading_state(False) is called BEFORE the dialog is
        shown, not after -- same reasoning as
        _handle_parcel_check_failure() above: messagebox.showerror() is
        modal and blocks here until dismissed, so the "⏳ Reading road
        network…" indicator would otherwise stay frozen on screen for
        the entire time the dialog is up.
        """
        if source_type == "local":
            failed_name = (os.path.basename(road_local_path.get())
                           if road_local_path.get() else "the selected file")
            road_local_path.set("")
        else:
            failed_name = road_db_table.get() if road_db_table.get() else "the selected table"
            road_db_table.set("")

        _clear_road_type_filter()

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

        _set_road_reading_state(False)
        messagebox.showerror(title, message, parent=win)

    def _poll_road_read_queue(result_queue, source_key, deadline):
        """
        Runs on the main thread via win.after() polling. Picks up the
        result placed on the queue by the background worker and applies
        it to the GUI — this is the only place the worker's result is
        allowed to touch Tkinter widgets/variables. Detects a timeout if
        60 seconds have elapsed with no result (see
        _handle_road_check_failure()).

        The winfo_exists() guard below only protects this callback from
        running after the window has been destroyed (e.g. the user
        closed the window while a read was still in progress) — it does
        not stop or cancel the background thread itself, which simply
        finishes and has its result discarded here.

        Ordering matters: the queue is ALWAYS checked before the
        deadline -- same reasoning as
        _poll_parcel_lot_location_queue() above; no generation counter
        is needed since each call to _refresh_road_type_filter() creates
        its own fresh, independent queue.Queue() instance.
        """
        if not win.winfo_exists():
            return

        source_type, _path_or_table = source_key

        try:
            gdf, error = result_queue.get_nowait()
        except queue.Empty:
            if time.time() >= deadline:
                _handle_road_check_failure(source_type, "timeout")
            else:
                win.after(100, lambda: _poll_road_read_queue(
                    result_queue, source_key, deadline))
            return

        nonlocal road_read_ok
        global _road_gdf_cache

        if error is not None or gdf is None:
            print(f"⚠️ Could not read road layer: {error}")
            _handle_road_check_failure(source_type, "failure")
            return

        # Success — unconditionally replace the cache with the new layer.
        # A failed read (handled above) never falls back to a previous
        # cache entry; the cache is either the current selection's data
        # or empty, never stale data from an earlier selection.
        _road_gdf_cache = {"key": source_key, "gdf": gdf}
        road_read_ok = True

        col = _detect_road_type_column(gdf)
        if col:
            # Three distinct data states, never merged into one bucket:
            #   - NULL/NaN            -> "(NULL / No Road Type)"
            #   - literal empty string -> "(Empty String)"
            #   - any other literal string (including "-") -> shown as-is,
            #     not special-cased — "-" is just an ordinary value here.
            # road_type_value_vars maps the DISPLAY label (with feature
            # count) to (real_value, BooleanVar). real_value is the actual
            # underlying value (np.nan, "", or the literal string) used
            # for filtering — process_lot_location()'s existing
            # `.isin(excluded_road_types)` call already matches np.nan and
            # "" correctly when they're literally present in that list, so
            # no change is needed there; only the display layer changes.
            counts = {}  # display_label -> [real_value, count]
            for v in gdf[col]:
                if pd.isna(v):
                    real_value, label = np.nan, "(NULL / No Road Type)"
                elif str(v) == "":
                    real_value, label = "", "(Empty String)"
                else:
                    real_value, label = str(v), str(v)
                if label not in counts:
                    counts[label] = [real_value, 0]
                counts[label][1] += 1

            if len(counts) > 1:
                # Auto-hide: only one distinct value in the whole column
                # means there's nothing meaningful to filter — leave
                # road_filter_frame unpacked, Section 2 stays unchanged.
                for label in sorted(counts.keys()):
                    real_value, count = counts[label]
                    display_text = f"{label} ({count})"
                    road_type_value_vars[display_text] = (
                        real_value, tk.BooleanVar(master=win, value=True)
                    )
                road_filter_frame.pack(fill="x", pady=(4, 2))
                # D-Cancel/Fix 2 regression fix: this call was never
                # actually reflowed, despite _reflow_window()'s own
                # docstring incorrectly claiming it was ("reflowed
                # alongside it below" -- that phrase described an
                # intention that was never implemented). Toggling Road
                # Network source (Local<->DB) when a road source with a
                # ROAD_TYPE column was already selected fires
                # _clear_road_type_filter(), which locks win's
                # minsize/maxsize to the smaller (road_filter_frame
                # hidden) size. Without this call, a subsequent fresh
                # read that re-shows road_filter_frame here has nowhere
                # to grow into against that already-locked size, and the
                # window's bottom content (the status message below Run
                # Processing) gets clipped -- same root cause as the
                # earlier scrollbar-cutoff regression, different trigger
                # path (toggle-then-reselect vs. initial setup).
                _reflow_window()
            # else: column exists but has only one distinct value (or is
            # entirely NULL/empty) — nothing to filter on.
        # else: no ROAD_TYPE-like column — leave road_filter_frame
        # unpacked, Section 2 behaves exactly as before this feature.

        _set_road_reading_state(False)

    def _refresh_road_type_filter():
        """
        Entry point called whenever the user finishes selecting a road
        source (new file chosen, or new DB table chosen). Clears any
        prior filter state/cache, then kicks off a background read to
        detect the ROAD_TYPE column and populate the checklist, without
        blocking the GUI. Gives up after 60 seconds with no result (see
        _poll_road_read_queue()) -- a hung read must not leave the tool
        waiting indefinitely.
        """
        _clear_road_type_filter()

        source_type = road_source_type.get()
        path_or_table = (road_local_path.get() if source_type == "local"
                          else road_db_table.get())
        if not path_or_table:
            return  # nothing selected yet, nothing to read

        source_key = (source_type, path_or_table)
        result_queue = queue.Queue()

        def worker():
            gdf, error = _read_road_layer_worker(source_type, path_or_table)
            result_queue.put((gdf, error))

        deadline = time.time() + 60  # see _poll_road_read_queue()
        _set_road_reading_state(True)
        threading.Thread(target=worker, daemon=True).start()
        win.after(100, lambda: _poll_road_read_queue(result_queue, source_key, deadline))

    def browse_road_file():
        f = filedialog.askopenfilename(filetypes=[
            ("Shapefiles", "*.shp"), ("GeoPackage", "*.gpkg"), ("All", "*.*")])
        if f:
            road_local_path.set(f)
            road_file_var.set(os.path.basename(f))
            _refresh_road_type_filter()
        else:
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

        def _on_road_table_selected(sel):
            if sel:
                road_db_table.set(sel[0])
                road_db_var.set(sel[0])
                _refresh_road_type_filter()
            else:
                _update_run_button_state()

        _pick_db_tables(win, tables, multi=False, on_select=_on_road_table_selected)

    def _toggle_road():
        # Switching Local <-> Database does NOT clear the other type's
        # remembered selection — that per-type memory is pre-existing
        # behavior in this file and is intentionally left untouched.
        if road_source_type.get() == "local":
            road_lbl.config(textvariable=road_file_var)
            road_btn.config(text="Browse…", command=browse_road_file)
        else:
            road_lbl.config(textvariable=road_db_var)
            road_btn.config(text="Select…", command=browse_road_db)

        # The road-type filter cache/UI, however, always belongs to
        # whichever source is currently active. If the newly active type
        # already has a remembered selection, re-read it in the
        # background so the cache and Run-button gating stay consistent
        # with what's shown in the label. Otherwise just hide/reset.
        has_selection = (road_local_path.get() if road_source_type.get() == "local"
                          else road_db_table.get())
        if has_selection:
            _refresh_road_type_filter()
        else:
            _clear_road_type_filter()

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

    out_btn = tk.Button(out_action_row, text="Browse…", width=10, cursor="hand2")
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
        background column-conflict result (PRIORITY 1), runs the local
        output-file conflict check (PRIORITY 2), and DB-output table
        resolution (PRIORITY 3) -- each able to cancel the whole run --
        then destroys this window and hands off to run_processing().
        Sets the module-level barangay_source, road_source, output_mode,
        road_type_excluded_values, and lot_location_column_overrides
        globals on success.
        """
        global barangay_source, road_source, output_mode, road_type_excluded_values, lot_location_column_overrides

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

        # Defensive fallback: the Run button is normally disabled while
        # parcel_is_reading (see _update_run_button_state), but this
        # check is kept in case that state is ever reached inconsistently.
        if parcel_is_reading:
            messagebox.showerror("Missing Input",
                "Still checking the selected Land Parcel source(s). "
                "Please wait for the status line to finish before running.")
            return

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

        # Defensive fallback: the Run button is normally disabled until
        # road_read_ok is True (see _update_run_button_state), but this
        # check is kept in case that state is ever reached inconsistently.
        if not road_read_ok:
            messagebox.showerror("Missing Input",
                "Please wait for the road network to finish loading "
                "(or re-select a valid road source) before running.")
            return

        if output_dest_type.get() == "local":
            if not output_local_dir.get():
                messagebox.showerror("Missing Input",
                    "Please select an output folder.")
                return
            output_mode = ("local", output_local_dir.get())
        else:
            output_mode = ("db", None)


        # PRIORITY 1: column conflict check — warn if the selected Land
        # Parcel source already has a CAMA_LOT_LOCATION column. Shown
        # before the file-conflict dialog so the user can decide whether
        # to proceed at all before being asked about filename conflicts.
        # Declining cancels the run entirely; main window stays open.
        if parcel_existing_lot_location:
            lines = "\n\n".join(
                f"'{os.path.basename(path_or_table)}' already has the following column(s):\n"
                f"  • {existing_col}"
                for path_or_table, existing_col in parcel_existing_lot_location
            )
            proceed = messagebox.askyesno(
                "Existing CAMA_LOT_LOCATION column found",
                f"{lines}\n\n"
                "Processing will overwrite the existing column(s) with the "
                "newly computed classification.\n\nProceed?"
            )
            if not proceed:
                return
            # Preserve each source's existing column name/casing exactly
            # -- e.g. a detected "caMA_Lot_locaTION" is written back to
            # "caMA_Lot_locaTION", not a hardcoded "CAMA_LOT_LOCATION" --
            # so no duplicate column is ever created regardless of the
            # existing casing. A source with no entry here (no conflict
            # was found) simply uses the default name in
            # process_lot_location() below.
            lot_location_column_overrides = dict(parcel_existing_lot_location)
        else:
            lot_location_column_overrides = {}

        # PRIORITY 2: file conflict check — warn if an output file with
        # the same name already exists in the chosen output folder.
        # Resolved here on the main thread, before win.destroy(), so the
        # dialog has a live parent. Cancel aborts the run; main window stays open.
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

        road_type_excluded_values = (
            [real_value for display_text, (real_value, var) in road_type_value_vars.items()
             if not var.get()]
            if road_type_filter_check_var.get() else []
        )

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
        #
        # D-Cancel: resolved_outcome is now actually threaded through to
        # run_processing() -- previously it was computed here and
        # immediately discarded (assigned to _resolved_outcome, never
        # read). _write_db_output_safely() needs it to know whether
        # FINAL_SWAP requires a rename-existing-aside-to-backup step.
        # _resolve_creds is also now passed into resolve_db_output_table()
        # itself, for its new D-Cancel orphan-recovery scan.
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

    run_btn = tk.Button(win, text="▶  Run Processing", command=on_run,
              font=("Segoe UI", 10, "bold"),
              relief="flat", padx=16, pady=6)
    run_btn.pack(pady=(4, 4))

    # Permanent status line under the Run button — always visible, no
    # hover required. Says exactly what's missing, or "Ready to run."
    run_status_var = tk.StringVar(master=win, value="")
    run_status_lbl = tk.Label(win, textvariable=run_status_var,
                               font=("Segoe UI", 8), fg="gray")
    run_status_lbl.pack(pady=(0, 12))

    # set initial button commands/state to match default radio state
    _toggle_parcel()
    _toggle_road()
    _toggle_output()
    _update_run_button_state()


# ============================================================
# Progress Event Protocol v9 — Phase 3 migration (lot_location.py)
# ============================================================
# PresentationState, the Presentation Policy, and the Tkinter View are
# no longer defined locally in this file -- they were identical to
# road_frontage.py's copies (confirmed during the shared-framework
# analysis), so both tools now import the same three classes from
# utils/progress_framework.py instead of each keeping its own copy.
# This is a pure extraction: no behavior change, no new abstraction,
# no wrapper/adapter/compatibility layer around the imported classes.
#
#   Worker (worker(), inside run_processing())
#       -> unchanged. Has no knowledge of any class below.
#   Main-thread Message Handler (poll_queue(), inside run_processing())
#       -> unchanged. Still calls progress.update(...) / progress.close()
#          with the exact same signatures as before.
#   ProgressWindow
#       -> stays in this file. Owns the Toplevel window and the widgets
#          that live in it (construction only — see __init__).
#          Delegates the *decision* of what to display to
#          ProgressPresentationPolicy and the *act* of displaying it to
#          TkinterProgressView, both imported from progress_framework.
#
# road_width.py is not part of this migration and is not touched by it
# -- its ProgressWindow/Policy/View stay fully standalone (see
# progress_framework.py's own top-of-file comment for why).
# ============================================================

from utils.progress_framework import (
    PresentationState,
    ProgressPresentationPolicy,
    TkinterProgressView,
    new_cancel_flag,
)


class ProgressWindow:
    """
    Simple progress dialog shown while run_processing() works on a
    background thread. Adapted from road_frontage.py's ProgressWindow
    (the reference implementation) — generic status label + determinate
    progress bar.

    D-Cancel (this rollout): this tool is the second (after
    landmarks_within_meters.py) to opt into progress_framework.py's D1
    Cancel mechanism -- an OPTIONAL cancel_flag passed through to
    TkinterProgressView, wiring the title-bar X as Cancel exactly the
    way landmarks_within_meters.py's own (separate, non-shared)
    progress window already does. This replaces the previous "No
    cancel/stop_flag support by design" note, which described this
    class before this rollout and is no longer accurate. See
    run_processing() for where the cancel_flag actually comes from
    (utils.progress_framework.new_cancel_flag(), one per run) and for
    this tool's actual Cancel checkpoints -- a pre-processing check
    (before process_lot_location() is even called) plus that function's
    own classification-loop checkpoints (mid-classification Cancel, full
    discard -- see process_lot_location()'s own progress-contract
    docstring). Cancel is disabled only once a real (non-cancelled)
    result exists, immediately before the save that follows. A
    ProgressWindow constructed WITHOUT cancel_flag (the pre-existing
    call shape) behaves exactly as before this rollout -- this is still
    an opt-in, not a redesign.

    Progress Event Protocol v9 role: ProgressWindow is the host, not the
    decision-maker. It owns the window and the widgets that live in it
    (all created here, in __init__, exactly as before this migration --
    construction/ownership is not "rendering" and does not move into
    TkinterProgressView). Its public interface (__init__, update,
    close) is otherwise unchanged from before this migration;
    poll_queue() requires no changes beyond passing the new optional
    cancelable argument through. Internally, update() and close()
    delegate to ProgressPresentationPolicy and TkinterProgressView
    (both from progress_framework.py, shared with road_frontage.py)
    instead of deciding/mutating inline.
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
                preserves the exact pre-D-Cancel behavior: no Cancel
                wiring at all.
        """
        self.win = tk.Toplevel(root)
        apply_icon(self.win, "lotlocation.ico")
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
        # Event Protocol v9), shared with road_frontage.py via
        # progress_framework.py. Constructed here, after the widgets
        # they render into already exist, so TkinterProgressView always
        # holds valid widget references for the lifetime of this window.
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
# DB ATOMIC WRITE / CANCEL-SAFE STAGING  (D-Cancel)
# ========================================
# Ported from landmarks_within_meters.py's own D4 section (the pilot for
# this exact mechanism -- staging-table-then-atomic-rename-swap, plus a
# genuine Cancel control, plus crash/orphan recovery on a later run).
# This is the SECOND tool this rollout, and per Section C/G.5 (Rule of
# Three) this is still a deliberate duplication, not a shared utils/
# module -- that decision is deferred until a third tool actually needs
# this same mechanism. Every function below is copied from
# landmarks_within_meters.py's D4 block with only the adaptations this
# file's own context requires (see individual docstrings) -- no design
# changes.
#
# Replaces the single `result.to_postgis(..., if_exists="replace")` call
# this file's worker() previously used with a staging-write / verify /
# atomic-rename-swap sequence. The real destination table is never
# touched until FINAL_SWAP, a single PostgreSQL transaction that either
# fully commits or is fully rolled back by PostgreSQL itself.
#
# Naming: staging/backup identifiers are independently random
# (_gen_id()), never derived from the destination table name -- this
# keeps them well under PostgreSQL's 63-byte identifier limit regardless
# of how long a user-derived destination name is. Human-readable context
# (which destination, which run, when) lives entirely in a
# COMMENT ON TABLE (_comment_cama_table()) -- metadata only, playing no
# role in collision safety, which is the random id alone.
#
# Ownership / crash recovery: each DB-output run acquires a PostgreSQL
# session-level advisory lock on a DEDICATED, non-pooled connection
# (_acquire_run_lock()) for the entire DB-output portion of the run --
# deliberately not a connection borrowed from the SQLAlchemy engine's
# pool, since a pooled connection can be returned and reused while
# PostgreSQL still considers the original session's lock held, which
# would make "lock lifetime == run lifetime" false. On the next
# DB-output run, _scan_orphaned_cama_tables() finds any leftover
# staging/backup table, re-derives the same lock key from the run_id
# recorded in that table's own comment, and treats it as orphaned only
# if the lock is NOT currently held by anyone -- i.e. only if the run
# that created it is provably no longer alive.
#
# THIS FILE'S CANCEL GRANULARITY: as of the mid-classification Cancel
# task, process_lot_location() now has its own bool-return progress
# contract (see that function's own docstring), checked at its
# classification loop's own throttled checkpoints -- explicit False
# only, full discard (returns None) on trigger. This now matches
# landmarks_within_meters.py's own PROCESSING-phase Cancel much more
# closely than the earlier version of this section did (which described
# a single pre-processing-only checkpoint, from before that contract
# existed). What's genuinely still true, and matches
# landmarks_within_meters.py exactly: NOTHING from STAGING_WRITE through
# FINAL_SWAP and the post-swap backup drop -- everything below this
# comment block -- is itself cancelable, for the same reason in both
# tools: gdf.to_postgis() (and the DB-only steps that follow it) is a
# single blocking library call with no hook a background thread could
# check a flag inside of, not a stricter design choice specific to this
# file. See run_processing()'s worker() for the actual Cancel checkpoint
# sequence (pre-processing check, then process_lot_location()'s own
# internal checkpoints, then disabled once a real result exists).


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
    (confirmed: no rename_geometry()/set_geometry() call anywhere in
    this file, and read_postgis_clean() explicitly names it "geometry"),
    so this default matches this file's own convention out of the box.

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
    `result.to_postgis(..., if_exists="replace")` call this file's
    worker() previously used. Implements STAGING_WRITE -> VERIFY ->
    FINAL_SWAP -> (DISCARDING on failure) -> post-swap backup cleanup.
    The real destination table (resolved_table_name) is never touched
    before FINAL_SWAP, and FINAL_SWAP is a single PostgreSQL transaction
    -- either both renames inside it commit, or PostgreSQL rolls back
    both and the destination is exactly as it was before this call. This
    function is only ever called from worker() AFTER the run's single
    Cancel checkpoint has already passed (see this section's own header
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
    apply_icon(win, "lotlocation.ico")
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
    source, BEFORE the worker thread starts -- same "resolve everything
    up front, main thread only" philosophy as ask_overwrite_dialog()
    (see run_processing()). This is what lets the fuzzy-match +
    confirmation flow avoid ever needing a thread-safe dialog
    mechanism: the Land Parcel source is singular (see parcel_local_path
    / parcel_db_table -- single-select architecture), so everything
    needed to resolve the destination table is already known before any
    background processing begins.

    D-Cancel: also runs the crash-orphan recovery scan first, before any
    of its own existing logic -- _scan_orphaned_cama_tables() plus, if
    it finds anything, _prompt_orphaned_cama_tables(). This is the
    natural point for it: it's the one place in this file that already
    runs on the main thread, synchronously, with a live `root` to open a
    dialog on, before any new staging table for THIS run could possibly
    exist to confuse the scan. creds is a new required parameter for
    this reason -- the caller (on_run()) already loads it to determine
    schema before calling this function, so this is a zero-cost addition
    at the call site.

    Two cases:
      - DB-source Land Parcel (barangay_source[0] == "db"): always
        writes back to the exact same table it was read from -- no
        matching, no dialog, matches worker()'s own pre-existing
        src_type handling.
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


# ========================================
# RUN
# ========================================
def run_processing(app_root, overwrite_mode=None, resolved_table_name=None,
                    resolved_outcome=None):
    """
    Runs the full batch (all selected parcel sources) on a background
    thread, reporting progress via a ProgressWindow.

    Threading discipline: the worker thread never touches Tkinter
    directly — it only puts messages on `q`. All widget updates happen
    in poll_queue(), on the main thread, via app_root.after(100,
    poll_queue) — the same pattern already used for the Section 2
    road-type background read in open_main_window().

    app_root is passed in directly from on_run()'s closure (which
    already has it as open_main_window()'s own `root` parameter) rather
    than a module-level `_app_root` global — smaller diff, no new global
    state, since this tool's architecture already makes it available.

    Per-source failure isolation: if one parcel source (file or DB
    table) fails, it's skipped and the rest of the batch continues —
    outputs already written for earlier sources are kept. A summary of
    any skipped sources is shown at the end instead of a raw crash
    aborting the whole batch. This is unchanged, existing behavior for
    actual exceptions.

    D-Cancel: one new_cancel_flag() is created per run and passed to
    ProgressWindow, wiring the title-bar X as Cancel for the lifetime of
    this run's progress window. Deliberately NOT extended with any
    multi-source/"Model A" batch semantics -- barangay_source is always
    a 1-tuple in practice today (single-select GUI; see on_run()), so
    per-run and per-source are the same thing here, and the `sources`
    loop below stays exactly as it already was for actual exceptions.
    Cancel is checked at two kinds of points inside worker(): once
    before process_lot_location() is even called (catches Cancel during
    "Loading road network..."/"Loading {name}"), and then internally, by
    process_lot_location() itself, at its own classification loop's
    throttled checkpoints -- see that function's own progress-contract
    docstring (mid-classification Cancel task) for the exact contract
    and for why a mid-loop Cancel returns None (full discard) rather
    than a partial result. Cancel is disabled only once a real
    (non-cancelled) result exists, immediately before the save that
    follows -- nothing from that point on can itself be interrupted. See
    ProgressWindow's own docstring and the "DB ATOMIC WRITE /
    CANCEL-SAFE STAGING" section above for the full rationale.

    resolved_outcome ("overwritten" | "created" | None): the other half
    of resolve_db_output_table()'s return value (via on_run()), only
    relevant for DB output mode -- passed to _write_db_output_safely()
    so it knows whether FINAL_SWAP needs a rename-existing-aside-to-
    backup step. Previously computed by on_run() and discarded; now
    actually used.
    """
    global barangay_source, road_source, output_mode, road_type_excluded_values, lot_location_column_overrides, _road_gdf_cache
    if not barangay_source or not road_source or not output_mode:
        messagebox.showerror("Error", "Selections incomplete (Barangay, Road, Output required).")
        return

    # resolved_table_name: the DB-output destination table. Resolution
    # responsibility now belongs to on_run() (PRIORITY 3), on the main
    # thread, BEFORE win.destroy() -- see Fix 1. By the time it reaches
    # this function it is treated as an already-validated value: either
    # None (local output, or output_mode[0] != "db") or a confirmed
    # table name (DB output, user already had the chance to cancel in
    # on_run()). No re-resolution or re-validation happens here.
    cancel_flag = new_cancel_flag()
    progress = ProgressWindow(app_root, "Lot Location Progress", cancel_flag=cancel_flag)
    q = queue.Queue()

    def worker():
        """
        Background-thread body: loads the Road Network layer (reusing
        _road_gdf_cache if it matches the currently selected source),
        then for each selected Land Parcel source runs
        process_lot_location() and saves the result (local .gpkg or
        PostGIS via _write_db_output_safely()'s staging/swap sequence),
        with per-source failure isolation -- a failing source is
        skipped and logged, the rest of the batch continues. Posts
        progress/completion/error events onto q for poll_queue() to
        consume on the main thread. Never touches Tkinter widgets
        directly.

        D-Cancel: acquires the DB run lock (only for DB output mode)
        before the sources loop, releases it in a finally: block
        regardless of how this function exits (clean completion,
        Cancel, or an exception), and checks cancel_flag["stop"] directly
        once per source (before process_lot_location() is even called),
        plus indirectly, via progress_cb's return value, at
        process_lot_location()'s own classification-loop checkpoints --
        see run_processing()'s own docstring and process_lot_location()'s
        progress-contract docstring for the full checkpoint sequence.
        """
        lock_conn = None
        try:
            def progress_cb(msg, val=None, maxv=None):
                q.put(("update", msg, val, maxv, None))
                # D-Cancel (mid-classification): a real bool every call,
                # never None -- process_lot_location()'s classification
                # loop only stops on an explicit False (see that
                # function's own progress-contract docstring), so this
                # must never accidentally return None itself.
                return not cancel_flag["stop"]

            creds = load_db_credentials()
            schema = creds["schema"]
            engine = create_engine(
                f"postgresql://{creds['username']}:{creds['password']}@"
                f"{creds['host']}:{creds['port']}/{creds['database']}"
            )

            # D-Cancel: one run_id/run_started_at for the whole run --
            # cheap to always generate, even for local output mode,
            # which doesn't use it. The advisory lock itself is only
            # ever acquired for DB output mode, since it exists purely
            # to let a FUTURE run's _scan_orphaned_cama_tables() tell
            # this run's staging/backup artifacts apart from a
            # genuinely abandoned one.
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
            # Reuse the road layer already read by the Section 2 filter UI
            # when it matches the currently selected source (avoids a
            # second full file/DB read). Falls back to a fresh read if the
            # cache is missing or doesn't match.
            road_cache_key = (road_source[0], road_source[1][0])
            if _road_gdf_cache.get("key") == road_cache_key and _road_gdf_cache.get("gdf") is not None:
                road_gdf = _road_gdf_cache["gdf"]
                print("ℹ️  Reusing cached road network (already read during source selection).")
            else:
                road_gdf = gpd.read_file(road_source[1][0]) if road_source[0] == "local" \
                    else read_postgis_clean(road_source[1][0], engine, schema)

            excluded_road_types = road_type_excluded_values or []

            sources = ([("local", p) for p in barangay_source[1]] if barangay_source[0] == "local"
                       else [("db", t) for t in barangay_source[1]])

            skipped = []
            cancelled = False
            for src_type, src in sources:
                name = os.path.basename(src) if src_type == "local" else src
                try:
                    q.put(("update", f"Loading {name}", None, None, None))
                    if src_type == "local":
                        brgy_gdf = gpd.read_file(src)
                        out_base = os.path.splitext(name)[0]
                    else:
                        brgy_gdf = read_postgis_clean(src, engine, schema)
                        out_base = name

                    # D-Cancel: pre-processing checkpoint. Catches a
                    # Cancel clicked during "Loading road network..."/
                    # "Loading {name}" -- i.e. before process_lot_location()
                    # is even called, and before that function's own
                    # first classification-loop checkpoint (i == 1,
                    # reached quickly but not instantly after
                    # reprojection/cleaning/spatial-join) could fire on
                    # its own. NOT the only checkpoint in this run
                    # anymore -- see the mid-classification check right
                    # after the process_lot_location() call below, and
                    # that function's own progress-contract docstring.
                    if cancel_flag["stop"]:
                        cancelled = True
                        break

                    # D-Cancel: Cancel STAYS enabled through
                    # process_lot_location() itself now -- this is a
                    # deliberate change from the prior task, where this
                    # message disabled Cancel here (cancelable=False)
                    # because process_lot_location() had no early-stop
                    # contract yet. It now honors Cancel internally, via
                    # progress_cb's return value, at its own throttled
                    # classification-loop checkpoints -- see that
                    # function's own docstring for the exact contract
                    # (explicit False only; the other four progress()
                    # call sites inside it don't check the return value
                    # and can't be interrupted mid-call).
                    q.put(("update", f"Processing {name}", None, None, True))

                    # NOTE: fix_geometry() is applied inside
                    # process_lot_location() only — scoped to spatial-test
                    # operations, never mutating brgy_gdf's geometry
                    # column, so the exported output stays faithful to
                    # the original source shapes.
                    # output_column_name: preserves the exact existing
                    # column name/casing this source's parcel layer
                    # already had (if the user confirmed overwriting one
                    # at Run time — see on_run()'s confirmation dialog),
                    # keyed by the same raw path/table string used to
                    # iterate `sources` above. A source with no entry
                    # here falls back to process_lot_location()'s own
                    # default ("CAMA_LOT_LOCATION").
                    result = process_lot_location(
                        brgy_gdf, road_gdf, name,
                        excluded_road_types=excluded_road_types,
                        progress=progress_cb,
                        output_column_name=lot_location_column_overrides.get(src, "CAMA_LOT_LOCATION")
                    )

                    if result is None:
                        # D-Cancel (mid-classification): progress_cb
                        # returned False at one of the classification
                        # loop's own throttled checkpoints -- full
                        # discard, per explicit decision. Nothing
                        # computed for this source so far is kept;
                        # output_mode[1]/the DB is never touched (the
                        # save step below is never reached). Same-tick
                        # "flash" of this message before the terminal
                        # "cancelled" message/dialog, per explicit
                        # decision -- no artificial delay -- the real
                        # screen paint TkinterProgressView.render()
                        # already forces is enough to avoid a
                        # "Not Responding" appearance.
                        q.put(("update", "Discarding run... Please wait.", None, None, False))
                        cancelled = True
                        break

                    # D-Cancel: from here through the save, Cancel is
                    # disabled -- a real result now exists and nothing
                    # past this point can be safely interrupted (the
                    # save itself is non-cancelable by design -- see
                    # _write_db_output_safely()).
                    q.put(("update", f"Saving {name}", None, None, False))

                    if output_mode[0] == "local":
                        desired_base_name = out_base
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
                        # The actual destination table was already
                        # decided by resolve_db_output_table(), BEFORE
                        # this function (and the worker thread) even
                        # started -- fuzzy matching + user confirmation
                        # already happened there (see that function's
                        # docstring). This just uses the result. Falls
                        # back to the old filename-lowercased behavior
                        # only if resolved_table_name is somehow None
                        # here (output_mode[0] != "db" can't reach this
                        # branch, so this is just a defensive fallback).
                        if src_type == "local":
                            table = resolved_table_name if resolved_table_name is not None else out_base.lower()
                        else:
                            table = out_base
                        # D-Cancel: staging-write / verify / atomic
                        # rename-swap, replacing the previous direct
                        # to_postgis(..., if_exists="replace") call --
                        # see "DB ATOMIC WRITE / CANCEL-SAFE STAGING"
                        # above.
                        _write_db_output_safely(engine, schema, result,
                                                 table, resolved_outcome,
                                                 run_id, run_started_at)
                        print(f"🔄 Saved to DB: {table}")

                except Exception as source_err:
                    # Isolate failures per source: log/report and move on
                    # to the next source instead of aborting the entire
                    # batch. Sources already written before this one keep
                    # their output — only this one is skipped.
                    skipped.append((name, str(source_err)))
                    q.put(("update", f"Skipped {name}: {source_err}", None, None, None))
                    continue

            if cancelled:
                print("🛑 Run cancelled by user.")
                q.put(("cancelled", None, None, None))
                return

            if skipped:
                summary = "Done, but some sources were skipped:\n" + "\n".join(
                    f"- {n}: {err}" for n, err in skipped
                )
            else:
                summary = "✅ Processing done!"
            q.put(("done", summary, None, None))

        except Exception as e:
            q.put(("error", str(e), None, None))
        finally:
            # D-Cancel: released here regardless of how the try block
            # above exits -- clean success, a Cancel (the `return` right
            # after the cancelled check above), or an exception (the
            # `except` clause's own fallthrough). A lingering advisory
            # lock past this point would make a FUTURE run's
            # _scan_orphaned_cama_tables() wrongly treat this run's own
            # staging/backup tables as still "live" even after this run
            # has fully ended.
            if lock_conn is not None:
                _release_run_lock(lock_conn)

    def poll_queue():
        """
        Main-thread poller (scheduled via app_root.after(100, ...)):
        drains q and updates the progress dialog, opens the result in
        Global Mapper, or shows the final success/error/cancelled
        dialog and stops polling, depending on the event kind. All
        Tkinter calls happen here, never inside worker() itself.
        """
        if not app_root.winfo_exists():
            return
        try:
            while True:
                kind, *rest = q.get_nowait()
                if kind == "update":
                    progress.update(rest[0], rest[1], rest[2], rest[3])
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
        app_root.after(100, poll_queue)

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
        apply_icon(root, "lotlocation.ico")
        root.withdraw()
        open_main_window(root)
        root.mainloop()


if __name__ == "__main__":
    main()