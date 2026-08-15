import tkinter as tk
from tkinter import messagebox, filedialog
import subprocess
import os
import time
import pyautogui
import pytesseract
from PIL import Image

# 🔥 Update this path if your Tesseract is installed elsewhere
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Set Global Mapper executable path
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"

# Set MAIN.py path
MAIN_SCRIPT_PATH = r"D:\2025_PROJECTS\BLGF-GM_TEST\FOR TESTING\DCS_CODES - testing\MAIN.py"

def connect_to_db(dbname, user, password, host, port):
    try:
        import psycopg2
        conn = psycopg2.connect(
            dbname=dbname,
            user=user,
            password=password,
            host=host,
            port=port
        )
        conn.close()
        messagebox.showinfo("Connection Status", "✅ Connected to PostgreSQL database successfully!")
        select_gmw_file()
    except Exception as e:
        messagebox.showerror("Connection Failed", f"❌ Failed to connect:\n{str(e)}")

def select_gmw_file():
    gmw_file = filedialog.askopenfilename(
        title="Select Global Mapper Workspace",
        filetypes=[("Global Mapper Workspace", "*.gmw")]
    )
    if gmw_file:
        open_global_mapper(gmw_file)
    else:
        messagebox.showwarning("No File", "⚠ No workspace selected.")

def open_global_mapper(filepath):
    try:
        # Launch Global Mapper with selected workspace
        subprocess.Popen([GM_EXE_PATH, filepath], shell=False)
        wait_for_workspace_load(filepath)
    except Exception as e:
        messagebox.showerror("Launch Error", f"❌ Failed to open Global Mapper:\n{str(e)}")

def wait_for_workspace_load(filepath):
    answer = messagebox.askokcancel(
        "Workspace Load",
        "✅ Global Mapper opened.\n\nPlease wait until all layers are visible, then click OK to continue."
    )
    if answer:
        # After confirming, take screenshot and extract layers
        extract_layers_from_screen(filepath)

def extract_layers_from_screen(gmw_file):
    print("⏳ Taking screenshot in 5 seconds...")
    time.sleep(5)  # Let GM fully load
    screenshot = pyautogui.screenshot()

    # 🔥 Adjust these crop coordinates based on your screen layout!
    left = 50    # X coordinate
    top = 150    # Y coordinate
    right = 350  # X2
    bottom = 800 # Y2

    # Crop the layers control panel
    layer_panel = screenshot.crop((left, top, right, bottom))

    # Optional: save cropped panel for debugging
    layer_panel.save("layer_panel_debug.png")

    # OCR extract text
    text = pytesseract.image_to_string(layer_panel)

    # Clean text
    layer_names = [line.strip() for line in text.split("\n") if line.strip()]

    if layer_names:
        # Save to same folder as GMW file
        output_path = os.path.join(os.path.dirname(gmw_file), "loaded_layer_names.txt")
        with open(output_path, "w", encoding="utf-8") as f:
            for name in layer_names:
                f.write(name + "\n")

        messagebox.showinfo("Layers Extracted", f"✅ Extracted {len(layer_names)} layers.\nSaved to:\n{output_path}")
        run_main_py()
        root.destroy()
    else:
        messagebox.showwarning("No Layers Found", "⚠ No layers detected from screenshot.")

def run_main_py():
    try:
        subprocess.Popen(["python", MAIN_SCRIPT_PATH], shell=True)
    except Exception as e:
        messagebox.showerror("Run Error", f"❌ Failed to run MAIN.py:\n{str(e)}")

def login():
    dbname = dbname_entry.get()
    user = user_entry.get()
    password = password_entry.get()
    host = host_entry.get()
    port = port_entry.get()
    connect_to_db(dbname, user, password, host, port)

# --- UI SETUP ---
root = tk.Tk()
root.title("PostgreSQL Login")
root.geometry("275x275")

tk.Label(root, text="Database Name:").pack(pady=2)
dbname_entry = tk.Entry(root)
dbname_entry.pack()

tk.Label(root, text="Username:").pack(pady=2)
user_entry = tk.Entry(root)
user_entry.pack()

tk.Label(root, text="Password:").pack(pady=2)
password_entry = tk.Entry(root, show="*")
password_entry.pack()

tk.Label(root, text="Host (default: localhost):").pack(pady=2)
host_entry = tk.Entry(root)
host_entry.insert(0, "localhost")
host_entry.pack()

tk.Label(root, text="Port (default: 5432):").pack(pady=2)
port_entry = tk.Entry(root)
port_entry.insert(0, "5432")
port_entry.pack()

tk.Button(root, text="Login", command=login).pack(pady=10)

root.mainloop()
