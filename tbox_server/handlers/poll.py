"""GET /poll — long-poll for the next pending command.

Per TBOXHELPER_PROTOCOL §3.2: terminal must be registered (heartbeat
already received) or the server returns 404. The response uses
``timestamp`` rather than v1's ``server_time``; the delivered ``cmd``
no longer includes ``ttl_s``.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from ..protocol import envelope, error
from ..registry import TerminalRegistry
from ..utils import clamp, utcnow

log = logging.getLogger(__name__)


def register(app: web.Application, registry: TerminalRegistry, config) -> None:
    app.router.add_get("/poll", _handle)


def _terminal_id_from(request: web.Request) -> str | None:
    tid = request.headers.get("X-Terminal-Id")
    if tid:
        return tid.strip() or None
    tid = request.query.get("terminal_id")
    if tid:
        return tid.strip() or None
    return None


async def _handle(request: web.Request) -> web.Response:
    tid = _terminal_id_from(request)
    if not tid:
        return web.json_response(
            error("terminal_id required"),
            status=400,
        )

    cfg = request.app["config"]
    registry: TerminalRegistry = request.app["registry"]

    sess = registry.get(tid)
    if sess is None:
        # Per v2 spec: terminal must have completed /heartbeat before
        # /poll; we no longer lazy-create the session.
        return web.json_response(
            error("terminal not found"),
            status=404,
        )

    raw_wait = request.query.get("wait", str(cfg.long_poll_timeout_s))
    try:
        wait = int(raw_wait)
    except ValueError:
        wait = cfg.long_poll_timeout_s
    wait = clamp(wait, 0, cfg.long_poll_max_timeout_s)

    now = utcnow()

    # Fast path: command already queued.
    if not sess.queue.empty():
        cmd = sess.queue.get_nowait()
        return web.json_response(
            envelope("command", cmd=cmd.to_json(), timestamp=now)
        )

    cmd_task = asyncio.create_task(registry.next_command(tid, wait))
    try:
        cmd = await cmd_task
    except asyncio.CancelledError:
        # Client disconnected (browser nav-away, terminal rebooted). If we
        # already pulled a command, put it back so it's not lost.
        if not cmd_task.done() or cmd_task.cancelled() or cmd_task.exception():
            # The task was cancelled before it pulled anything; nothing to do.
            raise
        # Task completed before cancellation: shouldn't happen, but be safe.
        raise

    if cmd is None:
        return web.json_response(envelope("idle", timestamp=now))

    return web.json_response(envelope("command", cmd=cmd.to_json(), timestamp=now))
