#!/usr/bin/env bash
# deploy_status_service.sh — Deploy the tradinebotte status collector to the collector account.
#
# Steps:
#   1. rsync    — push status_collector.py, heartbeat_query.py, scripts/, tradinetools/
#   2. install  — pip-install tradinetools into the remote venv, then run install_status_service.sh
#   3. start    — systemctl --user start (or restart) tradinebotte-status.service
#   4. verify   — check service active + show recent journal entries
#
# Uses TEST_USERS[0] as the collector account by default (--collector-idx to override).
# The remote install directory is TEST_REMOTE_INSTALL_DIR (default ~/tradinebotte).
#
# Rules respected:
#   - Sequential: single account, max 4 SSH connections.
#   - Never sudo — systemctl --user only.
#   - Writes version.stamp after the rsync step (before restart).
#
# Usage:
#   bash tradinebotte-status/scripts/deploy_status_service.sh
#   bash tradinebotte-status/scripts/deploy_status_service.sh --skip-restart
#   bash tradinebotte-status/scripts/deploy_status_service.sh --verify-only
#   bash tradinebotte-status/scripts/deploy_status_service.sh --collector-idx 0
#
# Optional env vars:
#   TEST_MULTIBOT_CONF   Credentials file path  (default ~/.tradinebotte-test.conf)
#
# Local prerequisites: sshpass, rsync  (apt-get install sshpass rsync)

set -uo pipefail

LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GIT_HASH=$(git -C "$LOCAL_REPO" rev-parse --short HEAD 2>/dev/null || echo "unknown")
source "$LOCAL_REPO/tradinebotte-status/scripts/record_deploy.sh"

COLLECTOR_IDX=0
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
        --collector-idx) COLLECTOR_IDX="$2"; shift ;;
        --skip-restart)  SKIP_RESTART=true ;;
        --verify-only)   VERIFY_ONLY=true; SKIP_RESTART=true ;;
        -h|--help)
            grep '^#' "${BASH_SOURCE[0]}" | head -30 | sed 's/^# \?//'; exit 0 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
    shift
done

# ─── Configuration ─────────────────────────────────────────────────────────────

CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
if [[ ! -f "$CONF" ]]; then
    echo -e "${RED}Missing configuration: $CONF${NC}"
    echo "  cp tradinebotte-status/scripts/test_multibot.conf.example ~/.tradinebotte-test.conf"
    exit 1
fi
# shellcheck source=/dev/null
source "$CONF"

SERVER="${TEST_SERVER:?TEST_SERVER missing in $CONF}"
PORT="${TEST_PORT:-22}"
ALL_USERS=("${TEST_USERS[@]:?TEST_USERS missing in $CONF}")
ALL_PASSWORDS=("${TEST_PASSWORDS[@]:?TEST_PASSWORDS missing in $CONF}")
INSTALL_DIR="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"

if [[ "$COLLECTOR_IDX" -ge "${#ALL_USERS[@]}" ]]; then
    echo "ERROR: --collector-idx=$COLLECTOR_IDX out of range (${#ALL_USERS[@]} users in conf)"
    exit 1
fi
COLL_USER="${ALL_USERS[$COLLECTOR_IDX]}"
COLL_PASS="${ALL_PASSWORDS[$COLLECTOR_IDX]}"

SVC_NAME="tradinebotte-status.service"

# ─── SSH / rsync helpers ───────────────────────────────────────────────────────

SSH_OPTS="-o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no
          -o ServerAliveInterval=10 -o ServerAliveCountMax=3
          -o PreferredAuthentications=password -p $PORT"

_ssh() {
    SSHPASS="$COLL_PASS" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
        -o PreferredAuthentications=password \
        -p "$PORT" "$COLL_USER@$SERVER" "$@" 2>&1
}

_rsync() {
    local ssh_cmd="ssh $SSH_OPTS"

    # status_collector.py, heartbeat_query.py, scripts/
    SSHPASS="$COLL_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='tests' --exclude='tradinetools' \
        --exclude='heartbeat.db' --exclude='*.log' \
        -e "$ssh_cmd" \
        "$LOCAL_REPO/tradinebotte-status/" "$COLL_USER@$SERVER:$INSTALL_DIR/" 2>&1 || return 1

    # tradinetools shared library
    SSHPASS="$COLL_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' \
        -e "$ssh_cmd" \
        "$LOCAL_REPO/tradinetools/" "$COLL_USER@$SERVER:$INSTALL_DIR/tradinetools/" 2>&1 || return 1
}

# ─── Pre-flight ─────────────────────────────────────────────────────────────────

section "PRE-FLIGHT"

if [[ ! -x "$(command -v sshpass)" ]]; then
    err "sshpass not found — install with: apt-get install sshpass"
    exit 1
fi
ok "sshpass OK"

if [[ ! -x "$(command -v rsync)" ]]; then
    err "rsync not found — install with: apt-get install rsync"
    exit 1
fi
ok "rsync OK"

mkdir -p ~/.ssh && chmod 700 ~/.ssh
if ! ssh-keygen -F "[$SERVER]:$PORT" &>/dev/null && \
   ! ssh-keygen -F "$SERVER"         &>/dev/null; then
    info "Adding $SERVER:$PORT to known_hosts..."
    ssh-keyscan -p "$PORT" -H "$SERVER" >> ~/.ssh/known_hosts 2>/dev/null
fi
info "Target: $COLL_USER@$SERVER:$PORT  install_dir: $INSTALL_DIR"
info "Commit: $GIT_HASH"

# ─── Step 1: rsync ─────────────────────────────────────────────────────────────

if [[ "$VERIFY_ONLY" == "false" ]]; then
    section "STEP 1 — RSYNC"
    if _rsync; then
        ok "status collector files synced"
    else
        err "rsync failed"
        exit 1
    fi
fi

# ─── Step 2: install tradinetools + systemd unit ───────────────────────────────

if [[ "$VERIFY_ONLY" == "false" ]]; then
    section "STEP 2 — INSTALL"
    INSTALL_OUT=$(_ssh "
        set -e
        INSTALL=${INSTALL_DIR}

        # Write version stamp
        echo '${GIT_HASH}' > \${INSTALL}/version.stamp
        echo 'version.stamp written: ${GIT_HASH}'

        # Detect the active venv
        if   [ -d \"\${INSTALL}/.venv\" ]; then VENV=\${INSTALL}/.venv
        elif [ -d \"\${INSTALL}/venv\"  ]; then VENV=\${INSTALL}/venv
        else echo 'ERROR: no venv at \${INSTALL}/.venv or \${INSTALL}/venv'; exit 1; fi
        echo \"venv: \${VENV}\"

        # Install tradinetools into site-packages directly
        PYVER=\$(\"\${VENV}/bin/python3\" -c \
            'import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")')
        SITE=\${VENV}/lib/python\${PYVER}/site-packages
        mkdir -p \"\${SITE}\"
        rm -rf \"\${SITE}/tradinetools\"
        cp -r \"\${INSTALL}/tradinetools/tradinetools\" \"\${SITE}/tradinetools\"
        echo 'tradinetools installed (site-packages copy)'

        # Validate
        if \"\${VENV}/bin/python3\" -c \
               'from tradinetools.zmq import default_status_addr' 2>/dev/null; then
            echo 'tradinetools importable: OK'
        else
            echo 'ERROR: tradinetools not importable after install'; exit 1
        fi

        # Run the systemd unit installer
        export XDG_RUNTIME_DIR=/run/user/\$(id -u)
        bash \"\${INSTALL}/scripts/install_status_service.sh\"
    " 2>&1)
    echo "$INSTALL_OUT"
    if echo "$INSTALL_OUT" | grep -q "^ERROR:"; then
        err "Install step failed"
        exit 1
    fi
    ok "Install complete"
fi

# ─── Step 3: start / restart service ──────────────────────────────────────────

if [[ "$SKIP_RESTART" == "false" ]]; then
    section "STEP 3 — START SERVICE"
    START_OUT=$(_ssh "
        export XDG_RUNTIME_DIR=/run/user/\$(id -u)
        if systemctl --user is-active '${SVC_NAME}' 2>/dev/null; then
            systemctl --user restart '${SVC_NAME}'
            echo 'service restarted'
        else
            systemctl --user start '${SVC_NAME}'
            echo 'service started'
        fi
        sleep 2
    " 2>&1)
    echo "$START_OUT"
    if echo "$START_OUT" | grep -qiE "^(failed|error)"; then
        err "Service start failed"
        exit 1
    fi
    ok "Service running"
fi

# ─── Step 4: verify ────────────────────────────────────────────────────────────

section "STEP 4 — VERIFY"
VERIFY_OUT=$(_ssh "
    export XDG_RUNTIME_DIR=/run/user/\$(id -u)
    echo '--- systemctl status ---'
    systemctl --user status '${SVC_NAME}' --no-pager -n 5 2>&1 || true
    echo '--- version stamp ---'
    cat ${INSTALL_DIR}/version.stamp 2>/dev/null || echo '(no version.stamp)'
    echo '--- state DB heartbeat rows ---'
    _DBP=\"\${TRADINEBOTTE_DB:-/data1/tradinebotte-shared/database/tradinebotte.db}\"
    if [ -f \"\$_DBP\" ]; then
        python3 -c \"
import sqlite3
db = sqlite3.connect('\$_DBP')
cnt = db.execute('SELECT count(*) FROM heartbeats').fetchone()[0]
print(f'  {cnt} total row(s) in heartbeats table')
db.close()
\" 2>/dev/null || echo '  (could not query DB)'
    else
        echo \"  state DB not found at \$_DBP\"
    fi
" 2>&1)
echo "$VERIFY_OUT"

if echo "$VERIFY_OUT" | grep -q "Active: active (running)"; then
    ok "Service is active"
else
    warn "Service may not be running — check: journalctl --user -u $SVC_NAME -n 30"
    FAILURES=$((FAILURES + 1))
fi

# ─── Summary ───────────────────────────────────────────────────────────────────

tbnt_record_deploy "$COLL_USER" status_collector "$([[ $FAILURES -eq 0 ]] && echo OK || echo FAILED)"

echo ""
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${BOLD}${GREEN}  Deploy complete.${NC}  commit=${GIT_HASH}"
    echo ""
    echo "  Monitor    :  bash tradinebotte-status/scripts/heartbeat_status.sh"
    echo "  Logs       :  journalctl --user -u $SVC_NAME -f"
    echo "  Status     :  systemctl --user status $SVC_NAME"
else
    echo -e "${BOLD}${RED}  Deploy finished with ${FAILURES} failure(s).${NC}"
    exit 1
fi
