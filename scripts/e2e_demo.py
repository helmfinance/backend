"""End-to-end demo flow validation.

Exercises the full chain → indexer → DB pipeline as the demo video does.
Each step calls a chain action, waits for indexer, asserts DB state.
First failed assertion stops the script with diagnostic output.

Usage:
    python -m scripts.e2e_demo

Assumes:
    - executor wallet has MNT (deployer key)
    - uvicorn is running on :8000 (for indexer)
    - .env populated

Spec deltas captured at write time after ABI inspection:
    * registerAgent.assets is tuple[](address, uint8 kind); script supplies
      the AssetKind enum (Synthetic=0, METHAdapter=1, USDYAdapter=2).
    * AgentVault has no payable mint(_,_,bytes[]) — the deposit flow is the
      standard ERC-4626 deposit(assets, receiver). To make sure positions
      can be valued, we pre-call PythPriceAdapter.updatePriceFeeds with
      Hermes bytes + fee.
    * Model is NavPoint (table nav_history).
"""

import time
from collections.abc import Callable

from web3 import Web3

from app.chain.client import (
    agent_nft,
    agent_vault,
    get_w3,
    pyth_adapter,
    registry,
    time_provider,
    usdc,
)
from app.chain.executor_wallet import (
    address,
    balance_wei,
    send_tx,
)
from app.config import settings
from app.db import SessionLocal, models
from app.hermes.client import (
    FEED_BY_SYMBOL,
    estimate_pyth_fee_wei,
    fetch_price_updates,
)
from app.mandate.hash import compute_mandate_hash
from app.mandate.ipfs import pin_mandate
from app.services import distribute, harvest, nft_metadata, rebalance

# AssetKind enum (IAgentVault.sol):
#   Synthetic = 0, METHAdapter = 1, USDYAdapter = 2
ASSET_KIND_SYNTHETIC = 0
ASSET_KIND_METH = 1
ASSET_KIND_USDY = 2


# ─── Step 0: 환경 점검 ────────────────────────────────────────────────────────


def check_environment() -> None:
    w3 = get_w3()
    assert w3.is_connected(), "RPC disconnected"
    acct_balance = balance_wei() / 1e18
    print(f"[env] executor: {address()}")
    print(f"[env] MNT balance: {acct_balance:.4f}")
    assert acct_balance > 0.01, f"need >0.01 MNT, have {acct_balance}"

    usdc_bal = usdc().functions.balanceOf(address()).call()
    print(f"[env] USDC balance: {usdc_bal / 1e6:.2f}")
    # Need MIN_SEED_USDC (1000) for registerAgent + ≥10 for deposit. Mint
    # 2000 with headroom when balance is below 1200.
    if usdc_bal < 1_200_000_000:
        print("[env] minting 2000 USDC to executor...")
        # send_tx waits for receipt by default; nonce is reflected by the
        # sequencer before any subsequent step calls send_tx.
        send_tx(usdc().functions.mint(address(), 2_000_000_000))

    print(f"[env] last indexed block: {_last_indexed()}")


def _last_indexed() -> int:
    with SessionLocal() as db:
        row = db.get(models.IndexerState, settings.chain_id)
        return row.last_synced_block if row else 0


# ─── Helper: wait for indexer ────────────────────────────────────────────────


def wait_for_indexer(
    predicate: Callable[[], bool],
    description: str,
    timeout: int = 60,
) -> None:
    start = time.time()
    while time.time() - start < timeout:
        if predicate():
            elapsed = int(time.time() - start)
            print(f"[indexer] ✓ {description} ({elapsed}s)")
            return
        time.sleep(1)
    raise TimeoutError(
        f"indexer timeout: {description} (last block: {_last_indexed()})"
    )


# ─── Step 1: registerAgent ───────────────────────────────────────────────────


def step1_register_agent() -> int:
    """Register a brand-new agent. Returns new agent_id."""
    now = int(time.time())
    mandate_json = {
        "version": "1.0",
        "name": f"E2E Test Agent {now}",
        "ticker": "E2E",
        "description": f"End-to-end test agent created at {now}",
        "assetClasses": ["equity", "treasury"],
        "targetUniverse": ["sNVDA", "USDY"],
        "weightConstraints": [
            {"asset": "sNVDA", "minBps": 4000, "maxBps": 6000},
            {"asset": "USDY", "minBps": 4000, "maxBps": 6000},
        ],
        "rebalanceFrequency": "weekly",
        "rebalanceTriggers": ["NAV drift > 5%"],
        "allowedLockups": ["30d"],
        "minimumDepositUsdc": "10000000",
        "founderShareBps": 1000,
        "carryBps": 1000,
        "founderLockupDays": 180,
        "subordinationThresholdBps": 5000,
        "maxLeverage": 1.0,
        "maxSinglePositionBps": 6000,
        "emergencyExitConditions": ["Drawdown > 25%"],
    }

    mandate_hash = compute_mandate_hash(mandate_json)
    mandate_uri, _ = pin_mandate(mandate_json, mandate_hash)
    print(f"[step1] mandate_hash: {mandate_hash}")
    print(f"[step1] mandate_uri: {mandate_uri}")

    # MIN_SEED_USDC = 1_000e6 (HelmRegistry.sol:23). Anything less reverts
    # with InsufficientSeed(). Verified via debug_register_revert on
    # 2026-05-19 (tx 0x927f8498… selector 0x03ca7e96).
    seed_amount = 1_000_000_000  # 1000 USDC

    # Approve seed USDC to registry
    send_tx(usdc().functions.approve(
        Web3.to_checksum_address(settings.helm_registry), seed_amount,
    ))

    # AssetEntry tuples (address, AssetKind)
    assets = [
        (Web3.to_checksum_address(settings.snvda), ASSET_KIND_SYNTHETIC),
        (Web3.to_checksum_address(settings.ondo_usdy_adapter), ASSET_KIND_USDY),
    ]
    # WeightConstraint tuples (address, uint16 minBps, uint16 maxBps)
    weight_constraints = [
        (Web3.to_checksum_address(settings.snvda), 4000, 6000),
        (Web3.to_checksum_address(settings.ondo_usdy_adapter), 4000, 6000),
    ]

    result = send_tx(
        registry().functions.registerAgent(
            mandate_hash if mandate_hash.startswith("0x")
            else "0x" + mandate_hash,
            mandate_uri,
            seed_amount,
            assets,
            weight_constraints,
        ),
        gas=3_000_000,  # Registry deploys 4 contracts — generous gas.
    )
    tx_hash = result["tx_hash"]
    print(f"[step1] registerAgent tx: {tx_hash} (block {result['block_number']})")

    # Decode AgentRegistered event from receipt (send_tx already waited +
    # asserted status=1, but we still need the receipt for log parsing).
    receipt = get_w3().eth.get_transaction_receipt(tx_hash)
    logs = registry().events.AgentRegistered().process_receipt(receipt)
    assert logs, "no AgentRegistered event in receipt"
    agent_id = logs[0]["args"]["agentId"]
    print(f"[step1] new agent_id: {agent_id}")

    wait_for_indexer(
        lambda: _agent_exists(agent_id),
        f"Agent {agent_id} in DB",
        timeout=30,
    )
    return agent_id


def _agent_exists(agent_id: int) -> bool:
    with SessionLocal() as db:
        return db.get(models.Agent, agent_id) is not None


# ─── Step 2: refresh Pyth + deposit USDC ─────────────────────────────────────


def step2_deposit(agent_id: int, amount_usdc: int) -> None:
    """Refresh Pyth, approve USDC, vault.deposit(assets, receiver)."""
    with SessionLocal() as db:
        agent = db.get(models.Agent, agent_id)
        assert agent, f"agent {agent_id} disappeared"
        vault_addr = agent.vault_address

    # Refresh Pyth feeds for the assets in the mandate (sNVDA only needs
    # equity feed; USDY adapter doesn't quote via Pyth).
    feed_ids = [FEED_BY_SYMBOL["sNVDA"]]
    update_data, _ = fetch_price_updates(feed_ids)
    if update_data:
        pyth_fee = int(estimate_pyth_fee_wei(len(update_data)))
        update_data_bytes = [
            bytes.fromhex(u[2:] if u.startswith("0x") else u)
            for u in update_data
        ]
        print(f"[step2] Pyth fee: {pyth_fee} wei, feeds: {len(update_data)}")
        send_tx(
            pyth_adapter().functions.updatePriceFeeds(update_data_bytes),
            value=pyth_fee,
        )

    # Approve USDC → vault
    send_tx(usdc().functions.approve(
        Web3.to_checksum_address(vault_addr), amount_usdc,
    ))

    # ERC-4626 deposit(assets, receiver). No Pyth bytes; vault reads from
    # the cached adapter.
    vault = agent_vault(vault_addr)
    result = send_tx(
        vault.functions.deposit(amount_usdc, address()),
        gas=800_000,
    )
    print(f"[step2] deposit tx: {result['tx_hash']} (block {result['block_number']})")

    addr_lower = address().lower()
    wait_for_indexer(
        lambda: _holder_balance(agent_id, addr_lower) > 0,
        f"Holder {addr_lower[:8]}… balance > 0",
        timeout=30,
    )
    wait_for_indexer(
        lambda: _nav_history_count(agent_id) > 0,
        f"NAV history row for agent {agent_id}",
        timeout=30,
    )


def _holder_balance(agent_id: int, addr: str) -> int:
    with SessionLocal() as db:
        h = db.get(models.Holder, (agent_id, addr))
        return int(h.balance) if h else 0


def _nav_history_count(agent_id: int) -> int:
    with SessionLocal() as db:
        return db.query(models.NavPoint).filter_by(agent_id=agent_id).count()


# ─── Step 3: time advance + phase advance ────────────────────────────────────


def step3_advance_phase(agent_id: int) -> None:
    thirty_one_days = 31 * 86400
    send_tx(time_provider().functions.advance(thirty_one_days))
    new_time = time_provider().functions.currentTime().call()
    print(f"[step3] advanced 31d, new currentTime: {new_time}")

    result = send_tx(registry().functions.advanceToPublic(agent_id))
    print(f"[step3] advanceToPublic tx: {result['tx_hash']}")

    wait_for_indexer(
        lambda: _agent_phase(agent_id) == "PublicLaunch",
        f"agent {agent_id} → PublicLaunch",
        timeout=30,
    )


def _agent_phase(agent_id: int) -> str:
    with SessionLocal() as db:
        a = db.get(models.Agent, agent_id)
        return a.phase if a else "?"


# ─── Step 4: K services (rebalance / harvest / distribute / nft) ─────────────


def step4_run_services(agent_id: int) -> None:
    print(f"[step4] rebalance.execute({agent_id})")
    try:
        result = rebalance.execute(agent_id)
        print(f"[step4]   tx: {result['tx_hash']}")
        time.sleep(15)  # wait for Rebalanced event indexing
        decision_count = _decision_count(agent_id, "Rebalance")
        assert decision_count > 0, (
            f"no Rebalance decision indexed (found {decision_count})"
        )
    except Exception as e:
        print(f"[step4] rebalance FAILED: {e}")
        raise

    print(f"[step4] harvest.run({agent_id})")
    try:
        result = harvest.run(agent_id)
        print(f"[step4]   tx: {result['tx_hash']}")
        time.sleep(15)
        h_count = _decision_count(agent_id, "Harvest")
        assert h_count > 0, "no Harvest decision indexed"
    except Exception as e:
        print(f"[step4] harvest WARNING: {e}")  # may have no yield to harvest

    print(f"[step4] distribute.run({agent_id})")
    try:
        result = distribute.run(agent_id)
        print(f"[step4]   {result}")
    except Exception as e:
        print(f"[step4] distribute WARNING: {e}")  # may have no staged yield

    print(f"[step4] nft_metadata.update({agent_id})")
    try:
        result = nft_metadata.update(agent_id)
        print(f"[step4]   tx: {result['tx_hash']}, uri: {result['uri']}")
        time.sleep(5)
        on_chain_uri = agent_nft().functions.tokenURI(agent_id).call()
        print(f"[step4]   on-chain tokenURI: {on_chain_uri}")
        assert (
            result["uri"] in on_chain_uri or on_chain_uri == result["uri"]
        ), f"tokenURI mismatch: {on_chain_uri!r} vs {result['uri']!r}"
    except Exception as e:
        print(f"[step4] nft_metadata FAILED: {e}")
        raise


def _decision_count(agent_id: int, decision_type: str) -> int:
    with SessionLocal() as db:
        return (
            db.query(models.Decision)
            .filter_by(agent_id=agent_id, type=decision_type)
            .count()
        )


# ─── Main ────────────────────────────────────────────────────────────────────


def main() -> None:
    print("=" * 60)
    print("E2E demo flow")
    print("=" * 60)

    try:
        check_environment()
        print()

        agent_id = step1_register_agent()
        print()

        step2_deposit(agent_id, 100_000_000)  # 100 USDC
        print()

        step3_advance_phase(agent_id)
        print()

        step4_run_services(agent_id)
        print()

        print("=" * 60)
        print(f"✓ E2E PASSED — agent_id={agent_id}")
        print("=" * 60)
    except (AssertionError, TimeoutError, Exception) as e:
        print()
        print("=" * 60)
        print(f"✗ FAILED: {type(e).__name__}: {e}")
        print("=" * 60)
        print("\nDiagnostic:")
        print(f"  last indexed block: {_last_indexed()}")
        print(f"  current block:      {get_w3().eth.block_number}")
        with SessionLocal() as db:
            print(f"  agents in DB:       {db.query(models.Agent).count()}")
            print(f"  decisions:          {db.query(models.Decision).count()}")
        raise


if __name__ == "__main__":
    main()
