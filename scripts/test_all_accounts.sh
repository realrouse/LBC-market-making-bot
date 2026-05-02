#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  test_all_accounts.sh — Clean install + test on all configured accounts
#
#  For each account (in order):
#    1. Kill any running bot
#    2. Wipe ~/tradinebotte and ~/account-sim
#    3. rsync latest repo from local machine
#    4. bash scripts/install.sh --with-tests  (creates venv + runs tests)
#    5. Wait DELAY seconds before moving to the next account
#
#  Configuration (required — same file as test_multibot_deploy.sh):
#    cp scripts/test_multibot.conf.example ~/.tradinebotte-test.conf
#    editor ~/.tradinebotte-test.conf   # set TEST_SERVER, TEST_PORT, TEST_USERS, TEST_PASSWORDS
#
#  Usage:
#    bash scripts/test_all_accounts.sh            # default 180s delay
#    bash scripts/test_all_accounts.sh --delay 60 # custom delay
#    bash scripts/test_all_accounts.sh --no-wait  # no delay between accounts
#
#  Custom conf location:
#    TEST_MULTIBOT_CONF=/path/to/conf bash scripts/test_all_accounts.sh
#
#  Requirements: sshpass (apt install sshpass)
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

DELAY=180   # seconds between accounts (default 3 min)

# ── Argument parsing ──────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --delay)   DELAY="$2"; shift 2 ;;
        --no-wait) DELAY=0;    shift   ;;
        *) echo "Usage: $0 [--delay SECONDS] [--no-wait]" >&2; exit 1 ;;
    esac
done

# ── Load configuration ────────────────────────────────────────────
BOLD="\033[1m"; GREEN="\033[32m"; RED="\033[31m"; YELLOW="\033[33m"; NC="\033[0m"

CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
if [[ ! -f "$CONF" ]]; then
    echo -e "${RED}Configuration manquante : $CONF${NC}"
    echo ""
    echo "Créer le fichier de configuration depuis le template :"
    echo "  cp scripts/test_multibot.conf.example ~/.tradinebotte-test.conf"
    echo "  editor ~/.tradinebotte-test.conf"
    echo ""
    echo "Ou pointer vers un fichier personnalisé :"
    echo "  TEST_MULTIBOT_CONF=/chemin/vers/conf bash scripts/test_all_accounts.sh"
    exit 1
fi
# shellcheck source=/dev/null
source "$CONF"

HOST="${TEST_SERVER:?TEST_SERVER manquant dans $CONF}"
PORT="${TEST_PORT:-22}"
USERS=("${TEST_USERS[@]:?TEST_USERS manquant dans $CONF}")
PASSWORDS=("${TEST_PASSWORDS[@]:?TEST_PASSWORDS manquant dans $CONF}")

if [[ ${#USERS[@]} -ne ${#PASSWORDS[@]} ]; then
    echo -e "${RED}TEST_USERS et TEST_PASSWORDS doivent avoir la même longueur.${NC}" >&2
    exit 1
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
    sshpass -p "$pwd" ssh -p "$PORT" -o StrictHostKeyChecking=no \
        -o ConnectTimeout=10 "${user}@${HOST}" "$@"
}

rsync_to() {
    local user="$1" pwd="$2"
    sshpass -p "$pwd" rsync -a \
        --exclude='*.db' --exclude='__pycache__' \
        --exclude='.git' --exclude='venv' \
        -e "ssh -p $PORT -o StrictHostKeyChecking=no" \
        "$REPO_DIR/" "${user}@${HOST}:/home/${user}/tradinebotte/"
}

# ── Check sshpass is available ────────────────────────────────────
if ! command -v sshpass &>/dev/null; then
    echo -e "${RED}ERROR: sshpass not found. Install with: sudo apt install sshpass${NC}" >&2
    exit 1
fi

# ── Main loop ─────────────────────────────────────────────────────
RESULTS=()
START_TOTAL=$(date +%s)
TOTAL=${#USERS[@]}

for i in "${!USERS[@]}"; do
    USER="${USERS[$i]}"
    PWD="${PASSWORDS[$i]}"
    ACCOUNT_NUM=$((i + 1))

    section "COMPTE ${ACCOUNT_NUM}/${TOTAL} — ${USER}@${HOST}"

    # ── Step 1: wipe ────────────────────────────────────────────
    info "Arrêt du bot..."
    ssh_run "$USER" "$PWD" "pkill -f live_bot.py 2>/dev/null; echo ok" 2>/dev/null || true

    info "Nettoyage du compte..."
    ssh_run "$USER" "$PWD" "rm -rf ~/tradinebotte ~/account-sim"
    ok "Compte vidé"

    # ── Step 2: rsync ───────────────────────────────────────────
    info "Rsync de la dernière version..."
    rsync_to "$USER" "$PWD"
    ok "Rsync terminé"

    # ── Step 3: install + tests ─────────────────────────────────
    info "Installation et tests (install.sh --with-tests)..."
    INSTALL_OUT=$(ssh_run "$USER" "$PWD" \
        "cd /home/${USER}/tradinebotte && bash scripts/install.sh --with-tests 2>&1")

    if echo "$INSTALL_OUT" | grep -q "^Ran [0-9]* tests"; then
        TEST_LINE=$(echo "$INSTALL_OUT" | grep "^Ran [0-9]* tests")
        if echo "$INSTALL_OUT" | grep -qE "^OK$"; then
            ok "Tests : ${TEST_LINE} — OK"
            RESULTS+=("${USER}: OK")
        else
            FAIL_LINE=$(echo "$INSTALL_OUT" | grep -E "^FAILED" | head -1)
            err "Tests : ${TEST_LINE} — ${FAIL_LINE}"
            RESULTS+=("${USER}: FAILED — ${FAIL_LINE}")
            echo "$INSTALL_OUT" | grep -A5 "^ERROR\|^FAIL:" | head -30 || true
        fi
    else
        err "install.sh a échoué avant les tests"
        RESULTS+=("${USER}: INSTALL ERROR")
        echo "$INSTALL_OUT" | tail -20
    fi

    # ── Step 4: wait before next account ────────────────────────
    if [[ $ACCOUNT_NUM -lt $TOTAL && $DELAY -gt 0 ]]; then
        info "Pause ${DELAY}s avant le prochain compte..."
        sleep "$DELAY"
    fi
done

# ── Summary ───────────────────────────────────────────────────────
ELAPSED=$(( $(date +%s) - START_TOTAL ))
section "RÉSUMÉ — ${ELAPSED}s total"

ALL_OK=true
for r in "${RESULTS[@]}"; do
    if [[ "$r" == *": OK" ]]; then
        ok "$r"
    else
        err "$r"
        ALL_OK=false
    fi
done

echo ""
if $ALL_OK; then
    echo -e "${GREEN}${BOLD}Tous les comptes : installation et tests OK.${NC}"
    exit 0
else
    echo -e "${RED}${BOLD}Certains comptes ont échoué.${NC}"
    exit 1
fi
