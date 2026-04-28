# utils_paths.py (you already have something like this)
import sys
from pathlib import Path

def resource_path(relative: str) -> str:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return str((base / relative).resolve())
