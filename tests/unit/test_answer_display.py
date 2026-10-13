import unittest
import json
from qqbot.model.answer_display import (
    source_requested,
    without_search_footer,
    internal_memory_present,
    without_internal_memory,
)

MEMORY_ECHO = "MEMORY_DATA_JSON（当前说话者与历史资料）:\n" + json.dumps(
    {
        "speaker_ref": "fictional-ref",
        "profile": {"names": []},
        "memories": [{"text": "用户希望回答简短"}],
    },
    ensure_ascii=False,
)


class AnswerDisplayTests(unittest.TestCase):
    def test_default_footer_removed_without_altering_answer_or_useful_url(self):
        main = "建议去这个下载地址：https://example.com/download\n先确认适配情况。"
        self.assertEqual(
            without_search_footer(
                main + "\n\n搜索资料：\nhttps://example.com/source-a\nhttps://example.org/source-b"
            ),
            main,
        )

    def test_explicit_sources_and_negation(self):
        for q in ("给我来源", "附上链接", "原文在哪", "官网是什么", "source please"):
            self.assertTrue(source_requested(q))
        for q in ("锐评Mac打游戏", "你天天只知道吃", "不要附来源链接", "链接不用给我"):
            self.assertFalse(source_requested(q))

    def test_legitimate_code_and_normal_urls_are_preserved(self):
        for text in (
            '```python\nurl="https://example.com/"\n```',
            "```text\n搜索资料：\nhttps://example.com/\n```",
            "资料：\nhttps://example.com/",
        ):
            self.assertEqual(without_search_footer(text), text)

    def test_logged_memory_envelope_removed_but_birthday_answer_preserved(self):
        answer = "生日快乐啦！🎂"
        for text in (MEMORY_ECHO + answer, "```json\n" + MEMORY_ECHO + "\n```\n" + answer):
            self.assertTrue(internal_memory_present(text))
            self.assertEqual(without_internal_memory(text), answer)

    def test_partial_or_unlabelled_memory_fails_closed(self):
        for text in (
            'MEMORY_DATA_JSON: {"speaker_ref":',
            '{"speaker_ref":"private", "profile":{}}',
            'MEMORY_DATA_JSON: {"unknown":123}',
        ):
            with self.assertRaises(ValueError):
                without_internal_memory(text)

    def test_ordinary_json_and_code_are_preserved(self):
        for text in (
            '```json\n{"name":"测试乙","age":21}\n```',
            "生日快乐！",
            '```python\nprint("hello")\n```',
        ):
            self.assertFalse(internal_memory_present(text))
            self.assertEqual(without_internal_memory(text), text)
