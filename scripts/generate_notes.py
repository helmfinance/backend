"""Regenerate narrator notes for all agents in the DB.

Usage:
    python -m scripts.generate_notes              # real LLM call
    python -m scripts.generate_notes --mock       # canned text (no LLM)
    python -m scripts.generate_notes --agent 1    # single agent
"""

from __future__ import annotations

import argparse

from app.db import Agent, SessionLocal
from app.narrator.generator import generate_note

MOCK_NOTE = """### Week of operations

A quiet week relative to last — I held tight on the equity legs and let the mETH position do the talking. Yield trickled in as expected; no surprises on the rebalance side.

Looking at the marketplace mood, retail flows into US tech kept buying volume steady. I didn't see drift large enough to fire my 5% trigger, so I left weights alone.

**Outlook**: holding through the earnings window unless one name breaks past the 2.5% upper band."""


def mock_llm(_system: str, _user: str) -> str:
    return MOCK_NOTE


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mock", action="store_true", help="Use canned text instead of LLM")
    p.add_argument("--agent", type=int, default=None, help="Single agent ID")
    args = p.parse_args()

    llm = mock_llm if args.mock else None

    with SessionLocal() as db:
        if args.agent:
            ids = [args.agent]
        else:
            ids = [a.agent_id for a in db.query(Agent).all()]

        for agent_id in ids:
            note = generate_note(db, agent_id, llm=llm)
            if note is None:
                print(f"agent {agent_id}: skipped (no activity / not found)")
            else:
                snippet = note.body_markdown[:80].replace("\n", " ")
                print(
                    f"agent {agent_id}: note id={note.id} "
                    f"({note.return_bps:+d}bps) — {snippet}..."
                )


if __name__ == "__main__":
    main()
