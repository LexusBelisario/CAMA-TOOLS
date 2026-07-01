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

    # Auto-detect surface column (case-insensitive)
    surface_col = next(
        (c for c in road_gdf.columns if c.lower() in ("surface", "surf", "road_surf", "rd_surface", "pavement")),
        None
    )
    if surface_col is None:
        messagebox.showerror(
            "Missing Column",
            f"Road layer has no 'surface' column.\n\n"
            f"Available columns: {', '.join(road_gdf.columns.tolist())}"
        )
        return brgy_gdf

    print(f"ℹ️ Using road surface column: '{surface_col}'")

    # Buffer the roads
    road_buffer = road_gdf.copy()
    road_buffer["geometry"] = road_gdf.buffer(10)

    brgy_gdf["RD_SURFACE"] = [[] for _ in range(len(brgy_gdf))]

    # Assign surfaces from intersecting roads
    for _, road in road_buffer.iterrows():
        surface_val = str(road.get(surface_col, "")).strip()
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
        nearest_surface = str(road_gdf.at[nearest_idx, surface_col]).strip()
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

# REPLACE WITH

# ========================= GLOBAL MAPPER =========================
def load_in_global_mapper(filepath):
    try:
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
    win.title("Road Surface Tool")
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
        engine = create_engine(
            f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
        )
        tables = inspect(engine).get_table_names(schema=creds["schema"])
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
            messagebox.showerror("Error", "Could not load DB credentials.")
            return
        engine = create_engine(
            f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
        )
        tables = inspect(engine).get_table_names(schema=creds["schema"])
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
        run_processing()

    tk.Button(win, text="▶  Run Processing", command=on_run,
              bg="#2e7d32", fg="white",
              font=("Segoe UI", 10, "bold"),
              relief="flat", padx=16, pady=6).pack(pady=(4, 14))


# ---------------- Run ----------------
def run_processing():
    global barangay_source, road_source, output_mode
    if not barangay_source or not road_source or not output_mode:
        messagebox.showerror("Error", "Selections incomplete.")
        return

    creds = load_db_credentials()
    schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    )

    if road_source[0] == "local":
        road_gdf = gpd.read_file(road_source[1][0])
    else:
        road_gdf = read_postgis_clean(road_source[1][0], engine, schema)

    if barangay_source[0] == "local":
        for path in barangay_source[1]:
            brgy_gdf = gpd.read_file(path)
            result = process_surface(brgy_gdf, road_gdf)
            if output_mode[0] == "local":
                base = os.path.splitext(os.path.basename(path))[0]
                out = os.path.join(output_mode[1], f"{base}_roadsurface.gpkg")
                result.to_file(out, driver="GPKG")
                print(f"✅ Saved {out}")
                load_in_global_mapper(out)
            else:
                base = os.path.splitext(os.path.basename(path))[0]
                existing = [t for t in inspect(engine).get_table_names(schema=schema)
                            if base.lower() in t.lower()]
                out_table = existing[0] if existing else base + "_roadsurface"
                result.to_postgis(out_table, engine, schema=schema,
                                  if_exists="replace", index=False)
                print(f"🔄 Saved to DB: {out_table}")
    else:
        for table in barangay_source[1]:
            brgy_gdf = read_postgis_clean(table, engine, schema)
            result = process_surface(brgy_gdf, road_gdf)
            if output_mode[0] == "local":
                out = os.path.join(output_mode[1], f"{table}_roadsurface.gpkg")
                result.to_file(out, driver="GPKG")
                print(f"✅ Saved {out}")
                load_in_global_mapper(out)
            else:
                existing = [t for t in inspect(engine).get_table_names(schema=schema)
                            if table.lower() in t.lower()]
                out_table = existing[0] if existing else table + "_roadsurface"
                result.to_postgis(out_table, engine, schema=schema,
                                  if_exists="replace", index=False)
                print(f"🔄 Saved to DB: {out_table}")

    messagebox.showinfo("Success", "✅ Processing complete!")


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