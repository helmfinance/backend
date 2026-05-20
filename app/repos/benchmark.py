"""Benchmark comparison helpers — agent NAV vs naive baselines.

No historical Pyth data available, and the testnet archive node isn't a
reliable source either. We instead apply a constant annualized growth rate
to a synthetic sSPY and 60/40 baseline, sampled at the same timestamps as
the agent's NavPoints. Good enough for demo framing ("Alpha vs SPY").
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import NavPoint

SPY_ANNUAL_RATE = 0.10                          # historical equity ~10%
SIXTY_FORTY_RATE = 0.10 * 0.6 + 0.04 * 0.4      # 60% equity + 40% USDY ≈ 7.6%
SECONDS_PER_YEAR = 365 * 86400


def _benchmark_value_at(rate: float, t_start: int, t_now: int) -> float:
    dt_years = (t_now - t_start) / SECONDS_PER_YEAR
    return 1.0 + rate * dt_years


def compute_benchmark_series(db: Session, agent_id: int) -> dict:
    """Returns agent NAV trajectory + two baseline trajectories, all normalized
    to start at 1.0. Empty series + None summary when fewer than 2 NavPoints."""
    points = list(
        db.execute(
            select(NavPoint)
            .where(NavPoint.agent_id == agent_id)
            .order_by(NavPoint.timestamp.asc())
        ).scalars()
    )
    if len(points) < 2:
        return {
            "agent_id": agent_id,
            "period_start": points[0].timestamp if points else None,
            "period_end": points[-1].timestamp if points else None,
            "sample_count": len(points),
            "series": [],
            "summary": None,
        }

    t_start = points[0].timestamp
    initial_nps = int(points[0].nav_per_share_usdc)

    series = []
    for p in points:
        agent_norm = (
            int(p.nav_per_share_usdc) / initial_nps if initial_nps > 0 else 1.0
        )
        spy_norm = _benchmark_value_at(SPY_ANNUAL_RATE, t_start, p.timestamp)
        sf_norm = _benchmark_value_at(SIXTY_FORTY_RATE, t_start, p.timestamp)
        series.append({
            "timestamp": p.timestamp,
            "agent": round(agent_norm, 6),
            "sspy": round(spy_norm, 6),
            "sixty_forty": round(sf_norm, 6),
        })

    final = series[-1]
    summary = {
        "agent_return": round(final["agent"] - 1, 6),
        "sspy_return": round(final["sspy"] - 1, 6),
        "sixty_forty_return": round(final["sixty_forty"] - 1, 6),
        "alpha_vs_sspy": round(final["agent"] - final["sspy"], 6),
        "alpha_vs_sixty_forty": round(final["agent"] - final["sixty_forty"], 6),
    }

    return {
        "agent_id": agent_id,
        "period_start": t_start,
        "period_end": points[-1].timestamp,
        "sample_count": len(points),
        "series": series,
        "summary": summary,
    }


__all__ = ["compute_benchmark_series"]
