#!/usr/bin/env bash
# deploy_accumulation_claude4.sh — Deploy and (re)start the BTC accumulation bot
#                                  on the scalping test account (index 3 in TEST_USERS).
#
# Runs accumulation_bot.py alongside the existing orderbook_bot (independent DB/log).
#   accumulation_bot → accumulation_bot.pid / accumulation_bot.log / live_accum.db
#
# Reads credentials from ~/.tradinebotte-test.conf
#
# Usage:
#   bash scripts/deploy_accumulation_claude4.sh
#   bash scripts/deploy_accumulation_claude4.sh --skip-restart
#   bash scripts/deploy_accumulation_claude4.sh --verify-only

set -uo pipefail

BOT_NAME="accumulation_bot"
BOT_SCRIPT="accumulation_bot.py"
BOT_STRATEGY="strategies/accumulation/btc_accumulation_deepdip.json"

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
            grep '^#' "${BASH_SOURCE[0]}" | head -15 | sed 's/^# \?//'
            exit 0 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
    shift
done

CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
if [[ ! -f "$CONF" ]]; then
    echo -e "${RED}Missing configuration: $CONF${NC}"; exit 1
fi
source "$CONF"

SERVER="${TEST_SERVER:?}"
PORT="${TEST_PORT:-22}"
ALL_USERS=("${TEST_USERS[@]:?}")
ALL_PASSWORDS=("${TEST_PASSWORDS[@]:?}")
INSTALL_DIR="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"

SC_IDX="${TEST_SCALPING_USER_IDX:-3}"
SC_USER="${ALL_USERS[$SC_IDX]}"
SC_PASS="${ALL_PASSWORDS[$SC_IDX]}"

LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

_ssh() {
    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -p "$PORT" "$SC_USER@$SERVER" "$@" 2>&1
}

_rsync() {
    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='config.json' --exclude='live*.db' --exclude='*.log' \
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
        --filter='+ **/' --filter='+ *.json' --filter='- *' \
        -e "ssh -p $PORT -o StrictHostKeyChecking=yes" \
        "$LOCAL_REPO/tradinebotte-cex/strategies/" "$SC_USER@$SERVER:$INSTALL_DIR/strategies/" 2>&1

    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az \
        -e "ssh -p $PORT -o StrictHostKeyChecking=yes" \
        "$LOCAL_REPO/requirements.txt" "$SC_USER@$SERVER:$INSTALL_DIR/" 2>&1

    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' \
        -e "ssh -p $PORT -o StrictHostKeyChecking=yes" \
        "$LOCAL_REPO/tradinetools/" "$SC_USER@$SERVER:$INSTALL_DIR/tradinetools/" 2>&1
}

section "PRE-FLIGHT"
if [[ ! -x "$(command -v sshpass)" ]]; then
    err "sshpass not found"; exit 1
fi
ok "sshpass OK"
mkdir -p ~/.ssh && chmod 700 ~/.ssh
if ! ssh-keygen -F "[$SERVER]:$PORT" &>/dev/null && \
   ! ssh-keygen -F "$SERVER"         &>/dev/null; then
    ssh-keyscan -p "$PORT" -H "$SERVER" >> ~/.ssh/known_hosts 2>/dev/null
fi
info "Target: $SC_USER@$SERVER:$PORT  dir: $INSTALL_DIR"
info "Bot: $BOT_SCRIPT  strategy: $BOT_STRATEGY"

if [[ "$VERIFY_ONLY" == "false" ]]; then
    section "STEP 1 — RSYNC"
    if _rsync; then ok "Code synced"; else err "rsync failed"; exit 1; fi
fi

if [[ "$SKIP_RESTART" == "false" ]]; then
    section "STEP 2 — RESTART"

    REMOTE_CMD="set -e; cd $INSTALL_DIR"$'\n'
    REMOTE_CMD+="
echo 'updating dependencies...'
venv/bin/pip install --quiet -r $INSTALL_DIR/requirements.txt \
    && echo 'deps ok' || echo 'pip warning (non-fatal)'
venv/bin/pip install --quiet -e $INSTALL_DIR/tradinetools \
    && echo 'tradinetools ok' || echo 'tradinetools install warning (non-fatal)'

PF=$INSTALL_DIR/${BOT_NAME}.pid
if [ -f \"\$PF\" ]; then
    PID=\$(cat \"\$PF\")
    if kill -0 \"\$PID\" 2>/dev/null; then
        kill \"\$PID\" && echo \"stopped ${BOT_NAME} pid=\$PID\"
    else
        echo \"${BOT_NAME}: stale pid (was \$PID)\"
    fi
    rm -f \"\$PF\"
else
    echo \"${BOT_NAME}: not running\"
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
    if echo "$RESTART_OUT" | grep -q "^started"; then
        ok "${BOT_NAME} started"
    else
        err "${BOT_NAME} did not start"
    fi
    info "Waiting 10s for startup and initial buy..."
    sleep 10
fi

section "STEP 3 — VERIFY"
VERIFY_CMD="
PF=$INSTALL_DIR/${BOT_NAME}.pid
if [ -f \"\$PF\" ]; then
    PID=\$(cat \"\$PF\")
    if kill -0 \"\$PID\" 2>/dev/null; then echo \"  PID=\$PID running\"
    else echo \"  PID=\$PID NOT running\"; fi
else echo '  no pid file'; fi
tail -12 $INSTALL_DIR/${BOT_NAME}.log 2>/dev/null || echo '  (no log yet)'
"
VERIFY_OUT=$(_ssh "$VERIFY_CMD")
echo "$VERIFY_OUT"

if echo "$VERIFY_OUT" | grep -qP "PID=\d+ running"; then ok "${BOT_NAME}: running"
else err "${BOT_NAME}: NOT running"; fi
if echo "$VERIFY_OUT" | grep -qE "BUY|connected|Accumulation"; then ok "startup OK"
else warn "startup message not yet in log"; fi

section "RESULT"
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${BOLD}${GREEN}  SUCCESS — accumulation_bot running on $SC_USER${NC}"
    echo -e "  Log : $INSTALL_DIR/accumulation_bot.log"
    echo -e "  DB  : $INSTALL_DIR/live_accum.db"
    echo -e "  PID : $INSTALL_DIR/accumulation_bot.pid"
    exit 0
else
    echo -e "${BOLD}${RED}  FAILURE — $FAILURES issue(s)${NC}"; exit 1
fi
