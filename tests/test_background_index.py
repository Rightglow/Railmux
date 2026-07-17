"""Deterministic concurrency/failure tests for the background Codex index."""
from __future__ import annotations

import threading
from pathlib import Path

from railmux.background_index import BackgroundCodexIndex
from railmux.codex_index import ScanReport
from railmux.models import Project, SessionMeta


def _meta(session_id: str, mtime: float = 1.0) -> SessionMeta:
    project = Project(Path("/project"), "-project", Path(), 0, 0.0)
    return SessionMeta(
        project=project,
        session_id=session_id,
        jsonl_path=Path(f"/rollout-{session_id}.jsonl"),
        title="title",
        message_count=2,
        token_total=3,
        last_mtime=mtime,
        session_type="codex",
    )


def _report(*, complete: bool = True, warning: str | None = None,
            errors: int = 0) -> ScanReport:
    return ScanReport(complete, warning, 1, 1, 1, errors, 0.01)


class _EventScanner:
    def __init__(self, sessions=()) -> None:
        self.sessions = tuple(sessions)
        self.report = _report()
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.block = False
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.invalidations = 0
        self.error: Exception | None = None

    def refresh(self) -> ScanReport:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        if self.block:
            assert self.release.wait(2.0)
        try:
            if self.error is not None:
                raise self.error
            return self.report
        finally:
            self.active -= 1
            self.finished.set()

    def snapshot(self):
        return self.sessions

    def invalidate(self) -> None:
        self.invalidations += 1


class _ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def test_publishes_immutable_monotonic_generations() -> None:
    scanner = _EventScanner([_meta("one")])
    index = BackgroundCodexIndex(Path("/unused"), scanner=scanner,
                                 min_interval_s=0)
    try:
        index.refresh()
        assert index.wait_for_generation(1)
        first = index.current_snapshot()
        scanner.sessions = (_meta("two", 2.0),)
        index.refresh()
        assert index.wait_for_generation(2)
        second = index.current_snapshot()
        assert first.generation == 1
        assert [meta.session_id for meta in first.sessions] == ["one"]
        assert second.generation == 2
        assert [meta.session_id for meta in second.sessions] == ["two"]
    finally:
        index.close()


def test_compound_read_stays_on_one_generation() -> None:
    scanner = _EventScanner([_meta("one")])
    index = BackgroundCodexIndex(Path("/unused"), scanner=scanner,
                                 min_interval_s=0)
    try:
        index.refresh()
        assert index.wait_for_generation(1)
        assert index.begin_read() == 1
        scanner.sessions = (_meta("two", 2.0),)
        index.refresh()
        assert index.wait_for_generation(2)
        assert index.current_snapshot().generation == 1
        assert index.get("one", refresh=False) is not None
        assert index.get("two", refresh=False) is None
        index.end_read()
        assert index.get("one", refresh=False) is None
        assert index.get("two", refresh=False) is not None
    finally:
        index.end_read()
        index.close()


def test_requests_coalesce_without_parallel_scans() -> None:
    scanner = _EventScanner([_meta("one")])
    scanner.block = True
    index = BackgroundCodexIndex(Path("/unused"), scanner=scanner,
                                 min_interval_s=0)
    try:
        index.refresh()
        assert scanner.started.wait(2.0)
        for _ in range(20):
            index.refresh()
        scanner.block = False
        scanner.release.set()
        assert index.wait_for_generation(2)
        assert scanner.calls == 2
        assert scanner.max_active == 1
    finally:
        index.close()


def test_prelaunch_wait_requires_scan_started_after_request() -> None:
    scanner = _EventScanner([_meta("one")])
    scanner.block = True
    index = BackgroundCodexIndex(Path("/unused"), scanner=scanner,
                                 min_interval_s=0)
    result = []
    waiter_done = threading.Event()
    try:
        index.refresh()
        assert scanner.started.wait(2.0)

        def wait_for_fresh_fence() -> None:
            result.append(index.refresh_and_wait(2.0))
            waiter_done.set()

        waiter = threading.Thread(target=wait_for_fresh_fence)
        waiter.start()
        with index._condition:
            assert index._condition.wait_for(
                lambda: index._request_serial >= 2, timeout=2.0)
        scanner.release.set()
        assert waiter_done.wait(2.0)
        waiter.join()
        assert result == [True]
        assert scanner.calls == 2
    finally:
        scanner.release.set()
        index.close()


def test_force_request_shortens_rate_limit() -> None:
    scanner = _EventScanner([_meta("one")])
    index = BackgroundCodexIndex(Path("/unused"), scanner=scanner,
                                 min_interval_s=3600, force_interval_s=0)
    try:
        index.refresh(force=True)
        assert index.wait_for_generation(1)
        index.refresh(force=True)
        assert index.wait_for_generation(2)
        assert scanner.calls == 2
    finally:
        index.close()


def test_ordinary_requests_respect_rate_limit_without_sleep() -> None:
    scanner = _EventScanner([_meta("one")])
    clock = _ManualClock()
    index = BackgroundCodexIndex(
        Path("/unused"), scanner=scanner, min_interval_s=10, clock=clock)
    try:
        index.refresh(force=True)
        assert index.wait_for_generation(1)
        index.refresh()
        with index._condition:
            assert index._condition.wait_for(
                lambda: index._rate_waiting, timeout=2.0)
        assert scanner.calls == 1
        clock.value = 10.0
        index.refresh()
        assert index.wait_for_generation(2)
        assert scanner.calls == 2
    finally:
        index.close()


def test_snapshot_query_does_not_wait_for_active_scan() -> None:
    scanner = _EventScanner([_meta("one")])
    scanner.block = True
    index = BackgroundCodexIndex(Path("/unused"), scanner=scanner,
                                 min_interval_s=0)
    index.refresh()
    assert scanner.started.wait(2.0)
    try:
        assert index.all_cwds(refresh=False) == {}
        assert index.generation == 0
    finally:
        scanner.release.set()
        index.close()


def test_failed_scan_keeps_last_known_good_and_bounds_warning() -> None:
    scanner = _EventScanner([_meta("one")])
    index = BackgroundCodexIndex(Path("/unused"), scanner=scanner,
                                 min_interval_s=0)
    try:
        index.refresh()
        assert index.wait_for_generation(1)
        scanner.report = _report(complete=False, warning="x" * 1000, errors=1)
        scanner.sessions = ()
        scanner.finished.clear()
        index.refresh()
        assert scanner.finished.wait(2.0)
        assert index.generation == 1
        assert index.get("one", refresh=False) is not None
        warning = index.take_warning()
        assert warning is not None and len(warning) <= 180
        assert index.take_warning() is None
    finally:
        index.close()


def test_initial_failure_exits_cold_loading_state() -> None:
    scanner = _EventScanner()
    scanner.report = _report(complete=False, warning="unavailable", errors=1)
    index = BackgroundCodexIndex(Path("/unused"), scanner=scanner,
                                 min_interval_s=0)
    try:
        scanner.finished.clear()
        index.refresh()
        assert scanner.finished.wait(2.0)
        assert index.has_snapshot is False
        assert index.is_unavailable is True
        scanner.report = _report()
        index.refresh()
        assert index.wait_for_generation(1)
        assert index.is_unavailable is False
    finally:
        index.close()


def test_delete_tombstone_hides_stale_generation_until_refresh() -> None:
    scanner = _EventScanner([_meta("one")])
    scanner.block = True
    scanner.release.set()
    index = BackgroundCodexIndex(Path("/unused"), scanner=scanner,
                                 min_interval_s=0)
    try:
        index.refresh()
        assert index.wait_for_generation(1)
        scanner.release.clear()
        scanner.started.clear()
        index.invalidate(tombstone="one")
        assert scanner.started.wait(2.0)
        assert index.get("one", refresh=False) is None
        scanner.sessions = ()
        scanner.release.set()
        assert index.wait_for_generation(2)
        assert scanner.invalidations == 1
    finally:
        index.close()


def test_shutdown_is_bounded_and_cannot_publish_after_close() -> None:
    scanner = _EventScanner([_meta("one")])
    scanner.block = True
    index = BackgroundCodexIndex(Path("/unused"), scanner=scanner,
                                 min_interval_s=0)
    index.refresh()
    assert scanner.started.wait(2.0)
    assert index.close(timeout_s=0) is False
    scanner.release.set()
    assert scanner.finished.wait(2.0)
    assert index.generation == 0


def test_snapshot_queries_preserve_all_session_fields() -> None:
    original = _meta("one")
    scanner = _EventScanner([original])
    index = BackgroundCodexIndex(Path("/unused"), scanner=scanner,
                                 min_interval_s=0)
    try:
        index.refresh()
        assert index.wait_for_generation(1)
        assert index.get("one", refresh=False) == original
    finally:
        index.close()
