import tkinter as tk
from tkinter import filedialog, messagebox
import geopandas as gpd
from shapely.geometry import Polygon
import math
import os
import subprocess

# 🔥 Set your paths here
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"
ICON_PATH = r"D:/2025_PROJECTS/BLGF-GM_TEST/FOR TESTING/DCS_CODES/BLGF.ico"

def angle_between(p1, p2, p3):
    v1 = (p1[0] - p2[0], p1[1] - p2[1])
    v2 = (p3[0] - p2[0], p3[1] - p2[1])

    dot = v1[0] * v2[0] + v1[1] * v2[1]
    det = v1[0] * v2[1] - v1[1] * v2[0]

    angle_rad = math.atan2(det, dot)
    angle_deg = math.degrees(angle_rad)
    return round(angle_deg + 360 if angle_deg < 0 else angle_deg, 2)

def vertex_angles(polygon):
    coords = list(polygon.exterior.coords)
    if coords[0] == coords[-1]:
        coords = coords[:-1]

    cleaned_coords = [coords[0]]
    for pt in coords[1:]:
        if math.dist(pt, cleaned_coords[-1]) != 0:
            cleaned_coords.append(pt)

    angles = []
    num_coords = len(cleaned_coords)

    for i in range(num_coords):
        p1 = cleaned_coords[i - 1]
        p2 = cleaned_coords[i]
        p3 = cleaned_coords[(i + 1) % num_coords]

        length = math.dist(p2, p3)
        if length == 0:
            continue

        angles.append(angle_between(p1, p2, p3))

    return angles

def classify_lot_shape(angles):
    low_angles = [a for a in angles if a <= 169]
    mid_angles = [a for a in angles if 170 <= a <= 190]
    high_angles = [a for a in angles if 190 < a < 265]
    lshape_angles = [a for a in angles if 260 <= a <= 280]

    if len(lshape_angles) == 1:
        return "L-Shaped"
    elif len(lshape_angles) > 1:
        return "Others"

    if len(angles) == 3 and len(low_angles) == 3:
        return "Triangle"
    if len(low_angles) == 3 and len(high_angles) == 0:
        return "Triangle"

    if len(angles) == 4 and len(low_angles) == 4:
        return "Quadrilateral"
    elif len(angles) > 4:
        if len(low_angles) == 4 and all(170 <= a <= 190 for a in mid_angles) and len(high_angles) == 0:
            return "Quadrilateral"
        elif len(low_angles) > 4:
            return "Others"

    return "Others"

def load_into_global_mapper(shapefile_path):
    try:
        if not os.path.exists(GM_EXE_PATH):
            messagebox.showerror("Error", f"Global Mapper not found at:\n{GM_EXE_PATH}")
            return

        cmd = f'"{GM_EXE_PATH}" "{shapefile_path}"'
        subprocess.Popen(cmd, shell=True)
        print(f"🗺️ Sent to Global Mapper: {shapefile_path}")

    except Exception as e:
        messagebox.showerror("Error", f"Failed to load into Global Mapper:\n{e}")

def process_multiple_shapefiles(root):
    brgy_paths = filedialog.askopenfilenames(
        title="Select Barangay Shapefiles (LAND SHAPE)",
        filetypes=[("Shapefiles", "*.shp")],
        parent=root
    )
    if not brgy_paths:
        return

    output_dir = filedialog.askdirectory(
        title="Select Output Directory (LAND SHAPE)",
        parent=root
    )
    if not output_dir:
        return

    try:
        for brgy_path in brgy_paths:
            barangay_gdf = gpd.read_file(brgy_path)

            vertex_counts = []
            vertex_angles_list = []
            lot_shapes = []

            for _, row in barangay_gdf.iterrows():
                geometry = row.geometry

                if isinstance(geometry, Polygon):
                    angles = vertex_angles(geometry)
                    vertex_counts.append(len(angles))
                    vertex_angles_list.append(",".join(map(str, angles)))
                    lot_shapes.append(classify_lot_shape(angles))
                else:
                    vertex_counts.append(0)
                    vertex_angles_list.append("")
                    lot_shapes.append("Others")

            barangay_gdf["VERTEX_COUNT"] = vertex_counts
            barangay_gdf["VERTEX_ANGLES"] = vertex_angles_list
            barangay_gdf["LOT_SHAPE"] = lot_shapes

            filename = os.path.basename(brgy_path).replace(".shp", "_lot_shape.shp")
            output_path = os.path.join(output_dir, filename)
            barangay_gdf.to_file(output_path)

            print(f"✅ Saved: {output_path}")

            # --- ✅ Auto-load each output shapefile into Global Mapper
            load_into_global_mapper(output_path)

        messagebox.showinfo("Success", f"✅ Processing complete.\nFiles saved to:\n{output_dir}", parent=root)

    except Exception as e:
        messagebox.showerror("Error", f"Processing failed:\n{e}", parent=root)

def launch_gui():
    root = tk.Tk()

    # 🔥 Set title and icon properly
    root.title("Land Shape Classifier")
    if os.path.exists(ICON_PATH):
        try:
            root.iconbitmap(ICON_PATH)
        except Exception as e:
            print(f"⚠ Failed to set custom icon: {e}")
    else:
        print(f"⚠ Icon file not found at: {ICON_PATH}")

    root.withdraw()  # Hide the main window AFTER setting icon and title

    process_multiple_shapefiles(root)

if __name__ == "__main__":
    launch_gui()
