from app.chain.abi_loader import load_abi
from app.chain.client import (
    agent_nft,
    dividend_distributor,
    get_w3,
    redemption_queue,
    registry,
)
from app.indexer.handlers import distributor as distributor_h
from app.indexer.handlers import founder as founder_h
from app.indexer.handlers import nft as nft_h
from app.indexer.handlers import redemption as redemption_h
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

DISTRIBUTOR_EVENTS = {
    "Distributed": distributor_h.handle_distributed,
    "Claimed":     distributor_h.handle_claimed,
}

REDEMPTION_EVENTS = {
    "RedeemRequested": redemption_h.handle_redeem_requested,
    "RedeemClaimed":   redemption_h.handle_redeem_claimed,
    "RedeemCancelled": redemption_h.handle_redeem_cancelled,
}

FOUNDER_EVENTS = {
    "CarryReceived":          founder_h.handle_carry_received,
    "CarryClaimed":           founder_h.handle_carry_claimed,
    "SharesWithdrawn":        founder_h.handle_shares_withdrawn,
    "SubordinationTriggered": founder_h.handle_subordination_triggered,
}


def process_range(db, start_block: int, end_block: int):
    w3 = get_w3()

    # 1) Registry events (global — one RPC regardless of agent count)
    _process_contract_events(db, registry(), REGISTRY_EVENTS, start_block, end_block)
    # 2) NFT events (global)
    _process_contract_events(db, agent_nft(), NFT_EVENTS, start_block, end_block)
    # 3) DividendDistributor (global)
    _process_contract_events(db, dividend_distributor(), DISTRIBUTOR_EVENTS, start_block, end_block)
    # 4) RedemptionQueue (global)
    _process_contract_events(db, redemption_queue(), REDEMPTION_EVENTS, start_block, end_block)
    # 5) Per-agent vault + founder vault events — only for on-chain agents
    #    that are still active. Skip:
    #      * seed agents (agent_id >= 9000) — stub vault addresses, never emit.
    #      * Settled agents — fully wound down, no rebalance/yield activity.
    from app.db.models import Agent
    vaults = (
        db.query(Agent)
        .filter(Agent.agent_id < 9000)
        .filter(Agent.phase != "Settled")
        .all()
    )
    for a in vaults:
        c = w3.eth.contract(
            address=w3.to_checksum_address(a.vault_address),
            abi=load_abi("AgentVault"),
        )
        _process_contract_events(db, c, VAULT_EVENTS, start_block, end_block)

        c_fv = w3.eth.contract(
            address=w3.to_checksum_address(a.founder_vault_address),
            abi=load_abi("FounderVault"),
        )
        _process_contract_events(db, c_fv, FOUNDER_EVENTS, start_block, end_block)


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
