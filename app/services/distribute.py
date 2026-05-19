from app.chain.client import agent_vault, dividend_distributor
from app.chain.executor_wallet import send_tx
from app.db import models
from app.db.session import SessionLocal


def run(agent_id: int) -> dict:
    """1) stageYield(agentId, amount) 2) distribute(agentId).

    Amount is read from the vault's yieldPool view (exact fn name varies; fall back
    to yieldPoolBalance if needed).
    """
    with SessionLocal() as db:
        agent = db.get(models.Agent, agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")
        vault = agent_vault(agent.vault_address)
        try:
            amount = vault.functions.yieldPool().call()
        except Exception:
            amount = vault.functions.yieldPoolBalance().call()

        if amount == 0:
            return {"tx_hash": None, "amount": 0, "note": "no yield to distribute"}

        d = dividend_distributor()
        stage_tx = send_tx(d.functions.stageYield(agent_id, amount))["tx_hash"]
        dist_tx = send_tx(d.functions.distribute(agent_id))["tx_hash"]
        return {
            "stage_tx_hash": stage_tx,
            "distribute_tx_hash": dist_tx,
            "amount": amount,
        }
