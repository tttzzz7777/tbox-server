"""JSON protocol helpers and command-type constants.

See ``TBOXHELPER_PROTOCOL.md`` (v2) for the wire-level spec.
"""

from __future__ import annotations

from typing import Any

# Commands the server may push to terminals.
CMD_TYPES: frozenset[str] = frozenset({"upload", "exec"})


def envelope(kind: str, **payload: Any) -> dict[str, Any]:
    """Build a simple JSON envelope: ``{"status": kind, ...payload}``."""
    return {"status": kind, **payload}


def ok(**payload: Any) -> dict[str, Any]:
    return envelope("ok", **payload)


def error(message: str, **extra: Any) -> dict[str, Any]:
    return envelope("error", message=message, **extra)
