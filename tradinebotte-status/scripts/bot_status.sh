#!/usr/bin/env bash
# bot_status.sh — Full bot status report: heartbeats + per-account service states + versions.
#
# Sequential SSH to each account (never in parallel — same server rule).
# Output is ANSI-coloured; use --no-color to suppress.
#
# Sections:
#   1. Heartbeat table  — via heartbeat_query.py on the collector account
#   2. Service states   — per account: active/failed tradinebotte-* units + version stamp
#
# Exit codes:
#   0 — all heartbeats ALIVE and every tradinebotte service active
#   1 — any STALE/DEAD heartbeat, any failed/missing service, or SSH failure
#
# Usage:
#   bash tradinebotte-status/scripts/bot_status.sh
#   bash tradinebotte-status/scripts/bot_status.sh --no-color
#   bash tradinebotte-status/scripts/bot_status.sh --stale-after 3600
#
# Optional env vars:
#   TEST_MULTIBOT_CONF   Credentials file  (default ~/.tradinebotte-test.conf)
#   HEARTBEAT_STALE_S    Alive→Stale threshold in seconds  (default 240)
#   HEARTBEAT_DEAD_S     Stale→Dead  threshold in seconds  (default 600)

set -uo pipefail

STALE_AFTER="${HEARTBEAT_STALE_S:-240}"
DEAD_AFTER="${HEARTBEAT_DEAD_S:-600}"
COLOR=true

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-color)    COLOR=false ;;
        --stale-after) STALE_AFTER="$2"; shift ;;
        --dead-after)  DEAD_AFTER="$2";  shift ;;
        -h|--help)
            grep '^#' "${BASH_SOURCE[0]}" | head -28 | sed 's/^# \?//'; exit 0 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
    shift
done

# ─── Configuration ─────────────────────────────────────────────────────────────

CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
if [[ ! -f "$CONF" ]]; then
    echo "Missing configuration: $CONF" >&2; exit 1
fi
# shellcheck source=/dev/null
source "$CONF"

SERVER="${TEST_SERVER:?TEST_SERVER missing in $CONF}"
PORT="${TEST_PORT:-22}"
ALL_USERS=("${TEST_USERS[@]:?TEST_USERS missing in $CONF}")
ALL_PASSWORDS=("${TEST_PASSWORDS[@]:?TEST_PASSWORDS missing in $CONF}")
INSTALL_DIR="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"
SHARED_DB="${TRADINEBOTTE_DB:-/data1/tradinebotte-shared/database/tradinebotte.db}"

if [[ ! -x "$(command -v sshpass)" ]]; then
    echo "sshpass not found — apt-get install sshpass" >&2; exit 1
fi

mkdir -p ~/.ssh && chmod 700 ~/.ssh
if ! ssh-keygen -F "[$SERVER]:$PORT" &>/dev/null && \
   ! ssh-keygen -F "$SERVER"         &>/dev/null; then
    ssh-keyscan -p "$PORT" -H "$SERVER" >> ~/.ssh/known_hosts 2>/dev/null
fi

# ─── Helpers ───────────────────────────────────────────────────────────────────

BOLD='\033[1m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
RED='\033[0;31m'; NC='\033[0m'
[[ "$COLOR" == false ]] && BOLD='' && YELLOW='' && GREEN='' && RED='' && NC=''

_ssh() {
    local user="$1" pass="$2"; shift 2
    SSHPASS="$pass" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=10 -o BatchMode=no \
        -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
        -o PreferredAuthentications=password \
        -p "$PORT" "$user@$SERVER" "$@" 2>/dev/null
}

ISSUES=0

# ─── Section 1: Heartbeat table ───────────────────────────────────────────────

echo -e "${BOLD}${YELLOW}═══ BOT STATUS — $(date -u '+%Y-%m-%d %H:%M UTC') ═══${NC}"
echo -e "\n${BOLD}${YELLOW}─── HEARTBEATS ───${NC}"

HB_ARGS=(
    "--db"          "${SHARED_DB}"
    "--stale-after" "$STALE_AFTER"
    "--dead-after"  "$DEAD_AFTER"
)
[[ "$COLOR" == true ]] && HB_ARGS+=("--color")

HB_OUT=$(_ssh "${ALL_USERS[0]}" "${ALL_PASSWORDS[0]}" \
    "python3 ${INSTALL_DIR}/heartbeat_query.py ${HB_ARGS[*]+"${HB_ARGS[*]}"}" 2>&1)
HB_EXIT=$?
if [[ -z "$HB_OUT" ]] || [[ $HB_EXIT -ge 2 ]]; then
    echo -e "  ${YELLOW}! heartbeat collector unreachable or no rows in DB${NC}"
    ISSUES=$((ISSUES + 1))
else
    echo -e "$HB_OUT"
    # exit 1 = heartbeat_query.py found STALE/DEAD rows — already shown in the table
    [[ $HB_EXIT -ne 0 ]] && ISSUES=$((ISSUES + 1))
fi

# ─── Section 2: Per-account service states ────────────────────────────────────

echo -e "\n${BOLD}${YELLOW}─── SERVICES ───${NC}"

# Account labels AND the set of accounts to poll are DERIVED from inventory.toml (single
# source of truth) via the shared helper; fall back to plain acct-N over 0..5 if the
# helper/inventory/python is unavailable.
# The index list MUST come from the inventory, never a hard-coded range: it used to be
# `for IDX in 0 1 2 3 4 5`, which silently skipped account_idx 7 — the real-money account —
# so its services were never polled yet the report still printed "All systems nominal".
_BS_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
_BS_PY="$_BS_REPO/.venv/bin/python3"; [ -x "$_BS_PY" ] || _BS_PY=python3
mapfile -t LABELS < <(
    "$_BS_PY" -c "import sys; sys.path.insert(0, '$_BS_REPO/tradinebotte-status'); \
import inventory_labels as i; print('\n'.join(i.account_labels(i.load_rows())))" 2>/dev/null
)
mapfile -t IDXS < <(
    "$_BS_PY" -c "import sys; sys.path.insert(0, '$_BS_REPO/tradinebotte-status'); \
import inventory_labels as i; print('\n'.join(str(x) for x in i.active_account_idxs(i.load_rows())))" 2>/dev/null
)
if [ "${#IDXS[@]}" -eq 0 ]; then
    # Degraded path (no venv python, missing/malformed inventory.toml). Derive the indexes
    # from TEST_USERS rather than hard-coding a range — a literal 0..5 here would silently
    # skip the real-money account all over again, and precisely when we know least about the
    # fleet. Only the ephemeral clean-install test account is excluded.
    IDXS=()
    for _i in "${!ALL_USERS[@]}"; do
        [ "$_i" -ne "${TEST_STANDALONE_USER_IDX:-6}" ] && IDXS+=("$_i")
    done
fi

for IDX in "${IDXS[@]}"; do
    USER="${ALL_USERS[$IDX]:-}"
    PASS="${ALL_PASSWORDS[$IDX]:-}"
    TAG="${LABELS[$IDX]:-acct-$((IDX + 1))}"

    # inventory.toml lists an account the local credentials file does not — report it
    # rather than SSHing to an empty username (which would hang/fail opaquely).
    if [ -z "$USER" ]; then
        echo -e "  ${RED}✗ $TAG: account_idx $IDX missing from TEST_USERS in $CONF${NC}"
        ISSUES=$((ISSUES + 1))
        continue
    fi

    ROW=$(_ssh "$USER" "$PASS" "
        export XDG_RUNTIME_DIR=/run/user/\$(id -u)
        VS=\$(cat ~/tradinebotte/version.stamp 2>/dev/null || printf '?')
        printf '  %-28s' '$TAG'
        systemctl --user list-units 'tradinebotte-*' --no-legend --plain 2>/dev/null \
        | while read -r UNIT _L STATE _S _REST; do
            SVC=\$(printf '%s' \"\$UNIT\" | sed 's/tradinebotte-//;s/\\.service\$//')
            case \"\$STATE\" in
                active)
                    printf ' \033[0;32m✓ %s\033[0;36m(v=%s)\033[0m' \"\$SVC\" \"\$VS\" ;;
                activating|deactivating)
                    # Transient, NOT a failure: feedwatchdog is a oneshot driven by
                    # feedwatchdog.timer, so a check landing mid-run used to print ✗ and
                    # exit 1 on a perfectly healthy fleet. Marked '~' so the ✗ count below
                    # ignores it. (An idle oneshot never shows up at all — list-units
                    # without --all lists only active/activating units.)
                    printf ' \033[1;33m~ %s(%s)\033[0m' \"\$SVC\" \"\$STATE\" ;;
                *)
                    printf ' \033[0;31m✗ %s(%s)\033[0m' \"\$SVC\" \"\$STATE\" ;;
            esac
        done
        printf '\n'
    ") || ROW="  ${RED}✗ $TAG: unreachable${NC}"

    echo -e "$ROW"

    # Count failures: ✗ appears for failed/inactive services and unreachable accounts
    if echo "$ROW" | grep -q '✗'; then
        ISSUES=$((ISSUES + 1))
    fi
done

echo ""
[[ $ISSUES -eq 0 ]] && echo -e "${BOLD}${GREEN}  All systems nominal.${NC}" \
                     || echo -e "${BOLD}${RED}  $ISSUES issue(s) detected — check above.${NC}"
echo ""

[[ $ISSUES -eq 0 ]]
