@echo off
setlocal
rem Packs the edits in VolExtractor's _extracted folders back into .vol files, using volpack.py.
rem Drag one or more <name>_extracted folders onto this file. With none, every _extracted folder here is packed.
rem Each new archive is written next to its folder as <name>_new.vol.
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

rem --- Pack ------------------------------------------------------------------
if "%~1"=="" (
    set "FOUND="
    for /d %%D in ("%~dp0*_extracted") do (
        set "FOUND=1"
        %PY% "%~dp0volpack.py" "%%D"
    )
    if not defined FOUND echo No _extracted folders here. Drag one onto this file.
) else (
    %PY% "%~dp0volpack.py" %*
)
echo.
echo Done.
pause
