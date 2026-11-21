"""Single-worker, snapshot-based background indexing for Codex histories."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from railmux.codex_index import (
    CodexIndex,
    ScanReport,
    _lineage_members,
    _path_key,
    persistent_cache_path,
)
from railmux.models import SessionMeta
from railmux.renames import Renames


_MAX_WARNING = 180


@dataclass(frozen=True)
class _SessionView:
    by_id: Mapping[str, SessionMeta]
    lineages: Mapping[str, tuple[SessionMeta, ...]]
    projects: Mapping[Path, tuple[SessionMeta, ...]]
    resolved_paths: Mapping[Path, Path]

    @classmethod
    def build(
        cls,
        sessions: tuple[SessionMeta, ...],
        resolved_paths: Mapping[Path, Path] | None = None,
    ) -> _SessionView:
        if resolved_paths is None:
            resolved_paths = MappingProxyType({
                path: _path_key(path)
                for path in {meta.project.real_path for meta in sessions}
            })
        lineages = _lineage_members(sessions, path_key=resolved_paths.__getitem__)
        projects: dict[Path, list[SessionMeta]] = {}
        by_id = {meta.session_id: meta for meta in sessions}
        for session_id, members in lineages.items():
            newest = members[0]
            if session_id == newest.session_id:
                cwd = resolved_paths[newest.project.real_path]
                projects.setdefault(cwd, []).append(newest)
        return cls(
            MappingProxyType(by_id),
            MappingProxyType(lineages),
            MappingProxyType({
                cwd: tuple(sorted(
                    records,
                    key=lambda meta: (meta.last_mtime, str(meta.jsonl_path), meta.session_id),
                    reverse=True,
                ))
                for cwd, records in projects.items()
            }),
            resolved_paths,
        )


@dataclass(frozen=True)
class IndexSnapshot:
    """One atomically published, immutable index generation."""

    generation: int
    sessions: tuple[SessionMeta, ...]
    published_at: float
    report: ScanReport | None
    hidden_statuses: Mapping[str, str]
    view: _SessionView


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
        self._scanner = scanner or CodexIndex(
            codex_home,
            cache_path=persistent_cache_path(codex_home),
        )
        self._min_interval_s = max(0.0, min_interval_s)
        self._force_interval_s = max(
            0.0, min(self._min_interval_s, force_interval_s))
        self._clock = clock
        self._condition = threading.Condition()
        self._reader = threading.local()
        self._snapshot = IndexSnapshot(
            0, (), 0.0, None, MappingProxyType({}), _SessionView.build(()))
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
        pinned = getattr(self._reader, "view", None)
        if pinned is not None:
            return pinned[0]
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

    def invalidate(
        self,
        *,
        tombstone: str | None = None,
        tombstones: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        """Invalidate worker state without clearing the last-good snapshot."""
        with self._condition:
            if tombstone:
                self._tombstones.add(tombstone)
            self._tombstones.update(tombstones)
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
        return {cwd: len(rows) for cwd, rows in self._visible_view().projects.items()}

    def sessions_for_cwd(
        self, cwd: Path, *, refresh: bool = True,
    ) -> list[SessionMeta]:
        if refresh:
            self.refresh()
        view = self._visible_view()
        target = view.resolved_paths.get(cwd, cwd)
        if target not in view.projects:
            target = _path_key(cwd)
        return [
            self._with_override(meta, view.lineages[meta.session_id])
            for meta in view.projects.get(target, ())
        ]

    def listed_sessions(self) -> tuple[SessionMeta, ...]:
        """One current display row per visible lineage, across all projects."""
        view = self._visible_view()
        return tuple(
            self._with_override(meta, view.lineages[meta.session_id])
            for rows in view.projects.values()
            for meta in rows
        )

    def lineage_ids(
        self, session_id: str, *, refresh: bool = True,
    ) -> frozenset[str]:
        """UUID aliases belonging to *session_id*'s logical conversation."""
        if refresh:
            self.refresh()
        members = self._visible_view().lineages.get(session_id, ())
        return frozenset(meta.session_id for meta in members)

    def representative_for(
        self, session_id: str, *, refresh: bool = True,
    ) -> SessionMeta | None:
        """Newest UI representative for the lineage containing *session_id*."""
        if refresh:
            self.refresh()
        members = self._visible_view().lineages.get(session_id)
        if not members:
            return None
        return self._with_override(members[0], members)

    def get(self, session_id: str, *, refresh: bool = True) -> SessionMeta | None:
        if refresh:
            self.refresh()
        meta = self._visible_view().by_id.get(session_id)
        return self._with_override(meta) if meta is not None else None

    def hidden_status(
        self, session_id: str, *, refresh: bool = True,
    ) -> str | None:
        """Status for a sidebar-filtered rollout in the current generation."""
        if refresh:
            self.refresh()
        return self.current_snapshot().hidden_statuses.get(session_id)

    def close(self, timeout_s: float = 0.2) -> bool:
        """Stop accepting work and wait only a bounded time for NFS IO."""
        with self._condition:
            self._closed = True
            self._requested = False
            self._condition.notify_all()
        self._thread.join(max(0.0, timeout_s))
        return not self._thread.is_alive()

    def _visible_view(self) -> _SessionView:
        pinned = getattr(self._reader, "view", None)
        if pinned is None:
            with self._condition:
                snapshot = self._snapshot
                tombstones = frozenset(self._tombstones)
        else:
            snapshot, tombstones = pinned
        if not tombstones:
            return snapshot.view
        cached = getattr(self._reader, "tombstone_view", None)
        if cached is not None and cached[:2] == (snapshot.generation, tombstones):
            return cached[2]
        view = _SessionView.build(
            tuple(meta for meta in snapshot.sessions if meta.session_id not in tombstones),
            snapshot.view.resolved_paths,
        )
        self._reader.tombstone_view = (snapshot.generation, tombstones, view)
        return view

    def _with_override(
        self,
        meta: SessionMeta,
        lineage: tuple[SessionMeta, ...] | None = None,
    ) -> SessionMeta:
        if self._renames is None:
            return meta
        for member in lineage or (meta,):
            title = self._renames.get(member.session_id)
            if title:
                return replace(meta, title=title)
        return meta

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
                view = _SessionView.build(sessions)
                hidden_statuses = getattr(
                    self._scanner, "hidden_statuses", lambda: {})()
                error: str | None = None
            except Exception:
                report = None
                sessions = ()
                hidden_statuses = {}
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
                        hidden_statuses=MappingProxyType(
                            dict(hidden_statuses)),
                        view=view,
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
