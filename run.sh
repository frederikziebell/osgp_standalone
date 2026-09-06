#!/usr/bin/env bash
set -euo pipefail

# Always run relative to this script's location, so it works regardless of
# where you call it from (cron, systemd, another shell, etc.).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CONFIG_FILE="${1:-config.properties}"

if [[ ! -f "$CONFIG_FILE" ]]; then
    if [[ -f "config.properties.example" ]]; then
        echo "No $CONFIG_FILE found - copying config.properties.example -> $CONFIG_FILE"
        cp config.properties.example "$CONFIG_FILE"
    fi
    echo "Edit $CONFIG_FILE and set your meter's password, then run this script again."
    exit 1
fi

# Prefer a local virtualenv if one exists, otherwise fall back to system python3.
if [[ -x "venv/bin/python3" ]]; then
    PYTHON="venv/bin/python3"
else
    PYTHON="python3"
fi

if ! "$PYTHON" -c "import serial" 2>/dev/null; then
    echo "pyserial is not installed for $PYTHON."
    echo "Install it with:  sudo apt install python3-serial"
    echo "             or:  $PYTHON -m pip install -r requirements.txt"
    exit 1
fi

# `exec` replaces this shell with the python process, so Ctrl+C / systemd stop
# signals go straight to it and the clean logoff runs.
exec "$PYTHON" main.py "$CONFIG_FILE"
