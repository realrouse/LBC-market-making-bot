#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  POLYMARKET LIVE BOT v3 — Lancement
#  Prérequis : POLYMARKET_DIR=<dir> python3 scripts/setup.py
#  (génère <POLYMARKET_DIR>/config.json)
#
#  Répertoire d'installation (par ordre de priorité) :
#    1. Variable d'environnement : POLYMARKET_DIR=~/polymarket bash scripts/start_bot.sh
#    2. Valeur par défaut : ~/polymarket (aucun accès root requis)
# ═══════════════════════════════════════════════════════════════════

INSTALL_DIR="${POLYMARKET_DIR:-$HOME/polymarket}"
INSTALL_DIR="$(eval echo "$INSTALL_DIR")"
CONFIG="$INSTALL_DIR/config.json"

# ── Vérification ──────────────────────────────────────────────────
if [ ! -f "$CONFIG" ]; then
    echo "❌ ERREUR : config.json introuvable dans $INSTALL_DIR"
    echo "   Lance d'abord : POLYMARKET_DIR=\"$INSTALL_DIR\" python3 scripts/setup.py"
    exit 1
fi

# ── Vérification instance unique ─────────────────────────────────
if pgrep -f live_bot.py > /dev/null; then
    echo "❌ ERREUR : une instance est déjà en cours (PID: $(pgrep -f live_bot.py))"
    echo "   Arrêtez-la d'abord : pkill -f live_bot.py"
    exit 1
fi

# ── Lancement ─────────────────────────────────────────────────────
echo "Lancement du bot depuis $INSTALL_DIR..."
export POLYMARKET_DIR="$INSTALL_DIR"
nohup python3 "$INSTALL_DIR/live_bot.py" > /dev/null 2>&1 &
echo "PID: $!"
sleep 3

if pgrep -f live_bot.py > /dev/null; then
    echo "✅ Bot en cours — PID: $(pgrep -f live_bot.py)"
    echo "Logs: tail -f $INSTALL_DIR/live.log"
else
    echo "❌ Bot arrêté — vérifier les logs:"
    tail -20 "$INSTALL_DIR/live.log"
fi
