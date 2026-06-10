#!/usr/bin/env bash
# install_status_service.sh — Install tradinebotte-status as a systemd --user service.
#
# The status collector binds tcp://127.0.0.1:5562 and receives heartbeats from
# all bot accounts over TCP loopback.  It MUST run as the collector account
# (same machine, user service).
#
# Usage:
#   bash scripts/install_status_service.sh
#
# Optional overrides (env vars):
#   TRADINEBOTTE_STATUS_DIR    Install directory (default ~/tradinebotte)
#   TRADINEBOTTE_STATUS_ADDR   ZMQ bind address (default tcp://127.0.0.1:5562)
#
# After running, the service is enabled but NOT started automatically.
# Start it with:
#   systemctl --user start tradinebotte-status.service

set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "${GREEN}  ✓ $*${NC}"; }
info() { echo -e "${BLUE}  → $*${NC}"; }
err()  { echo -e "${RED}  ✗ $*${NC}"; }
warn() { echo -e "${YELLOW}  ! $*${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"

INSTALL_DIR="${TRADINEBOTTE_STATUS_DIR:-$HOME/tradinebotte}"
STATUS_ADDR="${TRADINEBOTTE_STATUS_ADDR:-tcp://127.0.0.1:5562}"

UNIT_NAME="tradinebotte-status.service"
UNIT_TEMPLATE="$SCRIPT_DIR/systemd/$UNIT_NAME"
UNIT_DEST="$HOME/.config/systemd/user/$UNIT_NAME"

echo -e "\n${BOLD}${YELLOW}═══ tradinebotte-status install ═══${NC}"
info "Install dir  : $INSTALL_DIR"
info "Status addr  : $STATUS_ADDR"
info "Unit dest    : $UNIT_DEST"

# ─── Validate prerequisites ────────────────────────────────────────────────────

if [[ ! -f "$INSTALL_DIR/status_collector.py" ]]; then
    err "status_collector.py not found in $INSTALL_DIR"
    echo "  Run the rsync deploy first:  rsync $LOCAL_REPO/tradinebotte-status/ $INSTALL_DIR/"
    exit 1
fi
ok "status_collector.py present"

VENV=""
if [[ -x "$INSTALL_DIR/.venv/bin/python3" ]]; then
    VENV="$INSTALL_DIR/.venv"
elif [[ -x "$INSTALL_DIR/venv/bin/python3" ]]; then
    VENV="$INSTALL_DIR/venv"
fi
if [[ -z "$VENV" ]]; then
    err "No virtualenv found at $INSTALL_DIR/.venv or $INSTALL_DIR/venv"
    exit 1
fi
ok "venv: $VENV"

if ! "$VENV/bin/python3" -c "import zmq" 2>/dev/null; then
    err "zmq not importable — run: $VENV/bin/pip install pyzmq"
    exit 1
fi
ok "zmq importable"

if ! "$VENV/bin/python3" -c "from tradinetools.zmq import default_status_addr" \
       2>/dev/null; then
    err "tradinetools not importable — run: $VENV/bin/pip install -e $INSTALL_DIR/tradinetools"
    exit 1
fi
ok "tradinetools importable"

# ─── Generate unit file ────────────────────────────────────────────────────────

mkdir -p "$HOME/.config/systemd/user"

# Substitute the venv path and status addr into the template.
# The template uses %h for HOME (systemd expands it at runtime), so we only need
# to override the address when the caller set a non-default TRADINEBOTTE_STATUS_ADDR.
if [[ "$STATUS_ADDR" != "tcp://127.0.0.1:5562" ]]; then
    sed "s|tcp://127.0.0.1:5562|${STATUS_ADDR}|g" "$UNIT_TEMPLATE" > "$UNIT_DEST"
    info "Custom STATUS_ADDR substituted in unit file"
else
    cp "$UNIT_TEMPLATE" "$UNIT_DEST"
fi
ok "Unit installed to $UNIT_DEST"

# ─── Reload + enable ───────────────────────────────────────────────────────────

export XDG_RUNTIME_DIR="/run/user/$(id -u)"

systemctl --user daemon-reload
ok "systemd user daemon reloaded"

systemctl --user enable "$UNIT_NAME"
ok "$UNIT_NAME enabled (start on login)"

# ─── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}${GREEN}  Install complete.${NC}"
echo ""
echo "  Start now   :  systemctl --user start $UNIT_NAME"
echo "  Stop        :  systemctl --user stop  $UNIT_NAME"
echo "  Logs        :  journalctl --user -u $UNIT_NAME -f"
echo "  Status      :  systemctl --user status $UNIT_NAME"
echo ""
echo "  Heartbeat DB:  $INSTALL_DIR/heartbeat.db"
echo "  ZMQ address :  $STATUS_ADDR"
echo ""
warn "Verify that all bots can reach $STATUS_ADDR before starting (cross-account TCP)."
