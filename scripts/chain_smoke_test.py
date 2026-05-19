"""Read-only chain connectivity verification."""
from app.chain.client import (
    agent_nft,
    get_w3,
    registry,
    time_provider,
    usdc,
)
from app.chain.executor_wallet import address, balance_wei


def main():
    w3 = get_w3()
    print(f"connected: {w3.is_connected()}")
    print(f"chain_id: {w3.eth.chain_id}")
    print(f"block: {w3.eth.block_number}")
    print(f"executor address: {address()}")
    print(f"executor balance: {balance_wei() / 1e18:.4f} MNT")

    # Read a few view fns to verify ABIs match deployed contracts
    print(f"agentNFT.name: {agent_nft().functions.name().call()}")
    print(f"usdc.symbol: {usdc().functions.symbol().call()}")
    print(f"timeProvider.currentTime: {time_provider().functions.currentTime().call()}")
    # registry view fn — try a couple common ones
    try:
        print(f"registry.totalAgents: {registry().functions.totalAgents().call()}")
    except Exception:
        try:
            print(f"registry.agentCount: {registry().functions.agentCount().call()}")
        except Exception as e:
            print(f"(registry total view fn not found — non-fatal: {type(e).__name__})")


if __name__ == "__main__":
    main()
