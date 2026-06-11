"""
Binance Simple Earn — Flexible product manager.

Parks idle USDT in Binance Flexible Earn when not needed for trading,
and redeems before buy orders. Designed for the long-term cycle strategy
where large cash positions sit idle for weeks or months between trades.

Earn product lifecycle
----------------------
  sell trade completes  → park_idle(new_usdt_balance, keep=MIN_LIQUID)
  buy trade imminent    → ensure_liquid(needed_usdt) → redeem if short

Credentials
-----------
  BINANCE_API_KEY and BINANCE_API_SECRET env vars (same as trading).
  The API key must have the "Enable Flexible Savings" permission checked.
  Without credentials the manager runs in sim mode (logs, no real calls).

MEXC note
---------
  MEXC Earn API is not stable enough for production use. Binance only.

API reference
-------------
  GET  /sapi/v1/simple-earn/flexible/list     — discover product IDs
  GET  /sapi/v1/simple-earn/flexible/position — current parked amount
  POST /sapi/v1/simple-earn/flexible/subscribe
  POST /sapi/v1/simple-earn/flexible/redeem
"""

import hashlib
import hmac
import logging
import os
import time
from urllib.parse import urlencode

import aiohttp

logger = logging.getLogger(__name__)

_BASE_URL        = "https://api.binance.com"
_TIMEOUT         = aiohttp.ClientTimeout(total=15)

# Keep at least this much USDT liquid in spot wallet at all times.
# Covers fees, slippage, and next-trade readiness.
MIN_LIQUID_USDT  = 20.0

# Do not subscribe amounts below this (Binance enforces a ~$1 minimum).
MIN_SUBSCRIBE_USDT = 1.0


def _has_creds() -> bool:
    return bool(
        os.environ.get("BINANCE_API_KEY")
        and os.environ.get("BINANCE_API_SECRET")
    )


def _sign(params: dict) -> dict:
    """Add timestamp + HMAC-SHA256 signature to a Binance API params dict."""
    params["timestamp"] = int(time.time() * 1000)
    query = urlencode(params)
    secret = os.environ.get("BINANCE_API_SECRET", "").encode()
    params["signature"] = hmac.new(secret, query.encode(), hashlib.sha256).hexdigest()
    return params


def _headers() -> dict:
    return {"X-MBX-APIKEY": os.environ.get("BINANCE_API_KEY", "")}


class EarnManager:
    """
    Manages Binance Simple Earn Flexible for idle USDT.

    Usage
    -----
        async with aiohttp.ClientSession() as session:
            earn = EarnManager(session)
            await earn.park_idle(usdt_balance)      # after sell
            await earn.ensure_liquid(needed_usdt)   # before buy
    """

    def __init__(self, session: aiohttp.ClientSession):
        self._session    = session
        self._enabled    = _has_creds()
        self._product_id: str | None = None   # cached on first discovery
        if not self._enabled:
            logger.info("EarnManager: no credentials — sim mode (no real API calls)")

    async def _product(self) -> str | None:
        """Return USDT Flexible Earn product ID, discovering and caching it."""
        if self._product_id:
            return self._product_id
        params = _sign({"asset": "USDT", "status": "SUBSCRIBABLE"})
        try:
            async with self._session.get(
                f"{_BASE_URL}/sapi/v1/simple-earn/flexible/list",
                params=params,
                headers=_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                data = await resp.json(content_type=None)
                rows = data.get("rows", [])
                if not rows:
                    logger.warning("EarnManager: no USDT flexible product available")
                    return None
                self._product_id = rows[0]["productId"]
                apr = float(rows[0].get("latestAnnualPercentageRate", 0)) * 100
                logger.info("EarnManager: product=%s  APR=%.2f%%",
                            self._product_id, apr)
                return self._product_id
        except Exception as exc:
            logger.warning("EarnManager: product discovery error: %s", exc)
            return None

    async def get_position(self) -> float:
        """Return total USDT currently parked in Flexible Earn."""
        if not self._enabled:
            return 0.0
        params = _sign({"asset": "USDT"})
        try:
            async with self._session.get(
                f"{_BASE_URL}/sapi/v1/simple-earn/flexible/position",
                params=params,
                headers=_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                data = await resp.json(content_type=None)
                return sum(
                    float(r.get("totalAmount", 0))
                    for r in data.get("rows", [])
                )
        except Exception as exc:
            logger.warning("EarnManager: get_position error: %s", exc)
            return 0.0

    async def get_apr(self) -> float:
        """Return current annual percentage rate (0.0–1.0) for USDT Flexible."""
        if not self._enabled:
            return 0.0
        params = _sign({"asset": "USDT", "status": "SUBSCRIBABLE"})
        try:
            async with self._session.get(
                f"{_BASE_URL}/sapi/v1/simple-earn/flexible/list",
                params=params,
                headers=_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                data = await resp.json(content_type=None)
                rows = data.get("rows", [])
                if rows:
                    return float(rows[0].get("latestAnnualPercentageRate", 0))
                return 0.0
        except Exception as exc:
            logger.warning("EarnManager: get_apr error: %s", exc)
            return 0.0

    async def subscribe(self, amount_usdt: float) -> bool:
        """
        Subscribe amount_usdt to Flexible Earn.
        autoSubscribe=true re-invests accrued interest automatically.
        Returns True on success.
        """
        if not self._enabled:
            logger.info("EarnManager sim: subscribe %.2f USDT", amount_usdt)
            return True
        if amount_usdt < MIN_SUBSCRIBE_USDT:
            logger.info("EarnManager: subscribe %.2f USDT below minimum (%.2f) — skipped",
                        amount_usdt, MIN_SUBSCRIBE_USDT)
            return False
        pid = await self._product()
        if not pid:
            return False
        params = _sign({
            "productId":     pid,
            "amount":        f"{amount_usdt:.8f}",
            "autoSubscribe": "true",
        })
        try:
            async with self._session.post(
                f"{_BASE_URL}/sapi/v1/simple-earn/flexible/subscribe",
                params=params,
                headers=_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                data = await resp.json(content_type=None)
                ok = bool(data.get("success"))
                if ok:
                    logger.info("EarnManager: subscribed %.2f USDT (id=%s)",
                                amount_usdt, data.get("purchaseId"))
                else:
                    logger.warning("EarnManager: subscribe failed: %s", data)
                return ok
        except Exception as exc:
            logger.warning("EarnManager: subscribe error: %s", exc)
            return False

    async def redeem(self, amount_usdt: float) -> bool:
        """
        Redeem up to amount_usdt from Flexible Earn into spot wallet.
        Binance Flexible redemptions typically settle in <1 second.
        Returns True on success (or if nothing was parked).
        """
        if not self._enabled:
            logger.info("EarnManager sim: redeem %.2f USDT", amount_usdt)
            return True
        pid = await self._product()
        if not pid:
            return False
        parked = await self.get_position()
        if parked <= 0:
            return True
        to_redeem = min(amount_usdt, parked)
        if to_redeem < MIN_SUBSCRIBE_USDT:
            return True
        params = _sign({
            "productId": pid,
            "amount":    f"{to_redeem:.8f}",
            "redeemAll": "false",
        })
        try:
            async with self._session.post(
                f"{_BASE_URL}/sapi/v1/simple-earn/flexible/redeem",
                params=params,
                headers=_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                data = await resp.json(content_type=None)
                ok = bool(data.get("success"))
                if ok:
                    logger.info("EarnManager: redeemed %.2f USDT (parked=%.2f)",
                                to_redeem, parked)
                else:
                    logger.warning("EarnManager: redeem failed: %s", data)
                return ok
        except Exception as exc:
            logger.warning("EarnManager: redeem error: %s", exc)
            return False

    async def redeem_all(self) -> bool:
        """Redeem the entire Flexible Earn position. Returns True on success."""
        if not self._enabled:
            logger.info("EarnManager sim: redeem_all")
            return True
        pid = await self._product()
        if not pid:
            return False
        parked = await self.get_position()
        if parked <= 0:
            return True
        params = _sign({"productId": pid, "redeemAll": "true"})
        try:
            async with self._session.post(
                f"{_BASE_URL}/sapi/v1/simple-earn/flexible/redeem",
                params=params,
                headers=_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                data = await resp.json(content_type=None)
                ok = bool(data.get("success"))
                if ok:
                    logger.info("EarnManager: redeemed all (was %.2f USDT)", parked)
                else:
                    logger.warning("EarnManager: redeem_all failed: %s", data)
                return ok
        except Exception as exc:
            logger.warning("EarnManager: redeem_all error: %s", exc)
            return False

    # ------------------------------------------------------------------
    # High-level helpers for strategy integration
    # ------------------------------------------------------------------

    async def park_idle(self, usdt_balance: float,
                        keep_liquid: float = MIN_LIQUID_USDT) -> None:
        """
        Subscribe any USDT above keep_liquid to Flexible Earn.
        Call after each sell trade with the updated USD balance.

        Example
        -------
            # After selling BTC → $5,000 USD now in spot
            await earn.park_idle(5000.0)   # parks $4,980, keeps $20 liquid
        """
        to_park = usdt_balance - keep_liquid
        if to_park >= MIN_SUBSCRIBE_USDT:
            await self.subscribe(to_park)

    async def ensure_liquid(self, needed_usdt: float,
                             keep_liquid: float = MIN_LIQUID_USDT) -> bool:
        """
        Ensure at least needed_usdt is available in the spot wallet.
        Redeems from Earn if the parked amount can cover the shortfall.
        Call before any buy trade.

        Returns True when the requested amount can be covered (either
        already liquid or successfully redeemed). The caller should
        still verify the actual spot balance before placing the order.

        Example
        -------
            ok = await earn.ensure_liquid(10_000.0)  # need $10K for buy
            if ok:
                await api.post_order(...)
        """
        parked = await self.get_position()
        if parked <= 0:
            return True  # nothing parked in Earn — redemption not possible; caller must verify spot
        # How much more do we need beyond keep_liquid?
        shortfall = needed_usdt - keep_liquid
        if shortfall <= 0:
            return True
        to_redeem = min(shortfall, parked)
        if to_redeem < MIN_SUBSCRIBE_USDT:
            return True
        return await self.redeem(to_redeem)
