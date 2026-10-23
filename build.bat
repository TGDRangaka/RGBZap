@echo off
rem Build RGBZap for release (no Python needed to run the result). Produces, in dist\:
rem   RGBZap\RGBZap.exe              - the app folder
rem   RGBZap-<ver>-windows.zip       - portable: unzip anywhere and run
rem   RGBZap-Setup-<ver>.exe         - installer (only if Inno Setup 6 is installed)
rem
rem First time:  py -m pip install -r requirements-dev.txt
rem              (optional) install Inno Setup 6 for the installer: winget install JRSoftware.InnoSetup
rem Then:        build.bat

setlocal
set PY=py
where %PY% >nul 2>nul || set PY=python

rem version comes from APP_VERSION in elk_gui.py, so it's set in one place
for /f %%v in ('%PY% -c "import re;print(re.search('^APP_VERSION = .(.+?).$', open('elk_gui.py').read(), re.M)[1])"') do set VERSION=%%v
if "%VERSION%"=="" (echo Could not read APP_VERSION from elk_gui.py & exit /b 1)
echo Building RGBZap %VERSION%

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
if errorlevel 1 (echo PyInstaller failed & exit /b 1)

powershell -NoProfile -Command "Compress-Archive -Force -Path dist\RGBZap -DestinationPath dist\RGBZap-%VERSION%-windows.zip"
if errorlevel 1 (echo Zipping failed & exit /b 1)

set ISCC=
for %%p in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe" "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe") do if exist %%p set ISCC=%%p
if defined ISCC (
  %ISCC% /Q /DAppVersion=%VERSION% installer\RGBZap.iss
  if errorlevel 1 (echo Installer build failed & exit /b 1)
) else (
  echo Inno Setup 6 not found - skipped the installer. Install it: winget install JRSoftware.InnoSetup
)

echo.
echo Done. In dist\: RGBZap\RGBZap.exe, RGBZap-%VERSION%-windows.zip
if defined ISCC echo                RGBZap-Setup-%VERSION%.exe
endlocal
