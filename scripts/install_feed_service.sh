#!/usr/bin/env bash
# Generates a ready-to-install systemd unit file for tradinebotte-feed
# (the shared WebSocket broadcaster, Option B multi-bot architecture).
#
# The feed service is typically installed as a SYSTEM service so it:
#   - starts before any user logs in
#   - can be referenced by account_bot services running as different users
#
# Usage:
#   bash scripts/install_feed_service.sh
#   TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 bash scripts/install_feed_service.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TEMPLATE="$SCRIPT_DIR/tradinebotte-feed.service"
OUTPUT="${HOME}/tmp/tradinebotte-feed.service"
mkdir -p "${HOME}/tmp"

USER_NAME=$(id -un)
BOT_DIR="$PROJECT_DIR/bot"
FEED_ADDR="${TRADINEBOTTE_FEED_ADDR:-tcp://127.0.0.1:5557}"

# Auto-detect venv: prefer project .venv (dev), fall back to ~/tradinebotte/venv (prod)
if [[ -f "$PROJECT_DIR/.venv/bin/python3" ]]; then
    INSTALL_DIR="$PROJECT_DIR"
elif [[ -f "${TRADINEBOTTE_DIR:-$HOME/tradinebotte}/venv/bin/python3" ]]; then
    INSTALL_DIR="${TRADINEBOTTE_DIR:-$HOME/tradinebotte}"
    INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"
else
    echo "ERROR: no virtualenv found." >&2
    echo "  Expected: $PROJECT_DIR/.venv  or  ~/tradinebotte/venv" >&2
    echo "  Run scripts/install.sh first." >&2
    exit 1
fi

VENV="$INSTALL_DIR/venv"
[[ -d "$INSTALL_DIR/.venv" ]] && VENV="$INSTALL_DIR/.venv"

# ── Validations ───────────────────────────────────────────────────────────────
if [[ ! -f "$TEMPLATE" ]]; then
    echo "ERROR: template not found: $TEMPLATE" >&2
    exit 1
fi

if [[ ! -f "$BOT_DIR/feed.py" ]]; then
    echo "ERROR: feed.py not found at $BOT_DIR" >&2
    echo "  Run from the project root or set PROJECT_DIR." >&2
    exit 1
fi

# Verify pyzmq is available in the venv
if ! "$VENV/bin/python3" -c "import zmq" 2>/dev/null; then
    echo "ERROR: pyzmq not installed in $VENV" >&2
    echo "  Run: $VENV/bin/pip install pyzmq" >&2
    exit 1
fi

# ── Generate unit file ────────────────────────────────────────────────────────
sed \
    -e "s|__USER__|$USER_NAME|g" \
    -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
    -e "s|__VENV__|$VENV|g" \
    -e "s|__BOT_DIR__|$BOT_DIR|g" \
    -e "s|__FEED_ADDR__|$FEED_ADDR|g" \
    "$TEMPLATE" > "$OUTPUT"

echo "Generated: $OUTPUT"
echo ""
cat "$OUTPUT"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Feed : $FEED_ADDR"
echo "  User : $USER_NAME"
echo "  venv : $VENV"
echo ""
echo "  To install as a SYSTEM service (visible to all users):"
echo ""
echo "  sudo cp $OUTPUT /etc/systemd/system/tradinebotte-feed.service"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable tradinebotte-feed   # start on boot"
echo "  sudo systemctl start tradinebotte-feed    # start now"
echo ""
echo "  Useful commands:"
echo "  sudo systemctl status tradinebotte-feed"
echo "  sudo systemctl stop tradinebotte-feed"
echo "  journalctl -u tradinebotte-feed -f"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
