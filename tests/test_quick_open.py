from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from railmux.models import Project, SessionMeta
from railmux.ui.app import App, _Running
from railmux.ui.quick_open import QuickOpen, SessionChoice
from railmux.ui.running_pane import RunningSessionsPane


def test_quick_open_search_and_views_use_the_selected_exact_action():
    open_claude, open_devin, close = Mock(), Mock(), Mock()
    choices = [
        SessionChoice(("saved", "claude", "old"), "Settings", "Claude Code",
                      "Railmux", "Saved", open_claude, starred=True),
        SessionChoice(("running", "devin", "dv-work"), "Upgrade", "Devin",
                      "Railmux", "Running in pane 2", open_devin),
    ]
    modal = QuickOpen(choices, close, "Pane 1")
    size = (55, 12)
    assert b"Saved sessions open in Pane 1" in b"\n".join(modal.render(size).text)
    for char in "devin":
        modal.keypress(size, char)
    assert b"Settings" not in b"\n".join(modal.render(size).text)
    modal.keypress(size, "tab")
    modal.keypress(size, "enter")
    close.assert_called_once_with()
    open_devin.assert_called_once_with()
    open_claude.assert_not_called()


def test_quick_open_marks_keyboard_and_wheel_selection_while_search_has_focus():
    open_first, open_second = Mock(), Mock()
    modal = QuickOpen([
        SessionChoice(("saved", "codex", "first"), "First", "Codex",
                      "Railmux", "Saved", open_first),
        SessionChoice(("saved", "codex", "second"), "Second", "Codex",
                      "Railmux", "Saved", open_second),
    ], Mock(), "Pane 1")
    size = (55, 12)

    def marked_result():
        canvas = modal.render(size, focus=True)
        assert canvas.cursor is not None and canvas.cursor[1] == 2
        rows = [row.decode() for row in canvas.text]
        marked, = (line for line in rows if "› [Codex · Saved]" in line)
        return marked, next(index for index, line in enumerate(rows) if line == marked)

    assert "First" in marked_result()[0]
    modal.keypress(size, "down")
    assert "Second" in marked_result()[0]
    modal.keypress(size, "up")
    line, index = marked_result()
    assert "First" in line
    assert modal.mouse_event(size, "mouse press", 5, 10, index, True)
    assert "Second" in marked_result()[0]

    modal.keypress(size, "enter")
    open_first.assert_not_called()
    open_second.assert_called_once_with()


def test_running_scope_and_quick_open_keep_foreign_and_unavailable_live_agents():
    app = App.__new__(App)
    app._active_mode_key = "codex"
    app._running_pane = RunningSessionsPane(Mock())
    app._set_status = Mock()
    app._on_running_select = Mock()
    app._on_session_select = Mock()
    app._favorite_ids_for_view = lambda: {"starred"}
    app._launchable_mode_keys = lambda: ("claude",)
    app._renames = Mock()
    app._renames.get.return_value = None
    app._codex_lineage_ids = lambda key: {key}
    project = Project(Path("/project"), "project", Path("/hist"), 2, 0)
    devin = _Running("live", "dv-live", "project/Live", project=project, session_type="devin")
    codex = _Running("other", "cx-other", "project/Other", project=project, session_type="codex")
    app._running = {"live": devin, "other": codex}
    star = SessionMeta(project, "starred", Path("/hist/starred.jsonl"), "Starred", 1, 0, 12)

    app._render_running_pane()
    assert app._running_pane.section_count == "1"
    app._toggle_running_scope()
    assert app._running_pane.sidebar_scope == "All"
    assert app._running_pane.section_count == "2"

    choices = app._quick_open_choices((star,))
    assert [choice.provider for choice in choices] == ["Devin", "Codex", "Claude"]
    assert choices[-1].starred
    choices[-1].on_open()
    app._on_session_select.assert_called_once_with(star)
    choices[0].on_open()
    assert app._on_running_select.call_args.args[0].tmux_name == "dv-live"
    del app._running["live"]
    choices[0].on_open()
    assert app._on_running_select.call_count == 1


def test_quick_open_highlights_provider_workspace_state_and_matched_terms():
    from railmux.fuzzy import fuzzy_match_positions

    assert fuzzy_match_positions(["fp8"], "flash fp8 kernel") == {6, 7, 8}
    assert fuzzy_match_positions(["f8"], "fp8") == {0, 2}

    choices = [
        SessionChoice(("running", "devin", "dv-1"), "Tune fp8 kernel", "Devin",
                      "workspace", "Running", Mock(), starred=True),
        SessionChoice(("saved", "codex", "cx-1"), "Review fp8 patch", "Codex",
                      "railmux", "Saved", Mock()),
    ]
    modal = QuickOpen(choices, Mock(), "Pane 1")
    size = (64, 14)
    for ch in "fp8":
        modal.keypress(size, ch)

    canvas = modal.render(size, focus=True)
    attrs_by_text: dict[str, set[str]] = {}
    for row in canvas.content():
        for attr, _cs, chunk in row:
            text = chunk.decode("utf-8", errors="replace")
            if attr is not None:
                attrs_by_text.setdefault(text, set()).add(str(attr))

    # Active first row uses search_match_focus on "fp8".
    assert "search_match_focus" in attrs_by_text.get("fp8", set())
    # Inactive second row uses provider tint, workspace tint, and search_match on "fp8".
    assert "search_match" in attrs_by_text.get("fp8", set())
    assert "search_provider_codex" in attrs_by_text.get("Codex", set())
    assert "search_workspace" in attrs_by_text.get("railmux", set())


def test_quick_open_command_palette_triggers_on_gt_and_executes_selected():
    from railmux.ui.quick_open import CommandChoice

    ran: list[str] = []
    close = Mock()
    commands = [
        CommandChoice("Toggle left sidebar", "F9", "Hide or show sidebar", lambda: ran.append("sidebar")),
        CommandChoice("Cycle layout", "F8", "Single or split", lambda: ran.append("layout")),
    ]
    choices = [
        SessionChoice(("saved", "devin", "dv-1"), "Tune fp8", "Devin", "workspace", "Saved", Mock()),
    ]
    modal = QuickOpen(choices, close, "Pane 1", commands=commands)
    size = (70, 14)

    # Empty query shows hint row while keeping the first real session selectable.
    initial = b"\n".join(modal.render(size, focus=True).text).decode()
    assert "Tip: type >" in initial
    assert "› [Devin · Saved]" in initial

    # Typing ">" immediately switches into command palette (no space required).
    for ch in ">lay":
        modal.keypress(size, ch)
    palette = b"\n".join(modal.render(size, focus=True).text).decode()
    assert "Cycle layout" in palette
    assert "Toggle left sidebar" not in palette

    modal.keypress(size, "enter")
    close.assert_called_once_with()
    assert ran == ["layout"]


def test_quick_open_ranks_results_and_shows_truncation_footer():
    choices = [
        SessionChoice(("saved", "devin", "s-fuzzy"), "f_o_o_p_8 misc", "Devin", "ws", "Saved", Mock()),
        SessionChoice(("saved", "devin", "s-sub"), "tune fp8 kernel", "Devin", "ws", "Saved", Mock()),
        SessionChoice(("saved", "devin", "s-prefix"), "fp8 bench", "Devin", "ws", "Saved", Mock()),
        SessionChoice(("saved", "devin", "s-star"), "other fp8 note", "Devin", "ws", "Saved", Mock(), starred=True),
        SessionChoice(("running", "devin", "r-live"), "live fp8 run", "Devin", "ws", "Running", Mock()),
    ]
    modal = QuickOpen(choices, Mock(), "Pane 1")
    size = (70, 14)
    for ch in "fp8":
        modal.keypress(size, ch)
    assert [c.identity[2] for c in modal._shown] == [
        "r-live", "s-star", "s-prefix", "s-sub", "s-fuzzy",
    ]

    # >80 matches appends a transparent truncation footer row.
    many = [
        SessionChoice(("saved", "devin", f"s-{i}"), f"fp8 session {i}", "Devin", "ws", "Saved", Mock())
        for i in range(95)
    ]
    modal_many = QuickOpen(many, Mock(), "Pane 1")
    for ch in "fp8":
        modal_many.keypress(size, ch)
    assert len(modal_many._shown) == 80
    last_row_text = b"".join(modal_many._rows[-1].render((70,)).text).decode()
    assert "Showing 80 of 95" in last_row_text


def test_app_quick_open_commands_all_bind_existing_methods():
    from unittest.mock import MagicMock

    app = App.__new__(App)
    app._active_mode_key = "devin"
    app._settings = MagicMock(notification_sound_policy="off")
    app._settings.set_notification_sound_policy.return_value = True
    app._set_status = MagicMock()
    app._switch_mode = MagicMock()
    app._toggle_agent_fullscreen = MagicMock()
    app._rotate_split = MagicMock()
    app._launch_new_session = MagicMock()
    app._open_options_modal = MagicMock()
    app._toggle_sessions_scope = MagicMock()
    app._toggle_running_scope = MagicMock()
    app._toggle_sidebar_pane_zoom = MagicMock()
    app._open_help_modal = MagicMock()
    app._open_quit_confirm = MagicMock()

    commands = app._quick_open_commands()
    assert any(c.name == "Switch mode: Claude" for c in commands)
    assert any(c.name == "Switch mode: Codex" for c in commands)
    for cmd in commands:
        cmd.on_run()
    app._switch_mode.assert_any_call("claude")
    app._switch_mode.assert_any_call("codex")
