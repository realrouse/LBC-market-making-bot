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
TEMPLATE="$SCRIPT_DIR/systemd/tradinebotte-account.service"

USER_NAME=$(id -un)
ACCOUNT_DIR="${TRADINEBOTTE_DIR:-}"
_UID=$(id -u)
_IPC_DIR="/run/user/${_UID}"; [ -d "${_IPC_DIR}" ] || { _IPC_DIR="/tmp/tradinebotte-${_UID}"; mkdir -p "${_IPC_DIR}"; chmod 700 "${_IPC_DIR}"; }
FEED_ADDR="${TRADINEBOTTE_FEED_ADDR:-ipc://${_IPC_DIR}/tradinebotte-feed.sock}"
INSTALL_DIR="${TRADINEBOTTE_INSTALL_DIR:-$HOME/tradinebotte}"
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"
# account_bot runs FLAT from the install dir (uniform with live_bot on every account —
# the old PROJECT_DIR/bot subdir layout is eliminated).
BOT_DIR="$INSTALL_DIR"

# ── Validations ───────────────────────────────────────────────────────────────
if [[ -z "$ACCOUNT_DIR" ]]; then
    echo "ERROR: TRADINEBOTTE_DIR must be set to the account directory." >&2
    echo "  Example: TRADINEBOTTE_DIR=~/account-a bash scripts/install_account_service.sh" >&2
    exit 1
fi

ACCOUNT_DIR="${ACCOUNT_DIR/#\~/$HOME}"

if [[ ! -f "$ACCOUNT_DIR/config.json" ]]; then
    echo "ERROR: config.json not found in $ACCOUNT_DIR" >&2
    echo "  Run: TRADINEBOTTE_DIR=\"$ACCOUNT_DIR\" python3 scripts/setup.py" >&2
    exit 1
fi

# When account_bot is managed by systemd, the feed is also managed externally.
# Verify feed_auto_start is set to false so account_bot does not try to fork feed.py.
_FA=$(python3 -c "
import json, sys
try:
    cfg = json.load(open(sys.argv[1]))
    print(str(cfg.get('feed_auto_start', True)).lower())
except Exception:
    print('unknown')
" "$ACCOUNT_DIR/config.json" 2>/dev/null)
if [[ "$_FA" != "false" ]]; then
    echo "WARNING: config.json does not have feed_auto_start=false." >&2
    echo "  When using systemd, the feed is managed externally; set:" >&2
    echo "    \"feed_auto_start\": false" >&2
    echo "  in $ACCOUNT_DIR/config.json to prevent account_bot from" >&2
    echo "  forking a second feed.py process on startup." >&2
    echo "" >&2
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

# Derive the unit name from the Linux username so each user gets a unique service.
ACCOUNT_NAME="$(basename "$ACCOUNT_DIR")"
SERVICE_NAME="tradinebotte-account-${USER_NAME}"
ENV_FILE="$ACCOUNT_DIR/credentials"
OUTPUT="${HOME}/tmp/${SERVICE_NAME}.service"
mkdir -p "${HOME}/tmp"

# Guard against '|' in any value used as a sed substitution — it is the delimiter.
for _v in "$USER_NAME" "$ACCOUNT_NAME" "$ACCOUNT_DIR" "$INSTALL_DIR" "$VENV" "$BOT_DIR" "$FEED_ADDR" "$ENV_FILE"; do
    if [[ "$_v" == *'|'* ]]; then
        echo "ERROR: a path or variable contains '|', which conflicts with the sed delimiter: $_v" >&2
        exit 1
    fi
done

# ── Generate unit file ────────────────────────────────────────────────────────
sed \
    -e "s|__USER__|$USER_NAME|g" \
    -e "s|__ACCOUNT_NAME__|$ACCOUNT_NAME|g" \
    -e "s|__TRADINEBOTTE_DIR__|$ACCOUNT_DIR|g" \
    -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
    -e "s|__VENV__|$VENV|g" \
    -e "s|__BOT_DIR__|$BOT_DIR|g" \
    -e "s|__FEED_ADDR__|$FEED_ADDR|g" \
    -e "s|__ENV_FILE__|$ENV_FILE|g" \
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
echo "  Credentials file (optional, chmod 600):"
echo "  If you use POLY_PRIVATE_KEY / MEXC_API_KEY / BINANCE_API_KEY via env vars,"
echo "  create $ENV_FILE with KEY=VALUE pairs (one per line)."
echo "  The service loads it automatically; missing file is silently ignored."
echo "  Example:"
echo "    POLY_PRIVATE_KEY=0x..."
echo "    MEXC_API_KEY=..."
echo "    MEXC_API_SECRET=..."
echo "  chmod 600 $ENV_FILE"
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
