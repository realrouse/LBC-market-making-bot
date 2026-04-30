#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  POLYMARKET LIVE BOT v3 — Lancement
#  Prérequis : TRADINEBOTTE_DIR=<dir> python3 scripts/setup.py
#  (génère <TRADINEBOTTE_DIR>/config.json)
#
#  Répertoire d'installation (par ordre de priorité) :
#    1. Variable d'environnement : TRADINEBOTTE_DIR=~/tradinebotte bash scripts/start_bot.sh
#    2. Valeur par défaut : ~/tradinebotte (aucun accès root requis)
# ═══════════════════════════════════════════════════════════════════

INSTALL_DIR="${TRADINEBOTTE_DIR:-$HOME/tradinebotte}"
INSTALL_DIR="$(eval echo "$INSTALL_DIR")"
CONFIG="$INSTALL_DIR/config.json"

# ── Vérification ──────────────────────────────────────────────────
if [ ! -f "$CONFIG" ]; then
    echo "❌ ERREUR : config.json introuvable dans $INSTALL_DIR"
    echo "   Lance d'abord : TRADINEBOTTE_DIR=\"$INSTALL_DIR\" python3 scripts/setup.py"
    exit 1
fi

# ── Vérification instance unique ─────────────────────────────────
if pgrep -f live_bot.py > /dev/null; then
    echo "❌ ERREUR : une instance est déjà en cours (PID: $(pgrep -f live_bot.py))"
    echo "   Arrêtez-la d'abord : pkill -f live_bot.py"
    exit 1
fi

# ── Vérification venv ─────────────────────────────────────────────
PYTHON="$INSTALL_DIR/venv/bin/python3"
if [ ! -x "$PYTHON" ]; then
    echo "❌ ERREUR : virtualenv introuvable dans $INSTALL_DIR/venv"
    echo "   Lance d'abord : bash scripts/install.sh"
    exit 1
fi

# ── Lancement ─────────────────────────────────────────────────────
LOG="$INSTALL_DIR/live.log"
DISPLAY_LOG="${LOG/$HOME/\~}"
DISPLAY_DIR="${INSTALL_DIR/$HOME/\~}"
echo "Lancement du bot depuis $DISPLAY_DIR..."
export TRADINEBOTTE_DIR="$INSTALL_DIR"
nohup "$PYTHON" "$INSTALL_DIR/live_bot.py" >> "$LOG" 2>&1 &
echo "PID: $!"
sleep 3

if pgrep -f live_bot.py > /dev/null; then
    echo "✅ Bot en cours — PID: $(pgrep -f live_bot.py)"
    echo "Logs: tail -f $DISPLAY_LOG"
else
    echo "❌ Bot arrêté — dernières lignes du log:"
    tail -20 "$LOG"
fi
