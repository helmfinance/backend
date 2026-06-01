import time

from sqlalchemy.orm import Session

from app.chain.client import founder_vault as founder_vault_contract
from app.chain.client import get_w3, redemption_queue, registry, yield_harvester
from app.chain.executor_wallet import send_tx
from app.config import settings
from app.db import models
from web3 import Web3

_LOCKUP_TO_TIER = {"instant": 0, "30d": 1, "60d": 2, "90d": 3}

# Mandate symbol → yield adapter env key. Synthetic equities (sNVDA, sSPY, ...)
# don't generate yield so they're not in this map. Treasury (USDY) and crypto
# staking (mETH) do.
_YIELD_SOURCE_BY_SYMBOL = {
    "USDY": "ondo_usdy_adapter",
    "mETH": "mantle_meth_adapter",
    "METH": "mantle_meth_adapter",
}


def _setup_yield_sources(agent_id: int, mandate_dict: dict) -> None:
    """Register yield-bearing adapter sources with YieldHarvester for an agent.

    HelmRegistry.registerAgent does NOT wire YieldHarvester sources, so
    without this bridge `harvest(agentId)` iterates an empty `_sources[]`
    array and returns silently (no events, no yieldPool deposit). Mirrors
    the tier-whitelist bridge above; runs via the executor wallet (which is
    also the harvester's `executor`).
    """
    # Mandate authoring quirk: the LLM populates ``weightConstraints`` with
    # the per-asset min/max ranges but often leaves ``targetUniverse`` null,
    # since they encode the same information. Derive the universe from
    # weightConstraints[].asset when targetUniverse is empty, otherwise
    # yield-bearing adapters never get wired for valid mandates.
    universe = mandate_dict.get("targetUniverse") or []
    if not universe:
        universe = [
            c.get("asset")
            for c in mandate_dict.get("weightConstraints", []) or []
            if c.get("asset")
        ]
    sources: list[str] = []
    for sym in universe:
        env_key = _YIELD_SOURCE_BY_SYMBOL.get(sym)
        if not env_key:
            continue
        addr = getattr(settings, env_key, None)
        if not addr:
            print(f"[indexer] agent {agent_id}: {env_key} unset — skip {sym}")
            continue
        sources.append(Web3.to_checksum_address(addr))
    if not sources:
        print(
            f"[indexer] agent {agent_id}: no yield-bearing assets in mandate "
            f"— skip registerSource"
        )
        return
    harvester = yield_harvester()
    for src in sources:
        send_tx(harvester.functions.registerSource(agent_id, src, b""))
        print(f"[indexer] agent {agent_id}: registerSource({src}) ✓")


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
        # Fall through — register the Agent with a placeholder mandate_hash
        # so the demo flow (mint / rebalance / distribute / claim) still
        # works without a real mandate body. mandate-dependent features
        # (LLM narrator, tier whitelist) will no-op until a body is
        # supplied via /agents/{id}/mandate/import.

    # Fallback: synthesize a unique mandate_hash from the tx hash so the
    # Agent row's UNIQUE constraint on mandate_hash is satisfied and the
    # row gets created even if calldata decoding failed.
    if not mandate_hash:
        mandate_hash = "0x" + ("00" * 16) + tx_hash.replace("0x", "")[:32]
        print(
            f"[indexer] agent {agent_id}: no mandateHash in calldata — "
            f"using synthetic {mandate_hash}",
        )

    blob = db.get(models.MandateBlob, mandate_hash)
    mandate_dict = blob.mandate_json if blob else {}

    # Bridge before db.add: if any chain call fails, this handler raises and
    # the dispatcher retries on the next indexer cycle (no DB row yet, so the
    # early-return guard above won't short-circuit).
    _setup_redemption_tiers(agent_id, mandate_dict)
    _setup_yield_sources(agent_id, mandate_dict)

    now = int(time.time())
    # APScheduler tick + an HTTP-triggered /admin/debug/indexer-tick can race
    # and both INSERT the same agent_id. SQLite has no ON CONFLICT in the ORM
    # path, so we use the dialect insert helper to make the write idempotent
    # at the SQL layer rather than at the in-session db.get() layer (which
    # the second concurrent session bypasses because the first hasn't
    # committed yet).
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    stmt = sqlite_insert(models.Agent).values(
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
    ).on_conflict_do_nothing(index_elements=["agent_id"])
    db.execute(stmt)

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
        fv_stmt = sqlite_insert(models.FounderVault).values(
            agent_id=agent_id,
            address=fv_addr.lower(),
            shares_held=str(shares_held),
            lockup_ends_at=lockup_ends_at,
            cumulative_withdrawn_bps=0,
            is_subordination_active=is_sub_active,
            carry_balance_usdc=str(carry_balance),
        ).on_conflict_do_nothing(index_elements=["agent_id"])
        db.execute(fv_stmt)
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
    # HelmRegistry.slash() flips phase to Slashed but does NOT emit
    # PhaseAdvanced — only AgentSlashed. So this handler is responsible
    # for the phase write. Reputation is updated by ReputationSlashed.
    args = event["args"]
    a = db.get(models.Agent, args["agentId"])
    if a:
        a.phase = "Slashed"


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
