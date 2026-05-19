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

        # 1) Use cashUSDC (USDC balance minus reserved yieldPool) for sizing.
        # Leave headroom equal to the on-chain rebalance fee so the post-buy
        # cashUSDC still covers `_payFee` (else InsufficientCash() revert).
        cash_usdc = vault.functions.cashUSDC().call()
        if cash_usdc == 0:
            raise ValueError(
                f"Vault {vault_addr} has 0 cashUSDC — cannot rebalance",
            )
        try:
            fee_bps = int(platform_treasury().functions.feeRate(2).call())  # FeeKind.Rebalance = 2
        except Exception:
            fee_bps = 5  # conservative fallback matching the deployed default
        usdc_balance = cash_usdc * (10_000 - fee_bps - 1) // 10_000  # extra 1bps cushion

        # 2) Refresh Pyth feeds for synthetic targets BEFORE asking SyntheticAsset
        # for priceUSDC (else `getPriceUsdc` reverts with PriceStale).
        feed_ids = _collect_feed_ids(weight_targets)
        if feed_ids:
            _refresh_pyth(feed_ids)

        # 3) bps → executeRebalance amount per asset kind.
        abs_targets: list[tuple[str, int]] = []
        for symbol, weight_bps in weight_targets:
            asset_addr = _symbol_to_address(symbol)
            usdc_target = usdc_balance * weight_bps // 10_000
            kind = _KIND_BY_SYMBOL.get(symbol)
            if kind == KIND_SYNTHETIC:
                amount = _usdc_to_synthetic_tokens(asset_addr, usdc_target)
            elif kind in (KIND_METH_ADAPTER, KIND_USDY_ADAPTER):
                # Adapter takes USDC directly as the deposit amount.
                amount = usdc_target
            else:
                raise ValueError(f"Unknown asset kind for symbol {symbol}")
            abs_targets.append((asset_addr, amount))

        # 4) executeRebalance. proof is reserved for future ZK / signature
        # attestation — empty bytes today.
        proof = b""
        try:
            tx_hash = send_tx(
                agent_vault(agent.vault_address).functions.executeRebalance(
                    abs_targets, proof,
                )
            )["tx_hash"]
        except Exception:
            # Mandate breach possible — notify registry (reputation slash).
            try:
                send_tx(registry().functions.notifyMandateBreach(
                    agent_id, "executeRebalance reverted",
                ))
            except Exception:
                pass
            raise

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
