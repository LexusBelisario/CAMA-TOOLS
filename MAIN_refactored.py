# === MAIN3.py ===
import sys
import tkinter as tk
from tkinter import filedialog, messagebox

# === Local imports ===
from main_init import dispatch_tool_if_requested
from main_db_ops import (
    show_login_and_connect,
    update_database_from_geopackage,
    update_map_and_select_recorded
)
from main_gm_launcher import launch_global_mapper
from main_ui import build_main_ui

# === Step 1: Check if running with --tool (for subprocess launching) ===
dispatch_tool_if_requested()

# === Step 2: Create Tkinter root window ===
root = tk.Tk()
root.withdraw()

# === Step 3: Build the main CAMA UI ===
build_main_ui(root, update_map_and_select_recorded, update_database_from_geopackage)

# === Step 4: Ask user for Global Mapper Workspace file ===
gmw_file = filedialog.askopenfilename(
    title="Select Global Mapper Workspace File",
    filetypes=[("Global Mapper Workspace", "*.gmw")]
)
if not gmw_file:
    messagebox.showwarning("Cancelled", "No GMW file selected. Exiting.")
    root.destroy()
    sys.exit(0)

# === Step 5: Show Login Window and Connect ===
show_login_and_connect(root, launch_global_mapper, gmw_file)

# === Step 6: Start event loop ===
root.mainloop()
