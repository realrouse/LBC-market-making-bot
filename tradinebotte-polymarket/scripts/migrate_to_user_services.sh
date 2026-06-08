#!/usr/bin/env bash
# migrate_to_user_services.sh — Migrate tradinebotte live bot services from
# system units (/etc/systemd/system/) to user units (~/.config/systemd/user/).
#
# After migration, each user manages their own service with no sudo at all:
#   systemctl --user restart tradinebotte-live.service
#
# ─── Why user services ───────────────────────────────────────────────────────
# System services require root to restart. User services (systemctl --user)
# can be started/stopped/restarted by the owning user without any privilege.
# This is the correct model for per-account bots that only affect one user.
#
# ─── One-time admin step (root/sudo required, done once per account) ─────────
# loginctl enable-linger <user>
#   MUST be run as root — cannot be automated from a user SSH session.
#   Allows the user's systemd instance to start at boot and persist without
#   an active login session. Without linger, all user services stop when the
#   SSH session that started them ends — bots go down silently at logout.
#
# ─── This script does ────────────────────────────────────────────────────────
# Phase 1 (SSH as user, no sudo):
#   - Create ~/.config/systemd/user/tradinebotte-live.service
#   - systemctl --user daemon-reload
#   - systemctl --user enable tradinebotte-live.service
#   - systemctl --user start  tradinebotte-live.service
#
# Phase 2 (local, sudo required — run once by admin):
#   - loginctl enable-linger <user>        ← persistent boot start
#   - systemctl stop    tradinebotte-live-<user>.service   ← remove system svc
#   - systemctl disable tradinebotte-live-<user>.service
#
# Usage:
#   bash scripts/migrate_to_user_services.sh              # migrate all live-bot accounts
#   bash scripts/migrate_to_user_services.sh <username>   # single account
#   bash scripts/migrate_to_user_services.sh --phase2-only  # only print admin steps

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
TARGET_USER=""

for arg in "$@"; do
    case "$arg" in
        --phase2-only) PHASE2_ONLY=true ;;
        --*) echo "Unknown argument: $arg"; exit 1 ;;
        *) TARGET_USER="$arg" ;;
    esac
done

INSTALL_DIR="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"
PORT="${TEST_PORT:-22}"
SERVER="${TEST_SERVER:?}"

# Indices of live-bot accounts in TEST_USERS (accounts 1, 2, 3, 4 in the conf)
LIVE_IDXS=(1 2 3 4)

# ─── Phase 1: install user unit + enable (no sudo) ────────────────────────────
if [[ "$PHASE2_ONLY" == "false" ]]; then
    section "PHASE 1 — install user units (no sudo)"

    for IDX in "${LIVE_IDXS[@]}"; do
        USER="${TEST_USERS[$IDX]}"
        PASS="${TEST_PASSWORDS[$IDX]}"
        [[ -n "$TARGET_USER" && "$USER" != "$TARGET_USER" ]] && continue

        info "Migrating $USER..."

        OUT=$(SSHPASS="$PASS" /usr/bin/sshpass -e \
            ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
            -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
            -o PreferredAuthentications=password \
            -p "$PORT" "$USER@$SERVER" "
INSTALL=${INSTALL_DIR}
# Detect actual venv
if   [ -d \"\$INSTALL/.venv\" ]; then VENV=\"\$INSTALL/.venv/bin/python3\"
elif [ -d \"\$INSTALL/venv\"  ]; then VENV=\"\$INSTALL/venv/bin/python3\"
else echo 'ERROR: no venv found'; exit 1; fi

mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/tradinebotte-live.service << EOF
[Unit]
Description=tradinebotte — Polymarket live bot
After=network.target
Wants=network.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=\$INSTALL
ExecStart=\$VENV \$INSTALL/live_bot.py
Restart=on-failure
RestartSec=30
StandardOutput=null
StandardError=null

[Install]
WantedBy=default.target
EOF
echo 'unit file written'

systemctl --user daemon-reload && echo 'daemon-reload ok'
systemctl --user enable tradinebotte-live.service 2>&1 && echo 'enabled'
if ! systemctl --user is-active tradinebotte-live.service >/dev/null 2>&1; then
    for P in \$(pgrep -u \"\$(whoami)\" -f 'live_bot' 2>/dev/null || true); do
        if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
            kill \"\$P\" 2>/dev/null && echo \"killed nohup live_bot pid=\$P\"
        fi
    done
    sleep 1
fi
systemctl --user start  tradinebotte-live.service 2>&1 && echo 'started' || echo 'start failed (linger not yet set?)'
systemctl --user is-active tradinebotte-live.service 2>&1
" 2>&1)
        echo "$OUT"

        if echo "$OUT" | grep -q "started"; then
            ok "$USER: user service started"
        elif echo "$OUT" | grep -q "start failed"; then
            warn "$USER: started after linger — run phase 2 to activate at boot"
        else
            err "$USER: unexpected output — check manually"
        fi
    done
fi

# ─── Phase 2: linger + disable system service (sudo required, run once) ───────
section "PHASE 2 — linger + disable system services (requires sudo)"
echo ""
echo "  Run the following as root on the server:"
echo ""
for IDX in "${LIVE_IDXS[@]}"; do
    USER="${TEST_USERS[$IDX]}"
    [[ -n "$TARGET_USER" && "$USER" != "$TARGET_USER" ]] && continue
    SVC="tradinebotte-live-${USER}.service"
    echo "  # $USER"
    echo "  loginctl enable-linger $USER          # persist user services across reboots"
    echo "  systemctl stop    $SVC  # stop system service"
    echo "  systemctl disable $SVC  # prevent it from starting at boot"
    echo ""
done
echo "  Or copy-paste all at once:"
USERS_LIST=""
SVCS_STOP=""
SVCS_DISABLE=""
for IDX in "${LIVE_IDXS[@]}"; do
    USER="${TEST_USERS[$IDX]}"
    [[ -n "$TARGET_USER" && "$USER" != "$TARGET_USER" ]] && continue
    USERS_LIST+="$USER "
    SVCS_STOP+="tradinebotte-live-${USER}.service "
    SVCS_DISABLE+="tradinebotte-live-${USER}.service "
done
echo "  for u in ${USERS_LIST}; do loginctl enable-linger \$u; done"
echo "  systemctl stop ${SVCS_STOP}"
echo "  systemctl disable ${SVCS_DISABLE}"
echo ""
warn "Until phase 2 is done, BOTH system and user service exist — stop the system one to avoid DB lock."
