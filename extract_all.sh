#!/bin/sh
# Extracts every .vol in this folder (and its subfolders) using wiivol.py.
# Or pass folders / .vol files:  ./extract_all.sh /path/to/game/files
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

if [ $# -eq 0 ]; then
    "$PY" "$DIR/wiivol.py" "$DIR"
else
    "$PY" "$DIR/wiivol.py" "$@"
fi
