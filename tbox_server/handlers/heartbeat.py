"""POST /heartbeat — terminals announce themselves and report liveness.

Per TBOXHELPER_PROTOCOL §3.1, the body has only ``terminal_id`` (required)
and ``service`` (optional). Response uses ``timestamp`` instead of v1's
``server_time``.
"""

from __future__ import annotations

from aiohttp import web

from ..models import Heartbeat
from ..protocol import error, ok
from ..utils import utcnow


def register(app: web.Application, registry, config) -> None:
    app.router.add_post("/heartbeat", _handle)


async def _handle(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response(error("invalid JSON body"), status=400)
    try:
        hb = Heartbeat.from_json(data)
    except ValueError as e:
        return web.json_response(error(str(e)), status=400)

    await request.app["registry"].heartbeat(
        terminal_id=hb.terminal_id,
        service=hb.service,
    )

    cfg = request.app["config"]
    return web.json_response(
        ok(
            timestamp=utcnow(),
            heartbeat_interval_s=cfg.heartbeat_interval_s,
            long_poll_timeout_s=cfg.long_poll_timeout_s,
        )
    )
