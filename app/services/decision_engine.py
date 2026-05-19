from app.db.models import Agent


def compute_target_weights(agent: Agent) -> list[tuple[str, int]]:
    """Returns [(symbol, weight_bps), ...] summing to <= 10000.

    Algorithm: midpoint of each weightConstraint, scaled down if sum > 10000.
    """
    constraints = agent.mandate.get("weightConstraints", [])
    if not constraints:
        return []
    pairs = []
    total = 0
    for c in constraints:
        mid = (c["minBps"] + c["maxBps"]) // 2
        pairs.append((c["asset"], mid))
        total += mid
    if total > 10000:
        pairs = [(s, w * 10000 // total) for s, w in pairs]
    return pairs
