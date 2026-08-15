import geopandas as gpd
import tkinter as tk
from tkinter import filedialog
import os

# === Step 1: List of fields to clean ===
fields_to_clean = [
    "5km_school", "5km_shop", "5km_tp", "poi_200m",
    "poi_5km", "num_church", "num_mall", "num_park", "num_police"
]

# === Step 2: Launch file picker ===
root = tk.Tk()
root.withdraw()
input_path = filedialog.askopenfilename(
    title="Select Shapefile",
    filetypes=[("Shapefiles", "*.shp")]
)

if not input_path:
    print("No file selected. Exiting.")
    exit()

# === Step 3: Load shapefile ===
gdf = gpd.read_file(input_path)

# === Step 4: Process fields ===
for field in fields_to_clean:
    if field in gdf.columns:
        gdf[field] = gdf[field].fillna(0).astype(int)
        print(f"✔ Cleaned field: {field}")
    else:
        print(f"⚠ Field not found: {field} (skipped)")

# === Step 5: Save output ===
output_path = os.path.join(os.path.dirname(input_path), "output_no_decimals.shp")
gdf.to_file(output_path)
print(f"\n✅ Cleaned shapefile saved to:\n{output_path}")
