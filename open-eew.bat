@echo off
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
    set "PATH=%~dp0.venv\Scripts;%PATH%"
    .venv\Scripts\python.exe app.py %*
) else (
    python app.py %*
)
