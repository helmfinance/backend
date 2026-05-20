"""RedemptionQueue event handlers.

ABI events:
    RedeemRequested(uint256 requestId, uint256 agentId, address holder,
                    uint256 shares, uint8 tier, uint64 unlockAt)
    RedeemClaimed(uint256 requestId, uint256 usdcOut)
    RedeemCancelled(uint256 requestId)

Cancel / Claim look up the row by ``request_id`` — Requested is the only
event that carries the full context.
"""

from __future__ import annotations

import time

from sqlalchemy.orm import Session

from app.db import models

# uint8 tier (on-chain enum) → LockupTier string used in the model
_TIER_BY_INDEX = {0: "instant", 1: "30d", 2: "60d", 3: "90d"}


def handle_redeem_requested(db: Session, event) -> None:
    args = event["args"]
    request_id = int(args["requestId"])
    if db.get(models.RedemptionRequest, request_id):
        return

    holder_raw = args["holder"]
    holder = (holder_raw if isinstance(holder_raw, str) else holder_raw.hex()).lower()
    tier = _TIER_BY_INDEX.get(int(args["tier"]), "instant")

    db.add(models.RedemptionRequest(
        request_id=request_id,
        agent_id=int(args["agentId"]),
        holder_address=holder,
        shares=str(int(args["shares"])),
        tier=tier,
        requested_at=int(time.time()),
        unlock_at=int(args["unlockAt"]),
        estimated_usdc="0",  # populated on RedeemClaimed
        status="Pending",
    ))


def handle_redeem_claimed(db: Session, event) -> None:
    args = event["args"]
    request_id = int(args["requestId"])
    usdc_out = int(args["usdcOut"])

    req = db.get(models.RedemptionRequest, request_id)
    if req is None or req.status == "Claimed":
        return
    req.status = "Claimed"
    req.claimed_at = int(time.time())
    req.estimated_usdc = str(usdc_out)


def handle_redeem_cancelled(db: Session, event) -> None:
    args = event["args"]
    request_id = int(args["requestId"])

    req = db.get(models.RedemptionRequest, request_id)
    if req is None or req.status == "Cancelled":
        return
    req.status = "Cancelled"
