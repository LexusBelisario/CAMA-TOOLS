import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import geopandas as gpd
import pandas as pd
import numpy as np
from sklearn.linear_model import LinearRegression
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_squared_error, r2_score, mean_absolute_error
import matplotlib.pyplot as plt
import joblib

class LinearRegressionApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Linear Regression Tool")
        self.df = None
        self.create_widgets()

    def create_widgets(self):
        frame = ttk.Frame(self.root, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Button(frame, text="Load Shapefile", command=self.load_shapefile).grid(row=0, column=0)
        self.var_listbox = tk.Listbox(frame, selectmode=tk.MULTIPLE, height=8)
        self.var_listbox.grid(row=1, column=0, columnspan=2, sticky="we")
        ttk.Label(frame, text="Independent Variables").grid(row=2, column=0)

        self.target_var = tk.StringVar()
        self.target_menu = ttk.Combobox(frame, textvariable=self.target_var, state="readonly")
        self.target_menu.grid(row=1, column=2)
        ttk.Label(frame, text="Dependent Variable").grid(row=2, column=2)

        ttk.Button(frame, text="Train Model", command=self.train_model).grid(row=3, column=0, columnspan=3, pady=10)
        ttk.Button(frame, text="Run Saved Model", command=self.run_saved_model).grid(row=4, column=0, columnspan=3)

    def load_shapefile(self):
        path = filedialog.askopenfilename(filetypes=[("Shapefiles", "*.shp")])
        if not path:
            return
        gdf = gpd.read_file(path)
        self.gdf = gdf
        self.df = pd.DataFrame(gdf.drop(columns="geometry"))
        numeric_cols = self.df.select_dtypes(include=np.number).columns.tolist()

        self.var_listbox.delete(0, tk.END)
        for col in numeric_cols:
            self.var_listbox.insert(tk.END, col)

        self.target_menu['values'] = numeric_cols

    def train_model(self):
        import matplotlib.pyplot as plt
        import seaborn as sns
        from matplotlib.backends.backend_pdf import PdfPages
        from scipy import stats
        import os

        if self.df is None:
            return

        selected = [self.var_listbox.get(i) for i in self.var_listbox.curselection()]
        target = self.target_var.get()
        if not selected or not target:
            messagebox.showerror("Error", "Select variables.")
            return

        df = self.df[selected + [target]].dropna()
        if df.empty:
            messagebox.showerror("Error", "Selected columns contain no valid data.")
            return

        X = df[selected].values
        y = df[target].values
        from sklearn.preprocessing import StandardScaler
        from sklearn.model_selection import train_test_split
        from sklearn.linear_model import LinearRegression
        from sklearn.metrics import mean_squared_error, r2_score, mean_absolute_error

        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.3, random_state=42)
        scaler = StandardScaler().fit(X_train)
        X_train = scaler.transform(X_train)
        X_test = scaler.transform(X_test)

        model = LinearRegression()
        model.fit(X_train, y_train)
        preds = model.predict(X_test)
        residuals = y_test - preds

        r2 = r2_score(y_test, preds)
        mse = mean_squared_error(y_test, preds)
        mae = mean_absolute_error(y_test, preds)
        rmse = np.sqrt(mse)

        # Save model
        out_path = filedialog.asksaveasfilename(defaultextension=".pkl", title="Save Trained Model")
        if out_path:
            joblib.dump({'model': model, 'scaler': scaler, 'features': selected}, out_path)

        # Ask PDF path
        report_path = filedialog.asksaveasfilename(defaultextension=".pdf", title="Save Report PDF", filetypes=[("PDF", "*.pdf")])
        if not report_path:
            return

        with PdfPages(report_path) as pp:
            # 1. Metrics Table
            fig, ax = plt.subplots(figsize=(6, 1.5))
            ax.axis('off')
            metrics_data = [
                ["Model", "MSE", "MAE", "RMSE", "R²"],
                ["Linear Regression", f"{mse:.2f}", f"{mae:.2f}", f"{rmse:.2f}", f"{r2:.2f}"]
            ]
            table = ax.table(cellText=metrics_data, loc='center', cellLoc='center', colLabels=None)
            table.scale(1, 2)
            pp.savefig(fig); plt.close(fig)

            # 2. Feature Importance (standardized coefficients)
            std_X = np.std(X_train, axis=0)
            std_y = np.std(y_train)
            importance = model.coef_ * std_X / std_y
            fig, ax = plt.subplots(figsize=(8, 4))
            ax.bar(selected, importance)
            ax.set_ylabel("Standardized Coefficient")
            ax.set_title("Feature Importance")
            ax.tick_params(axis='x', rotation=45)
            plt.tight_layout()
            pp.savefig(fig); plt.close(fig)

            # 3. Residual Distribution (bell curve)
            fig, ax = plt.subplots(figsize=(6, 4))
            sns.histplot(residuals, kde=True, ax=ax)
            ax.set_title("Residual Distribution (Normal Curve)")
            ax.set_xlabel("Residual")
            ax.set_ylabel("Frequency")
            plt.tight_layout()
            pp.savefig(fig); plt.close(fig)

            # 4. T-test on residuals
            t_stat, p_val = stats.ttest_1samp(residuals, 0)
            fig, ax = plt.subplots(figsize=(6, 2))
            ax.axis('off')
            text = f"T-test on Residuals:\nT-statistic = {t_stat:.4f}\nP-value = {p_val:.4f}"
            ax.text(0.5, 0.5, text, fontsize=12, ha='center', va='center')
            pp.savefig(fig); plt.close(fig)

            # 5. Distribution of each independent variable
            for col in selected:
                try:
                    fig, ax = plt.subplots(figsize=(6, 4))
                    sns.histplot(df[col], kde=True, ax=ax)
                    ax.set_title(f"Distribution of '{col}'")
                    ax.set_xlabel(col)
                    ax.set_ylabel("Frequency")
                    plt.tight_layout()
                    pp.savefig(fig)
                    plt.close(fig)
                except Exception as e:
                    print(f"Failed to plot {col}: {e}")

            # 6. Scatter Plot: Actual vs Predicted
            fig, ax = plt.subplots(figsize=(6, 6))
            ax.scatter(y_test, preds, alpha=0.6)
            ax.plot([min(y_test), max(y_test)], [min(y_test), max(y_test)], 'k--', lw=1.5)
            ax.set_xlabel("Actual Values")
            ax.set_ylabel("Predicted Values")
            ax.set_title("Actual vs Predicted Scatter Plot")
            plt.tight_layout()
            pp.savefig(fig)
            plt.close(fig)


        messagebox.showinfo("Done", f"Model and report saved successfully to:\n{report_path}")

    def run_saved_model(self):
        mdl_path = filedialog.askopenfilename(filetypes=[("Pickle Files", "*.pkl")])
        shp_path = filedialog.askopenfilename(filetypes=[("Shapefiles", "*.shp")])
        if not mdl_path or not shp_path:
            return

        gdf = gpd.read_file(shp_path)
        df = pd.DataFrame(gdf.drop(columns='geometry'))
        data = joblib.load(mdl_path)
        mdl, scaler, feats = data['model'], data['scaler'], data['features']

        if not all(f in df.columns for f in feats):
            messagebox.showerror("Error", "Some features missing in data.")
            return

        X = df[feats].fillna(0).values
        X = scaler.transform(X)
        preds = mdl.predict(X)
        gdf['prediction'] = preds
        out = shp_path.replace('.shp', '_predicted.shp')
        gdf.to_file(out)
        messagebox.showinfo("Done", f"Saved to {out}")

# ✅ Entry point for MAIN3.exe dispatcher
def main():
    root = tk.Tk()
    app = LinearRegressionApp(root)
    root.mainloop()
    return 0

if __name__ == "__main__":
    main()