import os
import re
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox, ttk, StringVar
import geopandas as gpd
from shapely.geometry import LineString
from shapely.ops import unary_union, nearest_points
import math
import subprocess
import json
from sqlalchemy import create_engine, inspect, text
from shapely.validation import make_valid
from shapely.geometry import box
import psycopg2

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


# ========================= CRS UTILITY =========================
def get_prs92_zone(gdf):
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326)
    lon = gdf.union_all().centroid.x  # fixed to avoid deprecation
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


def fix_geometry(geom):
    if geom is None or geom.is_empty:
        return None
    try:
        if not geom.is_valid:
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


def normalize_name(name: str) -> str:
    return re.sub(r'[^a-z]', '', name.lower())


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


def find_matching_table(local_name, schema):
    all_tables = fetch_tables(schema)
    lname = normalize_name(local_name)
    for t in all_tables:
        tnorm = normalize_name(t)
        if lname in tnorm or tnorm in lname:
            return t
    return None


# ========================= PROGRESS WINDOW =========================
class ProgressWindow:
    def __init__(self, root, title="Processing"):
        self.win = tk.Toplevel(root)
        apply_icon(self.win)
        self.win.title(title)
        self.win.geometry("400x120")
        self.win.resizable(False, False)
        self.status_var = StringVar()
        self.status_var.set("Starting...")
        ttk.Label(self.win, textvariable=self.status_var, anchor="center").pack(pady=10)
        self.progress = ttk.Progressbar(self.win, orient="horizontal", mode="determinate", length=350)
        self.progress.pack(pady=10)
        self.win.attributes("-topmost", True)
        self.win.update()

        self.win.focus_force()
        self.win.lift()
        self.win.attributes("-topmost", True)
        self.win.after(100, lambda: self.win.attributes("-topmost", False))


    def update(self, message, value=None, maximum=None):
        self.status_var.set(message)
        if maximum is not None:
            self.progress["maximum"] = maximum
        if value is not None:
            self.progress["value"] = value
        self.win.update_idletasks()
        self.win.update()

    def close(self):
        self.win.destroy()


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
def process_frontage_single(brgy_gdf, road_gdf, source_name="", progress=None):
    original_crs = brgy_gdf.crs
    zone_epsg = get_prs92_zone(brgy_gdf)

    if progress:
        progress(f"Reprojecting {source_name} to EPSG:{zone_epsg}")

    brgy_gdf = brgy_gdf.to_crs(epsg=zone_epsg)
    road_gdf = road_gdf.to_crs(epsg=zone_epsg)

    if progress:
        progress("Preparing roads (union + buffer 10m)")

    # 🔧 Clean road geometries
    road_gdf = road_gdf.copy()
    road_gdf["geometry"] = road_gdf.geometry.apply(fix_geometry)
    road_gdf = road_gdf[road_gdf.geometry.notnull()]
    road_gdf = road_gdf[~road_gdf.geometry.is_empty]

    # ✂️ CLIP ROADS TO PARCEL EXTENT (CRITICAL)
    road_gdf = clip_roads_to_parcels(road_gdf, brgy_gdf, pad=50)

    if road_gdf.empty:
        raise RuntimeError("No road geometry near parcels after clipping")

    road_union = unary_union(road_gdf.geometry.values)
    road_buffer = road_union.buffer(10)

    frontage_lengths, depths, dwrs = [], [], []
    total = len(brgy_gdf)

    # ✅ iterate faster over geometry series
    geoms = brgy_gdf.geometry.values

    for i, geom_raw in enumerate(geoms, start=1):
        if progress and (i % 200 == 0 or i == 1 or i == total):
            progress(f"Processing {source_name}: {i}/{total}", i, total)

        geom = fix_geometry(geom_raw)
        if geom is None:
            frontage_lengths.append(0.0)
            depths.append(0.0)
            dwrs.append(0.0)
            continue

        boundary = geom.boundary

        # ✅ FRONTAGE: chord distance (straight-line) between the outermost
        # boundary nodes intersecting the road buffer.
        # Per BLGF MAG: "Reduce the irregular lot to the nearest equivalent
        # rectangular, triangular and trapezoidal sectors."
        # QA methodology (Global Mapper): point-to-point measure of the two
        # outermost road-facing nodes — this is the Effective Frontage, not
        # the arc/perimeter length of the jagged cadastral boundary.
        try:
            inter = boundary.intersection(road_buffer)
            if inter.is_empty:
                frontage_total = 0.0
            else:
                _fl = _longest_linestring(inter)
                if _fl is None or len(_fl.coords) < 2:
                    frontage_total = 0.0
                else:
                    _c = list(_fl.coords)
                    from shapely.geometry import Point as _Point
                    frontage_total = _Point(_c[0]).distance(_Point(_c[-1]))
        except Exception:
            frontage_total = 0.0

        frontage_lengths.append(frontage_total)

        if frontage_total > 0:
            # pick the longest frontage piece to define direction for perpendicular depth
            frontage_ls = _longest_linestring(inter)
            if frontage_ls is None or frontage_ls.length == 0:
                depth_val = 0.0
            else:
                # reuse your perpendicular logic, but on the longest frontage line
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

                    # choose the ray that intersects deeper
                    i1 = line1.intersection(geom)
                    i2 = line2.intersection(geom)
                    len1 = i1.length if not i1.is_empty else 0.0
                    len2 = i2.length if not i2.is_empty else 0.0
                    depth_val = max(len1, len2)

            dwr_val = round(depth_val / frontage_total, 2) if frontage_total else 0.0
        else:
            # ✅ FAST fallback: distance to road union (no loop over each road feature)
            try:
                depth_val = geom.centroid.distance(road_union)
            except Exception:
                depth_val = 0.0
            dwr_val = depth_val

        depths.append(depth_val)
        dwrs.append(dwr_val)
    
    # 🔒 Safety check (prevents silent column mismatch)
    if not (len(frontage_lengths) == len(brgy_gdf) == len(depths) == len(dwrs)):
        raise RuntimeError("Attribute length mismatch during frontage processing")


    brgy_gdf["ROAD_FRONTAGE"] = frontage_lengths
    brgy_gdf["DEPTH"] = depths
    brgy_gdf["DEPTH_WIDTH_RATIO"] = dwrs

    if original_crs:
        brgy_gdf = brgy_gdf.to_crs(original_crs)

    if progress:
        progress(f"Finished {source_name}", total, total)

    return brgy_gdf


# ========================= MAIN PROCESS =========================
def run_processing(app_root):
    global barangay_source, road_source, output_mode
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

    progress = ProgressWindow(app_root, "Road Frontage Progress")

    q = queue.Queue()

    def worker():
        try:
            q.put(("update", "Loading road data...", None, None))

            def progress_cb(msg, val=None, maxv=None):
                q.put(("update", msg, val, maxv))


            if road_source[0] == "local":
                road_gdf = gpd.read_file(road_source[1][0])
            else:
                road_table = road_source[1][0]
                road_gdf = read_postgis_clean(road_table, engine, schema)

            if barangay_source[0] == "local":
                sources = [("local", p) for p in barangay_source[1]]
            else:
                sources = [("db", t) for t in barangay_source[1]]

            for src_type, src in sources:
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

                brgy_gdf = process_frontage_single(
                    brgy_gdf,
                    road_gdf,
                    name,
                    progress=progress_cb
                )


                if output_mode[0] == "local":
                    out = os.path.join(
                        output_mode[1],
                        f"{out_base}_road_frontage.gpkg"
                    )
                    brgy_gdf.to_file(out, driver="GPKG")
                    q.put(("open_gm", out, None, None))
                else:
                    brgy_gdf.to_postgis(
                        out_base,
                        engine,
                        schema=schema,
                        if_exists="replace",
                        index=False
                    )



            q.put(("done", "✅ Processing complete!", None, None))

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
                    progress.update(msg[1], 100, 100)
                    messagebox.showinfo("Success", "✅ Processing done!")
                    progress.close()
                    return

                elif kind == "error":
                    messagebox.showerror("Error", msg[1])
                    progress.close()
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


def open_main_window(root):
    win = tk.Toplevel(root)
    apply_icon(win)
    win.title("Road Frontage Tool")
    win.resizable(False, False)
    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))

    # ── state ────────────────────────────────────────────────────
    parcel_source_type = tk.StringVar(value="local")
    road_source_type   = tk.StringVar(value="local")
    output_dest_type   = tk.StringVar(value="local")

    parcel_local_paths = []
    parcel_db_tables   = []
    road_local_path    = tk.StringVar()
    road_db_table      = tk.StringVar()
    output_local_dir   = tk.StringVar()

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

    # local sub-frame
    parcel_local_frame = tk.Frame(parcel_frame)
    parcel_local_frame.pack(fill="x", pady=2)

    parcel_files_var = tk.StringVar(value="No file(s) selected")
    tk.Label(parcel_local_frame, textvariable=parcel_files_var,
             fg="gray", anchor="w", width=42).pack(side="left")

    def browse_parcel_files():
        files = filedialog.askopenfilenames(
            filetypes=[("Shapefiles", "*.shp"),
                       ("GeoPackage", "*.gpkg"),
                       ("All", "*.*")])
        if files:
            parcel_local_paths.clear()
            parcel_local_paths.extend(files)
            parcel_files_var.set(f"{len(files)} file(s) selected")

    tk.Button(parcel_local_frame, text="Browse…", width=10,
              command=browse_parcel_files).pack(side="left", **PAD)

    # db sub-frame
    parcel_db_frame  = tk.Frame(parcel_frame)
    parcel_db_label  = tk.StringVar(value="No table(s) selected")
    tk.Label(parcel_db_frame, textvariable=parcel_db_label,
             fg="gray", anchor="w", width=42).pack(side="left")

    def browse_parcel_db():
        creds = load_db_credentials()
        if not creds:
            messagebox.showerror("Error", "Could not load DB credentials.")
            return
        tables = fetch_tables(creds["schema"])
        _pick_db_tables(win, tables, multi=True,
                        on_select=lambda sel: (
                            parcel_db_tables.__setitem__(slice(None), sel)
                            or parcel_db_label.set(f"{len(sel)} table(s) selected")
                        ))

    tk.Button(parcel_db_frame, text="Select…", width=10,
              command=browse_parcel_db).pack(side="left", **PAD)

    def _toggle_parcel():
        if parcel_source_type.get() == "local":
            parcel_db_frame.pack_forget()
            parcel_local_frame.pack(fill="x", pady=2)
        else:
            parcel_local_frame.pack_forget()
            parcel_db_frame.pack(fill="x", pady=2)

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

    # local sub-frame
    road_local_frame = tk.Frame(road_frame)
    road_local_frame.pack(fill="x", pady=2)

    road_file_label = tk.StringVar(value="No file selected")
    tk.Label(road_local_frame, textvariable=road_file_label,
             fg="gray", anchor="w", width=42).pack(side="left")

    def browse_road_file():
        f = filedialog.askopenfilename(
            filetypes=[("Shapefiles", "*.shp"),
                       ("GeoPackage", "*.gpkg"),
                       ("All", "*.*")])
        if f:
            road_local_path.set(f)
            road_file_label.set(os.path.basename(f))

    tk.Button(road_local_frame, text="Browse…", width=10,
              command=browse_road_file).pack(side="left", **PAD)

    # db sub-frame
    road_db_frame = tk.Frame(road_frame)
    road_db_label = tk.StringVar(value="No table selected")
    tk.Label(road_db_frame, textvariable=road_db_label,
             fg="gray", anchor="w", width=42).pack(side="left")

    def browse_road_db():
        creds = load_db_credentials()
        if not creds:
            messagebox.showerror("Error", "Could not load DB credentials.")
            return
        tables = fetch_tables(creds["schema"])
        _pick_db_tables(win, tables, multi=False,
                        on_select=lambda sel: (
                            road_db_table.set(sel[0])
                            or road_db_label.set(sel[0])
                        ))

    tk.Button(road_db_frame, text="Select…", width=10,
              command=browse_road_db).pack(side="left", **PAD)

    def _toggle_road():
        if road_source_type.get() == "local":
            road_db_frame.pack_forget()
            road_local_frame.pack(fill="x", pady=2)
        else:
            road_local_frame.pack_forget()
            road_db_frame.pack(fill="x", pady=2)

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

    # local sub-frame
    output_local_frame = tk.Frame(output_frame)
    output_local_frame.pack(fill="x", pady=2)

    tk.Label(output_local_frame, textvariable=output_local_dir,
             fg="gray", anchor="w", width=42).pack(side="left")

    def browse_output_dir():
        d = filedialog.askdirectory()
        if d:
            output_local_dir.set(d)

    tk.Button(output_local_frame, text="Browse…", width=10,
              command=browse_output_dir).pack(side="left", **PAD)

    # db sub-frame
    output_db_frame = tk.Frame(output_frame)
    tk.Label(output_db_frame,
             text="Will write back to the connected PostGIS schema.",
             fg="gray", font=("Segoe UI", 8, "italic")).pack(anchor="w", pady=4)

    def _toggle_output():
        if output_dest_type.get() == "local":
            output_db_frame.pack_forget()
            output_local_frame.pack(fill="x", pady=2)
        else:
            output_local_frame.pack_forget()
            output_db_frame.pack(fill="x", pady=2)

    # ── RUN BUTTON ───────────────────────────────────────────────
    ttk.Separator(win, orient="horizontal").pack(
        fill="x", padx=10, pady=(12, 4))

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

        win.destroy()
        if _app_root is None:
            messagebox.showerror("Error", "No root window available. Please restart the tool.")
            return
        run_processing(_app_root)

    tk.Button(win, text="▶  Run Processing", command=on_run,
              bg="#2e7d32", fg="white",
              font=("Segoe UI", 10, "bold"),
              relief="flat", padx=16, pady=6).pack(pady=(4, 14))


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