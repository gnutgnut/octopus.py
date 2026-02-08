#!/bin/bash
# Install cron jobs for Octopus Energy tracker
# - demand check every minute (lightweight GraphQL only)
# - full sync every 30 minutes (REST + GraphQL)
# - MOTD cache update every minute

DIR="$(cd "$(dirname "$0")" && pwd)"

DEMAND="* * * * * python3 ${DIR}/octopus.py -q demand"
SYNC="*/30 * * * * python3 ${DIR}/octopus.py -q sync"
MOTD="* * * * * python3 ${DIR}/octopus.py -q motd > /tmp/octobot-motd 2>/dev/null"

# Remove any existing octopus.py entries, then append new ones
(crontab -l 2>/dev/null | grep -v "octopus.py" ; echo "$DEMAND" ; echo "$SYNC" ; echo "$MOTD") | crontab -

# Install dynamic MOTD script
sudo cp "${DIR}/update-motd.sh" /etc/update-motd.d/50-octobot
sudo chmod 755 /etc/update-motd.d/50-octobot

# Seed the MOTD cache
python3 "${DIR}/octopus.py" -q motd > /tmp/octobot-motd 2>/dev/null || true

echo "Cron jobs installed:"
crontab -l | grep "octopus.py"
echo ""
echo "MOTD installed: /etc/update-motd.d/50-octobot"
