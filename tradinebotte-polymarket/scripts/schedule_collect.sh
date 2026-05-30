#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  schedule_collect.sh — Manage the weekly data-collection cron job
#
#  Installs, removes, or shows a crontab entry that runs collect_db.sh
#  automatically every week (Sunday 03:00 UTC by default).
#
#  The cron job:
#    1. Downloads live.db from the collector server → data/live_YYYY_WNN.db
#    2. Archives the remote DB and restarts the collector (--rotate)
#    3. Appends output to ~/tradinebotte/collect.log
#
#  Usage:
#    bash scripts/schedule_collect.sh --install    # add cron entry
#    bash scripts/schedule_collect.sh --remove     # remove cron entry
#    bash scripts/schedule_collect.sh --status     # show cron entry
#    bash scripts/schedule_collect.sh --run-now    # run immediately (dry preview)
#
#  Optional:
#    --hour H    UTC hour for the weekly run (default: 3)
#    --day  D    day of week: 0=Sun … 6=Sat (default: 0)
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
COLLECT_SCRIPT="$REPO_DIR/scripts/collect_db.sh"
LOG_FILE="$HOME/tradinebotte/collect.log"

ACTION=""
HOUR=3
DOW=0   # day-of-week: 0=Sunday

_prev=""
for _arg in "$@"; do
    case "$_arg" in
        --install)  ACTION=install ;;
        --remove)   ACTION=remove ;;
        --status)   ACTION=status ;;
        --run-now)  ACTION=run-now ;;
    esac
    [ "$_prev" = "--hour" ] && HOUR="$_arg"
    [ "$_prev" = "--day"  ] && DOW="$_arg"
    _prev="$_arg"
done

if [ -z "$ACTION" ]; then
    echo "Usage: bash scripts/schedule_collect.sh --install | --remove | --status | --run-now"
    echo "       [--hour H] [--day D]   (H=UTC hour 0-23, D=day-of-week 0=Sun)"
    exit 1
fi

# The cron line we manage — identified by the unique marker comment
MARKER="# tradinebotte-collect"
CRON_CMD="0 $HOUR * * $DOW  cd \"$REPO_DIR\" && bash scripts/collect_db.sh --rotate --yes >> \"$LOG_FILE\" 2>&1  $MARKER"

# ── --status ──────────────────────────────────────────────────────
if [ "$ACTION" = "status" ]; then
    if crontab -l 2>/dev/null | grep -q "$MARKER"; then
        echo "✅ Cron job installed:"
        crontab -l 2>/dev/null | grep "$MARKER"
    else
        echo "⭕ No tradinebotte-collect cron job found."
        echo "   Run: bash scripts/schedule_collect.sh --install"
    fi
    exit 0
fi

# ── --remove ──────────────────────────────────────────────────────
if [ "$ACTION" = "remove" ]; then
    if ! crontab -l 2>/dev/null | grep -q "$MARKER"; then
        echo "⭕ No tradinebotte-collect cron job to remove."
        exit 0
    fi
    { crontab -l 2>/dev/null || true; } | { grep -v "$MARKER" || true; } | crontab -
    echo "✅ Cron job removed."
    exit 0
fi

# ── --run-now ─────────────────────────────────────────────────────
if [ "$ACTION" = "run-now" ]; then
    echo "Running collect_db.sh --rotate --yes now..."
    mkdir -p "$(dirname "$LOG_FILE")"
    bash "$COLLECT_SCRIPT" --rotate --yes 2>&1 | tee -a "$LOG_FILE"
    exit 0
fi

# ── --install ─────────────────────────────────────────────────────
# Remove any existing entry first, then append new one (idempotent)
{ { crontab -l 2>/dev/null || true; } | { grep -v "$MARKER" || true; }; echo "$CRON_CMD"; } | crontab -

echo "✅ Cron job installed (every $([ "$DOW" = "0" ] && echo Sunday || echo "day $DOW") at ${HOUR}:00 UTC):"
echo "   $CRON_CMD"
echo ""
echo "   Log: $LOG_FILE"
echo "   Remove: bash scripts/schedule_collect.sh --remove"
echo "   Run now: bash scripts/schedule_collect.sh --run-now"
