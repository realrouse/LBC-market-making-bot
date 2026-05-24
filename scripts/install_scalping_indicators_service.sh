#!/usr/bin/env bash
# install_scalping_indicators_service.sh — Generate a systemd unit file for the
# tradinebotte scalping microstructure indicator service (indicators.py).
#
# This service connects DIRECTLY to Binance WebSocket (depth20 + aggTrade).
# It does NOT depend on tradinebotte-feed.service.
#
# It publishes on separate ZMQ ports from the standard indicator service so
# both can run simultaneously on the same machine without port conflicts:
#   Default PUB  : tcp://127.0.0.1:5565   (TRADINEBOTTE_SCALPING_IND_ADDR)
#   Default REP  : tcp://127.0.0.1:5567   (TRADINEBOTTE_SCALPING_REG_ADDR)
#
# Usage:
#   bash scripts/install_scalping_indicators_service.sh
#
# Optional overrides:
#   SCALPING_CONFIG=~/tradinebotte/strategies/indicators/indicators_scalping_btc.json
#   SCALPING_LABEL=btc               # suffix for multi-instance setups
#   TRADINEBOTTE_SCALPING_IND_ADDR=tcp://127.0.0.1:5565
#   TRADINEBOTTE_SCALPING_REG_ADDR=tcp://127.0.0.1:5567
#   TRADINEBOTTE_INSTALL_DIR=~/tradinebotte
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TEMPLATE="$SCRIPT_DIR/systemd/tradinebotte-scalping-indicators.service"

USER_NAME=$(id -un)
BOT_DIR="$PROJECT_DIR/bot"

# ── Config file ───────────────────────────────────────────────────────────────
CONFIG_FILE="${SCALPING_CONFIG:-$PROJECT_DIR/strategies/indicators/indicators_scalping_btc.json}"
CONFIG_FILE="${CONFIG_FILE/#\~/$HOME}"
if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "ERROR: config file not found: $CONFIG_FILE" >&2
    echo "  Set SCALPING_CONFIG to override." >&2
    exit 1
fi

# ── Service label ─────────────────────────────────────────────────────────────
if [[ -n "${SCALPING_LABEL:-}" ]]; then
    LABEL="$SCALPING_LABEL"
    SERVICE_NAME="tradinebotte-scalping-indicators-${LABEL}"
else
    LABEL="btc"
    SERVICE_NAME="tradinebotte-scalping-indicators"
fi

# ── Install directory and venv ────────────────────────────────────────────────
INSTALL_DIR="${TRADINEBOTTE_INSTALL_DIR:-}"
if [[ -z "$INSTALL_DIR" ]]; then
    if [[ -f "$PROJECT_DIR/.venv/bin/python3" ]]; then
        INSTALL_DIR="$PROJECT_DIR"
    elif [[ -f "${HOME}/tradinebotte/venv/bin/python3" ]]; then
        INSTALL_DIR="${HOME}/tradinebotte"
    else
        echo "ERROR: no virtualenv found." >&2
        echo "  Expected: $PROJECT_DIR/.venv  or  ~/tradinebotte/venv" >&2
        echo "  Set TRADINEBOTTE_INSTALL_DIR to override." >&2
        exit 1
    fi
fi
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"

VENV="$INSTALL_DIR/venv"
[[ -d "$INSTALL_DIR/.venv" ]] && VENV="$INSTALL_DIR/.venv"

# ── ZMQ addresses (separate ports from the standard indicators service) ───────
# Standard indicators: PUB=5559, REP=5561
# Scalping indicators: PUB=5565, REP=5567
IND_ADDR="${TRADINEBOTTE_SCALPING_IND_ADDR:-tcp://127.0.0.1:5565}"
REG_ADDR="${TRADINEBOTTE_SCALPING_REG_ADDR:-tcp://127.0.0.1:5567}"

ENV_FILE="$INSTALL_DIR/credentials"
OUTPUT="${HOME}/tmp/${SERVICE_NAME}.service"
mkdir -p "${HOME}/tmp"

# ── Validations ───────────────────────────────────────────────────────────────
if [[ ! -f "$TEMPLATE" ]]; then
    echo "ERROR: template not found: $TEMPLATE" >&2
    exit 1
fi

if [[ ! -f "$BOT_DIR/indicators.py" ]]; then
    echo "ERROR: indicators.py not found at $BOT_DIR" >&2
    exit 1
fi

if ! "$VENV/bin/python3" -c "import zmq" 2>/dev/null; then
    echo "ERROR: pyzmq not installed in $VENV" >&2
    echo "  Run: $VENV/bin/pip install pyzmq" >&2
    exit 1
fi

if ! "$VENV/bin/python3" -c "import websockets" 2>/dev/null; then
    echo "ERROR: websockets not installed in $VENV" >&2
    echo "  Run: $VENV/bin/pip install websockets" >&2
    exit 1
fi

# ── Check for already-running instance ────────────────────────────────────────
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    echo "WARNING: $SERVICE_NAME is currently ACTIVE as a system service." >&2
    echo "  Run: sudo systemctl stop $SERVICE_NAME" >&2
    echo ""
elif systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    echo "WARNING: $SERVICE_NAME is installed (enabled, not running)." >&2
    echo ""
fi

# ── Generate unit file ────────────────────────────────────────────────────────
sed \
    -e "s|__USER__|$USER_NAME|g" \
    -e "s|__LABEL__|$LABEL|g" \
    -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
    -e "s|__VENV__|$VENV|g" \
    -e "s|__BOT_DIR__|$BOT_DIR|g" \
    -e "s|__CONFIG_FILE__|$CONFIG_FILE|g" \
    -e "s|__IND_ADDR__|$IND_ADDR|g" \
    -e "s|__REG_ADDR__|$REG_ADDR|g" \
    -e "s|__ENV_FILE__|$ENV_FILE|g" \
    "$TEMPLATE" > "$OUTPUT"

echo "Generated: $OUTPUT"
echo ""
cat "$OUTPUT"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Service : $SERVICE_NAME"
echo "  Config  : $CONFIG_FILE"
echo "  User    : $USER_NAME"
echo "  PUB out : $IND_ADDR    (TRADINEBOTTE_SCALPING_IND_ADDR)"
echo "  REP reg : $REG_ADDR    (TRADINEBOTTE_SCALPING_REG_ADDR)"
echo "  venv    : $VENV"
echo ""
echo "  NOTE: This service connects directly to Binance WebSocket."
echo "  It does NOT require tradinebotte-feed.service."
echo ""
echo "  Bots subscribe to scalping indicators via config.json:"
echo "  \"indicators_addr\": \"$IND_ADDR\","
echo "  \"indicators_reg_addr\": \"$REG_ADDR\","
echo "  \"indicators_streams\": ["
echo "    {\"source\": \"binance_scalping\", \"asset\": \"BTCUSDT\","
echo "     \"stream_id\": \"btc_scalping_spot\","
echo "     \"params\": {\"market\": \"spot\", \"obi_levels\": 10}}"
echo "  ]"
echo ""
echo "  To install as a SYSTEM service:"
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
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
