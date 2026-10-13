"""python -m qqbot: the sole bot runtime entrypoint."""

import logging
from aiohttp import web
from qqbot.app import Bot


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bot = Bot()
    app = web.Application()
    app.router.add_get("/healthz", bot.health)
    app.on_startup.append(bot.start)
    app.on_cleanup.append(bot.stop)
    web.run_app(app, host="127.0.0.1", port=8080, access_log=None)


if __name__ == "__main__":
    main()
