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
        # Mantle Sepolia sequencer occasionally silent-drops the trailing tx of
        # a multi-tx burst (drain+approve+stage+distribute all from the same
        # signer in rapid succession). distribute() is most affected because
        # it's last. Retry up to 3 times with a small pause when the receipt
        # times out — eth_call simulation proves the contract state is fine
        # at this point, so a re-submission almost always succeeds.
        import time as _time
        dist_result = None
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                dist_result = send_tx(
                    dist.functions.distribute(agent_id), gas=1_000_000,
                )
                break
            except TimeoutError as e:
                last_err = e
                _time.sleep(3)
                continue
        if dist_result is None:
            raise last_err or RuntimeError("distribute() retries exhausted")
        dist_tx = dist_result["tx_hash"]

        # Synchronously materialize the Distributed event into DB rows so the
        # FE Portfolio surfaces the new claimable amount without waiting for
        # indexer lag. The indexer will see the same event later and skip via
        # the idempotency checks in materialize_distributed_event.
        try:
            from app.services.dividend_indexing import materialize_distributed_event
            receipt = dist_result["receipt"]
            logs = dist.events.Distributed().process_receipt(receipt)
            if logs:
                ev = logs[0]
                tx_hash_hex = (
                    ev["transactionHash"].hex()
                    if hasattr(ev["transactionHash"], "hex")
                    else ev["transactionHash"]
                )
                materialize_distributed_event(
                    db,
                    agent_id=agent_id,
                    epoch=int(ev["args"]["epoch"]),
                    total_amount=int(ev["args"]["totalAmount"]),
                    holders_share=int(ev["args"]["holdersShare"]),
                    carry_share=int(ev["args"]["carryShare"]),
                    block_number=int(ev["blockNumber"]),
                    tx_hash=tx_hash_hex,
                    log_index=int(ev["logIndex"]),
                    token_address=agent.token_address,
                )
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
