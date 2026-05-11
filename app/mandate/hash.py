"""Canonical JSON serialization + keccak256 hash for mandate blobs."""

from __future__ import annotations

import json

from web3 import Web3


def canonical_json(data: dict) -> str:
    """Sorted keys, no whitespace. Stable across runs."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_mandate_hash(mandate_dict: dict) -> str:
    """0x-prefixed lowercase keccak256 of canonical JSON. 66 chars total."""
    digest = Web3.keccak(text=canonical_json(mandate_dict)).hex()
    return "0x" + digest.removeprefix("0x").lower()
