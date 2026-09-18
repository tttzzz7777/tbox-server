"""Configuration loading from environment variables.

All settings have sensible defaults so the server can boot with zero config.
Override anything with `TBOX_<NAME>` (uppercased) environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else raw


@dataclass(slots=True)
class Config:
    host: str = "0.0.0.0"
    port: int = 9999
    data_dir: Path = field(default_factory=lambda: Path("./data"))

    # Heartbeat / liveness
    heartbeat_interval_s: int = 30
    offline_after_s: int = 90  # mark offline if last_seen older than this

    # Long polling
    long_poll_timeout_s: int = 30
    long_poll_max_timeout_s: int = 120

    # Background reaper
    reaper_interval_s: int = 10

    # Upload limits
    max_upload_bytes: int = 64 * 1024 * 1024  # 64 MiB

    # Command queue
    default_command_ttl_s: int = 3600
    max_pending_commands_per_terminal: int = 32

    # Data retention — how long terminal-uploaded files (uploads/,
    # reports/<tid>/status/latest.json) are kept on disk. 0 disables cleanup.
    data_retention_s: int = 86400  # 24 hours
    # How often the background task runs the cleanup sweep.
    data_cleanup_interval_s: int = 3600  # 1 hour

    server_version: str = "0.1.0"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            host=_env_str("TBOX_HOST", "0.0.0.0"),
            port=_env_int("TBOX_PORT", 9999),
            data_dir=Path(_env_str("TBOX_DATA_DIR", "./data")),
            heartbeat_interval_s=_env_int("TBOX_HEARTBEAT_INTERVAL_S", 30),
            offline_after_s=_env_int("TBOX_OFFLINE_AFTER_S", 90),
            long_poll_timeout_s=_env_int("TBOX_LONG_POLL_TIMEOUT_S", 30),
            long_poll_max_timeout_s=_env_int("TBOX_LONG_POLL_MAX_TIMEOUT_S", 120),
            reaper_interval_s=_env_int("TBOX_REAPER_INTERVAL_S", 10),
            max_upload_bytes=_env_int("TBOX_MAX_UPLOAD_BYTES", 64 * 1024 * 1024),
            default_command_ttl_s=_env_int("TBOX_DEFAULT_COMMAND_TTL_S", 3600),
            max_pending_commands_per_terminal=_env_int(
                "TBOX_MAX_PENDING_COMMANDS_PER_TERMINAL", 32
            ),
            data_retention_s=_env_int("TBOX_DATA_RETENTION_S", 86400),
            data_cleanup_interval_s=_env_int("TBOX_DATA_CLEANUP_INTERVAL_S", 3600),
            server_version=_env_str("TBOX_SERVER_VERSION", "0.1.0"),
        )
