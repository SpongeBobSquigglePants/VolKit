#!/bin/sh
# Opens VolKit, the window for looking inside .vol files and changing them.
#   ./volkit.sh [file.vol]
DIR=$(cd "$(dirname "$0")" && pwd)

if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else
    echo "Python 3 was not found. Install it from https://www.python.org/downloads/ and run this again."
    exit 1
fi

if ! "$PY" -c "import PIL" >/dev/null 2>&1; then
    echo "Installing Pillow..."
    "$PY" -m pip install --user --disable-pip-version-check pillow \
        || "$PY" -m pip install --user --break-system-packages --disable-pip-version-check pillow
    if ! "$PY" -c "import PIL" >/dev/null 2>&1; then
        echo "Could not install Pillow. Try running:  $PY -m pip install pillow"
        exit 1
    fi
fi

if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
    echo "This Python has no Tkinter. On Linux, install it with your package manager"
    echo "(for example: sudo apt install python3-tk); on a Mac, use Python from python.org."
    exit 1
fi

exec "$PY" "$DIR/volkit.pyw" "$@"
