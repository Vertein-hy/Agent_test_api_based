"""Explicit memory feedback, revision, plan and pause/delete controls."""

import hashlib
import json
import time
import uuid
import logging
from qqbot.memory.policy import checked_event, panel
from qqbot.memory.coordinator_types import OPERATIONS

log = logging.getLogger("qqbot")


class MemoryCommands:
    def __init__(self, bot, flow):
        self.bot = bot
        self.flow = flow

    async def operation_command(self, message):
        if not self.flow.enabled_for(message):
            return "记忆当前已暂停，请先开启记录并恢复记忆。"
        parts = message.text.split(maxsplit=2)
        command = parts[0]
        body = self.flow.context(message)
        if command == "/记忆反馈":
            if len(parts) != 2 or parts[1] not in ("有用", "无关"):
                return "用法：/记忆反馈 有用 或 /记忆反馈 无关，评价上一轮召回。"
            ident = self.bot.memory.recall_id(body["actor"], body["scope"])
            if not ident:
                return "当前会话还没有可评价的记忆召回。"
            self.bot.memory.enqueue(
                {**body, "recall_id": ident, "useful": parts[1] == "有用"}, "feedback"
            )
        else:
            if len(parts) < 2 or (command == "/记忆更正" and len(parts) != 3):
                return (
                    "用法："
                    + command
                    + " <记忆编号或具体内容>"
                    + (" <新内容>" if command == "/记忆更正" else "")
                    + "；/记忆 可以查看编号。"
                )
            target = parts[1] if command == "/记忆更正" else " ".join(parts[1:])
            source = hashlib.sha256(
                (
                    body["actor"]
                    + body["scope"]
                    + str(message.message_id or uuid.uuid4())
                    + message.text
                ).encode()
            ).hexdigest()
            event = checked_event(
                {"id": source, "created": time.time(), "question": message.text, "answer": ""}
            )
            if not event or panel(message.text):
                return "这类内容不能进入记忆更新。"
            self.bot.memory.enqueue(
                {
                    **body,
                    "event": event,
                    "update": {
                        "operation": OPERATIONS[command],
                        "target": target,
                        "evidence": event["question"],
                        "value": parts[2] if command == "/记忆更正" else "",
                    },
                },
                "update",
            )
        try:
            await self.bot.memory.flush(self.bot.session, 16)
        except Exception:
            return "更新已保存，服务恢复后会继续处理。"
        pending = self.bot.memory.db.execute(
            "SELECT 1 FROM outbox WHERE actor=? AND path IN ('update','feedback')", (body["actor"],)
        ).fetchone()
        if pending:
            return "更新已保存，服务恢复后会继续处理。"
        receipt = self.bot.memory.db.execute(
            "SELECT payload FROM receipts WHERE actor=?", (body["actor"],)
        ).fetchone()
        result = json.loads(receipt[0]) if receipt else {}
        if result.get("ok"):
            self.bot.turn.reset_short = command in (
                "/记忆删除",
                "/记忆更正",
                "/计划完成",
                "/计划取消",
            )
            return "已记录记忆反馈。" if command == "/记忆反馈" else "已更新这条记忆。"
        return "没有找到唯一对应的记忆，请用 /记忆 查看编号后再指定；没有改动其他记忆。"

    async def memory_command(self, message):
        if not self.bot.memory:
            return "长期记忆尚未启用。"
        actor = self.bot.archive.actor(message.user)
        capture = self.bot.archive.preferences(message.user)[0]
        if message.text == "/记忆":
            try:
                await self.bot.memory.flush(self.bot.session, 1)
                if self.bot.memory.has_control(actor):
                    return "记忆设置正在等待同步，当前不读取或写入记忆。"
                status = await self.bot.memory.request(
                    self.bot.session, "status", self.flow.context(message)
                )
                if status.get("stale"):
                    return "记忆状态需要管理员检查，当前不读取或写入记忆。"
                counts = status["counts"]
                facts = status.get("facts", [])
                text = "记忆" + ("已开启" if self.flow.enabled_for(message) else "已暂停")
                text += (
                    f"：本场景有 {counts.get('episode', 0)} 段经历；按 QQ 账号延续称呼和表达偏好。"
                )
                if facts:
                    text += "\n近期自述：" + "；".join(
                        ("[" + x["id"][:10] + "] " if x.get("id") else "") + x["text"]
                        for x in facts[:5]
                    )
                profile = status.get("profile", {})
                if profile.get("names"):
                    names = sorted(profile["names"], key=lambda n: n["key"] != "preferred_name")
                    text += "\n我记住的称呼：" + names[0]["value"]
                return (
                    text
                    + "\n/记忆删除 编号；/记忆更正 编号 新内容；/计划完成 内容；/计划取消 内容；/记忆保留 编号；/记忆淡化 编号；/记忆反馈 有用或无关。\n/记忆暂停、/记忆恢复、/记忆清空；/忘记我 同时删除存档与记忆。"
                )
            except Exception as e:
                log.warning("Memory status deferred (%s)", type(e).__name__)
                return "记忆服务暂时不可用，普通聊天可以继续。"
        if message.text == "/记忆恢复" and not capture:
            return "互动记录已关闭。先发送 /记录开启，再发送 /记忆恢复。"
        desired = message.text != "/记忆暂停"
        if message.text == "/记忆清空":
            desired = self.bot.memory.preference(actor)[1]
        self.bot.memory.control(actor, desired, capture, forget=message.text == "/记忆清空")
        await self.bot.memory.flush(self.bot.session, 1)
        pending = self.bot.memory.has_control(actor)
        if message.text == "/记忆清空":
            return (
                "已停止使用旧记忆，删除操作会在服务恢复后重试。"
                if pending
                else "已清空你的所有群聊及私聊长期记忆；原互动存档保留，不会自动重新导入。"
            )
        if message.text == "/记忆暂停":
            return "已暂停记忆的读取和写入，已有记忆保留。"
        return "记忆恢复设置等待同步，普通聊天可以继续。" if pending else "已恢复记忆的读取和写入。"
