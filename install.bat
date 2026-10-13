@echo off
setlocal
cd /d "%~dp0"

rem Windows users who installed Python from python.org without checking
rem "Add python.exe to PATH" have no `python` command in cmd.exe, but the
rem `py` launcher is still registered system-wide (installed to
rem C:\Windows\py.exe independent of PATH) as long as "Install launcher for
rem all users" was left checked, which is the default. Try that first, then
rem fall back to whatever's actually on PATH.

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 install.py %*
    goto :eof
)

where python >nul 2>nul
if %errorlevel%==0 (
    python install.py %*
    goto :eof
)

where python3 >nul 2>nul
if %errorlevel%==0 (
    python3 install.py %*
    goto :eof
)

echo.
echo Could not find Python. Neither "py", "python", nor "python3" works from this prompt.
echo.
echo  1. Install Python from https://www.python.org/downloads/
echo  2. On the first installer screen, check "Add python.exe to PATH"
echo     (even without that, the "py" launcher should still work here —
echo      if it doesn't, try reinstalling with "Install launcher for all users" checked)
echo  3. Close and reopen this terminal, then run install.bat again
echo.
pause
exit /b 1
