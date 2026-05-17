#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  TRADINEBOTTE — Shared WebSocket feed (Option B multi-bot)
#
#  Starts feed.py: a single WebSocket connection to Polymarket,
#  broadcasts updates via ZeroMQ PUB on TRADINEBOTTE_FEED_ADDR
#  (default: tcp://127.0.0.1:5557).
#
#  Start BEFORE account bots:
#    bash scripts/start_feed.sh
#    TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
#    TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
#
#  Custom address:
#    TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 bash scripts/start_feed.sh
# ═══════════════════════════════════════════════════════════════════

INSTALL_DIR="${TRADINEBOTTE_DIR:-$HOME/tradinebotte}"
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"
VENV="$INSTALL_DIR/venv"
FEED_LOG="$INSTALL_DIR/feed.log"
FEED_PID_FILE="$INSTALL_DIR/feed.pid"

if [ ! -d "$VENV" ]; then
    echo "ERROR: venv not found in $INSTALL_DIR"
    echo "Run first: bash scripts/install.sh"
    exit 1
fi

if [ -f "$FEED_PID_FILE" ]; then
    _pid=$(cat "$FEED_PID_FILE")
    if kill -0 "$_pid" 2>/dev/null; then
        echo "ERROR: feed.py already running (PID: $_pid)"
        echo "Stop it first: kill $_pid"
        exit 1
    fi
    rm -f "$FEED_PID_FILE"
fi

export TRADINEBOTTE_FEED_ADDR="${TRADINEBOTTE_FEED_ADDR:-tcp://127.0.0.1:5557}"
echo "Starting feed.py — PUB on $TRADINEBOTTE_FEED_ADDR"
echo "Log: $FEED_LOG"

nohup "$VENV/bin/python3" "$(dirname "$0")/../bot/feed.py" \
    </dev/null >> "$FEED_LOG" 2>&1 &
_pid=$!
disown "$_pid"
echo "$_pid" > "$FEED_PID_FILE"
echo "PID: $_pid"
sleep 2

if kill -0 "$_pid" 2>/dev/null; then
    echo "Feed running — PID: $_pid"
else
    rm -f "$FEED_PID_FILE"
    echo "Failed — check: $FEED_LOG"
    tail -20 "$FEED_LOG"
fi
