#!/usr/bin/env bash
# deploy_grid_mexc.sh — Deploy the MEXC Futures grid bot (simulation mode).
#
# Runs live_bot.py with strategy_type=grid / connector=mexc_futures.
# Simulation mode is automatic: no MEXC_FUTURES_API_KEY/SECRET on the remote →
# all orders return "sim_..." IDs (no real trades, no real API calls).
#
# Default target: TEST_GRID_MEXC_USER_IDX=5 (test account, index 5 in TEST_USERS).
# Override: TEST_GRID_MEXC_USER_IDX=<n> bash scripts/deploy_grid_mexc.sh
#
# Restart priority: systemd tradinebotte-live.service (installed on first run)
#                   → nohup + PID file fallback if service install fails.
#
# Usage:
#   bash tradinebotte-cex/scripts/deploy_grid_mexc.sh
#   bash tradinebotte-cex/scripts/deploy_grid_mexc.sh --skip-restart
#   bash tradinebotte-cex/scripts/deploy_grid_mexc.sh --verify-only

set -uo pipefail

LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GIT_HASH=$(git -C "$LOCAL_REPO" rev-parse --short HEAD 2>/dev/null || echo "unknown")
source "$LOCAL_REPO/tradinebotte-status/scripts/record_deploy.sh"
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
            grep '^#' "${BASH_SOURCE[0]}" | head -22 | sed 's/^# \?//'; exit 0 ;;
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
    echo "ERROR: TEST_GRID_MEXC_USER_IDX=$GR_IDX out of range (${#ALL_USERS[@]} users in $CONF)"
    echo "  Add the test account credentials to $CONF or set TEST_GRID_MEXC_USER_IDX"
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

    # tradinebotte-polymarket/ — live_bot.py, heartbeat, shared code
    SSHPASS="$GR_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='live.db' --exclude='*.log' --exclude='venv' --exclude='.venv' \
        --exclude='scripts' --exclude='tests' \
        --exclude='account_bot.py' --exclude='feed.py' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-polymarket/" "$GR_USER@$SERVER:$INSTALL_DIR/" 2>&1 || return 1

    # tradinebotte-cex/ — api_mexc_futures.py, api_common.py, connectors/, strategy_engines/
    # api_bitstamp.py excluded (unused); accumulation_bot/orderbook_bot excluded (not needed here)
    SSHPASS="$GR_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='scripts' --exclude='tests' \
        --exclude='accumulation_bot.py' --exclude='orderbook_bot.py' \
        --exclude='earn_manager.py' \
        --exclude='api_bitstamp.py' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-cex/" "$GR_USER@$SERVER:$INSTALL_DIR/" 2>&1 || return 1

    # Grid strategy JSON configs
    SSHPASS="$GR_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --filter='+ **/' --filter='+ *.json' --filter='- *' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-cex/strategies/" "$GR_USER@$SERVER:$INSTALL_DIR/strategies/" 2>&1 || return 1

    # requirements.txt and tradinetools
    SSHPASS="$GR_PASS" /usr/bin/sshpass -e \
        rsync -az -e "ssh $ssh_opts" \
        "$LOCAL_REPO/requirements.txt" "$GR_USER@$SERVER:$INSTALL_DIR/" 2>&1 || return 1
    SSHPASS="$GR_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinetools/" "$GR_USER@$SERVER:$INSTALL_DIR/tradinetools/" 2>&1 || return 1

    # systemd service template — deployed into INSTALL_DIR, installed in restart step
    SSHPASS="$GR_PASS" /usr/bin/sshpass -e \
        rsync -az -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-polymarket/scripts/systemd/tradinebotte-live.service" \
        "$GR_USER@$SERVER:$INSTALL_DIR/tradinebotte-live.service" 2>&1 || return 1
}

# ─── Pre-flight ─────────────────────────────────────────────────────────────────
section "PRE-FLIGHT"

if [[ ! -x "$(command -v sshpass)" ]]; then
    err "sshpass not found — install with: apt-get install sshpass"; exit 1
fi
ok "sshpass OK"

mkdir -p ~/.ssh && chmod 700 ~/.ssh
if ! ssh-keygen -F "[$SERVER]:$PORT" &>/dev/null && \
   ! ssh-keygen -F "$SERVER"         &>/dev/null; then
    ssh-keyscan -p "$PORT" -H "$SERVER" >> ~/.ssh/known_hosts 2>/dev/null
fi
info "Target  : $GR_USER@$SERVER:$PORT — dir: $INSTALL_DIR"
info "Strategy: $STRATEGY_PATH"
info "Mode    : SIMULATION (no MEXC_FUTURES_API_KEY/SECRET expected on remote)"

# ─── Step 1: rsync ─────────────────────────────────────────────────────────────
if [[ "$VERIFY_ONLY" == "false" ]]; then
    section "STEP 1 — RSYNC"
    if _rsync; then
        ok "Code synced"
    else
        err "rsync failed"; exit 1
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

# ─── Step 3: restart ───────────────────────────────────────────────────────────
if [[ "$SKIP_RESTART" == "false" ]]; then
    section "STEP 3 — RESTART"

    RESTART_OUT=$(_ssh "
        echo '${GIT_HASH}' > ${INSTALL_DIR}/version.stamp
        INSTALL=${INSTALL_DIR}
        export XDG_RUNTIME_DIR=/run/user/\$(id -u)
        cd \"\$INSTALL\"

        if   [ -d \"\$INSTALL/.venv\" ]; then VENV=.venv
        elif [ -d \"\$INSTALL/venv\"  ]; then VENV=venv
        else VENV=.venv; fi

        echo 'updating dependencies...'
        \"\$VENV/bin/pip\" install --quiet -r requirements.txt 2>/dev/null \\
            && echo 'deps ok' || echo 'pip warning (non-fatal)'
        PYVER=\$(\"\$VENV/bin/python3\" -c 'import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")')
        SITE=\$VENV/lib/python\${PYVER}/site-packages
        mkdir -p \"\$SITE\" && rm -rf \"\$SITE/tradinetools\"
        cp -r tradinetools/tradinetools \"\$SITE/tradinetools\" && echo 'tradinetools ok'

        # Install systemd service if not already present
        if ! systemctl --user is-enabled tradinebotte-live.service >/dev/null 2>&1; then
            mkdir -p ~/.config/systemd/user
            cp \"\$INSTALL/tradinebotte-live.service\" ~/.config/systemd/user/tradinebotte-live.service
            systemctl --user daemon-reload
            systemctl --user enable tradinebotte-live.service
            echo 'service installed and enabled'
        else
            echo 'service already configured'
        fi

        # Kill stale nohup live_bot processes before systemd restart (filter via /proc)
        for P in \$(pgrep -u \"\$(whoami)\" -f 'live_bot' 2>/dev/null || true); do
            if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
                kill \"\$P\" 2>/dev/null && echo \"killed stale live_bot pid=\$P\"
            fi
        done
        sleep 2

        # Prefer systemd restart; fall back to nohup if service activation fails
        if systemctl --user is-active tradinebotte-live.service >/dev/null 2>&1 \\
           || systemctl --user is-enabled tradinebotte-live.service >/dev/null 2>&1; then
            systemctl --user restart tradinebotte-live.service \\
                && echo 'systemd restarted' || echo 'user service restart failed'
        else
            rm -f \"\$INSTALL/live.pid\"
            sleep 1
            nohup \"\$VENV/bin/python3\" live_bot.py </dev/null >>live.log 2>&1 &
            BOT_PID=\$!
            disown \$BOT_PID
            echo \$BOT_PID > \"\$INSTALL/live.pid\"
            echo \"started pid=\$BOT_PID\"
        fi
    ")
    echo "$RESTART_OUT"

    if echo "$RESTART_OUT" | grep -qE 'systemd restarted|started pid=[0-9]+'; then
        ok "Bot restart confirmed"
    else
        err "Restart did not confirm start"
    fi

    if echo "$RESTART_OUT" | grep -q "systemd restarted"; then
        info "Waiting 36s for systemd restart (RestartSec=30 + startup)..."
        sleep 36
    else
        info "Waiting 8s for startup..."
        sleep 8
    fi
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
    if [ -n \"\$MPID\" ]; then echo \"PID=\$MPID running\"
    else
        STATE=\$(systemctl --user is-active tradinebotte-live.service 2>/dev/null || echo unknown)
        echo \"PID=0 NOT running — service state=\$STATE\"
    fi
    echo '=== startup log ==='
    tail -30 $INSTALL_DIR/live.log 2>/dev/null || echo '(no log yet)'
")
echo "$VERIFY_OUT"

if echo "$VERIFY_OUT" | grep -qE '^PID=[1-9][0-9]* running'; then
    ok "Bot is running"
else
    err "Bot is NOT running"
fi

if echo "$VERIFY_OUT" | grep -qi "GridStrategy\|mexc_futures"; then
    ok "GridStrategy / mexc_futures confirmed in log"
elif echo "$VERIFY_OUT" | grep -qi "SIMULATION\|grid"; then
    ok "Grid/simulation detected in log"
else
    warn "Strategy not yet in log — check: ssh $GR_USER@$SERVER 'tail -50 $INSTALL_DIR/live.log'"
fi

ERROR_COUNT=$(echo "$VERIFY_OUT" | grep -cE '\[ERROR\]|\[CRITICAL\]' || true)
[[ "$ERROR_COUNT" -eq 0 ]] && ok "No errors at startup" \
    || err "$ERROR_COUNT ERROR/CRITICAL line(s) in startup log"

# ─── Report ────────────────────────────────────────────────────────────────────
section "RESULT"
tbnt_record_deploy "$GR_USER" grid_bot "$([[ $FAILURES -eq 0 ]] && echo OK || echo FAILED)"
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${BOLD}${GREEN}  SUCCESS — MEXC Futures grid bot running on $GR_USER (SIMULATION)${NC}"
    echo -e "  Log : ssh $GR_USER@$SERVER 'tail -f $INSTALL_DIR/live.log'"
    echo -e "  DB  : ssh $GR_USER@$SERVER 'sqlite3 $INSTALL_DIR/live.db'"
    echo -e "  Svc : ssh $GR_USER@$SERVER 'export XDG_RUNTIME_DIR=/run/user/\$(id -u); systemctl --user status tradinebotte-live.service'"
    exit 0
else
    echo -e "${BOLD}${RED}  FAILURE — $FAILURES issue(s) found${NC}"; exit 1
fi
