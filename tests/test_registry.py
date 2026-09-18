"""Unit tests for the in-memory terminal registry."""

from __future__ import annotations

import asyncio

import pytest

from tbox_server.config import Config
from tbox_server.models import Command
from tbox_server.registry import TerminalRegistry
from tbox_server.utils import utcnow


def _cfg(**overrides) -> Config:
    base = dict(
        offline_after_s=90,
        long_poll_timeout_s=30,
        long_poll_max_timeout_s=120,
        reaper_interval_s=10,
        max_pending_commands_per_terminal=4,
    )
    base.update(overrides)
    return Config(**base)


async def test_heartbeat_creates_session_and_updates_last_seen():
    reg = TerminalRegistry(_cfg())
    sess = await reg.heartbeat("TBOX-1", "tboxhelper")
    assert sess.terminal_id == "TBOX-1"
    assert sess.service == "tboxhelper"
    assert reg.get("TBOX-1") is sess


async def test_heartbeat_records_service_field():
    reg = TerminalRegistry(_cfg())
    await reg.heartbeat("TBOX-S", "")
    assert reg.get("TBOX-S").service == ""
    await reg.heartbeat("TBOX-S", "another-service")
    assert reg.get("TBOX-S").service == "another-service"


async def test_enqueue_command_lazy_creates_session_and_registers_cmd():
    reg = TerminalRegistry(_cfg())
    cmd = Command(cmd_id="c1", terminal_id="TBOX-2", type="exec", issued_at=utcnow())
    sess = await reg.enqueue_command("TBOX-2", cmd)
    assert sess is not None
    assert sess.queue.qsize() == 1
    assert "c1" in sess.known_cmd_ids
    assert reg.cmd_known("TBOX-2", "c1") is True
    assert reg.cmd_known("TBOX-2", "unknown") is False
    assert reg.cmd_known("NEVER", "c1") is False


async def test_long_poll_returns_command_then_idle():
    reg = TerminalRegistry(_cfg(long_poll_timeout_s=1))
    await reg.heartbeat("TBOX-3", "tboxhelper")
    cmd = Command(cmd_id="c1", terminal_id="TBOX-3", type="exec", issued_at=utcnow())
    await reg.enqueue_command("TBOX-3", cmd)

    got = await reg.next_command("TBOX-3", timeout=2)
    assert got is not None and got.cmd_id == "c1"

    # next call should time out quickly
    start = asyncio.get_event_loop().time()
    got = await reg.next_command("TBOX-3", timeout=1)
    elapsed = asyncio.get_event_loop().time() - start
    assert got is None
    assert elapsed >= 0.9


async def test_record_ack_returns_false_for_unknown_cmd_id():
    reg = TerminalRegistry(_cfg())
    await reg.heartbeat("TBOX-A", "tboxhelper")
    assert await reg.record_ack("TBOX-A", "nope", "ok", {}, utcnow()) is False


async def test_record_ack_returns_false_for_unknown_terminal():
    reg = TerminalRegistry(_cfg())
    assert await reg.record_ack("NEVER", "x", "ok", {}, utcnow()) is False


async def test_record_ack_succeeds_for_known_cmd_and_records_history():
    reg = TerminalRegistry(_cfg())
    await reg.heartbeat("TBOX-B", "tboxhelper")
    cmd = Command(cmd_id="known", terminal_id="TBOX-B", type="exec", issued_at=utcnow())
    await reg.enqueue_command("TBOX-B", cmd)

    ok = await reg.record_ack("TBOX-B", "known", "ok", {"code": 0}, utcnow())
    assert ok is True
    sess = reg.get("TBOX-B")
    assert sess.history["known"]["status"] == "ok"
    assert sess.history["known"]["result"] == {"code": 0}


async def test_max_queue_size_enforced():
    cfg = _cfg(max_pending_commands_per_terminal=2)
    reg = TerminalRegistry(cfg)
    for i in range(2):
        await reg.enqueue_command(
            "TBOX-5",
            Command(cmd_id=f"c{i}", terminal_id="TBOX-5", type="exec", issued_at=utcnow()),
        )
    with pytest.raises(RuntimeError):
        await reg.enqueue_command(
            "TBOX-5",
            Command(cmd_id="cX", terminal_id="TBOX-5", type="exec", issued_at=utcnow()),
        )
