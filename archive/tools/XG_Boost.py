import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import geopandas as gpd
import pandas as pd
import numpy as np
from xgboost import XGBRegressor
from sklearn.preprocessing import StandardScaler, MinMaxScaler
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_squared_error, r2_score, mean_absolute_error
import joblib
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.pyplot as plt
import ctypes
import sqlalchemy
from sqlalchemy import text

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

APP_TITLE = "XGBoost Tool"

class XGBoostApp:
    def __init__(self, root):
        self.root = root
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.title(APP_TITLE)
        self.df = None
        self.model_dir = os.getcwd()
        self.report_path = os.path.join(self.model_dir, 'xgboost_report.pdf')
        self.create_widgets()

    def on_close(self):
        self.root.quit()
        self.root.destroy()
        import sys; sys.exit(0)

    def create_widgets(self):
        frame = ttk.Frame(self.root, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Button(frame, text="Load Shapefile", command=self.load_shapefile).grid(row=0, column=0, sticky=tk.W)
        ttk.Button(frame, text="Load from Database", command=self.load_from_database).grid(row=0, column=1, sticky=tk.W)

        self.vars_listbox = tk.Listbox(frame, selectmode=tk.MULTIPLE, exportselection=False, height=8)
        self.vars_listbox.grid(row=1, column=0, columnspan=2, sticky=tk.W+tk.E)
        ttk.Label(frame, text="Independent Variables").grid(row=2, column=0, sticky=tk.W)

        self.target_var = tk.StringVar()
        self.target_menu = ttk.Combobox(frame, textvariable=self.target_var, state='readonly')
        self.target_menu.grid(row=1, column=2)
        ttk.Label(frame, text="Dependent Variable").grid(row=2, column=2, sticky=tk.W)

        ttk.Label(frame, text="Scaler:").grid(row=3, column=0, sticky=tk.W)
        self.scaler_var = tk.StringVar(value="None")
        self.scaler_menu = ttk.Combobox(frame, textvariable=self.scaler_var, state='readonly',
                                        values=["None", "Standard", "MinMax"])
        self.scaler_menu.grid(row=3, column=1, sticky=tk.W)

        ttk.Label(frame, text="Model Directory:").grid(row=4, column=0, sticky=tk.W)
        self.model_dir_var = tk.StringVar(value=self.model_dir)
        ttk.Entry(frame, textvariable=self.model_dir_var, width=40).grid(row=4, column=1)
        ttk.Button(frame, text="Browse", command=self.browse_model_dir).grid(row=4, column=2)

        ttk.Label(frame, text="Report File:").grid(row=5, column=0, sticky=tk.W)
        self.report_path_var = tk.StringVar(value=self.report_path)
        ttk.Entry(frame, textvariable=self.report_path_var, width=40).grid(row=5, column=1)
        ttk.Button(frame, text="Browse", command=self.browse_report_file).grid(row=5, column=2)

        ttk.Button(frame, text="Train & Save", command=self.train_and_save).grid(row=6, column=0, columnspan=3, pady=(10,0))

        self.progress = tk.DoubleVar(value=0)
        ttk.Progressbar(frame, variable=self.progress, maximum=100).grid(row=7, column=0, columnspan=3, sticky=tk.W+tk.E)

        ttk.Button(frame, text="Run Saved Model", command=self.run_saved_model).grid(row=8, column=0, columnspan=3, pady=(10,0))

    def browse_model_dir(self):
        d = filedialog.askdirectory(initialdir=self.model_dir_var.get())
        if d:
            self.model_dir_var.set(d)

    def browse_report_file(self):
        p = filedialog.asksaveasfilename(defaultextension='.pdf', filetypes=[('PDF','*.pdf')],
                                         initialfile=os.path.basename(self.report_path_var.get()))
        if p:
            self.report_path_var.set(p)

    def load_shapefile(self):
        path = filedialog.askopenfilename(filetypes=[("Shapefiles","*.shp")])
        if not path:
            return
        gdf = gpd.read_file(path)
        df = pd.DataFrame(gdf.drop(columns='geometry')) if 'geometry' in gdf.columns else pd.DataFrame(gdf)
        numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
        if not numeric_cols:
            messagebox.showerror("Error", "No numeric columns found.")
            return
        self.df = df[numeric_cols]
        self.vars_listbox.delete(0, tk.END)
        for idx, col in enumerate(numeric_cols, start=1):
            self.vars_listbox.insert(tk.END, f"{idx}. {col}")
        self.target_menu['values'] = numeric_cols
        self.progress.set(0)

    def load_from_database(self):
        db_window = tk.Toplevel(self.root)
        db_window.title("Connect to Database")

        entries = {}
        fields = [("DB Type", "postgresql"),
                  ("Host", "aws-0-ap-southeast-1.pooler.supabase.com"),
                  ("Port", "6543"),
                  ("Database", "postgres"),
                  ("User", "postgres.btushbqshdmrcchbvepc"),
                  ("Password", "C97x2DhLxwARkrbX")]
        for i, (label, default) in enumerate(fields):
            ttk.Label(db_window, text=label).grid(row=i, column=0, sticky=tk.W, padx=5, pady=3)
            var = tk.StringVar(value=default)
            entry = ttk.Entry(db_window, textvariable=var, show="*" if label == "Password" else "")
            entry.grid(row=i, column=1, padx=5, pady=3)
            entries[label] = var

        ttk.Label(db_window, text="Schema").grid(row=len(fields)+1, column=0, sticky=tk.W, padx=5, pady=3)
        schema_var = tk.StringVar()
        schema_menu = ttk.Combobox(db_window, textvariable=schema_var, state="readonly")
        schema_menu.grid(row=len(fields)+1, column=1, padx=5, pady=3)

        ttk.Label(db_window, text="Table").grid(row=len(fields)+2, column=0, sticky=tk.W, padx=5, pady=3)
        table_var = tk.StringVar()
        table_menu = ttk.Combobox(db_window, textvariable=table_var, state="readonly")
        table_menu.grid(row=len(fields)+2, column=1, padx=5, pady=3)

        def load_schemas_and_tables():
            try:
                url = f"{entries['DB Type'].get()}://{entries['User'].get()}:{entries['Password'].get()}@" \
                      f"{entries['Host'].get()}:{entries['Port'].get()}/{entries['Database'].get()}"
                engine = sqlalchemy.create_engine(url)
                with engine.connect() as conn:
                    result = conn.execute(text(
                        "SELECT table_schema, table_name FROM information_schema.tables "
                        "WHERE table_type='BASE TABLE' ORDER BY table_schema, table_name"
                    ))
                    rows = result.fetchall()
                schema_table = pd.DataFrame(rows, columns=["schema", "table"])
                unique_schemas = schema_table['schema'].unique().tolist()
                schema_menu['values'] = unique_schemas

                def update_tables(event):
                    sel_schema = schema_var.get()
                    tables = schema_table[schema_table['schema'] == sel_schema]['table'].tolist()
                    table_menu['values'] = tables
                    if tables:
                        table_var.set(tables[0])

                schema_menu.bind("<<ComboboxSelected>>", update_tables)
                if unique_schemas:
                    schema_var.set(unique_schemas[0])
                    update_tables(None)
            except Exception as e:
                messagebox.showerror("Connection Error", str(e))

        ttk.Button(db_window, text="Connect", command=load_schemas_and_tables).grid(row=len(fields), column=0, columnspan=2, pady=8)

        def connect_and_load():
            try:
                url = f"{entries['DB Type'].get()}://{entries['User'].get()}:{entries['Password'].get()}@" \
                      f"{entries['Host'].get()}:{entries['Port'].get()}/{entries['Database'].get()}"
                engine = sqlalchemy.create_engine(url)
                df = pd.read_sql_table(table_name=table_var.get(), con=engine, schema=schema_var.get())
                df_numeric = df.select_dtypes(include=[np.number])
                if df_numeric.empty:
                    messagebox.showerror("Error", "No numeric columns in selected table.")
                    return
                self.df = df_numeric.copy()
                self.vars_listbox.delete(0, tk.END)
                for idx, col in enumerate(df_numeric.columns, start=1):
                    self.vars_listbox.insert(tk.END, f"{idx}. {col}")
                self.target_menu['values'] = df_numeric.columns.tolist()
                self.progress.set(0)
                db_window.destroy()
                messagebox.showinfo("Success", f"Loaded table '{table_var.get()}' from database.")
            except Exception as e:
                messagebox.showerror("Connection Error", str(e))

        ttk.Button(db_window, text="Load Table", command=connect_and_load).grid(row=len(fields)+4, column=0, columnspan=2, pady=18)

    def train_and_save(self):
        raw = [self.vars_listbox.get(i) for i in self.vars_listbox.curselection()]
        inds = [r.split('. ',1)[1] for r in raw]
        target = self.target_var.get()
        if self.df is None or not inds or not target:
            messagebox.showerror("Error", "Load data and select variables.")
            return

        df_clean = self.df[inds + [target]].dropna()
        if df_clean.empty:
            messagebox.showerror("Error", "No rows after dropping NA.")
            return

        X = df_clean[inds].values
        y = df_clean[target].values

        X_train_raw, X_test_raw, y_train, y_test = train_test_split(X, y, test_size=0.3, random_state=0)

        scaler_choice = self.scaler_var.get()
        scaler = None
        X_train, X_test = X_train_raw, X_test_raw
        if scaler_choice == "Standard":
            scaler = StandardScaler().fit(X_train_raw)
            X_train = scaler.transform(X_train_raw); X_test = scaler.transform(X_test_raw)
        elif scaler_choice == "MinMax":
            scaler = MinMaxScaler().fit(X_train_raw)
            X_train = scaler.transform(X_train_raw); X_test = scaler.transform(X_test_raw)

        mdl = XGBRegressor(
            objective='reg:squarederror',
            n_estimators=500,
            max_depth=6,
            learning_rate=0.1,
            subsample=0.8,
            colsample_bytree=0.8,
            tree_method='hist',
            n_jobs=-1,
            random_state=0
        )
        mdl.fit(X_train, y_train)
        preds = mdl.predict(X_test)

        imps = np.array(mdl.feature_importances_, dtype=float)
        imps = (imps - imps.min()) / (imps.max() - imps.min()) if imps.max() > imps.min() else np.zeros_like(imps)

        mse = mean_squared_error(y_test, preds)
        mae = mean_absolute_error(y_test, preds)
        rmse = np.sqrt(mse)
        r2 = r2_score(y_test, preds)

        model_dir = self.model_dir_var.get()
        os.makedirs(model_dir, exist_ok=True)
        base = os.path.join(model_dir, "xgboost")
        joblib.dump({'model': mdl, 'features': inds}, base + '.pkl')
        if scaler is not None:
            joblib.dump(scaler, base + '_scaler.pkl')

        report_path = self.report_path_var.get()
        pp = PdfPages(report_path)

        metrics_df = pd.DataFrame([{
            'MSE': mse, 'MAE': mae, 'RMSE': rmse, 'R²': r2
        }])
        fig, ax = plt.subplots(figsize=(6, 1.2))
        ax.axis('off')
        tbl = ax.table(cellText=np.round(metrics_df.values, 4),
                       colLabels=metrics_df.columns, cellLoc='center', loc='center')
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(10)
        ax.set_title('XGBoost — Metrics')
        pp.savefig(fig); plt.close(fig)

        mn, mx = min(y_test.min(), preds.min()), max(y_test.max(), preds.max())
        fig, ax = plt.subplots(figsize=(6,6))
        ax.scatter(y_test, preds, alpha=0.6)
        ax.plot([mn, mx], [mn, mx], 'k--')
        ax.set_title('Actual vs Predicted')
        ax.set_xlabel('Actual'); ax.set_ylabel('Predicted')
        pp.savefig(fig); plt.close(fig)

        fig, ax = plt.subplots(figsize=(10, max(2, 0.35*len(inds))))
        order = np.argsort(imps)
        ax.barh(np.array(inds)[order], imps[order])
        ax.set_xlabel('Importance (Gain | normalized)')
        ax.set_title('Feature Importance')
        plt.tight_layout()
        pp.savefig(fig); plt.close(fig)

        pp.close()
        self.progress.set(100)
        messagebox.showinfo("Done", f"Training complete.\nModel saved to:\n{base+'.pkl'}\nReport:\n{report_path}")

    def run_saved_model(self):
        mdl_file = filedialog.askopenfilename(initialdir=self.model_dir_var.get(),
                                              filetypes=[("Model files", "*.pkl")])
        if not mdl_file:
            return
        shp = filedialog.askopenfilename(filetypes=[("Shapefiles","*.shp")])
        if not shp:
            return

        gdf = gpd.read_file(shp)
        df = pd.DataFrame(gdf.drop(columns='geometry')) if 'geometry' in gdf.columns else pd.DataFrame(gdf)

        data = joblib.load(mdl_file)
        mdl, feats = data['model'], data['features']
        scaler_path = mdl_file.replace('.pkl', '_scaler.pkl')
        scaler = joblib.load(scaler_path) if os.path.exists(scaler_path) else None

        valid_feats = [c for c in feats if c in df.columns and pd.api.types.is_numeric_dtype(df[c])]
        if not valid_feats:
            messagebox.showerror("Error", "No valid numeric features for prediction.")
            return

        X = df[valid_feats].apply(pd.to_numeric, errors='coerce').fillna(0).values
        if scaler is not None:
            X = scaler.transform(X)
        preds = mdl.predict(X)
        preds = preds.flatten() if isinstance(preds, np.ndarray) and preds.ndim > 1 else preds

        gdf['prediction'] = preds
        out = shp.replace('.shp', '_pred.shp')
        gdf.to_file(out)
        messagebox.showinfo("Done", f"Results saved to {out}")

# ✅ Entry point for MAIN3.exe dispatcher
def main():
    root = tk.Tk()
    logo_path = r"D:\2025_PROJECTS\BLGF-GM_TEST\FOR TESTING\DCS_CODES - testing\icons\iGeoSys_Logo_Transparent.png"
    if os.path.exists(logo_path):
        try:
            img = tk.PhotoImage(file=logo_path)
            root.iconphoto(False, img)
        except Exception as e:
            print(f"⚠ Could not set icon: {e}")
    app = XGBoostApp(root)
    root.mainloop()
    return 0

if __name__ == '__main__':
    main()