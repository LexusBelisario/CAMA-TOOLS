import subprocess
import pygetwindow as gw
import pyautogui
import keyboard
import time
import os
import sys

# === Setup working directory ===
script_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(script_dir)

# === Step 1: Launch MAIN.py ===
main_py_path = os.path.join(script_dir, "MAIN.py")
print("🚀 Launching MAIN.py...")
main_proc = subprocess.Popen([sys.executable, main_py_path], shell=True)

# === Step 2: Wait for Global Mapper window ===
print("🕒 Waiting for Global Mapper window to open...")

def find_gm_window():
    for w in gw.getWindowsWithTitle("Global Mapper"):
        if w.title.lower().endswith(".gmw") or ".gmw" in w.title.lower():
            return w
    return None

gm_window = None
while gm_window is None:
    gm_window = find_gm_window()
    time.sleep(1)

# === Step 3: Bring Global Mapper to front safely ===
if gm_window:
    try:
        gm_window.minimize()
        gm_window.restore()
        gm_window.maximize()
        time.sleep(0.5)
        pyautogui.click(gm_window.left + 10, gm_window.top + 10)
        print("✅ Global Mapper is focused and ready.")
    except Exception as e:
        print("⚠️ Failed to focus GM window:", e)

# === Step 4: Monitor Ctrl+S for Export ===
print("⌨️ Monitoring for Ctrl+S inside Global Mapper... (Press Esc to quit)")

try:
    while True:
        gm_window = find_gm_window()
        if not gm_window:
            print("❌ Global Mapper closed. Exiting.")
            break

        if keyboard.is_pressed("ctrl+s"):
            print("📥 Ctrl+S detected. Triggering export shortcut...")
            try:
                gm_window.minimize()
                gm_window.restore()
                gm_window.maximize()
                time.sleep(0.01)
                pyautogui.click(gm_window.left + 10, gm_window.top + 10)
            except:
                pass

            time.sleep(0.1)

            # Alt+F → E → G → Enter
            pyautogui.keyDown("alt")
            pyautogui.press("f")
            pyautogui.keyUp("alt")
            time.sleep(0.001)
            pyautogui.press("e")
            time.sleep(0.001)
            pyautogui.press("g")
            time.sleep(0.001)
            pyautogui.press("enter")

            print("✅ Export triggered successfully.")

            while keyboard.is_pressed("ctrl+s"):
                time.sleep(0.3)

        if keyboard.is_pressed("esc"):
            print("🛑 ESC pressed. Exiting.")
            break

        time.sleep(0.1)

except KeyboardInterrupt:
    print("🛑 Interrupted by user. Exiting.")
