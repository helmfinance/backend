"""
Helm BE — SQLAlchemy 2.0 ORM models.

Conventions:
    - Big numbers (USDC, shares, wei) → String, to avoid SQLite int overflow and
      to round-trip 1:1 with Pydantic BigIntString.
    - Unix timestamps → BigInteger.
    - Addresses → String(42); bytes32 / tx hashes → String(66).
    - Enum-like values → String, validated at the Pydantic boundary.
    - Nested object payloads → JSON.

Naming convention for indexes / constraints is enforced by `Base.metadata` in
`app.db.base` so that SQLite ↔ Postgres migrations stay portable.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


# ─────────────────────────────────────────────────────────────────────────────
# 1. agents
# ─────────────────────────────────────────────────────────────────────────────

class Agent(Base):
    __tablename__ = "agents"

    agent_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False)

    founder_address: Mapped[str] = mapped_column(String(42), nullable=False, index=True)
    vault_address: Mapped[str] = mapped_column(String(42), nullable=False, unique=True)
    token_address: Mapped[str] = mapped_column(String(42), nullable=False, unique=True)
    founder_vault_address: Mapped[str] = mapped_column(String(42), nullable=False, unique=True)

    phase: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    incubation_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    public_launch_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    mandate: Mapped[dict] = mapped_column(JSON, nullable=False)
    mandate_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    mandate_hash: Mapped[str] = mapped_column(String(66), nullable=False, unique=True)

    reputation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    thumbnail_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Relationships
    positions: Mapped[list["Position"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )
    nav_points: Mapped[list["NavPoint"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )
    decisions: Mapped[list["Decision"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )
    dividend_epochs: Mapped[list["DividendEpoch"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )
    holders: Mapped[list["Holder"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )
    redemption_requests: Mapped[list["RedemptionRequest"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )
    narrator_notes: Mapped[list["NarratorNote"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )
    founder_vault: Mapped["FounderVault | None"] = relationship(
        back_populates="agent", uselist=False, cascade="all, delete-orphan"
    )
    wind_down: Mapped["WindDownState | None"] = relationship(
        back_populates="agent", uselist=False, cascade="all, delete-orphan"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. positions  (current snapshot, overwrite on rebalance)
# ─────────────────────────────────────────────────────────────────────────────

class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agents.agent_id"), nullable=False, index=True
    )
    asset_address: Mapped[str] = mapped_column(String(42), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[str] = mapped_column(String, nullable=False)
    value_usdc: Mapped[str] = mapped_column(String, nullable=False)
    weight_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    price_usdc: Mapped[str | None] = mapped_column(String, nullable=True)
    price_updated_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    price_stale: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    agent: Mapped["Agent"] = relationship(back_populates="positions")

    __table_args__ = (
        UniqueConstraint("agent_id", "asset_address"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. nav_history  (append-only time series)
# ─────────────────────────────────────────────────────────────────────────────

class NavPoint(Base):
    __tablename__ = "nav_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agents.agent_id"), nullable=False
    )
    timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    nav_usdc: Mapped[str] = mapped_column(String, nullable=False)
    nav_per_share_usdc: Mapped[str] = mapped_column(String, nullable=False)
    total_shares: Mapped[str] = mapped_column(String, nullable=False)

    agent: Mapped["Agent"] = relationship(back_populates="nav_points")

    __table_args__ = (
        Index("ix_nav_history_agent_id_timestamp", "agent_id", "timestamp"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. decisions  (single table for all 4 types)
# ─────────────────────────────────────────────────────────────────────────────

class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agents.agent_id"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tx_hash: Mapped[str] = mapped_column(String(66), nullable=False)
    block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    # Rebalance-only
    before_positions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    after_positions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    nav_before: Mapped[str | None] = mapped_column(String, nullable=True)
    nav_after: Mapped[str | None] = mapped_column(String, nullable=True)

    # Harvest-only
    harvested_usdc: Mapped[str | None] = mapped_column(String, nullable=True)
    harvested_from_sources: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Distribute-only
    distributed_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    distributed_holders_usdc: Mapped[str | None] = mapped_column(String, nullable=True)
    distributed_carry_usdc: Mapped[str | None] = mapped_column(String, nullable=True)

    agent: Mapped["Agent"] = relationship(back_populates="decisions")

    __table_args__ = (
        Index("ix_decisions_agent_id_timestamp", "agent_id", "timestamp"),
        Index("ix_decisions_agent_id_type", "agent_id", "type"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5. dividend_epochs  (composite PK)
# ─────────────────────────────────────────────────────────────────────────────

class DividendEpoch(Base):
    __tablename__ = "dividend_epochs"

    agent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agents.agent_id"), primary_key=True
    )
    epoch: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    total_amount_usdc: Mapped[str] = mapped_column(String, nullable=False)
    holders_share_usdc: Mapped[str] = mapped_column(String, nullable=False)
    carry_share_usdc: Mapped[str] = mapped_column(String, nullable=False)
    distributed_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_shares_at_snapshot: Mapped[str] = mapped_column(String, nullable=False)

    agent: Mapped["Agent"] = relationship(back_populates="dividend_epochs")
    claims: Mapped[list["DividendClaim"]] = relationship(
        back_populates="epoch_row",
        viewonly=True,
        primaryjoin=(
            "and_(DividendEpoch.agent_id == foreign(DividendClaim.agent_id), "
            "DividendEpoch.epoch == foreign(DividendClaim.epoch))"
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 6. dividend_claims  (composite PK; composite FK to dividend_epochs)
# ─────────────────────────────────────────────────────────────────────────────

class DividendClaim(Base):
    __tablename__ = "dividend_claims"

    agent_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    epoch: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    holder_address: Mapped[str] = mapped_column(String(42), primary_key=True)
    amount_usdc: Mapped[str] = mapped_column(String, nullable=False)
    claimed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    claimed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    epoch_row: Mapped["DividendEpoch"] = relationship(
        back_populates="claims",
        viewonly=True,
        primaryjoin=(
            "and_(DividendEpoch.agent_id == foreign(DividendClaim.agent_id), "
            "DividendEpoch.epoch == foreign(DividendClaim.epoch))"
        ),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_id", "epoch"],
            ["dividend_epochs.agent_id", "dividend_epochs.epoch"],
        ),
        Index("ix_dividend_claims_holder_address_claimed", "holder_address", "claimed"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 7. holders  (composite PK)
# ─────────────────────────────────────────────────────────────────────────────

class Holder(Base):
    __tablename__ = "holders"

    agent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agents.agent_id"), primary_key=True
    )
    address: Mapped[str] = mapped_column(String(42), primary_key=True)
    balance: Mapped[str] = mapped_column(String, nullable=False)
    weight_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    first_held_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cumulative_dividends_claimed_usdc: Mapped[str] = mapped_column(
        String, nullable=False, default="0"
    )

    agent: Mapped["Agent"] = relationship(back_populates="holders")

    __table_args__ = (
        Index("ix_holders_address", "address"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 8. redemption_requests
# ─────────────────────────────────────────────────────────────────────────────

class RedemptionRequest(Base):
    __tablename__ = "redemption_requests"

    request_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    agent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agents.agent_id"), nullable=False
    )
    holder_address: Mapped[str] = mapped_column(String(42), nullable=False)
    shares: Mapped[str] = mapped_column(String, nullable=False)
    tier: Mapped[str] = mapped_column(String(20), nullable=False)
    requested_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unlock_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    estimated_usdc: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    claimed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    agent: Mapped["Agent"] = relationship(back_populates="redemption_requests")

    __table_args__ = (
        Index("ix_redemption_requests_agent_id_status", "agent_id", "status"),
        Index("ix_redemption_requests_holder_address_status", "holder_address", "status"),
        Index("ix_redemption_requests_unlock_at", "unlock_at"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 9. narrator_notes
# ─────────────────────────────────────────────────────────────────────────────

class NarratorNote(Base):
    __tablename__ = "narrator_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agents.agent_id"), nullable=False
    )
    week_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    week_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    generated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    nav_start: Mapped[str] = mapped_column(String, nullable=False)
    nav_end: Mapped[str] = mapped_column(String, nullable=False)
    return_bps: Mapped[int] = mapped_column(Integer, nullable=False)

    agent: Mapped["Agent"] = relationship(back_populates="narrator_notes")

    __table_args__ = (
        UniqueConstraint("agent_id", "week_start"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 10. founder_vaults  (1:1 with agents)
# ─────────────────────────────────────────────────────────────────────────────

class FounderVault(Base):
    __tablename__ = "founder_vaults"

    agent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agents.agent_id"), primary_key=True
    )
    address: Mapped[str] = mapped_column(String(42), nullable=False)
    shares_held: Mapped[str] = mapped_column(String, nullable=False)
    lockup_ends_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cumulative_withdrawn_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_subordination_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    carry_balance_usdc: Mapped[str] = mapped_column(String, nullable=False, default="0")

    agent: Mapped["Agent"] = relationship(back_populates="founder_vault")


# ─────────────────────────────────────────────────────────────────────────────
# 11. wind_down_states  (0..1 per agent)
# ─────────────────────────────────────────────────────────────────────────────

class WindDownState(Base):
    __tablename__ = "wind_down_states"

    agent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agents.agent_id"), primary_key=True
    )
    triggered_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    triggered_by: Mapped[str] = mapped_column(String(42), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    positions_remaining: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_settle_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    senior_claimable_usdc: Mapped[str] = mapped_column(String, nullable=False)
    junior_claimable_usdc: Mapped[str] = mapped_column(String, nullable=False)

    agent: Mapped["Agent"] = relationship(back_populates="wind_down")


# ─────────────────────────────────────────────────────────────────────────────
# 12. mandate_blobs  (independent — written by /mandate/parse)
# ─────────────────────────────────────────────────────────────────────────────

class MandateBlob(Base):
    __tablename__ = "mandate_blobs"

    mandate_hash: Mapped[str] = mapped_column(String(66), primary_key=True)
    mandate_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    ipfs_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    pinned_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


# ─────────────────────────────────────────────────────────────────────────────
# 13. indexer_state  (one row per chain)
# ─────────────────────────────────────────────────────────────────────────────

class IndexerState(Base):
    __tablename__ = "indexer_state"

    chain_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    last_synced_block: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


__all__ = [
    "Agent",
    "Position",
    "NavPoint",
    "Decision",
    "DividendEpoch",
    "DividendClaim",
    "Holder",
    "RedemptionRequest",
    "NarratorNote",
    "FounderVault",
    "WindDownState",
    "MandateBlob",
    "IndexerState",
]
