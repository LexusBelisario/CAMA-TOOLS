import os
import re
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox, ttk
import geopandas as gpd
import pandas as pd
import osmnx as ox
import networkx as nx
from shapely.geometry import Point, LineString, box
from geopy.distance import geodesic
from sqlalchemy import create_engine, inspect, text
import subprocess
import json
import psycopg2

# --- CONFIG ---
ICON_PATH = r"D:/2025_PROJECTS/BLGF-GM_TEST/FOR TESTING/DCS_CODES/BLGF.ico"
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"
CREDENTIALS_FILE = "pg_credentials.json"

# --- GLOBALS ---
barangay_source = None
poi_source = None
output_mode = None
radius_meters = 200  # default
APP_ROOT = None
PROG_WIN = None
PROG_BAR = None
PROG_LABEL = None
PROG_STOP_FLAG = {"stop": False}


ox.settings.use_cache = True
ox.settings.log_console = False

# ---------------- DB HELPERS ----------------
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
    query = f'SELECT {col_str + "," if col_str else ""}"{geom_col}" AS geometry FROM "{schema}"."{table}"'
    return gpd.read_postgis(query, engine, geom_col="geometry")

def open_in_global_mapper(path):
    if os.path.exists(GM_EXE_PATH) and os.path.exists(path):
        subprocess.Popen([GM_EXE_PATH, path], shell=True)

def normalize_name(name: str) -> str:
    """Remove all non-alphabetic characters and convert to lowercase."""
    return re.sub(r'[^a-z]', '', name.lower())

def fetch_tables(schema):
    """Fetch all table names from the database schema."""
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
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=%s ORDER BY table_name;",
            (schema,)
        )
        tables = [row[0] for row in cur.fetchall()]
        conn.close()
        return tables
    except Exception as e:
        print(f"⚠️ Error fetching tables: {e}")
        return []

def find_matching_table(local_name, schema):
    """Find a database table that matches the local file name by substring."""
    all_tables = fetch_tables(schema)
    lname = normalize_name(local_name)
    
    for t in all_tables:
        tnorm = normalize_name(t)
        if lname in tnorm or tnorm in lname:
            return t
    return None


def create_progress_window(root, total, title="Processing Parcels"):
    global PROG_WIN, PROG_BAR, PROG_LABEL, PROG_STOP_FLAG

    try:
        if PROG_WIN and PROG_WIN.winfo_exists():
            PROG_WIN.destroy()
    except:
        pass

    PROG_STOP_FLAG = {"stop": False}

    PROG_WIN = tk.Toplevel(root)
    PROG_WIN.title(title)
    PROG_WIN.geometry("420x150")
    PROG_WIN.resizable(False, False)

    PROG_WIN.update_idletasks()
    sw = PROG_WIN.winfo_screenwidth()
    x = (sw // 2) - 210
    PROG_WIN.geometry(f"420x150+{x}+80")

    PROG_LABEL = tk.Label(PROG_WIN, text=f"0 / {total} parcels processed", anchor="w")
    PROG_LABEL.pack(fill="x", padx=12, pady=(12, 6))

    PROG_BAR = ttk.Progressbar(PROG_WIN, orient="horizontal",
                                mode="determinate", maximum=total)
    PROG_BAR.pack(fill="x", padx=12, pady=(0, 6))

    def on_cancel():
        PROG_STOP_FLAG["stop"] = True
        PROG_LABEL.config(text="Cancelling... please wait")

    tk.Button(PROG_WIN, text="Cancel", command=on_cancel,
              width=12).pack(pady=(4, 10))

    # Block X button from just destroying — treat it as cancel
    PROG_WIN.protocol("WM_DELETE_WINDOW", on_cancel)

    PROG_WIN.transient(root)
    PROG_WIN.grab_set()
    PROG_WIN.attributes("-topmost", True)
    PROG_WIN.update_idletasks()
    PROG_WIN.update()


def update_progress(current, total, msg=None):
    global PROG_WIN, PROG_BAR, PROG_LABEL, PROG_STOP_FLAG
    if not PROG_WIN or not PROG_WIN.winfo_exists():
        return False

    if PROG_STOP_FLAG.get("stop"):
        return False  # ← signal to stop

    PROG_BAR["value"] = current
    if msg:
        PROG_LABEL.config(text=f"{current} / {total} parcels processed — {msg}")
    else:
        PROG_LABEL.config(text=f"{current} / {total} parcels processed")

    if current == 1 or current == total or current % 5 == 0:
        PROG_WIN.update_idletasks()

    return True  # ← continue


def close_progress_window():
    global PROG_WIN
    try:
        if PROG_WIN and PROG_WIN.winfo_exists():
            PROG_WIN.grab_release()
            PROG_WIN.destroy()
    except:
        pass
    PROG_WIN = None


# ---------------- MAIN PROCESSING ----------------
def process_poi_counts(gdf, poi_gdf, radius_m, progress_cb=None):
    print(f"🚀 Starting POI count processing (radius = {radius_m} meters)...")

    gdf = gdf.to_crs(4326)
    poi_gdf = poi_gdf.to_crs(4326)

    # Ensure lowercase field names
    poi_gdf["fclass"] = poi_gdf["fclass"].astype(str).str.lower()

    # Add output fields
    gdf["num_police"] = 0
    gdf["num_park"] = 0
    gdf["num_mall"] = 0
    gdf["num_others"] = 0

    minx, miny, maxx, maxy = gdf.total_bounds
    bbox_poly = box(minx - 0.05, miny - 0.05, maxx + 0.05, maxy + 0.05)

    print("🌐 Downloading OSM road network within bounds...")
    try:
        G = ox.graph_from_polygon(bbox_poly, network_type='drive')
    except Exception as e:
        print(f"❌ Failed to download OSM data: {e}")
        return gdf

    def add_virtual_node(G, point, node_id):
        try:
            u, v, key = ox.distance.nearest_edges(G, point[1], point[0])
            edge_data = G.get_edge_data(u, v)[key]
            line = edge_data.get('geometry', LineString([
                (G.nodes[u]['x'], G.nodes[u]['y']),
                (G.nodes[v]['x'], G.nodes[v]['y'])
            ]))
            proj_point = line.interpolate(line.project(Point(point[1], point[0])))
            coords = (proj_point.y, proj_point.x)
            G.add_node(node_id, x=coords[1], y=coords[0])
            d_u = geodesic((G.nodes[u]['y'], G.nodes[u]['x']), coords).meters
            d_v = geodesic((G.nodes[v]['y'], G.nodes[v]['x']), coords).meters
            G.add_edge(u, node_id, 0, length=d_u)
            G.add_edge(node_id, u, 0, length=d_u)
            G.add_edge(v, node_id, 0, length=d_v)
            G.add_edge(node_id, v, 0, length=d_v)
            return node_id
        except Exception as e:
            return None
        
    
    total = len(gdf)
    for idx, row in gdf.iterrows():
        centroid = row.geometry.centroid
        lat, lon = centroid.y, centroid.x
        start_node = add_virtual_node(G, (lat, lon), f"start_{idx}")
        if not start_node:
            continue

        # Filter POIs within bounding box (rough prefilter)
        bbox = centroid.buffer(0.02).bounds
        subset = poi_gdf.cx[bbox[0]:bbox[2], bbox[1]:bbox[3]]

        police = park = mall = others = 0

        for _, poi in subset.iterrows():
            poi_lat, poi_lon = poi.geometry.y, poi.geometry.x
            fclass = poi["fclass"].lower()

            # fallback quick check (geodesic)
            dist = geodesic((lat, lon), (poi_lat, poi_lon)).meters
            if dist > radius_m:
                continue

            end_node = add_virtual_node(G, (poi_lat, poi_lon), f"end_{idx}_{_}")
            if not end_node:
                continue
            try:
                if nx.has_path(G, start_node, end_node):
                    length, _ = nx.bidirectional_dijkstra(G, start_node, end_node, weight='length')
                    if length <= radius_m:
                        if fclass == "police":
                            police += 1
                        elif fclass == "park":
                            park += 1
                        elif fclass == "mall":
                            mall += 1
                        else:
                            others += 1
                G.remove_node(end_node)
            except:
                continue

        gdf.at[idx, "num_police"] = police
        gdf.at[idx, "num_park"] = park
        gdf.at[idx, "num_mall"] = mall
        gdf.at[idx, "num_others"] = others

        if start_node in G:
            G.remove_node(start_node)

        print(f"✅ Feature {idx+1}: {police} police, {park} park, {mall} mall, {others} others")

        if progress_cb:
            should_continue = progress_cb(
                idx + 1,
                total,
                msg=f"P:{police} Park:{park} Mall:{mall} O:{others}"
            )
            if should_continue is False:
                print("⛔ Processing cancelled by user.")
                return gdf

    return gdf

# REPLACE WITH

# ---------------- LOAD IN GLOBAL MAPPER ----------------
def load_in_global_mapper(filepath):
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
                return False
            return True

        EnumWindowsProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        ctypes.windll.user32.EnumWindows(EnumWindowsProc(enum_callback), 0)

        subprocess.Popen([GM_EXE_PATH, filepath])
        print(f"🗺️ Sent to Global Mapper: {filepath}")
    except Exception as e:
        print(f"⚠️ Could not open in Global Mapper: {e}")


# ---------------- DB TABLE PICKER ----------------
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


# ---------------- MAIN WINDOW ----------------
def open_main_window(root):
    win = tk.Toplevel(root)
    win.title("POI Count Tool")
    win.resizable(False, False)
    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))

    # ── state ────────────────────────────────────────────────────
    parcel_source_type = tk.StringVar(value="local")
    poi_source_type    = tk.StringVar(value="local")
    output_dest_type   = tk.StringVar(value="local")

    parcel_local_paths = []
    parcel_db_tables   = []
    poi_local_path     = tk.StringVar()
    poi_db_table       = tk.StringVar()
    output_local_dir   = tk.StringVar()
    radius_var         = tk.StringVar(value="200")

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

    # ── SECTION 2: POI SOURCE ────────────────────────────────────
    section_label(win, "POI Source")

    poi_frame = tk.Frame(win)
    poi_frame.pack(fill="x", padx=18, pady=2)

    poi_radio_row = tk.Frame(poi_frame)
    poi_radio_row.pack(fill="x")
    tk.Radiobutton(poi_radio_row, text="Local File",
                   variable=poi_source_type, value="local",
                   command=lambda: _toggle_poi()).pack(side="left")
    tk.Radiobutton(poi_radio_row, text="Database Table",
                   variable=poi_source_type, value="db",
                   command=lambda: _toggle_poi()).pack(side="left", padx=(12, 0))

    poi_local_frame = tk.Frame(poi_frame)
    poi_local_frame.pack(fill="x", pady=2)
    poi_file_label = tk.StringVar(value="No file selected")
    tk.Label(poi_local_frame, textvariable=poi_file_label,
             fg="gray", anchor="w", width=42).pack(side="left")

    def browse_poi_file():
        f = filedialog.askopenfilename(
            filetypes=[("Shapefiles", "*.shp"),
                       ("GeoPackage", "*.gpkg"),
                       ("All", "*.*")])
        if f:
            poi_local_path.set(f)
            poi_file_label.set(os.path.basename(f))

    tk.Button(poi_local_frame, text="Browse…", width=10,
              command=browse_poi_file).pack(side="left", **PAD)

    poi_db_frame = tk.Frame(poi_frame)
    poi_db_label = tk.StringVar(value="No table selected")
    tk.Label(poi_db_frame, textvariable=poi_db_label,
             fg="gray", anchor="w", width=42).pack(side="left")

    def browse_poi_db():
        creds = load_db_credentials()
        if not creds:
            messagebox.showerror("Error", "Could not load DB credentials.")
            return
        tables = fetch_tables(creds["schema"])
        _pick_db_tables(win, tables, multi=False,
                        on_select=lambda sel: (
                            poi_db_table.set(sel[0])
                            or poi_db_label.set(sel[0])
                        ))

    tk.Button(poi_db_frame, text="Select…", width=10,
              command=browse_poi_db).pack(side="left", **PAD)

    def _toggle_poi():
        if poi_source_type.get() == "local":
            poi_db_frame.pack_forget()
            poi_local_frame.pack(fill="x", pady=2)
        else:
            poi_local_frame.pack_forget()
            poi_db_frame.pack(fill="x", pady=2)

    # ── SECTION 3: SEARCH RADIUS ─────────────────────────────────
    section_label(win, "Search Radius")

    radius_frame = tk.Frame(win)
    radius_frame.pack(fill="x", padx=18, pady=2)
    tk.Label(radius_frame, text="Radius (meters):",
             anchor="w").pack(side="left")
    tk.Entry(radius_frame, textvariable=radius_var,
             width=10).pack(side="left", **PAD)

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
        global barangay_source, poi_source, output_mode, radius_meters

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

        # validate poi
        if poi_source_type.get() == "local":
            if not poi_local_path.get():
                messagebox.showerror("Missing Input",
                    "Please select a POI file.")
                return
            poi_source = ("local", [poi_local_path.get()])
        else:
            if not poi_db_table.get():
                messagebox.showerror("Missing Input",
                    "Please select a POI table.")
                return
            poi_source = ("db", [poi_db_table.get()])

        # validate radius
        try:
            radius_meters = float(radius_var.get())
            if radius_meters <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid Input",
                "Please enter a valid positive number for the radius.")
            return

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


# ---------------- RUN PROCESSING ----------------
def run_processing(app_root):
    global barangay_source, poi_source, output_mode, radius_meters

    if not barangay_source or not poi_source or not output_mode:
        messagebox.showerror("Error", "Selections incomplete.")
        return

    creds = load_db_credentials()
    if not creds:
        return

    schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    )

    print("\n🔷 Loading POI data...")
    if poi_source[0] == "local":
        poi_gdf = gpd.read_file(poi_source[1][0])
    else:
        poi_gdf = read_postgis_clean(poi_source[1][0], engine, schema)
    print(f"✅ Loaded {len(poi_gdf)} POIs")

    if barangay_source[0] == "local":
        for path in barangay_source[1]:
            base_name = os.path.splitext(os.path.basename(path))[0]
            print(f"\n🔷 Processing: {base_name}")
            gdf = gpd.read_file(path)

            create_progress_window(app_root, len(gdf), title=f"Processing: {base_name}")
            result = process_poi_counts(gdf, poi_gdf, radius_meters,
                                        progress_cb=update_progress)
            close_progress_window()

            if output_mode[0] == "local":
                out = os.path.join(output_mode[1], f"{base_name}_poi_counts.gpkg")
                result.to_file(out, driver="GPKG")
                print(f"✅ Saved: {out}")
                load_in_global_mapper(out)
            else:
                matched_table = find_matching_table(base_name, schema)
                output_table = matched_table if matched_table else base_name.lower()
                result.to_postgis(output_table, engine, schema=schema,
                                  if_exists="replace", index=False)
                print(f"✅ Saved to DB: {output_table}")
    else:
        for table in barangay_source[1]:
            print(f"\n🔷 Processing DB table: {table}")
            gdf = read_postgis_clean(table, engine, schema)

            create_progress_window(app_root, len(gdf), title=f"Processing: {table}")
            result = process_poi_counts(gdf, poi_gdf, radius_meters,
                                        progress_cb=update_progress)
            close_progress_window()

            if output_mode[0] == "local":
                out = os.path.join(output_mode[1], f"{table}_poi_counts.gpkg")
                result.to_file(out, driver="GPKG")
                print(f"✅ Saved: {out}")
                load_in_global_mapper(out)
            else:
                result.to_postgis(table, engine, schema=schema,
                                  if_exists="replace", index=False)
                print(f"✅ Updated DB table: {table}")

    messagebox.showinfo("Success", "✅ Processing complete!")


# ---------------- MAIN ----------------
def main(parent=None):
    global APP_ROOT
    if parent is not None:
        APP_ROOT = parent
        open_main_window(parent)
    else:
        root = tk.Tk()
        APP_ROOT = root
        root.withdraw()
        open_main_window(root)
        root.mainloop()


if __name__ == "__main__":
    main()