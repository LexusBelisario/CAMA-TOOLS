import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import geopandas as gpd
import pandas as pd
import numpy as np
import os
import uuid
import joblib
from sklearn.preprocessing import StandardScaler
import matplotlib.pyplot as plt
import seaborn as sns
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from libpysal.weights import Queen
from spreg import ML_Lag  # Using lag_q=True for Durbin
import ctypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

class SDMApp:
    def __init__(self, master):
        self.master = master
        master.title("GeoStatistical Tool — SDM")

        frame = tk.Frame(master, padx=10, pady=10)
        frame.pack()

        self.load_button = tk.Button(frame, text="Load Shapefile", command=self.load_shapefile, width=30)
        self.load_button.grid(row=0, column=0, columnspan=2, pady=10)

        self.dep_combobox = ttk.Combobox(frame, state="readonly", width=30)
        self.dep_combobox.grid(row=1, column=0, columnspan=2, pady=10)
        self.dep_combobox.set("Select Dependent Variable")

        tk.Label(frame, text="Select Independent Variables").grid(row=2, column=0, pady=5, sticky="w")
        self.indep_listbox = tk.Listbox(frame, selectmode=tk.MULTIPLE, width=30, height=10)
        self.indep_listbox.grid(row=3, column=0, columnspan=2, pady=5)

        self.train_button = tk.Button(frame, text="Train SDM", command=self.train_model, width=30)
        self.train_button.grid(row=4, column=0, pady=15)

        self.predict_button = tk.Button(frame, text="Run Saved SDM", command=self.predict_with_saved_model, width=30)
        self.predict_button.grid(row=4, column=1, pady=15)

    def load_shapefile(self):
        file_path = filedialog.askopenfilename(filetypes=[("Shapefiles", "*.shp")])
        if not file_path:
            return
        self.file_path = file_path
        self.gdf = gpd.read_file(file_path)

        if not self.gdf.crs or not self.gdf.crs.is_projected:
            self.gdf = self.gdf.to_crs(epsg=3857)

        numeric_columns = self.gdf.select_dtypes(include=[np.number]).columns.tolist()
        if not numeric_columns:
            messagebox.showerror("Error", "No numeric fields found in shapefile.")
            return

        self.dep_combobox['values'] = numeric_columns
        self.dep_combobox.set("Select Dependent Variable")
        self.indep_listbox.delete(0, tk.END)
        for col in numeric_columns:
            self.indep_listbox.insert(tk.END, col)

    def train_model(self):
        if not hasattr(self, 'gdf'):
            messagebox.showerror("Error", "Please load a shapefile first.")
            return

        dep_var = self.dep_combobox.get()
        indep_indices = self.indep_listbox.curselection()

        if dep_var == 'Select Dependent Variable' or not dep_var:
            messagebox.showerror("Selection Error", "Please select a dependent variable.")
            return
        if not indep_indices:
            messagebox.showerror("Selection Error", "Please select independent variables.")
            return

        indep_vars = [self.indep_listbox.get(i) for i in indep_indices]
        required_cols = [dep_var] + indep_vars
        gdf_clean = self.gdf.dropna(subset=required_cols).copy()
        gdf_clean = gdf_clean[gdf_clean[dep_var] != 0]
        gdf_clean = gdf_clean[~gdf_clean.geometry.is_empty & gdf_clean.geometry.notnull()].copy()

        if not gdf_clean.geometry.geom_type.isin(['Polygon', 'MultiPolygon', 'Point']).all():
            gdf_clean['geometry'] = gdf_clean.geometry.centroid

        y = gdf_clean[[dep_var]].values
        X_df = gdf_clean[indep_vars].apply(pd.to_numeric, errors='coerce')
        X_df = X_df.dropna(axis=0, how='any')
        gdf_clean = gdf_clean.loc[X_df.index].copy()

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X_df.values)

        try:
            w = Queen.from_dataframe(gdf_clean, use_index=False)
            w.transform = 'r'
            # Spatial Durbin via ML_Lag with lagged X terms enabled
            sdm = ML_Lag(y, X_scaled, w=w, name_y=dep_var, name_x=indep_vars, lag_q=True)
            gdf_clean['Predicted'] = sdm.predy.flatten()
            gdf_clean['Residuals'] = sdm.u.flatten()

            result_info = {
                'type': 'SDM',
                'model': sdm,
                'w': w,
                'scaler': scaler,
                'dep_var': dep_var,
                'indep_vars': indep_vars
            }
        except Exception as e:
            messagebox.showerror("Model Error", str(e))
            return

        self.gdf['Predicted'] = np.nan
        self.gdf['Residuals'] = np.nan
        self.gdf.loc[gdf_clean.index, 'Predicted'] = gdf_clean['Predicted']
        self.gdf.loc[gdf_clean.index, 'Residuals'] = gdf_clean['Residuals']

        model_save_path = filedialog.asksaveasfilename(defaultextension=".joblib",
                                                       filetypes=[("Joblib files", "*.joblib")],
                                                       title="Save SDM Model As")
        if not model_save_path:
            return
        joblib.dump(result_info, model_save_path)

        shapefile_path = os.path.splitext(self.file_path)[0] + "_sdm_predicted.shp"
        self.gdf.to_file(shapefile_path)

        report_path = filedialog.asksaveasfilename(defaultextension=".pdf",
                                                   filetypes=[("PDF files", "*.pdf")],
                                                   title="Save SDM Report As")
        if not report_path:
            return
        self.generate_report(result_info, gdf_clean, report_path)
        messagebox.showinfo("Success",
                            f"SDM complete.\nModel: {model_save_path}\nShapefile: {shapefile_path}\nReport: {report_path}")

    def predict_with_saved_model(self):
        model_path = filedialog.askopenfilename(filetypes=[("Joblib files", "*.joblib")], title="Select Saved SDM Model")
        if not model_path:
            return
        data = joblib.load(model_path)

        dep_var = data['dep_var']
        indep_vars = data['indep_vars']
        scaler = data['scaler']

        shp_path = filedialog.askopenfilename(filetypes=[("Shapefiles", "*.shp")], title="Select Shapefile for Prediction")
        if not shp_path:
            return
        gdf = gpd.read_file(shp_path)
        if not gdf.crs or not gdf.crs.is_projected:
            gdf = gdf.to_crs(epsg=3857)
        gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notnull()].copy()

        missing = [f for f in indep_vars if f not in gdf.columns]
        if missing:
            messagebox.showerror("Error", f"Missing variables in shapefile: {', '.join(missing)}")
            return

        try:
            X_raw = gdf[indep_vars].apply(pd.to_numeric, errors='coerce')
            mask_valid = ~X_raw.isnull().any(axis=1) & ~(X_raw == 0).any(axis=1)
            gdf = gdf.loc[mask_valid].copy()
            X_array = X_raw.loc[mask_valid].values
            if np.linalg.matrix_rank(X_array) < X_array.shape[1]:
                messagebox.showerror("Prediction Error", "Matrix is singular due to multicollinearity or duplicates.")
                return

            X_scaled = scaler.transform(X_array)

            w = Queen.from_dataframe(gdf, use_index=False)
            w.transform = 'r'
            sdm = ML_Lag(np.zeros((len(X_scaled), 1)), X_scaled, w=w, name_y=dep_var, name_x=indep_vars, lag_q=True)
            gdf['Prediction'] = sdm.predy.flatten()

            out_path = os.path.splitext(shp_path)[0] + "_sdm_predicted.shp"
            gdf.to_file(out_path)
            messagebox.showinfo("Success", f"SDM prediction completed.\nSaved to: {out_path}")
        except Exception as e:
            messagebox.showerror("Prediction Error", str(e))

    def generate_report(self, data, gdf_clean, report_path):
        width, height = A4
        c = canvas.Canvas(report_path, pagesize=A4)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(50, height - 50, "SDM Regression Report")

        c.setFont("Helvetica", 12)
        y_pos = height - 80

        model = data['model']
        if hasattr(model, "aic"):
            c.drawString(50, y_pos, f"AIC: {model.aic:.2f}")
            y_pos -= 20
        if hasattr(model, "pr2"):
            c.drawString(50, y_pos, f"Pseudo R²: {model.pr2:.3f}")
            y_pos -= 20

        betas = model.betas.flatten()[1:]
        x_labels = data.get('indep_vars', [])
        if len(x_labels) < len(betas):
            x_labels += [f"ExtraCoef_{i}" for i in range(len(x_labels), len(betas))]

        y_pos -= 20
        c.drawString(50, y_pos, "Mean Coefficients:")
        y_pos -= 20
        for i, coef in enumerate(betas):
            c.drawString(60, y_pos, f"{x_labels[i]}: {coef:.4f}")
            y_pos -= 18

        coef_img = f"coef_plot_{uuid.uuid4().hex}.png"
        plt.figure(figsize=(6, 3), dpi=100)
        sns.barplot(x=x_labels, y=betas)
        plt.xticks(rotation=45)
        plt.title('Feature Coefficients')
        plt.tight_layout()
        plt.savefig(coef_img)
        plt.close()
        y_pos -= 160
        c.drawImage(ImageReader(coef_img), 50, y_pos, width=500, height=130)
        os.remove(coef_img)

        resid_img = f"resid_plot_{uuid.uuid4().hex}.png"
        fig, ax = plt.subplots(figsize=(6, 4))
        gdf_clean.plot(column='Residuals', ax=ax, cmap='coolwarm', edgecolor='black', legend=True)
        plt.title("Residuals Map")
        plt.axis('off')
        plt.tight_layout()
        plt.savefig(resid_img)
        plt.close()
        y_pos -= 170
        c.drawImage(ImageReader(resid_img), 50, y_pos, width=500, height=160)
        os.remove(resid_img)

        c.save()

def main():
    root = tk.Tk()
    app = SDMApp(root)
    root.mainloop()
    return 0   # ✅ clean exit status for dispatcher

if __name__ == "__main__":
    main()