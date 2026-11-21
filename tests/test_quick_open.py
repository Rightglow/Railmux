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


def test_running_scope_and_quick_open_keep_foreign_and_unavailable_live_agents():
    app = App.__new__(App)
    app._active_mode_key = "codex"
    app._running_pane = RunningSessionsPane(Mock())
    app._set_status = Mock()
    app._on_running_select = Mock()
    app._on_session_select = Mock()
    app._favorite_ids_for_view = lambda: {"starred"}
    app._cycle_mode_keys = lambda: ("claude",)
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
    assert app._running_pane.sidebar_title == "Running · All"
    assert app._running_pane.section_count == "2"

    choices = app._quick_open_choices((star,))
    assert [choice.provider for choice in choices] == ["Devin", "Codex", "Claude Code"]
    assert choices[-1].starred
    choices[-1].on_open()
    app._on_session_select.assert_called_once_with(star)
    choices[0].on_open()
    assert app._on_running_select.call_args.args[0].tmux_name == "dv-live"
    del app._running["live"]
    choices[0].on_open()
    assert app._on_running_select.call_count == 1
