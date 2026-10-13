"""Run on the server. Password is read interactively and is never saved."""

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request

ROOT = Path(os.getenv("BOT_STATE_DIR", "/srv/qqbot/bot"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--status", action="store_true", help="check QQ login without submitting a password"
    )
    parser.add_argument("--qq", help="QQ account; password is prompted privately")
    parser.add_argument("--url", default="http://127.0.0.1:6099")
    args = parser.parse_args()
    if not args.status and not (args.qq and args.qq.isdigit()):
        parser.error("provide --status or --qq ACCOUNT")
    token = json.loads((ROOT / "data/napcat/webui.json").read_text())["token"]
    credential = ""

    def call(path, data):
        headers = {"Content-Type": "application/json"}
        if credential:
            headers["Authorization"] = "Bearer " + credential
        req = urllib.request.Request(
            args.url.rstrip("/") + "/api/" + path,
            data=json.dumps(data).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.load(response)
        if result.get("code") != 0:
            raise RuntimeError("Management request failed; check WebUI for details")
        return result.get("data")

    hashed = hashlib.sha256((token + ".napcat").encode()).hexdigest()
    auth = call("auth/login", {"hash": hashed})
    credential = auth.get("Credential", "")
    if not credential:
        raise RuntimeError("WebUI requires additional authentication; use browser")
    status = call("QQLogin/CheckLoginStatus", {})
    if args.status:
        print(
            "QQ online:", bool(status.get("isLogin")), "phase:", status.get("loginPhase", "unknown")
        )
        return
    if status.get("isLogin"):
        print("QQ already online; use WebUI to manage the current account.")
        return
    target = ROOT / "data/napcat" / ("onebot11_" + args.qq + ".json")
    if not target.exists():
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write((ROOT / "data/napcat/onebot11.json").read_text())
    password = getpass.getpass("QQ password (not saved): ")
    password_md5 = hashlib.md5(password.encode()).hexdigest()
    del password
    result = call("QQLogin/PasswordLogin", {"uin": args.qq, "passwordMd5": password_md5})
    del password_md5
    if isinstance(result, dict) and (result.get("needCaptcha") or result.get("needNewDevice")):
        print("QQ requires account-owner verification. Open WebUI to complete it.")
        return
    for _ in range(15):
        status = call("QQLogin/CheckLoginStatus", {})
        if status.get("isLogin"):
            call("QQLogin/SetQuickLoginQQ", {"uin": args.qq})
            print("QQ online; automatic login configured.")
            return
        time.sleep(2)
    print("Login submitted; check WebUI for the current result.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            "Login unavailable:", type(error).__name__, "— inspect WebUI or server configuration."
        )
        raise SystemExit(1)
