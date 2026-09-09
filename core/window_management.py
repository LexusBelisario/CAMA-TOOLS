"""
core/window_management.py

PURPOSE:
    New module (this task) holding all Win32/process/window-management
    logic for three related fixes at the CAMA Tools launcher boundary:

    TASK A -- Single-instance enforcement + duplicate-launch dialog.
        A second launch of CAMA-Tools.exe, while a first instance is
        still running, must not create a second session. Instead it
        raises the existing Global Mapper window then the existing CAMA
        panel above it (both disabled for input), shows a small
        "CAMA Tools is already running" dialog, and exits once the
        dialog is dismissed -- either by the user clicking OK, or
        automatically if the first instance is detected to have died
        while the dialog was still showing.

    TASK B -- Global Mapper window identification fix.
        launch_global_mapper()/wait_for_global_mapper() in MAIN.py
        currently lock onto the first "Global Mapper Pro"-titled window
        found, with no way to distinguish CAMA's own just-launched GM
        instance from a pre-existing, unrelated GM window. This module
        provides the candidate-identification logic (PID match,
        authoritative; HWND-snapshot-diff, fallback; never an arbitrary
        guess) -- MAIN.py's wait_for_global_mapper() calls into it but
        keeps its own polling/stability loop unchanged.

    TASK C -- Topmost re-pin scope fix.
        MAIN.py's periodic topmost re-pin currently fires whenever ANY
        "relevant" window has focus (GM, CAMA itself, or any CAMA tool
        subprocess), which pushes transient popups (a file dialog's own
        dropdowns, an Explorer context menu) behind CAMA even when GM
        had nothing to do with that moment. This module provides a
        narrower, pure predicate -- should_repin_topmost() -- for
        MAIN.py's periodic re-pin call sites to use INSTEAD of the
        broader is_relevant_window_focused() check, for that one
        decision only.

NOT this module's responsibility (per the approved module boundary --
stays in MAIN.py, unmodified by this task):
    is_relevant_window_focused(), get_foreground_hwnd(),
    get_foreground_pid(), _locked_gm_snapshot(), _extract_hwnd(),
    monitor_gm_closure(), the gm_moved follow/reposition logic,
    hide_from_taskbar(), install_wm_moving_hook(), tooltip
    pinning/repinning. This module does not import from MAIN.py and
    MAIN.py's own get_foreground_pid() is what feeds
    should_repin_topmost()'s foreground_pid argument -- no duplicate
    foreground-window lookup exists in this file.

DEPENDENCIES:
    stdlib only: ctypes, ctypes.wintypes, os, sys, json, tempfile, time,
    tkinter. No new pip package introduced (see Instructions Section D).

CROSS-PROCESS STATE MECHANISM (Task A <-> Task B bridge):
    _locked_gm_hwnd[0]/_locked_gm_pid[0] in MAIN.py are plain Python
    list globals living in the first instance's own process memory -- a
    second, separate OS process cannot read them directly. This module
    bridges that gap with a small JSON state file,
    <temp_dir>/cama_session_state.json (temp_dir is supplied by the
    caller -- this module never hardcodes a copy of MAIN.py's own
    TEMP_DIR constant, to avoid the two ever drifting apart):

        {"first_instance_pid": <int>, "locked_gm_hwnd": <int>,
         "locked_gm_pid": <int>}

    Written ONLY by the first instance (write_session_state()), ONLY
    after wait_for_global_mapper() has already verified and locked a
    real candidate (Task B) -- never a guess. Written via a temp-file +
    os.replace() atomic swap so a concurrently-launched duplicate-launch
    instance can never observe a partially-written file.

    Read by a duplicate-launch instance via
    read_and_validate_session_state(), which trusts the recorded GM
    HWND/PID only if ALL of the following hold at read time:
        1. The file parses with the expected keys.
        2. Its recorded first_instance_pid matches the PID the caller
           independently derived from the live "CAMA Tools" HWND (guards
           against a stale file left by a since-crashed, unrelated
           earlier session).
        3. The recorded HWND passes IsWindow() right now.
        4. GetWindowThreadProcessId() for that HWND, checked right now,
           still equals the recorded PID (guards against Windows having
           recycled that HWND value onto an unrelated window after the
           original GM process exited).
    (First-instance liveness itself -- "is first_instance_pid still
    alive" -- is covered by the caller's own OpenProcess/
    WaitForSingleObject check in handle_duplicate_launch(), performed
    before this validation is even reached, rather than duplicated here.)
    If validation fails for any reason, the caller gets None back and
    must fall back to CAMA-only handling -- this module NEVER falls back
    to an arbitrary "Global Mapper Pro" window.

    Correctness does not depend on cleanup_session_state() actually
    running (a crashed first instance leaving the file behind is
    expected and handled by the validation chain above, not by cleanup
    discipline) -- but MAIN.py's wiring gate should still call it on
    normal shutdown as good hygiene.

MUTEX SEMANTICS:
    MUTEX_NAME (Global\\ namespace -- approved: CAMA Tools is a normal
    single-user desktop/workstation deployment, not concurrent multi-
    user RDS/Citrix, so machine-wide singleton scope is the intended
    architecture) is created with CreateMutexW(..., bInitialOwner=True,
    ...) via a ctypes.WinDLL("kernel32", use_last_error=True) handle so
    ctypes.get_last_error() reliably reports ERROR_ALREADY_EXISTS. Note:
    bInitialOwner is meaningless when CreateMutexW returns a handle to
    an ALREADY-EXISTING named object (the duplicate-launch case) -- it
    only matters for the winning, first-creating call, which is exactly
    the case this module sets it True for. WaitForSingleObject is NEVER
    called against MUTEX_NAME, by design -- existence (via
    GetLastError()) is the only signal used for the singleton gate
    itself; process-handle liveness (OpenProcess + WaitForSingleObject
    on FIRST_INSTANCE_PID's own process handle) is the actual liveness
    signal, kept entirely separate.

    DIALOG_GUARD_MUTEX_NAME uses a two-stage pattern: a fresh
    open-check-close existence test (_check_dialog_guard_exists()) for
    every CHECKING process, and a separate acquire-and-hold (never
    polled via WaitForSingleObject) for the one process that goes on to
    actually show the dialog (_acquire_dialog_guard()) -- held for that
    dialog's entire lifetime, released in handle_duplicate_launch()'s
    finally block.

CLEANUP / RESTORE GUARANTEES:
    - release_singleton() explicitly closes the singleton mutex handle
      for normal shutdown; not required for crash cleanup (the OS
      releases/closes process handles automatically on termination,
      including a crash -- confirmed Win32 process-termination
      semantics), but a clean intentional shutdown should not rely on
      that fallback alone.
    - handle_duplicate_launch()'s entire disable -> dialog -> restore
      sequence runs inside a try/finally: the finally block ALWAYS
      restores only the windows THIS invocation itself found enabled
      and then disabled (never force-enables a window that was already
      disabled for an unrelated reason beforehand), re-validating
      IsWindow() before touching either HWND again (either one may have
      closed while the dialog was up), then closes the dialog Toplevel,
      the FIRST_INSTANCE_PID process handle, and the dialog-guard mutex
      handle, in that order, regardless of which path (OK-click, stale-
      detection, or an exception) got there.

KNOWN MACHINE-DEPENDENT BEHAVIOR THAT STILL REQUIRES ON-MACHINE TESTING
(cannot be confirmed by static code reading alone -- see Instructions
Section G.4):
    1. Task B's own documented limitation (restated, unchanged): whether
       Global Mapper's launched-process PID matches its eventual main
       window's owning PID 1:1, or whether GM reuses an existing
       process/window in some configurations.
    2. RESOLVED (was open, now confirmed on-machine): the dialog's
       z-order mechanism originally included GWLP_HWNDPARENT ownership
       of cama_hwnd alongside HWND_TOPMOST. On-machine testing confirmed
       this as the cause of the dialog being excluded from Alt+Tab --
       Windows' task-switch list deliberately omits owned windows
       (documented Win32 behavior). Ownership was removed; the dialog is
       now HWND_TOPMOST-pinned (_set_topmost()) plus explicitly
       activated (_bring_to_foreground()) as a fully independent
       top-level window, and IS Alt+Tab visible. The dialog's z-order
       precedence for its full lifetime against the first instance's own
       periodic topmost re-pin loop (monitor_gm_state(), still running
       in the first instance's process, unaffected by this module)
       remains a residual, unresolved risk -- Task C's narrowing
       (should_repin_topmost() firing only when GM itself has focus)
       reduces the collision window but does not eliminate it, since a
       user could refocus GM or CAMA while the dialog is up. Resolving
       it further would require MAIN.py changes, out of scope for this
       file.
    3. RESOLVED (was open, investigated via added diagnostic logging,
       now understood -- see _try_set_foreground()'s [fg-diag] print()
       instrumentation, still present in the code as of this writing for
       one confirming retest): initial _bring_to_foreground() reports
       showed inconsistent-seeming success across on-machine duplicate-
       launch tests, narrowed to the dialog appearing not to be
       foreground specifically at the native file-picker ("Select Global
       Mapper Workspace File") stage. Direct Win32 measurement
       (GetForegroundWindow(), checked at 0ms/10ms/50ms/100ms after each
       SetForegroundWindow() call, plus the state at the start of the
       subsequent 500ms deferred pass) showed the dialog's own HWND WAS
       genuinely the foreground window at every checkpoint, with no
       steal-back and no delay. There was no foreground-lock failure, no
       evidence the earlier ALT-key fallback was ever actually needed,
       and no evidence that Tk's mainloop() timing was the problem --
       those were reasonable hypotheses at the time, but the diagnostic
       data does not support them as the explanation here.
       What the logging DID surface: the first-instance CAMA main window
       and this dialog were BOTH titled "CAMA Tools" (MAIN.py sets the
       former; this dialog previously set the same string via
       top.title()) -- two different HWNDs, same title, both visible in
       Alt+Tab. The technically accurate conclusion is: the Win32
       diagnostics confirm the duplicate-launch dialog was actually
       becoming and remaining the foreground window; the earlier visual
       observation was ambiguous because two different windows shared an
       identical title, not because of a confirmed Windows/Alt+Tab
       display defect. The dialog's title was changed to "CAMA Tools -
       Already Running" (cosmetic only -- no z-order, ownership, mutex,
       or foreground-activation logic changed) so the two windows are
       distinguishable in Alt+Tab going forward. The single bounded
       ALT-key fallback in _bring_to_foreground() (keybd_event(VK_MENU,
       ...) + one retry, if the first attempt is denied) and the
       deferred 50ms/500ms scheduling of the activation call both remain
       in place as harmless, already-bounded (non-looping) mechanisms,
       even though the diagnostic evidence suggests neither was actually
       the fix for what was observed -- removing them is not necessary,
       but no further foreground-activation mechanisms should be added
       on top of this without new evidence.
    4. The narrow OpenProcess(first_instance_pid) failure race (window
       still found via FindWindowW, but the owning process has already
       exited by the time OpenProcess runs): this module's chosen policy
       -- still perform the normal raise/disable/dialog sequence, but
       skip the auto-liveness poll (dialog then closes only via user
       OK-click) -- is a judgment call (category (c) per Section G.4),
       not a documented Win32 contract, and should be exercised on a
       real machine if feasible (e.g. via a scripted near-simultaneous
       kill).
    5. Whether SeCreateGlobalPrivilege is actually available to the
       interactive user on all target deployment machines (required for
       the Global\\ mutex namespace) -- expected to be true for normal,
       non-hardened desktop/workstation Windows per the confirmed
       deployment model, but not independently re-verified against any
       specific target machine's Group Policy here.
"""

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import sys
import tempfile
import time
import tkinter as tk
import traceback
from tkinter import messagebox

# ============================================================
# WIN32 CONSTANTS
# ============================================================
ERROR_ALREADY_EXISTS = 183

SYNCHRONIZE = 0x00100000

WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF

SW_RESTORE = 9

HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010

# For the single bounded ALT-key foreground-activation fallback in
# _bring_to_foreground() -- see that function's docstring for the exact,
# deliberately-limited scope of this technique.
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002

# ============================================================
# NAMED KERNEL OBJECTS
# ============================================================
# Global\ namespace: approved deployment model is single-user desktop/
# workstation Windows, never concurrent multi-user RDS/Citrix -- see
# module docstring MUTEX SEMANTICS. Machine-wide singleton scope is the
# intended architecture, not an oversight.
MUTEX_NAME = r"Global\CAMA_Tools_Singleton_Mutex"
DIALOG_GUARD_MUTEX_NAME = r"Global\CAMA_Tools_DuplicateDialog_Mutex"

# ============================================================
# WINDOW IDENTIFICATION CONSTANTS
# ============================================================
# Exact-match title, same string MAIN.py already sets via root.title()
# and already looks up via FindWindowW(None, "CAMA Tools") elsewhere
# (get_cama_size(), install_wm_moving_hook() vicinity) -- proven,
# already-working pattern, not a new risk.
CAMA_WINDOW_TITLE = "CAMA Tools"

# Substring used only by this module's OWN raw EnumWindows scan
# (snapshot_gm_hwnds()) -- this module does not import pygetwindow.
GM_WINDOW_TITLE_SUBSTRING = "Global Mapper Pro"

SESSION_STATE_FILENAME = "cama_session_state.json"

# ============================================================
# TIMING CONSTANTS
# ============================================================
CAMA_HWND_RETRY_INTERVAL_MS = 150
CAMA_HWND_RETRY_TIMEOUT_MS = 5000
FIRST_INSTANCE_POLL_INTERVAL_MS = 1500

# ============================================================
# DLL HANDLES
# ============================================================
# use_last_error=True is required here specifically: this is the first
# place in the codebase that needs a reliable GetLastError() reading
# (ERROR_ALREADY_EXISTS from CreateMutexW). MAIN.py's own existing raw
# ctypes.windll.user32 calls elsewhere never rely on GetLastError()
# afterward (they use return-value semantics only -- IsWindow,
# GetWindowRect, etc.), so that existing pattern is not reused here.
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.windll.user32

_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

_kernel32.GetCurrentThreadId.argtypes = []
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD

_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowW.restype = wintypes.HWND
_user32.GetParent.argtypes = [wintypes.HWND]
_user32.GetParent.restype = wintypes.HWND
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.AttachThreadInput.restype = wintypes.BOOL
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsIconic.restype = wintypes.BOOL
_user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_void_p]
_user32.keybd_event.restype = None

# Module-level singleton mutex handle -- explicit lifetime, see
# acquire_singleton() / release_singleton().
_singleton_mutex_handle = None


# ============================================================
# LOW-LEVEL WIN32 HELPERS (private -- thin wrappers only, no business
# logic; MAIN.py has its own equivalents for its own unrelated call
# sites, deliberately not shared, since MAIN.py imports this module and
# not the reverse)
# ============================================================
def _find_window(title):
    """Exact-match FindWindowW lookup. Returns HWND int, or None if not
    found."""
    hwnd = _user32.FindWindowW(None, title)
    return hwnd if hwnd else None


def _get_window_pid(hwnd):
    """Returns the PID owning hwnd via GetWindowThreadProcessId."""
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _get_window_title(hwnd):
    """
    DIAGNOSTIC HELPER: best-effort GetWindowTextW wrapper, used only for
    print() logging (see _try_set_foreground()'s diagnostic instrumen-
    tation) -- never used for any identification/matching logic
    elsewhere in this module (title-based matching for actual behavior
    stays confined to CAMA_WINDOW_TITLE/GM_WINDOW_TITLE_SUBSTRING lookups
    as before). Returns "" on any failure or for hwnd=0/None -- never
    raises, since it must never be allowed to disrupt the function it's
    diagnosing.
    """
    if not hwnd:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetWindowTextW(hwnd, buf, 256)
        return buf.value
    except Exception:
        return ""


def _is_window(hwnd):
    """True if hwnd is still a valid window handle right now."""
    return bool(_user32.IsWindow(hwnd))


def _is_window_enabled(hwnd):
    return bool(_user32.IsWindowEnabled(hwnd))


def _enable_window(hwnd, enable):
    _user32.EnableWindow(hwnd, wintypes.BOOL(enable))


def _try_set_foreground(hwnd):
    """
    A single attempt at SetForegroundWindow(hwnd), using the
    AttachThreadInput-to-current-foreground-thread technique: temporarily
    attach THIS thread's input queue to the CURRENT FOREGROUND window's
    thread ONLY -- never to hwnd's own thread, which has no documented
    basis and is deliberately not done here. This does NOT guarantee
    SetForegroundWindow() will succeed -- it is one documented technique
    that interacts with Windows' actual foreground-permission rules, not
    a bypass of them. Both GetForegroundWindow() (can return NULL) and
    GetWindowThreadProcessId() (returns 0 on failure) are treated as
    fallible: AttachThreadInput() is attempted only if a real foreground
    HWND and a real, non-zero, non-self thread ID were both obtained.

    Extracted as its own function so _bring_to_foreground() can call it
    twice (once directly, once after a single bounded ALT-key fallback)
    without duplicating the attach/detach bookkeeping. Does NOT perform
    the IsIconic/SW_RESTORE step -- that stays in _bring_to_foreground(),
    done once per call regardless of how many foreground attempts follow.

    DIAGNOSTIC LOGGING (temporary, does NOT affect behavior, return
    value, or control flow -- print() only): on-machine testing showed
    SetForegroundWindow() consistently reporting True for the dialog
    while it stayed non-foreground and last in Alt+Tab order at the
    native file-picker stage -- meaning the boolean return alone is not
    sufficient evidence of actual foreground state. To distinguish "the
    call succeeds and something steals foreground back moments later"
    from "the call's True result doesn't correspond to the target
    actually becoming foreground at all", this function now logs:
      - BEFORE the call: the target's own HWND/title/PID, and the
        CURRENT foreground window's HWND/title/PID.
      - The SetForegroundWindow() call's own boolean result.
      - GetForegroundWindow() checked at four cumulative checkpoints
        (0ms, 10ms, 50ms, 100ms after the call), each logged with the
        foreground HWND/title and whether it equals the target hwnd.
    GetForegroundWindow() is the direct Win32 measurement used for the
    checkpoints -- Alt+Tab MRU ordering is NOT used as a diagnostic
    signal here, since shell MRU behavior isn't necessarily identical to
    the actual foreground HWND. The small time.sleep() calls this adds
    (up to 100ms total) are an accepted, temporary diagnostic cost --
    this function is only ever called a handful of times per duplicate-
    launch dialog, not in any hot path.

    Returns the actual observed SetForegroundWindow() boolean result
    (False also covers hwnd already being invalid, or the call never
    being attempted because hwnd vanished during setup) -- unchanged by
    the diagnostic logging above.
    """
    if not _is_window(hwnd):
        return False

    target_title = _get_window_title(hwnd)
    target_pid = _get_window_pid(hwnd)
    before_fg_hwnd = _user32.GetForegroundWindow()
    before_fg_title = _get_window_title(before_fg_hwnd)
    before_fg_pid = _get_window_pid(before_fg_hwnd) if before_fg_hwnd else 0
    print(
        f"  [fg-diag] BEFORE: target_hwnd={hwnd} target_title={target_title!r} "
        f"target_pid={target_pid} | current_fg_hwnd={before_fg_hwnd} "
        f"current_fg_title={before_fg_title!r} current_fg_pid={before_fg_pid}"
    )

    current_thread_id = _kernel32.GetCurrentThreadId()
    fg_hwnd = before_fg_hwnd
    fg_thread_id = 0
    if fg_hwnd:
        fg_thread_id = _user32.GetWindowThreadProcessId(fg_hwnd, None)

    attached = False
    if fg_thread_id and fg_thread_id != current_thread_id:
        attached = bool(
            _user32.AttachThreadInput(current_thread_id, fg_thread_id, True)
        )

    try:
        # Cheap re-check right before the operation this attachment was
        # set up for -- not a strict safety requirement (a stale HWND
        # passed to SetForegroundWindow() simply fails gracefully,
        # returning False, rather than crashing -- Win32 calls are safe
        # against stale handles by design), but avoids attempting the
        # call at all if hwnd vanished during the AttachThreadInput()
        # setup above.
        if not _is_window(hwnd):
            return False

        result = bool(_user32.SetForegroundWindow(hwnd))
        print(f"  [fg-diag] SetForegroundWindow(hwnd={hwnd}) = {result}")

        elapsed_ms = 0
        for checkpoint_ms in (0, 10, 50, 100):
            if checkpoint_ms > elapsed_ms:
                time.sleep((checkpoint_ms - elapsed_ms) / 1000.0)
                elapsed_ms = checkpoint_ms
            check_fg_hwnd = _user32.GetForegroundWindow()
            check_fg_title = _get_window_title(check_fg_hwnd)
            target_is_foreground = (check_fg_hwnd == hwnd)
            print(
                f"  [fg-diag] @ {checkpoint_ms}ms: fg_hwnd={check_fg_hwnd} "
                f"fg_title={check_fg_title!r} target_is_foreground={target_is_foreground}"
            )

        return result
    finally:
        if attached:
            _user32.AttachThreadInput(current_thread_id, fg_thread_id, False)


def _bring_to_foreground(hwnd):
    """
    Best-effort attempt to bring hwnd to the foreground/active state.
    Replaces the earlier _restore_and_foreground() -- see Phase 1
    analysis for the two bugs this fixes.

    BUG 1 FIX (GM losing its maximized/full-screen state): ShowWindow(
    hwnd, SW_RESTORE) is called ONLY when IsIconic(hwnd) is currently
    True (i.e. hwnd is actually minimized right now). Per Microsoft's
    own documented SW_RESTORE behavior, this call restores a window to
    its "original size and position" regardless of whether it was
    minimized OR maximized beforehand -- calling it unconditionally on
    an already-maximized Global Mapper window incorrectly returns it to
    its pre-maximize (smaller) rectangle. If hwnd is already normal,
    maximized, or otherwise visible and not iconic, its show-state is
    left completely untouched here.

    BUG 2 FIX (SetForegroundWindow() silently denied for a background
    process): the first attempt is _try_set_foreground(hwnd) (see that
    function's docstring for the AttachThreadInput mechanism).

    EXPERIMENTAL ALT-KEY FALLBACK (single bounded retry, NOT a
    guaranteed/documented fix): on-machine testing showed the first
    attempt failing specifically while a native common file-picker
    dialog (IFileOpenDialog-based "Select Global Mapper Workspace File")
    has the foreground, while succeeding for CAMA's own credential
    dialog and for Global Mapper itself -- suggesting a timing/
    interaction specific to that one stage rather than a general failure
    of the AttachThreadInput approach. If the first _try_set_foreground()
    call is denied, this function simulates a single ALT key press+
    release (keybd_event(VK_MENU, ...)) -- one of Microsoft's documented
    SetForegroundWindow() exception conditions is "the process received
    the last input event," so generating a real input event is a
    plausible way to satisfy it -- and retries EXACTLY ONCE via a second
    _try_set_foreground() call. This is NOT claimed to be a guaranteed or
    officially documented fix for the native-file-picker case
    specifically; it is a single bounded experiment, deliberately never
    looped or retried further, so its actual effectiveness can be read
    from the diagnostic log below rather than assumed.

    Diagnostic logging (print(), best-effort, does not affect control
    flow) reports the result of each stage, specifically so on-machine
    testing can distinguish:
      (a) first attempt failed, ALT retry succeeded -- evidence the
          fallback helps for this case;
      (b) both attempts failed -- ALT is not the fix here;
      (c) first attempt actually succeeded, logged as True -- if the
          window still isn't visually foreground in that case despite
          this function reporting success, the cause is elsewhere
          (z-order/timing), not activation, and should be investigated
          separately rather than blamed on this function.

    Returns:
        bool -- True if EITHER attempt succeeded, False if both failed
        (or hwnd was already invalid at entry). Best-effort signal for
        the caller to optionally log further -- callers must not treat
        False as an error requiring special handling.
    """
    if not _is_window(hwnd):
        return False

    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, SW_RESTORE)

    first_result = _try_set_foreground(hwnd)
    print(f"  [foreground] hwnd={hwnd} first attempt: {first_result}")
    if first_result:
        return True

    # Single bounded ALT-key fallback -- exactly one retry, never looped.
    _user32.keybd_event(VK_MENU, 0, 0, None)
    _user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, None)
    second_result = _try_set_foreground(hwnd)
    print(f"  [foreground] hwnd={hwnd} ALT fallback attempted, second attempt: {second_result}")

    return second_result


def _set_topmost(hwnd):
    """Genuine Win32 HWND_TOPMOST pin, SWP_NOACTIVATE (does not steal
    keyboard focus) -- same call shape MAIN.py already uses for CAMA's
    own topmost pinning."""
    _user32.SetWindowPos(
        hwnd, HWND_TOPMOST, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
    )


# NOTE: an earlier revision had a _set_owner(child_hwnd, owner_hwnd)
# helper here (GWLP_HWNDPARENT-based ownership) as one half of the
# dialog's z-order mechanism, alongside _set_topmost() above. Removed --
# confirmed, on-machine, as the root cause of the "already running"
# dialog being invisible in Alt+Tab: Windows' task-switch list
# deliberately excludes owned windows (documented Win32 behavior, not
# implementation-specific). _set_topmost() (HWND_TOPMOST) plus
# _bring_to_foreground() (activation) are the z-order/focus mechanism
# now -- see their call site in handle_duplicate_launch() for the
# dialog. GWLP_HWNDPARENT and SetWindowLongPtrW are no longer used
# anywhere in this module as a result.


def _fail_closed_startup_error(message):
    """
    Shows a startup error dialog via a disposable Tk root -- no
    dependency on MAIN.py's real root, which does not exist yet at the
    point acquire_singleton() runs (immediately after
    dispatch_tool_if_requested() returns, before MAIN.py's own
    root = tk.Tk() line). Same disposable-Tk-for-an-error-dialog pattern
    MAIN.py's own dispatch_tool_if_requested() already uses (r = Tk();
    r.withdraw(); messagebox...) -- not a new pattern in this codebase.
    """
    try:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror("CAMA Tools - Startup Error", message)
        r.destroy()
    except Exception:
        # If Tk itself can't initialize, there is nothing further this
        # function can do to inform the user visually.
        pass


def _show_duplicate_launch_error(traceback_text):
    """
    DIAGNOSTIC (temporary, for on-machine debugging of a currently-
    unexplained "no dialog appears" report -- see handle_duplicate_
    launch()'s except clause). Shows the ALREADY-CAPTURED traceback text
    in a disposable-Tk messagebox -- same pattern as _fail_closed_
    startup_error(). Deliberately takes the traceback as a string the
    caller already captured (via traceback.format_exc(), before calling
    this function), rather than calling traceback.format_exc() itself,
    so that if THIS function's own Tk usage fails, the original
    traceback is never lost -- the caller already has it and prints it
    to stderr regardless of whether this dialog succeeds.

    Once the actual failing operation behind a captured traceback is
    identified and fixed, this helper (and the except clause that calls
    it) should be re-evaluated -- kept as a permanent production safety
    net, narrowed, or removed -- rather than assumed to be the fix
    itself.
    """
    try:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(
            "CAMA Tools - Duplicate Launch Error (diagnostic)",
            "An unexpected error occurred while handling a duplicate "
            "launch. Full traceback:\n\n" + traceback_text
        )
        r.destroy()
    except Exception:
        # If Tk itself can't initialize/display here, there is nothing
        # further this function can do visually -- the caller already
        # has the traceback and prints it to stderr regardless.
        pass


# ============================================================
# TASK A -- SINGLETON MUTEX
# ============================================================
def acquire_singleton(temp_dir):
    """
    Must be called immediately after dispatch_tool_if_requested()
    returns False in MAIN.py's launcher-mode startup, before MAIN.py's
    real root = tk.Tk() line and before any .gmw prompt / credential-
    dependent code -- see module docstring and the approved Phase 1
    placement discussion for why this exact spot matters (dispatch_
    tool_if_requested() must run first so --tool subprocess dispatches
    are never gated by the singleton at all).

    Args:
        temp_dir: MAIN.py's own TEMP_DIR (already created by that point
            in MAIN.py's startup sequence). Passed through to
            handle_duplicate_launch() for session-state-file access;
            never hardcoded in this module.

    Behavior:
        - CreateMutexW(..., bInitialOwner=True, MUTEX_NAME) returns a
          handle to a NEW object: this process is the first instance.
          The handle is kept in the module-level _singleton_mutex_handle
          for this process's entire lifetime; this function returns
          normally and the caller proceeds with normal launcher startup,
          completely unchanged.
        - CreateMutexW succeeds but GetLastError() == ERROR_ALREADY_
          EXISTS: this process is a duplicate. bInitialOwner is
          meaningless for an already-existing named object (see module
          docstring) -- this handle is just a fresh reference to the
          SAME kernel object the first instance created, needed for
          nothing further, so it is closed immediately. Control passes
          to handle_duplicate_launch(), which never returns normally
          (always sys.exit(0)).
        - CreateMutexW itself fails (returns a NULL handle -- a genuine
          Win32 call failure, distinct from ERROR_ALREADY_EXISTS): FAILS
          CLOSED, per approved decision. The single-instance invariant
          cannot be guaranteed if the gate itself can't be created, so
          this process does not silently proceed as if it were the
          first instance, and does not fall back to a Local\\-namespace
          mutex (that would silently change singleton scope without an
          explicit deployment decision). Shows a startup error and
          exits with status 1.

    Never calls WaitForSingleObject against MUTEX_NAME for any purpose
    -- existence via GetLastError() is the only signal used against
    this specific mutex, by design.
    """
    global _singleton_mutex_handle

    ctypes.set_last_error(0)
    handle = _kernel32.CreateMutexW(None, True, MUTEX_NAME)
    last_error = ctypes.get_last_error()

    if not handle:
        _fail_closed_startup_error(
            "Could not create the CAMA Tools single-instance lock "
            f"(Win32 error {last_error}). CAMA Tools cannot verify "
            "whether another instance is already running, so it will "
            "not start."
        )
        sys.exit(1)

    if last_error == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        handle_duplicate_launch(temp_dir)
        # handle_duplicate_launch() always calls sys.exit() itself on
        # every path; this is a defensive backstop only, in case a
        # future edit of that function forgets to on some path.
        sys.exit(0)

    # First instance -- keep the handle open for this process's
    # lifetime. See release_singleton() for explicit shutdown cleanup.
    _singleton_mutex_handle = handle


def release_singleton():
    """
    Explicit cleanup for the singleton mutex handle, intended for
    MAIN.py's normal shutdown path (e.g. an atexit hook). Not required
    for crash cleanup -- the OS releases/closes the handle automatically
    on process termination for any reason, including a crash (confirmed
    Win32 process-termination semantics) -- this function exists so a
    clean, intentional shutdown does not rely on that fallback alone.
    Safe to call even if acquire_singleton() was never called or this
    process was the duplicate (in which case it already exited before
    ever reaching any code that would call this).
    """
    global _singleton_mutex_handle
    if _singleton_mutex_handle:
        try:
            _kernel32.CloseHandle(_singleton_mutex_handle)
        except Exception:
            pass
        _singleton_mutex_handle = None


# ============================================================
# TASK A -- DIALOG GUARD MUTEX (duplicate-dialog suppression)
# ============================================================
def _check_dialog_guard_exists():
    """
    Existence-only check for DIALOG_GUARD_MUTEX_NAME: CreateMutexW, then
    ALWAYS CloseHandle immediately regardless of outcome -- open-check-
    close, per Instructions Section E's recommended pattern. Returns
    True if a dialog-holder is already active right now, False
    otherwise. This handle is never held across polls by a CHECKING
    process -- only the process that goes on to actually show the
    dialog keeps its own separate acquisition open, via
    _acquire_dialog_guard() below.
    """
    ctypes.set_last_error(0)
    handle = _kernel32.CreateMutexW(None, False, DIALOG_GUARD_MUTEX_NAME)
    already_exists = (ctypes.get_last_error() == ERROR_ALREADY_EXISTS)
    if handle:
        _kernel32.CloseHandle(handle)
    return already_exists


def _acquire_dialog_guard():
    """
    Creates/opens DIALOG_GUARD_MUTEX_NAME and returns the handle WITHOUT
    closing it -- "I am now the one showing the dialog", held for the
    dialog's entire lifetime. Returns None if the mutex already existed
    at this exact moment (a race against another duplicate-launch
    instance between _check_dialog_guard_exists() and this call -- see
    handle_duplicate_launch() for how the two together avoid a stale
    duplicate dialog) or if creation failed outright.
    """
    ctypes.set_last_error(0)
    handle = _kernel32.CreateMutexW(None, False, DIALOG_GUARD_MUTEX_NAME)
    last_error = ctypes.get_last_error()
    if not handle:
        return None
    if last_error == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return None
    return handle


def _release_dialog_guard(handle):
    if handle:
        try:
            _kernel32.CloseHandle(handle)
        except Exception:
            pass


# ============================================================
# TASK A -- CAMA HWND BOUNDED RETRY
# ============================================================
def _find_cama_hwnd_with_retry(timeout_ms=CAMA_HWND_RETRY_TIMEOUT_MS,
                                interval_ms=CAMA_HWND_RETRY_INTERVAL_MS):
    """
    Bounded poll for the first instance's "CAMA Tools" HWND. Addresses a
    real startup race (approved Phase 1 finding): the first instance
    can already own MUTEX_NAME -- acquired immediately after
    dispatch_tool_if_requested() returns -- before its own
    root = tk.Tk() / root.title("CAMA Tools") lines have run. A second
    launch arriving in that narrow window would see ERROR_ALREADY_EXISTS
    but find no CAMA HWND yet.

    Polls every interval_ms up to a total of timeout_ms; returns the
    HWND (int) as soon as found, or None if the timeout elapses first.
    Never guesses another window as a substitute.
    """
    elapsed = 0
    while elapsed <= timeout_ms:
        hwnd = _find_window(CAMA_WINDOW_TITLE)
        if hwnd:
            return hwnd
        time.sleep(interval_ms / 1000.0)
        elapsed += interval_ms
    return None


# ============================================================
# TASK B -- SESSION STATE FILE (cross-process GM identity bridge)
# ============================================================
def session_state_path(temp_dir):
    """Full path to the session-state file inside temp_dir (MAIN.py's
    own TEMP_DIR, passed in -- never duplicated as a constant here)."""
    return os.path.join(temp_dir, SESSION_STATE_FILENAME)


def write_session_state(temp_dir, first_instance_pid, locked_gm_hwnd, locked_gm_pid):
    """
    Called by the FIRST instance only, after wait_for_global_mapper()
    has already identified and locked a VERIFIED GM candidate (Task B)
    -- never before that point, and never with a guessed HWND.

    Writes {first_instance_pid, locked_gm_hwnd, locked_gm_pid} as JSON
    using a temp-file + os.replace() atomic swap, so a concurrently-
    launched duplicate-launch instance can never observe a partially-
    written file (os.replace() is atomic on Windows for files on the
    same volume, which this always is -- both paths are under the same
    temp_dir).
    """
    path = session_state_path(temp_dir)
    payload = {
        "first_instance_pid": first_instance_pid,
        "locked_gm_hwnd": locked_gm_hwnd,
        "locked_gm_pid": locked_gm_pid,
    }
    fd, tmp_path = tempfile.mkstemp(
        dir=temp_dir, prefix="cama_session_state_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


def cleanup_session_state(temp_dir):
    """
    Removes the session-state file on normal shutdown, when possible.
    Correctness of the duplicate-launch path must NOT depend on this
    ever running -- a crashed first instance leaving this file behind is
    expected and handled entirely by the validation chain in
    read_and_validate_session_state() (a stale recorded PID simply fails
    validation there), not by cleanup discipline here.
    """
    path = session_state_path(temp_dir)
    try:
        os.remove(path)
    except Exception:
        pass


def read_and_validate_session_state(temp_dir, expected_first_instance_pid):
    """
    Called by a duplicate-launch instance. Returns (locked_gm_hwnd,
    locked_gm_pid) ONLY if every one of the following holds RIGHT NOW --
    otherwise returns None, and the caller must fall back to CAMA-only
    handling. NEVER falls back to an arbitrary "Global Mapper Pro"
    window on validation failure.

        1. The file exists and parses as valid JSON with the expected
           keys.
        2. Its recorded first_instance_pid equals
           expected_first_instance_pid (the PID the caller independently
           derived from the live "CAMA Tools" HWND) -- guards against a
           stale file left by a since-crashed, unrelated earlier
           session.
        3. The recorded locked_gm_hwnd passes IsWindow() right now.
        4. GetWindowThreadProcessId() for that HWND, checked right now,
           still equals the recorded locked_gm_pid -- guards against
           Windows having recycled that HWND value onto an unrelated
           window since the original GM process exited.

    (Whether expected_first_instance_pid's PROCESS is still alive is
    checked separately by the caller, via OpenProcess/
    WaitForSingleObject, before this function is even called -- not
    duplicated here.)
    """
    path = session_state_path(temp_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        recorded_first_pid = data["first_instance_pid"]
        gm_hwnd = data["locked_gm_hwnd"]
        gm_pid = data["locked_gm_pid"]
    except Exception:
        return None

    if recorded_first_pid != expected_first_instance_pid:
        return None
    if not gm_hwnd or not _is_window(gm_hwnd):
        return None
    if _get_window_pid(gm_hwnd) != gm_pid:
        return None

    return gm_hwnd, gm_pid


# ============================================================
# TASK B -- GLOBAL MAPPER CANDIDATE IDENTIFICATION
# ============================================================
def snapshot_gm_hwnds():
    """
    Returns a set of HWNDs for every current TOP-LEVEL window whose
    title contains GM_WINDOW_TITLE_SUBSTRING, via a raw EnumWindows scan
    (this module does not import pygetwindow) -- deliberately NOT
    filtered by IsWindowVisible() here.

    Correctness reason: the pre-launch snapshot (existing_hwnds, taken
    by launch_global_mapper() BEFORE subprocess.Popen()) must capture
    EVERY existing matching window, including any that happen to be
    minimized or hidden at that exact moment -- otherwise a pre-existing
    GM window that later becomes visible AFTER launch would be absent
    from the snapshot and get misidentified as a newly-created window by
    identify_gm_candidate()'s snapshot-diff fallback, exactly the kind
    of false positive Task B exists to eliminate. Visibility/minimized/
    real-size READINESS checks belong to the caller
    (wait_for_global_mapper()), applied only to the specific identified
    candidate -- not mixed into this enumeration, which is used both for
    the pre-launch snapshot and for each poll's current candidate set.
    """
    found = set()
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetWindowTextW(hwnd, buf, 256)
        if GM_WINDOW_TITLE_SUBSTRING.lower() in buf.value.lower():
            found.add(hwnd)
        return True

    _user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found


def identify_gm_candidate(candidate_hwnds, launch_pid, existing_hwnds,
                           pid_lookup=_get_window_pid):
    """
    Task B's core decision function -- a PURE predicate over its
    arguments (no window enumeration of its own), so it can be exercised
    in a standalone logic simulation per Instructions Section G.4
    without any live Win32 calls: pass synthetic HWND ints for
    candidate_hwnds/existing_hwnds and a fake dict-backed pid_lookup
    callable.

    Args:
        candidate_hwnds: iterable of HWNDs currently matching the GM
            window title (real callers pass snapshot_gm_hwnds()'s
            current return value).
        launch_pid: the PID captured from subprocess.Popen(...).pid when
            Global Mapper was launched this session.
        existing_hwnds: the snapshot taken BEFORE that subprocess.Popen
            call (real callers pass an earlier snapshot_gm_hwnds()
            call's return value).
        pid_lookup: callable(hwnd) -> pid. Defaults to the real Win32
            GetWindowThreadProcessId wrapper; overridable for testing.

    Priority (approved design -- no third fallback beyond these two
    signals):
        1. PID match: any candidate whose owning PID == launch_pid.
           Authoritative -- returned immediately even if other
           candidates exist.
        2. No PID match, exactly ONE candidate not present in
           existing_hwnds: returned as the snapshot-diff fallback.
        3. No PID match, ZERO new candidates: returns None (GM may not
           have created its window yet -- caller keeps polling).
        4. No PID match, MULTIPLE new candidates: returns None --
           ambiguous. NEVER picks one arbitrarily; this is exactly the
           bug being fixed. Same "keep polling" outcome as case 3 from
           the caller's point of view.

    Returns:
        HWND (int) or None.
    """
    candidate_hwnds = list(candidate_hwnds)

    for hwnd in candidate_hwnds:
        if pid_lookup(hwnd) == launch_pid:
            return hwnd

    new_hwnds = [h for h in candidate_hwnds if h not in existing_hwnds]
    if len(new_hwnds) == 1:
        return new_hwnds[0]

    return None


# ============================================================
# TASK C -- TOPMOST RE-PIN SCOPE
# ============================================================
def should_repin_topmost(foreground_pid, locked_gm_pid):
    """
    Task C's entire fix, expressed as one pure predicate: the ongoing/
    throttled topmost re-pin (MAIN.py's monitor_gm_state() throttled
    branch, and launch_main_window()'s _force_z_order()) should fire
    ONLY when the CURRENT foreground window belongs to the locked Global
    Mapper process -- not the broader is_relevant_window_focused()
    check, which remains completely unchanged in MAIN.py and continues
    to gate the separate withdraw/show decision only.

    MAIN.py's existing get_foreground_pid() supplies foreground_pid;
    _locked_gm_pid[0] supplies locked_gm_pid -- no foreground-window
    lookup exists in this module. Pure equality with an explicit
    None-safety check: locked_gm_pid can legitimately be None before
    Global Mapper has been locked at all (e.g. very early during
    wait_for_global_mapper()'s own polling, before Task B's own logic
    above has identified anything).
    """
    if locked_gm_pid is None:
        return False
    return foreground_pid == locked_gm_pid


# ============================================================
# TASK A -- DUPLICATE-LAUNCH HANDLING
# ============================================================
def handle_duplicate_launch(temp_dir):
    """
    Entry point invoked by acquire_singleton() once CreateMutexW has
    confirmed ERROR_ALREADY_EXISTS for MUTEX_NAME -- i.e. this process
    is a duplicate launch. Never returns normally: every path through
    this function ends in sys.exit(0) (the silent-duplicate-dialog exit,
    the OK-click exit, or the stale-first-instance-detected exit).

    Flow:
        1. Dialog-guard existence check (open-check-close). If another
           duplicate-launch instance is already showing the dialog:
           exit silently, touching nothing.
        2. Acquire-and-hold the dialog guard for this instance's
           lifetime. Losing the race here (guard now held by someone
           else) is treated the same as step 1's outcome.
        3. Bounded retry for the "CAMA Tools" HWND
           (_find_cama_hwnd_with_retry()).
             - Not found within the timeout: show the dialog STANDALONE
               (genuinely Win32-topmost for basic visibility, no
               raise/disable of anything, and no liveness polling since
               FIRST_INSTANCE_PID was never obtained) -- exits on OK.
             - Found: derive FIRST_INSTANCE_PID, OpenProcess it, look up
               the GM window via the validated session-state file, raise
               GM then CAMA (their normal stacking order), record +
               disable both (only whichever were previously enabled),
               show the dialog topmost-pinned above both AND
               Alt+Tab-visible (see _set_topmost() call site -- an
               earlier revision also gave the dialog GWLP_HWNDPARENT
               ownership of cama_hwnd; removed, confirmed on-machine as
               the cause of the dialog being excluded from Alt+Tab),
               poll first-instance liveness roughly every
               FIRST_INSTANCE_POLL_INTERVAL_MS while showing.
        4. On EVERY exit path (OK click, stale-detected, or an
           exception anywhere above step 3's disable point), the
           finally block restores only what THIS invocation itself
           disabled, re-validating IsWindow() first, then releases the
           dialog guard.

    Note on the OpenProcess-failure race (CAMA HWND found, but its
    owning process has already exited by the time OpenProcess runs):
    this function still performs the normal raise/disable/dialog
    sequence but skips the auto-liveness poll (the dialog then only
    closes via user OK-click in that specific, narrow scenario) -- see
    module docstring KNOWN MACHINE-DEPENDENT BEHAVIOR item 3.
    """
    if _check_dialog_guard_exists():
        sys.exit(0)

    guard_handle = _acquire_dialog_guard()
    if guard_handle is None:
        sys.exit(0)

    cama_hwnd = None
    gm_hwnd = None
    process_handle = None
    cama_was_enabled = None
    gm_was_enabled = None
    dialog_root = None
    # DIAGNOSTIC (temporary -- see the except clause below): tracks
    # whether an unhandled exception was caught this invocation, purely
    # to pick the process exit code (1 instead of 0). Nothing reads this
    # process's exit code today, but a non-zero code is itself a small,
    # free diagnostic signal (e.g. via %errorlevel% from a batch script)
    # on top of the printed traceback and error dialog.
    _had_error = False

    try:
        cama_hwnd = _find_cama_hwnd_with_retry()

        if cama_hwnd is not None:
            first_instance_pid = _get_window_pid(cama_hwnd)

            ctypes.set_last_error(0)
            process_handle = _kernel32.OpenProcess(SYNCHRONIZE, False, first_instance_pid)
            # process_handle may legitimately be None here -- see the
            # OpenProcess-failure-race note in this function's docstring
            # and module docstring KNOWN MACHINE-DEPENDENT BEHAVIOR
            # item 3. Handled below by simply not scheduling the
            # liveness poll in that case, rather than looping on a bad
            # handle.

            gm_state = read_and_validate_session_state(temp_dir, first_instance_pid)
            if gm_state is not None:
                gm_hwnd, _gm_pid = gm_state

            # --- Raise GM then CAMA -- their normal stacking order ---
            if gm_hwnd is not None:
                gm_was_enabled = _is_window_enabled(gm_hwnd)
                if not _bring_to_foreground(gm_hwnd):
                    # Diagnostic only -- does not change control flow.
                    # SetForegroundWindow() being denied here means GM's
                    # z-order/activation may not have visually updated;
                    # _set_topmost() on the dialog below still applies
                    # regardless (see module docstring KNOWN
                    # MACHINE-DEPENDENT BEHAVIOR).
                    print("⚠ Could not bring Global Mapper to foreground (best-effort)")

            cama_was_enabled = _is_window_enabled(cama_hwnd)
            if not _bring_to_foreground(cama_hwnd):
                print("⚠ Could not bring CAMA Tools to foreground (best-effort)")

            # --- Disable only what was previously enabled ---
            if gm_hwnd is not None and gm_was_enabled:
                _enable_window(gm_hwnd, False)
            if cama_was_enabled:
                _enable_window(cama_hwnd, False)
        else:
            first_instance_pid = None

        # --- Build and show the "already running" dialog ---
        dialog_root = tk.Tk()
        dialog_root.withdraw()

        state = {"stale": False}

        def _on_ok():
            state["stale"] = False
            dialog_root.quit()

        top = tk.Toplevel(dialog_root)
        # Deliberately distinct from CAMA's own window title ("CAMA
        # Tools", set in MAIN.py). On-machine diagnostic logging (see
        # _try_set_foreground()'s [fg-diag] output) confirmed the
        # dialog's own HWND was genuinely the foreground window at every
        # checkpoint (0ms/10ms/50ms/100ms, and still foreground at the
        # start of the second 500ms pass) -- there was no foreground-
        # lock failure and no steal-back. The earlier "dialog doesn't
        # look foreground" observation was a visual identification
        # ambiguity: the first-instance CAMA main window and this dialog
        # were BOTH titled "CAMA Tools", so Alt+Tab showed two
        # indistinguishable entries. This rename does not change any
        # z-order, ownership, mutex, or foreground-activation logic --
        # it only makes the two windows distinguishable during testing
        # (and for the user, going forward).
        top.title("CAMA Tools - Already Running")
        top.resizable(False, False)
        # OK-only dialog -- clicking the window's own close button
        # behaves the same as clicking OK (same pattern as MAIN.py's
        # own do_nothing()/WM_DELETE_WINDOW override elsewhere, just
        # mapped to "confirm and close" instead of "do nothing", since
        # this dialog has no illegitimate way to be dismissed either
        # way).
        top.protocol("WM_DELETE_WINDOW", _on_ok)

        tk.Label(
            top, text="CAMA Tools is already running.",
            padx=24, pady=16
        ).pack()
        tk.Button(top, text="OK", width=10, command=_on_ok).pack(pady=(0, 16))

        top.update_idletasks()
        sw = top.winfo_screenwidth()
        sh = top.winfo_screenheight()
        w = top.winfo_reqwidth()
        h = top.winfo_reqheight()
        top.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")
        top.deiconify()
        top.lift()

        dialog_hwnd = _user32.GetParent(top.winfo_id())
        if dialog_hwnd:
            # Deliberately NOT calling _set_owner(dialog_hwnd, cama_hwnd)
            # here (removed -- was present in an earlier revision).
            # Confirmed root cause of an on-machine report that the
            # dialog was invisible in Alt+Tab: Windows' task-switch list
            # deliberately EXCLUDES owned windows (a window with
            # GWLP_HWNDPARENT set to another window is treated as a
            # dependent/transient popup of its owner, not a switchable
            # top-level window in its own right) -- this is documented
            # Win32 behavior, not implementation-specific. HWND_TOPMOST
            # (_set_topmost() below, synchronous) plus deferred
            # activation (_bring_to_foreground() below) are kept as the
            # z-order and focus mechanism instead -- the dialog is now a
            # fully independent top-level window, so it IS Alt+Tab
            # visible.
            _set_topmost(dialog_hwnd)

            # TIMING EXPERIMENT (single change: WHEN _bring_to_foreground
            # runs, not its own logic): on-machine testing showed
            # _bring_to_foreground(dialog_hwnd) reporting a successful
            # SetForegroundWindow() (both attempts returned True) while
            # called SYNCHRONOUSLY here, BEFORE dialog_root.mainloop()
            # starts processing this window's own message queue -- yet
            # the dialog was still not visually foreground and ended up
            # last in the Tab-cycle order. Whether this is specifically
            # because Tk's own message pump hadn't started yet is NOT
            # established Win32 behavior -- it is an unconfirmed
            # hypothesis, and SetForegroundWindow()'s interaction with
            # Tk's message processing and the native file-picker
            # process's own activation handling is more complex than
            # that alone. This experiment tests ONLY the timing: the
            # same _bring_to_foreground() call, unchanged, is deferred
            # via dialog_root.after() to run once the Tk event loop is
            # actually running (50ms pass), with a second pass at 500ms
            # in case foreground is lost again shortly after the first.
            # _set_topmost() above stays synchronous and unchanged --
            # only the ACTIVATION attempt is deferred, not the z-order
            # placement. If the dialog still fails to foreground with
            # this change, that is evidence AGAINST the timing
            # hypothesis, not confirmation of it either way -- see the
            # print() logging inside _bring_to_foreground() itself for
            # what each deferred attempt actually reports.
            def _deferred_bring_to_foreground():
                if not _bring_to_foreground(dialog_hwnd):
                    print("⚠ Could not activate the 'already running' dialog (best-effort)")

            dialog_root.after(50, _deferred_bring_to_foreground)
            dialog_root.after(500, _deferred_bring_to_foreground)

        # --- Liveness polling -- only if we actually have a handle ---
        if process_handle:
            def _poll_liveness():
                result = _kernel32.WaitForSingleObject(process_handle, 0)
                if result == WAIT_TIMEOUT:
                    dialog_root.after(FIRST_INSTANCE_POLL_INTERVAL_MS, _poll_liveness)
                else:
                    # WAIT_OBJECT_0 (first instance exited), WAIT_FAILED
                    # (broken/unusable handle), or any other unexpected
                    # result -- all treated identically: fail safe,
                    # assume stale, close the dialog and proceed to
                    # restore. See module docstring for why WAIT_FAILED
                    # is folded into "stale" rather than left unhandled.
                    state["stale"] = True
                    dialog_root.quit()

            dialog_root.after(FIRST_INSTANCE_POLL_INTERVAL_MS, _poll_liveness)

        dialog_root.mainloop()

    except Exception:
        # DIAGNOSTIC SAFETY NET (temporary, for on-machine debugging of
        # a currently-unexplained "no dialog appears" report -- see
        # Phase 1 discussion). Without this clause, any exception raised
        # above (between the try: and this point) would still run the
        # finally: block below, but then propagate UNHANDLED out of this
        # function -- killing the process with no visible sign if the
        # console window closes too fast to read the traceback. This
        # converts that into: the traceback is printed to stderr AND
        # captured as a string FIRST (so nothing below can lose it),
        # then shown in a visible error dialog on a best-effort basis.
        #
        # This is deliberately broad (bare `except Exception`) because
        # the goal right now is visibility into WHATEVER is failing, not
        # a targeted fix -- once a captured traceback identifies the
        # actual failing operation, that operation should be fixed
        # directly, and this except clause re-evaluated for whether it
        # remains as a permanent production safety net.
        _had_error = True
        _tb = traceback.format_exc()
        try:
            traceback.print_exc()
        except Exception:
            pass
        try:
            _show_duplicate_launch_error(_tb)
        except Exception:
            pass

    finally:
        # Restore only what THIS invocation itself disabled, and only
        # if the HWND is still valid right now (either window may have
        # closed while the dialog was up).
        if cama_hwnd is not None and cama_was_enabled and _is_window(cama_hwnd):
            _enable_window(cama_hwnd, True)
        if gm_hwnd is not None and gm_was_enabled and _is_window(gm_hwnd):
            _enable_window(gm_hwnd, True)

        if dialog_root is not None:
            try:
                dialog_root.destroy()
            except Exception:
                pass

        if process_handle:
            try:
                _kernel32.CloseHandle(process_handle)
            except Exception:
                pass

        _release_dialog_guard(guard_handle)

    sys.exit(1 if _had_error else 0)