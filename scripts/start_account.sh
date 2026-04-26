#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  TRADINEBOTTE — Démarrage d'un compte (Option A multi-bot)
#
#  Lance account_bot.py pour UN compte. Chaque compte a son propre
#  TRADINEBOTTE_DIR avec config.json et clé privée.
#
#  Prérequis :
#    1. feed.py en cours (bash scripts/start_feed.sh)
#    2. config.json du compte dans TRADINEBOTTE_DIR
#
#  Exemples :
#    TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
#    TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
#    TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 \
#      TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
# ═══════════════════════════════════════════════════════════════════

INSTALL_DIR="${TRADINEBOTTE_DIR:-$HOME/tradinebotte}"
INSTALL_DIR="$(eval echo "$INSTALL_DIR")"
VENV="$INSTALL_DIR/venv"
CONFIG="$INSTALL_DIR/config.json"
BOT_LOG="$INSTALL_DIR/account.log"

if [ ! -f "$CONFIG" ]; then
    echo "ERREUR : config.json introuvable dans $INSTALL_DIR"
    echo "Lance d'abord : TRADINEBOTTE_DIR=\"$INSTALL_DIR\" python3 scripts/setup.py"
    exit 1
fi

if [ ! -d "$VENV" ]; then
    echo "ERREUR : venv introuvable dans $INSTALL_DIR"
    echo "Lance d'abord : TRADINEBOTTE_DIR=\"$INSTALL_DIR\" bash scripts/install.sh"
    exit 1
fi

# Detect if another account_bot.py for THIS directory is already running.
if pgrep -f "account_bot.py" > /dev/null; then
    # Check if it's specifically for this INSTALL_DIR via env var.
    if pgrep -a -f "account_bot.py" | grep -q "$INSTALL_DIR"; then
        echo "ERREUR : account_bot.py deja en cours pour $INSTALL_DIR"
        exit 1
    fi
fi

if ! pgrep -f feed.py > /dev/null; then
    echo "ATTENTION : feed.py n'est pas en cours."
    echo "Lance d'abord : bash scripts/start_feed.sh"
    echo "(L'account bot attendra les messages du feed)"
fi

export TRADINEBOTTE_DIR="$INSTALL_DIR"
export TRADINEBOTTE_FEED_ADDR="${TRADINEBOTTE_FEED_ADDR:-tcp://127.0.0.1:5557}"

echo "Lancement account_bot.py — dir=$INSTALL_DIR feed=$TRADINEBOTTE_FEED_ADDR"
echo "Log : $BOT_LOG"

nohup "$VENV/bin/python3" "$(dirname "$0")/../bot/account_bot.py" \
    >> "$BOT_LOG" 2>&1 &
echo "PID: $!"
sleep 2

if pgrep -f "account_bot.py" > /dev/null; then
    echo "Account bot en cours — PID: $(pgrep -f account_bot.py | tail -1)"
else
    echo "Echec — verifier : $BOT_LOG"
    tail -20 "$BOT_LOG"
fi
