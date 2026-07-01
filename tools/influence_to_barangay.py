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
import subprocess

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
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"

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
            out_path = os.path.join(out_dir, f"{local_name}.gpkg")

            # 1️⃣ Ensure CRS exists
            if b_gdf.crs is None:
                raise RuntimeError("❌ Cannot write file: CRS is None")

            # 2️⃣ Reproject to WGS84
            b_gdf = b_gdf.to_crs(epsg=4326)
            print("🧭 CRS before save:", b_gdf.crs)

            # 3️⃣ Fix invalid geometries
            if not b_gdf.is_valid.all():
                print("⚠️ Fixing invalid geometries")
                b_gdf["geometry"] = b_gdf.geometry.buffer(0)

            # 4️⃣ Write GeoPackage
            b_gdf.to_file(out_path, driver="GPKG")

            print(f"✅ Saved: {out_path}")
            load_in_global_mapper(out_path)

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


# REPLACE WITH

# -------------------- GLOBAL MAPPER --------------------
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


# -------------------- DB TABLE PICKER --------------------
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


# -------------------- MAIN WINDOW --------------------
def open_main_window(root):
    from tkinter import ttk

    win = tk.Toplevel(root)
    apply_icon(win)
    win.title("Influence to Parcel Tool")
    win.resizable(False, False)
    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))

    # ── state ────────────────────────────────────────────────────
    parcel_source_type   = tk.StringVar(value="local")
    influence_source_type = tk.StringVar(value="local")
    output_dest_type     = tk.StringVar(value="local")

    parcel_local_paths   = []
    parcel_db_tables     = []
    influence_local_paths = []
    influence_db_tables  = []
    output_local_dir     = tk.StringVar()

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
            title="Select Land Parcel file(s)",
            filetypes=VECTOR_FILETYPES)
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

    # ── SECTION 2: INFLUENCE MAP ─────────────────────────────────
    section_label(win, "Influence Map Source")

    influence_frame = tk.Frame(win)
    influence_frame.pack(fill="x", padx=18, pady=2)

    infl_radio_row = tk.Frame(influence_frame)
    infl_radio_row.pack(fill="x")
    tk.Radiobutton(infl_radio_row, text="Local File(s)",
                   variable=influence_source_type, value="local",
                   command=lambda: _toggle_influence()).pack(side="left")
    tk.Radiobutton(infl_radio_row, text="Database Table(s)",
                   variable=influence_source_type, value="db",
                   command=lambda: _toggle_influence()).pack(side="left", padx=(12, 0))

    infl_local_frame = tk.Frame(influence_frame)
    infl_local_frame.pack(fill="x", pady=2)
    infl_files_var = tk.StringVar(value="No file(s) selected")
    tk.Label(infl_local_frame, textvariable=infl_files_var,
             fg="gray", anchor="w", width=42).pack(side="left")

    def browse_influence_files():
        files = filedialog.askopenfilenames(
            title="Select Influence Map file(s)",
            filetypes=VECTOR_FILETYPES)
        if files:
            influence_local_paths.clear()
            influence_local_paths.extend(files)
            infl_files_var.set(f"{len(files)} file(s) selected")

    tk.Button(infl_local_frame, text="Browse…", width=10,
              command=browse_influence_files).pack(side="left", **PAD)

    infl_db_frame = tk.Frame(influence_frame)
    infl_db_label = tk.StringVar(value="No table(s) selected")
    tk.Label(infl_db_frame, textvariable=infl_db_label,
             fg="gray", anchor="w", width=42).pack(side="left")

    def browse_influence_db():
        creds = load_db_credentials()
        if not creds:
            return
        tables = fetch_tables(creds["schema"])
        _pick_db_tables(win, tables, multi=True,
                        on_select=lambda sel: (
                            influence_db_tables.__setitem__(slice(None), sel)
                            or infl_db_label.set(f"{len(sel)} table(s) selected")
                        ))

    tk.Button(infl_db_frame, text="Select…", width=10,
              command=browse_influence_db).pack(side="left", **PAD)

    def _toggle_influence():
        if influence_source_type.get() == "local":
            infl_db_frame.pack_forget()
            infl_local_frame.pack(fill="x", pady=2)
        else:
            infl_local_frame.pack_forget()
            infl_db_frame.pack(fill="x", pady=2)

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
        global barangay_source, influence_source, output_mode

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

        # validate influence
        if influence_source_type.get() == "local":
            if not influence_local_paths:
                messagebox.showerror("Missing Input",
                    "Please select at least one Influence Map file.")
                return
            influence_source = ("local", tuple(influence_local_paths))
        else:
            if not influence_db_tables:
                messagebox.showerror("Missing Input",
                    "Please select at least one Influence Map table.")
                return
            influence_source = ("db", influence_db_tables)

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


# -------------------- MAIN --------------------
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