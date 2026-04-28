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
    global PROG_WIN, PROG_BAR, PROG_LABEL

    # close existing if any
    try:
        if PROG_WIN and PROG_WIN.winfo_exists():
            PROG_WIN.destroy()
    except:
        pass

    PROG_WIN = tk.Toplevel(root)
    PROG_WIN.title(title)
    PROG_WIN.geometry("420x120")
    PROG_WIN.resizable(False, False)

    PROG_LABEL = tk.Label(PROG_WIN, text=f"0 / {total} parcels processed", anchor="w")
    PROG_LABEL.pack(fill="x", padx=12, pady=(12, 6))

    PROG_BAR = ttk.Progressbar(PROG_WIN, orient="horizontal", mode="determinate", maximum=total)
    PROG_BAR.pack(fill="x", padx=12, pady=(0, 12))

    # keep on top but not annoying
    PROG_WIN.transient(root)
    PROG_WIN.grab_set()  # prevents clicking other windows while running
    PROG_WIN.update_idletasks()


def update_progress(current, total, msg=None):
    global PROG_WIN, PROG_BAR, PROG_LABEL
    if not PROG_WIN or not PROG_WIN.winfo_exists():
        return

    PROG_BAR["value"] = current
    if msg:
        PROG_LABEL.config(text=f"{current} / {total} parcels processed — {msg}")
    else:
        PROG_LABEL.config(text=f"{current} / {total} parcels processed")

    # refresh UI
    PROG_WIN.update_idletasks()
    PROG_WIN.update()


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
            progress_cb(
                idx + 1,
                total,
                msg=f"P:{police} Park:{park} Mall:{mall} O:{others}"
            )

    return gdf

# ---------------- TKINTER SELECTION WINDOWS ----------------
def select_barangay_window(root):
    win = tk.Toplevel(root)
    win.title("Barangay Parcel Source")
    tk.Label(win, text="Select Barangay Parcel Source").pack(padx=85, pady=10)

    def pick_local():
        global barangay_source
        files = filedialog.askopenfilenames(filetypes=[("Shapefiles", "*.shp")])
        if files:
            barangay_source = ("local", files)
            print("✅ Barangay (Local):", barangay_source)
            win.destroy()
            select_poi_window(root)

    def pick_db():
        global barangay_source
        creds = load_db_credentials()
        if not creds:
            return
        engine = create_engine(
            f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
        )
        tables = inspect(engine).get_table_names(schema=creds["schema"])
        
        db_win = tk.Toplevel(root)
        db_win.title("Select Barangay Tables")
        lb = Listbox(db_win, selectmode=tk.MULTIPLE, width=50, height=15)
        for t in tables:
            lb.insert(tk.END, t)
        lb.pack(padx=10, pady=10)
        
        def submit():
            global barangay_source
            sel = [lb.get(i) for i in lb.curselection()]
            if sel:
                barangay_source = ("db", sel)
                print("✅ Barangay (DB):", barangay_source)
                db_win.destroy()
                win.destroy()
                select_poi_window(root)
            else:
                messagebox.showwarning("Warning", "Please select at least one table.")
        
        tk.Button(db_win, text="Select", command=submit).pack(pady=5)
    
    tk.Button(win, text="Local File", command=pick_local).pack(pady=5)
    tk.Button(win, text="Database File", command=pick_db).pack(pady=5)

def select_poi_window(root):
    win = tk.Toplevel(root)
    win.title("POI Source")
    tk.Label(win, text="Select POI Source").pack(padx=85, pady=10)

    def pick_local():
        global poi_source
        file = filedialog.askopenfilename(filetypes=[("Shapefiles", "*.shp")])
        if file:
            poi_source = ("local", [file])
            print("✅ POI (Local):", poi_source)
            win.destroy()
            input_distance_window(root)

    def pick_db():
        global poi_source
        creds = load_db_credentials()
        if not creds:
            return
        engine = create_engine(
            f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
        )
        tables = inspect(engine).get_table_names(schema=creds["schema"])
        
        db_win = tk.Toplevel(root)
        db_win.title("Select POI Table")
        lb = Listbox(db_win, selectmode=tk.SINGLE, width=50, height=15)
        for t in tables:
            lb.insert(tk.END, t)
        lb.pack(padx=10, pady=10)
        
        def submit():
            global poi_source
            sel = [lb.get(i) for i in lb.curselection()]
            if sel:
                poi_source = ("db", sel)
                print("✅ POI (DB):", poi_source)
                db_win.destroy()
                win.destroy()
                input_distance_window(root)
            else:
                messagebox.showwarning("Warning", "Please select a table.")
        
        tk.Button(db_win, text="Select", command=submit).pack(pady=5)
    
    tk.Button(win, text="Local File", command=pick_local).pack(pady=5)
    tk.Button(win, text="Database File", command=pick_db).pack(pady=5)

def input_distance_window(root):
    win = tk.Toplevel(root)
    win.title("Set Distance")
    tk.Label(win, text="Enter search radius (meters):").pack(pady=10)
    entry = tk.Entry(win)
    entry.insert(0, "200")
    entry.pack(pady=5)
    
    def submit():
        global radius_meters
        try:
            radius_meters = float(entry.get())
            if radius_meters <= 0:
                raise ValueError
            print(f"📏 Radius set to {radius_meters} meters")
            win.destroy()
            select_output_window(root)
        except:
            messagebox.showerror("Error", "Please enter a valid positive number.")
    
    tk.Button(win, text="Next", command=submit).pack(pady=10)

def select_output_window(root):
    win = tk.Toplevel(root)
    win.title("Output Destination")
    tk.Label(win, text="Save output to:").pack(padx=85, pady=10)

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

    tk.Button(win, text="Local", command=save_local).pack(pady=5)
    tk.Button(win, text="Database", command=save_db).pack(pady=5)

# ---------------- RUN PROCESSING ----------------
def run_processing():
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

    # Load POI
    print("\n🔷 Loading POI data...")
    if poi_source[0] == "local":
        poi_gdf = gpd.read_file(poi_source[1][0])
        print(f"✅ Loaded {len(poi_gdf)} POIs from local file")
    else:
        poi_gdf = read_postgis_clean(poi_source[1][0], engine, schema)
        print(f"✅ Loaded {len(poi_gdf)} POIs from database table: {poi_source[1][0]}")

    # Process barangays
    if barangay_source[0] == "local":
        for path in barangay_source[1]:
            base_name = os.path.splitext(os.path.basename(path))[0]
            print(f"\n🔷 Processing barangay file: {base_name}")
            
            gdf = gpd.read_file(path)

            create_progress_window(APP_ROOT, len(gdf), title=f"Processing: {base_name}")
            result = process_poi_counts(gdf, poi_gdf, radius_meters, progress_cb=update_progress)
            close_progress_window()

            if output_mode[0] == "local":
                out = os.path.join(output_mode[1], f"{base_name}_poi_counts.shp")
                result.to_file(out)
                open_in_global_mapper(out)
                print(f"✅ Saved locally: {out}")
            else:
                # Database output with substring matching
                matched_table = find_matching_table(base_name, schema)
                output_table = matched_table if matched_table else base_name.lower()
                print(f"💾 Saving to DB table: {output_table} {'(matched)' if matched_table else '(new)'}")
                result.to_postgis(output_table, engine, schema=schema, if_exists="replace", index=False)
                print(f"✅ Saved to database: {output_table}")
    else:
        for table in barangay_source[1]:
            print(f"\n🔷 Processing barangay table: {table}")
            
            gdf = read_postgis_clean(table, engine, schema)

            create_progress_window(APP_ROOT, len(gdf), title=f"Processing: {table}")
            result = process_poi_counts(gdf, poi_gdf, radius_meters, progress_cb=update_progress)
            close_progress_window()

            if output_mode[0] == "local":
                out = os.path.join(output_mode[1], f"{table}_poi_counts.shp")
                result.to_file(out)
                open_in_global_mapper(out)
                print(f"✅ Saved locally: {out}")
            else:
                # Replace the same table when source is database
                print(f"💾 Updating DB table: {table}")
                result.to_postgis(table, engine, schema=schema, if_exists="replace", index=False)
                print(f"✅ Updated database table: {table}")

    messagebox.showinfo("Success", "✅ Processing complete!")

# ---------------- MAIN ----------------
def main():
    global APP_ROOT
    root = tk.Tk()
    APP_ROOT = root
    root.withdraw()
    select_barangay_window(root)
    root.mainloop()

if __name__ == "__main__":
    main()