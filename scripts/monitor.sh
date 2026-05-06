#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  POLYMARKET LIVE BOT v3 — Monitoring / Surveillance
#  Affiche le statut du bot, les derniers logs et les stats SQLite.
#  Shows bot status, recent logs, and SQLite stats.
#
#  Usage :
#    bash scripts/monitor.sh
#    TRADINEBOTTE_DIR=~/tradinebotte bash scripts/monitor.sh
# ═══════════════════════════════════════════════════════════════════

INSTALL_DIR="${TRADINEBOTTE_DIR:-$HOME/tradinebotte}"
INSTALL_DIR="$(eval echo "$INSTALL_DIR")"
CONFIG="$INSTALL_DIR/config.json"

# ── Language ──────────────────────────────────────────────────────
# Read the language preference saved by setup.py in config.json.
# Defaults to EN if config.json is absent.
LANG="EN"
if [ -f "$CONFIG" ]; then
    LANG=$(python3 -c "
import json
try:
    print(json.load(open('$CONFIG')).get('lang', 'EN'))
except Exception:
    print('EN')
" 2>/dev/null || echo "EN")
fi

# _t "EN text" "FR text" — print the string for the current language (no trailing newline)
_t() { [ "$LANG" = "FR" ] && printf '%s' "$2" || printf '%s' "$1"; }

# ── Status ────────────────────────────────────────────────────────
echo "=== STATUS ($INSTALL_DIR) ==="
if pgrep -fa live_bot.py > /dev/null 2>&1; then
    pgrep -fa live_bot.py
    echo "✅ $(_t "BOT RUNNING" "BOT EN COURS")"
else
    echo "❌ $(_t "BOT STOPPED" "BOT ARRÊTÉ")"
fi

# ── Recent logs ───────────────────────────────────────────────────
echo ""
echo "=== $(_t "RECENT LOGS" "DERNIERS LOGS") ==="
tail -15 "$INSTALL_DIR/live.log"

# ── Global stats ──────────────────────────────────────────────────
echo ""
echo "=== $(_t "GLOBAL STATS" "STATS GLOBALES") ==="

# SQL column aliases are translated so the output header matches the chosen language.
_col_trades=$(_t  "trades"          "trades")
_col_wins=$(_t    "wins"            "victoires")
_col_losses=$(_t  "losses"          "pertes")
_col_winpct=$(_t  "win_pct"         "pct_victoire")
_col_pnl=$(_t     "total_pnl"       "pnl_total")
_col_capital=$(_t "current_capital" "capital_actuel")

sqlite3 "$INSTALL_DIR/live.db" "
SELECT
  COUNT(*) as $_col_trades,
  SUM(CASE WHEN outcome='WIN'  THEN 1 ELSE 0 END) as $_col_wins,
  SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as $_col_losses,
  ROUND(100.0 * SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) / MAX(COUNT(*), 1), 1) as $_col_winpct,
  ROUND(SUM(pnl_net), 3)       as $_col_pnl,
  ROUND(MAX(capital_after), 2) as $_col_capital
FROM trades WHERE resolved = 1;
"

# ── Recent trades ─────────────────────────────────────────────────
echo ""
echo "=== $(_t "RECENT TRADES" "DERNIERS TRADES") ==="
sqlite3 "$INSTALL_DIR/live.db" "
SELECT id, direction, ROUND(entry_price, 4), outcome, ROUND(pnl_net, 3), ROUND(capital_after, 2)
FROM trades ORDER BY id DESC LIMIT 10;
"
