"""OneBot HTTP and quote validation. Segment arrays keep model CQ strings literal."""

import asyncio
import json
import logging
import aiohttp

log = logging.getLogger("qqbot")
from qqbot.config import secret


class OneBot:
    def __init__(self, bot):
        self.bot = bot

    async def action(self, action, params=None):
        async with self.bot.session.post(
            self.bot.http_url + "/" + action,
            headers={"Authorization": "Bearer " + secret("ONEBOT_TOKEN")},
            json=params or {},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            response.raise_for_status()
            result = await response.json()
            if result.get("status") != "ok" or result.get("retcode", 0) != 0:
                raise RuntimeError("OneBot action failed")
            return result.get("data")

    async def quoted(self, message):
        if not message.group or not message.reply_id:
            return None
        try:
            quoted = await self.bot.action("get_msg", {"message_id": int(message.reply_id)})
            if (
                not isinstance(quoted, dict)
                or str(quoted.get("group_id")) != message.group
                or quoted.get("message_type") != "group"
            ):
                return None
            own = str(quoted.get("sender", {}).get("user_id")) == message.bot_id
            sender = str(quoted.get("sender", {}).get("user_id") or "")
            speaker_role = (
                "bot"
                if own
                else "current_speaker"
                if sender == message.user
                else "other_member"
                if sender
                else "unknown"
            )
            if message.reply_only and not own:
                return None
            segments = quoted.get("message")
            if not isinstance(segments, list):
                return None
            text = "".join(
                str(x.get("data", {}).get("text", "")) for x in segments if x.get("type") == "text"
            )[:1500]
            return {"text": text, "own": own, "speaker_role": speaker_role}
        except Exception:
            return None

    async def send(self, message, text, images=()):
        """One successful action is the delivery boundary for archive and memory."""
        parts = []
        if message.group:
            if message.message_id:
                parts.append({"type": "reply", "data": {"id": message.message_id}})
            parts.extend(
                [
                    {"type": "at", "data": {"qq": message.user}},
                    {"type": "text", "data": {"text": "\n"}},
                ]
            )
        parts.append({"type": "text", "data": {"text": text[:3500]}})
        parts.extend({"type": "image", "data": {"file": "base64://" + i["base64"]}} for i in images)
        action = "send_group_msg" if message.group else "send_private_msg"
        target = (
            {"group_id": int(message.group)} if message.group else {"user_id": int(message.user)}
        )
        await self.bot.action(action, {**target, "message": parts})

    async def listen(self):
        while True:
            try:
                async with self.bot.session.ws_connect(
                    self.bot.ws_url,
                    headers={"Authorization": "Bearer " + secret("ONEBOT_TOKEN")},
                    heartbeat=30,
                    max_msg_size=1024 * 1024,
                ) as ws:
                    self.bot.connected = True
                    log.info("OneBot connected")
                    async for frame in ws:
                        if frame.type == aiohttp.WSMsgType.TEXT:
                            try:
                                self.bot.accept(json.loads(frame.data))
                            except (ValueError, TypeError, KeyError, AttributeError):
                                log.warning("Invalid OneBot event skipped")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.info("Waiting for QQ connection (%s)", type(error).__name__)
            finally:
                self.bot.connected = False
            await asyncio.sleep(5)

    async def poll(self):
        while True:
            try:
                result = await self.bot.action("get_status")
                self.bot.qq_online = bool(result and result.get("online"))
            except Exception:
                self.bot.qq_online = False
            await asyncio.sleep(15)
