"""
utils/db_discovery.py

PURPOSE:
    Shared database-discovery helpers, extracted from all 11 registered
    CAMA Tools tool modules as a Category B standardization.

    This module's responsibility is the full chain a tool needs before
    it can work against the database at all -- not any single function
    in isolation:

        filesystem lookup (find pg_credentials.json)
            -> credentials (read + validate it)
            -> database connection (using those credentials)
            -> database discovery (what tables actually exist there)

    _get_credentials_path() and load_db_credentials() exist here because
    they are prerequisites of fetch_tables(), not separate concerns
    bundled in for convenience -- a tool cannot discover what tables are
    available without first knowing how to connect, and it cannot
    connect without first locating and validating its credentials. The
    module is named for that whole responsibility -- helping a tool
    discover what it can access in the database -- rather than for any
    one function's implementation detail.

    Unlike the earlier Category A extractions (table_name_matching.py,
    resource_path.py), this module does NOT preserve every tool's
    existing behavior unchanged -- a fresh line-by-line diff across all
    11 tools found 5 distinct behavioral variants for
    load_db_credentials() and 3 distinct error-handling behaviors for
    fetch_tables(), which disqualified this pair from a pure,
    behavior-preserving Category A extraction. This module represents an
    explicit, approved Category B decision: standardize all 11 tools on
    ONE canonical behavior (the implementation previously used by
    influence_to_barangay.py / influence_to_map.py), intentionally
    changing the other 9 tools' observable behavior to match it.

    Approved, intentional behavioral changes from this standardization
    (not silent "while we're here" fixes -- each was explicitly
    identified and approved before this module was written):

    1. terrain.py previously called json.load() without wrapping it in a
       try/except, so a malformed (but present) pg_credentials.json
       would raise an uncaught JSONDecodeError and crash the tool. The
       canonical load_db_credentials() below catches this via its own
       try/except, so terrain.py now fails gracefully with a
       "Credential Error" dialog like every other tool, instead of
       crashing.

    2. road_frontage.py previously resolved its credentials file path
       via a bare, working-directory-relative constant
       (CREDENTIALS_FILE = "pg_credentials.json", resolved by whatever
       the process's current working directory happened to be at the
       time open() was called) -- fragile in a frozen/installed
       deployment, since CWD at launch time is not guaranteed to equal
       the executable's own directory (e.g. a shortcut with a different
       "Start in" folder, or a different launcher). Every other tool
       already resolved the path deterministically, relative to the
       running executable's own location (frozen) or the script file's
       own location (dev mode) -- see _get_credentials_path() below, now
       the single implementation all 11 tools share. This intentionally
       replaces road_frontage.py's working-directory-dependent behavior
       with the more robust, location-based lookup already used
       everywhere else.

    Both of the above are deliberate standardization decisions, not
    accidental bug fixes bundled into an unrelated extraction.

    Planned future architecture, for context on why this module's scope
    is deliberately narrow (see backlog notes for db_engine.py):
        utils/
            db_discovery.py   <- this module: locate credentials,
                                 connect, discover available tables
            db_engine.py       <- future: centralized engine creation,
                                 timeout
            db_schema.py        <- future: column/schema helpers
            db_queries.py        <- future: reusable SQL
            db_output.py          <- future: overwrite-confirmation
                                 dialogs

    NON-TECHNICAL ERROR WORDING (added by the database-error-messages
    task): this module is also the single home of the short, plain-
    English wording shown to a non-technical user for a database
    connection failure -- see friendly_connection_error_message() below.
    It is deliberately placed here rather than in core/startup_and_db_ui.py
    because the 11 tool files (and tools/batchProcessing/pickers.py) must
    never import anything from core/ -- only utils/ modules are shared
    across the core/tools boundary. core/startup_and_db_ui.py imports
    this function from here instead of keeping its own private copy.

INPUTS:
    _get_credentials_path() / get_credentials_path(): none.
    load_db_credentials(): none (reads pg_credentials.json from the path
    _get_credentials_path() resolves).
    fetch_tables(schema): schema (str) -- the PostGIS/PostgreSQL schema
    name to list tables from.
    friendly_connection_error_message(e, context="saved"): e -- the
    caught Exception from a psycopg2.connect() (or SQLAlchemy-wrapped
    psycopg2) call. context -- "saved" (the operation is using
    information already saved in Database Management, e.g. a tool's
    Database Table picker or the Update Map / Update Database
    pre-check) or "entered" (the user just typed the values into the
    Configure Database / Database Management dialog and is testing
    them). Any other value (including None or a typo) is treated the
    same as "saved" -- this function never raises over an unrecognized
    context.

OUTPUTS:
    _get_credentials_path() -> str: absolute path to pg_credentials.json
    under %APPDATA%\\CAMA-Tools.
    get_credentials_path() -> str: public wrapper, identical output --
    the only entry point external modules (e.g. MAIN.py) should use;
    _get_credentials_path() stays "private" by convention/naming.
    load_db_credentials() -> dict | None: the parsed, validated
    credentials dict (host, port, database, username, password, schema),
    or None if the file is missing, malformed, or missing a required key.
    A short, non-technical Tkinter messagebox dialog is shown before
    returning None; no raw exception text or file path ever appears in
    that dialog (the raw text, when there is one, is printed to stderr
    instead -- this module has no logging facility of its own).
    fetch_tables(schema) -> list[str] | None: table names found in the
    given schema (possibly an empty list, when the schema genuinely has
    no tables), or None when the table list could not be obtained at
    all (missing/invalid credentials, or a connection/query failure).
    On None, a single non-technical messagebox dialog has ALREADY been
    shown (either by load_db_credentials() for a credentials problem,
    or by fetch_tables() itself for a connection/query problem) --
    callers must not show a second dialog for a None return. An empty
    list ([]) is a successful result and never accompanied by a dialog.
    friendly_connection_error_message(e, context="saved") -> str: a
    short, non-technical, English-only message with no exception class
    names, driver wording, hostnames, ports, usernames, or trailing
    "try again" / "contact support" instruction -- safe to show directly
    in a messagebox.

DEPENDENCIES:
    os, sys, json, shutil, tkinter.messagebox (stdlib). psycopg2
    (third-party -- PostgreSQL driver).

SIDE EFFECTS:
    Reads pg_credentials.json from disk. Creates %APPDATA%\\CAMA-Tools
    if it doesn't exist yet, and may perform a one-time, best-effort
    copy of a legacy exe-relative/script-relative pg_credentials.json
    into that folder (see _get_credentials_path()'s own docstring).
    Opens (and closes) a live PostgreSQL/PostGIS network connection via
    psycopg2. Shows a Tkinter messagebox error dialog on any
    credential, connection, or query failure. Prints raw exception text
    to stderr (never to a dialog) when a credentials file cannot be
    read or a connection/query fails. No side effects occur at import
    time -- all of the above happens only when these functions are
    actually called.
"""
import os
import sys
import json
import shutil
from tkinter import messagebox

import psycopg2


def _get_credentials_path():
    """
    Resolves the absolute path to pg_credentials.json under the
    per-user %APPDATA%\\CAMA-Tools folder (NEVER relative to the
    executable/script location or the current working directory --
    see module docstring and task history: this file used to live
    next to the running exe/script, which is what this function used
    to resolve to before the %APPDATA% relocation).

    Side effects:
        - Creates %APPDATA%\\CAMA-Tools (os.makedirs(..., exist_ok=True))
          if it doesn't exist yet.
        - One-time, best-effort migration: if pg_credentials.json isn't
          present yet at the new %APPDATA% location but IS present at
          the legacy exe-relative/script-relative location this app
          used before the relocation, it is copied (not moved) into
          the new location, so an existing user upgrading from the old
          version keeps their saved DB credentials instead of being
          forced to re-enter them. An existing file at the new
          location is NEVER overwritten by this migration. A failure
          during the copy (permissions, locked file, etc.) is silently
          ignored -- the caller (load_db_credentials()) then behaves
          exactly as it would for a fresh install with no credentials
          file yet.

    Raises:
        RuntimeError: if the APPDATA environment variable is not set.
        No fallback location is used in that case -- the purpose of
        this function is specifically to stop resolving to an
        exe-relative/script-relative path, so silently falling back to
        one would defeat that.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError(
            "The Windows APPDATA environment variable is not available, "
            "so CAMA Tools cannot determine where to store its configuration files."
        )
    base_dir = os.path.join(appdata, "CAMA-Tools")
    os.makedirs(base_dir, exist_ok=True)
    new_path = os.path.join(base_dir, "pg_credentials.json")

    if not os.path.exists(new_path):
        # Legacy location this function used to resolve to, before the
        # %APPDATA% relocation -- used here ONLY to locate a file to
        # migrate, never returned as the live path anymore.
        if getattr(sys, "frozen", False):
            legacy_dir = os.path.dirname(sys.executable)
        else:
            legacy_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        legacy_path = os.path.join(legacy_dir, "pg_credentials.json")
        if os.path.exists(legacy_path):
            try:
                shutil.copy2(legacy_path, new_path)
            except Exception:
                # Best-effort migration only -- see docstring. A copy
                # failure here is not fatal; load_db_credentials() will
                # simply treat this as a missing-credentials case.
                pass

    return new_path


def get_credentials_path():
    """
    Public wrapper around _get_credentials_path(), for callers outside
    this module (currently: MAIN.py) that need the resolved
    pg_credentials.json path without importing a leading-underscore
    "private" function across a module boundary. Behavior is identical
    to _get_credentials_path() -- see its docstring for the full
    contract (including the migration side effect and the RuntimeError
    case).
    """
    return _get_credentials_path()


def friendly_connection_error_message(e, context="saved"):
    """
    Translates a psycopg2 (or SQLAlchemy-wrapped psycopg2) connection
    exception into a short, non-technical message safe to show directly
    in a Tkinter messagebox -- the single shared translator for every
    database-connection failure a non-technical user can see anywhere
    in the application (a tool's Database Table picker, the Update Map
    / Update Database pre-check, and the Configure Database / Database
    Management dialog's own connection test).

    psycopg2 does not expose a structured, reliable error-code
    attribute for every failure mode a plain psycopg2.connect() call
    can raise, so this matches on the exception's own message text --
    the same approach any non-technical-facing wrapper around a
    driver-level exception has to take when the driver's own message is
    the only signal available. Matching is case-insensitive substring
    matching against str(e).lower(), which also works unchanged when
    psycopg2's exception arrives wrapped by SQLAlchemy (e.g.
    "(psycopg2.OperationalError) connection to server ... failed:
    FATAL: password authentication failed ...") since the original
    driver wording is still present inside the wrapped text.

    The two contexts produce different wording for the same underlying
    failure, because the two situations are different for the user:
    "saved" means the operation is using information already saved in
    Database Management (so the fix is to go open Database Management),
    while "entered" means the user is inside the Database Management
    (Configure Database) dialog right now, testing values they just
    typed (so "you entered" is the accurate description). Any context
    value other than the literal string "entered" (including None, an
    empty string, or a typo) is treated as "saved" -- this function
    never raises over an unrecognized context.

    Matching order (checked in this exact sequence -- order matters,
    since some patterns are substrings of what other failures can also
    contain):
        1. "authentication failed" in text, OR ('role "' in text AND
           "does not exist" in text)
           -> wrong username/password. This ordering is what fixes a
           real defect: PostgreSQL's password-auth failure text does
           NOT reveal whether the role itself is unknown or the
           password is simply wrong (both say "password authentication
           failed for user ..."), and a separate, unknown-role failure
           can independently say `role "x" does not exist` -- both
           must map to the credentials message, and checking this rule
           BEFORE the database-name rule below is what keeps
           `role "x" does not exist` from being misread as an unknown
           database name.
        2. "database" in text AND "does not exist" in text
           -> database name not found.
        3. "invalid integer value" in text OR "invalid port" in text
           -> invalid port value.
        4. "could not translate host name" in text, OR
           "name or service not known" in text, OR
           "unknown host" in text, OR "no such host" in text, OR
           "getaddrinfo" in text
           -> host not found / connection refused wording.
        5. "connection refused" in text OR "actively refused" in text
           -> host not found / connection refused wording (same
           message as rule 4 -- from the user's point of view, an
           unreachable host and a refused connection look identical:
           "something about the saved/entered connection information
           is wrong").
        6. "timeout" in text OR "timed out" in text
           -> the database did not respond in time. Identical wording
           in both contexts (E.4) -- a slow/unreachable server is not
           attributed to a typo the user made.
        7. "no pg_hba.conf entry" in text
           -> the server rejected the connection. Identical wording in
           both contexts (E.4) -- a server-side access rule is not
           something either context's wording implies the user can fix
           by re-checking what they saved or typed.
        8. none of the above matched
           -> generic fallback: "Could not connect to the database."
           (identical wording in both contexts -- deliberately vague
           when the specific cause is unknown, rather than guessing).

    Args:
        e: the caught Exception from psycopg2.connect() (or a
            SQLAlchemy-wrapped connection/query failure that still
            contains the original psycopg2/libpq text).
        context (str): "saved" or "entered" -- see above. Defaults to
            "saved". Any other value is treated as "saved".

    Returns:
        str: a short, user-facing, English-only message. No exception
        class names, no port numbers, no hostnames, no usernames, no
        driver/SQL wording, and no trailing instruction sentence (e.g.
        no "try again" or "contact your administrator").
    """
    text = str(e).lower()
    entered = (context == "entered")

    if "authentication failed" in text or ('role "' in text and "does not exist" in text):
        if entered:
            return "The username or password you entered is incorrect."
        return "The username or password saved in Database Management is incorrect."

    if "database" in text and "does not exist" in text:
        if entered:
            return "The database name you entered could not be found on the server."
        return "The database name saved in Database Management could not be found on the server."

    if "invalid integer value" in text or "invalid port" in text:
        if entered:
            return "The port you entered is not valid."
        return "The port saved in Database Management is not valid."

    if ("could not translate host name" in text
            or "name or service not known" in text
            or "unknown host" in text
            or "no such host" in text
            or "getaddrinfo" in text
            or "connection refused" in text
            or "actively refused" in text):
        if entered:
            return "Could not connect to the database using the information you entered."
        return "Could not connect to the database using the information saved in Database Management."

    if "timeout" in text or "timed out" in text:
        return "The database did not respond in time."

    if "no pg_hba.conf entry" in text:
        return "The database server did not allow this connection."

    return "Could not connect to the database."


def load_db_credentials():
    """
    Loads and validates pg_credentials.json.

    Returns:
        dict | None: the parsed credentials dict (host, port, database,
        username, password, schema) on success. Returns None -- after
        showing a short, non-technical Tkinter messagebox error dialog
        -- if the file is missing, is not valid JSON, or is missing any
        required key. Dialog titles are unchanged ("Missing
        Credentials", "Invalid Credentials", "Credential Error"); no
        dialog body names a file path, a specific missing key, or
        contains raw exception text -- for the unreadable/malformed
        case the raw exception is printed to stderr instead (this
        module has no logging facility of its own).
    """
    path = _get_credentials_path()
    if not os.path.exists(path):
        messagebox.showerror(
            "Missing Credentials",
            "No database information has been saved in Database Management yet.",
        )
        return None
    try:
        with open(path, "r") as f:
            creds = json.load(f)
        required = ["host", "port", "database", "username", "password", "schema"]
        for key in required:
            if key not in creds:
                messagebox.showerror(
                    "Invalid Credentials",
                    "The database information saved in Database Management is incomplete.",
                )
                return None
        return creds
    except Exception as e:
        print(f"[db_discovery] could not read credentials: {type(e).__name__}: {e}", file=sys.stderr)
        messagebox.showerror(
            "Credential Error",
            "The database information saved in Database Management could not be read.",
        )
        return None


def fetch_tables(schema):
    """
    Connects to the database and lists table names in the given schema.

    Args:
        schema (str): the PostGIS/PostgreSQL schema name to query.

    Returns:
        list[str] | None: table names found in `schema`, ordered by
        name -- an empty list is a successful result when the schema
        genuinely has no tables. Returns None -- after showing exactly
        one short, non-technical Tkinter messagebox error dialog -- when
        the table list could not be obtained at all:
          - credentials are missing/invalid: load_db_credentials() has
            ALREADY shown its own dialog in that case, so this function
            shows nothing further and simply returns None.
          - the connection or query itself fails: this function shows
            one "DB Error" dialog with friendly_connection_error_message
            (e, context="saved"), prints the raw exception to stderr,
            and returns None.
        Callers must treat None as "a dialog has already been shown,
        show nothing more" and must not show a second dialog for it --
        only a genuinely empty list should ever reach the existing
        "No Tables" warning.
    """
    creds = load_db_credentials()
    if not creds:
        return None
    try:
        conn = psycopg2.connect(
            host=creds["host"],
            port=creds["port"],
            dbname=creds["database"],
            user=creds["username"],
            password=creds["password"],
        )
        cur = conn.cursor()
        cur.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema=%s ORDER BY table_name;
        """,
            (schema,),
        )
        tables = [row[0] for row in cur.fetchall()]
        conn.close()
        return tables
    except Exception as e:
        print(f"[db_discovery] fetch_tables failed: {type(e).__name__}: {e}", file=sys.stderr)
        messagebox.showerror("DB Error", friendly_connection_error_message(e, context="saved"))
        return None