import os
import re
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox, ttk, StringVar
import geopandas as gpd
import pandas as pd
import numpy as np
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import unary_union, nearest_points, linemerge
from shapely.strtree import STRtree
import math
import subprocess
import json
from sqlalchemy import create_engine, inspect, text
from shapely.validation import make_valid
from shapely.geometry import box
import psycopg2

from utils.table_name_matching import normalize_name, find_matching_tables

# =========================
# GeoPandas compatibility shim
# =========================
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

    # Windows taskbar / Alt-Tab icon
    if os.path.exists(ico):
        try:
            win.iconbitmap(ico)
        except Exception:
            pass

    # Titlebar icon fallback (critical for Tk)
    if os.path.exists(png):
        try:
            img = tk.PhotoImage(file=png)
            win.iconphoto(True, img)
            win._icon_ref = img  # prevent garbage collection
        except Exception:
            pass


# ========================= CONFIG =========================
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"
CREDENTIALS_FILE = "pg_credentials.json"

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

# emit_buffer_qa: opt-in flag for the buffer diagnostic QA layer (see
# process_frontage_single()'s docstring). Off by default -- set by
# open_main_window()'s on_run() from a GUI checkbox, read by
# run_processing(). A separate, unrelated concern from Road
# Classification -- purely a visual-debugging aid for the frontage
# measurement algorithm itself.
emit_buffer_qa = False

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

# _road_gdf_cache: TRUE dual-slot cache -- one independent slot for
# "local" and one for "db", so switching the Road Network Source radio
# back and forth between Local File and Database Table never re-reads a
# selection that's still valid, and never mixes up which selection
# belongs to which mode. Each slot holds:
#   "key"       : the exact path_or_table string this slot's data is for
#                 (None if nothing has been read for that mode yet)
#   "gdf"       : the actual read GeoDataFrame (safe to hold in full --
#                 Road Network only ever has ONE selected source at a
#                 time, unlike Land Parcel which can have many)
#   "value_vars": the built Road Type checklist's {display_text:
#                 (real_value, tk.BooleanVar)} dict, INCLUDING each
#                 BooleanVar's current checked state -- so toggling away
#                 and back restores not just the list of values but
#                 exactly which ones were checked/unchecked.
# A slot is only ever replaced by a fresh read of that SAME mode; the
# other mode's slot is untouched, which is what makes the two "isolated"
# from each other.
_road_gdf_cache = {
    "local": {"key": None, "gdf": None, "value_vars": {}, "filter_active": False},
    "db": {"key": None, "gdf": None, "value_vars": {}, "filter_active": False},
}

# _parcel_classification_cache: same dual-slot idea, adapted for Land
# Parcel Source. Deliberately does NOT cache the GeoDataFrames themselves
# (unlike _road_gdf_cache above) -- Land Parcel can have MANY selected
# sources at once, and holding every one of their full GeoDataFrames in
# memory just to make radio-toggling instant would be a real memory cost
# for a large batch. Instead, each slot caches only the lightweight
# per-source DETECTION RESULTS (tiny tuples: path/table, state, column
# name, kind -- see _detect_lot_classification()) plus the per-source
# checkbox BooleanVars (also tiny), keyed by the exact tuple of currently
# selected sources for that mode. Toggling back to a selection that
# hasn't changed restores the checklist instantly with no re-read; any
# change to which files/tables are selected invalidates that slot's key
# and forces a fresh read, exactly like the road side.
_parcel_classification_cache = {
    "local": {"key": None, "details": None, "vars": {}},
    "db": {"key": None, "details": None, "vars": {}},
}


# ========================= CRS UTILITY =========================
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
from shapely.prepared import prep


# ========================= MINIMUM FRONTAGE THRESHOLD =========================
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


# ========================= FRONTAGE BUFFER TOLERANCE (EXPERIMENT) =========================
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
# feature's history). Use the buffer diagnostic QA layer (the
# "Generate buffer diagnostic layer (Visual Measurement)" checkbox) to
# visually inspect the effect of this change on real parcel data.
FRONTAGE_BUFFER_TOLERANCE = 9


# ========================= PARALLEL-VALIDATION ALGORITHM =========================
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


# ========================= TWO-STAGE GATE+MEASURE (EXPERIMENT, NOT VALIDATED) =========================
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
# Use the buffer diagnostic QA layer to visually inspect Stage 2's buffer
# zones (TWO_STAGE_MEASURE_TOLERANCE) on real parcel data -- when this
# experiment is active, the diagnostic layer shows Stage 2's buffers only,
# not Stage 1's (Stage 1 is a pass/fail gate, not a measurement, so there
# is nothing meaningful to visualize for it).
TWO_STAGE_FRONTAGE_ENABLED = False
TWO_STAGE_GATE_TOLERANCE = 5
TWO_STAGE_MEASURE_TOLERANCE = 9


# ========================= CROSS-PARCEL CONFLICT RESOLUTION =========================
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


# ========================= ROAD TYPE FILTER UTILITIES =========================
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


# ========================= LOT CLASSIFICATION UTILITIES =========================
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


# ========================= EXISTING OUTPUT-COLUMN CONFLICT DETECTION =========================
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


def _detect_existing_output_columns(gdf):
    """
    Checks a parcel GeoDataFrame for pre-existing columns matching any of
    OUTPUT_COLUMN_TARGETS (ROAD_FRONTAGE, DEPTH, DEPTH_WIDTH_RATIO),
    exact match (case-insensitive) -- "road_frontage" matches
    "ROAD_FRONTAGE", but a column like "ROAD_FRONTAGE_OLD" or
    "ROAD_TYPE" does NOT match (no substring/partial matching).

    Returns a dict {target_name: actual_existing_column_name}, containing
    ONLY the targets that actually have a match -- e.g. {"DEPTH": "dePTH"}
    if only a differently-cased DEPTH column exists and the other two
    targets have no match at all. Empty dict if none of the three targets
    have any existing column. The actual column's ORIGINAL casing is
    preserved in the returned value (e.g. "dePTH", not "DEPTH") -- this
    is what gets shown to the user in the confirmation dialog and what
    process_frontage_single() writes back into, so an existing
    differently-cased column is reused exactly as found rather than
    renamed or duplicated.
    """
    found = {}
    for target in OUTPUT_COLUMN_TARGETS:
        match = next((c for c in gdf.columns if c.lower() == target.lower()), None)
        if match is not None:
            found[target] = match
    return found


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


def _all_linestrings(geom):
    """Return EVERY LineString piece inside LineString/MultiLineString/
    GeometryCollection as a flat list — used to capture all road-adjacent
    boundary segments (e.g. both sides of a corner lot) for the QA layer,
    matching the arc-sum ROAD_FRONT total rather than just the longest piece."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type == "MultiLineString":
        return list(geom.geoms)
    if geom.geom_type == "GeometryCollection":
        out = []
        for g in geom.geoms:
            out.extend(_all_linestrings(g))
        return out
    return []


# ========================= OUTPUT FILENAME CONFLICT HANDLING =========================
# Ported directly from road_width.py's canonical pattern (see that file's
# resolve_output_base_name() / with_qa_suffix() for the original,
# validated implementation this is copied from). Business decisions
# confirmed by the project lead before porting:
#   - QA outputs (frontage_lines, segment_buffers) always inherit the
#     MAIN output's resolved base name/number -- they never scan the
#     folder or resolve their own numbering independently. Filename
#     resolution happens exactly ONCE per run, for the main output only.
#   - "Create New File" resolves the number ONCE; every auxiliary output
#     for that same source automatically follows the same resolved name
#     (e.g. main "parcel_5.gpkg" -> "parcel_5_frontage_lines.gpkg",
#     "parcel_5_segment_buffers.gpkg" -- never "parcel_6_..." or any
#     other independently-resolved number).
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

    This function decides the number ONCE, for the MAIN output only.
    Every other output belonging to the same processing run (the QA
    layers) must reuse this exact returned name as its own base -- see
    with_output_suffix() below -- never re-run this scan independently.
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


def with_output_suffix(main_base_name: str, suffix: str) -> str:
    """
    Derives a QA output layer's base name from the ALREADY-FINALIZED main
    output base name (see resolve_output_base_name()) -- never scans the
    folder independently for its own numbering, so every output for a
    given source stays paired: "landparcel.gpkg" + "landparcel_frontage_
    lines.gpkg" + "landparcel_segment_buffers.gpkg", or
    "landparcel_5.gpkg" + "landparcel_5_frontage_lines.gpkg" +
    "landparcel_5_segment_buffers.gpkg", etc. Main output is always the
    source of truth for the number; every QA layer just follows it.

    suffix: "frontage_lines" or "segment_buffers" (road_frontage.py has
    TWO possible QA outputs, unlike road_width.py's single "_VM" layer --
    same underlying principle, just parameterized instead of hardcoded
    to one fixed suffix).
    """
    return f"{main_base_name}_{suffix}"


# ========================= GPKG OVERWRITE SAFETY =========================
def _write_gpkg(gdf, path):
    """
    Writes a GeoDataFrame to a .gpkg file, atomically.

    Why atomicity is necessary here specifically: the previous version
    of this function deleted any pre-existing file at `path` FIRST,
    then wrote the new content -- necessary because GeoPackage is a
    SQLite-based container that can hold multiple named layers, and
    calling gdf.to_file(path, driver="GPKG") when `path` already exists
    does NOT simply replace its contents; pyogrio/GDAL tries to create
    a new layer inside the existing file and fails with "Layer <name>
    already exists, CreateLayer failed" if a layer of that name is
    already there (confirmed reproduced when a user chose "Overwrite"
    in ask_overwrite_dialog() -- crashed the whole run with no success
    dialog and no clear message, just a console traceback invisible in
    the compiled EXE).

    But delete-then-write has its own, worse failure mode: if anything
    interrupts the process between the delete and the write completing
    (a crash, the machine losing power, disk full mid-write), the
    result isn't a corrupted file -- there is NO FILE AT ALL at `path`
    anymore, having deleted the original with nothing to show for it.

    This version instead writes to a temporary file first, VERIFIES
    that file is actually readable back (a write that raised no
    exception but produced something GDAL itself can't re-open is
    exactly the failure this guards against), and only then atomically
    replaces the destination via os.replace() -- which is atomic on
    the same filesystem on both Windows and POSIX, unlike
    os.remove()+os.rename(): there is no window where `path` doesn't
    exist. If ANY step before the final os.replace() fails, `path` is
    left completely untouched, exactly as if this call never happened.
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


def open_in_global_mapper(output_path):
    if os.path.exists(GM_EXE_PATH) and os.path.exists(output_path):
        subprocess.Popen([GM_EXE_PATH, output_path], shell=True)


def split_boundary_to_segments(boundary):
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
    centroid = parcel_geom.centroid
    min_distance = float("inf")
    for road in road_gdf.geometry:
        p1, p2 = nearest_points(centroid, road)
        dist = p1.distance(p2)
        if dist < min_distance:
            min_distance = dist
    return min_distance


def calculate_depth_perpendicular(parcel_geom, road_buffer, max_depth=1000):
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


# ========================= DB HELPERS =========================
def load_db_credentials():
    try:
        with open(CREDENTIALS_FILE, "r") as f:
            return json.load(f)
    except:
        return None


def get_geometry_column(table_name, engine, schema):
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
    geom_col = get_geometry_column(table, engine, schema)
    insp = inspect(engine)
    cols = [c['name'] for c in insp.get_columns(table, schema=schema) if c['name'] != geom_col]
    col_str = ", ".join([f'"{c}"' for c in cols]) if cols else ""
    if col_str:
        query = f'SELECT {col_str}, "{geom_col}" AS geometry FROM "{schema}"."{table}"'
    else:
        query = f'SELECT "{geom_col}" AS geometry FROM "{schema}"."{table}"'
    return gpd.read_postgis(query, engine, geom_col="geometry")


def fetch_tables(schema):
    creds = load_db_credentials()
    if not creds:
        return []
    try:
        conn = psycopg2.connect(
            host=creds["host"],
            port=creds["port"],
            dbname=creds["database"],
            user=creds["username"],
            password=creds["password"]
        )
        cur = conn.cursor()
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s ORDER BY table_name;", (schema,))
        tables = [row[0] for row in cur.fetchall()]
        conn.close()
        return tables
    except:
        return []


# ========================= PROGRESS WINDOW =========================
# ============================================================
# Progress Event Protocol v9 — Phase 4 migration (road_frontage.py)
# ============================================================
# PresentationState, the Presentation Policy, and the Tkinter View are
# no longer defined locally in this file -- identical to
# lot_location.py's Phase 3 migration, both tools now import the same
# three classes from tools/progress_framework.py instead of each
# keeping its own copy. Pure extraction: no behavior change, no new
# abstraction, no wrapper/adapter/compatibility layer.
#
#   Worker (worker(), inside run_processing())      -> unchanged
#   Main-thread Message Handler (poll_queue())       -> unchanged
#   ProgressWindow                                   -> owns window/widgets
#
# road_width.py is not part of this migration and is not touched by it
# -- see progress_framework.py's own top-of-file comment for why.
# ============================================================

from tools.progress_framework import (
    PresentationState,
    ProgressPresentationPolicy,
    TkinterProgressView,
)


class ProgressWindow:
    """
    Progress dialog shown while run_processing() works on a background
    thread. Progress Event Protocol v9 role: host, not decision-maker
    (see ProgressPresentationPolicy / TkinterProgressView, imported
    from progress_framework.py, shared with lot_location.py).
    Public interface (__init__, update, close) is byte-identical to
    before this migration; poll_queue() requires no changes.
    """
    def __init__(self, root, title="Processing"):
        self.win = tk.Toplevel(root)
        apply_icon(self.win)
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
        # render into already exist.
        self._policy = ProgressPresentationPolicy()
        self._view = TkinterProgressView(self.win, self.status_var, self.progress)

    def update(self, message, value=None, maximum=None):
        state = self._policy.compute(message, value, maximum)
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



# ========================= FRONTAGE PROCESSING =========================
def process_frontage_single(brgy_gdf, road_gdf, source_name="", progress=None, classification=None,
                             emit_buffer_qa=False,
                             road_frontage_col="CAMA_ROAD_FRONTAGE", depth_col="CAMA_DEPTH",
                             dwr_col="CAMA_DEPTH_WIDTH_RATIO"):
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
    # emit_buffer_qa: when True, collects the exact seg.buffer(tol,
    # cap_style=2) zone used by _edge_covered_portion() for every boundary
    # segment that registered ANY coverage (i.e. contributed to
    # frontage_total) -- purely a diagnostic aid to let the user visually
    # inspect whether a given segment's buffer genuinely reaches a road
    # running alongside it, or is bleeding across a thin parcel / grazing
    # a crossing road at an angle. Does NOT change any measurement --
    # _edge_covered_portion() itself is untouched; this only re-derives
    # the same buffer geometry it already computes internally, for
    # display purposes. Off by default (opt-in) so normal runs are not
    # slowed down or cluttered with an extra output file.
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
    # Accumulates (frontage_line, depth_line, frontage_total, depth_val) per parcel
    # for QA output. None entries mean no road frontage for that parcel.
    frontage_lines_data = []
    total = len(brgy_gdf)

    # Resolve parcel identifier column for QA output FEATURE_ID.
    # Priority: PIN → pin → Pin → ARP_NO → TD_NO → PARCEL_ID → row index.
    # Falls back to row index if none found — never crashes on missing column.
    _PIN_CANDIDATES = ["PIN", "pin", "Pin", "ARP_NO", "TD_NO", "PARCEL_ID"]
    _pin_col = next((c for c in _PIN_CANDIDATES if c in brgy_gdf.columns), None)

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

    # buffer_qa_records: only populated when emit_buffer_qa=True. One
    # record per boundary segment that registered coverage, holding the
    # exact seg.buffer(FRONTAGE_BUFFER_TOLERANCE, cap_style=2) zone -- the
    # same buffer geometry _edge_covered_portion() computes internally
    # (called with tol=FRONTAGE_BUFFER_TOLERANCE), re-derived here purely
    # for diagnostic display (see process_frontage_single()'s docstring).
    buffer_qa_records = []

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

    for i, geom_raw in enumerate(geoms, start=1):
        if progress and (i % 200 == 0 or i == 1 or i == total):
            progress(f"{source_name}: {i}/{total}", i, total)

        geom = fix_geometry(geom_raw)
        if geom is None:
            _parcel_states.append({
                "geom": None, "covered_pieces": [], "seg_buffer_records": [],
                "resolved": (0.0, 0.0, 0.0, None, None, []),
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
                "geom": geom, "covered_pieces": [], "seg_buffer_records": [],
                "resolved": (0.0, depth_val, depth_val, None, None, []),
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
            seg_buffer_records = []  # (segment_index, buffer_geom), QA only

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
                            if emit_buffer_qa:
                                # Stage 2's buffer only -- Stage 1 is a
                                # pass/fail gate with nothing meaningful
                                # to visualize as a measured "covered"
                                # zone.
                                seg_buffer_records.append(
                                    (_seg_idx, seg.buffer(TWO_STAGE_MEASURE_TOLERANCE, cap_style=2))
                                )
                # else: gate rejected this parcel -- covered_pieces stays
                # empty, frontage_total will be 0.0 in Pass 3.
            else:
                for _seg_idx, seg in enumerate(segments):
                    pieces = _edge_covered_pieces(seg, road_union, tol=FRONTAGE_BUFFER_TOLERANCE)
                    if pieces:
                        covered_pieces.extend(pieces)
                        if emit_buffer_qa:
                            # Explicitly passed FRONTAGE_BUFFER_TOLERANCE
                            # above (not _edge_covered_pieces()'s own
                            # default) -- this re-derives the identical
                            # buffer zone actually used for this call, it
                            # does not introduce a second, independent
                            # tolerance value.
                            seg_buffer_records.append(
                                (_seg_idx, seg.buffer(FRONTAGE_BUFFER_TOLERANCE, cap_style=2))
                            )
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
                "seg_buffer_records": seg_buffer_records, "resolved": None,
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
                "geom": geom, "covered_pieces": [], "seg_buffer_records": [],
                "resolved": None,
            })

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

            for idx in range(len(all_global_pieces)):
                if idx in disregarded:
                    continue
                parcel_idx_i, piece_i, len_i = all_global_pieces[idx]
                total_i = parcel_prelim_total[parcel_idx_i]
                buf_i = buffered_geoms[idx]
                candidate_idxs = tree.query(buf_i)
                for cand_idx in candidate_idxs:
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
    for i, state in enumerate(_parcel_states, start=1):
        if progress and (i % 200 == 0 or i == 1 or i == total):
            progress(f"{source_name}: {i}/{total}", i, total)

        if state["resolved"] is not None:
            frontage_total, depth_val, dwr_val, _frontage_line, _depth_line, _all_pieces = state["resolved"]
            frontage_lengths.append(frontage_total)
            depths.append(depth_val)
            dwrs.append(dwr_val)
            frontage_lines_data.append((_frontage_line, _depth_line, frontage_total, depth_val, dwr_val, _all_pieces))
            continue

        geom = state["geom"]
        covered_pieces = state["covered_pieces"]

        if emit_buffer_qa:
            feat_id_qa = brgy_gdf.iloc[i - 1][_pin_col] if _pin_col else (i - 1)
            for _seg_idx, buf_geom in state["seg_buffer_records"]:
                buffer_qa_records.append({
                    "FEATURE_ID": feat_id_qa,
                    "SEGMENT_IX": _seg_idx,
                    "geometry": buf_geom,
                })

        frontage_total = sum(p.length for p in covered_pieces)
        _all_pieces = covered_pieces
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
            _all_pieces = []

        frontage_lengths.append(frontage_total)

        # QA: capture actual frontage geometry (_fl itself, not the chord line)
        # so the exported layer faithfully shows the road-facing boundary
        # portion the algorithm detected, including any real kinks or curves.
        _frontage_line = _fl if (_fl is not None and not _fl.is_empty) else None

        _depth_line = None
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
                        len1, len2, depth_val = 0.0, 0.0, 0.0
                        i1 = i2 = type("_empty", (), {"is_empty": True})()

                    # QA: capture the actual chosen perpendicular ray so the
                    # exported layer shows exactly where depth was measured.
                    chosen_inter = i1 if len1 >= len2 else i2
                    if not chosen_inter.is_empty:
                        if chosen_inter.geom_type == "LineString":
                            end_pt = chosen_inter.coords[-1]
                        elif chosen_inter.geom_type == "MultiLineString":
                            end_pt = max(
                                chosen_inter.geoms, key=lambda g: g.length
                            ).coords[-1]
                        else:
                            end_pt = None
                        if end_pt:
                            _depth_line = LineString([midpoint, end_pt])

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
        frontage_lines_data.append((_frontage_line, _depth_line, frontage_total, depth_val, dwr_val, _all_pieces))


    # 🔒 Safety check — prevents silent column mismatch on partial failures.
    if not (len(frontage_lengths) == len(brgy_gdf) == len(depths) == len(dwrs)):
        raise RuntimeError("Attribute length mismatch during frontage processing")

    brgy_gdf[road_frontage_col] = frontage_lengths
    brgy_gdf[depth_col] = depths
    brgy_gdf[dwr_col] = dwrs

    if original_crs:
        brgy_gdf = brgy_gdf.to_crs(original_crs)

    if progress:
        progress(f"Building Visual Measurement layer for {source_name}...", total, total)

    # ---- Build Visual Measurement (VM) GeoDataFrame ----
    # TWO rows per parcel that has road frontage (one Method="frontage"
    # row, one Method="depth" row) -- NOT one combined row. This is a
    # deliberate change from an earlier, single-row design, made after
    # confirming (via independent research across three separate
    # sources, converging on the same documented explanation) that
    # Global Mapper has NO multi-part grouping support for LINE
    # features (only for AREA/polygon features, via its "Area Group ID"
    # mechanism) -- so a single feature whose geometry is a MultiLineString
    # with DISJOINT parts (e.g. a frontage boundary segment and a
    # separate, non-touching depth ray) is unavoidably split into one
    # attribute row per disconnected part by Global Mapper's own import
    # engine, regardless of how the geometry is combined on the writing
    # side. The underlying .gpkg itself is unaffected and spec-compliant
    # either way (confirmed: reading it back with geopandas always shows
    # the true row count) -- this is purely about what Global Mapper's
    # Attribute Editor displays.
    #
    # Rather than leave that split implicit (one row in the file,
    # mysteriously appearing as two-plus rows with duplicated values in
    # Global Mapper, with no column explaining why), it is made explicit
    # here: two separate, real rows are written, each tagged with a
    # Method column ("frontage" or "depth") describing which geometry
    # that row holds, and each carrying the SAME complete set of value
    # columns (CAMA_ROAD_FRONT, CAMA_DEPTH, CAMA_DEPTH_WIDTH_RATIO) --
    # never blank/NaN on one side -- so every row is self-consistent and
    # traceable back to its parcel via the PIN column, however many rows
    # Global Mapper (or any other viewer) ends up showing for a given
    # parcel's geometry.
    #
    # PIN uses the same PIN-like column already resolved for the main
    # output (_pin_col, see _PIN_CANDIDATES above), or the zero-based row
    # index as a fallback if no PIN-like column was found -- in that
    # fallback case this column holds a row index, not a literal PIN,
    # despite its name.
    #
    # CAMA_ROAD_FRONT / CAMA_DEPTH / CAMA_DEPTH_WIDTH_RATIO: cross-tool
    # CAMA_ prefix standard (see OUTPUT_COLUMN_TARGETS' module-level
    # docstring) -- these mirror the same computed values as
    # CAMA_ROAD_FRONTAGE/CAMA_DEPTH/CAMA_DEPTH_WIDTH_RATIO in the main
    # output (no "_M" suffix -- dropped per project-lead decision). PIN
    # and Method do NOT get the prefix -- they are reference/descriptor
    # fields, not computed measurement values, matching road_width.py's
    # own PIN column staying unprefixed while only its value column
    # (cama_road_width) gets "cama_".
    #
    # All numeric columns are always float64 — None cells use float("nan")
    # so GIS software infers consistent field types from the first row.
    line_records = []
    for idx, (fl, dl, fval, dval, dwr_val, all_pieces) in enumerate(frontage_lines_data):
        feat_id = brgy_gdf.iloc[idx][_pin_col] if _pin_col else idx
        # Only emit Visual Measurement row(s) when ROAD_FRONT is greater
        # than zero. This preserves the business rule:
        #   ROAD_FRONT > 0  -> Visual Measurement row(s) emitted
        #   ROAD_FRONT = 0  -> no Visual Measurement row(s)
        if fval > 0 and all_pieces:
            cama_road_front = float(round(fval, 2))
            cama_depth = float(round(dval, 2)) if dval else float("nan")
            cama_dwr = float(round(dwr_val, 2)) if dwr_val else float("nan")

            # Row 1: frontage geometry -- the boundary piece(s) only.
            # Kept as a MultiLineString (not a plain LineString) even
            # when there's only one piece, so this layer's geometry
            # column stays a single, consistent type across every row
            # (GeoPackage layers expect one geometry type per layer) --
            # matching the depth row below, which is also wrapped as a
            # single-part MultiLineString for the same reason.
            line_records.append({
                "PIN":                    feat_id,
                "Method":                 "frontage",
                "CAMA_ROAD_FRONT":        cama_road_front,
                "CAMA_DEPTH":             cama_depth,
                "CAMA_DEPTH_WIDTH_RATIO": cama_dwr,
                "geometry":               MultiLineString(list(all_pieces)),
            })
            # Row 2: depth geometry -- the perpendicular ray only. Only
            # emitted if a depth ray was actually computed (dl can be
            # None in edge cases, e.g. a degenerate perpendicular vector
            # -- see the depth-ray computation above).
            if dl is not None:
                line_records.append({
                    "PIN":                    feat_id,
                    "Method":                 "depth",
                    "CAMA_ROAD_FRONT":        cama_road_front,
                    "CAMA_DEPTH":             cama_depth,
                    "CAMA_DEPTH_WIDTH_RATIO": cama_dwr,
                    "geometry":               MultiLineString([dl]),
                })

    _qa_crs = original_crs if original_crs else f"EPSG:{zone_epsg}"
    if line_records:
        lines_gdf = gpd.GeoDataFrame(line_records, crs=f"EPSG:{zone_epsg}")
        if original_crs:
            lines_gdf = lines_gdf.to_crs(original_crs)
    else:
        lines_gdf = gpd.GeoDataFrame(
            columns=["PIN", "Method", "CAMA_ROAD_FRONT", "CAMA_DEPTH",
                     "CAMA_DEPTH_WIDTH_RATIO", "geometry"],
            geometry="geometry",
            crs=_qa_crs,
        )

    if progress:
        progress(f"Finished {source_name}", total, total)

    # Buffer diagnostic layer -- only built when emit_buffer_qa=True
    # (opt-in). One polygon per boundary segment that registered coverage,
    # tagged with FEATURE_ID and SEGMENT_IX -- the raw material for
    # visually auditing whether a segment's buffer genuinely reaches an
    # adjacent road or is bleeding across a thin parcel / grazing a
    # crossing road. Same CRS handling as the frontage-lines QA layer.
    if emit_buffer_qa and buffer_qa_records:
        buffers_gdf = gpd.GeoDataFrame(buffer_qa_records, crs=f"EPSG:{zone_epsg}")
        if original_crs:
            buffers_gdf = buffers_gdf.to_crs(original_crs)
    else:
        buffers_gdf = gpd.GeoDataFrame(
            columns=["FEATURE_ID", "SEGMENT_IX", "geometry"],
            geometry="geometry",
            crs=_qa_crs,
        )

    # Return parcels and QA layers as a structured dict so callers can
    # access each output by name. qa_layers is a container — future QA
    # outputs (e.g. snapped points, debug polygons) can be added here
    # without changing the caller API again.
    return {
        "parcels": brgy_gdf,
        "qa_layers": {
            "frontage_lines": lines_gdf,
            "segment_buffers": buffers_gdf,
        },
    }


def resolve_db_output_table(root, schema, barangay_source):
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
        table), then requires user confirmation before treating a
        match as an overwrite target -- zero candidates skips the
        dialog entirely and creates a new table under the filename.

    Returns (resolved_table_name, resolved_outcome), or (None, None) if
    the user cancelled -- caller must abort the entire run in that
    case, matching ask_overwrite_dialog()'s existing
    cancel-aborts-everything semantics (there is no "create new" choice
    for DB output).
    """
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


# ========================= MAIN PROCESS =========================
def run_processing(app_root, resolved_table_name=None):
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

    progress = ProgressWindow(app_root, "Road Frontage Progress")

    q = queue.Queue()

    def worker():
        try:
            q.put(("update", "Loading road data...", None, None))

            def progress_cb(msg, val=None, maxv=None):
                q.put(("update", msg, val, maxv))


            # Reuse the road layer already read by the Road Classification
            # section's background read (see open_main_window()) when it
            # matches the currently selected road source -- avoids a
            # second full file/DB read of the same data. Looks up the
            # dual-slot cache's slot for whichever mode (local/db) was
            # actually run, since the two are cached independently.
            road_slot = _road_gdf_cache.get(road_source[0], {})
            if road_slot.get("key") == road_source[1][0] and road_slot.get("gdf") is not None:
                road_gdf = road_slot["gdf"]
                print("ℹ️ Reusing cached road network (already read during source selection).")
            elif road_source[0] == "local":
                road_gdf = gpd.read_file(road_source[1][0])
            else:
                road_table = road_source[1][0]
                road_gdf = read_postgis_clean(road_table, engine, schema)

            if barangay_source[0] == "local":
                sources = [("local", p) for p in barangay_source[1]]
            else:
                sources = [("db", t) for t in barangay_source[1]]

            skipped = []
            for src_type, src in sources:
                try:
                    if src_type == "local":
                        name = os.path.basename(src)
                        q.put(("update", f"Loading {name}", None, None))
                        brgy_gdf = gpd.read_file(src)
                        out_base = os.path.splitext(name)[0]
                    else:
                        name = src
                        q.put(("update", f"Loading DB table {name}", None, None))
                        brgy_gdf = read_postgis_clean(name, engine, schema)
                        out_base = name

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

                    result = process_frontage_single(
                        brgy_gdf,
                        road_gdf,
                        name,
                        progress=progress_cb,
                        classification=classification,
                        emit_buffer_qa=emit_buffer_qa,
                        road_frontage_col=road_frontage_col,
                        depth_col=depth_col,
                        dwr_col=dwr_col
                    )
                    brgy_gdf    = result["parcels"]
                    lines_gdf   = result["qa_layers"]["frontage_lines"]
                    buffers_gdf = result["qa_layers"]["segment_buffers"]

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
                        # exactly). Both QA layers derive their base name
                        # from this SAME resolved base_name via
                        # with_output_suffix() -- never their own
                        # independent numbering scan -- so e.g. main
                        # "landparcel_1.gpkg" always pairs with QA lines
                        # "landparcel_1_VM.gpkg" ("VM" = Visual
                        # Measurement) and buffer diagnostic
                        # "landparcel_1_segment_buffers.gpkg", never a
                        # mismatched number.
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

                        # QA layer: {base_name}_VM.gpkg (Visual Measurement)
                        # — written alongside the main output so QA can
                        # load both in the same GM session. Only written
                        # for local output (DB mode has no output_dir).
                        if not lines_gdf.empty:
                            lines_base_name = with_output_suffix(base_name, "VM")
                            lines_out = os.path.join(
                                output_mode[1],
                                f"{lines_base_name}.gpkg"
                            )
                            _write_gpkg(lines_gdf, lines_out)
                            q.put(("open_gm", lines_out, None, None))

                        # Optional third QA layer: the exact per-segment
                        # buffer zones used by _edge_covered_portion() --
                        # only written when the user opted in via the
                        # "Generate buffer diagnostic layer" checkbox.
                        if emit_buffer_qa and not buffers_gdf.empty:
                            buffers_base_name = with_output_suffix(base_name, "segment_buffers")
                            buffers_out = os.path.join(
                                output_mode[1],
                                f"{buffers_base_name}.gpkg"
                            )
                            _write_gpkg(buffers_gdf, buffers_out)
                            q.put(("open_gm", buffers_out, None, None))
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
                        with engine.begin() as conn:
                            brgy_gdf.to_postgis(
                                db_table,
                                conn,
                                schema=schema,
                                if_exists="replace",
                                index=False
                            )
                        # frontage_lines is a QA-only artifact — not written to DB.

                except Exception as source_err:
                    # Isolate failures per source: log/report and move on to
                    # the next source instead of aborting the entire batch.
                    # Sources already written before this one keep their
                    # output — only this one is skipped.
                    skipped.append((name, str(source_err)))
                    q.put(("update", f"Skipped {name}: {source_err}", None, None))
                    continue

            if skipped:
                summary = "Done, but some sources were skipped:\n" + "\n".join(
                    f"- {n}: {err}" for n, err in skipped
                )
            else:
                summary = "Processing done!"
            q.put(("done", summary, None, None))

        except Exception as e:
            q.put(("error", str(e), None, None))

    def poll_queue():
        try:
            while True:
                msg = q.get_nowait()
                kind = msg[0]

                if kind == "update":
                    progress.update(msg[1], msg[2], msg[3])

                elif kind == "open_gm":
                    open_in_global_mapper(msg[1])

                elif kind == "done":
                    progress.close()
                    messagebox.showinfo("Success", msg[1])
                    return

                elif kind == "error":
                    progress.close()
                    messagebox.showerror("Error", msg[1])
                    return

        except queue.Empty:
            pass

        app_root.after(100, poll_queue)

    threading.Thread(target=worker, daemon=True).start()
    poll_queue()


# ========================= MAIN APP =========================
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
    apply_icon(dialog)
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
    apply_icon(dialog)
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
    apply_icon(dialog)
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


def open_main_window(root):
    win = tk.Toplevel(root)
    apply_icon(win)
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
        state. Deliberately does NOT pack the new container or call
        _reflow_window() here -- every caller of this function calls
        _update_road_classification_visibility() immediately afterward,
        which owns all packing/positioning decisions for whichever
        container is current at that moment.
        """
        nonlocal road_type_checklist_container
        new_container = tk.Frame(road_frame)
        for display_text in sorted(road_type_value_vars.keys()):
            real_value, var = road_type_value_vars[display_text]
            tk.Checkbutton(new_container, text=display_text,
                           variable=var).pack(anchor="w")
        old_container = road_type_checklist_container
        road_type_checklist_container = new_container
        old_container.destroy()

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
          2. Cache synchronization (no-op here -- parcel_classification_vars
             and _parcel_classification_cache[...]["vars"] already share
             the same BooleanVar objects by reference, so a checkbox
             toggle is automatically visible through the cache with no
             explicit sync step needed; kept as an explicit "nothing to
             do" step only for structural symmetry with the other callback)
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

    def _rebuild_lot_classification_checklist(reuse_vars=None):
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

        reuse_vars: optional {path_or_table: tk.BooleanVar} to reuse
        instead of creating fresh ones -- used by the cache-hit path in
        _refresh_parcel_classification() so toggling Local <-> Database
        back to an unchanged selection restores each checkbox's
        checked/unchecked state exactly as the user left it, instead of
        resetting everything back to unchecked.

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
            if reuse_vars is not None and path_or_table in reuse_vars:
                var = reuse_vars[path_or_table]
            else:
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
        _rebuild_lot_classification_checklist() (with the correct
        reuse_vars, if applicable) before calling this function -- kept
        as the caller's responsibility rather than threaded through here,
        so the cache-hit "restore checked state" behavior keeps working
        without extra parameters.
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

    def _update_road_classification_visibility():
        """
        Shows the "Filter by Road Type" checkbox plus (if checked) its
        per-value checklist, and, ADDITIVELY, a "⏳ Reading road
        network…" indicator while a background read is in flight -- the
        indicator appears alongside whatever was already on screen (old
        data, about to be replaced) without ever hiding or clearing it.
        No usable ROAD_TYPE-like column found shows neither -- matches
        lot_location.py exactly.

        Invariant this preserves: the GUI never passes through an empty
        intermediate state just because a background refresh is in
        progress. The checkbox/checklist's own pack state/position is
        entirely decided HERE (not by _rebuild_road_type_checklist(),
        which only swaps which Frame object is current) -- while
        reading, this function does not touch road_filter_checkbox's or
        road_type_checklist_container's pack state at all, so both stay
        exactly as they were.
        """
        if road_is_reading:
            road_reading_lbl.pack(anchor="w", pady=(2, 0))
            _reflow_window()
            return
        road_reading_lbl.pack_forget()
        if road_type_value_vars:
            road_filter_checkbox.pack(anchor="w", pady=(2, 0))
            if filter_road_type_var.get():
                road_type_checklist_container.pack(
                    fill="x", padx=(20, 0), pady=(2, 0), after=road_filter_checkbox)
            else:
                road_type_checklist_container.pack_forget()
        else:
            road_filter_checkbox.pack_forget()
            road_type_checklist_container.pack_forget()
        _reflow_window()

    def _on_filter_road_type_changed(*_args):
        """
        Mutual exclusion, mirror of _on_parcel_classification_checkbox_changed
        above: checking "Filter by Road Type" un-checks EVERY per-source
        classification checkbox currently on the Land Parcel side.

        Operation order identical to
        _on_parcel_classification_checkbox_changed() above, deliberately:
          1. Guarded mutual-exclusion mutation
          2. Cache synchronization -- mirrors the live checked-state into
             the currently active Road Network mode's cache slot
             (_road_gdf_cache), if that slot already has cached data, so
             a later toggle away and back (via
             _refresh_road_classification()'s cache-hit path) restores
             not just which Road Type values were checked, but whether
             Filter by Road Type itself was on, exactly as the user left
             it.
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
        # Step 2: cache sync.
        current_type = road_source_type.get()
        if _road_gdf_cache[current_type]["gdf"] is not None:
            _road_gdf_cache[current_type]["filter_active"] = filter_road_type_var.get()
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

    def _set_road_reading_state(reading):
        """Disable Road Network Browse/radio controls while its background
        classification read is in progress -- prevents starting a second,
        overlapping read of the same source.

        NOTE: unlike _set_parcel_reading_state() above, the Road Network
        side deliberately keeps its ORIGINAL separate-widget "Reading…"
        indicator (road_reading_lbl, shown/hidden via
        _update_road_classification_visibility()) -- per project-lead
        decision, the Canvas/reused-label treatment was scoped to Land
        Parcel only. Road Network's checklist is typically 5-15 ROAD_TYPE
        values (vs. potentially dozens/hundreds of Land Parcel sources),
        a much smaller unbounded-growth risk that didn't justify the
        added complexity here too.
        """
        state = "disabled" if reading else "normal"
        road_btn.config(state=state)
        road_radio_local.config(state=state)
        road_radio_db.config(state=state)

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
            run_status_var.set("Reading parcel source for classification…")
            ready = False
        elif road_is_reading:
            run_status_var.set("Reading road network for classification…")
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

    def _refresh_parcel_classification(force_refresh=False):
        """
        Background-reads EVERY currently selected Land Parcel file/table
        (not just the first) so the per-source checklist can offer a
        checkbox for every source that actually has a usable
        LOT_LOCATION/LOT_LABEL column -- UNLESS the dual-slot
        _parcel_classification_cache already has a still-valid entry for
        this exact mode+selection (e.g. toggling Local <-> Database back
        to a selection that hasn't changed), in which case the checklist
        -- including each checkbox's checked state -- is restored
        instantly with no read at all. Each source is still resolved
        independently at run time regardless of what's checked here;
        this background read only decides which checkboxes to SHOW.
        GeoDataFrames are discarded immediately after inspection, not
        cached -- only the tiny per-source detection tuples and the
        BooleanVars are kept in the cache, and run_processing() re-reads
        each source itself when the run actually starts.

        force_refresh: when True, skips the cache-hit check entirely and
        always does a fresh read, even if the cache key matches. Must be
        True whenever this is called because the user just ACTIVELY
        selected source(s) via Browse (browse_parcel_files() /
        browse_parcel_db()) -- if they re-select the exact same file(s)
        (e.g. after editing one externally in QGIS to add/change
        LOT_LOCATION values), a plain key match would otherwise silently
        serve the old, now-stale cached checklist instead of re-reading
        what's actually on disk/in the DB right now. The cache-hit
        shortcut is only safe to take on the _toggle_parcel() path (the
        user didn't select anything new, just switched which already-made
        selection is active), which calls this with the default
        force_refresh=False.
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
            # to read. The OTHER mode's cache slot is left untouched.
            parcel_read_details = []
            parcel_output_column_conflicts = []
            _rebuild_lot_classification_checklist()
            _update_parcel_classification_visibility()
            _update_run_button_state()
            return

        cache_key = tuple(sources)
        slot = _parcel_classification_cache[source_type]
        if not force_refresh and slot["key"] == cache_key and slot["details"] is not None:
            # True cache hit: same mode, exact same set of selected
            # sources, already inspected -- restore the checklist
            # (including each checkbox's checked state) with no I/O.
            parcel_read_details = slot["details"]
            parcel_output_column_conflicts = _check_parcel_frontage_conflicts(parcel_read_details)
            _rebuild_lot_classification_checklist(reuse_vars=slot["vars"])
            _update_parcel_classification_visibility()
            _update_run_button_state()
            return

        # Cache miss: selection changed for this mode, or first time
        # selecting these sources -- do the actual background read. Per
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
                existing_output_cols = _detect_existing_output_columns(gdf)
                per_source_results.append((path_or_table, state, col_name, kind, existing_output_cols))
                del gdf
            result_queue.put(per_source_results)

        parcel_is_reading = True
        _set_parcel_reading_state(True)
        _update_parcel_classification_visibility()
        _update_run_button_state()
        threading.Thread(target=worker, daemon=True).start()
        win.after(100, lambda: _poll_parcel_classification_queue(result_queue, source_type, cache_key))

    def _poll_parcel_classification_queue(result_queue, source_type, cache_key):
        nonlocal parcel_is_reading, parcel_read_details, parcel_output_column_conflicts
        if not win.winfo_exists():
            return
        try:
            per_source_results = result_queue.get_nowait()
        except queue.Empty:
            win.after(100, lambda: _poll_parcel_classification_queue(result_queue, source_type, cache_key))
            return

        parcel_is_reading = False
        _set_parcel_reading_state(False)
        parcel_read_details = per_source_results
        parcel_output_column_conflicts = _check_parcel_frontage_conflicts(per_source_results)

        failed = [src for (src, state, _c, _k, _rw) in per_source_results if state is None]
        for src in failed:
            print(f"⚠️ Could not read parcel layer for classification check: {src}")

        # Builds one checkbox per source with state == LOT_STATE_FOUND;
        # sources without a usable column are simply omitted (no "not
        # found" line), matching lot_location.py's own
        # auto-hide-when-nothing-usable convention. Fresh BooleanVars are
        # created here (reuse_vars not passed) since this is a genuinely
        # new read, not a cache restore.
        _rebuild_lot_classification_checklist()

        # This slot now holds the definitive, up-to-date data for this
        # exact mode+selection -- the other mode's slot (and any
        # differently-selected entry for THIS mode) is left untouched.
        _parcel_classification_cache[source_type] = {
            "key": cache_key,
            "details": per_source_results,
            "vars": dict(parcel_classification_vars),
        }

        _update_parcel_classification_visibility()
        _update_run_button_state()

    def _refresh_road_classification(force_refresh=False):
        """
        Background-reads the currently selected Road Network source (a
        single file/table -- Road Network, unlike Land Parcel, only ever
        supports one selection) -- UNLESS the dual-slot cache already has
        a still-valid entry for this exact mode+selection (e.g. the user
        just toggled Local <-> Database back to a selection that hasn't
        changed), in which case the checklist is restored instantly from
        cache with no read and no background thread at all. Populates
        road_type_value_vars for the Filter by Road Type checklist and
        caches the read gdf in _road_gdf_cache so run_processing() can
        reuse it instead of reading the same file/table a second time.

        force_refresh: when True, skips the cache-hit check entirely and
        always does a fresh read, even if the cache key matches. Must be
        True whenever this is called because the user just ACTIVELY
        selected a source via Browse (browse_road_file() /
        browse_road_db()) -- if they re-select the exact same file/table
        (e.g. after editing it externally to add/change ROAD_TYPE
        values), a plain key match would otherwise silently serve the
        old, now-stale cached checklist instead of re-reading what's
        actually on disk/in the DB right now. The cache-hit shortcut is
        only safe to take on the _toggle_road() path (the user didn't
        select anything new, just switched which already-made selection
        is active), which calls this with the default force_refresh=False.
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

        slot = _road_gdf_cache[source_type]
        if not force_refresh and slot["key"] == path_or_table and slot["gdf"] is not None:
            # True cache hit: same mode, same selection, already read --
            # restore the checklist (including each value's checked
            # state) with no I/O at all. Immediate, synchronous swap --
            # not the "reading" transient state.
            road_type_value_vars.clear()
            road_type_value_vars.update(slot["value_vars"])
            _rebuild_road_type_checklist()
            filter_road_type_var.set(slot.get("filter_active", False))
            _update_road_classification_visibility()
            _update_run_button_state()
            return

        # Cache miss: new file/table for this mode, or first time
        # selecting it -- do the actual background read. Per the "never
        # pass through an empty intermediate state" invariant, the
        # EXISTING checkbox/checklist (if any) is left completely
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

        road_is_reading = True
        _set_road_reading_state(True)
        _update_road_classification_visibility()
        _update_run_button_state()
        threading.Thread(target=worker, daemon=True).start()
        win.after(100, lambda: _poll_road_classification_queue(result_queue, source_key))

    def _poll_road_classification_queue(result_queue, source_key):
        nonlocal road_is_reading
        if not win.winfo_exists():
            return
        try:
            gdf, error = result_queue.get_nowait()
        except queue.Empty:
            win.after(100, lambda: _poll_road_classification_queue(result_queue, source_key))
            return

        source_type, path_or_table = source_key
        road_is_reading = False
        _set_road_reading_state(False)
        if error is not None or gdf is None:
            print(f"⚠️ Could not read road layer for classification check: {error}")
            _road_gdf_cache[source_type] = {
                "key": None, "gdf": None, "value_vars": {}, "filter_active": False
            }
            road_type_value_vars.clear()
            _rebuild_road_type_checklist()
            filter_road_type_var.set(False)
            _update_road_classification_visibility()
            _update_run_button_state()
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

        # This slot now holds the definitive, up-to-date data for this
        # mode -- the other mode's slot is completely untouched.
        _road_gdf_cache[source_type] = {
            "key": path_or_table, "gdf": gdf, "value_vars": new_value_vars,
            "filter_active": False
        }
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
            # guard) does NOT trigger a re-read. force_refresh=True: the
            # user actively chose this selection just now via Browse --
            # even if it's identical to a previously cached one (e.g.
            # re-selecting after editing the file externally), it must
            # be read fresh, never served from cache.
            #
            # No manual reflow/freeze needed here: under the swap-based
            # checklist lifecycle, the OLD checklist (if any) simply
            # stays fully visible and untouched throughout the read that
            # _refresh_parcel_classification() is about to start -- there
            # is no intermediate "cleared" state to protect against. The
            # window only ever resizes once, at the very end, when the
            # new checklist actually replaces the old one.
            _refresh_parcel_classification(force_refresh=True)

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
                # browse_parcel_files() above.
                _refresh_parcel_classification(force_refresh=True)

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
        # remembered selection or cached checklist state -- that's the
        # whole point of the dual-slot _parcel_classification_cache. This
        # call shows whichever of the three states actually applies to
        # the newly active mode: instantly restored from cache, freshly
        # read, or hidden (nothing selected for this mode yet).
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

    # "Filter by Road Type" checkbox -- created once, only packed/unpacked
    # (never destroyed) by _update_road_classification_visibility(). Same
    # static label as lot_location.py's own Filter by Road Type checkbox
    # (the text itself never needs to change, unlike the parcel side).
    road_filter_checkbox = tk.Checkbutton(
        road_frame, text="Filter by Road Type", variable=filter_road_type_var)
    # Holds one Checkbutton per unique ROAD_TYPE value found in the
    # currently selected road layer. Only packed while the checkbox above
    # is checked AND a usable ROAD_TYPE-like column was found (see
    # _update_road_classification_visibility()).
    road_type_checklist_container = tk.Frame(road_frame)
    road_reading_lbl = tk.Label(
        road_frame, text="⏳ Reading road network…",
        fg="#b36b00", font=("Segoe UI", 8, "italic"), anchor="w")
    # All three start unpacked; _update_road_classification_visibility()
    # (via _refresh_road_classification()) decides what to show.

    def browse_road_file():
        f = filedialog.askopenfilename(filetypes=[
            ("Shapefiles", "*.shp"), ("GeoPackage", "*.gpkg"), ("All", "*.*")])
        if f:
            road_local_path.set(f)
            road_file_var.set(os.path.basename(f))
            # A new Road Network selection invalidates any prior Road
            # Type detection -- re-inspect the new road layer.
            # force_refresh=True: the user actively chose this selection
            # just now via Browse -- even if it's identical to a
            # previously cached one (e.g. re-selecting after editing the
            # file externally), it must be read fresh, never served from
            # cache.
            #
            # No manual reflow/freeze needed here -- same reasoning as
            # browse_parcel_files(): under the swap-based checklist
            # lifecycle, the OLD checkbox/checklist (if any) simply stays
            # fully visible and untouched throughout the read that
            # _refresh_road_classification() is about to start.
            _refresh_road_classification(force_refresh=True)

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
                # browse_road_file() above.
                _refresh_road_classification(force_refresh=True)

        _pick_db_tables(win, tables, multi=False, on_select=_on_road_table_selected)

    def _toggle_road():
        if road_source_type.get() == "local":
            road_lbl.config(textvariable=road_file_var)
            road_btn.config(text="Browse…", command=browse_road_file)
        else:
            road_lbl.config(textvariable=road_db_var)
            road_btn.config(text="Select…", command=browse_road_db)
        # Switching Local <-> Database does NOT clear the other mode's
        # remembered selection or cached checklist state -- that's the
        # whole point of the dual-slot _road_gdf_cache. This call shows
        # whichever of the three states actually applies to the newly
        # active mode: instantly restored from cache, freshly read, or
        # hidden (nothing selected for this mode yet).
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

    # Opt-in diagnostic: writes a THIRD .gpkg (alongside the main output
    # and the frontage_lines QA layer) containing the exact per-segment
    # buffer zones _edge_covered_portion() uses internally, for visually
    # auditing whether a segment's buffer genuinely reaches an adjacent
    # road or is bleeding across a thin parcel / grazing a crossing road.
    # Off by default -- purely a debugging aid, not part of normal output.
    emit_buffer_qa_var = tk.BooleanVar(master=win, value=False)
    tk.Checkbutton(
        output_frame, text="Generate buffer diagnostic layer (Visual Measurement)",
        variable=emit_buffer_qa_var
    ).pack(anchor="w", pady=(4, 0))

    # ── RUN BUTTON ───────────────────────────────────────────────
    ttk.Separator(win, orient="horizontal").pack(
        fill="x", padx=10, pady=(12, 4))

    def on_run():
        global barangay_source, road_source, output_mode, parcel_classification_selection, filter_by_road_type_active, road_type_excluded_values, emit_buffer_qa, overwrite_mode, parcel_output_column_overrides

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

        emit_buffer_qa = emit_buffer_qa_var.get()

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
        # "_road_frontage" suffix that used to be appended here is gone;
        # only the QA lines layer gets a suffix now, "_VM" for Visual
        # Measurement -- see with_output_suffix() usage below).
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
        # is not threaded through (same as road_surface.py/road_density.py/
        # land_shape_compactness.py) because nothing downstream in this
        # file's worker() consumes it -- only resolved_table_name is read
        # (see the db_table fallback near "Falls back to out_base only
        # if...").
        # ------------------------------------------------------------------
        resolved_table_name = None
        if output_mode[0] == "db":
            _resolve_creds = load_db_credentials()
            if not _resolve_creds:
                messagebox.showerror("Error", "Missing pg_credentials.json")
                return
            _resolve_schema = _resolve_creds["schema"]
            resolved_table_name, _resolved_outcome = resolve_db_output_table(
                win, _resolve_schema, barangay_source
            )
            if resolved_table_name is None:
                print("Run cancelled by user (database output table not confirmed).")
                return

        win.destroy()
        if _app_root is None:
            messagebox.showerror("Error", "No root window available. Please restart the tool.")
            return
        run_processing(_app_root, resolved_table_name)

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



def main(parent=None):
    global _app_root
    if parent is not None:
        _app_root = parent
        open_main_window(parent)
    else:
        root = tk.Tk()
        _app_root = root
        apply_icon(root)
        root.withdraw()
        open_main_window(root)
        root.mainloop()


if __name__ == "__main__":
    main()