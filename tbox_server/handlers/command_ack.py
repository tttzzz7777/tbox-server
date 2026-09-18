"""POST /command/ack — terminals report the result of a command they ran.

Per TBOXHELPER_PROTOCOL §3.3: returns 404 with ``{status, cmd_id, message}``
when the cmd_id is unknown to the server, and the success body no longer
echoes cmd_id.
"""

from __future__ import annotations

from aiohttp import web

from ..protocol import error, ok
from ..utils import utcnow


def register(app: web.Application, registry, config) -> None:
    app.router.add_post("/command/ack", _handle)


async def _handle(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response(error("invalid JSON body"), status=400)

    tid = data.get("terminal_id")
    cmd_id = data.get("cmd_id")
    status = data.get("status", "ok")
    result = data.get("result")
    if not isinstance(tid, str) or not tid.strip():
        return web.json_response(error("terminal_id is required"), status=400)
    if not isinstance(cmd_id, str) or not cmd_id.strip():
        return web.json_response(error("cmd_id is required"), status=400)

    ok_ack = await request.app["registry"].record_ack(
        terminal_id=tid.strip(),
        cmd_id=cmd_id.strip(),
        status=str(status),
        result=result,
        ts=utcnow(),
    )
    if not ok_ack:
        return web.json_response(
            error("cmd_id not found", cmd_id=cmd_id.strip()),
            status=404,
        )
    return web.json_response(ok())
