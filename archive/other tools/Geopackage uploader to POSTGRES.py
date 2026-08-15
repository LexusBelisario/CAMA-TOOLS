import geopandas as gpd
import fiona
import tkinter as tk
from tkinter import filedialog, simpledialog, messagebox
from sqlalchemy import create_engine

# === Step 1: GUI Setup ===
root = tk.Tk()
root.withdraw()  # Hide the main Tkinter window

# === Step 2: Select GeoPackage ===
gpkg_path = filedialog.askopenfilename(
    title="Select GeoPackage",
    filetypes=[("GeoPackage files", "*.gpkg")]
)

if not gpkg_path:
    messagebox.showerror("No File", "You did not select a GeoPackage file.")
    exit()

# === Step 3: List layers and prompt for selection ===
try:
    layers = fiona.listlayers(gpkg_path)
except Exception as e:
    messagebox.showerror("Error", f"Failed to read GeoPackage:\n{e}")
    exit()

layer_name = simpledialog.askstring("Select Layer", f"Available layers:\n{', '.join(layers)}\n\nEnter layer name to upload:")

if not layer_name or layer_name not in layers:
    messagebox.showerror("Invalid Layer", "Layer not found or no input provided.")
    exit()

# === Step 4: Ask for DB credentials ===
db_user = simpledialog.askstring("DB User", "Enter PostgreSQL username:", initialvalue="postgres")
db_pass = simpledialog.askstring("DB Password", "Enter PostgreSQL password:", show="*")
db_host = simpledialog.askstring("DB Host", "Enter host:", initialvalue="localhost")
db_port = simpledialog.askstring("DB Port", "Enter port:", initialvalue="5432")
db_name = simpledialog.askstring("DB Name", "Enter database name:")

# === Validate DB input ===
inputs = [db_user, db_pass, db_host, db_port, db_name]
if not all(inputs):
    messagebox.showerror("Missing Input", "One or more required database fields were left blank.")
    exit()

# === Step 5: Ask for destination table name ===
table_name = simpledialog.askstring("Table Name", f"Enter table name for PostgreSQL:", initialvalue=layer_name)
if not table_name:
    messagebox.showerror("Missing Table Name", "Table name is required.")
    exit()

# === Step 6: Read layer from GeoPackage ===
try:
    gdf = gpd.read_file(gpkg_path, layer=layer_name)
except Exception as e:
    messagebox.showerror("Error Reading Layer", f"Could not read layer:\n{e}")
    exit()

# === Step 7: Build connection and upload ===
conn_str = f"postgresql://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"

try:
    engine = create_engine(conn_str)
    gdf.to_postgis(
        name=table_name,
        con=engine,
        if_exists="replace",
        index=False
    )
    messagebox.showinfo("Success", f"✅ Uploaded '{layer_name}' to PostgreSQL as '{table_name}'")
except Exception as e:
    messagebox.showerror("Upload Failed", f"Failed to upload to PostgreSQL:\n{e}")
    exit()
