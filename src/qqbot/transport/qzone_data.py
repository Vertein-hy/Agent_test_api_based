"""Bounded Qzone data decoding. JavaScript callback bodies are never executed."""

import json
import re
from html.parser import HTMLParser


class DataError(ValueError):
    """Closed error category; never include a provider body or identifier."""


class Literal:
    """Read the inert object-literal subset used by feeds3 (including undefined)."""

    def __init__(self, text):
        self.text = text
        self.i = 0
        self.count = 0

    def ws(self):
        while self.i < len(self.text) and self.text[self.i].isspace():
            self.i += 1

    def string(self):
        quote = self.text[self.i]
        self.i += 1
        out = []
        while self.i < len(self.text):
            c = self.text[self.i]
            self.i += 1
            if c == quote:
                return "".join(out).encode("utf-16-le", "surrogatepass").decode("utf-16-le")
            if c == "\\":
                if self.i >= len(self.text):
                    break
                c = self.text[self.i]
                self.i += 1
                if c in ("u", "x"):
                    n = 4 if c == "u" else 2
                    token = self.text[self.i : self.i + n]
                    if not re.fullmatch(r"[0-9a-fA-F]{" + str(n) + "}", token):
                        raise DataError("Invalid escape")
                    c = chr(int(token, 16))
                    self.i += n
                else:
                    c = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v"}.get(c, c)
            out.append(c)
        raise DataError("Unterminated string")

    def value(self, depth=0):
        self.count += 1
        if depth > 40 or self.count > 50000:
            raise DataError("Data budget exceeded")
        self.ws()
        if self.i >= len(self.text):
            raise DataError("Incomplete data")
        c = self.text[self.i]
        if c in "\"'":
            return self.string()
        if c in "[{":
            mapping = c == "{"
            end = "}" if mapping else "]"
            result = {} if mapping else []
            self.i += 1
            self.ws()
            while self.i < len(self.text) and self.text[self.i] != end:
                if mapping:
                    if self.text[self.i] in "\"'":
                        key = self.string()
                    else:
                        match = re.match(r"[A-Za-z_$][\w$]*", self.text[self.i :])
                        if not match:
                            raise DataError("Invalid key")
                        key = match[0]
                        self.i += len(key)
                    self.ws()
                    if self.i >= len(self.text) or self.text[self.i] != ":":
                        raise DataError("Missing colon")
                    self.i += 1
                    result[key] = self.value(depth + 1)
                else:
                    result.append(self.value(depth + 1))
                self.ws()
                if self.i < len(self.text) and self.text[self.i] == ",":
                    self.i += 1
                    self.ws()
                elif self.i >= len(self.text) or self.text[self.i] != end:
                    raise DataError("Invalid collection")
            if self.i >= len(self.text):
                raise DataError("Incomplete collection")
            self.i += 1
            return result
        match = re.match(
            r"(?:true|false|null|undefined)\b|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?",
            self.text[self.i :],
        )
        if not match:
            raise DataError("Executable or invalid data")
        token = match[0]
        self.i += len(token)
        return None if token == "undefined" else json.loads(token)


def decode(text):
    if len(text) > 2000000:
        raise DataError("Response too large")
    start = text.find("{")
    if start < 0:
        raise DataError("Missing object")
    parser = Literal(text[start:])
    value = parser.value()
    if not isinstance(value, dict):
        raise DataError("Invalid object")
    if any(value.get(k) not in (None, 0) for k in ("code", "subcode", "ret")):
        raise DataError("Qzone rejected request")
    if not any(value.get(k) == 0 for k in ("code", "subcode", "ret")):
        raise DataError("Missing success marker")
    return value


def identifier(value):
    value = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,180}", value):
        raise DataError("Invalid identifier")
    return value


def account(value):
    value = str(value or "")
    if not re.fullmatch(r"[1-9]\d{4,19}", value):
        raise DataError("Invalid account")
    return value


class ForwardMarker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.markers = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "i" and attrs.get("name") == "feed_data":
            self.markers.append(attrs)


def forward_candidate(feed):
    """Only platform metadata is considered, then detail must independently agree."""
    if str(feed.get("appid")) != "311":
        return None
    parser = ForwardMarker()
    parser.feed(str(feed.get("html", ""))[:200000])
    if len(parser.markers) != 1:
        return None
    m = parser.markers[0]
    try:
        owner = account(feed.get("opuin") or feed.get("uin"))
        if account(m.get("data-uin")) != owner:
            return None
        return {
            "owner": owner,
            "tid": identifier(m.get("data-tid")),
            "root_owner": account(m.get("data-origuin")),
            "root_tid": identifier(m.get("data-origtid")),
        }
    except DataError:
        return None


def comments(data):
    raw = data.get("commentlist")
    if raw is None:
        raw = data.get("comments")
    if raw is None and int(data.get("cmtnum", 0)) == 0:
        return []
    if not isinstance(raw, list) or len(raw) > 100:
        raise DataError("Invalid comment list")
    result = []
    for c in raw:
        if not isinstance(c, dict):
            raise DataError("Invalid comment")
        poster = c.get("poster") or {}
        author = account(c.get("uin") or poster.get("id"))
        cid = identifier(c.get("tid") or c.get("commentid") or c.get("id"))
        result.append(
            {
                "id": cid,
                "author": author,
                "parent": "",
                "text": str(c.get("content", ""))[:1500],
                "time": int(c.get("create_time") or c.get("createtime") or 0),
            }
        )
        replies = c.get("list_3") or c.get("replylist") or []
        if not isinstance(replies, list) or len(replies) > 100:
            raise DataError("Invalid replies")
        for r in replies:
            result.append(
                {
                    "id": identifier(r.get("tid") or r.get("commentid")),
                    "author": account(r.get("uin")),
                    "parent": cid,
                    "text": str(r.get("content", ""))[:1500],
                    "time": int(r.get("create_time") or r.get("createtime") or 0),
                }
            )
    return result
