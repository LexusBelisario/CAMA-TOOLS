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
from mgwr.gwr import GWR
from mgwr.sel_bw import Sel_BW
import ctypes

# optional: for VIF
try:
    from statsmodels.stats.outliers_influence import variance_inflation_factor as sm_vif
    HAS_SM = True
except Exception:
    HAS_SM = False

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass


# ============== Helpers to fix singular matrices + VIF ==============

def drop_zero_variance(df):
    keep = [c for c in df.columns if df[c].std(skipna=True) > 0]
    dropped = [c for c in df.columns if c not in keep]
    return df[keep], dropped

def drop_high_corr(df, thr=0.999):
    corr = df.corr().abs()
    upper = corr.where(np.triu(np.ones(corr.shape), k=1).astype(bool))
    to_drop = set()
    while True:
        max_corr = upper.max().max()
        if pd.isna(max_corr) or max_corr <= thr:
            break
        # drop one of the pair with highest correlation: choose the one with higher mean corr
        idx = upper.stack().idxmax()
        a, b = idx
        mean_a = upper[a].mean()
        mean_b = upper[b].mean()
        drop_col = a if mean_a >= mean_b else b
        to_drop.add(drop_col)
        df = df.drop(columns=[drop_col])
        corr = df.corr().abs()
        upper = corr.where(np.triu(np.ones(corr.shape), k=1).astype(bool))
    return df, sorted(list(to_drop))

def vif_prune(df, thr=10.0):
    if df.shape[1] <= 1:
        return df, []
    dropped = []
    # standardize (avoid scale effects)
    X = (df - df.mean()) / df.std(ddof=0).replace(0, 1)
    while True:
        if X.shape[1] <= 1:
            break
        if HAS_SM:
            vifs = pd.Series([sm_vif(X.values, i) for i in range(X.shape[1])], index=X.columns)
        else:
            # fallback simple VIF (R^2 from regression approx via pseudo-inverse)
            vifs = {}
            for col in X.columns:
                y = X[col].values.reshape(-1, 1)
                X_ = X.drop(columns=[col]).values
                # ridge to stabilize
                XtX = X_.T @ X_ + 1e-8 * np.eye(X_.shape[1])
                beta = np.linalg.solve(XtX, X_.T @ y)
                yhat = X_ @ beta
                ssr = float(((yhat - y.mean()) ** 2).sum())
                sst = float(((y - y.mean()) ** 2).sum())
                r2 = min(max(ssr / sst if sst > 0 else 0.0, 0.0), 0.999999)
                vifs[col] = 1.0 / (1.0 - r2)
            vifs = pd.Series(vifs)
        worst = vifs.idxmax()
        if vifs[worst] > thr:
            dropped.append(worst)
            X = X.drop(columns=[worst])
        else:
            break
    return df[X.columns.tolist()], dropped

def compute_vif_series(df):
    """
    Compute VIFs for the current dataframe (columns = predictors).
    Returns a pandas Series indexed by column name.
    """
    if df.shape[1] == 0:
        return pd.Series(dtype=float)
    X = (df - df.mean()) / df.std(ddof=0).replace(0, 1)
    if X.shape[1] == 1:
        return pd.Series([1.0], index=X.columns)  # single regressor => VIF=1
    if HAS_SM:
        vals = [sm_vif(X.values, i) for i in range(X.shape[1])]
        return pd.Series(vals, index=X.columns)
    # fallback
    vifs = {}
    for col in X.columns:
        y = X[col].values.reshape(-1, 1)
        X_ = X.drop(columns=[col]).values
        XtX = X_.T @ X_ + 1e-8 * np.eye(X_.shape[1])
        beta = np.linalg.solve(XtX, X_.T @ y)
        yhat = X_ @ beta
        ssr = float(((yhat - y.mean()) ** 2).sum())
        sst = float(((y - y.mean()) ** 2).sum())
        r2 = min(max(ssr / sst if sst > 0 else 0.0, 0.0), 0.999999)
        vifs[col] = 1.0 / (1.0 - r2)
    return pd.Series(vifs)

def ensure_full_rank(df):
    dropped = []
    while df.shape[1] > 0 and np.linalg.matrix_rank(df.values) < df.shape[1]:
        # drop the column with highest average absolute correlation to others
        corr = df.corr().abs()
        np.fill_diagonal(corr.values, 0)
        avg_corr = corr.mean()
        victim = avg_corr.idxmax()
        dropped.append(victim)
        df = df.drop(columns=[victim])
    return df, dropped

def jitter_duplicate_coords(xy, amount=1e-3):
    """Add tiny noise (meters) to duplicate coordinate rows."""
    xy = xy.copy()
    arr = xy.view([('', xy.dtype)] * xy.shape[1]).reshape(-1)
    _, counts = np.unique(arr, return_counts=True)
    if counts.max() <= 1:
        return xy  # already unique
    key, inv, counts = np.unique(arr, return_inverse=True, return_counts=True)
    for k in np.where(counts > 1)[0]:
        idxs = np.where(inv == k)[0]
        for j, row_idx in enumerate(idxs):
            jitter = (j) * amount
            xy[row_idx, 0] += jitter
            xy[row_idx, 1] += jitter
    return xy


# ============================= App =============================

class GWRApp:
    def __init__(self, master):
        self.master = master
        master.title("GeoStatistical Tool — GWR")

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

        self.train_button = tk.Button(frame, text="Train GWR", command=self.train_model, width=30)
        self.train_button.grid(row=4, column=0, pady=15)

        self.predict_button = tk.Button(frame, text="Run Saved GWR", command=self.predict_with_saved_model, width=30)
        self.predict_button.grid(row=4, column=1, pady=15)

    def load_shapefile(self):
        file_path = filedialog.askopenfilename(filetypes=[("Shapefiles", "*.shp")])
        if not file_path:
            return
        self.file_path = file_path
        self.gdf = gpd.read_file(file_path)

        if not self.gdf.crs or not self.gdf.crs.is_projected:
            self.gdf = self.gdf.to_crs(epsg=3857)

        # ============================================
        # 🧮 Convert numeric-looking object columns to numeric
        # ============================================
        for col in self.gdf.columns:
            if self.gdf[col].dtype == object:
                # Try to convert strings that look numeric
                converted = pd.to_numeric(self.gdf[col], errors='coerce')
                # If enough non-null values were converted, replace column
                non_null_ratio = converted.notnull().mean()
                if non_null_ratio > 0.8:  # at least 80% values successfully parsed
                    self.gdf[col] = converted

        # ============================================
        # 🧩 Get numeric columns after conversion
        # ============================================
        numeric_columns = self.gdf.select_dtypes(include=[np.number]).columns.tolist()
        if not numeric_columns:
            messagebox.showerror("Error", "No numeric fields found in shapefile.")
            return

        self.dep_combobox['values'] = numeric_columns
        self.dep_combobox.set("Select Dependent Variable")
        self.indep_listbox.delete(0, tk.END)
        for col in numeric_columns:
            self.indep_listbox.insert(tk.END, col)

    def _prepare_design(self, dep_var, indep_vars):
        required = [dep_var] + indep_vars
        gdf = self.gdf.dropna(subset=required).copy()
        gdf = gdf[gdf[dep_var] != 0]
        gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notnull()].copy()

        # 🧩 FIX: Preserve original geometry for output saving later
        original_geom = gdf.geometry.copy()

        # 🧩 FIX: Force all independent vars to numeric
        for col in indep_vars + [dep_var]:
            gdf[col] = pd.to_numeric(gdf[col], errors="coerce")

        # Drop any rows with NaNs after conversion
        gdf = gdf.dropna(subset=required)

        # 🧩 FIX: Create centroid coordinates for GWR math (but don't replace original geometry)
        centroids = gdf.geometry.centroid
        coords = np.column_stack((centroids.x, centroids.y))
        coords = jitter_duplicate_coords(coords, amount=1e-3)

        y = gdf[[dep_var]].values
        X_df = gdf[indep_vars].copy()

        # 1) zero variance
        X_df, drop0 = drop_zero_variance(X_df)

        # 2) high correlation — stricter threshold
        X_df, drop_corr = drop_high_corr(X_df, thr=0.9)

        # 3) VIF pruning — stricter
        X_df, drop_vif = vif_prune(X_df, thr=3.0)

        # 4) Ensure full rank
        X_df, drop_rank = ensure_full_rank(X_df)

        kept_vars = X_df.columns.tolist()
        dropped = drop0 + drop_corr + drop_vif + drop_rank

        if len(kept_vars) == 0:
            raise ValueError("All selected independent variables were dropped (zero variance / multicollinearity).")

        vif_final = compute_vif_series(X_df).sort_values(ascending=False)

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X_df.values)

        # 🧩 FIX: Reconfirm rank after scaling
        if np.linalg.matrix_rank(X_scaled) < X_scaled.shape[1]:
            X_df_tmp = pd.DataFrame(X_scaled, columns=kept_vars)
            X_df_tmp, more_drop = ensure_full_rank(X_df_tmp)
            kept_vars = X_df_tmp.columns.tolist()
            dropped += more_drop
            if len(kept_vars) == 0:
                raise ValueError("Design matrix remains singular after pruning.")
            X_scaled = X_scaled[:, [list(X_df_tmp.columns).index(k) for k in kept_vars]]

        # 🧩 FIX: Reattach original geometry for later shapefile output
        gdf["geometry"] = original_geom

        return gdf, y, X_scaled[:, :len(kept_vars)], scaler, kept_vars, dropped, coords, vif_final

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

        try:
            gdf_clean, y, X_scaled, scaler, kept_vars, dropped, coords, vif_final = self._prepare_design(dep_var, indep_vars)

            # bandwidth search & fit
            n, p = X_scaled.shape
            bw_min = max(40, p * 8)                      # ensure ≥ ~8p neighbors (at least 40)
            bw_max = min(n - 1, max(200, p * 20))
            selector = Sel_BW(coords, y, X_scaled, fixed=False, kernel='gaussian')
            bw = selector.search(bw_min=bw_min, bw_max=bw_max)
            model = GWR(coords, y, X_scaled, bw, fixed=False, kernel='gaussian')

            results = model.fit()

            gdf_clean['Predicted'] = results.predy.flatten()
            gdf_clean['Residuals'] = results.resid_response.flatten()

            result_info = {
                'type': 'GWR',
                'bw': bw,
                'scaler': scaler,
                'dep_var': dep_var,
                'indep_vars': kept_vars,
                'dropped_vars': dropped,
                'vif_final': vif_final.to_dict(),
                'params': results.params,          # 🧩 ADD THIS
                'has_intercept': results.intercept is not None if hasattr(results, "intercept") else True
            }

        except Exception as e:
            messagebox.showerror("Model Error", str(e))
            return

        # push back to original gdf
        self.gdf['Predicted'] = np.nan
        self.gdf['Residuals'] = np.nan
        self.gdf.loc[gdf_clean.index, 'Predicted'] = gdf_clean['Predicted']
        self.gdf.loc[gdf_clean.index, 'Residuals'] = gdf_clean['Residuals']

        model_save_path = filedialog.asksaveasfilename(defaultextension=".joblib",
                                                       filetypes=[("Joblib files", "*.joblib")],
                                                       title="Save GWR Model As")
        if not model_save_path:
            return
        joblib.dump(result_info, model_save_path)

        shapefile_path = os.path.splitext(self.file_path)[0] + "_gwr_predicted.shp"
        self.gdf.to_file(shapefile_path)

        report_path = filedialog.asksaveasfilename(defaultextension=".pdf",
                                                   filetypes=[("PDF files", "*.pdf")],
                                                   title="Save GWR Report As")
        if not report_path:
            return
        self.generate_report(results, result_info, gdf_clean, report_path)
        dropped_msg = f"\nDropped vars: {', '.join(result_info['dropped_vars'])}" if result_info['dropped_vars'] else ""
        messagebox.showinfo("Success",
                            f"GWR complete.\nModel: {model_save_path}\nShapefile: {shapefile_path}\nReport: {report_path}{dropped_msg}")

    def predict_with_saved_model(self):
        model_path = filedialog.askopenfilename(
            filetypes=[("Joblib files", "*.joblib")],
            title="Select Saved GWR Model"
        )
        if not model_path:
            return

        # Load the saved model metadata
        data = joblib.load(model_path)

        dep_var = data['dep_var']
        indep_vars = data['indep_vars']
        scaler = data['scaler']
        bw = data['bw']
        params = np.array(data.get('params', []))  # 🧩 FIX: load saved coefficients

        shp_path = filedialog.askopenfilename(
            filetypes=[("Shapefiles", "*.shp")],
            title="Select Shapefile for Prediction"
        )
        if not shp_path:
            return

        gdf = gpd.read_file(shp_path)
        if not gdf.crs or not gdf.crs.is_projected:
            gdf = gdf.to_crs(epsg=3857)

        gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notnull()].copy()
        original_geom = gdf.geometry.copy()  # 🧩 preserve polygons

        # --- convert all predictors to numeric
        for col in indep_vars:
            gdf[col] = pd.to_numeric(gdf[col], errors="coerce")

        missing = [f for f in indep_vars if f not in gdf.columns]
        if missing:
            messagebox.showerror("Error", f"Missing variables in shapefile: {', '.join(missing)}")
            return

        try:
            X_raw = gdf[indep_vars].apply(pd.to_numeric, errors='coerce')
            mask_valid = ~X_raw.isnull().any(axis=1)
            gdf = gdf.loc[mask_valid].copy()
            X_array = X_raw.loc[mask_valid].values

            if X_array.shape[0] == 0:
                messagebox.showerror("Prediction Error", "No valid rows to predict.")
                return

            # --- Ensure scaler column order matches model
            X_df = pd.DataFrame(X_array, columns=indep_vars)
            X_df = X_df[data['indep_vars']]  # enforce identical order
            X_scaled = scaler.transform(X_df)

            # --- Centroid coordinates (used only for consistency if needed)
            centroids = gdf.geometry.centroid
            coords = np.column_stack((centroids.x, centroids.y))
            coords = jitter_duplicate_coords(coords, amount=1e-3)

            # 🧩 FIX: use stored coefficients instead of new blank GWR
            if params.size > 0:
                # params is (n_obs, k) local coeffs → take mean as global weights
                mean_params = params.mean(axis=0)
                if mean_params.shape[0] == X_scaled.shape[1] + 1:
                    # has intercept column
                    X_with_const = np.hstack([np.ones((X_scaled.shape[0], 1)), X_scaled])
                else:
                    X_with_const = X_scaled

                predictions = np.dot(X_with_const, mean_params)
                gdf['Prediction'] = predictions
            else:
                # fallback: simple mean model if params not found
                messagebox.showwarning("Notice", "No stored coefficients found. Recomputing local model may be inaccurate.")
                gwr_model = GWR(coords, np.zeros((len(coords), 1)), X_scaled, bw)
                results = gwr_model.predict(coords, X_scaled)
                gdf['Prediction'] = results.predy.flatten()

            gdf['geometry'] = original_geom  # 🧩 restore polygons

            if np.allclose(gdf['Prediction'], 0):
                messagebox.showwarning("Warning", "Predicted values are all near zero. Check model parameters or data consistency.")

            out_path = os.path.splitext(shp_path)[0] + "_gwr_predicted.shp"
            gdf.to_file(out_path)
            messagebox.showinfo("Success", f"GWR prediction completed.\nSaved to: {out_path}")
        except Exception as e:
            messagebox.showerror("Prediction Error", str(e))

    # ======================= REPORT HELPERS (pagination) =======================

    def _footer(self, c, width, height, left_margin, bottom_margin):
        c.setFont("Helvetica", 9)
        page_no = c.getPageNumber()
        c.drawRightString(width - left_margin, bottom_margin - 10, f"Page {page_no}")

    def _maybe_new_page(self, c, y_pos, need, width, height, margins, header_text=None):
        """
        If remaining space < need, finish page and start a new one.
        Returns fresh y_pos at top of new content area.
        """
        left, right, top, bottom = margins
        if y_pos - need < bottom:
            self._footer(c, width, height, left, bottom)
            c.showPage()
            # reset font after showPage
            c.setFont("Helvetica", 12)
            y_pos = height - top
            if header_text:
                c.setFont("Helvetica-Bold", 13)
                c.drawString(left, y_pos, header_text); y_pos -= 16
                c.setFont("Helvetica", 12)
        return y_pos

    def _section_title(self, c, text, y_pos, width, height, margins):
        left, right, top, bottom = margins
        # Ensure title fits; if not, push to new page
        y_pos = self._maybe_new_page(c, y_pos, need=20, width=width, height=height, margins=margins)
        c.setFont("Helvetica-Bold", 13)
        c.drawString(left, y_pos, text)
        y_pos -= 16
        c.setFont("Helvetica", 12)
        return y_pos

    def _kv_list_block(self, c, pairs, y_pos, line_height, width, height, margins, header_text=None):
        """
        Render a list of (label -> value) strings with pagination.
        pairs: list of strings already formatted (e.g., "Var: 1.23")
        """
        left, right, top, bottom = margins
        if header_text:
            y_pos = self._section_title(c, header_text, y_pos, width, height, margins)
        for line in pairs:
            # 1 line needed; break page if needed
            y_pos = self._maybe_new_page(c, y_pos, need=line_height, width=width, height=height, margins=margins)
            c.drawString(left + 10, y_pos, line)
            y_pos -= line_height
        return y_pos

    def _image_block(self, c, img_path, desired_w, desired_h, y_pos, width, height, margins, header_text=None):
        """
        Place an image with pagination. If not enough room, move to next page before drawing.
        """
        left, right, top, bottom = margins
        needed = desired_h + 20  # include caption/spacing
        if header_text:
            needed += 16
        y_pos = self._maybe_new_page(c, y_pos, need=needed, width=width, height=height, margins=margins, header_text=header_text)
        # Optional header already drawn by _maybe_new_page if provided; if not, draw now
        if header_text:
            # If we already drew on new page via _maybe_new_page, it drew header.
            # If not forced, draw header here:
            pass
        # Draw image
        c.drawImage(ImageReader(img_path), left, y_pos - desired_h, width=desired_w, height=desired_h)
        y_pos -= (desired_h + 10)
        return y_pos

    # =============================== REPORT ===============================

    def generate_report(self, results, model_meta, gdf_clean, report_path):
        width, height = A4
        MARGIN_LEFT = 50
        MARGIN_RIGHT = 50
        MARGIN_TOP = 50
        MARGIN_BOTTOM = 50
        margins = (MARGIN_LEFT, MARGIN_RIGHT, MARGIN_TOP, MARGIN_BOTTOM)

        c = canvas.Canvas(report_path, pagesize=A4)

        # Title
        c.setFont("Helvetica-Bold", 16)
        c.drawString(MARGIN_LEFT, height - MARGIN_TOP, "GWR Regression Report")
        y_pos = height - MARGIN_TOP - 28
        c.setFont("Helvetica", 12)

        # ===== Summary Metrics =====
        metrics = [
            f"AICc: {results.aicc:.2f}",
            f"R²: {results.R2:.3f}",
            f"Adj. R²: {results.adj_R2:.3f}",
            f"Bandwidth: {model_meta['bw']}"
        ]
        # Dropped vars (if any)
        if model_meta.get('dropped_vars'):
            metrics.append(f"Dropped Vars: {', '.join(model_meta['dropped_vars'])}")

        # Render metrics (each is one line)
        y_pos = self._kv_list_block(
            c,
            pairs=metrics,
            y_pos=y_pos,
            line_height=16,
            width=width, height=height, margins=margins,
            header_text="Model Summary"
        )

        # ===== Mean Coefficients =====
        coef_means = results.params.mean(axis=0)
        if coef_means.shape[0] > len(model_meta['indep_vars']):
            coef_means = coef_means[1:]  # drop intercept if present

        x_labels = model_meta['indep_vars']
        coef_pairs = [f"{name}: {coef:.4f}" for name, coef in zip(x_labels, coef_means)]
        y_pos -= 6
        y_pos = self._kv_list_block(
            c,
            pairs=coef_pairs,
            y_pos=y_pos,
            line_height=14,
            width=width, height=height, margins=margins,
            header_text="Mean Coefficients (averaged across space)"
        )

        # Bar plot for coefficients
        coef_img = f"coef_plot_{uuid.uuid4().hex}.png"
        plt.figure(figsize=(6, 3), dpi=120)
        sns.barplot(x=x_labels, y=coef_means)
        plt.xticks(rotation=45, ha='right')
        plt.title('Feature Coefficients (mean across space)')
        plt.tight_layout()
        plt.savefig(coef_img); plt.close()

        # Ensure plot fits; pick a standard image size
        IMG_W, IMG_H = 500, 140  # pixels on page (ReportLab units are points; 1 px ~ 1 pt here for simplicity)
        y_pos = self._image_block(c, coef_img, IMG_W, IMG_H, y_pos, width, height, margins, header_text=None)
        os.remove(coef_img)

        # ===== Residuals Map =====
        resid_img = f"resid_plot_{uuid.uuid4().hex}.png"
        fig, ax = plt.subplots(figsize=(6, 4), dpi=120)
        gdf_clean.plot(column='Residuals', ax=ax, cmap='coolwarm', edgecolor='black', legend=True)
        plt.title("Residuals Map"); plt.axis('off'); plt.tight_layout()
        plt.savefig(resid_img); plt.close()

        IMG_W2, IMG_H2 = 500, 180
        y_pos = self._image_block(c, resid_img, IMG_W2, IMG_H2, y_pos, width, height, margins, header_text="Residuals Map")
        os.remove(resid_img)

        # ===== VIF Diagnostics =====
        vif_dict = model_meta.get('vif_final', {})
        if vif_dict:
            vif_series = pd.Series(vif_dict).sort_values(ascending=False)
            vif_pairs = [f"{k}: VIF = {v:.2f}" for k, v in vif_series.items()]
            y_pos -= 6
            y_pos = self._kv_list_block(
                c,
                pairs=vif_pairs,
                y_pos=y_pos,
                line_height=14,
                width=width, height=height, margins=margins,
                header_text="VIF Diagnostics (final design)"
            )

            # VIF plot
            vif_img = f"vif_plot_{uuid.uuid4().hex}.png"
            plt.figure(figsize=(6, 3), dpi=120)
            sns.barplot(x=vif_series.index.tolist(), y=vif_series.values)
            plt.xticks(rotation=45, ha='right')
            plt.title('VIF by Variable (lower is better)')
            plt.tight_layout()
            plt.savefig(vif_img); plt.close()

            IMG_W3, IMG_H3 = 500, 140
            y_pos = self._image_block(c, vif_img, IMG_W3, IMG_H3, y_pos, width, height, margins, header_text=None)
            os.remove(vif_img)

        # Final footer + save
        self._footer(c, width, height, MARGIN_LEFT, MARGIN_BOTTOM)
        c.save()


def main():
    root = tk.Tk()
    app = GWRApp(root)
    root.mainloop()
    return 0   # ✅ clean exit for dispatcher

if __name__ == "__main__":
    main()