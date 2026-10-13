"""Unified checks. Unit checks require only bot dependencies; memory checks run in its image."""

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BOT = [
    "test_public_release",
    "test_bot",
    "test_style",
    "test_search",
    "test_chat_quality",
    "test_archive_plugins",
    "test_adapter",
    "test_yunzai",
    "test_streaming",
    "test_stream_transport",
    "test_model_recovery",
    "test_tool_protocol",
    "test_answer_display",
    "test_composition",
    "test_memory_applicability",
    "test_addressing",
    "test_continuity",
    "test_social",
]
MEMORY = [
    "test_policy_jobs",
    "test_graph",
    "test_neural_recall",
    "test_dossier",
    "test_attention_service",
    "test_memory_v3",
]
POLICY = ["test_policy_jobs", "test_dossier"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "suite", choices=["bot", "policy", "memory", "all"], nargs="?", default="bot"
    )
    parser.add_argument(
        "--python-only",
        action="store_true",
        help="Run Node protocol checks separately in the panel image",
    )
    args = parser.parse_args()
    for path in ("src", "tests", "tools", "scripts"):
        for source in (ROOT / path).rglob("*.py"):
            compile(source.read_text(), str(source), "exec")
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests/unit")]
    import unittest

    names = {"bot": BOT, "policy": POLICY, "memory": MEMORY, "all": BOT + MEMORY}[args.suite]
    outcome = unittest.TextTestRunner(verbosity=1).run(
        unittest.defaultTestLoader.loadTestsFromNames(names)
    )
    if not outcome.wasSuccessful():
        raise SystemExit(1)
    if args.suite in ("bot", "all") and not args.python_only:
        subprocess.run(["node", "--test", str(ROOT / "tests/unit/test_protocol.mjs")], check=True)


if __name__ == "__main__":
    main()
