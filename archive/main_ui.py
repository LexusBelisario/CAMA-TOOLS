# === main_ui.py ===
import os
import tkinter as tk
from tkinter import messagebox
from PIL import Image, ImageTk, ImageDraw
from pathlib import Path
from main_utils import add_tooltip
from utils_paths import resource_path
from main_init import TOOL_MODULES
import subprocess


def build_main_ui(root, update_map_func, update_db_func):
    ICONS_DIR = Path(resource_path("icons"))
    root.title("CAMA Tools")
    root.geometry("355x410")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    # === Load background/hover images ===
    hover_bg = round_image(str(ICONS_DIR / "hover.png"), size=(38, 38), radius=7, master=root)
    clicked_bg = round_image(str(ICONS_DIR / "click.png"), size=(38, 38), radius=7, master=root)

    _icon_files = {
        "INFLUENCE MAP": "influencemap.png",
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

    # === Persistent image storage ===
    root.icon_images = {}

    for lbl, file in _icon_files.items():
        path = ICONS_DIR / file
        if not path.exists():
            messagebox.showwarning("Missing Icon", f"Icon not found: {path}")
            continue
        # 🟢 Tie PhotoImage to the same root
        root.icon_images[lbl] = ImageTk.PhotoImage(
            Image.open(path).resize((39, 39), Image.Resampling.LANCZOS),
            master=root
        )

    tooltip_desc = {
        "INFLUENCE MAP": "Generate influence zones from landmarks",
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
        "GEOGRAPHICALLY WEIGHTED REGRESSION": "Perform Geographically Weighted Regression",
    }

    # === Layout ===
    button_frame = tk.Frame(root, bg="#afd0f7", width=310, height=160)
    button_frame.pack(padx=3, pady=(5, 0))
    button_frame.pack_propagate(False)

    add_group_label(button_frame, "Feature Management Tools")
    first_row = create_row(button_frame)
    second_row = create_row(button_frame)
    add_group_label(button_frame, "AI Model Tools")
    third_row = create_row(button_frame)
    add_group_label(button_frame, "Geostatistical Model Tools")
    fourth_row = create_row(button_frame)

    # === Button groups ===
    rows = {
        first_row: ["INFLUENCE MAP", "ROAD WIDTH", "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO", "LOT LOCATION", "LAND SHAPE", "METERS FROM (SCHOOL, SHOP, TRANSPORT, CHURCH)"],
        second_row: ["LANDMARKS WITHIN METERS", "PARCEL TERRAIN LEVEL", "ROAD DENSITY", "ROAD SURFACE"],
        third_row: ["LINEAR REGRESSION", "RANDOM FOREST", "XG BOOST"],
        fourth_row: ["ORDINARY LEAST SQUARES", "SPATIAL LAG MODEL", "SPATIAL DURBIN MODEL", "GEOGRAPHICALLY WEIGHTED REGRESSION"]
    }

    for frame, labels in rows.items():
        for lbl in labels:
            canvas = tk.Canvas(frame, width=48, height=48, highlightthickness=0, bg="#afd0f7")
            bg_img_id = canvas.create_image(3, 3, anchor="nw", image=None)
            # ✅ Create the icon image (safe because master=root)
            icon_img = root.icon_images.get(lbl)
            if icon_img:
                canvas.create_image(2, 2, anchor="nw", image=icon_img)

            def on_click(event, label=lbl): run_tool_by_label(label)
            canvas.bind("<Button-1>", on_click)
            add_tooltip(canvas, str(ICONS_DIR / _icon_files[lbl]), lbl, tooltip_desc.get(lbl, ""), canvas=canvas, bg_id=bg_img_id)
            canvas.pack(side="left", padx=(2, 2), pady=(2, 2))

    # === Bottom Buttons ===
    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=15)
    tk.Button(
        btn_frame, text="Update Map", width=20, bg="#6a9f2f", fg="white",
        activebackground="#4c7a20", command=update_map_func
    ).pack(side="left", padx=5)
    tk.Button(
        btn_frame, text="Update Database", width=20, bg="#007acc", fg="white",
        activebackground="#005f99", command=update_db_func
    ).pack(side="left", padx=5)

    return button_frame


# === Helper Functions ===
def round_image(img_path, size=(38, 38), radius=7, master=None):
    img = Image.open(img_path).convert("RGBA")
    if img.width != img.height:
        max_dim = max(img.width, img.height)
        square_img = Image.new("RGBA", (max_dim, max_dim), (0, 0, 0, 0))
        square_img.paste(img, ((max_dim - img.width)//2, (max_dim - img.height)//2))
        img = square_img
    img = img.resize(size, Image.Resampling.LANCZOS)
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0], size[1]), radius=radius, fill=255)
    img.putalpha(mask)
    # 🟢 Master attached to root prevents garbage collection issues
    return ImageTk.PhotoImage(img, master=master)


def add_group_label(parent, text):
    tk.Label(parent, text=text, font=("Segoe UI", 9, "bold"), bg="#afd0f7", anchor="w").pack(
        side="top", anchor="w", padx=5, pady=(3, 1)
    )


def create_row(parent):
    row = tk.Frame(parent, bg="#afd0f7")
    row.pack(side="top", anchor="w", pady=(0, 4))
    return row


def run_tool_by_label(label):
    exe_path = subprocess.list2cmdline([subprocess.sys.executable])
    subprocess.Popen([exe_path, "--tool", label], shell=False)
