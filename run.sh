#!/usr/bin/env bash
set -euo pipefail

# Always run relative to this script's location, so it works regardless of
# where you call it from (cron, systemd, another shell, etc.).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CONFIG_FILE="${1:-config.properties}"
JAR_FILE="target/osgp-standalone.jar"

if [[ ! -f "$CONFIG_FILE" ]]; then
    if [[ -f "config.properties.example" ]]; then
        echo "No $CONFIG_FILE found - copying config.properties.example -> $CONFIG_FILE"
        cp config.properties.example "$CONFIG_FILE"
    fi
    echo "Edit $CONFIG_FILE and set your meter's password, then run this script again."
    exit 1
fi

if [[ ! -f "$JAR_FILE" ]]; then
    echo "$JAR_FILE not found - building it with Maven..."
    mvn -q package
fi

# `exec` replaces this shell with the java process, so Ctrl+C / systemd stop
# signals go straight to the JVM and its shutdown hook (clean logoff) runs.
exec java -jar "$JAR_FILE" "$CONFIG_FILE"