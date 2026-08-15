from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import urwid

from railmux import tmux_ctl, tmux_server
from railmux.models import Project, SessionMeta
from railmux.display_transport import AttachOutcome
from railmux.ui import keymap
from railmux.ui.app import App, _FocusAwareFrame, _Running
from railmux.ui.modals import DeleteConfirmModal, RenameModal
from railmux.ui.projects_pane import ProjectsPane
from railmux.ui.running_pane import RunningEntry
from railmux.ui.sessions_pane import _SessionRow
from railmux.ui.workspace import (
    AgentWorkspace,
    AgentSlot,
    DisplayTransportKind,
    WorkspaceLayout,
    WorkspacePage,
    WorkspacePresentation,
)


def test_active_claude_pane_stamps_and_clears_exact_transcript_source(
    monkeypatch, tmp_path,
):
    session_id = "47fca075-9cb8-44fb-a314-d57ef2256ad9"
    project = Project(tmp_path, "-tmp-project", tmp_path / "history", 1, 1.0)
    running = _Running(
        session_id,
        "agent-session",
        "Claude",
        project=project,
        session_type="claude",
    )
    app = App.__new__(App)
    app._by_tmux = lambda name: running if name == "agent-session" else None
    slot = AgentSlot("primary", pane_id="%8")
    observed = []
    monkeypatch.setattr(
        tmux_ctl,
        "set_pane_user_option",
        lambda pane, option, value: observed.append((pane, option, value)) or True,
    )

    app._sync_slot_transcript_source(slot, session_id, "agent-session")
    app._sync_slot_transcript_source(slot, None, None)

    assert observed[0][0:2] == (
        "%8", tmux_server.TRANSCRIPT_SOURCE_OPTION,
    )
    assert f"{session_id}.jsonl" in observed[0][2]
    assert observed[1] == (
        "%8", tmux_server.TRANSCRIPT_SOURCE_OPTION, None,
    )
    assert len(observed) == 2


def test_active_codex_pane_stamps_explicit_preview_transcript_source(
    monkeypatch, tmp_path,
):
    session_id = "019fc7c1-a27c-7ae0-9937-7570552a112a"
    running = _Running(
        session_id,
        "cx-agent",
        "Codex",
        session_type="codex",
    )
    project = Project(tmp_path, "-tmp", Path(), 1, 1.0)
    meta = SessionMeta(
        project=project,
        session_id=session_id,
        jsonl_path=tmp_path / (
            f"rollout-2026-08-04T02-49-33-{session_id}.jsonl"),
        title="Codex",
        message_count=1,
        token_total=0,
        last_mtime=1.0,
        session_type="codex",
    )
    app = App.__new__(App)
    app._by_tmux = lambda name: running if name == "cx-agent" else None
    app._codex_representative = lambda sid: meta if sid == session_id else None
    slot = AgentSlot("secondary", pane_id="%9")
    observed = []
    monkeypatch.setattr(
        tmux_ctl,
        "set_pane_user_option",
        lambda pane, option, value: observed.append((pane, option, value)) or True,
    )

    app._sync_slot_transcript_source(slot, session_id, "cx-agent")

    assert observed[0][0:2] == (
        "%9", tmux_server.TRANSCRIPT_SOURCE_OPTION,
    )
    assert session_id in observed[0][2]
    assert len(observed) == 1


def test_stable_attach_batches_preview_and_target_projection(
    monkeypatch, tmp_path,
):
    session_id = "47fca075-9cb8-44fb-a314-d57ef2256ad9"
    project = Project(tmp_path, "-tmp-project", tmp_path / "history", 1, 1.0)
    running = _Running(
        session_id,
        "agent-session",
        "Claude",
        project=project,
        session_type="claude",
    )
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.primary.pane_id = "%8"
    app._railmux_pane_id = "%1"
    app._tmux_binding_manager = SimpleNamespace(
        target_toggle_available=True)
    app._by_tmux = lambda name: running if name == "agent-session" else None
    app._projected_target_pane_id = "%7"
    batches = []
    fallback = MagicMock()
    app._sync_slot_transcript_source = fallback
    monkeypatch.setattr(tmux_ctl, "tmux_version", lambda: (3, 7))
    monkeypatch.setattr(
        tmux_ctl,
        "run_command_queue",
        lambda commands: batches.append(commands) or True,
    )

    app._sync_attached_slot_projection(
        app._workspace.primary, "agent-session")

    assert len(batches) == 1
    assert batches[0][0][:4] == ("set-option", "-p", "-t", "%8")
    assert batches[0][1] == (
        "set-window-option", "-t", "%1",
        tmux_ctl.RAILMUX_TARGET_OPTION, "%8",
    )
    assert app._projected_target_pane_id == "%8"
    fallback.assert_not_called()


def test_stable_attach_projection_batch_failure_preserves_both_fallbacks(
    monkeypatch,
):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.primary.pane_id = "%8"
    app._railmux_pane_id = "%1"
    app._tmux_binding_manager = SimpleNamespace(
        target_toggle_available=True)
    app._by_tmux = lambda _name: None
    app._sync_slot_transcript_source = MagicMock()
    app._sync_target_pane_option = MagicMock()
    monkeypatch.setattr(tmux_ctl, "tmux_version", lambda: (3, 7))
    monkeypatch.setattr(tmux_ctl, "run_command_queue", lambda _commands: False)

    app._sync_attached_slot_projection(
        app._workspace.primary, "agent-session")

    app._sync_slot_transcript_source.assert_called_once_with(
        app._workspace.primary, None, "agent-session")
    app._sync_target_pane_option.assert_called_once_with()


def _canvas_attrs(canvas) -> list[str | None]:
    return [
        attr
        for row in canvas.content()
        for attr, _charset, text in row
        for _ in text
    ]


def _row_attrs(canvas, row_index: int) -> list[str | None]:
    return [
        attr
        for attr, _charset, text in list(canvas.content())[row_index]
        for _ in text
    ]


def test_inactive_frame_suppresses_focus_but_keeps_selection_and_status():
    project = Project(Path("/tmp/p"), "-tmp-p", Path("/tmp/meta"), 1, 1.0)
    session = SessionMeta(
        project=project,
        session_id="1" * 36,
        jsonl_path=Path("/tmp/session.jsonl"),
        title="Selected",
        message_count=1,
        token_total=1,
        last_mtime=1.0,
        status="busy",
    )
    row = _SessionRow(session, is_running=True, is_selected=True)
    frame = _FocusAwareFrame(urwid.ListBox(urwid.SimpleFocusListWalker([row])))

    assert "focus" in _canvas_attrs(frame.render((30, 4), focus=True))

    frame.set_window_active(False)
    attrs = _canvas_attrs(frame.render((30, 4), focus=True))
    assert "focus" not in attrs
    assert "pane_focus" not in attrs
    assert "selected" in attrs
    assert "status_busy_sel" in attrs


def test_sidebar_gutter_separates_pane_edge_from_tmux_divider():
    pane = urwid.AttrMap(
        urwid.LineBox(urwid.SolidFill(" ")),
        "pane",
        focus_map="pane_focus",
    )
    padded = urwid.Padding(pane, right=1)
    canvas = padded.render((20, 5), focus=True)

    for row_index in range(canvas.rows()):
        attrs = _row_attrs(canvas, row_index)
        assert attrs[-2] == "pane_focus"
        assert attrs[-1] is None


def test_pane_focus_colours_chrome_without_leaking_into_body_rows():
    project = Project(Path("/tmp/proj"), "-tmp-proj", Path("/tmp/meta"), 1, 1.0)
    pane = ProjectsPane([project], on_select=lambda _project: None)
    wrapped = urwid.AttrMap(pane, "pane", focus_map="pane_focus")

    canvas = wrapped.render((30, 8), focus=True)

    # Border/title use the bright pane accent, the cursor has its own deep
    # focus background, and ordinary empty/divider cells remain neutral body.
    assert set(_row_attrs(canvas, 0)) == {"pane_focus"}
    cursor_segments = [attr for attr, _charset, _text
                       in list(canvas.content())[3]]
    body_segments = [attr for attr, _charset, _text
                     in list(canvas.content())[4]]
    assert cursor_segments[0] == cursor_segments[-1] == "pane_focus"
    assert set(cursor_segments[1:-1]) == {"focus"}
    assert body_segments == ["pane_focus", "body", "pane_focus"]


def test_focus_reports_are_consumed_and_update_divider(monkeypatch):
    app = App.__new__(App)
    app._frame = _FocusAwareFrame(urwid.SolidFill(" "))
    app._railmux_has_focus = True
    app._divider_active = None

    set_border = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_border_styles", set_border)

    assert app._filter_input(["focus out", "x"], []) == ["x"]
    assert app._frame._window_active is False
    assert set_border.call_args.args == ("fg=#5faf00", "fg=#5faf00")

    assert app._filter_input(["focus in"], []) == []
    assert app._frame._window_active is True
    assert set_border.call_args.args == ("fg=colour240", "fg=colour240")


def test_focus_return_activates_the_agent_tmux_reports_as_last(monkeypatch):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    app._workspace.primary.pane_id = "%2"
    app._workspace.primary.active_session_id = "primary-session"
    app._workspace.secondary.pane_id = "%3"
    app._workspace.secondary.active_session_id = "secondary-session"
    app._frame = _FocusAwareFrame(urwid.SolidFill(" "))
    app._sessions_pane = MagicMock()
    app._running_pane = MagicMock()
    app._railmux_pane_id = "%1"
    app._railmux_has_focus = False
    app._divider_active = None
    app._double_focus_visual_pending = False
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.active_pane_id", lambda _target: "%1")
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.last_pane_id", lambda _target: "%3")
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_border_styles", lambda *_a: True)

    assert app._filter_input(["focus in"], []) == []

    assert app._workspace.target_slot_key == AgentWorkspace.SECONDARY
    app._sessions_pane.set_active_session.assert_called_once_with(
        "secondary-session")


def test_stale_focus_in_cannot_gray_actually_focused_second_pane(monkeypatch):
    """tmux focus wins when a terminal host delivers a late focus report."""
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    app._workspace.primary.pane_id = "%2"
    app._workspace.secondary.pane_id = "%3"
    app._workspace.set_target(AgentWorkspace.SECONDARY)
    app._frame = _FocusAwareFrame(urwid.SolidFill(" "))
    app._frame.set_window_active(True)
    app._railmux_pane_id = "%1"
    app._railmux_has_focus = True  # stale all-gray model
    app._divider_active = None
    app._double_focus_visual_pending = False
    app._in_paste = False
    app._paste_passthrough = False
    app._sync_border_indicators = MagicMock(return_value=True)
    app._apply_tmux_bar = MagicMock()
    set_border = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.active_pane_id", lambda _target: "%3")
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_border_styles", set_border)

    assert app._filter_input(["focus in"], []) == []

    assert app._railmux_has_focus is False
    assert app._frame._window_active is False
    assert app._workspace.target_slot_key == AgentWorkspace.SECONDARY
    set_border.assert_called_once_with("fg=colour240", "fg=#5faf00")


def test_agent_to_agent_click_repaints_layout_indicator(monkeypatch):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    app._workspace.primary.pane_id = "%2"
    app._workspace.primary.agent_tmux_name = "cx-primary"
    app._workspace.secondary.pane_id = "%3"
    app._workspace.secondary.agent_tmux_name = "cx-secondary"
    app._frame = _FocusAwareFrame(urwid.SolidFill(" "))
    app._sessions_pane = MagicMock()
    app._running_pane = MagicMock()
    app._railmux_pane_id = "%1"
    app._railmux_has_focus = False
    app._apply_tmux_bar = MagicMock()
    app._hint_bar = MagicMock()
    app._set_status = MagicMock()
    app._sync_sidebar_to_agent_project = MagicMock()
    focused = ["%3"]
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.active_pane_id", lambda _target: focused[0])

    app._sync_target_slot_from_tmux()

    assert app._workspace.target_slot_key == AgentWorkspace.SECONDARY
    app._sync_sidebar_to_agent_project.assert_called_once_with(
        "cx-secondary")
    app._apply_tmux_bar.assert_called_once_with(app._tmux_error_bar)
    app._hint_bar.set_context.assert_called_once_with(
        keymap.CTX_AGENT_P2_SIDE_BY_SIDE)
    app._set_status.assert_called_once_with("Agent Pane 2 focused")

    app._apply_tmux_bar.reset_mock()
    app._hint_bar.set_context.reset_mock()
    app._set_status.reset_mock()
    app._sync_sidebar_to_agent_project.reset_mock()
    app._sync_target_slot_from_tmux()
    app._apply_tmux_bar.assert_not_called()
    app._hint_bar.set_context.assert_not_called()
    app._set_status.assert_not_called()
    app._sync_sidebar_to_agent_project.assert_not_called()

    focused[0] = "%2"
    app._sync_target_slot_from_tmux()

    assert app._workspace.target_slot_key == AgentWorkspace.PRIMARY
    app._sync_sidebar_to_agent_project.assert_called_once_with("cx-primary")
    app._hint_bar.set_context.assert_called_once_with(
        keymap.CTX_AGENT_P1_SIDE_BY_SIDE)
    app._set_status.assert_called_once_with("Agent Pane 1 focused")


def test_target_transition_projects_outer_pane_for_prefix_tab(monkeypatch):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.primary.pane_id = "%2"
    app._workspace.secondary.pane_id = "%3"
    app._workspace.layout = WorkspaceLayout.STACKED
    app._railmux_pane_id = "%1"
    app._projected_target_pane_id = None
    set_option = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_user_option", set_option)

    app._set_workspace_target(AgentWorkspace.SECONDARY)

    set_option.assert_called_once_with(
        "%1", tmux_ctl.RAILMUX_TARGET_OPTION, "%3")
    assert app._workspace.target is app._workspace.secondary


def test_stacked_agent_help_context_tracks_focused_slot():
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.STACKED
    app._workspace.primary.pane_id = "%2"
    app._workspace.secondary.pane_id = "%3"
    app._railmux_has_focus = False

    assert app._help_context() == keymap.CTX_AGENT_P1_STACKED
    app._workspace.set_target(AgentWorkspace.SECONDARY)
    assert app._help_context() == keymap.CTX_AGENT_P2_STACKED


def test_dual_workspace_keeps_inactive_borders_gray(monkeypatch):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.STACKED
    app._divider_active = None
    set_border = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_border_styles", set_border)

    app._set_divider_active(True)

    assert set_border.call_args.args == ("fg=colour240", "fg=#5faf00")


def test_side_by_side_agent_focus_adds_inward_border_arrows(monkeypatch):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    app._divider_active = None
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.tmux_version", lambda: (3, 4))
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.local_window_option",
        lambda _name: (True, None),
    )
    set_option = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_option", set_option)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_border_styles",
        lambda *_args: True,
    )

    app._set_divider_active(True)

    set_option.assert_called_once_with("pane-border-indicators", "arrows")
    assert app._border_indicators_arrows is True


def test_side_by_side_sidebar_focus_removes_directional_arrows(monkeypatch):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    app._workspace.primary.pane_id = "%2"
    app._divider_active = None
    app._border_indicators_original_known = True
    app._border_indicators_original = None
    app._border_indicators_arrows = True
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.tmux_version", lambda: (3, 4))
    set_option = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_option", set_option)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_border_styles",
        lambda *_args: True,
    )

    app._set_divider_active(False)

    set_option.assert_called_once_with("pane-border-indicators", "colour")
    assert app._border_indicators_arrows is False


def test_border_indicator_teardown_restores_inheritance(monkeypatch):
    app = App.__new__(App)
    app._border_indicators_original_known = True
    app._border_indicators_original = None
    app._border_indicators_arrows = False
    set_option = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_option", set_option)

    assert app._restore_border_indicators()

    set_option.assert_called_once_with("pane-border-indicators", None)
    assert app._border_indicators_original_known is False


def test_failed_border_indicator_restore_stays_pending_for_retry(monkeypatch):
    app = App.__new__(App)
    app._border_indicators_original_known = True
    app._border_indicators_original = "both"
    app._border_indicators_arrows = True
    set_option = MagicMock(side_effect=[False, True])
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_option", set_option)

    assert not app._restore_border_indicators()
    assert app._border_indicators_original_known is True
    assert app._restore_border_indicators()

    assert set_option.call_args_list[-1].args == (
        "pane-border-indicators", "both")
    assert app._border_indicators_original_known is False


def test_failed_border_arrow_update_remains_retryable(monkeypatch):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    app._divider_active = None
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.tmux_version", lambda: (3, 4))
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.local_window_option",
        lambda _name: (True, "both"),
    )
    set_option = MagicMock(side_effect=[False, True])
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_option", set_option)
    set_border = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_border_styles", set_border)

    app._set_divider_active(True)
    app._set_divider_active(True)

    assert set_option.call_count == 2
    assert set_border.call_count == 2
    assert app._divider_active == (
        True, WorkspaceLayout.SIDE_BY_SIDE, None)


def test_old_tmux_keeps_colour_only_focus_without_arrow_commands(monkeypatch):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    app._divider_active = None
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.tmux_version", lambda: (3, 2))
    set_option = MagicMock()
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_option", set_option)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_border_styles",
        lambda *_args: True,
    )

    app._set_divider_active(True)

    set_option.assert_not_called()
    assert app._divider_active == (
        True, WorkspaceLayout.SIDE_BY_SIDE, None)


def test_dual_sidebar_focus_removes_ambiguous_shared_highlight(monkeypatch):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.STACKED
    app._workspace.primary.pane_id = "%2"
    app._workspace.secondary.pane_id = "%3"
    app._workspace.set_target(AgentWorkspace.SECONDARY)
    app._divider_active = None
    set_border = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_border_styles", set_border)

    app._set_divider_active(False)

    assert set_border.call_args.args == ("fg=colour240", "fg=colour240")


def test_session_open_targets_remembered_secondary_slot():
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.STACKED
    app._workspace.primary.pane_id = "%2"
    app._workspace.secondary.pane_id = "%3"
    app._workspace.set_target(AgentWorkspace.SECONDARY)
    app._cancel_pending_double_focus = MagicMock()
    app._launch_resume = MagicMock()
    session = MagicMock()

    app._on_session_select(session, steal_focus=False, from_double=True)

    app._launch_resume.assert_called_once_with(
        session,
        steal_focus=False,
        from_double=True,
        slot=app._workspace.secondary,
    )


def test_single_workspace_reapplies_one_continuous_green_border(monkeypatch):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.STACKED
    app._divider_active = None
    set_border = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_border_styles", set_border)

    app._set_divider_active(True)
    app._workspace.layout = WorkspaceLayout.SINGLE
    app._set_divider_active(True, force=True)

    assert set_border.call_args.args == ("fg=#5faf00", "fg=#5faf00")


def test_single_workspace_with_tool_colours_only_active_border(monkeypatch):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.SINGLE
    app._divider_active = None
    app._managed_tool_visible = True
    set_border = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_border_styles", set_border
    )

    app._set_divider_active(True)

    set_border.assert_called_once_with("fg=colour240", "fg=#5faf00")


def test_focus_reconciliation_recognizes_managed_tool_pane(monkeypatch):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.primary.pane_id = "%2"
    app._railmux_pane_id = "%1"
    app._railmux_has_focus = True
    app._managed_tool_visible = False
    app._sync_compact_page_from_tmux = MagicMock()
    app._set_railmux_focus = MagicMock()
    manager = MagicMock()
    manager.slot_for_tool.return_value = AgentWorkspace.PRIMARY
    app._get_tool_pane_manager = MagicMock(return_value=manager)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.active_pane_id", lambda _owner: "%9"
    )

    assert app._reconcile_focus_from_tmux()

    assert app._managed_tool_visible is True
    app._set_railmux_focus.assert_called_once_with(False, force_border=True)


def test_tool_reconciliation_reuses_returned_visibility_for_border():
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._managed_tool_visible = False
    app._divider_active = None
    app._railmux_has_focus = False
    app._set_divider_active = MagicMock()
    manager = MagicMock()
    manager.reconcile.return_value = frozenset({"%9"})
    app._get_tool_pane_manager = MagicMock(return_value=manager)
    app._tool_owner_panes = MagicMock(
        return_value={"primary": "%2", "secondary": None}
    )

    app._reconcile_tool_panes()

    manager.visible_tool_panes.assert_not_called()
    assert app._managed_tool_visible is True
    app._set_divider_active.assert_called_once_with(True, force=True)


def test_single_workspace_sidebar_focus_clears_old_dim_target_format(monkeypatch):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.primary.pane_id = "%2"
    app._divider_active = None
    set_border = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_border_styles", set_border)

    app._set_divider_active(False)

    assert set_border.call_args.args == ("fg=colour240", "fg=colour240")


def test_sidebar_focus_does_not_rewrite_border_when_swap_changes_pane_id(
    monkeypatch,
):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.primary.pane_id = "%2"
    app._divider_active = None
    set_border = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_border_styles", set_border)

    app._set_divider_active(False)
    app._workspace.primary.pane_id = "%8"
    app._set_divider_active(False)

    assert set_border.call_count == 1
    assert app._divider_active == (
        False, WorkspaceLayout.SINGLE, AgentWorkspace.PRIMARY)


def test_failed_border_update_is_not_cached(monkeypatch):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._divider_active = None
    set_border = MagicMock(return_value=False)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_border_styles", set_border)

    app._set_divider_active(True)
    app._set_divider_active(True)

    assert set_border.call_count == 2
    assert app._divider_active is None


def test_refresh_retry_heals_arrow_and_green_border_partial_update(monkeypatch):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    app._railmux_has_focus = False
    app._divider_active = None
    app._sync_border_indicators = MagicMock(return_value=True)
    set_border = MagicMock(side_effect=[False, True])
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_border_styles", set_border)

    app._set_divider_active(True)
    app._retry_pending_divider_style()

    assert set_border.call_count == 2
    assert app._divider_active == (
        True, WorkspaceLayout.SIDE_BY_SIDE, None)


def test_refresh_retry_heals_border_options_that_drift_after_success(
        monkeypatch):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    app._railmux_has_focus = False
    app._divider_active = (
        True, WorkspaceLayout.SIDE_BY_SIDE, None)
    app._last_border_verify_at = 0.0
    app._sync_border_indicators = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.time.monotonic", lambda: 10.0)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.window_border_styles",
        lambda: (True, ("fg=colour240", "fg=colour240")),
    )
    set_border = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_border_styles", set_border)

    app._retry_pending_divider_style()

    set_border.assert_called_once_with("fg=colour240", "fg=#5faf00")
    assert app._divider_active == (
        True, WorkspaceLayout.SIDE_BY_SIDE, None)


def test_refresh_retry_repairs_stale_style_cache_from_reconciled_focus(
        monkeypatch):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    app._workspace.primary.pane_id = "%2"
    app._railmux_has_focus = False
    # A reconnect briefly painted sidebar focus, then tmux returned input to
    # P1 without the old process updating its cached style state.
    app._divider_active = (
        False, WorkspaceLayout.SIDE_BY_SIDE, AgentWorkspace.PRIMARY)
    app._sync_border_indicators = MagicMock(return_value=True)
    set_border = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_border_styles", set_border)

    app._retry_pending_divider_style()

    set_border.assert_called_once_with("fg=colour240", "fg=#5faf00")
    assert app._divider_active == (
        True, WorkspaceLayout.SIDE_BY_SIDE, None)


def test_refresh_border_verification_is_throttled(monkeypatch):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._divider_active = (
        True, WorkspaceLayout.SIDE_BY_SIDE, None)
    app._last_border_verify_at = 9.0
    monkeypatch.setattr(
        "railmux.ui.app.time.monotonic", lambda: 10.0)
    read_border = MagicMock()
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.window_border_styles", read_border)

    app._retry_pending_divider_style()

    read_border.assert_not_called()


def test_resize_event_checks_workspace_but_ordinary_input_does_not():
    app = _paste_app()
    app._check_terminal_size = MagicMock()

    assert app._filter_input(["x"], []) == ["x"]
    app._check_terminal_size.assert_not_called()

    assert app._filter_input(["window resize"], []) == ["window resize"]
    app._check_terminal_size.assert_called_once_with()


def _paste_app():
    """App stub in a command-mode context (sidebar focused, no modal)."""
    app = App.__new__(App)
    app._in_paste = False
    app._paste_passthrough = False
    app._double_focus_visual_pending = False
    app._set_status = MagicMock()
    app._loop = None
    app._frame = _FocusAwareFrame(urwid.SolidFill(" "))  # body-focused
    return app


def _with_modal(app, modal):
    """Put *modal* on screen as the active overlay."""
    loop = MagicMock()
    loop.widget = urwid.Overlay(
        modal, app._frame, "center", 10, "middle", 5)
    app._loop = loop
    return app


def _filter_mode(app):
    """Focus the footer filter Edit (text-input context)."""
    app._frame = _FocusAwareFrame(
        urwid.SolidFill(" "), footer=urwid.Edit(), focus_part="footer")
    return app


def test_bracketed_paste_burst_is_dropped_whole():
    # A clipboard whose bytes include destructive command keys (k=kill,
    # d+y=delete-confirm, q+enter+enter=twice-confirmed quit-all) must not
    # reach dispatch.
    app = _paste_app()
    out = app._filter_input(
        [
            "begin paste", "k", "d", "y", "q", "enter", "enter",
            "end paste",
        ], [])
    assert out == []
    assert app._in_paste is False
    app._set_status.assert_called_once()
    # A blocked paste is a rejected action → warn level, not plain info.
    assert app._set_status.call_args.args[1] == "warn"


def test_paste_span_across_reads_is_dropped_and_trailing_key_survives():
    # urwid may deliver a large paste over several input reads; the flag has to
    # persist until "end paste" arrives.  Real keystrokes after the paste closes
    # (same read) are preserved.
    app = _paste_app()
    assert app._filter_input(["begin paste", "h", "e"], []) == []
    assert app._in_paste is True
    assert app._filter_input(["l", "l", "o", "d", "y"], []) == []
    assert app._in_paste is True
    assert app._filter_input(["t", "end paste", "n"], []) == ["n"]
    assert app._in_paste is False
    # Status shown once, on the opening marker only.
    app._set_status.assert_called_once()


def test_real_keystrokes_and_stray_end_marker_pass_through():
    # Single command keys typed by hand (one per read) are NOT pastes and must
    # dispatch normally; a lone "end paste" with no open span is a no-op.
    app = _paste_app()
    assert app._filter_input(["k"], []) == ["k"]
    assert app._filter_input(["d"], []) == ["d"]
    assert app._filter_input(["end paste", "z"], []) == ["z"]
    assert app._in_paste is False
    app._set_status.assert_not_called()


def test_paste_into_filter_edit_passes_through():
    # Filter mode is a text field — pasted content is delivered, markers stripped,
    # and no "ignored" status is shown.
    app = _filter_mode(_paste_app())
    out = app._filter_input(["begin paste", "h", "i", "end paste"], [])
    assert out == ["h", "i"]
    assert app._paste_passthrough is True
    assert app._in_paste is False
    app._set_status.assert_not_called()


def test_paste_into_rename_modal_passes_through():
    app = _with_modal(_paste_app(), RenameModal("old", lambda *_: None, lambda: None))
    out = app._filter_input(["begin paste", "x", "y", "end paste"], [])
    assert out == ["x", "y"]
    app._set_status.assert_not_called()


def test_paste_into_delete_confirm_is_still_blocked():
    # The hazard case: a pasted 'y' must NOT confirm a pending delete.
    app = _with_modal(
        _paste_app(), DeleteConfirmModal(
            "Delete session", "t", "d", lambda: None, lambda: None))
    out = app._filter_input(["begin paste", "y", "end paste"], [])
    assert out == []
    app._set_status.assert_called_once()


def test_burst_without_markers_dropped_in_command_mode():
    # Terminals lacking bracketed paste: a dense character burst in one read is
    # still recognised as a paste and dropped in the sidebar.
    app = _paste_app()
    out = app._filter_input(list("hellodyk"), [])
    assert out == []
    app._set_status.assert_called_once()


def test_two_key_burst_is_treated_as_paste():
    # _PASTE_BURST_MIN is 2 by design — d+y is two single keys that arrive
    # in one read, which a paste would also do.  Two fast keystrokes hitting
    # the guard is an annoyance; missing a d+y paste is data loss.
    app = _paste_app()
    assert app._filter_input(["d", "y"], []) == []
    app._set_status.assert_called_once()


def test_burst_without_markers_allowed_in_text_field():
    app = _filter_mode(_paste_app())
    out = app._filter_input(list("hello"), [])
    assert out == list("hello")
    app._set_status.assert_not_called()



def test_double_click_prepaints_focus_before_tmux_settles(monkeypatch):
    app = App.__new__(App)
    app._frame = _FocusAwareFrame(urwid.SolidFill(" "))
    app._railmux_has_focus = True
    app._divider_active = None
    app._right_pane_id = "%2"
    app._double_focus_alarm = None
    app._double_focus_visual_pending = False
    loop = MagicMock()
    alarm = object()
    loop.set_alarm_in.return_value = alarm
    app._loop = loop
    set_border = MagicMock(return_value=True)
    select_pane = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_border_styles", set_border)
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.select_pane", select_pane)

    app._schedule_right_pane_focus_after_double()

    delay, callback = loop.set_alarm_in.call_args.args
    assert delay == App._DOUBLE_CLICK_FOCUS_DELAY
    assert app._double_focus_alarm is alarm
    assert app._double_focus_visual_pending is True
    assert app._railmux_has_focus is False
    assert app._frame._window_active is False
    set_border.assert_called_once_with("fg=#5faf00", "fg=#5faf00")
    loop.draw_screen.assert_called_once_with()
    select_pane.assert_not_called()

    callback(loop, None)

    select_pane.assert_called_once_with("%2")
    assert app._double_focus_alarm is None
    assert app._double_focus_visual_pending is False
    assert app._railmux_has_focus is False
    assert loop.draw_screen.call_count == 2


def test_single_click_prepaints_sidebar_before_agent_transport_switch():
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    slot = app._workspace.primary
    slot.active_session_id = "old-session"
    slot.agent_tmux_name = "cc-old"
    app._double_focus_visual_pending = False
    app._redraw_focus_state_now = MagicMock()
    app._check_agent_slot_size = MagicMock()
    app._set_active_tmux_target = MagicMock()
    app._sync_attached_slot_projection = MagicMock()
    app._set_railmux_focus = MagicMock()
    app._schedule_scroll_acceleration = MagicMock()
    app._install_tmux_bindings = MagicMock()
    app._modes = MagicMock()
    app._modes.return_value.for_tmux_name.return_value = MagicMock(key="claude")
    app._running = {
        "old-session": SimpleNamespace(
            key="old-session", tmux_name="cc-old", is_placeholder=False),
        "new-session": SimpleNamespace(
            key="new-session", tmux_name="cc-live", is_placeholder=False),
    }
    app._sessions_pane = MagicMock()
    app._running_pane = MagicMock()
    events: list[str] = []
    app._sessions_pane.set_active_session.side_effect = (
        lambda session_id: events.append(f"session:{session_id}"))
    app._running_pane.set_active.side_effect = (
        lambda tmux_name: events.append(f"running:{tmux_name}"))
    app._redraw_focus_state_now.side_effect = lambda: events.append("draw")
    transport = MagicMock()

    def attach(slot, _tmux_name):
        events.append("attach")
        assert slot.active_session_id == "old-session"
        assert slot.agent_tmux_name == "cc-old"
        slot.pane_id = "%2"
        return AttachOutcome(
            True, DisplayTransportKind.SWAP, display_stable=True)

    transport.attach.side_effect = attach
    app._display_transport_manager = transport

    assert app._attach_agent_slot(
        slot, "cc-live", steal_focus=False)
    assert events[:4] == [
        "session:new-session", "running:cc-live", "draw", "attach"]
    app._set_active_tmux_target.assert_called_once_with(
        "cc-live", slot, sync_transcript_source=False)
    app._sync_attached_slot_projection.assert_called_once_with(
        slot, "cc-live")


def test_stable_same_target_skips_geometry_chrome_and_binding_reprojection():
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    slot = app._workspace.primary
    slot.pane_id = "%2"
    slot.agent_tmux_name = "cc-live"
    app._double_focus_visual_pending = False
    app._paint_slot_active_tmux_target = MagicMock()
    app._redraw_focus_state_now = MagicMock()
    app._check_agent_slot_size = MagicMock()
    app._set_active_tmux_target = MagicMock()
    app._sync_attached_slot_projection = MagicMock()
    app._set_railmux_focus = MagicMock()
    app._schedule_scroll_acceleration = MagicMock()
    app._apply_layout_profile = MagicMock()
    app._install_tmux_bindings = MagicMock()
    app._modes = MagicMock()
    app._modes.return_value.for_tmux_name.return_value = MagicMock(key="claude")
    app._running = {
        "session": SimpleNamespace(
            key="session", tmux_name="cc-live", is_placeholder=False,
            is_legacy=False),
    }
    transport = MagicMock()
    transport.attach.return_value = AttachOutcome(
        True,
        DisplayTransportKind.SWAP,
        display_stable=True,
        target_unchanged=True,
    )
    app._display_transport_manager = transport

    assert app._attach_agent_slot(slot, "cc-live", steal_focus=False)

    app._check_agent_slot_size.assert_not_called()
    app._apply_layout_profile.assert_not_called()
    app._install_tmux_bindings.assert_not_called()
    app._sync_attached_slot_projection.assert_not_called()
    app._set_railmux_focus.assert_called_once_with(True, force_border=False)


def test_compact_background_attach_uses_full_target_then_returns_page():
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    app._workspace.presentation = WorkspacePresentation.COMPACT
    app._workspace.compact_page = WorkspacePage.SIDEBAR
    slot = app._workspace.secondary
    slot.pane_id = "%3"
    app._double_focus_visual_pending = False
    app._paint_slot_active_tmux_target = MagicMock()
    app._redraw_focus_state_now = MagicMock()
    app._check_agent_slot_size = MagicMock()
    app._set_active_tmux_target = MagicMock()
    app._set_railmux_focus = MagicMock()
    app._install_tmux_bindings = MagicMock()
    app._modes = MagicMock()
    app._modes.return_value.for_tmux_name.return_value = MagicMock(key="codex")
    app._running = {
        "new-session": SimpleNamespace(
            key="new-session",
            tmux_name="cx-live",
            is_placeholder=False,
            is_legacy=False,
        ),
    }
    selected: list[WorkspacePage] = []
    app._select_workspace_page = MagicMock(
        side_effect=lambda page: selected.append(page) or True)
    transport = MagicMock()

    def attach(target_slot, _tmux_name):
        assert selected == [WorkspacePage.SECONDARY]
        target_slot.pane_id = "%3"
        return AttachOutcome(True, DisplayTransportKind.SWAP)

    transport.attach.side_effect = attach
    app._display_transport_manager = transport

    assert app._attach_agent_slot(slot, "cx-live", steal_focus=False)

    assert selected == [WorkspacePage.SECONDARY, WorkspacePage.SIDEBAR]
    assert slot.agent_tmux_name == "cx-live"


def test_compact_first_attach_creates_inert_target_before_selecting_it(
    monkeypatch,
):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.presentation = WorkspacePresentation.COMPACT
    app._workspace.compact_page = WorkspacePage.SIDEBAR
    slot = app._workspace.primary
    app._double_focus_visual_pending = False
    app._paint_slot_active_tmux_target = MagicMock()
    app._redraw_focus_state_now = MagicMock()
    app._check_agent_slot_size = MagicMock()
    app._set_active_tmux_target = MagicMock()
    app._set_railmux_focus = MagicMock()
    app._schedule_scroll_acceleration = MagicMock()
    app._install_tmux_bindings = MagicMock()
    app._modes = MagicMock()
    app._modes.return_value.for_tmux_name.return_value = MagicMock(key="claude")
    app._running = {
        "new-session": SimpleNamespace(
            key="new-session",
            tmux_name="cc-live",
            is_placeholder=False,
            is_legacy=False,
        ),
    }
    events: list[str] = []
    transport = MagicMock()

    def create_primary():
        events.append("create")
        slot.pane_id = "%2"
        return True

    transport.create_primary.side_effect = create_primary

    def select_page(page):
        events.append(f"select:{page.value}")
        assert slot.pane_id == "%2"
        return True

    app._select_workspace_page = MagicMock(side_effect=select_page)

    def attach(target_slot, _tmux_name):
        events.append("attach")
        assert target_slot.pane_id == "%2"
        return AttachOutcome(True, DisplayTransportKind.SWAP)

    transport.attach.side_effect = attach
    app._display_transport_manager = transport
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.pane_alive", lambda _pane: True)

    assert app._attach_agent_slot(slot, "cc-live", steal_focus=True)
    assert events[:3] == ["create", "select:primary", "attach"]


def test_failed_attach_restores_prior_active_target_immediately():
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    slot = app._workspace.primary
    slot.active_session_id = "old-session"
    slot.agent_tmux_name = "cc-old"
    app._running = {
        "old-session": SimpleNamespace(
            key="old-session", tmux_name="cc-old", is_placeholder=False),
        "new-session": SimpleNamespace(
            key="new-session", tmux_name="cc-new", is_placeholder=False),
    }
    app._sessions_pane = MagicMock()
    app._running_pane = MagicMock()
    app._redraw_focus_state_now = MagicMock()
    app._display_transport_manager = MagicMock()
    app._display_transport_manager.attach.return_value = AttachOutcome(
        False, DisplayTransportKind.SWAP, "test failure")

    assert not app._attach_agent_slot(slot, "cc-new", steal_focus=False)

    assert [item.args for item in
            app._sessions_pane.set_active_session.call_args_list] == [
        ("new-session",), ("old-session",)]
    assert [item.args for item in
            app._running_pane.set_active.call_args_list] == [
        ("cc-new",), ("cc-old",)]
    assert app._redraw_focus_state_now.call_count == 2
    assert slot.active_session_id == "old-session"
    assert slot.agent_tmux_name == "cc-old"
    assert app._last_attach_failure_reason == "test failure"
    assert app._status_text == "Could not attach agent pane: test failure."


def test_failed_attach_reconciles_transport_recovery_target():
    """Do not roll back the UI if swap retained the new real pane."""
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    slot = app._workspace.primary
    slot.active_session_id = "old-session"
    slot.agent_tmux_name = "cc-old"
    app._running = {
        "old-session": SimpleNamespace(
            key="old-session", tmux_name="cc-old", is_placeholder=False,
            project=None),
        "new-session": SimpleNamespace(
            key="new-session", tmux_name="cc-new", is_placeholder=False,
            project=None),
    }
    app._sessions_pane = MagicMock()
    app._running_pane = MagicMock()
    app._redraw_focus_state_now = MagicMock()
    app._modes = MagicMock()
    app._modes.return_value.for_tmux_name.return_value = MagicMock(key="claude")
    app._display_transport_manager = MagicMock()

    def fail_after_movement(target_slot, _tmux_name):
        target_slot.agent_tmux_name = "cc-new"
        return AttachOutcome(False, DisplayTransportKind.SWAP, "uncertain")

    app._display_transport_manager.attach.side_effect = fail_after_movement

    assert not app._attach_agent_slot(slot, "cc-new", steal_focus=False)

    assert slot.active_session_id == "new-session"
    assert slot.agent_tmux_name == "cc-new"
    assert app._sessions_pane.set_active_session.call_args.args == (
        "new-session",)
    assert app._running_pane.set_active.call_args.args == ("cc-new",)


def test_failed_delayed_focus_restores_sidebar(monkeypatch):
    app = App.__new__(App)
    app._frame = _FocusAwareFrame(urwid.SolidFill(" "))
    app._railmux_has_focus = True
    app._divider_active = None
    app._right_pane_id = "%2"
    app._double_focus_alarm = None
    app._double_focus_visual_pending = False
    app._loop = MagicMock()
    app._loop.set_alarm_in.return_value = object()
    set_border = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_border_styles", set_border)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.select_pane", MagicMock(return_value=False))

    app._schedule_right_pane_focus_after_double()
    callback = app._loop.set_alarm_in.call_args.args[1]
    callback(app._loop, None)

    assert app._double_focus_visual_pending is False
    assert app._railmux_has_focus is True
    assert app._frame._window_active is True
    assert set_border.call_args_list[-1].args == (
        "fg=colour240", "fg=colour240")


def test_new_session_intent_cancels_pending_double_focus(monkeypatch):
    app = App.__new__(App)
    app._frame = _FocusAwareFrame(urwid.SolidFill(" "))
    app._frame.set_window_active(False)
    app._railmux_has_focus = False
    app._divider_active = True
    app._loop = MagicMock()
    alarm = object()
    app._double_focus_alarm = alarm
    app._double_focus_visual_pending = True
    app._in_history_mode = True
    app._restore_state = object()
    app._launch_resume = MagicMock()
    session = MagicMock()
    set_border = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_window_border_styles", set_border)

    app._on_session_select(session, steal_focus=False)

    app._loop.remove_alarm.assert_called_once_with(alarm)
    assert app._double_focus_alarm is None
    assert app._double_focus_visual_pending is False
    assert app._railmux_has_focus is True
    assert app._frame._window_active is True
    set_border.assert_called_once_with("fg=colour240", "fg=colour240")
    app._loop.draw_screen.assert_called_once_with()
    app._launch_resume.assert_called_once_with(
        session, steal_focus=False, from_double=False)


def test_double_click_intent_reaches_resume_without_cancelling_focus():
    """The Sessions -> resume hop must preserve double-click intent.

    A live-session resume is redirected through ``_on_running_select``. Losing
    the marker at this first hop makes that redirect cancel the delayed
    right-pane focus alarm and visibly bounce focus back to Sessions.
    """
    app = App.__new__(App)
    app._in_history_mode = True
    app._restore_state = object()
    app._cancel_pending_double_focus = MagicMock()
    app._launch_resume = MagicMock()
    session = MagicMock()

    app._on_session_select(
        session, steal_focus=False, from_double=True)

    app._cancel_pending_double_focus.assert_not_called()
    app._launch_resume.assert_called_once_with(
        session, steal_focus=False, from_double=True)



def test_pending_restore_consumes_state_after_success(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    monkeypatch.setattr(
        App, "_state_path", staticmethod(lambda: state_path))
    app = App.__new__(App)
    app._pending_restore_state = {"right_kind": "empty"}
    app._loaded_restart_state_path = state_path
    app._loaded_restart_source = None
    app._restore_right_pane = MagicMock()

    app._restore_pending_right_pane(None, None)

    app._restore_right_pane.assert_called_once_with({"right_kind": "empty"})
    assert app._pending_restore_state is None
    assert not state_path.exists()


def test_pending_restore_keeps_state_when_restore_raises(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    monkeypatch.setattr(
        App, "_state_path", staticmethod(lambda: state_path))
    app = App.__new__(App)
    app._pending_restore_state = {"right_kind": "claude"}
    app._restore_right_pane = MagicMock(side_effect=RuntimeError("failed"))

    with pytest.raises(RuntimeError, match="failed"):
        app._restore_pending_right_pane(None, None)

    assert state_path.exists()
    assert app._pending_restore_state is None


def test_scroll_configuration_is_coalesced_and_deferred():
    app = App.__new__(App)
    app._loop = MagicMock()
    app._pending_scroll_session = None
    app._scroll_alarm_pending = False
    app._right_pane_claude = "cc-b"
    app._configure_scroll_acceleration = MagicMock()

    app._schedule_scroll_acceleration("cc-a")
    app._schedule_scroll_acceleration("cc-b")

    app._loop.set_alarm_in.assert_called_once_with(
        0.05, app._apply_pending_scroll_acceleration)
    app._configure_scroll_acceleration.assert_not_called()

    app._apply_pending_scroll_acceleration(None, None)

    app._configure_scroll_acceleration.assert_called_once_with("cc-b")
    assert app._pending_scroll_session is None
    assert app._scroll_alarm_pending is False


def test_pending_project_load_is_deferred_and_stale_safe():
    project = Project(
        Path("/tmp/p"),
        "-tmp-p",
        Path("/tmp/meta"),
        1,
        1.0,
    )
    app = App.__new__(App)
    app._pending_project = project
    app._selected_project = project
    app._on_project_select = MagicMock()

    app._load_pending_project(None, None)

    app._on_project_select.assert_called_once_with(project)
    assert app._pending_project is None

    app._pending_project = project
    app._selected_project = None

    app._load_pending_project(None, None)
    app._on_project_select.assert_called_once_with(project)
    assert app._pending_project is None


# ── status message wording (points 2 & 3 of the tmux-bar cleanup) ─────────

def test_resume_status_omits_running_count(monkeypatch):
    """Opening a running session shows just the title — the old
    ``(N session(s) running)`` suffix was redundant with the sidebar and is
    gone."""
    app = App.__new__(App)
    app._config = MagicMock()
    app._launch = MagicMock(return_value=True)
    app._set_status = MagicMock()
    monkeypatch.setattr(
        "railmux.ui.app.build_resume_command", lambda **kw: ["claude"])
    claim = MagicMock(session_ids=("id1",))
    monkeypatch.setattr(
        "railmux.ui.app.session_lease.acquire", lambda *_args: claim)

    session = MagicMock()
    session.session_type = "claude"
    session.display_title = "sess-x"
    session.session_id = "id1"
    session.project.real_path = Path("/tmp/p")
    session.project.display_name = "p"

    app._launch_resume(session)

    msg = app._set_status.call_args.args[0]
    assert msg == "→ sess-x"
    assert "running" not in msg
    assert app._launch.call_args.kwargs["lease_claim"] is claim


def test_preview_reports_info_status():
    """Previewing a stopped session's history surfaces an info message in the
    (tmux) status bar — the action used to be silent on success."""
    app = App.__new__(App)
    app._has_less = True
    app._in_history_mode = False
    app._cancel_pending_double_focus = MagicMock()
    app._save_restore_state = MagicMock()
    app._show_transcript = MagicMock(return_value=True)
    app._set_active_target = MagicMock()
    app._set_status = MagicMock()
    app._sessions_pane = MagicMock()

    session = MagicMock()
    session.display_title = "old chat"
    session.session_id = "id2"
    session.jsonl_path = Path("/tmp/id2.jsonl")

    app._on_session_preview(session)

    assert app._in_history_mode is True
    msg = app._set_status.call_args.args[0]
    assert "Previewing" in msg and "old chat" in msg
    app._sessions_pane.set_selected_session.assert_not_called()


def test_preview_targets_explicit_active_secondary_slot():
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    app._workspace.primary.pane_id = "%2"
    app._workspace.secondary.pane_id = "%3"
    app._workspace.set_target(AgentWorkspace.SECONDARY)
    app._running = {}
    app._has_less = True
    app._cancel_pending_double_focus = MagicMock()
    app._save_restore_state = MagicMock()
    app._show_transcript = MagicMock(return_value=True)
    app._set_slot_active_target = MagicMock()
    app._set_status = MagicMock()
    app._show_attention_status = MagicMock(return_value=False)
    app._current_mode_key = MagicMock(return_value="claude")
    session = MagicMock()
    session.display_title = "secondary preview"
    session.session_id = "preview-id"
    session.jsonl_path = Path("/tmp/preview.jsonl")
    session.session_type = "claude"
    session.project.encoded_name = "project"

    app._on_session_preview(session)

    app._save_restore_state.assert_called_once_with(app._workspace.secondary)
    app._show_transcript.assert_called_once_with(
        session.jsonl_path,
        session_type="claude",
        slot=app._workspace.secondary,
    )
    app._set_slot_active_target.assert_called_once_with(
        app._workspace.secondary,
        "preview-id",
        None,
        mode_key="claude",
        project_key="project",
        sync_transcript_source=False,
    )
    assert app._workspace.secondary.in_history_mode is True


def test_codex_preview_resolves_newest_rewind_representative():
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._has_less = True
    app._cancel_pending_double_focus = MagicMock()
    app._save_restore_state = MagicMock()
    app._show_transcript = MagicMock(return_value=True)
    app._set_active_target = MagicMock()
    app._set_status = MagicMock()
    app._show_attention_status = MagicMock(return_value=False)
    app._current_mode_key = MagicMock(return_value="codex")
    root = SimpleNamespace(session_type="codex", session_id="root-id")
    current = SimpleNamespace(
        session_type="codex",
        session_id="current-id",
        jsonl_path=Path("/tmp/current-id.jsonl"),
        display_title="current branch",
        project=SimpleNamespace(encoded_name="project"),
        attention=None,
    )
    app._codex_representative = MagicMock(return_value=current)

    app._on_session_preview(root)

    app._show_transcript.assert_called_once_with(
        current.jsonl_path, session_type="codex")
    app._set_active_target.assert_called_once_with(
        "current-id",
        None,
        mode_key="codex",
        project_key="project",
        sync_transcript_source=False,
    )


def test_repeat_preview_reuses_owned_pane_without_revalidating_chrome(
    monkeypatch, tmp_path,
):
    """The stopped-preview hot path launches only the replacement viewer."""
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    slot = app._workspace.primary
    slot.pane_id = "%2"
    slot.in_history_mode = True
    slot.agent_tmux_name = None
    slot.swap_state = None
    app._less_mouse_flag = ""
    app._display_transport = MagicMock()
    app._sync_slot_transcript_source = MagicMock()
    app._set_divider_active = MagicMock()
    app._install_tmux_bindings = MagicMock()
    app._set_status = MagicMock()
    commands = []
    monkeypatch.setattr(
        tmux_ctl,
        "pane_alive",
        lambda _pane: pytest.fail("repeat preview must not list every pane"),
    )
    monkeypatch.setattr(
        tmux_ctl,
        "respawn_pane",
        lambda pane, command: commands.append((pane, command)) or True,
    )

    assert app._show_transcript(
        tmp_path / "next.jsonl", session_type="claude")

    assert len(commands) == 1
    assert commands[0][0] == "%2"
    app._display_transport.assert_not_called()
    app._sync_slot_transcript_source.assert_not_called()
    app._set_divider_active.assert_not_called()
    app._install_tmux_bindings.assert_not_called()


def test_repeat_preview_respawn_failure_keeps_safe_existing_state(
    monkeypatch, tmp_path,
):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    slot = app._workspace.primary
    slot.pane_id = "%2"
    slot.in_history_mode = True
    slot.agent_tmux_name = None
    slot.swap_state = None
    app._less_mouse_flag = ""
    app._display_transport = MagicMock()
    app._sync_slot_transcript_source = MagicMock()
    app._set_divider_active = MagicMock()
    app._install_tmux_bindings = MagicMock()
    app._set_status = MagicMock()
    monkeypatch.setattr(tmux_ctl, "respawn_pane", lambda *_args: False)

    assert not app._show_transcript(
        tmp_path / "missing.jsonl", session_type="claude")

    assert slot.pane_id == "%2"
    assert slot.in_history_mode is True
    app._display_transport.assert_not_called()
    app._sync_slot_transcript_source.assert_not_called()
    app._set_status.assert_called_once_with(
        "failed to respawn right pane for transcript")


def test_repeat_preview_with_inconsistent_transport_uses_full_safety_path(
    monkeypatch, tmp_path,
):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    slot = app._workspace.primary
    slot.pane_id = "%2"
    slot.in_history_mode = True
    slot.agent_tmux_name = None
    slot.swap_state = None
    slot.transport_kind = DisplayTransportKind.SWAP
    app._less_mouse_flag = ""
    transport = MagicMock()
    transport.prepare_preview.return_value = True
    app._display_transport = MagicMock(return_value=transport)
    app._sync_slot_transcript_source = MagicMock()
    app._set_divider_active = MagicMock()
    app._install_tmux_bindings = MagicMock()
    app._set_status = MagicMock()
    monkeypatch.setattr(tmux_ctl, "respawn_pane", lambda *_args: True)

    assert app._show_transcript(
        tmp_path / "next.jsonl", session_type="claude")

    transport.prepare_preview.assert_called_once_with(slot)
    app._sync_slot_transcript_source.assert_called_once_with(slot, None, None)
    app._set_divider_active.assert_called_once_with(False, force=True)
    app._install_tmux_bindings.assert_called_once_with()


def test_private_history_restore_key_routes_exact_slot_before_modal_checks():
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._restore_history_slot = MagicMock()

    app._on_input("f24")

    app._restore_history_slot.assert_called_once_with(app._workspace.secondary)


def test_live_transcript_viewer_exit_signals_exact_slot_restore(
    monkeypatch, tmp_path,
):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    slot = app._workspace.primary
    slot.pane_id = "%2"
    slot.agent_tmux_name = "cx-live"
    slot.restore_state = SimpleNamespace(kind="agent", tmux_name="cx-live")
    app._less_mouse_flag = ""
    app._railmux_pane_id = "%1"
    app._railmux_has_focus = True
    transport = MagicMock()
    transport.prepare_preview.return_value = True
    app._display_transport = MagicMock(return_value=transport)
    app._sync_slot_transcript_source = MagicMock()
    app._set_divider_active = MagicMock()
    app._install_tmux_bindings = MagicMock()
    commands = []
    monkeypatch.setattr(tmux_ctl, "pane_alive", lambda _pane: True)
    monkeypatch.setattr(
        tmux_ctl, "respawn_pane",
        lambda pane, command: commands.append((pane, command)) or True,
    )

    assert app._show_transcript(
        tmp_path / "session.jsonl", session_type="codex")

    assert commands[0][0] == "%2"
    assert "railmux.preview_pager" in commands[0][1]
    assert "tmux send-keys -l -t %1" in commands[0][1]
    assert "\x1b[42~" in commands[0][1]
    assert slot.agent_tmux_name is None
    transport.prepare_preview.assert_called_once_with(slot)
    app._sync_slot_transcript_source.assert_called_once_with(slot, None, None)
    app._set_divider_active.assert_called_once_with(False, force=True)
    app._install_tmux_bindings.assert_called_once_with()


def test_closed_live_history_restores_saved_agent_to_same_slot(monkeypatch):
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    slot = app._workspace.secondary
    slot.in_history_mode = True
    slot.restore_state = SimpleNamespace(kind="agent", tmux_name="cx-live")
    running = _Running("id", "cx-live", "Live", status="busy")
    app._by_tmux = MagicMock(return_value=running)
    app._on_running_select = MagicMock()
    monkeypatch.setattr(app, "_agent_session_alive", lambda _name: True)

    app._restore_history_slot(slot)

    entry = app._on_running_select.call_args.args[0]
    assert entry.tmux_name == "cx-live"
    app._on_running_select.assert_called_once_with(
        entry, steal_focus=False, slot=slot)


def test_session_context_menu_preview_uses_space_action():
    app = App.__new__(App)
    app._railmux_pane_id = None
    app._sessions_pane = MagicMock()
    app._running = {
        "preview-id": _Running(
            key="preview-id",
            tmux_name="cc-preview-id",
            label="Review layout policy",
        )
    }
    app._favorites = MagicMock()
    app._favorites.get_ids.return_value = set()
    app._close_modal = MagicMock()
    app._show_overlay = MagicMock()
    app._on_session_row_preview = MagicMock()
    app._on_session_preview = MagicMock()
    app._copy_session_title = MagicMock()
    session = MagicMock()
    session.session_id = "preview-id"
    session.display_title = "Review layout policy"

    app._open_session_context_menu(session)

    menu = app._show_overlay.call_args.args[0]
    labels = [row._wrapped_widget.base_widget.text for row in menu._walker]
    assert any("Preview" in label and "␣" in label for label in labels)
    preview_row = next(
        row for row in menu._walker
        if "Preview" in row._wrapped_widget.base_widget.text)
    preview_row._on_click()
    app._on_session_preview.assert_called_once_with(session)
    app._on_session_row_preview.assert_not_called()


def test_session_context_menu_copy_title_uses_provider_title():
    app = App.__new__(App)
    app._railmux_pane_id = None
    app._sessions_pane = MagicMock()
    app._running = {}
    app._favorites = MagicMock()
    app._favorites.get_ids.return_value = set()
    app._close_modal = MagicMock()
    app._show_overlay = MagicMock()
    app._copy_session_title = MagicMock()
    session = MagicMock()
    session.session_id = "preview-id"
    session.display_title = "Review layout policy"

    app._open_session_context_menu(session)

    menu = app._show_overlay.call_args.args[0]
    copy_row = next(
        row for row in menu._walker
        if "Copy title" in row._wrapped_widget.base_widget.text
    )
    copy_row._on_click()
    app._copy_session_title.assert_called_once_with("Review layout policy")


def test_project_context_menu_has_only_requested_actions(tmp_path):
    app = App.__new__(App)
    project = MagicMock()
    project.real_path = tmp_path / "project"
    project.encoded_name = "-tmp-project"
    app._railmux_pane_id = None
    app._projects_pane = MagicMock()
    app._project_favorites = MagicMock()
    app._project_favorites.is_favorite.return_value = False
    app._close_modal = MagicMock()
    app._show_overlay = MagicMock()

    app._open_project_context_menu(project)

    app._projects_pane.set_context_selected.assert_called_once_with(
        project.encoded_name)
    menu = app._show_overlay.call_args.args[0]
    labels = [row._wrapped_widget.base_widget.text for row in menu._walker]
    assert [label.split()[0] for label in labels] == [
        "Copy", "Star", "Info", "Term",
    ]
    assert all(
        forbidden not in " ".join(labels)
        for forbidden in ("Open", "Rename", "Kill", "Delete", "Preview")
    )


def test_project_context_menu_unstars_existing_favorite(tmp_path):
    app = App.__new__(App)
    project = MagicMock()
    project.real_path = tmp_path / "project"
    project.encoded_name = "-tmp-project"
    project.display_name = "project"
    app._railmux_pane_id = None
    app._projects_pane = MagicMock()
    app._project_favorites = MagicMock()
    app._project_favorites.is_favorite.side_effect = [True, True]
    app._project_favorites.get_paths.return_value = set()
    app._close_modal = MagicMock()
    app._show_overlay = MagicMock()
    app._set_status = MagicMock()

    app._open_project_context_menu(project)
    menu = app._show_overlay.call_args.args[0]
    unstar = next(
        row for row in menu._walker
        if "Unstar" in row._wrapped_widget.base_widget.text)
    unstar._on_click()

    app._project_favorites.set.assert_called_once_with(project.real_path, False)
    app._projects_pane.set_favorite_paths.assert_called_once_with(set())


def test_copy_project_path_uses_absolute_path(
    monkeypatch, tmp_path,
):
    app = App.__new__(App)
    app._set_status = MagicMock()
    project = MagicMock(real_path=tmp_path / "project")
    copied = []
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.copy_to_clipboard",
        lambda value: copied.append(value) or True,
    )

    app._copy_project_path(project)

    assert copied == [str(tmp_path / "project")]
    app._set_status.assert_called_once_with(
        f"Copied path: {tmp_path / 'project'}", "success")


def test_copy_project_path_reports_clipboard_failure_without_other_action(
    monkeypatch, tmp_path,
):
    app = App.__new__(App)
    app._set_status = MagicMock()
    project = MagicMock(real_path=tmp_path / "project")
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.copy_to_clipboard", lambda _value: False)

    app._copy_project_path(project)

    app._set_status.assert_called_once_with(
        "Could not copy path; terminal clipboard is unavailable", "warn")


def test_legacy_running_menu_copies_provider_title_without_project_prefix(
    tmp_path,
):
    app = App.__new__(App)
    project = MagicMock()
    project.real_path = tmp_path
    running = _Running(
        key="legacy-id",
        tmux_name="cc-legacy-id",
        label="railmux/Review layout policy",
        project=project,
        legacy_server=MagicMock(),
    )
    app._running = {"legacy-id": running}
    app._running_pane = MagicMock()
    app._railmux_pane_id = None
    app._find_session_meta = MagicMock(return_value=SimpleNamespace(
        display_title="Review layout policy"
    ))
    app._close_modal = MagicMock()
    app._show_overlay = MagicMock()
    app._copy_session_title = MagicMock()

    app._on_running_context_menu(RunningEntry(
        tmux_name="cc-legacy-id",
        label=running.label,
    ))

    menu = app._show_overlay.call_args.args[0]
    copy_row = next(
        row for row in menu._walker
        if "Copy title" in row._wrapped_widget.base_widget.text
    )
    copy_row._on_click()
    app._copy_session_title.assert_called_once_with("Review layout policy")


def test_space_on_running_row_previews_canonical_history():
    app = App.__new__(App)
    entry = RunningEntry(tmux_name="cx-live", label="project/session")
    app._focused_pane_menu_target = MagicMock(
        return_value=("running", entry, entry.label))
    app._preview_running_history = MagicMock()

    app._preview_focused_target()

    app._preview_running_history.assert_called_once_with(entry)


def test_space_on_session_row_previews_even_when_live():
    app = App.__new__(App)
    session = MagicMock(spec=SessionMeta)
    session.display_title = "session"
    app._focused_pane_menu_target = MagicMock(
        return_value=("session", session, "session"))
    app._on_session_preview = MagicMock()

    app._preview_focused_target()

    app._on_session_preview.assert_called_once_with(session)


def test_preview_failure_sets_no_success_status():
    """When the transcript pane can't be created, no 'Previewing' info is
    shown (the failure path inside _show_transcript reports its own error)."""
    app = App.__new__(App)
    app._has_less = True
    app._in_history_mode = False
    app._cancel_pending_double_focus = MagicMock()
    app._save_restore_state = MagicMock()
    app._show_transcript = MagicMock(return_value=False)
    app._set_active_target = MagicMock()
    app._set_status = MagicMock()

    session = MagicMock()
    session.jsonl_path = Path("/tmp/id3.jsonl")

    app._on_session_preview(session)

    assert app._in_history_mode is False
    app._set_status.assert_not_called()
    app._set_active_target.assert_not_called()
