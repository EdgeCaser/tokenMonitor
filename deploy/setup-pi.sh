#!/usr/bin/env bash
# Provisions tokmon on a Raspberry Pi as the central ingest + dashboard host.
# Idempotent: re-runnable to update.
#
# Run from the laptop:
#   ./deploy/install-on-pi.sh        # wraps source-sync + this script
# Or on the Pi directly, after rsync'ing the repo to ~/tokmon-app:
#   bash ~/tokmon-app/deploy/setup-pi.sh
#
# Knobs (env vars):
#   TOKMON_APP_DIR     where the source lives on the Pi (default: ~/tokmon-app)
#   TOKMON_PORT        dashboard port (default: 8765)
#   TOKMON_PYTHON      python binary to use for the venv (default: python3)
#   TOKMON_CLIENT_HOSTS comma-separated list of laptop hostnames to seed roots
#                       for (default: none — `tokmon ingest` will still pick up
#                       whatever lands under ~/sync/*/.claude/projects/ since
#                       the setup also adds those roots automatically).

set -euo pipefail

APP_DIR="${TOKMON_APP_DIR:-$HOME/tokmon-app}"
PORT="${TOKMON_PORT:-8765}"
PY_BIN="${TOKMON_PYTHON:-python3}"
CLIENT_HOSTS="${TOKMON_CLIENT_HOSTS:-}"

c_blue() { printf "\033[1;36m%s\033[0m\n" "$*"; }
c_green() { printf "\033[1;32m%s\033[0m\n" "$*"; }
c_red() { printf "\033[1;31m%s\033[0m\n" "$*" >&2; }

c_blue "▶ tokmon Pi setup"

# --- 1. Python version --------------------------------------------------------
PY_VER="$($PY_BIN -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
PY_MAJOR="${PY_VER%%.*}"
PY_MINOR="${PY_VER##*.}"
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    c_red "Python ≥ 3.11 required (found $PY_VER). Install via apt or pyenv first."
    exit 1
fi
c_blue "  python: $PY_VER ✓"

# --- 2. Source dir ------------------------------------------------------------
if [ ! -d "$APP_DIR" ]; then
    c_red "Source not found at $APP_DIR. Run install-on-pi.sh from the laptop,"
    c_red "or rsync the repo here first:"
    c_red "  rsync -av --exclude=.venv --exclude=__pycache__ ./ pi:~/tokmon-app/"
    exit 1
fi
c_blue "  source: $APP_DIR ✓"

# --- 3. Venv + install --------------------------------------------------------
cd "$APP_DIR"
if [ ! -d .venv ]; then
    c_blue "  creating venv…"
    "$PY_BIN" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -e .
c_green "  pip install -e . ✓"

# --- 4. Data dirs -------------------------------------------------------------
mkdir -p "$HOME/.tokmon" "$HOME/sync"
c_blue "  data dirs ✓"

# --- 5. Seed extra roots ------------------------------------------------------
# Anything already under ~/sync/<host>/.claude/projects/ becomes a root.
# Plus any explicitly-named hosts from TOKMON_CLIENT_HOSTS.
shopt -s nullglob
for d in "$HOME/sync"/*/.claude/projects; do
    host="$(basename "$(dirname "$(dirname "$d")")")"
    "$APP_DIR/.venv/bin/tokmon" roots add "$d" --host "$host" >/dev/null 2>&1 || true
done
shopt -u nullglob
if [ -n "$CLIENT_HOSTS" ]; then
    IFS=',' read -ra HOSTS <<< "$CLIENT_HOSTS"
    for h in "${HOSTS[@]}"; do
        h="$(echo "$h" | xargs)"  # trim
        [ -z "$h" ] && continue
        mkdir -p "$HOME/sync/$h/.claude/projects"
        "$APP_DIR/.venv/bin/tokmon" roots add "$HOME/sync/$h/.claude/projects" \
            --host "$h" >/dev/null 2>&1 || true
    done
fi
c_blue "  roots configured:"
"$APP_DIR/.venv/bin/tokmon" roots list

# --- 6. Systemd units ---------------------------------------------------------
SYSTEMD_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_DIR"

# Substitute %h-style placeholders (systemd does that itself, but be explicit
# if APP_DIR is not $HOME/tokmon-app).
render_unit() {
    local src="$1" dst="$2"
    sed -e "s|%h/tokmon-app|$APP_DIR|g" \
        -e "s|--port 8765|--port $PORT|g" \
        "$src" > "$dst"
}
render_unit "$APP_DIR/deploy/tokmon-serve.service"   "$SYSTEMD_DIR/tokmon-serve.service"
render_unit "$APP_DIR/deploy/tokmon-ingest.service"  "$SYSTEMD_DIR/tokmon-ingest.service"
cp "$APP_DIR/deploy/tokmon-ingest.timer"             "$SYSTEMD_DIR/tokmon-ingest.timer"
c_green "  systemd units installed ✓"

# --- 7. Linger + enable -------------------------------------------------------
# Linger so user-level units run without an active login session.
if command -v loginctl >/dev/null 2>&1; then
    if ! loginctl show-user "$USER" --property=Linger | grep -q "Linger=yes"; then
        c_blue "  enabling user linger (may prompt for sudo)…"
        sudo loginctl enable-linger "$USER" || c_red "  (skip if no sudo — units only run when logged in)"
    fi
fi

systemctl --user daemon-reload
systemctl --user enable --now tokmon-serve.service
systemctl --user enable --now tokmon-ingest.timer

# Kick off one ingest right now so the dashboard isn't empty
systemctl --user start tokmon-ingest.service || true

# --- 7b. Install `tokmon-update` symlink to ~/.local/bin -----------------------
mkdir -p "$HOME/.local/bin"
ln -sf "$APP_DIR/deploy/tokmon-update.sh" "$HOME/.local/bin/tokmon-update"
chmod +x "$APP_DIR/deploy/tokmon-update.sh"

# --- 8. Echo URLs -------------------------------------------------------------
HOSTNAME_SHORT="$(hostname -s)"
TAILSCALE_NAME=""
if command -v tailscale >/dev/null 2>&1; then
    TAILSCALE_NAME="$(tailscale status --self --json 2>/dev/null | \
                      "$PY_BIN" -c 'import json,sys;d=json.load(sys.stdin)["Self"];print(d.get("DNSName","").rstrip("."))' 2>/dev/null || true)"
fi

echo
c_green "▶ tokmon is up"
echo "    systemctl --user status tokmon-serve   # check it's running"
echo "    journalctl --user -u tokmon-serve -f   # tail logs"
echo
echo "  Dashboard URLs:"
echo "    http://localhost:$PORT/                  (on the Pi)"
echo "    http://$HOSTNAME_SHORT:$PORT/             (on the LAN)"
if [ -n "$TAILSCALE_NAME" ]; then
    echo "    http://$TAILSCALE_NAME:$PORT/   (over Tailscale)"
fi
echo
echo "  Push from a laptop:"
echo "    tokmon sync set --pi-user $USER --pi-host ${TAILSCALE_NAME:-$HOSTNAME_SHORT} --pi-path $HOME"
echo "    tokmon push"
