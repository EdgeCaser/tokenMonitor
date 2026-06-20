#!/usr/bin/env bash
# Installs tokmon as a sync client on Linux (cron variant of setup-client-macos.sh).

set -euo pipefail

PI_USER="${TOKMON_PI_USER:-}"
PI_HOST="${TOKMON_PI_HOST:-}"
PI_PATH="${TOKMON_PI_PATH:-}"

usage() {
    cat <<USAGE
Usage: $0 --pi-user USER --pi-host HOST --pi-path /home/USER
       (or set TOKMON_PI_USER / TOKMON_PI_HOST / TOKMON_PI_PATH)
USAGE
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --pi-user) PI_USER="$2"; shift 2 ;;
        --pi-host) PI_HOST="$2"; shift 2 ;;
        --pi-path) PI_PATH="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "unknown arg: $1" >&2; usage ;;
    esac
done

[ -z "$PI_USER" ] || [ -z "$PI_HOST" ] || [ -z "$PI_PATH" ] && usage

TOKMON_BIN="$(command -v tokmon || true)"
if [ -z "$TOKMON_BIN" ]; then
    echo "tokmon CLI not on PATH" >&2
    exit 1
fi

# SSH test
if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "$PI_USER@$PI_HOST" 'echo ok' >/dev/null 2>&1; then
    echo "SSH to $PI_USER@$PI_HOST failed" >&2
    exit 1
fi

# Write config
"$TOKMON_BIN" sync set --pi-user "$PI_USER" --pi-host "$PI_HOST" --pi-path "$PI_PATH"

# Install cron entry (every 10 min). Idempotent: removes any prior tokmon push line.
CRON_LINE="*/10 * * * * $TOKMON_BIN push >> \$HOME/.tokmon/sync.log 2>&1"
( crontab -l 2>/dev/null | grep -v "tokmon push" ; echo "$CRON_LINE" ) | crontab -

mkdir -p "$HOME/.tokmon"
"$TOKMON_BIN" push

echo "▶ done"
echo "    Pi dashboard:  http://$PI_HOST:8765/"
echo "    Log:           tail -f ~/.tokmon/sync.log"
echo "    Force a push:  tokmon push"
echo "    Remove cron:   crontab -e   # delete the tokmon push line"
