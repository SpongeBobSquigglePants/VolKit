@echo off
setlocal
rem Opens VolKit, the window for looking inside .vol files and changing them.
rem You can also drag a .vol file onto this file to open it.
cd /d "%~dp0"

rem --- Find Python -----------------------------------------------------------
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY (
    python --version >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo Python 3 was not found.
    echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
    echo then run this file again.
    pause
    exit /b 1
)

rem --- Install Pillow if it is missing --------------------------------------
%PY% -c "import PIL" >nul 2>nul
if errorlevel 1 (
    echo Installing Pillow...
    %PY% -m pip install --user --disable-pip-version-check pillow
    %PY% -c "import PIL" >nul 2>nul
    if errorlevel 1 (
        echo Could not install Pillow. Try running:  %PY% -m pip install pillow
        pause
        exit /b 1
    )
)

rem --- Check Tkinter (it comes with Python from python.org) ------------------
%PY% -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo This Python has no Tkinter. Reinstall Python from https://www.python.org/downloads/
    echo and keep "tcl/tk and IDLE" ticked.
    pause
    exit /b 1
)

rem --- Open ------------------------------------------------------------------
set "PYW=%PY:python=pythonw%"
if "%PY%"=="py -3" set "PYW=pyw -3"
start "" %PYW% "%~dp0volkit.pyw" %*
