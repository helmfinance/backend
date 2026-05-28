import time

from sqlalchemy.orm import Session

from app.chain.client import founder_vault as founder_vault_contract
from app.chain.client import get_w3, redemption_queue, registry
from app.chain.executor_wallet import send_tx
from app.db import models

_LOCKUP_TO_TIER = {"instant": 0, "30d": 1, "60d": 2, "90d": 3}


def _setup_redemption_tiers(agent_id: int, mandate_dict: dict) -> None:
    """Bridge mandate.allowedLockups → on-chain tierAllowed[agentId].

    HelmRegistry.registerAgent does NOT wire the RedemptionQueue tier
    whitelist, so without this bridge every requestRedeem reverts with
    TierNotAllowedByMandate. Called from the indexer (executor wallet =
    admin) after AgentRegistered, since user wallets can't reach the
    admin-gated setAllowedTiers setter.
    """
    allowed = [False, False, False, False]
    for lk in mandate_dict.get("allowedLockups", []) or []:
        idx = _LOCKUP_TO_TIER.get(lk)
        if idx is not None:
            allowed[idx] = True
    if not any(allowed):
        print(
            f"[indexer] agent {agent_id}: mandate has no recognized "
            f"allowedLockups — skip setAllowedTiers"
        )
        return
    send_tx(
        redemption_queue().functions.setAllowedTiers(agent_id, allowed),
    )
    print(f"[indexer] agent {agent_id}: setAllowedTiers({allowed}) ✓")


def handle_agent_registered(db: Session, event):
    """event.args: agentId, founder, deployment (vault/token/founderVault addrs).

    AgentRegistered does NOT emit mandateHash/mandateURI — those are only in
    the registerAgent calldata. We re-fetch the tx and decode its input to
    pull them, then resolve the body via mandate_blobs.

    Returns silently (no row inserted) when calldata decoding fails — this
    prevents the indexer from looping on the chunk with a UNIQUE constraint
    violation on an empty mandate_hash.
    """
    args = event["args"]
    agent_id = args["agentId"]
    if db.get(models.Agent, agent_id):
        return  # idempotent

    dep = args["deployment"]
    vault_addr = dep["vault"] if "vault" in dep else dep[0]
    token_addr = dep["token"] if "token" in dep else dep[1]
    fv_addr = dep["founderVault"] if "founderVault" in dep else dep[2]

    # Pull mandateHash / mandateURI from the registerAgent calldata.
    tx_hash = event["transactionHash"]
    if hasattr(tx_hash, "hex"):
        tx_hash = tx_hash.hex()

    mandate_hash = ""
    mandate_uri = ""
    try:
        tx = get_w3().eth.get_transaction(tx_hash)
        fn, params = registry().decode_function_input(tx["input"])
        if fn.fn_name == "registerAgent":
            mh = params.get("mandateHash")
            if isinstance(mh, (bytes, bytearray)):
                mandate_hash = "0x" + bytes(mh).hex()
            elif isinstance(mh, str):
                mandate_hash = mh if mh.startswith("0x") else "0x" + mh
            mandate_uri = params.get("mandateURI") or params.get("mandateUri") or ""
    except Exception as e:
        print(f"[indexer] decode calldata failed for tx {tx_hash}: {e}")
        return

    if not mandate_hash:
        print(
            f"[indexer] no mandateHash in calldata for agent {agent_id} — skipping",
        )
        return

    blob = db.get(models.MandateBlob, mandate_hash)
    mandate_dict = blob.mandate_json if blob else {}

    # Bridge before db.add: if the chain call fails, this handler raises and
    # the dispatcher retries on the next indexer cycle (no DB row yet, so the
    # early-return guard above won't short-circuit).
    _setup_redemption_tiers(agent_id, mandate_dict)

    now = int(time.time())
    db.add(models.Agent(
        agent_id=agent_id,
        name=mandate_dict.get("name", f"Agent #{agent_id}"),
        ticker=mandate_dict.get("ticker", "AGT"),
        founder_address=args["founder"].lower(),
        vault_address=vault_addr.lower(),
        token_address=token_addr.lower(),
        founder_vault_address=fv_addr.lower(),
        phase="Incubation",
        incubation_start=now,
        public_launch_at=None,
        mandate=mandate_dict,
        mandate_uri=mandate_uri,
        mandate_hash=mandate_hash,
        reputation=10000,
        created_at=now,
    ))

    # Seed a FounderVault row at the same time so the /agents/{id} and
    # /portfolio responses don't return null for the founder lifecycle UI.
    # Pull initial state from chain (FounderVault view fns) — subsequent
    # updates flow in via the dedicated founder.* event handlers.
    try:
        fv = founder_vault_contract(fv_addr)
        shares_held = int(fv.functions.totalSharesHeld().call())
        lockup_ends_at = int(fv.functions.lockupEndsAt().call())
        carry_balance = int(fv.functions.carryBalance().call())
        is_sub_active = bool(fv.functions.isSubordinationActive().call())
        db.add(models.FounderVault(
            agent_id=agent_id,
            address=fv_addr.lower(),
            shares_held=str(shares_held),
            lockup_ends_at=lockup_ends_at,
            cumulative_withdrawn_bps=0,
            is_subordination_active=is_sub_active,
            carry_balance_usdc=str(carry_balance),
        ))
    except Exception as e:
        # Don't block agent indexing on FounderVault read failure — founder
        # event handlers will fill it in later.
        print(f"[indexer] agent {agent_id}: FounderVault seed failed: {e}")


def handle_phase_advanced(db: Session, event):
    args = event["args"]
    agent_id = args["agentId"]
    new_phase = _decode_phase(args["to"])
    a = db.get(models.Agent, agent_id)
    if a:
        a.phase = new_phase
        if new_phase == "PublicLaunch" and a.public_launch_at is None:
            a.public_launch_at = int(time.time())


def handle_agent_slashed(db: Session, event):
    # Reputation is updated by ReputationSlashed; phase by PhaseAdvanced.
    return


def handle_agent_wind_down(db: Session, event):
    args = event["args"]
    agent_id = args["agentId"]
    a = db.get(models.Agent, agent_id)
    if a:
        a.phase = "WindDown"
    # Detailed WindDownState row is created by the vault's WindDownTriggered event handler.


def handle_agent_settled(db: Session, event):
    a = db.get(models.Agent, event["args"]["agentId"])
    if a:
        a.phase = "Settled"


def _decode_phase(enum_value: int) -> str:
    """Registry Phase enum: 0=Incubation, 1=PublicLaunch, 2=WindDown, 3=Slashed, 4=Settled."""
    return ["Incubation", "PublicLaunch", "WindDown", "Slashed", "Settled"][enum_value]
