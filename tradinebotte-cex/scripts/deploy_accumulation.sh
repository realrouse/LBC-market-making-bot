#!/usr/bin/env bash
# deploy_accumulation.sh — Deploy and (re)start the BTC accumulation bot.
#
# The accumulation strategy is now a hosted strategy_engine (strategy_type="accumulation"),
# run via live_bot.py — no more standalone accumulation_bot.py. Mirrors deploy_grid_binance.sh:
# code lives in ~/tradinebotte/ (shared with the Polymarket live_bot), data (config.json,
# live_accum.db, live.log, version.stamp) lives in ~/tradinebotte-accum/ via
# TRADINEBOTTE_DIR, and it runs under tradinebotte-accumulation.service.
#
# Simulation only: no BINANCE_API_KEY/SECRET on the remote → paper trading, Earn inert.
#
# The target account + strategy come from inventory.toml (deploy_env: ACCUM_USER_IDX,
# BOT_STRATEGY) and are injected by scripts/deploy.py — no per-account wrapper.
#
# Usage (via the orchestrator, or with presets directly):
#   bash tradinebotte-cex/scripts/deploy_all.sh --only "account-3 — accumulation"
#   ACCUM_USER_IDX=2 BOT_STRATEGY=strategies/accumulation/btc_accumulation.json \
#       bash scripts/deploy_accumulation.sh [--skip-restart|--verify-only]

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
            grep '^#' "${BASH_SOURCE[0]}" | head -20 | sed 's/^# \?//'; exit 0 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
    shift
done

# ─── Config ────────────────────────────────────────────────────────────────────
CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
if [[ ! -f "$CONF" ]]; then
    echo -e "${RED}Missing configuration: $CONF${NC}"; exit 1
fi
# shellcheck source=/dev/null
source "$CONF"

SERVER="${TEST_SERVER:?TEST_SERVER missing in $CONF}"
PORT="${TEST_PORT:-22}"
ALL_USERS=("${TEST_USERS[@]:?TEST_USERS missing in $CONF}")
ALL_PASSWORDS=("${TEST_PASSWORDS[@]:?TEST_PASSWORDS missing in $CONF}")
INSTALL_DIR="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"
ACCUM_DIR="${INSTALL_DIR}-accum"

SC_IDX="${ACCUM_USER_IDX:-${TEST_STANDALONE_USER_IDX:-2}}"
if [[ "$SC_IDX" -ge "${#ALL_USERS[@]}" ]]; then
    echo "ERROR: ACCUM_USER_IDX=$SC_IDX out of range (${#ALL_USERS[@]} users in $CONF)"; exit 1
fi
SC_USER="${ALL_USERS[$SC_IDX]}"
SC_PASS="${ALL_PASSWORDS[$SC_IDX]}"

STRATEGY_PATH="${BOT_STRATEGY:-strategies/accumulation/btc_accumulation.json}"

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

    # tradinebotte-polymarket/ — live_bot.py, heartbeat, shared code
    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='live.db' --exclude='*.log' --exclude='venv' --exclude='.venv' \
        --exclude='scripts' --exclude='tests' \
        --exclude='account_bot.py' --exclude='feed.py' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-polymarket/" "$SC_USER@$SERVER:$INSTALL_DIR/" 2>&1 || return 1

    # tradinebotte-cex/ — connectors/, strategy_engines/ (incl. accumulation.py), earn_manager.py, api_*
    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='scripts' --exclude='tests' \
        --exclude='orderbook_bot.py' --exclude='scalping_bot.py' --exclude='scalping_math.py' \
        --exclude='api_bitstamp.py' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-cex/" "$SC_USER@$SERVER:$INSTALL_DIR/" 2>&1 || return 1

    # tradinebotte-core/ — botcore (Strategy protocol, persistence, connectors validate)
    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-core/botcore/" "$SC_USER@$SERVER:$INSTALL_DIR/botcore/" 2>&1 || return 1

    # strategy JSON configs (recursive — includes strategies/accumulation/ subdir)
    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --filter='+ **/' --filter='+ *.json' --filter='- *' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-cex/strategies/" "$SC_USER@$SERVER:$INSTALL_DIR/strategies/" 2>&1 || return 1

    # requirements.txt and tradinetools
    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az -e "ssh $ssh_opts" \
        "$LOCAL_REPO/requirements.txt" "$SC_USER@$SERVER:$INSTALL_DIR/" 2>&1 || return 1
    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinetools/" "$SC_USER@$SERVER:$INSTALL_DIR/tradinetools/" 2>&1 || return 1

    # accumulation systemd service template → INSTALL_DIR, installed in restart step
    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-polymarket/scripts/systemd/tradinebotte-accumulation.service" \
        "$SC_USER@$SERVER:$INSTALL_DIR/tradinebotte-accumulation.service" 2>&1 || return 1
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
info "Target  : $SC_USER@$SERVER:$PORT — code: $INSTALL_DIR  data: $ACCUM_DIR"
info "Strategy: $STRATEGY_PATH"
info "Mode    : SIMULATION (paper trading; Earn inert without credentials)"

# ─── Step 1: rsync ─────────────────────────────────────────────────────────────
if [[ "$VERIFY_ONLY" == "false" ]]; then
    section "STEP 1 — RSYNC"
    if _rsync; then ok "Code synced"; else err "rsync failed"; exit 1; fi

    # ─── Step 2: write config.json to ACCUM_DIR ───────────────────────────────
    section "STEP 2 — CONFIG"
    CONFIG_OUT=$(_ssh "
        mkdir -p $ACCUM_DIR
        cat > $ACCUM_DIR/config.json << 'EOJSON'
{
    \"strategy\": \"$INSTALL_DIR/$STRATEGY_PATH\",
    \"data_source\": \"indicators\"
}
EOJSON
        echo 'config.json written'
        cat $ACCUM_DIR/config.json
    ")
    echo "$CONFIG_OUT"
    if echo "$CONFIG_OUT" | grep -q "config.json written"; then
        ok "config.json → $ACCUM_DIR/config.json  strategy=$STRATEGY_PATH"
    else
        err "config.json write failed"
    fi
fi

# ─── Step 3: restart ───────────────────────────────────────────────────────────
if [[ "$SKIP_RESTART" == "false" ]]; then
    section "STEP 3 — RESTART"

    RESTART_OUT=$(_ssh "
        echo '${GIT_HASH}' > ${ACCUM_DIR}/version.stamp
        INSTALL=${INSTALL_DIR}
        ACCUM=${ACCUM_DIR}
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

        # Stop the accumulation service (idempotent — OK if not yet installed).
        systemctl --user stop tradinebotte-accumulation.service 2>/dev/null || true
        sleep 1

        # Kill any stale process from the FORMER standalone accumulation_bot.py, and any
        # nohup live_bot orphan whose data dir is -accum (never the Polymarket live_bot,
        # which uses TRADINEBOTTE_DIR=~/tradinebotte).
        for P in \$(pgrep -u \"\$(whoami)\" -f 'accumulation_bot.py' 2>/dev/null || true); do
            if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
                kill \"\$P\" 2>/dev/null && echo \"killed former standalone accumulation_bot pid=\$P\"
            fi
        done
        for P in \$(pgrep -u \"\$(whoami)\" -f 'live_bot' 2>/dev/null || true); do
            if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
                _ENVDIR=\$(xargs -0 -n1 < /proc/\"\$P\"/environ 2>/dev/null | grep '^TRADINEBOTTE_DIR=' | cut -d= -f2- || true)
                if echo \"\$_ENVDIR\" | grep -q '\\-accum'; then
                    kill \"\$P\" 2>/dev/null && echo \"killed stale nohup accumulation live_bot pid=\$P\"
                fi
            fi
        done
        sleep 1

        # One-time DB migration: the former standalone kept live_accum.db in ~/tradinebotte;
        # the engine runs with TRADINEBOTTE_DIR=~/tradinebotte-accum. Move the DB (+WAL/SHM,
        # since it is WAL mode) so deployed holdings/avg_entry/realized survive. Service is
        # stopped above (handle released). Idempotent: only when src exists AND dst absent —
        # never clobber an already-migrated DB, so re-deploys are safe.
        mkdir -p \"\$ACCUM\"
        SRC=\$INSTALL/live_accum.db
        DST=\$ACCUM/live_accum.db
        if [ -f \"\$SRC\" ] && [ ! -f \"\$DST\" ]; then
            for EXT in '' '-wal' '-shm'; do
                if [ -f \"\$SRC\$EXT\" ]; then mv \"\$SRC\$EXT\" \"\$DST\$EXT\" && echo \"migrated live_accum.db\$EXT → \$ACCUM\"; fi
            done
        elif [ -f \"\$SRC\" ] && [ -f \"\$DST\" ]; then
            echo 'live_accum.db already migrated (dst exists) — leaving both in place'
        fi

        # Always refresh service file (picks up any Environment= or ExecStart= changes) —
        # this is what transitions an existing unit off the retired accumulation_bot.py.
        mkdir -p ~/.config/systemd/user
        cp \"\$INSTALL/tradinebotte-accumulation.service\" ~/.config/systemd/user/tradinebotte-accumulation.service
        systemctl --user daemon-reload
        systemctl --user enable tradinebotte-accumulation.service
        echo 'service installed/refreshed'

        systemctl --user start tradinebotte-accumulation.service \
            && echo 'systemd started' || echo 'user service start failed'
    ")
    echo "$RESTART_OUT"

    if echo "$RESTART_OUT" | grep -q 'systemd started'; then
        ok "Accumulation bot restart confirmed"
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
            if echo \"\$_ENVDIR\" | grep -q '\\-accum'; then MPID=\$P; break; fi
        fi
    done
    if [ -n \"\$MPID\" ]; then echo \"PID=\$MPID running\"
    else
        STATE=\$(systemctl --user is-active tradinebotte-accumulation.service 2>/dev/null || echo unknown)
        echo \"PID=0 NOT running — service state=\$STATE\"
    fi
    echo '=== startup log ==='
    tail -30 ${ACCUM_DIR}/live.log 2>/dev/null || echo '(no log yet)'
")
echo "$VERIFY_OUT"

if echo "$VERIFY_OUT" | grep -qE '^PID=[1-9][0-9]* running'; then ok "Accumulation bot is running"
else err "Accumulation bot is NOT running"; fi
if echo "$VERIFY_OUT" | grep -qi "AccumulationStrategy\|accumulation"; then ok "Accumulation strategy detected in log"
else warn "Strategy not yet in log — check: ssh $SC_USER@$SERVER 'tail -50 ${ACCUM_DIR}/live.log'"; fi
ERROR_COUNT=$(echo "$VERIFY_OUT" | grep -cE '\[ERROR\]|\[CRITICAL\]' || true)
[[ "$ERROR_COUNT" -eq 0 ]] && ok "No errors at startup" \
    || err "$ERROR_COUNT ERROR/CRITICAL line(s) in startup log"

# ─── Report ────────────────────────────────────────────────────────────────────
section "RESULT"
tbnt_record_deploy "$SC_USER" accumulation_bot "$([[ $FAILURES -eq 0 ]] && echo OK || echo FAILED)"
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${BOLD}${GREEN}  SUCCESS — accumulation bot running on $SC_USER (SIMULATION)${NC}"
    echo -e "  Log : ssh $SC_USER@$SERVER 'tail -f ${ACCUM_DIR}/live.log'"
    echo -e "  DB  : ssh $SC_USER@$SERVER 'sqlite3 ${ACCUM_DIR}/live_accum.db'"
    echo -e "  Svc : ssh $SC_USER@$SERVER 'export XDG_RUNTIME_DIR=/run/user/\$(id -u); systemctl --user status tradinebotte-accumulation.service'"
    exit 0
else
    echo -e "${BOLD}${RED}  FAILURE — $FAILURES issue(s) found${NC}"; exit 1
fi
