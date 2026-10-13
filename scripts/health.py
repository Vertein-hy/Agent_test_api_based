"""Bounded readiness wait; health is not a substitute for explicit real-model smoke."""

import json
import subprocess
import time


def ready():
    endpoints = [("qq-deepseek-bot-bot-1", 8080), ("qq-memory-addon-memory-service-1", 8083)]
    status = []
    for container, port in endpoints:
        code = f"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:{port}/healthz',timeout=4).read().decode())"
        result = subprocess.run(
            ["docker", "exec", container, "python", "-c", code], capture_output=True, text=True
        )
        if result.returncode:
            return False
        status.append(json.loads(result.stdout))
    bot, memory = status
    return (
        bot["qq_connected"]
        and bot["qq_online"]
        and memory["ready"]
        and bot["architecture"] == "composed-v1"
    )


def main():
    for _ in range(45):
        if ready():
            print("QQ connection and memory service are ready.")
            return
        time.sleep(2)
    raise SystemExit("Readiness failed; inspect service status and use the documented rollback.")


if __name__ == "__main__":
    main()
