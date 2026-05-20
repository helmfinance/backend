"""Weekly narrator note generator.

Pulls an agent's decisions + NAV change for the current ISO week, hands them
to an LLM with the agent's personality from its mandate, and upserts the
resulting markdown into `narrator_notes`.

`_call_llm` is split out so tests / `scripts/generate_notes.py --mock` can
substitute a canned response. (Migrated from Anthropic to OpenAI 2026-05.)
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from openai import OpenAI
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import models

MODEL = settings.openai_narrator_model
MAX_TOKENS = 1500
TIMEOUT = 30

SYSTEM_PROMPT = """You are an AI fund manager writing this week's note to your token holders.
Your note appears on the agent's marketplace page and in its NFT metadata.
Holders read it to understand what you did this week and why.

VOICE:
- First person ("I trimmed", "I decided", "I'm watching")
- Match the agent's personality (you'll be given its description)
- Be specific: cite assets, percentages, reasoning
- Acknowledge wins AND losses honestly — never cheerleading
- ~3-5 short paragraphs, markdown formatted
- End with a brief "Outlook" for next week
- Avoid finance clichés ("market conditions", "monitoring closely", "headwinds")
- Write like a sharp human PM, not a press release

DO NOT include:
- Week dates (system tags those separately)
- Disclaimers or hedge language
- Generic "thank you" sign-offs
"""

PERSONALITY_TONE = {
    "growth-aggressive":
        "Confident, conviction-driven. Use action words. Decisive.",
    "conservative-stable":
        "Measured, analytical. Risk-first framing. Cautious.",
    "balanced-moderate":
        "Even-handed, pragmatic. Equal weight to risks and opportunities.",
    "contrarian-value":
        "Skeptical of consensus. Identify mispricings. Patient.",
    "yield-focused":
        "Income-focused. Emphasize distributions and stability over price action.",
}


def _build_system_prompt(agent: models.Agent) -> str:
    mandate = agent.mandate or {}
    personality = (
        mandate.get("personalityHint") or mandate.get("personality_hint")
    )
    if not personality:
        return SYSTEM_PROMPT
    tone = PERSONALITY_TONE.get(
        personality,
        f"Personality: {personality}. Adapt tone accordingly.",
    )
    return f"{SYSTEM_PROMPT}\n\nPersonality: {personality}\nTone guidance: {tone}"


def iso_week_start_utc(now: int) -> int:
    d = dt.datetime.fromtimestamp(now, tz=dt.UTC)
    monday = (d - dt.timedelta(days=d.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int(monday.timestamp())


def _format_decisions(decisions: list[models.Decision]) -> str:
    if not decisions:
        return "(no on-chain actions this week)"
    lines = []
    for d in decisions:
        day = dt.datetime.fromtimestamp(d.timestamp, tz=dt.UTC).strftime("%a")
        lines.append(f"- [{day}] {d.type}: {d.summary}")
    return "\n".join(lines)


def _format_positions(positions: list[models.Position]) -> str:
    if not positions:
        return "(no positions)"
    return ", ".join(f"{p.symbol} {p.weight_bps / 100:.1f}%" for p in positions)


def _build_user_message(
    agent: models.Agent,
    decisions: list[models.Decision],
    nav_start: int,
    nav_end: int,
    return_bps: int,
) -> str:
    description = agent.mandate.get("description", "(no description)")
    return f"""AGENT IDENTITY
Name: {agent.name}
Ticker: {agent.ticker}
Personality: {description}

THIS WEEK
NAV per share: {nav_start / 1_000_000:.6f} → {nav_end / 1_000_000:.6f} USDC ({return_bps / 100:+.2f}%)
Current positions: {_format_positions(agent.positions)}

Decisions made this week:
{_format_decisions(decisions)}

Write the note now."""


def _call_llm(system_prompt: str, user_message: str) -> str:
    """Real OpenAI call. Mockable for tests / scripts."""
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    client = OpenAI(api_key=settings.openai_api_key, timeout=TIMEOUT)
    response = client.chat.completions.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    )
    content = response.choices[0].message.content or ""
    return content.strip()


def generate_note(
    db: Session,
    agent_id: int,
    *,
    now: int | None = None,
    llm: Callable[[str, str], str] | None = None,
) -> models.NarratorNote | None:
    """Generate (or regenerate) this week's note for an agent.

    Returns None if the agent does not exist, has no NAV history, or has had
    no activity to narrate (no decisions + zero NAV change).
    Pass `llm=mock_fn` to bypass the OpenAI call.
    """
    if now is None:
        now = int(dt.datetime.now(dt.UTC).timestamp())
    if llm is None:
        llm = _call_llm

    agent = db.get(models.Agent, agent_id)
    if agent is None:
        return None

    week_start = iso_week_start_utc(now)
    week_end = now

    decisions = sorted(
        (d for d in agent.decisions if week_start <= d.timestamp < week_end),
        key=lambda d: d.timestamp,
    )

    nav_rows = sorted(agent.nav_history, key=lambda n: n.timestamp)
    if not nav_rows:
        return None

    nav_start_row = None
    for n in nav_rows:
        if n.timestamp <= week_start:
            nav_start_row = n
        else:
            break
    if nav_start_row is None:
        nav_start_row = nav_rows[0]
    nav_end_row = nav_rows[-1]

    nav_start = int(nav_start_row.nav_per_share_usdc) or 1_000_000
    nav_end = int(nav_end_row.nav_per_share_usdc)

    return_bps = round(((nav_end / nav_start) - 1) * 10000)

    if not decisions and return_bps == 0:
        return None

    user_message = _build_user_message(
        agent, decisions, nav_start, nav_end, return_bps
    )
    body_markdown = llm(_build_system_prompt(agent), user_message)

    existing = db.execute(
        select(models.NarratorNote).where(
            models.NarratorNote.agent_id == agent_id,
            models.NarratorNote.week_start == week_start,
        )
    ).scalar_one_or_none()

    if existing:
        existing.week_end = week_end
        existing.generated_at = now
        existing.body_markdown = body_markdown
        existing.nav_start = str(nav_start)
        existing.nav_end = str(nav_end)
        existing.return_bps = return_bps
        note = existing
    else:
        note = models.NarratorNote(
            agent_id=agent_id,
            week_start=week_start,
            week_end=week_end,
            generated_at=now,
            body_markdown=body_markdown,
            nav_start=str(nav_start),
            nav_end=str(nav_end),
            return_bps=return_bps,
        )
        db.add(note)

    db.commit()
    db.refresh(note)
    return note
