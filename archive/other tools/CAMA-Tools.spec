# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('icons', 'icons'), ('influence_to_barangay.py', '.'), ('road_width.py', '.'), ('road_frontage.py', '.'), ('lot_location.py', '.'), ('land_shape_compactness.py', '.'), ('meters from closest (school, shop, transport) (for parcellary).py', '.'), ('POI within 200 meters (for parcellary) (church,mall,police,park).py', '.'), ('terrain.py', '.'), ('road_density.py', '.'), ('road_surface.py', '.'), ('linear_regression.py', '.'), ('random_forest.py', '.'), ('XG_Boost.py', '.'), ('Ordinary_Least_Squares.py', '.'), ('Spatial_Lag_Model.py', '.'), ('Spatial_Durbin_Model.py', '.'), ('Geographically_Weighted_Regression.py', '.')]
binaries = []
hiddenimports = []
tmp_ret = collect_all('geopandas')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('fiona')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('shapely')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pyproj')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('rtree')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['MAIN3.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='CAMA-Tools',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
