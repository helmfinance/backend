from app.db.models import Agent


def compute_target_weights(agent: Agent) -> list[tuple[str, int]]:
    """Returns [(symbol, weight_bps), ...] summing to <= 10000.

    Reads weight constraints directly from the on-chain vault (source of
    truth) and maps asset addresses back to symbols. Falls back to the
    BE mandate JSON only when chain reads fail.

    Algorithm: midpoint of each constraint, scaled down if sum > 10000.
    """
    from app.chain.client import agent_vault
    from app.config import settings

    addr_to_symbol = {
        settings.snvda.lower(): "sNVDA",
        settings.sspy.lower(): "sSPY",
        settings.saapl.lower(): "sAAPL",
        settings.stsla.lower(): "sTSLA",
        settings.smsft.lower(): "sMSFT",
        settings.mantle_meth_adapter.lower(): "mETH",
        settings.ondo_usdy_adapter.lower(): "USDY",
    }

    pairs: list[tuple[str, int]] = []
    total = 0
    try:
        vault = agent_vault(agent.vault_address)
        n = vault.functions.assetCount().call()
        for i in range(n):
            asset_addr, _ = vault.functions.assetAt(i).call()
            sym = addr_to_symbol.get(asset_addr.lower())
            if not sym:
                continue
            min_bps, max_bps = vault.functions.weightConstraintOf(asset_addr).call()
            mid = (min_bps + max_bps) // 2
            if mid <= 0:
                continue
            pairs.append((sym, mid))
            total += mid
    except Exception:
        # Chain unreachable / vault not yet deployed — fall back to mandate JSON
        # (best-effort; ignores asset-class entries that the LLM might emit).
        KNOWN_SYMBOLS = {"sNVDA", "sSPY", "sAAPL", "sTSLA", "sMSFT", "mETH", "USDY"}
        for c in agent.mandate.get("weightConstraints", []):
            if c.get("asset") not in KNOWN_SYMBOLS:
                continue
            mid = (c["minBps"] + c["maxBps"]) // 2
            if mid <= 0:
                continue
            pairs.append((c["asset"], mid))
            total += mid

    if total > 10000:
        pairs = [(s, w * 10000 // total) for s, w in pairs]
    return pairs
