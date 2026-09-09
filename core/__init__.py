"""
core package

PURPOSE:
    Namespace for application-level Windows/process coordination modules
    that are responsibility-driven extractions from MAIN.py, as opposed
    to generic utility helpers (see utils_paths.py, resource_path.py for
    that other category, which remain outside this package).

    Currently contains only window_management.py (single-instance
    enforcement, duplicate-launch coordination, Global Mapper session-
    window identification, foreground-PID/z-order coordination). Do not
    add further modules here speculatively -- new modules should only be
    created when a real, separately-justified responsibility boundary
    calls for one (see MAIN.py's own modularization notes).

PyInstaller note:
    This package is a plain top-level import from MAIN.py (via
    `from core.window_management import ...`), not a dispatched tool
    subprocess. PyInstaller's static import analysis of MAIN.py already
    bundles it -- no --add-data / --collect-all entry is needed in the
    build command or the .spec file for this package.
"""