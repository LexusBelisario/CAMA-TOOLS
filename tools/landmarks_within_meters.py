"""
tools/landmarks_within_meters.py

PURPOSE:
    CAMA Tools tool ("LANDMARKS WITHIN METERS" in MAIN.py's dispatch
    table): for each Land Parcel, counts how many POIs fall within
    range of the parcel centroid, per user-checked landmark category
    (dynamically discovered from the POI source's own 'fclass' values
    -- see the "Landmark Categories" checklist in open_main_window()).
    Each checked category independently uses either the Aerial method
    (straight-line geodesic distance, within a user-entered Aerial
    radius) or the Road method (network-routed distance along a
    user-supplied Road Network source, within a separate user-entered
    Road distance) -- see process_poi_counts_dynamic(). Writes one
    CAMA_NUM_{KEY}-prefixed count column per checked category
    (derive_target_columns()), e.g. CAMA_NUM_POLICE_STATION.

DISPATCH:
    Run as an isolated subprocess by MAIN.py via its `--tool` dispatch
    mechanism (see system context). Entry point is main(), triggered via
    the `if __name__ == "__main__":` guard at the bottom of this file.

INPUTS:
    Land Parcel source: one or more local .shp files, or one or more
    PostGIS tables.
    POI source: a single local .shp file or PostGIS table, with an
    `fclass` column categorizing each point.
    Search radius (meters): user-entered, must be a positive number.
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
    local-file Land Parcel source.

DEPENDENCIES:
    stdlib: os, re, subprocess, json, sys, threading, queue, time,
    bisect, typing, tkinter (+ ttk).
    third-party: geopandas, numpy, pandas, networkx, shapely, geopy,
    sqlalchemy, psycopg2.
    local: utils.table_name_matching, utils.resource_path,
    utils.db_discovery, utils.column_detection, utils.window_icon.

SIDE EFFECTS:
    File reads/writes (.shp/.gpkg). PostGIS reads/writes. A live
    PostgreSQL connection. Tkinter GUI windows throughout. The progress
    window itself is still module-global-driven
    (PROG_WIN/PROG_BAR/PROG_LABEL/PROG_STOP_FLAG -- see RUNTIME STATE
    below), but as of Part 3, run_processing()'s own processing runs on
    a background worker thread, following this file's own established
    worker-thread + queue.Queue() + win.after()-polling pattern (the
    same one _refresh_poi_categories()/_poll_poi_category_queue() use
    for POI category discovery) -- all Tkinter calls (progress window
    creation/update/close, the final result dialog) happen only in the
    main-thread poller, never in the worker. A subprocess launch to
    Global Mapper (load_in_global_mapper()) on local-output saves, plus
    a Win32 EnumWindows call to find/focus an already-open Global
    Mapper window first -- both run on the worker thread, since neither
    touches Tkinter.

    D3c: this tool no longer makes any network call of any kind -- the
    old osmnx-based road-network download (OpenStreetMap Overpass API)
    has been fully removed and replaced with the Road Network Source
    GUI section (Task 5) plus the local road-graph construction
    pipeline (D1/D2), which reads a user-uploaded road source (local
    file or DB table) instead of downloading one.

    KNOWN FOLLOW-UP (documented, not implemented here): GM_EXE_PATH
    below is currently hardcoded to a developer/machine-specific
    absolute path
    ("C:\\Program Files\\GlobalMapper26.1_64bit\\global_mapper.exe").
    The planned improvement is dynamic Global Mapper executable-path
    discovery instead of a hardcoded constant. That discovery logic
    (search locations, missing-executable handling, installation-
    variant handling, fallback behavior) is a separate, deliberately-
    scoped future task -- not implemented as part of this
    documentation/reorganization pass, since it would change runtime
    behavior.

    REDESIGN COMPLETE (Clusters A through D3c -- see project task
    documents for the full plan): the "Landmark Categories" checklist
    (dynamic, discovered from the POI source's distinct 'fclass'
    values), the per-category Aerial/Road method selection, the
    independent Aerial/Road radii, the Road Network Source section, and
    the road-network distance engine (process_poi_counts_dynamic(),
    replacing osmnx entirely) are now the tool's ONLY processing path.
    The old fixed police/park/mall/others category model and
    OUTPUT_COLUMN_TARGETS four-column model have been retired -- output
    columns are now entirely dynamic, one CAMA_NUM_{KEY} column per
    checked category, named via derive_target_columns().
"""
import os
import re
import subprocess
import json
import sys
import threading
import queue
import time
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox, ttk

import bisect
from typing import NamedTuple


import geopandas as gpd
import numpy as np
import pandas as pd
import networkx as nx
from shapely.geometry import Point, LineString, box
from shapely.ops import nearest_points
from shapely.strtree import STRtree
from geopy.distance import geodesic
from sqlalchemy import create_engine, inspect, text
import psycopg2

from utils.table_name_matching import find_matching_tables
from utils.resource_path import resource_path
from utils.db_discovery import load_db_credentials, fetch_tables
from utils.column_detection import detect_existing_output_columns
from utils.window_icon import apply_icon
from utils.gpkg_io import write_gpkg_atomic as _write_gpkg

# ========================================
# CONFIGURATION
# ========================================
# Hardcoded (current behavior). Planned improvement: dynamic Global
# Mapper executable discovery. Actual implementation: separate future
# task -- see module docstring SIDE EFFECTS for the full note.
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"


# ========================================
# WINDOW CHROME HELPERS
# ========================================
def _remove_close_button(win):
    """
    Strips the titlebar's close (X) button (and system menu) via the
    Win32 API directly, ported from road_width.py's implementation.

    protocol("WM_DELETE_WINDOW", lambda: None) (used alongside this,
    see create_progress_window() below) only prevents the CLICK from
    doing anything -- the X itself stays fully visible, still
    highlights on hover, and still looks clickable, since Tkinter/the
    OS's own window chrome has no idea the close action has been
    neutralized, which reads as broken (a button that does nothing
    when clicked) rather than intentionally absent. This function is a
    stronger fix -- actually removing the button from the titlebar so
    there's nothing there to click in the first place.

    NOTE (carried over from road_width.py, still applies here):
    clearing WS_SYSMENU via GetWindowLongW/SetWindowLongW is the
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
# RUNTIME STATE
# ========================================
barangay_source = None
poi_source = None
output_mode = None

# Handoff globals between on_run() and run_processing() (D3a/D3c):
# built and validated in on_run(), consumed by run_processing() (which
# passes them into process_poi_counts_dynamic()). Kept as module-level
# globals because on_run() and run_processing() are separate functions
# (the latter is NOT nested inside open_main_window()), matching the
# same handoff mechanism barangay_source/poi_source/output_mode
# already use. Read-only from run_processing()'s own background worker
# thread as of Part 3's threading change -- see run_processing()'s
# docstring for why this is safe (on_run() never reassigns them while
# a run is in flight).
checked_categories = None      # {sanitized_key: "aerial" | "road"} -- CHECKED categories only
target_column_map = None       # {sanitized_key: "CAMA_NUM_..."} -- derive_target_columns()'s mapping
aerial_radius_meters = None    # only meaningful if any checked_categories value == "aerial"
road_radius_meters = None      # only meaningful if any checked_categories value == "road"
road_source = None             # ("local", (path, layer_or_None)) | ("db", (table,)) | None if unused
APP_ROOT = None
PROG_WIN = None
PROG_BAR = None
PROG_LABEL = None
PROG_STOP_FLAG = {"stop": False}

# ========================================
# OUTPUT-COLUMN CONFLICT DETECTION
# ========================================
# Output-column targets for this tool's conflict check are entirely
# dynamic -- there is no fixed list. _check_parcel_poi_conflicts()
# below takes its `targets` argument from the CURRENT run's own
# target_column_map (built in on_run()'s D3a validation, one
# CAMA_NUM_{KEY} name per checked category -- see derive_target_
# columns()), so the set of columns checked always matches exactly
# what this run is about to write. This mirrors road_frontage.py's /
# terrain.py's / land_shape_compactness.py's own conflict-check
# pattern, adapted from their fixed OUTPUT_COLUMN_TARGETS tuple to
# this tool's dynamic category set: ALL currently-checked targets are
# checked together, not just one, since they're one feature set
# computed in the same run -- an existing CAMA_NUM_POLICE_STATION
# column with no existing CAMA_NUM_PARK column still needs a conflict
# warning, to avoid an old CAMA_NUM_PARK value sitting alongside a
# freshly-computed CAMA_NUM_POLICE_STATION from a different
# run/computation.
#
# Cross-tool CAMA_ prefix standard: every column this tool CREATES gets
# a "CAMA_" prefix -- matches road_width.py's own CAMA_ROAD_WIDTH
# convention. Casing: this tool's original columns were lowercase
# ("num_police", etc.) -- the new names use ALL_CAPS to match every
# other tool's CAMA_-prefixed column naming convention in this project
# (confirmed project decision), not a plain lowercase prefix
# ("CAMA_num_police"). These targets check for the NEW, prefixed names
# ONLY -- never the OLD, unprefixed, lowercase names (e.g. a plain
# "num_police" column left over from a pre-CAMA_-prefix version of this
# tool). This tool never auto-detects, auto-removes, or
# auto-overwrites an old, non-prefixed column -- if one exists, it is
# simply left alone, untouched, and a NEW CAMA_-prefixed column is
# created alongside it. Only conflicts against the NEW naming scheme
# are ever surfaced to the user.
#
# Matching is EXACT (case-insensitive) -- "CAMA_NUM_POLICE" vs
# "NUM_POLICE" is not a match; only "cama_num_police"/"CAMA_NUM_POLICE"/
# "Cama_Num_Police"/etc. (same letters, any casing) count as the same
# column.
#
# _check_parcel_poi_conflicts() below requires its `targets` argument
# explicitly (no default) -- its only call site (on_run()'s PRIORITY 1
# block) always supplies the current run's own target_column_map
# values.

# parcel_output_column_overrides: {path_or_table: {"CAMA_NUM_POLICE":
# name, ...}} -- for any Land Parcel source (Local file OR Database
# table) where one or more pre-existing CAMA_-prefixed output columns
# were detected (see _check_parcel_poi_conflicts() below) and the user
# confirmed proceeding at Run time. Read by run_processing() and
# resolved (per checked category, via a dict comprehension against
# target_column_map -- see resolved_target_column_map in each
# per-source loop) into the exact existing column name(s), so the tool
# writes back into the EXACT existing column(s) (preserving original
# casing) instead of always writing the default CAMA_-prefixed name.
# A source with no entry here (or a target missing from its entry)
# uses that target's default CAMA_ name.
parcel_output_column_overrides = {}


# ========================================
# POI CATEGORY DISCOVERY (Task 1 -- Cluster A)
# ========================================
# Foundation for the dynamic, checklist-driven category system this
# tool now uses exclusively (process_poi_counts_dynamic(), D3c) --
# Cluster A originally established just what categories exist and what
# the user has checked, deliberately Tk-free/pure so later clusters
# (the dynamic counting engine, and the relocated conflict check) could
# call these three functions directly without depending on any
# Tkinter Variable -- which is exactly how they're used today.
def _sanitize_fclass_to_key(raw_value):
    """
    Normalizes and sanitizes one raw 'fclass' value into a column-safe,
    LOWERCASE bucket key, or returns None if the value must be
    excluded entirely.

    Adapted from meters_from_school_shop_transport_church.py's
    _sanitize_fclass_to_suffix() -- but NOT a direct reuse, per this
    tool's own spec (Task 1):
      - Returns a LOWERCASE key (that reference function returns
        UPPERCASE). This tool's checklist label IS the sanitized key,
        shown lowercase (Task 1, step 6) -- the "CAMA_NUM_" +
        upper() conversion happens only in derive_target_columns()
        below, at column-name time, not here.
      - Returns None (never a fallback "OTHER" bucket) for any value
        that sanitizes to zero letters (Task 1, step 4) -- e.g.
        None/NaN/empty-string input, or a raw value that sanitizes to
        pure digits (e.g. "123"). A mixed value like "school2" or "A1"
        still returns a key ("school2"/"a1"), since it contains at
        least one letter.

    Steps (order matters):
      1. pandas-NA/None check FIRST (see below) -- must happen before
         any str()/lower() call, since str(float('nan')) == "nan",
         which itself contains letters and would otherwise silently
         evade the "no letters" exclusion rule.
      2. str(raw_value).lower().strip().
      3. Collapse every run of non-alphanumeric characters into a
         single "_" (already-lowercased input, so only [^0-9a-z] needs
         collapsing).
      4. Strip leading/trailing "_".
      5. If the result contains no a-z letter at all, return None.

    Examples:
        "police station"     -> "police_station"
        "POLICE_Station"      -> "police_station"
        "police-STATION"      -> "police_station"
        "police&STATION"      -> "police_station"
        "A-B" / "A B" / "A_B" -> all "a_b"
        "school2"             -> "school2"   (kept -- contains a letter)
        "A1"                  -> "a1"        (kept -- contains a letter)
        "123" / "" / None     -> None        (excluded -- no letter)
    """
    if raw_value is None:
        return None
    try:
        if pd.isna(raw_value):
            return None
    except (TypeError, ValueError):
        # pd.isna() raises on some non-scalar inputs (e.g. an array) --
        # not an expected shape for a single fclass cell value, but
        # fail open (treat as present) rather than crash discovery over
        # a single malformed row.
        pass

    normalized = str(raw_value).lower().strip()
    sanitized = re.sub(r"[^0-9a-z]+", "_", normalized).strip("_")
    if not sanitized or not re.search(r"[a-z]", sanitized):
        return None
    return sanitized


def _discover_poi_categories(source_type, path_or_table):
    """
    Reads the given POI source fresh and groups every distinct raw
    'fclass' value into {sanitized_key: [raw_values...]} buckets via
    _sanitize_fclass_to_key() above (Task 1, step 5: raw values that
    sanitize to the SAME key are merged into ONE bucket -- deliberately
    no _A/_B letter-tier disambiguation, unlike the reference file's
    _assign_other_type_column_suffixes(), which is NOT reused here per
    the Task 1 spec).

    Never touches any Tkinter widget or variable -- safe to call from a
    background thread. Mirrors this file's own
    _check_parcel_poi_conflicts() contract and the reference file's
    _read_poi_fclass_values_worker() contract (source_type dispatch,
    self-contained DB credential loading, (result, error) return shape).

    Returns:
        tuple: (dict, None) on success. The dict maps each sanitized
        key to a sorted list of the distinct raw fclass values that
        produced it (kept for display/debugging only -- not required
        by any consumer yet). An EMPTY dict, ({}, None), means the
        source read successfully but contained zero eligible
        categories -- either because every 'fclass' value sanitized to
        nothing, OR because the source has no 'fclass' column at all
        (the latter is deliberately grouped into this SAME "zero
        eligible" case, not treated as a distinct failure -- see the
        "fclass" not in gdf.columns branch below for why). This is NOT
        treated as an error by this function; the caller
        (_poll_poi_category_queue()) routes an empty result to
        _handle_zero_eligible_categories() -- a modal dialog plus
        reverting the selection -- not to the informational-only
        failure handler.
        (None, error_message) on a genuine read failure (unreadable
        source, DB credential failure, a malformed file, etc.) -- the
        None/empty-dict distinction mirrors _check_parcel_poi_
        conflicts()'s own None-vs-empty-list distinction ("could not
        verify" vs "verified, nothing found").
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
    except Exception as e:
        return None, str(e)

    if "fclass" not in gdf.columns:
        # Deliberately ({}, None) -- "successfully read, zero eligible
        # categories" -- NOT (None, error). A POI source with no
        # 'fclass' column at all has, by definition, zero eligible
        # categories to discover; this is semantically the SAME
        # situation as "has an 'fclass' column but every value
        # sanitizes to nothing", not a distinct read-failure category.
        # Routing it here means it goes through the ALREADY-BUILT
        # _handle_zero_eligible_categories() flow (modal dialog +
        # revert-only-the-failed-mode) -- whose existing dialog text
        # ("Please make sure it has an 'fclass' column...") was
        # already written with exactly this scenario in mind.
        # Confirmed via explicit feedback: this previously fell
        # through to _handle_poi_category_discovery_failure() instead
        # (purely informational, does NOT clear the selection), which
        # is why "Could not read POI source." was shown but the file
        # selection was never reverted to "No file selected" the way
        # a genuine zero-eligible-categories source correctly is.
        return {}, None

    buckets = {}
    for raw_value in gdf["fclass"]:
        key = _sanitize_fclass_to_key(raw_value)
        if key is None:
            continue
        buckets.setdefault(key, set()).add(str(raw_value).strip())
    return {key: sorted(values) for key, values in buckets.items()}, None


def derive_target_columns(checked_keys):
    """
    Pure function, no Tkinter dependency: converts an iterable of
    checked sanitized category keys (e.g. the keys of a
    {key: BooleanVar} dict filtered to var.get() == True) into the
    dynamic output column name list, "CAMA_NUM_" + key.upper() per
    Task 1's naming rule (e.g. "police_station" ->
    "CAMA_NUM_POLICE_STATION").

    Deliberately independent of any GUI state, which is exactly why it
    could be called directly from on_run()'s D3a validation (built into
    target_column_map) without depending on a live Tkinter window. Does
    not itself sort or deduplicate -- a 1:1 mapping over whatever the
    caller passes in; ordering/dedup, if needed, is the caller's
    responsibility.
    """
    return [f"CAMA_NUM_{key.upper()}" for key in checked_keys]


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
    query = f'SELECT {col_str + "," if col_str else ""}"{geom_col}" AS geometry FROM "{schema}"."{table}"'
    return gpd.read_postgis(query, engine, geom_col="geometry")

# ========================================
# PROGRESS WINDOW HELPERS
# ========================================
def create_progress_window(root, total, title="Landmarks Within Meters Tool"):
    """
    Creates (destroying any previous instance first) the module-global
    progress window. Sets the module-level PROG_WIN/PROG_BAR/PROG_LABEL/
    PROG_STOP_FLAG globals. This function's own body is a plain,
    direct-Tkinter-call function, unchanged by Part 3's threading work
    -- as of Part 3, it is called only from run_processing()'s
    main-thread poller (_poll_run_processing_queue(), in response to a
    "new_source" message from the worker thread), never directly from
    a background thread and never from inside the per-source processing
    loop itself (that loop now runs on the worker thread, which
    contains no Tkinter calls of any kind).

    Args:
        root: parent Tk window.
        total (int): total item count, used as the progress bar maximum.
        title (str): window title.
    """
    global PROG_WIN, PROG_BAR, PROG_LABEL, PROG_STOP_FLAG

    try:
        if PROG_WIN and PROG_WIN.winfo_exists():
            PROG_WIN.destroy()
    except:
        pass

    PROG_STOP_FLAG = {"stop": False}

    PROG_WIN = tk.Toplevel(root)
    PROG_WIN.title(title)
    apply_icon(PROG_WIN, "landmarks200.ico")
    PROG_WIN.geometry("420x170")
    PROG_WIN.resizable(False, False)

    PROG_WIN.update_idletasks()
    sw = PROG_WIN.winfo_screenwidth()
    x = (sw // 2) - 210
    PROG_WIN.geometry(f"420x170+{x}+80")

    # Two-line, centered status text: a constant, friendly top line plus
    # a "current / total" count line below it -- deliberately general
    # rather than technical, for non-technical users. The per-parcel
    # category:count breakdown (msg, in update_progress() below) is
    # still computed and passed all the way through from
    # process_poi_counts_dynamic() unchanged, but is intentionally not
    # shown here anymore -- it was too technical for this dialog's
    # audience (e.g. "bank:0, department_store:0").
    PROG_LABEL = tk.Label(
        PROG_WIN,
        text=f"Counting nearby landmarks...\n0 / {total} parcels processed",
        justify="center")
    PROG_LABEL.pack(fill="x", padx=12, pady=(12, 6))

    PROG_BAR = ttk.Progressbar(PROG_WIN, orient="horizontal",
                                mode="determinate", maximum=total)
    PROG_BAR.pack(fill="x", padx=12, pady=(0, 6))

    # Cancel button and its on_cancel() handler removed entirely, per
    # explicit instruction: there is no reliable in-progress cancel for
    # this tool's actual workload (per-parcel Road-network routing over
    # a potentially large parcel/POI set), matching the same decision
    # already made for
    # road_width.py's own progress dialog. The X (close) button is
    # neutralized the same way road_width.py's is -- see
    # _remove_close_button()'s own docstring for why both the Win32
    # removal attempt AND the protocol() no-op fallback are used
    # together, and for the caveat that visible removal isn't
    # guaranteed on every Windows/DWM build (the protocol() fallback is
    # what actually guarantees clicking X does nothing, even then).
    PROG_WIN.protocol("WM_DELETE_WINDOW", lambda: None)
    _remove_close_button(PROG_WIN)

    PROG_WIN.transient(root)
    PROG_WIN.grab_set()
    PROG_WIN.attributes("-topmost", True)
    PROG_WIN.update_idletasks()
    PROG_WIN.update()


def update_progress(current, total, msg=None):
    """
    Updates the progress window created by create_progress_window().

    Args:
        current (int): items completed so far.
        total (int): total items expected.
        msg (str, optional): extra status text appended to the label.

    Returns:
        bool: False if the window no longer exists or PROG_STOP_FLAG
        has been set (signaling the caller to stop early), True
        otherwise (continue).
    """
    global PROG_WIN, PROG_BAR, PROG_LABEL, PROG_STOP_FLAG
    if not PROG_WIN or not PROG_WIN.winfo_exists():
        return False

    if PROG_STOP_FLAG.get("stop"):
        return False  # ← signal to stop

    PROG_BAR["value"] = current
    # msg (the category:count breakdown from process_poi_counts_dynamic())
    # is still received but intentionally unused here -- too technical
    # for this dialog; see create_progress_window()'s own comment on the
    # same decision.
    PROG_LABEL.config(
        text=f"Counting nearby landmarks...\n{current} / {total} parcels processed")

    if current == 1 or current == total or current % 5 == 0:
        PROG_WIN.update_idletasks()

    return True  # ← continue


def close_progress_window():
    """Destroys the current progress window, if any, and clears
    PROG_WIN. Safe to call even if no window is currently open."""
    global PROG_WIN
    try:
        if PROG_WIN and PROG_WIN.winfo_exists():
            PROG_WIN.grab_release()
            PROG_WIN.destroy()
    except:
        pass
    PROG_WIN = None


# ========================================
# PARCEL COLUMN-CONFLICT CHECK
# ========================================
# _check_parcel_poi_conflicts(): checks the selected Land Parcel
# source -- Local file OR Database table (extended to cover both as
# part of Fix 3; previously LOCAL-only) -- for pre-existing columns
# matching any of the CALLER-SUPPLIED `targets` (D3c: the current
# run's own dynamic target_column_map values, not a fixed list) --
# this tool is about to write its computed POI-count columns into
# those columns, and on_run() shows a combined confirmation dialog
# before proceeding, regardless of which source type was selected.
#
# This function itself, _check_parcel_poi_conflicts(), still runs
# synchronously -- it's called directly from on_run(), on the main
# thread, before win.destroy() and before run_processing() (and its
# worker thread) even exist. As of Part 3, run_processing()'s own
# per-source processing loop DOES run on a background worker thread
# (this file's own established worker-thread + queue.Queue() +
# .after()-polling pattern -- see run_processing()'s docstring), but
# that's a separate function with a separate call site; it doesn't
# change anything about how or when this check runs. This check still
# runs synchronously, called directly from on_run() right before Run
# actually starts -- same adaptation already applied in
# road_density.py, road_surface.py, terrain.py, and
# land_shape_compactness.py.
#
# Read approach: plain gpd.read_file(path) for a Local source, matching
# road_width.py's own canonical _read_gdf_worker() exactly -- no
# partial/schema-only read trick. For a Database source,
# read_postgis_clean() is used instead, loading its own creds/schema/
# engine (self-contained, matching the pattern already used by
# on_run()'s PRIORITY 3 block).
#
# A read failure here is NEVER treated as a column-conflict failure --
# it aborts the check (returns None for ALL sources, not just the one
# that failed -- a single unreadable source means the WHOLE check
# could not be verified, per this function's own None-vs-empty-list
# distinction documented in its docstring). on_run()'s PRIORITY 1
# treats a None result as "no known conflict, no overrides" and lets
# the run proceed -- the real read inside run_processing() further
# below remains solely responsible for surfacing that same read
# failure as a genuine, user-facing error at that point.
def _check_parcel_poi_conflicts(sources, source_type, targets):
    """
    Returns a list of (path_or_table, existing_output_cols) tuples on a
    SUCCESSFUL read/check -- one entry only for sources where at least
    one entry in `targets` was found as an existing column; an empty
    list means the check succeeded and found no conflict.
    existing_output_cols is the dict returned by
    detect_existing_output_columns() for that source (target name ->
    actual existing column name, original casing preserved). Returns
    None if credentials could not be loaded, or if ANY source failed to
    read -- this is a REQUIRED distinction, not cosmetic: an empty list
    means "verified, no conflict", while None means "could not verify
    at all".

    source_type: "local" or "db" -- dispatches to gpd.read_file() or
    read_postgis_clean() respectively.

    targets: the column names to check for (D3b/Task 8 addition,
    required as of D3c) -- the caller's own current target_column_map
    values, so this check always matches exactly what's actually about
    to be written for this run.
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
        existing_output_cols = detect_existing_output_columns(gdf, targets)
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
    apply_icon(dialog, "landmarks200.ico")
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
    apply_icon(dialog, "landmarks200.ico")
    dialog.title("POI PROXIMITY TOOL")
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
    apply_icon(dialog, "landmarks200.ico")
    dialog.title("POI PROXIMITY TOOL")
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
# MAIN PROCESSING
# ========================================
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
        GM_EXE_PATH is currently hardcoded to a developer/machine-
        specific absolute path (see CONFIGURATION section above and the
        module docstring's SIDE EFFECTS note) -- dynamic executable
        discovery is a planned, separately-scoped future improvement,
        not implemented here.
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

        EnumWindowsProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
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
# ROAD NETWORK SOURCE -- GPKG LAYER RESOLUTION (Task 5)
# ========================================
# Adapted from influence_map_distance_to_land_parcel.py's own
# resolve_local_fault_layer()/_list_gpkg_layers()/_prompt_select_layer()
# -- the same single-select, shown-once-at-selection-time layer-
# disambiguation pattern, applied here to the Road Network Source
# instead of that tool's Fault Line Map. Kept local to this file (Rule
# of Three, Section C) -- not extracted to a shared module even though
# this is the second tool to need this exact pattern.
def _list_road_gpkg_layers(path):
    """Returns the list of layer names inside a GeoPackage file."""
    import fiona
    return fiona.listlayers(path)


def _prompt_select_road_layer(parent, path, layers):
    """
    Explicit, blocking layer-selection dialog for an ambiguous
    multi-layer GeoPackage selected as the Road Network Source. Shown
    ONCE, at file-selection time (not at Run time, and not silently
    defaulted) -- mirrors influence_map_distance_to_land_parcel.py's
    _prompt_select_layer() exactly, single-select only (Document 1's
    explicit requirement -- never a multi-select).

    Returns the chosen layer name, or None if the user cancelled.
    """
    result = {"layer": None}

    dlg = tk.Toplevel(parent)
    apply_icon(dlg, "landmarks200.ico")
    dlg.title("Select Road Network Layer")
    dlg.resizable(False, False)
    dlg.grab_set()
    dlg.attributes("-topmost", True)

    tk.Label(
        dlg,
        text=(
            f"'{os.path.basename(path)}' contains {len(layers)} layers.\n"
            "Select the ONE layer to use as the Road Network source:"
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


def resolve_local_road_layer(parent, path):
    """
    Called once, at file-selection time (inside browse_road_files()
    below), for a local Road Network Source file. For a non-GPKG file,
    returns None (no layer concept). For a GPKG:
      - 1 layer -> used directly, no prompt.
      - >1 layers, exactly one matches the file stem -> used directly
        (same convention the reference file and this project's other
        tools already rely on).
      - >1 layers, no unambiguous stem match -> explicit
        _prompt_select_road_layer() dialog; returns None only if the
        user cancels (caller must then treat the whole file selection
        as cancelled, not silently fall back to a first-layer guess).
    """
    ext = os.path.splitext(path)[1].lower()
    if ext != ".gpkg":
        return None  # not applicable; single-layer format
    layers = _list_road_gpkg_layers(path)
    if not layers:
        raise ValueError(f"No layers found in GeoPackage: {path}")
    if len(layers) == 1:
        return layers[0]
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    stem_matches = [l for l in layers if l.lower() == stem]
    if len(stem_matches) == 1:
        return stem_matches[0]
    return _prompt_select_road_layer(parent, path, layers)


# ========================================
# ROAD GRAPH CONSTRUCTION (Task 6, Cluster D1)
# ========================================
# Three functions, adapted from meters_from_school_shop_transport_
# church.py's detect_prs92_zone() / graph_from_roads() / worker_
# process()'s nested _snap_to_road() -- kept local to this file (Rule
# of Three, Section C; Document 1's explicit instruction not to extract
# this to a shared utils module even though it's the second tool to
# need this general shape of logic).
#
# Deliberately did NOT connect to process_poi_counts() or run_
# processing() at the time this section was first written (D1 of a
# 3-part cluster: D1: this section; D2: process_poi_counts_dynamic()
# -- the rewrite that actually USES these; D3: run_processing()/
# on_run() wiring). At that point, the (since-removed) old process_
# poi_counts() still ran its original osmnx-based, fixed-4-category
# logic completely unaffected by anything in this section -- D3c has
# since fully retired that old function and wired this section's
# functions into process_poi_counts_dynamic()/run_processing() instead.
#
# ARCHITECTURAL SPLIT -- two different lifecycles, not one:
#   RUN-LEVEL (expensive, built ONCE per run):
#     edges_list, edge_geoms, edge_tree (STRtree) -- via
#     build_road_network_index() below. This is what the OLD osmnx-
#     based code's D1 performance bug got wrong (nearest_edges()
#     rebuilding a full spatial index on every single snap call); this
#     is the fix -- build the expensive spatial index exactly once.
#   PARCEL-LEVEL (cheap, rebuilt per parcel):
#     G_local (a plain nx.Graph populated from the already-computed
#     edges_list -- never re-parses road_gdf's geometry) and
#     edge_chains (per-parcel virtual-node bookkeeping) -- via
#     build_parcel_local_graph() below, called once per parcel,
#     paired with a fresh edge_chains = {} at that same point.
#     MUST be fresh per parcel: edge_chains tracks virtual nodes
#     spliced into specific road edges DURING THIS PARCEL's own
#     centroid+POI-candidate snapping; if it (or the virtual nodes it
#     produced in G_local) survived into the next parcel, that next
#     parcel's routing could be silently corrupted by an earlier,
#     unrelated parcel's snap points still sitting on the same edge.
PRS92_ZONE_BOUNDS = [
    (-180.0, 118.0, 3121, "Zone I"),
    (118.0, 120.0, 3122, "Zone II"),
    (120.0, 122.0, 3123, "Zone III"),
    (122.0, 124.0, 3124, "Zone IV"),
    (124.0, 180.0, 3125, "Zone V"),
]


def _detect_road_working_crs(labeled_gdfs):
    """
    Auto-detects the PRS92 zone EPSG code for the Road subsystem's OWN
    scoped projected working CRS, from the combined bounding-box
    midpoint longitude of one or more input GeoDataFrames.

    labeled_gdfs: list of (label, gdf) tuples, e.g.
        [("Land Parcel", gdf), ("POI", poi_gdf), ("Road Network", road_gdf)]
    The label is used only for diagnostics/error messages.

    Per approved decision 0.2(c): this working CRS is used ONLY by the
    Road-method distance pipeline (this section, plus D2's Dijkstra
    routing) -- it NEVER touches the Aerial method's existing
    EPSG:4326 + geopy.geodesic() mechanism, which stays completely
    untouched by this whole cluster. Adapted from meters_from_school_
    shop_transport_church.py's own detect_prs92_zone() -- same
    PRS92_ZONE_BOUNDS table (copied above, not imported -- Rule of
    Three), same total_bounds-based detection (not a unioned-geometry
    centroid, a known source of GEOS TopologyExceptions on real-world
    cadastral data with invalid geometries), same missing-CRS
    fallback/warning behavior.

    Raises ValueError if no valid (non-empty, geometry-bearing) input
    remains, or if any individual input's bounds are NaN (empty-but-
    non-null geometry slipping past a plain notna() check), naming the
    specific layer.
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
                  f"for the Road subsystem's working CRS, from combined "
                  f"bbox-midpoint longitude {center_lon:.4f}°E")
            return epsg

    raise ValueError(f"Could not determine PRS92 zone for longitude {center_lon}")


def build_road_network_index(road_gdf):
    """
    Builds the RUN-LEVEL (expensive, build-once) road network index:
    the flat edge list, per-edge LineString geometries, and the STRtree
    spatial index over them -- adapted from graph_from_roads(), but
    returns flat data instead of an nx.Graph (parcel-level graph
    construction is build_parcel_local_graph()'s job, below, on
    purpose -- keeps this run-level artifact containing ONLY road-
    network topology, with no parcel-specific virtual-node state ever
    mixed into it).

    PRECONDITION: road_gdf is ALREADY reprojected to the detected
    working CRS (_detect_road_working_crs() above) by the caller. The
    edge-length computation below (Point(u).distance(Point(v))) is
    Euclidean, which is only meaningful in a projected CRS -- this
    function does not reproject anything itself.

    Geometry handling (Task 6's explicit requirements):
      - Only LineString / MultiLineString geometry is used. Non-line
        geometry (Polygon, Point, etc.) is silently skipped, never an
        error.
      - If road_gdf has NO LineString/MultiLineString geometry type at
        all, raises immediately, naming the geometry types actually
        found.
      - Tightened beyond the reference's own check: it is also
        possible for road_gdf to carry a LineString-labeled geom_type
        while every individual row is None/empty/malformed enough to
        be skipped during the actual per-row extraction below --
        passing the coarse geom_type check but still producing ZERO
        usable segments. That case is explicitly checked for AFTER the
        extraction loop and raises too -- it must not silently become
        a "successful" empty index; a road source with no usable line
        network is the same practical failure either way.

    Returns:
        tuple: (edges_list, edge_geoms, edge_tree) --
          edges_list: list of (u, v, length) tuples, u/v as (x, y)
            coordinate tuples, length in the working CRS's own units
            (meters, given a correctly-detected PRS92 zone).
          edge_geoms: list of shapely LineString objects, ONE PER
            ENTRY IN edges_list, at the SAME INDEX -- edges_list[i]
            and edge_geoms[i] always describe the same edge. This
            parallel-array invariant is what makes the STRtree lookup
            below deterministic (see build_parcel_local_graph()'s
            sibling function, snap_point_to_road(), for exactly how
            that mapping is used).
          edge_tree: shapely.strtree.STRtree built once over
            edge_geoms -- the expensive spatial index this whole
            function exists to build exactly once per run, reused
            (never rebuilt) for every snap query across every parcel.

    Raises:
        ValueError: no LineString/MultiLineString geometry found at
        all (names the types found), OR usable geometry types were
        present but zero actual segments could be extracted from them.
    """
    edges_list = []
    edge_geoms = []

    geom_types = road_gdf.geometry.geom_type.dropna().unique().tolist()
    print(f"ℹ️ Road geometry types found: {geom_types}")

    if not any(t in ("LineString", "MultiLineString") for t in geom_types):
        raise ValueError(
            f"Road Network Source has no LineString geometry.\n"
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
                    edges_list.append((u, v, float(length)))
                    edge_geoms.append(LineString([u, v]))
        except Exception:
            continue

    if not edge_geoms:
        # Tightened check (see docstring above): the coarse geom_type
        # scan passed, but nothing usable actually survived extraction
        # -- still a "no usable road network" failure, not a quietly-
        # empty success.
        raise ValueError(
            f"Road Network Source's geometry type(s) {geom_types} did not "
            f"yield any usable line segments after processing.\n\n"
            f"Please select a source with valid, non-empty line geometry."
        )

    edge_tree = STRtree(edge_geoms)
    return edges_list, edge_geoms, edge_tree


def build_parcel_local_graph(edges_list):
    """
    PARCEL-LEVEL (cheap, build-once-per-parcel): a fresh nx.Graph()
    populated from the already-computed, run-level edges_list -- never
    re-parses road_gdf's geometry (that already happened once, in
    build_road_network_index() above).

    Always pair a call to this function with a fresh edge_chains = {}
    at the same point in the caller (D2) -- see snap_point_to_road()'s
    own docstring below for exactly why this pairing matters: virtual
    nodes/chain state from one parcel must never survive into the
    next.

    Undirected (networkx.Graph(), not DiGraph()) -- Task 6's explicit,
    deliberate decision (BLGF road data is known to be incomplete; a
    directed graph would require a oneway-style attribute this tool
    does not require of the uploaded road source). Scoped to this tool
    only -- does not affect meters_from_school_shop_transport_church.py's
    own graph construction.
    """
    G_local = nx.Graph()
    for u, v, length in edges_list:
        G_local.add_edge(u, v, length=float(length))
    return G_local


def snap_point_to_road(G_local, edge_tree, edge_geoms, edges_list, point_xy, edge_chains):
    """
    Projects point_xy onto its true nearest point on the road network
    (nearest-point-ON-a-segment -- interpolated along the segment if
    that is closer than any existing vertex, not restricted to
    existing vertices only) and returns the (x, y) coordinate tuple to
    use as a G_local routing endpoint, mutating G_local and
    edge_chains in place as needed.

    edge_chains CONTRACT (per-parcel mutable state, keyed by the
    persistent edge index -- NOT geometry equality): a dict mapping
    edge_idx (an int, the SAME index that identifies that edge in both
    edges_list and edge_geoms -- see build_road_network_index()'s own
    parallel-array invariant) to an ORDERED list of
    (position_along_edge, node_id) tuples, sorted by position. Seeded
    lazily, the first time a point lands on a given edge this parcel,
    with that edge's own two original endpoints at positions 0 and
    edge_length. A second point landing on the SAME edge later in the
    same parcel's processing is spliced into the existing chain
    between its immediate left/right neighbors (with correct sub-
    distances), and the single now-superseded direct edge between
    those two neighbors is removed -- so two points on the same road
    segment always route at their true along-segment distance from
    each other, not via that segment's far endpoints. THIS DICT MUST
    BE FRESH ({}) FOR EVERY PARCEL -- see build_parcel_local_graph()'s
    docstring on why letting it (or the virtual nodes it produced)
    survive into the next parcel would silently corrupt that parcel's
    routing.

    Deterministic STRtree -> edge identity mapping: edge_tree.nearest()
    returns either a plain int index (current shapely) or the geometry
    object itself (older shapely) -- handled explicitly below so
    edge_idx always ends up as the correct integer index into BOTH
    edges_list and edge_geoms, never a fragile geometry-equality
    lookup that could resolve to the wrong edge if two edges happen to
    share identical coordinates.

    Collision handling: if the projected coordinate already matches an
    existing node in G_local -- an original road vertex, or a virtual
    node inserted earlier for THIS SAME parcel -- that existing node is
    reused as-is; no new node or edges are inserted, avoiding both an
    unintended overwrite of that node's existing edges and a zero-
    length self-loop.

    Returns:
        tuple[float, float] | None: the node ID to route to/from, or
        None if the edge lookup or projection fails for any reason
        (including edge_tree being falsy/empty -- defensive only;
        build_road_network_index() above never returns a successful-
        but-empty index, so this should be unreachable in practice,
        not a normal production path).

        IMPORTANT -- this function does NOT decide reachability or
        compute route distance, and a None return must NEVER be
        treated by the caller as "fall back to straight-line distance"
        -- Task 7's explicit requirement is that an unreachable Road-
        method POI is simply not counted, with no fallback of any
        kind. D1 (this section) only ever answers "what road-network
        node represents this point?" -- D2 (process_poi_counts_
        dynamic()) is what decides reachability (nx.has_path()) and
        computes the route distance (nx.bidirectional_dijkstra()), and
        is where Task 7's "no fallback" rule is actually enforced.
    """
    if not edge_tree or not edge_geoms:
        return None
    try:
        nearest = edge_tree.nearest(Point(point_xy))
        if isinstance(nearest, (int, np.integer)):
            edge_idx = int(nearest)
        else:
            # Older shapely returns the geometry itself; recover its
            # index for the parallel edges_list/edge_geoms lookup.
            edge_idx = edge_geoms.index(nearest)

        edge_line = edge_geoms[edge_idx]
        eu, ev, elen = edges_list[edge_idx]

        projected = nearest_points(Point(point_xy), edge_line)[1]
        node_id = (float(projected.x), float(projected.y))

        # Coordinate-based collision check FIRST, independent of which
        # edge_idx this lookup landed on -- if this exact point is
        # already a node anywhere in G_local (original vertex or
        # earlier virtual node THIS parcel), reuse it outright rather
        # than touching any chain bookkeeping.
        if node_id in G_local:
            return node_id

        chain = edge_chains.get(edge_idx)
        if chain is None:
            # First point on this edge THIS parcel -- seed the chain
            # with the edge's own original endpoints. The base edge
            # (eu, ev, elen) already exists in G_local from
            # build_parcel_local_graph() above, so nothing needs to
            # change in the graph yet.
            chain = [(0.0, eu), (float(elen), ev)]
            edge_chains[edge_idx] = chain

        proj_dist = float(edge_line.project(projected))
        positions = [c[0] for c in chain]
        insert_at = bisect.bisect_left(positions, proj_dist)

        # nearest_points() guarantees proj_dist falls within
        # [0, elen], and the chain always spans that full range
        # (seeded with both endpoints), so a left AND a right
        # neighbor always exist here.
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


# ========================================
# DYNAMIC POI COUNTING (Task 6/7, Cluster D2)
# ========================================
# process_poi_counts_dynamic() below is the counting engine: dynamic
# checked categories (Task 1/2), per-category Aerial/Road method
# (Task 3), independent Aerial/Road radii (Task 4), and Road-method
# reachability with NO straight-line fallback (Task 7) -- built on
# D1's road-graph primitives above.
#
# When first written (D2), this was deliberately a NEW function, not a
# rewrite of the old fixed-4-category process_poi_counts() -- run_
# processing() continued calling that OLD function with its original
# signature, completely unaffected by anything in this section, for
# the remainder of D2 and all of D3a/D3b. D3c is what swapped run_
# processing()'s call site to this function and retired the old one
# entirely (Task 2's actual removal) -- no compatibility shim/default
# arguments were ever added to bridge the two signatures in the
# meantime, avoiding an ambiguous, half-migrated function whose
# behavior would depend on which argument set was supplied.
class RoadContext(NamedTuple):
    """
    Structured, ONCE-PER-RUN road-network context -- built by D3 (in
    run_processing()) via build_road_network_index() (D1 above) and
    passed into process_poi_counts_dynamic() below, which never
    constructs one itself. Named fields (not a positional tuple) so
    every access is self-documenting -- road_context.working_epsg, not
    road_context[3].

    edges_list, edge_geoms, edge_tree: the three run-level artifacts
        from build_road_network_index() -- expensive to build (road
        geometry parsing, spatial index construction), so built
        exactly once per run and reused for every parcel, never
        rebuilt per snap call (the original osmnx-based D1 performance
        bug this whole redesign exists to fix).
    working_epsg: the PRS92 zone EPSG code (from
        _detect_road_working_crs()) that edges_list/edge_geoms are
        expressed in -- the projected working CRS the Road subsystem
        uses internally, per approved decision 0.2(c). Every parcel
        centroid and POI point must be reprojected to THIS EPSG before
        being passed to snap_point_to_road() below -- see process_poi_
        counts_dynamic()'s own CRS-handling section for exactly where
        that reprojection happens (a scratch copy, never the main gdf/
        poi_gdf that Aerial distance and the final output use).
    """
    edges_list: list
    edge_geoms: list
    edge_tree: object
    working_epsg: int


def _geodesic_bbox_envelope(centroid_lat, centroid_lon, margin_m):
    """
    Conservative geographic bounding envelope for the POI candidate
    prefilter, computed via geopy.distance.geodesic().destination() --
    the SAME ellipsoidal geodesic function already used for the Aerial
    distance check elsewhere in this file (ox... no relation to osmnx;
    this is the `from geopy.distance import geodesic` already imported
    at module top), not a second, independent distance approximation.

    Returns (min_lon, min_lat, max_lon, max_lat) -- the rectangle
    formed by the destination points at bearing 0°/90°/180°/270°
    (margin_m away from the centroid, due north/east/south/west).

    Empirically verified (not a formal mathematical proof) across 360
    bearings, multiple radii (200 m to 50 km), and multiple latitudes
    spanning the Philippines' operating area, that this rectangle
    fully contains the true geodesic circle of radius margin_m in
    every direction -- satisfying the required prefilter invariant
    (actual possible coordinate extent must be a subset of the
    computed bbox, so a legitimate candidate can never be silently
    excluded before the real distance check ever sees it).

    SCOPE / LIMITATIONS -- deliberately NOT a general-purpose global
    utility:
      - Longitude wraparound at ±180° is NOT handled. Not a practical
        concern for this tool's Philippine operating area (roughly
        117°E-127°E), but this function must not be copied elsewhere
        or treated as universally correct without adding that
        handling first.
      - This is a PREFILTER ONLY. The returned bbox is used solely to
        narrow the candidate POI set via a fast bounding-box query
        (gdf.cx[...]) -- it is NEVER used as the actual counted
        distance. Aerial-method counting still uses exact geodesic()
        distance; Road-method counting still uses the projected snap +
        network routing distance from D1. This function's only job is
        "don't exclude anyone who might matter," not "decide who's in
        range."
    """
    north = geodesic(meters=margin_m).destination((centroid_lat, centroid_lon), bearing=0)
    south = geodesic(meters=margin_m).destination((centroid_lat, centroid_lon), bearing=180)
    east = geodesic(meters=margin_m).destination((centroid_lat, centroid_lon), bearing=90)
    west = geodesic(meters=margin_m).destination((centroid_lat, centroid_lon), bearing=270)
    return west.longitude, south.latitude, east.longitude, north.latitude


def process_poi_counts_dynamic(gdf, poi_gdf, checked_categories, aerial_radius_m,
                                road_radius_m, road_context, target_column_map,
                                progress_cb=None):
    """
    Counts landmarks per parcel using the dynamic, per-category
    Aerial/Road pipeline (Tasks 1/2/3/4/6/7) -- this tool's only
    counting engine as of D3c, having replaced the original fixed
    police/park/mall/others counting function (retired entirely in
    D3c; this function was written to match its contract/conventions
    where they still made sense, back when both briefly coexisted
    during D2/D3).

    Args:
        gdf: Land Parcel GeoDataFrame. Its own CRS is preserved and
            restored at the end -- see the "CRS handling" section
            below for the precise sequence.
        poi_gdf: POI GeoDataFrame. Must have an 'fclass' column.
        checked_categories: {sanitized_key: "aerial" | "road"} --
            ONLY the categories the user actually checked. A POI whose
            sanitized fclass key is not a key in this dict is excluded
            entirely (Task 2 -- no catch-all "others" bucket).
        aerial_radius_m, road_radius_m: independent radii (Task 4),
            applied respectively to every "aerial"-method and "road"-
            method checked category. Either may be None -- on_run()
            leaves whichever method has no checked category at None
            (Step 4/5) -- but not both, since checked_categories is
            never empty by the time this function is called (on_run()
            requires at least one checked category), so at least one
            of the two is always a real float.
        road_context: a RoadContext (above), or None if no checked
            category currently uses the "road" method. Built ONCE per
            run by the caller (run_processing(), D3c) -- this function
            never constructs one itself, and never rebuilds any part
            of it per parcel.
        target_column_map: {sanitized_key: column_name} -- AUTHORITATIVE.
            This function looks up target_column_map[key] to decide
            where to write each category's count; it never regenerates
            a column name (e.g. never independently constructs
            "CAMA_NUM_" + key.upper()) -- that naming decision belongs
            entirely to the caller that built this map (run_processing(),
            with Task 8's per-source override logic already applied).
        progress_cb: optional callable(current, total, msg=...) ->
            bool -- a False return cancels processing early.

    Returns:
        gdf, with one new column per entry in target_column_map,
        reprojected back to its original CRS.

    CRS handling (per approved decision 0.2(c) -- see this section's
    header comment): TWO independent CRS treatments happen here, never
    mixed:
        MAIN gdf/poi_gdf -- reprojected to EPSG:4326. Aerial-method
            distance uses geopy.geodesic() on these 4326 coordinates.
            The dynamic count columns are written onto THIS gdf, which
            is what gets reprojected back to original_crs at the end
            -- never the projected scratch copies below.
        SCRATCH projected copies (gdf_road / poi_gdf_road) -- built
            ONLY if at least one checked category uses "road" AND
            road_context is not None, reprojected to road_context.
            working_epsg. Used ONLY for snap_point_to_road()/Dijkstra
            (D1's functions, which require projected/Euclidean
            coordinates as a precondition -- they never reproject
            anything themselves). Discarded after use; never written
            to the output, never restored to any other CRS -- there is
            nothing to "restore" since the main gdf/poi_gdf were never
            mutated into this projected form in the first place.

    Road-method reachability (Task 7 -- no fallback, ever):
        snap_point_to_road() returns None       -> not counted
        nx.has_path() is False                  -> not counted
        has_path()/Dijkstra raises a routing-
            specific exception (NetworkXNoPath,
            NodeNotFound)                        -> not counted
        route distance > road_radius_m           -> not counted
        route distance <= road_radius_m          -> counted
        ANY OTHER exception (a genuine bug/
            structural failure, e.g. a bad
            target_column_map entry)              -> PROPAGATES, caught
                                                      by run_processing()'s
                                                      existing outer
                                                      try/except and shown
                                                      as an error dialog,
                                                      never silently
                                                      swallowed as "no
                                                      count"
    """
    print(f"🚀 Starting dynamic POI count processing "
          f"(aerial radius = {aerial_radius_m}m, road radius = {road_radius_m}m)...")

    original_crs = gdf.crs
    gdf = gdf.to_crs(4326)
    poi_gdf = poi_gdf.to_crs(4326)

    # Dynamic classification (Task 1/2): sanitize every raw fclass
    # value, keep ONLY rows whose sanitized key is a checked category.
    # No "others" bucket -- an unchecked/unrecognized key's POIs are
    # simply excluded from candidacy entirely, never counted anywhere.
    poi_gdf = poi_gdf.copy()
    poi_gdf["_sanitized_key"] = poi_gdf["fclass"].apply(_sanitize_fclass_to_key)
    poi_gdf = poi_gdf[poi_gdf["_sanitized_key"].isin(checked_categories.keys())]

    # Initialize one output column per CHECKED category, all starting
    # at 0 -- this tool's own long-standing "start every count at zero"
    # convention (unchanged from the original fixed-4-category
    # implementation this function replaced).
    # Iterates checked_categories (not target_column_map.values()) so
    # a missing target_column_map entry for a checked category fails
    # immediately here, before any per-parcel processing runs, rather
    # than only surfacing partway through the first parcel's write-back
    # (see the write-back loop further below for the full contract
    # rationale).
    for key in checked_categories:
        gdf[target_column_map[key]] = 0

    any_road_checked = any(m == "road" for m in checked_categories.values())
    have_road_context = any_road_checked and road_context is not None

    # SCRATCH projected copies -- ONLY built if actually needed (Task
    # 5's requirement mirrored here: no road_context work at all when
    # every checked category is Aerial). Never touches the main
    # gdf/poi_gdf above -- see this function's own CRS-handling
    # docstring section.
    if have_road_context:
        gdf_road = gdf.to_crs(road_context.working_epsg)
        poi_gdf_road = poi_gdf.to_crs(road_context.working_epsg)
    else:
        gdf_road = None
        poi_gdf_road = None

    # bbox_margin_m: the shared candidate-prefilter margin (see
    # _geodesic_bbox_envelope()'s own docstring for why max() of the two
    # radii is a valid shared bound when both are in use). Either
    # aerial_radius_m or road_radius_m may be None -- on_run() leaves
    # whichever method has no checked category at None (Step 4/5) -- so
    # this filters None out before taking max(), rather than assuming
    # both are always floats. At least one of the two is always a real
    # float, since checked_categories is never empty by the time this
    # function is called (on_run() requires at least one checked
    # category, and every checked category is either "aerial" or
    # "road").
    bbox_margin_m = max(v for v in (aerial_radius_m, road_radius_m) if v is not None)

    total = len(gdf)
    for pos, (idx, row) in enumerate(gdf.iterrows()):
        centroid = row.geometry.centroid
        lat, lon = centroid.y, centroid.x

        # ONE common candidate prefilter for both methods (see
        # _geodesic_bbox_envelope()'s own docstring for why max() of
        # the two radii is a valid shared bound: road-network path
        # distance can never be shorter than straight-line distance,
        # so a POI outside road_radius_m in a straight line cannot be
        # within road_radius_m via the network either).
        minx, miny, maxx, maxy = _geodesic_bbox_envelope(lat, lon, bbox_margin_m)
        candidates = poi_gdf.cx[minx:maxx, miny:maxy]

        # PARCEL-LEVEL road graph state (D1's own lifecycle contract):
        # fresh G_local + fresh edge_chains for EVERY parcel, built
        # from the RUN-LEVEL edges_list (never re-parsed). start_node
        # is this parcel centroid's own snap -- computed once here,
        # reused for every Road-method candidate below (mirrors the
        # reference's own "compute the centroid snap once per call"
        # optimization).
        G_local = None
        edge_chains = None
        start_node = None
        if have_road_context:
            G_local = build_parcel_local_graph(road_context.edges_list)
            edge_chains = {}
            centroid_road = gdf_road.geometry.iloc[pos].centroid
            start_node = snap_point_to_road(
                G_local, road_context.edge_tree, road_context.edge_geoms,
                road_context.edges_list, (centroid_road.x, centroid_road.y),
                edge_chains)

        counters = {key: 0 for key in checked_categories}

        for cand_idx, cand in candidates.iterrows():
            key = cand["_sanitized_key"]
            method = checked_categories[key]
            poi_lat, poi_lon = cand.geometry.y, cand.geometry.x

            if method == "aerial":
                dist = geodesic((lat, lon), (poi_lat, poi_lon)).meters
                if dist <= aerial_radius_m:
                    counters[key] += 1

            elif method == "road":
                if not have_road_context or start_node is None:
                    continue  # Task 7: no road context/no start snap -> not counted

                cand_road_geom = poi_gdf_road.geometry.loc[cand_idx]
                end_node = snap_point_to_road(
                    G_local, road_context.edge_tree, road_context.edge_geoms,
                    road_context.edges_list,
                    (cand_road_geom.x, cand_road_geom.y), edge_chains)
                if end_node is None:
                    continue  # Task 7: snap failed -> not counted, no fallback

                # NARROW exception boundary (per explicit correction):
                # only routing-specific failures are treated as "not
                # counted" -- anything else (a genuine bug) propagates
                # up to run_processing()'s existing error handling,
                # never silently swallowed.
                try:
                    if nx.has_path(G_local, start_node, end_node):
                        dist, _ = nx.bidirectional_dijkstra(
                            G_local, start_node, end_node, weight="length")
                    else:
                        continue  # Task 7: unreachable -> not counted, no fallback
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue  # Task 7: routing failure -> not counted, no fallback

                if dist <= road_radius_m:
                    counters[key] += 1
                # else: exceeds road_radius_m -> not counted (Task 7,
                # no fallback -- this is the same "distance computed
                # but too far" case as Aerial's own `if dist <=` check
                # just above, not a failure case at all)

        # Iterates checked_categories (not target_column_map.items())
        # and directly indexes target_column_map[key] -- this is
        # deliberate, not equivalent to the reverse. checked_categories
        # and target_column_map are a matched pair the caller (D3) must
        # always build together, consistently; if a checked category
        # somehow has no corresponding target_column_map entry, that IS
        # a structural bug (the contract this function relies on was
        # violated) and target_column_map[key] correctly raises
        # KeyError for it -- this must propagate to run_processing()'s
        # existing error handling, never be silently dropped the way
        # iterating target_column_map.items() would (any checked key
        # missing from target_column_map would just never get written,
        # with no error at all -- confirmed as a real bug via testing,
        # not a hypothetical one).
        for key in checked_categories:
            gdf.at[idx, target_column_map[key]] = counters.get(key, 0)

        print(f"✅ Feature {pos + 1}: " +
              ", ".join(f"{key}={counters.get(key, 0)}" for key in checked_categories))

        if progress_cb:
            should_continue = progress_cb(
                pos + 1, total,
                msg=", ".join(f"{key}:{counters.get(key, 0)}" for key in checked_categories)
            )
            if should_continue is False:
                print("⛔ Processing cancelled by user.")
                if original_crs is not None:
                    gdf = gdf.to_crs(original_crs)
                return gdf

    if original_crs is not None:
        gdf = gdf.to_crs(original_crs)
    return gdf


# ========================================
# MAIN WINDOW
# ========================================
def open_main_window(root):
    """
    Builds and shows the tool's single unified configuration window:
    Land Parcel and POI source pickers (each with a Local-file/
    Database-table radio toggle), a dynamic Landmark Categories
    checklist with per-category Aerial/Road method selection
    (discovered from the POI source, see _refresh_poi_categories()),
    independent Aerial/Road distance entries, a Road Network Source
    picker (Local-file/Database-table, used only if any checked
    category selects Road), an Output destination picker, and a Run
    button gated by _update_run_button_state().

    Args:
        root: the parent Tk root this window is opened under.
    """
    win = tk.Toplevel(root)
    apply_icon(win, "landmarks200.ico")
    win.title("Landmarks Within Meters Tool")
    win.resizable(False, False)
    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))

    def _reflow_window():
        """
        Safety net for the Landmark Categories section's dynamic-height
        content (category_status_row / category_checklist_outer being
        shown/hidden as the POI source is selected, toggled, read, or
        rejected for zero eligible categories) -- combined with
        win.resizable(False, False) above. Adapted from influence_map_
        to_land_parcel.py's own verified-on-Windows _reflow_window()
        (see its docstring: "what previously left a stale/unpainted
        region behind whenever dynamic content was packed/unpacked" --
        the exact same class of symptom reported here: toggling Local
        -> Database left Local's checklist size/space behind even
        though its widgets were correctly hidden).

        Resets minsize/maxsize to permissive bounds FIRST, before
        remeasuring -- this is the one place a naive port of the
        reference pattern breaks: a PREVIOUS call's win.minsize(req_w,
        req_h) becomes a hard floor Tk will never shrink below on a
        LATER call, so simply re-measuring and re-setting minsize/
        maxsize/geometry to a new, smaller value would silently do
        nothing without first lifting that earlier floor.

        Called only in response to an actual category_status_row/
        category_checklist_outer visibility change (never on a timer
        or repeating event) -- see _set_poi_category_reading_state(),
        _poll_poi_category_queue(), _refresh_poi_categories(), and
        _handle_zero_eligible_categories() below. This window has never
        called .geometry() before this feature existed -- like the
        reference file's own window, it has only ever relied on
        pack()'s automatic initial sizing, so this only takes effect
        from the first time the Landmark Categories section actually
        changes visibility onward; the window's very first on-screen
        size (before any POI source is even selected) is unaffected.

        VERIFICATION NOTE: this exact pattern is already proven correct
        on real Windows in this same codebase (influence_map_to_land_
        parcel.py, road_width.py, POI_All_Distance.py all use it for
        the identical symptom). It could NOT be cleanly re-confirmed in
        this sandbox -- headless Xvfb testing reproduced the "window
        doesn't shrink" symptom consistently, but even fully destroying
        the hidden widget subtree (which should be structurally
        impossible to still influence a live ancestor's geometry) did
        not clear it, and installing a minimal window manager (twm) to
        rule out a no-WM Xvfb artifact caused the test process to hang
        rather than resolve -- both point at an environment-specific
        limitation of this sandbox's headless X server, not a defect
        in this specific fix, but this could not be proven conclusively
        here. Please verify the actual Local<->Database toggle
        behavior on the real machine.
        """
        win.minsize(1, 1)
        win.maxsize(10000, 10000)
        win.update_idletasks()
        req_w = win.winfo_reqwidth()
        req_h = win.winfo_reqheight()
        win.minsize(req_w, req_h)
        win.maxsize(req_w, req_h)
        win.geometry(f"{req_w}x{req_h}")

    # Hides the dotted focus-indicator rectangle that ttk.Radiobutton
    # (used for the Aerial/Road method radios, Cluster B) draws around
    # itself on click -- explicit feedback: it read as a rendering
    # glitch/broken box, not an intentional focus cue, in this
    # single-purpose tool. Matching focuscolor to the widget's own
    # background makes the ring invisible rather than disabling focus
    # outright, so keyboard Tab-navigation between radios still works
    # exactly as before -- only the visible dotted outline is gone.
    # ttk.Style shares one process-wide style registry (passing `win`
    # here doesn't create an isolated namespace scoped just to this
    # window) -- but since this tool runs as its own isolated
    # subprocess (Section A: MAIN.py dispatches each tool as a
    # separate process), this change can never reach any other tool's
    # GUI regardless.
    #
    # theme_use("clam") FIRST -- confirmed via explicit feedback that
    # the focuscolor override alone did NOT actually hide the ring on
    # a real Windows machine, even though it worked in this sandbox's
    # Xvfb testing. Root cause: Windows' default active ttk theme
    # ("vista"/"xpnative") renders focus indicators through native
    # Windows UxTheme APIs and largely ignores synthetic ttk.Style.
    # configure() properties such as focuscolor -- the override was
    # being silently no-opped by the native theme. "clam" is a pure-Tk
    # theme (not OS-native-rendered) that DOES respect Python-
    # configured style properties, so switching to it first is what
    # actually makes the focuscolor override take effect. The only
    # other ttk widget in this window is ttk.Separator (the thin
    # horizontal divider lines) -- its appearance under "clam" is
    # still a plain line, no meaningful visual change there.
    _style = ttk.Style(win)
    _style.theme_use("clam")
    # Explicit correction: switching to "clam" (above) fixed the
    # focus-ring issue, but introduced a NEW visible artifact --
    # "clam"'s own baked-in TRadiobutton background color (its
    # default, independent of this window) doesn't match this
    # window's actual background, so each radio button showed a
    # faint but clearly visible colored box/patch behind it, even
    # when grayed out/disabled -- confirmed via explicit feedback.
    # Fixed by explicitly matching BOTH the normal and disabled-state
    # background (and focuscolor, unchanged from before) to this
    # window's own real background color (win.cget("bg")), rather
    # than trusting "clam" theme's own default value to already
    # match -- it does not.
    _win_bg = win.cget("bg")
    _style.configure("TRadiobutton", background=_win_bg, focuscolor=_win_bg)
    _style.map("TRadiobutton", background=[("disabled", _win_bg), ("active", _win_bg)])

    # ── state ────────────────────────────────────────────────────
    parcel_source_type = tk.StringVar(master=win, value="local")
    poi_source_type    = tk.StringVar(master=win, value="local")
    output_dest_type   = tk.StringVar(master=win, value="local")

    # Single-selection architecture: one local file and one DB table
    # may exist in memory at any time. Authority variables -- all GUI
    # labels and run-button state are derived from them, never the reverse.
    parcel_local_path = None   # authority: single local file path
    parcel_db_table   = None   # authority: single DB table name
    poi_local_path     = tk.StringVar(master=win)
    poi_db_table       = tk.StringVar(master=win)
    output_local_dir   = tk.StringVar(master=win)

    # Road Network Source (Task 5) -- plain authority variables,
    # mirroring parcel_local_path/parcel_db_table's pattern rather than
    # POI's tk.StringVar pattern, since this section sits structurally
    # right below Land Parcel Source (its closest sibling) and needs
    # one extra piece of state (road_local_layer) that Land Parcel
    # doesn't. Feeds _update_run_button_state()'s has_road_source gate
    # ("blocked if a checked category is Road AND no road source is
    # selected") alongside road_source_type.
    road_source_type = tk.StringVar(master=win, value="local")
    road_local_path  = None   # authority: single local file path
    road_local_layer = None   # resolved GPKG layer name, or None (non-GPKG / single-layer)
    road_db_table    = None   # authority: single DB table name

    # Land Parcel existing-output-column check: detect-on-select,
    # matching the pattern established in lot_location.py/road_width.py/
    # road_frontage.py/road_density.py/road_surface.py/
    # influence_to_map.py/land_shape_compactness.py/terrain.py.
    # Deliberately does NOT cache the result across calls -- every
    # selection AND every Local/Database toggle triggers a fresh read
    # (see group-05-cache-removal-analysis.md). What IS still remembered
    # per mode is only WHICH file/table is selected (parcel_local_path /
    # parcel_db_table above), a separate concern.
    #
    # D3b (Task 8): the existing-output-column conflict check itself --
    # previously run in the background the moment a Land Parcel source
    # was selected/toggled (parcel_is_reading / parcel_existing_output_
    # conflicts, and the 4 functions that managed them) -- was
    # relocated to run synchronously instead. It now runs in on_run(),
    # as PRIORITY 1, BEFORE win.destroy() and BEFORE PRIORITY 2/3 --
    # not inside run_processing() (that placement was tried briefly but
    # corrected: a "No" response there could never actually return the
    # user to their configuration, since win.destroy() had already run
    # by the time run_processing() is even reached). Checks against the
    # DYNAMIC target_column_map's own column names (built earlier in
    # on_run()'s own D3a validation block), not any fixed list. There
    # is no more "reading" state to track here, and no more result to
    # cache between selection and Run -- the check now only ever runs
    # once, synchronously, at the moment Run Processing is actually
    # clicked. See on_run()'s own PRIORITY 1 section for the current
    # logic, and _check_parcel_poi_conflicts()'s signature (module
    # level, above) for the required targets parameter.

    # Landmark Categories (Task 1 -- Cluster A): discovered on POI
    # source change ONLY (see _refresh_poi_categories() below) -- never
    # on a checkbox interaction. poi_categories is rebuilt from scratch
    # by every discovery call (no cross-source caching). poi_category_
    # vars is rebuilt in lockstep by _rebuild_category_checklist()
    # every time poi_categories changes -- any BooleanVar from a
    # previous POI source's checklist is discarded, never reused across
    # a source change. As of D3c, checked categories (derived from
    # poi_category_vars) ARE consumed by both process_poi_counts_
    # dynamic() (via checked_categories/target_column_map, built in
    # on_run()) and _update_run_button_state() (live Run-readiness
    # feedback) -- this was originally inert at Cluster A and wired in
    # progressively through D3a/D3c.
    poi_categories = {}            # {sanitized_key: [raw_values...]}
    poi_category_vars = {}         # {sanitized_key: tk.BooleanVar}
    poi_category_reading = False

    # Placeholder declarations for every widget/value _create_category_
    # section() (below) builds and reassigns via `nonlocal` -- Python
    # requires a name to already exist in the enclosing scope before a
    # nested function can declare it `nonlocal`. All of these are
    # destroyed and rebuilt together, as one atomic unit, every time
    # _destroy_category_section() + _create_category_section() run --
    # see that function pair's own docstring for the full root-cause
    # history behind why this is a single atomic lifecycle unit rather
    # than individually-managed widgets.
    category_frame = None
    category_status_row = None
    category_status_var = None
    category_status_lbl = None
    category_checklist_outer = None
    category_header_row = None
    poi_header_frame = None
    poi_header_lbl = None
    category_links_frame = None
    check_all_link = None
    uncheck_all_link = None
    method_header_frame = None
    method_header_lbl = None
    POI_COLUMN_WIDTH = None
    METHOD_COLUMN_WIDTH = None
    category_body_row = None
    category_count_var = None
    category_count_lbl = None
    poi_col_frame = None
    poi_canvas = None
    category_hscroll = None
    method_canvas = None
    category_vscroll = None
    poi_checklist_container = None
    _poi_canvas_window = None
    method_checklist_container = None
    _method_canvas_window = None
    # distance_radius_anchor: the Distance Radius section's own header
    # Frame, captured once that section is built (see its construction
    # further below) -- used by _create_category_section() to reinsert
    # a RECREATED category_frame in its correct visual position. None
    # here means "not built yet" -- correctly matches the state during
    # _create_category_section()'s very first (initial-construction)
    # call, which legitimately doesn't need a before= anchor since
    # nothing has been packed after it yet at that point.
    distance_radius_anchor = None

    # Task 3 (Cluster B): per-category Aerial/Road method state and the
    # widget references needed to enable/disable each row's own radio
    # pair from that row's checkbox command. Rebuilt in lockstep with
    # poi_category_vars by _rebuild_category_checklist() -- same
    # discard-on-source-change convention, never reused across a POI
    # source change.
    poi_category_method_vars = {}      # {sanitized_key: tk.StringVar}  -- "aerial" | "road" | "" (unchecked)
    poi_category_radio_widgets = {}    # {sanitized_key: (Radiobutton, Radiobutton)}
    # poi_category_remembered_method: the last method the user EXPLICITLY
    # chose for each category, kept even while that category is
    # unchecked (and its radios are grayed out with neither selected).
    # Restored into poi_category_method_vars[key] the next time that
    # row is re-checked -- see _on_category_checked_toggle() below.
    # Starts at "road" for every row (Task 3's default), updated only
    # when the user actually clicks a radio while the row is checked.
    poi_category_remembered_method = {}  # {sanitized_key: "aerial" | "road"}

    # Task 4 (Cluster B): two independent radius inputs, one per
    # distance method. D3c: these are now the ONLY radii actually
    # consumed by run_processing() -- the old single radius_var field
    # they used to sit inert alongside has been fully retired.
    aerial_radius_var = tk.StringVar(master=win, value="200")
    road_radius_var   = tk.StringVar(master=win, value="200")

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
        return frm

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

    def browse_parcel_files():
        file = filedialog.askopenfilename(filetypes=[
            ("Shapefiles", "*.shp"), ("GeoPackage", "*.gpkg"), ("All", "*.*")])
        # Cancel returns "" -- do not assign, preserving previous selection.
        if file:
            nonlocal parcel_local_path
            parcel_local_path = file
            parcel_files_var.set(os.path.basename(file))
        _update_run_button_state()

    def _on_parcel_db_selected(sel):
        # Only called on confirmed selection -- Cancel never calls on_select,
        # so parcel_db_table retains its previous value automatically.
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
        # untouched.
        _update_run_button_state()

    # ── SECTION 1B: ROAD NETWORK SOURCE ──────────────────────────
    # Task 5: new secondary source, positioned below Land Parcel
    # Source per explicit request, before POI Source. Always visible
    # regardless of whether any checked category is currently set to
    # Road (Document 1's explicit requirement) -- unlike the Landmark
    # Categories section, this one is never conditionally hidden.
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

    road_files_var = tk.StringVar(master=win, value="No file selected")
    road_db_label  = tk.StringVar(master=win, value="No table selected")

    road_action_row = tk.Frame(road_frame)
    road_action_row.pack(fill="x", pady=2)

    road_lbl = tk.Label(road_action_row, textvariable=road_files_var,
                        fg="gray", anchor="w", width=42)
    road_lbl.pack(side="left")

    road_btn = tk.Button(road_action_row, text="Browse…", width=10)
    road_btn.pack(side="left", **PAD)

    def browse_road_files():
        nonlocal road_local_path, road_local_layer
        file = filedialog.askopenfilename(filetypes=[
            ("Shapefiles", "*.shp"), ("GeoPackage", "*.gpkg"), ("All", "*.*")])
        # Cancel returns "" -- do not assign, preserving previous selection.
        if not file:
            return
        try:
            layer = resolve_local_road_layer(win, file)
        except Exception as e:
            messagebox.showerror(
                "Error", f"Could not read Road Network Source: {e}", parent=win)
            return
        ext = os.path.splitext(file)[1].lower()
        if ext == ".gpkg" and layer is None:
            # User cancelled the layer-selection dialog -- treat the
            # whole file selection as cancelled, never silently fall
            # back to an arbitrary layer (Document 1's explicit
            # requirement).
            return
        road_local_path = file
        road_local_layer = layer
        label = os.path.basename(file) + (f"  (layer: {layer})" if layer else "")
        road_files_var.set(label)
        _update_run_button_state()

    def _on_road_db_selected(sel):
        # Only called on confirmed selection -- Cancel never calls
        # on_select, so road_db_table retains its previous value
        # automatically.
        nonlocal road_db_table
        road_db_table = sel[0]
        road_db_label.set(sel[0])
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
        # Always render from authority variables -- never from
        # StringVar state -- same pattern _toggle_parcel() uses above.
        if road_source_type.get() == "local":
            road_lbl.config(textvariable=road_files_var)
            road_btn.config(text="Browse…", command=browse_road_files)
            label = (
                os.path.basename(road_local_path)
                + (f"  (layer: {road_local_layer})" if road_local_layer else "")
                if road_local_path else "No file selected"
            )
            road_files_var.set(label)
        else:
            road_lbl.config(textvariable=road_db_label)
            road_btn.config(text="Select…", command=browse_road_db)
            road_db_label.set(
                road_db_table if road_db_table
                else "No table selected"
            )
        # Switching Local <-> Database does NOT clear the other mode's
        # remembered selection -- matches _toggle_parcel()'s own
        # behavior, left untouched.
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
            # POI-source-change-only trigger (Task 1, Cluster A) -- see
            # _refresh_poi_categories() docstring below.
            _refresh_poi_categories()
            _update_run_button_state()

    def _on_poi_db_selected(sel):
        # _pick_db_tables only ever invokes on_select with a non-empty
        # sel (see its submit(): "if sel: on_select(sel)"), so no
        # empty-selection branch is needed here.
        poi_db_table.set(sel[0])
        poi_db_var.set(sel[0])
        # POI-source-change-only trigger (Task 1, Cluster A) -- see
        # _refresh_poi_categories() docstring below.
        _refresh_poi_categories()
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
        # Switching Local <-> Database is a POI source change -- always
        # re-discovers fresh for whichever mode is now active, same
        # always-refresh-on-toggle convention as _toggle_parcel() above.
        _refresh_poi_categories()
        _update_run_button_state()

    # ── SECTION 2B: LANDMARK CATEGORIES ──────────────────────────
    # Task 1 (Cluster A): dynamic, checklist-driven category detection.
    # As of D3a/D3c, checking/unchecking a row here directly determines
    # what gets counted (checked_categories/target_column_map, built in
    # on_run(), consumed by process_poi_counts_dynamic()) and live-
    # updates the Run button's readiness. No section_label() call here
    # (explicit request) -- unlike every
    # other section in this window, this one has no bold title/divider
    # of its own; category_status_lbl below (once a POI source is
    # selected) is the only heading-like text this section shows.

    # Vertical scrollbar appears ONLY once more than 7 categories are
    # detected (explicit requirement); horizontal scrollbar appears ONLY
    # when a label is wider than the box, and applies ONLY to the POI/
    # checkbox column -- the Aerial/Road radio column is a "frozen" column
    # (spreadsheet-style: like Excel's freeze panes) that never scrolls
    # horizontally, so a long fclass label can never cover or push the
    # radios out of view. This requires TWO separate Canvas widgets
    # (poi_canvas for the checkbox column, method_canvas for the radio
    # column) whose vertical scroll positions are kept in sync, since a
    # single Canvas's horizontal scroll would otherwise carry its entire
    # embedded window -- including the radios -- along with it. Canvas/
    # Scrollbar/create_window/scrollregion/mousewheel mechanics per canvas
    # are still adapted from meters_from_school_shop_transport_church.py's
    # own other_landmarks_checklist_* pattern (see Cluster A); the
    # two-canvas vertical-sync wrapper is new, specific to this
    # frozen-column requirement.
    #
    # Defined ONCE here, OUTSIDE _create_category_section() -- unlike
    # every widget below, this is a plain constant, referenced by
    # _resize_category_checklist_box() (defined separately, further below,
    # unchanged by the destroy/recreate refactor) which needs it to exist
    # independent of any particular category_frame instance's lifetime.
    CATEGORY_CHECKLIST_MAX_ITEMS_BEFORE_VSCROLL = 7

    def _create_category_section():
        """
        Builds the ENTIRE Landmark Categories widget subtree from scratch --
        category_frame and every one of its descendants (status row,
        checklist headers/canvases, count label) -- as ONE atomic unit.

        WHY THIS EXISTS (root-cause history, confirmed via live headless
        experimentation, not theoretical): explicitly calling
        poi_canvas.configure(height=N)/method_canvas.configure(height=N)
        with a nontrivial N WHILE this section is packed leaves category_
        frame's own winfo_reqheight() permanently pinned at that value --
        confirmed to survive pack_forget(), destroying only category_
        checklist_outer (its child), update_idletasks(), toggling
        pack_propagate(False)/(True) on category_frame itself, and even a
        full win.withdraw()/win.deiconify() cycle. The ONLY thing that
        clears it: destroying category_frame itself and creating a fresh
        replacement -- proven experimentally (a fresh, empty replacement
        correctly reports winfo_reqheight()==1 and the whole window
        correctly shrinks; recreating only category_checklist_outer, one
        level down, does NOT clear the parent's stale value). This is why
        category_frame -- not category_checklist_outer, not anything
        narrower -- is the correct lifecycle boundary.

        This is the ONLY place any of these widgets are constructed.
        Called once at initial window construction, and again (paired with
        _destroy_category_section() first) at EVERY point this section's
        content is about to change in a way that could affect its size:
        _set_poi_category_reading_state(True), _refresh_poi_categories()'s
        "nothing selected" branch, and _handle_zero_eligible_categories().
        (_poll_poi_category_queue()'s N>0 branch does NOT call this again
        -- reading always destroys+recreates first, and nothing grows any
        canvas between then and there, so the section is still guaranteed
        fresh at that point; rebuilding again would just be redundant, not
        incorrect.)

        Deliberately separate from the LOGICAL/data state this section
        displays (poi_categories, poi_category_vars, poi_category_method_
        vars, poi_category_radio_widgets, poi_category_remembered_method)
        -- none of that is touched here. Those are plain Python dicts, not
        tied to any widget's lifetime, and _rebuild_category_checklist()
        (defined separately, below, and unchanged by this refactor) is
        still the only thing that repopulates them, exactly as before.
        This function only ever produces an EMPTY, freshly-built section
        (matching category_frame's state immediately after the original
        inline construction ran, before any POI source was ever selected)
        -- the caller is responsible for packing/populating it afterward
        exactly as it already did (category_status_row.pack(...),
        category_checklist_outer.pack(...), _rebuild_category_checklist()),
        unchanged from before this refactor.
        """
        nonlocal category_frame, category_status_row, category_status_var, category_status_lbl
        nonlocal category_checklist_outer, category_header_row, poi_header_frame, poi_header_lbl
        nonlocal category_links_frame, check_all_link, uncheck_all_link, method_header_frame
        nonlocal method_header_lbl, POI_COLUMN_WIDTH, METHOD_COLUMN_WIDTH, category_body_row
        nonlocal category_count_var, category_count_lbl, poi_col_frame, poi_canvas
        nonlocal category_hscroll, method_canvas, category_vscroll, poi_checklist_container
        nonlocal _poi_canvas_window, method_checklist_container, _method_canvas_window

        category_frame = tk.Frame(win)
        # WHY before=distance_radius_anchor matters: tk.Frame.pack()
        # with no explicit before=/after= APPENDS to the end of its
        # parent's current packing order. That's correct for the very
        # first call (nothing has been packed after this point in the
        # window yet), but every SUBSEQUENT call -- triggered by a user
        # interaction long after Distance Radius/Search Radius/Output
        # Destination/Run Processing are already packed below where
        # this section belongs -- would otherwise silently relocate the
        # whole recreated section to the very BOTTOM of the window,
        # below the Run Processing button, instead of back in its
        # correct slot between POI Source and Distance Radius. Reported
        # symptom this fixes: "Reading POI source..." appearing at the
        # very bottom of the window on the 2nd+ discovery cycle
        # (browsing again, or toggling back), not in its original
        # position right under POI Source.
        if distance_radius_anchor is not None:
            category_frame.pack(fill="x", padx=18, pady=2, before=distance_radius_anchor)
        else:
            category_frame.pack(fill="x", padx=18, pady=2)

        # category_status_row: starts UNPACKED (hidden) -- no guide text
        # appears until a POI source is actually selected. Holds ONLY the
        # guide-text label now -- Check All/Uncheck All moved into
        # poi_header_frame below (explicit request: the links must sit
        # inside the POI column itself, not span the full row). Packed by
        # _set_poi_category_reading_state(True) the moment a real
        # discovery read starts; unpacked again by _refresh_poi_
        # categories()'s "nothing selected" branch, and by
        # _handle_zero_eligible_categories() below (a zero-eligible result
        # is now a rejected source selection, not a valid empty state --
        # see that function's docstring).
        category_status_row = tk.Frame(category_frame)

        category_status_var = tk.StringVar(master=win, value="")
        category_status_lbl = tk.Label(
            category_status_row, textvariable=category_status_var,
            fg="gray", anchor="w", font=("Segoe UI", 9, "normal"))
        category_status_lbl.pack(side="left", fill="x", expand=True)

        def _check_all_categories():
            """Sets every discovered category's checkbox to True, going
            through _on_category_checked_toggle() per key so each row's
            Aerial/Road radios and remembered method are updated exactly
            as if the user had clicked each checkbox individually. Never
            touches method state directly."""
            for key in poi_category_vars:
                poi_category_vars[key].set(True)
                _on_category_checked_toggle(key)

        def _uncheck_all_categories():
            """Mirror of _check_all_categories() above -- unchecks every
            discovered category."""
            for key in poi_category_vars:
                poi_category_vars[key].set(False)
                _on_category_checked_toggle(key)

        # Vertical scrollbar appears ONLY once more than 7 categories are
        # detected (explicit requirement); horizontal scrollbar appears
        # ONLY when a label is wider than the box, and applies ONLY to the
        # POI/checkbox column -- the Aerial/Road radio column is a
        # "frozen" column (spreadsheet-style: like Excel's freeze panes)
        # that never scrolls horizontally, so a long fclass label can
        # never cover or push the radios out of view. This requires TWO
        # separate Canvas widgets (poi_canvas for the checkbox column,
        # method_canvas for the radio column) whose vertical scroll
        # positions are kept in sync, since a single Canvas's horizontal
        # scroll would otherwise carry its entire embedded window --
        # including the radios -- along with it. Canvas/Scrollbar/
        # create_window/scrollregion/mousewheel mechanics per canvas are
        # still adapted from meters_from_school_shop_transport_church.py's
        # own other_landmarks_checklist_* pattern (see Cluster A); the
        # two-canvas vertical-sync wrapper is new, specific to this
        # frozen-column requirement.
        CATEGORY_CHECKLIST_MAX_ITEMS_BEFORE_VSCROLL = 7

        category_checklist_outer = tk.Frame(category_frame)
        # NOT packed here -- this whole block (the "POI"/"Method" headers,
        # the checklist body, and the bottom category-count label) starts
        # hidden and is only shown once POI category discovery actually
        # COMPLETES with at least one eligible category -- not merely once
        # a POI source is selected, and not while reading is still in
        # progress (only category_status_row shows during reading -- see
        # _set_poi_category_reading_state()). A zero-eligible-categories
        # result no longer renders anything here at all (no "(no eligible
        # categories)" placeholder) -- it is treated as a rejected source
        # selection instead; see _handle_zero_eligible_categories() below.
        # Packed by _poll_poi_category_queue()'s success (N>0) branch;
        # unpacked by _refresh_poi_categories()'s "nothing selected"
        # branch and by _handle_zero_eligible_categories().

        # Header row: "POI" (+ Check All/Uncheck All, scoped to this
        # column) above the checkbox/fclass column, "Method" above the
        # Aerial/Road column. Both header Frames get a SESSION-LOCKED width
        # (and height) below -- measured ONCE from their actual real
        # content right after construction, then frozen via
        # pack_propagate(False) + an explicit width=/height= that
        # _resize_category_checklist_box() never touches again afterward.
        # This is deliberately NOT the same thing as a hardcoded pixel
        # constant -- the value comes from real Tk-rendered content in
        # this session's actual font/theme/DPI, so it stays DPI-correct;
        # it simply never changes again after that one measurement. This
        # removes the earlier poi_action_row.winfo_reqwidth() dependency
        # entirely -- the same "borrow a width from a sibling row above"
        # assumption that caused the earlier expand=True overshoot bug --
        # and is what makes toggling Local<->Database no longer visibly
        # shift the checklist's sizing (a value that is never recomputed
        # cannot shift for any reason, toggle included).
        category_header_row = tk.Frame(category_checklist_outer)
        category_header_row.pack(fill="x", side="top")

        poi_header_frame = tk.Frame(category_header_row)
        poi_header_frame.pack(side="left")
        poi_header_lbl = tk.Label(poi_header_frame, text="POI",
                                   font=("Segoe UI", 8, "bold"), anchor="w", fg="#444444")
        poi_header_lbl.pack(side="left")

        # Check All / Uncheck All -- affects ONLY the POI/checkbox column
        # (poi_category_vars), never the Aerial/Road method radios; lives
        # INSIDE poi_header_frame (explicit request: "katabi ng POI
        # column... nasa loob siya ng POI column"), not in a separate
        # full-width row. Each click reuses _on_category_checked_toggle()
        # per key so a bulk check/uncheck goes through the exact same
        # enable/disable + remembered-method restore/save logic as an
        # individual click, rather than duplicating that state management
        # here.
        category_links_frame = tk.Frame(poi_header_frame)
        # side="right" (not "left"+padx): flush against poi_header_frame's
        # own RIGHT edge -- "naka sagad sa kanan ng POI column" (explicit
        # request). Since poi_header_frame has a locked width
        # (pack_propagate(False), see POI_COLUMN_WIDTH below), packing
        # side="right" here means the links sit at that column's actual
        # right edge regardless of how wide "POI" itself is, rather than
        # being offset by a fixed padx from the left (which only
        # approximated "further right" without truly reaching the edge).
        category_links_frame.pack(side="right", padx=(0, 4))
        check_all_link = tk.Label(
            category_links_frame, text="Check All", fg="#1a73e8", cursor="hand2",
            font=("Segoe UI", 8, "underline"))
        check_all_link.pack(side="left")
        check_all_link.bind("<Button-1>", lambda e: _check_all_categories())
        tk.Label(category_links_frame, text=" | ", fg="gray",
                 font=("Segoe UI", 8)).pack(side="left")
        uncheck_all_link = tk.Label(
            category_links_frame, text="Uncheck All", fg="#1a73e8", cursor="hand2",
            font=("Segoe UI", 8, "underline"))
        uncheck_all_link.pack(side="left")
        uncheck_all_link.bind("<Button-1>", lambda e: _uncheck_all_categories())

        method_header_frame = tk.Frame(category_header_row)
        method_header_frame.pack(side="left")
        method_header_lbl = tk.Label(method_header_frame, text="Method",
                                      font=("Segoe UI", 8, "bold"), anchor="w", fg="#444444")
        method_header_lbl.pack(side="left")

        # One-time probe: a throwaway Aerial/Road ttk.Radiobutton pair,
        # built with the exact same widget type/styling/padding a real
        # checklist row's method radios use, ONLY to measure their true
        # rendered width in this session's actual font/theme -- "Method"
        # (the header word alone) is narrower than "Aerial  Road" (the
        # actual content that sits under it row after row), so locking the
        # Method column to the header text's width alone would clip the
        # real radios. Built, measured, and destroyed immediately -- never
        # part of the visible tree.
        _method_probe_frame = tk.Frame(win)
        _probe_road = ttk.Radiobutton(_method_probe_frame, text="Road")
        _probe_aerial = ttk.Radiobutton(_method_probe_frame, text="Aerial")
        _probe_road.pack(side="left", padx=(2, 0))
        _probe_aerial.pack(side="left", padx=(3, 0))
        _method_probe_frame.update_idletasks()
        _probe_content_width = (
            _probe_aerial.winfo_reqwidth() + _probe_road.winfo_reqwidth() + 5)
        _method_probe_frame.destroy()

        # POI_COLUMN_EXTRA_PADDING: added on top of the bare-minimum
        # measured header width (per explicit feedback: "madami pang space
        # sa kanan" -- there's still unused space to the right of the
        # checklist that the locked-but-tight width wasn't using; increased
        # again this turn per continued feedback that Method still had room
        # to move further right). Same caveat as the padx above -- a
        # tuning value, not confirmed against the real rendered window.
        POI_COLUMN_EXTRA_PADDING = 90

        category_header_row.update_idletasks()
        POI_COLUMN_WIDTH = poi_header_frame.winfo_reqwidth() + POI_COLUMN_EXTRA_PADDING
        METHOD_COLUMN_WIDTH = max(method_header_frame.winfo_reqwidth(), _probe_content_width)
        _category_header_height = max(
            poi_header_frame.winfo_reqheight(), method_header_frame.winfo_reqheight())

        poi_header_frame.configure(width=POI_COLUMN_WIDTH, height=_category_header_height)
        poi_header_frame.pack_propagate(False)
        method_header_frame.configure(
            width=METHOD_COLUMN_WIDTH, height=_category_header_height)
        method_header_frame.pack_propagate(False)

        category_body_row = tk.Frame(category_checklist_outer)
        category_body_row.pack(fill="both", expand=True, side="top")

        # category_count_lbl: the "N landmark categories found." line,
        # moved BELOW the checklist (was previously combined into the top
        # guide text) -- a separate, purely informational label. Nested
        # inside category_checklist_outer (not category_status_row), so it
        # automatically shows/hides together with the headers/body via the
        # same category_checklist_outer.pack()/.pack_forget() calls --
        # nothing extra to wire up. Only ever set to a real count at the
        # moment category_checklist_outer itself is first shown (see
        # _poll_poi_category_queue()'s N>0 branch below), so there's no
        # window where stale/zero count text could be visible.
        category_count_var = tk.StringVar(master=win, value="")
        category_count_lbl = tk.Label(
            category_checklist_outer, textvariable=category_count_var,
            fg="gray", anchor="w")
        category_count_lbl.pack(fill="x", side="top", pady=(4, 0))

        # poi_col_frame: the scrollable checkbox/fclass column -- its own
        # Canvas plus the ONE horizontal scrollbar in this whole checklist
        # (deliberately scoped to only this column's xview, never the
        # method column's).
        #
        # fill="y" WITHOUT expand=True is deliberate, not an oversight:
        # expand=True would let this Frame (and poi_canvas inside it, same
        # reasoning below) consume any leftover horizontal space in
        # category_body_row -- and there often IS leftover space, since
        # category_body_row's actual available width comes from the whole
        # window's width (set by whichever section is widest overall), not
        # from poi_action_row's width specifically. That leftover space
        # being silently consumed is exactly what pushed method_canvas (the
        # Aerial/Road column) too far to the right of where it should sit --
        # confirmed via isolated live testing: an explicit .configure(
        # width=200) on poi_canvas was silently overridden to 500px by
        # pack()'s own fill+expand behavior, and removing expand=True
        # (fill="y" only) made the pinned 200px width hold exactly. Width
        # for this column is controlled ENTIRELY by the explicit width=
        # set in _resize_category_checklist_box() below -- never by the
        # packer.
        poi_col_frame = tk.Frame(category_body_row)
        poi_col_frame.pack(side="left", fill="y")

        poi_canvas = tk.Canvas(poi_col_frame, highlightthickness=0, bd=0)
        category_hscroll = tk.Scrollbar(
            poi_col_frame, orient="horizontal", command=poi_canvas.xview)
        poi_canvas.pack(side="top", fill="y")
        # category_hscroll packed/unpacked dynamically by
        # _resize_category_checklist_box() below -- only shown when the
        # POI column's own content actually exceeds its own box width.

        # method_canvas: the frozen Aerial/Road column -- a genuine Canvas
        # (not a plain Frame) because with up to ~136 rows and only 7
        # visible at a time, this column still needs the SAME vertical
        # clipping/scrolling as the POI column, even though it never needs
        # horizontal scrolling of its own.
        method_canvas = tk.Canvas(category_body_row, highlightthickness=0, bd=0)
        method_canvas.pack(side="left")

        # category_vscroll: ONE shared vertical scrollbar, driving BOTH
        # canvases' yview together via _on_category_vscroll_command() below
        # -- this is what keeps every row's checkbox and its Aerial/Road
        # radios vertically aligned while scrolling.
        category_vscroll = tk.Scrollbar(category_body_row, orient="vertical")

        def _on_category_vscroll_command(*args):
            poi_canvas.yview(*args)
            method_canvas.yview(*args)
        category_vscroll.configure(command=_on_category_vscroll_command)

        def _on_poi_canvas_yscroll(first, last):
            # poi_canvas is the single source of truth for the shared
            # scrollbar's thumb position -- method_canvas has no visible
            # scrollbar of its own, so it never needs to report position,
            # only to be kept in step with poi_canvas's own movement.
            category_vscroll.set(first, last)
            method_canvas.yview_moveto(first)
        poi_canvas.configure(
            xscrollcommand=category_hscroll.set, yscrollcommand=_on_poi_canvas_yscroll)

        poi_checklist_container = tk.Frame(poi_canvas)
        _poi_canvas_window = poi_canvas.create_window(
            (0, 0), window=poi_checklist_container, anchor="nw")

        method_checklist_container = tk.Frame(method_canvas)
        _method_canvas_window = method_canvas.create_window(
            (0, 0), window=method_checklist_container, anchor="nw")

        def _on_poi_content_configure(_event=None):
            poi_canvas.configure(scrollregion=poi_canvas.bbox("all"))
        poi_checklist_container.bind("<Configure>", _on_poi_content_configure)

        def _on_method_content_configure(_event=None):
            method_canvas.configure(scrollregion=method_canvas.bbox("all"))
        method_checklist_container.bind("<Configure>", _on_method_content_configure)

        def _on_category_mousewheel(event):
            # Scrolling over EITHER canvas moves both together -- see
            # _on_poi_canvas_yscroll()'s docstring note on why method_canvas
            # is driven from poi_canvas's resulting position rather than
            # scrolled independently.
            poi_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            method_canvas.yview_moveto(poi_canvas.yview()[0])

        def _bind_category_mousewheel(_event=None):
            # bind_all()/unbind_all() register a global, application-wide
            # binding rather than a per-widget one -- which specific canvas
            # triggered <Enter> doesn't matter, so both canvases below
            # share this same pair of handlers rather than needing distinct
            # per-widget closures.
            poi_canvas.bind_all("<MouseWheel>", _on_category_mousewheel)

        def _unbind_category_mousewheel(_event=None):
            poi_canvas.unbind_all("<MouseWheel>")

        poi_canvas.bind("<Enter>", _bind_category_mousewheel)
        poi_canvas.bind("<Leave>", _unbind_category_mousewheel)
        method_canvas.bind("<Enter>", _bind_category_mousewheel)
        method_canvas.bind("<Leave>", _unbind_category_mousewheel)


    def _destroy_category_section():
        """
        Destroys category_frame and everything inside it, in one call --
        the other half of the atomic lifecycle unit _create_category_
        section() establishes. See that function's docstring for the full
        root-cause history this pairing exists to address.

        Safe to call even if category_frame was already destroyed (e.g.
        defensive double-calls) -- winfo_exists() guards against operating
        on an already-destroyed Tcl object, which would otherwise raise.
        Does NOT touch poi_categories or any of the per-category state
        dicts -- those are logical/data state, independent of this
        section's widget lifetime (see _create_category_section()'s
        docstring on this same separation).
        """
        nonlocal category_frame
        if category_frame is not None and category_frame.winfo_exists():
            category_frame.destroy()
        category_frame = None


    _create_category_section()  # initial build, matching original inline-construction behavior

    def _set_category_status_emphasis(is_emphasized):
        """
        Toggles category_status_lbl between its plain appearance
        (reading/error states -- fg=gray or the reading-state's own
        orange, regular weight) and an emphasized appearance (bold,
        used only for the "N landmark categories found" success
        message -- see _poll_poi_category_queue() below). No colored
        background or icon (reverted per explicit request) -- the
        message text itself is also set in UPPERCASE at that call site
        for additional emphasis alongside the bold weight.

        Defined here, OUTSIDE _create_category_section() -- unlike that
        function's own internal widget-construction code, this only
        needs category_status_lbl (nonlocal-reassigned by that
        function every time it runs) via closure, so it doesn't need
        to be redefined on every recreation. Also needed here rather
        than inside _create_category_section() because sibling
        functions (_set_poi_category_reading_state(),
        _poll_poi_category_queue()) call it -- a function defined
        LOCALLY inside a sibling function's body is not visible to
        other siblings, only to nested descendants of that same body.
        """
        if is_emphasized:
            category_status_lbl.config(font=("Segoe UI", 9, "bold"))
        else:
            category_status_lbl.config(font=("Segoe UI", 9, "normal"))

    def _resize_category_checklist_box():
        """
        Recomputes both canvases' HEIGHT (item-count-based vertical
        scroll cap -- see meters_from_school_shop_transport_church.py's
        _resize_other_landmarks_checklist_box() for the underlying
        reasoning this is adapted from: measured from actual rendered
        row height, not a guessed fixed-pixel cap) and whether the POI
        column's horizontal scrollbar is needed.

        WIDTH for both columns is NOT computed here at all anymore --
        POI_COLUMN_WIDTH and METHOD_COLUMN_WIDTH (see
        category_header_row's construction above) are session-locked
        values, measured once from real header content right after
        construction and never touched again by this function or
        anything else. This removes the earlier poi_action_row.
        winfo_reqwidth() dependency entirely, and is what keeps
        toggling Local<->Database from ever visibly shifting the
        checklist's sizing (a value that's never recomputed cannot
        shift for any reason, toggle included).

        Long fclass labels remain horizontally scrollable exactly as
        before -- POI_COLUMN_WIDTH is the POI column's VIEWPORT width,
        not a cap on individual label length; content wider than that
        locked viewport still triggers category_hscroll below, same
        mechanism as before, just measured against a fixed reference
        instead of a dynamic one.
        """
        poi_checklist_container.update_idletasks()
        method_checklist_container.update_idletasks()
        n_items = len(poi_category_vars)
        poi_content_height = poi_checklist_container.winfo_reqheight()
        poi_content_width = poi_checklist_container.winfo_reqwidth()

        show_vscroll = n_items > CATEGORY_CHECKLIST_MAX_ITEMS_BEFORE_VSCROLL
        poi_canvas.configure(width=POI_COLUMN_WIDTH)
        method_canvas.configure(width=METHOD_COLUMN_WIDTH)

        if not show_vscroll:
            poi_canvas.configure(height=poi_content_height)
            method_canvas.configure(height=poi_content_height)
            category_vscroll.pack_forget()
        else:
            row_height = poi_content_height / n_items
            capped_height = int(round(
                row_height * CATEGORY_CHECKLIST_MAX_ITEMS_BEFORE_VSCROLL))
            poi_canvas.configure(height=capped_height)
            method_canvas.configure(height=capped_height)
            category_vscroll.pack(side="right", fill="y")

        if poi_content_width > POI_COLUMN_WIDTH:
            poi_canvas.itemconfig(_poi_canvas_window, width=poi_content_width)
            category_hscroll.pack(side="bottom", fill="x")
        else:
            poi_canvas.itemconfig(_poi_canvas_window, width=POI_COLUMN_WIDTH)
            category_hscroll.pack_forget()

        # method_canvas's embedded window always matches the locked
        # METHOD_COLUMN_WIDTH -- this column never scrolls horizontally
        # (explicit requirement: the Aerial/Road radios must never be
        # covered by, or shift because of, a long fclass label).
        method_canvas.itemconfig(_method_canvas_window, width=METHOD_COLUMN_WIDTH)

    def _rebuild_category_checklist():
        """
        Clears and repopulates poi_checklist_container (checkbox
        column) and method_checklist_container (frozen Aerial/Road
        column) from the
        current poi_categories dict -- one row per sanitized key, each
        row a Checkbutton (label = the lowercase sanitized key itself,
        Task 1 step 6) plus an Aerial/Road Radiobutton pair (Task 3).
        Each row's BooleanVar defaults to False (unchecked): Document 1
        does not specify a default-checked state, and defaulting
        unchecked is the safer choice -- an unchecked category is
        excluded from processing (Task 2), so nothing is ever silently
        counted that the user didn't explicitly ask for.

        Uses grid() (row=row_idx, column=0[/1]), NOT pack(), within
        EACH container separately, so the Checkbutton column lines up
        vertically across every row in poi_checklist_container, and the
        Aerial/Road columns line up vertically across every row in
        method_checklist_container, regardless of label length
        ("archaeological" vs "alpine_hut") -- grid auto-sizes each
        column to its widest cell, shared across all rows in the same
        container. Splitting the row across two containers (rather
        than one container with three grid columns) is what keeps the
        Aerial/Road column from ever being affected by the checkbox
        column's own horizontal scrolling -- see the Canvas/column-
        split docstring on category_checklist_outer's construction
        above.

        Each row's method StringVar starts EMPTY (""), not "road" --
        an unchecked row must show NEITHER radio as selected (see
        _on_category_checked_toggle() below for why this is
        deliberately different from just disabling the widgets while
        leaving the variable's value in place, which Tkinter would
        still render as a grayed-out selected dot). poi_category_
        remembered_method[key] = "road" is what Task 3's actual default
        means here -- it's the value restored into method_var the
        moment this row is first checked, not the row's starting
        display state.

        Both radios start state="disabled" (unchecked rows' method
        selection is irrelevant -- Task 3) and are toggled by
        _on_category_checked_toggle() below via the Checkbutton's own
        command=.

        Rebuilds poi_category_vars, poi_category_method_vars,
        poi_category_radio_widgets, AND poi_category_remembered_method
        from scratch every call -- anything from a previous call
        (previous POI source) is discarded, never reused. Only ever
        called from the discovery path below (POI source change),
        never from a checkbox/method interaction -- see
        _refresh_poi_categories()'s docstring.

        Calls _resize_category_checklist_box() at the end of every
        rebuild -- item count (and therefore whether the vertical
        scrollbar is needed) only changes here, never on a checkbox/
        method toggle (neither adds or removes rows).

        When poi_categories is empty, both containers are simply left
        with no children -- no "(no eligible categories)" placeholder
        is rendered (removed by explicit design decision: zero
        eligible categories is now a rejected source selection,
        handled by _handle_zero_eligible_categories() below, not a
        legitimate empty-checklist state to display inline). This only
        happens transiently, either while category_checklist_outer
        itself is hidden (nothing selected) or immediately before that
        handler reverts the selection.
        """
        nonlocal poi_category_vars, poi_category_method_vars
        nonlocal poi_category_radio_widgets, poi_category_remembered_method
        for child in poi_checklist_container.winfo_children():
            child.destroy()
        for child in method_checklist_container.winfo_children():
            child.destroy()
        poi_category_vars = {}
        poi_category_method_vars = {}
        poi_category_radio_widgets = {}
        poi_category_remembered_method = {}
        if poi_categories:
            # row_wrapper_pairs: (poi_row_wrapper, method_row_wrapper)
            # per row -- used below to force both wrappers to an
            # IDENTICAL pixel height once every row's real content is
            # built. tk.Checkbutton and ttk.Radiobutton do not
            # necessarily render at the same natural row height (they
            # come from different widget toolkits with different
            # default padding/metrics); even a small per-row
            # discrepancy compounds across many rows into a real,
            # growing vertical misalignment between the checkbox column
            # and the Aerial/Road column the further down the (possibly
            # 100+ row) list you scroll -- confirmed via live headless
            # testing, not a theoretical concern.
            row_wrapper_pairs = []
            for row_idx, key in enumerate(sorted(poi_categories.keys())):
                checked_var = tk.BooleanVar(master=win, value=False)
                poi_category_vars[key] = checked_var

                # Starts empty -- see docstring above. Task 3's actual
                # default ("road") lives in poi_category_remembered_
                # method below, applied only once this row is checked.
                method_var = tk.StringVar(master=win, value="")
                poi_category_method_vars[key] = method_var
                poi_category_remembered_method[key] = "road"

                poi_row_wrapper = tk.Frame(poi_checklist_container)
                poi_row_wrapper.grid(row=row_idx, column=0, sticky="w")
                tk.Checkbutton(
                    poi_row_wrapper, text=key, variable=checked_var,
                    anchor="w", command=lambda k=key: _on_category_checked_toggle(k)
                ).pack(side="left")

                # Task 3: both radios start disabled -- checked_var
                # starts False for every row (see above), and an
                # unchecked category's method selection is irrelevant
                # since it won't be counted at all. ttk.Radiobutton
                # (not classic tk.Radiobutton) -- the classic widget's
                # disabled-indicator rendering on Windows can visually
                # read as "selected" regardless of the shared
                # variable's actual value, which is exactly what must
                # NOT happen here (an unchecked row must show neither
                # radio as selected). ttk uses the OS's native theme
                # engine and renders disabled/unselected state
                # unambiguously. Placed in method_checklist_container
                # (the frozen column), not poi_checklist_container --
                # see the Canvas/column-split docstring above.
                method_row_wrapper = tk.Frame(method_checklist_container)
                method_row_wrapper.grid(row=row_idx, column=0, sticky="w")
                # Road first (left), Aerial second (right) -- explicit
                # request to swap the original Aerial-first order.
                radio_road = ttk.Radiobutton(
                    method_row_wrapper, text="Road", variable=method_var,
                    value="road", state="disabled", command=_recompute_radius_enablement)
                radio_aerial = ttk.Radiobutton(
                    method_row_wrapper, text="Aerial", variable=method_var,
                    value="aerial", state="disabled", command=_recompute_radius_enablement)
                # Small, roughly one-space gaps on both sides -- the
                # Road radio sits close to the column's left edge so
                # it lines up with the flush-left "Method" header text
                # above it (poi_header_lbl/method_header_lbl both use
                # anchor="w" with no left indent), rather than the
                # previous 16px offset which visibly misaligned the
                # radios from their own column header (explicit
                # feedback: "hindi siya align sa title name na
                # Method"). Aerial's own gap from Road is kept equally
                # tight, per explicit request ("one space lang").
                radio_road.pack(side="left", padx=(2, 0))
                radio_aerial.pack(side="left", padx=(3, 0))
                poi_category_radio_widgets[key] = (radio_road, radio_aerial)
                row_wrapper_pairs.append((poi_row_wrapper, method_row_wrapper))

            # Force every row's two wrappers to the SAME pixel height --
            # the larger of that row's checkbox height and its radio-
            # pair height -- so nothing is clipped, and both containers'
            # scrollregions grow in perfect lockstep from here on. Width
            # is captured and re-applied per wrapper (NOT shared like
            # height -- a checkbox label's width and a radio-pair's
            # width vary independently row to row and column to
            # column). Both width AND height must be set explicitly
            # before pack_propagate(False): passing only height= and
            # leaving width unset collapses the wrapper to near-zero
            # width once propagation is disabled, silently discarding
            # a long fclass label's real width -- which then makes
            # poi_checklist_container.winfo_reqwidth() (used by
            # _resize_category_checklist_box() below to decide whether
            # the horizontal scrollbar is needed) report far too
            # narrow, and the scrollbar never appears even for a
            # genuinely overflowing label. Confirmed via live headless
            # testing, not a theoretical concern -- pack_propagate(False)
            # is required: without it, a Frame's size always shrinks
            # back to fit its own children, silently discarding the
            # explicit width=/height= just set.
            poi_checklist_container.update_idletasks()
            for poi_wrapper, method_wrapper in row_wrapper_pairs:
                row_height = max(
                    poi_wrapper.winfo_reqheight(), method_wrapper.winfo_reqheight())
                poi_wrapper.configure(
                    width=poi_wrapper.winfo_reqwidth(), height=row_height)
                poi_wrapper.pack_propagate(False)
                method_wrapper.configure(
                    width=method_wrapper.winfo_reqwidth(), height=row_height)
                method_wrapper.pack_propagate(False)
        _resize_category_checklist_box()


    def _set_poi_category_reading_state(is_reading):
        """
        Shows category_status_row (guide text) and toggles its message
        while POI category discovery is in progress. Deliberately does
        NOT show category_checklist_outer (the "POI"/"Method" headers
        and checklist body) yet -- that only becomes visible once
        discovery actually COMPLETES with at least one eligible
        category (see _poll_poi_category_queue()'s N>0 branch below).
        Showing it during "reading" would prematurely display stale/
        empty header content before there's anything real to show
        (this was a reported bug: the "POI" header appearing while
        "Detecting..." was still up).

        Deliberately does NOT disable the POI Browse/Select controls or
        the Local/Database radios (the Land Parcel section used to
        disable its own controls the same way while its background
        conflict check ran -- that mechanism was removed in Task 8/D3b)
        -- a slow or failed category discovery must never block the
        user from re-selecting or changing the POI source; see
        _handle_poi_category_discovery_failure()'s docstring for why a
        failure here is purely informational.

        Only ever called with is_reading=True from a context where a
        real POI source IS selected (see _refresh_poi_categories()
        below) -- so packing category_status_row here is exactly the
        "a POI source has been selected" moment; hiding it again is
        _refresh_poi_categories()'s "nothing selected" branch's job
        (or _handle_zero_eligible_categories()'s, for a rejected
        source), not this function's.
        """
        nonlocal poi_category_reading
        poi_category_reading = is_reading
        if is_reading:
            # Destroy+recreate FIRST, before packing anything -- see
            # _create_category_section()'s docstring for why this is
            # the correct, proven lifecycle boundary (not just
            # pack_forget/pack on the existing widgets). Guarantees
            # category_frame starts genuinely fresh for this discovery
            # cycle, regardless of whether the previous state was
            # populated (poisoned) or already empty.
            _destroy_category_section()
            _create_category_section()
            category_status_row.pack(fill="x")
            _set_category_status_emphasis(False)
            category_status_var.set("⏳ Reading POI source…")
            category_status_lbl.config(fg="#b36b00")
            _reflow_window()
        else:
            category_status_lbl.config(fg="gray")

    def _handle_zero_eligible_categories(source_type):
        """
        Called when a POI source read SUCCEEDS but yields ZERO eligible
        landmark categories (Task 1, step 4 -- e.g. the source has no
        'fclass' column, every value is empty/NaN, or every value
        sanitizes to pure digits with no letters). Per explicit design
        decision, this is treated as a REJECTED source selection, not
        a valid "source with an empty checklist" state:

            source selected -> reading -> 0 eligible categories
                -> clear ONLY the failed mode's source state
                -> hide reading/status UI
                -> show modal error
                -> user must select a different source

        Mirrors the pattern the Land Parcel section's own background
        conflict-check failure handler used to follow, before that
        mechanism was removed in Task 8/D3b (capture the failed
        source's display name BEFORE clearing; clear ONLY the mode
        that was actually being read, leaving the other mode's
        selection completely untouched; resolve the reading indicator
        BEFORE the modal dialog so it isn't left frozen behind a
        blocking dialog) -- but adapted for POI's tk.StringVar-based
        source variables (poi_local_path/poi_db_table +
        poi_file_var/poi_db_var), a different architecture from Land
        Parcel's None-or-string authority-variable pattern.

        Clears ONLY source_type's own variables: if Local was being
        read, poi_local_path/poi_file_var are reset to "" / "No file
        selected" and poi_db_table/poi_db_var are left completely
        untouched, and vice versa for "db" -- so a previously-selected
        source in the OTHER mode is never silently discarded by a
        failure in this one.
        """
        nonlocal poi_categories

        if source_type == "local":
            failed_name = (os.path.basename(poi_local_path.get())
                           if poi_local_path.get() else "the selected file")
            poi_local_path.set("")
            poi_file_var.set("No file selected")
        else:
            failed_name = poi_db_table.get() if poi_db_table.get() else "the selected table"
            poi_db_table.set("")
            poi_db_var.set("No table selected")

        poi_categories = {}
        _destroy_category_section()
        _create_category_section()
        _rebuild_category_checklist()
        _set_poi_category_reading_state(False)
        _reflow_window()
        _update_run_button_state()

        messagebox.showerror(
            "No Landmark Categories Found",
            f'The selected POI source "{failed_name}" has no eligible landmark '
            f'categories.\n\n'
            f"Please make sure it has an 'fclass' column with valid text values "
            f"— even a single distinct value is enough, as long as it's not "
            f"empty or purely numeric.\n\n"
            f"You will need to select a different POI source to continue.",
            parent=win)

    def _handle_poi_category_discovery_failure(reason):
        """
        A failed or timed-out ("reason") category discovery is purely
        informational -- unlike _handle_zero_eligible_categories()
        above (and unlike the Land Parcel section's own background
        conflict-check failure handling used to, before that mechanism
        was removed in Task 8/D3b), it does
        NOT clear the POI source selection (poi_local_path /
        poi_db_table are left exactly as the user set them). This is a
        genuine READ failure (unreadable file, DB connection error,
        timeout) -- categorically different from a successful read
        that simply found zero eligible categories, which IS now
        treated as a rejected source (see _handle_zero_eligible_
        categories() above). The checklist stays hidden with an
        explanatory status message; the user may retry by reselecting
        or re-toggling the POI source, which calls
        _refresh_poi_categories() again.
        """
        nonlocal poi_categories
        poi_categories = {}
        _rebuild_category_checklist()
        _set_poi_category_reading_state(False)
        if reason == "timeout":
            category_status_var.set(
                "Could not read POI source within 60 seconds.")
        else:
            category_status_var.set("Could not read POI source.")

    def _poll_poi_category_queue(result_queue, deadline, source_type):
        """
        Runs on the main thread via win.after() polling -- identical
        fresh-queue.Queue()-per-call skeleton to the one the Land
        Parcel section's own background conflict-check polling used to
        follow, before that mechanism was removed in Task 8/D3b (see
        this function's own reasoning below: the queue is always
        checked before the deadline, no generation counter needed).

        Takes source_type (captured by _refresh_poi_categories() at the
        moment the read started) so that a zero-eligible-categories
        result knows exactly which mode ("local" or "db") to revert --
        see _handle_zero_eligible_categories() above. Since
        _refresh_poi_categories() refuses to start a second overlapping
        read while one is already in flight, source_type here is
        always still the mode that was actually being read.
        """
        nonlocal poi_categories
        if not win.winfo_exists():
            return
        try:
            buckets, error = result_queue.get_nowait()
        except queue.Empty:
            if time.time() >= deadline:
                _handle_poi_category_discovery_failure("timeout")
            else:
                win.after(100, lambda: _poll_poi_category_queue(
                    result_queue, deadline, source_type))
            return

        if error is not None:
            _handle_poi_category_discovery_failure("failure")
            return

        if not buckets:
            # A successful read that found zero eligible categories --
            # treated as a rejected source selection, not a valid empty
            # state. See _handle_zero_eligible_categories()'s docstring.
            _handle_zero_eligible_categories(source_type)
            return

        poi_categories = buckets
        # No _destroy_category_section()/_create_category_section() call
        # here -- reading start (call site above) already guaranteed a
        # fresh section, and nothing has grown any canvas between then
        # and here, so it is still guaranteed unpoisoned at this point.
        _rebuild_category_checklist()
        _set_poi_category_reading_state(False)
        count = len(poi_categories)
        _set_category_status_emphasis(True)
        category_status_var.set(
            "SELECT THE LANDMARK TYPES TO COUNT AND THEIR METHOD:")
        category_count_var.set(
            f"{count} landmark categor{'y' if count == 1 else 'ies'} found.")
        category_checklist_outer.pack(fill="x", pady=(2, 0))
        _reflow_window()

    def _refresh_poi_categories():
        """
        Background-discovers the currently selected POI source's
        landmark categories (Task 1). Triggered ONLY by a POI source
        change -- a fresh Browse/Select, or toggling Local <-> Database
        (see browse_poi_file() / _on_poi_db_selected() / _toggle_poi()
        above) -- deliberately NEVER by a checkbox or (future) method
        change. This is the explicit, approved dependency for this
        cluster:

            POI source changes
                -> read/discover fclass categories -> rebuild checklist
            Checkbox/method changes
                -> modify existing GUI state only -> NO rediscovery

        Rereading the POI source on every checkbox interaction would be
        unnecessary I/O and unnecessary state churn for state that has
        nothing to do with which source is selected.

        Uses the same worker-thread -> queue.Queue() -> win.after()
        polling skeleton this file's own Land Parcel section used to use
        for its own existing-output-column check (that background
        mechanism was later removed in Task 8/D3b, once its check was
        relocated to run at Run-click time instead of at selection time
        -- but this discovery here still legitimately needs to run in
        the background at selection time, so the pattern itself is
        still reused, not the retired function). The worker thread
        below calls only _discover_poi_categories() (pure, Tk-free);
        all Tkinter mutation happens in _poll_poi_category_queue() /
        _rebuild_category_checklist() on the main thread via
        win.after(). Gives up after 60 seconds with no result.

        Deliberately does NOT cache the result across calls -- every
        call, whether triggered by a fresh Browse/Select or by toggling
        Local <-> Database, always performs a real read.
        """
        nonlocal poi_categories
        if poi_category_reading:
            # A discovery is already in flight -- do not start a
            # second, overlapping one.
            return

        source_type = poi_source_type.get()
        path_or_table = (
            poi_local_path.get() if source_type == "local" and poi_local_path.get()
            else poi_db_table.get() if source_type == "db" and poi_db_table.get()
            else None
        )

        if not path_or_table:
            # Nothing selected for this mode -- hide EVERYTHING this
            # section shows: the guide text (category_status_row), AND
            # the "POI"/"Method" headers, Check All/Uncheck All links,
            # the checklist body, and the bottom category-count label.
            # Explicit request: none of this should appear until a POI
            # source is actually selected -- not just the guide text.
            #
            # THIS is the exact call site that produced the reported
            # "toggling Local -> Database leaves Local's checklist size
            # behind" bug. Root cause (confirmed via live headless
            # experimentation): once poi_canvas/method_canvas are
            # configure(height=N)'d with a nontrivial N while packed,
            # category_frame's own winfo_reqheight() gets permanently
            # pinned at that value -- plain pack_forget() (the previous
            # approach here) does NOT clear it, nor does update_
            # idletasks(), pack_propagate() toggling, or even a full
            # win.withdraw()/deiconify() cycle. Destroying and
            # recreating category_frame itself is the only thing that
            # does -- see _create_category_section()'s docstring for
            # the full experimental history.
            poi_categories = {}
            _destroy_category_section()
            _create_category_section()
            _rebuild_category_checklist()
            _reflow_window()
            return

        result_queue = queue.Queue()

        def worker():
            result_queue.put(_discover_poi_categories(source_type, path_or_table))

        deadline = time.time() + 60  # see _poll_poi_category_queue()
        _set_poi_category_reading_state(True)
        # Force the "Detecting..." status (and any still-pending visual
        # change from a Local<->Database toggle in _toggle_poi() above,
        # which calls this function before the toggle's own .config()
        # calls have necessarily been painted) to actually render NOW,
        # before starting the worker thread below. Without this, the
        # repaint is only SCHEDULED, not yet drawn -- and geopandas/
        # pandas category discovery on a large POI source is CPU-heavy
        # enough that the worker thread can hold the GIL for a
        # noticeable stretch right after starting, delaying the main
        # thread's own repaint and making the status update appear to
        # lag 1-2 seconds behind the click instead of showing
        # immediately. update_idletasks() flushes the pending redraw
        # synchronously, so the user sees "Reading POI source..." (and
        # the completed toggle) the instant this function is called,
        # not whenever the worker thread next yields the GIL.
        win.update_idletasks()
        threading.Thread(target=worker, daemon=True).start()
        win.after(100, lambda: _poll_poi_category_queue(
            result_queue, deadline, source_type))

    # ── SECTION 2C: SEARCH DISTANCE (Aerial / Road) ──────────────
    # Task 4 (Cluster B): two independent radius inputs, one per
    # distance method. D3c: these are now the only radius fields in
    # this tool -- the old single "Search Radius" section (SECTION 3)
    # has been removed entirely.
    #
    # Captured into distance_radius_anchor (declared as a placeholder
    # before _create_category_section() -- see there) so that function
    # can position a RECREATED category_frame back in its correct
    # visual slot (before this section), rather than pack() silently
    # appending it to the very end of the window on every recreation
    # after the first -- see _create_category_section()'s own comment
    # on this anchor for the full explanation.
    distance_radius_anchor = section_label(win, "Search Distance (Aerial / Road)")

    dual_radius_frame = tk.Frame(win)
    dual_radius_frame.pack(fill="x", padx=18, pady=2)

    aerial_radius_row = tk.Frame(dual_radius_frame)
    aerial_radius_row.pack(fill="x", pady=2)
    tk.Label(aerial_radius_row, text="Aerial radius (meters):",
             anchor="w", width=18).pack(side="left")
    # Starts disabled -- nothing is checked yet at construction time,
    # so no checked category can be using the Aerial method yet (Task
    # 4). _recompute_radius_enablement() below is the single source of
    # truth for this state from here on.
    aerial_radius_entry = tk.Entry(aerial_radius_row, textvariable=aerial_radius_var,
                                    width=10, state="disabled")
    aerial_radius_entry.pack(side="left", padx=(4, 0))

    road_radius_row = tk.Frame(dual_radius_frame)
    road_radius_row.pack(fill="x", pady=2)
    # Label reads "Road distance", not "Road radius" -- a road-network
    # path length is a linear distance along the network, not a
    # circular/omnidirectional radius (which "Aerial radius" correctly
    # is, being straight-line). Deliberately deviates from Document 1
    # Task 4's literal field-label wording for this reason (confirmed
    # decision). The underlying variable/widget names (road_radius_var,
    # road_radius_row, road_radius_entry) are left as-is -- internal
    # identifiers, not user-facing text.
    tk.Label(road_radius_row, text="Road distance (meters):",
             anchor="w", width=18).pack(side="left")
    road_radius_entry = tk.Entry(road_radius_row, textvariable=road_radius_var,
                                  width=10, state="disabled")
    road_radius_entry.pack(side="left", padx=(4, 0))

    def _recompute_radius_enablement():
        """
        Task 4: each radius Entry is enabled iff at least one CHECKED
        category currently has that field's corresponding method
        selected -- e.g. if every checked category is set to Aerial,
        the Road distance input stays disabled.

        Only ever toggles Entry(state=...) -- textvariable is never
        touched here, so a value entered before a field became
        disabled is preserved unchanged and simply reappears,
        editable, whenever the field is re-enabled later (Task 4's
        explicit "disabling does NOT clear the value" requirement).

        Called from two places only: _on_category_checked_toggle()
        (a row's checkbox changed) and each Radiobutton's own
        command= (a row's method changed) -- these are the only two
        interactions that can change which methods are in use among
        CHECKED categories. Also calls _update_run_button_state() at
        the end (D3c) -- this is the single correct place for that,
        since checking/unchecking a category or switching its method
        is exactly the set of interactions that can change whether the
        Run button's dynamic-checklist readiness computation (checked-
        category count, which radii are actually relevant, whether a
        Road Network source is required) needs to be re-evaluated.
        """
        any_aerial = any(
            poi_category_vars[key].get() and poi_category_method_vars[key].get() == "aerial"
            for key in poi_category_vars
        )
        any_road = any(
            poi_category_vars[key].get() and poi_category_method_vars[key].get() == "road"
            for key in poi_category_vars
        )
        aerial_radius_entry.config(state="normal" if any_aerial else "disabled")
        road_radius_entry.config(state="normal" if any_road else "disabled")
        _update_run_button_state()

    def _on_category_checked_toggle(key):
        """
        Per-row Checkbutton command= (Task 3, refined per explicit
        request): enables/disables ONLY that row's own Aerial/Road
        radio pair, based on whether the row is now checked -- an
        unchecked category's method selection is irrelevant since it
        won't be counted at all. Touches no other row.

        Grayed-out rows must show NEITHER radio as selected -- merely
        disabling the widgets while leaving method_var's value in
        place would still render a grayed selected dot (Tkinter draws
        a disabled Radiobutton's selection state from its variable
        regardless of state=), which reads as "this is still the
        active choice" even though the row is excluded from
        processing. So the CURRENTLY VISIBLE method is handled
        differently depending on direction:

          Unchecking (was checked -> now unchecked):
            1. Save method_var's current value (whatever the user last
               explicitly picked, or the Task 3 default if never
               touched) into poi_category_remembered_method[key].
            2. Clear method_var to "" -- neither radio's `value=`
               ("aerial"/"road") matches "", so neither shows selected.
            3. Disable both radios.

          Checking (was unchecked -> now checked):
            1. Restore method_var from poi_category_remembered_method
               [key] -- whatever was last explicitly chosen for this
               category, or "road" (Task 3's default) if this row has
               never been checked before or the user never changed it
               away from the default.
            2. Enable both radios.

          This means a category's chosen method survives any number of
          uncheck/recheck cycles -- e.g. check -> switch to Aerial ->
          uncheck (Aerial remembered, nothing visibly selected) ->
          recheck (Aerial reappears selected, not reset to the Road
          default) -> switch back to Road -> uncheck -> recheck (Road
          reappears selected). Each explicit radio click updates
          method_var immediately via Tkinter's own shared-variable
          mechanism; this function only needs to persist whatever
          method_var held at the moment of unchecking.

        Deliberately does NOT trigger POI category rediscovery --
        checkbox changes are explicitly excluded from
        _refresh_poi_categories()'s trigger set (see its docstring's
        POI-source-change-only dependency).

        Ends by calling _recompute_radius_enablement(), since checking
        or unchecking a category can change which methods are
        currently in use among checked categories.
        """
        nonlocal poi_category_remembered_method
        widgets = poi_category_radio_widgets.get(key)
        if not widgets:
            return
        method_var = poi_category_method_vars[key]
        is_checked = poi_category_vars[key].get()

        if is_checked:
            method_var.set(poi_category_remembered_method.get(key, "road"))
            state = "normal"
        else:
            current = method_var.get()
            if current:
                poi_category_remembered_method[key] = current
            method_var.set("")
            state = "disabled"

        for radio in widgets:
            radio.config(state=state)
        _recompute_radius_enablement()

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
        Run button handler: validates Land Parcel + POI + the dynamic
        checklist (checked categories, per-method radii, Road Network
        source if needed) + Output selections are present, checks for
        existing output-column conflicts (PRIORITY 1), runs the local
        output-file conflict check (PRIORITY 2), and DB-output table
        resolution (PRIORITY 3) -- each able to cancel the whole run --
        then destroys this window and hands off to run_processing().
        Sets the module-level barangay_source, poi_source, output_mode,
        checked_categories, target_column_map, aerial_radius_meters,
        road_radius_meters, road_source, and
        parcel_output_column_overrides globals on success.

        D3c note: radius_var/radius_meters (the single old fixed-radius
        field) have been fully retired -- see this function's own
        dedicated validation block, right after POI validation below,
        for the precedence now actually used (checked categories, then
        Aerial radius if needed, then Road radius if needed, then Road
        Network source if needed).
        """
        global barangay_source, poi_source, output_mode

        # validate parcel
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

        # ------------------------------------------------------------------
        # D3a/D3c -- dynamic-checklist validation (approved precedence,
        # steps 3-6). Builds checked_categories/target_column_map/
        # aerial_radius_meters/road_radius_meters/road_source (module-
        # level globals declared above) -- consumed by run_processing()
        # (D3c wired this in; the old fixed-radius pipeline these
        # globals originally coexisted alongside has since been
        # retired entirely).
        # ------------------------------------------------------------------
        global checked_categories, target_column_map
        global aerial_radius_meters, road_radius_meters, road_source

        # Step 3: no checked categories. This is a validation rule on
        # user input (the checklist), not a re-statement of the
        # separately-deferred "zero eligible categories" question
        # (POI discovery producing no valid fclass buckets at all) --
        # that remains an explicitly NOT-yet-approved proposal and is
        # NOT implemented here. This step only blocks the case where
        # eligible categories DO exist but the user checked none of
        # them, which would otherwise let a run reach run_processing()
        # with an empty target list -- a silent no-op.
        checked_categories = {
            key: poi_category_method_vars[key].get()
            for key, var in poi_category_vars.items()
            if var.get()
        }
        if not checked_categories:
            messagebox.showerror("Missing Input",
                "Please check at least one landmark type to count.")
            return
        target_column_map = dict(zip(
            checked_categories.keys(),
            derive_target_columns(checked_categories.keys())))

        any_aerial = any(m == "aerial" for m in checked_categories.values())
        any_road = any(m == "road" for m in checked_categories.values())

        # Step 4: Aerial radius -- only validated if at least one
        # checked category actually uses the Aerial method. Left None
        # (its module-level default) if not currently relevant, rather
        # than validated-but-unused, so a stale/invalid value sitting
        # in a currently-disabled field can never block a run that
        # doesn't need it.
        if any_aerial:
            try:
                aerial_radius_meters = float(aerial_radius_var.get())
                if aerial_radius_meters <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid Input",
                    "Please enter a valid positive number for the Aerial radius.")
                return
        else:
            aerial_radius_meters = None

        # Step 5: Road distance -- same reasoning as Step 4, mirrored
        # for the Road method.
        if any_road:
            try:
                road_radius_meters = float(road_radius_var.get())
                if road_radius_meters <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid Input",
                    "Please enter a valid positive number for the Road distance.")
                return
        else:
            road_radius_meters = None

        # Step 6: Road Network source -- required ONLY if at least one
        # checked category uses Road (Task 5's explicit requirement).
        # Generic message, deliberately not naming which category
        # triggered it -- matches Document 1's exact wording ("Please
        # select a road network source"), independent of however many
        # categories are actually Road-method.
        if any_road:
            if road_source_type.get() == "local":
                if not road_local_path:
                    messagebox.showerror("Missing Input",
                        "Please select a road network source.")
                    return
                road_source = ("local", (road_local_path, road_local_layer))
            else:
                if not road_db_table:
                    messagebox.showerror("Missing Input",
                        "Please select a road network source.")
                    return
                road_source = ("db", (road_db_table,))
        else:
            road_source = None

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
        # PRIORITY 1: existing-output-column conflict check -- warn if the
        # selected Land Parcel source already has any of the currently
        # checked categories' dynamic output columns. Resolved here on the
        # main thread, BEFORE win.destroy(), so the dialog has a live
        # parent AND declining leaves the fully-configured win intact
        # instead of forcing a from-scratch reopen -- same "resolve before
        # destroy" pattern PRIORITY 2/3 below already follow.
        #
        # Explicit correction: this check briefly lived inside run_
        # processing() instead (D3b/D3c), which only ever runs AFTER win.
        # destroy() -- meaning a "No" response there could never actually
        # return the user to their configuration (the window was already
        # gone), and worse, since PRIORITY 2/3 still ran here in on_run()
        # (before destroy), this check ended up visibly running AFTER them
        # instead of before -- reversing the intended 1->2->3 order.
        # Task 8's real intent (no more background pre-fetch machinery --
        # always a fresh, synchronous read, never a cached result) is still
        # fully honored here: this is a synchronous read happening right
        # now, at Run-click time, not a background-prefetched result read
        # back later -- only the LOCATION relative to win.destroy() has
        # been corrected.
        #
        # Skips the check ENTIRELY if there's nothing to check for
        # (targets_for_check empty) -- mirrors influence_map_to_land_
        # parcel.py's own established pattern for this exact situation.
        # If the check itself cannot verify (conflicts is None -- a read
        # failure), this falls through as "no known conflict/no overrides"
        # rather than aborting here -- the SAME source is about to be read
        # again inside run_processing() for actual processing, so a
        # genuine read failure surfaces naturally via that function's own
        # try/except instead of needing a second, separate failure path
        # here.
        # ------------------------------------------------------------------
        global parcel_output_column_overrides
        targets_for_check = sorted(set(target_column_map.values())) if target_column_map else []
        if targets_for_check:
            conflicts = _check_parcel_poi_conflicts(
                list(barangay_source[1]), barangay_source[0], targets_for_check)
        else:
            conflicts = []
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
                parent=win
            )
            if not proceed:
                print("Run cancelled by user (existing output column(s) found).")
                return
            # Preserve each source's existing column name(s)/casing exactly
            # -- e.g. a detected "caMA_NUM_SCHOOL" is written back to
            # "caMA_NUM_SCHOOL", not the default "CAMA_NUM_SCHOOL" -- so no
            # duplicate column is ever created regardless of the existing
            # casing. A source with no entry here (no conflict was found,
            # or the check could not verify) simply uses target_column_map's
            # own default names, via the resolved_target_column_map
            # computation in each per-source loop inside run_processing().
            parcel_output_column_overrides = dict(conflicts)
        else:
            parcel_output_column_overrides = {}

        # PRIORITY 2: file conflict check -- warn if an output file with
        # the same name already exists in the chosen output folder.
        # Resolved here on the main thread, before win.destroy(), so the
        # dialog has a live parent. Cancel aborts the run; main window
        # stays open.
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
        # call site moved here. resolved_table_name is passed into
        # run_processing() as a parameter -- same approach already used
        # in lot_location.py, road_surface.py, road_density.py,
        # land_shape_compactness.py, road_frontage.py, and terrain.py.
        # resolved_outcome is not threaded through (same as those files)
        # because nothing downstream in this file's processing loop
        # consumes it -- only resolved_table_name is read (see the
        # output_table fallback near "Falls back to the old...").
        # This block sits entirely before run_processing()'s own
        # try/except/finally error-handling wrapper (added in a prior
        # fix) -- removing it from run_processing() does not touch,
        # shrink, or reorder that wrapper in any way.
        # ------------------------------------------------------------------
        resolved_table_name = None
        if output_mode[0] == "db":
            _resolve_creds = load_db_credentials()
            if not _resolve_creds:
                return
            _resolve_schema = _resolve_creds["schema"]
            resolved_table_name, _resolved_outcome = resolve_db_output_table(
                win, _resolve_schema, barangay_source
            )
            if resolved_table_name is None:
                print("Run cancelled by user (database output table not confirmed).")
                return

        win.destroy()
        run_processing(root, overwrite_mode, resolved_table_name)

    # Single source of truth for the Run button's enabled/disabled
    # colors -- used both at button creation and inside
    # _update_run_button_state() below, so there's only one place to
    # change if the theme changes later.
    RUN_BTN_BG_ENABLED  = "#2e7d32"
    RUN_BTN_FG_ENABLED  = "white"
    RUN_BTN_BG_DISABLED = "#e0e0e0"
    RUN_BTN_FG_DISABLED = "#888888"

    def _is_valid_radius(value):
        """Same acceptance rule on_run() applies (float, > 0)."""
        try:
            r = float(value)
        except (TypeError, ValueError):
            return False
        return r > 0

    def _update_run_button_state():
        """
        Single source of truth for whether the Run button may be
        pressed. Disabled (with an explanatory status message) until a
        Land Parcel source, a POI source, at least one checked
        category, a valid radius for every method actually in use, a
        Road Network source (if any checked category uses Road), and
        an Output destination are all present -- mirrors on_run()'s own
        D3a/D3c validation precedence exactly, so the Run button's live
        state never disagrees with what clicking it would actually do.
        Does not set any module-level global itself -- purely a live
        readiness check as the user configures.

        Explicit bg/fg/cursor toggling (not just state=) is required:
        Tkinter does NOT automatically gray out a classic tk.Button's
        custom bg/fg when state="disabled", and does not suppress a
        widget's assigned cursor either -- both must be set explicitly
        for each state.
        """
        has_parcel = bool(parcel_local_path) if parcel_source_type.get() == "local" else bool(parcel_db_table)
        has_poi = bool(poi_local_path.get()) if poi_source_type.get() == "local" else bool(poi_db_table.get())
        has_output = bool(output_local_dir.get()) if output_dest_type.get() == "local" else True

        live_checked = {
            key: poi_category_method_vars[key].get()
            for key, var in poi_category_vars.items()
            if var.get()
        }
        any_aerial = any(m == "aerial" for m in live_checked.values())
        any_road = any(m == "road" for m in live_checked.values())
        aerial_ok = _is_valid_radius(aerial_radius_var.get()) if any_aerial else True
        road_radius_ok = _is_valid_radius(road_radius_var.get()) if any_road else True
        has_road_source = (
            bool(road_local_path) if road_source_type.get() == "local" else bool(road_db_table)
        ) if any_road else True

        # D3b (Task 8): no more "existing-output-column check in
        # progress" gate here -- that check used to run in the
        # background the moment a Land Parcel source was selected, and
        # this gate existed to block Run while its result was still
        # unknown. It has been relocated to run inside on_run()'s own
        # PRIORITY 1 block, synchronously, at the moment Run is
        # actually clicked, before win.destroy() -- there is no more
        # in-between "reading" state for the Run button to ever need
        # to gate against.
        if not has_parcel:
            run_status_var.set("Please select a Land Parcel source.")
            ready = False
        elif not has_poi:
            run_status_var.set("Please select a POI source.")
            ready = False
        elif not live_checked:
            run_status_var.set("Please check at least one landmark type to count.")
            ready = False
        elif not aerial_ok:
            run_status_var.set("Please enter a valid Aerial radius.")
            ready = False
        elif not road_radius_ok:
            run_status_var.set("Please enter a valid Road distance.")
            ready = False
        elif not has_road_source:
            run_status_var.set("Please select a road network source.")
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

    # Live-updates the Run button as the user types in either radius
    # field, without requiring focus-out or Enter.
    aerial_radius_var.trace_add("write", lambda *_: _update_run_button_state())
    road_radius_var.trace_add("write", lambda *_: _update_run_button_state())

    _toggle_parcel()
    _toggle_road()
    _toggle_poi()
    _toggle_output()
    _update_run_button_state()


# ========================================
# DB OUTPUT RESOLUTION
# ========================================
def resolve_db_output_table(root, schema, barangay_source):
    """
    Determines the DB-output destination table for the Land Parcel
    source, BEFORE any processing or writing starts -- same "resolve
    everything up front" philosophy as ask_overwrite_dialog() (see
    run_processing()). This function runs synchronously, called from
    on_run() before win.destroy() -- before run_processing() (and its
    Part 3 background worker thread) is even invoked. It is still
    called once, up front, for separation of
    responsibilities: this function owns ALL user interaction and
    overwrite decisions, so the processing/write logic further below
    never has to ask any UI or overwrite question of its own.

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
# RUN PROCESSING
# ========================================
def run_processing(app_root, overwrite_mode=None, resolved_table_name=None):
    """
    Orchestrates the full run on a background worker thread, following
    this file's own established worker-thread + queue.Queue() +
    .after()-polling pattern (the same one
    _refresh_poi_categories()/_poll_poi_category_queue() use for POI
    category discovery, see those functions' own docstrings): loads
    the POI data once, builds a run-level RoadContext exactly once if
    any checked category uses the Road method (D3c), then for each
    Land Parcel file/table, opens a fresh progress window, runs
    process_poi_counts_dynamic() (the dynamic, per-category
    Aerial/Road counting engine -- D2), and saves the result either
    locally (.gpkg, optionally opened in Global Mapper) or to PostGIS
    (matched to an existing table by name for local sources, or
    replaced in place for DB sources).

    D3c retires the OLD fixed-4-category process_poi_counts() (Task
    2's actual removal) -- this function is now the ONLY processing
    path; there is no longer an inert parallel pipeline coexisting with
    it, unlike every prior cluster of this redesign.

    THREADING (Part 3): the "Selections incomplete" guard right below
    stays on the main thread (instantaneous, and already Tkinter-safe
    -- run_processing() is itself called from on_run() on the main
    thread). Once that guard passes, everything else -- credential
    loading, engine construction, POI/Road Network/Land-Parcel reads,
    road_context construction, and the full per-source
    process_poi_counts_dynamic()/save loop -- runs inside the nested
    worker() closure, on a background threading.Thread(daemon=True).
    worker() contains NO Tkinter calls of any kind (confirmed via a
    full trace of every call in this function's old synchronous body,
    including load_in_global_mapper(), which needed no changes -- it
    only uses ctypes/Win32 EnumWindows and subprocess.Popen, neither
    of which touches Tkinter). Every Tkinter call (create_progress_
    window(), update_progress(), close_progress_window(), and the
    final result messagebox) instead happens exclusively in the nested
    _poll_run_processing_queue() closure, on the main thread, via
    app_root.after(100, ...) polling -- never in worker(). worker()
    communicates with the poller exclusively through result_queue
    (queue.Queue()), using these message tags, always in this order
    per source: ("new_source", total) -> zero or more
    ("progress", current, total, msg) -> ("source_done",); and exactly
    one terminal message at the very end of the run:
    ("aborted_silently",) if load_db_credentials() failed (preserves
    the exact pre-Part-3 behavior: close the progress window if one
    happened to be open, but show no dialog at all -- this was already
    true before Part 3, since that early return happened before any
    exception was raised, skipping the old function's own trailing
    success/error dialog code), ("finished", None) on a clean
    completion (shows the existing success dialog), or
    ("finished", error_str) if any exception was raised (shows the
    existing error dialog, same message as before). The FIFO ordering
    of queue.Queue() guarantees a source's "new_source" message is
    always drained by the poller before any "progress" message queued
    behind it, even if the worker races ahead and queues several
    progress updates before the next 100ms poll tick.

    process_poi_counts_dynamic()'s own progress_cb contract (callable
    (current, total, msg=...) -> bool, a False return stopping early)
    is preserved exactly -- worker_progress_cb() below satisfies it
    without ever touching Tkinter: it pushes a "progress" message onto
    result_queue, then returns `not PROG_STOP_FLAG.get("stop")`.
    PROG_STOP_FLAG is a plain dict, not inherently thread-safe as a
    general synchronization primitive -- but nothing in this file ever
    sets PROG_STOP_FLAG["stop"] = True during processing (no Cancel
    button is wired to it; see create_progress_window()'s own
    docstring), so there is no concurrent mutation for
    worker_progress_cb()'s read to race against. Referencing
    PROG_STOP_FLAG by its global name (not a captured local alias)
    also means a fresh create_progress_window() reset (on the main
    thread, for the NEXT source) is always visible to the worker's next
    read, exactly matching the pre-Part-3 behavior of update_progress()
    reading the same global.

    Wraps EVERYTHING from credential loading through the final output
    save in one try/except inside worker(), so any exception -- a
    malformed DB credential, a Road Network source read/geometry
    failure, a Land Parcel/POI read failure, an unexpected error inside
    process_poi_counts_dynamic(), a save failure -- is caught and sent
    to the poller as a ("finished", error_str) message (instead of
    propagating silently as an uncaught crash on a background thread,
    which Python would otherwise only print a traceback for and never
    surface to the user at all). The poller always closes the progress
    window on any terminal message, regardless of outcome -- the same
    unconditional-cleanup guarantee the old synchronous finally: block
    provided.

    Args:
        app_root: the parent Tk root, used to open progress/error
        dialogs, and as the anchor for _poll_run_processing_queue()'s
        own app_root.after(...) polling (NOT win -- win no longer
        exists by the time run_processing() runs; on_run() always
        calls win.destroy() before calling this function).
        overwrite_mode (str | None): "overwrite" or "new", from
        ask_overwrite_dialog() in on_run() -- only relevant for local
        output mode.
        resolved_table_name (str | None): the already-confirmed DB
        output table name from resolve_db_output_table() in on_run() --
        only relevant for DB output mode.
    """
    global barangay_source, poi_source, output_mode
    global checked_categories, target_column_map
    global aerial_radius_meters, road_radius_meters, road_source

    if not barangay_source or not poi_source or not output_mode:
        messagebox.showerror("Error", "Selections incomplete.")
        return

    result_queue = queue.Queue()

    def worker_progress_cb(current, total, msg=None):
        """
        Passed as process_poi_counts_dynamic()'s progress_cb -- runs on
        the worker thread, alongside everything else in worker() below.
        Touches no Tkinter object; see this function's own docstring
        (THREADING section) for the PROG_STOP_FLAG safety argument.
        """
        result_queue.put(("progress", current, total, msg))
        return not PROG_STOP_FLAG.get("stop")

    def worker():
        """
        Runs entirely on a background thread -- pure computation plus
        file/DB I/O only, NO Tkinter calls of any kind. Progress and
        the terminal outcome are communicated back to the main thread
        exclusively via result_queue.put(...); _poll_run_processing_
        queue() below is the only code that ever touches
        PROG_WIN/PROG_BAR/PROG_LABEL or calls messagebox. See this
        function's enclosing docstring for the full message-tag
        protocol.
        """
        try:
            creds = load_db_credentials()
            if not creds:
                result_queue.put(("aborted_silently",))
                return

            schema = creds["schema"]
            engine = create_engine(
                f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
            )

            # parcel_output_column_overrides is already resolved by the
            # time this function runs -- on_run()'s own PRIORITY 1 (before
            # win.destroy()) is what performs the conflict check and the
            # "Existing output column(s) found" confirmation dialog now;
            # see on_run()'s docstring for why this must happen there, not
            # here (a "No" response must be able to return the user to
            # their still-live configuration window, which is impossible
            # once this function is even reached -- win.destroy() has
            # already run by then).

            # resolved_table_name: the DB-output destination table. Resolution
            # responsibility now belongs to on_run() (PRIORITY 3), on the main
            # thread, BEFORE win.destroy() -- see Fix 1. By the time it reaches
            # this function it is treated as an already-validated value: either
            # None (local output, or output_mode[0] != "db") or a confirmed
            # table name (DB output, user already had the chance to cancel in
            # on_run()). No re-resolution or re-validation happens here.

            # ============================================================
            # Error handling -- newly added, previously missing entirely.
            # ============================================================
            # Everything from here down used to run with NO try/except at all:
            # any exception (a network failure during OSM graph download, a
            # bad geometry, a DB write error, etc.) would propagate all the
            # way up uncaught -- no error dialog would ever appear, and if it
            # happened while a per-source progress window was open
            # (create_progress_window(), one per source in the loop, now
            # opened by the poller in response to this function's own
            # "new_source" message), that window would never be closed
            # either. As of Part 3, this try/except's own except clause
            # (below) always sends a terminal message so the poller can
            # close the window and show the right dialog no matter how this
            # function exits.
            #
            # error_message is captured (sent via the terminal queue
            # message) instead of calling messagebox.showerror() directly
            # inline, so that no modal dialog is ever shown while a
            # progress window might still be alive -- same principle
            # already applied to POI_All_Distance.py's run_with_progress()/
            # task() (and, as of Part 3, also required simply because this
            # function runs on a background thread and must never call
            # Tkinter directly at all).
            print("\n🔷 Loading POI data...")
            if poi_source[0] == "local":
                poi_gdf = gpd.read_file(poi_source[1][0])
            else:
                poi_gdf = read_postgis_clean(poi_source[1][0], engine, schema)
            print(f"✅ Loaded {len(poi_gdf)} POIs")

            # ============================================================
            # D3c: build road_context ONCE per run -- the expensive, run-
            # level road-network artifacts (D1's build_road_network_index())
            # -- conditionally, only if at least one checked category
            # currently uses the Road method (Task 5's explicit
            # requirement: no Road Network source work at all when every
            # checked category is Aerial). Passed into every parcel's
            # process_poi_counts_dynamic() call below unchanged -- never
            # rebuilt per parcel (see RoadContext's own docstring for the
            # full performance rationale this whole redesign exists to
            # fix).
            #
            # Working CRS is detected from the Road Network source + POI
            # source only (NOT every Land Parcel source too) -- this
            # tool's whole operating domain is a single LGU's cadastral
            # area, always far smaller than a single PRS92 zone's ~2°
            # longitude span, so this is sufficient without needing to
            # pre-load every Land Parcel source before this point (which
            # would mean reading each one twice: once here, once again in
            # the per-source loop below).
            #
            # Deliberately inside this function's existing try/except:
            # Task 6's "no usable line geometry" error (build_road_
            # network_index()), or any Road Network source read failure
            # here, must surface via the SAME graceful error dialog as
            # every other structural failure in this function -- not a
            # separate, uncaught crash.
            # ============================================================
            road_context = None
            if road_source is not None and any(m == "road" for m in checked_categories.values()):
                print("\n🔷 Loading Road Network source...")
                if road_source[0] == "local":
                    road_path, road_layer = road_source[1]
                    road_gdf = (gpd.read_file(road_path, layer=road_layer) if road_layer
                                else gpd.read_file(road_path))
                else:
                    road_table = road_source[1][0]
                    road_gdf = read_postgis_clean(road_table, engine, schema)
                print(f"✅ Loaded {len(road_gdf)} road features")

                working_epsg = _detect_road_working_crs([
                    ("Road Network", road_gdf), ("POI", poi_gdf)])
                road_gdf_projected = road_gdf.to_crs(epsg=working_epsg)
                edges_list, edge_geoms, edge_tree = build_road_network_index(road_gdf_projected)
                road_context = RoadContext(
                    edges_list=edges_list, edge_geoms=edge_geoms,
                    edge_tree=edge_tree, working_epsg=working_epsg)
                print(f"✅ Road network index built ({len(edges_list)} edges, "
                      f"EPSG:{working_epsg})")


            if barangay_source[0] == "local":
                for path in barangay_source[1]:
                    base_name = os.path.splitext(os.path.basename(path))[0]
                    print(f"\n🔷 Processing: {base_name}")
                    gdf = gpd.read_file(path)

                    # Preserves each source's existing output column name(s)/
                    # casing exactly, if a conflict was detected and confirmed
                    # in on_run()'s own PRIORITY 1 conflict check above --
                    # e.g. a detected "caMA_NUM_SCHOOL" is written back to
                    # "caMA_NUM_SCHOOL", not the default "CAMA_NUM_SCHOOL".
                    # Defaults to target_column_map's own name for any
                    # checked category this source has no override for.
                    # Unlike the old fixed-4-column override logic (num_
                    # police_col/num_park_col/num_mall_col/num_others_col),
                    # this resolves ALL currently-checked categories
                    # dynamically, in one dict comprehension, since the
                    # actual set of columns is only known at Run time.
                    output_col_overrides = parcel_output_column_overrides.get(path, {})
                    resolved_target_column_map = {
                        key: output_col_overrides.get(default_col, default_col)
                        for key, default_col in target_column_map.items()
                    }

                    result_queue.put(("new_source", len(gdf)))
                    result = process_poi_counts_dynamic(
                        gdf, poi_gdf, checked_categories, aerial_radius_meters,
                        road_radius_meters, road_context, resolved_target_column_map,
                        progress_cb=worker_progress_cb)
                    result_queue.put(("source_done",))

                    if output_mode[0] == "local":
                        desired_base_name = base_name
                        candidate_path = os.path.join(output_mode[1], f"{desired_base_name}.gpkg")
                        had_conflict = os.path.exists(candidate_path)
                        if had_conflict and overwrite_mode == "new":
                            base_name = resolve_output_base_name(output_mode[1], desired_base_name)
                        else:
                            base_name = desired_base_name
                        out = os.path.join(output_mode[1], f"{base_name}.gpkg")
                        _write_gpkg(result, out)
                        print(f"✅ Saved: {out}")
                        load_in_global_mapper(out)
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
                        output_table = resolved_table_name if resolved_table_name is not None else base_name.lower()
                        with engine.begin() as conn:
                            result.to_postgis(output_table, conn, schema=schema,
                                              if_exists="replace", index=False)
                        print(f"✅ Saved to DB: {output_table}")
            else:
                # Database Land Parcel sources: same dynamic override
                # resolution as the LOCAL branch above -- preserves the
                # exact existing column casing(s) detected in on_run()'s
                # own PRIORITY 1 conflict check.
                for table in barangay_source[1]:
                    print(f"\n🔷 Processing DB table: {table}")
                    gdf = read_postgis_clean(table, engine, schema)

                    output_col_overrides = parcel_output_column_overrides.get(table, {})
                    resolved_target_column_map = {
                        key: output_col_overrides.get(default_col, default_col)
                        for key, default_col in target_column_map.items()
                    }

                    result_queue.put(("new_source", len(gdf)))
                    result = process_poi_counts_dynamic(
                        gdf, poi_gdf, checked_categories, aerial_radius_meters,
                        road_radius_meters, road_context, resolved_target_column_map,
                        progress_cb=worker_progress_cb)
                    result_queue.put(("source_done",))

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
                        print(f"✅ Saved: {out}")
                        load_in_global_mapper(out)
                    else:
                        with engine.begin() as conn:
                            result.to_postgis(table, conn, schema=schema,
                                              if_exists="replace", index=False)
                        print(f"✅ Updated DB table: {table}")

        except Exception as e:
            result_queue.put(("finished", str(e)))
            return

        result_queue.put(("finished", None))

    def _poll_run_processing_queue():
        """
        Runs on the main thread via app_root.after() polling -- the
        counterpart to worker()/worker_progress_cb() above, following
        this file's own _poll_poi_category_queue() pattern exactly
        (drain the queue, react to each message, reschedule unless a
        terminal message was seen). This is the ONLY place any Tkinter
        call happens for the duration of a run: create_progress_
        window(), update_progress(), close_progress_window(), and the
        final result messagebox are all called from here, never from
        worker().

        Guards on app_root.winfo_exists() first, matching _poll_poi_
        category_queue()'s own guard on win -- needed here for a new
        reason as of Part 3: since the main thread's event loop now
        stays live and responsive for the whole duration of a run
        (rather than being blocked inside a synchronous run_
        processing() call, as before), it's newly possible for the
        rest of the app to be closed while a run is still in flight.
        If that happens, there is no live root left to open a progress
        window or a dialog on, so this just stops polling silently --
        the same safe behavior _poll_poi_category_queue() already
        falls back to in the equivalent situation.

        Drains every message currently queued per tick (not just one),
        since a single 100ms tick can easily contain several
        "progress" messages for a fast-processing parcel batch --
        the queue.Queue() FIFO ordering guarantees they're always
        handled in the exact order worker() produced them.
        """
        if not app_root.winfo_exists():
            return
        try:
            while True:
                msg = result_queue.get_nowait()
                tag = msg[0]
                if tag == "new_source":
                    _, total = msg
                    create_progress_window(app_root, total)
                elif tag == "progress":
                    _, current, total, pmsg = msg
                    update_progress(current, total, pmsg)
                elif tag == "source_done":
                    close_progress_window()
                elif tag == "aborted_silently":
                    # Preserves the exact pre-Part-3 behavior: a
                    # load_db_credentials() failure closes the progress
                    # window (a no-op here, since none was ever opened --
                    # this failure happens before the first "new_source")
                    # but shows NO dialog at all, success or error.
                    close_progress_window()
                    return
                elif tag == "finished":
                    _, error_message = msg
                    close_progress_window()
                    if error_message:
                        messagebox.showerror("Error", error_message)
                    else:
                        messagebox.showinfo("Success", "Processing complete!")
                    return
        except queue.Empty:
            pass
        app_root.after(100, _poll_run_processing_queue)

    threading.Thread(target=worker, daemon=True).start()
    app_root.after(100, _poll_run_processing_queue)


# ========================================
# MAIN / ENTRYPOINT
# ========================================
def main(parent=None):
    """
    Tool entry point. If parent is given (invoked from within another
    running Tk app), reuses it as APP_ROOT and just opens this tool's
    window. Otherwise creates and hides a new Tk root, sets it as
    APP_ROOT, and enters its own mainloop -- the standalone-subprocess
    dispatch path.

    Args:
        parent: an existing Tk root to reuse, or None to create one.
    """
    global APP_ROOT
    if parent is not None:
        APP_ROOT = parent
        open_main_window(parent)
    else:
        root = tk.Tk()
        APP_ROOT = root
        root.withdraw()
        open_main_window(root)
        root.mainloop()


if __name__ == "__main__":
    main()