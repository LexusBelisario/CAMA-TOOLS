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
from statsmodels.stats.outliers_influence import variance_inflation_factor
import ctypes

# Enable high DPI awareness on Windows
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

class OLSApp:
    def __init__(self, master):
        self.master = master
        master.title("GeoStatistical Tool — OLS")

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

        self.train_button = tk.Button(frame, text="Train OLS", command=self.train_model, width=30)
        self.train_button.grid(row=4, column=0, pady=15)

        self.predict_button = tk.Button(frame, text="Run Saved OLS", command=self.predict_with_saved_model, width=30)
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

    def _compute_vif(self, X_df):
        vif_data = []
        for i in range(X_df.shape[1]):
            vif = variance_inflation_factor(X_df.values, i)
            vif_data.append((X_df.columns[i], float(vif)))
        return vif_data

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

        # VIF diagnostics
        vif_info = self._compute_vif(pd.DataFrame(X_scaled, columns=indep_vars))

        try:
            from spreg import OLS
            ols = OLS(y, X_scaled, name_y=dep_var, name_x=indep_vars)
            gdf_clean['Predicted'] = ols.predy.flatten()
            gdf_clean['Residuals'] = ols.u.flatten()

            result_info = {
                'type': 'OLS',
                'model': ols,
                'scaler': scaler,
                'dep_var': dep_var,
                'indep_vars': indep_vars,
                'vif': vif_info,
                'betas': ols.betas.flatten().astype(float)
            }
        except Exception as e:
            messagebox.showerror("Model Error", str(e))
            return

        # Write back to full gdf
        self.gdf['Predicted'] = np.nan
        self.gdf['Residuals'] = np.nan
        self.gdf.loc[gdf_clean.index, 'Predicted'] = gdf_clean['Predicted']
        self.gdf.loc[gdf_clean.index, 'Residuals'] = gdf_clean['Residuals']

        model_save_path = filedialog.asksaveasfilename(defaultextension=".joblib",
                                                       filetypes=[("Joblib files", "*.joblib")],
                                                       title="Save OLS Model As")
        if not model_save_path:
            return
        joblib.dump(result_info, model_save_path)

        shapefile_path = os.path.splitext(self.file_path)[0] + "_ols_predicted.shp"
        self.gdf.to_file(shapefile_path)

        report_path = filedialog.asksaveasfilename(defaultextension=".pdf",
                                                   filetypes=[("PDF files", "*.pdf")],
                                                   title="Save OLS Report As")
        if not report_path:
            return
        self.generate_report(result_info, gdf_clean, report_path)
        messagebox.showinfo("Success",
                            f"OLS complete.\nModel: {model_save_path}\nShapefile: {shapefile_path}\nReport: {report_path}")

    def predict_with_saved_model(self):
        model_path = filedialog.askopenfilename(
            filetypes=[("Joblib files", "*.joblib")],
            title="Select Saved OLS Model"
        )
        if not model_path:
            return
        data = joblib.load(model_path)

        dep_var    = data['dep_var']
        indep_vars = data['indep_vars']
        scaler     = data['scaler']

        betas = None
        if 'betas' in data and data['betas'] is not None:
            betas = np.asarray(data['betas'], dtype=float).ravel()
        else:
            model = data.get('model', None)
            if model is not None and hasattr(model, 'betas'):
                betas = np.asarray(model.betas, dtype=float).ravel()

        if betas is None:
            messagebox.showerror("Prediction Error", "Saved coefficients not found in the model file.")
            return

        shp_path = filedialog.askopenfilename(
            filetypes=[("Shapefiles", "*.shp")],
            title="Select Shapefile for Prediction"
        )
        if not shp_path:
            return

        gdf = gpd.read_file(shp_path)
        if not gdf.crs or not gdf.crs.is_projected:
            gdf = gdf.to_crs(epsg=3857)

        missing = [col for col in indep_vars if col not in gdf.columns]
        if missing:
            messagebox.showerror("Error", f"Missing variables in shapefile: {', '.join(missing)}")
            return

        X_raw = gdf[indep_vars].apply(pd.to_numeric, errors='coerce')
        validX_mask = ~X_raw.isnull().any(axis=1)

        gdf['Prediction'] = np.nan
        gdf['Residuals']  = np.nan

        if validX_mask.any():
            X_valid = X_raw.loc[validX_mask].values
            X_scaled = scaler.transform(X_valid)

            X_design = np.column_stack([np.ones((X_scaled.shape[0], 1)), X_scaled])

            if X_design.shape[1] != len(betas):
                messagebox.showerror(
                    "Prediction Error",
                    f"Shape mismatch: design has {X_design.shape[1]} columns but betas has {len(betas)}."
                )
                return

            yhat = X_design @ betas
            gdf.loc[validX_mask, 'Prediction'] = yhat

        if dep_var in gdf.columns:
            y_series = pd.to_numeric(gdf[dep_var], errors='coerce')
            nullY_mask = y_series.isna()
            haveY_mask = ~nullY_mask & validX_mask

            if haveY_mask.any():
                gdf.loc[haveY_mask, 'Residuals'] = y_series.loc[haveY_mask] - gdf.loc[haveY_mask, 'Prediction']

            gdf['VALUE_filled'] = y_series
            gdf.loc[nullY_mask, 'VALUE_filled'] = gdf.loc[nullY_mask, 'Prediction']

        out_path = os.path.splitext(shp_path)[0] + "_ols_predicted.shp"
        gdf.to_file(out_path)
        messagebox.showinfo(
            "Success",
            f"Predictions written to: {out_path}\n\n"
            f"- 'Prediction'  = model output for rows with valid X\n"
            f"- 'Residuals'   = only where {dep_var} exists\n"
            f"- 'VALUE_filled' fills null {dep_var} with Prediction"
        )

    def generate_report(self, data, gdf_clean, report_path):
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfbase.pdfmetrics import stringWidth
        import matplotlib.pyplot as plt
        import seaborn as sns
        import numpy as np
        import uuid
        import os

        width, height = A4
        LEFT = 50
        RIGHT = width - 50
        TOP = height - 50
        BOTTOM = 50
        LINE = 16
        SMALL_GAP = 8
        SECTION_GAP = 18

        def draw_wrapped_text(c, text, x, y, max_width, font_name="Helvetica", font_size=11):
            c.setFont(font_name, font_size)
            words = text.split()
            line = ""
            y_cur = y
            from reportlab.pdfbase.pdfmetrics import stringWidth
            for w in words:
                trial = (line + " " + w).strip()
                if stringWidth(trial, font_name, font_size) <= max_width:
                    line = trial
                else:
                    if y_cur < BOTTOM + LINE:
                        c.showPage()
                        y_cur = TOP
                        c.setFont(font_name, font_size)
                    c.drawString(x, y_cur, line)
                    y_cur -= LINE
                    line = w
            if line:
                if y_cur < BOTTOM + LINE:
                    c.showPage()
                    y_cur = TOP
                    c.setFont(font_name, font_size)
                c.drawString(x, y_cur, line)
                y_cur -= LINE
            return y_cur

        def draw_kv(c, label, value, y, label_w=140):
            if y < BOTTOM + LINE:
                c.showPage()
                y = TOP
            c.setFont("Helvetica-Bold", 11)
            c.drawString(LEFT, y, f"{label}:")
            c.setFont("Helvetica", 11)
            c.drawString(LEFT + label_w, y, str(value))
            return y - LINE

        def draw_section_title(c, title, y):
            if y < BOTTOM + LINE:
                c.showPage()
                y = TOP
            c.setFont("Helvetica-Bold", 14)
            c.drawString(LEFT, y, title)
            return y - SECTION_GAP

        def draw_table(c, headers, rows, y, col_widths=None, font="Helvetica", font_size=10):
            if col_widths is None:
                avail = RIGHT - LEFT
                col_widths = [avail / len(headers)] * len(headers)

            if y < BOTTOM + 2*LINE:
                c.showPage()
                y = TOP
            c.setFont("Helvetica-Bold", font_size)
            x = LEFT
            for h, w in zip(headers, col_widths):
                c.drawString(x, y, str(h))
                x += w
            y -= LINE

            c.setFont(font, font_size)
            for r in rows:
                if y < BOTTOM + LINE:
                    c.showPage()
                    y = TOP
                    c.setFont("Helvetica-Bold", font_size)
                    x = LEFT
                    for h, w in zip(headers, col_widths):
                        c.drawString(x, y, str(h))
                        x += w
                    y -= LINE
                    c.setFont(font, font_size)

                x = LEFT
                for cell, w in zip(r, col_widths):
                    txt = "" if cell is None else str(cell)
                    c.drawString(x, y, txt[:60])
                    x += w
                y -= LINE
            return y

        def save_plot(fig, fname_base):
            out = f"{fname_base}_{uuid.uuid4().hex}.png"
            fig.tight_layout()
            fig.savefig(out, dpi=120)
            plt.close(fig)
            return out

        def draw_image_auto(c, img_path, y, img_w=500, img_h=160):
            if y < BOTTOM + img_h + LINE:
                c.showPage()
                y = TOP
            c.drawImage(ImageReader(img_path), LEFT, y - img_h, width=img_w, height=img_h)
            return y - img_h - SMALL_GAP

        c = canvas.Canvas(report_path, pagesize=A4)
        model = data['model']
        dep_var = data['dep_var']
        indep_vars = data['indep_vars']
        vif_info = data.get('vif', [])

        y_true = gdf_clean[dep_var].values.flatten()
        y_pred = gdf_clean['Predicted'].values.flatten()
        resid = gdf_clean['Residuals'].values.flatten()

        c.setFont("Helvetica-Bold", 18)
        c.drawString(50, 792 - 50, "OLS Regression Report")
        y = 792 - 80
        c.setFont("Helvetica", 11)
        y = draw_kv(c, "Dependent Variable", dep_var, y)
        y = draw_kv(c, "Observations (train)", len(gdf_clean), y)
        y = draw_section_title(c, "Independent Variables", y)
        y = draw_wrapped_text(c, ", ".join(indep_vars), 50, y, 500)

        y = draw_section_title(c, "Model Metrics", y)
        if hasattr(model, "aic"):
            y = draw_kv(c, "AIC", f"{model.aic:.4f}", y)
        if hasattr(model, "r2"):
            y = draw_kv(c, "R²", f"{model.r2:.4f}", y)
        if hasattr(model, "pr2"):
            y = draw_kv(c, "Pseudo R²", f"{model.pr2:.4f}", y)
        if hasattr(model, "adj_r2"):
            y = draw_kv(c, "Adj. R²", f"{model.adj_r2:.4f}", y)

        y = draw_section_title(c, "Coefficients (relative to intercept)", y)
        betas_all = np.asarray(model.betas).flatten()
        headers = ["Term", "Coefficient"]
        rows = [("Intercept", f"{betas_all[0]:.6f}")]
        for name, coef in zip(indep_vars, betas_all[1:]):
            rows.append((name, f"{coef:.6f}"))
        y = draw_table(c, headers, rows, y)

        if vif_info:
            y = draw_section_title(c, "VIF Diagnostics", y)
            vif_rows = [(name, f"{vif:.3f}") for name, vif in vif_info]
            y = draw_table(c, ["Variable", "VIF"], vif_rows, y)

        coef_fig = plt.figure(figsize=(6, 3))
        sns.barplot(x=indep_vars, y=betas_all[1:])
        plt.xticks(rotation=45, ha="right")
        plt.title("Feature Coefficients")
        coef_img = save_plot(coef_fig, "coef_plot")
        y = draw_image_auto(c, coef_img, y)
        os.remove(coef_img)

        if len(resid) > 0:
            fig = plt.figure(figsize=(6, 3))
            plt.hist(resid, bins=30)
            plt.title("Residuals Histogram")
            plt.xlabel("Residual")
            plt.ylabel("Count")
            resid_hist_img = save_plot(fig, "resid_hist")
            y = draw_image_auto(c, resid_hist_img, y)
            os.remove(resid_hist_img)

        if len(y_true) > 0 and len(y_pred) == len(y_true):
            fig = plt.figure(figsize=(6, 3))
            plt.scatter(y_pred, y_true, s=10)
            mn = np.nanmin([np.nanmin(y_pred), np.nanmin(y_true)])
            mx = np.nanmax([np.nanmax(y_pred), np.nanmax(y_true)])
            plt.plot([mn, mx], [mn, mx])
            plt.xlabel("Predicted")
            plt.ylabel("Actual")
            plt.title("Actual vs Predicted")
            avp_img = save_plot(fig, "actual_vs_pred")
            y = draw_image_auto(c, avp_img, y)
            os.remove(avp_img)

        resid_map_img = f"resid_map_{uuid.uuid4().hex}.png"
        fig_map, ax = plt.subplots(figsize=(6, 4))
        try:
            gdf_clean.plot(column='Residuals', ax=ax, cmap='coolwarm', edgecolor='black', legend=True)
            plt.title("Residuals Map")
            plt.axis('off')
            fig_map.tight_layout()
            fig_map.savefig(resid_map_img, dpi=120)
            plt.close(fig_map)
            y = draw_image_auto(c, resid_map_img, y, img_w=500, img_h=180)
        except Exception:
            plt.close(fig_map)
        finally:
            if os.path.exists(resid_map_img):
                os.remove(resid_map_img)

        c.showPage()
        c.save()

def main():
    root = tk.Tk()
    app = OLSApp(root)
    root.mainloop()
    return 0   # ✅ Added clean exit status

if __name__ == "__main__":
    main()
