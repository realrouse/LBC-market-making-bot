#!/usr/bin/env bash
# Generates a ready-to-install systemd unit file for the shared tradinebotte
# indicator service (indicators.py).
#
# The indicators service is a SHARED process: all account_bot instances on the
# machine connect to it.  Each bot registers its own indicator streams at
# startup via the REP socket (config.json key "indicators_streams").
#
# Typically installed as a SYSTEM service alongside tradinebotte-feed, owned
# by the same user as the feed service.
#
# Usage:
#   INDICATORS_CONFIG=~/tradinebotte/strategies/indicators_base.json \
#   bash scripts/install_indicators_service.sh
#
# Optional overrides:
#   INDICATORS_LABEL=btc               # suffix for multi-instance setups
#   TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5557
#   TRADINEBOTTE_INDICATORS_ADDR=tcp://127.0.0.1:5559    # PUB socket
#   TRADINEBOTTE_INDICATORS_REG_ADDR=tcp://127.0.0.1:5561  # REP registration
#   TRADINEBOTTE_INSTALL_DIR=~/tradinebotte               # where the venv lives
#
# Multiple instances (rare):
#   If two independent indicator services are needed (e.g. different markets),
#   set INDICATORS_LABEL and different port addresses for each.
#   Normal deployments need only one instance.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TEMPLATE="$SCRIPT_DIR/tradinebotte-indicators.service"

USER_NAME=$(id -un)
BOT_DIR="$PROJECT_DIR/bot"

# ── Config file ───────────────────────────────────────────────────────────────
CONFIG_FILE="${INDICATORS_CONFIG:-}"
if [[ -z "$CONFIG_FILE" ]]; then
    echo "ERROR: INDICATORS_CONFIG must be set to the strategy JSON config file." >&2
    echo "" >&2
    echo "  Example:" >&2
    echo "  INDICATORS_CONFIG=~/tradinebotte/strategies/indicators_base.json \\" >&2
    echo "  bash scripts/install_indicators_service.sh" >&2
    exit 1
fi
CONFIG_FILE="${CONFIG_FILE/#\~/$HOME}"
if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "ERROR: config file not found: $CONFIG_FILE" >&2
    exit 1
fi

# ── Service label ─────────────────────────────────────────────────────────────
# Single shared service → no label needed by default.
# Set INDICATORS_LABEL only when running two independent instances.
if [[ -n "${INDICATORS_LABEL:-}" ]]; then
    LABEL="$INDICATORS_LABEL"
    SERVICE_NAME="tradinebotte-indicators-${LABEL}"
else
    LABEL="shared"
    SERVICE_NAME="tradinebotte-indicators"
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

# ── ZMQ addresses ─────────────────────────────────────────────────────────────
_PORT_BASE="${TRADINEBOTTE_PORT_BASE:-5557}"
FEED_ADDR="${TRADINEBOTTE_FEED_ADDR:-tcp://127.0.0.1:${_PORT_BASE}}"
IND_ADDR="${TRADINEBOTTE_INDICATORS_ADDR:-tcp://127.0.0.1:$(( _PORT_BASE + 2 ))}"
REG_ADDR="${TRADINEBOTTE_INDICATORS_REG_ADDR:-tcp://127.0.0.1:$(( _PORT_BASE + 4 ))}"

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
    -e "s|__FEED_ADDR__|$FEED_ADDR|g" \
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
echo "  Feed    : $FEED_ADDR"
echo "  PUB out : $IND_ADDR    (TRADINEBOTTE_INDICATORS_ADDR)"
echo "  REP reg : $REG_ADDR    (TRADINEBOTTE_INDICATORS_REG_ADDR)"
echo "  venv    : $VENV"
echo ""
echo "  Each account_bot registers its streams at startup via config.json:"
echo "  \"indicators_reg_addr\": \"$REG_ADDR\","
echo "  \"indicators_streams\": ["
echo "    {\"source\": \"binance_ws\", \"asset\": \"BTCUSDT\", \"timeframe\": \"4h\","
echo "     \"indicators\": [{\"type\": \"rsi\", \"period\": 14}]}"
echo "  ]"
echo ""
echo "  To install as a SYSTEM service (start before account_bot services):"
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
