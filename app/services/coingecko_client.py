"""CoinGecko public API client (no auth, no geo restriction).

Replaces ``bybit_client`` because Bybit blocks Railway's US East egress IPs.
One call to ``/coins/markets`` returns both BTC + ETH price and 24h change.
Funding rate isn't part of the CoinGecko free tier — that metric stays None
and the DSL evaluator surfaces it as "Metric unavailable" gracefully.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.coingecko.com/api/v3"
CACHE_TTL = 60  # seconds
HTTP_TIMEOUT = 15.0

_cache: dict[str, tuple[float, Any]] = {}


def _cached_get(path: str, params: dict | None = None) -> Any:
    key = f"{path}?{sorted((params or {}).items())}"
    now = time.time()
    cached = _cache.get(key)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]
    r = httpx.get(f"{BASE_URL}{path}", params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    _cache[key] = (now, data)
    return data


def get_market_metrics() -> dict[str, float | None]:
    """Single CoinGecko call → BTC + ETH price + 24h change. Funding rate is
    not exposed by CoinGecko's free tier — kept as None to preserve the
    DSL surface that ``condition_evaluator`` already handles."""
    metrics: dict[str, float | None] = {
        "BTC_PRICE": None,
        "ETH_PRICE": None,
        "BTC_24H_CHANGE": None,
        "ETH_24H_CHANGE": None,
        "BTC_FUNDING_RATE": None,  # not provided by CoinGecko free tier
    }
    try:
        data = _cached_get("/coins/markets", {
            "vs_currency": "usd",
            "ids": "bitcoin,ethereum",
        })
    except Exception as e:
        logger.warning("[coingecko] fetch failed: %s: %s", type(e).__name__, e)
        return metrics

    for coin in data or []:
        cid = coin.get("id")
        price = coin.get("current_price")
        pct = coin.get("price_change_percentage_24h")
        if cid == "bitcoin":
            metrics["BTC_PRICE"] = float(price) if price is not None else None
            # CoinGecko returns the field as a percentage (e.g. 2.5 = +2.5%);
            # condition_evaluator expects decimal (0.025), so divide by 100.
            metrics["BTC_24H_CHANGE"] = float(pct) / 100 if pct is not None else None
        elif cid == "ethereum":
            metrics["ETH_PRICE"] = float(price) if price is not None else None
            metrics["ETH_24H_CHANGE"] = float(pct) / 100 if pct is not None else None

    return metrics


__all__ = ["get_market_metrics"]
