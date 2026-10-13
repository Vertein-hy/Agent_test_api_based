import json
import unittest
from qqbot.model.tool_protocol import decode_calls, protocol_present, protocol_requested

DIRECT = """<｜DSML｜tool_calls>
<｜DSML｜invoke name="web_read"><｜DSML｜parameter name="url" string="true">https://www.python.org/</｜DSML｜parameter></｜DSML｜invoke>
<｜DSML｜invoke name="web_search"><｜DSML｜parameter name="query" string="true">Python 官方 文档 安装 系统要求</｜DSML｜parameter></｜DSML｜invoke>
</｜DSML｜tool_calls>"""
WRAPPED = """<｜DSML｜tool_calls><｜DSML｜invoke name="web_search"><｜DSML｜parameter name="arguments" string="false">{"query": "Python 官方 文档 入门 教程"}</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜tool_calls>"""


class ProtocolTests(unittest.TestCase):
    def test_synthetic_direct_parameters(self):
        calls = decode_calls("我再确认一下。\n" + DIRECT)
        self.assertEqual([c["function"]["name"] for c in calls], ["web_read", "web_search"])
        self.assertEqual(
            json.loads(calls[0]["function"]["arguments"]), {"url": "https://www.python.org/"}
        )

    def test_synthetic_wrapped_arguments(self):
        calls = decode_calls(WRAPPED)
        self.assertEqual(
            json.loads(calls[0]["function"]["arguments"]),
            {"query": "Python 官方 文档 入门 教程"},
        )

    def test_ascii_delimiters_and_xml_escaping(self):
        calls = decode_calls(WRAPPED.replace("｜", "|").replace("Python", "Python &amp;"))
        self.assertIn("&", json.loads(calls[0]["function"]["arguments"])["query"])

    def test_fenced_samples_never_become_actions_and_regular_code_is_preserved(self):
        self.assertIsNone(decode_calls("```xml\n" + DIRECT + "\n```"))
        self.assertIsNone(decode_calls('```python\nprint("hello")\n```'))
        self.assertTrue(protocol_requested("解释DSML调用协议"))

    def test_incomplete_malformed_oversized_duplicate_or_many_calls_rejected(self):
        for text in (
            DIRECT[:80],
            WRAPPED.replace('string="false"', 'string="weird"'),
            WRAPPED.replace('"query":', '"query"'),
            DIRECT.replace("</｜DSML｜tool_calls>", ""),
            WRAPPED.replace("</｜DSML｜tool_calls>", WRAPPED + WRAPPED + "</｜DSML｜tool_calls>"),
        ):
            with self.assertRaises((ValueError, SyntaxError)):
                decode_calls(text)

    def test_plain_tool_names_and_search_result_code_do_not_match(self):
        self.assertFalse(protocol_present('web_search返回摘要，Python代码print("你好")'))
