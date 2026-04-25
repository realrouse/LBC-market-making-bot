#!/bin/bash
INSTALL_DIR="${POLYMARKET_DIR:-$HOME/polymarket}"
INSTALL_DIR="$(eval echo "$INSTALL_DIR")"

echo "=== STATUS ($INSTALL_DIR) ==="
pgrep -fa live_bot.py && echo "✅ BOT EN COURS" || echo "❌ BOT ARRETE"

echo ""
echo "=== DERNIERS LOGS ==="
tail -15 "$INSTALL_DIR/live.log"

echo ""
echo "=== STATS GLOBALES ==="
sqlite3 "$INSTALL_DIR/live.db" "
SELECT
  COUNT(*) as trades,
  SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
  SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as losses,
  ROUND(100.0*SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END)/MAX(COUNT(*),1),1) as win_pct,
  ROUND(SUM(pnl_net),3) as total_pnl,
  ROUND(MAX(capital_after),2) as capital_actuel
FROM trades WHERE resolved=1;
"

echo ""
echo "=== DERNIERS TRADES ==="
sqlite3 "$INSTALL_DIR/live.db" "
SELECT id, direction, ROUND(entry_price,4), outcome, ROUND(pnl_net,3), ROUND(capital_after,2)
FROM trades ORDER BY id DESC LIMIT 10;
"
