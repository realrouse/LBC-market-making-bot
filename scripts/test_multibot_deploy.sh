#!/usr/bin/env bash
# test_multibot_deploy.sh — Clean install + integration test on configurable test accounts
#
# Phases:
#   1. Cleanup    — kill processes, remove directories, clear locks
#   2. Deploy     — rsync local repo + create venv + pip install (no root)
#   3. Prepare    — create simulation directories
#   4. Launch     — start all bots simultaneously (race condition test)
#   5. Check init — verify: 1 feed, N bots, connections established
#   6. Sustained  — heartbeat every 30s for DURATION seconds
#   7. Analysis   — collect and analyse logs, verify book updates
#   8. Teardown   — kill all processes
#   9. Report     — SUCCESS / FAILURE with error details
#
# Usage:
#   bash scripts/test_multibot_deploy.sh
#   bash scripts/test_multibot_deploy.sh --duration 300
#   bash scripts/test_multibot_deploy.sh --skip-deploy   # reuse existing install
#
# Configuration (required):
#   cp scripts/test_multibot.conf.example ~/.tradinebotte-test.conf
#   editor ~/.tradinebotte-test.conf   # fill in SERVER, PORT, USERS, PASSWORDS
#   # or: TEST_MULTIBOT_CONF=/path/to/conf bash scripts/test_multibot_deploy.sh
#
# Local prerequisites  : sshpass (apt-get install sshpass)
# Server prerequisites : python3-venv, python3-pip, python3.X-venv

set -euo pipefail

LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DURATION=180
SKIP_DEPLOY=false

# ─── Couleurs ──────────────────────────────────────────────────────────────────
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
            grep '^#' "${BASH_SOURCE[0]}" | head -25 | sed 's/^# \?//'
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
USERS=("${TEST_USERS[@]:?TEST_USERS missing in $CONF}")
PASSWORDS=("${TEST_PASSWORDS[@]:?TEST_PASSWORDS missing in $CONF}")
# Config can set a default duration; --duration flag takes precedence if already changed
[[ "$DURATION" -eq 180 && -n "${TEST_DURATION:-}" ]] && DURATION="$TEST_DURATION"
# Remote directories — override in conf via TEST_REMOTE_INSTALL_DIR / TEST_REMOTE_BOT_DIR
REMOTE_INSTALL_DIR="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"
REMOTE_BOT_DIR="${TEST_REMOTE_BOT_DIR:-~/account-sim}"

N_BOTS=${#USERS[@]}
if [[ "$N_BOTS" -ne ${#PASSWORDS[@]} ]]; then
    echo "ERROR: TEST_USERS and TEST_PASSWORDS must have the same length"
    exit 1
fi

# Per-account indicators config paths (relative to REMOTE_INSTALL_DIR). "" = skip.
INDICATORS_CONFIGS=("${TEST_INDICATORS_CONFIGS[@]:-}")
# Pad with empty strings if the conf omitted the array or it's shorter than USERS.
while [[ ${#INDICATORS_CONFIGS[@]} -lt $N_BOTS ]]; do
    INDICATORS_CONFIGS+=("")
done

# ─── SSH helpers ───────────────────────────────────────────────────────────────
run() {
    local idx="$1"; shift
    SSHPASS="${PASSWORDS[$idx]}" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -p "$PORT" "${USERS[$idx]}@$SERVER" "$@" 2>&1
}

run_bg() {
    local idx="$1"; shift
    SSHPASS="${PASSWORDS[$idx]}" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -p "$PORT" "${USERS[$idx]}@$SERVER" "$@" 2>&1 &
}

deploy_code() {
    local idx="$1"
    SSHPASS="${PASSWORDS[$idx]}" /usr/bin/sshpass -e \
        rsync -az --delete \
        --exclude='.git' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.venv' \
        --exclude='venv/' \
        -e "ssh -p $PORT -o StrictHostKeyChecking=yes" \
        "$LOCAL_REPO/" "${USERS[$idx]}@$SERVER:$REMOTE_INSTALL_DIR/" 2>&1
}

# ─── Pre-flight ────────────────────────────────────────────────────────────────
section "PRE-FLIGHT"

SSHPASS_BIN=$(command -v sshpass || echo "/usr/bin/sshpass")
if [[ ! -x "$SSHPASS_BIN" ]]; then
    echo "ERROR: sshpass not found. Install with: apt-get install sshpass"
    exit 1
fi
ok "sshpass: $SSHPASS_BIN"
ok "Config: $CONF"
info "Server: $SERVER:$PORT — $N_BOTS accounts: ${USERS[*]}"
info "Local repo: $LOCAL_REPO"
info "Test duration: ${DURATION}s"
[[ "$SKIP_DEPLOY" == "true" ]] && info "--skip-deploy mode: deployment skipped"

# Populate known_hosts so subsequent SSH calls can use StrictHostKeyChecking=yes
mkdir -p ~/.ssh && chmod 700 ~/.ssh
if ! ssh-keygen -F "[$SERVER]:$PORT" &>/dev/null && ! ssh-keygen -F "$SERVER" &>/dev/null; then
    info "Adding host key $SERVER:$PORT to known_hosts..."
    ssh-keyscan -p "$PORT" -H "$SERVER" >> ~/.ssh/known_hosts 2>/dev/null
fi
ok "Host key $SERVER verified in known_hosts"

for idx in "${!USERS[@]}"; do
    if run $idx "echo ok" &>/dev/null; then
        ok "SSH ${USERS[$idx]}@$SERVER:$PORT"
    else
        echo "ERROR: unable to connect to ${USERS[$idx]}@$SERVER:$PORT"
        exit 1
    fi
done

# ─── Phase 1: Cleanup ──────────────────────────────────────────────────────────
section "PHASE 1 — CLEANUP"
for idx in "${!USERS[@]}"; do
    user="${USERS[$idx]}"
    info "Cleaning up $user..."
    run $idx "
        pkill -f '[a]ccount_bot.py' 2>/dev/null || true
        pkill -f '[f]eed.py'        2>/dev/null || true
        pkill -f '[i]ndicators.py'  2>/dev/null || true
        sleep 3
        pkill -9 -f '[a]ccount_bot.py' 2>/dev/null || true
        pkill -9 -f '[f]eed.py'        2>/dev/null || true
        pkill -9 -f '[i]ndicators.py'  2>/dev/null || true
        fuser -k 5557/tcp 2>/dev/null || true
        fuser -k 5559/tcp 2>/dev/null || true
        fuser -k 5560/tcp 2>/dev/null || true
        rm -rf $REMOTE_INSTALL_DIR $REMOTE_BOT_DIR || true
        rm -rf /tmp/tradinebotte-feed || true
        exit 0
    " && ok "$user cleaned up" || warn "$user partial cleanup"
done

# Check from the first account (ps aux = all users)
STALE=$(run 0 "ps aux | grep -E '(account_bot|feed)\.py' | grep -v grep | wc -l" || echo 0)
if [[ "$STALE" -eq 0 ]]; then
    ok "No residual processes"
else
    warn "$STALE residual process(es) visible — continuing"
fi

# ─── Phase 2: Deploy ───────────────────────────────────────────────────────────
if [[ "$SKIP_DEPLOY" == "true" ]]; then
    section "PHASE 2 — DEPLOY (skipped — --skip-deploy)"
else
    section "PHASE 2 — DEPLOY"
    for idx in "${!USERS[@]}"; do
        user="${USERS[$idx]}"
        info "rsync → $user..."
        deploy_code $idx && ok "$user: rsync OK" || { err "$user: rsync failed"; exit 1; }

        info "Creating venv for $user..."
        run $idx "python3 -m venv $REMOTE_INSTALL_DIR/venv 2>&1" \
            && ok "$user: venv created" || { err "$user: venv creation failed"; exit 1; }

        info "pip install $user..."
        run $idx "
            $REMOTE_INSTALL_DIR/venv/bin/pip install --quiet --upgrade pip
            $REMOTE_INSTALL_DIR/venv/bin/pip install --quiet -r $REMOTE_INSTALL_DIR/requirements.txt
        " && ok "$user: dependencies installed" || { err "$user: pip install failed"; exit 1; }
    done
fi

# ─── Phase 3: Prepare simulation directories ───────────────────────────────────
section "PHASE 3 — SIMULATION DIRECTORIES"
for idx in "${!USERS[@]}"; do
    user="${USERS[$idx]}"
    run $idx "mkdir -p $REMOTE_BOT_DIR" && ok "$user: $REMOTE_BOT_DIR ready"
done

# ─── Phase 3b: Launch indicator services ───────────────────────────────────────
section "PHASE 3b — INDICATOR SERVICES"
IND_STARTED=0
for idx in "${!USERS[@]}"; do
    cfg="${INDICATORS_CONFIGS[$idx]:-}"
    [[ -z "$cfg" ]] && continue
    user="${USERS[$idx]}"
    info "Launching indicators.py for $user — config=$cfg"
    run $idx "
        cd $REMOTE_INSTALL_DIR
        nohup $REMOTE_INSTALL_DIR/venv/bin/python3 -u bot/indicators.py \
            --config $REMOTE_INSTALL_DIR/$cfg \
            > $REMOTE_BOT_DIR/indicators.log 2>&1 < /dev/null &
        echo \"IND_PID=\$!\"
    " && ok "$user: indicators.py started" || warn "$user: indicators launch failed"
    IND_STARTED=$((IND_STARTED + 1))
done
[[ $IND_STARTED -gt 0 ]] && { sleep 5; ok "$IND_STARTED indicator service(s) started"; } \
    || info "No indicator services configured"

# ─── Phase 4: Simultaneous launch ──────────────────────────────────────────────
section "PHASE 4 — SIMULTANEOUS LAUNCH OF $N_BOTS BOTS"
info "Sending $N_BOTS launch commands in parallel (race condition test)..."

LAUNCH_CMD="
    cd $REMOTE_INSTALL_DIR
    TRADINEBOTTE_DIR=$REMOTE_BOT_DIR \\
    nohup $REMOTE_INSTALL_DIR/venv/bin/python3 -u bot/account_bot.py --verbose \\
        > $REMOTE_BOT_DIR/account.log 2>&1 < /dev/null &
    echo \"PID=\$!\"
"

for idx in "${!USERS[@]}"; do
    run_bg $idx "$LAUNCH_CMD"
done
wait  # wait for the N SSH sessions to return (not for bots to finish)

ok "$N_BOTS launch commands sent"
info "Waiting 30s — feed auto-start + stabilisation..."
sleep 30

# ─── Phase 5: Initial verification ────────────────────────────────────────────
section "PHASE 5 — INITIAL VERIFICATION"

FEED_COUNT=$(run 0 "ps aux | grep '[f]eed.py' | wc -l" || echo 0)
BOT_COUNT=$( run 0 "ps aux | grep '[a]ccount_bot.py' | wc -l" || echo 0)
info "feed.py processes     : $FEED_COUNT (expected: 1)"
info "account_bot processes : $BOT_COUNT (expected: $N_BOTS)"

[[ "$FEED_COUNT" -eq 1 ]]       && ok "Single feed active" || err "Incorrect number of feeds: $FEED_COUNT (expected 1)"
[[ "$BOT_COUNT"  -eq "$N_BOTS" ]] && ok "$N_BOTS bots active" || err "Incorrect number of bots: $BOT_COUNT (expected $N_BOTS)"

# Verify indicator services
for idx in "${!USERS[@]}"; do
    cfg="${INDICATORS_CONFIGS[$idx]:-}"
    [[ -z "$cfg" ]] && continue
    user="${USERS[$idx]}"
    IND_COUNT=$(run $idx "ps aux | grep '[i]ndicators.py' | wc -l" || echo 0)
    [[ "$IND_COUNT" -ge 1 ]] && ok "$user: indicators.py active ($cfg)" \
        || err "$user: indicators.py not found for config $cfg"
done

for idx in "${!USERS[@]}"; do
    user="${USERS[$idx]}"
    run $idx "grep -q 'Connected to feed' $REMOTE_BOT_DIR/account.log 2>/dev/null" && \
        ok "$user: connected to feed" || err "$user: no 'Connected to feed' message"
    if run $idx "grep -qE 'Feed started|Feed ready' $REMOTE_BOT_DIR/account.log 2>/dev/null"; then
        ok "$user: started the feed (race winner)"
    elif run $idx "grep -q 'Feed active on' $REMOTE_BOT_DIR/account.log 2>/dev/null"; then
        ok "$user: found feed already active"
    elif run $idx "grep -q 'Feed being started' $REMOTE_BOT_DIR/account.log 2>/dev/null"; then
        ok "$user: waited for feed to start (race loser)"
    fi
done

# The feed was launched by the race winner — look for its log in the
# shared directory /tmp/tradinebotte-feed/ (common to all users).
FEED_LOG_PATH="(not found)"
FEED_LOG_IDX=0
for idx in "${!USERS[@]}"; do
    fp=$(run $idx "ls -t /tmp/tradinebotte-feed/feed-*.log 2>/dev/null | head -1")
    if [[ -n "$fp" ]]; then
        FEED_LOG_PATH="$fp"
        FEED_LOG_IDX=$idx
        break
    fi
done
info "Feed log: $FEED_LOG_PATH (account: ${USERS[$FEED_LOG_IDX]})"
if [[ "$FEED_LOG_PATH" != "(not found)" ]]; then
    FEED_LOG=$(run $FEED_LOG_IDX "cat $FEED_LOG_PATH 2>/dev/null | head -60 || echo '(empty)'")
    if echo "$FEED_LOG" | grep -qE "WebSocket connected|Subscribing|BTC 5-min markets"; then
        ok "Feed: Polymarket WebSocket connected"
    else
        warn "Feed: WebSocket confirmation not yet seen"
    fi
else
    warn "Feed log not found in /tmp/tradinebotte-feed/"
fi

# ─── Phase 6: Sustained operation ──────────────────────────────────────────────
section "PHASE 6 — SUSTAINED OPERATION (${DURATION}s)"
ELAPSED=30
CHECK_INTERVAL=30

while [[ $ELAPSED -lt $DURATION ]]; do
    REMAINING=$((DURATION - ELAPSED))
    SLEEP_FOR=$CHECK_INTERVAL
    [[ $SLEEP_FOR -gt $REMAINING ]] && SLEEP_FOR=$REMAINING
    info "Heartbeat in ${SLEEP_FOR}s — time remaining: ${REMAINING}s"
    sleep $SLEEP_FOR
    ELAPSED=$((ELAPSED + SLEEP_FOR))

    FEED_C=$(run 0 "ps aux | grep '[f]eed.py' | wc -l" 2>/dev/null || echo "?")
    BOT_C=$( run 0 "ps aux | grep '[a]ccount_bot.py' | wc -l" 2>/dev/null || echo "?")
    info "  [${ELAPSED}s] feed=$FEED_C bots=$BOT_C"

    [[ "$FEED_C" == "1" ]]        || warn "  ! Abnormal number of feeds: $FEED_C"
    [[ "$BOT_C"  == "$N_BOTS" ]]  || warn "  ! Abnormal number of bots: $BOT_C"
done
ok "Duration ${DURATION}s reached"

# ─── Phase 7: Final analysis ────────────────────────────────────────────────────
section "PHASE 7 — LOG ANALYSIS"

for idx in "${!USERS[@]}"; do
    user="${USERS[$idx]}"
    echo ""
    echo -e "${BOLD}--- $user: account.log (last 20 lines) ---${NC}"
    run $idx "tail -20 $REMOTE_BOT_DIR/account.log 2>/dev/null || echo '(empty)'"

    run $idx "grep -q 'Connected to feed' $REMOTE_BOT_DIR/account.log 2>/dev/null" && \
        ok "$user: feed connection confirmed" || err "$user: no feed connection"
    BOOK_COUNT=$(run $idx "grep -c '\[FEED\] book' $REMOTE_BOT_DIR/account.log 2>/dev/null || true")
    [[ "$BOOK_COUNT" -gt 0 ]] && ok "$user: $BOOK_COUNT book updates received (feed active)" || \
        warn "$user: no book updates — market may be quiet"
    ERROR_COUNT=$(run $idx "grep -ciE '\[(ERROR|CRITICAL)\]' $REMOTE_BOT_DIR/account.log 2>/dev/null || true")
    [[ "$ERROR_COUNT" -eq 0 ]] && ok "$user: no critical errors" || \
        err "$user: $ERROR_COUNT ERROR/CRITICAL line(s) in logs"
done

echo ""
echo -e "${BOLD}--- Feed log (first 30 + last 10 lines) ---${NC}"
if [[ "${FEED_LOG_PATH:-}" != "(not found)" && -n "${FEED_LOG_PATH:-}" ]]; then
    FEED_LOG_HEAD=$(run $FEED_LOG_IDX "head -30 $FEED_LOG_PATH 2>/dev/null || echo '(empty)'")
    FEED_LOG_TAIL=$(run $FEED_LOG_IDX "tail -10 $FEED_LOG_PATH 2>/dev/null || echo '(empty)'")
    echo "$FEED_LOG_HEAD"
    echo "..."
    echo "$FEED_LOG_TAIL"
    # Grep on the server to avoid transferring a multi-MB log
    run $FEED_LOG_IDX "grep -qE 'WebSocket connected|Subscribing|BTC 5-min markets' $FEED_LOG_PATH 2>/dev/null" && \
        ok "Feed: WebSocket confirmed in final log" || err "Feed: WebSocket not confirmed"
    run $FEED_LOG_IDX "grep -qiE 'BTC|bitcoin|Marche' $FEED_LOG_PATH 2>/dev/null" && \
        ok "Feed: BTC markets found" || warn "Feed: BTC markets not found"
else
    warn "Feed log not found — cannot analyse"
fi

FEED_FINAL=$(run 0 "ps aux | grep '[f]eed.py' | wc -l" || echo 0)
BOT_FINAL=$( run 0 "ps aux | grep '[a]ccount_bot.py' | wc -l" || echo 0)
[[ "$FEED_FINAL" -eq 1 ]]        && ok "Feed still active after ${DURATION}s" || err "Feed stopped prematurely"
[[ "$BOT_FINAL"  -eq "$N_BOTS" ]] && ok "$N_BOTS bots still active after ${DURATION}s" || \
    err "$BOT_FINAL/$N_BOTS bots still active"

# ─── Phase 8 : Teardown ────────────────────────────────────────────────────────
section "PHASE 8 — TEARDOWN"
for idx in "${!USERS[@]}"; do
    user="${USERS[$idx]}"
    run $idx "
        pkill -f '[a]ccount_bot.py' 2>/dev/null || true
        pkill -f '[f]eed.py'        2>/dev/null || true
        pkill -f '[i]ndicators.py'  2>/dev/null || true
        sleep 2
        pkill -9 -f '[a]ccount_bot.py' 2>/dev/null || true
        pkill -9 -f '[f]eed.py'        2>/dev/null || true
        pkill -9 -f '[i]ndicators.py'  2>/dev/null || true
        fuser -k 5557/tcp 2>/dev/null || true
        fuser -k 5559/tcp 2>/dev/null || true
        fuser -k 5560/tcp 2>/dev/null || true
        rm -rf /tmp/tradinebotte-feed || true
        exit 0
    " && info "$user : processes stopped" || true
done
sleep 3
REMAINING_PROCS=$(run 0 "ps aux | grep -E '(account_bot|feed)\.py' | grep -v grep | wc -l" || echo 0)
[[ "$REMAINING_PROCS" -eq 0 ]] && ok "All processes stopped" || \
    warn "$REMAINING_PROCS process(es) still running"

# ─── Rapport final ─────────────────────────────────────────────────────────────
section "RAPPORT FINAL"
TOTAL_SECS=$(( $(date +%s) - START_TS ))
echo ""
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${GREEN}${BOLD}  SUCCESS — All tests passed (total duration: ${TOTAL_SECS}s)${NC}"
    exit 0
else
    echo -e "${RED}${BOLD}  FAILURE — $FAILURES test(s) failed (total duration: ${TOTAL_SECS}s)${NC}"
    exit 1
fi
