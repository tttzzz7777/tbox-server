"""Small utility helpers shared across the package."""

from __future__ import annotations

import re
import time
import uuid

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def utcnow() -> float:
    """Return the current UNIX timestamp (seconds, float)."""
    return time.time()


def utcnow_date_str() -> str:
    """Return today's date in YYYY-MM-DD (UTC)."""
    t = time.gmtime()
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


def new_id() -> str:
    """Generate a short random id (uuid4 hex)."""
    return uuid.uuid4().hex


def safe_filename(name: str, fallback: str = "file") -> str:
    """Sanitize a filename so it is safe to use on disk and in URLs.

    Replaces anything outside [A-Za-z0-9._-] with '_'. Trims leading dots and
    underscores, then truncates to 200 chars. Falls back to ``fallback`` when
    nothing usable is left.
    """
    if not name:
        return fallback
    cleaned = _SAFE_NAME_RE.sub("_", name).strip("._")
    if not cleaned:
        return fallback
    return cleaned[:200]


def clamp(value: int, lo: int, hi: int) -> int:
    """Clamp ``value`` into ``[lo, hi]``."""
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value