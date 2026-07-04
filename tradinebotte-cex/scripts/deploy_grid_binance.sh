#!/usr/bin/env bash
# deploy_grid_binance.sh — Generic Binance CEX grid-bot deployer (simulation mode).
#
# The target account + strategy come from inventory.toml (deploy_env: TEST_GRID_BINANCE_USER_IDX,
# TEST_GRID_BINANCE_STRATEGY) and are injected by scripts/deploy.py — no per-account wrapper.
#
# Runs live_bot.py via tradinebotte-grid.service with TRADINEBOTTE_DIR=~/tradinebotte-grid.
# Data (config.json, live.db, live.log, version.stamp) lives in ~/tradinebotte-grid/.
# Code lives in ~/tradinebotte/ — same install as the Polymarket live_bot.
#
# Simulation mode is automatic: no BINANCE_API_KEY/SECRET on the remote →
# all orders return "sim_..." IDs (no real trades, no real API calls).
#
# Fallback defaults (used only when the env vars are unset): TEST_GRID_BINANCE_USER_IDX=2,
# strategy grid_BTCUSDT_moderate.json (backtested, BTC bounds 49k–73.5k, capital_start=1500).
#
# Usage (via the orchestrator, or with presets directly):
#   bash tradinebotte-cex/scripts/deploy_all.sh --only "account-3 — grid"  # [--skip-restart|--verify-only]
#   TEST_GRID_BINANCE_USER_IDX=2 TEST_GRID_BINANCE_STRATEGY=strategies/grid/grid_BTCUSDT_moderate.json \
#       bash scripts/deploy_grid_binance.sh

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
            grep '^#' "${BASH_SOURCE[0]}" | head -24 | sed 's/^# \?//'; exit 0 ;;
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
GRID_DIR="${INSTALL_DIR}-grid"

GR_IDX="${TEST_GRID_BINANCE_USER_IDX:-2}"
if [[ "$GR_IDX" -ge "${#ALL_USERS[@]}" ]]; then
    echo "ERROR: TEST_GRID_BINANCE_USER_IDX=$GR_IDX out of range (${#ALL_USERS[@]} users in $CONF)"
    echo "  Add the account credentials to $CONF or set TEST_GRID_BINANCE_USER_IDX"
    exit 1
fi
GR_USER="${ALL_USERS[$GR_IDX]}"
GR_PASS="${ALL_PASSWORDS[$GR_IDX]}"

STRATEGY_PATH="${TEST_GRID_BINANCE_STRATEGY:-strategies/grid/grid_BTCUSDT_moderate.json}"

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

    # tradinebotte-cex/ — connectors/, strategy_engines/, api files
    SSHPASS="$GR_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='scripts' --exclude='tests' \
        --exclude='accumulation_bot.py' --exclude='orderbook_bot.py' \
        --exclude='earn_manager.py' \
        --exclude='api_bitstamp.py' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-cex/" "$GR_USER@$SERVER:$INSTALL_DIR/" 2>&1 || return 1

    # tradinebotte-core/ — botcore (neutral Strategy protocol, imported via strategy_engines.base)
    SSHPASS="$GR_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-core/botcore/" "$GR_USER@$SERVER:$INSTALL_DIR/botcore/" 2>&1 || return 1

    # Grid strategy JSON configs (recursive — includes strategies/grid/ subdir)
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

    # Grid systemd service template → deployed into INSTALL_DIR, installed in restart step
    SSHPASS="$GR_PASS" /usr/bin/sshpass -e \
        rsync -az -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-polymarket/scripts/systemd/tradinebotte-grid.service" \
        "$GR_USER@$SERVER:$INSTALL_DIR/tradinebotte-grid.service" 2>&1 || return 1
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
info "Target  : $GR_USER@$SERVER:$PORT — code: $INSTALL_DIR  data: $GRID_DIR"
info "Strategy: $STRATEGY_PATH"
info "Mode    : SIMULATION (no BINANCE_API_KEY/SECRET expected on remote)"

# ─── Step 1: rsync ─────────────────────────────────────────────────────────────
if [[ "$VERIFY_ONLY" == "false" ]]; then
    section "STEP 1 — RSYNC"
    if _rsync; then
        ok "Code synced"
    else
        err "rsync failed"; exit 1
    fi

    # ─── Step 2: write config.json to GRID_DIR ────────────────────────────────
    section "STEP 2 — CONFIG"
    CONFIG_OUT=$(_ssh "
        mkdir -p $GRID_DIR
        cat > $GRID_DIR/config.json << 'EOJSON'
{
    \"strategy\": \"$INSTALL_DIR/$STRATEGY_PATH\",
    \"data_source\": \"cex_feed\",
    \"feed_addr\": \"tcp://127.0.0.1:5563\"
}
EOJSON
        echo 'config.json written'
        cat $GRID_DIR/config.json
    ")
    echo "$CONFIG_OUT"
    if echo "$CONFIG_OUT" | grep -q "config.json written"; then
        ok "config.json → $GRID_DIR/config.json  strategy=$STRATEGY_PATH"
    else
        err "config.json write failed"
    fi
fi

# ─── Step 3: restart ───────────────────────────────────────────────────────────
if [[ "$SKIP_RESTART" == "false" ]]; then
    section "STEP 3 — RESTART"

    RESTART_OUT=$(_ssh "
        echo '${GIT_HASH}' > ${GRID_DIR}/version.stamp
        INSTALL=${INSTALL_DIR}
        GRID=${GRID_DIR}
        export XDG_RUNTIME_DIR=/run/user/\$(id -u)
        cd \"\$INSTALL\"

        if   [ -d \"\$INSTALL/.venv\" ]; then VENV=.venv
        elif [ -d \"\$INSTALL/venv\"  ]; then VENV=venv
        else VENV=.venv; fi

        echo 'updating dependencies...'
        \"\$VENV/bin/pip\" install --quiet -r requirements.txt 2>/dev/null \
            && echo 'deps ok' || echo 'pip warning (non-fatal)'
        PYVER=\$(\"\$VENV/bin/python3\" -c 'import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")')
        SITE=\$VENV/lib/python\${PYVER}/site-packages
        mkdir -p \"\$SITE\" && rm -rf \"\$SITE/tradinetools\"
        cp -r tradinetools/tradinetools \"\$SITE/tradinetools\" && echo 'tradinetools ok'

        # Stop grid service (idempotent — OK if not yet started)
        systemctl --user stop tradinebotte-grid.service 2>/dev/null || true
        sleep 1

        # Kill any stale nohup grid_bot orphan (pre-systemd process with TRADINEBOTTE_DIR=-grid)
        for P in \$(pgrep -u \"\$(whoami)\" -f 'live_bot' 2>/dev/null || true); do
            if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
                _ENVDIR=\$(xargs -0 -n1 < /proc/\"\$P\"/environ 2>/dev/null | grep '^TRADINEBOTTE_DIR=' | cut -d= -f2- || true)
                if echo \"\$_ENVDIR\" | grep -q '\\-grid'; then
                    kill \"\$P\" 2>/dev/null && echo \"killed stale nohup grid_bot pid=\$P\"
                fi
            fi
        done
        sleep 1

        # Always refresh service file (picks up any Environment= or ExecStart= changes)
        mkdir -p ~/.config/systemd/user
        cp \"\$INSTALL/tradinebotte-grid.service\" ~/.config/systemd/user/tradinebotte-grid.service
        systemctl --user daemon-reload
        systemctl --user enable tradinebotte-grid.service
        echo 'service installed/refreshed'

        systemctl --user start tradinebotte-grid.service \
            && echo 'systemd started' || echo 'user service start failed'
    ")
    echo "$RESTART_OUT"

    if echo "$RESTART_OUT" | grep -q 'systemd started'; then
        ok "Grid bot restart confirmed"
    else
        err "Restart did not confirm start"
    fi

    info "Waiting 36s for systemd startup (RestartSec=30 + startup)..."
    sleep 36
fi

# ─── Step 4: verify ────────────────────────────────────────────────────────────
section "STEP 4 — VERIFY"
VERIFY_OUT=$(_ssh "
    export XDG_RUNTIME_DIR=/run/user/\$(id -u)
    echo '=== process ==='
    MPID=\"\"
    for P in \$(pgrep -u \"\$(whoami)\" -f 'live_bot' 2>/dev/null || true); do
        if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
            _ENVDIR=\$(xargs -0 -n1 < /proc/\"\$P\"/environ 2>/dev/null | grep '^TRADINEBOTTE_DIR=' | cut -d= -f2- || true)
            if echo \"\$_ENVDIR\" | grep -q '\\-grid'; then
                MPID=\$P; break
            fi
        fi
    done
    if [ -n \"\$MPID\" ]; then echo \"PID=\$MPID running\"
    else
        STATE=\$(systemctl --user is-active tradinebotte-grid.service 2>/dev/null || echo unknown)
        echo \"PID=0 NOT running — service state=\$STATE\"
    fi
    echo '=== startup log ==='
    tail -30 ${GRID_DIR}/live.log 2>/dev/null || echo '(no log yet)'
")
echo "$VERIFY_OUT"

if echo "$VERIFY_OUT" | grep -qE '^PID=[1-9][0-9]* running'; then
    ok "Grid bot is running"
else
    err "Grid bot is NOT running"
fi

if echo "$VERIFY_OUT" | grep -qi "GridStrategy\|binance\|grid"; then
    ok "Grid strategy detected in log"
else
    warn "Strategy not yet in log — check: ssh $GR_USER@$SERVER 'tail -50 ${GRID_DIR}/live.log'"
fi

ERROR_COUNT=$(echo "$VERIFY_OUT" | grep -cE '\[ERROR\]|\[CRITICAL\]' || true)
[[ "$ERROR_COUNT" -eq 0 ]] && ok "No errors at startup" \
    || err "$ERROR_COUNT ERROR/CRITICAL line(s) in startup log"

# ─── Report ────────────────────────────────────────────────────────────────────
section "RESULT"
tbnt_record_deploy "$GR_USER" grid_bot "$([[ $FAILURES -eq 0 ]] && echo OK || echo FAILED)"
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${BOLD}${GREEN}  SUCCESS — Binance grid bot running on $GR_USER (SIMULATION)${NC}"
    echo -e "  Log : ssh $GR_USER@$SERVER 'tail -f ${GRID_DIR}/live.log'"
    echo -e "  DB  : ssh $GR_USER@$SERVER 'sqlite3 ${GRID_DIR}/live.db'"
    echo -e "  Svc : ssh $GR_USER@$SERVER 'export XDG_RUNTIME_DIR=/run/user/\$(id -u); systemctl --user status tradinebotte-grid.service'"
    exit 0
else
    echo -e "${BOLD}${RED}  FAILURE — $FAILURES issue(s) found${NC}"; exit 1
fi
