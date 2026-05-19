from app.chain.abi_loader import load_abi
from app.chain.client import agent_nft, get_w3, registry
from app.indexer.handlers import nft as nft_h
from app.indexer.handlers import registry as registry_h
from app.indexer.handlers import vault as vault_h

REGISTRY_EVENTS = {
    "AgentRegistered": registry_h.handle_agent_registered,
    "PhaseAdvanced":   registry_h.handle_phase_advanced,
    "AgentSlashed":    registry_h.handle_agent_slashed,
    "AgentWindDown":   registry_h.handle_agent_wind_down,
    "AgentSettled":    registry_h.handle_agent_settled,
}

VAULT_EVENTS = {
    "Rebalanced":     vault_h.handle_rebalanced,
    "YieldDeposited": vault_h.handle_yield_deposited,
    "Deposit":        vault_h.handle_deposit,
    "Withdraw":       vault_h.handle_withdraw,
}

NFT_EVENTS = {
    "ReputationSlashed": nft_h.handle_reputation_slashed,
    "TokenURISet":       nft_h.handle_token_uri_set,
}


def process_range(db, start_block: int, end_block: int):
    w3 = get_w3()

    # 1) Registry events
    _process_contract_events(db, registry(), REGISTRY_EVENTS, start_block, end_block)
    # 2) NFT events
    _process_contract_events(db, agent_nft(), NFT_EVENTS, start_block, end_block)
    # 3) Vault events for every known agent vault
    from app.db.models import Agent
    vaults = db.query(Agent).all()
    for a in vaults:
        c = w3.eth.contract(
            address=w3.to_checksum_address(a.vault_address),
            abi=load_abi("AgentVault"),
        )
        _process_contract_events(db, c, VAULT_EVENTS, start_block, end_block)


def _process_contract_events(db, contract, event_map: dict, start: int, end: int):
    for event_name, handler in event_map.items():
        try:
            ev = getattr(contract.events, event_name)
            logs = ev.get_logs(from_block=start, to_block=end)
            for log in logs:
                handler(db, log)
        except Exception as e:
            # Decoding failure (ABI mismatch etc.) — skip just that event type
            print(f"[indexer] {event_name} failed: {e}")
