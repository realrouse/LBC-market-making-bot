#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  POLYMARKET LIVE BOT v3 — Lancement
#  Remplir les clés ci-dessous AVANT de lancer
#  (obtenues via : python3 scripts/setup.py 0xTA_PRIVATE_KEY)
# ═══════════════════════════════════════════════════════════════════

export POLY_PRIVATE_KEY="0xTA_PRIVATE_KEY_ICI"
export POLY_API_KEY="TON_API_KEY_ICI"
export POLY_API_SECRET="TON_API_SECRET_ICI"
export POLY_PASSPHRASE="TON_PASSPHRASE_ICI"

# ── Vérification ──────────────────────────────────────────────────
if [ "$POLY_PRIVATE_KEY" = "0xTA_PRIVATE_KEY_ICI" ]; then
    echo "❌ ERREUR : Remplis les clés dans start_bot.sh d'abord"
    echo "   Lance : python3 scripts/setup.py 0xTA_PRIVATE_KEY"
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
