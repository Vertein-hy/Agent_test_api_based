"""Fixed boundaries shared by intake, extraction, and graph persistence."""

import hashlib
import re
import time
from qqbot.addressing import greeting_addressee

HEX = re.compile(r"^[0-9a-f]{64}$")
PANEL = re.compile(
    r"云崽|yunzai|喵喵插件|面板|展柜|(?:原神|UID).{0,60}(?:查询|角色|雷神|甘雨)|UID\s*[:：]?\s*[0-9]{9,10}",
    re.I,
)
SECRET = re.compile(
    r"密码|口令|密钥|令牌|token|api[ _-]?key|cookie|authorization|验证码|银行卡|身份证", re.I
)
UNTRUSTED = re.compile(
    r"转发|引用|网页说|例句|假如|假设|开玩笑|系统提示|忽略.{0,10}(指令|规则|之前)|你是.{0,20}(助手|模型)"
)
KEYS = {
    "name",
    "preferred_name",
    "city",
    "occupation",
    "language",
    "interest",
    "project",
    "preference",
    "goal",
    "todo",
    "commitment",
    "relationship",
}
SINGLE = {"name", "preferred_name", "city", "occupation", "language"}
KINDS = {"episode": 0, "person": 1, "topic": 2, "fact": 3, "preference": 4, "task": 5}
RELATIONS = {
    "PARTICIPATED": 0,
    "ABOUT": 1,
    "SUPPORTED_BY": 2,
    "SUBJECT": 3,
    "SUPERSEDES": 4,
    "ASSOCIATED": 5,
}


def panel(text):
    return bool(PANEL.search(text))


def display_name(value):
    """QQ display metadata is a label, never an identity or instruction."""
    if not isinstance(value, str) or SECRET.search(value) or panel(value):
        return ""
    value = clean(value, 48)
    if any(t in value for t in ("[凭据]", "[号码]", "[邮箱]", "[链接]")):
        return ""
    return " ".join(value.split())


def attention(query):
    compact = re.sub(r"[\s，。！？!?,.~～…]+", "", query).casefold()
    if compact in {
        "你好",
        "您好",
        "早",
        "早上好",
        "早安",
        "中午好",
        "下午好",
        "晚上好",
        "晚安",
        "在吗",
        "在不在",
        "谢谢",
        "谢谢你",
        "好",
        "好的",
        "嗯",
        "嗯嗯",
        "哈哈",
        "哈哈哈",
        "hello",
        "hi",
        "打个招呼",
        "来打个招呼",
        "打招呼",
        "和我打个招呼",
    }:
        return "social"
    if any(
        t in query
        for t in ("记得", "记住", "回忆", "上次", "之前", "我是谁", "我叫什么", "认识我", "继续")
    ):
        return "recall"
    if len(query) > 80 or any(
        t in query
        for t in ("详细", "分析", "解释", "方案", "代码", "教程", "步骤", "论文", "检索", "为什么")
    ):
        return "focused"
    return "chat"


def preference_slot(text):
    if re.search(r"emoji|表情符号|表情包|表情", text, re.I):
        return "emoji_usage"
    if re.search(r"简短|简洁|太长|长篇|字数|一句|两句|长度|展开|详细|精简|啰嗦|啰唆", text):
        return "response_length"
    if re.search(r"口癖", text):
        return "verbal_tic"
    if re.search(r"打招呼|问候|招呼的时候", text):
        return "greeting"
    if re.search(r"口吻|语气|客套|客服|表情|动作|称呼|傲娇|说话风格", text):
        return "response_style"
    return ""


def portable(node):
    return node.get("key") in ("name", "preferred_name", "language") or (
        node.get("key") == "preference"
        and bool(
            node.get("behavior_slot")
            or preference_slot(node.get("value", "") + " " + node.get("evidence", ""))
        )
    )


def checked_update(update, question):
    if not isinstance(update, dict):
        return None
    op, target, evidence = (update.get(k) for k in ("operation", "target", "evidence"))
    if op not in ("retract", "complete", "cancel", "correct", "pin", "unpin"):
        return None
    if not isinstance(target, str) or not 1 <= len(target) <= 160:
        return None
    if not isinstance(evidence, str) or not 4 <= len(evidence) <= 240 or evidence not in question:
        return None
    if SECRET.search(evidence) or UNTRUSTED.search(evidence) or panel(evidence):
        return None
    key = update.get("key", "")
    if key and key not in KEYS:
        return None
    value = clean(update.get("value", ""), 160)
    if op == "correct" and (not value or panel(value)):
        return None
    return {
        "operation": op,
        "target": clean(target, 160),
        "key": key,
        "value": value,
        "evidence": evidence,
    }


def clean(text, limit=2000):
    if not isinstance(text, str):
        raise ValueError("Expected text")
    # Password-bearing lines and credentials never enter encoding or extraction.
    lines = [line for line in text.splitlines() if not SECRET.search(line)]
    text = "\n".join(lines)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b|(?i:bearer)\s+[A-Za-z0-9._~-]{12,}", "[凭据]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[邮箱]", text)
    text = re.sub(r"https?://\S+", "[链接]", text)
    text = re.sub(r"(?<!\d)\d{5,}(?!\d)", "[号码]", text)
    return text.strip()[:limit]


def identity(body):
    if not isinstance(body, dict):
        raise ValueError("Invalid body")
    actor, scope, generation = (body.get(k) for k in ("actor", "scope", "generation"))
    if not isinstance(actor, str) or not HEX.fullmatch(actor):
        raise ValueError("Invalid actor")
    if scope is not None and (not isinstance(scope, str) or not HEX.fullmatch(scope)):
        raise ValueError("Invalid scope")
    if type(generation) is not int or not 0 <= generation < 2**31:
        raise ValueError("Invalid generation")
    return actor, scope, generation


def node_id(actor, scope, kind, key):
    return hashlib.sha256("\0".join((actor, scope, kind, key)).encode()).hexdigest()


def checked_extraction(result, question):
    if not isinstance(result, dict):
        raise ValueError("Invalid extraction")
    claims = result.get("claims", [])
    if not isinstance(claims, list) or len(claims) > 8:
        raise ValueError("Invalid claims")
    summary = clean(result.get("summary", ""), 240)
    topics = result.get("topics", [])
    if not isinstance(topics, list) or len(topics) > 5:
        raise ValueError("Invalid topics")
    topics = [clean(t, 32) for t in topics if isinstance(t, str)]
    topics = list(dict.fromkeys(t for t in topics if len(t) >= 2 and not panel(t)))
    accepted = []
    for c in claims:
        if not isinstance(c, dict):
            continue
        key, value, evidence = c.get("key"), c.get("value"), c.get("evidence")
        if (
            not isinstance(key, str)
            or key not in KEYS
            or not isinstance(value, str)
            or not isinstance(evidence, str)
        ):
            continue
        if not 4 <= len(evidence) <= 240 or evidence not in question:
            continue
        if any(
            f(evidence)
            for f in (panel, lambda t: bool(SECRET.search(t)), lambda t: bool(UNTRUSTED.search(t)))
        ):
            continue
        if type(c.get("temporary")) is not bool or c["temporary"]:
            continue
        value = clean(value, 160)
        if not value or panel(value) or SECRET.search(value) or "[号码]" in value:
            continue
        # A direct greeting names its recipient. An extraction model cannot
        # turn that vocative into the sender's persistent name/preferred name.
        if key in ("name", "preferred_name") and value == greeting_addressee(question):
            continue
        kind = (
            "preference"
            if key == "preference"
            else "task"
            if key in ("goal", "todo", "commitment")
            else "fact"
        )
        accepted.append({"kind": kind, "key": key, "value": value, "evidence": evidence})
    updates = result.get("updates", [])
    if not isinstance(updates, list) or len(updates) > 8:
        raise ValueError("Invalid updates")
    return {
        "summary": summary or question[:160],
        "topics": topics,
        "claims": accepted,
        "updates": [u for row in updates if (u := checked_update(row, question))],
    }


def checked_event(event):
    if not isinstance(event, dict):
        raise ValueError("Invalid event")
    source, created, question, answer = (
        event.get(k) for k in ("id", "created", "question", "answer")
    )
    if not isinstance(source, str) or not HEX.fullmatch(source):
        raise ValueError("Invalid source")
    if type(created) not in (int, float) or not 0 < created <= time.time() + 120:
        raise ValueError("Invalid time")
    if (
        not isinstance(question, str)
        or not isinstance(answer, str)
        or len(question) > 4000
        or len(answer) > 4000
    ):
        raise ValueError("Invalid event text")
    if panel(question) or panel(answer):
        return None
    question, answer = clean(question), clean(answer, 1000)
    if not question:
        return None
    return {
        "id": source,
        "created": created,
        "question": question,
        "answer": answer,
        "display_name": display_name(event.get("display_name", "")),
        "private": event.get("private") is True,
    }
