"""DividendDistributor event handlers.

ABI events:
    Distributed(uint256 agentId, uint256 epoch, uint256 totalAmount,
                uint256 holdersShare, uint256 carryShare, bytes32 snapshotRoot)
    Claimed(uint256 agentId, address holder, uint256 epoch, uint256 amount)
"""

from __future__ import annotations

import time

from sqlalchemy import select
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

    # Snapshot total supply at this block — the on-chain DividendDistributor
    # records totalSharesAtSnapshot in its EpochData; we replicate it in the BE
    # so claim-amount math works without a chain round-trip from the portfolio
    # endpoint.
    total_shares_snapshot = 0
    try:
        from app.chain.client import agent_token, get_w3
        agent = db.get(models.Agent, agent_id)
        if agent and agent.token_address:
            total_shares_snapshot = int(
                agent_token(agent.token_address)
                .functions.totalSupply()
                .call(block_identifier=int(event["blockNumber"]))
            )
    except Exception as e:
        print(f"[indexer] handle_distributed snapshot supply read failed: {e}")

    db.add(models.DividendEpoch(
        agent_id=agent_id,
        epoch=epoch,
        total_amount_usdc=str(total_amount),
        holders_share_usdc=str(holders_share),
        carry_share_usdc=str(carry_share),
        distributed_at=now,
        total_shares_at_snapshot=str(total_shares_snapshot),
    ))

    # Seed pending DividendClaim rows for every known holder so the portfolio
    # endpoint can surface "claimable" without re-reading the chain on every
    # request. Amount per holder = balance × holders_share / total_supply.
    # `claimed=False` flips to True when Claimed event fires (handle_claimed).
    if total_shares_snapshot > 0:
        holders = list(
            db.execute(
                select(models.Holder).where(models.Holder.agent_id == agent_id)
            ).scalars()
        )
        for h in holders:
            try:
                bal = int(h.balance or 0)
            except (TypeError, ValueError):
                bal = 0
            if bal == 0:
                continue
            pending = bal * holders_share // total_shares_snapshot
            if pending == 0:
                continue
            # Holder uses .address (col name); DividendClaim uses
            # .holder_address. Same field, different schemas.
            key = (agent_id, epoch, h.address)
            if db.get(models.DividendClaim, key):
                continue
            db.add(models.DividendClaim(
                agent_id=agent_id,
                epoch=epoch,
                holder_address=h.address,
                amount_usdc=str(pending),
                claimed=False,
                claimed_at=None,
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
