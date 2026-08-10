import atexit
import signal
from datetime import datetime

TOOL_PROCESSES = []

# ============================
# FORCE WINDOWS APP ICON
# ============================
import ctypes
import ctypes.wintypes
import sys

def set_app_user_model_id():
    appid = u"BLGF.CAMA.Tools.2025"
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(appid)

set_app_user_model_id()


import tkinter as tk
from tkinter import filedialog, messagebox, Listbox
import subprocess
import os
import json
import psycopg2
from rapidfuzz import process, fuzz
from PIL import Image, ImageTk, ImageDraw

from pathlib import Path
from utils_paths import resource_path

def force_png_icon(win):
    png = resource_path("BLGF.png")
    if os.path.exists(png):
        img = tk.PhotoImage(file=png)
        win.iconphoto(True, img)
        win._icon_ref = img  # prevent garbage collection

def apply_icon(win):
    ico = resource_path("BLGF.ico")
    if os.path.exists(ico):
        try:
            win.iconbitmap(ico)
        except Exception:
            pass
    force_png_icon(win)


import sys, importlib, argparse


TEMP_DIR = r"C:\Global Mapper Temp"
try:
    os.makedirs(TEMP_DIR, exist_ok=True)
except Exception as e:
    from tkinter import messagebox
    messagebox.showerror("Folder Error", f"Could not create {TEMP_DIR}:\n{e}")
    sys.exit(1)


# ── Diagnostic logging for GM export automation ──────────────────────
# Persistent breadcrumb trail for update_database_from_geopackage() and
# update_map_and_select_recorded(), so a failed run can be reconstructed
# after the fact instead of relying on console output that scrolls away
# or disappears if the app is force-closed.
#
# Scope: diagnostic instrumentation only for the two GM-automation
# entry points and _wait_for_gpkg_export(). Does not touch DB write
# logic, matching logic, or any other part of the application.
_LOG_PATH = os.path.join(TEMP_DIR, "cama_automation.log")


def _log(msg):
    """
    Write one timestamped line to both console and the persistent log
    file. Logging must never break automation — any failure to write
    the file is swallowed silently; the console print always happens.
    Opens/closes the file per call (not a held handle) so a force-kill
    mid-export loses at most one line.
    """
    line = f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}"
    print(line, flush=True)
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _log_session_start(func_name):
    """Marks the start of a new automation run in the log file."""
    _log(f"{'=' * 60}")
    _log(f"SESSION START: {func_name}")


def _fg_title():
    """
    Best-effort foreground window title, for logging which window
    actually has focus at a given automation step. Wraps the existing
    get_foreground_hwnd()/hwnd_title() helpers (defined later in this
    module, resolved at call time — safe since this is never invoked
    before those are defined during normal startup). Never raises.
    """
    try:
        return hwnd_title(get_foreground_hwnd())
    except Exception as e:
        return f"<fg lookup failed: {e}>"


def _dump_windows(context):
    """
    Logs all visible top-level window titles at a decision point, so
    an unexpected GM dialog (e.g. a filter/license/permission dialog)
    that intercepted the keystroke sequence shows up by name in the
    log instead of being invisible.
    """
    try:
        titles = [t for t in gw.getAllTitles() if t.strip()]
        _log(f"WINDOW DUMP ({context}): {titles}")
    except Exception as e:
        _log(f"WINDOW DUMP ({context}) failed: {e}")


# ── GM export automation timeouts (seconds) ─────────────────────────
# These bound the _wait_for_gpkg_export() poll that replaces the old
# unbounded `while True:` file-stability loops. Tuned against observed
# production exports of 11,000+ parcel features — see analysis notes.
#
# EXPORT_APPEARANCE_TIMEOUT_S:
#   Max wait for the .gpkg file to EXIST at all. In a healthy run GM
#   creates the file within seconds of the Save dialog confirming.
#   If it never appears, the export keystroke sequence was almost
#   certainly intercepted by an unexpected GM dialog (e.g. "Lidar
#   Filter Settings", a license prompt). This is the parameter that
#   catches the reported hang.
#
# EXPORT_STALL_TIMEOUT_S:
#   Max time the file may exist WITHOUT its byte size changing before
#   we declare the export dead (export started, then died behind a
#   dialog). GPKG writes are streaming SQLite inserts — a live export
#   grows continuously.
#
# EXPORT_HARD_TIMEOUT_S:
#   Absolute wall-clock ceiling regardless of activity. Guarantees
#   boundedness. ~10x margin over the largest observed real export.
EXPORT_APPEARANCE_TIMEOUT_S = 90
EXPORT_STALL_TIMEOUT_S = 180
EXPORT_HARD_TIMEOUT_S = 900


TOOL_MODULES = {
    "ANY MAP TO LAND PARCEL": "tools.influence_to_barangay",
    "INFLUENCE TO MAP": "tools.influence_to_map",
    "ROAD WIDTH": "tools.road_width",
    "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO": "tools.road_frontage",
    "LOT LOCATION": "tools.lot_location",
    "LAND SHAPE": "tools.land_shape_compactness",
    "METERS FROM (SCHOOL, SHOP, TRANSPORT, CHURCH)": "tools.POI_All_Distance",
    "LANDMARKS WITHIN METERS": "tools.poi_within_200_meters_for_parcellary_church_mall_police_park",
    "PARCEL TERRAIN LEVEL": "tools.terrain",
    "ROAD DENSITY": "tools.road_density",
    "ROAD SURFACE": "tools.road_surface",
}

def dispatch_tool_if_requested():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--tool", default=None)
    ap.add_argument("--icon", default=None)
    args, _ = ap.parse_known_args()

    # ✅ If no tool specified, do normal launcher flow
    if not args.tool:
        return False

    # ✅ Apply icon for the tool subprocess
    if args.icon:
        try:
            icon_path = resource_path(args.icon)
            tmp = tk.Tk()
            tmp.withdraw()
            apply_icon(tmp)
        except Exception:
            pass

    mod_path = TOOL_MODULES.get(args.tool)
    if not mod_path:
        from tkinter import Tk, messagebox
        r = Tk(); r.withdraw()
        messagebox.showerror("Tool Error", f"Unknown tool: {args.tool}")
        sys.exit(2)

    try:
        mod = importlib.import_module(mod_path)
        if hasattr(mod, "main") and callable(mod.main):
            import inspect
            sig = inspect.signature(mod.main)
            if sig.parameters:
                # Create proper hidden root BEFORE any icon calls
                _tool_root = tk.Tk()
                _tool_root.withdraw()
                _tool_root.geometry("1x1+-9999+-9999")

                # Apply icon bound to THIS root — never reuse PhotoImage
                # from another Tk instance (causes TclError)
                ico = resource_path("BLGF.ico")
                png = resource_path("BLGF.png")
                if os.path.exists(ico):
                    try:
                        _tool_root.iconbitmap(ico)
                    except Exception:
                        pass
                if os.path.exists(png):
                    try:
                        _img = tk.PhotoImage(file=png, master=_tool_root)
                        _tool_root.iconphoto(True, _img)
                        _tool_root._icon_ref = _img  # prevent GC
                    except Exception:
                        pass

                mod.main(_tool_root)
                _tool_root.mainloop()
            else:
                mod.main()
        sys.exit(0)
    except Exception:
        import traceback
        from tkinter import Tk, messagebox
        r = Tk(); r.withdraw()
        messagebox.showerror("Tool Crash", f"{mod_path}\n\n{traceback.format_exc()}")
        sys.exit(1)

# run BEFORE any GUI is created
IS_TOOL_RUN = dispatch_tool_if_requested()


# Define where icons are located (works both in dev and PyInstaller .exe)
ICONS_DIR = Path(resource_path("icons"))


confirm_win = None
confirm_clicked = {'ok': False}


DB_HOST = ""
DB_PORT = "5432"
DB_NAME = ""
DB_SCHEMA = ""
DEFAULT_DB_USERNAME = "postgres"
DEFAULT_DB_PASSWORD = ""

selected_gmw_file = None

# Initialize stored credentials with defaults
stored_username = DEFAULT_DB_USERNAME
stored_password = DEFAULT_DB_PASSWORD

if not IS_TOOL_RUN:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-alpha", 0)
    root.geometry("1x1+-9999+-9999")
    root.update_idletasks()               # flush any pending window creation events
    apply_icon(root)                      # safe to call now — window is invisible
    root.minsize(340, 200)
    # No fixed geometry — content determines size
    root.title("CAMA Tools")


from geoalchemy2 import Geometry

def ensure_postgis(psycopg_conn):
    with psycopg_conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
    psycopg_conn.commit()

def to_wgs84(gdf):
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326)
    elif getattr(gdf.crs, "to_epsg", lambda: None)() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    return gdf


import geopandas as gpd
import fiona
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from rapidfuzz import fuzz
from tkinter import filedialog, messagebox

from rapidfuzz import process, fuzz
import re

# Suffixes that GM/exports commonly append to layer names
_NOISY_SUFFIXES = (
    "_shp", "_gpkg",
    "_line", "_lines", "_poly", "_polygon", "_polygons",
    "_point", "_points",
    "_multiline", "_multipolygon",
    "_layer", "_export", "_copy"
)

def _strip_noisy_suffixes(s: str) -> str:
    s = s.lower()
    for suf in _NOISY_SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s

def _normalize_name(name: str, schema_prefix: str = "") -> str:
    """
    Normalize names for matching:
    - drop text in parentheses
    - drop extension
    - drop schema prefix like CALAUAN_LAGUNA_
    - lower, collapse non-alnum to underscores
    - drop common noisy suffixes like _shp, _polygon, _export
    - collapse multiple underscores and trim
    """
    name = name.strip()
    if "(" in name:
        name = name.split("(")[0]
    if "." in name:
        name = name.split(".")[0]
    if schema_prefix and name.lower().startswith(schema_prefix.lower()):
        name = name[len(schema_prefix):]
    name = re.sub(r"[^0-9a-zA-Z]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("_").lower()
    name = _strip_noisy_suffixes(name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name

def _name_tokens(name: str) -> list[str]:
    """
    Tokenize a normalized name, and if the first token is digits (e.g., '01'),
    also consider the view without it (so '01_kanluran' can match 'kanluran').
    """
    n = _normalize_name(name)
    tokens = [t for t in n.split("_") if t]
    if tokens and tokens[0].isdigit():
        return [t for t in tokens[1:] if t] or tokens
    return tokens

def _tokens_subset(a_tokens: list[str], b_tokens: list[str]) -> bool:
    return bool(a_tokens and b_tokens) and set(a_tokens).issubset(set(b_tokens))

def _find_best_table(layer_name: str, existing_tables: list, schema_prefix: str) -> str | None:
    """
    Best match for a layer to an existing table:
    1) exact normalized equality
    2) token-subset match (layer ⊆ table OR table ⊆ layer)
    3) substring match (either direction) on normalized strings
    4) fuzzy (token_set_ratio then partial_ratio)
    """
    norm_layer = _normalize_name(layer_name, schema_prefix=schema_prefix + "_")
    layer_tokens = _name_tokens(layer_name)

    norm_map = { _normalize_name(t): t for t in existing_tables }
    if norm_layer in norm_map:
        return norm_map[norm_layer]

    table_tokens_map = { nt: _name_tokens(nt) for nt in norm_map.keys() }

    for nt, orig_tbl in norm_map.items():
        if _tokens_subset(layer_tokens, table_tokens_map[nt]) or _tokens_subset(table_tokens_map[nt], layer_tokens):
            return orig_tbl

    for nt, orig_tbl in norm_map.items():
        if norm_layer in nt or nt in norm_layer:
            return orig_tbl

    if norm_map:
        choices = list(norm_map.keys())
        best1, sc1, _ = process.extractOne(norm_layer, choices, scorer=fuzz.token_set_ratio)
        if sc1 >= 90:
            return norm_map[best1]
        best2, sc2, _ = process.extractOne(norm_layer, choices, scorer=fuzz.partial_ratio)
        if sc2 >= 90:
            return norm_map[best2]

    return None


# Tracks only the currently-visible tooltip Toplevel(s) — not a
# registry of every tooltip ever created. A tooltip is a fully
# independent Toplevel (overrideredirect + its own -topmost), so its
# visibility was previously decoupled from CAMA's own show/hide state:
# if CAMA withdraws (e.g. focus moves to another application) while a
# tooltip is showing, nothing ever told that tooltip to withdraw too,
# leaving it floating on screen even over unrelated windows. This set
# lets monitor_gm_state() withdraw whatever tooltip(s) happen to be
# visible at the moment CAMA itself withdraws. Entries are added in
# enter() and removed in leave() below, so the set only ever contains
# tooltips that are actually on screen right now.
_active_tooltips = set()


def _repin_active_tooltips():
    """
    Z-order fix (independent of the tooltip lifecycle fix above): restores
    topmost status for any currently-visible tooltip(s) right after the
    main CAMA window has just been re-pinned topmost.

    On Windows, when two windows are both marked topmost, whichever one
    was most recently (re-)pinned wins the Z-order. CAMA's own window is
    repeatedly re-pinned topmost (launch, _force_z_order, and the
    periodic recheck in monitor_gm_state) independently of whether a
    tooltip happens to be showing at that moment — so a repin can push
    an already-visible tooltip behind CAMA even though the tooltip's own
    -topmost was set correctly when it first appeared.

    Re-applies the exact same mechanism add_tooltip()'s enter() uses when
    first showing a tooltip (lift() + the "-topmost" attribute) rather
    than lift() alone, since lift() by itself is not always sufficient
    to regain topmost precedence immediately after another window's own
    SetWindowPos(HWND_TOPMOST, ...) call.

    _active_tooltips is expected to contain only currently-visible
    tooltip(s) (enter()/leave() and the withdraw-on-hide path in
    monitor_gm_state() keep it that way) — if a tracked tooltip was
    destroyed out from under us, drop the stale reference instead of
    raising.
    """
    for _tip in list(_active_tooltips):
        try:
            _tip.lift()
            _tip.attributes("-topmost", True)
        except tk.TclError:
            _active_tooltips.discard(_tip)


def add_tooltip(widget, icon_path, title, subtitle="", canvas=None, bg_id=None):
    tooltip = tk.Toplevel(widget)
    tooltip.withdraw()
    tooltip.overrideredirect(True)        # overrideredirect BEFORE apply_icon
    # Skip apply_icon on tooltips — they're borderless so icon is never shown
    # and iconbitmap/iconphoto cause a flash on Toplevel creation
    tooltip.configure(bg="#e0e0e0", padx=1, pady=1)
    tooltip.attributes('-topmost', True)

    outer = tk.Frame(tooltip, bg="#fefefe", relief="solid", borderwidth=1)
    outer.pack()

    content = tk.Frame(outer, bg="#fefefe")
    content.pack(padx=8, pady=6)

    # 🟡 Icon
    icon = Image.open(icon_path).resize((20, 20), Image.Resampling.LANCZOS)
    icon_img = ImageTk.PhotoImage(icon)
    icon_label = tk.Label(content, image=icon_img, bg="#fefefe")
    icon_label.image = icon_img
    icon_label.grid(row=0, column=0, rowspan=2, padx=(0, 6), pady=(2, 0), sticky="n")

    # 🔵 Title (bold)
    title_label = tk.Label(content, text=title.title(), font=("Segoe UI", 10, "bold"), bg="#fefefe", anchor="w", justify="left")
    title_label.grid(row=0, column=1, sticky="w")

    # 🔹 Subtitle (normal)
    subtitle_label = tk.Label(content, text=subtitle, font=("Segoe UI", 9), bg="#fefefe", anchor="w", justify="left")
    subtitle_label.grid(row=1, column=1, sticky="w")

    def get_bounding_rect():
        """Combined bounds of GM window + CAMA window, so tooltip never escapes either."""
        rects = []

        gm = get_gm_rect()  # (left, top, w, h) or None
        if gm:
            gl, gt, gw_, gh = gm
            rects.append((gl, gt, gl + gw_, gt + gh))

        cw, ch = get_cama_size()
        cl, ct = root.winfo_x(), root.winfo_y()
        rects.append((cl, ct, cl + cw, ct + ch))

        if not rects:
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            return (0, 0, sw, sh)

        left   = min(r[0] for r in rects)
        top    = min(r[1] for r in rects)
        right  = max(r[2] for r in rects)
        bottom = max(r[3] for r in rects)
        return (left, top, right, bottom)

    def enter(event):
        tooltip.update_idletasks()
        tip_w = tooltip.winfo_reqwidth()
        tip_h = tooltip.winfo_reqheight()

        bl, bt, br, bb = get_bounding_rect()

        x = widget.winfo_rootx() + 45
        y = widget.winfo_rooty() + 10

        # Clamp horizontally
        if x + tip_w > br:
            x = widget.winfo_rootx() - tip_w - 10  # flip to the left of the widget
            if x < bl:
                x = br - tip_w  # last resort: pin to right edge of bounds
        if x < bl:
            x = bl

        # Clamp vertically
        if y + tip_h > bb:
            y = bb - tip_h
        if y < bt:
            y = bt

        tooltip.geometry(f"+{int(x)}+{int(y)}")
        tooltip.deiconify()
        tooltip.lift()
        tooltip.attributes("-topmost", True)
        _active_tooltips.add(tooltip)  # now visible — track it

        if canvas and bg_id:
            canvas.itemconfig(bg_id, image=hover_bg)

    def leave(event):
        tooltip.withdraw()
        _active_tooltips.discard(tooltip)  # no longer visible — stop tracking it
        root.attributes("-topmost", True)
        if canvas and bg_id:
            canvas.itemconfig(bg_id, image="")

    widget.bind("<Enter>", enter)
    widget.bind("<Leave>", leave)


def extract_actual_name(layer_name: str) -> str:
    # remove anything after " (" which GM sometimes adds
    if "(" in layer_name:
        layer_name = layer_name.split("(")[0]
    # remove file extension, if present
    if "." in layer_name:
        layer_name = layer_name.split(".")[0]
    return layer_name.strip().lower()


def _cleanup_and_raise(save_path, msg):
    """
    Best-effort removal of a partial/never-completed export file, then
    fail loud. GM may still hold the file handle if the export is merely
    slow rather than dead — removal failure is swallowed because the
    pre-export os.remove() at the start of each update function is the
    guaranteed recovery path on the next attempt.
    """
    try:
        if os.path.exists(save_path):
            os.remove(save_path)
    except Exception:
        pass
    raise RuntimeError(msg)


def _wait_for_gpkg_export(save_path, tk_root):
    """
    Bounded poll that waits for Global Mapper to finish exporting a
    GeoPackage. Used by both update_database_from_geopackage() and
    update_map_and_select_recorded().

    SUCCESS — ALL THREE conditions must hold on the same tick:
      1. File size > 1000 bytes AND unchanged for 2 consecutive 1 s
         checks (fast pre-filter; original criterion).
      2. No SQLite sidecar file present (save_path + "-journal" /
         "-wal"). While a sidecar exists the write is still in flight,
         even if the main file's size sits perfectly still — SQLite
         journals activity into the sidecar and only folds it into the
         main file on commit.
      3. Validity probe + LAYER-LIST STABILITY: fiona.listlayers(save_path)
         must succeed, return at least one layer, AND that exact layer
         list must remain UNCHANGED for LAYER_STABILITY_SECONDS (15)
         real wall-clock seconds before the export is declared complete.
         This is time-based, not a fixed count of probe attempts —
         probe cadence itself varies (it only runs once file size has
         re-stabilized, which can take a variable number of ticks), so
         a tick-count threshold would give an inconsistent real-world
         wait time. Size stability alone is NOT a validity signal — GM
         can pause > 2 s mid-write (reprojection of large layers),
         leaving a header-only or mid-transaction file that passes the
         size check but fails GDAL open with "Failed to open dataset
         (flags=68)". Observed in production. Layer-list stability was
         added after confirming (via live instrumentation) that
         FILE-SIZE stability alone is also insufficient for multi-layer
         exports: a small layer (e.g. 111 features) can finish and
         stabilize the file size while GM is still actively appending
         much larger layers (e.g. an 11,911-feature layer) in the
         background — a real observed gap of 6.5+ seconds between
         "file size looks done" and "all layers actually present", and
         up to 4.6s between two later layers appearing. The 15s window
         leaves a wide safety margin above that observed worst case for
         larger datasets on other machines. A failed probe, a
         zero-layer result, or a CHANGED layer list all mean "not ready
         yet", never "error" — they reset the relevant tracking and
         polling continues; the bounded exits below are the only
         failure paths.

    ACTIVITY SIGNAL (stall clock):
        last_change resets on ANY of: main file size change, sidecar
        size change, sidecar appearance/disappearance, OR main file
        mtime change. The mtime check exists because SQLite can rewrite
        pages in place during GM's finalize phase (spatial index build,
        transaction commit on large exports) — the file size stays
        perfectly flat while real write activity continues. Without
        this, size-only tracking could start the stall clock on a
        healthy large export and false-abort it at EXPORT_STALL_TIMEOUT_S
        even though GM is still actively working. mtime granularity on
        NTFS (~10 ms) is far finer than the 1 s poll interval, so this
        adds a reliable activity signal without weakening the existing
        size/sidecar checks — it only ever makes the stall clock MORE
        lenient toward genuinely active exports, never less strict
        about genuinely dead ones.

    FAILURE — all raise RuntimeError with actionable text, which routes
    through each caller's existing except handler (messagebox.showerror):
      - EXPORT_APPEARANCE_TIMEOUT_S: file never created — the export
        keystroke sequence was intercepted by an unexpected GM dialog.
      - EXPORT_STALL_TIMEOUT_S: no write activity (main file OR sidecar
        OR mtime) for this long without passing the success gate. The
        message distinguishes the case where the file was produced but
        never became a readable GeoPackage (probe kept failing).
      - EXPORT_HARD_TIMEOUT_S: absolute ceiling; guarantees the loop
        always terminates.

    DIAGNOSTIC LOGGING:
        Every tick writes one line to _LOG_PATH via _log() — elapsed
        time, file existence, size, mtime age, sidecar state, and
        stability count. Probe exceptions are logged verbatim (never
        swallowed silently) so a failed run can be reconstructed after
        the fact to distinguish an export that never became valid
        (probe kept raising) from one that was simply slow.

    EVENT PUMPING:
        tk_root.update() is called once per iteration so Tk keeps
        servicing the Windows message queue. Without this, even a
        legitimate multi-minute export makes Windows mark the app
        "(Not Responding)" because this loop runs on the Tk main thread
        and blocks mainloop(). Side effect: the UI is LIVE during the
        wait — reentrancy via the two Update buttons is blocked by
        _gm_export_guard, which must wrap every caller of this helper.

        If tk_root has been destroyed mid-wait (user closed Global
        Mapper → monitor_gm_state() → root.destroy()), update() raises
        TclError; we convert that to a loud abort instead of an
        unhandled traceback.

    UX (added later, no change to timing/logic above):
        A small, non-focus-stealing status window is shown for the
        duration of this poll (often 15-30+ seconds) so it doesn't look
        like the app has frozen — same pattern used elsewhere in this
        module (overrideredirect, no focus_force()/grab_set()).
        Wrapped in try/finally so it is destroyed on every exit path
        (the success return, or any _cleanup_and_raise() failure)
        without needing to touch each individual exit point. Shared by
        both callers of this function automatically.
    """
    import time
    start = time.monotonic()
    last_size = -1
    stable_count = 0
    last_change = start          # last observed write activity (main OR sidecar OR mtime)
    last_mtime = None            # last observed main-file mtime, for activity tracking
    sidecar_sizes = {}           # path -> last seen size, for activity tracking
    probe_ever_failed = False    # drives the stall-timeout message wording
    tick = 0                     # poll iteration counter, for log readability
    last_layers = None           # last observed layer list from a successful probe
    layers_last_change = None    # monotonic timestamp when last_layers last actually changed
    LAYER_STABILITY_SECONDS = 15 # required seconds of a genuinely UNCHANGED layer list before
                                  # declaring the export complete. Time-based (not tick-count-based)
                                  # so it isn't sensitive to how often the probe actually runs (probe
                                  # cadence depends on file-size stability, which varies). Set well
                                  # above the largest layer-to-layer gap observed in live testing
                                  # (4.6s between a LandParcel and RoadNetwork layer appearing) to
                                  # leave a wide safety margin for larger datasets on other machines.

    sidecar_paths = (save_path + "-journal", save_path + "-wal")

    _log(f"poll start: save_path={save_path}")

    # UX: non-focus-stealing status window for the duration of this
    # poll. CORRECTED positioning: previously anchored to tk_root (the
    # CAMA Tools panel's own position, which sits bottom-right near GM
    # per launch_main_window()'s own placement logic) - this visually
    # collided with GM's own "Exporting GeoPackage Vector Table"
    # progress dialog and was inconsistent with the other two status
    # windows in update_map_and_select_recorded(), which are anchored
    # to GM's window (upper-left area) instead. Now matches that same
    # anchor point for a consistent, predictable location across all
    # three status windows. Falls back to the old tk_root-relative
    # position only if GM's window cannot be located for any reason.
    # Never calls focus_force()/grab_set(), so it cannot steal keyboard
    # focus during the wait.
    _status_win = None
    try:
        _gm_win_for_status = None
        for _w in gw.getWindowsWithTitle("Global Mapper Pro"):
            if "global mapper" in _w.title.lower():
                _gm_win_for_status = _w
                break
        if _gm_win_for_status is not None:
            _status_x = _gm_win_for_status.left + 20
            _status_y = _gm_win_for_status.top + 20
        else:
            _status_x = tk_root.winfo_x() + 40
            _status_y = tk_root.winfo_y() + 40

        _status_win = tk.Toplevel(tk_root)
        _status_win.overrideredirect(True)
        _status_win.attributes("-topmost", True)
        _status_win.configure(bg="#2b2b2b")
        _status_label = tk.Label(
            _status_win,
            text="Verifying export completed successfully...",
            bg="#2b2b2b", fg="white", font=("Segoe UI", 9),
            padx=12, pady=8
        )
        _status_label.pack()
        _status_win.geometry(f"+{_status_x}+{_status_y}")
        _status_win.update_idletasks()
    except Exception as status_win_err:
        _log(f"status window could not be created (non-fatal): "
             f"{type(status_win_err).__name__}: {status_win_err}")
        _status_win = None

    try:
      while True:
        # Pump the Tk/Windows message queue (see docstring).
        try:
            tk_root.update()
        except tk.TclError:
            _cleanup_and_raise(
                save_path,
                "Global Mapper (or the CAMA Tools window) was closed while "
                "waiting for the export to finish. Export aborted."
            )

        now = time.monotonic()
        if _status_win is not None:
            try:
                _status_label.config(
                    text=f"Verifying export completed successfully... ({now - start:.0f}s)"
                )
            except tk.TclError:
                _status_win = None
        exists = os.path.exists(save_path)

        if exists:
            # --- condition 1: size stability (original criterion) ---
            current_size = os.path.getsize(save_path)
            if current_size == last_size and current_size > 1000:
                stable_count += 1
            else:
                stable_count = 0
            if current_size != last_size:
                last_change = now
            last_size = current_size

            # --- activity signal: mtime (catches in-place page rewrites) ---
            # SQLite can rewrite existing pages during GM's finalize phase
            # (spatial index build, transaction commit) without changing
            # file size. This counts as write activity for the stall
            # clock even when condition 1 sees no size change — it never
            # participates in the success gate itself, only in whether
            # the stall timer resets.
            try:
                current_mtime = os.path.getmtime(save_path)
            except OSError:
                current_mtime = last_mtime  # vanished between exists() and getmtime()
            if last_mtime is not None and current_mtime != last_mtime:
                last_change = now
            last_mtime = current_mtime

            # --- condition 2: sidecar activity / presence ---
            # A present sidecar means the write is in flight regardless
            # of main-file size. Sidecar growth AND sidecar removal
            # (= commit) both count as write activity for the stall clock.
            sidecar_active = False
            for sp in sidecar_paths:
                if os.path.exists(sp):
                    sidecar_active = True
                    try:
                        ssz = os.path.getsize(sp)
                    except OSError:
                        ssz = -1  # vanished between exists() and getsize()
                    if sidecar_sizes.get(sp) != ssz:
                        sidecar_sizes[sp] = ssz
                        last_change = now
                elif sp in sidecar_sizes:
                    del sidecar_sizes[sp]
                    last_change = now  # commit just happened — activity

            if stable_count >= 2:
                if sidecar_active:
                    # Main file looks stable but SQLite is still working.
                    # Not ready — keep waiting.
                    stable_count = 0
                else:
                    # --- condition 3: validity probe + layer-list stability ---
                    #
                    # FIXED: previously returned as soon as this probe
                    # succeeded with a non-empty layer list. Confirmed via
                    # live [WATCH] instrumentation (see project history)
                    # that this is NOT sufficient for multi-layer exports:
                    # when GM exports several layers of very different
                    # size (e.g. a 111-feature POI layer alongside an
                    # 11,911-feature LandParcel layer), the small layer
                    # can finish first and cause the FILE SIZE to look
                    # stable (passing condition 1) while GM is still
                    # actively writing the remaining layers in the
                    # background. A real observed run: file size stable
                    # and probe succeeded at t=2.1s showing only 1 of 3
                    # layers; the 2nd layer did not appear until t=4.0s,
                    # the 3rd not until t=8.6s — a 6.5s gap the old
                    # single-probe gate completely missed, silently
                    # importing only the layers present at that moment.
                    #
                    # Fix: a single successful read is not enough — the
                    # layer LIST returned by the probe must remain
                    # UNCHANGED for LAYER_STABILITY_SECONDS of real
                    # elapsed time before declaring the export complete.
                    # A changed layer list (a new layer appeared) resets
                    # that clock AND counts as write activity for the
                    # stall clock, same as size/sidecar/mtime changes.
                    try:
                        current_layers = fiona.listlayers(save_path)
                        if current_layers:
                            if current_layers != last_layers:
                                _log(f"poll: layer list changed at tick {tick}: "
                                     f"{last_layers} -> {current_layers}")
                                last_layers = current_layers
                                layers_last_change = now
                                last_change = now  # a growing layer list is write activity

                            layers_stable_duration = (
                                now - layers_last_change if layers_last_change is not None else 0.0
                            )
                            if layers_stable_duration >= LAYER_STABILITY_SECONDS:
                                _log(f"poll: probe succeeded and layer list stable for "
                                     f"{layers_stable_duration:.1f}s at tick {tick} — "
                                     f"export complete. Final layers: {current_layers}")
                                return  # success — file is a readable GPKG with a stable layer set
                            else:
                                # Not yet layer-stable — require file size to
                                # re-prove stability again before the next
                                # probe attempt (same pattern as the other
                                # "not ready" paths below); the layer-stability
                                # clock (layers_last_change) is untouched by
                                # this reset, so it keeps counting correctly
                                # across multiple probe cycles.
                                stable_count = 0
                        else:
                            probe_ever_failed = True   # zero layers: not ready
                            stable_count = 0
                            last_layers = None
                            layers_last_change = None
                            _log(f"poll: probe returned zero layers at tick {tick} — not ready")
                    except Exception as probe_err:
                        probe_ever_failed = True
                        stable_count = 0
                        last_layers = None
                        layers_last_change = None
                        _log(f"poll: probe FAILED at tick {tick}: "
                             f"{type(probe_err).__name__}: {probe_err}")

        # --- per-tick diagnostic log ---
        mtime_age = (now - last_change)
        _layers_stable_for = (
            f"{now - layers_last_change:.1f}s" if layers_last_change is not None else "n/a"
        )
        _log(
            f"poll t={now - start:.1f}s tick={tick} exists={exists} "
            f"size={last_size if exists else '-'} "
            f"stall_age={mtime_age:.1f}s stable={stable_count} "
            f"layers_stable_for={_layers_stable_for} last_layers={last_layers} "
            f"sidecars={list(sidecar_sizes.keys())}"
        )
        tick += 1

        # --- bounded exits (fail loud, never spin forever) ---
        if not exists and (now - start) > EXPORT_APPEARANCE_TIMEOUT_S:
            _log(f"poll ABORT: appearance timeout at t={now - start:.1f}s")
            _dump_windows("export appearance timeout")
            _cleanup_and_raise(
                save_path,
                f"Global Mapper never created the export file within "
                f"{EXPORT_APPEARANCE_TIMEOUT_S} seconds.\n\n"
                f"The export keystroke sequence was most likely intercepted "
                f"by an unexpected dialog in Global Mapper (e.g. 'Lidar "
                f"Filter Settings', a license prompt, or a permission "
                f"dialog).\n\n"
                f"Close any open dialog in Global Mapper and try again."
            )
        if exists and (now - last_change) > EXPORT_STALL_TIMEOUT_S:
            _log(f"poll ABORT: stall timeout at t={now - start:.1f}s "
                 f"(probe_ever_failed={probe_ever_failed})")
            _dump_windows("export stall timeout")
            if probe_ever_failed:
                _cleanup_and_raise(
                    save_path,
                    f"Global Mapper produced an export file, but it never "
                    f"became a readable GeoPackage within "
                    f"{EXPORT_STALL_TIMEOUT_S} seconds of the last write "
                    f"activity — the export likely died mid-write behind "
                    f"a dialog.\n\n"
                    f"Check Global Mapper for an open dialog or error, "
                    f"close it, and try again."
                )
            _cleanup_and_raise(
                save_path,
                f"The export file stopped growing for "
                f"{EXPORT_STALL_TIMEOUT_S} seconds without completing. "
                f"Global Mapper may be blocked by a dialog.\n\n"
                f"Close any open dialog in Global Mapper and try again."
            )
        if (now - start) > EXPORT_HARD_TIMEOUT_S:
            _log(f"poll ABORT: hard timeout at t={now - start:.1f}s")
            _dump_windows("export hard timeout")
            _cleanup_and_raise(
                save_path,
                f"Export did not complete within "
                f"{EXPORT_HARD_TIMEOUT_S // 60} minutes.\n\n"
                f"Close any open Global Mapper dialog and retry."
            )

        time.sleep(1)
    finally:
        try:
            if _status_win is not None:
                _status_win.destroy()
        except Exception:
            pass


# ── Reentrancy guard for GM keystroke automation ─────────────────────
_gm_automation_in_flight = False


def _gm_export_guard(func):
    """
    Serializes the GM keystroke-automation entry points
    (update_database_from_geopackage / update_map_and_select_recorded).

    Why this exists: _wait_for_gpkg_export() pumps the Tk event loop
    (root.update()) to prevent Windows marking the app "(Not
    Responding)" during a slow export. That makes the UI live during
    the wait, so a second click on EITHER Update button could start a
    second blind keystroke sequence against the same Global Mapper
    window. Previously the frozen UI accidentally acted as a mutex;
    this guard makes that protection explicit.

    - Disables BOTH update buttons for the duration — both drive
      keystroke automation against the same GM window, so cross-
      function interleaving is as dangerous as same-function reentry.
    - The module-level flag is belt-and-braces against click events
      queued before the disable takes effect. Re-entry is a silent
      no-op by design: with the buttons disabled it can only trip via
      that race, and it is not a user-facing state.
    - The finally block covers every exit path: early returns
      (credential guard, GM window not found, locked temp file),
      export failure/timeout, DB failure, and success. Widget access
      is wrapped because root may have been destroyed mid-operation
      (GM closed → monitor_gm_state() → root.destroy()).

    NOTE: buttons are looked up as globals at CALL time, so decorating
    these functions before update_btn / update_map_btn exist is safe —
    both buttons are created before any click can occur.
    """
    def wrapper(*args, **kwargs):
        global _gm_automation_in_flight
        if _gm_automation_in_flight:
            return  # silent no-op on re-entry attempt (see docstring)
        _gm_automation_in_flight = True
        try:
            for b in (update_btn, update_map_btn):
                try:
                    b.config(state="disabled")
                except Exception:
                    pass
            return func(*args, **kwargs)
        finally:
            _gm_automation_in_flight = False
            for b in (update_btn, update_map_btn):
                try:
                    b.config(state="normal")
                except Exception:
                    pass
    return wrapper


@_gm_export_guard
def update_database_from_geopackage():
    import pygetwindow as gw
    import pyautogui
    import time
    import os
    import geopandas as gpd
    import fiona
    from tkinter import messagebox
    from sqlalchemy import create_engine
    from sqlalchemy.engine import URL
    from geoalchemy2 import Geometry  # needed for dtype in to_postgis

    pyautogui.FAILSAFE = False
    _log_session_start("update_database_from_geopackage")

    def _wait_and_activate(title, timeout=2.0, poll=0.1):
        """
        Poll for a top-level window whose title matches `title` and
        activate it as soon as it appears, instead of assuming a fixed
        sleep is long enough on every machine. Used before the Tip and
        GeoPackage Export Options dialogs' confirming Enter keypress —
        both are GM-rendered dialogs whose appearance timing is not
        guaranteed to be constant across machines/load.

        Bounded by `timeout` so a dialog that never appears (e.g. GM's
        flow changed, or "Don't Show This Again" was previously checked
        on the Tip dialog) fails safe: the caller logs a WARNING and
        still sends the keystroke, matching this function's existing
        "never hang silently, always leave a log trail" pattern (see
        _wait_for_gpkg_export's diagnostic logging for the same philosophy).

        Local to update_database_from_geopackage() — not shared with
        update_map_and_select_recorded() or any other function, per this
        task's scope (only this function may be modified).
        """
        elapsed = 0.0
        while elapsed < timeout:
            wins = gw.getWindowsWithTitle(title)
            if wins:
                wins[0].activate()
                return True
            time.sleep(poll)
            elapsed += poll
        return False



    if not all([stored_username, stored_password]):
        _log("ABORT: not logged in")
        messagebox.showerror("Error", "You must log in first before updating the database.")
        return

    # ── Manual pre-flight confirmation ──────────────────────────────────
    # The GM right-click context menu has NO "Layer -> EXPORT..." path at
    # all when zero layers are highlighted (a different, canvas-level menu
    # opens instead — confirmed by screenshot). There is no scripting API
    # in use here to detect highlight state programmatically, so this is a
    # manual checkpoint, not real validation: a user who clicks "Yes"
    # without actually highlighting a layer will still hit the wrong menu
    # downstream. This dialog only prevents the *unattended/forgot* case.
    proceed = messagebox.askyesno(
        "Confirm Before Updating Database",
        "Before continuing, please make sure that:\n\n"
        "\u2022 The layer you want to update is the only one highlighted in Global Mapper's Control Center.\n"
        "\u2022 The highlighted layer is a single file/child layer, NOT "
        "the parent/group entry (one with a + expand icon in the "
        "Control Center) \u2014 selecting the parent node is not "
        "supported; select the child layer(s) inside it instead.\n\n"
        "Do not switch windows (Alt+Tab) or interact with your computer "
        "while Update Database is running \u2014 this may interrupt the "
        "automated process.\n\n"
        "Proceed with Update Database?"
    )
    if not proceed:
        _log("ABORT: user cancelled at pre-automation confirmation dialog")
        return

    try:
        # Step 1: Focus Global Mapper
        gm_window = None
        for w in gw.getWindowsWithTitle("Global Mapper Pro"):
            if "global mapper" in w.title.lower():
                gm_window = w
                break
        if not gm_window:
            _log("ABORT: Global Mapper window not found")
            _dump_windows("GM window not found")
            messagebox.showerror("Error", "Global Mapper window not found.")
            return

        _log(f"GM window found: '{gm_window.title}' "
             f"rect=({gm_window.left}, {gm_window.top}, "
             f"{gm_window.width}x{gm_window.height}) "
             f"minimized={gm_window.isMinimized}")

        gm_window.minimize(); time.sleep(0.1)
        gm_window.restore(); time.sleep(0.1)
        gm_window.activate(); time.sleep(0.3)
        _log(f"GM focused | fg='{_fg_title()}'")

        # DEFENSIVE: re-verify TEMP_DIR exists right before using it,
        # not just once at app startup. The module-level os.makedirs()
        # near the top of this file only runs one time, when the app
        # first launches. If this folder is deleted mid-session (by the
        # user, antivirus, a cleanup utility, or anything else) — or
        # simply doesn't reliably exist on every target machine at this
        # exact moment — every export from this point on would fail
        # inside Global Mapper's own Save As dialog with "Path does not
        # exist", a failure this code has no visibility into and cannot
        # distinguish from other Save As problems. Re-creating here
        # (exist_ok=True, so this is a no-op in the normal case) closes
        # that gap with a clear, accurate error if it genuinely cannot
        # be created (e.g. permissions), instead of a confusing GM-side
        # dialog error later in the sequence.
        try:
            os.makedirs(TEMP_DIR, exist_ok=True)
        except Exception as e:
            _log(f"ABORT: could not create/verify {TEMP_DIR}: {e}")
            messagebox.showerror(
                "Folder Error",
                f"Could not create or access the required temp folder:\n"
                f"{TEMP_DIR}\n\n{e}"
            )
            return

        # Save path for exported GPKG
        save_path = os.path.join(TEMP_DIR, "savetodb.gpkg")

        # Delete if already exists
        if os.path.exists(save_path):
            try:
                os.remove(save_path)
                _log(f"pre-export cleanup: removed stale {save_path}")
            except Exception as e:
                _log(f"ABORT: could not delete stale export file: {e}")
                messagebox.showerror("File Error", f"Could not delete existing file:\n{e}")
                return
        else:
            _log("pre-export cleanup: no stale export file present")

        # Step 2: Trigger Save via virtual right-click in left panel
        _dump_windows("before export right-click")
        pyautogui.hotkey("ctrl", "s")  # Save project first
        time.sleep(0.3)

        real_mouse_pos = pyautogui.position()
        gm_window.activate()
        time.sleep(0.05)
        left_panel_x = gm_window.left + 25
        left_panel_y = gm_window.top + 500
        pyautogui.moveTo(left_panel_x, left_panel_y)
        pyautogui.rightClick()
        pyautogui.moveTo(real_mouse_pos)

        # Step 3: Keyboard sequence for export
        #
        # VERIFIED MANUALLY (screenshots + user confirmation) — the real
        # GM flow from this right-click context menu is SEVEN distinct
        # dialogs/steps, not the four the old sequence assumed:
        #   1. Right-click menu -> hover "Layer" submenu (LAST item in the
        #      menu — item count above it is NOT stable, see fix below)
        #   2. "Layer" submenu -> "EXPORT - Export Layer(s) to New File..."
        #   3. "Select Layers" dialog -> OK (submits the highlight-derived
        #      default state as-is — Check All is no longer pressed)
        #   4. "Select Export Format" dialog -> pick "Geopackage"
        #   5. "Tip" info dialog -> OK
        #   6. "GeoPackage Export Options" dialog -> OK
        #   7. "Save As" dialog -> type path -> Save
        #
        # The old 4-action sequence (up/right/down/enter/enter/"a"/"gggggg"
        # /enter) skipped steps 3 and 6 entirely and used an unreliable
        # type-ahead guess for step 4 — confirmed via _wait_for_gpkg_export
        # logging to always fail the fiona validity probe (flags=68),
        # because the exported "savetodb.gpkg" was never a real export:
        # the keystrokes were landing on the wrong dialogs from the start.
        _log(f"export context menu invoked | fg='{_fg_title()}'")
        time.sleep(0.05)

        # --- Step 1-2: navigate to Layer submenu -> EXPORT ---
        #
        # FIXED (was): a fixed "down x16" count assumed 16 non-submenu
        # items always precede "Layer". Confirmed via screenshot this is
        # NOT stable — the menu has 17 items above "Layer" when 1 layer is
        # highlighted, and 19 when 2+ are highlighted (two extra items,
        # "DESCRIPTION - Edit the Selected Layer's Description..." and
        # "Open Selected Map Folder in Windows Explorer...", appear only
        # in the multi-select case). The fixed count consistently
        # overshot by landing on "Layer Order" (the sibling submenu
        # directly above "Layer") instead of "Layer" itself.
        #
        # FIX: count from the bottom instead of the top. "Layer" is
        # confirmed (via screenshot, both 1-layer and 2-layer highlighted
        # cases) to always be the LAST item in this context menu,
        # regardless of how many items precede it. A single "up" press on
        # a freshly-opened menu relies on standard Windows menu
        # wraparound (Up on the first-focused item wraps to the last
        # item), landing directly on "Layer" without needing to know the
        # item count above it at all.
        #
        # The "Layer" submenu itself has a fixed, confirmed 2-item order
        # regardless of highlight count: 1) "Create Workspace File from
        # Selected Layer(s)...", 2) "EXPORT - Export Layer(s) to New
        # File...". Submenu opens with nothing focused, so down x2 reaches
        # "EXPORT...".
        #
        # NOTE: this does not handle the zero-layers-highlighted case —
        # that opens a different, canvas-level menu entirely (no "Layer"
        # submenu present at all). That case is addressed upstream via the
        # manual confirmation dialog, not here; see docstring note there.
        pyautogui.press("up")     # wraps to last item = "Layer"
        pyautogui.press("enter")  # open "Layer" submenu
        time.sleep(0.1)
        pyautogui.press("down", presses=2, interval=0.05)  # "EXPORT - Export Layer(s) to New File..."
        pyautogui.press("enter")
        _log(f"EXPORT menu item selected | fg='{_fg_title()}'")

        # ============================================================
        # COMBINED POLL across "Select Layers" / "Tip" / "Select Export
        # Format" (new) - a live test confirmed GM can skip straight to
        # "Select Export Format" after the EXPORT menu item is
        # selected, bypassing "Select Layers" entirely for reasons not
        # yet understood. Detect whichever of the three actually
        # appears and skip the confirming keystroke(s) for any earlier
        # step(s) GM already bypassed, instead of treating a missing
        # "Select Layers" as an automatic failure. Same two-round,
        # 5.0s-each pattern as the existing Tip/GeoPackage Export
        # Options poll below, for the same reason (distinguish "took a
        # bit longer" from "genuinely never appeared").
        # ============================================================
        _step1_result = None
        _current_fg = _fg_title()
        for _poll_round in (1, 2):
            _round_start = time.monotonic()
            while time.monotonic() - _round_start < 5.0:
                _current_fg = _fg_title()
                if "select layers" in _current_fg.lower():
                    _step1_result = "select_layers"
                    break
                if _current_fg.strip() == "Tip":
                    _step1_result = "tip"
                    break
                if "select export format" in _current_fg.lower():
                    _step1_result = "select_export_format"
                    break
                time.sleep(0.2)
            if _step1_result is not None:
                _log(f"combined poll (Select Layers/Tip/Select Export "
                     f"Format) round {_poll_round}: found "
                     f"'{_step1_result}' after "
                     f"{time.monotonic() - _round_start:.1f}s | fg='{_current_fg}'")
                break
            _log(f"combined poll (Select Layers/Tip/Select Export "
                 f"Format) round {_poll_round}: none seen within 5.0s "
                 f"| fg='{_current_fg}'")

        # SAFETY ABORT: if none of the three appeared after both
        # rounds, this is a genuine navigation failure (e.g. a
        # nested/grouped layer entry changed what options exist in
        # GM's right-click menu) - abort with a clear, actionable
        # error instead of continuing to send keystrokes blind.
        if _step1_result is None:
            _dump_windows("export navigation lost focus after EXPORT menu item (update_database)")
            raise RuntimeError(
                "Update Database was aborted before any further "
                "keystrokes were sent to avoid typing into the wrong "
                "window.\n\n"
                "Please select the layer you want to update and make "
                "sure that what you select is a layer without an "
                "expand icon ('+' or '-') to its left."
            )

        # --- Step 3: "Select Layers" dialog -> OK (no Check All) ---
        #
        # Confirmed via manual testing: this dialog's default checkbox
        # state, when it opens, already reflects exactly which layer(s)
        # were highlighted beforehand (highlighted -> pre-checked,
        # non-highlighted -> unchecked) — the user's highlight selection
        # IS the intended export selection. "OK" is already the
        # default-focused button the instant this dialog opens, so a
        # single Enter submits it as-is. Skipped entirely if GM already
        # bypassed this dialog (detected via the combined poll above).
        if _step1_result == "select_layers":
            pyautogui.press("enter")  # "OK" — confirmed default-focused on dialog open
            _log(f"Select Layers dialog confirmed (OK, no Check All, no tab) | fg='{_fg_title()}'")
        elif _step1_result == "select_export_format":
            _log("'Select Layers' was not shown - 'Select Export "
                 f"Format' already focused | fg='{_fg_title()}'")
        else:
            _log("'Select Layers' and 'Select Export Format' were both "
                 f"not shown - 'Tip' already focused | fg='{_fg_title()}'")

        # --- Step 4: "Select Export Format" dialog -> Geopackage ---
        #
        # CONFIRMED via live test + screenshots: PageUp x20 resets the
        # dropdown to its first entry ("2DM File"), followed by "g" x6
        # type-ahead to advance the selection to "Geopackage". The
        # type-ahead commits the combobox value directly, so a single
        # Enter here activates the dialog's OK button. Skipped entirely
        # if GM already bypassed this dialog too (jumped straight to
        # "Tip").
        if _step1_result in ("select_layers", "select_export_format"):
            pyautogui.press("pageup", presses=20, interval=0.03)  # reset dropdown to top ("2DM File")
            time.sleep(0.3)
            for i in range(6):
                pyautogui.press("g")
                time.sleep(0.15)
            _log(f"Select Export Format: Geopackage type-ahead complete | fg='{_fg_title()}'")
            pyautogui.press("enter")  # OK on "Select Export Format" dialog
            _log(f"Select Export Format: OK pressed | fg='{_fg_title()}'")

        # --- Steps 5-6: "Tip" (optional) then "GeoPackage Export
        # Options" (mandatory) ---
        #
        # "Tip" is LEGITIMATELY OPTIONAL: if the user previously checked
        # "Don't Show This Again", GM skips straight to "GeoPackage
        # Export Options" and "Tip" never appears at all — a normal,
        # expected case, not a failure. If the combined poll above
        # already landed on "Tip" directly, skip this second poll
        # entirely and go straight to confirming it.
        if _step1_result == "tip":
            _tip_or_geo_result = "tip"
        else:
            _tip_or_geo_result = None
            _last_fg_seen = _fg_title()
            for _poll_round in (1, 2):
                _round_start = time.monotonic()
                while time.monotonic() - _round_start < 5.0:
                    _last_fg_seen = _fg_title()
                    if _last_fg_seen.strip() == "Tip":
                        _tip_or_geo_result = "tip"
                        break
                    if "geopackage export options" in _last_fg_seen.lower():
                        _tip_or_geo_result = "export_options"
                        break
                    time.sleep(0.2)
                if _tip_or_geo_result is not None:
                    _log(f"combined poll round {_poll_round}: found "
                         f"'{_tip_or_geo_result}' after "
                         f"{time.monotonic() - _round_start:.1f}s | fg='{_last_fg_seen}'")
                    break
                _log(f"combined poll round {_poll_round}: neither 'Tip' nor "
                     f"'GeoPackage Export Options' seen within 5.0s | "
                     f"fg='{_last_fg_seen}'")

            if _tip_or_geo_result is None:
                _dump_windows("neither Tip nor GeoPackage Export Options appeared (update_database)")
                raise RuntimeError(
                    "Export navigation failed: neither the 'Tip' dialog "
                    "nor the 'GeoPackage Export Options' dialog appeared "
                    f"within the expected time (focused window was "
                    f"'{_last_fg_seen}' instead). Update Database was "
                    "aborted before any further keystrokes were sent, to "
                    "avoid typing into the wrong window."
                )

        if _tip_or_geo_result == "tip":
            _log(f"Tip dialog focused | fg='{_fg_title()}'")
            pyautogui.press("enter")
            _log(f"Tip dialog OK | fg='{_fg_title()}'")
            # "Tip" confirmed - now specifically wait for "GeoPackage
            # Export Options", which is NOT skippable (always appears).
            if not _wait_and_activate("GeoPackage Export Options", timeout=5.0):
                _dump_windows("GeoPackage Export Options dialog not found after Tip (update_database)")
                raise RuntimeError(
                    "Export navigation failed: the 'GeoPackage Export "
                    "Options' dialog did not appear after confirming "
                    "the 'Tip' dialog. Update Database was aborted "
                    "before any further keystrokes were sent, to avoid "
                    "typing into the wrong window."
                )
            _log(f"GeoPackage Export Options focused | fg='{_fg_title()}'")

        else:
            _log("'Tip' was not shown (likely 'Don't Show This Again' "
                 f"was previously checked) - 'GeoPackage Export "
                 f"Options' already focused | fg='{_fg_title()}'")
        # Confirm "GeoPackage Export Options" - reached only via the
        # two valid paths above (Tip confirmed then GeoPackage Export
        # Options found, or GeoPackage Export Options found directly).
        # Confirmed via screenshot: default focus is already on "OK",
        # and the default state (Export Areas/Lines/Points all checked,
        # "Split data by feature layer name" checked) is exactly what's
        # wanted - no fields need to be touched here, just confirm.
        pyautogui.press("enter")
        _log(f"GeoPackage Export Options OK | fg='{_fg_title()}'")

        # --- Step 7: "Save As" dialog appears here (verified) ---

        # Step 4: Navigate Save dialog
        print("Waiting for Save As dialog...")

        # SAFETY: abort rather than continuing blind if this dialog is
        # not actually found — a live test in update_map_and_select_recorded()
        # confirmed exactly this failure typed the export path into an
        # unrelated browser tab instead of Global Mapper's Save As dialog.
        #
        # FIXED: previously used a single one-shot check after only a
        # 50ms sleep (not enough time for the dialog to actually
        # render), causing false aborts even when Save As was about to
        # appear normally, as confirmed by a live test where the dialog
        # was visibly open when the abort fired. Now uses the same
        # _wait_and_activate() polling helper already used for Tip and
        # GeoPackage Export Options above. Timeout standardized to 5.0s
        # (was briefly 3.0s) across every abort-capable check in this
        # function, so a slower user machine has consistent, adequate
        # room before any of them false-aborts.
        if _wait_and_activate("Save As", timeout=5.0):
            save_win = gw.getWindowsWithTitle("Save As")[0]
            save_win.activate()
            time.sleep(0.05)
            _log(f"Save As dialog found and activated | fg='{_fg_title()}'")
        else:
            _dump_windows("Save As not found (update_database)")
            raise RuntimeError(
                "Export navigation failed: the 'Save As' dialog did "
                "not appear within the expected time. Update Database "
                "was aborted before typing the export path, to avoid "
                "typing it into the wrong window."
            )

        # Focus filename field and type the full absolute path directly.
        #
        # REMOVED: a prior "Alt+D -> type C:\\ -> Enter" address-bar
        # navigation step here had a bug (typewrite(r"C:\\") is a raw
        # string literal containing TWO backslashes, not one — confirmed
        # by the resulting Windows error "Windows can't find 'C:\\'."
        # which matches that exact literal 1:1).
        #
        # More importantly, that whole step was redundant, not just
        # buggy: this dialog is the standard Windows common Save dialog
        # (confirmed by its chrome — Organize/New folder ribbon, breadcrumb
        # address bar, OneDrive/This PC/Quick Access nav pane, sortable
        # Name/Date modified/Type/Size columns — identical to any native
        # Windows Save/Open picker, not something GM renders itself). This
        # dialog type accepts a full absolute path typed directly into the
        # filename field and navigates + saves there in one step, with no
        # need to pre-navigate the address bar first. `save_path` is
        # already absolute (os.path.join(TEMP_DIR, "savetodb.gpkg"), and
        # TEMP_DIR = r"C:\Global Mapper Temp" is created at app startup on
        # every machine), so typing it directly is both simpler and more
        # reliable — it no longer depends on whatever folder the dialog
        # happened to be showing beforehand (dialog "recent location"
        # state, which this code does not control).
        pyautogui.hotkey("alt", "n")
        pyautogui.typewrite(save_path)
        pyautogui.press("enter")

        # Step 5: Wait until the file is fully saved (size stable AND
        # layer-list stable — see _wait_for_gpkg_export() for the
        # layer-stability fix added after this was confirmed necessary).
        # Bounded poll — raises RuntimeError on timeout, which is caught
        # by the "Export Failed" handler below (DB block never entered).
        print("Waiting for file to be fully written...")
        _wait_for_gpkg_export(save_path, root)
        _log(f"export phase complete: {save_path}")
        print("File saved:", save_path)

    except Exception as e:
        _log(f"EXPORT FAILED: {type(e).__name__}: {e}")
        messagebox.showerror("Export Failed", f"Export failed:\n{e}")
        return

    try:
        # Step 6: Import into PostgreSQL with smart matching
        connection_url = URL.create(
            drivername="postgresql+psycopg2",
            username=stored_username,
            password=stored_password,
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME
        )
        engine = create_engine(connection_url)

        conn = engine.connect()
        cursor = conn.connection.cursor()

        # make sure PostGIS types exist
        ensure_postgis(conn.connection)

        def _get_spatial_index_name(schema, table_name, column_name="geom"):
            """
            Look up the actual name of the GIST spatial index attached to a
            given column, using PostgreSQL's own catalog tables (pg_class,
            pg_index, pg_am, pg_attribute) — never assumes any naming
            convention such as "idx_<table>_geom". This makes the index
            rename step in Step E immune to identifier truncation
            (PostgreSQL's 63-char NAMEDATALEN limit) or any future change
            in how GeoPandas/GeoAlchemy2 names auto-generated spatial
            indexes. Verified empirically against a deliberately
            unrelated index name during development — the lookup found
            it correctly regardless of what it was named.

            Returns None if no spatial index exists on this column (e.g.
            brand-new table before any index was ever created).

            Raises RuntimeError if MORE than one GIST index is found on
            the same column — this should never happen in the normal
            single-spatial-index-per-geometry-column case this pipeline
            creates, so silently picking one (e.g. via fetchone()) would
            hide a genuinely unexpected schema state instead of
            surfacing it.
            """
            cursor.execute(
                """
                SELECT ix.relname AS index_name
                FROM pg_class t
                JOIN pg_namespace n ON n.oid = t.relnamespace
                JOIN pg_index idx ON idx.indrelid = t.oid
                JOIN pg_class ix ON ix.oid = idx.indexrelid
                JOIN pg_am am ON am.oid = ix.relam
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(idx.indkey)
                WHERE n.nspname = %s AND t.relname = %s
                  AND a.attname = %s AND am.amname = 'gist';
                """,
                (schema, table_name, column_name)
            )
            rows = cursor.fetchall()
            if len(rows) == 0:
                return None
            elif len(rows) == 1:
                return rows[0][0]
            else:
                raise RuntimeError(
                    f"Expected at most one GIST spatial index on "
                    f"\"{schema}\".\"{table_name}\".\"{column_name}\", but found "
                    f"{len(rows)}: {[r[0] for r in rows]}. Schema is in an "
                    f"unexpected state — aborting rather than guessing "
                    f"which index to rename."
                )

        def _index_exists(schema, index_name):
            """Check whether an index of this exact name already exists
            in the given schema, used as a pre-flight check before
            renaming so a collision produces a clear diagnostic instead
            of a generic PostgreSQL duplicate-object error."""
            cursor.execute(
                "SELECT 1 FROM pg_indexes WHERE schemaname = %s AND indexname = %s;",
                (schema, index_name)
            )
            return cursor.fetchone() is not None

        def _rename_spatial_index(schema, table_name, desired_name, column_name="geom"):
            """
            Find the actual spatial index on table_name/column_name via
            _get_spatial_index_name() and rename it to desired_name.
            No-op if no spatial index exists on this column. Raises a
            clear diagnostic (rather than letting PostgreSQL fail with a
            generic DuplicateTable/DuplicateObject error) if desired_name
            is already taken by something else in the schema.
            """
            current_name = _get_spatial_index_name(schema, table_name, column_name)
            if current_name is None:
                return  # nothing to rename — table has no spatial index yet
            if current_name == desired_name:
                return  # already correctly named, nothing to do
            if _index_exists(schema, desired_name):
                raise RuntimeError(
                    f"Spatial index rename aborted. Target index name "
                    f"'{desired_name}' already exists in schema '{schema}'. "
                    f"Schema state is inconsistent — a previous swap likely "
                    f"left an index behind under this name. Manual cleanup "
                    f"may be required."
                )
            cursor.execute(
                f'ALTER INDEX "{schema}"."{current_name}" RENAME TO "{desired_name}";'
            )

        cursor.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s;",
            (DB_SCHEMA,)
        )
        existing_tables = [row[0] for row in cursor.fetchall()]

        layers = fiona.listlayers(save_path)
        schema_prefix = DB_SCHEMA

        # TEMPORARY DIAGNOSTIC — proving/disproving the "connection closes
        # mid-loop" hypothesis by showing exactly what fiona reports the
        # .gpkg contains, and confirming the loop actually reaches each
        # layer. Remove once the multi-layer investigation is closed.
        _log(f"Layers returned by fiona.listlayers(): {layers}")

        # Tracks which tables actually reached a successful swap COMMIT,
        # for the summary dialog below. A layer only lands here after its
        # atomic rename swap (Step E) has committed — layers skipped via
        # the empty-layer user decline (Step D "soft warning: zero rows")
        # or that raised during the swap (caught by the except block,
        # which re-raises and aborts the whole run) never reach this list.
        updated_tables = []

        # Ordered candidate list for primary-key resolution (Step D.5,
        # below). Case-insensitive match against staging_columns. First
        # candidate found with zero NULLs and zero duplicates is promoted
        # to PRIMARY KEY; none exist and none qualify -> a surrogate
        # ud_id SERIAL PRIMARY KEY is created instead. Deliberately flat
        # and deterministic — the importer does not attempt to judge
        # whether a column is a "real" business identifier vs. an export
        # artifact (e.g. objectid/fid); it only checks NOT NULL + UNIQUE.
        # To support a future shapefile schema with its own identifier
        # column (e.g. BUILDING_ID, LOT_ID), add one line here — no other
        # code in this function needs to change.
        PK_CANDIDATES = [
            "id",
            "pin",
            "parcel_id",
            "road_id",
            "poi_id",
            "bldg_id",
            "gid",
            "objectid",
            "fid",
        ]

        for layer in layers:
            # TEMPORARY DIAGNOSTIC — remove once the multi-layer
            # investigation is closed. Confirms the loop is actually
            # reached for this layer (vs. layers list only containing
            # one entry in the first place).
            _log(f"Starting layer: {layer}")
            # ---------------------------------------------------------------
            # SAFE REPLACE WORKFLOW
            #
            # Previous behavior (UNSAFE):
            #   DROP existing table → commit → write new data
            #   If write failed after commit: old data permanently lost.
            #
            # Current behavior (SAFE):
            #   Write new data to _staging → validate → atomic rename swap
            #   → drop backup only after successful commit.
            #   Old table is never dropped until new data is confirmed live.
            #
            # Failure handling:
            #   Any exception at any step cleans up the _staging orphan and
            #   leaves the original table completely untouched.
            # ---------------------------------------------------------------

            # Step A: Read layer from GPKG into memory.
            gdf = gpd.read_file(save_path, layer=layer)
            gdf.columns = [col.lower() for col in gdf.columns]

            # Force WGS84 — all layers stored as EPSG:4326 in PostGIS.
            gdf = to_wgs84(gdf)
            gdf = gdf.rename_geometry("geom")

            # Geometry type stored as generic GEOMETRY with SRID 4326.
            # Using GEOMETRY (not MULTIPOLYGON etc.) because GM exports
            # may promote geometry types (Polygon → MultiPolygon) and
            # strict type enforcement causes false validation failures.
            #
            # spatial_index=False: GeoAlchemy2's Geometry type defaults to
            # spatial_index=True, which creates a GIST index
            # (idx_<table>_<column>) via a DDL event the moment the table
            # is created. GeoPandas' to_postgis() separately creates its
            # own spatial index on the geometry column after loading data,
            # using the same naming convention. Both mechanisms racing to
            # create the identically-named index caused:
            #   psycopg2.errors.DuplicateTable: relation
            #   "idx_<table>_staging_geom" already exists
            # Confirmed via live test (Database Update Failed dialog).
            # GeoPandas' own index creation already covers this, so
            # GeoAlchemy2's is disabled here to avoid the collision.
            dtype = {"geom": Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False)}

            # Step B: Determine target table name via fuzzy match.
            # _find_best_table() is unchanged — only the replacement
            # strategy below has been modified.
            match_table = _find_best_table(layer, existing_tables, schema_prefix=schema_prefix)
            if match_table:
                target_name = match_table
                print(f"Safe-replacing table: {target_name}  <- layer: {layer}")
            else:
                new_table = _normalize_name(layer, schema_prefix=schema_prefix + "_")
                if not new_table:
                    new_table = "layer_" + str(abs(hash(layer)))
                target_name = new_table
                print(f"Creating new table: {target_name}  <- layer: {layer}")

            staging_name = f"{target_name}_staging"
            backup_name  = f"{target_name}_backup"

            # Step C: Write new data to staging table.
            # The existing target table is completely untouched at this point.
            # if_exists="replace" handles orphaned staging tables from a
            # previous interrupted run — idempotent on retry.
            print(f"  Writing to staging: {staging_name}")
            gdf.to_postgis(
                name=staging_name,
                con=engine,
                schema=DB_SCHEMA,
                if_exists="replace",
                index=False,
                dtype=dtype
            )

            # Step D: Validate staging table.
            #
            # Hard requirements (abort if either fails):
            #   1. Staging table exists in information_schema — confirms
            #      to_postgis completed and PostgreSQL registered the table.
            #   2. Geometry column "geom" exists in staging — confirms the
            #      spatial data was written, not just attribute columns.
            #      A missing geometry column means a non-spatial layer was
            #      matched against a spatial target (wrong layer mapping).
            #
            # Soft warnings (user confirms, does not abort):
            #   - Zero rows: may be intentional (user cleared a layer) but
            #     unusual enough to require explicit confirmation.
            #   - Geometry type mismatch: common due to GM export promotions
            #     (Polygon → MultiPolygon), shown as informational warning only.

            cursor.execute(
                """
                SELECT column_name, udt_name
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s;
                """,
                (DB_SCHEMA, staging_name)
            )
            staging_columns = {row[0]: row[1] for row in cursor.fetchall()}

            # Hard requirement 1: staging table must exist.
            if not staging_columns:
                raise RuntimeError(
                    f"Staging table '{staging_name}' was not found in the database "
                    f"after import. The write may have failed silently."
                )

            # Hard requirement 2: geometry column must be present.
            # Missing geometry column indicates a non-spatial layer was
            # matched to a spatial target — high probability of wrong mapping.
            if "geom" not in staging_columns:
                cursor.execute(f'DROP TABLE IF EXISTS "{DB_SCHEMA}"."{staging_name}" CASCADE;')
                conn.connection.commit()
                raise RuntimeError(
                    f"Staging table '{staging_name}' has no geometry column. "
                    f"The incoming layer '{layer}' may be non-spatial or incorrectly matched. "
                    f"Staging table has been dropped. Existing table '{target_name}' is untouched."
                )

            # Step D.5: Primary key resolution.
            #
            # Walk PK_CANDIDATES in order (case-insensitive match against
            # staging_columns). For each candidate present in the table,
            # verify it has zero NULLs and zero duplicate values across
            # ALL rows (not just non-null ones) — a candidate is only
            # usable if every row has a distinct, non-null value. The
            # first candidate that qualifies is promoted to PRIMARY KEY
            # via ALTER TABLE (no data is modified, altered, or dropped —
            # the column is used exactly as the source data provided it).
            #
            # If no candidate qualifies (none present, or all present
            # candidates have NULLs/duplicates), a surrogate ud_id SERIAL
            # PRIMARY KEY column is added instead. The original candidate
            # column(s), if any, are left completely untouched — e.g. a
            # duplicate 'pin' value is never modified, deleted, or forced
            # into uniqueness; ud_id exists alongside it as the table's
            # stable row identity.
            pk_chosen = None
            for candidate in PK_CANDIDATES:
                # staging_columns keys are already lowercase (from the
                # earlier information_schema.columns query), and gdf
                # columns were lowercased in Step A, so a direct lowercase
                # comparison is sufficient here.
                if candidate not in staging_columns:
                    continue
                cursor.execute(
                    f'SELECT COUNT(*), COUNT("{candidate}"), COUNT(DISTINCT "{candidate}") '
                    f'FROM "{DB_SCHEMA}"."{staging_name}";'
                )
                total, non_null, distinct = cursor.fetchone()
                null_count = total - non_null
                dup_count = non_null - distinct
                if null_count == 0 and dup_count == 0:
                    cursor.execute(
                        f'ALTER TABLE "{DB_SCHEMA}"."{staging_name}" '
                        f'ADD PRIMARY KEY ("{candidate}");'
                    )
                    conn.connection.commit()
                    pk_chosen = candidate
                    _log(f"  PK: '{candidate}' promoted to PRIMARY KEY for {staging_name} "
                         f"(rows={total}, nulls=0, duplicates=0)")
                    break
                else:
                    _log(f"  PK: candidate '{candidate}' rejected for {staging_name} "
                         f"(rows={total}, nulls={null_count}, duplicates={dup_count})")

            if pk_chosen is None:
                cursor.execute(
                    f'ALTER TABLE "{DB_SCHEMA}"."{staging_name}" '
                    f'ADD COLUMN ud_id SERIAL PRIMARY KEY;'
                )
                conn.connection.commit()
                _log(f"  PK: no qualifying candidate found for {staging_name} — "
                     f"created surrogate 'ud_id' SERIAL PRIMARY KEY")

            # Soft warning: zero rows.
            cursor.execute(
                f'SELECT COUNT(*) FROM "{DB_SCHEMA}"."{staging_name}";'
            )
            staging_row_count = cursor.fetchone()[0]
            if staging_row_count == 0:
                proceed = messagebox.askyesno(
                    "Empty Layer Warning",
                    f"Layer '{layer}' imported zero features into staging.\n\n"
                    f"This will replace the existing table '{target_name}' with an empty table.\n\n"
                    f"Proceed?"
                )
                if not proceed:
                    print(f"  User cancelled empty-layer swap for: {target_name}")
                    cursor.execute(f'DROP TABLE IF EXISTS "{DB_SCHEMA}"."{staging_name}" CASCADE;')
                    conn.connection.commit()
                    continue  # skip this layer, leave target untouched

            # Soft warning: geometry type mismatch.
            # Informational only — does not block the swap.
            # Common cause: GM exports Polygon as MultiPolygon.
            if match_table:
                cursor.execute(
                    """
                    SELECT udt_name
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s AND column_name = 'geom';
                    """,
                    (DB_SCHEMA, target_name)
                )
                existing_geom_row = cursor.fetchone()
                if existing_geom_row:
                    existing_geom_type = existing_geom_row[0]
                    incoming_geom_type = staging_columns.get("geom", "")
                    if existing_geom_type != incoming_geom_type:
                        print(
                            f"  ⚠ Geometry type mismatch for '{target_name}': "
                            f"existing={existing_geom_type}, incoming={incoming_geom_type}. "
                            f"Proceeding — this is normal for GM exports."
                        )

            # Step E: Atomic swap inside a single PostgreSQL transaction.
            #
            # Both RENAME operations are inside one BEGIN/COMMIT block.
            # PostgreSQL guarantees: either both succeed or neither does.
            # If the connection dies mid-transaction, PostgreSQL rolls back
            # automatically — the original table survives under its original name.
            #
            # Swap only applies when a matching target already exists.
            # New tables (no match_table) go straight to rename from staging.
            print(f"  Swapping staging → target (atomic rename)...")
            try:
                cursor.execute("BEGIN;")

                if match_table:
                    # Rename existing table to backup first.
                    # Backup exists only for the duration of this transaction
                    # plus the DROP below — it is not a long-term backup.
                    cursor.execute(
                        f'ALTER TABLE "{DB_SCHEMA}"."{target_name}" '
                        f'RENAME TO "{backup_name}";'
                    )
                    # Renaming a TABLE does not rename any index attached to
                    # it — PostgreSQL treats these as independent objects.
                    # Without this, the old spatial index keeps living under
                    # a name derived from `target_name`, which then blocks
                    # the next swap's attempt to claim that same name for
                    # the newly-promoted table below. Confirmed via
                    # empirical lifecycle testing during development.
                    _rename_spatial_index(DB_SCHEMA, backup_name, f"idx_{backup_name}_geom")

                # Rename staging to target — this is the moment the new data goes live.
                cursor.execute(
                    f'ALTER TABLE "{DB_SCHEMA}"."{staging_name}" '
                    f'RENAME TO "{target_name}";'
                )
                # Same reasoning as above: rename the newly-live table's
                # spatial index to match, now that the backup step (if it
                # ran) has freed up this name.
                _rename_spatial_index(DB_SCHEMA, target_name, f"idx_{target_name}_geom")

                cursor.execute("COMMIT;")
                print(f"  ✅ Swap committed: {target_name} is now live.")
                # Only record here — after COMMIT succeeds. A failure
                # before this point raises (see except below) and aborts
                # the run before the success dialog is ever shown; a
                # user-declined empty-layer skip (Step D) never reaches
                # this line via its own `continue` above.
                updated_tables.append(target_name)

            except Exception as swap_err:
                # Transaction failed — roll back to preserve original table.
                # staging table is still present under staging_name.
                try:
                    cursor.execute("ROLLBACK;")
                except Exception:
                    pass
                # Clean up orphaned staging table before re-raising.
                try:
                    cursor.execute(
                        f'DROP TABLE IF EXISTS "{DB_SCHEMA}"."{staging_name}" CASCADE;'
                    )
                    conn.connection.commit()
                except Exception:
                    pass
                raise RuntimeError(
                    f"Atomic swap failed for layer '{layer}'. "
                    f"Original table '{target_name}' is untouched. "
                    f"Staging table has been dropped.\n\nCause: {swap_err}"
                )

            # Step F: Drop backup table now that new data is confirmed live.
            # This DROP is outside the swap transaction intentionally.
            # If the process crashes here, the worst outcome is an orphaned
            # backup table — the new live data is already committed and intact.
            if match_table:
                try:
                    cursor.execute(
                        f'DROP TABLE IF EXISTS "{DB_SCHEMA}"."{backup_name}" CASCADE;'
                    )
                    conn.connection.commit()
                    print(f"  Backup dropped: {backup_name}")
                except Exception as drop_err:
                    # Non-fatal: orphaned backup table does not affect live data.
                    # Log and continue — user can manually drop if needed.
                    print(
                        f"  ⚠ Could not drop backup table '{backup_name}': {drop_err}. "
                        f"Live data is intact. Backup may be dropped manually."
                    )

        # NOTE: conn.close()/engine.dispose() no longer called here directly —
        # the finally block below (wrapping this whole try/except) now
        # guarantees cleanup runs exactly once, on both the success and
        # failure paths, instead of only on success as before.
        _log(f"DB phase complete: all layers processed successfully. "
             f"Updated tables in schema '{DB_SCHEMA}': {updated_tables}")
        if updated_tables:
            messagebox.showinfo(
                "Success",
                f"Database updated successfully.\n\n"
                f"Schema: {DB_SCHEMA}\n"
                f"Updated tables:\n" + "\n".join(f"  • {t}" for t in updated_tables)
            )
        else:
            # All layers in the export were user-declined (empty-layer
            # warning) — the GPKG export succeeded but nothing was
            # actually swapped into the database. Distinct from the error
            # path: this is not a failure, just a no-op run.
            messagebox.showinfo(
                "No Changes",
                f"No tables were updated in schema '{DB_SCHEMA}'.\n\n"
                f"All exported layer(s) were skipped (see prompts during import)."
            )

        # Step 7: Cleanup
        if os.path.exists(save_path):
            os.remove(save_path)

    except Exception as e:
        _log(f"DB PHASE FAILED: {type(e).__name__}: {e}")
        messagebox.showerror("Database Update Failed", f"Database load failed:\n{e}")
    finally:
        # FIX: previously conn.close()/engine.dispose() only ran on the
        # success path (see above, right before the Success dialog).
        # Every failed DB phase left the connection open indefinitely —
        # confirmed via pg_stat_activity showing an "idle in transaction"
        # session from a prior failed run that never got cleaned up.
        # Wrapped in try/except since conn/engine may not even be
        # assigned yet if create_engine()/engine.connect() itself failed
        # before reaching this point — cleanup must never raise a new
        # error on top of the one already being reported above.
        try:
            conn.close()
        except Exception:
            pass
        try:
            engine.dispose()
        except Exception:
            pass


@_gm_export_guard
def update_map_and_select_recorded():
    import os, re, time
    import traceback
    import pygetwindow as gw
    import pyautogui
    from tkinter import messagebox
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import URL
    from rapidfuzz import process, fuzz
    import fiona
    import geopandas as gpd

    pyautogui.FAILSAFE = False
    _log_session_start("update_map_and_select_recorded")

    def _wait_and_activate(title, timeout=2.0, poll=0.1):
        """
        Poll for a top-level window whose title matches `title` and
        activate it as soon as it appears, instead of assuming a fixed
        sleep is long enough on every machine.

        Local to update_map_and_select_recorded() — mirrors the helper
        of the same name already used inside
        update_database_from_geopackage(), duplicated here (not
        promoted to a shared function) per this task's scope: only
        this function may be modified. Bounded by `timeout` so a
        dialog that never appears fails safe — the caller logs a
        WARNING and proceeds with the keystroke anyway, matching the
        "never hang silently, always leave a log trail" philosophy
        used throughout this module's GM-automation code.
        """
        elapsed = 0.0
        while elapsed < timeout:
            wins = gw.getWindowsWithTitle(title)
            if wins:
                wins[0].activate()
                return True
            time.sleep(poll)
            elapsed += poll
        return False

    if not all([stored_username, stored_password]):
        _log("ABORT: not logged in")
        messagebox.showerror("Error", "You must log in first before updating the map.")
        return

    # ── Manual pre-flight confirmation ──────────────────────────────────
    # Update Map has one required precondition that this automation
    # cannot verify programmatically (no scripting API is used here —
    # everything is simulated mouse/keyboard input against GM's own UI,
    # same as update_database_from_geopackage()): at least one layer
    # must already be highlighted in Global Mapper's Control Center.
    # (A second precondition — GM already connected to the target
    # PostGIS database via File -> Open Spatial Database — applied
    # under the old architecture, which routed the import through GM's
    # own database dialogs. That is no longer required: this workflow
    # now pulls data directly via Python's own SQLAlchemy connection,
    # per the Phase 1 v3 redesign, so GM's connection state is
    # irrelevant here.)
    # This dialog does not check the highlight condition — it is a
    # reminder only, mirroring the same "manual checkpoint, not real
    # validation" pattern already established for the pre-flight
    # confirmation in update_database_from_geopackage(), applied here
    # for consistency between the two GM-automation entry points. A
    # user who clicks "Yes" without a layer actually highlighted will
    # still hit whatever GM does in that state further down.
    proceed = messagebox.askyesno(
        "Confirm Before Updating Map",
        "Before continuing, please make sure that:\n\n"
        "\u2022 The layer you want to update is the only one highlighted in Global Mapper's Control Center.\n"
        "\u2022 The highlighted layer is a single file/child layer, NOT "
        "the parent/group entry (one with a + expand icon in the "
        "Control Center) \u2014 selecting the parent node is not "
        "supported; select the child layer(s) inside it instead.\n\n"
        "Do not switch windows (Alt+Tab) or interact with your computer "
        "while Update Map is running \u2014 this may interrupt the "
        "automated process.\n\n"
        "Proceed with Update Map?"
    )
    if not proceed:
        _log("ABORT: user cancelled at pre-automation confirmation dialog")
        return

    try:
        # ---- Focus Global Mapper ----
        gm_window = None
        for w in gw.getWindowsWithTitle("Global Mapper Pro"):
            if "global mapper" in w.title.lower():
                gm_window = w
                break
        if not gm_window:
            _log("ABORT: Global Mapper window not found")
            _dump_windows("GM window not found")
            messagebox.showerror("Error", "Global Mapper window not found.")
            return
        _log(f"GM window found: '{gm_window.title}' "
             f"rect=({gm_window.left}, {gm_window.top}, "
             f"{gm_window.width}x{gm_window.height})")
        gm_window.minimize(); time.sleep(0.1)
        gm_window.restore();  time.sleep(0.1)
        gm_window.activate(); time.sleep(0.1)
        _log(f"GM focused | fg='{_fg_title()}'")

        # DEFENSIVE: re-verify TEMP_DIR exists right before using it,
        # not just once at app startup - see the matching note in
        # update_database_from_geopackage() for the full rationale
        # (module-level os.makedirs() only runs once; this folder could
        # be deleted mid-session or simply not reliably present on a
        # given machine at this exact moment).
        try:
            os.makedirs(TEMP_DIR, exist_ok=True)
        except Exception as e:
            _log(f"ABORT: could not create/verify {TEMP_DIR}: {e}")
            messagebox.showerror(
                "Folder Error",
                f"Could not create or access the required temp folder:\n"
                f"{TEMP_DIR}\n\n{e}"
            )
            return

        # ---- Export to GPKG (C:\updatemap.gpkg) ----
        save_path = os.path.join(TEMP_DIR, "updatemap.gpkg")
        if os.path.exists(save_path):
            try:
                os.remove(save_path)
                _log(f"pre-export cleanup: removed stale {save_path}")
            except Exception as e:
                _log(f"ABORT: could not delete stale export file: {e}")
                messagebox.showerror("File Error", f"Could not delete existing file:\n{e}")
                return
        else:
            _log("pre-export cleanup: no stale export file present")

        # ── Export navigation — mirrored from update_database_from_geopackage() ──
        #
        # Everything from here through the Save As typing below is a
        # direct mirror of the confirmed, screenshot-verified export
        # sequence in update_database_from_geopackage(). It replaces
        # Update Map's own previous navigation (a fixed "up/right/down
        # /enter" + "a"/"gggggg" type-ahead), which was the same
        # unverified/disowned sequence Update Database itself moved
        # away from. Justification for mirroring now (rather than
        # waiting on fresh screenshots, per this project's normal
        # evidence-first discipline): the right-click export menu and
        # the GDAL/OGR-driven GeoPackage export dialogs (Select
        # Layers, Select Export Format, Tip, GeoPackage Export
        # Options) are the same GM/GDAL machinery regardless of which
        # button triggered the export — and a live test of the
        # previous Update Map sequence reproduced the exact blank
        # "File name" symptom already seen and fixed in Update
        # Database, which is direct evidence (not analogy) that the
        # same missing dialog-handling applies here.
        _dump_windows("before export right-click")
        pyautogui.hotkey("ctrl", "s")  # Save project first
        time.sleep(0.3)

        real_mouse_pos = pyautogui.position()
        gm_window.activate()
        time.sleep(0.05)
        left_panel_x = gm_window.left + 25
        left_panel_y = gm_window.top + 500
        pyautogui.moveTo(left_panel_x, left_panel_y)
        pyautogui.rightClick()
        pyautogui.moveTo(real_mouse_pos)

        _log(f"export context menu invoked | fg='{_fg_title()}'")
        time.sleep(0.05)

        # "Layer" is confirmed (via update_database_from_geopackage's
        # own screenshot verification) to always be the LAST item in
        # this context menu regardless of highlighted-layer count. A
        # single "up" on a freshly-opened menu wraps to the last item
        # via standard Windows menu wraparound, landing on "Layer"
        # without depending on how many items precede it.
        pyautogui.press("up")     # wraps to last item = "Layer"
        pyautogui.press("enter")  # open "Layer" submenu
        time.sleep(0.1)
        pyautogui.press("down", presses=2, interval=0.05)  # "EXPORT - Export Layer(s) to New File..."
        pyautogui.press("enter")
        _log(f"EXPORT menu item selected | fg='{_fg_title()}'")

        # ============================================================
        # COMBINED POLL across "Select Layers" / "Tip" / "Select Export
        # Format" (new) - a live test confirmed GM can skip straight to
        # "Select Export Format" after the EXPORT menu item is
        # selected, bypassing "Select Layers" entirely for reasons not
        # yet understood. Detect whichever of the three actually
        # appears and skip the confirming keystroke(s) for any earlier
        # step(s) GM already bypassed, instead of treating a missing
        # "Select Layers" as an automatic failure. Same two-round,
        # 5.0s-each pattern as the Tip/GeoPackage Export Options poll
        # below, for the same reason (distinguish "took a bit longer"
        # from "genuinely never appeared").
        # ============================================================
        _step1_result = None
        _current_fg = _fg_title()
        for _poll_round in (1, 2):
            _round_start = time.monotonic()
            while time.monotonic() - _round_start < 5.0:
                _current_fg = _fg_title()
                if "select layers" in _current_fg.lower():
                    _step1_result = "select_layers"
                    break
                if _current_fg.strip() == "Tip":
                    _step1_result = "tip"
                    break
                if "select export format" in _current_fg.lower():
                    _step1_result = "select_export_format"
                    break
                time.sleep(0.2)
            if _step1_result is not None:
                _log(f"combined poll (Select Layers/Tip/Select Export "
                     f"Format) round {_poll_round}: found "
                     f"'{_step1_result}' after "
                     f"{time.monotonic() - _round_start:.1f}s | fg='{_current_fg}'")
                break
            _log(f"combined poll (Select Layers/Tip/Select Export "
                 f"Format) round {_poll_round}: none seen within 5.0s "
                 f"| fg='{_current_fg}'")

        # SAFETY ABORT: if none of the three appeared after both
        # rounds, this is a genuine navigation failure (e.g. a
        # nested/grouped layer entry changed what options exist in
        # GM's right-click menu) - abort with a clear, actionable
        # error instead of continuing to send keystrokes blind.
        if _step1_result is None:
            _dump_windows("export navigation lost focus after EXPORT menu item (update_map)")
            raise RuntimeError(
                "Update Map was aborted before any further keystrokes "
                "were sent to avoid typing into the wrong window.\n\n"
                "Please select the layer you want to update and make "
                "sure that what you select is a layer without an "
                "expand/collapse icon ('+' or '-') to its left."
            )

        # "Select Layers" dialog -> OK. Confirmed that "OK" is already
        # default-focused the instant this dialog opens, and its
        # default checkbox state already reflects exactly which
        # layer(s) were highlighted beforehand — no "Check All", no
        # tabbing, a single Enter submits it as-is. Skipped entirely if
        # GM already bypassed this dialog (detected via the combined
        # poll above).
        if _step1_result == "select_layers":
            pyautogui.press("enter")
            _log(f"Select Layers dialog confirmed (OK, no Check All, no tab) | fg='{_fg_title()}'")
        elif _step1_result == "select_export_format":
            _log("'Select Layers' was not shown - 'Select Export "
                 f"Format' already focused | fg='{_fg_title()}'")
        else:
            _log("'Select Layers' and 'Select Export Format' were both "
                 f"not shown - 'Tip' already focused | fg='{_fg_title()}'")

        # "Select Export Format" dialog -> Geopackage. PageUp x20
        # resets the dropdown to its first entry ("2DM File" — no Home
        # key available on this keyboard), then "g" x6 type-ahead
        # advances the selection to "Geopackage". The type-ahead
        # commits the combobox value directly, so a single Enter here
        # activates OK with no separate confirm step needed. Skipped
        # entirely if GM already bypassed this dialog too (jumped
        # straight to "Tip").
        if _step1_result in ("select_layers", "select_export_format"):
            pyautogui.press("pageup", presses=20, interval=0.03)  # reset dropdown to top ("2DM File")
            time.sleep(0.3)
            for i in range(6):
                pyautogui.press("g")
                time.sleep(0.15)
            _log(f"Select Export Format: Geopackage type-ahead complete | fg='{_fg_title()}'")
            pyautogui.press("enter")  # OK on "Select Export Format" dialog
            _log(f"Select Export Format: OK pressed | fg='{_fg_title()}'")

        # "Tip" (optional) then "GeoPackage Export Options" (mandatory).
        #
        # "Tip" is LEGITIMATELY OPTIONAL: if the user previously checked
        # "Don't Show This Again", GM skips straight to "GeoPackage
        # Export Options" and "Tip" never appears at all - a normal,
        # expected case, not a failure. If the combined poll above
        # already landed on "Tip" directly, skip this second poll
        # entirely and go straight to confirming it.
        if _step1_result == "tip":
            _tip_or_geo_result = "tip"
        else:
            _tip_or_geo_result = None
            _last_fg_seen = _fg_title()
            for _poll_round in (1, 2):
                _round_start = time.monotonic()
                while time.monotonic() - _round_start < 5.0:
                    _last_fg_seen = _fg_title()
                    if _last_fg_seen.strip() == "Tip":
                        _tip_or_geo_result = "tip"
                        break
                    if "geopackage export options" in _last_fg_seen.lower():
                        _tip_or_geo_result = "export_options"
                        break
                    time.sleep(0.2)
                if _tip_or_geo_result is not None:
                    _log(f"combined poll round {_poll_round}: found "
                         f"'{_tip_or_geo_result}' after "
                         f"{time.monotonic() - _round_start:.1f}s | fg='{_last_fg_seen}'")
                    break
                _log(f"combined poll round {_poll_round}: neither 'Tip' nor "
                     f"'GeoPackage Export Options' seen within 5.0s | "
                     f"fg='{_last_fg_seen}'")

            if _tip_or_geo_result is None:
                _dump_windows("neither Tip nor GeoPackage Export Options appeared (update_map)")
                raise RuntimeError(
                    "Export navigation failed: neither the 'Tip' dialog "
                    "nor the 'GeoPackage Export Options' dialog appeared "
                    f"within the expected time (focused window was "
                    f"'{_last_fg_seen}' instead). Update Map was aborted "
                    "before any further keystrokes were sent, to avoid "
                    "typing into the wrong window."
                )

        if _tip_or_geo_result == "tip":
            _log(f"Tip dialog focused | fg='{_fg_title()}'")
            pyautogui.press("enter")
            _log(f"Tip dialog OK | fg='{_fg_title()}'")
            # "Tip" confirmed - now specifically wait for "GeoPackage
            # Export Options", which is NOT skippable (always appears).
            if not _wait_and_activate("GeoPackage Export Options", timeout=5.0):
                _dump_windows("GeoPackage Export Options dialog not found after Tip (update_map)")
                raise RuntimeError(
                    "Export navigation failed: the 'GeoPackage Export "
                    "Options' dialog did not appear after confirming "
                    "the 'Tip' dialog. Update Map was aborted before "
                    "any further keystrokes were sent, to avoid typing "
                    "into the wrong window."
                )
            _log(f"GeoPackage Export Options focused | fg='{_fg_title()}'")

        else:
            _log("'Tip' was not shown (likely 'Don't Show This Again' "
                 f"was previously checked) - 'GeoPackage Export "
                 f"Options' already focused | fg='{_fg_title()}'")
        # Confirm "GeoPackage Export Options" - reached only via the
        # two valid paths above (Tip confirmed then GeoPackage Export
        # Options found, or GeoPackage Export Options found directly).
        # Default focus is already on "OK" and the default export
        # settings are exactly what's wanted - just confirm.
        pyautogui.press("enter")
        _log(f"GeoPackage Export Options OK | fg='{_fg_title()}'")

        # "Save As" dialog appears here.
        # SAFETY: abort rather than typing the export path blind into
        # whatever has focus if this dialog is not actually found - a
        # live test confirmed exactly this failure typed the path into
        # a Brave browser tab instead of Global Mapper's Save As dialog.
        # Timeout standardized to 5.0s (was the 2.0s default) across
        # every abort-capable check in this function, so a slower user
        # machine has consistent, adequate room before any of them
        # false-aborts.
        print("Waiting for Save As dialog...")
        if _wait_and_activate("Save As", timeout=5.0):
            _log(f"Save As dialog focused | fg='{_fg_title()}'")
        else:
            _dump_windows("Save As not found (update_map)")
            raise RuntimeError(
                "Export navigation failed: the 'Save As' dialog did "
                "not appear within the expected time. Update Map was "
                "aborted before typing the export path, to avoid "
                "typing it into the wrong window."
            )

        pyautogui.hotkey("alt", "n")
        pyautogui.typewrite(save_path)
        pyautogui.press("enter")
        _log(f"save path typed | fg='{_fg_title()}'")

        # ---- Wait until file is fully saved (bounded poll) ----
        # Raises RuntimeError on timeout, caught by the "Update Map
        # Failed" handler at the end of this function. Shared,
        # already-fixed helper — unchanged.
        print("\u23f3 Waiting for updatemap.gpkg to be fully written...")
        _wait_for_gpkg_export(save_path, root)

        _log(f"export phase complete: {save_path}")
        print("\u2705 File saved:", save_path)

        # ---- Re-type the GPKG path into GM (if dialog still open) ----
        # Kept as a defensive fallback in case some other, still-
        # unidentified GM state leaves "Save As" open even after the
        # dialog-complete sequence above. Expected to be a no-op in
        # normal operation now that Tip / GeoPackage Export Options are
        # explicitly handled — the earlier blank-filename symptom this
        # was originally band-aiding should no longer occur upstream.
        if _wait_and_activate("Save As", timeout=3.0):
            _log(f"Save As dialog still open post-export \u2014 re-typing path | fg='{_fg_title()}'")
            pyautogui.typewrite(save_path)
            pyautogui.press("enter")
            print("\U0001f4c2 Re-typed path into Save As dialog")
        else:
            print("\u26a0\ufe0f Save As dialog not found \u2014 skipping re-type")
            _log("Save As dialog not open post-export — no re-type needed")

        # UX: status window covering the matching / database-read / local
        # write phase — previously this whole stretch was visually silent
        # (progress bar finishes, then nothing appears to happen until
        # Ctrl+O). Same non-focus-stealing pattern as the Ctrl+O wait
        # indicator (no focus_force()/grab_set(), overrideredirect).
        _status_win = None
        try:
            _status_win = tk.Toplevel(root)
            _status_win.overrideredirect(True)
            _status_win.attributes("-topmost", True)
            _status_win.configure(bg="#2b2b2b")
            _status_label = tk.Label(
                _status_win,
                text="Matching layers and reading data from database...",
                bg="#2b2b2b", fg="white", font=("Segoe UI", 9),
                padx=12, pady=8
            )
            _status_label.pack()
            _status_win.geometry(f"+{gm_window.left + 20}+{gm_window.top + 20}")
            _status_win.update_idletasks()
            root.update()
        except Exception as status_win_err:
            _log(f"status window could not be created (non-fatal): "
                 f"{type(status_win_err).__name__}: {status_win_err}")
            _status_win = None

        def _close_status_window():
            """Safely destroy the status window on any exit path (success
            continuing to the next phase, or an early return on failure).
            Never raises - a failure here must not mask the real error
            already being reported by the caller."""
            nonlocal _status_win
            if _status_win is not None:
                try:
                    _status_win.destroy()
                except Exception:
                    pass
                _status_win = None

        # ---- Match layers with PostgreSQL tables ----
        #
        # Phase 2 implementation of the Phase 1 v3 (frozen) analysis
        # document. Replaces the earlier diagnostic probe, which stood
        # in the already-exported `save_path` as a placeholder for a
        # freshly DB-pulled file. This block now does the real pull.
        #
        # Structure: several narrower, phase-specific try/except blocks
        # (matching -> DB read -> local write -> GM load -> delete-old)
        # instead of one broad try/except, per Section 5's design
        # implication. Each phase reports its own specific, accurate,
        # English-only error message and aborts (via `return`) rather
        # than falling through to a generic catch-all. `engine.dispose()`
        # in the existing `finally` below still runs on every exit path,
        # including these early returns.
        connection_url = URL.create(
            drivername="postgresql+psycopg2",
            username=stored_username,
            password=stored_password,
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME
        )
        engine = create_engine(connection_url)
        try:
            # ============================================================
            # PHASE: matching (all-or-nothing validation gate, Section 4)
            # ============================================================
            #
            # `matched_pairs` replaces the earlier flat `matched_tables`
            # list (Section 3, locked): each entry keeps the original
            # exported layer name alongside the table it matched to,
            # instead of discarding that information. Any layer that
            # fails to match aborts the ENTIRE operation before any data
            # is read, written, or touched in Global Mapper — no partial
            # updates (Section 4).
            try:
                with engine.begin() as conn:
                    db_tables = [r[0] for r in conn.execute(
                        text("SELECT table_name FROM information_schema.tables WHERE table_schema=:s AND table_type='BASE TABLE'"),
                        {"s": DB_SCHEMA}
                    )]

                db_lower = {t.lower(): t for t in db_tables}
                schema_prefix = DB_SCHEMA + "_"
                matched_pairs = []      # [(original_layer_name, matched_table_name), ...]
                unmatched_layers = []   # layers with no usable match at all

                for layer in fiona.listlayers(save_path):
                    lyr_name = layer.strip()
                    if lyr_name.lower().startswith(schema_prefix.lower()):
                        stripped = lyr_name[len(schema_prefix):]
                    else:
                        stripped = lyr_name
                    stripped = re.sub(r"\s+", "_", stripped)

                    if stripped.lower() in db_lower:
                        matched_pairs.append((layer, db_lower[stripped.lower()]))
                    else:
                        match, score, _ = process.extractOne(
                            stripped.lower(),
                            [t.lower() for t in db_tables],
                            scorer=fuzz.WRatio
                        )
                        if match is not None:
                            matched_pairs.append((layer, db_lower[match]))
                        else:
                            unmatched_layers.append(layer)

                _log(f"matched pairs ({len(matched_pairs)}): {matched_pairs}")
                if unmatched_layers:
                    _log(f"unmatched layers ({len(unmatched_layers)}): {unmatched_layers}")
                    raise RuntimeError(
                        "The following layer(s) could not be matched to any "
                        f"table in schema '{DB_SCHEMA}': "
                        f"{', '.join(unmatched_layers)}. Update Map was "
                        "aborted before any data was touched."
                    )
            except Exception as match_err:
                _log(f"MATCHING PHASE FAILED: {type(match_err).__name__}: {match_err}")
                _log(f"MATCHING PHASE traceback:\n{traceback.format_exc()}")
                _close_status_window()
                messagebox.showerror("Update Map Failed - Matching", str(match_err))
                return

            # ============================================================
            # PHASE: database read (Section 5 - technical + business
            # validation failures both handled here)
            # ============================================================
            def _get_geometry_column(table_name):
                """
                Dynamically detect the actual geometry column name for a
                table by querying PostGIS's own geometry_columns catalog
                view - the same approach already used throughout this
                project's other tool modules (see get_geometry_column()
                in road_width.py, land_shape_compactness.py, lot_location.py,
                etc.), confirmed available in this environment. Avoids
                assuming the geometry column is literally named "geom":
                tables that were never written by CAMA Tools' own Safe
                Replace Workflow (e.g. created via QGIS, shp2pgsql, or a
                manual import) commonly use "geometry", "the_geom", or
                other conventions instead - Update Database's own write
                path always normalizes to "geom", but a table that has
                never been through Update Database yet may not be.

                Local to update_map_and_select_recorded() only, matching
                this task's scope (no existing equivalent found in this
                file to reuse). Returns the column name as a string, or
                None if not found (e.g. the table has no geometry column
                registered in the catalog, or the query itself failed) -
                the caller decides how to handle a None result.
                """
                try:
                    with engine.connect() as conn:
                        result = conn.execute(
                            text(
                                "SELECT f_geometry_column FROM geometry_columns "
                                "WHERE f_table_schema = :schema AND f_table_name = :table"
                            ),
                            {"schema": DB_SCHEMA, "table": table_name}
                        )
                        row = result.fetchone()
                        return row[0] if row else None
                except Exception as geom_col_err:
                    _log(f"  geometry column detection failed for "
                         f"'{table_name}': {type(geom_col_err).__name__}: {geom_col_err}")
                    return None

            try:
                read_results = []  # [(matched_table_name, GeoDataFrame), ...]
                for layer_name, table_name in matched_pairs:
                    _geom_col = _get_geometry_column(table_name)
                    if not _geom_col:
                        raise RuntimeError(
                            f"Could not determine the geometry column for "
                            f"table '{DB_SCHEMA}.{table_name}' (no entry "
                            "found in PostGIS's geometry_columns catalog). "
                            "The table may not have a registered geometry "
                            "column. Update Map was aborted before any "
                            "changes were made to Global Mapper."
                        )
                    _log(f"  detected geometry column '{_geom_col}' for table '{table_name}'")
                    sql = f'SELECT * FROM "{DB_SCHEMA}"."{table_name}"'
                    gdf = gpd.read_postgis(sql, con=engine, geom_col=_geom_col)

                    # Business validation failure (Section 5, locked):
                    # a table that reads back with zero features is not
                    # a technical error - read_postgis() succeeded - but
                    # every matched table is required to contain data.
                    if gdf.empty:
                        raise RuntimeError(
                            f"The table '{DB_SCHEMA}.{table_name}' was read "
                            "successfully but contains no features. Update "
                            "Map requires every matched table to contain "
                            "data, so the operation was aborted before any "
                            "changes were made to Global Mapper."
                        )
                    read_results.append((table_name, gdf))
                    _log(f"read '{DB_SCHEMA}.{table_name}': {len(gdf)} feature(s)")
            except Exception as read_err:
                _log(f"DATABASE READ PHASE FAILED: {type(read_err).__name__}: {read_err}")
                _log(f"DATABASE READ PHASE traceback:\n{traceback.format_exc()}")
                _close_status_window()
                messagebox.showerror("Update Map Failed - Database Read", str(read_err))
                return

            # ============================================================
            # PHASE: write local GeoPackage (Section 6 - filename scheme,
            # mode="w"/"a" write order)
            # ============================================================
            try:
                if len(read_results) == 1:
                    base_stem = f"updatemap_{read_results[0][0]}"
                else:
                    base_stem = f"updatemap_{len(read_results)}layers"

                candidate = f"{base_stem}.gpkg"
                copy_n = 1
                while os.path.exists(os.path.join(TEMP_DIR, candidate)):
                    candidate = f"{base_stem}_copy{copy_n}.gpkg"
                    copy_n += 1
                new_gpkg_path = os.path.join(TEMP_DIR, candidate)

                # REQUIRED DEPENDENCY CHECK: this workflow's investigation
                # (see Phase 1 v3 analysis addenda) reproducibly confirmed
                # that Fiona 1.10.1 + GDAL 3.9.1's write path fails with a
                # "NULL pointer error" when reopening the same GeoPackage
                # for append (mode="a") - confirmed independent of this
                # application (bare Fiona minimal reproduction, both
                # inside the frozen exe and in a plain Python venv).
                # pyogrio's write path was confirmed to succeed under the
                # same conditions (both via GeoPandas' engine="pyogrio"
                # and via pyogrio's own API directly). Production writes
                # therefore require pyogrio explicitly - this check
                # happens here, immediately before the write phase, not
                # at module import time, so the rest of the application
                # remains usable even if pyogrio is missing; only Update
                # Map fails, with a precise cause. There is deliberately
                # no silent fallback to the Fiona engine: a future
                # environment missing pyogrio must fail loudly here
                # rather than quietly reverting to the confirmed-broken
                # append path.
                try:
                    import pyogrio
                except ImportError as imp_err:
                    raise ImportError(
                        "Update Map requires the 'pyogrio' package for "
                        "GeoPackage writing, but it is unavailable in "
                        "this environment."
                    ) from imp_err
                _log(f"GeoPackage writer: engine=pyogrio {pyogrio.__version__}")

                for i, (table_name, gdf) in enumerate(read_results):
                    # Only the FIRST layer write may use mode="w" - every
                    # subsequent layer in the same file must use mode="a"
                    # (append). Using "w" for every layer would silently
                    # overwrite each previous one, leaving only the last
                    # layer in the file (Section 6 implementation note).
                    write_mode = "w" if i == 0 else "a"
                    # Production write engine: pyogrio (see dependency
                    # check above). This is no longer a controlled
                    # experiment - Experiments A-D and E1/E2 (elsewhere in
                    # this diagnostic block and in the standalone
                    # diagnose_append*.py scripts) confirmed Fiona's
                    # append path reproducibly fails in this environment
                    # while pyogrio's succeeds, under both the GeoPandas
                    # wrapper and pyogrio's own direct API. Every other
                    # stage of this pipeline (matching, DB read, GM load,
                    # delete-old, cleanup) is unchanged - only the
                    # GeoPackage write backend changed.
                    gdf.to_file(new_gpkg_path, layer=table_name, driver="GPKG",
                                mode=write_mode, engine="pyogrio")
                    _log(f"wrote layer '{table_name}' "
                         f"(mode={write_mode}, engine=pyogrio)")

                # Defensive sanity check (Section 5, revised classification):
                # given the matching gate and the per-table empty check
                # above, this should never actually fire in normal
                # operation - if it does, it indicates a programming bug
                # or a GeoPackage-writer malfunction, not a real data
                # state. Kept as a guard, not as expected business logic.
                # Deliberately still verified via fiona.listlayers() (not
                # pyogrio) - Fiona's read path was never implicated in
                # the investigation and worked correctly throughout every
                # experiment, so there is no reason to change it here.
                written_layers = fiona.listlayers(new_gpkg_path)
                if not written_layers:
                    raise RuntimeError(
                        "The exported GeoPackage contains no layers. "
                        "Update Map was aborted before loading into "
                        "Global Mapper."
                    )
                _log(f"local GeoPackage written: {new_gpkg_path} "
                     f"(layers: {written_layers})")
            except ImportError as dep_err:
                _log(f"GEOPACKAGE WRITE PHASE FAILED: missing dependency - "
                     f"{type(dep_err).__name__}: {dep_err}")
                _log(f"GEOPACKAGE WRITE PHASE traceback:\n{traceback.format_exc()}")
                _close_status_window()
                messagebox.showerror(
                    "Update Map Failed",
                    "Update Map cannot continue because the required "
                    "GeoPackage writer is unavailable in this "
                    "installation. Please contact support."
                )
                return
            except Exception as write_err:
                _log(f"GEOPACKAGE WRITE PHASE FAILED: {type(write_err).__name__}: {write_err}")
                _log(f"GEOPACKAGE WRITE PHASE traceback:\n{traceback.format_exc()}")
                _close_status_window()
                messagebox.showerror("Update Map Failed - GeoPackage Write", str(write_err))
                return

            # Matching/DB-read/write phase complete - close this status
            # window before starting the Ctrl+O phase, which shows its
            # own status window with its own message.
            _close_status_window()

            # ============================================================
            # PHASE: Global Mapper Ctrl+O load
            # ============================================================
            #
            # Section 2.3 (completion-signal detection) — RESOLVED this
            # pass. A live test revealed GM's own foreground window title
            # changes to "Loading GeoPackage {name} (X%)" while the file
            # is being read, then reverts back to GM's normal title once
            # done. This is a real completion signal, replacing the
            # earlier flat 15s guess. Two bounded phases below:
            #   1. Wait (briefly) for "Loading GeoPackage" to appear at
            #      all - for small/fast files this may never be caught
            #      (observed: a single-layer load completed inside one
            #      1s log tick), which is not a failure.
            #   2. If seen, wait for it to disappear - bounded by a
            #      safety ceiling in case a large load genuinely takes
            #      longer than observed so far.
            try:
                gm_window.minimize(); time.sleep(0.1)
                gm_window.restore();  time.sleep(0.1)
                gm_window.activate(); time.sleep(0.1)
                _log(f"GM refocused before Ctrl+O | fg='{_fg_title()}'")
                _dump_windows("before Ctrl+O")

                pyautogui.hotkey("ctrl", "o")
                _log("Ctrl+O sent")
                _dump_windows("immediately after Ctrl+O")

                # Exact title of GM's "Open Data File(s)" dialog is not
                # yet confirmed (same open question as the earlier probe
                # run) - give it a moment to render, log every visible
                # window title so the actual title can be read from
                # cama_automation.log and wired into a precise
                # _wait_and_activate() call in a future pass.
                time.sleep(0.5)
                _dump_windows("0.5s after Ctrl+O")

                pyautogui.typewrite(new_gpkg_path)
                pyautogui.press("enter")
                _log(f"typed '{new_gpkg_path}' and pressed Enter | fg='{_fg_title()}'")

                # UX: a small, non-focus-stealing status window so this
                # wait doesn't look like Update Map has frozen. Uses
                # overrideredirect (no title bar/taskbar entry) and never
                # calls focus_force()/grab_set(), so it cannot steal
                # keyboard focus away from Global Mapper during this
                # wait. Also pumps the Tk event loop each tick (root.
                # update()) so Windows doesn't mark CAMA Tools as "(Not
                # Responding)" during the wait, the same class of issue
                # _wait_for_gpkg_export() already guards against
                # elsewhere.
                _status_win = None
                try:
                    _status_win = tk.Toplevel(root)
                    _status_win.overrideredirect(True)
                    _status_win.attributes("-topmost", True)
                    _status_win.configure(bg="#2b2b2b")
                    _status_label = tk.Label(
                        _status_win,
                        text="Verifying that the import completed successfully...",
                        bg="#2b2b2b", fg="white", font=("Segoe UI", 9),
                        padx=12, pady=8
                    )
                    _status_label.pack()
                    _status_win.geometry(f"+{gm_window.left + 20}+{gm_window.top + 20}")
                    _status_win.update_idletasks()
                except Exception as status_win_err:
                    _log(f"status window could not be created (non-fatal): "
                         f"{type(status_win_err).__name__}: {status_win_err}")
                    _status_win = None

                _LOAD_APPEARANCE_TIMEOUT_S = 5.0   # bounded wait for "Loading GeoPackage" to appear
                _LOAD_COMPLETION_TIMEOUT_S = 15.0  # bounded ceiling for it to disappear once seen

                def _update_load_status(_text):
                    try:
                        if _status_win is not None:
                            _status_label.config(text=_text)
                        root.update()
                    except tk.TclError:
                        pass  # root/status window destroyed mid-wait (e.g. GM closed) - non-fatal

                # Phase 1: wait briefly for "Loading GeoPackage" to appear
                _phase1_start = time.monotonic()
                _loading_seen = False
                while time.monotonic() - _phase1_start < _LOAD_APPEARANCE_TIMEOUT_S:
                    _fg = _fg_title()
                    _log(f"load-wait phase 1: t={time.monotonic() - _phase1_start:.1f}s | fg='{_fg}'")
                    if "loading geopackage" in _fg.lower():
                        _loading_seen = True
                        _log(f"'Loading GeoPackage' window appeared | fg='{_fg}'")
                        break
                    _update_load_status("Verifying that the import completed successfully...")
                    time.sleep(0.2)

                # Phase 2: only if seen - wait (bounded) for it to disappear
                if _loading_seen:
                    _phase2_start = time.monotonic()
                    _loading_done = False
                    while time.monotonic() - _phase2_start < _LOAD_COMPLETION_TIMEOUT_S:
                        _fg = _fg_title()
                        _elapsed = time.monotonic() - _phase2_start
                        _log(f"load-wait phase 2: t={_elapsed:.1f}s | fg='{_fg}'")
                        if "loading geopackage" not in _fg.lower():
                            _loading_done = True
                            _log(f"'Loading GeoPackage' window closed - load complete | fg='{_fg}'")
                            break
                        _update_load_status(
                            f"Verifying that the import completed successfully... "
                            f"({_elapsed:.0f}s / {_LOAD_COMPLETION_TIMEOUT_S:.0f}s)"
                        )
                        time.sleep(0.2)
                    if not _loading_done:
                        _log(f"WARNING: 'Loading GeoPackage' window still present after "
                             f"{_LOAD_COMPLETION_TIMEOUT_S:.0f}s - proceeding anyway")
                else:
                    _log(f"'Loading GeoPackage' window not observed within "
                         f"{_LOAD_APPEARANCE_TIMEOUT_S:.0f}s - likely too fast to catch "
                         "(small file); proceeding")

                try:
                    if _status_win is not None:
                        _status_win.destroy()
                except Exception:
                    pass

                _dump_windows("after Ctrl+O load wait")
                _log("Ctrl+O load wait complete")
            except Exception as load_err:
                _log(f"GLOBAL MAPPER LOAD PHASE FAILED: {type(load_err).__name__}: {load_err}")
                _log(f"GLOBAL MAPPER LOAD PHASE traceback:\n{traceback.format_exc()}")
                messagebox.showerror("Update Map Failed - Global Mapper Load", str(load_err))
                return

            # ============================================================
            # PHASE: close the old (superseded) layer
            # ============================================================
            #
            # Relies on Section 2.5 (confirmed under live automation):
            # the previously-highlighted layer stays highlighted through
            # the Ctrl+O load, so "Close Selected Overlays?" targets the
            # OLD layer with no re-select step needed. "Yes" is confirmed
            # (via live screenshot) to be the default-focused button, so
            # a single Enter accepts it automatically - no manual click
            # required from the user for this step.
            close_dialog_appeared = False
            try:
                _dump_windows("before post-load right-click")
                gm_window.minimize(); time.sleep(0.1)
                gm_window.restore();  time.sleep(0.1)
                gm_window.activate(); time.sleep(0.1)
                _log(f"post-load: GM refocused | fg='{_fg_title()}'")

                real_mouse_pos = pyautogui.position()
                left_panel_x = gm_window.left + 25
                left_panel_y = gm_window.top + 500
                pyautogui.moveTo(left_panel_x, left_panel_y)
                pyautogui.rightClick()
                pyautogui.moveTo(real_mouse_pos)
                _log(f"post-load: right-click menu invoked | fg='{_fg_title()}'")

                pyautogui.press("down", presses=3, interval=0.001)
                pyautogui.press("enter")
                _log(f"post-load: down x3 + enter sent | fg='{_fg_title()}'")

                close_dialog_appeared = _wait_and_activate("Close Selected Overlays?", timeout=2.0)
                if close_dialog_appeared:
                    _log("post-load: 'Close Selected Overlays?' dialog appeared "
                         f"| fg='{_fg_title()}'")
                    pyautogui.press("enter")
                    _log("post-load: 'Close Selected Overlays?' confirmed "
                         f"(auto Yes) | fg='{_fg_title()}'")
                else:
                    _log("post-load: WARNING - 'Close Selected Overlays?' dialog "
                         f"did not appear within timeout | fg='{_fg_title()}'")
                _dump_windows("after post-load down x3 + enter")
            except Exception as delete_err:
                _log(f"DELETE-OLD-LAYER PHASE FAILED: {type(delete_err).__name__}: {delete_err}")
                _log(f"DELETE-OLD-LAYER PHASE traceback:\n{traceback.format_exc()}")
                messagebox.showerror("Update Map Failed - Closing Old Layer", str(delete_err))
                return

            if not close_dialog_appeared:
                # Locked (Section 5): this is its own distinct, reported
                # failure, not a silent fallthrough to a generic success
                # message. The new data was loaded, but the old layer's
                # removal could not be confirmed.
                _log("Update Map FAILED: 'Close Selected Overlays?' dialog "
                     "did not appear where expected")
                messagebox.showerror(
                    "Update Map Failed - Closing Old Layer",
                    "Global Mapper did not show the 'Close Selected "
                    "Overlays?' confirmation after loading the new data. "
                    "The new data was loaded, but the old layer may not "
                    "have been closed. Please check Global Mapper's "
                    "Control Center manually before running Update Map "
                    "again."
                )
                return

            # NOTE: no rename step here, by deliberate decision (Phase 1
            # v3 analysis, Section 7) - not an omission. Layer identity
            # in this workflow never depends on the display name: it
            # always comes from fiona.listlayers() output and database
            # table names (Section 6's guardrail). The locked filename
            # scheme already signals to the user, by name, that the
            # layer came from Update Map. Renaming the Control Center
            # entry would be purely cosmetic, adds the most fragile
            # UI-automation step in this whole workflow, and fixes no
            # actual data-correctness concern.

            # ============================================================
            # PHASE: success + cleanup
            # ============================================================
            #
            # Reaching this point means every phase above completed
            # without raising an exception (Section 3/8 wording,
            # deliberately not "everything imported successfully" - this
            # workflow verifies DB read, GeoPackage write, Ctrl+O load,
            # and the close-old-layer step, but has no scripting API to
            # directly inspect Global Mapper's internal Control Center
            # state). No separate `imported_tables` accumulator is
            # needed - `matched_pairs` already accurately reflects what
            # was processed, since any failure above would have already
            # returned before this point (Section 3, locked).
            _log(f"Update Map complete. Tables used: {matched_pairs}")
            mapping_lines = "\n".join(
                f"  {layer_name}  \u2192  {table_name}"
                for layer_name, table_name in matched_pairs
            )
            messagebox.showinfo(
                "Update Map",
                "Update Map completed successfully.\n\n"
                f"Schema: {DB_SCHEMA}\n\n"
                "Tables used for this Update Map operation:\n"
                f"{mapping_lines}\n\n"
                "The workspace has been updated."
            )

            # Cleanup: remove BOTH the original export (used only to
            # read layer names) AND the new local GeoPackage written
            # above. Non-fatal if this fails - the operation already
            # succeeded from the user's perspective.
            try:
                if os.path.exists(save_path):
                    os.remove(save_path)
                if os.path.exists(new_gpkg_path):
                    os.remove(new_gpkg_path)
                _log("temporary files cleaned up")
            except Exception as cleanup_err:
                _log(f"WARNING: cleanup failed (non-fatal): "
                     f"{type(cleanup_err).__name__}: {cleanup_err}")

        finally:
            # FIX: engine (and its connection pool) was previously never
            # disposed on any exit path other than the whole outer try
            # block completing successfully. Any exception raised after
            # `engine = create_engine(...)` above skipped cleanup
            # entirely, leaving the pool (and, if still checked out, an
            # active connection) orphaned. Mirrors the equivalent fix
            # already applied in update_database_from_geopackage().
            # Wrapped in try/except since dispose() itself must never
            # raise a new error on top of whatever is already being
            # reported by the outer except below.
            try:
                engine.dispose()
            except Exception:
                pass

        # ---- Cleanup ----
        if os.path.exists(save_path):
            os.remove(save_path)

    except Exception as e:
        _log(f"UPDATE MAP FAILED: {type(e).__name__}: {e}")
        messagebox.showerror("Update Map Failed", str(e))




import json, shutil
from tkinter import filedialog

_base_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
            else os.path.dirname(os.path.abspath(__file__))
GM_PATH_FILE = os.path.join(_base_dir, "gm_exe_path.json")

def get_global_mapper_path() -> str:
    # 1) previously saved?
    if os.path.exists(GM_PATH_FILE):
        try:
            return json.load(open(GM_PATH_FILE, "r")).get("exe", "")
        except Exception:
            pass

    # 2) common installs
    candidates = [
        r"C:\Program Files\GlobalMapper25.2_64bit\global_mapper.exe",
        r"C:\Program Files\GlobalMapper26.0_64bit\global_mapper.exe",
        r"C:\Program Files\GlobalMapper26.2_64bit\global_mapper.exe",
        r"C:\Program Files\GlobalMapper26_64bit\global_mapper.exe",
        r"C:\Program Files\GlobalMapper27_64bit\global_mapper.exe",
        r"C:\Program Files\GlobalMapper\global_mapper.exe",
    ]
    for p in candidates:
        if os.path.exists(p):
            json.dump({"exe": p}, open(GM_PATH_FILE, "w"))
            return p

    # 3) prompt user
    exe = filedialog.askopenfilename(title="Locate global_mapper.exe",
                                     filetypes=[("Executable", "global_mapper.exe")])
    if exe:
        json.dump({"exe": exe}, open(GM_PATH_FILE, "w"))
    return exe or ""

GM_EXE_PATH = ""  # Will be resolved after login

LAST_EDITED_FILE = "last_edit_source.json"

def record_edit_source(source_name):
    with open(LAST_EDITED_FILE, "w") as f:
        json.dump({"source": source_name}, f)

def track_popup_close(popup, label):
    def on_close():
        popup_windows[label] -= 1
        if popup_windows[label] <= 0:
            popup_windows[label] = 0
            canvas, bg_id = canvas_refs[label]
            canvas.clicked = False
            canvas.itemconfig(bg_id, image="")
        popup.destroy()
    popup.protocol("WM_DELETE_WINDOW", on_close)

# Launcher: re-run this same EXE with --tool "<LABEL>"
def run_tool_by_label(label: str):
    import sys, os, threading

    IS_FROZEN = getattr(sys, 'frozen', False)

    icon_map = {
        "ANY MAP TO LAND PARCEL": "influencemap.ico",
        "ROAD WIDTH": "roadwidth.ico",
        "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO": "roadfrontage.ico",
        "LOT LOCATION": "lotlocation.ico",
        "LAND SHAPE": "landshape.ico",
        "METERS FROM (SCHOOL, SHOP, TRANSPORT, CHURCH)": "distancefrom.ico",
        "LANDMARKS WITHIN METERS": "landmarks200.ico",
        "PARCEL TERRAIN LEVEL": "terrain.ico",
        "ROAD DENSITY": "roaddensity.ico",
        "ROAD SURFACE": "roadsurface.ico",
        "LINEAR REGRESSION": "mlr.ico",
        "RANDOM FOREST": "randomforest1.ico",
        "XG BOOST": "xgboost.ico",
        "ORDINARY LEAST SQUARES": "ols.ico",
        "SPATIAL LAG MODEL": "slm.ico",
        "SPATIAL DURBIN MODEL": "sdm.ico",
        "GEOGRAPHICALLY WEIGHTED REGRESSION": "gwr1.ico",
    }

    icon_name = icon_map.get(label, "BLGF.ico")

    if IS_FROZEN:
        # ── Production: spawn a new process (existing behaviour) ──
        exe_path = sys.executable
        p = subprocess.Popen(
            [exe_path, "--tool", label, "--icon", icon_name],
            shell=False,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        )
        TOOL_PROCESSES.append(p)
        return p
    else:
        # ── Dev / VS Code: import and run the tool on a thread ──
        mod_path = TOOL_MODULES.get(label)
        if not mod_path:
            messagebox.showerror("Tool Error", f"No module mapped for: {label}")
            return None

        def run_in_thread():
            _active_tool_titles.add(label)
            try:
                import importlib
                mod = importlib.import_module(mod_path)
                importlib.reload(mod)
                if hasattr(mod, "main") and callable(mod.main):
                    # Pass main3's existing hidden root so the tool never
                    # creates its own tk.Tk() — that's what causes the taskbar icon
                    import inspect
                    sig = inspect.signature(mod.main)
                    if sig.parameters:
                        mod.main(root)   # tool accepts a parent root
                    else:
                        mod.main()       # fallback for tools not yet updated
            except Exception:
                import traceback
                messagebox.showerror("Tool Crash", f"{mod_path}\n\n{traceback.format_exc()}")
            finally:
                _active_tool_titles.discard(label)

        t = threading.Thread(target=run_in_thread, daemon=True)
        t.start()

        # Return a dummy object so callers that check .pid / .poll() don't crash
        class _FakeProcess:
            pid = -1
            def poll(self): return None   # pretend still running

        fake = _FakeProcess()
        TOOL_PROCESSES.append(fake)
        return fake

def on_button_click(label):
    print(f"▶ Launching tool: {label}", flush=True)  # debug line
    if label in TOOL_MODULES:
        popup_windows[label] += 1
        run_tool_by_label(label)
    else:
        messagebox.showerror("Unknown Tool", f"No module mapped for: {label}")



def show_login_and_connect():
    login_win = tk.Toplevel()
    apply_icon(login_win)
    login_win.title("Database Login")
    login_win.geometry("260x220")
    login_win.grab_set()
    login_win.resizable(False, False)

    def on_login_close():
        try:
            messagebox.showwarning("Cancelled", "Login cancelled. Exiting.")
        except Exception:
            pass
        try:
            root.quit()
            root.destroy()
        except Exception:
            pass
        sys.exit(0)

    login_win.protocol("WM_DELETE_WINDOW", on_login_close)

    # Load saved credentials if available
    _creds_path = os.path.join(
        os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)),
        "pg_credentials.json"
    )
    saved = {}
    if os.path.exists(_creds_path):
        try:
            with open(_creds_path, "r") as f:
                saved = json.load(f)
        except Exception:
            saved = {}

    tk.Label(login_win, text="Host:").grid(row=0, column=0, sticky="e", padx=5, pady=3)
    host_entry = tk.Entry(login_win, width=25)
    host_entry.grid(row=0, column=1)
    host_entry.insert(0, saved.get("host", DB_HOST))

    tk.Label(login_win, text="Port:").grid(row=1, column=0, sticky="e", padx=5, pady=3)
    port_entry = tk.Entry(login_win, width=25)
    port_entry.grid(row=1, column=1)
    port_entry.insert(0, saved.get("port", DB_PORT))

    tk.Label(login_win, text="Database:").grid(row=2, column=0, sticky="e", padx=5, pady=3)
    db_entry = tk.Entry(login_win, width=25)
    db_entry.grid(row=2, column=1)
    db_entry.insert(0, saved.get("database", DB_NAME))

    tk.Label(login_win, text="Schema:").grid(row=3, column=0, sticky="e", padx=5, pady=3)
    schema_entry = tk.Entry(login_win, width=25)
    schema_entry.grid(row=3, column=1)
    schema_entry.insert(0, saved.get("schema", DB_SCHEMA))

    tk.Label(login_win, text="Username:").grid(row=4, column=0, sticky="e", padx=5, pady=3)
    user_entry = tk.Entry(login_win, width=25)
    user_entry.grid(row=4, column=1)
    user_entry.insert(0, saved.get("username", stored_username or ""))

    tk.Label(login_win, text="Password:").grid(row=5, column=0, sticky="e", padx=5, pady=3)
    pass_entry = tk.Entry(login_win, width=25, show="*")
    pass_entry.grid(row=5, column=1)
    pass_entry.insert(0, saved.get("password", stored_password or ""))

    def try_connect():
        username = user_entry.get()
        password = pass_entry.get()

        global stored_username, stored_password, DB_HOST, DB_PORT, DB_NAME, DB_SCHEMA
        stored_username = username
        stored_password = password
        DB_HOST = host_entry.get()
        DB_PORT = port_entry.get()
        DB_NAME = db_entry.get()
        DB_SCHEMA = schema_entry.get()

        try:
            conn = psycopg2.connect(
                host=DB_HOST, port=DB_PORT, database=DB_NAME,
                user=username, password=password
            )
            conn.close()
            login_win.destroy()
            launch_global_mapper()
        except Exception as e:
            messagebox.showerror("Login Failed", f"Could not connect:\n{e}")

        _creds_path = os.path.join(
            os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)),
            "pg_credentials.json"
        )
        with open(_creds_path, "w") as f:
            json.dump({
                "host": DB_HOST,
                "port": DB_PORT,
                "database": DB_NAME,
                "schema": DB_SCHEMA,
                "username": stored_username,
                "password": stored_password
            }, f)


    tk.Button(login_win, text="Login", command=try_connect, bg="#007acc", fg="white").grid(row=6, columnspan=2, pady=10)


# Hide minimize/maximize, show in taskbar, and disable close
root.title("CAMA Tools")

# Hide from taskbar using tool window style
import ctypes
GWL_EXSTYLE    = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW  = 0x00040000
SWP_NOSIZE     = 0x0001
SWP_NOMOVE     = 0x0002
SWP_NOACTIVATE = 0x0010
HWND_TOPMOST   = -1
SWP_NOSIZE     = 0x0001
SWP_NOMOVE     = 0x0002
SWP_NOACTIVATE = 0x0010

def hide_from_taskbar():
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
    ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)

root.after(100, hide_from_taskbar)

# Disable the close button functionality
def do_nothing():
    pass
root.protocol("WM_DELETE_WINDOW", do_nothing)

# ── GM canvas offsets (skip GM's panels/toolbars) ────────────────────
GM_TITLEBAR_H   = 130
GM_LEFT_PANEL_W = 240

# ── Get CAMA and GM sizes via Win32 (always accurate) ────────────────
def get_cama_size():
    cama_hwnd = ctypes.windll.user32.FindWindowW(None, "CAMA Tools")
    if cama_hwnd:
        r = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(cama_hwnd, ctypes.byref(r))
        return r.right - r.left, r.bottom - r.top
    return root.winfo_width(), root.winfo_height()

# ── Locked GM window (session-scoped HWND lock) ──────────────────────
# Root cause being fixed here: gw.getWindowsWithTitle('Global Mapper
# Pro') is a TITLE-SUBSTRING search, and GM's own native alert/message
# dialogs (e.g. "No hidden layers were found to close.") also carry a
# window title starting with "Global Mapper Pro v26.0.2 (b121824)
# [64-bit]" - confirmed via live window-title dump. Any function that
# re-runs this search after startup risks locking onto a transient
# dialog instead of the real main application window, corrupting
# position-following / topmost-stacking / closure detection.
#
# Fix: discover the real GM main window ONCE, in
# wait_for_global_mapper(), the same way as before (title-substring
# search + stability check). At the moment it is confirmed ready,
# capture its actual Win32 HWND and store it here. Every other
# GM-window consumer below (get_gm_rect, launch_main_window,
# monitor_gm_state, monitor_gm_closure) reads this locked HWND
# directly via raw ctypes calls (IsWindow / GetWindowRect /
# IsWindowVisible / IsIconic) instead of performing another title
# search. The lock is intentionally never re-acquired mid-session -
# if the locked HWND stops being a valid window, that means the
# original GM instance actually closed (see monitor_gm_closure()),
# which is the existing, unchanged shutdown signal.
#
# Scope note: this does NOT change is_relevant_window_focused(), which
# solely answers "does the CURRENTLY FOCUSED window belong to GM (or
# CAMA)" for show/hide purposes. Locking that one to a single HWND
# would break the intended behavior of keeping CAMA Tools visible
# while a GM dialog has focus.
_locked_gm_hwnd = [None]
# PID of the locked GM process, derived once from _locked_gm_hwnd at
# lock time (see wait_for_global_mapper()). Used by
# is_relevant_window_focused() as a process-identity check instead of
# matching window titles — a window belongs to "GM" because it's
# owned by this PID, not because its title happens to contain the
# text "Global Mapper" (which unrelated windows, e.g. a browser tab
# titled after a GM-related chat conversation, could also contain).
_locked_gm_pid = [None]


def _extract_hwnd(win32window):
    """
    Pulls the raw Win32 HWND out of a pygetwindow Win32Window object.

    pygetwindow (checked against v0.0.9, the current PyPI release -
    there is no newer version, and no published version has ever
    added a public accessor) does not expose the handle publicly; it
    only exists as the private attribute `_hWnd` set in
    Win32Window.__init__(). This function is the ONLY place in this
    file that touches that private attribute, so if a future
    pygetwindow release renames/removes it, there is exactly one line
    to fix.

    Verified against the actual pygetwindow source for the
    getWindowsWithTitle() code path: EnumWindows' callback is declared
    with a plain `ctypes.c_int` hWnd parameter, so the value stored in
    `_hWnd` is a plain Python int here - directly usable in raw
    ctypes.windll.user32 calls (IsWindow, GetWindowRect, etc.), same
    as the hwnd values get_hwnd_by_title() already returns elsewhere
    in this file.
    """
    return win32window._hWnd


def _locked_gm_snapshot():
    """
    Returns (left, top, width, height, visible, minimized) read
    directly from the locked GM HWND via ctypes - no title search.
    Returns None if no window has been locked yet, or if the locked
    HWND is no longer a valid window (GM closed).
    """
    hwnd = _locked_gm_hwnd[0]
    if not hwnd or not ctypes.windll.user32.IsWindow(hwnd):
        return None
    r = ctypes.wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    visible = bool(ctypes.windll.user32.IsWindowVisible(hwnd))
    minimized = bool(ctypes.windll.user32.IsIconic(hwnd))
    return r.left, r.top, r.right - r.left, r.bottom - r.top, visible, minimized


def get_gm_rect():
    try:
        snap = _locked_gm_snapshot()
        if snap and snap[4]:  # snap[4] = visible - preserves old .visible filter
            left, top, width, height, _visible, _minimized = snap
            return left, top, width, height
    except Exception:
        pass
    return None

def get_hwnd_by_title(partial_title):
    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
        if partial_title.lower() in buf.value.lower():
            found.append(hwnd)
        return True
    ctypes.windll.user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found[0] if found else None

# ── Foreground-window focus tracking ──────────────────────────────
def get_foreground_hwnd():
    return ctypes.windll.user32.GetForegroundWindow()

def hwnd_title(hwnd):
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value

def get_foreground_pid():
    hwnd = get_foreground_hwnd()
    pid = ctypes.wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value

def is_relevant_window_focused():
    """
    True if the foreground window belongs to the locked Global Mapper
    process, to CAMA Tools' own process, or to one of the CAMA tool
    subprocess windows (Road Width, Land Shape, etc.)

    Uses process identity (PID), not window-title text. A title-substring
    check here used to treat ANY window as "relevant" if its title merely
    contained the text "Global Mapper" or "CAMA Tools" — which produced
    false positives for completely unrelated windows, e.g. a browser tab
    showing a chat conversation titled "CAMA Tools Workflow". Checking
    which process actually owns the foreground window avoids that: GM's
    own dialogs share GM's real PID (so they still count as relevant,
    same as before), while an unrelated application never will, no
    matter what text happens to appear in its title.
    """
    fg = get_foreground_hwnd()
    fg_pid = get_foreground_pid()

    # Belongs to the locked Global Mapper process — covers GM's own main
    # window as well as its child dialogs/alerts (same process, same PID).
    if _locked_gm_pid[0] is not None and fg_pid == _locked_gm_pid[0]:
        return True

    # Our own process: covers every window this process creates —
    # messageboxes ("Database Update Failed", etc.), the login window,
    # file dialogs — whose titles match neither string above. Without
    # this, the main window withdraws behind its OWN dialogs and, with
    # no taskbar icon (WS_EX_TOOLWINDOW), appears to vanish until the
    # user happens to click Global Mapper again.
    if fg_pid == os.getpid():
        return True

    # Frozen mode: tool subprocesses, matched by PID
    if any(p.pid == fg_pid for p in TOOL_PROCESSES if p.poll() is None):
        return True

    # Dev mode: match by foreground window title against active tool labels
    if _active_tool_titles:
        fg_title = hwnd_title(fg).lower()
        return any(t.lower() in fg_title or fg_title in t.lower()
                   for t in _active_tool_titles)

    return False

def clamp_position(new_x, new_y):
    """Clamp CAMA position strictly inside GM's map canvas area."""
    gm = get_gm_rect()
    if not gm:
        return new_x, new_y
    gm_left, gm_top, gm_w, gm_h = gm
    cama_w, cama_h = get_cama_size()

    min_x = gm_left + GM_LEFT_PANEL_W
    min_y = gm_top  + GM_TITLEBAR_H
    max_x = gm_left + gm_w - cama_w
    max_y = gm_top  + gm_h - cama_h

    cx = max(min_x, min(new_x, max_x))
    cy = max(min_y, min(new_y, max_y))

    # Update relative offset for GM-follow
    cama_offset[0] = cx - gm_left
    cama_offset[1] = cy - gm_top

    return cx, cy

# ── Client-area drag (anywhere in the tkinter widget area) ───────────
_drag_origin = [0, 0]   # screen coords of click minus root origin

def start_move(event):
    _drag_origin[0] = event.x_root - root.winfo_x()
    _drag_origin[1] = event.y_root - root.winfo_y()

def do_move(event):
    new_x = event.x_root - _drag_origin[0]
    new_y = event.y_root - _drag_origin[1]
    new_x, new_y = clamp_position(new_x, new_y)
    root.geometry(f"+{new_x}+{new_y}")

def bind_drag_to_all(widget):
    # Only bind to frames/labels/root — skip canvases (they have tool bindings)
    if not isinstance(widget, tk.Canvas):
        widget.bind("<Button-1>", start_move, add="+")
        widget.bind("<B1-Motion>", do_move,   add="+")
    for child in widget.winfo_children():
        bind_drag_to_all(child)

bind_drag_to_all(root)

# ── Title-bar drag — intercept WM_MOVING at Win32 level ──────────────
WM_MOVING   = 0x0216
GWL_WNDPROC = -4

WNDPROCTYPE = ctypes.WINFUNCTYPE(
    ctypes.c_longlong,
    ctypes.wintypes.HWND,
    ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM
)

# Set correct arg/return types for Win32 calls
ctypes.windll.user32.SetWindowLongPtrW.restype  = ctypes.c_longlong
ctypes.windll.user32.SetWindowLongPtrW.argtypes = [
    ctypes.wintypes.HWND,
    ctypes.c_int,
    WNDPROCTYPE
]
ctypes.windll.user32.CallWindowProcW.restype  = ctypes.c_longlong
ctypes.windll.user32.CallWindowProcW.argtypes = [
    ctypes.c_longlong,
    ctypes.wintypes.HWND,
    ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM
]

# SINGLE-VARIABLE TEST (Z-order investigation): SetWindowPos() had no
# explicit argtypes/restype declared anywhere in this file. Every logged
# call — across every call site, across multiple independent live-test
# runs — returned ret=0, even when the target HWND was separately
# confirmed valid (IsWindow=1) and correctly identified (title='CAMA
# Tools'). With the HWND itself ruled out, an incorrect/incomplete
# ctypes declaration for SetWindowPos() is the remaining leading
# hypothesis and the next controlled variable to test. This follows the
# same pattern already proven above for SetWindowLongPtrW/CallWindowProcW.
# In particular, hWndInsertAfter (HWND_TOPMOST = -1) must marshal as a
# full pointer-width HWND, not a default (32-bit) C int, for
# SetWindowPos to recognize it as the special "topmost" sentinel value.
ctypes.windll.user32.SetWindowPos.restype  = ctypes.wintypes.BOOL
ctypes.windll.user32.SetWindowPos.argtypes = [
    ctypes.wintypes.HWND,   # hWnd
    ctypes.wintypes.HWND,   # hWndInsertAfter
    ctypes.c_int,           # X
    ctypes.c_int,           # Y
    ctypes.c_int,           # cx
    ctypes.c_int,           # cy
    ctypes.wintypes.UINT    # uFlags
]

_old_wnd_proc = None

def _new_wnd_proc(hwnd, msg, wparam, lparam):
    try:
        if msg == WM_MOVING:
            proposed = ctypes.cast(lparam, ctypes.POINTER(ctypes.wintypes.RECT)).contents
            cx, cy = clamp_position(proposed.left, proposed.top)
            cw, ch = get_cama_size()
            proposed.left   = cx
            proposed.top    = cy
            proposed.right  = cx + cw
            proposed.bottom = cy + ch
            return 1
    except Exception as e:
        print("WndProc error:", e)
    return ctypes.windll.user32.CallWindowProcW(
        _old_wnd_proc, hwnd, msg, wparam, lparam
    )

def install_wm_moving_hook():
    global _old_wnd_proc
    cama_hwnd = ctypes.windll.user32.FindWindowW(None, "CAMA Tools")
    if not cama_hwnd:
        root.after(300, install_wm_moving_hook)
        return
    proc = WNDPROCTYPE(_new_wnd_proc)
    root._wnd_proc_ref = proc   # prevent GC
    _old_wnd_proc = ctypes.windll.user32.SetWindowLongPtrW(
        cama_hwnd, GWL_WNDPROC, proc
    )
    print("✅ WM_MOVING hook installed")

root.after(400, install_wm_moving_hook)

root.title("CAMA Tools")
root.resizable(False, False)
# No fixed geometry — root will wrap tightly around content


# === IMAGE HANDLING ===
def round_image(img_path, size=(38, 38), radius=7):
    img = Image.open(img_path).convert("RGBA")

    original_ratio = img.width / img.height
    target_w, target_h = size

    if img.width != img.height:
        max_dim = max(img.width, img.height)
        square_img = Image.new("RGBA", (max_dim, max_dim), (0, 0, 0, 0))
        paste_x = (max_dim - img.width) // 2
        paste_y = (max_dim - img.height) // 2
        square_img.paste(img, (paste_x, paste_y))
        img = square_img

    img = img.resize(size, Image.Resampling.LANCZOS)

    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size[0], size[1]), radius=radius, fill=255)

    img.putalpha(mask)
    return ImageTk.PhotoImage(img)

hover_bg = round_image(str(ICONS_DIR / "hover.png"), size=(38, 38), radius=7)

clicked_bg = round_image(str(ICONS_DIR / "click.png"), size=(38, 38), radius=7)

# Map labels to icon filenames (short & clean):
_icon_files = {
    "ANY MAP TO LAND PARCEL": "influencemap.png",
    "INFLUENCE TO MAP": "distancefactor.png",
    "ROAD WIDTH": "roadwidth.png",
    "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO": "roadfrontage.png",
    "LOT LOCATION": "lotlocation.png",
    "LAND SHAPE": "landshape.png",
    "METERS FROM (SCHOOL, SHOP, TRANSPORT, CHURCH)": "distancefrom.png",
    "LANDMARKS WITHIN METERS": "landmarks200.png",
    "PARCEL TERRAIN LEVEL": "terrain.png",
    "ROAD DENSITY": "roaddensity.png",
    "ROAD SURFACE": "roadsurface.png",
    "LINEAR REGRESSION": "mlr.png",
    "RANDOM FOREST": "randomforest1.png",
    "XG BOOST": "xgboost.png",
    "ORDINARY LEAST SQUARES": "ols.png",
    "SPATIAL LAG MODEL": "slm.png",
    "SPATIAL DURBIN MODEL": "sdm.png",
    "GEOGRAPHICALLY WEIGHTED REGRESSION": "gwr1.png",
}

icon_paths = {k: str(ICONS_DIR / v) for k, v in _icon_files.items()}
icons = {label: ImageTk.PhotoImage(Image.open(path).resize((39, 39), Image.Resampling.LANCZOS))
         for label, path in icon_paths.items()}


# === TOOLBAR BUTTONS PANEL (blue area) ===
button_frame = tk.Frame(root, bg="#afd0f7", width=310)
button_frame.pack(padx=8, pady=(6, 6))
# No pack_propagate(False) — let it size naturally to its content

# === Tooltip descriptions for icon buttons ===
tooltip_descriptions = {
    "ANY MAP TO LAND PARCEL": "Any map source to Land Parcel",
    "INFLUENCE TO MAP": "Distance to nearest Fault Line",
    "ROAD WIDTH": "Measure average road width",
    "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO": "Analyze parcel depth and frontage",
    "LOT LOCATION": "Classify lots based on proximity",
    "LAND SHAPE": "Assess lot geometry and compactness",
    "METERS FROM (SCHOOL, SHOP, TRANSPORT, CHURCH)": "Measure distance to nearest POIs",
    "LANDMARKS WITHIN METERS": "Check nearby landmark coverage",
    "PARCEL TERRAIN LEVEL": "Analyze slope and elevation difference",
    "ROAD DENSITY": "Calculate road concentration in area",
    "ROAD SURFACE": "Identify surface type of nearby roads",
    "LINEAR REGRESSION": "Run linear model on land data",
    "RANDOM FOREST": "Train a Random Forest model",
    "XG BOOST": "Train data using XG Boost",
    "ORDINARY LEAST SQUARES": "Train data using Ordinary Least Squares",
    "SPATIAL LAG MODEL": "Train data using Spatial Lag Model",
    "SPATIAL DURBIN MODEL": "Train data using Spatial Durbin Model",
    "GEOGRAPHICALLY WEIGHTED REGRESSION": "Perform Geographically Weighted Regression"
}


# === BUTTON DEFINITIONS ===
buttons_1st_row = [
    "ANY MAP TO LAND PARCEL",
    "INFLUENCE TO MAP",
    "ROAD WIDTH",
    "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO",
    "LOT LOCATION",
    "LAND SHAPE",
]

buttons_2nd_row = [
    "METERS FROM (SCHOOL, SHOP, TRANSPORT, CHURCH)",
    "LANDMARKS WITHIN METERS",
    "PARCEL TERRAIN LEVEL",
    "ROAD DENSITY",
    "ROAD SURFACE",
]

popup_windows = {}
canvas_refs = {}



# === GROUP TITLE: Feature Management Tools ===
feature_title = tk.Label(button_frame, text="Feature Management Tools", font=("Segoe UI", 9, "bold"), bg="#afd0f7", anchor="w")
feature_title.pack(side="top", anchor="w", padx=8, pady=(4, 1))

first_row = tk.Frame(button_frame, bg="#afd0f7")
first_row.pack(side="top", anchor="w", padx=4, pady=(0, 2))

second_row = tk.Frame(button_frame, bg="#afd0f7")
second_row.pack(side="top", anchor="w", padx=4, pady=(0, 6))

# === 1st ROW BUTTONS WITH INDIVIDUAL CONTROL ===
for label in buttons_1st_row:
    canvas = tk.Canvas(first_row, width=48, height=48, highlightthickness=0, bg="#afd0f7")
    bg_img_id = canvas.create_image(3, 3, anchor="nw", image=None)

    if label in icons:
        icon_img_id = canvas.create_image(2, 2, anchor="nw", image=icons[label])
    else:
        # Isolated fallback for a button with no bundled icon yet (e.g.
        # "INFLUENCE TO MAP"): render plain text instead of fabricating
        # a placeholder PNG. Guards on `icons` -- the runtime dict this
        # loop actually indexes two lines above -- rather than
        # `_icon_files`, its config-source dict, so this stays correct
        # even if the two ever diverge for any reason. Dropping in a
        # real icon later is then a clean, minimal change: add one
        # `_icon_files` entry, and this branch simply stops triggering.
        icon_img_id = canvas.create_text(
            24, 24, text="ITM", font=("Segoe UI", 9, "bold"), fill="#1a1a1a"
        )

    canvas_refs[label] = (canvas, bg_img_id)
    popup_windows[label] = 0

    def make_bindings(c, lbl, bg_id):
        def on_enter(e):
            c.itemconfig(bg_id, image=hover_bg)
        def on_leave(e):
            c.itemconfig(bg_id, image="")
        def on_click(e):
            on_button_click(lbl)
        c.bind("<Enter>", on_enter)
        c.bind("<Leave>", on_leave)
        c.bind("<Button-1>", on_click)

    make_bindings(canvas, label, bg_img_id)

    # ✅ Tooltip with icon and label. Falls back to the already-bundled
    # BLGF.png (same resource apply_icon() already relies on
    # everywhere) for any button with no entry in icon_paths yet --
    # add_tooltip() always needs a real, openable image path.
    tooltip_icon_path = icon_paths.get(label, resource_path("BLGF.png"))
    add_tooltip(canvas, tooltip_icon_path, label, tooltip_descriptions.get(label, "Launch tool"), canvas=canvas, bg_id=bg_img_id)
    canvas.pack(side="left", padx=(2, 2), pady=(2, 2))

    # Per-button packing adjustments
    if label == "ANY MAP TO LAND PARCEL":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "ROAD WIDTH":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "LOT LOCATION":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "LAND SHAPE":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))

# === 2nd ROW BUTTONS WITH INDIVIDUAL CONTROL ===
for label in buttons_2nd_row:
    canvas = tk.Canvas(second_row, width=48, height=48, highlightthickness=0, bg="#afd0f7")
    bg_img_id = canvas.create_image(3, 3, anchor="nw", image=None)
    icon_img_id = canvas.create_image(2, 2, anchor="nw", image=icons[label])

    canvas_refs[label] = (canvas, bg_img_id)
    popup_windows[label] = 0

    def make_bindings(c, lbl, bg_id):
        def on_enter(e):
            c.itemconfig(bg_id, image=hover_bg)
        def on_leave(e):
            c.itemconfig(bg_id, image="")
        def on_click(e):
            on_button_click(lbl)
        c.bind("<Enter>", on_enter)
        c.bind("<Leave>", on_leave)
        c.bind("<Button-1>", on_click)

    make_bindings(canvas, label, bg_img_id)

    # ✅ Tooltip with icon and label
    add_tooltip(canvas, icon_paths[label], label, tooltip_descriptions.get(label, "Launch tool"), canvas=canvas, bg_id=bg_img_id)
    canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
  
    # Per-button packing adjustments
    if label == "METERS FROM (SCHOOL, SHOP, TRANSPORT, CHURCH)":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "LANDMARKS WITHIN METERS":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "PARCEL TERRAIN LEVEL":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "ROAD DENSITY":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "ROAD SURFACE":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
        

# Create a frame to hold both buttons
btn_frame = tk.Frame(root)
btn_frame.pack(pady=(6, 6))



# === UPDATE MAP BUTTON ===
update_map_btn = tk.Button(
    btn_frame, text="Update Map",
    width=20,
    bg="#6a9f2f",
    fg="white",
    activebackground="#4c7a20",
    activeforeground="white",
    relief="flat",
    command=update_map_and_select_recorded
)
update_map_btn.pack(side="left", padx=5)  # Add horizontal spacing

# === UPDATE DB BUTTON ===
update_btn = tk.Button(
    btn_frame, text="Update Database",
    width=20,
    bg="#007acc",
    fg="white",
    activebackground="#005f99",
    activeforeground="white",
    relief="flat",
    command=update_database_from_geopackage
)
update_btn.pack(side="left", padx=5)  # Add horizontal spacing



def launch_main_window():
    snap = _locked_gm_snapshot()
    root.update_idletasks()  # ensure actual size is computed first
    cama_w = root.winfo_reqwidth()
    cama_h = root.winfo_reqheight()

    if snap and snap[4]:  # snap[4] = visible - preserves old .visible filter
        gm_left, gm_top, gm_width, gm_height, _visible, _minimized = snap
        new_x = gm_left + gm_width - cama_w - 10
        new_y = gm_top + gm_height - cama_h - 40
        root.geometry(f"+{new_x}+{new_y}")
    else:
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        root.geometry(f"+{(sw - cama_w) // 2}+{(sh - cama_h) // 2}")

    root.update_idletasks()
    root.attributes("-alpha", 1)
    root.deiconify()
    root.lift()
    # Pin as topmost at Win32 level — more reliable than tkinter's -topmost.
    # Uses GetParent(root.winfo_id()) — the same pattern hide_from_taskbar()
    # already uses — rather than a title-based lookup, to get CAMA's real
    # top-level HWND directly.
    cama_hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    if cama_hwnd:
        ctypes.windll.user32.SetWindowPos(
            cama_hwnd, HWND_TOPMOST,
            0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
        )

    # Force Z-order above GM immediately after showing
    def _force_z_order():
        cama_hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        if cama_hwnd:
            ctypes.windll.user32.SetWindowPos(
                cama_hwnd, HWND_TOPMOST,
                0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
            )

            # Z-order fix: CAMA was just re-pinned topmost above — restore
            # any currently-visible tooltip's topmost status so it doesn't
            # end up behind CAMA. See _repin_active_tooltips().
            _repin_active_tooltips()

    root.after(500, _force_z_order)   # after GM settles
    root.after(1500, _force_z_order)  # second pass in case GM repaints on top


import pygetwindow as gw
import time

def launch_global_mapper():
    import re
    import shutil
    import tempfile

    global GM_EXE_PATH
    if not GM_EXE_PATH:
        GM_EXE_PATH = get_global_mapper_path()
    if not GM_EXE_PATH:
        messagebox.showerror("Global Mapper", "global_mapper.exe not found. Please locate it.")
        return

    gmw_path = selected_gmw_file
    patched_path = gmw_path  # default: use as-is

    try:
        with open(gmw_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        # Patch DB_NAME
        content = re.sub(
            r'(POSTGIS_DATABASE\s*=\s*")[^"]*(")',
            lambda m: m.group(1) + DB_NAME + m.group(2),
            content
        )
        # Patch DB_HOST
        content = re.sub(
            r'(POSTGIS_HOST\s*=\s*")[^"]*(")',
            lambda m: m.group(1) + DB_HOST + m.group(2),
            content
        )
        # Patch DB_PORT
        content = re.sub(
            r'(POSTGIS_PORT\s*=\s*")[^"]*(")',
            lambda m: m.group(1) + DB_PORT + m.group(2),
            content
        )
        # Patch username
        content = re.sub(
            r'(POSTGIS_USER\s*=\s*")[^"]*(")',
            lambda m: m.group(1) + stored_username + m.group(2),
            content
        )
        # Patch password
        content = re.sub(
            r'(POSTGIS_PASSWORD\s*=\s*")[^"]*(")',
            lambda m: m.group(1) + stored_password + m.group(2),
            content
        )

        # Write patched content to a temp file so we don't overwrite the original
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".gmw", delete=False,
            encoding="utf-8", prefix="cama_patched_"
        )
        tmp.write(content)
        tmp.close()
        patched_path = tmp.name
        print(f"✅ Patched .gmw written to: {patched_path}")

    except Exception as e:
        print(f"⚠ Could not patch .gmw file: {e} — launching with original")

    subprocess.Popen([GM_EXE_PATH, patched_path], shell=False)
    wait_for_global_mapper()

prev_position  = [None, None]
prev_gm_rect   = [None, None, None, None]  # left, top, width, height
cama_offset    = [None, None]              # CAMA's offset relative to GM
_topmost_recheck_counter = [0]             # throttles repeated SetWindowPos calls to avoid title-bar flicker
_active_tool_titles = set()               # tracks open tool window titles in dev mode

def monitor_gm_state():
    try:
        snap = _locked_gm_snapshot()
        if snap and snap[4]:  # snap[4] = visible - preserves old .visible filter
            gm_left, gm_top, gm_w, gm_h, _visible, gm_minimized = snap

            if gm_minimized or not is_relevant_window_focused():
                if root.state() != 'withdrawn':
                    root.withdraw()
                    # CAMA itself is going invisible — any tooltip that
                    # happened to be showing has no business staying on
                    # screen (it would float over whatever other window
                    # the user switched to). It intentionally does NOT
                    # come back when CAMA is shown again below — it only
                    # reappears through the normal hover (enter) path.
                    if _active_tooltips:
                        for _tip in list(_active_tooltips):
                            _tip.withdraw()
                        _active_tooltips.clear()
            else:
                just_shown = (root.state() == 'withdrawn')
                if just_shown:
                    root.attributes("-alpha", 1)
                    root.deiconify()

                # See note above launch_main_window()'s cama_hwnd assignment.
                cama_hwnd = ctypes.windll.user32.GetParent(root.winfo_id())

                # --- Z-order: only re-pin topmost when just shown, or occasionally ---
                # Calling SetWindowPos every 200ms causes visible title-bar flicker.
                if cama_hwnd:
                    if just_shown:
                        ctypes.windll.user32.SetWindowPos(
                            cama_hwnd, HWND_TOPMOST,
                            0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                        )
                        # Z-order fix: see _repin_active_tooltips().
                        _repin_active_tooltips()
                    else:
                        _topmost_recheck_counter[0] += 1
                        if _topmost_recheck_counter[0] >= 10:  # ~every 2s instead of every 200ms
                            _topmost_recheck_counter[0] = 0
                            ctypes.windll.user32.SetWindowPos(
                                cama_hwnd, HWND_TOPMOST,
                                0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                            )
                            # Z-order fix: see _repin_active_tooltips().
                            _repin_active_tooltips()

                # --- Follow GM when it moves ---
                gm_moved = (
                    prev_gm_rect[0] != gm_left or
                    prev_gm_rect[1] != gm_top  or
                    prev_gm_rect[2] != gm_w    or
                    prev_gm_rect[3] != gm_h
                )

                if gm_moved:
                    if cama_offset[0] is None:
                        # First time — set offset from current CAMA position
                        cama_offset[0] = root.winfo_x() - gm_left
                        cama_offset[1] = root.winfo_y() - gm_top
                    else:
                        # GM moved — reposition CAMA using saved offset
                        new_x = gm_left + cama_offset[0]
                        new_y = gm_top  + cama_offset[1]

                        # Clamp inside GM bounds
                        cama_w = root.winfo_width()
                        cama_h = root.winfo_height()
                        new_x = max(gm_left + GM_LEFT_PANEL_W,
                                    min(new_x, gm_left + gm_w - cama_w))
                        new_y = max(gm_top  + GM_TITLEBAR_H,
                                    min(new_y, gm_top  + gm_h - cama_h))

                        root.geometry(f"+{new_x}+{new_y}")

                    prev_gm_rect[0] = gm_left
                    prev_gm_rect[1] = gm_top
                    prev_gm_rect[2] = gm_w
                    prev_gm_rect[3] = gm_h

        else:
            print("❌ Global Mapper closed. Closing Tkinter.")
            root.destroy()

    except Exception as e:
        print("Error in GM monitor:", e)

    root.after(200, monitor_gm_state)

def monitor_gm_closure():
    snap = _locked_gm_snapshot()
    if not snap or not snap[4]:  # not found, or found but not visible - preserves old .visible filter
        print("❌ Global Mapper closed. Exiting tools.")
        root.destroy()
    else:
        root.after(2000, monitor_gm_closure)


_gm_stable_count = [0]  # needs to be visible twice before we consider it ready

def wait_for_global_mapper():
    gm_windows = [w for w in gw.getWindowsWithTitle('Global Mapper Pro') if w.visible]
    # Require GM window to be visible AND non-minimized AND have a real size
    ready = (
        gm_windows and
        not gm_windows[0].isMinimized and
        gm_windows[0].width > 100 and
        gm_windows[0].height > 100
    )
    if ready:
        _gm_stable_count[0] += 1
        if _gm_stable_count[0] >= 2:      # stable for 2 consecutive checks (2s)
            # Lock onto the exact Win32Window instance pygetwindow just
            # confirmed as ready — not a fresh title lookup. This is
            # the single point where the session-wide lock is set; see
            # _locked_gm_hwnd for why every other GM-window consumer
            # below reads this instead of searching by title again.
            _locked_gm_hwnd[0] = _extract_hwnd(gm_windows[0])
            _pid_buf = ctypes.wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(_locked_gm_hwnd[0], ctypes.byref(_pid_buf))
            _locked_gm_pid[0] = _pid_buf.value
            _log(f"Global Mapper is fully open. Locked HWND: {_locked_gm_hwnd[0]} | Locked PID: {_locked_gm_pid[0]}")
            launch_main_window()
            monitor_gm_state()
            monitor_gm_closure()
            return
    else:
        _gm_stable_count[0] = 0           # reset if GM disappears or isn't ready

    print("⏳ Waiting for Global Mapper...")
    root.after(1000, wait_for_global_mapper)

# Step 1: Resize native dialog to medium centered via ctypes
import threading

def resize_file_dialog():
    import time
    time.sleep(0.25)
    hwnd = ctypes.windll.user32.FindWindowW(None, "Select Global Mapper Workspace File")
    if hwnd:
        screen_w = ctypes.windll.user32.GetSystemMetrics(0)
        screen_h = ctypes.windll.user32.GetSystemMetrics(1)
        win_w, win_h = 780, 500
        x = (screen_w - win_w) // 2
        y = (screen_h - win_h) // 2
        ctypes.windll.user32.MoveWindow(hwnd, x, y, win_w, win_h, True)

def startup_sequence():
    global selected_gmw_file

    threading.Thread(target=resize_file_dialog, daemon=True).start()

    gmw_file = filedialog.askopenfilename(
        title="Select Global Mapper Workspace File",
        filetypes=[("Global Mapper Workspace", "*.gmw")]
    )

    if not gmw_file:
        try:
            messagebox.showwarning("Cancelled", "No GMW file selected. Exiting.")
        except Exception:
            pass
        try:
            root.quit()
            root.destroy()
        except Exception:
            pass
        sys.exit(0)

    selected_gmw_file = gmw_file
    show_login_and_connect()

root.after(0, startup_sequence)
root.mainloop()