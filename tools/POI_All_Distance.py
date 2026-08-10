# ==========================================================
# POI_All_Distance.py
# Tool-style version (mirrors road_width behavior)
# ==========================================================

root = None

import os
import re
import time
import datetime
import traceback
import json
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox, ttk
import geopandas as gpd
import networkx as nx
from shapely.geometry import Point, LineString
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

import subprocess
import ctypes
import sys
import psycopg2
from sqlalchemy import create_engine, text, inspect

from utils.table_name_matching import normalize_name, find_matching_tables
from utils.resource_path import resource_path
from utils.db_discovery import load_db_credentials, fetch_tables
from utils.window_icon import apply_icon

# ---------------- CONFIG ----------------

def _remove_close_button(win):
    """
    Strips the titlebar's close (X) button (and system menu) via the
    Win32 API directly, ported from road_width.py's implementation
    (also used by poi_within_200_meters_for_parcellary_church_mall_
    police_park.py's own progress dialog for the same reason).

    protocol("WM_DELETE_WINDOW", lambda: None) (used alongside this,
    see run_with_progress() below) only prevents the CLICK from doing
    anything -- the X itself stays fully visible, still highlights on
    hover, and still looks clickable, since Tkinter/the OS's own
    window chrome has no idea the close action has been neutralized,
    which reads as broken (a button that does nothing when clicked)
    rather than intentionally absent. This function is a stronger fix
    -- actually removing the button from the titlebar so there's
    nothing there to click in the first place.

    NOTE: clearing WS_SYSMENU via GetWindowLongW/SetWindowLongW is the
    correct, well-documented Win32 pattern for this, but its actual
    visual result can vary by Windows/DWM build/theme -- confirmed in
    practice on this project's own deployment target that it does not
    always visibly remove the X, in which case the protocol()-based
    "click does nothing" behavior below is what actually takes effect.
    Kept anyway (it's the more correct fix when it does work, and is
    fully harmless when it doesn't -- any failure here is caught and
    silently ignored, falling back to the protocol() behavior).
    """
    try:
        import ctypes
        GWL_STYLE = -16
        WS_SYSMENU = 0x00080000
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_STYLE, style & ~WS_SYSMENU)
    except Exception:
        pass


GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"

GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"

parcel_source = None     # ("local", [paths]) OR ("db", [tables])
poi_source = None        # ("local", [path])  OR ("db", [table])
road_source = None       # ("local", [path])  OR ("db", [table])
output_mode = None       # ("local", out_dir) OR ("db", None)

ALLOWED_FCLASS = {"school", "church", "shop", "transport", "university"}

PRS92_ZONE_BOUNDS = [
    (-180.0, 118.0, 3121, "Zone I"),
    (118.0, 120.0, 3122, "Zone II"),
    (120.0, 122.0, 3123, "Zone III"),
    (122.0, 124.0, 3124, "Zone IV"),
    (124.0, 180.0, 3125, "Zone V"),
]


def detect_prs92_zone(labeled_gdfs):
    """
    Auto-detect the PRS92 zone EPSG code from the COMBINED bounding-box
    midpoint longitude of one or more input GeoDataFrames.

    labeled_gdfs: list of (label, gdf) tuples, e.g.
        [("Land Parcel", gdf), ("POI", poi_gdf), ("Road Network", road_gdf)]
    The label is used only for diagnostics. It has no effect on CRS
    detection.

    Canonical CRS-detection logic, standardized across CAMA GIS tools
    (see road_frontage.py / lot_location.py / road_width.py): total_bounds
    (not a unioned-geometry centroid -- a known source of GEOS
    TopologyExceptions on real-world cadastral data with invalid
    geometries), the same PRS92_ZONE_BOUNDS table, and the same
    missing-CRS fallback/warning behavior.

    Business rule -- auxiliary layers without usable geometry are
    ignored. Zone detection proceeds as long as at least one valid
    layer remains. This is intentional, not a gap: this tool's own
    graph_from_roads() already raises its own, more specific error if
    the road layer has no usable LineString geometry, and an empty POI
    layer is already handled gracefully further downstream (produces
    an all-NaN-distance result rather than a crash) -- duplicating a
    hard requirement here would only produce a less helpful error
    message for the same underlying situation, not add real
    protection.

    Two layers of defense against a layer with no usable geometry at
    all silently corrupting the zone calculation (NaN bounds slipping
    through undetected):
      1. Pre-filter: skip any gdf that's None, has zero rows, or has
         no non-null geometry at all (geometry.notna().any()). Note
         notna() alone does NOT catch empty-but-non-null geometries
         (Shapely's empty Polygon() passes notna() but still produces
         NaN bounds), so this filter is a cheap first pass, not a
         complete guarantee.
      2. Per-gdf post-check: after computing each gdf's total_bounds,
         explicitly verify it's not NaN and raise immediately, naming
         the specific layer, if it is -- BEFORE appending to
         all_bounds. This has to happen here and not after combining:
         min()/max() below are plain Python builtins, not NaN-aware,
         so a NaN slipping into all_bounds would silently propagate or
         vanish depending on its position in the list rather than
         raising anything.
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
            print("⚠️ No CRS found in one of the input datasets -- assuming "
                  "WGS84. Measurements may be incorrect if the actual CRS "
                  "is different.")
        g_wgs84 = g.to_crs(epsg=4326) if g.crs.to_epsg() != 4326 else g

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
            print(f"ℹ️ Auto-detected PRS92 {zone_label} (EPSG:{epsg}) "
                  f"from combined bbox-midpoint longitude {center_lon:.4f}°E")
            return epsg

    raise ValueError(f"Could not determine PRS92 zone for longitude {center_lon}")


# -----------------------------------
# Utility: logging helper
# -----------------------------------
def log_message(msg, logfile=None, flush=True):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=flush)
    if logfile:
        with open(logfile, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# -----------------------------------
# DB helpers (same pattern as road_width)
# -----------------------------------
def get_geometry_column(table, engine, schema):
    with engine.connect() as conn:
        q = text("""
            SELECT f_geometry_column
            FROM geometry_columns
            WHERE f_table_schema=:s AND f_table_name=:t
        """)
        r = conn.execute(q, {"s": schema, "t": table}).fetchone()
        return r[0] if r else None


def read_postgis_clean(table, engine, schema):
    geom_col = get_geometry_column(table, engine, schema)
    if not geom_col:
        raise ValueError(f"No geometry column in {table}")

    insp = inspect(engine)
    cols = [c["name"] for c in insp.get_columns(table, schema=schema) if c["name"] != geom_col]

    col_str = ", ".join([f'"{c}"' for c in cols])
    if col_str:
        sql = f'SELECT {col_str}, "{geom_col}" AS geometry FROM "{schema}"."{table}"'
    else:
        sql = f'SELECT "{geom_col}" AS geometry FROM "{schema}"."{table}"'

    return gpd.read_postgis(sql, engine, geom_col="geometry")


# ========================= EXISTING OUTPUT-COLUMN CONFLICT DETECTION =========================
# This tool's output columns are dynamic, not a fixed list -- they
# depend on which POI fclass types (from ALLOWED_FCLASS) actually
# appear in the selected POI source. Unlike road_frontage.py/terrain.py/
# land_shape_compactness.py's fixed OUTPUT_COLUMN_TARGETS tuples, this
# tool builds its target list per-run via _realizable_targets() below.
#
# Business decision (confirmed): only check for types that are actually
# PRESENT in the selected POI source this run -- if there's no
# "university" POI in the selected POI layer, there's no reason to
# check for a pre-existing CAMA_UNIVERSITY1 conflict. All three ranks
# (1-3) are checked for every present type, plus each rank's _METHOD
# companion column, e.g. present type "school" -> CAMA_SCHOOL1,
# CAMA_SCHOOL2, CAMA_SCHOOL3, CAMA_SCHOOL1_METHOD, CAMA_SCHOOL2_METHOD,
# CAMA_SCHOOL3_METHOD.
#
# NOTE (flagged, not acted on): the per-row distance-write loop in
# task() below unconditionally pre-initializes ALL FIVE ALLOWED_FCLASS
# types' distance columns (CAMA_SCHOOL1..CAMA_UNIVERSITY3) to NaN every
# run, regardless of which types are actually present in this run's POI
# source -- this is pre-existing tool behavior, unrelated to and not
# modified by this change. Scoping the conflict CHECK to only
# currently-present types (per the confirmed decision above) means a
# type absent this run won't trigger a warning even though its
# pre-init step will still silently wipe that column's old values to
# NaN, same as it always has. This is an accepted, informed tradeoff,
# not an oversight.
def _realizable_targets(poi_types):
    """
    Builds this run's actual list of CAMA_-prefixed target column names
    -- only for POI types present in poi_types (already filtered to
    ALLOWED_FCLASS and normalized/lowercased upstream). Six targets per
    type: three distance ranks + their three _METHOD companions.
    """
    targets = []
    for t in poi_types:
        for i in range(1, 4):
            targets.append(f"CAMA_{t.upper()}{i}")
            targets.append(f"CAMA_{t.upper()}{i}_METHOD")
    return targets


def _get_poi_types_for_check(poi_source, engine, schema):
    """
    Lightweight, standalone read of the selected POI source, used ONLY
    by the Run-time column-conflict pre-check in on_run() -- separate
    from, and NOT a substitute for, the "real" POI read task() performs
    later. Mirrors the same normalize/filter logic task() uses
    (lowercase + strip 'fclass', filtered to ALLOWED_FCLASS) so the
    target list built here matches what task() will actually realize.

    Returns a sorted list of present type strings, or an empty list if
    the read fails for any reason -- a failure here is NEVER treated as
    a column-conflict failure; it just means the conflict check is
    skipped entirely for this Run (logged to console). The real read
    inside task() remains solely responsible for surfacing any genuine
    read error to the user.
    """
    try:
        poi_gdf = (
            gpd.read_file(poi_source[1][0]) if poi_source[0] == "local"
            else read_postgis_clean(poi_source[1][0], engine, schema)
        )
        fclass_norm = poi_gdf["fclass"].astype(str).str.lower().str.strip()
        return sorted(t for t in fclass_norm.unique() if t in ALLOWED_FCLASS)
    except Exception as e:
        print(f"⚠️ Could not read POI source to check for existing output "
              f"column(s): {e}")
        return []


def _check_parcel_poi_distance_conflicts(sources, source_type, targets):
    """
    Checks the selected Land Parcel source -- Local file OR Database
    table (extended to cover both as part of Fix 3; previously
    LOCAL-only) -- for pre-existing columns matching any of `targets`
    (case-insensitive exact match). Same read approach as every other
    tool's conflict check for a Local source (plain gpd.read_file(),
    read failure = skip-only, never a conflict-check failure); for a
    Database source, read_postgis_clean() is used instead, loading its
    own creds/schema/engine (self-contained).

    Returns a list of (path_or_table, existing_output_cols) tuples --
    one entry only for sources where at least one target match was
    found. existing_output_cols is {target_name: actual_existing_
    column_name}, original casing preserved (shown in the confirmation
    dialog only -- see _normalize_conflicting_columns() below for how
    the ACTUAL write-back always converges to the canonical CAMA_ name
    instead of preserving this casing, since this tool's output is a
    single merged dataframe across possibly many sources, not one
    output per source).
    """
    conflicts = []
    engine = None
    schema = None
    if source_type == "db":
        creds = load_db_credentials()
        if not creds:
            print("⚠️ Could not load DB credentials to check for existing "
                  "output column(s).")
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
            print(f"⚠️ Could not read parcel layer to check for existing "
                  f"output column(s): {path_or_table}: {e}")
            continue
        found = {}
        for target in targets:
            match = next((c for c in gdf.columns if c.lower() == target.lower()), None)
            if match is not None:
                found[target] = match
        if found:
            conflicts.append((path_or_table, found))
    return conflicts


def _normalize_conflicting_columns(gdf, targets):
    """
    Runs on the MERGED parcel dataframe (after pd.concat of all
    selected sources), before any distance values are written.
    Guarantees that, for each canonical target name, there is at most
    ONE resulting column -- never a leftover, differently-cased
    duplicate sitting alongside the canonical one.

    Since this tool's output is one merged dataframe (not one output
    per source), there is no meaningful "which source's casing should
    win" question to answer -- the canonical CAMA_-prefixed name is
    always the final name, full stop. This function does not preserve
    any detected casing; it only prevents duplicate logical columns
    from surviving the merge.

    For each target:
      - No matching column (any casing) -> nothing to do.
      - Exactly one matching column -> renamed to the canonical name if
        its casing differs (a no-op if it's already exactly canonical).
      - More than one matching column (can only happen if two DIFFERENT
        source files each had their own, differently-cased version of
        the same logical column before merging) -> coalesced row-wise
        into a single canonical column, preferring the first column's
        value wherever it's non-null. This is safe and lossless in the
        normal case: after pd.concat, a row only ever has data in the
        ONE column that its OWN source file actually had -- every other
        matching column is NaN for that row (pd.concat fills missing
        columns with NaN), so there is no real overlap to resolve.
        The only genuinely "impossible/ambiguous" case -- a single row
        with non-null values in more than one matching column at once
        (only possible if a single source file itself already had two
        differently-cased duplicate columns, which would be corrupt/
        unusual input) -- is handled by keeping the first column's
        value and printing a console warning naming the affected target
        and row count. This is never surfaced as a dialog; it's a
        silent, deterministic fallback for a case that should not occur
        with normal input.
    """
    for target in targets:
        matches = [c for c in gdf.columns if c.lower() == target.lower()]
        if not matches:
            continue
        if len(matches) == 1:
            if matches[0] != target:
                gdf = gdf.rename(columns={matches[0]: target})
            continue
        combined = gdf[matches[0]]
        for extra in matches[1:]:
            both_populated = combined.notna() & gdf[extra].notna()
            if both_populated.any():
                print(f"⚠️ Ambiguous duplicate values found for '{target}' "
                      f"across columns {matches}: {int(both_populated.sum())} "
                      f"row(s) had values in more than one matching column -- "
                      f"keeping the first column's value for those rows.")
            combined = combined.combine_first(gdf[extra])
        gdf = gdf.drop(columns=matches)
        gdf[target] = combined
    return gdf


# -----------------------------------
# Build graph from road shapefile
# -----------------------------------
def graph_from_roads(road_gdf):
    G = nx.Graph()
    edges = []
    nodes_coords = set()

    geom_types = road_gdf.geometry.geom_type.dropna().unique().tolist()
    print(f"ℹ️ Road geometry types found: {geom_types}")

    if not any(t in ("LineString", "MultiLineString") for t in geom_types):
        raise Exception(
            f"Road layer has no LineString geometry.\n"
            f"Found types: {geom_types}\n\n"
            f"Please select a line/road layer, not a polygon or point layer."
        )

    for _, row in road_gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        try:
            if geom.geom_type == "LineString":
                segments = [geom]
            elif geom.geom_type in ("MultiLineString", "GeometryCollection"):
                segments = [g for g in geom.geoms if g.geom_type == "LineString"]
            else:
                continue  # skip Polygons, Points, etc.

            for seg in segments:
                coords = list(seg.coords)
                for i in range(len(coords) - 1):
                    u = (float(coords[i][0]), float(coords[i][1]))
                    v = (float(coords[i + 1][0]), float(coords[i + 1][1]))
                    length = Point(u).distance(Point(v))
                    G.add_edge(u, v, length=float(length))
                    edges.append((u, v, float(length)))
                    nodes_coords.add(u)
                    nodes_coords.add(v)
        except Exception:
            continue

    nodes_coords = np.array(list(nodes_coords)) if nodes_coords else np.zeros((0, 2))
    return G, edges, nodes_coords


# -----------------------------------
# Worker (routing logic UNCHANGED -- same nearest-vertex snap, same
# silent-to-straight-line fallback behavior as the original baseline.
# Only addition: this now also returns a METHOD label and the route
# geometry, so what the baseline was already doing internally becomes
# visible instead of hidden.)
# -----------------------------------
def worker_process(args):
    (row_idx, centroid_xy, poi_types, poi_coords_dict, edges_list, nodes_coords) = args
    route_records = []  # (typ, rank, method, dist, geometry)
    try:
        if len(nodes_coords) == 0:
            return row_idx, {}, route_records

        nodes_kdtree = cKDTree(nodes_coords)
        G_local = nx.Graph()
        for u, v, length in edges_list:
            G_local.add_edge(tuple(u), tuple(v), length=float(length))

        results = {}
        for typ in poi_types:
            coords = poi_coords_dict.get(typ)
            if coords is None or len(coords) == 0:
                continue

            k = min(3, len(coords))
            tree = cKDTree(coords)
            _, idxs = tree.query([centroid_xy], k=k)

            idxs = [int(idxs[0])] if k == 1 else [int(i) for i in idxs[0]]

            network_results = []
            for pi in idxs:
                poi_xy = coords[pi]
                _, sidx = nodes_kdtree.query([centroid_xy])
                _, eidx = nodes_kdtree.query([poi_xy])
                start = tuple(nodes_coords[int(sidx[0])])
                end = tuple(nodes_coords[int(eidx[0])])

                method = "Straight"
                route_geom = LineString([centroid_xy, tuple(poi_xy)])
                try:
                    if nx.has_path(G_local, start, end):
                        dist, path = nx.bidirectional_dijkstra(G_local, start, end, weight="length")
                        method = "Road"
                        route_geom = LineString(path)
                    else:
                        dist = Point(centroid_xy).distance(Point(poi_xy))
                except Exception:
                    dist = Point(centroid_xy).distance(Point(poi_xy))

                network_results.append((round(dist, 2), method, route_geom))

            network_results = sorted(network_results, key=lambda r: r[0])
            for i, (dist, method, route_geom) in enumerate(network_results[:3], start=1):
                results[f"CAMA_{typ.upper()}{i}"] = float(dist)
                results[f"CAMA_{typ.upper()}{i}_METHOD"] = method
                route_records.append({
                    "PARCEL_IDX": row_idx,
                    "CATEGORY": typ.upper(),
                    "RANK": i,
                    "METHOD": method,
                    "DIST_M": dist,
                    "geometry": route_geom,
                })

        return row_idx, results, route_records

    except Exception as e:
        return row_idx, {"_error": str(e)}, route_records


def run_cpu_parallel_with_progress(
    gdf, poi_gdf, road_gdf,
    poi_types, poi_coords_dict,
    output_path,
    progress_bar, status_var,
    stop_flag,
    original_crs=None,
):


    t0 = time.time()

    # Preserve the computation CRS used to generate route geometries.
    # Route coordinates stored in all_route_records are still expressed
    # in this CRS even if gdf is later reprojected back to
    # original_crs below -- routes_gdf must be tagged with THIS crs at
    # construction time, not gdf.crs after it's been changed, or the
    # geometry values and the CRS label would disagree (GeoDataFrame's
    # crs= constructor argument only tags metadata, it does not
    # transform coordinates -- that's what .to_crs() is for).
    projected_crs = gdf.crs

    G_main, edges_list, nodes_coords = graph_from_roads(road_gdf)
    if len(edges_list) == 0:
        raise Exception("No valid edges found in road network.")

    # NOTE (Part A3 investigation, resolved as NOT needed): same
    # centroid-only pattern already confirmed safe in road_density.py
    # and terrain.py -- only row.geometry.centroid is read from each
    # parcel below, never the full polygon via buffer/intersection/
    # union. Road geometry (graph_from_roads() above) is built from raw
    # LineString coordinates, not a union/buffer either. No
    # fix_geometry() added.
    args_list = []
    for idx, row in gdf.iterrows():
        centroid_xy = (row.geometry.centroid.x, row.geometry.centroid.y)
        args_list.append(
            (idx, centroid_xy, poi_types, poi_coords_dict, edges_list, nodes_coords)
        )

    total = len(args_list)
    processed = 0
    errors = 0
    all_route_records = []

    total_cpus = os.cpu_count() or 1
    cpu_count = max(1, total_cpus - 1)


    for i, args in enumerate(args_list, start=1):
        if stop_flag["stop"]:
            return None

        idx, res, route_records = worker_process(args)

        if "_error" in res:
            continue

        for k, v in res.items():
            gdf.at[idx, k] = v

        all_route_records.extend(route_records)

        progress_bar["value"] = i
        status_var.set(f"Processed {i} / {total} parcels")

        progress_bar.master.update_idletasks()
        progress_bar.master.update()

    if output_path:
        if original_crs is not None:
            gdf = gdf.to_crs(original_crs)
        _write_gpkg(gdf, output_path)

        if all_route_records:
            routes_gdf = gpd.GeoDataFrame(all_route_records, crs=projected_crs)
            routes_path = os.path.join(os.path.dirname(output_path), "poi_routes.gpkg")
            if original_crs is not None:
                routes_gdf = routes_gdf.to_crs(original_crs)
            _write_gpkg(routes_gdf, routes_path)
            print(f"ℹ️ Exported {len(routes_gdf)} route(s) with Road/Straight labels: {routes_path}")
            return routes_path
    return None


# -----------------------------------
# GUI — single unified window
# -----------------------------------
def _pick_db_tables(parent, tables, multi, on_select):
    picker = tk.Toplevel(parent)
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

    Why atomicity is necessary here specifically: a plain
    gdf.to_file(path, driver="GPKG") call, or a manual
    os.remove()-before-write, can fail partway through -- a crash, the
    machine losing power, disk full mid-write -- leaving `path` either
    gone entirely (if a pre-existing file was deleted first, with
    nothing written in its place) or corrupted/incomplete.

    This version writes to a temporary file first, VERIFIES that file
    is actually readable back (a write that raised no exception but
    produced something GDAL itself can't re-open is exactly the
    failure this guards against), and only then atomically replaces
    the destination via os.replace() -- which is atomic on the same
    filesystem on both Windows and POSIX: there is no window where
    `path` doesn't exist. If ANY step before the final os.replace()
    fails, `path` is left completely untouched, exactly as if this
    call never happened.
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
    apply_icon(dialog, "distancefrom.ico")
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


def open_main_window(app_root):
    win = tk.Toplevel(app_root)
    apply_icon(win, "distancefrom.ico")
    win.title("POI Distance Tool")
    win.resizable(False, False)
    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))

    # ── state ────────────────────────────────────────────────────
    parcel_source_type = tk.StringVar(master=win, value="local")
    poi_source_type    = tk.StringVar(master=win, value="local")
    road_source_type   = tk.StringVar(master=win, value="local")
    output_dest_type   = tk.StringVar(master=win, value="local")

    # Single-selection architecture
    parcel_local_path = None   # authority: single local file path
    parcel_db_table   = None   # authority: single DB table name
    poi_local_path     = tk.StringVar(master=win)
    poi_db_table       = tk.StringVar(master=win)
    road_local_path    = tk.StringVar(master=win)
    road_db_table      = tk.StringVar(master=win)
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
        if file:
            nonlocal parcel_local_path
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
            messagebox.showerror("Error", "Could not load DB credentials.")
            return
        tables = fetch_tables(creds["schema"])
        if not tables:
            messagebox.showwarning("No Tables", "No tables found in the database schema.")
            return
        _pick_db_tables(win, tables, multi=False, on_select=_on_parcel_db_selected)

    def _toggle_parcel():
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

    # ── SECTION 2: POI SOURCE ────────────────────────────────────
    section_label(win, "POI Source")

    poi_frame = tk.Frame(win)
    poi_frame.pack(fill="x", padx=18, pady=2)

    poi_radio_row = tk.Frame(poi_frame)
    poi_radio_row.pack(fill="x")
    tk.Radiobutton(poi_radio_row, text="Local File",
                   variable=poi_source_type, value="local",
                   command=lambda: _toggle_poi()).pack(side="left")
    tk.Radiobutton(poi_radio_row, text="Database Table",
                   variable=poi_source_type, value="db",
                   command=lambda: _toggle_poi()).pack(side="left", padx=(12, 0))

    poi_file_var = tk.StringVar(master=win, value="No file selected")
    poi_db_var   = tk.StringVar(master=win, value="No table selected")

    poi_action_row = tk.Frame(poi_frame)
    poi_action_row.pack(fill="x", pady=2)

    poi_lbl = tk.Label(poi_action_row, textvariable=poi_file_var,
                       fg="gray", anchor="w", width=42)
    poi_lbl.pack(side="left")

    poi_btn = tk.Button(poi_action_row, text="Browse…", width=10)
    poi_btn.pack(side="left", **PAD)

    def browse_poi_file():
        f = filedialog.askopenfilename(filetypes=[
            ("Shapefiles", "*.shp"), ("GeoPackage", "*.gpkg"), ("All", "*.*")])
        if f:
            poi_local_path.set(f)
            poi_file_var.set(os.path.basename(f))
            _update_run_button_state()

    def _on_poi_db_selected(sel):
        # _pick_db_tables() only invokes on_select after a confirmed
        # selection, so sel is never empty here -- the original
        # lambda's "if sel else None" branch was a redundant
        # conditional. Switching to a named callback is a readability
        # change only; no behavior change.
        poi_db_table.set(sel[0])
        poi_db_var.set(sel[0])
        _update_run_button_state()

    def browse_poi_db():
        creds = load_db_credentials()
        if not creds:
            messagebox.showerror("Error", "Could not load DB credentials.")
            return
        tables = fetch_tables(creds["schema"])
        if not tables:
            messagebox.showwarning("No Tables", "No tables found in the database schema.")
            return
        _pick_db_tables(win, tables, multi=False, on_select=_on_poi_db_selected)

    def _toggle_poi():
        if poi_source_type.get() == "local":
            poi_lbl.config(textvariable=poi_file_var)
            poi_btn.config(text="Browse…", command=browse_poi_file)
        else:
            poi_lbl.config(textvariable=poi_db_var)
            poi_btn.config(text="Select…", command=browse_poi_db)
        _update_run_button_state()

    # ── SECTION 3: ROAD NETWORK ──────────────────────────────────
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
        # Same note as _on_poi_db_selected() above: _pick_db_tables()
        # only calls on_select with a confirmed, non-empty selection.
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
        global parcel_source, poi_source, road_source, output_mode

        # validate parcel
        if parcel_source_type.get() == "local":
            if not parcel_local_path:
                messagebox.showerror("Missing Input",
                    "Please select a Land Parcel file.")
                return
            parcel_source = ("local", (parcel_local_path,))
        else:
            if not parcel_db_table:
                messagebox.showerror("Missing Input",
                    "Please select a Land Parcel table.")
                return
            parcel_source = ("db", (parcel_db_table,))

        # validate poi
        if poi_source_type.get() == "local":
            if not poi_local_path.get():
                messagebox.showerror("Missing Input",
                    "Please select a POI file.")
                return
            poi_source = ("local", [poi_local_path.get()])
        else:
            if not poi_db_table.get():
                messagebox.showerror("Missing Input",
                    "Please select a POI table.")
                return
            poi_source = ("db", [poi_db_table.get()])

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

        # ------------------------------------------------------------------
        # Existing OUTPUT-COLUMN conflict warning. This tool's output
        # columns are dynamic (see _realizable_targets()) -- only POI
        # types actually present in the selected POI source this run are
        # checked. Extended (Fix 3) to cover both Local and Database
        # Land Parcel sources -- previously LOCAL-only (see
        # _check_parcel_poi_distance_conflicts()'s own docstring).
        # Shown once, combined across every affected source, only here
        # at Run time. Declining cancels the run entirely -- nothing is
        # processed, including sources that had no conflict.
        #
        # Unlike every other tool's Task A, there is NO override map
        # threaded through to processing here: this tool's output is a
        # single merged dataframe (multiple parcel sources are
        # concatenated together), so there is no per-source casing to
        # preserve. This dialog is purely a confirmation gate -- the
        # actual write-back always converges to the canonical CAMA_
        # name, handled unconditionally and safely inside task() by
        # _normalize_conflicting_columns() regardless of what happens
        # here (and regardless of Local vs Database source, since that
        # function already runs unconditionally on the merged
        # dataframe).
        # ------------------------------------------------------------------
        poi_types_for_check = []
        try:
            creds_for_check = load_db_credentials()
            engine_for_check = None
            schema_for_check = None
            if poi_source[0] == "db" and creds_for_check:
                schema_for_check = creds_for_check["schema"]
                engine_for_check = create_engine(
                    f"postgresql://{creds_for_check['username']}:{creds_for_check['password']}@"
                    f"{creds_for_check['host']}:{creds_for_check['port']}/{creds_for_check['database']}"
                )
            poi_types_for_check = _get_poi_types_for_check(
                poi_source, engine_for_check, schema_for_check)
        except Exception as e:
            print(f"⚠️ Could not prepare POI-type check for column "
                  f"conflicts: {e}")
            poi_types_for_check = []

        if poi_types_for_check:
            targets_for_check = _realizable_targets(poi_types_for_check)
            conflicts = _check_parcel_poi_distance_conflicts(
                list(parcel_source[1]), parcel_source[0], targets_for_check)
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


        # OUTPUT-FILE conflict check (local output only) -- PRIORITY 2
        overwrite_mode = None
        if output_mode[0] == "local":
            desired_names = (
                [os.path.splitext(os.path.basename(p))[0] for p in parcel_source[1]]
                if parcel_source[0] == "local"
                else list(parcel_source[1])
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
        # PRIORITY 3: DB-output destination table resolution — mirrors
        # PRIORITY 2 above. MOVED here from run_with_progress() (where it
        # already existed from a prior fix, see that function's own
        # "DB-output resolution -- newly added, previously missing"
        # comment) rather than added from scratch. Resolved here on the
        # main thread, before win.destroy(), so confirm_db_overwrite_
        # dialog()/choose_db_overwrite_dialog() (invoked inside
        # resolve_db_output_table()) still have a live parent window, and
        # a Cancel here leaves the fully-configured win intact instead of
        # forcing a from-scratch reopen. Previously this resolution
        # happened inside run_with_progress(), which is only ever invoked
        # AFTER win.destroy() -- see Fix 1 root cause. resolve_db_output_
        # table()'s own matching/decision logic is untouched; only the
        # call site moved here. resolved_table_name is passed into
        # run_with_progress() as a parameter -- same approach already
        # used in every other migrated tool. resolved_outcome is not
        # threaded through (same as those files) because nothing
        # downstream in this file's task() consumes it -- table_action is
        # independently recomputed there via fetch_tables().
        #
        # The two app_root._poi_progress_open = False resets that used to
        # sit alongside this block inside run_with_progress() are dropped
        # here -- the guard is never set True until run_with_progress()
        # itself begins (see its own unchanged top), so there is nothing
        # to reset at this point in the flow.
        # ------------------------------------------------------------------
        resolved_table_name = None
        if output_mode[0] == "db":
            _resolve_creds = load_db_credentials()
            if not _resolve_creds:
                return
            _resolve_schema = _resolve_creds["schema"]
            _resolve_engine = create_engine(
                f"postgresql://{_resolve_creds['username']}:{_resolve_creds['password']}@"
                f"{_resolve_creds['host']}:{_resolve_creds['port']}/{_resolve_creds['database']}"
            )
            resolved_table_name, _resolved_outcome = resolve_db_output_table(
                win, _resolve_schema, parcel_source
            )
            if resolved_table_name is None:
                print("Run cancelled by user (database output table not confirmed).")
                return

        win.destroy()
        run_with_progress(app_root, overwrite_mode, resolved_table_name)

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
        Land Parcel source, a POI source, a Road Network source, and an
        Output destination are all present.

        The cascade below intentionally mirrors on_run()'s own
        validation order further down -- conscious duplication for a
        minimal-risk, additive gating layer, not a refactor of on_run()
        itself. Keep the two in sync if this tool's required inputs
        ever change. (The additional single-parcel-table check inside
        run_with_progress()'s task() for DB output is deeper than what
        on_run() validates and is intentionally NOT mirrored here.)

        Explicit bg/fg/cursor toggling (not just state=) is required:
        Tkinter does NOT automatically gray out a classic tk.Button's
        custom bg/fg when state="disabled", and does not suppress a
        widget's assigned cursor either -- both must be set explicitly
        for each state.
        """
        has_parcel = bool(parcel_local_path) if parcel_source_type.get() == "local" else bool(parcel_db_table)
        has_poi = bool(poi_local_path.get()) if poi_source_type.get() == "local" else bool(poi_db_table.get())
        has_road = bool(road_local_path.get()) if road_source_type.get() == "local" else bool(road_db_table.get())
        has_output = bool(output_local_dir.get()) if output_dest_type.get() == "local" else True

        if not has_parcel:
            run_status_var.set("Please select a Land Parcel source.")
            ready = False
        elif not has_poi:
            run_status_var.set("Please select a POI source.")
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
    _toggle_poi()
    _toggle_road()
    _toggle_output()
    _update_run_button_state()


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

    Ported from road_frontage.py's own confirm_db_overwrite_dialog(),
    including its topmost-persistence fix (no premature after(100, ...)
    reset), its content-then-geometry-then-topmost ordering fix, its
    periodic re-assert loop, and its screen-centered geometry fix --
    see that file's version of this function for the full history/
    rationale behind each of those. Not reproduced verbatim here to
    avoid drift between the two files' comments diverging over time on
    what is otherwise the same, already-validated dialog.
    """
    result = {"confirmed": False}

    dialog = tk.Toplevel(parent)
    apply_icon(dialog, "distancefrom.ico")
    dialog.title("POI ALL DISTANCE TOOL")
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
    Shown when find_matching_tables() returns MORE THAN ONE candidate
    for the DB-output destination table. Lets the user pick exactly
    which one to overwrite via radio buttons; the FIRST candidate in
    the list is pre-selected by default.

    Returns the chosen table name, or None if the user cancelled (must
    be treated as a full cancel by the caller -- there is no "create
    new" for DB output).

    Ported from road_frontage.py's own choose_db_overwrite_dialog(),
    same fixes as confirm_db_overwrite_dialog() above.
    """
    result = {"chosen": None}
    selected = tk.StringVar(value=candidates[0])

    dialog = tk.Toplevel(parent)
    apply_icon(dialog, "distancefrom.ico")
    dialog.title("POI ALL DISTANCE TOOL")
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


def resolve_db_output_table(root, schema, parcel_source):
    """
    Determines the DB-output destination table for the Land Parcel
    source, BEFORE the progress window is even opened -- same "resolve
    everything up front, main thread only" philosophy as
    ask_overwrite_dialog() (see run_with_progress() below).

    Two cases:
      - DB-source Land Parcel (parcel_source[0] == "db"): always writes
        back to the exact same table it was read from -- no matching,
        no dialog.
      - Local-file Land Parcel: fuzzy-matches the filename against
        existing tables via find_matching_tables(), then requires user
        confirmation before treating a match as an overwrite target --
        zero candidates skips the dialog entirely and creates a new
        table under the filename. Only the FIRST selected local source
        (parcel_source[1][0]) is used to derive the desired name --
        this tool always merges every selected parcel source into one
        combined output/table, so there is only ever one destination
        table to resolve, matching the same convention already used by
        lot_location.py/road_frontage.py/etc.'s own
        resolve_db_output_table().

    Returns (resolved_table_name, resolved_outcome), or (None, None) if
    the user cancelled -- caller must abort the entire run in that
    case.

    This function -- and the DB-output resolution workflow it
    implements -- did not previously exist in this file. Its absence
    is why this tool used to require output_mode[0]=="db" AND
    parcel_source[0]=="db" together, raising "Database output requires
    a Database parcel source." otherwise (see run_with_progress()'s own
    comment below for the full removal record). Ported from
    road_frontage.py's canonical implementation, adapted only for this
    file's `parcel_source` naming (same shape/semantics as that file's
    `barangay_source`).
    """
    if parcel_source[0] == "db":
        return parcel_source[1][0], "overwritten"

    desired_name = os.path.splitext(os.path.basename(parcel_source[1][0]))[0]
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


def run_with_progress(app_root, overwrite_mode=None, resolved_table_name=None):
    if hasattr(app_root, "_poi_progress_open") and app_root._poi_progress_open:
        return
    app_root._poi_progress_open = True

    # resolved_table_name: the DB-output destination table. Resolution
    # responsibility now belongs to on_run() (PRIORITY 3), on the main
    # thread, BEFORE win.destroy() -- see Fix 1. By the time it reaches
    # this function it is treated as an already-validated value: either
    # None (local output, or output_mode[0] != "db") or a confirmed
    # table name (DB output, user already had the chance to cancel in
    # on_run()). No re-resolution or re-validation happens here.

    progress_win = tk.Toplevel(app_root)
    progress_win.title("Processing Parcels...")
    progress_win.geometry("420x240")
    progress_win.resizable(False, False)

    tk.Label(progress_win, text="Computing network distances...",
             font=("Segoe UI", 11)).pack(pady=10)

    progress_bar = ttk.Progressbar(progress_win, orient="horizontal",
                                   length=360, mode="determinate", maximum=100)
    progress_bar.pack(pady=5)

    status_var = tk.StringVar(value="Starting...")
    tk.Label(progress_win, textvariable=status_var).pack(pady=5)

    stop_flag = {"stop": False}

    # Cancel button and its cancel() handler removed entirely, per
    # explicit instruction: there is no reliable in-progress cancel for
    # this tool's actual workload (sequential per-parcel routing, no
    # real background thread -- see run_cpu_parallel_with_progress()'s
    # own docstring/comments), matching the same decision already made
    # for road_width.py's own progress dialog. The X (close) button is
    # neutralized the same way -- see _remove_close_button()'s own
    # docstring for why both the Win32 removal attempt AND the
    # protocol() no-op fallback are used together. This also fixes a
    # latent bug: previously this window had NO protocol() override at
    # all, so clicking X would destroy progress_win (and the
    # progress_bar/status_var widgets living inside it) while
    # task() might still be running and referencing them -- a real
    # TclError risk that a no-op close now prevents entirely.
    progress_win.protocol("WM_DELETE_WINDOW", lambda: None)
    _remove_close_button(progress_win)

    progress_win.lift()
    progress_win.focus_force()

    def task():
        # error_message / success_title / success_message: captured
        # here instead of calling messagebox.showinfo()/showerror()
        # directly inline, so that no modal dialog is ever shown while
        # progress_win is still alive -- both were previously called
        # from inside the try block (success) or except block (error),
        # BEFORE finally's progress_win.destroy() ran, since
        # messagebox calls block until dismissed. That let the
        # "Processing Parcels..." window sit behind/alongside the
        # modal for as long as the user took to notice and dismiss it.
        # Single cleanup path preserved: finally still does exactly the
        # same two things it always did (destroy progress_win, reset
        # _poi_progress_open), and does them exactly once, regardless
        # of which path is taken below. Only the ORDERING changed --
        # every message shown, its exact text, and all business logic
        # above are otherwise unchanged.
        error_message = None
        success_title = None
        success_message = None
        try:
            status_var.set("Loading input data...")
            progress_bar["value"] = 0
            progress_win.update_idletasks()

            creds = load_db_credentials()
            schema = creds["schema"]
            engine = create_engine(
                f"postgresql://{creds['username']}:{creds['password']}@"
                f"{creds['host']}:{creds['port']}/{creds['database']}"
            )

            # target_table is resolved_table_name, computed up front in
            # run_with_progress() (before this progress window even
            # opened) via resolve_db_output_table() -- see that call
            # site's own comment for the full record. The old
            # `if parcel_source[0] != "db": raise Exception(...)` guard
            # that used to live here is gone: resolve_db_output_table()
            # now handles BOTH local and db parcel sources for DB
            # output, so this can no longer fail this way. Stays None
            # when output_mode[0] != "db", exactly as before.
            target_table = resolved_table_name

            parcel_gdfs = []
            if parcel_source[0] == "local":
                for p in parcel_source[1]:
                    parcel_gdfs.append(gpd.read_file(p))
            else:
                for t in parcel_source[1]:
                    parcel_gdfs.append(read_postgis_clean(t, engine, schema))

            gdf = gpd.GeoDataFrame(
                pd.concat(parcel_gdfs, ignore_index=True),
                crs=parcel_gdfs[0].crs
            )

            total_features = len(gdf)
            progress_bar["maximum"] = total_features
            progress_bar["value"] = 0
            progress_win.update_idletasks()

            status_var.set("Reprojecting layers...")
            progress_win.update_idletasks()

            poi_gdf = (
                gpd.read_file(poi_source[1][0]) if poi_source[0] == "local"
                else read_postgis_clean(poi_source[1][0], engine, schema)
            )
            road_gdf = (
                gpd.read_file(road_source[1][0]) if road_source[0] == "local"
                else read_postgis_clean(road_source[1][0], engine, schema)
            )

            # Zone is decided from the combined extent of all three
            # spatial layers (not just the parcel layer alone), so a
            # POI or road feature that sits near a zone boundary still
            # influences the zone choice. Reprojection is unconditional
            # -- always run zone detection and reproject all layers to
            # match, regardless of what CRS each layer started in
            # (matches road_width.py's canonical call pattern; the
            # previous "only if gdf.crs.is_geographic" guard here could
            # silently skip PRS92 assignment whenever a layer was
            # already in some other projected CRS).
            # Preserve the parcel layer's original CRS so the final
            # output can be reprojected back to it before saving --
            # PRS92 (zone_epsg below) is a working CRS for the distance
            # computation only, not the intended CRS of the saved
            # output. Captured now, before gdf gets reprojected to
            # zone_epsg. Defensive: gdf.crs can itself be None (e.g. a
            # shapefile with no .prj) -- normalize that to a plain
            # None here so every later check can just be
            # "if original_crs is not None" rather than re-deriving
            # this each time.
            original_crs = gdf.crs if gdf.crs is not None else None

            layers_for_zone = [
                ("Land Parcel", gdf),
                ("POI", poi_gdf),
                ("Road Network", road_gdf),
            ]
            zone_epsg = detect_prs92_zone(layers_for_zone)
            gdf = gdf.to_crs(epsg=zone_epsg)
            poi_gdf = poi_gdf.to_crs(epsg=zone_epsg)
            road_gdf = road_gdf.to_crs(epsg=zone_epsg)

            status_var.set("Preparing POIs...")
            progress_win.update_idletasks()

            poi_gdf["_fclass_norm"] = poi_gdf["fclass"].str.lower().str.strip()
            poi_types = sorted(
                t for t in poi_gdf["_fclass_norm"].unique()
                if t in ALLOWED_FCLASS
            )

            poi_coords = {
                t: np.array([[p.x, p.y] for p in poi_gdf[poi_gdf["_fclass_norm"] == t].geometry])
                for t in poi_types
            }

            # Consolidate any pre-existing, differently-cased CAMA_*
            # column(s) detected in the confirmation step (on_run()
            # above) into their single canonical name before writing
            # anything -- see _normalize_conflicting_columns()'s own
            # docstring for the full reasoning (safe row-wise coalesce,
            # never a per-source override). Runs unconditionally and
            # safely even if on_run()'s own pre-check was skipped (e.g.
            # the lightweight POI pre-read failed) or found nothing --
            # a no-op when there's nothing to consolidate.
            realizable_targets = _realizable_targets(poi_types)
            gdf = _normalize_conflicting_columns(gdf, realizable_targets)

            for t in ALLOWED_FCLASS:
                for i in range(1, 4):
                    gdf[f"CAMA_{t.upper()}{i}"] = np.nan

            if output_mode[0] == "local":
                if parcel_source[0] == "local":
                    desired_base = os.path.splitext(os.path.basename(parcel_source[1][0]))[0]
                else:
                    desired_base = parcel_source[1][0]
                candidate_path = os.path.join(output_mode[1], f"{desired_base}.gpkg")
                had_conflict = os.path.exists(candidate_path)
                if had_conflict and overwrite_mode == "new":
                    desired_base = resolve_output_base_name(output_mode[1], desired_base)
                output_path = os.path.join(output_mode[1], f"{desired_base}.gpkg")
            else:
                output_path = None

            status_var.set("Computing network distances...")
            progress_bar["value"] = 0
            progress_win.update_idletasks()

            routes_path = run_cpu_parallel_with_progress(
                gdf, poi_gdf, road_gdf,
                poi_types, poi_coords,
                output_path,
                progress_bar, status_var,
                stop_flag,
                original_crs=original_crs,
            )

            if not stop_flag["stop"]:
                if output_mode[0] == "local":
                    load_in_global_mapper(output_path)
                    if routes_path:
                        load_in_global_mapper(routes_path)
                    success_title, success_message = "Success", "✅ Processing complete!"
                else:
                    all_tables = fetch_tables(schema)
                    table_action = "replaced" if target_table in all_tables else "new"
                    # Restore the parcel layer's original CRS before
                    # writing to PostGIS -- same reasoning as the local
                    # .to_file() save path in
                    # run_cpu_parallel_with_progress(): PRS92 was only
                    # the working CRS for the distance computation.
                    if original_crs is not None:
                        gdf = gdf.to_crs(original_crs)
                    with engine.begin() as conn:
                        gdf.to_postgis(target_table, conn, schema=schema,
                                       if_exists="replace", index=False)
                    success_title = "Success"
                    success_message = f"✅ Updated DB table: {target_table} ({table_action})"

        except Exception as e:
            error_message = str(e)
        finally:
            if progress_win.winfo_exists():
                progress_win.destroy()
            app_root._poi_progress_open = False

        if error_message:
            messagebox.showerror("Error", error_message)
        elif success_message:
            messagebox.showinfo(success_title, success_message)

    app_root.after(100, task)


# -----------------------------------
# Entry point for MAIN3 / EXE
# -----------------------------------
def main(parent=None):
    global root
    if parent is not None:
        root = parent
        open_main_window(root)
    else:
        root = tk.Tk()
        root.withdraw()
        open_main_window(root)
        root.mainloop()


if __name__ == "__main__":
    main()