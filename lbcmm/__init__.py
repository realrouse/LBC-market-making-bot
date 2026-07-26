"""LBC-market-making-bot — standalone LBC/USDT liquidity bot for MEXC.

Forked from neofutur's tradinebotte multibot design (GPL-3.0).
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("lbc-market-making-bot")
except PackageNotFoundError:
    __version__ = "0.1.0"

__all__ = ["__version__"]
