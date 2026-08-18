"""
tools/influence_to_barangay.py

PURPOSE:
    CAMA Tools tool ("ANY MAP TO LAND PARCEL" in MAIN.py's dispatch
    table): for each selected Land Parcel/Barangay source, transfers one
    attribute value per selected Influence Map (thematic) layer onto the
    parcel, via a spatial join of the parcel's centroid against the
    influence layer (e.g. a flood-hazard or landslide-risk map). Each
    influence layer contributes exactly one CAMA_-prefixed output
    column, named after whichever attribute detect_attr_name() finds on
    that layer.

DISPATCH:
    Run as an isolated subprocess by MAIN.py via its `--tool` dispatch
    mechanism (see system context). Entry point is main(), triggered via
    the `if __name__ == "__main__":` guard at the bottom of this file.

INPUTS:
    Land Parcel/Barangay source: one or more local vector files (.shp,
    .gpkg, or any file type via VECTOR_FILETYPES) or PostGIS tables.
    Influence Map source: one or more local vector files or PostGIS
    tables, each contributing one detected attribute (see
    detect_attr_name()).
    pg_credentials.json (via load_db_credentials(), from
    utils/db_discovery.py) for any DB source or DB output.

OUTPUTS:
    Local output mode: writes one atomically-written .gpkg per Land
    Parcel/Barangay source (_write_gpkg()), then attempts to open it in
    Global Mapper (load_in_global_mapper()).
    DB output mode: writes/replaces one PostGIS table per source,
    resolved via resolve_db_output_table(), plus an entry in the shared
    CAMA_Transaction_Log table recording the tool name and fields
    written. A separate, intentionally-disabled CAMA_Table write also
    exists (commented out, not removed) -- see the large comment block
    around it inside run_processing()'s worker() for the full record of
    why it's disabled and why it's being kept, not deleted.

DEPENDENCIES:
    stdlib: os, re, json, argparse (imported but currently unused --
    see SIDE EFFECTS/report-only note below), subprocess, threading,
    queue, ctypes, sys, tkinter.
    third-party: geopandas, psycopg2, sqlalchemy, shapely.
    local: utils.table_name_matching, utils.resource_path,
    utils.db_discovery, utils.window_icon, tools.progress_framework
    (imported mid-file, directly above the class/function that uses it
    -- see the Progress Event Protocol v9 comment block further below).

SIDE EFFECTS:
    File reads/writes (.shp/.gpkg). PostGIS reads/writes, including a
    write to the shared CAMA_Transaction_Log table on every DB-output
    run. A live PostgreSQL connection. Tkinter GUI windows throughout,
    including a background thread + queue.Queue-based polling loop for
    the main processing run. A subprocess launch to Global Mapper
    (load_in_global_mapper()) on local-output saves, plus a Win32
    EnumWindows call to find/focus an already-open Global Mapper window
    first.

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
from __future__ import annotations
import os
import re
import json
import argparse
import subprocess
import threading
import queue
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox

import geopandas as gpd
import psycopg2
from sqlalchemy import create_engine, text
from shapely.geometry import Point

from utils.table_name_matching import normalize_name, find_matching_tables
from utils.resource_path import resource_path
from utils.db_discovery import load_db_credentials, fetch_tables
from utils.window_icon import apply_icon

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

# Supported vector file extensions
VECTOR_FILETYPES = [
    ("Vector files", "*.shp *.gpkg"),
    ("Shapefiles", "*.shp"),
    ("GeoPackage", "*.gpkg"),
    ("All files", "*.*"),
]

# ========================================
# RUNTIME STATE
# ========================================
barangay_source = None
influence_source = None
output_mode = None


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


# ========================================
# FILE READING
# ========================================
def read_vector_file(path: str) -> gpd.GeoDataFrame:
    """
    Read a vector file (SHP or GPKG) into a GeoDataFrame.
    For GPKG files that contain multiple layers, the first layer is used
    unless the filename stem matches a layer name exactly.
    """
    ext = os.path.splitext(path)[1].lower()

    if ext == ".gpkg":
        import fiona
        layers = fiona.listlayers(path)
        if not layers:
            raise ValueError(f"No layers found in GeoPackage: {path}")

        # Try to match layer name to file stem first
        stem = os.path.splitext(os.path.basename(path))[0].lower()
        matched_layer = next(
            (l for l in layers if l.lower() == stem), layers[0]
        )

        if len(layers) > 1:
            print(f"ℹ️  GPKG has {len(layers)} layers: {layers}. Using: '{matched_layer}'")

        return gpd.read_file(path, layer=matched_layer)

    # Default: let geopandas auto-detect (handles .shp and others)
    return gpd.read_file(path)


def get_local_name(path: str) -> str:
    """
    Extract a clean layer/table name from a file path.
    For GPKG, tries to use the matched layer name for consistency.
    """
    ext = os.path.splitext(path)[1].lower()
    stem = os.path.splitext(os.path.basename(path))[0]

    if ext == ".gpkg":
        try:
            import fiona
            layers = fiona.listlayers(path)
            if layers:
                matched = next(
                    (l for l in layers if l.lower() == stem.lower()), layers[0]
                )
                return matched
        except Exception:
            pass

    return stem


# ========================================
# GEOMETRY / ATTRIBUTES
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


def detect_attr_name(gdf, name_guess: str):
    """
    Detect attribute column based on layer/table name.

    Example:
      FloodHazardMap  -> finds column containing 'flood'
      Landslide_Risk  -> finds column containing 'landslide'
    """

    norm_layer = normalize_name(name_guess)

    # 1️⃣ PRIMARY RULE: substring match using normalized names
    for col in gdf.columns:
        if col.lower() in ("geometry", "geom"):
            continue
        if normalize_name(col) in norm_layer or norm_layer in normalize_name(col):
            return col

    # 2️⃣ Exact name match (legacy behavior)
    for col in gdf.columns:
        if col.upper() == name_guess.upper():
            return col

    # 3️⃣ Elevation fallback
    for col in gdf.columns:
        if "ELEVATION" in col.upper():
            return col

    # 4️⃣ Last fallback: first non-geometry column
    non_geom_cols = [c for c in gdf.columns if c.lower() not in ("geometry", "geom")]
    if non_geom_cols:
        return non_geom_cols[0]

    raise ValueError(f"No suitable attribute column found for {name_guess}")


# parcel_output_column_overrides: {path_or_table: {"CAMA_<attr_name>":
# name, ...}} -- for any Land Parcel/Barangay source (Local file OR
# Database table) where one or more pre-existing CAMA_-prefixed output
# columns were detected (see _check_parcel_influence_conflicts() below)
# and the user confirmed proceeding at Run time. Read by
# run_processing() and resolved into transfer_attributes()'s
# output_column_map, so the tool writes back into the EXACT existing
# column (preserving original casing) instead of always writing a
# hardcoded "CAMA_*" name. A source with no entry here (or an
# attr_name missing from its entry) uses the default CAMA_-prefixed
# name.
parcel_output_column_overrides = {}


# ========================================
# OUTPUT-COLUMN CONFLICT DETECTION
# ========================================
# This tool's output columns are dynamic, not a fixed list -- each
# selected Influence Map source contributes ONE column, named after
# whatever attribute detect_attr_name() finds on that specific layer
# (e.g. a "FloodHazardMap" layer might contribute a column detected as
# "FloodLevel" -> CAMA_FloodLevel). Unlike road_frontage.py/terrain.py/
# land_shape_compactness.py's fixed OUTPUT_COLUMN_TARGETS tuples, this
# tool's target list is built per-run from whichever Influence Map
# source(s) are actually selected -- see _get_added_fields_for_check()
# below.
def _get_added_fields_for_check(influence_source, engine, schema):
    """
    Lightweight, standalone read of the selected Influence Map
    source(s), used ONLY by the Run-time column-conflict pre-check in
    on_run() -- separate from, and NOT a substitute for, the "real"
    influence-layer read run_processing() performs later. Mirrors the
    exact same attr_name detection logic run_processing() uses
    (read_vector_file()/read_postgis + ensure_geometry_column() +
    detect_attr_name()) so the target list built here matches what
    run_processing() will actually use.

    Returns a list of attr_name strings (one per Influence Map source),
    or an empty list if the read fails for any reason -- a failure here
    is NEVER treated as a column-conflict failure; it just means the
    conflict check is skipped entirely for this Run (logged to
    console). The real read inside run_processing() remains solely
    responsible for surfacing any genuine read error to the user.
    """
    added_fields = []
    try:
        if influence_source[0] == "local":
            for path in influence_source[1]:
                gdf = read_vector_file(path).to_crs(epsg=3857)
                gdf = ensure_geometry_column(gdf)
                name_guess = get_local_name(path)
                added_fields.append(detect_attr_name(gdf, name_guess))
        else:
            for table in influence_source[1]:
                geom_col = get_geom_column(engine, schema, table)
                gdf = gpd.read_postgis(
                    f'SELECT * FROM "{schema}"."{table}"', engine, geom_col=geom_col
                ).to_crs(epsg=3857)
                gdf = ensure_geometry_column(gdf)
                added_fields.append(detect_attr_name(gdf, table))
    except Exception as e:
        print(f"⚠️ Could not read Influence Map source to check for "
              f"existing output column(s): {e}")
        return []
    return added_fields


def _check_parcel_influence_conflicts(sources, source_type, targets):
    """
    Checks the selected Land Parcel/Barangay source -- Local file OR
    Database table (extended to cover both as part of Fix 3; previously
    LOCAL-only) -- for pre-existing columns matching any of `targets`
    (case-insensitive exact match). Local sources use read_vector_file()
    -- this tool's own canonical reader, handling multi-layer GPKGs the
    same way run_processing() does. Database sources use this tool's
    own get_geom_column() + gpd.read_postgis() pattern (NOT
    read_postgis_clean(), which this file does not use anywhere else),
    loading its own creds/schema/engine, self-contained. Read failure
    = skip-only, never a conflict-check failure.

    Returns a list of (path_or_table, existing_output_cols) tuples --
    one entry only for sources where at least one target match was
    found. existing_output_cols is {target_name: actual_existing_
    column_name}, original casing preserved -- shown in the
    confirmation dialog and used verbatim as the write-back column
    (canonical road_width.py pattern: exact detected casing, per
    source, no coalescing needed here since -- unlike
    POI_All_Distance.py -- this tool saves one output per source,
    never merges multiple sources together).
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
                gdf = read_vector_file(path_or_table)
            else:
                geom_col = get_geom_column(engine, schema, path_or_table)
                gdf = gpd.read_postgis(
                    f'SELECT * FROM "{schema}"."{path_or_table}"', engine, geom_col=geom_col
                )
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


# ========================================
# CORE COMPUTATION
# ========================================
def transfer_attributes(barangay_gdf, influence_gdfs, output_column_map=None, progress=None):
    """
    output_column_map : optional {attr_name: output_col_name} -- for
        each (infl_gdf, attr_name) pair, the joined value is written
        into output_column_map.get(attr_name, f"CAMA_{attr_name}")
        instead of the bare attr_name directly. Defaults to the
        standard CAMA_-prefixed name (this tool's normal output,
        matching road_width.py's own ROAD_WIDTH -> CAMA_ROAD_WIDTH
        convention). The GUI overrides this per barangay/parcel source
        when that LOCAL source already has an existing matching column
        (see _check_parcel_influence_conflicts() below) -- the exact
        existing name/casing is passed here so processing writes back
        into that same column instead of creating a hardcoded
        CAMA_-prefixed duplicate.

        NOTE: this only affects the column name written into
        barangay_gdf (the tool's own local/DB output). It does NOT
        affect CAMA_Table -- that shared, cross-tool table's own
        column names/schema are explicitly out of scope for this
        change (see the CAMA_Table section of run_processing() below,
        which reads from the resolved column name here but writes
        into CAMA_Table under the same unprefixed name it always has).

    progress : optional callable progress(message, value=None, maximum=None),
    called once per influence layer (never per parcel -- each layer's
    spatial join below is a single vectorized gpd.sjoin() call with no
    per-row visibility to report progress against). Optional and
    defaults to None so this function's existing signature is unchanged
    for any call site that doesn't pass it -- added as part of this
    tool's Progress Event Protocol v9 migration (see run_processing()
    below).
    """
    output_column_map = output_column_map or {}
    total = len(influence_gdfs)
    for i, (infl_gdf, attr_name) in enumerate(influence_gdfs, start=1):
        if progress:
            progress(f"Transferring attribute {i}/{total}: {attr_name}", i, total)
        infl_clean = infl_gdf[[attr_name, "geometry"]].copy()
        infl_clean = infl_clean.rename(columns={attr_name: "joined_attr"})

        centroids = barangay_gdf.geometry.centroid
        centroid_gdf = gpd.GeoDataFrame(geometry=centroids, crs=barangay_gdf.crs)

        joined = gpd.sjoin(centroid_gdf, infl_clean, how="left", predicate="within")
        joined = joined.loc[:, ~joined.columns.duplicated(keep="first")]

        out_col = output_column_map.get(attr_name, f"CAMA_{attr_name}")
        barangay_gdf[out_col] = joined["joined_attr"].reset_index(drop=True)
    return barangay_gdf


# ========================================
# DB OUTPUT RESOLUTION
# ========================================
def resolve_db_output_table(root, schema, barangay_source):
    """
    Determines the DB-output destination table for the Land Parcel
    source, BEFORE any processing or writing starts -- same "resolve
    everything up front" philosophy as ask_overwrite_dialog() (see
    run_processing()). This tool has no background worker thread --
    this function is still called once, up front, for separation of
    responsibilities: this function owns ALL user interaction and
    overwrite decisions, so the processing/write logic further below
    never has to ask any UI or overwrite question of its own.

    Two cases:
      - DB-source Land Parcel (barangay_source[0] == "db"): always
        writes back to the exact same table it was read from -- no
        matching, no dialog, matches run_processing()'s own pre-
        existing "DB → DB: replace the SAME table" branch.
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


# ============================================================
# Progress Event Protocol v9 -- this tool's migration.
# ============================================================
# This tool had NO background worker thread and NO progress dialog at
# all -- run_processing() ran entirely synchronously on the main
# thread. Same shape of migration as land_shape_compactness.py's,
# road_surface.py's, and road_density.py's: reuses
# progress_framework.py's PresentationState/ProgressPresentationPolicy/
# TkinterProgressView directly -- no tool-local copies, no new
# abstraction.
#
# The existing unified per-source loop (local/db reading merged into
# one loop, per explicit instruction) is preserved exactly as-is --
# not split into two separate loops like the other migrated tools.
#
# Deliberately NOT done in this task:
#   - No per-source failure isolation added.
#   - The 3 overwrite dialogs in this file are untouched.
# ============================================================
from tools.progress_framework import (
    PresentationState,
    ProgressPresentationPolicy,
    TkinterProgressView,
)


class ProgressWindow:
    """
    Progress dialog shown while run_processing() works on a background
    thread. Same shape as the other migrated tools' ProgressWindow --
    status label + determinate progress bar, no cancel/stop_flag
    support. Progress Event Protocol v9 role: ProgressWindow is the
    host, not the decision-maker (see ProgressPresentationPolicy /
    TkinterProgressView, imported from progress_framework.py, shared
    with lot_location.py/road_frontage.py/land_shape_compactness.py/
    road_surface.py/road_density.py).
    """
    def __init__(self, root, title="Processing"):
        """
        Creates and immediately shows the progress dialog.

        Args:
            root: the parent Tk/Toplevel window.
            title (str): window title. Defaults to "Processing".
        """
        from tkinter import ttk
        self.win = tk.Toplevel(root)
        apply_icon(self.win, "influencemap.ico")
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

        self._policy = ProgressPresentationPolicy()
        self._view = TkinterProgressView(self.win, self.status_var, self.progress)

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
# PROCESSING
# ========================================
def run_processing(root, overwrite_mode=None, resolved_table_name=None, resolved_outcome=None):
    """
    Orchestrates the full run on a background thread (worker(), started
    at the bottom of this function) with progress reported via a
    queue.Queue polled by poll_queue() on the main thread: loads the
    selected Influence Map layer(s), then for each selected Land
    Parcel/Barangay source, runs transfer_attributes() and saves the
    result either locally (.gpkg, optionally opened in Global Mapper)
    or to PostGIS -- the DB-output path also writes an entry into the
    shared CAMA_Transaction_Log table (see the disabled CAMA_Table
    block's own comment for why a second, related write is disabled).

    Args:
        root: the live top-level window, used as the parent for any
        dialogs created here (currently none directly -- resolution
        already happened in on_run() before this was called).
        overwrite_mode (str | None): "overwrite" or "new", from
        ask_overwrite_dialog() in on_run() -- only relevant for local
        output mode.
        resolved_table_name (str | None): the already-confirmed DB
        output table name from resolve_db_output_table() in on_run() --
        only relevant for DB output mode.
        resolved_outcome (str | None): "created" or "overwritten" from
        resolve_db_output_table() -- recorded into the
        CAMA_Transaction_Log entry as part of table_action.
    """
    # root: the live top-level window (passed from on_run(); NOT
    # `win`, which is destroyed before run_processing() is ever
    # called -- see on_run()'s win.destroy() immediately before this
    # function's call site). Used as the parent for any dialogs
    # created in this function (currently just
    # resolve_db_output_table()'s DB confirmation dialogs).
    global barangay_source, influence_source, output_mode

    # 🧠 Debug info (helps verify what's actually set)
    print("=== PROCESSING START ===")
    print("Barangay Source:", barangay_source)
    print("Influence Source:", influence_source)
    print("Output Mode:", output_mode)
    print("=========================")

    # ✅ safer validation
    if not barangay_source or not isinstance(barangay_source, tuple) or not barangay_source[1]:
        messagebox.showerror("Error", "Barangay source not selected properly.")
        return
    if not influence_source or not isinstance(influence_source, tuple) or not influence_source[1]:
        messagebox.showerror("Error", "Influence map source not selected properly.")
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

    # resolved_table_name, resolved_outcome: the DB-output destination
    # table + outcome. Resolution responsibility now belongs to on_run()
    # (PRIORITY 3), on the main thread, BEFORE win.destroy() -- see
    # Fix 1. By the time they reach this function they are treated as
    # already-validated values: either None/None (local output, or
    # output_mode[0] != "db") or a confirmed table name + outcome (DB
    # output, user already had the chance to cancel in on_run()). No
    # re-resolution or re-validation happens here. resolved_outcome
    # specifically still matters downstream -- see table_action and the
    # CAMA_Transaction_Log INSERT further below.

    # ============================================================
    # Progress Event Protocol v9 -- this tool's migration.
    # ============================================================
    # Everything ABOVE this point (validation, credential loading,
    # resolve_db_output_table() + its confirmation dialog(s)) is
    # unchanged and stays on the main thread, exactly as before.
    #
    # Everything below is the exact same logic this function always
    # had: influence-layer loading, then the single UNIFIED per-source
    # loop (deliberately left unified, not split into separate
    # local/db branches like the other tools -- this tool's original
    # structure already merges local/db reading into one loop with an
    # if/else inside it, and a separate if/else for the write step;
    # that shape is preserved exactly, not restructured), including the
    # full CAMA_Table/CAMA_Transaction_Log transaction -- now wrapped
    # inside a background worker() thread instead of running inline on
    # the main thread.
    progress = ProgressWindow(root, "Influence to Barangay Progress")
    q = queue.Queue()

    def worker():
        """
        Background-thread body: loads the selected Influence Map
        layer(s), then for each selected Land Parcel/Barangay source
        runs transfer_attributes() and saves the result (local .gpkg or
        PostGIS + CAMA_Transaction_Log entry), posting progress/
        completion/error events onto q for poll_queue() to consume on
        the main thread. Never touches Tkinter widgets directly (all
        UI updates happen via progress_cb -> q, consumed by
        poll_queue()).
        """
        try:
            def progress_cb(msg, val=None, maxv=None):
                q.put(("update", msg, val, maxv))

            influence_gdfs = []
            added_fields = []

            # --- Load influence layers ---
            total_influence = len(influence_source[1])
            if influence_source[0] == "local":
                for i, path in enumerate(influence_source[1], start=1):
                    progress_cb(f"Loading influence layer {i}/{total_influence}", i, total_influence)
                    gdf = read_vector_file(path).to_crs(epsg=3857)
                    gdf = ensure_geometry_column(gdf)
                    name_guess = get_local_name(path)
                    attr_name = detect_attr_name(gdf, name_guess)
                    influence_gdfs.append((gdf, attr_name))
                    added_fields.append(attr_name)
            else:
                for i, table in enumerate(influence_source[1], start=1):
                    progress_cb(f"Loading influence layer {i}/{total_influence}", i, total_influence)
                    geom_col = get_geom_column(engine, schema, table)
                    gdf = gpd.read_postgis(
                        f'SELECT * FROM "{schema}"."{table}"', engine, geom_col=geom_col
                    ).to_crs(epsg=3857)
                    gdf = ensure_geometry_column(gdf)
                    attr_name = detect_attr_name(gdf, table)
                    influence_gdfs.append((gdf, attr_name))
                    added_fields.append(attr_name)

            # --- Process Barangay ---
            sources = barangay_source[1]
            for src in sources:
                if barangay_source[0] == "local":
                    local_name = get_local_name(src)
                    progress_cb(f"Loading {local_name}", None, None)
                    b_gdf_raw = read_vector_file(src)
                else:
                    local_name = src
                    progress_cb(f"Loading DB table {local_name}", None, None)
                    geom_col = get_geom_column(engine, schema, src)
                    b_gdf_raw = gpd.read_postgis(
                        f'SELECT * FROM "{schema}"."{src}"', engine, geom_col=geom_col
                    )

                # Preserve the parcel layer's original CRS so the final output
                # can be reprojected back to it before saving. 3857 (below) is
                # only the working CRS used for the spatial join against the
                # influence/thematic layers -- not the intended CRS of the
                # saved output. Captured now, before b_gdf gets reprojected.
                original_crs = b_gdf_raw.crs
                b_gdf = b_gdf_raw.to_crs(epsg=3857)

                b_gdf = ensure_geometry_column(b_gdf)

                # output_column_map: preserves this source's existing output
                # column name(s)/casing exactly, if a conflict was detected and
                # confirmed in on_run() -- e.g. a detected "caMA_FloodLevel" is
                # written back to "caMA_FloodLevel", not a hardcoded
                # "CAMA_FloodLevel". Defaults to the standard CAMA_-prefixed
                # name for any attr_name this source has no override for.
                # Extended (Fix 3) to also apply for Database-sourced parcels
                # -- previously this always fell back to {} for a DB-sourced
                # parcel via an explicit "if local else {}" gate, even though
                # parcel_output_column_overrides itself is now correctly
                # populated for DB sources too (see on_run()'s PRIORITY 1).
                src_col_overrides = parcel_output_column_overrides.get(src, {})
                output_column_map = {
                    attr_name: src_col_overrides.get(f"CAMA_{attr_name}", f"CAMA_{attr_name}")
                    for attr_name in added_fields
                }
                b_gdf = transfer_attributes(
                    b_gdf, influence_gdfs,
                    output_column_map=output_column_map,
                    progress=progress_cb,
                )

                # --- Save outputs ---
                if output_mode[0] == "local":
                    out_dir = output_mode[1]
                    desired_base_name = local_name
                    candidate_path = os.path.join(out_dir, f"{desired_base_name}.gpkg")
                    had_conflict = os.path.exists(candidate_path)
                    if had_conflict and overwrite_mode == "new":
                        base_name = resolve_output_base_name(out_dir, desired_base_name)
                    else:
                        base_name = desired_base_name
                    out_path = os.path.join(out_dir, f"{base_name}.gpkg")

                    # 1️⃣ Ensure CRS exists
                    if b_gdf.crs is None:
                        raise RuntimeError("❌ Cannot write file: CRS is None")

                    # 2️⃣ Restore the parcel layer's original CRS (captured
                    # above, before the 3857 working-CRS reprojection). Falls
                    # back to WGS84 only if the source itself had no CRS to
                    # begin with -- there's nothing to "restore" in that case.
                    if original_crs is not None:
                        b_gdf = b_gdf.to_crs(original_crs)
                    else:
                        b_gdf = b_gdf.to_crs(epsg=4326)
                    print("🧭 CRS before save:", b_gdf.crs)

                    # 3️⃣ Geometry validity check (measurement/output note)
                    #
                    # Deliberately NOT writing a buffer(0) repair back into
                    # b_gdf["geometry"] here. This tool's only geometry-dependent
                    # computation, the centroid-based sjoin() inside
                    # transfer_attributes() (see that function), already ran earlier
                    # above (before this point) using the original, unrepaired
                    # geometry -- this validity check runs strictly after that, with
                    # no measurement step left downstream of it. This matches the
                    # documented convention in influence_to_map.py ("Deliberately NOT
                    # applying any geometry repair (e.g. buffer(0)) to parcel_gdf_out
                    # here. This tool only MEASURES -- it must never alter a parcel's
                    # digitized shape, even if that shape happens to be technically
                    # invalid."), road_width.py, land_shape_compactness.py, and
                    # lot_location.py -- the exported output keeps each parcel's
                    # original, untouched shape, even if invalid.
                    #
                    # Previously (pre-fix) this block ran
                    # b_gdf["geometry"] = b_gdf.geometry.buffer(0) here, which DID
                    # silently alter the saved output geometry -- flagged and
                    # confirmed as an inconsistency against the dominant convention
                    # above (see influence_to_map.py's own inline NOTE, written at
                    # the time this was first discovered), corrected here.
                    if not b_gdf.is_valid.all():
                        print("⚠️ Invalid geometries detected -- kept as-is in output (not repaired), per project convention")

                    # 4️⃣ Write GeoPackage
                    _write_gpkg(b_gdf, out_path)

                    print(f"✅ Saved: {out_path}")
                    q.put(("open_gm", out_path, None, None))

                else:
                    # The actual destination table was already decided by
                    # resolve_db_output_table(), BEFORE this loop even
                    # started -- fuzzy matching + user confirmation already
                    # happened there (see that function's docstring). This
                    # just uses the result. Falls back to the old
                    # filename-lowercased behavior only if resolved_table_name
                    # is somehow None here (output_mode[0] != "db" can't reach
                    # this branch, so this is just a defensive fallback).
                    target_table = resolved_table_name if resolved_table_name is not None else local_name.lower()
                    table_action = resolved_outcome if resolved_outcome is not None else "new"

                    print(f"🗂️ Saving to DB: {target_table} ({table_action})")

                    # Same restoration as the local-file save path above --
                    # b_gdf is still in the 3857 working CRS at this point.
                    if original_crs is not None:
                        b_gdf = b_gdf.to_crs(original_crs)
                    else:
                        b_gdf = b_gdf.to_crs(epsg=4326)

                    # --------------- 🟢 Main table + CAMA Table and Log --------------- #
                    # The main table write and the CAMA_Table/log updates below
                    # now share ONE transaction -- previously the main table
                    # write used a bare `engine` (auto-committing on its own,
                    # outside any transaction), while only the CAMA_Table
                    # portion had real engine.begin() atomicity. That meant a
                    # CAMA_Table failure could leave the main table already
                    # committed with no rollback. Merging them closes that gap:
                    # if ANY part fails -- the main table write, CAMA_Table, or
                    # CAMA_Transaction_Log -- everything rolls back together as
                    # one unit.
                    with engine.begin() as conn:
                        b_gdf.to_postgis(
                            target_table,
                            conn,
                            schema=schema,
                            if_exists="replace",
                            index=False
                        )

                        # ------------------------------------------------------------------
                        # CAMA_Table write -- DISABLED (commented out, not removed).
                        #
                        # Confirmed (developer sign-off, August 2026) that no application --
                        # including BLGF-Web-App, iGeosys-LGU-Suite, or any other known
                        # system -- currently reads from CAMA_Table in the PostGIS database.
                        # This is NOT a statement that the implementation below is obsolete,
                        # broken, or wrong -- it is intentionally left fully intact so it can
                        # be re-enabled later with no rework if a consumer for CAMA_Table
                        # appears (e.g. a future reporting/dashboard need).
                        #
                        # Same convention already used for this exact table in
                        # influence_to_map.py (its own CAMA_Table block, disabled for a
                        # different reason -- see that file's comment) -- disabled here
                        # independently, matching the same comment-out-not-delete style.
                        #
                        # Untouched by this change: the b_gdf.to_postgis() main table write
                        # above, and the CAMA_Transaction_Log block below -- both stay inside
                        # the same `with engine.begin() as conn:` transaction as before.
                        # ------------------------------------------------------------------
                        # # Ensure CAMA_Table exists
                        # conn.execute(
                            # text(
                                # f"""
                            # CREATE TABLE IF NOT EXISTS "{schema}"."CAMA_Table" (
                                # id SERIAL PRIMARY KEY,
                                # PIN TEXT UNIQUE NOT NULL
                            # );
                        # """
                            # )
                        # )
                        #
                        # # Add missing columns as NUMERIC
                        # for col in added_fields:
                            # conn.execute(
                                # text(
                                    # f"""
                                # DO $$
                                # BEGIN
                                    # IF NOT EXISTS (
                                        # SELECT 1 FROM information_schema.columns
                                        # WHERE table_schema='{schema}'
                                          # AND table_name='CAMA_Table'
                                          # AND column_name='{col.lower()}'
                                    # ) THEN
                                        # EXECUTE 'ALTER TABLE "{schema}"."CAMA_Table" ADD COLUMN "{col.lower()}" NUMERIC';
                                    # END IF;
                                # END $$;
                            # """
                                # )
                            # )
                        #
                        # # Insert or update PIN-based values using named parameters
                        # pin_field = next((c for c in b_gdf.columns if c.lower() == "pin"), None)
                        # if pin_field:
                            # # Instrumentation only (per explicit instruction):
                            # # total/enumerate(..., start=1) added purely to
                            # # report progress -- the SQL logic and transaction
                            # # flow inside this loop are completely unchanged.
                            # # This loop executes one SQL statement per parcel
                            # # row and can become the longest-running part of
                            # # the operation, so it gets its own progress
                            # # messages rather than leaving the dialog looking
                            # # stalled for its whole duration.
                            # total_rows = len(b_gdf)
                            # for row_i, (_, row) in enumerate(b_gdf.iterrows(), start=1):
                                # progress_cb(f"Updating CAMA_Table: {row_i}/{total_rows}", row_i, total_rows)
                                # insert_cols = ["PIN"] + [c.lower() for c in added_fields]
                                # insert_placeholders = [f":{c.lower()}" for c in insert_cols]
                                # update_assignments = [f'"{c.lower()}" = :{c.lower()}_upd' for c in added_fields]
                        #
                                # sql = f"""
                                # INSERT INTO "{schema}"."CAMA_Table" ({', '.join(insert_cols)})
                                # VALUES ({', '.join(insert_placeholders)})
                                # ON CONFLICT (PIN) DO UPDATE
                                # SET {', '.join(update_assignments)};
                                # """
                        #
                                # params = {}
                                # params["pin"] = str(row[pin_field])
                        #
                                # for c in added_fields:
                                    # # Source-side lookup only -- CAMA_Table's
                                    # # OWN column names (c.lower(), used for
                                    # # insert_cols/update_assignments/params keys
                                    # # above and below) are UNCHANGED by this
                                    # # fix. This only changes WHERE the value is
                                    # # read FROM in b_gdf: prefer the new
                                    # # CAMA_-prefixed column (the one
                                    # # transfer_attributes() actually wrote this
                                    # # run -- output_column_map already resolved
                                    # # any per-source override casing), falling
                                    # # back to the legacy unprefixed column name
                                    # # if the new one somehow isn't present (e.g.
                                    # # a barangay/parcel source that still has an
                                    # # old, pre-CAMA_-prefix column from before
                                    # # this change, and for whatever reason the
                                    # # new column wasn't created this run).
                                    # # Without this fallback-aware lookup, every
                                    # # CAMA_Table value would silently become
                                    # # NULL after the CAMA_ prefix rollout, since
                                    # # b_gdf no longer has a column literally
                                    # # named `c`.
                                    # resolved_col = output_column_map.get(c, f"CAMA_{c}")
                                    # if resolved_col in row:
                                        # source_val = row[resolved_col]
                                    # elif c in row:
                                        # source_val = row[c]
                                    # else:
                                        # source_val = None
                        #
                                    # if source_val is not None:
                                        # try:
                                            # params[c.lower()] = float(source_val)
                                            # params[f"{c.lower()}_upd"] = float(source_val)
                                        # except (ValueError, TypeError):
                                            # params[c.lower()] = None
                                            # params[f"{c.lower()}_upd"] = None
                                    # else:
                                        # params[c.lower()] = None
                                        # params[f"{c.lower()}_upd"] = None
                        #
                                # conn.execute(text(sql), params)

                        # Ensure CAMA_Transaction_Log exists
                        conn.execute(
                            text(
                                f"""
                            CREATE TABLE IF NOT EXISTS "{schema}"."CAMA_Transaction_Log" (
                                id SERIAL PRIMARY KEY,
                                table_name TEXT,
                                cama_tool TEXT,
                                cama_fields TEXT,
                                transaction_date_time TIMESTAMP DEFAULT NOW()
                            );
                        """
                            )
                        )

                        # Log transaction
                        conn.execute(
                            text(
                                f"""
                            INSERT INTO "{schema}"."CAMA_Transaction_Log" 
                            (table_name, cama_tool, cama_fields)
                            VALUES (:tbl, :tool, :details);
                        """
                            ),
                            {
                                "tbl": f"{target_table} ({table_action})",
                                "tool": "influence_to_barangay",
                                "details": ", ".join(added_fields),
                            },
                        )

            q.put(("done", "✅ Processing done with CAMA logs!", None, None))

        except Exception as e:
            # New: this function had no top-level try/except before --
            # an uncaught exception here previously propagated silently
            # (no graceful dialog). Required by moving to a background
            # thread: an exception on a non-main thread that nobody
            # catches is otherwise simply lost.
            q.put(("error", str(e), None, None))

    def poll_queue():
        """
        Main-thread poller (scheduled via root.after(100, ...)): drains
        q and updates the progress dialog, opens the result in Global
        Mapper, or shows the final success/error dialog and stops
        polling, depending on the event kind. All Tkinter calls happen
        here, never inside worker() itself.
        """
        if not root.winfo_exists():
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
        root.after(100, poll_queue)

    threading.Thread(target=worker, daemon=True).start()
    poll_queue()



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
    apply_icon(dialog, "influencemap.ico")
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
    apply_icon(dialog, "influencemap.ico")
    dialog.title("INFLUENCE TO BARANGAY TOOL")
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
    apply_icon(dialog, "influencemap.ico")
    dialog.title("INFLUENCE TO BARANGAY TOOL")
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
    from tkinter import ttk
    picker = tk.Toplevel(parent)
    apply_icon(picker, "influencemap.ico")
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
    Land Parcel/Barangay and Influence Map source pickers (each with a
    Local-file/Database-table radio toggle), an Output destination
    picker, and a Run button gated by _update_run_button_state().

    Args:
        root: the parent Tk root this window is opened under.
    """
    from tkinter import ttk

    win = tk.Toplevel(root)
    apply_icon(win, "influencemap.ico")
    win.title("Influence to Parcel Tool")
    win.resizable(False, False)
    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))

    # ── state ────────────────────────────────────────────────────
    parcel_source_type    = tk.StringVar(master=win, value="local")
    influence_source_type = tk.StringVar(master=win, value="local")
    output_dest_type      = tk.StringVar(master=win, value="local")

    # Single-selection architecture: one local file and one DB table
    # may exist in memory at any time. Authority variables -- all GUI
    # labels and run-button state are derived from them, never the reverse.
    # NOTE: The Influence Source subsystem intentionally remains
    # multi-selection -- only the Land Parcel Source subsystem is
    # converted here.
    parcel_local_path = None   # authority: single local file path
    parcel_db_table   = None   # authority: single DB table name
    influence_local_paths = []
    influence_db_tables   = []
    output_local_dir      = tk.StringVar(master=win)

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
        file = filedialog.askopenfilename(
            title="Select Land Parcel file",
            filetypes=VECTOR_FILETYPES)
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
        _update_run_button_state()

    # ── SECTION 2: INFLUENCE MAP ─────────────────────────────────
    section_label(win, "Influence Map Source")

    influence_frame = tk.Frame(win)
    influence_frame.pack(fill="x", padx=18, pady=2)

    infl_radio_row = tk.Frame(influence_frame)
    infl_radio_row.pack(fill="x")
    tk.Radiobutton(infl_radio_row, text="Local File(s)",
                   variable=influence_source_type, value="local",
                   command=lambda: _toggle_influence()).pack(side="left")
    tk.Radiobutton(infl_radio_row, text="Database Table(s)",
                   variable=influence_source_type, value="db",
                   command=lambda: _toggle_influence()).pack(side="left", padx=(12, 0))

    infl_files_var = tk.StringVar(master=win, value="No file(s) selected")
    infl_db_label  = tk.StringVar(master=win, value="No table(s) selected")

    infl_action_row = tk.Frame(influence_frame)
    infl_action_row.pack(fill="x", pady=2)

    infl_lbl = tk.Label(infl_action_row, textvariable=infl_files_var,
                        fg="gray", anchor="w", width=42)
    infl_lbl.pack(side="left")

    infl_btn = tk.Button(infl_action_row, text="Browse…", width=10)
    infl_btn.pack(side="left", **PAD)

    def browse_influence_files():
        files = filedialog.askopenfilenames(
            title="Select Influence Map file(s)",
            filetypes=VECTOR_FILETYPES)
        if files:
            influence_local_paths.clear()
            influence_local_paths.extend(files)
            infl_files_var.set(f"{len(files)} file(s) selected")
            _update_run_button_state()

    def _on_influence_db_selected(sel):
        influence_db_tables.clear()
        influence_db_tables.extend(sel)
        infl_db_label.set(f"{len(sel)} table(s) selected")
        _update_run_button_state()

    def browse_influence_db():
        creds = load_db_credentials()
        if not creds:
            return
        tables = fetch_tables(creds["schema"])
        if not tables:
            messagebox.showwarning("No Tables", "No tables found in the database schema.")
            return
        _pick_db_tables(win, tables, multi=True, on_select=_on_influence_db_selected)

    def _toggle_influence():
        if influence_source_type.get() == "local":
            infl_lbl.config(textvariable=infl_files_var)
            infl_btn.config(text="Browse…", command=browse_influence_files)
        else:
            infl_lbl.config(textvariable=infl_db_label)
            infl_btn.config(text="Select…", command=browse_influence_db)
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
        Run button handler: validates Land Parcel/Barangay + Influence
        Map + Output selections are present, checks for existing
        output-column conflicts (PRIORITY 1), runs the local output-file
        conflict check (PRIORITY 2), and DB-output table resolution
        (PRIORITY 3) -- each able to cancel the whole run -- then
        destroys this window and hands off to run_processing(). Sets
        the module-level barangay_source, influence_source, output_mode,
        and parcel_output_column_overrides globals on success.
        """
        global barangay_source, influence_source, output_mode

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

        # validate influence
        if influence_source_type.get() == "local":
            if not influence_local_paths:
                messagebox.showerror("Missing Input",
                    "Please select at least one Influence Map file.")
                return
            influence_source = ("local", tuple(influence_local_paths))
        else:
            if not influence_db_tables:
                messagebox.showerror("Missing Input",
                    "Please select at least one Influence Map table.")
                return
            influence_source = ("db", influence_db_tables)

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
        # PRIORITY 1: existing OUTPUT-COLUMN conflict warning. This tool's
        # output columns are dynamic (see _get_added_fields_for_check()) --
        # the target list is built from whichever Influence Map source(s)
        # are actually selected this run. Extended (Fix 3) to cover both
        # Local and Database Land Parcel/Barangay sources -- previously
        # LOCAL-only (see _check_parcel_influence_conflicts()'s own
        # docstring). Shown once, combined across every affected source,
        # only here at Run time. Declining cancels the run entirely --
        # nothing is processed, including sources that had no conflict.
        #
        # Unlike POI_All_Distance.py, this tool saves ONE output per
        # source (never merges), so the standard per-source override
        # map applies here -- exact detected casing is preserved and
        # written back into, same canonical road_width.py pattern used
        # by every other per-source tool in this project.
        # ------------------------------------------------------------------
        global parcel_output_column_overrides
        added_fields_for_check = []
        try:
            creds_for_check = load_db_credentials()
            engine_for_check = None
            schema_for_check = None
            if influence_source[0] == "db" and creds_for_check:
                schema_for_check = creds_for_check["schema"]
                engine_for_check = create_engine(
                    f"postgresql://{creds_for_check['username']}:{creds_for_check['password']}@"
                    f"{creds_for_check['host']}:{creds_for_check['port']}/{creds_for_check['database']}"
                )
            added_fields_for_check = _get_added_fields_for_check(
                influence_source, engine_for_check, schema_for_check)
        except Exception as e:
            print(f"⚠️ Could not prepare Influence Map attribute check "
                  f"for column conflicts: {e}")
            added_fields_for_check = []

        if added_fields_for_check:
            targets_for_check = [f"CAMA_{a}" for a in added_fields_for_check]
            conflicts = _check_parcel_influence_conflicts(
                list(barangay_source[1]), barangay_source[0], targets_for_check)
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
                parcel_output_column_overrides = dict(conflicts)
            else:
                parcel_output_column_overrides = {}
        else:
            parcel_output_column_overrides = {}

        # PRIORITY 2: existing OUTPUT-FILE conflict check (local output only).
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
        # Both resolved_table_name AND resolved_outcome are threaded
        # through to run_processing() -- unlike most other migrated
        # tools, resolved_outcome is NOT a throwaway here: it feeds
        # table_action, which is written into the CAMA_Transaction_Log
        # INSERT further down in worker() (see that do-not-touch block's
        # own logic). Matches road_width.py's pattern exactly.
        # ------------------------------------------------------------------
        resolved_table_name = None
        resolved_outcome = None
        if output_mode[0] == "db":
            _resolve_creds = load_db_credentials()
            if not _resolve_creds:
                return
            _resolve_schema = _resolve_creds["schema"]
            resolved_table_name, resolved_outcome = resolve_db_output_table(
                win, _resolve_schema, barangay_source
            )
            if resolved_table_name is None:
                print("Run cancelled by user (database output table not confirmed).")
                return

        win.destroy()
        run_processing(root, overwrite_mode, resolved_table_name, resolved_outcome)

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
        Land Parcel source, an Influence Map source, and an Output
        destination are all selected.

        Explicit bg/fg/cursor toggling (not just state=) is required:
        Tkinter does NOT automatically gray out a classic tk.Button's
        custom bg/fg when state="disabled", and does not suppress a
        widget's assigned cursor either -- both must be set explicitly
        for each state.
        """
        has_parcel = bool(parcel_local_path) if parcel_source_type.get() == "local" else bool(parcel_db_table)
        has_influence = bool(influence_local_paths) if influence_source_type.get() == "local" else bool(influence_db_tables)
        has_output = bool(output_local_dir.get()) if output_dest_type.get() == "local" else True

        if not has_parcel:
            run_status_var.set("Please select a Land Parcel source.")
            ready = False
        elif not has_influence:
            run_status_var.set("Please select an Influence Map source.")
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
    _toggle_influence()
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
        apply_icon(root, "influencemap.ico")
        root.withdraw()
        open_main_window(root)
        root.mainloop()


if __name__ == "__main__":
    main()