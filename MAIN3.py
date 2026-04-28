
import atexit
import signal

TOOL_PROCESSES = []

# ============================
# FORCE WINDOWS APP ICON
# ============================
import ctypes
import sys

def set_app_user_model_id():
    appid = u"BLGF.CAMA.Tools.2025"
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(appid)

set_app_user_model_id()


import tkinter as tk
from tkinter import filedialog, messagebox, Listbox
import subprocess
import os
import json
import psycopg2
from rapidfuzz import process, fuzz
from PIL import Image, ImageTk, ImageDraw

from pathlib import Path
from utils_paths import resource_path

def force_png_icon(win):
    png = resource_path("BLGF.png")
    if os.path.exists(png):
        img = tk.PhotoImage(file=png)
        win.iconphoto(True, img)
        win._icon_ref = img  # prevent garbage collection

def apply_icon(win):
    ico = resource_path("BLGF.ico")
    if os.path.exists(ico):
        try:
            win.iconbitmap(ico)
        except Exception:
            pass
    force_png_icon(win)


import sys, importlib, argparse


TEMP_DIR = r"C:\Global Mapper Temp"
try:
    os.makedirs(TEMP_DIR, exist_ok=True)
except Exception as e:
    from tkinter import messagebox
    messagebox.showerror("Folder Error", f"Could not create {TEMP_DIR}:\n{e}")
    sys.exit(1)


TOOL_MODULES = {
    "ANY MAP TO LAND PARCEL": "tools.influence_to_barangay",
    "ROAD WIDTH": "tools.road_width",
    "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO": "tools.road_frontage",
    "LOT LOCATION": "tools.lot_location",
    "LAND SHAPE": "tools.land_shape_compactness",
    "METERS FROM (SCHOOL, SHOP, TRANSPORT, CHURCH)": "tools.POI_All_Distance",
    "LANDMARKS WITHIN METERS": "tools.poi_within_200_meters_for_parcellary_church_mall_police_park",
    "PARCEL TERRAIN LEVEL": "tools.terrain",
    "ROAD DENSITY": "tools.road_density",
    "ROAD SURFACE": "tools.road_surface",
    "LINEAR REGRESSION": "tools.linear_regression",
    "RANDOM FOREST": "tools.random_forest",
    "XG BOOST": "tools.XG_Boost",
    "ORDINARY LEAST SQUARES": "tools.Ordinary_Least_Squares",
    "SPATIAL LAG MODEL": "tools.Spatial_Lag_Model",
    "SPATIAL DURBIN MODEL": "tools.Spatial_Durbin_Model",
    "GEOGRAPHICALLY WEIGHTED REGRESSION": "tools.Geographically_Weighted_Regression",
}

def dispatch_tool_if_requested():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--tool", default=None)
    ap.add_argument("--icon", default=None)
    args, _ = ap.parse_known_args()

    # ✅ If no tool specified, do normal launcher flow
    if not args.tool:
        return False

    # ✅ Apply icon for the tool subprocess
    if args.icon:
        try:
            icon_path = resource_path(args.icon)
            tmp = tk.Tk()
            tmp.withdraw()
            apply_icon(tmp)
        except Exception:
            pass

    mod_path = TOOL_MODULES.get(args.tool)
    if not mod_path:
        from tkinter import Tk, messagebox
        r = Tk(); r.withdraw()
        messagebox.showerror("Tool Error", f"Unknown tool: {args.tool}")
        sys.exit(2)

    try:
        mod = importlib.import_module(mod_path)
        if hasattr(mod, "main") and callable(mod.main):
            mod.main()
        sys.exit(0)
    except Exception:
        import traceback
        from tkinter import Tk, messagebox
        r = Tk(); r.withdraw()
        messagebox.showerror("Tool Crash", f"{mod_path}\n\n{traceback.format_exc()}")
        sys.exit(1)

# run BEFORE any GUI is created
IS_TOOL_RUN = dispatch_tool_if_requested()


# Define where icons are located (works both in dev and PyInstaller .exe)
ICONS_DIR = Path(resource_path("icons"))


confirm_win = None
confirm_clicked = {'ok': False}


DB_HOST = "35.194.255.28"
DB_PORT = "5432"
DB_NAME = "PH03008"
DB_SCHEMA = "PH0300807_Mariveles"
DEFAULT_DB_USERNAME = "postgres"
DEFAULT_DB_PASSWORD = "#IGDIwebapp"

selected_gmw_file = None

# Initialize stored credentials with defaults
stored_username = DEFAULT_DB_USERNAME
stored_password = DEFAULT_DB_PASSWORD

if not IS_TOOL_RUN:
    root = tk.Tk()
    apply_icon(root)

    root.geometry("355x410")
    root.minsize(355, 220)
    root.title("CAMA Tools")


from geoalchemy2 import Geometry

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


import geopandas as gpd
import fiona
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from rapidfuzz import fuzz
from tkinter import filedialog, messagebox

from rapidfuzz import process, fuzz
import re

# Suffixes that GM/exports commonly append to layer names
_NOISY_SUFFIXES = (
    "_shp", "_gpkg",
    "_line", "_lines", "_poly", "_polygon", "_polygons",
    "_point", "_points",
    "_multiline", "_multipolygon",
    "_layer", "_export", "_copy"
)

def _strip_noisy_suffixes(s: str) -> str:
    s = s.lower()
    for suf in _NOISY_SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s

def _normalize_name(name: str, schema_prefix: str = "") -> str:
    """
    Normalize names for matching:
    - drop text in parentheses
    - drop extension
    - drop schema prefix like CALAUAN_LAGUNA_
    - lower, collapse non-alnum to underscores
    - drop common noisy suffixes like _shp, _polygon, _export
    - collapse multiple underscores and trim
    """
    name = name.strip()
    if "(" in name:
        name = name.split("(")[0]
    if "." in name:
        name = name.split(".")[0]
    if schema_prefix and name.lower().startswith(schema_prefix.lower()):
        name = name[len(schema_prefix):]
    name = re.sub(r"[^0-9a-zA-Z]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("_").lower()
    name = _strip_noisy_suffixes(name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name

def _name_tokens(name: str) -> list[str]:
    """
    Tokenize a normalized name, and if the first token is digits (e.g., '01'),
    also consider the view without it (so '01_kanluran' can match 'kanluran').
    """
    n = _normalize_name(name)
    tokens = [t for t in n.split("_") if t]
    if tokens and tokens[0].isdigit():
        return [t for t in tokens[1:] if t] or tokens
    return tokens

def _tokens_subset(a_tokens: list[str], b_tokens: list[str]) -> bool:
    return bool(a_tokens and b_tokens) and set(a_tokens).issubset(set(b_tokens))

def _find_best_table(layer_name: str, existing_tables: list, schema_prefix: str) -> str | None:
    """
    Best match for a layer to an existing table:
    1) exact normalized equality
    2) token-subset match (layer ⊆ table OR table ⊆ layer)
    3) substring match (either direction) on normalized strings
    4) fuzzy (token_set_ratio then partial_ratio)
    """
    norm_layer = _normalize_name(layer_name, schema_prefix=schema_prefix + "_")
    layer_tokens = _name_tokens(layer_name)

    norm_map = { _normalize_name(t): t for t in existing_tables }
    if norm_layer in norm_map:
        return norm_map[norm_layer]

    table_tokens_map = { nt: _name_tokens(nt) for nt in norm_map.keys() }

    for nt, orig_tbl in norm_map.items():
        if _tokens_subset(layer_tokens, table_tokens_map[nt]) or _tokens_subset(table_tokens_map[nt], layer_tokens):
            return orig_tbl

    for nt, orig_tbl in norm_map.items():
        if norm_layer in nt or nt in norm_layer:
            return orig_tbl

    if norm_map:
        choices = list(norm_map.keys())
        best1, sc1, _ = process.extractOne(norm_layer, choices, scorer=fuzz.token_set_ratio)
        if sc1 >= 90:
            return norm_map[best1]
        best2, sc2, _ = process.extractOne(norm_layer, choices, scorer=fuzz.partial_ratio)
        if sc2 >= 90:
            return norm_map[best2]

    return None


def add_tooltip(widget, icon_path, title, subtitle="", canvas=None, bg_id=None):
    tooltip = tk.Toplevel(widget)
    apply_icon(tooltip)
    tooltip.withdraw()
    tooltip.overrideredirect(True)
    tooltip.configure(bg="#e0e0e0", padx=1, pady=1)
    tooltip.attributes('-topmost', True)

    outer = tk.Frame(tooltip, bg="#fefefe", relief="solid", borderwidth=1)
    outer.pack()

    content = tk.Frame(outer, bg="#fefefe")
    content.pack(padx=8, pady=6)

    # 🟡 Icon
    icon = Image.open(icon_path).resize((20, 20), Image.Resampling.LANCZOS)
    icon_img = ImageTk.PhotoImage(icon)
    icon_label = tk.Label(content, image=icon_img, bg="#fefefe")
    icon_label.image = icon_img
    icon_label.grid(row=0, column=0, rowspan=2, padx=(0, 6), pady=(2, 0), sticky="n")

    # 🔵 Title (bold)
    title_label = tk.Label(content, text=title.title(), font=("Segoe UI", 10, "bold"), bg="#fefefe", anchor="w", justify="left")
    title_label.grid(row=0, column=1, sticky="w")

    # 🔹 Subtitle (normal)
    subtitle_label = tk.Label(content, text=subtitle, font=("Segoe UI", 9), bg="#fefefe", anchor="w", justify="left")
    subtitle_label.grid(row=1, column=1, sticky="w")

    def enter(event):
        x = widget.winfo_rootx() + 45
        y = widget.winfo_rooty() + 10
        tooltip.geometry(f"+{x}+{y}")
        tooltip.deiconify()
        tooltip.lift()
        tooltip.attributes("-topmost", True)
        root.attributes("-topmost", False)

        if canvas and bg_id:
            canvas.itemconfig(bg_id, image=hover_bg)

    def leave(event):
        tooltip.withdraw()
        root.attributes("-topmost", True)
        if canvas and bg_id:
            canvas.itemconfig(bg_id, image="")

    widget.bind("<Enter>", enter)
    widget.bind("<Leave>", leave)


def extract_actual_name(layer_name: str) -> str:
    # remove anything after " (" which GM sometimes adds
    if "(" in layer_name:
        layer_name = layer_name.split("(")[0]
    # remove file extension, if present
    if "." in layer_name:
        layer_name = layer_name.split(".")[0]
    return layer_name.strip().lower()


def update_database_from_geopackage():
    import pygetwindow as gw
    import pyautogui
    import time
    import os
    import geopandas as gpd
    import fiona
    from tkinter import messagebox
    from sqlalchemy import create_engine
    from sqlalchemy.engine import URL
    from geoalchemy2 import Geometry  # needed for dtype in to_postgis

    pyautogui.FAILSAFE = False

    if not all([stored_username, stored_password]):
        messagebox.showerror("Error", "You must log in first before updating the database.")
        return

    try:
        # Step 1: Focus Global Mapper
        gm_window = None
        for w in gw.getWindowsWithTitle("Global Mapper Pro"):
            if "global mapper" in w.title.lower():
                gm_window = w
                break
        if not gm_window:
            messagebox.showerror("Error", "Global Mapper window not found.")
            return

        gm_window.minimize(); time.sleep(0.1)
        gm_window.restore(); time.sleep(0.1)
        gm_window.activate(); time.sleep(0.3)

        # Save path for exported GPKG
        save_path = os.path.join(TEMP_DIR, "savetodb.gpkg")

        # Delete if already exists
        if os.path.exists(save_path):
            try:
                os.remove(save_path)
            except Exception as e:
                messagebox.showerror("File Error", f"Could not delete existing file:\n{e}")
                return

        # Step 2: Trigger Save via virtual right-click in left panel
        pyautogui.hotkey("ctrl", "s")  # Save project first
        time.sleep(0.3)

        real_mouse_pos = pyautogui.position()
        gm_window.activate()
        time.sleep(0.05)
        left_panel_x = gm_window.left + 25
        left_panel_y = gm_window.top + 500
        pyautogui.moveTo(left_panel_x, left_panel_y)
        pyautogui.rightClick()
        pyautogui.moveTo(real_mouse_pos)

        # Step 3: Keyboard sequence for export
        time.sleep(0.05)
        pyautogui.press("up", presses=1, interval=0.05)
        pyautogui.press("right", presses=1, interval=0.05)
        pyautogui.press("down", presses=1, interval=0.05)
        pyautogui.press("enter")
        time.sleep(0.05)
        pyautogui.press("enter")
        time.sleep(0.05)
        pyautogui.typewrite("a")
        pyautogui.typewrite("g" * 6)
        pyautogui.press("enter")

        # Step 4: Navigate Save dialog
        print("Waiting for Save As dialog...")
        time.sleep(0.05)  # let Save dialog appear

        try:
            save_win = gw.getWindowsWithTitle("Save As")[0]
            save_win.activate()
            time.sleep(0.05)
        except IndexError:
            print("⚠️ Save As dialog not found, continuing anyway...")

        # Go to address bar
        pyautogui.keyDown("alt")
        pyautogui.press("d")
        pyautogui.keyUp("alt")
        time.sleep(0.5)

        # Navigate to C:\
        pyautogui.typewrite(r"C:\\")
        pyautogui.press("enter")
        time.sleep(0.5)

        # Focus filename field and type the full path inside TEMP_DIR
        pyautogui.hotkey("alt", "n")
        pyautogui.typewrite(save_path)
        pyautogui.press("enter")

        # Step 5: Wait until the file is fully saved (size stable)
        print("Waiting for file to be fully written...")
        last_size = -1
        stable_count = 0
        while True:
            if os.path.exists(save_path):
                current_size = os.path.getsize(save_path)
                if current_size == last_size and current_size > 1000:
                    stable_count += 1
                    if stable_count >= 2:
                        break
                else:
                    stable_count = 0
                last_size = current_size
            time.sleep(1)
        print("File saved:", save_path)

    except Exception as e:
        messagebox.showerror("Export Failed", f"Export failed:\n{e}")
        return

    try:
        # Step 6: Import into PostgreSQL with smart matching
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

        # make sure PostGIS types exist
        ensure_postgis(conn.connection)

        cursor.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s;",
            (DB_SCHEMA,)
        )
        existing_tables = [row[0] for row in cursor.fetchall()]

        layers = fiona.listlayers(save_path)
        schema_prefix = DB_SCHEMA

        for layer in layers:
            # read layer
            gdf = gpd.read_file(save_path, layer=layer)
            gdf.columns = [col.lower() for col in gdf.columns]

            # force Geographic lat/long WGS84
            gdf = to_wgs84(gdf)
            gdf = gdf.rename_geometry("geom")

            # tell pandas→PostGIS to store with SRID 4326
            dtype = {"geom": Geometry(geometry_type="GEOMETRY", srid=4326)}

            # decide target table
            match_table = _find_best_table(layer, existing_tables, schema_prefix=schema_prefix)
            if match_table:
                print(f"Replacing table via match: {match_table}  <- layer: {layer}")
                cursor.execute(f'DROP TABLE IF EXISTS "{DB_SCHEMA}"."{match_table}" CASCADE;')
                conn.connection.commit()
                target_name = match_table
            else:
                new_table = _normalize_name(layer, schema_prefix=schema_prefix + "_")
                if not new_table:
                    new_table = "layer_" + str(abs(hash(layer)))
                print(f"Creating new table: {new_table}  <- layer: {layer}")
                target_name = new_table

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

        # Step 7: Cleanup
        if os.path.exists(save_path):
            os.remove(save_path)

    except Exception as e:
        messagebox.showerror("Database Update Failed", f"Database load failed:\n{e}")


def update_map_and_select_recorded():
    import os, re, time
    import pygetwindow as gw
    import pyautogui
    from tkinter import messagebox
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import URL
    from rapidfuzz import process, fuzz
    import fiona

    pyautogui.FAILSAFE = False

    if not all([stored_username, stored_password]):
        messagebox.showerror("Error", "You must log in first before updating the map.")
        return

    try:
        # ---- Focus Global Mapper ----
        gm_window = None
        for w in gw.getWindowsWithTitle("Global Mapper Pro"):
            if "global mapper" in w.title.lower():
                gm_window = w
                break
        if not gm_window:
            messagebox.showerror("Error", "Global Mapper window not found.")
            return
        gm_window.minimize(); time.sleep(0.1)
        gm_window.restore();  time.sleep(0.1)
        gm_window.activate(); time.sleep(0.1)

        # ---- Export to GPKG (C:\updatemap.gpkg) ----
        save_path = os.path.join(TEMP_DIR, "updatemap.gpkg")
        if os.path.exists(save_path):
            try:
                os.remove(save_path)
            except Exception as e:
                messagebox.showerror("File Error", f"Could not delete existing file:\n{e}")
                return

        # Ctrl+S first
        pyautogui.hotkey("ctrl", "s")
        time.sleep(0.05)

        # Simulate right-click in left panel (like Update Database)
        real_mouse_pos = pyautogui.position()
        left_panel_x = gm_window.left + 25
        left_panel_y = gm_window.top + 500
        pyautogui.moveTo(left_panel_x, left_panel_y)
        pyautogui.rightClick()
        pyautogui.moveTo(real_mouse_pos)

        time.sleep(0.01)
        pyautogui.press("up")
        pyautogui.press("right")
        pyautogui.press("down")
        pyautogui.press("enter")
        time.sleep(0.01)
        pyautogui.press("enter")
        time.sleep(0.01)
        pyautogui.typewrite("a")
        pyautogui.typewrite("g" * 6)
        pyautogui.press("enter")

        time.sleep(0.05)
        pyautogui.hotkey("alt", "d")
        pyautogui.typewrite("C:")
        pyautogui.press("enter")
        time.sleep(0.05)
        pyautogui.press("tab", presses=3, interval=0.05)
        pyautogui.hotkey("alt", "n")
        pyautogui.typewrite(save_path)
        pyautogui.press("enter")

        # ---- Wait until file is fully saved ----
        print("⏳ Waiting for updatemap.gpkg to be fully written...")
        last_size = -1
        stable_count = 0

        while True:
            if os.path.exists(save_path):
                current_size = os.path.getsize(save_path)
                if current_size == last_size and current_size > 1000:
                    stable_count += 1
                    if stable_count >= 2:  # Stable for 2 consecutive checks (2s)
                        break
                else:
                    stable_count = 0
                last_size = current_size
            time.sleep(1)

        print("✅ File saved:", save_path)

        # ---- Re-type the GPKG path into GM (if dialog still open) ----
        time.sleep(1.0)
        try:
            save_dialog = gw.getWindowsWithTitle("Save As")[0]
            save_dialog.activate()
            time.sleep(0.3)
            pyautogui.typewrite(save_path)
            pyautogui.press("enter")
            print("📂 Re-typed path into Save As dialog")
        except IndexError:
            print("⚠️ Save As dialog not found — skipping re-type")

        # ---- Match layers with PostgreSQL tables ----
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

        print("📄 Matched tables (in memory):", matched_tables)

        # ---- Back to GM and type in tables ----
        gm_window.minimize(); time.sleep(0.1)
        gm_window.restore();  time.sleep(0.1)
        gm_window.activate(); time.sleep(0.1)

        # Open the "Table Selection" dialog: Alt+F, Down, Enter, Enter
        pyautogui.keyDown("alt"); pyautogui.press("f"); pyautogui.keyUp("alt")
        time.sleep(0.08)
        pyautogui.press("down"); time.sleep(0.08)
        pyautogui.press("enter"); time.sleep(0.08)
        pyautogui.press("enter"); time.sleep(0.6)

        # Inside your existing function where matched_tables is already populated
        if matched_tables:
            # First table
            print(f"⌨ Typing first table: {matched_tables[0]}")
            pyautogui.typewrite(matched_tables[0], interval=0.01)
            pyautogui.press("space")
            time.sleep(0.05)

            # Remaining tables
            for tbl in matched_tables[1:]:
                pyautogui.press("down", presses=5, interval=0.05)
                time.sleep(0.05)
                pyautogui.press("tab")
                time.sleep(0.05)
                print(f"⌨ Typing next table: {tbl}")
                pyautogui.typewrite(tbl, interval=0.01)
                pyautogui.press("space")
                time.sleep(0.05)

            print("✅ Finished typing all tables. Pressing Enter...")
            pyautogui.press("enter")

        messagebox.showinfo("Update Map", "Updated table into GM.")

        # === Extra step after clicking OK ===
        try:
            gm_window.minimize(); time.sleep(0.1)
            gm_window.restore(); time.sleep(0.1)
            gm_window.activate(); time.sleep(0.1)

            real_mouse_pos = pyautogui.position()
            left_panel_x = gm_window.left + 25
            left_panel_y = gm_window.top + 500
            pyautogui.moveTo(left_panel_x, left_panel_y)
            pyautogui.rightClick()
            pyautogui.moveTo(real_mouse_pos)

            pyautogui.press("down", presses=3, interval=0.001)
            pyautogui.press("enter")

        except Exception as e:
            print("⚠ Error performing post-update GM action:", e)

        # ---- Cleanup ----
        if os.path.exists(save_path):
            os.remove(save_path)

    except Exception as e:
        messagebox.showerror("Update Map Failed", str(e))




import json, shutil
from tkinter import filedialog

GM_PATH_FILE = "gm_exe_path.json"

def get_global_mapper_path() -> str:
    # 1) previously saved?
    if os.path.exists(GM_PATH_FILE):
        try:
            return json.load(open(GM_PATH_FILE, "r")).get("exe", "")
        except Exception:
            pass

    # 2) common installs
    candidates = [
        r"C:\Program Files\GlobalMapper25.2_64bit\global_mapper.exe",
        r"C:\Program Files\GlobalMapper26.0_64bit\global_mapper.exe",
        r"C:\Program Files\GlobalMapper26.2_64bit\global_mapper.exe",
        r"C:\Program Files\GlobalMapper26_64bit\global_mapper.exe",
        r"C:\Program Files\GlobalMapper27_64bit\global_mapper.exe",
        r"C:\Program Files\GlobalMapper\global_mapper.exe",
    ]
    for p in candidates:
        if os.path.exists(p):
            json.dump({"exe": p}, open(GM_PATH_FILE, "w"))
            return p

    # 3) prompt user
    exe = filedialog.askopenfilename(title="Locate global_mapper.exe",
                                     filetypes=[("Executable", "global_mapper.exe")])
    if exe:
        json.dump({"exe": exe}, open(GM_PATH_FILE, "w"))
    return exe or ""

GM_EXE_PATH = get_global_mapper_path()
if not GM_EXE_PATH:
    messagebox.showerror("Global Mapper", "global_mapper.exe not found. Please locate it.")
    root.destroy()
    sys.exit(1)

LAST_EDITED_FILE = "last_edit_source.json"

def record_edit_source(source_name):
    with open(LAST_EDITED_FILE, "w") as f:
        json.dump({"source": source_name}, f)

def track_popup_close(popup, label):
    def on_close():
        popup_windows[label] -= 1
        if popup_windows[label] <= 0:
            popup_windows[label] = 0
            canvas, bg_id = canvas_refs[label]
            canvas.clicked = False
            canvas.itemconfig(bg_id, image="")
        popup.destroy()
    popup.protocol("WM_DELETE_WINDOW", on_close)

# Launcher: re-run this same EXE with --tool "<LABEL>"
def run_tool_by_label(label: str):
    import sys, os

    exe_path = sys.executable if getattr(sys, 'frozen', False) else sys.executable

    # Map each label to its icon filename (.ico)
    icon_map = {
        "ANY MAP TO LAND PARCEL": "influencemap.ico",
        "ROAD WIDTH": "roadwidth.ico",
        "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO": "roadfrontage.ico",
        "LOT LOCATION": "lotlocation.ico",
        "LAND SHAPE": "landshape.ico",
        "METERS FROM (SCHOOL, SHOP, TRANSPORT, CHURCH)": "distancefrom.ico",
        "LANDMARKS WITHIN METERS": "landmarks200.ico",
        "PARCEL TERRAIN LEVEL": "terrain.ico",
        "ROAD DENSITY": "roaddensity.ico",
        "ROAD SURFACE": "roadsurface.ico",
        "LINEAR REGRESSION": "mlr.ico",
        "RANDOM FOREST": "randomforest1.ico",
        "XG BOOST": "xgboost.ico",
        "ORDINARY LEAST SQUARES": "ols.ico",
        "SPATIAL LAG MODEL": "slm.ico",
        "SPATIAL DURBIN MODEL": "sdm.ico",
        "GEOGRAPHICALLY WEIGHTED REGRESSION": "gwr1.ico",
    }

    icon_name = icon_map.get(label, "BLGF.ico")  # fallback icon

    # 🔹 Pass icon name as argument to subprocess
    p = subprocess.Popen(
        [exe_path, "--tool", label, "--icon", icon_name],
        shell=False,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP  # Windows-safe
    )
    TOOL_PROCESSES.append(p)
    return p

def on_button_click(label):
    print(f"▶ Launching tool: {label}", flush=True)  # debug line
    if label in TOOL_MODULES:
        popup_windows[label] += 1
        run_tool_by_label(label)
    else:
        messagebox.showerror("Unknown Tool", f"No module mapped for: {label}")



def show_login_and_connect():
    login_win = tk.Toplevel()
    apply_icon(login_win)
    login_win.title("Database Login")
    login_win.geometry("260x220")
    login_win.grab_set()
    login_win.resizable(False, False)

    # Load saved credentials if available
    saved = {}
    if os.path.exists("pg_credentials.json"):
        try:
            with open("pg_credentials.json", "r") as f:
                saved = json.load(f)
        except Exception:
            saved = {}

    tk.Label(login_win, text="Host:").grid(row=0, column=0, sticky="e", padx=5, pady=3)
    host_entry = tk.Entry(login_win, width=25)
    host_entry.grid(row=0, column=1)
    host_entry.insert(0, saved.get("host", DB_HOST))

    tk.Label(login_win, text="Port:").grid(row=1, column=0, sticky="e", padx=5, pady=3)
    port_entry = tk.Entry(login_win, width=25)
    port_entry.grid(row=1, column=1)
    port_entry.insert(0, saved.get("port", DB_PORT))

    tk.Label(login_win, text="Database:").grid(row=2, column=0, sticky="e", padx=5, pady=3)
    db_entry = tk.Entry(login_win, width=25)
    db_entry.grid(row=2, column=1)
    db_entry.insert(0, saved.get("database", DB_NAME))

    tk.Label(login_win, text="Schema:").grid(row=3, column=0, sticky="e", padx=5, pady=3)
    schema_entry = tk.Entry(login_win, width=25)
    schema_entry.grid(row=3, column=1)
    schema_entry.insert(0, saved.get("schema", DB_SCHEMA))

    tk.Label(login_win, text="Username:").grid(row=4, column=0, sticky="e", padx=5, pady=3)
    user_entry = tk.Entry(login_win, width=25)
    user_entry.grid(row=4, column=1)
    user_entry.insert(0, saved.get("username", stored_username or ""))

    tk.Label(login_win, text="Password:").grid(row=5, column=0, sticky="e", padx=5, pady=3)
    pass_entry = tk.Entry(login_win, width=25, show="*")
    pass_entry.grid(row=5, column=1)
    pass_entry.insert(0, saved.get("password", stored_password or ""))

    def try_connect():
        username = user_entry.get()
        password = pass_entry.get()

        global stored_username, stored_password, DB_HOST, DB_PORT, DB_NAME, DB_SCHEMA
        stored_username = username
        stored_password = password
        DB_HOST = host_entry.get()
        DB_PORT = port_entry.get()
        DB_NAME = db_entry.get()
        DB_SCHEMA = schema_entry.get()

        try:
            conn = psycopg2.connect(
                host=DB_HOST, port=DB_PORT, database=DB_NAME,
                user=username, password=password
            )
            conn.close()
            login_win.destroy()
            launch_global_mapper()
        except Exception as e:
            messagebox.showerror("Login Failed", f"Could not connect:\n{e}")

        with open("pg_credentials.json", "w") as f:
            json.dump({
                "host": DB_HOST,
                "port": DB_PORT,
                "database": DB_NAME,
                "schema": DB_SCHEMA,
                "username": stored_username,
                "password": stored_password
            }, f)


    tk.Button(login_win, text="Login", command=try_connect, bg="#007acc", fg="white").grid(row=6, columnspan=2, pady=10)


# Hide minimize/maximize, show in taskbar, and disable close
root.title("CAMA Tools")


# Hide minimize and maximize (tool window style), keep close button
root.attributes("-topmost", True)

# Disable the close button functionality
def do_nothing():
    pass
root.protocol("WM_DELETE_WINDOW", do_nothing)

# Enable dragging the borderless window
def start_move(event):
    root.x = event.x
    root.y = event.y

def do_move(event):
    deltax = event.x - root.x
    deltay = event.y - root.y
    x = root.winfo_x() + deltax
    y = root.winfo_y() + deltay
    root.geometry(f"+{x}+{y}")

# Bind the movement to the root window
root.bind("<Button-1>", start_move)
root.bind("<B1-Motion>", do_move)

root.title("CAMA Tools")
root.geometry("100x100")
root.resizable(False, False)


# === IMAGE HANDLING ===
def round_image(img_path, size=(38, 38), radius=7):
    img = Image.open(img_path).convert("RGBA")

    original_ratio = img.width / img.height
    target_w, target_h = size

    if img.width != img.height:
        max_dim = max(img.width, img.height)
        square_img = Image.new("RGBA", (max_dim, max_dim), (0, 0, 0, 0))
        paste_x = (max_dim - img.width) // 2
        paste_y = (max_dim - img.height) // 2
        square_img.paste(img, (paste_x, paste_y))
        img = square_img

    img = img.resize(size, Image.Resampling.LANCZOS)

    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size[0], size[1]), radius=radius, fill=255)

    img.putalpha(mask)
    return ImageTk.PhotoImage(img)

hover_bg = round_image(str(ICONS_DIR / "hover.png"), size=(38, 38), radius=7)

clicked_bg = round_image(str(ICONS_DIR / "click.png"), size=(38, 38), radius=7)

# Map labels to icon filenames (short & clean):
_icon_files = {
    "ANY MAP TO LAND PARCEL": "influencemap.png",
    "ROAD WIDTH": "roadwidth.png",
    "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO": "roadfrontage.png",
    "LOT LOCATION": "lotlocation.png",
    "LAND SHAPE": "landshape.png",
    "METERS FROM (SCHOOL, SHOP, TRANSPORT, CHURCH)": "distancefrom.png",
    "LANDMARKS WITHIN METERS": "landmarks200.png",
    "PARCEL TERRAIN LEVEL": "terrain.png",
    "ROAD DENSITY": "roaddensity.png",
    "ROAD SURFACE": "roadsurface.png",
    "LINEAR REGRESSION": "mlr.png",
    "RANDOM FOREST": "randomforest1.png",
    "XG BOOST": "xgboost.png",
    "ORDINARY LEAST SQUARES": "ols.png",
    "SPATIAL LAG MODEL": "slm.png",
    "SPATIAL DURBIN MODEL": "sdm.png",
    "GEOGRAPHICALLY WEIGHTED REGRESSION": "gwr1.png",
}

icon_paths = {k: str(ICONS_DIR / v) for k, v in _icon_files.items()}
icons = {label: ImageTk.PhotoImage(Image.open(path).resize((39, 39), Image.Resampling.LANCZOS))
         for label, path in icon_paths.items()}


# === TOOLBAR BUTTONS PANEL (blue area) ===
button_frame = tk.Frame(root, bg="#afd0f7", width=310, height=160) #310 height #310 width
button_frame.pack(padx=3, pady=(5, 0))
button_frame.pack_propagate(False)

# === TOP ROW AND BOTTOM ROW CONTAINERS ===
first_row = tk.Frame(button_frame, bg="#afd0f7")
first_row.pack(side="top", anchor="w", pady=(2, 0))

second_row = tk.Frame(button_frame, bg="#afd0f7")
second_row.pack(side="top", anchor="w", pady=(0, 4))

third_row = tk.Frame(button_frame, bg="#afd0f7")
third_row.pack(side="top", anchor="w", pady=(0, 4))

fourth_row = tk.Frame(button_frame, bg="#afd0f7")
fourth_row.pack(side="top", anchor="w", pady=(0, 6))

# === Tooltip descriptions for icon buttons ===
tooltip_descriptions = {
    "ANY MAP TO LAND PARCEL": "Any map source to Land Parcel",
    "ROAD WIDTH": "Measure average road width",
    "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO": "Analyze parcel depth and frontage",
    "LOT LOCATION": "Classify lots based on proximity",
    "LAND SHAPE": "Assess lot geometry and compactness",
    "METERS FROM (SCHOOL, SHOP, TRANSPORT, CHURCH)": "Measure distance to nearest POIs",
    "LANDMARKS WITHIN METERS": "Check nearby landmark coverage",
    "PARCEL TERRAIN LEVEL": "Analyze slope and elevation difference",
    "ROAD DENSITY": "Calculate road concentration in area",
    "ROAD SURFACE": "Identify surface type of nearby roads",
    "LINEAR REGRESSION": "Run linear model on land data",
    "RANDOM FOREST": "Train a Random Forest model",
    "XG BOOST": "Train data using XG Boost",
    "ORDINARY LEAST SQUARES": "Train data using Ordinary Least Squares",
    "SPATIAL LAG MODEL": "Train data using Spatial Lag Model",
    "SPATIAL DURBIN MODEL": "Train data using Spatial Durbin Model",
    "GEOGRAPHICALLY WEIGHTED REGRESSION": "Perform Geographically Weighted Regression"
}


# === BUTTON DEFINITIONS ===
buttons_1st_row = [
    "ANY MAP TO LAND PARCEL",
    "ROAD WIDTH",
    "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO",
    "LOT LOCATION",
    "LAND SHAPE",
    "METERS FROM (SCHOOL, SHOP, TRANSPORT, CHURCH)",
]

buttons_2nd_row = [
    "LANDMARKS WITHIN METERS",
    "PARCEL TERRAIN LEVEL",
    "ROAD DENSITY",
    "ROAD SURFACE"
]

buttons_3rd_row = [
    "LINEAR REGRESSION",
    "RANDOM FOREST",
    "XG BOOST"
]

buttons_4th_row = [
    "ORDINARY LEAST SQUARES",
    "SPATIAL LAG MODEL",
    "SPATIAL DURBIN MODEL",
    "GEOGRAPHICALLY WEIGHTED REGRESSION"
]

popup_windows = {}
canvas_refs = {}



# === GROUP TITLE: Feature Management Tools ===
feature_title = tk.Label(button_frame, text="Feature Management Tools", font=("Segoe UI", 9, "bold"), bg="#afd0f7", anchor="w")
feature_title.pack(side="top", anchor="w", padx=5, pady=(3, 1))

first_row = tk.Frame(button_frame, bg="#afd0f7")
first_row.pack(side="top", anchor="w", pady=(0, 2))

second_row = tk.Frame(button_frame, bg="#afd0f7")
second_row.pack(side="top", anchor="w", pady=(0, 8))

# === GROUP TITLE: AI Model Tools ===
ai_title = tk.Label(button_frame, text="AI Model Tools", font=("Segoe UI", 9, "bold"), bg="#afd0f7", anchor="w")
ai_title.pack(side="top", anchor="w", padx=5, pady=(0, 1))

third_row = tk.Frame(button_frame, bg="#afd0f7")
third_row.pack(side="top", anchor="w", pady=(0, 4))

# === GROUP TITLE: Geostatistical Model Tools ===
ai_title = tk.Label(button_frame, text="Geostatistical Model Tools", font=("Segoe UI", 9, "bold"), bg="#afd0f7", anchor="w")
ai_title.pack(side="top", anchor="w", padx=5, pady=(0, 1))

fourth_row = tk.Frame(button_frame, bg="#afd0f7")
fourth_row.pack(side="top", anchor="w", pady=(0, 4))

# === 1st ROW BUTTONS WITH INDIVIDUAL CONTROL ===
for label in buttons_1st_row:
    canvas = tk.Canvas(first_row, width=48, height=48, highlightthickness=0, bg="#afd0f7")
    bg_img_id = canvas.create_image(3, 3, anchor="nw", image=None)
    icon_img_id = canvas.create_image(2, 2, anchor="nw", image=icons[label])

    canvas_refs[label] = (canvas, bg_img_id)
    popup_windows[label] = 0

    def make_bindings(c, lbl, bg_id):
        def on_enter(e):
            c.itemconfig(bg_id, image=hover_bg)
        def on_leave(e):
            c.itemconfig(bg_id, image="")
        def on_click(e):
            on_button_click(lbl)
        c.bind("<Enter>", on_enter)
        c.bind("<Leave>", on_leave)
        c.bind("<Button-1>", on_click)

    make_bindings(canvas, label, bg_img_id)

    # ✅ Tooltip with icon and label
    add_tooltip(canvas, icon_paths[label], label, tooltip_descriptions.get(label, "Launch tool"), canvas=canvas, bg_id=bg_img_id)
    canvas.pack(side="left", padx=(2, 2), pady=(2, 2))

    # Per-button packing adjustments
    if label == "ANY MAP TO LAND PARCEL":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "ROAD WIDTH":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "LOT LOCATION":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "LAND SHAPE":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))

# === 2nd ROW BUTTONS WITH INDIVIDUAL CONTROL ===
for label in buttons_2nd_row:
    canvas = tk.Canvas(second_row, width=48, height=48, highlightthickness=0, bg="#afd0f7")
    bg_img_id = canvas.create_image(3, 3, anchor="nw", image=None)
    icon_img_id = canvas.create_image(2, 2, anchor="nw", image=icons[label])

    canvas_refs[label] = (canvas, bg_img_id)
    popup_windows[label] = 0

    def make_bindings(c, lbl, bg_id):
        def on_enter(e):
            c.itemconfig(bg_id, image=hover_bg)
        def on_leave(e):
            c.itemconfig(bg_id, image="")
        def on_click(e):
            on_button_click(lbl)
        c.bind("<Enter>", on_enter)
        c.bind("<Leave>", on_leave)
        c.bind("<Button-1>", on_click)

    make_bindings(canvas, label, bg_img_id)

    # ✅ Tooltip with icon and label
    add_tooltip(canvas, icon_paths[label], label, tooltip_descriptions.get(label, "Launch tool"), canvas=canvas, bg_id=bg_img_id)
    canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
  
    # Per-button packing adjustments
    if label == "METERS FROM (SCHOOL, SHOP, TRANSPORT, CHURCH)":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "LANDMARKS WITHIN METERS":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "PARCEL TERRAIN LEVEL":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "ROAD DENSITY":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "ROAD SURFACE":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))

# === 3rd ROW BUTTONS WITH INDIVIDUAL CONTROL ===
for label in buttons_3rd_row:
    canvas = tk.Canvas(third_row, width=48, height=48, highlightthickness=0, bg="#afd0f7")
    bg_img_id = canvas.create_image(3, 3, anchor="nw", image=None)
    icon_img_id = canvas.create_image(2, 2, anchor="nw", image=icons[label])

    canvas_refs[label] = (canvas, bg_img_id)
    popup_windows[label] = 0

    def make_bindings(c, lbl, bg_id):
        def on_enter(e):
            c.itemconfig(bg_id, image=hover_bg)
        def on_leave(e):
            c.itemconfig(bg_id, image="")
        def on_click(e):
            on_button_click(lbl)
        c.bind("<Enter>", on_enter)
        c.bind("<Leave>", on_leave)
        c.bind("<Button-1>", on_click)

    make_bindings(canvas, label, bg_img_id)

    # ✅ Tooltip with icon and label
    add_tooltip(canvas, icon_paths[label], label, tooltip_descriptions.get(label, "Launch tool"), canvas=canvas, bg_id=bg_img_id)
    canvas.pack(side="left", padx=(2, 2), pady=(2, 2))

    # Per-button packing adjustments
    if label == "LINEAR REGRESSION":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "RANDOM FOREST":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "XG BOOST":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))

# === 4th ROW BUTTONS WITH INDIVIDUAL CONTROL ===
for label in buttons_4th_row:
    canvas = tk.Canvas(fourth_row, width=48, height=48, highlightthickness=0, bg="#afd0f7")
    bg_img_id = canvas.create_image(3, 3, anchor="nw", image=None)
    icon_img_id = canvas.create_image(2, 2, anchor="nw", image=icons[label])

    canvas_refs[label] = (canvas, bg_img_id)
    popup_windows[label] = 0

    def make_bindings(c, lbl, bg_id):
        def on_enter(e):
            c.itemconfig(bg_id, image=hover_bg)
        def on_leave(e):
            c.itemconfig(bg_id, image="")
        def on_click(e):
            on_button_click(lbl)
        c.bind("<Enter>", on_enter)
        c.bind("<Leave>", on_leave)
        c.bind("<Button-1>", on_click)

    make_bindings(canvas, label, bg_img_id)

    # ✅ Tooltip with icon and label
    add_tooltip(canvas, icon_paths[label], label, tooltip_descriptions.get(label, "Launch tool"), canvas=canvas, bg_id=bg_img_id)
    canvas.pack(side="left", padx=(2, 2), pady=(2, 2))

    # Per-button packing adjustments
    if label == "ORDINARY LEAST SQUARES":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "SPATIAL LAG MODEL":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "SPATIAL DURBIN MODEL":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "GEOGRAPHICALLY WEIGHTED REGRESSION":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
        

# Create a frame to hold both buttons
btn_frame = tk.Frame(root)
btn_frame.pack(pady=15)  # One padding for the whole row



# === UPDATE MAP BUTTON ===
update_map_btn = tk.Button(
    btn_frame, text="Update Map",
    width=20,
    bg="#6a9f2f",
    fg="white",
    activebackground="#4c7a20",
    activeforeground="white",
    relief="flat",
    command=update_map_and_select_recorded
)
update_map_btn.pack(side="left", padx=5)  # Add horizontal spacing

# === UPDATE DB BUTTON ===
update_btn = tk.Button(
    btn_frame, text="Update Database",
    width=20,
    bg="#007acc",
    fg="white",
    activebackground="#005f99",
    activeforeground="white",
    relief="flat",
    command=update_database_from_geopackage
)
update_btn.pack(side="left", padx=5)  # Add horizontal spacing



def launch_main_window():
    gm_windows = [w for w in gw.getWindowsWithTitle('Global Mapper Pro') if w.visible]
    if gm_windows:
        gm_win = gm_windows[0]
        root.geometry(
            f"+{gm_win.left + gm_win.width - 310}"
            f"+{gm_win.top + gm_win.height - 350}"
        )

    root.deiconify()
    root.lift()
    root.attributes('-topmost', True)
    root.after(100, lambda: root.attributes('-topmost', False))



root.withdraw()  # Hide the UI initially

import pygetwindow as gw
import time

def launch_global_mapper():
    import re
    import shutil
    import tempfile

    gmw_path = selected_gmw_file
    patched_path = gmw_path  # default: use as-is

    try:
        with open(gmw_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        # Patch DB_NAME
        content = re.sub(
            r'(POSTGIS_DATABASE\s*=\s*")[^"]*(")',
            lambda m: m.group(1) + DB_NAME + m.group(2),
            content
        )
        # Patch DB_HOST
        content = re.sub(
            r'(POSTGIS_HOST\s*=\s*")[^"]*(")',
            lambda m: m.group(1) + DB_HOST + m.group(2),
            content
        )
        # Patch DB_PORT
        content = re.sub(
            r'(POSTGIS_PORT\s*=\s*")[^"]*(")',
            lambda m: m.group(1) + DB_PORT + m.group(2),
            content
        )
        # Patch username
        content = re.sub(
            r'(POSTGIS_USER\s*=\s*")[^"]*(")',
            lambda m: m.group(1) + stored_username + m.group(2),
            content
        )
        # Patch password
        content = re.sub(
            r'(POSTGIS_PASSWORD\s*=\s*")[^"]*(")',
            lambda m: m.group(1) + stored_password + m.group(2),
            content
        )

        # Write patched content to a temp file so we don't overwrite the original
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".gmw", delete=False,
            encoding="utf-8", prefix="cama_patched_"
        )
        tmp.write(content)
        tmp.close()
        patched_path = tmp.name
        print(f"✅ Patched .gmw written to: {patched_path}")

    except Exception as e:
        print(f"⚠ Could not patch .gmw file: {e} — launching with original")

    subprocess.Popen([GM_EXE_PATH, patched_path], shell=False)
    wait_for_global_mapper()

prev_position = [None, None]

def monitor_gm_state():
    try:
        gm_windows = [w for w in gw.getWindowsWithTitle('Global Mapper Pro') if w.visible]
        if gm_windows:
            gm_win = gm_windows[0]

            if gm_win.isMinimized:
                if root.state() != 'withdrawn':
                    root.withdraw()  # hide the tools only if not already hidden
            else:
                if root.state() == 'withdrawn':
                    root.deiconify()  # show the tools again

                # Only lift if not already on top
                root.lift()

        else:
            print("❌ Global Mapper closed. Closing Tkinter.")
            root.destroy()

    except Exception as e:
        print("Error in GM monitor:", e)

    # Call again after some time
    root.after(1000, monitor_gm_state)

def monitor_gm_closure():
    gm_windows = [w for w in gw.getWindowsWithTitle('Global Mapper Pro') if w.visible]
    if not gm_windows:
        print("❌ Global Mapper closed. Exiting tools.")
        root.destroy()
    else:
        root.after(2000, monitor_gm_closure)


def wait_for_global_mapper():
    gm_windows = [w for w in gw.getWindowsWithTitle('Global Mapper Pro') if w.visible]
    if gm_windows:
        print("✅ Global Mapper is open.")
        launch_main_window()
        monitor_gm_state()
        monitor_gm_closure()   # ← ADD THIS
    else:
        print("⏳ Waiting for Global Mapper...")
        root.after(1000, wait_for_global_mapper)

# Step 1: Prompt for .gmw file first
gmw_file = filedialog.askopenfilename(
    title="Select Global Mapper Workspace File",
    filetypes=[("Global Mapper Workspace", "*.gmw")]
)

if not gmw_file:
    try:
        # optional: wag na mag-warning kung ayaw mo ng popup
        messagebox.showwarning("Cancelled", "No GMW file selected. Exiting.")
    except Exception:
        pass

    try:
        root.quit()
        root.destroy()
    except Exception:
        pass

    import sys
    sys.exit(0)   # <-- IMPORTANT: clean exit code for PyInstaller


selected_gmw_file = gmw_file
show_login_and_connect()

# Step 4: Enter main loop
root.mainloop()