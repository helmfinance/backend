import time

from app.chain.client import agent_nft, agent_vault
from app.chain.executor_wallet import send_tx
from app.db import models
from app.db.session import SessionLocal
from app.mandate.ipfs import pin_mandate
from app.repos import agents as agent_repo
from app.repos import analytics


def update(agent_id: int) -> dict:
    """Build NFT metadata from agent state, pin to IPFS, call setTokenURI."""
    with SessionLocal() as db:
        agent = db.get(models.Agent, agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")

        # Live chain read: AUM. Falls back to 0 on revert (inactive vaults).
        try:
            aum = agent_vault(agent.vault_address).functions.totalAssets().call()
        except Exception:
            aum = 0

        perf = analytics.compute_performance(db, agent_id)

        rebalance_count = agent_repo.count_decisions_by_type(db, agent_id, "Rebalance")
        holder_count = agent_repo.count_active_holders(db, agent_id)
        total_harvested = agent_repo.sum_harvested_usdc(db, agent_id)
        # Each mandate breach is assumed to slash 1000 bps of reputation.
        breach_count = max(0, (10000 - (agent.reputation or 10000)) // 1000)

        fv = (
            db.query(models.FounderVault)
            .filter_by(agent_id=agent_id)
            .first()
        )
        total_carry = int(fv.carry_balance_usdc) if fv else 0

        latest_note = (
            db.query(models.NarratorNote)
            .filter_by(agent_id=agent_id)
            .order_by(models.NarratorNote.week_start.desc())
            .first()
        )

        mandate = agent.mandate or {}
        lockups = (
            mandate.get("allowedLockups")
            or mandate.get("allowed_lockups")
            or []
        )
        redemption_queue_days = str(lockups[0]) if lockups else "instant"

        attributes = [
            {"trait_type": "Ticker", "value": agent.ticker},
            {"trait_type": "Phase", "value": agent.phase},
            {"trait_type": "Reputation", "value": agent.reputation, "max_value": 10000},
            {"trait_type": "Total Return", "value": perf["total_return"], "display_type": "number"},
            {"trait_type": "Sharpe Ratio", "value": perf["sharpe_ratio"], "display_type": "number"},
            {"trait_type": "Max Drawdown", "value": perf["max_drawdown"], "display_type": "number"},
            {"trait_type": "Rebalances", "value": rebalance_count, "display_type": "number"},
            {"trait_type": "Mandate Breaches", "value": breach_count, "display_type": "number"},
            {"trait_type": "Current AUM (USDC)", "value": aum / 1e6, "display_type": "number"},
            {"trait_type": "Holders", "value": holder_count, "display_type": "number"},
            {"trait_type": "Yield Distributed (USDC)", "value": total_harvested / 1e6, "display_type": "number"},
            {"trait_type": "Dev Carry Paid (USDC)", "value": total_carry / 1e6, "display_type": "number"},
            {"trait_type": "Redemption Queue", "value": redemption_queue_days},
        ]

        now_s = int(time.time())
        metadata = {
            "name": f"Helm Agent #{agent_id} — {agent.name}",
            "description": mandate.get("description") or "AI-managed ETF agent on Helm.",
            "image": "ipfs://placeholder-helm-logo",
            "external_url": f"https://helm.finance/agents/{agent_id}",
            "attributes": attributes,
            "helm": {
                "agentId": agent_id,
                "mandateHash": agent.mandate_hash,
                "mandateUri": agent.mandate_uri,
                "founderAddress": agent.founder_address,
                "vaultAddress": agent.vault_address,
                "tokenAddress": agent.token_address,
                "founderVaultAddress": agent.founder_vault_address,
                "performance": perf,
                "latestNarratorNote": latest_note.body_markdown if latest_note else None,
                "updatedAt": now_s,
            },
        }

        # Unique pin key per call so each refresh writes a separate local file
        # (IPFS pin_mandate uses this as the local-stub filename and the CID
        # fallback).
        pin_key = f"nft-meta-{agent_id}-{now_s}"
        ipfs_uri, _pinned = pin_mandate(metadata, pin_key)

        result = send_tx(
            agent_nft().functions.setTokenURI(agent_id, ipfs_uri),
        )
        tx_hash = result["tx_hash"] if isinstance(result, dict) else result
        return {
            "tx_hash": tx_hash,
            "uri": ipfs_uri,
            "attribute_count": len(attributes),
        }
