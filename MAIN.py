
import atexit
import signal

TOOL_PROCESSES = []

# ============================
# FORCE WINDOWS APP ICON
# ============================
import ctypes
import ctypes.wintypes
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
            import inspect
            sig = inspect.signature(mod.main)
            if sig.parameters:
                # Create proper hidden root BEFORE any icon calls
                _tool_root = tk.Tk()
                _tool_root.withdraw()
                _tool_root.geometry("1x1+-9999+-9999")

                # Apply icon bound to THIS root — never reuse PhotoImage
                # from another Tk instance (causes TclError)
                ico = resource_path("BLGF.ico")
                png = resource_path("BLGF.png")
                if os.path.exists(ico):
                    try:
                        _tool_root.iconbitmap(ico)
                    except Exception:
                        pass
                if os.path.exists(png):
                    try:
                        _img = tk.PhotoImage(file=png, master=_tool_root)
                        _tool_root.iconphoto(True, _img)
                        _tool_root._icon_ref = _img  # prevent GC
                    except Exception:
                        pass

                mod.main(_tool_root)
                _tool_root.mainloop()
            else:
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


DB_HOST = ""
DB_PORT = "5432"
DB_NAME = ""
DB_SCHEMA = ""
DEFAULT_DB_USERNAME = "postgres"
DEFAULT_DB_PASSWORD = ""

selected_gmw_file = None

# Initialize stored credentials with defaults
stored_username = DEFAULT_DB_USERNAME
stored_password = DEFAULT_DB_PASSWORD

if not IS_TOOL_RUN:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-alpha", 0)
    root.geometry("1x1+-9999+-9999")
    root.update_idletasks()               # flush any pending window creation events
    apply_icon(root)                      # safe to call now — window is invisible
    root.minsize(340, 200)
    # No fixed geometry — content determines size
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
    tooltip.withdraw()
    tooltip.overrideredirect(True)        # overrideredirect BEFORE apply_icon
    # Skip apply_icon on tooltips — they're borderless so icon is never shown
    # and iconbitmap/iconphoto cause a flash on Toplevel creation
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

    def get_bounding_rect():
        """Combined bounds of GM window + CAMA window, so tooltip never escapes either."""
        rects = []

        gm = get_gm_rect()  # (left, top, w, h) or None
        if gm:
            gl, gt, gw_, gh = gm
            rects.append((gl, gt, gl + gw_, gt + gh))

        cw, ch = get_cama_size()
        cl, ct = root.winfo_x(), root.winfo_y()
        rects.append((cl, ct, cl + cw, ct + ch))

        if not rects:
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            return (0, 0, sw, sh)

        left   = min(r[0] for r in rects)
        top    = min(r[1] for r in rects)
        right  = max(r[2] for r in rects)
        bottom = max(r[3] for r in rects)
        return (left, top, right, bottom)

    def enter(event):
        tooltip.update_idletasks()
        tip_w = tooltip.winfo_reqwidth()
        tip_h = tooltip.winfo_reqheight()

        bl, bt, br, bb = get_bounding_rect()

        x = widget.winfo_rootx() + 45
        y = widget.winfo_rooty() + 10

        # Clamp horizontally
        if x + tip_w > br:
            x = widget.winfo_rootx() - tip_w - 10  # flip to the left of the widget
            if x < bl:
                x = br - tip_w  # last resort: pin to right edge of bounds
        if x < bl:
            x = bl

        # Clamp vertically
        if y + tip_h > bb:
            y = bb - tip_h
        if y < bt:
            y = bt

        tooltip.geometry(f"+{int(x)}+{int(y)}")
        tooltip.deiconify()
        tooltip.lift()
        tooltip.attributes("-topmost", True)

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

_base_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
            else os.path.dirname(os.path.abspath(__file__))
GM_PATH_FILE = os.path.join(_base_dir, "gm_exe_path.json")

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

GM_EXE_PATH = ""  # Will be resolved after login

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
    import sys, os, threading

    IS_FROZEN = getattr(sys, 'frozen', False)

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

    icon_name = icon_map.get(label, "BLGF.ico")

    if IS_FROZEN:
        # ── Production: spawn a new process (existing behaviour) ──
        exe_path = sys.executable
        p = subprocess.Popen(
            [exe_path, "--tool", label, "--icon", icon_name],
            shell=False,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        )
        TOOL_PROCESSES.append(p)
        return p
    else:
        # ── Dev / VS Code: import and run the tool on a thread ──
        mod_path = TOOL_MODULES.get(label)
        if not mod_path:
            messagebox.showerror("Tool Error", f"No module mapped for: {label}")
            return None

        def run_in_thread():
            _active_tool_titles.add(label)
            try:
                import importlib
                mod = importlib.import_module(mod_path)
                importlib.reload(mod)
                if hasattr(mod, "main") and callable(mod.main):
                    # Pass main3's existing hidden root so the tool never
                    # creates its own tk.Tk() — that's what causes the taskbar icon
                    import inspect
                    sig = inspect.signature(mod.main)
                    if sig.parameters:
                        mod.main(root)   # tool accepts a parent root
                    else:
                        mod.main()       # fallback for tools not yet updated
            except Exception:
                import traceback
                messagebox.showerror("Tool Crash", f"{mod_path}\n\n{traceback.format_exc()}")
            finally:
                _active_tool_titles.discard(label)

        t = threading.Thread(target=run_in_thread, daemon=True)
        t.start()

        # Return a dummy object so callers that check .pid / .poll() don't crash
        class _FakeProcess:
            pid = -1
            def poll(self): return None   # pretend still running

        fake = _FakeProcess()
        TOOL_PROCESSES.append(fake)
        return fake

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

    def on_login_close():
        try:
            messagebox.showwarning("Cancelled", "Login cancelled. Exiting.")
        except Exception:
            pass
        try:
            root.quit()
            root.destroy()
        except Exception:
            pass
        sys.exit(0)

    login_win.protocol("WM_DELETE_WINDOW", on_login_close)

    # Load saved credentials if available
    _creds_path = os.path.join(
        os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)),
        "pg_credentials.json"
    )
    saved = {}
    if os.path.exists(_creds_path):
        try:
            with open(_creds_path, "r") as f:
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

        _creds_path = os.path.join(
            os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)),
            "pg_credentials.json"
        )
        with open(_creds_path, "w") as f:
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

# Hide from taskbar using tool window style
import ctypes
GWL_EXSTYLE    = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW  = 0x00040000
SWP_NOSIZE     = 0x0001
SWP_NOMOVE     = 0x0002
SWP_NOACTIVATE = 0x0010
HWND_TOPMOST   = -1
SWP_NOSIZE     = 0x0001
SWP_NOMOVE     = 0x0002
SWP_NOACTIVATE = 0x0010

def hide_from_taskbar():
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
    ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)

root.after(100, hide_from_taskbar)

# Disable the close button functionality
def do_nothing():
    pass
root.protocol("WM_DELETE_WINDOW", do_nothing)

# ── GM canvas offsets (skip GM's panels/toolbars) ────────────────────
GM_TITLEBAR_H   = 130
GM_LEFT_PANEL_W = 240

# ── Get CAMA and GM sizes via Win32 (always accurate) ────────────────
def get_cama_size():
    cama_hwnd = ctypes.windll.user32.FindWindowW(None, "CAMA Tools")
    if cama_hwnd:
        r = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(cama_hwnd, ctypes.byref(r))
        return r.right - r.left, r.bottom - r.top
    return root.winfo_width(), root.winfo_height()

def get_gm_rect():
    try:
        gm_wins = [w for w in gw.getWindowsWithTitle('Global Mapper Pro') if w.visible]
        if gm_wins:
            g = gm_wins[0]
            return g.left, g.top, g.width, g.height
    except Exception:
        pass
    return None

def get_hwnd_by_title(partial_title):
    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
        if partial_title.lower() in buf.value.lower():
            found.append(hwnd)
        return True
    ctypes.windll.user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found[0] if found else None

# ── Foreground-window focus tracking ──────────────────────────────
def get_foreground_hwnd():
    return ctypes.windll.user32.GetForegroundWindow()

def hwnd_title(hwnd):
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value

def hwnd_belongs_to(hwnd, titles_substrings):
    title = hwnd_title(hwnd).lower()
    return any(t.lower() in title for t in titles_substrings)

def get_foreground_pid():
    hwnd = get_foreground_hwnd()
    pid = ctypes.wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value

def is_relevant_window_focused():
    """
    True if the foreground window is Global Mapper, CAMA Tools itself,
    or one of the CAMA tool subprocess windows (Road Width, Land Shape, etc.)
    """
    fg = get_foreground_hwnd()
    if hwnd_belongs_to(fg, ["Global Mapper", "CAMA Tools"]):
        return True

    # Frozen mode: match by subprocess PID
    fg_pid = get_foreground_pid()
    if any(p.pid == fg_pid for p in TOOL_PROCESSES if p.poll() is None):
        return True

    # Dev mode: match by foreground window title against active tool labels
    if _active_tool_titles:
        fg_title = hwnd_title(fg).lower()
        return any(t.lower() in fg_title or fg_title in t.lower()
                   for t in _active_tool_titles)

    return False

def clamp_position(new_x, new_y):
    """Clamp CAMA position strictly inside GM's map canvas area."""
    gm = get_gm_rect()
    if not gm:
        return new_x, new_y
    gm_left, gm_top, gm_w, gm_h = gm
    cama_w, cama_h = get_cama_size()

    min_x = gm_left + GM_LEFT_PANEL_W
    min_y = gm_top  + GM_TITLEBAR_H
    max_x = gm_left + gm_w - cama_w
    max_y = gm_top  + gm_h - cama_h

    cx = max(min_x, min(new_x, max_x))
    cy = max(min_y, min(new_y, max_y))

    # Update relative offset for GM-follow
    cama_offset[0] = cx - gm_left
    cama_offset[1] = cy - gm_top

    return cx, cy

# ── Client-area drag (anywhere in the tkinter widget area) ───────────
_drag_origin = [0, 0]   # screen coords of click minus root origin

def start_move(event):
    _drag_origin[0] = event.x_root - root.winfo_x()
    _drag_origin[1] = event.y_root - root.winfo_y()

def do_move(event):
    new_x = event.x_root - _drag_origin[0]
    new_y = event.y_root - _drag_origin[1]
    new_x, new_y = clamp_position(new_x, new_y)
    root.geometry(f"+{new_x}+{new_y}")

def bind_drag_to_all(widget):
    # Only bind to frames/labels/root — skip canvases (they have tool bindings)
    if not isinstance(widget, tk.Canvas):
        widget.bind("<Button-1>", start_move, add="+")
        widget.bind("<B1-Motion>", do_move,   add="+")
    for child in widget.winfo_children():
        bind_drag_to_all(child)

bind_drag_to_all(root)

# ── Title-bar drag — intercept WM_MOVING at Win32 level ──────────────
WM_MOVING   = 0x0216
GWL_WNDPROC = -4

WNDPROCTYPE = ctypes.WINFUNCTYPE(
    ctypes.c_longlong,
    ctypes.wintypes.HWND,
    ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM
)

# Set correct arg/return types for Win32 calls
ctypes.windll.user32.SetWindowLongPtrW.restype  = ctypes.c_longlong
ctypes.windll.user32.SetWindowLongPtrW.argtypes = [
    ctypes.wintypes.HWND,
    ctypes.c_int,
    WNDPROCTYPE
]
ctypes.windll.user32.CallWindowProcW.restype  = ctypes.c_longlong
ctypes.windll.user32.CallWindowProcW.argtypes = [
    ctypes.c_longlong,
    ctypes.wintypes.HWND,
    ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM
]

_old_wnd_proc = None

def _new_wnd_proc(hwnd, msg, wparam, lparam):
    try:
        if msg == WM_MOVING:
            proposed = ctypes.cast(lparam, ctypes.POINTER(ctypes.wintypes.RECT)).contents
            cx, cy = clamp_position(proposed.left, proposed.top)
            cw, ch = get_cama_size()
            proposed.left   = cx
            proposed.top    = cy
            proposed.right  = cx + cw
            proposed.bottom = cy + ch
            return 1
    except Exception as e:
        print("WndProc error:", e)
    return ctypes.windll.user32.CallWindowProcW(
        _old_wnd_proc, hwnd, msg, wparam, lparam
    )

def install_wm_moving_hook():
    global _old_wnd_proc
    cama_hwnd = ctypes.windll.user32.FindWindowW(None, "CAMA Tools")
    if not cama_hwnd:
        root.after(300, install_wm_moving_hook)
        return
    proc = WNDPROCTYPE(_new_wnd_proc)
    root._wnd_proc_ref = proc   # prevent GC
    _old_wnd_proc = ctypes.windll.user32.SetWindowLongPtrW(
        cama_hwnd, GWL_WNDPROC, proc
    )
    print("✅ WM_MOVING hook installed")

root.after(400, install_wm_moving_hook)

root.title("CAMA Tools")
root.resizable(False, False)
# No fixed geometry — root will wrap tightly around content


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
button_frame = tk.Frame(root, bg="#afd0f7", width=310)
button_frame.pack(padx=8, pady=(6, 6))
# No pack_propagate(False) — let it size naturally to its content

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

popup_windows = {}
canvas_refs = {}



# === GROUP TITLE: Feature Management Tools ===
feature_title = tk.Label(button_frame, text="Feature Management Tools", font=("Segoe UI", 9, "bold"), bg="#afd0f7", anchor="w")
feature_title.pack(side="top", anchor="w", padx=8, pady=(4, 1))

first_row = tk.Frame(button_frame, bg="#afd0f7")
first_row.pack(side="top", anchor="w", padx=4, pady=(0, 2))

second_row = tk.Frame(button_frame, bg="#afd0f7")
second_row.pack(side="top", anchor="w", padx=4, pady=(0, 6))

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
        

# Create a frame to hold both buttons
btn_frame = tk.Frame(root)
btn_frame.pack(pady=(6, 6))



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
    root.update_idletasks()  # ensure actual size is computed first
    cama_w = root.winfo_reqwidth()
    cama_h = root.winfo_reqheight()

    if gm_windows:
        gm_win = gm_windows[0]
        new_x = gm_win.left + gm_win.width - cama_w - 10
        new_y = gm_win.top + gm_win.height - cama_h - 40
        root.geometry(f"+{new_x}+{new_y}")
    else:
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        root.geometry(f"+{(sw - cama_w) // 2}+{(sh - cama_h) // 2}")

    root.update_idletasks()
    root.attributes("-alpha", 1)
    root.deiconify()
    root.lift()
    # Pin as topmost at Win32 level — more reliable than tkinter's -topmost
    cama_hwnd = get_hwnd_by_title("CAMA Tools")
    if cama_hwnd:
        ctypes.windll.user32.SetWindowPos(
            cama_hwnd, HWND_TOPMOST,
            0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
        )

    # Force Z-order above GM immediately after showing
    def _force_z_order():
        cama_hwnd = get_hwnd_by_title("CAMA Tools")
        if cama_hwnd:
            ctypes.windll.user32.SetWindowPos(
                cama_hwnd, HWND_TOPMOST,
                0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
            )

    root.after(500, _force_z_order)   # after GM settles
    root.after(1500, _force_z_order)  # second pass in case GM repaints on top


import pygetwindow as gw
import time

def launch_global_mapper():
    import re
    import shutil
    import tempfile

    global GM_EXE_PATH
    if not GM_EXE_PATH:
        GM_EXE_PATH = get_global_mapper_path()
    if not GM_EXE_PATH:
        messagebox.showerror("Global Mapper", "global_mapper.exe not found. Please locate it.")
        return

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

prev_position  = [None, None]
prev_gm_rect   = [None, None, None, None]  # left, top, width, height
cama_offset    = [None, None]              # CAMA's offset relative to GM
_topmost_recheck_counter = [0]             # throttles repeated SetWindowPos calls to avoid title-bar flicker
_active_tool_titles = set()               # tracks open tool window titles in dev mode

def monitor_gm_state():
    try:
        gm_windows = [w for w in gw.getWindowsWithTitle('Global Mapper Pro') if w.visible]
        if gm_windows:
            gm_win = gm_windows[0]

            if gm_win.isMinimized or not is_relevant_window_focused():
                if root.state() != 'withdrawn':
                    root.withdraw()
            else:
                just_shown = (root.state() == 'withdrawn')
                if just_shown:
                    root.attributes("-alpha", 1)
                    root.deiconify()

                cama_hwnd = get_hwnd_by_title("CAMA Tools")
                gm_hwnd   = get_hwnd_by_title("Global Mapper Pro")

                # --- Z-order: only re-pin topmost when just shown, or occasionally ---
                # Calling SetWindowPos every 200ms causes visible title-bar flicker.
                if cama_hwnd:
                    if just_shown:
                        ctypes.windll.user32.SetWindowPos(
                            cama_hwnd, HWND_TOPMOST,
                            0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                        )
                    else:
                        _topmost_recheck_counter[0] += 1
                        if _topmost_recheck_counter[0] >= 10:  # ~every 2s instead of every 200ms
                            _topmost_recheck_counter[0] = 0
                            ctypes.windll.user32.SetWindowPos(
                                cama_hwnd, HWND_TOPMOST,
                                0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                            )

                # --- Follow GM when it moves ---
                gm_left   = gm_win.left
                gm_top    = gm_win.top
                gm_w      = gm_win.width
                gm_h      = gm_win.height

                gm_moved = (
                    prev_gm_rect[0] != gm_left or
                    prev_gm_rect[1] != gm_top  or
                    prev_gm_rect[2] != gm_w    or
                    prev_gm_rect[3] != gm_h
                )

                if gm_moved:
                    if cama_offset[0] is None:
                        # First time — set offset from current CAMA position
                        cama_offset[0] = root.winfo_x() - gm_left
                        cama_offset[1] = root.winfo_y() - gm_top
                    else:
                        # GM moved — reposition CAMA using saved offset
                        new_x = gm_left + cama_offset[0]
                        new_y = gm_top  + cama_offset[1]

                        # Clamp inside GM bounds
                        cama_w = root.winfo_width()
                        cama_h = root.winfo_height()
                        new_x = max(gm_left + GM_LEFT_PANEL_W,
                                    min(new_x, gm_left + gm_w - cama_w))
                        new_y = max(gm_top  + GM_TITLEBAR_H,
                                    min(new_y, gm_top  + gm_h - cama_h))

                        root.geometry(f"+{new_x}+{new_y}")

                    prev_gm_rect[0] = gm_left
                    prev_gm_rect[1] = gm_top
                    prev_gm_rect[2] = gm_w
                    prev_gm_rect[3] = gm_h

        else:
            print("❌ Global Mapper closed. Closing Tkinter.")
            root.destroy()

    except Exception as e:
        print("Error in GM monitor:", e)

    root.after(200, monitor_gm_state)

def monitor_gm_closure():
    gm_windows = [w for w in gw.getWindowsWithTitle('Global Mapper Pro') if w.visible]
    if not gm_windows:
        print("❌ Global Mapper closed. Exiting tools.")
        root.destroy()
    else:
        root.after(2000, monitor_gm_closure)


_gm_stable_count = [0]  # needs to be visible twice before we consider it ready

def wait_for_global_mapper():
    gm_windows = [w for w in gw.getWindowsWithTitle('Global Mapper Pro') if w.visible]
    # Require GM window to be visible AND non-minimized AND have a real size
    ready = (
        gm_windows and
        not gm_windows[0].isMinimized and
        gm_windows[0].width > 100 and
        gm_windows[0].height > 100
    )
    if ready:
        _gm_stable_count[0] += 1
        if _gm_stable_count[0] >= 2:      # stable for 2 consecutive checks (2s)
            print("✅ Global Mapper is fully open.")
            launch_main_window()
            monitor_gm_state()
            monitor_gm_closure()
            return
    else:
        _gm_stable_count[0] = 0           # reset if GM disappears or isn't ready

    print("⏳ Waiting for Global Mapper...")
    root.after(1000, wait_for_global_mapper)

# Step 1: Resize native dialog to medium centered via ctypes
import threading

def resize_file_dialog():
    import time
    time.sleep(0.25)
    hwnd = ctypes.windll.user32.FindWindowW(None, "Select Global Mapper Workspace File")
    if hwnd:
        screen_w = ctypes.windll.user32.GetSystemMetrics(0)
        screen_h = ctypes.windll.user32.GetSystemMetrics(1)
        win_w, win_h = 780, 500
        x = (screen_w - win_w) // 2
        y = (screen_h - win_h) // 2
        ctypes.windll.user32.MoveWindow(hwnd, x, y, win_w, win_h, True)

def startup_sequence():
    global selected_gmw_file

    threading.Thread(target=resize_file_dialog, daemon=True).start()

    gmw_file = filedialog.askopenfilename(
        title="Select Global Mapper Workspace File",
        filetypes=[("Global Mapper Workspace", "*.gmw")]
    )

    if not gmw_file:
        try:
            messagebox.showwarning("Cancelled", "No GMW file selected. Exiting.")
        except Exception:
            pass
        try:
            root.quit()
            root.destroy()
        except Exception:
            pass
        sys.exit(0)

    selected_gmw_file = gmw_file
    show_login_and_connect()

root.after(0, startup_sequence)
root.mainloop()