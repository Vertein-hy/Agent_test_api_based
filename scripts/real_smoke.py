"""Run an explicit real-model probe with ephemeral stores and no QQ delivery capability."""

import argparse
import os
from pathlib import Path
import runpy
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "boundary": "output_boundary_smoke.py",
    "search": "search_display_smoke.py",
    "panel": "model_smoke.py",
    "conversation": "bot_smoke.py",
    "memory-quality": "memory_quality_smoke.py",
    "addressing": "addressing_smoke.py",
    "continuity": "continuity_smoke.py",
    "voice": "voice_smoke.py",
    "model-comparison": "model_comparison_smoke.py",
    "social": "social_smoke.py",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("case", choices=CASES)
    args = parser.parse_args()
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests/unit")]
    from qqbot.app import Bot

    async def denied_qq_action(*args, **kwargs):
        raise AssertionError("Real QQ actions are forbidden during smoke verification")

    with tempfile.TemporaryDirectory(prefix="qqbot-fixture-") as tmp:
        env = {
            "ARCHIVE_PATH": tmp + "/archive.sqlite3",
            "ARCHIVE_SALT": "fictional-smoke-salt",
            "ARCHIVE_SALT_FILE": "",
            "ARCHIVE_DEFAULT_CAPTURE": "1",
            "MEMORY_OUTBOX": tmp + "/outbox.sqlite3",
            "MEMORY_ENABLED": "1",
            "YUNZAI_ENABLED": "1",
            "PLUGIN_RUNNER_URL": "http://plugins:8081",
            "ONEBOT_TOKEN": "",
            "ONEBOT_TOKEN_FILE": "",
            "SOCIAL_ENABLED": "0",
            "SOCIAL_ADMINS": "",
            "SOCIAL_ADMINS_FILE": "",
        }
        with patch.dict(os.environ, env), patch.object(Bot, "action", denied_qq_action):
            runpy.run_path(str(ROOT / "tests/integration" / CASES[args.case]), run_name="__main__")


if __name__ == "__main__":
    main()
