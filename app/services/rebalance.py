from web3 import Web3

from app.chain.client import agent_vault, registry
from app.chain.executor_wallet import send_tx
from app.db import models
from app.db.session import SessionLocal
from app.services import decision_engine


def execute(agent_id: int) -> dict:
    """Returns {tx_hash, target_weights} or raises."""
    with SessionLocal() as db:
        agent = db.get(models.Agent, agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")
        if agent.phase not in ("Incubation", "PublicLaunch"):
            raise ValueError(f"Agent phase {agent.phase} not rebalanceable")

        targets = decision_engine.compute_target_weights(agent)
        if not targets:
            raise ValueError("No mandate weight constraints")

        # Exact ABI: executeRebalance(tuple[]: (address, uint16)[], bytes proof).
        # `proof` is reserved for future ZK / signature attestation — empty for now.
        proof = b""

        try:
            tx_hash = send_tx(
                agent_vault(agent.vault_address).functions.executeRebalance(
                    [(_symbol_to_address(s), w) for s, w in targets],
                    proof,
                )
            )
        except Exception:
            # Mandate breach possible — notify registry (reputation slash).
            try:
                send_tx(registry().functions.notifyMandateBreach(
                    agent_id, "executeRebalance reverted",
                ))
            except Exception:
                pass
            raise

        return {"tx_hash": tx_hash, "target_weights": targets}


def _symbol_to_address(symbol: str) -> str:
    from app.config import settings
    mapping = {
        "sNVDA": settings.snvda, "sSPY": settings.sspy, "sAAPL": settings.saapl,
        "sTSLA": settings.stsla, "sMSFT": settings.smsft,
        "mETH": settings.mantle_meth_adapter,
        "USDY": settings.ondo_usdy_adapter,
    }
    addr = mapping.get(symbol)
    if not addr:
        raise ValueError(f"Unknown asset: {symbol}")
    return Web3.to_checksum_address(addr)
