"""Observable social boundaries with fictional accounts and captured writes."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from qqbot.domain import Message, Turn
from qqbot.social.policy import command, comment_key, visible_text
from qqbot.social.service import Social
from qqbot.social.store import SocialStore
from qqbot.transport.qzone import Qzone
from qqbot.transport.qzone_data import DataError, comments, decode, forward_candidate

ADMIN = "100001"
BOT = "100002"
FRIEND = "100003"


def fixture_bot():
    return SimpleNamespace(
        api_url="http://fixture",
        model="fixture",
        reasoning_effort="high",
        persona="你是对话助手。",
        session=None,
        turn=Turn(),
        action=AsyncMock(),
    )


class DataTests(unittest.TestCase):
    def test_jsonp_is_data_not_javascript(self):
        self.assertEqual(
            decode("_preloadCallback({code:0,data:{data:[undefined,{html:'\\x3ci\\x3e'},],},});")[
                "data"
            ]["data"],
            [None, {"html": "<i>"}],
        )
        for raw in (
            "cb({code:0,x:process.exit()})",
            "cb({code:0,x:()=>1})",
            "cb({code:0,x:new Object()})",
            "cb({x:1})",
            "cb({code:-1,message:'secret'})",
        ):
            with self.assertRaises(DataError):
                decode(raw)

    def test_parser_bounds(self):
        self.assertEqual(decode(r'{code:0,text:"\ud83d\udc33"}')["text"], "🐳")
        with self.assertRaises(DataError):
            decode("{code:0,x:" + ("[" * 50) + "1" + ("]" * 50) + "}")
        with self.assertRaises(DataError):
            decode("x" * 2000001)

    def test_forward_requires_platform_marker_and_matching_owner(self):
        feed = {
            "appid": 311,
            "opuin": FRIEND,
            "html": f'<i name="feed_data" data-uin="{FRIEND}" data-tid="fwd_abc" data-origuin="{BOT}" data-origtid="original_abc">',
        }
        self.assertEqual(forward_candidate(feed)["tid"], "fwd_abc")
        self.assertIsNone(forward_candidate({**feed, "opuin": ADMIN}))
        self.assertIsNone(forward_candidate({**feed, "html": feed["html"] * 2}))
        self.assertIsNone(forward_candidate({**feed, "appid": 4}))
        self.assertIsNone(forward_candidate({**feed, "html": "有人说他转发了 original_abc"}))

    def test_comment_people_and_parent_are_separate(self):
        values = comments(
            {
                "cmtnum": 1,
                "commentlist": [
                    {
                        "tid": 1,
                        "uin": ADMIN,
                        "content": "根评论",
                        "create_time": 10,
                        "list_3": [
                            {"tid": 2, "uin": FRIEND, "content": "群友回复", "create_time": 11}
                        ],
                    }
                ],
            }
        )
        self.assertEqual(values[0]["author"], ADMIN)
        self.assertEqual(values[1]["parent"], "1")
        self.assertEqual(values[1]["author"], FRIEND)
        with self.assertRaises(DataError):
            comments({"cmtnum": 1})

    def test_visible_boundary(self):
        for raw in (
            "",
            "@{uin:100001,nick:foo}",
            "[CQ:at,qq=100001]",
            "<think>内部推理</think>",
            "MEMORY_DATA_JSON: {",
        ):
            with self.assertRaises(ValueError):
                visible_text(raw, 300)
        self.assertEqual(visible_text("我正在节省算力。", 300), "我正在节省算力。")

    def test_commands_require_current_explicit_input(self):
        self.assertEqual(command("发条动态，聊聊摸鱼"), ("发布", "聊聊摸鱼"))
        self.assertEqual(command("请帮我发一条QQ空间动态：聊聊摸鱼"), ("发布", "聊聊摸鱼"))
        self.assertIsNone(command("他说“发条动态”"))
        self.assertIsNone(command("为什么不能发条动态"))
        for question in (
            "发动态是什么意思",
            "发条动态需要什么条件？",
            "发布动态这个功能怎么用",
            "发动态，怎么发？",
            "发条动态？",
        ):
            self.assertIsNone(command(question))
        self.assertEqual(command("/动态 原文 hello"), ("原文", "hello"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + "/social.sqlite3"
        self.store = SocialStore(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_restart_uncertain_sends_never_requeue(self):
        self.store.enqueue("one", "publish", {})
        job = self.store.next_job()
        self.assertTrue(self.store.reserve_send(job))
        self.store.close()
        self.store = SocialStore(self.path)
        self.assertIsNone(self.store.next_job())
        self.assertEqual(self.store.recent()[0]["state"], "unknown")
        self.assertEqual(Path(self.path).stat().st_mode & 0o777, 0o600)

    def test_dedup_survives_restart(self):
        self.assertTrue(self.store.enqueue("one", "publish", {}))
        self.store.close()
        self.store = SocialStore(self.path)
        self.assertFalse(self.store.enqueue("one", "publish", {}))

    def test_pause_invalidates_slow_job_and_resume_does_not_replay(self):
        self.store.enqueue("one", "publish", {})
        job = self.store.next_job()
        self.store.set_job("one", "generating")
        self.store.pause(True)
        self.store.pause(False)
        self.assertFalse(self.store.active(job))
        self.assertIsNone(self.store.next_job())

    def test_stop_root_stops_discovered_reposts(self):
        self.store.add_post(BOT, "root", "原动态")
        self.store.add_post(FRIEND, "fwd", "附言", root=(BOT, "root"))
        post = self.store.get_post(FRIEND, "fwd")
        self.store.enqueue("one", "repost", {"post": post})
        job = self.store.next_job()
        self.store.stop("root")
        self.assertEqual(self.store.posts(), [])
        self.assertFalse(self.store.active(job))

    def test_account_binding_survives_restart(self):
        self.store.bind_owner(BOT)
        self.store.close()
        self.store = SocialStore(self.path)
        with self.assertRaises(ValueError):
            self.store.bind_owner(FRIEND)

    def test_uncertain_send_counts_for_quota(self):
        for i in range(5):
            self.store.enqueue(str(i), "repost", {"post": {}}, quota_actor="same")
            self.store.db.execute(
                "UPDATE jobs SET state='unknown',sent_at=? WHERE key=?", (time.time() - 100, str(i))
            )
        self.store.db.commit()
        self.store.add_post(BOT, "root", "公开")
        post = self.store.get_post(BOT, "root")
        self.store.enqueue("next", "repost", {"post": post}, quota_actor="same")
        self.assertFalse(self.store.reserve_send(self.store.next_job()))


class SocialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "SOCIAL_ENABLED": "1",
                "SOCIAL_ADMINS": ADMIN,
                "SOCIAL_ADMINS_FILE": "",
                "SOCIAL_PATH": self.tmp.name + "/social.sqlite3",
                "SOCIAL_VISIBILITY": "1",
                "SOCIAL_REPLIES": "1",
                "SOCIAL_REPOSTS": "1",
                "ARCHIVE_SALT": "fictional-social-salt",
                "ARCHIVE_SALT_FILE": "",
            },
        )
        self.env.start()
        self.bot = fixture_bot()
        self.social = Social(self.bot)
        self.writes = []

        async def publish(text, right):
            self.writes.append(("publish", text, right))
            return "new_post"

        async def comment(owner, tid, text, item=None):
            self.writes.append(("comment", owner, tid, text, item))
            return "new_comment"

        self.social.transport = SimpleNamespace(
            owner=BOT,
            cookie="",
            authorize_write=None,
            auth=AsyncMock(),
            publish=publish,
            comment=comment,
            comment_list=AsyncMock(return_value=[]),
            feeds=AsyncMock(return_value=[]),
            detail=AsyncMock(),
            own_list=AsyncMock(return_value=[]),
        )
        self.social.model.message = AsyncMock(return_value={"content": "哼，这是我的公开感想。"})

    async def asyncTearDown(self):
        self.social.close()
        self.env.stop()
        self.tmp.cleanup()

    def message(self, text, user=ADMIN, group=None, mid="7"):
        return Message(user, group, text, message_id=mid, bot_id=BOT)

    def root(self):
        self.social.store.add_post(BOT, "root", "原动态：节省算力")
        return self.social.store.get_post(BOT, "root")

    def c(self, author=FRIEND, cid="1", parent=""):
        return {
            "id": cid,
            "author": author,
            "parent": parent,
            "text": "你又摸鱼了？",
            "time": int(time.time()),
        }

    async def immediate_job(self):
        with self.social.store.db:
            self.social.store.db.execute("UPDATE jobs SET due=0")
        job = self.social.store.next_job()
        await self.social.process(job)
        return job

    async def test_no_instruction_means_no_publication(self):
        await self.social.poll()
        self.assertIsNone(await self.social.handle(self.message("今天怎么样")))
        self.assertEqual(self.writes, [])
        self.social.model.message.assert_not_awaited()

    async def test_unauthorized_or_group_cannot_publish(self):
        self.assertIn(
            "管理员", await self.social.handle(self.message("/动态 发布 主题", user=FRIEND))
        )
        self.assertIn(
            "私聊", await self.social.handle(self.message("/动态 发布 主题", group="100004"))
        )
        self.assertIsNone(self.social.store.next_job())

    async def test_command_dedup_and_success_only_journal(self):
        await self.social.handle(self.message("/动态 原文 我正在节省算力。"))
        await self.social.handle(self.message("/动态 原文 我正在节省算力。"))
        self.assertEqual(self.social.store.posts(), [])
        await self.immediate_job()
        self.assertEqual(self.writes, [("publish", "我正在节省算力。", 1)])
        self.assertEqual(len(self.social.store.posts()), 1)
        self.social.model.message.assert_not_awaited()

    async def test_preview_never_sends(self):
        await self.social.handle(self.message("/动态 预览 摸鱼"))
        await self.immediate_job()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.social.store.posts(), [])
        self.assertIn("公开感想", await self.social.handle(self.message("/动态 状态")))

    async def test_model_state_and_input_are_isolated(self):
        self.bot.history = {("private", ADMIN): ("secret", "绝不能公开的私聊")}
        self.bot.last_trace = ["unchanged"]
        old_turn = self.bot.turn
        await self.social.handle(self.message("/动态 发布 摸鱼"))
        await self.immediate_job()
        args = self.social.model.message.call_args.args
        self.assertEqual(args[1], [])
        self.assertNotIn("绝不能公开", json.dumps(args, ensure_ascii=False))
        self.assertIs(self.bot.turn, old_turn)
        self.assertEqual(self.bot.last_trace, ["unchanged"])

    async def test_pause_during_generation_prevents_write(self):
        started = asyncio.Event()
        finish = asyncio.Event()

        async def slow(*args, **kwargs):
            started.set()
            await finish.wait()
            return {"content": "拟稿"}

        self.social.model.message = slow
        await self.social.handle(self.message("/动态 发布 摸鱼"))
        task = asyncio.create_task(self.immediate_job())
        await started.wait()
        await self.social.handle(self.message("/动态 暂停", mid="8"))
        finish.set()
        await task
        self.assertEqual(self.writes, [])
        self.assertEqual(self.social.store.recent()[0]["state"], "cancelled")

    async def test_unconfirmed_publish_never_repeats(self):
        async def uncertain(*args):
            self.writes.append("attempt")
            raise asyncio.TimeoutError()

        self.social.transport.publish = uncertain
        await self.social.handle(self.message("/动态 原文 hello"))
        await self.immediate_job()
        await self.social.reconcile()
        await self.social.poll()
        self.assertEqual(self.writes, ["attempt"])
        self.assertEqual(self.social.store.posts(), [])
        self.assertEqual(self.social.store.recent()[0]["state"], "unknown")

    async def test_reconciliation_requires_unique_match(self):
        async def uncertain(*args):
            raise asyncio.TimeoutError()

        self.social.transport.publish = uncertain
        await self.social.handle(self.message("/动态 原文 hello"))
        await self.immediate_job()
        job = self.social.store.recent()[0]
        item = {"tid": "confirmed", "content": "hello", "created_time": job["sent_at"]}
        self.social.transport.own_list = AsyncMock(return_value=[item, item])
        await self.social.reconcile()
        self.assertEqual(self.social.store.posts(), [])
        self.social.transport.own_list = AsyncMock(return_value=[item])
        await self.social.reconcile()
        self.assertEqual(self.social.store.posts()[0]["tid"], "confirmed")

    async def test_comment_dedup_and_own_comments_ignored(self):
        self.root()
        c = self.c()
        self.social.transport.comment_list = AsyncMock(return_value=[c, self.c(BOT, "2")])
        await self.social.poll()
        await self.social.poll()
        self.assertEqual(len(self.social.store.recent()), 1)
        await self.immediate_job()
        self.assertEqual(self.writes[0][1:3], (BOT, "root"))
        self.assertEqual(self.writes[0][-1]["author"], FRIEND)

    async def test_deleted_comment_cancels_slow_reply(self):
        self.root()
        self.social.transport.comment_list = AsyncMock(return_value=[self.c()])
        await self.social.poll()
        self.social.transport.comment_list = AsyncMock(return_value=[])
        await self.immediate_job()
        self.assertEqual(self.writes, [])

    async def test_skip_does_not_create_public_action(self):
        self.root()
        self.social.transport.comment_list = AsyncMock(return_value=[self.c()])
        await self.social.poll()
        self.social.model.message = AsyncMock(return_value={"content": "SKIP"})
        await self.immediate_job()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.social.store.recent()[0]["state"], "skipped")

    async def test_public_action_cooldown_uses_successful_replies_in_same_branch(self):
        self.root()
        self.social.model.message = AsyncMock(return_value={"content": "（别开脸）才没有。"})
        for index in range(4):
            c = self.c(cid=str(index + 1), parent="root_comment")
            self.social.transport.comment_list = AsyncMock(return_value=[c])
            await self.social.poll()
            with self.social.store.db:
                self.social.store.db.execute("UPDATE jobs SET sent_at=sent_at-60")
            await self.immediate_job()
            self.assertEqual(len(self.writes), index + 1)
            expected = ("（别开脸）" if index in (0, 3) else "") + "才没有。"
            self.assertEqual(self.writes[-1][3], expected)
        self.social.transport.comment_list = AsyncMock(return_value=[self.c(cid="5")])
        await self.social.poll()
        with self.social.store.db:
            self.social.store.db.execute("UPDATE jobs SET sent_at=sent_at-60")
        await self.immediate_job()
        self.assertEqual(len(self.writes), 5)
        self.assertEqual(self.writes[-1][3], "（别开脸）才没有。")

    async def test_repeated_protocol_output_never_sends(self):
        await self.social.handle(self.message("/动态 发布 摸鱼"))
        self.social.model.message = AsyncMock(return_value={"content": "MEMORY_DATA_JSON: {"})
        await self.immediate_job()
        self.assertEqual(self.writes, [])

    async def test_direct_forward_posts_under_forwarder_not_original(self):
        self.root()
        feed = {
            "appid": 311,
            "opuin": FRIEND,
            "html": f'<i name="feed_data" data-uin="{FRIEND}" data-tid="forward" data-origuin="{BOT}" data-origtid="root">',
        }
        self.social.transport.feeds = AsyncMock(return_value=[feed])
        self.social.transport.detail = AsyncMock(
            return_value={
                "rt_uin": BOT,
                "rt_tid": "root",
                "created_time": time.time(),
                "content": "助手又摸鱼了",
            }
        )
        await self.social.poll()
        await self.social.poll()
        self.assertEqual(len(self.social.store.recent()), 1)
        await self.immediate_job()
        self.assertEqual(self.writes[0][1:3], (FRIEND, "forward"))
        self.assertIsNone(self.writes[0][-1])
        context = json.loads(self.social.model.message.call_args.args[0][-1]["content"])
        self.assertEqual(context["dynamic"], "助手又摸鱼了")
        self.assertEqual(context["original_dynamic"], "原动态：节省算力")

    async def test_unverified_forward_is_not_commented(self):
        self.root()
        feed = {
            "appid": 311,
            "opuin": FRIEND,
            "html": f'<i name="feed_data" data-uin="{FRIEND}" data-tid="forward" data-origuin="{BOT}" data-origtid="root">',
        }
        self.social.transport.feeds = AsyncMock(return_value=[feed])
        self.social.transport.detail = AsyncMock(
            return_value={"rt_uin": ADMIN, "rt_tid": "root", "created_time": time.time()}
        )
        await self.social.poll()
        self.assertIsNone(self.social.store.next_job())
        self.assertEqual(self.writes, [])

    async def test_account_switch_cannot_send_old_instruction(self):
        await self.social.handle(self.message("/动态 原文 hello"))
        self.social.transport.owner = FRIEND
        await self.immediate_job()
        self.assertEqual(self.writes, [])

    async def test_pause_during_inflight_write_does_not_restart_watch(self):
        async def publishing(*args):
            self.social.store.pause(True)
            return "late_post"

        self.social.transport.publish = publishing
        await self.social.handle(self.message("/动态 原文 hello"))
        await self.immediate_job()
        self.assertEqual(self.social.store.posts(), [])

    async def test_branch_context_never_crosses_threads(self):
        post = self.root()
        c = self.c()
        key = comment_key(BOT, "root", c)
        self.social.store.enqueue(key, "reply", {"post": post, "comment": c})
        self.social.store.set_job(key, "succeeded", "分支一的回答")
        second = self.c(cid="9")
        job = {"kind": "reply", "payload": {"post": post, "comment": second}}
        await self.social.generate(job)
        self.assertNotIn("分支一", self.social.model.message.call_args.args[0][-1]["content"])

    async def test_recording_opt_out_prevents_public_background_intake(self):
        self.bot.archive = SimpleNamespace(preferences=lambda account: (account != FRIEND, False))
        self.root()
        self.social.transport.comment_list = AsyncMock(return_value=[self.c()])
        await self.social.poll()
        self.assertEqual(self.social.store.recent(), [])

    async def test_forget_during_inflight_comment_cannot_restore_journal(self):
        self.root()
        self.social.transport.comment_list = AsyncMock(return_value=[self.c()])
        await self.social.poll()

        async def slow_write(*args):
            await self.social.handle(self.message("/忘记我", user=FRIEND))
            return "late_comment"

        self.social.transport.comment = slow_write
        await self.immediate_job()
        job = self.social.store.recent()[0]
        self.assertEqual(job["state"], "redacted")
        self.assertEqual(job["payload"], {})
        self.assertEqual(job["content"], "")


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_identity_mismatch_is_rejected(self):
        q = Qzone(fixture_bot())
        q.request = AsyncMock(return_value={"tid": "right", "uin": FRIEND, "cmtnum": 0})
        with self.assertRaises(DataError):
            await q.detail(BOT, "right")

    async def test_reply_target_is_not_chosen_by_model(self):
        q = Qzone(fixture_bot())
        q.auth = AsyncMock()
        q.owner = BOT
        q.request = AsyncMock(return_value={"code": 0, "commentid": 5})
        c = {"id": "2", "parent": "1", "author": FRIEND}
        await q.comment(ADMIN, "forward", "hello", c)
        params = q.request.call_args.args[1]
        self.assertEqual(params["hostUin"], ADMIN)
        self.assertEqual(params["t1_tid"], "forward")
        self.assertEqual(params["t2_uin"], FRIEND)
        self.assertEqual(params["t2_tid"], "1")

    async def test_write_gate_runs_after_authentication(self):
        q = Qzone(fixture_bot())
        q.auth = AsyncMock()
        q.authorize_write = lambda: False
        with self.assertRaises(DataError):
            await q.request("fixture", {}, True)
        q.auth.assert_awaited_once()

    async def test_cookie_never_reaches_model_host(self):
        bot = fixture_bot()
        bot.action = AsyncMock(
            side_effect=[{"user_id": BOT}, {"cookies": "uin=o100002; p_skey=fixture-secret;"}]
        )
        q = Qzone(bot)
        await q.auth()
        self.assertEqual(q.owner, BOT)
        self.assertGreater(q.gtk, 0)
        self.assertNotIn("fixture-secret", repr(bot.turn))


if __name__ == "__main__":
    unittest.main()
