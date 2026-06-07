#!/usr/bin/env bash
# migrate_cex_bots.sh — Migrate CEX bot nohup processes to systemd user units.
#
# Installs and starts user services for accounts that run CEX bots:
#   tradinebotte-accumulation.service  — accumulation_bot (accounts 2 and 3 in conf)
#   tradinebotte-orderbook.service     — orderbook_bot    (account 3 in conf)
#
# Strategy paths are per-account and baked into the unit at install time:
#   account idx 2: btc_accumulation.json
#   account idx 3: btc_accumulation_deepdip.json + orderbook_btc.json
#
# ─── Phase 1 (SSH as user, no sudo) ─────────────────────────────────────────────
#   - Detect venv path (.venv or venv)
#   - Write ~/.config/systemd/user/<service>.service with full paths
#   - daemon-reload, enable, kill nohup orphan, start
#
# ─── Phase 2 (printed for admin, requires sudo once per account) ─────────────────
#   loginctl enable-linger <user>   — persist user services across reboots/logouts
#
# Usage:
#   bash scripts/migrate_cex_bots.sh              # migrate all CEX bot accounts
#   bash scripts/migrate_cex_bots.sh --phase2-only  # only print Phase 2 admin steps

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
INSTALL_DIR="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"

# Account index → service name → bot script → strategy path
# Format: "idx:service:script:strategy"
CEX_BOTS=(
    "2:tradinebotte-accumulation.service:accumulation_bot.py:strategies/accumulation/btc_accumulation.json"
    "3:tradinebotte-accumulation.service:accumulation_bot.py:strategies/accumulation/btc_accumulation_deepdip.json"
    "3:tradinebotte-orderbook.service:orderbook_bot.py:strategies/scalping/orderbook_btc.json"
)

# ─── Phase 1: install user units + enable + start (no sudo) ─────────────────────
if [[ "$PHASE2_ONLY" == "false" ]]; then
    section "PHASE 1 — install user units (no sudo)"

    for ENTRY in "${CEX_BOTS[@]}"; do
        IDX="${ENTRY%%:*}"
        REST="${ENTRY#*:}"
        SVC="${REST%%:*}"
        REST="${REST#*:}"
        SCRIPT="${REST%%:*}"
        STRATEGY="${REST#*:}"

        USER="${TEST_USERS[$IDX]}"
        PASS="${TEST_PASSWORDS[$IDX]}"
        BOT_NAME="${SCRIPT%.py}"

        info "Installing $SVC on $USER (strategy: $STRATEGY)..."

        OUT=$(SSHPASS="$PASS" /usr/bin/sshpass -e \
            ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
            -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
            -o PreferredAuthentications=password \
            -p "$PORT" "$USER@$SERVER" "
set -e
INSTALL=$INSTALL_DIR
if   [ -d \"\$INSTALL/.venv\" ]; then PYEXEC=\"\$INSTALL/.venv/bin/python3\"
elif [ -d \"\$INSTALL/venv\"  ]; then PYEXEC=\"\$INSTALL/venv/bin/python3\"
else echo 'ERROR: no venv found'; exit 1; fi

mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/${SVC} << EOF
[Unit]
Description=tradinebotte — ${BOT_NAME} bot
After=network.target
Wants=network.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=\$INSTALL
ExecStart=\$PYEXEC \$INSTALL/${SCRIPT} --strategy \$INSTALL/${STRATEGY} --dir \$INSTALL
Restart=on-failure
RestartSec=30
StandardOutput=null
StandardError=null

[Install]
WantedBy=default.target
EOF
echo 'unit file written'

export XDG_RUNTIME_DIR=/run/user/\$(id -u)
systemctl --user daemon-reload && echo 'daemon-reload ok'
systemctl --user enable ${SVC} 2>&1 && echo 'enabled'

if ! systemctl --user is-active ${SVC} >/dev/null 2>&1; then
    for P in \$(pgrep -u \"\$(whoami)\" -f '${SCRIPT}' 2>/dev/null || true); do
        if readlink /proc/\"\$P\"/exe 2>/dev/null | grep -q python; then
            kill \"\$P\" 2>/dev/null && echo \"killed nohup ${BOT_NAME} pid=\$P\"
        fi
    done
    sleep 1
fi
systemctl --user start ${SVC} 2>&1 && echo 'started' || echo 'start failed (linger not yet set?)'
systemctl --user is-active ${SVC} 2>&1
" 2>&1)
        echo "$OUT"

        if echo "$OUT" | grep -q "started"; then
            ok "$USER: $SVC started"
        elif echo "$OUT" | grep -q "start failed"; then
            warn "$USER: $SVC installed — set linger to activate at boot"
        else
            err "$USER: $SVC — unexpected output"
        fi
    done
fi

# ─── Phase 2: linger (sudo required, run once per account) ──────────────────────
section "PHASE 2 — enable linger (requires sudo on the server)"
echo ""
echo "  Run the following as root on the server to persist services across reboots:"
echo ""

SEEN_IDXS=()
for ENTRY in "${CEX_BOTS[@]}"; do
    IDX="${ENTRY%%:*}"
    already=false
    for seen in "${SEEN_IDXS[@]:-}"; do [[ "$seen" == "$IDX" ]] && already=true; done
    [[ "$already" == "true" ]] && continue
    SEEN_IDXS+=("$IDX")
    USER="${TEST_USERS[$IDX]}"
    echo "  loginctl enable-linger $USER"
done
echo ""
warn "Without linger, user services stop when the SSH session that started them ends."
