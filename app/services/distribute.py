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
        dist_tx = send_tx(dist.functions.distribute(agent_id))["tx_hash"]

        return {
            "drain_tx_hash": drain_tx,
            "stage_tx_hash": stage_tx,
            "distribute_tx_hash": dist_tx,
            "amount": amount,
        }
