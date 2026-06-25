#!/usr/bin/env bash
# install_statuspage_timer.sh — Install the status-page generator as a systemd --user
# timer on the OPERATOR machine, replacing the hand-maintained crontab so the schedule
# is versioned in the repo.
#
# The generator (generate_status.py) SSHes to every account and writes the status page;
# it runs locally on the operator, NOT on a bot account. systemd --user with linger
# enabled runs it regardless of an active login session (same as a crontab).
#
# Usage:
#   bash tradinebotte-status/scripts/install_statuspage_timer.sh
#   bash tradinebotte-status/scripts/install_statuspage_timer.sh --no-start   # install only
#
# After running, the timer is enabled and started; the first page is generated ~30s later.
# Remove the old crontab line (tagged `# tradinebotte-statuspage`) to avoid double runs —
# this script detects it and prints the exact command.

set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓ $*${NC}"; }
info() { echo -e "${BLUE}  → $*${NC}"; }
err()  { echo -e "${RED}  ✗ $*${NC}"; }
warn() { echo -e "${YELLOW}  ! $*${NC}"; }

NO_START=false
[[ "${1:-}" == "--no-start" ]] && NO_START=true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
UNIT_SRC="$SCRIPT_DIR/systemd"
UNIT_DIR="$HOME/.config/systemd/user"

echo -e "\n${BOLD}${YELLOW}═══ tradinebotte-statuspage timer install ═══${NC}"

# ─── Detect the venv ───────────────────────────────────────────────────────────
VENV=""
if   [[ -x "$REPO_DIR/.venv/bin/python3" ]]; then VENV="$REPO_DIR/.venv"
elif [[ -x "$REPO_DIR/venv/bin/python3"  ]]; then VENV="$REPO_DIR/venv"
fi
if [[ -z "$VENV" ]]; then
    err "No virtualenv at $REPO_DIR/.venv or $REPO_DIR/venv"
    exit 1
fi
info "Repo : $REPO_DIR"
info "Venv : $VENV"

# ─── Sanity: generator + config present ────────────────────────────────────────
if [[ ! -f "$REPO_DIR/tradinebotte-status/generate_status.py" ]]; then
    err "generate_status.py not found under $REPO_DIR/tradinebotte-status"
    exit 1
fi
if [[ ! -f "$HOME/.tradinebotte-test.conf" ]]; then
    warn "~/.tradinebotte-test.conf not found — the generator needs it for SSH credentials"
fi

# ─── Guard against '|' in any value used as a sed substitution ──────────────────
for _v in "$REPO_DIR" "$VENV"; do
    if [[ "$_v" == *'|'* ]]; then
        err "a path contains '|', which conflicts with the sed delimiter: $_v"
        exit 1
    fi
done

# ─── Render units ──────────────────────────────────────────────────────────────
mkdir -p "$UNIT_DIR"
sed -e "s|__REPO_DIR__|$REPO_DIR|g" -e "s|__VENV__|$VENV|g" \
    "$UNIT_SRC/tradinebotte-statuspage.service" > "$UNIT_DIR/tradinebotte-statuspage.service"
cp "$UNIT_SRC/tradinebotte-statuspage.timer" "$UNIT_DIR/tradinebotte-statuspage.timer"
ok "units installed in $UNIT_DIR"

# ─── Enable + start ────────────────────────────────────────────────────────────
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
systemctl --user daemon-reload
ok "systemd user daemon reloaded"

if [[ "$NO_START" == "true" ]]; then
    systemctl --user enable tradinebotte-statuspage.timer >/dev/null 2>&1
    ok "timer enabled (not started — --no-start)"
else
    systemctl --user enable --now tradinebotte-statuspage.timer
    ok "timer enabled + started"
fi

# ─── Linger reminder ───────────────────────────────────────────────────────────
if [[ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" != "yes" ]]; then
    warn "linger is NOT enabled for $(id -un) — the timer will pause when you log out."
    warn "  enable it with:  loginctl enable-linger $(id -un)"
fi

# ─── Old crontab detection ─────────────────────────────────────────────────────
if crontab -l 2>/dev/null | grep -q "tradinebotte-statuspage"; then
    echo ""
    warn "An old crontab entry still generates the page — remove it to avoid double runs:"
    echo "    crontab -l | grep -v 'tradinebotte-statuspage' | crontab -"
fi

echo ""
echo -e "${BOLD}${GREEN}  Done.${NC}"
echo "  Status :  systemctl --user status tradinebotte-statuspage.timer"
echo "  Runs   :  systemctl --user list-timers tradinebotte-statuspage.timer"
echo "  Logs   :  journalctl --user -u tradinebotte-statuspage.service -n 20"
echo "  Once   :  systemctl --user start tradinebotte-statuspage.service"
