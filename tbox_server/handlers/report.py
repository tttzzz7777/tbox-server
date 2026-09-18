"""POST /report — terminal uploads a structured status report.

Per TBOXHELPER_PROTOCOL §3.4 only ``report_type="status"`` is supported.
The body must be a JSON object with a ``data`` field (position, iccid,
imei, imsi, vin, sn, vehicle_model, dtc). The server does not validate
the internal schema of ``data`` — it stores the payload verbatim — but
the outer envelope (``terminal_id``, ``report_type``, ``timestamp``) is
required.

Storage: ``data/reports/<terminal_id>/status/latest.json`` (overwritten
on each call).
"""

from __future__ import annotations

import logging

from aiohttp import web

from ..protocol import error, ok
from ..storage import store_status_report

log = logging.getLogger(__name__)


def register(app: web.Application, registry, config) -> None:
    app.router.add_post("/report", _handle)


_REQUIRED_REPORT_TYPE = "status"


async def _handle(request: web.Request) -> web.Response:
    cfg = request.app["config"]
    if not request.content_type.lower().startswith("application/json"):
        return web.json_response(
            error("Content-Type must be application/json"),
            status=415,
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response(error("invalid JSON body"), status=400)
    if not isinstance(body, dict):
        return web.json_response(error("body must be a JSON object"), status=400)

    terminal_id = str(body.get("terminal_id", "")).strip()
    report_type = str(body.get("report_type", "")).strip() or _REQUIRED_REPORT_TYPE
    if not terminal_id:
        return web.json_response(error("terminal_id is required"), status=400)
    if report_type != _REQUIRED_REPORT_TYPE:
        return web.json_response(
            error(
                f"unsupported report_type {report_type!r}",
                supported=[_REQUIRED_REPORT_TYPE],
            ),
            status=400,
        )

    data = body.get("data")
    if not isinstance(data, dict):
        return web.json_response(
            error("body must contain a 'data' object"),
            status=400,
        )

    client_ts = body.get("timestamp")
    try:
        ts = float(client_ts) if client_ts is not None else None
    except (TypeError, ValueError):
        return web.json_response(error("'timestamp' must be a number"), status=400)

    try:
        stored = await store_status_report(
            data_dir=cfg.data_dir,
            terminal_id=terminal_id,
            payload=data,
        )
    except ValueError as e:
        return web.json_response(error(str(e)), status=400)

    await request.app["registry"].record_report(
        terminal_id=terminal_id,
        report_type=report_type,
        ts=ts if ts is not None else stored.latest_path.stat().st_mtime,
        path=str(stored.latest_path),
    )

    return web.json_response(
        ok(
            stored=str(stored.latest_path),
            bytes=stored.latest_path.stat().st_size,
            report_type=report_type,
        )
    )
