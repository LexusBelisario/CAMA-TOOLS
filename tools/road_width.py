root = None

import os
import re
import time
import tkinter as tk
from tkinter import ttk
from tkinter import filedialog, messagebox, Listbox
import geopandas as gpd
import pandas as pd
import numpy as np
from shapely.geometry import Point, LineString
from shapely.ops import unary_union, nearest_points
from shapely.validation import make_valid
import subprocess
import json
from sqlalchemy import create_engine, text, inspect
import psycopg2
import threading
import queue

# ----------------- CONFIG -----------------
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"
import sys as _sys

def _get_credentials_path():
    """
    Always resolve pg_credentials.json next to the EXE (frozen)
    or next to this script (dev). Never use CWD — it changes when
    a subprocess is spawned by PyInstaller.
    """
    if getattr(_sys, "frozen", False):
        # EXE: resolve next to the running executable
        return os.path.join(os.path.dirname(_sys.executable), "pg_credentials.json")
    else:
        # Dev: resolve relative to this file (tools/road_width.py -> parent)
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pg_credentials.json")

CREDENTIALS_FILE = _get_credentials_path()

def resource_path(relative_path):
    """PyInstaller-safe resource path. Ported from road_frontage.py."""
    try:
        base_path = _sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def apply_icon(win):
    """
    Sets both the Windows taskbar/Alt-Tab icon (.ico) and the Tk titlebar
    icon (.png fallback, needed since iconbitmap() alone doesn't reliably
    set the titlebar icon on every Windows/Tk combination) for a single
    window. Ported verbatim from road_frontage.py so the new
    ProgressWindow and show_success_dialog() in this file show the same
    BLGF logo as every other tool -- previously relied entirely on
    Toplevel windows inheriting the (permanently withdrawn) root's icon,
    which happens to work but isn't an explicit guarantee the way calling
    this directly is.
    """
    ico = resource_path("BLGF.ico")
    png = resource_path("BLGF.png")

    if os.path.exists(ico):
        try:
            win.iconbitmap(ico)
        except Exception:
            pass

    if os.path.exists(png):
        try:
            img = tk.PhotoImage(file=png)
            win.iconphoto(True, img)
        except Exception:
            pass


def _get_dialog_center_position(dialog_widget, req_w, req_h):
    """
    Returns (x, y) screen coordinates to center a dialog of the given
    size on.

    Tries the actual Global Mapper window first (found via the same
    EnumWindows technique as load_in_global_mapper() above) -- Global
    Mapper is the visible, foreground application the user is actually
    looking at when these dialogs appear (this tool's own selection
    windows are already destroyed by the time processing runs -- see
    on_run()'s win.destroy() before run_processing()).

    Falls back to centering on the physical screen if Global Mapper's
    window can't be found (not running, or the lookup fails for any
    reason) -- deliberately NOT on `root`'s own winfo_x()/winfo_y()/
    winfo_width()/winfo_height(), which was the previous approach and
    is exactly what caused a dialog to appear in an unexpected,
    seemingly-random position: `root` is permanently withdrawn (see
    main()) and its reported geometry is meaningless, not tied to
    anything visible on screen. winfo_screenwidth()/winfo_screenheight()
    on the other hand query the actual physical screen and are
    meaningful even from a withdrawn widget.

    dialog_widget: any already-created Tk widget (used only to query
    screen dimensions for the fallback case -- does not need to be
    mapped/visible itself).
    """
    try:
        import ctypes
        import ctypes.wintypes

        gm_hwnd = None

        def enum_callback(hwnd, _):
            nonlocal gm_hwnd
            if not ctypes.windll.user32.IsWindowVisible(hwnd):
                return True
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

        if gm_hwnd:
            rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(gm_hwnd, ctypes.byref(rect))
            gm_w = rect.right - rect.left
            gm_h = rect.bottom - rect.top
            x = rect.left + (gm_w - req_w) // 2
            y = rect.top + (gm_h - req_h) // 2
            return max(x, 0), max(y, 0)
    except Exception:
        pass

    screen_w = dialog_widget.winfo_screenwidth()
    screen_h = dialog_widget.winfo_screenheight()
    x = (screen_w - req_w) // 2
    y = (screen_h - req_h) // 2
    return max(x, 0), max(y, 0)


def _remove_close_button(win):
    """
    Strips the titlebar's close (X) button (and system menu) via the
    Win32 API directly.

    protocol("WM_DELETE_WINDOW", lambda: None) (used elsewhere for this
    same window) only prevents the CLICK from doing anything -- the X
    itself stays fully visible, still highlights on hover, and still
    looks clickable, since Tkinter/the OS's own window chrome has no
    idea the close action has been neutralized. Confirmed via direct
    user report/screenshot: this reads as broken (a button that does
    nothing when clicked) rather than intentionally absent. This
    function is a stronger fix -- actually removing the button from the
    titlebar so there's nothing there to click in the first place.

    Uses GetWindowLongW/SetWindowLongW to clear the WS_SYSMENU bit from
    the window's style. WS_SYSMENU controls the system menu AND the
    close button as a single unit in the Win32 API -- there is no
    separate flag for "close button only". Removing it also removes the
    right-click system menu and Alt+F4 for this specific window, and
    (on most Windows versions) the small titlebar icon -- acceptable
    here since none of those are meaningful for a progress window with
    no cancel action to offer regardless.

    GetParent(win.winfo_id()) rather than win.winfo_id() directly: a
    long-standing, widely-documented Tkinter-on-Windows quirk where
    winfo_id() can return a CHILD window's HWND rather than the actual
    top-level frame's HWND on some Tcl/Tk builds -- GetParent() walks up
    to the real top-level window whose style actually controls the
    titlebar, which winfo_id() does not reliably do on its own.

    Windows-only (ctypes.windll doesn't exist on other platforms) and
    fully defensive: any failure here (wrong platform, unexpected
    Win32 API/ABI mismatch) is caught and logged, falling back to the
    protocol()-based click-does-nothing behavior already in place --
    a visible-but-inert X is a much smaller problem than crashing the
    whole tool over what is ultimately a cosmetic fix.

    NOTE: could not be tested against a real Windows/DWM environment
    from this development environment (Linux-only) -- this is a
    well-established, commonly-documented Win32 API pattern for this
    exact "remove system menu + close box" scenario, but the actual
    visual/behavioral result (in particular, whether it affects the
    minimize/maximize buttons on this specific window's exact style
    flags) should be confirmed on the real deployment target.
    """
    try:
        import ctypes
        GWL_STYLE = -16
        WS_SYSMENU = 0x00080000
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_STYLE, style & ~WS_SYSMENU)
    except Exception as e:
        print(f"⚠️ Could not remove the titlebar close button (non-Windows platform, or unexpected Win32 API issue): {e}")

# MAX_ROAD_DISTANCE: sanity cutoff (meters, in the PRS92 projected CRS
# used internally by process()) applied to every width candidate in the
# frontage-first _measure_width() algorithm (see ROAD_FRONT_TOLERANCE
# below for the frontage-detection stage itself). Originally added when
# the measurement algorithm did an unbounded nearest-road search with no
# built-in distance limit -- a parcel with no real nearby road (layer
# misalignment, wrong CRS, a road type just excluded via Filter by Road
# Type, or a genuine data gap) could silently receive a large,
# meaningless ROAD_WIDTH instead of being flagged as unmeasurable (None),
# unlike every other bail-out case in process(). Kept as a defensive
# safeguard under the current frontage-first algorithm even though every
# width candidate there is already confined to ROAD_FRONT_TOLERANCE (so
# this check should not be structurally reachable in practice) -- not
# removed without evidence it's genuinely unnecessary.
#
# 50m is a starting point, not a fixed law -- distinct in purpose from
# the smaller (10-15m) proximity tolerances used elsewhere in this
# codebase for "is this touching/adjacent" classification decisions
# (e.g. the 10m road-touch buffer referenced in this file's own history,
# and lot_location.py's own tolerances). MAX_ROAD_DISTANCE instead
# answers "is this measurement still physically plausible at all" --
# it needs to comfortably cover genuine local/barangay road corridor
# widths, sidewalk/drainage/easement space between a parcel boundary and
# a road centerline, and typical misalignment between separately
# digitized parcel and road layers, while still catching real error
# cases (hundreds of meters off, or no nearby road at all). Adjust here
# if production data shows it's too strict or too loose.
MAX_ROAD_DISTANCE = 50

# ROAD_FRONT_TOLERANCE: adjacency tolerance (meters, same projected CRS)
# used by the frontage-first measurement algorithm to decide whether a
# parcel boundary segment is genuinely road-adjacent at all ("is this
# the road front"). Distinct business rule from MAX_ROAD_DISTANCE above
# -- that one answers "is this measurement still physically plausible,"
# this one answers "is this edge actually touching a road." Matches the
# established codebase convention for this kind of adjacency test (the
# same 10m used by road_frontage.py's _edge_covered_portion() default,
# and by lot_location.py's own tolerances).
ROAD_FRONT_TOLERANCE = 10

barangay_source = None
road_source = None
output_mode = None

# ----------------- ROAD CLASSIFICATION STATE (new) -----------------
# Ported from road_frontage.py's Road Classification feature. Same
# philosophy: parcel_classification_selection is a PER-SOURCE dict
# ({path_or_table: bool}) since a batch of Land Parcel sources may mix
# files that should and shouldn't have classification applied.
# filter_by_road_type_active is a single flag (Road Network only ever
# has one selected source). The two are mutually exclusive at the GUI
# level (see open_main_window()'s checkbox wiring). Set by
# open_main_window()'s on_run(), read by run_processing() and
# resolve_classification() (below).
parcel_classification_selection = {}
filter_by_road_type_active = False

# road_type_excluded_values: list[str] of ROAD_TYPE values (exact,
# case-sensitive) the user unchecked in the "Filter by Road Type"
# checklist. Only ever consulted when filter_by_road_type_active is
# True.
road_type_excluded_values = []

# parcel_road_width_column_overrides: {path_or_table: existing_col_name}
# -- for any Land Parcel source where a pre-existing "CAMA_ROAD_WIDTH"-like
# column was detected (see the merged Land Parcel background read below
# -- the SAME read that already checks for LOT_LOCATION classification
# columns, extended to also check for this) and the user confirmed
# proceeding via the single combined dialog in on_run(). Threaded into
# process() as output_column_name, so the tool writes back into the
# EXACT existing column (preserving its original casing) instead of
# always writing a hardcoded "CAMA_ROAD_WIDTH" -- the latter would silently
# create a confusing duplicate column whenever the existing one used
# different casing (e.g. "cama_road_width" alongside a new "CAMA_ROAD_WIDTH"). A
# source with no entry here uses the default "CAMA_ROAD_WIDTH" name.
parcel_road_width_column_overrides = {}

# _road_gdf_cache: dual-slot cache -- one independent slot for "local"
# and one for "db", so switching the Road Network Source radio back and
# forth never re-reads a selection that's still valid, and never mixes
# up which selection belongs to which mode. See
# _refresh_road_classification() below for exactly how each field is
# used.
_road_gdf_cache = {
    "local": {"key": None, "gdf": None, "value_vars": {}, "filter_active": False},
    "db": {"key": None, "gdf": None, "value_vars": {}, "filter_active": False},
}

# _parcel_classification_cache: same dual-slot idea, adapted for Land
# Parcel Source. Deliberately does NOT cache the GeoDataFrames themselves
# -- Land Parcel can have MANY selected sources at once, so holding every
# one of their full GeoDataFrames in memory just to make radio-toggling
# instant would be a real memory cost for a large batch. Instead, each
# slot caches only the lightweight per-source detection results (tiny
# tuples) plus the per-source checkbox BooleanVars, keyed by the exact
# tuple of currently selected sources for that mode.
_parcel_classification_cache = {
    "local": {"key": None, "details": None, "vars": {}},
    "db": {"key": None, "details": None, "vars": {}},
}

# ----------------- HELPERS -----------------
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
        tables = [row[0] for row in cur.fetchall()]
        conn.close()
        return tables
    except Exception as e:
        messagebox.showerror("DB Error", str(e))
        return []

# NOTE: normalize_name() and find_matching_table() previously existed
# here -- removed entirely. find_matching_table() used SUBSTRING
# matching (normalize_name() strips non-letters, then checks "a in b or
# b in a"), which was a real bug: a desired table name could match and
# overwrite a completely unrelated existing table just because one name
# happened to contain the other as a substring after normalization. The
# db-output write logic now uses exact matching only (see the
# local-source -> db-output branch in run_processing()), consistent
# with the db-source -> db-output branch, which already did this
# correctly.

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
    appended (no "_road_width", "_road_frontage", etc. -- this tool
    reuses the source's own name as its default output name).

    Rule: reuse the desired name exactly if nothing of that name exists
    yet in `folder`. If it already exists, NEVER overwrite -- instead,
    strip any existing trailing "_<N>" from the desired name to get a
    root (e.g. "landparcel_1" -> root "landparcel"), scan `folder` for
    every file matching "<root>_<N>.<ext>", and use "<root>_<max(N)+1>"
    -- the highest N found ANYWHERE in the folder, not just "the source
    file's own N + 1". This matters: if the selected source happens to
    be named "landparcel_1" but the folder already has files up through
    "landparcel_2" (or higher, or with gaps), naively trying
    "landparcel_2" next could still collide -- scanning for the true
    max avoids that regardless of what number the source itself had.

    This function decides the number ONCE, for the MAIN output only.
    Every other output belonging to the same processing run (e.g. the
    VM/visual-measurement layer) must reuse this exact returned name as
    its own base -- see with_qa_suffix() below -- never re-run this scan
    independently, or the two could drift out of the paired numbering a
    user expects (e.g. "landparcel_1.gpkg" should always pair with
    "landparcel_1_VM.gpkg", never with a mismatched "landparcel_VM.gpkg"
    from an independent scan).
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


def _find_existing_table_case_insensitive(desired_name, all_tables):
    """
    Case-insensitive lookup for an existing table matching desired_name
    among all_tables (as returned by fetch_tables()). Returns the EXACT
    existing name as stored in the database (never desired_name's own
    casing) if a case-insensitive match is found, else None.

    Only the COMPARISON is case-insensitive -- the return value never
    is. This exists specifically so callers can show the user the
    actual table name that already exists (e.g. "landparcel"), never
    silently substituting whatever casing the caller happened to be
    asking about (e.g. an incoming "LanDPARCEL" must never make this
    function claim the existing table is itself called "LanDPARCEL").
    """
    desired_lower = desired_name.lower()
    for existing in all_tables:
        if existing.lower() == desired_lower:
            return existing
    return None


def resolve_db_table_name(schema, desired_base_name):
    """
    Determines the actual table name to use for a NEW table, given the
    DESIRED name -- the same "reuse the desired name if nothing of that
    name exists yet; otherwise find the true max existing "<root>_<N>"
    suffix ANYWHERE in the schema and use max(N)+1" rule as
    resolve_output_base_name() uses for local files (see that
    function's own docstring for the full rationale -- identical logic,
    just scanning fetch_tables(schema) instead of a folder's file
    listing, and with no extension involved).

    Case-insensitive matching throughout (PostgreSQL table names read
    back from information_schema are compared without regard to case),
    but the RETURNED name always uses desired_base_name's own casing as
    its root -- e.g. desired "LandParcel" with existing "landparcel"
    and "LandParcel_2" (any casing) already present returns
    "LandParcel_3", preserving the caller's own casing, not whatever
    casing existing rows happened to use.
    """
    all_tables = fetch_tables(schema)
    if _find_existing_table_case_insensitive(desired_base_name, all_tables) is None:
        return desired_base_name

    root, _existing_number = _split_trailing_number(desired_base_name)

    pattern = re.compile(rf'^{re.escape(root)}_(\d+)$', re.IGNORECASE)
    max_n = 0
    for existing in all_tables:
        m = pattern.match(existing)
        if m:
            max_n = max(max_n, int(m.group(1)))

    return f"{root}_{max_n + 1}"


def normalize_name(name: str) -> str:
    """
    Lowercases name and strips everything that isn't a letter (digits,
    underscores, spaces, hyphens, etc.) -- e.g. "LandParcel_2026" and
    "Land Parcel Final" both normalize to "landparcelfinal"-style
    strings with no separators left, so filenames and table names that
    differ only by punctuation/casing/trailing numbers can still be
    recognized as referring to the same logical table.
    """
    return re.sub(r'[^a-z]', '', name.lower())


def find_matching_tables(desired_name, all_tables):
    """
    Returns the list of candidate table names from all_tables whose
    normalized form is a substring of (or contains) the normalized
    desired_name -- checked in both directions, so "landparcel" matches
    "landparcel_final" and "landparcel_2026" matches "landparcel"
    equally. This is intentionally permissive (fuzzy) matching: the
    caller is responsible for confirming the match with the user
    before treating it as a definite overwrite target (see
    confirm_db_overwrite_dialog() / choose_db_overwrite_dialog() and
    resolve_db_output_table()) -- this function only proposes
    candidates, it never decides on its own.

    Always excludes "CAMA_Table", "CAMA_Transaction_Log", and any table
    ending in "_VM" (case-insensitive) from the candidate list, since
    none of these are ever valid "main output" overwrite targets even
    if their name happens to contain a substring match (e.g. a
    Visual Measurement layer table like "landparcel_VM" would otherwise
    also match a "landparcel" search).
    """
    lname = normalize_name(desired_name)
    candidates = []
    for t in all_tables:
        if t.lower() in ("cama_table", "cama_transaction_log"):
            continue
        if t.lower().endswith("_vm"):
            continue
        tnorm = normalize_name(t)
        if lname in tnorm or tnorm in lname:
            candidates.append(t)
    return candidates


def ask_db_overwrite_dialog(parent, conflicting_pairs):
    """
    Combined dialog shown ONCE, before any processing starts, when one
    or more Land Parcel sources' desired database table name already
    exists (case-insensitively) in the target schema. Mirrors
    ask_overwrite_dialog()'s design (same three choices, same
    single-combined-decision-for-the-whole-batch philosophy, same
    grab_set()/no-transient() dialog-safety pattern) -- kept as a
    separate function rather than a shared one because the two operate
    on genuinely different resources (files in a folder vs. tables in a
    schema) with different name-matching rules (filesystem paths are
    typically case-sensitive; this dialog's whole reason to exist is
    PostgreSQL's case-INsensitive default table-name comparison).

      - "Overwrite": every conflicting table is replaced in place,
        using the schema's ACTUAL existing casing for each (e.g. an
        incoming "LandParcel" that matched an existing "landparcel"
        writes to "landparcel", not "LandParcel") -- never creates a
        second, differently-cased duplicate table.
      - "Create New": every conflicting table gets a new, non-colliding
        name instead (resolve_db_table_name()), using the INCOMING
        source's own casing as the new name's root, leaving the
        existing table(s) completely untouched.
      - "Cancel" (or closing the dialog): aborts the entire run. No
        source is processed, including ones with no conflict -- same
        all-or-nothing semantics as ask_overwrite_dialog().

    conflicting_pairs: list of (desired_name, existing_name) tuples --
    existing_name is the actual casing found in the schema, always
    shown to the user instead of desired_name, so "Found existing
    table: 'landparcel'" always names what's REALLY there, not what
    the incoming source happened to be called.

    Returns "overwrite", "new", or "cancel".
    """
    result = {"choice": "cancel"}

    dialog = tk.Toplevel(parent)
    apply_icon(dialog)
    dialog.title("ROAD WIDTH TOOL")
    dialog.resizable(False, False)
    dialog.grab_set()
    dialog.deiconify()
    dialog.lift()
    dialog.focus_force()
    dialog.attributes("-topmost", True)
    dialog.after(100, lambda: dialog.attributes("-topmost", False))

    def choose(choice):
        result["choice"] = choice
        dialog.destroy()

    dialog.protocol("WM_DELETE_WINDOW", lambda: choose("cancel"))

    # Buttons packed first, at the bottom -- same reasoning as
    # ask_overwrite_dialog() (the local-file version of this same
    # dialog): guaranteed visible/reachable at the bottom of the
    # window regardless of how tall the scrollable table list above
    # them ends up being. Packing this LAST (as before) left the
    # buttons wherever they fell in call order -- visually stranded
    # in the middle of the dialog, above the explanation label -- since
    # Tkinter's default side="top" packing places each widget in call
    # order, not by where the caller might expect it to land.
    btn_frame = tk.Frame(dialog)
    btn_frame.pack(side="bottom", fill="x", pady=(4, 12))
    tk.Button(btn_frame, text="Overwrite", width=14, cursor="hand2",
              command=lambda: choose("overwrite")).pack(side="left", padx=(16, 4))
    tk.Button(btn_frame, text="Create New", width=14, cursor="hand2",
              command=lambda: choose("new")).pack(side="left", padx=4)
    tk.Button(btn_frame, text="Cancel", width=14, cursor="hand2",
              command=lambda: choose("cancel")).pack(side="left", padx=(4, 16))

    tk.Label(
        dialog, text="Found existing table(s):",
        font=("Segoe UI", 10, "bold"), anchor="w"
    ).pack(fill="x", padx=16, pady=(16, 4))

    MAX_LIST_LINES = 10
    names = [existing for _desired, existing in conflicting_pairs]
    list_frame = tk.Frame(dialog)
    list_frame.pack(fill="both", expand=True, padx=16, pady=(0, 8))
    vscroll = tk.Scrollbar(list_frame, orient="vertical")
    text = tk.Text(
        list_frame, wrap="none",
        height=min(len(names), MAX_LIST_LINES), width=50,
        yscrollcommand=vscroll.set, relief="flat",
        bg=dialog.cget("bg"), font=("Segoe UI", 9))
    vscroll.config(command=text.yview)
    if len(names) > MAX_LIST_LINES:
        vscroll.pack(side="right", fill="y")
    text.pack(side="left", fill="both", expand=True)
    for name in names:
        text.insert("end", f"{name}\n")
    text.config(state="disabled")

    tk.Label(dialog, text="Do you want to overwrite them?",
             anchor="w").pack(fill="x", padx=16, pady=(0, 12))

    tk.Label(dialog, text=(
        "\"Overwrite\" replaces the existing table(s) shown above with "
        "the new results. \"Create New\" saves the new results under a "
        "new table name instead, leaving the existing table(s) "
        "untouched. This choice applies to all tables listed above."
    ), anchor="w", justify="left", wraplength=420
             ).pack(fill="x", padx=16, pady=(4, 16))

    dialog.update_idletasks()
    req_w = max(dialog.winfo_reqwidth(), 460)
    req_h = dialog.winfo_reqheight()
    x, y = _get_dialog_center_position(dialog, req_w, req_h)
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
    apply_icon(dialog)
    dialog.title("ROAD WIDTH TOOL")
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

    # Buttons packed first, at the bottom -- same reasoning as
    # ask_db_overwrite_dialog() above.
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
    x, y = _get_dialog_center_position(dialog, req_w, req_h)
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
    apply_icon(dialog)
    dialog.title("ROAD WIDTH TOOL")
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

    # Buttons packed first, at the bottom -- same reasoning as
    # ask_db_overwrite_dialog() above.
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
    x, y = _get_dialog_center_position(dialog, req_w, req_h)
    dialog.geometry(f"{req_w}x{req_h}+{x}+{y}")

    dialog.wait_window()
    return result["chosen"]


def with_qa_suffix(main_base_name: str) -> str:
    """
    Derives the Visual Measurement layer's base name from the
    ALREADY-FINALIZED main output base name (see
    resolve_output_base_name()) -- never scans the folder independently
    for its own numbering, so the two stay paired: "landparcel.gpkg" +
    "landparcel_VM.gpkg", "landparcel_1.gpkg" + "landparcel_1_VM.gpkg",
    etc. Main output is always the source of truth for the number; this
    layer just follows it.

    Suffix is "_VM" (Visual Measurement) -- kept as the function/variable
    name "qa" internally throughout this file (qa_gdf, qa_out, qa_table,
    etc.) since that still describes this layer's role (a validation
    aid), even though the actual file/table name it produces uses the
    "_VM" suffix instead of "_QA_lines".
    """
    return f"{main_base_name}_VM"

def get_geometry_column(table_name, engine, schema):
    try:
        with engine.connect() as conn:
            query = text("""
                SELECT f_geometry_column
                FROM geometry_columns
                WHERE f_table_schema = :schema AND f_table_name = :table
            """)
            result = conn.execute(query, {"schema": schema, "table": table_name})
            row = result.fetchone()
            return row[0] if row else None
    except Exception as e:
        print(f"❌ Error fetching geometry column: {e}")
        return None

def read_postgis_clean(table, engine, schema):
    """Read PostGIS table with only one geometry column (avoid geom as text)."""
    geom_col = get_geometry_column(table, engine, schema)
    if not geom_col:
        raise ValueError(f"No geometry column found in {table}")

    insp = inspect(engine)
    cols = [c['name'] for c in insp.get_columns(table, schema=schema) if c['name'] != geom_col]

    col_str = ", ".join([f'"{c}"' for c in cols]) if cols else ""
    if col_str:
        query = f'SELECT {col_str}, "{geom_col}" AS geometry FROM "{schema}"."{table}"'
    else:
        query = f'SELECT "{geom_col}" AS geometry FROM "{schema}"."{table}"'

    return gpd.read_postgis(query, engine, geom_col="geometry")

# NOTE: a module-level fix_geometry() helper previously existed here,
# applied via barangay_gdf["geometry"].apply(fix_geometry) directly onto
# the OUTPUT geometry column inside process() -- removed entirely. Two
# real problems with it: (1) it wrote the "fixed" geometry back into the
# exported column instead of scoping the fix to internal use only, and
# (2) for a MultiPolygon result (which buffer(0) can produce on complex/
# self-intersecting input), it kept only the LARGEST piece and silently
# discarded the rest -- if the discarded piece sat right next to the
# kept one, this showed up as a visible hole/missing chunk in the parcel
# output. The per-parcel measurement loop below already has its own,
# safe, LOCAL-SCOPE geometry repair (poly = poly.buffer(0), now with a
# make_valid() fallback -- see the loop) that never touches
# barangay_gdf's own geometry column at all, matching the same
# principle lot_location.py's fix_geometry() + brgy_fixed_geom pattern
# already established: repair for internal spatial-operation use only,
# keep the original shape in the exported output.

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

def load_in_global_mapper(filepath):
    """Open or load a file into Global Mapper if it is already running,
    otherwise launch Global Mapper with the file as an argument."""
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
                return False  # stop enumeration
            return True

        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        ctypes.windll.user32.EnumWindows(EnumWindowsProc(enum_callback), 0)

        if gm_hwnd:
            # GM is running — use subprocess to open the file via GM's command-line
            # GM supports being called again with a file path; it opens in the existing instance
            subprocess.Popen([GM_EXE_PATH, filepath])
            print(f"🗺️ Sent to Global Mapper: {filepath}")
        else:
            # GM is not running — launch it with the file
            subprocess.Popen([GM_EXE_PATH, filepath])
            print(f"🚀 Launched Global Mapper with: {filepath}")

    except Exception as e:
        print(f"⚠️ Could not open in Global Mapper: {e}")

# ----------------- CRS UTILITY -----------------
# PRS92_ZONE_BOUNDS: same zone boundary numbers road_width.py already
# used, restructured into the table-driven form shared with
# lot_location.py / road_frontage.py, so all three tools stay in sync if
# the boundaries are ever revised.
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
    its CRS isn't already WGS84, since the zone thresholds are
    degree-based. Uses the COMBINED extent of every GeoDataFrame
    passed in rather than deciding from a single layer alone.

    Auxiliary layers without usable geometry are ignored for CRS zone
    determination -- zone detection proceeds as long as at least one
    valid layer remains. A layer with no usable geometry at all
    (all-null, or all-empty-but-non-null shapes) raises a ValueError
    naming that specific layer, rather than silently corrupting the
    computed longitude.
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
        if epsg != 4326:
            g_wgs84 = g.to_crs(epsg=4326)
        else:
            g_wgs84 = g

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
            return epsg

    raise ValueError(f"Could not determine PRS92 zone for longitude {center_lon}")

# ----------------- ROAD CLASSIFICATION UTILITIES (new) -----------------
# Ported verbatim from road_frontage.py's Road Classification feature
# (the canonical, approved reference implementation).

# ROAD_TYPE_COLUMN_CANDIDATES: case-insensitive column-name aliases used
# to locate a road-classification column in a user-supplied road layer.
# Copied verbatim from lot_location.py / road_frontage.py so all three
# tools agree on what counts as a "ROAD_TYPE-like" column. Do not
# diverge without updating all three files.
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


# ROAD_NAME_COLUMN_CANDIDATES: reuses the EXACT candidate list already
# established in lot_location.py's own road_name_col detection (see
# lot_location.py's _deduplicate_road_ids()/process_lot_location()) --
# not a new, independently-invented convention.
ROAD_NAME_COLUMN_CANDIDATES = ("road_name", "roadname", "name", "street", "road_no")


def _detect_road_name_column(gdf):
    """
    Case-insensitive lookup of a ROAD_NAME-like column in a GeoDataFrame.
    Returns the actual column name (original casing preserved) or None.
    """
    if gdf is None:
        return None
    return next(
        (c for c in gdf.columns if c.lower() in ROAD_NAME_COLUMN_CANDIDATES),
        None
    )


# _PIN_CANDIDATES: exact-case column-name candidates for a parcel's own
# identifier, in priority order. Reuses the SAME list already
# established in lot_location.py (used there to identify unfixable-
# geometry parcels for logging) -- not a new, independently-invented
# convention.
_PIN_CANDIDATES = ["PIN", "pin", "Pin", "ARP_NO", "TD_NO", "PARCEL_ID"]


def _detect_pin_column(gdf):
    """
    Identifier-column lookup for a parcel GeoDataFrame, shared by every
    place road_width.py needs to label a row (the Visual Measurement layer's PIN field,
    and the CAMA_Table PIN write in run_processing()). Tries, in order:

      1. _PIN_CANDIDATES, exact case match first (PIN, pin, Pin, ARP_NO,
         TD_NO, PARCEL_ID) -- matches lot_location.py's own convention.
      2. Any of those same names, case-insensitive (catches e.g. "Arp_No").
      3. "FID" (case-insensitive) -- road_width.py-specific: lot_location.py
         has no equivalent need for this fallback since it doesn't
         produce a Visual Measurement layer that has to label individual rows; road_width.py
         does, so a last-resort identifier is worth having even if it's
         not a real cadastral PIN.

    Returns the actual column name (original casing preserved) or None
    if the parcel source has none of the above -- callers are expected
    to drop the PIN/identifier field entirely in that case rather than
    emit an all-None column.
    """
    if gdf is None:
        return None
    for candidate in _PIN_CANDIDATES:
        if candidate in gdf.columns:
            return candidate
    lower_candidates = {c.lower() for c in _PIN_CANDIDATES}
    found = next((c for c in gdf.columns if c.lower() in lower_candidates), None)
    if found:
        return found
    return next((c for c in gdf.columns if c.lower() == "fid"), None)


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

# Tri-state result of inspecting a parcel layer for a usable
# classification column -- kept as named states rather than a bare
# has_lot_location bool so "column present but unusable" (e.g. an
# all-NULL LOT_LOCATION column) stays distinguishable from "column
# absent entirely".
LOT_STATE_NOT_FOUND = "not_found"   # no LOT_LOCATION column at all
LOT_STATE_UNUSABLE = "unusable"     # column present but no usable values
LOT_STATE_FOUND = "found"           # a usable column was found

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
    one effective processing directive, so process() never branches on
    this logic itself -- it only consumes the result. Called once per
    parcel source in run_processing(), since sources are evaluated
    independently (a batch may mix sources that do and don't have a
    usable LOT_LOCATION column, AND the user may have only
    checked the per-source classification checkbox for some of them).

    use_lot_classification here is already resolved to THIS specific
    source (run_processing() looks it up from the per-source
    parcel_classification_selection dict before calling this function --
    each selected Land Parcel file/table gets its own checkbox in the
    GUI). filter_by_road_type_active, by contrast, is a single flag,
    since Road Network only ever has one selected source. The two are
    mutually exclusive at the GUI level (see open_main_window()'s
    checkbox wiring): checking Filter by Road Type unchecks every
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
                                   LOT_LOCATION" checkbox is checked.
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


# ----------------- FRONTAGE-FIRST WIDTH MEASUREMENT UTILITIES -----------------
# split_boundary_to_segments(): ported verbatim from road_frontage.py.
# No behavioral changes.
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


def _edge_covered_portion_and_road(seg, road_union, tol=10):
    """
    road_width.py-specific variant of road_frontage.py's
    _edge_covered_portion(). Deliberately kept as its OWN, separate
    function rather than modifying the reference implementation --
    _edge_covered_portion() has one responsibility ("how much of this
    edge is valid frontage") and this tool needs one additional piece of
    information road_frontage.py never needed: WHICH local road geometry
    produced that frontage, so ROAD_WIDTH can be measured against that
    same local road specifically -- not the road network as a whole,
    which risks matching the wrong nearby road on curves,
    Y-intersections, or divided highways.

    Uses the IDENTICAL geometric technique as _edge_covered_portion() --
    same tol, same flat-capped buffer confined to this segment's own
    footprint (cap_style=2, never extended past the segment's own two
    endpoints) -- so the two functions will always agree on WHETHER a
    segment has valid frontage. This function only additionally surfaces
    the `road_in_zone` geometry that _edge_covered_portion() computes
    internally and discards.

    For one elementary boundary segment (vertex-to-vertex), returns
    (covered_piece, road_in_zone):
      covered_piece : LineString spanning the covered sub-portion of
                       `seg`, or None if no part of it is within `tol`
                       of any road geometry.
      road_in_zone  : the portion of `road_union` that produced that
                       coverage -- confined to `seg`'s own tol-buffer
                       footprint, so it is always geometrically local to
                       THIS segment, never a stray piece of a different,
                       merely-nearby road. None if covered_piece is None.
    """
    zone = seg.buffer(tol, cap_style=2)
    road_in_zone = road_union.intersection(zone)
    if road_in_zone.is_empty:
        return None, None

    gt = road_in_zone.geom_type
    if gt == "LineString":
        pts = list(road_in_zone.coords)
    elif gt == "MultiLineString":
        pts = [c for g in road_in_zone.geoms for c in g.coords]
    elif gt == "Point":
        pts = [(road_in_zone.x, road_in_zone.y)]
    elif gt == "MultiPoint":
        pts = [(p.x, p.y) for p in road_in_zone.geoms]
    else:
        return None, None

    fracs = [seg.project(Point(p)) for p in pts]
    lo, hi = min(fracs), max(fracs)
    if hi - lo < 1e-9:
        return None, None

    covered_piece = LineString([seg.interpolate(lo), seg.interpolate(hi)])
    return covered_piece, road_in_zone


# ----------------- MAIN PROCESS -----------------
def process(barangay_gdf, road_gdf, source_name="", progress_cb=None, classification=None, output_column_name="CAMA_ROAD_WIDTH"):
    # classification: dict produced by resolve_classification() -- see
    # its docstring for the exact shape. Defaults to "no gating at all"
    # (identical to this tool's original, pre-feature behavior) so any
    # existing caller that doesn't pass this argument keeps working
    # exactly as before.
    if classification is None:
        classification = {
            "mode": "no_gating",
            "skip_mask": None,
            "excluded_road_types": [],
            "lot_column": None,
            "lot_kind": None,
        }

    # output_column_name: the column name the computed width is written
    # into on barangay_gdf (default "CAMA_ROAD_WIDTH"). Callers pass an
    # override here when the selected parcel source already has an
    # existing column matching "cama_road_width" (case-insensitive, any
    # casing) and the user confirmed proceeding -- see
    # parcel_road_width_column_overrides above and the combined
    # confirmation dialog in on_run(). Writing back into the EXACT
    # existing name (preserving its casing) avoids silently creating a
    # duplicate column (e.g. "cama_road_width" alongside a new "CAMA_ROAD_WIDTH"),
    # since pandas column names are case-sensitive. The Visual Measurement layer's own
    # "CAMA_ROAD_WIDTH" field is unaffected by this -- it's a brand-new output
    # layer with no pre-existing column to collide with.

    original_crs = barangay_gdf.crs

    # Row-count check happens BEFORE any geometry validity handling --
    # deliberately independent of it, since a parcel source with zero
    # rows to begin with is a different situation from one where every
    # row's geometry happens to be invalid (the latter no longer drops
    # rows at all -- see the per-parcel loop below).
    if len(barangay_gdf) == 0:
        raise ValueError(f"No parcels found in {source_name}")

    zone_epsg = detect_prs92_zone([("Land Parcel", barangay_gdf), ("Road Network", road_gdf)])
    print(f"🌍 [{source_name}] Reprojecting to EPSG:{zone_epsg}...")
    barangay_gdf = barangay_gdf.to_crs(epsg=zone_epsg)
    road_gdf = road_gdf.to_crs(epsg=zone_epsg)

    # ------------------------------------------------------------------
    # Optional, user-driven Road Type filter (Road Classification ->
    # "Filter by Road Type" mode only -- classification["excluded_road_types"]
    # is always [] for both Automatic modes, by construction in
    # resolve_classification(), so Automatic mode never reaches the
    # filtering branch below even if the checklist has stale unchecked
    # values from a previous "Filter by Road Type" session).
    #
    # Mirrors road_frontage.py's / lot_location.py's road-type filter --
    # column detection, .isin() exclusion, and the "all excluded -> fall
    # back to unfiltered" safety net -- so all three tools behave
    # identically given the same road layer and the same excluded
    # values.
    # ------------------------------------------------------------------
    excluded_road_types = classification.get("excluded_road_types") or []
    road_type_col = _detect_road_type_column(road_gdf)
    if road_type_col and excluded_road_types:
        original_count = len(road_gdf)
        filtered_gdf = road_gdf[~road_gdf[road_type_col].isin(excluded_road_types)].copy()
        if len(filtered_gdf) == 0:
            print(f"⚠️ [{source_name}] All road types excluded by filter -- "
                  f"falling back to full road layer.")
        else:
            road_gdf = filtered_gdf
            print(f"ℹ️ [{source_name}] Road type filter: {len(filtered_gdf)}/{original_count} "
                  f"roads retained after excluding {len(excluded_road_types)} type(s) "
                  f"(column: '{road_type_col}').")

    # ── Build road union for frontage detection ────────────────────
    # road_union: single, computed-once geometry combining every
    # PARTICIPATING road feature (already filtered by the Road Type
    # filter above, if that mode is active -- Step 1, Classification,
    # has already run by this point). Replaces the old segment_geoms /
    # segment_midpoints / seg_tree infrastructure: the frontage-first
    # algorithm below no longer does an approximate nearest-segment
    # search across the whole boundary -- it directly tests each parcel
    # boundary segment's own local footprint against this union (see
    # _edge_covered_portion_and_road() above).
    #
    # NOTE: road_union has no attribute table of its own (it's a single
    # unioned geometry blob) -- ROAD_TYPE/ROAD_NAME attribution for the
    # Visual Measurement layer below is looked up separately, from the original road_gdf
    # rows, confined to each winning segment's own local zone.
    road_geoms = [g for g in road_gdf.geometry if g is not None and not g.is_empty]

    # Visual Measurement layer column list, decided ONCE per process() call based on
    # what's actually available -- not hardcoded to a fixed 5/6 fields.
    # id_col: see _detect_pin_column() for the full priority order (PIN/
    # ARP_NO/TD_NO/PARCEL_ID, then FID as a last resort) -- never a
    # synthetic row index, since an index has no meaning outside this
    # one processing run and can't be used to look the parcel back up in
    # QGIS or the source data. If nothing is found, the PIN field is
    # dropped entirely rather than emitted as all-None.
    id_col = _detect_pin_column(barangay_gdf)
    road_type_col = _detect_road_type_column(road_gdf)
    road_name_col = _detect_road_name_column(road_gdf)

    qa_columns = ["CAMA_ROAD_WIDTH", "FRONT_SEGMENT"]
    if id_col:
        qa_columns.insert(0, "PIN")
    if road_type_col:
        qa_columns.append("ROAD_TYPE")
    if road_name_col:
        qa_columns.append("ROAD_NAME")
    qa_columns.append("geometry")

    if not road_geoms:
        if progress_cb:
            for _ in range(len(barangay_gdf)):
                progress_cb(1)
        barangay_gdf[output_column_name] = None
        qa_gdf = gpd.GeoDataFrame(columns=qa_columns, geometry="geometry", crs=barangay_gdf.crs)
        if original_crs:
            barangay_gdf = barangay_gdf.to_crs(original_crs)
            qa_gdf = qa_gdf.to_crs(original_crs)
        return barangay_gdf, qa_gdf

    road_union = unary_union(road_geoms)

    def _measure_width(poly, boundary):
        """
        Frontage-first measurement. Architecture (see project design
        notes for the full discussion):

          Step 1 -- Classification (already applied before this function
                    runs: Road Type filter on road_gdf above, and the
                    Inner-Lot skip_mask check in the parcel loop below)
                    determines WHICH parcels and roads participate. It
                    does not affect how width is measured.

          Step 2 -- Frontage detection: split the parcel boundary into
                    individual segments; for each, test whether it is
                    genuinely road-adjacent within ROAD_FRONT_TOLERANCE,
                    using the SAME geometric technique as
                    road_frontage.py's _edge_covered_portion() (a
                    flat-capped buffer confined to that segment's own
                    footprint -- never "bleeds" onto an unrelated nearby
                    road on curves, Y-intersections, or divided
                    highways). Each valid segment is paired with the
                    LOCAL road geometry that produced it, not the road
                    network as a whole.

          Step 3 -- Width measurement: for every valid frontage segment,
                    the width candidate is the distance from its covered
                    portion to its OWN corresponding local road geometry
                    (not to road_union as a whole -- preserves the
                    frontage <-> corresponding-road relationship instead
                    of "frontage <-> any nearby road"), doubled
                    (centerline -> edge = half width). A candidate is
                    discarded (excluded from the pool, not fabricated as
                    None-worthy) if it exceeds MAX_ROAD_DISTANCE -- kept
                    as a defensive safeguard even though it should not be
                    structurally reachable, since every candidate is
                    already confined to ROAD_FRONT_TOLERANCE.

          Final   -- ROAD_WIDTH = the MINIMUM among all valid width
                    candidates for this parcel -- not a "dominant" /
                    longest-frontage pick. For a corner lot facing two
                    genuine road frontages of different widths, the
                    smaller of the two is the more conservative value.
                    (road_frontage.py's own "pick the longest covered
                    piece" precedent is a DEPTH-direction heuristic, not
                    a general "primary road" rule, and is not carried
                    over here -- different business metric.)

        Returns a 5-tuple:
          (width, front_segment_index, qa_line, road_type_value, road_name_value)
        or (None, None, None, None, None) if the parcel has no boundary
        segments at all, or no segment qualifies as valid frontage.

        qa_line is a Visual Measurement geometry for the WINNING (minimum)
        candidate ONLY -- not a new or approximate measurement. It's
        built from the exact two points shapely's nearest_points() finds
        between that winning candidate's covered_piece and road_in_zone
        (the same pair whose .distance() produced the winning
        raw_distance), then extended once more in the same direction so
        the line's own length in QGIS equals the FULL, doubled
        ROAD_WIDTH value -- not just the undoubled half-width. This
        matches the "centerline is the midpoint, both edges are the
        endpoints" reading of the existing doubling convention: the
        point on the road is the line's midpoint, and both ends sit the
        same distance away from it.
        """
        segs = split_boundary_to_segments(boundary)
        if not segs:
            return None, None, None, None, None

        best = None  # (width, seg_idx, covered_piece, road_in_zone)
        for seg_idx, seg in enumerate(segs):
            covered_piece, road_in_zone = _edge_covered_portion_and_road(
                seg, road_union, tol=ROAD_FRONT_TOLERANCE
            )
            if covered_piece is None:
                continue

            raw_distance = covered_piece.distance(road_in_zone)

            # Defensive safeguard -- see docstring Step 3 above.
            if raw_distance > MAX_ROAD_DISTANCE:
                continue

            width = raw_distance * 2
            if best is None or width < best[0]:
                best = (width, seg_idx, covered_piece, road_in_zone)

        if best is None:
            return None, None, None, None, None

        width, seg_idx, covered_piece, road_in_zone = best
        width = round(width, 4)

        # Build the Visual Measurement line from the EXACT geometry that produced the
        # winning measurement -- see docstring above.
        pt_on_edge, pt_on_road = nearest_points(covered_piece, road_in_zone)
        dx = pt_on_road.x - pt_on_edge.x
        dy = pt_on_road.y - pt_on_edge.y
        qa_line = LineString([
            pt_on_edge,
            Point(pt_on_edge.x + 2 * dx, pt_on_edge.y + 2 * dy),
        ])

        # ROAD_TYPE / ROAD_NAME attribution: road_union has no attribute
        # table (see note above process()'s road_union comment), so
        # these are looked up from the ORIGINAL road_gdf rows -- confined
        # to the SAME zone used to detect this winning segment's
        # frontage, so the attribution stays local to the actual road
        # that produced the measurement. Only done once, for the winning
        # segment -- not for every candidate.
        road_type_val = None
        road_name_val = None
        if road_type_col or road_name_col:
            zone = segs[seg_idx].buffer(ROAD_FRONT_TOLERANCE, cap_style=2)
            matching = road_gdf[road_gdf.geometry.intersects(zone)]
            if not matching.empty:
                if len(matching) > 1:
                    dists = matching.geometry.distance(covered_piece)
                    nearest_row = matching.loc[dists.idxmin()]
                else:
                    nearest_row = matching.iloc[0]
                if road_type_col:
                    road_type_val = nearest_row[road_type_col]
                if road_name_col:
                    road_name_val = nearest_row[road_name_col]

        return width, seg_idx, qa_line, road_type_val, road_name_val

    # ------------------------------------------------------------------
    # Inner-Lot skip (Road Classification -> "Use LOT_LOCATION"
    # mode only -- see resolve_classification()). Reindexed onto
    # barangay_gdf's own index, then converted to a plain positional
    # boolean array so it can be checked by row position inside the
    # geometry loop below, same convention as skip_arr in
    # road_frontage.py's process_frontage_single().
    #
    # Rows flagged True have their width measurement bypassed entirely
    # and receive ROAD_WIDTH = None. This deliberately follows
    # road_width.py's OWN existing convention for "not computed" --
    # every other bail-out in this function (null/empty geometry,
    # invalid geometry that buffers to empty, no usable boundary
    # segments, no roads at all in the layer) already appends None, not
    # 0.0. ROAD_WIDTH is a distance-in-metres metric, not a
    # frontage-length -- unlike road_frontage.py's ROAD_FRONTAGE, a
    # value of 0.0 here would misleadingly claim "this parcel measured a
    # 0m-wide road," which is not what a skip means. None is the
    # correct, consistent choice for this tool specifically.
    # ------------------------------------------------------------------
    skip_mask = classification.get("skip_mask")
    if skip_mask is not None:
        skip_arr = skip_mask.reindex(barangay_gdf.index).fillna(False).to_numpy()
    else:
        skip_arr = None

    # ── Measure every parcel ──────────────────────────────────────
    road_widths = []
    qa_records = []
    for idx, poly in enumerate(barangay_gdf.geometry):
        if progress_cb:
            progress_cb(1)

        if skip_arr is not None and skip_arr[idx]:
            road_widths.append(None)
            continue

        if poly is None or poly.is_empty:
            road_widths.append(None)
            continue

        if not poly.is_valid:
            # Local-scope repair only -- reassigning `poly` here never
            # writes back into barangay_gdf's own geometry column, so
            # the exported output always keeps the parcel's original
            # shape regardless of what happens here. Two-step repair
            # (buffer(0), then make_valid() if that alone wasn't
            # enough), matching lot_location.py's fix_geometry() --
            # buffer(0) alone doesn't always fully repair severely
            # broken geometry.
            poly = poly.buffer(0)
            if not poly.is_valid:
                poly = make_valid(poly)
            if poly is None or poly.is_empty:
                road_widths.append(None)
                continue

        width, front_seg_idx, qa_line, road_type_val, road_name_val = _measure_width(poly, poly.boundary)
        road_widths.append(width)

        if width is not None:
            record = {"CAMA_ROAD_WIDTH": width, "FRONT_SEGMENT": front_seg_idx, "geometry": qa_line}
            if id_col:
                record["PIN"] = barangay_gdf.iloc[idx][id_col]
            if road_type_col:
                record["ROAD_TYPE"] = road_type_val
            if road_name_col:
                record["ROAD_NAME"] = road_name_val
            qa_records.append(record)

    barangay_gdf[output_column_name] = road_widths

    # ------------------------------------------------------------------
    # Visual Measurement layer. NOT another computation -- a visualization
    # of the exact geometry the production algorithm already selected as
    # the winning measurement for each parcel (see _measure_width()'s
    # docstring). Each Visual Measurement line's own length in QGIS equals that parcel's
    # ROAD_WIDTH value exactly. Separate output layer -- written by
    # run_processing() alongside, never merged into, the main parcel
    # output, so it can be toggled on/off independently in QGIS while
    # validating results.
    # ------------------------------------------------------------------
    if qa_records:
        qa_gdf = gpd.GeoDataFrame(qa_records, geometry="geometry", crs=barangay_gdf.crs)
        qa_gdf = qa_gdf[qa_columns]
    else:
        qa_gdf = gpd.GeoDataFrame(columns=qa_columns, geometry="geometry", crs=barangay_gdf.crs)

    if original_crs:
        barangay_gdf = barangay_gdf.to_crs(original_crs)
        qa_gdf = qa_gdf.to_crs(original_crs)

    return barangay_gdf, qa_gdf

# ----------------- SINGLE MAIN WINDOW -----------------
# Drop-in replacement for the entire open_main_window function in road_width.py
# Key fix: all toggle functions are defined BEFORE any widget references them,
# and toggle is explicitly called after widget creation to set initial state.

def open_main_window(root):

    win = tk.Toplevel(root)
    win.title("Road Width Tool")
    win.resizable(False, False)
    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))

    # ── state ────────────────────────────────────────────────────
    # master=win is REQUIRED in frozen EXE — StringVar() without master
    # binds to tk._default_root which may be a different Tk instance,
    # causing radio buttons to never update the variable.
    parcel_source_type = tk.StringVar(master=win, value="local")
    road_source_type   = tk.StringVar(master=win, value="local")
    output_dest_type   = tk.StringVar(master=win, value="local")

    # Single-selection architecture: one local file and one DB table
    # may exist in memory at any time. Authority variables -- all GUI
    # labels and run-button state are derived from them, never the reverse.
    parcel_local_path = None   # authority: single local file path
    parcel_db_table   = None   # authority: single DB table name
    road_local_path    = tk.StringVar(master=win)
    # road_db_table: DEDICATED var for the DB-mode road selection.
    # road_width.py originally reused road_local_path for both local
    # file path AND db table name -- this looked like a harmless
    # "least structural change" at first, but it's an actual bug: when
    # switching Local -> Database, the stale local path stays in
    # road_local_path, gets misread as "a table is already selected",
    # and a background read tries to query that path string as a table
    # name -- which fails and silently wipes the Filter by Road Type
    # checklist. Splitting this into two vars (matching
    # road_frontage.py's actual, bug-free road_local_path +
    # road_db_table design) fixes it at the source.
    road_db_table       = tk.StringVar(master=win)
    output_local_dir   = tk.StringVar(master=win)

    parcel_files_var = tk.StringVar(master=win, value="No file selected")
    parcel_db_var    = tk.StringVar(master=win, value="No table selected")
    road_file_var    = tk.StringVar(master=win, value="No file selected")
    road_db_var      = tk.StringVar(master=win, value="No table selected")
    output_dir_var   = tk.StringVar(master=win, value="No folder selected")
    output_db_var    = tk.StringVar(master=win, value="Will write back to the connected PostGIS schema.")

    PAD = dict(padx=8, pady=4)

    # ── Road Classification state (new) ─────────────────────────
    #   - parcel_classification_vars: {path_or_table: tk.BooleanVar} --
    #     one checkbox PER selected Land Parcel source that has a usable
    #     LOT_LOCATION column. Lives under Land Parcel Source.
    #     Sources without a usable column get no checkbox at all.
    #   - filter_road_type_var: "Filter by Road Type" -- lives under Road
    #     Network Source, since it depends entirely on the ROAD layer's
    #     columns.
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
    # for the Filter by Road Type checklist (checked = keep, unchecked =
    # exclude). No Select All / Unselect All controls -- matches the
    # canonical reference implementation exactly.
    road_type_value_vars = {}

    # run_status_var: drives the always-visible "Ready to run." / "Reading
    # ..." / "Please select ..." label below the Run button, and gates
    # whether the Run button itself is enabled (_update_run_button_state()
    # below).
    run_status_var = tk.StringVar(master=win, value="Preparing…")

    # Background-read state for the two new inspection reads (parcel ->
    # LOT_LOCATION detection + ROAD_WIDTH column conflict detection,
    # merged into one read -- see _refresh_parcel_classification() --,
    # and road -> ROAD_TYPE detection).
    # Plain closure locals, mutated via `nonlocal` from the nested
    # functions below -- never touched from a worker thread, only from
    # win.after() polling on the main thread.
    road_is_reading = False
    parcel_is_reading = False
    # parcel_read_details: per-source breakdown from the most recent
    # background read -- list of (path_or_table, state, col_name, kind,
    # road_width_existing_col) tuples, one per selected parcel source.
    parcel_read_details = []
    # parcel_road_width_conflicts: derived from parcel_read_details (see
    # _check_parcel_road_width_conflicts() below) -- list of (path_or_table,
    # existing_col_name) tuples, one per source with an existing
    # ROAD_WIDTH-like column. Consumed by the single combined
    # confirmation dialog in on_run().
    parcel_road_width_conflicts = []

    # _suppress_mutual_exclusion: guards against the circular cascade
    # between the two mutual-exclusion trace callbacks below. See
    # _on_parcel_classification_checkbox_changed() / _on_filter_road_type_changed()
    # docstrings for the exact bug this prevents.
    _suppress_mutual_exclusion = False

    # ── section label helper ─────────────────────────────────────
    def section_label(parent, text):
        frm = tk.Frame(parent)
        frm.pack(fill="x", padx=10, pady=(10, 2))
        tk.Label(frm, text=text, font=("Segoe UI", 9, "bold")).pack(side="left")
        ttk.Separator(frm, orient="horizontal").pack(
            side="left", fill="x", expand=True, padx=(6, 0), pady=4)

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
        classification checklist growing/shrinking, and the road-type
        checklist growing/shrinking -- combined with
        win.resizable(False, False) above.

        resizable() itself is never re-toggled: directly measuring
        winfo_reqwidth()/reqheight() (after update_idletasks()) and
        setting minsize/maxsize/geometry to that exact value avoids both
        the "window gets stuck too small" bug and the "window decoration
        flicker" that toggling resizable() causes on Windows.
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
        Rebuilds the Filter by Road Type checklist from
        road_type_value_vars: one Checkbutton per distinct ROAD_TYPE
        value found in the selected road layer.

        Plain destroy-and-repopulate, called directly by the CALLER
        before _update_road_classification_visibility() (never by that
        function itself -- see its own docstring for why). Simpler than
        the previous "build off-screen, swap, destroy old" approach --
        that complexity existed only to avoid a brief empty-content
        moment affecting the WINDOW's own size; now that
        road_type_checklist_container lives inside a fixed/capped Canvas
        (see its construction above), clearing and repopulating its
        children in place can never change the window's size at all, so
        there's nothing left to protect against.
        """
        for child in road_type_checklist_container.winfo_children():
            child.destroy()
        for display_text in sorted(road_type_value_vars.keys()):
            real_value, var = road_type_value_vars[display_text]
            tk.Checkbutton(road_type_checklist_container, text=display_text,
                           variable=var).pack(anchor="w")

    def _on_parcel_classification_checkbox_changed(*_args):
        """
        Mutual exclusion: checking ANY per-source "Use LOT_LOCATION/
        LOT_LABEL" checkbox un-checks "Filter by Road Type" if it was on.
        Multiple per-source checkboxes CAN be checked together -- this
        only fires the OTHER direction (toward Filter by Road Type), so
        checking a second per-source box while a first is already
        checked does not affect either of them.

        Operation order (kept identical to _on_filter_road_type_changed()
        below on purpose):
          1. Guarded mutual-exclusion mutation
          2. Cache synchronization (no-op here -- parcel_classification_vars
             and _parcel_classification_cache[...]["vars"] already share
             the same BooleanVar objects by reference)
          3. Visibility refresh
          4. Run button update

        _suppress_mutual_exclusion guards ONLY step 1 -- NOT the whole
        callback. Steps 2-4 must always run even when this callback was
        re-entered (nested) while already suppressed, or a genuine
        needed UI refresh gets silently skipped.
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
        # Step 3: visibility refresh.
        _update_road_classification_visibility()
        # Step 4: run button update.
        _update_run_button_state()

    def _rebuild_lot_classification_checklist(reuse_vars=None):
        """
        Rebuilds the per-source classification checklist from
        parcel_read_details: one checkbox per selected parcel source
        that has a usable LOT_LOCATION column (state ==
        LOT_STATE_FOUND). Sources without a usable column are omitted
        entirely -- not shown with a "not found" line. If no source
        qualifies at all, the box is simply left empty -- its own
        visibility/height is decided separately by
        _update_parcel_classification_visibility(), not by whether it
        happens to have children.

        Checkbox label is "Use <col_name> in <filename/table>" (e.g.
        "Use LOT_LABEL in Barangay_123.gpkg") -- filename/table name
        only, not the full path. Sufficient to disambiguate between
        multiple sources at a glance: a single Browse action always
        selects files from exactly one folder and REPLACES the previous
        selection, and no filesystem allows duplicate filenames within
        one folder (nor duplicate table names within one schema), so
        this name is always guaranteed unique among the currently
        selected sources.

        reuse_vars: optional {path_or_table: tk.BooleanVar} to reuse
        instead of creating fresh ones -- used by the cache-hit path in
        _refresh_parcel_classification() so toggling Local <-> Database
        back to an unchanged selection restores each checkbox's
        checked/unchecked state exactly as the user left it.

        Plain destroy-and-repopulate, called directly by the CALLER
        before _update_parcel_classification_visibility() (never by that
        function itself, which only handles the "Reading..." placeholder
        content -- see its own docstring for why the split is this way).
        Simpler than the previous "build off-screen, swap, destroy old"
        approach -- that complexity existed only to avoid a brief empty-
        content moment affecting the WINDOW's own size; now that
        lot_classification_list_container lives inside a fixed-height
        Canvas (see its construction above), clearing and repopulating
        its children in place can never change the window's size at all,
        so there's nothing left to protect against.
        """
        for child in lot_classification_list_container.winfo_children():
            child.destroy()
        new_vars = {}

        for path_or_table, state, col_name, kind, _rw_existing_col in parcel_read_details:
            if state != LOT_STATE_FOUND:
                continue
            if reuse_vars is not None and path_or_table in reuse_vars:
                var = reuse_vars[path_or_table]
            else:
                var = tk.BooleanVar(master=win, value=False)
                var.trace_add("write", _on_parcel_classification_checkbox_changed)
            new_vars[path_or_table] = var

            # os.path.basename() is safe to call unconditionally here
            # even for database table names (which have no path
            # separators) -- it just returns the string unchanged in
            # that case.
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
        a content-adaptive scrollable Canvas -- see its construction
        above) is shown at all, and if so, resizes it to fit its current
        content (capped -- see _resize_lot_classification_box()). Hidden
        entirely both when no Land Parcel source is selected, AND when
        one is selected but yields nothing to show (no source has a
        usable classification column) -- an empty, pointlessly-
        scrollable box was worse than just not showing it.

        Deliberately NEVER called while parcel_is_reading -- the "Reading
        parcel..." indicator lives elsewhere now (see
        _set_parcel_reading_state()'s docstring), and this box is left
        completely UNTOUCHED for the entire duration of a background
        read: if it was already showing a previous file's checklist, it
        stays exactly as it was until the new read's actual result is
        known. This function is only ever invoked once that result is
        ready (or immediately, for the synchronous no-sources/cache-hit
        paths), so it triggers at most ONE resize per call -- never a
        second one layered close in time on top of an earlier "entering
        reading" resize, which is what made the previously reported
        distortion worse rather than better.

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

        # Pack BEFORE resizing -- _resize_lot_classification_box() reads
        # lot_classification_canvas.winfo_width() to decide whether
        # horizontal scrolling is needed, and that value is meaningless
        # (returns 1, not the real layout-derived width) until the
        # canvas is actually packed into the window at least once.
        # Packing first, then resizing, guarantees a valid width to
        # compare against.
        if not lot_classification_outer.winfo_ismapped():
            lot_classification_outer.pack(
                fill="x", pady=(2, 0), after=parcel_action_row)
        _resize_lot_classification_box()
        _reflow_window()

    def _update_road_classification_visibility():
        """
        Shows the "Filter by Road Type" checkbox plus (if checked) its
        per-value checklist. No usable ROAD_TYPE-like column found shows
        neither.

        Deliberately a no-op while road_is_reading -- the "Reading Road
        Network..." indicator lives elsewhere now (see
        _set_road_reading_state()'s docstring: it reuses the existing
        "No file selected"/filename label via text swap, needing zero
        _reflow_window() calls of its own), and this function's own
        checkbox/checklist state is left completely UNTOUCHED for the
        entire duration of a background read -- if it was already
        showing a previous file's Filter by Road Type checklist, it
        stays exactly as it was until the new read's actual result is
        known (see _refresh_road_classification()'s cache-miss branch,
        which deliberately does not clear road_type_value_vars before
        starting the thread). This function is only ever invoked once
        that result is ready (or immediately, for the synchronous
        no-selection/cache-hit paths), so it triggers at most ONE resize
        per call -- matching the same "one resize per cycle" principle
        established on the Land Parcel side.
        """
        if road_is_reading:
            return
        if road_type_value_vars:
            road_filter_checkbox.pack(anchor="w", pady=(2, 0))
            if filter_road_type_var.get():
                # Pack BEFORE resizing -- see the matching comment in
                # _update_parcel_classification_visibility() for why:
                # winfo_width() is meaningless until the canvas has
                # actually been packed into the window at least once.
                if not road_type_checklist_outer.winfo_ismapped():
                    road_type_checklist_outer.pack(
                        fill="x", padx=(20, 0), pady=(2, 0), after=road_filter_checkbox)
                _resize_road_type_checklist_box()
            else:
                road_type_checklist_outer.pack_forget()
        else:
            road_filter_checkbox.pack_forget()
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
          2. Cache synchronization -- mirrors the live checked-state into
             the currently active Road Network mode's cache slot
             (_road_gdf_cache), if that slot already has cached data.
          3. Visibility refresh
          4. Run button update

        _suppress_mutual_exclusion guards ONLY step 1 -- NOT the whole
        callback (same reasoning as the sibling callback above).
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
        (parcel_lbl_widget, bound to parcel_files_var / parcel_db_var)
        that's already permanently present in parcel_action_row,
        temporarily overwriting its text via the StringVar and restoring
        it once done. Since this label's own row never changes shape
        because of a text-length change (no fill/expand on it, nothing
        below it repositions), this transition needs -- and gets -- ZERO
        _reflow_window() calls. This replaces an earlier design where
        entering/leaving the reading state showed/hid the classification
        checklist box itself, which meant an extra window resize per
        read cycle; that resize, so close in time to the one at the end
        of the same cycle, was found to make the reported visual
        distortion WORSE, not better. The classification checklist box
        itself is now left completely untouched during reading -- see
        _refresh_parcel_classification() and
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
            # runtime state.
            parcel_files_var.set("⏳ Reading Land Parcel...")
            parcel_db_var.set("⏳ Reading Land Parcel...")
            parcel_lbl_widget.config(fg="#b36b00")
        else:
            # Restore from authority variables -- never from StringVar
            # state. Same pattern as toggle_parcel() below.
            parcel_files_var.set(
                os.path.basename(parcel_local_path) if parcel_local_path
                else "No file selected"
            )
            parcel_db_var.set(
                parcel_db_table if parcel_db_table
                else "No table selected"
            )
            parcel_lbl_widget.config(fg="gray")

    def _set_road_reading_state(reading):
        """
        Disables Road Network Browse/radio controls while its background
        classification read is in progress -- prevents starting a
        second, overlapping read of the same source.

        Also drives the "Reading Road Network..." indicator itself --
        but NOT via a separate widget or any pack()/pack_forget() call.
        Reuses the EXISTING "No file selected" / filename / "No table
        selected" label (road_lbl_widget, bound to road_file_var /
        road_db_var) that's already permanently present in
        road_action_row, temporarily overwriting its text via the
        StringVar and restoring it once done -- exact same principle as
        _set_parcel_reading_state() on the Land Parcel side. Since this
        label's own row never changes shape from a text-length change,
        this transition needs -- and gets -- ZERO _reflow_window() calls.
        """
        state = "disabled" if reading else "normal"
        road_btn.config(state=state)
        road_radio_local.config(state=state)
        road_radio_db.config(state=state)

        if reading:
            road_file_var.set("Reading Road Network...")
            road_db_var.set("Reading Road Network...")
            road_lbl_widget.config(fg="#b36b00")
        else:
            road_path = road_local_path.get()
            road_file_var.set(os.path.basename(road_path) if road_path else "No file selected")
            road_table = road_db_table.get()
            road_db_var.set(road_table if road_table else "No table selected")
            road_lbl_widget.config(fg="gray")

    def _update_run_button_state():
        """
        Single source of truth for whether the Run button may be
        pressed. While a background read (parcel OR road) is still in
        progress, the checkboxes above haven't yet caught up to the true
        effective state, so Run stays disabled until both finish.

        Explicit bg/fg/cursor toggling (not just state=) is required:
        Tkinter does NOT automatically gray out a classic tk.Button's
        custom bg/fg when state="disabled", and does not suppress a
        widget's assigned cursor either -- both must be set explicitly
        for each state.
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

    def _check_parcel_road_width_conflicts(details):
        """
        Extracts the ROAD_WIDTH-conflict subset out of parcel_read_details
        (or an equivalent list, e.g. a cache slot's stored details) --
        one (path_or_table, existing_col_name) tuple per source where the
        merged background read found an existing column matching
        "cama_road_width" (case-insensitive). Sources with no conflict, or
        that failed to read, are simply absent from the result.
        """
        return [
            (path_or_table, rw_col)
            for path_or_table, _state, _col_name, _kind, rw_col in details
            if rw_col is not None
        ]

    def _refresh_parcel_classification(force_refresh=False):
        """
        Background-reads EVERY currently selected Land Parcel file/table
        (not just the first) so the per-source checklist can offer a
        checkbox for every source that actually has a usable
        LOT_LOCATION column -- UNLESS the dual-slot
        _parcel_classification_cache already has a still-valid entry for
        this exact mode+selection, in which case the checklist --
        including each checkbox's checked state -- is restored instantly
        with no read at all. GeoDataFrames are discarded immediately
        after inspection, not cached -- only the tiny per-source
        detection tuples and the BooleanVars are kept.

        This SAME read also checks each source for an existing
        ROAD_WIDTH-like column (case-insensitive) that would collide
        with the column this tool is about to write -- deliberately
        merged into this one pass rather than a second, separate
        background read, since both checks need to open the exact same
        file/table anyway. parcel_road_width_conflicts (a derived list,
        recomputed alongside parcel_read_details below) is consumed by
        the single combined confirmation dialog in on_run() -- unlike
        the LOT_LOCATION checklist above, this check has no GUI checklist
        of its own; it is purely a yes/no warning shown once at Run time.

        force_refresh: when True, skips the cache-hit check entirely and
        always does a fresh read, even if the cache key matches. Must be
        True whenever this is called because the user just ACTIVELY
        selected source(s) via Browse -- if they re-select the exact
        same file(s) (e.g. after editing one externally to add/change
        LOT_LOCATION values or a ROAD_WIDTH column), a plain key match
        would otherwise silently serve the old, now-stale cached
        results. The cache-hit shortcut is only safe to take on the
        toggle_parcel() path (the user didn't select anything new, just
        switched which already-made selection is active), which calls
        this with the default force_refresh=False.
        """
        nonlocal parcel_is_reading, parcel_read_details, parcel_road_width_conflicts
        if parcel_is_reading:
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
            parcel_read_details = []
            parcel_road_width_conflicts = []
            _rebuild_lot_classification_checklist()
            _update_parcel_classification_visibility()
            _update_run_button_state()
            return

        cache_key = tuple(sources)
        slot = _parcel_classification_cache[source_type]
        if not force_refresh and slot["key"] == cache_key and slot["details"] is not None:
            parcel_read_details = slot["details"]
            parcel_road_width_conflicts = _check_parcel_road_width_conflicts(parcel_read_details)
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
            per_source_results = []
            for path_or_table in sources:
                gdf, error = _read_gdf_worker(source_type, path_or_table)
                if error is not None or gdf is None:
                    per_source_results.append((path_or_table, None, None, None, None))
                    continue
                state, col_name, kind, _mask = _detect_lot_classification(gdf)
                road_width_existing_col = next(
                    (c for c in gdf.columns if c.lower() == "cama_road_width"), None
                )
                per_source_results.append((path_or_table, state, col_name, kind, road_width_existing_col))
                del gdf
            result_queue.put(per_source_results)

        parcel_is_reading = True
        _set_parcel_reading_state(True)
        _update_run_button_state()
        threading.Thread(target=worker, daemon=True).start()
        win.after(100, lambda: _poll_parcel_classification_queue(result_queue, source_type, cache_key))

    def _poll_parcel_classification_queue(result_queue, source_type, cache_key):
        nonlocal parcel_is_reading, parcel_read_details, parcel_road_width_conflicts
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
        parcel_road_width_conflicts = _check_parcel_road_width_conflicts(per_source_results)

        failed = [src for (src, state, _c, _k, _rw) in per_source_results if state is None]
        for src in failed:
            print(f"⚠️ Could not read parcel layer for classification check: {src}")

        _rebuild_lot_classification_checklist()

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
        single file/table -- Road Network only ever supports one
        selection) -- UNLESS the dual-slot cache already has a still-
        valid entry for this exact mode+selection, in which case the
        checklist is restored instantly from cache with no read and no
        background thread at all. Populates road_type_value_vars for the
        Filter by Road Type checklist and caches the read gdf in
        _road_gdf_cache.

        force_refresh: same semantics as _refresh_parcel_classification()
        above -- True whenever called from an active Browse selection,
        False on the toggle_road() path.
        """
        nonlocal road_is_reading
        if road_is_reading:
            return

        source_type = road_source_type.get()
        path_or_table = road_local_path.get() if source_type == "local" else road_db_table.get()

        if not path_or_table:
            # Nothing selected for this mode -- nothing to show, nothing
            # to read. No background read is involved, so this is an
            # immediate, synchronous swap to an empty checklist -- not
            # the "reading" transient state at all.
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
            # Three distinct data states, never merged into one bucket.
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

    # ════════════════════════════════════════════════════════════
    #  SECTION 1 — LAND PARCEL
    # ════════════════════════════════════════════════════════════
    section_label(win, "Land Parcel Source")

    parcel_frame = tk.Frame(win)
    parcel_frame.pack(fill="x", padx=18, pady=2)

    parcel_radio_row = tk.Frame(parcel_frame)
    parcel_radio_row.pack(fill="x")

    parcel_action_row = tk.Frame(parcel_frame)
    parcel_action_row.pack(fill="x", pady=2)

    parcel_lbl_widget = tk.Label(
        parcel_action_row, textvariable=parcel_files_var,
        fg="gray", anchor="w", width=42)
    parcel_lbl_widget.pack(side="left")

    parcel_btn = tk.Button(parcel_action_row, text="Browse…", width=10, cursor="hand2")
    parcel_btn.pack(side="left", **PAD)

    # Per-source classification checklist -- one Checkbutton per selected
    # Land Parcel source that has a usable LOT_LOCATION column, built
    # fresh by _rebuild_lot_classification_checklist() after each
    # background read.
    #
    # Content-adaptive height, capped, scrollable when needed (Canvas +
    # Scrollbar): the box sizes itself to fit however many checkboxes
    # are actually present, up to LOT_CLASSIFICATION_MAX_HEIGHT -- past
    # that cap, it stops growing and scrolls internally instead. This is
    # a deliberate middle ground: an earlier version used a permanently
    # FIXED height regardless of content, which avoided all resizing but
    # left an empty, pointlessly-scrollable box visible even when there
    # was nothing to show (0 checkboxes) -- clearly wrong looking. This
    # version instead hides the box ENTIRELY when there's nothing to
    # show, and resizes it (once, cleanly -- see
    # _resize_lot_classification_box()) whenever its content actually
    # changes. That reintroduces occasional, deliberate resizes, but NOT
    # the repeated, rapid-fire resize CASCADE that caused the original
    # visual distortion bug -- this box's own height is recomputed and
    # applied in one shot per state transition, not many times in quick
    # succession.
    LOT_CLASSIFICATION_MAX_HEIGHT = 90  # pixels -- cap; box grows to fit content up to this, then scrolls

    lot_classification_outer = tk.Frame(parcel_frame)
    lot_classification_canvas = tk.Canvas(
        lot_classification_outer, highlightthickness=0, bd=0)
    lot_classification_scrollbar = tk.Scrollbar(
        lot_classification_outer, orient="vertical",
        command=lot_classification_canvas.yview)
    lot_classification_hscroll = tk.Scrollbar(
        lot_classification_outer, orient="horizontal",
        command=lot_classification_canvas.xview)
    lot_classification_canvas.configure(
        yscrollcommand=lot_classification_scrollbar.set,
        xscrollcommand=lot_classification_hscroll.set)
    lot_classification_canvas.pack(side="left", fill="both", expand=True)
    # lot_classification_scrollbar (vertical) and lot_classification_hscroll
    # (horizontal) are both packed/unpacked dynamically by
    # _resize_lot_classification_box() below -- only shown when content
    # actually exceeds the box in that direction and scrolling is
    # genuinely needed (e.g. a very long "Use LOT_LOCATION in
    # <filename>.gpkg" label -- never truncated, never wrapped, scrolls
    # into view instead, same principle already used for long file paths
    # in ask_overwrite_dialog()/show_success_dialog()).

    # lot_classification_list_container: the actual content frame drawn
    # INSIDE the canvas -- this is what _rebuild_lot_classification_checklist()
    # (and the "Reading..." branch of _update_parcel_classification_visibility())
    # clears and repopulates.
    lot_classification_list_container = tk.Frame(lot_classification_canvas)
    _lot_classification_canvas_window = lot_classification_canvas.create_window(
        (0, 0), window=lot_classification_list_container, anchor="nw")

    def _on_lot_classification_content_configure(_event=None):
        lot_classification_canvas.configure(
            scrollregion=lot_classification_canvas.bbox("all"))
    lot_classification_list_container.bind(
        "<Configure>", _on_lot_classification_content_configure)

    def _on_lot_classification_mousewheel(event):
        lot_classification_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    lot_classification_canvas.bind(
        "<Enter>", lambda e: lot_classification_canvas.bind_all(
            "<MouseWheel>", _on_lot_classification_mousewheel))
    lot_classification_canvas.bind(
        "<Leave>", lambda e: lot_classification_canvas.unbind_all("<MouseWheel>"))

    def _resize_lot_classification_box():
        """
        Recomputes lot_classification_canvas's own height AND width
        handling to fit lot_classification_list_container's CURRENT
        content:

        - Vertical: capped at LOT_CLASSIFICATION_MAX_HEIGHT -- past that
          cap, stops growing and scrolls internally instead.
        - Horizontal: the inner content frame is matched to the canvas's
          own displayed width UNLESS its natural required width (the
          widest checkbox label, e.g. a long filename) exceeds that --
          in which case the frame is left at its full natural width and
          a horizontal scrollbar appears, rather than ever truncating or
          wrapping the text.

        Either scrollbar is shown ONLY when genuinely needed in that
        direction (nothing to scroll -> no scrollbar at all, avoiding
        pointless, always-visible scrollbars). Called once per content
        change (a state transition -- reading started, reading finished,
        checklist rebuilt) -- never in a tight loop.
        """
        lot_classification_list_container.update_idletasks()
        content_height = lot_classification_list_container.winfo_reqheight()
        content_width = lot_classification_list_container.winfo_reqwidth()
        canvas_width = lot_classification_canvas.winfo_width()

        if content_height <= LOT_CLASSIFICATION_MAX_HEIGHT:
            lot_classification_canvas.configure(height=content_height)
            lot_classification_scrollbar.pack_forget()
        else:
            lot_classification_canvas.configure(height=LOT_CLASSIFICATION_MAX_HEIGHT)
            lot_classification_scrollbar.pack(side="right", fill="y")

        if content_width > canvas_width:
            lot_classification_canvas.itemconfig(_lot_classification_canvas_window, width=content_width)
            lot_classification_hscroll.pack(side="bottom", fill="x")
        else:
            lot_classification_canvas.itemconfig(_lot_classification_canvas_window, width=canvas_width)
            lot_classification_hscroll.pack_forget()
    # Both start unpacked; _update_parcel_classification_visibility() (via
    # _refresh_parcel_classification()) decides what to show.

    # ── parcel browse callbacks ───────────────────────────────────
    def browse_parcel_files():
        file = filedialog.askopenfilename(filetypes=[
            ("Shapefiles", "*.shp"),
            ("GeoPackage", "*.gpkg"),
            ("All", "*.*")])
        # Cancel returns "" -- do not assign, preserving previous selection.
        if file:
            nonlocal parcel_local_path
            parcel_local_path = file
            parcel_files_var.set(os.path.basename(file))
            # A new Land Parcel selection invalidates any prior
            # LOT_LOCATION detection -- re-inspect it.
            # Deliberately NOT calling _reflow_window() here: doing so
            # BEFORE the old checklist is cleared would freeze the
            # window (inside _refresh_parcel_classification() below) at
            # a "hybrid" size -- new label text + stale checklist widget
            # count -- which then visibly jumps/distorts once the real
            # read finishes and the checklist changes count. Resizing
            # happens exactly once, only after the read is confirmed
            # complete and the final checkbox set is known (see
            # _update_parcel_classification_visibility()).
            # force_refresh=True: the user actively chose this selection
            # just now via Browse -- must be read fresh, never served
            # from cache.
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
            # Only called on confirmed selection (Confirm button in
            # _pick_db_tables) -- Cancel never calls on_select, so
            # parcel_db_table retains its previous value automatically.
            nonlocal parcel_db_table
            parcel_db_table = sel[0]
            parcel_db_var.set(sel[0])
            # See browse_parcel_files() above for why _reflow_window()
            # is deliberately NOT called here.
            # force_refresh=True: actively chosen just now via the table
            # picker -- never served from cache, same reasoning as
            # browse_parcel_files() above.
            _refresh_parcel_classification(force_refresh=True)

        _pick_db_tables(win, tables, multi=False, on_select=_on_parcel_tables_selected)

    # ── parcel toggle ─────────────────────────────────────────────
    def toggle_parcel(*_):
        # Always render from authority variables -- never from StringVar state.
        # Guarantees Local → DB → Local always restores the original selection.
        mode = parcel_source_type.get()
        if mode == "local":
            parcel_lbl_widget.config(textvariable=parcel_files_var,
                                     font=("Segoe UI", 9))
            parcel_btn.config(text="Browse…", command=browse_parcel_files)
            parcel_files_var.set(
                os.path.basename(parcel_local_path) if parcel_local_path
                else "No file selected"
            )
        else:
            parcel_lbl_widget.config(textvariable=parcel_db_var,
                                     font=("Segoe UI", 9))
            parcel_btn.config(text="Select…", command=browse_parcel_db)
            parcel_db_var.set(
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

    # ── parcel radio buttons (command wired AFTER toggle defined) ─
    parcel_radio_local = tk.Radiobutton(parcel_radio_row, text="Local File",
                   variable=parcel_source_type, value="local",
                   command=toggle_parcel)
    parcel_radio_local.pack(side="left")
    parcel_radio_db = tk.Radiobutton(parcel_radio_row, text="Database Table",
                   variable=parcel_source_type, value="db",
                   command=toggle_parcel)
    parcel_radio_db.pack(side="left", padx=(12, 0))

    # ════════════════════════════════════════════════════════════
    #  SECTION 2 — ROAD NETWORK
    # ════════════════════════════════════════════════════════════
    section_label(win, "Road Network Source")

    road_frame = tk.Frame(win)
    road_frame.pack(fill="x", padx=18, pady=2)

    road_radio_row = tk.Frame(road_frame)
    road_radio_row.pack(fill="x")

    road_action_row = tk.Frame(road_frame)
    road_action_row.pack(fill="x", pady=2)

    road_lbl_widget = tk.Label(
        road_action_row, textvariable=road_file_var,
        fg="gray", anchor="w", width=42)
    road_lbl_widget.pack(side="left")

    road_btn = tk.Button(road_action_row, text="Browse…", width=10, cursor="hand2")
    road_btn.pack(side="left", **PAD)

    # "Filter by Road Type" checkbox -- created once, only packed/unpacked
    # (never destroyed) by _update_road_classification_visibility().
    road_filter_checkbox = tk.Checkbutton(
        road_frame, text="Filter by Road Type", variable=filter_road_type_var)

    # Holds one Checkbutton per unique ROAD_TYPE value found in the
    # currently selected road layer. Only packed while the checkbox above
    # is checked AND a usable ROAD_TYPE-like column was found.
    #
    # Content-adaptive height, capped, dual-scroll (vertical + horizontal)
    # when needed -- identical construction/rationale to the Land Parcel
    # classification checklist above (see LOT_CLASSIFICATION_MAX_HEIGHT's
    # comment for the full "why a cap, why hide when empty, why this
    # avoids the resize-cascade distortion bug" explanation -- same
    # principle applies here). Horizontal scroll specifically matters
    # here since some ROAD_TYPE values in real cadastral data can be
    # long descriptive strings, not just short codes -- never truncated
    # or wrapped, only ever scrolled into view.
    ROAD_TYPE_CHECKLIST_MAX_HEIGHT = 90  # pixels -- same cap as the Land Parcel checklist

    road_type_checklist_outer = tk.Frame(road_frame)
    road_type_checklist_canvas = tk.Canvas(
        road_type_checklist_outer, highlightthickness=0, bd=0)
    road_type_checklist_vscroll = tk.Scrollbar(
        road_type_checklist_outer, orient="vertical",
        command=road_type_checklist_canvas.yview)
    road_type_checklist_hscroll = tk.Scrollbar(
        road_type_checklist_outer, orient="horizontal",
        command=road_type_checklist_canvas.xview)
    road_type_checklist_canvas.configure(
        yscrollcommand=road_type_checklist_vscroll.set,
        xscrollcommand=road_type_checklist_hscroll.set)
    road_type_checklist_canvas.pack(side="left", fill="both", expand=True)
    # Both scrollbars are packed/unpacked dynamically by
    # _resize_road_type_checklist_box() below -- only shown when content
    # actually exceeds the box in that direction.

    # road_type_checklist_container: the actual content frame drawn
    # INSIDE the canvas -- this is what _rebuild_road_type_checklist()
    # clears and repopulates.
    road_type_checklist_container = tk.Frame(road_type_checklist_canvas)
    _road_type_checklist_canvas_window = road_type_checklist_canvas.create_window(
        (0, 0), window=road_type_checklist_container, anchor="nw")

    def _on_road_type_checklist_content_configure(_event=None):
        road_type_checklist_canvas.configure(
            scrollregion=road_type_checklist_canvas.bbox("all"))
    road_type_checklist_container.bind(
        "<Configure>", _on_road_type_checklist_content_configure)

    def _on_road_type_checklist_mousewheel(event):
        road_type_checklist_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    road_type_checklist_canvas.bind(
        "<Enter>", lambda e: road_type_checklist_canvas.bind_all(
            "<MouseWheel>", _on_road_type_checklist_mousewheel))
    road_type_checklist_canvas.bind(
        "<Leave>", lambda e: road_type_checklist_canvas.unbind_all("<MouseWheel>"))

    def _resize_road_type_checklist_box():
        """
        Recomputes road_type_checklist_canvas's own height and width
        handling to fit road_type_checklist_container's CURRENT content
        -- identical logic to _resize_lot_classification_box() (see its
        docstring for the full rationale). Called once per content
        change, never in a tight loop.
        """
        road_type_checklist_container.update_idletasks()
        content_height = road_type_checklist_container.winfo_reqheight()
        content_width = road_type_checklist_container.winfo_reqwidth()
        canvas_width = road_type_checklist_canvas.winfo_width()

        if content_height <= ROAD_TYPE_CHECKLIST_MAX_HEIGHT:
            road_type_checklist_canvas.configure(height=content_height)
            road_type_checklist_vscroll.pack_forget()
        else:
            road_type_checklist_canvas.configure(height=ROAD_TYPE_CHECKLIST_MAX_HEIGHT)
            road_type_checklist_vscroll.pack(side="right", fill="y")

        if content_width > canvas_width:
            road_type_checklist_canvas.itemconfig(_road_type_checklist_canvas_window, width=content_width)
            road_type_checklist_hscroll.pack(side="bottom", fill="x")
        else:
            road_type_checklist_canvas.itemconfig(_road_type_checklist_canvas_window, width=canvas_width)
            road_type_checklist_hscroll.pack_forget()
    # Both start unpacked; _update_road_classification_visibility()
    # (via _refresh_road_classification()) decides what to show.

    # ── road browse callbacks ─────────────────────────────────────
    def browse_road_file():
        f = filedialog.askopenfilename(filetypes=[
            ("Shapefiles", "*.shp"),
            ("GeoPackage", "*.gpkg"),
            ("All", "*.*")])
        if f:
            road_local_path.set(f)
            road_file_var.set(os.path.basename(f))
            # See browse_parcel_files() above for why _reflow_window()
            # is deliberately NOT called here -- same reasoning applies
            # to the Road Type checklist.
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
            road_db_table.set(sel[0])
            road_db_var.set(sel[0])
            # See browse_parcel_files() above for why _reflow_window()
            # is deliberately NOT called here.
            _refresh_road_classification(force_refresh=True)

        _pick_db_tables(win, tables, multi=False, on_select=_on_road_table_selected)

    # ── road toggle ───────────────────────────────────────────────
    def toggle_road(*_):
        mode = road_source_type.get()
        if mode == "local":
            road_lbl_widget.config(textvariable=road_file_var,
                                   font=("Segoe UI", 9))
            road_btn.config(text="Browse…", command=browse_road_file)
        else:
            road_lbl_widget.config(textvariable=road_db_var,
                                   font=("Segoe UI", 9))
            road_btn.config(text="Select…", command=browse_road_db)
        # Switching Local <-> Database does NOT clear the other mode's
        # remembered selection or cached checklist state -- that's the
        # whole point of the dual-slot _road_gdf_cache.
        _refresh_road_classification()

    # ── road radio buttons ────────────────────────────────────────
    road_radio_local = tk.Radiobutton(road_radio_row, text="Local File",
                   variable=road_source_type, value="local",
                   command=toggle_road)
    road_radio_local.pack(side="left")
    road_radio_db = tk.Radiobutton(road_radio_row, text="Database Table",
                   variable=road_source_type, value="db",
                   command=toggle_road)
    road_radio_db.pack(side="left", padx=(12, 0))

    # ════════════════════════════════════════════════════════════
    #  SECTION 3 — OUTPUT
    # ════════════════════════════════════════════════════════════
    section_label(win, "Output Destination")

    output_frame = tk.Frame(win)
    output_frame.pack(fill="x", padx=18, pady=2)

    out_radio_row = tk.Frame(output_frame)
    out_radio_row.pack(fill="x")

    out_action_row = tk.Frame(output_frame)
    out_action_row.pack(fill="x", pady=2)

    out_lbl_widget = tk.Label(
        out_action_row, textvariable=output_dir_var,
        fg="gray", anchor="w", width=42)
    out_lbl_widget.pack(side="left")

    out_btn = tk.Button(out_action_row, text="Browse…", width=10, cursor="hand2")
    out_btn.pack(side="left", **PAD)

    # ── output browse callback ────────────────────────────────────
    def browse_output_dir():
        d = filedialog.askdirectory()
        if d:
            output_local_dir.set(d)
            output_dir_var.set(d)
            _update_run_button_state()

    # ── output toggle ─────────────────────────────────────────────
    def toggle_output(*_):
        mode = output_dest_type.get()
        if mode == "local":
            out_lbl_widget.config(textvariable=output_dir_var,
                                  font=("Segoe UI", 9), fg="gray")
            out_btn.config(text="Browse…", command=browse_output_dir)
            out_btn.pack(side="left", **PAD)
        else:
            out_lbl_widget.config(textvariable=output_db_var,
                                  font=("Segoe UI", 8, "italic"), fg="gray")
            out_btn.pack_forget()
        _update_run_button_state()

    # ── output radio buttons ──────────────────────────────────────
    tk.Radiobutton(out_radio_row, text="Save to Local Folder",
                   variable=output_dest_type, value="local",
                   command=toggle_output).pack(side="left")
    tk.Radiobutton(out_radio_row, text="Save to Database",
                   variable=output_dest_type, value="db",
                   command=toggle_output).pack(side="left", padx=(12, 0))

    # ════════════════════════════════════════════════════════════
    #  RUN BUTTON
    # ════════════════════════════════════════════════════════════
    ttk.Separator(win, orient="horizontal").pack(fill="x", padx=10, pady=(12, 4))

    def on_run():
        global barangay_source, road_source, output_mode
        global parcel_classification_selection, filter_by_road_type_active, road_type_excluded_values
        global parcel_road_width_column_overrides

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

        # validate output
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
        # consumes them from there.
        #
        # Belt-and-suspenders: the Run button is disabled while either
        # background read is in progress (_update_run_button_state()),
        # so this branch should be unreachable in normal use -- kept as
        # a hard stop in case on_run() is ever invoked some other way
        # while a read is still running.
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

        # Warn about any Land Parcel source(s) that already have a
        # column matching "cama_road_width" (case-insensitive) -- this tool
        # is about to write its computed ROAD_WIDTH into that column.
        # Shown once, combined across every affected source (not one
        # dialog per file mid-processing), only here at Run time -- never
        # at Browse time, and never as a console-only message, since a
        # user running the compiled EXE without a terminal open would
        # never see one. Declining cancels the run entirely rather than
        # skipping just the affected source(s), so the user always knows
        # exactly what did or didn't happen instead of a partial batch
        # silently going through.
        if parcel_road_width_conflicts:
            lines = "\n".join(
                f"- '{os.path.basename(path_or_table)}' already has a '{existing_col}' column"
                for path_or_table, existing_col in parcel_road_width_conflicts
            )
            proceed = messagebox.askyesno(
                "Existing CAMA_ROAD_WIDTH column found",
                f"{lines}\n\n"
                "Processing will overwrite the existing column(s) with the "
                "newly computed values.\n\nProceed?"
            )
            if not proceed:
                return
            # Preserve each source's existing column name/casing exactly
            # -- e.g. a detected "cama_road_width" (lowercase) is written back
            # to "cama_road_width", not a hardcoded "CAMA_ROAD_WIDTH" -- so no
            # duplicate column is ever created regardless of the existing
            # casing. A source with no entry here (no conflict was found)
            # simply uses the default name in process() below.
            parcel_road_width_column_overrides = dict(parcel_road_width_conflicts)
        else:
            parcel_road_width_column_overrides = {}

        # PRIORITY 2: file conflict check -- warn if an output file with
        # the same name already exists in the chosen output folder.
        # Resolved here on the main thread, before win.destroy(), so:
        #   (a) win is still live, giving the dialog a proper parent, and
        #   (b) the user can cancel without losing the configuration window.
        # overwrite_mode is passed explicitly to run_processing() as a
        # parameter -- no module-level global needed.
        overwrite_mode = None
        if output_mode[0] == "local":
            desired_names = [
                os.path.splitext(os.path.basename(p))[0] for p in barangay_source[1]
            ] if barangay_source[0] == "local" else list(barangay_source[1])
            conflicting_names = [
                f"{name}.gpkg" for name in desired_names
                if os.path.exists(os.path.join(output_mode[1], f"{name}.gpkg"))
            ]
            if conflicting_names:
                overwrite_mode = ask_overwrite_dialog(win, conflicting_names)
                if overwrite_mode == "cancel":
                    print("Run cancelled by user (existing output file(s) found).")
                    return

        win.destroy()
        run_processing(root, overwrite_mode)

    run_btn = tk.Button(win, text="▶  Run Processing", command=on_run,
              bg="#2e7d32", fg="white", font=("Segoe UI", 10, "bold"),
              relief="flat", padx=16, pady=6)
    run_btn.pack(pady=(4, 4))

    # Permanent status line UNDER the Run button -- always visible, no
    # hover required.
    run_status_lbl = tk.Label(win, textvariable=run_status_var,
                              font=("Segoe UI", 8), fg="gray")
    run_status_lbl.pack(pady=(0, 12))

    # ── apply initial toggle state so buttons have correct commands ──
    toggle_parcel()
    toggle_road()
    toggle_output()
    _update_parcel_classification_visibility()
    _update_road_classification_visibility()
    _update_run_button_state()


# ── shared DB table picker (used by both parcel and road) ────────
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

def select_output_window(root):
    win = tk.Toplevel(root)
    win.title("Select Output Destination")

    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))


    # keep size consistent with the other windows
    win.resizable(False, False)

    def save_local():
        global output_mode, barangay_source, road_source
        if not barangay_source or not road_source:
            messagebox.showerror("Error", "Barangay and Road must be selected first.")
            return
        out_dir = filedialog.askdirectory()
        if out_dir:
            output_mode = ("local", out_dir)
            print("✅ Output mode set:", output_mode)
            win.destroy()
            run_processing(root)

    def save_db():
        global output_mode, barangay_source, road_source
        if not barangay_source or not road_source:
            messagebox.showerror("Error", "Barangay and Road must be selected first.")
            return
        output_mode = ("db", None)
        print("✅ Output mode set:", output_mode)
        win.destroy()
        run_processing(root)

    # 🔹 SIDE-BY-SIDE buttons (same layout & size)
    btn_frame = tk.Frame(win)
    btn_frame.pack(padx=25, pady=10)  # 👈 SAME padding as other windows

    tk.Button(
        btn_frame,
        text="Save to Local",
        command=save_local,
        width=18
    ).pack(side=tk.LEFT, padx=5)

    tk.Button(
        btn_frame,
        text="Save to Database",
        command=save_db,
        width=18
    ).pack(side=tk.LEFT, padx=5)

# ----------------- SUCCESS DIALOG -----------------
def show_success_dialog(parent, total_sources, failed_sources, single_success_detail=None):
    """
    "Processing complete" summary dialog -- or, for the one specific
    case of a single-source run that failed, a "Processing failed"
    dialog instead (see single_failure below). Deliberately does NOT
    list every successful source individually -- with a batch of many
    sources, a full list of every filename that simply worked adds
    length without adding information the user needs to act on; the
    total count at the top already answers "did it work." Only
    FAILURES are listed individually, since those are exactly the
    entries a user needs to identify and act on (fix the source, then
    re-run just that one).

    Not a general-purpose replacement for messagebox elsewhere in this
    file -- just for this one summary, which needs something messagebox
    structurally can't do: messagebox has no scrolling and no height/
    width cap of its own -- it just grows to fit whatever text it's
    given. With many failed sources, or long reason text, the content
    could grow tall or wide enough to push the OK button off the
    visible screen, with no way to scroll or resize to recover.

    This dialog fixes that with:
      1. The OK button packed FIRST, side="bottom" -- guaranteed to stay
         visible/reachable regardless of how tall the content above it
         wants to be.
      2. The failed-sources list lives in a height-CAPPED (in text
         lines, not pixels), scrollable Text widget -- BOTH vertically
         (many entries) and horizontally (long names) -- but the
         scrollbar itself is only shown when actually needed (matches
         ask_overwrite_dialog()'s own convention elsewhere in this
         file): a short list with short names shows no scrollbar at
         all, the dialog just sizes itself to fit.
      3. The dialog's own size is left to its natural required size
         AFTER the Text widget's height is already capped.

    No decorative icons or emoji beyond a single "❌" per failed source
    name in the multi-source case -- that one IS meaningful
    (distinguishes a failed entry at a glance in a list), not
    decorative.

    total_sources: int -- how many parcel sources were part of this run
    (successful + failed). The success count shown is total_sources
    minus len(failed_sources); no separate success list is passed in.

    failed_sources: list of (source_name, reason) tuples, in the order
    they failed during processing. Grouped by reason for display (one
    reason header, one or more "❌ name" lines under it) rather than
    repeating the same reason text per source -- multiple sources
    commonly fail for the identical reason (e.g. a shared network drive
    briefly unavailable), and repeating that same sentence many times
    over adds length without adding information.

    single_failure (total_sources == 1 and exactly one failure): this
    isn't really a "batch summary" at all -- there's nothing to
    summarize across multiple sources, just one source that failed.
    Showing "0 of 1 source(s)..." or even "None of the 1 source(s)
    completed successfully." in this case reads like a batch-processing
    report for something that was never really a batch. Gets its own
    much simpler layout instead: a "Processing Failed" title (not
    "Processing Complete"), a direct "'{name}' could not be processed."
    statement, and the reason -- no source count anywhere, no "Failed
    (N):" header, no ❌ marker (nothing to distinguish since there's
    only the one item, already named directly above).

    single_success_detail: optional str, e.g. "'landparcel' overwritten
    successfully." or "'LandParcel_2' created successfully." -- the
    single-source SUCCESS counterpart to single_failure above. Only
    used when total_sources == 1 and that one source succeeded; shown
    instead of the generic "All 1 source(s) completed successfully."
    for the same reason single_failure exists -- a specific, precise
    statement about the one thing that happened is more useful than a
    batch-shaped summary of a non-batch. Caller-supplied (built in
    run_processing()'s worker(), which knows the exact outcome --
    "overwritten" vs. "created" -- and output name/table for that one
    source) rather than derived here, since this function has no other
    way to know which of those two words applies.
    """
    failed_count = len(failed_sources)
    success_count = total_sources - failed_count
    single_failure = (total_sources == 1 and failed_count == 1)

    dialog = tk.Toplevel(parent)
    apply_icon(dialog)
    dialog.title("ROAD WIDTH TOOL")
    dialog.resizable(False, False)
    # Deliberately NOT calling dialog.transient(parent) here. This app's
    # root is permanently withdrawn (see main()), and transient() on a
    # withdrawn parent is a known source of window-manager-dependent
    # "dialog never becomes viewable, no exception raised" behavior --
    # confirmed reproducible on Linux/X11 during testing, and NOT
    # reliably verifiable here against the actual Windows/DWM deployment
    # target. Rather than depend on a specific
    # transient()+update_idletasks()+deiconify() ordering that might not
    # behave identically across platforms, this simply avoids
    # transient() altogether for any dialog parented to the withdrawn
    # root -- the safer, more portable choice, even though it gives up
    # transient()'s normal UX benefits (no separate taskbar entry,
    # staying above its logical parent) for this one case.
    dialog.grab_set()
    dialog.deiconify()
    dialog.lift()
    dialog.focus_force()
    dialog.attributes("-topmost", True)
    dialog.after(100, lambda: dialog.attributes("-topmost", False))

    # OK button first, at the bottom -- see docstring point 1.
    btn_frame = tk.Frame(dialog)
    btn_frame.pack(side="bottom", fill="x", pady=(4, 12))
    tk.Button(btn_frame, text="OK", command=dialog.destroy,
              width=12, cursor="hand2").pack()

    MAX_LIST_LINES = 15
    TEXT_WIDTH_CHARS = 60

    if single_failure:
        source_label, reason = failed_sources[0]

        tk.Label(dialog, text="Processing failed.",
                 font=("Segoe UI", 10, "bold"), anchor="w"
                 ).pack(fill="x", padx=16, pady=(16, 4))
        tk.Label(dialog, text=f"'{source_label}' could not be processed.",
                 anchor="w"
                 ).pack(fill="x", padx=16, pady=(0, 8))

        reason_lines = reason.split("\n")

        list_frame = tk.Frame(dialog)
        list_frame.pack(fill="both", expand=True, padx=16, pady=(0, 4))
        vscroll = tk.Scrollbar(list_frame, orient="vertical")
        hscroll = tk.Scrollbar(list_frame, orient="horizontal")
        text = tk.Text(
            list_frame, wrap="none",
            height=min(len(reason_lines), MAX_LIST_LINES),
            width=TEXT_WIDTH_CHARS, yscrollcommand=vscroll.set, xscrollcommand=hscroll.set,
            relief="flat", bg=dialog.cget("bg"), font=("Segoe UI", 9))
        vscroll.config(command=text.yview)
        hscroll.config(command=text.xview)
        if len(reason_lines) > MAX_LIST_LINES:
            vscroll.pack(side="right", fill="y")
        if any(len(rl) > TEXT_WIDTH_CHARS for rl in reason_lines):
            hscroll.pack(side="bottom", fill="x")
        text.pack(side="left", fill="both", expand=True)
        for rl in reason_lines:
            text.insert("end", rl + "\n")
        text.config(state="disabled")

    else:
        tk.Label(dialog, text="Processing complete.",
                 font=("Segoe UI", 10, "bold"), anchor="w"
                 ).pack(fill="x", padx=16, pady=(16, 4))

        if failed_count == 0:
            tk.Label(
                dialog,
                text=(
                    single_success_detail if (total_sources == 1 and single_success_detail)
                    else f"All {total_sources} source(s) completed successfully."
                ),
                anchor="w"
            ).pack(fill="x", padx=16, pady=(0, 16))
        else:
            if success_count == 0:
                summary_text = f"None of the {total_sources} source(s) completed successfully."
            else:
                summary_text = f"{success_count} of {total_sources} source(s) completed. {failed_count} failed."
            tk.Label(
                dialog,
                text=summary_text,
                anchor="w"
            ).pack(fill="x", padx=16, pady=(0, 4))

            tk.Label(dialog, text=f"Failed ({failed_count}):",
                     font=("Segoe UI", 9, "bold"), anchor="w"
                     ).pack(fill="x", padx=16, pady=(8, 4))

            # Group by reason, preserving first-seen order (matches
            # processing order -- not alphabetical) so the display reads
            # naturally rather than shuffling entries the user just watched
            # happen in a different order in the progress window.
            grouped = {}
            for name, reason in failed_sources:
                grouped.setdefault(reason, []).append(name)

            # Each reason acts as a group heading with a dash-underline
            # (matches a plain-text convention rather than needing any
            # extra font-weight state on a single Text widget), followed by
            # every source that failed for exactly that reason, then a
            # blank line before the next group -- reads like short
            # sub-sections rather than one undifferentiated wall of text
            # when there are multiple distinct failure reasons in the same
            # batch.
            lines = []
            for i, (reason, names) in enumerate(grouped.items()):
                if i > 0:
                    lines.append("")
                # reason may itself contain explicit \n line breaks (see
                # _translate_exception()) -- each line is added separately
                # so the Text widget (wrap="none") displays them as
                # intended, and the dash-underline is sized to the LONGEST
                # of those lines, not the raw string length including the
                # \n characters themselves.
                reason_lines = reason.split("\n")
                lines.extend(reason_lines)
                lines.append("-" * max(len(rl) for rl in reason_lines))
                for name in names:
                    lines.append(f"❌ {name}")

            list_frame = tk.Frame(dialog)
            list_frame.pack(fill="both", expand=True, padx=16, pady=(0, 4))

            vscroll = tk.Scrollbar(list_frame, orient="vertical")
            hscroll = tk.Scrollbar(list_frame, orient="horizontal")
            text = tk.Text(
                list_frame, wrap="none",
                height=min(len(lines), MAX_LIST_LINES) if lines else 1,
                width=TEXT_WIDTH_CHARS, yscrollcommand=vscroll.set, xscrollcommand=hscroll.set,
                relief="flat", bg=dialog.cget("bg"), font=("Segoe UI", 9))
            vscroll.config(command=text.yview)
            hscroll.config(command=text.xview)
            if len(lines) > MAX_LIST_LINES:
                vscroll.pack(side="right", fill="y")
            if any(len(line) > TEXT_WIDTH_CHARS for line in lines):
                hscroll.pack(side="bottom", fill="x")
            text.pack(side="left", fill="both", expand=True)

            for line in lines:
                text.insert("end", line + "\n")
            text.config(state="disabled")

    dialog.update_idletasks()
    req_w = max(dialog.winfo_reqwidth(), 420)
    req_h = dialog.winfo_reqheight()
    x, y = _get_dialog_center_position(dialog, req_w, req_h)
    dialog.geometry(f"{req_w}x{req_h}+{x}+{y}")

    dialog.wait_window()

def ask_overwrite_dialog(parent, conflicting_names):
    """
    Combined dialog shown ONCE, before any processing starts, when one
    or more Land Parcel sources' desired local output filename already
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
    choosing per-file in a single dialog. This keeps the dialog itself
    simple (three buttons, one decision) instead of turning it into a
    per-row selection UI.

    Returns "overwrite", "new", or "cancel" (also returned if the
    dialog's own titlebar close button is used, treated the same as an
    explicit Cancel -- never silently defaults to a destructive choice).
    """
    result = {"choice": "cancel"}

    dialog = tk.Toplevel(parent)
    dialog.title("ROAD WIDTH TOOL")
    dialog.resizable(False, False)
    # Deliberately NOT calling dialog.transient(parent) here. This app's
    # root is permanently withdrawn (see main()), and transient() on a
    # withdrawn parent is a known source of window-manager-dependent
    # "dialog never becomes viewable, no exception raised" behavior --
    # confirmed reproducible on Linux/X11 during testing, and NOT
    # reliably verifiable here against the actual Windows/DWM deployment
    # target. Rather than depend on a specific
    # transient()+update_idletasks()+deiconify() ordering that might not
    # behave identically across platforms, this simply avoids
    # transient() altogether for any dialog parented to the withdrawn
    # root -- the safer, more portable choice, even though it gives up
    # transient()'s normal UX benefits (no separate taskbar entry,
    # staying above its logical parent) for this one case.
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

    # Buttons packed first, at the bottom -- same reasoning as
    # show_success_dialog(): guaranteed visible/reachable regardless of
    # how long the scrollable list above them ends up being.
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
    # Scrollbars are only shown when actually needed -- an always-visible
    # scrollbar next to a box with nothing to scroll (e.g. just one short
    # filename) is pointless clutter, same principle already applied to
    # the classification checklist box elsewhere in this file.
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
    x, y = _get_dialog_center_position(dialog, req_w, req_h)
    dialog.geometry(f"{req_w}x{req_h}+{x}+{y}")

    dialog.wait_window()
    return result["choice"]

# ----------------- PROGRESS WINDOW (thread-safe) -----------------
class ProgressWindow:
    """
    Thread-safe progress display. Ported from road_frontage.py's own
    ProgressWindow -- this class itself is only ever touched from the
    main thread (see poll_queue() in run_processing() below); the
    background worker() thread never calls any method on this class
    directly, it only puts messages onto a queue.Queue() that the main
    thread drains and translates into calls here.

    Supports indeterminate mode (a continuously-animating bar with no
    known total) for the "Counting parcels..." stage, where the total
    feature count genuinely isn't known yet -- switches to determinate
    mode once switch_to_determinate() is called with the real total,
    matching this tool's own "Found N parcels." UX decision.
    """
    def __init__(self, root, title="Processing"):
        self.win = tk.Toplevel(root)
        self._closed = False
        apply_icon(self.win)
        self.win.title(title)
        self.win.minsize(420, 140)
        self.win.resizable(False, False)
        # No cancel function exists for an in-progress run -- the X
        # button closing this window wouldn't actually stop worker()
        # (still running on its own thread) or the database transaction
        # it might be mid-way through. _remove_close_button() below
        # (called once the window is realized) removes the X visually
        # via the Win32 API -- this protocol() override is kept as a
        # defensive fallback in case that Win32-level call doesn't fully
        # succeed on some Windows version/build, so clicking still does
        # nothing even if the button is somehow still visible.
        self.win.protocol("WM_DELETE_WINDOW", lambda: None)

        self.status_var = tk.StringVar(master=self.win)
        self.status_var.set("Starting...")
        tk.Label(
            self.win, textvariable=self.status_var, anchor="center",
            justify="center", wraplength=380,
        ).pack(pady=(14, 8), padx=10, fill="x")

        self.progress = ttk.Progressbar(
            self.win, orient="horizontal", mode="indeterminate", length=360
        )
        self.progress.pack(pady=6)
        self.progress.start(12)

        self.count_var = tk.StringVar(master=self.win)
        self.count_var.set("")
        tk.Label(self.win, textvariable=self.count_var).pack(pady=(0, 10))

        self.win.attributes("-topmost", True)
        self.win.update_idletasks()
        req_w = max(self.win.winfo_reqwidth(), 420)
        req_h = self.win.winfo_reqheight()
        x, y = _get_dialog_center_position(self.win, req_w, req_h)
        self.win.geometry(f"{req_w}x{req_h}+{x}+{y}")
        _remove_close_button(self.win)
        self.win.focus_force()
        self.win.lift()
        self.win.after(100, lambda: self.win.attributes("-topmost", False))

    def switch_to_determinate(self, total):
        """Called once the real parcel count is known -- stops the
        indeterminate animation and switches to a normal 0..total bar."""
        if self._closed or not self.win.winfo_exists():
            return
        self.progress.stop()
        self.progress.config(mode="determinate", maximum=total, value=0)
        self.count_var.set(f"0 / {total}")

    def update(self, message, value=None, total=None):
        # Defensive: poll_queue() drains the ENTIRE queue in one pass
        # per call (see run_processing() below), and this window is
        # only ever destroyed in response to a "done"/"fatal_error"
        # message -- which is always the LAST message worker() puts.
        # In normal operation there should be nothing left to process
        # after that. This guard exists as a safety net regardless: if
        # this window has already been destroyed (self.win.destroy()
        # already ran, e.g. via close()) by the time some update
        # message is processed, silently do nothing instead of raising
        # _tkinter.TclError: invalid command name "...progressbar" --
        # confirmed reproduced in production (root cause not fully
        # pinned down; suspected relation to a duplicate tool launch
        # observed in the same session, tracked separately as a
        # dispatcher-level issue in MAIN.py, out of scope for this
        # file). A silently-dropped stale update is harmless -- the
        # window is already gone, there's nothing for the user to see
        # regardless -- whereas an uncaught TclError here surfaces as
        # a visible, alarming "Exception in Tkinter callback" even on
        # an otherwise fully successful run.
        if self._closed or not self.win.winfo_exists():
            return
        self.status_var.set(message)
        if value is not None and total is not None:
            self.progress["value"] = value
            self.count_var.set(f"{value} / {total}")
        self.win.update_idletasks()

    def close(self):
        self._closed = True
        try:
            self.progress.stop()
        except Exception:
            pass
        self.win.destroy()


# ----------------- PROGRESS WINDOW (OLD -- kept temporarily, unused) -----------------
# NOTE: superseded by the ProgressWindow class above as part of moving
# run_processing() onto a background thread (see that function's own
# docstring). create_progress_window()/update_progress() below are no
# longer called anywhere in this file -- left in place deliberately,
# not deleted yet, as the first phase of a two-phase migration: get the
# new thread+queue architecture working and validated first, remove
# this dead code as a separate, later, low-risk cleanup once confirmed
# stable. Do not call these two functions in any new code.
def create_progress_window(root, total):
    win = tk.Toplevel(root)
    win.title("Processing...")
    win.geometry("420x160")
    win.resizable(False, False)

    lbl = tk.Label(win, text="Starting...", wraplength=380)
    lbl.pack(pady=10)

    bar = ttk.Progressbar(
        win, orient="horizontal", length=360,
        mode="determinate", maximum=total
    )
    bar.pack(pady=10)

    count_lbl = tk.Label(win, text=f"0 / {total}")
    count_lbl.pack()

    win.update_idletasks()
    return win, lbl, bar, count_lbl


def update_progress(win, lbl, bar, count_lbl, step, total, msg):
    lbl.config(text=msg)
    bar["value"] = step
    count_lbl.config(text=f"{step} / {total}")
    win.update_idletasks()
    win.update()


# ----------------- PROCESSING -----------------
def _translate_exception(e, source_label):
    """
    Maps a caught exception to a plain-language, non-technical message
    -- shown to the user in the final show_success_dialog() summary.
    The exact technical exception (type + message) is still printed to
    the console separately by the caller for anyone who needs to
    actually debug it; this function's job is only to produce
    something a non-technical LGU user can read and act on without
    seeing a raw Python traceback.

    Matched by exception type name (not a strict isinstance() chain) so
    this also catches exceptions raised by lower-level libraries
    (SQLAlchemy, psycopg2, pyogrio) that wrap the same underlying OS or
    connection error in a library-specific exception class.

    The five CATEGORIZED reasons below deliberately do NOT embed
    source_label -- show_success_dialog() groups failed sources by
    their exact reason text, one shared header per group followed by
    every source that hit it. If the filename were embedded in the
    reason itself, every source would produce a technically-different
    reason string (even for the identical underlying cause) and
    grouping would never actually combine anything -- e.g. a shared
    network drive going briefly unavailable mid-batch would otherwise
    show as N separate one-line groups instead of one shared reason
    with N sources listed under it.

    The UNEXPECTED/fallback case is the one exception to that: kept
    deliberately generic-but-per-source (embeds source_label, doesn't
    group), since this catch-all covers whatever wasn't specifically
    anticipated above -- coincidentally-identical, unrelated failures
    across different sources are much less likely here than for the
    four specific, well-understood categories above, so there's little
    grouping benefit to lose, and naming the source directly in this
    one case is more useful than a generic "an unexpected error
    occurred" header with no other information.

    Explicit \\n line breaks (not just wrapping) throughout -- the
    failed-sources list in show_success_dialog() lives in a Text widget
    with wrap="none" (deliberately, so long filenames stay on one line
    and scroll into view rather than wrapping and losing readability --
    see that function's own docstring), so a long reason with no
    manual line breaks would just run off horizontally instead of
    wrapping on its own.
    """
    type_name = type(e).__name__
    # SQLAlchemy wraps the original DB-API (psycopg2) exception in its
    # own exception class, exposing it via .orig -- e.g. a raw
    # psycopg2.errors.InvalidSchemaName arrives here wrapped as a
    # SQLAlchemy ProgrammingError. Checking .orig's type name catches
    # the specific underlying database error, not just SQLAlchemy's
    # generic wrapper class (confirmed via a real production log: a
    # misconfigured pg_credentials.json schema name produced exactly
    # this ProgrammingError -> InvalidSchemaName chain; also verified
    # empirically that SQLAlchemy's ProgrammingError.orig is the exact
    # original psycopg2.errors.InvalidSchemaName instance).
    orig_type_name = type(getattr(e, "orig", None)).__name__

    if isinstance(e, FileNotFoundError):
        return (
            "The file could not be found.\n"
            "It may have been moved, renamed, or deleted."
        )
    if isinstance(e, PermissionError):
        return (
            "The output could not be saved.\n"
            "Make sure it is not open in another program and\n"
            "that you have permission to write to this folder."
        )
    if "InvalidSchemaName" in orig_type_name:
        return (
            "The database schema could not be found.\n"
            "Please check your database configuration and try again."
        )
    if "OperationalError" in type_name or "InterfaceError" in type_name:
        return (
            "Could not connect to the database.\n"
            "Please check the database connection and try again."
        )
    if isinstance(e, KeyError):
        return (
            "The file is missing required data (such as geometry\n"
            "or required fields). Please verify that you selected\n"
            "the correct dataset."
        )
    return f"An unexpected error occurred while processing '{source_label}'."


def resolve_db_output_table(root, schema, barangay_source):
    """
    Determines the DB-output destination table for the Land Parcel
    source, BEFORE the worker thread starts -- same "resolve everything
    up front, main thread only" philosophy as ask_overwrite_dialog() /
    ask_db_overwrite_dialog() (see run_processing()). This is what lets
    the fuzzy-match + confirmation flow avoid ever needing a
    thread-safe dialog mechanism: the Land Parcel source is singular
    (see parcel_local_path / parcel_db_table -- single-select
    architecture), so everything needed to resolve the destination
    table is already known before any background processing begins.

    Two cases:
      - DB-source Land Parcel (barangay_source[0] == "db"): always
        writes back to the exact same table it was read from -- no
        matching, no dialog, matches _process_one_source()'s own
        pre-existing is_db_source handling.
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


def _process_one_source(
    source_id, is_db_source, road_gdf, engine, schema,
    output_mode, overwrite_mode,
    parcel_classification_selection, filter_by_road_type_active,
    road_type_excluded_values, parcel_road_width_column_overrides,
    progress_cb, status_cb,
    resolved_table_name=None, resolved_outcome=None,
):
    """
    Fully processes ONE parcel source: load, classify, measure, and
    write (main output + Visual Measurement layer). Returns
    (source_label, main_output_path_or_table, vm_output_path_or_table_or_None, outcome)
    on success, where outcome is "overwritten" or "created" -- describes
    what happened to the MAIN output specifically (never the VM layer),
    used by the caller to build a precise single-source success
    message (e.g. "'landparcel' overwritten successfully." vs.
    "'LandParcel_2' created successfully.").

    Raises on any failure. Deliberately does NOT catch or swallow
    exceptions itself -- per-source failure isolation is the CALLER's
    responsibility (see worker() in run_processing()), so a failure
    partway through this function is always visible to the caller as a
    genuine failure for this source, never silently treated as success.

    Atomicity guarantees:
      - Local output: _write_gpkg() itself is atomic (temp file,
        verified readable, then os.replace()) -- if this function
        raises at any point, including mid-write, no partial or
        corrupted file is ever left at the destination path.
      - Database output: to_postgis() and the CAMA_Table update all run
        inside ONE transaction (see the inline comment at that call
        site for why passing the shared `conn`, not `engine`, is what
        makes this true) -- if anything in that block raises, the
        ENTIRE per-source database update rolls back together,
        including the to_postgis() write. (CAMA_Transaction_Log
        writes, previously also part of this block, were removed --
        confirmed unused: nothing in this project reads from that
        table, and the one other tool that also wrote to it,
        influence_to_barangay.py, does so independently via its own
        CREATE TABLE IF NOT EXISTS, not dependent on this tool's
        contribution.)
      - Visual Measurement layer: intentionally NOT covered by either
        guarantee above. It's a supplementary QA/visualization layer,
        not a core appraisal deliverable -- its own write is wrapped in
        its own try/except, logged to the console on failure, and never
        raised further, so a VM failure can never undo or block an
        already-successful main output.

    resolved_table_name, resolved_outcome: the DB-output destination
    already decided by resolve_db_output_table() (see run_processing()),
    BEFORE this function or the worker thread even starts -- parallel
    to overwrite_mode for local output, but resolved per-source rather
    than batch-wide, since the Land Parcel source is singular (see
    resolve_db_output_table()'s own docstring for why this avoids
    needing a thread-safe dialog mechanism). Only consulted for a LOCAL
    parcel source being written to a DATABASE table (is_db_source is
    False, output_mode[0] == "db") -- a source that was ITSELF read
    from the database always writes back to that exact same table (see
    below), which never depends on these parameters at all. Both
    default to None so this function remains independently callable/
    testable without requiring the full DB-resolution flow; when None
    for a DB-output local source, falls back to creating a new table
    under the source filename (see the is_db_source-is-False branch
    below).

    status_cb(message): called at each stage transition within this
    function (before classification, before each write) so the
    progress window's status text reflects what THIS function is
    actually doing, not an approximation guessed from the caller.
    """
    _t_read_start = time.perf_counter()
    if is_db_source:
        b_gdf = read_postgis_clean(source_id, engine, schema)
        source_label = source_id
    else:
        b_gdf = gpd.read_file(source_id)
        source_label = os.path.basename(source_id)
    print(f"⏱️ [{source_label}] Reading source: {time.perf_counter() - _t_read_start:.2f}s")

    output_column_name = parcel_road_width_column_overrides.get(source_id, "CAMA_ROAD_WIDTH")

    status_cb(f"Resolving road classification: {source_label}...")
    _t_classify_start = time.perf_counter()
    use_classification_for_source = parcel_classification_selection.get(source_id, False)
    classification = resolve_classification(
        b_gdf, use_classification_for_source, filter_by_road_type_active,
        road_type_excluded_values
    )
    print(f"⏱️ [{source_label}] Resolving classification: {time.perf_counter() - _t_classify_start:.2f}s")

    _t_process_start = time.perf_counter()
    b_gdf, qa_gdf = process(
        b_gdf, road_gdf, source_label, progress_cb,
        classification=classification, output_column_name=output_column_name
    )
    print(f"⏱️ [{source_label}] process() (measurement): {time.perf_counter() - _t_process_start:.2f}s")

    if output_mode[0] == "local":
        desired_base_name = (
            source_id if is_db_source
            else os.path.splitext(os.path.basename(source_id))[0]
        )
        candidate_path = os.path.join(output_mode[1], f"{desired_base_name}.gpkg")
        had_conflict = os.path.exists(candidate_path)
        # overwrite_mode was already resolved ONCE, up front, for the
        # whole batch (see the pre-scan + ask_overwrite_dialog() in
        # run_processing()) -- no per-file prompt here.
        base_name = (
            resolve_output_base_name(output_mode[1], desired_base_name)
            if had_conflict and overwrite_mode == "new"
            else desired_base_name
        )
        outcome = "overwritten" if (had_conflict and overwrite_mode == "overwrite") else "created"
        out = os.path.join(output_mode[1], f"{base_name}.gpkg")

        status_cb(f"Writing output file: {source_label}...")
        _write_gpkg(b_gdf, out)

        vm_out = None
        if not qa_gdf.empty:
            try:
                status_cb("Writing Visual Measurement layer...")
                qa_base_name = with_qa_suffix(base_name)
                qa_out = os.path.join(output_mode[1], f"{qa_base_name}.gpkg")
                _write_gpkg(qa_gdf, qa_out)
                vm_out = qa_out
            except Exception as e:
                print(f"⚠️ Could not write Visual Measurement layer for '{source_label}': {type(e).__name__}: {e}")

        return source_label, out, vm_out, outcome

    else:
        if is_db_source:
            # db-source -> db-output: writes back to the exact SAME
            # table it read from -- pre-existing, intentional design
            # (this is a read-modify-write of one dataset, not "here is
            # a new dataset, does something with this name already
            # exist" the way a local-file source is). resolve_db_output_
            # table() already returns this same (source_id,
            # "overwritten") pair for a DB-source Land Parcel -- this
            # branch's own direct handling is kept here too so this
            # function stays independently correct even if called with
            # resolved_table_name=None (see the docstring above).
            table = source_id
            outcome = "overwritten"
        else:
            # The actual destination table was already decided by
            # resolve_db_output_table(), BEFORE this function (and the
            # worker thread) even started -- fuzzy matching + user
            # confirmation already happened there (see that function's
            # docstring for why). This function just uses the result.
            # Falls back to creating a new table under the source
            # filename if resolved_table_name is None (e.g. this
            # function called directly/independently, without going
            # through the DB-resolution pre-processing step).
            if resolved_table_name is not None:
                table = resolved_table_name
                outcome = resolved_outcome
            else:
                table = os.path.splitext(os.path.basename(source_id))[0]
                outcome = "created"

        status_cb(
            "Updating database records..." if outcome == "overwritten"
            else "Creating new table in database..."
        )
        # Atomic per-source database write. to_postgis() is given the
        # shared, already-in-transaction `conn` (NOT the bare `engine`)
        # so it reuses this same connection/transaction rather than
        # opening its own independently-committing one -- confirmed via
        # geopandas.io.sql._get_conn(), which explicitly checks
        # Connection.in_transaction() and reuses the given connection
        # when True (empirically verified against a real SQLAlchemy
        # engine: passing `engine` to a nested write let that write
        # survive an outer rollback; passing the shared `conn` correctly
        # rolled both writes back together). If anything below raises,
        # the WHOLE block -- including the to_postgis() write -- rolls
        # back, leaving this table exactly as it was before this call.
        with engine.begin() as conn:
            _t_topg_start = time.perf_counter()
            b_gdf.to_postgis(table, conn, schema=schema, if_exists="replace", index=False)
            print(f"⏱️ [{source_label}] to_postgis() (full table write): {time.perf_counter() - _t_topg_start:.2f}s")

            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS "{schema}"."CAMA_Table" (
                    id SERIAL PRIMARY KEY,
                    PIN TEXT UNIQUE NOT NULL
                );
            """))
            conn.execute(text(f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema='{schema}'
                          AND table_name='CAMA_Table'
                          AND column_name='cama_road_width'
                    ) THEN
                        EXECUTE 'ALTER TABLE "{schema}"."CAMA_Table" ADD COLUMN "cama_road_width" NUMERIC';
                    END IF;
                END $$;
            """))
            pin_field = _detect_pin_column(b_gdf)
            if pin_field:
                _t_cama_start = time.perf_counter()
                # Batched execution instead of one .execute() call per
                # row (was: 11,911 individual round-trips for an
                # 11,911-row source, confirmed via direct user testing
                # to be the dominant cost of the whole DB-write phase --
                # far more than to_postgis() itself). SQLAlchemy's
                # Connection.execute() accepts a LIST of parameter
                # dicts for the same statement and forwards it to the
                # DBAPI's executemany-equivalent, which psycopg2 batches
                # into far fewer network round-trips than issuing each
                # one separately from a Python loop -- same SQL, same
                # ON CONFLICT DO UPDATE semantics per row, same
                # transaction, same rollback-on-failure guarantee
                # (still inside the `with engine.begin() as conn:`
                # block above) -- purely a performance change, no
                # difference in what ends up in the table.
                #
                # Chunked (not one single call for all 11,911 rows) so
                # progress can be reported incrementally instead of the
                # progress window sitting on one static message for the
                # DB write's entire duration -- and to keep each
                # individual batch a reasonable size regardless of how
                # large a given source is.
                CAMA_BATCH_SIZE = 1000
                sql = text(f"""
                    INSERT INTO "{schema}"."CAMA_Table" (PIN, cama_road_width)
                    VALUES (:pin, :rw)
                    ON CONFLICT (PIN) DO UPDATE
                    SET cama_road_width = EXCLUDED.cama_road_width;
                """)
                all_params = [
                    {
                        "pin": str(row[pin_field]),
                        "rw": float(row[output_column_name]) if row[output_column_name] is not None else None,
                    }
                    for _, row in b_gdf.iterrows()
                ]
                total_rows = len(all_params)
                for batch_start in range(0, total_rows, CAMA_BATCH_SIZE):
                    batch = all_params[batch_start:batch_start + CAMA_BATCH_SIZE]
                    # --- TEMPORARY DIAGNOSTIC INSTRUMENTATION ---
                    # Not permanent telemetry -- added to confirm whether
                    # the ~402s CAMA_Table update time (see profiling
                    # below this loop) is consistently spent inside the
                    # UPSERT execution itself (conn.execute(sql, batch))
                    # or concentrated in specific batches, which would
                    # point to a different cause (lock waits, connection
                    # warm-up, autovacuum, etc.) instead. Confirmed via
                    # the official SQLAlchemy 2.0 changelog that
                    # insertmanyvalues -- the multi-row VALUES batching
                    # optimization -- is disabled whenever the statement
                    # has an ON CONFLICT clause (as this one does), so
                    # conn.execute(sql, batch) here falls back to
                    # SQLAlchemy's legacy per-row execution path. That
                    # confirms batching is NOT happening, but does not by
                    # itself confirm this loop is where the 402s actually
                    # goes -- this print exists to check that directly
                    # against a real production run before deciding
                    # whether to change the UPSERT implementation. Remove
                    # or replace with proper structured logging once a
                    # decision is made from real profiling output -- do
                    # not treat this as a finished/permanent addition.
                    _t_batch_start = time.perf_counter()
                    conn.execute(sql, batch)
                    _batch_elapsed = time.perf_counter() - _t_batch_start
                    _batch_num = batch_start // CAMA_BATCH_SIZE + 1
                    print(
                        f"⏱️🔎 [{source_label}] CAMA_Table UPSERT batch "
                        f"#{_batch_num} ({len(batch)} rows): "
                        f"{_batch_elapsed:.2f}s "
                        f"({_batch_elapsed / len(batch) * 1000:.1f} ms/row)"
                    )
                    # --- END TEMPORARY DIAGNOSTIC INSTRUMENTATION ---
                    done = min(batch_start + CAMA_BATCH_SIZE, total_rows)
                    status_cb(f"Updating database records: {done} / {total_rows}...", done, total_rows)
                print(f"⏱️ [{source_label}] CAMA_Table update ({total_rows} rows, batched): {time.perf_counter() - _t_cama_start:.2f}s")

        # Visual Measurement layer -- best-effort, own separate write,
        # deliberately NOT inside the transaction above (see this
        # function's own docstring for why).
        vm_table = None
        if not qa_gdf.empty:
            try:
                status_cb("Writing Visual Measurement layer...")
                _t_vm_start = time.perf_counter()
                qa_table = f"{table}_VM"
                qa_gdf.to_postgis(qa_table, engine, schema=schema, if_exists="replace", index=False)
                vm_table = qa_table
                print(f"⏱️ [{source_label}] Visual Measurement layer write: {time.perf_counter() - _t_vm_start:.2f}s")
            except Exception as e:
                print(f"⚠️ Could not write Visual Measurement layer to DB for '{source_label}': {type(e).__name__}: {e}")

        return source_label, table, vm_table, outcome


def run_processing(app_root, overwrite_mode=None):
    # overwrite_mode: passed from on_run() -- resolved before win.destroy()
    # so the dialog has a live parent and Cancel returns the user to the
    # configuration window. Replaces the previous implementation that
    # resolved the dialog inside run_processing() after win was destroyed.
    """
    Runs the full batch (all selected parcel sources) on a background
    thread, showing live progress and a final summary via
    ProgressWindow + show_success_dialog().

    Ports the background-thread + queue.Queue() + poll_queue() pattern
    already used in road_frontage.py/lot_location.py onto this tool --
    this function previously ran entirely on the GUI's main thread,
    freezing the whole CAMA Tools window (indistinguishable from a
    crash to the user, per direct user report) for the full duration of
    a batch.

    Threading discipline: worker() below never touches Tkinter directly
    -- it only puts messages onto `q`. All widget updates happen in
    poll_queue(), on the main thread, via app_root.after(100,
    poll_queue).

    Per-source failure isolation: if one parcel source fails, it is
    skipped and the rest of the batch continues. See
    _process_one_source()'s own docstring for the atomicity guarantees
    that make this safe -- a failed source's existing output (if any)
    is left completely untouched, and a source that already completed
    successfully before a LATER source's failure remains committed, not
    rolled back. A summary naming every failed source and why is shown
    at the end, grouped by reason, instead of a raw crash aborting the
    whole batch with no explanation.
    """
    global barangay_source, road_source, output_mode
    global parcel_classification_selection, filter_by_road_type_active, road_type_excluded_values
    global parcel_road_width_column_overrides

    if not barangay_source or not road_source or not output_mode:
        messagebox.showerror("Error", "Selections incomplete (Barangay, Road, Output required).")
        return

    creds = load_db_credentials()
    if not creds:
        return
    schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    )

    # resolved_table_name / resolved_outcome: the DB-output destination
    # table, resolved ONCE, up front -- same "main thread, before the
    # worker starts" philosophy as overwrite_mode above, now via the
    # dedicated resolve_db_output_table() helper (fuzzy matching +
    # confirmation dialog(s) all happen inside it; see its own
    # docstring). Only relevant when output_mode[0] == "db" -- stays
    # None/None for local output, where _process_one_source() ignores
    # them entirely.
    resolved_table_name = None
    resolved_outcome = None
    if output_mode[0] == "db":
        resolved_table_name, resolved_outcome = resolve_db_output_table(
            root, schema, barangay_source
        )
        if resolved_table_name is None:
            print("Run cancelled by user (database output table not confirmed).")
            return

    progress = ProgressWindow(app_root, "ROAD WIDTH TOOL")
    q = queue.Queue()

    def worker():
        # Everything below runs inside an outer try/finally so that
        # engine.dispose() (further down) is GUARANTEED to run exactly
        # once no matter how this function exits -- full success, a
        # per-source failure (already caught inside the loop below,
        # doesn't escape this far), or the outer fatal_error path.
        # Releases the ENTIRE connection pool back to the database, not
        # just the individual per-source connections already released
        # by each `with engine.begin() as conn:` block inside
        # _process_one_source() -- without this, idle pooled
        # connections could accumulate across repeated runs of this
        # tool within the same CAMA Tools session.
        try:
            try:
                q.put(("update", "Loading road network...", None, None))
                if road_source[0] == "local":
                    road_gdf = gpd.read_file(road_source[1][0])
                else:
                    road_gdf = read_postgis_clean(road_source[1][0], engine, schema)

                q.put(("update", "Counting parcels...", None, None))
                total_features = 0
                if barangay_source[0] == "local":
                    for p in barangay_source[1]:
                        total_features += len(gpd.read_file(p))
                else:
                    for t in barangay_source[1]:
                        total_features += len(read_postgis_clean(t, engine, schema))
                q.put(("found_total", f"Found {total_features} parcel(s).", total_features, None))

                current_step = 0
                current_source_label = [""]  # mutable box -- progress_cb below closes over this

                def progress_cb(_):
                    nonlocal current_step
                    current_step += 1
                    msg = (
                        f"Measuring road width...\n"
                        f"Parcel {current_step} / {total_features}\n"
                        f"Source: {current_source_label[0]}"
                    )
                    q.put(("update", msg, current_step, total_features))

                def status_cb(message, value=None, total=None):
                    # value/total let a caller report its OWN live
                    # progress (e.g. the database-write phase's
                    # "Updating database records: N / Total..." -- see
                    # the status_cb() call inside _process_one_source())
                    # instead of falling back to current_step/
                    # total_features, which belong to the PARCEL
                    # MEASUREMENT phase (via progress_cb above) and are
                    # already maxed out (current_step == total_features)
                    # by the time any later phase runs -- previously
                    # caused the progress bar to render full/green
                    # throughout the entire database-write phase even
                    # while the status text above it correctly showed
                    # real incremental progress. Every OTHER existing
                    # status_cb(message) call (no value/total passed)
                    # keeps working exactly as before: falls back to
                    # current_step/total_features, same as always.
                    q.put((
                        "update", message,
                        value if value is not None else current_step,
                        total if total is not None else total_features,
                    ))

                sources = (
                    [(p, False) for p in barangay_source[1]]
                    if barangay_source[0] == "local"
                    else [(t, True) for t in barangay_source[1]]
                )

                failed_sources = []
                success_count = 0
                # Only meaningfully used when len(sources) == 1 -- the
                # single-source success case gets its own precise
                # message ("'landparcel' overwritten successfully.")
                # instead of the generic batch-count summary, which
                # reads oddly ("1 of 1 source(s)...") for something that
                # was never really a "batch". See show_success_dialog().
                single_success_detail = None

                for source_id, is_db_source in sources:
                    source_label = source_id if is_db_source else os.path.basename(source_id)
                    current_source_label[0] = source_label
                    q.put(("update", f"Loading parcel source: {source_label}...", None, None))
                    try:
                        label, out_ref, vm_ref, outcome = _process_one_source(
                            source_id, is_db_source, road_gdf, engine, schema,
                            output_mode, overwrite_mode,
                            parcel_classification_selection, filter_by_road_type_active,
                            road_type_excluded_values, parcel_road_width_column_overrides,
                            progress_cb, status_cb,
                            resolved_table_name=resolved_table_name,
                            resolved_outcome=resolved_outcome,
                        )
                        success_count += 1

                        if len(sources) == 1:
                            display_name = os.path.basename(out_ref) if output_mode[0] == "local" else out_ref
                            single_success_detail = f"'{display_name}' {outcome} successfully."

                        if output_mode[0] == "local":
                            q.put(("open_gm", out_ref, None, None))
                            if vm_ref:
                                q.put(("open_gm", vm_ref, None, None))
                            q.put(("update", "Opening in Global Mapper...", None, None))

                    except Exception as e:
                        reason = _translate_exception(e, source_label)
                        failed_sources.append((source_label, reason))
                        print(f"⚠️ Skipped '{source_label}': {type(e).__name__}: {e}")

                q.put(("done", success_count + len(failed_sources), failed_sources, single_success_detail))

            except Exception as e:
                # Failure OUTSIDE the per-source loop (e.g. the road network
                # itself couldn't be loaded) -- genuinely affects the whole
                # batch, since nothing can be measured without it. Not
                # per-source, so not added to failed_sources -- this aborts
                # the whole run with its own dialog instead.
                q.put(("fatal_error", str(e), None, None))
        finally:
            try:
                engine.dispose()
            except Exception as e:
                print(f"⚠️ Could not cleanly dispose of the database engine: {e}")

    def poll_queue():
        try:
            while True:
                msg = q.get_nowait()
                kind = msg[0]

                if kind == "update":
                    progress.update(msg[1], msg[2], msg[3])

                elif kind == "found_total":
                    progress.switch_to_determinate(msg[2])
                    progress.update(msg[1], 0, msg[2])

                elif kind == "open_gm":
                    load_in_global_mapper(msg[1])

                elif kind == "done":
                    progress.close()
                    show_success_dialog(app_root, msg[1], msg[2], msg[3])
                    return

                elif kind == "fatal_error":
                    progress.close()
                    messagebox.showerror(
                        "Error",
                        f"Could not complete processing: {msg[1]}"
                    )
                    return

        except queue.Empty:
            pass
        except tk.TclError as e:
            # Belt-and-suspenders alongside ProgressWindow's own
            # winfo_exists() guards above: if the progress window (or
            # any other widget touched in this loop) was destroyed by
            # some path this function doesn't already account for, stop
            # polling quietly instead of letting an uncaught TclError
            # surface as a visible "Exception in Tkinter callback" on
            # what may otherwise have been a fully successful run. Logged
            # to the console, not silently swallowed, so it's still
            # visible for debugging if it recurs.
            print(f"⚠️ poll_queue() stopped early (widget no longer exists): {e}")
            return

        app_root.after(100, poll_queue)

    threading.Thread(target=worker, daemon=True).start()
    poll_queue()


# ----------------- MAIN -----------------
def main(parent=None):
    global root

    if parent is not None:
        # Dev mode: reuse the already-hidden root from main3.py
        # No new tk.Tk() = no new taskbar icon
        root = parent
        open_main_window(root)
        # Do NOT call mainloop() — main3.py's loop is already running
    else:
        # Standalone / frozen exe mode: create our own hidden root
        import ctypes
        root = tk.Tk()
        root.withdraw()
        root.geometry("1x1+-9999+-9999")
        root.update_idletasks()

        GWL_EXSTYLE      = -20
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_APPWINDOW  = 0x00040000
        hwnd = root.winfo_id()
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        root.overrideredirect(True)

        open_main_window(root)
        root.mainloop()

if __name__ == "__main__":
    main()