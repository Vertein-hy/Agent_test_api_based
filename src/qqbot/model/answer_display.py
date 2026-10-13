"""QQ presentation policy; evidence remains in tool audit records."""

import re
import json

MEMORY_MARKER = re.compile(r"\bMEMORY_DATA_JSON\b")
MEMORY_OBJECT = re.compile(r"\"speaker_ref\"\s*:.*?\"(?:profile|memories)\"\s*:", re.S)


def internal_memory_present(text):
    return bool(MEMORY_MARKER.search(text) or MEMORY_OBJECT.search(text))


def without_internal_memory(text):
    """Remove complete memory envelopes, never arbitrary JSON or user code.

    Incomplete/unlabelled internal data fails closed instead of guessing where
    private context ends and the intended answer begins.
    """
    for _ in range(4):
        marker = MEMORY_MARKER.search(text)
        if not marker:
            if MEMORY_OBJECT.search(text):
                raise ValueError("Unlabelled internal memory output")
            return text.strip()
        tail = text[marker.end() :]
        heading = re.match(r"(?:[（(][^\n）)]{0,80}[）)])?\s*[:：]?\s*", tail)
        offset = marker.end() + heading.end()
        data, length = json.JSONDecoder().raw_decode(text[offset:])
        if not isinstance(data, dict) or not {"speaker_ref", "profile", "memories"} <= data.keys():
            raise ValueError("Invalid internal memory output")
        prefix, suffix = text[: marker.start()], text[offset + length :]
        # A provider may put the envelope in a fenced block.
        opening = re.search(r"```(?:json|text)?\s*$", prefix)
        closing = re.match(r"\s*```", suffix)
        if opening and closing:
            prefix, suffix = prefix[: opening.start()], suffix[closing.end() :]
        text = prefix + suffix
    raise ValueError("Repeated internal memory output")


def source_requested(question):
    terms = r"来源|出处|引用|参考资料|原文|链接|网址|下载地址|官网|source|url"
    if re.search(
        r"(?:不要|不用|无需|别|不必).{0,8}(?:"
        + terms
        + r")|(?:"
        + terms
        + r").{0,5}(?:不要|不用|不必)",
        question,
        re.I,
    ):
        return False
    return bool(re.search(terms, question, re.I))


def without_search_footer(text):
    # Remove only the automatic trailing source list; preserve answer/code URLs.
    return re.sub(
        r"\n{1,2}搜索资料[：:]\s*\n(?:https?://[^\s]+\s*\n?){1,10}\s*$", "", text
    ).rstrip()
