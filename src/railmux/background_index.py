"""Single-worker, snapshot-based background indexing for Codex histories."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from railmux.codex_index import CodexIndex, ScanReport
from railmux.models import SessionMeta
from railmux.renames import Renames


_MAX_WARNING = 180


@dataclass(frozen=True)
class IndexSnapshot:
    """One atomically published, immutable index generation."""

    generation: int
    sessions: tuple[SessionMeta, ...]
    published_at: float
    report: ScanReport | None


class BackgroundCodexIndex:
    """UI-facing Codex index backed by exactly one rate-limited worker.

    The worker exclusively owns the mutable ``CodexIndex``.  UI queries only
    inspect the current immutable generation, so a slow tree walk or rollout
    parse can never block an urwid tick.
    """

    def __init__(
        self,
        codex_home: Path,
        renames: Renames | None = None,
        *,
        min_interval_s: float = 1.0,
        force_interval_s: float = 0.25,
        scanner: CodexIndex | None = None,
        clock=time.monotonic,
    ) -> None:
        self._renames = renames
        self._scanner = scanner or CodexIndex(codex_home)
        self._min_interval_s = max(0.0, min_interval_s)
        self._force_interval_s = max(
            0.0, min(self._min_interval_s, force_interval_s))
        self._clock = clock
        self._condition = threading.Condition()
        self._reader = threading.local()
        self._snapshot = IndexSnapshot(0, (), 0.0, None)
        self._requested = False
        self._force_requested = False
        self._invalidate_requested = False
        self._scanning = False
        self._rate_waiting = False
        self._closed = False
        self._last_started = float("-inf")
        self._warning: str | None = None
        self._initial_failure = False
        self._warning_serial = 0
        self._seen_warning_serial = 0
        self._request_serial = 0
        self._completed_request_serial = 0
        self._tombstones: set[str] = set()
        self._thread = threading.Thread(
            target=self._run,
            name="railmux-codex-index",
            daemon=True,
        )
        self._thread.start()

    @property
    def generation(self) -> int:
        with self._condition:
            return self._snapshot.generation

    @property
    def is_pending(self) -> bool:
        with self._condition:
            return self._scanning or self._requested

    @property
    def has_snapshot(self) -> bool:
        return self.generation > 0

    @property
    def is_unavailable(self) -> bool:
        """Whether the initial source scan failed instead of yielding data."""
        with self._condition:
            return self._initial_failure and self._snapshot.generation == 0

    def current_snapshot(self) -> IndexSnapshot:
        with self._condition:
            return self._snapshot

    def begin_read(self) -> int:
        """Pin one immutable generation for a compound UI refresh."""
        with self._condition:
            snapshot = self._snapshot
            tombstones = frozenset(self._tombstones)
        self._reader.view = (snapshot, tombstones)
        return snapshot.generation

    def end_read(self) -> None:
        """Release the current thread's pinned generation, if any."""
        if hasattr(self._reader, "view"):
            del self._reader.view

    def refresh(self, *, force: bool = False) -> int:
        """Request a scan and return immediately; repeated requests coalesce."""
        with self._condition:
            if self._closed:
                return self._request_serial
            self._request_serial += 1
            self._requested = True
            self._force_requested = self._force_requested or force
            self._condition.notify()
            return self._request_serial

    def refresh_and_wait(self, timeout_s: float) -> bool:
        """Request a forced generation and wait at most *timeout_s*.

        This bounded path is reserved for pre-launch identity fencing, not UI
        ticks.  ``False`` means callers must disable heuristic adoption.
        """
        requested = self.refresh(force=True)
        deadline = self._clock() + max(0.0, timeout_s)
        with self._condition:
            while (not self._closed
                   and self._completed_request_serial < requested):
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return self._completed_request_serial >= requested

    def wait_for_generation(self, generation: int, timeout_s: float = 2.0) -> bool:
        """Wait for deterministic tests/integrations without polling sleeps."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._condition:
            while not self._closed and self._snapshot.generation < generation:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return self._snapshot.generation >= generation

    def invalidate(self, *, tombstone: str | None = None) -> None:
        """Invalidate worker state without clearing the last-good snapshot."""
        with self._condition:
            if tombstone:
                self._tombstones.add(tombstone)
            if self._closed:
                return
            self._invalidate_requested = True
            self._request_serial += 1
            self._requested = True
            self._force_requested = True
            self._condition.notify()

    def take_warning(self) -> str | None:
        """Return each bounded worker warning at most once to the UI."""
        with self._condition:
            if self._seen_warning_serial == self._warning_serial:
                return None
            self._seen_warning_serial = self._warning_serial
            return self._warning

    def all_cwds(self, *, refresh: bool = True) -> dict[Path, int]:
        if refresh:
            self.refresh()
        counts: dict[Path, int] = {}
        for meta in self._visible_sessions():
            cwd = meta.project.real_path
            counts[cwd] = counts.get(cwd, 0) + 1
        return counts

    def sessions_for_cwd(
        self, cwd: Path, *, refresh: bool = True,
    ) -> list[SessionMeta]:
        if refresh:
            self.refresh()
        target = self._path_key(cwd)
        results = [
            self._with_override(meta)
            for meta in self._visible_sessions()
            if self._path_key(meta.project.real_path) == target
        ]
        results.sort(key=lambda meta: meta.last_mtime, reverse=True)
        return results

    def get(self, session_id: str, *, refresh: bool = True) -> SessionMeta | None:
        if refresh:
            self.refresh()
        for meta in self._visible_sessions():
            if meta.session_id == session_id:
                return self._with_override(meta)
        return None

    def close(self, timeout_s: float = 0.2) -> bool:
        """Stop accepting work and wait only a bounded time for NFS IO."""
        with self._condition:
            self._closed = True
            self._requested = False
            self._condition.notify_all()
        self._thread.join(max(0.0, timeout_s))
        return not self._thread.is_alive()

    def _visible_sessions(self) -> tuple[SessionMeta, ...]:
        pinned = getattr(self._reader, "view", None)
        if pinned is None:
            with self._condition:
                snapshot = self._snapshot
                tombstones = frozenset(self._tombstones)
        else:
            snapshot, tombstones = pinned
        if not tombstones:
            return snapshot.sessions
        return tuple(
            meta for meta in snapshot.sessions
            if meta.session_id not in tombstones
        )

    def _with_override(self, meta: SessionMeta) -> SessionMeta:
        if self._renames is None:
            return meta
        title = self._renames.get(meta.session_id)
        return replace(meta, title=title) if title else meta

    @staticmethod
    def _path_key(path: Path) -> Path:
        try:
            return path.resolve()
        except OSError:
            return path

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._closed and not self._requested:
                    self._condition.wait()
                if self._closed:
                    return
                force = self._force_requested
                interval = (
                    self._force_interval_s if force else self._min_interval_s)
                wait_s = max(
                    0.0,
                    self._last_started + interval - self._clock(),
                )
                if wait_s > 0:
                    self._rate_waiting = True
                    self._condition.notify_all()
                    self._condition.wait(wait_s)
                    continue
                invalidate = self._invalidate_requested
                request_serial = self._request_serial
                self._requested = False
                self._force_requested = False
                self._invalidate_requested = False
                self._scanning = True
                self._rate_waiting = False
                self._last_started = self._clock()

            try:
                if invalidate:
                    self._scanner.invalidate()
                report = self._scanner.refresh()
                sessions = self._scanner.snapshot()
                error: str | None = None
            except Exception:
                report = None
                sessions = ()
                error = "Codex background index failed unexpectedly"

            with self._condition:
                self._scanning = False
                if self._closed:
                    self._condition.notify_all()
                    return
                unusable_empty = bool(
                    report is not None
                    and report.transient_errors
                    and not sessions
                )
                if (error is not None or report is None
                        or not report.complete or unusable_empty):
                    if self._snapshot.generation == 0:
                        self._initial_failure = True
                    self._record_warning_locked(
                        error or (report.warning if report else None)
                        or "Codex session scan failed"
                    )
                else:
                    self._initial_failure = False
                    self._snapshot = IndexSnapshot(
                        generation=self._snapshot.generation + 1,
                        sessions=sessions,
                        published_at=self._clock(),
                        report=report,
                    )
                    live_ids = {meta.session_id for meta in sessions}
                    self._tombstones.intersection_update(live_ids)
                    if report.warning:
                        self._record_warning_locked(report.warning)
                    else:
                        self._warning = None
                    self._completed_request_serial = max(
                        self._completed_request_serial, request_serial)
                self._condition.notify_all()

    def _record_warning_locked(self, warning: str) -> None:
        text = " ".join(str(warning).split())
        if len(text) > _MAX_WARNING:
            text = text[: _MAX_WARNING - 1] + "…"
        if text == self._warning:
            return
        self._warning = text
        self._warning_serial += 1
