@echo off
setlocal enabledelayedexpansion

REM === SUPABASE DATABASE CONNECTION SETTINGS ===
set PGHOST=@db.dhnjamnukkaxqtlwutiv.supabase.co
set PGPORT=5432
set PGUSER=postgres
set PGPASSWORD=MischiefManaged!
set PGDATABASE=postgres
set SCHEMA=CALAUAN_LAGUNA

REM === PATH TO YOUR SHAPEFILES ===
set SHAPEFILE_DIR=D:\2025_PROJECTS\BLGF_WEBAPP\SHAPEFILE\RPTdb\RPTdb\PER BRGY (Mock Data)

REM === LOOP THROUGH ALL SHAPEFILES IN DIRECTORY ===
for %%F in ("%SHAPEFILE_DIR%\*.shp") do (
    set "FILENAME=%%~nF"
    echo Uploading %%~nxF to !SCHEMA!.!FILENAME! ...

    ogr2ogr -f "PostgreSQL" ^
      PG:"host=%PGHOST% user=%PGUSER% dbname=%PGDATABASE% password=%PGPASSWORD% port=%PGPORT%" ^
      "%%F" ^
      -nln "%SCHEMA%.!FILENAME!" ^
      -nlt PROMOTE_TO_MULTI ^
      -lco GEOMETRY_NAME=geom ^
      -lco FID=id ^
      -overwrite

    echo Done uploading: !FILENAME!
)

echo.
echo ✅ All shapefiles uploaded.
pause
