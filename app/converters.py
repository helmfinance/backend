"""ORM → Pydantic converters for read endpoints."""

from __future__ import annotations

from app import schemas
from app.db import models as db_models


def to_position(p: db_models.Position) -> schemas.Position:
    return schemas.Position(
        asset=p.asset_address,
        symbol=p.symbol,
        asset_class=schemas.AssetClass(p.asset_class),
        amount=p.amount,
        value_usdc=p.value_usdc,
        weight_bps=p.weight_bps,
        price_usdc=p.price_usdc,
        price_updated_at=p.price_updated_at,
        price_stale=p.price_stale,
    )


def to_nav_point(p: db_models.NavPoint) -> schemas.NavPoint:
    return schemas.NavPoint(
        timestamp=p.timestamp,
        nav_usdc=p.nav_usdc,
        nav_per_share_usdc=p.nav_per_share_usdc,
        total_shares=p.total_shares,
    )


def to_dividend_epoch(d: db_models.DividendEpoch) -> schemas.DividendEpoch:
    return schemas.DividendEpoch(
        epoch=d.epoch,
        agent_id=d.agent_id,
        total_amount_usdc=d.total_amount_usdc,
        holders_share_usdc=d.holders_share_usdc,
        carry_share_usdc=d.carry_share_usdc,
        distributed_at=d.distributed_at,
        total_shares_at_snapshot=d.total_shares_at_snapshot,
    )


def to_decision(d: db_models.Decision) -> schemas.Decision:
    before = (
        [schemas.Position.model_validate(x) for x in d.before_positions]
        if d.before_positions is not None
        else None
    )
    after = (
        [schemas.Position.model_validate(x) for x in d.after_positions]
        if d.after_positions is not None
        else None
    )
    sources = (
        [schemas.HarvestSource.model_validate(x) for x in d.harvested_from_sources]
        if d.harvested_from_sources is not None
        else None
    )
    return schemas.Decision(
        id=d.id,
        type=schemas.DecisionType(d.type),
        timestamp=d.timestamp,
        tx_hash=d.tx_hash,
        block_number=d.block_number,
        summary=d.summary,
        before_positions=before,
        after_positions=after,
        nav_before=d.nav_before,
        nav_after=d.nav_after,
        harvested_usdc=d.harvested_usdc,
        harvested_from_sources=sources,
        distributed_epoch=d.distributed_epoch,
        distributed_holders_usdc=d.distributed_holders_usdc,
        distributed_carry_usdc=d.distributed_carry_usdc,
    )


def to_narrator_note(n: db_models.NarratorNote) -> schemas.NarratorNote:
    return schemas.NarratorNote(
        generated_at=n.generated_at,
        week_start=n.week_start,
        week_end=n.week_end,
        body_markdown=n.body_markdown,
        performance=schemas.NarratorPerformance(
            nav_start=n.nav_start,
            nav_end=n.nav_end,
            return_bps=n.return_bps,
        ),
    )


def to_founder_vault_snapshot(fv: db_models.FounderVault) -> schemas.FounderVaultSnapshot:
    return schemas.FounderVaultSnapshot(
        address=fv.address,
        shares_held=fv.shares_held,
        lockup_ends_at=fv.lockup_ends_at,
        cumulative_withdrawn_bps=fv.cumulative_withdrawn_bps,
        is_subordination_active=fv.is_subordination_active,
        carry_balance_usdc=fv.carry_balance_usdc,
    )


def to_wind_down_state(wd: db_models.WindDownState) -> schemas.WindDownState:
    return schemas.WindDownState(
        triggered_at=wd.triggered_at,
        triggered_by=wd.triggered_by,
        reason=wd.reason,
        positions_remaining=wd.positions_remaining,
        estimated_settle_at=wd.estimated_settle_at,
        senior_claimable_usdc=wd.senior_claimable_usdc,
        junior_claimable_usdc=wd.junior_claimable_usdc,
    )


def _summary_kwargs(
    a: db_models.Agent,
    *,
    current_nav: db_models.NavPoint | None,
    apy_30d_bps: int | None,
    apy_7d_bps: int | None,
    holder_count: int,
) -> dict:
    return {
        "agent_id": a.agent_id,
        "name": a.name,
        "ticker": a.ticker,
        "founder_address": a.founder_address,
        "vault_address": a.vault_address,
        "token_address": a.token_address,
        "phase": schemas.AgentPhase(a.phase),
        "incubation_start": a.incubation_start,
        "public_launch_at": a.public_launch_at,
        "nav_usdc": current_nav.nav_usdc if current_nav else "0",
        "nav_per_share_usdc": current_nav.nav_per_share_usdc if current_nav else "0",
        "total_shares": current_nav.total_shares if current_nav else "0",
        "holder_count": holder_count,
        "apy_30d_bps": apy_30d_bps,
        "apy_7d_bps": apy_7d_bps,
        "reputation": a.reputation,
        "strategy": a.mandate["description"],
        "asset_classes": [schemas.AssetClass(c) for c in a.mandate["asset_classes"]],
        "allowed_lockups": [schemas.LockupTier(lk) for lk in a.mandate["allowed_lockups"]],
        "thumbnail_url": a.thumbnail_url,
        "created_at": a.created_at,
    }


def to_agent_summary(
    a: db_models.Agent,
    *,
    current_nav: db_models.NavPoint | None,
    apy_30d_bps: int | None,
    apy_7d_bps: int | None,
    holder_count: int,
) -> schemas.AgentSummary:
    return schemas.AgentSummary(
        **_summary_kwargs(
            a,
            current_nav=current_nav,
            apy_30d_bps=apy_30d_bps,
            apy_7d_bps=apy_7d_bps,
            holder_count=holder_count,
        )
    )


def to_agent_detail(
    a: db_models.Agent,
    *,
    current_nav: db_models.NavPoint | None,
    apy_30d_bps: int | None,
    apy_7d_bps: int | None,
    holder_count: int,
    recent_dividends: list[db_models.DividendEpoch],
    recent_decisions: list[db_models.Decision],
    latest_narrator_note: db_models.NarratorNote | None,
    redemption_queue: dict,
) -> schemas.AgentDetail:
    return schemas.AgentDetail(
        **_summary_kwargs(
            a,
            current_nav=current_nav,
            apy_30d_bps=apy_30d_bps,
            apy_7d_bps=apy_7d_bps,
            holder_count=holder_count,
        ),
        mandate=schemas.MandateSchema.model_validate(a.mandate),
        mandate_uri=a.mandate_uri,
        mandate_hash=a.mandate_hash,
        positions=[to_position(p) for p in a.positions],
        cash_usdc="0",
        yield_pool="0",
        founder_vault=to_founder_vault_snapshot(a.founder_vault),
        recent_dividends=[to_dividend_epoch(d) for d in recent_dividends],
        recent_decisions=[to_decision(d) for d in recent_decisions],
        latest_narrator_note=(
            to_narrator_note(latest_narrator_note) if latest_narrator_note else None
        ),
        redemption_queue=schemas.RedemptionQueueSnapshot(**redemption_queue),
        wind_down=to_wind_down_state(a.wind_down) if a.wind_down else None,
    )


__all__ = [
    "to_position",
    "to_nav_point",
    "to_dividend_epoch",
    "to_decision",
    "to_narrator_note",
    "to_founder_vault_snapshot",
    "to_wind_down_state",
    "to_agent_summary",
    "to_agent_detail",
]
