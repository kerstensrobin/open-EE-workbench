#!/usr/bin/env bash
# open-EE-workbench — launch the web GUI from any working directory
# Usage:  ./launch_gui.sh [workbench_name] [--browser] [--port N]
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/app.py" "$@"
