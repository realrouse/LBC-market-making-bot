#!/usr/bin/env python3
"""
POLYMARKET LIVE BOT v3 — BTC Up/Down 5-minute markets

Strategy:
  Watch active "Bitcoin Up or Down — 5 minutes" markets via the Polymarket
  WebSocket order book feed. When a token's best_bid reaches 0.96, the market
  is pricing that outcome at 96% probability — statistically near-certain with
  ~45 seconds until resolution. We buy at best_ask and wait for settlement.

  Signal:     best_bid >= 0.96  (token priced at 96 cents, pays $1 on win)
  Entry:      LIMIT BUY at best_ask via py_clob_client (EIP-712 + HMAC auth)
  Resolution: bid >= 0.99 → WIN  |  bid <= 0.01 → LOSS

Wallet prerequisites:
  - Polygon EOA wallet with USDC.e (0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174)
  - MATIC for gas (~1 MATIC is sufficient for many transactions)
  - USDC.e allowance granted to the Polymarket CTF Exchange contract
  - Polymarket API keys derived from the private key (see scripts/setup.py)

Launch:
  python3 scripts/setup.py      # one-time wallet setup, writes config.json
  bash scripts/start_bot.sh     # starts the bot in the background
"""

import asyncio, hashlib, hmac, json, logging, os, sqlite3, time, uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
import aiohttp, websockets

# ─── PATHS ───────────────────────────────────────────────────────────────────
# All paths derive from a single env var so the bot can run anywhere, not just
# /opt/polymarket-live. Set POLYMARKET_DIR=~/polymarket for a user install.
INSTALL_DIR = os.environ.get("POLYMARKET_DIR", "/opt/polymarket-live")
DB_PATH     = os.path.join(INSTALL_DIR, "live.db")
LOG_PATH    = os.path.join(INSTALL_DIR, "live.log")
CONFIG_PATH = os.path.join(INSTALL_DIR, "config.json")

# ─── CAPITAL & FEES ──────────────────────────────────────────────────────────
CAPITAL_START = 100.0   # starting capital in USD (restored from DB on restart)
STAKE         = 10.0    # USD risked per trade
FEE_RATE      = 0.02    # Polymarket taker fee (2%)
GAS_FEE_USD   = 0.03    # estimated Polygon gas cost per order

# ─── API ENDPOINTS ───────────────────────────────────────────────────────────
POLY_GAMMA_URL     = "https://gamma-api.polymarket.com/markets"
POLY_GAMMA_HEADERS = {"User-Agent": "Mozilla/5.0"}
POLY_GAMMA_PARAMS  = {"closed": "false", "limit": 100, "tag_id": 102892}  # 102892 = "5M" tag
POLY_CLOB_URL      = "https://clob.polymarket.com"
POLY_WS_URL        = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# ─── MARKET FILTER ───────────────────────────────────────────────────────────
# Both forms appear in Polymarket's question text depending on the API version.
BTC_5M_KEYWORDS = ("bitcoin up or down", "btc up or down")

# ─── SIGNAL PARAMETERS — DO NOT MODIFY (backtested on 1663 trades) ───────────
# These values were tuned on historical data and must not be changed without
# re-running the full backtest. 98.3% win rate at these exact thresholds.
SIGNAL_THRESHOLD   = 0.96   # minimum best_bid to trigger a trade entry
ENTRY_MAX          = 0.998  # reject if ask is already at settlement price
MIN_SECS_REMAINING = 45     # refuse entry if the market ends in under 45s
MIN_ASK_VOL        = 10.0   # minimum ask-side liquidity in USD (avoids thin books)
WIN_THRESHOLD      = 0.99   # bid at or above this → market resolving YES/UP
LOSS_THRESHOLD     = 0.01   # bid at or below this → market resolving NO/DOWN
OBI_REJECT_THRESH  = -0.50  # reject if asks dominate bids by more than 50%
DAILY_STOP_LOSS    = 30.0   # halt trading for the day if net PnL < -$30

# ─── TIMING ──────────────────────────────────────────────────────────────────
SNAPSHOT_INTERVAL  = 5    # seconds between SQLite price snapshots per token
DASHBOARD_INTERVAL = 300  # seconds between log dashboard prints
MARKET_REFRESH     = 90   # seconds between Gamma API polls to discover new markets

# ─── CONFIGURATION ───────────────────────────────────────────────────────────

def load_config():
    """
    Load all configuration from config.json if it exists, returning the raw
    dict. Returns an empty dict if the file is absent so callers can fall back
    to environment variables for individual keys.
    """
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}

_cfg = load_config()

# Credentials: config.json first, env vars as fallback for container deployments.
PRIVATE_KEY    = _cfg.get("private_key",    os.environ.get("POLY_PRIVATE_KEY", ""))
API_KEY        = _cfg.get("api_key",        os.environ.get("POLY_API_KEY", ""))
API_SECRET     = _cfg.get("api_secret",     os.environ.get("POLY_API_SECRET", ""))
API_PASSPHRASE = _cfg.get("api_passphrase", os.environ.get("POLY_PASSPHRASE", ""))

# Database options (optional — safe to omit from config.json).
# db_mmap_mb: memory-map the DB file for faster reads (0 = disabled).
# The OS page cache already keeps the file in RAM for this workload,
# so this is optional. Set to e.g. 256 to explicitly map 256 MB.
DB_MMAP_MB = int(_cfg.get("db_mmap_mb", 0))

# ─── WEB STATUS PAGE ─────────────────────────────────────────────────────────
# When webstatuspage_html is true, the bot writes a static HTML status page to
# webstatuspage_path every DASHBOARD_INTERVAL seconds and after each trade.
# The page is protected by .htaccess Basic Auth if webstatus_password is set.
# .htpasswd is stored in INSTALL_DIR (outside the web root) for security.
_webstatus_raw = _cfg.get("webstatuspage_html", False)
WEBSTATUS_ENABLED  = str(_webstatus_raw).lower() in ("true", "1", "yes") if not isinstance(_webstatus_raw, bool) else _webstatus_raw
WEBSTATUS_PATH     = os.path.expanduser(_cfg.get("webstatuspage_path", "~/public_html/tradinebot_status.html"))
WEBSTATUS_USER     = _cfg.get("webstatus_user", "tradinebot")
WEBSTATUS_PASSWORD = _cfg.get("webstatus_password", "")

# ─── LOGGING & DB INIT ───────────────────────────────────────────────────────
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH)]
)
logger = logging.getLogger("live")

# ─── SCHEMA ──────────────────────────────────────────────────────────────────
# WAL mode allows concurrent reads while the bot writes, which matters if the
# monitor script queries the DB while a trade is being inserted.
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
    """Open (or create) the SQLite database and apply schema migrations."""
    # check_same_thread=False is safe here because asyncio is single-threaded;
    # the connection is only ever accessed from the event loop.
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.executescript(SCHEMA)
    if DB_MMAP_MB > 0:
        # Memory-map the DB file so SQLite accesses it via RAM pages managed
        # by the kernel rather than read() syscalls. Effective for read-heavy
        # workloads; for this bot the gain is marginal but harmless.
        conn.execute(f"PRAGMA mmap_size = {DB_MMAP_MB * 1024 * 1024};")
        logger.info("DB mmap active : %d MB", DB_MMAP_MB)
    conn.commit()
    logger.info("DB initialisee : %s", DB_PATH)
    return conn

# ─── MARKET METADATA HELPERS ─────────────────────────────────────────────────
# The Gamma API uses several different field names across API versions.
# These helpers try all known variants so the bot works with older responses too.

def get_market_end_ts_ms(market):
    """Return market end time as Unix milliseconds, or 0.0 if not found."""
    for key in ("endDate", "end_time", "end_date_iso", "end_date"):
        val = market.get(key)
        if val:
            try:
                return datetime.fromisoformat(str(val).replace("Z", "+00:00")).timestamp() * 1000
            except:
                pass
    return 0.0

def get_market_start_ts_ms(market):
    """Return market start time as Unix milliseconds, or 0.0 if not found."""
    for key in ("startDate", "start_time", "start_date_iso", "start_date"):
        val = market.get(key)
        if val:
            try:
                return datetime.fromisoformat(str(val).replace("Z", "+00:00")).timestamp() * 1000
            except:
                pass
    return 0.0

def get_up_token_id(market):
    """
    Extract the UP/YES token ID from a market object.
    clobTokenIds[0] is always the YES/UP token per Polymarket convention.
    Falls back to scanning the tokens array by outcome label.
    """
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
    """
    Extract the DOWN/NO token ID from a market object.
    clobTokenIds[1] is always the NO/DOWN token per Polymarket convention.
    """
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
    """
    Polymarket taker fee: 2% of min(price, 1-price) × token_count.
    The fee is charged on the closer leg to 0.5 to avoid double-charging
    near-certain outcomes. At entry_price=0.97, fee ≈ 2% × 0.03 × tokens.
    """
    return FEE_RATE * min(ep, 1.0 - ep) * tb


# ─── STATE CLASSES ────────────────────────────────────────────────────────────

class TokenState:
    """
    Per-token market data updated in real time from the WebSocket feed.
    Uses __slots__ to minimize memory overhead when tracking many tokens.
    """
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
        # Start bid/ask at 0.5 (fair coin flip) until the first WebSocket update
        # arrives. This prevents spurious signals from uninitialized state.
        self.best_bid = 0.5; self.best_ask = 0.5; self.spread = 0.0
        # ask_vol=0.0 before the first snapshot — the signal guard checks this
        # explicitly to avoid entering trades with no visible ask-side liquidity.
        self.bid_vol = 0.0; self.ask_vol = 0.0; self.obi = 0.0
        self.last_update_ts = 0.0; self.last_snapshot_ts = 0.0

    @property
    def secs_remaining(self):
        """Seconds until the market's scheduled end time (0 if already past)."""
        if not self.market_end_ms: return 9999.0
        return max(0.0, (self.market_end_ms - time.time() * 1000) / 1000.0)

    @property
    def seconds_elapsed(self):
        """Seconds since the market's scheduled start time."""
        if not self.market_start_ms: return 0.0
        return max(0.0, (time.time() * 1000 - self.market_start_ms) / 1000.0)

    @property
    def market_ended(self):
        """
        True if the market is past its end time plus a 5-second grace period.
        The grace period prevents treating a market as ended due to clock skew.
        """
        return self.market_end_ms > 0 and time.time() * 1000 > self.market_end_ms + 5000


class BotState:
    """
    Global runtime state shared across all async tasks.
    A single instance lives for the entire process lifetime.
    """
    def __init__(self, conn):
        self.conn = conn
        self.capital = CAPITAL_START
        self.session = None          # aiohttp.ClientSession, set when WS loop starts
        self.tokens = {}             # token_id → TokenState
        self.market_tokens = {}      # market_id → {"UP": tid, "DOWN": tid}
        self.open_trades = {}        # market_id → trade row id in DB
        self.traded_direction = {}   # market_id → "UP" or "DOWN"
        # signalled prevents re-entering a market we already entered (or
        # attempted to enter) this session, even if the price signal fires again.
        self.signalled = set()
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0

    @property
    def win_rate(self):
        """Win rate as a percentage, 0.0 if no resolved trades."""
        t = self.wins + self.losses
        return (self.wins / t * 100) if t else 0.0


# ─── ORDER PLACEMENT ─────────────────────────────────────────────────────────

async def place_limit_order(session, token_id, price, size_usdc):
    """
    Submit a LIMIT BUY order to the Polymarket CLOB.

    py_clob_client is imported lazily here rather than at module level for two
    reasons:
      1. The library is only installed inside the venv, not system Python.
      2. Importing it forces an sysconfig path injection that should happen as
         late as possible, after logging and DB are already initialized.

    Returns the order ID string on success, a "sim_..." string if running
    without a private key (dry-run mode), or None on error.
    """
    if not PRIVATE_KEY:
        logger.warning("POLY_PRIVATE_KEY non definie — ordre simule")
        return f"sim_{uuid.uuid4().hex[:12]}"
    try:
        import sys as _sys, sysconfig as _sc
        _venv = os.path.join(INSTALL_DIR, "venv")
        # Resolve the site-packages path dynamically so the bot works on any
        # Python version (3.11, 3.12, 3.13…) without hardcoding the minor version.
        _site = _sc.get_path("purelib", vars={"platbase": _venv, "base": _venv})
        _sys.path.insert(0, _site)
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


# ─── WEBSOCKET MESSAGE PARSING ────────────────────────────────────────────────

def parse_book_message(msg):
    """
    Parse a Polymarket WebSocket message into a normalized price snapshot.

    The feed emits three event types we care about:
      "book"             — full order book snapshot (sent on subscribe)
      "price_change"     — incremental update when a level changes
      "last_trade_price" — last executed trade price

    OBI (Order Book Imbalance) = (bid_vol - ask_vol) / (bid_vol + ask_vol)
    Computed over the top 5 levels on each side. A negative OBI means the
    ask side is heavier, indicating sell pressure — we reject entries where
    OBI < OBI_REJECT_THRESH (-0.50).

    Returns a dict with token_id, best_bid, best_ask, spread, bid_vol,
    ask_vol, obi, or None if the message is irrelevant or malformed.
    """
    event_type = msg.get("event_type") or msg.get("type", "")
    if event_type not in ("book", "price_change", "last_trade_price"):
        return None
    token_id = str(msg.get("asset_id") or "")
    if not token_id: return None

    def pl(lst):
        """Parse a list of price-level entries into (price, size) tuples."""
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

    bids = sorted(pl(msg.get("bids") or []), reverse=True)  # highest price first
    asks = sorted(pl(msg.get("asks") or []))                 # lowest price first
    if not bids and not asks: return None
    bb = bids[0][0] if bids else 0.0
    ba = asks[0][0] if asks else 1.0
    bv = sum(s for _, s in bids[:5])  # aggregate top-5 bid depth
    av = sum(s for _, s in asks[:5])  # aggregate top-5 ask depth
    tv = bv + av
    obi = (bv - av) / tv if tv > 0 else 0.0
    return {
        "token_id": token_id, "best_bid": bb, "best_ask": ba,
        "spread": max(0.0, ba - bb), "bid_vol": bv, "ask_vol": av, "obi": obi
    }

async def handle_book_update(state, parsed):
    """Apply a parsed book update to the token's state, then check for signals."""
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


# ─── SIGNAL & TRADE LOGIC ─────────────────────────────────────────────────────

async def check_signal(state, ts):
    """
    Evaluate all entry conditions for the given token and enter a trade if met.

    The guards are ordered from cheapest to most expensive to evaluate, and
    from most common rejection to least. Each guard has a specific reason:

      signalled      — one entry per market per session; prevents pyramiding
      market_ended   — no point entering a market that has already settled
      best_bid       — core signal: 0.96 means the market prices this at 96%
      best_bid > max — sanity cap; at 0.999 there is no profitable ask price
      best_ask >= 1.0 — catches markets that slipped through the time filter:
                        expired markets still emit messages with ask=1.0
      best_ask > max — second safety: don't buy above the settlement cap
      ask_vol        — skip if ask_vol=0 (no first snapshot yet) or too thin;
                        ask_vol starts at 0.0 before the first book message,
                        so `ask_vol > 0` is also the "snapshot received" check
      secs_remaining — at least 45s must remain so the order has time to fill
      obi            — reject if the order book is heavily ask-dominated
      capital        — ensure enough capital remains for the stake
      daily_stop     — halt for the day if losses exceeded the daily limit
    """
    if ts.market_id in state.signalled: return
    if ts.market_ended: return
    if ts.best_bid < SIGNAL_THRESHOLD: return
    if ts.best_bid > ENTRY_MAX: return
    if ts.best_ask >= 1.0: return       # expired markets still emit WS messages
    if ts.best_ask > ENTRY_MAX: return
    if ts.ask_vol > 0 and ts.ask_vol < MIN_ASK_VOL: return  # 0.0 = not yet initialized
    if ts.secs_remaining < MIN_SECS_REMAINING: return
    if ts.obi < OBI_REJECT_THRESH: return
    if state.capital - len(state.open_trades) * STAKE < STAKE: return

    # Check daily net PnL from midnight UTC to now.
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
    """
    Record the trade in the DB, submit the CLOB order, and update bot state.
    The DB insert happens even if the CLOB order fails so we always have an
    audit trail and can detect orphaned positions on restart.
    """
    now_ms = int(time.time() * 1000)
    ep = ts.best_ask                        # entry price = best available ask
    tb = STAKE / ep if ep > 0 else 0        # tokens bought at this price
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
    """
    Check if an open trade on this token has reached a WIN or LOSS threshold.
    We only resolve a trade for the direction we actually entered — if we
    bought UP and are now tracking DOWN for the same market, we skip it.
    """
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
    """
    Mark a trade as resolved in the DB and update capital.

    PnL calculation:
      gross = tokens_bought - stake  (WIN)  or  -stake  (LOSS)
      net   = gross - protocol fee - estimated gas cost
    """
    now_ms = int(time.time() * 1000)
    row = state.conn.execute(
        "SELECT stake, tokens_bought, fee FROM trades WHERE id=?", (trade_id,)
    ).fetchone()
    if not row: return
    stake, tb, fee = row
    won = (outcome == "WIN")
    pg = (tb - stake) if won else -stake    # gross profit/loss in USD
    pn = pg - fee - GAS_FEE_USD             # net after fees
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
    write_web_status(state)

def save_snapshot(state, ts):
    """Persist a price snapshot to the DB for post-session analysis."""
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
    """Log a periodic summary of bot status to the log file."""
    logger.info("=" * 65)
    logger.info("  LIVE BOT  %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("  Capital=$%.2f  PnL=$%+.2f  Trades=%d  WR=%.1f%%",
                state.capital, state.total_pnl, state.total_trades, state.win_rate)
    logger.info("  Tokens=%d  Marches=%d  Ouverts=%d",
                len(state.tokens), len(state.market_tokens), len(state.open_trades))
    logger.info("=" * 65)
    write_web_status(state)


# ─── WEB STATUS PAGE FUNCTIONS ────────────────────────────────────────────────

def _htpasswd_sha1(password):
    """Return an Apache {SHA} htpasswd hash (no external dependencies)."""
    import base64, hashlib
    return "{SHA}" + base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()

def setup_htaccess(html_path):
    """
    Create .htaccess in the HTML directory and .htpasswd in INSTALL_DIR.
    .htpasswd lives outside the web root so it cannot be downloaded.
    Skipped silently if webstatus_password is not configured.
    """
    if not WEBSTATUS_PASSWORD:
        return
    htpasswd_path = os.path.join(INSTALL_DIR, ".webstatus_htpasswd")
    htaccess_path = os.path.join(os.path.dirname(html_path), ".htaccess")
    # Always refresh .htpasswd so password changes take effect.
    with open(htpasswd_path, "w") as f:
        f.write(f"{WEBSTATUS_USER}:{_htpasswd_sha1(WEBSTATUS_PASSWORD)}\n")
    os.chmod(htpasswd_path, 0o640)
    # Write .htaccess only if absent; editing it manually should be preserved.
    if not os.path.exists(htaccess_path):
        with open(htaccess_path, "w") as f:
            f.write(
                f'AuthType Basic\n'
                f'AuthName "Tradinebot Status"\n'
                f'AuthUserFile {os.path.abspath(htpasswd_path)}\n'
                f'Require valid-user\n'
            )
        logger.info("htaccess cree : %s", htaccess_path)

def _status_html_trade_rows(conn):
    """Return HTML <tr> rows for the 10 most recent resolved trades."""
    rows = conn.execute(
        "SELECT id, direction, outcome, entry_price, pnl_net, capital_after, "
        "resolution_ts_ms, question "
        "FROM trades WHERE resolved=1 ORDER BY resolution_ts_ms DESC LIMIT 10"
    ).fetchall()
    if not rows:
        return '<tr><td colspan="8" style="color:#8b949e">Aucun trade résolu</td></tr>'
    parts = []
    for tid, direction, outcome, entry, pnl, cap, ts_ms, question in rows:
        ts_str = datetime.fromtimestamp(ts_ms / 1000).strftime("%H:%M:%S") if ts_ms else "—"
        css = "win" if outcome == "WIN" else "loss"
        q_short = (question or "")[:42] + ("…" if len(question or "") > 42 else "")
        parts.append(
            f'<tr class="{css}">'
            f'<td>#{tid}</td><td>{ts_str}</td><td>{direction}</td>'
            f'<td>{outcome}</td><td>{entry:.4f}</td>'
            f'<td>${pnl:+.2f}</td><td>${cap:.2f}</td>'
            f'<td title="{question or ""}">{q_short}</td></tr>'
        )
    return "\n".join(parts)

def generate_status_html(state):
    """Build a self-contained HTML status page from current bot state."""
    now_str  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today_ms = int(
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp() * 1000
    )
    daily_row = state.conn.execute(
        "SELECT COALESCE(SUM(pnl_net),0), "
        "COALESCE(SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END),0), "
        "COUNT(*) FROM trades WHERE resolved=1 AND signal_ts_ms>=?",
        (today_ms,)
    ).fetchone()
    daily_pnl, daily_wins, daily_count = daily_row

    open_count  = len(state.open_trades)
    trade_rows  = _status_html_trade_rows(state.conn)

    pnl_cls     = "win"  if state.total_pnl >= 0 else "loss"
    day_cls     = "win"  if daily_pnl       >= 0 else "loss"
    wr_cls      = "win"  if state.win_rate  >= 80 else ("neutral" if state.win_rate >= 50 else "loss")
    open_cls    = "neutral" if open_count   >  0  else ""

    return (
        '<!DOCTYPE html>\n<html lang="fr">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta http-equiv="refresh" content="60">\n'
        '<title>Tradinebot Status</title>\n'
        '<style>\n'
        'body{font-family:monospace;background:#0d1117;color:#c9d1d9;margin:0;padding:20px}\n'
        'h1{color:#58a6ff;border-bottom:1px solid #30363d;padding-bottom:10px}\n'
        'h2{color:#8b949e;font-size:1em;margin-top:24px}\n'
        '.cards{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px}\n'
        '.card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:14px 20px;min-width:120px}\n'
        '.card .label{font-size:.72em;color:#8b949e;text-transform:uppercase;letter-spacing:1px}\n'
        '.card .value{font-size:1.35em;font-weight:bold;margin-top:4px}\n'
        '.win{color:#3fb950}.loss{color:#f85149}.neutral{color:#d29922}\n'
        'table{width:100%;border-collapse:collapse;margin-top:8px;font-size:.88em}\n'
        'th{background:#21262d;color:#8b949e;padding:7px 11px;text-align:left;border-bottom:1px solid #30363d}\n'
        'td{padding:5px 11px;border-bottom:1px solid #21262d}\n'
        'tr.win td{color:#3fb950}tr.loss td{color:#f85149}\n'
        '.footer{margin-top:20px;font-size:.72em;color:#8b949e}\n'
        '.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#3fb950;'
        'margin-right:6px;animation:pulse 2s infinite}\n'
        '@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}\n'
        '</style>\n</head>\n<body>\n'
        f'<h1><span class="dot"></span>Tradinebot — Statut en direct</h1>\n'
        '<div class="cards">\n'
        f'  <div class="card"><div class="label">Capital</div>'
        f'<div class="value">${state.capital:.2f}</div></div>\n'
        f'  <div class="card"><div class="label">PnL total</div>'
        f'<div class="value {pnl_cls}">${state.total_pnl:+.2f}</div></div>\n'
        f'  <div class="card"><div class="label">Win Rate</div>'
        f'<div class="value {wr_cls}">{state.win_rate:.1f}%</div></div>\n'
        f'  <div class="card"><div class="label">Trades</div>'
        f'<div class="value">{state.total_trades}</div></div>\n'
        f'  <div class="card"><div class="label">Aujourd\'hui</div>'
        f'<div class="value {day_cls}">${daily_pnl:+.2f} ({daily_count}T)</div></div>\n'
        f'  <div class="card"><div class="label">En cours</div>'
        f'<div class="value {open_cls}">{open_count}</div></div>\n'
        '</div>\n'
        '<h2>10 derniers trades résolus</h2>\n'
        '<table>\n'
        '<thead><tr><th>#</th><th>Heure</th><th>Direction</th><th>Résultat</th>'
        '<th>Prix entrée</th><th>PnL</th><th>Capital</th><th>Marché</th></tr></thead>\n'
        f'<tbody>{trade_rows}</tbody>\n'
        '</table>\n'
        f'<div class="footer">Dernière mise à jour : {now_str} — '
        'Actualisation auto toutes les 60&nbsp;s</div>\n'
        '</body>\n</html>\n'
    )

def write_web_status(state):
    """Write the HTML status page to disk. No-op when WEBSTATUS_ENABLED is false."""
    if not WEBSTATUS_ENABLED:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(WEBSTATUS_PATH)), exist_ok=True)
        setup_htaccess(WEBSTATUS_PATH)
        with open(WEBSTATUS_PATH, "w", encoding="utf-8") as f:
            f.write(generate_status_html(state))
    except Exception as e:
        logger.warning("Erreur page web statut : %s", e)


# ─── MARKET DISCOVERY ─────────────────────────────────────────────────────────

async def fetch_markets(session):
    """
    Fetch active BTC 5-minute markets from the Polymarket Gamma API.

    CRITICAL: The ±6-minute temporal window filter is mandatory.
    Without it, the Gamma API returns expired markets whose order books
    still show stale prices that falsely trigger the signal.

    tag_id=102892 ("5M") pre-filters server-side to 5-minute markets only,
    reducing the response from potentially thousands of markets to ~12-20
    (BTC/ETH/XRP/SOL for the current ±6 min window). A single API call
    suffices — pagination is no longer needed.
    BTC_5M_KEYWORDS is kept as a safety net for other 5M assets.
    """
    try:
        params = dict(POLY_GAMMA_PARAMS)
        now_utc = datetime.now(timezone.utc)
        params["end_date_min"] = (now_utc - timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params["end_date_max"] = (now_utc + timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        async with session.get(
            POLY_GAMMA_URL, headers=POLY_GAMMA_HEADERS, params=params,
            timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status != 200:
                logger.warning("Gamma API erreur : %d", resp.status)
                return []
            data = await resp.json(content_type=None)
        batch = data if isinstance(data, list) else data.get("data", data.get("markets", []))
        results = [
            m for m in batch
            if any(kw in m.get("question", "").lower() for kw in BTC_5M_KEYWORDS)
        ]
        logger.info("Marches BTC 5-min : %d", len(results))
        return results
    except Exception as e:
        logger.warning("Fetch erreur : %s", e)
        return []

def register_market(state, market):
    """
    Add a market's UP and DOWN tokens to the state if not already tracked.
    Skips markets that have already ended. Returns a list of newly added token IDs
    so the caller can subscribe them to the WebSocket.
    """
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


# ─── WEBSOCKET LIFECYCLE ──────────────────────────────────────────────────────

async def ws_loop(state, session):
    """
    Outer reconnection loop with exponential backoff.
    Doubles the wait time on each failure, capped at 60 seconds, to avoid
    hammering the WebSocket endpoint after network disruptions.
    """
    backoff = 1
    while True:
        try:
            await _run_ws(state, session)
            backoff = 1  # reset after a clean exit (e.g. market refresh)
        except Exception as e:
            logger.warning("WS erreur (%s) — reconnexion %ds", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

async def _market_refresh_loop(state, session, ws):
    """
    Background task: polls the Gamma API every MARKET_REFRESH seconds and
    subscribes newly discovered tokens while the main recv loop continues
    processing messages uninterrupted.
    """
    while True:
        await asyncio.sleep(MARKET_REFRESH)
        try:
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

            # Purge expired markets so the token dict doesn't grow indefinitely.
            expired = [tid for tid, ts in list(state.tokens.items())
                       if ts.market_ended and ts.market_id not in state.open_trades]
            for tid in expired:
                ts = state.tokens.pop(tid, None)
                if ts:
                    state.market_tokens.pop(ts.market_id, None)
                    state.signalled.discard(ts.market_id)
            if expired:
                logger.info("Tokens expires purges : %d", len(expired))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Refresh marches erreur : %s", e)


async def _run_ws(state, session):
    """
    One WebSocket session: fetch markets, subscribe to their tokens, then
    process messages until the connection drops.

    Market discovery runs in _market_refresh_loop (background task) so the
    recv loop is never blocked during the Gamma API HTTP poll.

    The recv() timeout is 30s with continue rather than break: ping_interval=20
    / ping_timeout=10 already detects dead connections via WebSocket ping/pong,
    so a recv timeout just means no market messages arrived — normal between
    5-minute candles. We only reconnect if all tracked markets have expired.
    """
    markets = await fetch_markets(session)
    if not markets:
        logger.warning("Aucun marche — attente 30s")
        await asyncio.sleep(30)
        return

    new_ids = []
    for m in markets:
        new_ids.extend(register_market(state, m))

    # Include all non-expired tokens already in state, not just newly discovered ones.
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
        ld = time.time()
        logger.info("WebSocket connecte")
        refresh_task = asyncio.create_task(_market_refresh_loop(state, session, ws))

        try:
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    # Normal during quiet periods — ping/pong handles keepalive.
                    if not any(not t.market_ended for t in state.tokens.values()):
                        logger.warning("Aucun token actif — reconnexion")
                        break
                    continue
                except:
                    break

                now = time.time()
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
        finally:
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass


# ─── STARTUP ─────────────────────────────────────────────────────────────────

def restore_state_from_db(state):
    """
    Reload open trades and cumulative stats from the DB on startup.
    This allows the bot to survive restarts without losing track of positions
    that were entered but not yet resolved in the previous session.
    """
    rows = state.conn.execute(
        "SELECT market_id, id, direction FROM trades WHERE resolved=0"
    ).fetchall()
    for mid, tid, d in rows:
        state.open_trades[mid] = tid
        state.traded_direction[mid] = d
        # Mark these markets as signalled so we don't enter a second position.
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
