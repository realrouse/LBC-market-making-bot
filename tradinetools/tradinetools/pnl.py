"""Shared realized-PnL / fee math — the single source of truth for round-trip PnL,
imported by BOTH the live strategy engines and the backtests so the two cannot drift.

History: the swing stop-loss path once computed `(exit-entry)*qty` and silently omitted
the round-trip trading fee, while the backtest deducted it — they disagreed and nobody
caught it. Centralising the assembly here (fees computed inside) makes dropping a fee
leg structurally impossible: callers pass the fee rate, not a hand-assembled formula.

All tradinebotte CEX connectors define `compute_fee(price, qty) = FEE_RATE*price*qty`
(notional-based), so `round_trip_pnl(entry, exit, qty, FEE_RATE)` is numerically
identical to the connectors' per-leg `compute_fee` while guaranteeing both legs are
always charged.
"""
from __future__ import annotations


def trade_fee(price: float, qty: float, fee_rate: float) -> float:
    """Trading fee for one leg = fee_rate × notional (price × qty)."""
    return fee_rate * price * qty


def round_trip_pnl(
    entry_price: float,
    exit_price: float,
    qty: float,
    fee_rate: float,
    *,
    entry_fee_rate: float | None = None,
    exit_fee_rate: float | None = None,
) -> float:
    """Realized PnL of a buy@entry_price → sell@exit_price round trip of `qty`, net of
    BOTH legs' trading fees.

    fee_rate applies to both legs unless `entry_fee_rate`/`exit_fee_rate` override it
    (e.g. maker on the resting leg, taker on the market exit). Fees are computed here,
    so a caller can never forget to subtract one.
    """
    ef = fee_rate if entry_fee_rate is None else entry_fee_rate
    xf = fee_rate if exit_fee_rate is None else exit_fee_rate
    return ((exit_price - entry_price) * qty
            - trade_fee(entry_price, qty, ef)
            - trade_fee(exit_price, qty, xf))
