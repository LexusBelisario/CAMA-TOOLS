import os
import re
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox
import geopandas as gpd
from shapely.geometry import Point
import subprocess
import math
import json
import psycopg2
from sqlalchemy import create_engine, inspect, text
from shapely.validation import make_valid

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

    # Taskbar / Alt-Tab icon
    if os.path.exists(ico):
        try:
            win.iconbitmap(ico)
        except Exception:
            pass

    # Tk titlebar fallback
    if os.path.exists(png):
        try:
            img = tk.PhotoImage(file=png)
            win.iconphoto(True, img)
            win._icon_ref = img  # prevent garbage collection
        except Exception:
            pass


# Paths to icon and Global Mapper EXE
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"
def _get_credentials_path():
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "pg_credentials.json")
    else:
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "pg_credentials.json"
        )

CREDENTIALS_FILE = _get_credentials_path()

barangay_source = None
road_source = None
output_mode = None
buffer_size = None  # 🔹 global buffer size (meters)

# density_column_overrides: {path: existing_col_name} -- for any LOCAL
# Land Parcel source where a pre-existing "cama_dens_road"-like column
# was detected (see _check_parcel_density_conflicts() below) and the
# user confirmed proceeding at Run time. Read by run_processing() and
# passed into process_density() as output_column_name, so the tool
# writes back into the EXACT existing column (preserving its original
# casing) instead of always writing a hardcoded "CAMA_DENS_ROAD" --
# the latter would silently create a confusing duplicate column
# whenever the existing one used different casing (e.g. a detected
# "caMA_dens_ROAD" alongside a new "CAMA_DENS_ROAD"). A source with no
# entry here uses the default "CAMA_DENS_ROAD" name. Scope: LOCAL
# sources only -- Database Land Parcel sources are explicitly out of
# scope for this check (see _check_parcel_density_conflicts()).
density_column_overrides = {}

# ---------------- CRS Utility ----------------
def get_prs92_zone(labeled_gdfs):
    """
    Choose PRS92 zone EPSG from the combined bbox-midpoint longitude of
    one or more input GeoDataFrames.

    labeled_gdfs: list of (label, gdf) tuples, e.g.
        [("Land Parcel", brgy_gdf), ("Road Network", road_gdf)]
    The label is used only for diagnostics. It has no effect on CRS
    detection.

    Uses total_bounds, not a unioned-geometry centroid -- unary_union.centroid
    is a known source of GEOS TopologyExceptions on real-world cadastral
    data with invalid geometries (confirmed empirically: total_bounds
    stays fine on a mix of valid + self-intersecting polygons, while
    unary_union.centroid raises GEOSException/TopologyException on the
    exact same input). Same zone-boundary thresholds as before; only
    the longitude used to evaluate them has changed.

    total_bounds itself does not invoke GEOS topology operations, so it
    is not expected to raise the same TopologyException produced by
    unary_union.centroid -- but it is NOT immune to bad input in
    general: a GeoDataFrame with no usable geometry (all None, or all
    empty-but-non-null Polygon() shapes) still yields NaN bounds
    instead of crashing, which would otherwise silently fall through
    every "lon < ..." comparison below (NaN comparisons are always
    False) into the final "else" branch -- an incorrect zone returned
    with no warning at all.

    Two layers of defense against that:
      1. Pre-filter: skip any gdf that's None, has zero rows, or has
         no non-null geometry at all (geometry.notna().any()). Note
         notna() alone does NOT catch empty-but-non-null geometries
         (confirmed empirically -- Shapely's empty Polygon() passes
         notna() but still produces NaN bounds), so this filter is a
         cheap first pass, not a complete guarantee.
      2. Per-gdf post-check: after computing each gdf's total_bounds,
         explicitly verify it's not NaN and raise immediately, naming
         the specific layer -- BEFORE appending to all_bounds. This has
         to happen here and not after combining: the combination step
         below uses Python's built-in min()/max(), not a NaN-aware
         aggregation, so a single NaN slipping into all_bounds would
         silently propagate or vanish depending on its position in the
         list (confirmed empirically) rather than raising anything.
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

# ---------------- Geometry Fix ----------------
# NOTE (Part A3 investigation, resolved as NOT a confirmed bug): the two
# call sites below overwrite brgy_gdf["geometry"] directly with this
# function's repaired output, before process_density() runs -- the same
# "writes the fix into the output column" pattern flagged as a bug in
# other tools. Investigated specifically for THIS tool and found to be
# architecturally different:
#   1. process_density() only reads row.geometry.centroid from each
#      parcel -- never buffers/intersects the parcel polygon itself.
#   2. Empirically confirmed .centroid is safe on invalid geometry (no
#      crash, unlike unary_union) and the resulting centroid shift after
#      repair is negligible (~0.09m in a stress test, vs. a typical
#      1000m search radius) -- i.e. no evidence of a materially wrong
#      DENS_ROAD result.
#   3. This function does NOT have the historical "keep only the
#      largest MultiPolygon piece" defect that the older road_width.py
#      version had (confirmed by reading its body -- it returns
#      whatever buffer(0)/make_valid() produces, dropping nothing).
# What remains is a genuine but different question: should this tool
# persist repaired parcel geometry to its output at all, vs. keeping
# the original shape (matching the convention used elsewhere in this
# project)? That's a data-management POLICY decision, not a computation
# bug -- left as-is pending that decision, not modified here.
def fix_geometry(geom):
    if geom is None or geom.is_empty: 
        return None
    try:
        if not geom.is_valid:
            geom = geom.buffer(0)
        if not geom.is_valid:
            geom = make_valid(geom)
        return geom if not geom.is_empty else None
    except:
        return None

# ---------------- DB Helpers ----------------
def load_db_credentials():
    path = _get_credentials_path()
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None

def get_geometry_column(table_name, engine, schema):
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT f_geometry_column FROM geometry_columns
                WHERE f_table_schema=:schema AND f_table_name=:table
            """),{"schema":schema,"table":table_name}).fetchone()
            return row[0] if row else None
    except: 
        return None

def read_postgis_clean(table, engine, schema):
    geom_col = get_geometry_column(table,engine,schema)
    insp = inspect(engine)
    cols = [c["name"] for c in insp.get_columns(table,schema=schema) if c["name"]!=geom_col]
    col_str = ", ".join([f'"{c}"' for c in cols]) if cols else ""
    q = f'SELECT {col_str+", " if col_str else ""}"{geom_col}" AS geometry FROM "{schema}"."{table}"'
    return gpd.read_postgis(q, engine, geom_col="geometry")

def normalize_name(name):
    import re
    return re.sub(r'[^a-z]', '', name.lower())

def fetch_tables(schema):
    creds=load_db_credentials()
    if not creds: return []
    try:
        conn=psycopg2.connect(
            host=creds["host"],port=creds["port"],dbname=creds["database"],
            user=creds["username"],password=creds["password"]
        )
        cur=conn.cursor()
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s;",(schema,))
        return [r[0] for r in cur.fetchall()]
    except: 
        return []

def find_matching_table(local_name, schema):
    lname = normalize_name(local_name)
    for t in fetch_tables(schema):
        if lname in normalize_name(t) or normalize_name(t) in lname:
            return t
    return None


# ---------------- Output filename helpers ----------------
# Ported from road_width.py's validated pattern, already successfully
# adapted in road_frontage.py and lot_location.py. See those files for
# the full design rationale; only a brief summary is repeated here.

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
    yet in `folder`. If it already exists, strip any existing trailing
    "_<N>" from the desired name to get a root, scan `folder` for every
    file matching "<root>_<N>.<ext>", and use "<root>_<max(N)+1>" --
    the highest N found ANYWHERE in the folder, not just "the source
    file's own N + 1".

    This tool has no companion/QA outputs, so there is no
    with_output_suffix call needed here -- unlike road_frontage.py.
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
        pass  # folder unreadable -- fall through with max_n=0, worst case uses N=1

    return f"{root}_{max_n + 1}"


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

    Returns "overwrite", "new", or "cancel" (also returned if the
    dialog's own titlebar close button is used).

    Ported from road_width.py's validated implementation, already
    adapted in road_frontage.py and lot_location.py. Deliberately does
    NOT call dialog.transient(parent): this app's root is permanently
    withdrawn (see main()), and transient() on a withdrawn parent is a
    known source of window-manager-dependent "dialog never becomes
    viewable" behavior. grab_set()+deiconify()+lift()+focus_force()+
    topmost is used instead, matching this file's own existing dialog
    pattern (see _pick_db_tables()).
    """
    result = {"choice": "cancel"}

    dialog = tk.Toplevel(parent)
    apply_icon(dialog)
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


# ---------------- Core Processing ----------------
def process_density(brgy_gdf, road_gdf, source_name="", output_column_name="CAMA_DENS_ROAD"):
    """Compute road density (m/m²) for each barangay polygon.

    output_column_name : str -- the column name the computed density is
        written to. Defaults to "CAMA_DENS_ROAD" (this tool's normal
        output, CAMA_-prefixed per project-wide column naming convention
        -- see road_width.py's own ROAD_WIDTH -> CAMA_ROAD_WIDTH). The
        GUI overrides this per-source when the selected LOCAL parcel
        layer already has an existing "cama_dens_road"-like column (any
        casing) -- the exact existing name/casing is passed here so
        processing writes back into that same column instead of
        creating a hardcoded "CAMA_DENS_ROAD" alongside it as a
        confusing duplicate.
    """
    global buffer_size
    orig_crs = brgy_gdf.crs

    # ✅ Project both to correct PRS92 zone (combined parcel + road extent)
    zone_epsg = get_prs92_zone([("Land Parcel", brgy_gdf), ("Road Network", road_gdf)])
    print(f"🌍 [{source_name}] Using PRS92 EPSG:{zone_epsg}")
    brgy_proj = brgy_gdf.to_crs(epsg=zone_epsg)
    road_proj = road_gdf.to_crs(epsg=zone_epsg)

    brgy_proj = brgy_proj[brgy_proj.geometry.type.isin(["Polygon", "MultiPolygon"])]
    road_proj = road_proj[road_proj.geometry.type.isin(["LineString", "MultiLineString"])]
    print(f"ℹ️ [{source_name}] Parcels after filter: {len(brgy_proj)}, Roads after filter: {len(road_proj)}")
    if road_proj.empty:
        print(f"⚠️ [{source_name}] No road features remain after geometry filter — check road layer geometry type.")

    brgy_proj[output_column_name] = 0.0
    radius = buffer_size if buffer_size else 1000  # meters
    buffer_area = math.pi * (radius ** 2)

    for idx, row in brgy_proj.iterrows():
        centroid = row.geometry.centroid
        buffer = centroid.buffer(radius)
        intersecting = road_proj[road_proj.geometry.intersects(buffer)]
        if intersecting.empty:
            continue
        clipped = intersecting.geometry.intersection(buffer)
        total_length = clipped.length.sum()
        dens = round(total_length / buffer_area, 6)
        brgy_proj.at[idx, output_column_name] = dens

        print(f"🟡 Feature {idx}: Length={round(total_length,2)} m, Density={dens}")

    # ✅ Reproject back to original CRS
    if orig_crs:
        brgy_proj = brgy_proj.to_crs(orig_crs)

    return brgy_proj

# REPLACE WITH

# ========================= GLOBAL MAPPER =========================
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


# ========================= PARCEL COLUMN-CONFLICT CHECK =========================
# _check_parcel_density_conflicts(): checks LOCAL Land Parcel source(s)
# for an existing column matching "cama_dens_road" (case-insensitive
# exact match) -- this tool is about to write its computed road density
# into that column, and on_run() below shows a combined confirmation
# dialog before proceeding.
#
# Unlike road_width.py/lot_location.py, this tool has no background
# worker thread / progress window / queue-polling architecture, so this
# runs synchronously on the main thread, called directly from on_run()
# right before Run actually starts. This is deliberate -- adding
# threading here would be a separate, out-of-scope architectural change
# (see project notes on background-processing not yet being scoped for
# the other tools), not part of this column-conflict task.
#
# Read approach: plain gpd.read_file(path), matching road_width.py's own
# canonical _read_gdf_worker() exactly -- no partial/schema-only read
# trick (e.g. rows=0) is used, since that is not confirmed consistently
# supported across the GeoPandas/Fiona/pyogrio versions in this
# project's environment. A full read here costs the same I/O as
# road_width.py's own equivalent check.
#
# A read failure here is NEVER treated as a column-conflict failure --
# it only skips the conflict check for that one source (logged to
# console). The real read inside run_processing() further below remains
# solely responsible for surfacing any genuine read error to the user.
#
# Scope: LOCAL sources only. Database Land Parcel sources are
# explicitly out of scope for this check per the project task
# definition -- callers must not invoke this for "db"-mode sources.
def _check_parcel_density_conflicts(local_paths):
    """
    Returns a list of (path, existing_col_name) tuples -- one entry
    only for local sources where a column matching "cama_dens_road"
    (case-insensitive) was actually found. existing_col_name preserves
    the exact casing found in the source (e.g. a column literally named
    "caMA_dens_ROAD" is returned as-is, not normalized), so the
    confirmation dialog and the eventual write-back both show/use the
    real casing.
    """
    conflicts = []
    for path in local_paths:
        try:
            gdf = gpd.read_file(path)
        except Exception as e:
            print(f"⚠️ Could not read parcel layer to check for an "
                  f"existing CAMA_DENS_ROAD column: {path}: {e}")
            continue
        existing_col = next(
            (c for c in gdf.columns if c.lower() == "cama_dens_road"), None
        )
        if existing_col:
            conflicts.append((path, existing_col))
    return conflicts


# ========================= DB TABLE PICKER =========================
def _pick_db_tables(parent, tables, multi, on_select):
    from tkinter import ttk
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


# ========================= MAIN WINDOW =========================
def open_main_window(root):
    from tkinter import ttk

    win = tk.Toplevel(root)
    apply_icon(win)
    win.title("Road Density Tool")
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
    output_dest_type   = tk.StringVar(master=win, value="local")

    parcel_local_paths = []
    parcel_db_tables   = []
    road_local_path    = tk.StringVar(master=win)
    road_db_table      = tk.StringVar(master=win)
    output_local_dir   = tk.StringVar(master=win)
    buffer_var         = tk.StringVar(master=win, value="1000")

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
            _update_run_button_state()

    def _on_parcel_db_selected(sel):
        parcel_db_tables.clear()
        parcel_db_tables.extend(sel)
        parcel_db_label.set(f"{len(sel)} table(s) selected")
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
        _pick_db_tables(win, tables, multi=True, on_select=_on_parcel_db_selected)

    def _toggle_parcel():
        if parcel_source_type.get() == "local":
            parcel_lbl.config(textvariable=parcel_files_var)
            parcel_btn.config(text="Browse…", command=browse_parcel_files)
        else:
            parcel_lbl.config(textvariable=parcel_db_label)
            parcel_btn.config(text="Select…", command=browse_parcel_db)
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

    # ── SECTION 3: BUFFER RADIUS ─────────────────────────────────
    section_label(win, "Buffer Radius")

    buffer_frame = tk.Frame(win)
    buffer_frame.pack(fill="x", padx=18, pady=2)
    tk.Label(buffer_frame, text="Radius (meters):",
             anchor="w").pack(side="left")
    tk.Entry(buffer_frame, textvariable=buffer_var,
             width=10).pack(side="left", **PAD)

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
        global barangay_source, road_source, output_mode, buffer_size

        # validate parcel
        if parcel_source_type.get() == "local":
            if not parcel_local_paths:
                messagebox.showerror("Missing Input",
                    "Please select at least one Land Parcel file.")
                return
            barangay_source = ("local", tuple(parcel_local_paths))
        else:
            if not parcel_db_tables:
                messagebox.showerror("Missing Input",
                    "Please select at least one Land Parcel table.")
                return
            barangay_source = ("db", parcel_db_tables)

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

        # validate buffer
        try:
            buffer_size = float(buffer_var.get())
            if buffer_size <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid Input",
                "Please enter a valid positive number for the buffer radius.")
            return

        # validate output
        if output_dest_type.get() == "local":
            if not output_local_dir.get():
                messagebox.showerror("Missing Input",
                    "Please select an output folder.")
                return
            output_mode = ("local", output_local_dir.get())
        else:
            output_mode = ("db", None)

        # Warn about any LOCAL Land Parcel source(s) that already have a
        # column matching "cama_dens_road" (case-insensitive exact
        # match) -- this tool is about to write its computed road
        # density into that column. The dialog shows the existing
        # column exactly as found in the source (original casing
        # preserved). Shown once, combined across every affected
        # source, only here at Run time -- never at Browse time.
        # Declining cancels the run entirely rather than skipping just
        # the affected source(s), so the user always knows exactly what
        # did or didn't happen instead of a partial batch silently
        # going through. Database Land Parcel sources are explicitly
        # out of scope for this check (see _check_parcel_density_conflicts()).
        global density_column_overrides
        if parcel_source_type.get() == "local":
            conflicts = _check_parcel_density_conflicts(parcel_local_paths)
            if conflicts:
                lines = "\n".join(
                    f"- '{os.path.basename(path)}' already has a '{existing_col}' column"
                    for path, existing_col in conflicts
                )
                proceed = messagebox.askyesno(
                    "Existing CAMA_DENS_ROAD column found",
                    f"{lines}\n\n"
                    "Processing will overwrite the existing column(s) with the "
                    "newly computed values.\n\nProceed?"
                )
                if not proceed:
                    print("Run cancelled by user (existing CAMA_DENS_ROAD column(s) found).")
                    return
                # Preserve each source's existing column name/casing
                # exactly -- e.g. a detected "caMA_dens_ROAD" is written
                # back to "caMA_dens_ROAD", not a hardcoded
                # "CAMA_DENS_ROAD" -- so no duplicate column is ever
                # created regardless of the existing casing. A source
                # with no entry here (no conflict was found) simply
                # uses the default name in process_density() below.
                density_column_overrides = dict(conflicts)
            else:
                density_column_overrides = {}
        else:
            density_column_overrides = {}
        # PRIORITY 2: existing OUTPUT-FILE conflict check (local output only).
        # Resolved ONCE, up front, on the main thread -- before win.destroy()
        # so the dialog has a live parent window. The chosen action applies
        # to ALL conflicting sources in the batch (one combined dialog, not
        # per-file). Cancel aborts the entire run; nothing is written.
        # Ported from road_width.py / road_frontage.py's validated pattern.
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


        win.destroy()
        run_processing(overwrite_mode)

    # Single source of truth for the Run button's enabled/disabled
    # colors -- used both at button creation and inside
    # _update_run_button_state() below, so there's only one place to
    # change if the theme changes later.
    RUN_BTN_BG_ENABLED  = "#2e7d32"
    RUN_BTN_FG_ENABLED  = "white"
    RUN_BTN_BG_DISABLED = "#e0e0e0"
    RUN_BTN_FG_DISABLED = "#888888"

    def _is_valid_buffer(value):
        """
        Same acceptance rule on_run() already applies (float, > 0) --
        used here only to gate the Run button, not to clamp or
        auto-correct buffer_var itself.
        """
        try:
            r = float(value)
        except (TypeError, ValueError):
            return False
        return r > 0

    def _update_run_button_state():
        """
        Single source of truth for whether the Run button may be
        pressed. Disabled (with an explanatory status message) until a
        Land Parcel source, a Road Network source, a valid positive
        buffer radius, and an Output destination are all present.

        The cascade below intentionally mirrors on_run()'s own
        validation order further down -- conscious duplication for a
        minimal-risk, additive gating layer, not a refactor of on_run()
        itself. Keep the two in sync if this tool's required inputs
        ever change.

        Explicit bg/fg/cursor toggling (not just state=) is required:
        Tkinter does NOT automatically gray out a classic tk.Button's
        custom bg/fg when state="disabled", and does not suppress a
        widget's assigned cursor either -- both must be set explicitly
        for each state.
        """
        has_parcel = bool(parcel_local_paths) if parcel_source_type.get() == "local" else bool(parcel_db_tables)
        has_road = bool(road_local_path.get()) if road_source_type.get() == "local" else bool(road_db_table.get())
        has_output = bool(output_local_dir.get()) if output_dest_type.get() == "local" else True
        buffer_ok = _is_valid_buffer(buffer_var.get())

        if not has_parcel:
            run_status_var.set("Please select a Land Parcel source.")
            ready = False
        elif not has_road:
            run_status_var.set("Please select a Road Network source.")
            ready = False
        elif not buffer_ok:
            run_status_var.set("Please enter a valid buffer radius.")
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

    # Live-updates the Run button as the user types in the buffer
    # radius field, without requiring focus-out or Enter.
    buffer_var.trace_add("write", lambda *_: _update_run_button_state())

    _toggle_parcel()
    _toggle_road()
    _toggle_output()
    _update_run_button_state()


# ---------------- Main Processing ----------------
def run_processing(overwrite_mode=None):
    # overwrite_mode: "overwrite", "new", or None (no conflict existed).
    # Resolved ONCE, up front, on the main thread in on_run() before
    # win.destroy() -- passed here as a parameter (not a global) for
    # the same reason app_root is passed as a parameter in other tools.
    # See ask_overwrite_dialog() for the full behavior contract.
    global barangay_source, road_source, output_mode, buffer_size
    if not barangay_source or not road_source or not output_mode or buffer_size is None:
        messagebox.showerror("Error", "Selections incomplete.")
        return

    creds = load_db_credentials()
    schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    )

    road_gdf = (
        gpd.read_file(road_source[1][0]) if road_source[0] == "local"
        else read_postgis_clean(road_source[1][0], engine, schema)
    )

    if barangay_source[0] == "local":
        for path in barangay_source[1]:
            brgy_gdf = gpd.read_file(path)
            brgy_gdf["geometry"] = brgy_gdf["geometry"].apply(fix_geometry)
            # output_column_name: preserves the exact existing column
            # name/casing this LOCAL source's parcel layer already had
            # (if the user confirmed overwriting one at Run time -- see
            # on_run()'s confirmation dialog). A source with no entry
            # here falls back to process_density()'s own default
            # ("CAMA_DENS_ROAD").
            output_column_name = density_column_overrides.get(path, "CAMA_DENS_ROAD")
            result = process_density(brgy_gdf, road_gdf, os.path.basename(path),
                                      output_column_name=output_column_name)
            if output_mode[0] == "local":
                # Desired output filename = the Land Parcel source's own
                # name, unchanged -- no tool-name suffix appended (matching
                # road_width.py / road_frontage.py's established convention).
                # overwrite_mode was resolved ONCE, up front in on_run(),
                # for the whole batch -- no per-file prompt here.
                base = os.path.splitext(os.path.basename(path))[0]
                desired_base_name = base
                candidate_path = os.path.join(output_mode[1], f"{desired_base_name}.gpkg")
                had_conflict = os.path.exists(candidate_path)
                if had_conflict and overwrite_mode == "new":
                    base_name = resolve_output_base_name(output_mode[1], desired_base_name)
                else:
                    # No conflict, or user chose "Overwrite" --
                    # both cases use the plain desired name.
                    base_name = desired_base_name
                out = os.path.join(output_mode[1], f"{base_name}.gpkg")
                result.to_file(out, driver="GPKG")
                print(f"✅ Saved {out}")
                load_in_global_mapper(out)
            else:
                local_name = os.path.splitext(os.path.basename(path))[0]
                match = find_matching_table(local_name, schema)
                table = match if match else local_name.lower()
                result.to_postgis(table, engine, schema=schema,
                                  if_exists="replace", index=False)
                print(f"🔄 Saved to DB: {table}")
    else:
        # Database Land Parcel sources: column-conflict check is out of
        # scope (see _check_parcel_density_conflicts()) -- always uses
        # process_density()'s default "CAMA_DENS_ROAD" name.
        for table in barangay_source[1]:
            brgy_gdf = read_postgis_clean(table, engine, schema)
            brgy_gdf["geometry"] = brgy_gdf["geometry"].apply(fix_geometry)
            result = process_density(brgy_gdf, road_gdf, table)
            if output_mode[0] == "local":
                # DB parcel source: table name used as desired base name
                # directly (no .splitext() needed). Same overwrite/create-new
                # logic as the local parcel source branch above.
                desired_base_name = table
                candidate_path = os.path.join(output_mode[1], f"{desired_base_name}.gpkg")
                had_conflict = os.path.exists(candidate_path)
                if had_conflict and overwrite_mode == "new":
                    base_name = resolve_output_base_name(output_mode[1], desired_base_name)
                else:
                    base_name = desired_base_name
                out = os.path.join(output_mode[1], f"{base_name}.gpkg")
                result.to_file(out, driver="GPKG")
                print(f"✅ Saved {out}")
                load_in_global_mapper(out)
            else:
                result.to_postgis(table, engine, schema=schema,
                                  if_exists="replace", index=False)
                print(f"🔄 Updated DB table: {table}")

    messagebox.showinfo("Success", "Processing done!")


# ---------------- Main ----------------
def main(parent=None):
    if parent is not None:
        open_main_window(parent)
    else:
        root = tk.Tk()
        apply_icon(root)
        root.withdraw()
        open_main_window(root)
        root.mainloop()


if __name__ == "__main__":
    main()