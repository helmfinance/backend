"""DB query helpers for agent read endpoints."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Agent,
    Decision,
    DividendEpoch,
    FounderVault,
    Holder,
    NarratorNote,
    NavPoint,
    Position,
    RedemptionRequest,
    WindDownState,
)
from app.schemas import AgentPhase, AssetClass, LockupTier, NavPeriod


_PERIOD_SECONDS: dict[NavPeriod, int | None] = {
    NavPeriod.H24: 24 * 3600,
    NavPeriod.D7: 7 * 86400,
    NavPeriod.D30: 30 * 86400,
    NavPeriod.All: None,
}


def list_agents(
    db: Session,
    *,
    phase: list[AgentPhase] | None = None,
    asset_class: list[AssetClass] | None = None,
    lockup: list[LockupTier] | None = None,
    sort: str = "apy_30d",
    order: str = "desc",
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Agent], int]:
    """Returns (agents_after_pagination, total_after_filter_before_pagination)."""
    rows = list(db.execute(select(Agent)).scalars())

    if phase is None:
        rows = [a for a in rows if a.phase != AgentPhase.Slashed.value]
    else:
        wanted = {p.value for p in phase}
        rows = [a for a in rows if a.phase in wanted]

    if asset_class:
        targets = {c.value for c in asset_class}
        rows = [a for a in rows if set(a.mandate.get("asset_classes", [])) & targets]

    if lockup:
        targets = {lk.value for lk in lockup}
        rows = [a for a in rows if set(a.mandate.get("allowed_lockups", [])) & targets]

    descending = order == "desc"

    if sort in ("apy_30d", "apy_7d"):
        days = 30 if sort == "apy_30d" else 7
        with_val: list[tuple[Agent, int]] = []
        none_val: list[Agent] = []
        for a in rows:
            v = compute_apy_bps(db, a.agent_id, days)
            if v is None:
                none_val.append(a)
            else:
                with_val.append((a, v))
        with_val.sort(key=lambda t: t[1], reverse=descending)
        rows = [t[0] for t in with_val] + none_val
    elif sort in ("sharpe", "total_return", "max_drawdown"):
        from app.repos import analytics  # avoid top-level cycle risk

        perf_key = {
            "sharpe": "sharpe_ratio",
            "total_return": "total_return",
            "max_drawdown": "max_drawdown",
        }[sort]
        with_perf: list[tuple[Agent, float]] = []
        no_perf: list[Agent] = []
        for a in rows:
            v = analytics.compute_performance(db, a.agent_id).get(perf_key)
            if v is None:
                no_perf.append(a)
            else:
                with_perf.append((a, v))
        with_perf.sort(key=lambda t: t[1], reverse=descending)
        rows = [t[0] for t in with_perf] + no_perf
    else:
        def key(a: Agent):
            if sort in ("newest", "created_at"):
                return a.created_at
            if sort == "reputation":
                return a.reputation
            if sort == "nav":
                nav = get_latest_nav(db, a.agent_id)
                return int(nav.nav_usdc) if nav else 0
            if sort == "holders":
                return compute_holder_count(db, a.agent_id)
            return a.created_at  # fallback

        rows.sort(key=key, reverse=descending)

    total = len(rows)
    return rows[offset : offset + limit], total


def get_agent(db: Session, agent_id: int) -> Agent | None:
    stmt = (
        select(Agent)
        .where(Agent.agent_id == agent_id)
        .options(
            selectinload(Agent.positions),
            selectinload(Agent.founder_vault),
            selectinload(Agent.wind_down),
        )
    )
    return db.execute(stmt).scalar_one_or_none()


def get_recent_dividends(db: Session, agent_id: int, n: int = 5) -> list[DividendEpoch]:
    stmt = (
        select(DividendEpoch)
        .where(DividendEpoch.agent_id == agent_id)
        .order_by(DividendEpoch.distributed_at.desc())
        .limit(n)
    )
    return list(db.execute(stmt).scalars())


def get_recent_decisions(db: Session, agent_id: int, n: int = 5) -> list[Decision]:
    stmt = (
        select(Decision)
        .where(Decision.agent_id == agent_id)
        .order_by(Decision.timestamp.desc())
        .limit(n)
    )
    return list(db.execute(stmt).scalars())


def list_decisions_paginated(
    db: Session,
    agent_id: int,
    *,
    type_: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Decision], int]:
    """Returns (decisions, total_count_for_filter). Newest-first."""
    from sqlalchemy import func
    base = select(Decision).where(Decision.agent_id == agent_id)
    count_base = select(func.count(Decision.id)).where(Decision.agent_id == agent_id)
    if type_:
        base = base.where(Decision.type == type_)
        count_base = count_base.where(Decision.type == type_)
    base = base.order_by(Decision.timestamp.desc()).limit(limit).offset(offset)
    rows = list(db.execute(base).scalars())
    total = db.execute(count_base).scalar_one()
    return rows, total


def get_latest_narrator_note(db: Session, agent_id: int) -> NarratorNote | None:
    stmt = (
        select(NarratorNote)
        .where(NarratorNote.agent_id == agent_id)
        .order_by(NarratorNote.week_start.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_redemption_queue_snapshot(db: Session, agent_id: int) -> dict:
    rows = list(
        db.execute(
            select(RedemptionRequest).where(
                RedemptionRequest.agent_id == agent_id,
                RedemptionRequest.status == "Pending",
            )
        ).scalars()
    )
    pending_shares = sum(int(r.shares) for r in rows)
    next_unlock = min((r.unlock_at for r in rows), default=None)
    return {
        "pending_shares": str(pending_shares),
        "request_count": len(rows),
        "next_unlock_at": next_unlock,
    }


def get_latest_nav(db: Session, agent_id: int) -> NavPoint | None:
    stmt = (
        select(NavPoint)
        .where(NavPoint.agent_id == agent_id)
        .order_by(NavPoint.timestamp.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def compute_apy_bps(db: Session, agent_id: int, days: int) -> int | None:
    """latest nav_per_share vs ~`days` ago (±1 day tolerance), annualized.

    Returns None when the historical row is missing or yields a non-finite
    figure. Result is clamped into BasisPoints (0..10000); APYs above 100%
    are reported as 10000 rather than failing schema validation.
    """
    points = list(
        db.execute(
            select(NavPoint)
            .where(NavPoint.agent_id == agent_id)
            .order_by(NavPoint.timestamp.desc())
        ).scalars()
    )
    if not points:
        return None

    latest = points[0]
    target_ts = latest.timestamp - days * 86400
    candidates = [p for p in points if abs(p.timestamp - target_ts) <= 86400]
    if not candidates:
        return None
    closest = min(candidates, key=lambda p: abs(p.timestamp - target_ts))

    nps_now = int(latest.nav_per_share_usdc)
    nps_then = int(closest.nav_per_share_usdc)
    if nps_then <= 0:
        return None

    actual_days = (latest.timestamp - closest.timestamp) / 86400
    if actual_days <= 0:
        return None

    annualized = (nps_now / nps_then) ** (365 / actual_days) - 1
    bps = round(annualized * 10000)
    return max(0, min(10000, bps))


def compute_holder_count(db: Session, agent_id: int) -> int:
    return int(
        db.execute(
            select(func.count()).select_from(Holder).where(Holder.agent_id == agent_id)
        ).scalar_one()
    )


def count_decisions_by_type(db: Session, agent_id: int, decision_type: str) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(Decision)
            .where(Decision.agent_id == agent_id, Decision.type == decision_type)
        ).scalar_one()
    )


def count_active_holders(db: Session, agent_id: int) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(Holder)
            .where(Holder.agent_id == agent_id, Holder.balance != "0")
        ).scalar_one()
    )


def sum_harvested_usdc(db: Session, agent_id: int) -> int:
    rows = db.execute(
        select(Decision).where(
            Decision.agent_id == agent_id, Decision.type == "Harvest"
        )
    ).scalars()
    return sum(int(r.harvested_usdc or 0) for r in rows)


def get_nav_history(
    db: Session, agent_id: int, period: NavPeriod
) -> list[NavPoint]:
    stmt = select(NavPoint).where(NavPoint.agent_id == agent_id)

    window = _PERIOD_SECONDS[period]
    if window is not None:
        import time

        cutoff = int(time.time()) - window
        stmt = stmt.where(NavPoint.timestamp >= cutoff)

    stmt = stmt.order_by(NavPoint.timestamp.asc())
    return list(db.execute(stmt).scalars())


def get_pyth_feeds_for_agent(
    db: Session, agent_id: int
) -> list[tuple[str, str]]:
    """[(symbol, feed_id), ...] for positions backed by a Pyth feed.

    mETH / USDY positions are skipped (no Pyth feed). Order follows the row
    order in `positions` so callers can zip with updateData deterministically.
    """
    from app.hermes.client import FEED_BY_SYMBOL

    positions = list(
        db.execute(
            select(Position).where(Position.agent_id == agent_id)
        ).scalars()
    )
    out: list[tuple[str, str]] = []
    for p in positions:
        feed = FEED_BY_SYMBOL.get(p.symbol)
        if feed:
            out.append((p.symbol, feed))
    return out


def sweep_stale_agents(db: Session) -> dict:
    """Delete agent rows whose vault is missing on-chain or points at a
    different registry than the BE's configured ``settings.helm_registry``.

    Called at FastAPI startup so a registry re-deploy doesn't leave the BE
    serving stale vault addresses to clients. seed.py runs after this
    (entrypoint.sh order) so any deleted demo agent is immediately
    re-registered against the current chain config.

    Returns ``{"kept": int, "removed": [(agent_id, reason), ...]}``. The
    cascaded deletion follows the ``cascade="all, delete-orphan"`` setup on
    every Agent → child relationship.
    """
    from web3 import Web3

    from app.chain.client import agent_vault, get_w3
    from app.config import settings

    expected_registry = Web3.to_checksum_address(settings.helm_registry)
    w3 = get_w3()

    kept = 0
    removed: list[tuple[int, str]] = []
    rows = list(db.execute(select(Agent)).scalars())

    for agent in rows:
        try:
            vault_addr = Web3.to_checksum_address(agent.vault_address)
        except ValueError:
            removed.append((agent.agent_id, "invalid-vault-address"))
            db.delete(agent)
            continue

        code = w3.eth.get_code(vault_addr)
        if not code or len(code) <= 2:
            removed.append((agent.agent_id, "no-code"))
            db.delete(agent)
            continue

        try:
            actual_registry = Web3.to_checksum_address(
                agent_vault(vault_addr).functions.registry().call()
            )
        except Exception as e:  # noqa: BLE001
            removed.append((agent.agent_id, f"registry-call-failed: {type(e).__name__}"))
            db.delete(agent)
            continue

        if actual_registry != expected_registry:
            removed.append(
                (agent.agent_id, f"stale-registry={actual_registry[:10]}…")
            )
            db.delete(agent)
        else:
            kept += 1

    if removed:
        db.commit()

    return {"kept": kept, "removed": removed}


__all__ = [
    "FounderVault",
    "WindDownState",
    "list_agents",
    "get_agent",
    "get_recent_dividends",
    "get_recent_decisions",
    "get_latest_narrator_note",
    "get_redemption_queue_snapshot",
    "get_latest_nav",
    "compute_apy_bps",
    "compute_holder_count",
    "count_decisions_by_type",
    "count_active_holders",
    "sum_harvested_usdc",
    "get_nav_history",
    "get_pyth_feeds_for_agent",
    "sweep_stale_agents",
]
