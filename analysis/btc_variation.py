#!/usr/bin/env python3
"""
BTC price variation report from local collector databases.

Usage:
    python3 analysis/btc_variation.py
    python3 analysis/btc_variation.py --days 7
    python3 analysis/btc_variation.py --db data/ob_scalping_c4_*.db
    python3 analysis/btc_variation.py --days 3 --db data/swing_cex_c5_*.db
"""
import argparse
import datetime
import glob
import os
import sqlite3
import sys

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_MIN_PRICE = 1_000.0  # USD — filters out Polymarket probabilities and stale zeros


def _find_best_db() -> str | None:
    """Return the most recent .db in data/ that contains BTCUSDT ob_snapshots."""
    candidates = []
    for path in glob.glob(os.path.join(_DATA_DIR, "*.db")):
        try:
            con = sqlite3.connect(path)
            row = con.execute(
                "SELECT MAX(ts_ms), best_bid FROM ob_snapshots"
                " WHERE best_bid > ? LIMIT 1",
                (_MIN_PRICE,),
            ).fetchone()
            con.close()
            if row and row[0]:
                candidates.append((row[0], path))
        except Exception:
            pass
    if not candidates:
        # Fall back: any snapshots table with BTCUSDT prices
        for path in glob.glob(os.path.join(_DATA_DIR, "*.db")):
            try:
                con = sqlite3.connect(path)
                row = con.execute(
                    "SELECT MAX(ts_ms), best_bid FROM snapshots"
                    " WHERE best_bid > ? LIMIT 1",
                    (_MIN_PRICE,),
                ).fetchone()
                con.close()
                if row and row[0]:
                    candidates.append((row[0], path))
            except Exception:
                pass
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _detect_table(con: sqlite3.Connection) -> tuple[str, str]:
    """Return (table_name, price_col) for BTCUSDT data in this connection."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "ob_snapshots" in tables:
        row = con.execute(
            "SELECT best_bid FROM ob_snapshots WHERE best_bid > ? LIMIT 1", (_MIN_PRICE,)
        ).fetchone()
        if row:
            return "ob_snapshots", "best_bid"
    if "snapshots" in tables:
        row = con.execute(
            "SELECT best_bid FROM snapshots WHERE best_bid > ? LIMIT 1", (_MIN_PRICE,)
        ).fetchone()
        if row:
            return "snapshots", "best_bid"
    raise RuntimeError("No BTCUSDT price table found (ob_snapshots or snapshots with price > 1000)")


def _daily_ohlc(con: sqlite3.Connection, table: str, col: str, since_ms: int) -> list[tuple]:
    rows = con.execute(
        f"""
        SELECT
            date(ts_ms/1000, 'unixepoch')             AS day,
            MIN({col})                                 AS low,
            MAX({col})                                 AS high,
            (SELECT {col} FROM {table} s2
             WHERE date(s2.ts_ms/1000,'unixepoch') = date(s1.ts_ms/1000,'unixepoch')
               AND s2.{col} > {_MIN_PRICE}
             ORDER BY s2.ts_ms ASC LIMIT 1)            AS open,
            (SELECT {col} FROM {table} s3
             WHERE date(s3.ts_ms/1000,'unixepoch') = date(s1.ts_ms/1000,'unixepoch')
               AND s3.{col} > {_MIN_PRICE}
             ORDER BY s3.ts_ms DESC LIMIT 1)           AS close,
            COUNT(*)                                   AS ticks
        FROM {table} s1
        WHERE ts_ms >= ? AND {col} > {_MIN_PRICE}
        GROUP BY day
        ORDER BY day
        """,
        (since_ms,),
    ).fetchall()
    return rows


def report(db_path: str, days: int) -> None:
    since_dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    since_ms = int(since_dt.timestamp() * 1000)

    con = sqlite3.connect(db_path)
    table, col = _detect_table(con)

    # Overall first / last price in the window
    row_first = con.execute(
        f"SELECT ts_ms, {col} FROM {table}"
        f" WHERE ts_ms >= ? AND {col} > {_MIN_PRICE} ORDER BY ts_ms ASC LIMIT 1",
        (since_ms,),
    ).fetchone()
    row_last = con.execute(
        f"SELECT ts_ms, {col} FROM {table}"
        f" WHERE {col} > {_MIN_PRICE} ORDER BY ts_ms DESC LIMIT 1",
    ).fetchone()
    stats = con.execute(
        f"SELECT MIN({col}), MAX({col}), AVG({col}), COUNT(*) FROM {table}"
        f" WHERE ts_ms >= ? AND {col} > {_MIN_PRICE}",
        (since_ms,),
    ).fetchone()
    daily = _daily_ohlc(con, table, col, since_ms)
    con.close()

    if not row_first or not row_last:
        print("No data found for the requested period.")
        return

    def fmt_ts(ts_ms: int) -> str:
        return datetime.datetime.fromtimestamp(ts_ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    price_start = row_first[1]
    price_end   = row_last[1]
    var_pct     = (price_end - price_start) / price_start * 100
    var_usd     = price_end - price_start
    arrow       = "▲" if var_pct >= 0 else "▼"

    db_name = os.path.basename(db_path)
    width   = 64

    print("=" * width)
    print(f"  BTC PRICE VARIATION — last {days} day{'s' if days != 1 else ''}")
    print(f"  Source : {db_name}  [{table}]")
    print("=" * width)
    print(f"  Start  : {fmt_ts(row_first[0])}   ${price_start:,.2f}")
    print(f"  End    : {fmt_ts(row_last[0])}   ${price_end:,.2f}")
    print(f"  Change : {arrow} {var_pct:+.2f}%  (${var_usd:+,.2f})")
    print(f"  Range  : ${stats[0]:,.2f} — ${stats[1]:,.2f}  (avg ${stats[2]:,.2f})")
    print(f"  Ticks  : {stats[3]:,}")
    print("-" * width)
    if daily:
        print(f"  {'Day':<12} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10}  {'Δ':>7}  Ticks")
        print(f"  {'-'*12} {'-'*10} {'-'*10} {'-'*10} {'-'*10}  {'-'*7}  -----")
        for d in daily:
            day, low, high, open_, close, ticks = d
            if open_ and close:
                chg = (close - open_) / open_ * 100
                bar = "▲" if chg >= 0 else "▼"
                print(
                    f"  {day:<12} {open_:>10,.2f} {high:>10,.2f} {low:>10,.2f}"
                    f" {close:>10,.2f}  {bar}{abs(chg):>5.2f}%  {ticks:,}"
                )
    print("=" * width)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BTC price variation from local collector databases"
    )
    parser.add_argument(
        "--days", type=int, default=3,
        help="Number of days to look back (default: 3)"
    )
    parser.add_argument(
        "--db", type=str, default=None,
        help="Path to the SQLite database (auto-detected if omitted)"
    )
    args = parser.parse_args()

    db_path = args.db
    if db_path:
        matches = glob.glob(db_path)
        if not matches:
            print(f"Error: no database found matching '{db_path}'", file=sys.stderr)
            sys.exit(1)
        db_path = sorted(matches)[-1]
    else:
        db_path = _find_best_db()
        if not db_path:
            print(
                f"Error: no BTCUSDT database found in {_DATA_DIR}/\n"
                "Run a collector first or pass --db explicitly.",
                file=sys.stderr,
            )
            sys.exit(1)

    report(db_path, args.days)


if __name__ == "__main__":
    main()
