"""Separate read-only plugin service: no API key, QQ token or archive mount."""

import asyncio
import hmac
import json
import os
from pathlib import Path

from aiohttp import web

from qqbot.tools.registry import Registry, dispatch


def make_app():
    registry = Registry()
    token = Path(os.getenv("PLUGIN_TOKEN_FILE", "/run/secrets/plugin_token")).read_text().strip()

    @web.middleware
    async def authenticate(request, handler):
        if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token):
            raise web.HTTPUnauthorized()
        return await handler(request)

    async def call(request):
        try:
            body = await request.json()
            registry.check(body["name"], body["arguments"])
            result = await asyncio.wait_for(dispatch(body["name"], body["arguments"]), 20)
            return web.json_response({"result": result})
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            return web.json_response(
                {"error": "插件参数、域名或权限不允许，或来源不可读。"}, status=400
            )
        except Exception:
            return web.json_response({"error": "查询来源暂时不可用。"}, status=502)

    app = web.Application(middlewares=[authenticate], client_max_size=8192)
    app.router.add_post("/call", call)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=8081, access_log=None)
