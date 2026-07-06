# ==========================================================
# POI_All_Distance.py
# Tool-style version (mirrors road_width behavior)
# ==========================================================

root = None

import os
import time
import datetime
import traceback
import json
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox, ttk
import geopandas as gpd
import networkx as nx
from shapely.geometry import Point, LineString
from shapely.strtree import STRtree
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

import subprocess
import ctypes
import sys
import psycopg2
from sqlalchemy import create_engine, text, inspect

# ---------------- CONFIG ----------------
def _get_credentials_path():
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "pg_credentials.json")
    else:
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "pg_credentials.json"
        )

CREDENTIALS_FILE = _get_credentials_path()
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"

# ---------------------------------------------------------------------
# PRS92 CRS configuration -- auto-detected by longitude, not hardcoded
# ---------------------------------------------------------------------
# Section E compliance requires all network computations to occur in
# PRS92 (EPSG 3121-3125), not WGS84 UTM. The previous implementation used
# gdf.estimate_utm_crs() which resolved to EPSG:32651 (WGS84 UTM 51N) --
# a different datum/projection than what BLGF methodology specifies.
#
# An earlier version of this file hardcoded PRS92_EPSG = 3123 with a
# geographic description of each zone written from memory ("Zone II =
# Ilocos/Cordillera", "Zone IV = Bicol/Mindoro/Palawan") that turned out
# to be WRONG when checked against the authoritative EPSG registry --
# the codes were right, the described coverage was not. PRS92 zones are
# NOT complex administrative regions; they are simple, non-overlapping
# 2-degree LONGITUDE BANDS (confirmed via epsg.io/3121 through 3125):
#
#   Zone I   (3121): west of 118°E
#   Zone II  (3122): 118°E - 120°E  (Palawan, Calamian Islands)
#   Zone III (3123): 120°E - 122°E  (Luzon west of 122°E, Mindoro)
#   Zone IV  (3124): 122°E - 124°E  (SE Luzon, Panay, Cebu, Negros, west Mindanao)
#   Zone V   (3125): east of 124°E  (east Mindanao, east Visayas)
#
# Because the bands are simple, non-overlapping longitude ranges, the
# correct zone can be reliably auto-detected from the data's own
# geographic extent -- see detect_prs92_zone() below. This replaces the
# old hardcoded constant, removing the "confirm this matches your LGU"
# manual step and the risk of silently reusing the wrong zone when this
# tool is pointed at a different municipality/province.
PRS92_ZONE_BOUNDS = [
    # (min_lon, max_lon, epsg_code, zone_label)
    (-180.0, 118.0, 3121, "Zone I"),
    (118.0, 120.0, 3122, "Zone II"),
    (120.0, 122.0, 3123, "Zone III"),
    (122.0, 124.0, 3124, "Zone IV"),
    (124.0, 180.0, 3125, "Zone V"),
]


def detect_prs92_zone(gdf):
    """
    Auto-detect the correct PRS92 zone EPSG code from the geographic
    longitude of gdf's bounding-box center, per the verified EPSG zone
    boundaries in PRS92_ZONE_BOUNDS.

    Reprojects a COPY to WGS84 (EPSG:4326) only to read longitude --
    this does not affect the CRS used for the actual distance
    computation, which is the detected PRS92 zone itself.

    Uses the dataset's overall centroid rather than checking every
    feature individually: a single LGU dataset (municipality/city)
    should never legitimately span two PRS92 zones, since each zone is
    ~220km wide. If the data DOES span outside the detected zone's
    bounds (e.g. a province whose territory straddles a zone boundary,
    or a mixed/multi-LGU dataset), a warning is printed so the
    discrepancy is visible rather than silently producing slightly
    less accurate distances for features near the edge -- but a single
    zone is still returned and used for the whole batch, matching how
    this tool is meant to be run (one LGU dataset per invocation).

    Raises ValueError if gdf has no CRS defined (cannot determine
    longitude without knowing what the input coordinates mean) or if
    the longitude somehow falls outside all defined bands (should not
    happen for any real Philippine dataset).
    """
    if gdf.crs is None:
        raise ValueError(
            "Cannot auto-detect PRS92 zone: input layer has no CRS defined. "
            "Set the correct source CRS on the parcel layer before running this tool."
        )

    gdf_wgs84 = gdf.to_crs(epsg=4326) if gdf.crs.to_epsg() != 4326 else gdf
    minx, miny, maxx, maxy = gdf_wgs84.total_bounds
    center_lon = (minx + maxx) / 2

    for lon_min, lon_max, epsg, zone_label in PRS92_ZONE_BOUNDS:
        if lon_min <= center_lon < lon_max:
            if not (lon_min <= minx and maxx < lon_max):
                print(
                    f"⚠️ Dataset longitude range ({minx:.4f}° to {maxx:.4f}°E) "
                    f"extends outside the detected {zone_label} bounds "
                    f"({lon_min}°E-{lon_max}°E). Features near the dataset's "
                    f"edge may be very slightly less accurate. This is expected "
                    f"if your LGU sits near a PRS92 zone boundary; verify "
                    f"against NAMRIA's official zone map if in doubt."
                )
            print(f"ℹ️ Auto-detected PRS92 {zone_label} (EPSG:{epsg}) "
                  f"from data centroid longitude {center_lon:.4f}°E")
            return epsg

    raise ValueError(f"Could not determine PRS92 zone for longitude {center_lon}")

# ---------------------------------------------------------------------
# Road graph node-merge tolerance
# ---------------------------------------------------------------------
# Road shapefiles are frequently digitized as independent segments that
# are meant to share an intersection point but differ by sub-meter float
# precision (e.g. one segment's endpoint is 121.314960001 while the
# segment that should connect to it is 121.314960002). Because graph
# nodes are literal (x, y) float tuples, NetworkX treats these as two
# distinct nodes -- the graph silently fragments into disconnected
# components, which is a major contributor to the straight-line fallback
# bug (nx.has_path() returns False for routes that should be connected).
# This tolerance (in meters, since data is reprojected to PRS92 before
# graph construction) controls how aggressively nearby nodes are merged.
# Too large a value risks merging two genuinely distinct nearby
# intersections into one; too small leaves real digitizing gaps unfixed.
NODE_MERGE_TOLERANCE_M = 0.5

# ---------------------------------------------------------------------
# Disconnected-component bridging
# ---------------------------------------------------------------------
# NODE_MERGE_TOLERANCE_M above fixes sub-meter float-precision artifacts,
# but real road shapefiles often have GENUINE topology gaps -- a road
# segment that dead-ends a few meters short of the segment it should
# connect to, because it was digitized independently and never snapped.
# Without handling this, ANY such gap anywhere between a parcel and its
# target POI causes nx.has_path() to return False, and the ENTIRE
# distance silently falls back to straight-line (SRC=STR) even if 99%
# of the route is a perfectly good, connected road.
#
# _bridge_disconnected_components() (see graph_from_roads) adds a
# "bridge" edge from each disconnected component to its single nearest
# OTHER component, but ONLY if the gap is within MAX_BRIDGE_DISTANCE_M.
# This lets Dijkstra route through genuine small digitizing gaps while
# refusing to silently connect two road networks that are simply far
# apart (e.g. two different barangays that happen to be each other's
# nearest neighbor in an otherwise sparse rural area) -- that would
# produce a "network" distance that is mostly an invented straight-line
# jump wearing a NET label, defeating the whole point of separating
# NET from STR in the first place.
#
# Bridge edges are tagged is_bridge=True and any route that crosses one
# is reported as SRC="BRIDGED" (never silently folded into "NET") so the
# output stays auditable. MAX_BRIDGES_PER_PATH additionally caps how
# many bridges a single route may chain together -- a route needing to
# hop across many separate gaps to reach its destination is a stronger
# signal of genuinely disconnected road systems than of a few isolated
# digitizing mistakes, and should be treated with more suspicion (or
# rejected back to STR) rather than trusted as-is.
#
# TUNE THESE per dataset after running the connected-component
# diagnostic (see Section E analysis discussion) -- there is no single
# correct default for every road network's digitizing quality.
MAX_BRIDGE_DISTANCE_M = 30.0
MAX_BRIDGES_PER_PATH = 3

parcel_source = None     # ("local", [paths]) OR ("db", [tables])
poi_source = None        # ("local", [path])  OR ("db", [table])
road_source = None       # ("local", [path])  OR ("db", [table])
output_mode = None       # ("local", out_dir) OR ("db", None)

# ORDERED tuple, not a set. A Python set's iteration order is
# hash-based and NOT guaranteed/predictable -- using a set here
# previously caused output column order to appear effectively random
# across runs/environments (e.g. UNIVERSITY1/2/3 columns appearing
# BEFORE SCHOOL1/2/3 in the attribute table with no logical reason).
# A government-facing dataset should have a stable, predictable column
# schema. Order below matches the guidebook's own listing (school,
# shop, transport, church) with university appended.
ALLOWED_FCLASS = ("school", "shop", "transport", "church", "university")

# Candidate column names checked (in priority order) to label each POI in the
# output attribute table. This lets the user see, e.g., "Calauan Central
# Elementary School" next to SCHOOL1 instead of a bare distance value with no
# way to trace it back to the source feature. If none of these exist in the
# POI layer, we fall back to an auto-generated "<fclass>_<n>" label so the
# fields are still populated (see _resolve_poi_name_column()).
POI_NAME_COLUMN_CANDIDATES = [
    "name", "NAME", "Name",
    "poi_name", "POI_NAME",
    "label", "LABEL",
    "namelabel", "NAMELABEL",
]


def _resolve_poi_name_column(poi_gdf):
    """
    Pick the first matching name/label column from POI_NAME_COLUMN_CANDIDATES.
    Returns None if the POI layer has no recognizable name column, in which
    case callers must synthesize a placeholder label instead of crashing.
    """
    for col in POI_NAME_COLUMN_CANDIDATES:
        if col in poi_gdf.columns:
            return col
    return None


# Candidate column names checked (in priority order) to identify each
# parcel by its property identification number in poi_routes.gpkg. This
# lets a reviewer see which real-world parcel a route belongs to without
# having to cross-reference back to parcels_with_poi_distances.gpkg by
# PARCEL_IDX (the raw GeoDataFrame row index, which is stable but not
# human-meaningful). PARCEL_IDX is still included alongside this as a
# guaranteed-unique fallback join key, in case PIN data has duplicates
# or blanks in a given dataset.
PARCEL_PIN_COLUMN_CANDIDATES = [
    "pin", "PIN", "Pin",
    "arp_no", "ARP_NO",
    "prop_id", "PROP_ID",
]


def _resolve_parcel_pin_column(gdf):
    """
    Pick the first matching PIN/property-identifier column from
    PARCEL_PIN_COLUMN_CANDIDATES. Returns None if the parcel layer has no
    recognizable identifier column, in which case callers should leave
    PARCEL_PIN blank in poi_routes.gpkg rather than crashing -- the
    guaranteed-unique PARCEL_IDX field is still available for lookups.
    """
    for col in PARCEL_PIN_COLUMN_CANDIDATES:
        if col in gdf.columns:
            return col
    return None


def _point_xy(geom):
    """
    Return (x, y) for a POI geometry, defensively handling the case where
    the feature isn't a Point. POI layers are expected to contain Point
    geometries (school/shop/transport/church locations), but a Polygon
    (e.g. a school digitized as its campus/building footprint instead of
    a single point) has no .x/.y attribute and would otherwise raise
    AttributeError deep inside candidate-selection, crashing the whole
    batch on one bad feature. Falls back to the geometry's centroid.
    """
    if geom is None:
        raise ValueError("Encountered a null geometry in POI layer.")
    if geom.geom_type != "Point":
        geom = geom.centroid
    return geom.x, geom.y


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
def load_db_credentials():
    path = _get_credentials_path()
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None


def fetch_tables(schema):
    creds = load_db_credentials()
    if not creds:
        return []
    try:
        conn = psycopg2.connect(
            host=creds["host"], port=creds["port"],
            dbname=creds["database"],
            user=creds["username"], password=creds["password"]
        )
        cur = conn.cursor()
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema=%s ORDER BY table_name;
        """, (schema,))
        tables = [r[0] for r in cur.fetchall()]
        conn.close()
        return tables
    except Exception as e:
        messagebox.showerror("DB Error", str(e))
        return []


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


# -----------------------------------
# Build graph from road shapefile
# -----------------------------------
def _merge_close_nodes(G, tolerance=NODE_MERGE_TOLERANCE_M):
    """
    Merge graph nodes that lie within `tolerance` meters of each other.

    WHY THIS EXISTS: real-world road shapefiles are frequently digitized
    as independent segments that are meant to share an intersection but
    differ by sub-meter float precision (one segment's endpoint might be
    (121.314960, 14.147150) while the segment that should connect to it
    is (121.314961, 14.147151)). Because graph nodes are literal (x, y)
    float tuples, NetworkX treats these as two distinct nodes with no
    edge between them -- the graph silently fragments into disconnected
    components. nx.has_path() then correctly reports False for routes
    that should be connected, and the caller falls back to straight-line
    distance. This function is the fix for that specific failure mode.

    Uses a KDTree + union-find pass (O(n log n)) rather than an O(n^2)
    pairwise comparison, since road networks can have tens of thousands
    of vertices.

    NOTE: choosing `tolerance` too large risks incorrectly merging two
    genuinely distinct, closely-spaced intersections into one node.
    NODE_MERGE_TOLERANCE_M (module-level) should be tuned per dataset if
    the road network has unusually dense intersections.
    """
    nodes = list(G.nodes())
    if len(nodes) < 2:
        return G

    coords = np.array(nodes)
    tree = cKDTree(coords)
    pairs = tree.query_pairs(r=tolerance)

    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i, j in pairs:
        union(nodes[i], nodes[j])

    mapping = {n: find(n) for n in nodes}
    merged_count = sum(1 for n in nodes if mapping[n] != n)
    if merged_count:
        print(f"ℹ️ Node merge: collapsed {merged_count} near-duplicate "
              f"vertices (tolerance={tolerance}m) to fix graph connectivity.")

    return nx.relabel_nodes(G, mapping)


def build_edge_index(G):
    """
    Build a spatial index (STRtree) over the CURRENT edges of graph `G`
    so a point can be snapped to the nearest ROAD EDGE (i.e. the true
    nearest point on the road surface) instead of the nearest EXISTING
    VERTEX. This must be called once, after graph construction and node
    merging -- rebuilding it during per-parcel processing would defeat
    the "build once, reuse" performance optimization.

    WHY THIS MATTERS: the previous snapping approach
    (cKDTree.query() against node coordinates only) picks whichever
    polyline vertex happens to be closest. On a long, sparsely-vertexed
    road segment, that can be tens to hundreds of meters away from the
    true nearest point on the road -- silently inflating or deflating
    every distance that routes through that snap point, independent of
    whether the eventual path-finding succeeds or falls back to
    straight-line. This is a real-world manifestation of Section E's
    "nearest_points()" methodology option, applied via edge-splitting.

    NOTE ON SHAPELY VERSION: this uses the shapely>=2.0 STRtree API,
    where `tree.nearest(geom)` returns an integer index into the array
    of geometries passed to STRtree(), not the geometry itself. If your
    environment pins shapely<2.0, this call signature is different
    (nearest() returns the geometry). Verify with `pip show shapely`
    before deploying -- this file assumes >=2.0 (tested against 2.1.2).

    IMPORTANT: this index reflects the graph's edge set AT THE TIME OF
    CALL. snap_pair_to_network() below always fully restores any edge it
    removes before returning, so the index stays valid for every
    subsequent parcel as long as callers always invoke the returned
    restore_fn(). Do not call build_edge_index() again mid-processing --
    it is meant to be built once per run.
    """
    edge_geoms = []
    edge_endpoints = []
    for u, v, data in G.edges(data=True):
        # CRITICAL: exclude bridge edges from the snap index. A bridge
        # (see _bridge_disconnected_components) is a fabricated
        # gap-crossing assumption meant only for Dijkstra to traverse
        # WHEN NEEDED to connect two already-snapped points -- it is not
        # a physical road surface a parcel or POI can actually "stand
        # on" or attach to. Without this filter, a parcel/POI could snap
        # directly onto a bridge if it happened to be the nearest
        # geometric candidate, causing routes to be misclassified as
        # Bridged even when a genuinely closer, fully-real road edge was
        # available -- this was a real bug, caught via manual QA
        # (parcel-to-POI routes flagged Bridged despite an apparently
        # unbroken connecting road on the map).
        if data.get("is_bridge", False):
            continue
        edge_geoms.append(LineString([u, v]))
        edge_endpoints.append((u, v))

    if not edge_geoms:
        return None, []

    tree = STRtree(edge_geoms)
    return tree, edge_endpoints


def snap_pair_to_network(G, tree, edge_endpoints, point_a, point_b, label_a, label_b):
    """
    Snap two points (typically a parcel centroid and a candidate POI)
    onto the road network simultaneously, splitting whatever edge(s)
    they land on so both can be used as Dijkstra source/target nodes.

    CORRECTNESS NOTE (this replaced a buggy first version): a naive
    implementation that snaps each point independently by just ADDING
    two sub-edges (u, vnode) and (vnode, v) without REMOVING the
    original (u, v) edge leaves THREE parallel routes between u and v --
    the untouched original edge plus the two new sub-edges. Dijkstra can
    then route straight through the original full-length edge, silently
    ignoring the projection entirely. This function always removes the
    original edge being split and restores it via the returned
    restore_fn(); verified against a synthetic test case where the naive
    version returned a network distance that ignored the projected
    points completely.

    Also explicitly handles the case where BOTH points project onto the
    SAME original edge (e.g. a parcel directly across the street from
    its target POI): splitting the same edge twice independently would
    corrupt it, so this case instead splits it once into three ordered
    sub-edges (u -> nearer point -> farther point -> v).

    CRITICAL LIFECYCLE: this mutates `G` in place. Because the graph is
    built ONCE and reused across all parcels (see
    run_cpu_parallel_with_progress), the caller MUST call the returned
    restore_fn() as soon as routing for this parcel/POI pair is done.
    Failing to do so leaves stale virtual nodes/edges on the shared
    graph that corrupt routing for later parcels/POIs sharing the same
    edge -- the same category of bug found in the original prototype
    (meters_from_closest_school_shop_transport_for_parcellary.py).

    Returns (vnode_a, offset_a, vnode_b, offset_b, restore_fn).
    Returns (None, None, None, None, no-op) if the edge index is empty.
    """
    if tree is None:
        return None, None, None, None, (lambda: None)

    pt_a, pt_b = Point(point_a), Point(point_b)
    idx_a = tree.nearest(pt_a)
    idx_b = tree.nearest(pt_b)
    u_a, v_a = edge_endpoints[int(idx_a)]
    u_b, v_b = edge_endpoints[int(idx_b)]

    restores = []   # [(u, v, length), ...] edges to restore on cleanup
    new_nodes = []  # virtual node ids to remove on cleanup

    if {u_a, v_a} != {u_b, v_b}:
        vnode_a, off_a = _split_single_edge(G, u_a, v_a, pt_a, label_a, restores)
        vnode_b, off_b = _split_single_edge(G, u_b, v_b, pt_b, label_b, restores)
        new_nodes.extend([vnode_a, vnode_b])
    else:
        u, v = u_a, v_a
        line = LineString([u, v])
        da, db = line.project(pt_a), line.project(pt_b)
        proj_a, proj_b = line.interpolate(da), line.interpolate(db)
        off_a, off_b = pt_a.distance(proj_a), pt_b.distance(proj_b)

        vnode_a = (proj_a.x, proj_a.y, label_a)
        vnode_b = (proj_b.x, proj_b.y, label_b)

        original_length = G[u][v]["length"]
        G.remove_edge(u, v)
        restores.append((u, v, original_length))

        ordered = sorted(
            [(0.0, u), (da, vnode_a), (db, vnode_b), (line.length, v)],
            key=lambda x: x[0]
        )
        for (d1, n1), (d2, n2) in zip(ordered, ordered[1:]):
            G.add_edge(n1, n2, length=abs(d2 - d1))

        new_nodes.extend([vnode_a, vnode_b])

    def restore_fn():
        for n in new_nodes:
            if G.has_node(n):
                G.remove_node(n)
        for (u, v, length) in restores:
            G.add_edge(u, v, length=length)

    return vnode_a, off_a, vnode_b, off_b, restore_fn


def _split_single_edge(G, u, v, pt, label, restores):
    """
    Helper for snap_pair_to_network(): splits edge (u, v) at the
    projection of `pt`, removing the original edge and appending it to
    `restores` for later cleanup. Not intended to be called directly --
    always removes state that only snap_pair_to_network()'s restore_fn
    knows how to put back.
    """
    line = LineString([u, v])
    proj_dist = line.project(pt)
    proj_point = line.interpolate(proj_dist)
    offset = pt.distance(proj_point)

    vnode = (proj_point.x, proj_point.y, label)
    d_u = proj_point.distance(Point(u))
    d_v = proj_point.distance(Point(v))

    original_length = G[u][v]["length"]
    G.remove_edge(u, v)
    restores.append((u, v, original_length))

    G.add_edge(u, vnode, length=d_u)
    G.add_edge(vnode, v, length=d_v)

    return vnode, offset


def path_to_route_geometry(centroid_xy, path_nodes, poi_xy):
    """
    Convert a networkx shortest-path node sequence into a single
    LineString representing the FULL travel path, including the two
    stub segments (parcel centroid -> road, road -> POI) that
    nx.bidirectional_dijkstra's path does NOT include on its own (that
    path only runs between the two virtual snap nodes).

    This exists so the route can be exported as a visible layer in
    Global Mapper -- letting a reviewer see exactly which road segments
    and which stub hops a given SCHOOL1/SHOP1/etc. distance is based on,
    instead of having to manually re-trace the route with the GM measure
    tool to sanity-check a number. See run_cpu_parallel_with_progress()
    for how these are collected and exported as poi_routes.gpkg.

    `path_nodes` may contain a mix of plain (x, y) tuples (original road
    vertices) and (x, y, label) tuples (virtual snap nodes) -- only the
    first two elements of each are used. Consecutive duplicate points
    are dropped (can happen when a snap point coincides exactly with the
    centroid/POI, e.g. certain corner-adjacent POIs).

    Returns None if fewer than 2 distinct points remain (degenerate
    case), so callers can safely skip adding a route feature for it.
    """
    coords = [tuple(centroid_xy)]
    for n in path_nodes:
        coords.append((n[0], n[1]))
    coords.append(tuple(poi_xy))

    dedup = [coords[0]]
    for c in coords[1:]:
        if c != dedup[-1]:
            dedup.append(c)

    if len(dedup) < 2:
        return None
    return LineString(dedup)


def _bridge_disconnected_components(G, max_bridge_distance=MAX_BRIDGE_DISTANCE_M):
    """
    Add a 'bridge' edge connecting each disconnected component to its
    single nearest OTHER component, but ONLY if the gap is within
    max_bridge_distance. This handles GENUINE road-network topology gaps
    (a segment that dead-ends a few meters short of where it should
    connect) that _merge_close_nodes() cannot fix, since that function
    only merges near-duplicate coordinates, not distinct dangling
    endpoints separated by a real gap.

    WHY THE DISTANCE CAP IS MANDATORY: without it, this would happily
    connect two genuinely separate, unrelated road networks just because
    they happen to be nearest to each other somewhere in the dataset
    (e.g. two different barangays' road systems, with a large gap of
    open field between them). That would produce a "network" distance
    that is mostly an invented straight-line jump wearing a NET label --
    exactly the kind of silent inaccuracy this whole rewrite exists to
    eliminate. Only gaps within max_bridge_distance are assumed to be
    digitizing mistakes rather than real absence of a road connection.

    Added edges are tagged is_bridge=True so callers (see worker_process
    and _path_bridge_count()) can detect when a computed route crosses
    one and report it as SRC="BRIDGED" rather than silently folding it
    into SRC="NET" -- the output must stay auditable about which
    distances are pure mapped-road distance versus assumed-gap-crossing.

    Only ONE outgoing bridge is added per component (to its nearest
    neighboring component). A chain of several small islands can still
    be traversed via multiple bridge edges in a single Dijkstra path
    (each island bridges to its own nearest neighbor), which is how
    MAX_BRIDGES_PER_PATH-limited multi-hop bridging works without
    needing exhaustive all-pairs bridging or multiple passes.

    Returns (G, bridges_added).
    """
    components = list(nx.connected_components(G))
    if len(components) <= 1:
        return G, 0

    node_list = []
    comp_id_of_node = {}
    for cid, comp in enumerate(components):
        for n in comp:
            node_list.append(n)
            comp_id_of_node[n] = cid

    coords = np.array(node_list)
    tree = cKDTree(coords)

    bridges_added = 0
    for cid, comp in enumerate(components):
        comp_nodes = list(comp)
        if not comp_nodes:
            continue

        best = None  # (distance, own_node, other_node)
        for n in comp_nodes:
            pt = np.array(n)
            k = min(10, len(node_list))
            dists, idxs = tree.query(pt, k=k)
            if k == 1:
                dists, idxs = [dists], [idxs]
            for d, i in zip(dists, idxs):
                other = node_list[int(i)]
                if comp_id_of_node[other] != cid:
                    # dists are sorted ascending -- the first
                    # cross-component candidate found for this node is
                    # already its local nearest-other-component point.
                    if d <= max_bridge_distance and (best is None or d < best[0]):
                        best = (d, n, other)
                    break

        if best is not None:
            d, n, other = best
            if not G.has_edge(n, other):
                G.add_edge(n, other, length=float(d), is_bridge=True)
                bridges_added += 1

    return G, bridges_added


def _path_bridge_count(G, path_nodes):
    """
    Count how many is_bridge=True edges a shortest-path node sequence
    crosses. Used to decide SRC="NET" vs SRC="BRIDGED", and to enforce
    MAX_BRIDGES_PER_PATH -- a route chaining together many separate
    gap-crossings is a stronger signal of genuinely disconnected road
    systems than of a couple of isolated digitizing mistakes, and should
    be treated with more suspicion than a single bridged gap.
    """
    count = 0
    for u, v in zip(path_nodes, path_nodes[1:]):
        if G.get_edge_data(u, v, {}).get("is_bridge", False):
            count += 1
    return count


def graph_from_roads(road_gdf):
    G = nx.Graph()
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
                    nodes_coords.add(u)
                    nodes_coords.add(v)
        except Exception:
            continue

    # Fix false disconnections caused by digitizing float-precision drift
    # BEFORE any routing happens -- see _merge_close_nodes() docstring.
    G = _merge_close_nodes(G)

    # Then bridge GENUINE topology gaps (real dead-ends a few meters
    # short of where they should connect) that node-merging alone can't
    # fix -- see MAX_BRIDGE_DISTANCE_M / _bridge_disconnected_components()
    # docstring for why this is capped and separately flagged (SRC=BRIDGED)
    # rather than silently treated as pure NET.
    G, bridges_added = _bridge_disconnected_components(G)
    if bridges_added:
        print(f"ℹ️ Component bridging: added {bridges_added} bridge edge(s) "
              f"(max {MAX_BRIDGE_DISTANCE_M}m each) to reconnect gaps in the "
              f"road network. Routes crossing these will be flagged SRC=BRIDGED.")

    nodes_coords = np.array(list(G.nodes())) if G.number_of_nodes() else np.zeros((0, 2))
    edges = [(u, v, d["length"]) for u, v, d in G.edges(data=True)]
    return G, edges, nodes_coords


# -----------------------------------
# Worker
# -----------------------------------
# ARCHITECTURAL CHANGE FROM ORIGINAL: this used to rebuild a fresh
# nx.Graph() from edges_list on every call (once per parcel -- ~11,905
# rebuilds of the same graph for the Calauan dataset). It now receives
# the ALREADY-BUILT shared graph `G_main` plus its edge spatial index
# and mutates it transactionally per POI candidate (snap -> route ->
# clean up) instead of rebuilding. This is safe because
# run_cpu_parallel_with_progress calls this sequentially, not via
# multiprocessing -- a truly parallel version would need each worker to
# operate on its own graph copy instead.
def worker_process(G_main, edge_tree, edge_endpoints, args, debug=False):
    (row_idx, centroid_xy, poi_types, poi_coords_dict, poi_names_dict, parcel_pin) = args
    route_records = []  # collected across all types/ranks for this parcel
    try:
        if G_main.number_of_nodes() == 0:
            if debug:
                print(f"[DIAG] row={row_idx}: G_main is EMPTY -> no graph built, cannot compute anything")
            return row_idx, {}, route_records

        if debug:
            print(f"[DIAG] row={row_idx}: centroid={centroid_xy} | graph nodes={G_main.number_of_nodes()} edges={G_main.number_of_edges()}")

        results = {}
        for typ in poi_types:
            coords = poi_coords_dict.get(typ)
            if coords is None or len(coords) == 0:
                continue
            # names[i] must correspond 1:1 with coords[i] -- both are built
            # from the same filtered POI subset in the same row order
            # (see task() where poi_coords/poi_names are constructed).
            # Do not reorder/filter one without the other.
            names = poi_names_dict.get(typ, [])

            k = min(3, len(coords))
            candidate_tree = cKDTree(coords)
            _, idxs = candidate_tree.query([centroid_xy], k=k)

            # np.atleast_1d + .ravel() + .tolist() correctly flattens BOTH
            # the k=1 case (query returns shape (1,)) and the k>1 case
            # (query returns shape (1,k), since we pass a single-point
            # query wrapped in a list). A bare int(idxs) call breaks under
            # numpy>=2.0 when k=1 -- e.g. when a POI category has exactly
            # one feature in the whole dataset -- raising "only
            # 0-dimensional arrays can be converted to Python scalars"
            # and silently crashing that row (caught by the outer except,
            # but a real correctness bug, not just a testing edge case).
            idxs = np.atleast_1d(idxs).ravel().astype(int).tolist()

            # Each entry carries distance, name, method, the two snap
            # offsets, AND the route geometry (or None for fallback) so
            # everything travels together through sorting -- geometry
            # must be captured here since the path is only available at
            # computation time, not reconstructable after ranking.
            network_results = []
            for pi in idxs:
                poi_xy = coords[pi]
                poi_name = names[pi] if pi < len(names) else f"{typ}_{pi}"

                # Snap BOTH endpoints together via snap_pair_to_network(),
                # not two independent snap_point calls. Correctly handles
                # the case where both the centroid and this POI candidate
                # project onto the SAME road edge (splitting it twice
                # independently would corrupt the graph -- see that
                # function's docstring). Always fully removes/restores
                # any original edge it splits.
                start_vnode, snap_start_dist, end_vnode, snap_end_dist, restore_snap = (
                    snap_pair_to_network(
                        G_main, edge_tree, edge_endpoints,
                        centroid_xy, poi_xy,
                        f"start_{row_idx}_{typ}", f"end_{row_idx}_{typ}_{pi}"
                    )
                )

                straight_line = Point(centroid_xy).distance(Point(poi_xy))
                method = None
                path_exists = None
                network_dist = None
                route_geom = None
                bridge_count = 0

                try:
                    if start_vnode is not None and end_vnode is not None:
                        path_exists = nx.has_path(G_main, start_vnode, end_vnode)
                        if path_exists:
                            network_dist, path_nodes = nx.bidirectional_dijkstra(
                                G_main, start_vnode, end_vnode, weight="length"
                            )
                            # Check whether this shortest path crosses any
                            # bridge edges (see _bridge_disconnected_components).
                            # A route using bridges is NOT a pure mapped-road
                            # distance -- it includes assumed straight-line
                            # jumps across gaps in the road layer, so it must
                            # be labeled distinctly (SRC=BRIDGED), never
                            # silently reported as plain SRC=NET.
                            bridge_count = _path_bridge_count(G_main, path_nodes)
                            if bridge_count > MAX_BRIDGES_PER_PATH:
                                # Chaining this many gap-crossings is a
                                # stronger signal of genuinely separate,
                                # unconnected road systems than of a few
                                # isolated digitizing mistakes -- treat as
                                # no reliable path rather than trust it.
                                method = "fallback_too_many_bridges"
                            elif bridge_count > 0:
                                method = "network_bridged"
                            else:
                                method = "network"
                            # Build the full route geometry (centroid ->
                            # stub -> road path -> stub -> POI) for
                            # visual validation in GM. See
                            # path_to_route_geometry() docstring. Still
                            # built even for rejected too-many-bridges
                            # cases below is skipped -- only meaningful
                            # for accepted network/network_bridged results.
                            if method in ("network", "network_bridged"):
                                route_geom = path_to_route_geometry(
                                    centroid_xy, path_nodes, poi_xy
                                )
                        else:
                            method = "fallback_no_path"
                    else:
                        method = "fallback_no_edge_index"
                except Exception as e:
                    method = f"fallback_exception:{e}"
                finally:
                    # MANDATORY cleanup -- G_main is shared across every
                    # parcel and POI candidate. restore_snap() removes the
                    # virtual nodes AND puts back any original edge that
                    # was split, so the next parcel sees an untouched
                    # graph. See snap_pair_to_network() docstring.
                    restore_snap()

                # PHASE 1B PROMOTION (validated in production):
                # The stub-inclusive distance was withheld from the
                # official SCHOOL1/SHOP1/etc. value until validated
                # against a real Global Mapper field measurement (see
                # Section E analysis doc, Part 3, and the Phase 1A/1B
                # split agreed earlier). Validation result: manual GM
                # network measurement (253.58m) + PARCEL_STUB (10.3m) +
                # POI_STUB (54.47m) = 318.35m, exactly matching the main
                # value. The stub-inclusive total is now the
                # authoritative distance for Network/Bridged results --
                # it was always the physically-correct measurement per
                # the guidebook's "along a road or footpath" requirement
                # (page 296); raw network_dist alone always omitted the
                # two short hops connecting the parcel and POI to the
                # road network itself.
                #
                # A separate TOTAL_STUB field previously existed for
                # this value during the pre-promotion validation period.
                # It has been removed: after promotion, it was an exact
                # duplicate of the main value in EVERY case (Network,
                # Bridged, and Straight alike), not just an occasional
                # coincidence -- keeping it added a redundant column with
                # no distinguishing information.
                if method in ("network", "network_bridged"):
                    dist = network_dist + (snap_start_dist or 0) + (snap_end_dist or 0)
                else:
                    dist = straight_line

                if debug:
                    print(
                        f"[DIAG] row={row_idx} type={typ} poi='{poi_name}' poi_xy={poi_xy} | "
                        f"snap_start={snap_start_dist} | snap_end={snap_end_dist} | "
                        f"has_path={path_exists} | method={method} | bridges={bridge_count} | "
                        f"straight_line={straight_line:.2f} | network_dist={network_dist} | "
                        f"final_dist={dist:.2f}"
                    )

                # Stub geometries are built regardless of method -- start_vnode
                # and end_vnode are computed by snap_pair_to_network() BEFORE
                # the connectivity check, so they exist even for Straight
                # (fallback) results. This is what lets poi_routes.gpkg show
                # "Parcel to Road" / "Road to POI" segments for every row, not
                # just Network/Bridged ones -- useful for diagnosing WHY a
                # given row fell back to Straight (e.g. visually seeing that
                # the two snap points land on clearly separate parts of the
                # road network).
                parcel_stub_geom = None
                poi_stub_geom = None
                if start_vnode is not None:
                    parcel_stub_geom = LineString([tuple(centroid_xy), (start_vnode[0], start_vnode[1])])
                if end_vnode is not None:
                    poi_stub_geom = LineString([(end_vnode[0], end_vnode[1]), tuple(poi_xy)])

                network_results.append((
                    round(dist, 2), poi_name, method,
                    round(snap_start_dist, 2) if snap_start_dist is not None else None,
                    round(snap_end_dist, 2) if snap_end_dist is not None else None,
                    route_geom,
                    bridge_count,
                    parcel_stub_geom,
                    poi_stub_geom,
                ))

            network_results = sorted(network_results, key=lambda r: r[0])
            for i, (dist, poi_name, method, snap_start, snap_end, route_geom,
                    bridge_count, parcel_stub_geom, poi_stub_geom) in enumerate(
                network_results[:3], start=1
            ):
                # Column order below is intentional: NAME -> METHOD ->
                # PARCEL_STUB -> POI_STUB -> total distance -> BRIDGE_COUNT.
                # The main distance value is placed AFTER its component
                # breakdown (not first) so the attribute table reads as
                # "here's what it's made of, here's the total" rather than
                # leading with a bare number with no context beside it.
                results[f"{typ.upper()}{i}_NAME"] = poi_name
                # "Network" = pure road-network distance via Dijkstra, no
                #             gap-crossing assumptions.
                # "Bridged" = road-network distance that crosses one or
                #             more assumed gap-bridges (see
                #             MAX_BRIDGE_DISTANCE_M) -- still mostly real
                #             road, but includes invented straight-line
                #             jumps across mapped topology gaps. Treat as
                #             less certain than plain Network.
                # "Straight" = full straight-line fallback (disconnected
                #             beyond bridging range, too many bridges
                #             chained, or a routing exception).
                if method == "network":
                    method_label = "Network"
                elif method == "network_bridged":
                    method_label = "Bridged"
                else:
                    method_label = "Straight"
                results[f"{typ.upper()}{i}_METHOD"] = method_label
                results[f"{typ.upper()}{i}_PARCEL_STUB"] = snap_start
                results[f"{typ.upper()}{i}_POI_STUB"] = snap_end
                # --- Authoritative distance (Phase 1B PROMOTED) ---
                # Already includes both stubs for Network/Bridged results
                # -- see the promotion block above this loop for the
                # validation evidence. This is the correct, full "along a
                # road or footpath" distance per guidebook page 296, not
                # just the road-network-only portion. No separate
                # TOTAL_STUB field exists anymore -- it was removed as a
                # fully redundant duplicate of this value in every case.
                results[f"{typ.upper()}{i}"] = float(dist)
                # How many gap-bridges this route crossed (0 for plain
                # Network/Straight). Lets a reviewer immediately see how
                # much a Bridged result is relying on assumed
                # gap-crossings versus mapped road.
                results[f"{typ.upper()}{i}_BRIDGE_COUNT"] = bridge_count

                # --- Route geometries for visual validation (poi_routes.gpkg) ---
                # "Parcel to Road" and "Road to POI" are emitted for EVERY
                # method (including Straight) since the underlying stub
                # geometry is always available -- see comment above where
                # parcel_stub_geom/poi_stub_geom are built. "Full Route" is
                # only emitted for Network/Bridged results, since a
                # Straight fallback has no actual traced path to show; a
                # straight centroid-to-POI line would just duplicate what
                # the METHOD="Straight" flag already communicates.
                route_base = {
                    "PARCEL_IDX": row_idx,
                    "PARCEL_PIN": parcel_pin,
                    "CATEGORY": typ.upper(),
                    "RANK": i,
                    "POI_NAME": poi_name,
                    "METHOD": method_label,
                    "BRIDGE_COUNT": bridge_count,
                    "DIST_M": dist,
                }
                if parcel_stub_geom is not None:
                    route_records.append({
                        **route_base,
                        "SEGMENT_TYPE": "Parcel to Road",
                        "geometry": parcel_stub_geom,
                    })
                if poi_stub_geom is not None:
                    route_records.append({
                        **route_base,
                        "SEGMENT_TYPE": "Road to POI",
                        "geometry": poi_stub_geom,
                    })
                if route_geom is not None:
                    route_records.append({
                        **route_base,
                        "SEGMENT_TYPE": "Full Route",
                        "geometry": route_geom,
                    })

        return row_idx, results, route_records

    except Exception as e:
        if debug:
            print(f"[DIAG] row={row_idx}: worker_process raised: {e}")
        return row_idx, {"_error": str(e)}, route_records


def run_cpu_parallel_with_progress(
    gdf, poi_gdf, road_gdf,
    poi_types, poi_coords_dict, poi_names_dict,
    output_path,
    progress_bar, status_var, eta_var,
    stop_flag,
    debug=False
):


    t0 = time.time()

    G_main, edges_list, nodes_coords = graph_from_roads(road_gdf)
    if len(edges_list) == 0:
        raise Exception("No valid edges found in road network.")

    # Build the edge spatial index ONCE here, after node-merging, and
    # reuse it for every parcel/POI snap below -- this is the
    # "build once, reuse" performance fix. G_main itself is also shared
    # and mutated transactionally per snap (see worker_process /
    # snap_pair_to_network).
    edge_tree, edge_endpoints = build_edge_index(G_main)

    args_list = []
    # Resolve once which column holds the parcel's human-readable PIN
    # (see PARCEL_PIN_COLUMN_CANDIDATES) so poi_routes.gpkg can identify
    # parcels without requiring a join back to parcels_with_poi_distances.gpkg.
    parcel_pin_col = _resolve_parcel_pin_column(gdf)
    if parcel_pin_col is None:
        print(f"⚠️ No PIN/property-identifier column found in parcel layer "
              f"(checked: {PARCEL_PIN_COLUMN_CANDIDATES}). PARCEL_PIN will be "
              f"blank in poi_routes.gpkg -- use PARCEL_IDX to cross-reference instead.")

    for idx, row in gdf.iterrows():
        centroid_xy = (row.geometry.centroid.x, row.geometry.centroid.y)
        parcel_pin = row[parcel_pin_col] if parcel_pin_col else ""
        args_list.append(
            (idx, centroid_xy, poi_types, poi_coords_dict, poi_names_dict, parcel_pin)
        )

    total = len(args_list)
    processed = 0
    errors = 0
    start_time = time.time()

    total_cpus = os.cpu_count() or 1
    cpu_count = max(1, total_cpus - 1)


    all_route_records = []

    for i, args in enumerate(args_list, start=1):
        if stop_flag["stop"]:
            return None

        elapsed = time.time() - start_time
        avg = elapsed / max(1, i)
        remaining = (total - i) * avg

        idx, res, route_records = worker_process(G_main, edge_tree, edge_endpoints, args, debug=debug)

        if "_error" in res:
            continue

        for k, v in res.items():
            gdf.at[idx, k] = v

        all_route_records.extend(route_records)

        progress_bar["value"] = i
        status_var.set(f"Processed {i} / {total} parcels")
        eta_var.set(f"ETA: {remaining/60:.1f} min")

        progress_bar.master.update_idletasks()
        progress_bar.master.update()

    if output_path:
        gdf.to_file(output_path)

        # Export the actual routed paths (parcel -> road stub -> network
        # path -> road stub -> POI) as a separate line layer so results
        # can be visually audited in Global Mapper instead of requiring
        # a manual re-trace with the measure tool every time a distance
        # looks suspicious. Only NET-sourced results have a route (see
        # worker_process: STR fallback rows are skipped here since a
        # straight centroid-to-POI line adds no diagnostic value beyond
        # the _SRC=STR flag already exposed in the main output).
        #
        # Filter by PARCEL_IDX/CATEGORY/RANK in Global Mapper's attribute
        # table to isolate a single route instead of rendering all of
        # them at once -- with up to 5 categories x 3 ranks per parcel,
        # rendering every route for a large dataset at once will be
        # visually overwhelming and slow to draw.
        if all_route_records:
            routes_gdf = gpd.GeoDataFrame(all_route_records, crs=gdf.crs)
            routes_path = os.path.join(
                os.path.dirname(output_path), "poi_routes.gpkg"
            )
            routes_gdf.to_file(routes_path)
            print(f"ℹ️ Exported {len(routes_gdf)} route(s) for visual validation: {routes_path}")
        else:
            print("ℹ️ No NET-sourced routes to export (all results were STR fallback, or no candidates found).")


# -----------------------------------
# Core runner (adapted wrapper)
# -----------------------------------
def run_processing():

    global parcel_source, poi_source, road_source, output_mode


    if parcel_source is None or poi_source is None or road_source is None:
        messagebox.showerror(
            "Error",
            "Incomplete selection.\n\n"
            "Please select:\n"
            "- Land Parcels\n"
            "- POIs\n"
            "- Roads"
        )
        return

    creds = load_db_credentials()
    if not creds:
        messagebox.showerror("Error", "Database credentials not found.")
        return

    schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@"
        f"{creds['host']}:{creds['port']}/{creds['database']}"
    )

    # ---- Load parcels ----
    parcel_gdfs = []
    if parcel_source[0] == "local":
        for p in parcel_source[1]:
            parcel_gdfs.append(gpd.read_file(p))
    else:
        for t in parcel_source[1]:
            parcel_gdfs.append(read_postgis_clean(t, engine, schema))

    gdf = gpd.GeoDataFrame(pd.concat(parcel_gdfs, ignore_index=True), crs=parcel_gdfs[0].crs)

    # ---- Load POIs ----
    if poi_source[0] == "local":
        poi_gdf = gpd.read_file(poi_source[1][0])
    else:
        poi_gdf = read_postgis_clean(poi_source[1][0], engine, schema)

    # ---- Load roads ----
    if road_source[0] == "local":
        road_gdf = gpd.read_file(road_source[1][0])
    else:
        road_gdf = read_postgis_clean(road_source[1][0], engine, schema)

    # ---- CRS ----
    gdf = gdf.to_crs(gdf.estimate_utm_crs())
    poi_gdf = poi_gdf.to_crs(gdf.crs)
    road_gdf = road_gdf.to_crs(gdf.crs)

    # ---- POI filtering ----
    if "fclass" not in [c.lower() for c in poi_gdf.columns]:
        raise Exception("POI 'fclass' column not found.")

    poi_gdf["_fclass_norm"] = poi_gdf["fclass"].astype(str).str.strip().str.lower()
    poi_types = sorted(t for t in poi_gdf["_fclass_norm"].unique() if t in ALLOWED_FCLASS)

    G_main, edges_list, nodes_coords = graph_from_roads(road_gdf)

    poi_coords = {
        t: np.array([[p.x, p.y] for p in poi_gdf[poi_gdf["_fclass_norm"] == t].geometry])
        for t in poi_types
    }

    for t in ALLOWED_FCLASS:
        for i in range(1, 4):
            gdf[f"{t.upper()}{i}"] = np.nan

    args = [
        (idx, (row.geometry.centroid.x, row.geometry.centroid.y),
         poi_types, poi_coords, edges_list, nodes_coords)
        for idx, row in gdf.iterrows()
    ]
    # ---- Output ----
    if output_mode[0] == "local":
        out = os.path.join(output_mode[1], "parcels_with_poi_distances.shp")
        gdf.to_file(out)
        messagebox.showinfo("Success", f"Saved to:\n{out}")
    
# REPLACE WITH

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

    parcel_local_paths = []
    parcel_db_tables   = []
    poi_local_path     = tk.StringVar(master=win)
    poi_db_table       = tk.StringVar(master=win)
    road_local_path    = tk.StringVar(master=win)
    road_db_table      = tk.StringVar(master=win)
    output_local_dir   = tk.StringVar(master=win)

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

    def browse_parcel_db():
        creds = load_db_credentials()
        if not creds:
            messagebox.showerror("Error", "Could not load DB credentials.")
            return
        tables = fetch_tables(creds["schema"])
        if not tables:
            messagebox.showwarning("No Tables", "No tables found in the database schema.")
            return
        _pick_db_tables(win, tables, multi=True,
            on_select=lambda sel: (
                parcel_db_tables.__setitem__(slice(None), sel)
                or parcel_db_label.set(f"{len(sel)} table(s) selected")
            ))

    def _toggle_parcel():
        if parcel_source_type.get() == "local":
            parcel_lbl.config(textvariable=parcel_files_var)
            parcel_btn.config(text="Browse…", command=browse_parcel_files)
        else:
            parcel_lbl.config(textvariable=parcel_db_label)
            parcel_btn.config(text="Select…", command=browse_parcel_db)

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

    def browse_poi_db():
        creds = load_db_credentials()
        if not creds:
            messagebox.showerror("Error", "Could not load DB credentials.")
            return
        tables = fetch_tables(creds["schema"])
        if not tables:
            messagebox.showwarning("No Tables", "No tables found in the database schema.")
            return
        _pick_db_tables(win, tables, multi=False,
            on_select=lambda sel: (
                poi_db_table.set(sel[0]) if sel else None,
                poi_db_var.set(sel[0] if sel else "No table selected")
            ))

    def _toggle_poi():
        if poi_source_type.get() == "local":
            poi_lbl.config(textvariable=poi_file_var)
            poi_btn.config(text="Browse…", command=browse_poi_file)
        else:
            poi_lbl.config(textvariable=poi_db_var)
            poi_btn.config(text="Select…", command=browse_poi_db)

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

    def browse_road_db():
        creds = load_db_credentials()
        if not creds:
            messagebox.showerror("Error", "Could not load DB credentials.")
            return
        tables = fetch_tables(creds["schema"])
        if not tables:
            messagebox.showwarning("No Tables", "No tables found in the database schema.")
            return
        _pick_db_tables(win, tables, multi=False,
            on_select=lambda sel: (
                road_db_table.set(sel[0]) if sel else None,
                road_db_var.set(sel[0] if sel else "No table selected")
            ))

    def _toggle_road():
        if road_source_type.get() == "local":
            road_lbl.config(textvariable=road_file_var)
            road_btn.config(text="Browse…", command=browse_road_file)
        else:
            road_lbl.config(textvariable=road_db_var)
            road_btn.config(text="Select…", command=browse_road_db)

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

    # ── RUN BUTTON ───────────────────────────────────────────────
    ttk.Separator(win, orient="horizontal").pack(
        fill="x", padx=10, pady=(12, 4))

    def on_run():
        global parcel_source, poi_source, road_source, output_mode

        # validate parcel
        if parcel_source_type.get() == "local":
            if not parcel_local_paths:
                messagebox.showerror("Missing Input",
                    "Please select at least one Land Parcel file.")
                return
            parcel_source = ("local", tuple(parcel_local_paths))
        else:
            if not parcel_db_tables:
                messagebox.showerror("Missing Input",
                    "Please select at least one Land Parcel table.")
                return
            parcel_source = ("db", parcel_db_tables)

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

        win.destroy()
        run_with_progress(app_root)

    tk.Button(win, text="▶  Run Processing", command=on_run,
              bg="#2e7d32", fg="white",
              font=("Segoe UI", 10, "bold"),
              relief="flat", padx=16, pady=6).pack(pady=(4, 14))

    _toggle_parcel()
    _toggle_poi()
    _toggle_road()
    _toggle_output()


def run_with_progress(app_root):
    if hasattr(app_root, "_poi_progress_open") and app_root._poi_progress_open:
        return
    app_root._poi_progress_open = True

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

    eta_var = tk.StringVar(value="ETA: calculating...")
    tk.Label(progress_win, textvariable=eta_var).pack(pady=5)

    stop_flag = {"stop": False}

    def cancel():
        stop_flag["stop"] = True
        status_var.set("Cancelling...")

    tk.Button(progress_win, text="Cancel", command=cancel).pack(pady=10)
    progress_win.lift()
    progress_win.focus_force()

    def task():
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

            target_table = None
            if output_mode[0] == "db":
                if parcel_source[0] != "db" or len(parcel_source[1]) != 1:
                    raise Exception(
                        "Database output requires selecting exactly ONE parcel table.")
                target_table = parcel_source[1][0]

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

            # PRS92 per Section E CRS Rules (EPSG 3121-3125), replacing the
            # previous gdf.estimate_utm_crs() (WGS84 UTM 51N) AND the later
            # hardcoded PRS92_EPSG=3123 constant. Zone is now auto-detected
            # per-run from the parcel layer's own geographic extent -- see
            # detect_prs92_zone() for the verified zone boundaries this
            # relies on. Detected once here and reused for poi_gdf/road_gdf
            # so all three layers end up in the exact same CRS.
            prs92_epsg = detect_prs92_zone(gdf)
            gdf = gdf.to_crs(epsg=prs92_epsg)
            poi_gdf = poi_gdf.to_crs(gdf.crs)
            road_gdf = road_gdf.to_crs(gdf.crs)

            status_var.set("Preparing POIs...")
            progress_win.update_idletasks()

            poi_gdf["_fclass_norm"] = poi_gdf["fclass"].str.lower().str.strip()
            poi_types = sorted(
                t for t in poi_gdf["_fclass_norm"].unique()
                if t in ALLOWED_FCLASS
            )

            # Resolve which column identifies each POI by name so the
            # output can say e.g. "Calauan Central Elementary School"
            # instead of a bare, untraceable distance. Falls back to an
            # auto-generated "<fclass>_<n>" label if the POI layer has no
            # recognizable name/label column (see POI_NAME_COLUMN_CANDIDATES).
            poi_name_col = _resolve_poi_name_column(poi_gdf)
            if poi_name_col is None:
                log_message(
                    "⚠️ No name/label column found in POI layer "
                    f"(checked: {POI_NAME_COLUMN_CANDIDATES}). "
                    "Falling back to auto-generated POI labels "
                    "(e.g. 'school_0'). Add a 'name' column to the POI "
                    "source if you want real POI names in SCHOOL1_NM etc."
                )

            poi_coords = {}
            poi_names = {}
            for t in poi_types:
                sub = poi_gdf[poi_gdf["_fclass_norm"] == t]
                # coords[i] and names[i] MUST stay index-aligned -- both are
                # derived from the same `sub` frame in the same pass so
                # pandas row order is guaranteed consistent between them.
                #
                # _point_xy() guards against non-Point POI geometries (e.g.
                # a school digitized as a building-footprint Polygon
                # instead of a single point) by falling back to the
                # geometry's centroid instead of crashing on missing
                # .x/.y attributes.
                poi_coords[t] = np.array([_point_xy(g) for g in sub.geometry])
                if poi_name_col:
                    poi_names[t] = sub[poi_name_col].astype(str).tolist()
                else:
                    poi_names[t] = [f"{t}_{i}" for i in range(len(sub))]

            for t in ALLOWED_FCLASS:
                for i in range(1, 4):
                    # Column order below is intentional and must match the
                    # assignment order in worker_process's ranking loop --
                    # in a GeoDataFrame, column POSITION is fixed at first
                    # creation (here), not at value-assignment time, so
                    # reordering worker_process's dict writes alone would
                    # NOT change the actual output column order.
                    gdf[f"{t.upper()}{i}_NAME"] = ""
                    # "Straight" / "Network" / "Bridged" -- see worker_process
                    # for the exact classification logic.
                    gdf[f"{t.upper()}{i}_METHOD"] = ""
                    gdf[f"{t.upper()}{i}_PARCEL_STUB"] = np.nan
                    gdf[f"{t.upper()}{i}_POI_STUB"] = np.nan
                    # Authoritative distance (Phase 1B promoted) -- already
                    # includes both stubs for Network/Bridged results. No
                    # separate TOTAL_STUB field exists: it was removed as a
                    # fully redundant duplicate of this value in every case
                    # (Network, Bridged, AND Straight alike) once promoted.
                    gdf[f"{t.upper()}{i}"] = np.nan
                    # NaN (not 0) for uncomputed rows -- see the bug this
                    # fixes: a literal 0 default was indistinguishable from
                    # a genuine "Network/Straight result that used zero
                    # bridges", making it impossible to tell whether a
                    # category (e.g. SHOP/TRANSPORT/CHURCH) simply has no
                    # POI features in the source data versus having a real
                    # computed result of 0. Once worker_process actually
                    # computes a result for this cell, it always writes an
                    # explicit integer (0, 1, 2, ...) -- NaN can only mean
                    # "never computed."
                    gdf[f"{t.upper()}{i}_BRIDGE_COUNT"] = np.nan

            output_path = (
                os.path.join(output_mode[1], "parcels_with_poi_distances.gpkg")
                if output_mode[0] == "local"
                else None
            )

            status_var.set("Computing network distances...")
            eta_var.set("ETA: calculating...")
            progress_bar["value"] = 0
            progress_win.update_idletasks()

            run_cpu_parallel_with_progress(
                gdf, poi_gdf, road_gdf,
                poi_types, poi_coords, poi_names,
                output_path,
                progress_bar, status_var, eta_var,
                stop_flag
            )

            if not stop_flag["stop"]:
                if output_mode[0] == "local":
                    load_in_global_mapper(output_path)
                    messagebox.showinfo("Success", "✅ Processing complete!")
                else:
                    all_tables = fetch_tables(schema)
                    table_action = "replaced" if target_table in all_tables else "new"
                    gdf.to_postgis(target_table, engine, schema=schema,
                                   if_exists="replace", index=False)
                    messagebox.showinfo(
                        "Success",
                        f"✅ Updated DB table: {target_table} ({table_action})"
                    )

        except Exception as e:
            messagebox.showerror("Error", str(e))
        finally:
            progress_win.destroy()
            app_root._poi_progress_open = False

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