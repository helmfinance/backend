"""
Bootstrap two demo agents on-chain.

Replaces the legacy fixture-row seed. Every demo agent now has a real chain
vault: register → whitelist → (TEC only) deposit + advance + services. The
indexer fills NavPoint / Position / Decision / Holder / DividendEpoch /
FounderVault rows automatically as it picks up the chain events.

Idempotent: per-mandate-hash guard skips agents already in the DB, so
``entrypoint.sh`` can call this on every Railway boot without re-registering.

Produces (after indexer catches up):
    TEC   (chain agentId N)   — PublicLaunch, deposit + rebalance + harvest + distribute + nft
    DTECH (chain agentId N+1) — Incubation, founder seed only

Each agent's chain ``agentId`` is assigned by ``registry._nextAgentId``; the
BE DB uses the same value as its primary key. Legacy IDs 9001/9002 are gone.

Run:
    python -m scripts.seed              # called by scripts/entrypoint.sh
"""

from __future__ import annotations

import sys

from app.db import SessionLocal, models
from app.mandate.hash import compute_mandate_hash
from app.repos.agents import sweep_stale_agents
from scripts.e2e_demo import (
    check_environment,
    step1_5_whitelist_vault,
    step1_register_agent,
    step2_deposit,
    step3_advance_phase,
    step4_run_services,
)

USDC = 10**6


TEC_MANDATE: dict = {
    "version": "1.0",
    "name": "Tech Equity Composite",
    "ticker": "TEC",
    "description": "Diversified synthetic tech equity basket with USDY treasury sleeve.",
    "assetClasses": ["equity", "treasury"],
    "targetUniverse": ["sNVDA", "sMSFT", "sAAPL", "USDY"],
    "weightConstraints": [
        {"asset": "sNVDA", "minBps": 2000, "maxBps": 4000},
        {"asset": "sMSFT", "minBps": 1500, "maxBps": 3500},
        {"asset": "sAAPL", "minBps": 1500, "maxBps": 3500},
        {"asset": "USDY",  "minBps": 1500, "maxBps": 3500},
    ],
    "rebalanceFrequency": "weekly",
    "rebalanceTriggers": ["weight drift > 500 bps", "scheduled weekly"],
    "allowedLockups": ["instant", "30d", "60d", "90d"],
    "minimumDepositUsdc": str(10 * USDC),
    "founderShareBps": 500,
    "carryBps": 1000,
    "founderLockupDays": 180,
    "subordinationThresholdBps": 500,
    "maxLeverage": 1.0,
    "maxSinglePositionBps": 4000,
    "emergencyExitConditions": [
        "drawdown > 25% over 7d",
        "BTC_24H_CHANGE < -0.15",
    ],
    "expectedYieldAPY": "3-4% APY",
    "personalityHint": "growth-aggressive",
}


DTECH_MANDATE: dict = {
    "version": "1.0",
    "name": "Defensive Tech",
    "ticker": "DTECH",
    "description": "Single-name sNVDA exposure with USDY treasury reserve.",
    "assetClasses": ["equity", "treasury"],
    "targetUniverse": ["sNVDA", "USDY"],
    "weightConstraints": [
        {"asset": "sNVDA", "minBps": 4000, "maxBps": 6000},
        {"asset": "USDY",  "minBps": 4000, "maxBps": 6000},
    ],
    "rebalanceFrequency": "monthly",
    "rebalanceTriggers": ["weight drift > 1000 bps"],
    "allowedLockups": ["30d", "90d"],
    "minimumDepositUsdc": str(10 * USDC),
    "founderShareBps": 1000,
    "carryBps": 1000,
    "founderLockupDays": 180,
    "subordinationThresholdBps": 1000,
    "maxLeverage": 1.0,
    "maxSinglePositionBps": 6000,
    "emergencyExitConditions": [
        "drawdown > 15% over 14d",
    ],
    "expectedYieldAPY": "4-5% APY",
    "personalityHint": "yield-focused",
}


def _is_seeded(mandate_hash: str) -> bool:
    """True iff a row with this mandate_hash already exists in DB."""
    with SessionLocal() as db:
        return (
            db.query(models.Agent).filter_by(mandate_hash=mandate_hash).first()
            is not None
        )


def _seed_one(mandate: dict, *, full_lifecycle: bool, deposit_amount: int) -> None:
    """Register the agent on-chain, optionally run the full lifecycle.

    ``full_lifecycle=True`` walks step2 (deposit) → step3 (timeProvider
    advance + advanceToPublic) → step4 (rebalance + harvest + distribute +
    nft_metadata), each via the same helpers e2e_demo.py uses.
    """
    mandate_hash = compute_mandate_hash(mandate)
    if _is_seeded(mandate_hash):
        print(f"[seed] {mandate['ticker']} already seeded "
              f"(hash {mandate_hash[:10]}…) — skip")
        return

    print(f"[seed] registering {mandate['ticker']}...")
    step1 = step1_register_agent(mandate)
    agent_id = step1["agent_id"]
    vault_addr = step1["vault_addr"]

    step1_5_whitelist_vault(vault_addr)

    if full_lifecycle:
        print(f"[seed] {mandate['ticker']} → full lifecycle "
              f"(deposit + advance + services)")
        step2_deposit(agent_id, deposit_amount)
        step3_advance_phase(agent_id)
        step4_run_services(agent_id)
    else:
        print(f"[seed] {mandate['ticker']} stays in Incubation (register only)")


def main() -> None:
    print("=" * 60)
    print("Helm seed — chain-backed demo agents")
    print("=" * 60)

    check_environment()
    print()

    # Sweep stale agents first — protects against entrypoint.sh ordering:
    # FastAPI lifespan also calls this on startup, but seed runs in the
    # background and can race with it. Calling sweep here makes seed
    # self-sufficient: stale rows (vault missing on-chain or pointing at a
    # prior registry deploy) are deleted before the mandate_hash idempotent
    # check, so a seeded mandate that's now stale gets re-registered.
    with SessionLocal() as db:
        stats = sweep_stale_agents(db)
    if stats["removed"]:
        print(f"[seed] swept {len(stats['removed'])} stale agent(s): "
              f"{stats['removed']}")
    else:
        print(f"[seed] sweep clean: {stats['kept']} valid agent(s)")
    print()

    try:
        _seed_one(TEC_MANDATE,   full_lifecycle=True,  deposit_amount=100 * USDC)
        print()
        _seed_one(DTECH_MANDATE, full_lifecycle=False, deposit_amount=0)
        print()
        print("=" * 60)
        print("✓ Seed complete")
        print("=" * 60)
    except Exception as e:
        print()
        print("=" * 60)
        print(f"✗ Seed failed: {type(e).__name__}: {e}")
        print("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()
