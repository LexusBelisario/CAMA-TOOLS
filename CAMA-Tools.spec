# =============================================================
# CAMA-Tools.spec
# Place this file in the SAME folder as MAIN3.py
# Run:  pyinstaller CAMA-Tools.spec
#
# Only packages actually used by MAIN3.py and its tools/ are
# listed here. Removed: statsmodels, libpysal, spreg,
# matplotlib, seaborn, reportlab (none are imported anywhere).
# =============================================================

from PyInstaller.utils.hooks import collect_all
import sys, os

# ── ONLY packages confirmed used across MAIN3.py + all tools ──
# MAIN3.py:       psycopg2, rapidfuzz, Pillow, geopandas, fiona,
#                 sqlalchemy, geoalchemy2, pygetwindow, pyautogui
# tools/:         shapely, pyproj, rasterio, scipy, networkx,
#                 numpy, pandas, osmnx, geopy, rtree
# optional:       oracledb (not confirmed but low cost to include)
packages = [
    "geopandas",
    "fiona",
    "shapely",
    "pyproj",
    "rtree",
    "rasterio",
    "scipy",
    "networkx",
    "numpy",
    "pandas",
    "osmnx",
    "geopy",
    "psycopg2",
    "sqlalchemy",
    "geoalchemy2",
    "rapidfuzz",
    "Pillow",
    "pygetwindow",
    "pyautogui",
    "oracledb",
]

datas    = []
binaries = []
hiddenimports = []

for pkg in packages:
    try:
        d, b, h = collect_all(pkg)
        datas    += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass   # skip gracefully if not installed

# ── project assets ────────────────────────────────────────────
datas += [
    ("BLGF.png",       "."),       # title-bar / taskbar icon
    ("BLGF.ico",       "."),       # window icon
    ("icons",          "icons"),   # toolbar icon PNGs/ICOs
    ("tools",          "tools"),   # all CAMA tool .py files
    ("utils_paths.py", "."),       # imported by MAIN3 as utils_paths
]

# pg_credentials.json is user-supplied at runtime — do NOT bundle.
# gm_exe_path.json is written at runtime — do NOT bundle.

# ── hidden imports PyInstaller commonly misses ────────────────
hiddenimports += [
    # pyproj / fiona / rasterio PROJ data
    "pyproj.datadir",
    "fiona._shim",
    "fiona.schema",
    "rasterio._shim",
    # shapely
    "shapely.speedups._speedups",
    # scipy
    "scipy._lib.messagestream",
    "scipy.spatial",
    "scipy.spatial.cKDTree",
    # psycopg2
    "psycopg2",
    "psycopg2.extensions",
    "psycopg2.extras",
    # sqlalchemy dialects
    "sqlalchemy.dialects.postgresql",
    "sqlalchemy.dialects.postgresql.psycopg2",
    # geoalchemy2
    "geoalchemy2",
    "geoalchemy2.types",
    # oracledb
    "oracledb",
    # PIL/tkinter bridge
    "PIL._tkinter_finder",
    # pyautogui / pygetwindow deps
    "pynput",
    "pynput.keyboard",
    "pynput.mouse",
    # tkinter (stdlib but sometimes needs explicit hint)
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.messagebox",
]

block_cipher = None

a = Analysis(
    ["MAIN3.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Confirmed NOT used — explicitly exclude to shrink exe
        "statsmodels",
        "libpysal",
        "spreg",
        "matplotlib",
        "seaborn",
        "reportlab",
        "IPython",
        "jupyter",
        "notebook",
        "pytest",
        "wx",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "sklearn",
        "tensorflow",
        "torch",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="CAMA-Tools",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,       # set False if UPX is not on PATH
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # no black console window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="BLGF.ico",
    onefile=True,
)
