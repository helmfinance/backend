"""AgentToken (ERC-20) event handlers.

The vault emits ERC-4626 Deposit(owner=...) but on the public-mint path that
"owner" is the RegistryEntrypoint contract, not the wallet that actually ends
up holding shares. The end-user wallet only appears in the subsequent
AgentToken Transfer event. So Holder.address must be sourced from Transfer,
not from Deposit, for the DB to match what the chain says about share
custody — and that's what the DividendDistributor uses to seed claims.

Events handled:
    Transfer(address indexed from, address indexed to, uint256 value)

Mint: from = 0x0   → only "to" is credited.
Burn: to   = 0x0   → only "from" is debited.
Transfer: both sides update.
"""

from __future__ import annotations

import time

from sqlalchemy.orm import Session

from app.db import models

ZERO = "0x" + "00" * 20


def _find_agent_by_token(db: Session, token_addr: str):
    return (
        db.query(models.Agent)
        .filter(models.Agent.token_address == token_addr)
        .first()
    )


def _addr(raw) -> str:
    s = raw if isinstance(raw, str) else raw.hex()
    return s.lower()


def _credit(db: Session, agent_id: int, holder_addr: str, delta: int) -> None:
    """Apply ``delta`` (positive = inbound, negative = outbound) to Holder.

    The INSERT path uses dialect ON CONFLICT DO NOTHING because two indexer
    cycles (APScheduler tick + an HTTP-triggered debug tick) can race and
    both attempt to seed the same (agent_id, holder_addr); the loser would
    otherwise abort the entire chunk on commit. The UPDATE path is safe to
    re-run because the second writer recomputes balance from the persisted
    row.
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    h = db.get(models.Holder, (agent_id, holder_addr))
    now = int(time.time())
    if h:
        new_bal = int(h.balance or 0) + delta
        if new_bal < 0:
            new_bal = 0  # defensive — should never happen if events are ordered
        h.balance = str(new_bal)
        return

    stmt = sqlite_insert(models.Holder).values(
        agent_id=agent_id,
        address=holder_addr,
        balance=str(max(delta, 0)),
        weight_bps=0,
        first_held_at=now,
        cumulative_dividends_claimed_usdc="0",
    ).on_conflict_do_nothing(index_elements=["agent_id", "address"])
    db.execute(stmt)
    # autoflush=False: the next event in this chunk that touches the same
    # holder would still see None via db.get() unless we flush. The flush
    # itself can't conflict because the INSERT above is OR-IGNORE.
    db.flush()


def handle_transfer(db: Session, event) -> None:
    args = event["args"]
    token_addr = event["address"].lower()
    agent = _find_agent_by_token(db, token_addr)
    if not agent:
        return

    src = _addr(args["from"])
    dst = _addr(args["to"])
    value = int(args["value"])
    if value == 0:
        return

    if src != ZERO:
        _credit(db, agent.agent_id, src, -value)
    if dst != ZERO:
        _credit(db, agent.agent_id, dst, value)
