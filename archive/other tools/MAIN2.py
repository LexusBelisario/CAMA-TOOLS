
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox
import subprocess
import os
import json
import psycopg2
from rapidfuzz import process, fuzz
from PIL import Image, ImageTk, ImageDraw

confirm_win = None
confirm_clicked = {'ok': False}


DB_HOST = "localhost"
DB_PORT = "5432"
DB_NAME = "BLGF_DBTEST"
DB_SCHEMA = "PAGSANJAN_LAGUNA"

selected_gmw_file = None

stored_username = None
stored_password = None

root = tk.Tk()

root.geometry(f"355x410")
root.minsize(355, 380) #355 height #380width

import geopandas as gpd
import fiona
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from rapidfuzz import fuzz
from tkinter import filedialog, messagebox

from rapidfuzz import process, fuzz
import re

def _normalize_name(name: str, schema_prefix: str = "") -> str:
    """
    Lowercase, strip schema prefix, remove extension, drop text in parentheses,
    and collapse non-alnum to underscores.
    """
    name = name.strip()
    if "(" in name:
        name = name.split("(")[0]
    if "." in name:
        name = name.split(".")[0]
    if schema_prefix and name.lower().startswith(schema_prefix.lower()):
        name = name[len(schema_prefix):]
    name = re.sub(r"[^0-9a-zA-Z]+", "_", name)
    return name.strip("_").lower()

def _find_best_table(layer_name: str, existing_tables: list, schema_prefix: str) -> str | None:
    """
    Return best-matching existing table using exact -> substring -> fuzzy logic.
    """
    norm_layer = _normalize_name(layer_name, schema_prefix=schema_prefix + "_")
    norm_map = { _normalize_name(t): t for t in existing_tables }

    # 1) exact normalized match
    if norm_layer in norm_map:
        return norm_map[norm_layer]

    # 2) substring match (either direction)
    for norm_tbl, orig_tbl in norm_map.items():
        if norm_layer in norm_tbl or norm_tbl in norm_layer:
            return orig_tbl

    # 3) fuzzy match (WRatio)
    choices = list(norm_map.keys())
    if choices:
        best_norm, score, _ = process.extractOne(norm_layer, choices, scorer=fuzz.WRatio)
        if score >= 85:
            return norm_map[best_norm]

    return None


def add_tooltip(widget, icon_path, title, subtitle="", canvas=None, bg_id=None):
    tooltip = tk.Toplevel(widget)
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

    pyautogui.FAILSAFE = False

    if not all([stored_username, stored_password]):
        messagebox.showerror("Error", "You must log in first before updating the database.")
        return

    try:
        # Step 1: Focus Global Mapper
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

        # Save path for exported GPKG
        save_path = r"C:\savetodb.gpkg"

        # Delete if already exists
        if os.path.exists(save_path):
            try:
                os.remove(save_path)
            except Exception as e:
                messagebox.showerror("File Error", f"Could not delete existing file:\n{e}")
                return

        # Step 2: Trigger Save via virtual right-click in left panel (without moving real mouse)
        pyautogui.hotkey("ctrl", "s")  # Save project first
        time.sleep(0.001)

        # Move mouse virtually (store position to restore later)
        real_mouse_pos = pyautogui.position()
        gm_window.activate()
        time.sleep(0.001)
        left_panel_x = gm_window.left + 25
        left_panel_y = gm_window.top + 500
        pyautogui.moveTo(left_panel_x, left_panel_y)
        pyautogui.rightClick()
        pyautogui.moveTo(real_mouse_pos)

        # Step 3: Keyboard sequence for export
        time.sleep(0.01)
        pyautogui.press("up", presses=1, interval=0.001)
        pyautogui.press("right", presses=1, interval=0.001)
        pyautogui.press("down", presses=1, interval=0.001)
        pyautogui.press("enter")
        time.sleep(0.01)
        pyautogui.press("enter")
        time.sleep(0.01)
        pyautogui.typewrite("a")
        pyautogui.typewrite("g" * 6)
        pyautogui.press("enter")

        # Step 4: Navigate Save dialog
        time.sleep(0.05)
        pyautogui.hotkey("alt", "d")
        pyautogui.typewrite("C:")
        pyautogui.press("enter")
        time.sleep(0.05)
        pyautogui.press("tab", presses=3, interval=0.05)
        pyautogui.hotkey("alt", "n")
        pyautogui.typewrite("savetodb")
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

        cursor.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s;",
            (DB_SCHEMA,)
        )
        existing_tables = [row[0] for row in cursor.fetchall()]

        layers = fiona.listlayers(save_path)
        schema_prefix = DB_SCHEMA  # for normalizer

        for layer in layers:
            # read layer
            gdf = gpd.read_file(save_path, layer=layer)
            gdf.columns = [col.lower() for col in gdf.columns]

            # decide which existing table to replace (or create new)
            match_table = _find_best_table(layer, existing_tables, schema_prefix=schema_prefix)

            if match_table:
                print(f"Replacing table via match: {match_table}  <- layer: {layer}")
                cursor.execute(f'DROP TABLE IF EXISTS "{DB_SCHEMA}"."{match_table}" CASCADE;')
                conn.connection.commit()
                gdf.to_postgis(name=match_table, con=engine, schema=DB_SCHEMA, if_exists="replace", index=False)
            else:
                new_table = _normalize_name(layer, schema_prefix=schema_prefix + "_")
                if not new_table:
                    new_table = "layer_" + str(abs(hash(layer)))
                print(f"Creating new table: {new_table}  <- layer: {layer}")
                gdf.to_postgis(name=new_table, con=engine, schema=DB_SCHEMA, if_exists="replace", index=False)

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
            if ".gmw" in w.title.lower():
                gm_window = w
                break
        if not gm_window:
            messagebox.showerror("Error", "Global Mapper window not found.")
            return
        gm_window.minimize(); time.sleep(0.1)
        gm_window.restore();  time.sleep(0.1)
        gm_window.activate(); time.sleep(0.1)

        # ---- Export to GPKG (C:\updatemap.gpkg) ----
        save_path = r"C:\updatemap.gpkg"
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
        pyautogui.typewrite("updatemap")
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
                # Move down 5 rows
                pyautogui.press("down", presses=5, interval=0.05)
                time.sleep(0.05)

                # Force focus back to typing field
                pyautogui.press("tab")
                time.sleep(0.05)

                # Type the next table name
                print(f"⌨ Typing next table: {tbl}")
                pyautogui.typewrite(tbl, interval=0.01)
                pyautogui.press("space")
                time.sleep(0.05)

            # After last table
            print("✅ Finished typing all tables. Pressing Enter...")
            pyautogui.press("enter")

        messagebox.showinfo("Update Map", f"Updated table into GM.")

        # === Extra step after clicking OK ===
        try:
            # Focus GM again
            gm_window.minimize()
            time.sleep(0.1)
            gm_window.restore()
            time.sleep(0.1)
            gm_window.activate()
            time.sleep(0.1)

            # Save current mouse position
            real_mouse_pos = pyautogui.position()

            # Move to target position in left panel
            left_panel_x = gm_window.left + 25
            left_panel_y = gm_window.top + 500
            pyautogui.moveTo(left_panel_x, left_panel_y)
            pyautogui.rightClick()

            # Restore mouse
            pyautogui.moveTo(real_mouse_pos)

            # Press arrow down 3 times and Enter
            pyautogui.press("down", presses=3, interval=0.001)
            pyautogui.press("enter")

        except Exception as e:
            print("⚠ Error performing post-update GM action:", e)

        # ---- Cleanup ----
        if os.path.exists(save_path):
            os.remove(save_path)

        # ---- Cleanup ----
        if os.path.exists(save_path):
            os.remove(save_path)

    except Exception as e:
        messagebox.showerror("Update Map Failed", str(e))



INFLUENCE_MAP_SCRIPT = r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\influence_to_barangay.py"
GM_EXE_PATH = r"C:\\Program Files\\GlobalMapper26.1_64bit\\global_mapper.exe"
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

def on_button_click(label):
    if label == "INFLUENCE MAP":
        popup_windows[label] += 1
        label_copy = label  # fix closure bug

        # Get canvas and background image ID immediately
        canvas, bg_id = canvas_refs[label_copy]

        proc = subprocess.Popen(['python', INFLUENCE_MAP_SCRIPT], shell=True)

        def poll_proc():
            if proc.poll() is not None:
                # Process has exited
                popup_windows[label_copy] -= 1
                if popup_windows[label_copy] <= 0:
                    popup_windows[label_copy] = 0
                    canvas.clicked = False
                    canvas.itemconfig(bg_id, image="")  # reset icon background
            else:
                root.after(500, poll_proc)  # keep checking

        poll_proc()

    elif label == "ROAD WIDTH":
        popup_windows[label] += 1
        subprocess.Popen(['python', 'road_width.py'], shell=True)
    elif label == "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO":
        popup_windows[label] += 1
        subprocess.Popen(['python', 'road_frontage.py'], shell=True)
    elif label == "LOT LOCATION":
        popup_windows[label] += 1
        subprocess.Popen(['python', 'lot_location.py'], shell=True)
    elif label == "LAND SHAPE":
        popup_windows[label] += 1
        subprocess.Popen(['python', 'land_shape_compactness.py'], shell=True)
    elif label == "METERS FROM (SCHOOL, SHOP, TRANSPORT)":
        popup_windows[label] += 1
        subprocess.Popen(['python', 'meters from closest (school, shop, transport) (for parcellary).py'], shell=True)
    elif label == "LANDMARKS WITHIN 200 METERS":
        popup_windows[label] += 1
        subprocess.Popen(['python', 'POI within 200 meters (for parcellary) (church,mall,police,park).py'], shell=True)
    elif label == "TERRAIN":
        popup_windows[label] += 1
        subprocess.Popen(['python', 'terrain.py'], shell=True)
    elif label == "ROAD DENSITY":
        popup_windows[label] += 1
        subprocess.Popen(['python', 'road_density.py'], shell=True)
    elif label == "ROAD SURFACE":
        popup_windows[label] += 1
        subprocess.Popen(['python', 'road_surface.py'], shell=True)
    elif label == "LINEAR REGRESSION":
        popup_windows[label] += 1
        subprocess.Popen(['python', 'linear_regression.py'], shell=True)
    elif label == "RANDOM FOREST":
        popup_windows[label] += 1
        subprocess.Popen(['python', 'random_forest.py'], shell=True)
    elif label == "XG BOOST":
        popup_windows[label] += 1
        subprocess.Popen(['python', 'XG_Boost.py'], shell=True)
    elif label == "ORDINARY LEAST SQUARES":
        popup_windows[label] += 1
        subprocess.Popen(['python', 'Ordinary_Least_Squares.py'], shell=True)
    elif label == "SPATIAL LAG MODEL":
        popup_windows[label] += 1
        subprocess.Popen(['python', 'Spatial_Lag_Model.py'], shell=True)
    elif label == "SPATIAL DURBIN MODEL":
        popup_windows[label] += 1
        subprocess.Popen(['python', 'Spatial_Durbin_Model.py'], shell=True)
    elif label == "GEOGRAPHICALLY WEIGHTED REGRESSION":
        popup_windows[label] += 1
        subprocess.Popen(['python', 'Geographically_Weighted_Regression.py'], shell=True)

import ctypes
import sys

def set_app_user_model_id():
    myappid = u'BLGF.CAMA.Tools.2025'
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)

set_app_user_model_id()




def show_login_and_connect():
    login_win = tk.Toplevel()
    login_win.title("Database Login")
    login_win.geometry("260x220")
    login_win.grab_set()
    login_win.resizable(False, False)

    tk.Label(login_win, text="Host:").grid(row=0, column=0, sticky="e", padx=5, pady=3)
    host_entry = tk.Entry(login_win, width=25)
    host_entry.grid(row=0, column=1)
    host_entry.insert(0, DB_HOST)
    host_entry.config(state="disabled")

    tk.Label(login_win, text="Port:").grid(row=1, column=0, sticky="e", padx=5, pady=3)
    port_entry = tk.Entry(login_win, width=25)
    port_entry.grid(row=1, column=1)
    port_entry.insert(0, DB_PORT)
    port_entry.config(state="disabled")

    tk.Label(login_win, text="Database:").grid(row=2, column=0, sticky="e", padx=5, pady=3)
    db_entry = tk.Entry(login_win, width=25)
    db_entry.grid(row=2, column=1)
    db_entry.insert(0, DB_NAME)
    db_entry.config(state="disabled")

    tk.Label(login_win, text="Schema:").grid(row=3, column=0, sticky="e", padx=5, pady=3)
    schema_entry = tk.Entry(login_win, width=25)
    schema_entry.grid(row=3, column=1)
    schema_entry.insert(0, DB_SCHEMA)
    schema_entry.config(state="disabled")

    tk.Label(login_win, text="Username:").grid(row=4, column=0, sticky="e", padx=5, pady=3)
    user_entry = tk.Entry(login_win, width=25)
    user_entry.grid(row=4, column=1)

    tk.Label(login_win, text="Password:").grid(row=5, column=0, sticky="e", padx=5, pady=3)
    pass_entry = tk.Entry(login_win, width=25, show="*")
    pass_entry.grid(row=5, column=1)

    def try_connect():
        username = user_entry.get()
        password = pass_entry.get()

        global stored_username, stored_password
        stored_username = username
        stored_password = password

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

# Force show in taskbar with icon
myappid = u'BLGF.CAMA.Tools.2025'
ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)

icon_path = "D:/2025_PROJECTS/BLGF-GM_TEST/FOR TESTING/DCS_CODES/BLGF.ico"
if os.path.exists(icon_path):
    root.iconbitmap(icon_path)

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
icon_path = "D:/2025_PROJECTS/BLGF-GM_TEST/FOR TESTING/DCS_CODES/BLGF.ico"
if os.path.exists(icon_path):
    root.iconbitmap(icon_path)

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

hover_bg = round_image(
    r"D:\2025_PROJECTS\BLGF-GM_TEST\FOR TESTING\DCS_CODES - testing\icons\hover.png",
    size=(38, 38),
    radius=7
)

clicked_bg = round_image(
    r"D:\2025_PROJECTS\BLGF-GM_TEST\FOR TESTING\DCS_CODES - testing\icons\click.png",
    size=(38, 38),
    radius=7
)

# === ICON PATHS INCLUDING INFLUENCE MAP ===
icon_paths = {
    "INFLUENCE MAP": r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\icons\\influencemap.png",  # <== NEW
    "ROAD WIDTH": r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\icons\\roadwidth.png",
    "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO": r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\icons\\roadfrontage.png",
    "LOT LOCATION": r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\icons\\lotlocation.png",
    "LAND SHAPE": r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\icons\\landshape.png",
    "METERS FROM (SCHOOL, SHOP, TRANSPORT)": r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\icons\\distancefrom.png",
    "LANDMARKS WITHIN 200 METERS": r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\icons\\landmarks200.png",
    "TERRAIN": r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\icons\\terrain.png",
    "ROAD DENSITY": r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\icons\\roaddensity.png",
    "ROAD SURFACE": r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\icons\\roadsurface.png",
    "LINEAR REGRESSION": r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\icons\\mlr.png",
    "RANDOM FOREST": r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\icons\\randomforest1.png",
    "XG BOOST": r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\icons\\xgboost.png",
    "ORDINARY LEAST SQUARES": r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\icons\\ols.png",
    "SPATIAL LAG MODEL": r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\icons\\slm.png",
    "SPATIAL DURBIN MODEL": r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\icons\\sdm.png",
    "GEOGRAPHICALLY WEIGHTED REGRESSION": r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\icons\\gwr1.png"
}

icons = {
    label: ImageTk.PhotoImage(Image.open(path).resize((39, 39), Image.Resampling.LANCZOS))
    for label, path in icon_paths.items()
}


# === TOOLBAR BUTTONS PANEL (blue area) ===
button_frame = tk.Frame(root, bg="#afd0f7", width=310, height=310) #310 height #310 width
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
    "INFLUENCE MAP": "Generate influence zones from landmarks",
    "ROAD WIDTH": "Measure average road width",
    "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO": "Analyze parcel depth and frontage",
    "LOT LOCATION": "Classify lots based on proximity",
    "LAND SHAPE": "Assess lot geometry and compactness",
    "METERS FROM (SCHOOL, SHOP, TRANSPORT)": "Measure distance to nearest POIs",
    "LANDMARKS WITHIN 200 METERS": "Check nearby landmark coverage",
    "TERRAIN": "Analyze slope and elevation difference",
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
    "INFLUENCE MAP",
    "ROAD WIDTH",
    "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO",
    "LOT LOCATION",
    "LAND SHAPE",
    "METERS FROM (SCHOOL, SHOP, TRANSPORT)",
]

buttons_2nd_row = [
    "LANDMARKS WITHIN 200 METERS",
    "TERRAIN",
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
    if label == "INFLUENCE MAP":
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
    if label == "METERS FROM (SCHOOL, SHOP, TRANSPORT)":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "LANDMARKS WITHIN 200 METERS":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
    elif label == "TERRAIN":
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
        gm_x, gm_y = gm_win.left, gm_win.top
        gm_width, gm_height = gm_win.width, gm_win.height

        # Position Tkinter window near bottom right of Global Mapper
        root.geometry(f"+{gm_x + gm_width - 310}+{gm_y + gm_height - 350}")

    root.deiconify()
    root.lift()
    root.attributes('-topmost', True)
    root.after(100, lambda: root.attributes('-topmost', False))

import pygetwindow as gw

def set_app_user_model_id():
    myappid = u'BLGF.CAMA.Tools.2025'
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)

set_app_user_model_id()

def launch_main_window():
    root.deiconify()  # Show the main UI window

root.withdraw()  # Hide the UI initially

import pygetwindow as gw
import time

def launch_global_mapper():
    subprocess.Popen([GM_EXE_PATH, selected_gmw_file], shell=True)
    wait_for_global_mapper()


def wait_for_global_mapper():
    gm_windows = [w for w in gw.getWindowsWithTitle('Global Mapper Pro') if w.visible]
    if gm_windows:
        print("✅ Global Mapper is open.")

        # === ✅ Create or replace C:\Global Mapper Temp ===
        temp_dir = r"C:\Global Mapper Temp"
        try:
            import shutil
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            os.makedirs(temp_dir)
            print('📁 Created folder "Global Mapper Temp"')
        except Exception as e:
            print("❌ Error creating folder:", e)
            messagebox.showerror("Folder Error", f"Failed to create folder:\n{e}")
            return

        launch_main_window()
        monitor_gm_state()
        monitor_gm_closure()
    else:
        print("⏳ Waiting for Global Mapper...")
        root.after(1000, wait_for_global_mapper)

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
                # Set topmost once, do not toggle every second
                if not getattr(root, "_already_topmost", False):
                    root.attributes("-topmost", True)
                    root._already_topmost = True

        else:
            print("❌ Global Mapper closed. Closing Tkinter.")
            root.destroy()

    except Exception as e:
        print("Error in GM monitor:", e)

    # Call again after some time
    root.after(1000, monitor_gm_state)

def monitor_gm_closure():
    gm_windows = [w for w in gw.getWindowsWithTitle('Global Mapper Pro') if w.isVisible]
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
        monitor_gm_state()  # <-- START MONITORING HERE
    else:
        print("⏳ Waiting for Global Mapper...")
        root.after(1000, wait_for_global_mapper)

# Step 1: Prompt for .gmw file first
gmw_file = filedialog.askopenfilename(
    title="Select Global Mapper Workspace File",
    filetypes=[("Global Mapper Workspace", "*.gmw")]
)

if not gmw_file:
    messagebox.showwarning("Cancelled", "No GMW file selected. Exiting.")
    root.destroy()
    exit()

selected_gmw_file = gmw_file
show_login_and_connect()

# Step 4: Enter main loop
root.mainloop()