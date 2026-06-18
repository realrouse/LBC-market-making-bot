#!/usr/bin/env bash
# deploy_all.sh — Deploy all bots sequentially across all accounts.
#
# 12 bots total across 6 accounts (same server — never run in parallel):
#   account-1  indicators + feed + account_bot  (Polymarket multibot infra)
#   account-2  live_bot (Polymarket threshold)
#   account-3  live_bot (Polymarket grid) + accumulation_bot + grid_bot (Binance CEX sim)
#   account-4  live_bot (Polymarket) + accumulation_bot deepdip  (orderbook_bot disabled)
#   account-5  swing live_bot (CEX)
#   account-6  grid live_bot (MEXC Futures BTC_USDT — simulation)
#
# Account-1 services are rsync-only by default: restarting indicators/feed
# disconnects all other live_bots for ~30s (RestartSec). Use --restart-infra
# when indicators.py, feed.py, or account_bot.py have changed.
#
# Usage:
#   bash scripts/deploy_all.sh
#   bash scripts/deploy_all.sh --skip-restart     # rsync only, no bot restarts
#   bash scripts/deploy_all.sh --verify-only      # status check, no changes
#   bash scripts/deploy_all.sh --restart-infra  # also restart account-1 services

set -uo pipefail

RESTART_INFRA=false
FORWARD_ARGS=()

for arg in "$@"; do
    case "$arg" in
        --restart-infra) RESTART_INFRA=true ;;
        *) FORWARD_ARGS+=("$arg") ;;
    esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PM="$REPO/tradinebotte-polymarket/scripts"
CEX="$REPO/tradinebotte-cex/scripts"
STATUS="$REPO/tradinebotte-status/scripts"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; NC='\033[0m'

FAILURES=0
STEP_LABELS=()
STEP_RESULTS=()

# Run a real deploy step; tracks result and failure count.
run_step() {
    local label="$1"
    local script="$2"
    shift 2
    local extra_args=("$@")
    STEP_LABELS+=("$label")
    echo -e "\n${BOLD}${YELLOW}▶▶▶ $label ▶▶▶${NC}"
    if bash "$script" "${extra_args[@]}" "${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}"; then
        STEP_RESULTS+=("OK")
    else
        STEP_RESULTS+=("FAILED")
        FAILURES=$((FAILURES + 1))
    fi
}

# Add a display-only row to the summary (no actual execution, no failure count).
add_row() {
    STEP_LABELS+=("$1")
    STEP_RESULTS+=("$2")
}

show_heartbeat_status() {
    local label="$1"
    echo -e "\n${BOLD}${YELLOW}─── $label ───${NC}"
    if [[ ! -f "$STATUS/heartbeat_status.sh" ]]; then
        echo -e "  ${YELLOW}! heartbeat_status.sh not found — skipping${NC}"
        return 0
    fi
    bash "$STATUS/heartbeat_status.sh" || \
        echo -e "  ${YELLOW}! heartbeat check non-zero — collector may not be deployed yet${NC}"
    return 0
}

show_heartbeat_status "HEARTBEAT — PRE-DEPLOY SNAPSHOT"

# ── Account-1: rsync only (default) or full service restart (--restart-infra)
if [[ "$RESTART_INFRA" == "true" ]]; then
    run_step "account-1 — full restart (indicators + feed + account_bot)" \
        "$PM/update_claude1.sh" \
        --restart-indicators --restart-feed --restart-account
    _c1="${STEP_RESULTS[-1]}"
    add_row "  └─ account-1 — indicators  (restarted)" "$_c1"
    add_row "  └─ account-1 — feed        (restarted)" "$_c1"
    add_row "  └─ account-1 — account_bot (restarted)" "$_c1"
    # Shared data plane (feed5m + cex_feed + tradinetools refresh) — only with infra
    # restart, since it (re)starts the feeds and briefly reconnects all consumers.
    run_step "account-1 — data plane (feed5m + cex_feed)" "$STATUS/setup_data_plane.sh"
else
    run_step "account-1 — rsync (indicators + feed + account_bot)" \
        "$PM/update_claude1.sh" --skip-restart
    add_row "  └─ account-1 — indicators  (rsync only — pass --restart-infra to restart)" "RSYNC"
    add_row "  └─ account-1 — feed        (rsync only — pass --restart-infra to restart)" "RSYNC"
    add_row "  └─ account-1 — account_bot (rsync only — pass --restart-infra to restart)" "RSYNC"
fi

# ── Accounts 2–5 ──────────────────────────────────────────────────────────────
run_step "account-2 — live_bot (Polymarket)"            "$PM/update_claude2.sh"
run_step "account-3 — live_bot (Polymarket)"            "$PM/update_claude3.sh"
run_step "account-3 — accumulation_bot"                 "$CEX/deploy_accumulation_claude3.sh"
run_step "account-3 — grid_bot (Binance CEX sim)"       "$CEX/deploy_grid_claude3.sh"
run_step "account-4 — live_bot (Polymarket)"            "$PM/update_claude4.sh"
# account-4 — orderbook_bot DISABLED 2026-06-14: structurally unprofitable
# (−$643 over 9051 trades, 5.4% win rate, fee-dominated). Service stopped + disabled
# on the host. The acct-4 scalping deploy script is kept in scripts/ for reference;
# re-enable here and in inventory.toml only after the strategy is recalibrated.
run_step "account-4 — accumulation_bot (deepdip)"       "$CEX/deploy_accumulation_claude4.sh"
run_step "account-5 — swing live_bot"                   "$CEX/update_swing.sh"
run_step "account-6 — grid live_bot (MEXC Futures sim)" "$CEX/deploy_grid_mexc.sh"

show_heartbeat_status "HEARTBEAT — POST-DEPLOY SNAPSHOT"

echo -e "\n${BOLD}${YELLOW}═══ DEPLOY ALL — SUMMARY (12 bots) ═══${NC}"
for i in "${!STEP_LABELS[@]}"; do
    case "${STEP_RESULTS[$i]}" in
        OK)     echo -e "${GREEN}  ✓ ${STEP_LABELS[$i]}${NC}" ;;
        RSYNC)  echo -e "${YELLOW}  ~ ${STEP_LABELS[$i]}${NC}" ;;
        FAILED) echo -e "${RED}  ✗ ${STEP_LABELS[$i]}${NC}" ;;
    esac
done

if [[ $FAILURES -eq 0 ]]; then
    echo -e "\n${BOLD}${GREEN}  ALL ACCOUNTS DEPLOYED SUCCESSFULLY${NC}"
    exit 0
else
    echo -e "\n${BOLD}${RED}  $FAILURES ACCOUNT(S) FAILED — check output above${NC}"
    exit 1
fi
