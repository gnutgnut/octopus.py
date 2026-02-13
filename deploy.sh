#!/bin/bash
# Auto-deploy: poll origin/master, pull if changed, syntax-check, restart bot, notify.
# Runs via cron every 3 minutes. Logs to deploy.log.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LOGFILE="$DIR/deploy.log"
ENV_FILE="$DIR/.env"

# Prevent overlapping runs
exec 9>"/tmp/octopus-deploy.lock"
flock -n 9 || exit 0

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOGFILE"; }

# Read Telegram creds from .env (using curl, not Python, so notifications
# work even if app code is broken)
TG_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d \"\')"
TG_CHAT="$(grep -E '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | cut -d= -f2- | tr -d \"\')"

notify() {
    [[ -z "$TG_TOKEN" || -z "$TG_CHAT" ]] && return 0
    curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
        -d chat_id="$TG_CHAT" -d text="$1" >> "$LOGFILE" 2>&1 || true
}

cd "$DIR"

# Fetch — silent exit on network failure (transient, don't spam Telegram)
git fetch origin master >> "$LOGFILE" 2>&1 || { log "fetch failed"; exit 0; }

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/master)
[[ "$LOCAL" == "$REMOTE" ]] && exit 0

BEHIND=$(git rev-list HEAD..origin/master --count)
CHANGES=$(git log --oneline HEAD..origin/master)
SHORT=$(echo "$REMOTE" | cut -c1-7)

log "Deploy: $BEHIND new commit(s)"

# Pull (ff-only for safety — rejects diverged history)
if ! git pull --ff-only origin master >> "$LOGFILE" 2>&1; then
    log "ERROR: pull failed"
    notify "$(printf '\xf0\x9f\x94\xb4 Deploy failed: pull error\nLocal: %s\nRemote: %s' \
        "$(echo "$LOCAL" | cut -c1-7)" "$SHORT")"
    exit 1
fi

# Syntax check all .py files before restarting anything
BAD=""
for f in "$DIR"/*.py; do
    python3 -m py_compile "$f" 2>> "$LOGFILE" || BAD="$BAD ${f##*/}"
done
if [[ -n "$BAD" ]]; then
    log "ERROR: syntax error in$BAD — rolling back"
    git reset --hard "$LOCAL" >> "$LOGFILE" 2>&1
    notify "$(printf '\xf0\x9f\x94\xb4 Deploy rolled back: syntax error in%s\n\n%s' "$BAD" "$CHANGES")"
    exit 1
fi

# Restart bot service
if ! systemctl --user restart octopus-bot >> "$LOGFILE" 2>&1; then
    log "ERROR: bot restart failed"
    notify "$(printf '\xf0\x9f\x9f\xa1 Deployed %s but bot restart failed\n\n%s' "$SHORT" "$CHANGES")"
    exit 1
fi

# Verify bot stayed up
sleep 2
if ! systemctl --user is-active octopus-bot > /dev/null 2>&1; then
    log "ERROR: bot not active after restart"
    notify "$(printf '\xf0\x9f\x9f\xa1 Deployed %s but bot crashed after restart\n\n%s' "$SHORT" "$CHANGES")"
    exit 1
fi

log "Deploy success: $SHORT"
notify "$(printf '\xf0\x9f\x9f\xa2 Deployed %s (%d commit%s)\n\n%s' \
    "$SHORT" "$BEHIND" "$([ "$BEHIND" -ne 1 ] && echo s)" "$CHANGES")"
