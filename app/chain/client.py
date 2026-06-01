from web3 import Web3

from app.chain.abi_loader import load_abi
from app.config import settings

_w3: Web3 | None = None


def get_w3() -> Web3:
    global _w3
    if _w3 is None:
        rpc = (
            settings.mantle_sepolia_rpc
            if settings.chain_id == 5003
            else settings.mantle_rpc
        )
        # 10s timeout — without this a slow Mantle RPC node can wedge the
        # whole BE event loop for minutes per request.
        _w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
    return _w3


def contract_at(name: str, address: str):
    """Generic contract loader by name + address."""
    w3 = get_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(address),
        abi=load_abi(name),
    )


# Singleton accessors for system contracts
def registry():              return contract_at("HelmRegistry",        settings.helm_registry)
def agent_nft():             return contract_at("AgentNFT",            settings.agent_nft)
def time_provider():         return contract_at("TimeProvider",        settings.time_provider)
def platform_treasury():     return contract_at("PlatformTreasury",    settings.platform_treasury)
def yield_harvester():       return contract_at("YieldHarvester",      settings.yield_harvester)
def dividend_distributor():  return contract_at("DividendDistributor", settings.dividend_distributor)
def redemption_queue():      return contract_at("RedemptionQueue",     settings.redemption_queue)
def pyth_adapter():          return contract_at("PythPriceAdapter",    settings.pyth_price_adapter)
def usdc():                  return contract_at("MockERC20",           settings.usdc)


# Per-agent factories
def agent_vault(address: str):    return contract_at("AgentVault",    address)
def agent_token(address: str):    return contract_at("AgentToken",    address)
def founder_vault(address: str):  return contract_at("FounderVault",  address)
