import os
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox
import geopandas as gpd
from shapely.geometry import Point
import subprocess
import json
from sqlalchemy import create_engine, inspect, text

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
            win._icon_ref = img  # prevent GC
        except Exception:
            pass


# === Paths ===
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"
CREDENTIALS_FILE = "pg_credentials.json"

# Globals
barangay_source = None
road_source = None
output_mode = None

# ---------------- CRS Helper ----------------
def get_prs92_zone(gdf):
    """Determine PRS92 zone based on centroid longitude."""
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326)  
    gdf_wgs84 = gdf.to_crs(epsg=4326)
    lon = gdf_wgs84.unary_union.centroid.x
    if lon < 118: return 3121
    elif lon < 120: return 3122
    elif lon < 122: return 3123
    elif lon < 124: return 3124
    else: return 3125

# ---------------- DB Helpers ----------------
def load_db_credentials():
    try:
        with open(CREDENTIALS_FILE, "r") as f:
            return json.load(f)
    except:
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
    if col_str:
        query = f'SELECT {col_str}, "{geom_col}" AS geometry FROM "{schema}"."{table}"'
    else:
        query = f'SELECT "{geom_col}" AS geometry FROM "{schema}"."{table}"'
    return gpd.read_postgis(query, engine, geom_col="geometry")

def open_in_global_mapper(path):
    if os.path.exists(GM_EXE_PATH) and os.path.exists(path):
        subprocess.Popen([GM_EXE_PATH, path], shell=True)

# ---------------- Processing ----------------
def process_surface(brgy_gdf, road_gdf):
    # Save original CRS
    orig_crs = brgy_gdf.crs

    # Temporary reproject to PRS92
    zone_epsg = get_prs92_zone(brgy_gdf)
    print(f"🌍 Reprojecting layers to EPSG:{zone_epsg} for processing...")
    brgy_gdf = brgy_gdf.to_crs(epsg=zone_epsg)
    road_gdf = road_gdf.to_crs(epsg=zone_epsg)

    # Buffer the roads
    road_buffer = road_gdf.copy()
    road_buffer["geometry"] = road_gdf.buffer(10)

    brgy_gdf["RD_SURFACE"] = [[] for _ in range(len(brgy_gdf))]

    # Assign surfaces from intersecting roads
    for _, road in road_buffer.iterrows():
        surface_val = str(road.get("surface", "")).strip()
        if not surface_val:
            continue
        intersect_mask = brgy_gdf.geometry.intersects(road.geometry)
        for idx in brgy_gdf[intersect_mask].index:
            if surface_val not in brgy_gdf.at[idx, "RD_SURFACE"]:
                brgy_gdf.at[idx, "RD_SURFACE"].append(surface_val)

    # Nearest road for those with no intersections
    no_surface_mask = brgy_gdf["RD_SURFACE"].apply(lambda x: len(x) == 0)
    for idx, row in brgy_gdf[no_surface_mask].iterrows():
        centroid: Point = row.geometry.centroid
        distances = road_gdf.distance(centroid)
        nearest_idx = distances.idxmin()
        nearest_surface = str(road_gdf.at[nearest_idx, "surface"]).strip()
        if nearest_surface:
            brgy_gdf.at[idx, "RD_SURFACE"] = [nearest_surface]

    # Convert list → slash-separated string
    brgy_gdf["RD_SURFACE"] = brgy_gdf["RD_SURFACE"].apply(
        lambda surfaces: "/".join(sorted(set(surfaces))) if surfaces else None
    )

    # Reproject back to original CRS
    if orig_crs:
        brgy_gdf = brgy_gdf.to_crs(orig_crs)

    return brgy_gdf

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

# ---------------- Run ----------------
def run_processing():
    global barangay_source, road_source, output_mode
    if not barangay_source or not road_source or not output_mode:
        messagebox.showerror("Error", "Selections incomplete.")
        return

    creds = load_db_credentials(); schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}")

    # Load road
    if road_source[0] == "local":
        road_gdf = gpd.read_file(road_source[1][0])
    else:
        road_table = road_source[1][0]
        road_gdf = read_postgis_clean(road_table, engine, schema)

    # Process barangays
    if barangay_source[0] == "local":
        for path in barangay_source[1]:
            brgy_gdf = gpd.read_file(path)
            result = process_surface(brgy_gdf, road_gdf)
            if output_mode[0] == "local":
                out = os.path.join(output_mode[1], f"{os.path.splitext(os.path.basename(path))[0]}_roadsurface.shp")
                result.to_file(out); open_in_global_mapper(out)
                print(f"✅ Saved {out}")
            else:
                base = os.path.splitext(os.path.basename(path))[0]
                existing = [t for t in inspect(engine).get_table_names(schema=schema) if base.lower() in t.lower()]
                out_table = existing[0] if existing else base + "_roadsurface"
                result.to_postgis(out_table, engine, schema=schema, if_exists="replace", index=False)
                print(f"🔄 Saved to DB table: {out_table}")
    else:
        for table in barangay_source[1]:
            brgy_gdf = read_postgis_clean(table, engine, schema)
            result = process_surface(brgy_gdf, road_gdf)
            if output_mode[0] == "local":
                out = os.path.join(output_mode[1], f"{table}_roadsurface.shp")
                result.to_file(out); open_in_global_mapper(out)
                print(f"✅ Saved {out}")
            else:
                existing = [t for t in inspect(engine).get_table_names(schema=schema) if table.lower() in t.lower()]
                out_table = existing[0] if existing else table + "_roadsurface"
                result.to_postgis(out_table, engine, schema=schema, if_exists="replace", index=False)
                print(f"🔄 Saved to DB table: {out_table}")

    messagebox.showinfo("Success", "✅ Processing complete!")

# ---------------- Main ----------------
def main():
    root = tk.Tk()
    apply_icon(root)
    root.withdraw()
    select_barangay_window(root)
    root.mainloop()

if __name__ == "__main__":
    main()
