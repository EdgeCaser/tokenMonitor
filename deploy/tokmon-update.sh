#!/usr/bin/env bash
# Pull latest tokmon code, reinstall the package, and restart the dashboard.
# Idempotent. Safe to run while ingest is in-flight (ingest is one-shot, won't
# be disrupted; serve restart is brief).
#
# Run on the Pi:
#   ~/tokmon-app/deploy/tokmon-update.sh
# Or via symlink installed by setup-pi.sh:
#   tokmon-update

set -euo pipefail

APP_DIR="${TOKMON_APP_DIR:-$HOME/tokmon-app}"

c_blue()  { printf "\033[1;36m%s\033[0m\n" "$*"; }
c_green() { printf "\033[1;32m%s\033[0m\n" "$*"; }
c_red()   { printf "\033[1;31m%s\033[0m\n" "$*" >&2; }

if [ ! -d "$APP_DIR/.git" ]; then
    c_red "No git checkout at $APP_DIR — did you install via deploy/install-on-pi.sh (rsync) instead of git clone?"
    exit 1
fi

cd "$APP_DIR"

c_blue "▶ git pull"
git pull --ff-only

c_blue "▶ pip install"
"$APP_DIR/.venv/bin/pip" install --quiet -e .

c_blue "▶ systemd reload + restart"
# Re-install systemd units in case they changed.
SYSTEMD_DIR="$HOME/.config/systemd/user"
sed "s|%h/tokmon-app|$APP_DIR|g" deploy/tokmon-serve.service  > "$SYSTEMD_DIR/tokmon-serve.service"
sed "s|%h/tokmon-app|$APP_DIR|g" deploy/tokmon-ingest.service > "$SYSTEMD_DIR/tokmon-ingest.service"
cp deploy/tokmon-ingest.timer "$SYSTEMD_DIR/tokmon-ingest.timer"
systemctl --user daemon-reload
systemctl --user restart tokmon-serve

c_green "▶ tokmon updated"
echo "    Force a fresh ingest:  systemctl --user start tokmon-ingest.service"
echo "    Tail serve logs:       journalctl --user -u tokmon-serve -f"
