#!/usr/bin/env bash
# install_feedwatchdog.sh — Install the feed watchdog (Solution C) on the feed account.
#
# Pushes feed_watchdog.py + the systemd --user .service/.timer to the feed account
# (TEST_USERS[idx], default 0 = the 15M-feed/infra account), enables the timer, and
# runs one dry-run check. The watchdog restarts tradinebotte-feed.service when it is
# alive but no longer publishing (see feed_watchdog.py).
#
# Usage:
#   bash tradinebotte-status/scripts/install_feedwatchdog.sh            # install + start
#   bash tradinebotte-status/scripts/install_feedwatchdog.sh --verify-only
#   FEED_IDX=0 bash tradinebotte-status/scripts/install_feedwatchdog.sh
#
# Rules: sequential single account, never sudo (systemctl --user only).

set -uo pipefail

LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
section(){ echo -e "\n${BOLD}${YELLOW}═══ $* ═══${NC}"; }
info(){ echo -e "${BLUE}  → $*${NC}"; }; ok(){ echo -e "${GREEN}  ✓ $*${NC}"; }
warn(){ echo -e "${YELLOW}  ! $*${NC}"; }; err(){ echo -e "${RED}  ✗ $*${NC}"; }

VERIFY_ONLY=false
[[ "${1:-}" == "--verify-only" ]] && VERIFY_ONLY=true

CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
[[ -f "$CONF" ]] || { err "missing $CONF"; exit 1; }
# shellcheck source=/dev/null
source "$CONF"
SERVER="${TEST_SERVER:?}"; PORT="${TEST_PORT:-22}"
IDX="${FEED_IDX:-0}"
USER="${TEST_USERS[$IDX]}"; PASS="${TEST_PASSWORDS[$IDX]}"
SRC="$LOCAL_REPO/tradinebotte-status"
UNITS="$LOCAL_REPO/systemd"
SSH_OPTS=(-o StrictHostKeyChecking=yes -o ConnectTimeout=20 -p "$PORT")

_ssh(){ SSHPASS="$PASS" /usr/bin/sshpass -e ssh "${SSH_OPTS[@]}" "$USER@$SERVER" "$@"; }
_scp(){ SSHPASS="$PASS" /usr/bin/sshpass -e scp -o StrictHostKeyChecking=yes -P "$PORT" "$@"; }

section "FEED WATCHDOG — install on acct-$((IDX+1)) @ $SERVER"
command -v sshpass >/dev/null || { err "sshpass not found"; exit 1; }

if [[ "$VERIFY_ONLY" == "false" ]]; then
    info "rsync feed_watchdog.py + units"
    _scp "$SRC/feed_watchdog.py" "$USER@$SERVER:~/tradinebotte/feed_watchdog.py" >/dev/null \
        && ok "feed_watchdog.py synced" || { err "scp watchdog failed"; exit 1; }
    _scp "$UNITS/tradinebotte-feedwatchdog.service" "$UNITS/tradinebotte-feedwatchdog.timer" \
        "$USER@$SERVER:~/.config/systemd/user/" >/dev/null \
        && ok "units synced" || { err "scp units failed"; exit 1; }

    info "enable + start timer"
    _ssh 'export XDG_RUNTIME_DIR=/run/user/$(id -u)
        systemctl --user daemon-reload
        systemctl --user enable --now tradinebotte-feedwatchdog.timer' \
        && ok "timer enabled + started" || { err "enable failed"; exit 1; }
fi

section "VERIFY"
info "timer status + next run"
_ssh 'export XDG_RUNTIME_DIR=/run/user/$(id -u)
    systemctl --user is-active tradinebotte-feedwatchdog.timer
    systemctl --user list-timers tradinebotte-feedwatchdog.timer --no-pager | sed -n 2p'
info "one dry-run (reads shared DB, no action):"
_ssh "export XDG_RUNTIME_DIR=/run/user/\$(id -u)
    sg claudes -c '~/tradinebotte/.venv/bin/python3 ~/tradinebotte/feed_watchdog.py --dry-run' 2>&1"

echo -e "\n${BOLD}${GREEN}  Done.${NC}"
echo "  Logs : ssh $USER@$SERVER 'journalctl --user -u tradinebotte-feedwatchdog.service -n 20'"
