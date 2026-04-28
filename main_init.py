# === main_init.py ===
import os
import sys
import argparse
import importlib
from tkinter import Tk, messagebox

# === Create C:\Global Mapper Temp if not existing ===
TEMP_DIR = r"C:\Global Mapper Temp"
try:
    os.makedirs(TEMP_DIR, exist_ok=True)
except Exception as e:
    r = Tk(); r.withdraw()
    messagebox.showerror("Folder Error", f"Could not create {TEMP_DIR}:\n{e}")
    sys.exit(1)

# === Tool registry ===
TOOL_MODULES = {
    "INFLUENCE MAP": "tools.influence_to_barangay",
    "ROAD WIDTH": "tools.road_width",
    "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO": "tools.road_frontage",
    "LOT LOCATION": "tools.lot_location",
    "LAND SHAPE": "tools.land_shape_compactness",
    "METERS FROM (SCHOOL, SHOP, TRANSPORT, CHURCH)": "tools.meters_from_closest_school_shop_transport_for_parcellary",
    "LANDMARKS WITHIN METERS": "tools.poi_within_200_meters_for_parcellary_church_mall_police_park",
    "PARCEL TERRAIN LEVEL": "tools.terrain",
    "ROAD DENSITY": "tools.road_density",
    "ROAD SURFACE": "tools.road_surface",
    "LINEAR REGRESSION": "tools.linear_regression",
    "RANDOM FOREST": "tools.random_forest",
    "XG BOOST": "tools.XG_Boost",
    "ORDINARY LEAST SQUARES": "tools.Ordinary_Least_Squares",
    "SPATIAL LAG MODEL": "tools.Spatial_Lag_Model",
    "SPATIAL DURBIN MODEL": "tools.Spatial_Durbin_Model",
    "GEOGRAPHICALLY WEIGHTED REGRESSION": "tools.Geographically_Weighted_Regression",
}


def dispatch_tool_if_requested():
    """Runs individual tool modules if --tool argument is passed (used by subprocess)."""
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--tool", default=None)
    args, _ = ap.parse_known_args()

    if not args.tool:
        return  # normal startup

    mod_path = TOOL_MODULES.get(args.tool)
    if not mod_path:
        r = Tk(); r.withdraw()
        messagebox.showerror("Tool Error", f"Unknown tool: {args.tool}")
        sys.exit(2)

    try:
        mod = importlib.import_module(mod_path)
        rc = 0
        if hasattr(mod, "main") and callable(mod.main):
            rc = int(mod.main() or 0)
        sys.exit(rc)
    except Exception:
        import traceback
        r = Tk(); r.withdraw()
        messagebox.showerror("Tool Crash", f"{mod_path}\n\n{traceback.format_exc()}")
        sys.exit(1)
