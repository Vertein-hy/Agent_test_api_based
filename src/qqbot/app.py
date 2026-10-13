"""Single QQ application and composition root.

One foreground worker owns Turn and serializes replies; background style/outbox
workers never modify tool or delivery state. Components are invoked explicitly,
so optional capabilities do not depend on inheritance or deployment overrides.
"""

import asyncio
from collections import OrderedDict
import logging
import os
from pathlib import Path
import time
import aiohttp
from aiohttp import web
from qqbot.config import secret, ids
from qqbot.domain import Turn, parse_event
from qqbot.chat_policy import scene_context, quoted_message, quoted_boundary
from qqbot.storage.archive import Archive
from qqbot.storage.style import StyleMemory
from qqbot.style_learning import StyleLearning
from qqbot.tools.registry import Registry
from qqbot.tools.panel import PanelTool
from qqbot.tools.client import ToolClient
from qqbot.memory.coordinator import MemoryCoordinator
from qqbot.memory.policy import display_name, panel
from qqbot.model.client import ModelClient
from qqbot.transport.onebot import OneBot
from qqbot.conversation import Conversation
from qqbot.commands import Commands
from qqbot.social.service import Social
from qqbot.continuity import CONTINUITY_BOUNDARY

log = logging.getLogger("qqbot")


class Bot:
    """Own foreground lifecycle and call composed capabilities in explicit order."""

    def __init__(self):
        self.session = None
        self.queue = asyncio.Queue(maxsize=50)
        self.history = OrderedDict()
        self.seen = OrderedDict()
        self.cooldown = OrderedDict()
        self.connected = False
        self.qq_online = False
        self.api_state = "unchecked"
        self.tasks = []
        self.allowed_users = ids("ALLOWED_USERS")
        self.allowed_groups = ids("ALLOWED_GROUPS")
        self.http_url = os.getenv("ONEBOT_HTTP_URL", "http://napcat:3000").rstrip("/")
        self.ws_url = os.getenv("ONEBOT_WS_URL", "ws://napcat:3001")
        self.api_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        self.model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
        self.thinking = os.getenv("DEEPSEEK_THINKING", "disabled")
        self.reasoning_effort = os.getenv("DEEPSEEK_REASONING_EFFORT", "high")
        if self.thinking not in ("enabled", "disabled") or self.reasoning_effort not in (
            "low",
            "high",
            "max",
        ):
            raise ValueError("Invalid thinking configuration")
        self.reported_model = None
        self.last_trace = []
        self.plugins = Registry()
        self.plugin_url = os.getenv("PLUGIN_RUNNER_URL", "").rstrip("/")
        self.archive = None
        archive_path = os.getenv("ARCHIVE_PATH")
        if archive_path:
            self.archive = Archive(
                archive_path,
                secret("ARCHIVE_SALT").encode(),
                default_capture=os.getenv("ARCHIVE_DEFAULT_CAPTURE", "0") == "1",
            )
        self.persona = os.getenv("SYSTEM_PROMPT") or (
            Path(
                os.getenv("PERSONA_FILE")
                or Path(__file__).parent / "resources" / "default_prompt.txt"
            )
            .read_text()
            .strip()
        )
        self.styles = StyleMemory(self.archive) if self.archive else None
        self.style_queue = asyncio.Queue(maxsize=100)
        self.style_pending = {}
        self.style_state = "ready" if self.styles else "disabled"

        self.turn = Turn()
        self._sender_hints = OrderedDict()
        self._arrivals = OrderedDict()
        self.panel = PanelTool(self)
        self.memory_flow = MemoryCoordinator(self)
        self.model_client = ModelClient(self)
        self.transport = OneBot(self)
        self.conversation = Conversation(self)
        self.style_learning = StyleLearning(self)
        self.tool_client = ToolClient(self)
        self.commands = Commands(self)
        self.social = Social(self)
        self.persona += CONTINUITY_BOUNDARY + self.social.capability_prompt

    async def action(self, action, params=None):
        return await self.transport.action(action, params)

    async def model_message(self, messages, tools, tool_choice=None, **kwargs):
        return await self.model_client.message(messages, tools, tool_choice, **kwargs)

    async def revise_chat(self, messages, content):
        return await self.conversation.revise_chat(messages, content)

    async def completion(self, messages):
        return await self.memory_flow.completion(messages)

    async def complete_chat(self, messages):
        working, panel_request = self.panel.prepare(messages)
        return await self.conversation.complete(working, panel_request)

    async def call_plugin(self, name, arguments):
        if name == "yunzai_query":
            self.turn.panel = True
            if self.turn.with_memory:
                return {"error": "面板查询需要在独立请求中继续，本轮不执行。"}
            return await self.panel.call(name, arguments)
        return await self.tool_client.call(name, arguments)

    async def quoted_context(self, message):
        """Begin the turn before quote lookup, including control commands."""
        key = (message.bot_id, message.group, message.user, message.message_id)
        now = time.monotonic()
        self.turn = Turn(
            message=message,
            panel=panel(message.text),
            display_name=self._sender_hints.pop(key, ""),
            started=now,
            queue_ms=(now - self._arrivals.pop(key, now)) * 1000,
        )
        self.panel.images = []
        self.panel.results = {}
        quoted = await self.transport.quoted(message)
        self.turn.quoted_speaker = quoted.get("speaker_role", "unknown") if quoted else "unknown"
        return quoted

    async def reply(self, message, text):
        text = await self.memory_flow.before_delivery(message, text)
        success = self.turn.success and message is self.turn.message
        self.turn.success = False
        try:
            images = self.panel.images if message is self.turn.message else []
            await self.transport.send(message, text, images)
        finally:
            self.panel.images = []
        self.memory_flow.after_delivery(message, text, success)

    def schedule_style(self, actor, scope, force=False):
        self.style_learning.schedule_style(actor, scope, force)

    async def learn_style(self, actor, scope, force=False):
        return await self.style_learning.learn_style(actor, scope, force)

    def accept(self, event):
        """Queue only fresh, authorized events; retain display hints only for accepted work."""
        message = parse_event(event, self.allowed_users, self.allowed_groups)
        if not message:
            return
        now = time.time()
        if float(event.get("time", now)) < now - 120:
            return
        identity = (str(event.get("self_id")), message.group, message.user, event.get("message_id"))
        if identity in self.seen:
            return
        self.seen[identity] = now
        while len(self.seen) > 2048:
            self.seen.popitem(last=False)
        if now - self.cooldown.get(message.key, 0) < 3:
            return
        self.cooldown[message.key] = now
        self.cooldown.move_to_end(message.key)
        while len(self.cooldown) > 512:
            self.cooldown.popitem(last=False)
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            log.warning("Message queue full; skipped event")
            return

        sender = event.get("sender") or {}
        if isinstance(sender, dict):
            key = (message.bot_id, message.group, message.user, message.message_id)
            self._sender_hints[key] = display_name(sender.get("card")) or display_name(
                sender.get("nickname")
            )
            self._arrivals[key] = time.monotonic()
            while len(self._sender_hints) > 512:
                self._sender_hints.popitem(last=False)
            while len(self._arrivals) > 512:
                self._arrivals.popitem(last=False)

    async def worker(self):
        """Serialize turns and commit conversation state only after successful QQ delivery."""
        while True:
            message = await self.queue.get()
            self.last_trace = []
            self.reported_model = None
            kind = "control"
            request = []
            effective_system = self.persona
            try:
                quoted = await self.quoted_context(message)
                if message.reply_only and (not quoted or not quoted["own"]):
                    continue
                answer = await self.social.handle(message)
                if answer is None:
                    answer = self.commands.handle(message)
                if answer is not None:
                    pass
                elif len(message.text) > 4000:
                    answer = "问题过长，请控制在4000字以内。"
                else:
                    kind = "chat"
                    effective_system += scene_context(message.text) + quoted_boundary(quoted)
                    if message.key not in self.history and self.archive:
                        persisted = self.archive.recent(message.user, message.group)
                        if persisted:
                            self.history[message.key] = (time.time(), persisted)
                    when, history = self.history.get(message.key, (0, []))
                    if time.time() - when > 1800:
                        history = []
                    if self.styles:
                        effective_system += self.styles.prompt(
                            self.archive.actor(message.user),
                            self.archive.scope(message.user, message.group),
                        )
                    request = [{"role": "system", "content": effective_system}] + history
                    if quoted and quoted["text"]:
                        request.append(quoted_message(quoted))
                    request += [{"role": "user", "content": message.text}]
                    answer = await self.completion(request)
                turn_id = (
                    self.archive.append(
                        message.user,
                        message.group,
                        effective_system,
                        message.text,
                        answer,
                        self.model,
                        self.reported_model,
                        self.last_trace,
                        kind,
                        request,
                    )
                    if self.archive
                    else None
                )
                reset_short = self.turn.reset_short
                await self.reply(message, answer)
                if kind == "chat" and not reset_short:
                    updated = (
                        history
                        + [
                            {"role": "user", "content": message.text},
                            {"role": "assistant", "content": answer},
                        ]
                    )[-12:]
                    self.history[message.key] = (time.time(), updated)
                    self.history.move_to_end(message.key)
                    while len(self.history) > 512:
                        self.history.popitem(last=False)
                if self.archive:
                    self.archive.delivered(turn_id)
                    if kind == "chat" and turn_id is not None:
                        self.schedule_style(
                            self.archive.actor(message.user),
                            self.archive.scope(message.user, message.group),
                        )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.turn.success = False
                self.panel.images = []
                # Never log request data, API response bodies, URLs, or credentials.
                log.warning("Reply failed (%s); API state=%s", type(error).__name__, self.api_state)
                try:
                    answer = "暂时无法回复，请稍后再试。"
                    turn_id = (
                        self.archive.append(
                            message.user,
                            message.group,
                            self.persona,
                            message.text,
                            answer,
                            self.model,
                            self.reported_model,
                            self.last_trace,
                            "error",
                            request,
                        )
                        if self.archive
                        else None
                    )
                    await self.reply(message, answer)
                    if self.archive:
                        self.archive.delivered(turn_id)
                except Exception:
                    log.warning("QQ delivery failed")
            finally:
                self.queue.task_done()

    async def health(self, request):
        return web.json_response(
            {
                "process": "running",
                "architecture": "composed-v1",
                "yunzai_enabled": self.panel.enabled,
                "memory_enabled": self.memory_enabled,
                "memory_transport": self.memory.state if self.memory else "disabled",
                "qq_connected": self.connected,
                "qq_online": self.qq_online,
                "deepseek": self.api_state,
                "model": self.model,
                "queue": self.queue.qsize(),
                "archive_enabled": self.archive is not None,
                "style_learning": self.style_state,
                "style_queue": self.style_queue.qsize(),
                "thinking": self.thinking,
                "reasoning_effort": self.reasoning_effort,
                "plugins": list(self.plugins.functions) if self.plugin_url else [],
                "social": self.social.state,
            }
        )

    async def start(self, app):
        """Start one foreground worker plus independently bounded background queues."""
        self.session = aiohttp.ClientSession()
        self.tasks = [
            asyncio.create_task(f())
            for f in (self.transport.listen, self.worker, self.transport.poll)
        ]
        if self.styles:
            self.tasks.append(asyncio.create_task(self.style_learning.style_worker()))
            for actor, scope in self.styles.pending_scopes():
                self.schedule_style(actor, scope, force=True)

        if self.memory:
            self.tasks.append(asyncio.create_task(self.memory.run(self.session)))
        if self.social.enabled:
            self.tasks.append(asyncio.create_task(self.social.run()))

    async def stop(self, app):
        """Cancel producers before closing sessions and persistent stores."""
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.session.close()
        if self.archive:
            self.archive.close()

        if self.memory:
            self.memory.close()
        self.social.close()
