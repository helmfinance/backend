"""DividendDistributor event handlers.

ABI events:
    Distributed(uint256 agentId, uint256 epoch, uint256 totalAmount,
                uint256 holdersShare, uint256 carryShare, bytes32 snapshotRoot)
    Claimed(uint256 agentId, address holder, uint256 epoch, uint256 amount)
"""

from __future__ import annotations

import time

from sqlalchemy.orm import Session

from app.db import models


def _tx_hash(event) -> str:
    raw = event["transactionHash"]
    return raw.hex() if hasattr(raw, "hex") else raw


def handle_distributed(db: Session, event) -> None:
    args = event["args"]
    agent_id = int(args["agentId"])
    epoch = int(args["epoch"])

    # Idempotent on (agent_id, epoch) composite PK
    if db.get(models.DividendEpoch, (agent_id, epoch)):
        return

    total_amount = int(args["totalAmount"])
    holders_share = int(args["holdersShare"])
    carry_share = int(args["carryShare"])
    now = int(time.time())

    db.add(models.DividendEpoch(
        agent_id=agent_id,
        epoch=epoch,
        total_amount_usdc=str(total_amount),
        holders_share_usdc=str(holders_share),
        carry_share_usdc=str(carry_share),
        distributed_at=now,
        total_shares_at_snapshot="0",  # snapshot root carries Merkle commitment; supply unknown
    ))

    # Mirror as a Decision row for the agent's decision log
    tx_hash = _tx_hash(event)
    decision_id = f"{tx_hash}:{event['logIndex']}"
    if not db.get(models.Decision, decision_id):
        db.add(models.Decision(
            id=decision_id,
            agent_id=agent_id,
            type="Distribute",
            timestamp=now,
            tx_hash=tx_hash,
            block_number=event["blockNumber"],
            summary=f"Distributed epoch {epoch}: {total_amount} USDC "
                    f"({holders_share} holders / {carry_share} carry)",
            distributed_epoch=epoch,
            distributed_holders_usdc=str(holders_share),
            distributed_carry_usdc=str(carry_share),
        ))


def handle_claimed(db: Session, event) -> None:
    args = event["args"]
    agent_id = int(args["agentId"])
    epoch = int(args["epoch"])
    holder_raw = args["holder"]
    holder = (holder_raw if isinstance(holder_raw, str) else holder_raw.hex()).lower()
    amount = int(args["amount"])
    now = int(time.time())

    existing = db.get(models.DividendClaim, (agent_id, epoch, holder))
    if existing:
        if existing.claimed:
            return
        existing.claimed = True
        existing.claimed_at = now
        existing.amount_usdc = str(amount)
    else:
        db.add(models.DividendClaim(
            agent_id=agent_id,
            epoch=epoch,
            holder_address=holder,
            amount_usdc=str(amount),
            claimed=True,
            claimed_at=now,
        ))

    # Update holder's cumulative claimed counter
    h = db.get(models.Holder, (agent_id, holder))
    if h:
        prev = int(h.cumulative_dividends_claimed_usdc or 0)
        h.cumulative_dividends_claimed_usdc = str(prev + amount)
