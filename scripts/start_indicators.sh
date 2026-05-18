#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  TRADINEBOTTE — Technical indicator service
#
#  Starts indicators.py with the chosen JSON config file.
#  Each account uses its own config file and its own ZMQ port.
#
#  Examples:
#    # account-a (4h, port 5559):
#    TRADINEBOTTE_INDICATORS_CONFIG=strategies/indicators_4h_bitcoin.json \
#      bash scripts/start_indicators.sh
#
#    # account-b (daily, port 5560):
#    TRADINEBOTTE_INDICATORS_CONFIG=strategies/indicators_1d_bitcoin.json \
#      bash scripts/start_indicators.sh
#
#    # Custom deployment directory:
#    TRADINEBOTTE_DIR=~/account-a \
#    TRADINEBOTTE_INDICATORS_CONFIG=strategies/indicators_4h_bitcoin.json \
#      bash scripts/start_indicators.sh
# ═══════════════════════════════════════════════════════════════════

INSTALL_DIR="${TRADINEBOTTE_DIR:-$HOME/tradinebotte}"
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"
VENV="$INSTALL_DIR/venv"
BOT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IND_LOG="$INSTALL_DIR/indicators.log"
IND_PID_FILE="$INSTALL_DIR/indicators.pid"

# Default: 4h config.  Override with TRADINEBOTTE_INDICATORS_CONFIG.
CONFIG="${TRADINEBOTTE_INDICATORS_CONFIG:-$BOT_ROOT/strategies/indicators_4h_bitcoin.json}"

if [ ! -d "$VENV" ]; then
    echo "ERROR: venv not found in $INSTALL_DIR"
    echo "Run first: bash scripts/install.sh"
    exit 1
fi

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: config file not found: $CONFIG"
    exit 1
fi

# Refuse to start a second process with the same config file.
if [ -f "$IND_PID_FILE" ]; then
    _pid=$(cat "$IND_PID_FILE")
    if kill -0 "$_pid" 2>/dev/null; then
        echo "ERROR: indicators.py already running (PID: $_pid)"
        echo "Stop it first: kill $_pid"
        exit 1
    fi
    rm -f "$IND_PID_FILE"
fi

echo "Starting indicators.py — config=$CONFIG"
echo "Log: $IND_LOG"

nohup "$VENV/bin/python3" "$BOT_ROOT/bot/indicators.py" \
    --config "$CONFIG" \
    </dev/null >> "$IND_LOG" 2>&1 &
_pid=$!
disown "$_pid"
echo "$_pid" > "$IND_PID_FILE"
echo "PID: $_pid"
sleep 2

if kill -0 "$_pid" 2>/dev/null; then
    echo "Indicators running — PID: $_pid"
else
    rm -f "$IND_PID_FILE"
    echo "Failed — check: $IND_LOG"
    tail -20 "$IND_LOG"
fi
