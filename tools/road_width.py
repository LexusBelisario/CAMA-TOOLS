root = None

import os
import re
import tkinter as tk
from tkinter import ttk
from tkinter import filedialog, messagebox, Listbox
import geopandas as gpd
from shapely.geometry import Point, LineString, MultiPolygon
from shapely.ops import nearest_points
from scipy.spatial import cKDTree
import subprocess
import json
from sqlalchemy import create_engine, text, inspect
import psycopg2
import statistics

# ----------------- CONFIG -----------------
GM_EXE_PATH = r"C:\\Program Files\\GlobalMapper26.1_64bit\\global_mapper.exe"
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

barangay_source = None
road_source = None
output_mode = None

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

def normalize_name(name: str) -> str:
    return re.sub(r'[^a-z]', '', name.lower())

def find_matching_table(local_name, schema):
    all_tables = fetch_tables(schema)
    lname = normalize_name(local_name)
    for t in all_tables:
        tnorm = normalize_name(t)
        if lname in tnorm or tnorm in lname:
            return t
    return None

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

def fix_geometry(geom):
    if geom is None:
        return None
    try:
        if not geom.is_valid:
            geom = geom.buffer(0)
        if isinstance(geom, MultiPolygon):
            return max(geom.geoms, key=lambda a: a.area)
        return geom
    except Exception:
        return None
    
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
def get_prs92_zone(gdf):
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326)
    lon = gdf.unary_union.centroid.x
    if lon < 118:
        return 3121
    elif lon < 120:
        return 3122
    elif lon < 122:
        return 3123
    elif lon < 124:
        return 3124
    else:
        return 3125

# ----------------- MAIN PROCESS -----------------
def process(barangay_gdf, road_gdf, source_name="", progress_cb=None):
    original_crs = barangay_gdf.crs
    barangay_gdf["geometry"] = barangay_gdf["geometry"].apply(fix_geometry)
    barangay_gdf = barangay_gdf[barangay_gdf["geometry"].notnull()]
    if barangay_gdf.empty:
        raise ValueError(f"All geometries invalid in {source_name}")

    zone_epsg = get_prs92_zone(barangay_gdf)
    print(f"🌍 [{source_name}] Reprojecting to EPSG:{zone_epsg}...")
    barangay_gdf = barangay_gdf.to_crs(epsg=zone_epsg)
    road_gdf = road_gdf.to_crs(epsg=zone_epsg)

    # Extract road segments
    segment_geoms, segment_midpoints = [], []
    for geom in road_gdf.geometry:
        if geom is None or geom.is_empty: 
            continue
        parts = geom.geoms if geom.geom_type in ['MultiLineString', 'GeometryCollection'] else [geom]
        for ls in parts:
            if ls.geom_type != "LineString": continue
            coords = list(ls.coords)
            for i in range(len(coords)-1):
                seg = LineString([coords[i], coords[i+1]])
                segment_geoms.append(seg)
                midpoint = seg.interpolate(0.5, normalized=True)
                segment_midpoints.append((midpoint.x, midpoint.y))

    if not segment_midpoints:
        if progress_cb:
            for _ in range(len(barangay_gdf)):
                progress_cb(1)

        barangay_gdf["ROAD_WIDTH"] = None
        if original_crs:
            barangay_gdf = barangay_gdf.to_crs(original_crs)
        return barangay_gdf

    tree = cKDTree(segment_midpoints)

    # Classifier: road buffer union (10m — same tolerance as lot_location.py)
    from shapely.ops import unary_union
    road_buffer_union = unary_union(road_gdf.geometry).buffer(10)

    # ------------------------------------------------------------------
    # PASS 1 — compute raw width + classify each parcel
    # Road lots  → raw width is valid (parcel boundary IS the road edge)
    # Inner lots → raw width is wrong (gap measurement); flagged for Pass 3
    # ------------------------------------------------------------------
    pass1 = []  # list of dicts: {raw_width, seg_idx, is_road_lot}

    for idx, poly in enumerate(barangay_gdf.geometry):
        if progress_cb:
            progress_cb(1)

        if poly is None or poly.is_empty:
            pass1.append({"raw_width": None, "seg_idx": None, "is_road_lot": False})
            continue

        if not poly.is_valid:
            poly = poly.buffer(0)
            if poly.is_empty:
                pass1.append({"raw_width": None, "seg_idx": None, "is_road_lot": False})
                continue

        boundary = poly.boundary
        coords = list(boundary.coords) if boundary.geom_type == "LineString" \
            else [c for g in boundary.geoms for c in g.coords]
        if not coords:
            pass1.append({"raw_width": None, "seg_idx": None, "is_road_lot": False})
            continue

        dists, indices = tree.query(coords)
        best = dists.argmin()
        seg_idx = int(indices[best])
        nearest_segment = segment_geoms[seg_idx]
        nearest_point = Point(coords[best])
        nearest_on_road = nearest_segment.interpolate(nearest_segment.project(nearest_point))
        nearest_on_feature = nearest_points(nearest_on_road, boundary)[1]
        raw_width = nearest_on_road.distance(nearest_on_feature) * 2

        is_road_lot = bool(poly.intersects(road_buffer_union))

        pass1.append({
            "raw_width": raw_width,
            "seg_idx": seg_idx,
            "is_road_lot": is_road_lot,
        })

    # ------------------------------------------------------------------
    # PASS 2 — build segment_id → median road width from road lots only
    # ------------------------------------------------------------------
    seg_widths: dict = {}  # seg_idx -> list of raw widths from road lots
    for record in pass1:
        if record["is_road_lot"] and record["raw_width"] is not None and record["seg_idx"] is not None:
            seg_widths.setdefault(record["seg_idx"], []).append(record["raw_width"])

    segment_width_map: dict = {
        seg_idx: statistics.median(widths)
        for seg_idx, widths in seg_widths.items()
    }

    # ------------------------------------------------------------------
    # PASS 3 — assemble final ROAD_WIDTH values
    # Road lots  → use their own raw_width (unchanged from existing logic)
    # Inner lots → inherit median width of road lots on the same segment;
    #              None if that segment has no road-touching parcels
    # ------------------------------------------------------------------
    road_widths = []
    for record in pass1:
        if record["raw_width"] is None:
            road_widths.append(None)
        elif record["is_road_lot"]:
            road_widths.append(record["raw_width"])
        else:
            # Inner lot: look up the width associated with its nearest segment
            road_widths.append(segment_width_map.get(record["seg_idx"], None))

    barangay_gdf["ROAD_WIDTH"] = road_widths
    if original_crs:
        barangay_gdf = barangay_gdf.to_crs(original_crs)
    return barangay_gdf

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

    parcel_local_paths = []
    parcel_db_tables   = []
    road_local_path    = tk.StringVar(master=win)
    output_local_dir   = tk.StringVar(master=win)

    parcel_files_var = tk.StringVar(master=win, value="No file(s) selected")
    parcel_db_var    = tk.StringVar(master=win, value="No table(s) selected")
    road_file_var    = tk.StringVar(master=win, value="No file selected")
    road_db_var      = tk.StringVar(master=win, value="No table selected")
    output_dir_var   = tk.StringVar(master=win, value="No folder selected")
    output_db_var    = tk.StringVar(master=win, value="Will write back to the connected PostGIS schema.")

    PAD = dict(padx=8, pady=4)

    # ── section label helper ─────────────────────────────────────
    def section_label(parent, text):
        frm = tk.Frame(parent)
        frm.pack(fill="x", padx=10, pady=(10, 2))
        tk.Label(frm, text=text, font=("Segoe UI", 9, "bold")).pack(side="left")
        ttk.Separator(frm, orient="horizontal").pack(
            side="left", fill="x", expand=True, padx=(6, 0), pady=4)

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

    parcel_btn = tk.Button(parcel_action_row, text="Browse…", width=10)
    parcel_btn.pack(side="left", **PAD)

    # ── parcel browse callbacks ───────────────────────────────────
    def browse_parcel_files():
        files = filedialog.askopenfilenames(filetypes=[
            ("Shapefiles", "*.shp"),
            ("GeoPackage", "*.gpkg"),
            ("All", "*.*")])
        if files:
            parcel_local_paths.clear()
            parcel_local_paths.extend(files)
            parcel_files_var.set(f"{len(files)} file(s) selected")

    def browse_parcel_db():
        creds = load_db_credentials()
        if not creds:
            messagebox.showerror("Error", "Could not load DB credentials.")
            return
        tables = fetch_tables(creds["schema"])
        if not tables:
            messagebox.showwarning("No Tables", "No tables found in the database schema.")
            return
        _pick_db_tables(win, tables, multi=True,
            on_select=lambda sel: (
                parcel_db_tables.__setitem__(slice(None), sel)
                or parcel_db_var.set(f"{len(sel)} table(s) selected")
            ))

    # ── parcel toggle ─────────────────────────────────────────────
    def toggle_parcel(*_):
        mode = parcel_source_type.get()
        if mode == "local":
            parcel_lbl_widget.config(textvariable=parcel_files_var,
                                     font=("Segoe UI", 9))
            parcel_btn.config(text="Browse…", command=browse_parcel_files)
        else:
            parcel_lbl_widget.config(textvariable=parcel_db_var,
                                     font=("Segoe UI", 9))
            parcel_btn.config(text="Select…", command=browse_parcel_db)

    # ── parcel radio buttons (command wired AFTER toggle defined) ─
    tk.Radiobutton(parcel_radio_row, text="Local File(s)",
                   variable=parcel_source_type, value="local",
                   command=toggle_parcel).pack(side="left")
    tk.Radiobutton(parcel_radio_row, text="Database Table(s)",
                   variable=parcel_source_type, value="db",
                   command=toggle_parcel).pack(side="left", padx=(12, 0))

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

    road_btn = tk.Button(road_action_row, text="Browse…", width=10)
    road_btn.pack(side="left", **PAD)

    # ── road browse callbacks ─────────────────────────────────────
    def browse_road_file():
        f = filedialog.askopenfilename(filetypes=[
            ("Shapefiles", "*.shp"),
            ("GeoPackage", "*.gpkg"),
            ("All", "*.*")])
        if f:
            road_local_path.set(f)
            road_file_var.set(os.path.basename(f))

    def browse_road_db():
        creds = load_db_credentials()
        if not creds:
            messagebox.showerror("Error", "Could not load DB credentials.")
            return
        tables = fetch_tables(creds["schema"])
        if not tables:
            messagebox.showwarning("No Tables", "No tables found in the database schema.")
            return
        _pick_db_tables(win, tables, multi=False,
            on_select=lambda sel: (
                road_local_path.set(sel[0]) if sel else None,
                road_db_var.set(sel[0] if sel else "No table selected")
            ))

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

    # ── road radio buttons ────────────────────────────────────────
    tk.Radiobutton(road_radio_row, text="Local File",
                   variable=road_source_type, value="local",
                   command=toggle_road).pack(side="left")
    tk.Radiobutton(road_radio_row, text="Database Table",
                   variable=road_source_type, value="db",
                   command=toggle_road).pack(side="left", padx=(12, 0))

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

    out_btn = tk.Button(out_action_row, text="Browse…", width=10)
    out_btn.pack(side="left", **PAD)

    # ── output browse callback ────────────────────────────────────
    def browse_output_dir():
        d = filedialog.askdirectory()
        if d:
            output_local_dir.set(d)
            output_dir_var.set(d)

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
            if not road_local_path.get():   # road_local_path reused for db table name
                messagebox.showerror("Missing Input",
                    "Please select a Road Network table.")
                return
            road_source = ("db", [road_local_path.get()])

        # validate output
        if output_dest_type.get() == "local":
            if not output_local_dir.get():
                messagebox.showerror("Missing Input",
                    "Please select an output folder.")
                return
            output_mode = ("local", output_local_dir.get())
        else:
            output_mode = ("db", None)

        win.destroy()
        run_processing()

    tk.Button(win, text="▶  Run Processing", command=on_run,
              bg="#2e7d32", fg="white", font=("Segoe UI", 10, "bold"),
              relief="flat", padx=16, pady=6).pack(pady=(4, 14))

    # ── apply initial toggle state so buttons have correct commands ──
    toggle_parcel()
    toggle_road()
    toggle_output()


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
            run_processing()

    def save_db():
        global output_mode, barangay_source, road_source
        if not barangay_source or not road_source:
            messagebox.showerror("Error", "Barangay and Road must be selected first.")
            return
        output_mode = ("db", None)
        print("✅ Output mode set:", output_mode)
        win.destroy()
        run_processing()

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

# ----------------- PROGRESS WINDOW -----------------
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
def run_processing():
    global barangay_source, road_source, output_mode
    if not barangay_source or not road_source or not output_mode:
        messagebox.showerror("Error","Selections incomplete (Barangay, Road, Output required).")
        return

    creds = load_db_credentials()
    if not creds:
        return
    schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    )

    # --- Load road layer ---
    if road_source[0] == "local":
        road_gdf = gpd.read_file(road_source[1][0])
    else:
        road_table = road_source[1][0]
        road_gdf = read_postgis_clean(road_table, engine, schema)

    # count total features (local or db)
    total_features = 0
    if barangay_source[0] == "local":
        for p in barangay_source[1]:
            total_features += len(gpd.read_file(p))
    else:
        for t in barangay_source[1]:
            total_features += len(read_postgis_clean(t, engine, schema))

    progress_win, progress_lbl, progress_bar, progress_count = create_progress_window(root, total_features)

    progress_win.transient(root)
    progress_win.grab_set()

    progress_win.update_idletasks()
    progress_win.deiconify()
    progress_win.lift()
    progress_win.focus_force()
    progress_win.attributes("-topmost", True)
    progress_win.after(100, lambda: progress_win.attributes("-topmost", False))

    current_step = 0

    def progress_cb(_):
        nonlocal current_step
        current_step += 1
        update_progress(
            progress_win,
            progress_lbl,
            progress_bar,
            progress_count,
            current_step,
            total_features,
            f"Processing feature {current_step}"
        )



    # --- Process barangays ---
    if barangay_source[0] == "local":
        for path in barangay_source[1]:
            b_gdf = gpd.read_file(path)
            b_gdf = process(b_gdf, road_gdf, os.path.basename(path), progress_cb)
            if output_mode[0] == "local":
                base_name = os.path.splitext(os.path.basename(path))[0]
                out = os.path.join(output_mode[1], f"{base_name}.gpkg")
                b_gdf.to_file(out, driver="GPKG")
                print(f"✅ Saved {out}")
                load_in_global_mapper(out)
            else:
                local_name = os.path.splitext(os.path.basename(path))[0]
                match = find_matching_table(local_name, schema)
                table_action = "replaced" if match else "new"
                table = match if match else local_name.lower()
                b_gdf.to_postgis(table, engine, schema=schema, if_exists="replace", index=False)
                print(f"🔄 Saved to DB: {table} ({table_action})")

                # ---------------- 🟢 CAMA Table + Transaction Log ----------------
                with engine.begin() as conn:
                    # Ensure CAMA_Table exists
                    conn.execute(text(f"""
                        CREATE TABLE IF NOT EXISTS "{schema}"."CAMA_Table" (
                            id SERIAL PRIMARY KEY,
                            PIN TEXT UNIQUE NOT NULL
                        );
                    """))

                    # Ensure column for road width
                    conn.execute(text(f"""
                        DO $$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1 FROM information_schema.columns
                                WHERE table_schema='{schema}'
                                  AND table_name='CAMA_Table'
                                  AND column_name='road_width'
                            ) THEN
                                EXECUTE 'ALTER TABLE "{schema}"."CAMA_Table" ADD COLUMN "road_width" NUMERIC';
                            END IF;
                        END $$;
                    """))

                    # Update or insert per PIN
                    pin_field = next((c for c in b_gdf.columns if c.lower() == "pin"), None)
                    if pin_field:
                        for _, row in b_gdf.iterrows():
                            sql = f"""
                                INSERT INTO "{schema}"."CAMA_Table" (PIN, road_width)
                                VALUES (:pin, :rw)
                                ON CONFLICT (PIN) DO UPDATE
                                SET road_width = EXCLUDED.road_width;
                            """
                            params = {
                                "pin": str(row[pin_field]),
                                "rw": float(row["ROAD_WIDTH"]) if row["ROAD_WIDTH"] is not None else None,
                            }
                            conn.execute(text(sql), params)

                    # Ensure log table exists
                    conn.execute(text(f"""
                        CREATE TABLE IF NOT EXISTS "{schema}"."CAMA_Transaction_Log" (
                            id SERIAL PRIMARY KEY,
                            table_name TEXT,
                            cama_tool TEXT,
                            cama_fields TEXT,
                            transaction_date_time TIMESTAMP DEFAULT NOW()
                        );
                    """))

                    # Log transaction with (new) or (replaced)
                    conn.execute(text(f"""
                        INSERT INTO "{schema}"."CAMA_Transaction_Log"
                        (table_name, cama_tool, cama_fields)
                        VALUES (:tbl, :type, :details);
                    """), {
                        "tbl": f"{table} ({table_action})",
                        "type": "road_width",
                        "details": "ROAD_WIDTH"
                    })

    else:  # --- barangay_source == "db" ---
        for table in barangay_source[1]:
            b_gdf = read_postgis_clean(table, engine, schema)
            b_gdf = process(b_gdf, road_gdf, table, progress_cb)
            if output_mode[0] == "local":
                out = os.path.join(output_mode[1], f"{table}.gpkg")
                b_gdf.to_file(out, driver="GPKG")
                print(f"✅ Saved {out}")
                load_in_global_mapper(out)
            else:
                # Check if table already exists before overwrite
                all_tables = fetch_tables(schema)
                table_action = "replaced" if table in all_tables else "new"

                b_gdf.to_postgis(table, engine, schema=schema, if_exists="replace", index=False)
                print(f"🔄 Updated DB table: {table} ({table_action})")

                # ---------------- 🟢 CAMA Table + Transaction Log ----------------
                with engine.begin() as conn:
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
                                  AND column_name='road_width'
                            ) THEN
                                EXECUTE 'ALTER TABLE "{schema}"."CAMA_Table" ADD COLUMN "road_width" NUMERIC';
                            END IF;
                        END $$;
                    """))
                    pin_field = next((c for c in b_gdf.columns if c.lower() == "pin"), None)
                    if pin_field:
                        for _, row in b_gdf.iterrows():
                            sql = f"""
                                INSERT INTO "{schema}"."CAMA_Table" (PIN, road_width)
                                VALUES (:pin, :rw)
                                ON CONFLICT (PIN) DO UPDATE
                                SET road_width = EXCLUDED.road_width;
                            """
                            params = {
                                "pin": str(row[pin_field]),
                                "rw": float(row["ROAD_WIDTH"]) if row["ROAD_WIDTH"] is not None else None,
                            }
                            conn.execute(text(sql), params)

                    conn.execute(text(f"""
                        CREATE TABLE IF NOT EXISTS "{schema}"."CAMA_Transaction_Log" (
                            id SERIAL PRIMARY KEY,
                            table_name TEXT,
                            cama_tool TEXT,
                            cama_fields TEXT,
                            transaction_date_time TIMESTAMP DEFAULT NOW()
                        );
                    """))
                    conn.execute(text(f"""
                        INSERT INTO "{schema}"."CAMA_Transaction_Log"
                        (table_name, cama_tool, cama_fields)
                        VALUES (:tbl, :type, :details);
                    """), {
                        "tbl": f"{table} ({table_action})",
                        "type": "road_width",
                        "details": "ROAD_WIDTH"
                    })
    progress_win.destroy()

    messagebox.showinfo("Success", "✅ Processing done and logged to CAMA!")


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