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
    # Legacy DB rows may have stored tx_hash without the 0x prefix (some
    # eth-account hex() paths). Decision.tx_hash schema requires the prefix,
    # so normalize here on read.
    tx_hash = d.tx_hash
    if tx_hash and not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash

    return schemas.Decision(
        id=d.id,
        type=schemas.DecisionType(d.type),
        timestamp=d.timestamp,
        tx_hash=tx_hash,
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


def _mandate_get(mandate: dict, *keys, default=None):
    """First non-empty value across alternate keys (snake_case vs camelCase)."""
    for k in keys:
        v = mandate.get(k)
        if v:
            return v
    return default


def _safe_enum_list(values, enum_cls):
    out = []
    for v in values or []:
        try:
            out.append(enum_cls(v))
        except ValueError:
            continue
    return out


def _summary_kwargs(
    a: db_models.Agent,
    *,
    current_nav: db_models.NavPoint | None,
    apy_30d_bps: int | None,
    apy_7d_bps: int | None,
    holder_count: int,
) -> dict:
    from app.repos.analytics import compute_market_price

    mandate = a.mandate or {}
    nav_per_share = current_nav.nav_per_share_usdc if current_nav else None
    market_price, premium_bps = compute_market_price(nav_per_share, a.reputation)
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
        "strategy": _mandate_get(mandate, "description", default=""),
        "asset_classes": _safe_enum_list(
            _mandate_get(mandate, "asset_classes", "assetClasses", default=[]),
            schemas.AssetClass,
        ),
        "allowed_lockups": _safe_enum_list(
            _mandate_get(mandate, "allowed_lockups", "allowedLockups", default=[]),
            schemas.LockupTier,
        ),
        "thumbnail_url": a.thumbnail_url,
        "created_at": a.created_at,
        "market_price_per_share_usdc": str(market_price) if market_price is not None else None,
        "reputation_premium_bps": premium_bps,
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
    cash_usdc: str = "0",
    yield_pool: str = "0",
) -> schemas.AgentDetail:
    mandate_schema: schemas.MandateSchema | None = None
    if a.mandate:
        try:
            mandate_schema = schemas.MandateSchema.model_validate(a.mandate)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "[converters] agent %s mandate validation failed: %s",
                a.agent_id, e,
            )

    return schemas.AgentDetail(
        **_summary_kwargs(
            a,
            current_nav=current_nav,
            apy_30d_bps=apy_30d_bps,
            apy_7d_bps=apy_7d_bps,
            holder_count=holder_count,
        ),
        mandate=mandate_schema,
        mandate_uri=a.mandate_uri,
        mandate_hash=a.mandate_hash,
        positions=[to_position(p) for p in a.positions],
        cash_usdc=cash_usdc,
        yield_pool=yield_pool,
        founder_vault=to_founder_vault_snapshot(a.founder_vault) if a.founder_vault else None,
        recent_dividends=[to_dividend_epoch(d) for d in recent_dividends],
        recent_decisions=[to_decision(d) for d in recent_decisions],
        latest_narrator_note=(
            to_narrator_note(latest_narrator_note) if latest_narrator_note else None
        ),
        redemption_queue=schemas.RedemptionQueueSnapshot(**redemption_queue),
        wind_down=to_wind_down_state(a.wind_down) if a.wind_down else None,
    )


def to_portfolio_position(
    h: db_models.Holder,
    *,
    value_usdc: str,
    total_user_value_usdc: str,
) -> schemas.PortfolioPosition:
    total = int(total_user_value_usdc)
    weight_bps = (int(value_usdc) * 10000) // total if total > 0 else 0
    return schemas.PortfolioPosition(
        agent_id=h.agent_id,
        agent_name=h.agent.name,
        ticker=h.agent.ticker,
        vault_address=h.agent.vault_address,
        token_address=h.agent.token_address,
        shares=h.balance,
        weight_bps=weight_bps,
        value_usdc=value_usdc,
        cost_basis_usdc=None,
        unrealized_pnl_usdc=None,
    )


def to_dividend_claim_aggregate(
    agent: db_models.Agent,
    claims: list[db_models.DividendClaim],
    epochs_by_num: dict[int, db_models.DividendEpoch],
) -> schemas.DividendClaim:
    return schemas.DividendClaim(
        agent_id=agent.agent_id,
        agent_name=agent.name,
        ticker=agent.ticker,
        claimable_epochs=sorted(c.epoch for c in claims),
        claimable_amount_usdc=str(sum(int(c.amount_usdc) for c in claims)),
        oldest_epoch_at=min(epochs_by_num[c.epoch].distributed_at for c in claims),
    )


def to_redemption_request(
    r: db_models.RedemptionRequest,
    *,
    now: int,
) -> schemas.RedemptionRequest:
    """Pending + unlock_at <= now → Claimable in the response shape."""
    status = r.status
    if status == "Pending" and r.unlock_at <= now:
        status = "Claimable"
    return schemas.RedemptionRequest(
        request_id=r.request_id,
        agent_id=r.agent_id,
        agent_name=r.agent.name,
        shares=r.shares,
        tier=schemas.LockupTier(r.tier),
        unlock_at=r.unlock_at,
        estimated_usdc=r.estimated_usdc,
        status=schemas.RedemptionStatus(status),
    )


def to_synthetic_price_preview(p: dict) -> schemas.SyntheticPricePreview:
    """p is a `HermesPrice` TypedDict from app.hermes.client."""
    return schemas.SyntheticPricePreview(
        symbol=p["symbol"],
        price_usdc=p["price_usdc"],
        pyth_confidence=p["confidence"],
        fresh_at=p["publish_time"],
    )


def to_founder_vault_position(
    fv: db_models.FounderVault,
) -> schemas.FounderVaultPosition:
    return schemas.FounderVaultPosition(
        agent_id=fv.agent_id,
        agent_name=fv.agent.name,
        vault_address=fv.agent.vault_address,
        founder_vault_address=fv.address,
        shares_locked=fv.shares_held,
        lockup_ends_at=fv.lockup_ends_at,
        carry_balance_usdc=fv.carry_balance_usdc,
        cumulative_withdrawn_bps=fv.cumulative_withdrawn_bps,
        is_subordination_active=fv.is_subordination_active,
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
    "to_portfolio_position",
    "to_dividend_claim_aggregate",
    "to_redemption_request",
    "to_synthetic_price_preview",
    "to_founder_vault_position",
]
