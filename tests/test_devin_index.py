from pathlib import Path
from unittest.mock import MagicMock

import pytest

from railmux import devin_index, tmux_ctl
from railmux.models import SessionMeta
from railmux.restart_state import OuterTmuxIdentity
from railmux.ui.app import App, _Running
from railmux import orphan_marker


def test_devin_index_paginates_without_spawning_or_loading_sessions(tmp_path):
    script = tmp_path / "fake-devin"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "assert sys.argv[1:] == ['acp', '--process-model', 'in-process']\n"
        "for line in sys.stdin:\n"
        "    r = json.loads(line)\n"
        "    if r['method'] == 'initialize':\n"
        "        result = {'protocolVersion': 1}\n"
        "    else:\n"
        "        assert r['method'] == 'session/list'\n"
        "        cursor = r['params'].get('cursor')\n"
        "        a = {'sessionId': 'quiet-birch', 'cwd': '/one',\n"
        "             'title': 'Title\\nline', 'updatedAt': '2026-11-19T12:00:00Z',\n"
        "             '_meta': {'cognition.ai/isLocked': True,\n"
        "                       'cognition.ai/lockHolderPid': 123}}\n"
        "        b = {'sessionId': 'bright-river', 'cwd': '/two'}\n"
        "        empty = {'sessionId': 'unused-session', 'cwd': '/two',\n"
        "                 '_meta': {'cognition.ai/hasUserMessages': False}}\n"
        "        result = ({'sessions': [b, empty]} if cursor else\n"
        "                  {'sessions': [a], 'nextCursor': 'second'})\n"
        "    print(json.dumps({'id': r['id'], 'result': result}), flush=True)\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    result = devin_index.scan(str(script))
    assert list(result) == ["quiet-birch", "bright-river"]
    assert result["quiet-birch"].meta.title == "Title line"
    assert result["quiet-birch"].holder_pid == 123
    assert result["bright-river"].meta.project.real_path == Path("/two")


def test_devin_index_retains_snapshot_after_provider_failure(monkeypatch):
    index = devin_index.DevinIndex("devin")
    index.snapshot = {}
    monkeypatch.setattr(devin_index, "scan", MagicMock(side_effect=ValueError))
    index._refresh()
    assert index.snapshot == {}
    assert index.take_warning()
    assert not index.take_warning()


def test_devin_resume_requires_an_exact_non_option_id():
    assert devin_index.command("devin", "quiet-birch") == [
        "devin", "--process-model", "in-process", "--resume", "quiet-birch",
    ]
    with pytest.raises(ValueError, match="Invalid Devin session ID"):
        devin_index.command("devin", "--cloud")


def test_devin_placeholder_only_binds_a_session_locked_by_its_pane(monkeypatch):
    cwd = Path("/project")
    project = devin_index.project_for(cwd)
    key = "__new__-123456-1"
    name = "dv-new---123456-1"
    marker = orphan_marker.Marker(
        "devin", key, name, "$4", "%8",
        OuterTmuxIdentity("a" * 64, 4, "%1", "$1", "@1"),
        cwd, 1.0, "b" * 32, "unresolved",
    )
    running = _Running(
        key, name, "project/(new)", project, placeholder_path=cwd,
        created_at=1.0, session_type="devin", orphan=marker,
    )
    index = devin_index.DevinIndex("devin")
    other = SessionMeta(project, "other-writer", Path(), "Other", 0, 0, 4.0,
                        session_type="devin")
    ours = SessionMeta(project, "quiet-birch", Path(), "Ours", 0, 0, 5.0,
                       session_type="devin")
    index.snapshot = {other.session_id: devin_index.DevinSession(other, True, 91)}
    app = App.__new__(App)
    app._running = {key: running}
    app._devin_index = index
    app._renames = MagicMock()
    app._renames.get.return_value = None
    app._exact_running_pane = lambda _r: tmux_ctl.PaneIdentity(
        "%8", 50, name, "$4", "@4", False, 80, 24,
    )
    monkeypatch.setattr(tmux_ctl, "descendant_pids", lambda _pid: [51])
    app._write_orphan_marker = MagicMock(return_value=True)
    app._stamp_running = MagicMock()
    app._record_session_lease_result = MagicMock()
    app._ensure_running_session_lease = MagicMock(return_value=True)
    app._resolve_placeholders([project])
    assert list(app._running) == [key]
    app._write_orphan_marker.assert_not_called()

    index.snapshot[ours.session_id] = devin_index.DevinSession(ours, True, 51)
    app._resolve_placeholders([project])
    assert list(app._running) == [ours.session_id]
    assert app._running[ours.session_id].orphan == marker.resolved(ours.session_id)


def test_devin_delete_command_validates_id():
    assert devin_index.delete_command("devin", "quiet-birch") == [
        "devin", "rm", "--force", "--process-model", "in-process", "quiet-birch",
    ]
    with pytest.raises(ValueError, match="Invalid Devin session ID"):
        devin_index.delete_command("devin", "--force")


def test_devin_index_invalidate_and_mark_unlocked_prevent_stale_resurrection(monkeypatch):
    import threading

    project = devin_index.project_for(Path("/project"))
    a = SessionMeta(project, "quiet-birch", Path(), "A", 0, 0, 5.0, session_type="devin")
    b = SessionMeta(project, "bright-river", Path(), "B", 0, 0, 6.0, session_type="devin")
    index = devin_index.DevinIndex("devin")
    index.snapshot = {
        a.session_id: devin_index.DevinSession(a, True, 42),
        b.session_id: devin_index.DevinSession(b, False, None),
    }

    # mark_unlocked clears stale locked state immediately.
    index.mark_unlocked("quiet-birch")
    assert index.get("quiet-birch").locked is False
    assert index.get("quiet-birch").holder_pid is None

    # A stale scan that started before invalidate() must not resurrect a deleted ID.
    scan_started = threading.Event()
    release_first_scan = threading.Event()
    calls = 0

    def fake_scan(_binary):
        nonlocal calls
        calls += 1
        if calls == 1:
            scan_started.set()
            release_first_scan.wait(timeout=5)
            return {
                a.session_id: devin_index.DevinSession(a, False, None),
                b.session_id: devin_index.DevinSession(b, False, None),
            }
        return {b.session_id: devin_index.DevinSession(b, False, None)}

    monkeypatch.setattr(devin_index, "scan", fake_scan)
    monkeypatch.setattr(devin_index.shutil, "which", lambda _cmd: "/usr/bin/devin")
    index.refresh(force=True)
    assert scan_started.wait(timeout=5)
    index.invalidate(deleted_ids={"quiet-birch"})
    assert list(index.snapshot) == ["bright-river"]
    release_first_scan.set()
    if index._thread is not None:
        index._thread.join(timeout=5)
    # Wait for the follow-up rescan if spawned.
    if index._thread is not None:
        index._thread.join(timeout=5)
    assert list(index.snapshot) == ["bright-river"]
    assert calls == 2


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (
            [
                " ✓ Read ./notes/README.md · 39 lines",
                "",
                "⠀⠸ Running tools · 45s (esc twice to interrupt)",
                "────────────────────────────────────────────── Session Title ─",
                "❭ Guide Devin while it works",
                "──────────────────────────────────────────────────────────────",
                "Gemini 4 Neon High · bypass          Context: 284k / 1.0M tokens",
            ],
            "busy",
        ),
        (
            [
                " ⢀⣀ Thinking · 3m 14s (esc twice to interrupt) · (2998c)",
                "──────────────────────────────────────────────────────────────",
                "❭ 补充：一些草稿",
                "──────────────────────────────────────────────────────────────",
                "Gemini 4 Neon High · bypass          ctrl+o details · alt+t thinking",
            ],
            "busy",
        ),
        (
            [
                "──────────────────────────────────────────────────────────────",
                "❭ hello",
                "──────────────────────────────────────────────────────────────",
                "Gemini 4 Neon High                                Starting session…",
            ],
            "busy",
        ),
        (
            [
                " Permission to run the command `rm -rf build`",
                "  1. Allow (Approve once)",
                "  2. Accept edits mode",
                "  3. Bypass mode",
                "  4. Deny",
            ],
            "blocked",
        ),
        (
            [
                " Devin needs input",
                "  Choose an option to continue",
            ],
            "blocked",
        ),
        (
            [
                "⠀⠸ Running tools · 70m 30s",
                "─────────────────────────────────────────────── /btw 3/3 ──",
                "❭ side question",
                *(f" btw answer line {i}" for i in range(1, 20)),
                "❭ Ask a /btw…",
                "───────────────────────────────────────────────────────────",
                "↵ ask · ↑↓ scroll/history · esc back to chat",
            ],
            "busy",
        ),
        (
            [
                " ✓ Completed task",
                "─────────────────────────────────────────────── /btw 3/3 ──",
                "❭ side question",
                " btw answer",
                "❭ Ask a /btw…",
                "───────────────────────────────────────────────────────────",
                "↵ ask · ↑↓ scroll/history · esc back to chat",
            ],
            "idle",
        ),
        # Negative preservation: quoted busy/blocked phrases in earlier
        # conversation text above an idle prompt box must still report idle.
        (
            [
                " The spinner text is `(esc twice to interrupt)` and placeholder is `Guide Devin while it works`.",
                " Permission to run the command `foo` (Approve once) was discussed earlier.",
                "",
                "────────────────────────────────────────────── Session Title ─",
                "❭ Ask Devin anything…",
                "──────────────────────────────────────────────────────────────",
                "Gemini 4 Neon High · bypass          Context: 284k / 1.0M tokens",
            ],
            "idle",
        ),
    ],
)
def test_devin_classify_pane_status_positive_and_negative(lines, expected):
    assert devin_index.classify_pane_status(lines) == expected


def test_devin_delete_context_menu_and_worker(monkeypatch):
    import threading
    from types import SimpleNamespace
    from railmux.config import Config
    from railmux.ui import app as app_mod

    project = devin_index.project_for(Path("/project"))
    meta = SessionMeta(project, "quiet-birch", Path(), "My Devin chat", 0, 0, 5.0,
                       session_type="devin")
    index = devin_index.DevinIndex("devin")
    index.snapshot = {meta.session_id: devin_index.DevinSession(meta, False, None)}

    app = App.__new__(App)
    app._config = Config(devin_binary="devin")
    app._claude_home = Path("/nonexistent")
    app._devin_index = index
    app._codex_index = MagicMock()
    app._session_cache = MagicMock()
    app._running = {}
    app._renames = MagicMock()
    app._renames.get.return_value = None
    app._favorites = MagicMock()
    app._favorites.get_ids.return_value = set()
    app._railmux_pane_id = None
    app._sessions_pane = MagicMock()
    app._delete_lock = threading.Lock()
    app._delete_thread = None
    app._delete_result = None
    app._project_snapshot_at = 1.0
    statuses = []
    app._set_status = lambda text, level="info", **_kw: statuses.append((text, level))
    shown = []
    app._show_overlay = lambda widget, **_kw: shown.append(widget)

    # Right-click context menu on a Devin session includes both Preview and Delete.
    app._open_session_context_menu(meta)
    menu = shown[-1]
    rendered = b"\n".join(menu.render((36, 16)).text).decode("utf-8")
    assert "Delete" in rendered
    assert "Preview" in rendered

    # Worker runs exact `devin rm --force --process-model in-process quiet-birch`.
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(devin_index.shutil, "which", lambda _cmd: None)
    task = app_mod._DeleteTask(
        session_id=meta.session_id,
        jsonl_path=Path(),
        tmux_name=None,
        label=meta.display_title,
        session_type="devin",
        pane_identity=None,
        writer_pids=(),
        lineage_ids=frozenset(),
        claude_home=Path("/nonexistent"),
    )
    result = app._execute_delete_task(task)
    assert captured["argv"] == [
        "devin", "rm", "--force", "--process-model", "in-process", "quiet-birch",
    ]
    assert captured["cwd"] == Path.home()
    assert result.invalidate_devin is True
    assert result.deleted_devin_ids == frozenset({"quiet-birch"})
    with app._delete_lock:
        app._delete_result = result
    assert app._consume_delete_result() is True
    assert index.get("quiet-birch") is None
    assert statuses[-1] == ("Deleted: My Devin chat", "info")

    # Non-zero exit reports error without dropping the session.
    index.snapshot = {meta.session_id: devin_index.DevinSession(meta, False, None)}
    monkeypatch.setattr("subprocess.run", lambda *_a, **_kw: SimpleNamespace(returncode=1))
    fail = app._execute_delete_task(task)
    assert fail.status_level == "error"
    assert fail.deleted_devin_ids == frozenset()
    assert index.get("quiet-birch") is not None


def test_devin_delete_refuses_externally_locked_session(monkeypatch):
    import threading
    from railmux.config import Config
    from railmux.ui import app as app_mod

    project = devin_index.project_for(Path("/project"))
    meta = SessionMeta(project, "quiet-birch", Path(), "Locked chat", 0, 0, 5.0,
                       session_type="devin")
    index = devin_index.DevinIndex("devin")
    index.snapshot = {meta.session_id: devin_index.DevinSession(meta, True, 999)}
    index.refresh = MagicMock()

    app = App.__new__(App)
    app._config = Config(devin_binary="devin")
    app._claude_home = Path("/nonexistent")
    app._devin_index = index
    app._running = {}
    app._delete_lock = threading.Lock()
    app._delete_thread = None
    app._delete_result = None
    statuses = []
    app._set_status = lambda text, level="info", **_kw: statuses.append((text, level))
    app._redraw_focus_state_now = lambda: None
    monkeypatch.setattr(app_mod.session_lease, "acquire", MagicMock(side_effect=AssertionError("should refuse before lease")))

    app._cleanup_session(
        session_id=meta.session_id,
        label=meta.display_title,
        session_type="devin",
    )
    assert any("already open" in text and level == "error" for text, level in statuses)
    index.refresh.assert_called_once_with(force=True)


def test_devin_live_status_reflected_in_effective_status(monkeypatch):
    project = devin_index.project_for(Path("/project"))
    meta = SessionMeta(project, "quiet-birch", Path(), "Live chat", 0, 0, 5.0,
                       session_type="devin")
    running = _Running("quiet-birch", "dv-quiet-birch", "project/Live chat",
                       project, session_type="devin")
    app = App.__new__(App)
    app._running = {"quiet-birch": running}
    app._transport = MagicMock()
    app._transport.displayed_real_pane.return_value = "%72"

    captured_targets = []

    def fake_capture(target):
        captured_targets.append(target)
        return [
            "⠀⠸ Running tools · 12s (esc twice to interrupt)",
            "──────────────────────────────────────────────────────────────",
            "❭ Guide Devin while it works",
            "──────────────────────────────────────────────────────────────",
            "Gemini 4 Neon High · bypass          Context: 100k / 1.0M tokens",
        ]

    monkeypatch.setattr(tmux_ctl, "capture_pane_visible", fake_capture)
    assert app._effective_status(meta) == "busy"
    # Second call inside TTL reuses cached status so Sessions and Running agree.
    assert app._effective_status(meta) == "busy"
    assert captured_targets == ["%72"]


def test_devin_command_bypass_applies_only_to_new_conversations():
    assert devin_index.command("devin") == [
        "devin", "--process-model", "in-process",
    ]
    assert devin_index.command("devin", bypass=True) == [
        "devin", "--process-model", "in-process", "--permission-mode", "dangerous",
    ]
    # Resumed sessions keep their saved mode even if bypass=True is passed.
    assert devin_index.command("devin", "quiet-birch", bypass=True) == [
        "devin", "--process-model", "in-process", "--resume", "quiet-birch",
    ]


def test_devin_bypass_policy_options_and_ask_prompt(tmp_path, monkeypatch):
    from railmux.config import Config
    from railmux.modes import DEVIN_MODE
    from railmux.settings import Settings

    monkeypatch.setenv("HOME", str(tmp_path))
    settings = Settings()
    assert settings.devin_bypass_policy == "never"

    app = App.__new__(App)
    app._config = Config(devin_binary="devin")
    app._settings = settings
    app._loop = object()
    app._devin_bypass_runtime = False
    app._devin_bypass_prompt_handled = False
    app._codex_yolo_runtime = False
    app._codex_yolo_prompt_handled = False
    statuses = []
    app._set_status = lambda text, level="info", **_kw: statuses.append((text, level))
    shown = []
    app._show_overlay = lambda widget, **_kw: shown.append(widget)
    app._close_modal = lambda: None

    # Default ("never") does not prompt and launches without --permission-mode.
    app._maybe_prompt_auto_run(DEVIN_MODE)
    assert shown == []
    assert app._new_agent_command(DEVIN_MODE, Path("/project")) == (
        ["devin", "--process-model", "in-process"], None,
    )

    # "always" launches new Devin conversations in bypass (dangerous) mode.
    assert settings.set_devin_bypass_policy("always")
    assert app._new_agent_command(DEVIN_MODE, Path("/project")) == (
        ["devin", "--process-model", "in-process", "--permission-mode", "dangerous"], None,
    )

    # "ask" prompts once when entering Devin mode; pressing "t" enables for this run only.
    assert settings.set_devin_bypass_policy("ask")
    app._maybe_prompt_auto_run(DEVIN_MODE)
    assert len(shown) == 1
    modal = shown[-1]
    assert b"Start new Devin chats in bypass mode?" in b"\n".join(modal.render((60, 12)).text)
    modal.keypress((60, 12), "t")
    assert app._devin_bypass_enabled() is True
    assert settings.devin_bypass_policy == "ask"
    assert app._new_agent_command(DEVIN_MODE, Path("/project")) == (
        ["devin", "--process-model", "in-process", "--permission-mode", "dangerous"], None,
    )
    # Prompt does not reopen in the same run.
    app._maybe_prompt_auto_run(DEVIN_MODE)
    assert len(shown) == 1


def test_devin_saved_history_preview_json_and_sqlite_fallback(tmp_path, monkeypatch):
    import json
    import sqlite3
    from railmux import transcript

    monkeypatch.setenv("DEVIN_SESSIONS_DIR", str(tmp_path))
    transcripts_dir = tmp_path / "transcripts"
    transcripts_dir.mkdir()
    json_path = transcripts_dir / "quiet-birch.json"
    json_path.write_text(json.dumps({
        "session_id": "quiet-birch",
        "steps": [
            {"source": "system", "message": "<system_info>hidden</system_info>"},
            {"source": "user", "message": "<available_skills>hidden</available_skills>"},
            {"source": "user", "message": "Check FP8 parity"},
            {
                "source": "agent",
                "message": "Parity verified.",
                "tool_calls": [
                    {"function_name": "read", "arguments": {"file_path": "/tmp/a.py"}},
                ],
            },
        ],
    }), encoding="utf-8")

    assert devin_index.preview_source("quiet-birch") == json_path
    rendered = "".join(transcript.format_transcript(json_path, "devin"))
    assert "Check FP8 parity" in rendered
    assert "Parity verified." in rendered
    assert "read: /tmp/a.py" in rendered
    assert "<system_info>" not in rendered
    assert "<available_skills>" not in rendered

    # Fallback to read-only sessions.db when no JSON transcript exists yet.
    db_path = tmp_path / "sessions.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY, session_id TEXT, chat_message TEXT)")
    con.execute(
        "INSERT INTO message_nodes VALUES (1, ?, ?)",
        ("bright-river", json.dumps({"role": "user", "content": "<available_skills>skip</available_skills>"})),
    )
    con.execute(
        "INSERT INTO message_nodes VALUES (2, ?, ?)",
        ("bright-river", json.dumps({"role": "user", "content": "Hello from sqlite"})),
    )
    con.execute(
        "INSERT INTO message_nodes VALUES (3, ?, ?)",
        ("bright-river", json.dumps({"role": "assistant", "content": "Reply from sqlite"})),
    )
    con.commit()
    con.close()

    assert devin_index.preview_source("bright-river") == db_path
    db_rendered = "".join(
        transcript.format_transcript(db_path, "devin", session_id="bright-river")
    )
    assert "Hello from sqlite" in db_rendered
    assert "Reply from sqlite" in db_rendered
    assert "<available_skills>" not in db_rendered

    # Directory containing '#' must still open SQLite strictly read-only via URI escaping.
    hash_dir = tmp_path / "dir#with#hash"
    hash_dir.mkdir()
    hash_db = hash_dir / "sessions.db"
    con = sqlite3.connect(hash_db)
    con.execute("CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY, session_id TEXT, chat_message TEXT)")
    con.execute(
        "INSERT INTO message_nodes VALUES (1, ?, ?)",
        ("hashed-id", json.dumps({"role": "user", "content": "Hash dir prompt"})),
    )
    con.commit()
    con.close()
    hash_rendered = "".join(
        transcript.format_transcript(hash_db, "devin", session_id="hashed-id")
    )
    assert "Hash dir prompt" in hash_rendered
    assert not (tmp_path / "dir").exists()


def test_unread_done_fourth_status_transitions_and_sorting():
    from types import SimpleNamespace

    app = App.__new__(App)
    app._unread_finished = set()
    app._last_live_status = {}
    app._railmux_has_focus = True
    app._workspace = SimpleNamespace(target=SimpleNamespace(agent_tmux_name="dv-other"))

    r_done = _Running("a", "dv-a", "proj/A", session_type="devin", status="busy", last_mtime=10.0)
    r_idle = _Running("b", "dv-b", "proj/B", session_type="devin", status="idle", last_mtime=20.0)
    r_blocked = _Running("c", "dv-c", "proj/C", session_type="devin", status="blocked", last_mtime=5.0)

    # busy -> idle while NOT viewing becomes "done" (cyan unread-finished).
    assert app._record_live_status_transition(r_done, "busy") == "busy"
    assert app._record_live_status_transition(r_done, "idle") == "done"
    assert "dv-a" in app._unread_finished

    # Viewing the pane clears "done" back to ordinary "idle".
    app._mark_running_seen("dv-a")
    assert app._record_live_status_transition(r_done, "idle") == "idle"

    # Negative preservation: busy -> idle while actively focused in the right pane stays "idle".
    app._railmux_has_focus = False
    app._workspace = SimpleNamespace(target=SimpleNamespace(agent_tmux_name="dv-a"))
    assert app._record_live_status_transition(r_done, "busy") == "busy"
    assert app._record_live_status_transition(r_done, "idle") == "idle"

    # Running sort ranks blocked > done > idle even when idle is newer.
    r_done.status = "done"
    app._running = {"b": r_idle, "a": r_done, "c": r_blocked}
    app._running_sort_ts = 0.0
    app._maybe_resort_running()
    assert list(app._running) == ["c", "a", "b"]


def test_sessions_pane_all_agents_shows_provider_on_metadata_line():
    from railmux.models import Project
    from railmux.ui.sessions_pane import SessionsPane

    project = Project(Path("/tmp/workspace"), "-tmp-workspace", Path("/tmp/p"), 2, 100.0)
    s_devin = SessionMeta(project, "d-1", Path(), "Long important Devin title", 4, 500, 200.0,
                          session_type="devin", status="done")
    s_codex = SessionMeta(project, "x-1", Path(), "Long important Codex title", 3, 400, 150.0,
                          session_type="codex", status="idle")
    pane = SessionsPane(on_select=lambda *_a, **_kw: None, provider_label="Devin", boxed=False)
    pane.set_all_agents(True)
    pane.set_sessions(project, [s_devin, s_codex], running_ids={"d-1"})

    rendered = "\n".join(row.decode("utf-8") for row in pane.render((44, 10), focus=True).text)
    assert "Long important Devin title" in rendered
    assert "Devin · done · 4 msg · 500 tok" in rendered
    assert "Codex · " in rendered
    assert pane.sidebar_scope == "All"
    assert pane.sidebar_title == "Sessions workspace"
