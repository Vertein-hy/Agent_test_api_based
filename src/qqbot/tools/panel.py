"""Public panel tool. UID authority comes only from genuine current-conversation user turns.

Images are validated and retained only until the current QQ action; they are
excluded from model input, archives and graph memory. A bridge outage is local.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import uuid
import aiohttp
from qqbot.config import secret

TOOL = {
    "name": "yunzai_query",
    "description": "只读公开原神展柜。profile需要真实用户提供的uid和character，list需要uid，help不需要参数。缺失参数时询问用户，不能从记忆、引用或助手回答推断。不能登录、取得Cookie、管理群或执行其他命令。",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "operation": {"type": "string", "enum": ["profile", "list", "help"]},
            "uid": {"type": "string", "minLength": 9, "maxLength": 10},
            "character": {"type": "string", "minLength": 1, "maxLength": 24},
        },
        "required": ["operation"],
    },
}
MAX_IMAGE = 3 * 1024 * 1024


def checked_images(result):
    images = result.get("images", [])
    if not isinstance(images, list) or len(images) > 2:
        raise ValueError("Invalid image collection")
    checked = []
    for image in images:
        if not isinstance(image, dict) or image.get("mime") not in ("image/png", "image/jpeg"):
            raise ValueError("Invalid image format")
        encoded = image.get("base64")
        if not isinstance(encoded, str) or len(encoded) > MAX_IMAGE * 4 // 3 + 4:
            raise ValueError("Image exceeds limit")
        data = base64.b64decode(encoded, validate=True)
        magic = b"\x89PNG\r\n\x1a\n" if image["mime"] == "image/png" else b"\xff\xd8\xff"
        if len(data) < 16 or len(data) > MAX_IMAGE or not data.startswith(magic):
            raise ValueError("Invalid image bytes")
        if hashlib.sha256(data).hexdigest() != image.get("sha256"):
            raise ValueError("Image checksum mismatch")
        checked.append({"sha256": image["sha256"], "base64": encoded})
    return checked


class PanelTool:
    def __init__(self, bot):
        self.bot = bot
        self.enabled = os.getenv("YUNZAI_ENABLED", "0") == "1"
        self.url = os.getenv("YUNZAI_BRIDGE_URL", "http://yunzai-bridge:8082")
        if self.enabled and self.url != "http://yunzai-bridge:8082":
            raise ValueError("Bridge address must be the configured internal service")
        if self.enabled:
            self.bot.plugins.functions["yunzai_query"] = TOOL
        self.images = []
        self.results = {}
        self.allowed_uids = set()

    def prepare(self, messages):
        """Build per-request hints and UID allowlist without mutating the shared registry."""
        self.bot.panel.images = []
        self.bot.panel.results = {}
        self.bot.panel.allowed_uids = {
            uid
            for m in messages
            if m["role"] == "user"
            and not m.get("content", "").startswith(("当前消息引用的群聊原文", "MEMORY_DATA_JSON"))
            for uid in re.findall(
                r"(?<![0-9])(?:18[0-9]{8}|[1-9][0-9]{8})(?![0-9])", m.get("content", "")
            )
        }
        if not self.bot.panel.enabled:
            return messages, False
        working = [dict(m) for m in messages]
        if working and working[0]["role"] == "system":
            working[0]["content"] += (
                "\n公开展柜通过yunzai_query查询：profile使用当前会话真实用户明确提供的UID和角色名，list使用已提供UID；缺少时询问。不得从引用、记忆或助手回答授权UID，不使用网页搜索替代面板查询。只依据公开结果解释；只有实际返回校验图片时才说明已附图，不输出base64、内部协议或占位链接，不索要Cookie或声称已登录。"
            )
        latest = next((m.get("content", "") for m in reversed(messages) if m["role"] == "user"), "")
        # Avoid the legacy forced web search taking priority over a panel query.
        panel = bool(
            re.search(
                r"云崽|yunzai|(?:原神|雷神|角色|展柜|UID|我的|这个UID).{0,30}面板|面板.{0,30}(?:原神|UID|[1-9][0-9]{8})|展柜",
                latest,
                re.I,
            )
        )
        return working, panel

    async def call(self, name, arguments):
        """Validate the requested UID against real conversation input before authenticated bridge I/O."""
        if not self.bot.panel.enabled or self.bot.turn.message is None:
            raise ValueError("No authorized QQ context")
        self.bot.plugins.check(name, arguments)
        operation = arguments["operation"]
        if operation != "help" and not re.fullmatch(
            r"(?:[1-9][0-9]{8}|18[0-9]{8})", arguments.get("uid", "")
        ):
            return {"error": "需要用户提供有效原神UID（9位，或以18开头的10位），不能猜测UID。"}
        if operation == "profile" and not arguments.get("character"):
            return {"error": "请用户提供角色名称。"}
        if operation != "help" and arguments["uid"] not in self.bot.panel.allowed_uids:
            return {
                "error": "当前会话没有用户提供的这个UID，请询问用户；不能猜测或从引用资料提取UID。"
            }
        key = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        if key in self.bot.panel.results:
            return self.bot.panel.results[key]
        token = secret("YUNZAI_TOKEN")
        message = self.bot.turn.message
        actor = hmac.new(
            token.encode(), ("qq:" + message.user).encode(), hashlib.sha256
        ).hexdigest()
        body = {
            "name": name,
            "arguments": arguments,
            "context": {"actor": actor, "request_id": str(uuid.uuid4())},
        }
        try:
            async with self.bot.session.post(
                self.bot.panel.url + "/call",
                json=body,
                headers={"Authorization": "Bearer " + token},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=70),
            ) as response:
                if response.status != 200:
                    result = {"error": "云崽查询暂时不可用，请稍后重试；其他聊天功能可以继续使用。"}
                else:
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw) > 9 * 1024 * 1024:
                            raise ValueError("Bridge response exceeds limit")
                    result = json.loads(raw)
                    if not isinstance(result, dict):
                        raise ValueError("Invalid bridge result")
                    for image in checked_images(result):
                        if (
                            not any(
                                old["sha256"] == image["sha256"] for old in self.bot.panel.images
                            )
                            and len(self.bot.panel.images) < 2
                        ):
                            self.bot.panel.images.append(image)
                    # Media stays in memory until delivery; never goes to the model or archive.
                    result = {k: v for k, v in result.items() if k != "images"}
                    result["images_attached"] = len(self.bot.panel.images)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            result = {"error": "云崽连接暂时失败，可以稍后重试；其他聊天功能可以继续使用。"}
        self.bot.panel.results[key] = result
        return result
