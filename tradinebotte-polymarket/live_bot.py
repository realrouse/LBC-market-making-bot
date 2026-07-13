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

import argparse, asyncio, copy, json, logging, logging.handlers, os, queue, socket, sqlite3, sys, time, uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
import aiohttp

# process start epoch — used for uptime display; harmless to capture at import
_BOT_START: float = time.time()

# sys.path insert finds the connector modules (api_polymarket.py et al.) in the same
# directory as this file, whether running standalone from INSTALL_DIR or imported as
# tradinebotte-polymarket.live_bot from the project root.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
# connectors/ lives in tradinebotte-cex/ in the monorepo (flat deploy: same dir,
# install.sh/update_standalone.sh copy connectors/ into INSTALL_DIR).
_cex_dir = os.path.join(_THIS_DIR, "..", "tradinebotte-cex")
if os.path.isdir(_cex_dir) and _cex_dir not in sys.path:
    sys.path.insert(0, os.path.normpath(_cex_dir))
# botcore/ (neutral core — the Strategy protocol) lives in tradinebotte-core/ in the
# monorepo (flat deploy: same dir, copied into INSTALL_DIR beside connectors/).
_core_dir = os.path.join(_THIS_DIR, "..", "tradinebotte-core")
if os.path.isdir(_core_dir) and _core_dir not in sys.path:
    sys.path.insert(0, os.path.normpath(_core_dir))
from botcore.connectors import load as _load_connector_module
# ── Re-exports (Plan D steps 3b–4b) ───────────────────────────────────────────
# The trading logic, data plane, calendar, and shared helpers moved into the neutral
# core (botcore) and the co-located pm_* / cex plugins. live_bot re-exports them so
# existing `live_bot.<name>` / `bot.<name>` callers (tests, backtests, account_bot) keep
# working unchanged. Several names below are also used directly in this module; the rest
# are pure re-exports — pylint can't tell the two apart, so unused-import is disabled here.
# pylint: disable=unused-import
from botcore.persistence import (
    read_capital_base, write_capital_base, _persist_snapshot, cumulative_pnl, equity,
)
from botcore.schema import apply_base_schema
from pm_types import VOL_WINDOW, TokenState, RejectionStats
from pm_calendar import is_trading_hour, _in_weekend_session, _us_holidays
from pm_strategy import (
    compute_stake, check_signal, enter_live_trade, check_resolution,
    ThresholdStrategy, _STAKE_SECS_MIN_FACTOR,
)
from pm_data import (
    handle_book_update, save_snapshot, purge_expired_markets, register_market,
    _register_market_from_feed, ws_loop, _market_refresh_loop,
)
# pylint: enable=unused-import
import bot_utils
from tradinetools import (
    heartbeat_loop, control_loop, Command, health_server,
    write_reset_marker, consume_reset_marker, resolve_bot_id,
)
from tradinetools.zmq import PORT_FEED, PORT_INDICATORS, PORT_IND_REG, default_ipc_addr

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
# SNAPSHOT_COMMIT_SECS lives in botcore.persistence (re-exported at the top of this module).
DASHBOARD_INTERVAL      = 300
MARKET_REFRESH          = 30

# ─── CONNECTOR / STRATEGY DEFAULTS ────────────────────────────────────────────
CONNECTOR      = "polymarket"   # api_* module to use; see bot/connectors/
STRATEGY_TYPE  = "threshold"    # "threshold" (built-in) or "grid" (bot/strategy_engines/grid.py)

# The exchange connector is no longer a module global (Plan D step 4b): the entrypoint
# (main / account_bot) loads it via the registry by name and injects it into BotState,
# and the trading code reaches it through `state.connector`. The default connector name
# is CONNECTOR above; make_config() may override it per strategy.

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
# VOL_WINDOW lives in pm_types (re-exported at the top of this module).
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
# _STAKE_SECS_MIN_FACTOR / _STAKE_STEP_BREAKS live in pm_strategy (re-exported at the top).

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
    weekday_utc_ranges:   list[tuple[int, int]] = field(default_factory=list)
    weekend_utc_ranges:   list[tuple[int, int]] = field(default_factory=list)
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

    # Market-data source: "ws" = own Polymarket WS (standalone, default);
    # "feed" = consume the shared feed at feed_addr (data plane). Orders are placed
    # by this bot either way (order plane stays per-bot with its own credentials).
    data_source: str = "ws"
    # ZeroMQ feed address (used when data_source == "feed", and by account_bot)
    feed_addr: str = f"tcp://127.0.0.1:{PORT_FEED}"
    # When False, account_bot will not auto-start feed.py; it exits if the feed
    # is unreachable. Set to false when feed is managed by systemd.
    feed_auto_start: bool = True

    # Shared indicators service (optional).
    # indicators_addr: PUB socket to subscribe for published indicator messages.
    # indicators_reg_addr: REP socket to register streams dynamically at startup.
    # indicators_streams: list of subscribe requests sent at account_bot startup.
    indicators_addr:     str  = f"tcp://127.0.0.1:{PORT_INDICATORS}"
    indicators_reg_addr: str  = f"tcp://127.0.0.1:{PORT_IND_REG}"
    indicators_streams:  list = field(default_factory=list)

    # Credentials.
    # repr=False keeps secrets out of the dataclass __repr__ so they cannot leak
    # into logs or tracebacks if a BotConfig instance is ever logged/printed.
    # The values are still plain str (used as-is by eth_account / ClobClient);
    # this is log-masking only, not secure memory zeroing — see TODO L-1.
    private_key:    str = field(default="", repr=False)
    api_key:        str = field(default="", repr=False)
    api_secret:     str = field(default="", repr=False)
    api_passphrase: str = field(default="", repr=False)

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
        _cfg, _db, _log = instance_paths(self.install_dir)   # TRADINEBOTTE_INSTANCE-aware (single-tree)
        if not self.db_path:
            self.db_path = _db
        if not self.log_path:
            self.log_path = _log
        if not self.config_path:
            self.config_path = _cfg
        if not self.webstatus_path:
            self.webstatus_path = os.path.expanduser("~/public_html/tradinebot_status.html")
        if self.no_snapshots:
            self.enable_snapshots = False


# ─── CONFIGURATION HELPERS ───────────────────────────────────────────────────

def instance_paths(install_dir: str) -> tuple[str, str, str]:
    """(config_path, db_path, log_path) under install_dir.

    With TRADINEBOTTE_INSTANCE set (the single-tree layout — several bots share ~/tradinebotte),
    the files are per-instance suffixed (config_<inst>.json / live_<inst>.db / <inst>.log) so two
    bots in one dir never collide on the fixed names. Unset → the legacy config.json / live.db /
    live.log, so a not-yet-migrated bot is byte-for-byte unchanged (this ships inert)."""
    inst = os.environ.get("TRADINEBOTTE_INSTANCE", "").strip()
    if inst:
        return (os.path.join(install_dir, f"config_{inst}.json"),
                os.path.join(install_dir, f"live_{inst}.db"),
                os.path.join(install_dir, f"{inst}.log"))
    return (os.path.join(install_dir, "config.json"),
            os.path.join(install_dir, "live.db"),
            os.path.join(install_dir, "live.log"))


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
    config_path, db_path, log_path = instance_paths(install_dir)   # TRADINEBOTTE_INSTANCE-aware

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

    # Accumulation keeps its own DB file (live_accum.db) so the cutover from the former
    # standalone accumulation_bot preserves deployed holdings/avg_entry/realized (all on the
    # status page). Every other strategy shares live.db. LEGACY layout only — under the single-tree
    # layout (TRADINEBOTTE_INSTANCE set) instance_paths() already gave a per-instance live_<inst>.db,
    # so accumulation gets live_accumulation.db and needs no special case.
    if strategy_type == "accumulation" and not os.environ.get("TRADINEBOTTE_INSTANCE", "").strip():
        db_path = os.path.join(install_dir, "live_accum.db")

    # Grid parameters (only used when strategy_type == "grid").
    grid_symbol          = str(strat.get("grid_symbol",          GRID_SYMBOL))
    grid_lower           = float(strat.get("grid_lower",           GRID_LOWER))
    grid_upper           = float(strat.get("grid_upper",           GRID_UPPER))
    grid_levels          = int(strat.get("grid_levels",           GRID_LEVELS))
    grid_order_size_usdt = float(strat.get("grid_order_size_usdt", GRID_ORDER_SIZE_USDT))
    grid_trail_mode      = str(strat.get("trail_mode",             GRID_TRAIL_MODE))

    # Strategy overrides from JSON (fall back to module-level defaults).
    capital_start      = float(strat.get("capital_start",      CAPITAL_START))
    # Accumulation names its capital "capital_usdt" — map it so the reset default / equity base
    # match the engine's own capital (the engine reads capital_usdt from strategy_cfg).
    if strategy_type == "accumulation":
        capital_start = float(strat.get("capital_usdt", capital_start))
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

    # Feed address: config.json first, env var as fallback, IPC as final default.
    # Set TRADINEBOTTE_PORT_BASE to shift to a TCP-based stack (backward compat).
    _port_base = os.environ.get("TRADINEBOTTE_PORT_BASE")
    feed_addr = cfg.get(
        "feed_addr",
        os.environ.get(
            "TRADINEBOTTE_FEED_ADDR",
            f"tcp://127.0.0.1:{_port_base}" if _port_base
            else default_ipc_addr("tradinebotte-feed"),
        ),
    )
    feed_auto_start = bool(cfg.get("feed_auto_start", True))
    data_source = str(cfg.get("data_source",
                              os.environ.get("TRADINEBOTTE_DATA_SOURCE", "ws"))).lower()

    # Indicators service: config.json first, env vars as fallback, IPC as final default.
    indicators_addr = cfg.get(
        "indicators_addr",
        os.environ.get(
            "TRADINEBOTTE_INDICATORS_ADDR",
            f"tcp://127.0.0.1:{int(_port_base) + 2}" if _port_base
            else default_ipc_addr("tradinebotte-indicators"),
        ),
    )
    indicators_reg_addr = cfg.get(
        "indicators_reg_addr",
        os.environ.get(
            "TRADINEBOTTE_INDICATORS_REG_ADDR",
            f"tcp://127.0.0.1:{int(_port_base) + 4}" if _port_base
            else default_ipc_addr("tradinebotte-ind-reg"),
        ),
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
        with open(_fpath, "a", encoding="utf-8"):
            pass
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
        data_source=data_source,
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

    return config


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
-- schema_version + bot_meta live in botcore.schema (the neutral base, applied first).
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
    # v3: entry price of the BUY a SELL is closing, so a completed BUY→SELL cycle
    # can book its PnL. Without it, GridStrategy never counted a cycle (PnL stuck $0).
    3: "ALTER TABLE grid_levels ADD COLUMN entry_price REAL;\n",
    # v4: bot_meta (effective capital base, so equity resumes across restarts). The table
    # now lives in botcore.schema (applied before migrations); v4 stays as a no-op so the
    # version sequence and existing DBs' recorded version are unchanged (Plan D step 5).
    4: "",
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
    apply_base_schema(conn)   # neutral core tables first, so schema_version exists
    conn.executescript(SCHEMA)
    _apply_migrations(conn)
    if config.db_mmap_mb > 0:
        # Memory-map the DB file so SQLite accesses it via RAM pages managed
        # by the kernel rather than read() syscalls.
        # PRAGMA does not accept a bound parameter (sqlite raises "near ?: syntax
        # error"); db_mmap_mb is an int, so interpolate it directly.
        conn.execute(f"PRAGMA mmap_size = {int(config.db_mmap_mb) * 1024 * 1024}")
        logger.info("DB mmap enabled: %d MB", config.db_mmap_mb)
    conn.commit()
    logger.info("DB initialized: %s", config.db_path)
    return conn


# ─── STATE CLASSES ────────────────────────────────────────────────────────────

# TokenState + RejectionStats live in pm_types (re-exported at the top of this module).

class BotState:
    """
    Global runtime state shared across all async tasks.
    A single instance lives for the entire process lifetime.
    """
    def __init__(self, conn: sqlite3.Connection,
                 config: Optional["BotConfig"] = None,
                 strategy: Any = None,
                 connector: Any = None) -> None:
        self.conn = conn
        # Exchange connector (api_* module) — INJECTED by the entrypoint (Plan D step 4b),
        # not a module global: the data/order functions reach it via state.connector, so
        # the trading code carries no hard reference to any one exchange module.
        self.connector: Any = connector
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
        self.last_book_ts: float = 0.0       # epoch time of last processed book update
        self.last_write_ts: float = 0.0      # epoch time of last PERSISTED data row
                                             # (snapshot). Distinct from last_book_ts
                                             # (received): catches "heartbeating but
                                             # recording stopped" via the status page.
        # Circuit-breaker: suspend new entries after 3 consecutive CLOB failures.
        self.api_fail_streak: int = 0
        self.api_cooldown_until: float = 0.0
        # Active strategy instance — INJECTED by the entrypoint (Plan D step 3b), not
        # named here: BotState carries no dependency on any concrete strategy/plugin. The
        # Polymarket entrypoint (main / account_bot) passes ThresholdStrategy(); main()
        # then overrides it with a GridStrategy/SwingStrategy when strategy_type !=
        # "threshold". None until injected — the book-update dispatch requires it set.
        self.strategy: Any = strategy
        # Rejection counters — reset and logged every 60 s.
        self.rejection_stats = RejectionStats()
        self._last_stats_log: float = time.time()

    @property
    def win_rate(self) -> float:
        """Win rate as a percentage, 0.0 if no resolved trades."""
        t = self.wins + self.losses
        return (self.wins / t * 100) if t else 0.0


# cumulative_pnl / equity live in botcore.persistence (re-exported at the top of this module).


# ─── WEBSOCKET MESSAGE HANDLING ───────────────────────────────────────────────

# handle_book_update / save_snapshot / purge_expired_markets / register_market /
# ws_loop / _market_refresh_loop / _run_ws live in pm_data (the entrypoint re-exports the
# first two; _run_ws is internal to pm_data).


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

    # Resume the capital base across restarts: use the persisted base if present,
    # otherwise seed it from config (first boot, or first boot after a reset that
    # wiped the DB — config.capital_start then already holds the reset override).
    _base = read_capital_base(state.conn)
    if _base is None:
        _base = state.config.capital_start
        write_capital_base(state.conn, _base)
    state.config.capital_start = _base
    state.capital = _base + state.total_pnl

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


# ─── DATA PLANE: shared-feed consumer (alternative to the direct WS) ───────────
# When data_source == "feed", the bot sources market data from the shared feed
# instead of opening its own Polymarket WS. Order placement is unchanged — it still
# happens inside handle_book_update via this bot's own credentials (order plane).
# Mirrors account_bot's consumer loop; does NOT auto-start a feed.

# _register_market_from_feed / feed_consumer_loop live in pm_data (the entrypoint
# re-exports _register_market_from_feed; main() reaches feed_consumer_loop via the
# data-source dispatch registry).


# ─── DATA-SOURCE DISPATCH (Plan D step 4b-6) ──────────────────────────────────
# Registry: data_source → which plugin run-loop serves it. A strategy family plugs in by
# adding an entry here; main()'s dispatch never names a plugin loop. Loops are resolved by
# (plugin module, attr) and imported lazily at dispatch, so a polymarket-only account never
# imports the CEX consumer.
_CEX_CONNECTORS = ("binance", "mexc", "mexc_futures", "bitstamp")


def _cex_symbol(config: "BotConfig") -> str:
    """Symbol a CEX bot subscribes to: the grid symbol, else the strategy cfg's symbol."""
    return (config.grid_symbol if config.strategy_type == "grid"
            else config.strategy_cfg.get("symbol", config.grid_symbol))


# data_source -> (supported connector(s), plugin module, loop attr, args from (state, config, session))
_RUN_LOOPS: dict = {
    "feed": ("polymarket", "pm_data", "feed_consumer_loop",
             lambda state, config, session: (state, config.feed_addr)),
    "cex_feed": (_CEX_CONNECTORS, "cex_consumer", "cex_feed_consumer_loop",
                 lambda state, config, session: (state, config.feed_addr,
                                                 _cex_symbol(config), config.connector)),
    # accumulation: SUB the indicators service (not the cex_feed). Primary/scalping stream →
    # on_book_update, gate streams → on_indicator (handled inside indicators_consumer_loop).
    # The accumulation strategy JSON is self-contained (like the former standalone bot): prefer
    # its indicators_addr/reg_addr/streams from strategy_cfg, falling back to the config.json-
    # sourced BotConfig fields. data_source="indicators" itself still comes from config.json.
    "indicators": (_CEX_CONNECTORS, "cex_consumer", "indicators_consumer_loop",
                   lambda state, config, session: (
                       state,
                       config.strategy_cfg.get("indicators_addr") or config.indicators_addr,
                       config.strategy_cfg.get("indicators_reg_addr") or config.indicators_reg_addr,
                       config.strategy_cfg.get("indicators_streams") or config.indicators_streams,
                       config.strategy_cfg.get("scalping_stream_id", "btc_scalping_spot"),
                       config.strategy_cfg.get("register_interval_s", 120))),
}
# Fallback when no feed data_source matches the connector: the direct WS path.
_DIRECT_WS = ("pm_data", "ws_loop", lambda state, config, session: (state, session))


def _resolve_run_loop(config: "BotConfig"):
    """Pure selection (no import / no I/O) → (module, loop_attr, args_builder, warn).

    Mirrors the historical dispatch exactly: a feed/cex_feed data_source whose connector
    does not match falls back to the direct WS path WITH a warning; any other data_source
    (e.g. "ws") falls back to direct WS silently.
    """
    spec = _RUN_LOOPS.get(config.data_source)
    if spec is not None:
        connectors, mod, loop, args = spec
        ok = (config.connector == connectors if isinstance(connectors, str)
              else config.connector in connectors)
        if ok:
            return mod, loop, args, False
        return (*_DIRECT_WS, True)   # feed source, wrong connector → warn + direct WS
    return (*_DIRECT_WS, False)      # e.g. data_source="ws" → direct WS, no warning


async def main() -> None:
    args   = _parse_args()
    config = make_config(
        simulate=args.simulate,
        no_log=args.no_log,
        no_snapshots=args.no_snapshots,
        snapshot_interval=args.snapshot_interval,
    )
    _setup_logging(config)
    # IPv4-only egress pin for IP-whitelisted real accounts (e.g. a MEXC key locked to a
    # single v4 address). Gated by env var so the sim accounts are untouched. Pins the
    # whole process's DNS to A records — covers aiohttp (order plane) AND the websockets
    # user-data stream; loopback/ZMQ + Binance/Polymarket all resolve v4, so it is safe
    # process-wide. Belt-and-suspenders with family=AF_INET on the session connector below.
    if os.environ.get("TRADINEBOTTE_IPV4_ONLY"):
        _gai = socket.getaddrinfo
        socket.getaddrinfo = lambda h, p, f=0, *a, **k: _gai(h, p, socket.AF_INET, *a, **k)
        logger.info("IPv4-only egress pinned (TRADINEBOTTE_IPV4_ONLY)")
    connector = _load_connector_module(config.connector)
    logger.info("Connector loaded: %s (%s)", config.connector, connector.__name__)
    if config.strategy_type != "threshold":
        from botcore.connectors import validate as _validate_conn
        _validate_conn(connector, config.strategy_type)

    _up = int(time.time() - _BOT_START)
    _start_str = datetime.fromtimestamp(_BOT_START, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    logger.info("=" * 65)
    if config.simulate:
        logger.info("  SIMULATION MODE — data isolated in %s", config.install_dir)
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
        logger.info("  Strategy: file not found — using defaults")
    if config.connector == "polymarket":
        _tf = "15M" if config.market_tag_id == connector.GAMMA_TAG_15M else "5M"
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
        logger.info("  POLY_PRIVATE_KEY not set — orders SIMULATED")
    logger.info("=" * 65)

    # Self-reported mode (decided from config, never runtime order ids): threshold
    # places real orders only with a private_key; CEX grid/swing are always sim.
    _hb_mode = "live" if (config.strategy_type == "threshold" and config.private_key) else "sim"
    _is_live = _hb_mode == "live"
    # Fleet join key: a stable, globally-unique bot id (readable prefix + generated
    # suffix), read from <install_dir>/bot_id_<role> or generated on first creation. The
    # deploy writes this file before starting the bot; this call is the read/safety-net.
    _pair = (config.grid_symbol if config.strategy_type == "grid"
             else config.strategy_cfg.get("symbol", "")) or ""
    _hb_bot_name = resolve_bot_id(config.install_dir, config.strategy_type,
                                  config.connector, config.strategy_type, _pair)

    # Apply a pending operator reset BEFORE opening the DB so the wipe is atomic at
    # cold start. Sim-only: a live bot must never honour a reset marker.
    if not _is_live:
        _reset_cap = consume_reset_marker(config.install_dir, _hb_bot_name, config.db_path)
        if _reset_cap is not None:
            config.capital_start = _reset_cap
            logger.warning("RESET applied — fresh state, starting capital=$%.2f", _reset_cap)

    conn = init_db(config)
    try:
        state = BotState(conn, config, strategy=ThresholdStrategy(), connector=connector)
        restore_state_from_db(state)

        _fam = socket.AF_INET if os.environ.get("TRADINEBOTTE_IPV4_ONLY") else 0
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=10, family=_fam),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as session:
            state.session = session   # available before ws_loop for strategy restore
            if config.strategy_type != "threshold":
                from strategy_engines import load as _load_strat
                state.strategy = _load_strat(config.strategy_type, config)
                logger.info("  Algorithm   : %s", config.strategy_type)
                # Warm the connector's per-symbol precision cache at boot (before restore,
                # which for the grid also relies on the exchange tick) so the first order
                # never pays the exchangeInfo round-trip and an unreachable exchange
                # surfaces here rather than as a fail-closed rejection mid-trade.
                if hasattr(state.strategy, "warm_precision"):
                    await state.strategy.warm_precision(state)
                await state.strategy.restore_from_db(state)
            def _hb_payload() -> dict[str, Any]:
                # An engine may own its heartbeat shape (accumulation reports holdings/avg_entry/
                # realized, not the threshold/grid/swing fields cumulative_pnl assumes). When it
                # provides heartbeat_payload(), that IS the payload — keeps acct-2/3/4's status
                # page fields identical to the former standalone bot.
                _strat = getattr(state, "strategy", None)
                if hasattr(_strat, "heartbeat_payload"):
                    return _strat.heartbeat_payload()
                pnl_total, trades_total = cumulative_pnl(state)
                hb: dict[str, Any] = {
                    "bounds_ok":    state.daily_pnl >= -config.daily_stop_loss,
                    "daily_pnl":    round(state.daily_pnl, 2),
                    "pnl_total":    round(pnl_total, 2),
                    "trades_total": trades_total,
                    "capital":      round(config.capital_start + pnl_total, 2),  # equity
                    "open_trades":  len(state.open_trades),
                    "last_book_ts": state.last_book_ts,
                }
                # Data-recording freshness: lets the status page flag "bot heartbeats
                # but its snapshots table stopped growing" (the 2026-06-16 CEX bug) —
                # distinct from last_book_ts, which is data RECEIVED, not PERSISTED.
                # Omitted when snapshots are disabled so --no-snapshots bots never alarm.
                if config.enable_snapshots:
                    hb["last_write_ts"] = state.last_write_ts
                return hb

            # Start the data-freshness clock at boot: a recorder that never writes then
            # ages to ⚠data after _DATA_STALE_AFTER (catches "booted in a non-writing
            # mode" — the post-restart shape of the 06-16 bug) with no false blip on
            # every restart; a healthy recorder advances it on its first write.
            if config.enable_snapshots:
                state.last_write_ts = time.time()

            _hb_task = asyncio.create_task(
                heartbeat_loop(
                    _hb_bot_name,
                    config.install_dir,
                    _hb_payload,
                    mode=_hb_mode,
                )
            )

            # Opt-in HTTP /health (no-op unless TRADINEBOTTE_HEALTH_PORT is set);
            # reuses _hb_payload so the pulled view matches the pushed heartbeat.
            _health_task = asyncio.create_task(
                health_server(
                    _hb_bot_name,
                    config.install_dir,
                    _hb_payload,
                    mode=_hb_mode,
                )
            )

            def _reset_handler(cmd_args: dict[str, Any]) -> dict[str, Any]:
                """Sim-only: record a reset and hard-exit so systemd restarts cold,
                wiping state and starting from `capital`. Guarded by control_loop."""
                cap = float(cmd_args.get("capital", config.capital_start))
                if cap <= 0:
                    raise ValueError("capital must be > 0")
                write_reset_marker(config.install_dir, _hb_bot_name, cap)
                logger.warning("RESET requested via control plane — capital=$%.2f, "
                               "exiting for systemd restart", cap)
                # exit AFTER the reply is sent; non-zero trips Restart=on-failure.
                asyncio.get_running_loop().call_later(0.5, os._exit, 1)
                return {"capital": cap, "wiped_on_restart": True, "restart_in_s": 30}

            _ctl_task = asyncio.create_task(
                control_loop(
                    _hb_bot_name,
                    {"reset": Command(_reset_handler, destructive=True,
                                      help="wipe state + restart with given --capital (sim only)")},
                    mode=_hb_mode,
                    is_live=_is_live,
                )
            )
            import importlib  # noqa: PLC0415
            _mod, _loop, _args, _warn = _resolve_run_loop(config)
            if _warn:
                logger.warning("data_source=%s ignored for connector=%s — using direct WS",
                               config.data_source, config.connector)
            try:
                # Lazy import keeps a polymarket-only account from loading the CEX consumer.
                _run_loop = getattr(importlib.import_module(_mod), _loop)
                await _run_loop(*_args(state, config, session))
            finally:
                _hb_task.cancel()
                _ctl_task.cancel()
                _health_task.cancel()
                await asyncio.gather(_hb_task, _ctl_task, _health_task,
                                     return_exceptions=True)
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
