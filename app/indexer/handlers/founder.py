"""FounderVault event handlers (per-agent clones; events carry no agentId).

ABI events used:
    CarryReceived(uint256 amount)
    CarryClaimed(address founder, uint256 amount)
    SharesWithdrawn(address founder, uint256 amount)
    SubordinationTriggered(uint256 withdrawnRatioBps)

agent_id is resolved by looking up the FounderVault row whose ``address``
matches the contract address that emitted the event (event["address"]).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db import models


def _resolve_founder_vault(db: Session, event) -> models.FounderVault | None:
    addr = event["address"].lower()
    return (
        db.query(models.FounderVault)
        .filter(models.FounderVault.address == addr)
        .first()
    )


def handle_carry_received(db: Session, event) -> None:
    fv = _resolve_founder_vault(db, event)
    if fv is None:
        return
    amount = int(event["args"]["amount"])
    prev = int(fv.carry_balance_usdc or 0)
    fv.carry_balance_usdc = str(prev + amount)


def handle_carry_claimed(db: Session, event) -> None:
    fv = _resolve_founder_vault(db, event)
    if fv is None:
        return
    amount = int(event["args"]["amount"])
    prev = int(fv.carry_balance_usdc or 0)
    fv.carry_balance_usdc = str(max(0, prev - amount))


def handle_shares_withdrawn(db: Session, event) -> None:
    """Founder pulled some of their locked shares back. Reduces ``shares_held``."""
    fv = _resolve_founder_vault(db, event)
    if fv is None:
        return
    amount = int(event["args"]["amount"])
    prev = int(fv.shares_held or 0)
    fv.shares_held = str(max(0, prev - amount))


def handle_subordination_triggered(db: Session, event) -> None:
    """Sets cumulative_withdrawn_bps to the on-chain ratio and flips the
    subordination flag — emitted when the preventive cap is hit."""
    fv = _resolve_founder_vault(db, event)
    if fv is None:
        return
    fv.cumulative_withdrawn_bps = int(event["args"]["withdrawnRatioBps"])
    fv.is_subordination_active = True
