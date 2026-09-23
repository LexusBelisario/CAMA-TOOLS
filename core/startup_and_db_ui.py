"""
core/startup_and_db_ui.py

PURPOSE:
    Replaces the two old, separate startup steps -- startup_sequence()'s
    native .gmw file picker and show_login_and_connect()'s standalone
    login Toplevel -- with a startup dialog that only picks a Global
    Mapper Workspace file (see show_startup_dialog()'s own docstring):
    no credentials are collected at startup at all anymore. The
    session's database connection is configured ENTIRELY through the
    mid-session Configure Database dialog (show_configure_db_dialog()),
    reachable at any time via the runtime "Configure\\nDatabase" hub
    button -- the ONLY way to establish or change the DB connection for
    the rest of the CAMA Tools session.

    Also owns the DB-gate that disables/enables the Update Map /
    Update Database buttons (and the hub button's own color) based on
    current DB state. Deliberately does NOT gray or otherwise touch the
    Feature Management Tools icon grid itself -- whether a tool can be
    opened is governed exclusively by core/tool_exclusivity.py's own
    "one tool at a time" mechanism, independent of database
    connectivity (see DBGate's own class docstring below). This module
    DOES, however, now receive two core/tool_exclusivity.py callables
    from MAIN.py -- activate_manual_fn/deactivate_all_fn -- purely to
    pass along to show_configure_db_dialog()'s caller-managed busy
    period (see that function's own docstring); this module still never
    imports core.tool_exclusivity itself, matching is_any_tool_active's
    existing pass-through pattern below.

    Same architectural role as core/tool_exclusivity.py and
    core/window_management.py (see those modules' own docstrings for
    the precedent this follows): a focused, self-contained module the
    MAIN.py launcher boundary calls into, not a general-purpose shared
    utility other tool files import. This module does NOT import from
    MAIN.py -- every piece of MAIN.py state it needs (the Tk root,
    update_btn, update_map_btn, apply_icon(), get_credentials_path(),
    is_any_tool_active(), the automation-busy flag) is passed in
    explicitly by the caller. This module never imports
    core.tool_exclusivity itself either -- is_any_tool_active (and, as
    of this task, activate_manual/deactivate_all) are passed through as
    plain callables by MAIN.py, keeping this a true leaf module.

DB STATE MACHINE (two states -- identical rules used by BOTH the
startup dialog, which no longer has any credential fields to apply them
to, and the mid-session Configure Database dialog, which is now the
ONLY place they apply; both routes -- for as long as there were two --
went through the SAME _attempt_test_connection()/_bind_edit_invalidation()
pair below specifically so they could not drift apart, and that shared
plumbing remains even with a single caller now):

    VERIFIED   -- the credentials currently loaded in the Configure
                  Database dialog's six fields are EXACTLY the ones a
                  psycopg2.connect() via Test Connection most recently
                  succeeded against, AND no field has been edited
                  since, AND that success has since been explicitly
                  committed via CHANGE CONNECTION (see
                  show_configure_db_dialog()'s own docstring -- Test
                  Connection itself is now a pure probe with zero
                  effect on this state; only a CHANGE CONNECTION commit
                  ever sets VERIFIED). This is the ONLY state in which
                  the DB-gate is released.
    UNVERIFIED -- no currently-verified session configuration -- this
                  is NOT a claim about live database connectivity, only
                  about whether the CURRENT session credentials were
                  ever committed via CHANGE CONNECTION. Every session
                  starts, and stays, UNVERIFIED until the first
                  successful CHANGE CONNECTION commit -- there is no
                  longer a startup-time credential path that could
                  reach VERIFIED before that.

    An earlier version of this module tracked a third state, to
    distinguish "explicitly chose to skip" from "never tried" for a
    Yes/No message the OLD startup dialog used to show when credentials
    were only partially filled in. That distinction had nowhere left to
    apply once the startup dialog stopped collecting credentials at
    all, so that third state was removed entirely -- every session
    simply starts UNVERIFIED now, the same way that removed state
    always behaved for gating purposes anyway.

    Editing ANY of the six credential fields in the Configure Database
    dialog always collapses the current state to UNVERIFIED. This is
    implemented as a single eager write (_db_state[0] = "UNVERIFIED")
    the instant a field changes, so every reader sees an always-current
    flag rather than needing to re-diff live field values against a
    snapshot at read time -- there is exactly one flag, mutated
    eagerly, never lazily recomputed.

    VERIFIED is a statement about the last explicit commit, never a
    live guarantee of current connectivity. Busy gating (see DBGate
    below) prevents user-initiated DB reconfiguration during an active
    operation; it does NOT guarantee database connectivity. Existing
    tool/automation error handling remains authoritative for runtime
    DB failures encountered after this module has already handed off
    to VERIFIED -- this module has no involvement in that.

MUTABLE STATE (single owner: this file; nothing outside it reads or
writes these):
    _db_state = ["UNVERIFIED"]   -- one of "VERIFIED" / "UNVERIFIED".
                                     See state machine above.
    _verified_fields = [None]    -- snapshot dict of the six field
                                     values committed by the most recent
                                     successful CHANGE CONNECTION, or
                                     None if never verified this
                                     session. Written alongside
                                     _db_state[0] = "VERIFIED" at commit
                                     time (see show_configure_db_dialog()'s
                                     own docstring); not read on any hot
                                     path, kept for diagnostics/equality
                                     checks the same way it always was.

DBGate CONTRACT (exposed via create_hub_button()'s return value):
    set_db_connected(is_connected: bool) -- the ONE state-transition
        entry point for UI gating. Called from exactly one place: the
        Configure-DB dialog's CHANGE CONNECTION commit handler, always
        with True (there is no longer a Test-Connection-success call
        site -- Test Connection has zero effect on _db_state, see the
        state machine above -- and the field-edit listener's own
        UNVERIFIED collapse is picked up the next time anything calls
        refresh(), rather than needing its own set_db_connected(False)
        call). It is deliberately NOT a general "the database is
        currently reachable" signal -- runtime connectivity failures
        during a tool run or Update automation are NOT reported through
        this method, and this module makes no attempt to detect or
        react to them; that remains entirely the responsibility of the
        existing tool/automation error handling described in the state
        machine section above. Internally, set_db_connected() does not
        independently decide gate state -- it simply ensures the
        underlying _db_state reflects the transition already performed
        by the caller (see call sites below) and then calls refresh().
    refresh() -- reconciles the Update Map / Update Database buttons'
        AND the hub button's own color/state against CURRENT
        _db_state[0] (read fresh every call, never cached) and current
        busy state (via the is_any_tool_active_fn / is_automation_busy_fn
        callables supplied at construction, also queried fresh every
        call, never cached -- is_any_tool_active_fn reports True while
        EITHER a Feature Management Tool OR a Configure Database
        session is active, per core/tool_exclusivity.py's own
        activate_manual() addition; this class needs no changes to pick
        that up, since it only ever calls the callable). Never mutates
        _db_state itself. This is the ONE place that computes
        gated_controls_enabled = (_db_state[0] == "VERIFIED") AND NOT
        busy.
    apply_gate()/release_gate() -- private UI-mutation primitives
        (_apply_gate/_release_gate below), NOT independent state
        authorities. They are only ever invoked from inside refresh(),
        never called directly by anything outside this class. Both are
        idempotent (safe to call when already in the target state).

    DBGate does NOT touch the Feature Management Tools icon grid at
    all, in any state -- see DBGate's own class docstring. Whether a
    tool can be opened is governed exclusively by
    core/tool_exclusivity.py's own "one tool at a time" mechanism,
    completely independent of database connectivity. is_any_tool_active_fn
    is used here only as one input to the BUSY computation for the
    Update buttons/hub (an Update-button action and a Feature
    Management Tool run -- or, as of this task, a Configure Database
    session -- are still mutually exclusive with each other, same as
    before: Update Map/Database, the Feature Management Tools icon
    grid, and the hub button itself all stay grayed for a Configure
    Database session's ENTIRE lifetime, ungraying only once that
    dialog actually closes -- see show_configure_db_dialog()'s own
    docstring for what happens at that point, depending on whether a
    CHANGE CONNECTION inside it was ultimately committed), never to
    guard a grid-icon touch, since there is none left to guard.

    Busy is intentionally NOT folded into "is DB access permitted" as
    a separate caller-side formula -- refresh() computes it internally
    so every caller gets the correct combination automatically:
        gated_controls_enabled = (_db_state[0] == "VERIFIED") and not busy
        hub_clickable          = not busy   (independent of DB state --
                                              the hub is gated because
                                              an operation is in
                                              progress, never because
                                              the database happens to
                                              be disconnected; it must
                                              always remain the way OUT
                                              of a disconnected state.
                                              See create_hub_button()'s
                                              own docstring for how a
                                              disconnected-but-not-busy
                                              hub button stays CLICKABLE
                                              even though it is colored
                                              the same disabled-gray a
                                              busy button is.)

DEPENDENCIES:
    stdlib: tkinter (Toplevel/Frame/Label/Entry/Button/messagebox), os,
    threading (only to type-check nothing -- actual thread creation for
    the workspace-picker resize hook is done by the resize_file_dialog_fn
    callable MAIN.py supplies, not by this module).
    third-party: psycopg2 (connection test).
    local: none. This module is deliberately leaf-level, matching
    core/tool_exclusivity.py's own DEPENDENCIES section. It no longer
    depends on PIL -- the hub was an icon-swapping Canvas in an earlier
    version of this module; it is now a plain, color-driven tk.Button
    (see create_hub_button()), which needs no image assets at all.

SCOPE NOTE (this file only): this module does not touch MAIN.py or
core/tool_exclusivity.py. Wiring show_startup_dialog(), create_hub_button(),
and show_configure_db_dialog() into the actual application -- replacing
startup_sequence()/show_login_and_connect(), inserting the hub button
into the panel layout, wrapping the Update buttons' command= bindings,
and bracketing show_configure_db_dialog()'s own call with
core/tool_exclusivity.py's activate_manual()/deactivate_all() -- is
explicitly deferred to the MAIN.py edit step of this task, not part of
this file.
"""

from tkinter import (
    Toplevel, Frame, Label, Entry, Button,
    messagebox, TclError,
)
import sys
import threading

import psycopg2


# ============================================================
# MUTABLE STATE (see module docstring -- single owner: this file)
# ============================================================
_db_state = ["UNVERIFIED"]
_verified_fields = [None]

_FIELD_LABELS = [
    ("host", "Host:"),
    ("port", "Port:"),
    ("database", "Database:"),
    ("schema", "Schema:"),
    ("username", "Username:"),
    ("password", "Password:"),
]


# ============================================================
# SHARED CREDENTIAL FIELD BLOCK
# ============================================================
def _build_credential_fields(parent, saved, start_row=0):
    """
    Builds the six Host/Port/Database/Schema/Username/Password
    Label+Entry rows inside `parent`, starting at grid row start_row,
    pre-filled from `saved` (a dict with the same keys, or {} / missing
    keys tolerated -- falls back to "" for anything not present).
    Password field uses show="*".

    Returns:
        dict: field key ("host", "port", ...) -> the Entry widget.

    Does NOT bind any edit listener -- callers bind their own via
    _bind_edit_invalidation() below, since the startup dialog and the
    Configure-DB dialog react to an edit slightly differently in what
    else runs alongside the shared _db_state collapse (the Configure-DB
    dialog also has a live DBGate to notify; the startup dialog does
    not, since no CAMA Tools panel/gate exists yet at that point).
    """
    entries = {}
    for i, (key, label_text) in enumerate(_FIELD_LABELS):
        row = start_row + i
        Label(parent, text=label_text).grid(row=row, column=0, sticky="e", padx=5, pady=3)
        show_char = "*" if key == "password" else None
        entry = Entry(parent, width=25, show=show_char) if show_char else Entry(parent, width=25)
        entry.grid(row=row, column=1)
        entry.insert(0, saved.get(key, "") if saved else "")
        entries[key] = entry
    return entries


def _read_fields(field_entries):
    """Returns the current live values of the six fields as a plain
    dict, keyed the same as _build_credential_fields()."""
    return {key: entry.get() for key, entry in field_entries.items()}


def _bind_edit_invalidation(field_entries, extra_on_edit=None):
    """
    Binds <KeyRelease> on every field in field_entries so that any edit
    eagerly collapses _db_state[0] to "UNVERIFIED" (see module
    docstring, DB STATE MACHINE -- this is the single eager-write
    mechanism; nothing else ever mutates _db_state on an edit).

    extra_on_edit, if given, is called (no args) after the state
    collapse on every edit -- used by the Configure-DB dialog to also
    call db_gate.set_db_connected(False) so the outer panel re-gates
    immediately. The startup dialog passes None here, since there is
    no live DBGate yet at startup time.
    """
    def _on_edit(event=None):
        _db_state[0] = "UNVERIFIED"
        if extra_on_edit is not None:
            extra_on_edit()

    for entry in field_entries.values():
        entry.bind("<KeyRelease>", _on_edit)


# ============================================================
# SHARED TEST CONNECTION LOGIC
# ============================================================
def _friendly_connection_error_message(e):
    """
    Translates a psycopg2 connection exception into a short,
    non-technical message for the Test Connection failure dialog --
    called from _attempt_test_connection()'s except block below.

    psycopg2 does not expose a structured, reliable error-code
    attribute for every failure mode a plain psycopg2.connect() call
    can raise (unlike, say, a dedicated SQLSTATE lookup for a query
    error against an already-open connection), so this matches on the
    exception's own message text -- the same approach any
    non-technical-facing wrapper around a driver-level exception has
    to take when the driver's own message is the only signal
    available. Matching is case-insensitive and checks for a handful
    of substrings each candidate psycopg2/libpq message is known to
    contain, rather than an exact string match, since the exact
    wording can vary slightly (e.g. by libpq version).

    Falls back to a single generic message (see the final return
    below) for any error that matches none of the specific patterns --
    psycopg2 can raise for many reasons this function does not attempt
    to enumerate exhaustively (SSL certificate problems, server-side
    resource exhaustion, protocol/version mismatches, and so on); the
    fallback is written to remain accurate and non-alarming regardless
    of the real underlying cause, rather than guessing at one.

    Args:
        e: the caught Exception from psycopg2.connect().

    Returns:
        str: a short, user-facing message with no driver-level
        wording, port numbers, or hex error codes.
    """
    text = str(e).lower()

    if "password authentication failed" in text or "authentication failed" in text:
        return "The username or password was incorrect."
    if "does not exist" in text:
        return "The database name could not be found on the server."
    if "timeout" in text or "timed out" in text:
        return ("Could not reach the database server. Please check the "
                "host address and your network connection, then try again.")
    if "connection refused" in text:
        return "The server refused the connection. Please check the host and port."

    return ("Could not connect to the database. Please check your "
            "connection details and try again.\n\n"
            "If the problem continues, contact your system administrator.")


def _attempt_test_connection(field_entries, get_credentials_path_fn):
    """
    Reads the current live field values and attempts psycopg2.connect()
    ONLY -- this function is a pure connectivity probe with NO side
    effects on session state (does NOT touch _db_state or
    _verified_fields, does NOT write pg_credentials.json). Committing a
    successful result into the session (marking VERIFIED, persisting
    pg_credentials.json) is the separate responsibility of
    _run_test_connection_threaded()'s own _finish() callback -- see
    that function's own docstring for exactly why the commit step is
    there and not here: this function runs on a background thread (see
    _run_test_connection_threaded()'s docstring), and a Start-button
    cancellation (see that function's own cancel()) needs a single
    point, on the MAIN thread, after which it is still possible to
    decide "this result never happened" -- if this function itself
    already mutated _db_state/wrote pg_credentials.json the moment
    psycopg2.connect() succeeded, a cancellation arriving after that
    point could stop the UI from reflecting VERIFIED, but the session
    state and the on-disk credentials file would already have silently
    become VERIFIED anyway, which is exactly the inconsistency this
    split avoids: a cancelled attempt must leave EVERY observable trace
    of the session (not just the dialog's own widgets) as if the
    attempt had not completed at all.

    Does NOT show any messagebox itself, and does NOT touch any
    Tkinter widget -- this function is called from
    _run_test_connection_threaded()'s background thread (see that
    function's own docstring), and Tkinter calls are only safe from
    the main thread. The error message is returned as plain data;
    _run_test_connection_threaded()'s _finish() callback (which runs
    back on the main thread via root.after(0, ...)) is what actually
    calls messagebox.showerror() with it, strictly AFTER the spinner
    has already been stopped -- this ordering is what fixes the
    earlier bug where the spinner kept animating underneath the
    (blocking, modal) error dialog: showing that dialog from directly
    inside this function, while still running on the background
    thread, meant the code path that stops the spinner (back in
    _finish()) could not run until AFTER the user dismissed the
    dialog, since this function's own return was what _finish() was
    waiting on.

    Args:
        field_entries: dict from _build_credential_fields()/_read_fields().
        get_credentials_path_fn: callable, no args, returns the
            pg_credentials.json path (MAIN.py passes its existing
            get_credentials_path, imported from utils.db_discovery).
            Accepted but genuinely UNUSED by this function's own body --
            this connectivity probe never reads or writes
            pg_credentials.json at all (see this function's own Returns:
            section, and _run_test_connection_threaded()'s docstring for
            why: as of this task, nothing about Test Connection touches
            the credentials file on either outcome). Kept in the
            signature purely so the call chain
            (_run_test_connection_threaded() -> _attempt_test_connection())
            stays a simple straight pass-through of the same arguments
            both functions already accept, rather than branching their
            signatures apart for one now-unused parameter.

    Returns:
        tuple[bool, str | None, dict]: (True, None, values) on a
        successful connection test; (False, message, values) on
        failure, where message is the short, non-technical string from
        _friendly_connection_error_message(). values is always the
        exact field-values dict this function read and tested against
        -- _finish() passes it straight through to on_result() (only if
        not cancelled) so the caller has the EXACT values this specific
        test actually ran against, without a second, possibly-stale
        field read. As of this task, _finish() itself no longer commits
        anything into session state on success -- see
        _run_test_connection_threaded()'s own docstring for why Test
        Connection is now a pure probe in every caller, not just under
        cancellation.
    """
    values = _read_fields(field_entries)

    try:
        conn = psycopg2.connect(
            host=values["host"],
            port=values["port"],
            database=values["database"],
            user=values["username"],
            password=values["password"],
            connect_timeout=60,
        )
        conn.close()
    except Exception as e:
        return False, _friendly_connection_error_message(e), values

    return True, None, values


def test_live_connection(host, port, database, username, password):
    """
    Public, thin wrapper around the SAME connectivity probe
    _attempt_test_connection() above performs -- exists for callers
    OUTSIDE this module that need to test a connection given plain
    values (not a field_entries dict from one of this module's own
    dialogs). Currently used by MAIN.py's own
    update_map_and_select_recorded() / update_database_from_geopackage(),
    which pre-flight-check the LIVE session's already-stored
    credentials (stored_username/stored_password/DB_HOST/DB_PORT/
    DB_NAME) before attempting their own real work, using this
    function so their failure message uses the exact same short,
    non-technical wording _friendly_connection_error_message() already
    gives the Configure Database dialog's own Test Connection --
    rather than MAIN.py duplicating that translation logic itself, or
    (as confirmed happening before this function existed) letting the
    raw driver-level exception reach the user unfiltered.

    A pure probe, same as _attempt_test_connection() -- no side
    effects: does NOT touch _db_state, _verified_fields, or
    pg_credentials.json, does not know or care who is calling it or
    why. Runs on WHATEVER thread calls it -- no threading of its own;
    a caller that does not want to block its own UI while this runs
    (psycopg2.connect() can take up to connect_timeout=60 seconds) is
    responsible for calling this from a background thread itself, the
    same way _run_test_connection_threaded() already does for this
    module's own two dialogs.

    Args:
        host, port, database, username, password: plain strings.

    Returns:
        tuple[bool, str | None]: (True, None) on a successful
        connection test; (False, message) on failure, where message is
        the short, non-technical string from
        _friendly_connection_error_message().
    """
    try:
        conn = psycopg2.connect(
            host=host, port=port, database=database,
            user=username, password=password,
            connect_timeout=60,
        )
        conn.close()
    except Exception as e:
        return False, _friendly_connection_error_message(e)

    return True, None


# ============================================================
# THREADED TEST CONNECTION + SPINNER (shared by both dialogs)
# ============================================================
_SPINNER_FRAMES = ["\u25d0", "\u25d3", "\u25d1", "\u25d2"]  # ◐ ◓ ◑ ◒
_SPINNER_INTERVAL_MS = 120


def _run_test_connection_threaded(field_entries, get_credentials_path_fn,
                                   root, status_label, on_result):
    """
    Runs _attempt_test_connection() on a background thread, so the
    blocking psycopg2.connect() call (up to connect_timeout=60 seconds
    -- see _attempt_test_connection()) never freezes the dialog's Tk
    event loop. This is the ONE call site that spawns that background
    thread. As of this task, show_configure_db_dialog() is this
    function's only caller -- the startup dialog no longer has a Test
    Connection button at all (see show_startup_dialog()'s own
    docstring) -- but this function itself stays generic rather than
    being folded into that one caller, matching the rest of this file's
    "shared plumbing, even with one current consumer" pattern (see
    _attempt_test_connection()'s own docstring).

    While the background thread is running, status_label cycles
    through _SPINNER_FRAMES (a small rotating glyph, no image asset
    needed) via repeated root.after(...) reschedules -- animating a
    Tkinter widget is only ever safe from the main thread, so the
    spinner's own animation loop stays entirely on the main thread;
    only the actual psycopg2.connect() call runs on the background
    thread.

    status_label is the SAME single widget both the spinner and the
    eventual \u2713/\u2717 result are shown in -- not two separate labels --
    so there is never a fixed-width gap between them (a spinner label
    and a result label side by side, each reserving their own width,
    is what previously pushed the \u2717 visibly away from the Test
    Connection button; one shared label removes that gap entirely).

    Tkinter widgets (status_label, and anything on_result touches)
    must never be touched directly from the background thread -- the
    background thread's only job is to call _attempt_test_connection()
    and capture its bool result; the moment it finishes, the actual UI
    update (stopping the spinner, calling on_result) is handed back to
    the main thread via root.after(0, ...), which is the standard safe
    way to get a background thread's result back onto a Tk widget.

    Args:
        field_entries: dict from _build_credential_fields()/_read_fields(),
            passed straight through to _attempt_test_connection().
        get_credentials_path_fn: passed straight through.
        root: the Tk root -- used only for its thread-safe .after()
            scheduling method (this function never touches root's own
            widgets).
        status_label: the single Label this function owns exclusively
            while the test is running -- animates it with the rotating
            glyph, then clears it back to "" the moment the result is
            back (on_result is responsible for what it shows AFTER
            that, such as a \u2713/\u2717 result glyph, in this SAME label --
            this function's own job ends at clearing it).
        on_result: callable(ok: bool, error_message: str | None,
            values: dict), invoked on the main thread once the
            background attempt finishes, AFTER the spinner has already
            been stopped and cleared. error_message is None on success,
            or the short, non-technical string from
            _friendly_connection_error_message() on failure. values is
            the exact field-values dict _attempt_test_connection() read
            and tested against (see that function's own Returns:
            section) -- the caller uses it to remember what was tested,
            e.g. so a later CHANGE CONNECTION commit can reuse it
            instead of re-reading the fields. This is where the
            caller's own success/failure UI (status_label's own
            \u2713/\u2717 text, showing the failure messagebox, etc.) lives --
            Test Connection itself has zero effect on _db_state or
            pg_credentials.json on either outcome; only an explicit
            CHANGE CONNECTION commit (see show_configure_db_dialog()'s
            own docstring) ever changes session state. Showing the
            failure messagebox from inside on_result (main
            thread, after the spinner is already stopped) rather than
            from inside _attempt_test_connection() itself (which used
            to call messagebox.showerror() directly, from the
            background thread) is what fixes the spinner-still-
            animating-behind-the-error-dialog bug: previously, that
            call blocked _attempt_test_connection()'s own return until
            the user dismissed the dialog, which in turn blocked
            _finish() below (and therefore the spinner-stopping code)
            from ever running until after the dialog was already
            closed.

    Widget-destroyed race (confirmed via an actual on-machine crash log
    against an earlier version of this module, when the startup dialog
    still had its own Test Connection button): a background attempt
    finishing at the same moment its owning dialog is being destroyed
    some other way can leave root.after(0, _finish) -- scheduled from
    the background thread's _worker(), and therefore always racing
    against whatever the main thread does in the meantime -- firing
    against widgets that are already gone. As of this task, the
    Configure Database dialog's own X-close handler cancels an
    in-flight test BEFORE destroying the Toplevel (the same mechanism
    the old startup dialog's Start button used -- see cancel()'s own
    docstring below), which closes off the specific sequence the
    original crash log showed; the guards below remain regardless, as a
    second line of defense against any other path that might destroy
    the dialog while a test is still in flight. When this race does
    occur, status_label (and any widget on_result would touch) no
    longer exists as a live Tk widget, and
    .config(...) on it raises _tkinter.TclError ("invalid command
    name ..."), an unhandled exception surfacing as a Tkinter callback
    traceback. _animate() and _finish() below both guard every
    .config() call (and _finish()'s on_result(...) call, which may
    itself touch other now-gone widgets) with a narrow try/except
    TclError specifically for this: if the widget is already gone,
    there is nothing left to update and nothing further to do, so the
    exception is swallowed and the function simply returns rather than
    letting a stale, already-superseded UI update crash into the
    (harmless, but noisy and alarming) traceback the log showed. This
    is not a general "swallow all Tkinter errors" catch -- only
    TclError, and only around the specific calls that touch a widget
    which may have been destroyed by a legitimate, expected race
    (Start being clicked while a background verification is still
    in flight), not a sign of some other bug being hidden.

    Returns:
        callable: a cancel() function, taking no arguments. Calling it
        (from the main thread) immediately stops the spinner, shows a
        \u2717 in status_label, and marks this attempt as cancelled --
        when the background thread eventually finishes (it cannot
        actually be interrupted mid-psycopg2.connect(); it keeps
        running silently in the background until it returns or times
        out), its result is discarded and on_result is NEVER called
        for this attempt. This is what lets the Configure Database
        dialog's own X-close handler treat a still-in-flight Test
        Connection as an immediate, silent "abandon this attempt" the
        instant the dialog is closing -- the user does not wait for the
        background attempt to finish, and even if that attempt WOULD
        have succeeded, no on_result callback ever fires against a
        Toplevel that is (or is about to be) destroyed. Safe to call
        after the attempt has already finished on its own (a no-op:
        _finish() already ran, stop_spinner is already True, and
        on_result was already called once for this attempt -- calling
        cancel() at that point only redundantly re-sets status_label's
        text, which is harmless).
    """
    stop_spinner = {"flag": False}
    cancelled = {"flag": False}
    frame_index = {"i": 0}

    def _animate():
        if stop_spinner["flag"]:
            return
        try:
            status_label.config(text=_SPINNER_FRAMES[frame_index["i"] % len(_SPINNER_FRAMES)])
        except TclError:
            # status_label's window was destroyed (see this function's
            # own docstring, Widget-destroyed race) -- nothing left to
            # animate; stop rescheduling rather than continuing to
            # fire root.after() calls against a widget that no longer
            # exists.
            return
        frame_index["i"] += 1
        root.after(_SPINNER_INTERVAL_MS, _animate)

    def _worker():
        ok, error_message, values = _attempt_test_connection(field_entries, get_credentials_path_fn)

        def _finish():
            if cancelled["flag"]:
                # cancel() already ran (see its own docstring above) --
                # this attempt's result, whatever it is, is discarded.
                # on_result must NEVER be called for a cancelled attempt
                # either, since the caller has already moved on (e.g.
                # the Configure Database dialog is already destroyed)
                # and calling it now would touch widgets that may no
                # longer exist.
                return

            # No session-state commit happens here, on success or on
            # failure -- Test Connection is a pure probe with zero
            # effect on _db_state, _verified_fields, or
            # pg_credentials.json in every caller (see this function's
            # own docstring, and _attempt_test_connection()'s). The
            # ONLY place a successful test is ever turned into a
            # committed, persisted connection is the Configure Database
            # dialog's own CHANGE CONNECTION handler -- see
            # show_configure_db_dialog()'s docstring.
            stop_spinner["flag"] = True
            try:
                status_label.config(text="")
            except TclError:
                # See this function's own docstring, Widget-destroyed
                # race -- the dialog (and status_label with it) is
                # already gone. on_result() is also skipped in this
                # case: it exists to update the (now-gone) dialog's own
                # UI, so there is nothing left for it to correctly do.
                return
            on_result(ok, error_message, values)

        root.after(0, _finish)

    def cancel():
        """See this function's own Returns: section above for the full
        contract. Marks this attempt as cancelled (discarding whatever
        result the background thread eventually produces), stops the
        spinner's own reschedule loop, and immediately shows a \u2717 --
        the caller (the Configure Database dialog's own X-close
        handler) treats a still-in-flight Test Connection as
        immediately abandoned the instant this is called, without
        waiting for the background attempt."""
        cancelled["flag"] = True
        stop_spinner["flag"] = True
        try:
            status_label.config(text="\u2717", fg="#b02a2a")
        except TclError:
            # status_label's window is already gone (see this
            # function's own docstring, Widget-destroyed race) --
            # nothing left to update.
            pass

    _animate()
    threading.Thread(target=_worker, daemon=True).start()
    return cancel


# ============================================================
# PRIMARY (GREEN) BUTTON STYLE -- Start button's 4-state visual
# ============================================================
_PRIMARY_BTN_BG = "#2e8b3d"
_PRIMARY_BTN_FG = "#ffffff"
_PRIMARY_BTN_HOVER_BG = "#256e30"
_PRIMARY_BTN_DISABLED_BG = "#b8b8b8"
_PRIMARY_BTN_DISABLED_FG = "#777777"


def _set_primary_button_enabled(button, enabled):
    """
    Sets a "primary" (yellow) Button's Tk state=normal/disabled
    TOGETHER with the bg/fg pair that actually communicates that state
    visually. This is the ONE place that mapping is defined -- every
    caller that enables/disables such a button (currently just the
    Start button's Browse-workspace callback) goes through this
    function rather than calling .config(state=...) directly, so a
    future call site can never forget to also update bg/fg and
    accidentally reintroduce the original bug this fixes: a plain
    tkinter.Button's hardcoded bg does NOT automatically gray out when
    state="disabled" -- Tk only auto-dims the text via
    disabledforeground, leaving a disabled button that still looks
    fully active with its normal bg color showing through. Setting an
    explicit, distinctly gray bg/fg pair for the disabled state (see
    the module-level _PRIMARY_BTN_* constants above) is what actually
    fixes that, not disabledforeground alone.

    Does not touch hover behavior directly -- see
    _bind_primary_button_hover() below, which reads the button's own
    state (via this same state=normal/disabled Tk attribute) at
    <Enter>/<Leave> time rather than tracking "enabled" separately;
    state remains the single source of truth for both this function
    and the hover handler, so they can never disagree with each other.

    Args:
        button: the Button widget (constructed with the
            _PRIMARY_BTN_* colors already, and with
            _bind_primary_button_hover() called on it once at
            construction time -- see that function's own docstring).
        enabled: True to enable it (green, clickable, hover-
            responsive), False to disable it (gray, inert, with a
            "no" cursor when hovered -- Tk's own universal
            circle-with-slash "prohibited" cursor, the standard way to
            signal an inert control on hover, matching the same
            cursor="no" treatment core/tool_exclusivity.py already
            applies to a disabled Feature Management Tools icon).
    """
    if enabled:
        button.config(
            state="normal",
            bg=_PRIMARY_BTN_BG,
            fg=_PRIMARY_BTN_FG,
            cursor="hand2",
        )
    else:
        button.config(
            state="disabled",
            bg=_PRIMARY_BTN_DISABLED_BG,
            fg=_PRIMARY_BTN_DISABLED_FG,
            cursor="no",
        )


def _bind_primary_button_hover(button):
    """
    Binds <Enter>/<Leave> hover highlighting to a "primary" (yellow)
    Button -- called once, right after constructing such a button.

    The hover handlers check the button's OWN current Tk state
    (str(button["state"])) at the moment the mouse enters/leaves,
    rather than tracking "is this button currently enabled" as a
    separate flag -- Tk's own state attribute is kept as the single
    source of truth (matching _set_primary_button_enabled() above), so
    a disabled button can never be made to visually look hoverable/
    active by a stray mouse-over: the check is re-evaluated live on
    every <Enter>, not cached from whenever the button was last
    enabled/disabled.

    Args:
        button: the Button widget to bind hover behavior to. Must
            already have been constructed with the _PRIMARY_BTN_*
            colors (bg/fg matching whichever of
            _set_primary_button_enabled()'s two branches applies to
            its current state).
    """
    def _on_enter(event):
        if str(button["state"]) == "normal":
            button.config(bg=_PRIMARY_BTN_HOVER_BG)

    def _on_leave(event):
        if str(button["state"]) == "normal":
            button.config(bg=_PRIMARY_BTN_BG)

    button.bind("<Enter>", _on_enter)
    button.bind("<Leave>", _on_leave)


# ============================================================
# COLOR HELPER -- shared by the per-button darker-hover fix below
# ============================================================
def _darken_hex_color(hex_color, factor=0.7):
    """
    Returns hex_color scaled toward black by `factor` (each of R/G/B
    multiplied by factor and re-hexed) -- e.g. factor=0.7 keeps 70% of
    each channel's brightness, i.e. "the same color, just noticeably
    darker", which is what this task's hover requirement asks for on
    Update Map / Update Database / the hub button, replacing the
    earlier shared, unrelated hover colors those buttons used to fall
    back to.

    ASSUMPTION (not generally validated -- this is deliberately a
    narrow helper, not a general-purpose color parser): hex_color is
    always a 6-digit "#RRGGBB" string. Every color this helper is ever
    called with in this file is one of this module's or MAIN.py's own
    hardcoded button-color constants (_PRIMARY_BTN_BG, _HUB_BTN_BG,
    MAIN.py's UPDATE_MAP_BTN_NORMAL_BG / UPDATE_DB_BTN_NORMAL_BG) --
    all already known, by inspection, to be exactly this format. An
    assert enforces that assumption explicitly (fails loudly, at the
    call site, if a future caller ever passes something else -- e.g. a
    3-digit shorthand or a named color -- rather than silently
    computing a wrong or garbled color).

    Called ONCE per button, at bind time, against that button's own
    fixed, never-changing "normal" color -- never against a widget's
    CURRENT/live bg (which could itself already be a previously-
    darkened value). This is what guarantees Enter/Leave cannot
    compound: every <Enter> re-applies the SAME pre-computed darkened
    shade, and every <Leave> restores the SAME original normal_bg,
    regardless of how many times the mouse enters/leaves.

    Args:
        hex_color: a "#RRGGBB" string.
        factor: 0..1 -- 1.0 leaves the color unchanged, 0.0 produces
            black. Defaults to 0.7 (30% darker), this task's own
            starting point for "much darker, same color."

    Returns:
        str: a new "#rrggbb" string.
    """
    assert len(hex_color) == 7 and hex_color[0] == "#", (
        f"_darken_hex_color expects a 6-digit '#RRGGBB' string, got {hex_color!r}"
    )
    r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    return f"#{int(r * factor):02x}{int(g * factor):02x}{int(b * factor):02x}"


# ============================================================
# SECONDARY BUTTON STYLE -- Update Map / Update Database buttons'
# gray-when-disabled visual, driven by DBGate. Hover is each button's
# OWN darker shade (see bind_secondary_button_hover() below) -- no
# longer a shared hover color, per this task's own requirement that
# hover communicate "this exact button, but darker", not a generic
# highlight unrelated to the button's own color.
# ============================================================
_SECONDARY_BTN_DISABLED_BG = "#b8b8b8"
_SECONDARY_BTN_DISABLED_FG = "#777777"


def set_secondary_button_enabled(button, enabled, normal_bg, normal_fg="white"):
    """
    Sets a Button's Tk state=normal/disabled TOGETHER with the bg/fg
    pair that actually communicates that state visually -- the
    Update Map / Update Database buttons' equivalent of
    _set_primary_button_enabled() above (see that function's own
    docstring for why bg/fg must be set alongside state=, not left to
    Tk's own disabledforeground-only default).

    Unlike the Start button (a single fixed color), Update Map and
    Update Database keep their own distinct ENABLED colors (green and
    blue respectively, as MAIN.py already defines them) -- normal_bg/
    normal_fg let each button's own call site supply its own enabled
    color while sharing this one function's disabled-state and
    idempotency logic. The DISABLED color is the SAME for both buttons
    regardless of normal_bg (see _SECONDARY_BTN_DISABLED_BG above) --
    both buttons disable to the identical gray. The HOVER color, unlike
    an earlier version of this module, is NOT shared -- see
    bind_secondary_button_hover() below, a separate function (this one
    never touches hover) that computes each button's own darkened
    shade from its own normal_bg.

    This module-level function (not part of the DBGate class) is
    exported (no leading underscore) because MAIN.py's own
    button-construction code calls it directly to set each button's
    initial disabled appearance at construction time, before a DBGate
    even exists yet to call it via refresh() -- see MAIN.py's own
    Update Map/Database Button(...) calls.

    Args:
        button: the Button widget.
        enabled: True for normal_bg/normal_fg + state="normal", False
            for the shared gray disabled colors + state="disabled".
        normal_bg: this button's own enabled-state background (e.g.
            MAIN.py's "#6a9f2f" for Update Map, "#007acc" for Update
            Database).
        normal_fg: this button's own enabled-state foreground.
            Defaults to "white", matching both buttons' existing fg.
    """
    if enabled:
        button.config(state="normal", bg=normal_bg, fg=normal_fg, cursor="hand2")
    else:
        button.config(state="disabled", bg=_SECONDARY_BTN_DISABLED_BG,
                       fg=_SECONDARY_BTN_DISABLED_FG, cursor="no")


def bind_secondary_button_hover(button, normal_bg):
    """
    Binds <Enter>/<Leave> hover highlighting to an Update Map / Update
    Database button -- called once, right after constructing such a
    button (see MAIN.py's own call sites).

    Hover color is THIS button's own normal_bg, darkened (see
    _darken_hex_color() above) -- computed ONCE here, at bind time, and
    closed over by both handlers, rather than derived from the
    button's current/live bg on every <Enter> -- this is what prevents
    repeated Enter/Leave cycles from compounding the darkening (every
    <Enter> re-applies the same fixed hover_bg; every <Leave> restores
    the same fixed normal_bg). An earlier version of this function used
    one shared hover color (_SECONDARY_BTN_HOVER_BG) for both buttons
    regardless of their own color -- replaced per this task's own
    requirement that hover be "the same color, just darker", per
    button.

    Same "state is the single source of truth" principle as
    _bind_primary_button_hover() above: the handlers check the
    button's OWN current Tk state at the moment of the event, so a
    disabled button can never be made to look hoverable by a stray
    mouse-over.

    Args:
        button: the Button widget to bind hover behavior to.
        normal_bg: this button's own enabled-state background -- both
            the color restored on <Leave> and the source color
            _darken_hex_color() is computed from for <Enter>.
    """
    hover_bg = _darken_hex_color(normal_bg)

    def _on_enter(event):
        if str(button["state"]) == "normal":
            button.config(bg=hover_bg)

    def _on_leave(event):
        if str(button["state"]) == "normal":
            button.config(bg=normal_bg)

    button.bind("<Enter>", _on_enter)
    button.bind("<Leave>", _on_leave)


def _load_saved_credentials(get_credentials_path_fn):
    """Loads pg_credentials.json via get_credentials_path_fn(), same
    tolerant pattern as the old show_login_and_connect(): missing file
    or malformed JSON both simply yield {} (empty pre-fill), never an
    exception surfaced to the user at dialog-build time."""
    import json
    import os
    try:
        creds_path = get_credentials_path_fn()
    except Exception:
        return {}
    if not os.path.exists(creds_path):
        return {}
    try:
        with open(creds_path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


# ============================================================
# DB-GATE
# ============================================================
class DBGate:
    """
    Owns the runtime DB-gate: disables/enables the Update Map / Update
    Database buttons, AND drives the hub button's own color/state (via
    hub_set_visual_fn -- see create_hub_button() below), based on
    current DB state and busy state. Does NOT touch the Feature
    Management Tools icon grid at all -- that
    grid's ONLY gating is core/tool_exclusivity.py's own "one tool at
    a time" mutual-exclusivity mechanism (see that module), which is
    unrelated to database connectivity and stays completely
    unaffected by DBGate. Whether the database is connected or not has
    no bearing on whether a Feature Management Tool can be opened;
    each tool file is responsible for its own handling of any
    database-backed option inside its own window (see, e.g., a tool's
    "Database Table" radio button, which is that tool's own concern,
    not something this class reaches into).
    """

    def __init__(self, update_btn, update_map_btn,
                 update_btn_normal_bg, update_map_btn_normal_bg,
                 is_any_tool_active_fn, is_automation_busy_fn,
                 hub_set_visual_fn):
        """
        Args:
            update_btn, update_map_btn: the two existing tk.Button
                widgets.
            update_btn_normal_bg, update_map_btn_normal_bg: each
                button's own enabled-state background color (MAIN.py's
                "#007acc" / "#6a9f2f" respectively) -- see
                set_secondary_button_enabled()'s own normal_bg
                parameter for why DBGate needs to remember these
                (restoring the CORRECT color per button when
                re-enabling, since the two buttons do not share one
                enabled color the way they now share one disabled
                color and one hover color).
            is_any_tool_active_fn: zero-arg callable -> bool. MAIN.py
                passes core.tool_exclusivity.is_any_tool_active through
                here; this module never imports that module itself.
                Used only to fold into the busy computation below, NOT
                to guard any icon-grid touch (there is none here
                anymore).
            is_automation_busy_fn: zero-arg callable -> bool. MAIN.py
                passes something that reads its own
                _gm_automation_in_flight flag.
            hub_set_visual_fn: callable(connected: bool, busy: bool) ->
                None, provided by create_hub_button() (below) to update
                the hub button's own color/state. This class never
                touches the hub button directly -- create_hub_button()
                owns that widget and how it's recolored; DBGate only
                tells it what state to reflect.
        """
        self._update_btn = update_btn
        self._update_map_btn = update_map_btn
        self._update_btn_normal_bg = update_btn_normal_bg
        self._update_map_btn_normal_bg = update_map_btn_normal_bg
        self._is_any_tool_active_fn = is_any_tool_active_fn
        self._is_automation_busy_fn = is_automation_busy_fn
        self._hub_set_visual_fn = hub_set_visual_fn
        # Tracks whether the gate is CURRENTLY applied, purely so
        # _apply_gate()/_release_gate() can be genuinely idempotent
        # no-ops rather than re-running .config() calls on every
        # refresh() tick even when nothing changed.
        self._gate_applied = None  # None = not yet initialized

    # -- public contract --------------------------------------------
    def set_db_connected(self, is_connected):
        """
        The ONE state-transition entry point for UI gating -- see
        module docstring, DBGate CONTRACT, for the explicit scope
        limitation (this is NOT a live-connectivity signal; it is only
        ever called from this module's own CHANGE CONNECTION commit
        handler, in show_configure_db_dialog(), always with True).

        is_connected is informational only for callers' own clarity at
        the call site -- this method does not itself decide _db_state;
        by the time it is called, the caller (the CHANGE CONNECTION
        commit handler) has already performed the actual _db_state
        transition. This method's only job is to call refresh() so the
        UI catches up to whatever _db_state now is. A field edit's own
        collapse back to UNVERIFIED does NOT call this method -- it is
        picked up passively, the next time anything calls refresh()
        (e.g. the Update buttons' own command= lambdas already do, via
        MAIN.py's _db_gate_refresh_if_ready()), rather than needing its
        own dedicated set_db_connected(False) call site.
        """
        self.refresh()

    def refresh(self):
        """
        Reconciles the UI against CURRENT _db_state[0] and current busy
        state, both read fresh every call -- never cached. See module
        docstring, DBGate CONTRACT, for the exact invariant computed
        here (the one and only place gated_controls_enabled is
        computed).
        """
        busy = bool(self._is_any_tool_active_fn()) or bool(self._is_automation_busy_fn())
        db_ok = (_db_state[0] == "VERIFIED")
        gated_controls_enabled = db_ok and not busy

        if gated_controls_enabled:
            self._release_gate()
        else:
            self._apply_gate()

        if self._hub_set_visual_fn is not None:
            self._hub_set_visual_fn(connected=db_ok, busy=busy)

    # -- private UI-mutation primitives -------------------------------
    def _apply_gate(self):
        """Disables the two Update buttons (gray, "no" cursor).
        Idempotent."""
        if self._gate_applied is True:
            return
        self._gate_applied = True

        set_secondary_button_enabled(self._update_btn, False, self._update_btn_normal_bg)
        set_secondary_button_enabled(self._update_map_btn, False, self._update_map_btn_normal_bg)

    def _release_gate(self):
        """Inverse of _apply_gate(): restores each button's own normal
        color and cursor. Idempotent."""
        if self._gate_applied is False:
            return
        self._gate_applied = False

        set_secondary_button_enabled(self._update_btn, True, self._update_btn_normal_bg)
        set_secondary_button_enabled(self._update_map_btn, True, self._update_map_btn_normal_bg)


# ============================================================
# HUB BUTTON
# ============================================================
_HUB_BTN_BG = "#f2b705"
_HUB_BTN_FG = "#3a2c00"
_HUB_BTN_DISABLED_BG = _SECONDARY_BTN_DISABLED_BG
_HUB_BTN_DISABLED_FG = _SECONDARY_BTN_DISABLED_FG


def create_hub_button(parent_frame, update_btn, update_map_btn,
                       update_btn_normal_bg, update_map_btn_normal_bg,
                       is_any_tool_active_fn, is_automation_busy_fn,
                       on_click):
    """
    Builds the runtime DB-status hub control: a plain, two-line
    tk.Button reading "Configure\\nDatabase" (a literal embedded
    newline, not two widgets -- Tk renders a multi-line Button label
    correctly on its own). Replaces an earlier version of this function
    (create_hub_icon()) that swapped between three PNG images on a
    Canvas; this button needs no image assets at all -- its visual is
    entirely color-driven (see _set_hub_button_visual() below).
    Constructs the DBGate that goes with it.

    Does NOT pack/place the returned button -- placement between
    button_frame and btn_frame is MAIN.py's own layout decision (this
    module has no knowledge of those two frames' existence), matching
    this module's leaf-level, no-layout-opinions role. There are no
    connector lines to draw to neighboring widgets either (an earlier
    version of this function's hub-icon counterpart drew three; a plain
    button needs no visual routing to what it's near).

    Args:
        parent_frame: the Tk container the hub button will live in
            (MAIN.py packs the returned button into this itself; passed
            here only so the button is created with the right master).
        update_btn, update_map_btn, update_btn_normal_bg,
            update_map_btn_normal_bg: passed straight through to the
            constructed DBGate -- see DBGate.__init__ for what each is
            used for. The Feature Management Tools icon grid is
            deliberately NOT a parameter here -- DBGate never touches
            it at all (see DBGate's own class docstring).
        is_any_tool_active_fn, is_automation_busy_fn: zero-arg
            callables -> bool, passed straight through to DBGate AND
            used directly here for the hub's own click-gating (see
            module docstring -- hub_clickable = not busy, independent
            of DB state). Update Map / Update Database, the Feature
            Management Tools icon grid, and the hub button itself all
            stay grayed for a Configure Database session's ENTIRE
            lifetime -- per this task's own explicit requirement, a
            CHANGE CONNECTION commit succeeding partway through does
            NOT ungray anything early; only closing the dialog does
            (see show_configure_db_dialog()'s own docstring for the
            related "revert to last known-good state" logic that runs
            at that point).
        on_click: zero-arg callable MAIN.py supplies, invoked when the
            hub button is clicked while not busy (MAIN.py wires this to
            open show_configure_db_dialog(...), bracketed by
            core/tool_exclusivity.py's activate_manual()/deactivate_all(),
            with the constructed gate in scope).

    Returns:
        (DBGate, tk.Button): the gate instance and the hub's own button
        widget, for MAIN.py to pack/place.
    """
    hub_btn = Button(
        parent_frame, text="Configure\nDatabase",
        width=12, relief="flat", justify="center",
        font=("Segoe UI", 8, "bold"),
    )

    def _set_hub_button_visual(connected, busy):
        """Reflects `busy` on hub_btn's own color/state -- see module
        docstring, DBGate CONTRACT, for the hub_clickable = not busy
        invariant this implements.

        Only TWO combinations, per this task's own spec -- the hub is a
        CONFIGURATION control, not a live representation of DB
        connectivity, so it no longer varies by connected/disconnected
        at all:

        - busy: a genuine, Tk-level state="disabled" (the click handler
          below would also refuse it, but this is real, not just
          visual) -- gray, "no" cursor. The only case where the button
          is actually inert.
        - not busy (connected OR disconnected -- doesn't matter):
          state="normal", yellow, "hand2" cursor. Always looks
          available, because it always IS available (it is still the
          only way to open or change the connection, whatever the
          current state).

        `connected` is accepted but intentionally UNUSED in this body
        -- it exists ONLY so this function's signature still matches
        what DBGate.refresh() calls (hub_set_visual_fn(connected=db_ok,
        busy=busy)), letting DBGate itself stay completely unchanged.
        It is NOT a leftover hint that connected state used to matter
        here and might again -- this task's own explicit requirement is
        that the hub's color no longer depends on it at all.
        """
        if busy:
            hub_btn.config(state="disabled", bg=_HUB_BTN_DISABLED_BG,
                            fg=_HUB_BTN_DISABLED_FG, cursor="no")
        else:
            hub_btn.config(state="normal", bg=_HUB_BTN_BG,
                            fg=_HUB_BTN_FG, cursor="hand2")

    hub_hover_bg = _darken_hex_color(_HUB_BTN_BG)

    def _on_hub_enter(event):
        if str(hub_btn["state"]) == "normal":
            hub_btn.config(bg=hub_hover_bg)

    def _on_hub_leave(event):
        if str(hub_btn["state"]) == "normal":
            hub_btn.config(bg=_HUB_BTN_BG)

    hub_btn.bind("<Enter>", _on_hub_enter)
    hub_btn.bind("<Leave>", _on_hub_leave)

    gate = DBGate(
        update_btn=update_btn,
        update_map_btn=update_map_btn,
        update_btn_normal_bg=update_btn_normal_bg,
        update_map_btn_normal_bg=update_map_btn_normal_bg,
        is_any_tool_active_fn=is_any_tool_active_fn,
        is_automation_busy_fn=is_automation_busy_fn,
        hub_set_visual_fn=_set_hub_button_visual,
    )

    def _on_hub_click():
        # hub_clickable = not busy, independent of DB state -- see
        # module docstring. The hub is gated because an operation is
        # in progress, never because the database happens to be
        # disconnected; it must always remain the way OUT of a
        # disconnected state. This is the SAME busy pre-check the old
        # hub icon's own click handler used -- unchanged by the
        # icon-to-button visual redesign. Also serves as a defensive,
        # functional backstop to the busy-case state="disabled" above:
        # even if something ever bound a click to this button while Tk
        # still reported state="normal" (it shouldn't), this check
        # would still refuse it.
        busy = bool(is_any_tool_active_fn()) or bool(is_automation_busy_fn())
        if busy:
            return
        on_click()

    hub_btn.config(command=_on_hub_click)

    # Establish the hub's initial visual immediately (session starts
    # UNVERIFIED -- see module docstring, DB STATE MACHINE -- but
    # refresh() reads _db_state[0] fresh, so this reflects whatever
    # state is already current by the time create_hub_button() runs).
    gate.refresh()

    return gate, hub_btn


# ============================================================
# STARTUP DIALOG (workspace picker only -- see module docstring)
# ============================================================
def is_db_verified():
    """
    Read-only query: True if the CURRENT session's database connection
    is VERIFIED, False for UNVERIFIED (see the module docstring's DB
    STATE MACHINE section for the full definition). Reached either by a
    CHANGE CONNECTION commit in the Configure Database dialog (with no
    field edit since), or, as of this task's own addition, by
    try_restore_saved_connection() finding a complete, previously-saved
    connection at startup (see that function's own docstring) -- in
    either case, this module has no involvement in verifying the
    connection actually still works; it only tracks whether a complete
    set of credentials was committed or restored.

    This is this module's one exported read of _db_state -- MAIN.py
    calls it, in its dev-mode tool-launch path only (see
    run_tool_by_label()'s dev-mode branch), to snapshot the session's
    DB-verified status at the moment a Feature Management Tool is
    launched in-process, and pass that snapshot on to the tool's own
    main(db_verified=...) parameter. The frozen/production launch path
    does NOT call this directly -- a frozen tool runs as a genuinely
    separate OS process (subprocess.Popen), which cannot read this
    module's in-memory _db_state at all, so that path instead passes
    the same snapshot across the process boundary as a "--db-verified"
    command-line argument (computed at the same call site in MAIN.py,
    also by reading this same function, right before the subprocess is
    spawned) -- see MAIN.py's own run_tool_by_label() for both paths.
    Does not mutate any state.
    """
    return _db_state[0] == "VERIFIED"


def try_restore_saved_connection(get_credentials_path_fn):
    """
    Checks whether pg_credentials.json already holds a COMPLETE
    connection (all six required fields -- host, port, database,
    schema, username, password -- non-blank) and, if so, marks the
    session VERIFIED immediately, restoring the exact behavior a
    session used to have before this file existed: a usable saved
    connection just works on the next launch, with no extra step.

    Without this, every session would start UNVERIFIED regardless of
    what pg_credentials.json already holds (see the module docstring's
    DB STATE MACHINE section), leaving Update Map / Update Database and
    every tool's own Database Table option disabled by default even
    when a perfectly good, already-saved connection exists -- and per
    CHANGE CONNECTION's own "must differ from the saved connection"
    enablement rule (see show_configure_db_dialog()'s own docstring),
    the user could not even fix this by reopening Configure Database:
    the fields would read back exactly what's already saved, so CHANGE
    CONNECTION would just stay disabled, with no way forward short of
    editing something and then editing it back.

    Called once, by MAIN.py's own startup_sequence(), immediately AFTER
    show_startup_dialog() -- that function unconditionally resets
    _db_state to UNVERIFIED at its own construction time (see its own
    docstring); this call is what overrides that back to VERIFIED when
    appropriate, right afterward. Ordering matters: calling this BEFORE
    show_startup_dialog() would have its result immediately overwritten
    by that reset.

    Does NOT re-test the connection -- no psycopg2.connect() call here.
    Trusts a complete saved file the same way this module already
    trusts one to pre-fill the Configure Database dialog's own fields
    (_load_saved_credentials(), reused here). If the saved credentials
    are actually stale or wrong, that surfaces through the existing,
    unaffected non-technical error handling already inside
    update_map_and_select_recorded() / update_database_from_geopackage()
    / each of the 11 tools' own database code -- the same place it
    always has, whether a connection was committed via CHANGE
    CONNECTION or restored here.

    Args:
        get_credentials_path_fn: callable, no args -> credentials path.

    Returns:
        dict | None: the six field values, if pg_credentials.json
        exists and all six are non-blank (_db_state[0] is set to
        "VERIFIED" and _verified_fields[0] to this same dict before
        returning). The caller (MAIN.py) is expected to pass this
        straight to its own on_credentials_changed callback
        (_on_credentials_changed), so the session's stored_username/
        stored_password/DB_HOST/DB_PORT/DB_NAME/DB_SCHEMA globals get
        set together with this module's own state -- exactly the way a
        normal CHANGE CONNECTION commit already does (see
        show_configure_db_dialog()'s own _do_commit()). Returns None,
        and leaves _db_state exactly as show_startup_dialog() already
        set it (UNVERIFIED), if the file was missing, unreadable, or
        incomplete.
    """
    saved = _load_saved_credentials(get_credentials_path_fn)
    if not saved:
        return None
    required_keys = ("host", "port", "database", "schema", "username", "password")
    if not all(saved.get(k, "").strip() for k in required_keys):
        return None
    values = {k: saved[k] for k in required_keys}
    _db_state[0] = "VERIFIED"
    _verified_fields[0] = dict(values)
    return values


def _remove_minmax_buttons(win):
    """
    Strips the titlebar's minimize and maximize buttons via the Win32
    API directly, same GetWindowLongW/SetWindowLongW approach as
    road_width.py's _remove_close_button(). Neither button does
    anything useful on a fixed-size, non-resizable dialog like this
    one -- they stay visible and clickable by default even with
    resizable(False, False) set, which reads as broken rather than
    intentionally absent. This actually removes them from the
    titlebar, leaving just the close (X) button and icon.

    GetParent(win.winfo_id()) rather than win.winfo_id() directly:
    same long-standing Tkinter-on-Windows HWND quirk noted in
    road_width.py's own version of this pattern.

    Windows-only, fully defensive: any failure here is caught and
    logged, leaving the buttons visible but otherwise not affecting
    the dialog -- a cosmetic miss, not a crash.
    """
    try:
        import ctypes
        GWL_STYLE = -16
        WS_MINIMIZEBOX = 0x00020000
        WS_MAXIMIZEBOX = 0x00010000
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
        style &= ~WS_MINIMIZEBOX
        style &= ~WS_MAXIMIZEBOX
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_STYLE, style)
    except Exception as e:
        print(f"Could not remove the titlebar minimize/maximize buttons: {e}")


def show_startup_dialog(root, apply_icon_fn, resize_file_dialog_fn, on_start):
    """
    Shows the startup dialog: Global Mapper Workspace file picker +
    Start -- nothing else. Replaces the old startup_sequence() native
    file picker AND the entirety of the old show_login_and_connect()
    Toplevel. As of this task, no credentials are collected here at
    all: the six credential fields, Test Connection, and the
    all-fields-filled / partial-fields Yes/No "continue without
    database?" branching that used to live in this dialog are gone
    entirely -- see the module docstring's DB STATE MACHINE section for
    why (the session's DB connection is now configured exclusively
    through the mid-session Configure Database dialog). Because there
    is no longer a credential-based branch to choose between, Start
    always proceeds the same single way, regardless of DB state.

    Args:
        root: the Tk root (used only as the Toplevel's implicit
            parent; this function does not otherwise touch root).
        apply_icon_fn: callable(win) -> None, MAIN.py's existing
            apply_icon(), applied to this dialog the same way every
            other Toplevel in this codebase gets its icon set.
        resize_file_dialog_fn: callable, no args, MAIN.py's existing
            resize_file_dialog() -- started on a background thread by
            THIS function around the Browse button's own
            askopenfilename() call, mirroring the old startup_sequence()
            exactly (this module does the threading.Thread(...).start()
            call itself; resize_file_dialog_fn is just the target).
        on_start: callable(workspace_path: str) -> None. Called after
            this dialog is destroyed once Start is clicked (only
            reachable once a workspace path has been chosen -- see
            Start's own enabled state below). MAIN.py wires this to the
            existing launch_global_mapper() chain, always with
            db_less=True now (see launch_global_mapper()'s own
            docstring -- there are no credentials left to patch into
            the .gmw file at launch time in any session).

    Closing via the titlebar X asks for confirmation first
    ("Exit Application" / "Exit Land Valuation Tools?" -- Yes/No), and
    exits the entire process only on Yes. A No leaves the dialog open,
    untouched. This is a confirmation added on top of the old
    dialogs' own Cancel behavior, not a partial-cancel or "go back"
    affordance -- there is still no way to close this dialog and
    return to some earlier state; the only two outcomes remain
    "continue on this dialog" or "exit the whole process." Unchanged
    from the prior version of this dialog.
    """
    import threading
    from tkinter import filedialog

    # Every session starts, and stays, UNVERIFIED until a successful
    # mid-session CHANGE CONNECTION commit -- see module docstring, DB
    # STATE MACHINE. Reset here (as the prior version of this dialog
    # already did) purely for a clean, defensive baseline at the start
    # of a session; there is no credential path left in THIS dialog
    # that could have left _db_state anything else by this point.
    _db_state[0] = "UNVERIFIED"
    _verified_fields[0] = None

    win = Toplevel(root)
    apply_icon_fn(win, "resources/igdi_icon.ico", "resources/igdi_icon.png")
    win.title("Land Valuation Tools - Startup")
    win.resizable(False, False)
    _remove_minmax_buttons(win)
    win.grab_set()

    def _on_close():
        if not messagebox.askyesno("Exit Application", "Exit Land Valuation Tools?"):
            return
        try:
            root.quit()
            root.destroy()
        except Exception:
            pass
        sys.exit(0)

    win.protocol("WM_DELETE_WINDOW", _on_close)

    outer = Frame(win)
    outer.pack(padx=10, pady=10)

    # --- Global Mapper Workspace section ---
    Label(outer, text="Global Mapper Workspace", font=("Segoe UI", 9, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 0)
    )

    workspace_frame = Frame(outer)
    workspace_frame.grid(row=1, column=0, columnspan=2, sticky="we", pady=(4, 10))

    workspace_path_var = {"path": ""}
    path_entry = Entry(workspace_frame, width=28, state="readonly")
    path_entry.pack(side="left", padx=(0, 5))

    def _set_path_display(path):
        path_entry.config(state="normal")
        path_entry.delete(0, "end")
        path_entry.insert(0, path)
        path_entry.config(state="readonly")

    def _do_browse():
        threading.Thread(target=resize_file_dialog_fn, daemon=True).start()
        gmw_file = filedialog.askopenfilename(
            title="Select Global Mapper Workspace File",
            filetypes=[("Global Mapper Workspace", "*.gmw")],
        )
        if gmw_file:
            workspace_path_var["path"] = gmw_file
            _set_path_display(gmw_file)
            _set_primary_button_enabled(start_btn, True)

    Button(workspace_frame, text="Browse...", command=_do_browse).pack(side="left")

    # --- Start button -- enabled purely by workspace-path presence now;
    # there is no VERIFIED/UNVERIFIED branch left to choose between at
    # startup, since no credentials are ever collected here (see this
    # function's own docstring). ---
    def _do_start():
        win.destroy()
        on_start(workspace_path_var["path"])

    start_btn = Button(outer, text="START", command=_do_start, state="disabled",
                        bg=_PRIMARY_BTN_DISABLED_BG, fg=_PRIMARY_BTN_DISABLED_FG,
                        cursor="no", font=("Segoe UI", 10, "bold"))
    _bind_primary_button_hover(start_btn)
    start_btn.grid(row=2, column=0, columnspan=2, sticky="we", pady=(8, 0), ipady=4)

    # Center the dialog on the screen. update_idletasks() forces Tk to
    # finish laying out every widget above so winfo_reqwidth/reqheight
    # report the window's real, final size -- before this call, those
    # would still reflect an unlaid-out (typically much smaller) size,
    # producing a wrong, off-center position. This runs once, after
    # all widgets above are built, not on every resize -- the dialog
    # is a fixed, non-resizable Toplevel (win.resizable(False, False)
    # above), so its size never changes after this point.
    win.update_idletasks()
    win_w = win.winfo_reqwidth()
    win_h = win.winfo_reqheight()
    screen_w = win.winfo_screenwidth()
    screen_h = win.winfo_screenheight()
    center_x = (screen_w - win_w) // 2
    center_y = (screen_h - win_h) // 2
    win.geometry(f"+{center_x}+{center_y}")


# ============================================================
# MID-SESSION CONFIGURE-DB DIALOG
# ============================================================
_DISCARD_CHANGES_TITLE = "Discard Changes?"
_DISCARD_CHANGES_MSG = (
    "The credentials you typed have not been applied. Closing "
    "now will discard them and keep the current database connection. "
    "Continue?"
)
_CONN_FAILED_TITLE = "Connection Failed"
_CONN_FAILED_MSG_TEMPLATE = (
    "{error}\n\n"
    "Do you want to continue and change your database connection anyway?"
)
_CHANGE_CONN_SUCCESS_TITLE = "Connection Changed"
_CHANGE_CONN_SUCCESS_MSG = "The database connection has been updated successfully."
_CHANGE_CONN_UNSAVED_TITLE = "Connected, But Not Saved"
_CHANGE_CONN_UNSAVED_MSG = (
    "The database connection is active for this session, but "
    "could not be saved for next time. Please check that the "
    "application can write to its configuration folder."
)
_TOOLTIP_TEXT_EMPTY = (
    "Fill in all six database connection fields to enable Change Connection."
)
_TOOLTIP_TEXT_SAME = (
    "No changes to save \u2014 this matches your current database connection."
)


def show_configure_db_dialog(root, apply_icon_fn, get_credentials_path_fn, db_gate,
                              on_credentials_changed):
    """
    Shows the mid-session Configure Database dialog -- the hub button's
    click target, and, as of this task, the ONLY place the session's
    database connection is ever established or changed (the startup
    dialog no longer collects credentials at all -- see
    show_startup_dialog()'s own docstring). Reachable at any time during
    the session, and NEVER exits the process on close.

    BLOCKS the caller: this function calls win.wait_window() at the end
    and does not return until the Toplevel it built is destroyed, on
    ANY exit path (a commit does NOT close the dialog -- see below --
    so wait_window() keeps blocking through that; only the titlebar X,
    silent or confirmed-discard, ends it). This is what MAIN.py's own
    call site relies on to bracket core/tool_exclusivity.py's
    activate_manual()/deactivate_all() correctly around this dialog's
    real lifetime (see Section 1.5 of this task's own spec) -- a non-
    blocking version would have released that busy-lock the instant the
    Toplevel was built, not when the user actually finished with it.
    Update Map / Update Database, the Feature Management Tools icon
    grid, and the hub button itself all stay grayed for this dialog's
    ENTIRE lifetime regardless of what happens inside it -- a commit
    succeeding, or even a commit happening at all, does NOT ungray
    anything early; only this Toplevel actually closing does (see
    create_hub_button()'s own docstring for the broader rationale).

    There is no separate Test Connection step and no confirmation step
    before testing -- CHANGE CONNECTION is the ONLY action in this
    dialog, and clicking it immediately runs the connectivity test on a
    background thread (_run_test_connection_threaded(), unchanged --
    the button itself is passed as that function's `status_label`
    argument, so the existing rotating-glyph spinner animates directly
    on the button's own text while the test is in flight; no check/X
    result glyph is ever shown anywhere in this dialog). What happens
    once the test resolves is where this version differs most from an
    earlier one:

    - SUCCESS: commits immediately -- no confirmation dialog of any
      kind. _db_state/_verified_fields are set, pg_credentials.json is
      written (best-effort), db_gate.set_db_connected(True) and
      on_credentials_changed() are called, and exactly one of two
      follow-up dialogs is shown depending on whether the
      pg_credentials.json write itself succeeded (see _do_commit()).
    - FAILURE: shows ONE combined dialog -- the existing non-technical
      error message from _friendly_connection_error_message(), together
      with "Do you want to continue and change your database connection
      anyway?", Yes/No, NOT as two separate dialogs (per this task's own
      explicit requirement). No commits nothing (the fields are reverted
      to the last known-good connection -- see _reset_fields_to_reference()
      -- so a rejected, broken attempt never lingers visibly, and this
      dialog is no longer "dirty"). Yes commits anyway, through the
      EXACT same _do_commit() path SUCCESS uses -- a deliberately
      "engineer's-choice" connection is just as much a real commit as a
      verified one; this dialog's job is to ask, not to gatekeep. The
      actual, non-technical error handling for a connection that
      genuinely does not work lives where it already did -- inside
      update_map_and_select_recorded() / update_database_from_geopackage()
      / each of the 11 tools' own database code -- not here.

    CHANGE CONNECTION is enabled if and only if EVERY ONE of the six
    fields (host, port, database, schema, username, password -- ALL
    SIX, unlike an earlier version of this dialog which excluded port)
    has at least one character of input, AND at least one field differs
    from the reference connection (see _get_reference_values() -- the
    most recent successful commit THIS session if there was one, else
    whatever was loaded from pg_credentials.json when this dialog
    opened). Recomputed live on every keystroke (see
    _change_conn_disabled_reason()/_on_edit() below). Hovering the
    button while it is disabled shows a small tooltip explaining which
    of the two reasons applies (see _TOOLTIP_TEXT_EMPTY/_SAME above) --
    the disabled-cursor Tk already shows via _set_primary_button_enabled()
    communicates THAT it is inert; the tooltip is what explains WHY.

    Closing via the titlebar X first cancels any in-flight test (the
    same mechanism the old startup dialog's Start button used to cancel
    its own in-flight Test Connection -- see
    _run_test_connection_threaded()'s cancel() docstring), THEN checks
    this dialog's own "dirty since open" flag: not dirty (nothing
    edited since opening, or since the last commit/revert) closes
    silently; dirty shows a "Discard Changes?" Yes/No confirmation --
    Yes proceeds to close, No keeps the dialog open. On every path that
    actually closes the dialog, _db_state/_verified_fields are restored
    to the last known-good snapshot (see last_known_good below) right
    before win.destroy() -- this is what makes "fail an attempt, say No
    to committing it anyway, then close" correctly leave the PREVIOUS,
    still-working connection live, rather than stranding the session in
    UNVERIFIED just because a field was edited along the way. There is
    no separate Close button -- the titlebar X is the only way out.

    This dialog affects ONLY CAMA Tools' own database consumers
    (Update Map / Update Database automation, the Feature Management
    Tools subprocesses, pg_credentials.json, and, as of this task,
    MAIN.py's own stored_username/stored_password/DB_HOST/DB_PORT/
    DB_NAME/DB_SCHEMA globals via on_credentials_changed). It explicitly
    does NOT attempt to modify, relaunch, or repatch the already-running
    Global Mapper instance's own PostGIS connection. It also does NOT
    retroactively notify any Feature Management Tool window that was
    ALREADY OPEN before a commit -- each tool reads its own db_verified
    snapshot once, at its own launch time (see is_db_verified()'s own
    docstring); this is an accepted limitation of that existing
    snapshot-based design (each tool runs as a genuinely separate OS
    process in the frozen build, with no push-update channel), not
    something this task adds a mechanism for. A tool launched AFTER a
    commit correctly sees the new credentials from its own first read.

    Args:
        root: the Tk root (Toplevel parent only).
        apply_icon_fn: callable(win) -> None, MAIN.py's apply_icon().
        get_credentials_path_fn: callable, no args -> credentials path.
        db_gate: the DBGate instance returned by create_hub_button().
        on_credentials_changed: callable(credentials: dict) -> None,
            called at CHANGE CONNECTION's own commit step (see
            _do_commit()) with the exact field values just committed --
            fired identically whether the commit followed a successful
            test or a "continue anyway" Yes on a failed one. MAIN.py
            wires this to a callback that sets its own
            stored_username/stored_password/DB_HOST/DB_PORT/DB_NAME/
            DB_SCHEMA globals -- see module docstring and this task's
            own bug-fix notes.
    """
    win = Toplevel(root)
    apply_icon_fn(win, "resources/igdi_icon.ico", "resources/igdi_icon.png")
    win.title("Configure Database Connection")
    win.resizable(False, False)
    _remove_minmax_buttons(win)
    win.grab_set()

    dirty = {"flag": False}
    active_test_cancel = {"fn": None}
    disabled_reason = {"value": None}
    tooltip_state = {"win": None}
    # Snapshot of the "last known good" connection, captured fresh
    # every time this dialog opens and updated on every successful
    # commit made THIS session (see _do_commit()) -- what _on_close()
    # restores _db_state/_verified_fields to on every path that
    # actually closes the dialog, and what _get_reference_values()
    # below treats as the connection to compare the current fields
    # against / revert to on a rejected failed attempt. Deliberately
    # NOT always "whatever was on disk at open time": if a commit
    # already happened earlier in this SAME dialog session, that
    # commit -- not the pre-session state -- is the correct thing to
    # fall back to.
    last_known_good = {"db_state": _db_state[0], "verified_fields": _verified_fields[0]}

    outer = Frame(win)
    outer.pack(padx=10, pady=10)

    Label(outer, text="Database Information", font=("Segoe UI", 9, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 2)
    )

    saved = _load_saved_credentials(get_credentials_path_fn)
    field_entries = _build_credential_fields(outer, saved, start_row=1)

    # ALL SIX fields are required, per this task's own explicit
    # instruction -- unlike an earlier version of this dialog, which
    # excluded port (reusing an older, unrelated "five_filled" check
    # from a since-removed part of the startup dialog). That precedent
    # no longer applies here; this task's own instruction is explicit
    # and unambiguous about all six.
    _REQUIRED_FIELD_KEYS = ("host", "port", "database", "schema", "username", "password")

    def _get_reference_values():
        """The connection to compare the current fields against, and
        to revert to on a rejected failed attempt -- the most recent
        successful commit THIS session (last_known_good["verified_fields"])
        if there was one, else whatever was loaded from
        pg_credentials.json when this dialog opened (`saved`, read
        once, above)."""
        if last_known_good["verified_fields"] is not None:
            return last_known_good["verified_fields"]
        return {k: (saved.get(k, "") if saved else "") for k in _REQUIRED_FIELD_KEYS}

    def _change_conn_disabled_reason():
        """Returns "empty" (one or more required fields blank), "same"
        (every field matches _get_reference_values() exactly), or None
        (should be enabled). The two reasons are mutually exclusive and
        exhaustive of every disabled case -- see _TOOLTIP_TEXT_EMPTY/
        _SAME above for the tooltip text each one shows."""
        values = _read_fields(field_entries)
        if not all(values[k].strip() for k in _REQUIRED_FIELD_KEYS):
            return "empty"
        ref = _get_reference_values()
        if all(values[k] == ref.get(k, "") for k in _REQUIRED_FIELD_KEYS):
            return "same"
        return None

    def _refresh_change_conn_enabled():
        """Restores CHANGE CONNECTION to its idle label and recomputes
        both its enabled state AND its own disabled-reason (for the
        tooltip) from the CURRENT field contents -- the single function
        every terminal path below (a field edit, a rejected failed
        attempt, a commit, or an unexpected exception) calls to leave
        the button in a correct, never-stuck state. Always resets the
        text first: this is what guarantees the button never stays
        showing a leftover spinner glyph once whatever triggered this
        call has finished."""
        change_conn_btn.config(text="CHANGE CONNECTION")
        reason = _change_conn_disabled_reason()
        disabled_reason["value"] = reason
        _set_primary_button_enabled(change_conn_btn, reason is None)

    def _reset_fields_to_reference():
        """Overwrites every field's live contents with
        _get_reference_values() -- used when a failed test's connection
        is explicitly rejected (No on the "continue anyway?" dialog),
        so a broken attempt never lingers visibly in the fields.
        Programmatic Entry edits (.delete()/.insert()) do NOT fire
        <KeyRelease>, so this does not (and must not need to) go
        through _on_edit() -- the caller is responsible for its own
        dirty["flag"]/_refresh_change_conn_enabled() afterward."""
        ref = _get_reference_values()
        for key, entry in field_entries.items():
            entry.delete(0, "end")
            entry.insert(0, ref.get(key, ""))

    def _show_tooltip(text):
        _hide_tooltip()
        tip_win = Toplevel(win)
        tip_win.wm_overrideredirect(True)
        try:
            tip_win.wm_attributes("-topmost", True)
        except Exception:
            pass
        x = change_conn_btn.winfo_rootx() + 8
        y = change_conn_btn.winfo_rooty() + change_conn_btn.winfo_height() + 4
        tip_win.wm_geometry(f"+{x}+{y}")
        Label(
            tip_win, text=text, bg="#ffffe0", fg="black", relief="solid",
            borderwidth=1, font=("Segoe UI", 8), wraplength=260,
            justify="left", padx=4, pady=2,
        ).pack()
        tooltip_state["win"] = tip_win

    def _hide_tooltip():
        if tooltip_state["win"] is not None:
            try:
                tooltip_state["win"].destroy()
            except Exception:
                pass
            tooltip_state["win"] = None

    def _on_change_conn_tooltip_enter(event):
        # Only when disabled -- an enabled button already communicates
        # "click me" on its own; the tooltip's whole job is explaining
        # WHY a disabled one is inert, matching the disabled cursor
        # _set_primary_button_enabled() already applies. add="+" below
        # keeps this stacked alongside _bind_primary_button_hover()'s
        # own <Enter> (the color-hover handler), rather than replacing
        # it -- that handler already no-ops while disabled, so the two
        # never conflict.
        if str(change_conn_btn["state"]) == "disabled":
            reason = disabled_reason["value"]
            if reason == "empty":
                _show_tooltip(_TOOLTIP_TEXT_EMPTY)
            elif reason == "same":
                _show_tooltip(_TOOLTIP_TEXT_SAME)

    def _on_change_conn_tooltip_leave(event):
        _hide_tooltip()

    def _on_edit():
        # Any edit, even a single character: recompute CHANGE
        # CONNECTION's enabled state (and disabled-reason, for the
        # tooltip) from the current field contents, and mark this
        # dialog dirty. _bind_edit_invalidation() itself already
        # collapses _db_state[0] to "UNVERIFIED" alongside calling this
        # hook; that collapse has no VISIBLE effect while this dialog is
        # open (Update Map/Database, the tool grid, and the hub button
        # are all forced to their busy appearance for this dialog's
        # whole lifetime regardless -- see this function's own
        # docstring), and is picked up correctly the moment this dialog
        # closes and _on_close() below restores the correct
        # last-known-good state.
        dirty["flag"] = True
        _refresh_change_conn_enabled()

    _bind_edit_invalidation(field_entries, extra_on_edit=_on_edit)

    def _do_commit(values):
        """The ONE commit sequence -- shared by BOTH a successful test
        and a "continue anyway" Yes on a failed one (see
        _on_test_result() below): sets _db_state/_verified_fields
        (and updates last_known_good to match, so a LATER close in this
        same session restores to THIS commit, not an earlier one or the
        pre-session state), attempts to persist pg_credentials.json,
        calls db_gate.set_db_connected(True) and on_credentials_changed(),
        clears the dirty flag, restores the button, THEN shows exactly
        one of two follow-up dialogs depending on whether the
        pg_credentials.json write itself succeeded. The button is back
        to its normal, correctly-(dis)enabled look BEFORE either
        follow-up dialog appears -- not only after the user dismisses
        it. None of the earlier steps are skipped by a pg_credentials.json
        write failure -- only which follow-up dialog is shown changes;
        the LIVE session still commits either way."""
        _db_state[0] = "VERIFIED"
        _verified_fields[0] = dict(values)
        last_known_good["db_state"] = "VERIFIED"
        last_known_good["verified_fields"] = dict(values)

        write_ok = True
        try:
            creds_path = get_credentials_path_fn()
            import json
            with open(creds_path, "w") as f:
                json.dump({
                    "host": values["host"],
                    "port": values["port"],
                    "database": values["database"],
                    "schema": values["schema"],
                    "username": values["username"],
                    "password": values["password"],
                }, f)
        except Exception:
            # Persistence failure is isolated to the follow-up dialog
            # shown below -- the LIVE session state above still commits
            # regardless (see this function's own docstring).
            write_ok = False

        db_gate.set_db_connected(True)
        on_credentials_changed(dict(values))

        dirty["flag"] = False
        # Restore the button BEFORE showing either follow-up dialog --
        # the spinner must stop and CHANGE CONNECTION must already be
        # back to its normal, (dis)enabled look in sync with the
        # follow-up dialog appearing, not only after the user dismisses
        # it.
        _refresh_change_conn_enabled()

        if write_ok:
            messagebox.showinfo(_CHANGE_CONN_SUCCESS_TITLE, _CHANGE_CONN_SUCCESS_MSG)
        else:
            messagebox.showwarning(_CHANGE_CONN_UNSAVED_TITLE, _CHANGE_CONN_UNSAVED_MSG)
        # Deliberately does NOT close this dialog -- see this
        # function's own docstring.

    def _on_test_result(ok, error_message, values):
        active_test_cancel["fn"] = None
        try:
            if ok:
                _do_commit(values)
                return

            # FAILURE: one combined dialog -- the error message AND the
            # "continue anyway?" question together, per this task's own
            # explicit requirement that these NOT be two separate
            # dialogs.
            proceed = messagebox.askyesno(
                _CONN_FAILED_TITLE,
                _CONN_FAILED_MSG_TEMPLATE.format(error=error_message),
            )
            if proceed:
                _do_commit(values)
                return

            # No -> the failed attempt is rejected outright: revert the
            # fields to the last known-good connection (never leave a
            # broken attempt sitting visibly in the fields), and since
            # the fields are now, by construction, identical to that
            # reference, this dialog is no longer dirty either.
            _reset_fields_to_reference()
            dirty["flag"] = False
            _refresh_change_conn_enabled()
        finally:
            # Unconditional safety net: the ordinary paths above already
            # restore the button themselves, explicitly; this guarantees
            # it can NEVER be left stuck disabled/mid-spinner even if
            # something genuinely unexpected raised partway through --
            # e.g. inside db_gate.set_db_connected() or
            # on_credentials_changed(), neither of which this function
            # controls. Idempotent with the explicit calls above; the
            # exception itself, if any, still propagates after this
            # runs.
            _refresh_change_conn_enabled()

    def _start_test():
        _set_primary_button_enabled(change_conn_btn, False)
        change_conn_btn.config(text="")
        try:
            active_test_cancel["fn"] = _run_test_connection_threaded(
                field_entries, get_credentials_path_fn, win, change_conn_btn, _on_test_result)
        except Exception:
            # Extremely unlikely (starting the background thread itself
            # failing), but the same "never leave the button stuck"
            # guarantee applies here too.
            active_test_cancel["fn"] = None
            _refresh_change_conn_enabled()
            raise

    # width= is set explicitly (character units) so the button's own
    # requested size is fixed at construction time from its longest
    # label ("CHANGE CONNECTION") -- combined with sticky="we" below
    # (which stretches it to the grid cell's own width, itself set by
    # the six Entry fields above), this guarantees swapping its text
    # for a single spinner glyph and back (see _start_test()/
    # _refresh_change_conn_enabled()) can never visibly resize or
    # reflow the button, in either direction.
    change_conn_btn = Button(outer, text="CHANGE CONNECTION", command=_start_test,
                              width=20, state="disabled", bg=_PRIMARY_BTN_DISABLED_BG,
                              fg=_PRIMARY_BTN_DISABLED_FG, cursor="no",
                              font=("Segoe UI", 10, "bold"))
    _bind_primary_button_hover(change_conn_btn)
    change_conn_btn.bind("<Enter>", _on_change_conn_tooltip_enter, add="+")
    change_conn_btn.bind("<Leave>", _on_change_conn_tooltip_leave, add="+")
    change_conn_btn.grid(row=len(_FIELD_LABELS) + 1, column=0, columnspan=2,
                          sticky="we", pady=(10, 8), ipady=4)
    # Establish the correct initial (dis)enabled state and tooltip
    # reason immediately -- the fields were just pre-filled from
    # pg_credentials.json above, so this dialog opens with CHANGE
    # CONNECTION correctly disabled ("same") whenever that file already
    # holds a full, six-field connection.
    _refresh_change_conn_enabled()

    def _on_close():
        if active_test_cancel["fn"] is not None:
            # Same ordering _do_start() used to use in the old startup
            # dialog: cancel BEFORE evaluating the dirty check below, so
            # there is no window in which a same-moment background test
            # completion could race ahead of this close.
            active_test_cancel["fn"]()
            active_test_cancel["fn"] = None
            # cancel() (see _run_test_connection_threaded()'s own
            # docstring) writes a literal "\u2717" onto whatever widget it
            # was given -- change_conn_btn itself, here (see
            # _start_test()). Per this task's own "no check/X glyphs"
            # requirement, and so the button can never be left stuck
            # disabled/showing that glyph if the dialog goes on to stay
            # open (dirty below, and the user picks "No" to discard),
            # restore it immediately, unconditionally, regardless of
            # what happens next.
            _refresh_change_conn_enabled()

        if dirty["flag"]:
            proceed = messagebox.askyesno(_DISCARD_CHANGES_TITLE, _DISCARD_CHANGES_MSG)
            if not proceed:
                return  # No -> keep the dialog open.

        # Restore the module-level session state to the last known-good
        # connection before actually closing, on EVERY path that
        # reaches this point (silent close AND confirmed-discard alike)
        # -- see this function's own docstring, and last_known_good's,
        # for why this is what correctly leaves a still-working prior
        # connection live after "fail an attempt, decline to commit it
        # anyway, then close", rather than stranding the session in
        # UNVERIFIED. A no-op when nothing ever changed this session
        # (last_known_good already equals the live state in that case).
        _db_state[0] = last_known_good["db_state"]
        _verified_fields[0] = last_known_good["verified_fields"]
        _hide_tooltip()
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", _on_close)

    # Center the dialog on the screen, every time it opens -- matching
    # show_startup_dialog()'s own identical pattern (see that
    # function's own comment for why update_idletasks() must run
    # first: it forces Tk to finish laying out every widget above so
    # winfo_reqwidth/reqheight report the dialog's real, final size,
    # not an unlaid-out, too-small one). Unlike that dialog, this one
    # can be opened repeatedly across a session, so this centering
    # logic runs on every call to this function, not just once at
    # startup -- it must never be left to Tk's own default Toplevel
    # placement (which is effectively wherever the window manager
    # happens to put it, not necessarily centered, and not necessarily
    # consistent between openings).
    win.update_idletasks()
    win_w = win.winfo_reqwidth()
    win_h = win.winfo_reqheight()
    screen_w = win.winfo_screenwidth()
    screen_h = win.winfo_screenheight()
    center_x = (screen_w - win_w) // 2
    center_y = (screen_h - win_h) // 2
    win.geometry(f"+{center_x}+{center_y}")

    # Blocks until win is destroyed (via _on_close(), the titlebar X --
    # there is no other exit path anymore) -- see this function's own
    # docstring for why this function must actually block for MAIN.py's
    # activate_manual()/deactivate_all() bracket to be correct.
    win.wait_window()