#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  TRADINEBOTTE — Shared WebSocket feed (Option B multi-bot)
#
#  Starts feed.py: a single WebSocket connection to Polymarket,
#  broadcasts updates via ZeroMQ PUB on TRADINEBOTTE_FEED_ADDR
#  (default: tcp://127.0.0.1:5557).
#
#  Start BEFORE account bots:
#    bash tradinebotte-polymarket/scripts/start_feed.sh
#    TRADINEBOTTE_DIR=~/account-a bash tradinebotte-polymarket/scripts/start_account.sh
#    TRADINEBOTTE_DIR=~/account-b bash tradinebotte-polymarket/scripts/start_account.sh
#
#  Custom address:
#    TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 bash tradinebotte-polymarket/scripts/start_feed.sh
# ═══════════════════════════════════════════════════════════════════

INSTALL_DIR="${TRADINEBOTTE_DIR:-$HOME/tradinebotte}"
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"
if [ -x "$INSTALL_DIR/.venv/bin/python3" ]; then
    VENV="$INSTALL_DIR/.venv"
elif [ -x "$INSTALL_DIR/venv/bin/python3" ]; then
    VENV="$INSTALL_DIR/venv"
else
    echo "ERROR: virtualenv not found in $INSTALL_DIR/{.venv,venv}"
    echo "Run first: bash scripts/install.sh"
    exit 1
fi
FEED_LOG="$INSTALL_DIR/feed.log"
FEED_PID_FILE="$INSTALL_DIR/feed.pid"

if [ -f "$FEED_PID_FILE" ]; then
    _pid=$(cat "$FEED_PID_FILE")
    if kill -0 "$_pid" 2>/dev/null; then
        echo "ERROR: feed.py already running (PID: $_pid)"
        echo "Stop it first: kill $_pid"
        exit 1
    fi
    rm -f "$FEED_PID_FILE"
fi

_UID=$(id -u)
_IPC_DIR="/run/user/${_UID}"; [ -d "${_IPC_DIR}" ] || { _IPC_DIR="/tmp/tradinebotte-${_UID}"; mkdir -p "${_IPC_DIR}"; chmod 700 "${_IPC_DIR}"; }
export TRADINEBOTTE_FEED_ADDR="${TRADINEBOTTE_FEED_ADDR:-ipc://${_IPC_DIR}/tradinebotte-feed.sock}"
echo "Starting feed.py — PUB on $TRADINEBOTTE_FEED_ADDR"
echo "Log: $FEED_LOG"

nohup "$VENV/bin/python3" "$(dirname "$0")/../feed.py" \
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
