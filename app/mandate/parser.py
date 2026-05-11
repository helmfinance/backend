"""Claude Sonnet 4.6 mandate parser.

Uses Anthropic tool-use with `tool_choice={"type": "tool", "name": "set_mandate"}`
to force structured JSON output that matches MandateSchema.
"""

from __future__ import annotations

from anthropic import Anthropic

from app.config import settings
from app.schemas import MandateSchema

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2000
TIMEOUT_SECONDS = 30

SYSTEM_PROMPT = """You are a mandate parser for Helm, an AI Agent ETF protocol on Mantle.

Convert the user's natural language fund description into a structured MandateSchema by calling the `set_mandate` tool.

CONSTRAINTS (enforce regardless of user input):
- carryBps must always be 1000 (10%, protocol-locked)
- maxLeverage must always be 1.0 (v1 disallows leverage)
- targetUniverse items must be from: sNVDA, sSPY, sAAPL, sMSFT, sTSLA, mETH, USDY
- assetClasses: equity, crypto, treasury, cash, mixed
- allowedLockups items: instant, 30d, 60d, 90d
- All bps values are 0-10000
- founderShareBps: 500-3000 (5%-30%)
- founderLockupDays: >= 90
- subordinationThresholdBps: 2000-7000 (20%-70%)
- maxSinglePositionBps: <= 7000
- minimumDepositUsdc: at least "10000000" (10 USDC, 6-decimal string)
- description: 10-500 chars, name: 3-50 chars, ticker: 2-8 uppercase alnum

DEFAULT INFERENCE (when user doesn't specify):
- rebalanceFrequency: "weekly"
- founderLockupDays: 180 (6 months)
- subordinationThresholdBps: 5000 (50%)
- maxSinglePositionBps: based on universe size, default 2500
- weightConstraints: derive from user-stated targets, with ±200bps band

Use the set_mandate tool with the parsed mandate. Always include all required fields."""


def _client() -> Anthropic:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    return Anthropic(api_key=settings.anthropic_api_key, timeout=TIMEOUT_SECONDS)


def parse_mandate(natural_language: str, hints: dict | None = None) -> MandateSchema:
    """Calls Claude with tool-use to enforce MandateSchema output.

    Raises ValueError if tool not called or schema validation fails.
    Raises anthropic.* on transport errors (caller maps to 503).
    """
    client = _client()

    user_text = natural_language
    if hints:
        user_text += f"\n\nUser hints (apply where consistent): {hints}"

    schema = MandateSchema.model_json_schema()

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_text}],
        tools=[
            {
                "name": "set_mandate",
                "description": "Submit the parsed mandate fields.",
                "input_schema": schema,
            }
        ],
        tool_choice={"type": "tool", "name": "set_mandate"},
    )

    tool_block = next(
        (b for b in response.content if getattr(b, "type", None) == "tool_use"),
        None,
    )
    if tool_block is None:
        raise ValueError("Claude did not call set_mandate tool")

    return MandateSchema.model_validate(tool_block.input)
