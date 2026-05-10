import re

ZERO_ADDR = "0x0000000000000000000000000000000000000000"

_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def addr_or_zero(value: str | None) -> str:
    """Empty/None or non-EVM-address (e.g. ``0x...`` placeholder) → ZERO_ADDR."""
    if value and _ADDR_RE.match(value):
        return value
    return ZERO_ADDR
