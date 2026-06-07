#!/usr/bin/env bash
# update_swing.sh — Push a code update to the swing-trading user and restart the bot.
#
# Lightweight update (no full reinstall, no venv rebuild):
#   1. rsync   — push changed source files (excludes live.db, *.log, venv)
#   2. config  — write config.json pointing to the swing strategy (safe to overwrite)
#   3. restart — stop old bot + start new bot in ONE SSH session
#   4. verify  — show first startup lines from live.log
#
# Uses TEST_SWING_USER_IDX from ~/.tradinebotte-test.conf (default: 4).
#
# Rules respected:
#   - Maximum 4 SSH connections total (rsync + config + restart + verify).
#   - Stop uses the PID file (kill $PID), never pkill — avoids hitting other users.
#   - Bot started with </dev/null and disown — SSH session exits cleanly.
#   - No --simulate: absent API key is enough for simulated orders.
#
# Usage:
#   bash scripts/update_swing.sh
#   bash scripts/update_swing.sh --skip-restart   # rsync + config only, no restart
#   bash scripts/update_swing.sh --verify-only    # check log/process only

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
            grep '^#' "${BASH_SOURCE[0]}" | head -25 | sed 's/^# \?//'
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

SW_IDX="${TEST_SWING_USER_IDX:-4}"
if [[ "$SW_IDX" -ge "${#ALL_USERS[@]}" ]]; then
    echo "ERROR: TEST_SWING_USER_IDX=$SW_IDX is out of range (only ${#ALL_USERS[@]} users)"
    exit 1
fi
SW_USER="${ALL_USERS[$SW_IDX]}"
SW_PASS="${ALL_PASSWORDS[$SW_IDX]}"

STRATEGY_PATH="${TEST_SWING_STRATEGY:-strategies/swing/swing_BTCUSDT.json}"

# ─── SSH helpers ───────────────────────────────────────────────────────────────

_ssh() {
    SSHPASS="$SW_PASS" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
        -o PreferredAuthentications=password \
        -p "$PORT" "$SW_USER@$SERVER" "$@" 2>&1
}

_rsync() {
    local ssh_opts="-p $PORT -o StrictHostKeyChecking=yes -o PreferredAuthentications=password"

    # tradinebotte-polymarket/ contents → $INSTALL_DIR/ (flat layout)
    SSHPASS="$SW_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='live.db' --exclude='*.log' --exclude='venv' --exclude='.venv' \
        --exclude='scripts' --exclude='tests' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-polymarket/" "$SW_USER@$SERVER:$INSTALL_DIR/" 2>&1 || return 1

    # tradinebotte-cex/ Python files → $INSTALL_DIR/ (flat layout)
    SSHPASS="$SW_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='scripts' --exclude='tests' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-cex/" "$SW_USER@$SERVER:$INSTALL_DIR/" 2>&1 || return 1

    # CEX strategy JSON config files → $INSTALL_DIR/strategies/
    SSHPASS="$SW_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --filter='+ **/' --filter='+ *.json' --filter='- *' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-cex/strategies/" "$SW_USER@$SERVER:$INSTALL_DIR/strategies/" 2>&1 || return 1

    # requirements.txt
    SSHPASS="$SW_PASS" /usr/bin/sshpass -e \
        rsync -az \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/requirements.txt" "$SW_USER@$SERVER:$INSTALL_DIR/" 2>&1 || return 1

    # tradinetools shared library
    SSHPASS="$SW_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinetools/" "$SW_USER@$SERVER:$INSTALL_DIR/tradinetools/" 2>&1 || return 1
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
info "Target: $SW_USER@$SERVER:$PORT — install_dir: $INSTALL_DIR"
info "Strategy: $STRATEGY_PATH"

# ─── Step 1: rsync ─────────────────────────────────────────────────────────────
if [[ "$VERIFY_ONLY" == "false" ]]; then
    section "STEP 1 — RSYNC"
    if _rsync; then
        ok "Code synced to $SW_USER"
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
    info "Stopping old bot and starting updated one..."

    RESTART_OUT=$(_ssh "
        INSTALL=$INSTALL_DIR
        export XDG_RUNTIME_DIR=/run/user/\$(id -u)
        cd \$INSTALL

        echo 'updating dependencies...'
        if [ -d \$INSTALL/.venv ]; then VENV=\$INSTALL/.venv/bin; else VENV=\$INSTALL/venv/bin; fi
        \$VENV/pip install --quiet -r \$INSTALL/requirements.txt 2>/dev/null && echo 'deps ok' || echo 'pip warning (non-fatal)'
        \$VENV/pip install --quiet -e \$INSTALL/tradinetools 2>/dev/null && echo 'tradinetools ok' || echo 'tradinetools warning (non-fatal)'

        if systemctl --user is-active tradinebotte-live.service >/dev/null 2>&1 \
           || systemctl --user is-enabled tradinebotte-live.service >/dev/null 2>&1; then
            echo 'detected user service: tradinebotte-live.service'
            for P in \$(pgrep -u \"\$(whoami)\" -f \"live_bot\" 2>/dev/null || true); do
                if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
                    kill \"\$P\" 2>/dev/null && echo \"killed stale live_bot pid=\$P\"
                fi
            done
            sleep 2
            systemctl --user restart tradinebotte-live.service \
                && echo 'systemd restarted' \
                || echo 'user service restart failed'
        else
            PID_FILE=\$INSTALL/live.pid
            for P in \$(pgrep -u \"\$(whoami)\" -f \"live_bot\" 2>/dev/null || true); do
                if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
                    kill \"\$P\" 2>/dev/null && echo \"killed stale live_bot pid=\$P\"
                fi
            done
            sleep 1
            rm -f \"\$PID_FILE\"
            sleep 1
            PYTHON=\$(ls \$INSTALL/.venv/bin/python3 \$INSTALL/venv/bin/python3 2>/dev/null | head -1)
            nohup \$PYTHON live_bot.py </dev/null >>live.log 2>&1 &
            BOT_PID=\$!
            disown \$BOT_PID
            echo \$BOT_PID > \"\$PID_FILE\"
            echo \"started pid=\$BOT_PID\"
        fi
    ")
    echo "$RESTART_OUT"

    if echo "$RESTART_OUT" | grep -qE 'systemd restarted|started pid='; then
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
        STATE=\$(systemctl --user is-active tradinebotte-live.service 2>/dev/null || echo unknown)
        echo \"PID=0 NOT running — user service state=\$STATE\"
    fi
    echo '=== startup log ==='
    tail -25 $INSTALL_DIR/live.log
")
echo "$VERIFY_OUT"

if echo "$VERIFY_OUT" | grep -qE '^PID=[1-9][0-9]* running'; then
    ok "$SW_USER: bot is running"
else
    err "$SW_USER: bot is NOT running"
fi

if echo "$VERIFY_OUT" | grep -q "SwingStrategy"; then
    ok "SwingStrategy loaded"
elif echo "$VERIFY_OUT" | grep -q "swing"; then
    ok "Swing strategy detected in log"
else
    warn "SwingStrategy line not found in log — check manually"
fi

ERROR_COUNT=$(echo "$VERIFY_OUT" | grep -cE '\[ERROR\]|\[CRITICAL\]' || true)
[[ "$ERROR_COUNT" -eq 0 ]] && ok "No errors at startup" \
    || err "$ERROR_COUNT ERROR/CRITICAL line(s) in startup log"

# ─── Report ────────────────────────────────────────────────────────────────────
section "RESULT"
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${BOLD}${GREEN}  SUCCESS — $SW_USER updated and running${NC}"
    exit 0
else
    echo -e "${BOLD}${RED}  FAILURE — $FAILURES issue(s) found${NC}"
    exit 1
fi
