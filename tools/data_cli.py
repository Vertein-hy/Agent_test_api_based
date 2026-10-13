"""Local-only dataset review/export. Not exposed as a model or QQ tool."""

import argparse
import json
import os
from pathlib import Path

from qqbot.storage.archive import Archive, redact


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--limit", type=int, default=10)
    for action in ("approve", "reject"):
        command = sub.add_parser(action)
        command.add_argument("id", type=int)
    export = sub.add_parser("export")
    export.add_argument("path")
    export.add_argument("--format", choices=["messages", "sharegpt"], default="messages")
    args = parser.parse_args()
    salt = (
        Path(
            os.environ.get(
                "ARCHIVE_SALT_FILE",
                str(Path(os.getenv("BOT_STATE_DIR", "/srv/qqbot/bot")) / "secrets/archive_salt"),
            )
        )
        .read_bytes()
        .strip()
    )
    store = Archive(
        os.environ.get(
            "ARCHIVE_PATH",
            str(
                Path(os.getenv("BOT_STATE_DIR", "/srv/qqbot/bot"))
                / "data/archive/conversations.sqlite3"
            ),
        ),
        salt,
    )
    try:
        if args.action == "list":
            rows = store.db.execute(
                """SELECT id,question,answer,kind,delivered,reviewed,approved,prompt_messages,trace
                FROM turns ORDER BY id DESC LIMIT ?""",
                (max(1, min(args.limit, 100)),),
            ).fetchall()
            for row in rows:
                print(
                    json.dumps(
                        {
                            "id": row[0],
                            "question": redact(row[1]),
                            "answer": redact(row[2]),
                            "kind": row[3],
                            "delivered": bool(row[4]),
                            "reviewed": bool(row[5]),
                            "approved": bool(row[6]),
                            "prompt": json.loads(redact(row[7])),
                            "uses_tools": bool(json.loads(row[8])),
                        },
                        ensure_ascii=False,
                    )
                )
        elif args.action in ("approve", "reject"):
            store.review(args.id, args.action == "approve")
            print("Review recorded")
        else:
            count, skipped = store.export(args.path, args.format)
            print("Exported:", count, "tool-grounded turns omitted:", skipped)
    finally:
        store.close()


if __name__ == "__main__":
    main()
