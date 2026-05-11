"""mandate_blobs repository — write path for /mandate/parse."""

from __future__ import annotations

import time

from sqlalchemy.orm import Session

from app.db.models import MandateBlob


def upsert_mandate_blob(
    db: Session,
    *,
    mandate_hash: str,
    mandate_dict: dict,
    raw_text: str,
    ipfs_uri: str,
    pinned: bool,
) -> MandateBlob:
    """Idempotent: if hash exists, return existing row (do not overwrite)."""
    existing = db.get(MandateBlob, mandate_hash)
    if existing:
        return existing
    now = int(time.time())
    blob = MandateBlob(
        mandate_hash=mandate_hash,
        mandate_json=mandate_dict,
        raw_text=raw_text,
        ipfs_uri=ipfs_uri if pinned else None,
        pinned_at=now if pinned else None,
        created_at=now,
    )
    db.add(blob)
    db.commit()
    db.refresh(blob)
    return blob
