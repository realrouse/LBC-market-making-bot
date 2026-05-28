#!/usr/bin/env bash
# deploy_scalping_claude4.sh — Deploy and (re)start the OBI orderbook bot on the
#                              dedicated scalping test account (index 3 in TEST_USERS).
#
# Runs a single orderbook_bot.py instance monitoring BTCUSDT spot + perp streams:
#   orderbook_bot  → orderbook_bot.pid / orderbook_bot.log / live_ob.db
#
# Also stops legacy candle_momentum / meanrev / breakout bots if still running.
#
# Reads credentials from ~/.tradinebotte-test.conf
#   TEST_USERS[3] / TEST_PASSWORDS[3]  (or TEST_SCALPING_USER_IDX to override index)
#
# Rules:
#   - ≤ 4 SSH connections total (rsync ×2 + restart + verify).
#   - PID-file stop/start — never pkill by name (would hit other users' processes).
#   - Paper trading mode — no API key needed.
#
# Usage:
#   bash scripts/deploy_scalping_claude4.sh
#   bash scripts/deploy_scalping_claude4.sh --skip-restart   # rsync only
#   bash scripts/deploy_scalping_claude4.sh --verify-only    # check status

set -uo pipefail

BOT_NAME="orderbook_bot"
BOT_SCRIPT="orderbook_bot.py"
BOT_STRATEGY="strategies/scalping/orderbook_btc.json"

# Legacy bots to stop if still running
LEGACY_BOTS=("scalping_candle_momentum" "scalping_meanrev" "scalping_breakout")

SKIP_RESTART=false
VERIFY_ONLY=false
FAILURES=0

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

section() { echo -e "\n${BOLD}${YELLOW}═══ $* ═══${NC}"; }
info()    { echo -e "${BLUE}  → $*${NC}"; }
ok()      { echo -e "${GREEN}  ✓ $*${NC}"; }
warn()    { echo -e "${YELLOW}  ! $*${NC}"; }
err()     { echo -e "${RED}  ✗ $*${NC}"; FAILURES=$((FAILURES + 1)); }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-restart) SKIP_RESTART=true ;;
        --verify-only)  VERIFY_ONLY=true; SKIP_RESTART=true ;;
        -h|--help)
            grep '^#' "${BASH_SOURCE[0]}" | head -20 | sed 's/^# \?//'
            exit 0 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
    shift
done

# ─── Config ────────────────────────────────────────────────────────────────────
CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
if [[ ! -f "$CONF" ]]; then
    echo -e "${RED}Missing configuration: $CONF${NC}"
    echo "  Add TEST_USERS[3]=<scalping_user> and TEST_PASSWORDS[3]=<pass> to $CONF"
    exit 1
fi
# shellcheck source=/dev/null
source "$CONF"

SERVER="${TEST_SERVER:?TEST_SERVER missing in $CONF}"
PORT="${TEST_PORT:-22}"
ALL_USERS=("${TEST_USERS[@]:?TEST_USERS missing in $CONF}")
ALL_PASSWORDS=("${TEST_PASSWORDS[@]:?TEST_PASSWORDS missing in $CONF}")
INSTALL_DIR="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"

# Scalping test account is at index 3 by default
SC_IDX="${TEST_SCALPING_USER_IDX:-3}"
if [[ "$SC_IDX" -ge "${#ALL_USERS[@]}" ]]; then
    echo -e "${RED}ERROR: TEST_SCALPING_USER_IDX=$SC_IDX is out of range "
    echo        "       (TEST_USERS has ${#ALL_USERS[@]} entries)${NC}"
    exit 1
fi
SC_USER="${ALL_USERS[$SC_IDX]}"
SC_PASS="${ALL_PASSWORDS[$SC_IDX]}"

LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# ─── SSH helpers ───────────────────────────────────────────────────────────────
_ssh() {
    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -p "$PORT" "$SC_USER@$SERVER" "$@" 2>&1
}

_rsync() {
    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='config.json' --exclude='live.db' --exclude='live_ob.db' --exclude='*.log' \
        --exclude='scripts' --exclude='tests' \
        -e "ssh -p $PORT -o StrictHostKeyChecking=yes" \
        "$LOCAL_REPO/tradinebotte-polymarket/" "$SC_USER@$SERVER:$INSTALL_DIR/" 2>&1
    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='scripts' --exclude='tests' \
        -e "ssh -p $PORT -o StrictHostKeyChecking=yes" \
        "$LOCAL_REPO/tradinebotte-cex/" "$SC_USER@$SERVER:$INSTALL_DIR/" 2>&1
    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az \
        -e "ssh -p $PORT -o StrictHostKeyChecking=yes" \
        "$LOCAL_REPO/requirements.txt" "$SC_USER@$SERVER:$INSTALL_DIR/" 2>&1

    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --filter='+ **/' --filter='+ *.json' --filter='- *' \
        -e "ssh -p $PORT -o StrictHostKeyChecking=yes" \
        "$LOCAL_REPO/tradinebotte-cex/strategies/" "$SC_USER@$SERVER:$INSTALL_DIR/strategies/" 2>&1
}

# ─── Pre-flight ────────────────────────────────────────────────────────────────
section "PRE-FLIGHT"

if [[ ! -x "$(command -v sshpass)" ]]; then
    err "sshpass not found — install with: apt-get install sshpass"
    exit 1
fi
ok "sshpass OK"

mkdir -p ~/.ssh && chmod 700 ~/.ssh
if ! ssh-keygen -F "[$SERVER]:$PORT" &>/dev/null && \
   ! ssh-keygen -F "$SERVER"         &>/dev/null; then
    info "Adding $SERVER:$PORT to known_hosts..."
    ssh-keyscan -p "$PORT" -H "$SERVER" >> ~/.ssh/known_hosts 2>/dev/null
fi
info "Target: $SC_USER@$SERVER:$PORT  install_dir: $INSTALL_DIR"
info "Strategy: $BOT_STRATEGY"

# ─── Step 1: rsync ─────────────────────────────────────────────────────────────
if [[ "$VERIFY_ONLY" == "false" ]]; then
    section "STEP 1 — RSYNC"
    if _rsync; then
        ok "Code synced to $SC_USER"
    else
        err "rsync failed"
        exit 1
    fi
fi

# ─── Step 2: stop + start all three bots (single SSH session) ──────────────────
if [[ "$SKIP_RESTART" == "false" ]]; then
    section "STEP 2 — RESTART (3 bots)"
    info "Stopping legacy and current bots, starting orderbook_bot..."

    REMOTE_CMD="set -e; cd $INSTALL_DIR; mkdir -p strategies"$'\n'
    REMOTE_CMD+="
if [ ! -x venv/bin/python3 ]; then
    echo 'Creating venv...'
    python3 -m venv venv
    echo 'Venv ready'
fi
echo 'updating dependencies...'
venv/bin/pip install --quiet -r $INSTALL_DIR/requirements.txt \
    && echo 'deps ok' || echo 'pip warning (non-fatal)'
"
    # Stop legacy candle/meanrev/breakout bots if still running
    for LEGACY in scalping_candle_momentum scalping_meanrev scalping_breakout; do
        REMOTE_CMD+="
PF=$INSTALL_DIR/${LEGACY}.pid
if [ -f \"\$PF\" ]; then
    PID=\$(cat \"\$PF\")
    if kill -0 \"\$PID\" 2>/dev/null; then
        kill \"\$PID\" && echo \"stopped legacy ${LEGACY} pid=\$PID\"
    fi
    rm -f \"\$PF\"
fi
"
    done

    # Stop current orderbook_bot if running
    REMOTE_CMD+="
PF=$INSTALL_DIR/${BOT_NAME}.pid
if [ -f \"\$PF\" ]; then
    PID=\$(cat \"\$PF\")
    if kill -0 \"\$PID\" 2>/dev/null; then
        kill \"\$PID\" && echo \"stopped ${BOT_NAME} pid=\$PID\" || echo 'kill failed ${BOT_NAME}'
    else
        echo \"${BOT_NAME}: stale pid file (was \$PID)\"
    fi
    rm -f \"\$PF\"
else
    echo \"${BOT_NAME}: not running (no pid file)\"
fi
sleep 2

nohup venv/bin/python3 ${BOT_SCRIPT} \
    --strategy $INSTALL_DIR/${BOT_STRATEGY} \
    --dir $INSTALL_DIR \
    </dev/null >>$INSTALL_DIR/${BOT_NAME}.log 2>&1 &
BOT_PID=\$!
disown \$BOT_PID
echo \$BOT_PID > $INSTALL_DIR/${BOT_NAME}.pid
echo \"started ${BOT_NAME} pid=\$BOT_PID\"
"

    RESTART_OUT=$(_ssh "$REMOTE_CMD")
    echo "$RESTART_OUT"

    STARTED=$(echo "$RESTART_OUT" | grep -c "^started" || true)
    if [[ "$STARTED" -eq 1 ]]; then
        ok "${BOT_NAME} started"
    else
        err "${BOT_NAME} did not start (check log above)"
    fi

    info "Waiting 8s for startup and WebSocket connection..."
    sleep 8
fi

# ─── Step 3: verify ────────────────────────────────────────────────────────────
section "STEP 3 — VERIFY"

VERIFY_CMD="
echo '--- ${BOT_NAME} ---'
PF=$INSTALL_DIR/${BOT_NAME}.pid
if [ -f \"\$PF\" ]; then
    PID=\$(cat \"\$PF\")
    if kill -0 \"\$PID\" 2>/dev/null; then
        echo \"  PID=\$PID running\"
    else
        echo \"  PID=\$PID NOT running (stale)\"
    fi
else
    echo '  no pid file'
fi
tail -8 $INSTALL_DIR/${BOT_NAME}.log 2>/dev/null || echo '  (no log yet)'
"

VERIFY_OUT=$(_ssh "$VERIFY_CMD")
echo "$VERIFY_OUT"

if echo "$VERIFY_OUT" | grep -qP "PID=\d+ running"; then
    ok "${BOT_NAME}: running"
else
    err "${BOT_NAME}: NOT running"
fi
if echo "$VERIFY_OUT" | grep -qE "connected|started|OrderBook bot"; then
    ok "${BOT_NAME}: startup OK"
else
    warn "${BOT_NAME}: startup message not yet in log"
fi

# ─── Report ────────────────────────────────────────────────────────────────────
section "RESULT"
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${BOLD}${GREEN}  SUCCESS — $SC_USER: orderbook_bot running${NC}"
    echo -e "  Log  : $INSTALL_DIR/orderbook_bot.log"
    echo -e "  DB   : $INSTALL_DIR/live_ob.db"
    echo -e "  PID  : $INSTALL_DIR/orderbook_bot.pid"
    exit 0
else
    echo -e "${BOLD}${RED}  FAILURE — $FAILURES issue(s) — check logs above${NC}"
    exit 1
fi
