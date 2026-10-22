@echo off
setlocal
rem Extracts every .vol in this folder (and its subfolders) using wiivol.py.
rem You can also drag folders or .vol files onto this file to extract those instead.
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

rem --- Extract ---------------------------------------------------------------
if "%~1"=="" (
    %PY% "%~dp0wiivol.py" "%~dp0."
) else (
    %PY% "%~dp0wiivol.py" %*
)
echo.
echo Done.
pause
