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
if pgrep -a -f '[i]ndicators.py' 2>/dev/null | grep -qF "$CONFIG"; then
    echo "ERROR: indicators.py already running for this config: $CONFIG"
    echo "Stop it first: pkill -f '[i]ndicators.py'"
    exit 1
fi

echo "Starting indicators.py — config=$CONFIG"
echo "Log: $IND_LOG"

nohup "$VENV/bin/python3" "$BOT_ROOT/bot/indicators.py" \
    --config "$CONFIG" \
    >> "$IND_LOG" 2>&1 &
echo "PID: $!"
sleep 2

if pgrep -f '[i]ndicators.py' > /dev/null; then
    echo "Indicators running — PID: $(pgrep -f '[i]ndicators.py' | tail -1)"
else
    echo "Failed — check: $IND_LOG"
    tail -20 "$IND_LOG"
fi
