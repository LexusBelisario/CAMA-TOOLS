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


def load_in_global_mapper(filepath):
    try:
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

        import ctypes.wintypes
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        ctypes.windll.user32.EnumWindows(EnumWindowsProc(enum_callback), 0)

        subprocess.Popen([GM_EXE_PATH, filepath])
        print(f"🗺️ Sent to Global Mapper: {filepath}")

    except Exception as e:
        print(f"⚠️ Could not open in Global Mapper: {e}")


def open_main_window(root):
    from tkinter import ttk
    win = tk.Toplevel(root)
    apply_icon(win)
    win.title("Lot Shape Tool")
    win.resizable(False, False)
    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))

    # ── state ────────────────────────────────────────────────────
    parcel_source_type = tk.StringVar(value="local")
    output_dest_type   = tk.StringVar(value="local")

    parcel_local_paths = []
    parcel_db_tables   = []
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

    # ── SECTION 2: OUTPUT ────────────────────────────────────────
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
        global barangay_source, output_mode

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
              bg="#2e7d32", fg="white",
              font=("Segoe UI", 10, "bold"),
              relief="flat", padx=16, pady=6).pack(pady=(4, 14))


# ---------------- Processing ----------------
def run_processing():
    global barangay_source, output_mode
    if not barangay_source or not output_mode:
        messagebox.showerror("Error", "Selections incomplete (Barangay + Output required).")
        return

    creds = load_db_credentials()
    schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    )

    if barangay_source[0] == "local":
        for path in barangay_source[1]:
            gdf = gpd.read_file(path)
            gdf["geometry"] = gdf["geometry"].apply(fix_geometry)
            gdf = gdf[gdf["geometry"].notnull()]
            result = compute_ppr_and_lot_shape_gdf(gdf)
            if output_mode[0] == "local":
                base = os.path.splitext(os.path.basename(path))[0]
                out = os.path.join(output_mode[1], f"{base}_lotshape.gpkg")
                result.to_file(out, driver="GPKG")
                print(f"✅ Saved {out}")
                load_in_global_mapper(out)
            else:
                local_name = os.path.splitext(os.path.basename(path))[0]
                match = find_matching_table(local_name, schema)
                table = match if match else local_name.lower()
                result.to_postgis(table, engine, schema=schema, if_exists="replace", index=False)
                print(f"🔄 Saved to DB: {table}")
    else:
        for table in barangay_source[1]:
            gdf = read_postgis_clean(table, engine, schema)
            gdf["geometry"] = gdf["geometry"].apply(fix_geometry)
            gdf = gdf[gdf["geometry"].notnull()]
            result = compute_ppr_and_lot_shape_gdf(gdf)
            if output_mode[0] == "local":
                out = os.path.join(output_mode[1], f"{table}_lotshape.gpkg")
                result.to_file(out, driver="GPKG")
                print(f"✅ Saved {out}")
                load_in_global_mapper(out)
            else:
                result.to_postgis(table, engine, schema=schema, if_exists="replace", index=False)
                print(f"🔄 Updated DB table: {table}")

    messagebox.showinfo("Success", "✅ Processing done!")


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