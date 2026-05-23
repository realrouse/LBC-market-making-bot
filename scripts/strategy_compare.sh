#!/usr/bin/env bash
# strategy_compare.sh — full strategy parameter comparison workflow.
#
# Runs a grid search over all strategy parameter combinations across all
# available databases, then a per-DB backtest vs live-bot comparison, and
# saves the combined report to reports/strategy_compare_YYYYMMDD_HHMMSS.txt
#
# Usage:
#   bash scripts/strategy_compare.sh [OPTIONS]
#
# Options:
#   --top N       show top-N unique configs in sweep table (default: 10)
#   --sort METRIC sort order: ratio|pnl|wr (default: ratio)
#   --db PATH     restrict to a specific DB file (default: all)
#   --out FILE    override output file path
#   --no-save     print to stdout only, do not write a report file
#
# Examples:
#   bash scripts/strategy_compare.sh
#   bash scripts/strategy_compare.sh --top 20 --sort pnl
#   bash scripts/strategy_compare.sh --db data/liveweek.db --no-save

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
REPORTS_DIR="$PROJECT_DIR/reports"

# ── Resolve venv python ───────────────────────────────────────────────────────
if [ -d "$PROJECT_DIR/.venv" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python3"
elif [ -d "${TRADINEBOTTE_DIR:-$HOME/tradinebotte}/venv" ]; then
    PYTHON="${TRADINEBOTTE_DIR:-$HOME/tradinebotte}/venv/bin/python3"
else
    echo "ERROR: no virtual environment found." >&2
    echo "  Create one with: uv venv .venv && uv pip install -r requirements.txt" >&2
    exit 1
fi
BACKTEST="$PYTHON $PROJECT_DIR/analysis/backtest.py"

# ── Parse arguments ───────────────────────────────────────────────────────────
TOP=10
SORT="ratio"
DB_FLAG=""
OUT_FILE=""
NO_SAVE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --top)    TOP="$2";      shift 2 ;;
        --sort)   SORT="$2";     shift 2 ;;
        --db)     DB_FLAG="--db $2"; shift 2 ;;
        --out)    OUT_FILE="$2"; shift 2 ;;
        --no-save) NO_SAVE=1;   shift ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
: "${OUT_FILE:=$REPORTS_DIR/strategy_compare_$TIMESTAMP.txt}"

# ── Helper: run and optionally tee to report file ─────────────────────────────
cd "$PROJECT_DIR"

_run() {
    if [[ $NO_SAVE -eq 1 ]]; then
        "$@"
    else
        "$@" | tee -a "$OUT_FILE"
    fi
}

# ── Create reports dir ────────────────────────────────────────────────────────
if [[ $NO_SAVE -eq 0 ]]; then
    mkdir -p "$REPORTS_DIR"
    # Write header
    {
        echo "STRATEGY PARAMETER COMPARISON REPORT"
        echo "Date    : $(date '+%Y-%m-%d %H:%M:%S')"
        echo "Python  : $PYTHON ($("$PYTHON" --version 2>&1))"
        echo "DBs     : ${DB_FLAG:-all databases in data/ + live.db}"
        echo "Top-N   : $TOP unique configs (deduped on threshold/min_secs/obi)"
        echo "Sort    : $SORT"
        echo "========================================================================"
        echo ""
    } > "$OUT_FILE"
    echo "Report  : $OUT_FILE"
    echo ""
fi

# ── Section 1: Sweep-all ──────────────────────────────────────────────────────
echo "=== SECTION 1 — GRID SEARCH (sweep-all, sort=$SORT, top=$TOP) ==="
if [[ $NO_SAVE -eq 0 ]]; then echo "=== SECTION 1 — GRID SEARCH (sort=$SORT, top=$TOP) ===" >> "$OUT_FILE"; fi

# shellcheck disable=SC2086
_run $BACKTEST ${DB_FLAG:---all} --sweep-all --sort "$SORT" --top "$TOP"

echo ""
if [[ $NO_SAVE -eq 0 ]]; then echo "" >> "$OUT_FILE"; fi

# ── Section 2: Per-DB comparison (backtest vs live bot) ───────────────────────
echo "=== SECTION 2 — PER-DB COMPARISON (backtest vs live bot) ==="
if [[ $NO_SAVE -eq 0 ]]; then echo "=== SECTION 2 — PER-DB COMPARISON (backtest vs live bot) ===" >> "$OUT_FILE"; fi

# shellcheck disable=SC2086
_run $BACKTEST ${DB_FLAG:---all} --compare

# ── Done ──────────────────────────────────────────────────────────────────────
if [[ $NO_SAVE -eq 0 ]]; then
    echo ""
    echo "Report saved: $OUT_FILE"
    echo "Lines      : $(wc -l < "$OUT_FILE")"
fi
