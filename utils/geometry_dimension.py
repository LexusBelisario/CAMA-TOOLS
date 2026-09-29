"""
utils/geometry_dimension.py

PURPOSE:
    Shared, single-purpose helper that forces every geometry in a
    GeoDataFrame to 2D, for the database-error-messages / Z-dimension /
    ud_id task's Z-dimension fix (Task 2A).

    System policy this module enforces (LOCKED, Instructions E.6): the
    application treats parcel geometry as planar cadastral boundaries.
    Elevation, when needed, is attribute data derived from terrain
    rasters (the Terrain tool samples a DTM raster into
    CAMA_PRCL_ELEV and other columns) -- never a Z coordinate of the
    geometry itself. Update Database and every tool-side database write
    therefore force geometry to 2D before it reaches PostGIS, since a
    mixed 2D/3D column (or a single 3D row landing in an otherwise-2D
    staging column) makes PostGIS reject the whole COPY with "Column
    has Z dimension but geometry does not."

    This module exposes exactly ONE public function,
    normalize_geometry_dimension(gdf) -- no policy parameter and no
    other knobs (Instructions G.5, Rule of Three: this is a new,
    single-purpose module, not a place to add configurability nobody
    has asked for). Every caller gets the same behavior; there is
    currently no scenario in this codebase where a layer should be
    ALLOWED to keep its Z coordinate on write, so there is nothing to
    make configurable.

    Callers (later sessions, not this one):
        - MAIN.py's update_database_from_geopackage(), once per layer,
          immediately after gdf = gdf.rename_geometry("geom") and
          before to_postgis(...) -- see that function's own Group 2
          change (Instructions E.6/Doc 4 Session 4).
        - Each of the 11 tool files' own _write_db_output_safely(), as
          the first statement of the STAGING_WRITE step, immediately
          before gdf.to_postgis(...) (Instructions E.6/Doc 4 Sessions
          5-15, pattern identical in all 11 files).

    Why the active geometry column is located BY NAME (gdf.geometry.name)
    rather than assumed to be the literal string "geometry": a verified
    pitfall in this codebase (Instructions E.6) is that after
    gdf.rename_geometry("geom"), assigning to gdf["geometry"] silently
    creates an unrelated new plain column and leaves the actual active
    geometry column (now named "geom") completely untouched -- the
    write would then still fail, silently defeating this whole fix.
    (road_frontage.py's own, pre-existing _force_2d() helper uses the
    hard-coded gdf["geometry"] form; Instructions E.8 says leave that
    function exactly as it is -- it is not touched by this task, and
    this new module deliberately does NOT repeat that pattern.)

INPUTS:
    normalize_geometry_dimension(gdf): gdf (geopandas.GeoDataFrame) --
    any GeoDataFrame, including one with zero rows, an active geometry
    column not named "geometry", None geometries, empty geometries
    (including an empty geometry that still carries the Z flag, e.g.
    "POLYGON Z EMPTY"), and a mix of 2D and 3D geometries in the same
    column.

OUTPUTS:
    normalize_geometry_dimension(gdf) -> tuple[GeoDataFrame, int]:
        gdf_2d -- a NEW GeoDataFrame (gdf.copy() -- a deep copy by
            default: every column's own underlying data is duplicated,
            not just the DataFrame's own row/column index structure;
            see gdf.copy()'s own pandas/geopandas documentation) with
            the active geometry column replaced by a 2D-only version.
            Every other column, the row index, and the GeoDataFrame's
            own crs are all preserved unchanged. A geometry that was
            already 2D is functionally unchanged (force_2d is a no-op
            on an already-2D geometry) but IS still a newly-built
            geometry object, since it passes through the same
            vectorized call as every other row -- callers must not
            rely on `is` identity between an input geometry and its
            corresponding output geometry, only on equality/WKB.
        n_rows_that_had_z -- int: how many rows of the INPUT gdf had a
            geometry with has_z True, counted BEFORE conversion. This
            counts every non-None geometry whose has_z is True,
            INCLUDING an empty geometry that still carries the Z flag
            (e.g. "POLYGON Z EMPTY" has has_z=True and is_empty=True
            simultaneously -- shapely tracks the Z flag and emptiness
            independently, and PostGIS still rejects an empty-but-3D
            geometry landing in a 2D-typed column exactly the same way
            it rejects a non-empty 3D one, so such a row is correctly
            counted as "had Z" and IS converted, never skipped). A None
            geometry is never counted (there is no Z to drop from a row
            with no geometry at all).

DEPENDENCIES:
    geopandas, shapely (third-party -- already project dependencies,
    Instructions D). Uses shapely.force_2d when the module attribute
    exists (shapely >= 2.0), detected ONCE at import time and cached in
    a module-level flag -- not re-checked per call or per row. When
    shapely.force_2d is not available, falls back to
    shapely.ops.transform(lambda x, y, z=None: (x, y), geom) applied
    per geometry for every NON-EMPTY geometry -- the same technique
    road_frontage.py's own _force_2d() already uses (Instructions E.6)
    -- with one deliberate difference: an EMPTY geometry is handled by
    reconstructing a fresh, empty geometry of the same shapely class
    instead of passing it through transform(), because transform() is
    a coordinate-mapper and an empty geometry has no coordinates to
    map, so it would otherwise leave an empty-but-3D geometry's has_z
    flag unchanged (see _force_2d_single()'s own docstring below for
    why this matters, and why road_frontage.py's own _force_2d() --
    untouched by this task, Instructions E.8 -- still has this gap).

SIDE EFFECTS:
    None. Pure function: does not mutate its input gdf (verified: the
    returned GeoDataFrame is an independent deep copy, and the geometry
    values it holds are freshly built objects, not references into the
    input's own geometry array), does not read or write any file, does
    not touch the database, does not print or log anything, does not
    show any dialog. No side effects occur at import time either --
    only the one-time shapely.force_2d availability check runs at
    import time, and that check has no observable effect beyond
    setting the module-level flag.
"""
import geopandas as gpd
import shapely
from shapely.ops import transform as _shapely_ops_transform

_HAS_FORCE_2D = hasattr(shapely, "force_2d")


def _force_2d_single(geom):
    """
    Per-geometry 2D fallback, used only when shapely.force_2d is not
    available on the installed shapely version (< 2.0). None passes
    through unchanged (nothing to transform).

    An EMPTY geometry needs its own branch: shapely.ops.transform maps
    a function over each of a geometry's own coordinates, and an empty
    geometry has zero coordinates -- so the mapping function never
    runs, and the returned geometry is STILL 3D (its is_empty stays
    True, but its has_z ALSO stays True, unchanged). This matters
    because a geometry can be both is_empty AND has_z at once (e.g.
    "POLYGON Z EMPTY" -- shapely tracks emptiness and the Z flag
    independently), and PostGIS rejects such a row exactly the same
    way it rejects a non-empty 3D one landing in a 2D-typed staging
    column, so it must still be converted, not treated as a no-op.
    The fix: construct a fresh, empty geometry of the SAME shapely
    class (type(geom)(), e.g. Polygon() for a Polygon, MultiPolygon()
    for a MultiPolygon) -- shapely's own empty-geometry constructors
    never carry a Z flag, regardless of what the original empty
    geometry's own flag was.

    A non-empty geometry, already-2D or not, is passed through
    shapely.ops.transform as before -- a safe no-op on an already-2D
    geometry (the lambda already accepts and returns a 2-tuple in that
    case), and the coordinate-dropping conversion for a 3D one.

    NOTE: this exact gap (transform() silently leaving an empty-but-3D
    geometry's has_z flag unchanged) also exists, unfixed, in
    road_frontage.py's own pre-existing _force_2d() helper -- that
    function is explicitly out of scope for this task (Instructions
    E.8) and is NOT modified here; this is reported as a pre-existing
    quirk (Instructions E.12), not corrected in place.
    """
    if geom is None:
        return None
    if geom.is_empty:
        return type(geom)()
    return _shapely_ops_transform(lambda x, y, z=None: (x, y), geom)


def normalize_geometry_dimension(gdf):
    """
    Forces every geometry in gdf's ACTIVE geometry column to 2D (drops
    any Z coordinate), returning a new GeoDataFrame plus a count of how
    many input rows had a Z coordinate. See this module's own PURPOSE/
    OUTPUTS docstring sections above for the full contract, the system
    policy this enforces, and why empty-but-3D geometries are still
    converted and still counted.

    Args:
        gdf (geopandas.GeoDataFrame): any GeoDataFrame, including a
            zero-row one, one whose active geometry column is not
            named "geometry", and one containing None and/or empty
            geometries.

    Returns:
        tuple[geopandas.GeoDataFrame, int]: (gdf_2d, n_rows_that_had_z)
        -- see this module's OUTPUTS docstring section above. gdf's own
        crs and its active geometry column's own name are both
        preserved unchanged on gdf_2d. The input gdf itself is never
        mutated.
    """
    active_col = gdf.geometry.name
    original_geoms = gdf[active_col]

    # has_z is computed on the ORIGINAL geometries, before any
    # conversion -- this is a count of the input, not the output.
    # shapely.has_z(array) handles None elements safely (reports False
    # for them, does not raise), so no None-filtering is needed here.
    had_z_mask = shapely.has_z(original_geoms.to_numpy())
    n_rows_that_had_z = int(had_z_mask.sum())

    if _HAS_FORCE_2D:
        # Vectorized path (shapely >= 2.0): shapely.force_2d(array)
        # also handles None elements safely (passes them through as
        # None) and converts EVERY geometry, empty or not, 2D-already
        # or not -- there is no reason to pre-filter to only the rows
        # had_z_mask flagged, since force_2d is already a safe no-op on
        # a geometry that has no Z to drop.
        new_geoms_array = shapely.force_2d(original_geoms.to_numpy())
    else:
        # Fallback path (shapely < 2.0): per-geometry transform, same
        # technique as road_frontage.py's own _force_2d() -- see
        # _force_2d_single()'s own docstring above.
        new_geoms_array = original_geoms.apply(_force_2d_single).to_numpy()

    new_geoms = gpd.GeoSeries(new_geoms_array, index=gdf.index, crs=gdf.crs)

    # gdf.copy() is a DEEP copy by default (geopandas.GeoDataFrame.copy()
    # follows pandas.DataFrame.copy()'s own default deep=True) -- every
    # column's own underlying data is duplicated, not just the
    # DataFrame's row/column index structure, so mutating gdf_2d after
    # this point (including reassigning its geometry column, immediately
    # below) can never reach back into the caller's original gdf.
    gdf_2d = gdf.copy()
    gdf_2d[active_col] = new_geoms

    return gdf_2d, n_rows_that_had_z
