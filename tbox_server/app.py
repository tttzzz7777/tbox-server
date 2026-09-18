"""aiohttp Application factory + route table.

Routes are split into one module per concern under ``handlers/``. Each handler
module exposes a ``register(app, registry, storage, config)`` function that
wires up its endpoints.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

from . import __version__
from .config import Config
from .handlers import register_all
from .middlewares import access_log, request_context
from .registry import TerminalRegistry

log = logging.getLogger(__name__)


def _setup_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )


async def _healthz(request: web.Request) -> web.Response:
    cfg: Config = request.app["config"]
    return web.json_response(
        {"status": "ok", "server_version": __version__, "version": cfg.server_version}
    )


async def _version_root(request: web.Request) -> web.Response:
    """Serve the server's own version manifest (different from /version)."""
    return web.json_response(
        {"status": "ok", "server_version": __version__}
    )


def create_app(config: Config | None = None) -> web.Application:
    _setup_logging()
    cfg = config or Config.from_env()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    app = web.Application(
        middlewares=[request_context, access_log],
        client_max_size=cfg.max_upload_bytes + 4096,
    )
    app["config"] = cfg
    registry = TerminalRegistry(cfg)
    app["registry"] = registry

    # Static-ish health/version routes live on the app itself.
    app.router.add_get("/healthz", _healthz)
    app.router.add_get("/api/version", _version_root)

    # All feature routes:
    register_all(app, registry, cfg)

    async def on_startup(app: web.Application) -> None:
        await registry.start_reaper()

    async def on_cleanup(app: web.Application) -> None:
        await registry.stop_reaper()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    log.info(
        "tbox-server %s listening on %s:%d data=%s",
        __version__,
        cfg.host,
        cfg.port,
        cfg.data_dir,
    )
    return app


__all__ = ["create_app"]