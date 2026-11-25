"""Options screen integration with app-mutable settings."""

from __future__ import annotations

from unittest.mock import MagicMock

from railmux.config import Config
from railmux.settings import LayoutProfile, Settings
from railmux.ui.app import App
from railmux.ui.modals import OptionsModal
from railmux.ui.workspace import WorkspacePresentation


def _app(tmp_path, monkeypatch) -> App:
    monkeypatch.setattr(
        "railmux.settings._config_path", lambda: tmp_path / "config.toml")
    app = App.__new__(App)
    app._settings = Settings()
    app._layout_profile = None
    app._codex_yolo_runtime = False
    app._codex_yolo_prompt_handled = False
    app._capture_layout_profile = MagicMock(
        return_value=LayoutProfile("always", "single", 300))
    app._set_status = MagicMock()
    app._open_full_sidebar_modal = MagicMock()
    return app


def test_options_layout_choices_persist_and_update_live_preference(
    tmp_path, monkeypatch,
):
    app = _app(tmp_path, monkeypatch)

    app._open_options_modal()
    modal = app._open_full_sidebar_modal.call_args.args[0]
    assert isinstance(modal, OptionsModal)

    modal._option_rows["layout"][0].keypress((60,), "enter")
    assert app._settings.layout_save_policy == "always"
    # Captured before the full-sidebar Options zoom, never from its geometry.
    app._capture_layout_profile.assert_called_once_with("always")
    assert app._settings.layout_profile == LayoutProfile(
        "always", "single", 300)
    assert app._layout_profile == app._settings.layout_profile

    modal._option_rows["layout"][2].keypress((60,), "enter")
    assert app._settings.layout_save_policy == "never"
    assert app._settings.layout_profile is None
    assert app._layout_profile is None


def test_options_always_preserves_saved_wide_profile_in_compact_view(
    tmp_path, monkeypatch,
):
    app = _app(tmp_path, monkeypatch)
    saved = LayoutProfile("once", "stacked", 240, 600)
    assert app._settings.set_layout_save_policy("ask", saved)
    app._layout_profile = saved
    app._agent_workspace().presentation = WorkspacePresentation.COMPACT

    app._open_options_modal()
    modal = app._open_full_sidebar_modal.call_args.args[0]
    modal._option_rows["layout"][0].keypress((60,), "enter")

    assert app._capture_layout_profile.call_count == 0
    assert app._settings.layout_profile == LayoutProfile(
        "always", "stacked", 240, 600)


def test_options_yolo_policy_changes_only_future_launches(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app._codex_yolo_runtime = True

    app._open_options_modal()
    modal = app._open_full_sidebar_modal.call_args.args[0]
    modal._option_rows["yolo"][0].keypress((60,), "enter")

    assert app._settings.codex_yolo_policy == "always"
    assert app._codex_yolo_enabled() is True
    assert app._codex_yolo_runtime is False

    modal._option_rows["yolo"][1].keypress((60,), "enter")
    assert app._settings.codex_yolo_policy == "ask"
    assert app._codex_yolo_enabled() is False
    assert app._codex_yolo_prompt_handled is False


def test_options_update_policy_persists(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    app._open_options_modal()
    modal = app._open_full_sidebar_modal.call_args.args[0]

    modal._option_rows["update"][0].keypress((60,), "enter")
    assert app._settings.update_policy == "always"

    modal._option_rows["update"][2].keypress((60,), "enter")
    assert app._settings.update_policy == "never"


def test_options_claude_history_policy_persists(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    app._open_options_modal()
    modal = app._open_full_sidebar_modal.call_args.args[0]
    modal._option_rows["claude_history"][0].keypress((60,), "enter")
    assert app._settings.claude_history_policy == "local"
    modal._option_rows["claude_history"][2].keypress((60,), "enter")
    assert app._settings.claude_history_policy == "native"


def test_options_path_open_policy_persists(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    app._open_options_modal()
    modal = app._open_full_sidebar_modal.call_args.args[0]
    modal._option_rows["path_open"][0].keypress((60,), "enter")
    assert app._settings.path_open_policy == "internal"
    app._set_status.assert_called_with(
        "Clicked paths: Inside · managed Vim; applies to the next click in "
        "native Windows or railmux ssh."
    )
    modal._option_rows["path_open"][2].keypress((60,), "enter")
    assert app._settings.path_open_policy == "external"


def test_options_failed_claude_history_write_keeps_selected_policy(
    tmp_path, monkeypatch,
):
    app = _app(tmp_path, monkeypatch)
    app._settings.set_claude_history_policy = MagicMock(return_value=False)

    app._open_options_modal()
    modal = app._open_full_sidebar_modal.call_args.args[0]
    modal._option_rows["claude_history"][0].keypress((60,), "enter")

    assert modal._policies["claude_history"] == "ask"
    assert app._set_status.call_args.args[1] == "error"


def test_options_failed_write_keeps_modal_and_app_state(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app._settings.set_layout_save_policy = MagicMock(return_value=False)

    app._open_options_modal()
    modal = app._open_full_sidebar_modal.call_args.args[0]
    modal._option_rows["layout"][0].keypress((60,), "enter")

    assert modal._policies["layout"] == "ask"
    assert app._layout_profile is None
    assert app._set_status.call_args.args[1] == "error"


def test_persistent_policy_labels_distinguish_never_from_one_time_no():
    assert App._policy_label("always") == "Always"
    assert App._policy_label("ask") == "Ask every time"
    assert App._policy_label("never") == "Never"


def _option_text(modal, size: tuple[int, int]) -> str:
    return "\n".join(row.decode() for row in modal.render(size, focus=True).text)


def _click_option(modal, size: tuple[int, int], label: str) -> None:
    line = next(i for i, text in enumerate(_option_text(modal, size).splitlines())
                if label in text)
    assert modal.mouse_event(size, "mouse press", 1, 5, line, True)


def test_history_options_show_on_small_first_screen_and_save_independently(
    tmp_path, monkeypatch,
):
    app = _app(tmp_path, monkeypatch)
    app._active_mode_key = "devin"
    app._open_options_modal()
    modal = app._open_full_sidebar_modal.call_args.args[0]
    size = (38, 13)
    text = _option_text(modal, size)
    for label in (
        "Agent mode · Devin", "Claude history · Ask on scroll",
        "Codex history · Native", "Devin history · Local",
    ):
        line = next(line for line in text.splitlines() if label in line)
        assert "›" in line and "<" not in line and ">" not in line

    _click_option(modal, size, "Devin history ·")
    assert "Applies in railmux ssh." in _option_text(modal, size)
    _click_option(modal, size, "Native —")
    assert app._settings.terminal_history_policies() == ("native", "native")

    _click_option(modal, size, "Back to Options")
    assert "Devin history · Native" in _option_text(modal, size)
    _click_option(modal, size, "Codex history ·")
    _click_option(modal, size, "styled scrollback")
    assert app._settings.terminal_history_policies() == ("local", "native")

    _click_option(modal, size, "Back to Options")
    _click_option(modal, size, "Claude history ·")
    _click_option(modal, size, "use Railmux scrollback")
    assert app._settings.claude_history_policy == "local"
    assert app._settings.terminal_history_policies() == ("local", "native")


def test_agent_mode_option_opens_shared_picker_and_switches_with_keyboard(
    tmp_path, monkeypatch,
):
    app = _app(tmp_path, monkeypatch)
    app._active_mode_key = "devin"
    app._config = Config()
    app._running = {}
    app._probe_mode_availability = lambda: frozenset({"codex", "devin"})
    actions = []
    app._close_options_modal = lambda: actions.append("closed")
    app._switch_mode = lambda key: actions.append(key)
    app._open_options_modal()
    modal = app._open_full_sidebar_modal.call_args.args[0]
    size = (38, 13)

    modal.keypress(size, "left")
    modal.keypress(size, "right")
    assert "Agent mode · Devin" in _option_text(modal, size)
    assert actions == []

    modal.keypress(size, "enter")
    text = _option_text(modal, size)
    assert "Claude · Unavailable" in text
    assert "● Devin" in text
    modal.keypress(size, "down")
    modal.keypress(size, "enter")
    assert actions == ["closed", "codex"]


def test_options_modal_saves_devin_bypass_policy(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app._devin_bypass_runtime = False
    app._devin_bypass_prompt_handled = False
    app._open_options_modal()
    modal = app._open_full_sidebar_modal.call_args.args[0]

    assert modal._policies["devin_bypass"] == "never"
    modal._option_rows["devin_bypass"][0].keypress((60,), "enter")
    assert modal._policies["devin_bypass"] == "always"
    assert app._settings.devin_bypass_policy == "always"
    assert app._devin_bypass_prompt_handled is True


def test_notification_sound_defaults_off_and_rings_only_when_enabled(tmp_path, monkeypatch):
    from railmux import tmux_ctl
    from railmux.fast_display_protocol import (
        BellNotification,
        ServerMessageDecoder,
        encode_bell,
    )
    from railmux.terminal_screen import extended_pyte
    from railmux.ui import app as app_mod

    app = _app(tmp_path, monkeypatch)
    app._devin_bypass_runtime = False
    app._devin_bypass_prompt_handled = False
    app._unread_finished = set()
    app._last_live_status = {}
    app._railmux_has_focus = True
    app._tmux_status_session = "railmux-test"
    app._agent_workspace().target.agent_tmux_name = "dv-other"

    rung = []
    monkeypatch.setattr(tmux_ctl, "ring_session_bell", lambda name: rung.append(name) or True)

    # Defaults to off in Settings and OptionsModal; transition stays silent.
    app._open_options_modal()
    modal = app._open_full_sidebar_modal.call_args.args[0]
    assert modal._policies["notification_sound"] == "off"
    running = app_mod._Running("a", "dv-a", "proj/A", session_type="devin", status="busy")
    assert app._record_live_status_transition(running, "busy") == "busy"
    assert app._record_live_status_transition(running, "idle") == "done"
    assert rung == []

    # Enabling via Options rings when a background session finishes or blocks.
    modal._option_rows["notification_sound"][1].keypress((60,), "enter")
    assert app._settings.notification_sound_policy == "on"
    assert app._record_live_status_transition(running, "busy") == "busy"
    assert app._record_live_status_transition(running, "idle") == "done"
    assert rung == ["railmux-test"]

    # Bell protocol and extended_pyte Screen round-trip for railmux ssh.
    pyte_mod = extended_pyte(__import__("pyte"))
    screen = pyte_mod.Screen(40, 10)
    stream = pyte_mod.ByteStream(screen)
    assert screen.consume_bell() is False
    stream.feed(b"hi\x07")
    assert screen.consume_bell() is True
    assert screen.consume_bell() is False
    decoder = ServerMessageDecoder()
    assert decoder.feed(encode_bell()) == [BellNotification()]
