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

INPUTS:
    _get_credentials_path(): none.
    load_db_credentials(): none (reads pg_credentials.json from the path
    _get_credentials_path() resolves).
    fetch_tables(schema): schema (str) -- the PostGIS/PostgreSQL schema
    name to list tables from.

OUTPUTS:
    _get_credentials_path() -> str: absolute path to pg_credentials.json.
    load_db_credentials() -> dict | None: the parsed, validated
    credentials dict (host, port, database, username, password, schema),
    or None if the file is missing, malformed, or missing a required key.
    fetch_tables(schema) -> list[str]: table names found in the given
    schema, or an empty list if credentials are missing/invalid or the
    connection/query fails.

DEPENDENCIES:
    os, sys, json, tkinter.messagebox (stdlib). psycopg2 (third-party --
    PostgreSQL driver).

SIDE EFFECTS:
    Reads pg_credentials.json from disk. Opens (and closes) a live
    PostgreSQL/PostGIS network connection via psycopg2. Shows a Tkinter
    messagebox error dialog on any credential, connection, or query
    failure. No side effects occur at import time -- all of the above
    happens only when these functions are actually called.
"""
import os
import sys
import json
from tkinter import messagebox

import psycopg2


def _get_credentials_path():
    """
    Resolves the absolute path to pg_credentials.json, relative to the
    running executable's own location (PyInstaller-frozen mode) or this
    module's own location (dev mode) -- NEVER relative to the current
    working directory, which is not guaranteed to match either of those
    in every launch scenario (see module docstring).
    """
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "pg_credentials.json")
    else:
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "pg_credentials.json"
        )


def load_db_credentials():
    """
    Loads and validates pg_credentials.json.

    Returns:
        dict | None: the parsed credentials dict (host, port, database,
        username, password, schema) on success. Returns None -- after
        showing a Tkinter messagebox error dialog -- if the file is
        missing, is not valid JSON, or is missing any required key.
    """
    path = _get_credentials_path()
    if not os.path.exists(path):
        messagebox.showerror(
            "Missing Credentials",
            f"⚠️ File not found: {path}\n\n"
            "Please create pg_credentials.json with host, port, database, username, password, and schema.",
        )
        return None
    try:
        with open(path, "r") as f:
            creds = json.load(f)
        required = ["host", "port", "database", "username", "password", "schema"]
        for key in required:
            if key not in creds:
                messagebox.showerror("Invalid Credentials", f"Missing '{key}' in pg_credentials.json")
                return None
        return creds
    except Exception as e:
        messagebox.showerror("Credential Error", str(e))
        return None


def fetch_tables(schema):
    """
    Connects to the database and lists table names in the given schema.

    Args:
        schema (str): the PostGIS/PostgreSQL schema name to query.

    Returns:
        list[str]: table names found in `schema`, ordered by name.
        Returns an empty list -- after showing a Tkinter messagebox error
        dialog -- if credentials are missing/invalid, or if the
        connection or query fails.
    """
    creds = load_db_credentials()
    if not creds:
        return []
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
        messagebox.showerror("DB Error", str(e))
        return []