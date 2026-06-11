#!/usr/bin/env bash
# deploy_grid_mexc.sh — Deploy the MEXC Futures grid bot (simulation mode) on the test account.
#
# Runs live_bot.py with strategy_type=grid / connector=mexc_futures.
# Simulation mode is automatic: no MEXC_FUTURES_API_KEY/SECRET on the remote →
# all orders are returned as "sim_..." IDs (no real trades, no real API calls).
#
# Default target: TEST_GRID_MEXC_USER_IDX=5 (test-only account, index 5 in TEST_USERS).
# Override: TEST_GRID_MEXC_USER_IDX=<n> bash scripts/deploy_grid_mexc.sh
#
# Usage:
#   bash tradinebotte-cex/scripts/deploy_grid_mexc.sh
#   bash tradinebotte-cex/scripts/deploy_grid_mexc.sh --skip-restart
#   bash tradinebotte-cex/scripts/deploy_grid_mexc.sh --verify-only

set -uo pipefail

LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GIT_HASH=$(git -C "$LOCAL_REPO" rev-parse --short HEAD 2>/dev/null || echo "unknown")
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
    echo "  cp scripts/test_multibot.conf.example ~/.tradinebotte-test.conf"
    exit 1
fi
# shellcheck source=/dev/null
source "$CONF"

SERVER="${TEST_SERVER:?TEST_SERVER missing in $CONF}"
PORT="${TEST_PORT:-22}"
ALL_USERS=("${TEST_USERS[@]:?TEST_USERS missing in $CONF}")
ALL_PASSWORDS=("${TEST_PASSWORDS[@]:?TEST_PASSWORDS missing in $CONF}")
INSTALL_DIR="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"

GR_IDX="${TEST_GRID_MEXC_USER_IDX:-5}"
if [[ "$GR_IDX" -ge "${#ALL_USERS[@]}" ]]; then
    echo "ERROR: TEST_GRID_MEXC_USER_IDX=$GR_IDX is out of range (only ${#ALL_USERS[@]} users in $CONF)"
    echo "  Add the test account credentials to $CONF (TEST_USERS index $GR_IDX) or set TEST_GRID_MEXC_USER_IDX"
    exit 1
fi
GR_USER="${ALL_USERS[$GR_IDX]}"
GR_PASS="${ALL_PASSWORDS[$GR_IDX]}"

STRATEGY_PATH="${TEST_GRID_MEXC_STRATEGY:-strategies/grid/grid_BTC_USDT_mexc_futures.json}"

# ─── SSH helpers ───────────────────────────────────────────────────────────────

_ssh() {
    SSHPASS="$GR_PASS" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
        -o PreferredAuthentications=password \
        -p "$PORT" "$GR_USER@$SERVER" "$@" 2>&1
}

_rsync() {
    local ssh_opts="-p $PORT -o StrictHostKeyChecking=yes -o PreferredAuthentications=password"

    # tradinebotte-polymarket/ (live_bot.py and shared code)
    SSHPASS="$GR_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='live.db' --exclude='*.log' --exclude='venv' --exclude='.venv' \
        --exclude='scripts' --exclude='tests' \
        --exclude='account_bot.py' --exclude='feed.py' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-polymarket/" "$GR_USER@$SERVER:$INSTALL_DIR/" 2>&1 || return 1

    # tradinebotte-cex/ (api adapters + strategy engines + connectors)
    # Include api_mexc_futures.py; exclude spot-only adapters not needed for this bot.
    SSHPASS="$GR_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='scripts' --exclude='tests' \
        --exclude='accumulation_bot.py' --exclude='orderbook_bot.py' \
        --exclude='earn_manager.py' \
        --exclude='api_bitstamp.py' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-cex/" "$GR_USER@$SERVER:$INSTALL_DIR/" 2>&1 || return 1

    # CEX strategy JSON files
    SSHPASS="$GR_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --filter='+ **/' --filter='+ *.json' --filter='- *' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-cex/strategies/" "$GR_USER@$SERVER:$INSTALL_DIR/strategies/" 2>&1 || return 1

    # requirements.txt
    SSHPASS="$GR_PASS" /usr/bin/sshpass -e \
        rsync -az \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/requirements.txt" "$GR_USER@$SERVER:$INSTALL_DIR/" 2>&1 || return 1

    # tradinetools shared library
    SSHPASS="$GR_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinetools/" "$GR_USER@$SERVER:$INSTALL_DIR/tradinetools/" 2>&1 || return 1
}

# ─── Pre-flight ─────────────────────────────────────────────────────────────────
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
info "Target  : $GR_USER@$SERVER:$PORT — dir: $INSTALL_DIR"
info "Strategy: $STRATEGY_PATH"
info "Mode    : SIMULATION (no MEXC_FUTURES_API_KEY/SECRET expected on remote)"

# ─── Step 1: rsync ─────────────────────────────────────────────────────────────
if [[ "$VERIFY_ONLY" == "false" ]]; then
    section "STEP 1 — RSYNC"
    if _rsync; then
        ok "Code synced to $GR_USER"
    else
        err "rsync failed"
        exit 1
    fi

    # ─── Step 2: write config.json ─────────────────────────────────────────────
    section "STEP 2 — CONFIG"
    CONFIG_OUT=$(_ssh "
        mkdir -p $INSTALL_DIR
        cat > $INSTALL_DIR/config.json <<'EOJSON'
{
    \"strategy\": \"$STRATEGY_PATH\"
}
EOJSON
        echo 'config.json written'
        cat $INSTALL_DIR/config.json
    ")
    echo "$CONFIG_OUT"
    if echo "$CONFIG_OUT" | grep -q "config.json written"; then
        ok "config.json → strategy=$STRATEGY_PATH"
    else
        err "config.json write failed"
    fi
fi

# ─── Step 3: stop + start (single SSH session) ─────────────────────────────────
if [[ "$SKIP_RESTART" == "false" ]]; then
    section "STEP 3 — RESTART"
    info "Stopping old live_bot and starting grid/mexc_futures bot..."

    RESTART_OUT=$(_ssh "
        echo '${GIT_HASH}' > ${INSTALL_DIR}/version.stamp
        INSTALL=$INSTALL_DIR
        export XDG_RUNTIME_DIR=/run/user/\$(id -u)
        cd \$INSTALL

        echo 'updating dependencies...'
        if [ -d \$INSTALL/.venv ]; then VENV=\$INSTALL/.venv/bin; else VENV=\$INSTALL/venv/bin; fi
        \$VENV/pip install --quiet -r \$INSTALL/requirements.txt 2>/dev/null && echo 'deps ok' || echo 'pip warning (non-fatal)'
        PYVER=\$(\$VENV/python3 -c 'import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")')
        SITE=\$(dirname \$VENV)/lib/python\${PYVER}/site-packages
        mkdir -p \"\$SITE\" && rm -rf \"\$SITE/tradinetools\"
        cp -r \$INSTALL/tradinetools/tradinetools \"\$SITE/tradinetools\" && echo 'tradinetools ok'

        # Kill any live_bot.py process owned by this user (filter via /proc to avoid SSH bash match)
        for P in \$(pgrep -u \"\$(whoami)\" -f 'live_bot' 2>/dev/null || true); do
            if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
                kill \"\$P\" 2>/dev/null && echo \"killed stale live_bot pid=\$P\"
            fi
        done
        sleep 1
        rm -f \"\$INSTALL/live.pid\"
        sleep 1

        PYTHON=\$(ls \$INSTALL/.venv/bin/python3 \$INSTALL/venv/bin/python3 2>/dev/null | head -1)
        nohup \$PYTHON live_bot.py </dev/null >>live.log 2>&1 &
        BOT_PID=\$!
        disown \$BOT_PID
        echo \$BOT_PID > \"\$INSTALL/live.pid\"
        echo \"started grid_mexc pid=\$BOT_PID\"
    ")
    echo "$RESTART_OUT"

    if echo "$RESTART_OUT" | grep -qE 'started grid_mexc pid='; then
        ok "grid/mexc_futures bot started"
    else
        err "Start command did not confirm startup"
    fi

    info "Waiting 8s for startup..."
    sleep 8
fi

# ─── Step 4: verify ────────────────────────────────────────────────────────────
section "STEP 4 — VERIFY"
VERIFY_OUT=$(_ssh "
    export XDG_RUNTIME_DIR=/run/user/\$(id -u)
    echo '=== process ==='
    MPID=\"\"
    for P in \$(pgrep -u \"\$(whoami)\" -f 'live_bot' 2>/dev/null || true); do
        if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
            MPID=\$P; break
        fi
    done
    if [ -n \"\$MPID\" ]; then
        echo \"PID=\$MPID running\"
    else
        echo \"PID=0 NOT running\"
    fi
    echo '=== startup log ==='
    tail -30 $INSTALL_DIR/live.log 2>/dev/null || echo '(no log yet)'
")
echo "$VERIFY_OUT"

if echo "$VERIFY_OUT" | grep -qE '^PID=[1-9][0-9]* running'; then
    ok "$GR_USER: bot is running"
else
    err "$GR_USER: bot is NOT running"
fi

if echo "$VERIFY_OUT" | grep -qi "GridStrategy\|grid.*mexc\|mexc.*grid\|mexc_futures"; then
    ok "Grid/MEXC strategy confirmed in log"
elif echo "$VERIFY_OUT" | grep -qi "SIMULATION\|sim_\|grid"; then
    ok "Simulation mode detected in log"
else
    warn "Grid/MEXC strategy line not yet in log — check manually:"
    warn "  ssh $GR_USER@$SERVER 'tail -50 $INSTALL_DIR/live.log'"
fi

ERROR_COUNT=$(echo "$VERIFY_OUT" | grep -cE '\[ERROR\]|\[CRITICAL\]' || true)
[[ "$ERROR_COUNT" -eq 0 ]] && ok "No errors at startup" \
    || err "$ERROR_COUNT ERROR/CRITICAL line(s) in startup log"

# ─── Report ────────────────────────────────────────────────────────────────────
section "RESULT"
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${BOLD}${GREEN}  SUCCESS — MEXC Futures grid bot running on $GR_USER (SIMULATION)${NC}"
    echo -e "  Log : ssh $GR_USER@$SERVER 'tail -f $INSTALL_DIR/live.log'"
    echo -e "  DB  : ssh $GR_USER@$SERVER 'sqlite3 $INSTALL_DIR/live.db'"
    echo -e "  Stop: ssh $GR_USER@$SERVER 'kill \$(cat $INSTALL_DIR/live.pid)'"
    exit 0
else
    echo -e "${BOLD}${RED}  FAILURE — $FAILURES issue(s) found${NC}"
    exit 1
fi
