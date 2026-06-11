#!/usr/bin/env bash
# cleanup_server.sh — Remove stale files from all deployment accounts.
#
# Run phases selectively with --phase=N (default: all phases 2-6).
# Phase 1 (system services) requires root — commands are printed, not executed.
#
# Usage:
#   bash scripts/cleanup_server.sh             # all user-space phases
#   bash scripts/cleanup_server.sh --phase=2   # single phase
#   bash scripts/cleanup_server.sh --dry-run   # print what would be deleted

set -uo pipefail

CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
[[ -f "$CONF" ]] || { echo "Missing: $CONF"; exit 1; }
# shellcheck source=/dev/null
source "$CONF"

PORT="${TEST_PORT:-22}"
SERVER="${TEST_SERVER:?}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
section() { echo -e "\n${BOLD}${YELLOW}═══ $* ═══${NC}"; }
ok()      { echo -e "${GREEN}  ✓ $*${NC}"; }
warn()    { echo -e "${YELLOW}  ! $*${NC}"; }
info()    { echo -e "${BLUE}  → $*${NC}"; }

DRY_RUN=false
RUN_PHASES=()

for arg in "$@"; do
    case "$arg" in
        --dry-run)    DRY_RUN=true ;;
        --phase=*)    RUN_PHASES+=("${arg#--phase=}") ;;
        *) echo "Unknown argument: $arg"; exit 1 ;;
    esac
done

[[ ${#RUN_PHASES[@]} -eq 0 ]] && RUN_PHASES=(2 3 4 5 6)

should_run() { local p="$1"; for x in "${RUN_PHASES[@]}"; do [[ "$x" == "$p" ]] && return 0; done; return 1; }

_ssh() {
    local user="$1" pass="$2"; shift 2
    SSHPASS="$pass" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
        -o PreferredAuthentications=password \
        -p "$PORT" "$user@$SERVER" "$@" 2>&1
}

_rm() {
    # Remote rm wrapper — dry-run safe
    local user="$1" pass="$2" label="$3"; shift 3
    local cmd="$*"
    if [[ "$DRY_RUN" == "true" ]]; then
        echo "  [DRY-RUN] $label"
        return 0
    fi
    local out
    out=$(_ssh "$user" "$pass" "$cmd")
    echo "  $out" 2>/dev/null || true
}

# ─── Phase 1: Root commands — printed only, never auto-executed ─────────────────
section "PHASE 1 — System services (root required — print only)"
echo ""
echo "  3 old services are still ENABLED and will try to start at next server reboot."
echo "  Run the following as root on the server:"
echo ""
echo "  # Disable the 3 still-enabled services"
echo "  systemctl disable tradinebotte-accumulation-account2.service \\"
echo "                    tradinebotte-accumulation-account3.service \\"
echo "                    tradinebotte-orderbook-account3.service"
echo ""
echo "  # Remove all 9 old unit files"
OLD_SVCS=(
    tradinebotte-account-tradinebotte.service
    tradinebotte-accumulation-account2.service
    tradinebotte-accumulation-account3.service
    tradinebotte-feed.service
    tradinebotte-indicators.service
    tradinebotte-live-account1.service
    tradinebotte-live-account2.service
    tradinebotte-live-account4.service
    tradinebotte-orderbook-account3.service
)
for svc in "${OLD_SVCS[@]}"; do
    echo "  rm /etc/systemd/system/$svc"
done
echo ""
echo "  systemctl daemon-reload"
echo ""
warn "Run these as root BEFORE the next server reboot."

# ─── Phase 2: Duplicate venvs (accounts 0, 1, 2) ───────────────────────────────
if should_run 2; then
    section "PHASE 2 — Duplicate venvs (accounts 0, 1, 2)"
    info "Keeping .venv (used by running services). Removing unused venv/."
    for IDX in 0 1 2; do
        USER="${TEST_USERS[$IDX]}"
        PASS="${TEST_PASSWORDS[$IDX]}"
        info "[$USER] checking..."
        SIZE=$(_ssh "$USER" "$PASS" 'du -sh ~/tradinebotte/venv 2>/dev/null || echo "absent"')
        echo "  venv size: $SIZE"
        if echo "$SIZE" | grep -v "absent" | grep -q '.'; then
            _rm "$USER" "$PASS" "rm -rf ~/tradinebotte/venv" \
                'rm -rf ~/tradinebotte/venv && echo "venv removed"' \
            && ok "$USER: venv removed" || warn "$USER: rm failed"
        else
            ok "$USER: no duplicate venv"
        fi
    done
fi

# ─── Phase 3: Git-clone residues + docs + notes (accounts 0, 1, 2) ─────────────
if should_run 3; then
    section "PHASE 3 — Git-clone residues, docs, notes (accounts 0, 1, 2)"

    DIRS_TO_RM=(
        scripts tests docs .github .git-hooks
        .mypy_cache .pytest_cache reports
    )
    DOC_FILES=(
        README.md README.fr.md CHANGELOG.md CHANGELOG.fr.md
        INSTALL.md INSTALL.fr.md QUICKSTART.md QUICKSTART.fr.md
        UPDATE.md UPDATE.fr.md CLAUDE.md LICENSE TODO.md
        requirements-dev.txt config.json.example .coverage .pylintrc
    )
    NOTE_FILES=(
        ameliorationarchitecture.txt ameliorationtests.txt audispeed.txt
        audit170526.md backpnl080526explain.txt backpnl080526.txt
        backtest_05052026.txt latence_api.txt rust.txt semaineperdante.txt
        semaineperdante_volfilter.txt testcompare.txt usserver.txt volstop.txt
    )

    for IDX in 0 1 2; do
        USER="${TEST_USERS[$IDX]}"
        PASS="${TEST_PASSWORDS[$IDX]}"
        info "[$USER] removing git-clone residues..."

        DIR_LIST=""
        for d in "${DIRS_TO_RM[@]}"; do DIR_LIST+=" ~/tradinebotte/$d"; done

        DOC_LIST=""
        for f in "${DOC_FILES[@]}"; do DOC_LIST+=" ~/tradinebotte/$f"; done

        NOTE_LIST=""
        for f in "${NOTE_FILES[@]}"; do NOTE_LIST+=" ~/tradinebotte/$f"; done

        if [[ "$DRY_RUN" == "true" ]]; then
            echo "  [DRY-RUN] rm -rf $DIR_LIST"
            echo "  [DRY-RUN] rm -f $DOC_LIST"
            echo "  [DRY-RUN] rm -f $NOTE_LIST"
        else
            _ssh "$USER" "$PASS" "rm -rf $DIR_LIST 2>/dev/null; echo 'dirs done'" || true
            _ssh "$USER" "$PASS" "rm -f $DOC_LIST 2>/dev/null; echo 'docs done'" || true
            _ssh "$USER" "$PASS" "rm -f $NOTE_LIST 2>/dev/null; echo 'notes done'" || true
            ok "$USER: done"
        fi
    done

    # account-0-specific extras
    USER="${TEST_USERS[0]}"; PASS="${TEST_PASSWORDS[0]}"
    info "[${USER}] removing account-0-specific residues..."
    _ssh "$USER" "$PASS" 'rm -f ~/tradinebotte/restartfeeds 2>/dev/null; echo ok' || true

    # account-2-specific extras
    USER="${TEST_USERS[2]}"; PASS="${TEST_PASSWORDS[2]}"
    info "[${USER}] removing account-2-specific residues..."
    _ssh "$USER" "$PASS" 'rm -f ~/tradinebotte/run.sh ~/tradinebotte/live_restart.log 2>/dev/null; echo ok' || true
fi

# ─── Phase 4: Old flat strategy files (all accounts) ────────────────────────────
if should_run 4; then
    section "PHASE 4 — Old flat strategy files (all accounts)"
    info "Keeping subdir versions (strategies/grid/, strategies/indicators/, etc.)"
    info "Removing duplicate flat versions in strategies/ root + Python strategy modules"

    FLAT_STRATEGIES=(
        'strategies/grid_BTCUSDT*.json'
        'strategies/indicators_*.json'
        'strategies/indicators.json'
        'strategies/longtermcyclestrategy*.json'
        'strategies/orderbook_btc.json'
        'strategies/polymarket_BTC*.json'
        'strategies/scalping_*.json'
        'strategies/base.py'
        'strategies/grid.py'
        'strategies/__init__.py'
    )

    # Also: very old flat JSONs directly in ~/tradinebotte/ root (account-2 only)
    FLAT_ROOT_CLAUDE3=(
        'grid_BTCUSDT_bear_trailing.json' 'grid_BTCUSDT_bull_trailing.json'
        'grid_BTCUSDT.json' 'grid_BTCUSDT_moderate.json' 'grid_BTCUSDT_tight.json'
        'indicators_1d_bitcoin.json' 'indicators_4h_bitcoin.json'
        'indicators_deribit_iv_bitcoin.json' 'indicators_fear_greed.json'
        'indicators_funding_bitcoin.json' 'indicators.json'
        'indicators_liquidations_bitcoin.json' 'indicators_ls_ratio_bitcoin.json'
        'indicators_oi_bitcoin.json' 'polymarket_BTC15M_piste3.json'
        'polymarket_BTC5M.json' 'polymarket_BTC5M_piste3.json'
    )

    NUM_USERS=${#TEST_USERS[@]}
    for ((i=0; i<NUM_USERS; i++)); do
        USER="${TEST_USERS[$i]}"
        PASS="${TEST_PASSWORDS[$i]}"
        info "[$USER] removing flat strategies..."

        RM_CMD="cd ~/tradinebotte"
        for pattern in "${FLAT_STRATEGIES[@]}"; do
            RM_CMD+=" && rm -f $pattern 2>/dev/null"
        done
        RM_CMD+=" && rm -rf strategies/__pycache__ 2>/dev/null && echo 'flat strategies removed'"

        if [[ "$DRY_RUN" == "true" ]]; then
            echo "  [DRY-RUN] $RM_CMD"
        else
            _ssh "$USER" "$PASS" "$RM_CMD" || true
            ok "$USER: done"
        fi
    done

    # account-2 extra: flat JSONs in install root
    USER="${TEST_USERS[2]}"; PASS="${TEST_PASSWORDS[2]}"
    info "[${USER}] removing old flat JSONs from install root..."
    RM_FLAT=""
    for f in "${FLAT_ROOT_CLAUDE3[@]}"; do RM_FLAT+=" ~/tradinebotte/$f"; done
    if [[ "$DRY_RUN" == "true" ]]; then
        echo "  [DRY-RUN] rm -f $RM_FLAT"
    else
        _ssh "$USER" "$PASS" "rm -f $RM_FLAT 2>/dev/null && echo 'root flat JSONs removed'" || true
        ok "${USER}: root flat JSONs removed"
    fi
fi

# ─── Phase 5: Stale PID / orphan log+DB files ────────────────────────────────────
if should_run 5; then
    section "PHASE 5 — Stale PID files and orphan logs/DBs"

    # PID files are meaningless now — systemd manages all processes
    # Log rotations (.log.1, .log.2) for bots now managed by systemd (go to journal)
    # Scalping DBs on account-3 — scalping_bot not running, DBs are dead data

    _ssh_rm() {
        local user="$1" pass="$2" label="$3"; shift 3
        local cmd="$*"
        if [[ "$DRY_RUN" == "true" ]]; then
            echo "  [DRY-RUN] $label"
            return 0
        fi
        _ssh "$user" "$pass" "$cmd" || true
    }

    # account-0: corrupted DB backup + stale PID
    USER="${TEST_USERS[0]}"; PASS="${TEST_PASSWORDS[0]}"
    info "[${USER}] removing stale PID and corrupt DB backup..."
    warn "[${USER}] live.db is ACTIVE (account_bot uses it) — keeping live.db, live.db-shm, live.db-wal"
    _ssh_rm "$USER" "$PASS" "rm live.pid + live.db.corrupt_*" \
        'rm -f ~/tradinebotte/live.pid ~/tradinebotte/live.db.corrupt_* 2>/dev/null && echo done'
    ok "${USER}: done"

    # account-1: stale live.pid
    USER="${TEST_USERS[1]}"; PASS="${TEST_PASSWORDS[1]}"
    info "[${USER}] removing stale PID..."
    _ssh_rm "$USER" "$PASS" "rm live.pid" \
        'rm -f ~/tradinebotte/live.pid 2>/dev/null && echo done'
    ok "${USER}: done"

    # account-2: stale PIDs + old accumulation logs
    USER="${TEST_USERS[2]}"; PASS="${TEST_PASSWORDS[2]}"
    info "[${USER}] removing stale PIDs..."
    warn "[${USER}] keeping live_accum.db — may be active accumulation DB"
    _ssh_rm "$USER" "$PASS" "rm live.pid + accumulation_bot.pid" \
        'rm -f ~/tradinebotte/live.pid ~/tradinebotte/accumulation_bot.pid 2>/dev/null && echo done'
    ok "${USER}: done"

    # account-3: stale PIDs + scalping orphans + old log rotations
    USER="${TEST_USERS[3]}"; PASS="${TEST_PASSWORDS[3]}"
    info "[${USER}] removing stale PIDs, scalping orphans, old log rotations..."
    warn "[${USER}] keeping live.db, live_accum.db, live_ob.db — active"
    _ssh_rm "$USER" "$PASS" "rm stale PIDs + scalping_*.db/log + obi.*" '
        rm -f ~/tradinebotte/live.pid ~/tradinebotte/obi.pid 2>/dev/null
        rm -f ~/tradinebotte/accumulation_bot.pid ~/tradinebotte/orderbook_bot.pid 2>/dev/null
        rm -f ~/tradinebotte/obi.log 2>/dev/null
        rm -f ~/tradinebotte/scalping_breakout.db ~/tradinebotte/scalping_candle_momentum.db 2>/dev/null
        rm -f ~/tradinebotte/scalping_meanrev.db 2>/dev/null
        rm -f ~/tradinebotte/scalping_breakout.log ~/tradinebotte/scalping_candle_momentum.log 2>/dev/null
        rm -f ~/tradinebotte/scalping_meanrev.log 2>/dev/null
        rm -f ~/tradinebotte/accumulation_bot.log.1 ~/tradinebotte/accumulation_bot.log.2 2>/dev/null
        rm -f ~/tradinebotte/orderbook_bot.log.1 2>/dev/null
        echo done
    '
    ok "${USER}: done"

    # account-4: stale live.pid
    USER="${TEST_USERS[4]}"; PASS="${TEST_PASSWORDS[4]}"
    info "[${USER}] removing stale PID..."
    _ssh_rm "$USER" "$PASS" "rm live.pid" \
        'rm -f ~/tradinebotte/live.pid 2>/dev/null && echo done'
    ok "${USER}: done"
fi

# ─── Phase 6: Wrong-module files (permanent — from old git clone, won't return) ─
if should_run 6; then
    section "PHASE 6 — Wrong-module Python files (old git-clone residues)"
    warn "Files that WILL return after next deploy are NOT removed here — see Phase 7 notes."
    info "Only removing files that came from git clone and will NOT be re-synced."

    # Files that will NOT come back from any current rsync script:
    # - api_binance.py, api_bitstamp.py, api_mexc.py: not sent by update_standalone.sh
    #   (comes from tradinebotte-cex/, only sent by update_swing.sh to account-5,
    #    and by deploy_accumulation/scalping to accounts 3-4 — BUT those scripts
    #    also send the running bots, so we keep api_binance on accounts 3+4)
    # - scalping_bot.py, scalping_math.py: not running on any account
    # - earn_manager.py: needed on accounts 3+4 (imported by accumulation/orderbook)

    # account-0: CEX files (no CEX script ever deploys to account-0)
    USER="${TEST_USERS[0]}"; PASS="${TEST_PASSWORDS[0]}"
    info "[${USER}] removing CEX files (never needed on indicators/feed account)..."
    _ssh "$USER" "$PASS" '
        D=~/tradinebotte
        rm -f $D/accumulation_bot.py $D/orderbook_bot.py $D/scalping_bot.py 2>/dev/null
        rm -f $D/scalping_math.py $D/earn_manager.py 2>/dev/null
        rm -f $D/api_binance.py $D/api_bitstamp.py $D/api_mexc.py 2>/dev/null
        echo "done"
    ' || true
    ok "${USER}: done"

    # account-1: all CEX files (update_standalone.sh only — no CEX rsync ever targets account-1)
    USER="${TEST_USERS[1]}"; PASS="${TEST_PASSWORDS[1]}"
    info "[${USER}] removing CEX files (live-only account, no CEX deploy)..."
    _ssh "$USER" "$PASS" '
        D=~/tradinebotte
        rm -f $D/accumulation_bot.py $D/orderbook_bot.py $D/scalping_bot.py 2>/dev/null
        rm -f $D/scalping_math.py $D/earn_manager.py 2>/dev/null
        rm -f $D/api_binance.py $D/api_bitstamp.py $D/api_mexc.py 2>/dev/null
        echo "done"
    ' || true
    ok "${USER}: done"

    # account-2: scalping_bot + scalping_math + api_bitstamp/mexc only
    # (api_binance.py comes back via deploy_accumulation rsync — leave it)
    # (earn_manager.py is imported by accumulation_bot — leave it)
    USER="${TEST_USERS[2]}"; PASS="${TEST_PASSWORDS[2]}"
    info "[${USER}] removing unused scalping+bitstamp/mexc files..."
    _ssh "$USER" "$PASS" '
        D=~/tradinebotte
        rm -f $D/scalping_bot.py $D/scalping_math.py 2>/dev/null
        rm -f $D/api_bitstamp.py $D/api_mexc.py 2>/dev/null
        echo "done"
    ' || true
    ok "${USER}: done"

    # account-3: same as account-2
    USER="${TEST_USERS[3]}"; PASS="${TEST_PASSWORDS[3]}"
    info "[${USER}] removing unused scalping+bitstamp/mexc files..."
    _ssh "$USER" "$PASS" '
        D=~/tradinebotte
        rm -f $D/scalping_bot.py $D/scalping_math.py 2>/dev/null
        rm -f $D/api_bitstamp.py $D/api_mexc.py 2>/dev/null
        echo "done"
    ' || true
    ok "${USER}: done"

    # account-4: all CEX files (update_swing.sh will re-add them on next deploy
    # if swing bot is ever activated — acceptable until then)
    USER="${TEST_USERS[4]}"; PASS="${TEST_PASSWORDS[4]}"
    info "[${USER}] removing CEX files (swing bot inactive — clean now, re-synced if activated)..."
    _ssh "$USER" "$PASS" '
        D=~/tradinebotte
        rm -f $D/accumulation_bot.py $D/orderbook_bot.py $D/scalping_bot.py 2>/dev/null
        rm -f $D/scalping_math.py $D/earn_manager.py 2>/dev/null
        rm -f $D/api_bitstamp.py $D/api_mexc.py 2>/dev/null
        rm -rf $D/connectors $D/strategy_engines 2>/dev/null
        echo "done"
    ' || true
    ok "${USER}: done"

    echo ""
    warn "Phase 7 (rsync excludes) NOT automated — see notes below."
    echo ""
    echo "  Files that WILL return after next update_standalone.sh on accounts 2-5:"
    echo "    account_bot.py, feed.py  (in tradinebotte-polymarket/, always synced)"
    echo ""
    echo "  To prevent: add to update_standalone.sh _rsync():"
    echo "    --exclude='account_bot.py' --exclude='feed.py'"
    echo ""
    echo "  Files that return on accounts 3-4 via deploy_accumulation/scalping:"
    echo "    api_binance.py  (needed by connectors — acceptable)"
    echo "    api_bitstamp.py, api_mexc.py (add --exclude to CEX deploy scripts)"
fi

echo ""
section "DONE"
echo ""
echo "  Remaining manual actions:"
echo "  1. Phase 1 root commands above (disable + remove system services)"
echo "  2. Add rsync excludes to update_standalone.sh for account_bot.py, feed.py"
echo "  3. Add rsync excludes to deploy_accumulation.sh for api_bitstamp.py, api_mexc.py"
