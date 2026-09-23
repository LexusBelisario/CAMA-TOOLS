"""
utils/last_directory.py

PURPOSE:
    Generic, category-keyed "remember the last-used file path the user
    picked" storage. NOT specific to any one picker -- this module is
    deliberately generic from its first use, a documented exception to
    this codebase's own Rule of Three, because the SAME "remember last
    path, keyed by category" need is already known, concretely, to
    apply to several other file/table pickers elsewhere in this
    project (Land Parcel Source, Road Network Source, DTM Source, POI
    Source, Influence Map Source -- each of their own tool files' own
    pickers), not merely speculated about. This task itself wires up
    exactly ONE category ("gm_workspace") into exactly ONE call site
    (core/startup_and_db_ui.py's show_startup_dialog()) -- it does not
    touch, wire up, or lay groundwork for any of those other, future
    call sites.

    This is intentionally NOT the same module as
    utils/app_preferences.py, even though the two are superficially
    similar (both are small, disk-persisted, best-effort key/value
    stores under the same %APPDATA%\\CAMA-Tools folder). They persist
    a genuinely different KIND of thing: app_preferences.py's one
    preference is a one-time UI dismissal flag that is deliberately
    build-fingerprint-gated (reset on every rebuild -- see that
    module's own docstring, STATE MACHINE). The data this module
    persists -- a remembered filesystem path the user will want to
    reuse across ordinary app restarts AND across a rebuild of the
    same application -- must NOT reset on rebuild; a fresh build
    forcing the user to re-browse for the same workspace file every
    time would defeat the entire point of remembering it. See this
    task's own Instructions Section A.3 / E for the full reasoning.

STORAGE:
    A new, standalone file -- last_directory.json -- under the SAME
    per-user %APPDATA%\\CAMA-Tools folder db_discovery.py's
    get_credentials_path() and app_preferences.py's
    get_preferences_path() already resolve and create. This is a
    separate, standalone file, not merged with pg_credentials.json,
    gm_exe_path.json, or ui_preferences.json.
    _get_last_directory_path() below duplicates the small amount of
    %APPDATA% + os.makedirs(...) resolution logic those other two
    modules each already duplicate independently, for the same reason
    documented in app_preferences.py's own STORAGE section: this
    module is a leaf, not a reverse dependency on db_discovery.py's or
    app_preferences.py's own file-specific contracts.

    The on-disk shape is a flat {category: path, ...} JSON dict --
    nothing more elaborate (no timestamps, no per-category history, no
    sub-objects) than this task actually needs. The FULL file path is
    stored per category (not just its containing directory); a caller
    that only wants the containing folder can derive it with
    os.path.dirname(...) itself.

NOT BUILD-FINGERPRINT-GATED:
    Unlike app_preferences.py's dont_show_again preference, nothing
    saved here is scoped to the currently-running .exe's own identity.
    A path saved by one build is honored by any later build (or by dev
    mode) exactly the same way, subject only to the existence check
    below -- see PURPOSE above for why this differs from
    app_preferences.py's own gating policy.

INPUTS:
    get_last_directory(category): category (str) -- the picker's own
    key, e.g. "gm_workspace".
    set_last_directory(category, path): category (str), path (str) --
    the full file path to remember for that category.

OUTPUTS:
    get_last_directory(category) -> str | None: the last-saved full
    path for that category, or None if nothing has ever been saved for
    it, or the saved path no longer exists on disk (os.path.exists(...)
    is checked HERE, inside this shared module, since it is the same
    check regardless of category -- see module PURPOSE). Does NOT
    validate file extension/type -- that varies per category and is
    each CALLER's own responsibility (e.g. core/startup_and_db_ui.py's
    show_startup_dialog() additionally checks the returned path ends
    in ".gmw" before trusting it as a workspace file).
    set_last_directory(category, path) -> None. Best-effort: a write
    failure (permissions, a locked file, %APPDATA% unavailable, etc.)
    is silently ignored, matching app_preferences.py's own established
    best-effort-write posture for every preference it manages.

DEPENDENCIES:
    os, json (stdlib). No third-party dependencies, no local imports --
    this module is a leaf, matching core/tool_exclusivity.py's,
    db_discovery.py's, and app_preferences.py's own documented
    leaf-module role elsewhere in this project.

SIDE EFFECTS:
    Reads/writes last_directory.json under %APPDATA%\\CAMA-Tools.
    Creates that folder if it doesn't exist yet (os.makedirs(...,
    exist_ok=True), same as db_discovery.py and app_preferences.py).
    No side effects occur at import time -- everything above happens
    only when get_last_directory() or set_last_directory() is actually
    called.
"""
import os
import json


def _get_last_directory_path():
    """
    Resolves the absolute path to last_directory.json under the
    per-user %APPDATA%\\CAMA-Tools folder -- same folder
    db_discovery.py's get_credentials_path() and app_preferences.py's
    get_preferences_path() already resolve and create, but a
    different, standalone file. See module docstring, STORAGE, for why
    this duplicates that folder-resolution logic rather than
    importing/extending either of those modules.

    Side effects:
        - Creates %APPDATA%\\CAMA-Tools (os.makedirs(..., exist_ok=True))
          if it doesn't exist yet.

    Raises:
        RuntimeError: if the APPDATA environment variable is not set --
        same contract as db_discovery.py's and app_preferences.py's own
        path-resolution functions, for the same reason (no silent
        fallback to some other location).
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError(
            "The Windows APPDATA environment variable is not available, "
            "so this application cannot determine where to store its "
            "configuration files."
        )
    base_dir = os.path.join(appdata, "CAMA-Tools")
    os.makedirs(base_dir, exist_ok=True)
    return os.path.join(base_dir, "last_directory.json")


def _load_raw_last_directories():
    """Loads last_directory.json as a plain dict, tolerating a missing
    or malformed file the same way app_preferences.py's
    _load_raw_preferences() tolerates a missing/malformed
    ui_preferences.json -- returns {} rather than raising, since an
    absent or corrupt file is a normal, expected state (first run, or
    a fresh %APPDATA% folder), not an error condition worth surfacing
    to the user for a remembered-path convenience feature."""
    try:
        path = _get_last_directory_path()
    except RuntimeError:
        return {}
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def get_last_directory(category):
    """
    Returns the last-saved full path for this category, or None if
    nothing has ever been saved for it, or the saved path no longer
    exists on disk. See module docstring, OUTPUTS, for the full
    contract -- in particular, this function does NOT validate file
    extension/type; that is each caller's own responsibility.

    Args:
        category (str): the picker's own key, e.g. "gm_workspace".

    Returns:
        str | None
    """
    saved = _load_raw_last_directories()
    path = saved.get(category)
    if not path:
        return None
    if not os.path.exists(path):
        return None
    return path


def set_last_directory(category, path):
    """
    Saves path as the last-used one for category. Best-effort: a write
    failure (permissions, a locked file, %APPDATA% unavailable, etc.)
    is silently ignored, matching app_preferences.py's own established
    best-effort-write posture for every preference it manages.

    Args:
        category (str): the picker's own key, e.g. "gm_workspace".
        path (str): the full file path to remember for that category.

    Side effects:
        Writes last_directory.json under %APPDATA%\\CAMA-Tools.
    """
    try:
        resolved_path = _get_last_directory_path()
    except RuntimeError:
        return
    saved = _load_raw_last_directories()
    saved[category] = path
    try:
        with open(resolved_path, "w") as f:
            json.dump(saved, f)
    except Exception:
        pass
