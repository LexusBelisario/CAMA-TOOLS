"""
core/tool_exclusivity.py

PURPOSE:
    Mutual-exclusivity logic for the CAMA Tools launcher's tool-icon
    grid: only ONE tool (its config window open, or a run in progress)
    may ever be active at a time. While one is active, every other
    tool icon -- including the active tool's own icon -- becomes
    unclickable; every icon except the active one is also rendered
    grayscale. The moment the active tool ends (clean success, an
    error the user dismissed, or a crash/force-close -- all treated
    identically, there is no "stay locked" state), every icon is
    restored together.

    A second, narrower entry point -- activate_manual() (see MECHANISM
    below) -- engages this same "something is busy" grid-graying state
    for a caller-managed busy period that has no subprocess to poll at
    all (currently: the Configure Database dialog being open). Unlike
    activate_tool(), it grays out EVERY tracked icon, including what
    would otherwise be "the active one," since a Configure Database
    session has no tool icon of its own to leave in color. The two
    entry points share the same re-entrancy invariant (only one of
    activate_tool()/activate_manual() may be engaged at a time) and the
    same restore path (deactivate_all()) -- see MECHANISM and MUTABLE
    STATE below for how this is implemented without duplicating the
    per-canvas swap logic.

    Same architectural role as core/window_management.py (see that
    module's own docstring for the precedent this follows): a focused,
    self-contained module the MAIN.py launcher boundary calls into,
    not a general-purpose shared utility other tool files import. This
    module does NOT import from MAIN.py -- every piece of MAIN.py
    state it needs (canvas references, icon images, the hover-highlight
    image, the Tk root, the active tool's tracked process/fake-process,
    MAIN.py's own _active_tooltips set) is passed in explicitly by the
    caller, mirroring window_management.py's
    should_repin_topmost(foreground_pid, locked_gm_pid) pattern of pure
    arguments over implicit globals.
    MAIN.py's own add_tooltip() and its button-construction loops are
    never modified by this module or its callers -- every behavior
    change described below happens entirely inside activate_tool()/
    deactivate_all(), by adding to or temporarily overriding bindings
    that already exist on each canvas by the time activate_tool() runs.

MECHANISM:
    activate_tool(label, process, canvas_refs, icon_img_ids, icons,
    grayscale_icons, hover_bg, root, active_tooltips, on_finished) is
    the single entry point MAIN.py's launcher boundary calls once a
    tool has just been launched (frozen subprocess or dev-mode thread
    -- this module does not care which; it only ever calls .poll() on
    whatever process-like object it is given, matching the unified
    poll() contract MAIN.py's own _FakeProcess fix establishes). It:

    0. Withdraws and discards every tooltip currently in
       active_tooltips (MAIN.py's own _active_tooltips set) -- see
       STUCK TOOLTIP FIX below. Order relative to steps 1-4 does not
       matter for correctness (withdrawing a Toplevel is independent
       of what is bound to any canvas), so this runs first as the
       simplest place to put an unconditional, one-time cleanup step.
    1. Records label as the sole active tool (_active_label[0]).
    2. For every OTHER tracked canvas (grayed-out): swaps its icon
       layer's image for the cached grayscale version, makes its
       <Button-1> binding a no-op, sets its cursor to "no", and layers
       a supplementary <Enter> handler on top of add_tooltip()'s
       existing one (see GRAYED-OUT HOVER BEHAVIOR below) that
       immediately clears bg_img_id back to empty right after
       add_tooltip()'s own handler sets it -- so the tooltip still
       shows on hover, but the yellow background never visibly appears.
    3. For the ACTIVE canvas itself: keeps its icon in color, forces
       its background image to hover_bg (the same persistent highlight
       normal hovering already produces) for the duration, makes its
       <Button-1> binding a no-op too (re-launching the same tool while
       it is already running is blocked, not just launching a
       different one -- confirmed design decision), sets its cursor to
       "no" as well, and takes over its <Enter>/<Leave> bindings
       entirely so hovering it shows only the persistent yellow
       background with no tooltip popup. This part is UNCHANGED from
       the first version of this module.
    4. Starts a dedicated, self-rescheduling root.after() poll loop
       (owned entirely by this module -- deliberately NOT piggybacked
       onto MAIN.py's existing monitor_gm_state() 200ms loop, which
       already carries unrelated GM-sync/topmost/tooltip-repin
       responsibilities this module has no business coupling to) that
       checks process.poll() every EXCLUSIVITY_POLL_MS milliseconds.
       The moment poll() returns non-None, the tool is treated as
       finished -- regardless of exit code or how its window closed --
       and deactivate_all() runs automatically.

    Steps 0-3 above (reentrancy check, tooltip withdrawal, recording
    the "active" identity, the per-canvas swap loop) are implemented in
    a private helper, _activate_common(active_marker, canvas_refs,
    icon_img_ids, icons, grayscale_icons, hover_bg, root,
    active_tooltips), which activate_tool() calls with active_marker=
    label -- the per-canvas loop's own "is this the active one?" check
    (originally `if lbl == label:`) now reads `if lbl == active_marker:`,
    with identical behavior when active_marker is a real tool label.
    activate_tool() then layers its own poll-loop setup (step 4) on top,
    exactly as before this extraction -- see MUTABLE STATE below for
    what changed.

    activate_manual(canvas_refs, icon_img_ids, icons, grayscale_icons,
    hover_bg, root, active_tooltips) is the second entry point (see
    PURPOSE above): it calls _activate_common(_MANUAL_ACTIVE_MARKER,
    ...) and nothing else -- no process to track, so no step 4. Because
    _MANUAL_ACTIVE_MARKER (see MUTABLE STATE) never equals any real
    canvas label, _activate_common()'s per-canvas loop takes the
    "grayed-out" branch for every single tracked canvas, which is
    exactly the desired visual (the whole tool grid grays out; there is
    no "active tool icon" of its own to leave in color, since Configure
    Database is not one of the tracked tool icons at all). Raises the
    same RuntimeError, under the same re-entrancy rule, as
    activate_tool() -- both go through the same reentrancy check inside
    _activate_common(). The caller (MAIN.py) is responsible for calling
    deactivate_all() itself once the busy period ends -- there is no
    on_finished here, since there is no process whose completion this
    module could ever detect on activate_manual()'s own behalf.

    deactivate_all(canvas_refs, icon_img_ids, icons) is also exposed
    directly so MAIN.py can call it defensively (e.g. on its own
    shutdown path) without needing a live poll cycle to reach it first.
    Safe to call when no tool is active (no-ops).

    build_grayscale_icons(icons) builds the grayscale cache described
    below, once, at startup.

STUCK TOOLTIP FIX (confirmed regression, fixed this round):
    Before this fix, activate_tool() fully overrode the ACTIVE icon's
    <Leave> binding (step 3 above) without regard for whether
    add_tooltip()'s own tooltip Toplevel happened to already be visible
    at that exact moment -- an entirely ordinary sequence (hover long
    enough for the tooltip to appear, then click while still hovering).
    Since add_tooltip()'s leave() closure is the ONLY code that ever
    calls tooltip.withdraw() for that specific tooltip instance, and
    step 3 replaces that closure with a no-op, the tooltip was left
    indefinitely visible -- confirmed via the developer's own
    reproduction (a tool window open, its launching icon's tooltip
    still stuck on screen, cleared only by an incidental OS-level
    redraw such as alt-tab or a screenshot tool, never by this
    module's own logic). Grayed-out icons were never affected: their
    <Leave> binding is deliberately left untouched (see GRAYED-OUT
    HOVER BEHAVIOR below), so add_tooltip()'s real leave() -- and its
    tooltip.withdraw() call -- still fires normally for them.

    THE FIX: rather than requiring a way to identify which specific
    tooltip Toplevel belongs to the active canvas (add_tooltip() never
    exposes that mapping outside its own closure), activate_tool() now
    withdraws and discards EVERY tooltip in active_tooltips
    unconditionally, as step 0. This is deliberately broader than
    "just the active icon's own tooltip": by the time a tool is
    actually launching, any tooltip still technically tracked as
    visible belongs to a hover state that is now stale regardless of
    which icon it was for, so clearing the whole set is simpler and
    exactly as correct.

    Withdraws AND discards, not withdraw-only: MAIN.py's own
    add_tooltip() leave() closure and its existing CAMA-visibility-
    change handler (~line 4719) both always pair
    tooltip.withdraw() with _active_tooltips.discard(tooltip) --
    confirmed by reading both call sites and _repin_active_tooltips()'s
    own docstring, which states _active_tooltips "is expected to
    contain only currently-visible tooltip(s)". Withdrawing without
    discarding would leave a stale, invisible Toplevel in the set for
    _repin_active_tooltips() to keep calling .lift()/-topmost on every
    repin cycle -- confirmed empirically (in this sandbox) that doing
    so does NOT make a withdrawn Toplevel visible again (lift() and
    -topmost have no effect on a withdrawn window; only deiconify()
    would), so this was never a visible-reappearance risk -- but it
    would still violate that documented invariant and waste a repin
    cycle's worth of calls on a window nobody can see. Discarding too
    keeps the set consistent with what every other consumer expects.

GRAYED-OUT HOVER BEHAVIOR (revised -- see module changelog at the
bottom of this docstring for what changed and why):
    Confirmed requirements: a grayed-out icon must still show its
    tooltip on hover, but must NOT show the yellow hover_bg background
    anymore. The active icon's own behavior (persistent background,
    suppressed tooltip) is unchanged.

    Why this needs more than leaving the binding untouched or
    overriding it outright: add_tooltip()'s own enter() closure is ONE
    handler that does BOTH things at once -- shows the tooltip AND sets
    bg_img_id to hover_bg (confirmed by re-reading add_tooltip()'s
    signature, which takes canvas/bg_id specifically so its own enter()
    can reach bg_img_id directly). Leaving the binding fully untouched
    (the prior version's behavior) shows the background too. Overriding
    it outright loses the tooltip as well, which isn't wanted either.

    THE FIX: Tkinter supports stacking multiple handlers on the same
    event via widget.bind(sequence, func, add="+") -- registered
    commands then fire in registration order within the SAME event
    dispatch, rather than the second replacing the first (this is the
    exact mechanism whose ABSENCE, at the ORIGINAL add_tooltip()-after-
    make_bindings() call sites, caused the module's original hidden-
    coupling finding around hover ownership -- here it's put to
    deliberate use instead). For each grayed-out canvas, once grayed
    out, a SECOND <Enter> handler is registered with add="+": it does
    nothing but reset bg_img_id back to "" (empty), and it fires
    immediately after add_tooltip()'s own <Enter> handler (which fires
    first, since it was already bound before this module runs).
    <Leave> needs no supplementary handler -- add_tooltip()'s own
    leave() already resets bg_img_id to empty as part of its existing,
    untouched behavior, so grayed-out icons end up background-empty on
    mouse-leave regardless.

    VERIFIED, NOT ASSUMED, THAT THIS PRODUCES NO VISIBLE FLASH: tested
    empirically in this sandbox using the actual itemconfig(image=...)
    mechanism bg_img_id uses (not a color-fill proxy) -- the
    supplementary handler was confirmed to run within the SAME
    synchronous event dispatch as add_tooltip()'s own handler (zero
    pending Tk "after"/idle jobs existed between the two calls), and
    Tk's canvas repaint is idle-queue-driven -- meaning no screen paint
    can occur between the set and the immediate reset. The add="+"
    approach was chosen over the documented alternative (capturing
    add_tooltip()'s original <Enter> command and replacing it with a
    single new handler that calls the original via canvas.tk.call(...)
    then clears bg_img_id) because the empirical check found no flash
    risk, so the simpler, fire-order-based approach was preferred.

    RESTORE: the pre-add+ <Enter> command for each grayed-out canvas is
    captured (via canvas.bind("<Enter>") -- Tkinter's own binding
    introspection) BEFORE the supplementary handler is layered on, the
    same way the active icon's hover commands were already captured in
    the original version of this module. On deactivate, that captured
    command is restored verbatim via the widget's own Tcl interpreter
    (see RESTORE MECHANISM below) -- which cleanly strips the
    supplementary handler back off and leaves add_tooltip()'s original
    <Enter> behavior exactly as it was, confirmed empirically to
    reproduce a bind-table entry equal to the pre-add+ one.

CURSOR:
    Confirmed requirement: a "no" (Windows' standard not-allowed)
    cursor must show while hovering over EITHER a grayed-out icon OR
    the active icon itself -- both, not just grayed-out ones. `cursor`
    is a plain per-widget Canvas option, not something that needs
    binding-level logic -- Tk shows whichever cursor a widget is
    configured with automatically. activate_tool() sets
    canvas.config(cursor="no") on every tracked canvas (active and
    grayed-out alike); deactivate_all() resets every canvas back to
    canvas.config(cursor="") (Tk's default arrow). No saved/restored
    state is needed for this the way bindings need -- it is a plain,
    idempotent set-and-reset, matching the confirmed guidance that this
    needs no new mutable state.

    PLATFORM NOTE: "no" is a cursor name Tk only maps to a native
    cursor on Windows (confirmed against Tk's own cursors(n)
    documentation -- it is explicitly listed under Windows-only
    additional cursor names, alongside "starting", "wait", etc.; it is
    NOT among the cross-platform cursor names available on X11/macOS).
    This sandbox's Tk build (Linux/X11) genuinely cannot render it --
    confirmed empirically, it raises TclError: bad cursor spec "no" on
    this platform -- which is a real, expected platform gap, not a
    defect in this code; production runs on Windows, where "no" is a
    valid native cursor. Verification in this sandbox therefore checks
    that "no" is the value PASSED to canvas.config(cursor=...) (i.e.
    that this module attempts to set it, and resets correctly
    afterward), not that it visibly renders -- rendering itself needs
    the developer's own on-Windows test.

RESTORE MECHANISM (why bindings are captured/restored as raw Tcl
commands, not Python closures):
    activate_tool() does not know, and must not need to know, what
    add_tooltip() or make_bindings() actually bound to a given canvas's
    <Button-1>/<Enter>/<Leave> -- reconstructing those closures here
    would require importing MAIN.py (explicitly against this module's
    architectural role) or duplicating their logic (a maintenance
    hazard the first time either changes independently). Instead, the
    ACTUAL live Tcl command string is captured via canvas.bind(sequence)
    (Tkinter's own binding introspection) before this module overrides
    or layers onto anything, and restored verbatim via
    canvas.tk.call("bind", canvas._w, sequence, saved_cmd) -- confirmed
    empirically (in this sandbox, against a real Tk canvas) to produce
    a bind-table entry equal to the pre-override/pre-add+ one, which is
    the correct sufficient condition for identical dispatch behavior
    (Tk looks up and evaluates exactly this stored string).

DEPENDENCIES:
    stdlib: none beyond what tkinter itself pulls in (and this module
    never imports tkinter directly -- root/canvas objects are passed in
    by the caller, already-imported).
    third-party: PIL (Image, ImageOps) -- already a project dependency,
    already imported in MAIN.py for the existing color icons; this
    module introduces no new pip package. ImageOps specifically is new
    to the project's PIL usage (MAIN.py imports Image/ImageTk/
    ImageDraw but not ImageOps) but is still PIL, not a new dependency.
    local: none. This module is deliberately leaf-level.

MUTABLE STATE (single owner: this file; nothing outside it reads or
writes these):
    _active_label = [None]        -- the single active tool's label, or
                                      None when no tool is active. A
                                      single slot, not a set, since only
                                      one tool may ever be active by
                                      design. While a Configure Database
                                      (activate_manual()) session is
                                      active, this holds
                                      _MANUAL_ACTIVE_MARKER instead of a
                                      real label -- is_any_tool_active()
                                      only ever checks "is not None", so
                                      it correctly reports True for
                                      either kind of session without
                                      needing to know which.
    _MANUAL_ACTIVE_MARKER          -- a private sentinel object()
                                      (module-level, defined once,
                                      constant for the process lifetime)
                                      used as activate_manual()'s own
                                      "active_marker" value. An object()
                                      identity, not a string like
                                      "__manual__", specifically so it
                                      can never collide with a real tool
                                      label (which are always plain
                                      strings from MAIN.py's own
                                      TOOL_MODULES keys) -- no naming
                                      convention to accidentally violate,
                                      no chance of an actual tool one day
                                      being labelled the same thing.
    _poll_job = [None]            -- the current root.after() job id for
                                      the active poll loop, so
                                      deactivate_all() can cancel a
                                      pending poll before it fires again
                                      on now-stale state.
    _poll_root = [None]           -- the Tk root the current poll job
                                      was scheduled against, needed to
                                      call after_cancel() on it (Tkinter
                                      has no free-standing after_cancel
                                      -- it is a method on the specific
                                      Tk/widget instance the job was
                                      scheduled through).
    _grayscale_cache = {}         -- label -> ImageTk.PhotoImage,
                                      populated once by
                                      build_grayscale_icons().
    _saved_click_bindings = {}    -- label -> the Tcl bind command
                                      string captured from each canvas
                                      immediately before this module
                                      overrides <Button-1>. Populated
                                      for EVERY tracked canvas (active
                                      and grayed-out alike).
    _saved_enter_bindings = {}    -- label -> the Tcl bind command
                                      string captured from each canvas's
                                      <Enter> immediately before this
                                      module touches it -- for the
                                      active icon, before its <Enter> is
                                      fully overridden; for a grayed-out
                                      icon, before the supplementary
                                      add="+" handler is layered on.
                                      Populated for EVERY tracked
                                      canvas.
    _saved_leave_binding = [None] -- (label, leave_cmd) for the ACTIVE
                                      icon's <Leave> only -- the one
                                      canvas whose <Leave> this module
                                      still overrides outright (grayed-
                                      out icons' <Leave> is never
                                      touched, per GRAYED-OUT HOVER
                                      BEHAVIOR -- add_tooltip()'s own
                                      leave() already does the right
                                      thing for them). A single slot,
                                      not a dict, since only one icon
                                      can ever be the active one whose
                                      <Leave> was overridden.

CHANGELOG:
    v2: grayed-out icons now keep their tooltip on hover but no longer
    show the yellow background (previously they kept BOTH, per the
    original GRAYED-OUT HOVER BEHAVIOR resolution, which was explicitly
    flagged there as revisitable). Added a "no" cursor on every tracked
    canvas while a tool is active. Active icon's own behavior is
    unchanged. _saved_hover_bindings (a single (enter, leave) tuple
    keyed by label, active-icon-only) was split into
    _saved_enter_bindings (now populated for every canvas, since every
    canvas's <Enter> is now touched in some way) and
    _saved_leave_binding (still active-icon-only, since <Leave> is
    still only touched for that one canvas).

    v3: fixed a confirmed regression where a tooltip already visible at
    the moment a tool launches could be left stuck on screen
    indefinitely (see STUCK TOOLTIP FIX above). activate_tool() gained
    a new required parameter, active_tooltips (MAIN.py's own
    _active_tooltips set, passed in by reference), and a new step 0
    that withdraws and discards every tooltip in it. No other
    parameter's meaning changed; deactivate_all()'s signature is
    unaffected (the fix is entirely in activate_tool(), since the
    problem only ever occurs at activation time).

    v4 (this version): activate_tool()'s own steps 0-3 (everything
    except its poll-loop setup) were extracted, verbatim, into a new
    private helper, _activate_common(), so a second entry point,
    activate_manual(), could reuse them for a caller-managed busy
    period with no process to poll (the Configure Database dialog being
    open -- see PURPOSE/MECHANISM above). activate_tool()'s own
    signature, parameter meanings, and observable behavior are
    unchanged by this refactor -- it is a pure extraction, not a
    redesign. deactivate_all() required no changes at all: it already
    treats whatever _active_label[0] held as an opaque value to compare
    canvas labels against, so it restores correctly regardless of
    whether that value came from activate_tool() (a real label) or
    activate_manual() (_MANUAL_ACTIVE_MARKER).

WHY A DEDICATED POLL LOOP:
    MAIN.py already has monitor_gm_state(), a self-rescheduling
    root.after(200, ...) loop -- but it is busy with GM window sync,
    throttled topmost re-pinning, and tooltip re-pinning, none of which
    this module has any business coupling its own state changes to.
    EXCLUSIVITY_POLL_MS = 300 was chosen independently: this is a UI
    responsiveness concern, not a correctness one -- 300ms is fast
    enough that re-enabling the grid feels immediate to a user watching
    a tool's window close, and slow enough to be a negligible, one-line
    .poll() check per cycle.
"""

from PIL import Image, ImageOps, ImageTk

# ============================================================
# CONFIG
# ============================================================
EXCLUSIVITY_POLL_MS = 300  # see WHY A DEDICATED POLL LOOP above
DISABLED_CURSOR = "no"     # see CURSOR / PLATFORM NOTE above

# ============================================================
# MUTABLE STATE (see module docstring -- single owner: this file)
# ============================================================
_active_label = [None]
_poll_job = [None]
_poll_root = [None]
_grayscale_cache = {}
_saved_click_bindings = {}
_saved_enter_bindings = {}
_saved_leave_binding = [None]

# Sentinel "active_marker" value used by activate_manual() -- see
# module docstring, MUTABLE STATE. A plain object() so it can never
# equal any real tool label (always a string).
_MANUAL_ACTIVE_MARKER = object()


# ============================================================
# GRAYSCALE ICON CACHE
# ============================================================
def build_grayscale_icons(icons_pil):
    """
    Builds and returns a label -> ImageTk.PhotoImage dict: one
    grayscale conversion per entry in icons_pil, at whatever size each
    source image already is -- this function never resizes, it only
    recolors.

    Must be called with each icon's ORIGINAL PIL Image (post-resize,
    pre-PhotoImage), not MAIN.py's `icons` dict of already-converted
    ImageTk.PhotoImage objects -- PhotoImage has no pixel-level API to
    grayscale from. See MAIN.py's wiring for how this is threaded
    through alongside the existing icon_paths/icons construction
    without duplicating the resize call.

    ALPHA PRESERVATION: PIL's ImageOps.grayscale() on an RGBA source
    drops the alpha channel entirely (confirmed empirically against a
    synthetic sample icon before this function was written -- a plain
    ImageOps.grayscale(img.convert("RGBA")) silently turns transparent
    background pixels opaque gray). This function explicitly splits the
    source into its R/G/B/A channels, grayscales only the color
    channels, and remerges with the ORIGINAL alpha channel -- so a
    transparent icon background stays transparent in its grayscale
    version, and every remaining opaque pixel is true grayscale
    (R == G == B), not merely desaturated. Verified against a synthetic
    RGBA sample: 0 opaque-pixel R/G/B mismatches, and alpha values
    identical to the source at both a fully transparent and a fully
    opaque sample point.

    Args:
        icons_pil: dict of label -> a PIL Image.Image (already resized
            to the desired final size; RGBA or convertible to RGBA).

    Returns:
        dict of label -> ImageTk.PhotoImage, same keys as icons_pil.
        Also cached internally in _grayscale_cache for reuse.
    """
    cache = {}
    for label, pil_img in icons_pil.items():
        rgba = pil_img.convert("RGBA")
        r, g, b, a = rgba.split()
        gray_l = ImageOps.grayscale(rgba)
        gray_rgba = Image.merge("RGBA", (gray_l, gray_l, gray_l, a))
        cache[label] = ImageTk.PhotoImage(gray_rgba)
    _grayscale_cache.clear()
    _grayscale_cache.update(cache)
    return cache


# ============================================================
# QUERY (read-only)
# ============================================================
def is_any_tool_active() -> bool:
    """Read-only query: True if EITHER a Feature Management Tool
    (activate_tool()) OR a caller-managed busy period with no process
    to poll (activate_manual() -- currently: the Configure Database
    dialog being open) is currently active and deactivate_all() has not
    yet restored it, False otherwise. Deliberately does not distinguish
    which of the two is active -- every known consumer (the Feature
    Management Tools icon grid's own gating, DBGate's Update Map/
    Update Database gating, the hub button's own busy visual/click-
    gating, and each entry point's own re-entrancy check) treats the
    two kinds of session identically: everything stays grayed for the
    Configure Database dialog's entire lifetime, ungraying only once it
    actually closes, regardless of whether a CHANGE CONNECTION inside
    it succeeded, failed, or was never attempted -- the eventual status
    is checked at close time, not used to release anything early.

    Does not mutate any state."""
    return _active_label[0] is not None


# ============================================================
# ACTIVATION
# ============================================================
def activate_tool(label, process, canvas_refs, icon_img_ids, icons,
                   grayscale_icons, hover_bg, root, active_tooltips,
                   on_finished=None):
    """
    Marks label as the sole active tool and grays out/disables every
    other tracked icon, per the module docstring's MECHANISM section.
    Starts this module's own poll loop, which calls deactivate_all()
    automatically the instant process.poll() returns non-None.

    Args:
        label: the tool label that was just launched.
        process: the process/fake-process object returned by
            run_tool_by_label() -- anything with a .poll() method that
            returns None while running, non-None once finished (the
            unified contract Part A's _FakeProcess fix establishes;
            this module performs no frozen/dev-mode branching itself).
        canvas_refs: MAIN.py's existing canvas_refs dict, label ->
            (canvas, bg_img_id).
        icon_img_ids: label -> the canvas item id of that label's icon
            IMAGE layer (the id create_image() returned when the icon
            itself was drawn -- distinct from bg_img_id, which is the
            background/highlight layer). Needed so this module can swap
            the icon layer's image via itemconfig.
        icons: MAIN.py's existing color `icons` dict, label ->
            ImageTk.PhotoImage -- used to restore color on deactivate.
        grayscale_icons: the dict build_grayscale_icons() returned.
        hover_bg: MAIN.py's existing pre-rendered hover-highlight
            PhotoImage (the same image normal hovering already applies
            to bg_img_id).
        root: the Tk root, for scheduling and later cancelling this
            module's poll loop.
        active_tooltips: MAIN.py's own _active_tooltips set (passed by
            reference, mutated in place) -- every tooltip Toplevel
            currently tracked as visible is withdrawn and removed from
            this set as the first step of activation, fixing a
            confirmed stuck-tooltip regression (see module docstring,
            STUCK TOOLTIP FIX).
        on_finished: optional zero-arg callback invoked once, after
            this module's own restore work completes, so MAIN.py can
            hook additional cleanup without this module needing to know
            what that cleanup is.

    Raises:
        RuntimeError: if called while another tool is already active --
            see the re-entrancy note inline below.
    """
    if _active_label[0] is not None:
        # Re-entrancy guard: activate_tool() should never be called
        # while another tool is already active (mutual exclusivity is
        # meant to make that unreachable via the UI -- every icon,
        # including the one just clicked, is unclickable once a tool
        # is active). Fail loud rather than silently overwriting state
        # if a future call site ever bypasses the click-gating this
        # module installs.
        raise RuntimeError(
            f"activate_tool({label!r}) called while {_active_label[0]!r} "
            "is still active — mutual exclusivity invariant violated."
        )

    # Steps 0-3 (tooltip withdrawal, _active_label bookkeeping, and the
    # per-canvas swap loop) are shared with activate_manual() -- see
    # _activate_common()'s own docstring below and the module
    # docstring's MECHANISM section. This call is a pure, byte-for-byte
    # extraction of what used to be inline here: active_marker=label
    # makes _activate_common()'s `if lbl == active_marker:` check behave
    # exactly as the original `if lbl == label:` check did.
    _activate_common(label, canvas_refs, icon_img_ids, icons,
                      grayscale_icons, hover_bg, root, active_tooltips)

    _poll_root[0] = root
    _poll_job[0] = root.after(
        EXCLUSIVITY_POLL_MS,
        lambda: _poll_tick(process, canvas_refs, icon_img_ids, icons, root, on_finished),
    )


def _activate_common(active_marker, canvas_refs, icon_img_ids, icons,
                      grayscale_icons, hover_bg, root, active_tooltips):
    """
    Private helper shared by activate_tool() and activate_manual() --
    see module docstring, MECHANISM. Performs the STUCK TOOLTIP FIX
    withdrawal (step 0), records active_marker as the sole "active"
    identity (_active_label[0]), and runs the per-canvas swap loop
    (steps 1-3: cursor, click-block, and the active-vs-grayed-out
    branch) exactly as activate_tool() used to run them inline, before
    this extraction. Does NOT perform the re-entrancy check (each
    caller does its own, so each can raise its own, correctly-worded
    RuntimeError) and does NOT touch the poll loop (_poll_job/
    _poll_root) -- starting/not-starting a poll loop is entirely
    activate_tool()'s/activate_manual()'s own concern, since only
    activate_tool() ever has a process to poll.

    Args:
        active_marker: the value to record into _active_label[0] and to
            compare each tracked canvas's own label against. A canvas
            whose label equals active_marker is treated as "the active
            one" (stays in color, persistent hover highlight, tooltip
            suppressed); every other canvas is grayed out (grayscale
            icon, tooltip still shows on hover, yellow background
            suppressed). activate_tool() passes its own `label` here
            (a real tool label, so exactly one canvas matches);
            activate_manual() passes _MANUAL_ACTIVE_MARKER (which no
            real canvas label can ever equal, so every canvas takes the
            grayed-out branch -- there is no "active tool icon" for a
            Configure Database session, since it isn't one of the
            tracked tool icons at all).
        canvas_refs, icon_img_ids, icons, grayscale_icons, hover_bg,
        root, active_tooltips: identical in meaning to activate_tool()'s
            own parameters of the same names -- see that function's
            docstring.
    """
    # Step 0 -- see module docstring, STUCK TOOLTIP FIX. Unconditional:
    # withdrawing an already-withdrawn Toplevel is a safe no-op, so
    # there is no need to check visibility first. Withdraws AND
    # discards together (matching every existing consumer's own
    # pairing of the two calls) so active_tooltips stays consistent
    # with _repin_active_tooltips()'s documented invariant that it
    # holds only currently-visible tooltips.
    for _tip in list(active_tooltips):
        try:
            _tip.withdraw()
        except Exception:
            # Already destroyed at the Tcl level -- nothing left to
            # withdraw, just make sure it's dropped from the set below.
            pass
        active_tooltips.discard(_tip)

    _active_label[0] = active_marker

    for lbl, (canvas, bg_img_id) in canvas_refs.items():
        canvas.config(cursor=DISABLED_CURSOR)  # both active and grayed-out

        _saved_click_bindings[lbl] = canvas.bind("<Button-1>")
        canvas.bind("<Button-1>", lambda e: "break")

        if lbl == active_marker:
            # --- The active icon itself: stays in color, persistent
            # hover highlight, click inert, tooltip suppressed.
            # UNCHANGED from the prior version of this module. ---
            canvas.itemconfig(bg_img_id, image=hover_bg)

            _saved_enter_bindings[lbl] = canvas.bind("<Enter>")
            _saved_leave_binding[0] = (lbl, canvas.bind("<Leave>"))

            canvas.bind("<Enter>", lambda e: "break")
            canvas.bind("<Leave>", lambda e: "break")
        else:
            # --- Every other icon: grayscale, click inert, "no"
            # cursor, tooltip still shows on hover, yellow background
            # suppressed via a supplementary add="+" <Enter> handler
            # layered on top of add_tooltip()'s existing one -- see
            # module docstring, GRAYED-OUT HOVER BEHAVIOR. <Leave>
            # deliberately untouched: add_tooltip()'s own leave()
            # already clears bg_img_id. ---
            icon_item_id = icon_img_ids.get(lbl)
            if icon_item_id is not None and lbl in grayscale_icons:
                canvas.itemconfig(icon_item_id, image=grayscale_icons[lbl])

            _saved_enter_bindings[lbl] = canvas.bind("<Enter>")

            def _suppress_hover_bg(e, canvas=canvas, bg_img_id=bg_img_id):
                canvas.itemconfig(bg_img_id, image="")

            canvas.bind("<Enter>", _suppress_hover_bg, add="+")


def activate_manual(canvas_refs, icon_img_ids, icons, grayscale_icons,
                     hover_bg, root, active_tooltips):
    """
    Engages the same "something is busy" grayscale/click-block state
    activate_tool() applies, for a caller-managed busy period with no
    subprocess to poll (currently: the Configure Database dialog being
    open). Grays out EVERY tracked canvas -- there is no single "active
    icon" left in color, unlike activate_tool(), since a Configure
    Database session is not itself one of the tracked tool icons (see
    _activate_common()'s own docstring for the mechanics of how passing
    _MANUAL_ACTIVE_MARKER, which never equals a real canvas label,
    produces this).

    Raises RuntimeError under the same re-entrancy rule activate_tool()
    already enforces -- nothing else (another tool, or another
    Configure Database session) may be active at the same time.

    The caller releases this the same way an activate_tool() session is
    released: by calling deactivate_all() itself once the busy period
    ends. There is no automatic on_finished here, and no poll loop is
    started -- there is no process whose completion this module could
    ever detect on its own; the caller (MAIN.py) knows exactly when the
    busy period ends (the Configure Database dialog closing) and is
    responsible for calling deactivate_all() at that point, on every
    exit path.

    Args:
        canvas_refs, icon_img_ids, icons, grayscale_icons, hover_bg,
        root, active_tooltips: identical in meaning to activate_tool()'s
            own parameters of the same names -- see that function's
            docstring.

    Raises:
        RuntimeError: if called while another activate_tool()/
            activate_manual() session is already active.
    """
    if _active_label[0] is not None:
        # Re-entrancy guard, mirroring activate_tool()'s own (see that
        # function's inline comment for the full rationale) -- worded
        # for this entry point rather than reusing activate_tool()'s
        # exact text, since the caller here was never a tool label.
        raise RuntimeError(
            f"activate_manual() called while {_active_label[0]!r} is "
            "still active — mutual exclusivity invariant violated."
        )

    _activate_common(_MANUAL_ACTIVE_MARKER, canvas_refs, icon_img_ids, icons,
                      grayscale_icons, hover_bg, root, active_tooltips)


def _poll_tick(process, canvas_refs, icon_img_ids, icons, root, on_finished):
    """Internal: one poll cycle. Self-reschedules while the tool is
    still running; deactivates and stops rescheduling once
    process.poll() returns non-None. See module docstring, WHY A
    DEDICATED POLL LOOP."""
    if _active_label[0] is None:
        # Deactivated already via some other path (e.g. a defensive
        # deactivate_all() call) -- this tick is stale; do nothing and
        # do not reschedule.
        return

    if process.poll() is not None:
        deactivate_all(canvas_refs, icon_img_ids, icons)
        if on_finished is not None:
            on_finished()
        return

    _poll_job[0] = root.after(
        EXCLUSIVITY_POLL_MS,
        lambda: _poll_tick(process, canvas_refs, icon_img_ids, icons, root, on_finished),
    )


# ============================================================
# DEACTIVATION / RESTORE
# ============================================================
def deactivate_all(canvas_refs, icon_img_ids, icons):
    """
    Restores every tracked icon to its normal state: color image,
    original <Button-1> binding, original <Enter> binding (for a
    grayed-out icon this means stripping the supplementary add="+"
    handler back off; for the active icon it means restoring its fully
    overridden <Enter> too), the active icon's original <Leave>
    binding and cleared background image, and every canvas's cursor
    reset to default. Clears the active-label state and cancels any
    pending poll job. Safe to call even if no tool is currently active
    (no-ops in that case) -- this lets MAIN.py call it defensively on
    its own shutdown path without first checking whether a tool
    happens to be active.

    Args:
        canvas_refs: MAIN.py's existing canvas_refs dict, label ->
            (canvas, bg_img_id).
        icon_img_ids: the same label -> icon-layer-item-id mapping
            passed to activate_tool(), used to restore each icon
            layer's color image.
        icons: MAIN.py's existing color `icons` dict, label ->
            ImageTk.PhotoImage, used to restore each icon's normal
            image.
    """
    if _active_label[0] is None and not _saved_click_bindings:
        # Nothing to restore -- either never activated, or a previous
        # deactivate_all() already ran (e.g. called both by a poll tick
        # and defensively from MAIN.py's shutdown path in the same
        # pass). No-op rather than raising, per this function's
        # explicit "safe to call when nothing is active" contract.
        return

    if _poll_job[0] is not None and _poll_root[0] is not None:
        _poll_root[0].after_cancel(_poll_job[0])
    _poll_job[0] = None
    _poll_root[0] = None

    active_label_was = _active_label[0]
    _active_label[0] = None

    saved_leave_label, saved_leave_cmd = _saved_leave_binding[0] or (None, None)

    for lbl, (canvas, bg_img_id) in canvas_refs.items():
        icon_item_id = icon_img_ids.get(lbl)
        if icon_item_id is not None and lbl in icons:
            canvas.itemconfig(icon_item_id, image=icons[lbl])

        click_cmd = _saved_click_bindings.pop(lbl, None)
        _rebind_raw(canvas, "<Button-1>", click_cmd)

        enter_cmd = _saved_enter_bindings.pop(lbl, None)
        _rebind_raw(canvas, "<Enter>", enter_cmd)

        if lbl == active_label_was:
            canvas.itemconfig(bg_img_id, image="")
            if lbl == saved_leave_label:
                _rebind_raw(canvas, "<Leave>", saved_leave_cmd)

        canvas.config(cursor="")

    _saved_click_bindings.clear()
    _saved_enter_bindings.clear()
    _saved_leave_binding[0] = None


def _rebind_raw(canvas, sequence, tcl_cmd):
    """Internal: restores a previously-captured Tcl bind command string
    verbatim via the widget's own Tcl interpreter -- confirmed
    empirically to reproduce a bind-table entry identical to the
    pre-override/pre-add+ one (see module docstring, RESTORE
    MECHANISM). This works identically whether the prior state being
    restored was a full override (the active icon's <Enter>/<Leave>) or
    an add="+" stack (a grayed-out icon's <Enter>) -- either way, the
    captured string is the exact bind-table entry that existed
    immediately before this module touched it, and re-installing it
    verbatim reproduces that entry exactly, stripping away whatever
    this module added or replaced. Clears the binding instead if
    tcl_cmd is falsy (defensive fallback; should not normally happen,
    since every canvas this module touches always had SOME <Button-1>
    and <Enter> binding from make_bindings()/add_tooltip() by the time
    activate_tool() ran)."""
    if tcl_cmd:
        canvas.tk.call("bind", canvas._w, sequence, tcl_cmd)
    else:
        canvas.bind(sequence, lambda e: None)