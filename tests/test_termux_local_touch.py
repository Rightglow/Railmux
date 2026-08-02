"""Native/local Termux prompt-tap authority and restore contracts."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from railmux import tmux_ctl
from railmux.ui.app import App
from railmux.ui.workspace import AgentWorkspace


def _app() -> App:
    app = App.__new__(App)
    app._termux_local_touch = True
    app._restart_identity = SimpleNamespace(window_id="@3")
    app._tmux_binding_manager = SimpleNamespace(termux_tap_available=True)
    app._workspace = AgentWorkspace()
    app._workspace.primary.pane_id = "%7"
    app._workspace.primary.agent_tmux_name = "cx-live"
    app._last_workspace_size = (80, 24)
    app._projected_termux_tap = None
    app._termux_tap_route_cleared = False
    app._termux_keyboard_projection = None
    app._by_tmux = MagicMock(return_value=object())
    app._is_help_session_name = MagicMock(return_value=False)
    return app


def test_local_termux_projects_only_a_live_agent_cursor(monkeypatch):
    app = _app()
    route = tmux_ctl.TermuxTapRoute(
        pane_id="%7",
        window_id="@3",
        cursor_y=21,
        pane_height=24,
        in_mode=False,
        dead=False,
        frozen_by=None,
    )
    publish = MagicMock(return_value=True)
    monkeypatch.setattr(tmux_ctl, "termux_tap_route", lambda _pane: route)
    monkeypatch.setattr(tmux_ctl, "publish_termux_tap_route", publish)

    assert app._sync_termux_tap_route()
    publish.assert_called_once_with("@3", route, (80, 24))
    assert app._projected_termux_tap == ("%7", 21, 24, 80, 24)


def test_local_termux_clears_authority_for_copy_mode(monkeypatch):
    app = _app()
    app._projected_termux_tap = ("%7", 21, 24, 80, 24)
    route = tmux_ctl.TermuxTapRoute(
        pane_id="%7",
        window_id="@3",
        cursor_y=21,
        pane_height=24,
        in_mode=True,
        dead=False,
        frozen_by=None,
    )
    clear = MagicMock(return_value=True)
    monkeypatch.setattr(tmux_ctl, "termux_tap_route", lambda _pane: route)
    monkeypatch.setattr(tmux_ctl, "clear_termux_tap_route", clear)

    assert not app._sync_termux_tap_route()
    clear.assert_called_once_with("@3", expected_pane="%7")
    assert app._projected_termux_tap is None


def test_local_termux_clears_stale_route_after_process_restart(monkeypatch):
    app = _app()
    app._workspace.primary.pane_id = None
    app._workspace.primary.agent_tmux_name = None
    clear = MagicMock(return_value=True)
    monkeypatch.setattr(tmux_ctl, "clear_termux_tap_route", clear)

    assert not app._sync_termux_tap_route()
    clear.assert_called_once_with("@3", expected_pane=None)


def test_local_termux_keyboard_projection_restores_then_reasserts(monkeypatch):
    app = _app()
    armed = "railmux-termux-tap-v1-owner-123:80:24"
    live = {"value": armed}
    restore = MagicMock(return_value=True)
    reassert = MagicMock(return_value=True)
    monkeypatch.setattr(
        tmux_ctl, "termux_tap_armed_value", lambda _window: live["value"])

    def restored(_window, *, expected_armed=None):
        assert expected_armed == armed
        live["value"] = None
        return restore(_window, expected_armed=expected_armed)

    monkeypatch.setattr(tmux_ctl, "restore_termux_tap_mouse", restored)
    monkeypatch.setattr(tmux_ctl, "reassert_termux_tap_mouse", reassert)

    app._maintain_termux_tap_handoff((80, 14))
    restore.assert_called_once_with("@3", expected_armed=armed)
    assert app._termux_keyboard_projection == (80, 24)

    app._maintain_termux_tap_handoff((80, 24))
    reassert.assert_called_once_with("@3")
    assert app._termux_keyboard_projection is None


def test_local_termux_teardown_restores_only_its_armed_window(monkeypatch):
    app = _app()
    app._projected_termux_tap = ("%7", 21, 24, 80, 24)
    armed = "railmux-termux-tap-v1-owner-456:80:24"
    monkeypatch.setattr(
        tmux_ctl, "termux_tap_armed_value", lambda _window: armed)
    restore = MagicMock(return_value=True)
    clear = MagicMock(return_value=True)
    monkeypatch.setattr(tmux_ctl, "restore_termux_tap_mouse", restore)
    monkeypatch.setattr(tmux_ctl, "clear_termux_tap_route", clear)

    app._teardown_termux_tap()

    restore.assert_called_once_with("@3", expected_armed=armed)
    clear.assert_called_once_with("@3", expected_pane="%7")
    assert app._projected_termux_tap is None
