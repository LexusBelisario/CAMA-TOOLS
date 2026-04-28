root = None

import os
import re
import tkinter as tk
from tkinter import ttk
from tkinter import filedialog, messagebox, Listbox
import geopandas as gpd
from shapely.geometry import Point, LineString, MultiPolygon
from shapely.ops import nearest_points
from scipy.spatial import cKDTree
import subprocess
import json
from sqlalchemy import create_engine, text, inspect
import psycopg2

# ----------------- CONFIG -----------------
GM_EXE_PATH = r"C:\\Program Files\\GlobalMapper26.1_64bit\\global_mapper.exe"
CREDENTIALS_FILE = "pg_credentials.json"

barangay_source = None
road_source = None
output_mode = None

# ----------------- HELPERS -----------------
def load_db_credentials():
    try:
        with open(CREDENTIALS_FILE, "r") as f:
            return json.load(f)
    except:
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
        tables = [row[0] for row in cur.fetchall()]
        conn.close()
        return tables
    except Exception as e:
        messagebox.showerror("DB Error", str(e))
        return []

def normalize_name(name: str) -> str:
    return re.sub(r'[^a-z]', '', name.lower())

def find_matching_table(local_name, schema):
    all_tables = fetch_tables(schema)
    lname = normalize_name(local_name)
    for t in all_tables:
        tnorm = normalize_name(t)
        if lname in tnorm or tnorm in lname:
            return t
    return None

def get_geometry_column(table_name, engine, schema):
    try:
        with engine.connect() as conn:
            query = text("""
                SELECT f_geometry_column
                FROM geometry_columns
                WHERE f_table_schema = :schema AND f_table_name = :table
            """)
            result = conn.execute(query, {"schema": schema, "table": table_name})
            row = result.fetchone()
            return row[0] if row else None
    except Exception as e:
        print(f"❌ Error fetching geometry column: {e}")
        return None

def read_postgis_clean(table, engine, schema):
    """Read PostGIS table with only one geometry column (avoid geom as text)."""
    geom_col = get_geometry_column(table, engine, schema)
    if not geom_col:
        raise ValueError(f"No geometry column found in {table}")

    insp = inspect(engine)
    cols = [c['name'] for c in insp.get_columns(table, schema=schema) if c['name'] != geom_col]

    col_str = ", ".join([f'"{c}"' for c in cols]) if cols else ""
    if col_str:
        query = f'SELECT {col_str}, "{geom_col}" AS geometry FROM "{schema}"."{table}"'
    else:
        query = f'SELECT "{geom_col}" AS geometry FROM "{schema}"."{table}"'

    return gpd.read_postgis(query, engine, geom_col="geometry")

def fix_geometry(geom):
    if geom is None:
        return None
    try:
        if not geom.is_valid:
            geom = geom.buffer(0)
        if isinstance(geom, MultiPolygon):
            return max(geom.geoms, key=lambda a: a.area)
        return geom
    except Exception:
        return None

# ----------------- CRS UTILITY -----------------
def get_prs92_zone(gdf):
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326)
    lon = gdf.unary_union.centroid.x
    if lon < 118:
        return 3121
    elif lon < 120:
        return 3122
    elif lon < 122:
        return 3123
    elif lon < 124:
        return 3124
    else:
        return 3125

# ----------------- MAIN PROCESS -----------------
def process(barangay_gdf, road_gdf, source_name="", progress_cb=None):
    original_crs = barangay_gdf.crs
    barangay_gdf["geometry"] = barangay_gdf["geometry"].apply(fix_geometry)
    barangay_gdf = barangay_gdf[barangay_gdf["geometry"].notnull()]
    if barangay_gdf.empty:
        raise ValueError(f"All geometries invalid in {source_name}")

    zone_epsg = get_prs92_zone(barangay_gdf)
    print(f"🌍 [{source_name}] Reprojecting to EPSG:{zone_epsg}...")
    barangay_gdf = barangay_gdf.to_crs(epsg=zone_epsg)
    road_gdf = road_gdf.to_crs(epsg=zone_epsg)

    # Extract road segments
    segment_geoms, segment_midpoints = [], []
    for geom in road_gdf.geometry:
        if geom is None or geom.is_empty: 
            continue
        parts = geom.geoms if geom.geom_type in ['MultiLineString', 'GeometryCollection'] else [geom]
        for ls in parts:
            if ls.geom_type != "LineString": continue
            coords = list(ls.coords)
            for i in range(len(coords)-1):
                seg = LineString([coords[i], coords[i+1]])
                segment_geoms.append(seg)
                midpoint = seg.interpolate(0.5, normalized=True)
                segment_midpoints.append((midpoint.x, midpoint.y))

    if not segment_midpoints:
        if progress_cb:
            for _ in range(len(barangay_gdf)):
                progress_cb(1)

        barangay_gdf["ROAD_WIDTH"] = None
        if original_crs:
            barangay_gdf = barangay_gdf.to_crs(original_crs)
        return barangay_gdf

    tree = cKDTree(segment_midpoints)
    road_widths = []
    for idx, poly in enumerate(barangay_gdf.geometry):
        if progress_cb:
            progress_cb(1)

        if poly is None or poly.is_empty:
            road_widths.append(None)
            continue

        if not poly.is_valid:
            poly = poly.buffer(0)
            if poly.is_empty: 
                road_widths.append(None); continue
        boundary = poly.boundary
        coords = list(boundary.coords) if boundary.geom_type=="LineString" else [c for g in boundary.geoms for c in g.coords]
        if not coords: 
            road_widths.append(None); continue
        dists, indices = tree.query(coords)
        nearest_segment = segment_geoms[indices[dists.argmin()]]
        nearest_point = Point(coords[dists.argmin()])
        nearest_on_road = nearest_segment.interpolate(nearest_segment.project(nearest_point))
        nearest_on_feature = nearest_points(nearest_on_road, boundary)[1]
        road_widths.append(nearest_on_road.distance(nearest_on_feature) * 2)
    barangay_gdf["ROAD_WIDTH"] = road_widths
    if original_crs: barangay_gdf = barangay_gdf.to_crs(original_crs)
    return barangay_gdf

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
        global barangay_source
        files = filedialog.askopenfilenames(filetypes=[("Shapefiles", "*.shp")])
        if files:
            barangay_source = ("local", files)
            print("✅ Barangay source set:", barangay_source)
            win.destroy()
            select_road_window(root)

    def pick_db():
        global barangay_source
        creds = load_db_credentials()
        tables = fetch_tables(creds["schema"])

        db_win = tk.Toplevel(root)
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
                print("✅ Barangay source set:", barangay_source)
                db_win.destroy()
                win.destroy()
                select_road_window(root)

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
        global output_mode, barangay_source, road_source
        if not barangay_source or not road_source:
            messagebox.showerror("Error", "Barangay and Road must be selected first.")
            return
        out_dir = filedialog.askdirectory()
        if out_dir:
            output_mode = ("local", out_dir)
            print("✅ Output mode set:", output_mode)
            win.destroy()
            run_processing()

    def save_db():
        global output_mode, barangay_source, road_source
        if not barangay_source or not road_source:
            messagebox.showerror("Error", "Barangay and Road must be selected first.")
            return
        output_mode = ("db", None)
        print("✅ Output mode set:", output_mode)
        win.destroy()
        run_processing()

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

# ----------------- PROGRESS WINDOW -----------------
def create_progress_window(root, total):
    win = tk.Toplevel(root)
    win.title("Processing...")
    win.geometry("420x160")
    win.resizable(False, False)

    lbl = tk.Label(win, text="Starting...", wraplength=380)
    lbl.pack(pady=10)

    bar = ttk.Progressbar(
        win, orient="horizontal", length=360,
        mode="determinate", maximum=total
    )
    bar.pack(pady=10)

    count_lbl = tk.Label(win, text=f"0 / {total}")
    count_lbl.pack()

    win.update_idletasks()
    return win, lbl, bar, count_lbl


def update_progress(win, lbl, bar, count_lbl, step, total, msg):
    lbl.config(text=msg)
    bar["value"] = step
    count_lbl.config(text=f"{step} / {total}")
    win.update_idletasks()
    win.update()


# ----------------- PROCESSING -----------------
def run_processing():
    global barangay_source, road_source, output_mode
    if not barangay_source or not road_source or not output_mode:
        messagebox.showerror("Error","Selections incomplete (Barangay, Road, Output required).")
        return

    creds = load_db_credentials()
    if not creds:
        return
    schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    )

    # --- Load road layer ---
    if road_source[0] == "local":
        road_gdf = gpd.read_file(road_source[1][0])
    else:
        road_table = road_source[1][0]
        road_gdf = read_postgis_clean(road_table, engine, schema)

    # count total features (local or db)
    total_features = 0
    if barangay_source[0] == "local":
        for p in barangay_source[1]:
            total_features += len(gpd.read_file(p))
    else:
        for t in barangay_source[1]:
            total_features += len(read_postgis_clean(t, engine, schema))

    progress_win, progress_lbl, progress_bar, progress_count = create_progress_window(root, total_features)

    progress_win.transient(root)
    progress_win.grab_set()

    progress_win.update_idletasks()
    progress_win.deiconify()
    progress_win.lift()
    progress_win.focus_force()
    progress_win.attributes("-topmost", True)
    progress_win.after(100, lambda: progress_win.attributes("-topmost", False))

    current_step = 0

    def progress_cb(_):
        nonlocal current_step
        current_step += 1
        update_progress(
            progress_win,
            progress_lbl,
            progress_bar,
            progress_count,
            current_step,
            total_features,
            f"Processing feature {current_step}"
        )



    # --- Process barangays ---
    if barangay_source[0] == "local":
        for path in barangay_source[1]:
            b_gdf = gpd.read_file(path)
            b_gdf = process(b_gdf, road_gdf, os.path.basename(path), progress_cb)
            if output_mode[0] == "local":
                out = os.path.join(output_mode[1], os.path.basename(path))
                b_gdf.to_file(out)
                print(f"✅ Saved {out}")
            else:
                local_name = os.path.splitext(os.path.basename(path))[0]
                match = find_matching_table(local_name, schema)
                table_action = "replaced" if match else "new"
                table = match if match else local_name.lower()
                b_gdf.to_postgis(table, engine, schema=schema, if_exists="replace", index=False)
                print(f"🔄 Saved to DB: {table} ({table_action})")

                # ---------------- 🟢 CAMA Table + Transaction Log ----------------
                with engine.begin() as conn:
                    # Ensure CAMA_Table exists
                    conn.execute(text(f"""
                        CREATE TABLE IF NOT EXISTS "{schema}"."CAMA_Table" (
                            id SERIAL PRIMARY KEY,
                            PIN TEXT UNIQUE NOT NULL
                        );
                    """))

                    # Ensure column for road width
                    conn.execute(text(f"""
                        DO $$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1 FROM information_schema.columns
                                WHERE table_schema='{schema}'
                                  AND table_name='CAMA_Table'
                                  AND column_name='road_width'
                            ) THEN
                                EXECUTE 'ALTER TABLE "{schema}"."CAMA_Table" ADD COLUMN "road_width" NUMERIC';
                            END IF;
                        END $$;
                    """))

                    # Update or insert per PIN
                    pin_field = next((c for c in b_gdf.columns if c.lower() == "pin"), None)
                    if pin_field:
                        for _, row in b_gdf.iterrows():
                            sql = f"""
                                INSERT INTO "{schema}"."CAMA_Table" (PIN, road_width)
                                VALUES (:pin, :rw)
                                ON CONFLICT (PIN) DO UPDATE
                                SET road_width = EXCLUDED.road_width;
                            """
                            params = {
                                "pin": str(row[pin_field]),
                                "rw": float(row["ROAD_WIDTH"]) if row["ROAD_WIDTH"] is not None else None,
                            }
                            conn.execute(text(sql), params)

                    # Ensure log table exists
                    conn.execute(text(f"""
                        CREATE TABLE IF NOT EXISTS "{schema}"."CAMA_Transaction_Log" (
                            id SERIAL PRIMARY KEY,
                            table_name TEXT,
                            cama_tool TEXT,
                            cama_fields TEXT,
                            transaction_date_time TIMESTAMP DEFAULT NOW()
                        );
                    """))

                    # Log transaction with (new) or (replaced)
                    conn.execute(text(f"""
                        INSERT INTO "{schema}"."CAMA_Transaction_Log"
                        (table_name, cama_tool, cama_fields)
                        VALUES (:tbl, :type, :details);
                    """), {
                        "tbl": f"{table} ({table_action})",
                        "type": "road_width",
                        "details": "ROAD_WIDTH"
                    })

    else:  # --- barangay_source == "db" ---
        for table in barangay_source[1]:
            b_gdf = read_postgis_clean(table, engine, schema)
            b_gdf = process(b_gdf, road_gdf, table, progress_cb)
            if output_mode[0] == "local":
                out = os.path.join(output_mode[1], f"{table}.shp")
                b_gdf.to_file(out)
                print(f"✅ Saved {out}")
            else:
                # Check if table already exists before overwrite
                all_tables = fetch_tables(schema)
                table_action = "replaced" if table in all_tables else "new"

                b_gdf.to_postgis(table, engine, schema=schema, if_exists="replace", index=False)
                print(f"🔄 Updated DB table: {table} ({table_action})")

                # ---------------- 🟢 CAMA Table + Transaction Log ----------------
                with engine.begin() as conn:
                    conn.execute(text(f"""
                        CREATE TABLE IF NOT EXISTS "{schema}"."CAMA_Table" (
                            id SERIAL PRIMARY KEY,
                            PIN TEXT UNIQUE NOT NULL
                        );
                    """))
                    conn.execute(text(f"""
                        DO $$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1 FROM information_schema.columns
                                WHERE table_schema='{schema}'
                                  AND table_name='CAMA_Table'
                                  AND column_name='road_width'
                            ) THEN
                                EXECUTE 'ALTER TABLE "{schema}"."CAMA_Table" ADD COLUMN "road_width" NUMERIC';
                            END IF;
                        END $$;
                    """))
                    pin_field = next((c for c in b_gdf.columns if c.lower() == "pin"), None)
                    if pin_field:
                        for _, row in b_gdf.iterrows():
                            sql = f"""
                                INSERT INTO "{schema}"."CAMA_Table" (PIN, road_width)
                                VALUES (:pin, :rw)
                                ON CONFLICT (PIN) DO UPDATE
                                SET road_width = EXCLUDED.road_width;
                            """
                            params = {
                                "pin": str(row[pin_field]),
                                "rw": float(row["ROAD_WIDTH"]) if row["ROAD_WIDTH"] is not None else None,
                            }
                            conn.execute(text(sql), params)

                    conn.execute(text(f"""
                        CREATE TABLE IF NOT EXISTS "{schema}"."CAMA_Transaction_Log" (
                            id SERIAL PRIMARY KEY,
                            table_name TEXT,
                            cama_tool TEXT,
                            cama_fields TEXT,
                            transaction_date_time TIMESTAMP DEFAULT NOW()
                        );
                    """))
                    conn.execute(text(f"""
                        INSERT INTO "{schema}"."CAMA_Transaction_Log"
                        (table_name, cama_tool, cama_fields)
                        VALUES (:tbl, :type, :details);
                    """), {
                        "tbl": f"{table} ({table_action})",
                        "type": "road_width",
                        "details": "ROAD_WIDTH"
                    })
    progress_win.destroy()

    messagebox.showinfo("Success", "✅ Processing done and logged to CAMA!")


# ----------------- MAIN -----------------
def main():
    global root
    root = tk.Tk()
    root.withdraw()
    select_barangay_window(root)
    root.mainloop()

if __name__=="__main__":
    main()
