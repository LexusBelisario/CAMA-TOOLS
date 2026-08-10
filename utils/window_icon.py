# utils/window_icon.py
"""
Shared window-icon application, extracted from 10 of the 11 registered
CAMA Tools tool modules (all except influence_to_map.py -- see below).

Confirmed via literal body diff (see
docs/refactor-log/group-icon-application-FINAL-analysis.md): 10 of 11
tools had byte-identical executable logic in their own local
apply_icon(win) function -- sets a .ico for the taskbar/titlebar via
iconbitmap(), then a .png as a fallback via iconphoto() (needed since
iconbitmap() alone doesn't reliably set the titlebar icon on every
Windows/Tk combination), holding a reference on the window
(window._icon_ref) to prevent the PhotoImage from being garbage-
collected while the window is still open. road_width.py had the same
logic but was MISSING that reference -- a real, minor bug, fixed here
for good by centralizing the logic in one place.

This module also supersedes an earlier, unused, orphaned attempt at the
same idea (tools/utils_icon.py's set_window_icon()) -- confirmed that
file was never imported or called by any tool, MAIN.py, or
MAIN_refactored.py. That file had its own version of the same missing-
reference bug, a hardcoded developer-machine-only absolute-path
fallback, and its own re-implementation of resource_path() (this module
uses the already-shared utils/resource_path.py instead). Deleted as part
of this same change -- see the refactor log for the approval trail.

influence_to_map.py is deliberately NOT migrated here -- it has no
dedicated icon file in icons/ico/ yet (unlike influence_to_barangay.py,
which does: influencemap.ico -- the two tools' similar names caused this
to be mixed up once during analysis, corrected before implementation).
influence_to_map.py keeps its own local, untouched apply_icon() showing
the generic BLGF icon until a dedicated icon exists for it.
"""
import os
import tkinter as tk

from utils.resource_path import resource_path


def apply_icon(window, icon_filename):
    """
    Sets a window's taskbar/titlebar icon using the tool-specific .ico
    file (icons/ico/{icon_filename}), falling back SILENTLY to the
    generic root-level BLGF.ico if the tool-specific file isn't found --
    no console output either way. A missing tool-specific icon is
    expected, normal behavior for a tool that doesn't have a dedicated
    one yet, not an error worth flagging on every launch.

    Also sets the PNG titlebar fallback (icons/BLGF.png, always the
    generic one -- no per-tool PNG exists for any tool) since
    iconbitmap() alone doesn't reliably set the titlebar icon on every
    Windows/Tk combination. Holds a reference to the PhotoImage on the
    window itself (window._icon_ref) to prevent it from being garbage-
    collected while the window is still open -- see this module's own
    docstring for the bug this fixes.
    """
    ico_path = resource_path(f"icons/ico/{icon_filename}")
    if not os.path.exists(ico_path):
        ico_path = resource_path("BLGF.ico")

    if os.path.exists(ico_path):
        try:
            window.iconbitmap(ico_path)
        except Exception:
            pass

    png_path = resource_path("BLGF.png")
    if os.path.exists(png_path):
        try:
            img = tk.PhotoImage(file=png_path)
            window.iconphoto(True, img)
            window._icon_ref = img
        except Exception:
            pass
