"""Bounded model/tool conversation loop and narrow answer editing.

The registry is immutable during a turn. At most two tool rounds/four attempts
are allowed; tool data stays in this request and never becomes QQ instructions.
"""

import asyncio
import json
from datetime import datetime, timezone, timedelta
from qqbot.chat_policy import (
    CHAT_EDITOR,
    visible_reply,
    chat_revision_reason,
    needs_search,
    public_lookup_subject,
    search_evidence,
)
from qqbot.model.answer_display import source_requested
from qqbot.memory.grounding import attribution_revision_reason, ATTRIBUTION_EDITOR
from qqbot.continuity import CONTINUITY_EDITOR, continuity_revision_reason, limit_role_actions
from qqbot.addressing import (
    ADDRESSING_BOUNDARY,
    recipient_names,
    addressing_revision_reason,
    greeting_addressee,
)
import logging

log = logging.getLogger("qqbot")


class Conversation:
    def __init__(self, bot):
        self.bot = bot

    async def revise_chat(self, messages, content):
        question = next(
            (x.get("content", "") for x in reversed(messages) if x["role"] == "user"), ""
        )
        hour = datetime.now(timezone(timedelta(hours=8))).hour
        attribution = addressing_revision_reason(messages, content) or attribution_revision_reason(
            messages, content, self.bot.turn.quoted_speaker, self.bot.turn.with_memory
        )
        continuity = (
            continuity_revision_reason(messages, content) if not self.bot.last_trace else None
        )
        reason = (
            "action_only_reply"
            if not content.strip()
            else attribution
            or continuity
            or (chat_revision_reason(question, content, hour) if not self.bot.last_trace else None)
        )
        if not reason:
            return content
        system = (
            messages[0].get("content", "") if messages and messages[0]["role"] == "system" else ""
        )
        try:
            reply = await asyncio.wait_for(
                self.bot.model_message(
                    [
                        {
                            "role": "system",
                            "content": system
                            + "\n"
                            + CHAT_EDITOR
                            + ATTRIBUTION_EDITOR
                            + CONTINUITY_EDITOR,
                        },
                        # Attribution repairs retain only already-scoped source data.
                        # The ordinary style editor keeps its existing short input.
                        *([dict(m) for m in messages[1:-1]] if attribution or continuity else []),
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "current_message": question,
                                    "draft": content,
                                    "revision_reason": reason,
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ],
                    [],
                    thinking="disabled",
                ),
                20,
            )
            revised = reply.get("content")
            if not isinstance(revised, str) or not revised.strip() or reply.get("tool_calls"):
                raise ValueError("Invalid chat edit")
            revised = limit_role_actions(
                visible_reply(revised),
                question,
                [m.get("content", "") for m in messages if m["role"] == "assistant"],
            )[:800]
            if not revised:
                raise ValueError("Empty chat edit")
            if (
                chat_revision_reason(question, revised, hour)
                or continuity_revision_reason(messages, revised)
                in ("resolved_request_repeated", "resolved_topic_repeated")
                or addressing_revision_reason(messages, revised)
                or attribution_revision_reason(
                    messages, revised, self.bot.turn.quoted_speaker, self.bot.turn.with_memory
                )
            ):
                raise ValueError("Chat edit still off topic")
        except Exception as error:
            log.info("Chat edit fallback (%s)", type(error).__name__)
            revised = (
                "在的。"
                if reason == "ping_overplay"
                else "这个称呼是在叫我吧。"
                if reason == "greeting_addressee_confusion" and greeting_addressee(question)
                else "暂时无法确认称呼。"
                if reason == "greeting_addressee_confusion"
                else "这是那位群友的情况。"
                if reason == "quote_subject_confusion"
                else "不确定当时怎么说的，就不乱补了。"
                if reason == "unsupported_assistant_history"
                else "不客气。"
                if reason == "unsupported_personal_history" and "谢" in question
                else "好的，知道了。"
            )
        self.bot.last_trace.append(
            {
                "name": "chat_revision",
                "kind": "internal",
                "arguments": {"reason": reason},
                "result": {"draft": content, "revised": revised},
            }
        )
        return revised

    async def complete(self, messages, panel=False):
        if recipient_names(messages) and messages and messages[0]["role"] == "system":
            messages = [dict(m) for m in messages]
            messages[0]["content"] += ADDRESSING_BOUNDARY
        working = list(messages)
        tools = self.bot.plugins.tools() if self.bot.plugin_url else []
        if panel:
            tools = [t for t in tools if t["function"]["name"] != "web_search"]
        self.bot.last_trace = []
        latest_user = next(
            (x.get("content", "") for x in reversed(messages) if x["role"] == "user"), ""
        )
        lookup_subject = public_lookup_subject(latest_user, messages[:-1])
        force_search = (
            not panel
            and tools
            and "web_search" in self.bot.plugins.functions
            and needs_search(latest_user, messages[:-1])
        )
        base_system = (
            working[0].get("content", "") if working and working[0]["role"] == "system" else None
        )
        plugin_attempts = 0
        retried_lookup = False
        for round_number in range(3):
            choice = (
                {"type": "function", "function": {"name": "web_search"}}
                if force_search and round_number == 0
                else None
            )
            reply = await self.bot.model_message(working, tools if round_number < 2 else [], choice)
            calls = reply.get("tool_calls") or []
            origin = "model"
            if choice and not any(x.get("function", {}).get("name") == "web_search" for x in calls):
                if not lookup_subject:
                    raise RuntimeError("Required search was not called by provider")
                # This is an actual program-initiated tool action, recorded as
                # such; the discarded provider draft isn't treated as evidence.
                calls = [
                    {
                        "id": "program-lookup",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": json.dumps(
                                {"query": lookup_subject + " 介绍"}, ensure_ascii=False
                            ),
                        },
                    }
                ]
                reply = {"content": "", "tool_calls": calls}
                origin = "program_lookup"
            if calls:
                if not tools or round_number >= 2 or len(calls) > 2:
                    raise RuntimeError("Plugin call budget exceeded")
                # Only protocol fields are forwarded, not provider-added instructions.
                assistant = {
                    "role": "assistant",
                    "content": reply.get("content") or "",
                    "tool_calls": calls,
                }
                # Thinking-mode tool continuation requires this protocol field.
                # It stays in this call's memory, never in QQ/history/archive.
                if self.bot.thinking == "enabled":
                    reasoning = reply.get("reasoning_content")
                    assistant["reasoning_content"] = reasoning if isinstance(reasoning, str) else ""
                working.append(assistant)
                for call in calls:
                    name = call.get("function", {}).get("name", "")
                    try:
                        raw = call["function"]["arguments"]
                        if not isinstance(raw, str) or len(raw) > 4096:
                            raise ValueError("Invalid plugin arguments")
                        arguments = json.loads(raw)
                        if plugin_attempts >= 4:
                            raise ValueError("Plugin attempt budget exceeded")
                        plugin_attempts += 1
                        result = await self.bot.call_plugin(name, arguments)
                    except Exception:
                        result = {"error": "插件调用被拒绝或查询暂时不可用。"}
                        arguments = {}
                    serialized = json.dumps(result, ensure_ascii=False)[:16000]
                    self.bot.last_trace.append(
                        {
                            "name": name,
                            "arguments": arguments,
                            "origin": origin,
                            "result": serialized,
                        }
                    )
                    alternate = lookup_subject + " 介绍" if lookup_subject else ""
                    if (
                        name == "web_search"
                        and alternate
                        and not retried_lookup
                        and plugin_attempts < 4
                        and arguments.get("query") != alternate
                        and not search_evidence([self.bot.last_trace[-1]])[1]
                    ):
                        retried_lookup = True
                        plugin_attempts += 1
                        try:
                            retry = await self.bot.call_plugin(name, {"query": alternate})
                        except Exception:
                            retry = {"error": "备用关键词查询暂时不可用。"}
                        self.bot.last_trace.append(
                            {
                                "name": name,
                                "arguments": {"query": alternate},
                                "origin": "program_retry",
                                "result": json.dumps(retry, ensure_ascii=False)[:16000],
                            }
                        )
                        serialized = json.dumps(
                            {"first_attempt": result, "retry": retry}, ensure_ascii=False
                        )[:16000]
                    working.append(
                        {"role": "tool", "tool_call_id": call["id"], "content": serialized}
                    )
                attempted, sources = search_evidence(self.bot.last_trace)
                if attempted:
                    state = (
                        "搜索已取得候选资料，重名主体须区分；只依据返回的真实来源，不足以确认时说明不确定。"
                        if sources
                        else "搜索没有可用证据，明确说明尚未核实，不能伪称已经查证。"
                    )
                    if base_system is not None:
                        working[0] = {"role": "system", "content": base_system + "\n" + state}
                        self.bot.last_trace[-1]["summary_system_suffix"] = state
                continue
            content = reply.get("content")
            if not isinstance(content, str) or not content.strip():
                self.bot.api_state = "empty_response"
                raise RuntimeError("Empty DeepSeek response")
            content = limit_role_actions(
                visible_reply(content),
                latest_user,
                [m.get("content", "") for m in messages if m["role"] == "assistant"],
            )
            content = await self.bot.revise_chat(messages, content)
            content = content[:3500]
            _, sources = search_evidence(self.bot.last_trace)
            # Show actual evidence even when the provider omits its citations.
            if (
                sources
                and source_requested(latest_user)
                and not any(source in content for source in sources)
            ):
                appendix = "\n\n搜索资料：\n" + "\n".join(sources[:2])
                content = content[: 3500 - len(appendix)] + appendix
            self.bot.api_state = "ok"
            return content.strip()[:3500]
        raise RuntimeError("No final plugin response")
