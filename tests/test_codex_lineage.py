"""App-level identity tests for Codex rewind/fork lineages."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from railmux import tmux_ctl, tmux_server
from railmux.favorites import Favorites
from railmux.models import Project, SessionMeta
from railmux.ui.app import App, _Running
from railmux.ui.workspace import AgentWorkspace


class _LineageIndex:
    def lineage_ids(self, session_id: str, *, refresh: bool = True):
        if session_id in {"root", "middle", "leaf"}:
            return frozenset({"root", "middle", "leaf"})
        return frozenset()


def _app_with_running_root() -> tuple[App, _Running]:
    app = App.__new__(App)
    project = Project(Path("/project"), "-project", Path(), 1, 0.0)
    running = _Running(
        key="root",
        tmux_name="cx-root",
        label="project/conversation",
        project=project,
        session_type="codex",
    )
    app._running = {"root": running}
    app._codex_index = _LineageIndex()
    return app, running


def test_codex_fork_uuid_resolves_to_existing_running_owner() -> None:
    app, running = _app_with_running_root()

    assert app._by_session_id("leaf") is running
    assert app._by_session_id("middle") is running
    assert app._by_session_id("unrelated") is None


def test_running_ids_include_every_codex_lineage_alias() -> None:
    app, _ = _app_with_running_root()

    assert app._running_session_ids() == {"root", "middle", "leaf"}


def test_live_writer_matches_any_rewind_alias() -> None:
    app, _ = _app_with_running_root()

    assert app._codex_writer_matches("root", {"leaf"})
    assert not app._codex_writer_matches("root", {"unrelated"})


def test_existing_root_favorite_marks_visible_leaf(
    tmp_path, monkeypatch,
) -> None:
    favorites_path = tmp_path / "favorites.json"
    monkeypatch.setattr(
        "railmux.favorites._favorites_path", lambda: favorites_path)
    app, _ = _app_with_running_root()
    app._favorites = Favorites()
    app._favorites.set_many({"root"}, True)

    assert app._favorite_ids_for_view() == {"root", "middle", "leaf"}


def test_live_rewind_resets_view_and_stamps_canonical_transcript(
    monkeypatch, tmp_path,
) -> None:
    root_id = "019fc605-5188-7212-bc48-ea023fe8b73c"
    leaf_id = "019fc7c1-a27c-7ae0-9937-7570552a112a"
    project = Project(tmp_path, "-project", Path(), 1, 0.0)
    running = _Running(
        key=root_id,
        tmux_name="cx-live",
        label="project/conversation",
        project=project,
        session_type="codex",
        codex_canonical_session_id=root_id,
    )
    meta = SessionMeta(
        project=project,
        session_id=leaf_id,
        jsonl_path=tmp_path / (
            f"rollout-2026-08-03T13-13-03-{leaf_id}.jsonl"),
        title="current",
        message_count=2,
        token_total=0,
        last_mtime=2.0,
        session_type="codex",
        forked_from_id=root_id,
    )
    identity = tmux_ctl.PaneIdentity(
        "%9", 123, "railmux", "$4", "@5", False, 80, 24)
    app = App.__new__(App)
    app._codex_lineage_ids = MagicMock(return_value={root_id, leaf_id})
    app._codex_rollout_ids = MagicMock(return_value={root_id})
    transport = MagicMock()
    transport.displayed_real_pane.return_value = "%9"
    app._display_transport = MagicMock(return_value=transport)
    monkeypatch.setattr(tmux_ctl, "pane_identity", lambda _pane: identity)
    reset = MagicMock(return_value=True)
    monkeypatch.setattr(tmux_ctl, "reset_pane_view", reset)
    stamped = MagicMock(return_value=True)
    monkeypatch.setattr(tmux_ctl, "set_pane_user_option", stamped)
    monkeypatch.setattr(tmux_ctl, "pane_user_option", lambda *_args: None)
    probes: dict[str, set[str] | None] = {}

    # An indexed child is not enough: on procfs the exact live writer must
    # first expose that rollout through its open file descriptors.
    app._sync_codex_rewind_scrollback(running, meta, None, probes)
    reset.assert_not_called()
    assert running.codex_canonical_session_id == root_id

    probes.clear()
    app._codex_rollout_ids.return_value = {root_id, leaf_id}
    app._sync_codex_rewind_scrollback(running, meta, None, probes)

    reset.assert_called_once_with(identity)
    assert running.codex_canonical_session_id == leaf_id
    assert stamped.call_count == 2
    transcript_call, generation_call = stamped.call_args_list
    assert transcript_call.args[:2] == (
        "%9", tmux_server.TRANSCRIPT_SOURCE_OPTION,
    )
    assert leaf_id in transcript_call.args[2]
    assert generation_call.args == (
        "%9",
        tmux_ctl.RAILMUX_HISTORY_GENERATION_OPTION,
        f"{tmux_ctl.RAILMUX_CANONICAL_HISTORY_PREFIX}{leaf_id}",
    )


def test_first_codex_generation_only_establishes_scrollback_baseline(
    monkeypatch, tmp_path,
) -> None:
    session_id = "019fc605-5188-7212-bc48-ea023fe8b73c"
    project = Project(tmp_path, "-project", Path(), 1, 0.0)
    running = _Running(
        key=session_id,
        tmux_name="cx-live",
        label="project/conversation",
        project=project,
        session_type="codex",
    )
    meta = SessionMeta(
        project=project,
        session_id=session_id,
        jsonl_path=tmp_path / (
            f"rollout-2026-08-03T13-09-08-{session_id}.jsonl"),
        title="root",
        message_count=1,
        token_total=0,
        last_mtime=1.0,
        session_type="codex",
    )
    app = App.__new__(App)
    app._stamp_codex_history_state = MagicMock()
    reset = MagicMock(return_value=True)
    monkeypatch.setattr(tmux_ctl, "reset_pane_view", reset)

    app._sync_codex_rewind_scrollback(running, meta, None, {})

    assert running.codex_canonical_session_id == session_id
    reset.assert_not_called()
    app._stamp_codex_history_state.assert_called_once_with(running, meta)


def test_codex_history_state_stamps_nested_wrapper_and_real_pane(
    monkeypatch, tmp_path,
) -> None:
    session_id = "019fc7c1-a27c-7ae0-9937-7570552a112a"
    project = Project(tmp_path, "-project", Path(), 1, 0.0)
    running = _Running(
        key=session_id,
        tmux_name="cx-live",
        label="project/conversation",
        project=project,
        session_type="codex",
    )
    meta = SessionMeta(
        project=project,
        session_id=session_id,
        jsonl_path=tmp_path / (
            f"rollout-2026-08-03T13-13-03-{session_id}.jsonl"),
        title="current",
        message_count=2,
        token_total=0,
        last_mtime=2.0,
        session_type="codex",
    )
    identity = tmux_ctl.PaneIdentity(
        "%9", 123, "cx-live", "$4", "@5", False, 80, 24)
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.primary.pane_id = "%10"
    app._workspace.primary.agent_tmux_name = "cx-live"
    app._codex_real_pane_identity = MagicMock(return_value=identity)
    stamped = MagicMock(return_value=True)
    monkeypatch.setattr(tmux_ctl, "set_pane_user_option", stamped)
    monkeypatch.setattr(tmux_ctl, "pane_user_option", lambda *_args: None)

    app._stamp_codex_history_state(running, meta)

    assert running.codex_history_generation_stamped
    assert {call.args[0] for call in stamped.call_args_list} == {"%9", "%10"}
    assert stamped.call_count == 4


def test_codex_history_state_preserves_confirmed_canonical_marker_on_restart(
    monkeypatch, tmp_path,
) -> None:
    session_id = "019fc7c1-a27c-7ae0-9937-7570552a112a"
    project = Project(tmp_path, "-project", Path(), 1, 0.0)
    running = _Running(
        key=session_id,
        tmux_name="cx-live",
        label="project/conversation",
        project=project,
        session_type="codex",
    )
    meta = SessionMeta(
        project=project,
        session_id=session_id,
        jsonl_path=tmp_path / (
            f"rollout-2026-08-03T13-13-03-{session_id}.jsonl"),
        title="current",
        message_count=2,
        token_total=0,
        last_mtime=2.0,
        session_type="codex",
    )
    identity = tmux_ctl.PaneIdentity(
        "%9", 123, "cx-live", "$4", "@5", False, 80, 24)
    app = App.__new__(App)
    app._codex_real_pane_identity = MagicMock(return_value=identity)
    stamped = MagicMock(return_value=True)
    monkeypatch.setattr(tmux_ctl, "set_pane_user_option", stamped)
    canonical_marker = (
        f"{tmux_ctl.RAILMUX_CANONICAL_HISTORY_PREFIX}{session_id}"
    )
    monkeypatch.setattr(
        tmux_ctl, "pane_user_option", lambda *_args: canonical_marker)

    app._stamp_codex_history_state(running, meta)

    assert running.codex_history_generation_stamped
    assert stamped.call_count == 1
    assert stamped.call_args.args[:2] == (
        "%9", tmux_server.TRANSCRIPT_SOURCE_OPTION)
