"""Persistent favorite session tracking.

Favorites are stored as a JSON set of session_id strings under
``~/.config/railmux/favorites.json``.  Session IDs are globally unique
(UUIDs), so we don't need per-project namespacing.
"""
from __future__ import annotations

import json
from pathlib import Path

from railmux.atomic_file import atomic_write_text


def _favorites_path() -> Path:
    return Path.home() / ".config" / "railmux" / "favorites.json"


class Favorites:
    """In-memory set of favorited session IDs, backed by a JSON file."""

    def __init__(self) -> None:
        self._ids: set[str] = set()
        self._path = _favorites_path()
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._ids = set(data)
        except (json.JSONDecodeError, OSError, UnicodeError):
            self._ids = set()

    def _save(self) -> None:
        try:
            atomic_write_text(
                self._path, json.dumps(sorted(self._ids), indent=2))
        except OSError:
            pass

    def is_favorite(self, session_id: str) -> bool:
        return session_id in self._ids

    def toggle(self, session_id: str) -> bool:
        """Toggle favorite status. Returns the new state (True = favorited)."""
        if session_id in self._ids:
            self._ids.discard(session_id)
            self._save()
            return False
        else:
            self._ids.add(session_id)
            self._save()
            return True

    def set_many(self, session_ids: set[str], favorite: bool) -> None:
        """Set one logical session's provider aliases with a single write."""
        if favorite:
            updated = self._ids | session_ids
        else:
            updated = self._ids - session_ids
        if updated == self._ids:
            return
        self._ids = updated
        self._save()

    def get_ids(self) -> set[str]:
        return set(self._ids)
