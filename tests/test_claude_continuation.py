"""Claude ``continued-in`` session files: listing, renames and live re-keying."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from railmux.session_cache import SessionCache
from railmux.session_index import _scan_session
from railmux.ui.app import App, _Running

from tests.app_test_harness import (
    _project,
    isolate_tmux_identity_stamps as isolate_tmux_identity_stamps,
)


pytestmark = pytest.mark.usefixtures("isolate_tmux_identity_stamps")

OLD = "02b59000-6bb4-4ec5-bdca-24f7a96b0467"
MID = "47fca075-9cb8-44fb-a314-d57ef2256ad9"
NEW = "96fd3642-6c17-4c9b-8129-08d3d9db3e8a"


def _turn() -> list[dict]:
    return [
        {"type": "user", "message": {"role": "user", "content": "hello"}},
        {"type": "assistant", "message": {
            "role": "assistant", "id": "m1", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "hi"}],
        }},
    ]


def _continued(successor: str) -> dict:
    return {
        "type": "continued-in",
        "sessionId": "previous",
        "continuedInSessionId": successor,
    }


def _write(directory: Path, session_id: str, records: list[dict]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


class _MemoryRenames:
    def __init__(self, titles: dict[str, str] | None = None) -> None:
        self.titles = dict(titles or {})

    def get(self, session_id: str) -> str | None:
        return self.titles.get(session_id)

    def set(self, session_id: str, title: str) -> None:
        self.titles[session_id] = title


def test_scan_records_only_a_final_continued_in(tmp_path):
    project = _project(claude_dir=tmp_path)
    final = _write(tmp_path, OLD, _turn() + [_continued(NEW)])
    resumed = _write(tmp_path, MID, _turn() + [_continued(NEW)] + _turn())
    invalid = _write(tmp_path, NEW, _turn() + [_continued("../escape")])

    assert _scan_session(project, final).continued_in_id == NEW
    assert _scan_session(project, resumed).continued_in_id is None
    assert _scan_session(project, invalid).continued_in_id is None


def test_listing_hides_continued_sessions_and_carries_rename(tmp_path):
    project = _project(claude_dir=tmp_path)
    _write(tmp_path, OLD, _turn() + [_continued(MID)])
    _write(tmp_path, MID, _turn() + [_continued(NEW)])
    _write(tmp_path, NEW, _turn())
    renames = _MemoryRenames({OLD: "Rubin bringup"})
    cache = SessionCache(renames)

    sessions = cache.list_sessions(project)

    assert [session.session_id for session in sessions] == [NEW]
    assert sessions[0].title == "Rubin bringup"
    assert renames.titles[NEW] == "Rubin bringup"
    assert cache.continuations == {OLD: MID, MID: NEW}


def test_listing_keeps_session_whose_successor_is_missing(tmp_path):
    project = _project(claude_dir=tmp_path)
    _write(tmp_path, OLD, _turn() + [_continued(NEW)])

    sessions = SessionCache().list_sessions(project)

    assert [session.session_id for session in sessions] == [OLD]


def _app(project, running: dict[str, _Running]) -> App:
    app = App.__new__(App)
    app._running = running
    app._session_cache = SessionCache()
    app._session_cache.list_sessions(project)
    app._favorites = MagicMock()
    app._favorites.get_ids.return_value = {OLD}
    app._stamp_running = MagicMock(return_value=True)
    app._ensure_running_session_lease = MagicMock(return_value=True)
    app._record_session_lease_result = MagicMock()
    app._set_slot_active_target = MagicMock()
    return app


def test_running_claude_entry_follows_continuation_chain(tmp_path):
    project = _project(claude_dir=tmp_path)
    _write(tmp_path, OLD, _turn() + [_continued(MID)])
    _write(tmp_path, MID, _turn() + [_continued(NEW)])
    _write(tmp_path, NEW, _turn())
    running = _Running(
        key=OLD, tmux_name="cc-new---b14c77-2", label="x", project=project)
    app = _app(project, {OLD: running})
    app._agent_workspace().target.agent_tmux_name = running.tmux_name

    app._follow_claude_continuations()

    assert app._running == {NEW: running}
    assert running.key == NEW and running.tmux_name == "cc-new---b14c77-2"
    app._stamp_running.assert_called_once_with(running)
    lease_session = app._ensure_running_session_lease.call_args.args[1]
    assert lease_session.session_id == NEW
    app._set_slot_active_target.assert_called_once()
    assert app._set_slot_active_target.call_args.args[1:] == (
        NEW, running.tmux_name)
    app._favorites.set_many.assert_called_once_with({NEW}, True)


def test_running_entry_stays_when_successor_is_already_running(tmp_path):
    project = _project(claude_dir=tmp_path)
    _write(tmp_path, OLD, _turn() + [_continued(NEW)])
    _write(tmp_path, NEW, _turn())
    old = _Running(key=OLD, tmux_name="cc-a", label="a", project=project)
    new = _Running(key=NEW, tmux_name="cc-b", label="b", project=project)
    app = _app(project, {OLD: old, NEW: new})

    app._follow_claude_continuations()

    assert app._running == {OLD: old, NEW: new}
    app._stamp_running.assert_not_called()


def _background(records: list[dict]) -> list[dict]:
    return [{**record, "sessionKind": "bg"} for record in records]


def test_background_successor_of_visible_session_stays_listed(tmp_path):
    project = _project(claude_dir=tmp_path)
    _write(tmp_path, OLD, _turn() + [_continued(NEW)])
    _write(tmp_path, NEW, _background(_turn()))
    _write(tmp_path, MID, _background(_turn()))

    sessions = SessionCache().list_sessions(project)

    assert [session.session_id for session in sessions] == [NEW]
    assert sessions[0].background
    assert _scan_session(project, tmp_path / f"{NEW}.jsonl") is None


def test_running_entry_follows_background_successor(tmp_path):
    project = _project(claude_dir=tmp_path)
    _write(tmp_path, OLD, _turn() + [_continued(NEW)])
    _write(tmp_path, NEW, _background(_turn()))
    running = _Running(key=OLD, tmux_name="cc-a", label="a", project=project)
    app = _app(project, {OLD: running})

    app._follow_claude_continuations()

    assert app._running == {NEW: running}
