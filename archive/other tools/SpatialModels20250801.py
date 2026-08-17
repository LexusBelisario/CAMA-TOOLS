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
from spreg import ML_Lag
from mgwr.gwr import GWR
from mgwr.sel_bw import Sel_BW
from statsmodels.stats.outliers_influence import variance_inflation_factor
import ctypes

# Enable high DPI awareness on Windows
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

class SpatialModelApp:
    def __init__(self, master):
        self.master = master
        master.title("GeoStatistical Tool")

        frame = tk.Frame(master, padx=10, pady=10)
        frame.pack()

        self.load_button = tk.Button(frame, text="Load Shapefile", command=self.load_shapefile, width=30)
        self.load_button.grid(row=0, column=0, columnspan=2, pady=10)

        self.dep_combobox = ttk.Combobox(frame, state="readonly", width=30)
        self.dep_combobox.grid(row=1, column=0, columnspan=2, pady=10)
        self.dep_combobox.set("Select Dependent Variable")

        self.indep_label = tk.Label(frame, text="Select Independent Variables")
        self.indep_label.grid(row=2, column=0, pady=5)

        self.indep_listbox = tk.Listbox(frame, selectmode=tk.MULTIPLE, width=30, height=10)
        self.indep_listbox.grid(row=3, column=0, columnspan=2, pady=5)

        self.train_button = tk.Button(frame, text="Train Model", command=self.train_model, width=30)
        self.train_button.grid(row=4, column=0, pady=15)

        self.predict_button = tk.Button(frame, text="Run Saved Model", command=self.predict_with_saved_model, width=30)
        self.predict_button.grid(row=4, column=1, pady=15)

        # Model type selection
        self.model_type = tk.StringVar(value="GWR")
        tk.Label(frame, text="Select Model Type:").grid(row=5, column=0, pady=(10, 0), sticky='w')
        tk.Radiobutton(frame, text="OLS", variable=self.model_type, value="OLS").grid(row=6, column=0, sticky='w')
        tk.Radiobutton(frame, text="SLM", variable=self.model_type, value="SLM").grid(row=6, column=1, sticky='w')
        tk.Radiobutton(frame, text="SDM", variable=self.model_type, value="SDM").grid(row=7, column=0, sticky='w')
        tk.Radiobutton(frame, text="GWR", variable=self.model_type, value="GWR").grid(row=7, column=1, sticky='w')

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
        X = gdf_clean[indep_vars].values
        coords = np.column_stack((gdf_clean.geometry.centroid.x, gdf_clean.geometry.centroid.y))

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)
        model_type = self.model_type.get()

        model = None
        result_info = {}
        try:
            if model_type == "GWR":
                bw = Sel_BW(coords, y, X_scaled).search()
                model = GWR(coords, y, X_scaled, bw)
                results = model.fit()
                gdf_clean['Predicted'] = results.predy.flatten()
                gdf_clean['Residuals'] = results.resid_response.flatten()
                result_info = {
                    'model': model,
                    'results': results,
                    'bw': bw,
                    'type': 'GWR',
                    'scaler': scaler,
                    'dep_var': dep_var,
                    'indep_vars': indep_vars
                }
            elif model_type == "SDM":
                w = Queen.from_dataframe(gdf_clean, use_index=False)
                w.transform = 'r'
                sdm = ML_Lag(y, X_scaled, w=w, name_y=dep_var, name_x=indep_vars, lag_q=True)
                gdf_clean['Predicted'] = sdm.predy
                gdf_clean['Residuals'] = sdm.u
                result_info = {
                    'model': sdm,
                    'w': w,
                    'type': 'SDM',
                    'scaler': scaler,
                    'dep_var': dep_var,
                    'indep_vars': indep_vars
                }
            elif model_type == "OLS":
                from spreg import OLS
                ols = OLS(y, X_scaled, name_y=dep_var, name_x=indep_vars)
                gdf_clean['Predicted'] = ols.predy
                gdf_clean['Residuals'] = ols.u
                result_info = {
                    'model': ols,
                    'type': 'OLS',
                    'scaler': scaler,
                    'dep_var': dep_var,
                    'indep_vars': indep_vars
                }

            elif model_type == "SLM":
                from spreg import ML_Lag
                w = Queen.from_dataframe(gdf_clean, use_index=False)
                w.transform = 'r'
                slm = ML_Lag(y, X_scaled, w=w, name_y=dep_var, name_x=indep_vars)
                gdf_clean['Predicted'] = slm.predy
                gdf_clean['Residuals'] = slm.u
                result_info = {
                    'model': slm,
                    'w': w,
                    'type': 'SLM',
                    'scaler': scaler,
                    'dep_var': dep_var,
                    'indep_vars': indep_vars
                }

            else:
                messagebox.showerror("Model Error", "Unsupported model selected.")
                return
        except Exception as e:
            messagebox.showerror("Model Error", str(e))
            return

        self.gdf['Predicted'] = np.nan
        self.gdf['Residuals'] = np.nan
        self.gdf.loc[gdf_clean.index, 'Predicted'] = gdf_clean['Predicted']
        self.gdf.loc[gdf_clean.index, 'Residuals'] = gdf_clean['Residuals']

        model_save_path = filedialog.asksaveasfilename(defaultextension=".joblib", filetypes=[("Joblib files", "*.joblib")], title="Save Model As")
        if not model_save_path:
            return
        joblib.dump(result_info, model_save_path)

        shapefile_path = os.path.splitext(self.file_path)[0] + f"_{model_type.lower()}_predicted.shp"
        self.gdf.to_file(shapefile_path)

        report_path = filedialog.asksaveasfilename(defaultextension=".pdf", filetypes=[("PDF files", "*.pdf")], title="Save Report As")
        if not report_path:
            return
        self.generate_report(result_info, gdf_clean, report_path)
        messagebox.showinfo("Success", f"{model_type} Model complete.\nModel: {model_save_path}\nShapefile: {shapefile_path}\nReport: {report_path}")

    def predict_with_saved_model(self):
        model_path = filedialog.askopenfilename(filetypes=[("Joblib files", "*.joblib")], title="Select Saved Model")
        if not model_path:
            return
        data = joblib.load(model_path)

        model_type = data.get('type')
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
        missing_feats = [f for f in indep_vars if f not in gdf.columns]
        if missing_feats:
            messagebox.showerror("Error", f"Missing variables in shapefile: {', '.join(missing_feats)}")
            return

        try:
            X_raw = gdf[indep_vars].apply(pd.to_numeric, errors='coerce')
            mask_valid = ~X_raw.isnull().any(axis=1) & ~(X_raw == 0).any(axis=1)
            gdf = gdf.loc[mask_valid].copy()
            X_clean = X_raw.loc[mask_valid]
            X_array = X_clean.values
            if np.linalg.matrix_rank(X_array) < X_array.shape[1]:
                messagebox.showerror("Prediction Error", "Matrix is singular due to multicollinearity or duplicate variables.\nPlease remove or modify the input data.")
                return

            X_scaled = scaler.transform(X_array)

            if not gdf.geometry.geom_type.isin(['Point', 'Polygon', 'MultiPolygon']).all():
                gdf['geometry'] = gdf.geometry.centroid
            coords = np.column_stack((gdf.geometry.centroid.x, gdf.geometry.centroid.y))

            if model_type == "GWR":
                bw = data['bw']
                gwr_model = GWR(coords, np.zeros((len(coords), 1)), X_scaled, bw)
                results = gwr_model.predict(coords, X_scaled)
                gdf['Prediction'] = results.predy.flatten()

            elif model_type == "SDM":
                w = Queen.from_dataframe(gdf, use_index=False)
                w.transform = 'r'
                sdm = ML_Lag(np.zeros((len(X_scaled), 1)), X_scaled, w=w, lag_q=True)
                gdf['Prediction'] = sdm.predy

            elif model_type == "OLS":
                from spreg import OLS
                ols = OLS(np.zeros((len(X_scaled), 1)), X_scaled, name_y=dep_var, name_x=indep_vars)
                gdf['Prediction'] = ols.predy

            elif model_type == "SLM":
                from spreg import ML_Lag
                w = Queen.from_dataframe(gdf, use_index=False)
                w.transform = 'r'
                slm = ML_Lag(np.zeros((len(X_scaled), 1)), X_scaled, w=w, name_y=dep_var, name_x=indep_vars)
                gdf['Prediction'] = slm.predy

            else:
                messagebox.showerror("Unsupported", "Only GWR and SDM predictions are supported.")
                return

            out_path = os.path.splitext(shp_path)[0] + f"_{model_type.lower()}_predicted.shp"
            gdf.to_file(out_path)
            messagebox.showinfo("Success", f"{model_type} prediction completed.\nSaved to: {out_path}")
        except Exception as e:
            messagebox.showerror("Prediction Error", str(e))


    def generate_report(self, data, gdf_clean, report_path):
        width, height = A4
        c = canvas.Canvas(report_path, pagesize=A4)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(50, height - 50, f"{data['type']} Regression Report")

        c.setFont("Helvetica", 12)
        y_pos = height - 80

        if data['type'] == "GWR":
            results = data['results']
            c.drawString(50, y_pos, f"AICc: {results.aicc:.2f}")
            y_pos -= 20
            c.drawString(50, y_pos, f"R²: {results.R2:.3f}")
            y_pos -= 20
            c.drawString(50, y_pos, f"Adj. R²: {results.adj_R2:.3f}")
            y_pos -= 20
            c.drawString(50, y_pos, f"Bandwidth: {data['bw']}")
            y_pos -= 20
            coef_means = results.params.mean(axis=0)[1:]
        elif data['type'] in ["SDM", "SLM", "OLS"]:
            model = data['model']
            if hasattr(model, "aic"):
                c.drawString(50, y_pos, f"AIC: {model.aic:.2f}")
                y_pos -= 20
            if hasattr(model, "pr2"):
                c.drawString(50, y_pos, f"Pseudo R²: {model.pr2:.3f}")
                y_pos -= 20
            coef_means = model.betas.flatten()[1:]

        y_pos -= 20
        c.drawString(50, y_pos, "Mean Coefficients:")
        y_pos -= 20
        
        x_labels = data.get('indep_vars', [])
        if len(x_labels) < len(coef_means):
            x_labels += [f"ExtraCoef_{i}" for i in range(len(x_labels), len(coef_means))]

        for i, coef in enumerate(coef_means):
            var_name = x_labels[i]
            c.drawString(60, y_pos, f"{var_name}: {coef:.4f}")
            y_pos -= 20

        coef_img = f"coef_plot_{uuid.uuid4().hex}.png"
        plt.figure(figsize=(6, 3), dpi=100)

        # Ensure x-labels match coef length
        coef_len = len(coef_means)
        x_labels = data.get('indep_vars', [])
        if len(x_labels) < coef_len:
            x_labels += [f"ExtraCoef_{i}" for i in range(len(x_labels), coef_len)]

        sns.barplot(x=x_labels, y=coef_means)

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

if __name__ == "__main__":
    root = tk.Tk()
    app = SpatialModelApp(root)
    root.mainloop()
