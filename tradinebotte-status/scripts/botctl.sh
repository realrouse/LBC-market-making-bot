#!/usr/bin/env bash
# botctl.sh — operator CLI for the bot control plane.
#
# Sends a command to a bot's IPC control socket by SSH-ing in AS the bot's OS user
# (the socket is per-UID) and running tradinetools.ctl_client there.
#
# Usage:
#   bash botctl.sh <account_idx> <bot_name> <cmd> [--capital N] [--yes]
#
#   account_idx  index into TEST_USERS in ~/.tradinebotte-test.conf (0-based)
#   bot_name     the name the bot reports in its heartbeat (grid_bot, swing_bot,
#                accumulation_bot, live_bot)
#   cmd          ping | status | help | reset
#
# Read-only commands (ping/status/help) run unconditionally. Destructive commands
# (reset) are refused unless BOTH sources agree the bot is simulation:
#   - inventory.is_live = 0   (in the shared state DB)
#   - latest heartbeat mode = "sim" and fresh (< 5 min)
# and the operator passes --yes. Any ambiguity (live, missing, stale) → refused.
#
# Examples:
#   bash botctl.sh 5 grid_bot ping
#   bash botctl.sh 5 grid_bot status
#   bash botctl.sh 5 grid_bot reset --capital 2100 --yes
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
err()  { echo -e "${RED}✗ $*${NC}" >&2; }
ok()   { echo -e "${GREEN}✓ $*${NC}"; }
warn() { echo -e "${YELLOW}! $*${NC}"; }

DESTRUCTIVE_CMDS=" reset "          # space-delimited set
FRESH_MAX_S=300                     # heartbeat freshness window for destructive cmds

# ── Args ────────────────────────────────────────────────────────────────────────
[[ $# -ge 3 ]] || { err "usage: botctl.sh <account_idx> <bot_name> <cmd> [--capital N] [--yes]"; exit 2; }
ACC_IDX="$1"; BOT="$2"; CMD="$3"; shift 3
CAPITAL=""; ASSUME_YES=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --capital) CAPITAL="${2:?--capital needs a value}"; shift 2 ;;
        --yes|-y)  ASSUME_YES=true; shift ;;
        *) err "unknown arg: $1"; exit 2 ;;
    esac
done

# ── Config ──────────────────────────────────────────────────────────────────────
CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
[[ -f "$CONF" ]] || { err "missing config: $CONF"; exit 2; }
# shellcheck source=/dev/null
source "$CONF"
SERVER="${TEST_SERVER:?TEST_SERVER missing in $CONF}"
PORT="${TEST_PORT:-22}"
USERS=("${TEST_USERS[@]:?TEST_USERS missing in $CONF}")
PASSWORDS=("${TEST_PASSWORDS[@]:?TEST_PASSWORDS missing in $CONF}")
[[ "$ACC_IDX" =~ ^[0-9]+$ ]] || { err "account_idx must be a number"; exit 2; }
[[ "$ACC_IDX" -lt "${#USERS[@]}" ]] || { err "account_idx $ACC_IDX out of range (${#USERS[@]} users)"; exit 2; }
ACC_USER="${USERS[$ACC_IDX]}"
ACC_PASS="${PASSWORDS[$ACC_IDX]}"

SHARED_DB="${TRADINEBOTTE_DB:-/data1/tradinebotte-shared/database/tradinebotte.db}"

# ── Resolve install_dir from inventory ───────────────────────────────────────────
INSTALL_DIR="$(sqlite3 "$SHARED_DB" \
    "SELECT install_dir FROM inventory WHERE account='$ACC_USER' AND bot_name='$BOT';" 2>/dev/null || true)"
[[ -n "$INSTALL_DIR" ]] || { err "bot '$BOT' not found in inventory for account index $ACC_IDX"; exit 2; }

# ── Fail-closed guard for destructive commands ───────────────────────────────────
if [[ "$DESTRUCTIVE_CMDS" == *" $CMD "* ]]; then
    is_live="$(sqlite3 "$SHARED_DB" \
        "SELECT COALESCE(is_live,-1) FROM inventory WHERE account='$ACC_USER' AND bot_name='$BOT';" 2>/dev/null || echo -1)"
    if [[ "$is_live" != "0" ]]; then
        err "REFUSED: inventory says is_live=$is_live (need 0). '$CMD' is allowed on simulation bots only."
        exit 3
    fi
    read -r hb_ts hb_mode < <(sqlite3 -separator ' ' "$SHARED_DB" \
        "SELECT ts, COALESCE(json_extract(payload,'\$.mode'),'?') FROM heartbeats
         WHERE account='$ACC_USER' AND bot_name='$BOT' ORDER BY ts DESC LIMIT 1;" 2>/dev/null || echo "0 ?")
    now="$(date +%s)"; age=$(( now - ${hb_ts:-0} ))
    if [[ "${hb_mode:-?}" != "sim" ]]; then
        err "REFUSED: latest heartbeat mode='${hb_mode:-?}' (need 'sim')."; exit 3
    fi
    if [[ "${hb_ts:-0}" -le 0 || "$age" -gt "$FRESH_MAX_S" ]]; then
        err "REFUSED: heartbeat is stale/absent (age=${age}s > ${FRESH_MAX_S}s) — cannot confirm the bot is sim."; exit 3
    fi
    if [[ "$ASSUME_YES" != "true" ]]; then
        err "REFUSED: '$CMD' is destructive. Re-run with --yes to confirm."
        warn "would reset $BOT (acct idx $ACC_IDX) to capital=${CAPITAL:-<config default>} → backup+wipe state, restart in ~30s"
        exit 3
    fi
    ok "guard passed: is_live=0, heartbeat mode=sim (age=${age}s)"
fi

# ── Build and run the remote ctl_client invocation ───────────────────────────────
REMOTE_ARGS="--bot $BOT --cmd $CMD"
[[ -n "$CAPITAL" ]] && REMOTE_ARGS="$REMOTE_ARGS --capital $CAPITAL"
# Pick the bot's venv python (.venv vs venv), then run the client as the bot user.
REMOTE_CMD="cd \"$INSTALL_DIR\" || exit 9
PY=.venv/bin/python; [ -x \"\$PY\" ] || PY=venv/bin/python
\"\$PY\" -m tradinetools.ctl_client $REMOTE_ARGS"

echo -e "${YELLOW}▶ $CMD → $BOT (acct idx $ACC_IDX) @ $INSTALL_DIR${NC}"
set +e
REPLY_JSON="$(SSHPASS="$ACC_PASS" /usr/bin/sshpass -e \
    ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o PreferredAuthentications=password \
    -p "$PORT" "$ACC_USER@$SERVER" "$REMOTE_CMD" 2>&1)"
RC=$?
set -e
echo "$REPLY_JSON"
if [[ $RC -ne 0 ]]; then
    err "command returned non-zero ($RC)"
    exit $RC
fi
ok "done"
if [[ "$DESTRUCTIVE_CMDS" == *" $CMD "* && "$CMD" == "reset" ]]; then
    warn "bot exits now and systemd restarts it in ~30s (RestartSec); verify with: bash botctl.sh $ACC_IDX $BOT status"
fi
