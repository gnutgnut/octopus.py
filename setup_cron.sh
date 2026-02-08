#!/bin/bash
# Install cron jobs for Octopus Energy tracker
# - demand check every minute (lightweight GraphQL only)
# - full sync every 30 minutes (REST + GraphQL)

DIR="$(cd "$(dirname "$0")" && pwd)"

DEMAND="* * * * * python3 ${DIR}/octopus.py -q demand"
SYNC="*/30 * * * * python3 ${DIR}/octopus.py -q sync"

# Remove any existing octopus.py entries, then append new ones
(crontab -l 2>/dev/null | grep -v "octopus.py" ; echo "$DEMAND" ; echo "$SYNC") | crontab -

echo "Cron jobs installed:"
crontab -l | grep "octopus.py"
