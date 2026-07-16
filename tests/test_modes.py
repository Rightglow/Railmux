"""Provider registry and third-mode readiness."""
from pathlib import Path
from unittest.mock import MagicMock

from railmux.modes import (
    CLAUDE_MODE,
    CODEX_MODE,
    AgentMode,
    ModeRegistry,
    ProjectSource,
)
from railmux.ui.app import App


REVIEW_MODE = AgentMode(
    key="review",
    label="Review Agent",
    tmux_prefix="rv-",
    session_type="review",
    project_source=ProjectSource.CLAUDE,
)


def test_registry_cycles_three_modes_in_declared_order():
    registry = ModeRegistry(
        (CLAUDE_MODE, CODEX_MODE, REVIEW_MODE), default_key="claude")

    assert registry.next_key("claude") == "codex"
    assert registry.next_key("codex") == "review"
    assert registry.next_key("review") == "claude"
    assert registry.for_tmux_name("rv-session") is REVIEW_MODE


def test_app_mode_action_uses_registry_instead_of_boolean_toggle():
    registry = ModeRegistry(
        (CLAUDE_MODE, CODEX_MODE, REVIEW_MODE), default_key="claude")
    app = App.__new__(App)
    app._mode_registry = registry
    app._active_mode_key = "codex"
    app._switch_mode = MagicMock()

    app._cycle_mode()

    app._switch_mode.assert_called_once_with("review")


def test_unknown_restored_mode_falls_back_to_registry_default():
    registry = ModeRegistry(
        (CLAUDE_MODE, CODEX_MODE, REVIEW_MODE), default_key="claude")
    app = App.__new__(App)
    app._mode_registry = registry

    assert app._mode_key_from_state({"mode": "removed-provider"}) == "claude"
    assert app._mode_key_from_state({"codex_mode": True}) == "codex"


def test_arbitrary_mode_owns_independent_view_state_and_prefix():
    registry = ModeRegistry(
        (CLAUDE_MODE, CODEX_MODE, REVIEW_MODE), default_key="claude")
    app = App.__new__(App)
    app._mode_registry = registry
    app._active_mode_key = "review"
    app._mode_view_states = {}
    app._projects_pane = MagicMock()
    app._selected_project = None

    state = app._current_mode_view_state()
    state.selected_project_path = Path("/tmp/review")

    assert app._current_mode_key() == "review"
    assert app._mode_view_states["review"].selected_project_path == Path(
        "/tmp/review")
    assert app._session_name("abc") == "rv-abc"
