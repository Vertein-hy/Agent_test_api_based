"""Narrow attribution checks before delivery, without guessing identities or modifying records.

These checks catch known forms of invented past dialogue and quoted-patient
confusion. They are not a general semantic verifier of every model statement.
"""

import json
import re


def attribution_revision_reason(messages, answer, quoted_speaker="unknown", memory_active=False):
    question = messages[-1].get("content", "") if messages else ""
    if len(question) > 250 or re.search(
        r"创作|小说|剧本|角色扮演|翻译|代码|故事|举例|协议|自我介绍|第一人称|帮我写|代我|替我",
        question,
    ):
        return None
    if quoted_speaker == "other_member" and not re.search(r"我也|我(?:的)?腰|帮我|给我", question):
        if re.search(
            r"你(?:自己|本人|的)?腰(?:痛|疼|不好)|(?:给|帮|替|为)你.{0,8}(?:捶|揉|按|按摩|拉伸)",
            answer,
        ):
            return "quote_subject_confusion"
    if not memory_active:
        return None
    prior_users, prior_assistant, episodes, names = [], [], [], []
    # An explicit statement in the current input is also a source. A question
    # asking us to remember an unspecified past is not evidence of its contents.
    if not re.search(r"[?？]|吗|什么|哪里|是否|记得|回忆", question) and re.search(
        r"(?:上次|之前|昨天|当时|以前).{0,12}我|我.{0,12}(?:上次|之前|昨天|当时|以前)", question
    ):
        prior_users.append(question)
    for message in messages[1:-1]:
        text = message.get("content", "")
        if message.get("role") == "assistant":
            prior_assistant.append(text)
        elif message.get("role") == "user":
            if text.startswith("MEMORY_DATA_JSON"):
                try:
                    data = json.loads(text.split("\n", 1)[1])
                    episodes.extend(
                        m for m in data.get("memories", []) if m.get("kind") == "episode"
                    )
                    names.extend(
                        n.get("value", "")
                        for n in data.get("profile", {}).get("names", [])
                        if n.get("key") in ("name", "preferred_name")
                    )
                except (ValueError, IndexError, AttributeError):
                    pass
            elif not text.startswith("当前消息引用的群聊原文"):
                prior_users.append(text)
    if not re.search(r"叫你|称呼你|你的名字", question) and any(
        value
        and re.search(
            r"(?:^|[，。！!？?；;:\n])\s*(?:我叫|叫我|我的名字(?:是|叫))" + re.escape(value), answer
        )
        for value in names
    ):
        return "name_role_confusion"
    if (
        not prior_assistant
        and quoted_speaker != "bot"
        and re.search(
            r"当时我(?:还)?(?:说|吐槽|告诉|答应|承诺|表示)|上次我(?:还)?(?:说|告诉|答应|承诺)",
            answer,
        )
    ):
        return "unsupported_assistant_history"
    if (
        not prior_users
        and not episodes
        and quoted_speaker != "current_speaker"
        and re.search(
            r"(?:上次|之前|昨天|那次|当时).{0,30}(?:你|咱|我们|不回消息|不理我|答应|约定|承诺)",
            answer,
        )
    ):
        return "unsupported_personal_history"
    return None


ATTRIBUTION_EDITOR = "\n归属修订：greeting_addressee_confusion 表示把接收方误称为用户，应删除误称。quote_subject_confusion 表示把引用者情况误归用户，应恢复第三人称，不能推断症状或治疗。unsupported_personal_history/unsupported_assistant_history 表示缺少原始证据，应删除虚构往事。name_role_confusion 表示混淆用户与助手姓名，应按来源恢复归属。只处理当前问题，不建立身份、关系、权限或新事实，不输出内部容器。"
