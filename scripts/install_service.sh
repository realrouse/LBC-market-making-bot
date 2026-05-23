#!/usr/bin/env bash
# install_service.sh — Generate a systemd unit file for the standalone bot (Option A).
#
# Fills __USER__ and __TRADINEBOTTE_DIR__ placeholders in the tradinebotte.service
# template and writes the result to ~/tmp/tradinebotte.service, then prints the
# exact sudo commands to install and enable it.
#
# Requires sudo only for the copy step (sudo cp … /etc/systemd/system/).
#
# Usage:
#   bash scripts/install_service.sh
#   TRADINEBOTTE_DIR=~/tradinebotte bash scripts/install_service.sh
set -euo pipefail

USER_NAME=$(id -un)
TDIR="${TRADINEBOTTE_DIR:-$HOME/tradinebotte}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/tradinebotte.service"
OUTPUT="${HOME}/tmp/tradinebotte.service"
mkdir -p "${HOME}/tmp"

if [[ ! -f "$TEMPLATE" ]]; then
    echo "ERROR: template not found: $TEMPLATE" >&2
    exit 1
fi

if [[ ! -f "$TDIR/live_bot.py" ]]; then
    echo "ERROR: bot not installed at $TDIR — run scripts/install.sh first" >&2
    exit 1
fi

if [[ ! -f "$TDIR/venv/bin/python3" ]]; then
    echo "ERROR: virtualenv not found at $TDIR/venv — run scripts/install.sh first" >&2
    exit 1
fi

sed \
    -e "s|__USER__|$USER_NAME|g" \
    -e "s|__TRADINEBOTTE_DIR__|$TDIR|g" \
    "$TEMPLATE" > "$OUTPUT"

echo "Generated: $OUTPUT"
echo ""
cat "$OUTPUT"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  To install (requires sudo):"
echo ""
echo "  sudo cp $OUTPUT /etc/systemd/system/tradinebotte.service"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable tradinebotte   # start on boot"
echo "  sudo systemctl start tradinebotte    # start now"
echo ""
echo "  Useful commands:"
echo "  sudo systemctl status tradinebotte"
echo "  sudo systemctl stop tradinebotte"
echo "  journalctl -u tradinebotte -f        # live systemd logs"
echo "  tail -f $TDIR/live.log              # bot application logs"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
