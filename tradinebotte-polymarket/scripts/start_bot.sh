#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  tradinebotte — Launch
#  Prerequisite: TRADINEBOTTE_DIR=<dir> python3 scripts/setup.py
#  (generates <TRADINEBOTTE_DIR>/config.json)
#
#  Installation directory (in order of priority):
#    1. Environment variable: TRADINEBOTTE_DIR=~/tradinebotte bash scripts/start_bot.sh
#    2. Default value: ~/tradinebotte (no root access required)
#
#  Options:
#    --reset-db          backs up live.db then removes it before launching
#                        (the bot restarts from scratch: capital, trades, history)
#    --simulate          simulation mode — I/O directed to ~/tradinebotte-sim
#    --snapshot-interval N  snapshot interval in seconds (default: 5)
#    Any other flag is passed as-is to live_bot.py.
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
PID_FILE="$INSTALL_DIR/live.pid"

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

# ── Check for a running instance (via PID file) ───────────────────
if [ -f "$PID_FILE" ]; then
    _pid=$(cat "$PID_FILE")
    if kill -0 "$_pid" 2>/dev/null; then
        echo "$(_t "❌ ERROR: an instance is already running (PID:" "❌ ERREUR : une instance est déjà en cours (PID:") $_pid)"
        echo "   $(_t "Stop it first:" "Arrêtez-la d'abord :") kill $_pid"
        exit 1
    fi
    rm -f "$PID_FILE"
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

# ── Check venv (prefer .venv over venv) ──────────────────────────
if [ -x "$INSTALL_DIR/.venv/bin/python3" ]; then
    PYTHON="$INSTALL_DIR/.venv/bin/python3"
elif [ -x "$INSTALL_DIR/venv/bin/python3" ]; then
    PYTHON="$INSTALL_DIR/venv/bin/python3"
else
    echo "$(_t "❌ ERROR: virtualenv not found in" "❌ ERREUR : virtualenv introuvable dans") $INSTALL_DIR/{.venv,venv}"
    echo "   $(_t "Run first:" "Lance d'abord :") bash scripts/install.sh"
    exit 1
fi

# ── Locate live_bot.py ────────────────────────────────────────────
if [ -f "$INSTALL_DIR/live_bot.py" ]; then
    BOT_SCRIPT="$INSTALL_DIR/live_bot.py"
else
    echo "$(_t "❌ ERROR: live_bot.py not found in" "❌ ERREUR : live_bot.py introuvable dans") $INSTALL_DIR"
    exit 1
fi

# ── Launch ────────────────────────────────────────────────────────
LOG="$INSTALL_DIR/live.log"
DISPLAY_LOG="${LOG/$HOME/\~}"
DISPLAY_DIR="${INSTALL_DIR/$HOME/\~}"
echo "$(_t "Starting bot from" "Lancement du bot depuis") $DISPLAY_DIR..."
export TRADINEBOTTE_DIR="$INSTALL_DIR"
nohup "$PYTHON" "$BOT_SCRIPT" "${BOT_EXTRA_ARGS[@]}" </dev/null >> "$LOG" 2>&1 &
_pid=$!
disown "$_pid"
echo "$_pid" > "$PID_FILE"
echo "PID: $_pid"
sleep 3

if kill -0 "$_pid" 2>/dev/null; then
    echo "✅ $(_t "Bot running — PID:" "Bot en cours — PID:") $_pid"
    echo "$(_t "Logs:" "Logs :") tail -f $DISPLAY_LOG"
else
    rm -f "$PID_FILE"
    echo "$(_t "❌ Bot stopped — last log lines:" "❌ Bot arrêté — dernières lignes du log :")"
    tail -20 "$LOG"
fi
