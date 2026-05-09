#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  TRADINEBOTTE — Service d'indicateurs techniques
#
#  Lance indicators.py avec le fichier de config JSON choisi.
#  Chaque compte utilise son propre fichier config et son propre port ZMQ.
#
#  Exemples :
#    # account-a (4h, port 5559) :
#    TRADINEBOTTE_INDICATORS_CONFIG=strategies/indicators_4h_bitcoin.json \
#      bash scripts/start_indicators.sh
#
#    # account-b (daily, port 5560) :
#    TRADINEBOTTE_INDICATORS_CONFIG=strategies/indicators_1d_bitcoin.json \
#      bash scripts/start_indicators.sh
#
#    # Répertoire de déploiement personnalisé :
#    TRADINEBOTTE_DIR=~/account-a \
#    TRADINEBOTTE_INDICATORS_CONFIG=strategies/indicators_4h_bitcoin.json \
#      bash scripts/start_indicators.sh
# ═══════════════════════════════════════════════════════════════════

INSTALL_DIR="${TRADINEBOTTE_DIR:-$HOME/tradinebotte}"
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"
VENV="$INSTALL_DIR/venv"
BOT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IND_LOG="$INSTALL_DIR/indicators.log"

# Default: 4h config.  Override with TRADINEBOTTE_INDICATORS_CONFIG.
CONFIG="${TRADINEBOTTE_INDICATORS_CONFIG:-$BOT_ROOT/strategies/indicators_4h_bitcoin.json}"

if [ ! -d "$VENV" ]; then
    echo "ERREUR : venv introuvable dans $INSTALL_DIR"
    echo "Lance d'abord : bash scripts/install.sh"
    exit 1
fi

if [ ! -f "$CONFIG" ]; then
    echo "ERREUR : fichier de config introuvable : $CONFIG"
    exit 1
fi

# Refuse de lancer un second processus avec le même fichier de config.
if pgrep -a -f "indicators.py" 2>/dev/null | grep -qF "$CONFIG"; then
    echo "ERREUR : indicators.py déjà en cours pour ce config : $CONFIG"
    echo "Arrête-le d'abord : pkill -f indicators.py"
    exit 1
fi

echo "Lancement indicators.py — config=$CONFIG"
echo "Log : $IND_LOG"

nohup "$VENV/bin/python3" "$BOT_ROOT/bot/indicators.py" \
    --config "$CONFIG" \
    >> "$IND_LOG" 2>&1 &
echo "PID: $!"
sleep 2

if pgrep -f "indicators.py" > /dev/null; then
    echo "Indicators en cours — PID: $(pgrep -f indicators.py | tail -1)"
else
    echo "Echec — verifier : $IND_LOG"
    tail -20 "$IND_LOG"
fi
