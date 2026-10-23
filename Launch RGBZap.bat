@echo off
rem Launch the controller GUI from source with no console window.
rem Uses the Windows 'py' launcher (pythonw) so it works on any machine with
rem Python installed. %~dp0 = this .bat's folder, so it runs wherever the repo is.
rem (If you have the built RGBZap.exe, just run that instead — no Python needed.)
start "" pyw "%~dp0elk_gui.py" 2>nul || start "" pythonw "%~dp0elk_gui.py"
