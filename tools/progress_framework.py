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
#     shape) to justify sharing any of it.
#
# Used by: lot_location.py, road_frontage.py, road_surface.py,
# land_shape_compactness.py, influence_to_barangay.py, terrain.py,
# road_density.py, influence_to_map.py (each still owns its own
# ProgressWindow class -- see that class's docstring in each file for
# how it wires these three pieces together).
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
    """
    message: str
    value: Optional[float] = None
    maximum: Optional[float] = None


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
    """
    def compute(self, message, value=None, maximum=None):
        """Passes message/value/maximum through unchanged into a
        PresentationState -- see class docstring for why."""
        return PresentationState(message=message, value=value, maximum=maximum)


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
    """
    def __init__(self, win, status_var, progressbar):
        """Stores already-constructed widget references. Does not
        create any widgets itself -- see class docstring."""
        self.win = win
        self.status_var = status_var
        self.progress = progressbar

    def render(self, state: PresentationState):
        """
        Applies a PresentationState to the widgets: always updates the
        status text; updates the progress bar's maximum/value only if
        each is not None (independently -- see class docstring's OR-
        semantics note), then forces an immediate GUI refresh via the
        preserved update_idletasks() -> geometry("") -> update()
        sequence.
        """
        self.status_var.set(state.message)
        if state.maximum is not None:
            self.progress["maximum"] = state.maximum
        if state.value is not None:
            self.progress["value"] = state.value
        self.win.update_idletasks()
        self.win.geometry("")
        self.win.update()

    def destroy(self):
        """Destroys the progress window."""
        self.win.destroy()