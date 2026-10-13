"""Bounded OpenAI-compatible SSE reassembly, including fragmented tool calls."""

import json
from qqbot.model.errors import StreamError


class StreamAssembler:
    def __init__(self):
        self.message = {"role": "assistant", "content": ""}
        self.calls = {}
        self.finish = None
        self.model = None
        self.size = 0
        self.done = False

    def add(self, line):
        if not line.startswith("data:"):
            return False
        raw = line[5:].strip()
        if raw == "[DONE]":
            self.done = True
            return False
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            raise StreamError("invalid_stream_json") from error
        if not isinstance(data, dict):
            raise StreamError("invalid_stream_envelope")
        if data.get("error"):
            # Error bodies can include prompts or addresses. Keep only a closed
            # classification, never the provider's message/code as exception text.
            error = data["error"]
            kind = error.get("type", "") if isinstance(error, dict) else ""
            transient = kind in {"server_error", "internal_server_error", "overloaded_error"}
            raise StreamError("provider_stream_error", retryable=transient)
        self.model = data.get("model", self.model)
        choices = data.get("choices", [])
        if not choices:
            return False
        choice = choices[0]
        self.finish = choice.get("finish_reason") or self.finish
        delta = choice.get("delta", {})
        token = False
        for field in ("content", "reasoning_content"):
            value = delta.get(field)
            if value:
                if not isinstance(value, str):
                    raise StreamError("invalid_stream_fragment")
                self.size += len(value)
                self.message[field] = self.message.get(field, "") + value
                if field == "content":
                    token = True
        for call in delta.get("tool_calls") or []:
            index = call.get("index")
            if type(index) is not int or not 0 <= index < 8:
                raise StreamError("invalid_tool_index")
            out = self.calls.setdefault(
                index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            )
            if call.get("id"):
                out["id"] = call["id"]
            for key in ("name", "arguments"):
                part = (call.get("function") or {}).get(key, "")
                if not isinstance(part, str):
                    raise StreamError("invalid_tool_fragment")
                out["function"][key] += part
                self.size += len(part)
                token = token or bool(part)
        if self.size > 100000:
            raise StreamError("stream_too_large")
        return token

    def result(self):
        if not self.finish:
            raise StreamError("incomplete_stream", retryable=True)
        if self.finish == "length":
            raise RuntimeError("Model response truncated")
        if self.calls:
            self.message["tool_calls"] = [self.calls[i] for i in sorted(self.calls)]
        return self.message
