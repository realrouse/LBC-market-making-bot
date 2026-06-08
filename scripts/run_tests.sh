#!/usr/bin/env bash
# Run the automated test suite using the project virtual environment.
#
# Usage:
#   bash scripts/run_tests.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Prefer the project .venv created with `uv venv`.
# Fall back to the production venv at TRADINEBOTTE_DIR if .venv is absent.
if [ -d "$PROJECT_DIR/.venv" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python3"
elif [ -d "${TRADINEBOTTE_DIR:-$HOME/tradinebotte}/venv" ]; then
    PYTHON="${TRADINEBOTTE_DIR:-$HOME/tradinebotte}/venv/bin/python3"
else
    echo "ERROR: no virtual environment found."
    echo "Create one with:"
    echo "  uv venv .venv && uv pip install aiohttp websockets"
    exit 1
fi

cd "$PROJECT_DIR"

# Redirect bot I/O to ~/tmp so tests never touch /opt or write credentials.
export TRADINEBOTTE_DIR="${HOME}/tmp/tradinebotte-test"

echo "Python : $PYTHON ($("$PYTHON" --version))"
echo "Tests  : $PROJECT_DIR/tests/ + tradinebotte-*/tests/"
echo ""
"$PYTHON" -W ignore::ResourceWarning -m unittest discover -s tests/ -p "test_*.py" -v
for _svc_tests in tradinebotte-*/tests/ tradinetools/tests/; do
    if [ -d "$_svc_tests" ] && ls "$_svc_tests"test_*.py >/dev/null 2>&1; then
        echo ""
        echo "=== Tests: $_svc_tests ==="
        "$PYTHON" -W ignore::ResourceWarning -m unittest discover -s "$_svc_tests" -p "test_*.py" -v
    fi
done

# ── Backtest multi-DB ─────────────────────────────────────────────
if ls "$PROJECT_DIR"/data/*.db >/dev/null 2>&1; then
    echo ""
    echo "=== Backtest multi-DB (data/*.db) ==="
    "$PYTHON" analysis/backtest.py --all \
        || { echo "⚠️  Backtest --all non-blocking — check manually"; true; }
else
    echo ""
    echo "ℹ️  No data/*.db files found — multi-DB backtest skipped"
fi

# ── Documentation audit ───────────────────────────────────────────
if command -v claude &>/dev/null; then
    echo ""
    echo "=== Audit documentation CLI ==="
    claude --agent doc-sync -p "Run the documentation audit" \
        --allowedTools "Read,Bash,Glob,Grep" \
        --dangerously-skip-permissions --print \
        || echo "⚠️  Doc audit non-blocking — check manually"
else
    echo ""
    echo "ℹ️  claude CLI not found — doc audit skipped (agent: .claude/agents/doc-sync.md)"
fi
