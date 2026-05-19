import time

from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import IndexerState


def get_last_synced(db: Session) -> int:
    row = db.get(IndexerState, settings.chain_id)
    return row.last_synced_block if row else 0


def set_last_synced(db: Session, block: int):
    row = db.get(IndexerState, settings.chain_id)
    now = int(time.time())
    if row:
        row.last_synced_block = block
        row.updated_at = now
    else:
        db.add(IndexerState(
            chain_id=settings.chain_id,
            last_synced_block=block,
            updated_at=now,
        ))
