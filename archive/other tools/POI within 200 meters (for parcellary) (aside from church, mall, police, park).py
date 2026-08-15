import os
import tkinter as tk
from tkinter import filedialog, messagebox
import geopandas as gpd
import osmnx as ox
import networkx as nx
from shapely.geometry import Point, LineString, box
from geopy.distance import geodesic
import pandas as pd
import subprocess

# 🔥 Set your paths here
ICON_PATH = r"D:/2025_PROJECTS/BLGF-GM_TEST/FOR TESTING/DCS_CODES/BLGF.ico"
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"

ox.settings.use_cache = True
ox.settings.log_console = False

def load_into_global_mapper(shapefile_path):
    try:
        if not os.path.exists(GM_EXE_PATH):
            messagebox.showerror("Error", f"Global Mapper not found at:\n{GM_EXE_PATH}")
            return
        cmd = f'"{GM_EXE_PATH}" "{shapefile_path}"'
        subprocess.Popen(cmd, shell=True)
        print(f"🗺️  Sent to Global Mapper: {shapefile_path}")
    except Exception as e:
        messagebox.showerror("Error", f"Failed to load into Global Mapper:\n{e}")

def select_files_and_run(root):
    shp_paths = filedialog.askopenfilenames(title="Select Barangay Shapefiles", filetypes=[("Shapefiles", "*.shp")], parent=root)
    if not shp_paths:
        return

    poi_path = filedialog.askopenfilename(title="Select POI Shapefile", filetypes=[("Shapefiles", "*.shp")], parent=root)
    if not poi_path:
        return

    output_dir = filedialog.askdirectory(title="Select Output Directory", parent=root)
    if not output_dir:
        return

    try:
        gdf_list = [gpd.read_file(path) for path in shp_paths]
        gdf = gpd.GeoDataFrame(pd.concat(gdf_list, ignore_index=True), crs=gdf_list[0].crs)
        poi_gdf = gpd.read_file(poi_path)

        gdf = gdf.to_crs(epsg=4326)
        poi_gdf = poi_gdf.to_crs(epsg=4326)

        categories = {
            "church": "num_church",
            "mall": "num_mall",
            "police": "num_police",
            "park": "num_park"
        }

        for field in categories.values():
            gdf[field] = 0

        minx, miny, maxx, maxy = gdf.total_bounds
        bbox_poly = box(minx - 0.05, miny - 0.05, maxx + 0.05, maxy + 0.05)

        print("📦 Downloading road network...")
        G = ox.graph_from_polygon(bbox_poly, network_type='drive')

        def add_virtual_node(G, point, node_id):
            try:
                u, v, key = ox.distance.nearest_edges(G, point[1], point[0])
                edge_data = G.get_edge_data(u, v)[key]
                line = edge_data.get('geometry', LineString([
                    (G.nodes[u]['x'], G.nodes[u]['y']),
                    (G.nodes[v]['x'], G.nodes[v]['y'])
                ]))
                proj_point = line.interpolate(line.project(Point(point[1], point[0])))
                coords = (proj_point.y, proj_point.x)
                G.add_node(node_id, x=coords[1], y=coords[0])
                d_u = geodesic((G.nodes[u]['y'], G.nodes[u]['x']), coords).meters
                d_v = geodesic((G.nodes[v]['y'], G.nodes[v]['x']), coords).meters
                G.add_edge(u, node_id, 0, length=d_u)
                G.add_edge(node_id, u, 0, length=d_u)
                G.add_edge(v, node_id, 0, length=d_v)
                G.add_edge(node_id, v, 0, length=d_v)
                return node_id
            except Exception as e:
                print(f"⚠️ Failed to add virtual node at {point}: {e}")
                return None

        for idx, row in gdf.iterrows():
            centroid = row.geometry.centroid
            lat, lon = centroid.y, centroid.x
            start_node_id = f"start_{idx}"
            start_node = add_virtual_node(G, (lat, lon), start_node_id)

            for poi_type, field_name in categories.items():
                count = 0
                poi_subset = poi_gdf[poi_gdf["fclass"].str.lower() == poi_type].copy()
                if poi_subset.empty:
                    continue

                # Filter using ~200m buffer
                bbox = centroid.buffer(0.002).bounds
                poi_subset = poi_subset.cx[bbox[0]:bbox[2], bbox[1]:bbox[3]]

                for pidx, poi in poi_subset.iterrows():
                    poi_lat, poi_lon = poi.geometry.y, poi.geometry.x
                    fallback = geodesic((lat, lon), (poi_lat, poi_lon)).meters
                    if fallback > 250:
                        continue

                    end_node_id = f"end_{idx}_{field_name}_{pidx}"
                    end_node = add_virtual_node(G, (poi_lat, poi_lon), end_node_id)
                    try:
                        if start_node and end_node and nx.has_path(G, start_node, end_node):
                            length, _ = nx.bidirectional_dijkstra(G, start_node, end_node, weight='length')
                            if length <= 200:
                                print(f"✅ Feature {idx} → {poi_type} {pidx}: {length:.2f} m")
                                count += 1
                        if end_node_id in G:
                            G.remove_node(end_node_id)
                    except:
                        continue

                gdf.at[idx, field_name] = count
                print(f"📊 Feature {idx}: {count} {poi_type}(s) within 200m\n")

            if start_node_id in G:
                G.remove_node(start_node_id)

        output_path = os.path.join(output_dir, "barangays_with_poi_counts.shp")
        gdf.to_file(output_path)
        print(f"✅ Output saved to: {output_path}")
        load_into_global_mapper(output_path)
        messagebox.showinfo("Success", f"✅ Processing complete!\nSaved to:\n{output_path}", parent=root)

    except Exception as e:
        messagebox.showerror("Error", f"Processing failed:\n{e}", parent=root)

def launch_gui():
    root = tk.Tk()
    root.title("POI Proximity Counter")
    if os.path.exists(ICON_PATH):
        try:
            root.iconbitmap(ICON_PATH)
        except Exception as e:
            print(f"⚠ Failed to set custom icon: {e}")
    else:
        print(f"⚠ Icon file not found at: {ICON_PATH}")
    root.withdraw()
    select_files_and_run(root)

if __name__ == "__main__":
    launch_gui()
