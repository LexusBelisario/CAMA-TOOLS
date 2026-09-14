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
    before_swap (callable, optional): zero-argument callback run after
        the temp file is verified but before the destination is
        replaced. Optional and defaults to None, so the callers that
        don't pass it are entirely unaffected. See the function's own
        docstring.

OUTPUTS:
    None. On success, `path` contains the complete, verified new file.
    On failure, `path` is left completely untouched -- either the old
    valid file (if one existed) or nothing, exactly as if this call
    never happened -- and an exception is raised.

    Swap-stage failures raise GpkgWriteError (see the class below),
    which subclasses RuntimeError so existing `except RuntimeError:`
    callers are unaffected. Its messages are written for a
    NON-TECHNICAL reader and are meant to be shown AS-IS: no Windows
    error codes, no full file paths, and no naming of any specific
    application. The confirmed real-world failure -- the destination
    file being open elsewhere -- names the FILE the user has to close,
    since that is the one actionable detail. Any other failure gets a
    separate, more general message rather than claiming a lock that may
    not be the actual cause.

    The earlier write/verify stage still raises a plain RuntimeError:
    its message reports an internal consistency failure rather than
    something the user can act on, so it is not in the
    show-this-verbatim category.

DEPENDENCIES:
    os (stdlib), geopandas (already used project-wide).

SIDE EFFECTS:
    Writes a temp file alongside `path` (same directory, so the final
    os.replace() is guaranteed atomic on the same filesystem), reads it
    back once to verify, then replaces `path`. Deletes the temp file
    on any failure -- including a failure of the replace itself, which
    was previously unguarded and left an orphaned temp file on disk
    whenever the destination could not be replaced. Never deletes or
    truncates `path` itself.
"""
import os
import geopandas as gpd


class GpkgWriteError(RuntimeError):
    """
    Raised for this file's own hand-crafted, already user-facing
    messages.

    Callers that translate exceptions for display (e.g.
    _translate_exception() in several tools) should show these AS-IS,
    via str(e), WITHOUT prepending the exception class name. That
    differs from how a genuinely unexpected exception should be shown,
    where the class name still helps troubleshooting -- but here the
    message was written for the end user in the first place, and
    "GpkgWriteError: Could not overwrite LandParcel.gpkg..." reads as a
    crash rather than as the plain instruction it is meant to be.

    Deliberately subclasses RuntimeError rather than Exception: every
    error this function raised before this class existed was a
    RuntimeError, so any caller with an existing `except RuntimeError:`
    keeps catching these unchanged. Only callers that specifically want
    the cleaner display need to know this type exists.
    """
    pass


def write_gpkg_atomic(gdf, path, before_swap=None):
    """
    Writes a GeoDataFrame to a .gpkg file, atomically.

    before_swap (callable, optional): a zero-argument callback invoked
    AFTER the temp file has been written and verified, but BEFORE
    os.replace() touches the real destination. That point is deliberate:
    it is the last moment at which abandoning the write is free, because
    `path` is still completely untouched and the only thing that exists
    is a temp file this function will clean up itself. It is therefore
    the right place for a caller to make a final "should I still do
    this?" decision -- a cancel check being the motivating case.

    If before_swap raises, the temp file is removed and the SAME
    exception object is re-raised unmodified -- not wrapped, not
    replaced, and NOT turned into GpkgWriteError -- so a caller's own
    `except SomeSpecificType:` upstream still matches it. This is the
    one error path in this function that does not become a
    GpkgWriteError, precisely because the caller chose that exception
    type on purpose.

    Default None means no callback and no behavior change at all: the
    10 of 11 calling tools that never pass it reach os.replace()
    exactly as they always have.

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
    # The verified temp file is now complete and `path` has still not been
    # touched, so this is the last moment where abandoning the write costs
    # nothing. A caller that wants to bail out here (e.g. on a cancel
    # signal) opts in by passing before_swap; the 10 callers that don't
    # pass it reach os.replace() exactly as before.
    if before_swap is not None:
        try:
            before_swap()
        except Exception:
            # before_swap raised -- the temp file is now useless, so
            # remove it, then re-raise the SAME exception object,
            # unmodified. Deliberately NOT wrapped in RuntimeError the
            # way the swap failures below are: a caller that opted in is
            # raising its own exception type on purpose and will be
            # catching that exact type upstream (e.g.
            # `except _RunCancelled:`), which a wrapped or replaced
            # exception would no longer match.
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            raise
    try:
        os.replace(tmp_path, path)
    except PermissionError:
        # Confirmed real-world case: the destination .gpkg is open in
        # another application, so Windows refuses the replace. The raw
        # exception text that used to surface here was unusable for a
        # non-technical reader (`PermissionError: [WinError 5] Access is
        # denied: ...`, complete with full file paths). The replacement
        # names the actual FILE the user needs to close -- which is the
        # one piece of information they can act on -- but no Windows
        # error code, no full path, and no specific application.
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise GpkgWriteError(
            f"Could not overwrite {os.path.basename(path)}. "
            f"Please make sure {os.path.basename(path)} is not open in "
            "another app, then try again."
        )
    except Exception as e:
        # Anything else (disk full, invalid path, filesystem error...)
        # deliberately gets a DIFFERENT, more generic message. Reusing
        # the "open in another app" wording here would state a cause
        # that may well be wrong and send the user chasing a lock that
        # does not exist.
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise GpkgWriteError(
            f"Could not save the file. Details: {e}"
        ) from e