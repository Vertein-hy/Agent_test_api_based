"""DeepSeek transport: bounded SSE/JSON, measured latency, bounded recovery and protocol repair.

No raw provider content is returned with a tool/memory envelope. Tool execution
belongs to the conversation engine; this component only decodes protocol fields.
"""

import asyncio
from dataclasses import dataclass
import json
import time
import xml.etree.ElementTree as ET
import aiohttp
from qqbot.config import secret
from qqbot.model.streaming import StreamAssembler
from qqbot.model.errors import StreamError, HTTPError
from qqbot.model.tool_protocol import decode_calls, protocol_present, protocol_requested
from qqbot.model.answer_display import internal_memory_present, without_internal_memory
import logging

log = logging.getLogger("qqbot")


@dataclass
class RequestBudget:
    """A response and its protocol repair share one deadline and one transport retry."""

    deadline: float
    transport_retries: int = 1


class ModelClient:
    def __init__(self, bot):
        self.bot = bot

    async def message(
        self,
        messages,
        tools,
        tool_choice=None,
        *,
        thinking=None,
        _protocol_retry=False,
        _budget=None,
    ):
        mode = thinking or self.bot.thinking
        payload = {
            "model": self.bot.model,
            "messages": messages,
            "max_tokens": 4096 if mode == "enabled" else 1000,
            "stream": True,
            "thinking": {"type": mode},
        }
        if mode == "enabled":
            payload["reasoning_effort"] = self.bot.reasoning_effort
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
            payload["messages"] = [dict(m) for m in messages]
            if payload["messages"] and payload["messages"][0].get("role") == "system":
                payload["messages"][0]["content"] += (
                    "\n动作仅使用结构化 tool_calls；可见答复禁止内部DSML或调用参数。根据工具证据回答，不足时说明不确定。"
                )
        budget = _budget or RequestBudget(time.monotonic() + 90)
        reply = await self._response(payload, budget)
        content = reply.get("content") or ""
        question = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), ""
        )
        # Formal tool_calls always win. Accompanying DSML isn't a second action.
        if reply.get("tool_calls"):
            if protocol_present(content) or internal_memory_present(content):
                reply["content"] = ""
            return reply
        if internal_memory_present(content):
            try:
                cleaned = without_internal_memory(content)
                if not cleaned:
                    raise ValueError("No answer after memory envelope")
                reply["content"] = content = cleaned
                log.warning("Internal memory envelope removed from model output")
            except ValueError:
                if _protocol_retry:
                    self.bot.api_state = "invalid_memory_output"
                    raise RuntimeError("Provider repeated internal memory output")
                working = [dict(m) for m in messages]
                working.insert(
                    0,
                    {
                        "role": "system",
                        "content": "恢复可见回答：只提供面向用户的文字，排除内部记忆容器及标题，本次禁用工具。",
                    },
                )
                return await self.message(
                    working, [], thinking=thinking, _protocol_retry=True, _budget=budget
                )
        if protocol_present(content) and not protocol_requested(question):
            try:
                calls = decode_calls(content)
                allowed = {t["function"]["name"] for t in tools}
                if tools and calls and all(c["function"]["name"] in allowed for c in calls):
                    # Original call_plugin registry/budget/UID checks still apply.
                    reply["content"] = ""
                    reply["tool_calls"] = calls
                    return reply
            except (ValueError, ET.ParseError, json.JSONDecodeError):
                pass
            if not _protocol_retry:
                working = [dict(m) for m in messages]
                working[0]["content"] += (
                    "\n协议修复：结束工具循环，只使用现有证据生成最终答复；不输出DSML及内部参数，不能宣称后续检索。"
                )
                return await self.message(
                    working, [], thinking=thinking, _protocol_retry=True, _budget=budget
                )
            self.bot.api_state = "invalid_tool_protocol"
            raise RuntimeError("Provider emitted tool protocol without a usable final answer")
        return reply

    async def _response(self, payload, budget):
        """Retry only transient transport failures before any tool or QQ action.

        The exact request is regenerated, with no partial answer accepted. A
        shared deadline prevents two 90-second waits or an unbounded retry loop.
        Authentication, invalid payloads, size limits and truncated answers fail
        immediately. Protocol repairs use the same remaining retry allowance.
        """
        attempt = 0
        while True:
            remaining = budget.deadline - time.monotonic()
            if remaining <= 0:
                self.bot.api_state = "model_timeout"
                raise asyncio.TimeoutError()
            attempt += 1
            try:
                return await self._request(payload, remaining, attempt)
            except (HTTPError, StreamError, aiohttp.ClientError, asyncio.TimeoutError) as error:
                transient = getattr(error, "retryable", False) or isinstance(
                    error,
                    (
                        aiohttp.ClientConnectionError,
                        aiohttp.ClientPayloadError,
                        asyncio.TimeoutError,
                    ),
                )
                if (
                    not transient
                    or not budget.transport_retries
                    or budget.deadline - time.monotonic() <= 1
                ):
                    raise
                budget.transport_retries -= 1
                log.warning(
                    "model_retry outcome=%s remaining_retries=%s",
                    self.bot.api_state,
                    budget.transport_retries,
                )
                await asyncio.sleep(0.5)

    async def _request(self, payload, remaining, attempt):
        start = time.monotonic()
        first = None
        headers_ms = None
        transport = "sse"
        self.bot.api_state = "requesting"
        success = False
        try:
            async with self.bot.session.post(
                self.bot.api_url + "/chat/completions",
                json=payload,
                headers={"Authorization": "Bearer " + secret("DEEPSEEK_API_KEY")},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=remaining),
            ) as response:
                headers_ms = (time.monotonic() - start) * 1000
                if response.status != 200:
                    raise HTTPError(response.status)
                if "text/event-stream" not in response.headers.get("Content-Type", ""):
                    # Providers may ignore streaming; no invented TTFT measurement.
                    transport = "json"
                    raw = await response.content.read(200001)
                    if len(raw) > 200000:
                        raise StreamError("response_too_large")
                    data = json.loads(raw)
                    self.bot.reported_model = data.get("model")
                    if data["choices"][0].get("finish_reason") == "length":
                        self.bot.api_state = "truncated_response"
                        raise RuntimeError("Model response truncated")
                    reply = data["choices"][0]["message"]
                else:
                    assembler = StreamAssembler()
                    buffered = b""
                    received = 0
                    async for chunk in response.content.iter_chunked(4096):
                        received += len(chunk)
                        if received > 1000000:
                            raise StreamError("stream_too_large")
                        buffered += chunk
                        if len(buffered) > 200000:
                            raise StreamError("stream_line_too_large")
                        while b"\n" in buffered:
                            line, buffered = buffered.split(b"\n", 1)
                            if assembler.add(line.rstrip(b"\r").decode("utf-8")) and first is None:
                                first = (time.monotonic() - start) * 1000
                    if buffered:
                        if assembler.add(buffered.decode("utf-8")) and first is None:
                            first = (time.monotonic() - start) * 1000
                    if assembler.finish == "length":
                        self.bot.api_state = "truncated_response"
                    reply = assembler.result()
                    self.bot.reported_model = assembler.model
                success = True
                self.bot.api_state = "response_received"
        except (HTTPError, StreamError) as error:
            self.bot.api_state = error.code
            raise
        except asyncio.TimeoutError:
            self.bot.api_state = "model_timeout"
            raise
        except aiohttp.ClientError:
            self.bot.api_state = "model_transport_error"
            raise
        except (
            json.JSONDecodeError,
            KeyError,
            IndexError,
            AttributeError,
            TypeError,
            UnicodeError,
        ):
            self.bot.api_state = "invalid_model_response"
            raise
        finally:
            item = {
                "transport": transport,
                "attempt": attempt,
                "outcome": self.bot.api_state,
                "headers_ms": round(headers_ms, 1) if headers_ms is not None else None,
                "first_token_ms": round(first, 1) if first is not None else None,
                "total_ms": round((time.monotonic() - start) * 1000, 1),
                "success": success,
            }
            self.bot.turn.timings.append(item)
            log.info("model_timing %s", json.dumps(item))
        return reply
