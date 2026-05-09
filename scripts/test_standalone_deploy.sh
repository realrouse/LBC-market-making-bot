#!/usr/bin/env bash
# test_standalone_deploy.sh — Multi-user standalone (Option A) integration test
#
# Verifies that two Linux users can run start_bot.sh in parallel
# on the same server without blocking each other via pgrep.
#
# Tested scenario:
#   1. user[0] runs start_bot.sh → must succeed
#   2. user[1] runs start_bot.sh → must also succeed (the pgrep bug blocked here)
#   3. Both PIDs are running, WebSocket connected in both logs
#   4. No "ERROR: an instance is already running" in the logs
#
# Uses the first two accounts from ~/.tradinebotte-test.conf
# (the same file as test_multibot_deploy.sh).
#
# Usage:
#   bash scripts/test_standalone_deploy.sh
#   bash scripts/test_standalone_deploy.sh --skip-deploy
#   TEST_MULTIBOT_CONF=/path/to/conf bash scripts/test_standalone_deploy.sh
#
# Local prerequisites : sshpass (apt-get install sshpass)
# Server prerequisites: python3-venv, python3.X-venv

set -uo pipefail

LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKIP_DEPLOY=false

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

section() { echo -e "\n${BOLD}${YELLOW}═══ $* ═══${NC}"; }
info()    { echo -e "${BLUE}  → $*${NC}"; }
ok()      { echo -e "${GREEN}  ✓ $*${NC}"; }
warn()    { echo -e "${YELLOW}  ! $*${NC}"; }
err()     { echo -e "${RED}  ✗ $*${NC}"; FAILURES=$((FAILURES + 1)); }

FAILURES=0
START_TS=$(date +%s)

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-deploy) SKIP_DEPLOY=true ;;
        -h|--help)
            grep '^#' "${BASH_SOURCE[0]}" | head -20 | sed 's/^# \?//'
            exit 0 ;;
        *) echo "Argument inconnu : $1"; exit 1 ;;
    esac
    shift
done

# ─── Config ────────────────────────────────────────────────────────────────────
CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
if [[ ! -f "$CONF" ]]; then
    echo -e "${RED}Configuration manquante : $CONF${NC}"
    echo "  cp scripts/test_multibot.conf.example ~/.tradinebotte-test.conf"
    exit 1
fi
# shellcheck source=/dev/null
source "$CONF"

SERVER="${TEST_SERVER:?TEST_SERVER manquant dans $CONF}"
PORT="${TEST_PORT:-22}"
ALL_USERS=("${TEST_USERS[@]:?TEST_USERS manquant dans $CONF}")
ALL_PASSWORDS=("${TEST_PASSWORDS[@]:?TEST_PASSWORDS manquant dans $CONF}")

if [[ ${#ALL_USERS[@]} -lt 2 ]]; then
    echo "ERROR: this test requires at least 2 accounts in TEST_USERS"
    exit 1
fi

# Only use the first two accounts.
USERS=("${ALL_USERS[0]}" "${ALL_USERS[1]}")
PASSWORDS=("${ALL_PASSWORDS[0]}" "${ALL_PASSWORDS[1]}")
INSTALL_DIR="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"

# ─── Helpers ───────────────────────────────────────────────────────────────────
run() {
    local idx="$1"; shift
    SSHPASS="${PASSWORDS[$idx]}" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -p "$PORT" "${USERS[$idx]}@$SERVER" "$@" 2>&1
}

deploy_code() {
    local idx="$1"
    SSHPASS="${PASSWORDS[$idx]}" /usr/bin/sshpass -e \
        rsync -az --delete \
        --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='.venv' --exclude='venv/' \
        --exclude='config.json' --exclude='live.db' --exclude='*.log' \
        -e "ssh -p $PORT -o StrictHostKeyChecking=yes" \
        "$LOCAL_REPO/" "${USERS[$idx]}@$SERVER:$INSTALL_DIR/" 2>&1
}

# ─── Pré-vol ───────────────────────────────────────────────────────────────────
section "PRÉ-VOL"

if [[ ! -x "$(command -v sshpass)" ]]; then
    echo "ERREUR : sshpass introuvable. Installer : apt-get install sshpass"
    exit 1
fi
ok "sshpass OK"
ok "Config : $CONF"
info "Serveur : $SERVER:$PORT — comptes : ${USERS[*]}"

# Populate known_hosts so SSH calls use StrictHostKeyChecking=yes safely
mkdir -p ~/.ssh && chmod 700 ~/.ssh
if ! ssh-keygen -F "[$SERVER]:$PORT" &>/dev/null && ! ssh-keygen -F "$SERVER" &>/dev/null; then
    info "Adding host key $SERVER:$PORT to known_hosts..."
    ssh-keyscan -p "$PORT" -H "$SERVER" >> ~/.ssh/known_hosts 2>/dev/null
fi

for idx in 0 1; do
    if run $idx "echo ok" &>/dev/null; then
        ok "SSH ${USERS[$idx]}@$SERVER:$PORT"
    else
        echo "ERROR: cannot connect to ${USERS[$idx]}@$SERVER:$PORT"
        exit 1
    fi
done

# ─── Phase 1 : Cleanup ─────────────────────────────────────────────────────────
section "PHASE 1 — CLEANUP"
for idx in 0 1; do
    run $idx "
        pkill -u \$(id -u) -f '[l]ive_bot.py' 2>/dev/null || true
        sleep 2
        pkill -9 -u \$(id -u) -f '[l]ive_bot.py' 2>/dev/null || true
        rm -rf $INSTALL_DIR
        rm -rf "\${HOME}/tmp/tradinebotte-test"
        exit 0
    " && ok "${USERS[$idx]} wiped" || warn "${USERS[$idx]} partial cleanup"
done

# ─── Phase 2 : Deploy ──────────────────────────────────────────────────────────
if [[ "$SKIP_DEPLOY" == "false" ]]; then
    section "PHASE 2 — DEPLOY"
    for idx in 0 1; do
        info "rsync → ${USERS[$idx]}..."
        deploy_code $idx && ok "${USERS[$idx]} rsync OK" || { err "${USERS[$idx]} rsync ÉCHEC"; }
        run $idx "cd $INSTALL_DIR && bash scripts/install.sh" \
            && ok "${USERS[$idx]} install.sh OK" || { err "${USERS[$idx]} install.sh ÉCHEC"; }
        run $idx "echo '' | python3 $INSTALL_DIR/scripts/setup.py" \
            && ok "${USERS[$idx]} setup.py OK (simulation)" || { err "${USERS[$idx]} setup.py ÉCHEC"; }
    done
    [[ $FAILURES -gt 0 ]] && { echo -e "${RED}Deploy failed — aborting.${NC}"; exit 1; }
else
    section "PHASE 2 — DEPLOY (skipped)"
fi

# ─── Phase 3 : Lancer user[0] ──────────────────────────────────────────────────
section "PHASE 3 — LANCER ${USERS[0]}"
info "start_bot.sh pour ${USERS[0]}..."
START0_OUT=$(run 0 "cd $INSTALL_DIR && TRADINEBOTTE_DIR=$INSTALL_DIR bash scripts/start_bot.sh")
if echo "$START0_OUT" | grep -q "Bot en cours"; then
    ok "${USERS[0]} bot started"
else
    err "${USERS[0]} start_bot.sh ÉCHEC"
    echo "$START0_OUT"
fi

# ─── Phase 4 : Lancer user[1] — test clé ──────────────────────────────────────
section "PHASE 4 — LANCER ${USERS[1]} (test pgrep scope)"
info "start_bot.sh pour ${USERS[1]} pendant que ${USERS[0]} tourne..."
START1_OUT=$(run 1 "cd $INSTALL_DIR && TRADINEBOTTE_DIR=$INSTALL_DIR bash scripts/start_bot.sh")

if echo "$START1_OUT" | grep -q "an instance is already running"; then
    err "${USERS[1]} BLOQUÉ par le pgrep de ${USERS[0]} — BUG DÉTECTÉ"
    echo "$START1_OUT"
elif echo "$START1_OUT" | grep -q "Bot en cours"; then
    ok "${USERS[1]} bot started alongside ${USERS[0]} — pgrep scope correct"
else
    err "${USERS[1]} start_bot.sh: unexpected result"
    echo "$START1_OUT"
fi

# ─── Phase 5 : Vérifier logs ───────────────────────────────────────────────────
section "PHASE 5 — VÉRIFICATION LOGS"
sleep 8
for idx in 0 1; do
    LOG="$INSTALL_DIR/live.log"
    if run $idx "grep -q 'WebSocket connected' $LOG 2>/dev/null"; then
        ok "${USERS[$idx]} WebSocket connecté"
    else
        err "${USERS[$idx]} WebSocket not connected in $LOG"
    fi
    if run $idx "grep -q 'an instance is already running' $LOG 2>/dev/null"; then
        err "${USERS[$idx]} log contains 'an instance is already running'"
    else
        ok "${USERS[$idx]} log propre (pas d'erreur instance unique)"
    fi
done

# ─── Phase 6 : Teardown ────────────────────────────────────────────────────────
section "PHASE 6 — TEARDOWN"
for idx in 0 1; do
    run $idx "pkill -u \$(id -u) -f '[l]ive_bot.py' 2>/dev/null || true"
    ok "${USERS[$idx]} bot stopped"
done

# ─── Rapport ───────────────────────────────────────────────────────────────────
ELAPSED=$(( $(date +%s) - START_TS ))
section "RAPPORT FINAL"
echo -e "  Duration: ${ELAPSED}s | Failures: $FAILURES"
echo ""
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${BOLD}${GREEN}  ✅ SUCCÈS — multi-user standalone OK${NC}"
    exit 0
else
    echo -e "${BOLD}${RED}  ❌ ÉCHEC — $FAILURES assertion(s) en erreur${NC}"
    exit 1
fi
