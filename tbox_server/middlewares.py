"""aiohttp middlewares (request id, access log, JSON error envelope)."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Awaitable, Callable

from aiohttp import web

from .protocol import error

log = logging.getLogger("tbox.access")

_RequestHandler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@web.middleware
async def request_context(
    request: web.Request, handler: _RequestHandler
) -> web.StreamResponse:
    rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    request["request_id"] = rid
    request["start_time"] = time.monotonic()
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover - generic handler
        log.exception("unhandled error rid=%s path=%s", rid, request.path)
        return web.json_response(
            error("internal server error", request_id=rid, detail=str(exc)),
            status=500,
            headers={"X-Request-Id": rid},
        )


@web.middleware
async def access_log(
    request: web.Request, handler: _RequestHandler
) -> web.StreamResponse:
    rid = request.get("request_id", "-")
    start = request.get("start_time", time.monotonic())
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        dur_ms = (time.monotonic() - start) * 1000
        log.info(
            "%s %s rid=%s -> %s (%.1fms)",
            request.method,
            request.path,
            rid,
            exc.status,
            dur_ms,
        )
        raise
    dur_ms = (time.monotonic() - start) * 1000
    log.info(
        "%s %s rid=%s -> %s (%.1fms)",
        request.method,
        request.path,
        rid,
        response.status,
        dur_ms,
    )
    response.headers.setdefault("X-Request-Id", rid)
    return response