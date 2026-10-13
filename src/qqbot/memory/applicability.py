"""Pure request-time applicability, shared by graph recall and the QQ prompt boundary.

Audience filtering establishes who may see a record. These rules separately
establish whether it belongs in this request; a ranking score cannot waive them.
Stored records stay intact, including legacy episodes and conditional preferences.
"""

import re
from .policy import attention, preference_slot


def greeting_requested(query):
    compact = re.sub(r"[\s，。！？!?,.~～…]+", "", query).casefold()
    return compact in {
        "你好",
        "您好",
        "早",
        "早上好",
        "早安",
        "中午好",
        "下午好",
        "晚上好",
        "晚安",
        "hello",
        "hi",
        "打个招呼",
        "来打个招呼",
        "打招呼",
        "和我打个招呼",
    }


def memory_applies(item, query):
    """Old episodes need an explicit recall/continuation request; greetings need an occasion."""
    mode = attention(query)
    if item.get("kind") == "episode":
        return mode == "recall"
    if item.get("kind") and item["kind"] != "preference" and item.get("key") != "preference":
        return True
    slot = (
        item.get("slot")
        or item.get("behavior_slot")
        or preference_slot(" ".join(str(item.get(k, "")) for k in ("value", "text", "evidence")))
    )
    if slot == "greeting":
        return greeting_requested(query) or (
            mode == "recall" and bool(re.search(r"招呼|问候|表达|偏好|约定", query))
        )
    return True


def applicable_profile(profile, query):
    """Return a projection, never mutate a stored or shared dossier."""
    result = dict(profile)
    result["behavior_preferences"] = [
        item for item in profile.get("behavior_preferences", []) if memory_applies(item, query)
    ]
    # Legacy aggregate summaries omit speaker provenance. Explicit recall uses
    # individual source-bound episodes instead; status/admin views stay intact.
    result.pop("shared_experiences", None)
    return result


def presented_memory(item):
    """Present an episode as a past message, not as a verified personal experience."""
    result = dict(item)
    if item.get("kind") == "episode":
        excerpt = item.get("user_excerpt") or item.get("question") or ""
        result["source_role"] = "user" if excerpt else "unverified_summary"
        result["attribution"] = "dialogue_not_verified_experience"
        result["historical"] = True
        if excerpt:
            result["text"] = excerpt[:240]
            result["source_context"] = (
                "scenario_or_hypothesis"
                if re.search(r"评价|假设|假如|如果|虚构|场景|设想|笑话|故事", excerpt)
                else "user_message"
            )
    return result
