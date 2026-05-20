"""OpenAI mandate parser.

Uses OpenAI function calling with `tool_choice={"type":"function","name":"set_mandate"}`
to force structured JSON output that matches MandateSchema. (Migrated from
Anthropic tool-use 2026-05; anthropic SDK remains installed for fallback.)
"""

from __future__ import annotations

import json
from collections.abc import Callable

from openai import OpenAI

from app.config import settings
from app.schemas import MandateSchema

MODEL = settings.openai_mandate_model
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

MARKETPLACE FLAVOR (you MUST populate these two fields):

- expectedYieldAPY: a short string estimating the annual yield this strategy
  produces. Format: "X-Y% APY ({brief reasoning})".
  Base estimate on the asset mix:
  - USDY heavy (>30% target weight) → 3-5% APY (T-bill rates)
  - mETH heavy → 3-4% APY from staking
  - synthetic equity heavy → "0.5% APY (capital gain focused, low yield)"
  - mix → blended estimate
  Examples: "4-5% APY (USDY + mETH staking)", "0.5% APY (capital gain focused)"

- personalityHint: a 1-2 word hyphenated descriptor reflecting the agent's
  strategy character. Prefer one of these exact values:
  - "growth-aggressive": high equity, willing to take risk
  - "conservative-stable": yield-heavy, low volatility
  - "balanced-moderate": mixed asset classes
  - "contrarian-value": defensive, counter-trend
  - "yield-focused": prioritizes USDC dividend over capital gain
  If none fit, invent a similar 1-2 word hyphenated descriptor.

EMERGENCY EXIT CONDITIONS (machine-evaluable DSL preferred):

For emergencyExitConditions, extract entries in this DSL whenever possible:
  <METRIC> <OP> <VALUE>
Supported metrics: BTC_PRICE, ETH_PRICE, BTC_FUNDING_RATE,
BTC_24H_CHANGE, ETH_24H_CHANGE, DRAWDOWN
Operators: >, <, >=, <=, ==

Examples:
- "Panic exit if BTC drops 10% in a day" → "BTC_24H_CHANGE < -0.10"
- "Hedge when funding rate spikes"        → "BTC_FUNDING_RATE > 0.001"
- "Stop loss at 25% drawdown"             → "DRAWDOWN > 0.25"

If a condition cannot fit the DSL, keep it as free text — it will be logged
but not auto-evaluated.

Use the set_mandate tool with the parsed mandate. Always include all required fields."""


def _client() -> OpenAI:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    return OpenAI(api_key=settings.openai_api_key, timeout=TIMEOUT_SECONDS)


def parse_mandate(
    natural_language: str,
    hints: dict | None = None,
    *,
    llm: Callable[[str, str, dict], dict] | None = None,
) -> MandateSchema:
    """Calls OpenAI with function calling to enforce MandateSchema output.

    ``llm`` injection (matching narrator's pattern) takes
    ``(system_prompt, user_message, tool_spec)`` and returns the parsed
    tool-call arguments dict. Caller validates against MandateSchema.

    Raises ValueError if tool not called or schema validation fails.
    Raises openai.* on transport errors (caller maps to 503).
    """
    user_text = natural_language
    if hints:
        user_text += f"\n\nUser hints (apply where consistent): {hints}"

    tool_spec = {
        "type": "function",
        "function": {
            "name": "set_mandate",
            "description": "Submit the parsed mandate fields.",
            "parameters": MandateSchema.model_json_schema(),
        },
    }

    if llm is not None:
        args = llm(SYSTEM_PROMPT, user_text, tool_spec)
    else:
        client = _client()
        response = client.chat.completions.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            tools=[tool_spec],
            tool_choice={"type": "function", "function": {"name": "set_mandate"}},
        )
        msg = response.choices[0].message
        if not msg.tool_calls:
            raise ValueError("OpenAI did not call set_mandate tool")
        args = json.loads(msg.tool_calls[0].function.arguments)

    return MandateSchema.model_validate(args)
