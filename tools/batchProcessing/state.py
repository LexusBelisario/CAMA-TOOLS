"""
tools/batchProcessing/state.py

PURPOSE:
    All in-memory batch state and its own pure logic (checking/
    unchecking, numbering, reorder splicing, lazy tool-module loading,
    and the "is this tool's own last-Saved config complete" check),
    deliberately kept FREE of any tkinter import. Everything here is
    plain Python, so it is directly unit-testable without a display/
    mock-widget replay -- unlike checklist.py/pickers.py/window.py,
    which build real Tkinter widgets.

    checklist.py (the checkbox-turned-number/Edit rows) and window.py
    (the Run Processing enable check) both hold and drive ONE
    BatchState instance each; this class only tracks state and answers
    questions about it -- it never builds or touches a single widget.

CONTRACT REUSED HERE:
    _load_module() / is_complete() are the mechanism behind
    tools/batchProcessing/contract.py's own documented "graceful
    degradation" -- a tool not yet adapted to the batch-mode contract
    (no is_batch_config_complete() exposed yet) is always treated as
    Incomplete rather than crashing or being silently treated as
    ready. See contract.py's own PER-TOOL BATCH-MODE CONTRACT docstring
    for the full spec is_batch_config_complete(config) must satisfy.

INPUTS:
    None at import time. A BatchState instance is driven entirely
    through its own methods, called by checklist.py (check/uncheck/
    move/Edit-Save) and read by window.py (Run Processing gating).

OUTPUTS:
    None persisted -- a BatchState instance's entire lifetime is one
    Batch Processing window (see window.py); nothing here writes to
    disk. Saving/loading batch configuration to/from a file is
    explicitly out of scope for this task.

DEPENDENCIES:
    stdlib: importlib.
    local: tools.batchProcessing.contract (BATCH_TOOL_MODULES).

SIDE EFFECTS:
    load_module() imports a tools/<name>.py module the first time it
    is asked for (importlib.import_module), caching the result (or the
    failure) for the lifetime of the BatchState instance. No other
    side effects.
"""
import importlib

from tools.batchProcessing.contract import BATCH_TOOL_MODULES


class BatchState:
    """
    All in-memory Batch Processing state for ONE open Batch Processing
    window. Never persisted -- discard the instance (it goes out of
    scope when the window closes) and everything it held is gone; see
    window.py's own docstring for why this is correct, deliberate
    behavior for this task.
    """

    def __init__(self):
        self.checked = set()          # labels currently checked
        self.checked_order = []       # labels in run order; index+1
                                       # == that tool's displayed
                                       # number -- a plain ordered list
                                       # rather than a separate
                                       # int-per-label dict, since
                                       # every operation below (check/
                                       # uncheck/reorder) is naturally
                                       # a list splice.
        self.saved_configs = {}       # label -> last-Saved config
                                       # dict. RETAINED across an
                                       # uncheck/recheck within this
                                       # session (only `checked`/
                                       # `checked_order` are touched by
                                       # toggle_check()) -- a
                                       # rechecked tool still gets
                                       # pre-filled via initial_config
                                       # on its next Edit, same as any
                                       # other re-Edit, and nothing
                                       # here is persisted across a
                                       # window close anyway, so
                                       # discarding early would only be
                                       # a gratuitous inconsistency
                                       # (see Phase 1 analysis).
        self._module_cache = {}       # label -> imported module, or
                                       # False if import failed

    # ------------------------------------------------------------
    # Module loading (shared by is_complete() below and checklist.py's own
    # Edit-button handler -- ONE cache, never a second, possibly
    # out-of-sync one).
    # ------------------------------------------------------------
    def load_module(self, label):
        """
        Lazy-imports and caches BATCH_TOOL_MODULES[label]. Returns the
        module, or None if it can't be imported -- that tool's own
        file may not exist yet (this task's own per-file chats have
        not all run yet) or may fail to import for an unrelated
        reason. Every caller must handle None gracefully rather than
        assume all 11 tool files are already present/working -- see
        contract.py's own "graceful degradation" note.
        """
        if label in self._module_cache:
            cached = self._module_cache[label]
            return cached if cached is not False else None
        try:
            mod = importlib.import_module(BATCH_TOOL_MODULES[label])
            self._module_cache[label] = mod
            return mod
        except Exception:
            self._module_cache[label] = False
            return None

    # ------------------------------------------------------------
    # Checking / unchecking / numbering
    # ------------------------------------------------------------
    def toggle_check(self, label):
        """
        Flips label's checked state and updates checked_order per the
        Prompt's own numbering algorithm:
        - Checking assigns the next available number == the current
          end of checked_order (1 if none are checked yet).
        - Unchecking removes label from checked_order -- every tool
          with a HIGHER number shifts down by one automatically, for
          free, since this is a plain list removal.
        - Re-checking afterward assigns a NEW number at the current
          end of the sequence -- it does NOT return to its old
          position (checked_order.append(), never insert() at a
          remembered index).
        """
        if label in self.checked:
            self.checked.discard(label)
            if label in self.checked_order:
                self.checked_order.remove(label)
        else:
            self.checked.add(label)
            self.checked_order.append(label)

    def number_of(self, label):
        """Returns label's 1-based displayed number, or None if it is
        not currently checked."""
        if label not in self.checked_order:
            return None
        return self.checked_order.index(label) + 1

    def uses_output_of(self, label):
        """
        Returns the label of whichever tool immediately precedes
        `label` in the current run order, or None if `label` is
        unchecked or currently holds position #1 (nothing precedes
        it). Purely informational in this task -- no actual chaining
        happens yet (see contract.py's own TASK 1 SCOPE note); this is
        recomputed fresh from checked_order on every call, never
        cached, so it is always correct after a check/uncheck/reorder.
        """
        if label not in self.checked_order:
            return None
        idx = self.checked_order.index(label)
        if idx == 0:
            return None
        return self.checked_order[idx - 1]

    # ------------------------------------------------------------
    # Reorder via Up/Down buttons -- a gesture SEPARATE from checking/
    # unchecking. DEVIATION from the Prompt's own original "drag-to-
    # rearrange" spec: a small drag-handle gesture with zero visual
    # feedback during the drag proved unreliable in real use (tiny hit
    # target, no confirmation the drag was even registered until
    # release) -- per explicit user request, replaced with simple,
    # always-clickable Up/Down buttons, which this small, fixed-length
    # list (2-11 items) suits well. checklist.py owns the actual button
    # widgets and their enabled/disabled state; these two methods only
    # own the resulting list splice (an adjacent-element swap).
    # ------------------------------------------------------------
    def move_up(self, label):
        """
        Swaps `label` with whichever tool is immediately ABOVE it in
        checked_order (i.e. decreases its displayed number by one).
        No-op (returns False) if label is not currently checked, or is
        already first (number 1) -- callers should treat a False
        return as "nothing changed, no redraw needed." Returns True on
        a real move.
        """
        if label not in self.checked_order:
            return False
        idx = self.checked_order.index(label)
        if idx == 0:
            return False
        self.checked_order[idx - 1], self.checked_order[idx] = (
            self.checked_order[idx], self.checked_order[idx - 1])
        return True

    def move_down(self, label):
        """
        Swaps `label` with whichever tool is immediately BELOW it in
        checked_order (i.e. increases its displayed number by one).
        No-op (returns False) if label is not currently checked, or is
        already last -- same "nothing changed" convention as
        move_up(). Returns True on a real move.
        """
        if label not in self.checked_order:
            return False
        idx = self.checked_order.index(label)
        if idx >= len(self.checked_order) - 1:
            return False
        self.checked_order[idx + 1], self.checked_order[idx] = (
            self.checked_order[idx], self.checked_order[idx + 1])
        return True

    # ------------------------------------------------------------
    # Readiness ("Incomplete" overlay / Run Processing gating)
    # ------------------------------------------------------------
    def is_complete(self, label):
        """
        Reuses that tool's OWN exposed is_batch_config_complete()
        against its own last-Saved config -- never a second,
        reimplemented readiness definition (see contract.py's own
        PER-TOOL BATCH-MODE CONTRACT). Conservative fallback (treated
        as Incomplete) for any tool never Saved this session, or not
        yet adapted to the contract at all -- never raises.
        """
        if label not in self.saved_configs:
            return False
        mod = self.load_module(label)
        if mod is None or not hasattr(mod, "is_batch_config_complete"):
            return False
        try:
            return bool(mod.is_batch_config_complete(self.saved_configs[label]))
        except Exception:
            return False

    def save_config(self, label, config):
        """Records label's last-Saved config dict (called by
        checklist.py's Edit-button on_save callback). Overwrites any
        previous save for this label within the same session."""
        self.saved_configs[label] = config

    def can_run(self):
        """
        Run Processing's own enable condition: at least 2 tools
        checked, AND every checked tool's own last-Saved config passes
        its own is_batch_config_complete(). Matches the Prompt's own
        spec verbatim.
        """
        return (
            len(self.checked_order) >= 2
            and all(self.is_complete(label) for label in self.checked_order)
        )