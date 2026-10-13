"""Private configuration and online SQLite snapshots, without printing data or secrets."""

import argparse
import os
from pathlib import Path
import subprocess

DATABASES = {
    "qq-deepseek-bot-bot-1": [
        "/app/archive/conversations.sqlite3",
        "/app/memory-outbox/outbox.sqlite3",
        "/app/archive/social.sqlite3",
    ],
    "qq-memory-addon-memory-service-1": [
        "/data/jobs.sqlite3",
        "/data/vectors.sqlite3",
        "/data/attention.sqlite3",
    ],
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=True)
    args.destination.chmod(0o700)
    saved = 0
    for container, paths in DATABASES.items():
        for source in paths:
            if source.endswith("/social.sqlite3"):
                exists = subprocess.run(
                    [
                        "docker",
                        "exec",
                        container,
                        "python",
                        "-c",
                        "from pathlib import Path; raise SystemExit(0 if Path('/app/archive/social.sqlite3').is_file() else 1)",
                    ],
                    check=False,
                )
                if exists.returncode:
                    continue
            name = container + "-" + Path(source).name
            code = (
                "import sqlite3; "
                f"source=sqlite3.connect('file:{source}?mode=ro', uri=True); "
                "target=sqlite3.connect('/tmp/refactor-backup.sqlite3'); "
                "source.backup(target); target.close(); source.close()"
            )
            subprocess.run(["docker", "exec", container, "python", "-c", code], check=True)
            target = args.destination / name
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as file:
                subprocess.run(
                    ["docker", "exec", container, "cat", "/tmp/refactor-backup.sqlite3"],
                    stdout=file,
                    check=True,
                )
            subprocess.run(
                ["docker", "exec", container, "rm", "/tmp/refactor-backup.sqlite3"], check=True
            )
            saved += 1
    print(f"{saved} consistent SQLite snapshots saved; source databases unchanged.")


if __name__ == "__main__":
    main()
