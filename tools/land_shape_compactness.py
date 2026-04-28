import geopandas as gpd
import numpy as np
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox
from shapely.geometry import Polygon, MultiPolygon
import math
import os
import subprocess
import json
import psycopg2
from sqlalchemy import create_engine, inspect, text
from shapely.validation import make_valid
import re

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


# === Global Mapper EXE and Icon Paths
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"
CREDENTIALS_FILE = "pg_credentials.json"

barangay_source = None
output_mode = None

# ---------------- Geometry Fix ----------------
def fix_geometry(geom):
    if geom is None or geom.is_empty: return None
    try:
        if not geom.is_valid:
            geom = geom.buffer(0)
        if not geom.is_valid:
            geom = make_valid(geom)
        return geom if not geom.is_empty else None
    except:
        return None

# ---------------- Helpers ----------------
def angle_between(p1, p2, p3):
    v1 = (p1[0] - p2[0], p1[1] - p2[1])
    v2 = (p3[0] - p2[0], p3[1] - p2[1])
    dot = v1[0]*v2[0] + v1[1]*v2[1]
    det = v1[0]*v2[1] - v1[1]*v2[0]
    angle_rad = math.atan2(det, dot)
    angle_deg = math.degrees(angle_rad)
    return round(angle_deg + 360 if angle_deg < 0 else angle_deg, 2)

def vertex_angles(polygon: Polygon):
    coords = list(polygon.exterior.coords)
    if coords[0] == coords[-1]: coords = coords[:-1]
    cleaned = [coords[0]]
    for pt in coords[1:]:
        if math.dist(pt, cleaned[-1]) != 0:
            cleaned.append(pt)
    angles = []
    n = len(cleaned)
    if n < 3: return angles
    for i in range(n):
        p1, p2, p3 = cleaned[i-1], cleaned[i], cleaned[(i+1) % n]
        if math.dist(p2,p3)==0: continue
        angles.append(angle_between(p1,p2,p3))
    return angles

def classify_lot_shape(angles):
    low_angles = [a for a in angles if a <= 169]
    rightish   = [a for a in angles if 170 <= a <= 190]
    obtuse     = [a for a in angles if 190 < a < 260]
    l_cands    = [a for a in angles if 260 <= a <= 280]
    if len(l_cands)==1: return "L_SHAPED"
    elif len(l_cands)>1: return "OTHERS"
    if len(angles)==3 and len(low_angles)==3: return "TRIANGLE"
    if len(low_angles)==3 and len(obtuse)==0: return "TRIANGLE"
    if len(angles)==4 and len(low_angles)==4: return "RECTANGLE"
    elif len(angles)>4:
        if len(low_angles)==4 and all(170<=a<=190 for a in rightish) and len(obtuse)==0:
            return "RECTANGLE"
    return "OTHERS"

def largest_polygon(geom):
    if isinstance(geom, Polygon): return geom
    if isinstance(geom, MultiPolygon) and len(geom.geoms)>0:
        return max(geom.geoms, key=lambda g:g.area)
    return None

def auto_utm_epsg_from_gdf(gdf):
    if gdf.crs is None: gdf_tmp=gdf.set_crs(epsg=4326,allow_override=True)
    else: gdf_tmp=gdf.to_crs(epsg=4326)
    centroid=gdf_tmp.unary_union.centroid
    lon=centroid.x
    zone=int((lon+180)//6)+1
    return 32600+zone

# ---------------- Core ----------------
def compute_ppr_and_lot_shape_gdf(gdf):
    # Save the original CRS
    original_crs = gdf.crs

    # Ensure we work in projected CRS
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326, allow_override=True)
    if gdf.crs.is_geographic:
        epsg = auto_utm_epsg_from_gdf(gdf)
        gdf = gdf.to_crs(epsg=epsg)

    # ---- Do calculations in projected CRS ----
    area = gdf.geometry.area
    perimeter = gdf.geometry.length
    gdf["PP_RATIO"] = ((4 * np.pi * area) / (perimeter ** 2)).round(2)

    gdf["VTX_COUNT"] = 0
    gdf["ANGS_TXT"] = ""

    for col in ["TRIANGLE", "RECTANGLE", "L_SHAPED", "OTHERS"]:
        if col not in gdf.columns:
            gdf[col] = 0
    gdf["LOT_SHAPE"] = ""

    for idx, geom in enumerate(gdf.geometry):
        poly = largest_polygon(geom)
        if poly is None:
            gdf.at[idx, "OTHERS"] = 1
            gdf.at[idx, "LOT_SHAPE"] = "OTHERS"
            continue

        angles = vertex_angles(poly)
        shape_type = classify_lot_shape(angles)

        gdf.at[idx, "TRIANGLE"] = 0
        gdf.at[idx, "RECTANGLE"] = 0
        gdf.at[idx, "L_SHAPED"] = 0
        gdf.at[idx, "OTHERS"] = 0
        gdf.at[idx, shape_type] = 1
        gdf.at[idx, "LOT_SHAPE"] = shape_type
        gdf.at[idx, "VTX_COUNT"] = len(angles)
        gdf.at[idx, "ANGS_TXT"] = ",".join(map(str, angles))

    # ✅ Reproject back to original CRS before returning
    if original_crs:
        gdf = gdf.to_crs(original_crs)

    return gdf

def open_in_global_mapper(path):
    if os.path.exists(GM_EXE_PATH) and os.path.exists(path):
        subprocess.Popen([GM_EXE_PATH, path], shell=True)

# ---------------- DB Helpers ----------------
def load_db_credentials():
    try:
        with open(CREDENTIALS_FILE,"r") as f: return json.load(f)
    except: return None

def get_geometry_column(table, engine, schema):
    with engine.connect() as conn:
        row=conn.execute(text("""
            SELECT f_geometry_column FROM geometry_columns
            WHERE f_table_schema=:schema AND f_table_name=:table
        """),{"schema":schema,"table":table}).fetchone()
        return row[0] if row else None

def read_postgis_clean(table,engine,schema):
    geom_col=get_geometry_column(table,engine,schema)
    insp=inspect(engine)
    cols=[c['name'] for c in insp.get_columns(table,schema=schema) if c['name']!=geom_col]
    col_str=", ".join([f'"{c}"' for c in cols]) if cols else ""
    q=f'SELECT {col_str+", " if col_str else ""}"{geom_col}" AS geometry FROM "{schema}"."{table}"'
    return gpd.read_postgis(q,engine,geom_col="geometry")

def normalize_name(name): return re.sub(r'[^a-z]','',name.lower())

def fetch_tables(schema):
    creds=load_db_credentials()
    if not creds: return []
    try:
        conn=psycopg2.connect(
            host=creds["host"],port=creds["port"],
            dbname=creds["database"],user=creds["username"],password=creds["password"]
        )
        cur=conn.cursor()
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s;",(schema,))
        return [r[0] for r in cur.fetchall()]
    except: return []

def find_matching_table(local_name,schema):
    lname=normalize_name(local_name)
    for t in fetch_tables(schema):
        if lname in normalize_name(t) or normalize_name(t) in lname:
            return t
    return None

# ---------------- Tkinter Windows ----------------
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
            print("✅ Barangay (Local):", barangay_source)
            win.destroy()
            select_output_window(root)

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
                print("✅ Barangay (DB):", barangay_source)
                db_win.destroy()
                win.destroy()
                select_output_window(root)

        tk.Button(db_win, text="Select", command=submit).pack(pady=5)

    btn_frame = tk.Frame(win)
    btn_frame.pack(padx=25, pady=10)

    tk.Button(
        btn_frame,
        text="Select Local File",
        width=18,
        command=pick_local
    ).pack(side=tk.LEFT, padx=5)

    tk.Button(
        btn_frame,
        text="Select Database Table",
        width=18,
        command=pick_db
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
            print("✅ Output (Local):", output_mode)
            win.destroy()
            run_processing()

    def save_db():
        global output_mode
        output_mode = ("db", None)
        print("✅ Output (DB):", output_mode)
        win.destroy()
        run_processing()

    btn_frame = tk.Frame(win)
    btn_frame.pack(padx=25, pady=10)

    tk.Button(
        btn_frame,
        text="Save to Local",
        width=18,
        command=save_local
    ).pack(side=tk.LEFT, padx=5)

    tk.Button(
        btn_frame,
        text="Save to Database",
        width=18,
        command=save_db
    ).pack(side=tk.LEFT, padx=5)

# ---------------- Processing ----------------
def run_processing():
    global barangay_source, output_mode
    if not barangay_source or not output_mode:
        messagebox.showerror("Error","Selections incomplete (Barangay + Output required)."); return
    creds=load_db_credentials(); schema=creds["schema"]
    engine=create_engine(f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}")

    if barangay_source[0]=="local":
        for path in barangay_source[1]:
            gdf=gpd.read_file(path); gdf["geometry"]=gdf["geometry"].apply(fix_geometry)
            gdf=gdf[gdf["geometry"].notnull()]
            result=compute_ppr_and_lot_shape_gdf(gdf)
            if output_mode[0]=="local":
                out=os.path.join(output_mode[1],f"{os.path.splitext(os.path.basename(path))[0]}_lotshape.shp")
                result.to_file(out); open_in_global_mapper(out); print("✅ Saved",out)
            else:
                local_name=os.path.splitext(os.path.basename(path))[0]
                match=find_matching_table(local_name,schema); table=match if match else local_name.lower()
                result.to_postgis(table,engine,schema=schema,if_exists="replace",index=False)
                print("🔄 Saved to DB:",table)
    else:
        for table in barangay_source[1]:
            gdf=read_postgis_clean(table,engine,schema); gdf["geometry"]=gdf["geometry"].apply(fix_geometry)
            gdf=gdf[gdf["geometry"].notnull()]
            result=compute_ppr_and_lot_shape_gdf(gdf)
            if output_mode[0]=="local":
                out=os.path.join(output_mode[1],f"{table}_lotshape.shp")
                result.to_file(out); open_in_global_mapper(out); print("✅ Saved",out)
            else:
                result.to_postgis(table,engine,schema=schema,if_exists="replace",index=False)
                print("🔄 Updated DB table:",table)
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
