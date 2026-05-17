#!/usr/bin/env bash
# update_standalone.sh — Push a code update to the standalone user and restart the bot.
#
# Lightweight update (no full reinstall, no venv rebuild):
#   1. rsync   — push changed source files (excludes config.json, live.db, *.log, venv)
#   2. restart — stop old bot + start new bot in ONE SSH session
#   3. verify  — show first startup lines from live.log
#
# Uses TEST_STANDALONE_USER_IDX from ~/.tradinebotte-test.conf (default: 2).
#
# Rules respected:
#   - Maximum 3 SSH connections total (rsync + restart + verify).
#   - pkill always scoped to the target user (-u UID) — never touches other users.
#   - Bot started with </dev/null and disown — SSH session exits cleanly.
#   - No --simulate: absent API key is enough for simulated orders.
#
# Usage:
#   bash scripts/update_standalone.sh
#   bash scripts/update_standalone.sh --skip-restart   # rsync only, no restart
#   bash scripts/update_standalone.sh --verify-only    # check log/process only
#
# Local prerequisites: sshpass (apt-get install sshpass)

set -uo pipefail

LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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

SA_IDX="${TEST_STANDALONE_USER_IDX:-2}"
if [[ "$SA_IDX" -ge "${#ALL_USERS[@]}" ]]; then
    echo "ERROR: TEST_STANDALONE_USER_IDX=$SA_IDX is out of range"
    exit 1
fi
SA_USER="${ALL_USERS[$SA_IDX]}"
SA_PASS="${ALL_PASSWORDS[$SA_IDX]}"

# ─── SSH helpers ───────────────────────────────────────────────────────────────
# One helper per operation type — avoids per-call boilerplate and makes
# the number of connections explicit.

_ssh() {
    SSHPASS="$SA_PASS" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -p "$PORT" "$SA_USER@$SERVER" "$@" 2>&1
}

_rsync() {
    SSHPASS="$SA_PASS" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='.venv' --exclude='venv/' \
        --exclude='config.json' --exclude='live.db' --exclude='*.log' \
        -e "ssh -p $PORT -o StrictHostKeyChecking=yes" \
        "$LOCAL_REPO/" "$SA_USER@$SERVER:$INSTALL_DIR/" 2>&1
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
    section "STEP 1 — RSYNC"   # connection #1
    if _rsync; then
        ok "Code synced to $SA_USER"
    else
        err "rsync failed"
        exit 1
    fi
fi

# ─── Step 2: stop + start (single SSH session) ─────────────────────────────────
if [[ "$SKIP_RESTART" == "false" ]]; then
    section "STEP 2 — RESTART"   # connection #2
    info "Stopping old bot and starting updated one..."

    # Everything in ONE SSH session:
    #   - pkill scoped to this user only (-u UID prevents killing other users)
    #   - nohup with </dev/null so SSH session can exit without keeping stdin open
    #   - disown removes the job from the shell table before SSH exits
    #   - no sleep after disown — SSH exits immediately, bot keeps running
    RESTART_OUT=$(_ssh "
        UID_SELF=\$(id -u)
        pkill -u \"\$UID_SELF\" -f '[l]ive_bot\.py' 2>/dev/null && echo 'stopped' || echo 'no bot running'
        sleep 2
        cd $INSTALL_DIR
        nohup venv/bin/python3 live_bot.py </dev/null >>live.log 2>&1 & disown
        echo \"started pid=\$!\"
    ")
    echo "$RESTART_OUT"

    if echo "$RESTART_OUT" | grep -q "started pid="; then
        ok "Bot restart command sent"
    else
        err "Restart command did not confirm start"
    fi

    info "Waiting 6s for startup..."
    sleep 6
fi

# ─── Step 3: verify ────────────────────────────────────────────────────────────
section "STEP 3 — VERIFY"   # connection #3
VERIFY_OUT=$(_ssh "
    echo '=== process ==='
    pgrep -u \$(id -u) -f '[l]ive_bot\.py' | xargs -I{} sh -c 'echo \"PID={} running\"' 2>/dev/null || echo 'NOT RUNNING'
    echo '=== startup log ==='
    tail -20 $INSTALL_DIR/live.log
")
echo "$VERIFY_OUT"

if echo "$VERIFY_OUT" | grep -q "running"; then
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
