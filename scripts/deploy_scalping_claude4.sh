#!/usr/bin/env bash
# deploy_scalping_claude4.sh — Deploy and (re)start three scalping bots on the
#                              dedicated scalping test account (index 3 in TEST_USERS).
#
# Each bot runs a different scalping strategy:
#   1. candle_momentum  → scalping_candle_momentum.pid/log/db
#   2. meanrev          → scalping_meanrev.pid/log/db
#   3. breakout         → scalping_breakout.pid/log/db
#
# Reads credentials from ~/.tradinebotte-test.conf
#   TEST_USERS[3] / TEST_PASSWORDS[3]  (or TEST_SCALPING_USER_IDX to override index)
#
# Rules:
#   - ≤ 4 SSH connections total (rsync ×2 + restart + verify).
#   - PID-file stop/start — never pkill by name (would hit other users' processes).
#   - No API key needed — orders are simulated automatically.
#
# Usage:
#   bash scripts/deploy_scalping_claude4.sh
#   bash scripts/deploy_scalping_claude4.sh --skip-restart   # rsync only
#   bash scripts/deploy_scalping_claude4.sh --verify-only    # check status

set -uo pipefail

STRATEGIES=(
    "scalping_candle_momentum"
    "scalping_meanrev"
    "scalping_breakout"
)

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

LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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
        --exclude='config.json' --exclude='live.db' --exclude='*.log' \
        -e "ssh -p $PORT -o StrictHostKeyChecking=yes" \
        "$LOCAL_REPO/bot/" "$SC_USER@$SERVER:$INSTALL_DIR/" 2>&1

    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --include='*.json' --exclude='*' \
        -e "ssh -p $PORT -o StrictHostKeyChecking=yes" \
        "$LOCAL_REPO/strategies/" "$SC_USER@$SERVER:$INSTALL_DIR/strategies/" 2>&1
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
info "Strategies: ${STRATEGIES[*]}"

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
    info "Stopping old bots and starting updated ones..."

    # Build the remote command dynamically for all strategies
    REMOTE_CMD="set -e; cd $INSTALL_DIR; mkdir -p strategies"$'\n'
    for STRAT in "${STRATEGIES[@]}"; do
        REMOTE_CMD+="
PF=$INSTALL_DIR/${STRAT}.pid
if [ -f \"\$PF\" ]; then
    PID=\$(cat \"\$PF\")
    if kill -0 \"\$PID\" 2>/dev/null; then
        kill \"\$PID\" && echo \"stopped ${STRAT} pid=\$PID\" || echo 'kill failed ${STRAT}'
    else
        echo \"${STRAT}: stale pid file (was \$PID)\"
    fi
    rm -f \"\$PF\"
else
    echo \"${STRAT}: not running (no pid file)\"
fi
"
    done

    REMOTE_CMD+="sleep 2"$'\n'

    for STRAT in "${STRATEGIES[@]}"; do
        REMOTE_CMD+="
nohup venv/bin/python3 scalping_bot.py \
    --strategy $INSTALL_DIR/strategies/${STRAT}.json \
    --dir $INSTALL_DIR \
    </dev/null >>$INSTALL_DIR/${STRAT}.log 2>&1 &
BOT_PID=\$!
disown \$BOT_PID
echo \$BOT_PID > $INSTALL_DIR/${STRAT}.pid
echo \"started ${STRAT} pid=\$BOT_PID\"
"
    done

    RESTART_OUT=$(_ssh "$REMOTE_CMD")
    echo "$RESTART_OUT"

    STARTED=$(echo "$RESTART_OUT" | grep -c "^started" || true)
    if [[ "$STARTED" -eq "${#STRATEGIES[@]}" ]]; then
        ok "All ${#STRATEGIES[@]} bots started"
    else
        err "Only $STARTED / ${#STRATEGIES[@]} bots confirmed started"
    fi

    info "Waiting 8s for startup + Binance history warm-up..."
    sleep 8
fi

# ─── Step 3: verify ────────────────────────────────────────────────────────────
section "STEP 3 — VERIFY"

VERIFY_CMD=""
for STRAT in "${STRATEGIES[@]}"; do
    VERIFY_CMD+="
echo '--- ${STRAT} ---'
PF=$INSTALL_DIR/${STRAT}.pid
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
tail -5 $INSTALL_DIR/${STRAT}.log 2>/dev/null || echo '  (no log yet)'
"
done

VERIFY_OUT=$(_ssh "$VERIFY_CMD")
echo "$VERIFY_OUT"

for STRAT in "${STRATEGIES[@]}"; do
    if echo "$VERIFY_OUT" | grep -A5 "^--- ${STRAT}" | grep -q "PID=.* running"; then
        ok "$STRAT: running"
    else
        err "$STRAT: NOT running"
    fi
    if echo "$VERIFY_OUT" | grep -A10 "^--- ${STRAT}" | grep -qE "History loaded|WebSocket connected|ScalpingBot started"; then
        ok "$STRAT: startup OK"
    else
        warn "$STRAT: startup message not yet in log"
    fi
done

# ─── Report ────────────────────────────────────────────────────────────────────
section "RESULT"
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${BOLD}${GREEN}  SUCCESS — $SC_USER: all scalping bots running${NC}"
    echo -e "  Logs : $INSTALL_DIR/scalping_<strategy>.log"
    echo -e "  DBs  : $INSTALL_DIR/scalping_<strategy>.db"
    exit 0
else
    echo -e "${BOLD}${RED}  FAILURE — $FAILURES issue(s) — check logs above${NC}"
    exit 1
fi
