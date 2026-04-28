import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import geopandas as gpd
import pandas as pd
import numpy as np
from mgwr.gwr import GWR
from sklearn.preprocessing import StandardScaler
import joblib
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
import matplotlib.pyplot as plt
import seaborn as sns
import os
import warnings
warnings.filterwarnings("ignore", category=UserWarning)

class GWRApp:
    def __init__(self, master):
        self.master = master
        master.title("Geographically Weighted Regression (GWR)")

        self.load_button = tk.Button(master, text="Load Shapefile", command=self.load_shapefile, width=30)
        self.load_button.grid(row=0, column=0, columnspan=2, padx=10, pady=10)

        self.dep_combobox = ttk.Combobox(master, state="readonly", width=30)
        self.dep_combobox.grid(row=1, column=0, columnspan=2, padx=10, pady=10)
        self.dep_combobox.set("Select Dependent Variable")

        self.indep_label = tk.Label(master, text="Select Independent Variables")
        self.indep_label.grid(row=2, column=0, padx=10, pady=5)

        self.indep_listbox = tk.Listbox(master, selectmode=tk.MULTIPLE, width=30, height=10)
        self.indep_listbox.grid(row=3, column=0, columnspan=2, padx=10, pady=5)

        self.train_button = tk.Button(master, text="Train GWR Model", command=self.train_gwr, width=30)
        self.train_button.grid(row=4, column=0, padx=10, pady=15)

        self.predict_button = tk.Button(master, text="Run Saved GWR Model", command=self.predict_with_saved_model, width=30)
        self.predict_button.grid(row=4, column=1, padx=10, pady=15)

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

    def train_gwr(self):
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

        if gdf_clean.empty:
            messagebox.showerror("Data Error", "No valid records available after removing missing values.")
            return

        if not gdf_clean.geometry.geom_type.isin(['Point']).all():
            gdf_clean['geometry'] = gdf_clean.geometry.centroid

        coords = np.column_stack((gdf_clean.geometry.x, gdf_clean.geometry.y))
        y = gdf_clean[[dep_var]].values
        X = gdf_clean[indep_vars].values

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        from mgwr.sel_bw import Sel_BW
        bw = Sel_BW(coords, y, X_scaled).search()

        model = GWR(coords, y, X_scaled, bw)
        results = model.fit()

        gdf_clean['Predicted'] = results.predy.flatten()
        gdf_clean['Residuals'] = results.resid_response.flatten()

        self.gdf['Predicted'] = np.nan
        self.gdf['Residuals'] = np.nan
        self.gdf.loc[gdf_clean.index, 'Predicted'] = gdf_clean['Predicted']
        self.gdf.loc[gdf_clean.index, 'Residuals'] = gdf_clean['Residuals']

        dropped = len(self.gdf) - len(gdf_clean)
        if dropped > 0:
            messagebox.showwarning("Missing Data", f"{dropped} records were dropped due to missing values.")

        model_save_path = filedialog.asksaveasfilename(defaultextension=".joblib", filetypes=[("Joblib files", "*.joblib")], title="Save GWR Model As")
        if not model_save_path:
            return
        joblib.dump({'model': model, 'bw': bw, 'scaler': scaler, 'variables': (dep_var, indep_vars)}, model_save_path)

        pred_shapefile_path = os.path.splitext(self.file_path)[0] + "_predicted.shp"
        self.gdf.to_file(pred_shapefile_path)

        report_save_path = filedialog.asksaveasfilename(defaultextension=".pdf", filetypes=[("PDF files", "*.pdf")], title="Save Report As")
        if not report_save_path:
            return
        self.generate_report(results, indep_vars, gdf_clean, bw, report_save_path)

        messagebox.showinfo("Success", f"Model trained, predictions and report saved.\n\nModel: {model_save_path}\nShapefile: {pred_shapefile_path}\nReport: {report_save_path}")

    def predict_with_saved_model(self):
        model_path = filedialog.askopenfilename(filetypes=[("Joblib files", "*.joblib")], title="Select Saved GWR Model")
        if not model_path:
            return
        data = joblib.load(model_path)
        model, bw, scaler, (dep_var, indep_vars) = data['model'], data['bw'], data['scaler'], data['variables']

        shp_path = filedialog.askopenfilename(filetypes=[("Shapefiles", "*.shp")], title="Select Shapefile for Prediction")
        if not shp_path:
            return
        gdf = gpd.read_file(shp_path)
        if not gdf.crs or not gdf.crs.is_projected:
            gdf = gdf.to_crs(epsg=3857)

        missing_feats = [f for f in indep_vars if f not in gdf.columns]
        if missing_feats:
            messagebox.showerror("Error", f"Missing variables in shapefile: {', '.join(missing_feats)}")
            return

        X = gdf[indep_vars].apply(pd.to_numeric, errors='coerce').fillna(0).values
        X_scaled = scaler.transform(X)

        if not gdf.geometry.geom_type.isin(['Point']).all():
            gdf['geometry'] = gdf.geometry.centroid

        coords = np.column_stack((gdf.geometry.x, gdf.geometry.y))
        pred_model = GWR(coords, np.zeros((len(coords),1)), X_scaled, bw)
        pred_results = pred_model.predict(coords, X_scaled)

        gdf['Prediction'] = pred_results.predy.flatten()

        out_path = os.path.splitext(shp_path)[0] + "_predicted.shp"
        gdf.to_file(out_path)

        messagebox.showinfo("Done", f"Predictions saved to: {out_path}")

    def generate_report(self, results, indep_vars, gdf_clean, bw, report_path):
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.utils import ImageReader
        import matplotlib.pyplot as plt
        import seaborn as sns
        import os

        c = canvas.Canvas(report_path, pagesize=A4)
        width, height = A4
        y_pos = height - 50
        margin = 50
        image_height = 160

        c.setFont("Helvetica-Bold", 16)
        c.drawString(margin, y_pos, "Geographically Weighted Regression (GWR) Report")
        y_pos -= 30

        # Model performance metrics
        c.setFont("Helvetica-Bold", 12)
        c.drawString(margin, y_pos, "Model Performance:")
        c.setFont("Helvetica", 11)
        y_pos -= 20
        metrics = [
            ("AICc", f"{results.aicc:.2f}"),
            ("R²", f"{results.R2:.3f}"),
            ("Adjusted R²", f"{results.adj_R2:.3f}"),
            ("Bandwidth", f"{bw}"),
            ("# Observations", f"{len(results.predy)}")
        ]
        for name, val in metrics:
            c.drawString(margin + 10, y_pos, f"{name}: {val}")
            y_pos -= 15

        # Mean Coefficients (text)
        coef_means = results.params.mean(axis=0)
        y_pos -= 10
        c.setFont("Helvetica-Bold", 12)
        c.drawString(margin, y_pos, "Mean Coefficients:")
        c.setFont("Helvetica", 11)
        y_pos -= 20
        for i, coef in enumerate(coef_means[1:]):
            c.drawString(margin + 10, y_pos, f"{indep_vars[i]}: {coef:.3f}")
            y_pos -= 15

        # Feature importance bar chart
        coef_img = "coef_temp.png"
        plt.figure(figsize=(6, 3))
        sns.barplot(x=indep_vars, y=coef_means[1:])
        plt.xticks(rotation=45)
        plt.title('Mean Coefficients (Feature Importance)')
        plt.tight_layout()
        plt.savefig(coef_img)
        plt.close()
        if y_pos < image_height + margin:
            c.showPage()
            y_pos = height - margin
        y_pos -= image_height
        c.drawImage(ImageReader(coef_img), margin, y_pos, width=500, height=image_height)
        os.remove(coef_img)

        # Residual bell curve
        bell_img = "resid_hist.png"
        plt.figure(figsize=(5.5, 3.5))
        sns.histplot(gdf_clean["Residuals"].dropna(), kde=True)
        plt.title("Residual Distribution (Bell Curve)")
        plt.xlabel("Residual")
        plt.tight_layout()
        plt.savefig(bell_img)
        plt.close()
        if y_pos < image_height + margin:
            c.showPage()
            y_pos = height - margin
        y_pos -= image_height
        c.drawImage(ImageReader(bell_img), margin, y_pos, width=500, height=image_height)
        os.remove(bell_img)

        # Actual vs Predicted scatter plot
        scatter_img = "scatter_temp.png"
        plt.figure(figsize=(5.5, 5))
        plt.scatter(results.y.flatten(), results.predy.flatten(), alpha=0.6)
        plt.plot([results.y.min(), results.y.max()], [results.y.min(), results.y.max()], 'k--')
        plt.xlabel("Actual")
        plt.ylabel("Predicted")
        plt.title("Actual vs Predicted")
        plt.tight_layout()
        plt.savefig(scatter_img)
        plt.close()
        if y_pos < image_height + margin:
            c.showPage()
            y_pos = height - margin
        y_pos -= image_height
        c.drawImage(ImageReader(scatter_img), margin, y_pos, width=500, height=image_height)
        os.remove(scatter_img)

        # Distribution plots per independent variable
        for col in indep_vars:
            try:
                dist_img = f"dist_{col}.png"
                plt.figure(figsize=(5.5, 3.5))
                sns.histplot(gdf_clean[col].dropna(), kde=True)
                plt.title(f"Distribution of {col}")
                plt.xlabel(col)
                plt.tight_layout()
                plt.savefig(dist_img)
                plt.close()
                if y_pos < image_height + margin:
                    c.showPage()
                    y_pos = height - margin
                y_pos -= image_height
                c.drawImage(ImageReader(dist_img), margin, y_pos, width=500, height=image_height)
                os.remove(dist_img)
            except Exception as e:
                print(f"Error plotting {col}: {e}")

        c.save()


if __name__ == "__main__":
    root = tk.Tk()
    app = GWRApp(root)
    root.mainloop()