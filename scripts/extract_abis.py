"""Extract ABI from forge artifacts into app/chain/abis/."""
import json
import pathlib
import sys

FORGE_OUT = pathlib.Path("../contracts/out")
DEST = pathlib.Path("app/chain/abis")

CONTRACTS = [
    "HelmRegistry", "AgentNFT", "TimeProvider", "PlatformTreasury",
    "YieldHarvester", "DividendDistributor", "RedemptionQueue",
    "PythPriceAdapter", "MantleMETHAdapter", "OndoUSDYAdapter",
    "SyntheticAsset", "AgentToken", "AgentVault", "FounderVault",
    "MockERC20",
]

if not FORGE_OUT.exists():
    sys.exit(
        f"ERROR: forge output not at {FORGE_OUT.resolve()}. "
        f"Run `forge build` in contracts repo first."
    )

DEST.mkdir(parents=True, exist_ok=True)
for name in CONTRACTS:
    src = FORGE_OUT / f"{name}.sol" / f"{name}.json"
    if not src.exists():
        sys.exit(f"ERROR: {src} not found")
    with open(src) as f:
        artifact = json.load(f)
    abi = artifact.get("abi")
    if not abi:
        sys.exit(f"ERROR: no abi in {src}")
    with open(DEST / f"{name}.json", "w") as f:
        json.dump(abi, f, indent=2)
    print(f"  {name} → {DEST / f'{name}.json'}")

print(f"\nExtracted {len(CONTRACTS)} ABIs.")
