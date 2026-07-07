@echo off
rem Launch the ELK-BLEDOM controller GUI with no console window (pythonw).
rem %~dp0 = folder this .bat lives in, so it works wherever the repo is.
start "" "C:\Users\USER\AppData\Local\Programs\Python\Python314\pythonw.exe" "%~dp0elk_gui.py"
