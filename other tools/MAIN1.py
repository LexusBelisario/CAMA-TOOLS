
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox
import subprocess
import os
import json
import psycopg2
from PIL import Image, ImageTk, ImageDraw

confirm_win = None
confirm_clicked = {'ok': False}


DB_HOST = "localhost"
DB_PORT = "5432"
DB_NAME = "BLGF_DBTEST"
DB_SCHEMA = "CALAUAN_LAGUNA"

selected_gmw_file = None

stored_username = None
stored_password = None

root = tk.Tk()

# === Create or replace C:\Global Mapper Temp at script startup ===
import shutil

TEMP_DIR = r"C:\Global Mapper Temp"

try:
    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR)
    os.makedirs(TEMP_DIR)
    print('📁 Created folder "Global Mapper Temp" at startup.')
except Exception as e:
    print("❌ Failed to create folder at startup:", e)


root.geometry(f"300x300")
root.minsize(300, 300)

import geopandas as gpd
import fiona
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from rapidfuzz import fuzz
from tkinter import filedialog, messagebox

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


def extract_actual_name(layer_name):
    if "(" in layer_name:
        layer_name = layer_name.split("(")[0]
    if "." in layer_name:
        layer_name = layer_name.split(".")[1]
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

        gm_window.minimize()
        time.sleep(0.1)
        gm_window.restore()
        time.sleep(0.1)
        gm_window.activate()
        time.sleep(0.1)

        # Step 2: Prepare save path
        save_dir = r"C:\Global Mapper Temp"
        save_path = os.path.join(save_dir, "savetodb.gpkg")

        # Clean existing file if it exists
        if os.path.exists(save_path):
            os.remove(save_path)

        os.makedirs(save_dir, exist_ok=True)

        # Step 3: Trigger Save As GeoPackage
        pyautogui.hotkey("ctrl", "s")
        time.sleep(0.001)
        pyautogui.keyDown("alt")
        pyautogui.press("f")
        pyautogui.keyUp("alt")
        time.sleep(0.001)
        pyautogui.press("e")
        time.sleep(0.001)
        pyautogui.press("g")
        pyautogui.press("enter")  # Open 'Export to GeoPackage'
        time.sleep(0.3)

        # Step 4: Navigate Save Dialog
        pyautogui.hotkey("alt", "d")
        time.sleep(0.001)
        pyautogui.typewrite("C:\\Global")
        time.sleep(0.2)
        pyautogui.press("down")  # Select "Global Mapper Temp"
        pyautogui.press("enter")
        time.sleep(0.3)

        pyautogui.press("tab", presses=2, interval=0.001)  # Move to filename input
        pyautogui.hotkey("alt", "n")  # Or skip if Tab worked
        pyautogui.typewrite("savetodb")
        pyautogui.press("enter")

        # Step 5: Wait until the file is fully saved (not just exists)
        print("⏳ Waiting for file to be fully written...")
        max_wait = 10
        elapsed = 0
        last_size = -1

        while elapsed < max_wait:
            if os.path.exists(save_path):
                current_size = os.path.getsize(save_path)
                if current_size > 1000 and current_size == last_size:
                    break  # File is stable
                last_size = current_size
            time.sleep(0.5)
            elapsed += 0.5
        else:
            messagebox.showerror("GeoPackage Error", "GeoPackage was not created or still being written.")
            return

        print("✅ File saved:", save_path)

    except Exception as e:
        messagebox.showerror("Export Failed", f"Export failed:\n{e}")
        return

    try:
        # Step 6: Import into PostgreSQL
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

        cursor.execute(f"SELECT table_name FROM information_schema.tables WHERE table_schema = %s;", (DB_SCHEMA,))
        existing_tables = [row[0] for row in cursor.fetchall()]
        schema_prefix = DB_SCHEMA + "_"
        layers = fiona.listlayers(save_path)

        for layer in layers:
            gdf = gpd.read_file(save_path, layer=layer)
            gdf.columns = [col.lower() for col in gdf.columns]

            if layer.startswith(schema_prefix):
                stripped_name = layer[len(schema_prefix):]
            else:
                stripped_name = layer

            match_table = next((tbl for tbl in existing_tables if tbl.lower() == stripped_name.lower()), None)

            if match_table:
                print(f"✔️ Replacing table: {match_table}")
                cursor.execute(f'DROP TABLE IF EXISTS "{DB_SCHEMA}"."{match_table}" CASCADE;')
                conn.connection.commit()
                gdf.to_postgis(name=match_table, con=engine, schema=DB_SCHEMA, if_exists="replace", index=False)
            else:
                new_table = stripped_name.replace(" ", "_")
                print(f"➕ Creating new table: {new_table}")
                gdf.to_postgis(name=new_table, con=engine, schema=DB_SCHEMA, if_exists="replace", index=False)

        conn.close()
        engine.dispose()
        messagebox.showinfo("Success", "Database updated from GeoPackage.")

        # Step 7: Clean up saved file
        if os.path.exists(save_path):
            os.remove(save_path)

    except Exception as e:
        messagebox.showerror("Database Update Failed", f"Database load failed:\n{e}")


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
    elif label == "Geographically Weighted Regression":
        popup_windows[label] += 1
        subprocess.Popen(['python', 'GWR_20250714.py'], shell=True)

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
    "LINEAR REGRESSION": r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\icons\\linearregression.png",
    "RANDOM FOREST": r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\icons\\randomforest.png",
    "Geographically Weighted Regression": r"D:\\2025_PROJECTS\\BLGF-GM_TEST\\FOR TESTING\\DCS_CODES - testing\\icons\\gwr.png"
}

icons = {
    label: ImageTk.PhotoImage(Image.open(path).resize((39, 39), Image.Resampling.LANCZOS))
    for label, path in icon_paths.items()
}


# === TOOLBAR BUTTONS PANEL (blue area) ===
button_frame = tk.Frame(root, bg="#afd0f7", width=255, height=240)
button_frame.pack(padx=3, pady=(5, 0))
button_frame.pack_propagate(False)

# === TOP ROW AND BOTTOM ROW CONTAINERS ===
first_row = tk.Frame(button_frame, bg="#afd0f7")
first_row.pack(side="top", anchor="w", pady=(2, 0))

second_row = tk.Frame(button_frame, bg="#afd0f7")
second_row.pack(side="top", anchor="w", pady=(0, 4))

third_row = tk.Frame(button_frame, bg="#afd0f7")
third_row.pack(side="top", anchor="w", pady=(0, 4))

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
    "Geographically Weighted Regression": "Perform Geographically Weighted Regression"
}


# === BUTTON DEFINITIONS ===
buttons_1st_row = [
    "INFLUENCE MAP",
    "ROAD WIDTH",
    "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO",
    "LOT LOCATION",
    "LAND SHAPE"
]

buttons_2nd_row = [
    "METERS FROM (SCHOOL, SHOP, TRANSPORT)",
    "LANDMARKS WITHIN 200 METERS",
    "TERRAIN",
    "ROAD DENSITY",
    "ROAD SURFACE"
]

buttons_3rd_row = [
    "LINEAR REGRESSION",
    "RANDOM FOREST",
    "Geographically Weighted Regression"
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
    elif label == "Geographically Weighted Regression":
        canvas.pack(side="left", padx=(2, 2), pady=(2, 2))
        

# === UPDATE DB BUTTON ===
update_btn = tk.Button(
    root, text="Update Database",
    width=25,
    bg="#007acc",
    fg="white",
    activebackground="#005f99",
    activeforeground="white",
    relief="flat",
    command=update_database_from_geopackage
)
update_btn.pack(pady=15)

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