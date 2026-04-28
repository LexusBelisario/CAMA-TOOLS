import os
import pyproj
os.environ["PROJ_LIB"] = pyproj.datadir.get_data_dir()

import geopandas as gpd
import rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling
from shapely.geometry import LineString, MultiLineString, Point
from shapely.strtree import STRtree
import numpy as np
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox
import math
import json
from sqlalchemy import create_engine, inspect, text
from scipy.ndimage import sobel

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


CREDENTIALS_FILE = "pg_credentials.json"

# Globals
barangay_source = None
road_source = None
dtm_source = None
output_mode = None
progress_window = None
progress_label = None


# ---------------- Progress Window ----------------
def open_progress_window():
    global progress_window, progress_label
    progress_window = tk.Toplevel()
    apply_icon(progress_window)
    progress_window.title("Processing Progress")
    progress_window.geometry("320x100")
    progress_window.resizable(False, False)
    progress_label = tk.Label(progress_window, text="Starting...", wraplength=300)
    progress_label.pack(pady=20)
    progress_window.update()


def update_progress(msg):
    global progress_label, progress_window
    if progress_window and progress_label:
        progress_label.config(text=msg)
        progress_window.update()


def close_progress_window():
    global progress_window
    if progress_window:
        progress_window.destroy()
        progress_window = None


# ---------------- DB Helpers ----------------
def load_db_credentials():
    if not os.path.exists(CREDENTIALS_FILE):
        messagebox.showerror("Error", "Database credentials not found.")
        return None
    with open(CREDENTIALS_FILE, "r") as f:
        return json.load(f)


def get_geometry_column(table_name, engine, schema):
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT f_geometry_column
            FROM geometry_columns
            WHERE f_table_schema = :schema AND f_table_name = :table
        """), {"schema": schema, "table": table_name}).fetchone()
        return result[0] if result else None


def get_columns_except_geom(table_name, engine, schema, geom_col):
    insp = inspect(engine)
    return [c['name'] for c in insp.get_columns(table_name, schema=schema) if c['name'] != geom_col]


def get_raster_srid(engine, schema, table):
    with engine.connect() as conn:
        return conn.execute(text(
            f'SELECT ST_SRID(rast) FROM "{schema}"."{table}" LIMIT 1'
        )).scalar()


def read_postgis_clean(table, engine, schema):
    geom_col = get_geometry_column(table, engine, schema)
    cols = get_columns_except_geom(table, engine, schema, geom_col)
    col_str = ", ".join([f'"{c}"' for c in cols]) if cols else ""
    query = f'SELECT {col_str + "," if col_str else ""}"{geom_col}" AS geometry FROM "{schema}"."{table}"'
    return gpd.read_postgis(query, engine, geom_col="geometry")


# ---------------- CRS Helpers ----------------
def detect_prs92_zone(centroid):
    lon = centroid.x
    if 118 <= lon < 120: return 3121
    elif 120 <= lon < 122: return 3122
    elif 122 <= lon < 124: return 3123
    elif 124 <= lon < 126: return 3124
    else: return 3125


def reproject_raster_to_prs92(dtm, target_epsg):
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


# ---------------- Fast Terrain Processing ----------------
def compute_slope_array(dtm):
    """Compute slope in degrees for entire DTM using Sobel gradient."""
    update_progress("Precomputing slope from DTM...")
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


def process_parcels_fast(parcels, roads, dtm, parcels_crs):
    update_progress("Building road spatial index...")
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
    slope_arr = compute_slope_array(dtm)
    band1 = dtm.read(1)
    transform = dtm.transform

    update_progress("Sampling parcel elevations...")
    centroids = parcels.geometry.centroid
    coords = [(p.x, p.y) for p in centroids]
    prcl_elevs = [v[0] for v in dtm.sample(coords)]

    update_progress("Finding nearest road and road elevations...")
    nearest_segments = []
    for p in centroids:
        res = tree.nearest(p)
        if isinstance(res, (int, np.integer)):
            nearest_segments.append(segments[int(res)])
        else:
            nearest_segments.append(res)

    road_points = [seg.interpolate(0.5, normalized=True) for seg in nearest_segments]
    road_elevs = [v[0] for v in dtm.sample([(p.x, p.y) for p in road_points])]

    update_progress("Calculating slope and elevation differences...")
    diffs, slopes, terrains, topos = [], [], [], []

    for i, c in enumerate(centroids):
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

    parcels["SLOPE"] = slopes
    parcels["TERRAIN"] = terrains
    parcels["PRCL_ELEV"] = prcl_elevs
    parcels["ROAD_ELEV"] = road_elevs
    parcels["PRCL_ROAD"] = diffs
    parcels["TOPO_LVL"] = topos

    return parcels.to_crs(parcels_crs)


# ---------------- Run ----------------
def run_processing():
    open_progress_window()
    try:
        global barangay_source, road_source, dtm_source, output_mode
        creds = load_db_credentials()
        if not creds:
            close_progress_window()
            return
        schema = creds["schema"]
        engine = create_engine(
            f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
        )

        update_progress("Loading road data...")
        road_gdf = gpd.read_file(road_source[1][0]) if road_source[0] == "local" else read_postgis_clean(road_source[1][0], engine, schema)

        barangay_list = barangay_source[1]
        for idx, src in enumerate(barangay_list, 1):
            update_progress(f"Loading barangay {idx}/{len(barangay_list)}...")
            if barangay_source[0] == "local":
                parcels = gpd.read_file(src)
                name = os.path.splitext(os.path.basename(src))[0]
            else:
                parcels = read_postgis_clean(src, engine, schema)
                name = src

            centroid_ll = parcels.to_crs(4326).geometry.centroid.iloc[0]
            target_epsg = detect_prs92_zone(centroid_ll)
            parcels_crs = parcels.crs
            parcels = parcels.to_crs(epsg=target_epsg)
            roads = road_gdf.to_crs(epsg=target_epsg)

            update_progress("Loading DTM...")
            if dtm_source[0] == "local":
                dtm_raw = rasterio.open(dtm_source[1])
                dtm = reproject_raster_to_prs92(dtm_raw, target_epsg)
            else:
                dtm_table = dtm_source[1]
                dtm_raw = rasterio.open(
                    f"PG:dbname={creds['database']} host={creds['host']} user={creds['username']} password={creds['password']} schema={schema} table={dtm_table} column=rast"
                )
                srid = get_raster_srid(engine, schema, dtm_table)
                dtm = reproject_raster_to_prs92(dtm_raw, target_epsg) if srid != target_epsg else dtm_raw

            result = process_parcels_fast(parcels, roads, dtm, parcels_crs)

            update_progress("Saving output...")
            if output_mode[0] == "local":
                out = os.path.join(output_mode[1], f"{name}_terrain.shp")
                result.to_file(out)
            else:
                existing = [t for t in inspect(engine).get_table_names(schema=schema) if name.lower() in t.lower()]
                out_table = existing[0] if existing else name + "_terrain"
                result.to_postgis(out_table, engine, schema=schema, if_exists="replace", index=False)

            update_progress(f"✅ Completed {name}")

        close_progress_window()
        messagebox.showinfo("Success", "✅ Terrain processing complete!")

    except Exception as e:
        close_progress_window()
        messagebox.showerror("Error", f"❌ Processing failed:\n{str(e)}")


# ========================= UI WINDOWS =========================
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
        db_win.title("Select Parcel Table (DB)")
        lb = Listbox(db_win, selectmode=tk.MULTIPLE, width=55, height=15)
        for t in tables:
            lb.insert(tk.END, t)
        lb.pack()

        def submit():
            global barangay_source
            sel = [lb.get(i) for i in lb.curselection()]
            if sel:
                barangay_source = ("db", sel)
                db_win.destroy()
                win.destroy()
                select_road_window(root)

        tk.Button(db_win, text="Select", command=submit).pack(pady=5)

    btn_frame = tk.Frame(win)
    btn_frame.pack(padx=25, pady=10)

    tk.Button(
        btn_frame, text="Select Local File", width=18, command=pick_local
    ).pack(side=tk.LEFT, padx=5)

    tk.Button(
        btn_frame, text="Select Database Table", width=18, command=pick_db
    ).pack(side=tk.LEFT, padx=5)


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
            win.destroy()
            select_dtm_window(root)

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
                db_win.destroy()
                win.destroy()
                select_dtm_window(root)

        tk.Button(db_win, text="Select", command=submit).pack(pady=5)

    btn_frame = tk.Frame(win)
    btn_frame.pack(padx=25, pady=10)

    tk.Button(
        btn_frame, text="Select Local File", width=18, command=pick_local
    ).pack(side=tk.LEFT, padx=5)

    tk.Button(
        btn_frame, text="Select Database Table", width=18, command=pick_db
    ).pack(side=tk.LEFT, padx=5)


def select_dtm_window(root):
    win = tk.Toplevel(root)
    apply_icon(win)
    win.title("Select DTM Source")
    win.resizable(False, False)

    def pick_local():
        global dtm_source
        file = filedialog.askopenfilename(filetypes=[("TIFF", "*.tif")])
        if file:
            dtm_source = ("local", file)
            win.destroy()
            select_output_window(root)

    def pick_db():
        global dtm_source
        creds = load_db_credentials()
        engine = create_engine(
            f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
        )
        tables = inspect(engine).get_table_names(schema=creds["schema"])

        db_win = tk.Toplevel(root)
        apply_icon(db_win)
        db_win.title("Select DTM Table (DB)")
        lb = Listbox(db_win, selectmode=tk.SINGLE, width=55, height=15)
        for t in tables:
            lb.insert(tk.END, t)
        lb.pack()

        def submit():
            global dtm_source
            sel = [lb.get(i) for i in lb.curselection()]
            if sel:
                dtm_source = ("db", sel[0])
                db_win.destroy()
                win.destroy()
                select_output_window(root)

        tk.Button(db_win, text="Select", command=submit).pack(pady=5)

    btn_frame = tk.Frame(win)
    btn_frame.pack(padx=25, pady=10)

    tk.Button(
        btn_frame, text="Select Local File", width=18, command=pick_local
    ).pack(side=tk.LEFT, padx=5)

    tk.Button(
        btn_frame, text="Select Database Table", width=18, command=pick_db
    ).pack(side=tk.LEFT, padx=5)


def select_output_window(root):
    win = tk.Toplevel(root)
    apply_icon(win)
    win.title("Select Output Destination")
    win.resizable(False, False)

    def save_local():
        global output_mode
        out_dir = filedialog.askdirectory()
        if out_dir:
            output_mode = ("local", out_dir)
            win.destroy()
            run_processing()

    def save_db():
        global output_mode
        output_mode = ("db", None)
        win.destroy()
        run_processing()

    btn_frame = tk.Frame(win)
    btn_frame.pack(padx=25, pady=10)

    tk.Button(
        btn_frame, text="Save to Local", width=18, command=save_local
    ).pack(side=tk.LEFT, padx=5)

    tk.Button(
        btn_frame, text="Save to Database", width=18, command=save_db
    ).pack(side=tk.LEFT, padx=5)


# ---------------- Main ----------------
def main():
    root = tk.Tk()
    apply_icon(root)
    root.withdraw()
    select_barangay_window(root)
    root.mainloop()


if __name__ == "__main__":
    main()
