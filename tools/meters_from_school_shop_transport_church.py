"""
tools/meters_from_school_shop_transport_church.py

PURPOSE:
    CAMA Tools tool ("METERS FROM (SCHOOL, SHOP, TRANSPORT, CHURCH)" in
    MAIN.py's dispatch table): for each Land Parcel, computes network-
    routed distance (falling back to straight-line distance) to the 3
    nearest POIs of each type present in ALLOWED_FCLASS (school, church,
    shop, transport, university), writing up to 3 ranked distance +
    name columns per type (CAMA_{TYPE}{1-3} and its _NAME companion --
    the corresponding POI's own name, or Python None if missing/
    unavailable). Only types actually present in the selected POI
    source get any columns at all, and only ranks that are
    categorically achievable from that type's total POI count (see
    task()'s pre-init notes) -- a type with, say, only 1 total POI
    never gets CAMA_{TYPE}2/3 columns. Tool-style version, mirroring
    road_width.py's overall architecture (progress dialog, DB-output
    resolution flow, window-chrome handling).

DISPATCH:
    Run as an isolated subprocess by MAIN.py via its `--tool` dispatch
    mechanism (see system context). Entry point is main(), triggered via
    the `if __name__ == "__main__":` guard at the bottom of this file.

INPUTS:
    Land Parcel source: a single local file (.shp, .gpkg, or any file
    type via the "All" filter) or a single PostGIS table.
    POI source: a single local file or PostGIS table, with an `fclass`
    column whose values include the ALLOWED_FCLASS types
    (case-insensitive).
    Road Network source: a single local file or PostGIS table with
    LineString/MultiLineString geometry, used to build the routing graph
    (graph_from_roads()).
    pg_credentials.json (via load_db_credentials(), from
    utils/db_discovery.py) for any DB source or DB output.

OUTPUTS:
    Local output mode: writes one atomically-written .gpkg per run
    (_write_gpkg()), then attempts to open it in Global Mapper
    (load_in_global_mapper()).
    DB output mode: writes/replaces one PostGIS table, resolved via
    resolve_db_output_table() -- an exact-match replace for a DB Land
    Parcel source, or a fuzzy-match-with-confirmation flow
    (confirm_db_overwrite_dialog() / choose_db_overwrite_dialog()) for a
    local-file Land Parcel source.

DEPENDENCIES:
    stdlib: os, re, time, datetime, traceback, json, subprocess, ctypes,
    sys, tkinter (+ ttk).
    third-party: geopandas, networkx, shapely, numpy, pandas,
    scipy.spatial.cKDTree, psycopg2, sqlalchemy.
    local: utils.table_name_matching (normalize_name, find_matching_
    tables), utils.resource_path, utils.db_discovery
    (load_db_credentials, fetch_tables), utils.window_icon (apply_icon).

SIDE EFFECTS:
    Network-free (no OSM/Overpass calls -- road network comes from the
    user-selected Road Network source, not downloaded). File
    reads/writes (.shp/.gpkg). PostGIS reads/writes. A live PostgreSQL
    connection. Tkinter GUI windows throughout. A subprocess launch to
    Global Mapper (load_in_global_mapper()) on local-output saves, plus
    a Win32 EnumWindows call to find/focus an already-open Global Mapper
    window first.

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
import time
import datetime
import traceback
import json
import subprocess
import ctypes
import sys
import bisect
import threading
import queue
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox, ttk

import geopandas as gpd
import networkx as nx
from shapely.geometry import Point, LineString
from shapely.strtree import STRtree
from shapely.ops import nearest_points
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
import psycopg2
from sqlalchemy import create_engine, text, inspect

from utils.table_name_matching import normalize_name, find_matching_tables
from utils.resource_path import resource_path
from utils.db_discovery import load_db_credentials, fetch_tables
from utils.window_icon import apply_icon
from utils.gpkg_io import write_gpkg_atomic as _write_gpkg

# ========================================
# WINDOW CHROME HELPERS
# ========================================

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
root = None               # set inside main() -- see MAIN / ENTRYPOINT section
parcel_source = None     # ("local", [paths]) OR ("db", [tables])
poi_source = None        # ("local", [path])  OR ("db", [table])
road_source = None       # ("local", [path])  OR ("db", [table])
output_mode = None       # ("local", out_dir) OR ("db", None)

# PART 2: {raw_normalized_fclass: assigned_CAMA_column_suffix} for
# whichever "Include Other Landmark Types" sub-checkboxes were checked
# at Run time (see on_run()). Empty dict or None -- the default -- means
# no dynamic types are processed, i.e. today's exact ALLOWED_FCLASS-only
# behavior. Populated using the SAME suffix assignment already shown to
# the user in the checklist (see _assign_other_type_column_suffixes()),
# never recomputed from just the checked subset, so what the checklist
# displayed always matches what gets written.
selected_other_poi_column_map = None

ALLOWED_FCLASS = {"school", "church", "shop", "transport", "university"}

# Ordinary fixed-type POI fclass set used by task()'s main
# "ordinary_fixed_types" computation further below -- deliberately
# NARROWER than ALLOWED_FCLASS, which is NOT changed by this addition:
# ALLOWED_FCLASS still protects "SCHOOL"/"UNIVERSITY" as reserved
# suffixes for dynamic "Other Landmark Types" (see
# _assign_other_type_column_suffixes()) and is still consumed by the
# Part 3 pre-check (_get_poi_types_for_check()) -- neither of those is
# touched by this addition. "school", "university", and every other
# education-related fclass (elementary_school, high_school,
# kindergarten, college, etc.) are now routed through the separate,
# unified CAMA_SCHOOL{1-3} pool below (see _is_education_fclass() /
# _classify_education_poi() and task()'s own education-pool
# construction) instead of this ordinary fixed-type path.
ORDINARY_FIXED_FCLASS = {"church", "shop", "transport"}

# Priority-ordered keyword -> CAMA_SCHOOL*_TYPE value mapping, checked
# in this exact order against a POI's own combined fclass+name
# "haystack" (see _classify_education_poi() below) -- first match
# wins. More specific phrases MUST precede less specific ones that
# could otherwise also match the same POI (e.g. "junior high" before
# plain "high school"). ACADEMY is deliberately last among the named
# keywords so a name like "ABC Senior High Academy" resolves to SENIOR
# HIGH, not ACADEMY.
_EDUCATION_TYPE_PRIORITY = [
    ("UNIVERSITY",        ("university",)),
    ("COLLEGE",           ("college",)),
    ("JUNIOR HIGH",       ("juniorhigh",)),
    ("SENIOR HIGH",       ("seniorhigh",)),
    ("HIGH SCHOOL",       ("highschool",)),
    ("SECONDARY SCHOOL",  ("secondaryschool",)),
    ("MIDDLE SCHOOL",     ("middleschool",)),
    ("VOCATIONAL",        ("vocational",)),
    ("MONTESSORI",        ("montessori",)),
    ("ELEMENTARY",        ("elementary",)),
    ("PRIMARY",           ("primary",)),
    ("KINDERGARTEN",      ("kindergarten",)),
    ("PRESCHOOL",         ("preschool",)),
    ("NURSERY",           ("nursery",)),
    ("DAYCARE",           ("daycare",)),
    ("ACADEMY",           ("academy",)),
]


def _classify_education_poi(fclass_norm, name_raw):
    """
    Returns the CAMA_SCHOOL*_TYPE value (e.g. "MONTESSORI") for one POI
    already confirmed eligible for the education pool (see
    _is_education_fclass()) -- or None if no keyword in
    _EDUCATION_TYPE_PRIORITY matched (still part of the pool, simply
    untyped -- e.g. a bare fclass="school" whose name gives no further
    detail).

    Checks a single combined "haystack" -- normalize_name(fclass_norm)
    concatenated with normalize_name(str(name_raw)) -- against every
    keyword in priority order, first match wins. This means a POI whose
    fclass ALONE already identifies its education level (e.g.
    fclass="middle school", fclass="junior_high_school",
    fclass="university") resolves correctly without ever needing to
    look at `name` at all -- the fclass contributes to the same
    haystack the name would, so it is classified automatically.
    `name_raw` may be None/NaN; that becomes an empty-string
    contribution to the haystack, never a crash.

    normalize_name() (from utils.table_name_matching, already imported
    at the top of this file) lowercases and strips every non-letter
    character, so "Junior High School", "junior_high_school", and
    "junior-high-school" all normalize identically -- one keyword
    ("juniorhigh") covers every spacing/punctuation variant without
    needing to enumerate each one.
    """
    haystack = normalize_name(fclass_norm) + normalize_name(str(name_raw or ""))
    for type_value, keywords in _EDUCATION_TYPE_PRIORITY:
        if any(kw in haystack for kw in keywords):
            return type_value
    return None


def _is_education_fclass(fclass_norm):
    """
    Eligibility gate for the unified CAMA_SCHOOL pool -- deliberately a
    SEPARATE decision from _classify_education_poi()'s TYPE
    classification: whether a POI enters the pool at all must not
    depend on which specific keyword its name happens to contain.

    A normalized fclass is eligible if it contains "school" anywhere
    (catches "school" itself and every literal variant like
    "elementary_school", "high school", "day care school",
    "nursery-school", "academy school", etc. -- ANY fclass that
    literally says "school" is eligible, regardless of which
    education-level keyword also appears within it), OR if it is
    exactly "university", "college", "kindergarten", or "preschool"
    (eligible even without the word "school" appearing at all, since
    these four are themselves unambiguous education-level fclass
    values in common use).

    Deliberately NOT eligible: bare "daycare", "nursery", or "academy"
    (no "school" in them, and not one of the four exact-match values
    above) -- these three are TYPE values ONLY, reached exclusively via
    a POI whose fclass already qualifies through another route
    (typically bare fclass="school") with one of these words appearing
    in its `name`. This is intentional, not an oversight: e.g.
    fclass="daycare" alone must never enter the pool, while
    fclass="day care school" must (it contains "school").
    """
    n = normalize_name(fclass_norm)
    if "school" in n:
        return True
    return n in ("university", "college", "kindergarten", "preschool")

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


# ========================================
# LOGGING
# ========================================
def log_message(msg, logfile=None, flush=True):
    """Prints a timestamped message, optionally appending it to logfile."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=flush)
    if logfile:
        with open(logfile, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ========================================
# DB HELPERS
# ========================================
# Same pattern as road_width.py's own DB helpers -- kept file-local
# rather than imported from utils/db_discovery.py, which does not
# currently cover geometry-column lookup or PostGIS-to-GeoDataFrame
# reading (see utils/db_discovery.py's own "Planned future architecture"
# note re: a future db_schema.py). Not consolidated here -- see Section
# 3.E.7 of the governing instructions (no cross-file deduplication in
# this task).
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
        q = text("""
            SELECT f_geometry_column
            FROM geometry_columns
            WHERE f_table_schema=:s AND f_table_name=:t
        """)
        r = conn.execute(q, {"s": schema, "t": table}).fetchone()
        return r[0] if r else None


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

    Raises:
        ValueError: if the table has no detectable geometry column.
    """
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


# ========================================
# OUTPUT-COLUMN CONFLICT DETECTION
# ========================================
# This tool's output columns are dynamic, not a fixed list -- they
# depend on which POI fclass types (from ALLOWED_FCLASS) actually
# appear in the selected POI source. Unlike road_frontage.py/terrain.py/
# land_shape_compactness.py's fixed OUTPUT_COLUMN_TARGETS tuples, this
# tool builds its target list per-run via _realizable_targets() below.
#
# Business decision (confirmed): only check for types that are actually
# PRESENT in the selected POI source this run -- if there's no
# "university" POI in the selected POI layer, there's no reason to
# check for a pre-existing CAMA_UNIVERSITY1 conflict. Every rank that
# is categorically achievable for a present type (per that type's own
# total POI count -- see task()'s pre-init notes) is checked, plus each
# rank's _NAME companion column, e.g. present type "school" with 3+
# total POIs -> CAMA_SCHOOL1, CAMA_SCHOOL1_NAME, CAMA_SCHOOL2,
# CAMA_SCHOOL2_NAME, CAMA_SCHOOL3, CAMA_SCHOOL3_NAME.
#
# RESOLVED (previously flagged here as an accepted, unfixed gap): the
# main-output pre-init loop in task() below now iterates fixed_types
# (present-only) and rank-caps via min(3, that type's total POI count)
# instead of unconditionally pre-creating all three ranks for the full
# static ALLOWED_FCLASS set -- a type absent from this run's POI source
# gets no columns at all, and a present type with fewer than 3 total
# POIs only gets the ranks it could ever actually populate. The two
# steps (this conflict CHECK, and the actual pre-init) are therefore
# consistent with each other again.
def _sanitize_fclass_to_suffix(normalized_fclass):
    """
    PART 2: converts one already-normalized (lowercase, stripped)
    fclass value into a valid CAMA_ column-name suffix: any run of
    non-alphanumeric characters becomes a single underscore, repeated
    underscores collapse to one, leading/trailing underscores are
    stripped, and the result is uppercased.

    The result is guaranteed non-empty: if sanitization leaves nothing
    (e.g. the raw value was purely punctuation/whitespace/symbols), a
    fixed placeholder ("OTHER") is used instead. This placeholder is
    NOT special-cased for collisions -- it flows through
    _assign_other_type_column_suffixes() exactly like any other
    suffix, so two different unsanitizable values still get correctly
    disambiguated from each other rather than silently colliding.

    This function does NOT check for collisions with other dynamic
    types or with the fixed ALLOWED_FCLASS column names -- that is
    _assign_other_type_column_suffixes()'s job, since collision
    resolution requires seeing every candidate at once.
    """
    suffix = re.sub(r"[^0-9A-Za-z]+", "_", normalized_fclass)
    suffix = re.sub(r"_+", "_", suffix).strip("_")
    suffix = suffix.upper()
    if not suffix:
        suffix = "OTHER"
    return suffix


def _int_to_letter_tier(n):
    """
    PART 2: deterministic 0-indexed integer -> letter-tier string,
    used only to disambiguate colliding sanitized suffixes (0->"A",
    1->"B", ..., 25->"Z", 26->"AA", 27->"AB", ...). A LETTER tier is
    used specifically because the existing CAMA_{TYPE}{1-3} convention
    already appends a bare digit (1/2/3) for rank -- disambiguating
    with digits too (e.g. "_1") would make "CAMA_SUFFIX_11" ambiguous
    between disambiguator "_1" + rank "1" and a literal rank "11".
    Letters can never be confused with that numeric rank suffix.
    """
    n += 1
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _assign_other_type_column_suffixes(other_types):
    """
    PART 2: assigns a valid, mutually-distinct, collision-safe CAMA_
    column-name suffix to every distinct "other" (non-ALLOWED_FCLASS)
    fclass value discovered in a POI source.

    Args:
        other_types: iterable of distinct, already-normalized
        (lowercase, stripped) fclass strings. Must NOT include any
        value already in ALLOWED_FCLASS -- callers are responsible for
        excluding those upstream (they are always processed via the
        existing fixed-type path instead, unaffected by this
        function).

    Returns:
        dict: {normalized_fclass: column_suffix}. Every value is
        non-empty (see _sanitize_fclass_to_suffix()), every value is
        distinct from every other value in this dict, and no value
        ever equals an upper-cased ALLOWED_FCLASS name (SCHOOL,
        CHURCH, SHOP, TRANSPORT, UNIVERSITY) -- even if a type's own
        sanitized form would otherwise exactly match one (e.g. a raw
        fclass value like "school!" sanitizes to "SCHOOL", which would
        silently collide with the existing fixed CAMA_SCHOOL* columns
        if not caught here).

        Collision handling (never silently drops a type): types are
        grouped by their sanitized base suffix (via
        _sanitize_fclass_to_suffix()). A group's single member keeps
        that base suffix as-is ONLY if it doesn't collide with a fixed
        ALLOWED_FCLASS name. Any group with more than one member, or
        whose base suffix collides with a fixed name, has EVERY member
        disambiguated to "{base}_{letter}" (A, B, C, ... -- see
        _int_to_letter_tier()), assigned in a deterministic order
        (sorted by the member's own normalized fclass string) so the
        same POI source always produces the same assignment across
        repeated reads -- required since the checklist and task()'s
        actual column-writing step must always agree (see
        selected_other_poi_column_map's module-level docstring).
    """
    reserved_fixed_suffixes = {f.upper() for f in ALLOWED_FCLASS}

    base_suffix_groups = {}
    for t in sorted(set(other_types)):
        base = _sanitize_fclass_to_suffix(t)
        base_suffix_groups.setdefault(base, []).append(t)

    result = {}
    for base, members in base_suffix_groups.items():
        needs_disambiguation = len(members) > 1 or base in reserved_fixed_suffixes
        if not needs_disambiguation:
            result[members[0]] = base
            continue
        for i, t in enumerate(sorted(members)):
            result[t] = f"{base}_{_int_to_letter_tier(i)}"
    return result


def _realizable_targets(poi_types):
    """
    Builds this run's actual list of CAMA_-prefixed target column names
    for the MAIN CAMA output -- only for POI types present in poi_types
    (already filtered to ALLOWED_FCLASS and normalized/lowercased
    upstream, or already a pre-assigned dynamic suffix -- see task()).
    Six targets per type, interleaved per rank: CAMA_{TYPE}1,
    CAMA_{TYPE}1_NAME, CAMA_{TYPE}2, CAMA_{TYPE}2_NAME, CAMA_{TYPE}3,
    CAMA_{TYPE}3_NAME -- matching the exact column order task() now
    pre-creates in the main output (see its pre-init loop).

    NOTE: this always checks all three ranks unconditionally, even
    though task()'s actual pre-init step only creates a rank's columns
    when that rank is categorically achievable (>= that many total
    POIs of that type in the source). This function has no access to
    per-type POI counts -- it only ever receives a list of type name
    strings, at both of its call sites (task() below, and on_run()'s
    lighter pre-check earlier in this file) -- and checking for a
    conflict on a column name that happens not to exist is harmless:
    it simply never matches, never produces a false conflict. Trading
    a little unnecessary over-checking here avoids plumbing per-type
    POI counts through the pre-check flow, which is out of scope for
    this change.

    _METHOD is intentionally NOT generated here -- this is the MAIN
    CAMA output's target list only. The separate, dormant
    poi_routes.gpkg QA/diagnostic export (see worker_process()'s
    route_records) is completely untouched by this change and still
    carries its own METHOD internally; it has no target-list/conflict-
    check concept of its own today, so there is nothing to update for
    it here.
    SCHOOL is a deliberate, narrow special case: it represents the
    unified education pool (see task()'s own construction of it, and
    _classify_education_poi()/_is_education_fclass() above), which
    carries a third per-rank value -- CAMA_SCHOOL{N}_TYPE -- alongside
    its distance and name. No other type in poi_types has this third
    dimension, so this is intentionally NOT a generic mechanism applied
    to every type; it only fires when t.upper() == "SCHOOL".
    """
    targets = []
    for t in poi_types:
        for i in range(1, 4):
            targets.append(f"CAMA_{t.upper()}{i}")
            targets.append(f"CAMA_{t.upper()}{i}_NAME")
            if t.upper() == "SCHOOL":
                targets.append(f"CAMA_{t.upper()}{i}_TYPE")
    return targets


def _read_poi_fclass_values_worker(source_type, path_or_table):
    """
    PART 2: runs on a background thread (see open_main_window()'s
    _refresh_poi_landmark_types()). Fresh, standalone read of the given
    POI source that returns EVERY distinct normalized fclass value
    present -- not filtered to ALLOWED_FCLASS -- for the "Include Other
    Landmark Types" checklist.

    Deliberately separate from _get_poi_types_for_check() above: that
    function stays exactly as it was (ALLOWED_FCLASS-filtered,
    Run-time-only, called from on_run()'s existing-output-column
    conflict pre-check -- a Part 3 concern to relocate, not touched by
    this change). This function serves a different purpose (populating
    the dynamic checklist) and is never reused by that pre-check.

    Never touches any Tkinter widget or variable -- returns
    (fclass_values, error) only, matching road_width.py's
    _read_gdf_worker() contract, so it is safe to call from a
    background thread.

    Returns:
        tuple: (sorted list of distinct non-empty normalized fclass
        strings, None) on success, or (None, error_message) on
        failure (missing 'fclass' column, unreadable source, DB
        credential failure, etc.) -- treated by the caller as
        purely informational; see _poll_poi_landmark_queue()'s own
        docstring for why a failure here never invalidates the
        already-selected POI source.
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
        if "fclass" not in gdf.columns:
            return None, "POI source has no 'fclass' column."
        fclass_norm = gdf["fclass"].astype(str).str.lower().str.strip()
        values = sorted(v for v in fclass_norm.unique() if v)
        return values, None
    except Exception as e:
        return None, str(e)


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


# ========================================
# ROAD GRAPH CONSTRUCTION
# ========================================
def graph_from_roads(road_gdf):
    """
    Builds an undirected networkx graph from a road GeoDataFrame's
    LineString/MultiLineString geometry, one edge per consecutive
    coordinate pair (i.e. no simplification -- every vertex in the
    input geometry becomes a graph node).

    Args:
        road_gdf (geopandas.GeoDataFrame): road network layer. Non-line
        geometry (Polygons, Points) is silently skipped.

    Returns:
        tuple: (G, edges, nodes_coords, edge_geoms) where G is the
        networkx.Graph, edges is a list of (u, v, length) tuples,
        nodes_coords is an (N, 2) numpy array of every distinct node
        coordinate (for nearest-neighbor queries elsewhere), and
        edge_geoms is a list of shapely LineString objects -- one per
        entry in `edges`, in the same order/index -- built for an
        edge-level STRtree spatial index so callers can snap an
        arbitrary point to its true nearest point ON a road segment
        (not just to the nearest existing vertex; see worker_process()
        below).

    Raises:
        Exception: if road_gdf has no LineString/MultiLineString
        geometry at all.
    """
    G = nx.Graph()
    edges = []
    edge_geoms = []
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
                    edge_geoms.append(LineString([u, v]))
                    nodes_coords.add(u)
                    nodes_coords.add(v)
        except Exception:
            continue

    nodes_coords = np.array(list(nodes_coords)) if nodes_coords else np.zeros((0, 2))
    return G, edges, nodes_coords, edge_geoms


# ========================================
# ROUTING WORKER
# ========================================
# Snapping (PART 1 fix): a parcel/POI point is now snapped to the true
# geometric nearest point on the road network -- interpolated along a
# road segment via an edge-level STRtree + nearest_points() projection
# if that is closer than any existing vertex, inserted as a "virtual"
# graph node connected to that edge's two endpoints -- instead of the
# previous nearest-existing-vertex-only snap. Same silent-to-
# straight-line fallback behavior as before on any lookup/projection/
# routing failure. Also returns a METHOD label and the route geometry,
# so what the baseline was already doing internally becomes visible
# instead of hidden.
def worker_process(args):
    """
    For one parcel centroid, finds the 3 nearest POIs of each type in
    poi_types and computes the distance to each: network-routed via
    the road graph if a path exists, else straight-line.

    Args:
        args: (row_idx, centroid_xy, poi_types, poi_coords_dict,
        poi_names_dict, poi_types_tag_dict, edges_list, nodes_coords,
        edge_geoms) tuple -- packed this way so this function can be
        called uniformly whether or not the caller parallelizes (see
        run_cpu_parallel_with_progress(), which currently calls this
        sequentially). poi_names_dict mirrors poi_coords_dict exactly
        (same keys, same per-type ordering) -- type -> list of each
        POI's own "name" attribute value (Python None where missing or
        where the source has no name column at all -- never the
        string "None", never an empty string). poi_types_tag_dict is
        SPARSE -- it only ever has a "SCHOOL" key (the unified
        education pool; see task()'s own construction of it and
        _classify_education_poi()/_is_education_fclass() above), with
        the same per-index ordering as poi_coords_dict["SCHOOL"]/
        poi_names_dict["SCHOOL"] -- type -> CAMA_SCHOOL*_TYPE
        classification value (e.g. "MONTESSORI") or None per POI. Every
        other type has no entry in this dict at all, and is completely
        unaffected -- this is a deliberate, narrow addition for the
        school pool only, not a generic third dimension for every type.
        Built from the SAME filtered subset as poi_coords_dict for each
        type (see task()), so a given index pi always refers to the
        same POI across all three dicts. edge_geoms is a picklable
        list[LineString] (one per edges_list entry, same index) -- NOT
        a live STRtree, which is rebuilt fresh inside this function
        from edge_geoms, matching the existing rebuild-per-call
        convention already used for nodes_kdtree/G_local, so this
        function stays safe to call from a future multiprocessing pool
        even though it currently runs sequentially.

    Returns:
        tuple: (row_idx, results, route_records). results is a dict of
        CAMA_{TYPE}{1-3} / CAMA_{TYPE}{1-3}_NAME -> value for this
        parcel's MAIN output row (the corresponding POI's own name, or
        None if missing/unavailable -- never written for a rank this
        type doesn't have enough total POIs to reach; see task()'s
        pre-init notes), PLUS CAMA_SCHOOL{1-3}_TYPE for the school pool
        specifically (see poi_types_tag_dict above). route_records is a
        list of per-route dicts (parcel index, category, rank, METHOD,
        distance, route geometry) used ONLY by the separate, still-
        disabled poi_routes.gpkg QA/diagnostic export (see its own
        comment further below) -- this is intentionally untouched by
        the MAIN-output METHOD->NAME change: the `method` variable
        computed below is still fully alive and still flows into
        route_records exactly as before. On error, results is
        {"_error": str(e)} instead.
    """
    (row_idx, centroid_xy, poi_types, poi_coords_dict, poi_names_dict, poi_types_tag_dict, edges_list, nodes_coords, edge_geoms) = args
    route_records = []  # (typ, rank, method, dist, geometry)
    try:
        if len(nodes_coords) == 0:
            return row_idx, {}, route_records

        G_local = nx.Graph()
        for u, v, length in edges_list:
            G_local.add_edge(tuple(u), tuple(v), length=float(length))

        # Edge-level spatial index for true nearest-point-on-road
        # snapping (PART 1 fix). Rebuilt fresh from the picklable
        # edge_geoms list on every call, matching the existing
        # rebuild-per-call convention already used above for G_local
        # (and previously for nodes_kdtree) -- see the args docstring
        # note on why a live STRtree is never passed through args.
        edge_tree = STRtree(edge_geoms) if edge_geoms else None

        # Per-edge ordered "chain" of every point placed on that edge
        # so far during THIS call: edge_idx -> sorted list of
        # (proj_dist_along_edge, node_id), always seeded lazily with
        # the edge's own two original endpoints (proj_dist 0 and
        # elen). Needed so that if a second point (e.g. the centroid's
        # start-snap and a POI's end-snap) lands on the SAME edge as
        # an earlier point, it gets connected to its correct
        # immediate neighbor(s) on that edge -- not just to that
        # edge's far-apart original endpoints, which would silently
        # produce a too-long route between two points that are
        # actually close together on the same segment.
        edge_chains = {}

        def _snap_to_road(point_xy):
            """
            Projects point_xy onto its true nearest point on the road
            network (nearest-point-on-line -- interpolated along a
            segment if that is closer than any existing vertex, not
            restricted to existing vertices only) and returns the
            (x, y) coordinate tuple to use as a G_local routing
            endpoint.

            If the edge this point snaps to has not been touched
            before in this call, the new point is inserted as a
            virtual node connected to that edge's two original
            endpoints (distance-weighted), same as before. If the
            edge HAS already had one or more points placed on it
            earlier in this same call, the new point is instead
            spliced into the existing ordered chain of points on that
            edge: it is connected only to its immediate left/right
            neighbors on the edge (with the correct sub-distances),
            and the single now-superseded edge directly between those
            two neighbors is removed -- so two points on the same
            road segment always route at their true along-segment
            distance from each other, not via that segment's far
            endpoints.

            The virtual node's ID is its own (x, y) coordinate tuple
            -- required because route_geom = LineString(path) further
            below assumes every graph node ID IS an (x, y) coordinate,
            same as every existing road-vertex node ID already is.

            Collision handling: if the projected coordinate already
            matches an existing node in G_local -- an original road
            vertex, or a virtual node inserted earlier in THIS SAME
            call (e.g. the projection lands exactly on an existing
            vertex, or two different input points project to the same
            location) -- that existing node is reused as-is; no new
            node or edges are inserted, avoiding both an unintended
            overwrite of that node's existing edges and a zero-length
            self-loop.

            Returns:
                tuple[float, float] | None: the node ID to route
                to/from, or None if the edge lookup or projection
                fails for any reason. A None here is treated by the
                caller exactly like "no path found" -- it falls back
                to the existing straight-line distance, never raises.
            """
            if edge_tree is None:
                return None
            try:
                nearest = edge_tree.nearest(Point(point_xy))
                if isinstance(nearest, (int, np.integer)):
                    edge_idx = int(nearest)
                else:
                    # Older shapely returns the geometry itself; recover
                    # its index for the parallel edges_list lookup --
                    # same int-vs-geometry compatibility pattern already
                    # used in influence_to_map.py's process_parcels().
                    edge_idx = edge_geoms.index(nearest)

                edge_line = edge_geoms[edge_idx]
                eu, ev, elen = edges_list[edge_idx]
                eu = tuple(eu)
                ev = tuple(ev)

                projected = nearest_points(Point(point_xy), edge_line)[1]
                node_id = (float(projected.x), float(projected.y))

                # Coordinate-based collision check FIRST, independent
                # of which edge_idx this lookup landed on -- if this
                # exact point is already a node anywhere in G_local
                # (original vertex or earlier virtual node), reuse it
                # outright rather than touching any chain bookkeeping.
                if node_id in G_local:
                    return node_id

                chain = edge_chains.get(edge_idx)
                if chain is None:
                    # First point on this edge this call -- seed the
                    # chain with the edge's own original endpoints.
                    # The base edge (eu, ev, elen) already exists in
                    # G_local from the initial build above, so nothing
                    # needs to change in the graph yet.
                    chain = [(0.0, eu), (float(elen), ev)]
                    edge_chains[edge_idx] = chain

                proj_dist = float(edge_line.project(projected))
                positions = [c[0] for c in chain]
                insert_at = bisect.bisect_left(positions, proj_dist)

                # nearest_points() guarantees proj_dist falls within
                # [0, elen], and the chain always spans that full
                # range (seeded with both endpoints), so a left AND a
                # right neighbor always exist here.
                left_pos, left_id = chain[insert_at - 1]
                right_pos, right_id = chain[insert_at]

                if G_local.has_edge(left_id, right_id):
                    G_local.remove_edge(left_id, right_id)

                G_local.add_edge(node_id, left_id, length=float(proj_dist - left_pos))
                G_local.add_edge(node_id, right_id, length=float(right_pos - proj_dist))

                chain.insert(insert_at, (proj_dist, node_id))
                return node_id
            except Exception:
                return None

        # The centroid's true nearest-point-on-road snap is the same
        # for every POI type/candidate queried below -- computed once
        # here instead of being recomputed identically on every
        # candidate iteration (previous behavior queried this inside
        # the innermost loop even though centroid_xy never changes
        # within this call).
        start = _snap_to_road(centroid_xy)

        results = {}
        for typ in poi_types:
            coords = poi_coords_dict.get(typ)
            if coords is None or len(coords) == 0:
                continue
            names = poi_names_dict.get(typ, [])
            # Sparse: None for every type except "SCHOOL" (the unified
            # education pool) -- see poi_types_tag_dict's own docstring
            # note above.
            types_tag = poi_types_tag_dict.get(typ)

            k = min(3, len(coords))
            tree = cKDTree(coords)
            _, idxs = tree.query([centroid_xy], k=k)

            idxs = [int(idxs[0])] if k == 1 else [int(i) for i in idxs[0]]

            network_results = []
            for pi in idxs:
                poi_xy = coords[pi]
                poi_name = names[pi] if pi < len(names) else None
                poi_type_tag = types_tag[pi] if types_tag is not None and pi < len(types_tag) else None
                end = _snap_to_road(poi_xy)

                method = "Straight"
                route_geom = LineString([centroid_xy, tuple(poi_xy)])
                try:
                    if start is not None and end is not None and nx.has_path(G_local, start, end):
                        dist, path = nx.bidirectional_dijkstra(G_local, start, end, weight="length")
                        method = "Road"
                        route_geom = LineString(path)
                    else:
                        dist = Point(centroid_xy).distance(Point(poi_xy))
                except Exception:
                    dist = Point(centroid_xy).distance(Point(poi_xy))

                network_results.append((round(dist, 2), method, route_geom, poi_name, poi_type_tag))

            network_results = sorted(network_results, key=lambda r: r[0])
            for i, (dist, method, route_geom, poi_name, poi_type_tag) in enumerate(network_results[:3], start=1):
                results[f"CAMA_{typ.upper()}{i}"] = float(dist)
                results[f"CAMA_{typ.upper()}{i}_NAME"] = poi_name
                if types_tag is not None:
                    results[f"CAMA_{typ.upper()}{i}_TYPE"] = poi_type_tag
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
    poi_types, poi_coords_dict, poi_names_dict, poi_types_tag_dict,
    output_path,
    progress_bar, status_var,
    stop_flag,
    original_crs=None,
):
    """
    Builds the road graph once, then runs worker_process() sequentially
    (despite the name -- see Notes) for every parcel centroid in gdf,
    writing results back into gdf and updating progress_bar/status_var
    per parcel.

    Args:
        gdf, poi_gdf, road_gdf (geopandas.GeoDataFrame): parcel, POI,
        and road layers, already reprojected to a shared working CRS.
        poi_types (list[str]): POI types to compute distances for.
        poi_coords_dict (dict): type -> (N, 2) numpy array of POI
        coordinates.
        poi_names_dict (dict): type -> list of each POI's own "name"
        attribute value, same keys and same per-index ordering as
        poi_coords_dict (built from the identical filtered subset per
        type -- see task()) -- Python None where a name is missing or
        the POI source has no name column at all, never the string
        "None" or an empty string. Threaded straight through to
        worker_process() via args_list below for the MAIN output's
        CAMA_{TYPE}{N}_NAME columns.
        poi_types_tag_dict (dict): SPARSE -- only ever has a "SCHOOL"
        key (the unified education pool; see task()'s own construction
        of it), same per-index ordering as poi_coords_dict["SCHOOL"] --
        type -> list of each POI's CAMA_SCHOOL*_TYPE classification
        value (e.g. "MONTESSORI") or None. Every other type has no
        entry here at all. Threaded straight through to
        worker_process() for the MAIN output's CAMA_SCHOOL{N}_TYPE
        columns.
        output_path (str | None): if given, the result is written here
        via _write_gpkg() after processing (local output mode). None
        for DB output mode, where the caller writes to PostGIS instead.
        progress_bar, status_var: Tkinter widgets updated per parcel.
        stop_flag (dict): {"stop": bool} -- checked once per parcel;
        note this only stops BETWEEN parcels, not mid-computation (see
        Notes).
        original_crs: the parcel layer's CRS before reprojection, used
        to reproject the result back before saving.

    Returns:
        None always, currently. See the disabled poi_routes.gpkg
        export code further below for what a non-None return used to
        mean.

    Notes:
        Despite the function name, this currently runs worker_process()
        in a plain sequential for-loop, not a multiprocessing pool --
        cpu_count is computed but not otherwise used. The
        poi_routes.gpkg diagnostic/audit output (a GeoDataFrame of
        every computed route with its Road/Straight method label) is
        computed but its write-out is deliberately disabled (see the
        commented-out block below) so a run always produces exactly one
        output file.
    """
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

    G_main, edges_list, nodes_coords, edge_geoms = graph_from_roads(road_gdf)
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
            (idx, centroid_xy, poi_types, poi_coords_dict, poi_names_dict, poi_types_tag_dict, edges_list, nodes_coords, edge_geoms)
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

        # ------------------------------------------------------------------
        # poi_routes.gpkg write -- DISABLED (commented out, not removed).
        # Per-task decision to suppress this secondary/diagnostic-only
        # routes/audit output so a successful Run Processing always
        # produces exactly ONE output file per tool. all_route_records
        # itself is still computed above (line 569's initialization, line
        # 587's per-parcel .extend()) -- route_records is a byproduct of
        # the same worker_process() loop that produces the main
        # CAMA_{TYPE}{N}/CAMA_{TYPE}{N}_METHOD output columns -- only this
        # write (and its own early `return routes_path`) is disabled.
        # This function's existing `return None` immediately below (kept
        # active, untouched) already covers every resulting code path: it
        # was already the fallback whenever output_path was falsy (DB-
        # output mode) or all_route_records was empty, and now it is the
        # ONLY reachable return, since nothing above it can early-return
        # anymore. The caller's existing `if routes_path:` guard (see
        # run_cpu_parallel_with_progress()'s single call site) continues
        # to work exactly as before, just always taking the "no routes
        # layer" path.
        # ------------------------------------------------------------------
        # if all_route_records:
            # routes_gdf = gpd.GeoDataFrame(all_route_records, crs=projected_crs)
            # routes_path = os.path.join(os.path.dirname(output_path), "poi_routes.gpkg")
            # if original_crs is not None:
                # routes_gdf = routes_gdf.to_crs(original_crs)
            # _write_gpkg(routes_gdf, routes_path)
            # print(f"ℹ️ Exported {len(routes_gdf)} route(s) with Road/Straight labels: {routes_path}")
            # return routes_path
    return None


# ========================================
# GUI DIALOGS / HELPERS
# ========================================
def _pick_db_tables(parent, tables, multi, on_select):
    """
    Simple modal listbox dialog for picking one (multi=False) or more
    (multi=True) table names from `tables`. Calls on_select(selection)
    and closes itself once the user confirms a non-empty selection;
    stays open otherwise (no explicit cancel button -- closing the
    window is the only way to back out without selecting).

    Args:
        parent: parent Tk window.
        tables (list[str]): table names to list.
        multi (bool): whether multiple selection is allowed.
        on_select (callable): called with the list of selected names.
    """
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


def ask_overwrite_dialog(parent, conflicting_names):
    """
    Modal dialog shown when one or more local output files already
    exist. Lets the user choose to overwrite all of them, save all
    under new (non-colliding) names instead, or cancel the run
    entirely -- one choice applies to every listed file, there is no
    per-file choice.

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
    """
    Opens filepath in Global Mapper. First tries to find an already-open
    Global Mapper window (via a Win32 EnumWindows title-text scan) so a
    running instance can pick up the new file, then launches
    GM_EXE_PATH as a subprocess regardless of whether an existing
    window was found. Any failure (GM_EXE_PATH not existing, launch
    failure, etc.) is caught and only printed, never raised or shown to
    the user.

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
# MAIN WINDOW
# ========================================
def open_main_window(app_root):
    """
    Builds and shows the tool's single unified configuration window:
    four source/destination pickers (Land Parcel, POI, Road Network,
    Output) each with a Local-file/Database-table radio toggle, plus a
    Run button gated by _update_run_button_state().

    This is a large function (~550 lines) because all four
    source-selection blocks (Land Parcel / POI / Road Network / Output)
    follow the same repeated shape -- a radio toggle, a browse/select
    button, a state variable, and a toggle handler -- rather than a
    shared helper, matching this codebase's existing convention of
    per-tool GUI code (see Section 3.E.7 of the governing instructions:
    no cross-file/cross-block deduplication in this task). on_run() is
    defined here as a nested closure since it needs direct access to
    all four blocks' local state.

    Args:
        app_root: the parent Tk root this window is opened under.
    """
    win = tk.Toplevel(app_root)
    apply_icon(win, "distancefrom.ico")
    win.title("Meters From (School, Shop, Transport, Church) Tool")
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
    # PART 2: captured as named references (previously anonymous) so
    # _set_poi_reading_state() below can disable them while the
    # background landmark-type discovery read is in progress, matching
    # road_width.py's parcel_radio_local/parcel_radio_db precedent.
    poi_radio_local = tk.Radiobutton(poi_radio_row, text="Local File",
                   variable=poi_source_type, value="local",
                   command=lambda: _toggle_poi())
    poi_radio_local.pack(side="left")
    poi_radio_db = tk.Radiobutton(poi_radio_row, text="Database Table",
                   variable=poi_source_type, value="db",
                   command=lambda: _toggle_poi())
    poi_radio_db.pack(side="left", padx=(12, 0))

    poi_file_var = tk.StringVar(master=win, value="No file selected")
    poi_db_var   = tk.StringVar(master=win, value="No table selected")

    poi_action_row = tk.Frame(poi_frame)
    poi_action_row.pack(fill="x", pady=2)

    poi_lbl = tk.Label(poi_action_row, textvariable=poi_file_var,
                       fg="gray", anchor="w", width=42)
    poi_lbl.pack(side="left")

    poi_btn = tk.Button(poi_action_row, text="Browse…", width=10)
    poi_btn.pack(side="left", **PAD)

    # ── PART 2: "Include Other Landmark Types" state ────────────────
    # poi_types_reading: guards against overlapping background
    # discovery reads and gates the Run button (see
    # _update_run_button_state() below) -- distinct from, and never
    # affected by, which/whether landmark sub-checkboxes are checked.
    poi_types_reading = False
    # other_landmark_type_vars: {display_fclass: tk.BooleanVar},
    # rebuilt from scratch on every fresh discovery read -- never
    # merged/cached across POI source reselections, matching the
    # established no-cache convention already used for "Filter by Road
    # Type" in road_width.py.
    other_landmark_type_vars = {}
    # other_landmark_column_suffixes: {display_fclass: column_suffix},
    # the SAME assignment shown in the checklist -- reused verbatim by
    # on_run() so what's displayed always matches what gets written
    # (see _assign_other_type_column_suffixes()'s own docstring).
    other_landmark_column_suffixes = {}

    include_other_landmarks_var = tk.BooleanVar(master=win, value=False)

    # other_landmarks_header_row: shared row holding the "Include
    # Other Landmark Types" checkbox AND the "Check All" / "Uncheck
    # All" hyperlink-style labels on the SAME line -- no extra row,
    # no extra vertical space. The row itself is packed/unpacked by
    # _update_other_landmarks_visibility() below using the same
    # "has content" gate the checkbox alone previously used (never
    # destroyed, matching road_width.py's road_filter_checkbox
    # convention, plus this tool's own "has content" gate that
    # road_filter_checkbox does not need). The two link labels are
    # NOT gated by this row's own visibility alone -- they have their
    # own independent, narrower pack/pack_forget (checked AND has
    # content -- the exact same condition the checklist itself uses),
    # since "Check All"/"Uncheck All" are only meaningful once the
    # checklist they operate on is actually visible.
    other_landmarks_header_row = tk.Frame(poi_frame)

    include_other_checkbox = tk.Checkbutton(
        other_landmarks_header_row, text="Include Other Landmark Types",
        variable=include_other_landmarks_var,
        command=lambda: _on_include_other_toggled())
    include_other_checkbox.pack(side="left")

    # other_landmarks_links_frame: groups "Check All" / "|" / "Uncheck
    # All" together and packs the GROUP to the far right edge of the
    # header row (side="right") -- rather than simply appending them
    # after the checkbox text, which is what previously made the row
    # (and therefore the window) grow wider than intended. Hyperlink-
    # style labels: plain tk.Label styled to look clickable (blue,
    # underlined, hand cursor), bound to <Button-1> -- there is no
    # native Tkinter "link" widget. Text is static Title Case in both
    # states (never toggles to reflect current selection), per the
    # approved spec -- "Check All" always checks everything, "Uncheck
    # All" always unchecks everything, regardless of the checklist's
    # current state.
    other_landmarks_links_frame = tk.Frame(other_landmarks_header_row)

    check_all_link = tk.Label(
        other_landmarks_links_frame, text="Check All",
        fg="#1a73e8", cursor="hand2", font=("Segoe UI", 8, "underline"))
    check_all_link.pack(side="left")
    check_all_link.bind("<Button-1>", lambda e: _check_all_other_landmarks())

    other_landmarks_links_separator = tk.Label(
        other_landmarks_links_frame, text=" | ", fg="gray", font=("Segoe UI", 8))
    other_landmarks_links_separator.pack(side="left")

    uncheck_all_link = tk.Label(
        other_landmarks_links_frame, text="Uncheck All",
        fg="#1a73e8", cursor="hand2", font=("Segoe UI", 8, "underline"))
    uncheck_all_link.pack(side="left")
    uncheck_all_link.bind("<Button-1>", lambda e: _uncheck_all_other_landmarks())

    def _check_all_other_landmarks():
        """
        Sets every DISCOVERED "Other Landmark Type" BooleanVar to True
        -- iterates other_landmark_type_vars directly (the full
        dictionary populated by _rebuild_other_landmarks_checklist()
        for every eligible type this read found), never the Tkinter
        Checkbutton widgets currently rendered inside the Canvas
        viewport -- so every type is checked regardless of whether its
        row is currently scrolled into view. Each Checkbutton is bound
        to its own BooleanVar, so this automatically and correctly
        updates every widget's displayed check-state too, including
        off-screen ones, the moment they're scrolled into view.
        """
        for var in other_landmark_type_vars.values():
            var.set(True)
        _update_run_button_state()

    def _uncheck_all_other_landmarks():
        """Mirror of _check_all_other_landmarks() -- sets every
        discovered type's BooleanVar to False, same off-screen-safe
        approach (iterates the variable dict directly, never the
        currently-rendered widgets)."""
        for var in other_landmark_type_vars.values():
            var.set(False)
        _update_run_button_state()

    # Holds one Checkbutton per distinct non-ALLOWED_FCLASS fclass
    # value found in the currently selected POI source. Only packed
    # while the checkbox above is checked AND the checklist is
    # non-empty. Content-adaptive height with a vertical scrollbar
    # ONLY once more than 8 distinct types are found (per approved
    # spec -- an item-count threshold, computed from measured content
    # rather than road_width.py's own fixed-pixel cap, though the
    # rest of this Canvas/Scrollbar/mousewheel/bbox architecture is
    # otherwise identical to that reference), and a horizontal
    # scrollbar only when a label is wider than the box -- never
    # truncated or wrapped, only ever scrolled into view.
    OTHER_LANDMARKS_MAX_ITEMS_BEFORE_VSCROLL = 8
    # Left indent used when packing other_landmarks_checklist_outer
    # below (visually nests the checklist under the checkbox, matching
    # road_width.py's own sub-checklist indentation convention). Named
    # here so _resize_other_landmarks_checklist_box() can subtract this
    # exact same value from its available-width budget -- omitting it
    # there previously let the checklist's canvas+vertical-scrollbar
    # combined width extend this many pixels PAST poi_action_row's own
    # right edge (the Browse button), since the indent shifts the
    # whole outer frame right without shrinking its own width budget
    # to match.
    OTHER_LANDMARKS_CHECKLIST_LEFT_INDENT = 20

    other_landmarks_checklist_outer = tk.Frame(poi_frame)
    other_landmarks_checklist_canvas = tk.Canvas(
        other_landmarks_checklist_outer, highlightthickness=0, bd=0)
    other_landmarks_vscroll = tk.Scrollbar(
        other_landmarks_checklist_outer, orient="vertical",
        command=other_landmarks_checklist_canvas.yview)
    other_landmarks_hscroll = tk.Scrollbar(
        other_landmarks_checklist_outer, orient="horizontal",
        command=other_landmarks_checklist_canvas.xview)
    other_landmarks_checklist_canvas.configure(
        yscrollcommand=other_landmarks_vscroll.set,
        xscrollcommand=other_landmarks_hscroll.set)
    other_landmarks_checklist_canvas.pack(side="left", fill="both", expand=True)
    # Both scrollbars packed/unpacked dynamically by
    # _resize_other_landmarks_checklist_box() below -- only shown when
    # content actually exceeds the box in that direction.

    other_landmarks_checklist_container = tk.Frame(other_landmarks_checklist_canvas)
    _other_landmarks_canvas_window = other_landmarks_checklist_canvas.create_window(
        (0, 0), window=other_landmarks_checklist_container, anchor="nw")

    def _on_other_landmarks_content_configure(_event=None):
        other_landmarks_checklist_canvas.configure(
            scrollregion=other_landmarks_checklist_canvas.bbox("all"))
    other_landmarks_checklist_container.bind(
        "<Configure>", _on_other_landmarks_content_configure)

    def _on_other_landmarks_mousewheel(event):
        other_landmarks_checklist_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    other_landmarks_checklist_canvas.bind(
        "<Enter>", lambda e: other_landmarks_checklist_canvas.bind_all(
            "<MouseWheel>", _on_other_landmarks_mousewheel))
    other_landmarks_checklist_canvas.bind(
        "<Leave>", lambda e: other_landmarks_checklist_canvas.unbind_all("<MouseWheel>"))

    def _resize_other_landmarks_checklist_box():
        """
        Recomputes other_landmarks_checklist_canvas's own height/width
        handling to fit its current content. Vertical scrollbar trigger
        is an ITEM COUNT (> 8 distinct types, per approved spec), not
        road_width.py's own fixed-pixel cap -- the per-row pixel height
        is measured from the container's actual current content
        (content_height / n_items) so the 8-item cap is translated into
        an accurate pixel height regardless of font/theme, rather than
        guessing a constant.

        Horizontal overflow handling: the canvas's own displayed WIDTH
        is explicitly pinned here to a FIXED value on every call --
        never left to grow to match wide content. That fixed value is
        derived from poi_action_row's own already-established
        requested width, MINUS both the vertical scrollbar's width
        (whenever shown) AND OTHER_LANDMARKS_CHECKLIST_LEFT_INDENT
        (the same left indent already applied when
        other_landmarks_checklist_outer itself is packed below) --
        omitting that indent previously let canvas+scrollbar's combined
        width extend past poi_action_row's own right edge (the Browse
        button) by exactly the indent amount, since the indent shifts
        the whole outer frame right without shrinking its own width
        budget to compensate. Without this pin altogether, a long
        fclass label would need the embedded item widened past the
        canvas's then-current width (via itemconfig() below) to enable
        horizontal scrolling, but an un-pinned canvas's own natural/
        requested size grows to match that wider embedded item too --
        and since nothing else constrains this window's overall size,
        that growth cascades all the way up and visibly widens the
        whole configuration window. Pinning the canvas's width here
        means overflow is handled ENTIRELY by the horizontal
        scrollbar appearing, exactly as expected -- the window itself
        only ever grows taller (to fit new checklist rows), never
        wider.
        """
        other_landmarks_checklist_container.update_idletasks()
        n_items = len(other_landmark_type_vars)
        content_height = other_landmarks_checklist_container.winfo_reqheight()
        content_width = other_landmarks_checklist_container.winfo_reqwidth()

        show_vscroll = n_items > OTHER_LANDMARKS_MAX_ITEMS_BEFORE_VSCROLL and n_items > 0
        vscroll_width = other_landmarks_vscroll.winfo_reqwidth() if show_vscroll else 0
        fixed_row_width = poi_action_row.winfo_reqwidth()
        canvas_width = max(
            fixed_row_width - vscroll_width - OTHER_LANDMARKS_CHECKLIST_LEFT_INDENT, 1)
        other_landmarks_checklist_canvas.configure(width=canvas_width)

        if n_items <= OTHER_LANDMARKS_MAX_ITEMS_BEFORE_VSCROLL or n_items == 0:
            other_landmarks_checklist_canvas.configure(height=content_height)
            other_landmarks_vscroll.pack_forget()
        else:
            row_height = content_height / n_items
            capped_height = int(round(row_height * OTHER_LANDMARKS_MAX_ITEMS_BEFORE_VSCROLL))
            other_landmarks_checklist_canvas.configure(height=capped_height)
            other_landmarks_vscroll.pack(side="right", fill="y")

        if content_width > canvas_width:
            other_landmarks_checklist_canvas.itemconfig(_other_landmarks_canvas_window, width=content_width)
            other_landmarks_hscroll.pack(side="bottom", fill="x")
        else:
            other_landmarks_checklist_canvas.itemconfig(_other_landmarks_canvas_window, width=canvas_width)
            other_landmarks_hscroll.pack_forget()
    # Both scrollbars start unpacked; _update_other_landmarks_
    # visibility() (via the discovery-read completion callback)
    # decides what to show.

    def _rebuild_other_landmarks_checklist(fclass_values):
        """
        Plain destroy-and-repopulate of the "Other Landmark Types"
        checklist from a fresh list of distinct fclass values (see
        _refresh_poi_landmark_types()/_poll_poi_landmark_queue()) --
        never merged with any previous checklist state, matching the
        established no-cache convention. Values already covered by
        ALLOWED_FCLASS are excluded here (they are always processed
        via the existing fixed-type path regardless of this checklist).
        Column-suffix assignment (with collision handling) is computed
        once here, via _assign_other_type_column_suffixes(), and
        reused verbatim by on_run() -- see that function's own
        docstring for why this must never be recomputed from a
        filtered-down subset.
        """
        nonlocal other_landmark_type_vars, other_landmark_column_suffixes
        for child in other_landmarks_checklist_container.winfo_children():
            child.destroy()
        other_landmark_type_vars = {}

        eligible = sorted(
            v for v in fclass_values
            if v not in ALLOWED_FCLASS and not _is_education_fclass(v)
        )
        other_landmark_column_suffixes = _assign_other_type_column_suffixes(eligible)

        for display_text in eligible:
            var = tk.BooleanVar(master=win, value=False)
            other_landmark_type_vars[display_text] = var
            suffix = other_landmark_column_suffixes.get(display_text, "")
            base_suffix = _sanitize_fclass_to_suffix(display_text)
            label_text = display_text
            if suffix != base_suffix:
                # Collision was disambiguated -- make it explicit in
                # the checklist rather than resolving it invisibly.
                label_text = f"{display_text}  (\u2192 CAMA_{suffix}#)"
            tk.Checkbutton(other_landmarks_checklist_container, text=label_text,
                           variable=var).pack(anchor="w")

        # NOTE: _resize_other_landmarks_checklist_box() is deliberately
        # NOT called here -- see _update_other_landmarks_visibility()
        # below for why it must run AFTER the checklist is actually
        # packed into the window, not before.

    def _reflow_window():
        """
        PART 2: adapted from road_width.py's own _reflow_window()
        (same underlying principle -- measuring win.winfo_reqwidth()/
        reqheight() after update_idletasks() and explicitly setting
        minsize/maxsize/geometry to that exact value forces ONE clean,
        complete window repaint instead of an incremental resize,
        which is what was leaving a stale/unpainted region (reported
        as a black area, typically near Output Destination -- wherever
        the newly-exposed space happened to land) behind whenever the
        "Other Landmark Types" checklist was packed or unpacked).

        Adaptation notes -- this window's geometry differs from
        road_width.py's, so this is not a blind copy:
        - road_width.py calls its own _reflow_window() from SEVERAL
          independent checklist-visibility functions (Land Parcel
          classification, Filter by Road Type), since that tool has
          multiple dynamic-content sections. meters_from_school_shop_transport_church.py's
          `win` has exactly ONE dynamic-content section -- this one --
          so this function is called from exactly one place,
          _update_other_landmarks_visibility() below, never from
          anywhere else in this window.
        - Unlike road_width.py's own `win`, THIS window's `win` (see
          open_main_window()'s construction above) has never called
          .geometry() at all -- it has only ever relied on pack()'s
          own initial auto-sizing. This function is the first place in
          this file that explicitly locks win's size. That lock only
          ever takes effect from the first time the checklist actually
          changes visibility onward; the window's very first on-screen
          size (before any POI source is even selected) is completely
          unaffected, still purely pack()-computed exactly as before.
        - Called only in response to an actual checklist/link
          visibility change (never on a timer, keystroke, or any other
          repeating event), so there is no continuous geometry/repaint
          loop -- and since this function only ever reads/sets `win`'s
          OWN overall size, it never touches, resizes, or repacks any
          widget belonging to the Land Parcel, Road Network, or Output
          Destination sections.
        """
        win.update_idletasks()
        req_w = win.winfo_reqwidth()
        req_h = win.winfo_reqheight()
        win.minsize(req_w, req_h)
        win.maxsize(req_w, req_h)
        win.geometry(f"{req_w}x{req_h}")

    def _update_other_landmarks_visibility():
        """
        Single source of truth for the "Include Other Landmark Types"
        header row's visibility (checkbox + Check All/Uncheck All
        links), the links group's own narrower visibility, and the
        checklist's visibility -- re-run after every checklist rebuild
        (see _poll_poi_landmark_queue()) and every checkbox toggle
        (see _on_include_other_toggled()).

        Header row (checkbox): shown ONLY once a completed discovery
        read has actually found at least one eligible non-
        ALLOWED_FCLASS type (other_landmark_type_vars non-empty).
        Hidden before any POI source is selected, while a read is in
        progress, or when a completed read finds nothing -- matching
        the requirement that this checkbox must never appear as if it
        always offers something to check. When hidden,
        include_other_landmarks_var is also forced back to False, so
        a later POI source that DOES have eligible types always starts
        the checkbox in its default unchecked state rather than
        silently carrying over a stale checked state from a previous,
        now-irrelevant source.

        Header row WIDTH: pinned every time the row is shown, via
        pack_propagate(False), to poi_action_row's own established
        width (the same reference _resize_other_landmarks_checklist_
        box() already uses for the canvas). This row now holds THREE
        pieces of text on one line (checkbox label + the Check All /
        Uncheck All group) -- without this pin, that combined text's
        natural/requested width can exceed the window's established
        width and grow the whole configuration window, exactly the
        bug reported. Pinning here means the row (and therefore the
        window) stays at a constant width regardless of link content;
        other_landmarks_links_frame is packed side="right" within
        this fixed-width row so it sits flush against the far right
        edge, with the checkbox flush against the far left.

        Check All / Uncheck All links group: shown only when the
        checklist itself would be shown (checked AND has content) --
        the exact same condition as the checklist below, since acting
        on an invisible checklist would be meaningless.

        Checklist: shown only when the checkbox is both visible
        (content exists) AND checked. The width/height measurement
        _resize_other_landmarks_checklist_box() depends on must run
        AFTER other_landmarks_checklist_outer.pack() below, with an
        explicit update_idletasks() in between -- measuring before
        packing reads the canvas's stale/near-zero un-rendered width,
        which previously made the horizontal-scrollbar decision
        ("content wider than box") almost always true, spuriously
        showing that scrollbar and widening the whole configuration
        window to fit it.

        _reflow_window() runs once at the end, unconditionally --
        matching road_width.py's own "one resize per cycle" principle
        -- so this window's overall size is always recalculated to
        exactly fit whatever ended up packed/unpacked above, with a
        single clean repaint and no leftover unpainted region.
        """
        has_content = bool(other_landmark_type_vars)

        if has_content:
            win.update_idletasks()
            fixed_row_width = poi_action_row.winfo_reqwidth()
            row_height = include_other_checkbox.winfo_reqheight()
            other_landmarks_header_row.configure(width=fixed_row_width, height=row_height)
            other_landmarks_header_row.pack_propagate(False)
            other_landmarks_header_row.pack(fill="x", pady=(2, 0))
        else:
            other_landmarks_header_row.pack_forget()
            include_other_landmarks_var.set(False)

        checklist_should_show = include_other_landmarks_var.get() and has_content

        if checklist_should_show:
            other_landmarks_links_frame.pack(side="right")
        else:
            other_landmarks_links_frame.pack_forget()

        if checklist_should_show:
            other_landmarks_checklist_outer.pack(
                fill="x", padx=(OTHER_LANDMARKS_CHECKLIST_LEFT_INDENT, 0), pady=(0, 4))
            win.update_idletasks()
            _resize_other_landmarks_checklist_box()
        else:
            other_landmarks_checklist_outer.pack_forget()

        _reflow_window()

    def _on_include_other_toggled():
        # Checking/unchecking this box only ever shows/hides the
        # checklist -- it never gates Run (see _update_run_button_
        # state(), which is keyed only on poi_types_reading, never on
        # this checkbox or any sub-checkbox).
        _update_other_landmarks_visibility()
        _update_run_button_state()

    def _set_poi_reading_state(reading):
        """
        PART 2: mirrors road_width.py's _set_parcel_reading_state()
        exactly -- disables the POI Browse button and both POI radio
        buttons while the background landmark-type discovery read is
        in progress, and reuses the EXISTING "No file selected" /
        filename / "No table selected" label (poi_lbl, bound to
        poi_file_var / poi_db_var) via a temporary text swap rather
        than a separate widget, restoring it from the authority
        variables (poi_local_path / poi_db_table) once done -- never
        from whatever the StringVar happened to show during reading.
        """
        nonlocal poi_types_reading
        poi_types_reading = reading
        state = "disabled" if reading else "normal"
        poi_btn.config(state=state)
        poi_radio_local.config(state=state)
        poi_radio_db.config(state=state)

        if reading:
            poi_file_var.set("⏳ Reading POI...")
            poi_db_var.set("⏳ Reading POI...")
            poi_lbl.config(fg="#b36b00")
        else:
            poi_file_var.set(
                os.path.basename(poi_local_path.get()) if poi_local_path.get()
                else "No file selected"
            )
            poi_db_var.set(
                poi_db_table.get() if poi_db_table.get()
                else "No table selected"
            )
            poi_lbl.config(fg="gray")
        _update_run_button_state()

    def _refresh_poi_landmark_types():
        """
        PART 2: background-reads the currently selected POI source
        fresh (no caching -- every call performs a real read,
        regardless of whether this exact source was read before) to
        discover every distinct fclass value present, for the
        "Include Other Landmark Types" checklist. Fires on every POI
        selection change (Browse, DB table pick, or Local/Database
        toggle -- see browse_poi_file()/_on_poi_db_selected()/
        _toggle_poi() below).

        Purely informational: this read's failure never invalidates
        the already-selected POI source (poi_local_path/poi_db_table
        are untouched here) and never blocks Run once the read
        finishes -- see _poll_poi_landmark_queue(). No timeout/
        deadline logic (unlike road_width.py's parcel/road
        classification reads) -- explicitly not needed for this
        initial version.
        """
        if poi_types_reading:
            return

        if poi_source_type.get() == "local":
            source_type = "local"
            path_or_table = poi_local_path.get()
        else:
            source_type = "db"
            path_or_table = poi_db_table.get()

        if not path_or_table:
            _rebuild_other_landmarks_checklist([])
            _update_other_landmarks_visibility()
            _update_run_button_state()
            return

        result_queue = queue.Queue()

        def worker():
            values, error = _read_poi_fclass_values_worker(source_type, path_or_table)
            result_queue.put((values, error))

        _set_poi_reading_state(True)
        threading.Thread(target=worker, daemon=True).start()
        win.after(100, lambda: _poll_poi_landmark_queue(result_queue))

    def _poll_poi_landmark_queue(result_queue):
        """
        Runs on the main thread via win.after() polling. Picks up the
        (fclass_values, error) result placed on the queue by the
        background worker. No deadline/timeout branch -- see
        _refresh_poi_landmark_types()'s own docstring for why.

        A failure (error is not None) is purely informational: the
        already-selected POI source (poi_local_path/poi_db_table) is
        left completely untouched, the reading-state gate is released
        exactly the same as on success (so Run becomes available again
        immediately), a message is logged to the console, and the
        checklist is simply left empty/unavailable -- never treated as
        a failure of the mandatory POI source itself.
        """
        if not win.winfo_exists():
            return
        try:
            values, error = result_queue.get_nowait()
        except queue.Empty:
            win.after(100, lambda: _poll_poi_landmark_queue(result_queue))
            return

        _set_poi_reading_state(False)

        if error is not None or values is None:
            print(f"⚠️ Could not read POI source for Other Landmark Types "
                  f"discovery -- checklist left empty. Details: {error}")
            _rebuild_other_landmarks_checklist([])
        else:
            _rebuild_other_landmarks_checklist(values)

        _update_other_landmarks_visibility()
        _update_run_button_state()

    def browse_poi_file():
        f = filedialog.askopenfilename(filetypes=[
            ("Shapefiles", "*.shp"), ("GeoPackage", "*.gpkg"), ("All", "*.*")])
        if f:
            poi_local_path.set(f)
            poi_file_var.set(os.path.basename(f))
            _update_run_button_state()
            _refresh_poi_landmark_types()

    def _on_poi_db_selected(sel):
        # _pick_db_tables() only invokes on_select after a confirmed
        # selection, so sel is never empty here -- the original
        # lambda's "if sel else None" branch was a redundant
        # conditional. Switching to a named callback is a readability
        # change only; no behavior change.
        poi_db_table.set(sel[0])
        poi_db_var.set(sel[0])
        _update_run_button_state()
        _refresh_poi_landmark_types()

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
        _refresh_poi_landmark_types()

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
        """
        Run button handler: validates all four selections are present,
        runs the existing-output-column conflict check (Priority 1),
        the local output-file conflict check (Priority 2), and DB-output
        table resolution (Priority 3) -- in that order, each able to
        cancel the whole run -- then destroys this window and hands off
        to run_with_progress(). Sets the module-level parcel_source,
        poi_source, road_source, output_mode globals on success, plus
        (PART 2) selected_other_poi_column_map.
        """
        global parcel_source, poi_source, road_source, output_mode
        global selected_other_poi_column_map

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
        # PART 2: capture which "Other Landmark Types" are checked, using
        # the EXACT column-suffix assignment already shown in the
        # checklist (other_landmark_column_suffixes, built by
        # _rebuild_other_landmarks_checklist() from the full set of
        # discovered non-ALLOWED_FCLASS types) -- never recomputed from
        # just the checked subset, so the checklist and the columns
        # task() actually writes always agree (see
        # _assign_other_type_column_suffixes()'s own docstring for why
        # recomputing from a filtered subset could silently change a
        # collision-disambiguated suffix). Empty dict (not None) when
        # the checkbox is unchecked or no sub-checkbox is checked --
        # today's exact default behavior, fully unchanged.
        # ------------------------------------------------------------------
        if include_other_landmarks_var.get():
            selected_other_poi_column_map = {
                t: other_landmark_column_suffixes[t]
                for t, var in other_landmark_type_vars.items()
                if var.get() and t in other_landmark_column_suffixes
            }
        else:
            selected_other_poi_column_map = {}

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
        Output destination are all present, AND no PART 2 background
        POI landmark-type discovery read (poi_types_reading) is
        currently in progress.

        The cascade below intentionally mirrors on_run()'s own
        validation order further down -- conscious duplication for a
        minimal-risk, additive gating layer, not a refactor of on_run()
        itself. Keep the two in sync if this tool's required inputs
        ever change. (The additional single-parcel-table check inside
        run_with_progress()'s task() for DB output is deeper than what
        on_run() validates and is intentionally NOT mirrored here.)

        PART 2 note: poi_types_reading is the ONLY landmark-related
        condition that gates Run here. Whether "Include Other Landmark
        Types" is checked, or which/how many of its sub-checkboxes are
        checked, NEVER affects Run availability -- see
        _on_include_other_toggled(), which never calls this function
        in a way that could disable Run based on checkbox content.

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
        elif poi_types_reading:
            run_status_var.set("Reading POI landmark types...")
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


# ========================================
# DB OVERWRITE DIALOGS
# ========================================
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
    dialog.title("Meters From (School, Shop, Transport, Church) Tool")
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
    dialog.title("Meters From (School, Shop, Transport, Church) Tool")
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


# ========================================
# RUN / ORCHESTRATION
# ========================================
def run_with_progress(app_root, overwrite_mode=None, resolved_table_name=None):
    """
    Opens the processing progress window and runs the full parcel-
    distance computation via a Tk `.after()`-scheduled task() (see
    Notes), covering: loading parcel/POI/road data, PRS92 zone
    detection + reprojection, output-column conflict normalization,
    the actual distance computation (run_cpu_parallel_with_progress()),
    and saving the result (local .gpkg + Global Mapper, or PostGIS).

    Args:
        app_root: parent Tk root; also used to guard against a second
        concurrent run via app_root._poi_progress_open.
        overwrite_mode (str | None): "overwrite" or "new", from
        ask_overwrite_dialog() in on_run() -- only relevant for local
        output mode.
        resolved_table_name (str | None): the already-confirmed DB
        output table name from resolve_db_output_table() in on_run() --
        only relevant for DB output mode. Treated as already-validated;
        no re-resolution happens here.

    Notes:
        No real background thread is used -- task() is scheduled via
        app_root.after(100, task), and there is no working cancel
        button (see _remove_close_button()'s docstring and the comment
        at the progress_win.protocol() call below) because this tool's
        actual workload is sequential, not something a cancel button
        could safely interrupt mid-computation.
    """
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
        """
        The actual processing work, scheduled via app_root.after() so
        the progress window has a chance to render before this blocks
        the UI thread. Loads all three input layers, detects and
        reprojects to the working PRS92 zone, runs
        run_cpu_parallel_with_progress(), then saves locally or to
        PostGIS depending on output_mode.
        """
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

            # Ordinary fixed-type POIs -- church/shop/transport only.
            # "school" and "university" (and every other education-
            # related fclass) are NOT processed here anymore; they flow
            # through the separate unified education pool below
            # instead (see ORDINARY_FIXED_FCLASS's own module-level
            # docstring for why ALLOWED_FCLASS itself is deliberately
            # left unchanged rather than narrowed in place).
            ordinary_fixed_types = sorted(
                t for t in poi_gdf["_fclass_norm"].unique()
                if t in ORDINARY_FIXED_FCLASS
            )

            # PART 2: dynamic "Other Landmark Types" the user checked at
            # Run time, mapped raw_normalized_fclass -> already-final
            # column suffix (see on_run() / _assign_other_type_column_
            # suffixes()). Empty when the checkbox was unchecked or no
            # sub-checkbox was checked -- today's exact default
            # behavior, fully unchanged below.
            other_type_map = selected_other_poi_column_map or {}

            # Whether this POI source has a usable "name" attribute at
            # all -- "name" is treated as OPTIONAL (unlike "fclass",
            # which the whole pipeline already requires): if this
            # column is absent, every CAMA_{TYPE}{N}_NAME value below
            # simply becomes Python None for every candidate, the run
            # is never invalidated, and nothing crashes.
            has_poi_name_column = "name" in poi_gdf.columns

            def _coords_and_names_for(fclass_value):
                """
                Builds one type's coordinate array AND its parallel
                names list from the SAME filtered subset of poi_gdf,
                in the SAME row order -- required so a given
                candidate's name always travels with its own
                coordinate through the ranking pipeline in
                worker_process() (never independently re-filtered,
                which could otherwise let the two drift out of
                alignment). A missing/NULL "name" value becomes Python
                None here -- never the literal string "None", never an
                empty string -- preserved as None all the way through
                to the output GeoPackage, where GDAL/OGR (and
                therefore QGIS) renders it as NULL, matching the
                source data's own NULL semantics exactly.
                """
                subset = poi_gdf[poi_gdf["_fclass_norm"] == fclass_value]
                coords = np.array([[p.x, p.y] for p in subset.geometry])
                if has_poi_name_column:
                    names = [v if pd.notna(v) else None for v in subset["name"].tolist()]
                else:
                    names = [None] * len(subset)
                return coords, names

            poi_coords = {}
            poi_names = {}
            for t in ordinary_fixed_types:
                poi_coords[t], poi_names[t] = _coords_and_names_for(t)
            for raw_t, suffix in other_type_map.items():
                poi_coords[suffix], poi_names[suffix] = _coords_and_names_for(raw_t)

            # ------------------------------------------------------------------
            # Unified education pool ("CAMA_SCHOOL{1-3}"). Every POI whose
            # normalized fclass passes _is_education_fclass() (bare "school",
            # every literal "*_school" variant, or exactly university/
            # college/kindergarten/preschool) is gathered into ONE pool,
            # ranked purely by distance regardless of sub-type -- NOT split
            # into separate per-subtype buckets/columns. Each POI's own
            # classification (_classify_education_poi(), keyword-matched
            # against its own fclass+name -- see that function's docstring
            # for why a POI whose fclass alone already says "university" or
            # "middle school" resolves automatically without needing a name
            # at all) travels alongside its coordinate and name as a third
            # parallel array, written to CAMA_SCHOOL{rank}_TYPE by
            # worker_process(). "SCHOOL" is used as the literal poi_types/
            # poi_coords/poi_names/poi_types_tag key -- typ.upper() on it
            # downstream is a no-op, exactly like every other already-
            # uppercase dynamic suffix.
            # ------------------------------------------------------------------
            poi_types_tag = {}
            education_mask = poi_gdf["_fclass_norm"].apply(_is_education_fclass)
            education_subset = poi_gdf[education_mask]
            if len(education_subset) > 0:
                school_coords = np.array([[p.x, p.y] for p in education_subset.geometry])
                if has_poi_name_column:
                    school_names_raw = education_subset["name"].tolist()
                else:
                    school_names_raw = [None] * len(education_subset)
                school_names = [v if pd.notna(v) else None for v in school_names_raw]
                school_types = [
                    _classify_education_poi(fc, nm)
                    for fc, nm in zip(education_subset["_fclass_norm"].tolist(), school_names_raw)
                ]
                poi_coords["SCHOOL"] = school_coords
                poi_names["SCHOOL"] = school_names
                poi_types_tag["SCHOOL"] = school_types

            # poi_types doubles as both the per-type iteration key
            # (worker_process()) and the column-name source
            # (typ.upper()) -- ordinary fixed types keep using their raw
            # lowercase normalized string (unchanged, e.g. "church" ->
            # CAMA_CHURCH*), "SCHOOL" (the unified education pool) and
            # dynamic types use their pre-assigned/already-final suffix
            # directly (already uppercase/underscore-only, so
            # typ.upper() on it downstream is a no-op) instead of a raw
            # fclass string, so worker_process() and every other
            # already-generic downstream function need no further
            # changes at all.
            poi_types = (
                ordinary_fixed_types
                + (["SCHOOL"] if "SCHOOL" in poi_coords else [])
                + sorted(other_type_map.values())
            )

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

            # Main CAMA output column pre-init -- explicit, deterministic
            # order (NOT relying on pandas' .at[] lazy-column-creation
            # inside worker_process()'s caller below, which would
            # otherwise append each column in first-write order instead
            # -- previously the exact reason METHOD columns ended up
            # grouped at the very end of the output, in a non-
            # deterministic order dependent on which parcel happened to
            # populate a given type/rank first).
            #
            # For every realizable type, only the ranks that are
            # CATEGORICALLY POSSIBLE get columns at all: capped via
            # min(3, total POIs of that type in this source) -- the
            # exact same cap already used per-parcel for k in
            # worker_process()'s cKDTree query, so a type with e.g. only
            # 1 total POI can never populate a CAMA_{TYPE}2/3 value for
            # ANY parcel in this run, and therefore never gets those
            # columns pre-created either. This closes the previously-
            # flagged gap where ALL FIVE ALLOWED_FCLASS types' columns
            # were unconditionally created regardless of presence (see
            # the standing comment above _sanitize_fclass_to_suffix()) --
            # now using ordinary_fixed_types (present-only) instead of
            # the full static ALLOWED_FCLASS set.
            #
            # Each rank's distance column is immediately followed by its
            # _NAME column (CAMA_{TYPE}1, CAMA_{TYPE}1_NAME,
            # CAMA_{TYPE}2, CAMA_{TYPE}2_NAME, ...) -- this insertion
            # order IS the final output column order, matching
            # _realizable_targets()'s own interleaved ordering above.
            # METHOD is intentionally not pre-initialized here at all --
            # removed from the MAIN output entirely (the internal
            # `method` variable itself is untouched inside
            # worker_process(), still feeding the separate, dormant
            # poi_routes.gpkg QA export unchanged).
            for t in ordinary_fixed_types:
                max_rank = min(3, len(poi_coords[t]))
                for i in range(1, max_rank + 1):
                    gdf[f"CAMA_{t.upper()}{i}"] = np.nan
                    gdf[f"CAMA_{t.upper()}{i}_NAME"] = None

            for suffix in other_type_map.values():
                max_rank = min(3, len(poi_coords[suffix]))
                for i in range(1, max_rank + 1):
                    gdf[f"CAMA_{suffix}{i}"] = np.nan
                    gdf[f"CAMA_{suffix}{i}_NAME"] = None

            # Unified education pool pre-init -- same rank-capping
            # rule as every other type above, PLUS a third column per
            # rank (_TYPE) unique to the school pool (see
            # poi_types_tag's own docstring notes in worker_process()
            # and run_cpu_parallel_with_progress()).
            if "SCHOOL" in poi_coords:
                max_rank = min(3, len(poi_coords["SCHOOL"]))
                for i in range(1, max_rank + 1):
                    gdf[f"CAMA_SCHOOL{i}"] = np.nan
                    gdf[f"CAMA_SCHOOL{i}_NAME"] = None
                    gdf[f"CAMA_SCHOOL{i}_TYPE"] = None

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
                poi_types, poi_coords, poi_names, poi_types_tag,
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


# ========================================
# MAIN / ENTRYPOINT
# ========================================
def main(parent=None):
    """
    Tool entry point. If parent is given (invoked from within another
    running Tk app), reuses it as root and just opens this tool's
    window. Otherwise creates and hides a new Tk root and enters its
    own mainloop -- the standalone-subprocess dispatch path.

    Args:
        parent: an existing Tk root to reuse, or None to create one.
    """
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