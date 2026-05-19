"""Diagnose the registerAgent revert at tx 0x927f8498...

Outputs:
1. HelmRegistry.registerAgent ABI input types
2. Decoded calldata from the failed tx
3. eth_call simulation revert reason (if RPC supports)
"""
import json

from app.chain.abi_loader import load_abi
from app.chain.client import get_w3, registry

FAILED_TX = "0x927f8498c65d1e6a9dc7a87fcdc2502df734f33d4b0b9647616c38773be9b7e3"


def print_abi_inputs() -> None:
    abi = load_abi("HelmRegistry")
    fn = next(f for f in abi if f.get("name") == "registerAgent")
    print("=== registerAgent ABI inputs ===")
    print(json.dumps(fn["inputs"], indent=2))
    print()


def print_custom_errors() -> None:
    abi = load_abi("HelmRegistry")
    errs = [f for f in abi if f.get("type") == "error"]
    print(f"=== HelmRegistry custom errors ({len(errs)}) ===")
    for e in errs:
        params = ",".join(p["type"] for p in e.get("inputs", []))
        print(f"  error {e['name']}({params})")
    print()


def decode_failed_tx() -> dict:
    w3 = get_w3()
    tx = w3.eth.get_transaction(FAILED_TX)
    print("=== Failed tx ===")
    print(f"from:  {tx['from']}")
    print(f"to:    {tx['to']}")
    print(f"value: {tx['value']}")
    print(f"gas:   {tx['gas']}")
    print(f"block: {tx['blockNumber']}")
    print(f"input prefix (selector + first 200 hex): {tx['input'][:200].hex() if hasattr(tx['input'], 'hex') else tx['input'][:200]}")

    r = registry()
    try:
        fn, params = r.decode_function_input(tx["input"])
        print(f"\nDecoded function: {fn.fn_name}")
        for k, v in params.items():
            if isinstance(v, list) and len(v) > 0 and isinstance(v[0], (tuple, list)):
                print(f"  {k}: (list of tuples, n={len(v)})")
                for i, item in enumerate(v):
                    print(f"    [{i}]: {item}")
            else:
                preview = str(v)[:120]
                print(f"  {k}: {preview}")
    except Exception as e:
        print(f"decode failed: {e}")
    print()
    return dict(tx)


def simulate_eth_call(tx: dict) -> None:
    w3 = get_w3()
    raw_input = tx["input"]
    if hasattr(raw_input, "hex"):
        data_hex = "0x" + raw_input.hex()
    elif isinstance(raw_input, str):
        data_hex = raw_input if raw_input.startswith("0x") else "0x" + raw_input
    else:
        data_hex = "0x" + bytes(raw_input).hex()

    call_data = {
        "from": tx["from"],
        "to": tx["to"],
        "value": tx["value"],
        "data": data_hex,
        "gas": tx["gas"],
    }
    print("=== eth_call simulation ===")
    try:
        result = w3.eth.call(call_data, block_identifier=tx["blockNumber"] - 1)
        print(f"unexpected success: {result.hex() if hasattr(result, 'hex') else result}")
    except Exception as e:
        print(f"exception class: {type(e).__name__}")
        print(f"message:         {e}")
        # Try to extract the 4-byte selector for custom-error decoding
        for attr in ("data", "args"):
            val = getattr(e, attr, None)
            if val is not None:
                print(f"  e.{attr}: {val!r}")
    print()


def main() -> None:
    print_abi_inputs()
    print_custom_errors()
    tx = decode_failed_tx()
    simulate_eth_call(tx)


if __name__ == "__main__":
    main()
