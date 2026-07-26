"""Config load/save for LBC market making bot."""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

# tomli for write on older python — we write via simple toml dump

DEFAULT_SYMBOL = "LBCUSDT"
CONFIG_ENV = "LBCMM_CONFIG"


@dataclass
class BotConfig:
    symbol: str = DEFAULT_SYMBOL
    strategy: str = "depth_provider"  # depth_provider | bamm | grid
    paper: bool = True
    live_confirmed: bool = False
    # False until the user finishes first-time setup (CLI wizard or GUI wizard)
    setup_complete: bool = False

    # Simple mode — Provide Liquidity
    usdt_budget: float = 10.0
    lbc_budget: float = 5000.0  # coins; user-facing LBC amount
    bid_depth_pct: float = 2.0
    ask_depth_pct: float = 2.0
    n_levels: int = 4
    min_notional_usdt: float = 1.1
    reprice_pct: float = 0.35
    poll_interval_s: float = 3.0

    # Advanced
    advanced: bool = False
    # BAMM defaults (safer small-account)
    bamm_top: float = 0.0  # 0 = use mid * 1.05 at start
    bamm_floor: float = 0.001
    bamm_step_pct: float = 5.0
    bamm_stash_pct: float = 0.10
    # Grid
    grid_lower: float = 0.0
    grid_upper: float = 0.0
    grid_levels: int = 10
    grid_order_size_usdt: float = 5.0

    # Credentials: prefer env; optional file values (never commit)
    mexc_api_key: str = ""
    mexc_api_secret: str = ""

    # Paths
    data_dir: str = ""
    log_file: str = ""

    gui_host: str = "127.0.0.1"
    gui_port: int = 8787

    def resolve_data_dir(self) -> Path:
        if self.data_dir:
            p = Path(self.data_dir).expanduser()
        else:
            p = Path.home() / ".local" / "share" / "lbcmm"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def effective_paper(self) -> bool:
        if self.paper or not self.live_confirmed:
            return True
        key = self.mexc_api_key or os.environ.get("MEXC_API_KEY", "")
        secret = self.mexc_api_secret or os.environ.get("MEXC_API_SECRET", "")
        return not (key and secret)

    def api_key(self) -> str:
        return self.mexc_api_key or os.environ.get("MEXC_API_KEY", "")

    def api_secret(self) -> str:
        return self.mexc_api_secret or os.environ.get("MEXC_API_SECRET", "")


def default_config_path() -> Path:
    env = os.environ.get(CONFIG_ENV)
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(xdg) / "lbcmm" / "config.toml"


def load_config(path: Optional[Path] = None) -> BotConfig:
    path = path or default_config_path()
    if not path.is_file():
        return BotConfig()
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return _from_dict(raw)


def save_config(cfg: BotConfig, path: Optional[Path] = None) -> Path:
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(cfg)
    # never write secrets if empty; mask policy: write only if user set them
    text = _to_toml(data)
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _from_dict(raw: dict[str, Any]) -> BotConfig:
    known = {f.name for f in BotConfig.__dataclass_fields__.values()}  # type: ignore
    kwargs = {}
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        if k in known:
            kwargs[k] = v
    # nested sections
    for section in ("liquidity", "advanced", "mexc", "gui"):
        if isinstance(raw.get(section), dict):
            for k, v in raw[section].items():
                if k in known:
                    kwargs[k] = v
                elif section == "liquidity" and k == "usdt":
                    kwargs["usdt_budget"] = float(v)
                elif section == "liquidity" and k == "lbc":
                    kwargs["lbc_budget"] = float(v)
    if "mexc" in raw and isinstance(raw["mexc"], dict):
        if "api_key" in raw["mexc"]:
            kwargs["mexc_api_key"] = str(raw["mexc"]["api_key"])
        if "api_secret" in raw["mexc"]:
            kwargs["mexc_api_secret"] = str(raw["mexc"]["api_secret"])
    return BotConfig(**kwargs)


def _to_toml(data: dict) -> str:
    lines = [
        "# LBC-market-making-bot config",
        "# Keep this file private (mode 600). Prefer env MEXC_API_KEY / MEXC_API_SECRET.",
        "",
        f'symbol = "{data.get("symbol", DEFAULT_SYMBOL)}"',
        f'strategy = "{data.get("strategy", "depth_provider")}"',
        f"paper = {str(bool(data.get('paper', True))).lower()}",
        f"live_confirmed = {str(bool(data.get('live_confirmed', False))).lower()}",
        f"setup_complete = {str(bool(data.get('setup_complete', False))).lower()}",
        "",
        "[liquidity]",
        f"usdt_budget = {float(data.get('usdt_budget', 10))}",
        f"lbc_budget = {float(data.get('lbc_budget', 0))}",
        f"bid_depth_pct = {float(data.get('bid_depth_pct', 2))}",
        f"ask_depth_pct = {float(data.get('ask_depth_pct', 2))}",
        f"n_levels = {int(data.get('n_levels', 4))}",
        f"min_notional_usdt = {float(data.get('min_notional_usdt', 1.1))}",
        f"reprice_pct = {float(data.get('reprice_pct', 0.35))}",
        f"poll_interval_s = {float(data.get('poll_interval_s', 3))}",
        "",
        "[advanced]",
        f"advanced = {str(bool(data.get('advanced', False))).lower()}",
        f"bamm_floor = {float(data.get('bamm_floor', 0.001))}",
        f"bamm_step_pct = {float(data.get('bamm_step_pct', 5))}",
        f"bamm_stash_pct = {float(data.get('bamm_stash_pct', 0.10))}",
        f"grid_levels = {int(data.get('grid_levels', 10))}",
        f"grid_order_size_usdt = {float(data.get('grid_order_size_usdt', 5))}",
        "",
        "[mexc]",
        f'api_key = "{data.get("mexc_api_key", "")}"',
        f'api_secret = "{data.get("mexc_api_secret", "")}"',
        "",
        "[gui]",
        f'host = "{data.get("gui_host", "127.0.0.1")}"',
        f"port = {int(data.get('gui_port', 8787))}",
        "",
    ]
    return "\n".join(lines)
