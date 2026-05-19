import json
from pathlib import Path

ABI_DIR = Path(__file__).parent / "abis"


def load_abi(name: str) -> list:
    path = ABI_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"ABI not found: {path}. Run `python -m scripts.extract_abis`."
        )
    with open(path) as f:
        return json.load(f)
