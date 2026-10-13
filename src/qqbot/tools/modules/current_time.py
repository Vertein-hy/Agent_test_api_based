from datetime import datetime, timedelta, timezone


async def run(arguments):
    return {
        "time": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
    }
