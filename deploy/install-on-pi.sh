#!/usr/bin/env bash
# Convenience orchestrator: rsyncs the local tokenMonitor repo to the Pi, then
# runs setup-pi.sh remotely. Run from the laptop, in the repo root.
#
# Required env vars (or pass as flags):
#   TOKMON_PI_USER, TOKMON_PI_HOST
# Optional:
#   TOKMON_PI_APP_DIR   path on the Pi (default: /home/$TOKMON_PI_USER/tokmon-app)
#   TOKMON_CLIENT_HOSTS comma-separated laptop hostnames to seed roots for

set -euo pipefail

PI_USER="${TOKMON_PI_USER:-}"
PI_HOST="${TOKMON_PI_HOST:-}"
APP_DIR="${TOKMON_PI_APP_DIR:-}"
CLIENT_HOSTS="${TOKMON_CLIENT_HOSTS:-}"

usage() {
    cat <<USAGE
Usage: $0 --pi-user USER --pi-host HOST [--app-dir PATH] [--client-hosts h1,h2]

Rsyncs this repo to the Pi (excluding .venv, __pycache__, *.duckdb) and then
runs deploy/setup-pi.sh remotely.
USAGE
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --pi-user)       PI_USER="$2"; shift 2 ;;
        --pi-host)       PI_HOST="$2"; shift 2 ;;
        --app-dir)       APP_DIR="$2"; shift 2 ;;
        --client-hosts)  CLIENT_HOSTS="$2"; shift 2 ;;
        -h|--help)       usage ;;
        *) echo "unknown arg: $1" >&2; usage ;;
    esac
done

[ -z "$PI_USER" ] || [ -z "$PI_HOST" ] && usage
APP_DIR="${APP_DIR:-/home/$PI_USER/tokmon-app}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "▶ rsyncing $REPO_DIR → $PI_USER@$PI_HOST:$APP_DIR"
rsync -a --delete \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.duckdb' \
    --exclude='*.duckdb.wal' \
    --exclude='.pytest_cache' \
    --exclude='.git/objects/pack' \
    "$REPO_DIR/" "$PI_USER@$PI_HOST:$APP_DIR/"

echo "▶ running setup-pi.sh on $PI_USER@$PI_HOST"
ssh -t "$PI_USER@$PI_HOST" \
    "TOKMON_APP_DIR='$APP_DIR' TOKMON_CLIENT_HOSTS='$CLIENT_HOSTS' bash '$APP_DIR/deploy/setup-pi.sh'"

echo
echo "▶ Pi is provisioned. Now install this client:"
echo "    deploy/setup-client-macos.sh \\"
echo "        --pi-user $PI_USER --pi-host $PI_HOST --pi-path /home/$PI_USER"
