"""Data layer for the status page — pure loaders over the shared state DB.

Split out of generate_status.py (the render/orchestration keeps the rest). Every function is
parameterised on the DB path, so it holds no module state; generate_status re-exports these
names, so `generate_status.<fn>` (and the tests) keep working unchanged.
"""

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request


def _fetch_btc_24h(symbol: str = "BTCUSDT") -> dict:
    """Fetch 24h ticker stats from Binance public REST API. Returns {} on any error."""
    url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tradinebotte-status/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return {}


_TRADES_SQL = (
    "SELECT account, bot_name, ts_ms, side, reason, price, qty, quote, fee, order_id,"
    " maker, avg_entry_after, holdings_after, free_after FROM bot_trades ORDER BY ts_ms DESC"
)


def _qdb(path, queries):
    """Run each {key: sql} against a sqlite DB; return {key: [row dicts]}, or None if the
    DB is absent. A failing query yields [] for that key (a schema drift never crashes the
    whole page — the section that needs it just renders empty)."""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return None
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    result = {}
    for key, sql in queries.items():
        try:
            result[key] = [dict(row) for row in db.execute(sql).fetchall()]
        except Exception:
            result[key] = []
    db.close()
    return result


def _compute_pnl_windows(shared_path):
    """Windowed PnL (weekly/monthly) from heartbeat *history* in the shared DB — the only
    source that (a) covers every family uniformly (grid keeps no per-trade log) and (b)
    survives a per-bot DB reset (history lives in the shared DB). pnl_total is cumulative
    "since reset"; a window value is pnl_now - pnl_at(window_start), with a reset-to-zero
    discontinuity (|val|<0.01 after |prev|>1.0) rebasing the window."""
    shared_path = os.path.expanduser(shared_path)
    if not os.path.exists(shared_path):
        return {}
    now = int(time.time())
    wdb = sqlite3.connect(shared_path)
    wdb.row_factory = sqlite3.Row
    latest = {}
    for r in wdb.execute("SELECT account, bot_name, payload, max(ts) FROM heartbeats"
                         " GROUP BY account, bot_name"):
        try:
            p = json.loads(r["payload"]) if r["payload"] else {}
        except Exception:
            p = {}
        latest[(r["account"], r["bot_name"])] = p
    out = {}
    # Absolute window starts. daily = since UTC midnight ("today"); weekly/monthly = rolling
    # 7/30 days. All FOUR windows (incl. alltime) are now the SAME kind of metric — pnl_total
    # earned since the window start — so they compose consistently. (daily used to come from
    # the bot's self-reported daily_pnl, a different measure than the weekly/monthly deltas,
    # which made a positive daily look "impossibly" larger than a net-negative weekly.)
    windows = {"daily": now - (now % 86400), "weekly": now - 7 * 86400,
               "monthly": now - 30 * 86400}
    horizon = now - 30 * 86400
    for (acct, bot), p in latest.items():
        pt = p.get("pnl_total")
        rec = {"alltime": pt, "daily": None, "weekly": None, "monthly": None,
               "daily_reset": False, "weekly_reset": False, "monthly_reset": False}
        if isinstance(pt, (int, float)):
            series = [(row["ts"], row["p"]) for row in wdb.execute(
                "SELECT ts, json_extract(payload,'$.pnl_total') p FROM heartbeats"
                " WHERE account=? AND bot_name=? AND ts>=?"
                " AND json_extract(payload,'$.pnl_total') IS NOT NULL ORDER BY ts",
                (acct, bot, horizon))]
            for wname, wstart in windows.items():
                first_val = reset_val = None
                reset = False
                prev = None
                for ts, val in series:
                    # A reset wipes pnl_total to exactly 0 — that lands-on-zero signature
                    # distinguishes it from an ordinary drawdown (arbitrary value).
                    if prev is not None and abs(val) < 0.01 and abs(prev) > 1.0 and ts >= wstart:
                        reset_val = val
                        reset = True
                    if ts >= wstart and first_val is None:
                        first_val = val
                    prev = val
                base = reset_val if reset else first_val
                if base is not None:
                    rec[wname] = pt - base
                    rec[wname + "_reset"] = reset
        out[f"{acct}|{bot}"] = rec
    wdb.close()
    return out


_SERIES_MAX_POINTS = int(os.environ.get("STATUS_SERIES_POINTS", 120))


def _downsample(rows: list, max_pts: int) -> list:
    """Thin a time-ordered list of tuples (first element = ts) to ~max_pts by uniform time
    bucketing, keeping the LAST row of each bucket (pnl/equity are cumulative LEVELS, so the
    latest value in a bucket is the representative one)."""
    n = len(rows)
    if n <= max_pts or n == 0:
        return rows
    t0, t1 = rows[0][0], rows[-1][0]
    bucket = max((t1 - t0) / max_pts, 1e-9)
    out: list = []
    cur = None
    for r in rows:
        idx = int((r[0] - t0) / bucket)
        if idx != cur:
            out.append(r)
            cur = idx
        else:
            out[-1] = r
    return out


def _load_pnl_series(shared_path: str, keys: set | None = None) -> dict:
    """Per-bot time series {"account|bot": {"ts":[…],"pnl":[…],"asset":[…]}} from heartbeat
    history (last 30d), downsampled to ~_SERIES_MAX_POINTS points/bot to keep the embedded JSON
    light. asset = the payload's `equity` (accumulation, market value) if present, else `capital`
    (grid/swing/poly). `keys` (set of (account, bot_name)) filters; None keeps all."""
    shared_path = os.path.expanduser(shared_path)
    if not os.path.exists(shared_path):
        return {}
    db = sqlite3.connect(shared_path)
    horizon = int(time.time()) - 30 * 86400
    raw: dict = {}
    for acct, bot, ts, pnl, eq, cap in db.execute(
        "SELECT account, bot_name, ts,"
        " json_extract(payload,'$.pnl_total') AS pnl,"
        " json_extract(payload,'$.equity')   AS eq,"
        " json_extract(payload,'$.capital')  AS cap"
        " FROM heartbeats WHERE ts>=? AND json_extract(payload,'$.pnl_total') IS NOT NULL"
        " ORDER BY ts", (horizon,)):
        k = (acct, bot)
        if keys is not None and k not in keys:
            continue
        raw.setdefault(k, []).append((ts, pnl, eq if eq is not None else cap))
    db.close()
    out: dict = {}
    for (acct, bot), rows in raw.items():
        rows = _downsample(rows, _SERIES_MAX_POINTS)
        out[f"{acct}|{bot}"] = {
            "ts":    [t for t, _, _ in rows],
            "pnl":   [round(p, 2) if isinstance(p, (int, float)) else None for _, p, _ in rows],
            "asset": [round(a, 2) if isinstance(a, (int, float)) else None for _, _, a in rows],
        }
    return out


def _fleet_series(per_bot: dict) -> dict:
    """Aggregate per-bot series into one fleet series on a shared time grid. pnl/asset are
    cumulative LEVELS, so each bot is forward-filled (its last value carries across a gap) and
    summed — a bot that hadn't started yet contributes nothing before its first heartbeat."""
    if not per_bot:
        return {"ts": [], "pnl": [], "asset": []}
    grid = [t[0] for t in _downsample(
        [(t,) for t in sorted({t for s in per_bot.values() for t in s["ts"]})], _SERIES_MAX_POINTS)]
    if not grid:
        return {"ts": [], "pnl": [], "asset": []}
    sum_pnl = [0.0] * len(grid)
    sum_asset = [0.0] * len(grid)
    for s in per_bot.values():
        ts, pnl, asset = s["ts"], s["pnl"], s["asset"]
        j = 0
        last_p = last_a = None
        for gi, gt in enumerate(grid):
            while j < len(ts) and ts[j] <= gt:
                if pnl[j] is not None:
                    last_p = pnl[j]
                if asset[j] is not None:
                    last_a = asset[j]
                j += 1
            if last_p is not None:
                sum_pnl[gi] += last_p
            if last_a is not None:
                sum_asset[gi] += last_a
    return {"ts": grid,
            "pnl": [round(v, 2) for v in sum_pnl],
            "asset": [round(v, 2) for v in sum_asset]}


def _load_trades(shared_path: str, keys: set | None = None) -> dict:
    """Per-fill trade log from the shared DB, grouped by (account, bot_name), newest first.
    `keys` (a set of (account, bot_name)) filters to specific bots; None keeps all."""
    blob = _qdb(shared_path, {"rows": _TRADES_SQL}) or {}
    out: dict = {}
    for r in blob.get("rows", []):
        k = (r.get("account"), r.get("bot_name"))
        if keys is not None and k not in keys:
            continue
        out.setdefault(k, []).append(r)
    return out
