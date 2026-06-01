"""Distribute yield from a vault to its holders + founder.

On-chain flow:
  1. vault.withdrawYieldTo(executor, amount)  — pulls yieldPool USDC out of
     the vault to the BE executor wallet.
  2. usdc.approve(distributor, amount)         — lets DividendDistributor pull.
  3. distributor.stageYield(agentId, amount)   — DividendDistributor pulls the
     USDC; tracks staged amount per agent.
  4. distributor.distribute(agentId)           — splits 90/10 → epoch row +
     FounderVault.receiveCarry(carry).

`distributor.harvester` (immutable) is the only address allowed to call
stageYield/distribute. Phase 4 deploy wired this to the executor wallet so
the BE can drive the flow directly without an upgrade path.
"""

from web3 import Web3

from app.chain.client import agent_vault, dividend_distributor, usdc as usdc_contract
from app.chain.executor_wallet import address as executor_address
from app.chain.executor_wallet import send_tx
from app.config import settings
from app.db import models
from app.db.session import SessionLocal


def run(agent_id: int) -> dict:
    with SessionLocal() as db:
        agent = db.get(models.Agent, agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")

        vault = agent_vault(agent.vault_address)
        amount = int(vault.functions.yieldPool().call())
        if amount == 0:
            return {"tx_hash": None, "amount": 0, "note": "no yield to distribute"}

        exec_addr = Web3.to_checksum_address(executor_address())
        dist = dividend_distributor()
        dist_addr = Web3.to_checksum_address(settings.dividend_distributor)
        usdc_c = usdc_contract()

        # 1. Drain vault.yieldPool to executor.
        drain_tx = send_tx(
            vault.functions.withdrawYieldTo(exec_addr, amount),
        )["tx_hash"]

        # 2. Approve the distributor to pull the same amount.
        send_tx(usdc_c.functions.approve(dist_addr, amount))

        # 3+4. Stage + distribute.
        stage_tx = send_tx(dist.functions.stageYield(agent_id, amount))["tx_hash"]
        dist_result = send_tx(dist.functions.distribute(agent_id))
        dist_tx = dist_result["tx_hash"]

        # Synchronously decode the Distributed event from the receipt and write
        # DividendEpoch + DividendClaim + Decision rows. The indexer-fed
        # handle_distributed sometimes loses these to silent web3 errors during
        # totalSupply snapshot reads; running it here ensures the FE Portfolio
        # Dividends tab shows the claimable amount on the very next request.
        try:
            import time as _t
            from sqlalchemy import select
            receipt = dist_result["receipt"]
            logs = dist.events.Distributed().process_receipt(receipt)
            if logs:
                ev = logs[0]
                epoch = int(ev["args"]["epoch"])
                total_amount = int(ev["args"]["totalAmount"])
                holders_share = int(ev["args"]["holdersShare"])
                carry_share = int(ev["args"]["carryShare"])
                block_n = int(ev["blockNumber"])
                tx_hash = ev["transactionHash"].hex() if hasattr(ev["transactionHash"], "hex") else ev["transactionHash"]
                log_idx = int(ev["logIndex"])
                now_ts = int(_t.time())

                # Total supply snapshot (chain read at the dist block).
                total_shares_snapshot = 0
                try:
                    from app.chain.client import agent_token
                    if agent.token_address:
                        total_shares_snapshot = int(
                            agent_token(agent.token_address)
                            .functions.totalSupply()
                            .call(block_identifier=block_n)
                        )
                except Exception:
                    pass

                if not db.get(models.DividendEpoch, (agent_id, epoch)):
                    db.add(models.DividendEpoch(
                        agent_id=agent_id,
                        epoch=epoch,
                        total_amount_usdc=str(total_amount),
                        holders_share_usdc=str(holders_share),
                        carry_share_usdc=str(carry_share),
                        distributed_at=now_ts,
                        total_shares_at_snapshot=str(total_shares_snapshot),
                    ))

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
                        key = (agent_id, epoch, h.holder_address)
                        if db.get(models.DividendClaim, key):
                            continue
                        db.add(models.DividendClaim(
                            agent_id=agent_id, epoch=epoch,
                            holder_address=h.holder_address,
                            amount_usdc=str(pending),
                            claimed=False, claimed_at=None,
                        ))

                decision_id = f"{tx_hash}:{log_idx}"
                if not db.get(models.Decision, decision_id):
                    db.add(models.Decision(
                        id=decision_id, agent_id=agent_id, type="Distribute",
                        timestamp=now_ts, tx_hash=tx_hash, block_number=block_n,
                        summary=f"Distributed epoch {epoch}: {total_amount} USDC "
                                f"({holders_share} holders / {carry_share} carry)",
                        distributed_epoch=epoch,
                        distributed_holders_usdc=str(holders_share),
                        distributed_carry_usdc=str(carry_share),
                    ))
                db.commit()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "[distribute] post-tx event index failed (non-fatal): %s", e,
            )

        return {
            "drain_tx_hash": drain_tx,
            "stage_tx_hash": stage_tx,
            "distribute_tx_hash": dist_tx,
            "amount": amount,
        }
