"""
Helm BE — FastAPI app entrypoint.

This skeleton exposes the full API contract. All routes return 501 stubs for
now — the goal is to lock the OpenAPI shape so the FE can generate typed
client code against `/openapi.json`.

Run:
    uvicorn app.main:app --reload --port 8000

Inspect:
    http://localhost:8000/openapi.json    # raw OpenAPI 3.1 document
    http://localhost:8000/docs            # interactive Swagger UI
    http://localhost:8000/redoc           # alternate doc viewer

FE workflow (see docs/frontend/openapi-typegen.md):
    pnpm gen-types  # → src/lib/api-types.gen.ts
"""

import time

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app import converters
from app.config import settings
from app.db import get_db
from app.repos import agents as agent_repo
from app.repos import portfolio as portfolio_repo
from app.schemas import (
    AgentDetail, AgentPhase, AgentSummary, ApiError, ApiErrorCode, AssetClass,
    ContractAddresses, Decision, DecisionType, FeeRates, HealthResponse,
    LockupTier, MandateParseRequest, MandateParseResponse,
    MandateValidateRequest, MandateValidateResponse, MintPreviewRequest,
    MintPreviewResponse, NavGranularity, NavHistoryResponse, NavPeriod, Page,
    PortfolioResponse, PythUpdateBytesResponse, RedemptionRequest, SystemInfo,
)
from app.utils.addresses import addr_or_zero
from app.utils.cache import cache_for


_AUTO_GRANULARITY: dict[NavPeriod, NavGranularity] = {
    NavPeriod.H24: NavGranularity.Hour,
    NavPeriod.D7: NavGranularity.Hour,
    NavPeriod.D30: NavGranularity.Day,
    NavPeriod.All: NavGranularity.Day,
}


def _api_error(status: int, code: ApiErrorCode, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ApiError(error=code, message=message).model_dump(by_alias=True),
    )

app = FastAPI(
    title="Helm Backend",
    description="REST API for the Helm AI Agent ETF on Mantle.",
    version="0.1.0",
    contact={"name": "Helm team"},
    license_info={"name": "MIT"},
)

# CORS — open during hackathon, tighten before public deploy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def _todo() -> None:
    """All route handlers raise this until BE implementation lands."""
    raise HTTPException(
        status_code=501,
        detail={
            "error": ApiErrorCode.InternalError.value,
            "message": "Not implemented yet — schema-only stub.",
        },
    )


# ─── Agents ──────────────────────────────────────────────────────────────────

@app.get(
    "/agents",
    response_model=Page[AgentSummary],
    dependencies=[Depends(cache_for(15))],
    summary="List agents (marketplace)",
    tags=["agents"],
)
def list_agents(
    phase: list[AgentPhase] | None = Query(None),
    asset_class: list[AssetClass] | None = Query(None, alias="assetClass"),
    lockup: list[LockupTier] | None = Query(None),
    sort: str = Query("apy_30d", description="apy_30d|apy_7d|nav|holders|newest|reputation"),
    order: str = Query("desc", description="asc|desc"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    rows, total = agent_repo.list_agents(
        db,
        phase=phase,
        asset_class=asset_class,
        lockup=lockup,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )
    items = [
        converters.to_agent_summary(
            a,
            current_nav=agent_repo.get_latest_nav(db, a.agent_id),
            apy_30d_bps=agent_repo.compute_apy_bps(db, a.agent_id, 30),
            apy_7d_bps=agent_repo.compute_apy_bps(db, a.agent_id, 7),
            holder_count=agent_repo.compute_holder_count(db, a.agent_id),
        )
        for a in rows
    ]
    return Page[AgentSummary](items=items, total=total, limit=limit, offset=offset)


@app.get(
    "/agents/{agent_id}",
    response_model=AgentDetail,
    responses={404: {"model": ApiError}},
    dependencies=[Depends(cache_for(10))],
    summary="Agent detail",
    tags=["agents"],
)
def get_agent(agent_id: int, db: Session = Depends(get_db)):
    a = agent_repo.get_agent(db, agent_id)
    if a is None:
        raise _api_error(404, ApiErrorCode.NotFound, f"Agent {agent_id} not found")
    return converters.to_agent_detail(
        a,
        current_nav=agent_repo.get_latest_nav(db, agent_id),
        apy_30d_bps=agent_repo.compute_apy_bps(db, agent_id, 30),
        apy_7d_bps=agent_repo.compute_apy_bps(db, agent_id, 7),
        holder_count=agent_repo.compute_holder_count(db, agent_id),
        recent_dividends=agent_repo.get_recent_dividends(db, agent_id),
        recent_decisions=agent_repo.get_recent_decisions(db, agent_id),
        latest_narrator_note=agent_repo.get_latest_narrator_note(db, agent_id),
        redemption_queue=agent_repo.get_redemption_queue_snapshot(db, agent_id),
    )


@app.get(
    "/agents/{agent_id}/nav-history",
    response_model=NavHistoryResponse,
    responses={400: {"model": ApiError}, 404: {"model": ApiError}},
    dependencies=[Depends(cache_for(30))],
    summary="NAV time series",
    tags=["agents"],
)
def get_nav_history(
    agent_id: int,
    period: NavPeriod = Query(NavPeriod.D7),
    granularity: NavGranularity | None = Query(None),
    db: Session = Depends(get_db),
):
    if granularity == NavGranularity.Minute and period != NavPeriod.H24:
        raise _api_error(
            400,
            ApiErrorCode.BadRequest,
            "minute granularity only valid for period=24h",
        )
    if agent_repo.get_agent(db, agent_id) is None:
        raise _api_error(404, ApiErrorCode.NotFound, f"Agent {agent_id} not found")

    actual = granularity or _AUTO_GRANULARITY[period]
    points = [
        converters.to_nav_point(p)
        for p in agent_repo.get_nav_history(db, agent_id, period)
    ]
    return NavHistoryResponse(points=points, period=period, granularity=actual)


@app.get(
    "/agents/{agent_id}/decisions",
    response_model=Page[Decision],
    summary="Decision log (rebalance/harvest/distribute)",
    tags=["agents"],
)
def list_agent_decisions(
    agent_id: int,
    type: DecisionType | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    _todo()


@app.post(
    "/agents/{agent_id}/mint-preview",
    response_model=MintPreviewResponse,
    responses={400: {"model": ApiError}, 404: {"model": ApiError}},
    summary="Preview shares received for a USDC mint amount (see ADR D002)",
    tags=["agents"],
)
def mint_preview(agent_id: int, req: MintPreviewRequest):
    _todo()


@app.get(
    "/agents/{agent_id}/pyth-update-bytes",
    response_model=PythUpdateBytesResponse,
    responses={404: {"model": ApiError}, 503: {"model": ApiError}},
    summary="Pyth update bytes needed for this agent's mint/burn TX (see ADR D001)",
    tags=["agents"],
)
def pyth_update_bytes(agent_id: int):
    _todo()


# ─── Mandate parser ──────────────────────────────────────────────────────────

@app.post(
    "/mandate/parse",
    response_model=MandateParseResponse,
    responses={400: {"model": ApiError}, 429: {"model": ApiError}},
    summary="LLM mandate parse (NL → constrained JSON)",
    tags=["mandate"],
)
def parse_mandate(req: MandateParseRequest):
    _todo()


@app.post(
    "/mandate/validate",
    response_model=MandateValidateResponse,
    summary="Validate hand-edited mandate (no LLM)",
    tags=["mandate"],
)
def validate_mandate(req: MandateValidateRequest):
    _todo()


# ─── Portfolio ───────────────────────────────────────────────────────────────

@app.get(
    "/portfolio/{address}",
    response_model=PortfolioResponse,
    dependencies=[Depends(cache_for(10))],
    summary="Per-wallet portfolio aggregate",
    tags=["portfolio"],
)
def get_portfolio(address: str, db: Session = Depends(get_db)):
    addr = address.lower()
    now = int(time.time())

    holdings = portfolio_repo.get_holdings_by_address(db, addr)
    redemption_rows = portfolio_repo.get_redemption_requests_by_address(db, addr)
    founder_vaults = portfolio_repo.get_founder_vaults_by_address(db, addr)
    dividend_groups = portfolio_repo.get_pending_dividends_grouped(db, addr)

    position_values = [
        portfolio_repo.get_position_value_usdc(db, h.agent_id, h.balance)
        for h in holdings
    ]
    total_value = sum(int(v) for v in position_values)

    positions = [
        converters.to_portfolio_position(
            h, value_usdc=v, total_user_value_usdc=str(total_value)
        )
        for h, v in zip(holdings, position_values)
    ]
    pending_dividends = [
        converters.to_dividend_claim_aggregate(agent, claims, epochs)
        for agent, claims, epochs in dividend_groups
    ]
    pending_redemptions = [
        converters.to_redemption_request(r, now=now) for r in redemption_rows
    ]
    founder_vault_positions = [
        converters.to_founder_vault_position(fv) for fv in founder_vaults
    ]

    return PortfolioResponse(
        total_value_usdc=str(total_value),
        positions=positions,
        pending_dividends=pending_dividends,
        pending_redemptions=pending_redemptions,
        founder_vaults=founder_vault_positions,
    )


@app.get(
    "/redemptions/{address}",
    response_model=list[RedemptionRequest],
    dependencies=[Depends(cache_for(15))],
    summary="Pending redemption requests for a wallet",
    tags=["portfolio"],
)
def get_redemptions(address: str, db: Session = Depends(get_db)):
    addr = address.lower()
    now = int(time.time())
    rows = portfolio_repo.get_redemption_requests_by_address(db, addr)
    return [converters.to_redemption_request(r, now=now) for r in rows]


# ─── System ──────────────────────────────────────────────────────────────────

@app.get(
    "/system/info",
    response_model=SystemInfo,
    dependencies=[Depends(cache_for(300))],
    summary="Chain config + contract addresses + Pyth feeds",
    tags=["system"],
)
def system_info():
    s = settings
    return SystemInfo(
        chain_id=s.chain_id,
        rpc_url=s.mantle_sepolia_rpc if s.chain_id == 5003 else s.mantle_rpc,
        block_explorer_url=(
            "https://sepolia.mantlescan.xyz" if s.chain_id == 5003
            else "https://mantlescan.xyz"
        ),
        contracts=ContractAddresses(
            helm_registry=addr_or_zero(s.helm_registry),
            platform_treasury=addr_or_zero(s.platform_treasury),
            redemption_queue=addr_or_zero(s.redemption_queue),
            yield_harvester=addr_or_zero(s.yield_harvester),
            dividend_distributor=addr_or_zero(s.dividend_distributor),
            pyth_price_adapter=addr_or_zero(s.pyth_price_adapter),
            mantle_meth_adapter=addr_or_zero(s.mantle_meth_adapter),
            ondo_usdy_adapter=addr_or_zero(s.ondo_usdy_adapter),
            pyth=addr_or_zero(s.pyth_contract),
            usdc=addr_or_zero(s.usdc),
        ),
        fee_rates=FeeRates(
            mint_bps=s.mint_fee_bps,
            redeem_bps=s.redeem_fee_bps,
            rebalance_bps=s.rebalance_fee_bps,
        ),
        pyth_feed_ids={
            "sNVDA": s.pyth_feed_nvda,
            "sSPY": s.pyth_feed_spy,
            "sAAPL": s.pyth_feed_aapl,
            "sTSLA": s.pyth_feed_tsla,
            "sMSFT": s.pyth_feed_msft,
            "ETH/USD": s.pyth_feed_eth_usd,
            "USDC/USD": s.pyth_feed_usdc_usd,
        },
        synthetic_assets=[],
    )


@app.get(
    "/system/health",
    response_model=HealthResponse,
    summary="Service health (indexer, cron, LLM)",
    tags=["system"],
)
def system_health():
    _todo()
