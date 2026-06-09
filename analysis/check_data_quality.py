#!/usr/bin/env python3
"""
Data quality checks for tradinebotte collector databases.

Scans all .db files in data/ (or a specific path) and reports invariant
violations grouped by database and table.

Usage:
    python3 analysis/check_data_quality.py
    python3 analysis/check_data_quality.py --db data/ob_scalping_c4_*.db
    python3 analysis/check_data_quality.py --warn-only      # always exit 0
    python3 analysis/check_data_quality.py --verbose        # show PASS too
    python3 analysis/check_data_quality.py --no-gaps        # skip slow gap scan

Exit codes: 0 = all PASS/WARN,  1 = at least one FAIL (unless --warn-only).
"""
import argparse
import datetime
import glob
import os
import sqlite3
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Generator

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

_CEX_PRICE_MIN   = 1_000.0
_CEX_PRICE_MAX   = 250_000.0
_GAP_WARN_S      = 600      # 10 min
_GAP_FAIL_S      = 7_200    # 2 h
_KLINES_GAP_MULT = 3        # allow up to 3× expected interval before flagging


class Status(Enum):
    PASS = "✓"
    WARN = "⚠"
    FAIL = "✗"


@dataclass
class Result:
    name: str
    status: Status
    detail: str


def _r(name: str, ok: bool, detail: str, *, warn: bool = False) -> Result:
    status = Status.PASS if ok else (Status.WARN if warn else Status.FAIL)
    return Result(name, status, detail)


# ── Database classification ────────────────────────────────────────────────────

class DbKind(Enum):
    OB_CEX      = "ob_snapshots (CEX BTCUSDT)"
    CEX_SNAP    = "snapshots (CEX BTCUSDT)"
    POLYMARKET  = "snapshots (Polymarket)"
    KLINES      = "klines (OHLCV history)"
    DAILY       = "daily (OHLCV daily)"
    UNKNOWN     = "unknown"


def _classify(con: sqlite3.Connection) -> DbKind:
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "ob_snapshots" in tables:
        return DbKind.OB_CEX
    if "klines" in tables:
        return DbKind.KLINES
    if "daily" in tables:
        return DbKind.DAILY
    if "snapshots" in tables:
        row = con.execute("SELECT MAX(best_bid) FROM snapshots WHERE best_bid IS NOT NULL").fetchone()
        if row and row[0] is not None:
            return DbKind.CEX_SNAP if row[0] > _CEX_PRICE_MIN else DbKind.POLYMARKET
    return DbKind.UNKNOWN


def _fmt_ts(ts_ms: int) -> str:
    return datetime.datetime.fromtimestamp(ts_ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%d")


def _fmt_gap(seconds: float) -> str:
    h, s = divmod(int(seconds), 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# ── Gap detector (shared by ob_snapshots, snapshots CEX, klines) ───────────────

def _gap_check(
    con: sqlite3.Connection,
    table: str,
    ts_col: str,
    where: str = "",
    warn_s: int = _GAP_WARN_S,
    fail_s: int = _GAP_FAIL_S,
) -> Result:
    wclause = f"WHERE {where}" if where else ""
    row = con.execute(f"""
        SELECT MAX(gap_ms) / 1000.0, date(ts_ms/1000,'unixepoch')
        FROM (
            SELECT {ts_col} AS ts_ms,
                   ({ts_col} - LAG({ts_col}) OVER (ORDER BY {ts_col})) AS gap_ms
            FROM {table}
            {wclause}
        )
        WHERE gap_ms > {warn_s * 1000}
        ORDER BY gap_ms DESC LIMIT 1
    """).fetchone()

    if not row or row[0] is None:
        return _r("timestamp gaps", True, f"no gap > {warn_s}s")
    max_gap_s, day = row[0], row[1]
    detail = f"max gap {_fmt_gap(max_gap_s)} on {day}"
    ok_for_fail = max_gap_s < fail_s
    return _r("timestamp gaps", ok_for_fail, detail, warn=ok_for_fail)


# ── Trade invariant checks (shared across DB kinds) ───────────────────────────

def _check_trades(con: sqlite3.Connection, table: str = "trades") -> Generator[Result, None, None]:
    tbls = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if table not in tbls:
        return

    cnt = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if cnt == 0:
        yield Result(table, Status.PASS, "0 rows")
        return

    # Detect schema variant: Polymarket trades use 'resolved'/'resolution_ts_ms',
    # ob_trades use 'exit_reason'/'exit_ts_ms'.
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    if "resolved" in cols:
        closed_filter   = "resolved = 1"
        exit_ts_col     = "resolution_ts_ms"
    elif "exit_reason" in cols:
        closed_filter   = "exit_reason IS NOT NULL"
        exit_ts_col     = "exit_ts_ms"
    else:
        yield Result(table, Status.WARN, "unrecognised schema — trade checks skipped")
        return

    # Capital invariant on closed trades
    if "capital_before" in cols and "capital_after" in cols and "pnl_net" in cols:
        bad = con.execute(f"""
            SELECT COUNT(*) FROM {table}
            WHERE {closed_filter}
              AND capital_before IS NOT NULL
              AND ABS(capital_after - capital_before - pnl_net) > 0.01
        """).fetchone()[0]
        yield _r("capital invariant", bad == 0, f"{bad} violations / {cnt} trades")

    # Stake positive
    if "stake" in cols:
        bad_stake = con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE stake IS NOT NULL AND stake <= 0"
        ).fetchone()[0]
        yield _r("stake > 0", bad_stake == 0, f"{bad_stake} trades with stake ≤ 0")

    # Temporal: exit after entry
    if exit_ts_col in cols and "entry_ts_ms" in cols:
        bad_ts = con.execute(f"""
            SELECT COUNT(*) FROM {table}
            WHERE {closed_filter}
              AND entry_ts_ms IS NOT NULL
              AND {exit_ts_col} IS NOT NULL
              AND entry_ts_ms >= {exit_ts_col}
        """).fetchone()[0]
        yield _r("entry before exit", bad_ts == 0, f"{bad_ts} temporal inversions")


# ── Check sets per DB kind ─────────────────────────────────────────────────────

def _check_ob_cex(con: sqlite3.Connection, scan_gaps: bool) -> list[tuple[str, list[Result]]]:
    results: list[Result] = []
    cnt, t0, t1 = con.execute("SELECT COUNT(*), MIN(ts_ms), MAX(ts_ms) FROM ob_snapshots").fetchone()
    span = f"{_fmt_ts(t0)} → {_fmt_ts(t1)}  ({cnt:,} rows)"

    # Price bounds
    bad_price = con.execute(
        f"SELECT COUNT(*) FROM ob_snapshots WHERE best_bid < {_CEX_PRICE_MIN} OR best_bid > {_CEX_PRICE_MAX}"
    ).fetchone()[0]
    if bad_price == 0:
        lo, hi = con.execute("SELECT MIN(best_bid), MAX(best_bid) FROM ob_snapshots").fetchone()
        results.append(_r("price bounds", True, f"${lo:,.0f} – ${hi:,.0f}"))
    else:
        results.append(_r("price bounds", False, f"{bad_price:,} rows outside [${_CEX_PRICE_MIN:,.0f}, ${_CEX_PRICE_MAX:,.0f}]"))

    # OBI bounds — column may not exist in all schemas
    try:
        bad_obi, lo_obi, hi_obi = con.execute(
            "SELECT SUM(CASE WHEN ABS(obi_raw) > 1.001 THEN 1 ELSE 0 END), MIN(obi_raw), MAX(obi_raw) FROM ob_snapshots"
        ).fetchone()
        bad_obi = bad_obi or 0
        results.append(_r("OBI bounds", bad_obi == 0, f"[{lo_obi:.4f}, {hi_obi:.4f}]  {bad_obi} out-of-bounds"))
    except sqlite3.OperationalError:
        results.append(Result("OBI bounds", Status.WARN, "obi_raw column absent"))

    # Spread validity
    bad_spread = con.execute(
        "SELECT COUNT(*) FROM ob_snapshots WHERE best_ask < best_bid"
    ).fetchone()[0]
    results.append(_r("spread (ask≥bid)", bad_spread == 0, f"{bad_spread} inverted rows", warn=True))

    # NULL check
    bad_null = con.execute(
        "SELECT COUNT(*) FROM ob_snapshots WHERE ts_ms IS NULL OR best_bid IS NULL"
    ).fetchone()[0]
    results.append(_r("no NULL in ts_ms/best_bid", bad_null == 0, f"{bad_null} NULL rows"))

    # Timestamp gaps
    if scan_gaps:
        results.append(_gap_check(con, "ob_snapshots", "ts_ms"))

    sections = [(f"ob_snapshots  {span}", results)]
    sections += [("trades", list(_check_trades(con, "ob_trades")))]
    return sections


def _check_cex_snap(con: sqlite3.Connection, scan_gaps: bool) -> list[tuple[str, list[Result]]]:
    results: list[Result] = []
    cnt, t0, t1 = con.execute("SELECT COUNT(*), MIN(ts_ms), MAX(ts_ms) FROM snapshots").fetchone()
    span = f"{_fmt_ts(t0)} → {_fmt_ts(t1)}  ({cnt:,} rows)"

    # Type contamination (Polymarket rows mixed in)
    bad_type = con.execute(
        f"SELECT COUNT(*) FROM snapshots WHERE best_bid < {_CEX_PRICE_MIN}"
    ).fetchone()[0]
    if bad_type > 0:
        sample = con.execute(
            f"SELECT market_id FROM snapshots WHERE best_bid < {_CEX_PRICE_MIN} LIMIT 1"
        ).fetchone()[0]
        is_poly = len(sample) > 40  # Polymarket condition IDs are 64-char hex
        kind = "Polymarket rows" if is_poly else "rows with price < $1000"
        results.append(_r("type contamination", False, f"{bad_type:,} {kind} in CEX table"))
    else:
        results.append(_r("type contamination", True, "0 rows with best_bid < $1000"))

    # Price bounds on CEX rows
    lo, hi = con.execute(
        f"SELECT MIN(best_bid), MAX(best_bid) FROM snapshots WHERE best_bid > {_CEX_PRICE_MIN}"
    ).fetchone()
    bad_hi = con.execute(
        f"SELECT COUNT(*) FROM snapshots WHERE best_bid > {_CEX_PRICE_MAX}"
    ).fetchone()[0]
    results.append(_r("CEX price bounds", bad_hi == 0, f"${lo:,.0f} – ${hi:,.0f}  {bad_hi} rows > ${_CEX_PRICE_MAX:,.0f}"))

    # OBI bounds
    try:
        bad_obi, lo_obi, hi_obi = con.execute(
            f"SELECT SUM(CASE WHEN ABS(obi) > 1.001 THEN 1 ELSE 0 END), MIN(obi), MAX(obi)"
            f" FROM snapshots WHERE best_bid > {_CEX_PRICE_MIN}"
        ).fetchone()
        bad_obi = bad_obi or 0
        results.append(_r("OBI bounds", bad_obi == 0, f"[{lo_obi:.4f}, {hi_obi:.4f}]  {bad_obi} out-of-bounds"))
    except sqlite3.OperationalError:
        results.append(Result("OBI bounds", Status.WARN, "obi column absent"))

    # Timestamp gaps (on CEX rows only)
    if scan_gaps:
        results.append(_gap_check(con, "snapshots", "ts_ms", f"best_bid > {_CEX_PRICE_MIN}"))

    sections = [(f"snapshots (CEX)  {span}", results)]
    sections += [("trades", list(_check_trades(con)))]
    return sections


def _check_polymarket(con: sqlite3.Connection) -> list[tuple[str, list[Result]]]:
    results: list[Result] = []
    cnt, t0, t1 = con.execute("SELECT COUNT(*), MIN(ts_ms), MAX(ts_ms) FROM snapshots").fetchone()
    span = f"{_fmt_ts(t0)} → {_fmt_ts(t1)}  ({cnt:,} rows)"

    # Probability bounds — 0 (resolved LOSS) and 1 (resolved WIN) are valid endpoints
    bad_bid = con.execute(
        "SELECT COUNT(*) FROM snapshots WHERE best_bid IS NOT NULL AND (best_bid < 0 OR best_bid > 1)"
    ).fetchone()[0]
    lo, hi = con.execute("SELECT MIN(best_bid), MAX(best_bid) FROM snapshots WHERE best_bid >= 0").fetchone()
    results.append(_r("bid ∈ [0,1]", bad_bid == 0, f"[{lo:.4f}, {hi:.4f}]  {bad_bid} out-of-bounds"))

    bad_ask = con.execute(
        "SELECT COUNT(*) FROM snapshots WHERE best_ask IS NOT NULL AND (best_ask < 0 OR best_ask > 1)"
    ).fetchone()[0]
    results.append(_r("ask ∈ [0,1]", bad_ask == 0, f"{bad_ask} out-of-bounds"))

    # Ask ≥ bid
    bad_spread = con.execute(
        "SELECT COUNT(*) FROM snapshots WHERE best_ask IS NOT NULL AND best_bid IS NOT NULL AND best_ask < best_bid"
    ).fetchone()[0]
    results.append(_r("ask ≥ bid", bad_spread == 0, f"{bad_spread} inverted rows"))

    # Positive spread
    zero_spread = con.execute(
        "SELECT COUNT(*) FROM snapshots WHERE spread IS NOT NULL AND spread <= 0"
    ).fetchone()[0]
    results.append(_r("spread > 0", zero_spread == 0, f"{zero_spread} zero/neg spread rows", warn=True))

    # Direction validity
    try:
        bad_dir = con.execute(
            "SELECT COUNT(*) FROM snapshots WHERE direction NOT IN ('UP', 'DOWN')"
        ).fetchone()[0]
        results.append(_r("direction ∈ {UP,DOWN}", bad_dir == 0, f"{bad_dir} invalid direction values"))
    except sqlite3.OperationalError:
        results.append(Result("direction", Status.WARN, "direction column absent"))

    # NULL on critical fields
    bad_null = con.execute(
        "SELECT COUNT(*) FROM snapshots WHERE market_id IS NULL OR token_id IS NULL"
    ).fetchone()[0]
    results.append(_r("no NULL market_id/token_id", bad_null == 0, f"{bad_null} NULL rows"))

    sections = [(f"snapshots (Polymarket)  {span}", results)]
    sections += [("trades", list(_check_trades(con)))]
    return sections


def _check_klines(con: sqlite3.Connection, scan_gaps: bool) -> list[tuple[str, list[Result]]]:
    results: list[Result] = []
    cnt, t0, t1 = con.execute("SELECT COUNT(*), MIN(ts_ms), MAX(ts_ms) FROM klines").fetchone()

    # Detect interval from meta
    interval_label = "?"
    interval_ms = None
    try:
        row = con.execute("SELECT value FROM meta WHERE key='interval'").fetchone()
        if row:
            interval_label = row[0]
            _map = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000,
                    "4h": 14_400_000, "1d": 86_400_000}
            interval_ms = _map.get(row[0])
    except sqlite3.OperationalError:
        pass

    symbol = "?"
    try:
        row = con.execute("SELECT value FROM meta WHERE key='symbol'").fetchone()
        if row:
            symbol = row[0]
    except sqlite3.OperationalError:
        pass

    span = f"{symbol} {interval_label}  {_fmt_ts(t0)} → {_fmt_ts(t1)}  ({cnt:,} rows)"

    # OHLC validity: high ≥ max(open,close), low ≤ min(open,close)
    bad_ohlc = con.execute("""
        SELECT COUNT(*) FROM klines
        WHERE high < MAX(open, close)
           OR low  > MIN(open, close)
           OR high < low
    """).fetchone()[0]
    results.append(_r("OHLC validity", bad_ohlc == 0, f"{bad_ohlc} candles with high<low or OHLC violation"))

    # All prices positive
    bad_price = con.execute(
        "SELECT COUNT(*) FROM klines WHERE open <= 0 OR high <= 0 OR low <= 0 OR close <= 0"
    ).fetchone()[0]
    results.append(_r("prices > 0", bad_price == 0, f"{bad_price} non-positive price rows"))

    # Volume non-negative
    bad_vol = con.execute("SELECT COUNT(*) FROM klines WHERE volume < 0").fetchone()[0]
    results.append(_r("volume ≥ 0", bad_vol == 0, f"{bad_vol} negative volume rows", warn=True))

    # Timestamp gaps
    if scan_gaps and interval_ms:
        fail_ms  = interval_ms * _KLINES_GAP_MULT
        warn_ms  = int(interval_ms * 1.5)
        row = con.execute(f"""
            SELECT MAX(gap_ms), date(ts_ms/1000,'unixepoch')
            FROM (
                SELECT ts_ms, (ts_ms - LAG(ts_ms) OVER (ORDER BY ts_ms)) AS gap_ms FROM klines
            )
            WHERE gap_ms > {warn_ms}
            ORDER BY gap_ms DESC LIMIT 1
        """).fetchone()
        if not row or row[0] is None:
            results.append(_r("candle continuity", True, f"no gap > 1.5× interval ({interval_label})"))
        else:
            max_gap_s = row[0] / 1000
            # Klines are downloaded historical data — gaps are informational (WARN, never FAIL)
            results.append(Result(
                "candle continuity",
                Status.WARN,
                f"max gap {_fmt_gap(max_gap_s)} on {row[1]}  (exchange maintenance or download gap)",
            ))
    elif scan_gaps:
        results.append(Result("candle continuity", Status.WARN, "interval unknown — gap check skipped"))

    return [(f"klines  {span}", results)]


def _check_daily(con: sqlite3.Connection) -> list[tuple[str, list[Result]]]:
    results: list[Result] = []
    cnt, t0, t1 = con.execute("SELECT COUNT(*), MIN(day_ts), MAX(day_ts) FROM daily").fetchone()
    span = f"{_fmt_ts(t0 * 1000)} → {_fmt_ts(t1 * 1000)}  ({cnt:,} rows)"

    bad_ohlc = con.execute("""
        SELECT COUNT(*) FROM daily
        WHERE high < MAX(open, close)
           OR low  > MIN(open, close)
           OR high < low
    """).fetchone()[0]
    results.append(_r("OHLC validity", bad_ohlc == 0, f"{bad_ohlc} violations"))

    bad_price = con.execute(
        "SELECT COUNT(*) FROM daily WHERE open <= 0 OR high <= 0 OR low <= 0 OR close <= 0"
    ).fetchone()[0]
    results.append(_r("prices > 0", bad_price == 0, f"{bad_price} non-positive price rows"))

    # Date gaps > 2 days
    row = con.execute("""
        SELECT MAX(gap_days), date(day_ts,'unixepoch')
        FROM (
            SELECT day_ts,
                   (day_ts - LAG(day_ts) OVER (ORDER BY day_ts)) / 86400 AS gap_days
            FROM daily
        )
        WHERE gap_days > 2
        ORDER BY gap_days DESC LIMIT 1
    """).fetchone()
    if not row or row[0] is None:
        results.append(_r("date continuity", True, "no gap > 2 days"))
    else:
        results.append(_r("date continuity", False, f"gap of {row[0]} days on {row[1]}", warn=True))

    return [(f"daily  {span}", results)]


# ── Reporting ──────────────────────────────────────────────────────────────────

def _print_section(name: str, results: list[Result], verbose: bool) -> tuple[int, int, int]:
    """Prints one table's results. Returns (n_pass, n_warn, n_fail)."""
    visible = [r for r in results if r.status != Status.PASS or verbose]
    n_pass = sum(1 for r in results if r.status == Status.PASS)
    n_warn = sum(1 for r in results if r.status == Status.WARN)
    n_fail = sum(1 for r in results if r.status == Status.FAIL)

    if not results:
        return 0, 0, 0
    if not visible:
        # all pass, brief summary
        print(f"    [{name}]  all {n_pass} checks passed")
        return n_pass, 0, 0

    print(f"    [{name}]")
    for r in results:
        if r.status == Status.PASS and not verbose:
            continue
        print(f"      {r.status.value}  {r.name:<30}  {r.detail}")
    return n_pass, n_warn, n_fail


def check_db(path: str, verbose: bool, scan_gaps: bool) -> tuple[int, int, int]:
    """Check one database. Returns (n_pass, n_warn, n_fail)."""
    name = os.path.basename(path)
    try:
        con = sqlite3.connect(path)
        kind = _classify(con)
    except Exception as e:
        print(f"  ERROR  {name}: {e}")
        return 0, 0, 1

    if kind == DbKind.UNKNOWN:
        con.close()
        print(f"  ─  {name}  (skipped — unrecognised schema)")
        return 0, 0, 0

    print(f"\n  {name}")
    print(f"  {'─' * min(len(name), 60)}  type: {kind.value}")

    try:
        if kind == DbKind.OB_CEX:
            sections = _check_ob_cex(con, scan_gaps)
        elif kind == DbKind.CEX_SNAP:
            sections = _check_cex_snap(con, scan_gaps)
        elif kind == DbKind.POLYMARKET:
            sections = _check_polymarket(con)
        elif kind == DbKind.KLINES:
            sections = _check_klines(con, scan_gaps)
        elif kind == DbKind.DAILY:
            sections = _check_daily(con)
        else:
            sections = []
    except Exception as e:
        con.close()
        print(f"    ERROR during checks: {e}")
        return 0, 0, 1

    con.close()

    total_p = total_w = total_f = 0
    for sec_name, results in sections:
        if results:
            p, w, f = _print_section(sec_name, results, verbose)
            total_p += p
            total_w += w
            total_f += f

    return total_p, total_w, total_f


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Data quality checks for tradinebotte collector databases"
    )
    parser.add_argument("--db", type=str, default=None,
                        help="Glob pattern for .db file(s) to check (default: data/*.db)")
    parser.add_argument("--warn-only", action="store_true",
                        help="Exit 0 even if FAIL checks are found (for non-blocking CI use)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show PASS checks in addition to WARN/FAIL")
    parser.add_argument("--no-gaps", action="store_true",
                        help="Skip gap/continuity checks (much faster on large databases)")
    args = parser.parse_args()

    pattern = args.db or os.path.join(_DATA_DIR, "*.db")
    paths   = sorted(glob.glob(pattern))
    if not paths:
        print(f"No databases found matching: {pattern}", file=sys.stderr)
        sys.exit(1)

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    width = 66
    print("=" * width)
    print(f"  DATA QUALITY REPORT  —  {len(paths)} database(s)")
    print(f"  {now}")
    print("=" * width)

    grand_p = grand_w = grand_f = 0
    for path in paths:
        p, w, f = check_db(path, args.verbose, not args.no_gaps)
        grand_p += p
        grand_w += w
        grand_f += f

    print()
    print("=" * width)
    status_line = f"  SUMMARY  {len(paths)} DB(s) checked"
    if grand_f > 0:
        status_line += f"  —  {grand_f} FAIL  {grand_w} WARN  {grand_p} PASS"
    elif grand_w > 0:
        status_line += f"  —  all clean  {grand_w} WARN"
    else:
        status_line += "  —  all clean"
    print(status_line)
    print("=" * width)

    if grand_f > 0 and not args.warn_only:
        sys.exit(1)


if __name__ == "__main__":
    main()
