import time

from sqlalchemy.orm import Session

from app.chain.client import agent_token, agent_vault
from app.db import models


def _tx_hash(event) -> str:
    raw = event["transactionHash"]
    return raw.hex() if hasattr(raw, "hex") else raw


def _find_agent_by_vault(db: Session, vault_addr: str):
    return (
        db.query(models.Agent)
        .filter(models.Agent.vault_address == vault_addr)
        .first()
    )


def handle_rebalanced(db: Session, event):
    """event.args: strategyHash, navAfter, timestamp."""
    args = event["args"]
    tx_hash = _tx_hash(event)
    log_idx = event["logIndex"]
    decision_id = f"{tx_hash}:{log_idx}"

    if db.get(models.Decision, decision_id):
        return

    vault_addr = event["address"].lower()
    agent = _find_agent_by_vault(db, vault_addr)
    if not agent:
        return

    strategy_hash = args["strategyHash"]
    if hasattr(strategy_hash, "hex"):
        strategy_hash = strategy_hash.hex()

    db.add(models.Decision(
        id=decision_id,
        agent_id=agent.agent_id,
        type="Rebalance",
        timestamp=args["timestamp"],
        tx_hash=tx_hash,
        block_number=event["blockNumber"],
        summary=f"Rebalanced (strategy: {str(strategy_hash)[:10]}...)",
        nav_after=str(args["navAfter"]),
    ))


def handle_yield_deposited(db: Session, event):
    args = event["args"]
    tx_hash = _tx_hash(event)
    decision_id = f"{tx_hash}:{event['logIndex']}"
    if db.get(models.Decision, decision_id):
        return

    vault_addr = event["address"].lower()
    agent = _find_agent_by_vault(db, vault_addr)
    if not agent:
        return

    db.add(models.Decision(
        id=decision_id,
        agent_id=agent.agent_id,
        type="Harvest",
        timestamp=int(time.time()),
        tx_hash=tx_hash,
        block_number=event["blockNumber"],
        summary=f"Harvested yield: {args['amount']}",
        harvested_usdc=str(args["amount"]),
    ))


def handle_deposit(db: Session, event):
    """ERC-4626 Deposit(sender, owner, assets, shares). Update Holder + NAV snapshot."""
    args = event["args"]
    vault_addr = event["address"].lower()
    agent = _find_agent_by_vault(db, vault_addr)
    if not agent:
        return

    owner = args["owner"].lower()
    shares = args["shares"]

    holder = db.get(models.Holder, (agent.agent_id, owner))
    now = int(time.time())
    if holder:
        holder.balance = str(int(holder.balance) + shares)
    else:
        db.add(models.Holder(
            agent_id=agent.agent_id,
            address=owner,
            balance=str(shares),
            weight_bps=0,
            first_held_at=now,
            cumulative_dividends_claimed_usdc="0",
        ))

    _snapshot_nav(db, agent.agent_id, vault_addr, agent.token_address, now)


def handle_withdraw(db: Session, event):
    """ERC-4626 Withdraw(sender, receiver, owner, assets, shares)."""
    args = event["args"]
    vault_addr = event["address"].lower()
    agent = _find_agent_by_vault(db, vault_addr)
    if not agent:
        return

    owner = args["owner"].lower()
    shares = args["shares"]
    holder = db.get(models.Holder, (agent.agent_id, owner))
    if holder:
        new_bal = max(0, int(holder.balance) - shares)
        holder.balance = str(new_bal)

    _snapshot_nav(db, agent.agent_id, vault_addr, agent.token_address, int(time.time()))


def _snapshot_nav(db: Session, agent_id: int, vault_addr: str, token_addr: str, ts: int):
    """Read vault.totalAssets() + token.totalSupply(), append nav_history row.

    Uses ``NavPoint`` (the actual model class — the schema table is ``nav_history``).
    """
    try:
        nav_usdc = agent_vault(vault_addr).functions.totalAssets().call()
        total_shares = agent_token(token_addr).functions.totalSupply().call()
        nav_per_share = (
            (nav_usdc * 10**18 // total_shares) if total_shares > 0 else 1_000_000
        )
        db.add(models.NavPoint(
            agent_id=agent_id,
            timestamp=ts,
            nav_usdc=str(nav_usdc),
            nav_per_share_usdc=str(nav_per_share),
            total_shares=str(total_shares),
        ))
    except Exception:
        pass  # NAV snapshot best-effort; other sync proceeds
