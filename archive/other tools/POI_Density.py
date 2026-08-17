import geopandas as gpd
from shapely.geometry import Polygon
import tkinter as tk
from tkinter import filedialog, messagebox
import os

def compute_poi_densities(barangay_path, poi_path, output_path):
    # Load shapefiles
    brgy_gdf = gpd.read_file(barangay_path)
    poi_gdf = gpd.read_file(poi_path)

    # Reproject to metric CRS if needed
    if brgy_gdf.crs.is_geographic:
        brgy_gdf = brgy_gdf.to_crs(epsg=32651)
    poi_gdf = poi_gdf.to_crs(brgy_gdf.crs)

    # Initialize new fields
    den_school_list = []
    den_shop_list = []
    den_tp_list = []

    for idx, row in brgy_gdf.iterrows():
        geom = row.geometry
        buffer = geom.buffer(1000)  # 1 km buffer
        buffer_area_km2 = buffer.area / 1_000_000  # convert m² to km²

        pois_in_buffer = poi_gdf[poi_gdf.geometry.within(buffer)]
        type_counts = pois_in_buffer["type"].value_counts()

        den_school = type_counts.get("school", 0) / buffer_area_km2
        den_shop = type_counts.get("shop", 0) / buffer_area_km2
        den_tp = type_counts.get("transport", 0) / buffer_area_km2

        den_school_list.append(round(den_school, 2))
        den_shop_list.append(round(den_shop, 2))
        den_tp_list.append(round(den_tp, 2))

    brgy_gdf["DEN_SCHOOL"] = den_school_list
    brgy_gdf["DEN_SHOP"] = den_shop_list
    brgy_gdf["DEN_TP"] = den_tp_list

    # Save to output
    brgy_gdf.to_file(output_path)

def run_gui():
    root = tk.Tk()
    root.withdraw()

    # Select barangay shapefile
    barangay_path = filedialog.askopenfilename(
        title="Select Barangay Shapefile",
        filetypes=[("Shapefiles", "*.shp")]
    )
    if not barangay_path:
        return

    # Select POI shapefile
    poi_path = filedialog.askopenfilename(
        title="Select POI Shapefile",
        filetypes=[("Shapefiles", "*.shp")]
    )
    if not poi_path:
        return

    # Save As dialog for output shapefile
    output_path = filedialog.asksaveasfilename(
        title="Save Output Shapefile",
        defaultextension=".shp",
        filetypes=[("Shapefiles", "*.shp")]
    )
    if not output_path:
        return

    try:
        compute_poi_densities(barangay_path, poi_path, output_path)
        messagebox.showinfo("Success", f"Output saved to:\n{output_path}")
    except Exception as e:
        messagebox.showerror("Error", str(e))

if __name__ == "__main__":
    run_gui()
