@echo off
:: =============================================================
:: build.bat  —  CAMA-Tools EXE builder
:: Place this file in the SAME folder as MAIN3.py + CAMA-Tools.spec
:: Double-click OR run from cmd inside that folder.
:: =============================================================

setlocal

:: ── 1. Confirm we are in the right folder ─────────────────────
if not exist "MAIN3.py" (
    echo.
    echo  ERROR: MAIN3.py not found in this folder.
    echo  Please run build.bat from the folder that contains MAIN3.py.
    echo.
    pause
    exit /b 1
)

if not exist "CAMA-Tools.spec" (
    echo.
    echo  ERROR: CAMA-Tools.spec not found in this folder.
    echo  Please copy CAMA-Tools.spec here alongside MAIN3.py.
    echo.
    pause
    exit /b 1
)

if not exist "utils_paths.py" (
    echo.
    echo  ERROR: utils_paths.py not found. It is imported by MAIN3.py.
    echo.
    pause
    exit /b 1
)

:: ── 2. Kill any running instance + clean build artifacts ───────
echo.
echo  [1/4] Killing any running CAMA-Tools instance...
taskkill /f /im "CAMA-Tools.exe" >nul 2>&1
timeout /t 2 /nobreak >nul

echo  Cleaning previous build...
if exist "build"             rmdir /s /q "build"
if exist "dist"              rmdir /s /q "dist"
if exist "__pycache__"       rmdir /s /q "__pycache__"
if exist "tools\__pycache__" rmdir /s /q "tools\__pycache__"

:: ── 3. Run PyInstaller ────────────────────────────────────────
echo.
echo  [2/4] Running PyInstaller...
echo  (this may take several minutes on first run)
echo.

pyinstaller CAMA-Tools.spec --clean --noconfirm

if errorlevel 1 (
    echo.
    echo  ================================================
    echo   BUILD FAILED — check the output above for errors
    echo  ================================================
    echo.
    echo  Common fixes:
    echo   - Run:  pip install pyinstaller  (if not installed)
    echo   - If a package is missing, install it then rebuild
    echo   - If UPX errors appear, open CAMA-Tools.spec and
    echo     set  upx=False
    echo.
    pause
    exit /b 1
)

:: ── 4. Copy runtime files next to the exe ────────────────────
echo.
echo  [3/4] Copying runtime files to dist\...

if exist "pg_credentials.json" (
    copy /y "pg_credentials.json" "dist\pg_credentials.json" > nul
    echo  + pg_credentials.json
) else (
    echo  WARNING: pg_credentials.json not found here.
    echo           Users must supply it next to the exe.
)

if exist "gm_exe_path.json" (
    copy /y "gm_exe_path.json" "dist\gm_exe_path.json" > nul
    echo  + gm_exe_path.json
)

:: ── 5. Done ──────────────────────────────────────────────────
echo.
echo  [4/4] Build complete!
echo.
echo  Output:  dist\CAMA-Tools.exe
echo.
echo  -------------------------------------------------------
echo  DEPLOYMENT — copy these to the target machine (same folder):
echo.
echo    dist\CAMA-Tools.exe        <- the application
echo    pg_credentials.json        <- DB connection config
echo.
echo  On first run on a new machine the app will ask the user
echo  to locate global_mapper.exe and save gm_exe_path.json
echo  automatically beside the exe.
echo  -------------------------------------------------------
echo.
pause
