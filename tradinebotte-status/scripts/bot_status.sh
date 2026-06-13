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
#   HEARTBEAT_STALE_S    Alive→Stale threshold in seconds  (default 7200)
#   HEARTBEAT_DEAD_S     Stale→Dead  threshold in seconds  (default 14400)

set -uo pipefail

STALE_AFTER="${HEARTBEAT_STALE_S:-7200}"
DEAD_AFTER="${HEARTBEAT_DEAD_S:-14400}"
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

# Labels mirror the full account topology (6 accounts).
declare -a LABELS=(
    "acct-1 [poly+cex+status]"
    "acct-2 [poly]"
    "acct-3 [poly+accum]"
    "acct-4 [poly+ob+accum]"
    "acct-5 [swing]"
    "acct-6 [grid-mexc-sim]"
)

for IDX in 0 1 2 3 4 5; do
    USER="${ALL_USERS[$IDX]}"
    PASS="${ALL_PASSWORDS[$IDX]}"
    TAG="${LABELS[$IDX]}"

    ROW=$(_ssh "$USER" "$PASS" "
        export XDG_RUNTIME_DIR=/run/user/\$(id -u)
        VS=\$(cat ~/tradinebotte/version.stamp 2>/dev/null || printf '?')
        printf '  %-28s' '$TAG'
        systemctl --user list-units 'tradinebotte-*' --no-legend --plain 2>/dev/null \
        | while read -r UNIT _L STATE _S _REST; do
            SVC=\$(printf '%s' \"\$UNIT\" | sed 's/tradinebotte-//;s/\\.service\$//')
            [ \"\$STATE\" = 'active' ] \
                && printf ' \033[0;32m✓ %s\033[0;36m(v=%s)\033[0m' \"\$SVC\" \"\$VS\" \
                || printf ' \033[0;31m✗ %s(%s)\033[0m' \"\$SVC\" \"\$STATE\"
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
