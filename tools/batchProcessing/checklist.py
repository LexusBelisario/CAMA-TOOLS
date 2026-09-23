"""
tools/batchProcessing/checklist.py

PURPOSE:
    The 11-tool selection checklist -- TWO columns (left then right,
    COL1_COUNT rows in the left column, the rest in the right --
    matching MAIN.py's own 6+5 icon-grid split), inside one "boxy"
    bordered section with an uppercase "SELECT TOOLS TO RUN IN ORDER"
    header and a "Reorder…" button. Rows are NOT fixed to a static
    column/position: every checked tool is always displayed in its
    current RUN order -- column 1 top-to-bottom first, THEN column 2
    top-to-bottom (so reading left-column-then-right-column always
    reads 1, 2, 3, ... in sequence, with every checked row's own
    column AND vertical position -- not just its displayed number --
    moving to match after a check/uncheck/Reorder), followed by every
    unchecked tool in contract.TOOL_ORDER (mirroring MAIN.py's own
    icon-grid order) -- see _display_order()/refresh()'s own
    docstrings for exactly how this is computed and applied, and for
    why rows are DESTROYED AND REBUILT fresh on every refresh() rather
    than updated in place (Tkinter widgets cannot be reparented to a
    different column frame after creation). This replaces two earlier
    layouts in this same package: the original 2-row rectangle-grid
    (tools/batchProcessing/grid.py, superseded and removed), and an
    intermediate single-column checklist that kept every row in a
    fixed position and only updated its displayed number -- confirmed,
    after testing, that a fixed-position number-only update read as
    "reordering doesn't visibly do anything." Each row has:
      - a toggle control (upper-left) that displays "☐" when unchecked
        and its assigned run-order NUMBER when checked -- a single
        click target that both checks/unchecks the tool AND shows its
        position, replacing the earlier separate checkbox + number
        label pair. The entire row (its own Frame and the tool's own
        name) is ALSO a click target for the same check/uncheck action
        -- not just the small toggle glyph -- per explicit request to
        make the click target more forgiving. Deliberately NOT
        extended onto the "❗" indicator (keeps its own hover-only
        behavior unambiguous) or the Edit button (must stay a
        distinct, unambiguous action);
      - the tool's own name (single line, wrapping to two for a long
        name) -- an earlier version of this row also showed a small
        "Uses output of: ..." sub-line here; REMOVED per explicit
        request (its own wrapped 2-3-line text was bleeding past the
        row's fixed height -- see ROW_HEIGHT's own comment -- pushing
        the checklist, and therefore the whole window, taller than the
        screen and off the bottom edge; reported separately as "the
        START button and status message disappeared");
      - an "❗" indicator, shown ONLY when the row is BOTH checked AND
        its last-Saved config is Incomplete -- never shown for an
        unchecked row under any circumstance -- with a hover tooltip
        explaining what to do (see _show_incomplete_tooltip()'s own
        docstring for the current, deliberately generic wording --
        "V1" per this task's own explicit scope decision; a field-
        specific version, e.g. "Road Network Source missing", is a
        later, separately-scoped upgrade requiring each of the 11
        tool files to expose a NEW function describing exactly what is
        missing, not just is_batch_config_complete()'s own plain
        bool);
      - an Edit button (right), unchanged from the previous layout for
        every tool EXCEPT Land Shape, which has none at all -- per
        explicit request, since it has nothing to configure and its
        own batch_mode branch never builds a window regardless (see
        _build_row()'s and _on_check_toggle()'s own comments for the
        full rationale, including how it still becomes non-Incomplete
        the moment it is checked, without an Edit click to trigger
        that).

    Also owns the "Reorder…" action (a header button above the list)
    and the small modal dialog it opens: a Listbox of the currently
    checked tools (in run order) plus Move Up / Move Down buttons
    operating on the current selection -- REPLACES the originally-
    specified drag-to-rearrange gesture a second time (first replaced
    with per-row Up/Down buttons, which also proved unreliable in real
    use -- see this task's own conversation history). A Listbox +
    explicit buttons is a long-established, thoroughly reliable
    Tkinter pattern with no custom hit-testing or drag-state tracking
    of any kind.

    NOTE on why there is no background thread / spinner for
    reordering, despite being explicitly requested: reordering 2-11
    Python list elements is a microsecond-scale operation with nothing
    to meaningfully background -- and, more fundamentally, Tkinter
    widgets may ONLY be created, read, or modified on the main thread;
    there is no way to move this work to a background thread at all
    without risking undefined behavior or a crash. A background
    thread + spinner belongs to Task 2's own Run Processing (genuine
    I/O and computation), not here.

INPUTS:
    SelectionChecklist(container, win, db_verified, state, on_change):
        container: parent widget the checklist (header + rows) packs
            into.
        win: the Batch Processing Toplevel -- parent for every Edit-
            opened tool window, the Reorder dialog, this class's own
            tooltip Toplevels, and its own messagebox dialogs.
        db_verified: bool, forwarded unchanged to every tool's own
            open_main_window() call (same one-time snapshot semantics
            as everywhere else in this codebase).
        state: a state.BatchState instance -- SelectionChecklist reads
            and mutates it (toggle_check/move_up/move_down/
            save_config) but owns NO batch state of its own; see
            state.py.
        on_change: callable(), called after any change that could
            affect Run Processing's own enable condition (check/
            uncheck/reorder/tool-Save) -- window.py passes its own
            Run-button refresh callback here.

OUTPUTS:
    None -- this class only builds and updates widgets, driven by
    `state`. window.py reads state.can_run() directly when it needs
    to; this class does not expose a separate readiness signal.

DEPENDENCIES:
    stdlib: tkinter, inspect.
    local: tools.batchProcessing.contract (TOOL_ORDER, SHORT_LABELS),
    tools.batchProcessing.state (BatchState -- type only, not
    imported; SelectionChecklist is handed an instance).

SIDE EFFECTS:
    Builds one row Frame per tool inside `container`. Its own Edit
    button handler imports tool modules lazily (via
    state.BatchState.load_module(), the SAME cache readiness checks
    use -- never a second one) and opens child Toplevels of `win`. Its
    own Reorder button opens one modal child Toplevel of `win` at a
    time (grab_set()). Its own "❗" hover opens/closes a small,
    overrideredirect Toplevel positioned at the current mouse
    position, also parented on `win`.
"""
import inspect
import tkinter as tk
from tkinter import messagebox

from tools.batchProcessing.contract import TOOL_ORDER, ROW_1

# How many of the checklist's rows go in the LEFT column before the
# rest overflow into the right column -- matches MAIN.py's own 6+5
# icon-grid split (ROW_1's own length) so the two-column checklist
# visually echoes the familiar icon grid's own proportions. Derived
# from ROW_1 rather than hardcoded, so it can never silently drift out
# of sync with contract.py's own row split.
COL1_COUNT = len(ROW_1)

# Fixed pixel dimensions for EVERY row, regardless of which tool
# currently occupies it. Confirmed necessary: without a fixed size,
# each row's natural width/height depends on its own tool name's
# length (e.g. "METERS FROM (SCHOOL, SHOP, TRANSPORT, CHURCH)" vs.
# "LAND SHAPE") -- and since which tool occupies which row/column
# changes dynamically as the checklist reorders (see refresh()'s own
# docstring), the checklist's own natural size, and therefore the
# WHOLE WINDOW's natural size, was changing on every check/uncheck --
# a visible resize (and a contributor to the reported flicker; see
# refresh()'s own docstring for the other contributor and its own
# fix). Long names wrap to two lines (name_label's own wraplength,
# set below relative to ROW_WIDTH) rather than growing the row wider.
# ROW_HEIGHT only needs to fit a 1-2 line wrapped name plus the
# toggle/Edit buttons -- there is no longer a second "Uses output of"
# sub-line (removed per explicit request; its own 2-3-line wrapped
# text no longer needs to be accounted for here).
ROW_WIDTH = 280
ROW_HEIGHT = 40


class SelectionChecklist:
    """
    Builds and drives the 11-tool selection checklist -- TWO columns
    (left then right, COL1_COUNT rows in the left column, the rest in
    the right -- see module-level COL1_COUNT), inside one "boxy"
    bordered section with its own "SELECT TOOLS TO RUN IN ORDER"
    header and "Reorder…" button. See module docstring for the full
    constructor contract.
    """

    def __init__(self, container, win, db_verified, state, on_change):
        self._win = win
        self._db_verified = db_verified
        self._state = state
        self._on_change = on_change
        self._row_widgets = {}    # label -> dict of that row's own
                                   # widgets -- REBUILT fresh on every
                                   # refresh() (see refresh()'s own
                                   # docstring for why), never mutated
                                   # in place.
        self._tooltip_win = None  # the currently-open "❗" hover
                                   # tooltip Toplevel, or None

        # "Boxy" outer section -- matches the bordered-box look every
        # other section of this window already has (see pickers.py's
        # own SourcePicker), per explicit request.
        outer_box = tk.Frame(container, relief="groove", borderwidth=2)
        outer_box.pack(fill="x", padx=10, pady=(6, 6))

        self._build_header(outer_box)

        # Stored on self -- refresh() hides/shows this ONE frame as a
        # single unit around its own destroy-and-rebuild pass, rather
        # than letting the screen show a partially-torn-down state
        # mid-rebuild (see refresh()'s own docstring).
        self._columns_row = tk.Frame(outer_box)
        self._columns_row.pack(fill="both", expand=True, padx=8,
                                pady=(0, 8))
        self._col1_frame = tk.Frame(self._columns_row)
        self._col1_frame.pack(side="left", fill="both", expand=True,
                               padx=(0, 4))
        self._col2_frame = tk.Frame(self._columns_row)
        self._col2_frame.pack(side="left", fill="both", expand=True,
                               padx=(4, 0))

        self.refresh()
        # NOTE: centering is NOT done here. See window.py's own
        # open_main_window() -- it calls a centering step of its own,
        # ONCE, at the very end, after EVERY section of the window
        # (including status_row/start_row, which are built AFTER this
        # checklist in window.py's own construction order) is
        # complete. Centering here, before window.py finishes building
        # its own remaining sections, was CONFIRMED as a real bug: it
        # locked win's own maxsize to whatever height was required at
        # THIS moment (sources_row + checklist only) -- permanently
        # capping the window below the height status_row/start_row
        # would later need, clipping them invisible regardless of
        # screen resolution or DPI scaling. Centering is a whole-
        # window concern; only window.py, which owns the window's full
        # construction, is in a position to do it correctly.

    # ------------------------------------------------------------
    # Header: title + Reorder… button
    # ------------------------------------------------------------
    def _build_header(self, container):
        header = tk.Frame(container)
        header.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(header, text="SELECT TOOLS TO RUN IN ORDER",
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        tk.Button(header, text="Reorder…", width=10,
                  command=self._open_reorder_dialog
                  ).pack(side="right")

    # ------------------------------------------------------------
    # Row construction -- called fresh from refresh() every time (see
    # that method's own docstring for why), never built once and
    # updated in place.
    # ------------------------------------------------------------
    def _build_row(self, parent_widget, label):
        # Fixed size (ROW_WIDTH x ROW_HEIGHT), enforced by
        # pack_propagate(False) -- see that constant's own module-
        # level comment for why: content (a long vs. short tool name)
        # must never influence this row's own rendered size, since
        # WHICH tool occupies this row changes dynamically.
        row = tk.Frame(parent_widget, width=ROW_WIDTH, height=ROW_HEIGHT,
                        relief="groove", borderwidth=1, bg="white",
                        cursor="hand2")
        row.pack_propagate(False)
        row.pack(fill="x", pady=2)

        # Toggle control -- shows "☐" unchecked, or its assigned
        # number once checked (a single click target, replacing the
        # earlier separate checkbox + number label pair).
        num = self._state.number_of(label)
        toggle_btn = tk.Button(
            row, text=(str(num) if num else "\u2610"), width=3,
            font=("Segoe UI", 9, "bold"), relief="flat", bg="white",
            command=lambda l=label: self._on_check_toggle(l))
        toggle_btn.pack(side="left", padx=(6, 8), pady=6)

        # Name -- single line, wraps to two lines for a long tool name
        # (e.g. "METERS FROM (SCHOOL, SHOP, TRANSPORT, CHURCH)") WITHIN
        # the row's own fixed width (see ROW_WIDTH's own comment),
        # rather than overflowing. No longer shows a second
        # "Uses output of: ..." sub-line -- per explicit removal
        # request; this also fixes the row-overflow bug that sub-line
        # caused (its own wrapped 2-3-line text bled past the fixed
        # ROW_HEIGHT, since Tkinter does not auto-clip overflowing
        # child content within a pack_propagate(False) frame -- the
        # root cause of the checklist growing tall enough to push the
        # status/START row off-screen, reported separately).
        text_col = tk.Frame(row, bg="white", cursor="hand2")
        text_col.pack(side="left", fill="both", expand=True, pady=4)
        name_label = tk.Label(text_col, text=label, bg="white",
                               font=("Segoe UI", 9), anchor="w",
                               justify="left", cursor="hand2",
                               wraplength=ROW_WIDTH - 110)
        name_label.pack(fill="x", anchor="w")

        # Expand the check/uncheck click target to the whole row, not
        # just toggle_btn's own small glyph, per an earlier explicit
        # request. Bound on the row's own Frame plus the name area
        # (text_col and name_label) -- every one of these calls the
        # EXACT same handler toggle_btn's own command= already calls,
        # so there is only ever one code path for "this row's checked
        # state changed," never a second, possibly-divergent one.
        # Deliberately NOT bound on excl_label (the "❗" icon already
        # has its own hover tooltip bindings -- stacking a click-to-
        # toggle on top of that could read as accidentally dismissing/
        # toggling while trying to read the tooltip) or on the Edit
        # button (must stay a distinct, unambiguous action).
        # "cursor=hand2" on every bound widget visually hints the row
        # itself is clickable.
        for _clickable in (row, text_col, name_label):
            _clickable.bind(
                "<Button-1>", lambda e, l=label: self._on_check_toggle(l))

        # "❗" Incomplete indicator -- only visible for a checked row
        # whose last-Saved config is Incomplete (see
        # state.BatchState.is_complete()). Never shown for an
        # unchecked row, even if a stale/never-valid saved config
        # exists for it -- only "checked AND incomplete" ever
        # displays it. Hover shows a short explanation -- see
        # _show_incomplete_tooltip()'s own docstring for the current
        # generic ("V1") wording.
        is_checked = label in self._state.checked
        excl_text = ("\u2757" if (is_checked and not self._state.is_complete(label))
                     else "")
        excl_label = tk.Label(row, text=excl_text, bg="white",
                               font=("Segoe UI", 10), fg="#c0392b")
        excl_label.pack(side="left", padx=(4, 4))
        excl_label.bind(
            "<Enter>", lambda e, l=label: self._show_incomplete_tooltip(e, l))
        excl_label.bind("<Leave>", self._hide_incomplete_tooltip)

        # Edit -- unchanged from the previous rectangle-grid layout,
        # EXCEPT for Land Shape specifically: per explicit request, no
        # Edit button is shown for it at all -- it has no secondary
        # source and nothing to configure (confirmed:
        # is_batch_config_complete() in land_shape.py always returns
        # True), and its own batch_mode branch never builds a window
        # in the first place (see land_shape.py's own open_main_window()
        # docstring: "no window is ever built... clicking Edit... has
        # no visible effect"). See _on_check_toggle()'s own comment for
        # how this tool still becomes non-Incomplete without an Edit
        # click to trigger it. Available regardless of checked state
        # for every OTHER tool (a tool can be pre-configured before
        # being checked).
        if label != "LAND SHAPE":
            tk.Button(row, text="Edit", width=8,
                      command=lambda l=label: self._on_edit(l)
                      ).pack(side="right", padx=6, pady=6)

        self._row_widgets[label] = {
            "frame": row,
            "toggle_btn": toggle_btn,
            "name_label": name_label,
            "excl_label": excl_label,
        }

    # ------------------------------------------------------------
    # Checking / unchecking
    # ------------------------------------------------------------
    def _on_check_toggle(self, label):
        self._state.toggle_check(label)
        if (label == "LAND SHAPE" and label in self._state.checked
                and label not in self._state.saved_configs):
            # Land Shape has no Edit button (see _build_row()'s own
            # comment for why) -- so nothing would ever call
            # state.save_config() for it otherwise, and
            # state.BatchState.is_complete() requires an entry in
            # saved_configs to exist AT ALL before it even looks at
            # is_batch_config_complete()'s own answer. Auto-saving an
            # empty config here mirrors exactly what land_shape.py's
            # own batch_mode branch already does the instant Edit
            # would otherwise be clicked (on_save({}) called
            # immediately, no window ever built) -- this just performs
            # that same, already-established "nothing to configure"
            # confirmation proactively, the moment the row is checked,
            # instead of requiring an Edit click that no longer exists.
            self._state.save_config(label, {})
        self.refresh()
        self._on_change()

    # ------------------------------------------------------------
    # "❗" hover tooltip -- generic ("V1") wording only. Mirrors the
    # same overrideredirect-Toplevel-near-the-mouse pattern already
    # established in influence_map_to_land_parcel.py's own hover
    # preview (_influence_preview_tip), reused here as a small, self-
    # contained helper rather than imported -- this is the only use
    # site so far (Rule of Three not yet met; revisit only if a third
    # genuinely identical need appears elsewhere in this package).
    # ------------------------------------------------------------
    def _show_incomplete_tooltip(self, event, label):
        """
        Shown only when the hovered row is actually checked AND
        Incomplete (checked in here, not just relied on from the
        caller, since a stale hover could otherwise show a tooltip
        for a row that became complete a moment earlier). Currently a
        fixed, generic message -- "V1" per this task's own explicit
        scope decision. A future "V2" upgrade would replace this
        single string with a per-tool, field-specific list (e.g.
        "- Road Network Source missing"), which requires each of the
        11 tool files to expose a NEW function describing exactly
        what is missing -- out of scope for this change.
        """
        if self._tooltip_win is not None:
            return
        if label not in self._state.checked or self._state.is_complete(label):
            return
        tip = tk.Toplevel(self._win)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        tip.geometry(f"+{event.x_root + 12}+{event.y_root + 12}")
        tk.Label(
            tip, text="Configuration incomplete.\nClick Edit to review.",
            bg="#ffffe0", fg="black", relief="solid", borderwidth=1,
            font=("Segoe UI", 8), justify="left", padx=6, pady=4,
        ).pack()
        self._tooltip_win = tip

    def _hide_incomplete_tooltip(self, event=None):
        if self._tooltip_win is not None:
            self._tooltip_win.destroy()
            self._tooltip_win = None

    # ------------------------------------------------------------
    # Reorder… dialog -- Listbox + Move Up / Move Down, operating on
    # the current selection. Replaces the earlier per-row Up/Down
    # button design (see module docstring).
    # ------------------------------------------------------------
    def _open_reorder_dialog(self):
        if len(self._state.checked_order) < 2:
            messagebox.showinfo(
                "Nothing to Reorder",
                "Check at least two tools before reordering.",
                parent=self._win)
            return

        dialog = tk.Toplevel(self._win)
        dialog.title("Reorder")
        dialog.resizable(False, False)
        dialog.transient(self._win)
        dialog.grab_set()

        tk.Label(dialog, text="Select a tool, then Move Up / Move Down.",
                 font=("Segoe UI", 9)).pack(padx=12, pady=(12, 6))

        listbox = tk.Listbox(dialog, width=42,
                              height=len(self._state.checked_order),
                              exportselection=False)
        for label in self._state.checked_order:
            listbox.insert(tk.END, label)
        listbox.pack(padx=12, pady=4)
        listbox.selection_set(0)

        def _refresh_listbox(keep_label):
            listbox.delete(0, tk.END)
            for label in self._state.checked_order:
                listbox.insert(tk.END, label)
            idx = self._state.checked_order.index(keep_label)
            listbox.selection_set(idx)
            listbox.activate(idx)
            listbox.see(idx)

        def _move_up():
            sel = listbox.curselection()
            if not sel:
                return
            label = listbox.get(sel[0])
            if self._state.move_up(label):
                _refresh_listbox(label)

        def _move_down():
            sel = listbox.curselection()
            if not sel:
                return
            label = listbox.get(sel[0])
            if self._state.move_down(label):
                _refresh_listbox(label)

        btn_row = tk.Frame(dialog)
        btn_row.pack(pady=(4, 4))
        tk.Button(btn_row, text="Move Up", width=12,
                  command=_move_up).pack(side="left", padx=4)
        tk.Button(btn_row, text="Move Down", width=12,
                  command=_move_down).pack(side="left", padx=4)

        def _on_close():
            dialog.destroy()
            self.refresh()
            self._on_change()

        tk.Button(dialog, text="Done", width=12, command=_on_close,
                  bg="#2e7d32", fg="white").pack(pady=(8, 12))
        dialog.protocol("WM_DELETE_WINDOW", _on_close)

        # Center on the parent Batch Processing window (not the
        # screen) -- a small, transient dialog belongs near its own
        # parent, matching every other modal dialog already in this
        # package (e.g. pickers.py's own _pick_db_tables()).
        dialog.update_idletasks()
        dw, dh = dialog.winfo_reqwidth(), dialog.winfo_reqheight()
        try:
            px, py = self._win.winfo_rootx(), self._win.winfo_rooty()
            pw, ph = self._win.winfo_width(), self._win.winfo_height()
            dialog.geometry(f"+{px + (pw - dw) // 2}+{py + (ph - dh) // 2}")
        except tk.TclError:
            pass

    # ------------------------------------------------------------
    # Edit button -- drives the per-tool batch-mode contract
    # ------------------------------------------------------------
    def _on_edit(self, label):
        """
        Opens the target tool's own window as an IN-PROCESS Toplevel:
        a direct import + direct call to that tool's own
        open_main_window(batch_mode=True, ...) -- NOT a second
        subprocess through MAIN.py's run_tool_by_label()/--tool
        mechanism (confirmed correct against MAIN.py's actual dispatch
        code in this task's own Phase 1 analysis: the subprocess path
        has no channel to return a config dict back into this process
        at all). Parented on self._win, never on the app root -- see
        module docstring SIDE EFFECTS.
        """
        mod = self._state.load_module(label)
        if mod is None:
            messagebox.showerror(
                "Tool Unavailable",
                f'Could not load "{label}". Its module may not exist '
                f"yet, or failed to import.", parent=self._win)
            return
        if not hasattr(mod, "open_main_window"):
            messagebox.showerror(
                "Tool Unavailable",
                f'"{label}" has no open_main_window() entry point.',
                parent=self._win)
            return
        try:
            sig = inspect.signature(mod.open_main_window)
        except (TypeError, ValueError):
            sig = None
        if sig is None or "batch_mode" not in sig.parameters:
            # Graceful degradation for a tool file not yet adapted to
            # the batch-mode contract (see contract.py) -- never
            # crashes, and deliberately never falls back to opening
            # that tool's NORMAL window instead (which would show its
            # own Land Parcel Source / Output Destination / Run
            # Processing, confusing inside this orchestrator).
            messagebox.showinfo(
                "Not Yet Available",
                f'"{label}" has not been updated for Batch Processing '
                f"yet.", parent=self._win)
            return

        def _handle_save(config):
            self._state.save_config(label, config)
            self.refresh()
            self._on_change()

        def _handle_cancel():
            pass  # nothing this orchestrator holds for this tool changes

        mod.open_main_window(
            self._win, db_verified=self._db_verified, batch_mode=True,
            initial_config=self._state.saved_configs.get(label),
            on_save=_handle_save, on_cancel=_handle_cancel,
        )

    # ------------------------------------------------------------
    # Redraw
    # ------------------------------------------------------------
    def _display_order(self):
        """
        The order rows are actually PACKED in, top to bottom: every
        checked tool first, in its current run order (so reading top
        to bottom always reads 1, 2, 3, ... in sequence, with no gaps
        or out-of-order numbers), followed by every unchecked tool, in
        contract.TOOL_ORDER (the fixed, MAIN.py-icon-grid-matching
        order every tool had before any checking/reordering ever
        happened).
        """
        checked_order = self._state.checked_order
        unchecked = [l for l in TOOL_ORDER if l not in self._state.checked]
        return checked_order + unchecked

    def refresh(self):
        """
        Rebuilds every row FRESH (destroys the old ones, constructs new
        ones with correct current text) into the two column frames,
        split according to _display_order() -- COL1_COUNT rows in the
        left column, the rest in the right. Rebuilt rather than
        updated in place because Tkinter widgets cannot be reparented
        to a different master after creation -- a checked tool moving
        from the right column into the left (e.g. because something
        above it in the left column got unchecked) genuinely needs a
        new widget under the new column frame, not a repositioned old
        one. Cheap regardless (11 rows, plain widget construction, no
        I/O) -- no background thread or spinner is warranted (see
        module docstring's own note on why one was deliberately NOT
        added for the Reorder gesture).

        The single point where displayed state (both each row's own
        text AND which column/position it appears in) is derived from
        state.checked / state.checked_order / state.saved_configs --
        never the reverse. Called after every check/uncheck, Reorder
        dialog close, and tool Save.

        Deliberately does NOT re-center the window -- that happens
        ONCE, in __init__, right after the very first call to this
        method (see __init__'s own comment for why: centering belongs
        to each LAUNCH of the Batch Processing window, i.e. each press
        of its icon on MAIN.py's own main panel, not to every
        check/uncheck/Reorder/Save that happens inside an already-open
        window -- per explicit correction).
        """
        # Hovering a row that is about to be destroyed (e.g. its own
        # Incomplete state just changed) could otherwise leave a
        # stale tooltip open pointing at a widget that no longer
        # exists.
        self._hide_incomplete_tooltip()

        # Hide the whole columns area WHILE tearing down and rebuilding
        # its rows, rather than leaving it visible (and visibly empty,
        # then visibly refilling) during that process -- a well-
        # established Tkinter technique for this exact class of
        # flicker, since there is no true "freeze updates" API in
        # vanilla Tkinter. Combined with ROW_WIDTH/ROW_HEIGHT's own
        # fixed sizing (eliminating the window-resize that was the
        # OTHER, likely larger contributor to the reported flicker --
        # an OS-level window-frame resize is far more visually jarring
        # than a same-size content swap), this addresses both
        # confirmed sources together.
        self._columns_row.pack_forget()

        for w in self._row_widgets.values():
            w["frame"].destroy()
        self._row_widgets = {}

        order = self._display_order()
        col1_items = order[:COL1_COUNT]
        col2_items = order[COL1_COUNT:]

        for label in col1_items:
            self._build_row(self._col1_frame, label)
        for label in col2_items:
            self._build_row(self._col2_frame, label)

        self._columns_row.pack(fill="both", expand=True, padx=8,
                                pady=(0, 8))