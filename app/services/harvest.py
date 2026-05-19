from app.chain.client import yield_harvester
from app.chain.executor_wallet import send_tx


def run(agent_id: int) -> dict:
    tx_hash = send_tx(yield_harvester().functions.harvest(agent_id))
    return {"tx_hash": tx_hash, "agent_id": agent_id}
