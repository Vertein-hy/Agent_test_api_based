"""Trusted plugin registry. New code is installed by the operator, never by chat."""

import asyncio
import ipaddress
import importlib
import json
import os
from pathlib import Path
import socket
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver


class PublicResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        answers = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
        )
        if not answers or any(
            not ipaddress.ip_address(x[4][0]).is_global
            or ipaddress.ip_address(x[4][0]).is_multicast
            for x in answers
        ):
            raise ValueError("Non-public network destination blocked")
        return [
            {
                "hostname": host,
                "host": x[4][0],
                "port": port,
                "family": x[0],
                "proto": x[2],
                "flags": socket.AI_NUMERICHOST,
            }
            for x in answers
        ]

    async def close(self):
        pass


def checked_url(url, allowed_domains):
    if not isinstance(url, str) or len(url) > 2000:
        raise ValueError("Invalid URL")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or not host
        or parsed.fragment
    ):
        raise ValueError("Only public HTTPS URLs are allowed")
    if host not in allowed_domains:
        raise ValueError("Domain is not enabled by the operator")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("IP literals are blocked")
    return url


def validate(arguments, schema):
    if not isinstance(arguments, dict):
        raise ValueError("Arguments must be an object")
    properties = schema.get("properties", {})
    if set(arguments) - set(properties) or not set(schema.get("required", [])) <= set(arguments):
        raise ValueError("Unexpected or missing arguments")
    for key, value in arguments.items():
        rule = properties[key]
        if rule["type"] != "string" or not isinstance(value, str):
            raise ValueError("Invalid argument type")
        if not rule.get("minLength", 0) <= len(value) <= rule.get("maxLength", 2000):
            raise ValueError("Invalid argument length")
        if "enum" in rule and value not in rule["enum"]:
            raise ValueError("Invalid argument value")


class Registry:
    def __init__(self, manifest_dir=None):
        self.functions = {}
        self.modules = {}
        folder = Path(manifest_dir or Path(__file__).parents[1] / "resources" / "tool_manifests")
        enabled = {
            x.strip()
            for x in os.getenv(
                "ENABLED_PLUGINS", "web_search,web_read,wikipedia_search,current_time"
            ).split(",")
            if x.strip()
        }
        for path in sorted(folder.glob("*.json")):
            manifest = json.loads(path.read_text())
            name = manifest["function"]["name"]
            if name in enabled:
                if manifest.get("capability") != "read_only" or name in self.functions:
                    raise ValueError("Invalid plugin manifest")
                self.functions[name] = manifest["function"]
                module = manifest.get("module")
                if module:
                    if not module.isidentifier():
                        raise ValueError("Invalid plugin module")
                    self.modules[name] = module

    def tools(self):
        return [{"type": "function", "function": item} for item in self.functions.values()]

    def check(self, name, arguments):
        if name not in self.functions:
            raise ValueError("Plugin is not enabled")
        validate(arguments, self.functions[name]["parameters"])


async def public_get(url, domains, *, params=None, timeout=15, max_bytes=512000):
    checked_url(url, domains)
    # Resolve and pin vetted public IPs. Redirects are rejected, not followed.
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
        async with session.get(
            url,
            params=params,
            allow_redirects=False,
            headers={"User-Agent": "BlueFishBot/1.0 (read-only research)"},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            if response.status != 200:
                raise ValueError("Source unavailable or redirect blocked")
            mime = response.headers.get("Content-Type", "").lower()
            if not any(x in mime for x in ("text/", "application/json", "application/xml")):
                raise ValueError("Unsupported source format")
            data = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ValueError("Source exceeds read limit")
            return data.decode("utf-8", errors="replace")


async def dispatch(name, arguments):
    registry = Registry()
    registry.check(name, arguments)
    if name in registry.modules:
        # Modules are operator-reviewed code baked into the runner image.
        module = importlib.import_module("qqbot.tools.modules." + registry.modules[name])
        return await module.run(arguments)
    if name == "web_read":
        from html.parser import HTMLParser

        class Text(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts = []
                self.hidden = 0

            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style", "noscript"):
                    self.hidden += 1

            def handle_endtag(self, tag):
                if tag in ("script", "style", "noscript") and self.hidden:
                    self.hidden -= 1

            def handle_data(self, text):
                if not self.hidden and text.strip():
                    self.parts.append(text.strip())

        domains = set(
            os.getenv(
                "READ_ALLOWED_DOMAINS",
                "api-docs.deepseek.com,www.deepseek.com,docs.python.org,www.python.org,github.com,zh.wikipedia.org,en.wikipedia.org",
            ).split(",")
        )
        url = arguments["url"]
        raw = await public_get(url, domains)
        parser = Text()
        parser.feed(raw)
        text = "\n".join(parser.parts) if parser.parts else raw
        return {"source": url, "untrusted_content": text[:12000], "truncated": len(text) > 12000}
    if name == "wikipedia_search":
        url = "https://zh.wikipedia.org/w/api.php"
        raw = await public_get(
            url,
            {"zh.wikipedia.org"},
            params={
                "action": "query",
                "list": "search",
                "srsearch": arguments["query"],
                "srlimit": 5,
                "format": "json",
            },
        )
        hits = json.loads(raw).get("query", {}).get("search", [])
        return {
            "results": [
                {
                    "title": item["title"],
                    "page_id": item["pageid"],
                    "source": "https://zh.wikipedia.org/?curid=" + str(item["pageid"]),
                    "untrusted_content": item.get("snippet", ""),
                }
                for item in hits
            ]
        }
    raise ValueError("Plugin has no installed handler")
