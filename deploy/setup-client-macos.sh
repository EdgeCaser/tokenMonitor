#!/usr/bin/env bash
# Installs tokmon as a sync client on macOS:
#   - writes ~/.tokmon/sync.toml
#   - installs launchd plist that runs `tokmon push` every 10 min
#   - kicks off one push right now
#
# Requires tokmon to be importable from a location launchd can read. macOS TCC
# blocks launchd from ~/Documents, ~/Desktop and ~/Downloads, so if this repo
# lives in one of those, do NOT use a repo-local venv or an editable install.
# Both leave the scheduled push failing on every run. Instead:
#   python3 -m venv ~/.local/venvs/tokmon
#   ~/.local/venvs/tokmon/bin/pip install /path/to/tokenMonitor   # not -e
# The check in step 1 enforces this rather than letting it fail silently.
#
# Env vars (required if no flags):
#   TOKMON_PI_USER
#   TOKMON_PI_HOST
#   TOKMON_PI_PATH

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

c_blue() { printf "\033[1;36m%s\033[0m\n" "$*"; }
c_green() { printf "\033[1;32m%s\033[0m\n" "$*"; }
c_red() { printf "\033[1;31m%s\033[0m\n" "$*" >&2; }

c_blue "▶ tokmon macOS client setup"

# --- 1. tokmon binary check ---------------------------------------------------
# Prefer the venv next to this repo; fall back to PATH (activated venv / global install).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TOKMON_BIN="$REPO_ROOT/.venv/bin/tokmon"
if [ ! -x "$TOKMON_BIN" ]; then
    TOKMON_BIN="$(command -v tokmon || true)"
fi
if [ -z "$TOKMON_BIN" ] || [ ! -x "$TOKMON_BIN" ]; then
    c_red "tokmon CLI not found."
    c_red "  Expected at: $REPO_ROOT/.venv/bin/tokmon"
    c_red "  Or on PATH (activate venv: source .venv/bin/activate)."
    c_red "  To create the venv:"
    c_red "    python3 -m venv ~/.local/venvs/tokmon"
    c_red "    ~/.local/venvs/tokmon/bin/pip install $REPO_ROOT"
    exit 1
fi
c_blue "  tokmon: $TOKMON_BIN ✓"

# launchd cannot read ~/Documents, ~/Desktop or ~/Downloads (macOS TCC). A plist
# pointing into one of them bootstraps fine and then fails every single run with
# PermissionError on pyvenv.cfg, logging nothing useful, so refuse up front.
# Check the package source as well as the launcher: an editable install puts the
# binary outside Documents while still importing from the repo inside it.
resolve_path() {
    python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1" 2>/dev/null \
        || printf '%s\n' "$1"
}

tcc_blocked() {
    case "$1" in
        "$HOME"/Documents/*|"$HOME"/Desktop/*|"$HOME"/Downloads/*) return 0 ;;
        *) return 1 ;;
    esac
}

TOKMON_SRC=""
if [ -x "$(dirname "$TOKMON_BIN")/python" ]; then
    TOKMON_SRC="$("$(dirname "$TOKMON_BIN")/python" \
        -c 'import os,tokmon; print(os.path.dirname(tokmon.__file__))' 2>/dev/null || true)"
fi

for candidate in "$TOKMON_BIN" "$TOKMON_SRC"; do
    [ -n "$candidate" ] || continue
    if tcc_blocked "$(resolve_path "$candidate")"; then
        c_red "Refusing to install: $candidate is under a TCC-protected folder."
        c_red "  launchd cannot read ~/Documents, ~/Desktop or ~/Downloads, so the"
        c_red "  scheduled 'tokmon push' would fail on every run."
        c_red "  Install somewhere launchd can read, using a regular (non-editable)"
        c_red "  install so no source is loaded from the repo at runtime:"
        c_red "    python3 -m venv ~/.local/venvs/tokmon"
        c_red "    ~/.local/venvs/tokmon/bin/pip install $REPO_ROOT"
        c_red "  Then re-run this script with that venv's tokmon first on PATH."
        exit 1
    fi
done

# --- 2. Tailscale + SSH reachability -----------------------------------------
c_blue "  testing SSH to $PI_USER@$PI_HOST…"
if ssh -o BatchMode=yes -o ConnectTimeout=5 "$PI_USER@$PI_HOST" 'echo ok' >/dev/null 2>&1; then
    c_blue "  SSH ✓"
else
    c_red "  SSH failed. Ensure Tailscale is up and your key is authorized."
    c_red "  Try: ssh-copy-id $PI_USER@$PI_HOST"
    exit 1
fi

# --- 3. Write sync.toml -------------------------------------------------------
"$TOKMON_BIN" sync set --pi-user "$PI_USER" --pi-host "$PI_HOST" --pi-path "$PI_PATH"

# --- 4. Render launchd plist --------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/com.tokmon.sync.plist.template"
PLIST_DEST="$HOME/Library/LaunchAgents/com.tokmon.sync.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

sed -e "s|@TOKMON_BIN@|$TOKMON_BIN|g" \
    -e "s|@HOME@|$HOME|g" \
    "$TEMPLATE" > "$PLIST_DEST"
c_blue "  plist: $PLIST_DEST"

# --- 5. Load into launchd -----------------------------------------------------
# bootstrap fails if it's already loaded; bootout first to be idempotent.
launchctl bootout "gui/$UID/com.tokmon.sync" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST_DEST"
launchctl enable "gui/$UID/com.tokmon.sync"
c_green "  launchd ✓"

# --- 6. First push ------------------------------------------------------------
c_blue "  running first push…"
"$TOKMON_BIN" push
c_green "  initial sync ✓"

echo
c_green "▶ done"
echo "    Pi dashboard:  http://$PI_HOST:8765/"
echo "    Tail log:      tail -f ~/Library/Logs/tokmon-sync.log"
echo "    Force a push:  tokmon push"
echo "    Unload:        launchctl bootout gui/\$UID/com.tokmon.sync"
echo
echo "    Your local ~/.tokmon/tokmon.duckdb is no longer canonical."
echo "    Verify the Pi totals match, then optionally:  rm ~/.tokmon/tokmon.duckdb"
