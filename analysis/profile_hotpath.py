#!/usr/bin/env python3
"""
Performance profile of the bot's critical path.

Hot path (per WebSocket message):
  handle_book_update → check_signal → [check_resolution] → [save_snapshot]

Usage: python3 scripts/profile_hotpath.py
"""
import asyncio, cProfile, pstats, io, time, sys, os, timeit, shutil, glob

PROFILE_DIR = os.path.join(os.path.expanduser("~"), "tmp", "profile-bot")
os.makedirs(f"{PROFILE_DIR}/strategies", exist_ok=True)
os.environ["TRADINEBOTTE_DIR"] = PROFILE_DIR

for f in glob.glob("strategies/**/*.json", recursive=True):
    dest = os.path.join(PROFILE_DIR, "strategies", os.path.relpath(f, "strategies"))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy(f, dest)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bot.live_bot as bot

N       = 20_000   # iterations per benchmark
SEP     = "=" * 64

# ─── Fixtures ─────────────────────────────────────────────────────────────────
conn  = bot.init_db(bot.BotConfig())
state = bot.BotState(conn)

token_id  = "abc123token"
market_id = "mkt001"
state.tokens[token_id] = bot.TokenState(
    token_id, market_id, "UP", "BTC UP/DOWN test", 0,
    int(time.time() * 1000) + 120_000,   # expire dans 2 min
)
state.market_tokens[market_id] = {"UP": token_id, "DOWN": "def456"}

ts = state.tokens[token_id]
ts.best_bid = 0.50; ts.best_ask = 0.51
ts.bid_vol = 100.0; ts.ask_vol = 100.0; ts.obi = 0.0

# Parsed message as received by handle_book_update from account_bot / _run_ws
PARSED_HIGH = {
    "token_id": token_id,
    "best_bid": 0.96, "best_ask": 0.97, "spread": 0.01,
    "bid_vol": 500.0, "ask_vol": 200.0, "obi": 0.2,
}
PARSED_LOW = {
    "token_id": token_id,
    "best_bid": 0.50, "best_ask": 0.51, "spread": 0.01,
    "bid_vol": 100.0, "ask_vol": 100.0, "obi": 0.0,
}


# ─── 1. timeit — fonctions synchrones ─────────────────────────────────────────
print(SEP)
print(f"  TIMEIT — synchronous functions ({N:,} iterations)")
print(SEP)

sync_results = []

t = timeit.timeit(lambda: bot.is_trading_hour(bot.BotConfig()), number=N)
sync_results.append(("is_trading_hour()", t))

t = timeit.timeit(lambda: bot.check_resolution(state, ts), number=N)
sync_results.append(("check_resolution (pas de trade ouvert)", t))

t = timeit.timeit(lambda: bot.save_snapshot(state, ts), number=N)
sync_results.append(("save_snapshot (SQLite INSERT)", t))

print(f"\n  {'Fonction':<40} {'total ms':>9}  {'µs/appel':>9}")
print(f"  {'-'*40}  {'-'*9}  {'-'*9}")
for name, elapsed in sorted(sync_results, key=lambda x: -x[1]):
    print(f"  {name:<40} {elapsed*1000:>9.1f}  {elapsed/N*1e6:>9.2f}")


# ─── 2. timeit — chemin async complet ─────────────────────────────────────────
print(f"\n{SEP}")
print(f"  TIMEIT — full async path ({N:,} iterations)")
print(SEP)

async def bench_handle_low():
    for _ in range(N):
        await bot.handle_book_update(state, PARSED_LOW)

async def bench_handle_high():
    # high bid but market already in signalled set → no trade triggered
    state.signalled.add(market_id)
    for _ in range(N):
        await bot.handle_book_update(state, PARSED_HIGH)
    state.signalled.discard(market_id)

async def bench_check_signal_low():
    for _ in range(N):
        await bot.check_signal(state, ts)

t0 = time.perf_counter()
asyncio.run(bench_handle_low())
t_low = time.perf_counter() - t0

t0 = time.perf_counter()
asyncio.run(bench_handle_high())
t_high = time.perf_counter() - t0

ts.best_bid = 0.50
t0 = time.perf_counter()
asyncio.run(bench_check_signal_low())
t_sig = time.perf_counter() - t0
ts.best_bid = 0.50

async_results = [
    ("handle_book_update (bid=0.50, bas)", t_low),
    ("handle_book_update (bid=0.96, signalled)", t_high),
    ("check_signal (bid=0.50, sous seuil)", t_sig),
]

print(f"\n  {'Fonction':<44} {'total ms':>9}  {'µs/msg':>9}")
print(f"  {'-'*44}  {'-'*9}  {'-'*9}")
for name, elapsed in sorted(async_results, key=lambda x: -x[1]):
    print(f"  {name:<44} {elapsed*1000:>9.1f}  {elapsed/N*1e6:>9.2f}")


# ─── 3. cProfile — handle_book_update en masse ────────────────────────────────
print(f"\n{SEP}")
print(f"  cPROFILE — handle_book_update × {N:,} (bid=0.50, chemin nominal)")
print(SEP)

async def run_profiled():
    for _ in range(N):
        await bot.handle_book_update(state, PARSED_LOW)

pr = cProfile.Profile()
pr.enable()
asyncio.run(run_profiled())
pr.disable()
s = io.StringIO()
pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(15)
print(s.getvalue())


# ─── 4. cProfile — backtest run_backtest ──────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import backtest as bt  # pylint: disable=wrong-import-position,wrong-import-order

print(f"{SEP}")
print("  cPROFILE — run_backtest (basicsunday.db — 24 870 snapshots)")
print(SEP)

import sqlite3 as _sqlite3  # pylint: disable=wrong-import-position,wrong-import-order
rows = bt.load_rows(_sqlite3.connect("data/basicsunday.db"))
default_params = bt.Params()
pr2  = cProfile.Profile()
pr2.enable()
bt.run_backtest(rows, default_params)
pr2.disable()
s2 = io.StringIO()
pstats.Stats(pr2, stream=s2).sort_stats("tottime").print_stats(15)
print(s2.getvalue())


# ─── 5. cProfile — save_snapshot (SQLite writes) ──────────────────────────────
print(f"{SEP}")
print(f"  cPROFILE — save_snapshot × {N:,} (SQLite INSERT)")
print(SEP)

pr3 = cProfile.Profile()
pr3.enable()
for _ in range(N):
    bot.save_snapshot(state, ts)
pr3.disable()
s3 = io.StringIO()
pstats.Stats(pr3, stream=s3).sort_stats("tottime").print_stats(10)
print(s3.getvalue())
