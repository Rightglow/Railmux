import json
import os
import time

import pytest

from railmux.discovery import list_projects
from railmux.session_cache import SessionCache
from railmux.session_index import _scan_session, _scan_session_incremental
from railmux.session_index import _SessionScanResult


def _make_project(claude_home, tmp_path, write_session_fixture, sessions, name="proj"):
    real = tmp_path / name
    real.mkdir()
    encoded = str(real).replace("/", "-")
    for sid, records in sessions:
        write_session_fixture(encoded, sid, records)
    return list_projects(claude_home)[0]


def test_cache_returns_same_metadata(claude_home, write_session_fixture, tmp_path):
    project = _make_project(
        claude_home,
        tmp_path,
        write_session_fixture,
        [
            (
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                [
                    {"type": "user", "message": {"role": "user", "content": "hi"}},
                    {"type": "ai-title", "aiTitle": "Hello"},
                ],
            ),
        ],
    )
    cache = SessionCache()
    first = cache.list_sessions(project)
    second = cache.list_sessions(project)
    assert [s.session_id for s in first] == [s.session_id for s in second]
    assert first[0].title == second[0].title


def test_cache_skips_reparse_when_mtime_unchanged(
    claude_home, write_session_fixture, tmp_path, monkeypatch
):
    project = _make_project(
        claude_home,
        tmp_path,
        write_session_fixture,
        [
            (
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                [
                    {"type": "user", "message": {"role": "user", "content": "x"}},
                ],
            ),
        ],
    )
    cache = SessionCache()
    cache.list_sessions(project)  # populate

    import builtins

    real_open = builtins.open
    opens: list[str] = []

    def spy_open(path, *args, **kwargs):
        opens.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)

    cache.list_sessions(project)

    jsonl_opens = [p for p in opens if p.endswith(".jsonl")]
    assert jsonl_opens == [], f"expected zero JSONL re-reads, got: {jsonl_opens}"


def test_cache_reparses_when_mtime_changes(
    claude_home, write_session_fixture, tmp_path
):
    project = _make_project(
        claude_home,
        tmp_path,
        write_session_fixture,
        [
            (
                "cccccccc-cccc-cccc-cccc-cccccccccccc",
                [
                    {"type": "user", "message": {"role": "user", "content": "v1"}},
                ],
            ),
        ],
    )
    cache = SessionCache()
    first = cache.list_sessions(project)
    # 1 user record + 1 auto-injected assistant record (see conftest.py).
    assert first[0].message_count == 2

    jsonl = first[0].jsonl_path
    with jsonl.open("a") as f:
        f.write('{"type": "user", "message": {"role": "user", "content": "v2"}}\n')
    new_mtime = time.time() + 1
    os.utime(jsonl, (new_mtime, new_mtime))

    second = cache.list_sessions(project)
    assert second[0].message_count == 3


def test_cache_drops_entries_for_deleted_sessions(
    claude_home, write_session_fixture, tmp_path
):
    project = _make_project(
        claude_home,
        tmp_path,
        write_session_fixture,
        [
            (
                "dddddddd-dddd-dddd-dddd-dddddddddddd",
                [{"type": "user", "message": {"role": "user", "content": "x"}}],
            ),
            (
                "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                [{"type": "user", "message": {"role": "user", "content": "y"}}],
            ),
        ],
    )
    cache = SessionCache()
    first = cache.list_sessions(project)
    assert len(first) == 2

    first[0].jsonl_path.unlink()

    second = cache.list_sessions(project)
    assert len(second) == 1


def test_cache_keeps_other_projects_warm(
    claude_home,
    write_session_fixture,
    tmp_path,
    monkeypatch,
):
    first_project = _make_project(
        claude_home,
        tmp_path,
        write_session_fixture,
        [
            (
                "11111111-1111-1111-1111-111111111111",
                [{"type": "user", "message": {"role": "user", "content": "one"}}],
            )
        ],
        name="one",
    )
    second_project = _make_project(
        claude_home,
        tmp_path,
        write_session_fixture,
        [
            (
                "22222222-2222-2222-2222-222222222222",
                [{"type": "user", "message": {"role": "user", "content": "two"}}],
            )
        ],
        name="two",
    )
    cache = SessionCache()
    cache.list_sessions(first_project)
    cache.list_sessions(second_project)

    monkeypatch.setattr(
        "railmux.session_cache._scan_session_incremental",
        lambda *_args: pytest.fail("other project cache was evicted"),
    )

    assert len(cache.list_sessions(first_project)) == 1
    assert len(cache.list_sessions(second_project)) == 1


def test_append_during_scan_forces_next_poll_rescan(
    claude_home,
    write_session_fixture,
    tmp_path,
    monkeypatch,
):
    project = _make_project(
        claude_home,
        tmp_path,
        write_session_fixture,
        [
            (
                "33333333-3333-3333-3333-333333333333",
                [{"type": "user", "message": {"role": "user", "content": "initial"}}],
            )
        ],
    )
    import railmux.session_cache as cache_module

    real_scan = cache_module._scan_session_incremental
    calls = []

    def scan_then_append(project, path, previous=None):
        result = real_scan(project, path, previous)
        calls.append(path)
        if len(calls) == 1:
            with path.open("a") as stream:
                stream.write('{"type":"ai-title","aiTitle":"Late title"}\n')
        return result

    monkeypatch.setattr(
        cache_module,
        "_scan_session_incremental",
        scan_then_append,
    )
    cache = SessionCache()

    assert cache.list_sessions(project)[0].title != "Late title"
    assert cache.list_sessions(project)[0].title == "Late title"
    assert len(calls) == 2


def test_append_scan_matches_clean_full_scan_and_reads_only_tail(
    claude_home,
    write_session_fixture,
    tmp_path,
    monkeypatch,
):
    sid = "44444444-4444-4444-4444-444444444444"
    project = _make_project(
        claude_home,
        tmp_path,
        write_session_fixture,
        [
            (
                sid,
                [
                    {
                        "type": "user",
                        "message": {"role": "user", "content": "initial"},
                        "gitBranch": "feature/incremental",
                    }
                ],
            )
        ],
    )
    path = project.claude_dir / f"{sid}.jsonl"
    first = _scan_session_incremental(project, path)
    assert first.state is not None
    old_size = path.stat().st_size

    additions = [
        {
            "type": "assistant",
            "message": {
                "id": "new-message",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 12, "output_tokens": 3},
            },
        },
        {"type": "ai-title", "aiTitle": "Incremental title"},
    ]
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        for record in additions:
            stream.write(json.dumps(record) + "\n")

    real_read = os.read
    read_bytes = 0

    def counted_read(fd, size):
        nonlocal read_bytes
        raw = real_read(fd, size)
        read_bytes += len(raw)
        return raw

    monkeypatch.setattr(os, "read", counted_read)
    incremental = _scan_session_incremental(project, path, first.state)
    monkeypatch.setattr(os, "read", real_read)
    clean = _scan_session(project, path)

    assert incremental.meta == clean
    assert incremental.meta is not None
    assert incremental.meta.title == "Incremental title"
    assert incremental.meta.git_branch == "feature/incremental"
    assert incremental.meta.pending_tool is True
    assert read_bytes == path.stat().st_size - old_size


def test_partial_final_record_is_committed_exactly_once(
    claude_home,
    write_session_fixture,
    tmp_path,
):
    sid = "55555555-5555-5555-5555-555555555555"
    project = _make_project(
        claude_home,
        tmp_path,
        write_session_fixture,
        [
            (
                sid,
                [
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": "initial",
                        },
                    }
                ],
            )
        ],
    )
    path = project.claude_dir / f"{sid}.jsonl"
    first = _scan_session_incremental(project, path)
    assert first.state is not None and first.meta is not None
    original_count = first.meta.message_count

    record = json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "partial",
                "stop_reason": "end_turn",
                "usage": {"output_tokens": 7},
            },
        }
    )
    with path.open("a", encoding="utf-8", newline="") as stream:
        stream.write(record)
    partial = _scan_session_incremental(project, path, first.state)
    assert partial.state is not None and partial.meta is not None
    assert partial.meta.message_count == original_count + 1

    with path.open("a", encoding="utf-8", newline="") as stream:
        stream.write("\n")
    committed = _scan_session_incremental(project, path, partial.state)
    assert committed.meta is not None
    assert committed.meta.message_count == original_count + 1
    assert committed.meta.token_total == first.meta.token_total + 7


@pytest.mark.parametrize("mutation", ["truncate", "replace", "same_size"])
def test_non_append_mutation_falls_back_to_full_scan(
    claude_home,
    write_session_fixture,
    tmp_path,
    mutation,
):
    sid = "66666666-6666-6666-6666-666666666666"
    project = _make_project(
        claude_home,
        tmp_path,
        write_session_fixture,
        [
            (
                sid,
                [
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": "old title material",
                        },
                    }
                ],
            )
        ],
    )
    path = project.claude_dir / f"{sid}.jsonl"
    first = _scan_session_incremental(project, path)
    assert first.state is not None
    replacement = path.read_text(encoding="utf-8").replace(
        "old title material",
        "new title material",
    )
    if mutation == "truncate":
        path.write_text(replacement, encoding="utf-8")
    elif mutation == "replace":
        alternate = path.with_suffix(".replacement")
        alternate.write_text(replacement, encoding="utf-8")
        os.replace(alternate, path)
    else:
        assert len(replacement) == path.stat().st_size
        path.write_text(replacement, encoding="utf-8")
    bumped = time.time_ns() + 2_000_000_000
    os.utime(path, ns=(bumped, bumped))

    rescanned = _scan_session_incremental(project, path, first.state)
    clean = _scan_session(project, path)
    assert rescanned.meta == clean
    assert rescanned.meta is not None
    assert rescanned.meta.title == "new title material"


def test_scan_never_changes_provider_file(
    claude_home,
    write_session_fixture,
    tmp_path,
):
    sid = "77777777-7777-7777-7777-777777777777"
    project = _make_project(
        claude_home,
        tmp_path,
        write_session_fixture,
        [
            (
                sid,
                [
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": "read only",
                        },
                    }
                ],
            )
        ],
    )
    path = project.claude_dir / f"{sid}.jsonl"
    before = path.read_bytes(), path.stat().st_mtime_ns
    first = _scan_session_incremental(project, path)
    assert first.state is not None
    _scan_session_incremental(project, path, first.state)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_append_after_checkpoint_mutation_forces_clean_scan(
    claude_home,
    write_session_fixture,
    tmp_path,
):
    sid = "99999999-9999-9999-9999-999999999999"
    project = _make_project(
        claude_home,
        tmp_path,
        write_session_fixture,
        [
            (
                sid,
                [
                    {
                        "type": "user",
                        "message": {"role": "user", "content": "old-value"},
                    }
                ],
            )
        ],
    )
    path = project.claude_dir / f"{sid}.jsonl"
    first = _scan_session_incremental(project, path)
    assert first.state is not None
    changed = path.read_bytes().replace(b"old-value", b"new-value")
    assert len(changed) == path.stat().st_size
    path.write_bytes(changed + b'{"type":"ai-title","aiTitle":"checkpoint fallback"}\n')

    rescanned = _scan_session_incremental(project, path, first.state)

    assert rescanned.meta is not None
    assert rescanned.meta.title == "checkpoint fallback"
    assert rescanned.meta.last_user_message == "new-value"


def test_pending_tool_ages_without_reopening_jsonl(
    claude_home,
    write_session_fixture,
    tmp_path,
    monkeypatch,
):
    sid = "88888888-8888-8888-8888-888888888888"
    project = _make_project(
        claude_home,
        tmp_path,
        write_session_fixture,
        [
            (
                sid,
                [
                    {
                        "type": "assistant",
                        "message": {
                            "id": "tool",
                            "stop_reason": "tool_use",
                            "usage": {"output_tokens": 1},
                        },
                    }
                ],
            )
        ],
    )
    cache = SessionCache()
    first = cache.list_sessions(project)[0]
    assert first.pending_tool is True
    monkeypatch.setattr(
        "railmux.session_cache._scan_session_incremental",
        lambda *_args, **_kwargs: pytest.fail("unchanged JSONL was reopened"),
    )
    monkeypatch.setattr(time, "time", lambda: first.last_mtime + 20)
    assert cache.list_sessions(project)[0].status == "blocked"


def test_transient_incremental_read_failure_keeps_last_coherent_metadata(
    claude_home,
    write_session_fixture,
    tmp_path,
    monkeypatch,
):
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    project = _make_project(
        claude_home,
        tmp_path,
        write_session_fixture,
        [
            (
                sid,
                [
                    {
                        "type": "user",
                        "message": {"role": "user", "content": "stable"},
                    }
                ],
            )
        ],
    )
    cache = SessionCache()
    first = cache.list_sessions(project)[0]
    with first.jsonl_path.open("a", encoding="utf-8") as stream:
        stream.write('{"type":"ai-title","aiTitle":"new"}\n')

    monkeypatch.setattr(
        "railmux.session_cache._scan_session_incremental",
        lambda *_args, **_kwargs: _SessionScanResult((0, 0, 0, 0), None, None),
    )

    retained = cache.list_sessions(project)[0]
    assert retained.title == first.title
    assert retained.message_count == first.message_count


class _StubRenames:
    """Minimal stand-in for railmux.renames.Renames (get() only)."""

    def __init__(self, mapping):
        self._m = mapping

    def get(self, session_id):
        return self._m.get(session_id)


def test_rename_override_overlays_title(claude_home, write_session_fixture, tmp_path):
    # A user rename must win over the JSONL's own ai-title, in both the list
    # and the single-session lookup, and it must not need cache invalidation.
    sid = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    project = _make_project(
        claude_home,
        tmp_path,
        write_session_fixture,
        [
            (
                sid,
                [
                    {"type": "user", "message": {"role": "user", "content": "hi"}},
                    {"type": "ai-title", "aiTitle": "Auto Title"},
                ],
            ),
        ],
    )
    cache = SessionCache(_StubRenames({sid: "My Name"}))

    listed = cache.list_sessions(project)
    assert listed[0].title == "My Name"
    assert listed[0].display_title == "My Name"

    got = cache.get(project, sid)
    assert got is not None and got.title == "My Name"


def test_no_override_keeps_auto_title(claude_home, write_session_fixture, tmp_path):
    sid = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    project = _make_project(
        claude_home,
        tmp_path,
        write_session_fixture,
        [
            (
                sid,
                [
                    {"type": "user", "message": {"role": "user", "content": "hi"}},
                    {"type": "ai-title", "aiTitle": "Auto Title"},
                ],
            ),
        ],
    )
    cache = SessionCache(_StubRenames({}))
    assert cache.list_sessions(project)[0].title == "Auto Title"
