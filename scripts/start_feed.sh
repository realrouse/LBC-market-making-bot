#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  TRADINEBOTTE — Feed WebSocket partagé (Option A multi-bot)
#
#  Lance feed.py : une seule connexion WebSocket vers Polymarket,
#  publie les mises à jour via ZeroMQ PUB sur TRADINEBOTTE_FEED_ADDR
#  (défaut : tcp://127.0.0.1:5557).
#
#  Lancer AVANT les account bots :
#    bash scripts/start_feed.sh
#    TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
#    TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
#
#  Adresse personnalisée :
#    TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 bash scripts/start_feed.sh
# ═══════════════════════════════════════════════════════════════════

INSTALL_DIR="${TRADINEBOTTE_DIR:-$HOME/tradinebotte}"
INSTALL_DIR="$(eval echo "$INSTALL_DIR")"
VENV="$INSTALL_DIR/venv"
FEED_LOG="$INSTALL_DIR/feed.log"

if [ ! -d "$VENV" ]; then
    echo "ERREUR : venv introuvable dans $INSTALL_DIR"
    echo "Lance d'abord : bash scripts/install.sh"
    exit 1
fi

if pgrep -f feed.py > /dev/null; then
    echo "ERREUR : feed.py deja en cours (PID: $(pgrep -f feed.py))"
    echo "Arrete-le d'abord : pkill -f feed.py"
    exit 1
fi

export TRADINEBOTTE_FEED_ADDR="${TRADINEBOTTE_FEED_ADDR:-tcp://127.0.0.1:5557}"
echo "Lancement feed.py — PUB sur $TRADINEBOTTE_FEED_ADDR"
echo "Log : $FEED_LOG"

nohup "$VENV/bin/python3" "$(dirname "$0")/../bot/feed.py" \
    >> "$FEED_LOG" 2>&1 &
echo "PID: $!"
sleep 2

if pgrep -f feed.py > /dev/null; then
    echo "Feed en cours — PID: $(pgrep -f feed.py)"
else
    echo "Echec — verifier : $FEED_LOG"
    tail -20 "$FEED_LOG"
fi
