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

import openai
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app import converters
from app.config import settings
from app.converters import to_synthetic_price_preview
from app.db import SessionLocal, get_db
from app.hermes import client as hermes_client
from app.mandate import parser, rules
from app.mandate.hash import compute_mandate_hash
from app.mandate.ipfs import pin_mandate
from app.repos import agents as agent_repo
from app.repos import analytics as analytics_repo
from app.repos import benchmark as benchmark_repo
from app.repos import mandates as mandates_repo
from app.repos import portfolio as portfolio_repo
from app.schemas import (
    AdminDistributeResponse, AdminHarvestResponse,
    AdminNftMetadataResponse, AdminRebalanceResponse,
    AgentDetail, AgentPerformance, AgentPhase, AgentSummary, ApiError, ApiErrorCode, AssetClass,
    BenchmarkPoint, BenchmarkResponse, BenchmarkSummary,
    ConditionCheckResponse, ConditionResult,
    QualificationCriterion, QualificationResponse,
    ContractAddresses, Decision, DecisionType, FeeRates, HealthResponse,
    LockupTier, MandateParseRequest, MandateParseResponse, MandateSchema,
    MandateValidateRequest, MandateValidateResponse, MintPreviewRequest,
    MintPreviewResponse, MintUsdcRequest, MintUsdcResponse,
    NavGranularity, NavHistoryResponse, NavPeriod, Page,
    PortfolioResponse, PythUpdateBytesResponse, RedemptionRequest,
    SyntheticAssetInfo, SystemInfo,
    TimeAdvanceRequest, TimeAdvanceResponse,
)
from app.chain.client import time_provider, usdc
from app.chain.executor_wallet import send_tx
from app.services import condition_evaluator
from app.services import distribute, harvest, nft_metadata, qualification, rebalance
from web3 import Web3
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
    from app.repos.agents import sweep_stale_agents

    # Sweep stale agents (vault missing on-chain or pointing at a different
    # registry) before the indexer wakes up. Protects /agents responses from
    # leaking vaults that were created against a prior registry deploy.
    try:
        from app.db import SessionLocal
        with SessionLocal() as db:
            stats = sweep_stale_agents(db)
        if stats["removed"]:
            print(f"[startup] swept {len(stats['removed'])} stale agent(s): "
                  f"{stats['removed']}")
        else:
            print(f"[startup] sweep clean: {stats['kept']} agent(s) valid")
    except Exception as e:  # noqa: BLE001
        print(f"[startup] sweep skipped ({type(e).__name__}: {e})")

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

# CORS — origins driven by env (CORS_ORIGINS=comma-separated). Falls back to
# ["*"] when the env var is empty so local dev keeps working untouched.
_cors_origins = settings.cors_origins_list or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# Static smoke-test page (vanilla HTML/JS + CDN libs)
app.mount("/static", StaticFiles(directory="static"), name="static")


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
    sort: str = Query(
        "apy_30d",
        description="apy_30d|apy_7d|nav|holders|newest|created_at|reputation|sharpe|total_return|max_drawdown",
    ),
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
    items = []
    for a in rows:
        summary = converters.to_agent_summary(
            a,
            current_nav=agent_repo.get_latest_nav(db, a.agent_id),
            apy_30d_bps=agent_repo.compute_apy_bps(db, a.agent_id, 30),
            apy_7d_bps=agent_repo.compute_apy_bps(db, a.agent_id, 7),
            holder_count=agent_repo.compute_holder_count(db, a.agent_id),
        )
        summary.performance = AgentPerformance(
            **analytics_repo.compute_performance(db, a.agent_id)
        )
        items.append(summary)
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

    cash_usdc = "0"
    yield_pool = "0"
    import logging
    log = logging.getLogger(__name__)
    log.warning("[get_agent] CASHFIX_V2 agent=%s vault=%s", agent_id, a.vault_address)
    try:
        from app.chain.client import agent_vault
        vault = agent_vault(a.vault_address)
        cash_usdc = str(vault.functions.cashUSDC().call())
        yield_pool = str(vault.functions.yieldPool().call())
        log.warning("[get_agent] CASHFIX_V2 ok cash=%s yield=%s", cash_usdc, yield_pool)
    except Exception as e:
        log.warning("[get_agent] CASHFIX_V2 chain read failed: %s", e)

    detail = converters.to_agent_detail(
        a,
        current_nav=agent_repo.get_latest_nav(db, agent_id),
        apy_30d_bps=agent_repo.compute_apy_bps(db, agent_id, 30),
        apy_7d_bps=agent_repo.compute_apy_bps(db, agent_id, 7),
        holder_count=agent_repo.compute_holder_count(db, agent_id),
        recent_dividends=agent_repo.get_recent_dividends(db, agent_id),
        recent_decisions=agent_repo.get_recent_decisions(db, agent_id),
        latest_narrator_note=agent_repo.get_latest_narrator_note(db, agent_id),
        redemption_queue=agent_repo.get_redemption_queue_snapshot(db, agent_id),
        cash_usdc=cash_usdc,
        yield_pool=yield_pool,
    )
    detail.performance = AgentPerformance(**analytics_repo.compute_performance(db, agent_id))
    return detail


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
    "/agents/{agent_id}/benchmark",
    response_model=BenchmarkResponse,
    responses={404: {"model": ApiError}},
    dependencies=[Depends(cache_for(60))],
    summary="Agent NAV vs sSPY and 60/40 baselines (synthetic constant growth)",
    tags=["agents"],
)
def get_benchmark(agent_id: int, db: Session = Depends(get_db)):
    if agent_repo.get_agent(db, agent_id) is None:
        raise _api_error(404, ApiErrorCode.NotFound, f"Agent {agent_id} not found")
    result = benchmark_repo.compute_benchmark_series(db, agent_id)
    return BenchmarkResponse(
        agent_id=agent_id,
        period_start=result["period_start"],
        period_end=result["period_end"],
        sample_count=result["sample_count"],
        series=[BenchmarkPoint(**p) for p in result["series"]],
        summary=BenchmarkSummary(**result["summary"]) if result["summary"] else None,
    )


@app.get(
    "/agents/{agent_id}/conditions",
    response_model=ConditionCheckResponse,
    responses={404: {"model": ApiError}},
    dependencies=[Depends(cache_for(60))],
    summary="Evaluate mandate emergencyExitConditions against live CoinGecko market data",
    tags=["agents"],
)
def get_conditions(agent_id: int, db: Session = Depends(get_db)):
    if agent_repo.get_agent(db, agent_id) is None:
        raise _api_error(404, ApiErrorCode.NotFound, f"Agent {agent_id} not found")
    results = condition_evaluator.evaluate_conditions(db, agent_id)
    return ConditionCheckResponse(
        agent_id=agent_id,
        any_triggered=any(r["triggered"] for r in results),
        conditions=[ConditionResult(**r) for r in results],
    )


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
    except openai.APITimeoutError as e:
        raise HTTPException(
            503,
            detail=ApiError(
                error=ApiErrorCode.ChainUnreachable,
                message="LLM timeout (>30s)",
            ).model_dump(by_alias=True),
        ) from e
    except (openai.APIConnectionError, openai.APIError) as e:
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


def _check_testnet() -> None:
    if settings.chain_id not in (5003, 31337):
        raise HTTPException(503, detail=ApiError(
            error=ApiErrorCode.BadRequest,
            message="Admin endpoints disabled on mainnet",
        ).model_dump(by_alias=True))


@app.post(
    "/admin/time/advance",
    response_model=TimeAdvanceResponse,
    responses={400: {"model": ApiError}, 503: {"model": ApiError}},
    summary="Advance TimeProvider clock (testnet only)",
    tags=["admin"],
)
def admin_time_advance(req: TimeAdvanceRequest, response: Response) -> TimeAdvanceResponse:
    response.headers["Cache-Control"] = "no-store"
    _check_testnet()
    try:
        tx_hash = send_tx(time_provider().functions.advance(req.seconds))["tx_hash"]
        new_time = time_provider().functions.currentTime().call()
        return TimeAdvanceResponse(
            tx_hash=tx_hash,
            advanced_seconds=req.seconds,
            new_current_time=new_time,
        )
    except Exception as e:
        raise HTTPException(503, detail=ApiError(
            error=ApiErrorCode.ChainUnreachable,
            message=f"time advance failed: {e}",
        ).model_dump(by_alias=True)) from e


@app.post(
    "/admin/mint-usdc",
    response_model=MintUsdcResponse,
    responses={400: {"model": ApiError}, 503: {"model": ApiError}},
    summary="Mint MockUSDC to address (testnet only)",
    tags=["admin"],
)
def admin_mint_usdc(req: MintUsdcRequest, response: Response) -> MintUsdcResponse:
    response.headers["Cache-Control"] = "no-store"
    _check_testnet()
    try:
        amount = int(req.amount_usdc)
        if amount <= 0:
            raise HTTPException(400, detail=ApiError(
                error=ApiErrorCode.BadRequest,
                message="amountUsdc must be > 0",
            ).model_dump(by_alias=True))
        tx_hash = send_tx(usdc().functions.mint(
            Web3.to_checksum_address(req.to), amount
        ))["tx_hash"]
        return MintUsdcResponse(
            tx_hash=tx_hash,
            to=req.to.lower(),
            amount_usdc=req.amount_usdc,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, detail=ApiError(
            error=ApiErrorCode.ChainUnreachable,
            message=f"mint failed: {e}",
        ).model_dump(by_alias=True)) from e


@app.post(
    "/admin/agents/{agent_id}/rebalance",
    response_model=AdminRebalanceResponse,
    responses={503: {"model": ApiError}},
    summary="Trigger rebalance for agent (testnet only)",
    tags=["admin"],
)
def admin_rebalance(agent_id: int, response: Response) -> AdminRebalanceResponse:
    response.headers["Cache-Control"] = "no-store"
    _check_testnet()
    try:
        result = rebalance.execute(agent_id)
        return AdminRebalanceResponse(
            agent_id=agent_id,
            tx_hash=result["tx_hash"],
            target_weights=result.get("targets") or result.get("target_weights") or [],
        )
    except Exception as e:
        raise HTTPException(503, detail=ApiError(
            error=ApiErrorCode.ChainUnreachable,
            message=f"rebalance failed: {e}",
        ).model_dump(by_alias=True)) from e


@app.post(
    "/admin/agents/{agent_id}/harvest",
    response_model=AdminHarvestResponse,
    responses={503: {"model": ApiError}},
    summary="Trigger harvest for agent (testnet only)",
    tags=["admin"],
)
def admin_harvest(agent_id: int, response: Response) -> AdminHarvestResponse:
    response.headers["Cache-Control"] = "no-store"
    _check_testnet()
    try:
        result = harvest.run(agent_id)
        return AdminHarvestResponse(agent_id=agent_id, tx_hash=result["tx_hash"])
    except Exception as e:
        raise HTTPException(503, detail=ApiError(
            error=ApiErrorCode.ChainUnreachable,
            message=f"harvest failed: {e}",
        ).model_dump(by_alias=True)) from e


@app.post(
    "/admin/agents/{agent_id}/distribute",
    response_model=AdminDistributeResponse,
    responses={503: {"model": ApiError}},
    summary="Trigger dividend distribute for agent (testnet only)",
    tags=["admin"],
)
def admin_distribute(agent_id: int, response: Response) -> AdminDistributeResponse:
    response.headers["Cache-Control"] = "no-store"
    _check_testnet()
    try:
        result = distribute.run(agent_id)
        return AdminDistributeResponse(
            agent_id=agent_id,
            amount=str(result.get("amount", 0)),
            stage_tx_hash=result.get("stage_tx_hash"),
            distribute_tx_hash=result.get("distribute_tx_hash"),
            note=result.get("note"),
        )
    except Exception as e:
        raise HTTPException(503, detail=ApiError(
            error=ApiErrorCode.ChainUnreachable,
            message=f"distribute failed: {e}",
        ).model_dump(by_alias=True)) from e


@app.post(
    "/admin/agents/{agent_id}/nft-metadata",
    response_model=AdminNftMetadataResponse,
    responses={503: {"model": ApiError}},
    summary="Regenerate NFT metadata + pin IPFS + setTokenURI (testnet only)",
    tags=["admin"],
)
def admin_nft_metadata(agent_id: int, response: Response) -> AdminNftMetadataResponse:
    response.headers["Cache-Control"] = "no-store"
    _check_testnet()
    try:
        result = nft_metadata.update(agent_id)
        return AdminNftMetadataResponse(
            agent_id=agent_id,
            tx_hash=result["tx_hash"],
            uri=result["uri"],
            attribute_count=result.get("attribute_count"),
        )
    except Exception as e:
        raise HTTPException(503, detail=ApiError(
            error=ApiErrorCode.ChainUnreachable,
            message=f"nft metadata update failed: {e}",
        ).model_dump(by_alias=True)) from e


# ─── Debug / chain-inspector endpoints (smoke-test page) ────────────────────

_PHASE_NAMES = ["Incubation", "PublicLaunch", "WindDown", "Slashed", "Settled"]


@app.get(
    "/admin/debug/indexer-state",
    summary="Indexer sync state vs current chain head",
    tags=["admin"],
)
def debug_indexer_state(response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    _check_testnet()
    from app.chain.client import get_w3
    from app.db.models import IndexerState
    with SessionLocal() as db:
        state = db.query(IndexerState).first()
    try:
        head = get_w3().eth.block_number
    except Exception as e:
        raise HTTPException(503, detail=ApiError(
            error=ApiErrorCode.ChainUnreachable,
            message=f"chain head fetch failed: {e}",
        ).model_dump(by_alias=True)) from e
    last = state.last_synced_block if state else 0
    return {
        "chainHead": head,
        "lastSyncedBlock": last,
        "gap": head - last,
        "updatedAt": state.updated_at if state else None,
    }


@app.get(
    "/admin/debug/agents/{agent_id}/compare",
    summary="Compare BE DB state vs live SC chain state for a single agent",
    tags=["admin"],
)
def debug_agent_compare(agent_id: int, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    _check_testnet()
    from app.chain.client import agent_nft, agent_vault, registry
    from app.db.models import Agent

    with SessionLocal() as db:
        agent = db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, detail=ApiError(
            error=ApiErrorCode.NotFound, message=f"Agent {agent_id} not found",
        ).model_dump(by_alias=True))

    sc: dict = {}

    try:
        sc_phase_int = registry().functions.phaseOf(agent_id).call()
        sc["phase"] = (
            _PHASE_NAMES[sc_phase_int]
            if 0 <= sc_phase_int < len(_PHASE_NAMES)
            else f"Unknown({sc_phase_int})"
        )
    except Exception as e:
        sc["phase"] = f"err: {str(e)[:80]}"

    try:
        sc["totalAssets"] = str(
            agent_vault(agent.vault_address).functions.totalAssets().call()
        )
    except Exception as e:
        sc["totalAssets"] = f"err: {str(e)[:80]}"

    try:
        sc["reputation"] = agent_nft().functions.reputationOf(agent_id).call()
    except Exception as e:
        sc["reputation"] = f"err: {str(e)[:80]}"

    try:
        sc["tokenURI"] = agent_nft().functions.tokenURI(agent_id).call()
    except Exception as e:
        sc["tokenURI"] = f"err: {str(e)[:80]}"

    be = {
        "phase": agent.phase,
        "reputation": agent.reputation,
        "vaultAddress": agent.vault_address,
        "tokenAddress": agent.token_address,
        "mandateUri": agent.mandate_uri,
    }

    return {
        "agentId": agent_id,
        "be": be,
        "sc": sc,
        "consistent": {
            "phase": str(be["phase"]) == str(sc.get("phase")),
            "reputation": be["reputation"] == sc.get("reputation"),
            "tokenURI": be.get("mandateUri") == sc.get("tokenURI"),
        },
    }


_SYNTHETIC_SETTING_KEYS = ("snvda", "sspy", "saapl", "stsla", "smsft")


@app.get(
    "/admin/debug/synthetic-prices",
    summary="Live Pyth-priced synthetic asset prices (USDC, 6-dec)",
    tags=["admin"],
)
def debug_synthetic_prices(response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    _check_testnet()
    from app.chain.abi_loader import load_abi
    from app.chain.client import get_w3

    w3 = get_w3()
    abi = load_abi("SyntheticAsset")
    out: dict[str, dict] = {}
    for key in _SYNTHETIC_SETTING_KEYS:
        addr = getattr(settings, key, "")
        symbol = "s" + key[1:].upper()  # "snvda" → "sNVDA"
        if not addr:
            out[symbol] = {"address": "", "error": "not configured"}
            continue
        try:
            c = w3.eth.contract(address=w3.to_checksum_address(addr), abi=abi)
            price = c.functions.priceUSDC().call()
            out[symbol] = {"address": addr, "priceUsdc": str(price)}
        except Exception as e:
            out[symbol] = {"address": addr, "error": str(e)[:200]}
    return out


@app.get(
    "/admin/debug/adapters",
    summary="mETH / USDY adapter live state (exchange rates, simulated APY)",
    tags=["admin"],
)
def debug_adapters(response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    _check_testnet()
    from app.chain.abi_loader import load_abi
    from app.chain.client import get_w3

    w3 = get_w3()
    out: dict[str, dict] = {}

    meth_addr = settings.mantle_meth_adapter
    if meth_addr:
        try:
            c = w3.eth.contract(
                address=w3.to_checksum_address(meth_addr),
                abi=load_abi("MantleMETHAdapter"),
            )
            out["mEth"] = {
                "address": meth_addr,
                "exchangeRate": str(c.functions.exchangeRate().call()),
                "mEthEthRatio": str(c.functions.mEthEthRatio().call()),
                "simulatedApyBps": int(c.functions.SIMULATED_APY_BPS().call()),
            }
        except Exception as e:
            out["mEth"] = {"address": meth_addr, "error": str(e)[:200]}
    else:
        out["mEth"] = {"address": "", "error": "not configured"}

    usdy_addr = settings.ondo_usdy_adapter
    if usdy_addr:
        try:
            c = w3.eth.contract(
                address=w3.to_checksum_address(usdy_addr),
                abi=load_abi("OndoUSDYAdapter"),
            )
            out["usdy"] = {
                "address": usdy_addr,
                "exchangeRate": str(c.functions.exchangeRate().call()),
                "usdyPricePerShare": str(c.functions.usdyPricePerShare().call()),
                "simulatedApyBps": int(c.functions.SIMULATED_APY_BPS().call()),
            }
        except Exception as e:
            out["usdy"] = {"address": usdy_addr, "error": str(e)[:200]}
    else:
        out["usdy"] = {"address": "", "error": "not configured"}

    return out


@app.get(
    "/admin/debug/treasury",
    summary="PlatformTreasury fee rates + cumulative fees collected",
    tags=["admin"],
)
def debug_treasury(response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    _check_testnet()
    from app.chain.client import platform_treasury

    try:
        c = platform_treasury()
        mint_bps, redeem_bps, rebalance_bps = c.functions.feeRates().call()
        return {
            "address": settings.platform_treasury,
            "mintBps": int(mint_bps),
            "redeemBps": int(redeem_bps),
            "rebalanceBps": int(rebalance_bps),
            "totalFeesCollected": str(c.functions.totalFeesCollected().call()),
        }
    except Exception as e:
        return {"address": settings.platform_treasury, "error": str(e)[:200]}


@app.get(
    "/admin/debug/agents/{agent_id}/founder-vault",
    summary="Live FounderVault SC state (deposits, carry, lockup, subordination)",
    tags=["admin"],
)
def debug_founder_vault(agent_id: int, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    _check_testnet()
    from app.chain.abi_loader import load_abi
    from app.chain.client import get_w3
    from app.db.models import Agent

    with SessionLocal() as db:
        agent = db.get(Agent, agent_id)
    if not agent or not agent.founder_vault_address:
        raise HTTPException(404, detail=ApiError(
            error=ApiErrorCode.NotFound,
            message=f"FounderVault for agent {agent_id} not found",
        ).model_dump(by_alias=True))

    w3 = get_w3()
    c = w3.eth.contract(
        address=w3.to_checksum_address(agent.founder_vault_address),
        abi=load_abi("FounderVault"),
    )
    try:
        return {
            "address": agent.founder_vault_address,
            "totalDeposited": str(c.functions.totalDeposited().call()),
            "totalWithdrawn": str(c.functions.totalWithdrawn().call()),
            "totalSharesHeld": str(c.functions.totalSharesHeld().call()),
            "carryBalance": str(c.functions.carryBalance().call()),
            "lockupEndsAt": int(c.functions.lockupEndsAt().call()),
            "cumulativeWithdrawnBps": int(c.functions.cumulativeWithdrawnBps().call()),
            "isSubordinationActive": bool(c.functions.isSubordinationActive().call()),
            "subordinationThresholdBps": int(c.functions.subordinationThresholdBps().call()),
        }
    except Exception as e:
        return {"address": agent.founder_vault_address, "error": str(e)[:200]}


@app.get(
    "/admin/debug/agents/{agent_id}/redemption-queue",
    summary="Live RedemptionQueue state for an agent (pending shares + tier allow-list)",
    tags=["admin"],
)
def debug_redemption_queue(agent_id: int, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    _check_testnet()
    from app.chain.client import redemption_queue

    c = redemption_queue()
    # Tier index → human label (no on-chain TIER_DAYS function — keep in sync with FE).
    tier_labels = {0: "instant", 1: "30d", 2: "60d", 3: "90d"}
    tiers: list[dict] = []
    for t, label in tier_labels.items():
        try:
            tiers.append({
                "tier": t,
                "label": label,
                "allowed": bool(c.functions.tierAllowed(agent_id, t).call()),
            })
        except Exception as e:
            tiers.append({"tier": t, "label": label, "error": str(e)[:120]})
    try:
        pending = c.functions.pendingForAgent(agent_id).call()
    except Exception as e:
        return {"agentId": agent_id, "error": str(e)[:200], "tiers": tiers}
    return {
        "agentId": agent_id,
        "pendingShares": str(pending),
        "tiers": tiers,
    }


def _qualification_to_response(agent_id: int, result: dict) -> QualificationResponse:
    return QualificationResponse(
        agent_id=agent_id,
        overall_passed=result["overall_passed"],
        checks=[QualificationCriterion(**c) for c in result["checks"]],
        advanced=result.get("advanced", False),
        tx_hash=result.get("tx_hash"),
        new_phase=result.get("new_phase"),
    )


@app.get(
    "/admin/agents/{agent_id}/qualify",
    response_model=QualificationResponse,
    responses={400: {"model": ApiError}, 503: {"model": ApiError}},
    summary="Check Phase-2 qualification criteria (read-only, testnet only)",
    tags=["admin"],
)
def admin_qualify_check(agent_id: int, response: Response) -> QualificationResponse:
    response.headers["Cache-Control"] = "no-store"
    _check_testnet()
    try:
        result = qualification.evaluate(agent_id)
    except ValueError as e:
        raise HTTPException(400, detail=ApiError(
            error=ApiErrorCode.BadRequest, message=str(e),
        ).model_dump(by_alias=True)) from e
    except Exception as e:
        raise HTTPException(503, detail=ApiError(
            error=ApiErrorCode.ChainUnreachable,
            message=f"qualify check failed: {e}",
        ).model_dump(by_alias=True)) from e
    return _qualification_to_response(agent_id, result)


@app.post(
    "/admin/agents/{agent_id}/qualify",
    response_model=QualificationResponse,
    responses={400: {"model": ApiError}, 503: {"model": ApiError}},
    summary="Evaluate qualification + auto-advance to PublicLaunch if passed",
    tags=["admin"],
)
def admin_qualify_advance(agent_id: int, response: Response) -> QualificationResponse:
    response.headers["Cache-Control"] = "no-store"
    _check_testnet()
    try:
        result = qualification.advance_if_passed(agent_id)
    except ValueError as e:
        raise HTTPException(400, detail=ApiError(
            error=ApiErrorCode.BadRequest, message=str(e),
        ).model_dump(by_alias=True)) from e
    except Exception as e:
        raise HTTPException(503, detail=ApiError(
            error=ApiErrorCode.ChainUnreachable,
            message=f"qualify advance failed: {e}",
        ).model_dump(by_alias=True)) from e
    return _qualification_to_response(agent_id, result)


@app.get(
    "/system/health",
    response_model=HealthResponse,
    summary="Service health (indexer, cron, LLM)",
    tags=["system"],
)
def system_health():
    _todo()


@app.get(
    "/health",
    summary="Liveness probe (Railway / Docker / k8s)",
    tags=["system"],
)
def health(response: Response) -> dict:
    """Lightweight probe — DB SELECT 1 must succeed (HTTP 503 otherwise);
    chain RPC reachability is reported but non-fatal."""
    response.headers["Cache-Control"] = "no-store"
    from sqlalchemy import text

    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception as e:
        raise HTTPException(503, detail={"ok": False, "db": str(e)[:120]}) from e

    chain_ok = True
    try:
        from app.chain.client import get_w3
        _ = get_w3().eth.block_number  # property triggers an RPC call
    except Exception:
        chain_ok = False

    return {"ok": True, "db": True, "chain": chain_ok}
