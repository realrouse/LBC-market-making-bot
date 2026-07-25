#!/usr/bin/env bash
# cleanup_old_heartbeat_db.sh — delete the pre-migration heartbeat.db backup, but ONLY
# after a strict safety gate proves it is fully superseded by the shared state DB.
#
# Runs locally on apollo as neofutur; reaches the collector account (TEST_USERS[0]) by
# loopback SSH, exactly like bot_status.sh.  No secrets are stored anywhere — the
# credentials are sourced from ~/.tradinebotte-test.conf at run time. The account name
# is never hardcoded; the old file lives under the collector's own $HOME on the remote.
#
# Safety gate (ALL must pass, else abort without deleting):
#   1. shared state DB healthy — total rows >= 1398 AND newest heartbeat < 2h old
#      (i.e. the collector is actively writing the shared DB).
#   2. old file frozen — exactly 1398 rows AND mtime unchanged since migration
#      (epoch 1781362906 = 2026-06-13 15:01:46) → nothing has written it.
#   3. old file not open by any process (lsof).
# Only then: rm the old heartbeat.db and its -wal/-shm sidecars on the collector account.
#
# Usage:  bash scripts/cleanup_old_heartbeat_db.sh            # gated delete
#         bash scripts/cleanup_old_heartbeat_db.sh --dry-run  # run the gate, never delete
#
# Exit: 0 = deleted (or dry-run gate passed); 1 = aborted (gate failed / unreachable).

set -uo pipefail

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

OLD_REL="tradinebotte/heartbeat.db"     # under the collector account's $HOME (remote)
SHARED_DB="/data1/tradinebotte-shared/database/tradinebotte.db"
EXPECTED_ROWS=1398
EXPECTED_MTIME=1781362906               # 2026-06-13 15:01:46 UTC — last write before migration
FRESH_S=7200                            # shared DB newest heartbeat must be younger than this

LOG="${TRADINEBOTTE_CLEANUP_LOG:-$HOME/cleanup_old_heartbeat_db.log}"
log() { printf '%s  %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$LOG"; }

CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
if [[ ! -f "$CONF" ]]; then log "ABORT: conf not found: $CONF"; exit 1; fi
# shellcheck source=/dev/null
source "$CONF"
SERVER="${TEST_SERVER:?TEST_SERVER missing}"; PORT="${TEST_PORT:-22}"
USER0="${TEST_USERS[0]:?TEST_USERS missing}"; PASS0="${TEST_PASSWORDS[0]:?TEST_PASSWORDS missing}"

if ! command -v sshpass >/dev/null; then log "ABORT: sshpass not installed"; exit 1; fi

mkdir -p ~/.ssh/cm-sockets && chmod 700 ~/.ssh/cm-sockets
_ssh() {
    # ControlMaster: this script calls _ssh twice (gate + delete) on the same account —
    # reuse one authenticated connection instead of the ~13s password-auth cost each time.
    SSHPASS="$PASS0" sshpass -e ssh \
        -o StrictHostKeyChecking=yes -o ConnectTimeout=15 \
        -o PreferredAuthentications=password \
        -o ControlMaster=auto -o "ControlPath=$HOME/.ssh/cm-sockets/%C" -o ControlPersist=10m \
        -p "$PORT" "$USER0@$SERVER" "$@" 2>&1
}

log "=== cleanup_old_heartbeat_db (dry_run=$DRY_RUN) ==="

# ── Safety gate (read-only, on the collector account) ─────────────────────────
# \$HOME / \$OLD are evaluated on the REMOTE; the numeric thresholds are substituted
# locally and passed to python as argv.
GATE=$(_ssh "
OLD=\"\$HOME/$OLD_REL\"
python3 - \"\$OLD\" \"$SHARED_DB\" $EXPECTED_ROWS $EXPECTED_MTIME $FRESH_S <<'PY'
import sqlite3, time, os, sys
OLD, SHARED, exp_rows, exp_mtime, fresh_s = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
def q(p, sql):
    d = sqlite3.connect('file:%s?mode=ro' % p, uri=True)
    try: return d.execute(sql).fetchone()
    finally: d.close()
fail = []
try:
    n, mx = q(SHARED, 'SELECT count(*),max(ts) FROM heartbeats')
    age = int(time.time()) - int(mx or 0)
    if n < exp_rows: fail.append('shared rows %d < %d' % (n, exp_rows))
    if age > fresh_s: fail.append('shared newest heartbeat %ds old (> %d)' % (age, fresh_s))
    print('SHARED rows=%d newest_age_s=%d' % (n, age))
except Exception as e:
    fail.append('shared DB unreadable: %r' % e)
try:
    on, = q(OLD, 'SELECT count(*) FROM heartbeats')
    omt = int(os.stat(OLD).st_mtime)
    if on != exp_rows: fail.append('old rows %d != %d' % (on, exp_rows))
    if omt != exp_mtime: fail.append('old mtime %d != %d (was written!)' % (omt, exp_mtime))
    print('OLD rows=%d mtime=%d' % (on, omt))
except FileNotFoundError:
    fail.append('old file already gone: nothing to delete')
except Exception as e:
    fail.append('old DB unreadable: %r' % e)
print('GATE_FAIL: ' + ' | '.join(fail) if fail else 'GATE_OK')
PY
if lsof -- \"\$OLD\" >/dev/null 2>&1; then echo 'OPEN_FAIL: old file is held open by a process'; else echo 'OPEN_OK'; fi
")

log "gate output:"; printf '%s\n' "$GATE" | sed 's/^/    /' | tee -a "$LOG" >/dev/null

if ! grep -q 'GATE_OK' <<<"$GATE" || ! grep -q 'OPEN_OK' <<<"$GATE"; then
    log "ABORT: safety gate failed — old backup NOT deleted."
    exit 1
fi
log "safety gate PASSED."

if [[ "$DRY_RUN" == true ]]; then
    log "dry-run: would delete the old heartbeat.db (+ -wal/-shm). No changes made."
    exit 0
fi

# ── Delete (gate passed) ──────────────────────────────────────────────────────
DEL=$(_ssh "
OLD=\"\$HOME/$OLD_REL\"
rm -fv \"\$OLD\" \"\$OLD-wal\" \"\$OLD-shm\" 2>&1
echo '---'
ls -l \"\$OLD\" 2>&1 || echo 'confirmed gone'
")
log "delete output:"; printf '%s\n' "$DEL" | sed 's/^/    /' | tee -a "$LOG" >/dev/null

if grep -q 'confirmed gone' <<<"$DEL"; then
    log "SUCCESS: old heartbeat.db backup removed on the collector account."
    exit 0
else
    log "WARNING: deletion may not have completed — check manually."
    exit 1
fi
