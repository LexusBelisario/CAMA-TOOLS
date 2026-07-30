import geopandas as gpd
import numpy as np
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox
from shapely.geometry import Polygon, MultiPolygon
import math
import os
import subprocess
import json
import psycopg2
from sqlalchemy import create_engine, inspect, text
from shapely.validation import make_valid
import re

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

    # Taskbar / Alt-Tab icon
    if os.path.exists(ico):
        try:
            win.iconbitmap(ico)
        except Exception:
            pass

    # Tk titlebar fallback
    if os.path.exists(png):
        try:
            img = tk.PhotoImage(file=png)
            win.iconphoto(True, img)
            win._icon_ref = img  # prevent garbage collection
        except Exception:
            pass


# === Global Mapper EXE and Icon Paths
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"
def _get_credentials_path():
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "pg_credentials.json")
    else:
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "pg_credentials.json"
        )

CREDENTIALS_FILE = _get_credentials_path()

barangay_source = None
output_mode = None

# ---------------- Geometry Fix ----------------
def fix_geometry(geom):
    if geom is None or geom.is_empty: return None
    try:
        if not geom.is_valid:
            geom = geom.buffer(0)
        if not geom.is_valid:
            geom = make_valid(geom)
        return geom if not geom.is_empty else None
    except:
        return None

# ---------------- Helpers ----------------
def angle_between(p1, p2, p3):
    v1 = (p1[0] - p2[0], p1[1] - p2[1])
    v2 = (p3[0] - p2[0], p3[1] - p2[1])
    dot = v1[0]*v2[0] + v1[1]*v2[1]
    det = v1[0]*v2[1] - v1[1]*v2[0]
    angle_rad = math.atan2(det, dot)
    angle_deg = math.degrees(angle_rad)
    return round(angle_deg + 360 if angle_deg < 0 else angle_deg, 2)

def vertex_angles(polygon: Polygon):
    coords = list(polygon.exterior.coords)
    if coords[0] == coords[-1]: coords = coords[:-1]
    cleaned = [coords[0]]
    for pt in coords[1:]:
        if math.dist(pt, cleaned[-1]) != 0:
            cleaned.append(pt)
    angles = []
    n = len(cleaned)
    if n < 3: return angles
    for i in range(n):
        p1, p2, p3 = cleaned[i-1], cleaned[i], cleaned[(i+1) % n]
        if math.dist(p2,p3)==0: continue
        angles.append(angle_between(p1,p2,p3))
    return angles

def classify_lot_shape(angles):
    low_angles = [a for a in angles if a <= 169]
    rightish   = [a for a in angles if 170 <= a <= 190]
    obtuse     = [a for a in angles if 190 < a < 260]
    l_cands    = [a for a in angles if 260 <= a <= 280]
    if len(l_cands)==1: return "L_SHAPED"
    elif len(l_cands)>1: return "OTHERS"
    if len(angles)==3 and len(low_angles)==3: return "TRIANGLE"
    if len(low_angles)==3 and len(obtuse)==0: return "TRIANGLE"
    if len(angles)==4 and len(low_angles)==4: return "RECTANGLE"
    elif len(angles)>4:
        if len(low_angles)==4 and all(170<=a<=190 for a in rightish) and len(obtuse)==0:
            return "RECTANGLE"
    return "OTHERS"

def largest_polygon(geom):
    if isinstance(geom, Polygon): return geom
    if isinstance(geom, MultiPolygon) and len(geom.geoms)>0:
        return max(geom.geoms, key=lambda g:g.area)
    return None

def auto_utm_epsg_from_gdf(gdf):
    """
    Choose the UTM zone EPSG from the bbox-midpoint longitude of the
    input GeoDataFrame. UTM (not PRS92) is intentionally kept here --
    the Polsby-Popper ratio computed downstream is largely invariant
    under the locally uniform scale distortions expected for ordinary
    cadastral parcels (both UTM and PRS92 are Transverse Mercator
    family, locally conformal projections -- area and perimeter scale
    by k^2 and k respectively under a locally uniform scale factor k,
    which cancels out in the area/perimeter^2 ratio), so switching CRS
    systems has no established accuracy benefit for this computation
    and was not pursued.

    Uses total_bounds, not a unioned-geometry centroid -- unary_union.centroid
    is a known source of GEOS TopologyExceptions on real-world cadastral
    data with invalid geometries.
    """
    if gdf is None or gdf.empty or not gdf.geometry.notna().any():
        raise ValueError("No valid (non-empty) GeoDataFrame provided for UTM zone detection.")

    g = gdf if gdf.crs is not None else gdf.set_crs(epsg=4326, allow_override=True)
    epsg = g.crs.to_epsg()
    g_wgs84 = g.to_crs(epsg=4326) if epsg != 4326 else g

    bounds = g_wgs84.total_bounds
    if np.isnan(bounds).any():
        raise ValueError("Cannot determine UTM zone because the Land Parcel layer contains no valid geometry.")

    lon = (bounds[0] + bounds[2]) / 2
    zone = int((lon + 180) // 6) + 1
    return 32600 + zone

# ---------------- Core ----------------
def compute_ppr_and_lot_shape_gdf(gdf):
    # Save the original CRS
    original_crs = gdf.crs

    # Ensure we work in projected CRS
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326, allow_override=True)
    if gdf.crs.is_geographic:
        epsg = auto_utm_epsg_from_gdf(gdf)
        gdf = gdf.to_crs(epsg=epsg)

    # Geometry repair is scoped to this local Series only -- used below
    # for area/perimeter/vertex-angle computation, never written back
    # into gdf's own geometry column. The exported output keeps each
    # parcel's original, untouched shape, even if invalid.
    #
    # Repair is genuinely needed here (unlike, say, a centroid-only
    # computation elsewhere in this project) -- confirmed empirically:
    # a self-intersecting rectangle-with-digitizing-fold test case
    # classified as OTHERS with PP_RATIO 0.27 on the raw geometry, vs
    # RECTANGLE with PP_RATIO 0.96 after repair, because vertex_angles()
    # reads the exterior ring's raw coordinate sequence directly.
    fixed_geoms = gdf.geometry.apply(fix_geometry)

    # ---- Do calculations in projected CRS, using the repaired geometry ----
    area = fixed_geoms.area
    perimeter = fixed_geoms.length
    gdf["PP_RATIO"] = ((4 * np.pi * area) / (perimeter ** 2)).round(2)

    gdf["VTX_COUNT"] = 0
    gdf["ANGS_TXT"] = ""

    for col in ["TRIANGLE", "RECTANGLE", "L_SHAPED", "OTHERS"]:
        if col not in gdf.columns:
            gdf[col] = 0
    gdf["LOT_SHAPE"] = ""

    # .items() yields gdf's actual index LABELS (not positions), which
    # gdf.at[] requires. enumerate() gives positional 0..N-1 instead --
    # if gdf's index has any gaps (e.g. from upstream row filtering),
    # gdf.at[label, ...] with a positional number that isn't an actual
    # label silently creates a brand-new row full of NaNs rather than
    # raising an error. Confirmed empirically. Independent of the
    # geometry-repair change above -- this indexing correctness issue
    # applies regardless of what fixed_geoms contains.
    for idx, geom in fixed_geoms.items():
        poly = largest_polygon(geom)
        if poly is None:
            # Temporary behavior: parcels whose geometry cannot be
            # repaired are retained in the output (never dropped --
            # every input row must appear exactly once in the output)
            # with LOT_SHAPE="OTHERS" and PP_RATIO=NaN (area/perimeter
            # above are NaN for a None entry in fixed_geoms, so
            # PP_RATIO is already NaN for this row without extra code
            # here) until the business rule for a dedicated
            # "INVALID_GEOMETRY" classification is finalized with the
            # team lead. The parcel's OWN geometry in the output stays
            # exactly as originally read -- only this repaired local
            # `poly` failed, not gdf's own geometry column.
            gdf.at[idx, "TRIANGLE"] = 0
            gdf.at[idx, "RECTANGLE"] = 0
            gdf.at[idx, "L_SHAPED"] = 0
            gdf.at[idx, "OTHERS"] = 1
            gdf.at[idx, "LOT_SHAPE"] = "OTHERS"
            continue

        angles = vertex_angles(poly)
        shape_type = classify_lot_shape(angles)

        gdf.at[idx, "TRIANGLE"] = 0
        gdf.at[idx, "RECTANGLE"] = 0
        gdf.at[idx, "L_SHAPED"] = 0
        gdf.at[idx, "OTHERS"] = 0
        gdf.at[idx, shape_type] = 1
        gdf.at[idx, "LOT_SHAPE"] = shape_type
        gdf.at[idx, "VTX_COUNT"] = len(angles)
        gdf.at[idx, "ANGS_TXT"] = ",".join(map(str, angles))

    # ✅ Reproject back to original CRS before returning
    if original_crs:
        gdf = gdf.to_crs(original_crs)

    return gdf

def open_in_global_mapper(path):
    if os.path.exists(GM_EXE_PATH) and os.path.exists(path):
        subprocess.Popen([GM_EXE_PATH, path], shell=True)

# ---------------- DB Helpers ----------------
def load_db_credentials():
    path = _get_credentials_path()
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None

def get_geometry_column(table, engine, schema):
    with engine.connect() as conn:
        row=conn.execute(text("""
            SELECT f_geometry_column FROM geometry_columns
            WHERE f_table_schema=:schema AND f_table_name=:table
        """),{"schema":schema,"table":table}).fetchone()
        return row[0] if row else None

def read_postgis_clean(table,engine,schema):
    geom_col=get_geometry_column(table,engine,schema)
    insp=inspect(engine)
    cols=[c['name'] for c in insp.get_columns(table,schema=schema) if c['name']!=geom_col]
    col_str=", ".join([f'"{c}"' for c in cols]) if cols else ""
    q=f'SELECT {col_str+", " if col_str else ""}"{geom_col}" AS geometry FROM "{schema}"."{table}"'
    return gpd.read_postgis(q,engine,geom_col="geometry")

def normalize_name(name): return re.sub(r'[^a-z]','',name.lower())

def fetch_tables(schema):
    creds=load_db_credentials()
    if not creds: return []
    try:
        conn=psycopg2.connect(
            host=creds["host"],port=creds["port"],
            dbname=creds["database"],user=creds["username"],password=creds["password"]
        )
        cur=conn.cursor()
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s;",(schema,))
        return [r[0] for r in cur.fetchall()]
    except: return []

def find_matching_table(local_name,schema):
    lname=normalize_name(local_name)
    for t in fetch_tables(schema):
        if lname in normalize_name(t) or normalize_name(t) in lname:
            return t
    return None


# ---------------- Tkinter Windows ----------------
def _pick_db_tables(parent, tables, multi, on_select):
    from tkinter import ttk
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


def load_in_global_mapper(filepath):
    try:
        gm_hwnd = None

        def enum_callback(hwnd, _):
            nonlocal gm_hwnd
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            if "Global Mapper" in buf.value:
                gm_hwnd = hwnd
                return False
            return True

        import ctypes.wintypes
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        ctypes.windll.user32.EnumWindows(EnumWindowsProc(enum_callback), 0)

        subprocess.Popen([GM_EXE_PATH, filepath])
        print(f"🗺️ Sent to Global Mapper: {filepath}")

    except Exception as e:
        print(f"⚠️ Could not open in Global Mapper: {e}")


def open_main_window(root):
    from tkinter import ttk
    win = tk.Toplevel(root)
    apply_icon(win)
    win.title("Lot Shape Tool")
    win.resizable(False, False)
    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(100, lambda: win.attributes("-topmost", False))

    # ── state ────────────────────────────────────────────────────
    parcel_source_type = tk.StringVar(master=win, value="local")
    output_dest_type   = tk.StringVar(master=win, value="local")
    parcel_local_paths = []
    parcel_db_tables   = []
    output_local_dir   = tk.StringVar(master=win)

    # run_status_var: drives the always-visible status label under the
    # Run button ("Please select ..." / "Ready to run.") and mirrors
    # whether the Run button itself is enabled. Updated by
    # _update_run_button_state() below.
    run_status_var = tk.StringVar(master=win, value="Preparing…")

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
            _update_run_button_state()

    def _on_parcel_db_selected(sel):
        parcel_db_tables.clear()
        parcel_db_tables.extend(sel)
        parcel_db_label.set(f"{len(sel)} table(s) selected")
        _update_run_button_state()

    def browse_parcel_db():
        creds = load_db_credentials()
        if not creds:
            messagebox.showerror("Error", "Could not load DB credentials.")
            return
        tables = fetch_tables(creds["schema"])
        if not tables:
            messagebox.showwarning("No Tables", "No tables found in the database schema.")
            return
        _pick_db_tables(win, tables, multi=True, on_select=_on_parcel_db_selected)

    def _toggle_parcel():
        if parcel_source_type.get() == "local":
            parcel_lbl.config(textvariable=parcel_files_var)
            parcel_btn.config(text="Browse…", command=browse_parcel_files)
        else:
            parcel_lbl.config(textvariable=parcel_db_label)
            parcel_btn.config(text="Select…", command=browse_parcel_db)
        _update_run_button_state()

    # ── SECTION 2: OUTPUT ────────────────────────────────────────
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
            _update_run_button_state()

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
        _update_run_button_state()

    # ── RUN BUTTON ───────────────────────────────────────────────
    ttk.Separator(win, orient="horizontal").pack(
        fill="x", padx=10, pady=(12, 4))

    def on_run():
        global barangay_source, output_mode

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

        if output_dest_type.get() == "local":
            if not output_local_dir.get():
                messagebox.showerror("Missing Input",
                    "Please select an output folder.")
                return
            output_mode = ("local", output_local_dir.get())
        else:
            output_mode = ("db", None)

        win.destroy()
        run_processing()

    # Single source of truth for the Run button's enabled/disabled
    # colors -- used both at button creation and inside
    # _update_run_button_state() below, so there's only one place to
    # change if the theme changes later.
    RUN_BTN_BG_ENABLED  = "#2e7d32"
    RUN_BTN_FG_ENABLED  = "white"
    RUN_BTN_BG_DISABLED = "#e0e0e0"
    RUN_BTN_FG_DISABLED = "#888888"

    def _update_run_button_state():
        """
        Single source of truth for whether the Run button may be
        pressed. Disabled (with an explanatory status message) until a
        Land Parcel source and an Output destination are both selected.

        Explicit bg/fg/cursor toggling (not just state=) is required:
        Tkinter does NOT automatically gray out a classic tk.Button's
        custom bg/fg when state="disabled", and does not suppress a
        widget's assigned cursor either -- both must be set explicitly
        for each state.
        """
        has_parcel = bool(parcel_local_paths) if parcel_source_type.get() == "local" else bool(parcel_db_tables)
        has_output = bool(output_local_dir.get()) if output_dest_type.get() == "local" else True

        if not has_parcel:
            run_status_var.set("Please select a Land Parcel source.")
            ready = False
        elif not has_output:
            run_status_var.set("Please select an Output destination.")
            ready = False
        else:
            run_status_var.set("Ready to run.")
            ready = True

        if ready:
            run_btn.config(state="normal", cursor="hand2",
                            bg=RUN_BTN_BG_ENABLED, fg=RUN_BTN_FG_ENABLED)
        else:
            run_btn.config(state="disabled", cursor="no",
                            bg=RUN_BTN_BG_DISABLED, fg=RUN_BTN_FG_DISABLED,
                            disabledforeground=RUN_BTN_FG_DISABLED)

    run_btn = tk.Button(win, text="▶  Run Processing", command=on_run,
              bg=RUN_BTN_BG_ENABLED, fg=RUN_BTN_FG_ENABLED,
              font=("Segoe UI", 10, "bold"),
              relief="flat", padx=16, pady=6)
    run_btn.pack(pady=(4, 4))

    # Permanent status line UNDER the Run button -- always visible, no
    # hover required.
    run_status_lbl = tk.Label(win, textvariable=run_status_var,
                              font=("Segoe UI", 8), fg="gray")
    run_status_lbl.pack(pady=(0, 12))

    _toggle_parcel()
    _toggle_output()
    _update_run_button_state()


# ---------------- Processing ----------------
def run_processing():
    global barangay_source, output_mode
    if not barangay_source or not output_mode:
        messagebox.showerror("Error", "Selections incomplete (Barangay + Output required).")
        return

    creds = load_db_credentials()
    schema = creds["schema"]
    engine = create_engine(
        f"postgresql://{creds['username']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    )

    if barangay_source[0] == "local":
        for path in barangay_source[1]:
            gdf = gpd.read_file(path)
            # Row-dropping REMOVED (Phase 1B decision, approved): every
            # input parcel must appear exactly once in the output. A
            # parcel whose geometry can't be repaired is no longer
            # dropped -- compute_ppr_and_lot_shape_gdf() below keeps it
            # in the output with its ORIGINAL geometry, PP_RATIO=NaN,
            # and LOT_SHAPE="OTHERS" as a temporary placeholder pending
            # a dedicated "INVALID_GEOMETRY" classification once that
            # business rule is finalized with the team lead.
            result = compute_ppr_and_lot_shape_gdf(gdf)
            if output_mode[0] == "local":
                base = os.path.splitext(os.path.basename(path))[0]
                out = os.path.join(output_mode[1], f"{base}_lotshape.gpkg")
                result.to_file(out, driver="GPKG")
                print(f"✅ Saved {out}")
                load_in_global_mapper(out)
            else:
                local_name = os.path.splitext(os.path.basename(path))[0]
                match = find_matching_table(local_name, schema)
                table = match if match else local_name.lower()
                result.to_postgis(table, engine, schema=schema, if_exists="replace", index=False)
                print(f"🔄 Saved to DB: {table}")
    else:
        for table in barangay_source[1]:
            gdf = read_postgis_clean(table, engine, schema)
            # Row-dropping REMOVED -- same reasoning as the local-source
            # branch above.
            result = compute_ppr_and_lot_shape_gdf(gdf)
            if output_mode[0] == "local":
                out = os.path.join(output_mode[1], f"{table}_lotshape.gpkg")
                result.to_file(out, driver="GPKG")
                print(f"✅ Saved {out}")
                load_in_global_mapper(out)
            else:
                result.to_postgis(table, engine, schema=schema, if_exists="replace", index=False)
                print(f"🔄 Updated DB table: {table}")

    messagebox.showinfo("Success", "✅ Processing done!")


# ---------------- Main ----------------
def main(parent=None):
    if parent is not None:
        open_main_window(parent)
    else:
        root = tk.Tk()
        apply_icon(root)
        root.withdraw()
        open_main_window(root)
        root.mainloop()


if __name__ == "__main__":
    main()