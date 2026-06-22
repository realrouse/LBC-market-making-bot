#!/usr/bin/env bash
# heartbeat_status.sh — Report the latest heartbeat for every (account, bot_name) pair.
#
# Connects to the status collector account via SSH and runs heartbeat_query.py
# against the shared state DB (/data1/tradinebotte-shared/database/tradinebotte.db).
#
# NOTE: Only bots that have sent at least one heartbeat appear in the table.
#       A bot never deployed with Phase-4 code produces no rows and is invisible.
#       Use --require to assert that a given bot_name has at least one ALIVE row.
#
# Exit codes:
#   0 — all known bots ALIVE and all --require names satisfied
#   1 — any STALE/DEAD row, a required bot missing, DB not found, or SSH failure
#
# Usage:
#   bash tradinebotte-status/scripts/heartbeat_status.sh
#   bash tradinebotte-status/scripts/heartbeat_status.sh --require account_bot --require live_bot
#   bash tradinebotte-status/scripts/heartbeat_status.sh --collector-idx 0
#   bash tradinebotte-status/scripts/heartbeat_status.sh --stale-after 3600 --dead-after 7200
#
# Optional env vars:
#   TEST_MULTIBOT_CONF   Credentials file path  (default ~/.tradinebotte-test.conf)
#   HEARTBEAT_STALE_S    Alive→Stale threshold  (default 240  = 4 min, ~2 missed beats)
#   HEARTBEAT_DEAD_S     Stale→Dead threshold   (default 600  = 10 min, ~5 missed beats)
#
# Local prerequisites: sshpass  (apt-get install sshpass)

set -uo pipefail

COLLECTOR_IDX=0
STALE_AFTER="${HEARTBEAT_STALE_S:-240}"
DEAD_AFTER="${HEARTBEAT_DEAD_S:-600}"
REQUIRE_NAMES=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --collector-idx) COLLECTOR_IDX="$2"; shift ;;
        --stale-after)   STALE_AFTER="$2";  shift ;;
        --dead-after)    DEAD_AFTER="$2";   shift ;;
        --require)       REQUIRE_NAMES+=("$2"); shift ;;
        -h|--help)
            grep '^#' "${BASH_SOURCE[0]}" | head -30 | sed 's/^# \?//'; exit 0 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
    shift
done

# ─── Configuration ─────────────────────────────────────────────────────────────

CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
if [[ ! -f "$CONF" ]]; then
    echo "Missing configuration: $CONF"
    echo "  cp tradinebotte-status/scripts/test_multibot.conf.example ~/.tradinebotte-test.conf"
    exit 1
fi
# shellcheck source=/dev/null
source "$CONF"

SERVER="${TEST_SERVER:?TEST_SERVER missing in $CONF}"
PORT="${TEST_PORT:-22}"
ALL_USERS=("${TEST_USERS[@]:?TEST_USERS missing in $CONF}")
ALL_PASSWORDS=("${TEST_PASSWORDS[@]:?TEST_PASSWORDS missing in $CONF}")
INSTALL_DIR="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"
SHARED_DB="${TRADINEBOTTE_DB:-/data1/tradinebotte-shared/database/tradinebotte.db}"

if [[ "$COLLECTOR_IDX" -ge "${#ALL_USERS[@]}" ]]; then
    echo "ERROR: --collector-idx=$COLLECTOR_IDX is out of range (${#ALL_USERS[@]} users in conf)"
    exit 1
fi
COLL_USER="${ALL_USERS[$COLLECTOR_IDX]}"
COLL_PASS="${ALL_PASSWORDS[$COLLECTOR_IDX]}"

# ─── Prerequisites ─────────────────────────────────────────────────────────────

if [[ ! -x "$(command -v sshpass)" ]]; then
    echo "sshpass not found — install with: apt-get install sshpass"
    exit 1
fi

mkdir -p ~/.ssh && chmod 700 ~/.ssh
if ! ssh-keygen -F "[$SERVER]:$PORT" &>/dev/null && \
   ! ssh-keygen -F "$SERVER"         &>/dev/null; then
    ssh-keyscan -p "$PORT" -H "$SERVER" >> ~/.ssh/known_hosts 2>/dev/null
fi

# ─── Build remote arguments ────────────────────────────────────────────────────

REMOTE_ARGS=(
    "--db"          "${SHARED_DB}"
    "--stale-after" "$STALE_AFTER"
    "--dead-after"  "$DEAD_AFTER"
)
for name in "${REQUIRE_NAMES[@]+"${REQUIRE_NAMES[@]}"}"; do
    REMOTE_ARGS+=("--require" "$name")
done
# Pass --color when the local terminal is a TTY so ANSI codes work over SSH.
[ -t 1 ] && REMOTE_ARGS+=("--color")

# ─── Run query on the remote ───────────────────────────────────────────────────

SSHPASS="$COLL_PASS" /usr/bin/sshpass -e \
    ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
    -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
    -o PreferredAuthentications=password \
    -p "$PORT" "$COLL_USER@$SERVER" \
    "python3 ${INSTALL_DIR}/heartbeat_query.py ${REMOTE_ARGS[*]+"${REMOTE_ARGS[*]}"}"

# SSH exit code propagates directly — 0 = all ALIVE, 1 = issues or DB missing.
