"""Mandate emergencyExitConditions DSL evaluator.

DSL grammar::

    <METRIC> <OP> <VALUE>

Metrics: BTC_PRICE / ETH_PRICE / BTC_FUNDING_RATE / BTC_24H_CHANGE /
ETH_24H_CHANGE / DRAWDOWN. Ops: > < >= <= ==. Value: signed decimal.

Free-text conditions remain in the mandate but evaluate as ``parsed=false``
so the FE can show them as "informational only".
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.db.models import Agent
from app.repos import analytics
from app.services.coingecko_client import get_market_metrics

CONDITION_RE = re.compile(
    r"^\s*(\w+)\s*(>=|<=|==|>|<)\s*(-?\d+\.?\d*)\s*$"
)

SUPPORTED_METRICS = {
    "BTC_PRICE",
    "ETH_PRICE",
    "BTC_FUNDING_RATE",
    "BTC_24H_CHANGE",
    "ETH_24H_CHANGE",
    "DRAWDOWN",
}

OPS = {
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
}


def parse_condition(s: str) -> tuple[str, str, float] | None:
    """Returns (metric, op, value) or None when the string doesn't match the DSL."""
    m = CONDITION_RE.match(s)
    if not m:
        return None
    metric, op, raw_value = m.group(1), m.group(2), m.group(3)
    if metric not in SUPPORTED_METRICS:
        return None
    return metric, op, float(raw_value)


def evaluate_conditions(db: Session, agent_id: int) -> list[dict]:
    """Returns one dict per mandate condition with parse/eval state.

    Empty list when the agent has no mandate or no conditions.
    """
    agent = db.get(Agent, agent_id)
    if not agent:
        return []

    mandate = agent.mandate or {}
    raw_conditions = (
        mandate.get("emergencyExitConditions")
        or mandate.get("emergency_exit_conditions")
        or []
    )
    if not raw_conditions:
        return []

    metrics = get_market_metrics()
    perf = analytics.compute_performance(db, agent_id)
    max_dd = perf.get("max_drawdown")
    metrics["DRAWDOWN"] = abs(max_dd) if max_dd is not None else None

    results: list[dict] = []
    for raw in raw_conditions:
        parsed = parse_condition(raw)
        if parsed is None:
            results.append({
                "condition": raw,
                "parsed": False,
                "current_value": None,
                "threshold": None,
                "triggered": False,
                "note": "Unparseable condition (free-text)",
            })
            continue

        metric, op, threshold = parsed
        current = metrics.get(metric)
        if current is None:
            results.append({
                "condition": raw,
                "parsed": True,
                "current_value": None,
                "threshold": threshold,
                "triggered": False,
                "note": f"Metric {metric} unavailable",
            })
            continue

        results.append({
            "condition": raw,
            "parsed": True,
            "current_value": current,
            "threshold": threshold,
            "triggered": OPS[op](current, threshold),
            "note": None,
        })

    return results


__all__ = [
    "SUPPORTED_METRICS",
    "parse_condition",
    "evaluate_conditions",
]
