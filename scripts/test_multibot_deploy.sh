#!/usr/bin/env bash
# test_multibot_deploy.sh — Multibot integration test (account_bot + systemd feed)
#
# Architecture under test:
#   Feed owner  (TEST_FEED_USER_IDX)      — systemd tradinebotte-feed service
#   Account bots (TEST_ACCOUNT_USER_IDXS) — account_bot.py, feed_auto_start=false
#
# Phases:
#   1. Pre-flight  — sshpass, SSH connectivity, host-key scan
#   2. Cleanup     — kill stale account_bot/indicators, wipe REMOTE_BOT_DIR
#   3. Feed check  — capture feed.py hash before any deploy
#   4. Deploy      — rsync code + venv + pip to all users
#   5. Feed update — if feed.py changed, restart service or print instructions
#   6. Configure   — write config.json (feed_addr, feed_auto_start=false)
#   7. Indicators  — start optional indicators services
#   8. Launch      — start account_bot instances simultaneously
#   9. Verify init — systemd feed running + N account bots connected
#  10. Sustained   — heartbeat every 30s for DURATION seconds
#  11. Analysis    — collect and analyse logs
#  12. Teardown    — kill account_bot processes ONLY (feed service untouched)
#  13. Report      — SUCCESS / FAILURE with error count
#
# Usage:
#   bash scripts/test_multibot_deploy.sh
#   bash scripts/test_multibot_deploy.sh --duration 300
#   bash scripts/test_multibot_deploy.sh --skip-deploy   # reuse existing install
#
# Configuration (required):
#   cp scripts/test_multibot.conf.example ~/.tradinebotte-test.conf
#   editor ~/.tradinebotte-test.conf
#
# Local prerequisites  : sshpass (apt-get install sshpass)
# Server prerequisites : python3-venv, python3-pip, python3.X-venv,
#                        systemd tradinebotte-feed service installed and enabled

set -euo pipefail

LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DURATION=180
SKIP_DEPLOY=false

# ─── Output helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

section() { echo -e "\n${BOLD}${YELLOW}═══ $* ═══${NC}"; }
info()    { echo -e "${BLUE}  → $*${NC}"; }
ok()      { echo -e "${GREEN}  ✓ $*${NC}"; }
warn()    { echo -e "${YELLOW}  ! $*${NC}"; }
err()     { echo -e "${RED}  ✗ $*${NC}"; FAILURES=$((FAILURES + 1)); }

FAILURES=0
START_TS=$(date +%s)

# ─── Arguments ─────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-deploy) SKIP_DEPLOY=true ;;
        --duration)    DURATION="$2"; shift ;;
        -h|--help)
            grep '^#' "${BASH_SOURCE[0]}" | head -30 | sed 's/^# \?//'
            exit 0 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
    shift
done

# ─── Load configuration ─────────────────────────────────────────────────────────
CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
if [[ ! -f "$CONF" ]]; then
    echo -e "${RED}Missing configuration: $CONF${NC}"
    echo ""
    echo "Create the configuration file from the template:"
    echo "  cp scripts/test_multibot.conf.example ~/.tradinebotte-test.conf"
    echo "  editor ~/.tradinebotte-test.conf"
    echo ""
    echo "Or point to a custom file:"
    echo "  TEST_MULTIBOT_CONF=/path/to/conf bash scripts/test_multibot_deploy.sh"
    exit 1
fi
# shellcheck source=/dev/null
source "$CONF"

SERVER="${TEST_SERVER:?TEST_SERVER missing in $CONF}"
PORT="${TEST_PORT:-22}"
ALL_USERS=("${TEST_USERS[@]:?TEST_USERS missing in $CONF}")
ALL_PASSWORDS=("${TEST_PASSWORDS[@]:?TEST_PASSWORDS missing in $CONF}")

[[ "$DURATION" -eq 180 && -n "${TEST_DURATION:-}" ]] && DURATION="$TEST_DURATION"
REMOTE_INSTALL_DIR="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"
REMOTE_BOT_DIR="${TEST_REMOTE_BOT_DIR:-~/account-sim}"

# Role indices
FEED_IDX="${TEST_FEED_USER_IDX:-0}"
if [[ -n "${TEST_ACCOUNT_USER_IDXS+_}" ]]; then
    ACCOUNT_IDXS=("${TEST_ACCOUNT_USER_IDXS[@]}")
else
    ACCOUNT_IDXS=(0 1)
fi
FEED_ADDR="${TEST_FEED_ADDR:-tcp://127.0.0.1:5557}"
FEED_AUTO_RESTART="${TEST_FEED_AUTO_RESTART:-false}"

# Build deduplicated list of user indices to deploy to
declare -A _SEEN_DEPLOY
DEPLOY_IDXS=()
for _i in "$FEED_IDX" "${ACCOUNT_IDXS[@]}"; do
    [[ -z "${_SEEN_DEPLOY[$_i]+_}" ]] && { DEPLOY_IDXS+=("$_i"); _SEEN_DEPLOY[$_i]=1; }
done
unset _SEEN_DEPLOY _i

# Helpers: run / run_bg / deploy_code all accept indices into ALL_USERS
run() {
    local idx="$1"; shift
    SSHPASS="${ALL_PASSWORDS[$idx]}" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -p "$PORT" "${ALL_USERS[$idx]}@$SERVER" "$@" 2>&1
}

run_bg() {
    local idx="$1"; shift
    SSHPASS="${ALL_PASSWORDS[$idx]}" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -p "$PORT" "${ALL_USERS[$idx]}@$SERVER" "$@" 2>&1 &
}

deploy_code() {
    local idx="$1"
    SSHPASS="${ALL_PASSWORDS[$idx]}" /usr/bin/sshpass -e \
        rsync -az --delete \
        --exclude='.git' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.venv' \
        --exclude='venv/' \
        --exclude='*.db' \
        --exclude='config.json' \
        --exclude='credentials' \
        --exclude='*.log' \
        -e "ssh -p $PORT -o StrictHostKeyChecking=yes" \
        "$LOCAL_REPO/" "${ALL_USERS[$idx]}@$SERVER:$REMOTE_INSTALL_DIR/" 2>&1
}

# ─── Phase 1: Pre-flight ────────────────────────────────────────────────────────
section "PHASE 1 — PRE-FLIGHT"

SSHPASS_BIN=$(command -v sshpass || echo "/usr/bin/sshpass")
if [[ ! -x "$SSHPASS_BIN" ]]; then
    echo "ERROR: sshpass not found. Install with: apt-get install sshpass"
    exit 1
fi
ok "sshpass: $SSHPASS_BIN"
ok "Config: $CONF"
info "Server: $SERVER:$PORT"
info "Feed user   : ${ALL_USERS[$FEED_IDX]} (index $FEED_IDX)"
info "Account bots: $(for i in "${ACCOUNT_IDXS[@]}"; do echo -n "${ALL_USERS[$i]} "; done)"
info "Feed address: $FEED_ADDR"
info "Feed auto-restart: $FEED_AUTO_RESTART"
info "Local repo: $LOCAL_REPO"
info "Test duration: ${DURATION}s"
[[ "$SKIP_DEPLOY" == "true" ]] && info "--skip-deploy mode: deployment skipped"

mkdir -p ~/.ssh && chmod 700 ~/.ssh
if ! ssh-keygen -F "[$SERVER]:$PORT" &>/dev/null && ! ssh-keygen -F "$SERVER" &>/dev/null; then
    info "Adding host key $SERVER:$PORT to known_hosts..."
    ssh-keyscan -p "$PORT" -H "$SERVER" >> ~/.ssh/known_hosts 2>/dev/null
fi
ok "Host key $SERVER verified in known_hosts"

for idx in "${DEPLOY_IDXS[@]}"; do
    if run "$idx" "echo ok" &>/dev/null; then
        ok "SSH ${ALL_USERS[$idx]}@$SERVER:$PORT"
    else
        echo "ERROR: unable to connect to ${ALL_USERS[$idx]}@$SERVER:$PORT"
        exit 1
    fi
done

# Verify the systemd feed service is installed and enabled on the feed user
FEED_SVC_STATUS=$(run "$FEED_IDX" \
    "systemctl is-active tradinebotte-feed 2>/dev/null || echo inactive")
FEED_SVC_ENABLED=$(run "$FEED_IDX" \
    "systemctl is-enabled tradinebotte-feed 2>/dev/null || echo disabled")
info "Feed service on ${ALL_USERS[$FEED_IDX]}: status=$FEED_SVC_STATUS enabled=$FEED_SVC_ENABLED"

if [[ "$FEED_SVC_STATUS" != "active" ]]; then
    warn "Feed service is NOT active on ${ALL_USERS[$FEED_IDX]}"
    warn "  Start it first: sudo systemctl start tradinebotte-feed"
    warn "  (install with: bash scripts/install_feed_service.sh)"
    warn "Continuing — account_bot will fail to connect if feed is not running"
fi

# ─── Phase 2: Cleanup stale processes and runtime dirs ────────────────────────
section "PHASE 2 — CLEANUP"
info "Killing stale account_bot / indicators processes and wiping runtime dirs..."

# Kill shared indicators.py on the feed owner (may not be in ACCOUNT_IDXS)
run "$FEED_IDX" "
    PID_FILE=$REMOTE_INSTALL_DIR/indicators.pid
    if [ -f \"\$PID_FILE\" ]; then
        PID=\$(cat \"\$PID_FILE\")
        kill -0 \"\$PID\" 2>/dev/null && kill \"\$PID\" 2>/dev/null || true
        rm -f \"\$PID_FILE\"
    fi
    sleep 1
    pkill -9 -u \$(id -u) -f '[i]ndicators.py' 2>/dev/null || true
    fuser -k 5559/tcp 2>/dev/null || true
    fuser -k 5561/tcp 2>/dev/null || true
    exit 0
" && ok "${ALL_USERS[$FEED_IDX]}: stale indicators.py cleared" || true

for idx in "${ACCOUNT_IDXS[@]}"; do
    user="${ALL_USERS[$idx]}"
    run "$idx" "
        PID_FILE=$REMOTE_BOT_DIR/account.pid
        if [ -f \"\$PID_FILE\" ]; then
            PID=\$(cat \"\$PID_FILE\")
            kill -0 \"\$PID\" 2>/dev/null && kill \"\$PID\" 2>/dev/null || true
            rm -f \"\$PID_FILE\"
        fi
        sleep 2
        pkill -9 -u \$(id -u) -f '[a]ccount_bot.py' 2>/dev/null || true
        pkill -9 -u \$(id -u) -f '[i]ndicators.py'  2>/dev/null || true
        fuser -k 5559/tcp 2>/dev/null || true
        fuser -k 5560/tcp 2>/dev/null || true
        fuser -k 5561/tcp 2>/dev/null || true
        rm -rf $REMOTE_BOT_DIR
        exit 0
    " && ok "$user: cleaned (processes + $REMOTE_BOT_DIR)" \
      || warn "$user: partial cleanup"
done

# Confirm no residual account_bot processes
STALE=$(run "${ACCOUNT_IDXS[0]}" \
    "ps aux | grep '[a]ccount_bot.py' | wc -l" || echo 0)
[[ "$STALE" -eq 0 ]] && ok "No residual account_bot processes" \
    || warn "$STALE residual account_bot process(es) — continuing"

# ─── Phase 3: Capture current feed.py hash BEFORE any deploy ───────────────────
section "PHASE 3 — FEED VERSION CHECK"

LOCAL_FEED_HASH=$(sha256sum "$LOCAL_REPO/bot/feed.py" | cut -d' ' -f1)
REMOTE_FEED_HASH=$(run "$FEED_IDX" \
    "sha256sum $REMOTE_INSTALL_DIR/bot/feed.py 2>/dev/null | cut -d' ' -f1" || echo "")
FEED_UPDATE_NEEDED=false

if [[ -z "$REMOTE_FEED_HASH" ]]; then
    info "feed.py not yet deployed on ${ALL_USERS[$FEED_IDX]} — first deploy"
    FEED_UPDATE_NEEDED=true
elif [[ "$REMOTE_FEED_HASH" == "$LOCAL_FEED_HASH" ]]; then
    ok "feed.py unchanged (hash: ${LOCAL_FEED_HASH:0:12}…) — no service restart needed"
else
    warn "feed.py changed (remote: ${REMOTE_FEED_HASH:0:12}… → local: ${LOCAL_FEED_HASH:0:12}…)"
    FEED_UPDATE_NEEDED=true
fi

# ─── Phase 4: Deploy ───────────────────────────────────────────────────────────
if [[ "$SKIP_DEPLOY" == "true" ]]; then
    section "PHASE 4 — DEPLOY (skipped — --skip-deploy)"
else
    section "PHASE 4 — DEPLOY"
    for idx in "${DEPLOY_IDXS[@]}"; do
        user="${ALL_USERS[$idx]}"
        info "rsync → $user..."
        deploy_code "$idx" && ok "$user: rsync OK" || { err "$user: rsync failed"; exit 1; }

        info "Creating venv for $user..."
        run "$idx" "python3 -m venv $REMOTE_INSTALL_DIR/venv 2>&1" \
            && ok "$user: venv created" || { err "$user: venv creation failed"; exit 1; }

        info "pip install $user..."
        run "$idx" "
            $REMOTE_INSTALL_DIR/venv/bin/pip install --quiet --upgrade pip
            $REMOTE_INSTALL_DIR/venv/bin/pip install --quiet -r $REMOTE_INSTALL_DIR/requirements.txt
        " && ok "$user: dependencies installed" || { err "$user: pip install failed"; exit 1; }
    done
fi

# ─── Phase 4: Feed service update ─────────────────────────────────────────────
section "PHASE 5 — FEED SERVICE UPDATE"

if [[ "$FEED_UPDATE_NEEDED" == "false" ]] || [[ "$SKIP_DEPLOY" == "true" ]]; then
    ok "No feed service restart required"
else
    info "feed.py was updated — feed service restart needed"
    RESTART_OK=false
    if [[ "$FEED_AUTO_RESTART" == "true" ]]; then
        info "Attempting automatic restart via sudo -n systemctl restart..."
        if run "$FEED_IDX" "sudo -n systemctl restart tradinebotte-feed 2>&1"; then
            sleep 5
            NEW_STATUS=$(run "$FEED_IDX" \
                "systemctl is-active tradinebotte-feed 2>/dev/null || echo inactive")
            if [[ "$NEW_STATUS" == "active" ]]; then
                ok "Feed service restarted successfully (now: $NEW_STATUS)"
                RESTART_OK=true
            else
                warn "Restart command ran but service status: $NEW_STATUS"
            fi
        else
            warn "sudo -n systemctl failed (NOPASSWD sudo may not be configured)"
        fi
    fi
    if [[ "$RESTART_OK" == "false" ]]; then
        warn "Manual restart required on ${ALL_USERS[$FEED_IDX]}:"
        warn "  sudo systemctl restart tradinebotte-feed"
        warn "  sudo systemctl status tradinebotte-feed"
        warn "Run these commands now, then re-run this script with --skip-deploy"
    fi
fi

# Verify feed is actually reachable after any update step
FEED_SVC_STATUS=$(run "$FEED_IDX" \
    "systemctl is-active tradinebotte-feed 2>/dev/null || echo inactive")
[[ "$FEED_SVC_STATUS" == "active" ]] && ok "Feed service active" \
    || err "Feed service not active (status: $FEED_SVC_STATUS)"

# ─── Phase 5: Configure account bots ──────────────────────────────────────────
section "PHASE 6 — CONFIGURE ACCOUNT BOTS"

for idx in "${ACCOUNT_IDXS[@]}"; do
    user="${ALL_USERS[$idx]}"
    run "$idx" "mkdir -p $REMOTE_BOT_DIR"
    # Write minimal config.json: feed_auto_start=false + feed address.
    # Preserves any existing credentials (private_key, api_key, etc.) using
    # python json.load+update so we don't overwrite what was already there.
    run "$idx" "python3 - <<'PYEOF'
import json, os
path = os.path.expanduser('$REMOTE_BOT_DIR/config.json')
cfg = {}
if os.path.exists(path):
    try:
        with open(path) as f:
            cfg = json.load(f)
    except Exception:
        pass
cfg['feed_addr'] = '$FEED_ADDR'
cfg['feed_auto_start'] = False
cfg['indicators_reg_addr'] = 'tcp://127.0.0.1:5561'
with open(path, 'w') as f:
    json.dump(cfg, f, indent=2)
print('config.json updated')
PYEOF" && ok "$user: config.json → feed_auto_start=false, feed_addr=$FEED_ADDR" \
         || err "$user: config.json update failed"
done

# ─── Phase 6: Start shared indicator service (under feed owner) ───────────────
section "PHASE 7 — SHARED INDICATOR SERVICE"
# Indicators is a single machine-wide process (like the feed), owned by the feed
# user. Launch it under FEED_IDX only — each account_bot registers its streams
# at startup via the REP socket (indicators_reg_addr in config.json).
IND_CFG_REL="${TEST_INDICATORS_CONFIG:-}"
[[ -n "${TEST_INDICATORS_CONFIGS:-}" ]] && \
    warn "TEST_INDICATORS_CONFIGS is obsolete — use TEST_INDICATORS_CONFIG (singular)"
if [[ -n "$IND_CFG_REL" ]]; then
    info "Launching shared indicators.py under ${ALL_USERS[$FEED_IDX]} — config=$IND_CFG_REL"
    run_bg "$FEED_IDX" "
        cd $REMOTE_INSTALL_DIR
        nohup $REMOTE_INSTALL_DIR/venv/bin/python3 -u bot/indicators.py \
            --config $REMOTE_INSTALL_DIR/$IND_CFG_REL \
            > $REMOTE_INSTALL_DIR/indicators.log 2>&1 < /dev/null &
        IND_PID=\$!
        disown \$IND_PID
        echo \$IND_PID > $REMOTE_INSTALL_DIR/indicators.pid
        echo \"IND_PID=\$IND_PID\"
    " && ok "${ALL_USERS[$FEED_IDX]}: shared indicators.py started" \
      || warn "${ALL_USERS[$FEED_IDX]}: indicators launch failed"
    wait
    sleep 5
    ok "Shared indicators service started"
else
    info "TEST_INDICATORS_CONFIG not set — indicators service skipped"
fi

# ─── Phase 7: Simultaneous launch of account bots ─────────────────────────────
N_BOTS=${#ACCOUNT_IDXS[@]}
section "PHASE 8 — SIMULTANEOUS LAUNCH OF $N_BOTS ACCOUNT BOTS"
info "Sending $N_BOTS launch commands in parallel..."

LAUNCH_CMD="
    cd $REMOTE_INSTALL_DIR
    TRADINEBOTTE_DIR=$REMOTE_BOT_DIR \\
    nohup $REMOTE_INSTALL_DIR/venv/bin/python3 -u bot/account_bot.py --verbose \\
        > $REMOTE_BOT_DIR/account.log 2>&1 < /dev/null &
    BOT_PID=\$!
    disown \$BOT_PID
    echo \$BOT_PID > $REMOTE_BOT_DIR/account.pid
    echo \"PID=\$BOT_PID\"
"

for idx in "${ACCOUNT_IDXS[@]}"; do
    run_bg "$idx" "$LAUNCH_CMD"
done
wait

ok "$N_BOTS launch commands sent"
info "Waiting 30s — feed connection + stabilisation..."
sleep 30

# ─── Phase 8: Initial verification ────────────────────────────────────────────
section "PHASE 9 — INITIAL VERIFICATION"

# Feed service must be running (managed by systemd — not ps aux)
FEED_SVC_NOW=$(run "$FEED_IDX" \
    "systemctl is-active tradinebotte-feed 2>/dev/null || echo inactive")
[[ "$FEED_SVC_NOW" == "active" ]] && ok "Feed service: active (systemd)" \
    || err "Feed service: $FEED_SVC_NOW (expected: active)"

# Count account_bot processes (visible to all users via ps aux)
BOT_COUNT=$(run "${ACCOUNT_IDXS[0]}" "ps aux | grep '[a]ccount_bot.py' | wc -l" || echo 0)
info "account_bot processes: $BOT_COUNT (expected: $N_BOTS)"
[[ "$BOT_COUNT" -eq "$N_BOTS" ]] && ok "$N_BOTS account bots active" \
    || err "Expected $N_BOTS bots, found $BOT_COUNT"

# Verify shared indicators service (runs under feed owner, not per account)
if [[ -n "${IND_CFG_REL:-}" ]]; then
    IND_COUNT=$(run "$FEED_IDX" "ps aux | grep '[i]ndicators.py' | wc -l" || echo 0)
    [[ "$IND_COUNT" -ge 1 ]] \
        && ok "${ALL_USERS[$FEED_IDX]}: shared indicators.py active" \
        || err "${ALL_USERS[$FEED_IDX]}: indicators.py not running (config=$IND_CFG_REL)"
else
    info "Indicators service not configured — skipping check"
fi

# Verify each bot connected to the feed
for idx in "${ACCOUNT_IDXS[@]}"; do
    user="${ALL_USERS[$idx]}"
    if run "$idx" "grep -q 'Connected to feed' $REMOTE_BOT_DIR/account.log 2>/dev/null"; then
        ok "$user: connected to feed"
    else
        err "$user: 'Connected to feed' not found in log"
    fi
    # With feed_auto_start=false, no auto-start race; just verify no startup errors
    if run "$idx" "grep -qiE '\[ERROR\]|\[CRITICAL\]' $REMOTE_BOT_DIR/account.log 2>/dev/null"; then
        EARLY_ERR=$(run "$idx" \
            "grep -iE '\[ERROR\]|\[CRITICAL\]' $REMOTE_BOT_DIR/account.log 2>/dev/null | head -3")
        err "$user: errors at startup: $EARLY_ERR"
    fi
done

# ─── Phase 9: Sustained operation ──────────────────────────────────────────────
section "PHASE 10 — SUSTAINED OPERATION (${DURATION}s)"
ELAPSED=30
CHECK_INTERVAL=30

while [[ $ELAPSED -lt $DURATION ]]; do
    REMAINING=$((DURATION - ELAPSED))
    SLEEP_FOR=$CHECK_INTERVAL
    [[ $SLEEP_FOR -gt $REMAINING ]] && SLEEP_FOR=$REMAINING
    info "Heartbeat in ${SLEEP_FOR}s — time remaining: ${REMAINING}s"
    sleep $SLEEP_FOR
    ELAPSED=$((ELAPSED + SLEEP_FOR))

    FEED_C=$(run "$FEED_IDX" \
        "systemctl is-active tradinebotte-feed 2>/dev/null || echo inactive")
    BOT_C=$(run "${ACCOUNT_IDXS[0]}" \
        "ps aux | grep '[a]ccount_bot.py' | wc -l" 2>/dev/null || echo "?")
    info "  [${ELAPSED}s] feed=${FEED_C} bots=${BOT_C}"

    [[ "$FEED_C" == "active" ]] || warn "  ! Feed service not active: $FEED_C"
    [[ "$BOT_C"  == "$N_BOTS" ]] || warn "  ! Expected $N_BOTS bots, found: $BOT_C"
done
ok "Duration ${DURATION}s reached"

# ─── Phase 10: Final analysis ───────────────────────────────────────────────────
section "PHASE 11 — LOG ANALYSIS"

for idx in "${ACCOUNT_IDXS[@]}"; do
    user="${ALL_USERS[$idx]}"
    echo ""
    echo -e "${BOLD}--- $user: account.log (last 20 lines) ---${NC}"
    run "$idx" "tail -20 $REMOTE_BOT_DIR/account.log 2>/dev/null || echo '(empty)'"

    run "$idx" "grep -q 'Connected to feed' $REMOTE_BOT_DIR/account.log 2>/dev/null" && \
        ok "$user: feed connection confirmed" || err "$user: no feed connection"
    BOOK_COUNT=$(run "$idx" \
        "grep -c '\[FEED\] book' $REMOTE_BOT_DIR/account.log 2>/dev/null || true")
    [[ "$BOOK_COUNT" -gt 0 ]] && ok "$user: $BOOK_COUNT book updates received" \
        || warn "$user: no book updates — market may be quiet"
    ERROR_COUNT=$(run "$idx" \
        "grep -ciE '\[(ERROR|CRITICAL)\]' $REMOTE_BOT_DIR/account.log 2>/dev/null || true")
    [[ "$ERROR_COUNT" -eq 0 ]] && ok "$user: no critical errors" \
        || err "$user: $ERROR_COUNT ERROR/CRITICAL line(s) in logs"
done

# Indicators log (shared service under feed owner)
if [[ -n "${IND_CFG_REL:-}" ]]; then
    echo ""
    echo -e "${BOLD}--- Shared indicators.log (last 10 lines) ---${NC}"
    run "$FEED_IDX" "tail -10 $REMOTE_INSTALL_DIR/indicators.log 2>/dev/null || echo '(empty)'"
    IND_ERR=$(run "$FEED_IDX" \
        "grep -ciE '\[(ERROR|CRITICAL)\]' $REMOTE_INSTALL_DIR/indicators.log 2>/dev/null || true")
    [[ "$IND_ERR" -eq 0 ]] && ok "${ALL_USERS[$FEED_IDX]}: indicators — no critical errors" \
        || err "${ALL_USERS[$FEED_IDX]}: indicators — $IND_ERR ERROR/CRITICAL line(s)"
fi

# Feed service journal (last 30 lines)
echo ""
echo -e "${BOLD}--- Feed service journal (last 30 lines via journalctl) ---${NC}"
FEED_JOURNAL=$(run "$FEED_IDX" \
    "journalctl -u tradinebotte-feed --no-pager -n 30 2>/dev/null || echo '(not available)'")
echo "$FEED_JOURNAL"
echo "$FEED_JOURNAL" | grep -qE "WebSocket connected|Subscribing|BTC 5-min markets" && \
    ok "Feed: WebSocket confirmed in journal" || warn "Feed: WebSocket not confirmed in journal"

FEED_FINAL=$(run "$FEED_IDX" \
    "systemctl is-active tradinebotte-feed 2>/dev/null || echo inactive")
BOT_FINAL=$(run "${ACCOUNT_IDXS[0]}" \
    "ps aux | grep '[a]ccount_bot.py' | wc -l" || echo 0)
[[ "$FEED_FINAL" == "active" ]] && ok "Feed service still active after ${DURATION}s" \
    || err "Feed service stopped (status: $FEED_FINAL)"
[[ "$BOT_FINAL" -eq "$N_BOTS" ]] && ok "$N_BOTS bots still active after ${DURATION}s" \
    || err "$BOT_FINAL/$N_BOTS bots still active"

# ─── Phase 11: Teardown ────────────────────────────────────────────────────────
section "PHASE 12 — TEARDOWN"
info "Stopping account_bot processes (feed service left running)"

# Stop shared indicators.py under the feed owner
if [[ -n "${IND_CFG_REL:-}" ]]; then
    run "$FEED_IDX" "
        PID_FILE=$REMOTE_INSTALL_DIR/indicators.pid
        if [ -f \"\$PID_FILE\" ]; then
            PID=\$(cat \"\$PID_FILE\")
            kill -0 \"\$PID\" 2>/dev/null && kill \"\$PID\" 2>/dev/null || true
            rm -f \"\$PID_FILE\"
        fi
        sleep 2
        pkill -9 -u \$(id -u) -f '[i]ndicators.py' 2>/dev/null || true
        fuser -k 5559/tcp 2>/dev/null || true
        fuser -k 5561/tcp 2>/dev/null || true
        exit 0
    " && info "${ALL_USERS[$FEED_IDX]}: indicators.py stopped" || true
fi

for idx in "${ACCOUNT_IDXS[@]}"; do
    user="${ALL_USERS[$idx]}"
    run "$idx" "
        PID_FILE=$REMOTE_BOT_DIR/account.pid
        if [ -f \"\$PID_FILE\" ]; then
            PID=\$(cat \"\$PID_FILE\")
            kill -0 \"\$PID\" 2>/dev/null && kill \"\$PID\" 2>/dev/null || true
            rm -f \"\$PID_FILE\"
        fi
        sleep 2
        pkill -9 -u \$(id -u) -f '[a]ccount_bot.py' 2>/dev/null || true
        exit 0
    " && info "$user: processes stopped" || true
done
sleep 3
REMAINING_BOTS=$(run "${ACCOUNT_IDXS[0]}" \
    "ps aux | grep '[a]ccount_bot.py' | grep -v grep | wc -l" || echo 0)
[[ "$REMAINING_BOTS" -eq 0 ]] && ok "All account_bot processes stopped" \
    || warn "$REMAINING_BOTS account_bot process(es) still running"

# Confirm the feed service was not affected
FEED_AFTER=$(run "$FEED_IDX" \
    "systemctl is-active tradinebotte-feed 2>/dev/null || echo inactive")
[[ "$FEED_AFTER" == "active" ]] && ok "Feed service still running after teardown (as expected)" \
    || warn "Feed service status after teardown: $FEED_AFTER"

# ─── Phase 12: Final report ─────────────────────────────────────────────────────
section "FINAL REPORT"
TOTAL_SECS=$(( $(date +%s) - START_TS ))
echo ""
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${GREEN}${BOLD}  SUCCESS — All tests passed (total duration: ${TOTAL_SECS}s)${NC}"
    exit 0
else
    echo -e "${RED}${BOLD}  FAILURE — $FAILURES test(s) failed (total duration: ${TOTAL_SECS}s)${NC}"
    exit 1
fi
