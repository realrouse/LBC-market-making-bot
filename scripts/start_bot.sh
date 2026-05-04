#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  POLYMARKET LIVE BOT v3 — Lancement
#  Prérequis : TRADINEBOTTE_DIR=<dir> python3 scripts/setup.py
#  (génère <TRADINEBOTTE_DIR>/config.json)
#
#  Répertoire d'installation (par ordre de priorité) :
#    1. Variable d'environnement : TRADINEBOTTE_DIR=~/tradinebotte bash scripts/start_bot.sh
#    2. Valeur par défaut : ~/tradinebotte (aucun accès root requis)
#
#  Options :
#    --reset-db   sauvegarde live.db puis le supprime avant de lancer
#                 (le bot repart à zéro : capital, trades, historique)
# ═══════════════════════════════════════════════════════════════════

RESET_DB=0
for _arg in "$@"; do
    [ "$_arg" = "--reset-db" ] && RESET_DB=1
done

INSTALL_DIR="${TRADINEBOTTE_DIR:-$HOME/tradinebotte}"
INSTALL_DIR="$(eval echo "$INSTALL_DIR")"
CONFIG="$INSTALL_DIR/config.json"

# ── Vérification ──────────────────────────────────────────────────
if [ ! -f "$CONFIG" ]; then
    echo "❌ ERREUR : config.json introuvable dans $INSTALL_DIR"
    echo "   Lance d'abord : TRADINEBOTTE_DIR=\"$INSTALL_DIR\" python3 scripts/setup.py"
    exit 1
fi

# ── Vérification instance unique (utilisateur courant uniquement) ──
if pgrep -u "$(id -u)" -f live_bot.py > /dev/null; then
    echo "❌ ERREUR : une instance est déjà en cours (PID: $(pgrep -u "$(id -u)" -f live_bot.py))"
    echo "   Arrêtez-la d'abord : pkill -u \$(id -u) -f live_bot.py"
    exit 1
fi

# ── Réinitialisation DB (--reset-db) ──────────────────────────────
if [ "$RESET_DB" = "1" ]; then
    DB="$INSTALL_DIR/live.db"
    if [ -f "$DB" ]; then
        BAK="${DB}.bak.$(date +%Y%m%d_%H%M%S)"
        echo "⚠️  --reset-db : sauvegarde → $(basename "$BAK")"
        read -r -p "   Confirmer la réinitialisation (yes/N) : " _confirm
        if [ "$_confirm" != "yes" ]; then
            echo "Annulé."
            exit 0
        fi
        cp "$DB" "$BAK"
        rm "$DB"
        echo "✅ live.db réinitialisé (backup : $BAK)"
    else
        echo "live.db absent — rien à réinitialiser."
    fi
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

if pgrep -u "$(id -u)" -f live_bot.py > /dev/null; then
    echo "✅ Bot en cours — PID: $(pgrep -u "$(id -u)" -f live_bot.py)"
    echo "Logs: tail -f $DISPLAY_LOG"
else
    echo "❌ Bot arrêté — dernières lignes du log:"
    tail -20 "$LOG"
fi
