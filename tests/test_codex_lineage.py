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


def test_live_rewind_preserves_view_and_stamps_canonical_transcript(
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
        codex_rollout_proven_in_pane=True,
        codex_baseline_message_count=1,
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
    app._codex_exact_meta = MagicMock(return_value=None)
    app._codex_rollout_ids = MagicMock(return_value={root_id})
    app._stamp_running = MagicMock()
    transport = MagicMock()
    transport.displayed_real_pane.return_value = "%9"
    app._display_transport = MagicMock(return_value=transport)
    monkeypatch.setattr(tmux_ctl, "pane_identity", lambda _pane: identity)
    stamped = MagicMock(return_value=True)
    monkeypatch.setattr(tmux_ctl, "set_pane_user_option", stamped)
    monkeypatch.setattr(tmux_ctl, "pane_user_option", lambda *_args: None)
    probes: dict[str, set[str] | None] = {}

    # An indexed child is not enough: on procfs the exact live writer must
    # first expose that rollout through its open file descriptors.
    app._sync_codex_rewind_scrollback(running, meta, None, probes)
    assert running.codex_canonical_session_id == root_id

    probes.clear()
    app._codex_rollout_ids.return_value = {root_id, leaf_id}
    app._sync_codex_rewind_scrollback(running, meta, None, probes)

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
    app._stamp_running = MagicMock()

    app._sync_codex_rewind_scrollback(running, meta, None, {})

    assert running.codex_canonical_session_id == session_id
    app._stamp_codex_history_state.assert_called_once_with(running, meta)
    app._stamp_running.assert_called_once_with(running)


def test_codex_conversation_proof_is_persisted_only_once(tmp_path) -> None:
    session_id = "019fc605-5188-7212-bc48-ea023fe8b73c"
    project = Project(tmp_path, "-project", Path(), 1, 0.0)
    running = _Running(
        key=session_id,
        tmux_name="cx-live",
        label="project/conversation",
        project=project,
        session_type="codex",
        codex_canonical_session_id=session_id,
        codex_baseline_message_count=1,
        codex_history_generation_stamped=True,
    )
    meta = SessionMeta(
        project=project,
        session_id=session_id,
        jsonl_path=tmp_path / f"rollout-{session_id}.jsonl",
        title="root",
        message_count=2,
        token_total=0,
        last_mtime=2.0,
        session_type="codex",
    )
    app = App.__new__(App)
    app._stamp_running = MagicMock()

    app._sync_codex_rewind_scrollback(running, meta, None, {})
    app._sync_codex_rewind_scrollback(running, meta, None, {})

    assert running.codex_rollout_proven_in_pane
    app._stamp_running.assert_called_once_with(running)


def test_codex_resume_bootstrap_child_keeps_raw_history(
    monkeypatch, tmp_path,
) -> None:
    parent_id = "019fc605-5188-7212-bc48-ea023fe8b73c"
    child_id = "019fc7c1-a27c-7ae0-9937-7570552a112a"
    project = Project(tmp_path, "-project", Path(), 1, 0.0)
    running = _Running(
        key=parent_id,
        tmux_name="cx-live",
        label="project/conversation",
        project=project,
        session_type="codex",
        codex_canonical_session_id=parent_id,
        codex_baseline_message_count=8,
    )
    parent = SessionMeta(
        project=project,
        session_id=parent_id,
        jsonl_path=tmp_path / f"rollout-{parent_id}.jsonl",
        title="parent",
        message_count=8,
        token_total=0,
        last_mtime=1.0,
        session_type="codex",
    )
    child = SessionMeta(
        project=project,
        session_id=child_id,
        jsonl_path=tmp_path / f"rollout-{child_id}.jsonl",
        title="child",
        message_count=8,
        token_total=0,
        last_mtime=2.0,
        session_type="codex",
        forked_from_id=parent_id,
    )
    app = App.__new__(App)
    app._codex_lineage_ids = MagicMock(return_value={parent_id, child_id})
    app._codex_exact_meta = MagicMock(return_value=parent)
    app._codex_rollout_ids = MagicMock(return_value={child_id})
    app._stamp_codex_history_state = MagicMock()
    app._stamp_running = MagicMock()

    app._sync_codex_rewind_scrollback(running, child, None, {})

    assert running.codex_canonical_session_id == child_id
    assert running.codex_rollout_proven_in_pane
    assert running.codex_baseline_message_count == child.message_count
    app._stamp_codex_history_state.assert_called_once_with(running, child)
    app._stamp_running.assert_called_once_with(running)


def test_first_observed_resume_child_is_adopted_as_proven_raw_generation(
    tmp_path,
) -> None:
    parent_id = "019fc605-5188-7212-bc48-ea023fe8b73c"
    child_id = "019fc7c1-a27c-7ae0-9937-7570552a112a"
    project = Project(tmp_path, "-project", Path(), 1, 0.0)
    running = _Running(
        key=parent_id,
        tmux_name="cx-live",
        label="project/conversation",
        project=project,
        session_type="codex",
    )
    child = SessionMeta(
        project=project,
        session_id=child_id,
        jsonl_path=tmp_path / f"rollout-{child_id}.jsonl",
        title="child",
        message_count=8,
        token_total=0,
        last_mtime=2.0,
        session_type="codex",
        forked_from_id=parent_id,
    )
    app = App.__new__(App)
    app._codex_lineage_ids = MagicMock(return_value={parent_id, child_id})
    app._codex_rollout_ids = MagicMock(return_value={child_id})
    app._stamp_codex_history_state = MagicMock()
    app._stamp_running = MagicMock()

    app._sync_codex_rewind_scrollback(running, child, None, {})

    assert running.codex_canonical_session_id == child_id
    assert running.codex_rollout_proven_in_pane
    assert running.codex_baseline_message_count == child.message_count
    app._stamp_codex_history_state.assert_called_once_with(running, child)


def test_first_observed_resume_child_waits_for_exact_live_writer(
    tmp_path,
) -> None:
    parent_id = "019fc605-5188-7212-bc48-ea023fe8b73c"
    child_id = "019fc7c1-a27c-7ae0-9937-7570552a112a"
    project = Project(tmp_path, "-project", Path(), 1, 0.0)
    running = _Running(
        key=parent_id,
        tmux_name="cx-live",
        label="project/conversation",
        project=project,
        session_type="codex",
    )
    child = SessionMeta(
        project=project,
        session_id=child_id,
        jsonl_path=tmp_path / f"rollout-{child_id}.jsonl",
        title="child",
        message_count=8,
        token_total=0,
        last_mtime=2.0,
        session_type="codex",
        forked_from_id=parent_id,
    )
    app = App.__new__(App)
    app._codex_lineage_ids = MagicMock(return_value={parent_id, child_id})
    app._codex_rollout_ids = MagicMock(return_value={parent_id})
    app._stamp_codex_history_state = MagicMock()
    app._stamp_running = MagicMock()

    app._sync_codex_rewind_scrollback(running, child, None, {})

    assert running.codex_canonical_session_id is None
    assert not running.codex_rollout_proven_in_pane
    app._stamp_codex_history_state.assert_not_called()
    app._stamp_running.assert_not_called()


def test_codex_parent_activity_confirms_same_poll_rewind(
    monkeypatch, tmp_path,
) -> None:
    parent_id = "019fc605-5188-7212-bc48-ea023fe8b73c"
    child_id = "019fc7c1-a27c-7ae0-9937-7570552a112a"
    project = Project(tmp_path, "-project", Path(), 1, 0.0)
    running = _Running(
        key=parent_id,
        tmux_name="cx-live",
        label="project/conversation",
        project=project,
        session_type="codex",
        codex_canonical_session_id=parent_id,
        codex_baseline_message_count=8,
    )
    parent = SessionMeta(
        project=project,
        session_id=parent_id,
        jsonl_path=tmp_path / f"rollout-{parent_id}.jsonl",
        title="parent",
        message_count=10,
        token_total=0,
        last_mtime=2.0,
        session_type="codex",
    )
    child = SessionMeta(
        project=project,
        session_id=child_id,
        jsonl_path=tmp_path / f"rollout-{child_id}.jsonl",
        title="child",
        message_count=9,
        token_total=0,
        last_mtime=3.0,
        session_type="codex",
        forked_from_id=parent_id,
    )
    identity = tmux_ctl.PaneIdentity(
        "%9", 123, "railmux", "$4", "@5", False, 80, 24)
    app = App.__new__(App)
    app._codex_lineage_ids = MagicMock(return_value={parent_id, child_id})
    app._codex_exact_meta = MagicMock(return_value=parent)
    app._codex_rollout_ids = MagicMock(return_value={child_id})
    app._codex_real_pane_identity = MagicMock(return_value=identity)
    app._stamp_codex_history_state = MagicMock()
    app._stamp_running = MagicMock()

    app._sync_codex_rewind_scrollback(running, child, None, {})

    app._codex_real_pane_identity.assert_called_once_with(running)
    app._stamp_codex_history_state.assert_called_once_with(
        running, child, canonical=True)


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


def test_codex_history_state_downgrades_released_v1_marker_to_raw(
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
        jsonl_path=tmp_path / f"rollout-{session_id}.jsonl",
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
    monkeypatch.setattr(
        tmux_ctl,
        "pane_user_option",
        lambda *_args: (
            f"{tmux_ctl.RAILMUX_LEGACY_CANONICAL_HISTORY_PREFIX}{session_id}"
        ),
    )

    app._stamp_codex_history_state(running, meta)

    assert stamped.call_count == 2
    assert stamped.call_args_list[-1].args == (
        "%9", tmux_ctl.RAILMUX_HISTORY_GENERATION_OPTION, session_id)
    assert not running.codex_rollout_proven_in_pane


def test_codex_history_state_unifies_wrapper_with_existing_canonical_real_pane(
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
        jsonl_path=tmp_path / f"rollout-{session_id}.jsonl",
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
    canonical = f"{tmux_ctl.RAILMUX_CANONICAL_HISTORY_PREFIX}{session_id}"
    monkeypatch.setattr(
        tmux_ctl,
        "pane_user_option",
        lambda pane_id, _option: canonical if pane_id == "%9" else None,
    )
    stamped = MagicMock(return_value=True)
    monkeypatch.setattr(tmux_ctl, "set_pane_user_option", stamped)

    app._stamp_codex_history_state(running, meta)

    generation_calls = [
        call
        for call in stamped.call_args_list
        if call.args[1] == tmux_ctl.RAILMUX_HISTORY_GENERATION_OPTION
    ]
    assert [call.args for call in generation_calls] == [(
        "%10", tmux_ctl.RAILMUX_HISTORY_GENERATION_OPTION, canonical)]
    assert running.codex_rollout_proven_in_pane
    assert running.codex_history_generation_stamped
