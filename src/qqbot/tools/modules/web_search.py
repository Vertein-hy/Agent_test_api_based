"""Bounded public web search. No credentials, redirects or arbitrary URL fetches."""

import base64
from datetime import datetime, timezone
from html.parser import HTMLParser
import ipaddress
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

from qqbot.tools.registry import public_get


def source_url(value):
    try:
        parts = urlsplit(value)
        # Decode Bing's destination locally; never follow the redirect endpoint.
        if parts.hostname == "www.bing.com" and parts.path == "/ck/a":
            encoded = parse_qs(parts.query).get("u", [""])[0]
            if not encoded.startswith("a1"):
                return None
            encoded = encoded[2:]
            value = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
            parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if (
            parts.scheme not in ("https", "http")
            or not host
            or parts.username
            or parts.password
            or parts.port not in (None, 80, 443)
            or len(value) > 2000
            or any(ord(c) < 33 for c in value)
            or "[" in value
            or "]" in value
        ):
            return None
        if "." not in host or host.endswith((".local", ".localhost", ".internal")):
            return None
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return None
        if host in ("bing.com", "so.com") or host.endswith((".bing.com", ".so.com")):
            return None
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    except (ValueError, UnicodeError):
        return None


class Results(HTMLParser):
    """Extract only organic result title/paragraphs, excluding scripts and ads."""

    def __init__(self):
        super().__init__()
        self.stack = []
        self.root = None
        self.results = []

    def handle_starttag(self, tag, attrs):
        if tag in (
            "br",
            "img",
            "meta",
            "link",
            "input",
            "hr",
            "wbr",
            "source",
            "area",
            "base",
            "embed",
            "param",
            "track",
            "col",
        ):
            return
        attr = dict(attrs)
        classes = set(attr.get("class", "").split())
        self.stack.append((tag, classes))
        if self.root is None and tag == "li" and classes & {"b_algo", "res-list"}:
            self.root = {"depth": len(self.stack), "title": [], "snippet": [], "source": None}
        if self.root and tag == "a" and any(t in ("h2", "h3") for t, _ in self.stack):
            self.root["source"] = source_url(attr.get("data-mdurl") or attr.get("href", ""))

    def handle_endtag(self, tag):
        positions = [i for i, (t, _) in enumerate(self.stack) if t == tag]
        if not positions:
            return
        index = positions[-1]
        if self.root and index < self.root["depth"]:
            item = self.root
            title = " ".join("".join(item["title"]).split())[:200]
            snippet = " ".join("".join(item["snippet"]).split())[:800]
            if (
                title
                and snippet
                and item["source"]
                and not any(x["source"] == item["source"] for x in self.results)
            ):
                self.results.append(
                    {
                        "title": title,
                        "source": item["source"],
                        "untrusted_content": snippet,
                        "published_at": None,
                    }
                )
            self.root = None
        del self.stack[index:]

    def handle_data(self, text):
        if not self.root or any(t in ("script", "style", "noscript") for t, _ in self.stack):
            return
        if any(t in ("h2", "h3") for t, _ in self.stack):
            self.root["title"].append(text)
        elif any(t == "p" or "res-list-summary" in c for t, c in self.stack):
            self.root["snippet"].append(text)


async def run(arguments):
    query = arguments["query"].strip()
    if len(query) < 2 or re.search(
        r"sk-[A-Za-z0-9_-]{12,}|Bearer\s+\S+|(?<!\d)1[3-9]\d{9}(?!\d)|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
        query,
        re.I,
    ):
        raise ValueError("Use public search keywords without credentials or contact details")
    retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds")
    providers = (
        ("Bing", "https://www.bing.com/search", {"q": query, "setlang": "zh-Hans", "cc": "CN"}),
        ("360", "https://www.so.com/s", {"q": query}),
    )
    for provider, url, params in providers:
        try:
            raw = await public_get(
                url, {urlsplit(url).hostname}, params=params, timeout=7, max_bytes=1000000
            )
            parser = Results()
            parser.feed(raw)
            parser.close()
            if parser.results:
                return {
                    "status": "ok",
                    "provider": provider,
                    "query": query,
                    "retrieved_at": retrieved,
                    "results": parser.results[:5],
                    "evidence_level": "search_snippets",
                    "notice": "结果仅含未经正文核验的候选标题和摘要，不保证日期筛选或覆盖完整。retrieved_at是查询时刻，published_at未知时不能推断发布日期。",
                }
        except Exception:
            continue
    return {
        "status": "unavailable",
        "query": query,
        "retrieved_at": retrieved,
        "results": [],
        "notice": "搜索入口暂时不可用或没有可解析结果，不能声称已查证；可稍后再试。",
    }
