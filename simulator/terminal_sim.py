#!/usr/bin/env python3
"""Terminal simulator: stand-in for a real tbox device talking to the server.

Implements the v2 protocol (TBOXHELPER_PROTOCOL.md):
- POST /heartbeat  (terminal_id + service)
- GET  /poll       (long-poll for commands)
- POST /upload     (multipart, metadata.cmd_id is required)
- POST /command/ack
- POST /report     (status report every REPORT_PERIOD_S seconds)

Usage:
    TBOX_URL=http://127.0.0.1:8080 TERMINAL_ID=TBOX-0001 \
        python simulator/terminal_sim.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import signal
import subprocess
import sys
import time
from typing import Any

import aiohttp


URL = os.environ.get("TBOX_URL", "http://127.0.0.1:8080")
TID = os.environ.get("TERMINAL_ID", "TBOX-0001")
SERVICE = os.environ.get("SERVICE", "tboxhelper")
HEARTBEAT_PERIOD_S = int(os.environ.get("HEARTBEAT_PERIOD_S", "30"))
POLL_WAIT_S = int(os.environ.get("POLL_WAIT_S", "25"))
REPORT_PERIOD_S = int(os.environ.get("REPORT_PERIOD_S", "10"))

log = logging.getLogger("terminal-sim")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


# ---- HTTP helpers ----


async def heartbeat(session: aiohttp.ClientSession, tid: str) -> dict:
    body = {"terminal_id": tid, "service": SERVICE}
    async with session.post(f"{URL}/heartbeat", json=body) as r:
        r.raise_for_status()
        return await r.json()


async def long_poll_once(session: aiohttp.ClientSession, tid: str, wait: int) -> dict | None:
    async with session.get(
        f"{URL}/poll",
        params={"wait": str(wait)},
        headers={"X-Terminal-Id": tid},
        timeout=aiohttp.ClientTimeout(total=wait + 10),
    ) as r:
        if r.status == 404:
            return None
        r.raise_for_status()
        return await r.json()


async def ack(session: aiohttp.ClientSession, tid: str, cmd_id: str, status: str, result: dict) -> None:
    body = {
        "terminal_id": tid,
        "cmd_id": cmd_id,
        "status": status,
        "result": result,
        "timestamp": int(time.time()),
    }
    async with session.post(f"{URL}/command/ack", json=body) as r:
        r.raise_for_status()


async def upload_file(
    session: aiohttp.ClientSession, tid: str, cmd_id: str, path: str
) -> tuple[bool, str]:
    """Upload ``path`` to /upload tied to ``cmd_id``. Returns (ok, message)."""
    if not os.path.isfile(path):
        return False, "file not found"
    data = open(path, "rb").read()
    sha = hashlib.sha256(data).hexdigest()
    form = aiohttp.FormData()
    form.add_field(
        "metadata",
        json.dumps(
            {
                "terminal_id": tid,
                "cmd_id": cmd_id,
                "sha256": sha,
                "size": len(data),
            }
        ),
        content_type="application/json",
    )
    form.add_field(
        "file", data, filename=os.path.basename(path), content_type="application/octet-stream"
    )
    async with session.post(f"{URL}/upload", data=form) as r:
        if r.status != 200:
            return False, f"upload HTTP {r.status}: {await r.text()}"
        return True, "ok"


async def report_status(session: aiohttp.ClientSession, tid: str) -> None:
    """Send a synthetic /report status payload to the server."""
    payload = {
        "terminal_id": tid,
        "service": SERVICE,
        "report_type": "status",
        "timestamp": int(time.time()),
        "data": {
            "position": {
                "lat": 22.5 + random.random(),
                "lon": 114.0 + random.random(),
                "alt": 10.0,
                "speed": 0.0,
                "track": random.uniform(0, 360),
                "status": 1,
                "mode": 3,
            },
            "iccid": "89860123456789012345",
            "imei": "866987123456789",
            "imsi": "460011234567890",
            "vin": "LVX12345678901234",
            "sn": tid,
            "vehicle_model": "JMC-CX835",
            "dtc": [],
        },
    }
    async with session.post(f"{URL}/report", json=payload) as r:
        if r.status != 200:
            log.warning("report status HTTP %s: %s", r.status, await r.text())


# ---- command handlers ----


def _result_ok(code: int, output: str = "") -> dict:
    return {"code": code, "output": output}


def _result_err(code: int, message: str) -> dict:
    return {"code": code, "output": message}


async def _execute_exec(payload: dict) -> dict:
    cmd = payload.get("command", "")
    timeout = int(payload.get("timeout", 30))
    if not cmd:
        return _result_err(8999, "missing payload.command")
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return _result_ok(proc.returncode, proc.stdout + proc.stderr)
    except subprocess.TimeoutExpired:
        return _result_err(8101, f"exec timeout after {timeout}s")
    except Exception as e:  # pragma: no cover - defensive
        return _result_err(8999, f"exec failed: {e}")


async def _execute_upload(payload: dict) -> dict:
    file_type = payload.get("file_type")
    if file_type == "log":
        # Synthesize a tarball-like blob as the "log" payload. The simulator
        # doesn't have access to /var/log/messages* — just write a stub.
        path = "/tmp/tbox-sim-logs.tar.gz"
        with open(path, "wb") as f:
            f.write(b"simulated messages tarball\n")
        return _result_ok(0, f"prepared {path}")
    if file_type == "other":
        path = payload.get("path")
        if not path:
            return _result_err(8999, "missing payload.path")
        if not os.path.isfile(path):
            return _result_err(8001, f"file not found: {path}")
        return _result_ok(0, f"will upload {path}")
    return _result_err(8999, f"unsupported file_type {file_type!r}")


async def _run_command(
    session: aiohttp.ClientSession, tid: str, cmd: dict
) -> None:
    """Dispatch a command from /poll, upload files if needed, then ack."""
    cmd_id = cmd.get("cmd_id")
    cmd_type = cmd.get("type")
    payload = cmd.get("payload") or {}
    log.info("received command: cmd_id=%s type=%s payload=%s", cmd_id, cmd_type, payload)

    status = "ok"
    result: dict[str, Any] = {"code": 0}

    try:
        if cmd_type == "exec":
            result = await _execute_exec(payload)
            if result["code"] != 0:
                status = "fail"
        elif cmd_type == "upload":
            # Step 1: figure out which file to upload (and prepare it if log).
            prep = await _execute_upload(payload)
            if prep["code"] != 0:
                status = "fail"
                result = prep
            else:
                file_type = payload.get("file_type")
                path = (
                    "/tmp/tbox-sim-logs.tar.gz"
                    if file_type == "log"
                    else payload["path"]
                )
                ok, msg = await upload_file(session, tid, cmd_id, path)
                if not ok:
                    status = "fail"
                    result = _result_err(8006, f"upload failed: {msg}")
                else:
                    result = _result_ok(0, "uploaded")
        else:
            status = "fail"
            result = _result_err(8999, f"unknown command type {cmd_type!r}")
    except Exception as e:  # pragma: no cover - defensive
        status = "fail"
        result = _result_err(8999, f"handler crashed: {e}")

    try:
        await ack(session, tid, cmd_id, status, result)
        log.info("acked command %s status=%s result=%s", cmd_id, status, result)
    except Exception as e:
        log.warning("ack failed: %s", e)


# ---- main loop ----


async def main() -> int:
    setup_logging()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    async with aiohttp.ClientSession() as session:
        # First heartbeat (also discovers the server-suggested interval).
        try:
            hb = await heartbeat(session, TID)
        except Exception as e:
            log.error("initial heartbeat failed: %s", e)
            return 1
        interval = int(hb.get("heartbeat_interval_s", HEARTBEAT_PERIOD_S))
        log.info("heartbeat ok: %s", hb)

        hb_task = asyncio.create_task(_heartbeat_loop(session, TID, interval, stop))
        poll_task = asyncio.create_task(_poll_loop(session, TID, interval, stop))
        report_task = asyncio.create_task(_report_loop(session, TID, stop))

        await stop.wait()
        log.info("stopping…")
        for t in (hb_task, poll_task, report_task):
            t.cancel()
        await asyncio.gather(hb_task, poll_task, report_task, return_exceptions=True)
    return 0


async def _heartbeat_loop(session, tid, interval, stop):
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await heartbeat(session, tid)
        except Exception as e:
            log.warning("heartbeat failed: %s", e)


async def _poll_loop(session, tid, _interval, stop):
    while not stop.is_set():
        try:
            data = await long_poll_once(session, tid, POLL_WAIT_S)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("poll failed: %s", e)
            await asyncio.sleep(2)
            continue
        if data is None:
            # Terminal was unregistered (or 404); try to re-heartbeat.
            try:
                await heartbeat(session, tid)
            except Exception as e:
                log.warning("re-heartbeat failed: %s", e)
                await asyncio.sleep(2)
            continue
        if data.get("status") == "command":
            await _run_command(session, tid, data["cmd"])


async def _report_loop(session, tid, stop):
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=REPORT_PERIOD_S)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await report_status(session, tid)
        except Exception as e:
            log.warning("report failed: %s", e)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
