"""Diagnose AgentVault.executeRebalance revert at the failed tx.

Outputs:
1. executeRebalance ABI input/output types
2. Decoded calldata
3. eth_call simulation revert reason (selector mapped to custom errors)
"""
import json

from web3 import Web3

from app.chain.abi_loader import load_abi
from app.chain.client import agent_vault, get_w3

FAILED_TX = "0x215d17d20f1998380445099be5f1707f23de8b928afb4d637821d0fec5c46d9e"


def print_abi() -> None:
    abi = load_abi("AgentVault")
    fn = next(f for f in abi if f.get("name") == "executeRebalance")
    print("=== executeRebalance ABI ===")
    print(json.dumps(fn, indent=2))
    print()


def print_custom_errors() -> None:
    abi = load_abi("AgentVault")
    errs = [f for f in abi if f.get("type") == "error"]
    print(f"=== AgentVault custom errors ({len(errs)}) ===")
    for e in errs:
        sig = f"{e['name']}({','.join(i['type'] for i in e.get('inputs', []))})"
        sel = "0x" + Web3.keccak(text=sig)[:4].hex()
        print(f"  {sel}  {sig}")
    print()


def decode_tx() -> dict:
    w3 = get_w3()
    tx = w3.eth.get_transaction(FAILED_TX)
    print("=== Failed tx ===")
    print(f"to:    {tx['to']}")
    print(f"from:  {tx['from']}")
    print(f"value: {tx['value']}")
    print(f"gas:   {tx['gas']}")
    print(f"block: {tx['blockNumber']}")

    vault = agent_vault(tx["to"])
    try:
        fn, params = vault.decode_function_input(tx["input"])
        print(f"\nDecoded function: {fn.fn_name}")
        for k, v in params.items():
            if isinstance(v, list) and v and isinstance(v[0], (tuple, list)):
                print(f"  {k}: (list of {len(v)} tuples)")
                for i, item in enumerate(v):
                    print(f"    [{i}]: {item}")
            else:
                print(f"  {k}: {str(v)[:120]}")
    except Exception as e:
        print(f"decode failed: {e}")
    print()
    return dict(tx)


def simulate(tx: dict) -> None:
    w3 = get_w3()
    raw_input = tx["input"]
    if hasattr(raw_input, "hex"):
        data_hex = "0x" + raw_input.hex()
    elif isinstance(raw_input, str):
        data_hex = raw_input if raw_input.startswith("0x") else "0x" + raw_input
    else:
        data_hex = "0x" + bytes(raw_input).hex()

    call_data = {
        "from": tx["from"], "to": tx["to"], "value": tx["value"],
        "data": data_hex, "gas": tx["gas"],
    }
    print("=== eth_call simulation ===")
    try:
        result = w3.eth.call(call_data, block_identifier=tx["blockNumber"] - 1)
        print(f"unexpected success: {result.hex() if hasattr(result, 'hex') else result}")
    except Exception as e:
        print(f"exception class: {type(e).__name__}")
        print(f"message:         {e}")
        raw_sel = None
        if hasattr(e, "data") and e.data:
            print(f"e.data: {e.data!r}")
            d = e.data
            if isinstance(d, str):
                raw_sel = d[:10] if d.startswith("0x") else "0x" + d[:8]
            elif isinstance(d, (bytes, bytearray)):
                raw_sel = "0x" + bytes(d)[:4].hex()
        if raw_sel:
            abi = load_abi("AgentVault")
            for item in abi:
                if item.get("type") == "error":
                    sig = f"{item['name']}({','.join(i['type'] for i in item.get('inputs', []))})"
                    expected = "0x" + Web3.keccak(text=sig)[:4].hex()
                    if expected.lower() == raw_sel.lower():
                        print(f"  ← MATCH: {sig}")
                        break
            else:
                print(f"  selector {raw_sel} did not match any AgentVault error")
    print()


def main() -> None:
    print_abi()
    print_custom_errors()
    tx = decode_tx()
    simulate(tx)


if __name__ == "__main__":
    main()
