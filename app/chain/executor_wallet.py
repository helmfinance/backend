from eth_account import Account

from app.chain.client import get_w3
from app.config import settings


def get_account():
    if not settings.cron_signer_private_key:
        raise RuntimeError("CRON_SIGNER_PRIVATE_KEY not set in .env")
    return Account.from_key(settings.cron_signer_private_key)


def address() -> str:
    return get_account().address


def balance_wei() -> int:
    return get_w3().eth.get_balance(address())


def send_tx(contract_func, value: int = 0, gas: int = 500_000) -> str:
    """Build, sign, send a tx. Returns tx_hash hex.

    contract_func: bound function call, e.g. registry().functions.advanceToPublic(42)
    """
    w3 = get_w3()
    acct = get_account()
    tx = contract_func.build_transaction({
        "from": acct.address,
        "nonce": w3.eth.get_transaction_count(acct.address, "pending"),
        "value": value,
        "chainId": settings.chain_id,
        "gas": gas,
        "gasPrice": w3.eth.gas_price,
    })
    signed = acct.sign_transaction(tx)
    # eth-account ≥ 0.13 renamed `rawTransaction` → `raw_transaction`.
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    return tx_hash.hex()
