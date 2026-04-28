import os
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
CREDENTIALS_FILE = "pg_credentials.json"

barangay_source = None
road_source = None
output_mode = None
buffer_size = None  # 🔹 global buffer size (meters)

# ---------------- CRS Utility ----------------
def get_prs92_zone(gdf):
    """Choose PRS92 zone EPSG by centroid longitude."""
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326)
    gdf_wgs84 = gdf.to_crs(epsg=4326)
    lon = gdf_wgs84.unary_union.centroid.x
    if lon < 118: return 3121
    elif lon < 120: return 3122
    elif lon < 122: return 3123
    elif lon < 124: return 3124
    else: return 3125

# ---------------- Geometry Fix ----------------
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
    try:
        with open(CREDENTIALS_FILE,"r") as f: 
            return json.load(f)
    except: 
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

# ---------------- Core Processing ----------------
def process_density(brgy_gdf, road_gdf, source_name=""):
    """Compute road density (m/m²) for each barangay polygon."""
    global buffer_size
    orig_crs = brgy_gdf.crs

    # ✅ Project both to correct PRS92 zone
    zone_epsg = get_prs92_zone(brgy_gdf)
    print(f"🌍 [{source_name}] Using PRS92 EPSG:{zone_epsg}")
    brgy_proj = brgy_gdf.to_crs(epsg=zone_epsg)
    road_proj = road_gdf.to_crs(epsg=zone_epsg)

    brgy_proj = brgy_proj[brgy_proj.geometry.type == "Polygon"]
    road_proj = road_proj[road_proj.geometry.type == "LineString"]

    brgy_proj["DENS_ROAD"] = 0.0
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
        brgy_proj.at[idx, "DENS_ROAD"] = dens

        print(f"🟡 Feature {idx}: Length={round(total_length,2)} m, Density={dens}")

    # ✅ Reproject back to original CRS
    if orig_crs:
        brgy_proj = brgy_proj.to_crs(orig_crs)

    return brgy_proj

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
            select_buffer_window(root)

    def save_db():
        global output_mode
        output_mode = ("db", None)
        win.destroy()
        select_buffer_window(root)

    btn_frame = tk.Frame(win)
    btn_frame.pack(padx=25, pady=10)

    tk.Button(
        btn_frame, text="Save to Local", width=18, command=save_local
    ).pack(side=tk.LEFT, padx=5)

    tk.Button(
        btn_frame, text="Save to Database", width=18, command=save_db
    ).pack(side=tk.LEFT, padx=5)


def select_buffer_window(root):
    win = tk.Toplevel(root)
    apply_icon(win)
    win.title("Buffer Size (meters)")
    win.resizable(False, False)

    frame = tk.Frame(win)
    frame.pack(padx=25, pady=15)

    tk.Label(
        frame,
        text="Enter buffer radius (meters):"
    ).pack(pady=(0, 5))

    entry = tk.Entry(frame, width=18, justify="center")
    entry.insert(0, "1000")
    entry.pack(pady=(0, 10))

    def submit():
        global buffer_size
        try:
            val = float(entry.get())
            if val <= 0:
                raise ValueError
            buffer_size = val
            win.destroy()
            run_processing()
        except ValueError:
            messagebox.showerror(
                "Invalid Input",
                "Please enter a positive number."
            )

    tk.Button(frame, text="OK", width=10, command=submit).pack()

# ---------------- Main Processing ----------------
def run_processing():
    global barangay_source, road_source, output_mode, buffer_size
    if not barangay_source or not road_source or not output_mode or buffer_size is None:
        messagebox.showerror("Error","Selections incomplete."); return

    creds=load_db_credentials(); schema=creds["schema"]
    engine=create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    )

    # Load road
    road_gdf = gpd.read_file(road_source[1][0]) if road_source[0]=="local" else read_postgis_clean(road_source[1][0],engine,schema)

    # Process barangays
    if barangay_source[0]=="local":
        for path in barangay_source[1]:
            brgy_gdf=gpd.read_file(path); brgy_gdf["geometry"]=brgy_gdf["geometry"].apply(fix_geometry)
            result=process_density(brgy_gdf,road_gdf,os.path.basename(path))
            if output_mode[0]=="local":
                out=os.path.join(output_mode[1],f"{os.path.splitext(os.path.basename(path))[0]}_road_density.shp")
                result.to_file(out); print(f"✅ Saved {out}")
            else:
                local_name=os.path.splitext(os.path.basename(path))[0]
                match=find_matching_table(local_name,schema); table=match if match else local_name.lower()
                result.to_postgis(table,engine,schema=schema,if_exists="replace",index=False)
                print(f"🔄 Saved to DB: {table}")
    else:
        for table in barangay_source[1]:
            brgy_gdf=read_postgis_clean(table,engine,schema); brgy_gdf["geometry"]=brgy_gdf["geometry"].apply(fix_geometry)
            result=process_density(brgy_gdf,road_gdf,table)
            if output_mode[0]=="local":
                out=os.path.join(output_mode[1],f"{table}_road_density.shp")
                result.to_file(out); print(f"✅ Saved {out}")
            else:
                result.to_postgis(table,engine,schema=schema,if_exists="replace",index=False)
                print(f"🔄 Updated DB table: {table}")

    messagebox.showinfo("Success","✅ Processing done!")

# ---------------- Main ----------------
def main():
    root = tk.Tk()
    apply_icon(root)
    root.withdraw()
    select_barangay_window(root)
    root.mainloop()

if __name__=="__main__":
    main()
