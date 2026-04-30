#!/usr/bin/env bash
# test_multibot_deploy.sh — Clean install + integration test on configurable test accounts
#
# Phases :
#   1. Cleanup    — tue les processus, supprime les répertoires, nettoie les locks
#   2. Deploy     — rsync du repo local + création venv + pip install (sans root)
#   3. Prépare    — crée les répertoires de simulation
#   4. Lance      — démarre tous les bots simultanément (test de la race condition)
#   5. Check init — vérifie : 1 feed, N bots, connexions établies
#   6. Soutenu    — heartbeat toutes les 30s pendant DURATION secondes
#   7. Analyse    — collecte et analyse les logs, vérifie book updates
#   8. Teardown   — tue tous les processus
#   9. Rapport    — SUCCÈS / ÉCHEC avec détail des erreurs
#
# Usage :
#   bash scripts/test_multibot_deploy.sh
#   bash scripts/test_multibot_deploy.sh --duration 300
#   bash scripts/test_multibot_deploy.sh --skip-deploy   # réutilise l'install existante
#
# Configuration (requise) :
#   cp scripts/test_multibot.conf.example ~/.tradinebotte-test.conf
#   editor ~/.tradinebotte-test.conf   # renseigner SERVER, PORT, USERS, PASSWORDS
#   # ou : TEST_MULTIBOT_CONF=/chemin/vers/conf bash scripts/test_multibot_deploy.sh
#
# Prérequis locaux  : sshpass (apt-get install sshpass)
# Prérequis serveur : python3-venv, python3-pip, python3.X-venv

set -euo pipefail

LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DURATION=180
SKIP_DEPLOY=false

# ─── Couleurs ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

section() { echo -e "\n${BOLD}${YELLOW}═══ $* ═══${NC}"; }
info()    { echo -e "${BLUE}  → $*${NC}"; }
ok()      { echo -e "${GREEN}  ✓ $*${NC}"; }
warn()    { echo -e "${YELLOW}  ! $*${NC}"; }
err()     { echo -e "${RED}  ✗ $*${NC}"; FAILURES=$((FAILURES + 1)); }

FAILURES=0
START_TS=$(date +%s)

# ─── Arguments ─────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-deploy) SKIP_DEPLOY=true ;;
        --duration)    DURATION="$2"; shift ;;
        -h|--help)
            grep '^#' "${BASH_SOURCE[0]}" | head -25 | sed 's/^# \?//'
            exit 0 ;;
        *) echo "Argument inconnu : $1"; exit 1 ;;
    esac
    shift
done

# ─── Chargement de la configuration ────────────────────────────────────────────
CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
if [[ ! -f "$CONF" ]]; then
    echo -e "${RED}Configuration manquante : $CONF${NC}"
    echo ""
    echo "Créer le fichier de configuration depuis le template :"
    echo "  cp scripts/test_multibot.conf.example ~/.tradinebotte-test.conf"
    echo "  editor ~/.tradinebotte-test.conf"
    echo ""
    echo "Ou pointer vers un fichier personnalisé :"
    echo "  TEST_MULTIBOT_CONF=/chemin/vers/conf bash scripts/test_multibot_deploy.sh"
    exit 1
fi
# shellcheck source=/dev/null
source "$CONF"

SERVER="${TEST_SERVER:?TEST_SERVER manquant dans $CONF}"
PORT="${TEST_PORT:-22}"
USERS=("${TEST_USERS[@]:?TEST_USERS manquant dans $CONF}")
PASSWORDS=("${TEST_PASSWORDS[@]:?TEST_PASSWORDS manquant dans $CONF}")
# Config can set a default duration; --duration flag takes precedence if already changed
[[ "$DURATION" -eq 180 && -n "${TEST_DURATION:-}" ]] && DURATION="$TEST_DURATION"
# Remote directories — override in conf via TEST_REMOTE_INSTALL_DIR / TEST_REMOTE_BOT_DIR
REMOTE_INSTALL_DIR="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"
REMOTE_BOT_DIR="${TEST_REMOTE_BOT_DIR:-~/account-sim}"

N_BOTS=${#USERS[@]}
if [[ "$N_BOTS" -ne ${#PASSWORDS[@]} ]]; then
    echo "ERREUR : TEST_USERS et TEST_PASSWORDS doivent avoir la même longueur"
    exit 1
fi

# ─── Helpers SSH ───────────────────────────────────────────────────────────────
run() {
    local idx="$1"; shift
    /usr/bin/sshpass -p "${PASSWORDS[$idx]}" \
        ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=no \
        -p "$PORT" "${USERS[$idx]}@$SERVER" "$@" 2>&1
}

run_bg() {
    local idx="$1"; shift
    /usr/bin/sshpass -p "${PASSWORDS[$idx]}" \
        ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=no \
        -p "$PORT" "${USERS[$idx]}@$SERVER" "$@" 2>&1 &
}

deploy_code() {
    local idx="$1"
    /usr/bin/sshpass -p "${PASSWORDS[$idx]}" \
        rsync -az --delete \
        --exclude='.git' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.venv' \
        -e "ssh -p $PORT -o StrictHostKeyChecking=no" \
        "$LOCAL_REPO/" "${USERS[$idx]}@$SERVER:$REMOTE_INSTALL_DIR/" 2>&1
}

# ─── Pré-vol ───────────────────────────────────────────────────────────────────
section "PRÉ-VOL"

SSHPASS_BIN=$(command -v sshpass || echo "/usr/bin/sshpass")
if [[ ! -x "$SSHPASS_BIN" ]]; then
    echo "ERREUR : sshpass introuvable. Installer : apt-get install sshpass"
    exit 1
fi
ok "sshpass : $SSHPASS_BIN"
ok "Config : $CONF"
info "Serveur : $SERVER:$PORT — $N_BOTS comptes : ${USERS[*]}"
info "Repo local : $LOCAL_REPO"
info "Durée de test : ${DURATION}s"
[[ "$SKIP_DEPLOY" == "true" ]] && info "Mode --skip-deploy : déploiement ignoré"

for idx in "${!USERS[@]}"; do
    if run $idx "echo ok" &>/dev/null; then
        ok "SSH ${USERS[$idx]}@$SERVER:$PORT"
    else
        echo "ERREUR : impossible de se connecter à ${USERS[$idx]}@$SERVER:$PORT"
        exit 1
    fi
done

# ─── Phase 1 : Cleanup ─────────────────────────────────────────────────────────
section "PHASE 1 — CLEANUP"
for idx in "${!USERS[@]}"; do
    user="${USERS[$idx]}"
    info "Nettoyage $user..."
    run $idx "
        pkill -f account_bot.py 2>/dev/null || true
        pkill -f 'bot/feed.py'  2>/dev/null || true
        sleep 1
        rm -rf $REMOTE_INSTALL_DIR $REMOTE_BOT_DIR
        rm -f /tmp/tradinebotte-feed-*.lock /tmp/tradinebotte-feed-*.log
    " && ok "$user nettoyé" || warn "$user nettoyage partiel"
done

# Vérification depuis le premier compte (ps aux = tous les utilisateurs)
STALE=$(run 0 "ps aux | grep -E '(account_bot|feed)\.py' | grep -v grep | wc -l" || echo 0)
if [[ "$STALE" -eq 0 ]]; then
    ok "Aucun processus résiduel"
else
    warn "$STALE processus résiduel(s) visible(s) — on continue"
fi

# ─── Phase 2 : Deploy ──────────────────────────────────────────────────────────
if [[ "$SKIP_DEPLOY" == "true" ]]; then
    section "PHASE 2 — DEPLOY (ignoré — --skip-deploy)"
else
    section "PHASE 2 — DEPLOY"
    for idx in "${!USERS[@]}"; do
        user="${USERS[$idx]}"
        info "rsync → $user..."
        deploy_code $idx && ok "$user : rsync OK" || { err "$user : rsync échoué"; exit 1; }

        info "Création venv $user..."
        run $idx "python3 -m venv $REMOTE_INSTALL_DIR/venv 2>&1" \
            && ok "$user : venv créé" || { err "$user : création venv échouée"; exit 1; }

        info "pip install $user..."
        run $idx "
            $REMOTE_INSTALL_DIR/venv/bin/pip install --quiet --upgrade pip
            $REMOTE_INSTALL_DIR/venv/bin/pip install --quiet -r $REMOTE_INSTALL_DIR/requirements.txt
        " && ok "$user : dépendances installées" || { err "$user : pip install échoué"; exit 1; }
    done
fi

# ─── Phase 3 : Prépare les répertoires de simulation ──────────────────────────
section "PHASE 3 — RÉPERTOIRES SIMULATION"
for idx in "${!USERS[@]}"; do
    user="${USERS[$idx]}"
    run $idx "mkdir -p $REMOTE_BOT_DIR" && ok "$user : $REMOTE_BOT_DIR prêt"
done

# ─── Phase 4 : Lancement simultané ─────────────────────────────────────────────
section "PHASE 4 — LANCEMENT SIMULTANÉ DES $N_BOTS BOTS"
info "Envoi des $N_BOTS commandes de lancement en parallèle (test race condition)..."

LAUNCH_CMD="
    cd $REMOTE_INSTALL_DIR
    TRADINEBOTTE_DIR=$REMOTE_BOT_DIR \\
    nohup $REMOTE_INSTALL_DIR/venv/bin/python3 bot/account_bot.py --verbose \\
        > $REMOTE_BOT_DIR/account.log 2>&1 < /dev/null &
    echo \"PID=\$!\"
"

for idx in "${!USERS[@]}"; do
    run_bg $idx "$LAUNCH_CMD"
done
wait  # attend que les N sessions SSH retournent (pas que les bots terminent)

ok "$N_BOTS commandes de lancement envoyées"
info "Attente 20s — feed auto-start + stabilisation..."
sleep 20

# ─── Phase 5 : Vérification initiale ───────────────────────────────────────────
section "PHASE 5 — VÉRIFICATION INITIALE"

FEED_COUNT=$(run 0 "ps aux | grep '[f]eed.py' | wc -l" || echo 0)
BOT_COUNT=$( run 0 "ps aux | grep '[a]ccount_bot.py' | wc -l" || echo 0)
info "Processus feed.py     : $FEED_COUNT (attendu : 1)"
info "Processus account_bot : $BOT_COUNT (attendu : $N_BOTS)"

[[ "$FEED_COUNT" -eq 1 ]]       && ok "Feed unique actif" || err "Nombre incorrect de feeds : $FEED_COUNT (attendu 1)"
[[ "$BOT_COUNT"  -eq "$N_BOTS" ]] && ok "$N_BOTS bots actifs" || err "Nombre incorrect de bots : $BOT_COUNT (attendu $N_BOTS)"

for idx in "${!USERS[@]}"; do
    user="${USERS[$idx]}"
    LOG=$(run $idx "cat /account.log 2>/dev/null || echo '(vide)'")
    if echo "$LOG" | grep -q "Connecte au feed"; then
        ok "$user : connecté au feed"
    else
        err "$user : pas de message 'Connecte au feed'"
    fi
    if echo "$LOG" | grep -qE "Feed démarré|Feed prêt"; then
        ok "$user : a démarré le feed (gagnant de la race)"
    elif echo "$LOG" | grep -q "Feed actif sur"; then
        ok "$user : a trouvé le feed déjà actif"
    elif echo "$LOG" | grep -q "Feed en cours de démarrage"; then
        ok "$user : a attendu le démarrage du feed (perdant de la race)"
    fi
done

FEED_LOG_PATH=$(run 0 "ls /tmp/tradinebotte-feed-*.log 2>/dev/null | head -1 || echo '(introuvable)'")
info "Log feed : $FEED_LOG_PATH"
if [[ "$FEED_LOG_PATH" != "(introuvable)" ]]; then
    FEED_LOG=$(run 0 "cat $FEED_LOG_PATH 2>/dev/null | head -60 || echo '(vide)'")
    if echo "$FEED_LOG" | grep -qE "WebSocket connecte|Souscription|Marches BTC"; then
        ok "Feed : WebSocket Polymarket connecté"
    else
        warn "Feed : pas encore de confirmation WebSocket"
    fi
else
    warn "Feed log introuvable dans /tmp/"
fi

# ─── Phase 6 : Opération soutenue ──────────────────────────────────────────────
section "PHASE 6 — OPÉRATION SOUTENUE (${DURATION}s)"
ELAPSED=20
CHECK_INTERVAL=30

while [[ $ELAPSED -lt $DURATION ]]; do
    REMAINING=$((DURATION - ELAPSED))
    SLEEP_FOR=$CHECK_INTERVAL
    [[ $SLEEP_FOR -gt $REMAINING ]] && SLEEP_FOR=$REMAINING
    info "Heartbeat dans ${SLEEP_FOR}s — temps restant : ${REMAINING}s"
    sleep $SLEEP_FOR
    ELAPSED=$((ELAPSED + SLEEP_FOR))

    FEED_C=$(run 0 "ps aux | grep '[f]eed.py' | wc -l" 2>/dev/null || echo "?")
    BOT_C=$( run 0 "ps aux | grep '[a]ccount_bot.py' | wc -l" 2>/dev/null || echo "?")
    info "  [${ELAPSED}s] feed=$FEED_C bots=$BOT_C"

    [[ "$FEED_C" == "1" ]]        || warn "  ! Nombre anormal de feeds : $FEED_C"
    [[ "$BOT_C"  == "$N_BOTS" ]]  || warn "  ! Nombre anormal de bots : $BOT_C"
done
ok "Durée ${DURATION}s atteinte"

# ─── Phase 7 : Analyse finale ──────────────────────────────────────────────────
section "PHASE 7 — ANALYSE DES LOGS"

for idx in "${!USERS[@]}"; do
    user="${USERS[$idx]}"
    LOG=$(run $idx "cat /account.log 2>/dev/null || echo '(vide)'")
    echo ""
    echo -e "${BOLD}--- $user : account.log (20 dernières lignes) ---${NC}"
    echo "$LOG" | tail -20

    echo "$LOG" | grep -q "Connecte au feed" && ok "$user : connexion feed confirmée" || err "$user : pas de connexion feed"
    BOOK_COUNT=$(echo "$LOG" | grep -c '\[FEED\] book' || true)
    [[ "$BOOK_COUNT" -gt 0 ]] && ok "$user : $BOOK_COUNT book updates reçus (flux actif)" || \
        warn "$user : aucun book update — marché peut-être calme"
    ERROR_COUNT=$(echo "$LOG" | grep -ciE "^.*\[(ERROR|CRITICAL)\]" || true)
    [[ "$ERROR_COUNT" -eq 0 ]] && ok "$user : pas d'erreur critique" || \
        err "$user : $ERROR_COUNT ligne(s) ERROR/CRITICAL dans les logs"
done

echo ""
echo -e "${BOLD}--- Feed log (30 dernières lignes) ---${NC}"
if [[ "${FEED_LOG_PATH:-}" != "(introuvable)" && -n "${FEED_LOG_PATH:-}" ]]; then
    FEED_LOG_FINAL=$(run 0 "cat $FEED_LOG_PATH 2>/dev/null | tail -30 || echo '(vide)'")
    echo "$FEED_LOG_FINAL"
    echo "$FEED_LOG_FINAL" | grep -qE "WebSocket connecte|Souscription" && \
        ok "Feed : WebSocket confirmé dans le log final" || err "Feed : WebSocket non confirmé"
    echo "$FEED_LOG_FINAL" | grep -q "Marches BTC" && \
        ok "Feed : marchés BTC trouvés" || warn "Feed : marchés BTC non trouvés"
else
    warn "Feed log introuvable — impossible d'analyser"
fi

FEED_FINAL=$(run 0 "ps aux | grep '[f]eed.py' | wc -l" || echo 0)
BOT_FINAL=$( run 0 "ps aux | grep '[a]ccount_bot.py' | wc -l" || echo 0)
[[ "$FEED_FINAL" -eq 1 ]]        && ok "Feed encore actif après ${DURATION}s" || err "Feed arrêté prématurément"
[[ "$BOT_FINAL"  -eq "$N_BOTS" ]] && ok "$N_BOTS bots encore actifs après ${DURATION}s" || \
    err "$BOT_FINAL/$N_BOTS bots encore actifs"

# ─── Phase 8 : Teardown ────────────────────────────────────────────────────────
section "PHASE 8 — TEARDOWN"
for idx in "${!USERS[@]}"; do
    user="${USERS[$idx]}"
    run $idx "
        pkill -f account_bot.py 2>/dev/null || true
        pkill -f 'bot/feed.py'  2>/dev/null || true
    " && info "$user : processus arrêtés" || true
done
sleep 3
REMAINING_PROCS=$(run 0 "ps aux | grep -E '(account_bot|feed)\.py' | grep -v grep | wc -l" || echo 0)
[[ "$REMAINING_PROCS" -eq 0 ]] && ok "Tous les processus terminés" || \
    warn "$REMAINING_PROCS processus encore actif(s)"

# ─── Rapport final ─────────────────────────────────────────────────────────────
section "RAPPORT FINAL"
TOTAL_SECS=$(( $(date +%s) - START_TS ))
echo ""
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${GREEN}${BOLD}  SUCCÈS — Tous les tests passés (durée totale : ${TOTAL_SECS}s)${NC}"
    exit 0
else
    echo -e "${RED}${BOLD}  ÉCHEC — $FAILURES test(s) échoué(s) (durée totale : ${TOTAL_SECS}s)${NC}"
    exit 1
fi
