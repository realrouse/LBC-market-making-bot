#!/usr/bin/env bash
# Generates a ready-to-install systemd unit file for one tradinebotte account bot
# (account_bot.py, Option B multi-bot architecture).
#
# The account service declares Requires=tradinebotte-feed.service so systemd
# starts (and restarts) it only when the feed is running.
#
# Usage (run as the account owner):
#   TRADINEBOTTE_DIR=~/account-a bash scripts/install_account_service.sh
#   TRADINEBOTTE_DIR=~/account-b bash scripts/install_account_service.sh
#
# Optional overrides:
#   TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 \
#   TRADINEBOTTE_DIR=~/account-a \
#   bash scripts/install_account_service.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TEMPLATE="$SCRIPT_DIR/tradinebotte-account.service"

USER_NAME=$(id -un)
ACCOUNT_DIR="${TRADINEBOTTE_DIR:-}"
FEED_ADDR="${TRADINEBOTTE_FEED_ADDR:-tcp://127.0.0.1:5557}"
INSTALL_DIR="${TRADINEBOTTE_INSTALL_DIR:-$HOME/tradinebotte}"
INSTALL_DIR="$(eval echo "$INSTALL_DIR")"
BOT_DIR="$PROJECT_DIR/bot"

# ── Validations ───────────────────────────────────────────────────────────────
if [[ -z "$ACCOUNT_DIR" ]]; then
    echo "ERROR: TRADINEBOTTE_DIR must be set to the account directory." >&2
    echo "  Example: TRADINEBOTTE_DIR=~/account-a bash scripts/install_account_service.sh" >&2
    exit 1
fi

ACCOUNT_DIR="$(eval echo "$ACCOUNT_DIR")"

if [[ ! -f "$ACCOUNT_DIR/config.json" ]]; then
    echo "ERROR: config.json not found in $ACCOUNT_DIR" >&2
    echo "  Run: TRADINEBOTTE_DIR=\"$ACCOUNT_DIR\" python3 scripts/setup.py" >&2
    exit 1
fi

if [[ ! -f "$TEMPLATE" ]]; then
    echo "ERROR: template not found: $TEMPLATE" >&2
    exit 1
fi

if [[ ! -f "$BOT_DIR/account_bot.py" ]]; then
    echo "ERROR: account_bot.py not found at $BOT_DIR" >&2
    exit 1
fi

# Auto-detect venv: prefer project .venv (dev), fall back to $INSTALL_DIR/venv (prod)
if [[ -f "$INSTALL_DIR/.venv/bin/python3" ]]; then
    VENV="$INSTALL_DIR/.venv"
elif [[ -f "$INSTALL_DIR/venv/bin/python3" ]]; then
    VENV="$INSTALL_DIR/venv"
else
    echo "ERROR: virtualenv not found at $INSTALL_DIR/.venv or $INSTALL_DIR/venv" >&2
    echo "  Set TRADINEBOTTE_INSTALL_DIR if your venv is elsewhere." >&2
    exit 1
fi

# Derive a short name for the unit from the account directory basename.
ACCOUNT_NAME="$(basename "$ACCOUNT_DIR")"
SERVICE_NAME="tradinebotte-account-${ACCOUNT_NAME}"
OUTPUT="${HOME}/tmp/${SERVICE_NAME}.service"
mkdir -p "${HOME}/tmp"

# ── Generate unit file ────────────────────────────────────────────────────────
sed \
    -e "s|__USER__|$USER_NAME|g" \
    -e "s|__ACCOUNT_NAME__|$ACCOUNT_NAME|g" \
    -e "s|__TRADINEBOTTE_DIR__|$ACCOUNT_DIR|g" \
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
echo "  Account : $ACCOUNT_DIR"
echo "  Feed    : $FEED_ADDR"
echo "  User    : $USER_NAME"
echo "  Service : $SERVICE_NAME"
echo ""
echo "  Prerequisite: tradinebotte-feed.service must be installed first."
echo "  See: bash scripts/install_feed_service.sh"
echo ""
echo "  To install:"
echo ""
echo "  sudo cp $OUTPUT /etc/systemd/system/${SERVICE_NAME}.service"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable $SERVICE_NAME   # start on boot"
echo "  sudo systemctl start $SERVICE_NAME    # start now"
echo ""
echo "  Useful commands:"
echo "  sudo systemctl status $SERVICE_NAME"
echo "  sudo systemctl stop $SERVICE_NAME"
echo "  journalctl -u $SERVICE_NAME -f"
echo "  tail -f $ACCOUNT_DIR/account.log"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
