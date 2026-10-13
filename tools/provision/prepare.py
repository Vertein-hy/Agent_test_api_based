"""Generate private NapCat configuration; never overwrite existing credentials."""

import json
import os
from pathlib import Path
import secrets

ROOT = Path(os.getenv("BOT_STATE_DIR", "/srv/qqbot/bot"))


def private_write(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


def main():
    for folder in ("secrets", "data/qq", "data/napcat", "data/archive"):
        p = ROOT / folder
        p.mkdir(parents=True, exist_ok=True)
        p.chmod(0o700)
    for name in ("onebot_token", "webui_token", "archive_salt", "plugin_token"):
        path = ROOT / "secrets" / name
        if not path.exists():
            private_write(path, secrets.token_urlsafe(32))
    token = (ROOT / "secrets/onebot_token").read_text().strip()
    config = {
        "network": {
            "httpServers": [
                {
                    "name": "deepseek-http",
                    "enable": True,
                    "host": "0.0.0.0",
                    "port": 3000,
                    "token": token,
                    "messagePostFormat": "array",
                    "debug": False,
                }
            ],
            "httpClients": [],
            "websocketClients": [],
            "websocketServers": [
                {
                    "name": "deepseek-ws",
                    "enable": True,
                    "host": "0.0.0.0",
                    "port": 3001,
                    "token": token,
                    "messagePostFormat": "array",
                    "reportSelfMessage": False,
                    "enableForcePushEvent": True,
                    "debug": False,
                    "heartInterval": 30000,
                }
            ],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
    }
    files = {
        "onebot11.json": config,
        "webui.json": {
            "host": "0.0.0.0",
            "port": 6099,
            "token": (ROOT / "secrets/webui_token").read_text().strip(),
            "loginRate": 3,
        },
        "napcat.json": {
            "fileLog": False,
            "consoleLog": True,
            "fileLogLevel": "error",
            "consoleLogLevel": "error",
            "packetServer": "",
        },
    }
    for name, content in files.items():
        path = ROOT / "data/napcat" / name
        if not path.exists():
            private_write(path, json.dumps(content, ensure_ascii=False, indent=2))
    print("Private configuration ready. Read secrets/webui_token to log into WebUI.")


if __name__ == "__main__":
    main()
