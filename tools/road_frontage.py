import os
import re
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox, ttk, StringVar
import geopandas as gpd
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import unary_union, nearest_points, linemerge
import math
import subprocess
import json
from sqlalchemy import create_engine, inspect, text
from shapely.validation import make_valid
from shapely.geometry import box
import psycopg2

# =========================
# GeoPandas compatibility shim
# =========================
if not hasattr(gpd.GeoSeries, "from_bbox"):
    from shapely.geometry import box

    @staticmethod
    def _from_bbox(b):
        return gpd.GeoSeries([box(*b)])

    gpd.GeoSeries.from_bbox = _from_bbox

# ============================
# FORCE WINDOWS APP ICON
# ============================
import ctypes
import sys

def set_app_user_model_id():
    appid = u"BLGF.CAMA.Tools.2025"
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(appid)

set_app_user_model_id()


def resource_path(relative_path):
    """ PyInstaller-safe resource path """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def apply_icon(win):
    ico = resource_path("BLGF.ico")
    png = resource_path("BLGF.png")

    # Windows taskbar / Alt-Tab icon
    if os.path.exists(ico):
        try:
            win.iconbitmap(ico)
        except Exception:
            pass

    # Titlebar icon fallback (critical for Tk)
    if os.path.exists(png):
        try:
            img = tk.PhotoImage(file=png)
            win.iconphoto(True, img)
            win._icon_ref = img  # prevent garbage collection
        except Exception:
            pass


# ========================= CONFIG =========================
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"
CREDENTIALS_FILE = "pg_credentials.json"

barangay_source = None
road_source = None
output_mode = None
_app_root = None


# ========================= CRS UTILITY =========================
# PRS92 zones are non-overlapping 2-degree longitude bands (EPSG registry):
#   Zone I   (3121): west of 118°E
#   Zone II  (3122): 118°E – 120°E  (Palawan, Calamian Islands)
#   Zone III (3123): 120°E – 122°E  (Luzon west of 122°E, Mindoro)
#   Zone IV  (3124): 122°E – 124°E  (SE Luzon, Panay, Cebu, Negros, west Mindanao)
#   Zone V   (3125): east of 124°E  (east Mindanao, east Visayas)
PRS92_ZONE_BOUNDS = [
    (-180.0, 118.0, 3121, "Zone I"),
    (118.0,  120.0, 3122, "Zone II"),
    (120.0,  122.0, 3123, "Zone III"),
    (122.0,  124.0, 3124, "Zone IV"),
    (124.0,  180.0, 3125, "Zone V"),
]


def detect_prs92_zone(gdf):
    """
    Auto-detect the correct PRS92 zone EPSG code from the bounding-box
    midpoint longitude of the input GeoDataFrame.

    If the layer has no CRS defined, WGS84 (EPSG:4326) is assumed and a
    warning string is returned alongside the zone so the caller can
    surface it to the operator — processing continues rather than
    aborting, but the resulting measurements may be wrong if the actual
    source CRS was something other than WGS84.

    Uses total_bounds (min/max coordinates) rather than a unioned-geometry
    centroid. A union across an entire large parcel layer is a known
    source of GEOS TopologyExceptions on real-world cadastral data —
    confirmed by reproducing the exact failure this tool hit in
    production. total_bounds is pure min/max arithmetic and carries no
    such risk, at the cost of being slightly less representative of the
    dataset's true center for a layer with very unevenly distributed
    parcels near a zone boundary — a much smaller and purely theoretical
    concern next to a confirmed production crash.

    Returns (epsg, warning) where warning is None when no CRS issue was
    found, or a string describing the issue otherwise.
    """
    warning = None
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326)
        warning = (
            "No CRS found in the dataset -- assuming WGS84. "
            "Measurements may be incorrect if the actual CRS is different."
        )

    gdf_wgs84 = gdf.to_crs(epsg=4326) if gdf.crs.to_epsg() != 4326 else gdf
    minx, miny, maxx, maxy = gdf_wgs84.total_bounds
    center_lon = (minx + maxx) / 2

    for lon_min, lon_max, epsg, zone_label in PRS92_ZONE_BOUNDS:
        if lon_min <= center_lon < lon_max:
            if not (lon_min <= minx and maxx < lon_max):
                print(
                    f"⚠️ Dataset longitude range ({minx:.4f}° to {maxx:.4f}°E) "
                    f"extends outside the detected {zone_label} bounds "
                    f"({lon_min}°E–{lon_max}°E). Features near the dataset edge "
                    f"may be very slightly less accurate."
                )
            print(f"ℹ️ Auto-detected PRS92 {zone_label} (EPSG:{epsg}) "
                  f"from data bbox-midpoint longitude {center_lon:.4f}°E")
            return epsg, warning

    raise ValueError(f"Could not determine PRS92 zone for longitude {center_lon}")


def fix_geometry(geom):
    if geom is None or geom.is_empty:
        return None
    try:
        if not geom.is_valid:
            geom = geom.buffer(0)
        if not geom.is_valid:
            geom = make_valid(geom)
        if geom.is_empty:
            return None
        return geom
    except:
        return None

import threading
import queue
from shapely.prepared import prep

def _longest_linestring(geom):
    """Return the longest LineString inside LineString/MultiLineString/GeometryCollection."""
    if geom is None or geom.is_empty:
        return None
    gt = geom.geom_type
    if gt == "LineString":
        return geom
    if gt == "MultiLineString":
        return max(geom.geoms, key=lambda g: g.length, default=None)
    if gt == "GeometryCollection":
        lines = [g for g in geom.geoms if g.geom_type in ("LineString", "MultiLineString")]
        best = None
        best_len = 0.0
        for g in lines:
            ls = _longest_linestring(g)
            if ls and ls.length > best_len:
                best = ls
                best_len = ls.length
        return best
    return None


def _all_linestrings(geom):
    """Return EVERY LineString piece inside LineString/MultiLineString/
    GeometryCollection as a flat list — used to capture all road-adjacent
    boundary segments (e.g. both sides of a corner lot) for the QA layer,
    matching the arc-sum ROAD_FRONT total rather than just the longest piece."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type == "MultiLineString":
        return list(geom.geoms)
    if geom.geom_type == "GeometryCollection":
        out = []
        for g in geom.geoms:
            out.extend(_all_linestrings(g))
        return out
    return []


def open_in_global_mapper(output_path):
    if os.path.exists(GM_EXE_PATH) and os.path.exists(output_path):
        subprocess.Popen([GM_EXE_PATH, output_path], shell=True)


def split_boundary_to_segments(boundary):
    segments = []
    if boundary.geom_type == 'LineString':
        coords = list(boundary.coords)
        segments.extend([LineString([coords[i], coords[i + 1]]) for i in range(len(coords) - 1)])
    elif boundary.geom_type == 'MultiLineString':
        for line in boundary.geoms:
            coords = list(line.coords)
            segments.extend([LineString([coords[i], coords[i + 1]]) for i in range(len(coords) - 1)])
    return segments


def _edge_covered_portion(seg, road_union, tol=10):
    """For one elementary boundary segment (vertex-to-vertex), find which
    portion of it — if any — is genuinely road-adjacent, using a buffer
    confined to this segment's OWN footprint (flat-capped at cap_style=2,
    never extended past the segment's own two endpoints).

    This replaces buffering the road network as a whole, which is
    isotropic and was confirmed (via reproducible test) to "bleed" onto a
    perpendicular boundary segment near a corner even when no road
    actually runs alongside that segment. Confining the buffer to each
    segment's own footprint eliminates that spillover by construction —
    a corner's two meeting segments each only "see" road within their own
    rectangle, with a small uncovered gap exactly at the corner itself.

    Returns a LineString spanning only the covered sub-portion of `seg`
    (which may be the full segment, a partial/truncated portion matching
    where the road genuinely reaches, or None if no part of it is within
    `tol` of any road geometry).
    """
    zone = seg.buffer(tol, cap_style=2)
    road_in_zone = road_union.intersection(zone)
    if road_in_zone.is_empty:
        return None

    gt = road_in_zone.geom_type
    if gt == "LineString":
        pts = list(road_in_zone.coords)
    elif gt == "MultiLineString":
        pts = [c for g in road_in_zone.geoms for c in g.coords]
    elif gt == "Point":
        pts = [(road_in_zone.x, road_in_zone.y)]
    elif gt == "MultiPoint":
        pts = [(p.x, p.y) for p in road_in_zone.geoms]
    else:
        return None

    fracs = [seg.project(Point(p)) for p in pts]
    lo, hi = min(fracs), max(fracs)
    if hi - lo < 1e-9:
        return None
    return LineString([seg.interpolate(lo), seg.interpolate(hi)])


def calculate_centroid_to_road_depth(parcel_geom, road_gdf):
    centroid = parcel_geom.centroid
    min_distance = float("inf")
    for road in road_gdf.geometry:
        p1, p2 = nearest_points(centroid, road)
        dist = p1.distance(p2)
        if dist < min_distance:
            min_distance = dist
    return min_distance


def calculate_depth_perpendicular(parcel_geom, road_buffer, max_depth=1000):
    boundary = parcel_geom.boundary
    segments = split_boundary_to_segments(boundary)
    frontage_segments = [seg for seg in segments if seg.within(road_buffer)]
    if not frontage_segments:
        return 0

    frontage_seg = max(frontage_segments, key=lambda s: s.length)
    midpoint = frontage_seg.interpolate(0.5, normalized=True)

    x1, y1 = frontage_seg.coords[0]
    x2, y2 = frontage_seg.coords[1]
    dx = x2 - x1
    dy = y2 - y1

    perp_dx, perp_dy = -dy, dx
    length = math.hypot(perp_dx, perp_dy)
    if length == 0:
        return 0
    perp_dx /= length
    perp_dy /= length

    line1 = LineString([midpoint, (midpoint.x + perp_dx * max_depth, midpoint.y + perp_dy * max_depth)])
    line2 = LineString([midpoint, (midpoint.x - perp_dx * max_depth, midpoint.y - perp_dy * max_depth)])

    depth_line = max([line1, line2], key=lambda l: l.intersection(parcel_geom).length)
    intersection = depth_line.intersection(parcel_geom)
    if intersection.is_empty:
        return 0
    elif intersection.geom_type == 'MultiLineString':
        return max(part.length for part in intersection.geoms)
    elif intersection.geom_type == 'LineString':
        return intersection.length
    else:
        return 0


# ========================= DB HELPERS =========================
def load_db_credentials():
    try:
        with open(CREDENTIALS_FILE, "r") as f:
            return json.load(f)
    except:
        return None


def get_geometry_column(table_name, engine, schema):
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT f_geometry_column
                FROM geometry_columns
                WHERE f_table_schema = :schema AND f_table_name = :table
            """), {"schema": schema, "table": table_name}).fetchone()
            return result[0] if result else None
    except:
        return None


def read_postgis_clean(table, engine, schema):
    geom_col = get_geometry_column(table, engine, schema)
    insp = inspect(engine)
    cols = [c['name'] for c in insp.get_columns(table, schema=schema) if c['name'] != geom_col]
    col_str = ", ".join([f'"{c}"' for c in cols]) if cols else ""
    if col_str:
        query = f'SELECT {col_str}, "{geom_col}" AS geometry FROM "{schema}"."{table}"'
    else:
        query = f'SELECT "{geom_col}" AS geometry FROM "{schema}"."{table}"'
    return gpd.read_postgis(query, engine, geom_col="geometry")


def normalize_name(name: str) -> str:
    return re.sub(r'[^a-z]', '', name.lower())


def fetch_tables(schema):
    creds = load_db_credentials()
    if not creds:
        return []
    try:
        conn = psycopg2.connect(
            host=creds["host"],
            port=creds["port"],
            dbname=creds["database"],
            user=creds["username"],
            password=creds["password"]
        )
        cur = conn.cursor()
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s ORDER BY table_name;", (schema,))
        tables = [row[0] for row in cur.fetchall()]
        conn.close()
        return tables
    except:
        return []


def find_matching_table(local_name, schema):
    all_tables = fetch_tables(schema)
    lname = normalize_name(local_name)
    for t in all_tables:
        tnorm = normalize_name(t)
        if lname in tnorm or tnorm in lname:
            return t
    return None


# ========================= PROGRESS WINDOW =========================
class ProgressWindow:
    def __init__(self, root, title="Processing"):
        self.win = tk.Toplevel(root)
        apply_icon(self.win)
        self.win.title(title)
        self.win.minsize(400, 120)
        self.win.resizable(False, False)
        self.status_var = StringVar(master=self.win)
        self.status_var.set("Starting...")
        ttk.Label(
            self.win, textvariable=self.status_var, anchor="center",
            justify="left", wraplength=380,
        ).pack(pady=10, padx=10, fill="x")
        self.progress = ttk.Progressbar(self.win, orient="horizontal", mode="determinate", length=350)
        self.progress.pack(pady=10)
        self.win.attributes("-topmost", True)
        self.win.update()

        self.win.focus_force()
        self.win.lift()
        self.win.attributes("-topmost", True)
        self.win.after(100, lambda: self.win.attributes("-topmost", False))


    def update(self, message, value=None, maximum=None):
        self.status_var.set(message)
        if maximum is not None:
            self.progress["maximum"] = maximum
        if value is not None:
            self.progress["value"] = value
        self.win.update_idletasks()
        self.win.geometry("")
        self.win.update()

    def close(self):
        self.win.destroy()


def clip_roads_to_parcels(road_gdf, parcel_gdf, pad=50):
    """
    Clip roads to parcel extent (+ buffer) to massively speed up union/buffer.
    pad is in CRS units (meters after reprojection).
    """
    minx, miny, maxx, maxy = parcel_gdf.total_bounds

    clip_box = box(
        minx - pad,
        miny - pad,
        maxx + pad,
        maxy + pad
    )

    return road_gdf[road_gdf.geometry.intersects(clip_box)]



# ========================= FRONTAGE PROCESSING =========================
def process_frontage_single(brgy_gdf, road_gdf, source_name="", progress=None):
    original_crs = brgy_gdf.crs
    zone_epsg, crs_warning = detect_prs92_zone(brgy_gdf)

    if crs_warning and progress:
        progress(f"Warning: {source_name}: {crs_warning}")

    if progress:
        progress(f"Reprojecting {source_name} to EPSG:{zone_epsg}")

    brgy_gdf = brgy_gdf.to_crs(epsg=zone_epsg)
    road_gdf = road_gdf.to_crs(epsg=zone_epsg)

    if progress:
        progress("Preparing roads (union)")

    # 🔧 Clean road geometries
    road_gdf = road_gdf.copy()
    road_gdf["geometry"] = road_gdf.geometry.apply(fix_geometry)
    road_gdf = road_gdf[road_gdf.geometry.notnull()]
    road_gdf = road_gdf[~road_gdf.geometry.is_empty]

    # ✂️ CLIP ROADS TO PARCEL EXTENT (CRITICAL)
    road_gdf = clip_roads_to_parcels(road_gdf, brgy_gdf, pad=50)

    if road_gdf.empty:
        raise RuntimeError("No road geometry near parcels after clipping")

    road_union = unary_union(road_gdf.geometry.values)

    frontage_lengths, depths, dwrs = [], [], []
    # Accumulates (frontage_line, depth_line, frontage_total, depth_val) per parcel
    # for QA output. None entries mean no road frontage for that parcel.
    frontage_lines_data = []
    total = len(brgy_gdf)

    # Resolve parcel identifier column for QA output FEATURE_ID.
    # Priority: PIN → pin → Pin → ARP_NO → TD_NO → PARCEL_ID → row index.
    # Falls back to row index if none found — never crashes on missing column.
    _PIN_CANDIDATES = ["PIN", "pin", "Pin", "ARP_NO", "TD_NO", "PARCEL_ID"]
    _pin_col = next((c for c in _PIN_CANDIDATES if c in brgy_gdf.columns), None)

    # ✅ iterate faster over geometry series
    geoms = brgy_gdf.geometry.values

    for i, geom_raw in enumerate(geoms, start=1):
        if progress and (i % 200 == 0 or i == 1 or i == total):
            progress(f"Processing {source_name}: {i}/{total}", i, total)

        geom = fix_geometry(geom_raw)
        if geom is None:
            frontage_lengths.append(0.0)
            depths.append(0.0)
            dwrs.append(0.0)
            frontage_lines_data.append((None, None, 0.0, 0.0, []))
            continue

        boundary = geom.boundary

        # FRONTAGE: per-edge adjacency test, not a whole-road buffer.
        # Each elementary boundary segment (vertex-to-vertex) gets its own
        # confined 10m buffer (flat-capped via _edge_covered_portion) —
        # a road detected near one segment cannot "bleed" onto a
        # perpendicular segment near a corner, which was a confirmed,
        # reproducible defect of the old whole-road-buffer approach.
        # ROAD_FRONT is the sum of the genuinely covered portion of every
        # segment; a segment with no nearby road contributes nothing.
        # Truncation matches the road's true extent (flat cap), and
        # segments genuinely dangling past where the road ends are simply
        # not covered — no artificial full-edge extension.
        #
        # Internal roads (running through the parcel interior, not near
        # any boundary edge) are explicitly excluded per business
        # decision — frontage is boundary-only, never interior.
        try:
            segments = split_boundary_to_segments(boundary)
            covered_pieces = [
                piece for piece in (
                    _edge_covered_portion(seg, road_union) for seg in segments
                )
                if piece is not None
            ]
            # Weld consecutive covered pieces back into continuous lines.
            # _edge_covered_portion() works per elementary (vertex-to-vertex)
            # segment, so a long run of adjacent covered segments would
            # otherwise stay fragmented into many tiny pieces instead of
            # one continuous edge — confirmed reproducible via a jagged
            # boundary test. linemerge() only welds pieces that genuinely
            # share an endpoint; two disjoint edges (e.g. both sides of a
            # corner lot) or a truncated piece that stops short of a
            # vertex are correctly left separate.
            if covered_pieces:
                merged = linemerge(covered_pieces)
                if merged.geom_type == "LineString":
                    covered_pieces = [merged]
                elif merged.geom_type == "MultiLineString":
                    covered_pieces = list(merged.geoms)
            frontage_total = sum(p.length for p in covered_pieces)
            _all_pieces = covered_pieces
            _fl = max(covered_pieces, key=lambda p: p.length) if covered_pieces else None
        except Exception:
            # TEMPORARY DIAGNOSTIC -- remove after capturing frontage_error.log.
            # Logs full traceback beside the exe so the actual exception type
            # and failing geometry coordinates can be identified before any
            # permanent exception-handling change is made.
            import traceback as _tb
            try:
                _log = os.path.join(os.path.dirname(sys.executable), "frontage_error.log")
                with open(_log, "a", encoding="utf-8") as _f:
                    _f.write("[FRONTAGE BLOCK]\n")
                    _f.write(_tb.format_exc())
                    _f.write("\n---\n")
            except Exception:
                pass
            frontage_total = 0.0
            _fl = None
            _all_pieces = []

        frontage_lengths.append(frontage_total)

        # QA: capture actual frontage geometry (_fl itself, not the chord line)
        # so the exported layer faithfully shows the road-facing boundary
        # portion the algorithm detected, including any real kinks or curves.
        _frontage_line = _fl if (_fl is not None and not _fl.is_empty) else None

        _depth_line = None
        if frontage_total > 0:
            # Use the longest frontage piece to define the perpendicular direction.
            frontage_ls = _fl  # already resolved above
            if frontage_ls is None or frontage_ls.length == 0:
                depth_val = 0.0
            else:
                midpoint = frontage_ls.interpolate(0.5, normalized=True)
                coords = list(frontage_ls.coords)
                x1, y1 = coords[0]
                x2, y2 = coords[-1]
                dx = x2 - x1
                dy = y2 - y1

                perp_dx, perp_dy = -dy, dx
                length = math.hypot(perp_dx, perp_dy)
                if length == 0:
                    depth_val = 0.0
                else:
                    perp_dx /= length
                    perp_dy /= length
                    max_depth = 1000

                    line1 = LineString([midpoint, (midpoint.x + perp_dx * max_depth, midpoint.y + perp_dy * max_depth)])
                    line2 = LineString([midpoint, (midpoint.x - perp_dx * max_depth, midpoint.y - perp_dy * max_depth)])

                    # Choose the ray that intersects deeper into the parcel.
                    try:
                        i1 = line1.intersection(geom)
                        i2 = line2.intersection(geom)
                        len1 = i1.length if not i1.is_empty else 0.0
                        len2 = i2.length if not i2.is_empty else 0.0
                        depth_val = max(len1, len2)
                    except Exception:
                        # TEMPORARY DIAGNOSTIC -- remove after capturing frontage_error.log.
                        import traceback as _tb
                        try:
                            _log = os.path.join(os.path.dirname(sys.executable), "frontage_error.log")
                            with open(_log, "a", encoding="utf-8") as _f:
                                _f.write("[DEPTH BLOCK]\n")
                                _f.write(_tb.format_exc())
                                _f.write("\n---\n")
                        except Exception:
                            pass
                        len1, len2, depth_val = 0.0, 0.0, 0.0
                        i1 = i2 = type("_empty", (), {"is_empty": True})()

                    # QA: capture the actual chosen perpendicular ray so the
                    # exported layer shows exactly where depth was measured.
                    chosen_inter = i1 if len1 >= len2 else i2
                    if not chosen_inter.is_empty:
                        if chosen_inter.geom_type == "LineString":
                            end_pt = chosen_inter.coords[-1]
                        elif chosen_inter.geom_type == "MultiLineString":
                            end_pt = max(
                                chosen_inter.geoms, key=lambda g: g.length
                            ).coords[-1]
                        else:
                            end_pt = None
                        if end_pt:
                            _depth_line = LineString([midpoint, end_pt])

            dwr_val = round(depth_val / frontage_total, 2) if frontage_total else 0.0
        else:
            # Inner lot fallback: no road frontage — store centroid-to-road
            # distance as depth so the attribute is still meaningful.
            try:
                depth_val = geom.centroid.distance(road_union)
            except Exception:
                depth_val = 0.0
            dwr_val = depth_val

        depths.append(depth_val)
        dwrs.append(dwr_val)
        frontage_lines_data.append((_frontage_line, _depth_line, frontage_total, depth_val, _all_pieces))

    # 🔒 Safety check — prevents silent column mismatch on partial failures.
    if not (len(frontage_lengths) == len(brgy_gdf) == len(depths) == len(dwrs)):
        raise RuntimeError("Attribute length mismatch during frontage processing")

    brgy_gdf["ROAD_FRONTAGE"] = frontage_lengths
    brgy_gdf["DEPTH"] = depths
    brgy_gdf["DEPTH_WIDTH_RATIO"] = dwrs

    if original_crs:
        brgy_gdf = brgy_gdf.to_crs(original_crs)

    if progress:
        progress(f"Building QA layer for {source_name}...", total, total)

    # ---- Build QA frontage_lines GeoDataFrame ----
    # ONE row per parcel that has road frontage — combines every frontage
    # boundary piece (all of them, not just the longest — matches the
    # arc-sum ROAD_FRONT total, so a corner lot's two edges both appear)
    # plus the DEPTH perpendicular ray into a single MultiLineString.
    # Both ROAD_FRONT_M and DEPTH_M are populated on that one row, so
    # clicking any part of the feature shows both measurements together.
    # FEATURE_ID uses PIN if available, otherwise the zero-based row index.
    # All numeric columns are always float64 — None cells use float("nan")
    # so GIS software infers consistent field types from the first row.
    line_records = []
    for idx, (fl, dl, fval, dval, all_pieces) in enumerate(frontage_lines_data):
        feat_id = brgy_gdf.iloc[idx][_pin_col] if _pin_col else idx
        # Only emit a QA feature when ROAD_FRONT is greater than zero.
        # This preserves the business rule:
        #   ROAD_FRONT > 0  -> exactly one QA feature (frontage + depth)
        #   ROAD_FRONT = 0  -> no QA feature
        if fval > 0 and all_pieces:
            parts = list(all_pieces)
            if dl is not None:
                parts.append(dl)
            line_records.append({
                "FEATURE_ID":   feat_id,
                "ROAD_FRONT_M": float(round(fval, 2)),
                "DEPTH_M":      float(round(dval, 2)) if dval else float("nan"),
                "geometry":     MultiLineString(parts),
            })

    _qa_crs = original_crs if original_crs else f"EPSG:{zone_epsg}"
    if line_records:
        lines_gdf = gpd.GeoDataFrame(line_records, crs=f"EPSG:{zone_epsg}")
        if original_crs:
            lines_gdf = lines_gdf.to_crs(original_crs)
    else:
        lines_gdf = gpd.GeoDataFrame(
            columns=["FEATURE_ID", "ROAD_FRONT_M", "DEPTH_M", "geometry"],
            geometry="geometry",
            crs=_qa_crs,
        )

    if progress:
        progress(f"Finished {source_name}", total, total)

    # Return parcels and QA layers as a structured dict so callers can
    # access each output by name. qa_layers is a container — future QA
    # outputs (e.g. snapped points, debug polygons) can be added here
    # without changing the caller API again.
    return {
        "parcels": brgy_gdf,
        "qa_layers": {
            "frontage_lines": lines_gdf,
        },
    }


# ========================= MAIN PROCESS =========================
def run_processing(app_root):
    global barangay_source, road_source, output_mode
    if not barangay_source or not road_source or not output_mode:
        messagebox.showerror("Error", "Selections incomplete (Barangay, Road, Output required).")
        return

    creds = load_db_credentials()
    if not creds:
        messagebox.showerror("Error", "Missing pg_credentials.json")
        return

    schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    )

    progress = ProgressWindow(app_root, "Road Frontage Progress")

    q = queue.Queue()

    def worker():
        try:
            q.put(("update", "Loading road data...", None, None))

            def progress_cb(msg, val=None, maxv=None):
                q.put(("update", msg, val, maxv))


            if road_source[0] == "local":
                road_gdf = gpd.read_file(road_source[1][0])
            else:
                road_table = road_source[1][0]
                road_gdf = read_postgis_clean(road_table, engine, schema)

            if barangay_source[0] == "local":
                sources = [("local", p) for p in barangay_source[1]]
            else:
                sources = [("db", t) for t in barangay_source[1]]

            skipped = []
            for src_type, src in sources:
                try:
                    if src_type == "local":
                        name = os.path.basename(src)
                        q.put(("update", f"Loading {name}", None, None))
                        brgy_gdf = gpd.read_file(src)
                        out_base = os.path.splitext(name)[0]
                    else:
                        name = src
                        q.put(("update", f"Loading DB table {name}", None, None))
                        brgy_gdf = read_postgis_clean(name, engine, schema)
                        out_base = name

                    result = process_frontage_single(
                        brgy_gdf,
                        road_gdf,
                        name,
                        progress=progress_cb
                    )
                    brgy_gdf  = result["parcels"]
                    lines_gdf = result["qa_layers"]["frontage_lines"]

                    if output_mode[0] == "local":
                        out = os.path.join(
                            output_mode[1],
                            f"{out_base}_road_frontage.gpkg"
                        )
                        brgy_gdf.to_file(out, driver="GPKG")
                        q.put(("open_gm", out, None, None))

                        # QA layer: frontage_lines.gpkg — written alongside the
                        # main output so QA can load both in the same GM session.
                        # Only written for local output (DB mode has no output_dir).
                        if not lines_gdf.empty:
                            lines_out = os.path.join(
                                output_mode[1],
                                f"{out_base}_frontage_lines.gpkg"
                            )
                            lines_gdf.to_file(lines_out, driver="GPKG")
                            q.put(("open_gm", lines_out, None, None))
                    else:
                        brgy_gdf.to_postgis(
                            out_base,
                            engine,
                            schema=schema,
                            if_exists="replace",
                            index=False
                        )
                        # frontage_lines is a QA-only artifact — not written to DB.

                except Exception as source_err:
                    # Isolate failures per source: log/report and move on to
                    # the next source instead of aborting the entire batch.
                    # Sources already written before this one keep their
                    # output — only this one is skipped.
                    skipped.append((name, str(source_err)))
                    q.put(("update", f"Skipped {name}: {source_err}", None, None))
                    continue

            if skipped:
                summary = "Done, but some sources were skipped:\n" + "\n".join(
                    f"- {n}: {err}" for n, err in skipped
                )
            else:
                summary = "Processing done!"
            q.put(("done", summary, None, None))

        except Exception as e:
            q.put(("error", str(e), None, None))

    def poll_queue():
        try:
            while True:
                msg = q.get_nowait()
                kind = msg[0]

                if kind == "update":
                    progress.update(msg[1], msg[2], msg[3])

                elif kind == "open_gm":
                    open_in_global_mapper(msg[1])

                elif kind == "done":
                    progress.close()
                    messagebox.showinfo("Success", msg[1])
                    return

                elif kind == "error":
                    progress.close()
                    messagebox.showerror("Error", msg[1])
                    return

        except queue.Empty:
            pass

        app_root.after(100, poll_queue)

    threading.Thread(target=worker, daemon=True).start()
    poll_queue()


# ========================= MAIN APP =========================
def _pick_db_tables(parent, tables, multi, on_select):
    picker = tk.Toplevel(parent)
    apply_icon(picker)
    picker.title("Select Table(s)")
    picker.resizable(False, False)
    picker.grab_set()

    mode = tk.MULTIPLE if multi else tk.SINGLE
    lb = Listbox(picker, selectmode=mode, width=55, height=15)
    for t in tables:
        lb.insert(tk.END, t)
    lb.pack(padx=10, pady=10)

    def submit():
        sel = [lb.get(i) for i in lb.curselection()]
        if sel:
            on_select(sel)
            picker.destroy()

    tk.Button(picker, text="Confirm Selection", command=submit,
              width=20).pack(pady=(0, 10))


def open_main_window(root):
    win = tk.Toplevel(root)
    apply_icon(win)
    win.title("Road Frontage & Depth-To-Width Ratio Tool")
    win.resizable(False, False)
    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))

    # ── state ────────────────────────────────────────────────────
    # master=win on every StringVar is required — without it, Tkinter
    # attaches the variable to whatever the "default root" happens to be,
    # which is unreliable in this dispatcher's multi-window setup
    # (MAIN3.py creates a hidden root, then this Toplevel is created on
    # top of it). A mismatched master causes widgets to silently read a
    # different variable instance than the one being set, showing blank
    # labels and radio buttons that never appear selected — confirmed as
    # the root cause of the blank-GUI symptom reported in testing.
    parcel_source_type = tk.StringVar(master=win, value="local")
    road_source_type   = tk.StringVar(master=win, value="local")
    output_dest_type   = tk.StringVar(master=win, value="local")

    parcel_local_paths = []
    parcel_db_tables   = []
    road_local_path    = tk.StringVar(master=win)
    road_db_table      = tk.StringVar(master=win)
    output_local_dir   = tk.StringVar(master=win)

    PAD = dict(padx=8, pady=4)

    def section_label(parent, text):
        frm = tk.Frame(parent)
        frm.pack(fill="x", padx=10, pady=(10, 2))
        tk.Label(frm, text=text,
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        ttk.Separator(frm, orient="horizontal").pack(
            side="left", fill="x", expand=True, padx=(6, 0), pady=4)

    # ── SECTION 1: LAND PARCEL ───────────────────────────────────
    section_label(win, "Land Parcel Source")

    parcel_frame = tk.Frame(win)
    parcel_frame.pack(fill="x", padx=18, pady=2)

    radio_row = tk.Frame(parcel_frame)
    radio_row.pack(fill="x")
    tk.Radiobutton(radio_row, text="Local File(s)",
                   variable=parcel_source_type, value="local",
                   command=lambda: _toggle_parcel()).pack(side="left")
    tk.Radiobutton(radio_row, text="Database Table(s)",
                   variable=parcel_source_type, value="db",
                   command=lambda: _toggle_parcel()).pack(side="left", padx=(12, 0))

    parcel_files_var = tk.StringVar(master=win, value="No file(s) selected")
    parcel_db_label  = tk.StringVar(master=win, value="No table(s) selected")

    parcel_action_row = tk.Frame(parcel_frame)
    parcel_action_row.pack(fill="x", pady=2)

    parcel_lbl = tk.Label(parcel_action_row, textvariable=parcel_files_var,
                          fg="gray", anchor="w", width=42)
    parcel_lbl.pack(side="left")

    parcel_btn = tk.Button(parcel_action_row, text="Browse…", width=10)
    parcel_btn.pack(side="left", **PAD)

    def browse_parcel_files():
        files = filedialog.askopenfilenames(filetypes=[
            ("Shapefiles", "*.shp"), ("GeoPackage", "*.gpkg"), ("All", "*.*")])
        if files:
            parcel_local_paths.clear()
            parcel_local_paths.extend(files)
            parcel_files_var.set(f"{len(files)} file(s) selected")

    def browse_parcel_db():
        creds = load_db_credentials()
        if not creds:
            messagebox.showerror("Error", "Could not load DB credentials.")
            return
        tables = fetch_tables(creds["schema"])
        if not tables:
            messagebox.showwarning("No Tables", "No tables found in the database schema.")
            return
        _pick_db_tables(win, tables, multi=True,
            on_select=lambda sel: (
                parcel_db_tables.__setitem__(slice(None), sel)
                or parcel_db_label.set(f"{len(sel)} table(s) selected")
            ))

    def _toggle_parcel():
        if parcel_source_type.get() == "local":
            parcel_lbl.config(textvariable=parcel_files_var)
            parcel_btn.config(text="Browse…", command=browse_parcel_files)
        else:
            parcel_lbl.config(textvariable=parcel_db_label)
            parcel_btn.config(text="Select…", command=browse_parcel_db)

    # ── SECTION 2: ROAD NETWORK ──────────────────────────────────
    section_label(win, "Road Network Source")

    road_frame = tk.Frame(win)
    road_frame.pack(fill="x", padx=18, pady=2)

    road_radio_row = tk.Frame(road_frame)
    road_radio_row.pack(fill="x")
    tk.Radiobutton(road_radio_row, text="Local File",
                   variable=road_source_type, value="local",
                   command=lambda: _toggle_road()).pack(side="left")
    tk.Radiobutton(road_radio_row, text="Database Table",
                   variable=road_source_type, value="db",
                   command=lambda: _toggle_road()).pack(side="left", padx=(12, 0))

    road_file_var = tk.StringVar(master=win, value="No file selected")
    road_db_var   = tk.StringVar(master=win, value="No table selected")

    road_action_row = tk.Frame(road_frame)
    road_action_row.pack(fill="x", pady=2)

    road_lbl = tk.Label(road_action_row, textvariable=road_file_var,
                        fg="gray", anchor="w", width=42)
    road_lbl.pack(side="left")

    road_btn = tk.Button(road_action_row, text="Browse…", width=10)
    road_btn.pack(side="left", **PAD)

    def browse_road_file():
        f = filedialog.askopenfilename(filetypes=[
            ("Shapefiles", "*.shp"), ("GeoPackage", "*.gpkg"), ("All", "*.*")])
        if f:
            road_local_path.set(f)
            road_file_var.set(os.path.basename(f))

    def browse_road_db():
        creds = load_db_credentials()
        if not creds:
            messagebox.showerror("Error", "Could not load DB credentials.")
            return
        tables = fetch_tables(creds["schema"])
        if not tables:
            messagebox.showwarning("No Tables", "No tables found in the database schema.")
            return
        _pick_db_tables(win, tables, multi=False,
            on_select=lambda sel: (
                road_db_table.set(sel[0]) if sel else None,
                road_db_var.set(sel[0] if sel else "No table selected")
            ))

    def _toggle_road():
        if road_source_type.get() == "local":
            road_lbl.config(textvariable=road_file_var)
            road_btn.config(text="Browse…", command=browse_road_file)
        else:
            road_lbl.config(textvariable=road_db_var)
            road_btn.config(text="Select…", command=browse_road_db)

    # ── SECTION 3: OUTPUT ────────────────────────────────────────
    section_label(win, "Output Destination")

    output_frame = tk.Frame(win)
    output_frame.pack(fill="x", padx=18, pady=2)

    out_radio_row = tk.Frame(output_frame)
    out_radio_row.pack(fill="x")
    tk.Radiobutton(out_radio_row, text="Save to Local Folder",
                   variable=output_dest_type, value="local",
                   command=lambda: _toggle_output()).pack(side="left")
    tk.Radiobutton(out_radio_row, text="Save to Database",
                   variable=output_dest_type, value="db",
                   command=lambda: _toggle_output()).pack(side="left", padx=(12, 0))

    output_dir_var = tk.StringVar(master=win, value="No folder selected")
    output_db_var  = tk.StringVar(master=win,
                                  value="Will write back to the connected PostGIS schema.")

    out_action_row = tk.Frame(output_frame)
    out_action_row.pack(fill="x", pady=2)

    out_lbl = tk.Label(out_action_row, textvariable=output_dir_var,
                       fg="gray", anchor="w", width=42)
    out_lbl.pack(side="left")

    out_btn = tk.Button(out_action_row, text="Browse…", width=10)
    out_btn.pack(side="left", **PAD)

    def browse_output_dir():
        d = filedialog.askdirectory()
        if d:
            output_local_dir.set(d)
            output_dir_var.set(d)

    def _toggle_output():
        if output_dest_type.get() == "local":
            out_lbl.config(textvariable=output_dir_var,
                           font=("Segoe UI", 9), fg="gray")
            out_btn.config(text="Browse…", command=browse_output_dir)
            out_btn.pack(side="left", **PAD)
        else:
            out_lbl.config(textvariable=output_db_var,
                           font=("Segoe UI", 8, "italic"), fg="gray")
            out_btn.pack_forget()

    out_btn.config(command=browse_output_dir)

    # ── RUN BUTTON ───────────────────────────────────────────────
    ttk.Separator(win, orient="horizontal").pack(
        fill="x", padx=10, pady=(12, 4))

    def on_run():
        global barangay_source, road_source, output_mode

        if parcel_source_type.get() == "local":
            if not parcel_local_paths:
                messagebox.showerror("Missing Input",
                    "Please select at least one Land Parcel file.")
                return
            barangay_source = ("local", tuple(parcel_local_paths))
        else:
            if not parcel_db_tables:
                messagebox.showerror("Missing Input",
                    "Please select at least one Land Parcel table.")
                return
            barangay_source = ("db", parcel_db_tables)

        if road_source_type.get() == "local":
            if not road_local_path.get():
                messagebox.showerror("Missing Input",
                    "Please select a Road Network file.")
                return
            road_source = ("local", [road_local_path.get()])
        else:
            if not road_db_table.get():
                messagebox.showerror("Missing Input",
                    "Please select a Road Network table.")
                return
            road_source = ("db", [road_db_table.get()])

        if output_dest_type.get() == "local":
            if not output_local_dir.get():
                messagebox.showerror("Missing Input",
                    "Please select an output folder.")
                return
            output_mode = ("local", output_local_dir.get())
        else:
            output_mode = ("db", None)

        win.destroy()
        if _app_root is None:
            messagebox.showerror("Error", "No root window available. Please restart the tool.")
            return
        run_processing(_app_root)

    tk.Button(win, text="▶  Run Processing", command=on_run,
              bg="#2e7d32", fg="white",
              font=("Segoe UI", 10, "bold"),
              relief="flat", padx=16, pady=6).pack(pady=(4, 14))

    # set initial button commands to match default radio state
    _toggle_parcel()
    _toggle_road()
    _toggle_output()



def main(parent=None):
    global _app_root
    if parent is not None:
        _app_root = parent
        open_main_window(parent)
    else:
        root = tk.Tk()
        _app_root = root
        apply_icon(root)
        root.withdraw()
        open_main_window(root)
        root.mainloop()


if __name__ == "__main__":
    main()