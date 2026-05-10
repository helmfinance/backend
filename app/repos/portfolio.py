"""DB query helpers for /portfolio and /redemptions endpoints."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Agent,
    DividendClaim,
    DividendEpoch,
    FounderVault,
    Holder,
    RedemptionRequest,
)
from app.repos.agents import get_latest_nav


def get_holdings_by_address(db: Session, address: str) -> list[Holder]:
    stmt = (
        select(Holder)
        .where(Holder.address == address)
        .options(selectinload(Holder.agent))
    )
    return list(db.execute(stmt).scalars())


def get_pending_dividend_claims_by_address(
    db: Session, address: str
) -> list[DividendClaim]:
    stmt = select(DividendClaim).where(
        DividendClaim.holder_address == address,
        DividendClaim.claimed.is_(False),
    )
    return list(db.execute(stmt).scalars())


def get_pending_dividends_grouped(
    db: Session, address: str
) -> list[tuple[Agent, list[DividendClaim], dict[int, DividendEpoch]]]:
    """Group unclaimed claims by agent, with the matching epoch rows attached."""
    claims = get_pending_dividend_claims_by_address(db, address)
    if not claims:
        return []

    by_agent: dict[int, list[DividendClaim]] = {}
    for c in claims:
        by_agent.setdefault(c.agent_id, []).append(c)

    agent_ids = list(by_agent)
    agents = {
        a.agent_id: a
        for a in db.execute(
            select(Agent).where(Agent.agent_id.in_(agent_ids))
        ).scalars()
    }

    out: list[tuple[Agent, list[DividendClaim], dict[int, DividendEpoch]]] = []
    for aid, cs in by_agent.items():
        epoch_nums = [c.epoch for c in cs]
        epochs = {
            e.epoch: e
            for e in db.execute(
                select(DividendEpoch).where(
                    DividendEpoch.agent_id == aid,
                    DividendEpoch.epoch.in_(epoch_nums),
                )
            ).scalars()
        }
        out.append((agents[aid], cs, epochs))
    return out


def get_redemption_requests_by_address(
    db: Session, address: str
) -> list[RedemptionRequest]:
    """Pending only — Claimed/Cancelled are excluded from portfolio views."""
    stmt = (
        select(RedemptionRequest)
        .where(
            RedemptionRequest.holder_address == address,
            RedemptionRequest.status == "Pending",
        )
        .options(selectinload(RedemptionRequest.agent))
    )
    return list(db.execute(stmt).scalars())


def get_founder_vaults_by_address(db: Session, address: str) -> list[FounderVault]:
    stmt = (
        select(FounderVault)
        .join(Agent, Agent.agent_id == FounderVault.agent_id)
        .where(Agent.founder_address == address)
        .options(selectinload(FounderVault.agent))
    )
    return list(db.execute(stmt).scalars())


def get_position_value_usdc(db: Session, agent_id: int, shares: str) -> str:
    """shares (18-dec atomic) × latest nav_per_share_usdc (6-dec) → 6-dec USDC."""
    nav = get_latest_nav(db, agent_id)
    if nav is None:
        return "0"
    return str(int(shares) * int(nav.nav_per_share_usdc) // 10**18)


__all__ = [
    "get_holdings_by_address",
    "get_pending_dividend_claims_by_address",
    "get_pending_dividends_grouped",
    "get_redemption_requests_by_address",
    "get_founder_vaults_by_address",
    "get_position_value_usdc",
]
