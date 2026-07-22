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

# Isolation floor: point the status channel at a dead loopback port so no test can push a
# heartbeat/trade to the PROD collector on 5562 and pollute the shared state DB. The cex
# conftest.py sets the same thing, but it only covers the cex suite — this line is the
# project-wide floor for the other subprojects (poly/status/indicators/tradinetools), which
# have no conftest, in case any grows a pushing path. push_trade caches its socket from this
# env on first use, so setting it here covers every test regardless of subproject.
export TRADINEBOTTE_STATUS_ADDR="tcp://127.0.0.1:5599"

echo "Python : $PYTHON ($("$PYTHON" --version))"
echo "Tests  : $PROJECT_DIR/tests/ + tradinebotte-*/tests/"
echo ""
# pytest (not `unittest discover`) so the per-subproject conftest.py isolation ALWAYS applies —
# unittest never loads conftest, which let a test's push_trade reach the prod collector. Run one
# invocation per directory (as the old runner did) to keep each subproject's sys.path/module
# resolution and its own conftest scoped to its own tree. pytest runs the unittest.TestCase
# suites unchanged. -p no:cacheprovider keeps .pytest_cache out of every test dir.
"$PYTHON" -W ignore::ResourceWarning -m pytest tests/ -p no:cacheprovider
for _svc_tests in tradinebotte-*/tests/ tradinetools/tests/; do
    if [ -d "$_svc_tests" ] && ls "$_svc_tests"test_*.py >/dev/null 2>&1; then
        echo ""
        echo "=== Tests: $_svc_tests ==="
        "$PYTHON" -W ignore::ResourceWarning -m pytest "$_svc_tests" -p no:cacheprovider
    fi
done

# ── Data quality check ───────────────────────────────────────────
if ls "$PROJECT_DIR"/data/*.db >/dev/null 2>&1; then
    echo ""
    echo "=== Data quality check (data/*.db) ==="
    "$PYTHON" analysis/check_data_quality.py --no-gaps --warn-only \
        || { echo "⚠️  Data quality check non-blocking — check manually"; true; }
else
    echo ""
    echo "ℹ️  No data/*.db files found — data quality check skipped"
fi

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

# ── Pylint ────────────────────────────────────────────────────────
echo ""
echo "=== Pylint ==="
if "$PYTHON" -m pylint --version &>/dev/null; then
    _py_files=$(git ls-files '*.py' 2>/dev/null)
    if [ -n "$_py_files" ]; then
        # shellcheck disable=SC2086
        "$PYTHON" -m pylint $_py_files \
            || { echo "⚠️  Pylint non-blocking — check before releasing"; true; }
    else
        echo "ℹ️  No tracked Python files found — pylint skipped"
    fi
else
    echo "ℹ️  pylint not installed — skipped (pip install -r requirements-dev.txt)"
fi
