"""
tools/batchProcessing/__init__.py

PURPOSE:
    Package entry point for tools.batchProcessing. Re-exports main()
    from window.py so this package satisfies the SAME dispatch
    contract every one of the 11 tool MODULES already does (a
    module-level main(parent, db_verified)) -- confirmed against
    MAIN.py's own dispatch_tool_if_requested() / run_tool_by_label(),
    both of which do importlib.import_module(mod_path) and then
    hasattr(mod, "main") / inspect.signature(mod.main) directly on
    whatever object that import returns. For a package (this
    directory), that returned object is THIS __init__.py's own module
    namespace -- so `main` must be bound here, not only inside
    window.py, for MAIN.py's existing, UNCHANGED dispatch code to find
    it. No change to MAIN.py's own TOOL_MODULES entry is needed for
    this: "tools.batchProcessing" resolves to this package exactly the
    way it would resolve to a plain tools/batchProcessing.py module.

    Deliberately kept to this one re-export -- all real logic lives in
    contract.py / state.py / pickers.py / checklist.py / window.py;
    nothing is defined here that isn't already defined elsewhere. See
    window.py's own module docstring for the full behavior spec.

DEPENDENCIES:
    tools.batchProcessing.window (main).

SIDE EFFECTS:
    None beyond the import itself (which transitively imports
    state.py, checklist.py, pickers.py, contract.py, and the three
    shared utils/ modules those files use -- no circular imports among
    them; contract.py has no local imports, state.py/checklist.py
    import only contract.py, pickers.py imports only shared utils/
    modules, and window.py imports state.py/checklist.py/pickers.py --
    a strict, one-directional dependency chain).
"""
from tools.batchProcessing.window import main

__all__ = ["main"]