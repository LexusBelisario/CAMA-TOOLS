# tools/utils_icon.py
import os, sys
from pathlib import Path
import tkinter as tk
import ctypes

def resource_path(relative: str) -> str:
    """
    Works both in dev and in PyInstaller .exe.
    """
    try:
        base = Path(sys._MEIPASS)
    except AttributeError:
        base = Path(__file__).resolve().parent.parent  # go up from /tools
    return str((base / relative).resolve())

def set_window_icon(window: tk.Tk, icon_filename: str):
    """
    Sets the icon for a Tkinter window (title bar + taskbar)
    Works for both .ico and .png.
    """
    # 🔹 Support both icons/ico/ and icons/
    icon_candidates = [
        resource_path(f"icons/ico/{icon_filename}"),
        resource_path(f"icons/{icon_filename}")
    ]

    icon_path = next((p for p in icon_candidates if os.path.exists(p)), None)

    # 🔹 Last resort fallback (absolute dev path)
    if not icon_path:
        fallback = r"D:\2025_PROJECTS\BLGF-GM_TEST\FOR PRODUCTION\DCS_CODES - testing\icons\ico\influencemap.ico"
        if os.path.exists(fallback):
            icon_path = fallback
        else:
            print(f"⚠️ Icon not found in: {icon_candidates + [fallback]}")
            return

    # 🔹 Force Windows taskbar to use this icon
    appid = u'BLGF.CAMA.Tools.SubTool'
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(appid)

    try:
        if icon_path.lower().endswith(".ico"):
            window.iconbitmap(icon_path)
        else:
            # PNG fallback
            photo = tk.PhotoImage(file=icon_path)
            window.tk.call('wm', 'iconphoto', window._w, photo)
        print(f"🟢 Icon applied successfully: {icon_path}")
    except Exception as e:
        print(f"⚠️ Failed to apply icon: {e}")
