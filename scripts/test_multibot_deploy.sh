#!/usr/bin/env bash
# test_multibot_deploy.sh — Multibot integration test (account_bot + shared feed)
#
# Architecture under test:
#   Feed owner  (TEST_FEED_USER_IDX)      — feed.py as an ephemeral nohup process
#   Account bots (TEST_ACCOUNT_USER_IDXS) — account_bot.py, feed_auto_start=false
#
# The feed is run as a plain nohup process (not a systemd service) and everything is
# deleted at teardown, so the test account holds NO persistent state — it is only for
# short-lived clean-install tests.
#
# Phases:
#   1. Pre-flight  — sshpass, SSH connectivity, host-key scan
#   2. Cleanup     — kill stale account_bot/indicators/feed, wipe REMOTE_BOT_DIR
#   3. Feed check  — capture feed.py hash before any deploy
#   4. Deploy      — rsync code + venv + pip + tradinetools to all users
#   5. Start feed  — launch feed.py (nohup) and wait until it publishes
#   6. Configure   — write config.json (feed_addr, feed_auto_start=false)
#   7. Indicators  — start optional indicators services
#   8. Launch      — start account_bot instances simultaneously
#   9. Verify init — feed running + N account bots connected
#  10. Sustained   — heartbeat every 30s for DURATION seconds
#  11. Analysis    — collect and analyse logs
#  12. Teardown    — stop bots + feed, wipe the test account clean
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
# Server prerequisites : python3-venv, python3-pip, python3.X-venv
#                        (the feed is started by the test — nothing to pre-install)

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

# Role indices — default to the dedicated test account so a minimal conf that only
# sets TEST_STANDALONE_USER_IDX never targets production.
_DEFAULT_IDX="${TEST_STANDALONE_USER_IDX:-0}"
FEED_IDX="${TEST_FEED_USER_IDX:-$_DEFAULT_IDX}"
if [[ -n "${TEST_ACCOUNT_USER_IDXS+_}" ]]; then
    ACCOUNT_IDXS=("${TEST_ACCOUNT_USER_IDXS[@]}")
else
    ACCOUNT_IDXS=("$_DEFAULT_IDX")
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

# Fail-closed guard: this test deploys + tears down account_bot processes, so it MUST
# only run on the dedicated test account (TEST_STANDALONE_USER_IDX). Refuse if any
# resolved index points elsewhere (e.g. a default of 0 → production). Mirrors the
# control-plane sim-only guard. Override only for a deliberate non-prod multi-account
# setup via TEST_ALLOW_NONTEST_ACCOUNTS=true.
if [[ "${TEST_ALLOW_NONTEST_ACCOUNTS:-false}" != "true" ]]; then
    if [[ -z "${TEST_STANDALONE_USER_IDX:-}" ]]; then
        echo "REFUSED: TEST_STANDALONE_USER_IDX unset — cannot confirm a dedicated test account." >&2
        echo "  Set it (and TEST_FEED_USER_IDX/TEST_ACCOUNT_USER_IDXS) to the test account index in $CONF." >&2
        exit 1
    fi
    for _i in "${DEPLOY_IDXS[@]}"; do
        if [[ "$_i" != "$TEST_STANDALONE_USER_IDX" ]]; then
            echo "REFUSED: integration test would target account index $_i, not the dedicated" >&2
            echo "  test account (index $TEST_STANDALONE_USER_IDX). This test must never touch production." >&2
            echo "  Fix TEST_FEED_USER_IDX / TEST_ACCOUNT_USER_IDXS in $CONF," >&2
            echo "  or set TEST_ALLOW_NONTEST_ACCOUNTS=true for a deliberate non-prod multi-account run." >&2
            exit 1
        fi
    done
    unset _i
fi

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

# Feed liveness — the test runs the feed as an ephemeral nohup process (no systemd /
# linger, so the test account holds no persistent state). uid-scoped so it never
# matches a production feed.py running under another account on this shared host.
feed_active() {  # echoes "active" or "inactive"
    run "$FEED_IDX" "ps -u \$(id -u) -o args= | grep -q '[f]eed.py' && echo active || echo inactive"
}

deploy_code() {
    # Flat rsync: tradinebotte-polymarket/ + tradinebotte-cex/ → $REMOTE_INSTALL_DIR/
    # indicators.py lives in tradinebotte-indicators/ and is also synced flat.
    # tradinebotte-cex/ Python files (strategy_engines, connectors, etc.) also synced flat.
    # Never use --delete on the full repo: it wipes flat live_bot.py on standalone accounts.
    local idx="$1"
    local user="${ALL_USERS[$idx]}"
    local ssh_opts="-p $PORT -o StrictHostKeyChecking=yes"

    SSHPASS="${ALL_PASSWORDS[$idx]}" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='config.json' --exclude='*.db' --exclude='*.log' \
        --exclude='scripts' --exclude='tests' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-polymarket/" "$user@$SERVER:$REMOTE_INSTALL_DIR/" 2>&1 || return 1

    SSHPASS="${ALL_PASSWORDS[$idx]}" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-indicators/indicators.py" \
        "$user@$SERVER:$REMOTE_INSTALL_DIR/indicators.py" 2>&1 || return 1

    SSHPASS="${ALL_PASSWORDS[$idx]}" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='scripts' --exclude='tests' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-cex/" "$user@$SERVER:$REMOTE_INSTALL_DIR/" 2>&1 || return 1

    SSHPASS="${ALL_PASSWORDS[$idx]}" /usr/bin/sshpass -e \
        rsync -az \
        --filter='+ **/' --filter='+ *.json' --filter='- *' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-polymarket/strategies/" "$user@$SERVER:$REMOTE_INSTALL_DIR/strategies/" 2>&1 || return 1

    SSHPASS="${ALL_PASSWORDS[$idx]}" /usr/bin/sshpass -e \
        rsync -az \
        --filter='+ **/' --filter='+ *.json' --filter='- *' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-cex/strategies/" \
        "$user@$SERVER:$REMOTE_INSTALL_DIR/strategies/" 2>&1 || return 1

    SSHPASS="${ALL_PASSWORDS[$idx]}" /usr/bin/sshpass -e \
        rsync -az \
        --filter='+ **/' --filter='+ *.json' --filter='- *' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinebotte-indicators/strategies/" \
        "$user@$SERVER:$REMOTE_INSTALL_DIR/strategies/indicators/" 2>&1 || return 1

    SSHPASS="${ALL_PASSWORDS[$idx]}" /usr/bin/sshpass -e \
        rsync -az \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/requirements.txt" "$user@$SERVER:$REMOTE_INSTALL_DIR/" 2>&1 || return 1

    # tradinetools source — not on PyPI, installed into the venv in Phase 4.
    SSHPASS="${ALL_PASSWORDS[$idx]}" /usr/bin/sshpass -e \
        rsync -az --exclude='__pycache__' --exclude='*.pyc' \
        -e "ssh $ssh_opts" \
        "$LOCAL_REPO/tradinetools/" "$user@$SERVER:$REMOTE_INSTALL_DIR/tradinetools/" 2>&1 || return 1
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

# The feed is started by this test in Phase 5 as a nohup process — no pre-provisioned
# service required (the test account is ephemeral). Just note where it will bind.
info "Feed will be started in Phase 5 (nohup, on $FEED_ADDR)"

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
    "ps -u \$(id -u) -o args= | grep '[a]ccount_bot.py' | wc -l" || echo 0)
[[ "$STALE" -eq 0 ]] && ok "No residual account_bot processes" \
    || warn "$STALE residual account_bot process(es) — continuing"

# ─── Phase 3: Capture current feed.py hash BEFORE any deploy ───────────────────
section "PHASE 3 — FEED VERSION CHECK"

# Informational only — Phase 5 always (re)installs the feed with the deployed code.
LOCAL_FEED_HASH=$(sha256sum "$LOCAL_REPO/tradinebotte-polymarket/feed.py" | cut -d' ' -f1)
REMOTE_FEED_HASH=$(run "$FEED_IDX" \
    "sha256sum $REMOTE_INSTALL_DIR/feed.py 2>/dev/null | cut -d' ' -f1" || echo "")

if [[ -z "$REMOTE_FEED_HASH" ]]; then
    info "feed.py not yet deployed on ${ALL_USERS[$FEED_IDX]} — first deploy"
elif [[ "$REMOTE_FEED_HASH" == "$LOCAL_FEED_HASH" ]]; then
    ok "feed.py unchanged (hash: ${LOCAL_FEED_HASH:0:12}…)"
else
    warn "feed.py changed (remote: ${REMOTE_FEED_HASH:0:12}… → local: ${LOCAL_FEED_HASH:0:12}…)"
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
        run "$idx" "python3 -m venv $REMOTE_INSTALL_DIR/.venv 2>&1" \
            && ok "$user: venv created" || { err "$user: venv creation failed"; exit 1; }

        info "pip install $user..."
        run "$idx" "
            $REMOTE_INSTALL_DIR/.venv/bin/pip install --quiet --upgrade pip
            $REMOTE_INSTALL_DIR/.venv/bin/pip install --quiet -r $REMOTE_INSTALL_DIR/requirements.txt
        " && ok "$user: dependencies installed" || { err "$user: pip install failed"; exit 1; }

        # tradinetools isn't on PyPI / in requirements.txt — install it into the venv
        # so account_bot's 'from tradinetools import heartbeat_loop' resolves to a real
        # package instead of the source namespace dir on cwd. Plain copy (not pip) to
        # avoid stale dist-info breaking restarts (see feedback_integration_test_tradinetools).
        info "install tradinetools into venv ($user)..."
        run "$idx" "
            VENV=$REMOTE_INSTALL_DIR/.venv
            PYVER=\$(\$VENV/bin/python3 -c 'import sys;print(f\"{sys.version_info.major}.{sys.version_info.minor}\")')
            SITE=\$VENV/lib/python\$PYVER/site-packages
            rm -rf \"\$SITE/tradinetools\"
            cp -r $REMOTE_INSTALL_DIR/tradinetools/tradinetools \"\$SITE/tradinetools\"
            \$VENV/bin/python3 -c 'from tradinetools import heartbeat_loop'
        " && ok "$user: tradinetools installed" || { err "$user: tradinetools install failed"; exit 1; }
    done
fi

# ─── Phase 5: Start the feed (ephemeral nohup process) ────────────────────────
section "PHASE 5 — START FEED"

# Run the feed from the same venv the test just built (which now has tradinetools),
# as a nohup background process — no systemd / linger, so nothing persists on the
# test account. It binds the dedicated test port and publishes book updates that the
# account bots consume.
# Kill any stale feed first (uid-scoped), then launch fresh.
run "$FEED_IDX" "pkill -9 -u \$(id -u) -f '[f]eed.py' 2>/dev/null; sleep 1; exit 0" || true
run_bg "$FEED_IDX" "
    cd $REMOTE_INSTALL_DIR
    TRADINEBOTTE_FEED_ADDR=$FEED_ADDR TRADINEBOTTE_DIR=$REMOTE_INSTALL_DIR \\
    nohup $REMOTE_INSTALL_DIR/.venv/bin/python3 -u feed.py \\
        > $REMOTE_INSTALL_DIR/feed.log 2>&1 < /dev/null &
    FEED_PID=\$!
    disown \$FEED_PID
    echo \$FEED_PID > $REMOTE_INSTALL_DIR/feed.pid
    echo \"FEED_PID=\$FEED_PID\"
"
wait
ok "feed.py launched on ${ALL_USERS[$FEED_IDX]} ($FEED_ADDR)"

# Wait for the feed to actually start publishing — a cold feed needs to fetch markets
# and connect its upstream WebSocket (~10–20s) before book updates flow. Gate the
# account_bot launch on this so the test isn't flaky on cold start.
info "Waiting for feed to connect + publish (up to 60s)..."
FEED_READY=false
for _i in $(seq 1 20); do
    if run "$FEED_IDX" "grep -q 'WebSocket connected' $REMOTE_INSTALL_DIR/feed.log 2>/dev/null && echo y" | grep -q y; then
        FEED_READY=true
        break
    fi
    sleep 3
done
if [[ "$FEED_READY" == "true" ]]; then
    ok "Feed connected and broadcasting on $FEED_ADDR"
else
    err "Feed did not start publishing within 60s — see $REMOTE_INSTALL_DIR/feed.log"
fi

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
        nohup $REMOTE_INSTALL_DIR/.venv/bin/python3 -u indicators.py \
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
    nohup $REMOTE_INSTALL_DIR/.venv/bin/python3 -u account_bot.py --verbose \\
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

# Feed process must be running (ephemeral nohup process, uid-scoped check)
FEED_SVC_NOW=$(feed_active)
[[ "$FEED_SVC_NOW" == "active" ]] && ok "Feed process: active" \
    || err "Feed process: $FEED_SVC_NOW (expected: active)"

# Count account_bot processes — scoped to the test user's uid, since on this shared
# host `ps aux` would also count production account_bot.py running under other accounts.
BOT_COUNT=$(run "${ACCOUNT_IDXS[0]}" "ps -u \$(id -u) -o args= | grep '[a]ccount_bot.py' | wc -l" || echo 0)
info "account_bot processes: $BOT_COUNT (expected: $N_BOTS)"
[[ "$BOT_COUNT" -eq "$N_BOTS" ]] && ok "$N_BOTS account bots active" \
    || err "Expected $N_BOTS bots, found $BOT_COUNT"

# Verify shared indicators service (runs under feed owner, not per account)
if [[ -n "${IND_CFG_REL:-}" ]]; then
    IND_COUNT=$(run "$FEED_IDX" "ps -u \$(id -u) -o args= | grep '[i]ndicators.py' | wc -l" || echo 0)
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

    FEED_C=$(feed_active)
    BOT_C=$(run "${ACCOUNT_IDXS[0]}" \
        "ps -u \$(id -u) -o args= | grep '[a]ccount_bot.py' | wc -l" 2>/dev/null || echo "?")
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

# Feed log (last 15 lines) — the nohup feed logs to feed.log, not journald.
echo ""
echo -e "${BOLD}--- Feed log (last 15 lines: $REMOTE_INSTALL_DIR/feed.log) ---${NC}"
FEED_LOG=$(run "$FEED_IDX" \
    "tail -n 15 $REMOTE_INSTALL_DIR/feed.log 2>/dev/null || echo '(not available)'")
echo "$FEED_LOG"
echo "$FEED_LOG" | grep -qE "WebSocket connected|Subscribing|BTC .* markets" && \
    ok "Feed: WebSocket confirmed in log" || warn "Feed: WebSocket not confirmed in log"

FEED_FINAL=$(feed_active)
BOT_FINAL=$(run "${ACCOUNT_IDXS[0]}" \
    "ps -u \$(id -u) -o args= | grep '[a]ccount_bot.py' | wc -l" || echo 0)
[[ "$FEED_FINAL" == "active" ]] && ok "Feed still active after ${DURATION}s" \
    || err "Feed stopped (status: $FEED_FINAL)"
[[ "$BOT_FINAL" -eq "$N_BOTS" ]] && ok "$N_BOTS bots still active after ${DURATION}s" \
    || err "$BOT_FINAL/$N_BOTS bots still active"

# ─── Phase 12: Teardown ────────────────────────────────────────────────────────
section "PHASE 12 — TEARDOWN"
# The test account is only for short-lived clean-install tests — it must hold no
# persistent state. Stop the bots AND the feed, then wipe everything this test left.
info "Stopping bots + feed and wiping the test account (no persistent state)"

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
    "ps -u \$(id -u) -o args= | grep '[a]ccount_bot.py' | grep -v grep | wc -l" || echo 0)
[[ "$REMAINING_BOTS" -eq 0 ]] && ok "All account_bot processes stopped" \
    || warn "$REMAINING_BOTS account_bot process(es) still running"

# Kill the nohup feed; also defensively remove any legacy systemd feed unit + linger
# left by older runs, so the test account never accumulates persistent state.
run "$FEED_IDX" "
    export XDG_RUNTIME_DIR=/run/user/\$(id -u)
    pkill -9 -u \$(id -u) -f '[f]eed.py' 2>/dev/null || true
    systemctl --user stop tradinebotte-feed.service 2>/dev/null || true
    systemctl --user disable tradinebotte-feed.service 2>/dev/null || true
    rm -f \$HOME/.config/systemd/user/tradinebotte-feed.service
    systemctl --user daemon-reload 2>/dev/null || true
    systemctl --user reset-failed 2>/dev/null || true
    loginctl disable-linger \$(whoami) 2>/dev/null || true
    exit 0
" && info "${ALL_USERS[$FEED_IDX]}: feed stopped (+ legacy unit/linger cleared)" || true

# Delete the install + test directories on every account the test deployed to.
for idx in "${DEPLOY_IDXS[@]}"; do
    run "$idx" "rm -rf $REMOTE_INSTALL_DIR $REMOTE_BOT_DIR \$HOME/feed.log 2>/dev/null; exit 0" \
        && info "${ALL_USERS[$idx]}: install + test dirs deleted" || true
done

# Best-effort: purge this account's heartbeats from the shared state DB so it does
# not linger as DEAD on the status page (runs on the deployer host; needs sg claudes).
_SHARED_DB="${TRADINEBOTTE_DB:-/data1/tradinebotte-shared/database/tradinebotte.db}"
if command -v sqlite3 >/dev/null 2>&1 && [[ -f "$_SHARED_DB" ]]; then
    for idx in "$FEED_IDX" "${ACCOUNT_IDXS[@]}"; do
        sg claudes -c "sqlite3 '$_SHARED_DB' \"DELETE FROM heartbeats WHERE account='${ALL_USERS[$idx]}';\"" 2>/dev/null || true
    done
    info "shared-DB heartbeats purged for the test account"
fi

# Verify the account is clean.
CLEAN_LEFT=$(run "$FEED_IDX" "
    export XDG_RUNTIME_DIR=/run/user/\$(id -u)
    n=0
    ls -d $REMOTE_INSTALL_DIR $REMOTE_BOT_DIR >/dev/null 2>&1 && n=\$((n+1))
    [ -f \$HOME/.config/systemd/user/tradinebotte-feed.service ] && n=\$((n+1))
    ps -u \$(id -u) -o args= | grep -qE '[f]eed.py|[a]ccount_bot.py' && n=\$((n+1))
    echo \$n
" || echo "?")
[[ "$CLEAN_LEFT" == "0" ]] && ok "Test account wiped clean (no dirs, unit, or processes)" \
    || warn "Test account not fully clean (residual classes: $CLEAN_LEFT)"

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
