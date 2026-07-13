#!/usr/bin/env bash
# test_standalone_deploy.sh — Standalone (live_bot.py) integration test
#
# Verifies that the standalone deployment (Option A, no shared feed) works
# correctly for the designated standalone user (TEST_STANDALONE_USER_IDX).
#
# Tested scenario:
#   1. Deploy code to the standalone user
#   2. Run start_bot.sh
#   3. Verify WebSocket connected in the log
#   4. No startup errors
#
# Uses TEST_STANDALONE_USER_IDX from ~/.tradinebotte-test.conf (default: 2).
# Shares the same configuration file as test_multibot_deploy.sh.
#
# Usage:
#   bash scripts/test_standalone_deploy.sh
#   bash scripts/test_standalone_deploy.sh --skip-deploy
#   TEST_MULTIBOT_CONF=/path/to/conf bash scripts/test_standalone_deploy.sh
#
# Local prerequisites : sshpass (apt-get install sshpass)
# Server prerequisites: python3-venv, python3.X-venv

set -uo pipefail

LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SKIP_DEPLOY=false

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

section() { echo -e "\n${BOLD}${YELLOW}═══ $* ═══${NC}"; }
info()    { echo -e "${BLUE}  → $*${NC}"; }
ok()      { echo -e "${GREEN}  ✓ $*${NC}"; }
warn()    { echo -e "${YELLOW}  ! $*${NC}"; }
err()     { echo -e "${RED}  ✗ $*${NC}"; FAILURES=$((FAILURES + 1)); }

FAILURES=0
START_TS=$(date +%s)

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-deploy) SKIP_DEPLOY=true ;;
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
# The repo is rsynced to a SEPARATE source dir and installed into a clean INSTALL_DIR
# (mirrors a real user: clone here, install there). Installing in-place would leave
# the tradinetools/ source dir in the install root, shadowing the pip-installed
# package as a PEP-420 namespace ("cannot import name 'heartbeat_loop'").
SRC_DIR="${INSTALL_DIR}-src"

SA_IDX="${TEST_STANDALONE_USER_IDX:-2}"
if [[ "$SA_IDX" -ge "${#ALL_USERS[@]}" ]]; then
    echo "ERROR: TEST_STANDALONE_USER_IDX=$SA_IDX is out of range (${#ALL_USERS[@]} users)"
    exit 1
fi
SA_USER="${ALL_USERS[$SA_IDX]}"
SA_PASS="${ALL_PASSWORDS[$SA_IDX]}"

run() {
    SSHPASS="$SA_PASS" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -p "$PORT" "$SA_USER@$SERVER" "$@" 2>&1
}

deploy_code() {
    SSHPASS="$SA_PASS" /usr/bin/sshpass -e \
        rsync -az --delete \
        --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='.venv' --exclude='venv/' \
        --exclude='config.json' --exclude='live.db' --exclude='*.log' \
        -e "ssh -p $PORT -o StrictHostKeyChecking=yes" \
        "$LOCAL_REPO/" "$SA_USER@$SERVER:$SRC_DIR/" 2>&1
}

# Purge the test account's rows from the shared state DB. During a run the bots PUSH
# heartbeats to the PROD status collector, and after teardown those rows would linger as
# DEAD and inflate every consumer's issue count (the status page, bot_status.sh). Runs
# LOCALLY on the deployer (has /data1); the helper REFUSES any inventory/prod account, so a
# clobbered TEST_STANDALONE_USER_IDX cannot nuke a live bot's history. Idempotent — safe to
# call at both start (clears a prior crashed run) and teardown.
purge_test_db_rows() {
    local helper="$LOCAL_REPO/tradinebotte-status/purge_account_state.py"
    [ -f "$helper" ] || return 0
    # Run under the `claudes` group (setgid shared dir + group-writable WAL) — same rule
    # the status collector follows so shared-DB writes keep the right group/perms.
    sg claudes -c "umask 002; python3 '$helper' --account '$SA_USER'" 2>&1 | sed 's/^/  /' || true
}

# ─── Pre-flight ─────────────────────────────────────────────────────────────────
section "PRE-FLIGHT"

if [[ ! -x "$(command -v sshpass)" ]]; then
    echo "ERROR: sshpass not found. Install: apt-get install sshpass"
    exit 1
fi
ok "sshpass OK"
ok "Config: $CONF"
info "Server: $SERVER:$PORT — standalone user: $SA_USER (index $SA_IDX)"

mkdir -p ~/.ssh && chmod 700 ~/.ssh
if ! ssh-keygen -F "[$SERVER]:$PORT" &>/dev/null && ! ssh-keygen -F "$SERVER" &>/dev/null; then
    info "Adding host key $SERVER:$PORT to known_hosts..."
    ssh-keyscan -p "$PORT" -H "$SERVER" >> ~/.ssh/known_hosts 2>/dev/null
fi

if run "echo ok" &>/dev/null; then
    ok "SSH $SA_USER@$SERVER:$PORT"
else
    echo "ERROR: cannot connect to $SA_USER@$SERVER:$PORT"
    exit 1
fi

# ─── Phase 1: Cleanup ──────────────────────────────────────────────────────────
section "PHASE 1 — CLEANUP"
run "
    PID_FILE=$INSTALL_DIR/live.pid
    if [ -f \"\$PID_FILE\" ]; then
        PID=\$(cat \"\$PID_FILE\")
        kill -0 \"\$PID\" 2>/dev/null && kill \"\$PID\" 2>/dev/null || true
        rm -f \"\$PID_FILE\"
    fi
    sleep 2
    pkill -9 -u \$(id -u) -f '[l]ive_bot.py' 2>/dev/null || true
    rm -rf $INSTALL_DIR $SRC_DIR
    rm -rf \"\${HOME}/tmp/tradinebotte-standalone-test\"
    exit 0
" && ok "$SA_USER: cleaned up" || warn "$SA_USER: partial cleanup"
purge_test_db_rows   # clear any residue left by a prior crashed run

# ─── Phase 2: Deploy ───────────────────────────────────────────────────────────
if [[ "$SKIP_DEPLOY" == "false" ]]; then
    section "PHASE 2 — DEPLOY"
    info "rsync → $SA_USER..."
    deploy_code && ok "$SA_USER: rsync OK" || { err "$SA_USER: rsync failed"; }
    run "cd $SRC_DIR && TRADINEBOTTE_DIR=$INSTALL_DIR bash scripts/install.sh --lang EN $INSTALL_DIR" \
        && ok "$SA_USER: install.sh OK" || { err "$SA_USER: install.sh failed"; }
    # Write a simulation config.json directly — setup.py uses getpass(/dev/tty)
    # which is incompatible with non-interactive SSH sessions.
    run "python3 - <<'PYEOF'
import json, os
path = os.path.join(os.path.expanduser('$INSTALL_DIR'), 'config.json')
cfg = {'private_key': '', 'api_key': '', 'api_secret': '', 'api_passphrase': ''}
with open(path, 'w') as f:
    json.dump(cfg, f, indent=2)
os.chmod(path, 0o600)
print('Simulation config.json written')
PYEOF" && ok "$SA_USER: config.json (simulation) created" \
        || { err "$SA_USER: config.json creation failed"; }
    [[ $FAILURES -gt 0 ]] && { echo -e "${RED}Deploy failed — aborting.${NC}"; exit 1; }
else
    section "PHASE 2 — DEPLOY (skipped)"
fi

# ─── Phase 3: Start ────────────────────────────────────────────────────────────
section "PHASE 3 — START $SA_USER"
info "starting bot for $SA_USER via run.sh..."
# Use the entry point install.sh creates + documents (run.sh → the real start_bot.sh).
# install.sh deploys a FLAT layout (live_bot.py at the install root), so the source
# tree's scripts/start_bot.sh path does not exist here — run.sh resolves it correctly.
START_OUT=$(run "cd $INSTALL_DIR && TRADINEBOTTE_DIR=$INSTALL_DIR bash run.sh")
if echo "$START_OUT" | grep -qE "Bot running|Bot en cours"; then
    ok "$SA_USER: bot started"
else
    err "$SA_USER: start_bot.sh failed"
    echo "$START_OUT"
fi

# ─── Phase 4: Verify ───────────────────────────────────────────────────────────
section "PHASE 4 — VERIFICATION"
LOG="$INSTALL_DIR/live.log"

# A cold fresh-install bot must fetch markets then connect its WebSocket (~10-20s),
# so poll the log instead of a fixed sleep — avoids a startup-timing flake.
info "Waiting for WebSocket connection (up to 60s)..."
WS_OK=false
for _i in $(seq 1 20); do
    if run "grep -q 'WebSocket connected' $LOG 2>/dev/null"; then
        WS_OK=true
        break
    fi
    sleep 3
done
if [[ "$WS_OK" == "true" ]]; then
    ok "$SA_USER: WebSocket connected"
else
    err "$SA_USER: WebSocket not connected in $LOG after 60s"
fi
if run "grep -q 'an instance is already running' $LOG 2>/dev/null"; then
    err "$SA_USER: log contains 'an instance is already running'"
else
    ok "$SA_USER: log clean (no single-instance error)"
fi
ERROR_COUNT=$(run "grep -ciE '\[(ERROR|CRITICAL)\]' $LOG 2>/dev/null || true")
[[ "$ERROR_COUNT" -eq 0 ]] && ok "$SA_USER: no critical errors" \
    || err "$SA_USER: $ERROR_COUNT ERROR/CRITICAL line(s) in log"

# ─── Phase 5: Teardown ─────────────────────────────────────────────────────────
section "PHASE 5 — TEARDOWN"
run "
    PID_FILE=$INSTALL_DIR/live.pid
    if [ -f \"\$PID_FILE\" ]; then
        PID=\$(cat \"\$PID_FILE\")
        kill -0 \"\$PID\" 2>/dev/null && kill \"\$PID\" 2>/dev/null || true
        rm -f \"\$PID_FILE\"
    else
        pkill -u \$(id -u) -f '[l]ive_bot.py' 2>/dev/null || true
    fi
    sleep 1
    # Leave the dedicated test account clean — no persistent install/source state.
    rm -rf $INSTALL_DIR $SRC_DIR
    exit 0
"
ok "$SA_USER: bot stopped + test dirs removed"
purge_test_db_rows   # remove this run's heartbeat/deploy rows from the shared state DB
ok "$SA_USER: shared-DB rows purged"

# ─── Final report ──────────────────────────────────────────────────────────────
ELAPSED=$(( $(date +%s) - START_TS ))
section "FINAL REPORT"
echo -e "  Duration: ${ELAPSED}s | Failures: $FAILURES"
echo ""
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${BOLD}${GREEN}  SUCCESS — standalone deployment OK${NC}"
    exit 0
else
    echo -e "${BOLD}${RED}  FAILURE — $FAILURES assertion(s) failed${NC}"
    exit 1
fi
