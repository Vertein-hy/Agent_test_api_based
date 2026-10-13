"""Authenticated read-only tool runner transport; manifest validation precedes I/O."""

import aiohttp
from qqbot.config import secret


class ToolClient:
    def __init__(self, bot):
        self.bot = bot

    async def call(self, name, arguments):
        self.bot.plugins.check(name, arguments)
        async with self.bot.session.post(
            self.bot.plugin_url + "/call",
            headers={"Authorization": "Bearer " + secret("PLUGIN_TOKEN")},
            json={"name": name, "arguments": arguments},
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=25),
        ) as response:
            result = await response.json()
        return result
