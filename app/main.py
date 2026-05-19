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
from contextlib import asynccontextmanager

import anthropic
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app import converters
from app.config import settings
from app.converters import to_synthetic_price_preview
from app.db import get_db
from app.hermes import client as hermes_client
from app.mandate import parser, rules
from app.mandate.hash import compute_mandate_hash
from app.mandate.ipfs import pin_mandate
from app.repos import agents as agent_repo
from app.repos import mandates as mandates_repo
from app.repos import portfolio as portfolio_repo
from app.schemas import (
    AgentDetail, AgentPhase, AgentSummary, ApiError, ApiErrorCode, AssetClass,
    ContractAddresses, Decision, DecisionType, FeeRates, HealthResponse,
    LockupTier, MandateParseRequest, MandateParseResponse, MandateSchema,
    MandateValidateRequest, MandateValidateResponse, MintPreviewRequest,
    MintPreviewResponse, NavGranularity, NavHistoryResponse, NavPeriod, Page,
    PortfolioResponse, PythUpdateBytesResponse, RedemptionRequest,
    SyntheticAssetInfo, SystemInfo,
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

scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Lazy import so test imports don't drag the chain client.
    from app.indexer.listener import run_one_cycle

    scheduler.add_job(
        run_one_cycle,
        "interval",
        seconds=settings.indexer_poll_seconds,
        id="indexer",
    )
    scheduler.start()
    print(f"[indexer] started, poll={settings.indexer_poll_seconds}s")
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(
    title="Helm Backend",
    description="REST API for the Helm AI Agent ETF on Mantle.",
    version="0.1.0",
    contact={"name": "Helm team"},
    license_info={"name": "MIT"},
    lifespan=lifespan,
)

# Per-IP rate limiter (in-memory; single-instance MVP).
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

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
    responses={
        400: {"model": ApiError},
        404: {"model": ApiError},
        503: {"model": ApiError},
    },
    summary="Preview shares received for a USDC mint amount (see ADR D002)",
    tags=["agents"],
)
def mint_preview(
    agent_id: int,
    req: MintPreviewRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"

    # 1. Amount validation
    try:
        amount = int(req.amount_usdc)
    except (ValueError, TypeError) as e:
        raise HTTPException(
            400,
            detail=ApiError(
                error=ApiErrorCode.BadRequest,
                message="amountUsdc must be a decimal integer string",
            ).model_dump(by_alias=True),
        ) from e
    if amount <= 0:
        raise HTTPException(
            400,
            detail=ApiError(
                error=ApiErrorCode.BadRequest,
                message="amountUsdc must be > 0",
            ).model_dump(by_alias=True),
        )

    # 2. Agent + positions
    agent = agent_repo.get_agent(db, agent_id)
    if agent is None:
        raise HTTPException(
            404,
            detail=ApiError(
                error=ApiErrorCode.NotFound,
                message=f"Agent {agent_id} not found",
            ).model_dump(by_alias=True),
        )

    # 3. Minimum deposit (mandate is stored snake_case in DB)
    min_deposit = int(
        agent.mandate.get("minimum_deposit_usdc")
        or agent.mandate.get("minimumDepositUsdc")
        or "0"
    )
    if amount < min_deposit:
        raise HTTPException(
            400,
            detail=ApiError(
                error=ApiErrorCode.BadRequest,
                message=f"amountUsdc ({amount}) below minimumDepositUsdc ({min_deposit})",
            ).model_dump(by_alias=True),
        )

    # 4. Pyth feeds for this agent
    feeds = agent_repo.get_pyth_feeds_for_agent(db, agent_id)
    feed_ids = [f[1] for f in feeds]

    # 5. Hermes fetch
    try:
        update_data, parsed_prices = hermes_client.fetch_price_updates(feed_ids)
    except hermes_client.HermesTimeout as e:
        raise HTTPException(
            503,
            detail=ApiError(
                error=ApiErrorCode.ChainUnreachable,
                message="Hermes timeout",
            ).model_dump(by_alias=True),
        ) from e
    except hermes_client.HermesError as e:
        raise HTTPException(
            503,
            detail=ApiError(
                error=ApiErrorCode.ChainUnreachable,
                message=f"Hermes unavailable: {e}",
            ).model_dump(by_alias=True),
        ) from e

    # 6. NAV with fresh prices (synthetic-equity positions only)
    price_by_symbol = {p["symbol"]: int(p["price_usdc"]) for p in parsed_prices}
    total_value = 0
    for pos in agent.positions:
        if pos.symbol in price_by_symbol:
            old_price = int(pos.price_usdc or "0")
            new_price = price_by_symbol[pos.symbol]
            if old_price > 0:
                total_value += int(pos.value_usdc) * new_price // old_price
            else:
                total_value += int(pos.value_usdc)
        else:
            total_value += int(pos.value_usdc)

    # 7. NAV per share (first mint = 1.0 USDC anchor)
    latest_nav = agent_repo.get_latest_nav(db, agent_id)
    total_shares = int(latest_nav.total_shares) if latest_nav else 0
    if total_shares == 0:
        nav_per_share = 1_000_000
    else:
        nav_per_share = total_value * 10**18 // total_shares
        if nav_per_share == 0:
            nav_per_share = 1_000_000

    # 8. Fee + shares
    mint_fee = amount * settings.mint_fee_bps // 10000
    net_amount = amount - mint_fee
    shares = net_amount * 10**18 // nav_per_share

    # 9. Pyth submission fee estimate (per parsed feed, not per VAA blob)
    pyth_fee_wei = hermes_client.estimate_pyth_fee_wei(len(feed_ids))

    return MintPreviewResponse(
        amount_usdc=req.amount_usdc,
        shares=str(shares),
        nav_at_preview=str(nav_per_share),
        platform_fee_usdc=str(mint_fee),
        pyth_fee_mnt_wei=pyth_fee_wei,
        valid_until=int(time.time()) + 60,
        synthetic_prices=[to_synthetic_price_preview(p) for p in parsed_prices],
    )


@app.get(
    "/agents/{agent_id}/pyth-update-bytes",
    response_model=PythUpdateBytesResponse,
    responses={404: {"model": ApiError}, 503: {"model": ApiError}},
    summary="Pyth update bytes needed for this agent's mint/burn TX (see ADR D001)",
    tags=["agents"],
)
def pyth_update_bytes(
    agent_id: int,
    response: Response,
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"

    agent = agent_repo.get_agent(db, agent_id)
    if agent is None:
        raise HTTPException(
            404,
            detail=ApiError(
                error=ApiErrorCode.NotFound,
                message=f"Agent {agent_id} not found",
            ).model_dump(by_alias=True),
        )

    feeds = agent_repo.get_pyth_feeds_for_agent(db, agent_id)
    feed_symbols = [f[0] for f in feeds]
    feed_ids = [f[1] for f in feeds]

    try:
        update_data, _ = hermes_client.fetch_price_updates(feed_ids)
    except hermes_client.HermesTimeout as e:
        raise HTTPException(
            503,
            detail=ApiError(
                error=ApiErrorCode.ChainUnreachable,
                message="Hermes timeout",
            ).model_dump(by_alias=True),
        ) from e
    except hermes_client.HermesError as e:
        raise HTTPException(
            503,
            detail=ApiError(
                error=ApiErrorCode.ChainUnreachable,
                message=f"Hermes unavailable: {e}",
            ).model_dump(by_alias=True),
        ) from e

    return PythUpdateBytesResponse(
        update_data=update_data,
        fee_mnt_wei=hermes_client.estimate_pyth_fee_wei(len(feed_ids)),
        feeds=feed_symbols,
        fetched_at=int(time.time()),
    )


# ─── Mandate parser ──────────────────────────────────────────────────────────

_LOCKED_HINT_KEYS = {"carryBps", "maxLeverage", "carry_bps", "max_leverage"}


@app.post(
    "/mandate/parse",
    response_model=MandateParseResponse,
    responses={
        400: {"model": ApiError},
        429: {"model": ApiError},
        503: {"model": ApiError},
    },
    summary="LLM mandate parse (NL → constrained JSON)",
    tags=["mandate"],
)
@limiter.limit("30/5 minutes")
def parse_mandate(
    request: Request,
    req: MandateParseRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"

    # 1. LLM call
    try:
        parsed = parser.parse_mandate(req.natural_language_mandate, req.hints)
    except anthropic.APITimeoutError as e:
        raise HTTPException(
            503,
            detail=ApiError(
                error=ApiErrorCode.ChainUnreachable,
                message="LLM timeout (>30s)",
            ).model_dump(by_alias=True),
        ) from e
    except (anthropic.APIConnectionError, anthropic.APIError) as e:
        raise HTTPException(
            503,
            detail=ApiError(
                error=ApiErrorCode.ChainUnreachable,
                message=f"LLM unavailable: {type(e).__name__}",
            ).model_dump(by_alias=True),
        ) from e
    except (ValueError, ValidationError) as e:
        raise HTTPException(
            400,
            detail=ApiError(
                error=ApiErrorCode.MandateParseFailed,
                message=f"Could not extract mandate: {e}",
            ).model_dump(by_alias=True),
        ) from e

    # 2. Hints override (non-locked fields only). Hints come in as camelCase
    # JSON; MandateSchema accepts both camelCase aliases and snake_case attrs.
    if req.hints:
        non_locked_hints = {
            k: v for k, v in req.hints.items() if k not in _LOCKED_HINT_KEYS
        }
        if non_locked_hints:
            try:
                merged = parsed.model_dump(by_alias=True)
                merged.update(non_locked_hints)
                parsed = MandateSchema.model_validate(merged)
            except ValidationError:
                pass  # ignore malformed hints — LLM output stays authoritative

    # 3. Validation + protocol-locked normalization
    try:
        normalized, warnings = rules.validate_and_normalize(parsed)
    except rules.MandateValidationError as e:
        raise HTTPException(
            400,
            detail=ApiError(
                error=ApiErrorCode.MandateParseFailed,
                message=f"Mandate validation failed: {'; '.join(e.errors)}",
                details={"errors": e.errors},
            ).model_dump(by_alias=True),
        ) from e

    # 4. Hash + IPFS
    mandate_dict = normalized.model_dump(by_alias=True)
    mandate_hash = compute_mandate_hash(mandate_dict)
    ipfs_uri, pinned = pin_mandate(mandate_dict, mandate_hash)

    # 5. DB upsert
    mandates_repo.upsert_mandate_blob(
        db,
        mandate_hash=mandate_hash,
        mandate_dict=mandate_dict,
        raw_text=req.natural_language_mandate,
        ipfs_uri=ipfs_uri,
        pinned=pinned,
    )

    return MandateParseResponse(
        mandate=normalized,
        mandate_hash=mandate_hash,
        mandate_uri=ipfs_uri,
        warnings=warnings,
    )


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
            agent_nft=addr_or_zero(s.agent_nft),
            time_provider=addr_or_zero(s.time_provider),
            agent_token_impl=addr_or_zero(s.agent_token_impl),
            agent_vault_impl=addr_or_zero(s.agent_vault_impl),
            founder_vault_impl=addr_or_zero(s.founder_vault_impl),
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
        synthetic_assets=[
            SyntheticAssetInfo(
                address=addr_or_zero(s.snvda),
                symbol="sNVDA",
                underlying="NVDA",
                pyth_feed_id=s.pyth_feed_nvda,
            ),
            SyntheticAssetInfo(
                address=addr_or_zero(s.sspy),
                symbol="sSPY",
                underlying="SPY",
                pyth_feed_id=s.pyth_feed_spy,
            ),
            SyntheticAssetInfo(
                address=addr_or_zero(s.saapl),
                symbol="sAAPL",
                underlying="AAPL",
                pyth_feed_id=s.pyth_feed_aapl,
            ),
            SyntheticAssetInfo(
                address=addr_or_zero(s.stsla),
                symbol="sTSLA",
                underlying="TSLA",
                pyth_feed_id=s.pyth_feed_tsla,
            ),
            SyntheticAssetInfo(
                address=addr_or_zero(s.smsft),
                symbol="sMSFT",
                underlying="MSFT",
                pyth_feed_id=s.pyth_feed_msft,
            ),
        ],
    )


@app.get(
    "/system/health",
    response_model=HealthResponse,
    summary="Service health (indexer, cron, LLM)",
    tags=["system"],
)
def system_health():
    _todo()
