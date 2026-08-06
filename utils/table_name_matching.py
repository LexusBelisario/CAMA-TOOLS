# utils/table_name_matching.py
"""
Shared table-name-matching helpers, extracted from all 11 registered
CAMA Tools tool modules (POI_All_Distance.py, influence_to_barangay.py,
influence_to_map.py, land_shape_compactness.py, lot_location.py,
poi_within_200_meters_for_parcellary_church_mall_police_park.py,
road_density.py, road_frontage.py, road_surface.py, road_width.py,
terrain.py).

Confirmed Category A (see Shared Utilities Refactor, function group:
normalize_name() + find_matching_tables()): a line-by-line body diff
across all 11 source copies found zero executable-code differences --
only docstring wording and single/double-quote style varied. This
module is the single canonical implementation; the 11 tool files now
import from here instead of each carrying their own copy.

Both functions are used together to resolve the DB-output overwrite
target inside each tool's own resolve_db_output_table(): a local-file
Land Parcel source's filename is fuzzy-matched against existing DB
tables so the user can be asked to confirm (or reject) an overwrite,
rather than the tool guessing silently.
"""
import re


def normalize_name(name: str) -> str:
    """
    Lowercases name and strips everything that isn't a letter (digits,
    underscores, spaces, hyphens, etc.) -- e.g. "LandParcel_2026" and
    "Land Parcel Final" both normalize to "landparcelfinal"-style
    strings with no separators left, so filenames and table names that
    differ only by punctuation/casing/trailing numbers can still be
    recognized as referring to the same logical table.
    """
    return re.sub(r'[^a-z]', '', name.lower())


def find_matching_tables(desired_name, all_tables):
    """
    Returns the list of candidate table names from all_tables whose
    normalized form is a substring of (or contains) the normalized
    desired_name -- checked in both directions, so "landparcel" matches
    "landparcel_final" and "landparcel_2026" matches "landparcel"
    equally. This is intentionally permissive (fuzzy) matching: the
    caller is responsible for confirming the match with the user
    before treating it as a definite overwrite target (see
    confirm_db_overwrite_dialog() / choose_db_overwrite_dialog() and
    resolve_db_output_table() in each tool) -- this function only
    proposes candidates, it never decides on its own.

    Always excludes "CAMA_Table", "CAMA_Transaction_Log", and any table
    ending in "_VM" (case-insensitive) from the candidate list, since
    none of these are ever valid "main output" overwrite targets even
    if their name happens to contain a substring match (e.g. a
    Visual Measurement layer table like "landparcel_VM" would otherwise
    also match a "landparcel" search).
    """
    lname = normalize_name(desired_name)
    candidates = []
    for t in all_tables:
        if t.lower() in ("cama_table", "cama_transaction_log"):
            continue
        if t.lower().endswith("_vm"):
            continue
        tnorm = normalize_name(t)
        if lname in tnorm or tnorm in lname:
            candidates.append(t)
    return candidates
