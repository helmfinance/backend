"""
Helm BE — Pydantic schemas (single source of truth).

Convention:
    - Big numbers (USDC, shares, wei) serialized as decimal strings.
    - All addresses are 0x-prefixed hex.
    - All timestamps are unix seconds (UTC).
    - All `*_bps` fields are basis points (10000 = 100%).
    - Field names are snake_case in Python, camelCase in JSON (alias_generator).

Workflow:
    1. Edit a model here.
    2. Restart uvicorn — FastAPI rebuilds OpenAPI at /openapi.json.
    3. FE runs `pnpm gen-types` to regenerate TypeScript.

Mirror of frontend/api-types.ts. Keep the two structures in sync (eventually
the TS file is generated, so this file becomes the only place to edit).
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Generic, Literal, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from pydantic.alias_generators import to_camel


# ─────────────────────────────────────────────────────────────────────────────
# Type aliases
# ─────────────────────────────────────────────────────────────────────────────

Hex = Annotated[str, StringConstraints(pattern=r"^0x[a-fA-F0-9]+$")]
"""0x-prefixed lowercase hex string (address or bytes32)."""

BigIntString = Annotated[str, StringConstraints(pattern=r"^\d+$")]
"""Decimal string representation of a bigint (USDC amounts, shares, wei)."""

UnixSeconds = int
"""Unix epoch seconds (UTC)."""

BasisPoints = Annotated[int, Field(ge=0, le=10000)]
"""Basis points: 0..10000 where 10000 = 100%."""


# ─────────────────────────────────────────────────────────────────────────────
# Base model with camelCase JSON serialization
# ─────────────────────────────────────────────────────────────────────────────

class HelmModel(BaseModel):
    """Base for all schemas. snake_case in Python, camelCase in JSON."""
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────

class AgentPhase(str, Enum):
    Incubation = "Incubation"
    PublicLaunch = "PublicLaunch"
    WindDown = "WindDown"
    Slashed = "Slashed"
    Settled = "Settled"


class AssetClass(str, Enum):
    Crypto = "crypto"
    Equity = "equity"
    Treasury = "treasury"
    Cash = "cash"
    Mixed = "mixed"


class LockupTier(str, Enum):
    Instant = "instant"
    ThirtyDay = "30d"
    SixtyDay = "60d"
    NinetyDay = "90d"


class DecisionType(str, Enum):
    Rebalance = "Rebalance"
    Harvest = "Harvest"
    Distribute = "Distribute"
    WindDown = "WindDown"


class NavPeriod(str, Enum):
    H24 = "24h"
    D7 = "7d"
    D30 = "30d"
    All = "all"


class NavGranularity(str, Enum):
    Minute = "minute"
    Hour = "hour"
    Day = "day"


class ApiErrorCode(str, Enum):
    BadRequest = "BadRequest"
    NotFound = "NotFound"
    RateLimited = "RateLimited"
    MandateParseFailed = "MandateParseFailed"
    MandateValidationFailed = "MandateValidationFailed"
    ChainUnreachable = "ChainUnreachable"
    InternalError = "InternalError"


class RedemptionStatus(str, Enum):
    Pending = "Pending"
    Claimable = "Claimable"
    Claimed = "Claimed"
    Cancelled = "Cancelled"


class HealthStatus(str, Enum):
    Ok = "ok"
    Degraded = "degraded"
    Down = "down"


# ─────────────────────────────────────────────────────────────────────────────
# Errors and pagination
# ─────────────────────────────────────────────────────────────────────────────

class ApiError(HelmModel):
    error: ApiErrorCode
    message: str
    details: dict | None = None


T = TypeVar("T")


class Page(HelmModel, Generic[T]):
    """Pagination envelope. Concrete instances: `Page[AgentSummary]`."""
    items: list[T]
    total: int
    limit: int
    offset: int


# ─────────────────────────────────────────────────────────────────────────────
# Position
# ─────────────────────────────────────────────────────────────────────────────

class Position(HelmModel):
    asset: Hex
    symbol: str                       # "sNVDA", "mETH", "USDY", "USDC"
    asset_class: AssetClass
    amount: BigIntString
    value_usdc: BigIntString
    weight_bps: BasisPoints
    price_usdc: BigIntString | None = None
    price_updated_at: UnixSeconds | None = None
    price_stale: bool | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Mandate
# ─────────────────────────────────────────────────────────────────────────────

class WeightConstraint(HelmModel):
    asset: str
    min_bps: BasisPoints
    max_bps: BasisPoints


class MandateSchema(HelmModel):
    version: Literal["1.0"]

    # Identity
    name: str
    ticker: str
    description: str

    # Strategy
    asset_classes: list[AssetClass]
    target_universe: list[str]
    weight_constraints: list[WeightConstraint]
    rebalance_frequency: Literal["daily", "weekly", "monthly", "event-driven"]
    rebalance_triggers: list[str]

    # User-facing terms
    allowed_lockups: list[LockupTier]
    minimum_deposit_usdc: BigIntString

    # Founder economics — immutable after publish
    founder_share_bps: BasisPoints
    carry_bps: BasisPoints
    founder_lockup_days: int
    subordination_threshold_bps: BasisPoints

    # Risk policy
    max_leverage: float
    max_single_position_bps: BasisPoints
    emergency_exit_conditions: list[str]

    # Marketplace flavor — optional, LLM-inferred from asset mix and tone.
    # Explicit alias keeps the acronym in camelCase (auto-generator → "Apy").
    expected_yield_apy: str | None = Field(default=None, alias="expectedYieldAPY")
    personality_hint: str | None = None


class MandateParseRequest(HelmModel):
    natural_language_mandate: Annotated[str, Field(min_length=10, max_length=5000)]
    hints: dict | None = None  # Partial<MandateSchema>; permissive shape on purpose


class MandateParseResponse(HelmModel):
    mandate: MandateSchema
    mandate_hash: Hex
    mandate_uri: str
    warnings: list[str]


class MandateValidateRequest(HelmModel):
    mandate: MandateSchema


class MandateValidateResponse(HelmModel):
    valid: bool
    errors: list[str]
    hash: Hex | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Agent — wind-down + nested snapshots
# ─────────────────────────────────────────────────────────────────────────────

class WindDownState(HelmModel):
    triggered_at: UnixSeconds
    triggered_by: Hex
    reason: str
    positions_remaining: int
    estimated_settle_at: UnixSeconds
    senior_claimable_usdc: BigIntString
    junior_claimable_usdc: BigIntString


class FounderVaultSnapshot(HelmModel):
    """Embedded inside AgentDetail. (Distinct from full FounderVaultPosition in Portfolio.)"""
    address: Hex
    shares_held: BigIntString
    lockup_ends_at: UnixSeconds
    cumulative_withdrawn_bps: BasisPoints
    is_subordination_active: bool
    carry_balance_usdc: BigIntString


class RedemptionQueueSnapshot(HelmModel):
    pending_shares: BigIntString
    request_count: int
    next_unlock_at: UnixSeconds | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Decision log + narrator
# ─────────────────────────────────────────────────────────────────────────────

class HarvestSource(HelmModel):
    source: Hex
    amount: BigIntString


class Decision(HelmModel):
    id: str                           # "<txHash>:<logIndex>"
    type: DecisionType
    timestamp: UnixSeconds
    tx_hash: Hex
    block_number: int
    summary: str

    # Rebalance-only
    before_positions: list[Position] | None = None
    after_positions: list[Position] | None = None
    nav_before: BigIntString | None = None
    nav_after: BigIntString | None = None

    # Harvest-only
    harvested_usdc: BigIntString | None = None
    harvested_from_sources: list[HarvestSource] | None = None

    # Distribute-only
    distributed_epoch: int | None = None
    distributed_holders_usdc: BigIntString | None = None
    distributed_carry_usdc: BigIntString | None = None


class NarratorPerformance(HelmModel):
    nav_start: BigIntString
    nav_end: BigIntString
    return_bps: int                   # signed; negative = loss


class NarratorNote(HelmModel):
    generated_at: UnixSeconds
    week_start: UnixSeconds
    week_end: UnixSeconds
    body_markdown: str
    performance: NarratorPerformance


# ─────────────────────────────────────────────────────────────────────────────
# Dividend epoch (referenced by AgentDetail and DividendClaim)
# ─────────────────────────────────────────────────────────────────────────────

class DividendEpoch(HelmModel):
    epoch: int
    agent_id: int
    total_amount_usdc: BigIntString
    holders_share_usdc: BigIntString
    carry_share_usdc: BigIntString
    distributed_at: UnixSeconds
    total_shares_at_snapshot: BigIntString


# ─────────────────────────────────────────────────────────────────────────────
# Agent — summary + detail
# ─────────────────────────────────────────────────────────────────────────────

class AgentPerformance(HelmModel):
    total_return: float | None = None         # 0.184 = +18.4%
    max_drawdown: float | None = None         # -0.072 = -7.2%
    sharpe_ratio: float | None = None         # None if insufficient samples
    sample_count: int
    period_start: UnixSeconds | None = None
    period_end: UnixSeconds | None = None


class AgentSummary(HelmModel):
    agent_id: int
    name: str
    ticker: str
    founder_address: Hex
    vault_address: Hex
    token_address: Hex

    phase: AgentPhase
    incubation_start: UnixSeconds
    public_launch_at: UnixSeconds | None = None

    nav_usdc: BigIntString
    nav_per_share_usdc: BigIntString
    total_shares: BigIntString
    holder_count: int

    apy_30d_bps: BasisPoints | None = None
    apy_7d_bps: BasisPoints | None = None
    reputation: int

    strategy: str                     # 1-line, marketplace-card-ready
    asset_classes: list[AssetClass]
    allowed_lockups: list[LockupTier]

    thumbnail_url: str | None = None
    created_at: UnixSeconds

    performance: AgentPerformance | None = None  # additive

    # Simulated secondary-market price (NAV × reputation premium factor)
    market_price_per_share_usdc: BigIntString | None = None
    reputation_premium_bps: int | None = None  # signed: +ve premium, -ve discount


class AgentDetail(AgentSummary):
    mandate: MandateSchema
    mandate_uri: str
    mandate_hash: Hex

    positions: list[Position]
    cash_usdc: BigIntString
    yield_pool: BigIntString

    founder_vault: FounderVaultSnapshot

    recent_dividends: list[DividendEpoch]
    recent_decisions: list[Decision]
    latest_narrator_note: NarratorNote | None = None

    redemption_queue: RedemptionQueueSnapshot
    wind_down: WindDownState | None = None


# ─────────────────────────────────────────────────────────────────────────────
# NAV history
# ─────────────────────────────────────────────────────────────────────────────

class NavPoint(HelmModel):
    timestamp: UnixSeconds
    nav_usdc: BigIntString
    nav_per_share_usdc: BigIntString
    total_shares: BigIntString


class NavHistoryResponse(HelmModel):
    points: list[NavPoint]
    period: NavPeriod
    granularity: NavGranularity


# ─────────────────────────────────────────────────────────────────────────────
# Benchmark comparison (agent NAV vs naive baselines)
# ─────────────────────────────────────────────────────────────────────────────

class BenchmarkPoint(HelmModel):
    timestamp: UnixSeconds
    agent: float          # normalized (starts at 1.0)
    sspy: float
    sixty_forty: float


class BenchmarkSummary(HelmModel):
    agent_return: float           # 0.0277 = +2.77%
    sspy_return: float
    sixty_forty_return: float
    alpha_vs_sspy: float          # agent - sspy
    alpha_vs_sixty_forty: float


class BenchmarkResponse(HelmModel):
    agent_id: int
    period_start: UnixSeconds | None = None
    period_end: UnixSeconds | None = None
    sample_count: int
    series: list[BenchmarkPoint]
    summary: BenchmarkSummary | None = None  # None when < 2 samples


# ─────────────────────────────────────────────────────────────────────────────
# Holders
# ─────────────────────────────────────────────────────────────────────────────

class Holder(HelmModel):
    address: Hex
    balance: BigIntString
    weight_bps: BasisPoints
    first_held_at: UnixSeconds
    cumulative_dividends_claimed_usdc: BigIntString


# ─────────────────────────────────────────────────────────────────────────────
# Dividends, redemptions, portfolio
# ─────────────────────────────────────────────────────────────────────────────

class DividendClaim(HelmModel):
    agent_id: int
    agent_name: str
    ticker: str
    claimable_epochs: list[int]
    claimable_amount_usdc: BigIntString
    oldest_epoch_at: UnixSeconds


class RedemptionRequest(HelmModel):
    request_id: int
    agent_id: int
    agent_name: str
    shares: BigIntString
    tier: LockupTier
    unlock_at: UnixSeconds
    estimated_usdc: BigIntString
    status: RedemptionStatus


class PortfolioPosition(HelmModel):
    agent_id: int
    agent_name: str
    ticker: str
    vault_address: Hex
    token_address: Hex
    shares: BigIntString
    weight_bps: BasisPoints           # share of user's portfolio
    value_usdc: BigIntString
    cost_basis_usdc: BigIntString | None = None
    unrealized_pnl_usdc: BigIntString | None = None


class FounderVaultPosition(HelmModel):
    agent_id: int
    agent_name: str
    vault_address: Hex
    founder_vault_address: Hex
    shares_locked: BigIntString
    lockup_ends_at: UnixSeconds
    carry_balance_usdc: BigIntString
    cumulative_withdrawn_bps: BasisPoints
    is_subordination_active: bool


class PortfolioResponse(HelmModel):
    total_value_usdc: BigIntString
    positions: list[PortfolioPosition]
    pending_dividends: list[DividendClaim]
    pending_redemptions: list[RedemptionRequest]
    founder_vaults: list[FounderVaultPosition]


# ─────────────────────────────────────────────────────────────────────────────
# Mint preview + Pyth update bytes (see ADR D001 + D002)
# ─────────────────────────────────────────────────────────────────────────────

class MintPreviewRequest(HelmModel):
    amount_usdc: BigIntString          # 6-decimal USDC string, e.g. "100000000" = 100 USDC


class SyntheticPricePreview(HelmModel):
    symbol: str                        # "sNVDA"
    price_usdc: BigIntString           # 6-decimal USDC string
    pyth_confidence: BigIntString      # Pyth confidence interval (same scale)
    fresh_at: UnixSeconds              # Pyth publish_time of the price


class MintPreviewResponse(HelmModel):
    amount_usdc: BigIntString          # echoed back
    shares: BigIntString               # AGT shares the user will receive (18 decimals)
    nav_at_preview: BigIntString       # NAV per share at preview time (6 decimals)
    platform_fee_usdc: BigIntString    # mint fee deducted
    pyth_fee_mnt_wei: BigIntString     # MNT to attach to vault.mint() call (msg.value)
    valid_until: UnixSeconds           # preview becomes stale after this; FE should refresh
    synthetic_prices: list[SyntheticPricePreview]  # transparency: equity prices used


class PythUpdateBytesResponse(HelmModel):
    update_data: list[Hex]             # bytes[] from Hermes, ready for vault.mint(updateData)
    fee_mnt_wei: BigIntString          # call vault.mint() with this MNT value attached
    feeds: list[str]                   # which feed symbols were included (e.g. ["NVDA", "SPY"])
    fetched_at: UnixSeconds            # when BE pulled from Hermes


# ─────────────────────────────────────────────────────────────────────────────
# System info
# ─────────────────────────────────────────────────────────────────────────────

class ContractAddresses(HelmModel):
    helm_registry: Hex
    platform_treasury: Hex
    redemption_queue: Hex
    yield_harvester: Hex
    dividend_distributor: Hex
    pyth_price_adapter: Hex
    mantle_meth_adapter: Hex
    ondo_usdy_adapter: Hex
    pyth: Hex
    usdc: Hex

    # NEW — added 2026-05 after SC deploy. Additive only; FE can ignore.
    agent_nft: Hex
    time_provider: Hex
    agent_token_impl: Hex
    agent_vault_impl: Hex
    founder_vault_impl: Hex


class FeeRates(HelmModel):
    mint_bps: BasisPoints
    redeem_bps: BasisPoints
    rebalance_bps: BasisPoints


class SyntheticAssetInfo(HelmModel):
    address: Hex
    symbol: str                       # "sNVDA"
    underlying: str                   # "NVDA"
    pyth_feed_id: Hex


class SystemInfo(HelmModel):
    chain_id: int                     # 5003 = Mantle Sepolia, 5000 = Mantle
    rpc_url: str
    block_explorer_url: str
    contracts: ContractAddresses
    fee_rates: FeeRates
    pyth_feed_ids: dict[str, Hex]     # { "sNVDA": "0xb107...", ... }
    synthetic_assets: list[SyntheticAssetInfo]


# ─────────────────────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────────────────────

class IndexerHealth(HelmModel):
    last_synced_block: int
    blocks_behind: int
    healthy: bool


class CronHealth(HelmModel):
    last_harvest_run: UnixSeconds
    last_dividend_run: UnixSeconds
    healthy: bool


class LlmHealth(HelmModel):
    last_call_at: UnixSeconds
    last_24h_success_rate: float      # 0..1
    healthy: bool


class HealthResponse(HelmModel):
    status: HealthStatus
    indexer: IndexerHealth
    cron: CronHealth
    llm: LlmHealth


# ─────────────────────────────────────────────────────────────────────────────
# Streaming events (v2 — optional)
# ─────────────────────────────────────────────────────────────────────────────

class NavStreamEvent(HelmModel):
    kind: Literal["nav"]
    nav_usdc: BigIntString
    nav_per_share_usdc: BigIntString
    at: UnixSeconds


class DecisionStreamEvent(HelmModel):
    kind: Literal["decision"]
    decision: Decision


class RedemptionStreamEvent(HelmModel):
    kind: Literal["redemption"]
    request: RedemptionRequest


class DividendStreamEvent(HelmModel):
    kind: Literal["dividend"]
    epoch: DividendEpoch


AgentStreamEvent = Annotated[
    Union[NavStreamEvent, DecisionStreamEvent, RedemptionStreamEvent, DividendStreamEvent],
    Field(discriminator="kind"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Admin (testnet only)
# ─────────────────────────────────────────────────────────────────────────────

class TimeAdvanceRequest(HelmModel):
    seconds: int = Field(ge=1, le=86400 * 365)  # 1s ~ 1y


class TimeAdvanceResponse(HelmModel):
    tx_hash: str
    advanced_seconds: int
    new_current_time: int


class MintUsdcRequest(HelmModel):
    to: Hex
    amount_usdc: BigIntString  # 6-decimal integer string


class MintUsdcResponse(HelmModel):
    tx_hash: str
    to: Hex
    amount_usdc: BigIntString


# ─────────────────────────────────────────────────────────────────────────────
# Admin — K service triggers (testnet only)
# ─────────────────────────────────────────────────────────────────────────────

class AdminRebalanceResponse(HelmModel):
    agent_id: int
    tx_hash: str
    target_weights: list[tuple[str, int]]


class AdminHarvestResponse(HelmModel):
    agent_id: int
    tx_hash: str


class AdminDistributeResponse(HelmModel):
    agent_id: int
    amount: BigIntString
    stage_tx_hash: str | None = None
    distribute_tx_hash: str | None = None
    note: str | None = None


class AdminNftMetadataResponse(HelmModel):
    agent_id: int
    tx_hash: str
    uri: str
    attribute_count: int | None = None


class QualificationCriterion(HelmModel):
    name: str                  # e.g. "continuous_days"
    description: str
    passed: bool
    actual: str
    threshold: str
    note: str | None = None


class QualificationResponse(HelmModel):
    agent_id: int
    overall_passed: bool
    checks: list[QualificationCriterion]
    advanced: bool             # True if this call triggered advanceToPublic
    tx_hash: str | None = None
    new_phase: str | None = None


__all__ = [
    "Hex", "BigIntString", "UnixSeconds", "BasisPoints",
    "HelmModel",
    "AgentPhase", "AssetClass", "LockupTier", "DecisionType",
    "NavPeriod", "NavGranularity", "ApiErrorCode", "RedemptionStatus", "HealthStatus",
    "ApiError", "Page",
    "Position",
    "WeightConstraint", "MandateSchema",
    "MandateParseRequest", "MandateParseResponse",
    "MandateValidateRequest", "MandateValidateResponse",
    "WindDownState", "FounderVaultSnapshot", "RedemptionQueueSnapshot",
    "AgentSummary", "AgentDetail", "AgentPerformance",
    "Decision", "HarvestSource",
    "NarratorPerformance", "NarratorNote",
    "DividendEpoch",
    "NavPoint", "NavHistoryResponse",
    "BenchmarkPoint", "BenchmarkSummary", "BenchmarkResponse",
    "Holder",
    "DividendClaim", "RedemptionRequest",
    "MintPreviewRequest", "MintPreviewResponse", "SyntheticPricePreview",
    "PythUpdateBytesResponse",
    "PortfolioPosition", "FounderVaultPosition", "PortfolioResponse",
    "ContractAddresses", "FeeRates", "SyntheticAssetInfo", "SystemInfo",
    "IndexerHealth", "CronHealth", "LlmHealth", "HealthResponse",
    "NavStreamEvent", "DecisionStreamEvent", "RedemptionStreamEvent", "DividendStreamEvent",
    "AgentStreamEvent",
    "TimeAdvanceRequest", "TimeAdvanceResponse",
    "MintUsdcRequest", "MintUsdcResponse",
    "AdminRebalanceResponse", "AdminHarvestResponse",
    "AdminDistributeResponse", "AdminNftMetadataResponse",
    "QualificationCriterion", "QualificationResponse",
]
