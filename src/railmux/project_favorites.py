"""Persistent favorite project tracking.

Project favorites are deliberately separate from session favorites.  A
project is identified by the absolute path Railmux already discovered, while
session favorites are provider UUIDs and retain their released JSON schema.
"""
from __future__ import annotations

import json
from pathlib import Path

from railmux.atomic_file import atomic_write_text


_MAX_FAVORITES = 10_000
_MAX_PATH_LENGTH = 4096


def _project_favorites_path() -> Path:
    return Path.home() / ".config" / "railmux" / "project-favorites.json"


def _path_key(path: Path) -> str:
    """Return the stable absolute spelling used by project discovery."""
    return str(path if path.is_absolute() else path.absolute())


class ProjectFavorites:
    """In-memory set of favorite absolute project paths, backed by JSON."""

    def __init__(self) -> None:
        self._paths: set[str] = set()
        self._path = _project_favorites_path()
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if (
                not isinstance(data, list)
                or len(data) > _MAX_FAVORITES
                or any(
                    not isinstance(value, str)
                    or not value
                    or "\x00" in value
                    or len(value) > _MAX_PATH_LENGTH
                    or not Path(value).is_absolute()
                    for value in data
                )
            ):
                return
            self._paths = set(data)
        except (json.JSONDecodeError, OSError, UnicodeError):
            self._paths = set()

    def _save(self) -> None:
        try:
            atomic_write_text(
                self._path,
                json.dumps(sorted(self._paths), ensure_ascii=False, indent=2),
            )
        except OSError:
            pass

    def is_favorite(self, path: Path) -> bool:
        return _path_key(path) in self._paths

    def set(self, path: Path, favorite: bool) -> None:
        key = _path_key(path)
        updated = self._paths | {key} if favorite else self._paths - {key}
        if updated == self._paths:
            return
        self._paths = updated
        self._save()

    def get_paths(self) -> set[Path]:
        return {Path(value) for value in self._paths}
