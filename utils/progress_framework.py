# ============================================================
# Progress Event Protocol v9 — shared framework
# ============================================================
# Pure extraction. Not a new abstraction layer.
#
# lot_location.py and road_frontage.py each independently implemented
# the exact same PresentationState / Presentation Policy / Tkinter View
# code (identical field names, identical OR-independent-checks
# semantics, identical update_idletasks() -> geometry("") -> update()
# render sequence) during their Progress Event Protocol v9 migrations.
# This module is that duplicated code, moved to one place. Nothing here
# is new behavior -- every line matches what both tools already had.
#
# D1 (Cancel support): PresentationState/ProgressPresentationPolicy/
# TkinterProgressView each gained one new, fully optional piece
# (cancelable / cancel_flag) so a tool can opt in to a title-bar-X-is-
# Cancel control, matching the mechanism already built and proven in
# landmarks_within_meters.py's own (separate, non-shared) progress
# window. A tool that does not pass cancel_flag to TkinterProgressView
# behaves EXACTLY as before this change -- every existing call site
# across all 8 tools already using this module remains valid untouched.
# This is still additive, not a redesign: see each class's own
# docstring below for exactly what changed and why it doesn't disturb
# the "None means don't touch this" philosophy the module already had
# for value/maximum.
#
# What this module deliberately does NOT contain:
#   - A ProgressWindow base class, or any inheritance hierarchy for
#     ProgressWindow itself. Each tool's own ProgressWindow class stays
#     exactly where it is, in its own file, and simply constructs
#     PresentationState/ProgressPresentationPolicy/TkinterProgressView
#     instead of its own tool-prefixed copies of the same three things.
#   - Configurable widget factories, parameterized constructors,
#     label_cls/bar_length/wraplength-style knobs, or any other
#     generalization of ProgressWindow.__init__ itself. Only two tools
#     currently share this shape -- Rule of Three: that isn't enough
#     evidence yet for a shared ProgressWindow abstraction, and forcing
#     one now risks exactly the kind of speculative, unused flexibility
#     ("parameter explosion") that a real third or fourth matching tool
#     would be needed to justify.
#   - Anything from road_width.py. That tool has real, intentional
#     divergences from this shape (switch_to_determinate(), the
#     _closed/winfo_exists() lifecycle guard, the count_var widget,
#     indeterminate mode, and AND- rather than OR-semantics on
#     value/total) -- it stays fully standalone until there's enough
#     evidence (a third or fourth tool with road_width's specific
#     shape) to justify sharing any of it. D1 does not change this --
#     road_width.py gets its own, separate Cancel implementation if/
#     when that work happens, not this module's.
#
# Used by: lot_location.py, road_frontage.py, road_surface.py,
# land_shape.py, influence_map_distance_to_land_parcel.py, terrain.py,
# road_density.py, influence_map_to_land_parcel.py (each still owns its
# own ProgressWindow class -- see that class's docstring in each file
# for how it wires these three pieces together). Confirmed current as
# of D1 by checking every tool file's own import statement directly --
# road_width.py explicitly does NOT import from here (see its own
# comment at the top of that file); the earlier "land_shape_compactness.py"/
# "influence_to_barangay.py"/"influence_to_map.py" names in this
# comment were stale (the actual current filenames are
# land_shape.py / influence_map_to_land_parcel.py /
# influence_map_distance_to_land_parcel.py, respectively) and have been
# corrected here.
# ============================================================

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PresentationState:
    """
    Immutable snapshot of what TkinterProgressView should render for one
    progress event: status text plus an optional determinate-bar
    value/maximum pair. `value` and `maximum` being None means "leave
    the bar as it currently is" -- this is the original
    ProgressWindow.update() behavior from both tools this was extracted
    from (the bar doesn't reset between text-only status updates),
    preserved here by passing None through unchanged rather than
    resolving it to a default. Do not add fields (color, icon,
    animation, visibility, etc.) without a real, evidenced need across
    multiple tools -- this is an extraction, not a redesign.

    D1: `cancelable` follows this exact same None-means-leave-it-alone
    convention -- True enables the title-bar X as Cancel, False disables
    it, None (the default) makes no change to whatever it currently is.
    Only has any effect on a TkinterProgressView constructed WITH a
    cancel_flag (see that class's docstring) -- a tool that never opts
    in can pass cancelable freely or never set it; either way nothing
    happens, since there is no cancel_flag/close-button wiring to act on.
    """
    message: str
    value: Optional[float] = None
    maximum: Optional[float] = None
    cancelable: Optional[bool] = None


class ProgressPresentationPolicy:
    """
    Presentation Policy (Progress Event Protocol v9). Transforms raw
    progress-event data (message, value, maximum) into a
    PresentationState -- a decision, not a widget mutation. Single
    static strategy, identical to both lot_location.py's and
    road_frontage.py's prior tool-prefixed policy classes: message
    passed through as-is, value/maximum passed through unchanged
    (including None).

    Note: this is the OR-independent-checks variant (maximum and value
    are each applied independently in TkinterProgressView.render() below).
    road_width.py's AND-semantics variant (both required together) is
    NOT this class and is not represented here -- see this module's
    top-of-file comment.

    D1: cancelable is a new trailing optional parameter (default None,
    same passthrough treatment as value/maximum) -- appending it here
    rather than inserting it earlier in the signature means every
    existing positional call across all 8 tools using this module
    (`compute(message, value, maximum)`) remains valid unchanged.
    """
    def compute(self, message, value=None, maximum=None, cancelable=None):
        """Passes message/value/maximum/cancelable through unchanged
        into a PresentationState -- see class docstring for why."""
        return PresentationState(message=message, value=value,
                                  maximum=maximum, cancelable=cancelable)


def new_cancel_flag():
    """
    D1: creates a fresh cancel flag -- a plain {"stop": False} dict, the
    exact shape landmarks_within_meters.py's own PROG_STOP_FLAG already
    uses and already proved safe for this purpose: a single boolean
    mutated from the main thread (TkinterProgressView's close-button
    handler, on a click) and read from a background worker thread
    (whatever loop the calling tool checks it in) -- CPython's GIL makes
    each individual dict get/set atomic, so there is nothing to race on
    beyond ordinary, acceptable click-to-next-read latency, not a
    correctness issue. A tool wanting Cancel support creates ONE of
    these per run (matching PROG_STOP_FLAG's own per-run/per-window
    reset) and passes it to both TkinterProgressView (to wire the
    close button) and its own processing loop (to check it).
    """
    return {"stop": False}


def _disable_close_button(win):
    """
    Grays out the titlebar's close (X) button via the Win32 API -- the
    button stays visibly present but visually disabled, and clicking it
    does nothing. Ported verbatim from landmarks_within_meters.py's own
    _disable_close_button() -- see that function's docstring (same file,
    same name) for the full history of why this SC_CLOSE-system-menu
    approach replaced an earlier WS_SYSMENU-clearing one that didn't
    survive a window activation/focus-change event.

    D1: needed here because none of the 8 tools using this module
    currently disable their progress window's close button at all --
    confirmed by checking every one of their ProgressWindow classes
    directly: none binds WM_DELETE_WINDOW, so today the X just destroys
    the Toplevel via Tkinter's default handling while the background
    worker keeps running completely unaware. This function (plus
    _enable_close_button()'s counterpart below, and the wiring in
    TkinterProgressView) is what closes that gap for any tool that opts
    in via cancel_flag.

    Any failure in the Win32 call below is caught and silently ignored,
    falling back to whatever protocol()-only behavior the caller set up
    (X visible and looks clickable, but does nothing) -- see
    TkinterProgressView's own close-button wiring.
    """
    try:
        import ctypes
        MF_BYCOMMAND = 0x00000000
        MF_GRAYED = 0x00000001
        MF_DISABLED = 0x00000002
        SC_CLOSE = 0xF060
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        hmenu = ctypes.windll.user32.GetSystemMenu(hwnd, False)
        if hmenu:
            ctypes.windll.user32.EnableMenuItem(
                hmenu, SC_CLOSE, MF_BYCOMMAND | MF_GRAYED | MF_DISABLED)
    except Exception:
        pass


def _enable_close_button(win):
    """
    Re-enables (un-grays) the titlebar close (X) button previously
    disabled by _disable_close_button() above -- the counterpart call,
    using MF_ENABLED instead of MF_GRAYED|MF_DISABLED against the same
    SC_CLOSE system-menu entry.

    D1: landmarks_within_meters.py never needed this counterpart -- its
    own progress window always starts a fresh Toplevel per phase
    transition, always starting enabled, so it only ever calls the
    disable half once per window. A progress_framework.py-based tool's
    SINGLE ProgressWindow instance is longer-lived across phases (it is
    never destroyed/recreated mid-run the way landmarks_within_meters.py's
    was before its own D6 change), so Cancel may legitimately need to
    toggle on, then off, within that one window's lifetime -- this makes
    that possible.
    """
    try:
        import ctypes
        MF_BYCOMMAND = 0x00000000
        MF_ENABLED = 0x00000000
        SC_CLOSE = 0xF060
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        hmenu = ctypes.windll.user32.GetSystemMenu(hwnd, False)
        if hmenu:
            ctypes.windll.user32.EnableMenuItem(
                hmenu, SC_CLOSE, MF_BYCOMMAND | MF_ENABLED)
    except Exception:
        pass


class TkinterProgressView:
    """
    Tkinter View (Progress Event Protocol v9). The only component that
    touches the progress dialog's status_var/progress/win widgets on a
    per-event basis. Construction/ownership of those widgets stays in
    each tool's own ProgressWindow.__init__, unchanged -- this class is
    only ever handed already-constructed widget references.

    The update_idletasks() -> geometry("") -> update() sequence in
    render() is preserved verbatim from both tools' original
    ProgressWindow.update() implementations and must not be reordered,
    simplified, or removed -- it is existing, battle-tested Tk
    behavior, not incidental code.

    D1: an OPTIONAL cancel_flag (see new_cancel_flag() above) may be
    passed to opt in to title-bar-X-is-Cancel support -- see __init__'s
    own docstring. A tool that does not pass one gets EXACTLY the
    pre-D1 behavior: this class never touches WM_DELETE_WINDOW or the
    close button at all, and render() silently ignores
    state.cancelable if it's ever set (there's nothing to act on
    without a cancel_flag).
    """
    def __init__(self, win, status_var, progressbar, cancel_flag=None):
        """
        Stores already-constructed widget references. Does not create
        any widgets itself -- see class docstring.

        D1: if cancel_flag is provided (a dict from new_cancel_flag()),
        wires win's title-bar X to Cancel, ONCE, here at construction --
        clicking it sets cancel_flag["stop"] = True and immediately
        disables the close button (both the visible X via
        _disable_close_button() and the protocol binding itself, so
        neither a second click nor Alt+F4 can do anything once
        cancelled), matching landmarks_within_meters.py's own
        _on_cancel() pattern. Deliberately does NOT change status_var
        or the progress bar on cancel -- wording is left entirely to
        the calling tool's own next render() call (e.g.
        policy.compute("Cancelling...", cancelable=False)), since this
        module stays intentionally silent on message wording (see the
        module's own top-of-file philosophy).

        If cancel_flag is None (the default -- every one of the 8 tools
        using this module today), no protocol binding happens at all;
        the window's close button behaves exactly as it always has
        (Tkinter's default: destroys the window, and does not stop
        whatever the background worker is doing).
        """
        self.win = win
        self.status_var = status_var
        self.progress = progressbar
        self._cancel_flag = cancel_flag
        self._on_cancel = None

        if self._cancel_flag is not None:
            def _on_cancel():
                self._cancel_flag["stop"] = True
                _disable_close_button(self.win)
                self.win.protocol("WM_DELETE_WINDOW", lambda: None)
            self._on_cancel = _on_cancel
            self.win.protocol("WM_DELETE_WINDOW", self._on_cancel)

    def render(self, state: PresentationState):
        """
        Applies a PresentationState to the widgets: always updates the
        status text; updates the progress bar's maximum/value only if
        each is not None (independently -- see class docstring's OR-
        semantics note); D1: if this view was constructed WITH a
        cancel_flag AND state.cancelable is not None, enables or
        disables the close button to match (True ->
        _enable_close_button(), False -> _disable_close_button() plus
        re-binding WM_DELETE_WINDOW to a no-op, same as a click's own
        _on_cancel() above); then forces an immediate GUI refresh via
        the preserved update_idletasks() -> geometry("") -> update()
        sequence.
        """
        self.status_var.set(state.message)
        if state.maximum is not None:
            self.progress["maximum"] = state.maximum
        if state.value is not None:
            self.progress["value"] = state.value
        if state.cancelable is not None and self._cancel_flag is not None:
            if state.cancelable:
                _enable_close_button(self.win)
                self.win.protocol("WM_DELETE_WINDOW", self._on_cancel)
            else:
                _disable_close_button(self.win)
                self.win.protocol("WM_DELETE_WINDOW", lambda: None)
        self.win.update_idletasks()
        self.win.geometry("")
        self.win.update()

    def destroy(self):
        """Destroys the progress window."""
        self.win.destroy()