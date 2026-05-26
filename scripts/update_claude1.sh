#!/usr/bin/env bash
# update_claude1.sh — Push a code update to the BTC 15M Polymarket account and restart live_bot.
#
# Targets TEST_USERS[0] (15M Polymarket collector, tag=102467).
# Retrieve live.db BEFORE any update if you plan to wipe the install.
#
# Usage:
#   bash scripts/update_claude1.sh                       # rsync + restart live_bot
#   bash scripts/update_claude1.sh --skip-restart        # rsync only
#   bash scripts/update_claude1.sh --verify-only         # check status only
#   bash scripts/update_claude1.sh --restart-indicators  # rsync + restart tradinebotte-indicators
#   bash scripts/update_claude1.sh --skip-restart --restart-indicators  # rsync + restart indicators only

set -uo pipefail

RESTART_INDICATORS=false
FORWARD_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --restart-indicators) RESTART_INDICATORS=true ;;
        *) FORWARD_ARGS+=("$1") ;;
    esac
    shift
done

# ─── Run the standard update (rsync + optional live_bot restart + verify) ──────
TEST_STANDALONE_USER_IDX=0 bash "$(dirname "$0")/update_standalone.sh" "${FORWARD_ARGS[@]}"
UPDATE_EXIT=$?

if [[ "$UPDATE_EXIT" -ne 0 ]]; then
    exit "$UPDATE_EXIT"
fi

# ─── Restart tradinebotte-indicators if requested ──────────────────────────────
if [[ "$RESTART_INDICATORS" == "true" ]]; then
    CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
    source "$CONF"

    SERVER="${TEST_SERVER:?}"
    PORT="${TEST_PORT:-22}"
    C1_USER="${TEST_USERS[0]}"
    C1_PASS="${TEST_PASSWORDS[0]}"

    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
    BOLD='\033[1m'; NC='\033[0m'
    echo -e "\n${BOLD}${YELLOW}═══ RESTART INDICATORS ═══${NC}"

    OUT=$(SSHPASS="$C1_PASS" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -p "$PORT" "$C1_USER@$SERVER" \
        "echo '$C1_PASS' | sudo -S systemctl restart tradinebotte-indicators 2>/dev/null \
         && echo 'restarted' \
         && sleep 3 \
         && journalctl -u tradinebotte-indicators --no-pager -n 6 2>/dev/null | grep -E 'PUB bind|scalping|ERROR'" 2>&1)

    echo "$OUT"
    if echo "$OUT" | grep -q "restarted"; then
        echo -e "${GREEN}  ✓ tradinebotte-indicators restarted${NC}"
    else
        echo -e "${RED}  ✗ indicators restart failed — check manually${NC}"
        exit 1
    fi
fi
