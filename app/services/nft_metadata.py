import time

from app.chain.client import agent_nft
from app.chain.executor_wallet import send_tx
from app.db import models
from app.db.session import SessionLocal
from app.mandate.ipfs import pin_mandate


def update(agent_id: int) -> dict:
    """Build NFT metadata from agent state, pin to IPFS, call setTokenURI."""
    with SessionLocal() as db:
        agent = db.get(models.Agent, agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")
        latest_note = (
            db.query(models.NarratorNote)
            .filter_by(agent_id=agent_id)
            .order_by(models.NarratorNote.week_start.desc())
            .first()
        )

        metadata = {
            "name": f"Helm Agent #{agent_id} — {agent.name}",
            "ticker": agent.ticker,
            "description": agent.mandate.get("description", ""),
            "reputation": agent.reputation,
            "phase": agent.phase,
            "mandate_uri": agent.mandate_uri,
            "latest_note": latest_note.body_markdown if latest_note else None,
            "updated_at": int(time.time()),
        }
        # Reuse mandate.ipfs.pin_mandate(dict, hash). Hash is a placeholder
        # because the on-chain mandate hash is unrelated to the NFT metadata.
        fake_hash = f"0x{'0' * 64}"
        ipfs_uri, _pinned = pin_mandate(metadata, fake_hash)
        tx_hash = send_tx(
            agent_nft().functions.setTokenURI(agent_id, ipfs_uri),
        )["tx_hash"]
        return {"tx_hash": tx_hash, "uri": ipfs_uri}
