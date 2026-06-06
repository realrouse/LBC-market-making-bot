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
SKIP_RESTART=false
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
# shellcheck source=/dev/null
source "$CONF"

SERVER="${TEST_SERVER:?TEST_SERVER missing in $CONF}"
PORT="${TEST_PORT:-22}"
ALL_USERS=("${TEST_USERS[@]:?TEST_USERS missing in $CONF}")
ALL_PASSWORDS=("${TEST_PASSWORDS[@]:?TEST_PASSWORDS missing in $CONF}")
INSTALL_DIR="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"

SA_IDX="${TEST_STANDALONE_USER_IDX:-2}"
if [[ "$SA_IDX" -ge "${#ALL_USERS[@]}" ]]; then
    echo "ERROR: TEST_STANDALONE_USER_IDX=$SA_IDX is out of range"
    exit 1
fi
SA_USER="${ALL_USERS[$SA_IDX]}"
SA_PASS="${ALL_PASSWORDS[$SA_IDX]}"
SVC_NAME="tradinebotte-live-${SA_USER}.service"

# ─── SSH helpers ───────────────────────────────────────────────────────────────
_ssh() {
    SSHPASS="$SA_PASS" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
        -o PreferredAuthentications=password \
        -p "$PORT" "$SA_USER@$SERVER" "$@" 2>&1
}

_rsync() {
    local ssh_opts="-p $PORT -o StrictHostKeyChecking=yes -o PreferredAuthentications=password"

    # tradinebotte-polymarket/ contents → $INSTALL_DIR/ (flat deploy layout)
    SSHPASS="$SA_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='config.json' --exclude='live.db' --exclude='*.log' \
        --exclude='scripts' --exclude='tests' \
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

# ─── Step 2: restart ───────────────────────────────────────────────────────────
if [[ "$SKIP_RESTART" == "false" ]]; then
    section "STEP 2 — RESTART"   # connection #3
    info "Stopping old bot and starting updated one..."

    RESTART_OUT=$(_ssh "
        SVC=${SVC_NAME}
        INSTALL=${INSTALL_DIR}
        PASS='${SA_PASS}'

        # Detect the active venv (prefer .venv, fall back to venv)
        if   [ -d \"\$INSTALL/.venv\" ]; then VENV=.venv
        elif [ -d \"\$INSTALL/venv\"  ]; then VENV=venv
        else VENV=venv; fi

        # Update dependencies before restart so the new process uses them
        cd \"\$INSTALL\"
        echo 'updating dependencies...'
        \"\$VENV/bin/pip\" install --quiet -r requirements.txt 2>/dev/null \
            && echo 'deps ok' || echo 'pip warning (non-fatal)'
        \"\$VENV/bin/pip\" install --quiet -e tradinetools 2>/dev/null \
            && echo 'tradinetools ok' || echo 'tradinetools warning (non-fatal)'

        # XDG_RUNTIME_DIR is required for systemctl --user in non-interactive SSH sessions
        export XDG_RUNTIME_DIR=/run/user/\$(id -u)

        # Prefer user service (no sudo) over system service
        if systemctl --user is-active tradinebotte-live.service >/dev/null 2>&1 \
           || systemctl --user is-enabled tradinebotte-live.service >/dev/null 2>&1; then
            echo 'detected user service: tradinebotte-live.service'
            # Kill any nohup orphans before restarting so no stale process holds the DB
            for P in \$(pgrep -u \"\$(whoami)\" -f \"live_bot\" 2>/dev/null || true); do
                if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
                    kill \"\$P\" 2>/dev/null && echo \"killed stale live_bot pid=\$P\"
                fi
            done
            sleep 2
            systemctl --user restart tradinebotte-live.service \
                && echo 'systemd restarted' \
                || echo 'user service restart failed'
        elif [ -f \"/etc/systemd/system/\$SVC\" ]; then
            echo \"detected system service: \$SVC (consider migrating to user service)\"
            PIDS=\$(pgrep -u ${SA_USER} -f 'python3.*live_bot' 2>/dev/null || true)
            if [ -n \"\$PIDS\" ]; then
                for PID in \$PIDS; do
                    kill \"\$PID\" 2>/dev/null && echo \"killed pid=\$PID\"
                done
            fi
            echo 'systemd restarted'
        else
            # Nohup path — kill ALL stale instances (PID file + orphans from prior sessions)
            PID_FILE=\$INSTALL/live.pid
            for P in \$(pgrep -u \"\$(whoami)\" -f \"live_bot\" 2>/dev/null || true); do
                if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
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

# ─── Step 3: verify ────────────────────────────────────────────────────────────
section "STEP 3 — VERIFY"   # connection #4
VERIFY_OUT=$(_ssh "
    export XDG_RUNTIME_DIR=/run/user/\$(id -u)
    SVC=${SVC_NAME}
    echo '=== process ==='
    # Use pgrep to find the live_bot process — avoids D-Bus dependency of
    # 'systemctl show --property=MainPID' which hangs in non-interactive SSH.
    MPID=\$(pgrep -u \"\$(whoami)\" -f 'python.*live_bot' 2>/dev/null | head -1)
    if [ -n \"\$MPID\" ]; then
        echo \"PID=\$MPID running\"
    else
        # Fallback: check user service state without querying MainPID via D-Bus
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

# ─── Report ────────────────────────────────────────────────────────────────────
section "RESULT"
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${BOLD}${GREEN}  SUCCESS — $SA_USER updated and running${NC}"
    exit 0
else
    echo -e "${BOLD}${RED}  FAILURE — $FAILURES issue(s) found${NC}"
    exit 1
fi
