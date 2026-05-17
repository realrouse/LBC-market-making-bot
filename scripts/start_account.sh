#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  TRADINEBOTTE — Start one account bot (Option B multi-bot)
#
#  Starts account_bot.py for ONE account. Each account has its own
#  TRADINEBOTTE_DIR containing config.json and private key.
#
#  Prerequisites:
#    1. feed.py running (bash scripts/start_feed.sh)
#    2. account config.json present in TRADINEBOTTE_DIR
#
#  Examples:
#    TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
#    TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
#    TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 \
#      TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
# ═══════════════════════════════════════════════════════════════════

INSTALL_DIR="${TRADINEBOTTE_DIR:-$HOME/tradinebotte}"
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"
VENV="$INSTALL_DIR/venv"
CONFIG="$INSTALL_DIR/config.json"
BOT_LOG="$INSTALL_DIR/account.log"

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: config.json not found in $INSTALL_DIR"
    echo "Run first: TRADINEBOTTE_DIR=\"$INSTALL_DIR\" python3 scripts/setup.py"
    exit 1
fi

if [ ! -d "$VENV" ]; then
    echo "ERROR: venv not found in $INSTALL_DIR"
    echo "Run first: TRADINEBOTTE_DIR=\"$INSTALL_DIR\" bash scripts/install.sh"
    exit 1
fi

# Detect if another account_bot.py for THIS directory is already running.
if pgrep -f '[a]ccount_bot.py' > /dev/null; then
    # Check if it's specifically for this INSTALL_DIR via env var.
    if pgrep -a -f '[a]ccount_bot.py' | grep -q "$INSTALL_DIR"; then
        echo "ERROR: account_bot.py already running for $INSTALL_DIR"
        exit 1
    fi
fi

if ! pgrep -f '[f]eed.py' > /dev/null; then
    echo "WARNING: feed.py is not running."
    echo "Run first: bash scripts/start_feed.sh"
    echo "(The account bot will wait for feed messages)"
fi

export TRADINEBOTTE_DIR="$INSTALL_DIR"
export TRADINEBOTTE_FEED_ADDR="${TRADINEBOTTE_FEED_ADDR:-tcp://127.0.0.1:5557}"

echo "Starting account_bot.py — dir=$INSTALL_DIR feed=$TRADINEBOTTE_FEED_ADDR"
echo "Log: $BOT_LOG"

nohup "$VENV/bin/python3" "$(dirname "$0")/../bot/account_bot.py" \
    </dev/null >> "$BOT_LOG" 2>&1 & disown
echo "PID: $!"
sleep 2

if pgrep -f '[a]ccount_bot.py' > /dev/null; then
    echo "Account bot running — PID: $(pgrep -f '[a]ccount_bot.py' | tail -1)"
else
    echo "Failed — check: $BOT_LOG"
    tail -20 "$BOT_LOG"
fi
