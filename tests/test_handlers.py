"""End-to-end tests over a real aiohttp server in-process."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from tbox_server.app import create_app
from tbox_server.config import Config


@pytest.fixture
async def client(tmp_path: Path):
    cfg = Config(
        data_dir=tmp_path,
        heartbeat_interval_s=1,
        offline_after_s=2,
        long_poll_timeout_s=2,
        long_poll_max_timeout_s=5,
        reaper_interval_s=1,
        max_upload_bytes=1024 * 1024,
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_healthz(client: TestClient):
    r = await client.get("/healthz")
    assert r.status == 200
    body = await r.json()
    assert body["status"] == "ok"


async def test_heartbeat_then_admin_list(client: TestClient):
    r = await client.post(
        "/heartbeat",
        json={"terminal_id": "TBOX-1", "service": "tboxhelper"},
    )
    assert r.status == 200
    data = await r.json()
    assert data["status"] == "ok"
    assert "heartbeat_interval_s" in data
    assert "long_poll_timeout_s" in data
    assert "timestamp" in data
    # v2 dropped server_time / server_version from the heartbeat response.
    assert "server_time" not in data
    assert "server_version" not in data

    r = await client.get("/admin/terminals")
    assert r.status == 200
    body = await r.json()
    tids = [t["terminal_id"] for t in body["terminals"]]
    assert "TBOX-1" in tids
    target = next(t for t in body["terminals"] if t["terminal_id"] == "TBOX-1")
    assert target["online"] is True
    assert target["service"] == "tboxhelper"
    # v2 dropped fw_version / address / caps / free_disk_mb from session.
    assert "fw_version" not in target


async def test_admin_command_delivered_via_poll(client: TestClient):
    # Pre-register terminal so /poll can find it (no more lazy create in v2).
    await client.post("/heartbeat", json={"terminal_id": "TBOX-9"})

    r = await client.post(
        "/admin/command",
        json={
            "terminal_id": "TBOX-9",
            "type": "exec",
            "payload": {"command": "ls", "timeout": 5},
        },
    )
    assert r.status == 200
    cmd_id = (await r.json())["cmd_id"]

    r = await client.get("/poll", params={"wait": "2"}, headers={"X-Terminal-Id": "TBOX-9"})
    assert r.status == 200
    body = await r.json()
    assert body["status"] == "command"
    assert body["cmd"]["type"] == "exec"
    assert body["cmd"]["cmd_id"] == cmd_id
    assert "issued_at" in body["cmd"]
    # v2 dropped ttl_s from the cmd envelope.
    assert "ttl_s" not in body["cmd"]
    assert "timestamp" in body

    # Ack and confirm.
    r = await client.post(
        "/command/ack",
        json={
            "terminal_id": "TBOX-9",
            "cmd_id": cmd_id,
            "status": "ok",
            "result": {"code": 0},
            "timestamp": 0,
        },
    )
    assert r.status == 200
    body = await r.json()
    # v2 ack body is just {status:ok}, no cmd_id echoed.
    assert body == {"status": "ok"}


async def test_poll_idle_timeout(client: TestClient):
    await client.post("/heartbeat", json={"terminal_id": "TBOX-EMPTY"})
    r = await client.get("/poll", params={"wait": "1"}, headers={"X-Terminal-Id": "TBOX-EMPTY"})
    assert r.status == 200
    body = await r.json()
    assert body["status"] == "idle"
    assert "timestamp" in body
    # v2 dropped server_time from idle response.
    assert "server_time" not in body


async def test_poll_404_on_unknown_terminal(client: TestClient):
    r = await client.get(
        "/poll", params={"wait": "1"}, headers={"X-Terminal-Id": "NEVER-HEARTBEATED"}
    )
    assert r.status == 404
    body = await r.json()
    assert body["status"] == "error"
    assert "terminal" in body["message"].lower()


async def test_upload_endpoint_round_trip(client: TestClient, tmp_path: Path):
    await client.post("/heartbeat", json={"terminal_id": "TBOX-1"})

    push = await client.post(
        "/admin/command",
        json={
            "terminal_id": "TBOX-1",
            "type": "upload",
            "payload": {"file_type": "other", "path": "/tmp/diag.bin"},
        },
    )
    assert push.status == 200
    cmd_id = (await push.json())["cmd_id"]

    data = b"hello tbox upload" * 8
    sha = hashlib.sha256(data).hexdigest()
    form = aiohttp.FormData()
    form.add_field(
        "metadata",
        json.dumps(
            {
                "terminal_id": "TBOX-1",
                "cmd_id": cmd_id,
                "sha256": sha,
                "size": len(data),
            }
        ),
        content_type="application/json",
    )
    form.add_field("file", data, filename="diag.bin", content_type="application/octet-stream")
    r = await client.post("/upload", data=form)
    assert r.status == 200, await r.text()
    body = await r.json()
    # v2 upload success body is just {status:ok}.
    assert body == {"status": "ok"}

    # The file should still be on disk.
    uploads = list((tmp_path / "uploads").rglob("*"))
    files = [p for p in uploads if p.is_file() and not p.name.endswith(".meta.json")]
    assert files, "expected uploaded file to land on disk"
    assert any(p.read_bytes() == data for p in files)


async def test_upload_404_on_unknown_cmd_id(client: TestClient):
    await client.post("/heartbeat", json={"terminal_id": "TBOX-1"})
    data = b"hi"
    form = aiohttp.FormData()
    form.add_field(
        "metadata",
        json.dumps(
            {"terminal_id": "TBOX-1", "cmd_id": "deadbeef", "size": len(data)}
        ),
        content_type="application/json",
    )
    form.add_field("file", data, filename="x.bin", content_type="application/octet-stream")
    r = await client.post("/upload", data=form)
    assert r.status == 404
    body = await r.json()
    assert body["status"] == "error"
    assert body["cmd_id"] == "deadbeef"


async def test_ack_404_on_unknown_cmd_id(client: TestClient):
    await client.post("/heartbeat", json={"terminal_id": "TBOX-1"})
    r = await client.post(
        "/command/ack",
        json={
            "terminal_id": "TBOX-1",
            "cmd_id": "nope",
            "status": "ok",
            "result": {"code": 0},
        },
    )
    assert r.status == 404
    body = await r.json()
    assert body["status"] == "error"
    assert body["cmd_id"] == "nope"


# ---- /report ----


async def test_report_status_round_trip(client: TestClient, tmp_path: Path):
    data = {
        "iccid": "89860123456789012345",
        "imei": "866987123456789",
        "vin": "LVX12345678901234",
        "sn": "TBOX-SN-1",
        "vehicle_model": "JMC-CX835",
        "position": {"lat": 22.5, "lon": 114.0, "status": 1, "mode": 3},
        "dtc": ["B20413"],
    }
    r = await client.post(
        "/report",
        json={
            "terminal_id": "TBOX-1",
            "service": "tboxhelper",
            "report_type": "status",
            "timestamp": int(time.time()),
            "data": data,
        },
    )
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["status"] == "ok"
    stored = Path(body["stored"])
    # v2 stores at <tid>/status/latest.json
    assert stored == tmp_path / "reports" / "TBOX-1" / "status" / "latest.json"
    assert stored.exists()
    assert json.loads(stored.read_text(encoding="utf-8")) == data

    # last_report written to the session.
    r = await client.get("/admin/terminals")
    target = next(t for t in (await r.json())["terminals"] if t["terminal_id"] == "TBOX-1")
    assert target["last_report"]["type"] == "status"
    assert target["last_report"]["path"].endswith("latest.json")


async def test_report_rejects_missing_terminal_id(client: TestClient):
    r = await client.post(
        "/report",
        json={"report_type": "status", "data": {"x": 1}},
    )
    assert r.status == 400


async def test_report_rejects_missing_data(client: TestClient):
    r = await client.post(
        "/report",
        json={"terminal_id": "TBOX-X", "report_type": "status"},
    )
    assert r.status == 400


async def test_report_rejects_non_status_type(client: TestClient):
    r = await client.post(
        "/report",
        json={
            "terminal_id": "TBOX-X",
            "report_type": "diagnostics",
            "data": {"a": 1},
        },
    )
    assert r.status == 400


async def test_admin_reports_listing_status(client: TestClient, tmp_path: Path):
    await client.post(
        "/report",
        json={
            "terminal_id": "TBOX-3",
            "service": "tboxhelper",
            "report_type": "status",
            "timestamp": int(time.time()),
            "data": {"iccid": "8986"},
        },
    )
    r = await client.get("/admin/reports", params={"terminal_id": "TBOX-3"})
    assert r.status == 200
    body = await r.json()
    types = {rep["type"] for rep in body["reports"]}
    assert types == {"status"}


async def test_admin_report_get_status(client: TestClient, tmp_path: Path):
    payload = {"iccid": "8986", "imei": "86"}
    await client.post(
        "/report",
        json={
            "terminal_id": "TBOX-4",
            "service": "tboxhelper",
            "report_type": "status",
            "timestamp": int(time.time()),
            "data": payload,
        },
    )
    r = await client.get("/admin/report", params={"terminal_id": "TBOX-4", "type": "status"})
    assert r.status == 200
    body = await r.json()
    assert body["payload"] == payload


async def test_admin_report_404_when_missing(client: TestClient):
    r = await client.get("/admin/report", params={"terminal_id": "NOPE", "type": "status"})
    assert r.status == 404


# ---- /admin/command payload validation ----


async def test_admin_push_upload_validates_payload(client: TestClient):
    await client.post("/heartbeat", json={"terminal_id": "TBOX-V"})
    r = await client.post(
        "/admin/command",
        json={"terminal_id": "TBOX-V", "type": "upload", "payload": {"file_type": "bogus"}},
    )
    assert r.status == 400
    r = await client.post(
        "/admin/command",
        json={
            "terminal_id": "TBOX-V",
            "type": "upload",
            "payload": {"file_type": "other"},  # missing path
        },
    )
    assert r.status == 400
    r = await client.post(
        "/admin/command",
        json={
            "terminal_id": "TBOX-V",
            "type": "upload",
            "payload": {"file_type": "other", "path": "/var/log/x.log"},
        },
    )
    assert r.status == 200


async def test_admin_push_exec_validates_payload(client: TestClient):
    await client.post("/heartbeat", json={"terminal_id": "TBOX-E"})
    r = await client.post(
        "/admin/command",
        json={"terminal_id": "TBOX-E", "type": "exec", "payload": {"timeout": 5}},
    )
    assert r.status == 400
    r = await client.post(
        "/admin/command",
        json={"terminal_id": "TBOX-E", "type": "exec", "payload": {"command": "ls"}},
    )
    assert r.status == 200


# ---- retention admin endpoints ----


async def test_admin_disk_reports_breakdown(client: TestClient):
    r = await client.get("/admin/disk")
    assert r.status == 200
    body = await r.json()
    assert body["status"] == "ok"
    assert "categories" in body
    assert body["retention_s"] == 86400  # default


async def test_admin_cleanup_returns_report(client: TestClient, tmp_path: Path):
    import os
    import time

    # plant an old upload date-dir
    old = tmp_path / "uploads" / "2020-01-01" / "TBOX-X"
    old.mkdir(parents=True)
    f = old / "f.bin"
    f.write_bytes(b"x" * 200)
    ts = time.time() - 5 * 86400
    os.utime(f, (ts, ts))
    os.utime(old, (ts, ts))
    os.utime(old.parent, (ts, ts))

    r = await client.post("/admin/cleanup")
    assert r.status == 200
    body = await r.json()
    assert body["uploads_dirs_removed"] >= 1
    assert body["bytes_freed"] >= 200
    assert not (tmp_path / "uploads" / "2020-01-01").exists()


# ---- /admin/uploads + /admin/history ----


async def test_admin_uploads_listing_empty(client: TestClient):
    await client.post("/heartbeat", json={"terminal_id": "TBOX-UP"})
    r = await client.get("/admin/uploads", params={"terminal_id": "TBOX-UP"})
    assert r.status == 200
    body = await r.json()
    assert body["uploads"] == []


async def test_admin_uploads_listing_returns_uploaded_file(client: TestClient):
    await client.post("/heartbeat", json={"terminal_id": "TBOX-UP2"})

    push = await client.post(
        "/admin/command",
        json={
            "terminal_id": "TBOX-UP2",
            "type": "upload",
            "payload": {"file_type": "other", "path": "/tmp/x.bin"},
        },
    )
    cmd_id = (await push.json())["cmd_id"]

    data = b"payload bytes"
    form = aiohttp.FormData()
    form.add_field(
        "metadata",
        json.dumps({"terminal_id": "TBOX-UP2", "cmd_id": cmd_id, "size": len(data)}),
        content_type="application/json",
    )
    form.add_field("file", data, filename="x.bin", content_type="application/octet-stream")
    r = await client.post("/upload", data=form)
    assert r.status == 200

    r = await client.get("/admin/uploads", params={"terminal_id": "TBOX-UP2"})
    assert r.status == 200
    body = await r.json()
    assert len(body["uploads"]) == 1
    assert body["uploads"][0]["cmd_id"] == cmd_id
    assert body["uploads"][0]["size"] == len(data)


async def test_admin_uploads_missing_terminal_id(client: TestClient):
    r = await client.get("/admin/uploads")
    assert r.status == 400


async def test_admin_history_listing(client: TestClient):
    await client.post("/heartbeat", json={"terminal_id": "TBOX-H1"})
    push = await client.post(
        "/admin/command",
        json={"terminal_id": "TBOX-H1", "type": "exec", "payload": {"command": "ls"}},
    )
    cmd_id = (await push.json())["cmd_id"]

    # No ack yet -> empty history.
    r = await client.get("/admin/history", params={"terminal_id": "TBOX-H1"})
    assert r.status == 200
    assert (await r.json())["history"] == []

    # Ack the command.
    r = await client.post(
        "/command/ack",
        json={
            "terminal_id": "TBOX-H1",
            "cmd_id": cmd_id,
            "status": "ok",
            "result": {"code": 0},
        },
    )
    assert r.status == 200

    r = await client.get("/admin/history", params={"terminal_id": "TBOX-H1"})
    assert r.status == 200
    body = await r.json()
    assert len(body["history"]) == 1
    assert body["history"][0]["cmd_id"] == cmd_id
    assert body["history"][0]["status"] == "ok"


async def test_admin_history_404_for_unknown_terminal(client: TestClient):
    r = await client.get("/admin/history", params={"terminal_id": "NEVER"})
    assert r.status == 404


async def test_admin_history_missing_terminal_id(client: TestClient):
    r = await client.get("/admin/history")
    assert r.status == 400


# ---- /webui ----


async def test_webui_index_served(client: TestClient):
    r = await client.get("/")
    assert r.status == 200
    body = await r.text()
    assert "<title>tbox-server" in body


async def test_webui_static_assets_served(client: TestClient):
    r = await client.get("/webui/style.css")
    assert r.status == 200
    assert "css" in (r.headers.get("content-type", "")).lower()
    r = await client.get("/webui/app.js")
    assert r.status == 200
    assert "javascript" in (r.headers.get("content-type", "")).lower()
