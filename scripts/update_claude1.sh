#!/usr/bin/env bash
# update_claude1.sh — Push a code update to the BTC 15M Polymarket account and restart live_bot.
#
# Targets TEST_USERS[0] (15M Polymarket collector, tag=102467).
# Retrieve live.db BEFORE any update if you plan to wipe the install.
#
# Usage:
#   bash scripts/update_claude1.sh                    # rsync + restart live_bot
#   bash scripts/update_claude1.sh --skip-restart     # rsync only, nothing restarted
#   bash scripts/update_claude1.sh --verify-only      # check status only
#   bash scripts/update_claude1.sh --restart-indicators  # rsync + restart tradinebotte-indicators (live_bot untouched)
#   bash scripts/update_claude1.sh --restart-feed        # rsync + restart tradinebotte-feed (live_bot untouched)
#   bash scripts/update_claude1.sh --restart-indicators --restart-feed  # rsync + restart both services

set -uo pipefail

RESTART_INDICATORS=false
RESTART_FEED=false
FORWARD_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --restart-indicators) RESTART_INDICATORS=true ;;
        --restart-feed)       RESTART_FEED=true ;;
        *) FORWARD_ARGS+=("$1") ;;
    esac
    shift
done

# --restart-indicators / --restart-feed imply --skip-restart (don't touch live_bot)
# unless the caller explicitly passed --skip-restart themselves.
if [[ "$RESTART_INDICATORS" == "true" || "$RESTART_FEED" == "true" ]]; then
    # Only add --skip-restart if it wasn't already in FORWARD_ARGS
    if ! printf '%s\n' "${FORWARD_ARGS[@]}" | grep -q -- '--skip-restart\|--verify-only'; then
        FORWARD_ARGS+=(--skip-restart)
    fi
fi

# ─── Run the standard update (rsync + optional live_bot restart + verify) ──────
TEST_STANDALONE_USER_IDX=0 bash "$(dirname "$0")/update_standalone.sh" "${FORWARD_ARGS[@]}"
UPDATE_EXIT=$?

if [[ "$UPDATE_EXIT" -ne 0 ]]; then
    exit "$UPDATE_EXIT"
fi

# ─── Shared helper: restart a systemd service on the deployment account ────────
_restart_service() {
    local svc="$1" label="$2" grep_pat="$3"

    CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
    source "$CONF"
    local server="${TEST_SERVER:?}" port="${TEST_PORT:-22}"
    local c1_user="${TEST_USERS[0]}" c1_pass="${TEST_PASSWORDS[0]}"

    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
    echo -e "\n${BOLD}${YELLOW}═══ RESTART ${label} ═══${NC}"

    local out
    out=$(SSHPASS="$c1_pass" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -p "$port" "$c1_user@$server" \
        "echo '$c1_pass' | sudo -S systemctl restart ${svc} 2>/dev/null \
         && echo 'restarted' \
         && sleep 3 \
         && journalctl -u ${svc} --no-pager -n 6 2>/dev/null | grep -E '${grep_pat}'" 2>&1)

    echo "$out"
    if echo "$out" | grep -q "restarted"; then
        echo -e "${GREEN}  ✓ ${svc} restarted${NC}"
    else
        echo -e "${RED}  ✗ ${svc} restart failed — check manually${NC}"
        return 1
    fi
}

# ─── Restart tradinebotte-indicators if requested ──────────────────────────────
if [[ "$RESTART_INDICATORS" == "true" ]]; then
    _restart_service "tradinebotte-indicators" "INDICATORS" "PUB bind|scalping|ERROR"
fi

# ─── Restart tradinebotte-feed if requested ────────────────────────────────────
if [[ "$RESTART_FEED" == "true" ]]; then
    _restart_service "tradinebotte-feed.service" "FEED" "connected|bind|ERROR"
fi
