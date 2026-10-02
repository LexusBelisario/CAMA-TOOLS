"""
core/gm_script_runner.py

PURPOSE:
    Retrieves the list of every layer currently loaded in the user's
    Global Mapper workspace (and their exact display names/
    descriptions) WITHOUT disturbing, taking over, or visibly
    automating the user's already-open, locked Global Mapper window --
    see HOW THIS WORKS below for the mechanism, and module history
    further down for the earlier, visible-automation design this
    replaced and why.

    This module exists to support exactly one new capability: letting
    MAIN.py's new Database Management "LOAD TABLE" orchestration
    function (see MAIN.py's own upcoming edit for this task) check
    whether a database table the user picked is ALREADY loaded into
    the open Global Mapper workspace, before deciding whether to load
    it again. This module does the "ask Global Mapper what is
    currently loaded" half of that; MAIN.py's own orchestration
    function does the comparison and, for a not-yet-loaded table,
    reuses the existing export-then-Ctrl+O pattern already proven in
    MAIN.py's own update_map_and_select_recorded() -- this module does
    not duplicate that second half; it is scoped to the query-loaded-
    layers step only.

HOW THIS WORKS (current design -- headless, no visible automation):
    1. A single, brief, visible keystroke -- Ctrl+S sent to the
       already-focused Global Mapper window -- saves the user's
       current workspace to its own existing .gmw file. This is the
       ONLY visible interaction with the user's own window; nothing
       else about it is touched, and no new window is ever opened
       inside it.
    2. A SEPARATE, second, independent Global Mapper process is
       launched headlessly (no UI at all, confirmed hands-on -- see
       module history) via a short, fixed Global Mapper Script that:
       a. Loads that same .gmw file via EMBED_SCRIPT (confirmed,
          hands-on, to pick up layers added since the LAST save --
          this is exactly why step 1's Ctrl+S must happen immediately
          beforehand, every time, not be skipped as an optimization).
       b. Loops over every layer it just loaded (LAYER_LOOP_START) and
          writes each one's own display name (%LAYER_DESC%) to a
          plain output file via SET_LOG_FILE + LOG_MESSAGE.
    3. This headless process exits on its own once the script
       completes (confirmed, hands-on: Blue Marble's own documentation
       states a script run this way causes Global Mapper to
          "immediately exit when the script file completes
          processing" -- and this was independently reproduced: the
          subprocess this module launches reliably returns within
          roughly 8-10 seconds with no lingering process left behind).
    4. This module reads the output file the second process wrote,
       parses out the layer description lines, and returns them.

    The user's own, already-open Global Mapper window is NEVER
    refocused, never has any dialog opened inside it beyond the single
    Ctrl+S, and is completely free to keep using throughout steps 2-4
    above (those steps run in a wholly separate OS process against a
    file on disk, not against the user's own window or its in-memory
    state).

WHY THIS MECHANISM, AND WHY NOT SOMETHING SIMPLER OR MORE DIRECT
(locked design, Instructions E.3 as originally written -- see module
history below for how this module's OWN mechanism evolved within that
locked design as new evidence came in):
    - No native IMPORT/connection-string command exists with a
      publicly documented PostGIS connection-string syntax -- guessing
      one was rejected as too risky.
    - The Global Mapper Python SDK (`import globalmapper as gm`),
      called externally, links its own separate, invisible engine --
      it has NO connection to the visible, already-open Global Mapper
      window a user is looking at, and requires a separate, additional
      paid SDK license beyond a standard Global Mapper Pro license --
      a real deployment/cost blocker for LGU/BLGF distribution,
      independent of the technical reason. This applies equally
      whether that SDK is called from an external Python process or
      (per the Script Editor's own confirmed "Cannot load PyGM.dll"
      error on the developer's own machine) from inside a .gms script
      itself -- neither path was used or is used anywhere in this
      module.
    - A plain headless/batch run of a .gms file against NOTHING (no
      EMBED_SCRIPT, no prior IMPORT) was independently confirmed,
      hands-on, to have NO access to any layer except ones the script
      itself re-imports -- this is why step 2a above (EMBED_SCRIPT
      loading the user's own, just-saved .gmw) is necessary; a bare
      headless LAYER_LOOP_START with nothing imported first finds
      nothing.

MODULE HISTORY -- WHY THIS REPLACED AN EARLIER, VISIBLE-AUTOMATION
DESIGN (kept here because the earlier design's own reasoning, and the
reason it was abandoned, are both worth a future reader having without
re-deriving them):
    An earlier version of this module drove Global Mapper's own Script
    Editor open on screen (Ctrl+Shift+O), typed the fixed script into
    it, checked its "Run Script in the Context of the Main View"
    checkbox via a confirmed Tab+Space sequence, and clicked its "Run
    Script" button via maximized-window-relative coordinates -- all of
    this fully implemented and confirmed working, hands-on, end to
    end, against the developer's own Global Mapper Pro v26
    installation (multiple consecutive successful runs returning the
    correct, exact loaded-layer list, including the confirmed
    "{schema}.{table} ({connection name})" format for database-sourced
    layers).

    That design was abandoned in favor of the current one for a single
    reason: it required Global Mapper's own Script Editor window to
    visibly open, populate, and run on screen in front of the user
    every time a LOAD TABLE check ran -- the developer's own stated
    preference was for this to be invisible to the user, and
    investigation (prompted by that preference, and independently
    confirmed via the official Global Mapper Scripting Reference and
    further hands-on testing) found that EMBED_SCRIPT, run from a
    SEPARATE headless process against the user's own saved workspace
    file, achieves the exact same result -- the correct, exact loaded-
    layer list -- with zero visible UI beyond the single Ctrl+S
    keystroke. The earlier design's full set of keystroke/timing fixes
    (the ~2 second wait for the Script Editor window to appear in
    pygetwindow's own enumeration on first open per session; the need
    to close any stray pre-existing Script Editor window before
    starting, since its checkbox state is a TOGGLE and an already-
    checked stray window would be unchecked by this module's own
    Tab+Space step) is retained in this file's own git/version history
    for reference, but none of that logic is part of the current
    design: there is no Script Editor interaction of any kind left in
    this module.

    One real, accepted trade-off from this change: the LOGICAL
    headless process's own startup cost (confirmed, hands-on: roughly
    8-10 seconds from launch to exit, since the full 64-bit Global
    Mapper binary and its projection/licensing engines must initialize
    fresh every single time, exactly as they would for a normal user-
    facing launch) is NOT meaningfully faster than the Script-Editor-
    based design it replaced (confirmed, hands-on: that design
    completed in well under 5 seconds once Global Mapper's Script
    Editor subsystem was "warmed up" within a session). This module's
    design was chosen for being invisible to the user, not for being
    faster -- a future reader re-measuring this trade-off should not
    assume headless execution is the quicker option; it is not.

DEPENDENCIES:
    stdlib: os, time, subprocess.
    third-party: pygetwindow (gw), pyautogui -- used ONLY for the
    single Ctrl+S keystroke to the user's own, already-open Global
    Mapper window (see _save_user_workspace() below); no Script Editor
    or other dialog automation remains in this module.
    local: utils.last_directory (get_last_directory) -- a genuine,
    already-established utils/ module (used today by core/startup_
    and_db_ui.py for this exact same purpose) that reads the per-user
    "%APPDATA%\\CAMA-Tools\\last_directory.json" file to recover the path
    of the Global Mapper Workspace file the user most recently started
    a session with. Importing a utils/ module is consistent with this
    project's own layering (Instructions A.1/A.3: utils/ modules are
    "genuinely reusable... used directly by tool files and/or
    MAIN.py"); this module still imports nothing from MAIN.py or any
    other core/ module, preserving its leaf-level status.
    A NEW pip dependency was deliberately NOT introduced for the
    subprocess launch -- Python's own stdlib `subprocess` module is
    used, never a new third-party process-management library
    (Instructions D).

A KNOWN, ACCEPTED LIMITATION OF SOURCING THE WORKSPACE PATH FROM
last_directory.json (not a bug to silently work around):
    utils.last_directory's own "gm_workspace" entry is written exactly
    ONCE per session, at startup, when the user picks a workspace file
    and presses START on the startup dialog (see core/startup_and_db_
    ui.py's own show_startup_dialog()). It is NOT updated if the user
    later uses Global Mapper's own File -> Save Workspace As... to
    switch to a DIFFERENT .gmw file mid-session. In that specific,
    expected-to-be-rare scenario, this module's own EMBED_SCRIPT step
    would load the ORIGINAL workspace file, not whatever new file the
    user most recently switched to -- producing an inaccurate (but not
    crashing, not data-corrupting) "already loaded" check for that one
    session. This is accepted as an edge case worth documenting, not
    solving in this task's scope -- the common case (one workspace
    file for the lifetime of a session, which is how Global Mapper
    automation already behaves throughout the rest of this codebase)
    is unaffected.

A KNOWN, ACCEPTED, NON-DANGEROUS LOG ARTIFACT (do not mistake this for
a real failure when reading this module's own diagnostic logs):
    Every headless run of the EMBED_SCRIPT-based script against a REAL
    .gmw file produces a line reading "ERROR: Large number of unknown
    commands encountered, likely file corruption." in its own log
    output. This was confirmed, hands-on, to NOT indicate a corrupted
    file and to NOT interrupt script execution -- it originates from
    UI/pane-layout XML tags Global Mapper's own GUI writes into the
    tail of every .gmw file it saves (to remember dockable-panel
    positions), which the script-processing engine does not recognize
    as valid script syntax when running outside the main view context
    and simply skips, logging this message in the process. The correct,
    complete loaded-layer list was confirmed present in the SAME output
    file, on every single test run, alongside this message -- this
    module's own _parse_loaded_layers() below filters it out like any
    other known non-layer line (see _NON_LAYER_LINE_PREFIXES) and never
    treats its presence as a failure signal.
"""

import os
import subprocess
import time

import pygetwindow as gw
import pyautogui

from utils.last_directory import get_last_directory


# ============================================================
# FIXED GMS SCRIPT -- see module docstring, HOW THIS WORKS, for the
# mechanism this implements.
# ============================================================
#
# Confirmed, hands-on, against the developer's own Global Mapper Pro
# v26.0 (b121824) installation, across several separate headless runs
# with consistent results:
#   - EMBED_SCRIPT FILENAME="...": loads the user's own, just-saved
#     .gmw workspace file into this otherwise-empty headless process --
#     confirmed to pick up layers added since the PREVIOUS save, so
#     long as a fresh save (this module's own _save_user_workspace())
#     happens immediately beforehand on every call.
#   - SET_LOG_FILE FILENAME=... APPEND_TO_FILE=NO: deletes any stale
#     output file from a previous run and starts a fresh one, per that
#     command's own documented behavior.
#   - LAYER_LOOP_START FILENAME="*": loops every layer EMBED_SCRIPT
#     just loaded. Explicit "*" avoids an extra "Missing FILENAME"
#     warning line in the output (cosmetic only -- this module's own
#     parser would skip it either way).
#   - LOG_MESSAGE %LAYER_DESC%: writes exactly the layer's own display
#     name/description, one line per layer, confirmed to NOT include
#     any [N Features] suffix and to use the exact format
#     "{schema}.{table} ({connection name})" (with a literal space
#     before the parenthesis) for database-sourced layers -- see
#     build_expected_description() below, which reproduces this exact
#     format for comparison.
#
# ENABLE_PROGRESS=NO on the GLOBAL_MAPPER_SCRIPT header line suppresses
# Global Mapper's own progress-bar/status overhead during the headless
# run -- confirmed, hands-on, to have no effect on correctness, kept
# as a harmless minor speed aid. SHOW_WARNINGS=NO was ALSO tried and
# confirmed, hands-on, to NOT suppress the "likely file corruption"
# line described in the module docstring above (that line is a true
# ERROR in Global Mapper's own classification, and the scripting
# engine always displays true errors regardless of SHOW_WARNINGS) --
# it is omitted here since it has no effect on the one thing it was
# tried for, though it is harmless to have included.
#
# The %WORKSPACE_PATH% and %OUTPUT_PATH% placeholders below are
# substituted at write-time by _write_script_file() -- this constant
# is never used as-is.
_GMS_SCRIPT_TEMPLATE = (
    'GLOBAL_MAPPER_SCRIPT VERSION=1.00 ENABLE_PROGRESS=NO\n'
    'SET_LOG_FILE FILENAME="%OUTPUT_PATH%" APPEND_TO_FILE=NO\n'
    'EMBED_SCRIPT FILENAME="%WORKSPACE_PATH%"\n'
    'LAYER_LOOP_START FILENAME="*"\n'
    'LOG_MESSAGE %LAYER_DESC%\n'
    'LAYER_LOOP_END\n'
)

# Lines Global Mapper's own headless script-processing engine writes
# into the SET_LOG_FILE output file itself, around the actual per-layer
# LOG_MESSAGE lines this module cares about. Confirmed, hands-on,
# across several separate headless runs -- the exact set of lines
# observed here is a strict superset of what the earlier, Script-
# Editor-based design produced (see module history above), since the
# headless EMBED_SCRIPT path additionally logs its own import/
# projection/layout-skip messages before reaching the loop. A known-
# line EXCLUSION list (skip anything matching one of these shapes),
# not a fixed line-count assumption -- deliberately, since the exact
# set of lines Global Mapper emits here is not part of any documented,
# stable contract and could plausibly change between versions or
# between runs with a different number of imported layers/database
# connections.
_NON_LAYER_LINE_PREFIXES = (
    "Running Script ",
    "Global Mapper Pro ",
    "Starting loop over layers",
    "Changing current/default directory",
    "Loop over values completed.",
    "Script processing COMPLETED",
    "Importing file ",
    "Importing Spatial Database",
    "Removed all loaded overlays.",
    "Loaded new global display",
    "Reading map layout",
    "Skipping pane layout",
    "WARNING:",
    "ERROR:",
)

# Fixed filenames (inside the scripts subfolder -- see _scripts_dir()
# below) the .gms script and its own output are written to/read from.
_SCRIPT_FILENAME = "query_loaded_layers_headless.gms"
_OUTPUT_FILENAME = "loaded_layers_headless_output.txt"

# Confirmed, hands-on: a full headless run (launch global_mapper.exe,
# load the .gmw via EMBED_SCRIPT, loop layers, exit) took 8.5-9.5
# seconds across several separate test runs on the developer's own
# machine, with no relationship found between that time and the
# number of loaded layers in the small test workspaces used (3-6
# layers each) -- the dominant cost is Global Mapper's own fixed
# process-startup overhead (initializing its projection/licensing
# engines fresh every launch), not per-layer work. This timeout is a
# generous upper bound for a workspace with many more layers or a
# slower machine, not a tuned minimum.
_HEADLESS_PROCESS_TIMEOUT_SECONDS = 60


def _is_layer_description_line(line):
    """
    Returns True if `line` (already stripped of trailing newline) looks
    like a genuine %LAYER_DESC% output line from the fixed script
    above, False if it matches one of Global Mapper's own known
    auto-generated header/footer/import/warning/error line shapes (see
    _NON_LAYER_LINE_PREFIXES above) or is blank.

    Deliberately a prefix check, not an exact-line match: several of
    Global Mapper's own lines (timestamps, file paths, elapsed times)
    embed run-specific values that cannot be matched exactly -- the
    fixed prefixes confirmed hands-on are stable across runs even
    though the full line is not.
    """
    stripped = line.strip()
    if not stripped:
        return False
    for prefix in _NON_LAYER_LINE_PREFIXES:
        if stripped.startswith(prefix):
            return False
    return True


def _scripts_dir(temp_dir_root):
    """
    Returns the dedicated scripts subfolder path, under the caller-
    supplied temp_dir_root (MAIN.py's own existing TEMP_DIR constant,
    r"C:\\Global Mapper Temp" -- passed in explicitly, never
    hardcoded or re-derived here, since this module must not import
    MAIN.py).

    Per Instructions E.6/I4: kept separate from TEMP_DIR's own
    existing exported-data temp files (e.g. updatemap.gpkg) -- this
    subfolder holds only this module's own script/output files.
    """
    return os.path.join(temp_dir_root, "scripts")


def _write_script_file(temp_dir_root, workspace_path, output_path):
    """
    Writes the fixed GMS script (see _GMS_SCRIPT_TEMPLATE above), with
    %WORKSPACE_PATH% and %OUTPUT_PATH% substituted, to a fixed filename
    inside the scripts subfolder (see _scripts_dir() above), creating
    that subfolder first if it does not already exist.

    Always overwrites the script file with the SAME fixed structure on
    every call (only the two substituted paths can differ between
    calls, and the workspace path in practice does not change within
    a session -- see module docstring's own known limitation about
    last_directory.json) -- this is a deliberate, never-regenerated-
    per-request-beyond-these-two-substitutions constant (Instructions
    E.6), so re-writing it every run is harmless and simpler than
    checking whether it already exists.

    Returns:
        str: the full path to the .gms script file just written.

    Raises:
        OSError (or a subclass): if the scripts subfolder cannot be
            created, or the script file cannot be written. The caller
            is responsible for catching this and showing a plain,
            non-technical error.
    """
    scripts_dir = _scripts_dir(temp_dir_root)
    os.makedirs(scripts_dir, exist_ok=True)

    script_path = os.path.join(scripts_dir, _SCRIPT_FILENAME)
    script_content = (
        _GMS_SCRIPT_TEMPLATE
        .replace("%WORKSPACE_PATH%", workspace_path)
        .replace("%OUTPUT_PATH%", output_path)
    )

    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script_content)

    return script_path


def _output_file_path(temp_dir_root):
    """
    Returns the fixed path the GMS script's own SET_LOG_FILE command
    writes its per-layer output to. Stale output is never misread as
    the current result because the script itself always passes
    APPEND_TO_FILE=NO -- Global Mapper's own SET_LOG_FILE command
    deletes this file at the START of every run, before writing
    anything new to it. This module only reads this file AFTER the
    headless subprocess has fully exited (see run_and_get_loaded_
    layers() below) -- never speculatively, before a run has actually
    completed.
    """
    return os.path.join(_scripts_dir(temp_dir_root), _OUTPUT_FILENAME)


def _parse_loaded_layers(output_path, log_fn=None):
    """
    Reads `output_path` (the GMS script's own SET_LOG_FILE output --
    see _output_file_path() above) and returns the list of layer
    description lines found in it, in the order Global Mapper wrote
    them, skipping every line _is_layer_description_line() rejects.

    Args:
        output_path: path to the output file to read.
        log_fn: optional callable(str), default None. Called once
            with a short diagnostic message if the file cannot be
            read at all. When None (the default), nothing is called.

    Returns:
        list[str] | None: the layer description lines found (possibly
        an empty list, if the output file exists and was read
        successfully but genuinely contains no layer lines -- a
        SUCCESSFUL result, not a failure), or None if the file could
        not be read at all. Callers must treat None as "the loaded-
        layer list is unknown" and must NOT proceed to a load-table
        decision based on it -- per Instructions E.9, a failed read
        must abort with a plain error rather than silently falling
        through to loading blindly.
    """
    try:
        with open(output_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        if log_fn is not None:
            log_fn(f"gm_script_runner: could not read {output_path}: "
                    f"{type(e).__name__}: {e}")
        return None

    return [line.rstrip("\r\n") for line in lines if _is_layer_description_line(line)]


def _find_gm_window():
    """
    Finds the already-open, already-running Global Mapper Pro window
    by title, the same way MAIN.py's own update_map_and_select_
    recorded() already does (see that function's own inline focus
    logic) -- a plain gw.getWindowsWithTitle("Global Mapper Pro") scan,
    keeping the first match whose title also contains "global mapper"
    case-insensitively.

    Returns:
        A pygetwindow Window object for the Global Mapper Pro window,
        or None if no matching window is currently open.
    """
    for w in gw.getWindowsWithTitle("Global Mapper Pro"):
        if "global mapper" in w.title.lower():
            return w
    return None


def _save_user_workspace(gm_window, log_fn=None):
    """
    Sends a single Ctrl+S keystroke to the user's own, already-focused
    Global Mapper window, saving its current workspace to its own
    existing .gmw file -- the ONLY visible interaction this module has
    with the user's own window (see module docstring, HOW THIS WORKS).

    WHY THIS IS NECESSARY, EVERY TIME, NOT SKIPPABLE: confirmed,
    hands-on, that the headless EMBED_SCRIPT step (see
    run_and_get_loaded_layers() below) reads the .gmw file's own
    ON-DISK, last-saved content -- NOT the live, in-memory state of
    the user's currently-open window. A layer added to the visible
    workspace since the last save is invisible to the headless check
    until a fresh save happens. Two separate hands-on tests confirmed
    this exactly: a newly-loaded layer was absent from the headless
    result both before AND immediately after a believed-successful
    Ctrl+S (titlebar asterisk gone), until the specific combination of
    "save, then run headless" was followed in that exact order on a
    single, uninterrupted attempt.

    Assumes the workspace already has a filename (Ctrl+S saves
    silently to the existing file in that case, with no dialog) --
    this holds for the normal case this module is designed around (an
    already-open session with a workspace loaded via the startup
    dialog's own Browse flow, which always assigns a .gmw filename
    before Global Mapper is even launched). A workspace with no
    filename yet (not expected in this application's own startup
    flow) would instead raise a "Save Workspace As..." dialog that
    this module does not handle -- see run_and_get_loaded_layers()'s
    own docstring for how a stuck/unexpected dialog here is NOT
    specially detected, only the overall timeout protects against it.

    Does not return anything; the caller proceeds immediately after a
    short settle delay.
    """
    gm_window.activate()
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "s")
    time.sleep(0.5)
    if log_fn is not None:
        log_fn("gm_script_runner: sent Ctrl+S to save user's workspace")


def run_and_get_loaded_layers(temp_dir_root, gm_exe_path, log_fn=None):
    """
    THE single public entry point of this module. Retrieves the list
    of every layer currently loaded in the user's Global Mapper
    workspace, via the headless mechanism described in the module
    docstring (HOW THIS WORKS):

        1. Locate the user's already-open Global Mapper Pro window;
           abort if not found.
        2. Send Ctrl+S to it, saving its current workspace to disk
           (see _save_user_workspace() above) -- the only visible
           interaction with the user's own window.
        3. Look up that workspace's own .gmw path via
           utils.last_directory.get_last_directory("gm_workspace");
           abort if none is on record (see module docstring's own
           known limitation section for when this can be stale/wrong,
           and MAIN.py's own orchestration function for how an abort
           here is surfaced to the user).
        4. Write the fixed GMS script (see _write_script_file() above),
           pointing EMBED_SCRIPT at that workspace path and SET_LOG_
           FILE at a fixed output path.
        5. Launch `gm_exe_path <script_path>` as a SEPARATE, headless
           subprocess (subprocess.run(), capturing nothing from stdout/
           stderr since this module only cares about the log file the
           script itself writes) and wait for it to exit, up to
           _HEADLESS_PROCESS_TIMEOUT_SECONDS.
        6. Read back the output file (see _parse_loaded_layers()
           above) and return the parsed layer descriptions.

    Args:
        temp_dir_root: MAIN.py's own existing TEMP_DIR value (r"C:\\
            Global Mapper Temp"), passed in explicitly by the caller.
            This module never imports or hardcodes this path itself.
        gm_exe_path: full path to global_mapper.exe on this machine
            (e.g. r"C:\\Program Files\\GlobalMapper26.0_64bit\\
            global_mapper.exe"), passed in explicitly by the caller.
            This module does not discover or hardcode this path --
            confirming exactly where MAIN.py's new orchestration
            function sources this value from (a new constant, an
            existing one already used elsewhere for build/install
            purposes, or a lookup via the Windows registry/install
            location) is listed as an open item for that file's own
            Phase 2 delivery.
        log_fn: optional callable(str), default None. Called at each
            major step with a short diagnostic message, mirroring
            MAIN.py's own _log()/_dump_windows() diagnostic style for
            its two existing GM-automation entry points (see module
            docstring for why this function cannot call those
            directly). When None (the default), nothing is called.

    Returns:
        tuple[bool, list[str] | None, str | None]:
            (True, layer_descriptions, None) on success -- the layer
            descriptions list from _parse_loaded_layers() (possibly
            empty; an empty workspace is a valid result, not a
            failure).
            (False, None, error_message) on any failure -- error_
            message is a short, plain, non-technical string safe to
            show directly in a messagebox, matching this codebase's
            own established non-technical-error-message convention.
            Per Instructions E.9, the caller (MAIN.py's new
            orchestration function) MUST treat a False result as "the
            loaded-layer list is unknown" and abort the LOAD TABLE
            operation entirely.

    NOT YET ON-MACHINE TESTED AS A SINGLE, UNBROKEN PYTHON FUNCTION
    CALL (Instructions E.11/G.4 -- this sandbox cannot run Global
    Mapper): every individual piece of this mechanism (the Ctrl+S save
    behavior and its necessity, the exact GMS script content and its
    confirmed output format, the subprocess launch and its confirmed
    ~8-10 second runtime and clean exit, the "likely file corruption"
    non-fatal log line) was independently confirmed hands-on, manually
    or via a standalone .gms/command-line test -- but this exact
    function, calling subprocess.run() itself rather than the
    developer typing the command line by hand, has not yet been run.
    See this file's own Phase 2 sign-off for the complete breakdown.
    """
    if log_fn is not None:
        log_fn("gm_script_runner: starting run_and_get_loaded_layers()")

    gm_window = _find_gm_window()
    if gm_window is None:
        if log_fn is not None:
            log_fn("gm_script_runner: ABORT - Global Mapper window not found")
        return False, None, "Global Mapper window not found."

    _save_user_workspace(gm_window, log_fn=log_fn)

    workspace_path = get_last_directory("gm_workspace")
    if not workspace_path:
        if log_fn is not None:
            log_fn("gm_script_runner: ABORT - no recorded Global Mapper "
                    "workspace path")
        return False, None, "Could not determine the current Global Mapper workspace."
    if log_fn is not None:
        log_fn(f"gm_script_runner: using workspace path '{workspace_path}'")

    output_path = _output_file_path(temp_dir_root)
    try:
        script_path = _write_script_file(temp_dir_root, workspace_path, output_path)
    except Exception as e:
        if log_fn is not None:
            log_fn(f"gm_script_runner: ABORT - could not write script file: "
                    f"{type(e).__name__}: {e}")
        return False, None, "Could not prepare the Global Mapper script file."
    if log_fn is not None:
        log_fn(f"gm_script_runner: wrote script to {script_path}")

    try:
        # NOTE on result.returncode: confirmed, hands-on, that Global
        # Mapper's own exit code for this exact headless script,
        # against this exact workspace, is NOT consistent between runs
        # -- two consecutive runs with IDENTICAL input (same .gms
        # script, same .gmw workspace, same "likely file corruption"
        # non-fatal log line present in the output both times) and
        # IDENTICAL, correct output (6 correctly parsed layers both
        # times) returned exit code 1 on one run and 0 on the next.
        # The exit code is therefore DELIBERATELY not checked here or
        # anywhere in this function as a success/failure signal --
        # only the output file's own content (via _parse_loaded_
        # layers() below) determines success. A future reader tempted
        # to add `if result.returncode != 0: return False, ...` should
        # not -- that would intermittently and incorrectly fail runs
        # that in fact produced a complete, correct result.
        result = subprocess.run(
            [gm_exe_path, script_path],
            timeout=_HEADLESS_PROCESS_TIMEOUT_SECONDS,
            capture_output=True,
        )
        if log_fn is not None:
            log_fn(f"gm_script_runner: headless process exited with code "
                    f"{result.returncode} (not used as a success/failure "
                    f"signal -- see this block's own comment)")
    except subprocess.TimeoutExpired:
        if log_fn is not None:
            log_fn(f"gm_script_runner: ABORT - headless process did not exit "
                    f"within {_HEADLESS_PROCESS_TIMEOUT_SECONDS}s")
        return False, None, "Global Mapper did not respond in time."
    except Exception as e:
        if log_fn is not None:
            log_fn(f"gm_script_runner: ABORT - could not launch headless "
                    f"process: {type(e).__name__}: {e}")
        return False, None, "Could not run the Global Mapper background check."

    layer_descriptions = _parse_loaded_layers(output_path, log_fn=log_fn)

    if layer_descriptions is None:
        return False, None, "Could not read the list of loaded layers from Global Mapper."

    if log_fn is not None:
        log_fn(f"gm_script_runner: found {len(layer_descriptions)} loaded layer(s)")

    return True, layer_descriptions, None


def is_table_already_loaded(schema, table, layer_descriptions):
    """
    Returns True if `schema`.`table` appears to already be loaded among
    `layer_descriptions` (run_and_get_loaded_layers()'s own returned
    list), False otherwise.

    MATCH STRATEGY (revised from this task's own original I6 proposal --
    see this file's own Phase 2 sign-off for why): a PREFIX match
    against "{schema}.{table} (" -- NOT a full, exact match against
    "{schema}.{table} ({connection_name})". This module deliberately
    does not require or accept a connection_name argument, because
    nothing in this codebase (confirmed by a full search across the
    project's own files) records what connection name Global Mapper
    itself assigned when a table was first loaded via its own File ->
    Open Spatial Database dialog -- that name is set by the END USER,
    inside Global Mapper's own UI, independent of pg_credentials.json
    or any of the six Database Management dialog fields, and this
    codebase has no existing mechanism that would let MAIN.py's own
    orchestration function supply it.

    Format confirmed, hands-on, against the developer's own Global
    Mapper Pro installation, across multiple separate real workspaces:
    a database-sourced layer's own %LAYER_DESC% is always
    "{schema}.{table} ({connection_name})" -- schema and table joined
    by a literal ".", then a single literal space, then an opening
    parenthesis before the connection name. The prefix this function
    checks for, "{schema}.{table} (", is exactly the portion of that
    format that does NOT depend on knowing connection_name, while still
    being specific enough that a plain, non-database local-file layer
    (e.g. "LandParcel", confirmed, hands-on, to carry no parenthetical
    suffix of any kind) can never match it by accident -- the literal
    ".", space, and opening "(" together are not characters that would
    appear by coincidence in an unrelated local layer's own bare name.

    ACCEPTED, DELIBERATE TRADE-OFF: this prefix match would treat two
    DIFFERENT PostGIS connections that happen to share the exact same
    schema name and table name as "the same, already loaded" table,
    even if the user intended to load the one from the OTHER
    connection. This is considered an acceptable risk for this task's
    scope -- it requires two separate database servers/connections
    with an identically-named schema AND table to coexist in the same
    Global Mapper workspace, which is not a configuration this
    application's own Database Management dialog (one connection
    configured at a time, stored in a single pg_credentials.json) makes
    easy to create by accident. The worst outcome if it ever does occur
    is a single incorrect "already loaded, not reloading" skip for that
    one table -- not a crash, not data loss, and not silently loading
    into the wrong place.

    Args:
        schema: str, the schema name as entered/selected in the
            Database Management dialog's own Schema field.
        table: str, the table name the user selected in the new table
            list.
        layer_descriptions: list[str], run_and_get_loaded_layers()'s
            own returned layer_descriptions.

    Returns:
        bool: True if any entry in layer_descriptions starts with
        "{schema}.{table} (", False otherwise (including when
        layer_descriptions is empty).
    """
    prefix = f"{schema}.{table} ("
    return any(desc.startswith(prefix) for desc in layer_descriptions)