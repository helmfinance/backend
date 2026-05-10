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

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
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
):
    _todo()


@app.get(
    "/agents/{agent_id}",
    response_model=AgentDetail,
    responses={404: {"model": ApiError}},
    summary="Agent detail",
    tags=["agents"],
)
def get_agent(agent_id: int):
    _todo()


@app.get(
    "/agents/{agent_id}/nav-history",
    response_model=NavHistoryResponse,
    summary="NAV time series",
    tags=["agents"],
)
def get_nav_history(
    agent_id: int,
    period: NavPeriod = Query(NavPeriod.D7),
    granularity: NavGranularity | None = Query(None),
):
    _todo()


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
    summary="Per-wallet portfolio aggregate",
    tags=["portfolio"],
)
def get_portfolio(address: str):
    _todo()


@app.get(
    "/redemptions/{address}",
    response_model=list[RedemptionRequest],
    summary="Pending redemption requests for a wallet",
    tags=["portfolio"],
)
def get_redemptions(address: str):
    _todo()


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
