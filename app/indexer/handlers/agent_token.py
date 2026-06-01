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
    """Apply ``delta`` (positive = inbound, negative = outbound) to Holder."""
    h = db.get(models.Holder, (agent_id, holder_addr))
    now = int(time.time())
    if h:
        new_bal = int(h.balance or 0) + delta
        if new_bal < 0:
            new_bal = 0  # defensive — should never happen if events are ordered
        h.balance = str(new_bal)
        return
    db.add(models.Holder(
        agent_id=agent_id,
        address=holder_addr,
        balance=str(max(delta, 0)),
        weight_bps=0,
        first_held_at=now,
        cumulative_dividends_claimed_usdc="0",
    ))
    # SessionLocal is autoflush=False, so the next Transfer event for the
    # same (agent_id, holder_addr) within this same chunk would not see this
    # pending INSERT via db.get() and would add a duplicate — UNIQUE
    # constraint then aborts the entire chunk on commit. Flushing here keeps
    # the identity map aligned so consecutive events coalesce.
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
