# === main_gm_launcher.py ===
import os
import time
import shutil
import subprocess
import pygetwindow as gw
from tkinter import messagebox
from main_init import TEMP_DIR
import ctypes

GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"
selected_gmw_file = None

# === Set App ID so taskbar icon works correctly ===
def set_app_user_model_id():
    myappid = u'BLGF.CAMA.Tools.2025'
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)

set_app_user_model_id()


# === Launch the Global Mapper executable with the selected GMW file ===
def launch_global_mapper():
    global selected_gmw_file

    if not GM_EXE_PATH or not os.path.exists(GM_EXE_PATH):
        messagebox.showerror("Global Mapper", "global_mapper.exe not found.")
        return

    # 🟢 Ask the user for a GMW if not provided yet
    if not selected_gmw_file or not os.path.exists(selected_gmw_file):
        from tkinter import filedialog
        selected_gmw_file = filedialog.askopenfilename(
            title="Select Global Mapper Workspace File",
            filetypes=[("Global Mapper Workspace", "*.gmw")]
        )
        if not selected_gmw_file:
            messagebox.showerror("Global Mapper", "No .gmw file selected. Launch cancelled.")
            return

    try:
        subprocess.Popen([GM_EXE_PATH, selected_gmw_file], shell=True)
        wait_for_global_mapper()
    except Exception as e:
        messagebox.showerror("Global Mapper", f"Failed to launch:\n{e}")


# === Wait for Global Mapper to appear and monitor its state ===
def wait_for_global_mapper():
    gm_windows = [w for w in gw.getWindowsWithTitle('Global Mapper Pro') if w.visible]
    if gm_windows:
        print("✅ Global Mapper is open.")
        try:
            if os.path.exists(TEMP_DIR):
                shutil.rmtree(TEMP_DIR)
            os.makedirs(TEMP_DIR, exist_ok=True)
            print(f"📁 Recreated temp folder: {TEMP_DIR}")
        except Exception as e:
            print("❌ Error creating folder:", e)
            messagebox.showerror("Folder Error", f"Failed to create folder:\n{e}")
            return

        launch_main_window()
        monitor_gm_state()
        monitor_gm_closure()
    else:
        print("⏳ Waiting for Global Mapper...")
        from tkinter import Tk
        root = Tk()
        root.withdraw()
        root.after(1000, wait_for_global_mapper)


# === Keep checking if GM minimized / closed ===
from tkinter import Tk
root = Tk()
root.withdraw()
prev_position = [None, None]


def launch_main_window():
    gm_windows = [w for w in gw.getWindowsWithTitle('Global Mapper Pro') if w.visible]
    if gm_windows:
        gm_win = gm_windows[0]
        gm_x, gm_y = gm_win.left, gm_win.top
        gm_width, gm_height = gm_win.width, gm_win.height
        root.geometry(f"+{gm_x + gm_width - 310}+{gm_y + gm_height - 350}")
    root.deiconify()
    root.lift()
    root.attributes('-topmost', True)
    root.after(100, lambda: root.attributes('-topmost', False))


def monitor_gm_state():
    try:
        gm_windows = [w for w in gw.getWindowsWithTitle('Global Mapper Pro') if w.visible]
        if gm_windows:
            gm_win = gm_windows[0]
            if gm_win.isMinimized:
                if root.state() != 'withdrawn':
                    root.withdraw()
            else:
                if root.state() == 'withdrawn':
                    root.deiconify()
                root.lift()
                if not getattr(root, "_already_topmost", False):
                    root.attributes("-topmost", True)
                    root._already_topmost = True
        else:
            print("❌ Global Mapper closed. Closing Tkinter.")
            root.destroy()
    except Exception as e:
        print("Error in GM monitor:", e)
    root.after(1000, monitor_gm_state)


def monitor_gm_closure():
    gm_windows = [w for w in gw.getWindowsWithTitle('Global Mapper Pro') if w.visible]
    if not gm_windows:
        print("❌ Global Mapper closed. Exiting tools.")
        root.destroy()
    else:
        root.after(2000, monitor_gm_closure)
