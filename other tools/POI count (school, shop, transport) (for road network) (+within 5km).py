import os
import tkinter as tk
from tkinter import filedialog, messagebox
import geopandas as gpd
import networkx as nx
from shapely.geometry import Point, LineString
from geopy.distance import geodesic
import pandas as pd
import subprocess
from scipy.spatial import cKDTree
import numpy as np

ICON_PATH = r"D:/2025_PROJECTS/BLGF-GM_TEST/FOR TESTING/DCS_CODES/BLGF.ico"
GM_EXE_PATH = r"C:\Program Files\GlobalMapper26.1_64bit\global_mapper.exe"

def load_into_global_mapper(shapefile_path):
    try:
        if not os.path.exists(GM_EXE_PATH):
            messagebox.showerror("Error", f"Global Mapper not found at:\n{GM_EXE_PATH}")
            return
        cmd = f'"{GM_EXE_PATH}" "{shapefile_path}"'
        subprocess.Popen(cmd, shell=True)
    except Exception as e:
        messagebox.showerror("Error", f"Failed to load into Global Mapper:\n{e}")

def build_graph_from_roads(road_gdf):
    G = nx.Graph()
    for idx, row in road_gdf.iterrows():
        geom = row.geometry
        if isinstance(geom, LineString):
            coords = list(geom.coords)
            for i in range(len(coords) - 1):
                p1, p2 = coords[i], coords[i + 1]
                dist = geodesic((p1[1], p1[0]), (p2[1], p2[0])).meters
                G.add_node(p1, x=p1[0], y=p1[1])
                G.add_node(p2, x=p2[0], y=p2[1])
                G.add_edge(p1, p2, length=dist, geom=LineString([p1, p2]))
    return G

def add_virtual_node_on_line(G, line, point, label):
    try:
        projected_distance = line.project(point)
        projected_point = line.interpolate(projected_distance)
        coords = (projected_point.x, projected_point.y)

        coords_list = list(line.coords)
        min_seg = None
        min_dist = float('inf')

        for i in range(len(coords_list) - 1):
            seg = LineString([coords_list[i], coords_list[i + 1]])
            dist = seg.distance(projected_point)
            if dist < min_dist:
                min_dist = dist
                min_seg = seg

        p1, p2 = list(min_seg.coords)
        d1 = geodesic((projected_point.y, projected_point.x), (p1[1], p1[0])).meters
        d2 = geodesic((projected_point.y, projected_point.x), (p2[1], p2[0])).meters

        G.add_node(coords, x=coords[0], y=coords[1])
        G.add_edge(p1, coords, length=d1, geom=LineString([p1, coords]))
        G.add_edge(coords, p1, length=d1, geom=LineString([coords, p1]))
        G.add_edge(p2, coords, length=d2, geom=LineString([p2, coords]))
        G.add_edge(coords, p2, length=d2, geom=LineString([coords, p2]))

        return coords
    except Exception as e:
        print(f"⚠️ Failed to add virtual node '{label}': {e}")
        return None

def select_files_and_run(root):
    road_path = filedialog.askopenfilename(title="Select Road Shapefile", filetypes=[("Shapefiles", "*.shp")], parent=root)
    if not road_path:
        return

    poi_path = filedialog.askopenfilename(title="Select POI Shapefile (type = school/shop/transport)", filetypes=[("Shapefiles", "*.shp")], parent=root)
    if not poi_path:
        return

    output_dir = filedialog.askdirectory(title="Select Output Directory", parent=root)
    if not output_dir:
        return

    try:
        road_gdf = gpd.read_file(road_path).to_crs(epsg=4326)
        poi_gdf = gpd.read_file(poi_path).to_crs(epsg=4326)

        if "name" not in road_gdf.columns:
            road_gdf["name"] = [f"Road_{i}" for i in range(len(road_gdf))]
            print("ℹ️ Added default 'name' field to roads.")

        print("🔧 Building road network graph...")
        G = build_graph_from_roads(road_gdf)

        projected_epsg = 3857
        road_proj = road_gdf.to_crs(epsg=projected_epsg)
        poi_proj = poi_gdf.to_crs(epsg=3857)
        poi_proj["x"] = poi_proj.geometry.x
        poi_proj["y"] = poi_proj.geometry.y

        types = {
            "school": "5km_school",
            "shop": "5km_shop",
            "transport": "5km_tp"
        }

        for field in types.values():
            road_gdf[field] = 0

        poi_trees = {}
        for typ in types:
            sub = poi_proj[poi_proj["fclass"].str.lower() == typ]
            if not sub.empty:
                poi_trees[typ] = (cKDTree(np.c_[sub["x"], sub["y"]]), sub)

        for idx, row in road_gdf.iterrows():
            line = row.geometry
            road_name = row["name"]
            print(f"\n🛠️ Processing Road: {road_name} (Index {idx})")

            if not isinstance(line, LineString):
                print("⚠️ Skipped: Not a LineString")
                continue

            midpoint = line.interpolate(line.length / 2)
            mid_pt = Point(midpoint.x, midpoint.y)
            start_node = add_virtual_node_on_line(G, line, mid_pt, f"{road_name}_mid")

            if start_node is None:
                print(f"⚠️ Skipped {road_name}: failed to add midpoint node.")
                continue

            mid_proj = gpd.GeoSeries([mid_pt], crs="EPSG:4326").to_crs(epsg=3857).iloc[0]
            mid_x, mid_y = mid_proj.x, mid_proj.y

            for typ, field in types.items():
                if typ not in poi_trees:
                    print(f"   ↪ {typ.upper()} POIs within 5km: 0")
                    continue

                tree, sub = poi_trees[typ]
                indices = tree.query_ball_point([mid_x, mid_y], r=5000)
                if not indices:
                    print(f"   ↪ {typ.upper()} POIs within 5km: 0")
                    continue

                count = 0
                nearby = sub.iloc[indices]

                for _, poi_row in nearby.iterrows():
                    poi_geom = poi_row.geometry
                    poi_idx = poi_row.name
                    poi_proj_geom = poi_row.geometry
                    closest_idx = road_proj.distance(poi_proj_geom).idxmin()
                    line_for_poi = road_gdf.loc[closest_idx].geometry
                    end_node = add_virtual_node_on_line(G, line_for_poi, poi_geom, f"{road_name}_{typ}_poi")

                    try:
                        if end_node and nx.has_path(G, start_node, end_node):
                            dist = nx.shortest_path_length(G, source=start_node, target=end_node, weight='length')
                            if dist <= 5000:
                                count += 1
                    except:
                        continue

                road_gdf.at[idx, field] = count
                print(f"   ↪ {typ.upper()} POIs within 5km: {count}")

        output_path = os.path.join(output_dir, "road_lines_with_5km_poi_counts.shp")
        road_gdf.to_file(output_path)
        print(f"\n✅ Output saved to: {output_path}")

        load_into_global_mapper(output_path)
        messagebox.showinfo("Success", f"Processing complete!\nSaved to:\n{output_path}", parent=root)

    except Exception as e:
        messagebox.showerror("Error", f"Processing failed:\n{e}", parent=root)

def launch_gui():
    root = tk.Tk()
    root.title("Count POIs within 5km of Road Midpoints")
    if os.path.exists(ICON_PATH):
        try:
            root.iconbitmap(ICON_PATH)
        except Exception as e:
            print(f"⚠️ Failed to set custom icon: {e}")
    root.withdraw()
    select_files_and_run(root)

if __name__ == "__main__":
    launch_gui()
