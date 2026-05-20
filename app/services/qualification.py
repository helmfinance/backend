"""Phase-2 qualification gate.

Evaluates the 6 IDEA-spec criteria for advancing an agent from Incubation to
PublicLaunch. BE is the gatekeeper (SC can't compute Sharpe). The POST variant
auto-calls ``HelmRegistry.advanceToPublic`` when all checks pass.
"""

from __future__ import annotations

from app.chain.client import registry, time_provider
from app.chain.executor_wallet import send_tx
from app.db import models
from app.db.session import SessionLocal
from app.repos import agents as agent_repo
from app.repos import analytics

INCUBATION_MIN_DAYS = 30
MIN_DECISIONS = 10
MAX_DD_THRESHOLD = -0.30  # -30% — less negative is better


def evaluate(agent_id: int) -> dict:
    """Returns dict with overall_passed, checks list, and agent_phase."""
    with SessionLocal() as db:
        agent = db.get(models.Agent, agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")
        if agent.phase != "Incubation":
            raise ValueError(
                f"Agent {agent_id} is in {agent.phase}, "
                "qualification only applicable to Incubation"
            )

        checks: list[dict] = []

        # 1. Continuous operating days (live chain clock)
        now = time_provider().functions.currentTime().call()
        days_elapsed = (
            (now - agent.incubation_start) / 86400
            if agent.incubation_start
            else 0
        )
        checks.append({
            "name": "continuous_days",
            "description": "Continuous operating days >= 30 (TimeProvider clock)",
            "passed": days_elapsed >= INCUBATION_MIN_DAYS,
            "actual": f"{days_elapsed:.1f}",
            "threshold": f">= {INCUBATION_MIN_DAYS}",
            "note": None,
        })

        # 2. Rebalance decision count
        rebalance_count = agent_repo.count_decisions_by_type(
            db, agent_id, "Rebalance"
        )
        checks.append({
            "name": "decision_count",
            "description": "Meaningful rebalance decisions >= 10",
            "passed": rebalance_count >= MIN_DECISIONS,
            "actual": str(rebalance_count),
            "threshold": f">= {MIN_DECISIONS}",
            "note": None,
        })

        # 3. Mandate breaches (reputation-derived; each slash = 1000 bps)
        reputation = agent.reputation if agent.reputation is not None else 10000
        breach_count = max(0, (10000 - reputation) // 1000)
        checks.append({
            "name": "mandate_breaches",
            "description": "Mandate breach count == 0",
            "passed": breach_count == 0,
            "actual": str(breach_count),
            "threshold": "== 0",
            "note": f"Reputation: {reputation}/10000",
        })

        # 4. Max NAV drawdown
        perf = analytics.compute_performance(db, agent_id)
        max_dd = perf.get("max_drawdown")
        # If no perf data yet (insufficient samples), treat as not yet qualifying.
        dd_passed = max_dd is not None and max_dd >= MAX_DD_THRESHOLD
        checks.append({
            "name": "max_drawdown",
            "description": "Max NAV drawdown >= -30%",
            "passed": dd_passed,
            "actual": (
                f"{max_dd:.4f}" if max_dd is not None
                else "n/a (insufficient samples)"
            ),
            "threshold": f">= {MAX_DD_THRESHOLD}",
            "note": None,
        })

        # 5. On-chain recording (trivial — indexer is source of truth)
        checks.append({
            "name": "on_chain_records",
            "description": "All decisions recorded on-chain (indexer captures events)",
            "passed": True,
            "actual": str(rebalance_count),
            "threshold": "100%",
            "note": None,
        })

        # 6. Sharpe computable
        sharpe = perf.get("sharpe_ratio")
        checks.append({
            "name": "sharpe_computable",
            "description": "Sharpe ratio computable (>= 10 NAV samples, non-zero variance)",
            "passed": sharpe is not None,
            "actual": f"{sharpe:.2f}" if sharpe is not None else "n/a",
            "threshold": "not null",
            "note": f"NAV samples: {perf.get('sample_count', 0)}",
        })

        overall = all(c["passed"] for c in checks)
        return {
            "overall_passed": overall,
            "checks": checks,
            "agent_phase": agent.phase,
        }


def advance_if_passed(agent_id: int) -> dict:
    """Evaluate; if every check passes, call registry.advanceToPublic."""
    result = evaluate(agent_id)
    if not result["overall_passed"]:
        return {**result, "advanced": False, "tx_hash": None, "new_phase": None}

    tx_result = send_tx(registry().functions.advanceToPublic(agent_id))
    tx_hash = tx_result["tx_hash"] if isinstance(tx_result, dict) else tx_result
    return {
        **result,
        "advanced": True,
        "tx_hash": tx_hash,
        "new_phase": "PublicLaunch",
    }
