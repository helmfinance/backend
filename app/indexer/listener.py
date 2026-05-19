from app.chain.client import get_w3
from app.config import settings
from app.db.session import SessionLocal
from app.indexer.dispatcher import process_range
from app.indexer.state import get_last_synced, set_last_synced

CONFIRMATIONS = 6
CHUNK_SIZE = settings.indexer_chunk_blocks


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
            # First run — anchor near head; full history backfill is a separate task
            last = safe - 100
        start = last + 1
        if start > safe:
            return  # nothing to do

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
        print(f"[indexer] synced to block {safe}")
