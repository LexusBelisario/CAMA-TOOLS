import tkinter as tk
from tkinter import messagebox
import pyautogui
import pygetwindow as gw
import time

# === SET YOUR SCHEMA PREFIX HERE ===
SCHEMA = "CALAUAN_LAGUNA"

pyautogui.FAILSAFE = False

def focus_global_mapper():
    for w in gw.getWindowsWithTitle("Global Mapper"):
        if ".gmw" in w.title.lower():
            w.restore()
            w.activate()
            return True
    return False

def search_and_delete_layer():
    base_name = entry.get().strip()
    if not base_name:
        messagebox.showwarning("Input Required", "Please enter a layer name.")
        return

    full_layer_name = f"{SCHEMA}.{base_name}"

    if not focus_global_mapper():
        messagebox.showerror("Error", "Global Mapper workspace not found.")
        return

    time.sleep(1)

    # Open Control Center
    pyautogui.hotkey("ctrl", "shift", "c")
    time.sleep(1)

    # Type the full layer name (e.g., CALAUAN_LAGUNA.01_Kanluran)
    pyautogui.typewrite(full_layer_name)
    time.sleep(0.5)

    # Press Delete and confirm
    pyautogui.press("delete")
    time.sleep(0.3)
    pyautogui.press("enter")

    messagebox.showinfo("Success", f"Layer '{full_layer_name}' deleted.")

# === Tkinter UI ===
root = tk.Tk()
root.title("Search & Delete Layer in Global Mapper")

tk.Label(root, text="Enter Table Name (without schema):").pack(padx=10, pady=(10, 5))
entry = tk.Entry(root, width=40)
entry.pack(padx=10, pady=5)

btn = tk.Button(root, text="Search and Delete", command=search_and_delete_layer, width=30)
btn.pack(padx=10, pady=15)

root.mainloop()
