"""Tests for filesystem storage helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tbox_server.models import UploadMeta
from tbox_server.storage import (
    cleanup_old_data,
    data_disk_usage,
    list_status_report,
    list_uploads,
    read_status_report,
    store_status_report,
    store_upload,
)


async def _chunks(data: bytes, size: int = 1024):
    for i in range(0, len(data), size):
        yield data[i : i + size]


# ---- upload ----


async def test_store_upload_writes_file_and_meta(tmp_path: Path):
    data = b"hello tbox" * 100
    sha = hashlib.sha256(data).hexdigest()
    meta = UploadMeta(
        terminal_id="TBOX-1",
        cmd_id="cmd-abc",
        sha256=sha,
        size=len(data),
    )
    stored = await store_upload(tmp_path, meta, _chunks(data), max_bytes=1024 * 1024)

    assert stored.path.exists()
    assert stored.sha256 == sha
    assert stored.bytes_written == len(data)
    assert stored.meta_path.exists()
    assert stored.path.read_bytes() == data

    sidecar = json.loads(stored.meta_path.read_text(encoding="utf-8"))
    assert sidecar["cmd_id"] == "cmd-abc"
    assert sidecar["terminal_id"] == "TBOX-1"


async def test_store_upload_rejects_sha_mismatch(tmp_path: Path):
    data = b"hello tbox"
    meta = UploadMeta(terminal_id="TBOX-1", cmd_id="c", sha256="0" * 64, size=len(data))
    with pytest.raises(ValueError, match="sha256 mismatch"):
        await store_upload(tmp_path, meta, _chunks(data), max_bytes=1024)


async def test_store_upload_rejects_size_mismatch(tmp_path: Path):
    data = b"hello"
    meta = UploadMeta(terminal_id="TBOX-1", cmd_id="c", sha256="", size=999)
    with pytest.raises(ValueError, match="size mismatch"):
        await store_upload(tmp_path, meta, _chunks(data), max_bytes=1024)


async def test_store_upload_enforces_max_bytes(tmp_path: Path):
    data = b"x" * 1024
    meta = UploadMeta(terminal_id="TBOX-1", cmd_id="c")
    with pytest.raises(OverflowError):
        await store_upload(tmp_path, meta, _chunks(data), max_bytes=512)


# ---- status report ----


async def test_store_status_report_writes_only_latest(tmp_path: Path):
    payload = {"iccid": "8986", "imei": "86", "vin": "LVX"}
    stored = await store_status_report(tmp_path, "TBOX-1", payload)
    assert stored.latest_path.exists()
    assert stored.stored == payload
    # Overwriting keeps only latest.json (no history dir).
    assert not (stored.latest_path.parent / "history").exists()


async def test_store_status_report_overwrites(tmp_path: Path):
    await store_status_report(tmp_path, "TBOX-1", {"a": 1})
    await store_status_report(tmp_path, "TBOX-1", {"a": 2})
    out = read_status_report(tmp_path, "TBOX-1")
    assert out == {"a": 2}


async def test_store_status_report_rejects_non_object(tmp_path: Path):
    with pytest.raises(ValueError, match="must be a JSON object"):
        await store_status_report(tmp_path, "TBOX-1", [1, 2, 3])  # type: ignore[arg-type]


def test_read_status_report_missing_returns_none(tmp_path: Path):
    assert read_status_report(tmp_path, "TBOX-NONE") is None


def test_list_status_report_returns_descriptor(tmp_path: Path):
    import asyncio

    asyncio.run(store_status_report(tmp_path, "TBOX-1", {"x": 1}))
    desc = list_status_report(tmp_path, "TBOX-1")
    assert desc is not None
    assert desc["type"] == "status"
    assert desc["latest_path"].endswith("latest.json")


def test_list_status_report_missing_returns_none(tmp_path: Path):
    assert list_status_report(tmp_path, "TBOX-NONE") is None


# ---- retention cleanup ----


async def _make_old_upload(data_dir: Path, tid: str, day_str: str, body: bytes) -> Path:
    p = data_dir / "uploads" / day_str / tid
    p.mkdir(parents=True, exist_ok=True)
    f = p / f"old__{body.decode().replace(' ', '_')}.bin"
    f.write_bytes(body)
    (p / f"{f.name}.meta.json").write_text('{"k":"v"}')
    # backdate mtime to 3 days ago — must also backdate the date_dir itself,
    # otherwise the cleanup sweep (which checks dir mtime) won't see it.
    import os
    import time

    old = time.time() - 3 * 86400
    os.utime(f, (old, old))
    os.utime(p / f"{f.name}.meta.json", (old, old))
    os.utime(p, (old, old))
    os.utime(p.parent, (old, old))
    return f


async def _make_fresh_upload(data_dir: Path, tid: str, body: bytes) -> Path:
    import time

    day = time.strftime("%Y-%m-%d", time.gmtime())
    p = data_dir / "uploads" / day / tid
    p.mkdir(parents=True, exist_ok=True)
    f = p / "fresh.bin"
    f.write_bytes(body)
    return f


def test_cleanup_old_data_removes_old_upload_dirs(tmp_path: Path):
    import asyncio
    import time

    asyncio.run(_make_old_upload(tmp_path, "TBOX-1", "2020-01-01", b"old"))
    asyncio.run(_make_fresh_upload(tmp_path, "TBOX-1", b"new"))
    rep = cleanup_old_data(tmp_path, max_age_s=86400, now=time.time())
    # old YYYY-MM-DD dir gone
    assert not (tmp_path / "uploads" / "2020-01-01").exists()
    # fresh stays
    assert (tmp_path / "uploads").exists()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    assert (tmp_path / "uploads" / today).exists()
    assert rep.uploads_dirs_removed >= 1
    assert rep.bytes_freed > 0


def test_cleanup_old_data_removes_old_status_file(tmp_path: Path):
    import asyncio
    import os
    import time

    asyncio.run(store_status_report(tmp_path, "TBOX-1", {"a": 1}))
    latest = tmp_path / "reports" / "TBOX-1" / "status" / "latest.json"
    assert latest.exists()
    old = time.time() - 3 * 86400
    os.utime(latest, (old, old))
    rep = cleanup_old_data(tmp_path, max_age_s=86400, now=time.time())
    assert rep.reports_files_removed == 1
    assert not latest.exists()


def test_cleanup_disabled_when_retention_zero(tmp_path: Path):
    import asyncio
    import time

    asyncio.run(_make_old_upload(tmp_path, "TBOX-1", "2020-01-01", b"old"))
    rep = cleanup_old_data(tmp_path, max_age_s=0, now=time.time())
    assert rep.uploads_dirs_removed == 0
    assert (tmp_path / "uploads" / "2020-01-01").exists()


def test_data_disk_usage_breakdown(tmp_path: Path):
    import asyncio

    asyncio.run(_make_old_upload(tmp_path, "TBOX-1", "2020-01-01", b"x" * 100))
    usage = data_disk_usage(tmp_path)
    assert "uploads" in usage["categories"]
    assert usage["categories"]["uploads"]["bytes"] >= 100
    assert usage["total_bytes"] >= 100


# ---- upload listing ----


async def test_list_uploads_empty(tmp_path: Path):
    assert list_uploads(tmp_path, "TBOX-NONE") == []


async def test_list_uploads_returns_meta(tmp_path: Path):
    data = b"x" * 100
    sha = hashlib.sha256(data).hexdigest()
    meta = UploadMeta(terminal_id="TBOX-1", cmd_id="cmd-abc", sha256=sha, size=len(data))
    stored = await store_upload(tmp_path, meta, _chunks(data), max_bytes=1024 * 1024)
    out = list_uploads(tmp_path, "TBOX-1")
    assert len(out) == 1
    assert out[0]["filename"] == stored.path.name
    assert out[0]["cmd_id"] == "cmd-abc"
    assert out[0]["sha256"] == sha
    assert out[0]["size"] == len(data)


async def test_list_uploads_skips_meta_files_and_others(tmp_path: Path):
    data = b"hello"
    meta = UploadMeta(terminal_id="TBOX-1", cmd_id="c1")
    await store_upload(tmp_path, meta, _chunks(data), max_bytes=1024)
    out = list_uploads(tmp_path, "TBOX-1")
    assert len(out) == 1
    assert not out[0]["filename"].endswith(".meta.json")


async def test_list_uploads_orders_newest_first(tmp_path: Path):
    import os
    import time

    data = b"x"
    meta = UploadMeta(terminal_id="TBOX-1", cmd_id="c1")
    s1 = await store_upload(tmp_path, meta, _chunks(data), max_bytes=1024)
    # Backdate the first upload so c2 is newer.
    old = time.time() - 100
    os.utime(s1.path, (old, old))
    meta2 = UploadMeta(terminal_id="TBOX-1", cmd_id="c2")
    s2 = await store_upload(tmp_path, meta2, _chunks(data), max_bytes=1024)
    out = list_uploads(tmp_path, "TBOX-1")
    assert [r["cmd_id"] for r in out] == ["c2", "c1"]
