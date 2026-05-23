#!/usr/bin/env python3
"""
Build an extended daily OHLCV database merging two sources:
  1. Bitstamp public API  — BTC/USD daily candles 2011 → 2017-08-16
  2. Existing Binance 1m  — aggregated to daily       2017-08-17 → present

Output: data/BTCUSD_daily_extended.db  (single `daily` table, no credentials needed)

Schema
------
    daily(
        day_ts  INTEGER PRIMARY KEY,   -- UTC midnight Unix seconds
        open    REAL,
        high    REAL,
        low     REAL,
        close   REAL,
        volume  REAL,
        source  TEXT                   -- 'bitstamp' | 'binance'
    )

Usage
-----
    python3 scripts/download_btc_daily_extended.py
    python3 scripts/download_btc_daily_extended.py --start 2013-01-01
    python3 scripts/download_btc_daily_extended.py --binance-db data/BTCUSDT3197d_20172026.db
"""

import argparse
import asyncio
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

BITSTAMP_URL = "https://www.bitstamp.net/api/v2/ohlc/btcusd/"
BITSTAMP_STEP = 86400        # daily candles (seconds)
BITSTAMP_LIMIT = 1000

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    day_ts  INTEGER PRIMARY KEY,
    open    REAL    NOT NULL,
    high    REAL    NOT NULL,
    low     REAL    NOT NULL,
    close   REAL    NOT NULL,
    volume  REAL    NOT NULL,
    source  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_daily_ts ON daily(day_ts);
"""


def _parse_date(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp())


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Bitstamp downloader
# ---------------------------------------------------------------------------
async def _fetch_bitstamp(session, start: int, end: int) -> list:
    params = {
        "step":  BITSTAMP_STEP,
        "start": start,
        "end":   end,
        "limit": BITSTAMP_LIMIT,
    }
    async with session.get(
        BITSTAMP_URL, params=params,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {await resp.text()}")
        data = await resp.json(content_type=None)
    return data.get("data", {}).get("ohlc", [])


async def download_bitstamp(db: sqlite3.Connection, start: int, end: int) -> int:
    """Download Bitstamp daily candles and insert into db.  Returns rows inserted."""
    # Resume from last stored bitstamp candle
    row = db.execute(
        "SELECT MAX(day_ts) FROM daily WHERE source='bitstamp'"
    ).fetchone()
    if row and row[0]:
        start = row[0] + BITSTAMP_STEP
        print("  Bitstamp: resuming from last stored candle")

    inserted = 0
    async with aiohttp.ClientSession() as session:
        current = start
        while current < end:
            batch_end = min(current + BITSTAMP_LIMIT * BITSTAMP_STEP, end)
            try:
                candles = await _fetch_bitstamp(session, current, batch_end)
            except RuntimeError as exc:
                print(f"\n  Bitstamp error: {exc} — retrying in 5s")
                await asyncio.sleep(5)
                continue

            if not candles:
                current = batch_end + BITSTAMP_STEP
                continue

            db.executemany(
                "INSERT OR IGNORE INTO daily(day_ts,open,high,low,close,volume,source)"
                " VALUES (?,?,?,?,?,?,'bitstamp')",
                [
                    (
                        int(c["timestamp"]),
                        float(c["open"]),
                        float(c["high"]),
                        float(c["low"]),
                        float(c["close"]),
                        float(c["volume"]),
                    )
                    for c in candles
                ],
            )
            db.commit()
            inserted += len(candles)
            current = int(candles[-1]["timestamp"]) + BITSTAMP_STEP

            done  = current - start
            total = end - start
            pct   = min(100.0, done / total * 100)
            bar   = "█" * int(pct // 5) + "░" * (20 - int(pct // 5))
            print(f"\r  Bitstamp [{bar}] {pct:5.1f}%  {inserted:>6,} days",
                  end="", flush=True)
            await asyncio.sleep(0.5)

    print(f"\r  Bitstamp {'█'*20} 100.0%  {inserted:>6,} days{' '*10}")
    return inserted


# ---------------------------------------------------------------------------
# Aggregate Binance 1m → daily
# ---------------------------------------------------------------------------
def aggregate_binance(binance_db_path: str, out_db: sqlite3.Connection,
                      cutoff_ts: int) -> int:
    """Aggregate Binance 1m klines to daily and insert into out_db.
    Only processes candles with day_ts >= cutoff_ts."""
    print(f"  Binance: aggregating 1m klines from {_fmt(cutoff_ts)} …")
    src = sqlite3.connect(binance_db_path)

    rows = src.execute("""
        SELECT (ts_ms/86400000)*86400 AS day_ts,
               MAX(high)   AS high,
               MIN(low)    AS low,
               SUM(volume) AS volume,
               MAX(ts_ms)  AS last_ms,
               MIN(ts_ms)  AS first_ms
        FROM klines
        WHERE (ts_ms/86400000)*86400 >= ?
        GROUP BY day_ts
        ORDER BY day_ts
    """, (cutoff_ts,)).fetchall()

    if not rows:
        src.close()
        return 0

    last_ms_list  = [r[4] for r in rows]
    first_ms_list = [r[5] for r in rows]
    phs = ",".join("?" * len(rows))

    close_map = dict(src.execute(
        f"SELECT ts_ms, close FROM klines WHERE ts_ms IN ({phs})",
        last_ms_list,
    ).fetchall())
    open_map = dict(src.execute(
        f"SELECT ts_ms, open FROM klines WHERE ts_ms IN ({phs})",
        first_ms_list,
    ).fetchall())
    src.close()

    inserted = 0
    for r in rows:
        day_ts = r[0]
        close_px = close_map.get(r[4])
        open_px  = open_map.get(r[5])
        if close_px is None or open_px is None:
            continue
        out_db.execute(
            "INSERT OR IGNORE INTO daily(day_ts,open,high,low,close,volume,source)"
            " VALUES (?,?,?,?,?,?,'binance')",
            (day_ts, open_px, r[1], r[2], close_px, r[3]),
        )
        inserted += 1

    out_db.commit()
    print(f"  Binance: {inserted:,} daily bars inserted")
    return inserted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    default_binance = str(
        Path(__file__).resolve().parent.parent
        / "data" / "BTCUSDT3197d_20172026.db"
    )
    default_out = str(
        Path(__file__).resolve().parent.parent
        / "data" / "BTCUSD_daily_extended.db"
    )

    parser = argparse.ArgumentParser(
        description="Build extended daily OHLCV DB merging Bitstamp (pre-2017) + Binance",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--start",      default="2013-01-01", metavar="YYYY-MM-DD",
                        help="Bitstamp download start date")
    parser.add_argument("--binance-db", default=default_binance,
                        help="Path to the Binance 1m klines SQLite DB")
    parser.add_argument("--out",        default=default_out,
                        help="Output daily DB path")
    args = parser.parse_args()

    out_path = args.out
    Path(out_path).parent.mkdir(exist_ok=True)

    conn = sqlite3.connect(out_path)
    conn.executescript(SCHEMA)

    # --- Bitstamp: from --start up to the day before Binance data begins ---
    # Binance BTCUSDT begins 2017-08-17; use 2017-08-16 as Bitstamp cutoff
    bitstamp_end = _parse_date("2017-08-17")   # exclusive upper bound
    bitstamp_start = _parse_date(args.start)

    print(f"Step 1/2  Download Bitstamp daily candles"
          f"  {args.start} → 2017-08-16")
    t0 = time.time()
    n_bs = asyncio.run(download_bitstamp(conn, bitstamp_start, bitstamp_end))
    print(f"  {time.time()-t0:.1f}s  {n_bs:,} Bitstamp candles\n")

    # --- Binance: aggregate from the Binance 1m DB ---
    print(f"Step 2/2  Aggregate Binance 1m → daily  (2017-08-17 → present)")
    if not Path(args.binance_db).exists():
        print(f"  WARNING: Binance DB not found: {args.binance_db} — skipping")
        n_bn = 0
    else:
        t0 = time.time()
        n_bn = aggregate_binance(args.binance_db, conn, _parse_date("2017-08-17"))
        print(f"  {time.time()-t0:.1f}s\n")

    # Summary
    total = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
    span  = conn.execute("SELECT MIN(day_ts), MAX(day_ts) FROM daily").fetchone()
    conn.close()

    size_mb = Path(out_path).stat().st_size / 1_048_576
    print(f"Output    : {out_path}")
    print(f"Range     : {_fmt(span[0])} → {_fmt(span[1])}")
    print(f"Total     : {total:,} daily bars  ({n_bs:,} Bitstamp + {n_bn:,} Binance)")
    print(f"Size      : {size_mb:.1f} MB")
    print(f"✓ Done")


if __name__ == "__main__":
    main()
