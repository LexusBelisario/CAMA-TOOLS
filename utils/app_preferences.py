"""
utils/app_preferences.py

PURPOSE:
    Disk-persisted UI preference state for the "don't show again"
    checkbox on the Configure-DB dialog's Global-Mapper-independence
    note (see core/startup_and_db_ui.py's show_configure_db_dialog()).

    Persisted (survives an app restart), but scoped to the CURRENT
    build: checking "don't show again" suppresses the note across
    restarts of the SAME built .exe, but a genuinely new build (a
    fresh pyinstaller run producing a new dist\\Land Valuation
    Tools.exe -- see build.bat, which deletes and recreates the whole
    dist\\ folder on every run, making "new .exe file" a real,
    meaningful build event for this project) resets it, so the note is
    shown again at least once per build.

    This is intentionally NOT extended into a generic key/value
    preferences store with a get_preference(key)/set_preference(key,
    value) API -- there is exactly one preference to persist right
    now (the don't-show-again flag for this one note). Per the Rule of
    Three (see this codebase's own architecture-preservation
    guidance), a generic abstraction is not introduced ahead of a
    third genuinely-identical need; this module's two public functions
    (should_show_db_note(), set_dont_show_db_note()) are named for
    what they mean, not for a key string, and a future second
    preference is expected to get its own similarly-named function
    pair here rather than forcing this one into a generic shape
    retroactively.

STATE MACHINE (build-fingerprint gated):
    The preference file stores TWO things together: the checkbox's
    last-set value, and a fingerprint of the .exe that was running
    when it was set. should_show_db_note() only honors a saved "don't
    show again = True" when the CURRENT .exe's fingerprint matches the
    saved one -- any mismatch (including no saved fingerprint at all)
    means "show the note," exactly once, until the user checks the
    box again under this new build. This is deliberately narrower than
    a bare boolean flag: a bare flag would suppress the note forever,
    across every future rebuild, which is not what "don't show again"
    is meant to promise here -- it is scoped to "not again this
    build," not "not again ever."

FINGERPRINT CHOICE (why content hash, not file mtime/size):
    The fingerprint is a SHA-256 hash of the running .exe's own file
    content (sys.executable, frozen builds only), not its OS-level
    modification timestamp or file size. A content hash is
    deterministic from the deployed artifact itself: copying the exact
    same .exe to another machine, or a deployment process that touches
    file metadata without changing the binary's actual bytes, still
    yields the same fingerprint (correctly recognized as "the same
    build"), which a bare mtime/size comparison could get wrong. This
    matters specifically because build.bat's own "always a fresh
    dist\\ folder" behavior is what makes ANY new .exe a meaningful
    build event for this project in the first place (see PURPOSE
    above) -- the fingerprint's job is to reliably answer "is this
    exact .exe the one the checkbox was last set under", and a content
    hash is the most direct, artifact-derived way to answer that,
    rather than a proxy signal (timestamp) that can drift for reasons
    unrelated to the actual binary content.

    In dev mode (`python MAIN.py`, not frozen), there is no built .exe
    to fingerprint at all -- _compute_exe_fingerprint() returns None in
    that case, and should_show_db_note() always returns True (the note
    always shows in dev mode; "don't show again" has no meaningful
    build to scope itself to there, so it is not honored rather than
    silently doing something arbitrary).

STORAGE:
    A new, standalone file -- ui_preferences.json -- under the SAME
    per-user %APPDATA%\\CAMA-Tools folder db_discovery.py's
    get_credentials_path() already resolves and creates (see that
    module's own docstring). This is a NEW, separate file, not an
    added key inside pg_credentials.json -- that file's on-disk format
    (host, port, database, schema, username, password) is explicitly
    unchanged elsewhere in this project's own history, and this
    module does not touch it. get_preferences_path() below duplicates
    db_discovery.py's %APPDATA% + os.makedirs(...) resolution pattern
    exactly (same folder, same RuntimeError-if-APPDATA-missing
    contract) rather than importing/extending
    db_discovery._get_credentials_path(), since that function's own
    docstring and OUTPUTS section describe it as specific to
    pg_credentials.json, not as a generic %APPDATA%\\CAMA-Tools
    resolver -- duplicating the small amount of folder-resolution
    logic here keeps this module a genuine leaf, not a reverse
    dependency on db_discovery.py's own file-specific contract.

    The %APPDATA%\\CAMA-Tools folder name itself is left exactly as
    db_discovery.py already uses it -- unrelated to, and unaffected
    by, this project's separate application-branding rename (see that
    rename's own change map: this folder is a persisted-data
    compatibility path, kept unchanged so existing users do not lose
    already-saved credentials/preferences merely because the
    application's visible name changed).

INPUTS:
    should_show_db_note(): none.
    set_dont_show_db_note(value): value (bool) -- the checkbox's new
    state, as just set by the user in the Configure-DB dialog.

OUTPUTS:
    get_preferences_path() -> str: absolute path to
    ui_preferences.json under %APPDATA%\\CAMA-Tools.
    should_show_db_note() -> bool: True if the note should be shown
    (no saved preference, a saved preference from a different build,
    or a saved preference of False/"show it"), False only when the
    CURRENT build's fingerprint matches a saved "don't show again"
    preference exactly.
    set_dont_show_db_note(value) -> None. Writes value together with
    the current build's fingerprint (or without one, in dev mode) to
    ui_preferences.json. Best-effort: a write failure (permissions, a
    locked file, etc.) is silently ignored, matching
    db_discovery.py's own best-effort-write posture elsewhere in this
    project -- a failure to persist this one cosmetic UI preference is
    not worth surfacing an error dialog over.

DEPENDENCIES:
    os, sys, json, hashlib (stdlib). No third-party dependencies, no
    local imports -- this module is a leaf, matching
    core/tool_exclusivity.py's and core/startup_and_db_ui.py's own
    documented leaf-module role elsewhere in this project.

SIDE EFFECTS:
    Reads/writes ui_preferences.json under %APPDATA%\\CAMA-Tools.
    Creates that folder if it doesn't exist yet (os.makedirs(...,
    exist_ok=True), same as db_discovery.py). Reads the full content
    of the running .exe file (sys.executable) to compute its SHA-256
    hash, but only when frozen -- no effect at all in dev mode. No
    side effects occur at import time -- everything above happens only
    when should_show_db_note() or set_dont_show_db_note() is actually
    called.
"""
import os
import sys
import json
import hashlib


def get_preferences_path():
    """
    Resolves the absolute path to ui_preferences.json under the
    per-user %APPDATA%\\CAMA-Tools folder -- same folder
    db_discovery.py's get_credentials_path() already resolves and
    creates, but a different, standalone file. See module docstring,
    STORAGE, for why this duplicates that folder-resolution logic
    rather than importing/extending db_discovery.py itself.

    Side effects:
        - Creates %APPDATA%\\CAMA-Tools (os.makedirs(..., exist_ok=True))
          if it doesn't exist yet.

    Raises:
        RuntimeError: if the APPDATA environment variable is not set --
        same contract as db_discovery.py's own _get_credentials_path(),
        for the same reason (no silent fallback to some other
        location).
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
    return os.path.join(base_dir, "ui_preferences.json")


def _compute_exe_fingerprint():
    """
    Returns a SHA-256 hex digest of the currently-running .exe's own
    file content (sys.executable), or None if this process is not a
    frozen (PyInstaller) build -- see module docstring, FINGERPRINT
    CHOICE, for why a content hash was chosen over a modification-time
    or file-size comparison.

    Returns:
        str | None: the hex digest, or None in dev mode (`python
        MAIN.py`) or if the .exe's content could not be read for any
        reason (best-effort -- a read failure here is treated the same
        as "no fingerprint available", not surfaced as an error, since
        the only consequence is that should_show_db_note() falls back
        to its default of showing the note).
    """
    if not getattr(sys, "frozen", False):
        return None
    try:
        hasher = hashlib.sha256()
        with open(sys.executable, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception:
        return None


def _load_raw_preferences():
    """Loads ui_preferences.json as a plain dict, tolerating a missing
    or malformed file the same way db_discovery.py's
    load_db_credentials() tolerates a missing/malformed
    pg_credentials.json -- returns {} rather than raising, since an
    absent or corrupt preferences file is a normal, expected state
    (first run, or a fresh %APPDATA% folder), not an error condition
    worth surfacing to the user for a cosmetic UI preference."""
    try:
        path = get_preferences_path()
    except RuntimeError:
        return {}
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def should_show_db_note():
    """
    True if the Configure-DB dialog's Global-Mapper-independence note
    should be shown, False if the user has already checked "don't show
    again" under the CURRENT build specifically. See module docstring,
    STATE MACHINE, for the exact matching rule.

    Returns:
        bool: False only when a saved preference exists, its
        "dont_show_again" value is True, AND its saved
        "exe_fingerprint" matches _compute_exe_fingerprint() exactly
        (including the dev-mode case, where both sides are None --
        see the note below). True in every other case: no saved
        preference, a saved preference from a different build, or a
        saved preference that is not "don't show again".

        Dev-mode note: _compute_exe_fingerprint() returns None in dev
        mode. A prior save's exe_fingerprint would also be None only
        if that save itself happened in dev mode -- there is no
        practical path to a None/None match against a REAL prior
        frozen-build save, since a frozen build's fingerprint is never
        None. This function does not special-case dev mode separately
        from the general matching rule; the rule's own None-vs-None
        equality already produces the intended behavior (dev-mode
        saves only suppress the note across dev-mode runs, never
        across a real build, and vice versa).
    """
    prefs = _load_raw_preferences()
    if not prefs.get("dont_show_again", False):
        return True
    saved_fingerprint = prefs.get("exe_fingerprint")
    current_fingerprint = _compute_exe_fingerprint()
    return saved_fingerprint != current_fingerprint


def set_dont_show_db_note(value):
    """
    Persists the Configure-DB dialog's "don't show again" checkbox
    state, together with the CURRENT build's fingerprint, to
    ui_preferences.json. Called by the dialog itself the instant the
    checkbox is toggled -- see core/startup_and_db_ui.py's
    show_configure_db_dialog() for the call site (not part of this
    module; this module only defines the storage/matching logic, not
    the checkbox widget itself).

    Args:
        value (bool): the checkbox's new state. True means "don't show
            again" (for this build); False means "show it again" (the
            user unchecked the box), which simply saves
            dont_show_again=False -- should_show_db_note() would
            already return True for that regardless of the saved
            fingerprint, but writing it explicitly keeps the on-disk
            state an honest record of the checkbox's own last-set
            value, not just an all-or-nothing signal, in case a future
            reader other than should_show_db_note() ever wants that
            distinction.

    Side effects:
        Writes ui_preferences.json under %APPDATA%\\CAMA-Tools.
        Best-effort: a write failure (permissions, a locked file, disk
        full, etc.) is silently ignored -- see module docstring,
        OUTPUTS, for why this is not surfaced as an error to the user.
    """
    try:
        path = get_preferences_path()
    except RuntimeError:
        return
    try:
        with open(path, "w") as f:
            json.dump({
                "dont_show_again": bool(value),
                "exe_fingerprint": _compute_exe_fingerprint(),
            }, f)
    except Exception:
        pass
