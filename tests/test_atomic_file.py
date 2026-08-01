"""Tests for atomic railmux state-file writes."""
import os

import pytest

from railmux.atomic_file import atomic_write_text


def test_atomic_write_creates_parent_and_replaces_content(tmp_path):
    path = tmp_path / "nested" / "state.json"

    atomic_write_text(path, "first")
    atomic_write_text(path, "second")

    assert path.read_text() == "second"


def test_atomic_write_failure_preserves_original_and_cleans_temp(
    tmp_path, monkeypatch,
):
    path = tmp_path / "state.json"
    path.write_text("original")

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        atomic_write_text(path, "replacement")

    assert path.read_text() == "original"
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_write_fsyncs_file_before_replace_and_parent_after(
    tmp_path, monkeypatch,
):
    path = tmp_path / "state.json"
    events = []
    real_fsync = os.fsync
    real_replace = os.replace

    def observe_fsync(fd):
        events.append(("fsync", fd))
        return real_fsync(fd)

    def observe_replace(source, target):
        events.append(("replace", target))
        return real_replace(source, target)

    monkeypatch.setattr(os, "fsync", observe_fsync)
    monkeypatch.setattr(os, "replace", observe_replace)

    atomic_write_text(path, "durable")

    assert path.read_text() == "durable"
    assert [event[0] for event in events] == ["fsync", "replace", "fsync"]


def test_file_fsync_failure_preserves_original_and_cleans_temp(
    tmp_path, monkeypatch,
):
    path = tmp_path / "state.json"
    path.write_text("original")
    replaced = False

    def fail_fsync(_fd):
        raise OSError("file fsync failed")

    def observe_replace(_source, _target):
        nonlocal replaced
        replaced = True

    monkeypatch.setattr(os, "fsync", fail_fsync)
    monkeypatch.setattr(os, "replace", observe_replace)

    with pytest.raises(OSError, match="file fsync failed"):
        atomic_write_text(path, "replacement")

    assert not replaced
    assert path.read_text() == "original"
    assert list(tmp_path.iterdir()) == [path]


def test_parent_directory_fsync_failure_is_nonfatal(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    real_fsync = os.fsync
    calls = 0

    def fail_second_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync unsupported")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_second_fsync)

    atomic_write_text(path, "replacement")

    assert calls == 2
    assert path.read_text() == "replacement"


def test_parent_directory_open_failure_is_nonfatal(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    real_open = os.open

    def fail_directory_open(target, flags, *args, **kwargs):
        if target == path.parent:
            raise OSError("directory open unsupported")
        return real_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_directory_open)

    atomic_write_text(path, "replacement")

    assert path.read_text() == "replacement"
