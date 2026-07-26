@echo off
setlocal
if "%~1"=="" (
  echo Usage: load_shp_to_postgis.bat path\file.shp table_name EPSG
  echo Example: load_shp_to_postgis.bat data\sample_area.shp imported_area 5186
  pause
  exit /b 1
)
set SHP=%~1
set TABLE=%~2
set EPSG=%~3
if "%TABLE%"=="" set TABLE=imported_layer
if "%EPSG%"=="" set EPSG=5186
docker run --rm --network host -v "%cd%:/work" ghcr.io/osgeo/gdal:ubuntu-small-latest ogr2ogr -f PostgreSQL "PG:host=127.0.0.1 port=5432 dbname=neunggureongi user=neung password=neungpass" "/work/%SHP%" -nln public.%TABLE% -lco GEOMETRY_NAME=geom -lco FID=id -t_srs EPSG:%EPSG% -overwrite
pause
