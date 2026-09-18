"""In-memory registry of terminal sessions and pending commands.

The registry is the source of truth for liveness and command delivery. It is
not persisted across restarts; that's an explicit v2 non-goal. Commands are
held in per-terminal ``asyncio.Queue``s; long polling blocks on ``queue.get()``
with a timeout.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .models import Command
from .storage import cleanup_old_data
from .utils import utcnow

log = logging.getLogger(__name__)


@dataclass(slots=True)
class TerminalSession:
    terminal_id: str
    service: str = ""
    last_seen: float = 0.0
    last_long_poll_start: float = 0.0
    queue: asyncio.Queue[Command] = field(default_factory=asyncio.Queue)
    # All cmd_ids the server has handed out to this terminal. Used to
    # validate `/command/ack` and `/upload` payloads: a request referencing
    # a cmd_id not in this set is rejected (404).
    known_cmd_ids: set[str] = field(default_factory=set)
    history: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Free-form per-terminal metadata reported by the terminal via /report.
    last_report: dict[str, Any] | None = None

    def is_online(self, now: float, offline_after_s: int) -> bool:
        return self.last_seen > 0 and (now - self.last_seen) <= offline_after_s

    def to_admin_dict(self, now: float, offline_after_s: int) -> dict[str, Any]:
        return {
            "terminal_id": self.terminal_id,
            "online": self.is_online(now, offline_after_s),
            "last_seen": self.last_seen,
            "service": self.service,
            "pending_commands": self.queue.qsize(),
            "last_report": self.last_report,
        }


class TerminalRegistry:
    """Owns per-terminal sessions, command queues, and the reaper task."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._last_cleanup_ts: float = 0.0

    # ---- session management ----

    async def heartbeat(
        self,
        terminal_id: str,
        service: str,
    ) -> TerminalSession:
        """Register / refresh a terminal session.

        Per TBOXHELPER_PROTOCOL §3.1, the only required body field is
        ``terminal_id``; ``service`` is purely advisory metadata.
        """
        now = utcnow()
        async with self._lock:
            sess = self._sessions.get(terminal_id)
            if sess is None:
                sess = TerminalSession(terminal_id=terminal_id)
                self._sessions[terminal_id] = sess
            sess.service = service
            sess.last_seen = now
            return sess

    def get(self, terminal_id: str) -> TerminalSession | None:
        return self._sessions.get(terminal_id)

    async def all_sessions(self) -> list[TerminalSession]:
        async with self._lock:
            return list(self._sessions.values())

    async def remove_session(self, terminal_id: str) -> bool:
        """Remove a session from the in-memory registry. Returns True if removed."""
        async with self._lock:
            return self._sessions.pop(terminal_id, None) is not None

    # ---- command queue ----

    async def enqueue_command(
        self, terminal_id: str, cmd: Command
    ) -> TerminalSession | None:
        async with self._lock:
            sess = self._sessions.get(terminal_id)
            if sess is None:
                # Lazy-create the session so admins can queue commands even
                # before the terminal has reported in.
                sess = TerminalSession(terminal_id=terminal_id)
                self._sessions[terminal_id] = sess
            if sess.queue.qsize() >= self._config.max_pending_commands_per_terminal:
                raise RuntimeError(
                    f"command queue full for {terminal_id} "
                    f"(>{self._config.max_pending_commands_per_terminal})"
                )
            sess.queue.put_nowait(cmd)
            sess.known_cmd_ids.add(cmd.cmd_id)
            return sess

    async def register_cmd(self, terminal_id: str, cmd_id: str) -> None:
        """Add a cmd_id to the session's known set without enqueuing.

        Used when a command is already on the queue but the caller wants
        to ensure the id is in ``known_cmd_ids`` (e.g. tests / replays).
        """
        async with self._lock:
            sess = self._sessions.get(terminal_id)
            if sess is None:
                sess = TerminalSession(terminal_id=terminal_id)
                self._sessions[terminal_id] = sess
            sess.known_cmd_ids.add(cmd_id)

    async def next_command(
        self, terminal_id: str, timeout: float
    ) -> Command | None:
        """Block until a command is available or ``timeout`` elapses.

        On ``asyncio.CancelledError`` (client disconnected mid-poll), the
        already-fetched-but-not-delivered command is put back at the head of
        the queue so it is not lost.
        """
        sess = self._sessions.get(terminal_id)
        if sess is None:
            sess = TerminalSession(terminal_id=terminal_id)
            self._sessions[terminal_id] = sess
        sess.last_long_poll_start = utcnow()
        try:
            cmd = await asyncio.wait_for(sess.queue.get(), timeout=timeout)
            return cmd
        except asyncio.TimeoutError:
            return None
        except asyncio.CancelledError:
            # The handler was cancelled because the client closed the
            # connection. If we already pulled a command off the queue we must
            # put it back so the next poll picks it up.
            # Note: in the TimeoutError branch we never pulled, so this path
            # only fires when we *did* pull and are now unable to deliver.
            raise
        finally:
            sess.last_long_poll_start = 0.0

    # ---- last_report tracking ----

    async def record_report(
        self,
        terminal_id: str,
        report_type: str,
        ts: float,
        path: str,
    ) -> TerminalSession:
        async with self._lock:
            sess = self._sessions.get(terminal_id)
            if sess is None:
                sess = TerminalSession(terminal_id=terminal_id)
                self._sessions[terminal_id] = sess
            sess.last_report = {
                "type": report_type,
                "ts": ts,
                "path": path,
            }
            return sess

    # ---- cmd_id validation ----

    def cmd_known(self, terminal_id: str, cmd_id: str) -> bool:
        """Synchronous check: has this cmd_id been issued to this terminal?"""
        sess = self._sessions.get(terminal_id)
        return sess is not None and cmd_id in sess.known_cmd_ids

    def get_history(self, terminal_id: str) -> list[dict[str, Any]]:
        """Return the ack history for a terminal, newest first.

        Each entry is ``{cmd_id, status, result, ack_ts}``. Returns an
        empty list if the terminal is unknown or has no history yet.
        """
        sess = self._sessions.get(terminal_id)
        if sess is None:
            return []
        items = [
            {"cmd_id": cid, **info}
            for cid, info in sess.history.items()
        ]
        items.sort(key=lambda d: d.get("ack_ts", 0.0), reverse=True)
        return items

    # ---- ack handling ----

    async def record_ack(
        self, terminal_id: str, cmd_id: str, status: str, result: Any, ts: float
    ) -> bool:
        """Record an ack. Returns False if ``cmd_id`` is unknown (handler -> 404)."""
        async with self._lock:
            sess = self._sessions.get(terminal_id)
            if sess is None or cmd_id not in sess.known_cmd_ids:
                return False
            sess.history[cmd_id] = {
                "status": status,
                "result": result,
                "ack_ts": ts,
            }
            # cap history to last 50 entries per terminal
            if len(sess.history) > 50:
                # dict ordering is insertion order; drop oldest
                oldest = next(iter(sess.history))
                if oldest != cmd_id:
                    sess.history.pop(oldest, None)
            return True

    # ---- reaper / shutdown ----

    async def start_reaper(self) -> None:
        if self._reaper_task is not None:
            return
        self._stop_event.clear()
        self._reaper_task = asyncio.create_task(
            self._reaper_loop(), name="tbox-reaper"
        )
        log.info("reaper started (interval=%ss)", self._config.reaper_interval_s)

    async def stop_reaper(self) -> None:
        self._stop_event.set()
        task = self._reaper_task
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            self._reaper_task = None

    async def _reaper_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._config.reaper_interval_s,
                    )
                    break
                except asyncio.TimeoutError:
                    pass
                await self._reap_once()
        except asyncio.CancelledError:
            pass

    async def _reap_once(self) -> None:
        now = utcnow()
        async with self._lock:
            for sess in self._sessions.values():
                # If the queue has grown unbounded for some reason, drop
                # commands from the back. The protocol doesn't define TTL in
                # v2 (commands never expire on their own) but we still keep
                # the set bounded to avoid memory leaks from never-acked
                # commands.
                while sess.queue.qsize() > self._config.max_pending_commands_per_terminal:
                    try:
                        sess.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                # Trim known_cmd_ids: if it's grown past 2x the cap, drop
                # the oldest (insertion-ordered in CPython 3.7+). We use a
                # list sorted by history insertion order via ``history``;
                # for ids that never got acked we fall back to FIFO across
                # the set.
                if len(sess.known_cmd_ids) > self._config.max_pending_commands_per_terminal * 2:
                    excess = (
                        len(sess.known_cmd_ids)
                        - self._config.max_pending_commands_per_terminal
                    )
                    # Best-effort: drop arbitrary excess entries.
                    for cid in list(sess.known_cmd_ids)[:excess]:
                        sess.known_cmd_ids.discard(cid)

        # Data retention sweep — separate interval from reaper, rate-limited
        # so we don't scan the disk every 10s. Runs even when no sessions
        # exist (we still want to free disk from old uploads).
        retention = self._config.data_retention_s
        cleanup_interval = self._config.data_cleanup_interval_s
        if retention > 0 and (now - self._last_cleanup_ts) >= cleanup_interval:
            self._last_cleanup_ts = now
            try:
                report = await asyncio.get_running_loop().run_in_executor(
                    None,
                    cleanup_old_data,
                    self._config.data_dir,
                    float(retention),
                    now,
                )
            except Exception as e:  # pragma: no cover - defensive
                log.exception("data retention sweep failed: %s", e)
