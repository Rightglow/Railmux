"""Tests for soft-quit feature: state file, orphan discovery, truncated ID
resolution, QuitConfirmModal s-key, and teardown branching."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from railmux.models import Project, SessionMeta
from railmux import orphan_marker, tmux_ctl
from railmux.modes import CLAUDE_MODE, CODEX_MODE
from railmux.ui.app import App, _Running


from tests.app_test_harness import (
    _minimal_app,
    _project,
    isolate_tmux_identity_stamps as isolate_tmux_identity_stamps,
)


pytestmark = pytest.mark.usefixtures("isolate_tmux_identity_stamps")


def test_discover_orphans_finds_cc_sessions():
    """A cc-* tmux session in a known project is added to _running."""
    proj = _project("myproj")
    full_id = "ae54affd-ec33-465c-b3c4-c1dc7c46990b"
    truncated = App._safe_name(full_id, 16)

    with (
        patch(
            "subprocess.check_output",
            return_value=f"cc-{truncated}\t/tmp/myproj\nrailmux\t/home/user\n",
        ),
        patch("railmux.ui.app.list_projects", return_value=[proj]),
        patch.object(App, "_resolve_truncated_id", return_value=full_id),
    ):
        app = _minimal_app()
        app._discover_orphans()

    assert full_id in app._running
    assert app._running[full_id].tmux_name == f"cc-{truncated}"
    assert app._running[full_id].project is proj


def test_discover_orphans_finds_codex_only_project():
    """A cx-* session without a Claude project is re-adopted through a
    synthetic project built from the Codex index."""
    cwd = Path("/tmp/codex-only")
    full_id = "ae54affd-ec33-465c-b3c4-c1dc7c46990b"
    truncated = App._safe_name(full_id, 16)
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    app._resolve_truncated_codex_id = MagicMock(return_value=full_id)

    with (
        patch("subprocess.check_output", return_value=f"cx-{truncated}\t{cwd}\n"),
        patch("railmux.ui.app.list_projects", return_value=[]),
    ):
        app._discover_orphans()

    running = app._running[full_id]
    assert running.session_type == "codex"
    assert running.project.real_path == cwd
    assert running.project.claude_dir == Path()


def _codex_meta(project: Project, session_id: str) -> SessionMeta:
    return SessionMeta(
        project=project,
        session_id=session_id,
        jsonl_path=Path("/tmp/rollout.jsonl"),
        title="Recovered",
        message_count=1,
        token_total=1,
        last_mtime=1000.0,
        status="idle",
        session_type="codex",
    )


def test_discover_orphans_recovers_codex_placeholder_from_procfs(monkeypatch):
    """A state-free Linux restart re-adopts the exact live rollout writer."""
    cwd = Path("/tmp/codex-only")
    project = _project("codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    meta = _codex_meta(project, session_id)
    app = _minimal_app()
    app._codex_mode = True
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    app._codex_index.get.side_effect = lambda candidate, refresh=False: (
        meta if candidate == session_id else None
    )
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_rollout_ids",
        lambda name, root: {session_id},
    )

    with (
        patch(
            "subprocess.check_output", return_value=f"cx-new---abcdef-1\t{cwd}\t100\n"
        ),
        patch("railmux.ui.app.list_projects", return_value=[]),
    ):
        app._discover_orphans()

    assert app._running[session_id].tmux_name == "cx-new---abcdef-1"
    assert not app._running[session_id].is_placeholder


def test_soft_restart_migrates_idle_pre_marker_codex_session(monkeypatch):
    """An old idle cx-new session remains visible across the next restart.

    Idle Codex closes its rollout fd, so the normal exact fd correlation has
    nothing to bind.  A strict historical launch-command match may preserve it
    as unresolved; the resulting state binding then makes future restarts
    independent of both procfs and the migration path.
    """
    import shlex

    cwd = Path("/tmp/codex-only")
    tmux_name = "cx-new---61404b-6"
    tmux_row = f"{tmux_name}\t{cwd}\t100\t$42\t%9\t\t\n"
    start_command = shlex.quote(
        f"cd {cwd} && exec $SHELL -li -c "
        f"'export CODEX_HOME=/tmp/codex-home && "
        f"exec codex -C {cwd}'"
    )

    app = _minimal_app()
    app._config = MagicMock(codex_binary="codex", claude_binary="claude")
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    monkeypatch.setattr(tmux_ctl, "session_rollout_ids", lambda *_args: set())
    start_probe = MagicMock(return_value=start_command)
    monkeypatch.setattr(tmux_ctl, "detached_single_pane_start_command", start_probe)
    written: list[orphan_marker.Marker] = []
    app._write_orphan_marker = lambda marker: written.append(marker) or True

    with (
        patch("subprocess.check_output", return_value=tmux_row),
        patch("railmux.ui.app.list_projects", return_value=[]),
    ):
        assert app._discover_orphans() is True

    key = "__new__-61404b-6"
    migrated = app._running[key]
    assert migrated.tmux_name == tmux_name
    assert migrated.label.endswith("/(recovering)")
    assert migrated.allow_heuristic_resolution is False
    assert migrated.orphan == written[0]
    assert migrated.orphan.phase == "unresolved"
    assert migrated.orphan.tmux_session_id == "$42"
    assert migrated.orphan.tmux_pane_id == "%9"
    start_probe.assert_called_once_with(tmux_name, session_id="$42", pane_id="%9")

    binding = app._running_binding_data(migrated, include_launch_context=True)
    assert binding is not None
    assert binding["pre_launch_complete"] is False
    state = {
        "running_bindings_version": 1,
        "running_bindings": [binding],
    }

    restarted = _minimal_app()
    restarted._config = app._config
    restarted._codex_index = MagicMock()
    restarted._codex_index.all_cwds.return_value = {cwd: 1}
    restarted._codex_home_path = app._codex_home_path
    start_probe.reset_mock()
    marker_row = (
        f"{tmux_name}\t{cwd}\t100\t$42\t%9\t{orphan_marker.encode(migrated.orphan)}\t\n"
    )
    pane = tmux_ctl.PaneIdentity(
        pane_id="%9",
        pane_pid=999,
        session_name=tmux_name,
        session_id="$42",
        window_id="@42",
        dead=False,
        width=80,
        height=24,
    )
    monkeypatch.setattr(tmux_ctl, "pane_identity", lambda _pane_id: pane)
    with (
        patch("subprocess.check_output", return_value=marker_row),
        patch("railmux.ui.app.list_projects", return_value=[]),
    ):
        assert restarted._discover_orphans(state) is True

    assert restarted._running[key].tmux_name == tmux_name
    assert restarted._running[key].allow_heuristic_resolution is False
    assert restarted._running[key].orphan == migrated.orphan
    start_probe.assert_not_called()


def test_legacy_session_is_not_claimed_when_v2_marker_write_fails(monkeypatch):
    import shlex

    cwd = Path("/tmp/codex-only")
    tmux_name = "cx-new---61404b-6"
    app = _minimal_app()
    app._config = MagicMock(codex_binary="codex", claude_binary="claude")
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    app._write_orphan_marker = MagicMock(return_value=False)
    monkeypatch.setattr(tmux_ctl, "session_rollout_ids", lambda *_args: set())
    monkeypatch.setattr(
        tmux_ctl,
        "detached_single_pane_start_command",
        lambda *_args, **_kwargs: shlex.quote(
            f"cd {cwd} && exec $SHELL -li -c 'exec codex -C {cwd}'"
        ),
    )
    row = f"{tmux_name}\t{cwd}\t100\t$42\t%9\t\t\n"

    with (
        patch("subprocess.check_output", return_value=row),
        patch("railmux.ui.app.list_projects", return_value=[]),
    ):
        assert app._discover_orphans() is True

    assert app._running == {}
    app._write_orphan_marker.assert_called_once()


def test_legacy_command_matcher_accepts_only_historical_launch_grammar():
    import shlex

    cwd = Path("/tmp/codex-only")
    app = _minimal_app()
    app._config = MagicMock(codex_binary="codex", claude_binary="claude")
    assert app._is_legacy_new_session_command(
        shlex.quote(f"cd {cwd} && exec claude"), CLAUDE_MODE, cwd
    )
    command = shlex.quote(
        f"cd {cwd} && exec $SHELL -li -c 'exec codex -C {cwd}; touch /tmp/not-railmux'"
    )

    assert not app._is_legacy_new_session_command(command, CODEX_MODE, cwd)


def test_discover_orphans_restores_persisted_binding_without_procfs(monkeypatch):
    """A validated state binding is the cross-platform soft-restart path."""
    cwd = Path("/tmp/codex-only")
    project = _project("codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    meta = _codex_meta(project, session_id)
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    app._codex_index.get.return_value = meta
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.session_rollout_ids", lambda *a: None)
    state = {
        "running_bindings_version": 1,
        "running_bindings": [
            {
                "key": session_id,
                "tmux_name": "cx-new---abcdef-1",
                "session_type": "codex",
                "cwd": str(cwd),
            }
        ],
    }

    with (
        patch(
            "subprocess.check_output", return_value=f"cx-new---abcdef-1\t{cwd}\t100\n"
        ),
        patch("railmux.ui.app.list_projects", return_value=[]),
    ):
        app._discover_orphans(state)

    assert app._running[session_id].tmux_name == "cx-new---abcdef-1"
    assert app._running[session_id].label.endswith("/Recovered")


def test_soft_restart_keeps_resumed_parent_with_open_background_rollout(
    monkeypatch,
):
    """A completed parent rollout may close while its resumed Codex process
    still owns a busy background rollout; exact resume argv preserves it."""
    cwd = Path("/tmp/codex-only")
    project = _project("codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    background_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    meta = _codex_meta(project, session_id)
    tmux_name = "cx-12345678-1234-12"
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    app._codex_index.get.side_effect = lambda candidate, refresh=False: (
        meta if candidate == session_id else None
    )
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    monkeypatch.setattr(
        tmux_ctl,
        "session_rollout_ids",
        lambda *_args: {background_id},
    )
    exact_arg = MagicMock(return_value=True)
    monkeypatch.setattr(tmux_ctl, "session_process_has_exact_arg", exact_arg)
    binding = {
        "key": session_id,
        "tmux_name": tmux_name,
        "session_type": "codex",
        "cwd": str(cwd),
    }

    running = app._valid_running_binding(
        binding,
        {tmux_name: (cwd, 100)},
        {app._path_key(cwd): project},
    )

    assert running is not None
    assert running.tmux_name == tmux_name
    exact_arg.assert_called_once_with(tmux_name, session_id)


def test_soft_restart_accepts_open_rewind_descendant_as_same_writer(
    monkeypatch,
):
    """A new-session process keeps its original binding while Codex rewind
    moves the live file descriptor to a fork UUID in the same lineage."""
    cwd = Path("/tmp/codex-only")
    project = _project("codex-only")
    root_id = "12345678-1234-1234-1234-1234567890ab"
    leaf_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    root = _codex_meta(project, root_id)
    leaf = replace(
        root,
        session_id=leaf_id,
        title="Latest rewind",
        last_mtime=2000.0,
        forked_from_id=root_id,
    )
    tmux_name = "cx-new---abcdef-1"
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.get.side_effect = lambda candidate, refresh=False: (
        root if candidate == root_id else leaf if candidate == leaf_id else None
    )
    app._codex_index.lineage_ids.return_value = frozenset({root_id, leaf_id})
    app._codex_index.representative_for.return_value = leaf
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    monkeypatch.setattr(tmux_ctl, "session_rollout_ids", lambda *_args: {leaf_id})
    exact_arg = MagicMock(return_value=False)
    monkeypatch.setattr(tmux_ctl, "session_process_has_exact_arg", exact_arg)
    binding = {
        "key": root_id,
        "tmux_name": tmux_name,
        "session_type": "codex",
        "cwd": str(cwd),
    }

    running = app._valid_running_binding(
        binding,
        {tmux_name: (cwd, 100)},
        {app._path_key(cwd): project},
    )

    assert running is not None
    assert running.key == root_id
    assert running.label.endswith("/Latest rewind")
    exact_arg.assert_not_called()


def test_only_live_tmux_binding_migrates_codex_rollback_baseline(monkeypatch):
    cwd = Path("/tmp/codex-only")
    project = _project("codex-only")
    root_id = "12345678-1234-1234-1234-1234567890ab"
    current_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    root = _codex_meta(project, root_id)
    current = replace(
        root,
        session_id=current_id,
        title="Current",
        last_mtime=2000.0,
        forked_from_id=root_id,
        codex_rollback_count=12,
    )
    tmux_name = "cx-new---abcdef-1"
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.get.side_effect = lambda candidate, refresh=False: (
        root if candidate == root_id else current if candidate == current_id else None
    )
    app._codex_index.lineage_ids.return_value = frozenset({root_id, current_id})
    app._codex_index.representative_for.return_value = current
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    monkeypatch.setattr(tmux_ctl, "session_rollout_ids", lambda *_args: {current_id})
    binding = {
        "key": root_id,
        "tmux_name": tmux_name,
        "session_type": "codex",
        "cwd": str(cwd),
        "codex_canonical_session_id": current_id,
    }

    trusted = app._valid_running_binding(
        binding,
        {tmux_name: (cwd, 100)},
        {app._path_key(cwd): project},
        trust_codex_history_state=True,
    )
    cached_only = app._valid_running_binding(
        binding,
        {tmux_name: (cwd, 100)},
        {app._path_key(cwd): project},
    )

    assert trusted is not None and cached_only is not None
    assert trusted.codex_canonical_session_id == current_id
    assert trusted.codex_baseline_rollback_count == 12
    assert cached_only.codex_canonical_session_id is None
    assert cached_only.codex_baseline_rollback_count == 0


@pytest.mark.parametrize("probe_result", [False, None, OSError("denied")])
def test_soft_restart_rejects_other_writer_without_exact_resume_arg(
    monkeypatch,
    probe_result,
):
    """Sibling rollout ids alone cannot weaken stale-writer rejection."""
    cwd = Path("/tmp/codex-only")
    project = _project("codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    tmux_name = "cx-12345678-1234-12"
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    app._codex_index.get.return_value = _codex_meta(project, session_id)
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    monkeypatch.setattr(
        tmux_ctl,
        "session_rollout_ids",
        lambda *_args: {"bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"},
    )

    def exact_arg_probe(*_args):
        if isinstance(probe_result, Exception):
            raise probe_result
        return probe_result

    monkeypatch.setattr(tmux_ctl, "session_process_has_exact_arg", exact_arg_probe)
    binding = {
        "key": session_id,
        "tmux_name": tmux_name,
        "session_type": "codex",
        "cwd": str(cwd),
    }

    assert (
        app._valid_running_binding(
            binding,
            {tmux_name: (cwd, 100)},
            {app._path_key(cwd): project},
        )
        is None
    )


def test_discover_orphans_reuses_initial_project_snapshot():
    project = _project("cached-startup")
    app = _minimal_app(selected_project=project)
    app._project_snapshot = [project]

    with (
        patch("subprocess.check_output", return_value=""),
        patch("railmux.ui.app.list_projects") as scan,
    ):
        assert app._discover_orphans() is True

    scan.assert_not_called()


def test_discover_orphans_prefers_valid_tmux_stamp_without_procfs(monkeypatch):
    """The live session-local stamp is the primary cross-platform identity."""
    cwd = Path("/tmp/codex-only")
    project = _project("codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    meta = _codex_meta(project, session_id)
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    app._codex_index.get.return_value = meta
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.session_rollout_ids", lambda *a: None)
    stamp = json.dumps(
        {
            "key": session_id,
            "tmux_name": "cx-new---abcdef-1",
            "session_type": "codex",
            "cwd": str(cwd),
        },
        separators=(",", ":"),
        sort_keys=True,
    )

    with (
        patch(
            "subprocess.check_output",
            return_value=f"cx-new---abcdef-1\t{cwd}\t100\t{stamp}\n",
        ),
        patch("railmux.ui.app.list_projects", return_value=[]),
    ):
        app._discover_orphans()

    assert app._running[session_id].tmux_name == "cx-new---abcdef-1"


def test_valid_v1_placeholder_binding_is_upgraded_to_resolved_v2(monkeypatch):
    import shlex

    cwd = Path("/tmp/codex-only")
    tmux_name = "cx-new---abcdef-1"
    session_id = "12345678-1234-1234-1234-1234567890ab"
    project = _project("codex-only")
    app = _minimal_app()
    app._config = MagicMock(codex_binary="codex", claude_binary="claude")
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    app._codex_index.get.return_value = _codex_meta(project, session_id)
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    monkeypatch.setattr(tmux_ctl, "session_rollout_ids", lambda *_args: None)
    monkeypatch.setattr(
        tmux_ctl,
        "detached_single_pane_start_command",
        lambda *_args, **_kwargs: shlex.quote(
            f"cd {cwd} && exec $SHELL -li -c 'exec codex -C {cwd}'"
        ),
    )
    stamp = json.dumps(
        {
            "key": session_id,
            "tmux_name": tmux_name,
            "session_type": "codex",
            "cwd": str(cwd),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    written: list[orphan_marker.Marker] = []
    app._write_orphan_marker = lambda marker: written.append(marker) or True
    row = f"{tmux_name}\t{cwd}\t100\t$42\t%9\t\t{stamp}\n"

    with (
        patch("subprocess.check_output", return_value=row),
        patch("railmux.ui.app.list_projects", return_value=[]),
    ):
        assert app._discover_orphans() is True

    marker = app._running[session_id].orphan
    assert marker == written[0]
    assert marker.phase == "resolved"
    assert marker.session_id == session_id


def test_generation_zero_keeps_exact_codex_stamp_visible(monkeypatch):
    """A slow first scan must not drop an exact live session from Running."""
    cwd = Path("/tmp/codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {}
    app._codex_index.get.return_value = None
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.session_rollout_ids", lambda *a: None)
    stamp = json.dumps(
        {
            "key": session_id,
            "tmux_name": "cx-new---abcdef-1",
            "session_type": "codex",
            "cwd": str(cwd),
        },
        separators=(",", ":"),
        sort_keys=True,
    )

    with (
        patch(
            "subprocess.check_output",
            return_value=f"cx-new---abcdef-1\t{cwd}\t100\t{stamp}\n",
        ),
        patch("railmux.ui.app.list_projects", return_value=[]),
    ):
        complete = app._discover_orphans(allow_missing_codex_metadata=True)

    assert complete is True
    assert app._running[session_id].tmux_name == "cx-new---abcdef-1"
    assert app._running[session_id].status == "busy"


def test_generation_zero_keeps_resolved_rewind_marker_resolved(monkeypatch):
    """A cold index cannot mistake a rewind descendant for another writer."""
    cwd = Path("/tmp/codex-only")
    root_id = "12345678-1234-1234-1234-1234567890ab"
    leaf_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    tmux_name = "cx-new---abcdef-1"
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {}
    app._codex_index.get.return_value = None
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    marker = orphan_marker.Marker(
        mode_key="codex",
        placeholder_key="__new__-abcdef-1",
        tmux_name=tmux_name,
        tmux_session_id="$42",
        tmux_pane_id="%9",
        owner=app._restart_identity,
        cwd=cwd,
        created_at=100.0,
        creation_token="c" * 32,
        phase="resolved",
        session_id=root_id,
    )
    pane = tmux_ctl.PaneIdentity(
        pane_id="%9",
        pane_pid=999,
        session_name=tmux_name,
        session_id="$42",
        window_id="@42",
        dead=False,
        width=80,
        height=24,
    )
    monkeypatch.setattr(tmux_ctl, "pane_identity", lambda _pane_id: pane)
    rollout_probe = MagicMock(return_value={leaf_id})
    monkeypatch.setattr(tmux_ctl, "session_rollout_ids", rollout_probe)
    exact_arg_probe = MagicMock(return_value=False)
    monkeypatch.setattr(tmux_ctl, "session_process_has_exact_arg", exact_arg_probe)
    row = f"{tmux_name}\t{cwd}\t100\t$42\t%9\t{orphan_marker.encode(marker)}\t\n"

    with (
        patch("subprocess.check_output", return_value=row),
        patch("railmux.ui.app.list_projects", return_value=[]),
    ):
        complete = app._discover_orphans(allow_missing_codex_metadata=True)

    assert complete is True
    running = app._running[root_id]
    assert running.tmux_name == tmux_name
    assert running.key == root_id
    assert running.status == "busy"
    assert not running.is_placeholder
    assert running.orphan == marker
    assert running.codex_canonical_session_id == root_id
    assert running.codex_baseline_rollback_count == 0
    rollout_probe.assert_not_called()
    exact_arg_probe.assert_not_called()


def test_first_codex_generation_revalidates_provisional_recovery():
    session_id = "12345678-1234-1234-1234-1234567890ab"
    app = _minimal_app()
    app._running[session_id] = _Running(
        key=session_id,
        tmux_name="cx-new---abcdef-1",
        label="codex-only/12345678",
        project=_project("codex-only"),
        status="busy",
        session_type="codex",
    )
    app._codex_recovery_pending = True
    app._codex_recovery_state = {"running_bindings_version": 1}
    app._codex_recovery_generation = 0
    app._codex_provisional_recovery_keys = {session_id}
    app._pending_restore_state = None
    app._loop = None
    app._codex_index = MagicMock()
    app._codex_index.current_snapshot.return_value = MagicMock(
        generation=1, report=MagicMock(transient_errors=0)
    )
    app._codex_index.is_unavailable = False

    def rediscover(_state, *, allow_missing_codex_metadata):
        assert allow_missing_codex_metadata is False
        assert session_id not in app._running
        app._running[session_id] = _Running(
            key=session_id,
            tmux_name="cx-new---abcdef-1",
            label="codex-only/Recovered",
            project=_project("codex-only"),
            status="idle",
            session_type="codex",
        )
        return True

    app._discover_orphans = MagicMock(side_effect=rediscover)
    app._retry_pending_codex_recovery()

    assert app._codex_recovery_pending is False
    assert app._running_recovery_ok is True
    assert app._running[session_id].label.endswith("/Recovered")


def test_transient_first_generation_keeps_provisional_session_visible():
    session_id = "12345678-1234-1234-1234-1234567890ab"
    recovered = _Running(
        key=session_id,
        tmux_name="cx-new---abcdef-1",
        label="codex-only/12345678",
        project=_project("codex-only"),
        status="busy",
        session_type="codex",
    )
    app = _minimal_app()
    app._running = {session_id: recovered}
    app._codex_recovery_pending = True
    app._codex_recovery_state = {"running_bindings_version": 1}
    app._codex_recovery_generation = 0
    app._codex_provisional_recovery_keys = {session_id}
    app._last_orphan_probe_ok = True
    app._codex_index = MagicMock()
    app._codex_index.current_snapshot.return_value = MagicMock(
        generation=1, report=MagicMock(transient_errors=1)
    )
    app._discover_orphans = MagicMock(return_value=False)

    app._retry_pending_codex_recovery()

    assert app._codex_recovery_pending is True
    assert app._running[session_id] is recovered
    assert app._codex_provisional_recovery_keys == {session_id}


def test_clean_generation_keeps_exact_session_until_metadata_appears():
    session_id = "12345678-1234-1234-1234-1234567890ab"
    recovered = _Running(
        key=session_id,
        tmux_name="cx-new---abcdef-1",
        label="codex-only/12345678",
        project=_project("codex-only"),
        status="busy",
        session_type="codex",
    )
    app = _minimal_app()
    app._running = {session_id: recovered}
    app._codex_recovery_pending = True
    app._codex_recovery_state = {"running_bindings_version": 1}
    app._codex_recovery_generation = 0
    app._codex_provisional_recovery_keys = {session_id}
    app._last_orphan_probe_ok = True
    app._codex_index = MagicMock()
    app._codex_index.current_snapshot.return_value = MagicMock(
        generation=1, report=MagicMock(transient_errors=0)
    )
    app._codex_index.get.return_value = None
    app._discover_orphans = MagicMock(return_value=False)

    app._retry_pending_codex_recovery()

    assert app._codex_recovery_pending is True
    assert app._running[session_id] is recovered
    assert app._codex_provisional_recovery_keys == {session_id}


def test_unavailable_initial_index_keeps_recovery_pending_for_later_retry():
    app = _minimal_app()
    app._codex_recovery_pending = True
    app._codex_recovery_generation = 0
    app._codex_index = MagicMock()
    app._codex_index.current_snapshot.return_value = MagicMock(
        generation=0, report=None
    )
    app._codex_index.is_unavailable = True

    app._retry_pending_codex_recovery()

    assert app._codex_recovery_pending is True


def test_stamp_running_writes_session_local_identity(monkeypatch):
    project = _project("codex-only")
    running = _Running(
        key="12345678-1234-1234-1234-1234567890ab",
        tmux_name="cx-new---abcdef-1",
        label="label",
        project=project,
        session_type="codex",
    )
    app = _minimal_app()
    set_option = MagicMock(return_value=True)
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.set_session_user_option", set_option)

    assert app._stamp_running(running) is True

    tmux_name, option, raw = set_option.call_args.args
    assert tmux_name == running.tmux_name
    assert option == "@railmux_binding_v1"
    assert json.loads(raw)["key"] == running.key


def test_discover_orphans_restores_unresolved_placeholder_state(monkeypatch):
    """Launch snapshots survive restart until normal polling can resolve them."""
    cwd = Path("/tmp/codex-only")
    key = "__new__-abcdef-1"
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    state = {
        "running_bindings_version": 1,
        "running_bindings": [
            {
                "key": key,
                "tmux_name": "cx-new---abcdef-1",
                "session_type": "codex",
                "cwd": str(cwd),
                "created_at": 123.0,
                "pre_launch_ids": ["old-session"],
            }
        ],
    }

    with (
        patch(
            "subprocess.check_output", return_value=f"cx-new---abcdef-1\t{cwd}\t100\n"
        ),
        patch("railmux.ui.app.list_projects", return_value=[]),
    ):
        app._discover_orphans(state)

    running = app._running[key]
    assert running.is_placeholder
    assert running.created_at == 123.0
    assert running.pre_launch_ids == frozenset({"old-session"})


def test_unresolved_stamp_merges_state_pre_launch_fence(monkeypatch):
    """Stamp identity must not discard the macOS anti-misbinding snapshot."""
    cwd = Path("/tmp/codex-only")
    key = "__new__-abcdef-1"
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    stamp = json.dumps(
        {
            "key": key,
            "tmux_name": "cx-new---abcdef-1",
            "session_type": "codex",
            "cwd": str(cwd),
            "created_at": 123.0,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    state = {
        "running_bindings_version": 1,
        "running_bindings": [
            {
                "key": key,
                "tmux_name": "cx-new---abcdef-1",
                "session_type": "codex",
                "cwd": str(cwd),
                "created_at": 123.0,
                "pre_launch_ids": ["old-session"],
            }
        ],
    }

    with (
        patch(
            "subprocess.check_output",
            return_value=f"cx-new---abcdef-1\t{cwd}\t100\t{stamp}\n",
        ),
        patch("railmux.ui.app.list_projects", return_value=[]),
    ):
        app._discover_orphans(state)

    assert app._running[key].pre_launch_ids == frozenset({"old-session"})


def test_unresolved_legacy_stamp_keeps_heuristic_resolution_disabled(monkeypatch):
    cwd = Path("/tmp/codex-only")
    key = "__new__-abcdef-1"
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    stamp = json.dumps(
        {
            "key": key,
            "tmux_name": "cx-new---abcdef-1",
            "session_type": "codex",
            "cwd": str(cwd),
            "created_at": 123.0,
            "pre_launch_complete": False,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    state = {
        "running_bindings_version": 1,
        "running_bindings": [
            {
                "key": key,
                "tmux_name": "cx-new---abcdef-1",
                "session_type": "codex",
                "cwd": str(cwd),
                "created_at": 123.0,
                "pre_launch_ids": [],
                "pre_launch_complete": False,
            }
        ],
    }

    with (
        patch(
            "subprocess.check_output",
            return_value=f"cx-new---abcdef-1\t{cwd}\t100\t{stamp}\n",
        ),
        patch("railmux.ui.app.list_projects", return_value=[]),
    ):
        app._discover_orphans(state)

    assert app._running[key].allow_heuristic_resolution is False


def test_discover_orphans_rejects_persisted_binding_with_wrong_cwd(monkeypatch):
    """A stale/untrusted state file cannot bind a live tmux from another cwd."""
    live_cwd = Path("/tmp/live")
    saved_cwd = Path("/tmp/saved")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {live_cwd: 1, saved_cwd: 1}
    app._codex_index.get.return_value = None
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.session_rollout_ids", lambda *a: set())
    state = {
        "running_bindings_version": 1,
        "running_bindings": [
            {
                "key": session_id,
                "tmux_name": "cx-new---abcdef-1",
                "session_type": "codex",
                "cwd": str(saved_cwd),
            }
        ],
    }

    with (
        patch(
            "subprocess.check_output",
            return_value=f"cx-new---abcdef-1\t{live_cwd}\t100\n",
        ),
        patch("railmux.ui.app.list_projects", return_value=[]),
    ):
        complete = app._discover_orphans(state)

    assert app._running == {}
    assert complete is False


def test_discover_orphans_duplicate_uuid_keeps_oldest_writer(monkeypatch):
    """Historical duplicate resumes never replace the original live writer."""
    cwd = Path("/tmp/codex-only")
    project = _project("codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    meta = _codex_meta(project, session_id)
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    app._codex_index.get.side_effect = lambda candidate, refresh=False: (
        meta if candidate == session_id else None
    )
    app._resolve_truncated_codex_id = MagicMock(return_value=session_id)
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_rollout_ids",
        lambda name, root: {session_id},
    )
    stable = f"cx-{App._safe_name(session_id, 16)}"
    output = f"{stable}\t{cwd}\t200\ncx-new---abcdef-1\t{cwd}\t100\n"

    with (
        patch("subprocess.check_output", return_value=output),
        patch("railmux.ui.app.list_projects", return_value=[]),
    ):
        app._discover_orphans()

    assert app._running[session_id].tmux_name == "cx-new---abcdef-1"


def test_restore_right_pane_refuses_unrepresented_live_tmux(monkeypatch):
    """Pane restoration cannot bypass the exactly-once running registry."""
    app = _minimal_app()
    app._attach_in_right_pane = MagicMock(return_value=True)
    app._set_status = MagicMock()
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.session_exists", lambda _name: True)

    restored = app._restore_right_pane(
        {
            "right_kind": "agent",
            "right_tmux": "cx-untracked",
        }
    )

    assert restored is False
    app._attach_in_right_pane.assert_not_called()


def test_restore_portable_agent_by_validated_session_id(monkeypatch):
    project = _project("portable")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    running = _Running(
        key=session_id,
        tmux_name="cc-12345678-1234-12",
        label="portable/session",
        project=project,
        session_type="claude",
    )
    app = _minimal_app(selected_project=project)
    app._running[session_id] = running
    app._attach_in_right_pane = MagicMock(return_value=True)
    app._set_status = MagicMock()
    pane = tmux_ctl.PaneIdentity("%9", 42, running.tmux_name, "$9", "@9", False, 80, 24)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_topology",
        lambda name: (
            tmux_ctl.SessionTopology(
                name,
                "$9",
                0,
                ("@9",),
                (pane,),
            )
            if name == running.tmux_name
            else None
        ),
    )

    restored = app._restore_right_pane(
        {
            "right_kind": "agent",
            "right_mode": "claude",
            "right_session": session_id,
            "right_project": project.encoded_name,
        }
    )

    assert restored is True
    app._attach_in_right_pane.assert_called_once_with(
        running.tmux_name, steal_focus=False
    )


def test_restore_preview_uses_persisted_mode_and_project_not_sidebar_mode():
    preview_project = _project("preview")
    sidebar_project = _project("sidebar")
    session_id = "preview-session"
    meta = MagicMock()
    meta.session_id = session_id
    meta.session_type = "claude"
    meta.jsonl_path = Path("/tmp/preview.jsonl")
    meta.project = preview_project
    app = _minimal_app(selected_project=sidebar_project)
    app._codex_mode = True
    app._project_snapshot = [preview_project, sidebar_project]
    app._session_cache.get.return_value = meta
    app._codex_index = MagicMock()
    app._show_transcript = MagicMock(return_value=True)
    app._set_active_target = MagicMock()

    restored = app._restore_right_pane(
        {
            "right_kind": "preview",
            "right_mode": "claude",
            "right_session": session_id,
            "right_project": preview_project.encoded_name,
        }
    )

    assert restored is True
    app._session_cache.get.assert_called_once_with(preview_project, session_id)
    app._codex_index.get.assert_not_called()
    app._set_active_target.assert_called_once_with(
        session_id,
        None,
        mode_key="claude",
        project_key=preview_project.encoded_name,
    )


def test_pending_restore_retains_state_after_incomplete_running_recovery(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    app = _minimal_app()
    app._pending_restore_state = {"right_kind": "empty"}
    app._running_recovery_ok = False
    app._restore_right_pane = MagicMock(return_value=True)
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: state_path))

    app._restore_pending_right_pane(None, None)

    assert state_path.exists()


def test_completed_agent_restore_releases_deferred_sidebar_focus_visual():
    app = _minimal_app()
    app._pending_restore_state = {"right_kind": "empty"}
    app._running_recovery_ok = True
    app._restore_right_pane = MagicMock(return_value=True)
    app._frame = MagicMock()
    app._railmux_has_focus = False
    app._defer_startup_sidebar_focus_visual = True
    app._loaded_restart_state_path = None
    app._loaded_restart_source = None

    app._restore_pending_right_pane(None, None)

    app._frame.set_window_active.assert_called_once_with(False)
    assert app._defer_startup_sidebar_focus_visual is False


def test_pending_index_allows_exact_running_target_restore(monkeypatch):
    state_path = Path("/tmp/not-used-state")
    app = _minimal_app()
    app._codex_recovery_pending = True
    app._running_recovery_ok = False
    app._pending_restore_state = {
        "right_kind": "agent",
        "right_tmux": "cx-new---abcdef-1",
    }
    app._running["session"] = _Running(
        key="session",
        tmux_name="cx-new---abcdef-1",
        label="project/session",
        project=_project("codex-only"),
        session_type="codex",
    )
    app._restore_right_pane = MagicMock(return_value=True)
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: state_path))

    app._restore_pending_right_pane(None, None)

    app._restore_right_pane.assert_called_once()
    assert app._pending_restore_state is None


def test_pending_codex_preview_waits_for_first_history_generation(monkeypatch):
    class _Snapshot:
        generation = 0

    class _Index:
        def current_snapshot(self):
            return _Snapshot()

    monkeypatch.setattr("railmux.ui.app.BackgroundCodexIndex", _Index)
    app = _minimal_app()
    app._codex_index = _Index()
    app._codex_recovery_pending = False
    app._running_recovery_ok = False
    app._pending_restore_state = {
        "right_kind": "preview",
        "right_mode": "codex",
        "right_session": "codex-session",
    }
    app._restore_right_pane = MagicMock(return_value=True)

    app._restore_pending_right_pane(None, None)

    app._restore_right_pane.assert_not_called()
    assert app._pending_restore_state is not None

    _Snapshot.generation = 1
    app._restore_pending_right_pane(None, None)
    app._restore_right_pane.assert_called_once()
    assert app._pending_restore_state is None


def test_pending_secondary_codex_preview_waits_for_history_generation(monkeypatch):
    class _Snapshot:
        generation = 0

    class _Index:
        def current_snapshot(self):
            return _Snapshot()

    monkeypatch.setattr("railmux.ui.app.BackgroundCodexIndex", _Index)
    app = _minimal_app()
    app._codex_index = _Index()
    app._codex_recovery_pending = False
    app._running_recovery_ok = False
    app._pending_restore_state = {
        "right_kind": "agent",
        "right_mode": "claude",
        "right_tmux": "cc-primary",
        "workspace": {
            "slots": {
                "primary": {"kind": "agent", "mode": "claude"},
                "secondary": {"kind": "preview", "mode": "codex"},
            },
        },
    }
    app._restore_right_pane = MagicMock(return_value=True)

    app._restore_pending_right_pane(None, None)

    app._restore_right_pane.assert_not_called()
    assert app._pending_restore_state is not None

    _Snapshot.generation = 1
    app._restore_pending_right_pane(None, None)
    app._restore_right_pane.assert_called_once()


def test_launch_resume_attaches_recovered_writer_instead_of_resuming_again():
    project = _project("codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    meta = _codex_meta(project, session_id)
    running = _Running(
        key=session_id,
        tmux_name="cx-new---abcdef-1",
        label="codex-only/Recovered",
        project=project,
        session_type="codex",
    )
    app = App.__new__(App)
    app._running = {session_id: running}
    app._agent_session_alive = MagicMock(return_value=True)
    app._on_running_select = MagicMock()
    app._launch = MagicMock()

    app._launch_resume(meta, steal_focus=False, from_double=True)

    entry = app._on_running_select.call_args.args[0]
    assert entry.tmux_name == running.tmux_name
    assert app._on_running_select.call_args.kwargs == {
        "steal_focus": False,
        "from_double": True,
    }
    app._launch.assert_not_called()


def test_launch_resume_promotes_recovered_placeholder_before_resume():
    """Linux exact correlation closes the pre-poll duplicate-writer window."""
    project = _project("codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    meta = _codex_meta(project, session_id)
    key = "__new__-abcdef-1"
    app = _minimal_app(selected_project=project)
    app._running[key] = _Running(
        key=key,
        tmux_name="cx-new---abcdef-1",
        label="codex-only/(new)",
        project=project,
        placeholder_path=project.real_path,
        created_at=999.0,
        session_type="codex",
    )
    app._codex_index = MagicMock()
    app._codex_index.sessions_for_cwd.return_value = [meta]
    app._correlate_codex_rollout = lambda _running: {session_id}
    app._discover_orphans = MagicMock(return_value=True)
    app._agent_session_alive = MagicMock(return_value=True)
    app._on_running_select = MagicMock()
    app._launch = MagicMock()

    app._launch_resume(meta)

    assert session_id in app._running and key not in app._running
    app._on_running_select.assert_called_once()
    app._launch.assert_not_called()


def test_launch_resume_refuses_ambiguous_live_placeholder_without_procfs():
    """When exact identity is unknowable, fail closed instead of duplicating."""
    project = _project("codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    meta = _codex_meta(project, session_id)
    key = "__new__-abcdef-1"
    app = _minimal_app(selected_project=project)
    app._running[key] = _Running(
        key=key,
        tmux_name="cx-new---abcdef-1",
        label="codex-only/(new)",
        project=project,
        placeholder_path=project.real_path,
        created_at=999.0,
        session_type="codex",
        allow_heuristic_resolution=False,
    )
    app._codex_index = MagicMock()
    app._codex_index.sessions_for_cwd.return_value = [meta]
    app._correlate_codex_rollout = lambda _running: None
    app._discover_orphans = MagicMock(return_value=True)
    app._agent_session_alive = MagicMock(return_value=True)
    app._launch = MagicMock()
    app._set_status = MagicMock()

    app._launch_resume(meta)

    assert key in app._running and session_id not in app._running
    app._launch.assert_not_called()
    app._set_status.assert_called_once_with(
        "Resume deferred: a live initializing agent in this project could "
        "own this session",
        "error",
    )


def test_launch_refuses_untracked_preexisting_tmux(monkeypatch):
    """The final launch gate cannot stamp or reuse an identity collision."""
    project = _project("codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    app = App.__new__(App)
    app._running = {}
    app._session_name = lambda _key: "cx-12345678-1234-12"
    app._set_status = MagicMock()
    app._ensure_detached_agent = MagicMock()
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.session_exists", lambda _name: True)

    launched = app._launch(
        session_id,
        ["codex"],
        project.real_path,
        "label",
        project,
        session_type="codex",
    )

    assert launched is False
    app._ensure_detached_agent.assert_not_called()


def test_discover_orphans_skips_placeholder():
    """__new__-N tmux sessions are skipped (handled by the normal poll)."""
    proj = _project()
    with (
        patch("subprocess.check_output", return_value="cc-__new__-1\t/tmp/test-proj\n"),
        patch("railmux.ui.app.list_projects", return_value=[proj]),
    ):
        app = _minimal_app()
        app._discover_orphans()
    assert len(app._running) == 0


def test_discover_orphans_skips_already_running():
    """Already-tracked sessions are not re-added."""
    proj = _project("myproj")
    full_id = "ae54affd-ec33-465c-b3c4-c1dc7c46990b"
    truncated = App._safe_name(full_id, 16)

    with (
        patch("subprocess.check_output", return_value=f"cc-{truncated}\t/tmp/myproj\n"),
        patch("railmux.ui.app.list_projects", return_value=[proj]),
        patch.object(App, "_resolve_truncated_id", return_value=full_id),
    ):
        app = _minimal_app()
        app._running[full_id] = _Running(
            key=full_id, tmux_name=f"cc-{truncated}", label="existing", project=proj
        )
        app._discover_orphans()
    assert app._running[full_id].label == "existing"  # not overwritten


def test_discover_orphans_skips_railmux():
    """The railmux outer tmux session is not treated as an orphan."""
    with patch("subprocess.check_output", return_value="railmux\t/home/user\n"):
        app = _minimal_app()
        app._discover_orphans()
    assert len(app._running) == 0


def test_discover_orphans_handles_tmux_error():
    """If tmux list-sessions fails, quietly return with no orphans."""
    with patch("subprocess.check_output", side_effect=OSError("no tmux")):
        app = _minimal_app()
        app._discover_orphans()  # should not raise
    assert len(app._running) == 0


# ── _teardown_tmux branching ────────────────────────────────────────────
