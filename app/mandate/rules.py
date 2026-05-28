"""Mandate business validation rules + protocol-locked field overrides.

Shared by /mandate/parse and (post-MVP) /mandate/validate.
"""

from __future__ import annotations

from app.schemas import MandateSchema

PROTOCOL_LOCKED_CARRY_BPS = 1000
PROTOCOL_LOCKED_MAX_LEVERAGE = 1.0
CANONICAL_ASSETS = {"sNVDA", "sSPY", "sAAPL", "sMSFT", "sTSLA", "mETH", "USDY"}
ALLOWED_REBALANCE_FREQ = {"daily", "weekly", "monthly", "event-driven"}

# Mapping for LLM weight-class → individual symbols. Used when the parser
# returns weightConstraints keyed by asset *class* ("equity", "treasury", …)
# instead of individual ticker symbols. We split the class band evenly
# across symbols of that class that also appear in targetUniverse.
ASSET_CLASS_TO_SYMBOLS = {
    "equity":   {"sNVDA", "sSPY", "sAAPL", "sMSFT", "sTSLA"},
    "crypto":   {"mETH"},
    "treasury": {"USDY"},
    "cash":     set(),  # cash = USDC, not a tradeable asset
    "mixed":    {"sNVDA", "sSPY", "sAAPL", "sMSFT", "sTSLA", "mETH", "USDY"},
}


class MandateValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def validate_and_normalize(
    mandate: MandateSchema,
) -> tuple[MandateSchema, list[str]]:
    """Apply protocol-locked overrides, run all validation rules.

    Returns (normalized_mandate, warnings).
    Raises MandateValidationError if hard rules violated.
    """
    warnings: list[str] = []
    errors: list[str] = []

    # 0. Expand class-keyed weightConstraints to per-symbol entries.
    # The LLM sometimes outputs `{"asset": "equity", minBps: 6000, maxBps: 8000}`
    # — but the on-chain registerAgent only accepts ticker symbols. Split the
    # class band evenly across symbols in targetUniverse that belong to that
    # class (drops the class entry and adds N symbol entries).
    if mandate.weight_constraints:
        from app.schemas import WeightConstraint
        universe = set(mandate.target_universe or [])
        expanded: list[WeightConstraint] = []
        for c in mandate.weight_constraints:
            if c.asset in universe:
                expanded.append(c)
                continue
            class_syms = ASSET_CLASS_TO_SYMBOLS.get(c.asset, set()) & universe
            if not class_syms:
                warnings.append(
                    f"Dropping weight constraint for unknown asset '{c.asset}'"
                )
                continue
            n = len(class_syms)
            # Split the band evenly across each symbol, with a ±100bps cushion
            # so floor-division doesn't push us below sum-min.
            per_min = max(0, c.min_bps // n - 100)
            per_max = min(10000, (c.max_bps + n - 1) // n + 100)
            for s in sorted(class_syms):
                expanded.append(WeightConstraint(
                    asset=s, min_bps=per_min, max_bps=per_max,
                ))
            warnings.append(
                f"Expanded class '{c.asset}' weight {c.min_bps}-{c.max_bps}bps "
                f"into {n} symbols at {per_min}-{per_max}bps each."
            )
        mandate = mandate.model_copy(update={"weight_constraints": expanded})

    # 1. Protocol-locked overrides
    if mandate.carry_bps != PROTOCOL_LOCKED_CARRY_BPS:
        warnings.append(
            f"Carry was specified as {mandate.carry_bps / 100}% but is "
            f"protocol-locked at {PROTOCOL_LOCKED_CARRY_BPS / 100}%."
        )
        mandate = mandate.model_copy(update={"carry_bps": PROTOCOL_LOCKED_CARRY_BPS})

    if mandate.max_leverage != PROTOCOL_LOCKED_MAX_LEVERAGE:
        warnings.append(
            f"Leverage was specified as {mandate.max_leverage}x but v1 "
            f"protocol does not allow leverage; locked to {PROTOCOL_LOCKED_MAX_LEVERAGE}."
        )
        mandate = mandate.model_copy(update={"max_leverage": PROTOCOL_LOCKED_MAX_LEVERAGE})

    # 2. Hard validation rules
    if mandate.version != "1.0":
        errors.append(f"version must be '1.0', got '{mandate.version}'")

    if not (3 <= len(mandate.name) <= 50):
        errors.append("name length must be 3..50")
    if (
        not (2 <= len(mandate.ticker) <= 8)
        or not mandate.ticker.replace("_", "").isalnum()
        or not mandate.ticker.isupper()
    ):
        errors.append("ticker must be 2..8 uppercase alphanumeric")
    if not (10 <= len(mandate.description) <= 500):
        errors.append("description length must be 10..500")

    if not mandate.asset_classes:
        errors.append("assetClasses must not be empty")
    if not mandate.target_universe:
        errors.append("targetUniverse must not be empty")
    invalid_assets = [a for a in mandate.target_universe if a not in CANONICAL_ASSETS]
    if invalid_assets:
        errors.append(
            f"unknown assets: {invalid_assets}; allowed: {sorted(CANONICAL_ASSETS)}"
        )

    if mandate.weight_constraints:
        sum_min = sum(c.min_bps for c in mandate.weight_constraints)
        if sum_min > 10000:
            errors.append(f"sum of weightConstraints.minBps ({sum_min}) > 10000")
        for c in mandate.weight_constraints:
            if c.min_bps > c.max_bps:
                errors.append(
                    f"{c.asset}: minBps ({c.min_bps}) > maxBps ({c.max_bps})"
                )

    if mandate.rebalance_frequency not in ALLOWED_REBALANCE_FREQ:
        errors.append(f"rebalanceFrequency must be one of {ALLOWED_REBALANCE_FREQ}")

    if not mandate.allowed_lockups:
        errors.append("allowedLockups must not be empty")

    try:
        min_dep = int(mandate.minimum_deposit_usdc)
    except (TypeError, ValueError):
        errors.append("minimumDepositUsdc must be a decimal string")
        min_dep = None
    if min_dep is not None and min_dep < 10_000_000:
        errors.append("minimumDepositUsdc must be >= 10 USDC (10000000)")

    if not (500 <= mandate.founder_share_bps <= 3000):
        errors.append("founderShareBps must be in [500, 3000]")

    if mandate.founder_lockup_days < 90:
        errors.append("founderLockupDays must be >= 90")

    if not (2000 <= mandate.subordination_threshold_bps <= 7000):
        errors.append("subordinationThresholdBps must be in [2000, 7000]")

    if mandate.max_single_position_bps > 7000:
        errors.append("maxSinglePositionBps must be <= 7000")

    if errors:
        raise MandateValidationError(errors)

    return mandate, warnings
