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

        # ✅ FAST frontage: no segment splitting, no within checks per segment
        # This returns the boundary portion that lies inside the 10m road buffer.
        try:
            inter = boundary.intersection(road_buffer)
            frontage_total = inter.length if not inter.is_empty else 0.0
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


    brgy_gdf["ROAD_FRONT"] = frontage_lengths
    brgy_gdf["DEPTH"] = depths
    brgy_gdf["DWR"] = dwrs

    if original_crs:
        brgy_gdf = brgy_gdf.to_crs(original_crs)

    if progress:
        progress(f"Finished {source_name}", total, total)

    return brgy_gdf


# ========================= MAIN PROCESS =========================
def run_processing():
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

    root = tk.Toplevel()
    root.withdraw()
    progress = ProgressWindow(root, "Road Frontage Progress")

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
                        f"{out_base}_road_frontage.shp"
                    )
                    brgy_gdf.to_file(out)
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

        root.after(100, poll_queue)

    threading.Thread(target=worker, daemon=True).start()
    poll_queue()


# ========================= MAIN APP =========================
def select_barangay_window(root):
    win = tk.Toplevel(root)
    apply_icon(win)
    win.title("Select Land Parcel Source")
    win.resizable(False, False)

    def pick_local():
        global barangay_source
        files = filedialog.askopenfilenames(filetypes=[("Shapefiles", "*.shp")])
        if files:
            barangay_source = ("local", files)
            print("✅ Barangay source set:", barangay_source)
            win.destroy()
            select_road_window(root)

    def pick_db():
        global barangay_source
        creds = load_db_credentials()
        engine = create_engine(
            f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
        )
        tables = inspect(engine).get_table_names(schema=creds["schema"])

        db_win = tk.Toplevel(root)
        apply_icon(db_win)
        db_win.title("Select Land Parcel Table (DB)")
        lb = Listbox(db_win, selectmode=tk.MULTIPLE, width=55, height=15)
        for t in tables:
            lb.insert(tk.END, t)
        lb.pack()

        def submit():
            global barangay_source
            sel = [lb.get(i) for i in lb.curselection()]
            if sel:
                barangay_source = ("db", sel)
                print("✅ Barangay source set:", barangay_source)
                db_win.destroy()
                win.destroy()
                select_road_window(root)

        tk.Button(db_win, text="Select", command=submit).pack(pady=5)

    btn_frame = tk.Frame(win)
    btn_frame.pack(padx=25, pady=10)

    tk.Button(btn_frame, text="Select Local File", width=18, command=pick_local)\
        .pack(side=tk.LEFT, padx=5)

    tk.Button(btn_frame, text="Select Database Table", width=18, command=pick_db)\
        .pack(side=tk.LEFT, padx=5)


def select_road_window(root):
    win = tk.Toplevel(root)
    apply_icon(win)
    win.title("Select Road Source")
    win.resizable(False, False)

    def pick_local():
        global road_source
        file = filedialog.askopenfilename(filetypes=[("Shapefiles", "*.shp")])
        if file:
            road_source = ("local", [file])
            print("✅ Road source set:", road_source)
            win.destroy()
            select_output_window(root)

    def pick_db():
        global road_source
        creds = load_db_credentials()
        engine = create_engine(
            f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
        )
        tables = inspect(engine).get_table_names(schema=creds["schema"])

        db_win = tk.Toplevel(root)
        apply_icon(db_win)
        db_win.title("Select Road Table (DB)")
        lb = Listbox(db_win, selectmode=tk.SINGLE, width=55, height=15)
        for t in tables:
            lb.insert(tk.END, t)
        lb.pack()

        def submit():
            global road_source
            sel = [lb.get(i) for i in lb.curselection()]
            if sel:
                road_source = ("db", sel)
                print("✅ Road source set:", road_source)
                db_win.destroy()
                win.destroy()
                select_output_window(root)

        tk.Button(db_win, text="Select", command=submit).pack(pady=5)

    btn_frame = tk.Frame(win)
    btn_frame.pack(padx=25, pady=10)

    tk.Button(btn_frame, text="Select Local File", width=18, command=pick_local)\
        .pack(side=tk.LEFT, padx=5)

    tk.Button(btn_frame, text="Select Database Table", width=18, command=pick_db)\
        .pack(side=tk.LEFT, padx=5)


def select_output_window(root):
    win = tk.Toplevel(root)
    apply_icon(win)
    win.title("Select Output Destination")
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

    btn_frame = tk.Frame(win)
    btn_frame.pack(padx=25, pady=10)

    tk.Button(btn_frame, text="Save to Local", width=18, command=save_local)\
        .pack(side=tk.LEFT, padx=5)

    tk.Button(btn_frame, text="Save to Database", width=18, command=save_db)\
        .pack(side=tk.LEFT, padx=5)


def main():
    root = tk.Tk()
    apply_icon(root)
    root.withdraw()
    select_barangay_window(root)
    root.mainloop()


if __name__ == "__main__":
    main()
