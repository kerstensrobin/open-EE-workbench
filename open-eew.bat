@echo off
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
    set "PATH=%~dp0.venv\Scripts;%PATH%"
    .venv\Scripts\python.exe open_ee_workbench\app.py %*
) else (
    python open_ee_workbench\app.py %*
)
