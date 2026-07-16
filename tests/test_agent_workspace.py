"""Bounded agent-workspace state and legacy primary-slot accessors."""
from railmux.ui.app import App
from railmux.ui.workspace import AgentWorkspace, WorkspaceLayout


def test_workspace_slots_are_independent_and_bounded_to_two():
    workspace = AgentWorkspace()
    workspace.primary.pane_id = "%1"
    workspace.primary.agent_tmux_name = "cc-one"
    workspace.secondary.pane_id = "%2"
    workspace.secondary.agent_tmux_name = "cx-two"

    assert len(workspace.slots) == 2
    assert workspace.slot_for_pane("%2") is workspace.secondary
    assert workspace.slot_for_agent("cc-one") is workspace.primary
    assert workspace.can_display(workspace.primary, "cc-one") is True
    assert workspace.can_display(workspace.secondary, "cc-one") is False
    assert workspace.primary.agent_tmux_name == "cc-one"
    assert workspace.secondary.agent_tmux_name == "cx-two"


def test_collapse_resets_only_secondary_and_returns_outer_pane():
    workspace = AgentWorkspace()
    workspace.layout = WorkspaceLayout.STACKED
    workspace.primary.pane_id = "%1"
    workspace.primary.agent_tmux_name = "cc-one"
    workspace.secondary.pane_id = "%2"
    workspace.secondary.agent_tmux_name = "cx-two"
    workspace.activate(AgentWorkspace.SECONDARY)

    assert workspace.collapse_to_primary() == "%2"
    assert workspace.layout == WorkspaceLayout.SINGLE
    assert workspace.active is workspace.primary
    assert workspace.primary.agent_tmux_name == "cc-one"
    assert workspace.secondary.pane_id is None
    assert workspace.secondary.agent_tmux_name is None


def test_legacy_right_pane_properties_are_primary_slot_views():
    app = App.__new__(App)
    app._right_pane_id = "%7"
    app._right_pane_claude = "cx-agent"
    app._active_session_id = "session-id"
    app._in_history_mode = True

    slot = app._agent_workspace().primary
    assert slot.pane_id == "%7"
    assert slot.agent_tmux_name == "cx-agent"
    assert slot.active_session_id == "session-id"
    assert slot.in_history_mode is True
