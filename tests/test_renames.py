"""Tests for ccmgr.renames — the user-rename sidecar store."""
from __future__ import annotations

import json

import pytest

from ccmgr.renames import Renames


@pytest.fixture
def renames(tmp_path, monkeypatch):
    """A Renames store backed by a throwaway JSON file."""
    path = tmp_path / "renames.json"
    monkeypatch.setattr("ccmgr.renames._renames_path", lambda: path)
    return Renames(), path


def test_set_get_round_trip(renames):
    store, _ = renames
    assert store.get("sid-1") is None
    store.set("sid-1", "My Session")
    assert store.get("sid-1") == "My Session"


def test_set_persists_to_disk(renames):
    store, path = renames
    store.set("sid-1", "中文标题")
    # A fresh store reads the same value back (and Chinese stays unescaped).
    assert "中文标题" in path.read_text()
    assert Renames().get("sid-1") == "中文标题"


def test_set_strips_and_empty_clears(renames):
    store, _ = renames
    store.set("sid-1", "  Trimmed  ")
    assert store.get("sid-1") == "Trimmed"
    store.set("sid-1", "   ")  # whitespace-only → clear
    assert store.get("sid-1") is None


def test_clear_removes_and_persists(renames):
    store, path = renames
    store.set("sid-1", "Name")
    store.clear("sid-1")
    assert store.get("sid-1") is None
    assert Renames().get("sid-1") is None
    # Clearing a missing key is a no-op, not an error.
    store.clear("never-existed")


def test_load_ignores_malformed_file(tmp_path, monkeypatch):
    path = tmp_path / "renames.json"
    path.write_text("{ this is not json")
    monkeypatch.setattr("ccmgr.renames._renames_path", lambda: path)
    store = Renames()  # must not raise
    assert store.get("anything") is None


def test_load_skips_non_string_values(tmp_path, monkeypatch):
    path = tmp_path / "renames.json"
    path.write_text(json.dumps({"good": "Title", "bad": 42, "empty": ""}))
    monkeypatch.setattr("ccmgr.renames._renames_path", lambda: path)
    store = Renames()
    assert store.get("good") == "Title"
    assert store.get("bad") is None
    assert store.get("empty") is None
