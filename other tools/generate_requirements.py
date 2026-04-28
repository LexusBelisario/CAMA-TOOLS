import os
import re

folder = r"D:\2025_PROJECTS\BLGF-GM_TEST\FOR TESTING\DCS_CODES - testing"
imports = set()

for filename in os.listdir(folder):
    if filename.endswith(".py"):
        filepath = os.path.join(folder, filename)
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                match = re.match(r'^\s*(import|from)\s+([\w\.]+)', line)
                if match:
                    module = match.group(2).split('.')[0]
                    imports.add(module)

# Standard library modules to exclude
std_libs = {
    'os', 'sys', 're', 'math', 'time', 'json', 'datetime',
    'tkinter', 'subprocess', 'itertools', 'collections',
    'pathlib', 'random', 'typing', 'threading', 'logging',
    'unittest', 'functools', 'shutil', 'copy', 'traceback'
}

third_party = sorted(imports - std_libs)

# Save to requirements.txt
req_path = os.path.join(folder, "requirements.txt")
with open(req_path, "w") as f:
    for lib in third_party:
        f.write(f"{lib}\n")

print(f"✅ requirements.txt created at:\n{req_path}")
print("📦 Packages detected:")
print("\n".join(third_party))
