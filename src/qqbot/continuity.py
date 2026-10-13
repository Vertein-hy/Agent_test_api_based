"""Pure continuity hints from the current scoped conversation, never durable tasks.

Assistant requests are signals to review repetition, not evidence of a user's
obligation. Only subsequent real user input can close or reopen an interaction.
The model handles semantic topic changes; bounded text checks select a single
review for obvious repeated requests or copied passages, without an item list.
"""

from difflib import SequenceMatcher
import re


CONTINUITY_BOUNDARY = "\n对话规则：以当前真实输入为准，跟随用户的话题。助手台词不产生用户义务或记忆事实。已完成、拒绝或结束的请求不得再次索取，必要的任务材料仍可询问。轻松对话至多保留一个12字以内的动作插语；前两条成功回复出现过动作时，本轮不使用。技术、正式或认真回答及用户拒绝旁白时不使用动作。代码、公式、引用和明确的创作正文保留。不能以礼物、物品或报酬作为回答条件。"

_PARENTHETICAL = re.compile(
    r'```[\s\S]*?```|`[^`\n]*`|“[^”\n]*”|"[^"\n]*"|'
    r"（(?P<cn>[^（）\n]{1,240})）|\((?P<en>[^()\n]{1,240})\)"
)
_BODY_PART = r"(?:尾巴|手|耳朵|长发|头发|嘴角|眼睛|眼神|脸颊|脸|肩膀|手指|脑袋|下巴|身子|身体)"
_BODY_ACTION = re.compile(
    _BODY_PART + r".{0,30}(?:晃|甩|摇|拍|抖|翘|竖|垂|红|躲|收|扬|弯|压|眨|瞪|撇|别|靠|探|伸|捂)|"
    r"(?:甩|摇|晃|抖|摆|翘|收|竖|垂|眨|瞪|撇|抿|捂|抬|低|挠|摸).{0,8}" + _BODY_PART + r"|"
    r"(?:^|[，,；;])\s*(?:轻轻|慢吞吞|缓缓|默默|偷偷|小声|低声|冷哼|哼了|笑|微笑|傻笑|冷笑|"
    r"叹气|愣|转身|转过身|别过脸|别开脸|歪头|挑眉|扶额|抱臂|叉腰|摆手|挥手|挠头|眨眼|"
    r"嘟嘴|鼓腮|翻白眼|探出头|深吸|清嗓|摇摇头|点点头|脸红|心想|内心|害羞|委屈|得意|无奈|嫌弃)"
)


def role_actions(text):
    """Recognized narration only; quoted text, code and ordinary parentheses are data."""
    return [
        match
        for match in _PARENTHETICAL.finditer(text)
        if (body := match.group("cn") or match.group("en")) and _BODY_ACTION.search(body)
    ]


def limit_role_actions(text, question="", previous=()):
    """Allow one brief aside, followed by two successfully delivered plain replies.

    Callers supply only prior successful replies from this conversation (or public
    comment branch). No global counter is advanced by a failed send or model edit.
    Explicit creative work is content, not an incidental character performance.
    """
    creative = re.search(r"写.{0,20}(?:剧本|小说|故事|动作)|角色扮演|动作描写|翻译|原文", question)
    # A cited gesture such as “（别开脸）这个动作” is not an instruction to stop.
    negative = re.search(
        r"(?:不要|去掉|不带|不加|不需要|无需|别(?:再)?(?:加|写|带|用|有|出现|配|附))"
        r".{0,12}(?:动作|旁白|括号)",
        question,
    )
    if creative and not negative:
        return text
    serious = re.search(
        r"解释|计算|算一下|公式|代码|函数|算法|报错|调试|技术|正式|文书|通知|邮件|客户|"
        r"只(?:给|输出|要)|直接(?:回答|说|给)|认真|别开玩笑|难过|伤心|痛苦",
        question,
    )
    matches = role_actions(text)
    plain = text
    for match in reversed(matches):
        plain = plain[: match.start()] + plain[match.end() :]
    if not plain.strip() or (matches and not re.search(r"[^\W_]", plain)):
        return ""  # A gesture alone still needs the existing bounded dialogue repair.
    allowed = not (negative or serious or any(role_actions(x) for x in list(previous)[-2:]))
    kept = False
    chunks, start = [], 0
    for match in matches:
        body = (match.group("cn") or match.group("en")).strip()
        chunks.append(text[start : match.start()])
        if allowed and not kept and len(body) <= 12:
            chunks.append(match[0])
            kept = True
        start = match.end()
    chunks.append(text[start:])
    return "".join(chunks).strip()


_QUANTITY = r"(?:[一二两三四五六七八九十几0-9]+(?:个|只|条|份|块|杯|碗|根|本|张|颗|包|套|支|台|把|辆|斤|瓶|件|盒))?"
_REQUEST = re.compile(
    r"(?:(?:给|送|买|递|喂|赔|欠|请)(?:给)?我(?:吃|喝)?"
    r"|我(?:还|再)?(?:想要|想吃|想喝))"
    + _QUANTITY
    + r"([\u4e00-\u9fffA-Za-z0-9]{2,16}?)(?=[，,。.!！?？…～~]|的话|才|再|就|吧|啊|呀|哦|呗|嘛|呢|$)"
)
_CLOSED = re.compile(
    r"给你(?:了|啦|买了|送了)|(?:已经|都|刚才)(?:给|送|买|喂)(?:了)?你|拿去|收下|收到了|喂饱|吃完了|"
    r"满足了|完成了|不要再|别再|别老|别一直|不(?:要|用|想|再).{0,8}(?:提|说|聊|讨|要)|"
    r"换(?:个|一个)?话题|结束这个话题"
)
_REOPEN = re.compile(r"要不要(?:我)?再|(?:我|那我)(?:再|还)(?:给|送|买)你|再给你|再送你")
_STOP = re.compile(
    r"不要再|别再|别老|别一直|不(?:要|用|想|再).{0,8}(?:提|说|聊|讨|要)|"
    r"换(?:个|一个)?话题|结束这个话题"
)
_FOLLOWUP = re.compile(r"^(?:继续|接着|再讲|再解释|展开|详细说|为什么|怎么说|然后呢|还有呢)")


def _prose(text):
    return re.sub(r"```[\s\S]*?```|`[^`\n]*`|（[^）]*）|\([^)]*\)", "", text[:1200])


def request_terms(text):
    """Bounded grammatical objects, independent of which food/item is mentioned."""
    terms = set()
    for clause in re.split(r"[。.!！?？\n]", _prose(text)):
        if re.search(r"已经|不用|不必|不要|不需要|够了|谢谢|收到了|不欠", clause):
            continue
        terms.update(m[1] for m in _REQUEST.finditer(clause))
        terms.update(re.findall(r"没有([\u4e00-\u9fffA-Za-z0-9]{2,16}?)免谈", clause))
    return terms


def continuity_revision_reason(messages, answer):
    """Distinguish closed requests (hard check) from repetition needing semantic review."""
    real = [
        m
        for m in messages
        if m.get("role") in ("user", "assistant")
        and not m.get("content", "").startswith(("MEMORY_DATA_JSON", "当前消息引用的群聊原文"))
    ][-13:]
    question = real[-1].get("content", "") if real else ""
    if re.search(r"创作|小说|剧本|角色扮演|翻译|举例|协议示例", question):
        return None
    prior = real[:-1]
    terms = request_terms(answer)
    for index, message in enumerate(prior):
        if message["role"] != "assistant":
            continue
        old = request_terms(message["content"])
        repeated = {term for term in terms if any(term in x or x in term for x in old)}
        if (
            repeated
            and not re.search(r"还需要|还缺|不完整|截了一半|补充|继续|接着", question)
            and not _REOPEN.search(question)
            and any(m["role"] == "user" and _CLOSED.search(m["content"]) for m in real[index + 1 :])
        ):
            return "resolved_request_repeated"
        # A closed request can echo as a reward/acknowledgement without a new
        # imperative. Explicitly stopping that topic also stops this framing.
        mentions = {term for term in old if term in _prose(answer)}
        if mentions and _STOP.search(question) and not _REOPEN.search(question):
            return "resolved_topic_repeated"
        if (
            mentions
            and not any(term in question for term in mentions)
            and any(
                m["role"] == "user" and _CLOSED.search(m["content"]) for m in real[index + 1 : -1]
            )
        ):
            return "repeated_topic_review"
        if not repeated:
            continue
        if not _FOLLOWUP.search(question) and not any(t in question for t in repeated):
            return "repeated_topic_review"
    if _FOLLOWUP.search(question):
        return None
    draft = _prose(answer)
    previous = [m["content"] for m in prior if m["role"] == "assistant"][-2:]
    for text in previous:
        for block in SequenceMatcher(
            None, _prose(text), draft, autojunk=False
        ).get_matching_blocks():
            phrase = draft[block.b : block.b + block.size]
            if len(re.findall(r"[\w\u4e00-\u9fff]", phrase)) >= 12 and phrase not in question:
                return "repeated_topic_review"
    return None


CONTINUITY_EDITOR = "\n根据 revision_reason 编辑草稿。action_only_reply：补上针对当前输入的文字答复。resolved_request_repeated：移除已结束请求的重复索取；resolved_topic_repeated：遵守停止旧话题的要求；repeated_topic_review：删去与当前任务无关的重复，保留必要事实、术语及未齐备材料的说明。只输出最终文字，不说明编辑步骤，不增加用户义务。"
