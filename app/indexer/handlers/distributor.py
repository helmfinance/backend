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
from app.services.dividend_indexing import materialize_distributed_event


def _tx_hash(event) -> str:
    raw = event["transactionHash"]
    return raw.hex() if hasattr(raw, "hex") else raw


def handle_distributed(db: Session, event) -> None:
    args = event["args"]
    agent_id = int(args["agentId"])
    agent = db.get(models.Agent, agent_id)
    materialize_distributed_event(
        db,
        agent_id=agent_id,
        epoch=int(args["epoch"]),
        total_amount=int(args["totalAmount"]),
        holders_share=int(args["holdersShare"]),
        carry_share=int(args["carryShare"]),
        block_number=int(event["blockNumber"]),
        tx_hash=_tx_hash(event),
        log_index=int(event["logIndex"]),
        token_address=(agent.token_address if agent else None),
    )


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
