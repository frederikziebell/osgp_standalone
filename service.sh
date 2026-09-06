#!/usr/bin/env bash
# Installs/manages this tool as a systemd service.
#
# Usage: sudo ./service.sh install [config-file]
#        sudo ./service.sh start|stop|restart|status|uninstall
#
# The generated unit runs run.sh directly out of THIS checkout (whatever directory
# this script lives in when you run 'install') - there is no separate copy-to-/opt
# step. That means the checkout itself becomes the live deployment: don't delete or
# move it while the service is installed. To update, 'git pull' here and run
# './service.sh restart'. To relocate the checkout, run './service.sh uninstall'
# first, move the directory, then './service.sh install' again from the new location.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SERVICE_NAME="osgp-meter-reader"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
PLACEHOLDER_PASSWORD="YOUR_20_CHAR_ASCII_KEY_HERE"

usage() {
    echo "Usage: sudo $0 install [config-file]"
    echo "       sudo $0 start|stop|restart|uninstall"
    echo "       $0 status"
    exit 1
}

require_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "This command needs root - run it with sudo." >&2
        exit 1
    fi
}

require_systemd() {
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "systemctl not found - this installer needs a systemd-based Linux (e.g. Raspberry Pi OS)." >&2
        exit 1
    fi
}

cmd_install() {
    require_root
    require_systemd

    local config_file="${1:-config.properties}"
    local config_path
    if [[ "$config_file" = /* ]]; then
        config_path="$config_file"
    else
        config_path="$SCRIPT_DIR/$config_file"
    fi

    if [[ ! -f "$config_path" ]]; then
        if [[ -f "$SCRIPT_DIR/config.properties.example" ]]; then
            echo "No $config_path found - copying config.properties.example -> $config_path"
            cp "$SCRIPT_DIR/config.properties.example" "$config_path"
        fi
        echo "Edit $config_path and set your meter's password, then run 'sudo $0 install' again."
        exit 1
    fi
    if grep -q "$PLACEHOLDER_PASSWORD" "$config_path"; then
        echo "Please edit $config_path and set your real meter password before installing the service."
        exit 1
    fi

    # Run as the user who invoked sudo, not root - the reader only needs 'dialout'
    # group membership for /dev/ttyUSB0, and running arbitrary serial protocol code
    # as root is an unnecessary privilege.
    local run_user="${SUDO_USER:-}"
    if [[ -z "$run_user" || "$run_user" == "root" ]]; then
        echo "Run this via 'sudo' from the normal user account that should own the process" >&2
        echo "(the one already added to the 'dialout' group), not directly as root." >&2
        exit 1
    fi
    if ! id -nG "$run_user" 2>/dev/null | grep -qw dialout; then
        echo "Warning: user '$run_user' is not in the 'dialout' group yet - the service will"
        echo "fail to open the serial port until you run:"
        echo "  sudo usermod -a -G dialout $run_user"
        echo "and log '$run_user' out and back in (or reboot)."
    fi

    if [[ -f "$UNIT_PATH" ]]; then
        local existing_dir
        existing_dir="$(grep '^WorkingDirectory=' "$UNIT_PATH" | cut -d= -f2- || true)"
        if [[ -n "$existing_dir" && "$existing_dir" != "$SCRIPT_DIR" ]]; then
            echo "A '$SERVICE_NAME' service is already installed, pointing at a different checkout:" >&2
            echo "  $existing_dir" >&2
            echo "Run 'sudo $0 uninstall' there first, or here to take over, before installing" >&2
            echo "from this one." >&2
            exit 1
        fi
    fi

    cat > "$UNIT_PATH" <<EOF
[Unit]
Description=OSGP standalone smart meter reader
After=network.target

[Service]
Type=simple
User=$run_user
WorkingDirectory=$SCRIPT_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$SCRIPT_DIR/run.sh $config_file
Restart=on-failure
RestartSec=5
SyslogIdentifier=$SERVICE_NAME

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    echo
    echo "Installed '$SERVICE_NAME', running as user '$run_user' out of:"
    echo "  $SCRIPT_DIR"
    echo
    echo "This checkout is now the live deployment - don't delete or move it while the"
    echo "service is installed. 'git pull' here and 'sudo $0 restart' to update; run"
    echo "'sudo $0 uninstall' first if you need to relocate or remove this checkout."
    echo
    echo "Run 'sudo $0 start' to start it now, or reboot - it's enabled at boot already."
    echo "Logs: journalctl -u $SERVICE_NAME -f"
}

cmd_uninstall() {
    require_root
    require_systemd
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl disable "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$UNIT_PATH"
    systemctl daemon-reload
    echo "Uninstalled '$SERVICE_NAME'."
}

cmd_start()   { require_root; require_systemd; systemctl start "$SERVICE_NAME"; }
cmd_stop()    { require_root; require_systemd; systemctl stop "$SERVICE_NAME"; }
cmd_restart() { require_root; require_systemd; systemctl restart "$SERVICE_NAME"; }
cmd_status()  { require_systemd; systemctl status "$SERVICE_NAME" --no-pager; }

case "${1:-}" in
    install)   shift; cmd_install "$@" ;;
    start)     cmd_start ;;
    stop)      cmd_stop ;;
    restart)   cmd_restart ;;
    status)    cmd_status ;;
    uninstall) cmd_uninstall ;;
    *)         usage ;;
esac
