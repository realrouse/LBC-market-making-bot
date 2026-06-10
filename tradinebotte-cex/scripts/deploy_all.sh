#!/usr/bin/env bash
# deploy_all.sh — Deploy all bots sequentially across all accounts.
#
# Order (same server — never run in parallel):
#   1. account-1  (Polymarket live_bot — update_claude1.sh)
#   2. account-2  (Polymarket live_bot — update_claude2.sh)
#   3. account-3  (Polymarket live_bot + accumulation_bot)
#   4. account-4  (Polymarket live_bot + orderbook_bot + accumulation_bot deepdip)
#   5. account-5  (swing live_bot — update_claude5.sh)
#
# Each script exits non-zero on failure; this script reports a summary
# at the end and exits 1 if any account had failures.
#
# Usage:
#   bash scripts/deploy_all.sh
#   bash scripts/deploy_all.sh --skip-restart   # rsync only
#   bash scripts/deploy_all.sh --verify-only    # status check, no changes

set -uo pipefail

ARGS=("$@")

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PM="$REPO/tradinebotte-polymarket/scripts"
CEX="$REPO/tradinebotte-cex/scripts"
STATUS="$REPO/tradinebotte-status/scripts"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; NC='\033[0m'

FAILURES=0
STEP_LABELS=()
STEP_RESULTS=()

run_step() {
    local label="$1"
    local script="$2"
    shift 2
    local extra_args=("$@")
    STEP_LABELS+=("$label")
    echo -e "\n${BOLD}${YELLOW}▶▶▶ $label ▶▶▶${NC}"
    if bash "$script" "${extra_args[@]}" "${ARGS[@]}"; then
        STEP_RESULTS+=("OK")
    else
        STEP_RESULTS+=("FAILED")
        FAILURES=$((FAILURES + 1))
    fi
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

run_step "account-1 — rsync (Polymarket)"                "$PM/update_claude1.sh" --skip-restart
run_step "account-2 — live_bot (Polymarket)"            "$PM/update_claude2.sh"
run_step "account-3 — live_bot (Polymarket)"            "$PM/update_claude3.sh"
run_step "account-3 — accumulation_bot"                 "$CEX/deploy_accumulation_claude3.sh"
run_step "account-4 — live_bot (Polymarket)"            "$PM/update_claude4.sh"
run_step "account-4 — orderbook_bot"                    "$CEX/deploy_scalping_claude4.sh"
run_step "account-4 — accumulation_bot (deepdip)"       "$CEX/deploy_accumulation_claude4.sh"
run_step "account-5 — swing live_bot"                   "$CEX/update_swing.sh"

show_heartbeat_status "HEARTBEAT — POST-DEPLOY SNAPSHOT"

echo -e "\n${BOLD}${YELLOW}═══ DEPLOY ALL — SUMMARY ═══${NC}"
for i in "${!STEP_LABELS[@]}"; do
    if [[ "${STEP_RESULTS[$i]}" == "OK" ]]; then
        echo -e "${GREEN}  ✓ ${STEP_LABELS[$i]}${NC}"
    else
        echo -e "${RED}  ✗ ${STEP_LABELS[$i]}${NC}"
    fi
done

if [[ $FAILURES -eq 0 ]]; then
    echo -e "\n${BOLD}${GREEN}  ALL ACCOUNTS DEPLOYED SUCCESSFULLY${NC}"
    exit 0
else
    echo -e "\n${BOLD}${RED}  $FAILURES ACCOUNT(S) FAILED — check output above${NC}"
    exit 1
fi
