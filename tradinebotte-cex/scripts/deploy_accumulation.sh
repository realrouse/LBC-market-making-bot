#!/usr/bin/env bash
# deploy_accumulation.sh — Deploy and (re)start the BTC accumulation bot.
#
# Parameterized generic engine. Per-account presets (ACCUM_USER_IDX, BOT_STRATEGY) live in
# inventory.toml (deploy_env) and are injected by scripts/deploy.py — no per-account wrapper.
#
#   accumulation_bot → accumulation_bot.pid / accumulation_bot.log / live_accum.db
#
# Reads credentials from ~/.tradinebotte-test.conf
#
# Usage (via the orchestrator, or with presets directly):
#   bash tradinebotte-cex/scripts/deploy_all.sh --only "account-3 — accumulation"
#   ACCUM_USER_IDX=2 BOT_STRATEGY=strategies/accumulation/btc_accumulation.json \
#       bash scripts/deploy_accumulation.sh [--skip-restart|--verify-only]

set -uo pipefail

BOT_NAME="accumulation_bot"
BOT_SCRIPT="accumulation_bot.py"
BOT_STRATEGY="${BOT_STRATEGY:-strategies/accumulation/btc_accumulation.json}"

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
# shellcheck source=/dev/null
source "$CONF"

SERVER="${TEST_SERVER:?}"
PORT="${TEST_PORT:-22}"
ALL_USERS=("${TEST_USERS[@]:?}")
ALL_PASSWORDS=("${TEST_PASSWORDS[@]:?}")
INSTALL_DIR="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"

SC_IDX="${ACCUM_USER_IDX:-${TEST_STANDALONE_USER_IDX:-2}}"
SC_USER="${ALL_USERS[$SC_IDX]}"
SC_PASS="${ALL_PASSWORDS[$SC_IDX]}"

LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GIT_HASH=$(git -C "$LOCAL_REPO" rev-parse --short HEAD 2>/dev/null || echo "unknown")
source "$LOCAL_REPO/tradinebotte-status/scripts/record_deploy.sh"

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
        rsync -az --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='config.json' --exclude='live*.db' --exclude='*.log' \
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
        --filter='+ **/' --filter='+ *.json' --filter='- *' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-cex/strategies/" "$SC_USER@$SERVER:$INSTALL_DIR/strategies/" 2>&1

    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/requirements.txt" "$SC_USER@$SERVER:$INSTALL_DIR/" 2>&1

    SSHPASS="$SC_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' \
        -e "ssh $ssh_opts" \
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

    REMOTE_CMD="cd $INSTALL_DIR"$'\n'
    REMOTE_CMD+="echo '${GIT_HASH}' > $INSTALL_DIR/version.stamp
echo 'updating dependencies...'
if [ -d $INSTALL_DIR/.venv ]; then VENV=$INSTALL_DIR/.venv/bin; else VENV=$INSTALL_DIR/venv/bin; fi
\$VENV/pip install --quiet -r $INSTALL_DIR/requirements.txt 2>/dev/null && echo 'deps ok' || echo 'pip warning (non-fatal)'
PYVER=\$(\$VENV/python3 -c 'import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")')
SITE=\$(dirname \$VENV)/lib/python\${PYVER}/site-packages
mkdir -p \"\$SITE\" && rm -rf \"\$SITE/tradinetools\"
cp -r $INSTALL_DIR/tradinetools/tradinetools \"\$SITE/tradinetools\" && echo 'tradinetools ok'

export XDG_RUNTIME_DIR=/run/user/\$(id -u)
# Install the systemd --user unit if missing (idempotent: accounts that already have it
# are untouched). ExecStart is templated with this deploy's BOT_STRATEGY, so each
# account runs its own strategy (e.g. btc_accumulation_mexc.json on account-2).
ACC_UNIT=\$HOME/.config/systemd/user/tradinebotte-accumulation.service
if [ ! -f \"\$ACC_UNIT\" ]; then
    echo 'installing tradinebotte-accumulation.service'
    mkdir -p \$HOME/.config/systemd/user
    cat > \"\$ACC_UNIT\" <<UNITEOF
[Unit]
Description=tradinebotte — BTC accumulation bot
After=network.target
Wants=network.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=%h/tradinebotte
ExecStart=%h/tradinebotte/.venv/bin/python3 %h/tradinebotte/${BOT_SCRIPT} --strategy %h/tradinebotte/${BOT_STRATEGY} --dir %h/tradinebotte
Restart=on-failure
RestartSec=30
StandardOutput=null
StandardError=null

[Install]
WantedBy=default.target
UNITEOF
    loginctl enable-linger \$(whoami) >/dev/null 2>&1 || true
    systemctl --user daemon-reload
    systemctl --user enable tradinebotte-accumulation.service >/dev/null 2>&1
    echo 'unit installed + enabled + linger'
fi
if systemctl --user is-active tradinebotte-accumulation.service >/dev/null 2>&1 \
   || systemctl --user is-enabled tradinebotte-accumulation.service >/dev/null 2>&1; then
    echo 'detected user service: tradinebotte-accumulation.service'
    for P in \$(pgrep -u \"\$(whoami)\" -f \"${BOT_SCRIPT}\" 2>/dev/null || true); do
        if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
            kill \"\$P\" 2>/dev/null && echo \"killed stale ${BOT_NAME} pid=\$P\"
        fi
    done
    sleep 2
    systemctl --user restart tradinebotte-accumulation.service \
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
        err "${BOT_NAME} did not start"
    fi
    if echo "$RESTART_OUT" | grep -q "systemd restarted"; then
        info "Waiting 36s for systemd restart (RestartSec=30 + startup)..."
        sleep 36
    else
        info "Waiting 10s for startup..."
        sleep 10
    fi
fi

section "STEP 3 — VERIFY"
VERIFY_CMD="
export XDG_RUNTIME_DIR=/run/user/\$(id -u)
MPID=\"\"
for P in \$(pgrep -u \"\$(whoami)\" -f '${BOT_SCRIPT}' 2>/dev/null || true); do
    if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
        MPID=\$P; break
    fi
done
if [ -n \"\$MPID\" ]; then echo \"  PID=\$MPID running\"
else
    STATE=\$(systemctl --user is-active tradinebotte-accumulation.service 2>/dev/null || echo unknown)
    echo \"  PID=0 NOT running — user service state=\$STATE\"
fi
tail -12 $INSTALL_DIR/${BOT_NAME}.log 2>/dev/null || echo '  (no log yet)'
"
VERIFY_OUT=$(_ssh "$VERIFY_CMD")
echo "$VERIFY_OUT"

if echo "$VERIFY_OUT" | grep -qE 'PID=[1-9][0-9]* running'; then ok "${BOT_NAME}: running"
else err "${BOT_NAME}: NOT running"; fi
if echo "$VERIFY_OUT" | grep -qE "BUY|connected|Accumulation"; then ok "startup OK"
else warn "startup message not yet in log"; fi

section "RESULT"
tbnt_record_deploy "$SC_USER" accumulation_bot "$([[ $FAILURES -eq 0 ]] && echo OK || echo FAILED)"
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${BOLD}${GREEN}  SUCCESS — accumulation_bot running on $SC_USER${NC}"
    echo -e "  Log : $INSTALL_DIR/accumulation_bot.log"
    echo -e "  DB  : $INSTALL_DIR/live_accum.db"
    echo -e "  PID : $INSTALL_DIR/accumulation_bot.pid"
    exit 0
else
    echo -e "${BOLD}${RED}  FAILURE — $FAILURES issue(s)${NC}"; exit 1
fi
