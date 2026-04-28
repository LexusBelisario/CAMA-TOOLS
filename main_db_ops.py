# === main_db_ops.py ===
import os
import json
import re
import time
import fiona
import geopandas as gpd
import pygetwindow as gw
import pyautogui
import psycopg2
import shutil
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from geoalchemy2 import Geometry
from tkinter import messagebox, filedialog, Toplevel, Label, Entry, Button
from rapidfuzz import process, fuzz
from main_utils import _normalize_name

DB_HOST = "localhost"
DB_PORT = "5432"
DB_NAME = "BLGF_DBTEST"
DB_SCHEMA = "CALAUAN_LAGUNA"
TEMP_DIR = r"C:\Global Mapper Temp"

stored_username = None
stored_password = None
selected_gmw_file = None

# === Helper ===
def ensure_postgis(psycopg_conn):
    with psycopg_conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
    psycopg_conn.commit()

def to_wgs84(gdf):
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326)
    elif getattr(gdf.crs, "to_epsg", lambda: None)() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    return gdf


# ===============================================================
# === DATABASE UPDATE FROM GEOPACKAGE ===
# ===============================================================
def update_database_from_geopackage():
    pyautogui.FAILSAFE = False
    if not all([stored_username, stored_password]):
        messagebox.showerror("Error", "You must log in first before updating the database.")
        return

    try:
        gm_window = None
        for w in gw.getWindowsWithTitle("Global Mapper Pro"):
            if ".gmw" in w.title.lower():
                gm_window = w
                break
        if not gm_window:
            messagebox.showerror("Error", "Global Mapper window not found.")
            return

        gm_window.minimize(); time.sleep(0.1)
        gm_window.restore(); time.sleep(0.1)
        gm_window.activate(); time.sleep(0.3)

        save_path = os.path.join(TEMP_DIR, "savetodb.gpkg")
        if os.path.exists(save_path):
            try: os.remove(save_path)
            except Exception as e:
                messagebox.showerror("File Error", f"Could not delete existing file:\n{e}")
                return

        pyautogui.hotkey("ctrl", "s")
        time.sleep(0.3)
        real_mouse_pos = pyautogui.position()
        left_panel_x = gm_window.left + 25
        left_panel_y = gm_window.top + 500
        pyautogui.moveTo(left_panel_x, left_panel_y)
        pyautogui.rightClick()
        pyautogui.moveTo(real_mouse_pos)
        time.sleep(0.05)
        pyautogui.press("up")
        pyautogui.press("right")
        pyautogui.press("down")
        pyautogui.press("enter")
        time.sleep(0.05)
        pyautogui.press("enter")
        time.sleep(0.05)
        pyautogui.typewrite("a")
        pyautogui.typewrite("g" * 6)
        pyautogui.press("enter")
        time.sleep(0.05)

        try:
            save_win = gw.getWindowsWithTitle("Save As")[0]
            save_win.activate()
            time.sleep(0.05)
        except IndexError:
            print("⚠ Save As dialog not found, continuing...")

        pyautogui.keyDown("alt")
        pyautogui.press("d")
        pyautogui.keyUp("alt")
        time.sleep(0.5)
        pyautogui.typewrite(r"C:\\")
        pyautogui.press("enter")
        time.sleep(0.5)
        pyautogui.hotkey("alt", "n")
        pyautogui.typewrite(save_path)
        pyautogui.press("enter")

        last_size = -1
        stable_count = 0
        while True:
            if os.path.exists(save_path):
                size = os.path.getsize(save_path)
                if size == last_size and size > 1000:
                    stable_count += 1
                    if stable_count >= 2:
                        break
                else:
                    stable_count = 0
                last_size = size
            time.sleep(1)
        print("✅ File saved:", save_path)
    except Exception as e:
        messagebox.showerror("Export Failed", f"Export failed:\n{e}")
        return

    try:
        connection_url = URL.create(
            drivername="postgresql+psycopg2",
            username=stored_username,
            password=stored_password,
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME
        )
        engine = create_engine(connection_url)
        conn = engine.connect()
        cursor = conn.connection.cursor()
        ensure_postgis(conn.connection)

        cursor.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s;", (DB_SCHEMA,)
        )
        existing_tables = [row[0] for row in cursor.fetchall()]
        layers = fiona.listlayers(save_path)
        schema_prefix = DB_SCHEMA

        for layer in layers:
            gdf = gpd.read_file(save_path, layer=layer)
            gdf.columns = [col.lower() for col in gdf.columns]
            gdf = to_wgs84(gdf)
            gdf = gdf.rename_geometry("geom")
            dtype = {"geom": Geometry(geometry_type="GEOMETRY", srid=4326)}

            match_table = None
            for tbl in existing_tables:
                if _normalize_name(tbl) == _normalize_name(layer):
                    match_table = tbl
                    break

            if match_table:
                print(f"Replacing table via match: {match_table} <- {layer}")
                cursor.execute(f'DROP TABLE IF EXISTS "{DB_SCHEMA}"."{match_table}" CASCADE;')
                conn.connection.commit()
                target_name = match_table
            else:
                new_table = _normalize_name(layer, schema_prefix=schema_prefix + "_")
                if not new_table:
                    new_table = "layer_" + str(abs(hash(layer)))
                target_name = new_table
                print(f"Creating new table: {target_name}")

            gdf.to_postgis(
                name=target_name,
                con=engine,
                schema=DB_SCHEMA,
                if_exists="replace",
                index=False,
                dtype=dtype
            )

        conn.close()
        engine.dispose()
        messagebox.showinfo("Success", "Database updated from GeoPackage.")
        if os.path.exists(save_path):
            os.remove(save_path)
    except Exception as e:
        messagebox.showerror("Database Update Failed", f"Database load failed:\n{e}")

# ===============================================================
# === UPDATE MAP AND SELECT RECORDS ===
# ===============================================================
def update_map_and_select_recorded():
    pyautogui.FAILSAFE = False
    if not all([stored_username, stored_password]):
        messagebox.showerror("Error", "You must log in first before updating the map.")
        return
    try:
        gm_window = None
        for w in gw.getWindowsWithTitle("Global Mapper Pro"):
            if ".gmw" in w.title.lower():
                gm_window = w
                break
        if not gm_window:
            messagebox.showerror("Error", "Global Mapper window not found.")
            return

        gm_window.minimize(); time.sleep(0.1)
        gm_window.restore(); time.sleep(0.1)
        gm_window.activate(); time.sleep(0.1)

        save_path = os.path.join(TEMP_DIR, "updatemap.gpkg")
        if os.path.exists(save_path):
            try: os.remove(save_path)
            except Exception as e:
                messagebox.showerror("File Error", f"Could not delete:\n{e}")
                return

        pyautogui.hotkey("ctrl", "s")
        time.sleep(0.05)
        real_mouse_pos = pyautogui.position()
        left_panel_x = gm_window.left + 25
        left_panel_y = gm_window.top + 500
        pyautogui.moveTo(left_panel_x, left_panel_y)
        pyautogui.rightClick()
        pyautogui.moveTo(real_mouse_pos)
        time.sleep(0.05)
        pyautogui.press("up")
        pyautogui.press("right")
        pyautogui.press("down")
        pyautogui.press("enter")
        pyautogui.press("enter")
        pyautogui.typewrite("a")
        pyautogui.typewrite("g" * 6)
        pyautogui.press("enter")
        pyautogui.hotkey("alt", "d")
        pyautogui.typewrite("C:")
        pyautogui.press("enter")
        pyautogui.hotkey("alt", "n")
        pyautogui.typewrite(save_path)
        pyautogui.press("enter")

        last_size = -1
        stable_count = 0
        while True:
            if os.path.exists(save_path):
                size = os.path.getsize(save_path)
                if size == last_size and size > 1000:
                    stable_count += 1
                    if stable_count >= 2:
                        break
                else:
                    stable_count = 0
                last_size = size
            time.sleep(1)
        print("✅ File saved:", save_path)

        connection_url = URL.create(
            drivername="postgresql+psycopg2",
            username=stored_username,
            password=stored_password,
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME
        )
        engine = create_engine(connection_url)
        with engine.begin() as conn:
            db_tables = [r[0] for r in conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema=:s AND table_type='BASE TABLE'"),
                {"s": DB_SCHEMA}
            )]
        db_lower = {t.lower(): t for t in db_tables}
        schema_prefix = DB_SCHEMA + "_"
        matched_tables = []
        for layer in fiona.listlayers(save_path):
            lyr_name = layer.strip()
            if lyr_name.lower().startswith(schema_prefix.lower()):
                stripped = lyr_name[len(schema_prefix):]
            else:
                stripped = lyr_name
            stripped = re.sub(r"\s+", "_", stripped)
            if stripped.lower() in db_lower:
                matched_tables.append(f"{DB_SCHEMA}.{db_lower[stripped.lower()]}")
            else:
                match, score, _ = process.extractOne(
                    stripped.lower(),
                    [t.lower() for t in db_tables],
                    scorer=fuzz.WRatio
                )
                if match is not None:
                    matched_tables.append(f"{DB_SCHEMA}.{db_lower[match]}")
                else:
                    matched_tables.append(f"{DB_SCHEMA}.{stripped}")
        print("📄 Matched tables:", matched_tables)
        messagebox.showinfo("Update Map", "Updated table into GM.")
        if os.path.exists(save_path):
            os.remove(save_path)
    except Exception as e:
        messagebox.showerror("Update Map Failed", str(e))

# ===============================================================
# === LOGIN WINDOW ===
# ===============================================================
def show_login_and_connect(root, launch_global_mapper, gmw_file):
    win = Toplevel(root)
    win.title("Database Login")
    win.geometry("260x220")
    win.grab_set()
    win.resizable(False, False)

    Label(win, text="Host:").grid(row=0, column=0, sticky="e", padx=5, pady=3)
    host_entry = Entry(win, width=25)
    host_entry.insert(0, DB_HOST)
    host_entry.config(state="disabled")
    host_entry.grid(row=0, column=1)
    Label(win, text="Port:").grid(row=1, column=0, sticky="e", padx=5, pady=3)
    port_entry = Entry(win, width=25)
    port_entry.insert(0, DB_PORT)
    port_entry.config(state="disabled")
    port_entry.grid(row=1, column=1)
    Label(win, text="Database:").grid(row=2, column=0, sticky="e", padx=5, pady=3)
    db_entry = Entry(win, width=25)
    db_entry.insert(0, DB_NAME)
    db_entry.config(state="disabled")
    db_entry.grid(row=2, column=1)
    Label(win, text="Schema:").grid(row=3, column=0, sticky="e", padx=5, pady=3)
    schema_entry = Entry(win, width=25)
    schema_entry.insert(0, DB_SCHEMA)
    schema_entry.config(state="disabled")
    schema_entry.grid(row=3, column=1)
    Label(win, text="Username:").grid(row=4, column=0, sticky="e", padx=5, pady=3)
    user_entry = Entry(win, width=25); user_entry.grid(row=4, column=1)
    Label(win, text="Password:").grid(row=5, column=0, sticky="e", padx=5, pady=3)
    pass_entry = Entry(win, width=25, show="*"); pass_entry.grid(row=5, column=1)

    def try_connect():
        global stored_username, stored_password, selected_gmw_file
        stored_username = user_entry.get()
        stored_password = pass_entry.get()
        selected_gmw_file = gmw_file
        try:
            conn = psycopg2.connect(
                host=DB_HOST, port=DB_PORT, database=DB_NAME,
                user=stored_username, password=stored_password
            )
            conn.close()
            win.destroy()
            launch_global_mapper()
        except Exception as e:
            messagebox.showerror("Login Failed", f"Could not connect:\n{e}")
            return
        with open("pg_credentials.json", "w") as f:
            json.dump({
                "host": DB_HOST,
                "port": DB_PORT,
                "database": DB_NAME,
                "schema": DB_SCHEMA,
                "username": stored_username,
                "password": stored_password
            }, f)

    Button(win, text="Login", command=try_connect, bg="#007acc", fg="white").grid(row=6, columnspan=2, pady=10)
