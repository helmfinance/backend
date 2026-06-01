import time

from app.chain.client import get_w3
from app.config import settings
from app.db.session import SessionLocal
from app.indexer.dispatcher import process_range
from app.indexer.state import get_last_synced, set_last_synced

CONFIRMATIONS = 6
CHUNK_SIZE = settings.indexer_chunk_blocks

# Phase 5 contracts were deployed around block 39,260,530 on Mantle Sepolia.
# When the BE DB is wiped (Railway redeploy without persistent volume), the
# indexer must replay history from the deploy block — otherwise every existing
# agent on chain becomes invisible to the BE forever.
BOOTSTRAP_BLOCK = 39_260_530


def run_one_cycle():
    """Process from last_synced+1 to (current - confirmations)."""
    w3 = get_w3()
    if not w3.is_connected():
        print("[indexer] RPC disconnected")
        return

    head = w3.eth.block_number
    safe = head - CONFIRMATIONS

    with SessionLocal() as db:
        last = get_last_synced(db)
        if last == 0:
            # Bootstrap from contract-deploy block so a DB wipe doesn't orphan
            # every previously-registered agent. ~115k blocks to catch up at
            # first deploy; CHUNK_SIZE controls the per-cycle slice.
            last = BOOTSTRAP_BLOCK - 1
        start = last + 1
        if start > safe:
            return  # nothing to do

        # Active-vault count drives per-cycle RPC cost; log it so a slowdown
        # tied to growing agent set is visible.
        from app.db.models import Agent
        active_vaults = (
            db.query(Agent)
            .filter(Agent.agent_id < 9000)
            .filter(Agent.phase != "Settled")
            .count()
        )
        cycle_start = time.monotonic()
        print(
            f"[indexer] cycle: blocks {start}-{safe} "
            f"({safe - start + 1} blocks), active vaults: {active_vaults}"
        )

        cur = start
        while cur <= safe:
            chunk_end = min(cur + CHUNK_SIZE - 1, safe)
            try:
                process_range(db, cur, chunk_end)
                set_last_synced(db, chunk_end)
                db.commit()
            except Exception as e:
                db.rollback()
                print(f"[indexer] chunk {cur}-{chunk_end} failed: {e}")
                return  # next cycle retries the same chunk
            cur = chunk_end + 1
        elapsed = time.monotonic() - cycle_start
        print(f"[indexer] synced to block {safe} in {elapsed:.1f}s")
