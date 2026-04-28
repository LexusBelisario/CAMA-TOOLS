import tkinter as tk
from tkinter import filedialog, messagebox
import geopandas as gpd
import numpy as np
import os
import subprocess

GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"

def compute_shape_metrics(gdf):
    fd_list = []
    si_list = []
    ff_list = []

    for geom in gdf.geometry:
        if geom.is_empty or geom.area <= 0 or geom.length <= 0:
            fd_list.append(None)
            si_list.append(None)
            ff_list.append(None)
            continue

        area = geom.area
        perimeter = geom.length
        minx, miny, maxx, maxy = geom.bounds
        width = maxx - minx
        height = maxy - miny

        # Fractal Dimension
        fd = 2 * (np.log(perimeter) / np.log(area))
        fd_list.append(round(fd, 2))

        # Shape Index (relative to circle)
        si = perimeter / (2 * np.sqrt(np.pi * area))
        si_list.append(round(si, 2))

        # Form Factor (normalized to 1 max)
        if width > 0 and height > 0:
            ff = area / (width * height)
            ff_list.append(round(ff, 2))
        else:
            ff_list.append(None)

    gdf["FD_LOT"] = fd_list
    gdf["SI_LOT"] = si_list
    gdf["FF_LOT"] = ff_list
    return gdf

def main():
    root = tk.Tk()
    root.withdraw()

    shp_path = filedialog.askopenfilename(
        title="Select Parcel Shapefile",
        filetypes=[("Shapefiles", "*.shp")]
    )
    if not shp_path:
        messagebox.showerror("Error", "No shapefile selected.")
        return

    out_path = filedialog.asksaveasfilename(
        defaultextension=".shp",
        filetypes=[("Shapefiles", "*.shp")],
        title="Save Output Shapefile"
    )
    if not out_path:
        messagebox.showerror("Error", "No output file specified.")
        return

    try:
        gdf = gpd.read_file(shp_path)
        gdf = compute_shape_metrics(gdf)
        gdf.to_file(out_path, driver="ESRI Shapefile")
        messagebox.showinfo("Success", f"Shapefile saved to:\n{out_path}")
    except Exception as e:
        messagebox.showerror("Processing Error", str(e))
        return

    if os.path.exists(GM_EXE_PATH):
        subprocess.Popen([GM_EXE_PATH, out_path], shell=True)
    else:
        messagebox.showwarning("Global Mapper", "Global Mapper not found or path is incorrect.")

if __name__ == "__main__":
    main()
