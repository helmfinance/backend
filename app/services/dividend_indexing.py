"""Shared logic for materializing a Distributed event into DB rows.

Called from two paths:
1. Indexer event handler (indexer/handlers/distributor.py) — async, picks up
   the event from chain logs during the next index cycle.
2. Distribute service (services/distribute.py) — synchronous, runs immediately
   after the distribute() tx receipt so the FE Portfolio surfaces the new
   claimable amount without waiting for indexer lag.

Both paths produce the same rows; each insert is independently idempotent so
calling twice for the same event is safe.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chain.client import agent_token
from app.db import models

log = logging.getLogger(__name__)


def materialize_distributed_event(
    db: Session,
    *,
    agent_id: int,
    epoch: int,
    total_amount: int,
    holders_share: int,
    carry_share: int,
    block_number: int,
    tx_hash: str,
    log_index: int,
    token_address: str | None,
    now_ts: int | None = None,
) -> None:
    """Insert DividendEpoch + per-holder DividendClaim + Decision rows for a
    Distributed event. Each insert is independently idempotent.
    """
    if now_ts is None:
        now_ts = int(time.time())

    epoch_exists = db.get(models.DividendEpoch, (agent_id, epoch)) is not None
    decision_id = f"{tx_hash}:{log_index}"
    decision_exists = db.get(models.Decision, decision_id) is not None

    if epoch_exists and decision_exists:
        return

    if not epoch_exists:
        # Snapshot total supply at the dist block — replicates the on-chain
        # DividendDistributor's totalSharesAtSnapshot so claim math works
        # without a chain round-trip from the portfolio endpoint.
        total_shares_snapshot = 0
        if token_address:
            token = agent_token(token_address)
            try:
                total_shares_snapshot = int(
                    token.functions.totalSupply()
                    .call(block_identifier=block_number)
                )
            except Exception:
                # Mantle public RPC frequently refuses historical eth_call.
                # Latest totalSupply equals snapshot-at-dist-block as long as
                # no other distribute ran between (we serialize per agent).
                try:
                    total_shares_snapshot = int(
                        token.functions.totalSupply().call()
                    )
                except Exception as ts_err:
                    log.warning(
                        "[dividend_indexing] totalSupply read failed for "
                        "agent %s: %s", agent_id, ts_err,
                    )

        db.add(models.DividendEpoch(
            agent_id=agent_id,
            epoch=epoch,
            total_amount_usdc=str(total_amount),
            holders_share_usdc=str(holders_share),
            carry_share_usdc=str(carry_share),
            distributed_at=now_ts,
            total_shares_at_snapshot=str(total_shares_snapshot),
        ))

        # Seed pending DividendClaim rows for every known holder so the
        # portfolio endpoint surfaces "claimable" without re-reading the chain
        # on every request. `claimed=False` flips to True when the Claimed
        # event fires (handle_claimed).
        if total_shares_snapshot > 0:
            holders = list(db.execute(
                select(models.Holder).where(models.Holder.agent_id == agent_id)
            ).scalars())
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
                # Holder.address is the column name; DividendClaim uses
                # .holder_address. Mixing them up silently raises
                # AttributeError and loses the whole epoch.
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

    if not decision_exists:
        db.add(models.Decision(
            id=decision_id,
            agent_id=agent_id,
            type="Distribute",
            timestamp=now_ts,
            tx_hash=tx_hash,
            block_number=block_number,
            summary=f"Distributed epoch {epoch}: {total_amount} USDC "
                    f"({holders_share} holders / {carry_share} carry)",
            distributed_epoch=epoch,
            distributed_holders_usdc=str(holders_share),
            distributed_carry_usdc=str(carry_share),
        ))
