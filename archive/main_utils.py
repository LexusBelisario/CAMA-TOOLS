# === main_utils.py ===
import tkinter as tk
from PIL import Image, ImageTk
from rapidfuzz import process, fuzz
import re

# -------------------------------------------------------------------
# 🔹 Tooltip Function (for icon hover behavior)
# -------------------------------------------------------------------
def add_tooltip(widget, icon_path, title, subtitle="", canvas=None, bg_id=None):
    tooltip = tk.Toplevel(widget)
    tooltip.withdraw()
    tooltip.overrideredirect(True)
    tooltip.configure(bg="#e0e0e0", padx=1, pady=1)
    tooltip.attributes('-topmost', True)

    outer = tk.Frame(tooltip, bg="#fefefe", relief="solid", borderwidth=1)
    outer.pack()

    content = tk.Frame(outer, bg="#fefefe")
    content.pack(padx=8, pady=6)

    # 🟢 Create and keep a reference to the image
    icon = Image.open(icon_path).resize((20, 20), Image.Resampling.LANCZOS)
    icon_img = ImageTk.PhotoImage(icon, master=widget.winfo_toplevel())
    tooltip.icon_img = icon_img        # <— this line keeps the image alive

    icon_label = tk.Label(content, image=tooltip.icon_img, bg="#fefefe")
    icon_label.grid(row=0, column=0, rowspan=2, padx=(0, 6), pady=(2, 0), sticky="n")

    # 🔵 Title
    title_label = tk.Label(content, text=title.title(),
                           font=("Segoe UI", 10, "bold"), bg="#fefefe",
                           anchor="w", justify="left")
    title_label.grid(row=0, column=1, sticky="w")

    # 🔹 Subtitle
    subtitle_label = tk.Label(content, text=subtitle,
                              font=("Segoe UI", 9), bg="#fefefe",
                              anchor="w", justify="left")
    subtitle_label.grid(row=1, column=1, sticky="w")

    # === Tooltip hover logic ===
    def enter(event):
        x = widget.winfo_rootx() + 45
        y = widget.winfo_rooty() + 10
        tooltip.geometry(f"+{x}+{y}")
        tooltip.deiconify()
        tooltip.lift()
        tooltip.attributes("-topmost", True)
        if canvas and bg_id:
            canvas.itemconfig(bg_id, image=getattr(widget.master, "hover_bg", ""))

    def leave(event):
        tooltip.withdraw()
        if canvas and bg_id:
            canvas.itemconfig(bg_id, image="")

    widget.bind("<Enter>", enter)
    widget.bind("<Leave>", leave)


# -------------------------------------------------------------------
# 🔹 Name Normalization + Matching Utilities
# -------------------------------------------------------------------

# Common noisy suffixes
_NOISY_SUFFIXES = (
    "_shp", "_gpkg",
    "_line", "_lines", "_poly", "_polygon", "_polygons",
    "_point", "_points",
    "_multiline", "_multipolygon",
    "_layer", "_export", "_copy"
)

def _strip_noisy_suffixes(s: str) -> str:
    s = s.lower()
    for suf in _NOISY_SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s

def _normalize_name(name: str, schema_prefix: str = "") -> str:
    """
    Normalize names for matching:
    - remove text in parentheses
    - remove file extension
    - remove schema prefix like CALAUAN_LAGUNA_
    - lowercase, collapse non-alnum to underscores
    - remove noisy suffixes like _shp, _polygon, _export
    """
    name = name.strip()
    if "(" in name:
        name = name.split("(")[0]
    if "." in name:
        name = name.split(".")[0]
    if schema_prefix and name.lower().startswith(schema_prefix.lower()):
        name = name[len(schema_prefix):]
    name = re.sub(r"[^0-9a-zA-Z]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("_").lower()
    name = _strip_noisy_suffixes(name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name

def _name_tokens(name: str) -> list[str]:
    n = _normalize_name(name)
    tokens = [t for t in n.split("_") if t]
    if tokens and tokens[0].isdigit():
        return [t for t in tokens[1:] if t] or tokens
    return tokens

def _tokens_subset(a_tokens: list[str], b_tokens: list[str]) -> bool:
    return bool(a_tokens and b_tokens) and set(a_tokens).issubset(set(b_tokens))

def _find_best_table(layer_name: str, existing_tables: list, schema_prefix: str) -> str | None:
    """
    Smart match a layer to an existing table:
    1. exact normalized equality
    2. token-subset match
    3. substring match
    4. fuzzy match (token_set_ratio, partial_ratio)
    """
    norm_layer = _normalize_name(layer_name, schema_prefix=schema_prefix + "_")
    layer_tokens = _name_tokens(layer_name)

    norm_map = { _normalize_name(t): t for t in existing_tables }
    if norm_layer in norm_map:
        return norm_map[norm_layer]

    table_tokens_map = { nt: _name_tokens(nt) for nt in norm_map.keys() }

    for nt, orig_tbl in norm_map.items():
        if _tokens_subset(layer_tokens, table_tokens_map[nt]) or _tokens_subset(table_tokens_map[nt], layer_tokens):
            return orig_tbl

    for nt, orig_tbl in norm_map.items():
        if norm_layer in nt or nt in norm_layer:
            return orig_tbl

    if norm_map:
        choices = list(norm_map.keys())
        best1, sc1, _ = process.extractOne(norm_layer, choices, scorer=fuzz.token_set_ratio)
        if sc1 >= 90:
            return norm_map[best1]
        best2, sc2, _ = process.extractOne(norm_layer, choices, scorer=fuzz.partial_ratio)
        if sc2 >= 90:
            return norm_map[best2]

    return None
