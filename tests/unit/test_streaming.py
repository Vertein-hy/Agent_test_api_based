import json
import unittest
from qqbot.model.streaming import StreamAssembler


class StreamingTests(unittest.TestCase):
    def send(self, a, delta, finish=None):
        return a.add(
            "data: "
            + json.dumps({"model": "test", "choices": [{"delta": delta, "finish_reason": finish}]})
        )

    def test_content_reasoning_and_finish(self):
        a = StreamAssembler()
        self.assertFalse(self.send(a, {"role": "assistant"}))
        self.assertFalse(self.send(a, {"reasoning_content": "thinking"}))
        self.assertTrue(self.send(a, {"content": "你好"}))
        self.send(a, {"content": "啊"}, "stop")
        self.assertEqual(a.result()["content"], "你好啊")

    def test_fragmented_tool_calls_preserve_ids_names_and_arguments(self):
        a = StreamAssembler()
        self.send(
            a,
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-a",
                        "function": {"name": "yunzai_", "arguments": '{"uid":'},
                    }
                ]
            },
        )
        self.send(
            a,
            {
                "tool_calls": [
                    {"index": 0, "function": {"name": "query", "arguments": '"700000001"}'}}
                ]
            },
            "tool_calls",
        )
        call = a.result()["tool_calls"][0]
        self.assertEqual(call["id"], "call-a")
        self.assertEqual(call["function"]["name"], "yunzai_query")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"uid": "700000001"})

    def test_incomplete_truncated_and_oversized_streams_fail(self):
        a = StreamAssembler()
        self.send(a, {"content": "a"})
        with self.assertRaises(ValueError):
            a.result()
        self.send(a, {}, "length")
        with self.assertRaises(RuntimeError):
            a.result()
        with self.assertRaises(ValueError):
            self.send(StreamAssembler(), {"content": "a" * 100001})

    def test_usage_comments_and_done_are_not_first_tokens(self):
        a = StreamAssembler()
        for line in (": keepalive", 'data: {"choices":[],"usage":{}}', "data: [DONE]"):
            self.assertFalse(a.add(line))
