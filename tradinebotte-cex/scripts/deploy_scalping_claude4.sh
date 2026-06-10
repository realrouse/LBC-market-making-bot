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
GIT_HASH=$(git -C "$LOCAL_REPO" rev-parse --short HEAD 2>/dev/null || echo "unknown")

# ─── SSH helpers ───────────────────────────────────────────────────────────────
_ssh() {
    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
        -o PreferredAuthentications=password \
        -p "$PORT" "$SC_USER@$SERVER" "$@" 2>&1
}

_rsync() {
    local ssh_opts="-p $PORT -o StrictHostKeyChecking=yes -o PreferredAuthentications=password"
    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='config.json' --exclude='live.db' --exclude='live_ob.db' --exclude='*.log' \
        --exclude='scripts' --exclude='tests' \
        --exclude='account_bot.py' --exclude='feed.py' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-polymarket/" "$SC_USER@$SERVER:$INSTALL_DIR/" 2>&1
    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='scripts' --exclude='tests' \
        --exclude='scalping_bot.py' --exclude='scalping_math.py' \
        --exclude='api_bitstamp.py' --exclude='api_mexc.py' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-cex/" "$SC_USER@$SERVER:$INSTALL_DIR/" 2>&1
    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/requirements.txt" "$SC_USER@$SERVER:$INSTALL_DIR/" 2>&1

    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --filter='+ **/' --filter='+ *.json' --filter='- *' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-cex/strategies/" "$SC_USER@$SERVER:$INSTALL_DIR/strategies/" 2>&1

    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinetools/" "$SC_USER@$SERVER:$INSTALL_DIR/tradinetools/" 2>&1
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

# ─── Step 2: stop + start orderbook_bot (single SSH session) ───────────────────
if [[ "$SKIP_RESTART" == "false" ]]; then
    section "STEP 2 — RESTART"
    info "Stopping legacy and current bots, starting orderbook_bot..."

    REMOTE_CMD="cd $INSTALL_DIR; mkdir -p strategies"$'\n'
    REMOTE_CMD+="echo '${GIT_HASH}' > $INSTALL_DIR/version.stamp
if [ ! -x venv/bin/python3 ] && [ ! -x .venv/bin/python3 ]; then
    echo 'Creating venv...'
    python3 -m venv venv
    echo 'Venv ready'
fi
echo 'updating dependencies...'
if [ -d $INSTALL_DIR/.venv ]; then VENV=$INSTALL_DIR/.venv/bin; else VENV=$INSTALL_DIR/venv/bin; fi
\$VENV/pip install --quiet -r $INSTALL_DIR/requirements.txt 2>/dev/null && echo 'deps ok' || echo 'pip warning (non-fatal)'
\$VENV/pip install --quiet -e $INSTALL_DIR/tradinetools 2>/dev/null && echo 'tradinetools ok' || echo 'tradinetools warning (non-fatal)'
"
    # Stop legacy candle/meanrev/breakout bots if still running (always)
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

    REMOTE_CMD+="
export XDG_RUNTIME_DIR=/run/user/\$(id -u)
if systemctl --user is-active tradinebotte-orderbook.service >/dev/null 2>&1 \
   || systemctl --user is-enabled tradinebotte-orderbook.service >/dev/null 2>&1; then
    echo 'detected user service: tradinebotte-orderbook.service'
    for P in \$(pgrep -u \"\$(whoami)\" -f \"${BOT_SCRIPT}\" 2>/dev/null || true); do
        if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
            kill \"\$P\" 2>/dev/null && echo \"killed stale ${BOT_NAME} pid=\$P\"
        fi
    done
    sleep 2
    systemctl --user restart tradinebotte-orderbook.service \
        && echo 'systemd restarted' \
        || echo 'user service restart failed'
else
    for P in \$(pgrep -u \"\$(whoami)\" -f \"${BOT_SCRIPT}\" 2>/dev/null || true); do
        if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
            if kill \"\$P\" 2>/dev/null; then echo \"killed stale ${BOT_NAME} pid=\$P\"; fi
        fi
    done
    sleep 1
    rm -f $INSTALL_DIR/${BOT_NAME}.pid || true
    sleep 1
    nohup \$VENV/python3 ${BOT_SCRIPT} \
        --strategy $INSTALL_DIR/${BOT_STRATEGY} \
        --dir $INSTALL_DIR \
        </dev/null >>$INSTALL_DIR/${BOT_NAME}.log 2>&1 &
    BOT_PID=\$!
    disown \$BOT_PID 2>/dev/null || true
    echo \$BOT_PID > $INSTALL_DIR/${BOT_NAME}.pid
    echo \"started ${BOT_NAME} pid=\$BOT_PID\"
fi
"

    RESTART_OUT=$(_ssh "$REMOTE_CMD")
    echo "$RESTART_OUT"

    if echo "$RESTART_OUT" | grep -qE 'systemd restarted|^started'; then
        ok "${BOT_NAME} started"
    else
        err "${BOT_NAME} did not start (check log above)"
    fi

    if echo "$RESTART_OUT" | grep -q "systemd restarted"; then
        info "Waiting 36s for systemd restart (RestartSec=30 + startup)..."
        sleep 36
    else
        info "Waiting 8s for startup and WebSocket connection..."
        sleep 8
    fi
fi

# ─── Step 3: verify ────────────────────────────────────────────────────────────
section "STEP 3 — VERIFY"

VERIFY_CMD="
export XDG_RUNTIME_DIR=/run/user/\$(id -u)
echo '--- ${BOT_NAME} ---'
MPID=\"\"
for P in \$(pgrep -u \"\$(whoami)\" -f '${BOT_SCRIPT}' 2>/dev/null || true); do
    if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
        MPID=\$P; break
    fi
done
if [ -n \"\$MPID\" ]; then
    echo \"  PID=\$MPID running\"
else
    STATE=\$(systemctl --user is-active tradinebotte-orderbook.service 2>/dev/null || echo unknown)
    echo \"  PID=0 NOT running — user service state=\$STATE\"
fi
tail -8 $INSTALL_DIR/${BOT_NAME}.log 2>/dev/null || echo '  (no log yet)'
"

VERIFY_OUT=$(_ssh "$VERIFY_CMD")
echo "$VERIFY_OUT"

if echo "$VERIFY_OUT" | grep -qE 'PID=[1-9][0-9]* running'; then
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
