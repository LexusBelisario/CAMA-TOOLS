import os
import re
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox, ttk
import geopandas as gpd
import pandas as pd
import osmnx as ox
import networkx as nx
from shapely.geometry import Point, LineString, box
from geopy.distance import geodesic
from sqlalchemy import create_engine, inspect, text
import subprocess
import json
import sys
import psycopg2

# --- CONFIG ---
ICON_PATH = r"D:/2025_PROJECTS/BLGF-GM_TEST/FOR TESTING/DCS_CODES/BLGF.ico"
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"


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

    # Tk titlebar fallback (important)
    if os.path.exists(png):
        try:
            img = tk.PhotoImage(file=png)
            win.iconphoto(True, img)
            win._icon_ref = img  # prevent garbage collection
        except Exception:
            pass


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

# --- GLOBALS ---
barangay_source = None
poi_source = None
output_mode = None
radius_meters = 200  # default
APP_ROOT = None
PROG_WIN = None
PROG_BAR = None
PROG_LABEL = None
PROG_STOP_FLAG = {"stop": False}

# ========================= EXISTING OUTPUT-COLUMN CONFLICT DETECTION =========================
# OUTPUT_COLUMN_TARGETS: this tool's four output column names, checked
# for pre-existing conflicts in a selected LOCAL Land Parcel source (see
# _check_parcel_poi_conflicts() below, and the combined dialog in
# on_run()). Mirrors road_frontage.py's / terrain.py's /
# land_shape_compactness.py's OUTPUT_COLUMN_TARGETS exactly: ALL four
# are checked, not just one -- they are one feature set computed
# together in the same run, so a source with (for example) an existing
# CAMA_NUM_POLICE column but no existing CAMA_NUM_PARK column still
# needs a conflict warning, to avoid ending up with an old
# CAMA_NUM_PARK value sitting alongside a freshly-computed
# CAMA_NUM_POLICE from a DIFFERENT run/computation -- an inconsistent,
# misleading combination.
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
OUTPUT_COLUMN_TARGETS = (
    "CAMA_NUM_POLICE", "CAMA_NUM_PARK", "CAMA_NUM_MALL", "CAMA_NUM_OTHERS",
)

# parcel_output_column_overrides: {path: {"CAMA_NUM_POLICE": name, ...}} --
# for any LOCAL Land Parcel source where one or more pre-existing
# CAMA_-prefixed output columns were detected (see
# _check_parcel_poi_conflicts() below) and the user confirmed
# proceeding at Run time. Read by run_processing() and resolved into
# the four individual *_col keyword arguments passed to
# process_poi_counts() -- matches the exact same override-storage-as-
# dict / function-signature-as-individual-kwargs split already
# established in terrain.py, road_frontage.py, and
# land_shape_compactness.py, so the tool writes back into the EXACT
# existing column(s) (preserving original casing) instead of always
# writing hardcoded "CAMA_*" names. A source with no entry here (or a
# target missing from its entry) uses that target's default CAMA_ name.
# Scope: LOCAL sources only -- Database Land Parcel sources are
# explicitly out of scope for this check.
parcel_output_column_overrides = {}


ox.settings.use_cache = True
ox.settings.log_console = False

# ---------------- DB HELPERS ----------------
def load_db_credentials():
    path = _get_credentials_path()
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        messagebox.showerror("Error", "Database credentials not found.")
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
    query = f'SELECT {col_str + "," if col_str else ""}"{geom_col}" AS geometry FROM "{schema}"."{table}"'
    return gpd.read_postgis(query, engine, geom_col="geometry")

def open_in_global_mapper(path):
    if os.path.exists(GM_EXE_PATH) and os.path.exists(path):
        subprocess.Popen([GM_EXE_PATH, path], shell=True)

def normalize_name(name: str) -> str:
    """Remove all non-alphabetic characters and convert to lowercase."""
    return re.sub(r'[^a-z]', '', name.lower())

def fetch_tables(schema):
    """Fetch all table names from the database schema."""
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
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=%s ORDER BY table_name;",
            (schema,)
        )
        tables = [row[0] for row in cur.fetchall()]
        conn.close()
        return tables
    except Exception as e:
        print(f"⚠️ Error fetching tables: {e}")
        return []

def find_matching_table(local_name, schema):
    """Find a database table that matches the local file name by substring."""
    all_tables = fetch_tables(schema)
    lname = normalize_name(local_name)
    
    for t in all_tables:
        tnorm = normalize_name(t)
        if lname in tnorm or tnorm in lname:
            return t
    return None


def create_progress_window(root, total, title="Processing Parcels"):
    global PROG_WIN, PROG_BAR, PROG_LABEL, PROG_STOP_FLAG

    try:
        if PROG_WIN and PROG_WIN.winfo_exists():
            PROG_WIN.destroy()
    except:
        pass

    PROG_STOP_FLAG = {"stop": False}

    PROG_WIN = tk.Toplevel(root)
    PROG_WIN.title(title)
    PROG_WIN.geometry("420x150")
    PROG_WIN.resizable(False, False)

    PROG_WIN.update_idletasks()
    sw = PROG_WIN.winfo_screenwidth()
    x = (sw // 2) - 210
    PROG_WIN.geometry(f"420x150+{x}+80")

    PROG_LABEL = tk.Label(PROG_WIN, text=f"0 / {total} parcels processed", anchor="w")
    PROG_LABEL.pack(fill="x", padx=12, pady=(12, 6))

    PROG_BAR = ttk.Progressbar(PROG_WIN, orient="horizontal",
                                mode="determinate", maximum=total)
    PROG_BAR.pack(fill="x", padx=12, pady=(0, 6))

    def on_cancel():
        PROG_STOP_FLAG["stop"] = True
        PROG_LABEL.config(text="Cancelling... please wait")

    tk.Button(PROG_WIN, text="Cancel", command=on_cancel,
              width=12).pack(pady=(4, 10))

    # Block X button from just destroying — treat it as cancel
    PROG_WIN.protocol("WM_DELETE_WINDOW", on_cancel)

    PROG_WIN.transient(root)
    PROG_WIN.grab_set()
    PROG_WIN.attributes("-topmost", True)
    PROG_WIN.update_idletasks()
    PROG_WIN.update()


def update_progress(current, total, msg=None):
    global PROG_WIN, PROG_BAR, PROG_LABEL, PROG_STOP_FLAG
    if not PROG_WIN or not PROG_WIN.winfo_exists():
        return False

    if PROG_STOP_FLAG.get("stop"):
        return False  # ← signal to stop

    PROG_BAR["value"] = current
    if msg:
        PROG_LABEL.config(text=f"{current} / {total} parcels processed — {msg}")
    else:
        PROG_LABEL.config(text=f"{current} / {total} parcels processed")

    if current == 1 or current == total or current % 5 == 0:
        PROG_WIN.update_idletasks()

    return True  # ← continue


def close_progress_window():
    global PROG_WIN
    try:
        if PROG_WIN and PROG_WIN.winfo_exists():
            PROG_WIN.grab_release()
            PROG_WIN.destroy()
    except:
        pass
    PROG_WIN = None


def _detect_existing_output_columns(gdf):
    """
    Checks a parcel GeoDataFrame for pre-existing columns matching any of
    OUTPUT_COLUMN_TARGETS (CAMA_NUM_POLICE, CAMA_NUM_PARK, CAMA_NUM_MALL,
    CAMA_NUM_OTHERS), exact match (case-insensitive) -- "cama_num_police"
    matches "CAMA_NUM_POLICE", but a column like "CAMA_NUM_POLICE_OLD" or
    "num_police" does NOT match (no substring/partial matching, and no
    matching against the old, unprefixed, lowercase names -- see
    OUTPUT_COLUMN_TARGETS' own docstring).

    Mirrors road_frontage.py's / terrain.py's / land_shape_compactness.py's
    _detect_existing_output_columns() exactly.

    Returns a dict {target_name: actual_existing_column_name}, containing
    ONLY the targets that actually have a match. Empty dict if none of
    the four targets have any existing column. The actual column's
    ORIGINAL casing is preserved in the returned value -- this is what
    gets shown to the user in the confirmation dialog and what
    process_poi_counts() writes back into, so an existing
    differently-cased column is reused exactly as found rather than
    renamed or duplicated.
    """
    found = {}
    for target in OUTPUT_COLUMN_TARGETS:
        match = next((c for c in gdf.columns if c.lower() == target.lower()), None)
        if match is not None:
            found[target] = match
    return found


# ========================= PARCEL COLUMN-CONFLICT CHECK =========================
# _check_parcel_poi_conflicts(): checks LOCAL Land Parcel source(s) for
# pre-existing columns matching any of OUTPUT_COLUMN_TARGETS -- this
# tool is about to write its four computed POI-count columns into those
# columns, and on_run() below shows a combined confirmation dialog
# before proceeding.
#
# Unlike road_frontage.py/road_width.py, this tool has no background
# worker thread -- run_processing() runs synchronously on the main
# thread (on_run() validates, destroys the window, then calls
# run_processing() directly; the modal progress window during
# processing is driven by direct update_progress() calls inside the
# synchronous loop, not a queue-polling pattern). So this check also
# runs synchronously, called directly from on_run() right before Run
# actually starts -- same adaptation already applied in
# road_density.py, road_surface.py, terrain.py, and
# land_shape_compactness.py. Adding threading here would be a separate,
# out-of-scope architectural change.
#
# Read approach: plain gpd.read_file(path), matching road_width.py's own
# canonical _read_gdf_worker() exactly -- no partial/schema-only read
# trick.
#
# A read failure here is NEVER treated as a column-conflict failure --
# it only skips the conflict check for that one source (logged to
# console). The real read inside run_processing() further below remains
# solely responsible for surfacing any genuine read error to the user.
#
# Scope: LOCAL sources only. Database Land Parcel sources are
# explicitly out of scope for this check per the project task
# definition -- callers must not invoke this for "db"-mode sources.
def _check_parcel_poi_conflicts(local_paths):
    """
    Returns a list of (path, existing_output_cols) tuples -- one entry
    only for local sources where at least one OUTPUT_COLUMN_TARGETS
    match was found. existing_output_cols is the dict returned by
    _detect_existing_output_columns() for that source (target name ->
    actual existing column name, original casing preserved).
    """
    conflicts = []
    for path in local_paths:
        try:
            gdf = gpd.read_file(path)
        except Exception as e:
            print(f"⚠️ Could not read parcel layer to check for existing "
                  f"output column(s): {path}: {e}")
            continue
        existing_output_cols = _detect_existing_output_columns(gdf)
        if existing_output_cols:
            conflicts.append((path, existing_output_cols))
    return conflicts


# ---------------- Output filename helpers ----------------
def _split_trailing_number(base_name: str):
    m = re.match(r'^(.*)_(\d+)$', base_name)
    if m:
        return m.group(1), int(m.group(2))
    return base_name, None


def resolve_output_base_name(folder: str, desired_base_name: str, ext: str = "gpkg") -> str:
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



# ---------------- MAIN PROCESSING ----------------
def process_poi_counts(gdf, poi_gdf, radius_m, progress_cb=None,
                        num_police_col="CAMA_NUM_POLICE", num_park_col="CAMA_NUM_PARK",
                        num_mall_col="CAMA_NUM_MALL", num_others_col="CAMA_NUM_OTHERS"):
    """
    num_police_col, num_park_col, num_mall_col, num_others_col : str --
    the column names this tool's four computed POI counts are written
    to. Each defaults to its standard CAMA_-prefixed, ALL_CAPS name
    (this tool's normal output, matching road_width.py's own
    ROAD_WIDTH -> CAMA_ROAD_WIDTH convention). The GUI overrides these
    per-source when the selected LOCAL parcel layer already has
    existing matching columns (see OUTPUT_COLUMN_TARGETS /
    _detect_existing_output_columns()) -- the exact existing
    name/casing is passed here so processing writes back into that same
    column instead of creating a hardcoded CAMA_-prefixed duplicate.
    """
    print(f"🚀 Starting POI count processing (radius = {radius_m} meters)...")

    # Preserve the parcel layer's original CRS so the final output can
    # be reprojected back to it before returning. EPSG:4326 below is
    # only the working CRS for the geodesic() distance calculations --
    # not the intended CRS of the saved output. Captured now, before
    # gdf gets reprojected. Used ONLY at the final return -- every
    # intermediate step (geodesic(), graph_from_polygon(), coordinate
    # extraction, the bbox pre-filter) keeps using EPSG:4326 unchanged.
    original_crs = gdf.crs

    gdf = gdf.to_crs(4326)
    poi_gdf = poi_gdf.to_crs(4326)

    # Ensure lowercase field names
    poi_gdf["fclass"] = poi_gdf["fclass"].astype(str).str.lower()

    # Add output fields
    gdf[num_police_col] = 0
    gdf[num_park_col] = 0
    gdf[num_mall_col] = 0
    gdf[num_others_col] = 0

    minx, miny, maxx, maxy = gdf.total_bounds
    bbox_poly = box(minx - 0.05, miny - 0.05, maxx + 0.05, maxy + 0.05)

    print("🌐 Downloading OSM road network within bounds...")
    try:
        G = ox.graph_from_polygon(bbox_poly, network_type='drive')
    except Exception as e:
        print(f"❌ Failed to download OSM data: {e}")
        if original_crs is not None:
            gdf = gdf.to_crs(original_crs)
        return gdf

    def add_virtual_node(G, point, node_id):
        try:
            u, v, key = ox.distance.nearest_edges(G, point[1], point[0])
            edge_data = G.get_edge_data(u, v)[key]
            line = edge_data.get('geometry', LineString([
                (G.nodes[u]['x'], G.nodes[u]['y']),
                (G.nodes[v]['x'], G.nodes[v]['y'])
            ]))
            proj_point = line.interpolate(line.project(Point(point[1], point[0])))
            coords = (proj_point.y, proj_point.x)
            G.add_node(node_id, x=coords[1], y=coords[0])
            d_u = geodesic((G.nodes[u]['y'], G.nodes[u]['x']), coords).meters
            d_v = geodesic((G.nodes[v]['y'], G.nodes[v]['x']), coords).meters
            G.add_edge(u, node_id, 0, length=d_u)
            G.add_edge(node_id, u, 0, length=d_u)
            G.add_edge(v, node_id, 0, length=d_v)
            G.add_edge(node_id, v, 0, length=d_v)
            return node_id
        except Exception as e:
            return None
        
    
    # NOTE (Part A3 investigation, resolved as NOT needed): same
    # centroid-only pattern already confirmed safe elsewhere in this
    # project (road_density.py, terrain.py, POI_All_Distance.py) --
    # only row.geometry.centroid is read from each parcel below, never
    # the full polygon via buffer/intersection/union. No
    # fix_geometry() added.
    total = len(gdf)
    for idx, row in gdf.iterrows():
        centroid = row.geometry.centroid
        lat, lon = centroid.y, centroid.x
        start_node = add_virtual_node(G, (lat, lon), f"start_{idx}")
        if not start_node:
            continue

        # Filter POIs within bounding box (rough prefilter)
        bbox = centroid.buffer(0.02).bounds
        subset = poi_gdf.cx[bbox[0]:bbox[2], bbox[1]:bbox[3]]

        police = park = mall = others = 0

        for _, poi in subset.iterrows():
            poi_lat, poi_lon = poi.geometry.y, poi.geometry.x
            fclass = poi["fclass"].lower()

            # fallback quick check (geodesic)
            dist = geodesic((lat, lon), (poi_lat, poi_lon)).meters
            if dist > radius_m:
                continue

            end_node = add_virtual_node(G, (poi_lat, poi_lon), f"end_{idx}_{_}")
            if not end_node:
                continue
            try:
                if nx.has_path(G, start_node, end_node):
                    length, _ = nx.bidirectional_dijkstra(G, start_node, end_node, weight='length')
                    if length <= radius_m:
                        if fclass == "police":
                            police += 1
                        elif fclass == "park":
                            park += 1
                        elif fclass == "mall":
                            mall += 1
                        else:
                            others += 1
                G.remove_node(end_node)
            except:
                continue

        gdf.at[idx, num_police_col] = police
        gdf.at[idx, num_park_col] = park
        gdf.at[idx, num_mall_col] = mall
        gdf.at[idx, num_others_col] = others

        if start_node in G:
            G.remove_node(start_node)

        print(f"✅ Feature {idx+1}: {police} police, {park} park, {mall} mall, {others} others")

        if progress_cb:
            should_continue = progress_cb(
                idx + 1,
                total,
                msg=f"P:{police} Park:{park} Mall:{mall} O:{others}"
            )
            if should_continue is False:
                print("⛔ Processing cancelled by user.")
                if original_crs is not None:
                    gdf = gdf.to_crs(original_crs)
                return gdf

    if original_crs is not None:
        gdf = gdf.to_crs(original_crs)
    return gdf

# REPLACE WITH

# ---------------- LOAD IN GLOBAL MAPPER ----------------
def load_in_global_mapper(filepath):
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


# ---------------- DB TABLE PICKER ----------------
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


# ---------------- MAIN WINDOW ----------------
def open_main_window(root):
    win = tk.Toplevel(root)
    win.title("POI Count Tool")
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
    output_dest_type   = tk.StringVar(master=win, value="local")

    # Single-selection architecture: one local file and one DB table
    # may exist in memory at any time. Authority variables -- all GUI
    # labels and run-button state are derived from them, never the reverse.
    parcel_local_path = None   # authority: single local file path
    parcel_db_table   = None   # authority: single DB table name
    poi_local_path     = tk.StringVar(master=win)
    poi_db_table       = tk.StringVar(master=win)
    output_local_dir   = tk.StringVar(master=win)
    radius_var         = tk.StringVar(master=win, value="200")

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
            _update_run_button_state()

    def _on_poi_db_selected(sel):
        # _pick_db_tables only ever invokes on_select with a non-empty
        # sel (see its submit(): "if sel: on_select(sel)"), so no
        # empty-selection branch is needed here.
        poi_db_table.set(sel[0])
        poi_db_var.set(sel[0])
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
        _update_run_button_state()

    # ── SECTION 3: SEARCH RADIUS ─────────────────────────────────
    section_label(win, "Search Radius")

    radius_frame = tk.Frame(win)
    radius_frame.pack(fill="x", padx=18, pady=2)
    tk.Label(radius_frame, text="Radius (meters):",
             anchor="w").pack(side="left")
    tk.Entry(radius_frame, textvariable=radius_var,
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
        global barangay_source, poi_source, output_mode, radius_meters

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

        # validate radius
        try:
            radius_meters = float(radius_var.get())
            if radius_meters <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid Input",
                "Please enter a valid positive number for the radius.")
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

        # ------------------------------------------------------------------
        # PRIORITY 1: column conflict check -- warn if the selected Land
        # Parcel source already has any of the 5 output columns
        # (CAMA_NUM_POLICE, CAMA_NUM_PARK, CAMA_NUM_MALL,
        # CAMA_NUM_OTHERS, CAMA_CHURCH). Shown before the file-conflict
        # dialog so the user can decide whether to proceed at all before
        # being asked about filename conflicts. Declining cancels the
        # run entirely; main window stays open. LOCAL sources only --
        # Database Land Parcel sources are explicitly out of scope.
        # ------------------------------------------------------------------
        global parcel_output_column_overrides
        if parcel_source_type.get() == "local":
            # parcel_local_path is guaranteed non-None here -- validation
            # above already returned if it was falsy.
            conflicts = _check_parcel_poi_conflicts([parcel_local_path])
            if conflicts:
                lines = "\n".join(
                    f"- '{os.path.basename(path)}': found "
                    + ", ".join(
                        f"'{existing_name}' column (for {target})"
                        for target, existing_name in existing_output_cols.items()
                    )
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
                # exactly -- e.g. a detected "caMA_NUM_POLICE" is
                # written back to "caMA_NUM_POLICE", not a hardcoded
                # "CAMA_NUM_POLICE" -- so no duplicate column is ever
                # created regardless of the existing casing. A source
                # with no entry here (no conflict was found) simply
                # uses the default names in process_poi_counts() below.
                parcel_output_column_overrides = dict(conflicts)
            else:
                parcel_output_column_overrides = {}
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

        win.destroy()
        run_processing(root, overwrite_mode)

    # Single source of truth for the Run button's enabled/disabled
    # colors -- used both at button creation and inside
    # _update_run_button_state() below, so there's only one place to
    # change if the theme changes later.
    RUN_BTN_BG_ENABLED  = "#2e7d32"
    RUN_BTN_FG_ENABLED  = "white"
    RUN_BTN_BG_DISABLED = "#e0e0e0"
    RUN_BTN_FG_DISABLED = "#888888"

    def _is_valid_radius(value):
        """
        Same acceptance rule on_run() already applies (float, > 0) --
        used here only to gate the Run button, not to clamp or
        auto-correct radius_var itself.
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
        Land Parcel source, a POI source, a valid positive search
        radius, and an Output destination are all present.

        Explicit bg/fg/cursor toggling (not just state=) is required:
        Tkinter does NOT automatically gray out a classic tk.Button's
        custom bg/fg when state="disabled", and does not suppress a
        widget's assigned cursor either -- both must be set explicitly
        for each state.
        """
        has_parcel = bool(parcel_local_path) if parcel_source_type.get() == "local" else bool(parcel_db_table)
        has_poi = bool(poi_local_path.get()) if poi_source_type.get() == "local" else bool(poi_db_table.get())
        has_output = bool(output_local_dir.get()) if output_dest_type.get() == "local" else True
        radius_ok = _is_valid_radius(radius_var.get())

        if not has_parcel:
            run_status_var.set("Please select a Land Parcel source.")
            ready = False
        elif not has_poi:
            run_status_var.set("Please select a POI source.")
            ready = False
        elif not radius_ok:
            run_status_var.set("Please enter a valid search radius.")
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

    # Live-updates the Run button as the user types in the radius
    # field, without requiring focus-out or Enter.
    radius_var.trace_add("write", lambda *_: _update_run_button_state())

    _toggle_parcel()
    _toggle_poi()
    _toggle_output()
    _update_run_button_state()


# ---------------- RUN PROCESSING ----------------
def run_processing(app_root, overwrite_mode=None):
    global barangay_source, poi_source, output_mode, radius_meters

    if not barangay_source or not poi_source or not output_mode:
        messagebox.showerror("Error", "Selections incomplete.")
        return

    creds = load_db_credentials()
    if not creds:
        return

    schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    )

    print("\n🔷 Loading POI data...")
    if poi_source[0] == "local":
        poi_gdf = gpd.read_file(poi_source[1][0])
    else:
        poi_gdf = read_postgis_clean(poi_source[1][0], engine, schema)
    print(f"✅ Loaded {len(poi_gdf)} POIs")

    if barangay_source[0] == "local":
        for path in barangay_source[1]:
            base_name = os.path.splitext(os.path.basename(path))[0]
            print(f"\n🔷 Processing: {base_name}")
            gdf = gpd.read_file(path)

            # Preserves each source's existing output column name(s)/
            # casing exactly, if a conflict was detected and confirmed
            # in on_run() -- e.g. a detected "caMA_NUM_POLICE" is
            # written back to "caMA_NUM_POLICE", not a hardcoded
            # "CAMA_NUM_POLICE". Defaults to the standard
            # CAMA_-prefixed name for any output this source has no
            # override for.
            output_col_overrides = parcel_output_column_overrides.get(path, {})
            num_police_col = output_col_overrides.get("CAMA_NUM_POLICE", "CAMA_NUM_POLICE")
            num_park_col = output_col_overrides.get("CAMA_NUM_PARK", "CAMA_NUM_PARK")
            num_mall_col = output_col_overrides.get("CAMA_NUM_MALL", "CAMA_NUM_MALL")
            num_others_col = output_col_overrides.get("CAMA_NUM_OTHERS", "CAMA_NUM_OTHERS")

            create_progress_window(app_root, len(gdf), title=f"Processing: {base_name}")
            result = process_poi_counts(gdf, poi_gdf, radius_meters,
                                        progress_cb=update_progress,
                                        num_police_col=num_police_col, num_park_col=num_park_col,
                                        num_mall_col=num_mall_col, num_others_col=num_others_col)
            close_progress_window()

            if output_mode[0] == "local":
                desired_base_name = base_name
                candidate_path = os.path.join(output_mode[1], f"{desired_base_name}.gpkg")
                had_conflict = os.path.exists(candidate_path)
                if had_conflict and overwrite_mode == "new":
                    base_name = resolve_output_base_name(output_mode[1], desired_base_name)
                else:
                    base_name = desired_base_name
                out = os.path.join(output_mode[1], f"{base_name}.gpkg")
                result.to_file(out, driver="GPKG")
                print(f"✅ Saved: {out}")
                load_in_global_mapper(out)
            else:
                matched_table = find_matching_table(base_name, schema)
                output_table = matched_table if matched_table else base_name.lower()
                result.to_postgis(output_table, engine, schema=schema,
                                  if_exists="replace", index=False)
                print(f"✅ Saved to DB: {output_table}")
    else:
        # Database Land Parcel sources: column-conflict check is out of
        # scope (see _check_parcel_poi_conflicts()) -- always uses
        # process_poi_counts()'s four default CAMA_-prefixed names.
        for table in barangay_source[1]:
            print(f"\n🔷 Processing DB table: {table}")
            gdf = read_postgis_clean(table, engine, schema)

            create_progress_window(app_root, len(gdf), title=f"Processing: {table}")
            result = process_poi_counts(gdf, poi_gdf, radius_meters,
                                        progress_cb=update_progress)
            close_progress_window()

            if output_mode[0] == "local":
                desired_base_name = table
                candidate_path = os.path.join(output_mode[1], f"{desired_base_name}.gpkg")
                had_conflict = os.path.exists(candidate_path)
                if had_conflict and overwrite_mode == "new":
                    base_name = resolve_output_base_name(output_mode[1], desired_base_name)
                else:
                    base_name = desired_base_name
                out = os.path.join(output_mode[1], f"{base_name}.gpkg")
                result.to_file(out, driver="GPKG")
                print(f"✅ Saved: {out}")
                load_in_global_mapper(out)
            else:
                result.to_postgis(table, engine, schema=schema,
                                  if_exists="replace", index=False)
                print(f"✅ Updated DB table: {table}")

    messagebox.showinfo("Success", "Processing complete!")


# ---------------- MAIN ----------------
def main(parent=None):
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