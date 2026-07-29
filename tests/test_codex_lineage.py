"""App-level identity tests for Codex rewind/fork lineages."""
from __future__ import annotations

from pathlib import Path

from railmux.favorites import Favorites
from railmux.models import Project
from railmux.ui.app import App, _Running


class _LineageIndex:
    def lineage_ids(self, session_id: str, *, refresh: bool = True):
        if session_id in {"root", "middle", "leaf"}:
            return frozenset({"root", "middle", "leaf"})
        return frozenset()


def _app_with_running_root() -> tuple[App, _Running]:
    app = App.__new__(App)
    project = Project(Path("/project"), "-project", Path(), 1, 0.0)
    running = _Running(
        key="root",
        tmux_name="cx-root",
        label="project/conversation",
        project=project,
        session_type="codex",
    )
    app._running = {"root": running}
    app._codex_index = _LineageIndex()
    return app, running


def test_codex_fork_uuid_resolves_to_existing_running_owner() -> None:
    app, running = _app_with_running_root()

    assert app._by_session_id("leaf") is running
    assert app._by_session_id("middle") is running
    assert app._by_session_id("unrelated") is None


def test_running_ids_include_every_codex_lineage_alias() -> None:
    app, _ = _app_with_running_root()

    assert app._running_session_ids() == {"root", "middle", "leaf"}


def test_live_writer_matches_any_rewind_alias() -> None:
    app, _ = _app_with_running_root()

    assert app._codex_writer_matches("root", {"leaf"})
    assert not app._codex_writer_matches("root", {"unrelated"})


def test_existing_root_favorite_marks_visible_leaf(
    tmp_path, monkeypatch,
) -> None:
    favorites_path = tmp_path / "favorites.json"
    monkeypatch.setattr(
        "railmux.favorites._favorites_path", lambda: favorites_path)
    app, _ = _app_with_running_root()
    app._favorites = Favorites()
    app._favorites.set_many({"root"}, True)

    assert app._favorite_ids_for_view() == {"root", "middle", "leaf"}
