@echo off
setlocal
cd /d "%~dp0"

rem Windows installer: double-click this file. It finds Python (or offers to
rem install it), then opens the installer window and closes this one.
rem   install-windows.bat --zeroconf y --usb-driver n   (any arguments) runs
rem   the installer here in the console instead, with its prompts pre-answered.
rem
rem Keep this file pure ASCII: cmd.exe reads .bat files in the OEM codepage,
rem so UTF-8 characters such as an em dash are echoed as garbage.

call :find_python
if defined PY goto :have_python

echo.
echo Could not find Python. Neither "py", "python", nor "python3" works from this prompt.
echo.

where winget >nul 2>nul
if not %errorlevel%==0 goto :no_winget

set /p INSTALL_PY="Install Python automatically now via winget (Windows Package Manager)? [Y/n] "
if /i "%INSTALL_PY:~0,1%"=="n" goto :no_winget

echo.
echo Installing Python 3.12 via winget - this can take a minute...
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
call :find_python
if not defined PY (
    for /d %%d in ("%LocalAppData%\Programs\Python\Python3*") do (
        if exist "%%d\python.exe" set "PY=%%d\python.exe"
    )
)
if defined PY goto :have_python

echo.
echo Python was installed but this window can't see it yet. Close this
echo window and double-click install-windows.bat again.
pause
exit /b 1

:have_python
if not "%~1"=="" goto :console

rem The installer window needs tkinter (part of every python.org install)
rem and pythonw.exe, so no console window stays open behind it.
"%PY%" -c "import tkinter" >nul 2>nul
if not %errorlevel%==0 goto :console
for %%i in ("%PY%") do set "PYW=%%~dpipythonw.exe"
if not exist "%PYW%" goto :console
start "" "%PYW%" src\installer\install_gui.pyw
exit /b 0

:console
"%PY%" src\installer\install.py %*
set "RC=%errorlevel%"
if "%~1"=="" pause
exit /b %RC%

rem Sets PY to the full path of a working Python 3, or leaves it undefined.
rem Windows users who installed Python from python.org without checking
rem "Add python.exe to PATH" have no `python` command in cmd.exe, but the
rem `py` launcher is still registered system-wide (installed to
rem C:\Windows\py.exe independent of PATH) as long as "Install launcher for
rem all users" was left checked, which is the default. Try that first, then
rem fall back to whatever's actually on PATH. The Microsoft Store "python"
rem alias prints nothing to stdout, so it is skipped automatically.
:find_python
set "PY="
for %%c in ("py -3" python python3) do (
    if not defined PY (
        for /f "delims=" %%i in ('%%~c -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%i"
    )
)
if defined PY if not exist "%PY%" set "PY="
exit /b

:no_winget
echo  1. Install Python from https://www.python.org/downloads/
echo  2. On the first installer screen, check "Add python.exe to PATH"
echo     (even without that, the "py" launcher should still work here -
echo      if it doesn't, try reinstalling with "Install launcher for all users" checked)
echo  3. Double-click install-windows.bat again
echo.
pause
exit /b 1
