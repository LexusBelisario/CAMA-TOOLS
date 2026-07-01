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
def open_progress_window(parent=None):
    global progress_window, progress_label
    progress_window = tk.Toplevel(parent)
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


# REPLACE WITH

# ========================= GLOBAL MAPPER =========================
def load_in_global_mapper(filepath):
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
    win.title("Terrain Analysis Tool")
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
    dtm_source_type    = tk.StringVar(value="local")
    output_dest_type   = tk.StringVar(value="local")

    parcel_local_paths = []
    parcel_db_tables   = []
    road_local_path    = tk.StringVar()
    road_db_table      = tk.StringVar()
    dtm_local_path     = tk.StringVar()
    dtm_db_table       = tk.StringVar()
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

    parcel_db_frame = tk.Frame(parcel_frame)
    parcel_db_label = tk.StringVar(value="No table(s) selected")
    tk.Label(parcel_db_frame, textvariable=parcel_db_label,
             fg="gray", anchor="w", width=42).pack(side="left")

    def browse_parcel_db():
        creds = load_db_credentials()
        if not creds:
            return
        from sqlalchemy import create_engine as ce
        eng = ce(f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}")
        tables = inspect(eng).get_table_names(schema=creds["schema"])
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

    road_db_frame = tk.Frame(road_frame)
    road_db_label = tk.StringVar(value="No table selected")
    tk.Label(road_db_frame, textvariable=road_db_label,
             fg="gray", anchor="w", width=42).pack(side="left")

    def browse_road_db():
        creds = load_db_credentials()
        if not creds:
            return
        from sqlalchemy import create_engine as ce
        eng = ce(f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}")
        tables = inspect(eng).get_table_names(schema=creds["schema"])
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

    dtm_local_frame = tk.Frame(dtm_frame)
    dtm_local_frame.pack(fill="x", pady=2)
    dtm_file_label = tk.StringVar(value="No file selected")
    tk.Label(dtm_local_frame, textvariable=dtm_file_label,
             fg="gray", anchor="w", width=42).pack(side="left")

    def browse_dtm_file():
        f = filedialog.askopenfilename(
            filetypes=[("GeoTIFF", "*.tif"),
                       ("All", "*.*")])
        if f:
            dtm_local_path.set(f)
            dtm_file_label.set(os.path.basename(f))

    tk.Button(dtm_local_frame, text="Browse…", width=10,
              command=browse_dtm_file).pack(side="left", **PAD)

    dtm_db_frame = tk.Frame(dtm_frame)
    dtm_db_label = tk.StringVar(value="No table selected")
    tk.Label(dtm_db_frame, textvariable=dtm_db_label,
             fg="gray", anchor="w", width=42).pack(side="left")

    def browse_dtm_db():
        creds = load_db_credentials()
        if not creds:
            return
        from sqlalchemy import create_engine as ce
        eng = ce(f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}")
        tables = inspect(eng).get_table_names(schema=creds["schema"])
        _pick_db_tables(win, tables, multi=False,
                        on_select=lambda sel: (
                            dtm_db_table.set(sel[0])
                            or dtm_db_label.set(sel[0])
                        ))

    tk.Button(dtm_db_frame, text="Select…", width=10,
              command=browse_dtm_db).pack(side="left", **PAD)

    def _toggle_dtm():
        if dtm_source_type.get() == "local":
            dtm_db_frame.pack_forget()
            dtm_local_frame.pack(fill="x", pady=2)
        else:
            dtm_local_frame.pack_forget()
            dtm_db_frame.pack(fill="x", pady=2)

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
        global barangay_source, road_source, dtm_source, output_mode

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

        win.destroy()
        run_processing(root)

    tk.Button(win, text="▶  Run Processing", command=on_run,
              bg="#2e7d32", fg="white",
              font=("Segoe UI", 10, "bold"),
              relief="flat", padx=16, pady=6).pack(pady=(4, 14))


# ========================= RUN =========================
def run_processing(app_root):
    open_progress_window(app_root)
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
        road_gdf = (
            gpd.read_file(road_source[1][0]) if road_source[0] == "local"
            else read_postgis_clean(road_source[1][0], engine, schema)
        )

        barangay_list = barangay_source[1]
        for idx, src in enumerate(barangay_list, 1):
            update_progress(f"Loading parcel {idx}/{len(barangay_list)}...")
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
                    f"PG:dbname={creds['database']} host={creds['host']} "
                    f"user={creds['username']} password={creds['password']} "
                    f"schema={schema} table={dtm_table} column=rast"
                )
                srid = get_raster_srid(engine, schema, dtm_table)
                dtm = reproject_raster_to_prs92(dtm_raw, target_epsg) \
                    if srid != target_epsg else dtm_raw

            result = process_parcels_fast(parcels, roads, dtm, parcels_crs)

            update_progress("Saving output...")
            if output_mode[0] == "local":
                out = os.path.join(output_mode[1], f"{name}_terrain.gpkg")
                result.to_file(out, driver="GPKG")
                print(f"✅ Saved: {out}")
                load_in_global_mapper(out)
            else:
                existing = [
                    t for t in inspect(engine).get_table_names(schema=schema)
                    if name.lower() in t.lower()
                ]
                out_table = existing[0] if existing else name + "_terrain"
                result.to_postgis(out_table, engine, schema=schema,
                                  if_exists="replace", index=False)
                print(f"✅ Saved to DB: {out_table}")

            update_progress(f"✅ Completed {name}")

        close_progress_window()
        messagebox.showinfo("Success", "✅ Terrain processing complete!")

    except Exception as e:
        close_progress_window()
        messagebox.showerror("Error", f"❌ Processing failed:\n{str(e)}")


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