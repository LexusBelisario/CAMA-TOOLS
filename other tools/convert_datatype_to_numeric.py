import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import geopandas as gpd
import pandas as pd
import os

def convert_field_to_numeric():
    shp_path = filedialog.askopenfilename(
        title="Select Shapefile",
        filetypes=[("Shapefiles", "*.shp")]
    )
    if not shp_path:
        return

    gdf = gpd.read_file(shp_path)

    # Create selection window
    sel_win = tk.Toplevel(root)
    sel_win.title("Select Field to Convert to Numeric")

    tk.Label(sel_win, text="Choose a field:").pack(pady=5)
    field_var = tk.StringVar()
    field_cb = ttk.Combobox(sel_win, textvariable=field_var, values=list(gdf.columns), state="readonly", width=40)
    field_cb.pack(pady=5)
    field_cb.current(0)

    def do_conversion():
        field_name = field_var.get()
        if not field_name:
            messagebox.showerror("Error", "No field selected.")
            return

        try:
            gdf[field_name] = pd.to_numeric(gdf[field_name], errors="coerce")
            out_path = os.path.splitext(shp_path)[0] + "_numeric.shp"
            gdf.to_file(out_path)
            messagebox.showinfo("Success", f"Field '{field_name}' converted to numeric.\nSaved as:\n{out_path}")
            sel_win.destroy()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to convert field:\n{e}")

    tk.Button(sel_win, text="Convert", command=do_conversion).pack(pady=10)

# Main GUI
root = tk.Tk()
root.withdraw()
root.after(100, convert_field_to_numeric)
root.mainloop()
