"""
utils/resource_path.py

PURPOSE:
    Shared PyInstaller-safe resource-path helper, originally duplicated
    across all 11 registered CAMA Tools tool modules (POI_All_Distance.py,
    influence_to_barangay.py, influence_to_map.py, land_shape_compactness.py,
    lot_location.py,
    poi_within_200_meters_for_parcellary_church_mall_police_park.py,
    road_density.py, road_frontage.py, road_surface.py, road_width.py,
    terrain.py) and consolidated here. A line-by-line body diff across all
    11 source copies found zero executable-code differences -- only
    docstring wording, and a local `_sys` import-alias artifact in
    road_width.py (that file used `import sys as _sys` instead of a plain
    `import sys`), neither of which is a behavioral difference. This module
    owns its own `import sys`, so no tool file's local import naming
    matters anymore.

INPUTS:
    relative_path (str): path relative to the application's resource root.

OUTPUTS:
    str: absolute path to the resource, safe for both dev-mode execution
    and a PyInstaller-frozen executable.

DEPENDENCIES:
    os, sys (stdlib only). Relies on `sys._MEIPASS`, which PyInstaller
    injects at runtime only when running as a frozen executable.

    IMPORTANT -- do not confuse this module with utils_paths.py
    (root-level, used by MAIN.py / main_ui.py). That module has a
    DIFFERENT resource_path() implementation: a different dev-mode
    fallback base (script's own directory vs. this module's CWD-based
    fallback) and different return-value normalization (a fully resolved
    Path vs. this module's plain os.path.join). They currently produce the
    same base directory only because run.bat sets CWD to the project root,
    which is where utils_paths.py also lives -- that is incidental, not
    structural. Do NOT unify the two without an explicit,
    separately-approved design decision to standardize resource-path
    semantics across the entire application.
"""
import os
import sys


def resource_path(relative_path):
    """
    Resolves a relative path to an absolute path, safe for both normal
    (dev-mode) execution and a PyInstaller-frozen executable. When frozen,
    PyInstaller exposes the extracted bundle directory as sys._MEIPASS;
    otherwise falls back to the current working directory.
    """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)