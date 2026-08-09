"""Explicit legal baseline for focused white-box App tests."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from railmux import restart_state
from railmux.models import Project
from railmux.restart_state import OuterTmuxIdentity
from railmux.ui.app import App


@pytest.fixture
def isolate_tmux_identity_stamps(monkeypatch, tmp_path):
    """Keep focused App tests away from the developer's real tmux server."""
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_session_user_option",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        App,
        "_portable_state_path",
        staticmethod(lambda: tmp_path / "portable.json"),
    )
    monkeypatch.setattr(
        restart_state,
        "legacy_state_path",
        lambda: tmp_path / "legacy.json",
    )
    monkeypatch.setattr(
        restart_state,
        "cleanup_stale_instances",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        "railmux.ui.app.tmux_health.record_clean_exit",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        "railmux.ui.app.tmux_health.clear_clean_exit",
        lambda: None,
    )
    monkeypatch.setattr(
        "railmux.ui.app.tmux_health.record_soft_exit",
        lambda **_kwargs: True,
    )


def _project(name: str = "test-proj", claude_dir: Path | None = None) -> Project:
    return Project(
        real_path=Path(f"/tmp/{name}"),
        encoded_name=f"-tmp-{name}",
        claude_dir=claude_dir or Path(f"/tmp/{name}/.claude/projects/-tmp-{name}"),
        session_count=3,
        last_activity_ts=1000.0,
    )


def _minimal_app(*, selected_project=None) -> App:
    """Build only the common state explicitly required by these App tests."""
    app = App.__new__(App)
    app._selected_project = selected_project
    app._restart_identity = OuterTmuxIdentity(
        server_digest="a" * 64,
        server_pid=123,
        pane_id="%1",
        session_id="$1",
        window_id="@1",
    )
    app._running = {}
    app._codex_mode = False
    app._claude_home = Path.home() / ".claude"
    app._session_cache = MagicMock()
    app._session_cache.list_sessions.return_value = []
    app._status = MagicMock()
    app._favorites = MagicMock()
    app._favorites.get_ids.return_value = set()
    app._currently_focused_session_meta = MagicMock(return_value=None)
    app._in_history_mode = False
    app._right_pane_claude = None
    app._active_session_id = None
    return app
