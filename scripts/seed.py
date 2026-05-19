"""
Seed Helm DB with two demo agents.

Usage:
    python -m scripts.seed              # wipe + re-seed (idempotent)
    python -m scripts.seed --append     # keep existing rows, just add demo data

Produces:
    Agent 1 (TEC, PublicLaunch) — 4 positions, 30d NAV, 5 decisions,
        1 dividend epoch + 3 holders + 3 claims, founder vault, 1 narrator note.
    Agent 2 (DTECH, Incubation) — 2 positions, 12d NAV, 1 decision,
        founder = sole holder.
    IndexerState(chain_id=5003).
"""

from __future__ import annotations

import argparse
import hashlib
import time

from sqlalchemy import delete

from app.db import (
    Agent,
    Decision,
    DividendClaim,
    DividendEpoch,
    FounderVault,
    Holder,
    IndexerState,
    NarratorNote,
    NavPoint,
    Position,
    RedemptionRequest,
    SessionLocal,
    WindDownState,
)
from app.db.models import MandateBlob

USDC = 10**6
SHARES = 10**18
ZERO_ADDR = "0x" + "0" * 40


def addr(seed: str) -> str:
    """Deterministic 20-byte address derived from a seed string."""
    return "0x" + hashlib.sha256(seed.encode()).hexdigest()[:40]


def hash66(seed: str) -> str:
    """Deterministic 32-byte hash derived from a seed string."""
    return "0x" + hashlib.sha256(seed.encode()).hexdigest()[:64]


def _wipe(db) -> None:
    # Order matters: child rows first, then dividend_epochs, then agents.
    for model in (
        DividendClaim,
        Position,
        NavPoint,
        Decision,
        DividendEpoch,
        Holder,
        RedemptionRequest,
        NarratorNote,
        FounderVault,
        WindDownState,
        MandateBlob,
        IndexerState,
        Agent,
    ):
        db.execute(delete(model))


def _agent1_mandate() -> dict:
    return {
        "version": "1.0",
        "name": "Tech Equity Composite",
        "ticker": "TEC",
        "description": "Diversified synthetic tech equity basket with mETH yield sleeve.",
        "asset_classes": ["equity", "crypto"],
        "target_universe": ["sNVDA", "sMSFT", "sAAPL", "mETH"],
        "weight_constraints": [
            {"asset": "sNVDA", "min_bps": 2000, "max_bps": 4000},
            {"asset": "sMSFT", "min_bps": 1500, "max_bps": 3500},
            {"asset": "sAAPL", "min_bps": 1500, "max_bps": 3500},
            {"asset": "mETH", "min_bps": 1000, "max_bps": 3000},
        ],
        "rebalance_frequency": "weekly",
        "rebalance_triggers": ["weight drift > 500 bps", "scheduled weekly"],
        "allowed_lockups": ["instant", "30d", "60d", "90d"],
        "minimum_deposit_usdc": str(100 * USDC),
        "founder_share_bps": 500,
        "carry_bps": 1000,
        "founder_lockup_days": 180,
        "subordination_threshold_bps": 500,
        "max_leverage": 1.0,
        "max_single_position_bps": 4000,
        "emergency_exit_conditions": [
            "drawdown > 25% over 7d",
            "Pyth feed stale > 60s",
        ],
    }


def _agent2_mandate() -> dict:
    return {
        "version": "1.0",
        "name": "Defensive Tech",
        "ticker": "DTECH",
        "description": "Single-name sNVDA exposure with USDY treasury reserve.",
        "asset_classes": ["equity", "treasury"],
        "target_universe": ["sNVDA", "USDY"],
        "weight_constraints": [
            {"asset": "sNVDA", "min_bps": 4000, "max_bps": 6000},
            {"asset": "USDY", "min_bps": 4000, "max_bps": 6000},
        ],
        "rebalance_frequency": "monthly",
        "rebalance_triggers": ["weight drift > 1000 bps"],
        "allowed_lockups": ["30d", "90d"],
        "minimum_deposit_usdc": str(500 * USDC),
        "founder_share_bps": 1000,
        "carry_bps": 1000,
        "founder_lockup_days": 180,
        "subordination_threshold_bps": 1000,
        "max_leverage": 1.0,
        "max_single_position_bps": 6000,
        "emergency_exit_conditions": ["drawdown > 15% over 14d"],
    }


def _seed_agent1(db, now: int) -> None:
    mandate = _agent1_mandate()
    mandate_hash = hash66("agent:9001:mandate")
    db.add(
        MandateBlob(
            mandate_hash=mandate_hash,
            mandate_json=mandate,
            raw_text="Diversified tech equity ETF, weekly rebalance, mETH yield sleeve.",
            ipfs_uri=f"ipfs://{mandate_hash[2:]}",
            pinned_at=now - 35 * 86400,
            created_at=now - 35 * 86400,
        )
    )

    incubation_start = now - 60 * 86400
    public_launch_at = now - 30 * 86400

    agent = Agent(
        agent_id=9001,
        name=mandate["name"],
        ticker=mandate["ticker"],
        founder_address="0x" + "0" * 36 + "f001",
        vault_address=addr("agent:1:vault"),
        token_address=addr("agent:1:token"),
        founder_vault_address=addr("agent:1:foundervault"),
        phase="PublicLaunch",
        incubation_start=incubation_start,
        public_launch_at=public_launch_at,
        mandate=mandate,
        mandate_uri=f"ipfs://{mandate_hash[2:]}",
        mandate_hash=mandate_hash,
        reputation=72,
        thumbnail_url="https://placehold.co/256x256?text=TEC",
        created_at=incubation_start,
    )
    db.add(agent)

    # 4 positions, weights sum to 10000
    total_shares = 1_000_000 * SHARES  # 1M AGT
    nav_per_share = 1_043_000  # 1.043 USDC (6 dec)
    nav_total = total_shares * nav_per_share // SHARES  # in USDC 6 dec

    positions_spec = [
        ("sNVDA", "equity", 3000, addr("asset:sNVDA"), 850 * USDC // 100),  # ~$8.50 syn-share
        ("sMSFT", "equity", 2500, addr("asset:sMSFT"), 420 * USDC // 100),
        ("sAAPL", "equity", 2500, addr("asset:sAAPL"), 190 * USDC // 100),
        ("mETH", "crypto", 2000, addr("asset:mETH"), 3500 * USDC),
    ]
    for symbol, asset_class, weight_bps, asset_addr, price in positions_spec:
        value = nav_total * weight_bps // 10000
        amount_units = value * SHARES // price if price > 0 else 0
        db.add(
            Position(
                agent_id=9001,
                asset_address=asset_addr,
                symbol=symbol,
                asset_class=asset_class,
                amount=str(amount_units),
                value_usdc=str(value),
                weight_bps=weight_bps,
                price_usdc=str(price),
                price_updated_at=now - 60,
                price_stale=False,
                updated_at=now - 60,
            )
        )

    # NAV history: 30 days, 1.000 → 1.043
    start_nav = 1_000_000
    end_nav = 1_043_000
    days = 30
    for i in range(days + 1):
        ts = public_launch_at + i * 86400
        nav_ps = start_nav + (end_nav - start_nav) * i // days
        nav_usdc = total_shares * nav_ps // SHARES
        db.add(
            NavPoint(
                agent_id=9001,
                timestamp=ts,
                nav_usdc=str(nav_usdc),
                nav_per_share_usdc=str(nav_ps),
                total_shares=str(total_shares),
            )
        )

    # 5 decisions: 3 rebalance, 1 harvest, 1 distribute
    decisions_spec = [
        ("Rebalance", public_launch_at + 7 * 86400, "Trim sNVDA from 35% to 30%; add sAAPL."),
        ("Rebalance", public_launch_at + 14 * 86400, "Rebalance to mandate weights."),
        ("Harvest", public_launch_at + 18 * 86400, "Harvested mETH staking yield."),
        ("Rebalance", public_launch_at + 21 * 86400, "Mid-cycle drift correction."),
        ("Distribute", public_launch_at + 28 * 86400, "Distributed epoch 1 to holders."),
    ]
    for i, (kind, ts, summary) in enumerate(decisions_spec):
        tx = hash66(f"agent:9001:tx:{i}")
        kwargs = {
            "id": f"{tx}:{i}",
            "agent_id": 1,
            "type": kind,
            "timestamp": ts,
            "tx_hash": tx,
            "block_number": 1_000_000 + i * 17_280,
            "summary": summary,
        }
        if kind == "Rebalance":
            kwargs["nav_before"] = str(nav_total - 5_000 * USDC)
            kwargs["nav_after"] = str(nav_total)
            kwargs["before_positions"] = []
            kwargs["after_positions"] = []
        elif kind == "Harvest":
            kwargs["harvested_usdc"] = str(8_500 * USDC)
            kwargs["harvested_from_sources"] = [
                {"source": addr("source:meth"), "amount": str(8_500 * USDC)},
            ]
        elif kind == "Distribute":
            kwargs["distributed_epoch"] = 1
            kwargs["distributed_holders_usdc"] = str(90_000 * USDC)
            kwargs["distributed_carry_usdc"] = str(10_000 * USDC)
        db.add(Decision(**kwargs))

    # 1 dividend epoch
    epoch_ts = public_launch_at + 28 * 86400
    db.add(
        DividendEpoch(
            agent_id=9001,
            epoch=1,
            total_amount_usdc=str(100_000 * USDC),
            holders_share_usdc=str(90_000 * USDC),
            carry_share_usdc=str(10_000 * USDC),
            distributed_at=epoch_ts,
            total_shares_at_snapshot=str(total_shares),
        )
    )

    # 3 holders + 3 claims (test wallets used by /portfolio gates)
    holder_specs = [
        ("0x" + "11" * 20, 5000, 500_000),  # 50% weight, 50k claim
        ("0x" + "22" * 20, 3000, 300_000),
        ("0x" + "33" * 20, 2000, 200_000),
    ]
    holder_first_at = public_launch_at + 86400
    for h_addr, weight_bps, claim_units in holder_specs:
        balance = total_shares * weight_bps // 10000
        db.add(
            Holder(
                agent_id=9001,
                address=h_addr,
                balance=str(balance),
                weight_bps=weight_bps,
                first_held_at=holder_first_at,
                cumulative_dividends_claimed_usdc="0",
            )
        )
        db.add(
            DividendClaim(
                agent_id=9001,
                epoch=1,
                holder_address=h_addr,
                amount_usdc=str(claim_units * USDC // 1000 * 1000),  # cosmetic
                claimed=False,
                claimed_at=None,
            )
        )

    # Redemption requests (drives /portfolio + /redemptions gates)
    redemption_specs = [
        # (request_id, holder, shares, requested_offset_d, status, claim_offset_d)
        (1, "0x" + "11" * 20, 100 * SHARES, -5,  "Pending", None),
        (2, "0x" + "22" * 20, 200 * SHARES, -35, "Pending", None),
        (3, "0x" + "33" * 20, 50  * SHARES, -60, "Claimed", -25),
    ]
    for rid, h_addr, shares, req_off_d, status, claim_off_d in redemption_specs:
        requested_at = now + req_off_d * 86400
        unlock_at = requested_at + 30 * 86400
        estimated = shares * nav_per_share // SHARES
        db.add(
            RedemptionRequest(
                request_id=rid,
                agent_id=9001,
                holder_address=h_addr,
                shares=str(shares),
                tier="30d",
                requested_at=requested_at,
                unlock_at=unlock_at,
                estimated_usdc=str(estimated),
                status=status,
                claimed_at=(now + claim_off_d * 86400) if claim_off_d is not None else None,
            )
        )

    # Founder vault, 6mo lockup
    db.add(
        FounderVault(
            agent_id=9001,
            address=addr("agent:1:foundervault"),
            shares_held=str(50_000 * SHARES),  # 5% founder share
            lockup_ends_at=public_launch_at + 180 * 86400,
            cumulative_withdrawn_bps=0,
            is_subordination_active=False,
            carry_balance_usdc=str(10_000 * USDC),
        )
    )

    # 1 narrator note
    week_start = public_launch_at + 21 * 86400
    week_end = week_start + 7 * 86400
    db.add(
        NarratorNote(
            agent_id=9001,
            week_start=week_start,
            week_end=week_end,
            generated_at=week_end + 3600,
            body_markdown=(
                "**Week recap.** TEC posted +1.4% on sNVDA-led tape; mETH harvest "
                "of $8.5k swelled the yield pool. Rebalance trimmed sNVDA back to "
                "policy weight."
            ),
            nav_start=str(total_shares * 1_028_000 // SHARES),
            nav_end=str(total_shares * 1_043_000 // SHARES),
            return_bps=146,
        )
    )


def _seed_agent2(db, now: int) -> None:
    mandate = _agent2_mandate()
    mandate_hash = hash66("agent:9002:mandate")
    db.add(
        MandateBlob(
            mandate_hash=mandate_hash,
            mandate_json=mandate,
            raw_text="Defensive tech: sNVDA + USDY treasury reserve.",
            ipfs_uri=f"ipfs://{mandate_hash[2:]}",
            pinned_at=now - 12 * 86400,
            created_at=now - 12 * 86400,
        )
    )

    incubation_start = now - 12 * 86400

    agent = Agent(
        agent_id=9002,
        name=mandate["name"],
        ticker=mandate["ticker"],
        founder_address=addr("agent:2:founder"),
        vault_address=addr("agent:2:vault"),
        token_address=addr("agent:2:token"),
        founder_vault_address=addr("agent:2:foundervault"),
        phase="Incubation",
        incubation_start=incubation_start,
        public_launch_at=None,
        mandate=mandate,
        mandate_uri=f"ipfs://{mandate_hash[2:]}",
        mandate_hash=mandate_hash,
        reputation=20,
        thumbnail_url=None,
        created_at=incubation_start,
    )
    db.add(agent)

    # Founder = sole holder. Seed = 50k USDC.
    total_shares = 50_000 * SHARES
    nav_per_share = 1_005_000
    nav_total = total_shares * nav_per_share // SHARES

    # 2 positions: sNVDA seed + USDY reserve.
    positions_spec = [
        ("sNVDA", "equity", 5000, addr("asset:sNVDA"), 850 * USDC // 100),
        ("USDY",  "treasury", 5000, addr("asset:USDY"), 1 * USDC),
    ]
    for symbol, asset_class, weight_bps, asset_addr, price in positions_spec:
        value = nav_total * weight_bps // 10000
        amount_units = value * SHARES // price if price > 0 else 0
        db.add(
            Position(
                agent_id=9002,
                asset_address=asset_addr,
                symbol=symbol,
                asset_class=asset_class,
                amount=str(amount_units),
                value_usdc=str(value),
                weight_bps=weight_bps,
                price_usdc=str(price),
                price_updated_at=now - 60,
                price_stale=False,
                updated_at=now - 60,
            )
        )

    # NAV history, 12 days
    start_nav = 1_000_000
    end_nav = 1_005_000
    days = 12
    for i in range(days + 1):
        ts = incubation_start + i * 86400
        nav_ps = start_nav + (end_nav - start_nav) * i // days
        nav_usdc = total_shares * nav_ps // SHARES
        db.add(
            NavPoint(
                agent_id=9002,
                timestamp=ts,
                nav_usdc=str(nav_usdc),
                nav_per_share_usdc=str(nav_ps),
                total_shares=str(total_shares),
            )
        )

    # 1 decision: founder seed mint logged as a Rebalance.
    tx = hash66("agent:9002:tx:0")
    db.add(
        Decision(
            id=f"{tx}:0",
            agent_id=9002,
            type="Rebalance",
            timestamp=incubation_start + 3600,
            tx_hash=tx,
            block_number=1_500_000,
            summary="Founder seeded vault: 50% sNVDA / 50% USDY.",
            before_positions=[],
            after_positions=[],
            nav_before=str(0),
            nav_after=str(nav_total),
        )
    )

    # Founder = sole holder.
    founder_addr = addr("agent:2:founder")
    db.add(
        Holder(
            agent_id=9002,
            address=founder_addr,
            balance=str(total_shares),
            weight_bps=10000,
            first_held_at=incubation_start,
            cumulative_dividends_claimed_usdc="0",
        )
    )
    db.add(
        FounderVault(
            agent_id=9002,
            address=addr("agent:2:foundervault"),
            shares_held=str(total_shares // 10),  # 10% founder share
            lockup_ends_at=incubation_start + 180 * 86400,
            cumulative_withdrawn_bps=0,
            is_subordination_active=False,
            carry_balance_usdc="0",
        )
    )


def seed(append: bool = False) -> None:
    now = int(time.time())
    with SessionLocal() as db:
        if not append:
            _wipe(db)
            db.flush()

        _seed_agent1(db, now)
        _seed_agent2(db, now)

        db.merge(
            IndexerState(
                chain_id=5003,
                last_synced_block=1_750_000,
                updated_at=now,
            )
        )
        db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--append",
        action="store_true",
        help="Skip the wipe step and just add demo rows.",
    )
    args = parser.parse_args()
    seed(append=args.append)
    print("Seed complete.")


if __name__ == "__main__":
    main()
