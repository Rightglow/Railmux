"""Selection isolation keeps only the non-selected agent display still."""
from __future__ import annotations

from unittest.mock import MagicMock

from railmux import tmux_ctl
from railmux.selection_isolation import (
    SelectionIsolationManager,
    reconcile_pane,
)
from railmux.ui.workspace import (
    AgentWorkspace,
    DisplayTransportKind,
    WorkspaceLayout,
)


def _state(
    pane_id: str,
    *,
    in_mode: bool = False,
    key: str | None = None,
    peer: str | None = None,
    frozen_by: str | None = None,
) -> tmux_ctl.SelectionPaneState:
    return tmux_ctl.SelectionPaneState(
        pane_id, in_mode, key, peer, frozen_by)


def test_mode_hook_freezes_and_releases_only_the_exact_peer(monkeypatch):
    key = "%9:primary"
    freeze = MagicMock(return_value=True)
    release = MagicMock(return_value=True)
    monkeypatch.setattr(tmux_ctl, "freeze_selection_peer", freeze)
    monkeypatch.setattr(tmux_ctl, "release_selection_peer", release)
    current = _state("%1", in_mode=True, key=key, peer="%2")
    monkeypatch.setattr(
        tmux_ctl, "selection_pane_state", lambda _pane: current)

    assert reconcile_pane("%1")
    freeze.assert_called_once_with("%2", key)

    current = _state("%1", key=key, peer="%2")
    monkeypatch.setattr(
        tmux_ctl, "list_selection_pane_states", lambda: (current,))
    assert reconcile_pane("%1")
    release.assert_called_once_with("%2", key)

    current = _state("%2", in_mode=True, frozen_by=key)
    assert not reconcile_pane("%2")
    assert freeze.call_count == 1
    assert release.call_count == 1


def test_mode_exit_keeps_freeze_while_nested_representation_is_selecting(
        monkeypatch):
    key = "%9:primary"
    exiting = _state("%1", key=key, peer="%2")
    inner = _state("%11", in_mode=True, key=key, peer="%2")
    monkeypatch.setattr(
        tmux_ctl, "selection_pane_state", lambda _pane: exiting)
    monkeypatch.setattr(
        tmux_ctl, "list_selection_pane_states", lambda: (exiting, inner))
    release = MagicMock(return_value=True)
    monkeypatch.setattr(tmux_ctl, "release_selection_peer", release)

    assert reconcile_pane("%1")
    release.assert_not_called()


def test_nested_projection_covers_outer_and_inner_panes(monkeypatch):
    workspace = AgentWorkspace()
    workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    workspace.primary.pane_id = "%1"
    workspace.primary.agent_tmux_name = "agent-a"
    workspace.primary.transport_kind = DisplayTransportKind.NESTED
    workspace.secondary.pane_id = "%2"
    workspace.secondary.agent_tmux_name = "agent-b"
    workspace.secondary.transport_kind = DisplayTransportKind.SWAP
    monkeypatch.setattr(tmux_ctl, "tmux_version", lambda: (3, 4))
    monkeypatch.setattr(
        tmux_ctl, "session_pane_id",
        lambda name: "%11" if name == "agent-a" else "%12",
    )
    set_option = MagicMock(return_value=True)
    monkeypatch.setattr(tmux_ctl, "set_pane_user_option", set_option)
    monkeypatch.setattr(
        tmux_ctl, "unset_pane_user_option_if_value", MagicMock())
    monkeypatch.setattr(
        tmux_ctl, "release_selection_peer", MagicMock(return_value=True))

    manager = SelectionIsolationManager("%9")
    manager.sync(workspace, enabled=True)

    assert set(manager._projected) == {"%1", "%11", "%2"}
    assert manager._projected["%1"].selection_key == "%9:primary"
    assert manager._projected["%11"].peer_pane_id == "%2"
    assert manager._projected["%2"].selection_key == "%9:secondary"


def test_tmux_before_3_0_never_projects_pane_options(monkeypatch):
    workspace = AgentWorkspace()
    workspace.layout = WorkspaceLayout.STACKED
    workspace.primary.pane_id = "%1"
    workspace.secondary.pane_id = "%2"
    monkeypatch.setattr(tmux_ctl, "tmux_version", lambda: (2, 9))
    set_option = MagicMock(return_value=True)
    list_states = MagicMock()
    monkeypatch.setattr(tmux_ctl, "set_pane_user_option", set_option)
    monkeypatch.setattr(tmux_ctl, "list_selection_pane_states", list_states)

    manager = SelectionIsolationManager("%9")
    manager.sync(workspace, enabled=True)
    manager.maintain()
    tmux_ctl.cleanup_stale_selection_markers(frozenset({"%9"}))

    set_option.assert_not_called()
    list_states.assert_not_called()


def test_maintain_heals_missed_hook_and_late_freeze_after_projection_clear(
        monkeypatch):
    key = "%9:primary"
    workspace = AgentWorkspace()
    workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    workspace.primary.pane_id = "%1"
    workspace.secondary.pane_id = "%2"
    monkeypatch.setattr(tmux_ctl, "tmux_version", lambda: (3, 4))
    manager = SelectionIsolationManager("%9")
    manager._projected = {
        "%1": manager._desired_projections(workspace)["%1"],
    }
    freeze = MagicMock(return_value=True)
    release = MagicMock(return_value=True)
    monkeypatch.setattr(tmux_ctl, "freeze_selection_peer", freeze)
    monkeypatch.setattr(tmux_ctl, "release_selection_peer", release)
    monkeypatch.setattr(
        tmux_ctl,
        "list_selection_pane_states",
        lambda: (
            _state("%1", in_mode=True, key=key, peer="%2"),
            _state("%2"),
        ),
    )

    manager.maintain()
    freeze.assert_called_once_with("%2", key)

    manager._projected = {}
    monkeypatch.setattr(
        tmux_ctl,
        "list_selection_pane_states",
        lambda: (_state("%2", in_mode=True, frozen_by=key),),
    )
    manager.maintain()
    release.assert_called_once_with("%2", key)


def test_dead_controller_markers_are_released_without_touching_live_owner(
        monkeypatch):
    stale = "%7:primary"
    live = "%9:secondary"
    monkeypatch.setattr(
        tmux_ctl,
        "list_selection_pane_states",
        lambda: (
            _state("%1", key=stale, peer="%2"),
            _state("%2", in_mode=True, frozen_by=stale),
            _state("%3", key=live, peer="%4"),
        ),
    )
    release = MagicMock(return_value=True)
    unset = MagicMock(return_value=True)
    monkeypatch.setattr(tmux_ctl, "release_selection_peer", release)
    monkeypatch.setattr(tmux_ctl, "unset_pane_user_option_if_value", unset)

    tmux_ctl.cleanup_stale_selection_markers(
        frozenset({"%1", "%2", "%3", "%4", "%9"}))

    release.assert_called_once_with("%2", stale)
    assert unset.call_count == 2
    assert all(live not in call.args for call in unset.call_args_list)
