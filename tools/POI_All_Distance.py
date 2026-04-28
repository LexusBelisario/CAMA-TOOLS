# ==========================================================
# POI_All_Distance.py
# Tool-style version (mirrors road_width behavior)
# ==========================================================

root = None

import os
import time
import datetime
import traceback
import json
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox, ttk
import geopandas as gpd
import networkx as nx
from shapely.geometry import Point, LineString
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

import psycopg2
from sqlalchemy import create_engine, text, inspect

# ---------------- CONFIG ----------------
CREDENTIALS_FILE = "pg_credentials.json"

parcel_source = None     # ("local", [paths]) OR ("db", [tables])
poi_source = None        # ("local", [path])  OR ("db", [table])
road_source = None       # ("local", [path])  OR ("db", [table])
output_mode = None       # ("local", out_dir) OR ("db", None)

ALLOWED_FCLASS = {"school", "church", "shop", "transport", "university"}


# -----------------------------------
# Utility: logging helper
# -----------------------------------
def log_message(msg, logfile=None, flush=True):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=flush)
    if logfile:
        with open(logfile, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# -----------------------------------
# DB helpers (same pattern as road_width)
# -----------------------------------
def load_db_credentials():
    try:
        with open(CREDENTIALS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return None


def fetch_tables(schema):
    creds = load_db_credentials()
    if not creds:
        return []
    try:
        conn = psycopg2.connect(
            host=creds["host"], port=creds["port"],
            dbname=creds["database"],
            user=creds["username"], password=creds["password"]
        )
        cur = conn.cursor()
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema=%s ORDER BY table_name;
        """, (schema,))
        tables = [r[0] for r in cur.fetchall()]
        conn.close()
        return tables
    except Exception as e:
        messagebox.showerror("DB Error", str(e))
        return []


def get_geometry_column(table, engine, schema):
    with engine.connect() as conn:
        q = text("""
            SELECT f_geometry_column
            FROM geometry_columns
            WHERE f_table_schema=:s AND f_table_name=:t
        """)
        r = conn.execute(q, {"s": schema, "t": table}).fetchone()
        return r[0] if r else None


def read_postgis_clean(table, engine, schema):
    geom_col = get_geometry_column(table, engine, schema)
    if not geom_col:
        raise ValueError(f"No geometry column in {table}")

    insp = inspect(engine)
    cols = [c["name"] for c in insp.get_columns(table, schema=schema) if c["name"] != geom_col]

    col_str = ", ".join([f'"{c}"' for c in cols])
    if col_str:
        sql = f'SELECT {col_str}, "{geom_col}" AS geometry FROM "{schema}"."{table}"'
    else:
        sql = f'SELECT "{geom_col}" AS geometry FROM "{schema}"."{table}"'

    return gpd.read_postgis(sql, engine, geom_col="geometry")


# -----------------------------------
# Build graph from road shapefile
# -----------------------------------
def graph_from_roads(road_gdf):
    G = nx.Graph()
    edges = []
    nodes_coords = set()

    for _, row in road_gdf.iterrows():
        geom = row.geometry
        if geom is None:
            continue
        try:
            segments = [geom] if isinstance(geom, LineString) else list(geom)
            for seg in segments:
                coords = list(seg.coords)
                for i in range(len(coords) - 1):
                    u = (float(coords[i][0]), float(coords[i][1]))
                    v = (float(coords[i + 1][0]), float(coords[i + 1][1]))
                    length = Point(u).distance(Point(v))
                    G.add_edge(u, v, length=float(length))
                    edges.append((u, v, float(length)))
                    nodes_coords.add(u)
                    nodes_coords.add(v)
        except Exception:
            continue

    nodes_coords = np.array(list(nodes_coords)) if nodes_coords else np.zeros((0, 2))
    return G, edges, nodes_coords


# -----------------------------------
# Worker (UNCHANGED logic)
# -----------------------------------
def worker_process(args):
    (row_idx, centroid_xy, poi_types, poi_coords_dict, edges_list, nodes_coords) = args
    try:
        if len(nodes_coords) == 0:
            return row_idx, {}

        nodes_kdtree = cKDTree(nodes_coords)
        G_local = nx.Graph()
        for u, v, length in edges_list:
            G_local.add_edge(tuple(u), tuple(v), length=float(length))

        results = {}
        for typ in poi_types:
            coords = poi_coords_dict.get(typ)
            if coords is None or len(coords) == 0:
                continue

            k = min(3, len(coords))
            tree = cKDTree(coords)
            _, idxs = tree.query([centroid_xy], k=k)

            idxs = [int(idxs)] if k == 1 else [int(i) for i in idxs[0]]

            network_results = []
            for pi in idxs:
                poi_xy = coords[pi]
                _, sidx = nodes_kdtree.query([centroid_xy])
                _, eidx = nodes_kdtree.query([poi_xy])
                start = tuple(nodes_coords[int(sidx[0])])
                end = tuple(nodes_coords[int(eidx[0])])

                try:
                    if nx.has_path(G_local, start, end):
                        dist, _ = nx.bidirectional_dijkstra(G_local, start, end, weight="length")
                    else:
                        dist = Point(centroid_xy).distance(Point(poi_xy))
                except Exception:
                    dist = Point(centroid_xy).distance(Point(poi_xy))

                network_results.append(round(dist, 2))

            network_results = sorted(network_results)
            for i, dist in enumerate(network_results[:3], start=1):
                results[f"{typ.upper()}{i}"] = float(dist)

        return row_idx, results

    except Exception as e:
        return row_idx, {"_error": str(e)}


def run_cpu_parallel_with_progress(
    gdf, poi_gdf, road_gdf,
    poi_types, poi_coords_dict,
    output_path,
    progress_bar, status_var, eta_var,
    stop_flag
):


    t0 = time.time()

    G_main, edges_list, nodes_coords = graph_from_roads(road_gdf)
    if len(edges_list) == 0:
        raise Exception("No valid edges found in road network.")

    args_list = []
    for idx, row in gdf.iterrows():
        centroid_xy = (row.geometry.centroid.x, row.geometry.centroid.y)
        args_list.append(
            (idx, centroid_xy, poi_types, poi_coords_dict, edges_list, nodes_coords)
        )

    total = len(args_list)
    processed = 0
    errors = 0
    start_time = time.time()

    total_cpus = os.cpu_count() or 1
    cpu_count = max(1, total_cpus - 1)


    for i, args in enumerate(args_list, start=1):
        if stop_flag["stop"]:
            return None

        elapsed = time.time() - start_time
        avg = elapsed / max(1, i)
        remaining = (total - i) * avg

        idx, res = worker_process(args)

        if "_error" in res:
            continue

        for k, v in res.items():
            gdf.at[idx, k] = v

        progress_bar["value"] = i
        status_var.set(f"Processed {i} / {total} parcels")
        eta_var.set(f"ETA: {remaining/60:.1f} min")

        progress_bar.master.update_idletasks()
        progress_bar.master.update()

    if output_path:
        gdf.to_file(output_path)


# -----------------------------------
# Core runner (adapted wrapper)
# -----------------------------------
def run_processing():

    global parcel_source, poi_source, road_source, output_mode


    if parcel_source is None or poi_source is None or road_source is None:
        messagebox.showerror(
            "Error",
            "Incomplete selection.\n\n"
            "Please select:\n"
            "- Land Parcels\n"
            "- POIs\n"
            "- Roads"
        )
        return

    creds = load_db_credentials()
    if not creds:
        messagebox.showerror("Error", "Database credentials not found.")
        return

    schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@"
        f"{creds['host']}:{creds['port']}/{creds['database']}"
    )

    # ---- Load parcels ----
    parcel_gdfs = []
    if parcel_source[0] == "local":
        for p in parcel_source[1]:
            parcel_gdfs.append(gpd.read_file(p))
    else:
        for t in parcel_source[1]:
            parcel_gdfs.append(read_postgis_clean(t, engine, schema))

    gdf = gpd.GeoDataFrame(pd.concat(parcel_gdfs, ignore_index=True), crs=parcel_gdfs[0].crs)

    # ---- Load POIs ----
    if poi_source[0] == "local":
        poi_gdf = gpd.read_file(poi_source[1][0])
    else:
        poi_gdf = read_postgis_clean(poi_source[1][0], engine, schema)

    # ---- Load roads ----
    if road_source[0] == "local":
        road_gdf = gpd.read_file(road_source[1][0])
    else:
        road_gdf = read_postgis_clean(road_source[1][0], engine, schema)

    # ---- CRS ----
    gdf = gdf.to_crs(gdf.estimate_utm_crs())
    poi_gdf = poi_gdf.to_crs(gdf.crs)
    road_gdf = road_gdf.to_crs(gdf.crs)

    # ---- POI filtering ----
    if "fclass" not in [c.lower() for c in poi_gdf.columns]:
        raise Exception("POI 'fclass' column not found.")

    poi_gdf["_fclass_norm"] = poi_gdf["fclass"].astype(str).str.strip().str.lower()
    poi_types = sorted(t for t in poi_gdf["_fclass_norm"].unique() if t in ALLOWED_FCLASS)

    G_main, edges_list, nodes_coords = graph_from_roads(road_gdf)

    poi_coords = {
        t: np.array([[p.x, p.y] for p in poi_gdf[poi_gdf["_fclass_norm"] == t].geometry])
        for t in poi_types
    }

    for t in ALLOWED_FCLASS:
        for i in range(1, 4):
            gdf[f"{t.upper()}{i}"] = np.nan

    args = [
        (idx, (row.geometry.centroid.x, row.geometry.centroid.y),
         poi_types, poi_coords, edges_list, nodes_coords)
        for idx, row in gdf.iterrows()
    ]
    # ---- Output ----
    if output_mode[0] == "local":
        out = os.path.join(output_mode[1], "parcels_with_poi_distances.shp")
        gdf.to_file(out)
        messagebox.showinfo("Success", f"Saved to:\n{out}")
    
# -----------------------------------
# GUI selection windows (mirrors road_width)
# -----------------------------------
# ----------------- TKINTER WINDOWS -----------------
def select_barangay_window(root):
    win = tk.Toplevel(root)
    win.title("Select Land Parcel Source")

    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))

    # optional: prevent resizing so width stays consistent
    win.resizable(False, False)

    def pick_local():
        global parcel_source
        files = filedialog.askopenfilenames(filetypes=[("Shapefiles", "*.shp")])
        if files:
            parcel_source = ("local", files)
            print("✅ Barangay source set:", parcel_source)
            win.destroy()
            select_poi_window(root)   # ✅ CORRECT

    def pick_db():
        global parcel_source
        creds = load_db_credentials()
        tables = fetch_tables(creds["schema"])

        db_win = tk.Toplevel(root)
        db_win.title("Select Land Parcel Table (DB)")

        lb = Listbox(db_win, selectmode=tk.MULTIPLE, width=55, height=15)
        for t in tables:
            lb.insert(tk.END, t)
        lb.pack()

        def submit():
            global parcel_source
            sel = [lb.get(i) for i in lb.curselection()]
            if sel:
                parcel_source = ("db", sel)
                print("✅ Barangay source set:", parcel_source)
                db_win.destroy()
                win.destroy()
                select_poi_window(root)   # ✅ CORRECT

        tk.Button(db_win, text="Select", command=submit).pack(pady=5)

    # 🔹 Button container (SIDE-BY-SIDE)
    btn_frame = tk.Frame(win)
    btn_frame.pack(padx=25, pady=10)  # 👈 controls window width

    tk.Button(
        btn_frame,
        text="Select Local File",
        command=pick_local,
        width=18
    ).pack(side=tk.LEFT, padx=5)

    tk.Button(
        btn_frame,
        text="Select Database Table",
        command=pick_db,
        width=18
    ).pack(side=tk.LEFT, padx=5)


def select_poi_window(root):
    win = tk.Toplevel(root)
    win.title("Select POI Source")

    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))

    win.resizable(False, False)

    def pick_local():
        global poi_source
        file = filedialog.askopenfilename(filetypes=[("Shapefiles", "*.shp")])
        if file:
            poi_source = ("local", [file])
            print("✅ POI source set:", poi_source)
            win.destroy()
            select_road_window(root)   # ✅ correct next step

    def pick_db():
        global poi_source
        creds = load_db_credentials()
        tables = fetch_tables(creds["schema"])

        db_win = tk.Toplevel(root)
        db_win.title("Select POI Table (DB)")

        lb = Listbox(db_win, selectmode=tk.SINGLE, width=55, height=15)
        for t in tables:
            lb.insert(tk.END, t)
        lb.pack()

        def submit():
            global poi_source
            sel = [lb.get(i) for i in lb.curselection()]
            if sel:
                poi_source = ("db", sel)
                print("✅ POI source set:", poi_source)
                db_win.destroy()
                win.destroy()
                select_road_window(root)   # ✅ correct next step

        tk.Button(db_win, text="Select", command=submit).pack(pady=5)

    # 🔹 SIDE-BY-SIDE buttons (same layout as other windows)
    btn_frame = tk.Frame(win)
    btn_frame.pack(padx=25, pady=10)

    tk.Button(
        btn_frame,
        text="Select Local File",
        command=pick_local,
        width=18
    ).pack(side=tk.LEFT, padx=5)

    tk.Button(
        btn_frame,
        text="Select Database Table",
        command=pick_db,
        width=18
    ).pack(side=tk.LEFT, padx=5)


def select_road_window(root):
    win = tk.Toplevel(root)
    win.title("Select Road Source")

    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))

    # keep size consistent with Select Land Parcel Source
    win.resizable(False, False)

    def pick_local():
        global road_source
        file = filedialog.askopenfilename(filetypes=[("Shapefiles", "*.shp")])
        if file:
            road_source = ("local", [file])
            print("✅ Road source set:", road_source)
            win.destroy()
            select_output_window(root)

    def pick_db():
        global road_source
        creds = load_db_credentials()
        tables = fetch_tables(creds["schema"])

        db_win = tk.Toplevel(root)
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
                print("✅ Road source set:", road_source)
                db_win.destroy()
                win.destroy()
                select_output_window(root)

        tk.Button(db_win, text="Select", command=submit).pack(pady=5)

    # 🔹 SIDE-BY-SIDE buttons (same layout as Barangay window)
    btn_frame = tk.Frame(win)
    btn_frame.pack(padx=25, pady=10)  # 👈 SAME padding = SAME width

    tk.Button(
        btn_frame,
        text="Select Local File",
        command=pick_local,
        width=18
    ).pack(side=tk.LEFT, padx=5)

    tk.Button(
        btn_frame,
        text="Select Database Table",
        command=pick_db,
        width=18
    ).pack(side=tk.LEFT, padx=5)

def select_output_window(root):
    win = tk.Toplevel(root)
    win.title("Select Output Destination")

    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))


    # keep size consistent with the other windows
    win.resizable(False, False)

    def save_local():
        global output_mode, parcel_source, road_source
        if not parcel_source or not poi_source or not road_source:
            messagebox.showerror(
                "Error",
                "Barangay, POI, and Road must be selected first."
            )
            return

        out_dir = filedialog.askdirectory()
        if out_dir:
            output_mode = ("local", out_dir)
            print("✅ Output mode set:", output_mode)
            win.destroy()
            run_with_progress(root)

    def save_db():
        global output_mode, parcel_source, road_source
        if not parcel_source or not poi_source or not road_source:
            messagebox.showerror(
                "Error",
                "Barangay, POI, and Road must be selected first."
            )
            return

        output_mode = ("db", None)
        print("✅ Output mode set:", output_mode)
        win.destroy()
        run_with_progress(root)

    # 🔹 SIDE-BY-SIDE buttons (same layout & size)
    btn_frame = tk.Frame(win)
    btn_frame.pack(padx=25, pady=10)  # 👈 SAME padding as other windows

    tk.Button(
        btn_frame,
        text="Save to Local",
        command=save_local,
        width=18
    ).pack(side=tk.LEFT, padx=5)

    tk.Button(
        btn_frame,
        text="Save to Database",
        command=save_db,
        width=18
    ).pack(side=tk.LEFT, padx=5)


def run_with_progress(root):

    if hasattr(root, "_poi_progress_open") and root._poi_progress_open:
        return
    root._poi_progress_open = True

    progress_win = tk.Toplevel(root)
    progress_win.title("Processing Parcels...")
    progress_win.geometry("420x240")
    progress_win.resizable(False, False)

    tk.Label(
        progress_win,
        text="Computing network distances...",
        font=("Segoe UI", 11)
    ).pack(pady=10)

    progress_bar = ttk.Progressbar(
        progress_win,
        orient="horizontal",
        length=360,
        mode="determinate",
        maximum=100
    )

    progress_bar.pack(pady=5)

    status_var = tk.StringVar(value="Starting...")
    tk.Label(progress_win, textvariable=status_var).pack(pady=5)

    eta_var = tk.StringVar(value="ETA: calculating...")
    tk.Label(progress_win, textvariable=eta_var).pack(pady=5)

    stop_flag = {"stop": False}

    def cancel():
        stop_flag["stop"] = True
        status_var.set("Cancelling...")

    tk.Button(progress_win, text="Cancel", command=cancel).pack(pady=10)

    progress_win.lift()
    progress_win.focus_force()

    def task():
        try:
            status_var.set("Loading input data...")
            progress_bar["value"] = 0
            progress_win.update_idletasks()
            # ---- PREP DATA (same as run_processing) ----
            creds = load_db_credentials()
            schema = creds["schema"]

            engine = create_engine(
                f"postgresql://{creds['username']}:{creds['password']}@"
                f"{creds['host']}:{creds['port']}/{creds['database']}"
            )

            # ---- DB target table (road_width behavior) ----
            target_table = None
            if output_mode[0] == "db":
                if parcel_source[0] != "db" or len(parcel_source[1]) != 1:
                    raise Exception(
                        "Database output requires selecting exactly ONE parcel table."
                    )
                target_table = parcel_source[1][0]

            # parcels
            parcel_gdfs = []
            if parcel_source[0] == "local":
                for p in parcel_source[1]:
                    parcel_gdfs.append(gpd.read_file(p))
            else:
                for t in parcel_source[1]:
                    parcel_gdfs.append(read_postgis_clean(t, engine, schema))

            gdf = gpd.GeoDataFrame(
                pd.concat(parcel_gdfs, ignore_index=True),
                crs=parcel_gdfs[0].crs
            )

            total_features = len(gdf)
            progress_bar["maximum"] = total_features
            progress_bar["value"] = 0
            progress_win.update_idletasks()


            status_var.set("Reprojecting layers...")
            progress_win.update_idletasks()

            # POIs
            poi_gdf = (
                gpd.read_file(poi_source[1][0])
                if poi_source[0] == "local"
                else read_postgis_clean(poi_source[1][0], engine, schema)
            )

            # Roads
            road_gdf = (
                gpd.read_file(road_source[1][0])
                if road_source[0] == "local"
                else read_postgis_clean(road_source[1][0], engine, schema)
            )

            # CRS
            gdf = gdf.to_crs(gdf.estimate_utm_crs())
            poi_gdf = poi_gdf.to_crs(gdf.crs)
            road_gdf = road_gdf.to_crs(gdf.crs)
            status_var.set("Preparing POIs...")
            progress_win.update_idletasks()


            poi_gdf["_fclass_norm"] = poi_gdf["fclass"].str.lower().str.strip()
            poi_types = sorted(
                t for t in poi_gdf["_fclass_norm"].unique()
                if t in ALLOWED_FCLASS
            )

            poi_coords = {
                t: np.array([[p.x, p.y] for p in poi_gdf[poi_gdf["_fclass_norm"] == t].geometry])
                for t in poi_types
            }

            for t in ALLOWED_FCLASS:
                for i in range(1, 4):
                    gdf[f"{t.upper()}{i}"] = np.nan

            output_path = (
                os.path.join(output_mode[1], "parcels_with_poi_distances.shp")
                if output_mode[0] == "local"
                else None
            )

            status_var.set("Computing network distances...")
            eta_var.set("ETA: calculating...")
            progress_bar["value"] = 0
            progress_win.update_idletasks()

            run_cpu_parallel_with_progress(
                gdf, poi_gdf, road_gdf,
                poi_types, poi_coords,
                output_path,
                progress_bar, status_var, eta_var,
                stop_flag
            )


            if not stop_flag["stop"]:
                if output_mode[0] == "local":
                    messagebox.showinfo("Success", "✅ Processing complete!")
                else:
                    all_tables = fetch_tables(schema)
                    table_action = "replaced" if target_table in all_tables else "new"

                    gdf.to_postgis(
                        target_table,
                        engine,
                        schema=schema,
                        if_exists="replace",
                        index=False
                    )

                    messagebox.showinfo(
                        "Success",
                        f"✅ Updated DB table: {target_table} ({table_action})"
                    )

        except Exception as e:
            messagebox.showerror("Error", str(e))
        finally:
            progress_win.destroy()
            root._poi_progress_open = False


    root.after(100, task)

 


# -----------------------------------
# Entry point for MAIN3 / EXE
# -----------------------------------
def main(existing_root=None):
    global root

    if existing_root:
        root = existing_root
    else:
        root = tk.Tk()
        root.withdraw()   # hide empty root window

    select_barangay_window(root)
    root.mainloop()

if __name__ == "__main__":
    main()
