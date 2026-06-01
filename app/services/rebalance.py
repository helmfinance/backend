from web3 import Web3

from app.chain.client import (
    agent_vault,
    contract_at,
    platform_treasury,
    pyth_adapter,
    registry,
)
from app.chain.executor_wallet import send_tx
from app.db import models
from app.db.session import SessionLocal
from app.hermes.client import fetch_price_updates
from app.services import decision_engine

# IAgentVault.AssetKind enum values.
KIND_SYNTHETIC = 0
KIND_METH_ADAPTER = 1
KIND_USDY_ADAPTER = 2

# Per-symbol kind classifier (matches the e2e mandate authoring convention).
_KIND_BY_SYMBOL = {
    "sNVDA": KIND_SYNTHETIC, "sSPY": KIND_SYNTHETIC, "sAAPL": KIND_SYNTHETIC,
    "sTSLA": KIND_SYNTHETIC, "sMSFT": KIND_SYNTHETIC,
    "mETH": KIND_METH_ADAPTER,
    "USDY": KIND_USDY_ADAPTER,
}


def execute(agent_id: int) -> dict:
    """Compute target weights → convert to executeRebalance-compatible amounts
    (per-kind units, NOT raw USDC) → refresh Pyth → executeRebalance.

    Unit handling reflects AgentVault.executeRebalance design:
      * Synthetic: ``t.amount`` is the asset's own token unit (18d). _buy
        derives USDC via ``amount * priceUSDC / 10^d``.
      * METH/USDY adapter: ``t.amount`` is USDC (6d). _buy uses it directly
        as the deposit amount into the adapter.
    """
    with SessionLocal() as db:
        agent = db.get(models.Agent, agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")
        if agent.phase not in ("Incubation", "PublicLaunch"):
            raise ValueError(f"Agent phase {agent.phase} not rebalanceable")

        weight_targets = decision_engine.compute_target_weights(agent)
        if not weight_targets:
            raise ValueError("No mandate weight constraints")

        vault_addr = Web3.to_checksum_address(agent.vault_address)
        vault = agent_vault(agent.vault_address)

        # 1) Use totalAssets (NAV) as the sizing base — not just cashUSDC.
        # After the first rebalance the cash is mostly drained into positions,
        # so cashUSDC-based sizing collapses every subsequent rebalance target
        # to ~0 (which then forces step-1 sell of nearly everything). NAV is
        # the same on first and Nth rebalance, so target weights stay stable.
        # Leave headroom equal to the on-chain rebalance fee so the post-buy
        # cashUSDC still covers `_payFee` (else InsufficientCash() revert).
        nav = vault.functions.totalAssets().call()
        if nav == 0:
            raise ValueError(
                f"Vault {vault_addr} has 0 NAV — cannot rebalance",
            )
        try:
            fee_bps = int(platform_treasury().functions.feeRate(2).call())  # FeeKind.Rebalance = 2
        except Exception:
            fee_bps = 5  # conservative fallback matching the deployed default
        # Carve out fee + 100 bps slippage headroom from the sizing base so
        # step-1 trim leaves enough cash to cover step-4 fee transfer. Without
        # this, NAV-sized targets equal NAV exactly → cash drains to 0 → next
        # rebalance hits InsufficientCash on the fee payment.
        usdc_balance = nav * (10_000 - fee_bps - 100) // 10_000

        # 2) Refresh Pyth feeds. We refresh ALL deployed synthetic feeds, not
        # just the ones in this agent's mandate — AgentVault.executeRebalance
        # internally references the system-wide synthetic universe (e.g. for
        # invariant checks / benchmark pricing), and stale feeds for assets
        # OUTSIDE this agent's targets still revert the entire tx with
        # PriceStale. Cheap to update them all; expensive to debug a partial
        # refresh.
        feed_ids = _collect_all_synthetic_feed_ids()
        if feed_ids:
            _refresh_pyth(feed_ids)

        # 3) bps → executeRebalance amount per asset kind.
        # Synthetic targets are token units (18 dec). Adapter targets are
        # USDC value (6 dec) — matches the post-fix AgentVault._balanceOf
        # adapter path that now returns valueInUSDC for METH/USDY.
        abs_targets: list[tuple[str, int]] = []
        for symbol, weight_bps in weight_targets:
            asset_addr = _symbol_to_address(symbol)
            usdc_target = usdc_balance * weight_bps // 10_000
            kind = _KIND_BY_SYMBOL.get(symbol)
            if kind == KIND_SYNTHETIC:
                amount = _usdc_to_synthetic_tokens(asset_addr, usdc_target)
            elif kind in (KIND_METH_ADAPTER, KIND_USDY_ADAPTER):
                amount = usdc_target
            else:
                raise ValueError(f"Unknown asset kind for symbol {symbol}")
            abs_targets.append((asset_addr, amount))

        # 4) executeRebalance. proof is reserved for future ZK / signature
        # attestation — empty bytes today.
        proof = b""
        exec_fn = agent_vault(agent.vault_address).functions.executeRebalance(
            abs_targets, proof,
        )

        # Dry-run via eth_call to capture revert selector before the real tx.
        # Mantle's public RPC blocks debug_traceTransaction, so this is the
        # only way to surface the actual revert reason in BE logs.
        # rebuild-marker: DRYRUN_V3_2026-05-27
        import logging
        log = logging.getLogger(__name__)
        from app.chain.executor_wallet import address as executor_address
        print("[rebalance] DRYRUN_V3 entering dry-run", flush=True)
        try:
            exec_fn.call({"from": executor_address()})
        except Exception as dry_err:
            data_attr = getattr(dry_err, "data", None) or getattr(dry_err, "message", None)
            log.warning(
                "[rebalance] DRYRUN_V3 REVERT agent=%s data=%r err=%r",
                agent_id, data_attr, dry_err,
            )
            print(f"[rebalance] DRYRUN_V3 REVERT data={data_attr} err={dry_err}", flush=True)
            raise RuntimeError(f"DRYRUN_V3 revert: data={data_attr} err={dry_err}")

        # executeRebalance touches N synthetics + adapters + Pyth — easily
        # exceeds the default 500k gas cap on Mantle. Bump explicitly.
        try:
            tx_hash = send_tx(exec_fn, gas=2_000_000)["tx_hash"]
        except Exception:
            # Mandate breach possible — notify registry (reputation slash).
            try:
                send_tx(registry().functions.notifyMandateBreach(
                    agent_id, "executeRebalance reverted",
                ))
            except Exception:
                pass
            raise

        # Synchronously refresh positions so the FE detail page reflects the
        # new allocation on the very next request. The indexer-fed refresh in
        # handle_rebalanced sometimes loses Position rows to a single failed
        # web3 call inside the loop (outer except swallows it); calling here
        # in a fresh session with explicit commit makes the update reliable.
        try:
            from app.indexer.handlers.vault import _refresh_positions, _snapshot_nav
            import time as _t
            with SessionLocal() as fresh_db:
                agent_fresh = fresh_db.get(models.Agent, agent_id)
                if agent_fresh:
                    _refresh_positions(fresh_db, agent_id, agent_fresh.vault_address)
                    _snapshot_nav(
                        fresh_db, agent_id, agent_fresh.vault_address,
                        agent_fresh.token_address, int(_t.time()),
                    )
                    fresh_db.commit()
        except Exception as e:
            log.warning("[rebalance] post-tx position refresh failed (non-fatal): %s", e)

        return {"tx_hash": tx_hash, "targets": abs_targets}


def _symbol_to_address(symbol: str) -> str:
    from app.config import settings
    mapping = {
        "sNVDA": settings.snvda, "sSPY": settings.sspy, "sAAPL": settings.saapl,
        "sTSLA": settings.stsla, "sMSFT": settings.smsft,
        "mETH": settings.mantle_meth_adapter,
        "USDY": settings.ondo_usdy_adapter,
    }
    addr = mapping.get(symbol)
    if not addr:
        raise ValueError(f"Unknown asset: {symbol}")
    return Web3.to_checksum_address(addr)


def _usdc_to_synthetic_tokens(asset_addr: str, usdc_target_6d: int) -> int:
    """Convert a USDC amount (6 decimals) into the synthetic asset's own
    token units. The vault's _buy will reverse this back to USDC via
    ``amount * priceUSDC / 10^decimals`` before minting, so any drift comes
    from integer division only.
    """
    c = contract_at("SyntheticAsset", asset_addr)
    price_6d = int(c.functions.priceUSDC().call())  # USDC-per-token, 1e6 scale
    if price_6d == 0:
        raise ValueError(f"priceUSDC returned 0 for {asset_addr}")
    decimals = int(c.functions.decimals().call())
    return (usdc_target_6d * 10**decimals) // price_6d


def _collect_feed_ids(weight_targets: list[tuple[str, int]]) -> list[str]:
    """Return the on-chain pythFeedId for every SyntheticAsset target.
    Adapter targets (mETH/USDY) don't quote via Pyth so they're skipped.
    """
    feed_ids: list[str] = []
    for symbol, _ in weight_targets:
        if _KIND_BY_SYMBOL.get(symbol) != KIND_SYNTHETIC:
            continue
        asset_addr = _symbol_to_address(symbol)
        try:
            fid = contract_at("SyntheticAsset", asset_addr).functions.pythFeedId().call()
            feed_ids.append("0x" + fid.hex())
        except Exception:
            continue
    return feed_ids


def _collect_all_synthetic_feed_ids() -> list[str]:
    """Return pythFeedIds that AgentVault.executeRebalance touches across
    the system-wide asset universe, independent of the calling agent's
    mandate.

    Includes:
      * Every deployed SyntheticAsset (sNVDA, sSPY, sAAPL, sTSLA, sMSFT) —
        any stale feed reverts the entire rebalance via Pyth's staleness
        check during invariant computation.
      * ETH/USD — the mETH adapter values its stake via this feed. Even when
        the current agent doesn't hold mETH, the adapter's internal price
        path is touched and reverts on staleness (this is the feed the
        recent demo runs were hitting: 0xff61491a... is ETH/USD, not sSPY).
      * USDC/USD — symmetrically used for stablecoin valuation paths.
    """
    from app.config import settings
    feed_ids: list[str] = []

    for env_key in ("snvda", "sspy", "saapl", "stsla", "smsft"):
        addr = getattr(settings, env_key, None)
        if not addr:
            continue
        try:
            fid = contract_at(
                "SyntheticAsset", Web3.to_checksum_address(addr),
            ).functions.pythFeedId().call()
            feed_ids.append("0x" + fid.hex())
        except Exception:
            continue

    # Non-synthetic feeds the adapters depend on. Pulled from envvars rather
    # than chain reads because adapters don't expose their feed ids.
    for env_key in ("pyth_feed_eth_usd", "pyth_feed_usdc_usd"):
        fid = getattr(settings, env_key, "")
        if fid:
            feed_ids.append(fid if fid.startswith("0x") else "0x" + fid)

    return feed_ids


def _refresh_pyth(feed_ids: list[str]) -> None:
    """Pull priceUpdateData from Hermes, pay the on-chain fee, broadcast."""
    update_data, _ = fetch_price_updates(feed_ids)
    if not update_data:
        return
    update_bytes = [
        bytes.fromhex(u[2:] if u.startswith("0x") else u)
        for u in update_data
    ]
    adapter = pyth_adapter()
    fee = int(adapter.functions.getUpdateFee(update_bytes).call())
    send_tx(adapter.functions.updatePriceFeeds(update_bytes), value=fee)
