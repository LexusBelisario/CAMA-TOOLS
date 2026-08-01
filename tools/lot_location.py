import os
import re
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox, ttk
import geopandas as gpd
import pandas as pd
import numpy as np
import subprocess
import json
import psycopg2
import threading
import queue
from shapely.validation import make_valid
from shapely.geometry import Point
from itertools import combinations
from sqlalchemy import create_engine, inspect, text

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

    # Tk titlebar fallback (important)
    if os.path.exists(png):
        try:
            img = tk.PhotoImage(file=png)
            win.iconphoto(True, img)
            win._icon_ref = img  # prevent garbage collection
        except Exception:
            pass


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


def load_in_global_mapper(filepath):
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

# ----------------- CRS UTILITY -----------------
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


# ----------------- Geometry Fix -----------------
def fix_geometry(geom):
    if geom is None or geom.is_empty: return None
    try:
        if not geom.is_valid: geom = geom.buffer(0)
        if not geom.is_valid: geom = make_valid(geom)
        return geom if not geom.is_empty else None
    except: return None

# ----------------- Lot Location Logic -----------------

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
    for i, (idx, row) in enumerate(result.iterrows(), start=1):
        if progress and (i % 200 == 0 or i == 1 or i == total):
            progress(f"Classifying {source_name}: {i}/{total}", i, total)

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

# ----------------- DB Helpers -----------------
def load_db_credentials():
    path = _get_credentials_path()
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None

def get_geometry_column(table_name, engine, schema):
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT f_geometry_column FROM geometry_columns
                WHERE f_table_schema=:schema AND f_table_name=:table
            """),{"schema":schema,"table":table_name}).fetchone()
            return row[0] if row else None
    except: return None

def read_postgis_clean(table, engine, schema):
    geom_col = get_geometry_column(table,engine,schema)
    insp = inspect(engine)
    cols = [c["name"] for c in insp.get_columns(table,schema=schema) if c["name"]!=geom_col]
    col_str = ", ".join([f'"{c}"' for c in cols]) if cols else ""
    q = f'SELECT {col_str+", " if col_str else ""}"{geom_col}" AS geometry FROM "{schema}"."{table}"'
    return gpd.read_postgis(q, engine, geom_col="geometry")

def normalize_name(name): return re.sub(r'[^a-z]', '', name.lower())

def fetch_tables(schema):
    creds=load_db_credentials()
    if not creds: return []
    try:
        conn=psycopg2.connect(host=creds["host"],port=creds["port"],dbname=creds["database"],
                              user=creds["username"],password=creds["password"])
        cur=conn.cursor()
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s;",(schema,))
        return [r[0] for r in cur.fetchall()]
    except: return []

def find_matching_table(local_name, schema):
    lname = normalize_name(local_name)
    for t in fetch_tables(schema):
        if lname in normalize_name(t) or normalize_name(t) in lname:
            return t
    return None

# REPLACE WITH

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



# ----------------- Single Main Window -----------------
def _pick_db_tables(parent, tables, multi, on_select):
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


def open_main_window(root):
    from tkinter import ttk
    win = tk.Toplevel(root)
    apply_icon(win)
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
    road_read_status_var = tk.StringVar(master=win, value="")
    road_is_reading = False     # True while the background read thread is active
    road_read_ok = False        # True once the current road source has been read successfully

    # Land Parcel existing-CAMA_LOT_LOCATION-column check (Section 1). Mirrors
    # the Road Network background-read shape above, and the dual-slot
    # cache shape already proven in road_frontage.py/road_width.py's own
    # _parcel_classification_cache -- adapted here for much lighter data
    # (just a conflict list, not full GeoDataFrames or checklist
    # BooleanVars, since this check's only job is a yes/no warning before
    # Run, not building a UI checklist).
    parcel_is_reading = False
    parcel_existing_lot_location = []   # [(source_label, existing_col_name), ...]
    _parcel_lot_location_cache = {
        "local": {"key": None, "conflicts": None},
        "db": {"key": None, "conflicts": None},
    }

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
            return "Reading Land Parcel..."
        if road_is_reading:
            return "Reading Road Network..."
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

    parcel_read_status_var = tk.StringVar(master=win, value="")
    parcel_read_status_lbl = tk.Label(
        parcel_frame, textvariable=parcel_read_status_var,
        fg="#b36b00", font=("Segoe UI", 8, "italic"), anchor="w")
    # packed/unpacked by _set_parcel_reading_state()

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

        Returns a list of (path_or_table, existing_col_name) tuples — one
        entry only for sources where a conflicting column was actually
        found. existing_col_name preserves the exact casing found in the
        source (e.g. a column literally named "caMA_Lot_locaTION" is
        returned as-is, not normalized), so the confirmation dialog and
        the eventual write-back both show/use the real casing. The raw
        path/table (not a display-friendly basename) is returned here so
        it can be used directly as a lookup key by run_processing()
        later — the basename is derived separately, only where actually
        needed for display (see on_run() below). Sources with no
        conflict, or that fail to read, are simply omitted — a read
        failure here is reported via a console warning only and never
        blocks Run for a reason unrelated to this check's own purpose;
        the real read, at Run time, will surface any genuine failure on
        its own.
        """
        conflicts = []
        for path_or_table in sources_list:
            gdf, error = _read_road_layer_worker(source_type, path_or_table)
            if error is not None or gdf is None:
                print(f"⚠️ Could not read parcel layer to check for an "
                      f"existing CAMA_LOT_LOCATION column: {path_or_table}: {error}")
                continue
            existing_col = next(
                (c for c in gdf.columns if c.lower() == "cama_lot_location"), None
            )
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
        """
        nonlocal parcel_is_reading
        parcel_is_reading = is_reading
        if is_reading:
            parcel_read_status_var.set("⏳ Reading Land Parcel…")
            parcel_read_status_lbl.pack(fill="x", pady=(2, 0))
            parcel_btn.config(state="disabled")
            parcel_radio_local.config(state="disabled")
            parcel_radio_db.config(state="disabled")
        else:
            parcel_read_status_lbl.pack_forget()
            parcel_btn.config(state="normal")
            parcel_radio_local.config(state="normal")
            parcel_radio_db.config(state="normal")
        _update_run_button_state()

    def _poll_parcel_lot_location_queue(result_queue, source_type, cache_key):
        """
        Runs on the main thread via win.after() polling. Picks up the
        conflict list placed on the queue by the background worker.
        Mirrors _poll_road_read_queue() below.
        """
        nonlocal parcel_existing_lot_location
        if not win.winfo_exists():
            return
        try:
            conflicts = result_queue.get_nowait()
        except queue.Empty:
            win.after(100, lambda: _poll_parcel_lot_location_queue(
                result_queue, source_type, cache_key))
            return

        _parcel_lot_location_cache[source_type] = {
            "key": cache_key, "conflicts": conflicts
        }
        parcel_existing_lot_location = conflicts
        _set_parcel_reading_state(False)

    def _refresh_parcel_lot_location_check(force_refresh=False):
        """
        Background-checks every currently selected Land Parcel
        file/table for an existing column that would collide with the
        CAMA_LOT_LOCATION column this tool is about to write — UNLESS the
        dual-slot cache already has a still-valid entry for this exact
        mode+selection (e.g. toggling Local <-> Database back to a
        selection that hasn't changed), in which case the result is
        restored instantly with no read at all.

        force_refresh: when True, skips the cache-hit check entirely and
        always re-reads, even if the cache key matches. Must be True
        whenever this is called because the user just ACTIVELY selected
        source(s) via Browse/Select — if they re-select the exact same
        file(s) (e.g. after externally adding/removing a CAMA_LOT_LOCATION
        column), a plain key match would otherwise silently serve a
        stale result. Only safe to leave at the default False on the
        _toggle_parcel() path, where the user didn't select anything new.
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

        cache_key = tuple(sources)
        slot = _parcel_lot_location_cache[source_type]
        if not force_refresh and slot["key"] == cache_key and slot["conflicts"] is not None:
            # True cache hit: same mode, exact same set of selected
            # sources, already checked — restore with no I/O.
            parcel_existing_lot_location = slot["conflicts"]
            _update_run_button_state()
            return

        result_queue = queue.Queue()

        def worker():
            conflicts = _check_parcel_lot_location_worker(sources, source_type)
            result_queue.put(conflicts)

        _set_parcel_reading_state(True)
        threading.Thread(target=worker, daemon=True).start()
        win.after(100, lambda: _poll_parcel_lot_location_queue(
            result_queue, source_type, cache_key))

    def browse_parcel_files():
        file = filedialog.askopenfilename(filetypes=[
            ("Shapefiles", "*.shp"), ("GeoPackage", "*.gpkg"), ("All", "*.*")])
        # Cancel returns "" -- do not assign, preserving previous selection.
        if file:
            nonlocal parcel_local_path
            parcel_local_path = file
            parcel_files_var.set(os.path.basename(file))
            # A new Land Parcel selection invalidates any prior
            # CAMA_LOT_LOCATION-conflict check — force_refresh=True: the user
            # actively chose this selection just now, so it must be
            # checked fresh, never served from a stale cache entry.
            _refresh_parcel_lot_location_check(force_refresh=True)
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
                _refresh_parcel_lot_location_check(force_refresh=True)
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
        # untouched. Re-check (or instantly restore from cache) whichever
        # of the three states applies to the newly active mode.
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

    road_type_filter_checkbox = tk.Checkbutton(
        road_filter_frame, text="Filter by Road Type",
        variable=road_type_filter_check_var,
        command=lambda: _toggle_road_type_checklist())
    road_type_filter_checkbox.pack(anchor="w")

    # Holds one Checkbutton per unique ROAD_TYPE value found in the
    # currently selected road layer. Rebuilt from scratch on every new
    # successful read (see _poll_road_read_queue). Only packed while
    # road_type_filter_check_var is True.
    road_type_checklist_container = tk.Frame(road_filter_frame)

    road_read_status_lbl = tk.Label(
        road_frame, textvariable=road_read_status_var,
        fg="#b36b00", font=("Segoe UI", 8, "italic"), anchor="w")
    # packed/unpacked by _set_road_reading_state()

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
        """
        nonlocal road_is_reading
        road_is_reading = is_reading
        if is_reading:
            road_read_status_var.set("⏳ Reading road network…")
            road_read_status_lbl.pack(fill="x", pady=(2, 0))
            road_btn.config(state="disabled")
            road_radio_local.config(state="disabled")
            road_radio_db.config(state="disabled")
        else:
            road_read_status_lbl.pack_forget()
            road_btn.config(state="normal")
            road_radio_local.config(state="normal")
            road_radio_db.config(state="normal")
        _update_run_button_state()

    def _toggle_road_type_checklist():
        if road_type_filter_check_var.get():
            for display_text, (real_value, var) in road_type_value_vars.items():
                tk.Checkbutton(road_type_checklist_container, text=display_text,
                                variable=var).pack(anchor="w")
            road_type_checklist_container.pack(fill="x", padx=(20, 0))
        else:
            for child in road_type_checklist_container.winfo_children():
                child.destroy()
            road_type_checklist_container.pack_forget()

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
        road_type_checklist_container.pack_forget()
        road_filter_frame.pack_forget()

        road_read_ok = False
        _road_gdf_cache = {"key": None, "gdf": None}
        _update_run_button_state()

    def _poll_road_read_queue(result_queue, source_key):
        """
        Runs on the main thread via win.after() polling. Picks up the
        result placed on the queue by the background worker and applies
        it to the GUI — this is the only place the worker's result is
        allowed to touch Tkinter widgets/variables.

        The winfo_exists() guard below only protects this callback from
        running after the window has been destroyed (e.g. the user
        closed the window while a read was still in progress) — it does
        not stop or cancel the background thread itself, which simply
        finishes and has its result discarded here.
        """
        if not win.winfo_exists():
            return

        try:
            gdf, error = result_queue.get_nowait()
        except queue.Empty:
            win.after(100, lambda: _poll_road_read_queue(result_queue, source_key))
            return

        nonlocal road_read_ok
        global _road_gdf_cache

        if error is not None or gdf is None:
            print(f"⚠️ Could not read road layer: {error}")
            _road_gdf_cache = {"key": None, "gdf": None}
            road_read_ok = False
            _set_road_reading_state(False)
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
        blocking the GUI.
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

        _set_road_reading_state(True)
        threading.Thread(target=worker, daemon=True).start()
        win.after(100, lambda: _poll_road_read_queue(result_queue, source_key))

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
            lines = "\n".join(
                f"• '{os.path.basename(path_or_table)}' already has a '{existing_col}' column"
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

        win.destroy()
        run_processing(root, overwrite_mode)

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


# ----------------- Processing -----------------
class ProgressWindow:
    """
    Simple progress dialog shown while run_processing() works on a
    background thread. Adapted from road_frontage.py's ProgressWindow
    (the reference implementation) — generic status label + determinate
    progress bar. No cancel/stop_flag support by design: this tool's
    first progress dialog intentionally stays as simple as
    road_frontage.py's; cancellation is a separate future task if needed.
    """
    def __init__(self, root, title="Processing"):
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

    def update(self, message, value=None, maximum=None):
        self.status_var.set(message)
        if maximum is not None:
            self.progress["maximum"] = maximum
        if value is not None:
            self.progress["value"] = value
        self.win.update_idletasks()
        self.win.geometry("")
        self.win.update()

    def close(self):
        self.win.destroy()


def run_processing(app_root, overwrite_mode=None):
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
    aborting the whole batch.
    """
    global barangay_source, road_source, output_mode, road_type_excluded_values, lot_location_column_overrides, _road_gdf_cache
    if not barangay_source or not road_source or not output_mode:
        messagebox.showerror("Error", "Selections incomplete (Barangay, Road, Output required).")
        return

    progress = ProgressWindow(app_root, "Lot Location Progress")
    q = queue.Queue()

    def worker():
        try:
            def progress_cb(msg, val=None, maxv=None):
                q.put(("update", msg, val, maxv))

            creds = load_db_credentials()
            schema = creds["schema"]
            engine = create_engine(
                f"postgresql://{creds['username']}:{creds['password']}@"
                f"{creds['host']}:{creds['port']}/{creds['database']}"
            )

            q.put(("update", "Loading road network...", None, None))
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
            for src_type, src in sources:
                name = os.path.basename(src) if src_type == "local" else src
                try:
                    q.put(("update", f"Loading {name}", None, None))
                    if src_type == "local":
                        brgy_gdf = gpd.read_file(src)
                        out_base = os.path.splitext(name)[0]
                    else:
                        brgy_gdf = read_postgis_clean(src, engine, schema)
                        out_base = name

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

                    if output_mode[0] == "local":
                        desired_base_name = out_base
                        candidate_path = os.path.join(output_mode[1], f"{desired_base_name}.gpkg")
                        had_conflict = os.path.exists(candidate_path)
                        if had_conflict and overwrite_mode == "new":
                            base_name = resolve_output_base_name(output_mode[1], desired_base_name)
                        else:
                            base_name = desired_base_name
                        out = os.path.join(output_mode[1], f"{base_name}.gpkg")
                        result.to_file(out, driver="GPKG")
                        print(f"✅ Saved {out}")
                        q.put(("open_gm", out, None, None))
                    else:
                        if src_type == "local":
                            match = find_matching_table(out_base, schema)
                            table = match if match else out_base.lower()
                        else:
                            table = out_base
                        result.to_postgis(table, engine, schema=schema, if_exists="replace", index=False)
                        print(f"🔄 Saved to DB: {table}")

                except Exception as source_err:
                    # Isolate failures per source: log/report and move on
                    # to the next source instead of aborting the entire
                    # batch. Sources already written before this one keep
                    # their output — only this one is skipped.
                    skipped.append((name, str(source_err)))
                    q.put(("update", f"Skipped {name}: {source_err}", None, None))
                    continue

            if skipped:
                summary = "Done, but some sources were skipped:\n" + "\n".join(
                    f"- {n}: {err}" for n, err in skipped
                )
            else:
                summary = "✅ Processing done!"
            q.put(("done", summary, None, None))

        except Exception as e:
            q.put(("error", str(e), None, None))

    def poll_queue():
        if not app_root.winfo_exists():
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
        app_root.after(100, poll_queue)

    threading.Thread(target=worker, daemon=True).start()
    poll_queue()


# ----------------- MAIN -----------------
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