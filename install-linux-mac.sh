#!/usr/bin/env bash
# Mac / Linux installer. In a terminal, from this folder:
#
#     bash install-linux-mac.sh
#
# Arguments are passed on to src/installer/install.py, e.g.
#     bash install-linux-mac.sh --zeroconf y --desktop-entry y
cd "$(dirname "$0")" || exit 1

for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1 &&
       "$py" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        exec "$py" src/installer/install.py "$@"
    fi
done

echo
echo "Could not find Python 3.10 or newer. Install it first, then run this script again:"
echo "  Mac:            https://www.python.org/downloads/  (or: brew install python)"
echo "  Ubuntu/Debian:  sudo apt install python3 python3-venv"
echo "  Fedora:         sudo dnf install python3"
exit 1
