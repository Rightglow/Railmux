"""Tests for soft-quit feature: state file, orphan discovery, truncated ID
resolution, QuitConfirmModal s-key, and teardown branching."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
import urwid

from railmux import tmux_server
from railmux.ui.app import App, _Running
from railmux.ui.modals import (
    ExitProgressModal,
    HardQuitConfirmModal,
    QuitConfirmModal,
)


from tests.app_test_harness import (
    _minimal_app,
    isolate_tmux_identity_stamps as isolate_tmux_identity_stamps,
)


pytestmark = pytest.mark.usefixtures("isolate_tmux_identity_stamps")


def test_commit_soft_exit_publishes_intent_before_teardown(monkeypatch):
    app = _minimal_app()
    events = []
    app._save_state = MagicMock(side_effect=lambda **_kwargs: events.append("state"))
    app._publish_managed_restart_handoff = MagicMock(
        side_effect=lambda: events.append("handoff")
    )
    app._begin_exit = MagicMock(side_effect=lambda **_kwargs: events.append("begin"))
    record = MagicMock(side_effect=lambda **_kwargs: events.append("intent") or True)
    monkeypatch.setattr("railmux.ui.app.tmux_health.record_soft_exit", record)

    app._commit_exit(soft=True)

    assert events == ["state", "handoff", "intent", "begin"]
    record.assert_called_once_with(server_pid=123, session_id="$1")
    app._begin_exit.assert_called_once_with(soft=True)


def test_begin_exit_paints_progress_before_synchronous_cleanup():
    app = _minimal_app()
    app._exit_in_progress = False
    app._soft_quit_flag = False
    app._loop = MagicMock()
    app._close_modal = MagicMock()
    events = []
    app._show_overlay = MagicMock(
        side_effect=lambda *_args, **_kwargs: events.append("overlay")
    )
    app._loop.draw_screen.side_effect = lambda: events.append("draw")
    app._teardown_tmux = MagicMock(
        side_effect=lambda **_kwargs: events.append("teardown")
    )

    with pytest.raises(urwid.ExitMainLoop):
        app._begin_exit(soft=False)

    assert events[:3] == ["overlay", "draw", "teardown"]
    assert app._show_overlay.call_args.kwargs == {
        "width": 44,
        "height": 7,
        "fixed_width": True,
        "fixed_height": True,
    }
    app._teardown_tmux.assert_called_once_with(defer_outer=True)


def test_begin_exit_reuses_current_modal_geometry_for_clean_transition():
    app = _minimal_app()
    app._exit_in_progress = False
    app._soft_quit_flag = False
    app._frame = urwid.SolidFill(" ")
    previous = urwid.Overlay(
        urwid.SolidFill("x"),
        app._frame,
        align="center",
        width=56,
        valign="middle",
        height=13,
    )
    app._loop = MagicMock()
    app._loop.widget = previous
    app._close_modal = MagicMock(
        side_effect=lambda: setattr(app._loop, "widget", app._frame)
    )
    app._show_overlay = MagicMock()
    app._teardown_tmux = MagicMock()

    with pytest.raises(urwid.ExitMainLoop):
        app._begin_exit(soft=True)

    assert app._loop.widget is previous
    assert isinstance(previous.top_w, ExitProgressModal)
    assert previous.width_amount == 56
    assert previous.height_amount == 13
    app._show_overlay.assert_not_called()


def test_teardown_phases_are_idempotent():
    app = _minimal_app()
    app._soft_quit_flag = False
    app._auto_launched = True
    app._scroll_manager = MagicMock()
    app._root_wheel_manager = MagicMock()
    app._running = {
        "abc123": _Running(
            key="abc123", tmux_name="cc-abc123", label="test", project=None
        ),
    }
    transport = MagicMock()
    transport.close_all.return_value = True
    app._display_transport_manager = transport

    with patch("railmux.ui.app.tmux_ctl") as tmux:
        tmux.current_session_name.return_value = "railmux"
        app._teardown_tmux()
        app._teardown_tmux()

    transport.close_all.assert_called_once_with()
    app._root_wheel_manager.close.assert_called_once_with()
    assert tmux.kill_session.call_count == 2
    tmux.kill_session.assert_any_call("cc-abc123")
    tmux.kill_session.assert_any_call("railmux")


def test_teardown_hard_quit_publishes_exact_clean_exit_intent(monkeypatch):
    app = _minimal_app()
    app._soft_quit_flag = False
    app._auto_launched = True
    app._scroll_manager = MagicMock()
    app._running = {}
    record = MagicMock(return_value=True)
    clear = MagicMock()
    monkeypatch.setattr("railmux.ui.app.tmux_health.record_clean_exit", record)
    monkeypatch.setattr("railmux.ui.app.tmux_health.clear_clean_exit", clear)

    with patch("railmux.ui.app.tmux_ctl") as tmux:
        tmux.current_session_name.return_value = "railmux"
        tmux.current_session_id.return_value = "$1"
        tmux.kill_session.return_value = True
        app._teardown_tmux()

    record.assert_called_once_with(server_pid=123, session_id="$1")
    clear.assert_not_called()


def test_teardown_clears_clean_exit_intent_when_outer_kill_fails(monkeypatch):
    app = _minimal_app()
    app._soft_quit_flag = False
    app._auto_launched = True
    app._scroll_manager = MagicMock()
    app._running = {}
    record = MagicMock(return_value=True)
    clear = MagicMock()
    clear_windows_exit = MagicMock()
    monkeypatch.setattr("railmux.ui.app.tmux_health.record_clean_exit", record)
    monkeypatch.setattr("railmux.ui.app.tmux_health.clear_clean_exit", clear)
    monkeypatch.setattr(
        "railmux.ui.app.windows_tmux_lifecycle.clear_empty_server_exit",
        clear_windows_exit,
    )

    with patch("railmux.ui.app.tmux_ctl") as tmux:
        tmux.current_session_name.return_value = "railmux"
        tmux.current_session_id.return_value = "$1"
        tmux.kill_session.return_value = False
        app._teardown_tmux()

    clear.assert_called_once_with()
    clear_windows_exit.assert_called_once_with()


def test_teardown_soft_quit_skips_session_kill():
    """With _soft_quit_flag set, cc-* and outer tmux sessions are left alive."""
    app = _minimal_app()
    app._soft_quit_flag = True
    app._right_pane_id = "%5"
    app._auto_launched = False
    app._scroll_manager = MagicMock()
    app._running = {
        "abc123": _Running(
            key="abc123", tmux_name="cc-abc123", label="test", project=None
        ),
    }

    with (
        patch("railmux.ui.app.tmux_ctl") as tmux,
        patch("railmux.display_transport.tmux_ctl", tmux),
    ):
        app._teardown_tmux()

    # Right-pane cleanup still happens.
    tmux.kill_pane.assert_called_once_with("%5")
    # Session kill must NOT be called.
    tmux.kill_session.assert_not_called()


def test_teardown_hard_quit_kills_sessions():
    """Without the flag, cc-* sessions are killed."""
    app = _minimal_app()
    app._soft_quit_flag = False
    app._right_pane_id = None
    app._auto_launched = False
    app._scroll_manager = MagicMock()
    app._running = {
        "abc123": _Running(
            key="abc123", tmux_name="cc-abc123", label="test", project=None
        ),
    }

    with patch("railmux.ui.app.tmux_ctl") as tmux:
        app._teardown_tmux()

    tmux.kill_session.assert_any_call("cc-abc123")


def test_teardown_hard_quit_preserves_legacy_server_sessions():
    """Only an explicit per-row Kill may mutate a legacy server."""
    app = _minimal_app()
    app._soft_quit_flag = False
    app._right_pane_id = None
    app._auto_launched = False
    app._scroll_manager = MagicMock()
    target = tmux_server.TmuxServerTarget("/tmp/default", 44)
    app._running = {
        "current": _Running("current", "cc-current", "current"),
        "legacy": _Running(
            "legacy",
            "cc-old::legacy:44:7",
            "old",
            legacy_server=target,
            legacy_session_id="$7",
        ),
    }

    with patch("railmux.ui.app.tmux_ctl") as tmux:
        app._teardown_tmux()

    tmux.kill_session.assert_called_once_with("cc-current")


def test_teardown_failed_swap_return_degrades_to_soft_quit():
    app = _minimal_app()
    app._soft_quit_flag = False
    app._auto_launched = True
    app._scroll_manager = MagicMock()
    app._running = {
        "abc123": _Running(
            key="abc123", tmux_name="cc-abc123", label="test", project=None
        ),
    }
    transport = MagicMock()
    transport.close_all.return_value = False
    app._display_transport_manager = transport

    with patch("railmux.ui.app.tmux_ctl") as tmux:
        app._teardown_tmux()

    assert app._soft_quit_flag is True
    tmux.kill_session.assert_not_called()


def test_teardown_reverts_every_bar_option(monkeypatch):
    """Every appearance option railmux paints onto the outer bar — plus the
    dynamically set status-right — is reverted with ``set-option -u`` on
    teardown, so the user's tmux config is left clean. The revert runs BEFORE
    the soft-quit early return (the outer session survives soft quit, so a
    leftover would linger)."""
    app = _minimal_app()
    app._soft_quit_flag = True
    app._right_pane_id = None
    app._auto_launched = False
    app._scroll_manager = MagicMock()
    app._tmux_status_enabled = True
    app._tmux_status_session = "railmux"

    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    with patch("railmux.ui.app.tmux_ctl"):
        app._teardown_tmux()

    reverted = {
        argv[5]
        for c in run.call_args_list
        if (argv := c.args[0])[:4] == ["tmux", "set-option", "-u", "-t"]
    }
    expected = (
        {opt for opt, _ in App._TMUX_BAR_OPTIONS}
        | set(App._TMUX_BAR_STYLE_OPTIONS)
        | {"status-right"}
    )
    assert reverted == expected
    # Regression guard: the noisy window list and the unified bar style are
    # among what we set — and therefore must be among what we revert.
    assert {
        "window-status-format",
        "window-status-current-format",
        "status-style",
        "status-left",
    } <= reverted


def test_run_defers_saved_agent_focus_and_reverts_bar_if_setup_raises(monkeypatch):
    """A pre-loop failure leaves the startup status hidden and tears down."""
    import railmux.ui.app as app_mod

    app = _minimal_app()
    app._pending_project = None
    app._pending_restore_state = None
    app._config = MagicMock(poll_interval_ms=500)
    app._frame = MagicMock()
    app._hint_bar = MagicMock()
    app._set_railmux_focus = MagicMock()
    app._defer_startup_sidebar_focus_visual = True
    teardown = MagicMock()
    app._teardown_tmux = teardown

    monkeypatch.setattr(app_mod.tmux_ctl, "in_tmux", lambda: True)
    monkeypatch.setattr(app_mod.tmux_ctl, "current_session_name", lambda: "railmux")
    monkeypatch.setattr(app_mod.tmux_ctl, "enable_clipboard_passthrough", lambda: None)
    monkeypatch.setattr(app_mod.tmux_ctl, "current_pane_id", lambda: "%0")
    monkeypatch.setattr(
        app_mod.tmux_ctl, "use_smallest_window_size", lambda _pane: True
    )
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    # Screen construction blows up before the managed status bar is revealed.
    monkeypatch.setattr(
        "urwid.raw_display.Screen", MagicMock(side_effect=RuntimeError("boom"))
    )

    with pytest.raises(RuntimeError, match="boom"):
        app.run()

    app._frame.set_window_active.assert_called_once_with(False)
    assert app._tmux_status_enabled is False
    assert not any(call.args[0][-2:] == ["status", "on"] for call in run.call_args_list)
    teardown.assert_called_once_with()


# ── QuitConfirmModal ─────────────────────────────────────────────────────


def _render_text(modal) -> str:
    """Extract all plain text from a QuitConfirmModal's body."""
    height = modal.preferred_height(60)
    canvas = modal.render((60, height), focus=False)
    return "\n".join(line.decode(errors="replace") for line in canvas.text)


def test_quit_modal_shows_s_option():
    modal = QuitConfirmModal(
        on_confirm=lambda: None,
        on_soft_quit=lambda: None,
        on_cancel=lambda: None,
        running_count=3,
    )
    text = _render_text(modal)
    assert "s = soft quit" in text


def test_quit_modal_soft_quit_key_fires_callback():
    called = []
    modal = QuitConfirmModal(
        on_confirm=lambda: None,
        on_soft_quit=lambda: called.append("soft"),
        on_cancel=lambda: None,
        running_count=0,
    )
    result = modal.keypress((20,), "s")
    assert result is None  # consumed
    assert called == ["soft"]


def test_quit_modal_soft_quit_key_upper_case():
    called = []
    modal = QuitConfirmModal(
        on_confirm=lambda: None,
        on_soft_quit=lambda: called.append("soft"),
        on_cancel=lambda: None,
    )
    modal.keypress((20,), "S")
    assert called == ["soft"]


def test_quit_modal_soft_quit_none_callback_ignores_s():
    """When on_soft_quit is None, 's' is passed through."""
    modal = QuitConfirmModal(
        on_confirm=lambda: None,
        on_soft_quit=None,
        on_cancel=lambda: None,
    )
    result = modal.keypress((20,), "s")
    assert result == "s"  # not consumed


def test_quit_modal_enter_confirms():
    called = []
    modal = QuitConfirmModal(
        on_confirm=lambda: called.append("hard"),
        on_soft_quit=lambda: None,
        on_cancel=lambda: None,
    )
    modal.keypress((20,), "enter")
    assert called == ["hard"]


def test_first_hard_quit_choice_opens_final_warning_before_exit():
    app = _minimal_app()
    app._running = {"one": MagicMock(), "two": MagicMock()}
    app._request_exit = MagicMock()
    app._open_quit_confirm = MagicMock()
    app._show_hard_quit_confirm = MagicMock()

    app._confirm_quit()

    app._request_exit.assert_not_called()
    modal = app._show_hard_quit_confirm.call_args.args[0]
    assert isinstance(modal, HardQuitConfirmModal)
    assert "stop 2 running agent sessions" in _render_text(modal)

    modal.keypress((60,), "enter")

    app._request_exit.assert_called_once_with(soft=False)


def test_final_hard_quit_cancel_returns_to_the_quit_choices():
    app = _minimal_app()
    app._request_exit = MagicMock()
    app._open_quit_confirm = MagicMock()
    app._show_hard_quit_confirm = MagicMock()

    app._confirm_quit()
    modal = app._show_hard_quit_confirm.call_args.args[0]
    modal.keypress((60,), "esc")

    app._open_quit_confirm.assert_called_once_with()
    app._request_exit.assert_not_called()


def test_quit_modal_esc_cancels():
    called = []
    modal = QuitConfirmModal(
        on_confirm=lambda: None,
        on_soft_quit=lambda: None,
        on_cancel=lambda: called.append("cancel"),
    )
    modal.keypress((20,), "esc")
    assert called == ["cancel"]


def test_quit_modal_shows_running_count():
    modal = QuitConfirmModal(
        on_confirm=lambda: None,
        on_soft_quit=lambda: None,
        on_cancel=lambda: None,
        running_count=5,
    )
    text = _render_text(modal)
    assert "5 agent sessions" in text


def test_quit_modal_no_running():
    modal = QuitConfirmModal(
        on_confirm=lambda: None,
        on_soft_quit=lambda: None,
        on_cancel=lambda: None,
        running_count=0,
    )
    text = _render_text(modal)
    assert "No running sessions" in text


# ── Codex placeholder resolution ─────────────────────────────────────────
