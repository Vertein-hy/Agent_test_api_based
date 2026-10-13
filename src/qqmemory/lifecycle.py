"""Recall strength: no deletion, no resurrection of corrected facts."""

import math
import time


def strength(node, now=None):
    now = time.time() if now is None else now
    if node.get("pinned") or node.get("key") in ("name", "preferred_name", "language"):
        return 1.0
    if node.get("kind") == "task" and node.get("active", True):
        return 1.0
    observations = max(1, node.get("observations", 1))
    # Reading alone adds only a bounded boost; explicit usefulness and repetition consolidate.
    boost = 1 + min(math.log1p(node.get("access", 0)), 3) * 0.15 + min(observations - 1, 8) * 0.3
    boost += min(max(node.get("useful", 0), 0), 5) * 0.3
    half_life = {"episode": 14, "topic": 30, "fact": 90, "preference": 180}.get(
        node.get("kind"), 30
    ) * boost
    age = (
        max(
            0,
            now
            - max(node.get("last_seen", node.get("created", now)), node.get("reinforced_at", 0)),
        )
        / 86400
    )
    return math.exp(-math.log(2) * age / half_life)


def visible(nodes, mode, now=None):
    return [
        dict(n, retention=round(strength(n, now), 5))
        for n in nodes
        if n.get("active", True)
        and not n.get("invalidated")
        and (mode == "recall" or strength(n, now) >= 0.15 or n.get("kind") == "person")
    ]
