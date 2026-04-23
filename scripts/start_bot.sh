#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  POLYMARKET LIVE BOT v3 — Lancement
#  Prérequis : python3 scripts/setup.py 0xTA_PRIVATE_KEY
#  (génère /opt/polymarket-live/config.json)
# ═══════════════════════════════════════════════════════════════════

CONFIG="/opt/polymarket-live/config.json"

# ── Vérification ──────────────────────────────────────────────────
if [ ! -f "$CONFIG" ]; then
    echo "❌ ERREUR : config.json introuvable"
    echo "   Lance d'abord : python3 scripts/setup.py 0xTA_PRIVATE_KEY"
    exit 1
fi

# ── Stop instance existante ───────────────────────────────────────
if pgrep -f live_bot.py > /dev/null; then
    echo "Arrêt de l'instance existante..."
    pkill -f live_bot.py
    sleep 2
fi

# ── Lancement ─────────────────────────────────────────────────────
echo "Lancement du bot..."
nohup python3 /opt/polymarket-live/live_bot.py > /dev/null 2>&1 &
echo "PID: $!"
sleep 3

if pgrep -f live_bot.py > /dev/null; then
    echo "✅ Bot en cours — PID: $(pgrep -f live_bot.py)"
    echo "Logs: tail -f /opt/polymarket-live/live.log"
else
    echo "❌ Bot arrêté — vérifier les logs:"
    tail -20 /opt/polymarket-live/live.log
fi
