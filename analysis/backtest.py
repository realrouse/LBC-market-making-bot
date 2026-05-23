#!/usr/bin/env python3
# pylint: disable=too-many-lines   # walk-forward section adds ~170 lines; threshold raised intentionally
"""
Backtest trading strategy parameters against historical snapshot data.

The snapshots table records price data every 5 seconds per tracked token.
This script replays those snapshots chronologically, applies the signal logic
with configurable parameters, and produces simulated trade statistics.

Database resolution when no --db / --all flag is given (first match wins):
    1. $TRADINEBOTTE_DIR/live.db  (or ~/tradinebotte/live.db by default)
    2. data/paper3.db             (paper-trading session — 764k snapshots)
    3. data/backtest_sample_btc5m_range_2026.db  (bundled sample dataset)

Usage:
    python3 analysis/backtest.py                              # default DB
    python3 analysis/backtest.py --db ~/tradinebotte/live.db  # explicit file
    python3 analysis/backtest.py --db data/session_a.db data/session_b.db
    python3 analysis/backtest.py --db data/*.db               # shell glob
    python3 analysis/backtest.py --all                        # all data/*.db + live.db
    python3 analysis/backtest.py --all --threshold 0.95 --detail
    python3 analysis/backtest.py --sweep                      # grid search
    python3 analysis/backtest.py --db data/paper3.db --compare
        → three-way table: backtest(user) | backtest(aligned to actual) | actual bot
"""

import argparse, math, os, sqlite3, sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import product
from typing import List, Optional, Tuple

INSTALL_DIR = os.path.expanduser(os.environ.get("TRADINEBOTTE_DIR", "~/tradinebotte"))
_live_db    = os.path.join(INSTALL_DIR, "live.db")
_data_dir   = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_sample_db  = os.path.join(_data_dir, "backtest_sample_btc5m_range_2026.db")
_paper3_db  = os.path.join(_data_dir, "paper3.db")

_LIVE_DB_MIN_SNAPSHOTS = 100  # below this, fall back to the sample dataset

def _live_db_usable(path: str) -> bool:
    """True if path exists and has enough snapshots to be worth backtesting."""
    if not os.path.exists(path):
        return False
    try:
        c = sqlite3.connect(path)
        count = c.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        c.close()
        return count >= _LIVE_DB_MIN_SNAPSHOTS
    except Exception:
        return False

def _collect_dbs(db_args: Optional[List[str]], scan_all: bool) -> List[str]:
    """
    Resolve the ordered list of database files to replay.

    Priority:
      --all              → all .db files in data/, then live.db if usable
      --db path [path…]  → exactly those paths (shell expands globs)
      (neither)          → live.db (if usable) + paper3.db (if present),
                           falling back to the bundled sample dataset
    """
    if scan_all:
        found = sorted(
            os.path.join(_data_dir, f)
            for f in os.listdir(_data_dir)
            if f.endswith(".db")
        ) if os.path.isdir(_data_dir) else []
        # Prepend live.db when it has enough data so it appears first.
        if _live_db_usable(_live_db) and _live_db not in found:
            found = [_live_db] + found
        return found
    if db_args:
        return list(db_args)
    # Default: live.db then paper3.db; fall back to sample when neither is available.
    defaults = []
    if _live_db_usable(_live_db):
        defaults.append(_live_db)
    if os.path.exists(_paper3_db):
        defaults.append(_paper3_db)
    return defaults if defaults else [_sample_db]

FEE_RATE    = 0.02    # Polymarket taker fee rate
GAS_FEE_USD = 0.03    # estimated gas cost per order


# ─── Parameter set ────────────────────────────────────────────────────────────

@dataclass
class Params:
    """All tunable parameters for one backtest run."""
    signal_threshold:   float = 0.95
    entry_max:          float = 0.998
    min_secs_remaining: float = 30.0
    min_ask_vol:        float = 10.0
    win_threshold:      float = 0.99
    loss_threshold:     float = 0.01
    obi_reject_thresh:  float = -0.25
    stake:              float = 10.0
    daily_stop_loss:    float = 30.0
    capital_start:      float = 100.0
    # Hour filter
    hour_filter:        bool              = False
    weekday_utc_ranges: list              = None   # type: ignore[assignment]
    weekend_utc_ranges: list              = None   # type: ignore[assignment]
    us_weekly_open:     bool              = True
    us_weekly_close:    bool              = True

    def __post_init__(self) -> None:
        if self.weekday_utc_ranges is None:
            self.weekday_utc_ranges = []
        if self.weekend_utc_ranges is None:
            self.weekend_utc_ranges = []


# ─── Simulated trade record ───────────────────────────────────────────────────

@dataclass
class SimTrade:
    market_id:            str
    direction:            str
    entry_ts_ms:          int
    entry_price:          float
    tokens:               float
    fee:                  float
    entry_bid:            float
    entry_secs_remaining: float
    # filled at resolution
    outcome:     Optional[str]   = None   # "WIN", "LOSS", or "OPEN"
    exit_bid:    Optional[float] = None
    exit_ts_ms:  Optional[int]   = None
    pnl_net:     Optional[float] = None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fee(price: float, tokens: float) -> float:
    return FEE_RATE * min(price, 1.0 - price) * tokens


def _date(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _daily_ratios(resolved: List["SimTrade"]) -> Tuple[float, float]:
    """Compute annualised daily Sharpe and Sortino from resolved SimTrades.

    Groups net PnL by resolution day (exit_ts_ms). Returns (sharpe, sortino).
    Returns (0.0, 0.0) when fewer than 2 trading days exist.
    Sortino uses all-day denominator (not just negative days), which is the
    standard definition: E[min(r,0)^2] under the zero-target convention.
    """
    daily: dict = {}
    for t in resolved:
        if t.exit_ts_ms is None:
            continue
        day = _date(t.exit_ts_ms)
        daily[day] = daily.get(day, 0.0) + (t.pnl_net or 0.0)

    vals = list(daily.values())
    if len(vals) < 2:
        return 0.0, 0.0

    n    = len(vals)
    mean = sum(vals) / n
    ann  = math.sqrt(365)

    variance = sum((v - mean) ** 2 for v in vals) / n
    std      = math.sqrt(variance)
    sharpe   = mean / std * ann if std > 0 else 0.0

    downside_sq = sum(min(v, 0.0) ** 2 for v in vals) / n
    dd_dev      = math.sqrt(downside_sq)
    if dd_dev > 0:
        sortino = mean / dd_dev * ann
    else:
        sortino = float("inf") if mean > 0 else 0.0

    return sharpe, sortino


# ─── Hour filter ──────────────────────────────────────────────────────────────

def _is_trading_hour(ts_ms: int, params: Params) -> bool:
    """Mirror of live_bot.is_trading_hour() for backtest replay."""
    if not params.hour_filter:
        return True
    dt     = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    dow    = dt.weekday()
    hour   = dt.hour
    minute = dt.minute

    if dow >= 5:
        if not params.weekend_utc_ranges:
            return False
        return any(s <= hour < e for s, e in params.weekend_utc_ranges)

    if dow == 0 and params.us_weekly_open:
        if hour < 13 or (hour == 13 and minute < 30):
            return False
    if dow == 4 and params.us_weekly_close:
        if hour >= 20:
            return False

    if not params.weekday_utc_ranges:
        return True
    return any(s <= hour < e for s, e in params.weekday_utc_ranges)


# ─── Core replay engine ───────────────────────────────────────────────────────

def run_backtest(rows: list, params: Params) -> Tuple[List[SimTrade], float]:
    """
    Replay snapshot rows chronologically and simulate trades.

    Each row is: (ts_ms, market_id, direction, secs_remaining,
                  best_bid, best_ask, ask_vol, obi)

    Returns (list_of_sim_trades, final_capital).
    """
    open_trades: dict  = {}   # market_id → SimTrade (one active trade per market)
    signalled:   set   = set()  # market_ids that have already been entered
    daily_pnl:   dict  = {}   # "YYYY-MM-DD" → cumulative net PnL that day
    all_trades:  list  = []
    capital:     float = params.capital_start

    for (ts_ms, market_id, direction,
         secs_remaining, best_bid, best_ask, ask_vol, obi) in rows:

        # ── Resolution check for any open trade on this market ────────────────
        if market_id in open_trades:
            trade = open_trades[market_id]

            # Only the token we entered can resolve the trade.
            if trade.direction == direction:
                outcome = None
                if best_bid >= params.win_threshold:
                    outcome = "WIN"
                elif best_bid <= params.loss_threshold:
                    outcome = "LOSS"
                elif secs_remaining <= 0:
                    outcome = "WIN" if best_bid >= 0.50 else "LOSS"

                if outcome:
                    won = (outcome == "WIN")
                    pg  = (trade.tokens - params.stake) if won else -params.stake
                    pn  = pg - trade.fee - GAS_FEE_USD
                    trade.outcome   = outcome
                    trade.exit_bid  = best_bid
                    trade.exit_ts_ms = ts_ms
                    trade.pnl_net   = pn
                    capital        += pn
                    daily_pnl[_date(ts_ms)] = daily_pnl.get(_date(ts_ms), 0.0) + pn
                    all_trades.append(trade)
                    del open_trades[market_id]

            # Don't fall through to the signal check — this market is taken.
            continue

        # ── Signal check ─────────────────────────────────────────────────────
        if market_id in signalled:                                   continue
        if secs_remaining <= 0:                                      continue
        if not _is_trading_hour(ts_ms, params):                      continue
        if best_bid < params.signal_threshold:                       continue
        if best_bid > params.entry_max:                              continue
        if best_ask >= 1.0:                                          continue
        if best_ask > params.entry_max:                              continue
        if 0 < ask_vol < params.min_ask_vol:                          continue
        if secs_remaining < params.min_secs_remaining:               continue
        if obi < params.obi_reject_thresh:                           continue
        if capital - len(open_trades) * params.stake < params.stake: continue
        if daily_pnl.get(_date(ts_ms), 0.0) < -params.daily_stop_loss: continue

        # Signal fires — open a simulated trade at current best_ask.
        ep     = best_ask
        tokens = params.stake / ep if ep > 0 else 0
        fee    = _fee(ep, tokens)
        open_trades[market_id] = SimTrade(
            market_id=market_id, direction=direction,
            entry_ts_ms=ts_ms, entry_price=ep, tokens=tokens, fee=fee,
            entry_bid=best_bid, entry_secs_remaining=secs_remaining,
        )
        signalled.add(market_id)

    # Trades still open when the snapshot data ends.
    for trade in open_trades.values():
        trade.outcome = "OPEN"
        all_trades.append(trade)

    return all_trades, capital


# ─── Statistics ───────────────────────────────────────────────────────────────

def summarize(trades: List[SimTrade], params: Params, capital_final: float) -> dict:
    """Compute aggregate statistics for a list of simulated trades."""
    resolved = [t for t in trades if t.outcome in ("WIN", "LOSS")]
    wins     = sum(1 for t in resolved if t.outcome == "WIN")
    losses   = len(resolved) - wins
    open_t   = sum(1 for t in trades if t.outcome == "OPEN")
    pnls     = [t.pnl_net for t in resolved]
    total_pnl = sum(pnls)
    win_rate  = (wins / len(resolved) * 100) if resolved else 0.0

    # Maximum drawdown: largest peak-to-trough drop in cumulative PnL.
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        cum += pnl
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    sharpe, sortino = _daily_ratios(resolved)

    return {
        "total":         len(resolved) + open_t,
        "wins":          wins,
        "losses":        losses,
        "open":          open_t,
        "win_rate":      win_rate,
        "total_pnl":     total_pnl,
        "max_drawdown":  max_dd,
        "sharpe":        sharpe,
        "sortino":       sortino,
        "capital_final": capital_final,
        "pnl_pct":       (total_pnl / params.capital_start * 100) if params.capital_start > 0 else 0.0,
    }


# ─── Actual-params detection ──────────────────────────────────────────────────

# Production call sites for _percentile — all use string literals, no external input.
# Allowlisted for reference; not enforced at runtime (offline script, private function).
_PERCENTILE_TABLES = frozenset({"trades", "snapshots"})
_PERCENTILE_COLS   = frozenset({
    "pnl", "stake", "signal_best_bid", "signal_secs_remaining",
    "entry_price", "exit_price", "duration_s",
})

def _percentile(conn: sqlite3.Connection, col: str, table: str,
                where: str, pct: float) -> Optional[float]:
    """
    Return the value at the given percentile (0–1) for a numeric column.
    Uses an ORDER BY + OFFSET approach compatible with SQLite.
    Private — only called from detect_actual_params() with hardcoded string
    literals. col/table are never derived from network or user input.
    """
    n_row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()  # nosec B608
    if not n_row or not n_row[0]:
        return None
    offset = max(0, int(pct * n_row[0]))
    row = conn.execute(
        f"SELECT {col} FROM {table} WHERE {where} ORDER BY {col} LIMIT 1 OFFSET ?",  # nosec B608
        (offset,),
    ).fetchone()
    return row[0] if row else None


def detect_actual_params(conn: sqlite3.Connection) -> Optional[dict]:
    """
    Infer the bot's actual runtime config from the trades table.

    Returns a dict with:
      stake         — most common stake value (modal)
      threshold     — 5th-percentile of signal_best_bid, rounded down to 0.01
                      (robust against outliers from old bot versions)
      min_secs      — 5th-percentile of signal_secs_remaining > 0, rounded down to 5s
      capital_start — minimum capital_before across all resolved trades
      stops         — count of STOP-outcome trades (daily stop-loss hits)
      ghosts        — count of GHOST-outcome trades (markets expired without result)
    Returns None when the trades table is empty or required columns are missing.
    """
    try:
        stake_row = conn.execute(
            "SELECT stake, COUNT(*) FROM trades WHERE resolved=1 "
            "GROUP BY stake ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not stake_row:
        return None

    try:
        bid_p5   = _percentile(conn, "signal_best_bid", "trades",
                               "resolved=1 AND signal_best_bid IS NOT NULL", 0.05)
        secs_p5  = _percentile(conn, "signal_secs_remaining", "trades",
                               "resolved=1 AND signal_secs_remaining > 0", 0.05)
        cap_row  = conn.execute(
            "SELECT capital_before FROM trades WHERE resolved=1 AND capital_before IS NOT NULL "
            "ORDER BY signal_ts_ms ASC LIMIT 1"
        ).fetchone()
        capital_start = cap_row[0] if cap_row and cap_row[0] else None
        # Estimate daily_stop_loss from the worst observed day: the bot must have
        # run all day through that loss, so the actual limit was at least that large.
        # We add 20 % headroom and round up to the nearest $50.
        worst_row = conn.execute("""
            SELECT MIN(day_pnl) FROM (
                SELECT SUM(pnl_net) AS day_pnl
                FROM trades
                WHERE resolved=1 AND pnl_net IS NOT NULL AND signal_ts_ms IS NOT NULL
                GROUP BY strftime('%Y-%m-%d', signal_ts_ms/1000, 'unixepoch')
            )
        """).fetchone()
        worst_day = worst_row[0] if worst_row and worst_row[0] else None
        if worst_day and worst_day < 0:
            raw_dsl = abs(worst_day) * 1.20
            daily_stop_loss = math.ceil(raw_dsl / 50) * 50
        else:
            daily_stop_loss = None
        # If estimated DSL exceeds 5× stake, it can't be trusted: when
        # stake > DSL a single loss already triggers the daily stop, so the
        # worst-day heuristic always overestimates.
        if daily_stop_loss and daily_stop_loss > 5 * stake_row[0]:
            daily_stop_loss = None
    except sqlite3.OperationalError:
        bid_p5, secs_p5, capital_start, daily_stop_loss = None, None, None, None

    stops_row = conn.execute(
        "SELECT "
        "  SUM(CASE WHEN outcome='STOP'  THEN 1 ELSE 0 END), "
        "  SUM(CASE WHEN outcome='GHOST' THEN 1 ELSE 0 END) "
        "FROM trades WHERE resolved=1"
    ).fetchone()

    # Round threshold down to nearest 0.01, min_secs down to nearest 5s.
    threshold = math.floor((bid_p5 or 0.95) * 100) / 100 if bid_p5 else None
    min_secs  = max(30.0, math.floor((secs_p5 or 30.0) / 5) * 5) if secs_p5 else None

    return {
        "stake":           stake_row[0],
        "threshold":       threshold,
        "min_secs":        min_secs,
        "capital_start":   capital_start,
        "daily_stop_loss": daily_stop_loss,
        "raw_min_bid":     bid_p5,
        "raw_min_secs":    secs_p5,
        "stops":           stops_row[0] or 0,
        "ghosts":          stops_row[1] or 0,
    }


def _actual_stats(conn: sqlite3.Connection) -> Optional[dict]:
    """Query aggregate statistics from the actual trades table."""
    try:
        row = conn.execute(
            "SELECT COUNT(*), "
            "  SUM(CASE WHEN outcome='WIN'  THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN outcome='STOP' THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN outcome='GHOST' THEN 1 ELSE 0 END), "
            "  COALESCE(SUM(pnl_net), 0) "
            "FROM trades WHERE resolved=1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    total, wins, losses, stops, ghosts, pnl = (
        row[0] or 0, row[1] or 0, row[2] or 0,
        row[3] or 0, row[4] or 0, row[5] or 0.0,
    )
    return {
        "total":    total,
        "wins":     wins,
        "losses":   losses,
        "stops":    stops,
        "ghosts":   ghosts,
        "win_rate": (wins / total * 100) if total else 0.0,
        "total_pnl": pnl,
    }


# ─── Output helpers ───────────────────────────────────────────────────────────

def _stat_block(label: str, params: Params, stats: dict, n_snapshots: int) -> str:
    lines = [
        "=" * 62,
        f"  {label}",
        f"  signal={params.signal_threshold}  min_secs={params.min_secs_remaining:.0f}"
        f"  min_ask={params.min_ask_vol:.0f}  obi={params.obi_reject_thresh}",
        f"  Snapshots: {n_snapshots:,}",
        "=" * 62,
        f"  Trades   : {stats['total']}",
        f"  Wins     : {stats['wins']}",
        f"  Losses   : {stats['losses']}",
    ]
    if stats["open"]:
        lines.append(f"  Open     : {stats['open']}  (unresolved at end of data)")
    pnl_pct = stats.get("pnl_pct", 0.0)
    sharpe  = stats.get("sharpe",  0.0)
    sortino = stats.get("sortino", 0.0)
    sortino_str = f"{sortino:+.2f}" if sortino != float("inf") else "  ∞"
    lines += [
        f"  Stake    : ${params.stake:.2f}  (capital start: ${params.capital_start:.2f})",
        f"  Win rate : {stats['win_rate']:.1f}%",
        f"  Total PnL: ${stats['total_pnl']:+.2f}  ({pnl_pct:+.1f}%)",
        f"  Max DD   : ${stats['max_drawdown']:.2f}",
        f"  Sharpe   : {sharpe:+.2f}  (annualised daily)",
        f"  Sortino  : {sortino_str}  (annualised daily, downside only)",
        f"  Capital  : ${stats['capital_final']:.2f}",
        "=" * 62,
    ]
    return "\n".join(lines)


def print_detail(trades: List[SimTrade]) -> None:
    """Print a table of individual simulated trades."""
    resolved = [t for t in trades if t.outcome in ("WIN", "LOSS")]
    if not resolved:
        print("  (no resolved trades)")
        return
    print(f"\n  {'#':>3}  {'Direction':<5}  {'Entry':>6}  {'Entry bid':>9}"
          f"  {'Secs':>4}  {'Outcome':<5}  {'Exit bid':>8}  {'PnL':>7}")
    print("  " + "-" * 68)
    for i, t in enumerate(resolved, 1):
        print(f"  {i:>3}  {t.direction:<5}  {t.entry_price:>6.4f}  {t.entry_bid:>9.4f}"
              f"  {t.entry_secs_remaining:>4.0f}  {t.outcome:<5}  {t.exit_bid:>8.4f}"
              f"  ${t.pnl_net:>+6.2f}")


def print_aggregate(results: List[dict]) -> None:
    """
    Print a cross-file summary after replaying multiple databases.

    Each file's capital starts at params.capital_start (independent sessions).
    Win rate is computed over all resolved trades across all files combined.
    Max drawdown shown is the worst single-session drawdown (not global).
    """
    n_files       = len(results)
    total_snaps   = sum(r["n_snapshots"] for r in results)
    total_wins    = sum(r["stats"]["wins"] for r in results)
    total_losses  = sum(r["stats"]["losses"] for r in results)
    total_open    = sum(r["stats"]["open"] for r in results)
    total_pnl     = sum(r["stats"]["total_pnl"] for r in results)
    resolved      = total_wins + total_losses
    win_rate      = (total_wins / resolved * 100) if resolved else 0.0
    worst_dd      = max((r["stats"]["max_drawdown"] for r in results), default=0.0)
    sharpe_vals   = [r["stats"]["sharpe"]  for r in results if r["stats"].get("sharpe")]
    sortino_vals  = [r["stats"]["sortino"] for r in results
                     if r["stats"].get("sortino") not in (None, float("inf"))]
    avg_sharpe    = sum(sharpe_vals)  / len(sharpe_vals)  if sharpe_vals  else 0.0
    avg_sortino   = sum(sortino_vals) / len(sortino_vals) if sortino_vals else 0.0

    print("=" * 62)
    print(f"  AGGREGATE — {n_files} file(s)  {total_snaps:,} snapshots")
    print("  (capital reset per file — independent sessions)")
    print("=" * 62)
    print(f"  Trades   : {resolved + total_open}")
    print(f"  Wins     : {total_wins}")
    print(f"  Losses   : {total_losses}")
    if total_open:
        print(f"  Open     : {total_open}  (unresolved at end of data)")
    print(f"  Win rate : {win_rate:.1f}%")
    print(f"  Total PnL: ${total_pnl:+.2f}")
    total_capital = sum(r.get("capital_start", 100.0) for r in results)
    if total_capital > 0:
        print(f"  PnL%     : {total_pnl / total_capital * 100:+.1f}%  (on total capital ${total_capital:.2f})")
    print(f"  Worst DD : ${worst_dd:.2f}  (worst single session)")
    if avg_sharpe:
        print(f"  Sharpe   : {avg_sharpe:+.2f}  (avg across files, annualised daily)")
    if avg_sortino:
        print(f"  Sortino  : {avg_sortino:+.2f}  (avg across files, annualised daily)")
    print("=" * 62)


def print_comparison(
    db_name:        str,
    user_params:    Params,
    user_stats:     dict,
    matched_params: Optional[Params],
    matched_stats:  Optional[dict],
    actual:         Optional[dict],
    detected:       Optional[dict],
) -> None:
    """
    Three-column comparison table:
      BACKTEST (user params) | BACKTEST (aligned to actual) | ACTUAL BOT RESULTS

    The matched column is omitted when no trades table is found or when the
    detected params are identical to the user params (no mismatch to show).
    """
    W = 16   # column width

    def _row(label: str, a: str, b: str, c: str) -> str:
        return f"  {label:<20} {a:>{W}} {b:>{W}} {c:>{W}}"

    show_matched = matched_params is not None and matched_stats is not None

    print()
    print("=" * (22 + W * 3 + 2))
    print(f"  COMPARISON — {db_name}")
    print("=" * (22 + W * 3 + 2))
    print(_row("", "BACKTEST", "BACKTEST", "BOT"))
    print(_row("", "(params)", "(aligned)", "ACTUAL"))
    print("  " + "-" * (20 + W * 3 + 2))
    # Config rows
    thr_b = f"{user_params.signal_threshold}"
    thr_m = f"{matched_params.signal_threshold}" if show_matched else "—"
    thr_a = f"≈{detected['raw_min_bid']:.4f}" if detected and detected.get("raw_min_bid") else "—"
    print(_row("Threshold", thr_b, thr_m, thr_a))

    secs_b = f"{user_params.min_secs_remaining:.0f}s"
    secs_m = f"{matched_params.min_secs_remaining:.0f}s" if show_matched else "—"
    secs_a = f"≈{detected['raw_min_secs']:.0f}s" if detected and detected.get("raw_min_secs") else "—"
    print(_row("Min secs", secs_b, secs_m, secs_a))

    stake_b = f"${user_params.stake:.0f}"
    stake_m = f"${matched_params.stake:.0f}" if show_matched else "—"
    stake_a = f"${detected['stake']:.0f} (modal)" if detected else "—"
    print(_row("Stake", stake_b, stake_m, stake_a))

    cap_b = f"${user_params.capital_start:.0f}"
    cap_m = f"${matched_params.capital_start:.0f}" if show_matched else "—"
    cap_a = f"${detected['capital_start']:.0f}" if detected and detected.get("capital_start") else "—"
    print(_row("Capital start", cap_b, cap_m, cap_a))

    dsl_b = f"${user_params.daily_stop_loss:.0f}"
    dsl_m = f"${matched_params.daily_stop_loss:.0f}" if show_matched else "—"
    dsl_a = f"≈${detected['daily_stop_loss']:.0f}" if detected and detected.get("daily_stop_loss") else "—"
    print(_row("Daily stop-loss", dsl_b, dsl_m, dsl_a))

    print("  " + "-" * (20 + W * 3 + 2))

    # Results rows
    def _fmt_int(v: Optional[int], fallback: str = "—") -> str:
        return f"{v:,}" if v is not None else fallback

    def _fmt_pct(v: Optional[float]) -> str:
        return f"{v:.1f}%" if v is not None else "—"

    def _fmt_pnl(v: Optional[float]) -> str:
        return f"${v:+.2f}" if v is not None else "—"

    def _fmt_dd(v: Optional[float]) -> str:
        return f"${v:.2f}" if v is not None else "—"

    a_total = actual["total"] if actual else None
    m_total = matched_stats["total"] if matched_stats else None
    print(_row("Trades", _fmt_int(user_stats["total"]), _fmt_int(m_total), _fmt_int(a_total)))

    a_wins = actual["wins"] if actual else None
    m_wins = matched_stats["wins"] if matched_stats else None
    print(_row("Wins", _fmt_int(user_stats["wins"]), _fmt_int(m_wins), _fmt_int(a_wins)))

    a_losses = actual["losses"] if actual else None
    m_losses = matched_stats["losses"] if matched_stats else None
    print(_row("Losses", _fmt_int(user_stats["losses"]), _fmt_int(m_losses), _fmt_int(a_losses)))

    if actual and (actual.get("stops") or actual.get("ghosts")):
        stops  = actual.get("stops",  0)
        ghosts = actual.get("ghosts", 0)
        if stops:
            print(_row("Stops (daily SL)", "—", "—", _fmt_int(stops)))
        if ghosts:
            print(_row("Ghosts (no exit)", "—", "—", _fmt_int(ghosts)))

    if user_stats.get("open"):
        m_open = _fmt_int(matched_stats["open"]) if matched_stats else "—"
        print(_row("Open (end of data)", _fmt_int(user_stats["open"]), m_open, "—"))

    print(_row("Win rate",
               _fmt_pct(user_stats["win_rate"]),
               _fmt_pct(matched_stats["win_rate"] if matched_stats else None),
               _fmt_pct(actual["win_rate"] if actual else None)))

    print(_row("Total PnL",
               _fmt_pnl(user_stats["total_pnl"]),
               _fmt_pnl(matched_stats["total_pnl"] if matched_stats else None),
               _fmt_pnl(actual["total_pnl"] if actual else None)))

    def _fmt_pnl_pct(pnl: Optional[float], capital: Optional[float]) -> str:
        if pnl is None or not capital:
            return "—"
        return f"{pnl / capital * 100:+.1f}%"

    cap_actual = detected["capital_start"] if detected and detected.get("capital_start") else None
    print(_row("PnL%",
               _fmt_pnl_pct(user_stats["total_pnl"], user_params.capital_start),
               _fmt_pnl_pct(matched_stats["total_pnl"] if matched_stats else None,
                             matched_params.capital_start if matched_params else None),
               _fmt_pnl_pct(actual["total_pnl"] if actual else None, cap_actual)))

    print(_row("Max DD",
               _fmt_dd(user_stats["max_drawdown"]),
               _fmt_dd(matched_stats["max_drawdown"] if matched_stats else None),
               "—"))

    def _fmt_ratio(v: Optional[float]) -> str:
        if v is None:
            return "—"
        return "∞" if v == float("inf") else f"{v:+.2f}"

    print(_row("Sharpe (ann.)",
               _fmt_ratio(user_stats.get("sharpe")),
               _fmt_ratio(matched_stats.get("sharpe") if matched_stats else None),
               "—"))
    print(_row("Sortino (ann.)",
               _fmt_ratio(user_stats.get("sortino")),
               _fmt_ratio(matched_stats.get("sortino") if matched_stats else None),
               "—"))

    print("=" * (22 + W * 3 + 2))

    # Config-mismatch warning
    if detected:
        mismatches = []
        if abs(detected["stake"] - user_params.stake) > 1:
            ratio = detected["stake"] / user_params.stake
            mismatches.append(
                f"stake: backtest=${user_params.stake:.0f}, "
                f"actual=${detected['stake']:.0f} (×{ratio:.0f})"
            )
        if detected["threshold"] and abs(detected["threshold"] - user_params.signal_threshold) >= 0.01:
            mismatches.append(
                f"threshold: backtest={user_params.signal_threshold}, "
                f"actual≈{detected['threshold']}"
            )
        if detected["min_secs"] and abs(detected["min_secs"] - user_params.min_secs_remaining) >= 10:
            mismatches.append(
                f"min_secs: backtest={user_params.min_secs_remaining:.0f}s, "
                f"actual≈{detected['min_secs']:.0f}s"
            )
        if mismatches:
            print()
            print("  ⚠  Divergent parameters:")
            for m in mismatches:
                print(f"     {m}")
            parts = [f"--stake {detected['stake']:.0f}"]
            if detected["threshold"]:
                parts.append(f"--threshold {detected['threshold']}")
            if detected["min_secs"]:
                parts.append(f"--min-secs {detected['min_secs']:.0f}")
            print("\n     For an exact comparison (aligned column):")
            print(f"     python3 analysis/backtest.py --db <file> --compare {' '.join(parts)}")
            print()


def _ratio(pnl: float, dd: float) -> float:
    """PnL / MaxDD risk-adjusted ratio (Calmar-style). Inf when DD=0 and PnL>0."""
    if dd <= 0:
        return float("inf") if pnl > 0 else 0.0
    return pnl / dd


def print_sweep_table(results: list, sort_by: str = "ratio", show_dsl: bool = False,
                      top_n: int = 0) -> None:
    """
    Print a comparison table of all parameter combinations.

    sort_by: 'wr'     → sort by win rate
             'pnl'    → sort by total PnL
             'ratio'  → sort by PnL/MaxDD (risk-adjusted, recommended)
             'sharpe' → sort by annualised Sharpe ratio
    show_dsl: include the daily_stop_loss column (set True when sweep varies it)
    top_n:    if > 0, show only the top-N unique configs (deduped on thr/secs/obi)
    """
    header = (
        f"{'threshold':>9} | {'min_secs':>8} | {'min_ask':>7} | {'obi':>6}"
        + (f" | {'dsl':>6}" if show_dsl else "")
        + f" | {'trades':>6} | {'wins':>5} | {'WR%':>6} | {'PnL':>9}"
        + f" | {'PnL%':>7} | {'MaxDD':>7} | {'Sharpe':>7} | {'PnL/DD':>7}"
    )
    sep = "-" * len(header)

    sort_keys = {
        "wr":     lambda x: -x[1]["win_rate"],
        "pnl":    lambda x: -x[1]["total_pnl"],
        "ratio":  lambda x: -(x[1].get("ratio") or _ratio(x[1]["total_pnl"], x[1]["max_drawdown"])),
        "sharpe": lambda x: -(x[1].get("sharpe") or 0.0),
    }
    key_fn = sort_keys.get(sort_by, sort_keys["ratio"])
    sorted_results = sorted(results, key=key_fn)

    if top_n > 0:
        seen_keys: set = set()
        rows_to_print = []
        for params, stats in sorted_results:
            key = (params.signal_threshold, params.min_secs_remaining, params.obi_reject_thresh)
            if key not in seen_keys:
                seen_keys.add(key)
                rows_to_print.append((params, stats))
                if len(rows_to_print) >= top_n:
                    break
        note = f"  (top {top_n} unique configs thr/secs/obi — {len(results)} combos total)"
    else:
        rows_to_print = sorted_results
        note = ""

    print(f"\n{header}\n{sep}{note}")
    for params, stats in rows_to_print:
        ratio = stats.get("ratio") or _ratio(stats["total_pnl"], stats["max_drawdown"])
        ratio_str  = f"{ratio:>7.2f}" if ratio != float("inf") else "    ∞"
        sharpe     = stats.get("sharpe", 0.0)
        sharpe_str = f"{sharpe:>+7.2f}"
        dsl_part   = f" {params.daily_stop_loss:>5.0f} |" if show_dsl else ""
        pnl_pct    = stats.get("pnl_pct", stats["total_pnl"] / 100.0)
        pnl_pct_str = f"{pnl_pct:>+6.1f}%"
        print(
            f"  {params.signal_threshold:>7.2f} | {params.min_secs_remaining:>8.0f} |"
            f" {params.min_ask_vol:>7.0f} | {params.obi_reject_thresh:>6.2f} |"
            f"{dsl_part}"
            f" {stats['total']:>6} | {stats['wins']:>5} |"
            f" {stats['win_rate']:>5.1f}% | ${stats['total_pnl']:>+8.2f} |"
            f" {pnl_pct_str} |"
            f" ${stats['max_drawdown']:>6.2f} | {sharpe_str} | {ratio_str}"
        )


def print_recommendations(results: list, n: int = 5) -> None:
    """Print top-N configs by three criteria: PnL/DD ratio, total PnL, win rate."""
    def _r(x):
        return x[1].get("ratio") or _ratio(x[1]["total_pnl"], x[1]["max_drawdown"])

    def _row(rank: int, params: Params, stats: dict) -> str:
        ratio = _r((params, stats))
        ratio_s = f"{ratio:.2f}" if ratio != float("inf") else "∞"
        pnl_pct = stats.get("pnl_pct", stats["total_pnl"] / 100.0)
        return (
            f"  {rank}. thr={params.signal_threshold}  secs={params.min_secs_remaining:.0f}  "
            f"ask={params.min_ask_vol:.0f}  obi={params.obi_reject_thresh}  "
            f"dsl={params.daily_stop_loss:.0f}"
            f"  → trades={stats['total']}  WR={stats['win_rate']:.1f}%"
            f"  PnL=${stats['total_pnl']:+.2f} ({pnl_pct:+.1f}%)"
            f"  DD=${stats['max_drawdown']:.2f}"
            f"  ratio={ratio_s}"
        )

    filtered = [(p, s) for p, s in results if s["total"] >= 50]  # ignore thin configs

    print("\n" + "=" * 80)
    print("  RECOMMENDATIONS — top configs by criterion (configs with ≥50 trades)")
    print("=" * 80)

    print("\n  ── By PnL/MaxDD ratio (risk-adjusted — recommended) ──")
    for i, (p, s) in enumerate(sorted(filtered, key=lambda x: -_r(x))[:n], 1):
        print(_row(i, p, s))

    print("\n  ── By total PnL ──")
    for i, (p, s) in enumerate(sorted(filtered, key=lambda x: -x[1]["total_pnl"])[:n], 1):
        print(_row(i, p, s))

    print("\n  ── By win rate ──")
    for i, (p, s) in enumerate(sorted(filtered, key=lambda x: -x[1]["win_rate"])[:n], 1):
        print(_row(i, p, s))

    # Single best recommendation
    if filtered:
        best_p, _ = sorted(filtered, key=lambda x: -_r(x))[0]
        print("\n  ★  BEST OVERALL CONFIG (PnL/DD ratio):")
        print(f"     python3 analysis/backtest.py --threshold {best_p.signal_threshold}"
              f" --min-secs {best_p.min_secs_remaining:.0f}"
              f" --min-ask {best_p.min_ask_vol:.0f}"
              f" --obi {best_p.obi_reject_thresh}"
              f" --stake {best_p.stake:.0f}")
    print("=" * 80)


# ─── Walk-forward optimization ────────────────────────────────────────────────

_WEEK_MS = 7 * 24 * 3600 * 1000   # one calendar week in milliseconds

# Sweep grid used inside each walk-forward fold (min_ask fixed at 10 to limit
# runtime; primary drivers are threshold, secs, obi).
_WF_THRESHOLDS = [0.94, 0.95, 0.96, 0.97, 0.98]
_WF_MIN_SECS   = [30,   45,   60]
_WF_MIN_ASK    = 10.0
_WF_OBI_VALS   = [-0.75, -0.50, -0.25]
_WF_DSL_VALS   = [30,   100,  500]
_WF_MIN_TEST_TRADES = 5   # skip folds with fewer resolved test-window trades


@dataclass
class WFold:
    """Results for one walk-forward fold."""
    fold_n:      int
    train_start: str       # "YYYY-MM-DD" — actual first day of train data
    train_end:   str       # actual last day of train data
    test_start:  str       # actual first day of test data
    test_end:    str       # actual last day of test data
    best_params: Params
    train_stats: dict
    test_stats:  dict

    @property
    def train_ev(self) -> float:
        n = self.train_stats["wins"] + self.train_stats["losses"]
        return self.train_stats["total_pnl"] / n if n else 0.0

    @property
    def test_ev(self) -> float:
        n = self.test_stats["wins"] + self.test_stats["losses"]
        return self.test_stats["total_pnl"] / n if n else 0.0

    @property
    def ev_degradation_pct(self) -> Optional[float]:
        """(test_ev − train_ev) / |train_ev| × 100. None when train_ev == 0."""
        if not self.train_ev:
            return None
        return (self.test_ev - self.train_ev) / abs(self.train_ev) * 100


def _split_weekly(rows: list) -> List[list]:
    """Partition rows into whole-week buckets (multiples of _WEEK_MS).
    Returns a list of per-week row lists in ascending order, empty weeks omitted.
    Called once so walk_forward avoids O(N²) full-list scans."""
    if not rows:
        return []
    t_min   = rows[0][0]
    w_start = (t_min // _WEEK_MS) * _WEEK_MS

    buckets: dict = {}
    for r in rows:
        key = (r[0] - w_start) // _WEEK_MS
        if key not in buckets:
            buckets[key] = []
        buckets[key].append(r)

    return [buckets[k] for k in sorted(buckets)]


def _fold_sweep(rows: list, stake: float) -> Tuple[Params, dict]:
    """Grid-search the walk-forward parameter space on train rows.
    Selects by PnL/MaxDD ratio; falls back to default Params when no trades fire."""
    best_p: Optional[Params] = None
    best_s: Optional[dict]   = None
    best_r  = -float("inf")

    for thr, secs, obi, dsl in product(
            _WF_THRESHOLDS, _WF_MIN_SECS, _WF_OBI_VALS, _WF_DSL_VALS):
        sp = Params(signal_threshold=thr, min_secs_remaining=secs,
                    min_ask_vol=_WF_MIN_ASK, obi_reject_thresh=obi,
                    daily_stop_loss=dsl, stake=stake)
        trades, cap = run_backtest(rows, sp)
        stats = summarize(trades, sp, cap)
        ratio = _ratio(stats["total_pnl"], stats["max_drawdown"])
        if ratio > best_r:
            best_r, best_p, best_s = ratio, sp, stats

    return best_p or Params(stake=stake), best_s or summarize([], Params(stake=stake), 0.0)


def walk_forward(rows: list, train_weeks: int = 4,
                 stake: float = 10.0) -> List[WFold]:
    """Walk-forward optimization: slide a (train_weeks + 1) window over weekly
    buckets, sweep train → apply best OOS on test. Skips folds with fewer than
    _WF_MIN_TEST_TRADES resolved test-window trades."""
    weeks = _split_weekly(rows)
    n_combos = (len(_WF_THRESHOLDS) * len(_WF_MIN_SECS)
                * len(_WF_OBI_VALS) * len(_WF_DSL_VALS))
    total_folds = max(0, len(weeks) - train_weeks)

    if total_folds == 0:
        return []

    folds: List[WFold] = []
    for slide in range(total_folds):
        train_weeks_data = weeks[slide: slide + train_weeks]
        test_week_data   = weeks[slide + train_weeks]

        train_rows = [r for w in train_weeks_data for r in w]
        test_rows  = test_week_data

        print(f"  Fold {slide + 1}/{total_folds}: sweeping {n_combos} combos"
              f"  (train={len(train_rows):,} snaps,"
              f" test={len(test_rows):,} snaps) …", flush=True)

        best_p, train_stats = _fold_sweep(train_rows, stake)

        t_trades, t_cap = run_backtest(test_rows, best_p)
        test_stats = summarize(t_trades, best_p, t_cap)

        if test_stats["wins"] + test_stats["losses"] < _WF_MIN_TEST_TRADES:
            print(f"         skipped — fewer than {_WF_MIN_TEST_TRADES} resolved test trades")
            continue

        folds.append(WFold(
            fold_n      = len(folds) + 1,
            train_start = _date(train_rows[0][0]),
            train_end   = _date(train_rows[-1][0]),
            test_start  = _date(test_rows[0][0]),
            test_end    = _date(test_rows[-1][0]),
            best_params = best_p,
            train_stats = train_stats,
            test_stats  = test_stats,
        ))

    return folds


def print_walk_forward(folds: List[WFold], db_name: str, train_weeks: int) -> None:
    """Print per-fold detail and aggregate summary for walk-forward results."""
    n_combos = (len(_WF_THRESHOLDS) * len(_WF_MIN_SECS)
                * len(_WF_OBI_VALS) * len(_WF_DSL_VALS))

    print()
    print("═" * 70)
    print(f"  WALK-FORWARD — {db_name}"
          f"  (train={train_weeks}w, test=1w, {n_combos} combos/fold)")
    print("═" * 70)

    if not folds:
        print(f"  Not enough data: need ≥{train_weeks + 1} weeks of snapshots"
              f" with ≥{_WF_MIN_TEST_TRADES} trades in the test week.")
        print("═" * 70)
        return

    for f in folds:
        bp  = f.best_params
        deg = f.ev_degradation_pct
        deg_str = f"{deg:>+.1f}%" if deg is not None else "—"
        tr_n = f.train_stats["wins"] + f.train_stats["losses"]
        te_n = f.test_stats["wins"]  + f.test_stats["losses"]
        print()
        print(f"  Fold {f.fold_n}:"
              f"  train {f.train_start}→{f.train_end}"
              f"  |  test {f.test_start}→{f.test_end}")
        print(f"    Best : thr={bp.signal_threshold}  secs={bp.min_secs_remaining:.0f}"
              f"  obi={bp.obi_reject_thresh}  dsl=${bp.daily_stop_loss:.0f}")
        print(f"    Train: trades={tr_n:>4}  WR={f.train_stats['win_rate']:.1f}%"
              f"  PnL=${f.train_stats['total_pnl']:>+8.2f}"
              f"  EV=${f.train_ev:>+.4f}  Sharpe={f.train_stats.get('sharpe', 0.0):>+.2f}")
        print(f"    Test : trades={te_n:>4}  WR={f.test_stats['win_rate']:.1f}%"
              f"  PnL=${f.test_stats['total_pnl']:>+8.2f}"
              f"  EV=${f.test_ev:>+.4f}  Sharpe={f.test_stats.get('sharpe', 0.0):>+.2f}")
        print(f"    EV degradation: {deg_str}")

    # ── Aggregate summary ─────────────────────────────────────────────────────
    valid_deg = [f.ev_degradation_pct for f in folds if f.ev_degradation_pct is not None]
    pos_folds = sum(1 for f in folds if f.test_ev > 0)
    avg_test_pnl = sum(f.test_stats["total_pnl"] for f in folds) / len(folds)
    avg_test_ev  = sum(f.test_ev for f in folds) / len(folds)
    avg_deg      = sum(valid_deg) / len(valid_deg) if valid_deg else None

    # Most frequently selected parameter signature across folds.
    def _sig(p: Params) -> str:
        return (f"thr={p.signal_threshold}  secs={p.min_secs_remaining:.0f}"
                f"  obi={p.obi_reject_thresh}  dsl=${p.daily_stop_loss:.0f}")

    freq = Counter(_sig(f.best_params) for f in folds)
    modal_sig, modal_n = freq.most_common(1)[0]

    print()
    print("─" * 70)
    print(f"  SUMMARY  ({len(folds)} fold(s) with ≥{_WF_MIN_TEST_TRADES} test trades)")
    print(f"    Avg test PnL / week  : ${avg_test_pnl:>+.2f}")
    print(f"    Avg test EV / trade  : ${avg_test_ev:>+.4f}")
    if avg_deg is not None:
        print(f"    Avg EV degradation   : {avg_deg:>+.1f}%")
    print(f"    Positive test folds  : {pos_folds}/{len(folds)}")
    print(f"    Most frequent best   : {modal_sig}  ({modal_n}/{len(folds)} folds)")
    print("═" * 70)


# ─── DB loading ───────────────────────────────────────────────────────────────

def load_rows(conn: sqlite3.Connection) -> list:
    """Load all snapshot rows needed for the backtest, ordered by time."""
    return conn.execute(
        "SELECT ts_ms, market_id, direction, secs_remaining, "
        "       best_bid, best_ask, ask_vol, obi "
        "FROM snapshots "
        "ORDER BY ts_ms ASC"
    ).fetchall()


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Backtest strategy parameters against snapshot databases.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--db", nargs="+", default=None, metavar="PATH",
                        help="one or more .db files to replay (the shell expands globs)")
    parser.add_argument("--all",        action="store_true",
                        help="replay all .db files in data/ and live.db if usable")
    parser.add_argument("--threshold",  type=float, default=0.95,
                        help="entry signal threshold (best_bid >= X)")
    parser.add_argument("--min-secs",   type=float, default=30.0,
                        help="minimum seconds remaining at entry")
    parser.add_argument("--min-ask",    type=float, default=10.0,
                        help="minimum ask-side volume in USD")
    parser.add_argument("--obi",        type=float, default=-0.25,
                        help="OBI reject threshold (below = no entry)")
    parser.add_argument("--stake",      type=float, default=10.0,
                        help="USD stake per trade")
    parser.add_argument("--detail",     action="store_true",
                        help="print individual simulated trade table")
    parser.add_argument("--compare",    action="store_true",
                        help="three-way comparison: backtest(user) | backtest(aligned) | actual bot")
    parser.add_argument("--sweep",      action="store_true",
                        help="grid search over multiple parameter combinations (per file)")
    parser.add_argument("--sweep-all",  action="store_true",
                        help="grid search aggregated across all DB files (more robust than per-file)")
    parser.add_argument("--sort",       default="ratio",
                        choices=["wr", "pnl", "ratio", "sharpe"],
                        help="sweep sort order: wr=win rate, pnl=total PnL, ratio=PnL/MaxDD, sharpe=Sharpe ratio")
    parser.add_argument("--top",          type=int, default=0, metavar="N",
                        help="show only the top-N unique configs in sweep (deduped on thr/secs/obi); 0=all")
    parser.add_argument("--walk-forward", nargs="?", const=4, type=int, metavar="WEEKS",
                        dest="walk_forward",
                        help="walk-forward: WEEKS-week train window + 1-week test (default: 4). "
                             "Sweeps %(default)s combos/fold; runtime ~1-3 min on large datasets.")
    args = parser.parse_args()

    db_paths = _collect_dbs(args.db, args.all or args.sweep_all)
    if not db_paths:
        print("No database files found. Use --db PATH, --all, or run the bot first.")
        sys.exit(1)

    multi = len(db_paths) > 1

    # Strategy parameters shared across all files.
    p = Params(
        signal_threshold=args.threshold,
        min_secs_remaining=args.min_secs,
        min_ask_vol=args.min_ask,
        obi_reject_thresh=args.obi,
        stake=args.stake,
    )

    file_results: List[dict] = []  # accumulate for aggregate summary

    # ── Collect rows for all DBs (needed by --sweep-all) ─────────────────────
    all_db_rows: List[Tuple[str, list]] = []  # (db_name, rows) pairs

    for db_path in db_paths:
        if not os.path.exists(db_path):
            print(f"WARNING: not found, skipping: {db_path}")
            continue

        if db_path == _sample_db:   tag = "(sample)"
        elif db_path == _live_db:   tag = "(live)"
        elif db_path == _paper3_db: tag = "(paper3)"
        else:                       tag = ""

        conn = sqlite3.connect(db_path)
        try:
            n_snapshots = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        except sqlite3.OperationalError:
            conn.close()
            print(f"  Skipping {os.path.basename(db_path)} (no snapshots table)")
            continue

        if n_snapshots == 0:
            conn.close()
            continue

        rows = load_rows(conn)
        all_db_rows.append((os.path.basename(db_path), rows))

        if args.sweep_all or args.walk_forward is not None:
            conn.close()
            continue   # rows collected; per-file output skipped for sweep-all/walk-forward

        print(f"DB: {db_path} {tag}".rstrip())

        if args.sweep:
            # Per-file grid search — original behaviour extended with daily_sl.
            thresholds = [0.94, 0.95, 0.96, 0.97, 0.98]
            min_secs   = [30,   45,   60]
            min_asks   = [5,    10,   20]
            obi_vals   = [-0.75, -0.50, -0.25]
            dsl_vals   = [30, 100, 500]
            combos     = (len(thresholds) * len(min_secs) * len(min_asks)
                          * len(obi_vals) * len(dsl_vals))
            print(f"Grid search: {len(thresholds)}×{len(min_secs)}×{len(min_asks)}"
                  f"×{len(obi_vals)}×{len(dsl_vals)} = {combos} combinations")
            print(f"Snapshots  : {n_snapshots:,}")

            sweep_results = []
            for thr, secs, ask, obi, dsl in product(
                    thresholds, min_secs, min_asks, obi_vals, dsl_vals):
                sp = Params(signal_threshold=thr, min_secs_remaining=secs,
                            min_ask_vol=ask, obi_reject_thresh=obi,
                            daily_stop_loss=dsl, stake=args.stake)
                trades, capital_final = run_backtest(rows, sp)
                stats = summarize(trades, sp, capital_final)
                sweep_results.append((sp, stats))
            print_sweep_table(sweep_results, sort_by=args.sort, show_dsl=True, top_n=args.top)
            print_recommendations(sweep_results)

        else:
            trades, capital_final = run_backtest(rows, p)
            stats = summarize(trades, p, capital_final)

            if args.compare:
                detected = detect_actual_params(conn)
                actual   = _actual_stats(conn)

                matched_params = None
                matched_stats  = None
                if detected and actual:
                    matched_params = Params(
                        signal_threshold   = detected["threshold"]       or p.signal_threshold,
                        min_secs_remaining = detected["min_secs"]        or p.min_secs_remaining,
                        min_ask_vol        = p.min_ask_vol,
                        obi_reject_thresh  = p.obi_reject_thresh,
                        stake              = detected["stake"],
                        capital_start      = detected["capital_start"]   or detected["stake"] * 10,
                        # When DSL cannot be reliably detected (stake > DSL),
                        # fall back to the user's own DSL so aligned vs user columns
                        # remain comparable on that dimension.
                        daily_stop_loss    = detected["daily_stop_loss"] or p.daily_stop_loss,
                    )
                    m_trades, m_capital = run_backtest(rows, matched_params)
                    matched_stats = summarize(m_trades, matched_params, m_capital)

                # When the bot ran with a different stake/capital than the strategy
                # defaults, re-run the user-params backtest with the same stake and
                # capital_start so all three columns share the same economic base.
                # Signal parameters (threshold, min_secs, obi) are kept as-is.
                user_params_cmp = p
                user_stats_cmp  = stats
                if matched_params is not None and (
                    abs(matched_params.stake         - p.stake)         > 1 or
                    abs(matched_params.capital_start - p.capital_start) > 1
                ):
                    user_params_cmp = Params(
                        signal_threshold   = p.signal_threshold,
                        min_secs_remaining = p.min_secs_remaining,
                        min_ask_vol        = p.min_ask_vol,
                        obi_reject_thresh  = p.obi_reject_thresh,
                        stake              = matched_params.stake,
                        capital_start      = matched_params.capital_start,
                        daily_stop_loss    = p.daily_stop_loss,
                    )
                    u_trades, u_capital = run_backtest(rows, user_params_cmp)
                    user_stats_cmp = summarize(u_trades, user_params_cmp, u_capital)

                label = f"BACKTEST — {os.path.basename(db_path)}" if multi else "BACKTEST"
                print(_stat_block(label, p, stats, n_snapshots))
                print_comparison(
                    db_name        = os.path.basename(db_path),
                    user_params    = user_params_cmp,
                    user_stats     = user_stats_cmp,
                    matched_params = matched_params,
                    matched_stats  = matched_stats,
                    actual         = actual,
                    detected       = detected,
                )
            else:
                label = f"BACKTEST — {os.path.basename(db_path)}" if multi else "BACKTEST"
                print(_stat_block(label, p, stats, n_snapshots))

            if args.detail:
                print_detail(trades)

            file_results.append({
                "label":         os.path.basename(db_path),
                "stats":         stats,
                "n_snapshots":   n_snapshots,
                "capital_start": p.capital_start,
            })

        conn.close()

    # ── --sweep-all: aggregate grid search across all DB files ────────────────
    if args.sweep_all and all_db_rows:
        total_snaps = sum(len(r) for _, r in all_db_rows)
        thresholds  = [0.94, 0.95, 0.96, 0.97, 0.98]
        min_secs    = [30,   45,   60]
        min_asks    = [5,    10,   20]
        obi_vals    = [-0.75, -0.50, -0.25]
        dsl_vals    = [30, 100, 500]
        combos      = (len(thresholds) * len(min_secs) * len(min_asks)
                       * len(obi_vals) * len(dsl_vals))
        db_names    = ", ".join(n for n, _ in all_db_rows)
        print(f"SWEEP-ALL — {len(all_db_rows)} DB(s): {db_names}")
        print(f"Grid search: {len(thresholds)}×{len(min_secs)}×{len(min_asks)}"
              f"×{len(obi_vals)}×{len(dsl_vals)} = {combos} combinations")
        print(f"Snapshots  : {total_snaps:,} (aggregated — capital reset per file)")

        sweep_results = []
        for thr, secs, ask, obi, dsl in product(
                thresholds, min_secs, min_asks, obi_vals, dsl_vals):
            sp = Params(signal_threshold=thr, min_secs_remaining=secs,
                        min_ask_vol=ask, obi_reject_thresh=obi,
                        daily_stop_loss=dsl, stake=args.stake)
            agg_wins = agg_losses = agg_open = 0
            agg_pnl  = 0.0
            worst_dd = 0.0
            for _, rows in all_db_rows:
                trades, cap = run_backtest(rows, sp)
                s = summarize(trades, sp, cap)
                agg_wins    += s["wins"]
                agg_losses  += s["losses"]
                agg_open    += s["open"]
                agg_pnl     += s["total_pnl"]
                worst_dd     = max(worst_dd, s["max_drawdown"])
            resolved = agg_wins + agg_losses
            wr = (agg_wins / resolved * 100) if resolved else 0.0
            agg_stats = {
                "total":        resolved + agg_open,
                "wins":         agg_wins,
                "losses":       agg_losses,
                "open":         agg_open,
                "win_rate":     wr,
                "total_pnl":    agg_pnl,
                "max_drawdown": worst_dd,
                "ratio":        _ratio(agg_pnl, worst_dd),
                "capital_final": 0.0,
            }
            sweep_results.append((sp, agg_stats))

        print_sweep_table(sweep_results, sort_by=args.sort, show_dsl=True, top_n=args.top)
        print_recommendations(sweep_results)
        return

    # ── --walk-forward: walk-forward optimization across all collected rows ────
    if args.walk_forward is not None and all_db_rows:
        combined = sorted(
            (r for _, rows in all_db_rows for r in rows),
            key=lambda x: x[0],
        )
        db_label = (all_db_rows[0][0] if len(all_db_rows) == 1
                    else f"{len(all_db_rows)} DB(s)")
        print(f"\nWALK-FORWARD — {db_label}"
              f"  train={args.walk_forward}w  stake=${args.stake:.0f}")
        folds = walk_forward(combined, train_weeks=args.walk_forward, stake=args.stake)
        print_walk_forward(folds, db_label, train_weeks=args.walk_forward)
        return

    # Print cross-file aggregate when more than one file was replayed.
    if multi and len(file_results) > 1 and not args.sweep:
        print()
        print_aggregate(file_results)


if __name__ == "__main__":
    main()
