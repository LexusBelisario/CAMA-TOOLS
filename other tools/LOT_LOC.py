import tkinter as tk
from tkinter import filedialog, messagebox
import geopandas as gpd
import os

def process_lot_location():
    root = tk.Tk()
    root.withdraw()

    # Step 1: Select shapefile
    shp_path = filedialog.askopenfilename(title="Select a shapefile", filetypes=[("Shapefiles", "*.shp")])
    if not shp_path:
        messagebox.showerror("Error", "No shapefile selected.")
        return

    # Step 2: Read the shapefile
    try:
        gdf = gpd.read_file(shp_path)
    except Exception as e:
        messagebox.showerror("Error", f"Failed to read shapefile:\n{e}")
        return

    # Step 3: Check LOT_LOCATI exists
    if "LOT_LOCATI" not in gdf.columns:
        messagebox.showerror("Error", "'LOT_LOCATI' field not found in shapefile.")
        return

    # Step 4: Convert to int and map values to new LOT_LOC field
    mapping = {
        0: "Inner Lot",
        1: "Road Lot",
        2: "Corner Lot"
    }

    try:
        gdf["LOT_LOCATI"] = gdf["LOT_LOCATI"].astype(float).astype(int)
        gdf["LOT_LOC"] = gdf["LOT_LOCATI"].map(mapping).fillna("Unknown")
    except Exception as e:
        messagebox.showerror("Error", f"Failed to process LOT_LOCATI values:\n{e}")
        return

    # Step 5: Select output directory
    output_dir = filedialog.askdirectory(title="Select output directory")
    if not output_dir:
        messagebox.showerror("Error", "No output directory selected.")
        return

    output_path = os.path.join(output_dir, os.path.basename(shp_path))

    # Step 6: Save the modified shapefile
    try:
        gdf.to_file(output_path)
        messagebox.showinfo("Success", f"✅ Shapefile saved with LOT_LOC:\n{output_path}")
    except Exception as e:
        messagebox.showerror("Error", f"Failed to save shapefile:\n{e}")

if __name__ == "__main__":
    process_lot_location()
