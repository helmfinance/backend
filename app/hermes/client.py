"""Pyth Hermes HTTP client.

Fetches latest VAA-style update bytes + parsed prices for synthetic-equity feeds.
Used by /agents/{id}/mint-preview and /agents/{id}/pyth-update-bytes.
"""

from __future__ import annotations

from typing import TypedDict

import httpx

from app.config import settings

# Pyth-tracked synthetic symbols. mETH (Mantle staked ETH) and USDY (Ondo)
# are valued via their own adapters / rebasing — no Pyth feed entries here.
FEED_BY_SYMBOL: dict[str, str] = {
    "sNVDA": settings.pyth_feed_nvda,
    "sSPY": settings.pyth_feed_spy,
    "sAAPL": settings.pyth_feed_aapl,
    "sMSFT": settings.pyth_feed_msft,
    "sTSLA": settings.pyth_feed_tsla,
}


class HermesError(Exception):
    """Hermes HTTP error (non-timeout)."""


class HermesTimeout(HermesError):
    """Hermes request exceeded the client timeout."""


class HermesPrice(TypedDict):
    feed_id: str
    symbol: str
    price_usdc: str
    confidence: str
    publish_time: int


def fetch_price_updates(
    feed_ids: list[str],
) -> tuple[list[str], list[HermesPrice]]:
    """Returns (update_data_hex_with_0x, parsed_prices_in_usdc_scale).

    Empty feed_ids → ([], []) without an HTTP call.
    """
    if not feed_ids:
        return [], []

    url = f"{settings.pyth_hermes_url}/v2/updates/price/latest"
    params: list[tuple[str, str]] = [("ids[]", fid) for fid in feed_ids]
    params.append(("encoding", "hex"))

    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.TimeoutException as e:
        raise HermesTimeout(str(e)) from e
    except httpx.HTTPError as e:
        raise HermesError(str(e)) from e

    update_data = [
        h if h.startswith("0x") else ("0x" + h)
        for h in data.get("binary", {}).get("data", [])
    ]

    feed_lookup = {v.lower(): k for k, v in FEED_BY_SYMBOL.items() if v}

    parsed: list[HermesPrice] = []
    for item in data.get("parsed", []):
        fid_raw = item["id"]
        feed_id = fid_raw if fid_raw.startswith("0x") else ("0x" + fid_raw)
        symbol = feed_lookup.get(feed_id.lower(), feed_id)

        p = item["price"]
        price_int = int(p["price"])
        conf_int = int(p["conf"])
        expo = int(p["expo"])

        # Pyth expo is typically -8 for equities. Target scale is 6 (USDC).
        scale_diff = 6 + expo
        if scale_diff >= 0:
            mul = 10 ** scale_diff
            price_usdc = str(price_int * mul)
            conf_usdc = str(conf_int * mul)
        else:
            div = 10 ** (-scale_diff)
            price_usdc = str(price_int // div)
            conf_usdc = str(conf_int // div)

        parsed.append(
            {
                "feed_id": feed_id,
                "symbol": symbol,
                "price_usdc": price_usdc,
                "confidence": conf_usdc,
                "publish_time": int(p["publish_time"]),
            }
        )

    return update_data, parsed


def estimate_pyth_fee_wei(feed_count: int) -> str:
    """Conservative estimate: 0.001 MNT per feed.

    Real on-chain fee via Pyth.getUpdateFee is post-MVP. Over-estimating is
    safe — the contract refunds the difference.
    """
    return str(1_000_000_000_000_000 * max(feed_count, 0))
