#!/usr/bin/env bash
# prepare_release.sh — pre-release checklist gate.
#
# Run this before every merge to main.  Aborts on blocking failures.
#
# Usage:
#   bash scripts/prepare_release.sh
#
# Steps:
#   1. Full unittest suite
#   2. Data quality check — full scan with gap detection (slow, ~22 s)
#   3. Backtest --all (non-blocking)
#   4. Pylint (non-blocking)
#   5. Reminder: integration tests if touching IPC / deploy / multibot code
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# ── Virtual environment ────────────────────────────────────────────
if [ -d "$PROJECT_DIR/.venv" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python3"
elif [ -d "${TRADINEBOTTE_DIR:-$HOME/tradinebotte}/venv" ]; then
    PYTHON="${TRADINEBOTTE_DIR:-$HOME/tradinebotte}/venv/bin/python3"
else
    echo "ERROR: no virtual environment found."
    echo "Create one with: uv venv .venv && uv pip install aiohttp websockets"
    exit 1
fi

cd "$PROJECT_DIR"
export TRADINEBOTTE_DIR="${HOME}/tmp/tradinebotte-test"

PASS=0; FAIL=0

_step() { echo ""; echo "════════════════════════════════════════"; echo "  $1"; echo "════════════════════════════════════════"; }
_ok()   { echo "  ✓ $1"; PASS=$((PASS + 1)); }
_warn() { echo "  ⚠  $1"; }
_fail() { echo "  ✗ $1"; FAIL=$((FAIL + 1)); }

echo ""
echo "tradinebotte — pre-release checklist"
echo "Python : $PYTHON ($("$PYTHON" --version))"

# ── 1. Unit tests (BLOCKING) ──────────────────────────────────────
_step "1/5  Unit tests"
if "$PYTHON" -W ignore::ResourceWarning -m unittest discover -s tests/ -p "test_*.py" -v; then
    _ok "Core unit tests passed"
else
    _fail "Core unit tests FAILED — fix before releasing"
fi
for _svc in tradinebotte-*/tests/ tradinetools/tests/; do
    if [ -d "$_svc" ] && ls "$_svc"test_*.py >/dev/null 2>&1; then
        if "$PYTHON" -W ignore::ResourceWarning -m unittest discover -s "$_svc" -p "test_*.py" -v; then
            _ok "$_svc passed"
        else
            _fail "$_svc FAILED — fix before releasing"
        fi
    fi
done

# ── 2. Data quality — full scan with gaps (NON-BLOCKING, review FAILs) ──
_step "2/5  Data quality check (full — with gap detection)"
if ls "$PROJECT_DIR"/data/*.db >/dev/null 2>&1; then
    # --warn-only: always exit 0 — FAILs are shown for human review but do not
    # block the release automatically (data issues vs code bugs require judgement).
    "$PYTHON" analysis/check_data_quality.py --warn-only
    _warn "Data quality: review any FAIL items above before releasing"
else
    _warn "No data/*.db files found — data quality check skipped"
fi

# ── 3. Backtest --all (NON-BLOCKING) ─────────────────────────────
_step "3/5  Backtest --all"
if ls "$PROJECT_DIR"/data/*.db >/dev/null 2>&1; then
    "$PYTHON" analysis/backtest.py --all \
        || _warn "Backtest --all non-blocking — check manually"
    _ok "Backtest step done"
else
    _warn "No data/*.db files — backtest skipped"
fi

# ── 4. Pylint (NON-BLOCKING) ──────────────────────────────────────
_step "4/5  Pylint"
if "$PYTHON" -m pylint --version &>/dev/null; then
    _py_files=$(git ls-files '*.py' 2>/dev/null)
    if [ -n "$_py_files" ]; then
        # shellcheck disable=SC2086
        "$PYTHON" -m pylint $_py_files \
            || _warn "Pylint warnings/errors — review before releasing"
        _ok "Pylint step done"
    else
        _warn "No tracked .py files — pylint skipped"
    fi
else
    _warn "pylint not installed — skipped (pip install -r requirements-dev.txt)"
fi

# ── 5. Integration tests reminder ─────────────────────────────────
_step "5/5  Integration tests"
if [ -f "${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}" ]; then
    echo "  SSH config found. Run integration tests if this release touches"
    echo "  multibot, IPC, or deploy code:"
    echo ""
    echo "    bash scripts/run_integration_tests.sh"
    echo ""
    _warn "Integration tests not run automatically — run manually if needed"
else
    _warn "~/.tradinebotte-test.conf absent — integration tests skipped"
fi

# ── Summary ───────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════"
echo "  Result: $PASS passed, $FAIL blocking failure(s)"
echo "════════════════════════════════════════"
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo "  RELEASE BLOCKED — fix the $FAIL failure(s) above before merging to main."
    echo ""
    exit 1
fi

echo "  All blocking checks passed."
echo "  Review any ⚠  warnings above, then merge to main."
echo ""
