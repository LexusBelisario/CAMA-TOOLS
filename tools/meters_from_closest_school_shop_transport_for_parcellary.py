import os
import re
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox
import geopandas as gpd
import osmnx as ox
import networkx as nx
from shapely.geometry import Point, LineString, box
from geopy.distance import geodesic
import subprocess
import json
from sqlalchemy import create_engine, inspect, text
import psycopg2
import time
from tkinter import ttk


class ProgressWindow:
    def __init__(self, root, title="Processing"):
        self.win = tk.Toplevel(root)
        self.win.title(title)
        self.win.geometry("420x140")
        self.win.resizable(False, False)

        self.status_var = tk.StringVar(value="Starting...")
        tk.Label(self.win, textvariable=self.status_var).pack(pady=5)

        self.progress = ttk.Progressbar(
            self.win, orient="horizontal", mode="determinate", length=380
        )
        self.progress.pack(pady=5)

        self.eta_var = tk.StringVar(value="ETA: calculating...")
        tk.Label(self.win, textvariable=self.eta_var).pack(pady=5)

        self.start_time = time.time()
        self.win.update()

    def update(self, message, current, total):
        self.status_var.set(message)
        self.progress["maximum"] = total
        self.progress["value"] = current

        elapsed = time.time() - self.start_time
        if current > 0:
            avg = elapsed / current
            remaining = (total - current) * avg
            self.eta_var.set(f"ETA: {remaining/60:.1f} min")

        self.win.update_idletasks()
        self.win.update()

    def close(self):
        self.win.destroy()


# --- Config ---
ICON_PATH = r"D:/2025_PROJECTS/BLGF-GM_TEST/FOR TESTING/DCS_CODES/BLGF.ico"
GM_EXE_PATH = r"C:\\Program Files\\GlobalMapper26.1_64bit\\global_mapper.exe"
CREDENTIALS_FILE = "pg_credentials.json"

# --- Globals ---
barangay_source = None
poi_source = None
output_mode = None

ox.settings.use_cache = True
ox.settings.log_console = False

# ---------------- Helpers ----------------
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
    cols = [c["name"] for c in insp.get_columns(table, schema=schema) if c["name"] != geom_col]
    col_str = ", ".join([f'"{c}"' for c in cols]) if cols else ""
    query = f'SELECT {col_str + "," if col_str else ""}"{geom_col}" AS geometry FROM "{schema}"."{table}"'
    return gpd.read_postgis(query, engine, geom_col="geometry")

def load_into_global_mapper(shapefile_path):
    if os.path.exists(GM_EXE_PATH) and os.path.exists(shapefile_path):
        subprocess.Popen([GM_EXE_PATH, shapefile_path], shell=True)

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

root = tk.Toplevel()
root.withdraw()

progress = ProgressWindow(root, "POI Distance Processing")

# ---------------- Core Processing ----------------
def process_distances(gdf, poi_gdf, progress=None):

    gdf = gdf.to_crs(4326)
    poi_gdf = poi_gdf.to_crs(4326)
    
    gdf["LAT_LONG"] = gdf.to_crs(3857).centroid.to_crs(4326).apply(
        lambda c: f"{c.y:.6f}, {c.x:.6f}"
    )
    
    for typ in ["SCHOOL", "SHOP", "CHURCH", "TRANSPORT"]:
        gdf[typ] = None
    
    minx, miny, maxx, maxy = gdf.total_bounds
    bbox_poly = box(minx - 0.02, miny - 0.02, maxx + 0.02, maxy + 0.02)
    
    print("🌐 Downloading OSM road network within barangay bounds...")
    G = ox.graph_from_polygon(bbox_poly, network_type="drive")
    
    def add_virtual_node(G, point, node_id):
        try:
            u, v, key = ox.distance.nearest_edges(G, point[1], point[0])
            edge_data = G.get_edge_data(u, v)[key]
            line = edge_data.get(
                "geometry",
                LineString(
                    [
                        (G.nodes[u]["x"], G.nodes[u]["y"]),
                        (G.nodes[v]["x"], G.nodes[v]["y"]),
                    ]
                ),
            )
            proj_point = line.interpolate(line.project(Point(point[1], point[0])))
            coords = (proj_point.y, proj_point.x)
            
            G.add_node(node_id, x=coords[1], y=coords[0])
            
            d_u = geodesic((G.nodes[u]["y"], G.nodes[u]["x"]), coords).meters
            d_v = geodesic((G.nodes[v]["y"], G.nodes[v]["x"]), coords).meters
            
            for a, b, d in [(u, node_id, d_u), (v, node_id, d_v)]:
                G.add_edge(a, b, 0, length=d)
                G.add_edge(b, a, 0, length=d)
            
            return node_id
        except Exception as e:
            print(f"⚠️ Virtual node failed at {point}: {e}")
            return None
    
    types = ["school", "shop", "church", "transport"]
    
    total = len(gdf)

    for idx, row in gdf.iterrows():
        if progress:
            progress.update(
                f"Processing parcel {idx + 1} of {total}",
                idx + 1,
                total
            )

        centroid = row.geometry.centroid
        lat, lon = centroid.y, centroid.x
        
        for typ in types:
            subset = poi_gdf[poi_gdf["fclass"].str.lower() == typ]
            if subset.empty:
                print(f"⚠️ No POIs for {typ}")
                continue
            
            subset["DIST"] = subset.geometry.distance(centroid)
            nearest_poi = subset.loc[subset["DIST"].idxmin()]
            
            try:
                start_node = add_virtual_node(G, (lat, lon), f"start_{idx}_{typ}")
                end_node = add_virtual_node(
                    G,
                    (nearest_poi.geometry.y, nearest_poi.geometry.x),
                    f"end_{idx}_{typ}"
                )
                
                if start_node and end_node and nx.has_path(G, start_node, end_node):
                    length, _ = nx.bidirectional_dijkstra(G, start_node, end_node, weight="length")
                    gdf.at[idx, typ.upper()] = round(length, 2)
                    print(f"✅ {typ.upper()} = {length:.2f} m")
                else:
                    raise Exception("No route found")
            except Exception as e:
                fallback = geodesic(
                    (lat, lon),
                    (nearest_poi.geometry.y, nearest_poi.geometry.x)
                ).meters
                gdf.at[idx, typ.upper()] = round(fallback, 2)
                print(f"⚠️ {typ.upper()} fallback = {fallback:.2f} m ({e})")
            
            for node in [f"start_{idx}_{typ}", f"end_{idx}_{typ}"]:
                if node in G:
                    G.remove_node(node)
    
    return gdf

# ---------------- Tkinter Selections ----------------
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
        engine = create_engine(
            f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
        )
        tables = inspect(engine).get_table_names(schema=creds["schema"])
        
        db_win = tk.Toplevel(root)
        lb = Listbox(db_win, selectmode=tk.MULTIPLE, width=50)
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
                select_poi_window(root)
        
        tk.Button(db_win, text="Select", command=submit).pack()
    
    tk.Button(win, text="Local File", command=pick_local).pack()
    tk.Button(win, text="Database File", command=pick_db).pack()

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
            select_output_window(root)
    
    def pick_db():
        global poi_source
        creds = load_db_credentials()
        engine = create_engine(
            f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
        )
        tables = inspect(engine).get_table_names(schema=creds["schema"])
        
        db_win = tk.Toplevel(root)
        lb = Listbox(db_win, selectmode=tk.SINGLE, width=50)
        for t in tables:
            lb.insert(tk.END, t)
        lb.pack()
        
        def submit():
            global poi_source
            sel = [lb.get(i) for i in lb.curselection()]
            if sel:
                poi_source = ("db", sel)
                print("✅ POI (DB):", poi_source)
                db_win.destroy()
                win.destroy()
                select_output_window(root)
        
        tk.Button(db_win, text="Select", command=submit).pack()
    
    tk.Button(win, text="Local File", command=pick_local).pack()
    tk.Button(win, text="Database File", command=pick_db).pack()

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
    
    tk.Button(win, text="Local", command=save_local).pack()
    tk.Button(win, text="Database", command=save_db).pack()

# ---------------- Run Processing ----------------
def run_processing():
    global barangay_source, poi_source, output_mode
    
    if not barangay_source or not poi_source or not output_mode:
        messagebox.showerror("Error", "Selections incomplete.")
        return
    
    creds = load_db_credentials()
    schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    )
    
    # Load POI data
    poi_gdf = (
        gpd.read_file(poi_source[1][0])
        if poi_source[0] == "local"
        else read_postgis_clean(poi_source[1][0], engine, schema)
    )
    
    # Process each barangay file
    for item in barangay_source[1]:
        if barangay_source[0] == "local":
            gdf = gpd.read_file(item)
            base_name = os.path.splitext(os.path.basename(item))[0]
        else:
            gdf = read_postgis_clean(item, engine, schema)
            base_name = item
        
        print(f"\n🔷 Processing {base_name}...")
        
        result = process_distances(gdf, poi_gdf, progress)

        if output_mode[0] == "local":
            out_path = os.path.join(output_mode[1], f"{base_name}_poi_distances.shp")
            result.to_file(out_path)
            load_into_global_mapper(out_path)
            print(f"✅ Saved locally: {out_path}")
        else:
            # Database output with substring matching
            if barangay_source[0] == "local":
                # For local files, find matching table
                matched_table = find_matching_table(base_name, schema)
                output_table = matched_table if matched_table else base_name.lower()
                print(f"💾 Saving to DB table: {output_table} {'(matched)' if matched_table else '(new)'}")
            else:
                # For DB sources, replace the same table
                output_table = base_name
                print(f"💾 Updating DB table: {output_table}")
            
            result.to_postgis(output_table, engine, schema=schema, if_exists="replace", index=False)
            print(f"✅ Saved to database: {output_table}")
    
    messagebox.showinfo("Success", "✅ Processing complete!")

# ---------------- Main ----------------
def main():
    root = tk.Tk()
    root.withdraw()
    select_barangay_window(root)
    root.mainloop()

if __name__ == "__main__":
    main()