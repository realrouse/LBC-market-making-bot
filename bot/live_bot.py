#!/usr/bin/env python3
"""
POLYMARKET LIVE BOT v0.41 — BTC Up/Down 5-minute markets

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

import argparse, asyncio, copy, json, logging, logging.handlers, math, os, queue, sqlite3, sys, time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
import aiohttp, websockets

# process start epoch — used for uptime display; harmless to capture at import
_BOT_START: float = time.time()

# sys.path insert finds api_polymarket.py in the same directory as this file,
# whether running standalone from INSTALL_DIR or imported as bot.live_bot from
# the project root. To target a different exchange, replace the import below.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api_polymarket as api
import bot_utils
from bot_utils import print_dashboard, write_web_status

# ─── STRATEGY DEFAULTS (module-level — tests reference these directly) ────────
# Hardcoded defaults — overridden by make_config() when a strategy JSON is
# found. Kept as module-level constants so test_regression.py can read them
# with getattr(bot, 'SIGNAL_THRESHOLD') without importing config.
# FEE_RATE (2%) lives in api_polymarket.py as it is exchange-specific.
CAPITAL_START      = 100.0
STAKE              = 10.0
GAS_FEE_USD        = 0.03
SIGNAL_THRESHOLD   = 0.95   # was 0.96 — sweep: +$14.73 vs +$13.14 on liveweek (30s/-0.25)
ENTRY_MAX          = 0.998
MIN_SECS_REMAINING = 30.0   # was 45.0 — sweep: 30s gains ~+$7 PnL on liveweek vs 45s
MIN_ASK_VOL        = 10.0
WIN_THRESHOLD      = 0.99
LOSS_THRESHOLD     = 0.01
OBI_REJECT_THRESH  = -0.25  # was -0.50 — sweep: -0.25 flips PnL positive on liveweek
DAILY_STOP_LOSS    = 30.0

# ─── HOUR FILTER DEFAULTS ─────────────────────────────────────────────────────
HOUR_FILTER_ENABLED:    bool                  = False
WEEKDAY_UTC_RANGES:     list[tuple[int, int]] = []
WEEKEND_UTC_RANGES:     list[tuple[int, int]] = []
US_WEEKLY_OPEN:         bool                  = True
US_WEEKLY_CLOSE:        bool                  = True

# ─── TIMING / SNAPSHOT DEFAULTS ───────────────────────────────────────────────
SNAPSHOT_INTERVAL  = 5
DASHBOARD_INTERVAL = 300
MARKET_REFRESH     = 30

# ─── VOLATILITY FILTER DEFAULTS ───────────────────────────────────────────────
VOL_FILTER_ENABLED      = True
VOL_FILTER_WEEKDAY_ONLY = True
VOL_WINDOW              = 12
VOL_MIN_SAMPLES         = 6
VOL_BID_MAX             = 0.07
RANGE_BID_MAX           = 0.30
OBI_VOL_MAX             = 0.40

# ─── LOGGING FORMATTERS ───────────────────────────────────────────────────────
_LEVEL_ABBR: dict[str, str] = {
    "DEBUG": "DEBUG", "INFO": "INFO ", "WARNING": "WARN ",
    "ERROR": "ERROR", "CRITICAL": "CRIT ",
}
_ANSI_COLOR: dict[str, str] = {
    "WARN ": "\033[33m",
    "ERROR": "\033[31m",
    "CRIT ": "\033[35m",
}
_ANSI_RESET = "\033[0m"
_LOG_FMT  = "%(asctime)s [%(levelname)s] %(message)s"
_LOG_DATE = "%Y-%m-%d %H:%M:%S"

class _PlainFmt(logging.Formatter):
    """File formatter: no colors, fixed-width level abbreviation."""
    def format(self, record: logging.LogRecord) -> str:
        r = copy.copy(record)
        r.levelname = _LEVEL_ABBR.get(r.levelname, r.levelname[:5])
        return super().format(r)

class _ColorFmt(logging.Formatter):
    """Stdout formatter: same as _PlainFmt plus ANSI color on WARN/ERROR."""
    def format(self, record: logging.LogRecord) -> str:
        abbr = _LEVEL_ABBR.get(record.levelname, record.levelname[:5])
        r = copy.copy(record)
        r.levelname = abbr
        msg = super().format(r)
        color = _ANSI_COLOR.get(abbr, "")
        return f"{color}{msg}{_ANSI_RESET}" if color else msg

# _log_listener is module-level so the __main__ block can call .stop() on exit
# to flush any buffered records before the process terminates.
_log_listener: Optional[logging.handlers.QueueListener] = None

# Named logger — defined here so all functions below can reference it.
# Handlers are configured later by _setup_logging(); until then any record
# falls through to the root logger (which is a no-op before basicConfig).
logger = logging.getLogger("live")

# ─── CONFIGURATION DATACLASS ─────────────────────────────────────────────────

@dataclass
class BotConfig:
    """
    All runtime configuration for one bot instance.

    Instantiated by make_config() which reads env vars, config.json, and the
    strategy JSON.  A default BotConfig() computes paths from TRADINEBOTTE_DIR
    so it is safe to construct without calling make_config() (e.g. in tests).
    """
    # Runtime flags
    simulate:     bool = False
    no_log:       bool = False
    no_snapshots: bool = False

    # Paths — computed in __post_init__ when left empty
    install_dir:  str = ""
    db_path:      str = ""
    log_path:     str = ""
    config_path:  str = ""

    # Strategy
    capital_start:      float = CAPITAL_START
    stake:              float = STAKE
    gas_fee_usd:        float = GAS_FEE_USD
    signal_threshold:   float = SIGNAL_THRESHOLD
    entry_max:          float = ENTRY_MAX
    min_secs_remaining: float = MIN_SECS_REMAINING
    min_ask_vol:        float = MIN_ASK_VOL
    win_threshold:      float = WIN_THRESHOLD
    loss_threshold:     float = LOSS_THRESHOLD
    obi_reject_thresh:  float = OBI_REJECT_THRESH
    daily_stop_loss:    float = DAILY_STOP_LOSS

    # Hour filter
    hour_filter_enabled:  bool = HOUR_FILTER_ENABLED
    weekday_utc_ranges:   list = field(default_factory=list)
    weekend_utc_ranges:   list = field(default_factory=list)
    us_weekly_open:       bool = US_WEEKLY_OPEN
    us_weekly_close:      bool = US_WEEKLY_CLOSE

    # Timing
    snapshot_interval:  int = SNAPSHOT_INTERVAL
    enable_snapshots:   bool = True
    dashboard_interval: int = DASHBOARD_INTERVAL
    market_refresh:     int = MARKET_REFRESH

    # Volatility filter
    vol_filter_enabled:      bool  = VOL_FILTER_ENABLED
    vol_filter_weekday_only: bool  = VOL_FILTER_WEEKDAY_ONLY
    vol_window:              int   = VOL_WINDOW
    vol_min_samples:         int   = VOL_MIN_SAMPLES
    vol_bid_max:             float = VOL_BID_MAX
    range_bid_max:           float = RANGE_BID_MAX
    obi_vol_max:             float = OBI_VOL_MAX

    # Credentials
    private_key:    str = ""
    api_key:        str = ""
    api_secret:     str = ""
    api_passphrase: str = ""

    # DB options
    db_mmap_mb: int = 0

    # Web status
    webstatus_enabled:  bool = False
    webstatus_path:     str  = ""
    webstatus_user:     str  = "tradinebot"
    webstatus_password: str  = ""

    # Strategy file (for logging)
    strategy_loaded: Optional[str] = None

    def __post_init__(self) -> None:
        # Compute paths from TRADINEBOTTE_DIR when not explicitly provided.
        if not self.install_dir:
            self.install_dir = os.path.expanduser(
                os.environ.get("TRADINEBOTTE_DIR", "~/tradinebotte")
            )
        if not self.db_path:
            self.db_path = os.path.join(self.install_dir, "live.db")
        if not self.log_path:
            self.log_path = os.path.join(self.install_dir, "live.log")
        if not self.config_path:
            self.config_path = os.path.join(self.install_dir, "config.json")
        if not self.webstatus_path:
            self.webstatus_path = os.path.expanduser("~/public_html/tradinebot_status.html")
        if self.no_snapshots:
            self.enable_snapshots = False


# ─── CONFIGURATION HELPERS ───────────────────────────────────────────────────

def load_config(config_path: str) -> dict[str, Any]:
    """Load config.json from the given path. Returns {} if the file is absent."""
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_strategy(path: str) -> Optional[dict[str, Any]]:
    """Load strategy parameters from a JSON file. Returns None if not found."""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _parse_args() -> argparse.Namespace:
    """Parse CLI flags for live_bot.py."""
    p = argparse.ArgumentParser(description="tradinebotte live bot")
    p.add_argument("--simulate",      action="store_true",
                   help="Redirect I/O to ~/tradinebotte-sim; orders simulated")
    p.add_argument("--no-log",        action="store_true",
                   help="Skip log file (NullHandler); SQLite DB unaffected")
    p.add_argument("--no-snapshots",  action="store_true",
                   help="Skip snapshot writes for lower disk I/O")
    p.add_argument("--snapshot-interval", type=int, default=None, metavar="SECS",
                   help=f"Snapshot write interval in seconds (default: {SNAPSHOT_INTERVAL}). "
                        "Use 1 for data-collection mode.")
    return p.parse_args()


def make_config(simulate: bool = False, no_log: bool = False,
                no_snapshots: bool = False,
                snapshot_interval: Optional[int] = None) -> "BotConfig":
    """
    Build a BotConfig by reading env vars, config.json, and the strategy JSON.

    This is the only function that performs file I/O for configuration; it must
    be called from main() (or account_bot) before any BotState is created.
    """
    # --simulate: redirect all file I/O to ~/tradinebotte-sim.
    if simulate and "TRADINEBOTTE_DIR" not in os.environ:
        os.environ["TRADINEBOTTE_DIR"] = os.path.expanduser("~/tradinebotte-sim")

    install_dir = os.path.expanduser(
        os.environ.get("TRADINEBOTTE_DIR", "~/tradinebotte")
    )
    db_path     = os.path.join(install_dir, "live.db")
    log_path    = os.path.join(install_dir, "live.log")
    config_path = os.path.join(install_dir, "config.json")

    cfg   = load_config(config_path)
    strat_path = cfg.get("strategy",
                         os.path.join(install_dir, "strategies", "polymarket_BTC5M_v2.json"))
    strat_path = os.path.expanduser(strat_path)
    if not os.path.isabs(strat_path):
        strat_path = os.path.join(install_dir, strat_path)
    strat = load_strategy(strat_path) or {}
    strategy_loaded = strat_path if strat else None

    # Strategy overrides from JSON (fall back to module-level defaults).
    capital_start      = float(strat.get("capital_start",      CAPITAL_START))
    stake              = float(strat.get("stake",               STAKE))
    gas_fee_usd        = float(strat.get("gas_fee_usd",         GAS_FEE_USD))
    signal_threshold   = float(strat.get("signal_threshold",    SIGNAL_THRESHOLD))
    entry_max          = float(strat.get("entry_max",           ENTRY_MAX))
    min_secs_remaining = float(strat.get("min_secs_remaining",  MIN_SECS_REMAINING))
    min_ask_vol        = float(strat.get("min_ask_vol",         MIN_ASK_VOL))
    win_threshold      = float(strat.get("win_threshold",       WIN_THRESHOLD))
    loss_threshold     = float(strat.get("loss_threshold",      LOSS_THRESHOLD))
    obi_reject_thresh  = float(strat.get("obi_reject_thresh",   OBI_REJECT_THRESH))
    daily_stop_loss    = float(strat.get("daily_stop_loss",     DAILY_STOP_LOSS))

    _hf = strat.get("hour_filter", {})
    hour_filter_enabled = bool(_hf.get("enabled", False))
    weekday_utc_ranges  = [tuple(r) for r in _hf.get("weekday_utc_ranges", [])]
    weekend_utc_ranges  = [tuple(r) for r in _hf.get("weekend_utc_ranges", [])]
    us_weekly_open      = bool(_hf.get("us_weekly_open", True))
    us_weekly_close     = bool(_hf.get("us_weekly_close", True))

    # Credentials: config.json first, env vars as fallback for containers.
    private_key    = cfg.get("private_key",    os.environ.get("POLY_PRIVATE_KEY", ""))
    api_key        = cfg.get("api_key",        os.environ.get("POLY_API_KEY", ""))
    api_secret     = cfg.get("api_secret",     os.environ.get("POLY_API_SECRET", ""))
    api_passphrase = cfg.get("api_passphrase", os.environ.get("POLY_PASSPHRASE", ""))
    db_mmap_mb     = int(cfg.get("db_mmap_mb", 0))

    # Web status page.
    _ws_raw = cfg.get("webstatuspage_html", False)
    webstatus_enabled = (
        str(_ws_raw).lower() in ("true", "1", "yes")
        if not isinstance(_ws_raw, bool) else _ws_raw
    )
    webstatus_path     = os.path.expanduser(
        cfg.get("webstatuspage_path", "~/public_html/tradinebot_status.html"))
    webstatus_user     = cfg.get("webstatus_user", "tradinebot")
    webstatus_password = cfg.get("webstatus_password", "")

    # Ensure the install directory exists before any file handle is opened.
    os.makedirs(install_dir, exist_ok=True)

    config = BotConfig(
        simulate=simulate,
        no_log=no_log,
        no_snapshots=no_snapshots,
        snapshot_interval=snapshot_interval if snapshot_interval is not None else SNAPSHOT_INTERVAL,
        install_dir=install_dir,
        db_path=db_path,
        log_path=log_path,
        config_path=config_path,
        capital_start=capital_start,
        stake=stake,
        gas_fee_usd=gas_fee_usd,
        signal_threshold=signal_threshold,
        entry_max=entry_max,
        min_secs_remaining=min_secs_remaining,
        min_ask_vol=min_ask_vol,
        win_threshold=win_threshold,
        loss_threshold=loss_threshold,
        obi_reject_thresh=obi_reject_thresh,
        daily_stop_loss=daily_stop_loss,
        hour_filter_enabled=hour_filter_enabled,
        weekday_utc_ranges=weekday_utc_ranges,
        weekend_utc_ranges=weekend_utc_ranges,
        us_weekly_open=us_weekly_open,
        us_weekly_close=us_weekly_close,
        enable_snapshots=not no_snapshots,
        private_key=private_key,
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
        db_mmap_mb=db_mmap_mb,
        webstatus_enabled=webstatus_enabled,
        webstatus_path=webstatus_path,
        webstatus_user=webstatus_user,
        webstatus_password=webstatus_password,
        strategy_loaded=strategy_loaded,
    )

    # Sync display config to bot_utils so its functions read the correct values.
    bot_utils.WEBSTATUS_ENABLED  = webstatus_enabled
    bot_utils.WEBSTATUS_PATH     = webstatus_path
    bot_utils.WEBSTATUS_USER     = webstatus_user
    bot_utils.WEBSTATUS_PASSWORD = webstatus_password
    bot_utils.INSTALL_DIR        = install_dir

    return config


def _setup_logging(config: "BotConfig") -> Optional[logging.handlers.QueueListener]:
    """
    Configure the root logger according to config and return the QueueListener
    (or None when --no-log is active without --simulate).

    Async logging: logger.info() enqueues a LogRecord nanoseconds; the
    QueueListener daemon thread calls FileHandler.emit() — the only disk I/O.
    """
    global _log_listener  # pylint: disable=global-statement

    if config.no_log:
        if config.simulate:
            _h = logging.StreamHandler(sys.stdout)
            _h.setFormatter(_ColorFmt(_LOG_FMT, datefmt=_LOG_DATE))
            _log_handlers: list[logging.Handler] = [_h]
        else:
            _log_handlers = [logging.NullHandler()]
        listener = None
    else:
        _log_queue: queue.Queue[logging.LogRecord] = queue.Queue()
        _file_handler = logging.FileHandler(config.log_path)
        _file_handler.setFormatter(_PlainFmt(_LOG_FMT, datefmt=_LOG_DATE))
        listener = logging.handlers.QueueListener(
            _log_queue, _file_handler, respect_handler_level=True)
        listener.start()
        _qh = logging.handlers.QueueHandler(_log_queue)
        # Passthrough formatter prevents double-formatting (raw message only).
        _qh.setFormatter(logging.Formatter("%(message)s"))
        _log_handlers = [_qh]
        if config.simulate:
            _h = logging.StreamHandler(sys.stdout)
            _h.setFormatter(_ColorFmt(_LOG_FMT, datefmt=_LOG_DATE))
            _log_handlers.append(_h)

    logging.basicConfig(
        level=logging.INFO,
        format=_LOG_FMT,
        datefmt=_LOG_DATE,
        handlers=_log_handlers,
    )
    _log_listener = listener
    return listener


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
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id);
CREATE INDEX IF NOT EXISTS idx_trades_resolved ON trades(resolved);
"""

# Each key is a schema version number; value is the DDL to apply.
# Version 1 is the baseline: all tables already exist from SCHEMA above.
# Add future entries here: MIGRATIONS[2] = "ALTER TABLE trades ADD COLUMN ..."
MIGRATIONS: dict[int, str] = {
    1: "",
}


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply any pending schema migrations and record the version reached."""
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    current = row[0] if row else 0
    for version in sorted(MIGRATIONS):
        if version <= current:
            continue
        ddl = MIGRATIONS[version]
        if ddl:
            conn.executescript(ddl)
        if row is None:
            conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
            row = True
        else:
            conn.execute("UPDATE schema_version SET version=?", (version,))
        logger.info("DB schema migration v%d appliquee", version)


def init_db(config: "BotConfig") -> sqlite3.Connection:
    """Open (or create) the SQLite database and apply schema migrations."""
    # check_same_thread=False is safe here because asyncio is single-threaded;
    # the connection is only ever accessed from the event loop.
    conn = sqlite3.connect(config.db_path, check_same_thread=False)
    conn.executescript(SCHEMA)
    _apply_migrations(conn)
    if config.db_mmap_mb > 0:
        # Memory-map the DB file so SQLite accesses it via RAM pages managed
        # by the kernel rather than read() syscalls.
        conn.execute(f"PRAGMA mmap_size = {config.db_mmap_mb * 1024 * 1024};")
        logger.info("DB mmap active : %d MB", config.db_mmap_mb)
    conn.commit()
    logger.info("DB initialisee : %s", config.db_path)
    return conn


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
                 "last_update_ts", "last_snapshot_ts",
                 "bid_history", "obi_history")

    def __init__(self, token_id: str, market_id: str, direction: str, question: str,
                 market_start_ms: int, market_end_ms: int) -> None:
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
        # Rolling windows sampled every SNAPSHOT_INTERVAL seconds.
        # Used by the volatility filter in check_signal to detect choppy markets.
        self.bid_history: deque[float] = deque(maxlen=VOL_WINDOW)
        self.obi_history: deque[float] = deque(maxlen=VOL_WINDOW)

    @property
    def secs_remaining(self) -> float:
        """Seconds until the market's scheduled end time (0 if already past)."""
        if not self.market_end_ms: return 9999.0
        return max(0.0, (self.market_end_ms - time.time() * 1000) / 1000.0)

    @property
    def seconds_elapsed(self) -> float:
        """Seconds since the market's scheduled start time."""
        if not self.market_start_ms: return 0.0
        return max(0.0, (time.time() * 1000 - self.market_start_ms) / 1000.0)

    @property
    def market_ended(self) -> bool:
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
    def __init__(self, conn: sqlite3.Connection,
                 config: Optional["BotConfig"] = None) -> None:
        self.conn = conn
        self.config: BotConfig = config if config is not None else BotConfig()
        self.capital = self.config.capital_start
        self.session: Optional[aiohttp.ClientSession] = None
        self.tokens: dict[str, "TokenState"] = {}             # token_id → TokenState
        self.market_tokens: dict[str, dict[str, str]] = {}    # market_id → {"UP": tid, "DOWN": tid}
        self.open_trades: dict[str, int] = {}                 # market_id → trade row id in DB
        self.traded_direction: dict[str, str] = {}            # market_id → "UP" or "DOWN"
        # signalled prevents re-entering a market we already entered (or
        # attempted to enter) this session, even if the price signal fires again.
        self.signalled: set[str] = set()
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        # Daily PnL cache — updated incrementally in close_trade, reset at UTC
        # midnight by check_signal. Eliminates the SQL SELECT on the hot path.
        self.daily_pnl: float = 0.0
        self._daily_pnl_day: int = -1   # UTC day number (days since epoch)
        # Circuit-breaker: suspend new entries after 3 consecutive CLOB failures.
        self.api_fail_streak: int = 0
        self.api_cooldown_until: float = 0.0

    @property
    def win_rate(self) -> float:
        """Win rate as a percentage, 0.0 if no resolved trades."""
        t = self.wins + self.losses
        return (self.wins / t * 100) if t else 0.0


# ─── WEBSOCKET MESSAGE HANDLING ───────────────────────────────────────────────

async def handle_book_update(state: BotState, parsed: dict[str, Any]) -> None:
    """Apply a parsed book update to the token's state, then check for signals."""
    # Capture the monotonic clock as early as possible — before even the token
    # lookup — so t_ws represents the true start of processing this WS message.
    t_ws = time.monotonic()
    ts = state.tokens.get(parsed["token_id"])
    if not ts: return
    ts.best_bid  = parsed["best_bid"]
    ts.best_ask  = parsed["best_ask"]
    ts.spread    = parsed["spread"]
    ts.bid_vol   = parsed["bid_vol"]
    ts.ask_vol   = parsed["ask_vol"]
    ts.obi       = parsed["obi"]
    ts.last_update_ts = time.time()
    # Pass t_ws so enter_live_trade can compute end-to-end latency if a trade fires.
    await check_signal(state, ts, _t_ws=t_ws)
    check_resolution(state, ts)
    now = time.time()
    if now - ts.last_snapshot_ts >= state.config.snapshot_interval:
        ts.bid_history.append(ts.best_bid)
        ts.obi_history.append(ts.obi)
        if state.config.enable_snapshots:
            save_snapshot(state, ts)
        ts.last_snapshot_ts = now


# ─── SIGNAL & TRADE LOGIC ─────────────────────────────────────────────────────

def _in_weekend_session(ts_ms: Optional[int] = None) -> bool:
    """Return True if timestamp falls in the weekend session (Fri 20:00 → Mon 13:30 UTC)."""
    dt     = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms is not None \
             else datetime.now(timezone.utc)
    dow    = dt.weekday()
    hour   = dt.hour
    minute = dt.minute
    if dow in (5, 6):                                  # Sat / Sun: always weekend
        return True
    if dow == 4 and hour >= 20:                       # Fri from US weekly close
        return True
    if dow == 0 and (hour < 13 or (hour == 13 and minute < 30)):  # Mon before US open
        return True
    return False


def is_trading_hour(config: "BotConfig", ts_ms: Optional[int] = None) -> bool:
    """Return True if the timestamp (or now) falls in the configured trading window."""
    if not config.hour_filter_enabled:
        return True
    dt     = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms is not None \
             else datetime.now(timezone.utc)
    dow    = dt.weekday()
    hour   = dt.hour
    minute = dt.minute

    if dow >= 5:
        if not config.weekend_utc_ranges:
            return False
        return any(s <= hour < e for s, e in config.weekend_utc_ranges)

    if dow == 0 and config.us_weekly_open:
        if hour < 13 or (hour == 13 and minute < 30):
            return False
    if dow == 4 and config.us_weekly_close:
        if hour >= 20:
            return False

    if not config.weekday_utc_ranges:
        return True
    return any(s <= hour < e for s, e in config.weekday_utc_ranges)


async def check_signal(state: BotState, ts: TokenState, _t_ws: Optional[float] = None) -> None:
    """
    Evaluate entry conditions (cheapest first) and enter a trade if all pass.

    Guards: signalled, market_ended, is_trading_hour, best_bid threshold,
    best_bid/ask caps, ask_vol, secs_remaining, obi, vol_filter, capital,
    daily_stop_loss.  _t_ws is passed through to enter_live_trade for latency
    tracking (None = no tracking).
    """
    cfg = state.config

    # Midnight UTC reset — runs on every book update so the counter resets
    # promptly at midnight regardless of whether any signal fires that day.
    today_day = int(time.time() // 86400)
    if state._daily_pnl_day != today_day:
        state.daily_pnl = 0.0
        state._daily_pnl_day = today_day

    if ts.market_id in state.signalled: return
    if ts.market_ended: return
    if not is_trading_hour(cfg): return
    if ts.best_bid < cfg.signal_threshold: return
    if ts.best_bid > cfg.entry_max: return
    if ts.best_ask >= 1.0: return       # expired markets still emit WS messages
    if ts.best_ask > cfg.entry_max: return
    if ts.ask_vol > 0 and ts.ask_vol < cfg.min_ask_vol: return
    if ts.secs_remaining < cfg.min_secs_remaining: return
    if ts.obi < cfg.obi_reject_thresh: return
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
            logger.debug("VOL FILTER bid_vol=%.3f range=%.3f obi_vol=%.3f — skip %s",
                         vol_bid, range_bid, obi_vol, ts.market_id[:12])
            return
    if state.capital - len(state.open_trades) * cfg.stake < cfg.stake: return
    if state.daily_pnl < -cfg.daily_stop_loss: return
    if time.time() < state.api_cooldown_until: return

    state.signalled.add(ts.market_id)
    await enter_live_trade(state, ts, _t_ws=_t_ws)

async def enter_live_trade(state: BotState, ts: TokenState, _t_ws: Optional[float] = None) -> None:
    """
    Record the trade in the DB, submit the CLOB order, and update bot state.
    The DB insert happens even if the CLOB order fails so we always have an
    audit trail and can detect orphaned positions on restart.

    _t_ws: monotonic timestamp from handle_book_update. When provided, latency
    metrics are computed and emitted as a [LATENCY] log line parseable by
    scripts/latency.py.
    """
    cfg = state.config
    now_ms = int(time.time() * 1000)
    ep = ts.best_ask                        # entry price = best available ask
    tb = cfg.stake / ep if ep > 0 else 0   # tokens bought at this price
    fee = api.compute_fee(ep, tb)
    cost = cfg.stake + fee
    oid = None

    # Latency measurement — point A: everything from WS message receipt up to
    # this point (token update, all signal guards, daily PnL query, fee calc).
    t_signal_ms = (time.monotonic() - _t_ws) * 1000 if _t_ws is not None else None

    # Latency measurement — point B: bracket only the CLOB API HTTP call so we
    # can isolate network RTT from in-process signal latency.
    t_pre_order = time.monotonic()
    if state.session:
        oid = await api.post_order(
            state.session, ts.token_id, ep, cfg.stake,
            private_key=cfg.private_key, install_dir=cfg.install_dir,
        )
    order_rtt_ms = (time.monotonic() - t_pre_order) * 1000
    if state.session and cfg.private_key:
        if oid is None:
            state.api_fail_streak += 1
            if state.api_fail_streak >= 3:
                state.api_cooldown_until = time.time() + 300
                logger.warning(
                    "CIRCUIT-BREAKER: %d échecs CLOB consécutifs — entrées suspendues 5 min",
                    state.api_fail_streak,
                )
        else:
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
         now_ms, ep, oid, cfg.stake, tb, fee, cost, state.capital, 0)
    )
    state.conn.commit()
    tid = cur.lastrowid or 0
    state.open_trades[ts.market_id] = tid
    state.traded_direction[ts.market_id] = ts.direction
    state.total_trades += 1
    logger.info(
        "▶ TRADE #%d | %s %s | entry=%.4f  bid=%.4f  secs=%.0fs | order=%s",
        tid, ts.direction, ts.market_id[:12], ep, ts.best_bid,
        ts.secs_remaining, oid or "sim"
    )
    if t_signal_ms is not None:
        # total_ms = signal latency + order RTT; these two intervals are
        # contiguous so their sum equals true end-to-end time from WS to order.
        total_ms = t_signal_ms + order_rtt_ms
        logger.info(
            "[LATENCY] signal_ms=%.2f order_rtt_ms=%.2f total_ms=%.2f"
            " direction=%s market=%s",
            t_signal_ms, order_rtt_ms, total_ms, ts.direction, ts.market_id[:12],
        )

def check_resolution(state: BotState, ts: TokenState) -> None:
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

def close_trade(state: BotState, ts: TokenState, trade_id: int, outcome: str) -> None:
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
    pg = (tb - stake) if won else -stake
    pn = pg - fee - state.config.gas_fee_usd
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
    state.daily_pnl += pn
    if won: state.wins += 1
    else: state.losses += 1
    del state.open_trades[ts.market_id]
    del state.traded_direction[ts.market_id]
    _icon    = "✓" if won else "✗"
    _outcome = "WIN " if won else "LOSS"
    logger.info(
        "%s %s #%d | %s %s | pnl=$%+.2f (%.1f%%) | WR=%.1f%% | capital=$%.2f",
        _icon, _outcome, trade_id, ts.direction, ts.market_id[:12],
        pn, roi, state.win_rate, state.capital
    )
    write_web_status(state)

def save_snapshot(state: BotState, ts: TokenState) -> None:
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

# ─── MARKET DISCOVERY ─────────────────────────────────────────────────────────

def purge_expired_markets(state: BotState) -> int:
    """
    Remove tokens whose market has ended (plus grace period) and has no open
    trade, cleaning up tokens, market_tokens, and signalled in one pass.
    Returns the number of tokens removed.
    """
    expired = [tid for tid, ts in list(state.tokens.items())
               if ts.market_ended and ts.market_id not in state.open_trades]
    for tid in expired:
        ts = state.tokens.pop(tid, None)
        if ts:
            state.market_tokens.pop(ts.market_id, None)
            state.signalled.discard(ts.market_id)
    return len(expired)


def register_market(state: BotState, market: dict[str, Any]) -> list[str]:
    """
    Add a market's UP and DOWN tokens to the state if not already tracked.
    Skips markets that have already ended. Returns a list of newly added token IDs
    so the caller can subscribe them to the WebSocket.
    """
    mid = api.get_market_id(market)
    if not mid: return []
    up = api.get_up_token_id(market)
    dn = api.get_down_token_id(market)
    if not up or not dn: return []
    sm = api.get_market_start_ts_ms(market)
    em = api.get_market_end_ts_ms(market)
    if em and time.time() * 1000 > em: return []
    q = api.get_market_question(market)[:80]
    new = []
    for tid, d in ((up, "UP"), (dn, "DOWN")):
        if tid not in state.tokens:
            state.tokens[tid] = TokenState(tid, mid, d, q, sm, em)
            new.append(tid)
    state.market_tokens[mid] = {"UP": up, "DOWN": dn}
    return new


# ─── WEBSOCKET LIFECYCLE ──────────────────────────────────────────────────────

async def ws_loop(state: BotState, session: aiohttp.ClientSession) -> None:
    """
    Outer reconnection loop with exponential backoff.
    Doubles the wait time on each failure, capped at 60 seconds, to avoid
    hammering the WebSocket endpoint after network disruptions.
    """
    backoff = 1
    while True:
        try:
            await _run_ws(state, session)
            backoff = 1
        except Exception as e:
            logger.warning("WS erreur (%s) — reconnexion %ds", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

async def _market_refresh_loop(state: BotState, session: aiohttp.ClientSession, ws: Any) -> None:
    """
    Background task: polls the exchange API every market_refresh seconds and
    subscribes newly discovered tokens while the main recv loop continues
    processing messages uninterrupted.
    """
    while True:
        await asyncio.sleep(state.config.market_refresh)
        try:
            nm = await api.get_markets(session)
            ni = []
            for m in nm:
                ni.extend(register_market(state, m))
            if ni:
                for i in range(0, len(ni), api.WS_BATCH_SIZE):
                    await ws.send(api.make_subscribe_msg(ni[i:i + api.WS_BATCH_SIZE]))
                logger.info("Nouveaux tokens : %d", len(ni))

            n = purge_expired_markets(state)
            if n:
                logger.info("Tokens expires purges : %d", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Refresh marches erreur : %s", e)


async def _run_ws(state: BotState, session: aiohttp.ClientSession) -> None:
    """
    One WebSocket session: fetch markets, subscribe to their tokens, then
    process messages until the connection drops.

    Market discovery runs in _market_refresh_loop (background task) so the
    recv loop is never blocked during the exchange API HTTP poll.

    The recv() timeout is 30s with continue rather than break: ping_interval=20
    / ping_timeout=10 already detects dead connections via WebSocket ping/pong,
    so a recv timeout just means no market messages arrived — normal between
    5-minute candles. We only reconnect if all tracked markets have expired.
    """
    markets = await api.get_markets(session)
    if not markets:
        logger.warning("Aucun marche — attente 30s")
        await asyncio.sleep(30)
        return

    new_ids = []
    for m in markets:
        new_ids.extend(register_market(state, m))

    # Include all non-expired tokens already in state, not just newly discovered.
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

    async with websockets.connect(api.WS_URL, ping_interval=20, ping_timeout=10) as ws:
        for i in range(0, len(all_token_ids), api.WS_BATCH_SIZE):
            await ws.send(api.make_subscribe_msg(all_token_ids[i:i + api.WS_BATCH_SIZE]))
        ld = time.time()
        logger.info("WebSocket connecte")
        refresh_task = asyncio.create_task(_market_refresh_loop(state, session, ws))

        try:
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    if not any(not t.market_ended for t in state.tokens.values()):
                        logger.warning("Aucun token actif — reconnexion")
                        break
                    continue
                except Exception:
                    break

                now = time.time()
                if now - ld >= state.config.dashboard_interval:
                    ld = now
                    print_dashboard(state)

                try:
                    msgs = json.loads(raw)
                    if isinstance(msgs, dict): msgs = [msgs]
                except Exception:
                    continue

                for msg in msgs:
                    p = api.parse_book_update(msg)
                    if p: await handle_book_update(state, p)
        finally:
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass


# ─── STARTUP ─────────────────────────────────────────────────────────────────

def restore_state_from_db(state: BotState) -> None:
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
        state.signalled.add(mid)
    if rows: logger.info("Restaure %d trade(s)", len(rows))

    # Mark recently resolved markets as signalled to prevent re-entry if the
    # bot restarts within the same 5-minute market window (e.g. a restart 30s
    # after resolution could see the price still at the signal level).
    now_ms = int(time.time() * 1000)
    recent_rows = state.conn.execute(
        "SELECT DISTINCT market_id FROM trades "
        "WHERE resolved=1 AND signal_ts_ms>=?",
        (now_ms - 600_000,)   # 10 minutes back
    ).fetchall()
    for (mid,) in recent_rows:
        state.signalled.add(mid)
    if recent_rows:
        logger.info("Signalles recents : %d marche(s) exclus du re-signal", len(recent_rows))

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
    state.capital = state.config.capital_start + state.total_pnl

    # Initialize daily PnL cache from DB so the in-memory counter starts
    # accurate after a restart, not at zero.
    today_ms = bot_utils._today_ms_utc()
    daily_row = state.conn.execute(
        "SELECT COALESCE(SUM(pnl_net),0) FROM trades "
        "WHERE resolved=1 AND signal_ts_ms>=?",
        (today_ms,)
    ).fetchone()
    state.daily_pnl = daily_row[0] or 0.0
    state._daily_pnl_day = int(time.time() // 86400)

    logger.info("State : capital=$%.2f | %d trades | WR=%.1f%% | daily_pnl=$%+.2f",
                state.capital, row[0], state.win_rate, state.daily_pnl)

async def main() -> None:
    args   = _parse_args()
    config = make_config(
        simulate=args.simulate,
        no_log=args.no_log,
        no_snapshots=args.no_snapshots,
        snapshot_interval=args.snapshot_interval,
    )
    _setup_logging(config)

    _up = int(time.time() - _BOT_START)
    _start_str = datetime.fromtimestamp(_BOT_START, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    logger.info("=" * 65)
    if config.simulate:
        logger.warning("  MODE SIMULATION — donnees isolees dans %s", config.install_dir)
    logger.info(
        "  LIVE BOT v0.41 | start=%s UTC | up %dh%02dm%02ds"
        " | seuil=%.2f mise=$%.0f minAskVol=%.0f",
        _start_str, _up // 3600, (_up % 3600) // 60, _up % 60,
        config.signal_threshold, config.stake, config.min_ask_vol,
    )
    if config.strategy_loaded:
        logger.info("  Strategie : %s", os.path.basename(config.strategy_loaded))
    else:
        logger.warning("  Strategie : fichier absent — parametres par defaut")
    if config.hour_filter_enabled:
        _wd = " ".join(f"{s}-{e}h" for s, e in config.weekday_utc_ranges) or "toutes heures"
        _we = " ".join(f"{s}-{e}h" for s, e in config.weekend_utc_ranges) or "bloque"
        _mo = " ouv.lun=13h30" if config.us_weekly_open else ""
        _fr = " ferm.ven=20h00" if config.us_weekly_close else ""
        logger.info("  Filtre horaire : sem=%s | we=%s%s%s", _wd, _we, _mo, _fr)
    if not config.private_key:
        logger.warning("  POLY_PRIVATE_KEY non definie — ordres SIMULES")
    logger.info("=" * 65)
    conn = init_db(config)
    state = BotState(conn, config)
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
    finally:
        # Flush the async log queue before exit. Without this, records still
        # sitting in the queue when the process ends would be silently dropped.
        if _log_listener:
            _log_listener.stop()
