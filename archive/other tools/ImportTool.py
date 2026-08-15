import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import psycopg
import geopandas as gpd
import os
from sqlalchemy import create_engine

# === Global connection holders ===
conn = None
engine = None
conn_params = {}

# === Utility: Ensure connection is alive ===
def ensure_connection():
    global conn, engine, conn_params
    try:
        if conn is None or conn.closed:
            conn = psycopg.connect(**conn_params)
    except Exception:
        conn = psycopg.connect(**conn_params)

    # Rebuild engine
    engine = create_engine(
        f"postgresql+psycopg://{conn_params['user']}:{conn_params['password']}@{conn_params['host']}:{conn_params['port']}/{conn_params['dbname']}?sslmode={conn_params['sslmode']}"
    )
    return conn, engine


# === STEP 1: Login Window ===
def show_connection_window():
    def connect():
        global conn, engine, conn_params
        try:
            # Save params for reconnect
            conn_params = {
                "host": host_entry.get(),
                "port": port_entry.get(),
                "dbname": dbname_entry.get(),
                "user": user_entry.get(),
                "password": password_entry.get(),
                "sslmode": "disable"  # change to "require" for Supabase
            }

            # Initial connect
            conn = psycopg.connect(**conn_params)

            # SQLAlchemy engine
            engine = create_engine(
                f"postgresql+psycopg://{conn_params['user']}:{conn_params['password']}@{conn_params['host']}:{conn_params['port']}/{conn_params['dbname']}?sslmode={conn_params['sslmode']}"
            )

            messagebox.showinfo("Success", "Connected to database.")
            root.destroy()
            show_import_window(conn, engine)
        except Exception as e:
            messagebox.showerror("Connection Failed", str(e))

    root = tk.Tk()
    root.title("Database Connection")

    for i, label in enumerate(["Host", "Port", "Database", "User", "Password"]):
        tk.Label(root, text=f"{label}:").grid(row=i, column=0, sticky="e")

    host_entry = tk.Entry(root); port_entry = tk.Entry(root)
    dbname_entry = tk.Entry(root); user_entry = tk.Entry(root)
    password_entry = tk.Entry(root, show="*")

    host_entry.grid(row=0, column=1); port_entry.grid(row=1, column=1)
    dbname_entry.grid(row=2, column=1); user_entry.grid(row=3, column=1)
    password_entry.grid(row=4, column=1)

    host_entry.insert(0, "localhost")
    port_entry.insert(0, "5432")

    tk.Button(root, text="Connect", command=connect).grid(row=5, columnspan=2, pady=10)
    root.mainloop()


# === STEP 2: Main Import Window ===
def show_import_window(conn, engine):
    def fetch_schemas():
        try:
            conn, engine = ensure_connection()
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT schema_name FROM information_schema.schemata
                    WHERE schema_name NOT IN ('information_schema','pg_catalog','pg_toast','public')
                    ORDER BY schema_name;
                """)
                schemas = [row[0] for row in cur.fetchall()]
                schema_combo['values'] = schemas
                if schemas:
                    schema_combo.set(schemas[0])
        except Exception as e:
            messagebox.showerror("Error", f"Schema fetch failed:\n{e}")

    def show_schema_create():
        schema_input_frame.grid(row=2, columnspan=2, pady=5)

    def create_schema():
        new_schema = new_schema_entry.get().strip()
        if not new_schema:
            return messagebox.showerror("Missing Name", "Enter a schema name.")
        try:
            conn, engine = ensure_connection()
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{new_schema}";')
                conn.commit()
                fetch_schemas()
                schema_combo.set(new_schema)
                schema_input_frame.grid_remove()
                new_schema_entry.delete(0, tk.END)
                messagebox.showinfo("Created", f"Schema '{new_schema}' created.")
        except Exception as e:
            messagebox.showerror("Error", f"Schema creation failed:\n{e}")

    def open_file_dialog():
        paths = filedialog.askopenfilenames(title="Select Shapefile(s)", filetypes=[("Shapefiles", "*.shp")])
        if paths:
            open_import_popup(paths)

    def open_import_popup(paths):
        popup = tk.Toplevel()
        popup.title("Configure Shapefiles")
        popup.geometry("600x400")

        listbox = tk.Listbox(popup, width=40)
        listbox.pack(side="left", fill="y", padx=10, pady=10)

        entry_widgets = []
        for path in paths:
            name = os.path.splitext(os.path.basename(path))[0]
            listbox.insert("end", name)
            entry_widgets.append({"path": path, "name": name})

        selected_entry = tk.Entry(popup, width=30)
        selected_entry.pack(pady=(0, 5))

        last_selected_index = [None]

        def apply_rename():
            idx = last_selected_index[0]
            if idx is not None:
                new_name = selected_entry.get().strip()
                if not new_name:
                    return
                entry_widgets[idx]["name"] = new_name
                listbox.delete(idx)
                listbox.insert(idx, new_name)
                listbox.select_set(idx)
                listbox.activate(idx)

        def show_summary():
            index = listbox.curselection()
            if index:
                idx = index[0]
                path = entry_widgets[idx]["path"]
                try:
                    gdf = gpd.read_file(path)
                    if gdf.crs and gdf.crs.to_epsg() != 4326:
                        gdf = gdf.to_crs(epsg=4326)
                    cols = [c.lower() for c in gdf.columns]
                    summary = f"File: {path}\nRows: {len(gdf)}\nColumns: {len(cols)}\n\n" + "\n".join(cols)
                    detail = tk.Toplevel(popup)
                    detail.title("Summary")
                    txt = tk.Text(detail, wrap="word")
                    txt.insert("1.0", summary)
                    txt.config(state="disabled")
                    txt.pack(fill="both", expand=True, padx=10, pady=10)
                except Exception as e:
                    messagebox.showerror("Summary Error", str(e))

        def remove_selected():
            index = listbox.curselection()
            if index:
                idx = index[0]
                listbox.delete(idx)
                del entry_widgets[idx]

        def import_all():
            schema = schema_combo.get()
            if not schema:
                return messagebox.showerror("Missing Schema", "Select a schema first.")
            create_transaction_log_table(schema)  # ✅ removed conn param

            for item in entry_widgets:
                path = item["path"]
                name = item["name"].strip()

                try:
                    gdf = gpd.read_file(path)
                    if gdf.crs and gdf.crs.to_epsg() != 4326:
                        gdf = gdf.to_crs(epsg=4326)
                    gdf.columns = [c.lower() for c in gdf.columns]
                    gdf = gdf.rename_geometry("geom")

                    conn, engine = ensure_connection()
                    gdf.to_postgis(name=name, con=engine, schema=schema, if_exists="fail", index=False)

                    with conn.cursor() as cur:
                        cur.execute(f'''
                            DO $$
                            BEGIN
                                IF NOT EXISTS (
                                    SELECT 1 FROM information_schema.columns
                                    WHERE table_schema = '{schema}' AND table_name = '{name}' AND column_name = 'id'
                                ) THEN
                                    ALTER TABLE "{schema}"."{name}" ADD COLUMN id SERIAL PRIMARY KEY;
                                END IF;
                            END
                            $$ LANGUAGE plpgsql;
                        ''')
                        conn.commit()
                    
                    alter_column_types(schema, name)
                except Exception as e:
                    messagebox.showerror("Import Failed", f"{path}:\n{e}")
                    return
            messagebox.showinfo("Success", "All shapefiles imported.")
            popup.destroy()


        def create_transaction_log_table(schema):
            global conn, engine
            try:
                conn, engine = ensure_connection()
                with conn.cursor() as cur:
                    cur.execute(f'''
                        CREATE TABLE IF NOT EXISTS "{schema}".parcel_transaction_log (
                            id SERIAL PRIMARY KEY,
                            ...
                            geom GEOMETRY
                        );
                    ''')
                    conn.commit()
            except Exception as e:
                if conn and not conn.closed:
                    conn.rollback()
                messagebox.showerror("Log Table Error", f"Failed to create transaction log:\n{e}")

        def update_entry(event=None):
            index = listbox.curselection()
            if index:
                idx = index[0]
                last_selected_index[0] = idx
                selected_entry.delete(0, tk.END)
                selected_entry.insert(0, listbox.get(idx))

        listbox.bind("<<ListboxSelect>>", update_entry)

        def add_more_shapefiles():
            new_paths = filedialog.askopenfilenames(title="Add Shapefile(s)", filetypes=[("Shapefiles", "*.shp")])
            for path in new_paths:
                name = os.path.splitext(os.path.basename(path))[0]
                entry_widgets.append({"path": path, "name": name})
                listbox.insert("end", name)

        tk.Button(popup, text="Add More Shapefile(s)", command=add_more_shapefiles).pack(pady=(0, 10))

        ctrl = tk.Frame(popup)
        ctrl.pack(pady=10)

        tk.Button(ctrl, text="Apply Rename", command=apply_rename).pack(side="left", padx=5)
        tk.Button(ctrl, text="Show Summary", command=show_summary).pack(side="left", padx=5)
        tk.Button(ctrl, text="Remove Selected", command=remove_selected).pack(side="left", padx=5)
        tk.Button(ctrl, text="Import All", command=import_all).pack(side="left", padx=5)

    def alter_column_types(schema, table):
        try:
            conn, engine = ensure_connection()
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT column_name, data_type FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s AND column_name != 'geom'
                """, (schema, table))
                
                for col, dtype in cur.fetchall():
                    if dtype.startswith("character") or dtype == "text":
                        cur.execute(f'''
                            ALTER TABLE "{schema}"."{table}"
                            ALTER COLUMN "{col}" TYPE text
                            USING "{col}"::text;
                        ''')
                    elif dtype in ("smallint", "integer", "bigint", "real", "double precision"):
                        cur.execute(f'''
                            ALTER TABLE "{schema}"."{table}"
                            ALTER COLUMN "{col}" TYPE numeric
                            USING "{col}"::numeric;
                        ''')
                conn.commit()
        except Exception as e:
            if conn and not conn.closed:
                conn.rollback()
            print(f"Post-import cleanup failed: {e}")

    def create_transaction_log_table(schema):
        global conn, engine
        try:
            conn, engine = ensure_connection()
            with conn.cursor() as cur:
                cur.execute(f'''
                    CREATE TABLE IF NOT EXISTS "{schema}".parcel_transaction_log (
                        id SERIAL PRIMARY KEY,
                        table_name TEXT,
                        transaction_type TEXT,
                        transaction_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        pin TEXT,
                        province TEXT,
                        municipal TEXT,
                        prov_code TEXT,
                        mun_code TEXT,
                        barangay TEXT,
                        brgy_code TEXT,
                        section TEXT,
                        parcel TEXT,
                        vicinity TEXT,
                        class_bir TEXT,
                        value TEXT,
                        land_arpn TEXT,
                        tct_no TEXT,
                        survey_no TEXT,
                        updte_code TEXT,
                        blk_no TEXT,
                        td_no TEXT,
                        lot_no TEXT,
                        l_acctno TEXT,
                        l_lastname TEXT,
                        l_frstname TEXT,
                        l_midname TEXT,
                        l_ownadd TEXT,
                        l_owndist TEXT,
                        l_ownmuni TEXT,
                        l_ownbrgy TEXT,
                        l_ownprov TEXT,
                        l_ownzip TEXT,
                        l_owntel TEXT,
                        north TEXT,
                        south TEXT,
                        east TEXT,
                        west TEXT,
                        extent TEXT,
                        l_prvarp TEXT,
                        l_prvpin TEXT,
                        l_prvowner TEXT,
                        effectvty TEXT,
                        l_prvav TEXT,
                        land_sbcls TEXT,
                        land_area TEXT,
                        land_uv TEXT,
                        land_mval TEXT,
                        land_desc TEXT,
                        adj_rate TEXT,
                        adj_val TEXT,
                        land_areat TEXT,
                        land_totmv TEXT,
                        land_aslvl TEXT,
                        land_asval TEXT,
                        land_totav TEXT,
                        bldg_pin TEXT,
                        mach_pin TEXT,
                        bldg_arpn TEXT,
                        cct TEXT,
                        bldg_class TEXT,
                        bldg_sbcls TEXT,
                        bldg_age TEXT,
                        bldg_areat TEXT,
                        bldg_area TEXT,
                        bldg_uv TEXT,
                        bldg_mval TEXT,
                        bldg_drate TEXT,
                        bldg_dval TEXT,
                        bldg_mval2 TEXT,
                        bldg_dmv TEXT,
                        bldg_ause TEXT,
                        bldg_aslvl TEXT,
                        bldg_asval TEXT,
                        b_prvarp TEXT,
                        b_prvpin TEXT,
                        b_prvowner TEXT,
                        b_effectvt TEXT,
                        mach_arpn TEXT,
                        mach_desc TEXT,
                        mach_units TEXT,
                        mach_cost TEXT,
                        mach_ause TEXT,
                        mach_mval2 TEXT,
                        mach_level TEXT,
                        mach_adjmv TEXT,
                        mach_totav TEXT,
                        m_prvarp TEXT,
                        m_prvpin TEXT,
                        m_prvowner TEXT,
                        m_prvav TEXT,
                        m_effectvt TEXT,
                        tax_year TEXT,
                        paymt_type TEXT,
                        or_no TEXT,
                        or_date TEXT,
                        pay_period TEXT,
                        qtr_no TEXT,
                        basic_prin TEXT,
                        basic_int TEXT,
                        basic_disc TEXT,
                        basictotal TEXT,
                        sef_prin TEXT,
                        sef_int TEXT,
                        sef_disc TEXT,
                        sef_total TEXT,
                        street TEXT,
                        perimeter TEXT,
                        enclosed_a TEXT,
                        b_prvav TEXT,
                        mach_mval TEXT,
                        b_acctno TEXT,
                        b_lastname TEXT,
                        b_frstname TEXT,
                        b_midname TEXT,
                        b_ownadd TEXT,
                        b_owndist TEXT,
                        b_ownmuni TEXT,
                        b_ownbrgy TEXT,
                        b_ownprov TEXT,
                        b_ownzip TEXT,
                        b_owntel TEXT,
                        m_acctno TEXT,
                        m_lastname TEXT,
                        m_frstname TEXT,
                        m_midname TEXT,
                        m_ownadd TEXT,
                        m_owndist TEXT,
                        m_ownmuni TEXT,
                        m_ownbrgy TEXT,
                        m_ownprov TEXT,
                        m_ownzip TEXT,
                        m_owntel TEXT,
                        cad_no TEXT,
                        land_ause TEXT,
                        land_class TEXT,
                        ownership TEXT,
                        taxability TEXT,
                        payment TEXT,
                        del_amount TEXT,
                        geom GEOMETRY
                    );
                ''')
                conn.commit()
        except Exception as e:
            if conn and not conn.closed:
                conn.rollback()
            messagebox.showerror("Log Table Error", f"Failed to create transaction log:\n{e}")

    win = tk.Tk()
    win.title("Shapefile Import Tool")

    tk.Label(win, text="Target Schema:").grid(row=0, column=0, sticky="e")
    schema_combo = ttk.Combobox(win, width=30)
    schema_combo.grid(row=0, column=1, padx=5, pady=5)
    fetch_schemas()

    tk.Button(win, text="Create a Schema", command=show_schema_create).grid(row=1, columnspan=2)

    schema_input_frame = tk.Frame(win)
    tk.Label(schema_input_frame, text="Schema Name:").grid(row=0, column=0)
    new_schema_entry = tk.Entry(schema_input_frame, width=25)
    new_schema_entry.grid(row=0, column=1)
    tk.Button(schema_input_frame, text="Add Schema", command=create_schema).grid(row=0, column=2, padx=5)
    schema_input_frame.grid(row=2, columnspan=2)
    schema_input_frame.grid_remove()

    tk.Button(win, text="Add Shapefile(s)", command=open_file_dialog).grid(row=3, columnspan=2, pady=10)

    win.protocol("WM_DELETE_WINDOW", lambda: (conn.close(), win.destroy()))
    win.mainloop()


# === START ===
if __name__ == "__main__":
    show_connection_window()
