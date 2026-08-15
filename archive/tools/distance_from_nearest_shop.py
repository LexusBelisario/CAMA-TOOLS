import os
import tkinter as tk
from tkinter import filedialog, messagebox
import geopandas as gpd
import osmnx as ox
import networkx as nx
from shapely.geometry import Point, LineString, box
from geopy.distance import geodesic
import subprocess

# =====================================================
# CONFIG
# =====================================================
POI_TYPE = "shop"  # Change to "shop" or "transport" for other versions
GM_EXE_PATH = r"C:\\Program Files\\GlobalMapper26.1_64bit\\global_mapper.exe"

barangay_source = None
poi_source = None
output_dir = None

ox.settings.use_cache = True
ox.settings.log_console = False


# =====================================================
# HELPERS
# =====================================================
def load_into_global_mapper(path):
    if os.path.exists(GM_EXE_PATH) and os.path.exists(path):
        subprocess.Popen([GM_EXE_PATH, path], shell=True)


# =====================================================
# CORE DISTANCE PROCESSING (for one POI type)
# =====================================================
def process_distances(gdf, poi_gdf):
    gdf = gdf.to_crs(4326)
    poi_gdf = poi_gdf.to_crs(4326)

    gdf[f"{POI_TYPE.upper()}_DIST"] = None

    minx, miny, maxx, maxy = gdf.total_bounds
    bbox_poly = box(minx - 0.02, miny - 0.02, maxx + 0.02, maxy + 0.02)

    print(f"🌐 Downloading OSM road network for {POI_TYPE} computation...")
    G = ox.graph_from_polygon(bbox_poly, network_type="drive")

    def add_virtual_node(G, lat, lon, node_id):
        try:
            u, v, key = ox.distance.nearest_edges(G, lon, lat)
            edge_data = G.get_edge_data(u, v)[key]
            line = edge_data.get("geometry", LineString([
                (G.nodes[u]["x"], G.nodes[u]["y"]),
                (G.nodes[v]["x"], G.nodes[v]["y"])
            ]))
            proj_point = line.interpolate(line.project(Point(lon, lat)))
            coords = (proj_point.y, proj_point.x)

            G.add_node(node_id, x=coords[1], y=coords[0])
            d_u = geodesic((G.nodes[u]["y"], G.nodes[u]["x"]), coords).meters
            d_v = geodesic((G.nodes[v]["y"], G.nodes[v]["x"]), coords).meters
            for a, b, d in [(u, node_id, d_u), (v, node_id, d_v)]:
                G.add_edge(a, b, 0, length=d)
                G.add_edge(b, a, 0, length=d)
            return node_id
        except:
            return None

    subset = poi_gdf[poi_gdf["fclass"].str.lower() == POI_TYPE]
    if subset.empty:
        messagebox.showerror("Error", f"No POIs found for type '{POI_TYPE}'")
        return gdf

    for idx, row in gdf.iterrows():
        centroid = row.geometry.centroid
        lat, lon = centroid.y, centroid.x
        subset["DIST"] = subset.geometry.distance(centroid)
        nearest_poi = subset.loc[subset["DIST"].idxmin()]
        start_node = add_virtual_node(G, lat, lon, f"start_{idx}")
        end_node = add_virtual_node(G, nearest_poi.geometry.y, nearest_poi.geometry.x, f"end_{idx}")

        try:
            if start_node and end_node and nx.has_path(G, start_node, end_node):
                dist, _ = nx.bidirectional_dijkstra(G, start_node, end_node, weight="length")
                gdf.at[idx, f"{POI_TYPE.upper()}_DIST"] = round(dist, 2)
                print(f"✅ {POI_TYPE} distance: {dist:.2f} m")
            else:
                raise Exception("No route found")
        except:
            fallback = geodesic((lat, lon), (nearest_poi.geometry.y, nearest_poi.geometry.x)).meters
            gdf.at[idx, f"{POI_TYPE.upper()}_DIST"] = round(fallback, 2)
            print(f"⚠️ Fallback distance: {fallback:.2f} m")

        for n in [f"start_{idx}", f"end_{idx}"]:
            if n in G:
                G.remove_node(n)

    return gdf


# =====================================================
# TKINTER UI
# =====================================================
def select_files():
    global barangay_source, poi_source, output_dir

    root = tk.Tk()
    root.withdraw()

    # Select barangay shapefile
    messagebox.showinfo("Barangay Layer", "Select the Barangay Parcel shapefile (.shp)")
    barangay_file = filedialog.askopenfilename(filetypes=[("Shapefiles", "*.shp")])
    if not barangay_file:
        return
    barangay_source = barangay_file

    # Select POI shapefile
    messagebox.showinfo("POI Layer", f"Select the POI shapefile containing '{POI_TYPE}' data")
    poi_file = filedialog.askopenfilename(filetypes=[("Shapefiles", "*.shp")])
    if not poi_file:
        return
    poi_source = poi_file

    # Select output folder
    messagebox.showinfo("Output Folder", "Select a folder to save output shapefile")
    output_dir = filedialog.askdirectory()
    if not output_dir:
        return

    run_processing()


# =====================================================
# MAIN PROCESSING
# =====================================================
def run_processing():
    global barangay_source, poi_source, output_dir
    print(f"\n🔷 Processing for {POI_TYPE.upper()} distance...")

    gdf = gpd.read_file(barangay_source)
    poi_gdf = gpd.read_file(poi_source)

    result = process_distances(gdf, poi_gdf)

    out_path = os.path.join(output_dir, f"{os.path.splitext(os.path.basename(barangay_source))[0]}_{POI_TYPE}_dist.shp")
    result.to_file(out_path)
    print(f"✅ Saved: {out_path}")

    load_into_global_mapper(out_path)
    messagebox.showinfo("Done", f"Processing complete!\nSaved to:\n{out_path}")


if __name__ == "__main__":
    select_files()
