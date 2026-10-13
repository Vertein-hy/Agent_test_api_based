"""Compose manual publication, public comment jobs and direct-repost discovery.

This worker owns its model state and journal. It reads only recording-consent flags,
never personal conversation archives,
retrieves memory, executes tools, or changes the foreground Turn. Controls run
in the foreground and invalidate queued/slow work before the next external write.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import time
from types import SimpleNamespace

from qqbot.config import secret
from qqbot.domain import Turn
from qqbot.continuity import limit_role_actions
from qqbot.model.client import ModelClient, RequestBudget
from qqbot.social.policy import SOCIAL_RULES, command, comment_key, public_context, visible_text
from qqbot.social.store import SocialStore
from qqbot.transport.qzone import Qzone
from qqbot.transport.qzone_data import forward_candidate

log = logging.getLogger("qqbot")


class Social:
    def __init__(self, bot):
        self.bot = bot
        self.enabled = os.getenv("SOCIAL_ENABLED", "0") == "1"
        self.admins = {x.strip() for x in secret("SOCIAL_ADMINS").split(",") if x.strip()}
        if any(not x.isdigit() or len(x) < 5 for x in self.admins):
            raise ValueError("Invalid social administrator configuration")
        self.right = int(os.getenv("SOCIAL_VISIBILITY", "4"))
        if self.right not in (1, 4, 64):
            raise ValueError("Invalid social visibility")
        self.replies = os.getenv("SOCIAL_REPLIES", "1") == "1"
        self.reposts = os.getenv("SOCIAL_REPOSTS", "1") == "1"
        self.store = (
            SocialStore(os.getenv("SOCIAL_PATH", "/app/archive/social.sqlite3"))
            if self.enabled
            else None
        )
        self.transport = Qzone(bot)
        self.state = "idle" if self.enabled and self.admins else "disabled"
        self.last_poll = 0
        self.poll_offset = 0
        self.quota_salt = secret("ARCHIVE_SALT").encode()
        self.host = SimpleNamespace(
            api_url=bot.api_url,
            model=bot.model,
            thinking="disabled",
            reasoning_effort=bot.reasoning_effort,
            api_state="unchecked",
            reported_model=None,
            turn=Turn(),
            session=None,
        )
        self.model = ModelClient(self.host)

    @property
    def capability_prompt(self):
        if not self.enabled or not self.admins:
            return ""
        return "\n可用能力：授权管理员私聊使用 /动态 发布、原文、预览、状态、暂停、恢复、停止或核对。普通聊天不能触发发布，只有已接受的明确指令才能安排任务；状态以实际任务结果为准。"

    async def handle(self, message):
        if self.store and message.text in ("/忘记我", "/记录关闭"):
            self.store.forget_actor(message.user, self.quota_actor(message.user))
        parsed = command(message.text)
        if parsed is None:
            return None
        if not self.enabled or not self.admins:
            return "空间动态功能尚未启用。"
        if message.user not in self.admins:
            return "只有已配置的管理员能管理空间动态。"
        # Public publishing instructions are deliberately explicit private commands.
        # A group mention or quote cannot accidentally authorize disclosure.
        if message.group:
            return "请私聊发送动态指令，避免把群聊内容误发到空间。"
        action, text = parsed
        if action == "help":
            return "/动态 发布 主题；/动态 原文 正文；/动态 预览 主题；/动态 状态；/动态 暂停；/动态 恢复；/动态 停止 动态ID；/动态 核对。仅私聊管理员可用。"
        if action in ("暂停", "恢复"):
            self.store.pause(action == "暂停")
            return (
                "已暂停发布和动态互动，未发送任务已取消。"
                if action == "暂停"
                else "已恢复动态功能；已取消的任务不会补发。"
            )
        if action == "停止":
            if not text or len(text) > 180:
                return "请提供 /动态 状态 中的动态ID。"
            self.store.stop(text)
            return "已停止跟进该动态及已发现的转发，未发送任务已取消。"
        if action == "状态":
            labels = {
                "queued": "排队",
                "generating": "拟稿中",
                "sending": "发送中",
                "unknown": "结果待核对",
                "succeeded": "完成",
                "failed": "失败",
                "cancelled": "已取消",
                "skipped": "已跳过",
                "redacted": "已清理",
            }
            rows = []
            for job in self.store.recent():
                label = {
                    "publish": "发布",
                    "preview": "预览",
                    "reply": "评论回复",
                    "repost": "转发评论",
                }.get(job["kind"], job["kind"])
                row = label + "：" + labels.get(job["state"], job["state"])
                if job["kind"] == "publish" and job["remote_id"]:
                    row += "，动态ID：" + job["remote_id"]
                if job["kind"] == "preview" and job["state"] == "succeeded":
                    row += "\n" + job["content"][:300]
                rows.append(row)
            return (
                ("已暂停" if self.store.paused else "运行中")
                + "；接口状态："
                + self.state
                + "\n"
                + ("\n".join(rows) or "暂无动态任务。")
                + "\n跟进中的原动态ID："
                + (
                    "、".join(
                        p["tid"]
                        for p in self.store.posts()
                        if p["owner"] == p["root_owner"] and p["tid"] == p["root_tid"]
                    )[:600]
                    or "暂无"
                )
            )
        if self.store.paused:
            return "动态功能已暂停，请先发送 /动态 恢复。"
        if action == "核对":
            self.last_poll = 0
            self.store.enqueue("reconcile:" + str(time.time_ns()), "reconcile", {})
            return "已安排只读核对；结果不明的动态和评论不会自动重发。"
        if len(text) > 1000:
            return "主题或正文请控制在1000字以内。"
        if not message.message_id or not message.bot_id:
            return "无法校验指令的消息编号，本次不发布。"
        if action == "原文":
            try:
                visible_text(text, 1000)
            except ValueError:
                return "正文为空、过长或含内部协议，未安排发布。"
        kind = "preview" if action == "预览" else "publish"
        key = hashlib.sha256((message.bot_id + ":" + message.message_id).encode()).hexdigest()
        payload = {
            "instruction": text or "就指定主题写一则简短公开文字",
            "verbatim": action == "原文",
            "bot_id": message.bot_id,
        }
        queued = self.store.enqueue(key, kind, payload, quota_actor=self.quota_actor(message.user))
        return (
            (
                "已安排预览，不会发布；稍后用 /动态 状态 查看。"
                if kind == "preview"
                else "已接受发布指令；稍后用 /动态 状态 查看结果。"
            )
            if queued
            else "该指令已处理或队列已满，请查看 /动态 状态。"
        )

    async def generate(self, job):
        self.host.session = self.bot.session
        self.host.turn = Turn()
        p = job["payload"]
        branch = []
        if job["kind"] in ("publish", "preview"):
            task = {
                "task": "根据管理员提供的主题拟写50至180字公开正文，不使用SKIP。",
                "topic": p["instruction"],
            }
        else:
            post = p["post"]
            c = p.get("comment")
            branch = (
                self.store.branch(post["owner"], post["tid"], c["parent"] or c["id"]) if c else []
            )
            task = public_context(post, c, branch)
            task["task"] = (
                "仅回应给定评论，限制一至两句；不需回复则返回SKIP。"
                if c
                else "按已核验直接转发的附言生成一至两句评论；不需回复则返回SKIP。"
            )
            if not c:
                root = self.store.get_post(post["root_owner"], post["root_tid"])
                task["original_dynamic"] = root["content"] if root else ""
        messages = [
            {
                "role": "system",
                "content": self.bot.persona + "\n" + SOCIAL_RULES,
            },
            {"role": "user", "content": json.dumps(task, ensure_ascii=False)},
        ]
        reply = await self.model.message(
            messages, [], thinking="disabled", _budget=RequestBudget(time.monotonic() + 45)
        )
        if reply.get("tool_calls"):
            raise ValueError("Unexpected social tool call")
        question = p.get("instruction") or p.get("comment", {}).get("text", "")
        text = visible_text(
            limit_role_actions(reply.get("content") or "", question, [x["reply"] for x in branch]),
            1000 if job["kind"] in ("publish", "preview") else 300,
        )
        if text == "SKIP" and job["kind"] in ("publish", "preview"):
            raise ValueError("Missing publication")
        return text

    async def process(self, job):
        if job["kind"] == "reconcile":
            await self.reconcile()
            self.store.set_job(job["key"], "succeeded")
            return
        if not self.store.active(job):
            self.store.set_job(job["key"], "cancelled")
            return
        self.store.set_job(job["key"], "generating")
        try:
            text = job["content"] or (
                visible_text(job["payload"]["instruction"], 1000)
                if job["payload"].get("verbatim")
                else await self.generate(job)
            )
            if not self.store.active(job):
                self.store.set_job(job["key"], "cancelled")
                return
            if text == "SKIP":
                self.store.set_job(job["key"], "skipped")
                return
            if job["kind"] == "preview":
                self.store.set_job(job["key"], "succeeded", text)
                return
            await self.authenticate()
            if job["kind"] == "publish" and job["payload"]["bot_id"] != self.transport.owner:
                raise ValueError("Publishing account mismatch")
            if job["kind"] == "reply":
                p = job["payload"]
                post = p["post"]
                c = p["comment"]
                if not self.recording_allowed(c["author"]):
                    self.store.forget_actor(c["author"], job["quota_actor"])
                    return
                current = await self.transport.comment_list(post["owner"], post["tid"])
                if not any(
                    comment_key(post["owner"], post["tid"], x) == job["key"] for x in current
                ):
                    self.store.set_job(job["key"], "cancelled")
                    return
            elif job["kind"] == "repost":
                p = job["payload"]["post"]
                if not self.recording_allowed(p["owner"]):
                    self.store.forget_actor(p["owner"], job["quota_actor"])
                    return
                detail = await self.transport.detail(p["owner"], p["tid"])
                if (str(detail.get("rt_uin")), str(detail.get("rt_tid"))) != (
                    p["root_owner"],
                    p["root_tid"],
                ):
                    self.store.set_job(job["key"], "cancelled")
                    return
            if not self.store.active(job):
                self.store.set_job(job["key"], "cancelled")
                return
            self.store.set_job(job["key"], "generating", text)
            if not self.store.reserve_send(job):
                return
            expected_owner = self.transport.owner
            self.transport.authorize_write = lambda: (
                self.transport.owner == expected_owner and self.store.write_allowed(job)
            )
            # No await between final generation check/reservation and starting the write.
            try:
                if job["kind"] == "publish":
                    remote = await self.transport.publish(text, self.right)
                    self.store.add_post(self.transport.owner, remote, text)
                    if job["generation"] != self.store.generation:
                        with self.store.db:
                            self.store.db.execute(
                                "UPDATE posts SET active=0 WHERE owner=? AND tid=?",
                                (self.transport.owner, remote),
                            )
                else:
                    p = job["payload"]
                    post = p["post"]
                    remote = await self.transport.comment(
                        post["owner"], post["tid"], text, p.get("comment")
                    )
                self.store.set_job(job["key"], "succeeded", text, remote)
                self.state = "ready"
                log.info("social_delivery kind=%s outcome=confirmed", job["kind"])
            except asyncio.CancelledError:
                self.store.set_job(job["key"], "unknown")
                raise
            except Exception as error:
                self.store.set_job(job["key"], "unknown")
                self.state = "write_unconfirmed"
                log.warning(
                    "social_delivery kind=%s outcome=unknown category=%s",
                    job["kind"],
                    type(error).__name__,
                )
            finally:
                self.transport.authorize_write = None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.store.set_job(job["key"], "failed")
            self.state = "task_failed"
            log.warning("social_task outcome=failed category=%s", type(error).__name__)

    async def authenticate(self):
        await self.transport.auth()
        self.store.bind_owner(self.transport.owner)

    def quota_actor(self, account):
        return hmac.new(self.quota_salt, account.encode(), hashlib.sha256).hexdigest()

    def recording_allowed(self, account):
        archive = getattr(self.bot, "archive", None)
        return archive.preferences(account)[0] if archive else True

    async def poll(self):
        posts = self.store.posts()
        if not posts or self.store.paused:
            return
        await self.authenticate()
        generation = self.store.generation
        self.state = "ready"
        if self.reposts:
            try:
                await asyncio.wait_for(self.discover_reposts(posts, generation), 35)
            except Exception as error:
                self.state = "reposts_unavailable"
                log.warning("social_reposts outcome=unavailable category=%s", type(error).__name__)
        if self.replies:
            try:
                await asyncio.wait_for(self.poll_comments(posts, generation), 45)
            except Exception as error:
                self.state = "comments_unavailable"
                log.warning("social_comments outcome=unavailable category=%s", type(error).__name__)

    async def poll_comments(self, posts, generation):
        batch = (posts[self.poll_offset :] + posts[: self.poll_offset])[:8]
        for post in batch:
            self.poll_offset = (posts.index(post) + 1) % len(posts)
            try:
                values = await self.transport.comment_list(post["owner"], post["tid"])
            except Exception as error:
                log.warning("social_comments outcome=unavailable category=%s", type(error).__name__)
                self.state = "comments_unavailable"
                continue
            if generation != self.store.generation or self.store.paused:
                return
            for c in values:
                if (
                    c["author"] == self.transport.owner
                    or not c["text"].strip()
                    or not self.recording_allowed(c["author"])
                ):
                    continue
                key = comment_key(post["owner"], post["tid"], c)
                quota = self.quota_actor(c["author"])
                self.store.enqueue(
                    key, "reply", {"post": post, "comment": c}, random.uniform(60, 180), quota
                )

    async def discover_reposts(self, posts, generation):
        roots = {
            (p["owner"], p["tid"]): p
            for p in posts
            if p["owner"] == self.transport.owner and p["root_tid"] == p["tid"]
        }
        for feed in await self.transport.feeds():
            p = forward_candidate(feed)
            if not p or p["owner"] == self.transport.owner:
                continue
            if not self.recording_allowed(p["owner"]):
                continue
            root = roots.get((p["root_owner"], p["root_tid"]))
            if not root or self.store.get_post(p["owner"], p["tid"]):
                continue
            detail = await self.transport.detail(p["owner"], p["tid"])
            if generation != self.store.generation or self.store.paused:
                return
            if (str(detail.get("rt_uin")), str(detail.get("rt_tid"))) != (
                p["root_owner"],
                p["root_tid"],
            ):
                continue
            created = float(detail.get("created_time") or 0)
            if created < root["created"] - 5 or created < time.time() - 7 * 86400:
                continue
            self.store.add_post(
                p["owner"],
                p["tid"],
                str(detail.get("content") or "")[:1500],
                created,
                (p["root_owner"], p["root_tid"]),
            )
            post = self.store.get_post(p["owner"], p["tid"])
            if post:
                key = "repost:" + hashlib.sha256((p["owner"] + ":" + p["tid"]).encode()).hexdigest()
                self.store.enqueue(
                    key,
                    "repost",
                    {"post": post},
                    random.uniform(60, 180),
                    self.quota_actor(p["owner"]),
                )

    async def reconcile(self):
        """Only a unique matching platform record can resolve an uncertain write."""
        jobs = self.store.recent("unknown")
        if not jobs:
            return
        await self.authenticate()
        own = None
        for job in jobs:
            if job["kind"] == "publish":
                if own is None:
                    own = await self.transport.own_list()
                matches = [
                    x
                    for x in own
                    if str(x.get("content")) == job["content"]
                    and abs(float(x.get("created_time") or 0) - job["sent_at"]) < 180
                ]
                if len(matches) == 1 and matches[0].get("tid"):
                    tid = str(matches[0]["tid"])
                    # Pausing during a write must not re-enable its watcher.
                    self.store.add_post(
                        self.transport.owner, tid, job["content"], matches[0]["created_time"]
                    )
                    if job["generation"] != self.store.generation:
                        with self.store.db:
                            self.store.db.execute(
                                "UPDATE posts SET active=0 WHERE owner=? AND tid=?",
                                (self.transport.owner, tid),
                            )
                    self.store.set_job(job["key"], "succeeded", remote_id=tid)
            else:
                post = job["payload"]["post"]
                source = job["payload"].get("comment")
                values = await self.transport.comment_list(post["owner"], post["tid"])
                matches = [
                    c
                    for c in values
                    if c["author"] == self.transport.owner
                    and c["text"] == job["content"]
                    and abs(c["time"] - job["sent_at"]) < 180
                    and (not source or c["parent"] == (source["parent"] or source["id"]))
                ]
                if len(matches) == 1:
                    self.store.set_job(job["key"], "succeeded", remote_id=matches[0]["id"])

    async def run(self):
        if not self.enabled or not self.admins:
            return
        while True:
            try:
                if not self.store.paused:
                    job = self.store.next_job()
                    if job:
                        await self.process(job)
                    if time.monotonic() - self.last_poll >= 120:
                        self.last_poll = time.monotonic()
                        try:
                            await asyncio.wait_for(self.reconcile(), 30)
                        except Exception as error:
                            log.warning(
                                "social_reconcile outcome=unavailable category=%s",
                                type(error).__name__,
                            )
                        await self.poll()
                        self.store.prune()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.state = "read_unavailable"
                log.warning("social_poll outcome=failed category=%s", type(error).__name__)
            await asyncio.sleep(5)

    def close(self):
        self.transport.cookie = ""
        if self.store:
            self.store.close()
