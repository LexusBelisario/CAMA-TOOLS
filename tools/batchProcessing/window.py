"""
tools/batchProcessing/window.py

PURPOSE:
    Top-level assembly for the Batch Processing window: builds the one
    Toplevel, owns the single state.BatchState instance for this
    window's lifetime, and wires together checklist.SelectionChecklist
    (the two-column, 11-tool selection checklist) and the two
    pickers.py-built global pickers (Land Parcel Source / Output
    Destination), plus a status row and a Start-button row -- per this
    task's own most recent revised UI sketch (a Gemini-inspired 2-
    column mockup), STACKED in this exact order top to bottom:
      1. Land Parcel Source + Output Destination, SIDE BY SIDE in one
         shared row (each keeping its own distinct title -- no
         combined outer title box)
      2. the two-column, 11-tool selection checklist, inside its own
         "boxy" bordered section with an uppercase "SELECT TOOLS TO
         RUN IN ORDER" header and its own "Reorder…" button
      3. a status/message row (its own row, full width)
      4. a Start-button row (its own row, right-aligned, labeled
         "START")
    This is the SECOND deliberate layout revision to this window (the
    first moved the two global pickers to bracket the checklist, one
    above and one below it -- since superseded): per the latest
    explicit request, the two pickers now share ONE row instead,
    matching the Gemini-inspired mockup's own combined "Land Parcel
    Source & Output Destination" section (though deliberately WITHOUT
    that mockup's own outer combined title -- see pickers.py's own
    SourcePicker docstring for why each picker keeps its own title
    instead). The status row and Start-button row remain separate,
    stacked rows.

    Also exposes main(), the SAME module-level entry-point contract
    every one of the 11 tool modules already has (module-level
    main(parent=None, db_verified=False)) -- MAIN.py's own
    dispatch_tool_if_requested() / run_tool_by_label() (re-verified
    against the current file before this package was written) expect
    exactly this shape, regardless of whether the tool module is a
    single .py file or, as here, a package -- tools.batchProcessing is
    imported the same way either way (see __init__.py, which re-
    exports main from this file).

TASK 1 SCOPE (UI SHELL ONLY -- see contract.py's own note): this file
    builds the status row as a STATIC placeholder label ("Ready.") --
    reserved slot for Task 2's own background-thread progress
    reporting, not wired to any real thread here. Run Processing's own
    click handler is a deliberate no-op placeholder; its ENABLED/
    DISABLED state is real (state.BatchState.can_run(), unchanged from
    state.py), but clicking it does nothing yet.

INPUTS:
    main(parent, db_verified): see main()'s own docstring below --
    same contract as every other tool module.

OUTPUTS:
    None from main()/open_main_window() itself (side-effecting only,
    like every other tool module). The BatchState instance this window
    owns lives only in this function's own closure -- discarded the
    instant `win` is destroyed; see state.py's own docstring for why
    this is correct, deliberate behavior for this task.

DEPENDENCIES:
    stdlib: tkinter (ttk).
    local: utils.window_icon (apply_icon), tools.batchProcessing.state
    (BatchState), tools.batchProcessing.checklist (SelectionChecklist),
    tools.batchProcessing.pickers (build_land_parcel_source_picker,
    build_output_destination_picker).

SIDE EFFECTS:
    Builds exactly ONE tk.Toplevel(parent) synchronously and returns
    without blocking (no mainloop()/wait_window() call on that
    Toplevel itself) when parent is not None -- REQUIRED for
    core/tool_exclusivity.py's window-tracking (a before/after diff of
    parent.winfo_children(), confirmed by reading MAIN.py's own
    run_tool_by_label()/run_in_thread()) to correctly identify this as
    "the" Batch Processing window, the same way it already does for
    every other tool. Every window checklist.py's own Edit button and
    Reorder dialog open, and pickers.py's own table-picker dialog, is
    parented on THIS window (win), never on `parent` -- so none of
    them ever appear in that diff or confuse exclusivity tracking (see
    checklist.py's and pickers.py's own docstrings).
"""
import tkinter as tk
from tkinter import ttk

from utils.window_icon import apply_icon

from tools.batchProcessing.state import BatchState
from tools.batchProcessing.checklist import SelectionChecklist
from tools.batchProcessing.pickers import (
    build_land_parcel_source_picker,
    build_output_destination_picker,
)


def main(parent=None, db_verified=False):
    """
    Tool entry point -- the SAME contract every one of the 11 tool
    modules already exposes (module-level main(parent, db_verified),
    confirmed against MAIN.py's own dispatch_tool_if_requested() /
    run_in_thread()). If parent is given (invoked from within another
    running Tk app -- the normal icon-grid launch path), reuses it and
    just opens this window. Otherwise creates and hides a new Tk root
    and enters its own mainloop -- the standalone-subprocess dispatch
    path, mirroring every one of the 11 tool files' own identical
    main() pattern (e.g. road_surface.py's main()).

    Args:
        parent: an existing Tk root to reuse, or None to create one.
        db_verified: bool, passed through from MAIN.py's own launcher.
            One-time launch-time snapshot, not a live/continuously-
            updated signal -- see open_main_window()'s own docstring.
    """
    if parent is not None:
        open_main_window(parent, db_verified=db_verified)
    else:
        root = tk.Tk()
        apply_icon(root, "batchvaluation.ico")
        root.withdraw()
        open_main_window(root, db_verified=db_verified)
        root.mainloop()


def open_main_window(root, db_verified=False):
    """
    Builds and shows the Batch Processing window. See module docstring
    for the full, revised layout spec (Land Parcel Source above the
    checklist, Output Destination below it, status and Start as their
    own separate rows) and the SIDE EFFECTS note on why exactly one
    Toplevel is built here, synchronously, with no blocking call.

    Args:
        root: the parent Tk root this window is opened under.
        db_verified: bool, see main()'s own docstring. Forwarded to
            checklist.SelectionChecklist (for every Edit-opened tool
            window) and to both pickers.py builders (for their own
            "Database"-style radio gating) unchanged.
    """
    win = tk.Toplevel(root)
    apply_icon(win, "batchvaluation.ico")
    win.title("Batch Processing")
    win.resizable(False, False)
    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))

    # ONE BatchState instance for this window's entire lifetime --
    # never persisted, discarded the instant `win` closes (see module
    # docstring OUTPUTS and state.py's own docstring).
    state = BatchState()

    # ================================================================
    # Run Processing readiness -- defined here (before the checklist is
    # built) so it can be handed to SelectionChecklist as its on_change
    # callback, but its BODY references run_btn, which is only
    # assigned further below. Safe: Python resolves a closure's names
    # at CALL time, not definition time, and the only calls to
    # _update_run_state() before run_btn exists would come through
    # on_change -- which SelectionChecklist only invokes in response to
    # a later user action (check/uncheck/Reorder/tool-Save), never
    # during its own construction. The one call before any user
    # interaction (at the very end of this function) happens after
    # run_btn is built.
    # ================================================================
    def _update_run_state():
        run_btn.config(state="normal" if state.can_run() else "disabled")

    # ================================================================
    # SECTION 1: GLOBAL SOURCE/DESTINATION PICKERS -- Land Parcel
    # Source and Output Destination, SIDE BY SIDE in one shared row,
    # above the checklist (see pickers.py's own SourcePicker docstring
    # for the side-by-side packing itself). DEVIATION from an earlier
    # version of this window's own layout (Land Parcel Source above
    # the checklist, Output Destination below it) -- per the latest
    # explicit request, both now share one row, matching the Gemini-
    # inspired mockup's own "Land Parcel Source & Output Destination"
    # combined section (this window deliberately does NOT add that
    # mockup's own OUTER combined title box, though -- each picker
    # keeps its own existing, distinct title ("Land Parcel Source" /
    # "Output Destination") as its own box header; see pickers.py).
    #
    # Neither picker's own selected value is read anywhere in this
    # file -- Task 1 is UI shell only (see module docstring TASK 1
    # SCOPE); Task 2's own batch execution engine is what will
    # eventually read parcel_picker.get_mode()/get_value() and
    # output_picker.get_mode()/get_value().
    # ================================================================
    sources_row = tk.Frame(win)
    sources_row.pack(fill="x", padx=5, pady=(6, 0))
    build_land_parcel_source_picker(sources_row, win, db_verified)
    build_output_destination_picker(sources_row, win, db_verified)

    # ================================================================
    # SECTION 2: TOOL SELECTION CHECKLIST (two columns, its own boxy
    # bordered section, header "SELECT TOOLS TO RUN IN ORDER" +
    # "Reorder…" button -- see checklist.py for the full row/dialog
    # spec)
    # ================================================================
    checklist_frame = tk.Frame(win)
    checklist_frame.pack(fill="x")

    SelectionChecklist(checklist_frame, win, db_verified, state,
                        on_change=_update_run_state)

    # ================================================================
    # SECTION 3: STATUS ROW (its own row, full width)
    # ================================================================
    ttk.Separator(win, orient="horizontal").pack(
        fill="x", padx=10, pady=(12, 4))

    status_row = tk.Frame(win)
    status_row.pack(fill="x", padx=10, pady=(0, 4))

    # Static placeholder text -- reserved slot for Task 2's own
    # background-thread progress reporting (see module docstring TASK
    # 1 SCOPE). No real background thread exists in this task.
    status_var = tk.StringVar(master=win, value="Ready.")
    tk.Label(status_row, textvariable=status_var, fg="gray",
             anchor="w").pack(fill="x")

    # ================================================================
    # SECTION 4: START BUTTON ROW (its own row, right-aligned)
    # ================================================================
    start_row = tk.Frame(win)
    start_row.pack(fill="x", padx=10, pady=(0, 10))

    def _on_run_clicked():
        # TODO (Task 2 -- batch execution engine): running the checked
        # tools in sequence, chaining each one's output into the next
        # one's input, progress reporting (via status_var above),
        # cancel/pause, safe atomic writes between steps, and column-
        # conflict pre-checks are all explicitly OUT OF SCOPE for Task
        # 1 (UI shell only). This handler is a deliberate no-op
        # placeholder -- see Batch_Valuation_Prompt.txt, Task 1 scope.
        pass

    run_btn = tk.Button(start_row, text="START", width=18,
                         command=_on_run_clicked, state="disabled")
    run_btn.pack(side="right")

    # Minimize: standard OS title-bar control on this Toplevel -- no
    # special handling needed, since nothing runs in the background in
    # this task (see Prompt Section 1).
    #
    # Closing (titlebar X): deliberately no WM_DELETE_WINDOW override
    # is bound here, so Tk's own default close behavior applies --
    # immediate, no confirmation dialog. `state` (the BatchState
    # instance) and every widget built above live only in this
    # function's own closures and are garbage-collected the instant
    # `win` is destroyed -- nothing to explicitly clear. Re-opening
    # Batch Processing calls open_main_window() fresh, starting
    # completely over (see module docstring OUTPUTS).

    _update_run_state()

    # Center the window on screen -- deliberately the LAST thing this
    # function does, after every section (pickers, checklist, status
    # row, START row) has been built. CONFIRMED root cause of a real
    # bug when this used to run earlier (inside checklist.py's own
    # __init__, right after the checklist itself but BEFORE this
    # file's own status_row/start_row existed): resetting then
    # re-locking win's own minsize/maxsize to whatever height was
    # required AT THAT EARLIER MOMENT permanently capped the window
    # below the height status_row/start_row would later need once
    # THEY were packed on top -- clipping them invisible regardless of
    # screen resolution or Windows DPI scaling (neither changes this
    # geometric lock). Centering belongs to window.py, not
    # checklist.py, precisely because only this function is in a
    # position to do it AFTER the whole window is actually complete.
    #
    # Runs once per LAUNCH (each press of the Batch Processing icon on
    # MAIN.py's own main panel calls open_main_window() fresh) -- not
    # tied to checklist.py's own refresh() (check/uncheck/Reorder/
    # Save), which must NOT re-trigger this (per earlier explicit
    # correction) since check/uncheck's own row-rebuild no longer
    # changes win's own overall required size anyway (every row has a
    # fixed size -- see checklist.py's own ROW_WIDTH/ROW_HEIGHT).
    #
    # Reset-then-remeasure technique matches utils/batch_mode_ui.py's
    # own build_save_cancel_row() -- avoids relying on winfo_reqwidth()
    # /reqheight() while any earlier lock might still be in effect.
    # Only the window's own on-screen POSITION is set (the "+x+y" part
    # of geometry()), deliberately never "WxH" -- sizing is governed
    # entirely by ordinary pack()-based layout plus checklist.py's own
    # fixed row dimensions, never forced explicitly here.
    win.minsize(1, 1)
    win.maxsize(10000, 10000)
    win.update_idletasks()
    _req_w = win.winfo_reqwidth()
    _req_h = win.winfo_reqheight()
    win.minsize(_req_w, _req_h)
    win.maxsize(_req_w, _req_h)
    _screen_w = win.winfo_screenwidth()
    _screen_h = win.winfo_screenheight()
    win.geometry(f"+{(_screen_w - _req_w) // 2}+{(_screen_h - _req_h) // 2}")


if __name__ == "__main__":
    main()