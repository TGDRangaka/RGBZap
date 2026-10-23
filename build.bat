@echo off
rem Build a standalone Windows app (no Python needed to run the result).
rem Produces dist\RGBZap\RGBZap.exe — zip the whole dist\RGBZap folder for a release.
rem
rem First time:  py -m pip install -r requirements-dev.txt
rem Then:        build.bat

setlocal
set PY=py
where %PY% >nul 2>nul || set PY=python

rem --onedir (not --onefile): starts fast and trips Windows Defender far less.
rem --windowed: no console window. --collect-all: bundle the dynamically-loaded
rem BLE / capture backends that PyInstaller can't see by static analysis.
%PY% -m PyInstaller ^
  --noconfirm --clean ^
  --name RGBZap ^
  --onedir --windowed ^
  --icon assets\rgbzap.ico ^
  --add-data "assets;assets" ^
  --add-data "LICENSE;." ^
  --add-data "THIRD_PARTY_NOTICES.md;." ^
  --collect-all bleak ^
  --collect-all mss ^
  --collect-all pystray ^
  --collect-all soundcard ^
  --collect-all winrt ^
  elk_gui.py

echo.
echo Done. Run dist\RGBZap\RGBZap.exe  (zip the dist\RGBZap folder to share it).
endlocal
