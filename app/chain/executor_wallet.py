import re
import time

from eth_account import Account

from app.chain.client import get_w3
from app.config import settings

# Mantle Sepolia gas oracle is noisy; over-pay by 20% so a tx doesn't get
# stuck below the next block's base price. On `replacement underpriced`
# we bump multiplier by 1.3x (capped at 1.5x floor). On `nonce too low`
# we parse the RPC's expected nonce out of the error string and re-sign.
GAS_PRICE_MULTIPLIER = 1.2
GAS_PRICE_RETRY_MULTIPLIER = 1.5
RECEIPT_TIMEOUT = 300
MAX_RETRIES = 3
# Geth/Erigon-style sequencer error: "nonce too low: next nonce 55, tx nonce 54"
NONCE_RE = re.compile(r"next nonce (\d+)")


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
    transaction context. ``wait=False`` returns the hex tx-hash immediately.

    On ``nonce too low``: parse the RPC's "next nonce N" hint and retry with
    that nonce overridden. On ``replacement underpriced``: bump the gas
    multiplier. Total retries capped at ``MAX_RETRIES``.

    Returns:
        wait=True:  ``{"tx_hash", "block_number", "gas_used", "status"}``
        wait=False: tx_hash hex string

    Raises:
        RuntimeError: receipt ``status != 1`` (transaction reverted) or
            retry budget exhausted.
        TimeoutError: receipt did not appear within ``RECEIPT_TIMEOUT``.
    """
    return _send_with_retry(
        contract_func,
        value=value,
        gas=gas,
        wait=wait,
        override_nonce=None,
        gas_multiplier=GAS_PRICE_MULTIPLIER,
        attempt=0,
    )


def _send_with_retry(
    contract_func,
    *,
    value,
    gas,
    wait,
    override_nonce,
    gas_multiplier,
    attempt,
):
    if attempt >= MAX_RETRIES:
        raise RuntimeError(f"send_tx exceeded {MAX_RETRIES} retries")

    w3 = get_w3()
    acct = get_account()

    nonce = (
        override_nonce
        if override_nonce is not None
        else w3.eth.get_transaction_count(acct.address, "pending")
    )
    gas_price = int(w3.eth.gas_price * gas_multiplier)

    tx = contract_func.build_transaction({
        "from": acct.address,
        "nonce": nonce,
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

        # Nonce stale — parse expected nonce from the error and re-sign.
        if "nonce too low" in msg or "nonce is too low" in msg:
            m = NONCE_RE.search(str(e))
            if m:
                expected = int(m.group(1))
                print(f"[send_tx] nonce stale ({nonce}); using expected nonce {expected}")
            else:
                expected = nonce + 1
                print(f"[send_tx] nonce stale ({nonce}); incrementing to {expected}")
            time.sleep(1)
            return _send_with_retry(
                contract_func,
                value=value,
                gas=gas,
                wait=wait,
                override_nonce=expected,
                gas_multiplier=gas_multiplier,
                attempt=attempt + 1,
            )

        # Same-nonce conflict — bump gas to displace the stuck tx.
        if "replacement transaction underpriced" in msg or "underpriced" in msg:
            new_mult = max(gas_multiplier * 1.3, GAS_PRICE_RETRY_MULTIPLIER)
            print(f"[send_tx] underpriced; gas multiplier {gas_multiplier} → {new_mult}")
            time.sleep(1)
            return _send_with_retry(
                contract_func,
                value=value,
                gas=gas,
                wait=wait,
                override_nonce=override_nonce,
                gas_multiplier=new_mult,
                attempt=attempt + 1,
            )

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
        "receipt": receipt,  # raw receipt incl. logs — callers reuse to avoid
                             # a second eth_getTransactionReceipt RPC roundtrip,
                             # which on Mantle Sepolia occasionally hits a node
                             # that hasn't seen the tx yet (TransactionNotFound).
    }
