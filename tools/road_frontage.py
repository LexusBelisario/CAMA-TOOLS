"""
tools/road_frontage.py

PURPOSE:
    CAMA Tools tool ("ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO" in
    MAIN.py's dispatch table): for each Land Parcel, measures the
    parcel's road frontage (the length of parcel boundary running
    along an adjacent Road Network segment), depth (a derived measure
    perpendicular to that frontage), and depth-to-width ratio, writing
    CAMA_ROAD_FRONTAGE, CAMA_DEPTH, and CAMA_DEPTH_WIDTH_RATIO (or
    existing differently-cased columns, if detected and confirmed at
    Run time -- see parcel_output_column_overrides below). Supports an
    optional Road Type exclusion filter and an optional per-source Lot
    Location/Lot Label classification pass (see the "Road
    Classification" runtime-state block below), which are mutually
    exclusive with each other at the GUI level.

DISPATCH:
    Run as an isolated subprocess by MAIN.py via its `--tool` dispatch
    mechanism (see system context). Entry point is main(), triggered via
    the `if __name__ == "__main__":` guard at the bottom of this file.

INPUTS:
    Land Parcel source: one or more local files or PostGIS tables.
    Road Network source: a single local file or PostGIS table.
    pg_credentials.json (via load_db_credentials(), from
    utils/db_discovery.py) for any DB source or DB output.

OUTPUTS:
    Local output mode: writes exactly one atomically-written .gpkg per
    processed Land Parcel source, then attempts to open it in Global
    Mapper (via load_in_global_mapper()-equivalent logic in this
    file's GPKG overwrite safety / Global Mapper handling). The
    previously-disabled buffer-diagnostic/Visual Measurement (VM) QA
    layers have been removed entirely (not just disabled) -- a
    successful run now always produces exactly one output artifact per
    source, local or DB.
    DB output mode: writes/replaces one PostGIS table per source, via a
    cancel-safe staging/verify/atomic-rename-swap sequence
    (_write_db_output_safely() -- see the "DB ATOMIC WRITE / CANCEL-SAFE
    STAGING" section below), never touching the real destination table
    until the swap itself. The destination table is resolved via
    resolve_db_output_table() -- an exact-match replace for a DB Land
    Parcel source, or a fuzzy-match-with-confirmation flow
    (confirm_db_overwrite_dialog() / choose_db_overwrite_dialog()) for a
    local-file Land Parcel source. That resolution step also scans for,
    and offers to clean up, any leftover staging/backup table left
    behind by a previous run that was interrupted before it could clean
    up after itself (_scan_orphaned_cama_tables() /
    _prompt_orphaned_cama_tables()).

    CANCELLATION: the Run Processing progress window's title-bar close
    button acts as a cooperative Cancel control (see ProgressWindow and
    the "DB ATOMIC WRITE / CANCEL-SAFE STAGING" section below).
    Cancellation is checked at explicit checkpoints between and within
    the per-parcel processing passes in process_frontage_single() --
    it is NOT instantaneous everywhere: a single blocking geometry
    operation (reprojection, clipping, union, an individual Shapely
    call) or a single blocking DB call (the staging write, the final
    atomic rename-swap) always runs to completion once started. A
    Cancel that lands before a real result exists discards everything
    computed so far for that source (no partial output is ever saved,
    locally or to the DB); nothing after the point a real result exists
    can be interrupted.

DEPENDENCIES:
    stdlib: os, re, math, subprocess, json, ctypes, sys, tkinter (+
    ttk).
    third-party: geopandas, pandas, numpy, shapely (geometry, ops,
    strtree, validation), sqlalchemy, psycopg2.
    local: utils.table_name_matching, utils.resource_path,
    utils.db_discovery, utils.column_detection, utils.window_icon,
    utils.progress_framework (imported mid-file, directly above the
    class/function that uses it -- see the Progress Event Protocol v9
    comment block further below).

SIDE EFFECTS:
    File reads/writes (.shp/.gpkg). PostGIS reads/writes. A live
    PostgreSQL connection. Tkinter GUI windows throughout, including
    TWO independent background-thread + queue.Queue-based detect-on-
    select systems in open_main_window() -- one for Land Parcel
    existing-output-column detection, one for Road Network road-type/
    classification reading (cached in _road_gdf_cache and reused by
    run_processing() if the same source is still selected) -- plus a
    third background thread + queue.Queue for the main processing run.
    A subprocess launch to Global Mapper on local-output saves.

    IMPORTANT -- this module has TWO separate, genuine import-time side
    effects:

    1. The "GeoPandas compatibility shim" (see that section below):
       if the installed geopandas version's GeoSeries class lacks
       from_bbox, this module monkey-patches one onto it at import
       time. This mutates a third-party library class, not just this
       module's own state -- order-dependent only on `import geopandas
       as gpd` already having executed. Preserved exactly as found.

    2. The module-level call to set_app_user_model_id() (see "FORCE
       WINDOWS APP ICON" below) invokes the Win32
       SetCurrentProcessExplicitAppUserModelID API the moment this
       file is imported or run -- not lazily, not inside main(). This
       affects how Windows groups/identifies this process's taskbar
       icon.

    Both are preserved exactly as found -- not moved, deferred, or
    wrapped in a function -- since doing so would change when these
    effects happen, which is out of scope for a documentation/
    reorganization task (see Section C of the governing instructions:
    no behavior changes).

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
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox, ttk, StringVar

import geopandas as gpd
import pandas as pd
import numpy as np
from shapely.geometry import LineString, MultiLineString, Point, box
from shapely.ops import unary_union, nearest_points, linemerge, transform
from shapely.strtree import STRtree
from shapely.validation import make_valid
from sqlalchemy import create_engine, inspect, text
import psycopg2

from utils.table_name_matching import normalize_name, find_matching_tables
from utils.resource_path import resource_path
from utils.db_discovery import load_db_credentials, fetch_tables
from utils.column_detection import detect_existing_output_columns
from utils.window_icon import apply_icon
from utils.gpkg_io import write_gpkg_atomic as _write_gpkg

# =========================
# GeoPandas compatibility shim
# =========================
# NOTE: import-time side effect -- monkey-patches geopandas.GeoSeries
# the moment this module is loaded, if the installed version lacks
# from_bbox (see module docstring SIDE EFFECTS). Not moved or
# deferred; see module docstring for why.
if not hasattr(gpd.GeoSeries, "from_bbox"):
    from shapely.geometry import box

    @staticmethod
    def _from_bbox(b):
        return gpd.GeoSeries([box(*b)])

    gpd.GeoSeries.from_bbox = _from_bbox

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
_app_root = None

# ── Road Classification (new) ────────────────────────────────────
# parcel_classification_selection: {path_or_table: bool} -- one entry per
# selected Land Parcel source, True where the user checked "use this
# source's LOT_LOCATION/LOT_LABEL classification" for that SPECIFIC file
# or table. A per-source dict rather than one aggregate flag, since a
# batch of parcel sources may mix files that should and shouldn't have
# classification applied -- the user decides per file, not per batch.
# filter_by_road_type_active is still a single flag (Road Network only
# ever has one selected source, unlike Land Parcel). Mutually exclusive
# at the GUI level (see open_main_window()'s trace_add() wiring): if
# Filter by Road Type is checked, no per-source classification checkbox
# can also be checked, and vice versa -- but multiple per-source
# classification checkboxes CAN be checked together, since those don't
# conflict with each other, only with Filter by Road Type. Set by
# open_main_window()'s on_run(), read by run_processing() and
# resolve_classification() (below).
parcel_classification_selection = {}
filter_by_road_type_active = False

# road_type_excluded_values: list[str] of ROAD_TYPE values (exact,
# case-sensitive) the user unchecked in the "Filter by Road Type"
# checklist. Only ever consulted when filter_by_road_type_active is True
# -- resolve_classification() ignores this for any source whose
# per-source classification checkbox is checked, so a stale non-empty
# value left over from a previous session can never leak into that
# source's run.
road_type_excluded_values = []

# overwrite_mode: "overwrite" | "new" | None (no conflicts found, or
# output destination isn't local). Resolved ONCE, up front, in on_run(),
# via ask_overwrite_dialog() -- see that function's docstring for why
# this is a single combined decision for the whole batch rather than a
# per-file prompt. Read by run_processing() when writing each source's
# output files. Ported from road_width.py's validated pattern.
overwrite_mode = None

# parcel_output_column_overrides: {path_or_table: {"CAMA_ROAD_FRONTAGE": name,
# "CAMA_DEPTH": name, "CAMA_DEPTH_WIDTH_RATIO": name}} -- for any Land Parcel
# source where the merged background read (see the Land Parcel
# classification read below, extended to also check for this) found a
# PRE-EXISTING column matching one of this tool's three output column
# names (case-insensitive), and the user confirmed proceeding via the
# combined dialog in on_run(). Threaded into process_frontage_single()
# as road_frontage_col/depth_col/dwr_col, so the tool writes back into
# the EXACT existing column (preserving its original casing) instead of
# always writing the hardcoded standard name -- avoids silently creating
# a confusing duplicate column when the existing one used different
# casing (e.g. "dePTH" alongside a new "DEPTH"). A source with no entry
# here (or a source whose overrides dict doesn't mention a given output)
# uses that output's default standard name. Ported from road_width.py's
# parcel_road_width_column_overrides pattern, extended from one column to
# three, per project-lead decision: this tool's three outputs
# (ROAD_FRONTAGE, DEPTH, DEPTH_WIDTH_RATIO) are one feature set computed
# together, so a conflict on ANY of them triggers ONE combined dialog
# covering all three, not three separate prompts.
parcel_output_column_overrides = {}

# _road_gdf_cache: last-successful-read snapshot, one independent slot
# for "local" and one for "db". Scope narrowed (2026-08): this NO
# LONGER skips a fresh read on toggle/selection -- _refresh_road_classification() always
# performs a real background read every time it's called, so the UI-
# facing checklist is never served a stale result. What THIS cache
# still exists for: run_processing() re-reading the same source at Run
# time would otherwise be a redundant second full file/DB read of data
# already read moments earlier during selection/toggle -- this slot
# lets it reuse that already-fresh GeoDataFrame instead, saving that
# second read, while remaining explicitly scoped to "reuse ONLY if it's
# the freshest available read for the currently active source" (see
# _refresh_road_classification()'s docstring for how freshness is kept
# guaranteed). Each slot holds:
#   "key" : the exact path_or_table string this slot's data is for
#           (None if nothing has been read for that mode yet)
#   "gdf" : the actual read GeoDataFrame (safe to hold in full -- Road
#           Network only ever has ONE selected source at a time, unlike
#           Land Parcel which can have many)
# A slot is only ever replaced by a fresh read of that SAME mode; the
# other mode's slot is untouched, which is what makes the two
# "isolated" from each other. (Previously also held "value_vars" and
# "filter_active" for a cache-hit UI-restore path -- removed along with
# that path, since both fields had no other consumer.)
_road_gdf_cache = {
    "local": {"key": None, "gdf": None},
    "db": {"key": None, "gdf": None},
}

# Land Parcel Source: deliberately does NOT cache detection results
# across selections/toggles --
# a cache keyed only on "which file/table was selected" cannot detect
# that the file/table's CONTENTS changed externally (e.g. another CAMA
# tool, QGIS, or Global Mapper modifying it) between one selection and
# the next. Every selection AND every Local/Database toggle triggers a
# fresh read instead -- see _refresh_parcel_classification() below.


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
    Auto-detect the correct PRS92 zone EPSG code from the COMBINED
    bounding-box midpoint longitude of one or more input GeoDataFrames.

    labeled_gdfs: list of (label, gdf) tuples, e.g.
        [("Land Parcel", brgy_gdf), ("Road Network", road_gdf)]
    The label is used only for diagnostics. It has no effect on CRS
    detection.

    If a layer has no CRS defined, WGS84 (EPSG:4326) is assumed and a
    warning string naming that layer is included in the returned
    warning so the caller can surface it to the operator — processing
    continues rather than aborting, but the resulting measurements may
    be wrong if the actual source CRS was something other than WGS84.
    Multiple such warnings (one per affected layer) are joined into a
    single multi-line string rather than the last one silently
    overwriting the others.

    Uses total_bounds (min/max coordinates) rather than a unioned-geometry
    centroid. A union across an entire large parcel layer is a known
    source of GEOS TopologyExceptions on real-world cadastral data —
    confirmed by reproducing the exact failure this tool hit in
    production. total_bounds is pure min/max arithmetic and carries no
    such risk.

    Auxiliary layers (e.g. Road Network) without usable geometry are
    ignored for CRS zone determination -- zone detection proceeds as
    long as at least one valid layer remains. Downstream processing
    (the "if road_gdf.empty:" check further down) already has its own,
    more specific error for a missing/unusable road layer, so failing
    zone detection over it here would only produce a less helpful
    message for the same situation.

    A layer with no usable geometry at all (all-null, or
    all-empty-but-non-null shapes) raises a ValueError naming that
    specific layer, rather than silently corrupting the computed
    longitude into NaN.

    Returns (epsg, warning) where warning is None when no CRS issue was
    found, or a string describing the issue(s) otherwise.
    """
    valid = [
        (label, g) for label, g in labeled_gdfs
        if g is not None and not g.empty and g.geometry.notna().any()
    ]
    if not valid:
        raise ValueError("No valid (non-empty) GeoDataFrames provided for PRS92 zone detection.")

    warnings = []
    all_bounds = []
    for label, gdf in valid:
        g = gdf
        if g.crs is None:
            g = g.set_crs(epsg=4326)
            warnings.append(
                f"No CRS found in the '{label}' layer -- assuming WGS84. "
                "Measurements may be incorrect if the actual CRS is different."
            )
        epsg = g.crs.to_epsg()
        g_wgs84 = g.to_crs(epsg=4326) if epsg != 4326 else g

        bounds = g_wgs84.total_bounds
        if np.isnan(bounds).any():
            raise ValueError(
                f"Cannot determine PRS92 zone because the '{label}' layer "
                f"contains no valid geometry."
            )
        all_bounds.append(bounds)

    warning = "\n".join(warnings) if warnings else None

    minx = min(b[0] for b in all_bounds)
    maxx = max(b[2] for b in all_bounds)
    center_lon = (minx + maxx) / 2

    for lon_min, lon_max, epsg, zone_label in PRS92_ZONE_BOUNDS:
        if lon_min <= center_lon < lon_max:
            if not (lon_min <= minx and maxx < lon_max):
                print(
                    f"⚠️ Dataset longitude range ({minx:.4f}° to {maxx:.4f}°E) "
                    f"extends outside the detected {zone_label} bounds "
                    f"({lon_min}°E–{lon_max}°E). Features near the dataset edge "
                    f"may be very slightly less accurate."
                )
            print(f"ℹ️ Auto-detected PRS92 {zone_label} (EPSG:{epsg}) "
                  f"from data bbox-midpoint longitude {center_lon:.4f}°E")
            return epsg, warning

    raise ValueError(f"Could not determine PRS92 zone for longitude {center_lon}")


def fix_geometry(geom):
    """Repairs an invalid geometry via buffer(0), falling back to
    make_valid() if that isn't enough. Returns None for a None, empty,
    or unrepairable geometry."""
    if geom is None or geom.is_empty:
        return None
    try:
        if not geom.is_valid:
            # buffer(0) is a polygon-repair technique. Applied to
            # LineString/MultiLineString it can collapse the geometry
            # into POLYGON EMPTY (confirmed empirically, including on
            # already-valid LineStrings), silently destroying that
            # feature and changing its geometry type. Line geometries
            # are repaired directly with make_valid() instead, which
            # handles both geometry families correctly.
            if geom.geom_type in {"Polygon", "MultiPolygon"}:
                geom = geom.buffer(0)
            if not geom.is_valid:
                geom = make_valid(geom)
        if geom.is_empty:
            return None
        return geom
    except:
        return None

import threading
import queue
import time
from shapely.prepared import prep


# ========================================
# MINIMUM FRONTAGE THRESHOLD
# ========================================
# MIN_FRONTAGE_THRESHOLD: a computed ROAD_FRONTAGE of this many meters or less
# is NOT treated as genuine road frontage. This is a separate, deliberate
# task from the Road Classification feature above/below it in this file --
# it does not gate WHICH parcels/roads participate, it changes what counts
# as frontage once the existing algorithm has already run. See the
# "MINIMUM FRONTAGE THRESHOLD" section further down (search for
# MIN_FRONTAGE_THRESHOLD in process_frontage_single()) for the exact
# mechanics and the investigation that led to this value.
#
# Root cause this mitigates: _edge_covered_portion() (untouched, unmodified)
# derives "covered length" purely from the range of positions where a road
# geometry projects onto a boundary segment's own axis -- it does not
# consider the road's ORIENTATION relative to that segment. A road that
# merely crosses near-perpendicular through a segment's buffer zone (rather
# than running alongside it) can still register a small, non-zero "covered"
# length purely from a shallow angle of approach. Confirmed via direct,
# reproducible testing against the actual function (not assumed).
#
# Business rule confirmed by the project lead: a road that only touches or
# crosses a parcel corner, without substantially running alongside an edge,
# should NOT contribute any frontage. A plain minimum-length threshold is a
# PARTIAL mitigation for this -- it only catches short slivers; a crossing
# road at a shallower angle can still produce a longer, non-trivial "covered"
# length that this threshold will not catch. A more complete,
# orientation-aware fix is tracked as a separate follow-on task.
#
# Value basis: confirmed against real production data that genuine road
# frontage values as small as 1-2m exist, but nothing at or below 0.9m was
# observed to be genuine -- 0.9 was chosen as a value inside that gap.
MIN_FRONTAGE_THRESHOLD = 0.9


# ========================================
# FRONTAGE BUFFER TOLERANCE (EXPERIMENT)
# ========================================
# FRONTAGE_BUFFER_TOLERANCE: the buffer distance (in meters) _edge_covered_portion()
# uses around each boundary segment when deciding whether a road counts as
# "near" that segment at all. This is the SAME "tol" parameter that was
# explicitly off-limits for the entire Road Classification task
# ("Do NOT touch _edge_covered_portion()'s tolerance value...") -- it is
# being changed here ONLY because the project lead explicitly authorized
# a deliberate experiment to observe the effect of a smaller tolerance
# (10m -> 5m) on the buffer-bleed/sliver behavior documented in
# ROAD_FRONTAGE_ALGORITHM_INVESTIGATION.md (Seksyon 2.1, Candidate 3 --
# "Adaptive Tolerance" was rated weakest there precisely because it
# treats a symptom, not the root cause; this experiment exists to
# directly observe that tradeoff, not to declare it solved).
#
# Pulled out as a single named constant (rather than a scattered literal)
# specifically so this experiment is easy to find, adjust, and revert.
# Original, pre-experiment value was 10 (matching the algorithm's
# original design, in production for all of the Road Classification
# feature's history). The buffer-diagnostic QA layer previously used to
# visually inspect the effect of this value has been removed (see the
# module docstring's OUTPUTS section) -- effects of changing this value
# are now only observable via the actual CAMA_ROAD_FRONTAGE/CAMA_DEPTH
# output columns themselves.
FRONTAGE_BUFFER_TOLERANCE = 9


# ========================================
# PARALLEL-VALIDATION ALGORITHM
# ========================================
# This replaces the earlier proximity-only frontage detection
# (_edge_covered_portion(), now removed) with a three-stage pipeline:
# candidate detection (buffer, unchanged) -> parallel validation (NEW) ->
# measurement. See _edge_covered_pieces() below for the full
# implementation and rationale.
#
# PARALLEL_ANGLE_THRESHOLD: maximum angle (degrees) between a boundary
# segment's own direction and a road piece's local direction for that
# road piece to be considered "running alongside" the segment (genuine
# frontage) rather than merely crossing near it. A road piece whose
# local angle exceeds this is rejected outright, regardless of
# proximity -- this is what fixes the crossing-road sliver problem
# (a road crossing near-perpendicular through a segment's buffer zone
# used to register a small, spurious "covered" length purely from a
# shallow angle of approach; it now contributes nothing). Deliberately
# NOT yet tuned against real cadastral data -- treat this value as a
# starting point for the first round of real-data validation, not a
# final constant.
PARALLEL_ANGLE_THRESHOLD = 25  # degrees

# ROAD_DENSIFY_INTERVAL: before running the parallel-angle test, the
# candidate road geometry is resampled into consecutive pieces of
# approximately this length (meters), rather than testing angle between
# the road's own ORIGINAL vertices. This exists specifically so the
# algorithm's behavior does not depend on how densely or sparsely the
# source road layer happened to be digitized -- a road digitized with
# few, widely-spaced vertices and the same road digitized with many
# closely-spaced vertices now produce the same validation result, since
# both are resampled to this same fixed interval before any angle is
# measured. Also used as the basis for measurement (every resampled
# point of a validated run is projected onto the segment's axis, not
# just the road's original vertices) so a curved road's true covered
# range is not underestimated by relying on sparse original vertices.
ROAD_DENSIFY_INTERVAL = 1.0  # meters


# ========================================
# TWO-STAGE GATE+MEASURE (EXPERIMENT, NOT VALIDATED)
# ========================================
# TWO_STAGE_FRONTAGE_ENABLED: when True, process_frontage_single() uses a
# two-stage tolerance instead of a single FRONTAGE_BUFFER_TOLERANCE:
#   Stage 1 (TWO_STAGE_GATE_TOLERANCE): a strict, narrow gate -- if NO
#     boundary segment gets ANY coverage at this tight tolerance, the
#     parcel is rejected outright (ROAD_FRONTAGE=0), and Stage 2 never runs.
#   Stage 2 (TWO_STAGE_MEASURE_TOLERANCE): for parcels that passed the
#     gate, the WHOLE boundary is re-measured at this wider tolerance, and
#     THIS result (not a sum/combination with Stage 1) becomes the final
#     ROAD_FRONTAGE.
#
# THIS IS AN ACTIVE EXPERIMENT, EXPLICITLY REQUESTED BY THE PROJECT LEAD,
# NOT A VALIDATED SOLUTION. Prototype testing (outside this file) already
# found real limitations before this was ever wired in here:
#   - Does NOT fix the crossing-road sliver problem (Isyu B): a parcel
#     that passes the Stage-1 gate via genuine frontage on one edge can
#     still pick up an unrelated crossing-road sliver on a DIFFERENT edge
#     during the wider Stage-2 remeasurement, because the gate is
#     evaluated at the PARCEL level, not per-road or per-segment.
#   - Only PARTIALLY and INCONSISTENTLY helps the thin-parcel bleed
#     problem (Isyu A): tested against the same 8m-wide synthetic strip
#     at multiple road-offset distances, this approach looked like a big
#     improvement at one specific offset (~3m, "true" gap to the far edge
#     ~11m) but was AS BAD AS or WORSE than the original single-tolerance
#     bug at closer offsets (1-2m, "true" gap ~9-10m) -- it does not solve
#     the geometric root cause, it only relocates where the same
#     buffer-bleed threshold sits. This matches the earlier assessment in
#     ROAD_FRONTAGE_ALGORITHM_INVESTIGATION.md (Candidate 3, "Adaptive
#     Tolerance") that this class of fix treats a symptom, not the root
#     cause.
#
# The buffer-diagnostic QA layer previously used to visually inspect
# Stage 2's buffer zones (TWO_STAGE_MEASURE_TOLERANCE) on real parcel
# data has been removed (see the module docstring's OUTPUTS section) --
# effects of this experiment are now only observable via the actual
# CAMA_ROAD_FRONTAGE/CAMA_DEPTH output columns themselves.
TWO_STAGE_FRONTAGE_ENABLED = False
TWO_STAGE_GATE_TOLERANCE = 5
TWO_STAGE_MEASURE_TOLERANCE = 9


# ========================================
# CROSS-PARCEL CONFLICT RESOLUTION
# ========================================
# CROSS_PARCEL_CONFLICT_RESOLUTION_ENABLED: the existing per-segment
# frontage algorithm (above) is entirely PER-PARCEL -- it has zero
# awareness of any OTHER parcel's boundary or its own frontage claim.
# Confirmed via real production data: two adjacent, non-overlapping
# parcels (no polygon-level area overlap between them -- verified) can
# each INDEPENDENTLY claim the SAME physical stretch of road as their
# own frontage, when one parcel is large/elongated (genuinely touching
# the road for a long stretch) and a smaller neighboring parcel sits
# close enough to that same road stretch to also register it within its
# own FRONTAGE_BUFFER_TOLERANCE, even though only one of them can
# genuinely be "facing" that road in the way frontage is meant to
# represent.
#
# Real example that surfaced this: PIN ...-013-001 (a large, ~332m-long,
# ordinary Residential-1 parcel, ROAD_FRONTAGE=621.04m) and PIN
# ...-013-020 (a small parcel sitting near the same road stretch,
# ROAD_FRONTAGE=11.93m, confirmed correct by the project lead against a
# known-good reference). Both parcels' OWN, independent per-segment
# measurement is individually correct by the existing algorithm's own
# logic -- the conflict only becomes visible when comparing across
# parcels, which nothing in the existing algorithm ever does.
#
# Mechanism: after Pass 1 (below) computes each parcel's own covered
# pieces exactly as before (untouched), a NEW pass compares every
# parcel's covered piece against every OTHER parcel's covered piece
# (spatially indexed via STRtree for tractability across a full, large
# batch -- this is NOT limited to any specific example PIN, it runs
# against every parcel in the source). Two pieces from DIFFERENT parcels
# are treated as a genuine conflict (the same physical road stretch)
# when their buffered zones (buffered by CROSS_PARCEL_CONFLICT_TOLERANCE)
# intersect. On conflict, the SMALLER piece survives and the LARGER one
# is disregarded entirely (removed before frontage_total is summed) --
# per the project lead's explicit rule and reasoning: an anomalously
# large claim overlapping a smaller, independently-plausible claim is
# the more likely of the two to be wrong.
#
# Resolution is PER PIECE (a parcel's own individually continuous,
# post-linemerge frontage stretch -- e.g. one side of a corner lot),
# NOT per whole-parcel total -- a parcel can have one piece disregarded
# (conflicting with a neighbor) while a DIFFERENT piece of the SAME
# parcel (e.g. its other, non-conflicting side) is kept untouched. This
# was an explicit design requirement: a parcel must not lose genuine,
# non-conflicting frontage just because one of its OTHER sides conflicts
# with a neighbor.
#
# CROSS_PARCEL_CONFLICT_TOLERANCE reuses FRONTAGE_BUFFER_TOLERANCE's
# scale by default (see below) -- two pieces drawn along two DIFFERENT
# parcels' own boundaries (not the road itself) will not generally be
# exactly coincident even when they represent "the same" road stretch
# (e.g. one parcel touches the road at 0m, a neighboring parcel's own
# edge is a few meters further back) -- the conflict tolerance needs to
# be at least as generous as the frontage-detection tolerance itself for
# this to reliably catch the cases it's meant to catch.
CROSS_PARCEL_CONFLICT_RESOLUTION_ENABLED = True
CROSS_PARCEL_CONFLICT_TOLERANCE = FRONTAGE_BUFFER_TOLERANCE


# ========================================
# ROAD TYPE FILTER UTILITIES
# ========================================
# ROAD_TYPE_COLUMN_CANDIDATES: case-insensitive column-name aliases used to
# locate a road-classification column in a user-supplied road layer.
# Copied verbatim from lot_location.py (the canonical implementation of
# Road Type filtering in this codebase) so both tools agree on what counts
# as a "ROAD_TYPE-like" column. Do not diverge without updating both files.
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


# ========================================
# LOT CLASSIFICATION UTILITIES
# ========================================
# LOT_LOCATION_COLUMN_CANDIDATES: case-insensitive column-name alias for
# lot_location.py's single output column.
#
# lot_location.py now writes ONE column, "LOT_LOCATION", containing the
# human-readable classification directly ("Inner Lot"/"Road Lot"/
# "Corner Lot") -- per project decision, the column named after the tool
# should hold the actual classification, not an internal numeric code,
# and there is no reason for an end user to see a code column in the
# attribute table. The old two-column format (numeric LOT_LOCATION +
# text LOT_LABEL) is still fully supported for backward compatibility
# with files produced by the earlier version of the tool -- detection
# below is CONTENT-based, not column-name-based: whichever format is
# actually found in the "lot_location" column's values is what's used.
# There is no longer a separate LOT_LABEL column to check -- every file
# lot_location.py has ever produced, old or new format, always has
# "lot_location" present, so no real dataset depends on a label-only
# fallback.
KNOWN_LOT_LABEL_VALUES = {"Inner Lot", "Road Lot", "Corner Lot"}
LOT_LOCATION_COLUMN_CANDIDATES = ("cama_lot_location",)

# Tri-state result of inspecting a parcel layer for a usable classification
# column -- kept as named states rather than a bare has_lot_location bool
# so "column present but unusable" (e.g. an all-NULL LOT_LOCATION column)
# stays distinguishable from "column absent entirely" without another
# signature change later.
LOT_STATE_NOT_FOUND = "not_found"   # no LOT_LOCATION column at all
LOT_STATE_UNUSABLE = "unusable"     # column present but no usable values
LOT_STATE_FOUND = "found"           # a usable column was found


# ========================================
# EXISTING OUTPUT-COLUMN CONFLICT DETECTION
# ========================================
# OUTPUT_COLUMN_TARGETS: this tool's three output column names, checked
# for pre-existing conflicts in a selected Land Parcel source (see the
# merged background read in _refresh_parcel_classification() below, and
# the combined dialog in on_run()). Business decision confirmed by the
# project lead: all three are checked, not just CAMA_ROAD_FRONTAGE --
# they are one feature set computed together in the same run, so a
# source with (for example) an existing CAMA_DEPTH column but no existing
# CAMA_ROAD_FRONTAGE column still needs a conflict warning, to avoid
# ending up with an old CAMA_DEPTH value sitting alongside a freshly-
# computed CAMA_ROAD_FRONTAGE from a DIFFERENT run/computation -- an
# inconsistent, misleading combination.
#
# Cross-tool CAMA_ prefix standard: every column this tool CREATES gets a
# "CAMA_" prefix -- matches road_width.py's own CAMA_ROAD_WIDTH
# convention. These targets check for the NEW, prefixed names ONLY --
# never the OLD, non-prefixed names (e.g. a plain "DEPTH" column left
# over from a pre-CAMA_-prefix version of this tool). Per the same
# principle already established in road_width.py: this tool never
# auto-detects, auto-removes, or auto-overwrites an old, non-prefixed
# column -- if one exists, it is simply left alone, untouched, and a NEW
# CAMA_-prefixed column is created alongside it. Only conflicts against
# the NEW naming scheme are ever surfaced to the user.
#
# Matching is EXACT (case-insensitive) -- "CAMA_ROAD_FRONTAGE" vs
# "ROAD_TYPE" is not a match; only "cama_road_frontage"/
# "CAMA_ROAD_FRONTAGE"/"Cama_Road_Frontage"/etc. (same letters, any
# casing) count as the same column.
OUTPUT_COLUMN_TARGETS = ("CAMA_ROAD_FRONTAGE", "CAMA_DEPTH", "CAMA_DEPTH_WIDTH_RATIO")

def _detect_lot_classification(gdf):
    """
    Inspect a parcel GeoDataFrame for a usable Inner/Road/Corner Lot
    classification source. Checks a single column, "lot_location"
    (case-insensitive), and determines which of two supported CONTENT
    formats it holds:
      - "text"    : values are (at least partly) the known, literal
                    strings "Inner Lot"/"Road Lot"/"Corner Lot" -- the
                    current lot_location.py output format. Trusted
                    directly, since these values are self-describing and
                    can be verified by reading them.
      - "numeric" : values are 0/1/2 -- the OLD lot_location.py output
                    format (kept for backward compatibility with files
                    already generated by earlier versions of the tool).
                    Only used when no recognizable text value is present,
                    since a user-authored column that merely happens to
                    be named "lot_location" could use different numbering
                    entirely, and there's no way to catch that mismatch
                    from the number alone.

    Returns (state, column_name, kind, inner_lot_mask):
      state          : LOT_STATE_NOT_FOUND / LOT_STATE_UNUSABLE / LOT_STATE_FOUND
      column_name    : the actual column name found, or None
      kind           : "text" or "numeric", or None
      inner_lot_mask : pandas boolean Series (index-aligned to gdf), True
                       where the row is classified as Inner Lot -- only
                       populated when state == LOT_STATE_FOUND, else None.
    """
    if gdf is None or len(gdf) == 0:
        return (LOT_STATE_NOT_FOUND, None, None, None)

    loc_col = next(
        (c for c in gdf.columns if c.lower() in LOT_LOCATION_COLUMN_CANDIDATES),
        None
    )
    if not loc_col:
        return (LOT_STATE_NOT_FOUND, None, None, None)

    str_vals = gdf[loc_col].astype(str).str.strip()
    recognized = str_vals.isin(KNOWN_LOT_LABEL_VALUES)
    if recognized.any():
        # At least one row has a value we can trust literally -- any row
        # whose value ISN'T one of the three known labels (typo, blank,
        # unrelated text) is simply treated as "not Inner Lot" (never
        # skipped) rather than guessed at.
        return (LOT_STATE_FOUND, loc_col, "text", str_vals == "Inner Lot")

    numeric = pd.to_numeric(gdf[loc_col], errors="coerce")
    if numeric.notna().any():
        return (LOT_STATE_FOUND, loc_col, "numeric", numeric == 0)

    return (LOT_STATE_UNUSABLE, loc_col, None, None)


def resolve_classification(brgy_gdf, use_lot_classification, filter_by_road_type_active, excluded_road_types):
    """
    Single, centralized decision point for "what should this parcel
    source's Road Classification behavior be". Resolves the GUI's
    checkbox states plus ONE specific parcel layer's actual columns into
    one effective processing directive, so process_frontage_single()
    never branches on this logic itself -- it only consumes the result.
    Called once per parcel source in run_processing(), since sources are
    evaluated independently (a batch may mix sources that do and don't
    have a usable LOT_LOCATION/LOT_LABEL column, AND the user may have
    only checked the per-source classification checkbox for some of
    them).

    use_lot_classification here is already resolved to THIS specific
    source (run_processing() looks it up from the per-source
    parcel_classification_selection dict before calling this function --
    each selected Land Parcel file/table gets its own checkbox in the
    GUI). filter_by_road_type_active, by contrast, is a single flag,
    since Road Network only ever has one selected source. The two are
    mutually exclusive at the GUI level (see open_main_window()'s
    trace_add() wiring): checking Filter by Road Type unchecks every
    per-source classification checkbox, and checking any per-source
    classification checkbox unchecks Filter by Road Type -- but "neither
    checked, for this source" is a normal, valid state (today's
    original, ungated behavior: all roads used, no parcels skipped). If
    both were somehow True at once (shouldn't happen given the GUI
    wiring), use_lot_classification wins as a defensive default -- this
    is unrelated to, and does not override, _detect_lot_classification()'s
    own content-based LOT_LABEL-vs-LOT_LOCATION priority (see that
    function's docstring).

    Parameters
    ----------
    brgy_gdf                    : the parcel GeoDataFrame for ONE source.
    use_lot_classification      : whether THIS source's own "Use
                                   LOT_LOCATION/LOT_LABEL" checkbox is checked.
    filter_by_road_type_active  : "Filter by Road Type" checkbox state.
    excluded_road_types         : list of ROAD_TYPE values unchecked in the
                                   Filter by Road Type checklist. Only
                                   consulted when filter_by_road_type_active.

    Returns a dict:
      {
        "mode": "lot_classification" | "filter_by_road_type" | "no_gating",
        "skip_mask": pandas boolean Series or None,
        "excluded_road_types": list[str],  # always [] unless mode is
                               "filter_by_road_type".
        "lot_column": str or None,
        "lot_kind": "text" | "numeric" | None,
      }
    """
    if use_lot_classification:
        state, col_name, kind, mask = _detect_lot_classification(brgy_gdf)
        if state == LOT_STATE_FOUND:
            return {
                "mode": "lot_classification",
                "skip_mask": mask,
                "excluded_road_types": [],
                "lot_column": col_name,
                "lot_kind": kind,
            }
        # Checkbox checked, but THIS particular source doesn't actually
        # have a usable column -- falls back to no gating for this
        # source only (per-source evaluation; a mixed batch is expected).
        return {
            "mode": "no_gating",
            "skip_mask": None,
            "excluded_road_types": [],
            "lot_column": col_name,
            "lot_kind": kind,
        }

    if filter_by_road_type_active:
        return {
            "mode": "filter_by_road_type",
            "skip_mask": None,
            "excluded_road_types": list(excluded_road_types or []),
            "lot_column": None,
            "lot_kind": None,
        }

    # Neither checkbox active -- today's original, unmodified default:
    # all roads used, no parcels skipped.
    return {
        "mode": "no_gating",
        "skip_mask": None,
        "excluded_road_types": [],
        "lot_column": None,
        "lot_kind": None,
    }


def _longest_linestring(geom):
    """Return the longest LineString inside LineString/MultiLineString/GeometryCollection."""
    if geom is None or geom.is_empty:
        return None
    gt = geom.geom_type
    if gt == "LineString":
        return geom
    if gt == "MultiLineString":
        return max(geom.geoms, key=lambda g: g.length, default=None)
    if gt == "GeometryCollection":
        lines = [g for g in geom.geoms if g.geom_type in ("LineString", "MultiLineString")]
        best = None
        best_len = 0.0
        for g in lines:
            ls = _longest_linestring(g)
            if ls and ls.length > best_len:
                best = ls
                best_len = ls.length
        return best
    return None


# ========================================
# OUTPUT FILENAME CONFLICT HANDLING
# ========================================
# Ported directly from road_width.py's canonical pattern (see that file's
# resolve_output_base_name() for the original, validated implementation
# this is copied from). This tool now writes exactly ONE output file per
# source -- the previously-paired QA outputs (frontage_lines/VM,
# segment_buffers) and the with_output_suffix() helper that derived their
# filenames from this resolved name have been removed entirely (see the
# module docstring's OUTPUTS section, and process_frontage_single()'s
# docstring, for the full removal rationale). resolve_output_base_name()
# itself is unchanged -- it still resolves the MAIN output's own
# "Create New File" numbering exactly as before.
def _split_trailing_number(base_name: str):
    """
    Splits a base name into (root, existing_number) if it ends with
    "_<digits>" (e.g. "landparcel_1" -> ("landparcel", 1)), else returns
    (base_name, None) unchanged.
    """
    m = re.match(r'^(.*)_(\d+)$', base_name)
    if m:
        return m.group(1), int(m.group(2))
    return base_name, None


def resolve_output_base_name(folder: str, desired_base_name: str, ext: str = "gpkg") -> str:
    """
    Determines the actual output base name (no extension) to use for a
    NEW file in `folder`, given the DESIRED name -- normally the Land
    Parcel source's own filename, unchanged, with no tool-name suffix
    appended.

    Rule: reuse the desired name exactly if nothing of that name exists
    yet in `folder`. If it already exists, NEVER overwrite -- instead,
    strip any existing trailing "_<N>" from the desired name to get a
    root (e.g. "landparcel_1" -> root "landparcel"), scan `folder` for
    every file matching "<root>_<N>.<ext>", and use "<root>_<max(N)+1>"
    -- the highest N found ANYWHERE in the folder, not just "the source
    file's own N + 1".

    This function decides the number ONCE, for the MAIN (and, as of
    this tool's current output shape, only) output.
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
        pass  # folder unreadable for some reason -- fall through with max_n=0, worst case reuses N=1

    return f"{root}_{max_n + 1}"


def open_in_global_mapper(output_path):
    """Opens output_path in Global Mapper (subprocess), if both
    GM_EXE_PATH and output_path exist. Simpler than other tool files'
    load_in_global_mapper() (no EnumWindows focus-existing-window
    step). GM_EXE_PATH is currently hardcoded -- see CONFIGURATION
    section above and the module docstring's SIDE EFFECTS note for the
    planned dynamic-discovery follow-up."""
    if os.path.exists(GM_EXE_PATH) and os.path.exists(output_path):
        subprocess.Popen([GM_EXE_PATH, output_path], shell=True)


def split_boundary_to_segments(boundary):
    """Splits a LineString or MultiLineString boundary into individual
    2-point LineString segments (one per consecutive coordinate pair),
    for per-segment frontage testing against the road network."""
    segments = []
    if boundary.geom_type == 'LineString':
        coords = list(boundary.coords)
        segments.extend([LineString([coords[i], coords[i + 1]]) for i in range(len(coords) - 1)])
    elif boundary.geom_type == 'MultiLineString':
        for line in boundary.geoms:
            coords = list(line.coords)
            segments.extend([LineString([coords[i], coords[i + 1]]) for i in range(len(coords) - 1)])
    return segments


def _direction_vector(p0, p1):
    """Unit direction vector from point p0 to point p1. Returns (0.0, 0.0)
    for a degenerate (zero-length) pair -- callers must check for this
    before using the result, since it has no meaningful angle."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return (0.0, 0.0)
    return (dx / length, dy / length)


def _angle_between_deg(d1, d2):
    """Angle between two unit direction vectors, in degrees, folded into
    the 0-90 range (absolute value of the dot product) -- direction
    SIGN doesn't matter for parallelism (a road running "backwards"
    relative to the segment's own vertex ordering is still parallel)."""
    dot = d1[0] * d2[0] + d1[1] * d2[1]
    dot = max(-1.0, min(1.0, abs(dot)))  # clamp against float rounding past +/-1
    return math.degrees(math.acos(dot))


def _densify_linestring(line, interval):
    """Resamples `line` into a list of points spaced approximately
    `interval` apart (including both endpoints), regardless of the
    line's own original vertex spacing. This is the mechanism that
    makes parallel validation and measurement independent of source
    digitizing density -- see ROAD_DENSIFY_INTERVAL's module-level
    docstring."""
    length = line.length
    if length == 0:
        return [line.coords[0]]
    n_steps = max(1, int(length / interval))
    return [line.interpolate(i * length / n_steps).coords[0] for i in range(n_steps + 1)]


def _project_run_to_segment(run_pts, seg):
    """Projects every point of one validated, contiguous run onto seg's
    own axis, returning the LineString spanning the resulting lo/hi
    range -- or None if the run collapses to a single point on that
    axis (e.g. a run running exactly perpendicular to seg, which
    shouldn't normally survive the angle test but is guarded against
    here regardless)."""
    fracs = [seg.project(Point(p)) for p in run_pts]
    lo, hi = min(fracs), max(fracs)
    if hi - lo < 1e-9:
        return None
    return LineString([seg.interpolate(lo), seg.interpolate(hi)])


def _edge_covered_pieces(seg, road_union, tol=FRONTAGE_BUFFER_TOLERANCE,
                          angle_threshold=PARALLEL_ANGLE_THRESHOLD,
                          densify_interval=ROAD_DENSIFY_INTERVAL):
    """For one elementary boundary segment (vertex-to-vertex), finds
    every genuinely road-adjacent portion of it via a three-stage
    pipeline, replacing the earlier proximity-only _edge_covered_portion().

    Stage 1 -- Candidate detection (unchanged from the earlier
    implementation): a buffer confined to this segment's OWN footprint
    (flat-capped at cap_style=2, never extended past the segment's own
    two endpoints) isolates nearby road geometry. This is what prevents
    a corner's two meeting segments from "seeing" road that only runs
    alongside the OTHER segment -- a confirmed, reproducible defect of
    buffering the road network as a whole.

    Stage 2 -- Parallel validation (NEW): the candidate road geometry is
    densified into fixed-length pieces (ROAD_DENSIFY_INTERVAL), and EACH
    piece is independently tested for whether its own local direction is
    within PARALLEL_ANGLE_THRESHOLD degrees of seg's direction. A road
    piece that merely crosses through the buffer zone at a steep angle
    -- previously counted as a small, spurious "covered" sliver purely
    from proximity -- is now rejected outright, regardless of how close
    it is. Consecutive validated pieces are grouped into "runs"; a
    SINGLE rejected piece breaks a run immediately (no gap tolerance --
    deliberately strict for this first implementation, so the
    algorithm's behavior stays fully deterministic and easy to validate
    against real data before any smoothing heuristic is considered).

    Stage 3 -- Measurement: each validated run is projected onto seg's
    own axis using EVERY one of its densified points (not just the
    road's original vertices), so a curved road's true covered range
    isn't underestimated by relying on sparse source vertices.

    Returns a LIST of covered LineString pieces along `seg` -- possibly
    empty, possibly more than one (e.g. two separate parallel runs with
    a rejected crossing piece between them). Deliberately NEVER merges
    these into a single min-to-max range across the whole candidate
    geometry -- doing so would silently re-include geometry the
    parallel-validation stage specifically rejected. Merging validated
    pieces that happen to be very close together is a separate,
    deliberately deferred concern (see linemerge() at the call site,
    which only welds pieces that already share an endpoint).
    """
    zone = seg.buffer(tol, cap_style=2)
    road_in_zone = road_union.intersection(zone)
    if road_in_zone.is_empty:
        return []

    seg_p0, seg_p1 = seg.coords[0], seg.coords[-1]
    d_S = _direction_vector(seg_p0, seg_p1)
    if d_S == (0.0, 0.0):
        return []

    gt = road_in_zone.geom_type
    if gt == "LineString":
        road_lines = [road_in_zone]
    elif gt == "MultiLineString":
        road_lines = list(road_in_zone.geoms)
    elif gt == "GeometryCollection":
        road_lines = []
        for g in road_in_zone.geoms:
            if g.geom_type == "LineString":
                road_lines.append(g)
            elif g.geom_type == "MultiLineString":
                road_lines.extend(g.geoms)
        # Point/MultiPoint members of the collection are skipped -- a
        # road that only grazes the zone at an isolated point has no
        # local direction to validate against.
    else:
        # Point / MultiPoint -- road only touches the zone at isolated
        # points, no line direction to test. Correctly contributes
        # nothing (a single touching point can't be "parallel").
        return []

    covered_pieces = []
    for line in road_lines:
        densified_pts = _densify_linestring(line, densify_interval)
        if len(densified_pts) < 2:
            continue

        current_run = []
        for i in range(len(densified_pts) - 1):
            a, b = densified_pts[i], densified_pts[i + 1]
            d_R = _direction_vector(a, b)
            valid = d_R != (0.0, 0.0) and _angle_between_deg(d_S, d_R) <= angle_threshold

            if valid:
                if not current_run:
                    current_run.append(a)
                current_run.append(b)
            else:
                if len(current_run) >= 2:
                    piece = _project_run_to_segment(current_run, seg)
                    if piece is not None:
                        covered_pieces.append(piece)
                current_run = []

        if len(current_run) >= 2:
            piece = _project_run_to_segment(current_run, seg)
            if piece is not None:
                covered_pieces.append(piece)

    return covered_pieces


def calculate_centroid_to_road_depth(parcel_geom, road_gdf):
    """Fallback depth measure: straight-line distance from the
    parcel's centroid to the nearest point on any road in road_gdf."""
    centroid = parcel_geom.centroid
    min_distance = float("inf")
    for road in road_gdf.geometry:
        p1, p2 = nearest_points(centroid, road)
        dist = p1.distance(p2)
        if dist < min_distance:
            min_distance = dist
    return min_distance


def calculate_depth_perpendicular(parcel_geom, road_buffer, max_depth=1000):
    """
    Primary depth measure: finds the parcel boundary's longest segment
    that falls within road_buffer (the frontage segment), then casts a
    perpendicular line from its midpoint into the parcel (in both
    directions, keeping whichever intersects the parcel more) up to
    max_depth, and returns the length of that line's intersection with
    the parcel -- an approximation of "how deep" the parcel extends
    back from its road frontage.

    Args:
        parcel_geom: the parcel polygon.
        road_buffer: buffered road geometry defining the frontage zone.
        max_depth (float): maximum perpendicular probe length.

    Returns:
        float: the perpendicular depth, or 0 if no frontage segment is
        found or the probe line doesn't intersect the parcel.
    """
    boundary = parcel_geom.boundary
    segments = split_boundary_to_segments(boundary)
    frontage_segments = [seg for seg in segments if seg.within(road_buffer)]
    if not frontage_segments:
        return 0

    frontage_seg = max(frontage_segments, key=lambda s: s.length)
    midpoint = frontage_seg.interpolate(0.5, normalized=True)

    x1, y1 = frontage_seg.coords[0]
    x2, y2 = frontage_seg.coords[1]
    dx = x2 - x1
    dy = y2 - y1

    perp_dx, perp_dy = -dy, dx
    length = math.hypot(perp_dx, perp_dy)
    if length == 0:
        return 0
    perp_dx /= length
    perp_dy /= length

    line1 = LineString([midpoint, (midpoint.x + perp_dx * max_depth, midpoint.y + perp_dy * max_depth)])
    line2 = LineString([midpoint, (midpoint.x - perp_dx * max_depth, midpoint.y - perp_dy * max_depth)])

    depth_line = max([line1, line2], key=lambda l: l.intersection(parcel_geom).length)
    intersection = depth_line.intersection(parcel_geom)
    if intersection.is_empty:
        return 0
    elif intersection.geom_type == 'MultiLineString':
        return max(part.length for part in intersection.geoms)
    elif intersection.geom_type == 'LineString':
        return intersection.length
    else:
        return 0


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


def _force_2d(gdf):
    """Drops any Z coordinate from every geometry in gdf. Safe to call
    on already-2D geometry (no-op in that case).

    Fix for: LandParcel_3.gpkg-style sources carry a Z coordinate on
    every vertex (each coordinate a (x, y, 0.0) 3-tuple), while
    RoadNetwork.gpkg-style sources are pure 2D. That asymmetry causes
    a bare `x1, y1 = coords[0]`-style unpack (see
    process_frontage_single()'s inline depth-direction block) to raise
    `ValueError: too many values to unpack (expected 2)` the moment it
    hits a 3D coordinate. Applied once at every read/reuse site for
    both brgy_gdf (parcel) and road_gdf (road network) -- local file,
    DB table, and the cached road_gdf reuse path alike -- so nothing
    downstream (resolve_classification(), process_frontage_single(),
    or any future geometry-touching code) ever has to special-case a
    3D input again.
    """
    gdf = gdf.copy()
    gdf["geometry"] = gdf["geometry"].apply(
        lambda geom: transform(lambda x, y, z=None: (x, y), geom)
        if geom is not None else None
    )
    return gdf


# ========================================
# PROGRESS WINDOW
# ========================================
# ============================================================
# Progress Event Protocol v9 — Phase 4 migration (road_frontage.py)
# ============================================================
# PresentationState, the Presentation Policy, and the Tkinter View are
# no longer defined locally in this file -- identical to
# lot_location.py's Phase 3 migration, both tools now import the same
# three classes from utils/progress_framework.py instead of each
# keeping its own copy. Pure extraction: no behavior change, no new
# abstraction, no wrapper/adapter/compatibility layer.
#
#   Worker (worker(), inside run_processing())      -> owns q.put() events
#   Main-thread Message Handler (poll_queue())       -> owns Tkinter calls
#   ProgressWindow                                   -> owns window/widgets
#
# road_width.py is not part of this migration and is not touched by it
# -- see progress_framework.py's own top-of-file comment for why.
#
# D-Cancel (later task, this rollout): worker()/poll_queue() are NO
# LONGER unchanged by this file's own history -- both were updated to
# carry a cancel_flag and a fifth `cancelable` field on every "update"
# event, and poll_queue() gained a "cancelled" event kind. See
# ProgressWindow's own docstring (just below) and run_processing()'s
# own docstring for the current, accurate contract -- this block is
# kept for the ORIGINAL migration's own history/rationale, not as a
# claim that nothing has changed since.
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
    thread. Progress Event Protocol v9 role: host, not decision-maker
    (see ProgressPresentationPolicy / TkinterProgressView, imported
    from progress_framework.py, shared with lot_location.py).

    D-Cancel (this rollout): adapted to the same cancellation-capable
    shape lot_location.py already uses -- an OPTIONAL cancel_flag
    (from utils.progress_framework.new_cancel_flag(), one per run) is
    now accepted and threaded straight through to TkinterProgressView,
    which is what actually wires the window's title-bar close button
    to Cancel (see progress_framework.py's own docstring for that
    mechanism -- it is untouched here, only actually being used by this
    file for the first time). Everything else about this class --
    window construction, widget layout, public method names -- is
    unchanged from before this rollout. A ProgressWindow constructed
    WITHOUT cancel_flag (cancel_flag=None, the default) behaves exactly
    as it did before this task: no Cancel wiring at all.

    See run_processing() for where the cancel_flag actually comes from
    and for this tool's actual Cancel checkpoints (process_frontage_
    single()'s own per-pass checkpoints, checked via progress_cb's
    return value) -- Cancel is disabled (cancelable=False) only once a
    real, non-cancelled result exists for the current source,
    immediately before that source is saved.
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
        apply_icon(self.win, "roadfrontage.ico")
        self.win.title(title)
        self.win.minsize(400, 120)
        self.win.resizable(False, False)
        self.status_var = StringVar(master=self.win)
        self.status_var.set("Starting...")
        ttk.Label(
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
        # Event Protocol v9), shared with lot_location.py via
        # progress_framework.py. Constructed after the widgets they
        # render into already exist. cancel_flag is passed straight
        # through -- progress_framework.py itself is unmodified; this
        # is simply the first time this file actually supplies one.
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
        self._view.destroy()


def clip_roads_to_parcels(road_gdf, parcel_gdf, pad=50):
    """
    Clip roads to parcel extent (+ buffer) to massively speed up union/buffer.
    pad is in CRS units (meters after reprojection).
    """
    minx, miny, maxx, maxy = parcel_gdf.total_bounds

    clip_box = box(
        minx - pad,
        miny - pad,
        maxx + pad,
        maxy + pad
    )

    return road_gdf[road_gdf.geometry.intersects(clip_box)]



# ========================================
# FRONTAGE PROCESSING
# ========================================
def process_frontage_single(brgy_gdf, road_gdf, source_name="", progress=None, classification=None,
                             is_cancelled=None,
                             road_frontage_col="CAMA_ROAD_FRONTAGE", depth_col="CAMA_DEPTH",
                             dwr_col="CAMA_DEPTH_WIDTH_RATIO"):
    """
    Core measurement engine: for each parcel, computes road frontage
    (via split_boundary_to_segments() + buffer-intersection against
    the road network), depth, and depth-to-width ratio, writing the
    three results into road_frontage_col/depth_col/dwr_col.

    Depth is computed inline (NOT via calculate_depth_perpendicular()
    or calculate_centroid_to_road_depth() -- neither is actually called
    anywhere in this file; see those functions' own docstrings/module
    notes). The live logic: if frontage_total > 0, take the longest
    covered frontage piece (_fl), find its midpoint, and derive a
    perpendicular direction from that piece's first-to-last-coordinate
    vector rotated 90 degrees. Two probe rays of length max_depth are
    cast from the midpoint in both directions along that perpendicular;
    whichever ray's intersection with the parcel polygon is longer is
    kept, and that intersection length is the depth value. If
    frontage_total is 0 (no frontage segment found), depth falls back
    to 0.0 directly in this inline block -- there is currently no
    centroid-to-road fallback measure wired into the live path. NONE of
    this measurement logic is touched by this task -- only the QA-only
    scaffolding that used to sit alongside it (see below) and the new
    Cancel checkpoints (see the "D-Cancel" paragraph below) are new.

    QA/VM removal (this task): the previously-computed buffer-diagnostic
    and Visual Measurement (frontage_lines) QA layers, and the
    emit_buffer_qa opt-in that gated one of them, have been removed
    entirely -- they were already fully disabled (writes commented out,
    checkbox removed from the GUI) before this task; this only finishes
    that removal by also deleting the now-pointless computation that fed
    them. This does NOT touch the real measurement math: frontage_total/
    depth_val/dwr_val (and the frontage_lengths/depths/dwrs lists they
    feed) were always computed independently of the QA-only
    frontage_lines_data/buffer_qa_records structures -- see the PASS 3
    comments below for exactly which locals were QA-only vs. load-bearing
    for the real output.

    D-Cancel (this task): progress, if given, is now called at three
    kinds of checkpoints -- Pass 1's and Pass 3's existing throttled
    per-parcel checkpoints, plus a new throttled checkpoint inside Pass
    2's cross-parcel conflict resolution loop (see that pass's own
    comments for the exact throttle) -- and its return value is checked
    at each one. Cancellation is COOPERATIVE, not instantaneous: only
    these explicit checkpoints are ever consulted; a single blocking
    geometry operation already in progress (reprojection, the road
    union, an individual Shapely call) always runs to completion. ONLY
    an explicit `False` return stops processing -- None, True, or no
    return at all (the shape every caller before this contract existed
    already produces) is treated as "continue," so an old-style caller
    that never returns anything keeps working exactly as before. On a
    Cancel at any of the three passes, everything computed so far for
    this source is discarded (no partial/padded result) and this
    function returns None instead of a dict.

    Args:
        brgy_gdf (geopandas.GeoDataFrame): parcels to process.
        road_gdf (geopandas.GeoDataFrame): road network layer.
        source_name (str): label used in progress/log messages only.
        progress (callable, optional): progress(message, value=None,
        maximum=None) -> bool | None. Called at coarse milestones
        (return value ignored) and, throttled, at each pass's own
        checkpoints (return value checked -- see the D-Cancel paragraph
        above).
        classification (dict, optional): per-parcel Lot Location/Lot
        Label classification values, if the optional Road
        Classification feature is active for this source (see the
        module docstring and the RUNTIME STATE section above).
        is_cancelled (callable, optional): zero-argument () -> bool,
        checked (throttled) ONLY inside Pass 2's inner candidate loop --
        a separate, lightweight mechanism from progress()'s own
        checkpoints, deliberately: it never touches the progress
        queue/UI (see Pass 2's own comments for why calling progress()
        once per candidate would be wasteful), it only exists so a
        single outer Pass-2 iteration with an unusually large STRtree
        candidate set can still notice a Cancel before the next %200
        outer checkpoint. None (the default) disables this check
        entirely -- Pass 2 then relies solely on its own outer
        checkpoint, exactly like Pass 1/3.
        road_frontage_col, depth_col, dwr_col (str): exact output
        column names to write into -- see the preserved comment below
        for why these default to the standard CAMA_-prefixed names but
        can be overridden per-source.

    Returns:
        On normal completion: {"parcels": brgy_gdf} -- a dict (kept as
        a dict, not a bare GeoDataFrame, to minimize the caller-side
        diff now that the qa_layers key is gone -- see run_processing()).
        On a Cancel at any of the three passes: None.
    """
    # road_frontage_col / depth_col / dwr_col: the exact column names to
    # write the three computed outputs into. Default to the standard
    # CAMA_-prefixed names -- cross-tool standard (see
    # OUTPUT_COLUMN_TARGETS' module-level docstring for the full
    # rationale): every column this tool CREATES gets a "CAMA_" prefix,
    # matching road_width.py's own CAMA_ROAD_WIDTH convention. Set by
    # run_processing() from parcel_output_column_overrides when an
    # existing, differently-cased column matching one of these NEW,
    # prefixed names was found and confirmed via the combined dialog in
    # on_run() -- e.g. a source with an existing "cama_DEPTH" column gets
    # depth_col="cama_DEPTH" here, so values are written back into that
    # exact existing column rather than creating a new, differently-cased
    # duplicate. This tool never searches for or auto-removes older,
    # non-prefixed columns (e.g. a plain "DEPTH" column left over from a
    # pre-CAMA_-prefix version of this tool) -- per the same principle
    # already established in road_width.py, only the NEW, prefixed name
    # is ever checked for conflicts.
    #
    # classification: dict produced by resolve_classification() -- see its
    # docstring for the exact shape. Defaults to "no gating at all"
    # (identical to this tool's original, pre-feature behavior) so any
    # existing caller that doesn't pass this argument keeps working exactly
    # as before.
    if classification is None:
        classification = {
            "mode": "no_gating",
            "skip_mask": None,
            "excluded_road_types": [],
            "lot_column": None,
            "lot_kind": None,
        }

    original_crs = brgy_gdf.crs
    zone_epsg, crs_warning = detect_prs92_zone([("Land Parcel", brgy_gdf), ("Road Network", road_gdf)])

    if crs_warning and progress:
        progress(f"Warning: {source_name}: {crs_warning}")

    if progress:
        progress(f"Reprojecting {source_name} to EPSG:{zone_epsg}")

    brgy_gdf = brgy_gdf.to_crs(epsg=zone_epsg)
    road_gdf = road_gdf.to_crs(epsg=zone_epsg)

    if progress:
        _mode = classification["mode"]
        if _mode == "lot_classification":
            progress(f"{source_name}: Using {classification['lot_column']} "
                      f"classification, skipping Inner Lots. All roads used.")
        elif _mode == "filter_by_road_type":
            progress(f"{source_name}: Filter by Road Type active.")
        else:
            progress(f"{source_name}: No classification/filter applied -- using all roads.")

    if progress:
        progress("Preparing roads (union)")

    # 🔧 Clean road geometries
    road_gdf = road_gdf.copy()
    road_gdf["geometry"] = road_gdf.geometry.apply(fix_geometry)
    road_gdf = road_gdf[road_gdf.geometry.notnull()]
    road_gdf = road_gdf[~road_gdf.geometry.is_empty]

    # ------------------------------------------------------------------
    # Optional, user-driven Road Type filter (Road Classification ->
    # "Filter by Road Type" mode only -- classification["excluded_road_types"]
    # is always [] for both Automatic modes, by construction in
    # resolve_classification(), so Automatic mode never reaches the
    # filtering branch below even if the checklist has stale unchecked
    # values from a previous "Filter by Road Type" session).
    #
    # Mirrors lot_location.py's process_lot_location() road-type filter --
    # column detection, .isin() exclusion, and the "all excluded -> fall
    # back to unfiltered" safety net -- so both tools behave identically
    # given the same road layer and the same excluded values.
    # ------------------------------------------------------------------
    excluded_road_types = classification.get("excluded_road_types") or []
    road_type_col = _detect_road_type_column(road_gdf)
    if road_type_col and excluded_road_types:
        original_count = len(road_gdf)
        filtered_gdf = road_gdf[~road_gdf[road_type_col].isin(excluded_road_types)].copy()
        if len(filtered_gdf) == 0:
            print(f"⚠️ [{source_name}] All road types excluded by filter -- "
                  f"falling back to full road layer.")
            if progress:
                progress(f"{source_name}: All road types excluded -- using unfiltered road layer.")
        else:
            road_gdf = filtered_gdf
            print(f"ℹ️ [{source_name}] Road type filter: {len(filtered_gdf)}/{original_count} "
                  f"roads retained after excluding {len(excluded_road_types)} type(s) "
                  f"(column: '{road_type_col}').")

    # ✂️ CLIP ROADS TO PARCEL EXTENT (CRITICAL)
    road_gdf = clip_roads_to_parcels(road_gdf, brgy_gdf, pad=50)

    if road_gdf.empty:
        raise RuntimeError("No road geometry near parcels after clipping")

    road_union = unary_union(road_gdf.geometry.values)

    frontage_lengths, depths, dwrs = [], [], []
    total = len(brgy_gdf)

    # ------------------------------------------------------------------
    # Inner-Lot skip mask (Automatic mode with a usable LOT_LOCATION/
    # LOT_LABEL column only -- see resolve_classification()). Reindexed
    # onto brgy_gdf's own index, then converted to a plain positional
    # boolean array so it can be checked by row position inside the
    # geometry loop below, the same way `geoms` is iterated. Rows flagged
    # True have their frontage measurement bypassed entirely but STAY in
    # the output -- they land in the same "no frontage" depth fallback
    # already used for any parcel with zero road-adjacent boundary (see
    # the `else` branch further below), just without spending time running
    # _edge_covered_portion() over their boundary first.
    # ------------------------------------------------------------------
    skip_mask = classification.get("skip_mask")
    if skip_mask is not None:
        skip_arr = skip_mask.reindex(brgy_gdf.index).fillna(False).to_numpy()
    else:
        skip_arr = None

    # ✅ iterate faster over geometry series
    geoms = brgy_gdf.geometry.values

    # ==================================================================
    # PASS 1: per-parcel raw covered-pieces computation. Uses the SAME,
    # UNMODIFIED per-segment logic as before (_edge_covered_portion(),
    # TWO_STAGE_FRONTAGE_ENABLED branch, linemerge welding) -- the only
    # change from the original single-pass loop is that this stops
    # short of finalizing frontage_total/threshold/depth, so Pass 2
    # (new, below) can adjust covered_pieces BEFORE any parcel's final
    # ROAD_FRONTAGE number is computed. Runs over every parcel in this
    # source, exactly like the original loop did.
    # ==================================================================
    _parcel_states = []  # one entry per row, same order as `geoms`
    cancelled_pass1 = False

    for i, geom_raw in enumerate(geoms, start=1):
        if progress and (i % 200 == 0 or i == 1 or i == total):
            # D-Cancel: explicit False only -- see this function's own
            # progress-contract docstring. None/True/no-return all mean
            # "continue," so an old-style caller keeps working unchanged.
            if progress(f"{source_name}: {i}/{total}", i, total) is False:
                cancelled_pass1 = True
                break

        geom = fix_geometry(geom_raw)
        if geom is None:
            _parcel_states.append({
                "geom": None, "covered_pieces": [],
                "resolved": (0.0, 0.0, 0.0),
            })
            continue

        # --- Road Classification: Inner Lot skip (Automatic mode only) ---
        # Bypasses the entire boundary/edge-adjacency measurement for rows
        # the classification source has already marked Inner Lot. Lands in
        # exactly the same depth fallback (centroid-to-road distance) that
        # a parcel with genuinely zero frontage already receives, just
        # without spending time running _edge_covered_portion() over its
        # boundary first. Marked "resolved" -- has no covered_pieces of
        # its own, so it does not participate in Pass 2's cross-parcel
        # comparison at all.
        if skip_arr is not None and skip_arr[i - 1]:
            try:
                depth_val = geom.centroid.distance(road_union)
            except Exception:
                depth_val = 0.0
            _parcel_states.append({
                "geom": geom, "covered_pieces": [],
                "resolved": (0.0, depth_val, depth_val),
            })
            continue

        boundary = geom.boundary

        # FRONTAGE: per-edge adjacency test, not a whole-road buffer.
        # Each elementary boundary segment (vertex-to-vertex) gets its own
        # confined buffer (flat-capped via _edge_covered_portion) —
        # a road detected near one segment cannot "bleed" onto a
        # perpendicular segment near a corner, which was a confirmed,
        # reproducible defect of the old whole-road-buffer approach.
        # ROAD_FRONT is the sum of the genuinely covered portion of every
        # segment; a segment with no nearby road contributes nothing.
        # Truncation matches the road's true extent (flat cap), and
        # segments genuinely dangling past where the road ends are simply
        # not covered — no artificial full-edge extension.
        #
        # Internal roads (running through the parcel interior, not near
        # any boundary edge) are explicitly excluded per business
        # decision — frontage is boundary-only, never interior.
        try:
            segments = split_boundary_to_segments(boundary)
            covered_pieces = []

            if TWO_STAGE_FRONTAGE_ENABLED:
                # Stage 1 (gate): strict/narrow tolerance. If NOT ONE
                # segment gets any coverage here, this parcel is rejected
                # outright -- covered_pieces stays empty, frontage_total
                # ends up 0.0 in Pass 3, Stage 2 never runs for this parcel.
                gate_hit = False
                for seg in segments:
                    if _edge_covered_pieces(seg, road_union, tol=TWO_STAGE_GATE_TOLERANCE):
                        gate_hit = True
                        break

                if gate_hit:
                    # Stage 2 (measure): the WHOLE boundary is re-measured
                    # at the wider tolerance -- this result becomes the
                    # final frontage_total directly. It is NOT summed or
                    # combined with anything from Stage 1 -- Stage 1 is
                    # purely a pass/fail gate, never a measurement.
                    for _seg_idx, seg in enumerate(segments):
                        pieces = _edge_covered_pieces(seg, road_union, tol=TWO_STAGE_MEASURE_TOLERANCE)
                        if pieces:
                            covered_pieces.extend(pieces)
                # else: gate rejected this parcel -- covered_pieces stays
                # empty, frontage_total will be 0.0 in Pass 3.
            else:
                for _seg_idx, seg in enumerate(segments):
                    pieces = _edge_covered_pieces(seg, road_union, tol=FRONTAGE_BUFFER_TOLERANCE)
                    if pieces:
                        covered_pieces.extend(pieces)
            # Weld consecutive covered pieces back into continuous lines.
            # _edge_covered_pieces() can already return MULTIPLE pieces per
            # elementary (vertex-to-vertex) segment (e.g. two separate
            # parallel-validated runs with a rejected crossing piece
            # between them), and a long run of adjacent covered segments
            # would otherwise stay fragmented into many tiny pieces instead of
            # one continuous edge — confirmed reproducible via a jagged
            # boundary test. linemerge() only welds pieces that genuinely
            # share an endpoint; two disjoint edges (e.g. both sides of a
            # corner lot) or a truncated piece that stops short of a
            # vertex are correctly left separate. This welded granularity
            # -- one entry per continuous frontage stretch, not one entry
            # per raw elementary segment -- is also the granularity Pass 2
            # (below) resolves cross-parcel conflicts at, so a parcel's
            # other, non-conflicting side is never affected by a conflict
            # on just one of its sides.
            if covered_pieces:
                merged = linemerge(covered_pieces)
                if merged.geom_type == "LineString":
                    covered_pieces = [merged]
                elif merged.geom_type == "MultiLineString":
                    covered_pieces = list(merged.geoms)

            _parcel_states.append({
                "geom": geom, "covered_pieces": covered_pieces,
                "resolved": None,
            })
        except Exception:
            # TEMPORARY DIAGNOSTIC -- remove after capturing frontage_error.log.
            # Logs full traceback beside the exe so the actual exception type
            # and failing geometry coordinates can be identified before any
            # permanent exception-handling change is made.
            import traceback as _tb
            try:
                _log = os.path.join(os.path.dirname(sys.executable), "frontage_error.log")
                with open(_log, "a", encoding="utf-8") as _f:
                    _f.write("[FRONTAGE BLOCK]\n")
                    _f.write(_tb.format_exc())
                    _f.write("\n---\n")
            except Exception:
                pass
            _parcel_states.append({
                "geom": geom, "covered_pieces": [],
                "resolved": None,
            })

    # D-Cancel: Pass 1 was stopped early at one of its own throttled
    # checkpoints above -- full discard, per explicit decision. Pass 2
    # and Pass 3 never run; _parcel_states so far (necessarily partial --
    # it only covers parcels processed before the checkpoint that
    # triggered) is thrown away rather than padded/truncated into a
    # partial result. There is no use for a half-computed output.
    if cancelled_pass1:
        return None

    # ==================================================================
    # PASS 2 (NEW): CROSS-PARCEL CONFLICT RESOLUTION.
    # See CROSS_PARCEL_CONFLICT_RESOLUTION_ENABLED's module-level
    # docstring above for the full rationale and the real production
    # case that surfaced this. Runs across EVERY parcel in this source
    # -- not limited to any specific PIN -- via a spatial index so it
    # stays tractable for a full, large batch.
    # ==================================================================
    if CROSS_PARCEL_CONFLICT_RESOLUTION_ENABLED:
        all_global_pieces = []  # (parcel_row_idx, piece_geom, piece_length)
        # parcel_prelim_total: each parcel's OWN sum of all its Pass-1
        # pieces, computed BEFORE any conflict resolution. This -- not
        # each individual piece's own isolated length -- is what conflict
        # winners are decided by. A real bug caught via direct testing
        # against production data motivated this: a large, naturally
        # fragmented parcel (one long frontage broken into many separate
        # linemerge()'d pieces by gaps in road coverage) can have
        # individual fragments shorter than a legitimate small neighbor's
        # single whole piece -- comparing PIECE lengths in isolation let
        # one such fragment "win" a conflict purely because of how that
        # large parcel's own frontage happened to fragment, not because
        # it had any genuine claim to that neighbor's road stretch. The
        # project lead's own rule was framed in terms of comparing
        # PARCELS ("11.93 vs 621.04"), not comparing arbitrary fragments
        # of them -- this matches that framing exactly.
        parcel_prelim_total = {}
        for parcel_row_idx, state in enumerate(_parcel_states):
            if state["resolved"] is not None:
                continue
            prelim_total = sum(p.length for p in state["covered_pieces"])
            parcel_prelim_total[parcel_row_idx] = prelim_total
            for piece in state["covered_pieces"]:
                all_global_pieces.append((parcel_row_idx, piece, piece.length))

        if progress:
            progress(f"{source_name}: resolving cross-parcel conflicts "
                      f"({len(all_global_pieces)} candidate pieces)...")

        cancelled_pass2 = False
        if all_global_pieces:
            # Smallest-parcel-total-survives greedy resolution: process
            # pieces ordered by their OWN PARCEL's preliminary total
            # (ascending), so a piece belonging to a parcel with a small,
            # independently-plausible overall frontage is never
            # accidentally discarded in favor of a piece belonging to a
            # parcel with a much larger, likely-wrong overall claim --
            # regardless of how either parcel's own total happens to be
            # fragmented into individual pieces. Ties (equal parcel
            # totals) keep whichever is encountered first in this sorted
            # order -- an edge case not expected to occur meaningfully
            # often in real data.
            all_global_pieces.sort(key=lambda x: parcel_prelim_total[x[0]])
            buffered_geoms = [
                p[1].buffer(CROSS_PARCEL_CONFLICT_TOLERANCE) for p in all_global_pieces
            ]
            tree = STRtree(buffered_geoms)
            disregarded = set()
            n_pieces = len(all_global_pieces)

            # D-Cancel: TWO SEPARATE checkpoint mechanisms in this loop,
            # deliberately not merged into one:
            #   - the OUTER %200 checkpoint below calls progress() (same
            #     throttle/contract as Pass 1/3) -- this is the normal
            #     UI-progress-reporting path, and its return value is
            #     what actually stops the loop.
            #   - the INNER candidate-loop check calls is_cancelled()
            #     directly -- a cheap, non-queue-writing lookup (see this
            #     function's own Args docstring) -- purely so a single
            #     outer iteration with an unusually large STRtree
            #     candidate set (a dense cluster of overlapping pieces)
            #     can't make Cancel appear unresponsive until the NEXT
            #     outer checkpoint. It never calls progress() and never
            #     posts a UI message -- calling the full progress()
            #     callback once per candidate could flood the queue with
            #     duplicate messages for no benefit. Throttled by
            #     _inner_checked (a running count of inner-loop
            #     iterations across the WHOLE Pass 2, not reset per
            #     outer idx) so the lookup itself stays cheap even when
            #     candidate sets are individually small.
            _inner_checked = 0
            INNER_CANCEL_CHECK_EVERY = 2000

            for idx in range(n_pieces):
                if progress and (idx % 200 == 0 or idx == 0 or idx == n_pieces - 1):
                    if progress(f"{source_name}: resolving cross-parcel conflicts "
                                f"({idx}/{n_pieces})", idx, n_pieces) is False:
                        cancelled_pass2 = True
                        break
                if idx in disregarded:
                    continue
                parcel_idx_i, piece_i, len_i = all_global_pieces[idx]
                total_i = parcel_prelim_total[parcel_idx_i]
                buf_i = buffered_geoms[idx]
                candidate_idxs = tree.query(buf_i)
                for cand_idx in candidate_idxs:
                    _inner_checked += 1
                    if (is_cancelled is not None
                            and _inner_checked % INNER_CANCEL_CHECK_EVERY == 0
                            and is_cancelled()):
                        cancelled_pass2 = True
                        break
                    cand_idx = int(cand_idx)
                    if cand_idx == idx or cand_idx in disregarded:
                        continue
                    parcel_idx_j, piece_j, len_j = all_global_pieces[cand_idx]
                    if parcel_idx_j == parcel_idx_i:
                        # Same parcel's own two pieces (e.g. both sides of
                        # a corner lot) -- never a conflict with itself.
                        continue
                    total_j = parcel_prelim_total[parcel_idx_j]
                    if total_j <= total_i:
                        # Candidate's PARCEL has a smaller-or-equal overall
                        # total -- it survives on its own turn (or already
                        # has); do not disregard it from here.
                        continue
                    if buf_i.intersects(buffered_geoms[cand_idx]):
                        disregarded.add(cand_idx)
                if cancelled_pass2:
                    break

            if cancelled_pass2:
                # D-Cancel: full discard, per explicit decision -- same as
                # Pass 1. disregarded/per_parcel_kept are necessarily
                # incomplete at this point (the conflict-resolution pass
                # never finished), so nothing from Pass 2 is applied to
                # _parcel_states, and Pass 3 never runs.
                return None

            # Rebuild each parcel's covered_pieces list, excluding

            # disregarded ones -- Pass 3 (below) sums frontage_total from
            # this adjusted list, not the original Pass-1 list.
            per_parcel_kept = {}
            for idx, (parcel_row_idx, piece, length) in enumerate(all_global_pieces):
                if idx in disregarded:
                    continue
                per_parcel_kept.setdefault(parcel_row_idx, []).append(piece)

            for parcel_row_idx, state in enumerate(_parcel_states):
                if state["resolved"] is not None:
                    continue
                state["covered_pieces"] = per_parcel_kept.get(parcel_row_idx, [])

    # ==================================================================
    # PASS 3: finalize frontage_total, threshold, depth, QA per parcel.
    # Identical logic to the original single-pass loop's second half --
    # only the SOURCE of covered_pieces has changed (Pass 2's possibly-
    # reduced list, instead of computing it fresh inline).
    # ==================================================================
    cancelled_pass3 = False
    for i, state in enumerate(_parcel_states, start=1):
        if progress and (i % 200 == 0 or i == 1 or i == total):
            # D-Cancel: explicit False only, same contract as Pass 1.
            # Cancellation here must occur BEFORE frontage_lengths/
            # depths/dwrs are treated as a completed result -- see the
            # discard check immediately after this loop, before those
            # lists are ever assigned onto brgy_gdf.
            if progress(f"{source_name}: {i}/{total}", i, total) is False:
                cancelled_pass3 = True
                break

        if state["resolved"] is not None:
            frontage_total, depth_val, dwr_val = state["resolved"]
            frontage_lengths.append(frontage_total)
            depths.append(depth_val)
            dwrs.append(dwr_val)
            continue

        geom = state["geom"]
        covered_pieces = state["covered_pieces"]

        frontage_total = sum(p.length for p in covered_pieces)
        _fl = max(covered_pieces, key=lambda p: p.length) if covered_pieces else None

        # ------------------------------------------------------------------
        # MINIMUM FRONTAGE THRESHOLD (separate task from Road Classification,
        # see MIN_FRONTAGE_THRESHOLD's module-level docstring above for the
        # full business/technical justification). Applied here, AFTER the
        # existing frontage-detection algorithm has already run and produced
        # its result -- this does not modify _edge_covered_portion(),
        # linemerge(), or any other part of the existing measurement logic;
        # it only decides whether a small, already-computed result counts as
        # frontage at all. A sub-threshold result is treated exactly like a
        # parcel with genuinely zero frontage: it falls through to the same
        # centroid-to-road depth fallback below, and produces no QA frontage
        # line (matching the existing "ROAD_FRONT = 0 -> no QA feature" rule).
        # ------------------------------------------------------------------
        if 0 < frontage_total <= MIN_FRONTAGE_THRESHOLD:
            frontage_total = 0.0
            _fl = None

        frontage_lengths.append(frontage_total)

        if frontage_total > 0:
            # Use the longest frontage piece to define the perpendicular direction.
            frontage_ls = _fl  # already resolved above
            if frontage_ls is None or frontage_ls.length == 0:
                depth_val = 0.0
            else:
                midpoint = frontage_ls.interpolate(0.5, normalized=True)
                coords = list(frontage_ls.coords)
                x1, y1 = coords[0]
                x2, y2 = coords[-1]
                dx = x2 - x1
                dy = y2 - y1

                perp_dx, perp_dy = -dy, dx
                length = math.hypot(perp_dx, perp_dy)
                if length == 0:
                    depth_val = 0.0
                else:
                    perp_dx /= length
                    perp_dy /= length
                    max_depth = 1000

                    line1 = LineString([midpoint, (midpoint.x + perp_dx * max_depth, midpoint.y + perp_dy * max_depth)])
                    line2 = LineString([midpoint, (midpoint.x - perp_dx * max_depth, midpoint.y - perp_dy * max_depth)])

                    # Choose the ray that intersects deeper into the parcel.
                    try:
                        i1 = line1.intersection(geom)
                        i2 = line2.intersection(geom)
                        len1 = i1.length if not i1.is_empty else 0.0
                        len2 = i2.length if not i2.is_empty else 0.0
                        depth_val = max(len1, len2)
                    except Exception:
                        # TEMPORARY DIAGNOSTIC -- remove after capturing frontage_error.log.
                        import traceback as _tb
                        try:
                            _log = os.path.join(os.path.dirname(sys.executable), "frontage_error.log")
                            with open(_log, "a", encoding="utf-8") as _f:
                                _f.write("[DEPTH BLOCK]\n")
                                _f.write(_tb.format_exc())
                                _f.write("\n---\n")
                        except Exception:
                            pass
                        depth_val = 0.0

            dwr_val = round(depth_val / frontage_total, 2) if frontage_total else 0.0
        else:
            # Inner lot fallback: no road frontage — store centroid-to-road
            # distance as depth so the attribute is still meaningful.
            try:
                depth_val = geom.centroid.distance(road_union)
            except Exception:
                depth_val = 0.0
            dwr_val = depth_val

        depths.append(depth_val)
        dwrs.append(dwr_val)

    # D-Cancel: Pass 3 was stopped early at one of its own throttled
    # checkpoints above -- full discard, per explicit decision, same as
    # Pass 1/2. This check runs BEFORE frontage_lengths/depths/dwrs are
    # ever assigned onto brgy_gdf below, so a cancelled run never
    # produces a partially-populated output column.
    if cancelled_pass3:
        return None

    # 🔒 Safety check — prevents silent column mismatch on partial failures.
    if not (len(frontage_lengths) == len(brgy_gdf) == len(depths) == len(dwrs)):
        raise RuntimeError("Attribute length mismatch during frontage processing")

    brgy_gdf[road_frontage_col] = frontage_lengths
    brgy_gdf[depth_col] = depths
    brgy_gdf[dwr_col] = dwrs

    if original_crs:
        brgy_gdf = brgy_gdf.to_crs(original_crs)

    if progress:
        progress(f"Finished {source_name}", total, total)

    # Return shape (see this function's own docstring): normal
    # completion always returns this one-key dict -- kept as a dict
    # rather than a bare GeoDataFrame to minimize the caller-side diff
    # now that the qa_layers key is gone (see run_processing()). A
    # Cancel at any of the three passes above returns None instead,
    # well before this point is ever reached.
    return {"parcels": brgy_gdf}


# ========================================
# DB ATOMIC WRITE / CANCEL-SAFE STAGING  (D-Cancel)
# ========================================
# Ported from lot_location.py's own D4 section (itself ported from
# landmarks_within_meters.py, the pilot for this exact mechanism --
# staging-table-then-atomic-rename-swap, plus a genuine Cancel control,
# plus crash/orphan recovery on a later run). This is now the THIRD
# tool with this mechanism (landmarks_within_meters.py, lot_location.py,
# and this file); per Section C/G.5 (Rule of Three) this is still a
# deliberate duplication, not a shared utils/ module -- that decision is
# for a future task, not this one. Every function below is copied from
# lot_location.py's D4 block with only the adaptations this file's own
# context requires: "roadfrontage.ico" for the orphan-recovery dialog,
# and "geometry" as the default geometry column name (confirmed to
# match this file's own convention -- read_postgis_clean() always
# renames the geometry column to "geometry", and no to_postgis() call
# in this file ever renames it away from that) -- no other design
# changes.
#
# Replaces the single `brgy_gdf.to_postgis(..., if_exists="replace")`
# call this file's worker() previously used with a staging-write /
# verify / atomic-rename-swap sequence. The real destination table is
# never touched until FINAL_SWAP, a single PostgreSQL transaction that
# either fully commits or is fully rolled back by PostgreSQL itself.
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
# THIS FILE'S CANCEL GRANULARITY: process_frontage_single() has its own
# bool-return progress contract (see that function's own docstring),
# checked at Pass 1's, Pass 2's, and Pass 3's own throttled checkpoints
# -- explicit False only, full discard (returns None) on trigger. What's
# genuinely still true, and matches lot_location.py/landmarks_within_
# meters.py exactly: NOTHING from STAGING_WRITE through FINAL_SWAP and
# the post-swap backup drop -- everything below this comment block -- is
# itself cancelable, for the same reason in all three tools:
# gdf.to_postgis() (and the DB-only steps that follow it) is a single
# blocking library call with no hook a background thread could check a
# flag inside of, not a stricter design choice specific to this file.
# See run_processing()'s worker() for the actual Cancel checkpoint
# sequence (pre-processing check, then process_frontage_single()'s own
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
    apply_icon(win, "roadfrontage.ico")
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

    Two cases:
      - DB-source Land Parcel (barangay_source[0] == "db"): always
        writes back to the exact same table it was read from -- no
        matching, no dialog, matches worker()'s own pre-existing
        src_type handling (out_base = name).
      - Local-file Land Parcel: fuzzy-matches the filename against
        existing tables via find_matching_tables() (which already
        excludes CAMA_Table, CAMA_Transaction_Log, and any "_VM"
        table -- that exclusion is shared, out-of-scope utils code for
        this task and is left untouched), then requires user
        confirmation before treating a match as an overwrite target --
        zero candidates skips the dialog entirely and creates a new
        table under the filename.

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
# MAIN PROCESS
# ========================================
def run_processing(app_root, resolved_table_name=None, resolved_outcome=None):
    """
    Orchestrates the full run on a background thread with progress
    reported via a queue.Queue: loads the Road Network layer once,
    then for each selected Land Parcel source runs
    process_frontage_single() (with the module-level overwrite_mode
    and parcel_output_column_overrides settings already resolved by
    on_run() before this was called) and saves the result either
    locally (.gpkg, optionally opened in Global Mapper) or to PostGIS.
    The previously-optional QA layers (frontage_lines/VM,
    segment_buffers) have been removed entirely -- a successful run now
    always produces exactly one output artifact per source.

    D-Cancel (this task): one new_cancel_flag() is created per run and
    passed to ProgressWindow, wiring the title-bar X as Cancel for the
    lifetime of this run's progress window. Cancel is checked at two
    kinds of points inside worker(): once before process_frontage_
    single() is even called (catches Cancel during "Loading road
    data..."/"Loading {name}"), and then internally, by process_
    frontage_single() itself, at its own three passes' throttled
    checkpoints -- see that function's own progress-contract docstring
    for the exact contract and for why a Cancel at any pass returns
    None (full discard) rather than a partial result. Cancel is
    disabled only once a real (non-cancelled) result exists,
    immediately before the save that follows -- nothing from that
    point on can itself be interrupted. See ProgressWindow's own
    docstring and the "DB ATOMIC WRITE / CANCEL-SAFE STAGING" section
    above for the full rationale. barangay_source is always a 1+-tuple
    processed one source at a time by the existing `sources` loop below
    (unchanged) -- Cancel applies per-run, not per-source: a Cancel
    while processing one source in a multi-source batch discards that
    source's own in-progress work and stops the whole batch, exactly
    like an unhandled exception already would, rather than skipping
    just that one source and continuing (that per-source-failure
    isolation below is reserved for actual exceptions).

    Args:
        app_root: the live top-level window, used as the parent for
        the ProgressWindow and any dialogs.
        resolved_table_name (str | None): the already-confirmed DB
        output table name from resolve_db_output_table() in on_run() --
        only relevant for DB output mode.
        resolved_outcome (str | None): "overwritten" or "created", the
        other half of resolve_db_output_table()'s return value (via
        on_run()) -- only relevant for DB output mode. Passed straight
        through to _write_db_output_safely() so it knows whether
        FINAL_SWAP needs a rename-existing-aside-to-backup step.
        Previously discarded by on_run() (the call site kept only
        resolved_table_name); now actually used -- see on_run().
    """
    global barangay_source, road_source, output_mode, overwrite_mode, parcel_output_column_overrides
    if not barangay_source or not road_source or not output_mode:
        messagebox.showerror("Error", "Selections incomplete (Barangay, Road, Output required).")
        return

    creds = load_db_credentials()
    if not creds:
        messagebox.showerror("Error", "Missing pg_credentials.json")
        return

    schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    )

    # resolved_table_name: the DB-output destination table. Resolution
    # responsibility now belongs to on_run() (PRIORITY 3), on the main
    # thread, BEFORE win.destroy() -- see Fix 1. Passed in as a parameter
    # -- same approach already used in lot_location.py, road_surface.py,
    # road_density.py, and land_shape_compactness.py. By the time it
    # reaches this function it is treated as an already-validated value:
    # either None (local output, or output_mode[0] != "db") or a
    # confirmed table name (DB output, user already had the chance to
    # cancel in on_run()). No re-resolution or re-validation happens here.

    cancel_flag = new_cancel_flag()
    progress = ProgressWindow(app_root, "Road Frontage Progress", cancel_flag=cancel_flag)

    q = queue.Queue()

    def worker():
        """
        Background-thread body -- see run_processing()'s own docstring
        for the full D-Cancel checkpoint sequence. Never touches
        Tkinter widgets directly; only puts messages on q for
        poll_queue() (main thread) to consume.
        """
        lock_conn = None
        try:
            def progress_cb(msg, val=None, maxv=None):
                q.put(("update", msg, val, maxv, None))
                # D-Cancel: a real bool every call, never None --
                # process_frontage_single()'s three passes only stop on
                # an explicit False (see that function's own
                # progress-contract docstring), so this must never
                # accidentally return None itself.
                return not cancel_flag["stop"]

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

            q.put(("update", "Loading road data...", None, None, None))

            # Reuse the road layer already read by the Road Classification
            # section's background read (see open_main_window()) when it
            # matches the currently selected road source -- avoids a
            # second full file/DB read of the same data. Looks up the
            # dual-slot cache's slot for whichever mode (local/db) was
            # actually run, since the two are cached independently.
            road_slot = _road_gdf_cache.get(road_source[0], {})
            if road_slot.get("key") == road_source[1][0] and road_slot.get("gdf") is not None:
                # Already forced to 2D at the cache-write site (see
                # _poll_road_classification_queue()'s _road_gdf_cache[...]
                # assignment); wrapped again here as a cheap no-op
                # safeguard so this reuse path never depends on that
                # being the only place the cache is ever written.
                road_gdf = _force_2d(road_slot["gdf"])
                print("ℹ️ Reusing cached road network (already read during source selection).")
            elif road_source[0] == "local":
                road_gdf = _force_2d(gpd.read_file(road_source[1][0]))
            else:
                road_table = road_source[1][0]
                road_gdf = _force_2d(read_postgis_clean(road_table, engine, schema))

            if barangay_source[0] == "local":
                sources = [("local", p) for p in barangay_source[1]]
            else:
                sources = [("db", t) for t in barangay_source[1]]

            skipped = []
            cancelled = False
            for src_type, src in sources:
                try:
                    if src_type == "local":
                        name = os.path.basename(src)
                        q.put(("update", f"Loading {name}", None, None, None))
                        brgy_gdf = _force_2d(gpd.read_file(src))
                        out_base = os.path.splitext(name)[0]
                    else:
                        name = src
                        q.put(("update", f"Loading DB table {name}", None, None, None))
                        brgy_gdf = _force_2d(read_postgis_clean(name, engine, schema))
                        out_base = name

                    # D-Cancel: pre-processing checkpoint. Catches a
                    # Cancel clicked during "Loading road data..."/
                    # "Loading {name}" -- i.e. before process_frontage_
                    # single() is even called, and before that
                    # function's own first Pass-1 checkpoint (i == 1,
                    # reached quickly but not instantly after
                    # reprojection/road-union/road-type filtering) could
                    # fire on its own. NOT the only checkpoint in this
                    # run -- see the result-is-None check right after
                    # the process_frontage_single() call below, and that
                    # function's own progress-contract docstring.
                    if cancel_flag["stop"]:
                        cancelled = True
                        break

                    # Road Classification: resolved independently for THIS
                    # parcel source. use_classification_for_source comes
                    # from the per-source checkbox the user checked (or
                    # didn't) for exactly this file/table in the GUI --
                    # mixed batches (some sources checked, some not, or
                    # some lacking a usable column entirely) are
                    # intentionally supported.
                    use_classification_for_source = parcel_classification_selection.get(src, False)
                    classification = resolve_classification(
                        brgy_gdf, use_classification_for_source, filter_by_road_type_active,
                        road_type_excluded_values
                    )

                    # Priority 2: preserves each source's existing output
                    # column name(s)/casing exactly, if a conflict was
                    # detected and confirmed in on_run() -- e.g. a
                    # detected "cama_DEPTH" is written back to
                    # "cama_DEPTH", not a hardcoded "CAMA_DEPTH". Defaults
                    # to the standard CAMA_-prefixed name for any output
                    # this source has no override for.
                    output_col_overrides = parcel_output_column_overrides.get(src, {})
                    road_frontage_col = output_col_overrides.get("CAMA_ROAD_FRONTAGE", "CAMA_ROAD_FRONTAGE")
                    depth_col = output_col_overrides.get("CAMA_DEPTH", "CAMA_DEPTH")
                    dwr_col = output_col_overrides.get("CAMA_DEPTH_WIDTH_RATIO", "CAMA_DEPTH_WIDTH_RATIO")

                    # D-Cancel: Cancel STAYS enabled through
                    # process_frontage_single() itself -- it honors
                    # Cancel internally, via progress_cb's return value,
                    # at its own three passes' throttled checkpoints
                    # (plus, in Pass 2's inner loop only, the separate
                    # lightweight is_cancelled() check -- see that
                    # function's own docstring).
                    q.put(("update", f"Processing {name}", None, None, True))

                    result = process_frontage_single(
                        brgy_gdf,
                        road_gdf,
                        name,
                        progress=progress_cb,
                        classification=classification,
                        is_cancelled=lambda: cancel_flag["stop"],
                        road_frontage_col=road_frontage_col,
                        depth_col=depth_col,
                        dwr_col=dwr_col
                    )

                    if result is None:
                        # D-Cancel: progress_cb returned False at one of
                        # process_frontage_single()'s own pass
                        # checkpoints (or its inner is_cancelled() check
                        # fired) -- full discard, per explicit decision.
                        # Nothing computed for this source so far is
                        # kept; output_mode[1]/the DB is never touched
                        # (the save step below is never reached).
                        # Same-tick "flash" of this message before the
                        # terminal "cancelled" message/dialog, per
                        # explicit decision -- no artificial delay.
                        q.put(("update", "Discarding run... Please wait.", None, None, False))
                        cancelled = True
                        break

                    # D-Cancel: from here through the save, Cancel is
                    # disabled -- a real result now exists and nothing
                    # past this point can be safely interrupted (the
                    # save itself is non-cancelable by design -- see
                    # _write_db_output_safely()).
                    q.put(("update", f"Saving {name}", None, None, False))

                    brgy_gdf = result["parcels"]

                    if output_mode[0] == "local":
                        # Priority 1: filename resolution happens ONCE
                        # here, for the main output only -- overwrite_mode
                        # was already decided ONCE, up front, for the
                        # whole batch (see the pre-scan + ask_overwrite_dialog()
                        # in on_run()) -- no per-file prompt here.
                        #
                        # Naming convention: the main output reuses the
                        # parcel source's own name directly, no tool-name
                        # suffix (matches road_width.py's convention
                        # exactly). This tool writes exactly this ONE
                        # output file per source -- the previously-paired
                        # QA layers have been removed entirely (see the
                        # module docstring and process_frontage_single()'s
                        # own docstring).
                        desired_base_name = out_base
                        candidate_path = os.path.join(output_mode[1], f"{desired_base_name}.gpkg")
                        had_conflict = os.path.exists(candidate_path)
                        if had_conflict and overwrite_mode == "new":
                            base_name = resolve_output_base_name(output_mode[1], desired_base_name)
                        else:
                            # Either no conflict, or the user chose
                            # "Overwrite" -- both cases use the plain
                            # desired name (overwriting in place).
                            base_name = desired_base_name

                        out = os.path.join(output_mode[1], f"{base_name}.gpkg")
                        _write_gpkg(brgy_gdf, out)
                        q.put(("open_gm", out, None, None))
                    else:
                        # The actual destination table was already
                        # decided by resolve_db_output_table(), BEFORE
                        # this function (and the worker thread) even
                        # started -- fuzzy matching + user confirmation
                        # already happened there (see that function's
                        # docstring). This just uses the result. Falls
                        # back to out_base only if resolved_table_name
                        # is somehow None here (output_mode[0] != "db"
                        # can't reach this branch, so this is just a
                        # defensive fallback).
                        db_table = resolved_table_name if resolved_table_name is not None else out_base
                        # D-Cancel: staging-write / verify / atomic
                        # rename-swap, replacing the previous direct
                        # to_postgis(..., if_exists="replace") call --
                        # see "DB ATOMIC WRITE / CANCEL-SAFE STAGING"
                        # above.
                        _write_db_output_safely(engine, schema, brgy_gdf,
                                                 db_table, resolved_outcome,
                                                 run_id, run_started_at)

                except Exception as source_err:
                    # Isolate failures per source: log/report and move on to
                    # the next source instead of aborting the entire batch.
                    # Sources already written before this one keep their
                    # output — only this one is skipped.
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
                summary = "Processing done!"
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
        try:
            while True:
                msg = q.get_nowait()
                kind, *rest = msg

                if kind == "update":
                    progress.update(rest[0], rest[1], rest[2], rest[3])

                elif kind == "open_gm":
                    open_in_global_mapper(rest[0])

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
    apply_icon(picker, "roadfrontage.ico")
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
# OVERWRITE DIALOGS
# ========================================
def ask_overwrite_dialog(parent, conflicting_names):
    """
    Combined dialog shown ONCE, before any processing starts, when one or
    more Land Parcel sources' desired local output filename already
    exists in the chosen output folder. Not a per-file prompt -- every
    conflicting name in the batch is listed together, and the chosen
    action applies to ALL of them:

      - "Overwrite": every conflicting file is replaced in place, using
        its plain desired name (no numbering).
      - "Create New File": every conflicting file is instead saved under
        a new, non-colliding name via resolve_output_base_name()'s
        auto-numbering -- the existing files are left untouched.
      - "Cancel": aborts the ENTIRE run. Nothing is written, including
        sources that had no conflict at all.

    If the user wants a MIXED outcome (overwrite some, rename others),
    the expected workflow is to run the tool twice -- once selecting
    only the sources to overwrite, once for the rest -- rather than
    choosing per-file in a single dialog.

    Returns "overwrite", "new", or "cancel" (also returned if the
    dialog's own titlebar close button is used, treated the same as an
    explicit Cancel -- never silently defaults to a destructive choice).

    Ported directly from road_width.py's validated implementation.
    Deliberately does NOT call dialog.transient(parent): this app's root
    is permanently withdrawn (see main()), and transient() on a withdrawn
    parent is a known source of window-manager-dependent "dialog never
    becomes viewable" behavior -- confirmed in road_width.py's own
    testing. grab_set()+deiconify()+lift()+focus_force()+topmost is used
    instead, matching this file's own existing dialog pattern elsewhere
    (see _pick_db_tables()).
    """
    result = {"choice": "cancel"}

    dialog = tk.Toplevel(parent)
    apply_icon(dialog, "roadfrontage.ico")
    dialog.title("File(s) Already Exist")
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

    # Scrollable BOTH ways -- vertical for many conflicting names,
    # horizontal for long filenames -- wrap="none" so long names stay on
    # one line and scroll into view rather than wrapping awkwardly.
    # Scrollbars are only shown when actually needed.
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
    # Centered on the SCREEN, not on `parent` -- `parent` here is
    # MAIN.py's _tool_root, a deliberately invisible 1x1 anchor window
    # placed off-screen at (-9999, -9999) purely for Tk plumbing (icon
    # binding, mainloop hosting). Centering against it produced a wildly
    # negative x/y that the old max(x,0)/max(y,0) clamp collapsed to
    # exactly (0, 0) every time -- pinning the dialog to the screen's
    # top-left corner regardless of where any real window (the CAMA
    # Tools panel, Global Mapper, etc.) actually was. Screen dimensions
    # are a stable, always-meaningful reference this dialog can center
    # against instead. Button layout (packed side="bottom" above) is
    # unaffected -- this only changes where the whole window is placed,
    # not how its own contents are arranged inside it.
    screen_w = dialog.winfo_screenwidth()
    screen_h = dialog.winfo_screenheight()
    x = (screen_w - req_w) // 2
    y = (screen_h - req_h) // 2
    dialog.geometry(f"{req_w}x{req_h}+{max(x,0)}+{max(y,0)}")

    # deiconify/lift/focus_force/topmost are called LAST -- after content
    # and geometry() -- see confirm_db_overwrite_dialog()'s matching
    # comment for the full rationale (repositioning a window can perturb
    # its stacking order against another always-on-top window from a
    # separate process on some Windows builds). Topmost is deliberately
    # never reset back to False afterward -- this dialog can stay open
    # indefinitely waiting on the user's answer, and grab_set() alone
    # cannot protect it from being covered by a separate process's window.
    dialog.deiconify()
    dialog.lift()
    dialog.focus_force()
    dialog.attributes("-topmost", True)

    # A single lift()/topmost assertion at creation time was confirmed
    # (in testing) to still be insufficient: the CAMA Tools floating
    # panel is ITSELF persistently topmost (a separate process, always
    # floating above the map), so whenever it regains z-order priority
    # over this dialog -- e.g. after the user interacts with the map --
    # a one-time lift() at dialog-creation time doesn't help, since that
    # moment has already passed. This keeps re-asserting lift()+topmost
    # every 250ms for as long as the dialog exists, so it keeps winning
    # that z-order fight for its entire (indefinite, user-controlled)
    # lifetime rather than only at the instant it first appeared.
    # Self-cancels via the winfo_exists() guard once dialog.destroy()
    # runs in choose() above -- no dangling after() callbacks survive
    # the dialog closing.
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
    apply_icon(dialog, "roadfrontage.ico")
    dialog.title("ROAD FRONTAGE TOOL")
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
    # Centered on the SCREEN, not on `parent` -- `parent` here is
    # MAIN.py's _tool_root, a deliberately invisible 1x1 anchor window
    # placed off-screen at (-9999, -9999) purely for Tk plumbing (icon
    # binding, mainloop hosting). Centering against it produced a wildly
    # negative x/y that the old max(x,0)/max(y,0) clamp collapsed to
    # exactly (0, 0) every time -- pinning the dialog to the screen's
    # top-left corner regardless of where any real window (the CAMA
    # Tools panel, Global Mapper, etc.) actually was. Screen dimensions
    # are a stable, always-meaningful reference this dialog can center
    # against instead. Button layout (packed side="bottom" above) is
    # unaffected -- this only changes where the whole window is placed,
    # not how its own contents are arranged inside it.
    screen_w = dialog.winfo_screenwidth()
    screen_h = dialog.winfo_screenheight()
    x = (screen_w - req_w) // 2
    y = (screen_h - req_h) // 2
    dialog.geometry(f"{req_w}x{req_h}+{max(x,0)}+{max(y,0)}")

    # deiconify/lift/focus_force/topmost are called LAST -- after all
    # content is built and geometry() has already repositioned the
    # window -- not before. Calling them before geometry() risked losing
    # the z-order fight against another always-on-top window from a
    # separate process (the CAMA Tools floating panel, which sets its
    # own -topmost persistently): repositioning a window can perturb its
    # stacking order on some Windows compositor/window-manager builds,
    # so asserting "come to front" only after the window has reached its
    # final size/position is the safer order. See also the comment above
    # -- topmost is deliberately never reset back to False afterward,
    # since this dialog can stay open indefinitely waiting on the user.
    dialog.deiconify()
    dialog.lift()
    dialog.focus_force()
    dialog.attributes("-topmost", True)

    # See ask_overwrite_dialog()'s matching comment -- a one-time lift()
    # at creation isn't enough against a persistently-topmost window from
    # a separate process (the CAMA Tools floating panel). Keeps
    # re-asserting for the dialog's whole (indefinite) lifetime;
    # self-cancels once dialog.destroy() runs in choose() above.
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
    apply_icon(dialog, "roadfrontage.ico")
    dialog.title("ROAD FRONTAGE TOOL")
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
    # Centered on the SCREEN, not on `parent` -- `parent` here is
    # MAIN.py's _tool_root, a deliberately invisible 1x1 anchor window
    # placed off-screen at (-9999, -9999) purely for Tk plumbing (icon
    # binding, mainloop hosting). Centering against it produced a wildly
    # negative x/y that the old max(x,0)/max(y,0) clamp collapsed to
    # exactly (0, 0) every time -- pinning the dialog to the screen's
    # top-left corner regardless of where any real window (the CAMA
    # Tools panel, Global Mapper, etc.) actually was. Screen dimensions
    # are a stable, always-meaningful reference this dialog can center
    # against instead. Button layout (packed side="bottom" above) is
    # unaffected -- this only changes where the whole window is placed,
    # not how its own contents are arranged inside it.
    screen_w = dialog.winfo_screenwidth()
    screen_h = dialog.winfo_screenheight()
    x = (screen_w - req_w) // 2
    y = (screen_h - req_h) // 2
    dialog.geometry(f"{req_w}x{req_h}+{max(x,0)}+{max(y,0)}")

    # deiconify/lift/focus_force/topmost are called LAST -- see
    # confirm_db_overwrite_dialog()'s matching comment for the full
    # rationale.
    dialog.deiconify()
    dialog.lift()
    dialog.focus_force()
    dialog.attributes("-topmost", True)

    # See ask_overwrite_dialog()'s matching comment -- periodic
    # re-assertion for the dialog's whole lifetime; self-cancels once
    # dialog.destroy() runs in choose() above.
    def _keep_dialog_on_top():
        if dialog.winfo_exists():
            dialog.lift()
            dialog.attributes("-topmost", True)
            dialog.after(250, _keep_dialog_on_top)
    dialog.after(250, _keep_dialog_on_top)

    dialog.wait_window()
    return result["chosen"]


# ========================================
# MAIN WINDOW
# ========================================
def open_main_window(root):
    """
    Builds and shows the tool's single unified configuration window:
    Land Parcel and Road Network source pickers (each with a
    Local-file/Database-table radio toggle), a Road Classification
    section (per-source Lot Location/Lot Label checkboxes, mutually
    exclusive with the Road Type exclusion filter -- see the module
    docstring and the RUNTIME STATE section), an Output destination
    picker, and a Run button gated by _update_run_button_state().

    Runs TWO independent background detect-on-select systems (each its
    own daemon thread + win.after()-polled queue.Queue): one for Land
    Parcel existing-output-column detection
    (_refresh_parcel_classification()), one for Road Network road-type/
    classification reading (_refresh_road_classification(), whose
    result is cached in _road_gdf_cache for run_processing() to reuse).
    Both are independent of Run being clicked.

    Args:
        root: the parent Tk root this window is opened under.
    """
    win = tk.Toplevel(root)
    apply_icon(win, "roadfrontage.ico")
    win.title("Road Frontage & Depth-To-Width Ratio Tool")
    win.resizable(False, False)
    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))

    # ── state ────────────────────────────────────────────────────
    # master=win on every StringVar is required — without it, Tkinter
    # attaches the variable to whatever the "default root" happens to be,
    # which is unreliable in this dispatcher's multi-window setup
    # (MAIN3.py creates a hidden root, then this Toplevel is created on
    # top of it). A mismatched master causes widgets to silently read a
    # different variable instance than the one being set, showing blank
    # labels and radio buttons that never appear selected — confirmed as
    # the root cause of the blank-GUI symptom reported in testing.
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

    PAD = dict(padx=8, pady=4)

    def section_label(parent, text):
        frm = tk.Frame(parent)
        frm.pack(fill="x", padx=10, pady=(10, 2))
        tk.Label(frm, text=text,
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        ttk.Separator(frm, orient="horizontal").pack(
            side="left", fill="x", expand=True, padx=(6, 0), pady=4)

    # ── Road Classification state (new) ─────────────────────────
    #   - parcel_classification_vars: {path_or_table: tk.BooleanVar} --
    #     one checkbox PER selected Land Parcel source that has a usable
    #     LOT_LOCATION/LOT_LABEL column. Lives under Land Parcel Source.
    #     Sources without a usable column get no checkbox at all -- they
    #     simply aren't listed (no placeholder text either; per-project
    #     convention, users are trained on this tool directly).
    #   - filter_road_type_var: "Filter by Road Type" -- lives under Road
    #     Network Source, since it depends entirely on the ROAD layer's
    #     columns. Structurally the same control as lot_location.py's
    #     road_type_filter_check_var (a plain Checkbutton, opt-in, default
    #     unchecked even when a usable column is found).
    # Mutual exclusion (wired via trace_add() below, once per-source
    # checkbox is created, plus once for filter_road_type_var): checking
    # Filter by Road Type unchecks every per-source classification
    # checkbox; checking any per-source classification checkbox unchecks
    # Filter by Road Type. Multiple per-source classification checkboxes
    # CAN be checked together -- they don't conflict with each other,
    # only with Filter by Road Type.
    parcel_classification_vars = {}
    filter_road_type_var = tk.BooleanVar(master=win, value=False)

    # road_type_value_vars: {display_text: (real_value, tk.BooleanVar)}
    # for the Filter by Road Type checklist -- same structure/semantics as
    # lot_location.py's road_type_value_vars (checked = keep, unchecked =
    # exclude). No Select All / Unselect All controls: lot_location.py
    # (the canonical implementation this pattern is adapted from) does
    # not actually have them -- confirmed against the real file.
    road_type_value_vars = {}

    # run_status_var: drives the always-visible "Ready to run." / "Reading
    # ...' / "Please select ..." label just above the Run button, and
    # gates whether the Run button itself is enabled (_update_run_button_state()
    # below). Same philosophy as lot_location.py's run_status_var -- the
    # GUI must never let the user launch a run whose Road Classification
    # outcome hasn't finished being determined yet.
    run_status_var = tk.StringVar(master=win, value="Preparing…")

    # Background-read state for the two new inspection reads (parcel ->
    # LOT_LOCATION/LOT_LABEL detection, road -> ROAD_TYPE detection). Plain
    # closure locals, mutated via `nonlocal` from the nested functions
    # below -- never touched from a worker thread, only from win.after()
    # polling on the main thread (same discipline as lot_location.py).
    road_is_reading = False
    parcel_is_reading = False
    # parcel_read_details: per-source breakdown from the most recent
    # background read -- list of (path_or_table, state, col_name, kind,
    # existing_output_cols) tuples, one per selected parcel source. This
    # is the single source of truth the per-source checklist is built
    # from (only sources with state == LOT_STATE_FOUND get a row/
    # checkbox; the rest are omitted entirely, not shown as "not found").
    # existing_output_cols (Priority 2) is a dict {"CAMA_ROAD_FRONTAGE": name,
    # "CAMA_DEPTH": name, "CAMA_DEPTH_WIDTH_RATIO": name} containing only the
    # targets that actually have a pre-existing column match -- see
    # _detect_existing_output_columns().
    parcel_read_details = []
    # parcel_output_column_conflicts: derived from parcel_read_details
    # (see _check_parcel_frontage_conflicts() below) -- list of
    # (path_or_table, existing_output_cols) tuples, one per source where
    # the merged background read found at least one pre-existing
    # ROAD_FRONTAGE/DEPTH/DEPTH_WIDTH_RATIO column. Kept in sync with
    # parcel_read_details everywhere the latter is (re)assigned. Consumed
    # by the combined confirmation dialog in on_run() -- unlike the
    # LOT_LOCATION checklist above, this check has no GUI checklist of
    # its own; it is purely a yes/no warning shown once at Run time.
    parcel_output_column_conflicts = []

    # _suppress_mutual_exclusion: guards against the circular cascade
    # between the two mutual-exclusion trace callbacks below
    # (_on_parcel_classification_checkbox_changed and
    # _on_filter_road_type_changed). Without this, checking "Filter by
    # Road Type" while a parcel classification checkbox is already
    # checked triggers: filter->True enters its callback, which sets the
    # parcel var->False, which SYNCHRONOUSLY fires ITS OWN callback
    # (Tkinter trace callbacks run synchronously within the .set() call,
    # not queued), which sees filter_road_type_var still True (the outer
    # callback hasn't returned yet) and sets it back to False -- so the
    # checkbox the user just checked immediately un-checks itself. Each
    # callback sets this flag before touching the OTHER control and
    # clears it in a finally block, so the nested callback can detect
    # "an exclusion enforcement is already in progress" and return
    # immediately instead of re-triggering the opposite direction.
    _suppress_mutual_exclusion = False

    def _read_gdf_worker(source_type, path_or_table):
        """
        Runs on a background thread. Generic reader used by BOTH new
        Road Classification background reads (parcel -> LOT_LOCATION/
        LOT_LABEL detection, road -> ROAD_TYPE detection). Returns
        (gdf, error) and never touches any Tkinter widget or variable.
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

    def _reflow_window():
        """
        Safety net for dynamic-width/height content -- the per-source
        classification checklist growing/shrinking, long file/table path
        labels, and the road-type checklist growing/shrinking -- combined
        with win.resizable(False, False) above.

        Root-caused bug this fixes: calling geometry("") on a window that
        is ALREADY resizable(False, False) doesn't just recompute size --
        on this Tk build it also re-locks the window's min/max size to
        whatever is CURRENTLY packed at that exact moment. If this fires
        while a section is in a smaller, transient state (e.g. the
        "⏳ Reading parcel…" indicator showing instead of the eventual
        checklist), the window gets permanently capped at that smaller
        size -- later content (the checklist appearing, other sections
        below it) has nowhere to grow into and visibly overlaps/truncates
        instead.

        First attempt at a fix toggled resizable(True, True) around the
        geometry("") call to force Tk to release the stale lock. That
        fixed the truncation but introduced a new, visible problem: on
        Windows, toggling `resizable` itself repaints the window's
        border/decoration, so calling it 2-3 times in quick succession
        (this fires multiple times per single browse action) reads as the
        whole window "blinking".

        Fix: never touch resizable() again after the one-time initial
        call above. Instead, directly measure the window's current
        natural size (winfo_reqwidth/reqheight, after update_idletasks()
        flushes pending layout) and set minsize/maxsize/geometry to
        exactly that size. This achieves the same "always sized correctly
        for current content" result without ever re-toggling the resize
        lock, so there's no decoration repaint and no blink.
        """
        win.update_idletasks()
        req_w = win.winfo_reqwidth()
        req_h = win.winfo_reqheight()
        win.minsize(req_w, req_h)
        win.maxsize(req_w, req_h)
        win.geometry(f"{req_w}x{req_h}")

    # NOTE: a _freeze_window_size() helper previously existed here,
    # pinning the window's min/max size to its current displayed size
    # immediately before a checklist was cleared, so the automatic
    # pack-geometry shrink (from clearing) wouldn't be visible before the
    # new content arrived. It has been REMOVED: the swap-based checklist
    # lifecycle below (see _rebuild_lot_classification_checklist() and
    # _rebuild_road_type_checklist()) never clears/destroys the OLD
    # checklist until the NEW one is already built and ready to take its
    # place in a single swap -- there is no longer an empty intermediate
    # state to freeze against. If a future need for it reappears, it
    # was: win.update_idletasks(); w,h = win.winfo_width(),
    # win.winfo_height(); win.minsize(w,h); win.maxsize(w,h).

    def _rebuild_road_type_checklist():
        """
        Swap-based rebuild: builds the new Road Type checklist in a
        fresh, off-screen Frame first, then swaps road_type_checklist_
        container's reference to it and destroys the old one -- the old
        checklist is never cleared/destroyed before the new one is fully
        built, so the GUI never passes through an empty intermediate
        state. Deliberately does NOT pack the new container, resize the
        scroll box, or call _reflow_window() here -- every caller of
        this function calls _update_road_classification_visibility()
        immediately afterward, which owns all packing/sizing/
        positioning decisions for whichever container is current at
        that moment.

        D-Scrollbar (this task): the new container is now built as a
        child of road_type_checklist_canvas (not road_frame directly --
        road_frame's own direct children are road_filter_checkbox and
        road_type_checklist_outer, see the widget construction below),
        and the swap itself additionally re-points the canvas's own
        embedded window item (_road_type_canvas_window) at the new
        container via itemconfig -- the SAME window item id is reused
        across every rebuild, only which Frame it displays changes, so
        the canvas's scroll position/scrollregion machinery never needs
        to be torn down and rebuilt. The new container's own
        <Configure> binding (for scrollregion updates) is re-established
        here too, since binding follows the widget instance, not the
        window item id.
        """
        nonlocal road_type_checklist_container
        new_container = tk.Frame(road_type_checklist_canvas)
        for display_text in sorted(road_type_value_vars.keys()):
            real_value, var = road_type_value_vars[display_text]
            tk.Checkbutton(new_container, text=display_text,
                           variable=var).pack(anchor="w")

        def _on_road_type_content_configure(_event=None):
            road_type_checklist_canvas.configure(
                scrollregion=road_type_checklist_canvas.bbox("all"))
        new_container.bind("<Configure>", _on_road_type_content_configure)

        old_container = road_type_checklist_container
        road_type_checklist_container = new_container
        road_type_checklist_canvas.itemconfig(_road_type_canvas_window, window=new_container)
        old_container.destroy()

    def _resize_road_type_checklist_box():
        """
        Recomputes road_type_checklist_canvas's own height/width handling
        to fit its current content. Ported from lot_location.py's own
        _resize_road_type_checklist_box() (itself ported from meters_
        from_school_shop_transport_church.py's _resize_other_landmarks_
        checklist_box()) -- re-traced against this file's OWN widget
        tree (road_action_row is a Label(width=42) + a Browse button
        here too, confirmed matching) rather than assumed to be a
        drop-in port.

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

    def _on_parcel_classification_checkbox_changed(*_args):
        """
        Mutual exclusion: checking ANY per-source "Use LOT_LOCATION/
        LOT_LABEL" checkbox un-checks "Filter by Road Type" if it was on.
        Multiple per-source checkboxes CAN be checked together -- this
        only fires the OTHER direction (toward Filter by Road Type), so
        checking a second per-source box while a first is already checked
        does not affect either of them.

        Operation order (kept identical to _on_filter_road_type_changed()
        below on purpose -- same four steps, same sequence, so the two
        callbacks stay easy to compare and don't drift into inconsistent
        behavior as either one changes later):
          1. Guarded mutual-exclusion mutation
          2. (No cache synchronization step -- detection results are no
             longer cached at all; parcel_classification_vars is simply the
             live, current set of BooleanVars for whatever was most
             recently read; kept as an explicit "nothing to do" step
             only for structural symmetry with the other callback)
          3. Visibility refresh
          4. Run button update

        _suppress_mutual_exclusion guards ONLY step 1 (the mutation) --
        NOT the whole callback. Steps 2-4 must always run, even when this
        callback was re-entered (nested) while already suppressed, or a
        genuine, needed UI refresh gets silently skipped (this was a real
        regression caught during testing: the mirror callback's
        visibility refresh was being skipped this way). Steps 2-4 are
        safe to run unconditionally because none of them call .set() on
        any traced BooleanVar/StringVar that could re-trigger this or the
        other mutual-exclusion callback -- verified directly against
        _update_road_classification_visibility() (pack()/pack_forget()/
        .get() only) and _update_run_button_state() (only .set()s
        run_status_var, which has no trace_add() of its own).
        """
        nonlocal _suppress_mutual_exclusion
        if not _suppress_mutual_exclusion:
            _suppress_mutual_exclusion = True
            try:
                if filter_road_type_var.get():
                    filter_road_type_var.set(False)
            finally:
                _suppress_mutual_exclusion = False
        # Step 2: cache sync -- nothing to do, see docstring.
        # Step 3: visibility refresh -- the mutation above may have just
        # turned Filter by Road Type off, which the road checklist's
        # visibility depends on.
        _update_road_classification_visibility()
        # Step 4: run button update.
        _update_run_button_state()

    def _rebuild_lot_classification_checklist():
        """
        Rebuilds the per-source classification checklist from
        parcel_read_details: one checkbox per selected parcel source that
        has a usable LOT_LOCATION/LOT_LABEL column (state == LOT_STATE_FOUND).
        Sources without a usable column are omitted entirely -- not shown
        with a "not found" line, matching lot_location.py's own
        auto-hide-when-nothing-usable convention. If no source qualifies
        at all, the box is simply left empty -- its own visibility/height
        is decided separately by _update_parcel_classification_visibility(),
        not by whether it happens to have children.

        Checkbox label is "Use <col_name> in <filename/table>" (e.g. "Use
        LOT_LABEL in Barangay_123.gpkg") -- filename/table name only, not
        the full path. Sufficient to disambiguate between multiple
        sources at a glance: a single Browse action always selects files
        from exactly one folder and REPLACES the previous selection, and
        no filesystem allows duplicate filenames within one folder (nor
        duplicate table names within one schema), so this name is always
        guaranteed unique among the currently selected sources -- no
        separate "full path" popup is needed.

        Always creates fresh BooleanVars -- no reuse-across-calls
        mechanism (that existed only to preserve checkbox state across a
        cache hit; detection results are no longer cached at all, so
        every rebuild reflects a
        genuinely fresh read and starts each checkbox unchecked).

        Plain destroy-and-repopulate, called directly by the CALLER
        before _update_parcel_classification_visibility() (never by that
        function itself, which only handles the classification box's
        own visibility/sizing -- see its own docstring). Ported from
        road_width.py's canonical Canvas-based pattern: this used to be a
        swap-based rebuild (build off-screen, swap, destroy old) to avoid
        a brief empty-content moment affecting the WINDOW's own size --
        now that lot_classification_list_container lives inside a fixed,
        capped-height Canvas (see its construction in Section 1 below),
        clearing and repopulating its children in place can never change
        the window's size at all, so there is nothing left to protect
        against, and the simpler in-place approach is used instead.
        """
        for child in lot_classification_list_container.winfo_children():
            child.destroy()
        new_vars = {}

        for path_or_table, state, col_name, kind, _existing_output_cols in parcel_read_details:
            if state != LOT_STATE_FOUND:
                continue
            var = tk.BooleanVar(master=win, value=False)
            var.trace_add("write", _on_parcel_classification_checkbox_changed)
            new_vars[path_or_table] = var

            # os.path.basename() is safe to call unconditionally here even
            # for database table names (which have no path separators) --
            # it just returns the string unchanged in that case, so this
            # works for both local files and DB tables without needing to
            # track which kind each entry is. No separate "full path"
            # popup is needed: a single Browse action always selects
            # files from exactly one folder (a native OS file dialog
            # can't span two folders in one session) and REPLACES the
            # previous selection rather than appending to it, and no
            # filesystem allows two files with the same name in the same
            # folder -- so the filename shown here is always guaranteed
            # unique among the currently selected sources. Database table
            # names are similarly guaranteed unique within one schema.
            display_name = os.path.basename(path_or_table)
            tk.Checkbutton(
                lot_classification_list_container,
                text=f"Use {col_name} in {display_name}", variable=var
            ).pack(anchor="w")

        parcel_classification_vars.clear()
        parcel_classification_vars.update(new_vars)

    def _update_parcel_classification_visibility():
        """
        Decides whether the classification box (lot_classification_outer,
        a content-adaptive scrollable Canvas -- see its construction in
        Section 1 below) is shown at all, and if so, resizes it to fit
        its current content (capped -- see _resize_lot_classification_box()).
        Hidden entirely both when no Land Parcel source is selected, AND
        when one is selected but yields nothing to show (no source has a
        usable classification column) -- an empty, pointlessly-scrollable
        box was worse than just not showing it.

        Deliberately does NOT reference parcel_is_reading or
        parcel_reading_lbl at all -- the "Reading…" indicator no longer
        lives here (see _set_parcel_reading_state()'s docstring, which
        reuses the existing "N file(s)/table(s) selected" label instead
        of a separate widget). This box is left completely UNTOUCHED for
        the entire duration of a background read: if it was already
        showing a previous file's checklist, it stays exactly as it was
        until the new read's actual result is known -- callers only
        invoke _rebuild_lot_classification_checklist() (which this
        function assumes has already run) once that result is ready, so
        this function triggers at most ONE resize per state change,
        never a second one layered close in time on top of an earlier
        "entering reading" resize.

        Assumes the caller already populated
        lot_classification_list_container via
        _rebuild_lot_classification_checklist() before calling this
        function -- kept as the caller's responsibility rather than
        threaded through here.
        """
        has_any_parcel_source = (
            bool(parcel_local_path) if parcel_source_type.get() == "local"
            else bool(parcel_db_table)
        )

        if not has_any_parcel_source:
            if lot_classification_outer.winfo_ismapped():
                lot_classification_outer.pack_forget()
                _reflow_window()
            return

        has_content = bool(lot_classification_list_container.winfo_children())
        if not has_content:
            if lot_classification_outer.winfo_ismapped():
                lot_classification_outer.pack_forget()
                _reflow_window()
            return

        _resize_lot_classification_box()
        if not lot_classification_outer.winfo_ismapped():
            lot_classification_outer.pack(
                anchor="w", fill="x", pady=(2, 0), after=parcel_action_row)
        _reflow_window()

    def _check_all_road_types():
        """
        Sets every discovered ROAD_TYPE value's BooleanVar to True --
        iterates road_type_value_vars directly (the full dictionary
        populated by _rebuild_road_type_checklist() for every distinct
        value this read found), never the Tkinter Checkbutton widgets
        currently rendered inside the Canvas viewport -- so every value
        is checked regardless of whether its row is currently scrolled
        into view. Each Checkbutton is bound to its own BooleanVar, so
        this automatically and correctly updates every widget's
        displayed check-state too, including off-screen ones, the
        moment they're scrolled into view. Mirrors lot_location.py's
        own _check_all_road_types() exactly (itself ported from
        meters_from_school_shop_transport_church.py's
        _check_all_other_landmarks()).

        Checking/unchecking individual ROAD_TYPE values never gates the
        Run button here either (same as lot_location.py) -- so there is
        no _update_run_button_state() call needed.
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

    def _update_road_classification_visibility():
        """
        Shows the "Filter by Road Type" checkbox plus (if checked) its
        per-value checklist. No usable ROAD_TYPE-like column found shows
        neither -- matches lot_location.py exactly.

        While a background read is in flight (road_is_reading), this
        function does nothing at all -- no widget touched, no resize.
        The "⏳ Reading road network…" indication is handled entirely by
        _set_road_reading_state()'s in-place label text-swap (see its
        docstring), not by anything here. This is a change from the
        tool's earlier design, where this function additionally packed
        a separate road_reading_lbl widget while reading -- removed
        along with that widget (see _set_road_reading_state()'s
        docstring for why).

        Invariant this preserves: the GUI never passes through an empty
        intermediate state just because a background refresh is in
        progress. The checkbox/checklist's own pack state/position is
        entirely decided HERE (not by _rebuild_road_type_checklist(),
        which only swaps which Frame object is embedded in the canvas)
        -- while reading, this function does not touch road_filter_
        checkbox's or road_type_checklist_outer's pack state at all, so
        both stay exactly as they were.

        D-Scrollbar (this task): road_type_checklist_container itself
        (the actual Checkbutton parent) is no longer packed/unpacked
        directly -- it lives inside road_type_checklist_canvas, which
        lives inside road_type_checklist_outer alongside the vertical/
        horizontal scrollbars. road_type_checklist_outer is what gets
        packed/unpacked here now. _resize_road_type_checklist_box() is
        called whenever the checklist has content, immediately before
        _reflow_window(), so the window's own size is always computed
        from the checklist's current (possibly just-resized) box.

        D-CheckAll (this task): road_filter_checkbox itself is no
        longer packed/unpacked directly either -- it now lives inside
        road_type_header_row alongside road_type_links_frame (the
        "Check All"/"Uncheck All" group). road_type_header_row is what
        gets packed/unpacked here now, and its WIDTH is pinned every
        call (via pack_propagate(False), to road_action_row's own
        established width -- the same reference _resize_road_type_
        checklist_box() already uses for the canvas) so the combined
        checkbox+links text on one line never grows the window wider.
        road_type_checklist_outer's own `after=` anchor is updated to
        road_type_header_row (its actual sibling within road_frame now
        -- road_filter_checkbox is no longer road_type_checklist_
        outer's sibling, it is nested one level deeper inside road_
        type_header_row). road_type_links_frame itself is shown only
        when the checklist itself would be shown (checked AND has
        content) -- the exact same condition as the checklist below.
        """
        if road_is_reading:
            return
        if road_type_value_vars:
            win.update_idletasks()
            fixed_row_width = road_action_row.winfo_reqwidth()
            row_height = road_filter_checkbox.winfo_reqheight()
            road_type_header_row.configure(width=fixed_row_width, height=row_height)
            road_type_header_row.pack_propagate(False)
            road_type_header_row.pack(fill="x", pady=(2, 0))
            if filter_road_type_var.get():
                road_type_links_frame.pack(side="right")
                road_type_checklist_outer.pack(
                    fill="x", padx=(ROAD_TYPE_CHECKLIST_LEFT_INDENT, 0), pady=(2, 0),
                    after=road_type_header_row)
                _resize_road_type_checklist_box()
            else:
                road_type_links_frame.pack_forget()
                road_type_checklist_outer.pack_forget()
        else:
            road_type_header_row.pack_forget()
            road_type_links_frame.pack_forget()
            road_type_checklist_outer.pack_forget()
        _reflow_window()

    def _on_filter_road_type_changed(*_args):
        """
        Mutual exclusion, mirror of _on_parcel_classification_checkbox_changed
        above: checking "Filter by Road Type" un-checks EVERY per-source
        classification checkbox currently on the Land Parcel side.

        Operation order identical to
        _on_parcel_classification_checkbox_changed() above, deliberately:
          1. Guarded mutual-exclusion mutation
          2. (No cache synchronization step -- Road Network's checklist
             state is no longer restorable from cache at all; kept as an explicit
             "nothing to do" step only for structural symmetry with the
             other callback)
          3. Visibility refresh
          4. Run button update

        _suppress_mutual_exclusion guards ONLY step 1 -- NOT the whole
        callback. This callback can itself be re-entered (nested) by the
        other callback's own var.set(False) call; when that happens,
        step 1 correctly no-ops (avoiding the circular bounce-back this
        flag exists to prevent), but steps 2-4 still run using
        filter_road_type_var's final, already-settled value from the
        OUTER call -- skipping them here was a real regression caught
        during testing (the Road Type checklist stayed visible after
        being un-checked via this exact nested path). See the sibling
        callback's docstring for why steps 2-4 are safe to run
        unconditionally (no traced Var is ever .set() inside them).
        """
        nonlocal _suppress_mutual_exclusion
        if not _suppress_mutual_exclusion:
            _suppress_mutual_exclusion = True
            try:
                if filter_road_type_var.get():
                    for var in parcel_classification_vars.values():
                        var.set(False)
            finally:
                _suppress_mutual_exclusion = False
        # Step 3: visibility refresh.
        _update_road_classification_visibility()
        # Step 4: run button update.
        _update_run_button_state()

    filter_road_type_var.trace_add("write", _on_filter_road_type_changed)

    def _set_parcel_reading_state(reading):
        """
        Disables Land Parcel Browse/radio controls while its background
        classification read is in progress -- prevents starting a
        second, overlapping read of the same source.

        Also drives the "Reading..." indicator itself -- but NOT via a
        separate widget or any pack()/pack_forget() call. It reuses the
        EXISTING "N file(s) selected" / "N table(s) selected" label
        (parcel_lbl, bound to parcel_files_var / parcel_db_label) that's
        already permanently present in parcel_action_row, temporarily
        overwriting its text via the StringVar and restoring it once
        done. Since this label's own row never changes shape because of
        a text-length change (no fill/expand on it, nothing below it
        repositions), this transition needs -- and gets -- ZERO
        _reflow_window() calls. Ported from road_width.py's validated
        pattern: an earlier design showed/hid a separate classification-
        checklist-adjacent indicator widget instead, which meant an extra
        window resize per read cycle -- close in time to the resize at
        the end of the same cycle, confirmed to make visual distortion
        WORSE, not better. The classification checklist box itself
        (lot_classification_outer) is left completely untouched during
        reading -- see _refresh_parcel_classification() and
        _poll_parcel_classification_queue() -- so it only ever resizes
        once, when the read's final result is actually known.
        """
        state = "disabled" if reading else "normal"
        parcel_btn.config(state=state)
        parcel_radio_local.config(state=state)
        parcel_radio_db.config(state=state)

        if reading:
            # Plural logic is removed: under the single-selection
            # architecture, it is structurally impossible for more than
            # one source to be selected, so the plural branch can never
            # fire. The invariant is the reason, not an assumption about
            # runtime state. Color matches the Road Network side's own
            # "Reading..." indicator (#b36b00, amber) for visual
            # consistency between the two sections.
            parcel_files_var.set("⏳ Reading Land Parcel...")
            parcel_db_label.set("⏳ Reading Land Parcel...")
            parcel_lbl.config(fg="#b36b00")
        else:
            # Restore from authority variables -- never from StringVar
            # state. Same pattern as _toggle_parcel() below.
            parcel_files_var.set(
                os.path.basename(parcel_local_path) if parcel_local_path
                else "No file selected"
            )
            parcel_db_label.set(
                parcel_db_table if parcel_db_table
                else "No table selected"
            )
            parcel_lbl.config(fg="gray")
        _update_run_button_state()

    def _set_road_reading_state(reading):
        """
        Disables Road Network Browse/radio controls while its background
        classification read is in progress -- prevents starting a
        second, overlapping read of the same source.

        Also drives the "Reading road network…" indicator itself -- but
        NOT via a separate widget or any pack()/pack_forget() call.
        Reuses the EXISTING "No file selected" / filename / "No table
        selected" label (road_lbl, bound to road_file_var / road_db_var
        per _toggle_road()'s textvariable swap) that's already
        permanently present in road_action_row, temporarily overwriting
        its text via the currently-bound StringVar and restoring it once
        done -- exact same principle as _set_parcel_reading_state()
        above. Since this label's own row never changes shape from a
        text-length change, this transition needs -- and gets -- ZERO
        _reflow_window() calls.

        UPDATE: this REPLACES the tool's earlier separate-widget design
        (road_reading_lbl, packed/unpacked via
        _update_road_classification_visibility()) -- that design was a
        deliberate prior decision (see project history), reasoned that
        Road Network's checklist (typically 5-15 ROAD_TYPE values) was a
        smaller layout-distortion risk than Land Parcel's, not worth the
        added complexity of matching the reused-label treatment. Revisited
        and overridden: the same layout-jump symptom (every widget below
        the Road Network section visibly shifting when the indicator
        appeared/disappeared) was confirmed to occur here too, regardless
        of the checklist's smaller typical size -- the distortion comes
        from adding/removing a widget row at all, not from how large the
        checklist happens to be. Matches lot_location.py's own Road
        Network fix, applied for the same reason.
        """
        state = "disabled" if reading else "normal"
        road_btn.config(state=state)
        road_radio_local.config(state=state)
        road_radio_db.config(state=state)

        if reading:
            if road_source_type.get() == "local":
                road_file_var.set("⏳ Reading road network…")
            else:
                road_db_var.set("⏳ Reading road network…")
            road_lbl.config(fg="#b36b00")
        else:
            if road_source_type.get() == "local":
                road_path = road_local_path.get()
                road_file_var.set(os.path.basename(road_path) if road_path else "No file selected")
            else:
                road_table = road_db_table.get()
                road_db_var.set(road_table if road_table else "No table selected")
            road_lbl.config(fg="gray")
        _update_run_button_state()

    def _update_run_button_state():
        """
        Single source of truth for whether the Run button may be pressed.
        The GUI's displayed Road Classification state must always match
        what will actually happen at runtime: while a background read
        (parcel OR road) is still in progress, the checkboxes above
        haven't yet caught up to the true effective state, so Run stays
        disabled until both finish -- rather than letting the user launch
        a run whose classification outcome the GUI hasn't reflected yet.
        This only gates *when* the button may be pressed; the eventual
        classification is still resolved independently per parcel source
        at runtime, exactly as before.

        Explicit bg/fg/cursor toggling (not just state=) is required and
        mirrors lot_location.py's run_btn handling exactly: Tkinter does
        NOT automatically gray out a classic tk.Button's custom bg/fg when
        state="disabled" -- only `disabledforeground` gets a built-in
        default, and it doesn't coordinate with a custom `bg`. Cursor is
        toggled the same explicit way: Tkinter does not suppress a
        widget's assigned `cursor` just because state="disabled" -- the
        last-assigned cursor keeps showing regardless, so "no" must be
        set for the disabled state as deliberately as "hand2" is set for
        the enabled one.
        """
        has_parcel = bool(parcel_local_path) if parcel_source_type.get() == "local" else bool(parcel_db_table)
        has_road = bool(road_local_path.get()) if road_source_type.get() == "local" else bool(road_db_table.get())
        has_output = bool(output_local_dir.get()) if output_dest_type.get() == "local" else True

        if not has_parcel:
            run_status_var.set("Please select a Land Parcel source.")
            ready = False
        elif not has_road:
            run_status_var.set("Please select a Road Network source.")
            ready = False
        elif not has_output:
            run_status_var.set("Please select an Output destination.")
            ready = False
        elif parcel_is_reading:
            checking_name = (
                os.path.basename(parcel_local_path) if parcel_source_type.get() == "local"
                else parcel_db_table
            ) or "source"
            run_status_var.set(f'Checking "{checking_name}" columns…')
            ready = False
        elif road_is_reading:
            checking_name = (
                os.path.basename(road_local_path.get()) if road_source_type.get() == "local"
                else road_db_table.get()
            ) or "source"
            run_status_var.set(f'Checking "{checking_name}" columns…')
            ready = False
        else:
            run_status_var.set("Ready to run.")
            ready = True

        if ready:
            run_btn.config(state="normal", cursor="hand2",
                            bg="#2e7d32", fg="white")
        else:
            run_btn.config(state="disabled", cursor="no",
                            bg="#e0e0e0", fg="#888888", disabledforeground="#888888")

    def _handle_parcel_check_failure(source_type, reason):
        """
        Shared cleanup for both outcomes of a FAILED Land Parcel
        background read: a read that never completed within 60 seconds
        ("timeout"), or one that completed but the single selected
        source's read itself failed ("failure" -- signaled by
        per_source_results containing a (path_or_table, None, None,
        None, {}) tuple, i.e. state is None -- see worker()'s docstring
        inside _refresh_parcel_classification()).

        Captures the failed source's display name BEFORE clearing the
        authority variable (needed for the dialog text below), then
        clears ONLY the authority variable for source_type (the mode
        that was actually being read) -- parcel_local_path if source_type
        is "local", parcel_db_table if "db". Also resets
        parcel_read_details/parcel_output_column_conflicts and rebuilds
        the (now-empty) classification checklist -- there is no valid
        checklist data to show when the read that would have produced it
        never succeeded.

        Clearing the authority variable is the entire recovery
        mechanism -- no new "check failed" state is introduced. This
        forces the EXISTING "no source selected -> Run disabled" path
        (_update_run_button_state()) to handle recovery: the display
        reverts to "No file selected" / "No table selected", and the
        user must select a source again.

        CRITICAL: unlike the simpler single-worker tools (e.g.
        road_density.py), where _set_parcel_reading_state() itself
        resets parcel_is_reading internally, THIS tool's
        _set_parcel_reading_state() only manages widget state --
        parcel_is_reading here is owned directly by
        _refresh_parcel_classification()/_poll_parcel_classification_
        queue()'s own piggyback structure. It must be explicitly reset
        to False here too, or every subsequent
        _refresh_parcel_classification() call (including one triggered
        by toggling back to a source that still has a valid selection)
        would silently no-op forever against its own
        "if parcel_is_reading: return" guard. (This exact gap was found
        and fixed in road_width.py first -- see that file's identical
        function for the bug this avoids from the start here.)

        _set_parcel_reading_state(False) is called BEFORE the dialog is
        shown, not after -- messagebox.showerror() is modal and blocks
        here until dismissed, so showing it first would leave the
        "⏳ Reading Land Parcel…" indicator frozen on screen for the
        entire time the dialog is up.
        """
        nonlocal parcel_is_reading, parcel_local_path, parcel_db_table, parcel_read_details, parcel_output_column_conflicts

        parcel_is_reading = False

        if source_type == "local":
            failed_name = (os.path.basename(parcel_local_path)
                           if parcel_local_path else "the selected file")
            parcel_local_path = None
        else:
            failed_name = parcel_db_table if parcel_db_table else "the selected table"
            parcel_db_table = None

        parcel_read_details = []
        parcel_output_column_conflicts = []
        _rebuild_lot_classification_checklist()
        _update_parcel_classification_visibility()

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

    def _check_parcel_frontage_conflicts(details):
        """
        Extracts the output-column-conflict subset out of
        parcel_read_details (or an equivalent list, e.g. a cache slot's
        stored details) -- one (path_or_table, existing_output_cols)
        tuple per source where the merged background read found at
        least one pre-existing column matching ROAD_FRONTAGE, DEPTH,
        or DEPTH_WIDTH_RATIO. Sources with no conflict, or that failed
        to read, are simply absent from the result.
        """
        return [
            (path_or_table, existing_output_cols)
            for path_or_table, _state, _col_name, _kind, existing_output_cols in details
            if existing_output_cols
        ]

    def _refresh_parcel_classification():
        """
        Background-reads EVERY currently selected Land Parcel file/table
        (not just the first) so the per-source checklist can offer a
        checkbox for every source that actually has a usable
        LOT_LOCATION/LOT_LABEL column. Each source is still resolved
        independently at run time regardless of what's checked here;
        this background read only decides which checkboxes to SHOW.

        Deliberately does NOT cache the result across calls -- every
        call, whether triggered by a fresh Browse/Select or by toggling
        Local <-> Database, always performs a real read. A cache keyed
        only on "which file/table was selected" cannot detect that the
        file/table's CONTENTS changed externally (e.g. another CAMA
        tool, QGIS, or Global Mapper modifying it) since it was last
        read here -- serving a stale result would defeat the purpose of
        the existing-output-column check this same read also performs
        (see below) -- the check must always reflect a fresh read,
        never a cached result. What IS still remembered across calls is only
        WHICH file/table is selected per mode (parcel_local_path /
        parcel_db_table) -- a separate concern, untouched by this
        function.
        """
        nonlocal parcel_is_reading, parcel_read_details, parcel_output_column_conflicts
        if parcel_is_reading:
            # A read is already in flight -- do not start a second,
            # overlapping one. The controls that would trigger this are
            # disabled while reading anyway (_set_parcel_reading_state),
            # but this guard is the actual enforcement.
            return

        if parcel_source_type.get() == "local":
            source_type = "local"
            # Single-selection: build a one-element list from the authority
            # variable, or empty list if nothing selected. The early-return
            # on "if not sources:" below is completely unchanged -- only
            # the list construction changes, not when or whether the
            # refresh fires.
            sources = [parcel_local_path] if parcel_local_path else []
        else:
            source_type = "db"
            sources = [parcel_db_table] if parcel_db_table else []

        if not sources:
            # Nothing selected for this mode -- nothing to show, nothing
            # to read.
            parcel_read_details = []
            parcel_output_column_conflicts = []
            _rebuild_lot_classification_checklist()
            _update_parcel_classification_visibility()
            _update_run_button_state()
            return

        # Always a real background read -- no cache-hit shortcut. Per
        # the "never pass through an empty intermediate state" invariant,
        # the EXISTING checklist (if any) is left completely untouched
        # here -- it stays fully visible throughout the read, and is
        # only ever replaced in one atomic swap once the new data is
        # ready (see _poll_parcel_classification_queue()).
        result_queue = queue.Queue()

        def worker():
            # Inspect every selected source; keep only the lightweight
            # detection tuple per source (state, col_name, kind,
            # existing_output_cols) -- the GeoDataFrame itself is dropped
            # as soon as it's inspected, so this never holds every parcel
            # file in memory at once even for a large batch.
            #
            # existing_output_cols (Priority 2): checked from this SAME
            # already-open gdf, not a second separate read -- both checks
            # need to open the exact same file/table anyway.
            per_source_results = []
            for path_or_table in sources:
                gdf, error = _read_gdf_worker(source_type, path_or_table)
                if error is not None or gdf is None:
                    per_source_results.append((path_or_table, None, None, None, {}))
                    continue
                state, col_name, kind, _mask = _detect_lot_classification(gdf)
                existing_output_cols = detect_existing_output_columns(gdf, OUTPUT_COLUMN_TARGETS)
                per_source_results.append((path_or_table, state, col_name, kind, existing_output_cols))
                del gdf
            result_queue.put(per_source_results)

        deadline = time.time() + 60  # see _poll_parcel_classification_queue()
        parcel_is_reading = True
        _set_parcel_reading_state(True)
        _update_parcel_classification_visibility()
        _update_run_button_state()
        threading.Thread(target=worker, daemon=True).start()
        win.after(100, lambda: _poll_parcel_classification_queue(result_queue, source_type, deadline))

    def _poll_parcel_classification_queue(result_queue, source_type, deadline):
        """
        Runs on the main thread via win.after() polling. Picks up the
        per-source result list placed on the queue by the background
        worker, or detects a timeout if 60 seconds have elapsed with no
        result. Ordering matters: the queue is ALWAYS checked before the
        deadline -- see road_density.py's identical function for the
        full reasoning (single-threaded Tkinter main loop, fresh
        queue.Queue() per call, no generation counter needed).
        """
        nonlocal parcel_is_reading, parcel_read_details, parcel_output_column_conflicts
        if not win.winfo_exists():
            return
        try:
            per_source_results = result_queue.get_nowait()
        except queue.Empty:
            if time.time() >= deadline:
                _handle_parcel_check_failure(source_type, "timeout")
            else:
                win.after(100, lambda: _poll_parcel_classification_queue(
                    result_queue, source_type, deadline))
            return

        # Single-selection: per_source_results has exactly one entry.
        # state is None signals that source's own read failed (see
        # worker()'s error branch above) -- distinct from a successful
        # read that simply found no usable LOT_LOCATION/output column,
        # which has a real (non-None) state.
        if per_source_results and per_source_results[0][1] is None:
            _handle_parcel_check_failure(source_type, "failure")
            return

        parcel_is_reading = False
        _set_parcel_reading_state(False)
        parcel_read_details = per_source_results
        parcel_output_column_conflicts = _check_parcel_frontage_conflicts(per_source_results)

        # Builds one checkbox per source with state == LOT_STATE_FOUND;
        # sources without a usable column are simply omitted (no "not
        # found" line), matching lot_location.py's own
        # auto-hide-when-nothing-usable convention. Always fresh
        # BooleanVars -- this is always a genuinely new read now, never
        # a cache restore.
        _rebuild_lot_classification_checklist()

        _update_parcel_classification_visibility()
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
        authority variable, then clears ONLY road_local_path or
        road_db_table (whichever source_type was actually being read).
        Also clears that mode's _road_gdf_cache slot -- there is no
        valid GeoDataFrame for run_processing() to reuse when the read
        that would have produced it never succeeded.

        CRITICAL: this tool's _set_road_reading_state() only manages
        widget state, not road_is_reading itself (owned directly by
        _refresh_road_classification()/_poll_road_classification_
        queue()). Must be explicitly reset here or every subsequent
        _refresh_road_classification() call would silently no-op (this
        exact gap was found and fixed in road_width.py first).

        _set_road_reading_state(False) is called BEFORE the dialog is
        shown, not after -- same reasoning as
        _handle_parcel_check_failure() above.
        """
        nonlocal road_is_reading

        road_is_reading = False

        if source_type == "local":
            failed_name = (os.path.basename(road_local_path.get())
                           if road_local_path.get() else "the selected file")
            road_local_path.set("")
        else:
            failed_name = road_db_table.get() if road_db_table.get() else "the selected table"
            road_db_table.set("")

        _road_gdf_cache[source_type] = {"key": None, "gdf": None}
        road_type_value_vars.clear()
        _rebuild_road_type_checklist()
        filter_road_type_var.set(False)
        _update_road_classification_visibility()

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

    def _refresh_road_classification():
        """
        Background-reads the currently selected Road Network source (a
        single file/table -- Road Network, unlike Land Parcel, only ever
        supports one selection). Populates road_type_value_vars for the
        Filter by Road Type checklist. Gives up after 60 seconds with no
        result (see _poll_road_classification_queue()) -- a hung read
        must not leave the tool waiting indefinitely.

        Deliberately does NOT skip this read based on any prior cached
        state -- every call, whether triggered by a fresh Browse
        selection or by toggling Local <-> Database, always performs a
        real background read. This extends the same reasoning already
        applied to Land Parcel's existing-output-column detection to
        Road Network's road-type
        filter too: a cache keyed only on "which file/table was
        selected" cannot detect that the file/table's CONTENTS changed
        externally since it was last read here.

        After a successful read, the result IS still written into
        _road_gdf_cache (see _poll_road_classification_queue() below) --
        but only so run_processing() can reuse this same, now-guaranteed-
        fresh GeoDataFrame at Run time instead of reading the source a
        second time; that cache entry is never itself consulted to skip
        a read here. See _road_gdf_cache's own module-level comment for
        the full scope of what it's still used for.
        """
        nonlocal road_is_reading
        if road_is_reading:
            # A read is already in flight -- do not start a second,
            # overlapping one (controls are disabled while reading, but
            # this guard is the actual enforcement).
            return

        source_type = road_source_type.get()
        path_or_table = road_local_path.get() if source_type == "local" else road_db_table.get()

        if not path_or_table:
            # Nothing selected for this mode -- nothing to show, nothing
            # to read. No background read is involved, so this is an
            # immediate, synchronous swap to an empty checklist -- not
            # the "reading" transient state at all. The OTHER mode's
            # cache slot is left untouched.
            road_type_value_vars.clear()
            _rebuild_road_type_checklist()
            filter_road_type_var.set(False)
            _update_road_classification_visibility()
            _update_run_button_state()
            return

        # Always a real background read -- no cache-hit shortcut. Per
        # the "never pass through an empty intermediate state" invariant,
        # the EXISTING checkbox/checklist (if any) is left completely
        # untouched here -- it stays fully visible throughout the read,
        # and is only ever replaced in one atomic swap once the new data
        # is ready (see _poll_road_classification_queue()). This
        # includes filter_road_type_var itself -- its reset to False for
        # the new file happens there too, not here, so the OLD file's
        # checked state doesn't visibly flicker off mid-read.
        source_key = (source_type, path_or_table)
        result_queue = queue.Queue()

        def worker():
            gdf, error = _read_gdf_worker(source_type, path_or_table)
            result_queue.put((gdf, error))

        deadline = time.time() + 60  # see _poll_road_classification_queue()
        road_is_reading = True
        _set_road_reading_state(True)
        _update_road_classification_visibility()
        _update_run_button_state()
        threading.Thread(target=worker, daemon=True).start()
        win.after(100, lambda: _poll_road_classification_queue(result_queue, source_key, deadline))

    def _poll_road_classification_queue(result_queue, source_key, deadline):
        """
        Ordering matters: the queue is ALWAYS checked before the
        deadline -- see road_density.py's identical function for the
        full reasoning (single-threaded Tkinter main loop, fresh
        queue.Queue() per call, no generation counter needed).
        """
        nonlocal road_is_reading
        if not win.winfo_exists():
            return

        source_type, path_or_table = source_key

        try:
            gdf, error = result_queue.get_nowait()
        except queue.Empty:
            if time.time() >= deadline:
                _handle_road_check_failure(source_type, "timeout")
            else:
                win.after(100, lambda: _poll_road_classification_queue(
                    result_queue, source_key, deadline))
            return

        road_is_reading = False
        _set_road_reading_state(False)
        if error is not None or gdf is None:
            _handle_road_check_failure(source_type, "failure")
            return

        col = _detect_road_type_column(gdf)
        new_value_vars = {}
        if col:
            # Three distinct data states, never merged into one bucket --
            # same NULL/empty-string/literal-value handling as
            # lot_location.py's road-type checklist.
            counts = {}
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
                for label in sorted(counts.keys()):
                    real_value, count = counts[label]
                    display_text = f"{label} ({count})"
                    new_value_vars[display_text] = (
                        real_value, tk.BooleanVar(master=win, value=True)
                    )
            # else: only one distinct value (or entirely NULL/empty) --
            # nothing meaningful to filter on; checkbox stays hidden.
        # else: no ROAD_TYPE-like column found -- checkbox stays hidden.

        # This slot now holds the definitive, freshly-read GeoDataFrame
        # for this mode -- kept ONLY for run_processing()'s Run-time
        # reuse (see _road_gdf_cache's module-level comment); the other
        # mode's slot is completely untouched.
        _road_gdf_cache[source_type] = {"key": path_or_table, "gdf": _force_2d(gdf)}
        road_type_value_vars.clear()
        road_type_value_vars.update(new_value_vars)
        _rebuild_road_type_checklist()
        # Reset here (not in _refresh_road_classification(), before the
        # read started) -- committing this atomically alongside the new
        # checklist swap means the OLD file's checked state stays fully
        # intact and visible for the entire duration of the read, only
        # changing at the exact moment the new checklist replaces it.
        filter_road_type_var.set(False)

        _update_road_classification_visibility()
        _update_run_button_state()

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

    # Per-source classification checklist -- one Checkbutton per selected
    # Land Parcel source that has a usable LOT_LOCATION/LOT_LABEL column,
    # built fresh by _rebuild_lot_classification_checklist() after each
    # background read.
    #
    # Content-adaptive height, capped, scrollable when needed (Canvas +
    # Scrollbar): the box sizes itself to fit however many checkboxes
    # are actually present, up to LOT_CLASSIFICATION_MAX_HEIGHT -- past
    # that cap, it stops growing and scrolls internally instead. Ported
    # from road_width.py's validated pattern: an earlier version used a
    # plain Frame with no height cap, which grew unbounded with content
    # and resized the window twice per read cycle (once entering the
    # "reading" state, once for the final content) -- confirmed to make
    # a visual distortion bug worse. This version hides the box ENTIRELY
    # when there's nothing to show (0 checkboxes) instead of an always-
    # visible, pointlessly-scrollable empty box, and resizes it (once,
    # cleanly -- see _resize_lot_classification_box()) only when its
    # content actually changes.
    LOT_CLASSIFICATION_MAX_HEIGHT = 90  # pixels -- cap; box grows to fit content up to this, then scrolls

    lot_classification_outer = tk.Frame(parcel_frame)
    lot_classification_canvas = tk.Canvas(
        lot_classification_outer, highlightthickness=0, bd=0)
    lot_classification_scrollbar = tk.Scrollbar(
        lot_classification_outer, orient="vertical",
        command=lot_classification_canvas.yview)
    lot_classification_canvas.configure(yscrollcommand=lot_classification_scrollbar.set)
    lot_classification_canvas.pack(side="left", fill="both", expand=True)
    # lot_classification_scrollbar is packed/unpacked dynamically by
    # _resize_lot_classification_box() below -- only shown when content
    # actually exceeds the cap and scrolling is genuinely needed.

    # lot_classification_list_container: the actual content frame drawn
    # INSIDE the canvas -- this is what _rebuild_lot_classification_checklist()
    # clears and repopulates.
    lot_classification_list_container = tk.Frame(lot_classification_canvas)
    _lot_classification_canvas_window = lot_classification_canvas.create_window(
        (0, 0), window=lot_classification_list_container, anchor="nw")

    def _on_lot_classification_content_configure(_event=None):
        lot_classification_canvas.configure(
            scrollregion=lot_classification_canvas.bbox("all"))
    lot_classification_list_container.bind(
        "<Configure>", _on_lot_classification_content_configure)

    def _on_lot_classification_canvas_resize(event):
        # Keep the inner frame's width matched to the canvas's own width
        # so checkboxes wrap/align correctly and only VERTICAL scrolling
        # is ever needed.
        lot_classification_canvas.itemconfig(_lot_classification_canvas_window, width=event.width)
    lot_classification_canvas.bind("<Configure>", _on_lot_classification_canvas_resize)

    def _on_lot_classification_mousewheel(event):
        lot_classification_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    lot_classification_canvas.bind(
        "<Enter>", lambda e: lot_classification_canvas.bind_all(
            "<MouseWheel>", _on_lot_classification_mousewheel))
    lot_classification_canvas.bind(
        "<Leave>", lambda e: lot_classification_canvas.unbind_all("<MouseWheel>"))

    def _resize_lot_classification_box():
        """
        Recomputes lot_classification_canvas's own height to fit
        lot_classification_list_container's CURRENT content, capped at
        LOT_CLASSIFICATION_MAX_HEIGHT. Shows the scrollbar only when the
        content genuinely exceeds the cap (nothing to scroll -> no
        scrollbar shown at all, avoiding a pointless, always-visible
        scrollbar next to a box that never needs it). Called once per
        content change (a state transition -- reading finished, checklist
        rebuilt) -- never in a tight loop.
        """
        lot_classification_list_container.update_idletasks()
        content_height = lot_classification_list_container.winfo_reqheight()
        if content_height <= LOT_CLASSIFICATION_MAX_HEIGHT:
            lot_classification_canvas.configure(height=content_height)
            lot_classification_scrollbar.pack_forget()
        else:
            lot_classification_canvas.configure(height=LOT_CLASSIFICATION_MAX_HEIGHT)
            lot_classification_scrollbar.pack(side="right", fill="y")
    # Starts unpacked; _update_parcel_classification_visibility() (via
    # _refresh_parcel_classification()) decides what to show.

    def browse_parcel_files():
        file = filedialog.askopenfilename(filetypes=[
            ("Shapefiles", "*.shp"), ("GeoPackage", "*.gpkg"), ("All", "*.*")])
        # Cancel returns "" -- do not assign, preserving previous selection.
        if file:
            nonlocal parcel_local_path
            parcel_local_path = file
            parcel_files_var.set(os.path.basename(file))
            # A new Land Parcel selection invalidates any prior
            # LOT_LOCATION/LOT_LABEL detection -- re-inspect the (new)
            # selected file. Canceling the dialog (the `if file:`
            # Always checks fresh -- see _refresh_parcel_classification()
            # docstring: no result is ever cached across calls.
            #
            # No manual reflow/freeze needed here: under the swap-based
            # checklist lifecycle, the OLD checklist (if any) simply
            # stays fully visible and untouched throughout the read that
            # _refresh_parcel_classification() is about to start -- there
            # is no intermediate "cleared" state to protect against. The
            # window only ever resizes once, at the very end, when the
            # new checklist actually replaces the old one.
            _refresh_parcel_classification()

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
                # No _reflow_window() here -- same reasoning as
                # browse_parcel_files() above. Always checks fresh.
                _refresh_parcel_classification()

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
        # remembered selection -- that's pre-existing behavior, left
        # untouched. Always re-checks fresh for whichever mode is now
        # active -- no cached result is ever restored.
        _refresh_parcel_classification()

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

    # road_type_header_row: shared row holding "Filter by Road Type"
    # (left) AND the "Check All" / "Uncheck All" hyperlink-style links
    # (right, grouped in road_type_links_frame) -- no extra row, no
    # extra vertical space. Its own pack()/pack_forget() is owned
    # entirely by _update_road_classification_visibility() below (same
    # single packing authority the checkbox alone used to be packed
    # by directly -- see that function's own docstring). road_type_
    # links_frame is NOT gated by this row's own visibility alone --
    # it has its own independent, narrower pack/pack_forget (checked
    # AND has content -- the exact same condition the checklist itself
    # uses), since "Check All"/"Uncheck All" are only meaningful once
    # the checklist they operate on is actually visible. Ported from
    # lot_location.py's own version of this same fix, re-verified
    # against this file's own widget tree first: no separate
    # road_filter_frame wrapper exists here -- road_type_header_row is
    # a direct child of road_frame, same as road_type_checklist_outer
    # itself, matching how road_filter_checkbox alone used to be a
    # direct child of road_frame before this fix.
    road_type_header_row = tk.Frame(road_frame)

    road_filter_checkbox = tk.Checkbutton(
        road_type_header_row, text="Filter by Road Type", variable=filter_road_type_var)
    road_filter_checkbox.pack(side="left")

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
    # lot_location.py's own ROAD_TYPE_MAX_ITEMS_BEFORE_VSCROLL threshold
    # (itself matching meters_from_school_shop_transport_church.py's
    # OTHER_LANDMARKS_MAX_ITEMS_BEFORE_VSCROLL), for consistency across
    # tools. A horizontal scrollbar appears only when a label is wider
    # than the box -- never truncated or wrapped, only ever scrolled
    # into view.
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
    # successful read (see _rebuild_road_type_checklist()). Only packed
    # while road_filter_checkbox is checked AND a usable ROAD_TYPE-like
    # column was found (see _update_road_classification_visibility()).
    # road_type_checklist_container (the actual Checkbutton parent) lives
    # INSIDE road_type_checklist_canvas, which itself lives inside
    # road_type_checklist_outer alongside the vertical/horizontal
    # scrollbars -- road_type_checklist_outer is what
    # _update_road_classification_visibility() actually packs/
    # pack_forgets now; road_type_checklist_container's own pack()/
    # pack_forget() calls from before this fix are gone, since its
    # visibility is entirely governed by its parent outer frame's
    # visibility. Unlike lot_location.py, this file has no separate
    # road_filter_frame wrapper -- road_type_checklist_outer is a direct
    # child of road_frame, same as road_filter_checkbox itself (re-
    # verified against this file's own widget tree, not assumed to match
    # lot_location.py's shape).
    road_type_checklist_outer = tk.Frame(road_frame)
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
    # _resize_road_type_checklist_box() -- only shown when content
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

    # All three (road_type_header_row -- which now holds road_filter_
    # checkbox and road_type_links_frame -- road_type_checklist_outer,
    # and by extension road_type_checklist_container inside it) start
    # unpacked; _update_road_classification_visibility() (via
    # _refresh_road_classification()) decides what to show.

    def browse_road_file():
        f = filedialog.askopenfilename(filetypes=[
            ("Shapefiles", "*.shp"), ("GeoPackage", "*.gpkg"), ("All", "*.*")])
        if f:
            road_local_path.set(f)
            road_file_var.set(os.path.basename(f))
            # A new Road Network selection invalidates any prior Road
            # Type detection -- re-inspect the new road layer. Always
            # checks fresh -- see _refresh_road_classification()
            # docstring: no read is ever skipped based on prior state.
            #
            # No manual reflow/freeze needed here -- same reasoning as
            # browse_parcel_files(): under the swap-based checklist
            # lifecycle, the OLD checkbox/checklist (if any) simply stays
            # fully visible and untouched throughout the read that
            # _refresh_road_classification() is about to start.
            _refresh_road_classification()

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
                # No _reflow_window() here -- same reasoning as
                # browse_road_file() above. Always checks fresh.
                _refresh_road_classification()

        _pick_db_tables(win, tables, multi=False, on_select=_on_road_table_selected)

    def _toggle_road():
        if road_source_type.get() == "local":
            road_lbl.config(textvariable=road_file_var)
            road_btn.config(text="Browse…", command=browse_road_file)
        else:
            road_lbl.config(textvariable=road_db_var)
            road_btn.config(text="Select…", command=browse_road_db)
        # Switching Local <-> Database does NOT clear the other mode's
        # remembered selection -- that's pre-existing behavior, left
        # untouched. Always re-checks fresh for whichever mode is now
        # active -- no cached checklist state is ever restored.
        _refresh_road_classification()

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
            _reflow_window()
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

    out_btn.config(command=browse_output_dir)

    # ── RUN BUTTON ───────────────────────────────────────────────
    ttk.Separator(win, orient="horizontal").pack(
        fill="x", padx=10, pady=(12, 4))

    def on_run():
        global barangay_source, road_source, output_mode, parcel_classification_selection, filter_by_road_type_active, road_type_excluded_values, overwrite_mode, parcel_output_column_overrides

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

        if output_dest_type.get() == "local":
            if not output_local_dir.get():
                messagebox.showerror("Missing Input",
                    "Please select an output folder.")
                return
            output_mode = ("local", output_local_dir.get())
        else:
            output_mode = ("db", None)

        # Road Classification: resolved mode + excluded values are read
        # here and stored as module globals, same pattern as
        # barangay_source / road_source / output_mode above --
        # run_processing() (and, per source, resolve_classification())
        # consumes them from there. excluded_road_types is only ever
        # populated when the user is explicitly in "Filter by Road Type"
        # mode; Automatic mode always stores [] regardless of any stale
        # checklist state, since Automatic mode never consults it (see
        # resolve_classification()).
        #
        # Belt-and-suspenders: the Run button is disabled while either
        # background read is in progress (_update_run_button_state()), so
        # this branch should be unreachable in normal use -- kept as a
        # hard stop in case on_run() is ever invoked some other way (e.g.
        # a future keyboard shortcut) while a read is still running.
        if parcel_is_reading or road_is_reading:
            messagebox.showwarning(
                "Please Wait",
                "Still reading the selected source(s) for Road Classification. "
                "Please wait for the status line to finish updating before running."
            )
            return

        parcel_classification_selection = {
            path_or_table: var.get() for path_or_table, var in parcel_classification_vars.items()
        }
        filter_by_road_type_active = filter_road_type_var.get()
        if filter_by_road_type_active:
            road_type_excluded_values = [
                real_value for display_text, (real_value, var) in road_type_value_vars.items()
                if not var.get()
            ]
        else:
            road_type_excluded_values = []

        # ------------------------------------------------------------------
        # PRIORITY 1: existing OUTPUT-COLUMN conflict warning. Checks all
        # three output columns (ROAD_FRONTAGE, DEPTH, DEPTH_WIDTH_RATIO) --
        # not just ROAD_FRONTAGE -- per project-lead decision: they are one
        # feature set computed together, so a conflict on ANY of them
        # warrants one combined warning covering all affected sources and
        # columns, shown once here (never per-file mid-processing, never
        # only at Browse time). Declining cancels the run entirely rather
        # than skipping just the affected source(s). Column names are
        # shown with their EXACT existing casing (e.g. "dePTH"), and that
        # exact casing/name is what process_frontage_single() will write
        # into later -- never renamed to the standard casing.
        # ------------------------------------------------------------------
        if parcel_output_column_conflicts:
            lines = "\n\n".join(
                f"'{os.path.basename(path_or_table)}' already has the following column(s):\n"
                + "\n".join(f"  • {existing_name}" for existing_name in existing_output_cols.values())
                for path_or_table, existing_output_cols in parcel_output_column_conflicts
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
            # Preserve each source's existing column name(s)/casing exactly
            # -- e.g. a detected "dePTH" is written back to "dePTH", not a
            # hardcoded "DEPTH" -- so no duplicate column is ever created
            # regardless of the existing casing. A source with no entry
            # here (no conflict was found) simply uses the default names
            # in process_frontage_single() below.
            parcel_output_column_overrides = dict(parcel_output_column_conflicts)
        else:
            parcel_output_column_overrides = {}

        # ------------------------------------------------------------------
        # PRIORITY 2: output FILENAME conflict pre-scan. Resolved ONCE, up
        # front, here on the main thread -- BEFORE the window is destroyed
        # and BEFORE run_processing()'s background worker starts (Tkinter
        # dialogs must never be shown from a worker thread). Desired names
        # only need each source's own filename/table name (no need to
        # actually read/measure anything yet), so this check is cheap.
        # Ported from road_width.py's validated pattern -- see
        # ask_overwrite_dialog()'s docstring for the full behavior.
        # Matches road_width.py's own convention exactly: the main output
        # reuses the parcel source's own name directly, no tool-name
        # suffix appended (confirmed/changed by the project lead -- the
        # "_road_frontage" suffix that used to be appended here is gone).
        # This tool now writes exactly one output file per source -- the
        # previously-paired QA layers (and the with_output_suffix()
        # helper that derived their filenames) have been removed
        # entirely -- see the module docstring and process_frontage_
        # single()'s own docstring.
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

        # ------------------------------------------------------------------
        # PRIORITY 3: DB-output destination table resolution — mirrors
        # PRIORITY 2 above. Resolved here on the main thread, before
        # win.destroy(), so confirm_db_overwrite_dialog() /
        # choose_db_overwrite_dialog() (invoked inside
        # resolve_db_output_table()) still have a live parent window, and
        # a Cancel here leaves the fully-configured win intact instead of
        # forcing a from-scratch reopen. Previously this resolution
        # happened inside run_processing(), which is only ever invoked
        # AFTER win.destroy() -- see Fix 1 root cause. resolve_db_output_
        # table()'s own matching/decision logic is untouched; only the
        # call site moved here.
        #
        # resolved_table_name is passed into run_processing() as a
        # parameter -- same approach already used in lot_location.py,
        # road_surface.py, road_density.py, and land_shape_compactness.py.
        # overwrite_mode above is left exactly as-is (module-level global,
        # unique to this file) -- not refactored to match. resolved_outcome
        # IS threaded through as of this task (D-Cancel): unlike road_
        # surface.py/road_density.py/land_shape_compactness.py, this file
        # now has a _write_db_output_safely()-style write (see the "DB
        # ATOMIC WRITE / CANCEL-SAFE STAGING" section above), which needs
        # to know whether FINAL_SWAP requires a rename-existing-aside-to-
        # backup step -- so both halves of resolve_db_output_table()'s
        # return value are kept and passed into run_processing() below.
        # resolve_db_output_table()'s own matching logic also now runs
        # the crash-orphan-recovery scan first (see that function's own
        # docstring) -- creds is loaded here regardless (already was, to
        # determine _resolve_schema) and passed straight through.
        # ------------------------------------------------------------------
        resolved_table_name = None
        resolved_outcome = None
        if output_mode[0] == "db":
            _resolve_creds = load_db_credentials()
            if not _resolve_creds:
                messagebox.showerror("Error", "Missing pg_credentials.json")
                return
            _resolve_schema = _resolve_creds["schema"]
            resolved_table_name, resolved_outcome = resolve_db_output_table(
                win, _resolve_schema, barangay_source, _resolve_creds
            )
            if resolved_table_name is None:
                print("Run cancelled by user (database output table not confirmed).")
                return

        win.destroy()
        if _app_root is None:
            messagebox.showerror("Error", "No root window available. Please restart the tool.")
            return
        run_processing(_app_root, resolved_table_name, resolved_outcome)

    run_btn = tk.Button(win, text="▶  Run Processing", command=on_run,
              bg="#2e7d32", fg="white",
              font=("Segoe UI", 10, "bold"),
              relief="flat", padx=16, pady=6)
    run_btn.pack(pady=(4, 4))

    # Permanent status line UNDER the Run button -- always visible, no
    # hover required. Matches lot_location.py exactly (position below the
    # button, not italic) -- confirmed against the real file rather than
    # assumed; road_frontage.py originally had this above the button and
    # italic, an inconsistency with no deliberate reason behind it.
    run_status_lbl = tk.Label(win, textvariable=run_status_var,
                              font=("Segoe UI", 8), fg="gray")
    run_status_lbl.pack(pady=(0, 12))

    # set initial button commands to match default radio state
    _toggle_parcel()
    _toggle_road()
    _toggle_output()
    _update_parcel_classification_visibility()
    _update_road_classification_visibility()
    _update_run_button_state()



# ========================================
# MAIN / ENTRYPOINT
# ========================================
def main(parent=None):
    """
    Tool entry point. If parent is given (invoked from within another
    running Tk app), reuses it as _app_root and just opens this tool's
    window. Otherwise creates and hides a new Tk root, sets it as
    _app_root, applies this tool's icon, and enters its own mainloop --
    the standalone-subprocess dispatch path.

    Args:
        parent: an existing Tk root to reuse, or None to create one.
    """
    global _app_root
    if parent is not None:
        _app_root = parent
        open_main_window(parent)
    else:
        root = tk.Tk()
        _app_root = root
        apply_icon(root, "roadfrontage.ico")
        root.withdraw()
        open_main_window(root)
        root.mainloop()


if __name__ == "__main__":
    main()