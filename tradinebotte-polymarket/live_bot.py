#!/usr/bin/env python3
# pylint: disable=too-many-lines  # single-file async state machine by design
"""
POLYMARKET LIVE BOT v0.41 — BTC Up/Down 5-minute markets

Strategy:
  Watch active "Bitcoin Up or Down — 5 minutes" markets via the Polymarket
  WebSocket order book feed. When a token's best_bid reaches 0.95, the market
  is pricing that outcome at 95% probability — statistically near-certain with
  ~30 seconds until resolution. We buy at best_ask and wait for settlement.

  Signal:     best_bid >= 0.95  (token priced at 95 cents, pays $1 on win)
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

import argparse, asyncio, copy, json, logging, logging.handlers, math, os, queue, sqlite3, sys, time, uuid
from collections import deque
from dataclasses import dataclass, field
import functools
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
import aiohttp, websockets

# process start epoch — used for uptime display; harmless to capture at import
_BOT_START: float = time.time()

# sys.path insert finds api_polymarket.py in the same directory as this file,
# whether running standalone from INSTALL_DIR or imported as tradinebotte-polymarket.live_bot
# from the project root. To target a different exchange, replace the import below.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
# connectors/ lives in tradinebotte-cex/ in the monorepo (flat deploy: same dir)
_cex_dir = os.path.join(_THIS_DIR, "..", "tradinebotte-cex")
if os.path.isdir(_cex_dir) and _cex_dir not in sys.path:
    sys.path.insert(0, os.path.normpath(_cex_dir))
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
OBI_REJECT_THRESH  = -0.40  # calibrated 2026-05-30 on 874 15M live trades: MaxDD $10 vs $68, Sharpe 4.30 vs 2.23
DAILY_STOP_LOSS    = 30.0
API_FAIL_THRESHOLD        = 3           # consecutive CLOB failures before circuit-breaker trips
API_COOLDOWN_SECS         = 300         # seconds to suspend entries after circuit-breaker
RECENT_TRADE_LOOKBACK_MS  = 600_000     # ms back to exclude recently-resolved markets on restart

# ─── HOUR FILTER DEFAULTS ─────────────────────────────────────────────────────
HOUR_FILTER_ENABLED:    bool                  = False
US_HOLIDAY_FILTER:      bool                  = False
WEEKDAY_UTC_RANGES:     list[tuple[int, int]] = []
WEEKEND_UTC_RANGES:     list[tuple[int, int]] = []
US_WEEKLY_OPEN:         bool                  = True
US_WEEKLY_CLOSE:        bool                  = True

# ─── TIMING / SNAPSHOT DEFAULTS ───────────────────────────────────────────────
SNAPSHOT_INTERVAL       = 1
SNAPSHOT_COMMIT_SECS    = 30   # batch snapshot commits: one flush every 30 s
DASHBOARD_INTERVAL      = 300
MARKET_REFRESH          = 30

# ─── CONNECTOR / STRATEGY DEFAULTS ────────────────────────────────────────────
CONNECTOR      = "polymarket"   # api_* module to use; see bot/connectors/
STRATEGY_TYPE  = "threshold"    # "threshold" (built-in) or "grid" (bot/strategy_engines/grid.py)

# Grid strategy defaults (ignored when strategy_type == "threshold")
GRID_SYMBOL          = "BTCUSDT"
GRID_LOWER           = 0.0
GRID_UPPER           = 0.0
GRID_LEVELS          = 10
GRID_ORDER_SIZE_USDT = 50.0
GRID_TRAIL_MODE      = "static"   # "static" | "bull" | "bear"

# ─── VOLATILITY FILTER DEFAULTS ───────────────────────────────────────────────
VOL_FILTER_ENABLED      = True
VOL_FILTER_WEEKDAY_ONLY = True
VOL_WINDOW              = 12
VOL_MIN_SAMPLES         = 6
VOL_BID_MAX             = 0.07
RANGE_BID_MAX           = 0.30
OBI_VOL_MAX             = 0.40

# ─── MARKET DISCOVERY DEFAULTS ───────────────────────────────────────────────
MARKET_TAG_ID    = 102892   # Polymarket tag: 102892=5M, 102467=15M
MARKET_WINDOW_MINS = 6      # ±N-minute temporal window for Gamma API queries

# ─── STAKE SCALING DEFAULTS ───────────────────────────────────────────────────
# Bid×secs dynamic sizing (Phase 3, 2026-05-16).
# Set bid_alpha=0 AND secs_alpha=0 to disable (flat stake = STAKE).
STAKE_BID_ALPHA:       float = 0.0   # boost per unit of bid confidence above threshold
STAKE_SECS_REF:        float = 45.0  # safe zone (s): no secs penalty below this
STAKE_SECS_ALPHA:      float = 0.0   # penalty intensity for secs > secs_ref
STAKE_MAX:             float = 10.0  # absolute stake cap; overridden by JSON
STAKE_MAX_PCT_CAPITAL: float = 0.0   # 0 = off; 0.12 = cap at 12 % of current capital
# Floor multiplier applied by compute_stake(); not exposed in JSON.
_STAKE_SECS_MIN_FACTOR: float = 0.3

# ─── WEEKLY STOP-LOSS DEFAULT ─────────────────────────────────────────────────
WEEKLY_STOP_LOSS: float = 0.0   # 0 = disabled; e.g. 60.0 = halt after −$60/week

# ─── KELLY CRITERION DEFAULTS ─────────────────────────────────────────────────
# Set kelly_fraction=0 to disable (flat or bid×secs stake used instead).
# Typical values: 0.25 (quarter-Kelly) to 0.5 (half-Kelly).
# Bootstrap gate: flat stake is used until kelly_min_trades resolved trades exist.
KELLY_FRACTION:   float = 0.0   # 0 = disabled
KELLY_MIN_TRADES: int   = 30    # minimum resolved trades before Kelly activates

# ─── STAKE STEP-FUNCTION DEFAULTS ────────────────────────────────────────────
# Curve B: stake by secs zone. Non-increasing: s0 ≥ s1 ≥ s2 ≥ s3.
# Best Sharpe config (backtest 2026-05-19): $15/$12/$6/$6 with vol=weekday.
STAKE_STEP_ENABLED: bool  = False
STAKE_STEP_S0:      float = 15.0   # stake when secs_remaining < 45
STAKE_STEP_S1:      float = 12.0   # stake when 45 ≤ secs_remaining < 60
STAKE_STEP_S2:      float =  6.0   # stake when 60 ≤ secs_remaining < 90
STAKE_STEP_S3:      float =  6.0   # stake when secs_remaining ≥ 90
_STAKE_STEP_BREAKS: tuple = (45.0, 60.0, 90.0)

# ─── LOGGING FORMATTERS ───────────────────────────────────────────────────────
_SESSION_ID = uuid.uuid4().hex[:8].upper()

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
_LOG_FMT  = "%(asctime)s [%(levelname)s] [%(session)s] %(message)s"
_LOG_DATE = "%Y-%m-%d %H:%M:%S"

class _SessionFilter(logging.Filter):
    """Inject session ID into every log record without changing call sites."""
    def filter(self, record: logging.LogRecord) -> bool:
        """Attach the module-level _SESSION_ID to the record and allow it through."""
        record.session = _SESSION_ID  # type: ignore[attr-defined]
        return True

class _PlainFmt(logging.Formatter):
    """File formatter: no colors, fixed-width level abbreviation."""
    def format(self, record: logging.LogRecord) -> str:
        """Replace the full level name with a 5-char abbreviation before formatting."""
        r = copy.copy(record)
        r.levelname = _LEVEL_ABBR.get(r.levelname, r.levelname[:5])
        return super().format(r)

class _ColorFmt(logging.Formatter):
    """Stdout formatter: same as _PlainFmt plus ANSI color on WARN/ERROR."""
    def format(self, record: logging.LogRecord) -> str:
        """Apply abbreviated level name and ANSI escape codes for WARNING/ERROR/CRITICAL."""
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

    # Circuit-breaker
    api_fail_threshold:       int = API_FAIL_THRESHOLD
    api_cooldown_secs:        int = API_COOLDOWN_SECS
    recent_trade_lookback_ms: int = RECENT_TRADE_LOOKBACK_MS

    # Hour filter
    hour_filter_enabled:  bool = HOUR_FILTER_ENABLED
    us_holiday_filter:    bool = US_HOLIDAY_FILTER
    weekday_utc_ranges:   list = field(default_factory=list)
    weekend_utc_ranges:   list = field(default_factory=list)
    us_weekly_open:       bool = US_WEEKLY_OPEN
    us_weekly_close:      bool = US_WEEKLY_CLOSE

    # Market discovery
    market_tag_id:      int = MARKET_TAG_ID
    market_window_mins: int = MARKET_WINDOW_MINS

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

    # Stake scaling (bid×secs dynamic sizing)
    stake_bid_alpha:       float = STAKE_BID_ALPHA
    stake_secs_ref:        float = STAKE_SECS_REF
    stake_secs_alpha:      float = STAKE_SECS_ALPHA
    stake_max:             float = STAKE_MAX
    stake_max_pct_capital: float = STAKE_MAX_PCT_CAPITAL

    # Kelly criterion stake sizing
    kelly_fraction:   float = KELLY_FRACTION
    kelly_min_trades: int   = KELLY_MIN_TRADES

    # Step-function stake sizing (Curve B)
    stake_step_enabled: bool  = STAKE_STEP_ENABLED
    stake_step_s0:      float = STAKE_STEP_S0
    stake_step_s1:      float = STAKE_STEP_S1
    stake_step_s2:      float = STAKE_STEP_S2
    stake_step_s3:      float = STAKE_STEP_S3

    # Weekly stop-loss
    weekly_stop_loss: float = WEEKLY_STOP_LOSS

    # ZeroMQ feed address (account_bot / indicators only)
    feed_addr: str = "tcp://127.0.0.1:5557"
    # When False, account_bot will not auto-start feed.py; it exits if the feed
    # is unreachable. Set to false when feed is managed by systemd.
    feed_auto_start: bool = True

    # Shared indicators service (optional).
    # indicators_addr: PUB socket to subscribe for published indicator messages.
    # indicators_reg_addr: REP socket to register streams dynamically at startup.
    # indicators_streams: list of subscribe requests sent at account_bot startup.
    indicators_addr:     str  = "tcp://127.0.0.1:5559"
    indicators_reg_addr: str  = "tcp://127.0.0.1:5561"
    indicators_streams:  list = field(default_factory=list)

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

    # Connector + strategy type
    connector:     str = CONNECTOR
    strategy_type: str = STRATEGY_TYPE

    # Grid strategy parameters (read only when strategy_type == "grid")
    grid_symbol:          str   = GRID_SYMBOL
    grid_lower:           float = GRID_LOWER
    grid_upper:           float = GRID_UPPER
    grid_levels:          int   = GRID_LEVELS
    grid_order_size_usdt: float = GRID_ORDER_SIZE_USDT
    grid_trail_mode:      str   = GRID_TRAIL_MODE

    # Raw strategy JSON — available to all strategy engines that need
    # params not explicitly declared above (e.g. SwingStrategy).
    strategy_cfg:         dict  = field(default_factory=dict)

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
                         os.path.join(install_dir, "strategies",
                                      "polymarket_BTC5M_piste3.json"))
    strat_path = os.path.expanduser(strat_path)
    if not os.path.isabs(strat_path):
        strat_path = os.path.join(install_dir, strat_path)
    strat = load_strategy(strat_path) or {}
    strategy_loaded = strat_path if strat else None

    # Connector and strategy type (fall back to module-level defaults).
    connector     = str(strat.get("connector",     CONNECTOR))
    strategy_type = str(strat.get("strategy_type", STRATEGY_TYPE))

    # Grid parameters (only used when strategy_type == "grid").
    grid_symbol          = str(strat.get("grid_symbol",          GRID_SYMBOL))
    grid_lower           = float(strat.get("grid_lower",           GRID_LOWER))
    grid_upper           = float(strat.get("grid_upper",           GRID_UPPER))
    grid_levels          = int(strat.get("grid_levels",           GRID_LEVELS))
    grid_order_size_usdt = float(strat.get("grid_order_size_usdt", GRID_ORDER_SIZE_USDT))
    grid_trail_mode      = str(strat.get("trail_mode",             GRID_TRAIL_MODE))

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
    api_fail_threshold       = int(strat.get("api_fail_threshold",       API_FAIL_THRESHOLD))
    api_cooldown_secs        = int(strat.get("api_cooldown_secs",        API_COOLDOWN_SECS))
    recent_trade_lookback_ms = int(strat.get("recent_trade_lookback_ms", RECENT_TRADE_LOOKBACK_MS))

    # Stake scaling — default stake_max to base stake so flat mode is unchanged.
    stake_bid_alpha       = float(strat.get("stake_bid_alpha",       STAKE_BID_ALPHA))
    stake_secs_ref        = float(strat.get("stake_secs_ref",        STAKE_SECS_REF))
    stake_secs_alpha      = float(strat.get("stake_secs_alpha",      STAKE_SECS_ALPHA))
    stake_max             = float(strat.get("stake_max",             stake))  # default = base
    stake_max_pct_capital = float(strat.get("stake_max_pct_capital", STAKE_MAX_PCT_CAPITAL))

    weekly_stop_loss = float(strat.get("weekly_stop_loss", WEEKLY_STOP_LOSS))

    kelly_fraction   = float(strat.get("kelly_fraction",   KELLY_FRACTION))
    kelly_min_trades = int(strat.get("kelly_min_trades",   KELLY_MIN_TRADES))

    # Vol-filter parameters (JSON section "vol_filter": {…}).
    _vf = strat.get("vol_filter", {})
    vol_filter_enabled      = bool(_vf.get("enabled",      VOL_FILTER_ENABLED))
    vol_filter_weekday_only = bool(_vf.get("weekday_only", VOL_FILTER_WEEKDAY_ONLY))
    vol_window              = int(_vf.get("window",        VOL_WINDOW))
    vol_min_samples         = int(_vf.get("min_samples",   VOL_MIN_SAMPLES))
    vol_bid_max_cfg         = float(_vf.get("vol_bid_max",   VOL_BID_MAX))
    range_bid_max_cfg       = float(_vf.get("range_bid_max", RANGE_BID_MAX))
    obi_vol_max_cfg         = float(_vf.get("obi_vol_max",   OBI_VOL_MAX))

    # Step-function stake parameters (JSON section "stake_step": {…}).
    _ss = strat.get("stake_step", {})
    stake_step_enabled = bool(_ss.get("enabled", STAKE_STEP_ENABLED))
    stake_step_s0      = float(_ss.get("s0",     STAKE_STEP_S0))
    stake_step_s1      = float(_ss.get("s1",     STAKE_STEP_S1))
    stake_step_s2      = float(_ss.get("s2",     STAKE_STEP_S2))
    stake_step_s3      = float(_ss.get("s3",     STAKE_STEP_S3))

    market_tag_id      = int(strat.get("market_tag_id",      MARKET_TAG_ID))
    market_window_mins = int(strat.get("market_window_mins", MARKET_WINDOW_MINS))

    _hf = strat.get("hour_filter", {})
    hour_filter_enabled = bool(_hf.get("enabled", False))
    weekday_utc_ranges  = [tuple(r) for r in _hf.get("weekday_utc_ranges", [])]
    weekend_utc_ranges  = [tuple(r) for r in _hf.get("weekend_utc_ranges", [])]
    us_weekly_open      = bool(_hf.get("us_weekly_open",    True))
    us_weekly_close     = bool(_hf.get("us_weekly_close",   True))
    us_holiday_filter   = bool(_hf.get("us_holiday_filter", US_HOLIDAY_FILTER))

    # Feed address: config.json first, env var as fallback.
    _port_base = int(os.environ.get("TRADINEBOTTE_PORT_BASE", "5557"))
    feed_addr = cfg.get(
        "feed_addr",
        os.environ.get("TRADINEBOTTE_FEED_ADDR", f"tcp://127.0.0.1:{_port_base}"),
    )
    feed_auto_start = bool(cfg.get("feed_auto_start", True))

    # Indicators service: config.json first, env vars as fallback.
    indicators_addr = cfg.get(
        "indicators_addr",
        os.environ.get("TRADINEBOTTE_INDICATORS_ADDR", f"tcp://127.0.0.1:{_port_base + 2}"),
    )
    indicators_reg_addr = cfg.get(
        "indicators_reg_addr",
        os.environ.get("TRADINEBOTTE_INDICATORS_REG_ADDR", f"tcp://127.0.0.1:{_port_base + 4}"),
    )
    indicators_streams = cfg.get("indicators_streams", [])

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
    for _fpath in (log_path, db_path):
        open(_fpath, "a").close()
        os.chmod(_fpath, 0o640)

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
        api_fail_threshold=api_fail_threshold,
        api_cooldown_secs=api_cooldown_secs,
        recent_trade_lookback_ms=recent_trade_lookback_ms,
        hour_filter_enabled=hour_filter_enabled,
        us_holiday_filter=us_holiday_filter,
        weekday_utc_ranges=weekday_utc_ranges,
        weekend_utc_ranges=weekend_utc_ranges,
        us_weekly_open=us_weekly_open,
        us_weekly_close=us_weekly_close,
        enable_snapshots=not no_snapshots,
        feed_addr=feed_addr,
        feed_auto_start=feed_auto_start,
        indicators_addr=indicators_addr,
        indicators_reg_addr=indicators_reg_addr,
        indicators_streams=indicators_streams,
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
        connector=connector,
        strategy_type=strategy_type,
        grid_symbol=grid_symbol,
        grid_lower=grid_lower,
        grid_upper=grid_upper,
        grid_levels=grid_levels,
        grid_order_size_usdt=grid_order_size_usdt,
        grid_trail_mode=grid_trail_mode,
        strategy_cfg=strat,
        stake_bid_alpha=stake_bid_alpha,
        stake_secs_ref=stake_secs_ref,
        stake_secs_alpha=stake_secs_alpha,
        stake_max=stake_max,
        stake_max_pct_capital=stake_max_pct_capital,
        kelly_fraction=kelly_fraction,
        kelly_min_trades=kelly_min_trades,
        stake_step_enabled=stake_step_enabled,
        stake_step_s0=stake_step_s0,
        stake_step_s1=stake_step_s1,
        stake_step_s2=stake_step_s2,
        stake_step_s3=stake_step_s3,
        vol_filter_enabled=vol_filter_enabled,
        vol_filter_weekday_only=vol_filter_weekday_only,
        vol_window=vol_window,
        vol_min_samples=vol_min_samples,
        vol_bid_max=vol_bid_max_cfg,
        range_bid_max=range_bid_max_cfg,
        obi_vol_max=obi_vol_max_cfg,
        weekly_stop_loss=weekly_stop_loss,
        market_tag_id=market_tag_id,
        market_window_mins=market_window_mins,
    )

    # Sync display config to bot_utils so its functions read the correct values.
    bot_utils.WEBSTATUS_ENABLED  = webstatus_enabled
    bot_utils.WEBSTATUS_PATH     = webstatus_path
    bot_utils.WEBSTATUS_USER     = webstatus_user
    bot_utils.WEBSTATUS_PASSWORD = webstatus_password
    bot_utils.INSTALL_DIR        = install_dir

    return config


def _load_connector(name: str) -> None:
    """
    Replace the module-level `api` global with the requested exchange connector.
    No-op when name == "polymarket" (default import already in place).
    """
    global api  # pylint: disable=global-statement
    if name == "polymarket":
        return
    from connectors import load as _load
    api = _load(name)
    logger.info("Connector loaded: %s (%s)", name, api.__name__)


def _setup_logging(config: "BotConfig") -> Optional[logging.handlers.QueueListener]:
    """
    Configure the root logger according to config and return the QueueListener
    (or None when --no-log is active without --simulate).

    Async logging: logger.info() enqueues a LogRecord nanoseconds; the
    QueueListener daemon thread calls FileHandler.emit() — the only disk I/O.
    """
    global _log_listener  # pylint: disable=global-statement

    _sf = _SessionFilter()
    if config.no_log:
        if config.simulate:
            _h = logging.StreamHandler(sys.stdout)
            _h.setFormatter(_ColorFmt(_LOG_FMT, datefmt=_LOG_DATE))
            _h.addFilter(_sf)
            _log_handlers: list[logging.Handler] = [_h]
        else:
            _log_handlers = [logging.NullHandler()]
        listener = None
    else:
        _log_queue: queue.Queue[logging.LogRecord] = queue.Queue()
        _file_handler = logging.handlers.TimedRotatingFileHandler(
            config.log_path, when="midnight", backupCount=30, encoding="utf-8")
        _file_handler.setFormatter(_PlainFmt(_LOG_FMT, datefmt=_LOG_DATE))
        _file_handler.addFilter(_sf)
        listener = logging.handlers.QueueListener(
            _log_queue, _file_handler, respect_handler_level=True)
        listener.start()
        _qh = logging.handlers.QueueHandler(_log_queue)
        # Passthrough formatter prevents double-formatting (raw message only).
        _qh.setFormatter(logging.Formatter("%(message)s"))
        _qh.addFilter(_sf)
        _log_handlers = [_qh]
        if config.simulate:
            _h = logging.StreamHandler(sys.stdout)
            _h.setFormatter(_ColorFmt(_LOG_FMT, datefmt=_LOG_DATE))
            _h.addFilter(_sf)
            _log_handlers.append(_h)

    logging.basicConfig(
        level=logging.INFO,
        format=_LOG_FMT,
        datefmt=_LOG_DATE,
        handlers=_log_handlers,
        force=True,
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
    2: """
CREATE TABLE IF NOT EXISTS grid_state (
    symbol           TEXT    PRIMARY KEY,
    grid_lower       REAL    NOT NULL,
    grid_upper       REAL    NOT NULL,
    grid_step        REAL    NOT NULL,
    order_size_usdt  REAL    NOT NULL,
    total_cycles     INTEGER NOT NULL DEFAULT 0,
    total_profit_usd REAL    NOT NULL DEFAULT 0.0,
    initialised      INTEGER NOT NULL DEFAULT 0,
    halted           INTEGER NOT NULL DEFAULT 0,
    updated_at       REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS grid_levels (
    symbol        TEXT NOT NULL,
    level_price   REAL NOT NULL,
    buy_order_id  TEXT,
    sell_order_id TEXT,
    buy_price     REAL,
    sell_price    REAL,
    status        TEXT NOT NULL DEFAULT 'idle',
    filled_at_ts  REAL,
    updated_at    REAL NOT NULL,
    PRIMARY KEY (symbol, level_price)
);
""",
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
        logger.info("DB schema migration v%d applied", version)


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
        conn.execute("PRAGMA mmap_size = ?", (config.db_mmap_mb * 1024 * 1024,))
        logger.info("DB mmap enabled: %d MB", config.db_mmap_mb)
    conn.commit()
    logger.info("DB initialized: %s", config.db_path)
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


@dataclass
class RejectionStats:
    """Counts of check_signal() early-exits per reason, logged every 60 s."""
    signalled:     int = 0
    market_ended:  int = 0
    trading_hour:  int = 0
    best_bid:      int = 0
    entry_max:     int = 0
    best_ask:      int = 0
    ask_vol:       int = 0
    secs_remaining: int = 0
    obi:           int = 0
    vol_filter:    int = 0
    capital:       int = 0
    daily_stop:    int = 0
    weekly_stop:   int = 0
    api_cooldown:  int = 0


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
        # Weekly PnL cache — same pattern; resets every 7-day period.
        self.weekly_pnl: float = 0.0
        self._weekly_pnl_week: int = -1  # 7-day period index since Unix epoch
        # Batched snapshot commits — flushed every SNAPSHOT_COMMIT_SECS seconds.
        self.last_snapshot_commit_ts: float = 0.0
        # Circuit-breaker: suspend new entries after 3 consecutive CLOB failures.
        self.api_fail_streak: int = 0
        self.api_cooldown_until: float = 0.0
        # Active strategy instance (None = built-in threshold strategy).
        # Set in main() after make_config() when strategy_type != "threshold".
        self.strategy: Any = None
        # Rejection counters — reset and logged every 60 s.
        self.rejection_stats = RejectionStats()
        self._last_stats_log: float = time.time()

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
    # Dispatch to the active strategy.
    # state.strategy is None for the built-in threshold strategy (default),
    # or a GridStrategy/SwingStrategy instance for "grid"/"swing".
    if state.strategy is not None:
        await state.strategy.on_book_update(state, ts, _t_ws=t_ws)
    else:
        # Default: Polymarket threshold strategy (check_signal / check_resolution).
        await check_signal(state, ts, _t_ws=t_ws)
        check_resolution(state, ts)
    now = time.time()
    if now - ts.last_snapshot_ts >= state.config.snapshot_interval:
        ts.bid_history.append(ts.best_bid)
        ts.obi_history.append(ts.obi)
        if state.config.enable_snapshots:
            save_snapshot(state, ts)
            if now - state.last_snapshot_commit_ts >= SNAPSHOT_COMMIT_SECS:
                state.conn.commit()
                state.last_snapshot_commit_ts = now
        ts.last_snapshot_ts = now


# ─── STAKE SCALING ────────────────────────────────────────────────────────────

def compute_stake(cfg: "BotConfig", bid: float, secs: float,
                  capital: float = 0.0, win_rate: float = 0.5,
                  n_trades: int = 0, ask: float = 0.0) -> float:
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
        b_net = (1.0 / ask - 1.0) - api.FEE_RATE * min(ask, 1.0 - ask) / ask
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


# ─── SIGNAL & TRADE LOGIC ─────────────────────────────────────────────────────

@functools.lru_cache(maxsize=4)
def _us_holidays(year: int) -> frozenset:
    """Return the frozenset of US federal holiday *observed* dates for `year`.

    Covers the 10 NYSE-recognised holidays.  Saturday holidays shift to Friday;
    Sunday holidays shift to Monday.  Results are cached per year (lru_cache).
    """
    def _observed(d: date) -> date:
        if d.weekday() == 5: return d - timedelta(days=1)   # Sat → Fri
        if d.weekday() == 6: return d + timedelta(days=1)   # Sun → Mon
        return d

    def _nth_weekday(y: int, m: int, wd: int, n: int) -> date:
        first = date(y, m, 1)
        delta = (wd - first.weekday()) % 7
        return first.replace(day=1 + delta + (n - 1) * 7)

    def _last_monday(y: int, m: int) -> date:
        for day in range(31, 21, -1):
            try:
                d = date(y, m, day)
                if d.weekday() == 0:
                    return d
            except ValueError:
                continue
        raise ValueError  # pragma: no cover

    def _easter(y: int) -> date:
        a, b, c = y % 19, y // 100, y % 100
        d, e, f = b // 4, b % 4, (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i, k = c // 4, c % 4
        ll = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * ll) // 451
        mo = (h + ll - 7 * m + 114) // 31
        dy = (h + ll - 7 * m + 114) % 31 + 1
        return date(y, mo, dy)

    mon, thu = 0, 3
    return frozenset([
        _observed(date(year, 1,  1)),               # New Year's Day
        _nth_weekday(year, 1, mon, 3),              # MLK Day (3rd Mon Jan)
        _nth_weekday(year, 2, mon, 3),              # Presidents' Day (3rd Mon Feb)
        _easter(year) - timedelta(days=2),          # Good Friday
        _last_monday(year, 5),                      # Memorial Day (last Mon May)
        _observed(date(year, 6, 19)),               # Juneteenth
        _observed(date(year, 7,  4)),               # Independence Day
        _nth_weekday(year, 9, mon, 1),              # Labor Day (1st Mon Sep)
        _nth_weekday(year, 11, thu, 4),             # Thanksgiving (4th Thu Nov)
        _observed(date(year, 12, 25)),              # Christmas Day
    ])


def _is_us_holiday(dt: datetime) -> bool:
    """Return True if `dt` (UTC) falls on a US federal holiday (observed date)."""
    return dt.date() in _us_holidays(dt.year)


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
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms is not None \
         else datetime.now(timezone.utc)
    if config.us_holiday_filter and _is_us_holiday(dt):
        return False
    if not config.hour_filter_enabled:
        return True
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
    if state.capital - len(state.open_trades) * cfg.stake < cfg.stake:
        state.rejection_stats.capital += 1; return
    if state.daily_pnl < -cfg.daily_stop_loss: state.rejection_stats.daily_stop += 1; return
    if cfg.weekly_stop_loss > 0 and state.weekly_pnl < -cfg.weekly_stop_loss:
        state.rejection_stats.weekly_stop += 1; return
    if time.time() < state.api_cooldown_until: state.rejection_stats.api_cooldown += 1; return

    state.signalled.add(ts.market_id)
    await enter_live_trade(state, ts, _t_ws=_t_ws)

async def enter_live_trade(state: BotState, ts: TokenState, _t_ws: Optional[float] = None) -> None:
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
    fee   = api.compute_fee(ep, tb)
    cost  = stake + fee
    oid   = None

    # Latency measurement — point A: everything from WS message receipt up to
    # this point (token update, all signal guards, daily PnL query, fee calc).
    t_signal_ms = (time.monotonic() - _t_ws) * 1000 if _t_ws is not None else None

    # Latency measurement — point B: bracket only the CLOB API HTTP call so we
    # can isolate network RTT from in-process signal latency.
    t_pre_order = time.monotonic()
    if state.session:
        oid = await api.post_order(
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
    write_web_status(state)

def save_snapshot(state: BotState, ts: TokenState) -> None:
    """Insert a snapshot row without committing — caller batches commits via handle_book_update."""
    state.conn.execute(
        "INSERT INTO snapshots (ts_ms, market_id, token_id, direction, "
        "secs_remaining, best_bid, best_ask, spread, ask_vol, obi, has_open_trade) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (int(time.time() * 1000), ts.market_id, ts.token_id, ts.direction,
         ts.secs_remaining, ts.best_bid, ts.best_ask, ts.spread, ts.ask_vol, ts.obi,
         1 if ts.market_id in state.open_trades else 0)
    )

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
            logger.warning("WS error — reconnecting in %ds: %s", backoff, e)
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
            nm = await api.get_markets(
                session,
                tag_id=state.config.market_tag_id,
                window_minutes=state.config.market_window_mins,
            )
            ni = []
            for m in nm:
                ni.extend(register_market(state, m))
            if ni:
                for i in range(0, len(ni), api.WS_BATCH_SIZE):
                    await ws.send(api.make_subscribe_msg(ni[i:i + api.WS_BATCH_SIZE]))
                logger.info("New tokens: %d", len(ni))

            n = purge_expired_markets(state)
            if n:
                logger.info("Expired tokens purged: %d", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Market refresh error: %s", e)


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
    markets = await api.get_markets(
        session,
        tag_id=state.config.market_tag_id,
        window_minutes=state.config.market_window_mins,
    )
    if not markets:
        logger.warning("No markets — waiting 30s")
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
        logger.warning("No active tokens — waiting 30s")
        await asyncio.sleep(30)
        return

    state.session = session
    logger.info("Subscribing to %d tokens...", len(all_token_ids))

    async with websockets.connect(api.WS_URL, ping_interval=20, ping_timeout=10) as ws:
        for i in range(0, len(all_token_ids), api.WS_BATCH_SIZE):
            await ws.send(api.make_subscribe_msg(all_token_ids[i:i + api.WS_BATCH_SIZE]))
        ld = time.time()
        logger.info("WebSocket connected")
        refresh_task = asyncio.create_task(_market_refresh_loop(state, session, ws))

        try:
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    if not any(not t.market_ended for t in state.tokens.values()):
                        logger.warning("No active tokens — reconnecting")
                        break
                    continue
                except Exception as _exc:
                    logger.warning("Unexpected WS recv exception", exc_info=True)
                    break

                now = time.time()
                if now - ld >= state.config.dashboard_interval:
                    ld = now
                    print_dashboard(state)

                try:
                    msgs = json.loads(raw)
                    if isinstance(msgs, dict): msgs = [msgs]
                except Exception as _json_exc:
                    logger.debug("JSON parse failed: %s | raw=%r", _json_exc, raw[:200])
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
    if rows: logger.info("Restored %d open trade(s)", len(rows))

    # Mark recently resolved markets as signalled to prevent re-entry if the
    # bot restarts within the same 5-minute market window (e.g. a restart 30s
    # after resolution could see the price still at the signal level).
    now_ms = int(time.time() * 1000)
    recent_rows = state.conn.execute(
        "SELECT DISTINCT market_id FROM trades "
        "WHERE resolved=1 AND signal_ts_ms>=?",
        (now_ms - state.config.recent_trade_lookback_ms,)
    ).fetchall()
    for (mid,) in recent_rows:
        state.signalled.add(mid)
    if recent_rows:
        logger.info("Recent resolved: %d market(s) excluded from re-signal", len(recent_rows))

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

    # Weekly PnL — Monday UTC boundary; reconstruct from DB on startup.
    _dt_now = datetime.now(timezone.utc)
    _week_mon = (_dt_now - timedelta(days=_dt_now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_start_ms = int(_week_mon.timestamp() * 1000)
    weekly_row = state.conn.execute(
        "SELECT COALESCE(SUM(pnl_net),0) FROM trades "
        "WHERE resolved=1 AND signal_ts_ms>=?",
        (week_start_ms,)
    ).fetchone()
    state.weekly_pnl = weekly_row[0] or 0.0
    state._weekly_pnl_week = (_dt_now - timedelta(days=_dt_now.weekday())).toordinal()

    logger.info(
        "State : capital=$%.2f | %d trades | WR=%.1f%% | daily_pnl=$%+.2f | weekly_pnl=$%+.2f",
        state.capital, row[0], state.win_rate, state.daily_pnl, state.weekly_pnl,
    )

async def main() -> None:
    args   = _parse_args()
    config = make_config(
        simulate=args.simulate,
        no_log=args.no_log,
        no_snapshots=args.no_snapshots,
        snapshot_interval=args.snapshot_interval,
    )
    _setup_logging(config)
    _load_connector(config.connector)
    if config.strategy_type != "threshold":
        from connectors import validate as _validate_conn
        _validate_conn(api, config.strategy_type)

    _up = int(time.time() - _BOT_START)
    _start_str = datetime.fromtimestamp(_BOT_START, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    logger.info("=" * 65)
    if config.simulate:
        logger.warning("  SIMULATION MODE — data isolated in %s", config.install_dir)
    logger.info(
        "  LIVE BOT v0.41 | start=%s UTC | up %dh%02dm%02ds"
        " | thresh=%.2f stake=$%.0f minAskVol=%.0f",
        _start_str, _up // 3600, (_up % 3600) // 60, _up % 60,
        config.signal_threshold, config.stake, config.min_ask_vol,
    )
    if config.strategy_loaded:
        logger.info("  Strategy: %s [%s/%s]",
                    os.path.basename(config.strategy_loaded),
                    config.strategy_type, config.connector)
    else:
        logger.warning("  Strategy: file not found — using defaults")
    if config.connector == "polymarket":
        _tf = "15M" if config.market_tag_id == api.GAMMA_TAG_15M else "5M"
        logger.info("  Markets: BTC Up/Down %s (tag=%d, window=±%dmin)",
                    _tf, config.market_tag_id, config.market_window_mins)
    if config.hour_filter_enabled:
        _wd = " ".join(f"{s}-{e}h" for s, e in config.weekday_utc_ranges) or "all hours"
        _we = " ".join(f"{s}-{e}h" for s, e in config.weekend_utc_ranges) or "blocked"
        _mo = " mon-open=13h30" if config.us_weekly_open else ""
        _fr = " fri-close=20h00" if config.us_weekly_close else ""
        logger.info("  Hour filter: weekday=%s | weekend=%s%s%s", _wd, _we, _mo, _fr)
    if config.vol_filter_enabled:
        _wdo = " (weekday only)" if config.vol_filter_weekday_only else ""
        logger.info("  Vol filter%s: bid_vol≤%.2f  range≤%.2f  obi_vol≤%.2f",
                    _wdo, config.vol_bid_max, config.range_bid_max, config.obi_vol_max)
    if config.stake_step_enabled:
        logger.info("  Stake step: <45s=$%.0f  45-60s=$%.0f  60-90s=$%.0f  ≥90s=$%.0f",
                    config.stake_step_s0, config.stake_step_s1,
                    config.stake_step_s2, config.stake_step_s3)
    if config.stake_bid_alpha != 0.0 or config.stake_secs_alpha != 0.0:
        _pct = (f"  cap={config.stake_max_pct_capital*100:.0f}%capital"
                if config.stake_max_pct_capital > 0 else "")
        logger.info(
            "  Stake scaling: bid_α=%.1f  secs_ref=%.0fs  secs_α=%.2f  max=$%.0f%s",
            config.stake_bid_alpha, config.stake_secs_ref,
            config.stake_secs_alpha, config.stake_max, _pct,
        )
    if config.kelly_fraction > 0:
        logger.info("  Kelly criterion: fraction=%.2f  bootstrap=%d trades",
                    config.kelly_fraction, config.kelly_min_trades)
    if config.weekly_stop_loss > 0:
        logger.info("  Weekly stop-loss: $%.0f", config.weekly_stop_loss)
    if not config.private_key:
        logger.warning("  POLY_PRIVATE_KEY not set — orders SIMULATED")
    logger.info("=" * 65)
    conn = init_db(config)
    try:
        state = BotState(conn, config)
        restore_state_from_db(state)

        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=10),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as session:
            state.session = session   # available before ws_loop for strategy restore
            if config.strategy_type != "threshold":
                from strategy_engines import load as _load_strat
                state.strategy = _load_strat(config.strategy_type, config)
                logger.info("  Algorithm   : %s", config.strategy_type)
                await state.strategy.restore_from_db(state)
            await ws_loop(state, session)
    finally:
        conn.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped.")
    finally:
        # Flush the async log queue before exit. Without this, records still
        # sitting in the queue when the process ends would be silently dropped.
        if _log_listener:
            _log_listener.stop()
