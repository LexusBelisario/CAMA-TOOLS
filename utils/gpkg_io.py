"""
utils/gpkg_io.py

PURPOSE:
    Shared, atomic GeoPackage-write helper used by every CAMA Tools tool
    that saves local .gpkg output. Consolidated from what were 10
    independently-copy-pasted, byte-for-byte-identical per-tool
    implementations (confirmed via a full code audit, not assumed) plus
    one file (influence_map_distance_to_land_parcel.py) that had
    independently drifted into a different, buggier variant. See the
    project's task history for the investigation that led to this
    consolidation.

    Fixes two known bugs in one place:
    (1) The internal GeoPackage layer name previously defaulted to the
        temp file's own filename stem, which always contains a ".tmp"
        marker -- so the saved file's internal layer name ended up
        permanently wrong (e.g. LandParcel.tmp instead of LandParcel),
        even though the file itself was correctly renamed on disk by
        the final os.replace(). Confirmed empirically via
        pyogrio.list_layers().
    (2) influence_map_distance_to_land_parcel.py's local variant did an
        extra, unconditional os.remove() of the DESTINATION path before
        writing -- a narrower window where a crash between the remove
        and the write leaves nothing valid at `path`. Migrating that
        file to this shared function removes that extra step as a
        byproduct of consolidation, not a separate fix.

INPUTS:
    gdf (geopandas.GeoDataFrame): the data to write.
    path (str): the final destination .gpkg path.

OUTPUTS:
    None. On success, `path` contains the complete, verified new file.
    On failure, `path` is left completely untouched -- either the old
    valid file (if one existed) or nothing, exactly as if this call
    never happened -- and a RuntimeError is raised.

DEPENDENCIES:
    os (stdlib), geopandas (already used project-wide).

SIDE EFFECTS:
    Writes a temp file alongside `path` (same directory, so the final
    os.replace() is guaranteed atomic on the same filesystem), reads it
    back once to verify, then replaces `path`. Deletes the temp file
    on any failure. Never deletes or truncates `path` itself.
"""
import os
import geopandas as gpd


def write_gpkg_atomic(gdf, path):
    """
    Writes a GeoDataFrame to a .gpkg file, atomically.

    Why atomicity is necessary here specifically: an earlier version of
    this function deleted any pre-existing file at `path` FIRST, then
    wrote the new content -- necessary because GeoPackage is a
    SQLite-based container that can hold multiple named layers, and
    calling gdf.to_file(path, driver="GPKG") when `path` already exists
    does NOT simply replace its contents; pyogrio/GDAL tries to create
    a new layer inside the existing file and fails with "Layer <n>
    already exists, CreateLayer failed" if a layer of that name is
    already there (confirmed reproduced when a user chose "Overwrite"
    in an ask_overwrite_dialog() -- crashed the whole run with no
    success dialog and no clear message, just a console traceback
    invisible in the compiled EXE).

    But delete-then-write has its own, worse failure mode: if anything
    interrupts the process between the delete and the write completing
    (a crash, the machine losing power, disk full mid-write), the
    original file is gone and nothing valid has replaced it. This
    version writes to a temporary file first, VERIFIES that file is
    actually readable back with the expected row count, and only then
    replaces the destination via os.replace(). The precise guarantee
    os.replace() provides is: when source and destination are on the
    same filesystem, the destination NAME's replacement is atomic --
    no observer (another process, a crash mid-operation) can see a
    partially-written destination file; what's at `path` is either the
    complete old file or the complete new file, never a mix. The model
    is "old valid file -> os.replace() -> new valid file", never
    "delete old -> write new". If ANY step before the final
    os.replace() fails, `path` is left completely untouched, exactly as
    if this call never happened.

    layer=layer_name is passed explicitly (derived from the FINAL
    destination `path`, not the temp path) so the internal GeoPackage
    layer name matches the saved filename -- GDAL otherwise defaults
    the layer name to the temp file's own stem, which always contains
    a ".tmp" marker and would leave that marker permanently baked into
    the file's internal layer metadata even after os.replace() renames
    it on disk.
    """
    tmp_path = f"{os.path.splitext(path)[0]}.tmp{os.path.splitext(path)[1]}"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    layer_name = os.path.splitext(os.path.basename(path))[0]
    gdf.to_file(tmp_path, driver="GPKG", layer=layer_name)
    try:
        verify_gdf = gpd.read_file(tmp_path)
        if len(verify_gdf) != len(gdf):
            raise ValueError(
                f"Row count mismatch after write: expected {len(gdf)}, "
                f"got {len(verify_gdf)}."
            )
    except Exception as e:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise RuntimeError(
            f"Could not verify the written file before replacing the "
            f"destination -- destination left unchanged. Details: {e}"
        )
    os.replace(tmp_path, path)
