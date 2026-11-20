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
