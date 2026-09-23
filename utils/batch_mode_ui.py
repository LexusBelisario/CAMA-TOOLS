"""
utils/batch_mode_ui.py

PURPOSE:
    Shared builder for the Cancel/Save button row every one of the 11
    tools' own batch-mode window (see tools/batchValuation/contract.py
    for the full per-tool batch-mode contract) uses at the bottom of
    its window, plus the window-lifecycle behavior that goes with it:
    dirty-change tracking, a Save confirmation dialog, an unsaved-
    changes confirmation on Cancel/titlebar-X, and screen centering.

    Extracted here once three of the 11 tool files (land_shape.py,
    road_surface.py, road_density.py) needed the SAME block, byte-for-
    byte identical in behavior -- see Instructions Section C / G.5
    (Rule of Three): this is UI chrome, not per-tool business/discovery
    logic, so centralizing it is the correct call once the pattern is
    confirmed genuinely identical across at least three files with
    real, matching needs.

BEHAVIOR (see build_save_cancel_row()'s own docstring for the full
    contract):
    - Save: gathers the current config, hands it to the caller's own
      on_save callback (already fully committed at that point --
      synchronous, no timing gap), shows a blocking "Saved"
      confirmation dialog, records the just-saved config as the new
      baseline, and leaves the window OPEN -- Save behaves like Apply,
      repeatable any number of times.
    - Cancel, and the window's own titlebar X (both wired to the exact
      same handler): compares the CURRENTLY gathered config against
      the last-saved baseline. If they differ, asks for confirmation
      before closing ("Unsaved changes will not be applied. Cancel
      anyway?"); if they're identical -- including the common case of
      a Browse/Select dialog that was opened and then dismissed
      without picking anything, which never changes the gathered
      config in the first place -- the window closes immediately, no
      dialog. Either way, the caller's own on_cancel callback is
      called right before the window closes (harmless even after a
      prior Save -- on_cancel is a pure "nothing more to do" signal,
      never an undo).
    - Centers the window on screen, using the exact same technique
      already established in core/startup_and_db_ui.py for the
      Configure Database dialog and the startup workspace picker
      (update_idletasks() then winfo_reqwidth/reqheight against
      winfo_screenwidth/screenheight) -- reused here rather than
      reinvented. Runs once, after this row is added (the last widget
      built), since every batch-mode window is a fixed-size, non-
      resizable Toplevel.

INPUTS:
    See build_save_cancel_row()'s own docstring for its full parameter
    contract.

OUTPUTS:
    None -- this module only builds widgets and wires callbacks into
    an already-open window; it holds no state of its own beyond what
    each call's own closures capture for that one window's lifetime.

DEPENDENCIES:
    stdlib: tkinter, tkinter.messagebox.

SIDE EFFECTS:
    Builds two Button widgets inside `win`, binds win's own
    WM_DELETE_WINDOW protocol (so the titlebar X behaves identically
    to Cancel -- see BEHAVIOR above), and repositions `win` via
    win.geometry() once, at the end of the call.
"""
import tkinter as tk
from tkinter import ttk, messagebox


def build_save_cancel_row(win, gather_config, on_save, on_cancel,
                           saved_message="Changes applied."):
    """
    Builds the Cancel (left) / Save (right) button row for a tool's
    batch-mode window, and wires up dirty-change tracking, the Save
    confirmation dialog, the Cancel/titlebar-X unsaved-changes
    confirmation, and screen centering. Call this LAST, after every
    other widget in the window has already been built -- centering
    measures the window's final requested size, and the dirty-tracking
    baseline is captured, at call time, from whatever gather_config()
    returns right now (i.e. the window's fully-initialized state,
    initial_config already applied if any).

    Args:
        win: the batch-mode Toplevel. Must already be fully built
            (every other section's widgets already packed) and
            resizable(False, False) -- centering assumes a fixed final
            size.
        gather_config: callable() -> dict. Returns the CURRENT state
            of every batch-visible field, in the same shape the
            caller's own on_save/initial_config already use -- the
            same function each tool's own open_main_window() already
            defines for its Save button (e.g. _gather_batch_config()).
            Called fresh every time a Save/Cancel/X decision needs the
            current state -- never cached except as the dirty-tracking
            baseline itself (updated only on a successful Save).
        on_save: callable(config: dict) -> None, or None. Forwarded
            from open_main_window's own batch-mode parameter of the
            same name -- called with gather_config()'s result every
            time Save is clicked, before the confirmation dialog is
            shown. Safe to pass None (simply skipped).
        on_cancel: callable() -> None, or None. Forwarded from
            open_main_window's own batch-mode parameter of the same
            name -- called once, right before the window actually
            closes via Cancel or the titlebar X (in both the
            immediate-close and the confirmed-close-after-dialog
            cases). Safe to pass None (simply skipped).
        saved_message: str, the Save confirmation dialog's own body
            text. Defaults to "Changes applied."; override only if a
            tool ever needs a more specific message.
    """
    # Captured once, right now -- i.e. AFTER every other section's
    # widgets (and any initial_config pre-fill) are already in place,
    # since this function must be called last. This is what "no
    # changes yet" means for this window: whatever gather_config()
    # reports at this exact moment. A single-element list is used as a
    # mutable cell so the nested functions below can reassign it
    # without a `nonlocal` declaration falling out of sync with this
    # module's other closures.
    baseline = [gather_config()]

    def _has_unsaved_changes():
        return gather_config() != baseline[0]

    def _do_close():
        if on_cancel:
            on_cancel()
        win.destroy()

    def _on_save_click():
        config = gather_config()
        if on_save:
            on_save(config)
        baseline[0] = config
        messagebox.showinfo("Saved", saved_message, parent=win)
        # Deliberately does NOT close -- Save behaves like Apply, and
        # is safe to click any number of times (state.BatchState's own
        # save_config() on the orchestrator side is a plain dict
        # overwrite, not additive -- see tools/batchValuation/state.py).

    def _on_cancel_or_close():
        if _has_unsaved_changes():
            # Only asks when the CURRENT config genuinely differs from
            # the last-saved baseline -- e.g. a Browse/Select dialog
            # that was opened and then dismissed without picking
            # anything never changes what gather_config() returns, so
            # this branch is correctly skipped in that case; re-
            # picking the exact same file/table also leaves the
            # gathered config identical, so that also correctly skips
            # this dialog.
            proceed = messagebox.askyesno(
                "Unsaved Changes",
                "Unsaved changes will not be applied. Cancel anyway?",
                parent=win,
            )
            if not proceed:
                return  # stay open, nothing changes
        _do_close()

    # Separator -- visually detaches the Cancel/Save row from
    # whatever section sits directly above it, matching every
    # non-batch "Run Processing" row's own existing convention
    # (ttk.Separator right before the Run button in every one of the
    # 11 tools' own non-batch code).
    ttk.Separator(win, orient="horizontal").pack(
        fill="x", padx=10, pady=(12, 4))

    # Right-aligned within the window (anchor="e") -- Cancel packed
    # first (side="left", so it sits immediately left of Save within
    # this row), Save packed second so it lands at the row's own right
    # edge, which is now also the window's right edge.
    btn_row = tk.Frame(win)
    btn_row.pack(pady=(4, 16), padx=10, fill="x")

    tk.Button(btn_row, text="Save", width=12,
              command=_on_save_click,
              bg="#2e7d32", fg="white").pack(side="right", padx=4)
    tk.Button(btn_row, text="Cancel", width=12,
              command=_on_cancel_or_close).pack(side="right", padx=4)

    # Titlebar X behaves identically to Cancel -- same dirty-check,
    # same confirmation dialog when warranted, same on_cancel callback.
    win.protocol("WM_DELETE_WINDOW", _on_cancel_or_close)

    # Center on screen -- same technique already established in
    # core/startup_and_db_ui.py's own Configure Database dialog and
    # startup workspace picker. Runs once, here, since this is the
    # last widget added to a fixed-size (resizable(False, False))
    # window.
    #
    # RESET minsize/maxsize to permissive bounds FIRST, before
    # remeasuring -- several tools' own dynamic-checklist visibility
    # functions (e.g. road_width.py's own
    # _update_road_classification_visibility(), called unconditionally
    # -- even with nothing selected -- by the batch-mode branch's own
    # early _toggle_road()/_toggle_poi() sync call) already call their
    # OWN _reflow_window()-equivalent, which locks win.minsize()/
    # maxsize() to whatever size the window happened to be at THAT
    # moment -- before this Cancel/Save row (and this separator) were
    # ever added. Without resetting that lock first, win's own hard
    # maxsize prevents it from ever growing enough to show the row
    # just packed above, silently clipping Save/Cancel off the bottom
    # of the window -- confirmed as the exact, reported root cause of
    # several tools' own batch-mode windows showing no Save/Cancel at
    # all. Mirrors landmarks_within_meters.py's own established fix
    # for the identical class of bug (see that file's own
    # _reflow_window() docstring: "a PREVIOUS call's win.minsize(...)
    # becomes a hard floor Tk will never shrink below on a LATER
    # call").
    win.minsize(1, 1)
    win.maxsize(10000, 10000)
    win.update_idletasks()
    win_w = win.winfo_reqwidth()
    win_h = win.winfo_reqheight()
    win.minsize(win_w, win_h)
    win.maxsize(win_w, win_h)
    screen_w = win.winfo_screenwidth()
    screen_h = win.winfo_screenheight()
    center_x = (screen_w - win_w) // 2
    center_y = (screen_h - win_h) // 2
    win.geometry(f"{win_w}x{win_h}+{center_x}+{center_y}")