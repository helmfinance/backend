import time

from sqlalchemy.orm import Session

from app.db import models


def handle_agent_registered(db: Session, event):
    """event.args: agentId, founder, deployment (struct with vault/token/founderVault addresses).

    Exact struct key names are inferred from the ABI; fall back to positional access.
    """
    args = event["args"]
    agent_id = args["agentId"]
    if db.get(models.Agent, agent_id):
        return  # idempotent

    dep = args["deployment"]
    vault_addr = dep["vault"] if "vault" in dep else dep[0]
    token_addr = dep["token"] if "token" in dep else dep[1]
    fv_addr = dep["founderVault"] if "founderVault" in dep else dep[2]

    # mandate body is not on chain; only hash + URI. Reverse-resolve via mandate_blobs.
    mandate_hash = args.get("mandateHash") or ""
    mandate_uri = args.get("mandateURI") or ""

    blob = db.get(models.MandateBlob, mandate_hash) if mandate_hash else None
    mandate_dict = blob.mandate_json if blob else {}

    now = int(time.time())
    db.add(models.Agent(
        agent_id=agent_id,
        name=mandate_dict.get("name", f"Agent #{agent_id}"),
        ticker=mandate_dict.get("ticker", "AGT"),
        founder_address=args["founder"].lower(),
        vault_address=vault_addr.lower(),
        token_address=token_addr.lower(),
        founder_vault_address=fv_addr.lower(),
        phase="Incubation",
        incubation_start=now,
        public_launch_at=None,
        mandate=mandate_dict,
        mandate_uri=mandate_uri,
        mandate_hash=mandate_hash,
        reputation=10000,
        created_at=now,
    ))


def handle_phase_advanced(db: Session, event):
    args = event["args"]
    agent_id = args["agentId"]
    new_phase = _decode_phase(args["to"])
    a = db.get(models.Agent, agent_id)
    if a:
        a.phase = new_phase
        if new_phase == "PublicLaunch" and a.public_launch_at is None:
            a.public_launch_at = int(time.time())


def handle_agent_slashed(db: Session, event):
    # Reputation is updated by ReputationSlashed; phase by PhaseAdvanced.
    return


def handle_agent_wind_down(db: Session, event):
    args = event["args"]
    agent_id = args["agentId"]
    a = db.get(models.Agent, agent_id)
    if a:
        a.phase = "WindDown"
    # Detailed WindDownState row is created by the vault's WindDownTriggered event handler.


def handle_agent_settled(db: Session, event):
    a = db.get(models.Agent, event["args"]["agentId"])
    if a:
        a.phase = "Settled"


def _decode_phase(enum_value: int) -> str:
    """Registry Phase enum: 0=Incubation, 1=PublicLaunch, 2=WindDown, 3=Slashed, 4=Settled."""
    return ["Incubation", "PublicLaunch", "WindDown", "Slashed", "Settled"][enum_value]
