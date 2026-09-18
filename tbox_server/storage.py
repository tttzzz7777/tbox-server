"""Filesystem storage for uploads and status reports.

Layout (all paths derived from ``data_dir``):
    data_dir/
        uploads/YYYY-MM-DD/<terminal_id>/<sha256>__<safe_name> [+ .meta.json]
        reports/<terminal_id>/status/latest.json      (overwritten each /report)
        state/                                         (reserved)

The previous v1 layout (``logs/``, ``versions/``) is no longer written by
the server. Old files left on disk are left in place by reads (so any
stale backup doesn't error) but are not produced.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from .models import UploadMeta
from .utils import safe_filename, utcnow_date_str

log = logging.getLogger(__name__)

_CHUNK = 64 * 1024


@dataclass(slots=True)
class Stored:
    path: Path
    bytes_written: int
    sha256: str
    meta_path: Path


def upload_dir(data_dir: Path, terminal_id: str) -> Path:
    p = data_dir / "uploads" / utcnow_date_str() / terminal_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def status_report_path(data_dir: Path, terminal_id: str) -> Path:
    """``data_dir/reports/<terminal_id>/status/latest.json`` (created on demand)."""
    p = data_dir / "reports" / terminal_id / "status"
    p.mkdir(parents=True, exist_ok=True)
    return p / "latest.json"


# ---------- upload ----------


async def store_upload(
    data_dir: Path,
    meta: UploadMeta,
    source: AsyncIterator[bytes],
    max_bytes: int,
) -> Stored:
    """Stream ``source`` to disk, hashing and size-checking on the fly.

    Writes to ``<final>.part`` first, fsyncs, then atomically renames. Raises
    ``ValueError`` on size/sha mismatch, ``OverflowError`` on too-large uploads.
    """
    target_dir = upload_dir(data_dir, meta.terminal_id)
    # Group multiple uploads for the same cmd_id by reusing the cmd_id
    # prefix as the safe name. The file name carries the cmd_id so admin
    # can trace which command produced the file.
    base_name = safe_filename(meta.cmd_id or "file", fallback="file")
    sha_prefix = (meta.sha256 or "nohash")[:16]
    final = target_dir / f"{sha_prefix}__{base_name}"
    part = final.with_suffix(final.suffix + ".part")

    hasher = hashlib.sha256()
    total = 0
    loop = asyncio.get_running_loop()
    # Open the part file in a thread so we don't stall the event loop.
    fd = await loop.run_in_executor(None, os.open, str(part), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        async for chunk in source:
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise OverflowError(f"upload exceeds {max_bytes} bytes")
            hasher.update(chunk)
            await loop.run_in_executor(None, os.write, fd, chunk)
        await loop.run_in_executor(None, os.fsync, fd)
    finally:
        await loop.run_in_executor(None, os.close, fd)

    digest = hasher.hexdigest()
    if meta.sha256 and meta.sha256 != digest:
        # remove the partial file before raising
        try:
            part.unlink()
        except OSError:
            pass
        raise ValueError(
            f"sha256 mismatch: declared={meta.sha256} actual={digest}"
        )
    if meta.size and meta.size != total:
        try:
            part.unlink()
        except OSError:
            pass
        raise ValueError(
            f"size mismatch: declared={meta.size} actual={total}"
        )

    # atomic rename
    await loop.run_in_executor(None, os.replace, str(part), str(final))

    meta_path = final.with_suffix(final.suffix + ".meta.json")
    sidecar = {
        "terminal_id": meta.terminal_id,
        "cmd_id": meta.cmd_id,
        "sha256": digest,
        "size": total,
        "stored": str(final),
        "stored_at": utcnow_date_str(),
    }
    meta_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")

    log.info(
        "stored upload terminal=%s cmd_id=%s bytes=%d sha=%s path=%s",
        meta.terminal_id,
        meta.cmd_id,
        total,
        digest,
        final,
    )
    return Stored(path=final, bytes_written=total, sha256=digest, meta_path=meta_path)


# ---------- status report ----------


@dataclass(slots=True)
class StoredStatusReport:
    latest_path: Path
    stored: dict[str, Any]  # the parsed JSON body, returned to the caller


async def store_status_report(
    data_dir: Path,
    terminal_id: str,
    payload: dict[str, Any],
) -> StoredStatusReport:
    """Persist a ``/report`` body as the terminal's latest status snapshot.

    The file is overwritten on every call — there is no history archive in
    v2 (the protocol only stores a single ``latest.json`` per terminal).
    """
    if not isinstance(payload, dict):
        raise ValueError("status report body must be a JSON object")
    p = status_report_path(data_dir, terminal_id)
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log.info("stored status report terminal=%s bytes=%d path=%s", terminal_id, p.stat().st_size, p)
    return StoredStatusReport(latest_path=p, stored=payload)


def read_status_report(data_dir: Path, terminal_id: str) -> dict[str, Any] | None:
    """Read the latest status report JSON body. Returns None if missing."""
    p = data_dir / "reports" / terminal_id / "status" / "latest.json"
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, json.JSONDecodeError):
        return None


def list_status_report(data_dir: Path, terminal_id: str) -> dict[str, Any] | None:
    """Return a single-entry descriptor for the terminal's status report, or None."""
    p = data_dir / "reports" / terminal_id / "status" / "latest.json"
    if not p.exists():
        return None
    return {
        "type": "status",
        "latest_path": str(p),
        "latest_mtime": p.stat().st_mtime,
    }


# ---------- uploaded files ----------


def list_uploads(data_dir: Path, terminal_id: str) -> list[dict[str, Any]]:
    """Enumerate every file uploaded by ``terminal_id`` across all date dirs.

    Walks ``data_dir/uploads/<YYYY-MM-DD>/<tid>/`` and returns each non-meta
    file with its size, mtime, and the cmd_id / sha256 from the sidecar
    JSON (if present). Newest first.
    """
    base = data_dir / "uploads"
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for date_dir in base.iterdir():
        if not date_dir.is_dir():
            continue
        tid_dir = date_dir / terminal_id
        if not tid_dir.is_dir():
            continue
        for f in tid_dir.iterdir():
            if not f.is_file():
                continue
            if f.name.endswith(".meta.json"):
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            sidecar: dict[str, Any] = {}
            meta_path = f.with_suffix(f.suffix + ".meta.json")
            if meta_path.exists():
                try:
                    sidecar = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    sidecar = {}
            out.append(
                {
                    "filename": f.name,
                    "path": str(f),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "date_dir": date_dir.name,
                    "cmd_id": sidecar.get("cmd_id", ""),
                    "sha256": sidecar.get("sha256", ""),
                    "stored_at": sidecar.get("stored_at", ""),
                }
            )
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out


# ---------- retention cleanup ----------


@dataclass(slots=True)
class CleanupReport:
    """Result of one cleanup sweep."""

    uploads_dirs_removed: int = 0
    reports_files_removed: int = 0
    reports_empty_dirs_removed: int = 0
    bytes_freed: int = 0
    skipped_recent: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "uploads_dirs_removed": self.uploads_dirs_removed,
            "reports_files_removed": self.reports_files_removed,
            "reports_empty_dirs_removed": self.reports_empty_dirs_removed,
            "bytes_freed": self.bytes_freed,
            "skipped_recent": self.skipped_recent,
        }


def _dir_size(p: Path) -> int:
    """Best-effort recursive size in bytes. Returns 0 on any error."""
    total = 0
    try:
        for child in p.rglob("*"):
            if child.is_file():
                try:
                    total += child.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _safe_rmtree(p: Path) -> int:
    """Remove a directory tree and return the bytes freed. Logs failures."""
    freed = _dir_size(p)
    try:
        import shutil

        shutil.rmtree(p)
        return freed
    except OSError as e:
        log.warning("cleanup: failed to remove %s: %s", p, e)
        return 0


def cleanup_old_data(
    data_dir: Path,
    max_age_s: float,
    now: float | None = None,
) -> CleanupReport:
    """Sweep terminal-uploaded data older than ``max_age_s`` from disk.

    Cleans:
      - ``data_dir/uploads/YYYY-MM-DD/``     — entire date dirs older than the
        retention window
      - ``data_dir/reports/<tid>/status/latest.json`` — overwritten-only, so
        when the file's mtime is older than the window the server removes it
        and the per-status directory. The terminal's parent report dir is
        only tidied up when empty.

    Skipped (never touched):
      - anything else under ``data_dir/``
    """
    import time

    rep = CleanupReport()
    if max_age_s <= 0:
        return rep  # retention disabled
    now = now if now is not None else time.time()
    cutoff = now - max_age_s

    # uploads/ — whole YYYY-MM-DD directories
    base = data_dir / "uploads"
    if base.exists():
        for date_dir in base.iterdir():
            if not date_dir.is_dir():
                continue
            try:
                mtime = date_dir.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                rep.skipped_recent += 1
                continue
            freed = _safe_rmtree(date_dir)
            rep.bytes_freed += freed
            rep.uploads_dirs_removed += 1
            log.info("cleanup: removed %s (%d bytes)", date_dir, freed)

    # reports/<tid>/status/latest.json — per-file mtime check
    reports_root = data_dir / "reports"
    if reports_root.exists():
        for tid_dir in reports_root.iterdir():
            if not tid_dir.is_dir():
                continue
            status_dir = tid_dir / "status"
            if not status_dir.exists():
                continue
            latest = status_dir / "latest.json"
            if latest.exists():
                try:
                    mtime = latest.stat().st_mtime
                except OSError:
                    mtime = 0.0
                if mtime >= cutoff:
                    rep.skipped_recent += 1
                else:
                    try:
                        size = latest.stat().st_size
                        latest.unlink()
                        rep.bytes_freed += size
                        rep.reports_files_removed += 1
                        log.info("cleanup: removed %s (%d bytes)", latest, size)
                    except OSError as e:
                        log.warning("cleanup: failed to remove %s: %s", latest, e)
            # tidy empty dirs
            for d in (status_dir, tid_dir):
                try:
                    if d.exists() and not any(d.iterdir()):
                        d.rmdir()
                        if d is status_dir:
                            rep.reports_empty_dirs_removed += 1
                except OSError:
                    pass

    if rep.uploads_dirs_removed or rep.reports_files_removed:
        log.info(
            "cleanup: removed %d upload-dirs, %d status files, freed %d bytes",
            rep.uploads_dirs_removed,
            rep.reports_files_removed,
            rep.bytes_freed,
        )
    return rep


def data_disk_usage(data_dir: Path) -> dict[str, Any]:
    """Return a breakdown of disk usage under ``data_dir`` for the admin UI."""
    out: dict[str, Any] = {"total_bytes": 0, "categories": {}}
    if not data_dir.exists():
        return out
    for child in sorted(data_dir.iterdir()):
        if not child.is_dir():
            continue
        size = _dir_size(child)
        out["total_bytes"] += size
        out["categories"][child.name] = {
            "bytes": size,
            "path": str(child),
        }
    return out
