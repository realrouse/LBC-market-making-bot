#!/usr/bin/env python3
"""
POLYMARKET LIVE BOT v3 — BTC Up/Down 5-min
Toutes corrections appliquées et validées en production le 23/04/2026.

STRATÉGIE :
- Surveille les marchés "Bitcoin Up or Down — 5 minutes" actifs (fenêtre ±6min)
- Signal : best_bid >= 0.96 sur un token UP ou DOWN
- Entrée : ordre LIMIT BUY au best_ask via py_clob_client (EIP-712 + HMAC)
- Résolution : bid >= 0.99 = WIN | bid <= 0.01 = LOSS

PRÉREQUIS WALLET :
- Wallet EOA Polygon avec USDC.e (0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174)
- MATIC pour les gas fees (~1 MATIC suffit)
- Allowance USDC.e accordée au contrat CTF Exchange Polymarket (0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E)
- API keys Polymarket dérivées via py_clob_client (voir setup.py)

LANCEMENT :
  export POLY_PRIVATE_KEY=0x...
  export POLY_API_KEY=...
  export POLY_API_SECRET=...
  export POLY_PASSPHRASE=...
  nohup python3 live_bot.py > /dev/null 2>&1 &
"""

import asyncio, hashlib, hmac, json, logging, os, sqlite3, time, uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
import aiohttp, websockets

# ─── PATHS ───────────────────────────────────────────────────────────────────
DB_PATH  = "/opt/polymarket-live/live.db"
LOG_PATH = "/opt/polymarket-live/live.log"

# ─── CAPITAL & FEES ──────────────────────────────────────────────────────────
CAPITAL_START = 100.0
STAKE         = 10.0
FEE_RATE      = 0.02
GAS_FEE_USD   = 0.03

# ─── API ENDPOINTS ───────────────────────────────────────────────────────────
POLY_GAMMA_URL     = "https://gamma-api.polymarket.com/markets"
POLY_GAMMA_HEADERS = {"User-Agent": "Mozilla/5.0"}
POLY_GAMMA_PARAMS  = {"closed": "false", "limit": 100}
POLY_CLOB_URL      = "https://clob.polymarket.com"
POLY_WS_URL        = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# ─── MARKET FILTER ───────────────────────────────────────────────────────────
BTC_5M_KEYWORDS = ("bitcoin up or down", "btc up or down")

# ─── SIGNAL PARAMETERS (NE PAS MODIFIER — BACKTESTÉ) ────────────────────────
SIGNAL_THRESHOLD   = 0.96
ENTRY_MAX          = 0.998
MIN_SECS_REMAINING = 45
MIN_ASK_VOL        = 10.0
WIN_THRESHOLD      = 0.99
LOSS_THRESHOLD     = 0.01
OBI_REJECT_THRESH  = -0.50
DAILY_STOP_LOSS    = 30.0

# ─── TIMING ──────────────────────────────────────────────────────────────────
SNAPSHOT_INTERVAL  = 5
DASHBOARD_INTERVAL = 300
MARKET_REFRESH     = 90

# ─── CREDENTIALS (via env vars) ──────────────────────────────────────────────
PRIVATE_KEY    = os.environ.get("POLY_PRIVATE_KEY", "")
API_KEY        = os.environ.get("POLY_API_KEY", "")
API_SECRET     = os.environ.get("POLY_API_SECRET", "")
API_PASSPHRASE = os.environ.get("POLY_PASSPHRASE", "")

# ─── INIT ────────────────────────────────────────────────────────────────────
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH)]
)
logger = logging.getLogger("live")

# ─── SCHEMA ──────────────────────────────────────────────────────────────────
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL, token_id TEXT NOT NULL,
    direction TEXT NOT NULL, question TEXT,
    signal_ts_ms INTEGER, signal_seconds_elapsed REAL,
    signal_secs_remaining REAL, signal_best_bid REAL,
    signal_best_ask REAL, signal_spread REAL,
    signal_ask_vol REAL, signal_obi REAL,
    entry_ts_ms INTEGER, entry_price REAL,
    clob_order_id TEXT, stake REAL, tokens_bought REAL,
    fee REAL, cost_total REAL,
    resolved INTEGER DEFAULT 0,
    resolution_ts_ms INTEGER, resolution_bid REAL,
    outcome TEXT, pnl_gross REAL, pnl_net REAL,
    pnl_roi_pct REAL, capital_before REAL, capital_after REAL,
    created_at INTEGER DEFAULT (CAST(strftime('%s','now') AS INTEGER)*1000));
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER, market_id TEXT, token_id TEXT,
    direction TEXT, secs_remaining REAL,
    best_bid REAL, best_ask REAL, spread REAL,
    ask_vol REAL, obi REAL, has_open_trade INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id);
CREATE INDEX IF NOT EXISTS idx_trades_resolved ON trades(resolved);
"""

def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.executescript(SCHEMA)
    conn.commit()
    logger.info("DB initialisee : %s", DB_PATH)
    return conn

def get_market_end_ts_ms(market):
    for key in ("endDate", "end_time", "end_date_iso", "end_date"):
        val = market.get(key)
        if val:
            try:
                return datetime.fromisoformat(str(val).replace("Z", "+00:00")).timestamp() * 1000
            except:
                pass
    return 0.0

def get_market_start_ts_ms(market):
    for key in ("startDate", "start_time", "start_date_iso", "start_date"):
        val = market.get(key)
        if val:
            try:
                return datetime.fromisoformat(str(val).replace("Z", "+00:00")).timestamp() * 1000
            except:
                pass
    return 0.0

def get_up_token_id(market):
    clob = market.get("clobTokenIds")
    if clob:
        ids = json.loads(clob) if isinstance(clob, str) else clob
        if ids: return str(ids[0])
    tokens = market.get("tokens") or []
    for t in tokens:
        if isinstance(t, dict) and t.get("outcome", "").lower() in ("yes", "up"):
            return t.get("token_id")
    if tokens:
        t = tokens[0]
        return t.get("token_id") if isinstance(t, dict) else str(t)
    return None

def get_down_token_id(market):
    clob = market.get("clobTokenIds")
    if clob:
        ids = json.loads(clob) if isinstance(clob, str) else clob
        if len(ids) > 1: return str(ids[1])
    tokens = market.get("tokens") or []
    for t in tokens:
        if isinstance(t, dict) and t.get("outcome", "").lower() in ("no", "down"):
            return t.get("token_id")
    if len(tokens) > 1:
        t = tokens[1]
        return t.get("token_id") if isinstance(t, dict) else str(t)
    return None

def compute_fee(ep, tb):
    return FEE_RATE * min(ep, 1.0 - ep) * tb

class TokenState:
    __slots__ = ("token_id", "market_id", "direction", "question",
                 "market_end_ms", "market_start_ms",
                 "best_bid", "best_ask", "spread",
                 "bid_vol", "ask_vol", "obi",
                 "last_update_ts", "last_snapshot_ts")

    def __init__(self, token_id, market_id, direction, question,
                 market_start_ms, market_end_ms):
        self.token_id = token_id
        self.market_id = market_id
        self.direction = direction
        self.question = question
        self.market_start_ms = market_start_ms
        self.market_end_ms = market_end_ms
        self.best_bid = 0.5; self.best_ask = 0.5; self.spread = 0.0
        self.bid_vol = 0.0; self.ask_vol = 0.0; self.obi = 0.0
        self.last_update_ts = 0.0; self.last_snapshot_ts = 0.0

    @property
    def secs_remaining(self):
        if not self.market_end_ms: return 9999.0
        return max(0.0, (self.market_end_ms - time.time() * 1000) / 1000.0)

    @property
    def seconds_elapsed(self):
        if not self.market_start_ms: return 0.0
        return max(0.0, (time.time() * 1000 - self.market_start_ms) / 1000.0)

    @property
    def market_ended(self):
        return self.market_end_ms > 0 and time.time() * 1000 > self.market_end_ms + 5000

class BotState:
    def __init__(self, conn):
        self.conn = conn
        self.capital = CAPITAL_START
        self.session = None
        self.tokens = {}
        self.market_tokens = {}
        self.open_trades = {}
        self.traded_direction = {}
        self.signalled = set()
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0

    @property
    def win_rate(self):
        t = self.wins + self.losses
        return (self.wins / t * 100) if t else 0.0

async def place_limit_order(session, token_id, price, size_usdc):
    """
    Place un ordre LIMIT BUY via py_clob_client.
    Gère la signature EIP-712 + HMAC automatiquement.
    Nécessite POLY_PRIVATE_KEY définie en variable d'environnement.
    """
    if not PRIVATE_KEY:
        logger.warning("POLY_PRIVATE_KEY non definie — ordre simule")
        return f"sim_{uuid.uuid4().hex[:12]}"
    try:
        import sys as _sys
        _sys.path.insert(0, '/opt/polymarket-live/venv/lib/python3.12/site-packages')
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON
        from py_clob_client.clob_types import OrderArgs
        _client = ClobClient(POLY_CLOB_URL, key=PRIVATE_KEY, chain_id=POLYGON,
                            signature_type=0)
        _client.set_api_creds(_client.create_or_derive_api_creds())
        size_tokens = round(size_usdc / price, 4)
        _order = _client.create_order(OrderArgs(
            token_id=token_id, price=round(price, 4),
            size=size_tokens, side="BUY"))
        _resp = _client.post_order(_order)
        oid = str(_resp.get("orderID") or _resp.get("id") or "")
        if oid: return oid
        logger.warning("CLOB resp sans orderID: %s", _resp)
        return None
    except Exception as e:
        logger.error("Erreur CLOB : %s", e)
        return None

def parse_book_message(msg):
    """
    Parse les messages WebSocket Polymarket.
    Accepte book, price_change, last_trade_price.
    Format: {event_type, asset_id, bids: [{price, size}], asks: [{price, size}]}
    """
    event_type = msg.get("event_type") or msg.get("type", "")
    if event_type not in ("book", "price_change", "last_trade_price"):
        return None
    token_id = str(msg.get("asset_id") or "")
    if not token_id: return None

    def pl(lst):
        r = []
        for item in lst:
            try:
                if isinstance(item, dict):
                    p, s = float(item.get("price", 0)), float(item.get("size", 0))
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    p, s = float(item[0]), float(item[1])
                else:
                    continue
                if p > 0 and s > 0: r.append((p, s))
            except:
                continue
        return r

    bids = sorted(pl(msg.get("bids") or []), reverse=True)
    asks = sorted(pl(msg.get("asks") or []))
    if not bids and not asks: return None
    bb = bids[0][0] if bids else 0.0
    ba = asks[0][0] if asks else 1.0
    bv = sum(s for _, s in bids[:5])
    av = sum(s for _, s in asks[:5])
    tv = bv + av
    obi = (bv - av) / tv if tv > 0 else 0.0
    return {
        "token_id": token_id, "best_bid": bb, "best_ask": ba,
        "spread": max(0.0, ba - bb), "bid_vol": bv, "ask_vol": av, "obi": obi
    }

async def handle_book_update(state, parsed):
    ts = state.tokens.get(parsed["token_id"])
    if not ts: return
    ts.best_bid  = parsed["best_bid"]
    ts.best_ask  = parsed["best_ask"]
    ts.spread    = parsed["spread"]
    ts.bid_vol   = parsed["bid_vol"]
    ts.ask_vol   = parsed["ask_vol"]
    ts.obi       = parsed["obi"]
    ts.last_update_ts = time.time()
    await check_signal(state, ts)
    check_resolution(state, ts)
    now = time.time()
    if now - ts.last_snapshot_ts >= SNAPSHOT_INTERVAL:
        save_snapshot(state, ts)
        ts.last_snapshot_ts = now

async def check_signal(state, ts):
    if ts.market_id in state.signalled: return
    if ts.market_ended: return
    if ts.best_bid < SIGNAL_THRESHOLD: return
    if ts.best_bid > ENTRY_MAX: return
    if ts.best_ask >= 1.0: return          # FIX : exclut marchés déjà résolus
    if ts.best_ask > ENTRY_MAX: return     # FIX : double protection
    if ts.ask_vol > 0 and ts.ask_vol < MIN_ASK_VOL: return   # BUG #3 FIX
    if ts.secs_remaining < MIN_SECS_REMAINING: return
    if ts.obi < OBI_REJECT_THRESH: return
    if state.capital - len(state.open_trades) * STAKE < STAKE: return
    today_ms = int(
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp() * 1000
    )
    pnl = state.conn.execute(
        "SELECT COALESCE(SUM(pnl_net),0) FROM trades WHERE resolved=1 AND signal_ts_ms>=?",
        (today_ms,)
    ).fetchone()[0]
    if pnl < -DAILY_STOP_LOSS: return
    state.signalled.add(ts.market_id)
    await enter_live_trade(state, ts)

async def enter_live_trade(state, ts):
    now_ms = int(time.time() * 1000)
    ep = ts.best_ask
    tb = STAKE / ep if ep > 0 else 0
    fee = compute_fee(ep, tb)
    cost = STAKE + fee
    oid = None
    if state.session:
        oid = await place_limit_order(state.session, ts.token_id, ep, STAKE)
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
         now_ms, ep, oid, STAKE, tb, fee, cost, state.capital, 0)
    )
    state.conn.commit()
    tid = cur.lastrowid
    state.open_trades[ts.market_id] = tid
    state.traded_direction[ts.market_id] = ts.direction
    state.total_trades += 1
    logger.info(
        "TRADE #%d | %s %s | entry=%.4f | bid=%.4f | secs=%.0f | order=%s",
        tid, ts.direction, ts.market_id[:12], ep, ts.best_bid,
        ts.secs_remaining, oid or "sim"
    )

def check_resolution(state, ts):
    if ts.market_id not in state.open_trades: return
    if ts.direction != state.traded_direction[ts.market_id]: return
    outcome = None
    if ts.best_bid >= WIN_THRESHOLD: outcome = "WIN"
    elif ts.best_bid <= LOSS_THRESHOLD: outcome = "LOSS"
    elif ts.market_ended and ts.best_bid >= 0.50: outcome = "WIN"
    elif ts.market_ended: outcome = "LOSS"
    if outcome:
        close_trade(state, ts, state.open_trades[ts.market_id], outcome)

def close_trade(state, ts, trade_id, outcome):
    now_ms = int(time.time() * 1000)
    row = state.conn.execute(
        "SELECT stake, tokens_bought, fee FROM trades WHERE id=?", (trade_id,)
    ).fetchone()
    if not row: return
    stake, tb, fee = row
    won = (outcome == "WIN")
    pg = (tb - stake) if won else -stake
    pn = pg - fee - GAS_FEE_USD
    roi = (pn / stake * 100) if stake else 0.0
    ca = state.capital + pn
    state.conn.execute(
        "UPDATE trades SET resolved=1, resolution_ts_ms=?, resolution_bid=?, "
        "outcome=?, pnl_gross=?, pnl_net=?, pnl_roi_pct=?, capital_after=? WHERE id=?",
        (now_ms, ts.best_bid, outcome, pg, pn, roi, ca, trade_id)
    )
    state.conn.commit()
    state.capital = ca
    state.total_pnl += pn
    if won: state.wins += 1
    else: state.losses += 1
    del state.open_trades[ts.market_id]
    del state.traded_direction[ts.market_id]
    logger.info(
        "%s #%d | %s %s | pnl=$%+.2f (%.1f%%) | WR=%.1f%% | capital=$%.2f",
        "WIN" if won else "LOSS", trade_id, ts.direction, ts.market_id[:12],
        pn, roi, state.win_rate, state.capital
    )

def save_snapshot(state, ts):
    state.conn.execute(
        "INSERT INTO snapshots (ts_ms, market_id, token_id, direction, "
        "secs_remaining, best_bid, best_ask, spread, ask_vol, obi, has_open_trade) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (int(time.time() * 1000), ts.market_id, ts.token_id, ts.direction,
         ts.secs_remaining, ts.best_bid, ts.best_ask, ts.spread, ts.ask_vol, ts.obi,
         1 if ts.market_id in state.open_trades else 0)
    )
    state.conn.commit()

def print_dashboard(state):
    logger.info("=" * 65)
    logger.info("  LIVE BOT  %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("  Capital=$%.2f  PnL=$%+.2f  Trades=%d  WR=%.1f%%",
                state.capital, state.total_pnl, state.total_trades, state.win_rate)
    logger.info("  Tokens=%d  Marches=%d  Ouverts=%d",
                len(state.tokens), len(state.market_tokens), len(state.open_trades))
    logger.info("=" * 65)

async def fetch_markets(session):
    """
    Récupère uniquement les marchés BTC 5-min dont endDate est dans ±6 minutes.
    CRITIQUE : sans ce filtre, le bot souscrit à des centaines de marchés expirés.
    """
    results = []
    offset = 0
    limit = 100
    try:
        for _ in range(20):
            params = dict(POLY_GAMMA_PARAMS)
            now_utc = datetime.now(timezone.utc)
            params["end_date_min"] = (now_utc - timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
            params["end_date_max"] = (now_utc + timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
            params["offset"] = offset
            async with session.get(
                POLY_GAMMA_URL, headers=POLY_GAMMA_HEADERS, params=params,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200: break
                data = await resp.json(content_type=None)
            batch = data if isinstance(data, list) else data.get("data", data.get("markets", []))
            if not batch: break
            results.extend([
                m for m in batch
                if any(kw in m.get("question", "").lower() for kw in BTC_5M_KEYWORDS)
            ])
            if len(batch) < limit: break
            offset += limit
        logger.info("Marches BTC 5-min : %d", len(results))
        return results
    except Exception as e:
        logger.warning("Fetch erreur : %s", e)
        return []

def register_market(state, market):
    mid = (market.get("conditionId") or market.get("condition_id") or
           market.get("market_id") or market.get("id") or "")
    if not mid: return []
    up = get_up_token_id(market)
    dn = get_down_token_id(market)
    if not up or not dn: return []
    sm = get_market_start_ts_ms(market)
    em = get_market_end_ts_ms(market)
    if em and time.time() * 1000 > em: return []
    q = market.get("question", "")[:80]
    new = []
    for tid, d in ((up, "UP"), (dn, "DOWN")):
        if tid not in state.tokens:
            state.tokens[tid] = TokenState(tid, mid, d, q, sm, em)
            new.append(tid)
    state.market_tokens[mid] = {"UP": up, "DOWN": dn}
    return new

async def ws_loop(state, session):
    backoff = 1
    while True:
        try:
            await _run_ws(state, session)
            backoff = 1
        except Exception as e:
            logger.warning("WS erreur (%s) — reconnexion %ds", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

async def _run_ws(state, session):
    markets = await fetch_markets(session)
    if not markets:
        logger.warning("Aucun marche — attente 30s")
        await asyncio.sleep(30)
        return

    new_ids = []
    for m in markets:
        new_ids.extend(register_market(state, m))

    all_token_ids = list(new_ids)
    for tid, ts in state.tokens.items():
        if not ts.market_ended and tid not in all_token_ids:
            all_token_ids.append(tid)

    if not all_token_ids:
        logger.warning("Aucun token actif — attente 30s")
        await asyncio.sleep(30)
        return

    state.session = session
    logger.info("Souscription %d tokens...", len(all_token_ids))

    async with websockets.connect(POLY_WS_URL, ping_interval=20, ping_timeout=10) as ws:
        for i in range(0, len(all_token_ids), 50):
            await ws.send(json.dumps({
                "type": "subscribe", "channel": "market",
                "assets_ids": all_token_ids[i:i + 50]
            }))
        lr = ld = time.time()
        logger.info("WebSocket connecte")

        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=90)
            except asyncio.TimeoutError:
                logger.warning("WS timeout — reconnexion")
                break
          except:
                break

            now = time.time()
            if now - lr >= MARKET_REFRESH:
                lr = now
                nm = await fetch_markets(session)
                ni = []
                for m in nm:
                    ni.extend(register_market(state, m))
                if ni:
                    for i in range(0, len(ni), 50):
                        await ws.send(json.dumps({
                            "type": "subscribe", "channel": "market",
                            "assets_ids": ni[i:i + 50]
                        }))
                    logger.info("Nouveaux tokens : %d", len(ni))
                for tid in [t for t, ts in list(state.tokens.items())
                            if ts.market_ended and ts.market_id not in state.open_trades]:
                    ts = state.tokens.pop(tid, None)
                    if ts:
                        state.market_tokens.pop(ts.market_id, None)
                        state.signalled.discard(ts.market_id)
                if not [t for t in state.tokens.values() if not t.market_ended]:
                    logger.warning("Aucun token actif — reconnexion")
                    await asyncio.sleep(15)
                    break

            if now - ld >= DASHBOARD_INTERVAL:
                ld = now
                print_dashboard(state)

            try:
                msgs = json.loads(raw)
                if isinstance(msgs, dict): msgs = [msgs]
            except:
                continue

            for msg in msgs:
                p = parse_book_message(msg)
                if p: await handle_book_update(state, p)

def restore_state_from_db(state):
    rows = state.conn.execute(
        "SELECT market_id, id, direction FROM trades WHERE resolved=0"
    ).fetchall()
    for mid, tid, d in rows:
        state.open_trades[mid] = tid
        state.traded_direction[mid] = d
        state.signalled.add(mid)
    if rows: logger.info("Restaure %d trade(s)", len(rows))
    row = state.conn.execute(
        "SELECT COUNT(*), "
        "COALESCE(SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END),0), "
        "COALESCE(SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END),0), "
        "COALESCE(SUM(pnl_net),0) "
        "FROM trades WHERE resolved=1"
    ).fetchone()
    state.total_trades = (row[0] or 0) + len(rows)
    state.wins   = row[1] or 0
    state.losses = row[2] or 0
    state.total_pnl = row[3] or 0.0
    state.capital = CAPITAL_START + state.total_pnl
    logger.info("State : capital=$%.2f | %d trades | WR=%.1f%%",
                state.capital, row[0], state.win_rate)

async def main():
    logger.info("=" * 65)
    logger.info("  LIVE BOT v3 — Threshold=%.2f Stake=$%.0f MinAskVol=%.0f",
                SIGNAL_THRESHOLD, STAKE, MIN_ASK_VOL)
    if not PRIVATE_KEY:
        logger.warning("  POLY_PRIVATE_KEY non definie — ordres SIMULES")
    logger.info("=" * 65)
    conn = init_db()
    state = BotState(conn)
    restore_state_from_db(state)
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=10)
    ) as session:
        await ws_loop(state, session)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Arret.")

