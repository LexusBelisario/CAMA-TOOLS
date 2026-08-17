import tkinter as tk
from tkinter import filedialog, messagebox
import geopandas as gpd
from shapely.geometry import box
import os
import subprocess

# Path to Global Mapper
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"

def compute_psi_rectangular(gdf):
    psi_values = []
    for geom in gdf.geometry:
        if geom.is_empty or geom.area == 0:
            psi_values.append(None)
            continue
        mbr = box(*geom.bounds)
        psi = geom.area / mbr.area
        psi_values.append(round(psi, 2))
    gdf["PSI_LOT"] = psi_values
    return gdf

def main():
    root = tk.Tk()
    root.withdraw()

    # Step 1: Select input shapefile
    shp_path = filedialog.askopenfilename(
        title="Select Barangay Shapefile",
        filetypes=[("Shapefiles", "*.shp")]
    )
    if not shp_path:
        messagebox.showerror("Error", "No shapefile selected.")
        return

    # Step 2: Select output shapefile path with "Save As" dialog
    out_path = filedialog.asksaveasfilename(
        defaultextension=".shp",
        filetypes=[("Shapefiles", "*.shp")],
        title="Save Output Shapefile"
    )
    if not out_path:
        messagebox.showerror("Error", "No output file specified.")
        return

    try:
        # Step 3: Load, compute PSI, and save
        gdf = gpd.read_file(shp_path)
        gdf = compute_psi_rectangular(gdf)
        gdf.to_file(out_path, driver="ESRI Shapefile")
        messagebox.showinfo("Success", f"Shapefile saved to:\n{out_path}")
    except Exception as e:
        messagebox.showerror("Processing Error", str(e))
        return

    # Step 4: Open result in Global Mapper
    if os.path.exists(GM_EXE_PATH):
        subprocess.Popen([GM_EXE_PATH, out_path], shell=True)
    else:
        messagebox.showwarning("Global Mapper", "Global Mapper not found or path is incorrect.")

if __name__ == "__main__":
    main()
