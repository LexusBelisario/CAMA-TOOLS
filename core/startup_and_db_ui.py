"""
core/startup_and_db_ui.py

PURPOSE:
    Replaces the two old, separate startup steps -- startup_sequence()'s
    native .gmw file picker and show_login_and_connect()'s standalone
    login Toplevel -- with ONE combined startup dialog that lets the
    user pick a Global Mapper Workspace file, optionally test a
    PostGIS connection, and either proceed VERIFIED (credentials just
    confirmed) or DB-LESS (explicitly continuing without a database).

    Also owns everything related to database availability for the rest
    of the CAMA Tools session: the runtime "hub" icon (the ONLY way to
    reconfigure the DB connection after startup), its Configure-DB
    dialog, and the DB-gate that disables/enables the Update Map /
    Update Database buttons based on current DB state. Deliberately
    does NOT gray or otherwise touch the Feature Management Tools icon
    grid -- whether a tool can be opened is governed exclusively by
    core/tool_exclusivity.py's own "one tool at a time" mechanism,
    independent of database connectivity (see DBGate's own class
    docstring below).

    Same architectural role as core/tool_exclusivity.py and
    core/window_management.py (see those modules' own docstrings for
    the precedent this follows): a focused, self-contained module the
    MAIN.py launcher boundary calls into, not a general-purpose shared
    utility other tool files import. This module does NOT import from
    MAIN.py -- every piece of MAIN.py state it needs (the Tk root,
    update_btn, update_map_btn, ICONS_DIR, apply_icon(),
    get_credentials_path(), is_any_tool_active(), the automation-busy
    flag) is passed in explicitly by the caller. This module never
    imports core.tool_exclusivity itself either -- is_any_tool_active
    is passed through as a plain callable by MAIN.py, keeping this a
    true leaf module.

DB STATE MACHINE (three states -- identical rules used by BOTH the
startup dialog and the mid-session Configure-DB dialog; both dialogs
route through the SAME _attempt_test_connection()/_mark_edited() pair
below specifically so they cannot drift apart):

    VERIFIED   -- the credentials currently loaded in the six fields
                  are EXACTLY the ones a psycopg2.connect() via Test
                  Connection most recently succeeded against, AND no
                  field has been edited since. This is the ONLY state
                  in which the DB-gate is released.
    UNVERIFIED -- no currently-verified session configuration -- this
                  is NOT a claim about live database connectivity, only
                  about whether a Test Connection has succeeded against
                  the CURRENT field values. Reached via: no Test
                  Connection yet this session, a Test Connection that
                  failed, or a field edited after a prior success.
    DB_LESS    -- the user explicitly chose "continue without
                  database" at startup (either the all-fields-filled or
                  the partial/empty Yes/No branch). Functionally
                  identical to UNVERIFIED for gating purposes (the
                  DB-gate treats both as "not VERIFIED"), but tracked
                  as its own distinct state because it was reached via
                  explicit user confirmation rather than "hasn't tried
                  yet" -- this only matters for which message/dialog
                  logic applies, never for what the gate does.

    Editing ANY of the six credential fields, in EITHER dialog, always
    collapses the current state to UNVERIFIED -- including a collapse
    from DB_LESS (the user is now trying again, so "intentionally
    without a database" no longer applies once they start typing new
    credentials). This is implemented as a single eager write
    (_db_state[0] = "UNVERIFIED") the instant a field changes, so
    every reader sees an always-current flag rather than needing to
    re-diff live field values against a snapshot at read time -- there
    is exactly one flag, mutated eagerly, never lazily recomputed.

    VERIFIED is a statement about the last explicit test, never a live
    guarantee of current connectivity. Busy gating (see DBGate below)
    prevents user-initiated DB reconfiguration during an active
    operation; it does NOT guarantee database connectivity. Existing
    tool/automation error handling remains authoritative for runtime
    DB failures encountered after this module has already handed off
    to VERIFIED/DB_LESS -- this module has no involvement in that.

MUTABLE STATE (single owner: this file; nothing outside it reads or
writes these):
    _db_state = ["UNVERIFIED"]   -- one of "VERIFIED" / "UNVERIFIED" /
                                     "DB_LESS". See state machine above.
    _verified_fields = [None]    -- snapshot dict of the six field
                                     values at the moment of the last
                                     successful Test Connection, or None
                                     if never verified this session.
                                     Currently used only for potential
                                     future diagnostics / equality
                                     checks -- the eager-invalidation
                                     edit listener is what actually
                                     drives _db_state, so this snapshot
                                     is not read on the hot path, only
                                     written alongside a successful
                                     test for traceability.

DBGate CONTRACT (exposed via create_hub_icon()'s return value):
    set_db_connected(is_connected: bool) -- the ONE state-transition
        entry point for UI gating. In THIS task's scope it is called
        from exactly two places, both inside this module: a successful
        Test Connection in the Configure-DB dialog (True) and the
        shared field-edit listener while a DBGate is in scope (False).
        It is deliberately NOT a general "the database is currently
        reachable" signal -- runtime connectivity failures during a
        tool run or Update automation are NOT reported through this
        method, and this module makes no attempt to detect or react to
        them; that remains entirely the responsibility of the existing
        tool/automation error handling described in the state machine
        section above. Internally, set_db_connected() does not
        independently decide gate state -- it simply ensures the
        underlying _db_state reflects the transition already performed
        by the caller (see call sites below) and then calls refresh().
    refresh() -- reconciles the Update Map / Update Database buttons'
        state against CURRENT _db_state[0] (read fresh every call,
        never cached) and current busy state (via the
        is_any_tool_active_fn / is_automation_busy_fn callables
        supplied at construction, also queried fresh every call, never
        cached). Never mutates _db_state itself. This is the ONE place
        that computes gated_controls_enabled = (_db_state[0] ==
        "VERIFIED") AND NOT busy.
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
    Management Tool run are still mutually exclusive with each other,
    same as before), never to guard a grid-icon touch, since there is
    none left to guard.

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
                                              of a disconnected state)

DEPENDENCIES:
    stdlib: tkinter (Toplevel/Frame/Label/Entry/Button/Canvas/
    messagebox), os, threading (only to type-check nothing -- actual
    thread creation for the workspace-picker resize hook is done by
    the resize_file_dialog_fn callable MAIN.py supplies, not by this
    module).
    third-party: psycopg2 (connection test), PIL (Image, ImageTk -- for
    loading database.png the same way every other icon in MAIN.py is
    loaded).
    local: none. This module is deliberately leaf-level, matching
    core/tool_exclusivity.py's own DEPENDENCIES section.

SCOPE NOTE (this file only): this module does not touch MAIN.py or
core/tool_exclusivity.py. Wiring show_startup_dialog(), create_hub_icon(),
and show_configure_db_dialog() into the actual application -- replacing
startup_sequence()/show_login_and_connect(), inserting the hub icon
into the panel layout, and wrapping the Update buttons' command=
bindings -- is explicitly deferred to the MAIN.py edit step of this
task, not part of this file.
"""

from pathlib import Path
from tkinter import Toplevel, Frame, Label, Entry, Button, Canvas, messagebox, TclError
import sys
import threading

import psycopg2
from PIL import Image, ImageTk


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
            get_credentials_path, imported from utils.db_discovery) --
            accepted here (unused directly by this function's own
            connectivity probe) so _finish()'s later commit step can
            reuse the exact same values dict this function already
            read, rather than re-reading field_entries a second time
            on the main thread (which would risk reading DIFFERENT
            values if the user edited a field in the gap between the
            background probe finishing and _finish() running).

    Returns:
        tuple[bool, str | None, dict]: (True, None, values) on a
        successful connection test; (False, message, values) on
        failure, where message is the short, non-technical string from
        _friendly_connection_error_message(). values is always the
        exact field-values dict this function read and tested against
        -- _finish() uses it (only on success, only if not cancelled)
        to commit _verified_fields/pg_credentials.json without a
        second, possibly-stale field read.
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
    thread; both the startup dialog and the Configure-DB dialog call
    this instead of calling _attempt_test_connection() directly.

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
        on_result: callable(ok: bool, error_message: str | None),
            invoked on the main thread once the background attempt
            finishes, AFTER the spinner has already been stopped and
            cleared. error_message is None on success, or the short,
            non-technical string from _friendly_connection_error_message()
            on failure -- this is where each dialog's own
            success/failure UI (status_label's own \u2713/\u2717 text,
            showing the failure messagebox, db_gate calls, etc.) lives.
            Showing the failure messagebox from inside on_result (main
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

    Widget-destroyed race (confirmed via an actual on-machine crash
    log): a successful Test Connection followed IMMEDIATELY by the
    user clicking Start can destroy the startup dialog (win.destroy()
    inside on_verified_start()'s launch_global_mapper() call) before
    this function's own root.after(0, _finish) callback -- scheduled
    from the background thread's _worker(), and therefore always
    racing against whatever the main thread does in the meantime --
    has actually run. When that happens, status_label (and any widget
    on_result would touch) no longer exists as a live Tk widget, and
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
        for this attempt. This is what lets the Start button, per this
        task's own confirmed design, treat a still-in-flight Test
        Connection as an immediate, silent "not verified" the instant
        Start is clicked -- the user does not wait for the background
        attempt to finish, and even if that attempt WOULD have
        succeeded, the session proceeds as UNVERIFIED/DB_LESS rather
        than racing to become VERIFIED after the fact. Safe to call
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
                # Critically, this is checked BEFORE the VERIFIED/
                # pg_credentials.json commit below runs at all: a
                # cancelled attempt must leave EVERY observable trace
                # of the session (not just the dialog's own widgets)
                # as if the attempt had not completed -- see
                # _attempt_test_connection()'s own docstring for the
                # full rationale for why that commit step lives here,
                # gated on this same check, rather than inside
                # _attempt_test_connection() itself (which runs
                # unconditionally on the background thread, before
                # cancellation is even possible to check). on_result
                # must NEVER be called for a cancelled attempt either,
                # since the caller has already moved on (e.g. the
                # startup dialog may already be destroyed, or the
                # session may already be proceeding down the DB_LESS
                # path) and calling it now would apply a stale result
                # on top of whatever has happened since.
                return

            if ok:
                # Commit the successful result into session state now,
                # on the main thread, only after confirming above that
                # this attempt was NOT cancelled. This is the ONLY
                # place _db_state is marked VERIFIED and
                # pg_credentials.json is written -- see
                # _attempt_test_connection()'s own docstring for why
                # this moved here instead of living inside that
                # function's own (background-thread) body.
                _verified_fields[0] = dict(values)
                _db_state[0] = "VERIFIED"
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
                    # Writing pg_credentials.json is best-effort
                    # persistence, not a condition of VERIFIED itself
                    # -- the live psycopg2.connect() already succeeded,
                    # which is the actual thing VERIFIED represents
                    # this session. A write failure here (e.g. a locked
                    # file, a permissions issue) should not un-verify a
                    # connection that genuinely just succeeded.
                    pass

            stop_spinner["flag"] = True
            try:
                status_label.config(text="")
            except TclError:
                # See this function's own docstring, Widget-destroyed
                # race -- the dialog (and status_label with it) is
                # already gone. on_result() is also skipped in this
                # case: it exists to update the (now-gone) dialog's own
                # UI and/or a db_gate that reflects that dialog's own
                # live state, so there is nothing left for it to
                # correctly do either.
                return
            on_result(ok, error_message)

        root.after(0, _finish)

    def cancel():
        """See this function's own Returns: section above for the full
        contract. Marks this attempt as cancelled (discarding whatever
        result the background thread eventually produces), stops the
        spinner's own reschedule loop, and immediately shows a \u2717 --
        the caller (e.g. the startup dialog's Start button, per this
        task's own confirmed design) treats a still-in-flight Test
        Connection as an immediate "not verified" the instant this is
        called, without waiting for the background attempt."""
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
# SECONDARY BUTTON STYLE -- Update Map / Update Database buttons'
# gray-when-disabled + shared-hover-color visual, driven by DBGate
# ============================================================
_SECONDARY_BTN_HOVER_BG = "#5a6b7a"
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
    idempotency logic. The DISABLED color and the HOVER color are,
    however, the SAME for both buttons regardless of normal_bg (see
    _SECONDARY_BTN_DISABLED_BG / _SECONDARY_BTN_HOVER_BG above) --
    both buttons disable to the identical gray, and both show the
    identical hover shade when enabled, per this task's own
    requirement that the two buttons look consistent with each other
    at those two moments even though their normal/idle colors differ.

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

    Same "state is the single source of truth" principle as
    _bind_primary_button_hover() above: the handlers check the
    button's OWN current Tk state at the moment of the event, so a
    disabled button can never be made to look hoverable by a stray
    mouse-over.

    Args:
        button: the Button widget to bind hover behavior to.
        normal_bg: this button's own enabled-state background to
            restore on <Leave> -- see set_secondary_button_enabled()'s
            own normal_bg parameter for why this varies per button
            while the hover color itself (_SECONDARY_BTN_HOVER_BG)
            does not.
    """
    def _on_enter(event):
        if str(button["state"]) == "normal":
            button.config(bg=_SECONDARY_BTN_HOVER_BG)

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
    Database buttons based on current DB state and busy state. Does
    NOT touch the Feature Management Tools icon grid at all -- that
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
                None, provided by create_hub_icon() (below) to update
                the hub canvas's own look. This class never draws on
                the hub canvas directly -- create_hub_icon() owns that
                canvas and its drawing primitives; DBGate only tells it
                what state to reflect.
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
        ever called from this module's own Test-Connection-success and
        field-edit-invalidation call sites).

        is_connected is informational only for callers' own clarity at
        the call site -- this method does not itself decide _db_state;
        by the time it is called, the caller (a Test Connection success
        handler or the shared edit listener) has already performed the
        actual _db_state transition. This method's only job is to call
        refresh() so the UI catches up to whatever _db_state now is.
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
# HUB ICON
# ============================================================
def create_hub_icon(parent_frame, icons_dir, update_btn, update_map_btn,
                     update_btn_normal_bg, update_map_btn_normal_bg,
                     is_any_tool_active_fn, is_automation_busy_fn,
                     on_click):
    """
    Builds the runtime DB-status hub icon (loaded from
    Path(icons_dir) / "database.png", the same file already shipped in
    icons/ alongside every other tool icon -- see project confirmation;
    loaded via the same Image.open()+ImageTk.PhotoImage pattern every
    other icon in MAIN.py uses, no custom fallback added for a missing
    file, matching existing convention exactly) and constructs the
    DBGate that goes with it.

    Does NOT pack/place the returned canvas -- placement between
    button_frame and btn_frame is MAIN.py's own layout decision (this
    module has no knowledge of those two frames' existence), matching
    this module's leaf-level, no-layout-opinions role.

    Does NOT draw the three connector lines to the grid panel / Update
    buttons here at construction time -- see draw_hub_connectors()
    below, a separate function MAIN.py calls once those widgets'
    positions are final (after packing), since Canvas.create_line()
    needs real winfo_rootx/rooty/width/height values that do not exist
    until the widgets have actually been laid out.

    Args:
        parent_frame: the Tk container the hub canvas will live in
            (MAIN.py packs the returned canvas into this itself; passed
            here only so the canvas is created with the right master).
            The hub_canvas itself is created with NO explicit bg= --
            it inherits parent_frame's own background (btn_frame in
            MAIN.py, which itself has no explicit bg= either, so both
            end up matching Tk's default system color). An earlier
            version of this function hardcoded bg="#afd0f7" (the
            Feature Management Tools grid's own blue), which visibly
            mismatched btn_frame's actual (unset, default) background
            -- producing a visible blue square behind the hub icon
            rather than a blended-in one. Leaving bg unset here fixes
            that by letting the canvas simply match whatever its real
            parent's background actually is, instead of assuming a
            color that was never btn_frame's own.
        icons_dir: str or Path -- MAIN.py's existing ICONS_DIR. This
            module only ever joins filenames onto it; it never resolves
            resource_path() itself (kept out of this module to avoid a
            reverse dependency on MAIN.py's own resolution logic). The
            hub swaps between three icon files in this directory:
            "database.png" (neutral -- shown while BUSY, since busy is
            orthogonal to DB state and must not visually claim
            connected/disconnected), "database_connected.png" (shown
            when not busy and VERIFIED), and "database_disconnected.png"
            (shown when not busy and not VERIFIED). All three are
            loaded via the same Image.open()+ImageTk.PhotoImage pattern
            every other icon in MAIN.py uses, no custom fallback added
            for a missing file, matching existing convention exactly.
        update_btn, update_map_btn, update_btn_normal_bg,
            update_map_btn_normal_bg: passed straight through to the
            constructed DBGate -- see DBGate.__init__ for what each is
            used for. The Feature Management Tools icon grid is
            deliberately NOT a parameter here anymore -- DBGate no
            longer touches it at all (see DBGate's own class
            docstring).
        is_any_tool_active_fn, is_automation_busy_fn: zero-arg
            callables -> bool, passed straight through to DBGate AND
            used directly here for the hub's own click-gating (see
            module docstring -- hub_clickable = not busy, independent
            of DB state).
        on_click: zero-arg callable MAIN.py supplies, invoked when the
            hub icon is clicked while not busy (MAIN.py wires this to
            open show_configure_db_dialog(...) with the constructed
            gate in scope).

    Returns:
        (DBGate, tk.Canvas): the gate instance and the hub's own
        canvas widget, for MAIN.py to pack/place and to pass into
        draw_hub_connectors() once layout is final.
    """
    def _load_hub_icon(filename):
        """Loads one of the three hub icon states from icons_dir, same
        Image.open()+resize()+ImageTk.PhotoImage pattern every other
        icon in MAIN.py uses. Returns None (not a fallback image) if
        the file cannot be loaded -- see the module-level note on
        matching existing convention rather than inventing a new
        missing-asset fallback."""
        try:
            pil_img = Image.open(str(Path(icons_dir) / filename)).resize(
                (39, 39), Image.Resampling.LANCZOS
            )
            return ImageTk.PhotoImage(pil_img)
        except Exception:
            return None

    hub_img_neutral = _load_hub_icon("database.png")
    hub_img_connected = _load_hub_icon("database_connected.png")
    hub_img_disconnected = _load_hub_icon("database_disconnected.png")

    hub_canvas = Canvas(parent_frame, width=48, height=48,
                         highlightthickness=0)
    # Keep references so none of the three PhotoImages are garbage-
    # collected the moment this function returns -- same pattern
    # force_png_icon() already uses in MAIN.py (win._icon_ref = img).
    hub_canvas._hub_img_refs = (hub_img_neutral, hub_img_connected, hub_img_disconnected)
    hub_icon_item = hub_canvas.create_image(2, -2, anchor="nw", image=hub_img_neutral)

    def _set_hub_visual(connected, busy):
        """Reflects (connected, busy) on the hub canvas by swapping
        which of the three loaded icon images is shown -- NOT by
        changing the canvas background color. Busy always takes visual
        precedence and shows the neutral database.png (busy is
        orthogonal to DB state per the module docstring's invariant --
        hub_clickable = not busy, independent of connected/disconnected
        -- so its icon must not claim either color while an operation
        is running). Not-busy shows database_connected.png or
        database_disconnected.png depending on connected. Falls back
        to whichever of the three images actually loaded if one is
        None (see _load_hub_icon's own missing-file behavior) rather
        than crashing on a missing asset."""
        if busy:
            target_img = hub_img_neutral
        elif connected:
            target_img = hub_img_connected
        else:
            target_img = hub_img_disconnected

        if target_img is not None:
            hub_canvas.itemconfig(hub_icon_item, image=target_img)

    gate = DBGate(
        update_btn=update_btn,
        update_map_btn=update_map_btn,
        update_btn_normal_bg=update_btn_normal_bg,
        update_map_btn_normal_bg=update_map_btn_normal_bg,
        is_any_tool_active_fn=is_any_tool_active_fn,
        is_automation_busy_fn=is_automation_busy_fn,
        hub_set_visual_fn=_set_hub_visual,
    )

    def _on_hub_click(event=None):
        # hub_clickable = not busy, independent of DB state -- see
        # module docstring. The hub is gated because an operation is
        # in progress, never because the database happens to be
        # disconnected; it must always remain the way OUT of a
        # disconnected state.
        busy = bool(is_any_tool_active_fn()) or bool(is_automation_busy_fn())
        if busy:
            return
        on_click()

    hub_canvas.bind("<Button-1>", _on_hub_click)

    # Establish the hub's initial visual immediately (session starts
    # either VERIFIED or DB_LESS/UNVERIFIED depending on the startup
    # path MAIN.py just completed -- refresh() reads _db_state[0]
    # fresh, so this reflects whatever state is already current by the
    # time create_hub_icon() runs).
    gate.refresh()

    return gate, hub_canvas


def draw_hub_connectors(hub_canvas, grid_panel_widget, update_map_btn, update_btn):
    """
    Draws the three connector lines from the hub icon toward (a) the
    bottom-center of the Feature Management Tools grid's background
    panel, (b) the Update Map button, (c) the Update Database button,
    using Canvas.create_line() against each target's CURRENT runtime
    bounding box (winfo_rootx/rooty/width/height) so routing stays
    correct across window sizes/DPI, per this task's own spec.

    MAIN.py is expected to call this once, after all four widgets have
    been packed and the window has been sized (i.e. after a
    root.update_idletasks() following packing) -- calling it before
    winfo_* values are meaningful will draw degenerate (0,0) lines.
    This function does not itself schedule a retry or call
    update_idletasks(); that sequencing decision belongs to MAIN.py's
    own layout code, not this leaf module.

    Any pre-existing connector lines drawn by a previous call are NOT
    tracked or cleared here -- MAIN.py is expected to call this exactly
    once per session (the four target widgets do not change identity
    during a session), matching the Prompt doc's description of a
    one-time runtime draw rather than a repeated redraw. If a future
    need arises to redraw on a live resize, that is a new, separate
    requirement, not something this function guesses at now.

    Args:
        hub_canvas: the tk.Canvas returned by create_hub_icon().
        grid_panel_widget: the Feature Management Tools grid's
            background panel widget (MAIN.py's button_frame or
            equivalent), whose bottom-center is one connector's target.
        update_map_btn, update_btn: the two existing tk.Button widgets.
    """
    hub_canvas.update_idletasks()

    hub_x = hub_canvas.winfo_rootx()
    hub_y = hub_canvas.winfo_rooty()
    hub_w = hub_canvas.winfo_width()
    hub_h = hub_canvas.winfo_height()

    # Anchor points on the hub's own three nub stems: top, left, right
    # of the icon, in CANVAS-LOCAL coordinates (create_line draws in
    # the hub_canvas's own coordinate space).
    nub_top = (hub_w // 2, 0)
    nub_left = (0, hub_h // 2)
    nub_right = (hub_w, hub_h // 2)

    def _target_point_in_hub_coords(widget, anchor):
        """Converts a target widget's screen-space anchor point
        (anchor is one of "bottom-center", "left-center") into the
        hub_canvas's own local coordinate space, since create_line()
        on hub_canvas needs coordinates relative to hub_canvas, not
        screen-absolute ones."""
        wx = widget.winfo_rootx()
        wy = widget.winfo_rooty()
        ww = widget.winfo_width()
        wh = widget.winfo_height()
        if anchor == "bottom-center":
            sx, sy = wx + ww // 2, wy + wh
        elif anchor == "left-center":
            sx, sy = wx, wy + wh // 2
        else:
            sx, sy = wx + ww // 2, wy + wh // 2
        return (sx - hub_x, sy - hub_y)

    grid_target = _target_point_in_hub_coords(grid_panel_widget, "bottom-center")
    update_map_target = _target_point_in_hub_coords(update_map_btn, "left-center")
    update_db_target = _target_point_in_hub_coords(update_btn, "left-center")

    line_color = "#5a7fa8"
    hub_canvas.create_line(nub_top[0], nub_top[1], grid_target[0], grid_target[1],
                            fill=line_color, width=1)
    hub_canvas.create_line(nub_left[0], nub_left[1], update_map_target[0], update_map_target[1],
                            fill=line_color, width=1)
    hub_canvas.create_line(nub_right[0], nub_right[1], update_db_target[0], update_db_target[1],
                            fill=line_color, width=1)


# ============================================================
# COMBINED STARTUP DIALOG
# ============================================================
_ALL_FIVE_CONFIRM_MSG = (
    "You need to test the connection first before you can access "
    "features that need the database. Are you sure you want to "
    "continue without database?"
)
_PARTIAL_CONFIRM_TITLE = "Continue without database?"
_PARTIAL_CONFIRM_MSG = (
    "No database connection has been tested yet. "
    "Continue without database?"
)


def is_db_verified():
    """
    Read-only query: True if the CURRENT session's database connection
    is VERIFIED (a successful Test Connection since the last field
    edit -- see the module docstring's DB STATE MACHINE section for
    the full definition), False for UNVERIFIED or DB_LESS.

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


def show_startup_dialog(root, apply_icon_fn, get_credentials_path_fn,
                         resize_file_dialog_fn,
                         on_verified_start, on_dbless_start):
    """
    Shows the combined startup dialog: workspace file picker + the six
    credential fields + Test Connection + Start. Replaces the old
    startup_sequence() native file picker AND the entirety of the old
    show_login_and_connect() Toplevel.

    Args:
        root: the Tk root (used only as the Toplevel's implicit
            parent; this function does not otherwise touch root).
        apply_icon_fn: callable(win) -> None, MAIN.py's existing
            apply_icon(), applied to this dialog the same way every
            other Toplevel in this codebase gets its icon set.
        get_credentials_path_fn: callable, no args, returns the
            pg_credentials.json path (MAIN.py's get_credentials_path,
            imported from utils.db_discovery).
        resize_file_dialog_fn: callable, no args, MAIN.py's existing
            resize_file_dialog() -- started on a background thread by
            THIS function around the Browse button's own
            askopenfilename() call, mirroring the old startup_sequence()
            exactly (this module does the threading.Thread(...).start()
            call itself; resize_file_dialog_fn is just the target).
        on_verified_start: callable(workspace_path: str,
            credentials: dict) -> None. Called after this dialog is
            destroyed when Start is clicked with _db_state[0] ==
            "VERIFIED". MAIN.py wires this to the existing
            launch_global_mapper() chain, DB-enabled path.
        on_dbless_start: callable(workspace_path: str) -> None. Called
            after this dialog is destroyed when the user confirms
            continuing without a database (either Yes/No branch).
            MAIN.py wires this to launch_global_mapper() with its
            db_less flag set, skipping the .gmw patch step entirely.

    Closing via the titlebar X asks for confirmation first
    ("Exit Application" / "Exit Land Valuation Tools?" -- Yes/No), and
    exits the entire process only on Yes. A No leaves the dialog open,
    untouched. This is a confirmation added on top of the old
    dialogs' own Cancel behavior, not a partial-cancel or "go back"
    affordance -- there is still no way to close this dialog and
    return to some earlier state; the only two outcomes remain
    "continue on this dialog" or "exit the whole process."
    """
    import threading
    from tkinter import filedialog

    _db_state[0] = "UNVERIFIED"
    _verified_fields[0] = None

    win = Toplevel(root)
    apply_icon_fn(win)
    win.title("Land Valuation Tools - Startup")
    win.resizable(False, False)
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

    Label(outer, text="Database Information", font=("Segoe UI", 9, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 2)
    )

    saved = _load_saved_credentials(get_credentials_path_fn)
    field_entries = _build_credential_fields(outer, saved, start_row=1)

    def _on_edit():
        status_label.config(text="")

    _bind_edit_invalidation(field_entries, extra_on_edit=_on_edit)

    def _on_test_result(ok, error_message):
        test_btn.config(state="normal")
        _active_test_cancel["fn"] = None
        if ok:
            status_label.config(text="\u2713", fg="#2e7d32")
        else:
            status_label.config(text="\u2717", fg="#b02a2a")
            messagebox.showerror("Connection Failed", error_message)

    _active_test_cancel = {"fn": None}

    def _do_test_connection():
        test_btn.config(state="disabled")
        status_label.config(text="")
        _active_test_cancel["fn"] = _run_test_connection_threaded(
            field_entries, get_credentials_path_fn, win, status_label, _on_test_result)

    test_row = Frame(outer)
    test_row.grid(row=len(_FIELD_LABELS) + 1, column=0, columnspan=2, pady=(4, 10))
    test_btn = Button(test_row, text="Test Connection", command=_do_test_connection)
    test_btn.pack(side="left")
    status_label = Label(test_row, text="\u2717", fg="#b02a2a", width=2, font=("Segoe UI", 11, "bold"))
    status_label.pack(side="left", padx=(6, 0))

    # --- Global Mapper Workspace section ---
    gmw_row = len(_FIELD_LABELS) + 2
    Label(outer, text="Global Mapper Workspace", font=("Segoe UI", 9, "bold")).grid(
        row=gmw_row, column=0, columnspan=2, sticky="w", pady=(4, 0)
    )

    workspace_frame = Frame(outer)
    workspace_frame.grid(row=gmw_row + 1, column=0, columnspan=2, sticky="we", pady=(0, 10))

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

    # --- Start button ---
    def _proceed_dbless():
        win.destroy()
        on_dbless_start(workspace_path_var["path"])

    def _do_start():
        if _active_test_cancel["fn"] is not None:
            # A Test Connection is still in flight (background thread
            # not yet finished) -- per this task's own confirmed
            # design, clicking Start silently cancels it: the spinner
            # stops, status_label shows a plain \u2717, and whatever
            # the background attempt eventually returns is discarded
            # (see _run_test_connection_threaded()'s own cancel()
            # docstring). This runs BEFORE the _db_state[0] check right
            # below, in the same main-thread call, so there is no
            # window in which a same-moment background completion
            # could race ahead of this cancellation -- that
            # completion's own root.after(0, _finish) callback cannot
            # run until this function returns.
            _active_test_cancel["fn"]()
            _active_test_cancel["fn"] = None

        if _db_state[0] == "VERIFIED":
            creds = _read_fields(field_entries)
            win.destroy()
            on_verified_start(workspace_path_var["path"], creds)
            return

        values = _read_fields(field_entries)
        five_filled = all(
            values[k] for k in ("host", "database", "schema", "username", "password")
        )

        if five_filled:
            proceed = messagebox.askyesno("Continue without database?", _ALL_FIVE_CONFIRM_MSG)
        else:
            proceed = messagebox.askyesno(_PARTIAL_CONFIRM_TITLE, _PARTIAL_CONFIRM_MSG)

        if proceed:
            _db_state[0] = "DB_LESS"
            _proceed_dbless()
        # No -> do nothing further; stay on the dialog.

    start_btn = Button(outer, text="START", command=_do_start, state="disabled",
                        bg=_PRIMARY_BTN_DISABLED_BG, fg=_PRIMARY_BTN_DISABLED_FG,
                        cursor="no", font=("Segoe UI", 10, "bold"))
    _bind_primary_button_hover(start_btn)
    start_btn.grid(row=gmw_row + 2, column=0, columnspan=2, sticky="we", pady=(8, 0), ipady=4)

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
def show_configure_db_dialog(root, apply_icon_fn, get_credentials_path_fn, db_gate):
    """
    Shows the mid-session Configure-DB dialog -- the hub icon's click
    target. Same six credential fields + Test Connection as the
    startup dialog's own credential section, but reachable at any time
    during the session, and NEVER exits the process on close.

    A successful Test Connection here immediately commits live (no
    separate "Apply" step): _attempt_test_connection() already sets
    _db_state[0] = "VERIFIED" and writes pg_credentials.json, and this
    function additionally calls db_gate.set_db_connected(True) right
    away so the outer panel (grid icons, Update buttons, hub icon)
    un-gates the instant the test succeeds.

    An edit to any field after a successful test immediately re-gates:
    the shared _bind_edit_invalidation() edit listener collapses
    _db_state[0] to "UNVERIFIED" AND calls db_gate.set_db_connected(False)
    via its extra_on_edit hook, so the outer panel re-gates immediately.

    Closing via Cancel/X preserves whatever the CURRENTLY ACTIVE session
    DB state already was before this dialog opened -- there is nothing
    to roll back or explicitly re-commit on close, since any success
    already committed live the instant it happened, and this dialog's
    own field edits never touch the live session credentials unless a
    Test Connection actually succeeds (see _attempt_test_connection()
    -- it only mutates _db_state/_verified_fields/pg_credentials.json
    on a confirmed success, never merely because a field was typed
    into). Nothing further happens on close beyond destroying the
    Toplevel.

    This dialog affects ONLY CAMA Tools' own database consumers
    (Update Map / Update Database automation, the Feature Management
    Tools subprocesses, pg_credentials.json). It explicitly does NOT
    attempt to modify, relaunch, or repatch the already-running Global
    Mapper instance's own PostGIS connection -- stated directly in this
    dialog's own UI text below, so the user does not assume changing DB
    creds here also reconnects Global Mapper.

    Args:
        root: the Tk root (Toplevel parent only).
        apply_icon_fn: callable(win) -> None, MAIN.py's apply_icon().
        get_credentials_path_fn: callable, no args -> credentials path.
        db_gate: the DBGate instance returned by create_hub_icon().
    """
    win = Toplevel(root)
    apply_icon_fn(win)
    win.title("Configure Database Connection")
    win.resizable(False, False)

    outer = Frame(win)
    outer.pack(padx=10, pady=10)

    Label(outer, text="Database Information", font=("Segoe UI", 9, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 2)
    )

    saved = _load_saved_credentials(get_credentials_path_fn)
    field_entries = _build_credential_fields(outer, saved, start_row=1)

    def _on_edit():
        status_label.config(text="")
        db_gate.set_db_connected(False)

    _bind_edit_invalidation(field_entries, extra_on_edit=_on_edit)

    def _on_test_result(ok, error_message):
        test_btn.config(state="normal")
        if ok:
            status_label.config(text="\u2713", fg="#2e7d32")
            db_gate.set_db_connected(True)
        else:
            status_label.config(text="\u2717", fg="#b02a2a")
            messagebox.showerror("Connection Failed", error_message)

    def _do_test_connection():
        test_btn.config(state="disabled")
        status_label.config(text="")
        _run_test_connection_threaded(field_entries, get_credentials_path_fn,
                                       win, status_label, _on_test_result)

    test_row = Frame(outer)
    test_row.grid(row=len(_FIELD_LABELS) + 1, column=0, columnspan=2, pady=(4, 10))
    test_btn = Button(test_row, text="Test Connection", command=_do_test_connection)
    test_btn.pack(side="left")
    status_label = Label(
        test_row,
        text="\u2713" if _db_state[0] == "VERIFIED" else "\u2717",
        fg="#2e7d32" if _db_state[0] == "VERIFIED" else "#b02a2a",
        width=2, font=("Segoe UI", 11, "bold"),
    )
    status_label.pack(side="left", padx=(6, 0))

    Label(
        outer,
        text="Note: changing the database connection here does not\n"
             "reconnect or affect the already-running Global Mapper\n"
             "instance. Global Mapper's own connection is set once,\n"
             "at launch, and is unaffected by this dialog.",
        fg="#666666", justify="left", font=("Segoe UI", 8),
    ).grid(row=len(_FIELD_LABELS) + 2, column=0, columnspan=2, pady=(0, 8), sticky="w")

    Button(outer, text="Close", command=win.destroy).grid(
        row=len(_FIELD_LABELS) + 3, column=0, columnspan=2
    )