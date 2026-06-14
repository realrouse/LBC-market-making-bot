#!/usr/bin/env bash
# check_no_legacy_refs.sh — fail if any code still READS the pre-migration heartbeat.db
# location instead of the shared state DB (/data1/tradinebotte-shared/database/tradinebotte.db,
# overridable via $TRADINEBOTTE_DB).
#
# Allowed (NOT flagged):
#   - status_collector.py's documented write fallback: os.path.join(args.dir, "heartbeat.db")
#     and the bare "heartbeat.db" filename in its docstrings (used only when --db is unset).
#   - the rsync `--exclude='heartbeat.db'` in deploy_status_service.sh.
# Flagged: any path pointing at the OLD per-account location, e.g. ~/tradinebotte/heartbeat.db
#   or ${INSTALL_DIR}/heartbeat.db used as a DB to open/query.
#
# Exit 0 = clean, 1 = legacy read-path(s) found. Wired into prepare_release.sh.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

# Stale read-path patterns pointing at the old per-account heartbeat.db.
PATTERNS='tradinebotte/heartbeat\.db|\$\{?INSTALL_DIR\}?/heartbeat\.db'

HITS=$(grep -rnE "$PATTERNS" \
         --include='*.py' --include='*.sh' --include='*.service' \
         --exclude='check_no_legacy_refs.sh' . 2>/dev/null \
       | grep -v '__pycache__' | grep -vE '/tests/|test_')

if [[ -n "$HITS" ]]; then
    echo "FAIL: legacy heartbeat.db read-path(s) found — use the shared state DB / \$TRADINEBOTTE_DB:"
    echo "$HITS" | sed 's/^/  /'
    exit 1
fi
echo "OK: no legacy heartbeat.db read-path references"
