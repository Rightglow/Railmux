"""Rendering the status line into the outer tmux status bar (railmux's only status
surface). Only the pure escaping/style/guard logic of ``_render_status_to_tmux``
is exercised here (tmux itself is stubbed) — the option round-trip, the forced
redraw, and the actual colour rendering are verified manually against a
throwaway tmux session.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from railmux.ui.app import (
    App,
    _TMUX_BAR_STYLE_NORMAL,
    _TMUX_BAR_STYLE_ERROR,
    _TMUX_LEVEL_STYLE,
    _compact_tmux_status_left,
    _tmux_status_left,
)
from railmux.ui.workspace import (
    AgentWorkspace,
    WorkspaceLayout,
    WorkspacePage,
    WorkspacePresentation,
)


def _status_app(*, enabled=True, session="railmux", codex_mode=False):
    app = App.__new__(App)
    app._tmux_status_enabled = enabled
    app._tmux_status_session = session
    app._codex_mode = codex_mode
    app._rendered_status_text = None
    app._rendered_status_level = "tip"
    app._status_feedback_alarm = None
    app._loop = None
    return app


def _tmux_commands(run):
    """Expand batched ``tmux cmd ; cmd`` argv into logical commands."""
    commands = []
    for call in run.call_args_list:
        argv = call.args[0]
        if not argv or argv[0] != "tmux":
            continue
        command = ["tmux"]
        for token in argv[1:]:
            if token == ";":
                commands.append(command)
                command = ["tmux"]
            else:
                command.append(token)
        commands.append(command)
    return commands


def _style_calls(run, option):
    """Every value pushed to ``option`` via tmux set-option, in call order."""
    return [
        argv[5]
        for argv in _tmux_commands(run)
        if argv[:2] == ["tmux", "set-option"] and argv[4] == option
    ]


def _status_right_call(run):
    """The full argv of the status-right set-option (skips the bar-style swap)."""
    for argv in _tmux_commands(run):
        if argv[:2] == ["tmux", "set-option"] and argv[4] == "status-right":
            return argv
    raise AssertionError("no status-right set-option captured")


def _payload(run):
    """The status-right value pushed by the set-option call."""
    return _status_right_call(run)[5]


def test_escapes_hash_and_percent_inside_style(monkeypatch):
    # tmux runs status strings through #{...} format expansion AND strftime, so
    # both '#' and '%' in the BODY must be doubled. The style prefix is added
    # after escaping, so its '#[' stays a real directive (single '#').
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app()._render_status_to_tmux("50% done #{x} #[bold] /a#b", "info")

    argv = _status_right_call(run)
    assert argv[:5] == ["tmux", "set-option", "-t", "railmux", "status-right"]
    assert argv[5] == "#[fg=colour231]50%% done ##{x} ##[bold] /a##b#[default] "


def test_forces_status_redraw(monkeypatch):
    # tmux only auto-repaints the bar every status-interval seconds; a short
    # status message must trigger an immediate redraw or it never shows.
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app()._render_status_to_tmux("hi", "info")

    argvs = _tmux_commands(run)
    assert ["tmux", "refresh-client", "-S"] in argvs
    # set-option must come before the refresh so the redraw shows the new value.
    assert argvs.index(_status_right_call(run)) < argvs.index(
        ["tmux", "refresh-client", "-S"])


def test_status_update_uses_one_tmux_client(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app()._render_status_to_tmux("hi", "info")

    run.assert_called_once()
    assert _tmux_commands(run)[-1] == ["tmux", "refresh-client", "-S"]


def test_level_styles_differ(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    app = _status_app()

    app._render_status_to_tmux("boom", "error")
    assert _payload(run) == "#[fg=colour231,bold]boom#[default] "

    run.reset_mock()
    app._render_status_to_tmux("careful", "warn")
    assert _payload(run) == "#[fg=colour220,bold]careful#[default] "

    run.reset_mock()
    app._render_status_to_tmux("copied", "success")
    assert _payload(run) == "#[fg=colour17,bold]copied#[default] "

    run.reset_mock()
    app._render_status_to_tmux("hint", "tip")
    assert _payload(run) == "#[fg=colour0]hint#[default] "


def test_unknown_level_is_unstyled(monkeypatch):
    # A level with no mapping falls back to raw (escaped) text, no #[...] wrap.
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app()._render_status_to_tmux("plain #x", "bogus")

    assert _payload(run) == "plain ##x "


def test_noop_when_disabled(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app(enabled=False)._render_status_to_tmux("anything", "error")

    run.assert_not_called()


def test_noop_without_session(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app(session=None)._render_status_to_tmux("anything", "error")

    run.assert_not_called()


def test_swallows_tmux_errors(monkeypatch):
    # A tmux failure must never propagate into the UI thread.
    def boom(*_a, **_k):
        raise OSError("tmux gone")
    monkeypatch.setattr("subprocess.run", boom)

    # Should not raise.
    _status_app()._render_status_to_tmux("hello", "info")


# ── whole-bar error flip (green normal, dark red on error) ───────────────

def test_error_flips_whole_bar_then_reverts(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    app = _status_app()  # starts in normal (green) mode

    app._render_status_to_tmux("ERROR: boom", "error")
    assert app._tmux_error_bar is True
    assert _style_calls(run, "status-style")[-1] == _TMUX_BAR_STYLE_ERROR
    assert _style_calls(run, "status-left")[-1] == _tmux_status_left(True, False)

    run.reset_mock()
    app._render_status_to_tmux("→ back to normal", "info")
    assert app._tmux_error_bar is False
    assert _style_calls(run, "status-style")[-1] == _TMUX_BAR_STYLE_NORMAL
    assert _style_calls(run, "status-left")[-1] == _tmux_status_left(False, False)


def test_non_error_levels_leave_the_bar_green(monkeypatch):
    # info/warn/tip must not touch status-style/status-left — the bar stays green
    # and only the status-right text colour changes.
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    app = _status_app()

    for level in ("info", "success", "warn", "tip"):
        run.reset_mock()
        app._render_status_to_tmux("msg", level)
        assert _style_calls(run, "status-style") == []
        assert _style_calls(run, "status-left") == []
        assert app._tmux_error_bar is False


def test_error_bar_not_repainted_while_staying_in_error(monkeypatch):
    # A second error render (or a held error re-render) must not re-push the bar
    # style — the swap only fires on the normal↔error transition.
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    app = _status_app()

    app._render_status_to_tmux("ERROR: one", "error")
    run.reset_mock()
    app._render_status_to_tmux("ERROR: two", "error")
    assert _style_calls(run, "status-style") == []  # no re-paint
    assert _payload(run) == "#[fg=colour231,bold]ERROR: two#[default] "


def test_apply_bar_sets_normal_and_error_styles(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    app = _status_app()

    app._apply_tmux_bar(error=False)
    assert _style_calls(run, "status-style")[-1] == _TMUX_BAR_STYLE_NORMAL
    assert _style_calls(run, "status-left")[-1] == _tmux_status_left(False, False)

    run.reset_mock()
    app._apply_tmux_bar(error=True)
    assert _style_calls(run, "status-style")[-1] == _TMUX_BAR_STYLE_ERROR
    assert _style_calls(run, "status-left")[-1] == _tmux_status_left(True, False)


def test_identical_bar_frame_is_not_reapplied(monkeypatch):
    run = MagicMock()
    run.return_value.returncode = 0
    monkeypatch.setattr("subprocess.run", run)
    app = _status_app()

    app._apply_tmux_bar(error=False)
    app._apply_tmux_bar(error=False)

    run.assert_called_once()
    commands = _tmux_commands(run)
    assert [argv[4] for argv in commands if argv[:2] == ["tmux", "set-option"]] == [
        "status-style",
        "status-left",
        "status-left-length",
        "status-right-length",
    ]
    assert commands[-1] == ["tmux", "refresh-client", "-S"]


def test_status_left_shows_current_mode(monkeypatch):
    # The brand carries a "· Claude Code" / "· Codex" indicator that reflects
    # the current mode; toggling mode repaints status-left.
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app(codex_mode=False)._apply_tmux_bar(error=False)
    claude_left = _style_calls(run, "status-left")[-1]
    assert "Claude" in claude_left and "Codex" not in claude_left

    run.reset_mock()
    _status_app(codex_mode=True)._apply_tmux_bar(error=False)
    codex_left = _style_calls(run, "status-left")[-1]
    assert codex_left.endswith("· Codex #[default]")
    assert claude_left != codex_left


def test_status_left_pure_function():
    normal = _tmux_status_left(False, False)
    assert "◧ · Claude #[default]" in normal
    assert "Railmux" not in normal
    error = _tmux_status_left(True, True)
    assert "◧ · Codex #[default]" in error

    third = _tmux_status_left(False, "Review Agent")
    assert "◧ · Review Agent #[default]" in third

    with_layout = _tmux_status_left(False, "Codex", "◨")
    assert "◧ · ◨ · Codex #[default]" in with_layout

    hidden_with_counts = _tmux_status_left(
        False, "Devin", "◨", sidebar_hidden=True, running_counts=(1, 2, 0, 3),
    )
    assert "▢ · ◨ · Devin" in hidden_with_counts
    assert "#[bg=#1b4214]" in hidden_with_counts
    assert "•#[fg=#edf5ee,nobold]1" in hidden_with_counts
    assert "•#[fg=#edf5ee,nobold]2" in hidden_with_counts
    assert "•#[fg=#edf5ee,nobold]3" in hidden_with_counts
    assert "•#[fg=#edf5ee,nobold]0" not in hidden_with_counts


def test_wide_status_left_wraps_mode_and_layout_as_separate_actions():
    wrapped = []

    def wrap(action, content):
        wrapped.append((action, content))
        return f"<{action}>{content}</{action}>"

    value = _tmux_status_left(
        False, "Claude", "◧", wrap, running_counts=(0, 1, 2, 0),
    )

    assert [a for a, _c in wrapped] == [
        "railmux-sidebar",
        "railmux-layout",
        "railmux-mode",
        "railmux-running",
    ]
    assert "<railmux-sidebar>◧</railmux-sidebar>" in value
    assert "<railmux-layout>◧</railmux-layout>" in value
    assert "<railmux-mode>Claude</railmux-mode>" in value
    assert "<railmux-running>" in value


def test_compact_status_left_has_stable_phone_navigation_and_mode_abbreviation():
    value, visible = _compact_tmux_status_left(
        False,
        "Claude",
        WorkspacePage.PRIMARY,
        ("%1", "%2", "%3"),
        40,
    )

    assert "[R]" in value and "[1]" in value and "[2]" in value
    assert " Cl " in value
    assert "#[fg=colour231][1]" in value
    assert "#[fg=colour0][R]" in value
    assert visible == len("[R][1][2] Cl ")


def test_compact_status_left_expands_without_shortening_tip_pool():
    value, visible = _compact_tmux_status_left(
        False,
        "Codex",
        WorkspacePage.SIDEBAR,
        ("%1", "%2", "%3"),
        105,
    )

    assert "[Railmux]" in value
    assert "[Agent 1]" in value
    assert "[Agent 2]" in value
    assert " Codex " in value
    assert visible == len("[Railmux][Agent 1][Agent 2] Codex ")


def test_compact_status_left_wraps_mode_and_layout_as_actions():
    actions = []

    def wrap_action(action, content):
        actions.append((action, content))
        return f"<{action}>{content}</{action}>"

    value, visible = _compact_tmux_status_left(
        False,
        "Claude Code",
        WorkspacePage.PRIMARY,
        ("%1", "%2", "%3"),
        40,
        action_range_wrapper=wrap_action,
        layout_indicator="◨",
    )

    assert actions == [
        ("railmux-mode", "Cl"),
        ("railmux-layout", "◨"),
    ]
    assert "<railmux-mode>Cl</railmux-mode>" in value
    assert "<railmux-layout>◨</railmux-layout>" in value
    assert visible == len("[R][1][2] Cl · ◨ ")


def test_compact_status_left_routes_available_pages_through_controller_actions():
    pages = []

    def wrap_page(action, content):
        pages.append((action, content))
        return f"<{action}>{content}</{action}>"

    value, _visible = _compact_tmux_status_left(
        False,
        "Codex",
        WorkspacePage.PRIMARY,
        ("%1", "%2", None),
        40,
        page_range_wrapper=wrap_page,
    )

    assert pages == [
        ("railmux-page-r", "#[fg=colour0][R]"),
        ("railmux-page-1", "#[fg=colour231][1]"),
    ]
    assert "<railmux-page-r>" in value
    assert "<railmux-page-1>" in value
    assert "<railmux-page-2>" not in value


def test_apply_bar_uses_dynamic_compact_left_length(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    app = _status_app()
    app._workspace = AgentWorkspace()
    app._workspace.presentation = WorkspacePresentation.COMPACT
    app._workspace.compact_page = WorkspacePage.SIDEBAR
    app._workspace.primary.pane_id = "%2"
    app._railmux_pane_id = "%1"
    app._last_workspace_size = (40, 20)
    app._tmux_binding_manager = None

    app._apply_tmux_bar(error=False)

    lengths = _style_calls(run, "status-left-length")
    assert lengths[-1] == str(len("[R][1][2] Cl · ▣ "))
    right_lengths = _style_calls(run, "status-right-length")
    assert right_lengths[-1] == str(40 - len("[R][1][2] Cl · ▣ "))

    run.reset_mock()
    app._workspace.presentation = WorkspacePresentation.WIDE
    app._apply_tmux_bar(error=False)
    assert _style_calls(run, "status-right-length")[-1] == str(
        app._TMUX_STATUS_RIGHT_LENGTH)


def test_apply_compact_bar_makes_pages_mode_and_layout_clickable(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.tmux_version", lambda: (3, 4))
    app = _status_app()
    app._workspace = AgentWorkspace()
    app._workspace.presentation = WorkspacePresentation.COMPACT
    app._workspace.compact_page = WorkspacePage.PRIMARY
    app._workspace.primary.pane_id = "%2"
    app._workspace.secondary.pane_id = "%3"
    app._workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    app._workspace.set_target(AgentWorkspace.SECONDARY)
    app._railmux_pane_id = "%1"
    app._last_workspace_size = (40, 20)
    app._tmux_binding_manager = MagicMock(status_navigation_available=True)

    app._apply_tmux_bar(error=False)

    left = _style_calls(run, "status-left")[-1]
    assert "#[range=user|railmux-page-r]" in left
    assert "#[range=user|railmux-page-1]" in left
    assert "#[range=user|railmux-page-2]" in left
    assert "#[range=user|railmux-mode]Cl#[norange]" in left
    assert "#[range=user|railmux-layout]◨#[norange]" in left
    visible = len("[R][1][2] Cl · ◨ ")
    assert _style_calls(run, "status-left-length")[-1] == str(visible)
    assert _style_calls(run, "status-right-length")[-1] == str(40 - visible)


def test_apply_wide_bar_makes_mode_and_layout_clickable(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.tmux_version", lambda: (3, 4))
    app = _status_app()
    app._workspace = AgentWorkspace()
    app._workspace.primary.pane_id = "%2"
    app._workspace.layout = WorkspaceLayout.SINGLE
    app._tmux_binding_manager = MagicMock(status_navigation_available=True)

    app._apply_tmux_bar(error=False)

    left = _style_calls(run, "status-left")[-1]
    assert "#[range=user|railmux-sidebar]◧#[norange]" in left
    assert "#[range=user|railmux-layout]▣#[norange]" in left
    assert "#[range=user|railmux-mode]Claude#[norange]" in left


def test_rendered_status_text_is_clickable_and_copy_uses_full_source(
        monkeypatch):
    run = MagicMock()
    copied = MagicMock(return_value=True)
    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.tmux_version", lambda: (3, 4))
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.copy_to_clipboard", copied)
    app = _status_app()
    app._tmux_binding_manager = MagicMock(status_navigation_available=True)
    app._loop = MagicMock()
    app._loop.set_alarm_in.return_value = object()

    app._render_status_to_tmux("A long tip that tmux may truncate", "tip")
    assert _payload(run).startswith(
        "#[range=user|railmux-copy]#[fg=colour0]")
    assert _payload(run).endswith("#[default] #[norange]")
    run.reset_mock()

    app._copy_current_status()
    copied.assert_called_once_with("A long tip that tmux may truncate")
    assert app._rendered_status_text == "A long tip that tmux may truncate"
    assert "#[fg=colour17,bold]Copied status message." in _payload(run)

    delay, restore = app._loop.set_alarm_in.call_args.args
    assert delay == 1.2
    run.reset_mock()
    restore(app._loop, None)

    assert "#[fg=colour0]A long tip that tmux may truncate" in _payload(run)
    assert app._status_feedback_alarm is None


def test_internal_status_actions_are_modal_safe():
    app = _status_app()
    app._frame = object()
    app._loop = MagicMock(widget=object())
    app._show_tmux_feedback = MagicMock()
    app._open_mode_menu = MagicMock()
    app._rotate_split = MagicMock()

    app._on_input("f5")
    app._on_input("f7")

    app._open_mode_menu.assert_not_called()
    app._rotate_split.assert_not_called()
    assert app._show_tmux_feedback.call_count == 2
    assert "Close the dialog" in app._show_tmux_feedback.call_args.args[0]


def test_internal_status_actions_dispatch_without_modal():
    app = _status_app()
    app._frame = object()
    app._loop = MagicMock()
    app._loop.widget = app._frame
    app._open_mode_menu = MagicMock()
    app._rotate_split = MagicMock()

    app._on_input("f5")
    app._on_input("f7")

    app._open_mode_menu.assert_called_once_with()
    app._rotate_split.assert_called_once_with()


def test_compact_error_status_uses_legible_control_colours():
    value, _visible = _compact_tmux_status_left(
        True,
        "Claude Code",
        WorkspacePage.PRIMARY,
        ("%1", "%2", "%3"),
        40,
    )

    assert "#[fg=colour231][R]" in value
    assert "#[fg=colour220][1]" in value


def test_status_left_keeps_layout_and_target_visible_across_focus(monkeypatch):
    run = MagicMock()
    run.return_value.returncode = 0
    monkeypatch.setattr("subprocess.run", run)
    app = _status_app()
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    app._workspace.primary.pane_id = "%2"
    app._workspace.secondary.pane_id = "%3"
    app._workspace.set_target(AgentWorkspace.SECONDARY)
    app._railmux_has_focus = True

    app._apply_tmux_bar(error=False)
    assert _style_calls(run, "status-left")[-1] == _tmux_status_left(False, "Claude Code", "◨")

    run.reset_mock()
    app._railmux_has_focus = False
    app._apply_tmux_bar(error=False)
    run.assert_not_called()

    app._workspace.secondary.pane_id = None
    app._workspace.layout = WorkspaceLayout.SINGLE
    app._workspace.set_target(AgentWorkspace.PRIMARY)
    app._apply_tmux_bar(error=False)
    single_status = _style_calls(run, "status-left")[-1]
    assert "◧ · ▣ · Claude" in single_status
    assert "Target" not in single_status


def test_layout_indicator_maps_orientation_and_target():
    app = _status_app()
    app._workspace = AgentWorkspace()
    workspace = app._workspace

    assert app._status_layout_indicator() is None
    workspace.primary.pane_id = "%2"
    assert app._status_layout_indicator() == "▣"

    workspace.secondary.pane_id = "%3"
    workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    assert app._status_layout_indicator() == "◧"
    workspace.set_target(AgentWorkspace.SECONDARY)
    assert app._status_layout_indicator() == "◨"

    workspace.layout = WorkspaceLayout.STACKED
    assert app._status_layout_indicator() == "⬓"
    workspace.set_target(AgentWorkspace.PRIMARY)
    assert app._status_layout_indicator() == "⬒"


def test_apply_bar_noop_when_disabled(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app(enabled=False)._apply_tmux_bar(error=True)

    run.assert_not_called()


def test_level_styles_are_pill_free():
    # The design decision: info/warn/tip sit directly on the green bar and error
    # sits on the whole-bar red — so NO per-level style carries its own bg.
    for level, style in _TMUX_LEVEL_STYLE.items():
        assert "bg=" not in style, f"{level} should not set its own background"
    # warn is the only non-error level bolded (alert without a pill); error too.
    assert "bold" in _TMUX_LEVEL_STYLE["warn"]
    assert "bold" in _TMUX_LEVEL_STYLE["error"]
    # The bar/brand styles are what carry the background.
    assert "bg=" in _TMUX_BAR_STYLE_NORMAL and "bg=" in _TMUX_BAR_STYLE_ERROR


def test_normal_bar_uses_grass_green_accent():
    assert _TMUX_BAR_STYLE_NORMAL.startswith("bg=#5faf00,")


def test_devin_input_cursor_keys_handles_ascii_cjk_and_bails_on_placeholders():
    from railmux.fast_display_input import devin_input_cursor_keys

    def _cells(text: str, width: int = 40) -> tuple[str | None, ...]:
        out: list[str | None] = []
        for ch in text:
            out.append(ch)
            if ord(ch) > 127 and ch not in "─❭…":
                out.append(None)
        while len(out) < width:
            out.append(" ")
        return tuple(out[:width])

    rule = "─" * 40
    # ASCII: cursor at end of "hello world" (col 13), click on 'w' (col 8) -> 5 Lefts.
    rows = (_cells(rule), _cells("❭ hello world"), _cells(rule), _cells("Gemini · bypass"))
    assert devin_input_cursor_keys(rows, 1, 13, 1, 8) == b"\x1b[D" * 5
    # CJK double-width: "❭ 你好世界" -> '你'(2,3) '好'(4,5) '世'(6,7) '界'(8,9), end=10
    rows_cjk = (_cells(rule), _cells("❭ 你好世界"), _cells(rule), _cells("Gemini · bypass"))
    assert devin_input_cursor_keys(rows_cjk, 1, 10, 1, 4) == b"\x1b[D" * 3
    # Placeholder / Image token bails out safely.
    rows_ph = (_cells(rule), _cells("❭ Ask Devin anything"), _cells(rule), _cells("Gemini · bypass"))
    assert devin_input_cursor_keys(rows_ph, 1, 2, 1, 8) == b""
    rows_img = (_cells(rule), _cells("❭ check [Image #1] here"), _cells(rule), _cells("Gemini · bypass"))
    assert devin_input_cursor_keys(rows_img, 1, 10, 1, 4) == b""


def test_dual_pane_f9_toggle_sidebar_detaches_and_rejoins_keeping_split(monkeypatch):
    from railmux import tmux_ctl

    app = _status_app()
    app._railmux_pane_id = "%1"
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    app._workspace.primary.pane_id = "%2"
    app._workspace.secondary.pane_id = "%3"
    app._sidebar_hidden_detached = False
    app._saved_sidebar_width = 30
    app._set_railmux_focus = MagicMock()
    app._set_status = MagicMock()
    app._apply_tmux_bar = MagicMock()
    app._sync_pane_headers = MagicMock()
    monkeypatch.setattr(tmux_ctl, "pane_alive", lambda _pid: True)
    monkeypatch.setattr(tmux_ctl, "pane_size", lambda _pid: (28, 38))
    selected = []
    monkeypatch.setattr(tmux_ctl, "select_pane", lambda pid: selected.append(pid) or True)
    broken = []
    monkeypatch.setattr(tmux_ctl, "break_pane_detached", lambda pid: broken.append(pid) or "@9")
    joined = []
    monkeypatch.setattr(
        tmux_ctl, "join_pane_left",
        lambda src, dst, width: joined.append((src, dst, width)) or True,
    )

    # First F9 detaches ONLY the sidebar pane, keeping %2 and %3 split.
    app._toggle_agent_fullscreen()
    assert app._sidebar_hidden_detached is True
    assert broken == ["%1"]
    assert app._saved_sidebar_width == 28

    # Second F9 re-joins the sidebar on the left edge at its exact saved width.
    app._toggle_agent_fullscreen()
    assert app._sidebar_hidden_detached is False
    assert joined == [("%1", "%2", 28)]
    assert selected[-1] == "%1"


def test_devin_live_status_preserves_busy_on_transient_swap_capture(monkeypatch):
    from railmux import tmux_ctl
    from railmux.ui.app import _Running

    app = _status_app()
    running = _Running("s1", "dv-1", "ws/task", session_type="devin", status="busy")
    app._running = {"s1": running}
    app._last_live_status = {"dv-1": "busy"}
    app._unread_finished = set()
    app._devin_status_probe_cache = {}
    app._transport = None

    # Transient empty capture during pane swap must keep "busy" (no false cyan done dot).
    monkeypatch.setattr(tmux_ctl, "capture_pane_visible", lambda _t: ["", "   "])
    assert app._devin_live_status(running) == "busy"
    assert app._record_live_status_transition(running, "busy") == "busy"
    assert "dv-1" not in app._unread_finished

    # Transient mid-SIGWINCH capture with output but no settled prompt box keeps "busy".
    app._devin_status_probe_cache.clear()
    monkeypatch.setattr(tmux_ctl, "capture_pane_visible", lambda _t: ["some output line"])
    assert app._devin_live_status(running) == "busy"
