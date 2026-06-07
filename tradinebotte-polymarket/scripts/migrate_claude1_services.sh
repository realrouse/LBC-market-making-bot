#!/usr/bin/env bash
# migrate_claude1_services.sh — Migrate account-1 system services to user units.
#
# Migrates three services that currently require sudo to restart:
#   tradinebotte-indicators          (ZeroMQ PUB — binds ports 5559 and 5561)
#   tradinebotte-feed                (ZeroMQ broadcaster — binds port 5557)
#   tradinebotte-account-<user>      (account_bot — depends on feed)
#
# After migration, update_claude1.sh uses systemctl --user with no sudo at all.
#
# ─── CRITICAL ORDER ──────────────────────────────────────────────────────────────
# feed and indicators bind ZeroMQ ports. Starting user units while system services
# hold those ports causes bind failure, burning through StartLimitBurst → dead.
# Phase 1 installs and ENABLES but does NOT start the user units.
# Phase 2 (admin sudo) stops system services FIRST, then starts user units.
#
# ─── One-time admin step (sudo required, done once) ──────────────────────────────
# loginctl enable-linger <user>
#   Allows the user's systemd manager to start at boot and persist without an
#   active login session. Without linger, user services stop on logout.
#
# ─── This script does ────────────────────────────────────────────────────────────
# Phase 1 (SSH as account-1 user, no sudo):
#   - Write ~/.config/systemd/user/tradinebotte-indicators.service
#   - Write ~/.config/systemd/user/tradinebotte-feed.service
#   - Write ~/.config/systemd/user/tradinebotte-account-<user>.service
#   - systemctl --user daemon-reload
#   - systemctl --user enable (all three) — but NOT start yet
#
# Phase 2 (printed for admin, requires sudo):
#   - loginctl enable-linger <user>
#   - systemctl stop + disable the three system services
#   - systemctl --user start the three user services (as account-1 user)
#
# Usage:
#   bash scripts/migrate_claude1_services.sh              # phase 1 + print phase 2
#   bash scripts/migrate_claude1_services.sh --phase2-only  # only print phase 2 steps

set -uo pipefail

CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
[[ -f "$CONF" ]] || { echo "Missing: $CONF"; exit 1; }
# shellcheck source=/dev/null
source "$CONF"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
section() { echo -e "\n${BOLD}${YELLOW}═══ $* ═══${NC}"; }
ok()      { echo -e "${GREEN}  ✓ $*${NC}"; }
warn()    { echo -e "${YELLOW}  ! $*${NC}"; }
err()     { echo -e "${RED}  ✗ $*${NC}"; }
info()    { echo -e "${BLUE}  → $*${NC}"; }

PHASE2_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --phase2-only) PHASE2_ONLY=true ;;
        *) echo "Unknown argument: $arg"; exit 1 ;;
    esac
done

PORT="${TEST_PORT:-22}"
SERVER="${TEST_SERVER:?}"
c1_user="${TEST_USERS[0]}"
c1_pass="${TEST_PASSWORDS[0]}"

# ─── Phase 1: install user units + enable (no sudo) ───────────────────────────
if [[ "$PHASE2_ONLY" == "false" ]]; then
    section "PHASE 1 — install user units (no sudo)"
    info "Installing user service units for account-1..."

    OUT=$(SSHPASS="$c1_pass" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
        -o PreferredAuthentications=password \
        -p "$PORT" "$c1_user@$SERVER" "
set -e
SVC_ACCOUNT=\"tradinebotte-account-\$(whoami).service\"
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/tradinebotte-indicators.service << 'UNITEOF'
[Unit]
Description=tradinebotte — Unified indicator service (shared)
Documentation=https://github.com/neofutur/tradinebotte/blob/main/docs/multi.md
After=network.target
Wants=network.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
Environment=TRADINEBOTTE_INDICATORS_ADDR=tcp://127.0.0.1:5559
Environment=TRADINEBOTTE_INDICATORS_REG_ADDR=tcp://127.0.0.1:5561
EnvironmentFile=-%h/tradinebotte/credentials
WorkingDirectory=%h/tradinebotte
ExecStart=%h/tradinebotte/.venv/bin/python3 %h/tradinebotte/indicators.py --config %h/tradinebotte/strategies/indicators/indicators_all.json
Restart=on-failure
RestartSec=15
StandardOutput=null
StandardError=null

[Install]
WantedBy=default.target
UNITEOF
echo 'indicators unit written'

cat > ~/.config/systemd/user/tradinebotte-feed.service << 'UNITEOF'
[Unit]
Description=tradinebotte — Shared WebSocket feed (ZeroMQ broadcaster)
Documentation=https://github.com/neofutur/tradinebotte/blob/main/docs/multi.md
After=network.target
Wants=network.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
Environment=TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5557
WorkingDirectory=%h/tradinebotte
ExecStart=%h/tradinebotte/.venv/bin/python3 %h/tradinebotte/feed.py
Restart=on-failure
RestartSec=10
StandardOutput=null
StandardError=null

[Install]
WantedBy=default.target
UNITEOF
echo 'feed unit written'

cat > ~/.config/systemd/user/\$SVC_ACCOUNT << 'UNITEOF'
[Unit]
Description=tradinebotte — Account bot (tradinebotte)
Documentation=https://github.com/neofutur/tradinebotte/blob/main/docs/multi.md
After=network.target tradinebotte-feed.service
Wants=network.target
Requires=tradinebotte-feed.service
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
Environment=TRADINEBOTTE_DIR=%h/tradinebotte
Environment=TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5557
WorkingDirectory=%h/tradinebotte
ExecStart=%h/tradinebotte/.venv/bin/python3 %h/tradinebotte/bot/account_bot.py
Restart=on-failure
RestartSec=30
StandardOutput=null
StandardError=null

[Install]
WantedBy=default.target
UNITEOF
echo 'account unit written'

export XDG_RUNTIME_DIR=/run/user/\$(id -u)
systemctl --user daemon-reload && echo 'daemon-reload ok'
systemctl --user enable tradinebotte-indicators.service 2>&1 && echo 'indicators enabled'
systemctl --user enable tradinebotte-feed.service 2>&1 && echo 'feed enabled'
systemctl --user enable \"\$SVC_ACCOUNT\" 2>&1 && echo 'account enabled'
echo 'phase1 complete'
" 2>&1)
    echo "$OUT"

    if echo "$OUT" | grep -q "phase1 complete"; then
        ok "Phase 1 complete — units installed and enabled (not started)"
        warn "Units are ENABLED but NOT STARTED (ports still held by system services)"
        warn "Complete Phase 2 admin steps below to finish the migration"
    else
        err "Phase 1 failed — check output above"
        exit 1
    fi
fi

# ─── Phase 2: linger + stop system + start user (sudo required, run once) ─────
section "PHASE 2 — admin steps (requires sudo on the server)"
echo ""
echo "  Run the following as root (or with sudo) on the server:"
echo ""
echo "  # 1. Enable linger so user services persist across reboots (no active session needed)"
echo "  loginctl enable-linger $c1_user"
echo ""
echo "  # 2. Stop and disable system services (releases ZeroMQ ports 5557, 5559, 5561)"
echo "  systemctl stop    tradinebotte-indicators tradinebotte-feed tradinebotte-account-${c1_user}"
echo "  systemctl disable tradinebotte-indicators tradinebotte-feed tradinebotte-account-${c1_user}"
echo ""
echo "  # 3. Start user services (run as account-1 user, in order)"
echo "  UID_C1=\$(id -u $c1_user)"
echo "  su -l $c1_user -c \"XDG_RUNTIME_DIR=/run/user/\$UID_C1 systemctl --user start tradinebotte-indicators\""
echo "  su -l $c1_user -c \"XDG_RUNTIME_DIR=/run/user/\$UID_C1 systemctl --user start tradinebotte-feed\""
echo "  su -l $c1_user -c \"XDG_RUNTIME_DIR=/run/user/\$UID_C1 systemctl --user start tradinebotte-account-${c1_user}\""
echo ""
echo "  # 4. Verify all three are active"
echo "  su -l $c1_user -c \"XDG_RUNTIME_DIR=/run/user/\$UID_C1 systemctl --user status tradinebotte-indicators tradinebotte-feed tradinebotte-account-${c1_user}\""
echo ""
warn "Do NOT start user services before stopping system services — ports 5557/5559/5561 must be free."
