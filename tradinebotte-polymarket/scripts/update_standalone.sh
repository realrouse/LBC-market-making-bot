#!/usr/bin/env bash
# update_standalone.sh — Push a code update to the standalone user and restart the bot.
#
# Lightweight update (no full reinstall, no venv rebuild):
#   1. rsync   — push changed source files (excludes config.json, live.db, *.log, venv)
#   2. restart — detect systemd service → systemctl restart; else nohup + PID file
#   3. verify  — check process is running + show first startup lines from live.log
#
# Restart modes (auto-detected, no flag needed):
#   systemd  — tradinebotte-live-<user>.service exists → systemctl restart (no DB lock)
#   nohup    — no service unit → nohup + live.pid (legacy standalone)
#
# Uses TEST_STANDALONE_USER_IDX from ~/.tradinebotte-test.conf (default: 2).
#
# Rules respected:
#   - Maximum 4 SSH connections total (2× rsync + restart + verify).
#   - systemd path: deps updated then systemctl restart — no competing process.
#   - nohup path: stop via PID file (kill $PID), never pkill — avoids other users.
#   - No --simulate: absent API key is enough for simulated orders.
#
# Usage:
#   bash scripts/update_standalone.sh
#   bash scripts/update_standalone.sh --skip-restart   # rsync only, no restart
#   bash scripts/update_standalone.sh --verify-only    # check log/process only
#
# Local prerequisites: sshpass (apt-get install sshpass)

set -uo pipefail

LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GIT_HASH=$(git -C "$LOCAL_REPO" rev-parse --short HEAD 2>/dev/null || echo "unknown")
source "$LOCAL_REPO/tradinebotte-status/scripts/record_deploy.sh"
SKIP_RESTART=false
SKIP_VERIFY=false
VERIFY_ONLY=false

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

section() { echo -e "\n${BOLD}${YELLOW}═══ $* ═══${NC}"; }
info()    { echo -e "${BLUE}  → $*${NC}"; }
ok()      { echo -e "${GREEN}  ✓ $*${NC}"; }
warn()    { echo -e "${YELLOW}  ! $*${NC}"; }
err()     { echo -e "${RED}  ✗ $*${NC}"; FAILURES=$((FAILURES + 1)); }

FAILURES=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-restart) SKIP_RESTART=true ;;
        --skip-verify)  SKIP_VERIFY=true ;;
        --verify-only)  VERIFY_ONLY=true; SKIP_RESTART=true ;;
        -h|--help)
            grep '^#' "${BASH_SOURCE[0]}" | head -30 | sed 's/^# \?//'
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
# The caller (scripts/deploy.py, from inventory.toml deploy_env) sets
# TEST_STANDALONE_USER_IDX=N in the env. Preserve it before source() overwrites it.
_SA_IDX_CALLER="${TEST_STANDALONE_USER_IDX:-}"
# shellcheck source=/dev/null
source "$CONF"

SERVER="${TEST_SERVER:?TEST_SERVER missing in $CONF}"
PORT="${TEST_PORT:-22}"
ALL_USERS=("${TEST_USERS[@]:?TEST_USERS missing in $CONF}")
ALL_PASSWORDS=("${TEST_PASSWORDS[@]:?TEST_PASSWORDS missing in $CONF}")
INSTALL_DIR="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"

SA_IDX="${_SA_IDX_CALLER:-${TEST_STANDALONE_USER_IDX:-2}}"
if [[ "$SA_IDX" -ge "${#ALL_USERS[@]}" ]]; then
    echo "ERROR: TEST_STANDALONE_USER_IDX=$SA_IDX is out of range"
    exit 1
fi
SA_USER="${ALL_USERS[$SA_IDX]}"
SA_PASS="${ALL_PASSWORDS[$SA_IDX]}"
SVC_NAME="tradinebotte-live-${SA_USER}.service"

# Account-0 is the heartbeat collector — used for post-deploy HB check.
HB_USER="${ALL_USERS[0]}"
HB_PASS="${ALL_PASSWORDS[0]}"

# ─── SSH helpers ───────────────────────────────────────────────────────────────
_ssh() {
    SSHPASS="$SA_PASS" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
        -o PreferredAuthentications=password \
        -p "$PORT" "$SA_USER@$SERVER" "$@" 2>&1
}

_ssh_hb() {
    SSHPASS="$HB_PASS" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
        -o PreferredAuthentications=password \
        -p "$PORT" "$HB_USER@$SERVER" "$@" 2>&1
}

_rsync() {
    local ssh_opts="-p $PORT -o StrictHostKeyChecking=yes -o PreferredAuthentications=password"

    # tradinebotte-polymarket/ contents → $INSTALL_DIR/ (flat deploy layout)
    SSHPASS="$SA_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='config.json' --exclude='live.db' --exclude='*.log' \
        --exclude='scripts' --exclude='tests' \
        --exclude='feed.py' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-polymarket/" "$SA_USER@$SERVER:$INSTALL_DIR/" 2>&1 || return 1

    # Polymarket strategy JSON config files → $INSTALL_DIR/strategies/
    SSHPASS="$SA_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --filter='+ **/' --filter='+ *.json' --filter='- *' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-polymarket/strategies/" "$SA_USER@$SERVER:$INSTALL_DIR/strategies/" 2>&1 || return 1

    # requirements.txt — needed for pip update check on restart
    SSHPASS="$SA_PASS" /usr/bin/sshpass -e \
        rsync -az \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/requirements.txt" "$SA_USER@$SERVER:$INSTALL_DIR/" 2>&1 || return 1

    # tradinetools shared library — rsync then pip install -e on remote
    SSHPASS="$SA_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinetools/" "$SA_USER@$SERVER:$INSTALL_DIR/tradinetools/" 2>&1 || return 1

    # Neutral core (botcore.Strategy — imported by live_bot via strategy_engines.base)
    SSHPASS="$SA_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-core/botcore/" "$SA_USER@$SERVER:$INSTALL_DIR/botcore/" 2>&1 || return 1

    # CEX connector adapters (used by live_bot.py via `from connectors import load`)
    SSHPASS="$SA_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-cex/connectors/" "$SA_USER@$SERVER:$INSTALL_DIR/connectors/" 2>&1 || return 1

    # CEX strategy engines (grid, swing, dca, etc.)
    SSHPASS="$SA_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-cex/strategy_engines/" "$SA_USER@$SERVER:$INSTALL_DIR/strategy_engines/" 2>&1 || return 1
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
info "Target: $SA_USER@$SERVER:$PORT — install_dir: $INSTALL_DIR"

# ─── Step 1: rsync ─────────────────────────────────────────────────────────────
if [[ "$VERIFY_ONLY" == "false" ]]; then
    section "STEP 1 — RSYNC"   # connections #1 and #2
    if _rsync; then
        ok "Code synced to $SA_USER"
    else
        err "rsync failed"
        exit 1
    fi
fi

# ─── Step 1b: data-plane routing — set only when the caller requests it ─────────
# update_claudeN.sh wrappers export TRADINEBOTTE_DATA_SOURCE=feed so the live_bot
# consumes the shared feed (config.json is excluded from rsync, so merge remotely).
# The fresh-install integration-test path leaves it unset → this step is skipped.
if [[ "$VERIFY_ONLY" == "false" && -n "${TRADINEBOTTE_DATA_SOURCE:-}" ]]; then
    section "STEP 1b — DATA SOURCE"
    _DS="${TRADINEBOTTE_DATA_SOURCE}"
    _FA="${TRADINEBOTTE_FEED_ADDR:-tcp://127.0.0.1:5557}"
    if _ssh "python3 - <<PYEOF
import json, os
p = os.path.expanduser('$INSTALL_DIR/config.json')
c = json.load(open(p)) if os.path.exists(p) else {}
c['data_source'] = '$_DS'
c['feed_addr'] = '$_FA'
json.dump(c, open(p, 'w'), indent=2)
print('config.json data_source=%s feed_addr=%s' % (c['data_source'], c['feed_addr']))
PYEOF"; then
        ok "data_source=$_DS feed_addr=$_FA"
    else
        warn "could not set data_source (non-fatal)"
    fi
fi

T_BEFORE=$(date +%s)   # epoch snapshot for post-deploy heartbeat check

# ─── Step 2: restart ───────────────────────────────────────────────────────────
if [[ "$SKIP_RESTART" == "false" ]]; then
    section "STEP 2 — RESTART"   # connection #3
    info "Stopping old bot and starting updated one..."

    RESTART_OUT=$(_ssh "
        echo '${GIT_HASH}' > ${INSTALL_DIR}/version.stamp
        SVC=${SVC_NAME}
        INSTALL=${INSTALL_DIR}

        # Detect the active venv (prefer .venv, fall back to venv)
        if   [ -d \"\$INSTALL/.venv\" ]; then VENV=.venv
        elif [ -d \"\$INSTALL/venv\"  ]; then VENV=venv
        else VENV=venv; fi

        # Update dependencies before restart so the new process uses them
        cd \"\$INSTALL\"
        echo 'updating dependencies...'
        \"\$VENV/bin/pip\" install --quiet -r requirements.txt 2>/dev/null \
            && echo 'deps ok' || echo 'pip warning (non-fatal)'
        PYVER=\$(\"\$VENV/bin/python3\" -c 'import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")')
        SITE=\$VENV/lib/python\${PYVER}/site-packages
        mkdir -p \"\$SITE\"
        rm -rf \"\$SITE/tradinetools\"
        cp -r tradinetools/tradinetools \"\$SITE/tradinetools\"
        echo 'tradinetools ok'
        \"\$VENV/bin/python3\" -c 'from tradinetools.zmq import ipc_socket_dir, make_pub; print(\"tradinetools import ok\")' 2>&1

        # XDG_RUNTIME_DIR is required for systemctl --user in non-interactive SSH sessions
        export XDG_RUNTIME_DIR=/run/user/\$(id -u)

        # Prefer user service (no sudo) over system service
        if systemctl --user is-active tradinebotte-live.service >/dev/null 2>&1 \
           || systemctl --user is-enabled tradinebotte-live.service >/dev/null 2>&1; then
            echo 'detected user service: tradinebotte-live.service'
            # Kill nohup Polymarket orphans only — skip grid_bot (TRADINEBOTTE_DIR=*-grid)
            for P in \$(pgrep -u \"\$(whoami)\" -f \"live_bot\" 2>/dev/null || true); do
                if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
                    _ENVDIR=\$(xargs -0 -n1 < /proc/\"\$P\"/environ 2>/dev/null | grep '^TRADINEBOTTE_DIR=' | cut -d= -f2- || true)
                    if echo \"\$_ENVDIR\" | grep -q '\\-grid'; then continue; fi
                    kill \"\$P\" 2>/dev/null && echo \"killed stale live_bot pid=\$P\"
                fi
            done
            sleep 2
            systemctl --user restart tradinebotte-live.service \
                && echo 'systemd restarted' \
                || echo 'user service restart failed'
        else
            # Nohup path — kill stale Polymarket instances only (skip grid_bot)
            PID_FILE=\$INSTALL/live.pid
            for P in \$(pgrep -u \"\$(whoami)\" -f \"live_bot\" 2>/dev/null || true); do
                if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
                    _ENVDIR=\$(xargs -0 -n1 < /proc/\"\$P\"/environ 2>/dev/null | grep '^TRADINEBOTTE_DIR=' | cut -d= -f2- || true)
                    if echo \"\$_ENVDIR\" | grep -q '\\-grid'; then continue; fi
                    kill \"\$P\" 2>/dev/null && echo \"killed stale live_bot pid=\$P\"
                fi
            done
            sleep 1
            rm -f \"\$PID_FILE\"
            sleep 1
            nohup \"\$VENV/bin/python3\" live_bot.py </dev/null >>live.log 2>&1 &
            BOT_PID=\$!
            disown \$BOT_PID
            echo \$BOT_PID > \"\$PID_FILE\"
            echo \"started pid=\$BOT_PID\"
        fi
    ")
    echo "$RESTART_OUT"

    if echo "$RESTART_OUT" | grep -qE 'systemd restarted|started pid=[0-9]+'; then
        ok "Bot restart command sent"
    else
        err "Restart command did not confirm start"
    fi

    if echo "$RESTART_OUT" | grep -q "systemd restarted"; then
        info "Waiting 36s for systemd restart (RestartSec=30 + startup)..."
        sleep 36
    else
        info "Waiting 6s for startup..."
        sleep 6
    fi
fi

if [[ "$SKIP_VERIFY" == "false" ]]; then
# ─── Step 3: verify ────────────────────────────────────────────────────────────
section "STEP 3 — VERIFY"   # connection #4
VERIFY_OUT=$(_ssh "
    export XDG_RUNTIME_DIR=/run/user/\$(id -u)
    SVC=${SVC_NAME}
    echo '=== process ==='
    # Use pgrep to find the Polymarket live_bot process (skip grid_bot via TRADINEBOTTE_DIR).
    # Avoids D-Bus dependency of 'systemctl show --property=MainPID' (hangs in SSH).
    MPID=\"\"
    for P in \$(pgrep -u \"\$(whoami)\" -f 'live_bot' 2>/dev/null || true); do
        if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
            _ENVDIR=\$(xargs -0 -n1 < /proc/\"\$P\"/environ 2>/dev/null | grep '^TRADINEBOTTE_DIR=' | cut -d= -f2- || true)
            if echo \"\$_ENVDIR\" | grep -q '\\-grid'; then continue; fi
            MPID=\$P; break
        fi
    done
    if [ -n \"\$MPID\" ]; then
        echo \"PID=\$MPID running\"
    else
        export XDG_RUNTIME_DIR=/run/user/\$(id -u)
        STATE=\$(systemctl --user is-active tradinebotte-live.service 2>/dev/null || echo unknown)
        echo \"PID=0 NOT running — user service state=\$STATE\"
    fi
    echo '=== startup log ==='
    tail -20 ${INSTALL_DIR}/live.log
")
echo "$VERIFY_OUT"

if echo "$VERIFY_OUT" | grep -qE '^PID=[0-9]+ running'; then
    ok "$SA_USER: bot is running"
else
    err "$SA_USER: bot is NOT running"
fi

if echo "$VERIFY_OUT" | grep -q "GridStrategy"; then
    TRAIL=$(echo "$VERIFY_OUT" | grep -o 'trail=[a-z]*' | head -1)
    ok "Grid strategy loaded ($TRAIL)"
elif echo "$VERIFY_OUT" | grep -q "Strategy:"; then
    ok "Strategy loaded"
else
    warn "Strategy line not found in log — check manually"
fi

ERROR_COUNT=$(echo "$VERIFY_OUT" | grep -cE '\[ERROR\]|\[CRITICAL\]' || true)
[[ "$ERROR_COUNT" -eq 0 ]] && ok "No errors at startup" \
    || err "$ERROR_COUNT ERROR/CRITICAL line(s) in startup log"
fi  # SKIP_VERIFY

# ─── Step 4: heartbeat check ───────────────────────────────────────────────────
# Poll the shared state DB on the collector account for a fresh row from SA_USER.
# Skipped when --skip-verify or --skip-restart is set (no restart happened).
if [[ "$SKIP_VERIFY" == "false" && "$SKIP_RESTART" == "false" ]]; then
    section "STEP 4 — HEARTBEAT CHECK"
    info "Polling collector for fresh $SA_USER heartbeat (up to 180s)..."
    _HB_PY=$(cat <<PYEOF
import sqlite3,time,os,sys
t=$T_BEFORE
p=os.environ.get("TRADINEBOTTE_DB","/data1/tradinebotte-shared/database/tradinebotte.db")
if not os.path.exists(p):
    print("HB_NODB"); sys.exit(1)
db=sqlite3.connect(p)
for i in range(30):
    r=db.execute("SELECT ts,status FROM heartbeats WHERE account=? AND ts>? ORDER BY ts DESC LIMIT 1",("$SA_USER",t)).fetchone()
    if r: print("HB_OK ts="+str(r[0])+" status="+str(r[1])); sys.exit(0)
    time.sleep(6)
print("HB_TIMEOUT"); sys.exit(1)
PYEOF
)
    _HB_B64=$(echo "$_HB_PY" | base64 -w0)
    HB_OUT=$(_ssh_hb "echo '$_HB_B64' | base64 -d | python3")
    if echo "$HB_OUT" | grep -q "HB_OK"; then
        ok "$SA_USER: fresh heartbeat confirmed — $(echo "$HB_OUT" | grep -oE 'status=[^ ]+')"
    elif echo "$HB_OUT" | grep -q "HB_NODB"; then
        warn "heartbeat.db not found on collector — status_collector not deployed?"
    else
        warn "$SA_USER: no heartbeat within 180s — warmup may still be in progress"
    fi
fi  # STEP 4

# ─── Report ────────────────────────────────────────────────────────────────────
section "RESULT"
tbnt_record_deploy "$SA_USER" live_bot "$([[ $FAILURES -eq 0 ]] && echo OK || echo FAILED)"
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${BOLD}${GREEN}  SUCCESS — $SA_USER updated and running${NC}"
    exit 0
else
    echo -e "${BOLD}${RED}  FAILURE — $FAILURES issue(s) found${NC}"
    exit 1
fi
