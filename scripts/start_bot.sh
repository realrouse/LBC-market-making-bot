#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  POLYMARKET LIVE BOT v3 — Lancement / Launch
#  Prérequis : TRADINEBOTTE_DIR=<dir> python3 scripts/setup.py
#  (génère <TRADINEBOTTE_DIR>/config.json)
#
#  Répertoire d'installation (par ordre de priorité) :
#    1. Variable d'environnement : TRADINEBOTTE_DIR=~/tradinebotte bash scripts/start_bot.sh
#    2. Valeur par défaut : ~/tradinebotte (aucun accès root requis)
#
#  Options :
#    --reset-db          sauvegarde live.db puis le supprime avant de lancer
#                        (le bot repart à zéro : capital, trades, historique)
#    --simulate          mode simulation — I/O vers ~/tradinebotte-sim
#    --snapshot-interval N  intervalle snapshots en secondes (défaut : 5)
#    Tout autre flag est transmis tel quel à live_bot.py.
# ═══════════════════════════════════════════════════════════════════

RESET_DB=0
BOT_EXTRA_ARGS=()
for _arg in "$@"; do
    if [ "$_arg" = "--reset-db" ]; then
        RESET_DB=1
    else
        BOT_EXTRA_ARGS+=("$_arg")
    fi
done

INSTALL_DIR="${TRADINEBOTTE_DIR:-$HOME/tradinebotte}"
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"
CONFIG="$INSTALL_DIR/config.json"

# ── Language ──────────────────────────────────────────────────────
# Read the language preference saved by setup.py in config.json.
# Defaults to EN if config.json is absent or does not contain "lang".
LANG="EN"
if [ -f "$CONFIG" ]; then
    LANG=$(python3 -c '
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("lang", "EN"))
except Exception:
    print("EN")
' "$CONFIG" 2>/dev/null || echo "EN")
fi

# _t "EN text" "FR text" — print the string for the current language (no trailing newline)
_t() { [ "$LANG" = "FR" ] && printf '%s' "$2" || printf '%s' "$1"; }

# ── Check config ──────────────────────────────────────────────────
if [ ! -f "$CONFIG" ]; then
    echo "$(_t "❌ ERROR: config.json not found in" "❌ ERREUR : config.json introuvable dans") $INSTALL_DIR"
    echo "   $(_t "Run first:" "Lance d'abord :") TRADINEBOTTE_DIR=\"$INSTALL_DIR\" python3 scripts/setup.py"
    exit 1
fi

# ── Check for a running instance (current user only) ──────────────
if pgrep -u "$(id -u)" -f '[l]ive_bot\.py' > /dev/null; then
    _pid=$(pgrep -u "$(id -u)" -f '[l]ive_bot\.py')
    echo "$(_t "❌ ERROR: an instance is already running (PID:" "❌ ERREUR : une instance est déjà en cours (PID:") $_pid)"
    echo "   $(_t "Stop it first:" "Arrêtez-la d'abord :") pkill -u \$(id -u) -f '[l]ive_bot\.py'"
    exit 1
fi

# ── --reset-db ────────────────────────────────────────────────────
if [ "$RESET_DB" = "1" ]; then
    DB="$INSTALL_DIR/live.db"
    if [ -f "$DB" ]; then
        BAK="${DB}.bak.$(date +%Y%m%d_%H%M%S)"
        echo "⚠️  --reset-db : $(_t "backup →" "sauvegarde →") $(basename "$BAK")"
        read -r -p "   $(_t "Confirm reset (yes/N): " "Confirmer la réinitialisation (yes/N) : ")" _confirm
        if [ "$_confirm" != "yes" ]; then
            echo "$(_t "Cancelled." "Annulé.")"
            exit 0
        fi
        cp "$DB" "$BAK"
        rm "$DB"
        echo "✅ $(_t "live.db reset (backup:" "live.db réinitialisé (backup :") $BAK)"
    else
        echo "$(_t "live.db not found — nothing to reset." "live.db absent — rien à réinitialiser.")"
    fi
fi

# ── Check venv ────────────────────────────────────────────────────
PYTHON="$INSTALL_DIR/venv/bin/python3"
if [ ! -x "$PYTHON" ]; then
    echo "$(_t "❌ ERROR: virtualenv not found in" "❌ ERREUR : virtualenv introuvable dans") $INSTALL_DIR/venv"
    echo "   $(_t "Run first:" "Lance d'abord :") bash scripts/install.sh"
    exit 1
fi

# ── Launch ────────────────────────────────────────────────────────
LOG="$INSTALL_DIR/live.log"
DISPLAY_LOG="${LOG/$HOME/\~}"
DISPLAY_DIR="${INSTALL_DIR/$HOME/\~}"
echo "$(_t "Starting bot from" "Lancement du bot depuis") $DISPLAY_DIR..."
export TRADINEBOTTE_DIR="$INSTALL_DIR"
nohup "$PYTHON" "$INSTALL_DIR/live_bot.py" "${BOT_EXTRA_ARGS[@]}" >> "$LOG" 2>&1 &
echo "PID: $!"
sleep 3

if pgrep -u "$(id -u)" -f '[l]ive_bot\.py' > /dev/null; then
    echo "✅ $(_t "Bot running — PID:" "Bot en cours — PID:") $(pgrep -u "$(id -u)" -f '[l]ive_bot\.py')"
    echo "$(_t "Logs:" "Logs :") tail -f $DISPLAY_LOG"
else
    echo "$(_t "❌ Bot stopped — last log lines:" "❌ Bot arrêté — dernières lignes du log :")"
    tail -20 "$LOG"
fi
