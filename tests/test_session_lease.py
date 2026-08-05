from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from railmux import session_lease, tmux_ctl
from railmux.models import Project, SessionMeta
from railmux.ui import app as app_mod
from railmux.ui.app import App, _Running
from railmux.ui.running_pane import RunningEntry, _RunningRow
from railmux.ui.workspace import AgentWorkspace


def test_locked_lease_reports_owner_and_refuses_second_claim(tmp_path) -> None:
    first = session_lease.acquire(tmp_path, "claude", ("session-a",))
    try:
        owner = session_lease.active_owner(
            tmp_path, "claude", ("session-a",))
        assert owner is not None
        assert owner.provider == "claude"
        assert owner.session_id == "session-a"
        with pytest.raises(session_lease.LeaseConflict):
            session_lease.acquire(tmp_path, "claude", ("session-a",))
    finally:
        first.close()


def test_unlocked_stale_record_is_not_an_active_lease(tmp_path) -> None:
    claim = session_lease.acquire(tmp_path, "codex", ("rollout-a",))
    path = claim.files[0][1]
    claim.close()

    assert path.exists()
    assert session_lease.active_owner(
        tmp_path, "codex", ("rollout-a",)) is None


def test_mode_masking_filesystem_can_still_host_a_safe_lease(
    monkeypatch, tmp_path,
) -> None:
    directory = tmp_path / session_lease.LEASE_DIRECTORY
    directory.mkdir(mode=0o700)
    directory.chmod(0o777)
    monkeypatch.setattr(session_lease.Path, "chmod", lambda *_args: None)

    claim = session_lease.acquire(tmp_path, "claude", ("session-a",))
    try:
        assert session_lease.active_owner(
            tmp_path, "claude", ("session-a",)) is not None
    finally:
        claim.close()
    assert directory.stat().st_mode & 0o077


def test_lease_directory_symlink_is_rejected(tmp_path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / session_lease.LEASE_DIRECTORY).symlink_to(
        target, target_is_directory=True)

    with pytest.raises(session_lease.LeaseError, match="unsafe"):
        session_lease.acquire(tmp_path, "claude", ("session-a",))


def test_fallback_process_token_includes_command_identity(monkeypatch) -> None:
    monkeypatch.setattr(
        session_lease.Path, "read_text",
        MagicMock(side_effect=OSError("no procfs")),
    )
    outputs = iter((
        "Tue Aug  4 12:00:00 2026 claude",
        "Tue Aug  4 12:00:00 2026 codex",
    ))
    check = MagicMock(side_effect=lambda *_args, **_kwargs: next(outputs))
    monkeypatch.setattr(session_lease.subprocess, "check_output", check)

    assert session_lease.process_start_token(42) != (
        session_lease.process_start_token(42))
    assert "comm=" in check.call_args_list[0].args[0]


def test_active_owner_lock_service_error_fails_closed(
    monkeypatch, tmp_path,
) -> None:
    claim = session_lease.acquire(tmp_path, "claude", ("session-a",))
    claim.close()

    def unavailable(*_args):
        raise OSError("locking disabled")

    monkeypatch.setattr(session_lease.fcntl, "flock", unavailable)
    with pytest.raises(session_lease.LeaseError, match="locking is unavailable"):
        session_lease.active_owner(tmp_path, "claude", ("session-a",))


def test_acquire_retries_a_transient_probe_collision(monkeypatch, tmp_path) -> None:
    owner = session_lease.LeaseOwner(
        "claude", "session-a", "remote", "probe")
    claim = MagicMock()
    attempts = iter((session_lease.LeaseConflict(owner), claim))

    def acquire_once(*_args):
        result = next(attempts)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(session_lease, "_acquire_once", acquire_once)
    monkeypatch.setattr(session_lease.time, "sleep", lambda _seconds: None)

    assert session_lease.acquire(
        tmp_path, "claude", ("session-a",)) is claim


def test_codex_alias_claim_is_all_or_nothing(tmp_path) -> None:
    parent = session_lease.acquire(tmp_path, "codex", ("parent",))
    try:
        with pytest.raises(session_lease.LeaseConflict):
            session_lease.acquire(
                tmp_path, "codex", ("child", "parent"))
        assert session_lease.active_owner(
            tmp_path, "codex", ("child",)) is None
    finally:
        parent.close()


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="requires procfs")
def test_holder_keeps_lease_after_parent_detaches(tmp_path) -> None:
    pane = subprocess.Popen(["sleep", "30"])
    claim = session_lease.acquire(tmp_path, "claude", ("session-a",))
    try:
        assert session_lease.start_holder(
            claim, pane_id="%9", pane_pid=pane.pid)
        owner = session_lease.active_owner(
            tmp_path, "claude", ("session-a",))
        assert owner is not None
        assert owner.pane_id == "%9"
        assert owner.pane_pid == pane.pid
        assert session_lease.owner_matches_pane(owner, "%9", pane.pid)
    finally:
        pane.terminate()
        pane.wait(timeout=3)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if session_lease.active_owner(
                tmp_path, "claude", ("session-a",)) is None:
            break
        time.sleep(0.05)
    assert session_lease.active_owner(
        tmp_path, "claude", ("session-a",)) is None


def test_hold_cli_rejects_missing_inherited_lease_fd() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "railmux.session_lease",
            "hold",
            "--provider",
            "claude",
            "--instance",
            "test",
            "--pane-id",
            "%1",
            "--pane-pid",
            str(os.getpid()),
            "--process-start",
            session_lease.process_start_token(os.getpid()) or "missing",
            "--lease",
            "session-a:999999",
        ],
        capture_output=True,
        check=False,
        timeout=3,
    )

    assert result.returncode != 0
    assert result.stdout == b""


def test_running_entry_reacquires_after_its_holder_lock_disappears(
    monkeypatch, tmp_path,
) -> None:
    project = Project(tmp_path, "-tmp", tmp_path / "claude-project", 1, 1.0)
    session = SessionMeta(
        project=project,
        session_id="session-a",
        jsonl_path=tmp_path / "session-a.jsonl",
        title="Shared conversation",
        message_count=2,
        token_total=3,
        last_mtime=1.0,
        session_type="claude",
    )
    running = _Running(
        key="session-a",
        tmux_name="cc-session-a",
        label="tmp/Shared conversation",
        project=project,
        session_type="claude",
        lease_session_ids=frozenset({"session-a"}),
    )
    pane = tmux_ctl.PaneIdentity(
        pane_id="%9",
        pane_pid=os.getpid(),
        session_name=running.tmux_name,
        session_id="$9",
        window_id="@9",
        dead=False,
        width=80,
        height=24,
    )
    app = App.__new__(App)
    app._claude_home = tmp_path
    app._codex_real_pane_identity = MagicMock(return_value=pane)
    claim = MagicMock(session_ids=("session-a",))
    acquire = MagicMock(return_value=claim)
    monkeypatch.setattr(session_lease, "active_owner", lambda *_args: None)
    monkeypatch.setattr(session_lease, "acquire", acquire)
    start_holder = MagicMock(return_value=True)
    monkeypatch.setattr(session_lease, "start_holder", start_holder)

    assert app._ensure_running_session_lease(running, session)

    assert running.lease_repair_thread is not None
    running.lease_repair_thread.join(timeout=1.0)

    acquire.assert_called_once_with(tmp_path, "claude", ["session-a"])
    start_holder.assert_called_once_with(
        claim, pane_id=pane.pane_id, pane_pid=pane.pane_pid)


def test_running_lease_failure_stays_visible_on_its_row(
    monkeypatch, tmp_path,
) -> None:
    project = Project(tmp_path, "-tmp", tmp_path / "claude-project", 1, 1.0)
    session = SessionMeta(
        project=project, session_id="session-a",
        jsonl_path=tmp_path / "session-a.jsonl", title="Shared",
        message_count=1, token_total=1, last_mtime=1.0,
        session_type="claude",
    )
    running = _Running(
        key="session-a", tmux_name="cc-session-a", label="tmp/Shared",
        project=project, session_type="claude",
    )
    app = App.__new__(App)
    app._claude_home = tmp_path
    app._codex_real_pane_identity = MagicMock(return_value=None)
    app._set_status = MagicMock()

    assert not app._ensure_running_session_lease(running, session)
    app._record_session_lease_result(running, False)
    row = _RunningRow(RunningEntry(
        running.tmux_name, running.label,
        lease_warning=running.lease_warning))

    assert running.lease_warning == "exact provider pane identity is unavailable"
    assert "⚠ exact provider pane identity is unavailable" in (
        row._wrapped_widget.base_widget.text)


def test_codex_rewind_keeps_one_stable_holder_anchor(
    monkeypatch, tmp_path,
) -> None:
    project = Project(tmp_path, "-tmp", Path(), 1, 1.0)
    session = SessionMeta(
        project=project, session_id="child",
        jsonl_path=tmp_path / "child.jsonl", title="Shared",
        message_count=1, token_total=1, last_mtime=1.0,
        session_type="codex", forked_from_id="parent",
    )
    running = _Running(
        key="child", tmux_name="cx-child", label="tmp/Shared",
        project=project, session_type="codex",
        lease_session_ids=frozenset({"parent"}),
    )
    pane = tmux_ctl.PaneIdentity(
        pane_id="%9", pane_pid=os.getpid(), session_name=running.tmux_name,
        session_id="$9", window_id="@9", dead=False, width=80, height=24,
    )
    token = session_lease.process_start_token(pane.pane_pid)
    assert token is not None
    owner = session_lease.LeaseOwner(
        "codex", "parent", session_lease._bounded_host(), "local",
        pane_id=pane.pane_id, pane_pid=pane.pane_pid, process_start=token,
    )
    app = App.__new__(App)
    app._config = MagicMock()
    app._config.resolved_codex_home.return_value = tmp_path
    app._codex_index = MagicMock()
    app._codex_index.lineage_ids.return_value = {"parent", "child"}
    app._codex_real_pane_identity = MagicMock(return_value=pane)
    active = MagicMock(return_value=owner)
    monkeypatch.setattr(session_lease, "active_owner", active)
    acquire = MagicMock()
    monkeypatch.setattr(session_lease, "acquire", acquire)

    assert app._ensure_running_session_lease(running, session)

    active.assert_called_once_with(tmp_path, "codex", ("parent",))
    acquire.assert_not_called()
    assert running.lease_session_ids == frozenset({"parent"})


def test_launch_closes_claim_when_new_tmux_topology_never_appears(
    monkeypatch, tmp_path,
) -> None:
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._running = {}
    app._session_name = MagicMock(return_value="cc-session-a")
    app._shellify = MagicMock(return_value="claude --resume session-a")
    app._ensure_detached_agent = MagicMock(return_value=(True, None))
    app._set_status = MagicMock()
    claim = MagicMock(session_ids=("session-a",))
    monkeypatch.setattr(app_mod.tmux_ctl, "session_exists", lambda _name: False)
    monkeypatch.setattr(app_mod.tmux_ctl, "session_topology", lambda _name: None)
    killed = MagicMock()
    monkeypatch.setattr(app_mod.tmux_ctl, "kill_session", killed)
    monkeypatch.setattr(app_mod.time, "sleep", lambda _seconds: None)

    assert not app._launch(
        "session-a", ["claude"], tmp_path, "tmp/Shared", None,
        lease_claim=claim)

    claim.close.assert_called_once_with()
    killed.assert_called_once_with("cc-session-a")


def test_resume_releases_claim_when_launch_double_does_not_take_it(
    monkeypatch, tmp_path,
) -> None:
    project = Project(tmp_path, "-tmp", tmp_path / "claude-project", 1, 1.0)
    session = SessionMeta(
        project=project, session_id="session-a",
        jsonl_path=tmp_path / "session-a.jsonl", title="Shared",
        message_count=1, token_total=1, last_mtime=1.0,
        session_type="claude",
    )
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._config = MagicMock(claude_binary="claude")
    app._claude_home = tmp_path
    app._launch = MagicMock(return_value=False)
    app._set_status = MagicMock()
    claim = MagicMock()
    monkeypatch.setattr(session_lease, "acquire", MagicMock(return_value=claim))

    assert not app._launch_resume(session)
    claim.close.assert_called_once_with()


def test_delete_refuses_a_fresh_remote_lease(monkeypatch, tmp_path) -> None:
    app = App.__new__(App)
    app._running = {}
    app._delete_lock = threading.Lock()
    app._delete_thread = None
    app._delete_result = None
    app._loop = None
    app._claude_home = tmp_path
    app._set_status = MagicMock()
    remote = session_lease.LeaseOwner(
        "claude", "session-a", "computelab-303", "remote")
    monkeypatch.setattr(
        session_lease, "acquire",
        MagicMock(side_effect=session_lease.LeaseConflict(remote)),
    )

    app._cleanup_session(
        session_id="session-a", jsonl_path=tmp_path / "session-a.jsonl",
        label="Shared", session_type="claude")

    assert app._delete_thread is None
    assert "computelab-303" in app._set_status.call_args.args[0]
    assert app._set_status.call_args.args[1] == "error"


def test_codex_delete_checks_every_alias_after_matching_local_owner(
    monkeypatch, tmp_path,
) -> None:
    pane = tmux_ctl.PaneIdentity(
        pane_id="%9", pane_pid=os.getpid(), session_name="cx-session-a",
        session_id="$9", window_id="@9", dead=False, width=80, height=24,
    )
    token = session_lease.process_start_token(pane.pane_pid)
    assert token is not None
    local = session_lease.LeaseOwner(
        "codex", "alias-a", session_lease._bounded_host(), "local",
        pane_id=pane.pane_id, pane_pid=pane.pane_pid, process_start=token,
    )
    remote = session_lease.LeaseOwner(
        "codex", "alias-b", "computelab-303", "remote")
    running = _Running(
        key="session-a", tmux_name=pane.session_name, label="tmp/Shared",
        session_type="codex",
    )
    app = App.__new__(App)
    app._running = {"session-a": running}
    app._delete_lock = threading.Lock()
    app._delete_thread = None
    app._delete_result = None
    app._loop = None
    app._config = MagicMock()
    app._config.resolved_codex_home.return_value = tmp_path
    app._codex_lineage_ids = MagicMock(return_value={"alias-a", "alias-b"})
    app._return_agent_before_kill = MagicMock(return_value=True)
    app._set_status = MagicMock()
    monkeypatch.setattr(app_mod.tmux_ctl, "session_exists", lambda _name: True)
    monkeypatch.setattr(app_mod.tmux_ctl, "session_process_ids", lambda _name: ())
    monkeypatch.setattr(
        app_mod.tmux_ctl, "session_topology",
        lambda _name: SimpleNamespace(single_live_pane=pane),
    )
    monkeypatch.setattr(
        session_lease, "acquire",
        MagicMock(side_effect=session_lease.LeaseConflict(local)),
    )
    monkeypatch.setattr(
        session_lease, "active_owner",
        lambda _root, _provider, ids: local if ids == ("alias-a",) else remote,
    )

    app._cleanup_session(
        session_id="session-a", label="Shared", session_type="codex")

    assert app._delete_thread is None
    assert "computelab-303" in app._set_status.call_args.args[0]


@pytest.mark.parametrize("provider", ("claude", "codex"))
def test_resume_refuses_active_cross_host_lease(
    monkeypatch, tmp_path, provider,
) -> None:
    project = Project(tmp_path, "-tmp", tmp_path / "claude-project", 1, 1.0)
    session = SessionMeta(
        project=project,
        session_id="session-a",
        jsonl_path=tmp_path / "session-a.jsonl",
        title="Shared conversation",
        message_count=2,
        token_total=3,
        last_mtime=1.0,
        session_type=provider,
    )
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._claude_home = tmp_path / ".claude"
    app._config = MagicMock(
        claude_binary="claude",
        codex_binary="codex",
    )
    app._config.resolved_codex_home.return_value = tmp_path / ".codex"
    app._codex_index = MagicMock()
    app._codex_index.lineage_ids.return_value = {"session-a", "parent-a"}
    app._launch = MagicMock(return_value=True)
    app._set_status = MagicMock()
    app._codex_yolo_enabled = MagicMock(return_value=False)
    app._codex_env = MagicMock(return_value={"CODEX_HOME": str(tmp_path / ".codex")})
    monkeypatch.setattr(
        "railmux.ui.app.session_lease.acquire",
        MagicMock(side_effect=session_lease.LeaseConflict(
            session_lease.LeaseOwner(
                provider, "session-a", "computelab-303", "remote"))),
    )

    assert not app._launch_resume(session)

    app._launch.assert_not_called()
    assert "computelab-303" in app._set_status.call_args.args[0]
    assert app._set_status.call_args.args[1] == "error"
