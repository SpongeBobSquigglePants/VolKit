#!/bin/sh
# Packs the edits in VolExtractor's _extracted folders back into .vol files, using volpack.py.
# Pass the folders:  ./pack_all.sh Boot_extracted menus_extracted
# With none, every _extracted folder next to this script is packed. Each new archive is <name>_new.vol.
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
    found=
    for d in "$DIR"/*_extracted; do
        [ -d "$d" ] || continue
        found=1
        "$PY" "$DIR/volpack.py" "$d"
    done
    [ -n "$found" ] || echo "No _extracted folders here. Pass one:  ./pack_all.sh Boot_extracted"
else
    "$PY" "$DIR/volpack.py" "$@"
fi
