"""Qzone transport using the existing authenticated NapCat session.

Only fixed HTTPS CGI endpoints are reachable. Cookies remain local to this
transport, never enter model inputs, logs or the public journal. Read failures
propagate instead of pretending an inaccessible feed has no comments.
"""

from http.cookies import SimpleCookie
import time
import aiohttp
from qqbot.transport.qzone_data import DataError, account, identifier, decode, comments


BASE = "https://user.qzone.qq.com/proxy/domain/"


class Qzone:
    def __init__(self, bot):
        self.bot = bot
        self.owner = ""
        self.cookie = ""
        self.gtk = 0
        self.expires = 0
        self.authorize_write = None

    async def auth(self):
        if time.monotonic() < self.expires:
            return
        login = await self.bot.action("get_login_info")
        owner = account(login.get("user_id"))
        data = await self.bot.action("get_cookies", {"domain": "qzone.qq.com"})
        cookie = data.get("cookies") or ""
        jar = SimpleCookie()
        jar.load(cookie)
        cookie_owner = jar.get("p_uin") or jar.get("uin")
        if cookie_owner and cookie_owner.value.lstrip("o0") != owner:
            raise DataError("Space cookie account mismatch")
        skey = jar.get("p_skey") or jar.get("skey")
        if not skey or not skey.value:
            raise DataError("Space authentication unavailable")
        value = 5381
        for ch in skey.value:
            value = (value * 33 + ord(ch)) & 0xFFFFFFFF
        self.owner = owner
        self.cookie = cookie
        self.gtk = value & 0x7FFFFFFF
        self.expires = time.monotonic() + 300

    async def request(self, path, params, write=False):
        await self.auth()
        if write and (self.authorize_write is None or not self.authorize_write()):
            raise DataError("Space write no longer authorized")
        params = {**params, "g_tk": str(self.gtk)}
        kwargs = {"data" if write else "params": params}
        if write:
            kwargs["params"] = {"g_tk": str(self.gtk)}
        try:
            async with self.bot.session.request(
                "POST" if write else "GET",
                BASE + path,
                headers={
                    "Cookie": self.cookie,
                    "Referer": "https://user.qzone.qq.com/" + self.owner,
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=aiohttp.ClientTimeout(total=20),
                allow_redirects=False,
                **kwargs,
            ) as response:
                if response.status != 200:
                    raise DataError("Space HTTP failure")
                raw = await response.content.read(2000001)
                if len(raw) > 2000000:
                    raise DataError("Space response too large")
                return decode(raw.decode("utf-8"))
        except Exception:
            self.expires = 0
            raise

    async def publish(self, text, right):
        # Use the same credential-owning transport for every Qzone write.
        await self.auth()
        data = await self.request(
            "taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6",
            {
                "syn_tweet_verson": "1",
                "paramstr": "1",
                "con": text,
                "feedversion": "1",
                "ver": "1",
                "ugc_right": str(right),
                "to_sign": "0",
                "hostuin": self.owner,
                "code_version": "1",
                "format": "json",
                "qzreferrer": "https://user.qzone.qq.com/" + self.owner,
            },
            True,
        )
        return identifier(data.get("t1_tid") or data.get("tid"))

    async def own_list(self):
        await self.auth()
        data = await self.request(
            "taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6",
            {
                "uin": self.owner,
                "pos": "0",
                "num": "50",
                "ftype": "0",
                "sort": "0",
                "format": "json",
                "code_version": "1",
            },
        )
        if not isinstance(data.get("msglist"), list):
            raise DataError("Invalid own post list")
        return data["msglist"]

    async def detail(self, owner, tid, pos=0):
        owner = account(owner)
        tid = identifier(tid)
        data = await self.request(
            "taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6",
            {
                "uin": owner,
                "tid": tid,
                "pos": str(pos),
                "num": "100",
                "ftype": "0",
                "sort": "0",
                "t1_source": "1",
                "not_trunc_con": "1",
                "need_private_comment": "1",
                "replynum": "100",
                "format": "json",
                "code_version": "1",
            },
        )
        if str(data.get("tid")) != tid or str(data.get("uin")) != owner:
            raise DataError("Post identity mismatch")
        return data

    async def comment_list(self, owner, tid):
        result = []
        for pos in (0, 100, 200):
            data = await self.detail(owner, tid, pos)
            result.extend(comments(data))
            if int(data.get("cmtnum", 0)) <= pos + 100:
                break
        return result

    async def feeds(self):
        await self.auth()
        result = []
        cursor = ""
        for page in range(1, 4):
            data = await self.request(
                "ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more",
                {
                    "uin": self.owner,
                    "scope": "0",
                    "view": "1",
                    "filter": "all",
                    "flag": "1",
                    "applist": "all",
                    "pagenum": str(page),
                    "count": "50",
                    "begintime": "0",
                    "dayspac": "5",
                    "refresh": "1" if page == 1 else "0",
                    "externparam": cursor,
                    "useutf8": "1",
                    "outputhtmlfeed": "1",
                    "format": "jsonp",
                    "callback": "_preloadCallback",
                    "aisortEndTime": "0",
                    "aisortOffset": "0",
                    "aisortBeginTime": "0",
                },
            )
            body = data.get("data") or {}
            values = body.get("data")
            if not isinstance(values, list):
                raise DataError("Invalid friend feeds")
            result.extend(x for x in values if isinstance(x, dict))
            main = body.get("main") or {}
            following = str(main.get("externparam") or "")
            if not main.get("hasMoreFeeds") or not following or following == cursor:
                break
            cursor = following
        return result

    async def comment(self, owner, tid, text, item=None):
        await self.auth()
        owner = account(owner)
        tid = identifier(tid)
        params = {
            "qzreferrer": "https://user.qzone.qq.com/" + self.owner,
            "hostUin": owner,
            "uin": self.owner,
            "topicId": owner + "_" + tid + "__1",
            "content": text,
            "format": "json",
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "ref": "feeds",
            "feedsType": "100",
            "private": "0",
            "paramstr": "1",
            "plat": "qzone",
            "source": "ic",
            "platformid": "52",
        }
        if item:
            params.update(
                {
                    "paramstr": "2",
                    "commentId": identifier(item["parent"] or item["id"]),
                    "commentUin": account(item["author"]),
                    "t1_uin": owner,
                    "t1_tid": tid,
                    "t2_uin": account(item["author"]),
                    "t2_tid": identifier(item["parent"] or item["id"]),
                }
            )
        data = await self.request("taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds", params, True)
        if data.get("code") != 0 and data.get("subcode") != 0:
            raise DataError("Unconfirmed comment")
        return str(data.get("commentid") or data.get("commentId") or "")
