#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  start_collector.sh — Deploy and start the data-collection bot
#
#  Deploys tradinebotte code to the first VPS deployment account and
#  launches it in simulation mode with 1-second snapshots.
#  No real orders are placed; only live.db + snapshots are written.
#
#  Reads credentials from ~/.tradinebotte-test.conf (same format as
#  test_multibot.conf.example):
#    TEST_SERVER, TEST_PORT, TEST_USERS[0], TEST_PASSWORDS[0]
#
#  Usage:
#    bash scripts/start_collector.sh [options]
#
#  Options:
#    --snapshot-interval N   snapshot interval in seconds (default: 1)
#    --dry-run               print SSH/rsync commands without executing
#    --status                check if collector is running (no deploy)
#    --stop                  stop the running collector
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────
SNAPSHOT_INTERVAL=1
DRY_RUN=0
STATUS_ONLY=0
STOP_ONLY=0

for _arg in "$@"; do
    case "$_arg" in
        --dry-run)  DRY_RUN=1 ;;
        --status)   STATUS_ONLY=1 ;;
        --stop)     STOP_ONLY=1 ;;
    esac
done
# Handle --snapshot-interval N (two-token option)
_prev=""
for _arg in "$@"; do
    if [ "$_prev" = "--snapshot-interval" ]; then
        SNAPSHOT_INTERVAL="$_arg"
    fi
    _prev="$_arg"
done

# ── Load conf ─────────────────────────────────────────────────────
CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
if [ ! -f "$CONF" ]; then
    echo "❌ ERROR: $CONF not found."
    echo "   Copy scripts/test_multibot.conf.example → ~/.tradinebotte-test.conf and fill in values."
    exit 1
fi
# shellcheck source=/dev/null
source "$CONF"

SERVER="${TEST_SERVER:?TEST_SERVER not set in $CONF}"
PORT="${TEST_PORT:-22}"
RUSER="${TEST_USERS[0]:?TEST_USERS[0] not set in $CONF}"
PASSWORD="${TEST_PASSWORDS[0]:?TEST_PASSWORDS[0] not set in $CONF}"

INSTALL_DIR="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"
COLLECTOR_DIR="${TEST_REMOTE_COLLECTOR_DIR:-~/tradinebotte-collector}"

LOCAL_REPO="$(cd "$(dirname "$0")/.." && pwd)"

# ── Helpers ───────────────────────────────────────────────────────
# _ssh_cmd: run a simple one-liner on the remote host
_ssh_cmd() {
    if [ "$DRY_RUN" = "1" ]; then echo "[DRY-RUN] ssh $*"; return 0; fi
    SSHPASS="$PASSWORD" sshpass -e ssh -p "$PORT" \
        -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 \
        "$RUSER@$SERVER" "$@"
}

# _ssh_script: pipe a heredoc script to remote bash via stdin (avoids
# argument-length issues and SSH rate-limiting from multiple connections)
_ssh_script() {
    if [ "$DRY_RUN" = "1" ]; then echo "[DRY-RUN] ssh bash -s (stdin)"; cat; return 0; fi
    SSHPASS="$PASSWORD" sshpass -e ssh -p "$PORT" \
        -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 \
        "$RUSER@$SERVER" bash -s
}

# ── --status ──────────────────────────────────────────────────────
if [ "$STATUS_ONLY" = "1" ]; then
    echo "Checking collector status on $SERVER ($RUSER)..."
    _ssh_cmd "pgrep -u \$(id -u) -f live_bot.py > /dev/null && echo '✅ Running PID:' \$(pgrep -u \$(id -u) -f live_bot.py) || echo '⭕ Not running'"
    exit 0
fi

# ── --stop ────────────────────────────────────────────────────────
if [ "$STOP_ONLY" = "1" ]; then
    echo "Stopping collector on $SERVER ($RUSER)..."
    _ssh_cmd "pkill -u \$(id -u) -f live_bot.py && echo '✅ Stopped' || echo '⭕ Not running'"
    exit 0
fi

# ── Deploy code via rsync ──────────────────────────────────────────
echo "Deploying code to $RUSER@$SERVER:$INSTALL_DIR ..."
if [ "$DRY_RUN" = "1" ]; then
    echo "[DRY-RUN] rsync -az --delete ... $LOCAL_REPO/ $RUSER@$SERVER:$INSTALL_DIR/"
else
    SSHPASS="$PASSWORD" sshpass -e rsync -az --delete \
        --exclude='.git' \
        --exclude='venv/' \
        --exclude='*.pyc' \
        --exclude='__pycache__' \
        --exclude='config.json' \
        --exclude='live.db' \
        --exclude='live.log' \
        --exclude='*.db-shm' \
        --exclude='*.db-wal' \
        -e "ssh -p $PORT -o StrictHostKeyChecking=accept-new" \
        "$LOCAL_REPO/" "$RUSER@$SERVER:$INSTALL_DIR/"
fi
echo "✅ Code deployed."

# ── Prepare env + stop old instance + launch — all in one SSH session
# Bot is run from INSTALL_DIR (where api_polymarket.py, bot_utils.py live flat)
# with TRADINEBOTTE_DIR=COLLECTOR_DIR so data (live.db, live.log) is isolated.
echo "Preparing environment and launching collector (snapshot-interval=${SNAPSHOT_INTERVAL}s)..."

_ssh_script << REMOTE
set -e

INSTALL=$INSTALL_DIR
COLLECTOR=$COLLECTOR_DIR
SNAP=$SNAPSHOT_INTERVAL

# Create collector data dir
mkdir -p "\$COLLECTOR"

# Remove broken venv (dir exists but bin/python3 missing)
if [ -d "\$INSTALL/venv" ] && [ ! -x "\$INSTALL/venv/bin/python3" ]; then
    echo "Removing broken venv..."
    rm -rf "\$INSTALL/venv"
fi

# Install venv if absent
if [ ! -x "\$INSTALL/venv/bin/python3" ]; then
    echo "Installing venv..."
    TRADINEBOTTE_DIR="\$INSTALL" bash "\$INSTALL/scripts/install.sh"
fi

# Minimal config.json for simulate mode (no API keys needed)
if [ ! -f "\$COLLECTOR/config.json" ]; then
    echo '{"lang": "EN"}' > "\$COLLECTOR/config.json"
    echo "Created minimal config.json (simulate mode)."
fi

# Copy strategies
mkdir -p "\$COLLECTOR/strategies"
cp -r "\$INSTALL/strategies/." "\$COLLECTOR/strategies/" 2>/dev/null || true

# Stop any running instance
pkill -u "\$(id -u)" -f live_bot.py 2>/dev/null && echo "Stopped previous instance." || true
sleep 2

# Launch
PYTHON="\$INSTALL/venv/bin/python3"
LOG="\$COLLECTOR/live.log"
export TRADINEBOTTE_DIR="\$COLLECTOR"
cd "\$INSTALL/bot"
nohup "\$PYTHON" "\$INSTALL/bot/live_bot.py" \
    --simulate \
    --snapshot-interval "\$SNAP" \
    >> "\$LOG" 2>&1 &
sleep 5

if pgrep -u "\$(id -u)" -f live_bot.py > /dev/null; then
    echo "✅ Collector running — PID: \$(pgrep -u \$(id -u) -f live_bot.py)"
    echo "   Data: \$COLLECTOR/live.db"
    echo "   Logs: tail -f \$COLLECTOR/live.log"
else
    echo "❌ Collector failed — last log lines:"
    tail -20 "\$LOG" 2>/dev/null || echo "(no log)"
    exit 1
fi
REMOTE
