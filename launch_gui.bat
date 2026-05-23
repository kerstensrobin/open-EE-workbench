@echo off
REM open-EE-workbench — launch the web GUI on Windows
REM Usage:  launch_gui.bat [workbench_name] [--browser] [--port N]
python "%~dp0app.py" %*
