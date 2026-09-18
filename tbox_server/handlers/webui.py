"""Static web UI for tbox-server.

Serves a single-page dashboard at ``/`` plus assets under ``/webui/``.
The dashboard talks to the existing ``/admin/*`` JSON API (same-origin,
no CORS, no auth — by design; see README §"v2 non-goals").

We use a custom asset handler instead of ``web.static`` so we can set
``Cache-Control: no-cache`` on every response. Without this the browser
would happily cache ``app.js`` between deploys and the user would see
stale behaviour even after a server restart.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

log = logging.getLogger(__name__)

# Resolve the webui dir relative to this file so it works regardless of CWD.
WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"

# Whitelist of asset filenames we serve. Keeps a typo from becoming a
# directory traversal probe (``/webui/../etc/passwd``).
_ALLOWED_ASSETS = {"style.css", "app.js"}


def register(app: web.Application, registry, config) -> None:
    if not WEBUI_DIR.is_dir():
        log.warning("webui directory not found at %s; UI disabled", WEBUI_DIR)
        return

    async def _index(request: web.Request) -> web.Response:
        return web.FileResponse(
            WEBUI_DIR / "index.html",
            headers={"Cache-Control": "no-cache"},
        )

    async def _asset(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in _ALLOWED_ASSETS:
            raise web.HTTPNotFound(reason="no such asset")
        return web.FileResponse(
            WEBUI_DIR / name,
            headers={"Cache-Control": "no-cache"},
        )

    app.router.add_get("/", _index)
    app.router.add_get("/webui/{name}", _asset)
    log.info("webui mounted at / (assets under /webui/)")


__all__ = ["register"]
