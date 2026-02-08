#!/bin/sh
# Dynamic MOTD: display cached Octobot status
# Cached by cron: * * * * * python3 /home/jay/octopus/octopus.py -q motd > /tmp/octobot-motd
MOTD_FILE="/tmp/octobot-motd"
if [ -f "$MOTD_FILE" ]; then
    echo ""
    cat "$MOTD_FILE"
    echo ""
fi
