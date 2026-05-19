from eth_account import Account

from app.chain.client import get_w3
from app.config import settings

# Mantle Sepolia gas oracle is noisy; over-pay by 20% so a tx doesn't get
# stuck below the next block's base price. On a same-nonce conflict
# ("replacement transaction underpriced") we retry once at 1.5x.
GAS_PRICE_MULTIPLIER = 1.2
GAS_PRICE_RETRY_MULTIPLIER = 1.5
RECEIPT_TIMEOUT = 60


def get_account():
    if not settings.cron_signer_private_key:
        raise RuntimeError("CRON_SIGNER_PRIVATE_KEY not set in .env")
    return Account.from_key(settings.cron_signer_private_key)


def address() -> str:
    return get_account().address


def balance_wei() -> int:
    return get_w3().eth.get_balance(address())


def send_tx(
    contract_func,
    *,
    value: int = 0,
    gas: int = 500_000,
    wait: bool = True,
) -> dict | str:
    """Build, sign, send a transaction.

    By default waits for the receipt and returns a dict with the resolved
    transaction context. Callers that just want fire-and-forget can pass
    ``wait=False`` to get the hex tx-hash back.

    Returns:
        wait=True:  ``{"tx_hash", "block_number", "gas_used", "status"}``
        wait=False: tx_hash hex string

    Raises:
        RuntimeError: receipt ``status != 1`` (transaction reverted).
        TimeoutError: receipt did not appear within ``RECEIPT_TIMEOUT``.
    """
    return _send_with_retry(contract_func, value, gas, wait, retry_count=0)


def _send_with_retry(contract_func, value, gas, wait, retry_count):
    w3 = get_w3()
    acct = get_account()
    base_gas_price = w3.eth.gas_price
    multiplier = (
        GAS_PRICE_RETRY_MULTIPLIER if retry_count > 0 else GAS_PRICE_MULTIPLIER
    )
    gas_price = int(base_gas_price * multiplier)

    tx = contract_func.build_transaction({
        "from": acct.address,
        "nonce": w3.eth.get_transaction_count(acct.address, "pending"),
        "value": value,
        "chainId": settings.chain_id,
        "gas": gas,
        "gasPrice": gas_price,
    })
    signed = acct.sign_transaction(tx)
    # eth-account ≥ 0.13 renamed `rawTransaction` → `raw_transaction`.
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction

    try:
        tx_hash = w3.eth.send_raw_transaction(raw).hex()
    except Exception as e:
        msg = str(e).lower()
        if "replacement transaction underpriced" in msg and retry_count == 0:
            print(
                f"[send_tx] underpriced; retrying at "
                f"{GAS_PRICE_RETRY_MULTIPLIER}x gas price",
            )
            return _send_with_retry(contract_func, value, gas, wait, retry_count + 1)
        raise

    if not wait:
        return tx_hash

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=RECEIPT_TIMEOUT)
    if receipt["status"] != 1:
        raise RuntimeError(
            f"tx reverted: {tx_hash} (block {receipt['blockNumber']})",
        )

    return {
        "tx_hash": tx_hash,
        "block_number": receipt["blockNumber"],
        "gas_used": receipt["gasUsed"],
        "status": receipt["status"],
    }
