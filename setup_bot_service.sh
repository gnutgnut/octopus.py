#!/bin/bash
# Install or remove the octopus-bot systemd user service.
# Usage: ./setup_bot_service.sh install
#        ./setup_bot_service.sh remove

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="octopus-bot"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/${SERVICE_NAME}.service"

install_service() {
    mkdir -p "$SERVICE_DIR"

    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Octopus Energy Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${DIR}
ExecStart=/usr/bin/python3 ${DIR}/octopus.py bot
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME"
    systemctl --user start "$SERVICE_NAME"

    echo "Service installed and started."
    systemctl --user status "$SERVICE_NAME" --no-pager
}

remove_service() {
    systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl --user disable "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$SERVICE_FILE"
    systemctl --user daemon-reload

    echo "Service removed."
}

case "${1:-}" in
    install)
        install_service
        ;;
    remove)
        remove_service
        ;;
    *)
        echo "Usage: $0 {install|remove}"
        exit 1
        ;;
esac
