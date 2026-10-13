"""Semantic seeding, bounded evidence graph and frozen neural/feedback ranking."""

import json
import time
from collections import defaultdict
import numpy as np
from qqbot.memory.policy import preference_slot, attention
from qqbot.memory.applicability import memory_applies, presented_memory
from .attention_model import AttentionLearner, features as attention_features


def rank(service, nodes, edges, q, query, actor=None, attention_state=None):
    # Apply hard request boundaries before semantic/neural ranking and evidence expansion.
    nodes = [n for n in nodes if memory_applies(n, query)]
    if not nodes:
        return []
    # Contradictory response preferences must not compete with their newer version.
    latest = {}
    for n in nodes:
        slot = (
            (n.get("behavior_slot") or preference_slot(n["text"] + " " + n.get("evidence", "")))
            if n["kind"] == "preference"
            else ""
        )
        if slot and (
            slot not in latest
            or n.get("last_seen", n["created"])
            > latest[slot].get("last_seen", latest[slot]["created"])
        ):
            latest[slot] = n
    nodes = [
        n
        for n in nodes
        if n["kind"] != "preference"
        or not (
            slot := (
                n.get("behavior_slot") or preference_slot(n["text"] + " " + n.get("evidence", ""))
            )
        )
        or latest[slot]["id"] == n["id"]
    ]
    ids = {n["id"] for n in nodes}
    edges = [e for e in edges if e["src"] in ids and e["dst"] in ids]
    vectors = np.array([n["vector"] for n in nodes], dtype="float32")
    similarity = vectors @ q
    # Seed a bounded subgraph, then include evidence neighbors for association.
    seed = set(np.argsort(similarity)[-30:].tolist())
    lookup = {n["id"]: i for i, n in enumerate(nodes)}
    selected = set(seed)
    for _ in range(2):
        frontier = set(selected)
        for e in edges:
            a, b = lookup[e["src"]], lookup[e["dst"]]
            if (a in frontier and nodes[a]["kind"] != "person") or (
                b in frontier and nodes[b]["kind"] != "person"
            ):
                if len(selected) < 160:
                    selected.update((a, b))
    indices = sorted(selected)
    sub = [nodes[i] for i in indices]
    neural = service.ranker.score(sub, edges, vectors[indices], q, time.time())
    probabilities = 1 / (1 + np.exp(-np.clip(neural, -30, 30)))
    candidates = []
    wants_recall = any(
        t in query for t in ("记得", "记住", "上次", "之前", "继续", "我是谁", "回忆")
    )
    threshold = 0.25 if wants_recall else 0.40
    # A relevant self-reported fact can activate its source episode even when
    # that episode's summary does not repeat the query words.
    evidence_links = defaultdict(list)
    for edge in edges:
        if edge["relation"] != 2:
            continue
        a, b = lookup[edge["src"]], lookup[edge["dst"]]
        if nodes[a]["kind"] in ("fact", "preference", "task") and similarity[a] >= threshold:
            evidence_links[b].append(a)
    for j, i in enumerate(indices):
        n = nodes[i]
        if n["kind"] not in ("episode", "fact", "preference", "task"):
            continue
        linked = evidence_links.get(i, []) if n["kind"] == "episode" else []
        activated = max([float(similarity[i])] + [float(similarity[a]) * 0.9 for a in linked])
        if activated < threshold:
            continue
        f = attention_features(n, activated, probabilities[j], attention(query))
        adjustment = AttentionLearner.adjust_state(attention_state, f) if attention_state else 0
        score = float(
            0.75 * activated + 0.25 * probabilities[j] + adjustment + 0.03 * n.get("retention", 1)
        )
        candidates.append((score, n, f))
    mode = attention(query)
    maximum, budget, episode_limit = (7, 2200, 3) if mode == "recall" else (3, 1100, 1)
    result, episodes = [], 0
    for score, n, f in sorted(candidates, key=lambda x: x[0], reverse=True):
        if len(result) >= maximum:
            break
        if n["kind"] == "episode":
            if episodes >= episode_limit:
                continue
            episodes += 1
        item = {
            "id": n["id"],
            "kind": n["kind"],
            "text": n["text"],
            "recorded_at": n["created"],
            "source": n.get("source"),
            "score": round(score, 4),
        }
        if n.get("evidence"):
            item["evidence"] = n["evidence"]
        if n["kind"] == "episode":
            item["historical"] = True
            item["user_excerpt"] = n.get("question", "")[:240]
            # Legacy summaries may contain assistant imagination or turn a quoted
            # scenario into a personal event. Expose the original speaker message
            # when available; its contents still are not verified lived experience.
            item = presented_memory(item)
            item["associated_via"] = [
                nodes[a]["id"] for a in evidence_links.get(lookup[n["id"]], [])
            ][:4]
        size = len(json.dumps(item, ensure_ascii=False))
        if size <= budget:
            if actor:
                item["_attention_features"] = f
            result.append(item)
            budget -= size
    return result
