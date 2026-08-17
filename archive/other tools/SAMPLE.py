import tkinter as tk
import globalmapper as gm
import os


def run_landslide_hazard_script():
    script = (
        "GLOBAL_MAPPER_SCRIPT VERSION=1.00\n"
        "\n"
        "DEFINE_LAYER_STYLE NAME=\"LandslideHazardStyle\" TYPE=AREA "
        "FILENAME=\"C:\\Users\\igdi\\Documents\\aNat\\CLN_LANDSLIDE HAZARD MAP\\Landslidehazardstyle.gm_layer_style\"\n"
        "END_DEFINE_LAYER_STYLE\n"
        "\n"
        "IMPORT FILENAME=\"C:\\Users\\igdi\\Documents\\aNat\\CLN_LANDSLIDE HAZARD MAP\\CLN_LANDSLIDE HAZARD MAP.shp\" "
        "TYPE=SHAPEFILE AREA_STYLE_NAME=\"LandslideHazardStyle\"\n"
        "\n"
        "IMPORT FILENAME=\"C:\\Users\\igdi\\Documents\\aNat\\CLN_PER BRGY (with elevation and slope)\\07_IMOK.shp\" "
        "TYPE=SHAPEFILE AREA_STYLE_NAME=\"LandslideHazardStyle\"\n"
        "\n"
        "SET_LAYER_OPTIONS FILENAME=\"C:\\Users\\igdi\\Documents\\aNat\\CLN_LANDSLIDE HAZARD MAP\\CLN_LANDSLIDE HAZARD MAP.shp\" "
        "AREA_STYLE_NAME=\"LandslideHazardStyle\"\n"
        "\n"
        "COPY_ATTRS "
        "LAYER1_FILENAME=\"C:\\Users\\igdi\\Documents\\aNat\\CLN_LANDSLIDE HAZARD MAP\\CLN_LANDSLIDE HAZARD MAP.shp\" "
        "FROM_TYPE=AREAS "
        "LAYER2_FILENAME=\"C:\\Users\\igdi\\Documents\\aNat\\CLN_PER BRGY (with elevation and slope)\\07_IMOK.shp\" "
        "TO_TYPE=AREAS ATTR_TO_COPY=\"RATING\" MULTI_AREA=ALL AREA_COVERAGE=CENTROID\n"
        "\n"
        "EXPORT_VECTOR "
        "FILENAME=\"C:\\GUInla\\Button\\landslidehazard.shp\" "
        "EXPORT_LAYER=\"C:\\Users\\igdi\\Documents\\aNat\\CLN_PER BRGY (with elevation and slope)\\07_IMOK.shp\" "
        "TYPE=SHAPEFILE INC_STYLE_ATTRS=YES\n"
        "\n"
        "SAVE_WORKSPACE FILENAME=\"C:\\GUInla\\Button\\landslide_workspace.gmw\"\n"
    )


    err, output, outputCount = gm.RunScript(script, 0x0, 0x0)
    if err != 0:
        label_status.config(text=f"Error: {err}", fg="red")
    else:
        label_status.config(text="Landslide Hazard Mapping Completed!", fg="green")
       
        # Open the saved workspace in Global Mapper
        workspace_file = "C:\\GUInla\\Button\\landslide_workspace.gmw"
        if os.path.exists(workspace_file):
            os.startfile(workspace_file)
        else:
            label_status.config(text="Workspace file not found.", fg="red")


#------------------------------------------------------------------------------------------------------------------------------
def calculate_slope_script():
    script = (


        "GLOBAL_MAPPER_SCRIPT VERSION=1.00\n"
         "\n"
        "DEFINE_LAYER_STYLE NAME=\"SlopeStyle\" TYPE=AREA "
        "FILENAME=\"C:\\GUInla\\DTM\\CLN_SLOPE.gm_layer_style\"\n"
        "END_DEFINE_LAYER_STYLE\n"
        "\n"
        "IMPORT FILENAME=\"C:\\GUInla\\DTM\\CLN_ELEV_SLOPE.shp\" "
        "TYPE=SHAPEFILE AREA_STYLE_NAME=\"SlopeStyle\"\n"
        "\n"
        "IMPORT FILENAME=\"C:\\GUInla\\DTM\\LIMAO.shp\" "
        "TYPE=SHAPEFILE AREA_STYLE_NAME=\"SlopeStyle\"\n"
        "\n"
        "COPY_ATTRS "
        "LAYER1_FILENAME=\"C:\\GUInla\\DTM\\CLN_ELEV_SLOPE.shp\" "
        "FROM_TYPE=AREAS "
        "LAYER2_FILENAME=\"C:\\GUInla\\DTM\\LIMAO.shp\" "
        "TO_TYPE=AREAS ATTR_TO_COPY=\"SLOPE\" MULTI_AREA=ALL AREA_COVERAGE=CENTROID\n"
        "\n"
        "EXPORT_VECTOR "
        "FILENAME=\"C:\\GUInla\\Button\\limao_slope.shp\" "
        "EXPORT_LAYER=\"C:\\GUInla\\DTM\\LIMAO.shp\" "
        "TYPE=SHAPEFILE INC_STYLE_ATTRS=YES\n"
        "\n"
         "SAVE_WORKSPACE FILENAME=\"C:\\GUInla\\Button\\calculate_slope.gmw\"\n"
       


    )


    err, output, outputCount = gm.RunScript(script, 0x0, 0x0)
    if err != 0:
        label_status.config(text=f"Error: {err}", fg="red")
    else:
        label_status.config(text="Calculate Slope Completed!", fg="green")
       
        # Open the saved workspace in Global Mapper
        workspace_file = "C:\\GUInla\\Button\\calculate_slope.gmw"
        if os.path.exists(workspace_file):
            os.startfile(workspace_file)
        else:
            label_status.config(text="Workspace file not found.", fg="red")




# Create Tkinter window
root = tk.Tk()
root.title("Global Mapper Tool")
root.geometry("300x350")


button_run = tk.Button(root, text="Run Landslide Hazard", command=run_landslide_hazard_script, font=("Arial", 12), bg="lightblue")
button_run.pack(pady=10)


button_run = tk.Button(root, text="Calculate Slope", command=calculate_slope_script, font=("Arial", 12), bg="lightgreen")
button_run.pack(pady=10)


label_status = tk.Label(root, text="", font=("Arial", 10))
label_status.pack()


root.mainloop()