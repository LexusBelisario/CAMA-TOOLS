"""
tools/influence_map_to_land_parcel.py

PURPOSE:
    CAMA Tools tool ("INFLUENCE MAP TO LAND PARCEL" in MAIN.py's dispatch
    table): for each selected Land Parcel/Barangay source, transfers one
    attribute value per selected Influence Map (thematic) layer onto the
    parcel, via a spatial join of the parcel's centroid against the
    influence layer (e.g. a flood-hazard or landslide-risk map). Each
    influence layer contributes exactly one CAMA_-prefixed output
    column, named after whichever attribute detect_attr_name() finds on
    that layer.

    UPDATE: detect_attr_name()'s heuristic is no longer the mechanism
    that selects which column(s) get copied -- see its own docstring
    and the "COLUMN-NAME SANITIZATION / COLLISION RESOLUTION" section
    below. Column selection is now a manual, per-source checklist in
    the GUI (built after a background read discovers each selected
    source's eligible -- non-geometry, at-least-one-non-null-value --
    columns), and EVERY checked source now contributes one output
    column per checked column, named CAMA_{SOURCE}_{COLUMN} (both parts
    always sanitized and present, disambiguated with a letter-tier
    suffix on collision -- see _resolve_influence_column_names()).

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
    queue, time (added -- used only for the Influence Map column-
    discovery read's 60-second deadline, time.time()), ctypes, sys,
    tkinter.
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
import time
import math
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox
from tkinter import font as tkfont

import geopandas as gpd
import psycopg2
from sqlalchemy import create_engine, text
from shapely.geometry import Point

from utils.table_name_matching import normalize_name, find_matching_tables
from utils.resource_path import resource_path
from utils.db_discovery import load_db_credentials, fetch_tables
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

    NOTE (multi-layer Influence Map checklist feature): this function's
    own guessing logic is now used ONLY as a fallback for single-layer
    GPKGs and non-GPKG files. Once a specific layer has been discovered
    and checked in the Influence Map checklist, both discovery
    (_read_influence_source_columns_worker()) and the actual Run-time
    read (run_processing().worker()) read that EXACT layer directly via
    gpd.read_file(path, layer=layer) -- bypassing this function's
    guessing entirely for the multi-layer case, since the layer is
    already known with certainty and must never be re-guessed.
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


def _list_gpkg_layers(path: str):
    """
    Returns the list of layer names in a .gpkg file, or [None] for any
    other extension, OR for a .gpkg that happens to have EXACTLY ONE
    layer -- [None] is a sentinel meaning "a single implicit layer,
    read normally via read_vector_file()'s own existing guessing logic"
    (no explicit layer= kwarg needed), used uniformly by the Influence
    Map discovery/processing code below so the same iteration shape
    ("for layer in _list_gpkg_layers(path)") works identically whether
    the file has one implicit layer or several explicitly-named ones.

    Returning [None] for the single-layer .gpkg case (rather than that
    one real layer name) is deliberate and important: it is what makes
    every downstream piece of the multi-layer naming/grouping/nested-
    display machinery engage ONLY for genuinely multi-layer files --
    with only one layer available, read_vector_file()'s own layers[0]
    fallback always lands on that same layer regardless of whether the
    filename stem happens to match it, so falling back to layer=None
    here is functionally identical to explicitly specifying that one
    layer, while preserving the exact pre-multi-layer-feature display
    name (the filename, not the layer name) and code path for that
    common case.

    Mirrors read_vector_file()'s own existing zero-layer error
    convention (raise ValueError) rather than silently returning an
    empty list -- a malformed/empty GeoPackage is treated as a read
    failure through the same existing error-handling path used
    elsewhere in this file, not a silent "no layers, no columns found."
    """
    if os.path.splitext(path)[1].lower() == ".gpkg":
        import fiona
        layers = fiona.listlayers(path)
        if not layers:
            raise ValueError(f"No layers found in GeoPackage: {path}")
        if len(layers) == 1:
            return [None]
        return layers
    return [None]


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

    RETAINED FOR REFERENCE/HISTORY ONLY -- NOT CALLED ANYWHERE IN THIS
    FILE. This heuristic previously auto-selected the single column
    copied from each Influence Map source. It has been fully replaced
    by an explicit, user-checked per-source checklist in the GUI (see
    open_main_window()'s Influence Map section) plus
    _resolve_influence_column_names() for the resulting output-column
    naming -- no execution path in this file calls this function to
    produce a copied attribute value anymore. Kept in place, unused,
    only so its heuristic remains readable for anyone comparing old vs.
    new behavior -- matches this file's own established convention of
    preserving retired logic in place with a clear comment rather than
    deleting it outright (see the disabled CAMA_Table block further
    below for the same convention applied to a different piece of
    retired code).
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

# selected_influence_columns: [{"source_type", "path_or_table",
# "raw_column", "final_column"}, ...] -- the fully resolved,
# collision-disambiguated set of Influence Map columns the user checked
# in the GUI's per-source checklist, computed ONCE per Run (in
# on_run(), via _resolve_influence_column_names() -- see that
# function's own docstring for the collision-resolution rules) and
# consumed by run_processing().worker() so the exact same raw columns
# are read, and the exact same final CAMA_ names are written, that the
# Priority-1 existing-output-column conflict check (also in on_run())
# validated against. Empty list by default/between runs -- never left
# stale from a previous run since on_run() always reassigns it fresh
# before run_processing() is ever invoked.
selected_influence_columns = []


# ========================================
# OUTPUT-COLUMN CONFLICT DETECTION
# ========================================
# This tool's output columns are dynamic, not a fixed list -- each
# CHECKED Influence Map source-column pair contributes one column,
# named CAMA_{SOURCE}_{COLUMN} (see the COLUMN-NAME SANITIZATION /
# COLLISION RESOLUTION section further below for the full naming
# rules). Unlike road_frontage.py/terrain.py/land_shape_compactness.py's
# fixed OUTPUT_COLUMN_TARGETS tuples, this tool's target list is built
# per-run from whichever Influence Map column(s) the user actually
# checked in the GUI.
#
# UPDATE: this section previously included a standalone
# _get_added_fields_for_check() helper that RE-READ every selected
# Influence Map source at Run time (via detect_attr_name()) purely to
# rebuild the target-name list for the conflict pre-check below. That
# helper has been removed entirely -- it is no longer needed at all,
# since the target list (the resolved final_column names) is already
# known the moment the checklist selections are resolved in on_run(),
# with no re-read required. Removing it also closes the specific
# regression risk the task called out explicitly: a leftover
# detect_attr_name()-based fallback path coexisting with the new
# checklist mechanism. See selected_influence_columns above and
# on_run()'s PRIORITY 1 block for the replacement.


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
# COLUMN-NAME SANITIZATION / COLLISION RESOLUTION
# ========================================
# Reimplemented locally here -- NOT imported from
# tools/POI_All_Distance.py. The sanitize/letter-tier/collision pattern
# below is conceptually identical to that file's own
# _sanitize_fclass_to_suffix() / _int_to_letter_tier() /
# _assign_other_type_column_suffixes(), but this is only the SECOND
# tool in the codebase to need it -- per the Rule of Three (governing
# instructions, Section 3.G.5), no shared utility module is extracted
# yet. A genuine third tool needing this exact logic would be the
# trigger to consolidate, not this task.
def _sanitize_influence_name_to_suffix(raw_name: str) -> str:
    """
    Converts one raw name -- an Influence Map source's own display name,
    OR one of that source's column names -- into a valid CAMA_
    column-name fragment: any run of non-alphanumeric characters
    becomes a single underscore, repeated underscores collapse to one,
    leading/trailing underscores are stripped, and the result is
    uppercased. Applied identically to both the SOURCE portion and the
    COLUMN portion of a final CAMA_{SOURCE}_{COLUMN} name -- same rule,
    two call sites (see _resolve_influence_column_names() below).

    The result is guaranteed non-empty: if sanitization leaves nothing
    (the raw value was purely punctuation/whitespace/symbols), a fixed
    placeholder ("UNNAMED") is used instead. Deliberately a different
    placeholder from POI_All_Distance.py's own "OTHER" fallback -- same
    underlying rule, just a visually distinct dead-end value so the two
    tools' fallback columns are never mistaken for each other if ever
    compared side by side.
    """
    suffix = re.sub(r"[^0-9A-Za-z]+", "_", raw_name)
    suffix = re.sub(r"_+", "_", suffix).strip("_")
    suffix = suffix.upper()
    if not suffix:
        suffix = "UNNAMED"
    return suffix


def _int_to_letter_tier(n: int) -> str:
    """
    Deterministic 0-indexed integer -> letter-tier string, used only to
    disambiguate colliding final CAMA_ column names (0->"A", 1->"B",
    ..., 25->"Z", 26->"AA", 27->"AB", ...). Local reimplementation of
    POI_All_Distance.py's own _int_to_letter_tier() -- identical logic,
    duplicated per the Rule of Three (see module note above). A LETTER
    tier (never a digit) is used so it can never be confused with any
    digit that might legitimately appear elsewhere in a sanitized
    column/source name.
    """
    n += 1
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _generate_matching_variants(name: str) -> set:
    """
    Returns the set of "matching variants" for one raw name (either an
    Influence Map filename's own stem, or one layer name of a
    multi-layer .gpkg) -- used SYMMETRICALLY by
    _rebuild_influence_checklist() to decide which layer(s) of a
    multi-layer GeoPackage "belong" to that file (their name matches
    the file's own name closely enough), per design review with the
    user and validated by ChatGPT. The SAME function, with NO special
    casing for which side (filename vs. layer name) it's called on, is
    applied to both -- two names are considered a MATCH if their
    variant sets share at least one common element (plain set
    intersection).

    Algorithm (exact steps, as approved):
      1. Lowercase, then strip EVERY non-alphanumeric character
         entirely (never replaced with a separator -- just removed).
         E.g. "PH_FAULT LINES_1" -> "phfaultlines1",
         "flood_2" -> "flood2".
      2. Split off a TRAILING DIGIT RUN of any length, if present:
         letter_part + digit_suffix. E.g. "phfaultlines1" ->
         letter_part="phfaultlines", digit_suffix="1"; "flood2" ->
         letter_part="flood", digit_suffix="2".
         SPECIAL CASE: if the ENTIRE normalized string is digits only
         (no letters at all -- e.g. a file literally named "2024.gpkg"),
         it is NOT split at all; it is treated as a single atomic
         literal variant (just {"2024"}), since there is no letter part
         to pluralize and splitting a pure number would be meaningless.
      3. From letter_part, derive singular/plural: if letter_part ends
         in "s", singular = letter_part[:-1] and plural = letter_part;
         otherwise singular = letter_part and plural = letter_part+"s".
      4. Build up to 4 variants: {singular, plural} combined with
         {"", digit_suffix} (2 variants instead of 4 when there was no
         digit_suffix to split off). E.g. "phfaultlines1" ->
         {"phfaultline", "phfaultlines", "phfaultline1", "phfaultlines1"};
         "flood2" -> {"flood", "floods", "flood2", "floods2"}.

    Returns a Python set (never a list) -- callers compare two names by
    checking whether their two variant sets intersect at all.
    """
    normalized = re.sub(r"[^0-9a-z]+", "", name.lower())
    if not normalized:
        return {""}

    if normalized.isdigit():
        # Pure-numeric name (no letters at all) -- atomic literal
        # variant only, never digit-split/pluralized.
        return {normalized}

    match = re.search(r"(\d+)$", normalized)
    if match:
        digit_suffix = match.group(1)
        letter_part = normalized[: -len(digit_suffix)]
    else:
        digit_suffix = ""
        letter_part = normalized

    if letter_part.endswith("s"):
        singular = letter_part[:-1]
        plural = letter_part
    else:
        singular = letter_part
        plural = letter_part + "s"

    digit_options = {"", digit_suffix} if digit_suffix else {""}
    return {f"{base}{digits}" for base in (singular, plural) for digits in digit_options}


def _influence_source_display_stem(source_type: str, path_or_table: str, layer=None) -> str:
    """
    Returns the raw (UN-sanitized) name-source string used for one
    Influence Map source's own portion of its final
    CAMA_{SOURCE}_{COLUMN} output column name(s).

    Local file, SINGLE-layer (layer is None -- the vast majority of
    cases: .shp files, and any .gpkg with exactly one layer): the
    filename with its extension stripped (everything after the FINAL
    '.' removed) -- deliberately NOT get_local_name() (defined earlier
    in this file), which instead resolves to the matched GPKG LAYER
    name for a multi-layer GeoPackage. That distinction matters here:
    get_local_name() serves a different, unrelated purpose elsewhere in
    this file (choosing which layer to READ when none has been
    explicitly discovered/checked yet, and naming local OUTPUT files
    after the Barangay/Parcel source) -- reusing it here would silently
    violate this feature's own literal "strip the extension" naming
    rule whenever a .gpkg's internal layer name differs from its
    filename stem. UNCHANGED behavior from before the multi-layer
    feature existed.

    Local file, MULTI-layer (layer is a real layer name string): the
    LAYER NAME itself (sanitized downstream, same as any other naming
    portion) -- NOT the filename. A GeoPackage with several layers
    contributes one independently-named output column set PER LAYER,
    so the layer is what actually identifies which data a checked
    column came from; the filename alone would be ambiguous (and,
    combined with the fact that two different files can legitimately
    have same-named layers -- e.g. A.gpkg's "roads" layer and B.gpkg's
    "roads" layer -- collisions between those are exactly what
    _resolve_influence_column_names()'s existing letter-tier mechanism
    already handles, keyed off _influence_canonical_source_id() below,
    which does distinguish them via the file path).

    Database table: the table name itself, unchanged -- no extension to
    strip, and no concept of "layer" applies (layer is always None for
    DB sources).
    """
    if source_type == "local":
        if layer is not None:
            return layer
        return os.path.splitext(os.path.basename(path_or_table))[0]
    return path_or_table


def _influence_canonical_source_id(source_type: str, path_or_table: str, layer=None) -> str:
    """
    Returns a STABLE identity string for one Influence Map source, used
    ONLY as the deterministic sort key for letter-tier collision
    disambiguation in _resolve_influence_column_names() below -- never
    for display, and never derived from GUI selection order, a file
    dialog's return order, or a database query's return order (all of
    which are explicitly disallowed as a tiering basis by the task).

    Local file: the normalized absolute path, so the same physical file
    always sorts identically regardless of how it was referenced
    (relative vs. absolute, mixed path separators, or case differences
    on a case-insensitive filesystem). When layer is not None (a
    multi-layer GeoPackage), the layer name is appended to this
    identity string -- two different layers of the SAME .gpkg file
    share the same path but are still genuinely different sources
    (different columns, different geometries, different rows), so they
    must never collapse into a single canonical identity.

    Database table: the schema-qualified table identifier. This tool
    only ever targets one configured schema (creds["schema"]) at a
    time, so schema-qualification here is defensive/future-proofing
    rather than a live collision case under the current single-schema
    architecture -- included anyway so the identity string's shape
    stays meaningful if that ever changes. layer is always None for DB
    sources -- no concept of "layer" applies there.
    """
    if source_type == "local":
        base = os.path.normcase(os.path.abspath(path_or_table))
    else:
        creds = load_db_credentials()
        schema = creds["schema"] if creds else ""
        base = f"{schema}.{path_or_table}"
    if layer is not None:
        return f"{base}::{layer}"
    return base


def _resolve_influence_column_names(checked_items):
    """
    Assigns a final, globally-unique CAMA_{SOURCE}_{COLUMN} output
    column name to every CHECKED (source_type, path_or_table, layer,
    raw_column) 4-tuple. Resolution happens ONLY over the currently
    CHECKED set -- a column that was discovered but never checked never
    competes for or consumes a name -- computed fresh at Run time
    inside on_run(), never cached across runs.

    Args:
        checked_items: iterable of (source_type, path_or_table, layer,
        raw_column) 4-tuples, one per checked checklist row across
        every selected Influence Map source. layer is None for any
        single-layer source (a .shp file, a single-layer .gpkg, or a DB
        table) -- unchanged, pre-multi-layer-feature shape in that
        case; layer is a real layer-name string only for a specific
        layer of a multi-layer .gpkg.

    Returns:
        list of dicts, one per input 4-tuple (same order as input),
        each: {"source_type", "path_or_table", "layer", "raw_column",
        "final_column"} -- final_column is the resolved, collision-safe
        CAMA_ name. "layer" is carried through unchanged so
        run_processing().worker() can read the EXACT same layer that
        was discovered and checked, never re-guessing via
        read_vector_file().

    Collision handling -- unifies all three collision types the task
    requires into ONE mechanism, since each is really the same
    underlying event (two checked 4-tuples producing the identical
    CANDIDATE name before disambiguation):
      1. Two different sources with the same sanitized source-name and
         an identically-named checked column -- INCLUDING two different
         .gpkg files whose same-named layers collide (e.g. A.gpkg's
         "roads" layer and B.gpkg's "roads" layer both contributing a
         checked "length" column) -- since the naming basis for a
         multi-layer source is the LAYER name alone (see
         _influence_source_display_stem()), this is the same mechanism
         as any other same-sanitized-name collision, disambiguated the
         same way.
      2. Two different raw column names within the SAME source (same
         file AND same layer) that sanitize to the identical suffix.
      3. Two different sources whose sanitized source-names collide
         even when the selected raw column names differ (this only
         actually produces a collision when the COLUMN portions also
         sanitize identically -- if they don't, the combined candidate
         strings differ and there is nothing to disambiguate).

    A candidate string shared by exactly one 4-tuple keeps that
    candidate name unchanged. A candidate shared by 2+ 4-tuples has
    EVERY one of those 4-tuples' final name disambiguated to
    "{candidate}_{letter_tier}", with letter tiers assigned by sorting
    the colliding 4-tuples on (canonical_source_id, raw_column) --
    canonical_source_id already includes the layer (see
    _influence_canonical_source_id()), so two different layers of the
    SAME file are never conflated -- NEVER on GUI selection order,
    file-dialog return order, or database-query return order -- so the
    same physical source/layer/column, selected in any order relative
    to another colliding source, always resolves to the same final name
    across repeated runs.
    """
    candidates = []
    for source_type, path_or_table, layer, raw_column in checked_items:
        source_suffix = _sanitize_influence_name_to_suffix(
            _influence_source_display_stem(source_type, path_or_table, layer))
        column_suffix = _sanitize_influence_name_to_suffix(raw_column)
        candidate = f"CAMA_{source_suffix}_{column_suffix}"
        canonical_id = _influence_canonical_source_id(source_type, path_or_table, layer)
        candidates.append({
            "source_type": source_type,
            "path_or_table": path_or_table,
            "layer": layer,
            "raw_column": raw_column,
            "candidate": candidate,
            "canonical_id": canonical_id,
        })

    groups = {}
    for entry in candidates:
        groups.setdefault(entry["candidate"], []).append(entry)

    for candidate, members in groups.items():
        if len(members) == 1:
            members[0]["final_column"] = candidate
            continue
        ordered = sorted(members, key=lambda e: (e["canonical_id"], e["raw_column"]))
        for i, entry in enumerate(ordered):
            entry["final_column"] = f"{candidate}_{_int_to_letter_tier(i)}"

    return [
        {
            "source_type": e["source_type"],
            "path_or_table": e["path_or_table"],
            "layer": e["layer"],
            "raw_column": e["raw_column"],
            "final_column": e["final_column"],
        }
        for e in candidates
    ]


# ========================================
# INFLUENCE MAP COLUMN DISCOVERY (background read)
# ========================================
def _read_influence_source_columns_worker(source_type, path_or_table, engine=None, schema=None, layer=None):
    """
    Runs on a background thread (see open_main_window()'s
    _refresh_influence_columns() / _poll_influence_discovery() below).
    Reads ONE Influence Map source fresh (no caching -- matches this
    codebase's established detect-on-select convention) and returns,
    for every ELIGIBLE column (non-geometry, at least one non-null
    value), up to its first 10 distinct non-null values, sorted
    alphabetically (string-cast, for a stable sort across mixed-type
    columns).

    layer: None for a single-layer source (.shp, a single-layer .gpkg,
    or a DB table) -- read exactly as before the multi-layer feature
    existed, via read_vector_file()'s own guessing logic. A real layer
    name string for one specific layer of a multi-layer .gpkg -- reads
    that EXACT layer directly via gpd.read_file(path, layer=layer),
    bypassing read_vector_file()'s guessing entirely, since the caller
    (_refresh_influence_columns()'s worker(), which enumerates every
    layer via _list_gpkg_layers()) already knows precisely which layer
    this call is for.

    Never touches any Tkinter widget or variable -- returns
    (eligible_columns, error) only, matching road_width.py's
    _read_gdf_worker() contract, so it is safe to call from a
    background thread.

    Returns:
        tuple: (eligible_columns, None) on success, where
        eligible_columns is {raw_column_name: [preview_value_str, ...]}
        -- an EMPTY dict (not None) is a valid, successful result for a
        source with no eligible columns at all; the caller
        (_poll_influence_discovery()) is responsible for treating a
        combined zero-eligible-columns-across-every-source result as a
        failure per the task's requirements, which is a distinct
        concern from this function's own per-source success/failure.
        On a hard read error, returns (None, error_message).
    """
    try:
        if source_type == "local":
            if layer is not None:
                gdf = gpd.read_file(path_or_table, layer=layer)
            else:
                gdf = read_vector_file(path_or_table)
        else:
            geom_col = get_geom_column(engine, schema, path_or_table)
            gdf = gpd.read_postgis(
                f'SELECT * FROM "{schema}"."{path_or_table}"', engine, geom_col=geom_col
            )
        gdf = ensure_geometry_column(gdf)
    except Exception as e:
        return None, str(e)

    eligible = {}
    for col in gdf.columns:
        if col.lower() in ("geometry", "geom"):
            continue
        series = gdf[col].dropna()
        # Blank/whitespace-only strings are a distinct "no real value"
        # case from NaN/None -- common for shapefile/DBF text fields
        # (and occasionally numeric-labeled fields stored as text in
        # the DBF) where a "blank" cell reads back as "" rather than an
        # actual null. dropna() alone does not catch these: a column
        # that is visually all-blank in the source data was still
        # passing the eligibility check below, and any blank entries
        # mixed into an otherwise-real column were sorting first in the
        # preview (since "" < any non-empty string), showing as an
        # empty-looking leading value ahead of the real ones.
        series = series[~series.map(
            lambda v: isinstance(v, str) and v.strip() == "")]
        if series.empty:
            continue
        unique_vals = sorted({str(v) for v in series.unique()})[:10]
        eligible[col] = unique_vals
    return eligible, None


# ========================================
# CORE COMPUTATION
# ========================================
def transfer_attributes(barangay_gdf, influence_gdfs, output_column_map=None, progress=None):
    """
    UPDATED (per-source checklist feature): influence_gdfs is now a
    list of (infl_gdf, column_pairs) tuples -- ONE entry per Influence
    Map source that has at least one CHECKED column, where column_pairs
    is a list of (raw_column, final_column) tuples for every column
    checked on that specific source (previously: a list of
    (infl_gdf, attr_name) tuples, exactly one attr_name per source,
    chosen by detect_attr_name()). A source contributes as many output
    columns as the user checked for it -- zero, one, or several -- all
    from a SINGLE spatial join against that source (the join itself is
    unchanged: one gpd.sjoin() per source, not one per column, so
    checking 5 columns on one source still only joins that source's
    geometry once).

    output_column_map : optional {final_column: output_col_name} --
        for each checked (raw_column, final_column) pair, the joined
        value is written into output_column_map.get(final_column,
        final_column) instead of final_column directly. final_column is
        already the fully-resolved, collision-safe CAMA_ name (see
        _resolve_influence_column_names()), so this map's job is now
        purely the pre-existing "write back into an already-existing,
        differently-cased column instead of creating a duplicate"
        override -- the GUI populates it per barangay/parcel source
        when that source already has a matching existing column (see
        _check_parcel_influence_conflicts()), with the exact existing
        name/casing passed here so processing writes back into that
        same column instead of creating a hardcoded duplicate.

        NOTE: this only affects the column name written into
        barangay_gdf (the tool's own local/DB output). It does NOT
        affect CAMA_Table -- that shared, cross-tool table's own
        column names/schema are explicitly out of scope for this
        change (see the CAMA_Table section of run_processing() below,
        which reads from the resolved column name here but writes
        into CAMA_Table under the same unprefixed name it always has).

    progress : optional callable progress(message, value=None, maximum=None),
    called once per influence SOURCE (never per column, and never per
    parcel -- each source's spatial join below is a single vectorized
    gpd.sjoin() call with no per-row visibility to report progress
    against). Optional and defaults to None so this function's existing
    signature is unchanged for any call site that doesn't pass it --
    added as part of this tool's Progress Event Protocol v9 migration
    (see run_processing() below).
    """
    output_column_map = output_column_map or {}
    total = len(influence_gdfs)
    for i, (infl_gdf, column_pairs) in enumerate(influence_gdfs, start=1):
        if progress:
            names = ", ".join(final_col for _raw_col, final_col in column_pairs)
            progress(f"Transferring attribute(s) {i}/{total}: {names}", i, total)

        raw_cols = [raw_col for raw_col, _final_col in column_pairs]
        infl_clean = infl_gdf[raw_cols + ["geometry"]].copy()

        centroids = barangay_gdf.geometry.centroid
        centroid_gdf = gpd.GeoDataFrame(geometry=centroids, crs=barangay_gdf.crs)

        joined = gpd.sjoin(centroid_gdf, infl_clean, how="left", predicate="within")
        joined = joined.loc[:, ~joined.columns.duplicated(keep="first")]

        for raw_col, final_col in column_pairs:
            out_col = output_column_map.get(final_col, final_col)
            barangay_gdf[out_col] = joined[raw_col].reset_index(drop=True)
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
    progress = ProgressWindow(root, "Influence Map to Land Parcel — Progress")
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
            # UPDATE (per-source checklist feature): no longer calls
            # detect_attr_name() -- every raw column read here, and
            # every final CAMA_ name written, comes directly from
            # selected_influence_columns, the resolved (collision-safe)
            # checklist selection already computed once in on_run() via
            # _resolve_influence_column_names(). This is the ONLY place
            # in the module that reads selected_influence_columns; it
            # is treated as read-only, already-validated input here --
            # no re-resolution happens on this thread.
            #
            # Grouped by (source_type, path_or_table, layer) -- now
            # including layer, so a genuinely multi-layer .gpkg's
            # several checked layers are each read/joined exactly ONCE
            # PER LAYER (see transfer_attributes()'s own per-source
            # join), never conflated with each other even though they
            # share the same file path. layer is None for every
            # single-layer source (.shp, single-layer .gpkg, DB table)
            # -- unchanged shape/behavior for that (common) case.
            columns_by_source = {}
            for entry in selected_influence_columns:
                key = (entry["source_type"], entry["path_or_table"], entry["layer"])
                columns_by_source.setdefault(key, []).append(
                    (entry["raw_column"], entry["final_column"]))

            # Only the (source, layer) groups that actually have at
            # least one checked column are read at all -- a
            # selected-but-nothing-checked source (possible when
            # multiple sources are selected and the user only checked
            # columns on some of them) is silently skipped here,
            # contributing zero output columns, per the task's explicit
            # requirement. Iterates the GROUPING KEYS directly (not
            # influence_source[1]'s raw file list, which can no longer
            # represent "one file, several layers" as a 1:1 lookup) --
            # sorted deterministically by (path_or_table, layer) for a
            # stable, reproducible progress-numbering order across
            # runs, never relying on selected_influence_columns' own
            # incidental order. Layer name compared case-INSENSITIVELY
            # (same fix applied to _refresh_influence_columns()'s own
            # layer-listing sort, caught via live testing: raw string
            # comparison sorts ALL-uppercase names before any lowercase
            # one regardless of actual letter order, e.g. "PH_Fault_Line"
            # before "gem_active_faults_harmonized" purely because 'P'
            # precedes 'g' in ASCII -- not the natural alphabetical
            # order a human expects). path_or_table itself stays
            # case-SENSITIVE -- it's a real file path, where case can be
            # semantically significant on case-sensitive filesystems.
            sources_with_columns = sorted(
                columns_by_source.keys(),
                key=lambda k: (k[1], k[2].lower() if k[2] is not None else "")
            )
            total_influence = len(sources_with_columns)
            for i, (src_type, path_or_table, layer) in enumerate(sources_with_columns, start=1):
                label = path_or_table if layer is None else f"{path_or_table} ({layer})"
                progress_cb(f"Loading influence layer {i}/{total_influence}: {label}", i, total_influence)
                column_pairs = columns_by_source[(src_type, path_or_table, layer)]
                if src_type == "local":
                    # A real layer name means this is one specific
                    # layer of a multi-layer .gpkg, discovered and
                    # checked in the GUI checklist -- read that EXACT
                    # layer directly, bypassing read_vector_file()'s
                    # guessing entirely, so the layer actually
                    # processed here can never drift from the layer the
                    # user saw and checked. layer is None for every
                    # single-layer source -- falls back to
                    # read_vector_file() exactly as before this feature
                    # existed (functionally identical for that case,
                    # see _list_gpkg_layers()'s own docstring).
                    if layer is not None:
                        gdf = gpd.read_file(path_or_table, layer=layer).to_crs(epsg=3857)
                    else:
                        gdf = read_vector_file(path_or_table).to_crs(epsg=3857)
                else:
                    geom_col = get_geom_column(engine, schema, path_or_table)
                    gdf = gpd.read_postgis(
                        f'SELECT * FROM "{schema}"."{path_or_table}"', engine, geom_col=geom_col
                    ).to_crs(epsg=3857)
                gdf = ensure_geometry_column(gdf)
                influence_gdfs.append((gdf, column_pairs))
                added_fields.extend(final_col for _raw_col, final_col in column_pairs)

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
                    final_col: src_col_overrides.get(final_col, final_col)
                    for final_col in added_fields
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
                                "tool": "influence_map_to_land_parcel",
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
    dialog.title("INFLUENCE MAP TO LAND PARCEL TOOL")
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
    dialog.title("INFLUENCE MAP TO LAND PARCEL TOOL")
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
    win.title("Influence Map to Land Parcel Tool")
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

    def _reflow_window():
        """
        Safety net for the Influence Map section's dynamic-height
        content (the per-source checklist blocks growing/shrinking as
        sources are selected/deselected/re-read) -- combined with
        win.resizable(False, False) below. Adapted from the verified
        road_width.py/POI_All_Distance.py implementations of the same
        fix (see their own docstrings for the full "why direct
        geometry measurement instead of toggling resizable()"
        rationale): measuring win.winfo_reqwidth()/reqheight() after
        update_idletasks() and explicitly setting minsize/maxsize/
        geometry to that exact value forces ONE clean, complete window
        repaint instead of an incremental resize -- what previously
        left a stale/unpainted region behind whenever dynamic content
        was packed/unpacked elsewhere in this codebase.

        This `win` has never called .geometry() before this feature
        existed -- unlike either reference file's own main window, this
        one has only ever relied on pack()'s automatic initial sizing.
        The lock this function applies therefore only takes effect from
        the first time the Influence Map checklist actually changes
        visibility onward; this window's very first on-screen size
        (before any Influence Map source is even selected) is
        completely unaffected. Called only in response to an actual
        checklist rebuild/visibility change (never on a timer or
        repeating event), so there is no continuous geometry/repaint
        loop, and it never touches any widget belonging to the Land
        Parcel or Output Destination sections.
        """
        win.update_idletasks()
        req_w = win.winfo_reqwidth()
        req_h = win.winfo_reqheight()
        win.minsize(req_w, req_h)
        win.maxsize(req_w, req_h)
        win.geometry(f"{req_w}x{req_h}")

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
    # Named (not anonymous, unlike the previous version) -- needed so
    # _set_influence_reading_state() below can disable/enable them
    # alongside infl_btn while a background column-discovery read is in
    # progress. Confirmed: this section has exactly ONE action button
    # (infl_btn, toggled between "Browse…"/"Select…" by
    # _toggle_influence() -- never two independent buttons) and exactly
    # these two radio buttons -- disabling all three during a read is
    # therefore sufficient to make a second, overlapping discovery
    # structurally impossible, matching the verified convention in
    # road_width.py's _set_parcel_reading_state()/_set_road_reading_state()
    # (one button + two radios each, same reasoning).
    infl_radio_local = tk.Radiobutton(
        infl_radio_row, text="Local File(s)",
        variable=influence_source_type, value="local",
        command=lambda: _toggle_influence())
    infl_radio_local.pack(side="left")
    infl_radio_db = tk.Radiobutton(
        infl_radio_row, text="Database Table(s)",
        variable=influence_source_type, value="db",
        command=lambda: _toggle_influence())
    infl_radio_db.pack(side="left", padx=(12, 0))

    infl_files_var = tk.StringVar(master=win, value="No file(s) selected")
    infl_db_label  = tk.StringVar(master=win, value="No table(s) selected")

    infl_action_row = tk.Frame(influence_frame)
    infl_action_row.pack(fill="x", pady=2)

    infl_lbl = tk.Label(infl_action_row, textvariable=infl_files_var,
                        fg="gray", anchor="w", width=42)
    infl_lbl.pack(side="left")

    infl_btn = tk.Button(infl_action_row, text="Browse…", width=10)
    infl_btn.pack(side="left", **PAD)

    # ── Per-source column checklist state ───────────────────────
    # influence_is_reading: gates a second, overlapping discovery read
    # AND the Run button (see _update_run_button_state()) -- owned
    # directly by _refresh_influence_columns()/_poll_influence_discovery(),
    # NOT by _set_influence_reading_state() (see that function's own
    # docstring for why: it only manages widget state, matching the
    # verified division of responsibility in road_width.py's own
    # parcel_is_reading/road_is_reading flags).
    influence_is_reading = False

    # influence_column_vars: {(source_type, path_or_table, layer):
    # {raw_column: tk.BooleanVar}} -- one entry per (source, layer)
    # that successfully discovered at least one eligible column, layer
    # is None for every single-layer source (unchanged shape from
    # before the multi-layer feature existed), a real layer-name string
    # only for one specific layer of a multi-layer .gpkg -- rebuilt
    # from scratch (never merged across re-selections) every time
    # discovery completes -- matching this codebase's established
    # no-cache convention for detect-on-select checklists.
    influence_column_vars = {}

    # influence_checklist_widgets: {(source_type, path_or_table, layer):
    # {"outer","canvas","vscroll","hscroll","container","window_id"}}
    # -- the live widget handles for each (source, layer)'s own
    # scrollable checklist block, needed by
    # _resize_influence_source_checklist_box() below. Rebuilt in
    # lockstep with influence_column_vars.
    influence_checklist_widgets = {}

    # Item-count-based vertical-scrollbar threshold and left-indent --
    # same values/convention as POI_All_Distance.py's
    # OTHER_LANDMARKS_MAX_ITEMS_BEFORE_VSCROLL /
    # OTHER_LANDMARKS_CHECKLIST_LEFT_INDENT (the explicitly-designated
    # reference implementation for this scrollbar/resize behavior --
    # see governing Prompt). NOT road_width.py's own fixed-pixel-height
    # cap (LOT_CLASSIFICATION_MAX_HEIGHT/ROAD_TYPE_CHECKLIST_MAX_HEIGHT)
    # -- that is a different, older convention in the same codebase;
    # the Prompt explicitly designates POI_All_Distance.py's item-count
    # approach as the reference for this feature.
    INFLUENCE_CHECKLIST_MAX_ITEMS_BEFORE_VSCROLL = 8
    INFLUENCE_CHECKLIST_LEFT_INDENT = 20
    # Extra breathing room between a checklist row's own content
    # (checkbox + label + ℹ️ icon) and the inner scrollbar's left edge
    # -- without this, the canvas width calculation left content
    # running flush against the scrollbar, reported as feeling cramped/
    # slightly cut off at the right edge.
    INFLUENCE_CHECKLIST_RIGHT_MARGIN = 8
    # Per the user's explicitly approved "3 spaces" indent format for
    # the Filename -> Layers: -> layername -> Checkboxes nesting
    # hierarchy (see _rebuild_influence_checklist()'s own multi-layer
    # grouping logic below) -- measured against the ACTUAL font used
    # for those labels (never a guessed pixel number, matching the
    # "measure the real rendered content" principle already applied
    # throughout this feature), so "3 spaces" means the same visual
    # width it would in any text editor, not an arbitrary constant.
    # Deliberately SEPARATE from INFLUENCE_CHECKLIST_LEFT_INDENT above
    # -- that constant governs a different, already-tested mechanism
    # (the checkbox rows' own indent WITHIN one source's block), not
    # this new group-level nesting.
    _group_nesting_font = tkfont.Font(family="Segoe UI", size=8)
    INFLUENCE_LAYER_GROUP_INDENT = _group_nesting_font.measure("   ")

    # INFLUENCE_SOURCES_OUTER_MAX_HEIGHT: pixel-height cap for the OUTER
    # wrapper holding every selected source's own block (header + its
    # own inner scrollable column checklist) stacked vertically -- grows
    # to fit content up to this cap, then scrolls internally, so
    # selecting many Influence Map sources at once no longer forces the
    # whole configuration window to grow arbitrarily tall (previously
    # the window itself just kept growing with every additional source,
    # eventually running off-screen). A fixed-pixel cap (not an
    # item-count threshold like INFLUENCE_CHECKLIST_MAX_ITEMS_BEFORE_VSCROLL
    # above) is the right fit HERE specifically because each source
    # block's own height varies widely depending on how many columns it
    # has (and whether ITS OWN inner checklist is already showing a
    # scrollbar) -- unlike POI_All_Distance.py's uniform-height
    # checkbox rows, a per-source "item count" doesn't translate to a
    # predictable pixel height. This instead mirrors road_width.py's
    # own fixed-pixel-cap convention (LOT_CLASSIFICATION_MAX_HEIGHT/
    # ROAD_TYPE_CHECKLIST_MAX_HEIGHT), applied at the OUTER
    # (multi-source) level while the existing per-source INNER
    # checklists keep their own, separately-designated item-count
    # convention untouched.
    INFLUENCE_SOURCES_OUTER_MAX_HEIGHT = 260

    # INFLUENCE_PREVIEW_TRUNCATE_CHARS: per-VALUE character ceiling in
    # the hover preview -- a value longer than this is shown truncated
    # (with a trailing "…") until its column's icon is clicked to
    # expand it. This is a per-value cap, not a per-column/total cap --
    # a column with several short values and one very long one only
    # truncates the long one.
    #
    # INFLUENCE_PREVIEW_WRAP_PIXELS: the preview Text widget's own
    # target pixel width -- content wraps within this width rather than
    # ever growing wider (or leaving dead space narrower than its
    # content, the exact visual bug worked through and fixed during
    # design review). Converted to a character count via
    # tkfont.Font.measure() against the ACTUAL font used for the
    # preview text below, never a guessed average-character-width
    # constant, since that would drift from the real rendered width per
    # font/theme.
    INFLUENCE_PREVIEW_TRUNCATE_CHARS = 72
    INFLUENCE_PREVIEW_WRAP_PIXELS = 420

    # influence_preview_expanded: {(source_key, raw_column): bool} --
    # per-column sticky "see more"/"see less" toggle state for the
    # hover preview. False (truncated) by default for every column;
    # flipped by clicking the column's own ℹ️ icon (see
    # _toggle_influence_preview() below). Deliberately SURVIVES a
    # hover leaving and returning -- the whole point of "sticky" here
    # is that the next hover on the same column shows whichever state
    # was last chosen, not always resetting to truncated. Reset to
    # empty only when the checklist itself is rebuilt from a fresh
    # discovery (see _rebuild_influence_checklist()) -- a genuinely new
    # read has nothing meaningful to stay "expanded" from.
    influence_preview_expanded = {}

    # infl_sources_outer / infl_sources_canvas / infl_sources_vscroll:
    # the OUTER scroll wrapper holding every selected source's own
    # block, stacked vertically inside infl_sources_container (below) --
    # capped at INFLUENCE_SOURCES_OUTER_MAX_HEIGHT, scrolling internally
    # past that. This is the "outer" half of the nested-scroll design:
    # each individual source's own INNER checklist (built inside its own
    # block, see _build_influence_source_block()) keeps its own,
    # completely separate Canvas+Scrollbar exactly as before -- this
    # outer wrapper only ever scrolls the LIST OF SOURCES itself, never
    # reaches into or affects any individual source's own inner
    # scrolling. Same Canvas/create_window/scrollregion/mousewheel
    # architecture already used for every inner per-source checklist in
    # this file, applied one level up.
    infl_sources_outer = tk.Frame(influence_frame)
    infl_sources_outer.pack(fill="x", pady=(2, 0))
    infl_sources_canvas = tk.Canvas(infl_sources_outer, highlightthickness=0, bd=0)
    infl_sources_vscroll = tk.Scrollbar(
        infl_sources_outer, orient="vertical", command=infl_sources_canvas.yview,
        width=12)
    # infl_sources_content_width: the SINGLE authoritative "available
    # width for checklist content, already accounting for the outer
    # (main) Influence checklist scrollbar's own width whenever it's
    # shown" -- set ONLY by _resize_infl_sources_outer() (the one place
    # that actually knows the outer scrollbar's current state), and
    # read directly by _resize_influence_source_checklist_box() for
    # every per-source/per-layer block's own canvas-width calculation.
    # Previously, that per-block calculation independently recomputed
    # its own separate estimate of this same "outer scrollbar
    # clearance" from infl_action_row's width minus its own separate
    # infl_sources_vscroll.winfo_ismapped() check -- two parallel
    # calculations of the identical thing, which is exactly the kind of
    # accumulating-margins risk that can drift out of sync and leave
    # checklist content overlapping the outer (main) Influence checklist
    # scrollbar, distinct from and in addition to the already-verified
    # INNER per-source scrollbar clearance (INFLUENCE_CHECKLIST_RIGHT_MARGIN,
    # defined above). Starts at 0 (meaning "not yet established for the
    # current rebuild") -- _resize_influence_source_checklist_box()
    # falls back to the old infl_action_row-based estimate only for
    # that brief window (the first per-block sizing pass, which always
    # runs before _resize_infl_sources_outer() has had a chance to
    # compute the authoritative value for THIS rebuild -- see
    # _rebuild_influence_checklist()'s own two-pass structure), then
    # every block is re-sized a second time using the now-authoritative
    # value once it's known.
    infl_sources_content_width = 0
    infl_sources_canvas.configure(yscrollcommand=infl_sources_vscroll.set)
    infl_sources_canvas.pack(side="left", fill="both", expand=True)
    # infl_sources_vscroll is packed/unpacked dynamically by
    # _resize_infl_sources_outer() below -- only shown once the
    # combined height of every selected source's block actually exceeds
    # INFLUENCE_SOURCES_OUTER_MAX_HEIGHT. No horizontal scrollbar is
    # needed at this OUTER level -- every source block's own content is
    # already width-pinned to infl_action_row's width by each source's
    # own _resize_influence_source_checklist_box() (with ITS OWN
    # horizontal scrollbar for long column names, unaffected by any of
    # this) -- nothing at the outer level ever needs to scroll
    # horizontally.

    # infl_sources_container: holds one stacked block per discovered
    # Influence Map source (built fresh by _rebuild_influence_checklist()
    # below). A plain vertically-packed Frame -- unlike POI's single
    # flat checklist, there is no separate "has content" visibility
    # toggle needed here: each source's own header+checklist block is
    # only ever created when that source actually has eligible columns,
    # so an empty container naturally contributes zero height on its
    # own with no extra bookkeeping. Now lives INSIDE infl_sources_canvas
    # (the outer scroll wrapper above) rather than directly in
    # influence_frame -- _build_influence_source_block()'s own use of
    # infl_sources_container as the parent for each new block is
    # completely unchanged.
    infl_sources_container = tk.Frame(infl_sources_canvas)
    _infl_sources_canvas_window = infl_sources_canvas.create_window(
        (0, 0), window=infl_sources_container, anchor="nw")

    def _on_infl_sources_content_configure(_e=None):
        infl_sources_canvas.configure(scrollregion=infl_sources_canvas.bbox("all"))
    infl_sources_container.bind("<Configure>", _on_infl_sources_content_configure)

    # Explicit, small yscrollincrement -- Tk's Canvas defaults to an
    # UNSET increment, which makes each "units" scroll (below) jump by a
    # coarse, uneven pixel amount that reads as "jumpy" rather than
    # smooth. A small, fixed increment (4px) makes every wheel notch
    # move a small, consistent distance instead, regardless of how much
    # total content is currently in the scroll region.
    infl_sources_canvas.configure(yscrollincrement=4)

    def _on_infl_sources_mousewheel(event):
        infl_sources_canvas.yview_scroll(int(-1 * (event.delta / 120)) * 3, "units")
    infl_sources_canvas.bind(
        "<Enter>", lambda e: infl_sources_canvas.bind_all(
            "<MouseWheel>", _on_infl_sources_mousewheel))
    infl_sources_canvas.bind(
        "<Leave>", lambda e: infl_sources_canvas.unbind_all("<MouseWheel>"))

    def _resize_infl_sources_outer(known_content_height=None):
        """
        Recomputes infl_sources_canvas's own height/width handling to
        fit the CURRENT combined content of every built source block --
        the outer half of the nested-scroll design (see
        infl_sources_outer's own construction comment above). Called
        once, at the end of _rebuild_influence_checklist(), after every
        source block for the current discovery result has already been
        built (and each already correctly self-sized via its own
        _resize_influence_source_checklist_box() call) -- so this
        function only ever measures already-final content, never
        triggers a second round of inner-block resizing.

        known_content_height: the pure-Python-computed total height
        (pixels) of every top-level item now inside
        infl_sources_container, accumulated by
        _rebuild_influence_checklist() as it builds each block/group
        (see that function's own running-total tracking) -- used
        DIRECTLY when provided, in preference to ever asking Tk for
        infl_sources_container's own winfo_reqheight(). That live query,
        on a container that can now be several levels deep (flat blocks
        mixed with multi-layer groups, each themselves containing a
        header Label plus a nested sub-frame of further blocks),
        measured correctly in every test run in this Linux/Xvfb sandbox
        but was reported to produce an incorrect (missing) outer
        scrollbar decision during real on-machine Windows testing --
        matching the same class of cross-platform Tk geometry-
        realization-timing discrepancy already found and fixed once
        before for this exact tooltip subsystem (see
        _show_influence_preview()'s own history with tk.Text's
        count(..., "displaylines")). Falls back to the old live-query
        behavior only when known_content_height is not supplied (no
        current caller omits it, but this keeps the function safely
        callable in isolation, e.g. for future maintenance or testing).

        Width is pinned to infl_action_row's own already-established
        requested width (same reference every inner per-source block
        already uses) -- never left to grow/shrink independently, so
        the outer wrapper's edges always line up with everything above
        and below it in this section.

        Also sets the canvas's own scrollregion authoritatively from
        this SAME known_content_height (see the dedicated comment at
        that call site, near the end of this function's body) --
        previously left solely to the separate <Configure> binding's
        own live canvas.bbox("all") query, which is a distinct
        potential point of platform-dependent staleness from the
        scrollbar-visibility decision above it (the scrollbar could
        show as present while still scrolling against a stale/
        undersized region). That <Configure> binding itself is
        untouched and still active as a secondary sync mechanism.

        Also establishes infl_sources_content_width (nonlocal, see its
        own module-level comment above) -- the single authoritative
        "available width for checklist content, with outer-scrollbar
        clearance already applied" value that
        _resize_influence_source_checklist_box() reads directly for
        every block's own width calculation, rather than that function
        independently recomputing the same "does the outer scrollbar
        need clearance" logic a second, separate time.
        """
        nonlocal infl_sources_content_width
        if known_content_height is not None:
            content_height = known_content_height
        else:
            infl_sources_container.update_idletasks()
            content_height = infl_sources_container.winfo_reqheight()
        fixed_width = infl_action_row.winfo_reqwidth()

        if content_height <= INFLUENCE_SOURCES_OUTER_MAX_HEIGHT:
            infl_sources_canvas.configure(height=content_height, width=fixed_width)
            infl_sources_vscroll.pack_forget()
        else:
            vscroll_width = infl_sources_vscroll.winfo_reqwidth()
            infl_sources_canvas.configure(
                height=INFLUENCE_SOURCES_OUTER_MAX_HEIGHT,
                width=max(fixed_width - vscroll_width, 1))
            infl_sources_vscroll.pack(side="right", fill="y")

        infl_sources_canvas.update_idletasks()
        embedded_width = infl_sources_canvas.winfo_reqwidth()
        infl_sources_canvas.itemconfig(_infl_sources_canvas_window, width=embedded_width)
        infl_sources_content_width = embedded_width

        # Explicit, AUTHORITATIVE scrollregion -- derived from the SAME
        # known_content_height already trusted above for the
        # scrollbar-visibility decision, not a second, independent live
        # canvas.bbox("all") query. Scrollbar VISIBILITY and the
        # canvas's actual scrollable EXTENT were previously computed
        # from two different sources of truth (this function's
        # known_content_height for visibility, vs. bbox("all") --
        # queried only indirectly, via the separate <Configure>
        # binding above -- for the extent itself): it's possible for
        # the scrollbar to correctly show as visible while the
        # scrollregion it operates against is still stale or too small
        # (e.g. if the <Configure> event backing bbox("all") hasn't
        # fired/settled yet on a given platform), which would make the
        # scrollbar appear present but not actually reveal the
        # remaining content when used -- a distinct failure mode from
        # "scrollbar missing entirely." Setting scrollregion here,
        # authoritatively, at this same known-reliable point (right
        # after the checklist has just been built/rebuilt), removes
        # that gap -- both the visibility decision and the actual
        # scrollable extent now derive from the identical trusted
        # value. embedded_width matches infl_sources_container's own
        # pinned width (set just above via itemconfig), so the
        # scrollregion's virtual coordinate space matches what's
        # actually embedded, not just a nominal checklist-rows height.
        #
        # The <Configure> binding (_on_infl_sources_content_configure(),
        # bound near this canvas's own construction above) is left
        # completely unchanged and still active as a SECONDARY
        # synchronization mechanism -- it can still refresh scrollregion
        # via its own bbox("all") query in response to any further,
        # unanticipated content change -- it is simply no longer the
        # ONLY mechanism responsible for the scrollregion being correct
        # immediately after a rebuild.
        infl_sources_canvas.configure(
            scrollregion=(0, 0, embedded_width, content_height))

    # ── floating hover-preview tooltip (Toplevel-based, per the
    # required "never packed inline into the main window" mechanism) ──
    # A single, PERSISTENT Toplevel/Frame/Text, created ONCE here and
    # reused for every hover (hidden via withdraw(), shown via
    # deiconify(), content/size/position updated in place) -- NOT
    # destroyed and recreated on every <Enter>/<Leave> cycle as an
    # earlier version of this feature did. On-machine Windows testing
    # of that earlier destroy/recreate design showed two symptoms
    # consistent with a stale, not-yet-repainted remnant of a PREVIOUS
    # tooltip window still occupying screen space: a small stray
    # visual artifact left floating on screen, and a case where a NEW
    # tooltip's own content (specifically its trailing "click ℹ️ to
    # see more" hint line) appeared hidden/obscured. Rapid
    # destroy+recreate of an overrideredirect Toplevel is a known class
    # of Windows-compositor redraw-timing issue; reusing one persistent
    # window removes that churn entirely, eliminating the whole class
    # of bug regardless of its exact underlying cause -- rather than
    # attempting to patch around a specific symptom.
    _influence_tooltip_ref = {
        "icon": None, "source_key": None, "col": None, "values": None,
    }

    _influence_preview_tip = tk.Toplevel(win)
    _influence_preview_tip.wm_overrideredirect(True)
    _influence_preview_tip.attributes("-topmost", True)
    _influence_preview_tip.withdraw()
    _influence_preview_frame = tk.Frame(
        _influence_preview_tip, bg="#333333", bd=1, relief="solid")
    _influence_preview_frame.pack()
    _influence_preview_font = tkfont.Font(family="Segoe UI", size=8)
    # Italic variant, used ONLY for the trailing "to see more/less
    # click ℹ️" hint line -- styled distinctly from the bullet-point
    # value rows above it so it visually reads as an instruction/hint,
    # not part of the actual data content, per explicit request.
    _influence_preview_hint_font = tkfont.Font(family="Segoe UI", size=8, slant="italic")
    _influence_preview_text = tk.Text(
        _influence_preview_frame, bg="#333333", fg="white",
        font=_influence_preview_font, wrap="char", bd=0,
        highlightthickness=0, padx=6, pady=4, cursor="hand2")
    # Hanging indent: a WRAPPED continuation line (lmargin2) must start
    # flush with the FIRST LETTER of the value text on the line above it
    # -- i.e. immediately after "• " -- never under the bullet character
    # itself. Measured from the actual rendered pixel width of "• " in
    # this exact font/size (never a fixed guessed pixel number, which
    # reported live as visibly misaligned on real Windows rendering --
    # the same "measure the real content, don't guess a constant"
    # principle already applied throughout this preview subsystem's
    # truncation/wrapping/width logic elsewhere in this file). lmargin1
    # stays 0 -- the bullet's own first line is flush against the
    # Text widget's own padx, unaffected by this.
    _influence_preview_text.tag_configure(
        "hanging", lmargin1=0, lmargin2=_influence_preview_font.measure("\u2022 "))
    _influence_preview_text.tag_configure("hint", font=_influence_preview_hint_font)
    _influence_preview_text.pack()

    def _on_influence_preview_click(_e=None):
        """
        Single, permanent click handler for the persistent preview
        Text widget -- reads WHICH column the tooltip is currently
        showing from _influence_tooltip_ref at the moment of the
        click (always current, since _show_influence_preview() updates
        it on every call), rather than a fresh per-hover lambda closure
        binding a specific (source_key, col) each time the way the
        previous destroy/recreate design needed to.
        """
        ref = _influence_tooltip_ref
        if ref["source_key"] is not None and ref["col"] is not None:
            _toggle_influence_preview(ref["source_key"], ref["col"])
    _influence_preview_text.bind("<Button-1>", _on_influence_preview_click)

    def _hide_influence_preview():
        """
        Hides the persistent preview tooltip (withdraw(), never
        destroy() -- see this section's own opening comment above for
        why). Being a Toplevel (never packed/gridded into `win`'s own
        layout) means showing or hiding it can NEVER change win's own
        required width or height -- the exact bug class already found
        and fixed in POI_All_Distance.py for a DIFFERENT mechanism
        (dynamic content packed directly into the main window); the
        floating Toplevel approach here is the deliberate opposite of
        that fix, staying entirely outside win's pack() geometry rather
        than being pinned to a fixed width within it.
        """
        _influence_preview_tip.withdraw()
        _influence_tooltip_ref["source_key"] = None
        _influence_tooltip_ref["col"] = None

    def _show_influence_preview(icon_widget, source_key, col, values):
        """
        Shows a small borderless Toplevel to the right of icon_widget
        (the per-row ℹ️ icon), listing `values` -- already the exact
        up-to-10 sorted unique non-null values discovered for that
        column during the background read (see
        _read_influence_source_columns_worker()) -- never a fresh
        per-hover re-read. Positioned via the icon's own screen
        coordinates (winfo_rootx()/winfo_rooty()), so it can visually
        extend beyond win's own on-screen boundaries, same as a native
        OS tooltip.

        Content is a read-only tk.Text (not a tk.Label -- a Label has
        no per-paragraph indent support). Each value is its own
        bullet ("• value") paragraph, tagged with a hanging indent
        (lmargin1=0 for the bullet's own first line, lmargin2 indented
        for any WRAPPED continuation of that same value) -- this is
        the Tkinter-native equivalent of the CSS
        text-indent/padding-left hanging-indent trick worked out during
        design review, so a value that wraps to a second visual line is
        visibly distinguishable from the start of the NEXT value's own
        bullet. wrap="char" fills each line as full as possible before
        wrapping (never breaks preemptively the way CSS
        overflow-wrap:anywhere could) -- confirmed during design review
        to be the correct choice so a long, space-free value (e.g. a
        comma-separated numeric string) still wraps within the widget's
        own width instead of overflowing past it, without ever
        orphaning a bullet onto its own line.

        Per-value truncation: any value longer than
        INFLUENCE_PREVIEW_TRUNCATE_CHARS is shown cut short with a
        trailing "…" UNLESS this exact column's sticky
        influence_preview_expanded state is currently True, in which
        case every value is shown in full. If at least one value in
        this column was long enough to be affected, a final clickable
        line reads "to see more click ℹ️" (currently truncated) or
        "to see less click ℹ️" (currently expanded) -- ℹ️ at the END
        of the phrase (not sandwiched mid-sentence, per an earlier,
        removed placement), styled ITALIC via the "hint" tag so it
        reads as an instruction/hint distinct from the bullet-point
        data rows above it -- clicking anywhere in the Text widget,
        not just that line, toggles the state (see the <Button-1>
        binding below), matching the ℹ️ icon's own click behavior for
        a larger, easier click target.
        """
        # Reuses the SINGLE persistent Toplevel/Frame/Text built once
        # above (_influence_preview_tip/_influence_preview_frame/
        # _influence_preview_text) -- never creates a new one per hover
        # (see this section's opening comment for why).
        preview_font = _influence_preview_font

        # ------------------------------------------------------------
        # PIXEL-ACCURATE truncation and wrapping -- everything below
        # measures the ACTUAL candidate text via preview_font.measure(),
        # never a character-COUNT approximation. An earlier version of
        # this function derived a fixed "characters per line" ceiling
        # from the pixel width of the "0" glyph alone, then used plain
        # len()/textwrap.wrap() (character-count-based) against that
        # ceiling -- which silently assumed every character renders as
        # wide as "0". That assumption fails for ordinary mixed-case
        # text (citation-style values full of wide capital letters, e.g.
        # "Abdnasser and McCaffrey 2015 J. Earth Systems Science"): the
        # ACTUAL pixel width of such a truncated string can exceed the
        # intended ceiling even though its character COUNT was within
        # the computed limit, so it still wrapped to a second line
        # despite the truncation "guarantee." Every measurement below
        # is instead against the real rendered pixel width of the
        # specific text being displayed, which cannot have that failure
        # mode regardless of which characters are involved.
        # ------------------------------------------------------------
        bullet_prefix = "\u2022 "

        def _pixel_truncate_value(v):
            """
            Returns (display_text, was_truncated). Measures the
            COMBINED "bullet_prefix + value" string directly (never the
            bullet prefix and the value as two separately-measured
            pieces summed together) -- summing separate measurements
            implicitly assumes zero kerning/combining effect between the
            last character of the bullet and the first character of the
            value, which isn't guaranteed for every font; measuring the
            exact string that will actually be rendered removes that
            assumption entirely, one more layer of defense against the
            reported cross-platform wrapping discrepancy (see this
            block's own opening comment).

            If the full "bullet + value" already fits
            INFLUENCE_PREVIEW_WRAP_PIXELS, returned unchanged.
            Otherwise, greedily builds the longest PREFIX of v whose
            combined "bullet + prefix + …" measured width still fits,
            one real character at a time (not an estimated count).
            Guarantees the final rendered line's true width never
            exceeds INFLUENCE_PREVIEW_WRAP_PIXELS, for any font or
            character mix.
            """
            if preview_font.measure(f"{bullet_prefix}{v}") <= INFLUENCE_PREVIEW_WRAP_PIXELS:
                return v, False
            truncated = ""
            for ch in v:
                candidate = truncated + ch
                if preview_font.measure(f"{bullet_prefix}{candidate}…") <= INFLUENCE_PREVIEW_WRAP_PIXELS:
                    truncated = candidate
                else:
                    break
            return truncated + "…", True

        def _pixel_wrap_line(text, budget_px):
            """
            Greedy pixel-based line wrap -- the same "fill each line as
            full as possible before wrapping" behavior as the Text
            widget's own wrap="char" mode, but measured against REAL
            per-character pixel widths (preview_font.measure()) instead
            of a character-count approximation. Only reached for values
            in the EXPANDED state (multi-line wrapping is legitimate
            there, unlike the truncated state where a single line is
            mandatory) -- used so the box's HEIGHT estimate stays
            accurate for wide-character text too, not just narrow/
            numeric content.
            """
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

        is_expanded = influence_preview_expanded.get((source_key, col), False)
        any_truncated = False

        if not values:
            # Defensive only -- an eligible column always has at least
            # one real value (see _read_influence_source_columns_worker()'s
            # blank/whitespace filtering), so this path is not expected
            # to be reached in normal use.
            display_lines = ["(no values)"]
            all_wrapped_lines = ["(no values)"]
        else:
            display_lines = []
            # all_wrapped_lines: every line this content will actually
            # occupy once rendered, computed HERE in plain Python --
            # BEFORE the Text widget is even created, let alone packed/
            # positioned/realized. Reused for BOTH the widget's height
            # (line count) and its adaptive width (longest line's real
            # pixel width, converted to Tk's own character-unit basis
            # below) -- one computation, two uses, so the two can never
            # drift out of sync with each other.
            #
            # UPDATE: previously line count alone was measured AFTER
            # insertion via tk.Text's own count(..., "displaylines"),
            # which requires the widget to already have real, realized
            # on-screen pixel geometry to compute correctly -- confirmed
            # correct in Linux/Xvfb testing during design review, but
            # on-machine Windows testing showed a wildly oversized
            # tooltip box, meaning Windows Tk's realization timing for
            # an overrideredirect Toplevel does not reliably match
            # Xvfb's. Precomputing in pure Python removes that
            # cross-platform timing dependency entirely.
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
                    f"{bullet_prefix}{display_v}", INFLUENCE_PREVIEW_WRAP_PIXELS)
                all_wrapped_lines.extend(wrapped)

            show_hint = any_truncated or (is_expanded and any(
                preview_font.measure(f"{bullet_prefix}{v}") > INFLUENCE_PREVIEW_WRAP_PIXELS
                for v in values))
            if show_hint:
                # Design history (full round-trip): "click ℹ️ to see
                # more" (mid-sentence icon, single space) reported
                # cramped -> "click  ℹ️  ..." (double space) overcorrected
                # into an unexplained-looking large gap -> reverted to
                # single space, still inconsistent on real Windows
                # rendering (unreproducible in this Linux/Xvfb sandbox at
                # every stage) -> emoji removed entirely ("click icon to
                # see more/less", plain text) as a deterministic
                # workaround, confirmed working via live Windows
                # screenshot -> per this follow-up request, the icon is
                # now back, but relocated to the END of the phrase
                # (never sandwiched between two words, where the earlier
                # spacing issues were reported) and the whole line styled
                # ITALIC (see the "hint" tag, configured once alongside
                # the persistent Text widget above) so it reads
                # distinctly as an instruction/hint, not another data
                # row. The actual clickable ℹ️ icon in each checklist
                # row (a separate, standalone glyph next to its checkbox
                # -- see the icon Label built in
                # _build_influence_source_block() below) has never been
                # affected by any of this -- only this hint TEXT line.
                # Trailing position means there's no surrounding word on
                # the icon's right side for a font-fallback metrics
                # mismatch to visibly disrupt (the earlier "click ℹ️ to
                # see more" mid-sentence placement was the one reported
                # as inconsistently spaced on real Windows) -- still not
                # a guarantee, since this remains unverified on real
                # Windows rendering, only a lower-risk placement.
                hint = "to see less click ℹ️" if is_expanded else "to see more click ℹ️"
                all_wrapped_lines.append(hint)

        total_display_line_count = len(all_wrapped_lines)
        # Adaptive WIDTH: shrinks to the actual longest line rendered
        # (never wider than the same INFLUENCE_PREVIEW_WRAP_PIXELS
        # ceiling used for wrapping/truncation above) instead of always
        # reserving a fixed width even when every value is short -- the
        # black background now tracks content width the same way it
        # already tracks content height, rather than staying a fixed
        # wide box regardless of how little text is actually inside it.
        #
        # Measured in ACTUAL PIXELS via preview_font.measure() throughout
        # (all_wrapped_lines above is already pixel-accurate -- see
        # _pixel_truncate_value()/_pixel_wrap_line()). Converted into
        # Tk's own "0"-glyph character-unit basis only here, at the very
        # end, ONLY because tk.Text's `width` option itself has no
        # pixel-direct equivalent (it is always defined internally as
        # multiples of the "0" digit glyph's pixel width) -- this is the
        # sole remaining character-count concept in this function, used
        # purely as the unavoidable final unit conversion for the widget
        # API, never as an approximation anywhere in the truncation or
        # wrapping logic above it.
        #
        # The trailing hint line, when present, is measured with
        # _influence_preview_hint_font (its own actual ITALIC font) --
        # never preview_font -- since italic glyph metrics can differ
        # slightly from the upright font used for every bullet line
        # above it; every OTHER line in all_wrapped_lines still uses
        # preview_font, matching how each is actually rendered.
        #
        # math.ceil() (never round()) for this pixel-to-character-unit
        # conversion, PLUS a 1-unit safety margin: round() can round
        # DOWN, which would configure the Text widget slightly NARROWER
        # (in real pixels) than the content actually measured -- on a
        # font/DPI combination where measured metrics differ even
        # slightly from what actually renders (this Linux sandbox has no
        # Segoe UI installed and silently substitutes a different font
        # with different glyph widths, so this exact discrepancy can't
        # be fully verified here; real Windows DPI scaling is another
        # plausible source of the same kind of small mismatch), a
        # rounded-down width is exactly the mechanism that could cause a
        # value narrow enough to NOT need truncation to still wrap onto
        # an unwanted second line. Erring toward ceil()+1 costs at most a
        # sliver of harmless empty space on the right; erring toward
        # round()/floor() risked the actual reported bug.
        zero_char_pixel_width = max(preview_font.measure("0"), 1)
        width_ceiling_chars = max(
            math.ceil(INFLUENCE_PREVIEW_WRAP_PIXELS / zero_char_pixel_width), 20)
        bullet_lines_only = all_wrapped_lines[:-1] if show_hint else all_wrapped_lines
        max_line_pixel_width = max(
            (preview_font.measure(l) for l in bullet_lines_only),
            default=zero_char_pixel_width * 10)
        if show_hint:
            max_line_pixel_width = max(
                max_line_pixel_width, _influence_preview_hint_font.measure(hint))
        actual_width_chars = min(
            max(math.ceil(max_line_pixel_width / zero_char_pixel_width) + 1, 1),
            width_ceiling_chars)

        _influence_preview_text.configure(state="normal")
        _influence_preview_text.delete("1.0", "end")
        _influence_preview_text.configure(
            width=actual_width_chars, height=total_display_line_count)

        if not values:
            _influence_preview_text.insert("end", "(no values)")
        else:
            for display_v in display_lines:
                _influence_preview_text.insert("end", f"\u2022 {display_v}\n", "hanging")
            if show_hint:
                _influence_preview_text.insert("end", hint, "hint")
            else:
                # No trailing newline to trim on the last bullet line
                # when there's no hint line following it.
                _influence_preview_text.delete("end-2c", "end-1c")

        _influence_preview_text.configure(state="disabled")

        x = icon_widget.winfo_rootx() + icon_widget.winfo_width() + 4
        y = icon_widget.winfo_rooty()
        _influence_preview_tip.geometry(f"+{x}+{y}")
        _influence_preview_tip.deiconify()
        _influence_preview_tip.lift()
        _influence_preview_tip.update_idletasks()

        _influence_tooltip_ref["icon"] = icon_widget
        _influence_tooltip_ref["source_key"] = source_key
        _influence_tooltip_ref["col"] = col
        _influence_tooltip_ref["values"] = values

    def _toggle_influence_preview(source_key, col):
        """
        Flips the sticky truncate/expand state for exactly one column
        (source_key, col) -- persists across hover leave/enter cycles
        until toggled again or the checklist is rebuilt from a fresh
        discovery (see influence_preview_expanded's own module-note and
        _rebuild_influence_checklist()).

        If the tooltip currently on screen belongs to this EXACT same
        column (tracked in _influence_tooltip_ref, set by
        _show_influence_preview()), re-renders it immediately in place
        so the click's effect is visible without requiring the mouse to
        leave and re-enter the icon -- the confirmed "click while still
        hovering" interaction from design review. If the click somehow
        arrives for a column whose tooltip is no longer the one shown
        (a defensive case, not expected in normal use since the Text
        widget the click landed on IS that tooltip's own content), only
        the state is updated -- the NEXT hover on that column will
        reflect it correctly regardless.
        """
        key = (source_key, col)
        influence_preview_expanded[key] = not influence_preview_expanded.get(key, False)

        ref = _influence_tooltip_ref
        if (_influence_preview_tip.state() != "withdrawn"
                and ref.get("source_key") == source_key
                and ref.get("col") == col):
            _show_influence_preview(ref["icon"], source_key, col, ref["values"])

    def _check_all_source(source_key):
        """
        Sets EVERY discovered column's BooleanVar to True for exactly
        ONE source (source_key = (source_type, path_or_table, layer)) --
        iterates influence_column_vars[source_key] directly, never any
        other source's vars, so checking one source's "Check All" can
        never affect any other selected source's checklist. Mirrors
        POI_All_Distance.py's _check_all_other_landmarks(), parametrized
        per source instead of a single module-level checklist.
        """
        for var in influence_column_vars.get(source_key, {}).values():
            var.set(True)
        _update_run_button_state()

    def _uncheck_all_source(source_key):
        """Mirror of _check_all_source() above -- sets every column's
        BooleanVar to False for exactly one source."""
        for var in influence_column_vars.get(source_key, {}).values():
            var.set(False)
        _update_run_button_state()

    def _resize_influence_source_checklist_box(source_key):
        """
        Recomputes ONE source's own checklist Canvas height/width
        handling to fit its current content -- the exact same
        item-count-threshold + width-pinning logic as
        POI_All_Distance.py's _resize_other_landmarks_checklist_box()
        (the Prompt's designated reference implementation for this
        behavior), parametrized here per source_key so it can be
        called once for each of potentially several simultaneously-
        visible source blocks, rather than operating on one fixed
        module-level canvas.

        Vertical scrollbar trigger: item count > 8 (matching POI's own
        threshold), with the per-row pixel height derived from this
        source's own current content (content_height / n_items) so the
        cap translates into an accurate pixel height regardless of
        font/theme.

        Horizontal overflow: this source's canvas width is explicitly
        pinned to a FIXED value on every call -- derived from
        infl_action_row's own already-established requested width,
        minus both the vertical scrollbar's width (whenever shown) and
        INFLUENCE_CHECKLIST_LEFT_INDENT -- never left to grow to match
        wide column-name content. A column name wider than this pinned
        width triggers the horizontal scrollbar instead of ever
        widening the canvas (and therefore win) itself -- same
        principle, same reasoning as POI's own reference implementation.

        Returns this block's own TOTAL height in pixels (header row +
        canvas + spacing), computed in pure Python -- see the "PURE-
        PYTHON block-height tracking" comment near the end of this
        function's body for why this is now returned (and stored in
        widgets["block_total_height"]) instead of leaving the caller to
        later query a live Tk geometry measurement on a deeper,
        multi-level nested container.
        """
        widgets = influence_checklist_widgets.get(source_key)
        if not widgets:
            return 0
        canvas = widgets["canvas"]
        container = widgets["container"]
        vscroll = widgets["vscroll"]
        hscroll = widgets["hscroll"]
        window_id = widgets["window_id"]
        header_row = widgets.get("header_row")

        container.update_idletasks()
        n_items = len(influence_column_vars.get(source_key, {}))
        content_height = container.winfo_reqheight()
        content_width = container.winfo_reqwidth()

        show_vscroll = n_items > INFLUENCE_CHECKLIST_MAX_ITEMS_BEFORE_VSCROLL and n_items > 0
        vscroll_width = vscroll.winfo_reqwidth() if show_vscroll else 0
        # OUTER (main) Influence checklist scrollbar clearance: derived
        # directly from infl_sources_content_width (nonlocal, the
        # SINGLE authoritative value _resize_infl_sources_outer()
        # establishes -- see that variable's own module-level comment).
        # A PREVIOUS version of this line independently recomputed the
        # same "does the outer scrollbar need clearance" logic here, a
        # second time, from infl_action_row's raw width minus its own
        # separate infl_sources_vscroll.winfo_ismapped() check -- two
        # parallel calculations of the identical quantity, which is
        # exactly the kind of accumulating/diverging-margins risk that
        # can leave checklist content overlapping the outer scrollbar
        # even when each individual calculation looks reasonable in
        # isolation. Falling back to the old infl_action_row-based
        # estimate only when infl_sources_content_width isn't yet
        # established (0) -- the brief window before
        # _resize_infl_sources_outer() has run for the CURRENT rebuild,
        # during the very first per-block sizing pass inside
        # _build_influence_source_block(); _rebuild_influence_checklist()'s
        # guaranteed second pass re-calls this function once the
        # authoritative value is known, correcting any block sized
        # during that brief window.
        if infl_sources_content_width > 0:
            fixed_row_width = infl_sources_content_width
        else:
            fixed_row_width = infl_action_row.winfo_reqwidth()
            if infl_sources_vscroll.winfo_ismapped():
                fixed_row_width -= infl_sources_vscroll.winfo_reqwidth()
        # Nested blocks (one layer's checklist inside a multi-layer
        # group's layers_subframe) sit under an EXTRA
        # INFLUENCE_LAYER_GROUP_INDENT*2 of padding beyond a flat
        # block's own -- layers_subframe applies its own
        # padx=(INFLUENCE_LAYER_GROUP_INDENT*2, 0) on top of this
        # block's own `outer` padx below, so `outer`'s actual available
        # width is that much narrower than a flat block's. Without this
        # subtraction, a nested block's canvas was configured to the
        # SAME width as a flat block's -- wider than its real available
        # space -- leaving no room for its own vertical scrollbar to
        # ever render, a confirmed (not hypothesized) bug found via
        # live pixel measurement during design review.
        if widgets.get("nested"):
            fixed_row_width -= INFLUENCE_LAYER_GROUP_INDENT * 2
        canvas_width = max(
            fixed_row_width - vscroll_width - INFLUENCE_CHECKLIST_LEFT_INDENT
            - INFLUENCE_CHECKLIST_RIGHT_MARGIN, 1)
        canvas.configure(width=canvas_width)

        if n_items <= INFLUENCE_CHECKLIST_MAX_ITEMS_BEFORE_VSCROLL or n_items == 0:
            final_canvas_height = content_height
            canvas.configure(height=content_height)
            vscroll.pack_forget()
        else:
            row_height = content_height / n_items
            capped_height = int(round(row_height * INFLUENCE_CHECKLIST_MAX_ITEMS_BEFORE_VSCROLL))
            final_canvas_height = capped_height
            canvas.configure(height=capped_height)
            vscroll.pack(side="right", fill="y")

        if content_width > canvas_width:
            canvas.itemconfig(window_id, width=content_width)
            hscroll.pack(side="bottom", fill="x")
        else:
            canvas.itemconfig(window_id, width=canvas_width)
            hscroll.pack_forget()

        # PURE-PYTHON block-height tracking (see this function's own
        # docstring update above, and _resize_infl_sources_outer()'s own
        # docstring, for the full "why" -- avoids ever asking Tk for the
        # combined required height of a deep, multi-level nested widget
        # tree, which live testing in this sandbox could not fault but
        # on-machine Windows testing showed producing an incorrect outer
        # scrollbar decision). header_row is a single, SHALLOW widget
        # (a Frame with two Labels) -- measuring ITS OWN reqheight
        # directly is a much lower-risk live query than measuring a
        # whole nested container tree's propagated height, and is the
        # only live measurement left in this height computation; the
        # canvas height itself is never queried -- it's the exact value
        # WE just set above, already fully known in Python. hscroll's
        # own height, when shown, is small and deliberately not added
        # here (a few extra pixels of slack in the outer estimate is
        # harmless; the real risk this whole fix addresses is
        # under-estimating, never over-estimating).
        header_height = header_row.winfo_reqheight() if header_row is not None else 0
        block_total_height = header_height + 4 + final_canvas_height + 2
        widgets["block_total_height"] = block_total_height
        return block_total_height

    def _build_influence_source_block(source_key, info, parent=None, nested=False):
        """
        Builds ONE source's own header (display name + per-source
        "Check All" | "Uncheck All" links) and its own scrollable
        checklist (Canvas + vertical/horizontal Scrollbar), fully
        analogous in mechanics to POI_All_Distance.py's single
        module-level "Other Landmark Types" block -- but instantiated
        HERE, once per discovered source, since (unlike POI) this
        feature supports several simultaneously-selected sources that
        each need their own independent block. This factory-per-source
        shape has no direct precedent in either road_width.py (which
        has exactly one flat per-source-name checkbox list, no
        per-source SUB-checklist) or POI_All_Distance.py (exactly one
        checklist, for a single-selection source) -- it combines
        road_width.py's per-source iteration shape with POI's
        per-block Canvas/Scrollbar/resize mechanics.

        parent: the Tkinter Frame this block is packed into --
        defaults to infl_sources_container (top-level, exactly as
        before the multi-layer feature existed) when not given. For one
        LAYER's block within a multi-layer .gpkg's group,
        _rebuild_influence_checklist() passes the group's own nested
        sub-frame instead, so the layer's block is visually indented
        under its file's group header. source_key here is always
        already the full (source_type, path_or_table, layer) 3-tuple --
        this function itself doesn't need to know or care whether it's
        being built standalone or as one layer within a group; every
        mechanism below (Check All/Uncheck All, the checklist's own
        Canvas/Scrollbar, hover preview) operates purely off source_key
        as an opaque dict key, unchanged either way.

        nested: True when this block sits inside a multi-layer group's
        layers_subframe (which applies its OWN INFLUENCE_CHECKLIST_LEFT_INDENT
        padding on top of this block's own `outer` padding below) --
        stored in the widgets dict so
        _resize_influence_source_checklist_box() can subtract that
        EXTRA indent from its canvas-width calculation. Without this,
        a nested block's canvas was being configured to the SAME width
        as a flat (non-nested) block's, which is wider than the actual
        space available inside its doubly-indented `outer` frame --
        leaving no room for its own vertical scrollbar to render at
        all (a concrete, reproducible bug, not a hypothesis -- see that
        function's own updated comment for the exact pixel measurements
        that confirmed it).
        """
        if parent is None:
            parent = infl_sources_container
        source_type, path_or_table, layer = source_key
        columns = info["columns"]
        display_name = info["display_name"]

        block = tk.Frame(parent)
        block.pack(fill="x", pady=(4, 0))

        header_row = tk.Frame(block)
        header_row.pack(fill="x")
        tk.Label(header_row, text=display_name, font=("Segoe UI", 8, "bold"),
                 anchor="w").pack(side="left")

        links_frame = tk.Frame(header_row)
        links_frame.pack(side="right", padx=(0, 6))
        check_all_link = tk.Label(
            links_frame, text="Check All", fg="#1a73e8", cursor="hand2",
            font=("Segoe UI", 8, "underline"))
        check_all_link.pack(side="left")
        check_all_link.bind(
            "<Button-1>", lambda e, sk=source_key: _check_all_source(sk))
        tk.Label(links_frame, text=" | ", fg="gray",
                 font=("Segoe UI", 8)).pack(side="left")
        uncheck_all_link = tk.Label(
            links_frame, text="Uncheck All", fg="#1a73e8", cursor="hand2",
            font=("Segoe UI", 8, "underline"))
        uncheck_all_link.pack(side="left")
        uncheck_all_link.bind(
            "<Button-1>", lambda e, sk=source_key: _uncheck_all_source(sk))

        outer = tk.Frame(block)
        outer.pack(fill="x", padx=(INFLUENCE_CHECKLIST_LEFT_INDENT, 0), pady=(0, 2))
        canvas = tk.Canvas(outer, highlightthickness=0, bd=0)
        vscroll = tk.Scrollbar(outer, orient="vertical", command=canvas.yview, width=9)
        hscroll = tk.Scrollbar(outer, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        # Both scrollbars start unpacked; _resize_influence_source_checklist_box()
        # below decides what to show, once content is actually known.

        container = tk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=container, anchor="nw")

        def _on_content_configure(_e=None, canvas=canvas):
            canvas.configure(scrollregion=canvas.bbox("all"))
        container.bind("<Configure>", _on_content_configure)

        # Same smooth-scroll fix as the outer sources wrapper above --
        # small, explicit yscrollincrement so each wheel notch moves a
        # small, consistent pixel distance instead of Tk's default
        # coarse "units" jump.
        canvas.configure(yscrollincrement=4)

        def _on_mousewheel(event, canvas=canvas):
            canvas.yview_scroll(int(-1 * (event.delta / 120)) * 3, "units")
        canvas.bind("<Enter>", lambda e, c=canvas, h=_on_mousewheel:
                    c.bind_all("<MouseWheel>", h))
        canvas.bind("<Leave>", lambda e, c=canvas: c.unbind_all("<MouseWheel>"))

        influence_checklist_widgets[source_key] = {
            "outer": outer, "canvas": canvas, "vscroll": vscroll,
            "hscroll": hscroll, "container": container, "window_id": window_id,
            "header_row": header_row, "nested": nested,
        }

        vars_for_source = {}
        for col in sorted(columns.keys()):
            var = tk.BooleanVar(master=win, value=False)
            # No pre-checked column, ever (per the task's explicit
            # requirement) -- value=False above is the only state a
            # freshly-built checkbox can start in.
            var.trace_add("write", lambda *_a: _update_run_button_state())
            vars_for_source[col] = var

            row = tk.Frame(container)
            row.pack(fill="x", anchor="w")
            tk.Checkbutton(row, text=col, variable=var).pack(side="left")
            icon = tk.Label(row, text=" ℹ️", cursor="hand2",
                             font=("Segoe UI", 8))
            icon.pack(side="left")
            preview_values = columns[col]
            icon.bind("<Enter>", lambda e, sk=source_key, c=col, vals=preview_values, w=icon:
                      _show_influence_preview(w, sk, c, vals))
            icon.bind("<Leave>", lambda e: _hide_influence_preview())
            icon.bind("<Button-1>", lambda e, sk=source_key, c=col:
                      _toggle_influence_preview(sk, c))

        influence_column_vars[source_key] = vars_for_source
        return _resize_influence_source_checklist_box(source_key)

    def _rebuild_influence_checklist(discovery_results):
        """
        Plain destroy-and-repopulate of EVERY per-source checklist
        block, from a fresh discovery result -- never merged with any
        previous checklist state, matching the established no-cache
        convention (same principle as POI's
        _rebuild_other_landmarks_checklist() and road_width.py's
        _rebuild_lot_classification_checklist()/_rebuild_road_type_checklist()).

        discovery_results: {(source_type, path_or_table, layer):
        {"display_name", "columns"}} -- layer is None for every
        single-layer source (.shp, single-layer .gpkg, DB table); a
        real layer-name string only for one specific layer of a
        genuinely multi-layer .gpkg (2+ layers). Entries sharing the
        same (source_type, path_or_table) are grouped here, then --
        for a group with 2+ entries only -- filtered by the approved
        name-matching rule (_generate_matching_variants(), applied
        symmetrically to the filename and every layer name) before
        deciding how to display them:
          - A lone entry (genuinely single-layer source): flat, exactly
            as before this feature existed. The matching filter never
            applies here.
          - A multi-layer .gpkg with exactly ONE matched layer: flat,
            no "Layers:" grouping -- the display header is overridden
            to the filename (matching every other single-layer
            source's visual convention), but output-column naming
            still uses that layer's own real name, unchanged.
          - A multi-layer .gpkg with 2+ matched layers: nested, with a
            "Layers:" indicator under the filename, one block per
            MATCHED layer only (non-matching layers are excluded
            entirely -- never shown, never read, never selectable).
          - A multi-layer .gpkg with ZERO matched layers: fallback --
            nested, "Layers:" shown, EVERY layer displayed (since
            nothing matched well enough to narrow the choice).
        A source present in discovery_results with an EMPTY "columns"
        dict is simply omitted here -- not shown with an empty/
        placeholder block -- matching road_width.py's own documented
        convention for its per-source Land Parcel checklist ("Sources
        without a usable column are omitted entirely -- not shown with
        a 'not found' line"). This is a per-SOURCE omission only: if
        some other selected source in this same discovery_results DOES
        have eligible columns, this still produces a non-empty, usable
        checklist overall. The stricter "the WHOLE discovery failed"
        case -- every selected source combined has zero eligible
        columns -- is handled one level up, in
        _poll_influence_discovery(), before this function is ever
        called for that outcome (see _handle_influence_discovery_failure()).
        """
        nonlocal influence_column_vars, influence_checklist_widgets
        nonlocal influence_preview_expanded
        _hide_influence_preview()
        for child in infl_sources_container.winfo_children():
            child.destroy()
        influence_column_vars = {}
        influence_checklist_widgets = {}
        influence_preview_expanded = {}

        # Group by (source_type, path_or_table) -- ignoring layer --
        # so a genuinely multi-layer .gpkg's several (source_type,
        # path_or_table, layer) entries can be told apart from a
        # single-layer source's lone entry. Per _list_gpkg_layers()'s
        # own contract, a group ever having more than one member is
        # only possible for a .gpkg with 2+ real layers -- every
        # single-layer source (.shp, single-layer .gpkg, DB table)
        # always produces exactly one entry per file/table, so it is
        # guaranteed to take the flat, unchanged code path below with
        # zero special-casing needed for that (overwhelmingly common)
        # case.
        groups = {}
        for source_key, info in discovery_results.items():
            if not info["columns"]:
                continue
            file_key = (source_key[0], source_key[1])
            groups.setdefault(file_key, []).append((source_key, info))

        # total_content_height: pure-Python running total of every
        # top-level item's own height, accumulated here as each block/
        # group is built -- passed directly to _resize_infl_sources_outer()
        # below instead of leaving it to query a live, deep, multi-level
        # Tk geometry measurement on infl_sources_container itself (see
        # that function's own docstring for the full "why").
        total_content_height = 0

        for file_key, entries in groups.items():
            source_type, path_or_table = file_key
            group_display_name = (
                os.path.basename(path_or_table) if source_type == "local"
                else path_or_table
            )

            if len(entries) == 1:
                # Genuinely single-layer source (the common case) --
                # built exactly as before this feature existed: flat,
                # directly inside infl_sources_container, no group
                # header, using that entry's own display_name (the
                # filename, or the DB table name -- never a layer
                # name, since layer is always None here per
                # _list_gpkg_layers()'s [None] contract for this
                # case). The name-matching filter below NEVER applies
                # here -- it only ever engages for a .gpkg with 2+
                # real layers to begin with.
                source_key, info = entries[0]
                total_content_height += _build_influence_source_block(source_key, info)
                continue

            # Multi-layer .gpkg (2+ real layers, each with at least one
            # eligible column) -- apply the approved name-matching
            # filter BEFORE deciding flat vs. nested display, per
            # design review (validated with ChatGPT). Only layers whose
            # name "matches" the file's own name -- via
            # _generate_matching_variants(), the SAME function applied
            # symmetrically to both the filename stem and every layer
            # name, no special-casing per side -- are considered to
            # "belong" to this file. A layer that merely happens to
            # also mention a similar word as a substring (e.g.
            # "gem_active_faults_harmonized" alongside a file literally
            # named "PH_FAULT_LINES") is excluded entirely if it
            # doesn't actually match via the approved variant rule --
            # never shown, never read, never selectable for this file.
            filename_stem = os.path.splitext(os.path.basename(path_or_table))[0]
            filename_variants = _generate_matching_variants(filename_stem)
            matched_entries = [
                (sk, inf) for sk, inf in entries
                if _generate_matching_variants(sk[2]) & filename_variants
            ]

            if len(matched_entries) == 1:
                # Exactly ONE layer matched -- FLAT, no "Layers:"
                # grouping at all (per approved design: "1 matched
                # layer -> FLAT"), even though the underlying .gpkg
                # has 2+ total layers. Column naming still uses the
                # LAYER's own name (info["display_name"] is already
                # that layer name, and _influence_source_display_stem()
                # always uses the layer name whenever layer is not
                # None, regardless of how this block is DISPLAYED) --
                # only the visible HEADER for this flat block is
                # overridden here to the filename, matching the visual
                # convention of every other single-layer source in
                # this checklist (a display-only change, via a shallow
                # dict copy -- info["columns"] and everything else
                # about the entry is untouched, so naming/processing
                # downstream is completely unaffected).
                matched_key, matched_info = matched_entries[0]
                flat_info = dict(matched_info)
                flat_info["display_name"] = group_display_name
                total_content_height += _build_influence_source_block(matched_key, flat_info)
                continue

            # Either 2+ layers matched, or ZERO matched (the approved
            # fallback: show EVERY layer, nested, when nothing matched
            # the filename at all) -- both cases render NESTED with the
            # "Layers:" indicator. display_entries is the matched
            # subset for the 2+ case, or every original entry for the
            # 0-matched fallback case.
            display_entries = matched_entries if matched_entries else entries

            # Filename GROUP header first (bold, no Check All/Uncheck
            # All of its own -- those stay per-LAYER, inside each
            # layer's own block below, exactly like every other
            # source's block), then the "Layers:" indicator (small,
            # non-bold, gray -- shown ONLY here, in the nested case;
            # never shown for the single-matched-layer flat case
            # above, per approved design), then one block PER
            # (filtered) LAYER, nested in an indented sub-frame so it
            # reads visually as "Filename -> Layers: -> layername ->
            # columns". Indentation uses INFLUENCE_LAYER_GROUP_INDENT
            # ("3 spaces", measured -- see that constant's own
            # definition above) for this group-nesting hierarchy
            # specifically -- one level for "Layers:", two levels
            # (twice the indent) for layers_subframe, matching the
            # user's explicitly approved ASCII-art format exactly.
            group_frame = tk.Frame(infl_sources_container)
            group_frame.pack(fill="x", pady=(4, 0))
            group_header_label = tk.Label(
                group_frame, text=group_display_name,
                font=("Segoe UI", 8, "bold"), anchor="w")
            group_header_label.pack(anchor="w")
            layers_indicator_label = tk.Label(
                group_frame, text="Layers:", font=("Segoe UI", 8), fg="gray", anchor="w")
            layers_indicator_label.pack(
                anchor="w", padx=(INFLUENCE_LAYER_GROUP_INDENT, 0))
            layers_subframe = tk.Frame(group_frame)
            layers_subframe.pack(fill="x", padx=(INFLUENCE_LAYER_GROUP_INDENT * 2, 0))

            # group_header_label and layers_indicator_label are both
            # single, SHALLOW widgets (plain Labels, not a nested tree)
            # -- measuring their own reqheight directly here is the
            # same lower-risk category of live query already used for
            # header_row in _resize_influence_source_checklist_box(),
            # not the kind of deep-nested measurement this whole fix
            # moves away from.
            group_header_label.update_idletasks()
            layers_indicator_label.update_idletasks()
            total_content_height += (
                4 + group_header_label.winfo_reqheight()
                + layers_indicator_label.winfo_reqheight())

            # Sorted case-insensitively (same fix, same reasoning as
            # _refresh_influence_columns()'s own layer-listing sort and
            # run_processing().worker()'s sources_with_columns sort --
            # raw string comparison would put all-uppercase layer names
            # before any lowercase one regardless of actual letter
            # order, confirmed as a real bug via live testing) -- so
            # the order the user SEES here matches the order layers
            # were actually discovered/read in.
            display_entries.sort(key=lambda pair: pair[1]["display_name"].lower())
            for source_key, info in display_entries:
                total_content_height += _build_influence_source_block(
                    source_key, info, parent=layers_subframe, nested=True)

        _resize_infl_sources_outer(known_content_height=total_content_height)

        # Re-resize every already-built block now that the outer
        # scrollbar's own final visibility is known (see
        # _resize_influence_source_checklist_box()'s own comment on
        # why this second pass is required) -- width-only correction,
        # does not affect any block's height or re-trigger the outer
        # cap decision above.
        for source_key in influence_checklist_widgets:
            _resize_influence_source_checklist_box(source_key)

    def _set_influence_reading_state(reading):
        """
        Disables infl_btn and BOTH infl_radio_local/infl_radio_db while
        the background column-discovery read is in progress --
        verified (see infl_radio_row construction above) that this
        section has exactly one action button and exactly two radio
        buttons, so disabling all three is sufficient to make a second,
        overlapping discovery structurally impossible; matches
        road_width.py's verified _set_parcel_reading_state()/
        _set_road_reading_state() pattern exactly.

        Reuses the EXISTING "N file(s) selected"/"N table(s) selected"
        label (infl_lbl, bound to infl_files_var/infl_db_label) via a
        temporary text swap -- no new widget, per the task's explicit
        instruction -- restoring it from the authority lists
        (influence_local_paths/influence_db_tables) once done.

        Does NOT itself touch influence_is_reading -- that flag is
        owned and reset directly by _refresh_influence_columns()/
        _poll_influence_discovery()/_handle_influence_discovery_failure(),
        exactly matching the verified, explicitly-documented convention
        in road_width.py's own _set_parcel_reading_state() (its
        docstring states plainly that it "only manages widget state,"
        never the reading flag itself) -- this function does the same
        here, deliberately.
        """
        state = "disabled" if reading else "normal"
        infl_btn.config(state=state)
        infl_radio_local.config(state=state)
        infl_radio_db.config(state=state)

        if reading:
            infl_files_var.set("Reading Influence Map(s)...")
            infl_db_label.set("Reading Influence Map(s)...")
            infl_lbl.config(fg="#b36b00")
        else:
            if influence_source_type.get() == "local":
                infl_files_var.set(
                    f"{len(influence_local_paths)} file(s) selected" if influence_local_paths
                    else "No file(s) selected"
                )
            else:
                infl_db_label.set(
                    f"{len(influence_db_tables)} table(s) selected" if influence_db_tables
                    else "No table(s) selected"
                )
            infl_lbl.config(fg="gray")
        _update_run_button_state()

    def _handle_influence_discovery_failure(source_type, reason, detail=None):
        """
        Shared cleanup for all three ways a discovery for the CURRENT
        selection set can fail to produce a usable checklist: a read
        that never completed within 60 seconds ("timeout"), one that
        completed but a source's own read raised an error ("failure"),
        or one that completed successfully for every source but found
        ZERO eligible columns combined across the whole set ("empty") --
        the task requires this third case be treated identically to a
        genuine failure.

        Clears the ENTIRE current selection for source_type (not just
        whichever individual source triggered the failure) and reverts
        the status label to "No file(s) selected"/"No table(s)
        selected" -- per the task's explicit "discard the entire
        in-progress discovery result for that selection set" / "revert
        to No file(s) selected, restore controls, allow immediate
        retry" requirement. This is broader than road_width.py's own
        single-selection failure handlers (which only ever had one
        source to clear) -- extended here to the whole list since
        Influence Map retains multi-selection.

        _set_influence_reading_state(False) is called BEFORE the
        dialog is shown, not after -- messagebox.showerror() is modal
        and blocks here until dismissed; showing it first would leave
        the "Reading Influence Map(s)..." indicator frozen on screen
        for the entire time the dialog is up (same reasoning, same
        ordering, as the verified road_width.py
        _handle_parcel_check_failure()/_handle_road_check_failure()).
        """
        nonlocal influence_is_reading
        influence_is_reading = False

        if source_type == "local":
            influence_local_paths.clear()
        else:
            influence_db_tables.clear()

        _rebuild_influence_checklist({})

        if reason == "timeout":
            title = "Read Timeout"
            message = ("Could not read the selected Influence Map source(s) "
                       "within 60 seconds.\n\nPlease try again or choose a "
                       "different selection.")
        elif reason == "empty":
            title = "No Usable Columns Found"
            message = ("The selected Influence Map source(s) have no column "
                       "with at least one non-null value to copy.\n\n"
                       "Please choose a different file or table.")
        else:  # "failure"
            title = "Read Error"
            message = "Could not read the selected Influence Map source(s)."
            if detail:
                message += f"\n\n{detail}"
            message += "\n\nPlease try again or choose a different selection."

        _set_influence_reading_state(False)
        _reflow_window()
        messagebox.showerror(title, message, parent=win)

    def _poll_influence_discovery(result_queue, source_type, deadline):
        """
        Runs on the main thread via win.after() polling. Ordering
        matters: the queue is ALWAYS checked before the deadline --
        verified pattern from road_width.py's
        _poll_parcel_classification_queue()/_poll_road_classification_queue()
        (single-threaded Tkinter main loop, fresh queue.Queue() per
        call). No generation/request-id counter is used or needed:
        _set_influence_reading_state(True) disables the only action
        button AND both radio buttons for this section (verified --
        see that function's own docstring), so a second, overlapping
        discovery for a different selection cannot be started while
        this one is still in flight.
        """
        nonlocal influence_is_reading
        if not win.winfo_exists():
            return
        try:
            payload = result_queue.get_nowait()
        except queue.Empty:
            if time.time() >= deadline:
                _handle_influence_discovery_failure(source_type, "timeout")
            else:
                win.after(100, lambda: _poll_influence_discovery(
                    result_queue, source_type, deadline))
            return

        influence_is_reading = False

        if "error" in payload:
            _handle_influence_discovery_failure(source_type, "failure", payload["error"])
            return

        results = payload["results"]
        total_eligible_columns = sum(len(v["columns"]) for v in results.values())
        if total_eligible_columns == 0:
            _handle_influence_discovery_failure(source_type, "empty")
            return

        _set_influence_reading_state(False)
        _rebuild_influence_checklist(results)
        _reflow_window()
        _update_run_button_state()

    def _refresh_influence_columns():
        """
        Background-reads EVERY currently selected Influence Map source
        (not just the first) to discover eligible columns + preview
        values for the per-source checklist. Deliberately does NOT
        cache across calls -- every call, whether triggered by a fresh
        Browse/Select or by toggling Local <-> Database, performs a
        real read, matching this codebase's established no-cache
        detect-on-select convention.

        60-second deadline applies to the WHOLE discovery operation for
        the CURRENT selection set (see _poll_influence_discovery()) --
        not per individual source; the checklist is only considered
        ready once EVERY selected source has finished reading
        successfully. All sources are read sequentially on ONE
        background thread, so a hard failure on any single source
        aborts the whole worker() call with a single error payload --
        there is no partial per-source result ever placed on the
        queue, which is what guarantees _poll_influence_discovery()
        can never apply a partial checklist.
        """
        nonlocal influence_is_reading
        if influence_is_reading:
            return

        if influence_source_type.get() == "local":
            source_type = "local"
            sources = list(influence_local_paths)
        else:
            source_type = "db"
            sources = list(influence_db_tables)

        if not sources:
            _rebuild_influence_checklist({})
            _reflow_window()
            _update_run_button_state()
            return

        result_queue = queue.Queue()

        def worker():
            engine = None
            schema = None
            if source_type == "db":
                creds = load_db_credentials()
                if not creds:
                    result_queue.put({"error": "Could not load DB credentials."})
                    return
                schema = creds["schema"]
                engine = create_engine(
                    f"postgresql://{creds['username']}:{creds['password']}@"
                    f"{creds['host']}:{creds['port']}/{creds['database']}"
                )

            results = {}
            try:
                for path_or_table in sources:
                    # _list_gpkg_layers() returns [None] for .shp/DB
                    # (a single implicit layer -- completely unchanged
                    # read behavior from before this feature existed)
                    # or the real list of layer names for a .gpkg.
                    # Sorted case-insensitively (alphabetically, the way
                    # a human reads it -- NOT raw ASCII string order,
                    # which would sort all-uppercase names before any
                    # lowercase one regardless of actual letter order,
                    # confirmed as a real bug via live testing) for a
                    # deterministic display order -- never relying on
                    # fiona's arbitrary file-internal layer order,
                    # matching the same "never depend on return order"
                    # principle already applied to canonical source
                    # identity.
                    layers = (
                        sorted(_list_gpkg_layers(path_or_table),
                               key=lambda l: (l is None, l.lower() if l is not None else ""))
                        if source_type == "local" else [None]
                    )
                    for layer in layers:
                        key = (source_type, path_or_table, layer)
                        eligible, error = _read_influence_source_columns_worker(
                            source_type, path_or_table, engine, schema, layer=layer)
                        if error is not None:
                            label = path_or_table if layer is None else f"{path_or_table} ({layer})"
                            result_queue.put(
                                {"error": f'Could not read "{label}": {error}'})
                            return
                        results[key] = {
                            "display_name": (
                                layer if layer is not None
                                else (os.path.basename(path_or_table) if source_type == "local"
                                      else path_or_table)
                            ),
                            "columns": eligible,
                        }
            except Exception as e:
                # Covers _list_gpkg_layers()'s own possible ValueError
                # (a malformed/zero-layer .gpkg) -- same "whole
                # selection set fails" treatment as any other read
                # error above, not a silent partial result.
                result_queue.put({"error": f'Could not read "{path_or_table}": {e}'})
                return
            result_queue.put({"results": results})

        deadline = time.time() + 60  # see _poll_influence_discovery()
        influence_is_reading = True
        _set_influence_reading_state(True)
        _update_run_button_state()
        threading.Thread(target=worker, daemon=True).start()
        win.after(100, lambda: _poll_influence_discovery(result_queue, source_type, deadline))

    def browse_influence_files():
        files = filedialog.askopenfilenames(
            title="Select Influence Map file(s)",
            filetypes=VECTOR_FILETYPES)
        if files:
            influence_local_paths.clear()
            influence_local_paths.extend(files)
            # Always checks fresh -- see _refresh_influence_columns()
            # docstring: no result is ever cached across calls. This
            # call also handles updating infl_files_var/Run-button
            # state (via _set_influence_reading_state()/
            # _poll_influence_discovery()) -- no separate call needed
            # here, unlike the previous version.
            _refresh_influence_columns()

    def _on_influence_db_selected(sel):
        influence_db_tables.clear()
        influence_db_tables.extend(sel)
        _refresh_influence_columns()

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
        # Always checks fresh -- re-discovers whichever mode's already-
        # selected sources (if any) every time Local <-> Database is
        # toggled, matching road_width.py's toggle_road()/toggle_parcel()
        # convention of always re-triggering their own detect-on-select
        # reads on toggle.
        _refresh_influence_columns()

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
        global selected_influence_columns

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
        # UPDATE (per-source checklist feature): three new guards vs.
        # the previous version -- (1) can't Run while a discovery read
        # is still in progress (mirrors _update_run_button_state()'s
        # own gate, defensive here in case Run was somehow triggered
        # anyway, e.g. via a stale keyboard shortcut), (2) at least one
        # column must actually be CHECKED across every selected source
        # -- non-empty influence_local_paths/influence_db_tables alone
        # is no longer sufficient, since selecting a source no longer
        # implies any column will be copied from it, (3) the checked
        # selections are resolved into their final, collision-safe
        # output-column names HERE, once, via
        # _resolve_influence_column_names() -- stored in the
        # module-level selected_influence_columns global that both the
        # PRIORITY 1 check just below and run_processing().worker()
        # later consume, so both always agree on exactly the same
        # final names for exactly the same run.
        if influence_is_reading:
            messagebox.showerror("Please Wait",
                "Still reading the selected Influence Map source(s). "
                "Please wait for that to finish.")
            return

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

        checked_items = [
            (source_type, path_or_table, layer, raw_column)
            for (source_type, path_or_table, layer), col_vars in influence_column_vars.items()
            for raw_column, var in col_vars.items()
            if var.get()
        ]
        if not checked_items:
            messagebox.showerror("Missing Input",
                "Please select a column to be copied.")
            return

        selected_influence_columns = _resolve_influence_column_names(checked_items)

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
        # output columns are dynamic -- the target list is built from
        # the checked source-column selections resolved above into
        # selected_influence_columns. Extended (Fix 3) to cover both
        # Local and Database Land Parcel/Barangay sources -- previously
        # LOCAL-only (see _check_parcel_influence_conflicts()'s own
        # docstring). Shown once, combined across every affected source,
        # only here at Run time. Declining cancels the run entirely --
        # nothing is processed, including sources that had no conflict.
        #
        # UPDATE (per-source checklist feature): previously this block
        # RE-READ every selected Influence Map source at Run time (via
        # the now-removed _get_added_fields_for_check() helper) purely
        # to rebuild this target-name list. That re-read is no longer
        # needed at all -- the resolved final_column names above are
        # already exactly what run_processing().worker() will write,
        # with no re-derivation possible to drift out of sync.
        #
        # Unlike POI_All_Distance.py, this tool saves ONE output per
        # source (never merges), so the standard per-source override
        # map applies here -- exact detected casing is preserved and
        # written back into, same canonical road_width.py pattern used
        # by every other per-source tool in this project.
        # ------------------------------------------------------------------
        global parcel_output_column_overrides
        targets_for_check = sorted({
            entry["final_column"] for entry in selected_influence_columns
        })

        if targets_for_check:
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
        has_checked_column = any(
            var.get()
            for col_vars in influence_column_vars.values()
            for var in col_vars.values()
        )

        if not has_parcel:
            run_status_var.set("Please select a Land Parcel source.")
            ready = False
        elif not has_influence:
            run_status_var.set("Please select an Influence Map source.")
            ready = False
        elif influence_is_reading:
            # Per the task's explicit requirement: Run stays disabled
            # for the ENTIRE duration of the background column-
            # discovery read, reusing infl_files_var's own "Reading
            # Influence Map(s)..." text (set by
            # _set_influence_reading_state()) as this status line too,
            # so the two indicators never say different things at once.
            run_status_var.set("Reading Influence Map(s)...")
            ready = False
        elif not has_checked_column:
            # Exact wording required by the task -- shown whenever at
            # least one Influence Map source is selected and its
            # discovery read (if any) has finished, but nothing has
            # been checked in any source's checklist yet.
            run_status_var.set("Please select a column to be copied.")
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