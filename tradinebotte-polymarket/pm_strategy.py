"""
Polymarket plugin — the threshold strategy + trade logic (Plan D step 4b).

Extracted from the universal entrypoint (live_bot.py): the single-entry threshold
signal, the order-entry / resolution / close flow, the stake sizing, and the
`ThresholdStrategy` that wires them behind the neutral `botcore.Strategy` protocol.

`state` is duck-typed (the entrypoint's BotState): these functions read/mutate
`state.config`, `state.connector`, `state.conn`, the open-trade/PnL bookkeeping, etc.,
but import no entrypoint type — so live_bot re-exports them and callers are unchanged.
Shipped flat beside live_bot.py. Logs to the entrypoint's "live" logger.
"""

import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from botcore import Strategy
from bot_utils import write_web_status
from pm_types import TokenState, RejectionStats
from pm_calendar import is_trading_hour, _in_weekend_session

logger = logging.getLogger("live")

# Stake-sizing tuning constants (compute_stake only).
_STAKE_SECS_MIN_FACTOR: float = 0.3
_STAKE_STEP_BREAKS: tuple = (45.0, 60.0, 90.0)


def compute_stake(cfg: "BotConfig", bid: float, secs: float,  # noqa: F821
                  capital: float = 0.0, win_rate: float = 0.5,
                  n_trades: int = 0, ask: float = 0.0,
                  fee_rate: float = 0.0) -> float:
    """
    Compute the effective stake for one trade.

    Priority order (first matching path wins):

    Kelly path (kelly_fraction > 0, n_trades ≥ kelly_min_trades):
      b_net   = (1/ask − 1) − FEE_RATE × min(ask, 1−ask) / ask
      f*      = (p × b_net − q) / b_net
      stake   = kelly_fraction × f* × capital, capped at stake_max.
      Returns 0.0 when f* ≤ 0 (no mathematical edge).

    Step path (stake_step_enabled=True):
      Zones: <45s → s0, 45-60s → s1, 60-90s → s2, ≥90s → s3.
      Returns the zone stake directly (no additional cap applied).

    Bid×secs path (bid_alpha or secs_alpha > 0):
      bid_score   = (bid − threshold) / (1 − threshold)
      bid_boost   = 1 + stake_bid_alpha × bid_score
      secs_factor = max(floor, 1 − secs_alpha × excess)
      stake       = clip(base × bid_boost × secs_factor, floor, eff_max)

    Flat path (all above disabled): returns cfg.stake.
    """
    if cfg.kelly_fraction > 0 and n_trades >= cfg.kelly_min_trades and capital > 0 and ask > 0:
        b_net = (1.0 / ask - 1.0) - fee_rate * min(ask, 1.0 - ask) / ask
        p = win_rate
        q = 1.0 - p
        f_star = (p * b_net - q) / b_net if b_net > 0 else 0.0
        if f_star <= 0:
            return 0.0
        kelly_stake = cfg.kelly_fraction * f_star * capital
        eff_max = (min(cfg.stake_max, cfg.stake_max_pct_capital * capital)
                   if cfg.stake_max_pct_capital > 0 else cfg.stake_max)
        return min(kelly_stake, eff_max)

    if cfg.stake_step_enabled:
        secs_b = cfg.stake_step_s3
        if secs < _STAKE_STEP_BREAKS[0]:
            secs_b = cfg.stake_step_s0
        elif secs < _STAKE_STEP_BREAKS[1]:
            secs_b = cfg.stake_step_s1
        elif secs < _STAKE_STEP_BREAKS[2]:
            secs_b = cfg.stake_step_s2
        return secs_b

    if cfg.stake_bid_alpha == 0.0 and cfg.stake_secs_alpha == 0.0:
        if cfg.stake_max_pct_capital > 0 and capital > 0:
            return min(cfg.stake, cfg.stake_max_pct_capital * capital)
        return cfg.stake

    bid_range  = 1.0 - cfg.signal_threshold
    bid_score  = (bid - cfg.signal_threshold) / bid_range if bid_range > 0 else 0.0
    bid_boost  = 1.0 + cfg.stake_bid_alpha * bid_score

    if secs <= cfg.stake_secs_ref:
        secs_factor = 1.0
    else:
        excess      = (secs - cfg.stake_secs_ref) / cfg.stake_secs_ref
        secs_factor = max(_STAKE_SECS_MIN_FACTOR, 1.0 - cfg.stake_secs_alpha * excess)

    eff_max = (min(cfg.stake_max, cfg.stake_max_pct_capital * capital)
               if cfg.stake_max_pct_capital > 0 and capital > 0
               else cfg.stake_max)
    floor = cfg.stake * _STAKE_SECS_MIN_FACTOR
    return min(eff_max, max(floor, cfg.stake * bid_boost * secs_factor))


async def check_signal(state: Any, ts: TokenState, _t_ws: Optional[float] = None) -> None:
    """
    Evaluate entry conditions (cheapest first) and enter a trade if all pass.

    Guards: signalled, market_ended, is_trading_hour, best_bid threshold,
    best_bid/ask caps, ask_vol, secs_remaining, obi, vol_filter, capital,
    daily_stop_loss.  _t_ws is passed through to enter_live_trade for latency
    tracking (None = no tracking).
    """
    cfg = state.config

    # Midnight UTC reset — deferred while trades are open so that a trade
    # entered just before midnight counts against the correct day's stop-loss.
    today_day = int(time.time() // 86400)
    if state._daily_pnl_day != today_day and not state.open_trades:
        state.daily_pnl = 0.0
        state._daily_pnl_day = today_day

    # Weekly reset — Monday UTC boundary; deferred while trades are open.
    _dt = datetime.now(timezone.utc)
    today_week = (_dt - timedelta(days=_dt.weekday())).toordinal()
    if state._weekly_pnl_week != today_week and not state.open_trades:
        state.weekly_pnl = 0.0
        state._weekly_pnl_week = today_week

    # Periodic rejection stats — every 60 s.
    _now_t = time.time()
    if _now_t - state._last_stats_log >= 60:
        r = state.rejection_stats
        logger.info(
            "[REJECTIONS] signalled=%d ended=%d hour=%d bid=%d emax=%d"
            " ask=%d askvol=%d secs=%d obi=%d vol=%d capital=%d dstop=%d wstop=%d cooldown=%d",
            r.signalled, r.market_ended, r.trading_hour, r.best_bid, r.entry_max,
            r.best_ask, r.ask_vol, r.secs_remaining, r.obi, r.vol_filter,
            r.capital, r.daily_stop, r.weekly_stop, r.api_cooldown,
        )
        state.rejection_stats = RejectionStats()
        state._last_stats_log = _now_t

    if ts.market_id in state.signalled: state.rejection_stats.signalled += 1; return
    if ts.market_ended: state.rejection_stats.market_ended += 1; return
    if not is_trading_hour(cfg): state.rejection_stats.trading_hour += 1; return
    if ts.best_bid < cfg.signal_threshold: state.rejection_stats.best_bid += 1; return
    if ts.best_bid > cfg.entry_max: state.rejection_stats.entry_max += 1; return
    if ts.best_ask >= 1.0: state.rejection_stats.best_ask += 1; return   # expired markets
    if ts.best_ask > cfg.entry_max: state.rejection_stats.entry_max += 1; return
    if ts.ask_vol < cfg.min_ask_vol: state.rejection_stats.ask_vol += 1; return
    if ts.secs_remaining < cfg.min_secs_remaining: state.rejection_stats.secs_remaining += 1; return
    if ts.obi < cfg.obi_reject_thresh: state.rejection_stats.obi += 1; return
    if cfg.vol_filter_enabled \
            and not (cfg.vol_filter_weekday_only and _in_weekend_session()) \
            and len(ts.bid_history) >= cfg.vol_min_samples:
        bids = list(ts.bid_history)
        obis = list(ts.obi_history)
        mean_b    = sum(bids) / len(bids)
        vol_bid   = math.sqrt(sum((b - mean_b) ** 2 for b in bids) / len(bids))
        range_bid = max(bids) - min(bids)
        mean_o    = sum(obis) / len(obis)
        obi_vol   = math.sqrt(sum((o - mean_o) ** 2 for o in obis) / len(obis))
        if vol_bid > cfg.vol_bid_max or range_bid > cfg.range_bid_max or obi_vol > cfg.obi_vol_max:
            logger.debug("[VOL_FILTER] bid_vol=%.3f range=%.3f obi_vol=%.3f — skip %s",
                         vol_bid, range_bid, obi_vol, ts.market_id[:12])
            state.rejection_stats.vol_filter += 1
            return
    _eff_stake = (min(cfg.stake_max, cfg.stake_max_pct_capital * state.capital)
                  if cfg.stake_max_pct_capital > 0 else cfg.stake_max)
    if state.capital - len(state.open_trades) * _eff_stake < _eff_stake:
        state.rejection_stats.capital += 1; return
    if state.daily_pnl < -cfg.daily_stop_loss: state.rejection_stats.daily_stop += 1; return
    if cfg.weekly_stop_loss > 0 and state.weekly_pnl < -cfg.weekly_stop_loss:
        state.rejection_stats.weekly_stop += 1; return
    if time.time() < state.api_cooldown_until: state.rejection_stats.api_cooldown += 1; return

    state.signalled.add(ts.market_id)
    await enter_live_trade(state, ts, _t_ws=_t_ws)


async def enter_live_trade(state: Any, ts: TokenState, _t_ws: Optional[float] = None) -> None:
    """
    Submit the CLOB order, record the trade in the DB, and update bot state.
    In live mode (session + private_key), returns early without inserting if
    post_order returns None, preventing ghost rows with order_id=NULL.
    In simulation mode (no private_key), the insert always runs with oid=None.

    _t_ws: monotonic timestamp from handle_book_update. When provided, latency
    metrics are computed and emitted as a [LATENCY] log line parseable by
    scripts/latency.py.
    """
    cfg = state.config
    now_ms = int(time.time() * 1000)
    ep    = ts.best_ask                                                          # entry price
    n_trades = state.wins + state.losses
    stake = compute_stake(
        cfg, ts.best_bid, ts.secs_remaining, state.capital,
        win_rate=state.win_rate / 100.0, n_trades=n_trades, ask=ep,
        fee_rate=state.connector.FEE_RATE,
    )
    if stake <= 0:
        logger.info("[KELLY] f*≤0 — no edge at ask=%.4f wr=%.1f%% — skipping %s",
                    ep, state.win_rate, ts.market_id[:12])
        return
    # Hard capital guard vs actual computed stake — check_signal uses cfg.stake
    # as a proxy, which may underestimate when stake_step or Kelly yields a
    # higher-than-base stake, causing capital to go negative on close.
    _committed = len(state.open_trades) * cfg.stake
    if stake > state.capital - _committed:
        logger.warning(
            "[CAPITAL] computed stake=$%.2f > available=$%.2f — skipping %s",
            stake, state.capital - _committed, ts.market_id[:12],
        )
        return
    tb    = stake / ep if ep > 0 else 0                         # tokens bought
    fee   = state.connector.compute_fee(ep, tb)
    cost  = stake + fee
    oid   = None

    # Latency measurement — point A: everything from WS message receipt up to
    # this point (token update, all signal guards, daily PnL query, fee calc).
    t_signal_ms = (time.monotonic() - _t_ws) * 1000 if _t_ws is not None else None

    # Latency measurement — point B: bracket only the CLOB API HTTP call so we
    # can isolate network RTT from in-process signal latency.
    t_pre_order = time.monotonic()
    if state.session:
        oid = await state.connector.post_order(
            state.session, ts.token_id, ep, stake,
            private_key=cfg.private_key, install_dir=cfg.install_dir,
        )
    order_rtt_ms = (time.monotonic() - t_pre_order) * 1000
    if state.session and cfg.private_key:
        if oid is None:
            state.api_fail_streak += 1
            if state.api_fail_streak >= cfg.api_fail_threshold:
                state.api_cooldown_until = time.time() + cfg.api_cooldown_secs
                logger.warning(
                    "[CIRCUIT_BREAKER] %d consecutive CLOB failures — entries suspended 5 min",
                    state.api_fail_streak,
                )
            logger.warning("[GHOST_GUARD] post_order returned None — aborting entry")
            return
        state.api_fail_streak = 0
    cur = state.conn.execute(
        "INSERT INTO trades ("
        "market_id, token_id, direction, question, "
        "signal_ts_ms, signal_seconds_elapsed, signal_secs_remaining, "
        "signal_best_bid, signal_best_ask, signal_spread, signal_ask_vol, signal_obi, "
        "entry_ts_ms, entry_price, clob_order_id, stake, tokens_bought, fee, cost_total, "
        "capital_before, resolved) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts.market_id, ts.token_id, ts.direction, ts.question,
         now_ms, ts.seconds_elapsed, ts.secs_remaining,
         ts.best_bid, ts.best_ask, ts.spread, ts.ask_vol, ts.obi,
         now_ms, ep, oid, stake, tb, fee, cost, state.capital, 0)
    )
    state.conn.commit()
    tid = cur.lastrowid or 0
    state.open_trades[ts.market_id] = tid
    state.traded_direction[ts.market_id] = ts.direction
    state.total_trades += 1
    logger.info(
        "▶ TRADE #%d | %s %s | entry=%.4f  bid=%.4f  secs=%.0fs  obi=%.3f  ask_vol=%.0f  stake=$%.2f | order=%s",
        tid, ts.direction, ts.market_id[:12], ep, ts.best_bid, ts.secs_remaining,
        ts.obi, ts.ask_vol, stake, oid or "sim"
    )
    if t_signal_ms is not None:
        # total_ms = signal latency + order RTT; these two intervals are
        # contiguous so their sum equals true end-to-end time from WS to order.
        total_ms = t_signal_ms + order_rtt_ms
        logger.info(
            "[LATENCY] signal_ms=%.2f order_rtt_ms=%.2f total_ms=%.2f"
            " ts_ms=%d direction=%s market=%s",
            t_signal_ms, order_rtt_ms, total_ms, now_ms, ts.direction, ts.market_id[:12],
        )


def check_resolution(state: Any, ts: TokenState) -> None:
    """
    Check if an open trade on this token has reached a WIN or LOSS threshold.
    We only resolve a trade for the direction we actually entered — if we
    bought UP and are now tracking DOWN for the same market, we skip it.
    """
    if ts.market_id not in state.open_trades: return
    if ts.direction != state.traded_direction[ts.market_id]: return
    cfg = state.config
    outcome = None
    if ts.best_bid >= cfg.win_threshold: outcome = "WIN"
    elif ts.best_bid <= cfg.loss_threshold: outcome = "LOSS"
    elif ts.market_ended and ts.best_bid >= 0.50: outcome = "WIN"
    elif ts.market_ended: outcome = "LOSS"
    if outcome:
        close_trade(state, ts, state.open_trades[ts.market_id], outcome)


def close_trade(state: Any, ts: TokenState, trade_id: int, outcome: str) -> None:
    """
    Mark a trade as resolved in the DB and update capital.

    PnL calculation:
      gross = tokens_bought - (stake + fee)  (WIN)  or  -(stake + fee)  (LOSS)
      net   = gross - estimated gas cost
    """
    now_ms = int(time.time() * 1000)
    row = state.conn.execute(
        "SELECT stake, tokens_bought, fee, signal_ts_ms FROM trades WHERE id=?", (trade_id,)
    ).fetchone()
    if not row: return
    stake, tb, fee, signal_ts_ms = row
    duration_s = int((now_ms - signal_ts_ms) / 1000) if signal_ts_ms else 0
    won = (outcome == "WIN")
    pg = (tb - stake - fee) if won else (-stake - fee)
    pn = pg - state.config.gas_fee_usd
    roi = (pn / stake * 100) if stake else 0.0
    ca = state.capital + pn
    state.conn.execute(
        "UPDATE trades SET resolved=1, resolution_ts_ms=?, resolution_bid=?, "
        "outcome=?, pnl_gross=?, pnl_net=?, pnl_roi_pct=?, capital_after=? WHERE id=?",
        (now_ms, ts.best_bid, outcome, pg, pn, roi, ca, trade_id)
    )
    state.conn.commit()
    state.capital = ca
    state.total_pnl  += pn
    state.daily_pnl  += pn
    state.weekly_pnl += pn
    if won: state.wins += 1
    else: state.losses += 1
    del state.open_trades[ts.market_id]
    del state.traded_direction[ts.market_id]
    _icon    = "✓" if won else "✗"
    _outcome = "WIN " if won else "LOSS"
    logger.info(
        "%s %s #%d | %s %s | pnl=$%+.2f (%.1f%%) | WR=%.1f%% | capital=$%.2f | duration=%ds",
        _icon, _outcome, trade_id, ts.direction, ts.market_id[:12],
        pn, roi, state.win_rate, state.capital, duration_s
    )
    write_web_status(state, state.config)


class ThresholdStrategy(Strategy):
    """Polymarket binary-market threshold strategy: single-entry signal + resolution.

    The default strategy (the entrypoint injects it; used when strategy_type is
    "threshold" or absent). Stateless — it delegates to the module-level check_signal /
    check_resolution, which read everything from `state`/`state.config`. This makes the
    threshold a peer of GridStrategy/SwingStrategy behind one dispatch, so
    handle_book_update no longer special-cases it (Plan D step 1).

    Explicitly conforms to the neutral-core `botcore.Strategy` protocol (Plan D step 3):
    the protocol lives in the core that every plugin may depend on, so subclassing it here
    introduces no plugin↔plugin cycle (the dependency is plugin → core).
    """

    STRATEGY_TYPE = "threshold"

    async def on_book_update(self, state: Any, ts: TokenState,
                             _t_ws: Optional[float] = None) -> None:
        await check_signal(state, ts, _t_ws=_t_ws)
        check_resolution(state, ts)
