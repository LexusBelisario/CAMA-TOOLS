import os
import re
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox
import geopandas as gpd
import pandas as pd
import subprocess
import json
import psycopg2
from shapely.validation import make_valid
from shapely.geometry import Point
from itertools import combinations
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

    # Tk titlebar fallback (important)
    if os.path.exists(png):
        try:
            img = tk.PhotoImage(file=png)
            win.iconphoto(True, img)
            win._icon_ref = img  # prevent garbage collection
        except Exception:
            pass


GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"
CREDENTIALS_FILE = "pg_credentials.json"

barangay_source = None
road_source = None
output_mode = None

# ----------------- CRS UTILITY -----------------
def get_prs92_zone(gdfs):
    centroids = []
    for gdf in gdfs:
        if gdf.crs is None:
            gdf = gdf.set_crs(epsg=4326)
        gdf_wgs84 = gdf.to_crs(epsg=4326)
        centroids.append(gdf_wgs84.unary_union.centroid)
    avg_lon = sum(c.x for c in centroids) / len(centroids)
    if avg_lon < 118: return 3121
    elif avg_lon < 120: return 3122
    elif avg_lon < 122: return 3123
    elif avg_lon < 124: return 3124
    else: return 3125

# ----------------- Geometry Fix -----------------
def fix_geometry(geom):
    if geom is None or geom.is_empty: return None
    try:
        if not geom.is_valid: geom = geom.buffer(0)
        if not geom.is_valid: geom = make_valid(geom)
        return geom if not geom.is_empty else None
    except: return None

# ----------------- Lot Location Logic -----------------

# CORNER_TOLERANCE_METERS: radius of the buffer applied to the road intersection
# point. Must be >= half the typical ROW width plus a digitization margin.
# Default 15m per industry standard (Gemini / mass appraisal reference).
# QA team may adjust this value when final threshold parameters are confirmed.
CORNER_TOLERANCE_METERS = 15.0

def _is_corner_lot(parcel_geom, road_geom_list, tolerance=CORNER_TOLERANCE_METERS):
    """
    Intersection Buffer Test (Option C — industry standard for mass appraisal).

    For every unique pair of road geometries associated with the parcel:
      1. Test if the two roads intersect anywhere.
      2. If yes, extract the intersection point (junction).
      3. Buffer that junction by `tolerance` meters.
      4. If the parcel intersects that buffer → parcel is physically at the
         corner → Corner Lot.

    If no pair passes the buffer test, the parcel spans between two roads
    that either don't intersect near it, or don't intersect at all
    → Through-Lot / Road Lot.

    All geometries must already be in a projected metric CRS (PRS92)
    before this function is called.

    Parameters
    ----------
    parcel_geom   : Shapely geometry  — the parcel polygon (projected)
    road_geom_list: list of Shapely geometries — road centerlines (projected)
    tolerance     : float — buffer radius in meters (default 15.0)

    Returns
    -------
    True  → Corner Lot
    False → Road Lot (through-lot or single-frontage)
    """
    for road_a, road_b in combinations(road_geom_list, 2):
        # Skip if either geometry is missing
        if road_a is None or road_b is None:
            continue
        if road_a.is_empty or road_b.is_empty:
            continue

        # Step 1: Do these two roads intersect at all?
        if not road_a.intersects(road_b):
            continue  # No intersection → not a corner candidate for this pair

        # Step 2: Get the intersection geometry (point, multipoint, or segment)
        intersection = road_a.intersection(road_b)
        if intersection is None or intersection.is_empty:
            continue

        # Step 3: Reduce intersection to one or more junction points
        geom_type = intersection.geom_type
        if geom_type == "Point":
            junction_points = [intersection]
        elif geom_type == "MultiPoint":
            junction_points = list(intersection.geoms)
        else:
            # Overlapping segments (T-junction digitized as shared segment),
            # or GeometryCollection — use centroid as representative point
            junction_points = [intersection.centroid]

        # Step 4: Intersection Buffer Test
        # Is the parcel physically located at this junction?
        for jpt in junction_points:
            if parcel_geom.intersects(jpt.buffer(tolerance)):
                return True  # Parcel is at the corner → Corner Lot

    return False  # No corner condition found → Road Lot / Through-Lot


def _deduplicate_road_ids(id_list, road_name_map):
    """
    Optional deduplication: group road IDs by road_name so that two segments
    of the same named road are treated as one road.

    If road_name_map is empty (column absent or all NULL), returns id_list
    unchanged — geometry-only path is used.

    Returns a list of representative IDs (one per unique road name group).
    For unnamed roads (NULL / empty name), each ID is kept as its own group.
    """
    if not road_name_map:
        return id_list  # No name data → use all IDs as-is

    seen_names = set()
    deduped = []
    for rid in id_list:
        name = road_name_map.get(rid, "").strip() if road_name_map.get(rid) else ""
        if name:
            if name not in seen_names:
                seen_names.add(name)
                deduped.append(rid)
            # else: same named road already represented → skip this ID
        else:
            # NULL or empty name → treat as its own unique road
            deduped.append(rid)
    return deduped


def label_lot_location(val):
    return {0: "Inner Lot", 1: "Road Lot", 2: "Corner Lot"}.get(val, "Unknown")


def process_lot_location(barangay_gdf, road_gdf, source_name=""):
    orig_crs = barangay_gdf.crs
    zone_epsg = get_prs92_zone([barangay_gdf, road_gdf])
    print(f"🌍 [{source_name}] Using EPSG:{zone_epsg}")
    brgy_proj = barangay_gdf.to_crs(epsg=zone_epsg)
    road_proj = road_gdf.to_crs(epsg=zone_epsg)

    # ------------------------------------------------------------------
    # Filter: public roads only for Corner Lot classification.
    # Private Roads, Alleys, Driveways, and Bridges are excluded because
    # corner lot premium (per BLGF Mass Appraisal Guidebook) applies only
    # to publicly accessible roads (National, Provincial, Barangay).
    # Internal subdivision roads and alleys do not generate corner influence.
    # Falls back to full road layer if no road_type column exists.
    # ------------------------------------------------------------------
    PUBLIC_ROAD_TYPES = {
        "National Road", "Provincial Road", "Barangay Road",
        "Municipal Road", "City Road"
    }
    road_type_col = next(
        (c for c in road_proj.columns if c.lower() in ("road_type", "roadtype", "highway")),
        None
    )
    if road_type_col:
        original_count = len(road_proj)
        road_proj = road_proj[
            road_proj[road_type_col].isin(PUBLIC_ROAD_TYPES)
        ].copy()
        filtered_count = len(road_proj)
        print(f"ℹ️  [{source_name}] Road type filter: {filtered_count}/{original_count} "
              f"public roads retained (column: '{road_type_col}')")
        if filtered_count == 0:
            print(f"⚠️  [{source_name}] No public roads found after filter — "
                  f"falling back to full road layer.")
            road_proj = road_gdf.to_crs(epsg=zone_epsg)
    else:
        print(f"ℹ️  [{source_name}] No road_type column found — using full road layer.")

    # Assign a row-level integer ROAD_ID if not already present
    if "ROAD_ID" not in road_proj.columns:
        road_proj = road_proj.copy()
        road_proj["ROAD_ID"] = range(len(road_proj))

    # ------------------------------------------------------------------
    # Build lookup dicts from road_proj (all in projected metric CRS)
    # ------------------------------------------------------------------

    # road_id (int) → road geometry (Shapely, projected)
    road_geom_map = {
        int(row["ROAD_ID"]): row["geometry"]
        for _, row in road_proj.iterrows()
        if row["geometry"] is not None and not row["geometry"].is_empty
    }

    # road_id (int) → road_name (str or "")
    # Only populated if a non-trivial road_name column exists
    road_name_col = next(
        (c for c in road_proj.columns
         if c.lower() in ("road_name", "roadname", "name", "street", "road_no")),
        None
    )
    road_name_map = {}
    if road_name_col:
        non_null_count = road_proj[road_name_col].notna().sum()
        if non_null_count > 0:
            road_name_map = {
                int(row["ROAD_ID"]): (str(row[road_name_col]).strip()
                                      if pd.notna(row[road_name_col]) else "")
                for _, row in road_proj.iterrows()
            }
            print(f"ℹ️  [{source_name}] road_name column '{road_name_col}' found "
                  f"({non_null_count} non-null values) — segment deduplication enabled.")
        else:
            print(f"ℹ️  [{source_name}] road_name column '{road_name_col}' is all NULL "
                  f"— geometry-only classification.")
    else:
        print(f"ℹ️  [{source_name}] No road_name column found "
              f"— geometry-only classification.")

    # ------------------------------------------------------------------
    # Spatial join: which roads does each parcel's 10m buffer touch?
    # (unchanged from original — only the classification step changes)
    # ------------------------------------------------------------------
    road_buffer = road_proj.copy()
    road_buffer["geometry"] = road_proj.geometry.buffer(10, cap_style=2)
    joined = gpd.sjoin(brgy_proj, road_buffer[["ROAD_ID", "geometry"]],
                       how="left", predicate="intersects")
    grouped = joined.groupby(joined.index).agg({
        "ROAD_ID": lambda x: ",".join(
            sorted(set(str(int(v)) for v in x if pd.notna(v)))
        )
    })

    result = brgy_proj.copy()
    result["ROAD_ID"] = result.index.map(grouped["ROAD_ID"].to_dict()).fillna("")

    # ------------------------------------------------------------------
    # Classification — replaces the old compute_lot_location() string check
    # ------------------------------------------------------------------
    lot_locations = []
    for idx, row in result.iterrows():
        road_id_str = row["ROAD_ID"]

        # --- Inner Lot: no road contact ---
        if not road_id_str or not road_id_str.strip():
            lot_locations.append(0)
            continue

        id_list = [int(x) for x in road_id_str.split(",") if x.strip()]

        # --- Road Lot: only one road feature touched ---
        if len(id_list) == 1:
            lot_locations.append(1)
            continue

        # --- 2+ road features touched: need to determine Corner vs Road ---

        # Step 1: Optional deduplication by road_name
        # (reduces same-road multi-segment false positives)
        deduped_ids = _deduplicate_road_ids(id_list, road_name_map)

        # After deduplication, if only one unique road remains → Road Lot
        if len(deduped_ids) == 1:
            lot_locations.append(1)
            continue

        # Step 2: Intersection Buffer Test (Option C)
        # Retrieve the actual road geometries for the deduped IDs
        road_geoms = [road_geom_map[rid] for rid in deduped_ids if rid in road_geom_map]

        parcel_geom = row["geometry"]
        if parcel_geom is None or parcel_geom.is_empty:
            lot_locations.append(1)  # Can't test → default Road Lot (conservative)
            continue

        if _is_corner_lot(parcel_geom, road_geoms, tolerance=CORNER_TOLERANCE_METERS):
            lot_locations.append(2)  # Corner Lot
        else:
            lot_locations.append(1)  # Road Lot (through-lot / double frontage)

    result["LOT_LOCATION"] = lot_locations
    result["LOT_LABEL"] = result["LOT_LOCATION"].apply(label_lot_location)

    if orig_crs:
        result = result.to_crs(orig_crs)
    return result

# ----------------- DB Helpers -----------------
def load_db_credentials():
    try:
        with open(CREDENTIALS_FILE,"r") as f: return json.load(f)
    except: return None

def get_geometry_column(table_name, engine, schema):
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT f_geometry_column FROM geometry_columns
                WHERE f_table_schema=:schema AND f_table_name=:table
            """),{"schema":schema,"table":table_name}).fetchone()
            return row[0] if row else None
    except: return None

def read_postgis_clean(table, engine, schema):
    geom_col = get_geometry_column(table,engine,schema)
    insp = inspect(engine)
    cols = [c["name"] for c in insp.get_columns(table,schema=schema) if c["name"]!=geom_col]
    col_str = ", ".join([f'"{c}"' for c in cols]) if cols else ""
    q = f'SELECT {col_str+", " if col_str else ""}"{geom_col}" AS geometry FROM "{schema}"."{table}"'
    return gpd.read_postgis(q, engine, geom_col="geometry")

def normalize_name(name): return re.sub(r'[^a-z]', '', name.lower())

def fetch_tables(schema):
    creds=load_db_credentials()
    if not creds: return []
    try:
        conn=psycopg2.connect(host=creds["host"],port=creds["port"],dbname=creds["database"],
                              user=creds["username"],password=creds["password"])
        cur=conn.cursor()
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s;",(schema,))
        return [r[0] for r in cur.fetchall()]
    except: return []

def find_matching_table(local_name, schema):
    lname = normalize_name(local_name)
    for t in fetch_tables(schema):
        if lname in normalize_name(t) or normalize_name(t) in lname:
            return t
    return None

# ----------------- Tkinter Selection -----------------
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
                print("✅ Barangay (DB):", barangay_source)
                db_win.destroy()
                win.destroy()
                select_road_window(root)

        tk.Button(db_win, text="Select", command=submit).pack(pady=5)

    btn_frame = tk.Frame(win)
    btn_frame.pack(padx=25, pady=10)

    tk.Button(btn_frame, text="Select Local File", width=18, command=pick_local)\
        .pack(side=tk.LEFT, padx=5)

    tk.Button(btn_frame, text="Select Database Table", width=18, command=pick_db)\
        .pack(side=tk.LEFT, padx=5)


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
            print("✅ Road (Local):", road_source)
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
                print("✅ Road (DB):", road_source)
                db_win.destroy()
                win.destroy()
                select_output_window(root)

        tk.Button(db_win, text="Select", command=submit).pack(pady=5)

    btn_frame = tk.Frame(win)
    btn_frame.pack(padx=25, pady=10)

    tk.Button(btn_frame, text="Select Local File", width=18, command=pick_local)\
        .pack(side=tk.LEFT, padx=5)

    tk.Button(btn_frame, text="Select Database Table", width=18, command=pick_db)\
        .pack(side=tk.LEFT, padx=5)


def select_output_window(root):
    win = tk.Toplevel(root)
    apply_icon(win)
    win.title("Select Output Destination")
    win.resizable(False, False)

    def save_local():
        global output_mode, barangay_source, road_source
        if not barangay_source or not road_source:
            messagebox.showerror("Error", "Barangay and Road must be selected first.")
            return
        out_dir = filedialog.askdirectory()
        if out_dir:
            output_mode = ("local", out_dir)
            print("✅ Output (Local):", output_mode)
            win.destroy()
            run_processing()

    def save_db():
        global output_mode, barangay_source, road_source
        if not barangay_source or not road_source:
            messagebox.showerror("Error", "Barangay and Road must be selected first.")
            return
        output_mode = ("db", None)
        print("✅ Output (DB):", output_mode)
        win.destroy()
        run_processing()

    btn_frame = tk.Frame(win)
    btn_frame.pack(padx=25, pady=10)

    tk.Button(btn_frame, text="Save to Local", width=18, command=save_local)\
        .pack(side=tk.LEFT, padx=5)

    tk.Button(btn_frame, text="Save to Database", width=18, command=save_db)\
        .pack(side=tk.LEFT, padx=5)

# ----------------- Processing -----------------
def run_processing():
    global barangay_source, road_source, output_mode
    if not barangay_source or not road_source or not output_mode:
        messagebox.showerror("Error","Selections incomplete (Barangay, Road, Output required)."); return

    creds=load_db_credentials(); schema=creds["schema"]
    engine=create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    )

    # Load road
    road_gdf = gpd.read_file(road_source[1][0]) if road_source[0]=="local" else read_postgis_clean(road_source[1][0],engine,schema)

    # Process barangays
    if barangay_source[0]=="local":
        for path in barangay_source[1]:
            brgy_gdf = gpd.read_file(path); brgy_gdf["geometry"]=brgy_gdf["geometry"].apply(fix_geometry)
            result = process_lot_location(brgy_gdf, road_gdf, os.path.basename(path))

            if output_mode[0]=="local":
                out=os.path.join(output_mode[1], f"{os.path.splitext(os.path.basename(path))[0]}_lot_location.shp")
                result.to_file(out)
                print(f"✅ Saved {out}")
            else:
                local_name = os.path.splitext(os.path.basename(path))[0]
                match = find_matching_table(local_name, schema)
                table = match if match else local_name.lower()
                result.to_postgis(table, engine, schema=schema, if_exists="replace", index=False)
                print(f"🔄 Saved to DB: {table}")

    else:
        for table in barangay_source[1]:
            brgy_gdf = read_postgis_clean(table, engine, schema); brgy_gdf["geometry"]=brgy_gdf["geometry"].apply(fix_geometry)
            result = process_lot_location(brgy_gdf, road_gdf, table)

            if output_mode[0]=="local":
                out=os.path.join(output_mode[1], f"{table}_lot_location.shp")
                result.to_file(out)
                print(f"✅ Saved {out}")
            else:
                result.to_postgis(table, engine, schema=schema, if_exists="replace", index=False)
                print(f"🔄 Updated DB table: {table}")

    messagebox.showinfo("Success","✅ Processing done!")

# ----------------- MAIN -----------------
def main():
    root = tk.Tk()
    apply_icon(root)
    root.withdraw()
    select_barangay_window(root)
    root.mainloop()


if __name__=="__main__":
    main()