"""Background extraction of bounded expression preferences, never model training."""

import asyncio
import json
import aiohttp
from qqbot.config import secret
from qqbot.storage.style import EXTRACT_PROMPT, OPTIONS
import logging

log = logging.getLogger("qqbot")


class StyleLearning:
    def __init__(self, bot):
        self.bot = bot

    def schedule_style(self, actor, scope, force=False):
        if not self.bot.styles or not self.bot.styles.enabled(actor):
            return
        key = (actor, scope)
        if key in self.bot.style_pending:
            self.bot.style_pending[key] |= force
            return
        try:
            self.bot.style_queue.put_nowait(key)
            self.bot.style_pending[key] = force
        except asyncio.QueueFull:
            log.warning("Style queue full; update deferred")

    async def learn_style(self, actor, scope, force=False):
        if not self.bot.styles.enabled(actor):
            return 0
        messages = self.bot.styles.messages(actor, scope, force)
        if not messages:
            return 0
        profile, _, _ = self.bot.styles.profile(scope)
        payload = {
            "model": self.bot.model,
            "messages": [
                {"role": "system", "content": EXTRACT_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "allowed_options": {k: list(v) for k, v in OPTIONS.items()},
                            "current_preferences": profile,
                            "user_messages": messages,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "max_tokens": 700,
            "stream": False,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
        }
        async with self.bot.session.post(
            self.bot.api_url + "/chat/completions",
            headers={"Authorization": "Bearer " + secret("DEEPSEEK_API_KEY")},
            json=payload,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=25),
        ) as response:
            response.raise_for_status()
            result = await response.json()
        proposals = json.loads(result["choices"][0]["message"]["content"])
        return self.bot.styles.apply(actor, scope, proposals, messages)

    async def style_worker(self):
        while True:
            actor, scope = await self.bot.style_queue.get()
            key = (actor, scope)
            succeeded = False
            try:
                await self.learn_style(actor, scope, self.bot.style_pending.get(key, False))
                self.bot.style_state = "ready"
                succeeded = True
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Style learning failures must not prevent normal QQ replies.
                self.bot.style_state = "retry_on_next_message"
                log.warning("Style update deferred (%s)", type(error).__name__)
            finally:
                self.bot.style_pending.pop(key, None)
                self.bot.style_queue.task_done()
                if (
                    succeeded
                    and self.bot.styles.enabled(actor)
                    and self.bot.styles.messages(actor, scope)
                ):
                    self.schedule_style(actor, scope)
