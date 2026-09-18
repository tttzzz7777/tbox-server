"""POST /upload — terminal uploads a binary file as multipart/form-data.

Per TBOXHELPER_PROTOCOL §4.2: metadata is
``{terminal_id, cmd_id, sha256, size}`` (cmd_id required). The server
rejects uploads whose cmd_id was never issued to this terminal (404).
The success body is just ``{status: "ok"}``; error bodies include the
cmd_id so the client can correlate.
"""

from __future__ import annotations

import json
import logging

from aiohttp import web

from ..models import UploadMeta
from ..protocol import error, ok
from ..storage import store_upload

log = logging.getLogger(__name__)


def register(app: web.Application, registry, config) -> None:
    app.router.add_post("/upload", _handle)


async def _handle(request: web.Request) -> web.Response:
    cfg = request.app["config"]
    if not request.content_type.startswith("multipart/"):
        return web.json_response(
            error("expected multipart/form-data"),
            status=415,
        )

    reader = await request.multipart()
    meta: UploadMeta | None = None
    stored = None
    try:
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "metadata":
                try:
                    meta = UploadMeta.from_json(
                        json.loads((await part.text()).strip())
                    )
                except (ValueError, json.JSONDecodeError) as e:
                    return web.json_response(
                        error(f"bad metadata: {e}"), status=400
                    )
            elif part.name == "file":
                if meta is None:
                    return web.json_response(
                        error("'metadata' must precede 'file'"),
                        status=400,
                    )

                # Validate cmd_id is known for this terminal — the
                # protocol explicitly says the cmd_id links upload to ack.
                registry = request.app["registry"]
                if not registry.cmd_known(meta.terminal_id, meta.cmd_id):
                    return web.json_response(
                        error("cmd_id not found", cmd_id=meta.cmd_id),
                        status=404,
                    )

                async def source():
                    while True:
                        chunk = await part.read_chunk(size=64 * 1024)
                        if not chunk:
                            break
                        yield chunk

                try:
                    stored = await store_upload(
                        data_dir=cfg.data_dir,
                        meta=meta,
                        source=source(),
                        max_bytes=cfg.max_upload_bytes,
                    )
                except OverflowError as e:
                    return web.json_response(
                        error(str(e), cmd_id=meta.cmd_id), status=413
                    )
                except ValueError as e:
                    return web.json_response(
                        error(str(e), cmd_id=meta.cmd_id), status=400
                    )
                except Exception as e:  # pragma: no cover - defensive
                    log.exception("upload failed")
                    return web.json_response(
                        error(f"upload failed: {e}", cmd_id=meta.cmd_id),
                        status=500,
                    )
    except Exception as e:
        log.exception("multipart parse failed")
        return web.json_response(error(f"multipart parse failed: {e}"), status=400)

    if stored is None:
        return web.json_response(error("missing 'file' field"), status=400)

    return web.json_response(ok())
