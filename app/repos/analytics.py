"""Performance analytics from NavPoint time series."""

from __future__ import annotations

import math
import statistics

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import NavPoint

RISK_FREE_RATE = 0.04  # USDY-ish annualized
TRADING_DAYS_PER_YEAR = 365  # crypto markets 24/7
MIN_SAMPLES_FOR_SHARPE = 10


def compute_performance(db: Session, agent_id: int) -> dict:
    """Returns dict with total_return, max_drawdown, sharpe_ratio, sample_count,
    period_start, period_end. Fields may be None if insufficient data."""
    points = list(
        db.execute(
            select(NavPoint)
            .where(NavPoint.agent_id == agent_id)
            .order_by(NavPoint.timestamp.asc())
        ).scalars()
    )
    n = len(points)
    if n < 2:
        return {
            "total_return": None,
            "max_drawdown": None,
            "sharpe_ratio": None,
            "sample_count": n,
            "period_start": points[0].timestamp if points else None,
            "period_end": points[-1].timestamp if points else None,
        }

    # Scale-invariant: ratios cancel the divisor, so any positive normalization works.
    nps = [int(p.nav_per_share_usdc) / 10**18 for p in points]

    total_return = (nps[-1] / nps[0]) - 1.0 if nps[0] > 0 else None

    peak = nps[0]
    max_dd = 0.0
    for v in nps:
        if v > peak:
            peak = v
        dd = (v - peak) / peak if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd

    sharpe: float | None = None
    if n >= MIN_SAMPLES_FOR_SHARPE:
        daily_returns = [
            (nps[i] - nps[i - 1]) / nps[i - 1]
            for i in range(1, n)
            if nps[i - 1] > 0
        ]
        if len(daily_returns) > 1:
            mean_r = statistics.mean(daily_returns)
            stdev_r = statistics.stdev(daily_returns)
            if stdev_r > 0:
                daily_rf = RISK_FREE_RATE / TRADING_DAYS_PER_YEAR
                sharpe = (mean_r - daily_rf) / stdev_r * math.sqrt(TRADING_DAYS_PER_YEAR)

    return {
        "total_return": total_return,
        "max_drawdown": max_dd,
        "sharpe_ratio": sharpe,
        "sample_count": n,
        "period_start": points[0].timestamp,
        "period_end": points[-1].timestamp,
    }


def compute_market_price(
    nav_per_share_usdc: int | str | None,
    reputation: int | None,
) -> tuple[int | None, int | None]:
    """Simulated secondary-market price with reputation premium.

    Returns (market_price_per_share, premium_bps) or (None, None) when NAV is
    missing or zero. Formula::

        premium_factor = (reputation / 10000 - 0.5) * 0.2  # -10% to +10%
        market_price   = nav * (1 + premium_factor)
    """
    if nav_per_share_usdc is None:
        return None, None
    nav = int(nav_per_share_usdc) if isinstance(nav_per_share_usdc, str) else nav_per_share_usdc
    if nav == 0:
        return None, None
    rep = reputation if reputation is not None else 5000
    premium_factor = (rep / 10000 - 0.5) * 0.2
    market_price = int(nav * (1 + premium_factor))
    premium_bps = int(premium_factor * 10000)
    return market_price, premium_bps


__all__ = ["compute_performance", "compute_market_price"]
