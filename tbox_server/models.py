"""Dataclasses for the JSON envelopes exchanged with terminals.

Schema follows ``TBOXHELPER_PROTOCOL.md`` v2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Heartbeat:
    """``POST /heartbeat`` body. Only ``terminal_id`` is required."""

    terminal_id: str
    service: str = ""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Heartbeat":
        if not isinstance(data, dict):
            raise ValueError("heartbeat body must be a JSON object")
        tid = data.get("terminal_id")
        if not isinstance(tid, str) or not tid.strip():
            raise ValueError("terminal_id is required")
        if len(tid) > 64:
            raise ValueError("terminal_id too long (max 64 chars)")
        return cls(
            terminal_id=tid.strip(),
            service=str(data.get("service", "")),
        )


@dataclass(slots=True)
class Command:
    """A queued command the server delivers via ``GET /poll``.

    ``type`` is one of :data:`tbox_server.protocol.CMD_TYPES`.
    ``payload`` schema depends on ``type`` — see TBOXHELPER_PROTOCOL §4.
    """

    cmd_id: str
    terminal_id: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    issued_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "cmd_id": self.cmd_id,
            "type": self.type,
            "payload": self.payload,
            "issued_at": self.issued_at,
        }


@dataclass(slots=True)
class UploadMeta:
    """``POST /upload`` metadata part.

    ``cmd_id`` must reference a command that was queued for the same
    terminal — the handler rejects uploads whose ``cmd_id`` is unknown.
    ``sha256`` / ``size`` are optional: when non-empty / non-zero the
    handler enforces them against the bytes actually received.
    """

    terminal_id: str
    cmd_id: str = ""
    sha256: str = ""
    size: int = 0

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "UploadMeta":
        if not isinstance(data, dict):
            raise ValueError("upload metadata must be a JSON object")
        tid = data.get("terminal_id")
        cmd_id = data.get("cmd_id")
        if not isinstance(tid, str) or not tid.strip():
            raise ValueError("terminal_id is required")
        if not isinstance(cmd_id, str) or not cmd_id.strip():
            raise ValueError("cmd_id is required")
        return cls(
            terminal_id=tid.strip(),
            cmd_id=cmd_id.strip(),
            sha256=str(data.get("sha256", "")),
            size=int(data.get("size", 0) or 0),
        )
