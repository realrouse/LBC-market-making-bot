#!/usr/bin/env bash
# Generates a ready-to-install systemd unit file for one tradinebotte indicator
# service instance (indicators.py).
#
# Multiple instances can coexist on the same machine — each needs its own
# INDICATORS_CONFIG file and distinct ZMQ output ports
# (TRADINEBOTTE_INDICATORS_ADDR / TRADINEBOTTE_INDICATORS_REG_ADDR).
#
# Usage:
#   INDICATORS_CONFIG=~/tradinebotte/strategies/indicators_4h_bitcoin.json \
#   bash scripts/install_indicators_service.sh
#
# Optional overrides:
#   INDICATORS_LABEL=btc-4h               # service name suffix (default: derived from config)
#   TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5557
#   TRADINEBOTTE_INDICATORS_ADDR=tcp://127.0.0.1:5559    # PUB socket
#   TRADINEBOTTE_INDICATORS_REG_ADDR=tcp://127.0.0.1:5561  # REP registration socket
#   TRADINEBOTTE_INSTALL_DIR=~/tradinebotte               # where the venv lives
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
    echo "  INDICATORS_CONFIG=~/tradinebotte/strategies/indicators_4h_bitcoin.json \\" >&2
    echo "  bash scripts/install_indicators_service.sh" >&2
    exit 1
fi
CONFIG_FILE="${CONFIG_FILE/#\~/$HOME}"
if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "ERROR: config file not found: $CONFIG_FILE" >&2
    exit 1
fi

# ── Service label (used in Description and service name) ──────────────────────
# Derive from config filename by default: indicators_4h_bitcoin.json → 4h-bitcoin
# User can override with INDICATORS_LABEL.
if [[ -n "${INDICATORS_LABEL:-}" ]]; then
    LABEL="$INDICATORS_LABEL"
else
    _base="$(basename "$CONFIG_FILE" .json)"
    # Strip leading "indicators_" or "indicators-" prefix if present
    _base="${_base#indicators_}"
    _base="${_base#indicators-}"
    # Replace underscores with dashes, lowercase
    LABEL="${_base//_/-}"
    LABEL="${LABEL,,}"
fi

SERVICE_NAME="tradinebotte-indicators-${USER_NAME}-${LABEL}"

# ── Install directory and venv ────────────────────────────────────────────────
INSTALL_DIR="${TRADINEBOTTE_INSTALL_DIR:-}"
if [[ -z "$INSTALL_DIR" ]]; then
    # Auto-detect: prefer project .venv (dev), fall back to ~/tradinebotte (prod)
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
    echo "  Run from the project root or adjust TRADINEBOTTE_INSTALL_DIR." >&2
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
    echo "  Replacing the unit file without stopping it first may cause issues." >&2
    echo "  Run: sudo systemctl stop $SERVICE_NAME" >&2
    echo ""
elif systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    echo "WARNING: $SERVICE_NAME is installed as a system service (enabled, not running)." >&2
    echo "  The generated file will overwrite the existing unit if copied." >&2
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
echo "  Label   : $LABEL"
echo "  Config  : $CONFIG_FILE"
echo "  Service : $SERVICE_NAME"
echo "  User    : $USER_NAME"
echo "  Feed    : $FEED_ADDR"
echo "  PUB out : $IND_ADDR"
echo "  REP reg : $REG_ADDR"
echo "  venv    : $VENV"
echo ""
echo "  Note: if multiple indicator instances run on the same machine,"
echo "  each must use distinct PUB and REP ports:"
echo "    Instance 1:  TRADINEBOTTE_INDICATORS_ADDR=tcp://127.0.0.1:5559"
echo "                 TRADINEBOTTE_INDICATORS_REG_ADDR=tcp://127.0.0.1:5561"
echo "    Instance 2:  TRADINEBOTTE_INDICATORS_ADDR=tcp://127.0.0.1:5563"
echo "                 TRADINEBOTTE_INDICATORS_REG_ADDR=tcp://127.0.0.1:5565"
echo "  Or use TRADINEBOTTE_PORT_BASE=<base> to shift all ports uniformly."
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
