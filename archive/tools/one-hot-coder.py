import os
import re
import tkinter as tk
from tkinter import filedialog, messagebox

import geopandas as gpd
import pandas as pd


# -------------------------------------------------
# NORMALIZE LANDSLIDE VALUES
# -------------------------------------------------
def normalize_landslide(val):
    if val is None:
        return None

    v = str(val).strip().lower()

    if "low" in v:
        return "LS_LOW"
    if "mod" in v:
        return "LS_MOD"
    if "high" in v:
        return "LS_HIGH"

    return "LS_OTH"


# -------------------------------------------------
# ONE-HOT ENCODE LANDSLIDE FIELD
# -------------------------------------------------
def one_hot_landslide(gdf: gpd.GeoDataFrame, field="Landslide") -> gpd.GeoDataFrame:
    if field not in gdf.columns:
        raise ValueError(f"Field '{field}' not found.")

    gdf_out = gdf.copy()

    # Normalize values
    mapped = gdf_out[field].apply(normalize_landslide)

    # Define fixed output columns (order matters)
    ohe_fields = ["LS_LOW", "LS_MOD", "LS_HIGH", "LS_OTH"]

    # Initialize columns with 0
    for col in ohe_fields:
        gdf_out[col] = 0

    # Assign 1 where applicable
    for col in ohe_fields:
        gdf_out.loc[mapped == col, col] = 1

    return gdf_out


# -------------------------------------------------
# GUI WORKFLOW
# -------------------------------------------------
def run():
    root = tk.Tk()
    root.withdraw()

    shp_path = filedialog.askopenfilename(
        title="Select input shapefile",
        filetypes=[("Shapefile", "*.shp")]
    )
    if not shp_path:
        return

    try:
        gdf = gpd.read_file(shp_path)
    except Exception as e:
        messagebox.showerror("Read Error", str(e))
        return

    try:
        gdf2 = one_hot_landslide(gdf, field="Landslide")
    except Exception as e:
        messagebox.showerror("Processing Error", str(e))
        return

    # Ask output format
    use_gpkg = messagebox.askyesno(
        "Output Format",
        "Save as GeoPackage (.gpkg)?\n\n"
        "Yes = GeoPackage (recommended)\n"
        "No  = Shapefile"
    )

    out_dir = os.path.dirname(shp_path)
    base = os.path.splitext(os.path.basename(shp_path))[0]

    if use_gpkg:
        out_path = os.path.join(out_dir, f"{base}_landslide_ohe.gpkg")
        try:
            gdf2.to_file(
                out_path,
                layer=f"{base}_landslide_ohe",
                driver="GPKG"
            )
        except Exception as e:
            messagebox.showerror("Write Error", str(e))
            return
    else:
        out_path = os.path.join(out_dir, f"{base}_landslide_ohe.shp")

        # Remove existing shapefile parts (important on Windows)
        base_out = os.path.splitext(out_path)[0]
        for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg"]:
            try:
                os.remove(base_out + ext)
            except FileNotFoundError:
                pass

        try:
            gdf2.to_file(
                out_path,
                driver="ESRI Shapefile",
                encoding="UTF-8"
            )
        except Exception as e:
            messagebox.showerror("Write Error", str(e))
            return

        # Validate
        missing = [
            ext for ext in [".shp", ".shx", ".dbf"]
            if not os.path.exists(base_out + ext)
        ]
        if missing:
            messagebox.showerror(
                "Write Error",
                f"Incomplete shapefile. Missing: {', '.join(missing)}"
            )
            return

    messagebox.showinfo(
        "Success",
        "✅ One-hot encoding completed!\n\n"
        "Added fields:\n"
        "- LS_LOW\n"
        "- LS_MOD\n"
        "- LS_HIGH\n"
        "- LS_OTH\n\n"
        f"Output:\n{out_path}"
    )


if __name__ == "__main__":
    run()
