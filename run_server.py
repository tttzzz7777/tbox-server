#!/usr/bin/env python3
"""Entry point: build the app and run it under aiohttp's HTTP server.

Reads TBOX_* env vars via Config.from_env(). Installs signal handlers so
SIGTERM / SIGINT trigger a graceful shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web

from tbox_server.app import create_app
from tbox_server.config import Config


async def main() -> None:
    cfg = Config.from_env()
    app = create_app(cfg)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, cfg.host, cfg.port, shutdown_timeout=5)
    await site.start()

    log = logging.getLogger("tbox")
    log.info("server up on http://%s:%d", cfg.host, cfg.port)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal(name: str) -> None:
        log.info("received %s, shutting down", name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal, sig.name)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    try:
        await stop_event.wait()
    finally:
        log.info("cleaning up")
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass