"""Bybit V5 public market data client (no auth required).

DEPRECATED: Bybit geo-blocks Railway's US East egress IPs, so production
deploys saw every call fail. Live callers were switched to
``app.services.coingecko_client`` (see Task PP). This module is kept around
only because it has no other importers; remove once a cleanup pass confirms
nothing else loads it.

In-process TTL cache to keep us under the public rate limits during demo
traffic. All exceptions are caught at the metric-aggregator boundary so a
flaky upstream surfaces as ``None`` values rather than a 500.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

BASE_URL = "https://api.bybit.com"
CACHE_TTL = 60  # seconds
HTTP_TIMEOUT = 10.0

_cache: dict[str, tuple[float, Any]] = {}


def _cached_get(path: str, params: dict | None = None) -> dict:
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


def fetch_spot_ticker(symbol: str) -> dict:
    """Returns dict with lastPrice, price24hPcnt, volume24h etc."""
    data = _cached_get(
        "/v5/market/tickers", {"category": "spot", "symbol": symbol}
    )
    items = data.get("result", {}).get("list", [])
    if not items:
        raise ValueError(f"No ticker for {symbol}")
    return items[0]


def fetch_funding_rate(symbol: str) -> float:
    """Latest funding rate for a perpetual contract (linear category)."""
    data = _cached_get(
        "/v5/market/funding/history",
        {"category": "linear", "symbol": symbol, "limit": 1},
    )
    items = data.get("result", {}).get("list", [])
    if not items:
        raise ValueError(f"No funding history for {symbol}")
    return float(items[0]["fundingRate"])


def get_market_metrics() -> dict[str, float | None]:
    """All metrics for the DSL evaluator. Individual fetches that fail land
    as ``None`` so a single upstream hiccup doesn't blank the whole response."""
    metrics: dict[str, float | None] = {}
    try:
        btc = fetch_spot_ticker("BTCUSDT")
        metrics["BTC_PRICE"] = float(btc["lastPrice"])
        metrics["BTC_24H_CHANGE"] = float(btc["price24hPcnt"])
    except Exception:
        metrics["BTC_PRICE"] = None
        metrics["BTC_24H_CHANGE"] = None

    try:
        eth = fetch_spot_ticker("ETHUSDT")
        metrics["ETH_PRICE"] = float(eth["lastPrice"])
        metrics["ETH_24H_CHANGE"] = float(eth["price24hPcnt"])
    except Exception:
        metrics["ETH_PRICE"] = None
        metrics["ETH_24H_CHANGE"] = None

    try:
        metrics["BTC_FUNDING_RATE"] = fetch_funding_rate("BTCUSDT")
    except Exception:
        metrics["BTC_FUNDING_RATE"] = None

    return metrics


__all__ = [
    "fetch_spot_ticker",
    "fetch_funding_rate",
    "get_market_metrics",
]
