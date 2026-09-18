"""Admin endpoints: list terminals, push commands, list/show reports."""

from __future__ import annotations

import logging

from aiohttp import web

from ..models import Command
from ..protocol import CMD_TYPES, error, ok
from ..registry import TerminalRegistry
from ..storage import (
    cleanup_old_data,
    data_disk_usage,
    list_status_report,
    list_uploads,
    read_status_report,
)
from ..utils import new_id, utcnow

log = logging.getLogger(__name__)


def register(app: web.Application, registry: TerminalRegistry, config) -> None:
    app.router.add_get("/admin/terminals", _list)
    app.router.add_post("/admin/command", _push_command)
    app.router.add_get("/admin/reports", _list_reports)
    app.router.add_get("/admin/report", _get_report)
    app.router.add_get("/admin/uploads", _list_uploads)
    app.router.add_get("/admin/history", _list_history)
    app.router.add_post("/admin/cleanup", _cleanup)
    app.router.add_get("/admin/disk", _disk)
    app.router.add_post("/admin/terminal/close", _close_terminal)


# ---- payload validators ----


def _validate_upload_payload(payload: dict) -> str | None:
    """Return None if valid, else error message."""
    file_type = payload.get("file_type")
    if file_type not in ("log", "other"):
        return "payload.file_type must be 'log' or 'other'"
    if file_type == "other":
        path = payload.get("path")
        if not isinstance(path, str) or not path.strip():
            return "payload.path is required for file_type=other"
    if file_type == "log":
        for key in ("start_time", "end_time"):
            v = payload.get(key)
            if v is not None and not isinstance(v, str):
                return f"payload.{key} must be a string"
    return None


def _validate_exec_payload(payload: dict) -> str | None:
    cmd = payload.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return "payload.command is required"
    timeout = payload.get("timeout", 30)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        return "payload.timeout must be a positive number"
    return None


_VALIDATORS = {
    "upload": _validate_upload_payload,
    "exec": _validate_exec_payload,
}


# ---- routes ----


async def _list(request: web.Request) -> web.Response:
    cfg = request.app["config"]
    registry: TerminalRegistry = request.app["registry"]
    now = utcnow()
    sessions = await registry.all_sessions()
    return web.json_response(
        ok(
            terminals=[
                s.to_admin_dict(now=now, offline_after_s=cfg.offline_after_s)
                for s in sessions
            ]
        )
    )


async def _push_command(request: web.Request) -> web.Response:
    registry: TerminalRegistry = request.app["registry"]
    try:
        data = await request.json()
    except Exception:
        return web.json_response(error("invalid JSON body"), status=400)

    tid = data.get("terminal_id")
    cmd_type = data.get("type")
    payload = data.get("payload") or {}

    if not isinstance(tid, str) or not tid.strip():
        return web.json_response(error("terminal_id required"), status=400)
    if cmd_type not in CMD_TYPES:
        return web.json_response(
            error(
                f"unknown command type '{cmd_type}'",
                allowed=sorted(CMD_TYPES),
            ),
            status=400,
        )
    if not isinstance(payload, dict):
        return web.json_response(error("payload must be an object"), status=400)

    validator = _VALIDATORS.get(cmd_type)
    if validator is not None:
        msg = validator(payload)
        if msg is not None:
            return web.json_response(error(msg), status=400)

    cmd = Command(
        cmd_id=new_id(),
        terminal_id=tid.strip(),
        type=cmd_type,
        payload=payload,
        issued_at=utcnow(),
    )
    try:
        await registry.enqueue_command(tid.strip(), cmd)
    except RuntimeError as e:
        return web.json_response(error(str(e)), status=503)

    log.info("queued command %s -> %s type=%s", cmd.cmd_id, tid, cmd_type)
    return web.json_response(ok(cmd_id=cmd.cmd_id, issued_at=cmd.issued_at))


# ---- report introspection ----


async def _list_reports(request: web.Request) -> web.Response:
    cfg = request.app["config"]
    tid = request.query.get("terminal_id", "").strip()
    if not tid:
        return web.json_response(
            error("terminal_id query param required"),
            status=400,
        )
    report = list_status_report(cfg.data_dir, tid)
    reports = [report] if report is not None else []
    return web.json_response(ok(terminal_id=tid, reports=reports))


async def _get_report(request: web.Request) -> web.Response:
    cfg = request.app["config"]
    tid = request.query.get("terminal_id", "").strip()
    rtype = request.query.get("type", "status").strip() or "status"
    if not tid:
        return web.json_response(
            error("terminal_id query param required"),
            status=400,
        )
    if rtype != "status":
        return web.json_response(
            error(
                f"unsupported report_type {rtype!r}",
                supported=["status"],
            ),
            status=400,
        )
    payload = read_status_report(cfg.data_dir, tid)
    if payload is None:
        return web.json_response(
            error(f"no status report for terminal_id={tid!r}"),
            status=404,
        )
    return web.json_response(
        ok(terminal_id=tid, type=rtype, payload=payload)
    )


async def _list_uploads(request: web.Request) -> web.Response:
    cfg = request.app["config"]
    tid = request.query.get("terminal_id", "").strip()
    if not tid:
        return web.json_response(
            error("terminal_id query param required"),
            status=400,
        )
    files = list_uploads(cfg.data_dir, tid)
    return web.json_response(ok(terminal_id=tid, uploads=files))


async def _list_history(request: web.Request) -> web.Response:
    registry: TerminalRegistry = request.app["registry"]
    tid = request.query.get("terminal_id", "").strip()
    if not tid:
        return web.json_response(
            error("terminal_id query param required"),
            status=400,
        )
    if registry.get(tid) is None:
        return web.json_response(
            error(f"terminal {tid!r} not found"),
            status=404,
        )
    items = registry.get_history(tid)
    return web.json_response(ok(terminal_id=tid, history=items))


async def _cleanup(request: web.Request) -> web.Response:
    """Trigger a retention sweep on demand.

    Query/body params (all optional):
      ``older_than_s`` (int): override the configured retention for this call.
    """
    cfg = request.app["config"]
    retention = cfg.data_retention_s
    try:
        # allow override via query string for ops testing
        override = request.query.get("older_than_s")
        if override is not None:
            retention = int(override)
    except ValueError:
        return web.json_response(
            error("older_than_s must be an integer"),
            status=400,
        )

    report = cleanup_old_data(cfg.data_dir, retention, utcnow())
    return web.json_response(
        ok(
            retention_s=retention,
            **report.to_dict(),
        )
    )


async def _disk(request: web.Request) -> web.Response:
    cfg = request.app["config"]
    usage = data_disk_usage(cfg.data_dir)
    return web.json_response(
        ok(
            data_dir=str(cfg.data_dir),
            retention_s=cfg.data_retention_s,
            cleanup_interval_s=cfg.data_cleanup_interval_s,
            **usage,
        )
    )


async def _close_terminal(request: web.Request) -> web.Response:
    """Force-remove an offline terminal session from the in-memory registry.

    On-disk artifacts (reports/, uploads/) are intentionally NOT touched —
    only the live session record is dropped. Online terminals must be closed
    by waiting for the heartbeat to expire; we refuse with a 409 if the
    session is still considered online.
    """
    registry: TerminalRegistry = request.app["registry"]
    cfg = request.app["config"]
    try:
        data = await request.json()
    except Exception:
        return web.json_response(error("invalid JSON body"), status=400)

    tid = str(data.get("terminal_id", "")).strip()
    if not tid:
        return web.json_response(error("terminal_id required"), status=400)

    sess = registry.get(tid)
    if sess is None:
        return web.json_response(
            error(f"terminal {tid!r} not found"),
            status=404,
        )

    if sess.is_online(utcnow(), cfg.offline_after_s):
        return web.json_response(
            error("only offline terminals can be closed", online=True),
            status=409,
        )

    removed = await registry.remove_session(tid)
    log.info("closed offline terminal %s (removed=%s)", tid, removed)
    return web.json_response(
        ok(terminal_id=tid, removed=removed),
    )
