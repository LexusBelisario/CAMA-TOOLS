"""
utils/column_detection.py

PURPOSE:
    Shared existing-output-column detection, originally duplicated across
    9 of the 11 CAMA Tools tool modules (influence_to_map.py,
    land_shape_compactness.py, lot_location.py,
    poi_within_200_meters_for_parcellary_church_mall_police_park.py,
    road_density.py, road_frontage.py, road_surface.py, road_width.py,
    terrain.py) and consolidated here.

    A literal body diff across all 9 tools' implementations found:
    - 5 tools (multi-target) had a byte-identical standalone
      `_detect_existing_output_columns(gdf)`, differing only in each
      tool's own module-level `OUTPUT_COLUMN_TARGETS` constant.
    - 4 tools (single-target) had a structurally identical inline
      `next((c for c in gdf.columns if c.lower() == "<target>"), None)`
      expression -- the N=1 case of the exact same matching algorithm,
      differing only in the target column name literal.

    This module owns ONLY the generic matching algorithm. `targets`
    remains an explicit parameter, never hardcoded here -- each tool
    retains ownership of what it considers its own output column(s) via
    its own module-level constant (or, for single-target tools, a
    one-element tuple built at the call site). This module does not know
    or care how many targets a caller passes, or what they're named.

    IMPORTANT -- what this module deliberately does NOT include: none of
    the detect-on-select async infrastructure
    (`_set_parcel_reading_state()`, `_refresh_parcel_X_check()`,
    `_poll_parcel_X_queue()`, the disable/enable-while-reading logic, the
    in-place widget-reuse "Reading..." pattern, the `parcel_is_reading`
    Run-button gate, or any detection-result caching) lives here or is
    intended to move here. That infrastructure differs in real, necessary
    ways per tool (different widget variable names, different
    single-vs-multi-source shapes, different piggyback arrangements with
    other per-tool background reads) and stays local to each tool. This
    module is a narrow, single-purpose extraction of the matching logic
    only.

INPUTS:
    gdf: a GeoDataFrame (or any object exposing a `.columns` iterable of
    strings) to search.
    targets: an iterable of target column name strings to look for.

OUTPUTS:
    dict: {target: actual_existing_column_name}, containing only the
    targets that were actually found on `gdf` (case-insensitive exact
    match). A target with no match is simply absent from the result.

DEPENDENCIES:
    None (no imports; pure Python using only builtins).
"""


def detect_existing_output_columns(gdf, targets):
    """
    Checks `gdf` for any column matching one of `targets`, case-
    insensitive exact match (never substring/fuzzy).

    Returns {target: actual_existing_column_name} -- only for targets
    that were actually found; a target with no match is simply absent
    from the returned dict, never included with a None/empty value.

    The returned VALUE is the column's exact, original casing as found
    on `gdf` -- e.g. if `targets` includes "CAMA_DENS_ROAD" and the
    actual existing column is "cama_dens_road", the result is
    {"CAMA_DENS_ROAD": "cama_dens_road"}. This preserved casing is what
    gets shown in the overwrite-confirmation dialog and written back to
    -- the existing column's name is never renamed to match the
    target's own casing. The dict KEY's casing (i.e. exactly what was
    passed in `targets`) has no bearing on this -- it's purely an
    internal lookup label. Single-target callers should pass a
    one-element tuple/list and use `.get()` to pull out the single
    result, e.g.:
        found = detect_existing_output_columns(gdf, ("CAMA_DENS_ROAD",))
        existing_col = found.get("CAMA_DENS_ROAD")
    """
    found = {}
    for target in targets:
        match = next(
            (c for c in gdf.columns if c.lower() == target.lower()), None
        )
        if match is not None:
            found[target] = match
    return found