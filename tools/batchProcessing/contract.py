"""
tools/batchProcessing/contract.py

PURPOSE:
    Single source of truth for two things every other file in this
    package, and every one of the 11 tools/<name>.py files' own
    batch-mode adaptation (this task's remaining per-file chats),
    reads from -- never duplicated a second time anywhere else:

    1. The PER-TOOL BATCH-MODE CONTRACT itself (see the module-level
       docstring block below) -- the exact shape each of the 11
       tools/<name>.py files' own open_main_window() must additionally
       support so tools/batchProcessing/checklist.py can drive it.
    2. BATCH_TOOL_MODULES / TOOL_ORDER / SHORT_LABELS -- the dispatch
       table and grid ordering, mirrored from MAIN.py's own
       TOOL_MODULES / buttons_1st_row / buttons_2nd_row (re-verified
       against the current MAIN.py, not assumed from any earlier
       description, at the time this file was written).

    Deliberately NOT duplicated here from MAIN.py by import: this
    module runs as part of tools.batchProcessing, itself dispatched as
    its own subprocess/thread exactly like every one of the 11 tool
    files -- and, like them, cannot import a running MAIN.py
    instance's own module-level state. See MAIN.py's own
    dispatch_tool_if_requested() / run_tool_by_label() for the
    mechanism this mirrors.

PER-TOOL BATCH-MODE CONTRACT (what each of the 11 tools/<name>.py
files' own open_main_window() must additionally support, so
tools/batchProcessing/checklist.py's own Edit-button flow can drive it --
see Batch_Valuation_Prompt.txt Section 1 for the full generalized-
change description; this is the CONCRETE shape this package was built
against, and the shape each per-file chat that follows will be told to
match):

    def open_main_window(
        root,
        db_verified=True,
        batch_mode=False,        # NEW, default False -- non-batch
                                  # (normal icon-grid) callers are
                                  # completely unaffected: same
                                  # signature default, same behavior.
        initial_config=None,     # NEW, dict | None -- pre-fills every
                                  # batch-visible field when re-opening
                                  # a previously-saved batch entry. No
                                  # initial_config is passed the first
                                  # time a tool is edited this session
                                  # -- that tool's own normal defaults
                                  # apply.
        on_save=None,             # NEW, callable(config: dict) -> None.
                                  # Called with a plain, JSON-
                                  # serializable dict (strings/
                                  # numbers/booleans/lists of these
                                  # only -- no Tkinter Variable
                                  # objects, no widget references) of
                                  # every CURRENTLY VISIBLE field
                                  # except Land Parcel Source / Output
                                  # Destination (batch mode hides
                                  # those -- the orchestrator owns
                                  # them globally instead, see
                                  # pickers.py). Works even if the
                                  # form is currently incomplete by
                                  # that tool's own normal readiness
                                  # check -- batch mode never blocks
                                  # Save on completeness. Every one of
                                  # the 11 tools' own batch-mode
                                  # window builds its Save/Cancel row
                                  # via the shared
                                  # utils.batch_mode_ui.
                                  # build_save_cancel_row() helper
                                  # (Rule of Three -- Instructions
                                  # Section C/G.5), which calls
                                  # on_save and THEN shows a blocking
                                  # "Saved" confirmation dialog, but
                                  # deliberately does NOT close the
                                  # window afterward -- Save behaves
                                  # like Apply, safe to click any
                                  # number of times; only Cancel or
                                  # the window's own titlebar X
                                  # actually closes it (see on_cancel
                                  # below).
        on_cancel=None,           # NEW, callable() -> None. Called
                                  # once, right before the window
                                  # actually closes, when the user
                                  # clicks Cancel OR the titlebar X --
                                  # build_save_cancel_row() wires both
                                  # to the same handler. If the
                                  # window's current state differs
                                  # from whatever was last Saved (or
                                  # from its opening state, if never
                                  # Saved this time), the user is
                                  # asked to confirm first
                                  # ("Unsaved changes will not be
                                  # applied. Cancel anyway?"); if it's
                                  # identical -- including the common
                                  # case of a Browse/Select dialog
                                  # opened and then dismissed without
                                  # picking anything -- the window
                                  # closes immediately, no dialog.
                                  # Nothing the orchestrator holds for
                                  # that tool changes as a RESULT of
                                  # Cancel itself; if a Save already
                                  # happened earlier in the same Edit
                                  # session, that already-committed
                                  # config is simply left as-is.
    ):
        ...

    In batch_mode=True: the "Land Parcel Source" and "Output
    Destination" sections are NOT SHOWN (not merely disabled); "Run
    Processing" is replaced by a Cancel/Save row built via
    utils.batch_mode_ui.build_save_cancel_row() (see on_save/on_cancel
    above for its full open-until-Cancelled lifecycle). That same call
    also centers the window on screen, matching the convention already
    used by core/startup_and_db_ui.py's own Configure Database dialog
    and startup workspace picker.

    Each adapted tool module ALSO exposes, at module level, callable
    WITHOUT any window open:

        def is_batch_config_complete(config: dict) -> bool:
            ...

    returning the SAME boolean logic as that tool's own existing
    Run-button readiness check (its Land Parcel Source / Output
    Destination portion excluded, since batch mode never captures
    those). This is what powers the "Incomplete" overlay
    (tools/batchProcessing/checklist.py) -- reusing each tool's own
    existing definition of "ready to run" rather than a second,
    possibly-divergent one.


    Until a given tool's own file is adapted to this contract, this
    package degrades gracefully (see state.py's _load_module() /
    _tool_is_complete()): Edit shows a "not yet available" message
    instead of crashing or falling back to that tool's NORMAL
    (non-batch) window, and that tool is always treated as Incomplete
    if checked -- conservative, never silently lets an unconfigured
    tool count as ready.

DEPENDENCIES:
    None -- pure data + docstring. No tkinter, no local imports. Every
    other file in this package imports FROM this one, never the
    reverse.

SIDE EFFECTS:
    None. Importing this module does nothing beyond binding the module
    -level names below.
"""

# ========================================
# TOOL DISPATCH TABLE
# ========================================
# Mirrors MAIN.py's own TOOL_MODULES, re-verified against the actual
# current MAIN.py (not assumed from any earlier description) at the
# time this package was written.
BATCH_TOOL_MODULES = {
    "INFLUENCE MAP TO LAND PARCEL": "tools.influence_map_to_land_parcel",
    "INFLUENCE MAP DISTANCE TO LAND PARCEL": "tools.influence_map_distance_to_land_parcel",
    "ROAD WIDTH": "tools.road_width",
    "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO": "tools.road_frontage",
    "LOT LOCATION": "tools.lot_location",
    "LAND SHAPE": "tools.land_shape",
    "METERS FROM (SCHOOL, SHOP, TRANSPORT, CHURCH)": "tools.meters_from_school_shop_transport_church",
    "LANDMARKS WITHIN METERS": "tools.landmarks_within_meters",
    "PARCEL TERRAIN LEVEL": "tools.terrain",
    "ROAD DENSITY": "tools.road_density",
    "ROAD SURFACE": "tools.road_surface",
}

# Exact row split/order MAIN.py's own buttons_1st_row / buttons_2nd_row
# use (re-verified against the current file, not assumed) -- the
# Batch Processing grid mirrors this so a tool's position here matches
# its position in the familiar Feature Management Tools icon grid.
ROW_1 = [
    "INFLUENCE MAP TO LAND PARCEL",
    "INFLUENCE MAP DISTANCE TO LAND PARCEL",
    "ROAD WIDTH",
    "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO",
    "LOT LOCATION",
    "LAND SHAPE",
]
ROW_2 = [
    "METERS FROM (SCHOOL, SHOP, TRANSPORT, CHURCH)",
    "LANDMARKS WITHIN METERS",
    "PARCEL TERRAIN LEVEL",
    "ROAD DENSITY",
    "ROAD SURFACE",
]
TOOL_ORDER = ROW_1 + ROW_2

# Short display labels for the rectangle grid -- the real
# BATCH_TOOL_MODULES keys are long (e.g. "METERS FROM (SCHOOL, SHOP,
# TRANSPORT, CHURCH)") and would force very wide rectangles if shown
# verbatim. Purely cosmetic; every lookup elsewhere in this package
# still uses the real, full label (the BATCH_TOOL_MODULES key), never
# this shortened text, so there is no risk of drift between the two.
SHORT_LABELS = {
    "INFLUENCE MAP TO LAND PARCEL": "Influence Map\nto Land Parcel",
    "INFLUENCE MAP DISTANCE TO LAND PARCEL": "Influence Map\nDistance",
    "ROAD WIDTH": "Road Width",
    "ROAD FRONTAGE & DEPTH-TO-WIDTH RATIO": "Road Frontage &\nDepth-to-Width",
    "LOT LOCATION": "Lot Location",
    "LAND SHAPE": "Land Shape",
    "METERS FROM (SCHOOL, SHOP, TRANSPORT, CHURCH)": "Meters From\n(School/Shop/etc.)",
    "LANDMARKS WITHIN METERS": "Landmarks\nWithin Meters",
    "PARCEL TERRAIN LEVEL": "Parcel Terrain\nLevel",
    "ROAD DENSITY": "Road Density",
    "ROAD SURFACE": "Road Surface",
}