"""
utils/db_gate_ui.py

PURPOSE:
    Shared UI helpers for a Feature Management Tool's "Database
    Table"/"Database Table(s)"/"Save to Database"-style Radiobutton
    options, used across all 11 tool files that offer such an option
    (influence_map_distance_to_land_parcel.py,
    influence_map_to_land_parcel.py, land_shape.py,
    landmarks_within_meters.py, lot_location.py,
    meters_from_school_shop_transport_church.py, road_density.py,
    road_frontage.py, road_surface.py, road_width.py, terrain.py).

    Each tool's own main(parent=None, db_verified=True) receives a
    one-time, launch-time snapshot of whether the CAMA Tools session's
    database connection was VERIFIED at the moment that tool was
    spawned (see MAIN.py's run_tool_by_label() and
    core/startup_and_db_ui.py's is_db_verified() for where that
    snapshot comes from and why it is a snapshot, never a live/
    continuously-updated signal). When db_verified is False, each
    tool's own "Database"-style radio button(s) should be disabled --
    this module is the single, shared implementation of exactly how
    that disabling looks and behaves, so all 11 tool files present it
    identically rather than each re-implementing (and potentially
    drifting from) their own version.

    This does NOT replace or duplicate
    utils.db_discovery.load_db_credentials()/fetch_tables()'s own
    existing error handling for a connection that fails or is lost
    AFTER a tool's window has already opened -- that remains fully in
    effect regardless of what this module does; this module only
    prevents a user from choosing the database-backed option in the
    first place when the session's DB was already known-unverified at
    the tool's own launch time.

FUNCTIONS:
    disable_db_radio(radio_button) -- disables ONE Radiobutton widget
        with a "no" cursor (Tk's own universal circle-with-slash
        "prohibited" cursor), matching the same cursor="no" treatment
        core/tool_exclusivity.py already applies to a disabled Feature
        Management Tools icon, and core/startup_and_db_ui.py's Start
        button already applies to itself when disabled -- this module
        follows that same established convention rather than
        inventing a new disabled-cursor treatment. Call sites pass
        each of their own named "db" radio buttons to this one at a
        time (see USAGE below) rather than this module trying to
        discover them on its own -- each tool file already knows
        exactly which of its own widgets are its "Database"-style
        radios (they are already individually named in every tool
        file, e.g. parcel_radio_db, infl_radio_db, out_radio_db), so
        this module's job is only the shared disabling behavior
        itself, not widget discovery.
    attach_no_db_tooltip(widget) -- attaches a small, standalone hover
        tooltip reading "No database connection" to widget. Intended
        to be attached to a Radiobutton right after
        disable_db_radio(widget) disables it, so hovering a grayed-out
        "Database Table"-style option explains WHY it's inert, rather
        than leaving the user to guess. Deliberately NOT the same
        tooltip mechanism MAIN.py's own add_tooltip() uses -- that
        function is tightly coupled to MAIN.py's own window-tracking
        globals (get_cama_size(), get_gm_rect(), the shared
        _active_tooltips registry, root.winfo_screenwidth() against
        MAIN.py's own root) and MAIN.py's own multi-window
        (CAMA + Global Mapper) bounding-rect clamping logic -- none of
        which exists in a standalone tool subprocess's own Tk root.
        This function is a small, self-contained equivalent with no
        such dependencies, suitable for use inside any one of the 11
        tool files on its own.

USAGE (inside a tool file, after building its own named radio button):
    from utils.db_gate_ui import disable_db_radio, attach_no_db_tooltip

    if not db_verified:
        disable_db_radio(parcel_radio_db)
        attach_no_db_tooltip(parcel_radio_db)

DEPENDENCIES:
    stdlib: tkinter (Toplevel, Label -- for the tooltip window only;
    this module does not create or require any Radiobutton itself,
    only accepts one already built by the caller).
    local: none. This module is a leaf, matching
    utils/window_icon.py's and utils/resource_path.py's own
    established leaf-module role in this codebase's utils/ package.
"""
import tkinter as tk


def disable_db_radio(radio_button):
    """
    Disables radio_button (state="disabled") and sets its cursor to
    "no" -- the same disabled-cursor convention already established by
    core/tool_exclusivity.py (a disabled Feature Management Tools
    icon) and core/startup_and_db_ui.py (the Start button when
    disabled). Idempotent -- safe to call on an already-disabled
    button.

    Args:
        radio_button: a tk.Radiobutton widget, already constructed
            with its variable/value/command/text -- this function only
            ever adjusts state and cursor, never any other option.
    """
    radio_button.config(state="disabled", cursor="no")


def attach_no_db_tooltip(widget, text="No database connection"):
    """
    Attaches a small, borderless hover tooltip reading text (default:
    "No database connection") to widget. Self-contained -- creates and
    destroys its own Toplevel per hover cycle, with no shared registry
    or dependency on any other window's position/size, unlike
    MAIN.py's own add_tooltip() (see module docstring for why that
    heavier mechanism is not reused here).

    The tooltip is positioned just below and to the right of the
    widget's own on-screen position at the moment the mouse enters it
    (widget.winfo_rootx()/winfo_rooty() + a small fixed offset) -- it
    does not attempt to clamp itself against screen edges or any other
    window's bounds, since this tooltip's own content is short and
    fixed-size, making an off-screen edge case both unlikely in
    practice (a Radiobutton near the extreme edge of a screen) and low
    -consequence if it ever does occur (the tooltip is dismissed the
    moment the mouse leaves the widget, same as any other tooltip).

    Args:
        widget: any Tkinter widget supporting <Enter>/<Leave> binding
            (a tk.Radiobutton, in this module's own intended usage,
            but nothing here assumes that specifically).
        text: the tooltip's message. Defaults to "No database
            connection" -- the one message this module's own intended
            callers need; overridable for a future caller that needs
            different wording without this module having to grow a
            second, differently-named function for that.
    """
    tooltip_state = {"win": None}

    def _on_enter(event):
        if tooltip_state["win"] is not None:
            return
        tip = tk.Toplevel(widget)
        tip.withdraw()
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tip.configure(bg="#333333")
        tk.Label(
            tip, text=text, bg="#333333", fg="white",
            font=("Segoe UI", 8), padx=6, pady=3,
        ).pack()
        tip.update_idletasks()
        x = widget.winfo_rootx() + 4
        y = widget.winfo_rooty() + widget.winfo_height() + 4
        tip.geometry(f"+{x}+{y}")
        tip.deiconify()
        tooltip_state["win"] = tip

    def _on_leave(event):
        if tooltip_state["win"] is not None:
            try:
                tooltip_state["win"].destroy()
            except Exception:
                pass
            tooltip_state["win"] = None

    widget.bind("<Enter>", _on_enter)
    widget.bind("<Leave>", _on_leave)
