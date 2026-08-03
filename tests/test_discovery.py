import json
from pathlib import Path

import pytest

from railmux.discovery import list_projects


def test_list_projects_empty_when_no_claude_dir(tmp_path):
    fake_home = tmp_path / "no-claude"
    assert list_projects(fake_home) == []


def test_list_projects_empty_when_no_projects(claude_home):
    assert list_projects(claude_home) == []


def test_list_projects_returns_one(claude_home, write_session_fixture, tmp_path):
    # Make a real dir on disk so the path codec can decode unambiguously.
    real = tmp_path / "real_project"
    real.mkdir()
    encoded = str(real).replace("/", "-")
    write_session_fixture(encoded, "00000000-0000-0000-0000-000000000001", [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
    ])

    projects = list_projects(claude_home)
    assert len(projects) == 1
    assert projects[0].real_path == real
    assert projects[0].session_count == 1
    assert projects[0].last_activity_ts > 0


def test_list_projects_sorted_by_recency(claude_home, write_session_fixture, tmp_path):
    import time

    real_a = tmp_path / "alpha"
    real_b = tmp_path / "beta"
    real_a.mkdir()
    real_b.mkdir()
    enc_a = str(real_a).replace("/", "-")
    enc_b = str(real_b).replace("/", "-")

    write_session_fixture(enc_a, "11111111-1111-1111-1111-111111111111", [{"type": "user", "message": {"role": "user", "content": "a"}}])
    time.sleep(0.05)
    write_session_fixture(enc_b, "22222222-2222-2222-2222-222222222222", [{"type": "user", "message": {"role": "user", "content": "b"}}])

    projects = list_projects(claude_home)
    assert [p.real_path for p in projects] == [real_b, real_a]


def test_list_projects_skips_missing_dir(claude_home, write_session_fixture, tmp_path):
    """Projects whose decoded directory no longer exists on disk are not listed."""
    gone = tmp_path / "deleted_project"  # deliberately never created on disk
    encoded = str(gone).replace("/", "-")
    write_session_fixture(encoded, "33333333-3333-3333-3333-333333333333", [
        {"type": "user", "message": {"role": "user", "content": "x"}},
    ])
    assert list_projects(claude_home) == []


def test_path_cache_persists_and_is_reused(claude_home, write_session_fixture, tmp_path, monkeypatch):
    """Second scan resolves via the persistent cache without calling decode()."""
    import railmux.discovery as discovery

    real = tmp_path / "cached_project"
    real.mkdir()
    encoded = str(real).replace("/", "-")
    write_session_fixture(encoded, "44444444-4444-4444-4444-444444444444", [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
    ])

    # First scan populates the persistent cache.
    discovery._cache.clear()
    assert [p.real_path for p in discovery.list_projects(claude_home)] == [real]
    assert discovery._load_path_cache().get(encoded) == str(real)

    # Second scan (in-process cache cleared) must NOT call decode — it should
    # resolve straight from the persistent cache.
    discovery._cache.clear()
    monkeypatch.setattr(discovery, "decode", lambda name: (_ for _ in ()).throw(
        AssertionError("decode() should not be called on a cache hit")))
    assert [p.real_path for p in discovery.list_projects(claude_home)] == [real]


def test_path_cache_prunes_vanished_projects(claude_home, write_session_fixture, tmp_path):
    """Cache entries for projects whose dir disappeared are pruned on rescan."""
    import shutil
    import railmux.discovery as discovery

    real = tmp_path / "temp_project"
    real.mkdir()
    encoded = str(real).replace("/", "-")
    proj_entry = write_session_fixture(encoded, "55555555-5555-5555-5555-555555555555", [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
    ])

    discovery._cache.clear()
    discovery.list_projects(claude_home)
    assert encoded in discovery._load_path_cache()

    # Remove the real project dir AND its .claude/projects entry, then rescan.
    real.rmdir()
    shutil.rmtree(proj_entry.parent)
    discovery._cache.clear()
    assert discovery.list_projects(claude_home) == []
    assert encoded not in discovery._load_path_cache()


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_list_projects_excludes_bg_sessions(claude_home, write_session_fixture, tmp_path):
    """session_count must not include bg sessions."""
    real = tmp_path / "mixed_project"
    real.mkdir()
    encoded = str(real).replace("/", "-")
    # Two normal sessions and one bg session.
    write_session_fixture(encoded, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", [
        {"type": "user", "message": {"role": "user", "content": "normal A"}},
    ])
    write_session_fixture(encoded, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", [
        {"type": "user", "message": {"role": "user", "content": "normal B"}},
    ])
    bg_dir = claude_home / "projects" / encoded
    bg_path = bg_dir / "cccccccc-cccc-cccc-cccc-cccccccccccc.jsonl"
    _write_jsonl(bg_path, [
        {"type": "user", "message": {"role": "user", "content": "bg job"}, "sessionKind": "bg"},
    ])

    projects = list_projects(claude_home)
    assert len(projects) == 1
    assert projects[0].session_count == 2  # only the two normal sessions


def test_count_updates_when_existing_stub_becomes_resumable(
        claude_home, tmp_path):
    """Content writes must update count even though projects/ mtime is stable."""
    real = tmp_path / "new_chat"
    real.mkdir()
    encoded = str(real).replace("/", "-")
    project_dir = claude_home / "projects" / encoded
    project_dir.mkdir()
    session = project_dir / "11111111-2222-3333-4444-555555555555.jsonl"
    session.write_text('{"type":"ai-title","aiTitle":"Starting"}\n')

    parent_mtime = (claude_home / "projects").stat().st_mtime_ns
    assert list_projects(claude_home)[0].session_count == 0
    with session.open("a") as f:
        f.write('{"type":"user","message":{"content":"hello"}}\n')
        f.write(
            '{"type":"assistant","message":{"stop_reason":"end_turn"}}\n')
    assert (claude_home / "projects").stat().st_mtime_ns == parent_mtime
    assert list_projects(claude_home)[0].session_count == 1


def test_count_updates_when_last_session_file_is_deleted(
        claude_home, write_session_fixture, tmp_path):
    real = tmp_path / "deleted_chat"
    real.mkdir()
    encoded = str(real).replace("/", "-")
    session = write_session_fixture(
        encoded, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        [{"type": "user", "message": {"content": "hello"}}],
    )
    parent_mtime = (claude_home / "projects").stat().st_mtime_ns
    assert list_projects(claude_home)[0].session_count == 1
    session.unlink()
    assert (claude_home / "projects").stat().st_mtime_ns == parent_mtime
    assert list_projects(claude_home)[0].session_count == 0


def test_valid_append_does_not_reparse_whole_jsonl(
        claude_home, write_session_fixture, tmp_path, monkeypatch):
    import railmux.discovery as discovery

    real = tmp_path / "active_chat"
    real.mkdir()
    encoded = str(real).replace("/", "-")
    session = write_session_fixture(
        encoded, "11111111-aaaa-bbbb-cccc-222222222222",
        [{"type": "user", "message": {"content": "hello"}}],
    )
    assert discovery.list_projects(claude_home)[0].session_count == 1
    monkeypatch.setattr(
        discovery, "_scan_session",
        lambda *_args: pytest.fail("valid append reparsed the full JSONL"),
    )
    with session.open("a") as f:
        f.write(
            '{"type":"assistant","message":{"stop_reason":"end_turn"}}\n')
    assert discovery.list_projects(claude_home)[0].session_count == 1


def test_windows_persistent_validity_avoids_unchanged_jsonl_reads(
    claude_home, write_session_fixture, tmp_path, monkeypatch
):
    import railmux.discovery as discovery

    monkeypatch.setenv("RAILMUX_WINDOWS_RUNTIME", "msys2")
    real = tmp_path / "persistent_project"
    real.mkdir()
    encoded = str(real).replace("/", "-")
    write_session_fixture(
        encoded,
        "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb",
        [{"type": "user", "message": {"content": "hello"}}],
    )
    assert discovery.list_projects(claude_home)[0].session_count == 1
    cache_path = discovery._validity_cache_file(claude_home)
    rendered = cache_path.read_text(encoding="utf-8")
    assert "hello" not in rendered
    assert "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb" in rendered

    # Model a new Railmux process. An exact signature can reuse the boolean
    # classification without opening provider-owned content.
    discovery._cache.clear()
    discovery._session_validity.clear()
    discovery._persistent_validity_loaded.clear()
    discovery._persistent_validity_exact_only.clear()
    discovery._persistent_validity_dirty.clear()
    monkeypatch.setattr(
        discovery,
        "_scan_session",
        lambda *_args: pytest.fail("unchanged JSONL content was reopened"),
    )

    assert discovery.list_projects(claude_home)[0].session_count == 1


def test_windows_persistent_validity_never_trusts_changed_session_content(
    claude_home, write_session_fixture, tmp_path, monkeypatch
):
    import railmux.discovery as discovery

    monkeypatch.setenv("RAILMUX_WINDOWS_RUNTIME", "msys2")
    real = tmp_path / "changed_project"
    real.mkdir()
    encoded = str(real).replace("/", "-")
    session = write_session_fixture(
        encoded,
        "cccccccc-1111-2222-3333-dddddddddddd",
        [{"type": "user", "message": {"content": "hello"}}],
    )
    assert discovery.list_projects(claude_home)[0].session_count == 1

    discovery._cache.clear()
    discovery._session_validity.clear()
    discovery._persistent_validity_loaded.clear()
    discovery._persistent_validity_exact_only.clear()
    discovery._persistent_validity_dirty.clear()
    with session.open("a", encoding="utf-8") as stream:
        stream.write('{"type":"assistant","message":{"stop_reason":"end_turn"}}\n')
    calls = []
    original = discovery._scan_session

    def scan(*args):
        calls.append(args[1])
        return original(*args)

    monkeypatch.setattr(discovery, "_scan_session", scan)

    assert discovery.list_projects(claude_home)[0].session_count == 1
    assert calls == [session]


def test_windows_persistent_validity_ignores_nonprivate_cache(
    claude_home, write_session_fixture, tmp_path, monkeypatch
):
    import railmux.discovery as discovery

    monkeypatch.setenv("RAILMUX_WINDOWS_RUNTIME", "msys2")
    real = tmp_path / "private_cache_project"
    real.mkdir()
    encoded = str(real).replace("/", "-")
    session = write_session_fixture(
        encoded,
        "eeeeeeee-1111-2222-3333-ffffffffffff",
        [{"type": "user", "message": {"content": "hello"}}],
    )
    info = session.stat()
    cache_path = discovery._validity_cache_file(claude_home)
    cache_path.write_text(json.dumps({
        "schema": 1,
        "records": [[
            encoded,
            session.name,
            info.st_ino,
            info.st_mtime_ns,
            info.st_size,
            True,
        ]],
    }), encoding="utf-8")
    cache_path.chmod(0o644)
    calls = []
    original = discovery._scan_session

    def scan(*args):
        calls.append(args[1])
        return original(*args)

    monkeypatch.setattr(discovery, "_scan_session", scan)

    assert discovery.list_projects(claude_home)[0].session_count == 1
    assert calls == [session]


def test_replaced_valid_jsonl_is_rescanned(
        claude_home, write_session_fixture, tmp_path):
    real = tmp_path / "replaced_chat"
    real.mkdir()
    encoded = str(real).replace("/", "-")
    session = write_session_fixture(
        encoded, "33333333-aaaa-bbbb-cccc-444444444444",
        [{"type": "user", "message": {"content": "hello"}}],
    )
    assert list_projects(claude_home)[0].session_count == 1
    replacement = session.with_suffix(".replacement")
    replacement.write_text('{"type":"ai-title","aiTitle":"stub"}\n')
    replacement.replace(session)
    assert list_projects(claude_home)[0].session_count == 0
