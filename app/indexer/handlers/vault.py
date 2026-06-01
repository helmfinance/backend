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

    # Snapshot NAV + refresh positions so the agent detail page reflects the
    # new portfolio mix immediately after each rebalance.
    _snapshot_nav(db, agent.agent_id, vault_addr, agent.token_address, args["timestamp"])
    _refresh_positions(db, agent.agent_id, vault_addr)


def _refresh_positions(db: Session, agent_id: int, vault_addr: str):
    """Read current vault asset holdings from chain and upsert positions rows.

    Synthetic balances come from the SyntheticAsset ERC-20 path; METH/USDY
    adapters expose balanceOfHolder + valueInUSDC for their adapter path.
    Weight in bps is value / totalAssets.
    """
    from sqlalchemy import delete
    from web3 import Web3
    from app.chain.client import agent_vault, contract_at
    try:
        # Web3 calls reject lowercase addresses (require EIP-55 checksum).
        # vault_addr coming from DB is lowercase, so normalise once.
        vault_cs = Web3.to_checksum_address(vault_addr)
        vault = agent_vault(vault_cs)
        n = int(vault.functions.assetCount().call())
        nav_total = int(vault.functions.totalAssets().call())
        # Wipe existing rows for a clean upsert (small set, <10 assets/agent).
        db.execute(delete(models.Position).where(models.Position.agent_id == agent_id))

        KIND_CLASS = {0: "equity", 1: "crypto", 2: "treasury"}
        for i in range(n):
            asset_addr, kind = vault.functions.assetAt(i).call()
            try:
                if kind == 0:  # SyntheticAsset
                    sa = contract_at("SyntheticAsset", asset_addr)
                    bal = int(sa.functions.balanceOf(vault_cs).call())
                    price = int(sa.functions.priceUSDC().call())
                    value = bal * price // 10**18
                    symbol = sa.functions.symbol().call()
                elif kind == 1:  # METH adapter
                    ad = contract_at("MantleMETHAdapter", asset_addr)
                    bal = int(ad.functions.balanceOfHolder(vault_cs).call())
                    value = int(ad.functions.valueInUSDC(vault_cs).call())
                    price = None
                    symbol = "mETH"
                else:  # USDY adapter
                    ad = contract_at("OndoUSDYAdapter", asset_addr)
                    bal = int(ad.functions.balanceOfHolder(vault_cs).call())
                    value = int(ad.functions.valueInUSDC(vault_cs).call())
                    price = None
                    symbol = "USDY"
            except Exception as pe:
                print(f"[indexer] _refresh_positions asset[{i}] failed: {pe}")
                continue

            weight_bps = (value * 10_000 // nav_total) if nav_total > 0 else 0
            now_ts = int(time.time())
            # Two concurrent indexer cycles (APScheduler + a manual tick, or
            # the post-tx sync in rebalance.py + an in-flight scheduler chunk)
            # both delete+insert this row, racing on UNIQUE (agent_id,
            # asset_address). Use ON CONFLICT DO UPDATE so the second writer's
            # values overwrite the first's — the data is identical anyway, so
            # last-write-wins is safe and removes the integrity error that
            # otherwise aborts the entire chunk.
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert
            stmt = sqlite_insert(models.Position).values(
                agent_id=agent_id,
                asset_address=asset_addr.lower(),
                symbol=symbol,
                asset_class=KIND_CLASS.get(int(kind), "equity"),
                amount=str(bal),
                value_usdc=str(value),
                weight_bps=weight_bps,
                price_usdc=str(price) if price is not None else None,
                price_updated_at=now_ts,
                price_stale=False,
                updated_at=now_ts,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["agent_id", "asset_address"],
                set_={
                    "symbol": stmt.excluded.symbol,
                    "asset_class": stmt.excluded.asset_class,
                    "amount": stmt.excluded.amount,
                    "value_usdc": stmt.excluded.value_usdc,
                    "weight_bps": stmt.excluded.weight_bps,
                    "price_usdc": stmt.excluded.price_usdc,
                    "price_updated_at": stmt.excluded.price_updated_at,
                    "price_stale": stmt.excluded.price_stale,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            db.execute(stmt)
    except Exception as e:
        print(f"[indexer] _refresh_positions agent={agent_id} skipped: {e}")


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
    """ERC-4626 Deposit. NAV snapshot only — Holder rows are owned by the
    AgentToken Transfer handler.

    The Deposit event's ``owner`` is the public-mint entrypoint contract on
    the launch path (it receives the shares, then forwards them to the real
    user via a separate ERC-20 Transfer). Using it as the Holder.address
    poisoned the DB with entrypoint rows that never matched real wallets,
    so DividendDistributor seeding produced 0 claims. Transfer is the
    authoritative signal for share custody and lives in
    ``handlers.agent_token``.
    """
    vault_addr = event["address"].lower()
    agent = _find_agent_by_vault(db, vault_addr)
    if not agent:
        return
    _snapshot_nav(db, agent.agent_id, vault_addr, agent.token_address, int(time.time()))


def handle_withdraw(db: Session, event):
    """ERC-4626 Withdraw. NAV snapshot only — Holder mutation is handled by
    AgentToken Transfer (burn → from=user, to=0x0).
    """
    vault_addr = event["address"].lower()
    agent = _find_agent_by_vault(db, vault_addr)
    if not agent:
        return
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
