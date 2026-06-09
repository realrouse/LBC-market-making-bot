#!/usr/bin/env bash
# install_indicators_service.sh — Generate a systemd unit file for the shared
# tradinebotte indicator service (indicators.py) running the unified config.
#
# The unified config (indicators_all.json) runs ALL streams in one process:
#   binance_ws           — 4h and 1d BTC/USDT candles (RSI, EMA, ATR, volatility)
#   binance_scalping     — L2 orderbook + aggTrade for OBI/TFI/spread/realized-vol
#   binance_full_depth   — full spot OB 5000 levels, OBI 10/100/500, walls, cum vol
#   binance_funding      — perpetual funding rate (REST, every 15 min)
#   binance_oi           — open interest (REST, every 5 min)
#   binance_ls_ratio     — long/short ratio (REST, every 5 min)
#   binance_liquidations — forced liquidations (REST, every 5 min)
#   binance_vwap_context — 4h VWAP vs price, dip_score (REST, every 1 h)
#   binance_volume_profile — 24h taker buy/sell by price bucket, HVN zones (REST, every 1 h)
#   binance_macro_obi    — 1h of 1m candles macro OBI, trend direction (REST, every 1 min)
#   deribit_iv           — DVOL implied volatility (REST, every 5 min)
#   fear_greed           — Fear & Greed Index (REST, every 1 h)
#
# All streams connect directly to external APIs — tradinebotte-feed.service
# is NOT required.
#
# Usage:
#   bash scripts/install_indicators_service.sh
#
# Optional overrides:
#   INDICATORS_CONFIG=~/tradinebotte/strategies/indicators/indicators_all.json
#   INDICATORS_LABEL=btc               # suffix for multi-instance setups
#   TRADINEBOTTE_INDICATORS_ADDR=tcp://127.0.0.1:5559
#   TRADINEBOTTE_INDICATORS_REG_ADDR=tcp://127.0.0.1:5561
#   TRADINEBOTTE_INSTALL_DIR=~/tradinebotte
#
# Multiple instances (rare):
#   Set INDICATORS_LABEL and different port addresses for each instance.
#   Normal deployments need only one instance.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TEMPLATE="$SCRIPT_DIR/systemd/tradinebotte-indicators.service"

USER_NAME=$(id -un)
BOT_DIR="$PROJECT_DIR"

# ── Config file ───────────────────────────────────────────────────────────────
CONFIG_FILE="${INDICATORS_CONFIG:-$PROJECT_DIR/strategies/indicators_all.json}"
CONFIG_FILE="${CONFIG_FILE/#\~/$HOME}"
if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "ERROR: config file not found: $CONFIG_FILE" >&2
    echo "  Set INDICATORS_CONFIG to override." >&2
    exit 1
fi

# ── Service label ─────────────────────────────────────────────────────────────
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
_UID=$(id -u)
_IPC_DIR="/run/user/${_UID}"; [ -d "${_IPC_DIR}" ] || { _IPC_DIR="/tmp/tradinebotte-${_UID}"; mkdir -p -m 700 "${_IPC_DIR}"; }
IND_ADDR="${TRADINEBOTTE_INDICATORS_ADDR:-ipc://${_IPC_DIR}/tradinebotte-indicators.sock}"
REG_ADDR="${TRADINEBOTTE_INDICATORS_REG_ADDR:-ipc://${_IPC_DIR}/tradinebotte-ind-reg.sock}"

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
echo "  PUB out : $IND_ADDR    (TRADINEBOTTE_INDICATORS_ADDR)"
echo "  REP reg : $REG_ADDR    (TRADINEBOTTE_INDICATORS_REG_ADDR)"
echo "  venv    : $VENV"
echo ""
echo "  Streams included in unified config:"
echo "    binance_ws            — btc_4h, btc_1d (RSI, EMA, ATR, volatility)"
echo "    binance_scalping      — btc_scalping_spot, btc_scalping_perp (OBI/TFI/spread/vol)"
echo "    binance_full_depth    — btc_full_depth (5000 levels, OBI 10/100/500, walls, cum vol)"
echo "    binance_funding       — btc_funding (every 15 min)"
echo "    binance_oi            — btc_oi (every 5 min)"
echo "    binance_ls_ratio      — btc_ls_ratio (every 5 min)"
echo "    binance_liquidations  — btc_liquidations (every 5 min)"
echo "    binance_vwap_context  — btc_vwap_context (4h VWAP vs price, dip_score, every 1 h)"
echo "    binance_volume_profile— btc_volume_profile (HVN zones, every 1 h)"
echo "    binance_macro_obi     — btc_macro_obi (trend direction from 1m flow, every 1 min)"
echo "    deribit_iv            — btc_dvol (every 5 min)"
echo "    fear_greed            — fear_greed (every 1 h)"
echo ""
echo "  Bots subscribe to indicators via config.json:"
echo "  \"indicators_addr\": \"$IND_ADDR\","
echo "  \"indicators_reg_addr\": \"$REG_ADDR\","
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
