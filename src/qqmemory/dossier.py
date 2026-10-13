"""Source-linked consolidation, without inventing personality or relationships."""

import json
from qqbot.memory.policy import preference_slot
from qqbot.memory.applicability import applicable_profile


def consolidate(person, claims, experiences, interactions):
    current, plans, behavior, names, relationships = [], [], {}, {}, []
    for row in sorted(claims, key=lambda r: r["latest"] or 0, reverse=True):
        n = row["node"]
        sources = list(dict.fromkeys(p["id"] for p in row["proof"] if p.get("id")))
        if not sources or not n.get("evidence"):
            continue
        item = {
            "id": n["id"],
            "key": n["key"],
            "text": n["text"],
            "evidence": n["evidence"],
            "source": n.get("source"),
            "sources": sources[:4],
            "observations": len(sources),
            "recorded_at": row["latest"],
        }
        if n["key"] in ("name", "preferred_name"):
            names.setdefault(n["key"], {**item, "value": n["value"]})
        elif n["key"] == "relationship":
            if len(relationships) < 3:
                relationships.append({**item, "perspective": "user_statement"})
        elif n["kind"] == "task":
            if len(plans) < 3:
                plans.append(item)
        elif n["kind"] == "preference":
            slot = n.get("behavior_slot") or preference_slot(n["value"] + " " + n["evidence"])
            if slot:
                behavior.setdefault(slot, {**item, "slot": slot})
            elif len(current) < 6:
                current.append(item)
        elif len(current) < 6:
            current.append(item)
    return {
        "version": 2,
        "platform_name": (person or {}).get("display_name", ""),
        "names": list(names.values()),
        "behavior_preferences": list(behavior.values()),
        "current_facts": current,
        "ongoing_plans": plans,
        "shared_experiences": experiences,
        "successful_interactions": interactions,
        "relationship_statements": relationships,
    }


def attended_profile(profile, mode, query=None):
    if not profile:
        return {}
    if query is not None:
        profile = applicable_profile(profile, query)
    result = {
        k: profile[k]
        for k in ("version", "platform_name", "successful_interactions", "audience", "identity")
        if k in profile
    }
    budget = 1800 if mode == "recall" else 1000
    keys = ["names", "behavior_preferences"]
    if mode == "recall":
        keys += ["current_facts", "ongoing_plans", "relationship_statements", "shared_experiences"]
    for key in keys:
        result[key] = []
        items = profile.get(key, [])
        if key == "names":
            items = sorted(items, key=lambda i: i["key"] != "preferred_name")
        for item in items:
            compact = {
                k: v
                for k, v in item.items()
                if k
                in (
                    "key",
                    "value",
                    "text",
                    "evidence",
                    "source",
                    "recorded_at",
                    "slot",
                    "observations",
                    "topic",
                    "episodes",
                )
            }
            if key == "names":
                compact.pop("text", None)
            if key == "shared_experiences":
                compact["historical"] = True
            result[key].append(compact)
            if len(json.dumps(result, ensure_ascii=False)) > budget:
                result[key].pop()
    return result
