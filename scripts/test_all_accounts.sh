#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  test_all_accounts.sh — Clean install + test on all configured accounts
#
#  For each account:
#    1. Kill any running bot
#    2. Wipe ~/tradinebotte and ~/account-sim
#    3. rsync latest repo from local machine
#    4. bash scripts/install.sh --with-tests  (creates venv + runs tests)
#
#  Configuration (required — same file as test_multibot_deploy.sh):
#    cp scripts/test_multibot.conf.example ~/.tradinebotte-test.conf
#    editor ~/.tradinebotte-test.conf   # set TEST_SERVER, TEST_PORT, TEST_USERS, TEST_PASSWORDS
#
#  Usage:
#    bash scripts/test_all_accounts.sh             # sequential, 180s delay
#    bash scripts/test_all_accounts.sh --parallel  # all accounts at the same time
#    bash scripts/test_all_accounts.sh --delay 60  # custom delay (sequential only)
#    bash scripts/test_all_accounts.sh --no-wait   # no delay between accounts
#
#  Custom conf location:
#    TEST_MULTIBOT_CONF=/path/to/conf bash scripts/test_all_accounts.sh
#
#  Requirements: sshpass (apt install sshpass)
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
TMPDIR_LOCAL="${HOME}/tmp/test-all-accounts"

DELAY=180
PARALLEL=false

# ── Argument parsing ──────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --parallel) PARALLEL=true; shift ;;
        --delay)    DELAY="$2";    shift 2 ;;
        --no-wait)  DELAY=0;       shift ;;
        *) echo "Usage: $0 [--parallel] [--delay SECONDS] [--no-wait]" >&2; exit 1 ;;
    esac
done

# ── Load configuration ────────────────────────────────────────────
BOLD="\033[1m"; GREEN="\033[32m"; RED="\033[31m"; YELLOW="\033[33m"; CYAN="\033[36m"; NC="\033[0m"

CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
if [[ ! -f "$CONF" ]]; then
    echo -e "${RED}Missing configuration: $CONF${NC}"
    echo ""
    echo "Create the config file from the template:"
    echo "  cp scripts/test_multibot.conf.example ~/.tradinebotte-test.conf"
    echo "  editor ~/.tradinebotte-test.conf"
    echo ""
    echo "Or point to a custom file:"
    echo "  TEST_MULTIBOT_CONF=/path/to/conf bash scripts/test_all_accounts.sh"
    exit 1
fi
# shellcheck source=/dev/null
source "$CONF"

HOST="${TEST_SERVER:?TEST_SERVER missing in $CONF}"
PORT="${TEST_PORT:-22}"
USERS=("${TEST_USERS[@]:?TEST_USERS missing in $CONF}")
PASSWORDS=("${TEST_PASSWORDS[@]:?TEST_PASSWORDS missing in $CONF}")

if [[ ${#USERS[@]} -ne ${#PASSWORDS[@]} ]]; then
    echo -e "${RED}TEST_USERS and TEST_PASSWORDS must have the same length.${NC}" >&2
    exit 1
fi

# Populate known_hosts so SSH calls use StrictHostKeyChecking=yes safely
mkdir -p ~/.ssh && chmod 700 ~/.ssh
if ! ssh-keygen -F "[$HOST]:$PORT" &>/dev/null && ! ssh-keygen -F "$HOST" &>/dev/null; then
    ssh-keyscan -p "$PORT" -H "$HOST" >> ~/.ssh/known_hosts 2>/dev/null
fi

# ── Helpers ───────────────────────────────────────────────────────
ok()      { echo -e "  ${GREEN}✓${NC} $*"; }
err()     { echo -e "  ${RED}✗${NC} $*"; }
info()    { echo -e "  ${YELLOW}→${NC} $*"; }
section() {
    echo ""
    echo -e "${BOLD}══════════════════════════════════════════${NC}"
    echo -e "${BOLD}  $*${NC}"
    echo -e "${BOLD}══════════════════════════════════════════${NC}"
}

ssh_run() {
    local user="$1" pwd="$2"; shift 2
    SSHPASS="$pwd" sshpass -e ssh -p "$PORT" -o StrictHostKeyChecking=yes \
        -o ConnectTimeout=10 "${user}@${HOST}" "$@"
}

rsync_to() {
    local user="$1" pwd="$2"
    SSHPASS="$pwd" sshpass -e rsync -a \
        --exclude='*.db' --exclude='__pycache__' \
        --exclude='.git' --exclude='venv' \
        -e "ssh -p $PORT -o StrictHostKeyChecking=yes" \
        "$REPO_DIR/" "${user}@${HOST}:/home/${user}/tradinebotte/"
}

# ── Per-account worker (used in both sequential and parallel modes) ──
# Writes human-readable log to LOG_FILE and a one-line result to RESULT_FILE.
run_account() {
    local user="$1" pwd="$2" log_file="$3" result_file="$4"
    # Disable set -e inside the worker: each step handles its own errors,
    # and we must always reach the result-write at the end.
    set +e

    _ssh() { SSHPASS="$pwd" sshpass -e ssh -p "$PORT" -o StrictHostKeyChecking=yes \
                 -o ConnectTimeout=15 "${user}@${HOST}" "$@"; }
    _rsync() {
        SSHPASS="$pwd" sshpass -e rsync -a \
            --exclude='*.db' --exclude='__pycache__' \
            --exclude='.git' --exclude='venv' \
            -e "ssh -p $PORT -o StrictHostKeyChecking=yes" \
            "$REPO_DIR/" "${user}@${HOST}:/home/${user}/tradinebotte/"
    }

    {
        echo "=== START $(date '+%H:%M:%S') — ${user}@${HOST} ==="

        echo "→ Stopping bot..."
        _ssh "pkill -f '[l]ive_bot\.py' 2>/dev/null; true" 2>/dev/null

        echo "→ Cleaning account..."
        if _ssh "rm -rf ~/tradinebotte ~/account-sim"; then
            echo "✓ Account wiped"
        else
            echo "✗ Cleanup failed"
            echo "WIPE ERROR" > "$result_file"
            echo "=== END $(date '+%H:%M:%S') — ${user} ==="
        fi

        echo "→ Rsyncing latest version..."
        if _rsync; then
            echo "✓ Rsync done"
        else
            echo "✗ Rsync failed"
            echo "RSYNC ERROR" > "$result_file"
            echo "=== END $(date '+%H:%M:%S') — ${user} ==="
        fi

        echo "→ Installation et tests..."
        INSTALL_OUT=$(_ssh \
            "cd /home/${user}/tradinebotte && bash scripts/install.sh --with-tests 2>&1")
        local rc=$?
        echo "$INSTALL_OUT"

        if [[ $rc -ne 0 ]] && ! echo "$INSTALL_OUT" | grep -q "^Ran [0-9]* tests"; then
            echo "✗ install.sh failed (exit $rc)"
            echo "INSTALL ERROR (exit $rc)" > "$result_file"
        elif echo "$INSTALL_OUT" | grep -q "^Ran [0-9]* tests"; then
            TEST_LINE=$(echo "$INSTALL_OUT" | grep "^Ran [0-9]* tests")
            if echo "$INSTALL_OUT" | grep -qE "^OK( |$)"; then
                echo "✓ ${TEST_LINE} — OK"
                echo "OK" > "$result_file"
            else
                FAIL_LINE=$(echo "$INSTALL_OUT" | grep -E "^FAILED" | head -1)
                echo "✗ ${TEST_LINE} — ${FAIL_LINE}"
                echo "FAILED — ${FAIL_LINE}" > "$result_file"
            fi
        else
            echo "✗ install.sh produced no test output"
            echo "NO OUTPUT" > "$result_file"
        fi

        echo "=== END $(date '+%H:%M:%S') — ${user} ==="
    } >> "$log_file" 2>&1
}

# ── Check sshpass ─────────────────────────────────────────────────
if ! command -v sshpass &>/dev/null; then
    echo -e "${RED}ERROR: sshpass not found. Install with: sudo apt install sshpass${NC}" >&2
    exit 1
fi

mkdir -p "$TMPDIR_LOCAL"
START_TOTAL=$(date +%s)
TOTAL=${#USERS[@]}

# ── Parallel mode ─────────────────────────────────────────────────
if $PARALLEL; then
    section "PARALLEL MODE — ${TOTAL} accounts on ${HOST}"
    PIDS=()
    LOG_FILES=()
    RESULT_FILES=()

    for i in "${!USERS[@]}"; do
        USER="${USERS[$i]}"
        PWD="${PASSWORDS[$i]}"
        LOG_FILE="${TMPDIR_LOCAL}/${USER}.log"
        RESULT_FILE="${TMPDIR_LOCAL}/${USER}.result"
        > "$LOG_FILE"
        rm -f "$RESULT_FILE"
        LOG_FILES+=("$LOG_FILE")
        RESULT_FILES+=("$RESULT_FILE")

        info "Launching ${USER} in background..."
        run_account "$USER" "$PWD" "$LOG_FILE" "$RESULT_FILE" &
        PIDS+=($!)
    done

    echo ""
    info "Waiting for ${TOTAL} parallel jobs to complete..."
    for pid in "${PIDS[@]}"; do
        wait "$pid" || true
    done

    # Print logs in order
    for i in "${!USERS[@]}"; do
        section "LOG — ${USERS[$i]}"
        cat "${LOG_FILES[$i]}"
    done

# ── Sequential mode ───────────────────────────────────────────────
else
    for i in "${!USERS[@]}"; do
        USER="${USERS[$i]}"
        PWD="${PASSWORDS[$i]}"
        ACCOUNT_NUM=$((i + 1))
        LOG_FILE="${TMPDIR_LOCAL}/${USER}.log"
        RESULT_FILE="${TMPDIR_LOCAL}/${USER}.result"
        > "$LOG_FILE"
        rm -f "$RESULT_FILE"

        section "COMPTE ${ACCOUNT_NUM}/${TOTAL} — ${USER}@${HOST}"
        run_account "$USER" "$PWD" "$LOG_FILE" "$RESULT_FILE"
        cat "$LOG_FILE"

        if [[ $ACCOUNT_NUM -lt $TOTAL && $DELAY -gt 0 ]]; then
            info "Pausing ${DELAY}s before next account..."
            sleep "$DELAY"
        fi
    done
fi

# ── Summary ───────────────────────────────────────────────────────
ELAPSED=$(( $(date +%s) - START_TOTAL ))
section "SUMMARY — ${ELAPSED}s total"

ALL_OK=true
for i in "${!USERS[@]}"; do
    USER="${USERS[$i]}"
    RESULT_FILE="${TMPDIR_LOCAL}/${USER}.result"
    if [[ -f "$RESULT_FILE" ]]; then
        RESULT=$(cat "$RESULT_FILE")
    else
        RESULT="NO RESULT (job crash?)"
    fi
    if [[ "$RESULT" == "OK" ]]; then
        ok "${USER}: ${RESULT}"
    else
        err "${USER}: ${RESULT}"
        ALL_OK=false
    fi
done

echo ""
if $ALL_OK; then
    echo -e "${GREEN}${BOLD}All accounts: install and tests OK.${NC}"
    exit 0
else
    echo -e "${RED}${BOLD}Some accounts failed.${NC}"
    exit 1
fi
