from sqlalchemy.orm import Session

from app.db import models


def handle_reputation_slashed(db: Session, event):
    args = event["args"]
    agent_id = args["agentId"]
    a = db.get(models.Agent, agent_id)
    if a:
        a.reputation = args["after"]


def handle_token_uri_set(db: Session, event):
    """No-op for this task — narrator note flow updates URI separately."""
    return
