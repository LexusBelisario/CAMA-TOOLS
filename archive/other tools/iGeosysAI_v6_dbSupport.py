import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import geopandas as gpd
import pandas as pd
import numpy as np
from sklearn.linear_model import LinearRegression
from sklearn.ensemble import RandomForestRegressor
from sklearn.preprocessing import StandardScaler, MinMaxScaler
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_squared_error, r2_score, mean_absolute_error
from xgboost import XGBRegressor
import joblib
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.pyplot as plt
import ctypes
import sqlalchemy
from sqlalchemy import text

# Enable high DPI awareness on Windows
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

class ModelComparisonApp:
    def __init__(self, root):
        self.root = root
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.title("AI Model Comparison Tool")
        self.df = None
        self.model_dir = os.getcwd()
        self.report_path = os.path.join(self.model_dir, 'model_report.pdf')
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

        # Listbox for independent variables with feature indices
        self.vars_listbox = tk.Listbox(
            frame,
            selectmode=tk.MULTIPLE,
            exportselection=False,
            height=8
        )
        self.vars_listbox.grid(row=1, column=0, columnspan=2, sticky=tk.W+tk.E)
        ttk.Label(frame, text="Independent Variables").grid(row=2, column=0, sticky=tk.W)

        # Combobox for dependent variable
        self.target_var = tk.StringVar()
        self.target_menu = ttk.Combobox(
            frame,
            textvariable=self.target_var,
            state='readonly'
        )
        self.target_menu.grid(row=1, column=2)
        ttk.Label(frame, text="Dependent Variable").grid(row=2, column=2, sticky=tk.W)

        # Scaler selection
        ttk.Label(frame, text="Scaler:").grid(row=3, column=0, sticky=tk.W)
        self.scaler_var = tk.StringVar(value="None")
        self.scaler_menu = ttk.Combobox(
            frame,
            textvariable=self.scaler_var,
            state='readonly',
            values=["None", "Standard", "MinMax"]
        )
        self.scaler_menu.grid(row=3, column=1, sticky=tk.W)

        # Model directory
        ttk.Label(frame, text="Model Directory:").grid(row=4, column=0, sticky=tk.W)
        self.model_dir_var = tk.StringVar(value=self.model_dir)
        ttk.Entry(frame, textvariable=self.model_dir_var, width=40).grid(row=4, column=1)
        ttk.Button(frame, text="Browse", command=self.browse_model_dir).grid(row=4, column=2)

        # Report file path
        ttk.Label(frame, text="Report File:").grid(row=5, column=0, sticky=tk.W)
        self.report_path_var = tk.StringVar(value=self.report_path)
        ttk.Entry(frame, textvariable=self.report_path_var, width=40).grid(row=5, column=1)
        ttk.Button(frame, text="Browse", command=self.browse_report_file).grid(row=5, column=2)

        # Model checkboxes
        self.models = {
            'Linear Regression': tk.BooleanVar(value=True),
            'Random Forest': tk.BooleanVar(value=True),
            'XGBoost': tk.BooleanVar(value=True)
        }
        for i, (name, var) in enumerate(self.models.items(), start=6):
            ttk.Checkbutton(frame, text=name, variable=var).grid(row=i, column=0, sticky=tk.W)

        # Train & Compare button
        ttk.Button(
            frame,
            text="Train & Compare",
            command=self.train_compare
        ).grid(row=10, column=0, columnspan=3, pady=(10,0))

        # Progress bar
        self.progress = tk.DoubleVar(value=0)
        ttk.Progressbar(
            frame,
            variable=self.progress,
            maximum=100
        ).grid(row=11, column=0, columnspan=3, sticky=tk.W+tk.E)

        # Run Saved Model button
        ttk.Button(
            frame,
            text="Run Saved Model",
            command=self.run_saved_model
        ).grid(row=12, column=0, columnspan=3, pady=(10,0))

    def browse_model_dir(self):
        d = filedialog.askdirectory(initialdir=self.model_dir_var.get())
        if d:
            self.model_dir_var.set(d)

    def browse_report_file(self):
        p = filedialog.asksaveasfilename(
            defaultextension='.pdf',
            filetypes=[('PDF','*.pdf')],
            initialfile=os.path.basename(self.report_path_var.get())
        )
        if p:
            self.report_path_var.set(p)

    def load_shapefile(self):
        path = filedialog.askopenfilename(filetypes=[("Shapefiles","*.shp")])
        if not path:
            return
        gdf = gpd.read_file(path)
        df = pd.DataFrame(gdf.drop(columns='geometry'))
        numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
        self.df = df[numeric_cols]

        # Populate listbox with indices
        self.vars_listbox.delete(0, tk.END)
        for idx, col in enumerate(numeric_cols, start=1):
            self.vars_listbox.insert(tk.END, f"{idx}. {col}")

        self.target_menu['values'] = numeric_cols
        self.progress.set(0)

    def load_from_database(self):
        db_window = tk.Toplevel(self.root)
        db_window.title("Connect to Database")

        # Database config fields
        entries = {}
        fields = [("DB Type", "postgresql"), ("Host", "aws-0-ap-southeast-1.pooler.supabase.com"), ("Port", "6543"),
                ("Database", "postgres"), ("User", "postgres.btushbqshdmrcchbvepc"), ("Password", "C97x2DhLxwARkrbX")]
        for i, (label, default) in enumerate(fields):
            ttk.Label(db_window, text=label).grid(row=i, column=0, sticky=tk.W, padx=5, pady=3)
            var = tk.StringVar(value=default)
            entry = ttk.Entry(db_window, textvariable=var, show="*" if label == "Password" else "")
            entry.grid(row=i, column=1, padx=5, pady=3)
            entries[label] = var
        # Schema dropdown
        ttk.Label(db_window, text="Schema").grid(row=len(fields)+1, column=0, sticky=tk.W, padx=5, pady=3)
        schema_var = tk.StringVar()
        schema_menu = ttk.Combobox(db_window, textvariable=schema_var, state="readonly")
        schema_menu.grid(row=len(fields)+1, column=1, padx=5, pady=3)

        # Table dropdown
        ttk.Label(db_window, text="Table").grid(row=len(fields)+2, column=0, sticky=tk.W, padx=5, pady=3)
        table_var = tk.StringVar()
        table_menu = ttk.Combobox(db_window, textvariable=table_var, state="readonly")
        table_menu.grid(row=len(fields)+2, column=1, padx=5, pady=3)

        def load_schemas_and_tables():
            try:
                db_type = entries["DB Type"].get()
                host = entries["Host"].get()
                port = entries["Port"].get()
                database = entries["Database"].get()
                user = entries["User"].get()
                password = entries["Password"].get()
                url = f"{db_type}://{user}:{password}@{host}:{port}/{database}"
                print(f"Connecting to: {url}")

                engine = sqlalchemy.create_engine(url)

                with engine.connect() as conn:
                    result = conn.execute(text(
                        "SELECT table_schema, table_name FROM information_schema.tables "
                        "WHERE table_type='BASE TABLE' ORDER BY table_schema, table_name"
                    ))
                    rows = result.fetchall()
                    print(f"Found {len(rows)} tables.")
                    schema_table = pd.DataFrame(rows, columns=["schema", "table"])
                    print(schema_table.head())

                # Populate schema dropdown
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

                #messagebox.showinfo("Connected", "Schemas and tables loaded.")
            except Exception as e:
                import traceback
                traceback.print_exc()
                messagebox.showerror("Connection Error", str(e))

        ttk.Button(db_window, text="Connect", command=load_schemas_and_tables).grid(
            row=len(fields), column=0, columnspan=2, pady=8)

        def connect_and_load():
            try:
                db_type = entries["DB Type"].get()
                host = entries["Host"].get()
                port = entries["Port"].get()
                database = entries["Database"].get()
                user = entries["User"].get()
                password = entries["Password"].get()
                schema = schema_var.get()
                table = table_var.get()

                url = f"{db_type}://{user}:{password}@{host}:{port}/{database}"
                engine = sqlalchemy.create_engine(url)
                df = pd.read_sql_table(table_name=table, con=engine, schema=schema)
                df_numeric = df.select_dtypes(include=[np.number])

                self.df = df_numeric

                self.vars_listbox.delete(0, tk.END)
                for idx, col in enumerate(df_numeric.columns, start=1):
                    self.vars_listbox.insert(tk.END, f"{idx}. {col}")

                self.target_menu['values'] = df_numeric.columns.tolist()
                self.progress.set(0)
                db_window.destroy()
                messagebox.showinfo("Success", f"Loaded table '{table}' from database.")

            except Exception as e:
                messagebox.showerror("Connection Error", str(e))

        ttk.Button(db_window, text="Load Table", command=connect_and_load).grid(row=len(fields)+4, column=0, columnspan=2, pady=18)


    def train_compare(self):
        # Extract column names from "idx. name" labels
        raw = [self.vars_listbox.get(i) for i in self.vars_listbox.curselection()]
        inds = [r.split('. ',1)[1] for r in raw]
        target = self.target_var.get()
        if self.df is None or not inds or not target:
            messagebox.showerror("Error", "Load shapefile and select variables.")
            return

        df_clean = self.df[inds + [target]].dropna()
        X = df_clean[inds].values
        y = df_clean[target].values
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.3, random_state=0
        )

        # Apply chosen scaler
        scaler_choice = self.scaler_var.get()
        if scaler_choice == "Standard":
            scaler = StandardScaler().fit(X_train)
        elif scaler_choice == "MinMax":
            scaler = MinMaxScaler().fit(X_train)
        else:
            scaler = None
        if scaler:
            X_train = scaler.transform(X_train)
            X_test = scaler.transform(X_test)

        # Prepare report
        model_dir = self.model_dir_var.get()
        report_path = self.report_path_var.get()
        os.makedirs(model_dir, exist_ok=True)
        pp = PdfPages(report_path)
        def save_fig(fig):
            pp.savefig(fig)
            plt.close(fig)

        total_models = sum(1 for v in self.models.values() if v.get())
        done = 0
        preds_by_model = {}
        self.root.config(cursor="wait")
        self.root.update()

        metrics = []
        feature_importance_records = []

        for name, enabled in self.models.items():
            if not enabled.get():
                continue

            # Train and predict
            if name == 'Linear Regression':
                mdl = LinearRegression()
                mdl.fit(X_train, y_train)
                preds = mdl.predict(X_test)
                imps = np.abs((mdl.coef_ * np.std(X_train, axis=0)) / np.std(y_train))

            elif name == 'Random Forest':
                mdl = RandomForestRegressor(n_estimators=100, n_jobs=-1)
                mdl.fit(X_train, y_train)
                preds = mdl.predict(X_test)
                imps = mdl.feature_importances_

            elif name == 'XGBoost':
                mdl = XGBRegressor(objective='reg:squarederror', n_jobs=-1,
                                   tree_method='hist', verbosity=0)
                mdl.fit(X_train, y_train)
                preds = mdl.predict(X_test)
                imps = mdl.feature_importances_

            preds_by_model[name] = (y_test, preds)

            # store predictions for scatter
            preds_by_model[name] = (y_test, preds)

            # normalize importances
            imps = np.array(imps, dtype=float)
            if imps.max() - imps.min() > 0:
                imps = (imps - imps.min()) / (imps.max() - imps.min())
            else:
                imps = np.zeros_like(imps)

            # save model + scaler
            base = os.path.join(model_dir, name.replace(' ', '_').lower())
            joblib.dump({'model': mdl, 'features': inds}, base + '.pkl')
            if scaler:
                joblib.dump(scaler, base + '_scaler.pkl')

            # compute metrics
            mse = mean_squared_error(y_test, preds)
            mae = mean_absolute_error(y_test, preds)
            rmse = np.sqrt(mse)
            r2 = r2_score(y_test, preds)
            metrics.append({'Model': name, 'MSE': round(mse,2), 'MAE': round(mae,2),
                            'RMSE': round(rmse,2), 'R²': round(r2,2)})

            # per-model scatter and save
            fig, ax = plt.subplots()
            ax.scatter(y_test, preds, alpha=0.6)
            ax.plot([y_test.min(), y_test.max()], [y_test.min(), y_test.max()], 'k--')
            ax.set_title(f"{name}: Actual vs Predicted")
            ax.set_xlabel('Actual')
            ax.set_ylabel('Predicted')
            save_fig(fig)

            # feature importances PDF
            for feat, imp in zip(inds, imps):
                feature_importance_records.append({'Model': name, 'Feature': feat, 'Importance': imp})

            # update progress
            done += 1
            self.progress.set(done/total_models*100)
            self.root.update_idletasks()

        # summary metrics table
        metrics_df = pd.DataFrame(metrics)
        fig, ax = plt.subplots(figsize=(10, 0.5*len(metrics_df)))
        ax.axis('off')
        tbl = ax.table(cellText=metrics_df.values, colLabels=metrics_df.columns,
                       cellLoc='center', loc='center')
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(10)
        save_fig(fig)

        # combined scatter for PDF
        all_actual = np.concatenate([a for a,_ in preds_by_model.values()])
        all_pred   = np.concatenate([p for _,p in preds_by_model.values()])
        mn, mx = min(all_actual.min(), all_pred.min()), max(all_actual.max(), all_pred.max())
        scatter_fig, scatter_ax = plt.subplots(figsize=(6,6))
        for name, (a,p) in preds_by_model.items():
            scatter_ax.scatter(a, p, alpha=0.6, label=name)
        scatter_ax.plot([mn,mx], [mn,mx], 'k--')
        scatter_ax.set_title("Actual vs Predicted (All Models)")
        scatter_ax.set_xlabel('Actual')
        scatter_ax.set_ylabel('Predicted')
        scatter_ax.legend()
        save_fig(scatter_fig)

        # combined feature importance for PDF
        fi_df = pd.DataFrame(feature_importance_records)
        fig2, ax2 = plt.subplots(figsize=(10, 0.5 + 0.4*len(fi_df['Feature'].unique())))
        features = fi_df['Feature'].unique()
        models_list = fi_df['Model'].unique()
        x = np.arange(len(features))
        width = 0.8 / len(models_list)
        for i, model in enumerate(models_list):
            vals = [fi_df[(fi_df['Model']==model)&(fi_df['Feature']==f)]['Importance'].values[0]
                    for f in features]
            ax2.bar(x + i*width, vals, width, label=model)
        ax2.set_xticks(x + width*(len(models_list)-1)/2)
        ax2.set_xticklabels(features, rotation=30, ha='right')
        ax2.set_xlabel("Features")
        ax2.set_ylabel("Importance")
        ax2.legend()
        plt.tight_layout()
        save_fig(fig2)

        # finalize PDF
        pp.close()
        self.root.config(cursor="")
        self.progress.set(100)

        # Summary window
        summary_win = tk.Toplevel(self.root)
        summary_win.title("Model Performance Summary")
        summary_win.geometry("1200x800")
        summary_win.grid_rowconfigure(0, weight=1)
        summary_win.grid_rowconfigure(1, weight=1)
        summary_win.grid_rowconfigure(2, weight=1)
        summary_win.grid_columnconfigure(0, weight=1)

        # Performance table
        style = ttk.Style(summary_win)
        style.configure("Treeview", rowheight=42, font=("Segoe UI", 10))
        cols = ("Model","MSE","MAE","RMSE","R²")
        tree = ttk.Treeview(summary_win, columns=cols, show="headings")
        tree.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        vsb = ttk.Scrollbar(summary_win, orient="vertical", command=tree.yview)
        vsb.grid(row=0, column=1, sticky="ns", pady=10)
        tree.configure(yscrollcommand=vsb.set)
        for c in cols:
            tree.heading(c, text=c)
            tree.column(c, anchor=tk.CENTER, stretch=True)
        for m in metrics:
            tree.insert("", tk.END, values=(m['Model'], f"{m['MSE']:,}", f"{m['MAE']:,}", f"{m['RMSE']:,}", f"{m['R²']:.2f}"))

        # Scatter plot for actual vs predicted
        all_actual = np.concatenate([a for a,_ in preds_by_model.values()])
        all_pred   = np.concatenate([p for _,p in preds_by_model.values()])
        mn, mx = min(all_actual.min(), all_pred.min()), max(all_actual.max(), all_pred.max())
        scatter_fig, scatter_ax = plt.subplots(figsize=(6,6))
        for name, (a,p) in preds_by_model.items():
            scatter_ax.scatter(a, p, alpha=0.6, label=name)
        scatter_ax.plot([mn,mx], [mn,mx], 'k--')
        scatter_ax.set_title("Actual vs Predicted (All Models)")
        scatter_ax.set_xlabel('Actual')
        scatter_ax.set_ylabel('Predicted')
        scatter_ax.legend()
        canvas_sc = FigureCanvasTkAgg(scatter_fig, master=summary_win)
        canvas_sc.draw()
        canvas_sc.get_tk_widget().grid(row=1, column=0, sticky="nsew", padx=10, pady=10)

        # Feature importance bar chart
        fi_df = pd.DataFrame(feature_importance_records)
        fig2, ax2 = plt.subplots(figsize=(10, 0.5 + 0.4*len(fi_df['Feature'].unique())))
        features = fi_df['Feature'].unique()
        models_list = fi_df['Model'].unique()
        x = np.arange(len(features))
        width = 0.8 / len(models_list)
        for i, model in enumerate(models_list):
            vals = [
                fi_df[(fi_df['Model']==model)&(fi_df['Feature']==f)]['Importance'].values[0]
                if f in fi_df['Feature'].values else 0
                for f in features
            ]
            ax2.bar(x + i*width, vals, width, label=model)
        ax2.set_xticks(x + width*(len(models_list)-1)/2)
        ax2.set_xticklabels(features, rotation=30, ha='right')
        ax2.set_xlabel("Features")
        ax2.set_ylabel("Importance")
        ax2.legend()
        plt.tight_layout()
        canvas_fi = FigureCanvasTkAgg(fig2, master=summary_win)
        canvas_fi.draw()
        canvas_fi.get_tk_widget().grid(row=2, column=0, sticky="nsew", padx=10, pady=10)

        messagebox.showinfo("Done", f"Training complete. Report saved to {self.report_path}")

    def run_saved_model(self):
        mdl_file = filedialog.askopenfilename(
            initialdir=self.model_dir_var.get(),
            filetypes=[("Model files", "*.pkl *.h5")]
        )
        if not mdl_file:
            return
        shp = filedialog.askopenfilename(filetypes=[("Shapefiles","*.shp")])
        if not shp:
            return

        gdf = gpd.read_file(shp)
        df = pd.DataFrame(gdf.drop(columns='geometry'))

        # Load model and features
        if mdl_file.endswith('.pkl'):
            data = joblib.load(mdl_file)
            mdl, feats = data['model'], data['features']
            # try load scaler
            scaler_path = mdl_file.replace('.pkl', '_scaler.pkl')
            scaler = joblib.load(scaler_path) if os.path.exists(scaler_path) else None
        else:
            mdl = load_model(mdl_file)
            feats = joblib.load(os.path.join(self.model_dir_var.get(), 'nn_features.pkl'))['features']
            scaler = None

        valid_feats = [c for c in feats if c in df.columns and pd.api.types.is_numeric_dtype(df[c])]
        if not valid_feats:
            messagebox.showerror("Error", "No valid numeric features for prediction.")
            return

        X = df[valid_feats].apply(pd.to_numeric, errors='coerce').fillna(0).values
        if scaler is not None:
            X = scaler.transform(X)

        preds = mdl.predict(X)
        if isinstance(preds, np.ndarray) and preds.ndim > 1:
            preds = preds.flatten()

        gdf['prediction'] = preds
        out = shp.replace('.shp', '_pred.shp')
        gdf.to_file(out)
        messagebox.showinfo("Done", f"Results saved to {out}")
        # self.on_close()

if __name__ == '__main__':
    root = tk.Tk()
    logo_path = r"D:\2025_PROJECTS\BLGF-GM_TEST\FOR TESTING\DCS_CODES - testing\icons\iGeoSys_Logo_Transparent.png"
    if os.path.exists(logo_path):
        img = tk.PhotoImage(file=logo_path)
        root.iconphoto(False, img)
    else:
        print(f"⚠ Logo file not found at {logo_path}, skipping icon setup.")
    app = ModelComparisonApp(root)
    root.mainloop()
