#!/usr/bin/env python3
"""
Comparaison de performance sur trois configurations :
  CAS 1 — défaut         : snapshots activés, pas de mmap
  CAS 2 — mmap 256 MB   : snapshots activés, mmap SQLite
  CAS 3 — no-snapshots  : snapshots désactivés (pas d'I/O SQLite)

Usage : TRADINEBOTTE_DIR=/tmp/profile-bot python3 scripts/profile_compare.py
"""
import asyncio, time, timeit, sys, os, shutil, glob, sqlite3

PROFILE_DIR = "/tmp/profile-bot"
os.makedirs(f"{PROFILE_DIR}/strategies", exist_ok=True)
os.environ["TRADINEBOTTE_DIR"] = PROFILE_DIR

for f in glob.glob("strategies/*.json"):
    shutil.copy(f, f"{PROFILE_DIR}/strategies/")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bot.live_bot as bot

N   = 20_000
SEP = "=" * 72

MMAP_MB = 256

# ─── Helpers ──────────────────────────────────────────────────────────────────

def make_state(conn: sqlite3.Connection) -> tuple:
    """Return (BotState, TokenState) with a single active token."""
    state = bot.BotState(conn)
    token_id  = "abc123token"
    market_id = "mkt001"
    state.tokens[token_id] = bot.TokenState(
        token_id, market_id, "UP", "BTC UP/DOWN test", 0,
        int(time.time() * 1000) + 120_000,
    )
    state.market_tokens[market_id] = {"UP": token_id, "DOWN": "def456"}
    ts = state.tokens[token_id]
    ts.best_bid = 0.50; ts.best_ask = 0.51
    ts.bid_vol = 100.0; ts.ask_vol = 100.0; ts.obi = 0.0
    return state, ts

PARSED_LOW = {
    "token_id": "abc123token",
    "best_bid": 0.50, "best_ask": 0.51, "spread": 0.01,
    "bid_vol": 100.0, "ask_vol": 100.0, "obi": 0.0,
}

def conn_default() -> sqlite3.Connection:
    c = sqlite3.connect(bot.DB_PATH, check_same_thread=False)
    c.executescript(bot.SCHEMA)
    c.commit()
    return c

def conn_mmap() -> sqlite3.Connection:
    c = conn_default()
    c.execute(f"PRAGMA mmap_size = {MMAP_MB * 1024 * 1024};")
    c.commit()
    return c


# ─── Cas 1 : défaut ───────────────────────────────────────────────────────────
print(f"\n{SEP}")
print(f"  CAS 1 — DÉFAUT  (snapshots ON, mmap OFF)")
print(SEP)

bot.ENABLE_SNAPSHOTS = True
c1 = conn_default()
s1, ts1 = make_state(c1)

t_snap1 = timeit.timeit(lambda: bot.save_snapshot(s1, ts1), number=N)

async def _run_low1():
    for _ in range(N):
        await bot.handle_book_update(s1, PARSED_LOW)

t0 = time.perf_counter()
asyncio.run(_run_low1())
t_hbu1 = time.perf_counter() - t0

print(f"  save_snapshot × {N:,}        : {t_snap1*1000:8.1f} ms  |  {t_snap1/N*1e6:7.2f} µs/appel")
print(f"  handle_book_update × {N:,}   : {t_hbu1*1000:8.1f} ms  |  {t_hbu1/N*1e6:7.2f} µs/msg")
c1.close()


# ─── Cas 2 : mmap ─────────────────────────────────────────────────────────────
print(f"\n{SEP}")
print(f"  CAS 2 — MMAP {MMAP_MB} MB  (snapshots ON, mmap ON)")
print(SEP)

bot.ENABLE_SNAPSHOTS = True
c2 = conn_mmap()
s2, ts2 = make_state(c2)

t_snap2 = timeit.timeit(lambda: bot.save_snapshot(s2, ts2), number=N)

async def _run_low2():
    for _ in range(N):
        await bot.handle_book_update(s2, PARSED_LOW)

t0 = time.perf_counter()
asyncio.run(_run_low2())
t_hbu2 = time.perf_counter() - t0

print(f"  save_snapshot × {N:,}        : {t_snap2*1000:8.1f} ms  |  {t_snap2/N*1e6:7.2f} µs/appel")
print(f"  handle_book_update × {N:,}   : {t_hbu2*1000:8.1f} ms  |  {t_hbu2/N*1e6:7.2f} µs/msg")
c2.close()


# ─── Cas 3 : no-snapshots ─────────────────────────────────────────────────────
print(f"\n{SEP}")
print(f"  CAS 3 — NO-SNAPSHOTS  (snapshots OFF, mmap OFF)")
print(SEP)

bot.ENABLE_SNAPSHOTS = False
c3 = conn_default()
s3, ts3 = make_state(c3)

# save_snapshot n'est pas appelé — on mesure handle_book_update sans I/O
async def _run_low3():
    for _ in range(N):
        await bot.handle_book_update(s3, PARSED_LOW)

t0 = time.perf_counter()
asyncio.run(_run_low3())
t_hbu3 = time.perf_counter() - t0

print(f"  save_snapshot × {N:,}        :      N/A       |       N/A")
print(f"  handle_book_update × {N:,}   : {t_hbu3*1000:8.1f} ms  |  {t_hbu3/N*1e6:7.2f} µs/msg")
c3.close()

bot.ENABLE_SNAPSHOTS = True  # restore


# ─── Tableau comparatif ───────────────────────────────────────────────────────
print(f"\n{SEP}")
print(f"  TABLEAU COMPARATIF ({N:,} itérations)")
print(SEP)

def delta(a: float, b: float) -> str:
    pct = (b - a) / a * 100
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct:.1f}%"

print(f"\n  {'Mesure':<38} {'Cas 1 défaut':>14} {'Cas 2 mmap':>14} {'Cas 3 no-snap':>14}")
print(f"  {'-'*38}  {'-'*14}  {'-'*14}  {'-'*14}")

print(f"  {'save_snapshot (µs/appel)':<38} {t_snap1/N*1e6:>13.2f}  {t_snap2/N*1e6:>13.2f}  {'—':>14}")
print(f"  {'save_snapshot (ms total)':<38} {t_snap1*1000:>13.1f}  {t_snap2*1000:>13.1f}  {'—':>14}")
print(f"  {'handle_book_update (µs/msg)':<38} {t_hbu1/N*1e6:>13.2f}  {t_hbu2/N*1e6:>13.2f}  {t_hbu3/N*1e6:>13.2f}")
print(f"  {'handle_book_update (ms total)':<38} {t_hbu1*1000:>13.1f}  {t_hbu2*1000:>13.1f}  {t_hbu3*1000:>13.1f}")

print(f"\n  Écarts (base = Cas 1 défaut) :")
print(f"    save_snapshot  : Cas2 vs Cas1 = {delta(t_snap1, t_snap2)}")
print(f"    handle_book_update : Cas2 vs Cas1 = {delta(t_hbu1, t_hbu2)}")
print(f"    handle_book_update : Cas3 vs Cas1 = {delta(t_hbu1, t_hbu3)}")
print()
