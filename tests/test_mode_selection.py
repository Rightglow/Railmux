"""Per-provider project selection across mode switches and refresh ticks."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from railmux.config import Config
from railmux.modes import CLAUDE_MODE, CODEX_MODE
from railmux.models import Project
from railmux.ui.app import App


def _project(name: str, *, codex_only: bool = False) -> Project:
    return Project(
        real_path=Path(f"/tmp/{name}"),
        encoded_name=f"-tmp-{name}",
        claude_dir=Path() if codex_only else Path(f"/meta/{name}"),
        session_count=1,
        last_activity_ts=1.0,
    )


def _mode_app(monkeypatch, claude_projects: list[Project],
              codex_projects: list[Project]) -> App:
    """Bare App with real toggle/refresh selection logic and mocked I/O."""
    app = App.__new__(App)
    app._codex_mode = False
    app._config = Config()
    app._selected_project = None
    app._mode_view_states = {}
    app._project_snapshot = claude_projects
    app._project_snapshot_at = time.monotonic()
    app._codex_project_filter = {
        project.real_path: project.session_count for project in codex_projects
    }
    app._running = {}
    app._right_pane_id = None
    app._right_pane_claude = None
    app._in_history_mode = False
    app._tmux_error_bar = False
    app._mode_refresh_thread = None
    app._mode_refresh_result = None
    app._projects_pane = MagicMock()
    app._sessions_pane = MagicMock()
    app._running_pane = MagicMock()
    app._running_pane.filter_text = ""
    app._favorites = MagicMock()
    app._favorites.get_ids.return_value = set()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = app._codex_project_filter
    app._scroll_manager = MagicMock()
    app._hint_bar = MagicMock()
    app._pane_sessions = MagicMock(return_value=[])
    app._resolve_placeholders = MagicMock()
    app._update_running_pane = MagicMock()
    app._update_status = MagicMock()
    app._help_context = MagicMock(return_value="projects")
    app._cancel_pending_double_focus = MagicMock()
    app._set_status = MagicMock()
    app._apply_tmux_bar = MagicMock()
    app._maybe_prompt_codex_yolo = MagicMock()
    app._schedule_mode_data_refresh = MagicMock()
    app._mode_refresh_pending = MagicMock(return_value=False)
    app._consume_mode_refresh = MagicMock(return_value=False)
    monkeypatch.setattr("railmux.ui.app.shutil.which", lambda _binary: "/bin/agent")

    monkeypatch.setattr(
        app,
        "_visible_projects",
        lambda **_kwargs: codex_projects if app._codex_mode else claude_projects,
    )
    return app


def test_missing_provider_binary_warning_never_echoes_configured_path(
    monkeypatch,
):
    app = App.__new__(App)
    secret_path = "/private/company/user/claude-wrapper"
    app._config = Config(claude_binary=secret_path)
    app._set_status = MagicMock()
    monkeypatch.setattr("railmux.ui.app.shutil.which", lambda _binary: None)

    assert app._warn_missing_mode_binary(CLAUDE_MODE) is True

    message, level = app._set_status.call_args.args
    assert level == "warn"
    assert "Claude Code executable not found" in message
    assert secret_path not in message


def test_cold_codex_index_sets_and_clears_sidebar_loading(monkeypatch):
    class _Index:
        def __init__(self):
            self.has_snapshot = False
            self.is_unavailable = False

    monkeypatch.setattr("railmux.ui.app.BackgroundCodexIndex", _Index)
    app = App.__new__(App)
    app._codex_index = _Index()
    app._projects_pane = MagicMock()
    app._sessions_pane = MagicMock()

    app._set_index_loading(CODEX_MODE)

    app._projects_pane.set_loading.assert_called_once_with(True)
    app._sessions_pane.set_loading.assert_called_once_with(True)

    app._codex_index.has_snapshot = True
    app._set_index_loading(CODEX_MODE)

    app._projects_pane.set_loading.assert_called_with(False)
    app._sessions_pane.set_loading.assert_called_with(False)

    app._codex_index.has_snapshot = False
    app._set_index_loading(CLAUDE_MODE)

    app._projects_pane.set_loading.assert_called_with(False)
    app._sessions_pane.set_loading.assert_called_with(False)


def test_empty_codex_refresh_then_claude_restores_previous_project(monkeypatch):
    claude = _project("claude-a")
    app = _mode_app(monkeypatch, [claude], [])
    app._on_project_select(claude)

    app._toggle_codex_mode()
    assert app._codex_mode is True
    assert app._selected_project is None

    # This tick used to erase the only shared selection permanently.
    app._refresh()
    app._toggle_codex_mode()

    assert app._codex_mode is False
    assert app._selected_project is claude
    assert app._sessions_pane.set_sessions.call_args.args[0] is claude


def test_empty_codex_never_keeps_hidden_actionable_claude_project(monkeypatch):
    claude = _project("claude-a")
    app = _mode_app(monkeypatch, [claude], [])
    app._on_project_select(claude)

    app._toggle_codex_mode()

    assert app._selected_project is None
    assert app._projects_pane.set_selected.call_args.args == (None,)
    assert app._sessions_pane.set_sessions.call_args.args[:2] == (None, [])


def test_each_mode_restores_its_own_project(monkeypatch):
    claude_a = _project("claude-a")
    claude_b = _project("claude-b")
    codex_a = _project("codex-a", codex_only=True)
    codex_b = _project("codex-b", codex_only=True)
    app = _mode_app(
        monkeypatch, [claude_a, claude_b], [codex_a, codex_b])
    app._on_project_select(claude_b)

    app._toggle_codex_mode()
    app._on_project_select(codex_b)
    app._toggle_codex_mode()
    assert app._selected_project is claude_b

    app._toggle_codex_mode()
    assert app._selected_project is codex_b


def test_each_mode_applies_its_own_running_filter(monkeypatch):
    claude = _project("claude")
    codex = _project("codex", codex_only=True)
    app = _mode_app(monkeypatch, [claude], [codex])
    app._mode_view_states[CLAUDE_MODE.key] = app._current_mode_view_state()
    app._running_pane.filter_text = "project:claude"
    app._mode_view_states[CODEX_MODE.key] = type(
        app._mode_view_states[CLAUDE_MODE.key])(
            running_filter="project:codex")

    app._toggle_codex_mode()
    app._running_pane.set_filter.assert_called_with(
        "project:codex", capture_focus=False)
    app._running_pane.filter_text = "project:codex"

    app._toggle_codex_mode()
    app._running_pane.set_filter.assert_called_with(
        "project:claude", capture_focus=False)


def test_deleted_remembered_project_falls_back_to_visible_project(monkeypatch):
    deleted = _project("deleted")
    fallback = _project("fallback")
    codex = _project("codex", codex_only=True)
    claude_projects = [deleted, fallback]
    app = _mode_app(monkeypatch, claude_projects, [codex])
    app._on_project_select(deleted)
    app._toggle_codex_mode()

    claude_projects.remove(deleted)
    app._toggle_codex_mode()

    assert app._selected_project is fallback
    selected_projects = [call.args[0] for call in app._pane_sessions.call_args_list]
    assert deleted not in selected_projects[1:]


def test_foreign_pane_focus_only_selects_a_visible_project(monkeypatch):
    from railmux.ui.app import _Running

    claude = _project("claude-a")
    shared = _project("shared")
    foreign = _project("foreign", codex_only=True)
    app = _mode_app(monkeypatch, [claude, shared], [])
    app._on_project_select(claude)
    running = _Running("other", "cx-other", "codex/focus", foreign,
                       session_type="codex")
    app._running[running.key] = running

    app._sync_sidebar_to_agent_project(running.tmux_name)
    assert app._selected_project is claude

    running.project = _project("shared", codex_only=True)
    app._sync_sidebar_to_agent_project(running.tmux_name)
    assert app._selected_project is shared


def test_picker_and_restore_use_configured_cli_availability(monkeypatch):
    app = _mode_app(monkeypatch, [], [])
    app._config = Config(
        claude_binary="/missing/claude", codex_binary="/custom/codex")
    available = {"/custom/codex"}
    monkeypatch.setattr(
        "railmux.ui.app.shutil.which", lambda name: name if name in available else None)
    app._mode_availability = app._probe_mode_availability()
    assert app._mode_key_from_state({"mode": "claude"}) == "codex"
    assert app._right_mode_from_state({"mode": "claude"}) == CLAUDE_MODE

    app._active_mode_key = "codex"
    assert [label for label, choose in app._mode_menu_entries() if choose] == ["● Codex"]

    available.add("devin")
    app._mode_availability = app._probe_mode_availability()
    assert app._mode_key_from_state({"mode": "devin"}) == "devin"
    app._loop = None
    app._close_modal = MagicMock()
    app._show_overlay = MagicMock()
    app._on_input("m")
    assert app._current_mode_key() == "codex"
    menu = app._show_overlay.call_args.args[0]
    menu.keypress((39, 5), "down")
    menu.keypress((39, 5), "enter")
    assert app._current_mode_key() == "devin"

    app._config = Config(
        claude_binary="/missing/claude", codex_binary="/custom/codex",
        hidden_modes=("devin",))
    assert app._mode_key_from_state({"mode": "devin"}) == "codex"
    menu = app._mode_menu_entries()
    assert menu[-1] == ("● Devin · Hidden", None)
    assert [label.strip() for label, choose in menu if choose] == ["Codex"]


def test_mode_menu_can_reopen_a_hidden_provider_with_running_sessions(monkeypatch):
    from railmux.ui.app import _Running

    app = _mode_app(monkeypatch, [], [])
    app._config = Config(hidden_modes=("devin",))
    app._mode_availability = frozenset({"codex", "devin"})
    monkeypatch.setattr(app, "_probe_mode_availability", lambda: app._mode_availability)
    app._active_mode_key = "codex"
    app._last_available_mode_key = "codex"
    app._running["live"] = _Running(
        "live", "dv-live", "live Devin", None, session_type="devin")
    app._close_modal = MagicMock()
    app._show_overlay = MagicMock()
    app._open_mode_menu()
    menu = app._show_overlay.call_args.args[0]
    rendered = b"\n".join(menu.render((40, 5)).text)
    assert b"Claude Code" in rendered and b"Unavailable" in rendered
    assert b"Devin" in rendered and b"Running" in rendered and b"Hidden" in rendered
    menu.keypress((40, 5), "down")
    menu.keypress((40, 5), "enter")
    assert app._current_mode_key() == "devin"
    assert app._view_state_data()["mode"] == "codex"
    assert app._running_pane.set_running.call_args.args[0][0].tmux_name == "dv-live"


@pytest.mark.parametrize("compact", [False, True])
def test_status_mode_click_focuses_the_sidebar_before_showing_picker(monkeypatch, compact):
    from railmux import tmux_ctl
    from railmux.ui.workspace import WorkspacePage, WorkspacePresentation

    app = _mode_app(monkeypatch, [], [])
    app._active_mode_key = "codex"
    app._railmux_pane_id = "%1"
    app._frame = object()
    app._loop = MagicMock(widget=app._frame)
    app._probe_mode_availability = lambda: frozenset({"codex", "devin"})
    app._agent_workspace().presentation = (
        WorkspacePresentation.COMPACT if compact else WorkspacePresentation.WIDE)
    events = []
    monkeypatch.setattr(tmux_ctl, "active_pane_id", lambda _owner: "%2")
    monkeypatch.setattr(tmux_ctl, "select_pane", lambda pane: events.append(pane) or True)
    app._select_workspace_page = lambda page: events.append(page) or True
    app._set_railmux_focus = lambda active: events.append(active)
    app._show_overlay = lambda modal, **_kwargs: events.append(modal)

    app._on_input("f5")

    assert events[:-1] == ([WorkspacePage.SIDEBAR] if compact else ["%1", True])
    text = b"\n".join(events[-1].render((39, 5)).text)
    assert b"Mode" in text and b"Codex" in text and b"Devin" in text
    assert app._current_mode_key() == "codex"


def test_warm_switch_restores_rendered_row_and_scroll_without_loading_sessions(monkeypatch):
    from railmux.ui.projects_pane import ProjectsPane

    projects = [_project(f"work-{i:02d}") for i in range(20)]
    app = _mode_app(monkeypatch, [], projects)
    app._active_mode_key = "codex"
    app._projects_pane = ProjectsPane(projects, on_select=app._on_project_select, boxed=False)
    app._show_mode_projects(projects)
    app._on_project_select(projects[13])
    app._projects_pane.set_filter("work")
    listbox = app._projects_pane._listbox
    app._projects_pane._restore_focus(projects[13].encoded_name)
    listbox.shift_focus((40, 5), 2)
    before = app._projects_pane.remember_view()
    assert before.row_key == projects[13].encoded_name
    assert before.offset_rows == 2

    app._pane_sessions.reset_mock()
    app._switch_mode("devin")
    assert app._projects_pane.filter_text == ""
    assert app._selected_project is None
    app._switch_mode("codex")
    assert app._selected_project == projects[13]
    assert app._projects_pane.filter_text == "work"
    assert app._projects_pane.remember_view() == before
    app._pane_sessions.assert_not_called()


def test_shared_owner_badge_uses_worker_while_modes_remain_interactive(monkeypatch):
    from threading import Event

    from railmux import session_lease
    from railmux.models import SessionMeta

    app = _mode_app(monkeypatch, [], [])
    session = SessionMeta(
        _project("shared"), "session-id", Path("/dummy"), "session", 0, 0, 0.0,
        session_type="devin",
    )
    started, release = Event(), Event()

    def slow_probe(_root, provider, ids):
        started.set()
        assert release.wait(2)
        return session_lease.LeaseOwner(provider, ids[0], "remote-host", "test")

    monkeypatch.setattr(session_lease, "active_owner", slow_probe)
    try:
        assert app._with_remote_leases([session]) == [session]
        assert started.wait(2)
        app._switch_mode("codex")
        assert app._current_mode_key() == "codex"
        assert app._with_remote_leases([session]) == [session]
    finally:
        release.set()
        app._session_lease_probe_thread.join(2)
    assert app._with_remote_leases([session])[0].remote_owner == "remote-host"
