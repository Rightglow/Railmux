from railmux.mux import TmuxBackend
from pathlib import Path
from railmux.ui.app import App


def test_tmux_backend_capabilities_are_named_version_gates(monkeypatch):
    monkeypatch.setattr("railmux.mux.tmux_backend.tmux_ctl.tmux_version", lambda: (3, 4))
    monkeypatch.setattr(
        "railmux.mux.tmux_backend.tmux_ctl.proc_fs_available", lambda: True
    )

    capabilities = TmuxBackend().capabilities

    assert capabilities.status_ranges
    assert capabilities.border_indicators
    assert capabilities.binding_notes
    assert capabilities.grouped_sessions
    assert capabilities.process_correlation


def test_tmux_backend_delegates_without_changing_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "railmux.mux.tmux_backend.tmux_ctl.select_pane",
        lambda pane_id: calls.append(pane_id) or True,
    )

    assert TmuxBackend().select_pane("%42")
    assert calls == ["%42"]


def test_tmux_backend_keeps_existing_raw_screen_flags(monkeypatch):
    screen = object()
    constructor_calls = []
    monkeypatch.setattr(
        "urwid.raw_display.Screen",
        lambda **kwargs: constructor_calls.append(kwargs) or screen,
    )

    assert TmuxBackend().create_ui_screen() is screen
    assert constructor_calls == [{
        "focus_reporting": True,
        "bracketed_paste_mode": True,
    }]


def test_tmux_backend_prepare_launch_preserves_shell_contract():
    command = TmuxBackend().prepare_launch(
        ["/opt/Codex CLI/codex", "-C", "/tmp/work tree"],
        Path("/tmp/work tree"),
        env={"CODEX_HOME": "/tmp/codex home"},
        login_shell=True,
    )

    assert command == App._shellify(
        ["/opt/Codex CLI/codex", "-C", "/tmp/work tree"],
        Path("/tmp/work tree"),
        env={"CODEX_HOME": "/tmp/codex home"},
        login_shell=True,
    )
