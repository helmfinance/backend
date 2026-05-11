"""IPFS pinning for mandate blobs.

Priority: Pinata → web3.storage (deprecated stub) → local disk fallback.
Local stub never fails, so the route never breaks on pinning errors.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from app.config import settings

LOCAL_STORE = Path("data/mandates")


def pin_mandate(mandate_dict: dict, mandate_hash: str) -> tuple[str, bool]:
    """Returns (ipfs_uri, is_real_pin).

    Tries Pinata → web3.storage → local stub. Local stub never fails.
    Always writes a local copy first as a fallback safety.
    """
    LOCAL_STORE.mkdir(parents=True, exist_ok=True)
    local_path = LOCAL_STORE / f"{mandate_hash}.json"
    local_path.write_text(json.dumps(mandate_dict, indent=2))

    if settings.pinata_jwt:
        try:
            return _pin_pinata(mandate_dict), True
        except Exception:
            pass

    if settings.web3_storage_token:
        try:
            return _pin_web3storage(mandate_dict), True
        except Exception:
            pass

    return f"ipfs://local-{mandate_hash[2:]}", False


def _pin_pinata(mandate_dict: dict) -> str:
    """https://docs.pinata.cloud/api-reference/endpoint/pin-json"""
    with httpx.Client(timeout=15.0) as client:
        r = client.post(
            "https://api.pinata.cloud/pinning/pinJSONToIPFS",
            headers={"Authorization": f"Bearer {settings.pinata_jwt}"},
            json={"pinataContent": mandate_dict},
        )
        r.raise_for_status()
        cid = r.json()["IpfsHash"]
        return f"ipfs://{cid}"


def _pin_web3storage(mandate_dict: dict) -> str:
    """web3.storage migrated to w3up in 2024 — legacy HTTP endpoint deprecated.

    Raises NotImplementedError so caller falls through to the local stub.
    """
    raise NotImplementedError(
        "web3.storage HTTP API was deprecated; use Pinata or local stub."
    )
