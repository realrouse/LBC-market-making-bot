#!/usr/bin/env bash
# prepare_release.sh — pre-release checklist gate.
#
# Run this before every merge to main.  Aborts on blocking failures.
#
# Versioning (x.y.z):
#   - Only a GitHub Release bumps the minor (x.y): e.g. 0.87 -> 0.88.
#   - Work merged to main WITHOUT a GitHub Release bumps the patch (z):
#     0.87 -> 0.87.1 -> 0.87.2 …  The CHANGELOG header + --tag take the
#     full x.y.z string (e.g. --tag v0.87.1); this script is format-agnostic.
#
# Usage:
#   bash scripts/prepare_release.sh
#   bash scripts/prepare_release.sh --skip-integration
#   bash scripts/prepare_release.sh --tag v0.87.1

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# ── Argument parsing ────────────────────────────────────────────────
SKIP_INTEGRATION=false
GIT_TAG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-integration) SKIP_INTEGRATION=true ;;
        --tag)
            if [[ $# -lt 2 ]]; then
                printf 'ERROR: --tag requires a version argument (e.g. --tag v0.82)\n' >&2
                exit 1
            fi
            GIT_TAG="$2"
            shift
            ;;
        *)
            printf 'Unknown option: %s\n' "$1" >&2
            exit 1
            ;;
    esac
    shift
done

# ── Virtual environment ─────────────────────────────────────────────
if [[ -d "$PROJECT_DIR/.venv" ]]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python3"
elif [[ -d "${TRADINEBOTTE_DIR:-$HOME/tradinebotte}/venv" ]]; then
    PYTHON="${TRADINEBOTTE_DIR:-$HOME/tradinebotte}/venv/bin/python3"
else
    printf 'ERROR: no virtual environment found.\n' >&2
    printf 'Create one with: bash scripts/install.sh\n' >&2
    exit 1
fi

cd "$PROJECT_DIR" || exit 1

# ── Colors (ANSI-C quoting — actual escape bytes, not literal \033) ─
GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'
RED=$'\033[0;31m'
RESET=$'\033[0m'
BOLD=$'\033[1m'

# ── State tracking ──────────────────────────────────────────────────
declare -a RESULTS=()
FAILURES=0
WARNINGS=0

_sep()  { printf '%s\n' "${BOLD}════════════════════════════════════════${RESET}"; }
_step() { printf '\n'; _sep; printf '  %s\n' "$1"; _sep; }
_pass() { printf '  %s %s\n' "${GREEN}✓${RESET}" "$1"; RESULTS+=("PASS|$1"); }
_warn() { printf '  %s  %s\n' "${YELLOW}⚠${RESET}" "$1"; RESULTS+=("WARN|$1"); WARNINGS=$((WARNINGS + 1)); }
_fail() { printf '  %s %s\n' "${RED}✗${RESET}" "$1"; RESULTS+=("FAIL|$1"); FAILURES=$((FAILURES + 1)); }
_skip() { printf '  %s  %s\n' "${YELLOW}—${RESET}" "$1"; RESULTS+=("SKIP|$1"); }

printf '\n%s\n' "${BOLD}tradinebotte — pre-release checklist${RESET}"
printf 'Python : %s (%s)\n' "$PYTHON" "$("$PYTHON" --version 2>&1)"
printf 'Date   : %s\n' "$(date +%Y-%m-%d)"

# ══════════════════════════════════════════════════════════════════════
# 1 — Unit tests (BLOCKING)
# ══════════════════════════════════════════════════════════════════════
_step "1/8  Unit tests  [BLOCKING]"

_tests_ok=true

if "$PYTHON" -W ignore::ResourceWarning -m unittest discover \
        -s tests/ -p 'test_*.py' 2>&1; then
    _pass "Core unit tests"
else
    _fail "Core unit tests FAILED"
    _tests_ok=false
fi

for _svc_dir in tradinebotte-*/tests/ tradinetools/tests/; do
    if [[ -d "$_svc_dir" ]] && compgen -G "${_svc_dir}test_*.py" > /dev/null 2>&1; then
        if "$PYTHON" -W ignore::ResourceWarning -m unittest discover \
                -s "$_svc_dir" -p 'test_*.py' 2>&1; then
            _pass "$_svc_dir"
        else
            _fail "$_svc_dir FAILED"
            _tests_ok=false
        fi
    fi
done

if [[ "$_tests_ok" == "false" ]]; then
    printf '\n%s\n' "${RED}Blocking: fix unit test failures before proceeding.${RESET}"
fi

# ══════════════════════════════════════════════════════════════════════
# 2 — Pylint (BLOCKING if score < 9.90)
# ══════════════════════════════════════════════════════════════════════
_step "2/8  Pylint  [BLOCKING if score < 9.90]"

# Exclude generated protobuf modules (*_pb2.py) — they are machine-generated and not
# style-graded; pylint scores them ~5/10 which would drag the repo gate.
mapfile -t _py_files < <(git ls-files '*.py' 2>/dev/null | grep -vE '_pb2\.py$')

if ! "$PYTHON" -m pylint --version &> /dev/null; then
    _warn "pylint not installed — skipped (pip install pylint)"
elif [[ ${#_py_files[@]} -eq 0 ]]; then
    _warn "No tracked .py files — pylint skipped"
else
    _pylint_out=$("$PYTHON" -m pylint "${_py_files[@]}" 2>&1 || true)
    printf '%s\n' "$_pylint_out"
    _score=$(printf '%s' "$_pylint_out" | \
        grep -oP 'rated at \K[0-9]+\.[0-9]+' | tail -1 || true)
    if [[ -z "$_score" ]]; then
        _warn "Could not parse pylint score"
    else
        _cmp=$(awk -v s="$_score" \
            'BEGIN { print (s+0 < 9.90) ? "low" : (s+0 < 10.00) ? "near" : "perfect" }')
        case "$_cmp" in
            low)     _fail "Pylint score ${_score}/10 — below 9.90 threshold" ;;
            near)    _warn "Pylint score ${_score}/10 — below 10.00 (acceptable)" ;;
            perfect) _pass "Pylint score ${_score}/10 — perfect" ;;
        esac
    fi
fi

# ══════════════════════════════════════════════════════════════════════
# 3 — Shellcheck (BLOCKING)
# ══════════════════════════════════════════════════════════════════════
_step "3/8  Shellcheck  [BLOCKING]"

mapfile -t _sh_files < <(git ls-files '*.sh' 2>/dev/null)

if ! command -v shellcheck &> /dev/null; then
    _warn "shellcheck not installed — skipped (apt install shellcheck)"
elif [[ ${#_sh_files[@]} -eq 0 ]]; then
    _warn "No tracked .sh files — shellcheck skipped"
else
    _sc_out=$(shellcheck -S warning "${_sh_files[@]}" 2>&1 || true)
    if [[ -n "$_sc_out" ]]; then
        printf '%s\n' "$_sc_out"
        _fail "shellcheck found issues — fix before releasing"
    else
        _pass "shellcheck clean on ${#_sh_files[@]} scripts"
    fi
fi

# ══════════════════════════════════════════════════════════════════════
# 4 — Bilingual doc pairs (BLOCKING)
# ══════════════════════════════════════════════════════════════════════
_step "4/8  Bilingual doc pairs  [BLOCKING]"

_docs_ok=true
for _base in README CHANGELOG INSTALL QUICKSTART UPDATE; do
    _en="${_base}.md"
    _fr="${_base}.fr.md"
    if [[ ! -f "$_en" ]]; then
        _fail "Missing ${_en}"
        _docs_ok=false
    fi
    if [[ ! -f "$_fr" ]]; then
        _fail "Missing ${_fr}"
        _docs_ok=false
    fi
done
if [[ "$_docs_ok" == "true" ]]; then
    _pass "All 10 bilingual doc files present"
fi

# ══════════════════════════════════════════════════════════════════════
# 5 — Inventory & legacy-ref consistency (BLOCKING)
# ══════════════════════════════════════════════════════════════════════
_step "5/8  Inventory & legacy refs  [BLOCKING]"

if bash scripts/check_no_legacy_refs.sh >/dev/null 2>&1; then
    _pass "No legacy heartbeat.db read-path references"
else
    _fail "Legacy heartbeat.db read-path(s) — run: bash scripts/check_no_legacy_refs.sh"
fi

if "$PYTHON" tradinebotte-status/check_inventory.py >/dev/null 2>&1; then
    _pass "inventory.toml valid (offline structural + deploy_all drift)"
else
    _fail "inventory.toml invalid — run: python3 tradinebotte-status/check_inventory.py"
fi

# ══════════════════════════════════════════════════════════════════════
# 6 — CHANGELOG freshness (NON-BLOCKING)
# ══════════════════════════════════════════════════════════════════════
_step "6/8  CHANGELOG freshness  [warning only]"

_today=$(date +%Y-%m-%d)
_cl_date=$(grep -oP '\d{4}-\d{2}-\d{2}' CHANGELOG.md 2>/dev/null | head -1 || true)
if [[ -z "$_cl_date" ]]; then
    _warn "Could not parse a date from CHANGELOG.md"
elif [[ "$_cl_date" == "$_today" ]]; then
    _pass "CHANGELOG.md latest entry: ${_cl_date} (today)"
else
    _warn "CHANGELOG.md latest entry: ${_cl_date} — add today's entry before releasing"
fi

# ══════════════════════════════════════════════════════════════════════
# 7 — Data quality full scan (NON-BLOCKING)
# ══════════════════════════════════════════════════════════════════════
_step "7/8  Data quality full scan  [warning only]"

if compgen -G "${PROJECT_DIR}/data/*.db" > /dev/null 2>&1; then
    if "$PYTHON" analysis/check_data_quality.py --warn-only 2>&1; then
        _pass "Data quality scan completed"
    else
        _warn "Data quality issues found — review above before releasing"
    fi
else
    _skip "No data/*.db files — data quality check skipped"
fi

# ══════════════════════════════════════════════════════════════════════
# 8 — Integration tests (NON-BLOCKING)
# ══════════════════════════════════════════════════════════════════════
_step "8/8  Integration tests  [warning only]"

_conf="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
if [[ "$SKIP_INTEGRATION" == "true" ]]; then
    _skip "Skipped via --skip-integration"
elif [[ ! -f "$_conf" ]]; then
    _skip "$HOME/.tradinebotte-test.conf not found — skipped"
elif [[ ! -f "scripts/run_integration_tests.sh" ]]; then
    _skip "scripts/run_integration_tests.sh not found — skipped"
else
    if bash scripts/run_integration_tests.sh 2>&1; then
        _pass "Integration tests passed"
    else
        _warn "Integration tests FAILED — review above before releasing"
    fi
fi

# ══════════════════════════════════════════════════════════════════════
# Optional — git tag
# ══════════════════════════════════════════════════════════════════════
if [[ -n "$GIT_TAG" ]]; then
    _step "Tag: ${GIT_TAG}"
    if git tag "$GIT_TAG" 2>&1; then
        _pass "Created tag ${GIT_TAG}"
        printf '  Push with: git push origin %s\n' "$GIT_TAG"
    else
        _warn "Failed to create tag ${GIT_TAG} — already exists?"
    fi
fi

# ══════════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════════
printf '\n'
_sep
printf '  %s\n' "${BOLD}Pre-release summary${RESET}"
_sep

for _r in "${RESULTS[@]+"${RESULTS[@]}"}"; do
    _status="${_r%%|*}"
    _label="${_r#*|}"
    case "$_status" in
        PASS) printf '  %sPASS%s  %s\n' "${GREEN}" "${RESET}" "$_label" ;;
        WARN) printf '  %sWARN%s  %s\n' "${YELLOW}" "${RESET}" "$_label" ;;
        FAIL) printf '  %sFAIL%s  %s\n' "${RED}" "${RESET}" "$_label" ;;
        SKIP) printf '  %sSKIP%s  %s\n' "${YELLOW}" "${RESET}" "$_label" ;;
    esac
done

_sep
printf '\n'

if [[ "$FAILURES" -gt 0 ]]; then
    printf '%s\n' "${RED}${BOLD}RELEASE BLOCKED — ${FAILURES} blocking failure(s). Fix all FAIL items before merging to main.${RESET}"
    printf '\n'
    exit 1
fi

if [[ "$WARNINGS" -gt 0 ]]; then
    printf '%s\n' "${YELLOW}All blocking checks passed — ${WARNINGS} warning(s). Review WARN items before merging.${RESET}"
else
    printf '%s\n' "${GREEN}${BOLD}All checks passed — ready to merge to main.${RESET}"
fi
printf '\n'
