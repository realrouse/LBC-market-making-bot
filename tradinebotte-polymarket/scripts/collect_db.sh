#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  collect_db.sh — Download the weekly live.db from the collector
#
#  Downloads ~/tradinebotte-collector/live.db from the first deployment
#  account and saves it locally as:
#    data/live_YYYY_WNN.db   (ISO year + week number)
#
#  Reads credentials from ~/.tradinebotte-test.conf.
#
#  Usage:
#    bash scripts/collect_db.sh [options]
#
#  Options:
#    --rotate        archive current remote DB (rename to live_YYYYWNN.db.bak)
#                    and restart the collector after download
#    --output DIR    local destination directory (default: ./data)
#    --dry-run       print commands without executing
#    --status        show remote DB size + row counts (no download)
#    --yes           skip overwrite confirmation (for cron/non-interactive use)
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────
ROTATE=0
OUTPUT_DIR="$(cd "$(dirname "$0")/.." && pwd)/data"
DRY_RUN=0
STATUS_ONLY=0
YES=0

_prev=""
for _arg in "$@"; do
    case "$_arg" in
        --rotate)   ROTATE=1 ;;
        --dry-run)  DRY_RUN=1 ;;
        --status)   STATUS_ONLY=1 ;;
        --yes)      YES=1 ;;
    esac
    if [ "$_prev" = "--output" ]; then
        OUTPUT_DIR="$_arg"
    fi
    _prev="$_arg"
done

# ── Load conf ─────────────────────────────────────────────────────
CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
if [ ! -f "$CONF" ]; then
    echo "❌ ERROR: $CONF not found."
    echo "   Copy scripts/test_multibot.conf.example → ~/.tradinebotte-test.conf"
    exit 1
fi
# shellcheck source=/dev/null
source "$CONF"

SERVER="${TEST_SERVER:?TEST_SERVER not set in $CONF}"
PORT="${TEST_PORT:-22}"
USER="${TEST_USERS[0]:?TEST_USERS[0] not set in $CONF}"
PASSWORD="${TEST_PASSWORDS[0]:?TEST_PASSWORDS[0] not set in $CONF}"
COLLECTOR_DIR="${TEST_REMOTE_COLLECTOR_DIR:-~/tradinebotte-collector}"
INSTALL_DIR="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"

# Populate known_hosts so SSH calls use StrictHostKeyChecking=yes safely
mkdir -p ~/.ssh && chmod 700 ~/.ssh
if ! ssh-keygen -F "[$SERVER]:$PORT" &>/dev/null && ! ssh-keygen -F "$SERVER" &>/dev/null; then
    ssh-keyscan -p "$PORT" -H "$SERVER" >> ~/.ssh/known_hosts 2>/dev/null
fi

# ── Output filename ───────────────────────────────────────────────
YEAR=$(date +%Y)
WEEK=$(date +%V)
FILENAME="live_${YEAR}_W${WEEK}.db"
LOCAL_PATH="$OUTPUT_DIR/$FILENAME"

# ── Helpers ───────────────────────────────────────────────────────
_ssh() {
    if [ "$DRY_RUN" = "1" ]; then
        echo "[DRY-RUN] ssh $USER@$SERVER $*"
        return 0
    fi
    SSHPASS="$PASSWORD" sshpass -e ssh -p "$PORT" \
        -o StrictHostKeyChecking=yes \
        -o ConnectTimeout=10 \
        "$USER@$SERVER" "$@"
}

_scp() {
    if [ "$DRY_RUN" = "1" ]; then
        echo "[DRY-RUN] scp $USER@$SERVER:$1 $2"
        return 0
    fi
    SSHPASS="$PASSWORD" sshpass -e scp -P "$PORT" \
        -o StrictHostKeyChecking=yes \
        "$USER@$SERVER:$1" "$2"
}

# ── --status ──────────────────────────────────────────────────────
if [ "$STATUS_ONLY" = "1" ]; then
    echo "Remote DB status ($SERVER / $USER):"
    _ssh "
        DB='$COLLECTOR_DIR/live.db'
        if [ ! -f \"\$DB\" ]; then
            echo '  live.db: not found'
            exit 0
        fi
        SIZE=\$(du -h \"\$DB\" | cut -f1)
        echo \"  Size: \$SIZE\"
        PYTHON='$INSTALL_DIR/venv/bin/python3'
        \"\$PYTHON\" - \"\$DB\" <<'PYEOF'
import sys, sqlite3
db = sqlite3.connect(sys.argv[1])
trades = db.execute(\"SELECT COUNT(*) FROM trades\").fetchone()[0]
snaps  = db.execute(\"SELECT COUNT(*) FROM snapshots\").fetchone()[0]
wins   = db.execute(\"SELECT COUNT(*) FROM trades WHERE outcome='WIN'\").fetchone()[0]
losses = db.execute(\"SELECT COUNT(*) FROM trades WHERE outcome='LOSS'\").fetchone()[0]
print(f'  Trades: {trades}  (WIN: {wins}  LOSS: {losses})')
print(f'  Snapshots: {snaps:,}')
PYEOF
    "
    exit 0
fi

# ── Check for existing local file ────────────────────────────────
mkdir -p "$OUTPUT_DIR"
if [ -f "$LOCAL_PATH" ] && [ "$YES" = "0" ]; then
    echo "⚠️  $LOCAL_PATH already exists — overwrite? (yes/N)"
    read -r _confirm
    if [ "$_confirm" != "yes" ]; then
        echo "Cancelled."
        exit 0
    fi
fi

# ── Download ──────────────────────────────────────────────────────
echo "Downloading $COLLECTOR_DIR/live.db → $LOCAL_PATH ..."
_scp "$COLLECTOR_DIR/live.db" "$LOCAL_PATH"
echo "✅ Saved: $LOCAL_PATH"

# Print quick stats on the downloaded DB
if [ "$DRY_RUN" = "0" ] && command -v python3 &>/dev/null; then
    python3 - "$LOCAL_PATH" <<'PYEOF'
import sys, sqlite3
try:
    db = sqlite3.connect(sys.argv[1])
    trades = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    snaps  = db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    wins   = db.execute("SELECT COUNT(*) FROM trades WHERE outcome='WIN'").fetchone()[0]
    losses = db.execute("SELECT COUNT(*) FROM trades WHERE outcome='LOSS'").fetchone()[0]
    print(f"   Trades: {trades}  (WIN: {wins}  LOSS: {losses})")
    print(f"   Snapshots: {snaps:,}")
except Exception as e:
    print(f"   (stats unavailable: {e})")
PYEOF
fi

# ── --rotate ──────────────────────────────────────────────────────
if [ "$ROTATE" = "1" ]; then
    echo "Rotating remote DB and restarting collector..."
    # shellcheck disable=SC1083,SC2140
    _ssh "
        DB=$COLLECTOR_DIR/live.db
        BAK="\${DB%.db}_${YEAR}_W${WEEK}.db.bak"
        if [ -f "\$DB" ]; then
            cp "\$DB" "\$BAK"
            echo "  Archived: \$BAK"
        fi
        PID_FILE=$COLLECTOR_DIR/live.pid
        if [ -f "\$PID_FILE" ]; then
            PID=\$(cat "\$PID_FILE")
            if kill -0 "\$PID" 2>/dev/null; then
                kill "\$PID" 2>/dev/null && echo "  Stopped collector pid=\$PID" || true
            fi
            rm -f "\$PID_FILE"
        fi
        sleep 2
        rm -f "\$DB"
        PYTHON=$INSTALL_DIR/venv/bin/python3
        LOG=$COLLECTOR_DIR/live.log
        export TRADINEBOTTE_DIR=$COLLECTOR_DIR
        cd $INSTALL_DIR
        nohup "\$PYTHON" $INSTALL_DIR/live_bot.py \
            --simulate --snapshot-interval 1 \
            </dev/null >> "\$LOG" 2>&1 &
        CPID=\$!
        disown \$CPID
        echo \$CPID > "\$PID_FILE"
        sleep 3
        if kill -0 "\$CPID" 2>/dev/null; then
            echo \"✅ Collector restarted — PID: \$CPID\"
        else
            echo '❌ Collector failed to restart'
            exit 1
        fi
    "
fi
