#!/usr/bin/env python3
"""
Download OHLCV kline history from Binance public API (no credentials required).
Stores data in an SQLite database under data/.

Each row represents one candle: timestamp, open, high, low, close, volume.
Intended for grid trading backtesting where fills are detected by price-touch
on the [low, high] range of each candle.

Usage:
    python scripts/download_btc_history.py
    python scripts/download_btc_history.py --symbol BTCUSDT --interval 1m --days 90
    python scripts/download_btc_history.py --symbol BTCUSDT --interval 1h --days 365
    python scripts/download_btc_history.py --out /path/to/custom.db

Output DB schema:
    klines(ts_ms INTEGER PK, open REAL, high REAL, low REAL,
           close REAL, volume REAL, close_ms INTEGER)
"""

import argparse
import asyncio
import sqlite3
import sys
import time
from pathlib import Path

import aiohttp

BASE_URL = "https://api.binance.com"

SCHEMA = """
CREATE TABLE IF NOT EXISTS klines (
    ts_ms    INTEGER PRIMARY KEY,   -- candle open time (Unix ms)
    open     REAL    NOT NULL,
    high     REAL    NOT NULL,
    low      REAL    NOT NULL,
    close    REAL    NOT NULL,
    volume   REAL    NOT NULL,
    close_ms INTEGER NOT NULL       -- candle close time (Unix ms)
);
CREATE INDEX IF NOT EXISTS idx_klines_close ON klines(close_ms);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_INTERVAL_MS = {
    "1m":  60_000,       "3m":  180_000,     "5m":   300_000,
    "15m": 900_000,      "30m": 1_800_000,   "1h":   3_600_000,
    "2h":  7_200_000,    "4h":  14_400_000,  "6h":   21_600_000,
    "8h":  28_800_000,   "12h": 43_200_000,  "1d":   86_400_000,
}


async def _fetch_klines(session, symbol, interval, start_ms, end_ms):
    params = {
        "symbol":    symbol,
        "interval":  interval,
        "startTime": start_ms,
        "endTime":   end_ms,
        "limit":     1000,
    }
    async with session.get(
        f"{BASE_URL}/api/v3/klines",
        params=params,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            raise RuntimeError(f"Rate-limited — attente {retry_after}s")
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"HTTP {resp.status}: {body[:200]}")
        return await resp.json(content_type=None)


async def download(symbol: str, interval: str, days: int, db_path: str) -> int:
    """Download klines and store them in db_path.  Returns rows inserted."""
    step_ms  = _INTERVAL_MS[interval]
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO meta VALUES ('symbol',?)",    (symbol,))
    conn.execute(
        "INSERT OR REPLACE INTO meta VALUES ('interval',?)",  (interval,))
    conn.execute(
        "INSERT OR REPLACE INTO meta VALUES ('downloaded_at',?)",
        (str(int(time.time())),))
    conn.commit()

    # Resume from last stored candle if any.
    row = conn.execute("SELECT MAX(ts_ms) FROM klines").fetchone()
    if row and row[0]:
        start_ms = row[0] + step_ms
        print(f"  Reprise depuis la dernière bougie enregistrée")

    total_expected = (end_ms - start_ms) // step_ms
    inserted = 0

    async with aiohttp.ClientSession() as session:
        current = start_ms
        consecutive_errors = 0

        while current < end_ms:
            batch_end = min(current + 1000 * step_ms, end_ms)
            try:
                rows = await _fetch_klines(
                    session, symbol, interval, current, batch_end)
                consecutive_errors = 0
            except RuntimeError as exc:
                consecutive_errors += 1
                delay = min(5 * consecutive_errors, 60)
                print(f"\n  Erreur : {exc} — retry dans {delay}s")
                await asyncio.sleep(delay)
                if consecutive_errors >= 5:
                    print("  5 erreurs consécutives — abandon")
                    break
                continue

            if not rows:
                break

            conn.executemany(
                "INSERT OR IGNORE INTO klines(ts_ms,open,high,low,close,volume,close_ms)"
                " VALUES (?,?,?,?,?,?,?)",
                [
                    (int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                     float(r[4]), float(r[5]), int(r[6]))
                    for r in rows
                ],
            )
            conn.commit()
            inserted += len(rows)
            current = int(rows[-1][0]) + step_ms

            done = current - start_ms
            total = end_ms - start_ms
            pct   = min(100.0, done / total * 100)
            bar   = "█" * int(pct // 5) + "░" * (20 - int(pct // 5))
            print(
                f"\r  [{bar}] {pct:5.1f}%  {inserted:>8,} lignes",
                end="", flush=True,
            )

            await asyncio.sleep(0.12)   # ~8 req/s  (weight 2 per req, limit 1200/min)

    conn.close()
    print(f"\r  {'█'*20} 100.0%  {inserted:>8,} lignes{' '*20}")
    return inserted


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Télécharger l'historique OHLCV BTCUSDT depuis Binance",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--symbol",   default="BTCUSDT",
                        help="Paire de trading Binance")
    parser.add_argument("--interval", default="1m",
                        choices=sorted(_INTERVAL_MS),
                        help="Intervalle des bougies")
    parser.add_argument("--days",     default=90, type=int,
                        help="Nombre de jours d'historique")
    parser.add_argument("--out",      default=None,
                        help="Chemin du fichier SQLite de sortie")
    args = parser.parse_args()

    if args.interval not in _INTERVAL_MS:
        print(f"Intervalle invalide : {args.interval}", file=sys.stderr)
        sys.exit(1)

    data_dir = Path(__file__).resolve().parent.parent / "data"
    data_dir.mkdir(exist_ok=True)
    db_path = args.out or str(
        data_dir / f"btc_ohlcv_{args.symbol}_{args.interval}_{args.days}d.db"
    )

    step_ms = _INTERVAL_MS[args.interval]
    expected = args.days * 86_400_000 // step_ms

    print(f"Symbole   : {args.symbol}")
    print(f"Intervalle: {args.interval}  ({args.days} jours → ~{expected:,} bougies)")
    print(f"Source    : Binance public API (pas d'authentification)")
    print(f"Sortie    : {db_path}")
    print()

    t0 = time.time()
    inserted = asyncio.run(download(args.symbol, args.interval, args.days, db_path))
    elapsed  = time.time() - t0

    size_mb = Path(db_path).stat().st_size / 1_048_576
    print(f"Durée     : {elapsed:.1f}s")
    print(f"Taille DB : {size_mb:.1f} MB")
    print(f"✓ {inserted:,} bougies téléchargées dans {db_path}")


if __name__ == "__main__":
    main()
