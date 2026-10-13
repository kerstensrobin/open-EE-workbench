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

where winget >nul 2>nul
if not %errorlevel%==0 goto :no_winget

set /p INSTALL_PY="Install Python automatically now via winget (Windows Package Manager)? [Y/n] "
if /i "%INSTALL_PY:~0,1%"=="n" goto :no_winget

echo.
echo Installing Python 3.12 via winget — this can take a minute...
winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements
if not %errorlevel%==0 (
    echo.
    echo winget install failed. Install Python manually from https://www.python.org/downloads/
    pause
    exit /b 1
)

rem winget installing Python doesn't refresh THIS cmd window's PATH. py.exe
rem normally lands in %WINDIR%, which is already on PATH either way, but
rem fall back to the default per-user install location directly if it's
rem still not visible from here.
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 install.py %*
    goto :eof
)

for /d %%d in ("%LocalAppData%\Programs\Python\Python3*") do (
    if exist "%%d\python.exe" (
        "%%d\python.exe" install.py %*
        goto :eof
    )
)

echo.
echo Python was installed but this window can't see it yet. Close this
echo terminal, open a new one, and run install.bat again.
pause
exit /b 1

:no_winget
echo  1. Install Python from https://www.python.org/downloads/
echo  2. On the first installer screen, check "Add python.exe to PATH"
echo     (even without that, the "py" launcher should still work here —
echo      if it doesn't, try reinstalling with "Install launcher for all users" checked)
echo  3. Close and reopen this terminal, then run install.bat again
echo.
pause
exit /b 1
