from pathlib import Path

from railmux import project_favorites
from railmux.project_favorites import ProjectFavorites


def test_project_favorites_round_trip_absolute_unicode_paths(
    monkeypatch, tmp_path,
):
    state = tmp_path / "project-favorites.json"
    monkeypatch.setattr(
        project_favorites, "_project_favorites_path", lambda: state)
    first = ProjectFavorites()
    path = tmp_path / "中文 project"

    first.set(path, True)
    loaded = ProjectFavorites()

    assert loaded.is_favorite(path)
    assert loaded.get_paths() == {path}
    assert "中文 project" in state.read_text(encoding="utf-8")

    loaded.set(path, False)
    assert not ProjectFavorites().is_favorite(path)


def test_project_favorites_rejects_relative_or_malformed_persisted_values(
    monkeypatch, tmp_path,
):
    state = tmp_path / "project-favorites.json"
    state.write_text('["relative/project", 7]', encoding="utf-8")
    monkeypatch.setattr(
        project_favorites, "_project_favorites_path", lambda: state)

    favorites = ProjectFavorites()

    assert favorites.get_paths() == set()
    assert state.read_text(encoding="utf-8") == '["relative/project", 7]'


def test_project_favorites_normalizes_relative_api_input_to_absolute(
    monkeypatch, tmp_path,
):
    state = tmp_path / "project-favorites.json"
    monkeypatch.setattr(
        project_favorites, "_project_favorites_path", lambda: state)
    monkeypatch.chdir(tmp_path)
    favorites = ProjectFavorites()

    favorites.set(Path("project"), True)

    assert favorites.get_paths() == {tmp_path / "project"}
