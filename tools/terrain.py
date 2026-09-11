"""
tools/terrain.py

PURPOSE:
    CAMA Tools tool ("PARCEL TERRAIN LEVEL" in MAIN.py's dispatch
    table): for each Land Parcel, computes six terrain-related outputs
    from a Digital Terrain Model (DTM) raster and the nearest Road
    Network segment: CAMA_SLOPE, CAMA_TERRAIN (FLAT/SLOPING
    classification), CAMA_PRCL_ELEV, CAMA_ROAD_ELEV, CAMA_PRCL_ROAD
    (elevation difference), and CAMA_TOPO_LVL (a street-level
    classification derived from that difference).

DISPATCH:
    Run as an isolated subprocess by MAIN.py via its `--tool` dispatch
    mechanism (see system context). Entry point is main(), triggered via
    the `if __name__ == "__main__":` guard at the bottom of this file.

INPUTS:
    Land Parcel source: one or more local files or PostGIS tables.
    Road Network source: a single local file or PostGIS table.
    DTM source: a single local GeoTIFF (.tif) file, or a single PostGIS
    raster table (read via rasterio's PostGIS driver, "PG:...").
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
    stdlib: os, re, math (imported twice -- see note below), json,
    threading, queue, time, ctypes, sys, tkinter.
    third-party: pyproj, geopandas, rasterio (+ rasterio.warp), shapely
    (geometry + strtree), numpy, psycopg2, sqlalchemy, scipy.ndimage.
    local: utils.table_name_matching, utils.resource_path,
    utils.db_discovery, utils.column_detection, utils.window_icon,
    utils.progress_framework (imported mid-file, directly above the
    class/function that uses it -- see the Progress Event Protocol v9
    comment block further below).

    NOTE: `import math` appears twice in this file's import block (once
    as part of the protected PROJ_LIB/PROJ_DATA sequence below, once
    further down among the regrouped stdlib imports). This is a
    harmless, literal duplicate -- Python's import system is idempotent,
    a second `import math` is a no-op. Left as-is; not a deduplication
    task (see Section 3.E.7 of the governing instructions).

SIDE EFFECTS:
    File reads/writes (.shp/.gpkg/.tif). PostGIS reads/writes, including
    reading a PostGIS raster table directly via rasterio's "PG:" driver.
    A live PostgreSQL connection. Tkinter GUI windows throughout,
    including a background thread + queue.Queue-based polling loop for
    the main processing run. A subprocess launch to Global Mapper
    (load_in_global_mapper()) on local-output saves, plus a Win32
    EnumWindows call to find/focus an already-open Global Mapper window
    first.

    IMPORTANT -- this module has TWO distinct kinds of import-time
    behavior, and they should not be conflated:

    1. A REAL, REQUIRED initialization dependency, not a stylistic
       preference: `os.environ["PROJ_DATA"]` (right at the very top of
       this file, before any third-party import) MUST be set before
       `import rasterio` (and before `import geopandas`, which also
       depends on GDAL/PROJ) -- GDAL/rasterio reads this env var at
       import time to locate PROJ's data files.

       CONFIRMED ROOT CAUSE (direct inspection, both dev-mode venv and
       the PyInstaller-frozen build bundle four SEPARATE, independently
       -versioned copies of PROJ's proj.db -- one each from rasterio,
       pyproj, pyogrio, and fiona, since each of those packages' wheels
       bundles its own): their DATABASE.LAYOUT.VERSION schemas differ.
       rasterio's OWN bundled copy is the one whose schema actually
       matches this rasterio build's compiled GDAL requirement; the
       other three packages' copies are older and will trigger
       "DATABASE.LAYOUT.VERSION.MINOR = N whereas a number >= 6 is
       expected" if rasterio ends up pointed at any of them instead of
       its own. This is NOT a "point PROJ_DATA at pyproj" fix (an
       earlier, disproven diagnosis) -- it is specifically "point
       PROJ_DATA at rasterio's own bundled proj_data directory",
       resolved via `importlib.util.find_spec("rasterio")` (locates the
       package without importing/initializing it, so the required
       set-env-var-before-import ordering is preserved). This resolves
       correctly in both dev-mode (venv site-packages) and a frozen
       build (sys._MEIPASS), since find_spec() goes through whatever
       import machinery is active either way. A newer PROJ database
       schema is a superset of older ones for long-established EPSG
       definitions -- PRS92 zones (EPSG 3121-3125) have been in the
       PROJ database for well over a decade -- so using rasterio's copy
       for BOTH raster (rasterio.warp) and vector (geopandas/pyproj)
       CRS work in this file is safe; a second, different PROJ_DATA
       value is not needed for the two libraries. If rasterio's own
       proj_data directory cannot be located (unexpected on any machine
       where rasterio is actually importable), this falls back to
       pyproj's copy as a last resort, so the app degrades to
       previously-observed (imperfect but sometimes-working) behavior
       rather than leaving PROJ_DATA completely unset.

       This ordering is preserved exactly as found, including which
       imports precede vs. follow it -- not reorganized into the
       stdlib/third-party grouping used everywhere else in this file.

    2. An import-time SIDE EFFECT that is not itself an inter-import
       dependency: the module-level call to set_app_user_model_id()
       (see the "FORCE WINDOWS APP ICON" section below) invokes the
       Win32 SetCurrentProcessExplicitAppUserModelID API the moment
       this file is imported or run -- not lazily, not inside main().
       This affects how Windows groups/identifies this process's
       taskbar icon. Preserved exactly as found -- not moved, deferred,
       or wrapped in a function -- since doing so would change when
       this Windows-level identification happens, which is out of
       scope for a documentation/reorganization task (see Section C of
       the governing instructions: no behavior changes).

    KNOWN FOLLOW-UP (documented, not implemented here): unlike every
    other CAMA Tools tool file, this file's Global Mapper executable
    path is NOT a module-level constant -- it's a local variable
    (GM_EXE_PATH) defined and used entirely inside
    load_in_global_mapper() itself, currently hardcoded to
    "C:\\Program Files\\GlobalMapper26.1_64bit\\global_mapper.exe". The
    planned improvement is dynamic Global Mapper executable-path
    discovery instead of a hardcoded constant -- that discovery logic
    (search locations, missing-executable handling, installation-
    variant handling, fallback behavior) is a separate, deliberately-
    scoped future task, not implemented as part of this
    documentation/reorganization pass.
"""
import os
import re
import math
import importlib.util

# --- PROJ data-directory resolution (see module docstring above for the
# confirmed root cause) -- must run before `import rasterio` / `import
# geopandas` below. Locate rasterio's OWN bundled proj_data directory
# without importing rasterio itself (find_spec only resolves the
# package's location; it does not execute/initialize the module).
_rasterio_spec = importlib.util.find_spec("rasterio")
_proj_data_dir = None
if _rasterio_spec and _rasterio_spec.submodule_search_locations:
    _rasterio_pkg_dir = list(_rasterio_spec.submodule_search_locations)[0]
    _candidate = os.path.join(_rasterio_pkg_dir, "proj_data")
    if os.path.isdir(_candidate):
        _proj_data_dir = _candidate

if _proj_data_dir:
    os.environ["PROJ_DATA"] = _proj_data_dir
else:
    # Fallback: rasterio's own proj_data wasn't found -- fall back to
    # pyproj's copy so PROJ_DATA is still set to *something* rather than
    # left unset entirely (see module docstring for why this is a
    # degraded, imperfect-but-sometimes-working fallback, not the
    # primary fix).
    import pyproj
    os.environ["PROJ_DATA"] = pyproj.datadir.get_data_dir()

import tkinter as tk
from tkinter import filedialog, messagebox, Listbox
import math
import json
import threading
import queue
import time
import secrets  # D-Cancel: run-id/staging-table-name entropy, see the
                # "DB ATOMIC WRITE / CANCEL-SAFE STAGING" section below

import geopandas as gpd
import rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling
from shapely.geometry import LineString, MultiLineString, Point
from shapely.strtree import STRtree
import numpy as np
import psycopg2
from sqlalchemy import create_engine, inspect, text
from scipy.ndimage import sobel

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
# RUNTIME STATE
# ========================================
barangay_source = None
road_source = None
dtm_source = None
output_mode = None

# ========================================
# OUTPUT-COLUMN CONFLICT DETECTION
# ========================================
# OUTPUT_COLUMN_TARGETS: this tool's six output column names, checked
# for pre-existing conflicts in a selected LOCAL Land Parcel source (see
# _check_parcel_terrain_conflicts() below, and the combined dialog in
# on_run()). Mirrors road_frontage.py's OUTPUT_COLUMN_TARGETS exactly:
# ALL six are checked, not just one -- they are one feature set computed
# together in the same run, so a source with (for example) an existing
# CAMA_SLOPE column but no existing CAMA_TERRAIN column still needs a
# conflict warning, to avoid ending up with an old CAMA_TERRAIN value
# sitting alongside a freshly-computed CAMA_SLOPE from a DIFFERENT
# run/computation -- an inconsistent, misleading combination.
#
# Cross-tool CAMA_ prefix standard: every column this tool CREATES gets
# a "CAMA_" prefix -- matches road_width.py's own CAMA_ROAD_WIDTH
# convention. These targets check for the NEW, prefixed names ONLY --
# never the OLD, non-prefixed names (e.g. a plain "SLOPE" column left
# over from a pre-CAMA_-prefix version of this tool). This tool never
# auto-detects, auto-removes, or auto-overwrites an old, non-prefixed
# column -- if one exists, it is simply left alone, untouched, and a NEW
# CAMA_-prefixed column is created alongside it. Only conflicts against
# the NEW naming scheme are ever surfaced to the user.
#
# Matching is EXACT (case-insensitive) -- "CAMA_SLOPE" vs "SLOPE_PCT" is
# not a match; only "cama_slope"/"CAMA_SLOPE"/"Cama_Slope"/etc. (same
# letters, any casing) count as the same column.
OUTPUT_COLUMN_TARGETS = (
    "CAMA_SLOPE", "CAMA_TERRAIN", "CAMA_PRCL_ELEV",
    "CAMA_ROAD_ELEV", "CAMA_PRCL_ROAD", "CAMA_TOPO_LVL",
)

# parcel_output_column_overrides: {path_or_table: {"CAMA_SLOPE": name,
# ...}} -- for any Land Parcel source (Local file OR Database table)
# where one or more pre-existing CAMA_-prefixed output columns were
# detected (see _check_parcel_terrain_conflicts() below) and the user
# confirmed proceeding at Run time. Read by run_processing() and
# passed into process_parcels_fast() as the six *_col keyword
# arguments, so the tool writes back into the EXACT existing column(s)
# (preserving original casing) instead of always writing hardcoded
# "CAMA_*" names -- the latter would silently create confusing
# duplicate columns whenever an existing one used different casing. A
# source with no entry here (or a target missing from its entry) uses
# that target's default CAMA_ name.
parcel_output_column_overrides = {}


# ============================================================
# Progress Event Protocol v9 -- this tool's migration.
# ============================================================
# This tool previously had a minimal, ad-hoc progress mechanism
# (open_progress_window()/update_progress()/close_progress_window(),
# module globals, no actual progress bar -- just a status text label)
# and NO background worker thread -- run_processing() ran entirely
# synchronously on the main thread, using .update() calls to keep the
# window minimally repainted while blocking. That mechanism is fully
# replaced here by the same ProgressWindow shape used by the other five
# migrated tools, reusing progress_framework.py's
# PresentationState/ProgressPresentationPolicy/TkinterProgressView
# directly -- no tool-local copies, no new abstraction. Every call site
# that used to call update_progress(msg) directly (in
# compute_slope_array(), process_parcels_fast(), and run_processing()
# itself) now goes through an explicit `progress` parameter instead --
# see each function's own docstring/comment for the 1:1 message
# translation.
#
# Deliberately NOT done in the original v9 migration task:
#   - No per-source failure isolation added (this tool's existing
#     try/except in run_processing() already wraps the whole loop as
#     one unit -- that all-or-nothing behavior is preserved exactly).
#   - The 3 overwrite dialogs in this file are untouched.
#
# CANCEL SUPPORT (this rollout, following landmarks_within_meters.py's
# pilot and lot_location.py's first progress_framework.py-based tool):
# progress_framework.py itself required NO changes -- its cancel_flag/
# PresentationState.cancelable/new_cancel_flag() support was already
# complete and stable. What was actually missing was this file's own
# local integration: ProgressWindow.__init__ and ProgressWindow.update()
# (below) did not yet accept/forward cancel_flag/cancelable, and
# poll_queue()/the "update" queue-event producers in run_processing()
# (further below) had not yet been extended to the 5-field
# (message, value, maximum, cancelable) protocol. Both gaps are closed
# here -- the two claims immediately below ("no changes beyond passing
# the new optional cancelable argument through" applying to
# poll_queue(), and this class's own docstring's former "no cancel/
# stop_flag support" line) were true of the v9 migration alone and are
# corrected as part of this pass; see ProgressWindow's docstring,
# process_parcels_fast()'s docstring, and run_processing()/worker()'s
# docstrings further below for the concrete checkpoints added.
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
    thread. Same shape as the other migrated tools' ProgressWindow --
    status label + progress bar. Progress Event Protocol v9 role:
    ProgressWindow is the host, not the decision-maker (see
    ProgressPresentationPolicy / TkinterProgressView, imported from
    progress_framework.py, shared with lot_location.py/road_frontage.py/
    road_surface.py/land_shape.py/influence_map_distance_to_land_parcel.py/
    road_density.py/influence_map_to_land_parcel.py).

    Cancel support (this rollout, third tool after
    landmarks_within_meters.py's pilot and lot_location.py's first
    progress_framework.py-based adoption): an OPTIONAL cancel_flag,
    passed straight through to TkinterProgressView, wires the title-bar
    X as Cancel -- see __init__'s own docstring. A ProgressWindow
    constructed WITHOUT cancel_flag behaves exactly as it did before
    this rollout: no Cancel wiring at all. See run_processing() for
    where the cancel_flag actually comes from
    (utils.progress_framework.new_cancel_flag(), one per run) and for
    this tool's actual Cancel checkpoints -- a pre-processing check
    (immediately before process_parcels_fast() is called) plus that
    function's own final-loop checkpoint (mid-loop Cancel, full
    discard -- see process_parcels_fast()'s own progress-contract
    docstring). Cancel is disabled (cancelable=False) once a real,
    non-cancelled result exists, immediately before the save that
    follows.

    Indeterminate-mode support (this rollout, follow-up to the Cancel
    rollout): the single, static "Loading parcel {idx}/{total}..."
    message previously stayed on screen, with no progress-bar movement
    at all, for the entire duration of FOUR separate blocking steps in
    worker() (DB/file read, PRS92 zone detection, parcel reprojection,
    road reprojection) -- misleading, since none of those steps has a
    known item count to report a real X/Y against, unlike the final
    per-parcel loop. This is NOT handled via progress_framework.py (that
    module intentionally has no indeterminate-mode support -- see its
    own top-of-file comment on road_width.py's divergent, non-shared
    indeterminate implementation, and Rule of Three: this is only the
    second tool needing it, not yet enough evidence to add it there).
    Instead, set_indeterminate()/set_determinate() below toggle this
    class's own self.progress widget directly (mode="indeterminate" +
    .start(12), matching road_width.py's own animation speed constant,
    vs. mode="determinate" + .stop()) -- a tool-local addition, same
    spirit as road_width.py's own standalone ProgressWindow, not a
    change to the shared module. TkinterProgressView.render() (used by
    update(), below) never touches self.progress's mode -- it only sets
    ["value"]/["maximum"] when given, which has no effect while the bar
    is in indeterminate mode, so the two mechanisms don't conflict:
    set_indeterminate()/set_determinate() decide which mode the bar is
    in; update() continues to drive its determinate-mode value/maximum
    exactly as before once set_determinate() has been called for a
    given phase.
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
                preserves the exact pre-Cancel-support behavior: no
                Cancel wiring at all.
        """
        from tkinter import ttk
        self.win = tk.Toplevel(root)
        apply_icon(self.win, "terrain.ico")
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
        self._view = TkinterProgressView(self.win, self.status_var, self.progress,
                                          cancel_flag=cancel_flag)
        # Indeterminate-mode support (this rollout): tracks which mode
        # self.progress is currently in, purely so set_indeterminate()/
        # set_determinate() are safe to call repeatedly/redundantly
        # (e.g. two consecutive indeterminate phases in a row) without
        # calling .start() on an already-started bar or .stop() on an
        # already-stopped one -- both are harmless in Tkinter, but
        # tracking this keeps the intent explicit rather than relying
        # on that harmlessness.
        self._indeterminate = False

    def set_indeterminate(self, message):
        """
        Switches the bar to indeterminate ("bouncing") mode and updates
        the status text -- for a phase with no known item count to
        report a real value/maximum against (DB/file read, CRS zone
        detection, reprojection). Matches road_width.py's own
        indeterminate-mode animation speed (.start(12)). Does not touch
        cancelable/the title-bar X state -- callers that also need to
        change that should call update()'s cancelable argument
        separately, same as any other update() call.
        """
        if not self.win.winfo_exists():
            return
        self.status_var.set(message)
        if not self._indeterminate:
            self.progress.config(mode="indeterminate")
            self.progress.start(12)
            self._indeterminate = True
        self.win.update_idletasks()
        self.win.geometry("")
        self.win.update()

    def set_determinate(self, message=None, maximum=None):
        """
        Switches the bar back to determinate (normal X/Y) mode -- for
        re-entering a phase with a real, known total (the final
        per-parcel loop). message is optional so a caller that only
        wants the mode switch (letting the next update() call set the
        text) can pass None; maximum, if given, is set immediately
        (value resets to 0) so the bar doesn't briefly show a stale
        value/maximum left over from a previous determinate phase.
        """
        if not self.win.winfo_exists():
            return
        if self._indeterminate:
            self.progress.stop()
            self.progress.config(mode="determinate")
            self._indeterminate = False
        if maximum is not None:
            self.progress["maximum"] = maximum
            self.progress["value"] = 0
        if message is not None:
            self.status_var.set(message)
        self.win.update_idletasks()
        self.win.geometry("")
        self.win.update()

    def update(self, message, value=None, maximum=None, cancelable=None):
        """Updates the progress display via the shared
        ProgressPresentationPolicy/TkinterProgressView (see class
        docstring). cancelable is optional and, per
        progress_framework.py's own convention, None means "leave the
        title-bar X's enabled/disabled state as it currently is" --
        only has any effect on a window constructed WITH cancel_flag.
        Has no visible effect on the bar's position while
        set_indeterminate() has put it in indeterminate mode (Tkinter
        ignores ["value"]/["maximum"] writes in that mode) -- callers
        driving a real X/Y count should call set_determinate() first."""
        state = self._policy.compute(message, value, maximum, cancelable)
        self._view.render(state)

    def close(self):
        """Closes the progress window."""
        try:
            if self._indeterminate:
                self.progress.stop()
        except Exception:
            pass
        self._view.destroy()


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
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT f_geometry_column
            FROM geometry_columns
            WHERE f_table_schema = :schema AND f_table_name = :table
        """), {"schema": schema, "table": table_name}).fetchone()
        return result[0] if result else None


def get_columns_except_geom(table_name, engine, schema, geom_col):
    """Returns all column names for table_name except geom_col."""
    insp = inspect(engine)
    return [c['name'] for c in insp.get_columns(table_name, schema=schema) if c['name'] != geom_col]


def get_raster_srid(engine, schema, table):
    """Returns the SRID of the first raster tile found in a PostGIS
    raster table, via ST_SRID."""
    with engine.connect() as conn:
        return conn.execute(text(
            f'SELECT ST_SRID(rast) FROM "{schema}"."{table}" LIMIT 1'
        )).scalar()


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
    cols = get_columns_except_geom(table, engine, schema, geom_col)
    col_str = ", ".join([f'"{c}"' for c in cols]) if cols else ""
    query = f'SELECT {col_str + "," if col_str else ""}"{geom_col}" AS geometry FROM "{schema}"."{table}"'
    return gpd.read_postgis(query, engine, geom_col="geometry")


# ========================================
# CRS HELPERS
# ========================================
def detect_prs92_zone(labeled_gdfs):
    """
    Choose PRS92 zone EPSG from the combined bbox-midpoint longitude of
    one or more input GeoDataFrames.

    labeled_gdfs: list of (label, gdf) tuples, e.g.
        [("Land Parcel", parcels), ("Road Network", roads)]
    The label is used only for diagnostics. It has no effect on CRS
    detection.

    Auxiliary layers without usable geometry are ignored for CRS zone
    determination. Downstream processing may still validate required
    layers independently.

    Replaces the previous single-centroid, first-parcel-only version,
    which had a real off-by-one bug in its zone-boundary mapping (every
    threshold shifted one zone from the correct PRS92 EPSG codes) and
    used only feature 0's centroid rather than the dataset's extent.
    Uses total_bounds, not a unioned-geometry centroid -- unary_union.centroid
    is a known source of GEOS TopologyExceptions on real-world cadastral
    data with invalid geometries.
    """
    valid = [
        (label, g) for label, g in labeled_gdfs
        if g is not None and not g.empty and g.geometry.notna().any()
    ]
    if not valid:
        raise ValueError("No valid (non-empty) GeoDataFrames provided for PRS92 zone detection.")

    all_bounds = []
    for label, g in valid:
        if g.crs is None:
            g = g.set_crs(epsg=4326)
        epsg = g.crs.to_epsg()
        if epsg != 4326:
            g_wgs84 = g.to_crs(epsg=4326)
        else:
            g_wgs84 = g

        bounds = g_wgs84.total_bounds
        if any(math.isnan(v) for v in bounds):
            raise ValueError(
                f"Cannot determine PRS92 zone because the '{label}' layer "
                f"contains no valid geometry."
            )
        all_bounds.append(bounds)

    minx = min(b[0] for b in all_bounds)
    maxx = max(b[2] for b in all_bounds)
    lon = (minx + maxx) / 2
    if lon < 118: return 3121
    elif lon < 120: return 3122
    elif lon < 122: return 3123
    elif lon < 124: return 3124
    else: return 3125


def reproject_raster_to_prs92(dtm, target_epsg):
    """
    Reprojects an open rasterio dataset to the given EPSG code, entirely
    in memory (via rasterio.io.MemoryFile), band by band using bilinear
    resampling.

    Args:
        dtm: an open rasterio DatasetReader.
        target_epsg (int): the destination EPSG code.

    Returns:
        A new, open rasterio dataset (backed by the in-memory file) in
        the target CRS.
    """
    dst_crs = f"EPSG:{target_epsg}"
    transform, width, height = calculate_default_transform(
        dtm.crs, dst_crs, dtm.width, dtm.height, *dtm.bounds
    )
    kwargs = dtm.meta.copy()
    kwargs.update({"crs": dst_crs, "transform": transform, "width": width, "height": height})
    memfile = rasterio.io.MemoryFile()
    with memfile.open(**kwargs) as dst:
        for i in range(1, dtm.count + 1):
            reproject(
                source=rasterio.band(dtm, i),
                destination=rasterio.band(dst, i),
                src_transform=dtm.transform,
                src_crs=dtm.crs,
                dst_transform=transform,
                dst_crs=dst_crs,
                resampling=Resampling.bilinear
            )
    return memfile.open()


# ========================================
# FAST TERRAIN PROCESSING
# ========================================
def compute_slope_array(dtm, progress=None):
    """Compute slope in degrees for entire DTM using Sobel gradient.

    progress : optional callable progress(message, value=None, maximum=None),
    replacing the previous direct call to the old module-level
    update_progress(msg) -- see process_parcels_fast()'s docstring for
    the full migration rationale. Optional and defaults to None so this
    function's existing signature is unchanged for any call site that
    doesn't pass it.
    """
    if progress:
        progress("Precomputing slope from DTM...")
    arr = dtm.read(1)
    arr[arr == dtm.nodata] = np.nan
    xres, yres = dtm.res
    dzdx = sobel(arr, axis=1, mode="nearest") / (8 * xres)
    dzdy = sobel(arr, axis=0, mode="nearest") / (8 * yres)
    slope = np.degrees(np.arctan(np.sqrt(dzdx ** 2 + dzdy ** 2)))
    return slope


def get_raster_values_batch(raster, points):
    """Efficiently sample raster at multiple coordinates."""
    coords = [(p.x, p.y) for p in points]
    values = [v[0] if v[0] is not None else np.nan for v in raster.sample(coords)]
    return values


# ========================================
# PARCEL COLUMN-CONFLICT CHECK
# ========================================
# _check_parcel_terrain_conflicts(): checks the selected Land Parcel
# source -- Local file OR Database table (extended to cover both as
# part of Fix 3; previously LOCAL-only) -- for pre-existing columns
# matching any of OUTPUT_COLUMN_TARGETS -- this tool is about to write
# its six computed terrain columns into those columns, and on_run()
# below shows a combined confirmation dialog before proceeding,
# regardless of which source type was selected.
#
# Unlike road_frontage.py/road_width.py, this specific check
# (_check_parcel_terrain_conflicts()) is not part of the background
# worker thread run_processing() now uses (see run_processing()'s own
# Progress Event Protocol v9 migration comment further below) -- it
# still runs synchronously, called directly from on_run() right before
# Run actually starts, BEFORE the worker thread (and its ProgressWindow)
# are even created. Same adaptation already applied in road_density.py's
# _check_parcel_density_conflicts() and road_surface.py's
# _check_parcel_surface_conflicts(). Adding threading to THIS check
# specifically would be a separate, out-of-scope change.
#
# Read approach: plain gpd.read_file(path) for a Local source, matching
# road_width.py's own canonical _read_gdf_worker() exactly -- no
# partial/schema-only read trick. For a Database source,
# read_postgis_clean() is used instead, loading its own creds/schema/
# engine (self-contained, matching the pattern already used by
# on_run()'s PRIORITY 3 block).
#
# A read failure here is NEVER treated as a column-conflict failure --
# it only skips the conflict check for that one source (logged to
# console). The real read inside run_processing() further below remains
# solely responsible for surfacing any genuine read error to the user.
def _check_parcel_terrain_conflicts(sources, source_type):
    """
    Returns a list of (path_or_table, existing_output_cols) tuples on a
    SUCCESSFUL read/check -- one entry only for sources where at least
    one OUTPUT_COLUMN_TARGETS match was found; an empty list means the
    check succeeded and found no conflict. existing_output_cols is the
    dict returned by detect_existing_output_columns() for that source
    (target name -> actual existing column name, original casing
    preserved). Returns None if credentials could not be loaded, or if
    ANY source failed to read -- this is a REQUIRED distinction, not
    cosmetic: an empty list means "verified, no conflict", while None
    means "could not verify at all".

    source_type: "local" or "db" -- dispatches to gpd.read_file() or
    read_postgis_clean() respectively.
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
        existing_output_cols = detect_existing_output_columns(gdf, OUTPUT_COLUMN_TARGETS)
        if existing_output_cols:
            conflicts.append((path_or_table, existing_output_cols))
    return conflicts


def process_parcels_fast(parcels, roads, dtm, parcels_crs,
                          slope_col="CAMA_SLOPE", terrain_col="CAMA_TERRAIN",
                          prcl_elev_col="CAMA_PRCL_ELEV", road_elev_col="CAMA_ROAD_ELEV",
                          prcl_road_col="CAMA_PRCL_ROAD", topo_lvl_col="CAMA_TOPO_LVL",
                          progress=None):
    """
    slope_col, terrain_col, prcl_elev_col, road_elev_col, prcl_road_col,
    topo_lvl_col : str -- the column names this tool's six computed
        outputs are written to. Each defaults to its standard
        CAMA_-prefixed name (this tool's normal output, matching
        road_width.py's own ROAD_WIDTH -> CAMA_ROAD_WIDTH convention).
        The GUI overrides these per-source when the selected LOCAL
        parcel layer already has existing matching columns (see
        OUTPUT_COLUMN_TARGETS / _detect_existing_output_columns()) --
        the exact existing name/casing is passed here so processing
        writes back into that same column instead of creating a
        hardcoded CAMA_-prefixed duplicate.

    progress : optional callable progress(message, value=None, maximum=None).
    Replaces this function's previous direct calls to the old
    module-level update_progress(msg) (main-thread-only, unsafe to call
    from a background thread) -- the four existing stage messages below
    are a straight 1:1 translation (same text, same points in the
    function), plus new fine-grained value/maximum reporting inside the
    final per-parcel loop, which the old text-only update_progress()
    couldn't represent. Optional and defaults to None so this
    function's existing signature is unchanged for any call site that
    doesn't pass it -- added as part of this tool's Progress Event
    Protocol v9 migration (see run_processing() below).

    Return-value contract (mid-loop Cancel, added in this tool's Cancel-
    support rollout, matching the same decision already made and
    shipped for lot_location.py's process_lot_location()): ONLY the
    final per-parcel loop's own call site (below) checks the return
    value, and only stops on an EXPLICIT `False` -- any other return
    (None, True, or no return at all -- the shape every caller before
    this contract existed already produces) is treated as "continue,"
    so an old-style caller that never returns anything keeps working
    exactly as before. The four earlier call sites (road spatial index,
    compute_slope_array(), parcel-elevation sampling, nearest-road/
    road-elevation lookup) do not check the return value at all -- each
    is a single blocking operation (or, in compute_slope_array()'s
    case, delegates to its own single progress() call before its own
    non-loop array math) with no loop to break out of, so there is no
    meaningful place to stop mid-call; the final loop's own first
    checkpoint (the loop's very first iteration, i == 0, reported to
    the user as "1/total") is reached quickly enough after those phases
    that a Cancel clicked during them is still honored promptly,
    without needing a separate checkpoint of their own -- this bound is
    even tighter here than in process_lot_location(), since this loop's
    own progress() call has no i % 200 throttle (see the loop itself,
    below) and therefore checks on every single iteration.

    On an explicit-False mid-loop Cancel this function returns None
    instead of a GeoDataFrame -- the loop stops immediately, before the
    six per-parcel lists (diffs/slopes/terrains/topos, plus the
    already-fully-populated prcl_elevs/road_elevs) are assigned back as
    new columns, so the length-mismatch crash a truncated
    diffs/slopes/terrains/topos would otherwise cause
    (`ValueError: Length of values does not match length of index`) is
    never reached. There is no partial/truncated result to return or
    save -- see that column-assignment block, below.
    """
    # NOTE (Part A3 investigation, resolved as NOT needed): like
    # road_density.py, this function only reads parcels.geometry.centroid
    # from each parcel (line below) -- never buffers/intersects/unions
    # the parcel polygon itself. Road geometry is split into raw
    # LineString segments via direct coordinate-list slicing (no
    # buffer/union either) purely for STRtree nearest-neighbor lookup.
    # Centroid computation is already confirmed safe on invalid geometry
    # elsewhere in this project (no crash, unlike unary_union). No
    # fix_geometry() added.
    if progress:
        progress("Building road spatial index...")
    # Split roads into segments and index them
    segments = []
    for geom in roads.geometry:
        if geom.geom_type == "LineString":
            coords = list(geom.coords)
            segments += [LineString([coords[i], coords[i + 1]]) for i in range(len(coords) - 1)]
        elif geom.geom_type == "MultiLineString":
            for g in geom.geoms:
                coords = list(g.coords)
                segments += [LineString([coords[i], coords[i + 1]]) for i in range(len(coords) - 1)]

    tree = STRtree(segments)

    # Compute slope array
    slope_arr = compute_slope_array(dtm, progress=progress)
    band1 = dtm.read(1)
    transform = dtm.transform

    if progress:
        progress("Sampling parcel elevations...")
    centroids = parcels.geometry.centroid
    coords = [(p.x, p.y) for p in centroids]
    prcl_elevs = [v[0] for v in dtm.sample(coords)]

    if progress:
        progress("Finding nearest road and road elevations...")
    nearest_segments = []
    for p in centroids:
        res = tree.nearest(p)
        if isinstance(res, (int, np.integer)):
            nearest_segments.append(segments[int(res)])
        else:
            nearest_segments.append(res)

    road_points = [seg.interpolate(0.5, normalized=True) for seg in nearest_segments]
    road_elevs = [v[0] for v in dtm.sample([(p.x, p.y) for p in road_points])]

    diffs, slopes, terrains, topos = [], [], [], []

    total = len(centroids)
    for i, c in enumerate(centroids):
        if progress:
            # Mid-loop Cancel (full discard, per explicit decision --
            # matches lot_location.py's own full-discard behavior for
            # its classification loop). Explicit-False-only: only an
            # actual `False` return stops the loop -- None/True/no
            # return at all all mean "continue" (see this function's
            # own progress-contract docstring above). No i % 200
            # throttle here (unlike lot_location.py) -- this call
            # already fires on every iteration, so Cancel responsiveness
            # is already as tight as it can be without changing that
            # existing, unrelated behavior.
            if progress(f"Calculating slope and elevation differences: {i + 1}/{total}", i + 1, total) is False:
                return None
        elev = prcl_elevs[i]
        road = road_elevs[i]
        diff = elev - road if elev and road else None
        diffs.append(round(diff, 2) if diff else None)

        # Extract slope from slope raster using coordinates
        row, col = ~transform * (c.x, c.y)
        row, col = int(row), int(col)
        slope_val = (
            float(slope_arr[row, col]) if 0 <= row < slope_arr.shape[0] and 0 <= col < slope_arr.shape[1] else None
        )
        slopes.append(round(slope_val, 2) if slope_val else None)

        # Terrain classification
        if slope_val is None:
            terrains.append(None)
        elif slope_val < 3:
            terrains.append("FLAT")
        else:
            terrains.append("SLOPING")

        if diff is None:
            topos.append(None)
        elif abs(diff) < 0.01:
            topos.append("At Street Level")
        elif diff < 0:
            topos.append("Below Street Level 0.5m" if abs(diff) < 0.5 else "Below Street Level >= 0.5m")
        else:
            topos.append("Above Street Level" if diff < 0.5 else "Above Street Level >= 0.5m")

    parcels[slope_col] = slopes
    parcels[terrain_col] = terrains
    parcels[prcl_elev_col] = prcl_elevs
    parcels[road_elev_col] = road_elevs
    parcels[prcl_road_col] = diffs
    parcels[topo_lvl_col] = topos

    return parcels.to_crs(parcels_crs)


# NOTE: An earlier, unreachable run_processing() (no-argument signature)
# used to live here. It was permanently shadowed by the run_processing(app_root)
# definition further below (Python keeps only the last function bound to a
# given name at module level) and was never callable -- open_main_window()'s
# on_run() always called run_processing(root), which only matches the
# surviving definition's signature. Verified unreachable via: (1) no
# external references anywhere in the project (tools are dispatched as
# isolated subprocesses via importlib, never imported directly by name),
# (2) the only call site in this file passes one positional argument,
# matching only the surviving definition. Removed rather than left in
# place to avoid a future fix being silently applied to the dead copy
# instead of the live one.


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
    apply_icon(dialog, "terrain.ico")
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
    apply_icon(dialog, "terrain.ico")
    dialog.title("PARCEL TERRAIN LEVEL TOOL")
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
    apply_icon(dialog, "terrain.ico")
    dialog.title("PARCEL TERRAIN LEVEL TOOL")
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
    running instance can pick up the new file, then launches a local
    GM_EXE_PATH as a subprocess regardless of whether an existing
    window was found. Any failure is caught and only printed, never
    raised or shown to the user.

    Args:
        filepath (str): path to open in Global Mapper.

    Notes:
        Unlike every other CAMA Tools tool file, GM_EXE_PATH here is a
        local variable defined inside this function (not a module-level
        constant), currently hardcoded to a developer/machine-specific
        absolute path -- see the module docstring's SIDE EFFECTS note.
        Dynamic executable discovery is a planned, separately-scoped
        future improvement, not implemented here.
    """
    try:
        import ctypes.wintypes
        import subprocess
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

        GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"
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
    apply_icon(picker, "terrain.ico")
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
    Land Parcel, Road Network, and DTM source pickers (each with a
    Local-file/Database-table radio toggle), an Output destination
    picker, and a Run button gated by _update_run_button_state().

    Args:
        root: the parent Tk root this window is opened under.
    """
    from tkinter import ttk

    win = tk.Toplevel(root)
    apply_icon(win, "terrain.ico")
    win.title("Parcel Terrain Level Tool")
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
    dtm_source_type    = tk.StringVar(master=win, value="local")
    output_dest_type   = tk.StringVar(master=win, value="local")

    # Single-selection architecture: one local file and one DB table
    # may exist in memory at any time. Authority variables -- all GUI
    # labels and run-button state are derived from them, never the reverse.
    parcel_local_path = None   # authority: single local file path
    parcel_db_table   = None   # authority: single DB table name
    road_local_path    = tk.StringVar(master=win)
    road_db_table      = tk.StringVar(master=win)
    dtm_local_path     = tk.StringVar(master=win)
    dtm_db_table       = tk.StringVar(master=win)
    output_local_dir   = tk.StringVar(master=win)

    # Land Parcel existing-output-column check: detect-on-select,
    # matching the pattern established in lot_location.py/road_width.py/
    # road_frontage.py/road_density.py/road_surface.py/
    # influence_map_to_land_parcel.py/land_shape.py. Deliberately does
    # NOT cache the result across calls -- every selection AND every
    # Local/Database toggle triggers a fresh read (see
    # group-05-cache-removal-analysis.md). What IS still remembered per
    # mode is only WHICH file/table is selected (parcel_local_path /
    # parcel_db_table above), a separate concern. Multi-target (6
    # targets, OUTPUT_COLUMN_TARGETS): each conflict entry is
    # (path_or_table, {target: existing_col_name}), a dict.
    parcel_is_reading = False
    parcel_existing_output_conflicts = []   # [(path_or_table, {target: col}), ...]

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

    def _set_parcel_reading_state(is_reading):
        """
        Toggle GUI responsiveness while the Land Parcel existing-output-
        column check is in progress. Disables the parcel Browse/Select
        button and the Local/Database radio buttons for the duration of
        the read, preventing a second, concurrent read of the same
        selection.

        The "Reading..." indicator reuses the EXISTING label (parcel_lbl)
        in place -- via whichever StringVar is currently bound to it
        (parcel_files_var for Local, parcel_db_label for Database, per
        _toggle_parcel()'s textvariable swap below) -- rather than
        packing/unpacking a separate status widget, which would reflow
        every widget below it and cause a visible layout jump. Matches
        the corrected pattern already used by lot_location.py/
        road_width.py/road_frontage.py/road_density.py/road_surface.py/
        influence_map_to_land_parcel.py/land_shape.py.
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
        existing-output-column check: a read that never completed
        within 60 seconds ("timeout"), or one that completed with an
        actual read error ("failure" -- see
        _check_parcel_terrain_conflicts()'s docstring on why this is
        signaled as None, not an empty list).

        Captures the failed source's display name BEFORE clearing the
        authority variable (needed for the dialog text below), then
        clears ONLY the authority variable for source_type (the mode
        that was actually being read) -- parcel_local_path if source_type
        is "local", parcel_db_table if "db".

        Clearing the authority variable is the entire recovery
        mechanism -- no new "check failed" state is introduced. This
        forces the EXISTING "no source selected -> Run disabled" path
        (_update_run_button_state(), invoked via
        _set_parcel_reading_state(False) below) to handle recovery: the
        display reverts to "No file selected" / "No table selected",
        and the user must select a source again.

        _set_parcel_reading_state(False) is called BEFORE the dialog is
        shown, not after -- messagebox.showerror() is modal and blocks
        here until dismissed, so showing it first would leave the
        "⏳ Reading Land Parcel…" indicator frozen on screen for the
        entire time the dialog is up.
        """
        nonlocal parcel_local_path, parcel_db_table, parcel_existing_output_conflicts

        if source_type == "local":
            failed_name = (os.path.basename(parcel_local_path)
                           if parcel_local_path else "the selected file")
            parcel_local_path = None
        else:
            failed_name = parcel_db_table if parcel_db_table else "the selected table"
            parcel_db_table = None

        parcel_existing_output_conflicts = []

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

    def _poll_parcel_output_queue(result_queue, source_type, deadline):
        """
        Runs on the main thread via win.after() polling. Picks up the
        conflict list placed on the queue by the background worker, or
        detects a timeout if 60 seconds have elapsed with no result.

        Ordering matters: the queue is ALWAYS checked before the
        deadline -- see road_density.py's identical function for the
        full reasoning (single-threaded Tkinter main loop, fresh
        queue.Queue() per call, no generation counter needed).
        """
        nonlocal parcel_existing_output_conflicts
        if not win.winfo_exists():
            return
        try:
            conflicts = result_queue.get_nowait()
        except queue.Empty:
            if time.time() >= deadline:
                _handle_parcel_check_failure(source_type, "timeout")
            else:
                win.after(100, lambda: _poll_parcel_output_queue(
                    result_queue, source_type, deadline))
            return

        if conflicts is None:
            # Worker signaled a read failure (see
            # _check_parcel_terrain_conflicts()'s docstring) -- distinct
            # from an empty list, which means "verified, no conflict".
            _handle_parcel_check_failure(source_type, "failure")
            return

        parcel_existing_output_conflicts = conflicts
        _set_parcel_reading_state(False)

    def _refresh_parcel_output_check():
        """
        Background-checks the currently selected Land Parcel file/table
        for existing OUTPUT_COLUMN_TARGETS columns -- moved here from
        on_run() (Phase A of Group 5's detect-on-select generalization)
        so the check happens immediately on selection/toggle, not only
        when Run Processing is clicked. Reuses
        _check_parcel_terrain_conflicts() (defined above, already
        self-contained -- loads its own DB credentials internally) as
        the actual worker logic, just now called on a background thread.
        Gives up after 60 seconds with no result (see
        _poll_parcel_output_queue()) -- a hung read must not leave the
        tool waiting indefinitely.

        Deliberately does NOT cache the result across calls -- every
        call, whether triggered by a fresh Browse/Select or by toggling
        Local <-> Database, always performs a real read. See
        group-05-cache-removal-analysis.md for the full reasoning. What
        IS still remembered across calls is only WHICH file/table is
        selected per mode (parcel_local_path / parcel_db_table) -- a
        separate concern, untouched by this function.
        """
        nonlocal parcel_existing_output_conflicts
        if parcel_is_reading:
            # A check is already in flight — do not start a second,
            # overlapping one (controls are disabled while reading, but
            # this guard is the actual enforcement).
            return

        source_type = parcel_source_type.get()
        sources = (
            [parcel_local_path] if source_type == "local" and parcel_local_path
            else [parcel_db_table] if source_type == "db" and parcel_db_table
            else []
        )

        if not sources:
            # Nothing selected for this mode — nothing to check.
            parcel_existing_output_conflicts = []
            _update_run_button_state()
            return

        result_queue = queue.Queue()

        def worker():
            conflicts = _check_parcel_terrain_conflicts(sources, source_type)
            result_queue.put(conflicts)

        deadline = time.time() + 60  # see _poll_parcel_output_queue()
        _set_parcel_reading_state(True)
        threading.Thread(target=worker, daemon=True).start()
        win.after(100, lambda: _poll_parcel_output_queue(
            result_queue, source_type, deadline))

    def browse_parcel_files():
        file = filedialog.askopenfilename(filetypes=[
            ("Shapefiles", "*.shp"), ("GeoPackage", "*.gpkg"), ("All", "*.*")])
        # Cancel returns "" -- do not assign, preserving previous selection.
        if file:
            nonlocal parcel_local_path
            parcel_local_path = file
            parcel_files_var.set(os.path.basename(file))
            # Always checks fresh -- see _refresh_parcel_output_check()
            # docstring: no result is ever cached across calls.
            _refresh_parcel_output_check()
        _update_run_button_state()

    def _on_parcel_db_selected(sel):
        # Only called on confirmed selection -- Cancel never calls on_select,
        # so parcel_db_table retains its previous value automatically.
        nonlocal parcel_db_table
        parcel_db_table = sel[0]
        parcel_db_label.set(sel[0])
        _refresh_parcel_output_check()
        _update_run_button_state()

    def browse_parcel_db():
        creds = load_db_credentials()
        if not creds:
            return
        from sqlalchemy import create_engine as ce
        eng = ce(f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}")
        tables = inspect(eng).get_table_names(schema=creds["schema"])
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
        # untouched. Always re-checks fresh for whichever mode is now
        # active -- no cached result is ever restored (see
        # group-05-cache-removal-analysis.md).
        _refresh_parcel_output_check()
        _update_run_button_state()

    # ── SECTION 2: ROAD NETWORK ──────────────────────────────────
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
        # _pick_db_tables() only invokes on_select after a confirmed
        # selection, so sel is never empty here -- the original
        # lambda's "if sel else None" branch was a redundant
        # conditional. Switching to a named callback is a readability
        # change only; no behavior change.
        road_db_table.set(sel[0])
        road_db_var.set(sel[0])
        _update_run_button_state()

    def browse_road_db():
        creds = load_db_credentials()
        if not creds:
            return
        from sqlalchemy import create_engine as ce
        eng = ce(f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}")
        tables = inspect(eng).get_table_names(schema=creds["schema"])
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

    # ── SECTION 3: DTM ───────────────────────────────────────────
    section_label(win, "DTM Source")

    dtm_frame = tk.Frame(win)
    dtm_frame.pack(fill="x", padx=18, pady=2)

    dtm_radio_row = tk.Frame(dtm_frame)
    dtm_radio_row.pack(fill="x")
    tk.Radiobutton(dtm_radio_row, text="Local File (.tif)",
                   variable=dtm_source_type, value="local",
                   command=lambda: _toggle_dtm()).pack(side="left")
    tk.Radiobutton(dtm_radio_row, text="Database Table",
                   variable=dtm_source_type, value="db",
                   command=lambda: _toggle_dtm()).pack(side="left", padx=(12, 0))

    dtm_file_var = tk.StringVar(master=win, value="No file selected")
    dtm_db_var   = tk.StringVar(master=win, value="No table selected")

    dtm_action_row = tk.Frame(dtm_frame)
    dtm_action_row.pack(fill="x", pady=2)

    dtm_lbl = tk.Label(dtm_action_row, textvariable=dtm_file_var,
                       fg="gray", anchor="w", width=42)
    dtm_lbl.pack(side="left")

    dtm_btn = tk.Button(dtm_action_row, text="Browse…", width=10)
    dtm_btn.pack(side="left", **PAD)

    def browse_dtm_file():
        f = filedialog.askopenfilename(filetypes=[
            ("GeoTIFF", "*.tif"), ("All", "*.*")])
        if f:
            dtm_local_path.set(f)
            dtm_file_var.set(os.path.basename(f))
            _update_run_button_state()

    def _on_dtm_db_selected(sel):
        # Same note as _on_road_db_selected() above: _pick_db_tables()
        # only calls on_select with a confirmed, non-empty selection.
        dtm_db_table.set(sel[0])
        dtm_db_var.set(sel[0])
        _update_run_button_state()

    def browse_dtm_db():
        creds = load_db_credentials()
        if not creds:
            return
        from sqlalchemy import create_engine as ce
        eng = ce(f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}")
        tables = inspect(eng).get_table_names(schema=creds["schema"])
        if not tables:
            messagebox.showwarning("No Tables", "No tables found in the database schema.")
            return
        _pick_db_tables(win, tables, multi=False, on_select=_on_dtm_db_selected)

    def _toggle_dtm():
        if dtm_source_type.get() == "local":
            dtm_lbl.config(textvariable=dtm_file_var)
            dtm_btn.config(text="Browse…", command=browse_dtm_file)
        else:
            dtm_lbl.config(textvariable=dtm_db_var)
            dtm_btn.config(text="Select…", command=browse_dtm_db)
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
        Run button handler: validates Land Parcel + Road Network + DTM
        + Output selections are present, checks for existing output-
        column conflicts (PRIORITY 1), runs the local output-file
        conflict check (PRIORITY 2), and DB-output table resolution
        (PRIORITY 3) -- each able to cancel the whole run -- then
        destroys this window and hands off to run_processing(). Sets
        the module-level barangay_source, road_source, dtm_source,
        output_mode, and parcel_output_column_overrides globals on
        success.
        """
        global barangay_source, road_source, dtm_source, output_mode

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

        # validate dtm
        if dtm_source_type.get() == "local":
            if not dtm_local_path.get():
                messagebox.showerror("Missing Input",
                    "Please select a DTM file.")
                return
            dtm_source = ("local", dtm_local_path.get())
        else:
            if not dtm_db_table.get():
                messagebox.showerror("Missing Input",
                    "Please select a DTM table.")
                return
            dtm_source = ("db", dtm_db_table.get())

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
        # PRIORITY 1: column conflict check -- warn if the selected Land
        # Parcel source already has any of the 6 output columns
        # (CAMA_SLOPE, CAMA_TERRAIN, CAMA_PRCL_ELEV, CAMA_ROAD_ELEV,
        # CAMA_PRCL_ROAD, CAMA_TOPO_LVL). Shown before the file-conflict
        # dialog so the user can decide whether to proceed at all before
        # being asked about filename conflicts. Declining cancels the
        # run entirely; main window stays open (this block runs before
        # win.destroy() further below).
        #
        # Phase A (Group 5 detect-on-select generalization): this no
        # longer calls _check_parcel_terrain_conflicts() synchronously
        # here -- the check already ran in the background the moment the
        # Land Parcel source was selected/toggled (see
        # _refresh_parcel_output_check()). This just consults the
        # already-known result, parcel_existing_output_conflicts.
        # _update_run_button_state() already guarantees Run cannot be
        # reached while parcel_is_reading is True, so this value is
        # guaranteed current for the actively selected source at this
        # point.
        # ------------------------------------------------------------------
        global parcel_output_column_overrides
        conflicts = parcel_existing_output_conflicts
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
            # Preserve each source's existing column name(s)/casing
            # exactly -- e.g. a detected "caMA_SLOPE" is written back
            # to "caMA_SLOPE", not a hardcoded "CAMA_SLOPE" -- so no
            # duplicate column is ever created regardless of the
            # existing casing. A source with no entry here (no
            # conflict was found) simply uses the default names in
            # process_parcels_fast() below.
            parcel_output_column_overrides = dict(conflicts)
        else:
            parcel_output_column_overrides = {}

        # PRIORITY 2: file conflict check -- warn if an output file with
        # the same name already exists in the chosen output folder.
        # Root cause of bug fixed here: overwrite_mode was previously
        # local to on_run() and never reached run_processing(), causing
        # a NameError at runtime whenever a file conflict existed.
        # Fix: pass overwrite_mode explicitly as a parameter.
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
        # land_shape.py, and road_frontage.py.
        #
        # D-Cancel: resolved_outcome is NOW actually threaded through to
        # run_processing() -- previously (before this task) it was
        # computed here and immediately discarded (assigned to the
        # underscore-prefixed _resolved_outcome, never read anywhere).
        # _write_db_output_safely() needs it to know whether FINAL_SWAP
        # requires a rename-existing-aside-to-backup step. _resolve_creds
        # is also now passed into resolve_db_output_table() itself, for
        # its new D-Cancel orphan-recovery scan. Matches
        # landmarks_within_meters.py's/lot_location.py's/road_frontage.py's
        # own identical fix to this exact throwaway-outcome bug.
        # ------------------------------------------------------------------
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
        Land Parcel source, a Road Network source, a DTM source, and an
        Output destination are all present.

        The has_parcel / has_road / has_dtm / has_output cascade below
        intentionally mirrors on_run()'s own validation order further
        down -- this is a conscious duplication for a minimal-risk,
        additive gating layer, not a refactor of on_run() itself. Keep
        the two in sync if this tool's required inputs ever change.

        Explicit bg/fg/cursor toggling (not just state=) is required:
        Tkinter does NOT automatically gray out a classic tk.Button's
        custom bg/fg when state="disabled", and does not suppress a
        widget's assigned cursor either -- both must be set explicitly
        for each state.
        """
        has_parcel = bool(parcel_local_path) if parcel_source_type.get() == "local" else bool(parcel_db_table)
        has_road = bool(road_local_path.get()) if road_source_type.get() == "local" else bool(road_db_table.get())
        has_dtm = bool(dtm_local_path.get()) if dtm_source_type.get() == "local" else bool(dtm_db_table.get())
        has_output = bool(output_local_dir.get()) if output_dest_type.get() == "local" else True

        if parcel_is_reading:
            # Land Parcel existing-column check is still in flight --
            # never allow Run while its result is not yet known (see
            # Section 6's read-outcome invariant, group-05-FINAL-PLAN.md
            # -- an in-progress check must never be silently treated as
            # "no conflict").
            checking_name = (
                os.path.basename(parcel_local_path) if parcel_source_type.get() == "local"
                else parcel_db_table
            ) or "source"
            run_status_var.set(f'Checking "{checking_name}" columns…')
            ready = False
        elif not has_parcel:
            run_status_var.set("Please select a Land Parcel source.")
            ready = False
        elif not has_road:
            run_status_var.set("Please select a Road Network source.")
            ready = False
        elif not has_dtm:
            run_status_var.set("Please select a DTM source.")
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
    _toggle_road()
    _toggle_dtm()
    _toggle_output()
    _update_run_button_state()


# ========================================
# DB ATOMIC WRITE / CANCEL-SAFE STAGING  (D-Cancel)
# ========================================
# Ported from road_frontage.py's own D-Cancel section (itself ported
# from lot_location.py, itself ported from landmarks_within_meters.py,
# the pilot for this exact mechanism -- staging-table-then-atomic-
# rename-swap, plus a genuine Cancel control, plus crash/orphan recovery
# on a later run). This is now the FOURTH tool with this mechanism
# (landmarks_within_meters.py, lot_location.py, road_frontage.py, and
# this file) -- the function bodies in all three prior tools are
# confirmed byte-for-byte identical (verified by direct diff during this
# task's own Phase 1), so per Section C/G.5 (Rule of Three) this is a
# deliberate FOURTH duplication, not yet a shared utils/ module -- that
# decision remains for a future, separate task, not this one. Every
# function below is copied from road_frontage.py's own D-Cancel block
# with only the adaptations this file's own context requires:
# "terrain.ico" for the orphan-recovery dialog, and "geometry" as the
# default geometry column name (confirmed to match this file's own
# convention -- read_postgis_clean() always aliases the geometry column
# to "geometry" via its own `AS geometry` query, and no to_postgis()
# call in this file ever renames it away from that) -- no other design
# changes.
#
# Replaces the single `result.to_postgis(..., if_exists="replace")` call
# this file's worker() previously used (this exact call site was
# deliberately left unchanged and explicitly out of scope in terrain.py's
# own earlier Cancel-support task) with a staging-write / verify /
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
# THIS FILE'S CANCEL GRANULARITY: process_parcels_fast() has its own
# bool-return progress contract (see that function's own docstring),
# checked at its final per-parcel loop's own checkpoint -- explicit
# False only, full discard (returns None) on trigger, checked on EVERY
# iteration (no throttle exists in this loop, unlike the other three
# tools' own throttled checkpoints -- see that function's docstring for
# why this makes Cancel here even more responsive, not less safe). What's
# genuinely still true, and matches landmarks_within_meters.py/
# lot_location.py/road_frontage.py exactly: NOTHING from STAGING_WRITE
# through FINAL_SWAP and the post-swap backup drop -- everything below
# this comment block -- is itself cancelable, for the same reason in all
# four tools: gdf.to_postgis() (and the DB-only steps that follow it) is
# a single blocking library call with no hook a background thread could
# check a flag inside of, not a stricter design choice specific to this
# file. See run_processing()'s worker() for the actual Cancel checkpoint
# sequence (pre-processing check, then process_parcels_fast()'s own
# internal checkpoint, then disabled once a real result exists -- all
# already shipped in terrain.py's earlier, separate Cancel-support task
# and unchanged by this one).


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
    `result.to_postgis(..., if_exists="replace")` call this file's
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
    apply_icon(win, "terrain.ico")
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
    source, BEFORE the ProgressWindow is even opened -- all user
    interaction and overwrite decisions happen here, so the
    ProgressWindow that appears afterward genuinely means "processing
    has started," not "still waiting on a decision." Same "resolve
    everything up front" philosophy as ask_overwrite_dialog() (see
    run_processing()).

    Two cases:
      - DB-source Land Parcel (barangay_source[0] == "db"): always
        writes back to the exact same table it was read from -- no
        matching, no dialog. This corrects a confirmed regression in
        the PREVIOUS matching logic, which ran the same fuzzy
        `name.lower() in t.lower()` check regardless of source type,
        meaning even a DB-source run wasn't guaranteed to write back
        to its own source table if a differently-named table in the
        schema happened to substring-match. That behavior is
        intentionally NOT preserved here.
      - Local-file Land Parcel: fuzzy-matches the filename against
        existing tables via find_matching_tables() (which already
        excludes CAMA_Table, CAMA_Transaction_Log, and any "_VM"
        table), then requires user confirmation before treating a
        match as an overwrite target -- zero candidates skips the
        dialog entirely and creates a new table under the filename.

    D-Cancel: also runs the crash-orphan recovery scan first, before any
    of its own existing logic -- _scan_orphaned_cama_tables() plus, if
    it finds anything, _prompt_orphaned_cama_tables(). This is the
    natural point for it: it's the one place in this file that already
    runs on the main thread, synchronously, with a live `root` to open a
    dialog on, before any new staging table for THIS run could possibly
    exist to confuse the scan. creds is a new required parameter for
    this reason -- the caller (on_run()) already loads it to determine
    schema before calling this function, so this is a zero-cost addition
    at the call site. Matches landmarks_within_meters.py's/
    lot_location.py's/road_frontage.py's own identical addition to this
    same function.

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
    Orchestrates the full run on a background thread (worker(), started
    at the bottom of this function) with progress reported via a
    queue.Queue polled by poll_queue() on the main thread: loads the
    Road Network layer once, then for each selected Land Parcel
    file/table, detects the PRS92 zone, loads/reprojects the DTM (from
    a local .tif or a PostGIS raster table), runs
    process_parcels_fast(), and saves the result either locally (.gpkg,
    optionally opened in Global Mapper) or to PostGIS via
    _write_db_output_safely()'s staging/verify/swap sequence.

    Args:
        app_root: the live top-level window, used as the parent for the
        ProgressWindow and any dialogs.
        overwrite_mode (str | None): "overwrite" or "new", from
        ask_overwrite_dialog() in on_run() -- only relevant for local
        output mode.
        resolved_table_name (str | None): the already-confirmed DB
        output table name from resolve_db_output_table() in on_run() --
        only relevant for DB output mode.
        resolved_outcome (str | None): "created" or "overwritten" from
        the same source (D-Cancel: now actually threaded through, where
        it was previously computed in on_run() and discarded -- see
        on_run()'s own comment on this fix). Passed to
        _write_db_output_safely() so FINAL_SWAP knows whether it needs
        a rename-existing-aside-to-backup step.
    """
    # overwrite_mode: passed from on_run(). Root cause of original bug:
    # no parameter existed, so overwrite_mode was unbound inside this
    # function, causing a NameError whenever a file conflict existed.
    global barangay_source, road_source, dtm_source, output_mode
    creds = load_db_credentials()
    if not creds:
        return
    schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    )

    # resolved_table_name, resolved_outcome: the DB-output destination
    # table + outcome. Resolution responsibility now belongs to on_run()
    # (PRIORITY 3), on the main thread, BEFORE win.destroy() -- see Fix
    # 1. By the time they reach this function they are treated as
    # already-validated values: either None/None (local output, or
    # output_mode[0] != "db") or a confirmed table name + outcome (DB
    # output, user already had the chance to cancel in on_run()). No
    # re-resolution or re-validation happens here. resolved_outcome
    # specifically now matters downstream -- see _write_db_output_safely()
    # in worker().

    # ============================================================
    # Progress Event Protocol v9 -- this tool's migration.
    # ============================================================
    # Everything ABOVE this point (validation via load_db_credentials(),
    # resolve_db_output_table() + its confirmation dialog(s)) is
    # unchanged and stays on the main thread, exactly as before.
    #
    # Everything below is the exact same single loop this function
    # always had (NOT split into separate local/db branches -- the
    # local/db distinction happens via if/else INSIDE this one loop,
    # same as before), including the existing try/except that already
    # wrapped the whole thing as one all-or-nothing unit -- that
    # try/except is preserved here as worker()'s own, now routing
    # through the "error" event instead of calling messagebox directly
    # (which would be unsafe from a background thread). Every previous
    # update_progress(msg) call is now progress_cb(msg) instead -- same
    # message, same point in the flow, 1:1 translation.
    #
    # CANCEL SUPPORT (this rollout): one new_cancel_flag() is created
    # per run and passed to ProgressWindow, wiring the title-bar X as
    # Cancel for the lifetime of this run's progress dialog. This is an
    # addition on top of the v9 migration above, not a redesign of it --
    # the queue's "update" event grew a 5th (cancelable) field (see
    # progress_cb and poll_queue() below), and progress_cb now returns a
    # real bool every call so process_parcels_fast()'s own final-loop
    # checkpoint (see that function's docstring) can act on it. This
    # tool's existing all-or-nothing try/except (see comment above) is
    # untouched -- Cancel is handled entirely via the ("cancelled", ...)
    # event branch in poll_queue(), a `return` inside worker() (below),
    # and this same try/except's own `finally`-less fallthrough; no
    # per-source isolation, no DB staging/swap, no advisory lock.
    cancel_flag = new_cancel_flag()
    progress = ProgressWindow(app_root, "Terrain Progress", cancel_flag=cancel_flag)
    q = queue.Queue()

    def worker():
        """
        Background-thread body: loads the Road Network layer, then for
        each selected Land Parcel source detects the PRS92 zone, loads/
        reprojects the DTM, runs process_parcels_fast(), and saves the
        result (local .gpkg or PostGIS via _write_db_output_safely()'s
        staging/verify/swap sequence), posting progress/completion/
        error events onto q for poll_queue() to consume on the main
        thread. Never touches Tkinter widgets directly (all UI updates
        happen via progress_cb -> q, consumed by poll_queue()).

        Cancel support (this rollout): checks cancel_flag["stop"]
        directly once per source, immediately before the expensive
        process_parcels_fast() stage begins (a coarse checkpoint --
        does NOT make the loading/reprojection/DTM steps above it
        individually interruptible; a Cancel clicked during those is
        instead caught here, at the next opportunity, or at
        process_parcels_fast()'s own first per-parcel loop iteration --
        see that function's docstring), plus indirectly, via
        progress_cb's return value, at that function's own final-loop
        checkpoint. On Cancel (either checkpoint), this tool's existing
        all-or-nothing behavior is preserved: nothing is saved, no
        later source (there is normally only ever one) is attempted,
        and a single ("cancelled", ...) event is posted before this
        function returns.

        D-Cancel (this task): acquires the DB run lock (only for DB
        output mode) before the sources loop, releases it in a
        finally: block regardless of how this function exits (clean
        completion, Cancel, or an exception) -- see this section's own
        "DB ATOMIC WRITE / CANCEL-SAFE STAGING" header comment further
        above for the full rationale. Does not change any of the
        Cancel-checkpoint behavior described above -- the lock is
        acquired/released around that existing checkpoint sequence, not
        instead of it.
        """
        lock_conn = None
        try:
            def progress_cb(msg, val=None, maxv=None, cancelable=None):
                q.put(("update", msg, val, maxv, cancelable))
                # Cancel (mid-loop): a real bool every call, never
                # None -- process_parcels_fast()'s final loop only stops
                # on an explicit False (see that function's own
                # progress-contract docstring), so this must never
                # accidentally return None itself.
                return not cancel_flag["stop"]

            # D-Cancel: one run_id/run_started_at for the whole run --
            # cheap to always generate, even for local output mode,
            # which doesn't use it. The advisory lock itself is only
            # ever acquired for DB output mode, since it exists purely
            # to let a FUTURE run's _scan_orphaned_cama_tables() tell
            # this run's staging/backup artifacts apart from a
            # genuinely abandoned one. Matches landmarks_within_meters.py's/
            # lot_location.py's/road_frontage.py's own identical placement.
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

            q.put(("phase", "indeterminate", "Loading road data...", None))
            road_gdf = (
                gpd.read_file(road_source[1][0]) if road_source[0] == "local"
                else read_postgis_clean(road_source[1][0], engine, schema)
            )

            barangay_list = barangay_source[1]
            for idx, src in enumerate(barangay_list, 1):
                # Indeterminate-mode support (this rollout): the
                # previous single, static "Loading parcel {idx}/{total}..."
                # message stayed on screen -- with no bar movement, since
                # this is a determinate bar reporting 0 unknowns against
                # a total of len(barangay_list), a number that never
                # itself changes mid-phase -- for the ENTIRE duration of
                # all four blocking steps below (DB/file read, PRS92
                # zone detection, parcel reprojection, road
                # reprojection). Split into four distinct
                # ("phase", "indeterminate", ...) events instead, one
                # immediately before each step, so the status text
                # actually reflects which of the four is currently
                # running, and the bar visibly animates throughout
                # (none of the four has a known item count to report a
                # real value/maximum against -- see
                # ProgressWindow.set_indeterminate()'s own docstring).
                # This does NOT make any of the four steps themselves
                # individually interruptible or faster -- same
                # cooperative-cancellation-boundary blocking calls as
                # before (see the pre-processing Cancel checkpoint
                # further below); this is a status-text/bar-animation
                # accuracy fix only, not a behavior change to the
                # underlying reads/reprojections.
                q.put(("phase", "indeterminate",
                       f"Loading parcel {idx}/{len(barangay_list)} from "
                       + ("database..." if barangay_source[0] == "db" else "file..."),
                       None))
                if barangay_source[0] == "local":
                    parcels = gpd.read_file(src)
                    name = os.path.splitext(os.path.basename(src))[0]
                else:
                    parcels = read_postgis_clean(src, engine, schema)
                    name = src

                q.put(("phase", "indeterminate", "Detecting coordinate zone...", None))
                target_epsg = detect_prs92_zone([("Land Parcel", parcels), ("Road Network", road_gdf)])
                parcels_crs = parcels.crs

                q.put(("phase", "indeterminate", "Reprojecting parcel data...", None))
                parcels = parcels.to_crs(epsg=target_epsg)

                q.put(("phase", "indeterminate", "Reprojecting road network...", None))
                roads = road_gdf.to_crs(epsg=target_epsg)

                q.put(("phase", "indeterminate", "Loading DTM...", None))
                if dtm_source[0] == "local":
                    dtm_raw = rasterio.open(dtm_source[1])
                    dtm = reproject_raster_to_prs92(dtm_raw, target_epsg)
                else:
                    dtm_table = dtm_source[1]
                    dtm_raw = rasterio.open(
                        f"PG:dbname={creds['database']} host={creds['host']} "
                        f"user={creds['username']} password={creds['password']} "
                        f"schema={schema} table={dtm_table} column=rast"
                    )
                    srid = get_raster_srid(engine, schema, dtm_table)
                    dtm = reproject_raster_to_prs92(dtm_raw, target_epsg) \
                        if srid != target_epsg else dtm_raw

                # Preserves each source's existing output column name(s)/
                # casing exactly, if a conflict was detected and confirmed
                # in on_run() -- e.g. a detected "caMA_SLOPE" is written
                # back to "caMA_SLOPE", not a hardcoded "CAMA_SLOPE".
                # Defaults to the standard CAMA_-prefixed name for any
                # output this source has no override for. Extended
                # (Fix 3) to cover Database-sourced parcels too -- this
                # lookup already ran unconditionally for both source
                # types before Fix 3; only on_run()'s detection side was
                # LOCAL-only, so parcel_output_column_overrides was
                # always {} for a DB-sourced parcel until now.
                output_col_overrides = parcel_output_column_overrides.get(src, {})
                slope_col = output_col_overrides.get("CAMA_SLOPE", "CAMA_SLOPE")
                terrain_col = output_col_overrides.get("CAMA_TERRAIN", "CAMA_TERRAIN")
                prcl_elev_col = output_col_overrides.get("CAMA_PRCL_ELEV", "CAMA_PRCL_ELEV")
                road_elev_col = output_col_overrides.get("CAMA_ROAD_ELEV", "CAMA_ROAD_ELEV")
                prcl_road_col = output_col_overrides.get("CAMA_PRCL_ROAD", "CAMA_PRCL_ROAD")
                topo_lvl_col = output_col_overrides.get("CAMA_TOPO_LVL", "CAMA_TOPO_LVL")

                # Cancel (coarse, pre-processing checkpoint): checked
                # once per source, immediately before the expensive
                # process_parcels_fast() stage begins -- catches a
                # Cancel clicked during "Loading parcel...", "Loading
                # DTM...", or the CRS-detection/reprojection steps
                # above, before process_parcels_fast()'s own first
                # per-parcel loop iteration could catch it on its own.
                # This does NOT make those earlier steps themselves
                # individually interruptible -- they remain single,
                # cooperative-cancellation-boundary blocking calls, same
                # as before. All-or-nothing preserved: on Cancel here,
                # nothing is saved and no further source is attempted.
                if cancel_flag["stop"]:
                    q.put(("update", "Discarding run... Please wait.", None, None, False))
                    q.put(("cancelled", None, None, None))
                    return

                # Switch the bar back to determinate mode here -- the
                # four indeterminate phases above are now done, and
                # process_parcels_fast()'s final per-parcel loop (below)
                # DOES have a real, known total (len(parcels)) to report
                # a genuine X/Y progress value against. Sent as its own
                # "phase" event (not a plain progress_cb() "update") so
                # poll_queue() can call set_determinate() -- which also
                # resets the bar's value to 0 and its maximum to this
                # phase's real total -- before the very first
                # "Calculating slope and elevation differences: 1/total"
                # update arrives.
                q.put(("phase", "determinate", "Processing terrain data...", len(parcels)))
                progress_cb("Processing terrain data...", cancelable=True)
                result = process_parcels_fast(
                    parcels, roads, dtm, parcels_crs,
                    slope_col=slope_col, terrain_col=terrain_col,
                    prcl_elev_col=prcl_elev_col, road_elev_col=road_elev_col,
                    prcl_road_col=prcl_road_col, topo_lvl_col=topo_lvl_col,
                    progress=progress_cb,
                )

                if result is None:
                    # Cancel (mid-loop): progress_cb returned False at
                    # process_parcels_fast()'s own final-loop checkpoint
                    # -- full discard, per explicit decision. Nothing
                    # computed for this source so far is kept;
                    # output_mode[1]/the DB is never touched (the save
                    # step below is never reached). Same-tick "flash" of
                    # this message before the terminal "cancelled"
                    # message/dialog, per explicit decision -- no
                    # artificial delay, and no claim of guaranteed
                    # repaint timing beyond what TkinterProgressView's
                    # own render() (update_idletasks()/geometry("")/
                    # update() sequence) already does on every "update"
                    # event it's given.
                    q.put(("update", "Discarding run... Please wait.", None, None, False))
                    q.put(("cancelled", None, None, None))
                    return

                # From here through the save, Cancel is disabled -- a
                # real result now exists and nothing past this point
                # can be safely interrupted (the save itself is not
                # cancelable).
                progress_cb("Saving output...", cancelable=False)
                if output_mode[0] == "local":
                    desired_base_name = name
                    candidate_path = os.path.join(output_mode[1], f"{desired_base_name}.gpkg")
                    had_conflict = os.path.exists(candidate_path)
                    if had_conflict and overwrite_mode == "new":
                        base_name = resolve_output_base_name(output_mode[1], desired_base_name)
                    else:
                        base_name = desired_base_name
                    out = os.path.join(output_mode[1], f"{base_name}.gpkg")
                    _write_gpkg(result, out)
                    print(f"✅ Saved: {out}")
                    q.put(("open_gm", out, None, None))
                else:
                    # The actual destination table was already decided by
                    # resolve_db_output_table(), BEFORE the ProgressWindow
                    # was even opened -- fuzzy matching + user confirmation
                    # already happened there (see that function's
                    # docstring), and correctly bypasses matching entirely
                    # for DB-source parcels (writes back to the exact
                    # source table, unlike the old logic this replaces).
                    # Falls back to the old "name + _terrain" behavior only
                    # if resolved_table_name is somehow None here
                    # (output_mode[0] != "db" can't reach this branch, so
                    # this is just a defensive fallback).
                    out_table = resolved_table_name if resolved_table_name is not None else name + "_terrain"
                    # D-Cancel: staging-write / verify / atomic rename-
                    # swap, replacing the previous direct
                    # to_postgis(..., if_exists="replace") call -- see
                    # "DB ATOMIC WRITE / CANCEL-SAFE STAGING" above.
                    _write_db_output_safely(engine, schema, result,
                                             out_table, resolved_outcome,
                                             run_id, run_started_at)
                    print(f"🔄 Saved to DB: {out_table}")

                progress_cb(f"✅ Completed {name}")

            q.put(("done", "Terrain processing complete!", None, None))

        except Exception as e:
            q.put(("error", f"Processing failed:\n{str(e)}", None, None))
        finally:
            # D-Cancel: released here regardless of how the try block
            # above exits -- clean success, a Cancel (the `return`
            # statements at either checkpoint above), or an exception
            # (the `except` clause's own fallthrough). A lingering
            # advisory lock past this point would make a FUTURE run's
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

        Cancel support (this rollout): the "update" event grew a 5th
        field (cancelable, rest[3]) -- every producer in worker() above
        now emits it (either an explicit True/False or None, per
        progress_framework.py's own "None means leave it as-is"
        convention), so this consumer expects it unconditionally rather
        than defaulting it here. A new "cancelled" event kind is
        handled the same way as "done"/"error": closes the progress
        window, shows a terminal dialog, and stops polling.

        Backlog / terminal-event-starvation fix (this rollout):
        process_parcels_fast()'s final loop is deliberately unthrottled
        (fires progress() on every parcel, no i % 200 -- see that
        function's own docstring), so a large parcel set can enqueue
        thousands of "update" events before this poller gets a turn.
        Two different problems follow from that, and this function
        addresses both, deliberately NOT with a single flat per-tick
        item cap (a flat cap on its own would fix the first problem but
        reintroduce the second -- see below):

        1. Mainloop monopolization: draining every queued item with an
           unbounded `while True:` was previously harmless (nothing
           depended on the mainloop staying responsive mid-drain), but
           becomes a real problem now that a Cancel click needs the
           mainloop free to be processed. The fix here is NOT "drain
           fewer items" -- pulling a queued tuple off q with
           get_nowait() is cheap; what is actually expensive is calling
           progress.update() (each call does
           update_idletasks()/geometry("")/update()). So this loop
           drains the ENTIRE current backlog every tick (cheap), but
           coalesces consecutive "update" events into a SINGLE
           progress.update() call using only the latest one seen (see
           point 2 below for why this is safe) -- collapsing potentially
           thousands of renders into at most one keeps each tick fast
           regardless of backlog depth, which is what actually returns
           control to the Tk mainloop promptly.
        2. Terminal-event starvation: q is strict FIFO, so if the
           worker already queued thousands of "update" events before
           Cancel was clicked, a naive fixed-size-cap drain (e.g. only
           50 items/tick) would leave "cancelled" stuck behind roughly
           backlog_size / 50 ticks -- multiple seconds of apparent
           unresponsiveness even though the click itself registered.
           Since progress updates are pure display state (not
           transactional events -- superseded by whatever the next one
           says), it is safe to coalesce/skip stale intermediate
           "update" payloads entirely; only the LATEST one before a
           terminal/action event (or before q goes empty) is ever
           rendered. "open_gm"/"done"/"error"/"cancelled" are never
           coalesced or deferred -- each is handled the instant it is
           dequeued, whatever the current backlog looked like before it.
           Net effect: "cancelled" is reached in the same tick it was
           queued in, regardless of how many stale "update" events
           precede it, because reaching it only requires cheap
           get_nowait() calls, not a render per item.

        Phase-switch support (this rollout, follow-up to indeterminate-
        mode support above): a new "phase" event kind
        ("phase", "indeterminate"|"determinate", message, maximum) --
        distinct from "update" -- drives ProgressWindow.set_indeterminate()/
        set_determinate() directly. Handled the same way as
        "open_gm"/"done"/"error"/"cancelled": any pending coalesced
        "update" is flushed first (so the bar's last determinate/
        indeterminate reading before the switch is still shown, not
        skipped), then the phase switch itself is applied immediately
        -- never coalesced away, since each one represents a real,
        distinct stage transition, not a superseded display value.
        """
        if not app_root.winfo_exists():
            return
        try:
            pending_update = None
            while True:
                kind, *rest = q.get_nowait()
                if kind == "update":
                    # Coalesce: only the latest "update" payload seen
                    # before the next terminal/action event (or before q
                    # goes empty) is kept -- superseded ones are
                    # discarded unrendered. Safe because progress text/
                    # value/cancelable is display state, not a log --
                    # see docstring point 2.
                    pending_update = rest
                    continue
                # A non-"update" event follows: flush whatever the
                # latest pending "update" was (if any) BEFORE acting on
                # this event, so the displayed status text/bar never
                # goes stale relative to what's about to happen (e.g.
                # the "Discarding run..." update queued right before
                # "cancelled" is still shown, not skipped).
                if pending_update is not None:
                    progress.update(pending_update[0], pending_update[1], pending_update[2], pending_update[3])
                    pending_update = None
                if kind == "phase":
                    mode, message, maximum = rest
                    if mode == "indeterminate":
                        progress.set_indeterminate(message)
                    else:
                        progress.set_determinate(message, maximum)
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
            # q is empty for now -- render whatever the latest pending
            # "update" was (if any) so the dialog reflects the most
            # recent state, then wait for the next tick.
            if pending_update is not None:
                progress.update(pending_update[0], pending_update[1], pending_update[2], pending_update[3])
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
        apply_icon(root, "terrain.ico")
        root.withdraw()
        open_main_window(root)
        root.mainloop()


if __name__ == "__main__":
    main()