from __future__ import annotations
import os
import re
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox
import geopandas as gpd
import psycopg2, json
from sqlalchemy import create_engine, text
from shapely.geometry import Point
import argparse

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
    """ Get absolute path to resource (PyInstaller-safe) """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def apply_icon(win):
    ico = resource_path("BLGF.ico")
    png = resource_path("BLGF.png")

    if os.path.exists(ico):
        try:
            win.iconbitmap(ico)
        except Exception:
            pass

    if os.path.exists(png):
        try:
            img = tk.PhotoImage(file=png)
            win.iconphoto(True, img)
            win._icon_ref = img  # prevent GC
        except Exception:
            pass


# -------------------- CONFIG --------------------
CREDENTIALS_FILE = "pg_credentials.json"

barangay_source = None
influence_source = None
output_mode = None

# Supported vector file extensions
VECTOR_FILETYPES = [
    ("Vector files", "*.shp *.gpkg"),
    ("Shapefiles", "*.shp"),
    ("GeoPackage", "*.gpkg"),
    ("All files", "*.*"),
]


# -------------------- DB HELPERS --------------------
def load_db_credentials():
    """Load pg_credentials.json safely."""
    if not os.path.exists(CREDENTIALS_FILE):
        messagebox.showerror(
            "Missing Credentials",
            f"⚠️ File not found: {os.path.abspath(CREDENTIALS_FILE)}\n\n"
            "Please create pg_credentials.json with host, port, database, username, password, and schema.",
        )
        return None
    try:
        with open(CREDENTIALS_FILE, "r") as f:
            creds = json.load(f)
        required = ["host", "port", "database", "username", "password", "schema"]
        for key in required:
            if key not in creds:
                messagebox.showerror("Invalid Credentials", f"Missing '{key}' in pg_credentials.json")
                return None
        return creds
    except Exception as e:
        messagebox.showerror("Credential Error", str(e))
        return None


def fetch_tables(schema):
    creds = load_db_credentials()
    if not creds:
        return []
    try:
        conn = psycopg2.connect(
            host=creds["host"],
            port=creds["port"],
            dbname=creds["database"],
            user=creds["username"],
            password=creds["password"],
        )
        cur = conn.cursor()
        cur.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema=%s ORDER BY table_name;
        """,
            (schema,),
        )
        tables = [row[0] for row in cur.fetchall()]
        conn.close()
        return tables
    except Exception as e:
        messagebox.showerror("DB Error", str(e))
        return []


def get_geom_column(engine, schema, table):
    """Detect the geometry column name from PostGIS system catalogs."""
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                SELECT f_geometry_column
                FROM geometry_columns
                WHERE f_table_schema = :schema AND f_table_name = :table;
            """
                ),
                {"schema": schema, "table": table},
            ).fetchone()
            if result:
                return result[0]
    except Exception:
        pass
    return "geometry"


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())


def find_matching_table(local_name, schema):
    all_tables = fetch_tables(schema)
    lname = normalize_name(local_name)
    for t in all_tables:
        tnorm = normalize_name(t)
        if lname in tnorm or tnorm in lname:
            return t
    return None


# -------------------- FILE READING --------------------
def read_vector_file(path: str) -> gpd.GeoDataFrame:
    """
    Read a vector file (SHP or GPKG) into a GeoDataFrame.
    For GPKG files that contain multiple layers, the first layer is used
    unless the filename stem matches a layer name exactly.
    """
    ext = os.path.splitext(path)[1].lower()

    if ext == ".gpkg":
        import fiona
        layers = fiona.listlayers(path)
        if not layers:
            raise ValueError(f"No layers found in GeoPackage: {path}")

        # Try to match layer name to file stem first
        stem = os.path.splitext(os.path.basename(path))[0].lower()
        matched_layer = next(
            (l for l in layers if l.lower() == stem), layers[0]
        )

        if len(layers) > 1:
            print(f"ℹ️  GPKG has {len(layers)} layers: {layers}. Using: '{matched_layer}'")

        return gpd.read_file(path, layer=matched_layer)

    # Default: let geopandas auto-detect (handles .shp and others)
    return gpd.read_file(path)


def get_local_name(path: str) -> str:
    """
    Extract a clean layer/table name from a file path.
    For GPKG, tries to use the matched layer name for consistency.
    """
    ext = os.path.splitext(path)[1].lower()
    stem = os.path.splitext(os.path.basename(path))[0]

    if ext == ".gpkg":
        try:
            import fiona
            layers = fiona.listlayers(path)
            if layers:
                matched = next(
                    (l for l in layers if l.lower() == stem.lower()), layers[0]
                )
                return matched
        except Exception:
            pass

    return stem


# -------------------- GEOMETRY / ATTRIBUTES --------------------
def ensure_geometry_column(gdf):
    if "geometry" not in gdf.columns and "geom" in gdf.columns:
        gdf = gdf.rename(columns={"geom": "geometry"}).set_geometry("geometry")
    elif gdf.geometry.name != "geometry":
        gdf = gdf.set_geometry(gdf.geometry.name)
        gdf = gdf.rename_geometry("geometry")
    return gdf


def detect_attr_name(gdf, name_guess: str):
    """
    Detect attribute column based on layer/table name.

    Example:
      FloodHazardMap  -> finds column containing 'flood'
      Landslide_Risk  -> finds column containing 'landslide'
    """

    norm_layer = normalize_name(name_guess)

    # 1️⃣ PRIMARY RULE: substring match using normalized names
    for col in gdf.columns:
        if col.lower() in ("geometry", "geom"):
            continue
        if normalize_name(col) in norm_layer or norm_layer in normalize_name(col):
            return col

    # 2️⃣ Exact name match (legacy behavior)
    for col in gdf.columns:
        if col.upper() == name_guess.upper():
            return col

    # 3️⃣ Elevation fallback
    for col in gdf.columns:
        if "ELEVATION" in col.upper():
            return col

    # 4️⃣ Last fallback: first non-geometry column
    non_geom_cols = [c for c in gdf.columns if c.lower() not in ("geometry", "geom")]
    if non_geom_cols:
        return non_geom_cols[0]

    raise ValueError(f"No suitable attribute column found for {name_guess}")


def transfer_attributes(barangay_gdf, influence_gdfs):
    for infl_gdf, attr_name in influence_gdfs:
        infl_clean = infl_gdf[[attr_name, "geometry"]].copy()
        infl_clean = infl_clean.rename(columns={attr_name: "joined_attr"})

        centroids = barangay_gdf.geometry.centroid
        centroid_gdf = gpd.GeoDataFrame(geometry=centroids, crs=barangay_gdf.crs)

        joined = gpd.sjoin(centroid_gdf, infl_clean, how="left", predicate="within")
        joined = joined.loc[:, ~joined.columns.duplicated(keep="first")]

        barangay_gdf[attr_name] = joined["joined_attr"].reset_index(drop=True)
    return barangay_gdf


# -------------------- PROCESSING --------------------
def run_processing():
    global barangay_source, influence_source, output_mode

    # 🧠 Debug info (helps verify what's actually set)
    print("=== PROCESSING START ===")
    print("Barangay Source:", barangay_source)
    print("Influence Source:", influence_source)
    print("Output Mode:", output_mode)
    print("=========================")

    # ✅ safer validation
    if not barangay_source or not isinstance(barangay_source, tuple) or not barangay_source[1]:
        messagebox.showerror("Error", "Barangay source not selected properly.")
        return
    if not influence_source or not isinstance(influence_source, tuple) or not influence_source[1]:
        messagebox.showerror("Error", "Influence map source not selected properly.")
        return
    if not output_mode:
        messagebox.showerror("Error", "Output destination not selected.")
        return

    creds = load_db_credentials()
    if not creds:
        return
    schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    )

    influence_gdfs = []
    added_fields = []

    # --- Load influence layers ---
    if influence_source[0] == "local":
        for path in influence_source[1]:
            gdf = read_vector_file(path).to_crs(epsg=3857)
            gdf = ensure_geometry_column(gdf)
            name_guess = get_local_name(path)
            attr_name = detect_attr_name(gdf, name_guess)
            influence_gdfs.append((gdf, attr_name))
            added_fields.append(attr_name)
    else:
        for table in influence_source[1]:
            geom_col = get_geom_column(engine, schema, table)
            gdf = gpd.read_postgis(
                f'SELECT * FROM "{schema}"."{table}"', engine, geom_col=geom_col
            ).to_crs(epsg=3857)
            gdf = ensure_geometry_column(gdf)
            attr_name = detect_attr_name(gdf, table)
            influence_gdfs.append((gdf, attr_name))
            added_fields.append(attr_name)

    # --- Process Barangay ---
    sources = barangay_source[1]
    for src in sources:
        if barangay_source[0] == "local":
            local_name = get_local_name(src)
            b_gdf = read_vector_file(src).to_crs(epsg=3857)
        else:
            local_name = src
            geom_col = get_geom_column(engine, schema, src)
            b_gdf = gpd.read_postgis(
                f'SELECT * FROM "{schema}"."{src}"', engine, geom_col=geom_col
            ).to_crs(epsg=3857)

        b_gdf = ensure_geometry_column(b_gdf)
        b_gdf = transfer_attributes(b_gdf, influence_gdfs)

        # --- Save outputs ---
        if output_mode[0] == "local":
            out_dir = output_mode[1]
            out_path = os.path.join(out_dir, f"{local_name}.shp")

            # ===== HARDEN SHAPEFILE WRITE =====

            # 1️⃣ Ensure CRS exists
            if b_gdf.crs is None:
                raise RuntimeError("❌ Cannot write shapefile: CRS is None")

            # 2️⃣ Reproject explicitly (not inline)
            b_gdf = b_gdf.to_crs(epsg=4326)
            print("🧭 CRS before save:", b_gdf.crs)

            # 3️⃣ Fix invalid geometries (prevents DBF failure)
            if not b_gdf.is_valid.all():
                print("⚠️ Fixing invalid geometries")
                b_gdf["geometry"] = b_gdf.geometry.buffer(0)

            # 4️⃣ Remove existing shapefile parts (Windows lock safety)
            base = os.path.splitext(out_path)[0]
            for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg"]:
                try:
                    os.remove(base + ext)
                except FileNotFoundError:
                    pass

            # 5️⃣ Write shapefile with explicit driver + encoding
            b_gdf.to_file(
                out_path,
                driver="ESRI Shapefile",
                encoding="UTF-8"
            )

            # 6️⃣ Validate output (NO silent failures)
            required = [".shp", ".shx", ".dbf", ".prj"]
            missing = [ext for ext in required if not os.path.exists(base + ext)]

            if missing:
                raise RuntimeError(
                    f"❌ Shapefile write incomplete. Missing: {', '.join(missing)}"
                )

            print(f"✅ Valid shapefile written: {out_path}")

        else:
            # --- DB OUTPUT RULES ---
            if barangay_source[0] == "db":
                # 🔥 DB → DB : replace the SAME table
                target_table = local_name
                table_action = "replaced"
            else:
                # Local → DB : create / replace by filename
                target_table = local_name.lower()
                table_action = "new"

            print(f"🗂️ Saving to DB: {target_table} ({table_action})")

            b_gdf.to_postgis(
                target_table,
                engine,
                schema=schema,
                if_exists="replace",
                index=False
            )

            # --------------- 🟢 CAMA Table and Log --------------- # 
            with engine.begin() as conn:
                # Ensure CAMA_Table exists
                conn.execute(
                    text(
                        f"""
                    CREATE TABLE IF NOT EXISTS "{schema}"."CAMA_Table" (
                        id SERIAL PRIMARY KEY,
                        PIN TEXT UNIQUE NOT NULL
                    );
                """
                    )
                )

                # Add missing columns as NUMERIC
                for col in added_fields:
                    conn.execute(
                        text(
                            f"""
                        DO $$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1 FROM information_schema.columns
                                WHERE table_schema='{schema}'
                                  AND table_name='CAMA_Table'
                                  AND column_name='{col.lower()}'
                            ) THEN
                                EXECUTE 'ALTER TABLE "{schema}"."CAMA_Table" ADD COLUMN "{col.lower()}" NUMERIC';
                            END IF;
                        END $$;
                    """
                        )
                    )

                # Insert or update PIN-based values using named parameters
                pin_field = next((c for c in b_gdf.columns if c.lower() == "pin"), None)
                if pin_field:
                    for _, row in b_gdf.iterrows():
                        insert_cols = ["PIN"] + [c.lower() for c in added_fields]
                        insert_placeholders = [f":{c.lower()}" for c in insert_cols]
                        update_assignments = [f'"{c.lower()}" = :{c.lower()}_upd' for c in added_fields]

                        sql = f"""
                        INSERT INTO "{schema}"."CAMA_Table" ({', '.join(insert_cols)})
                        VALUES ({', '.join(insert_placeholders)})
                        ON CONFLICT (PIN) DO UPDATE
                        SET {', '.join(update_assignments)};
                        """

                        params = {}
                        params["pin"] = str(row[pin_field])
                        
                        for c in added_fields:
                            if c in row:
                                try:
                                    params[c.lower()] = float(row[c])
                                    params[f"{c.lower()}_upd"] = float(row[c])
                                except (ValueError, TypeError):
                                    params[c.lower()] = None
                                    params[f"{c.lower()}_upd"] = None
                            else:
                                params[c.lower()] = None
                                params[f"{c.lower()}_upd"] = None

                        conn.execute(text(sql), params)

                # Ensure CAMA_Transaction_Log exists
                conn.execute(
                    text(
                        f"""
                    CREATE TABLE IF NOT EXISTS "{schema}"."CAMA_Transaction_Log" (
                        id SERIAL PRIMARY KEY,
                        table_name TEXT,
                        cama_tool TEXT,
                        cama_fields TEXT,
                        transaction_date_time TIMESTAMP DEFAULT NOW()
                    );
                """
                    )
                )

                # Log transaction
                conn.execute(
                    text(
                        f"""
                    INSERT INTO "{schema}"."CAMA_Transaction_Log" 
                    (table_name, cama_tool, cama_fields)
                    VALUES (:tbl, :tool, :details);
                """
                    ),
                    {
                        "tbl": f"{target_table} ({table_action})",
                        "tool": "influence_to_barangay",
                        "details": ", ".join(added_fields),
                    },
                )

    messagebox.showinfo("Success", "✅ Processing done with CAMA logs!")


# -------------------- GUI WINDOWS --------------------
def select_barangay_window(root):
    win = tk.Toplevel(root)
    apply_icon(win)
    win.title("Select Land Parcel Source")
    win.geometry("325x80")
    win.resizable(False, False)

    def pick_local():
        global barangay_source
        files = filedialog.askopenfilenames(
            title="Select Land Parcel file",
            filetypes=VECTOR_FILETYPES,
        )
        if files:
            barangay_source = ("local", files)
            print("✅ Barangay source set:", barangay_source)
            win.destroy()
            select_influence_window(root)

    def pick_db():
        global barangay_source
        creds = load_db_credentials()
        if not creds:
            return
        tables = fetch_tables(creds["schema"])
        if not tables:
            messagebox.showerror("Error", "No tables found in schema.")
            return

        db_win = tk.Toplevel(root)
        apply_icon(db_win)
        db_win.title("Select Land Parcel Table")
        lb = Listbox(db_win, selectmode=tk.MULTIPLE, width=50, height=15)
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
                select_influence_window(root)
            else:
                messagebox.showwarning("Warning", "Please select at least one table.")

        tk.Button(db_win, text="Select", command=submit).pack(pady=5)

    tk.Button(win, text="Select Local File", command=pick_local).pack(pady=5)
    tk.Button(win, text="Select Database File", command=pick_db).pack(pady=5)


def select_influence_window(root):
    win = tk.Toplevel(root)
    apply_icon(win)
    win.title("Select Influence Map Source")
    tk.Label(win, text="Select Influence Map Source").pack(padx=85, pady=10)

    def pick_local():
        global influence_source
        files = filedialog.askopenfilenames(
            title="Select Influence Map file",
            filetypes=VECTOR_FILETYPES,
        )
        if files:
            influence_source = ("local", files)
            print("✅ Influence source set:", influence_source)
            win.destroy()
            select_output_window(root)

    def pick_db():
        global influence_source
        creds = load_db_credentials()
        if not creds:
            return
        tables = fetch_tables(creds["schema"])
        if not tables:
            messagebox.showerror("Error", "No tables found in schema.")
            return

        db_win = tk.Toplevel(root)
        db_win.title("Select Influence Tables")
        lb = Listbox(db_win, selectmode=tk.MULTIPLE, width=50, height=15)
        for t in tables:
            lb.insert(tk.END, t)
        lb.pack()

        def submit():
            global influence_source
            sel = [lb.get(i) for i in lb.curselection()]
            if sel:
                influence_source = ("db", sel)
                print("✅ Influence source set:", influence_source)
                db_win.destroy()
                win.destroy()
                select_output_window(root)
            else:
                messagebox.showwarning("Warning", "Please select at least one table.")

        tk.Button(db_win, text="Select", command=submit).pack(pady=5)

    tk.Button(win, text="Select Local File", command=pick_local).pack(pady=5)
    tk.Button(win, text="Select Database File", command=pick_db).pack(pady=5)


def select_output_window(root):
    win = tk.Toplevel(root)
    apply_icon(win)
    win.title("Select Output Destination")
    tk.Label(win, text="Save output to:").pack(padx=85, pady=10)

    def save_local():
        global output_mode, barangay_source, influence_source
        if not barangay_source or not influence_source:
            messagebox.showerror("Error", "Barangay and Influence sources must be selected first.")
            return
        out_dir = filedialog.askdirectory()
        if out_dir:
            output_mode = ("local", out_dir)
            print("✅ Output mode set:", output_mode)
            win.destroy()
            run_processing()

    def save_db():
        global output_mode, barangay_source, influence_source
        if not barangay_source or not influence_source:
            messagebox.showerror("Error", "Barangay and Influence sources must be selected first.")
            return
        output_mode = ("db", None)
        print("✅ Output mode set:", output_mode)
        win.destroy()
        run_processing()

    tk.Button(win, text="Save to Local", command=save_local).pack(pady=5)
    tk.Button(win, text="Save to Database", command=save_db).pack(pady=5)


# -------------------- MAIN --------------------
def main():
    root = tk.Tk()
    apply_icon(root)
    root.withdraw()
    select_barangay_window(root)
    root.mainloop()


if __name__ == "__main__":
    main()