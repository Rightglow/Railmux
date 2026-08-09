"""Tests for soft-quit feature: state file, orphan discovery, truncated ID
resolution, QuitConfirmModal s-key, and teardown branching."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from railmux.models import Project, SessionMeta
from railmux import tmux_ctl
from railmux.restart_state import OuterTmuxIdentity
from railmux.ui.app import App, _Running


from tests.app_test_harness import (
    _project,
    isolate_tmux_identity_stamps as isolate_tmux_identity_stamps,
)


pytestmark = pytest.mark.usefixtures("isolate_tmux_identity_stamps")


def _codex_session(project: Project, session_id: str, mtime: float) -> SessionMeta:
    return SessionMeta(
        project=project,
        session_id=session_id,
        jsonl_path=Path("/tmp/rollout.jsonl"),
        title="Real session",
        message_count=1,
        token_total=1,
        last_mtime=mtime,
        status="idle",
    )


def test_resolve_placeholders_codex_rekeys_to_real_uuid():
    """In Codex mode a `__new__-N` placeholder resolves to its real UUID
    via the Codex index (not the Claude session cache), so clicking the real
    row doesn't spawn a duplicate session and force_projects can clear."""
    proj = _project()
    real_id = "12345678-1234-1234-1234-1234567890ab"
    app = App.__new__(App)
    app._codex_mode = True
    app._right_pane_claude = None
    app._running_sort_ts = 1234.0
    app._running = {
        "__new__-1": _Running(
            key="__new__-1",
            tmux_name="cx-new----1",
            label="test-proj/(new)",
            project=proj,
            placeholder_path=proj.real_path,
            created_at=999.0,
        )
    }
    app._codex_index = MagicMock()
    app._codex_index.sessions_for_cwd.return_value = [
        _codex_session(proj, real_id, mtime=1000.0)
    ]
    # Codex mode must NOT consult the Claude session cache.
    app._session_cache = MagicMock()
    app._session_cache.list_sessions.side_effect = AssertionError(
        "Codex placeholder resolution queried the Claude cache"
    )

    app._resolve_placeholders([proj])

    assert "__new__-1" not in app._running
    assert real_id in app._running
    entry = app._running[real_id]
    assert entry.tmux_name == "cx-new----1"  # same tmux session, re-keyed
    assert not entry.is_placeholder
    assert entry.label == "test-proj/Real session"
    assert entry.status == "idle"
    assert entry.last_mtime == 1000.0
    assert app._running_sort_ts == 0.0
    app._codex_index.sessions_for_cwd.assert_called_once_with(
        proj.real_path, refresh=False
    )


def test_resolve_placeholder_keeps_visible_project_identity_on_promotion():
    """A Codex metadata key must not replace the Projects-pane key for a cwd."""
    visible = _project()
    codex_project = replace(
        visible,
        encoded_name="-cx-tmp-test-proj",
        claude_dir=Path(),
    )
    real_id = "12345678-1234-1234-1234-1234567890ab"
    tmux_name = "cx-new----1"
    app = App.__new__(App)
    app._codex_mode = True
    app._right_pane_claude = None
    app._running_sort_ts = 1234.0
    app._running = {
        "__new__-1": _Running(
            key="__new__-1",
            tmux_name=tmux_name,
            label="test-proj/(new)",
            project=visible,
            placeholder_path=visible.real_path,
            created_at=999.0,
            session_type="codex",
        )
    }
    app._codex_index = MagicMock()
    app._codex_index.sessions_for_cwd.return_value = [
        _codex_session(codex_project, real_id, mtime=1000.0)
    ]
    app._correlate_codex_rollout = MagicMock(return_value={real_id})
    app._set_current_project = MagicMock()
    app._set_slot_active_target = MagicMock()
    app._agent_workspace().target.agent_tmux_name = tmux_name

    app._resolve_placeholders([visible])

    entry = app._running[real_id]
    assert entry.project is visible
    assert entry.project.encoded_name == visible.encoded_name
    app._set_current_project.assert_called_once_with(visible)


def test_resolve_placeholders_codex_keeps_placeholder_until_jsonl_appears():
    """Before the rollout file exists (no session yet), the placeholder must
    stay a placeholder rather than mis-binding to nothing."""
    proj = _project()
    app = App.__new__(App)
    app._codex_mode = True
    app._right_pane_claude = None
    app._running = {
        "__new__-1": _Running(
            key="__new__-1",
            tmux_name="cx-new----1",
            label="test-proj/(new)",
            project=proj,
            placeholder_path=proj.real_path,
            created_at=999.0,
        )
    }
    app._codex_index = MagicMock()
    app._codex_index.sessions_for_cwd.return_value = []  # nothing on disk yet

    app._resolve_placeholders([proj])

    assert "__new__-1" in app._running
    assert app._running["__new__-1"].is_placeholder


def test_consume_mode_refresh_swaps_both_indexes():
    import threading

    proj = _project()
    index = MagicMock()
    index.all_cwds.return_value = {proj.real_path: 2}
    app = App.__new__(App)
    app._mode_refresh_lock = threading.Lock()
    app._mode_refresh_result = ([proj], index, None)
    app._project_snapshot = None
    app._project_snapshot_at = 0.0
    app._codex_index = MagicMock()
    app._codex_project_filter = {}

    assert app._consume_mode_refresh() is True
    assert app._project_snapshot == [proj]
    assert app._project_snapshot_at > 0.0
    assert app._codex_index is index
    assert app._codex_project_filter == {proj.real_path: 2}


def test_restore_codex_preview_uses_codex_index():
    app = App.__new__(App)
    app._codex_mode = True
    app._selected_project = _project()
    meta = MagicMock()
    meta.session_id = "codex-session"
    meta.session_type = "codex"
    meta.jsonl_path = Path("/tmp/codex-rollout.jsonl")
    meta.project = app._selected_project
    app._codex_index = MagicMock()
    app._codex_index.get.return_value = meta
    app._session_cache = MagicMock()
    app._show_transcript = MagicMock(return_value=True)
    app._set_active_target = MagicMock()
    app._in_history_mode = False

    app._restore_right_pane({"right_kind": "preview", "right_session": meta.session_id})

    app._codex_index.get.assert_called_once_with(meta.session_id)
    app._session_cache.get.assert_not_called()
    # Restore passes the explicit Codex format hint so a tailed long rollout
    # renders correctly (#5), not just the path.
    app._show_transcript.assert_called_once_with(
        meta.jsonl_path, session_type=meta.session_type
    )
    app._set_active_target.assert_called_once_with(
        meta.session_id,
        None,
        mode_key="codex",
        project_key=meta.project.encoded_name,
    )


# ── #11: placeholder names are namespaced per process (no restart collision)


def test_placeholder_names_are_process_namespaced():
    """Two railmux processes (distinct per-process tokens) never generate the
    same placeholder key OR tmux session name, even though each process's
    counter restarts at 0 — so a fresh launch can't reuse a previous process's
    placeholder name and hijack a surviving orphan tmux session (#11)."""

    def _app(token: str):
        app = App.__new__(App)
        app._proc_token = token
        app._new_session_counter = 0
        app._codex_mode = True
        return app

    a, b = _app("aaaaaa"), _app("bbbbbb")
    # First placeholder of each "process" — identical counter value.
    ka, kb = a._new_placeholder_key(), b._new_placeholder_key()
    assert ka != kb
    assert ka.startswith("__new__-") and kb.startswith("__new__-")
    # Still classified as placeholders.
    assert _Running(key=ka, tmux_name="x", label="l").is_placeholder
    # The token survives _safe_name's 16-char truncation, so the derived tmux
    # session names differ too (the actual collision surface).
    assert a._session_name(ka) != b._session_name(kb)
    assert a._session_name(ka).startswith("cx-")


def test_placeholder_counter_reset_still_unique_across_processes():
    """Within one process the counter increments; across processes the token
    differs — so `process A #1` and `process B #1` never collide."""

    def _app(token: str):
        app = App.__new__(App)
        app._proc_token = token
        app._new_session_counter = 0
        app._codex_mode = False
        return app

    a, b = _app("a1b2c3"), _app("d4e5f6")
    keys = {
        a._new_placeholder_key(),
        a._new_placeholder_key(),
        b._new_placeholder_key(),
        b._new_placeholder_key(),
    }
    assert len(keys) == 4  # all distinct


def test_placeholder_session_name_not_truncated_to_collision():
    """High counters must not collapse to the same tmux name. Placeholders skip
    the 16-char _safe_name truncation, so `__new__-<tok>-1000` and `-10000`
    (which would both truncate to `...-100`) stay distinct (#11)."""
    app = App.__new__(App)
    app._proc_token = "abcdef"
    app._codex_mode = True
    app._new_session_counter = 999
    k1 = app._new_placeholder_key()  # counter -> 1000
    app._new_session_counter = 9999
    k2 = app._new_placeholder_key()  # counter -> 10000
    assert app._session_name(k1) != app._session_name(k2)


# ── #12: placeholder never binds a pre-existing same-cwd rollout ──────────


def test_resolve_placeholders_ignores_pre_existing_cwd_rollout():
    """A rollout that existed in the launch cwd BEFORE this placeholder launched
    (captured in pre_launch_ids) is never bound — even if it is the NEWEST
    session in the cwd — so a placeholder can't hijack another process's
    conversation written to the same cwd (#12)."""
    proj = _project()
    pre_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    new_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    app = App.__new__(App)
    app._codex_mode = True
    app._right_pane_claude = None
    app._running = {
        "__new__-tok-1": _Running(
            key="__new__-tok-1",
            tmux_name="cx-new---tok-1",
            label="test-proj/(new)",
            project=proj,
            placeholder_path=proj.real_path,
            created_at=999.0,
            pre_launch_ids=frozenset({pre_id}),
        )
    }
    app._codex_index = MagicMock()
    # pre_id is the NEWEST session in the cwd; without the fix it would win.
    app._codex_index.sessions_for_cwd.return_value = [
        _codex_session(proj, pre_id, mtime=2000.0),
        _codex_session(proj, new_id, mtime=1001.0),
    ]

    app._resolve_placeholders([proj])

    assert pre_id not in app._running  # never bound
    assert new_id in app._running  # our real session bound
    assert "__new__-tok-1" not in app._running
    assert not app._running[new_id].is_placeholder


def test_resolve_placeholders_ambiguous_new_rollouts_not_bound():
    """If TWO new rollouts appear in the launch cwd since our launch, a
    concurrent codex/railmux is writing there and we can't tell which is ours —
    the placeholder stays unresolved rather than binding the wrong one (#12)."""
    proj = _project()
    app = App.__new__(App)
    app._codex_mode = True
    app._right_pane_claude = None
    app._running = {
        "__new__-tok-1": _Running(
            key="__new__-tok-1",
            tmux_name="cx-new----tok-1",
            label="test-proj/(new)",
            project=proj,
            placeholder_path=proj.real_path,
            created_at=999.0,
            pre_launch_ids=frozenset(),
        )
    }
    app._codex_index = MagicMock()
    # Two brand-new rollouts, both post-launch, both unclaimed → ambiguous.
    app._codex_index.sessions_for_cwd.return_value = [
        _codex_session(proj, "aaaa1111-0000-0000-0000-000000000000", mtime=1001.0),
        _codex_session(proj, "bbbb2222-0000-0000-0000-000000000000", mtime=1002.0),
    ]

    app._resolve_placeholders([proj])

    # Nothing bound; placeholder preserved (safer than mis-binding).
    assert "__new__-tok-1" in app._running
    assert app._running["__new__-tok-1"].is_placeholder


def test_resolve_placeholders_correlation_binds_exact_rollout(monkeypatch):
    """Staggered race (#12): our OWN rollout AND an unrelated newer rollout both
    appear in the launch cwd, so both are candidates. The heuristic would refuse
    (ambiguous). Exact child→rollout correlation — the codex process in the
    placeholder's pane holds OUR rollout open — binds THAT id, not the newer one.
    """
    proj = _project()
    ours = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    unrelated = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    app = App.__new__(App)
    app._codex_mode = True
    app._right_pane_claude = None
    app._running = {
        "__new__-tok-1": _Running(
            key="__new__-tok-1",
            tmux_name="cx-new----tok-1",
            label="test-proj/(new)",
            project=proj,
            placeholder_path=proj.real_path,
            created_at=999.0,
            session_type="codex",
        )
    }
    app._codex_index = MagicMock()
    app._codex_index.sessions_for_cwd.return_value = [
        _codex_session(proj, unrelated, mtime=1002.0),  # newer, NOT ours
        _codex_session(proj, ours, mtime=1001.0),
    ]
    # Correlation resolves the pane's codex process to OUR rollout fd.
    app._correlate_codex_rollout = lambda r: {ours}

    app._resolve_placeholders([proj])

    assert ours in app._running and not app._running[ours].is_placeholder
    assert unrelated not in app._running  # newer rollout NOT mis-bound
    assert "__new__-tok-1" not in app._running


def test_resolve_placeholders_correlation_waits_when_id_not_yet_candidate(monkeypatch):
    """Correlation KNOWS the exact rollout, but it isn't a bindable candidate
    yet (index lag). Even though an unrelated single rollout is present (which
    the heuristic would bind), we WAIT rather than let the heuristic mis-bind.
    """
    proj = _project()
    ours = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    unrelated = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    app = App.__new__(App)
    app._codex_mode = True
    app._right_pane_claude = None
    app._running = {
        "__new__-tok-1": _Running(
            key="__new__-tok-1",
            tmux_name="cx-new----tok-1",
            label="test-proj/(new)",
            project=proj,
            placeholder_path=proj.real_path,
            created_at=999.0,
            session_type="codex",
        )
    }
    app._codex_index = MagicMock()
    # Only the unrelated rollout is indexed so far; ours (held open) isn't yet.
    app._codex_index.sessions_for_cwd.return_value = [
        _codex_session(proj, unrelated, mtime=1001.0),
    ]
    app._correlate_codex_rollout = lambda r: {ours}

    app._resolve_placeholders([proj])

    assert "__new__-tok-1" in app._running  # left unresolved
    assert app._running["__new__-tok-1"].is_placeholder
    assert unrelated not in app._running  # heuristic did NOT bind it


def test_resolve_placeholders_falls_back_to_heuristic_when_no_correlation(monkeypatch):
    """Correlation unavailable (None: no procfs/macOS, no pane pid, no fd yet) →
    the existing exactly-one heuristic still binds the single new rollout, so the
    #12 fix never regresses the interactive default on platforms without /proc.
    """
    proj = _project()
    real_id = "12345678-1234-1234-1234-1234567890ab"
    app = App.__new__(App)
    app._codex_mode = True
    app._right_pane_claude = None
    app._running = {
        "__new__-tok-1": _Running(
            key="__new__-tok-1",
            tmux_name="cx-new----tok-1",
            label="test-proj/(new)",
            project=proj,
            placeholder_path=proj.real_path,
            created_at=999.0,
            session_type="codex",
        )
    }
    app._codex_index = MagicMock()
    app._codex_index.sessions_for_cwd.return_value = [
        _codex_session(proj, real_id, mtime=1000.0)
    ]
    app._correlate_codex_rollout = lambda r: None  # correlation unavailable

    app._resolve_placeholders([proj])

    assert real_id in app._running and not app._running[real_id].is_placeholder
    assert "__new__-tok-1" not in app._running


def test_resolve_placeholders_empty_correlation_waits_not_fallback(monkeypatch):
    """procfs available but codex hasn't opened its rollout fd YET (correlation
    returns an empty set), while an unrelated rollout already appeared. We must
    WAIT — NOT fall back to the heuristic, which would mis-bind the unrelated
    one (the staggered race codex flagged, #12)."""
    proj = _project()
    unrelated = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    app = App.__new__(App)
    app._codex_mode = True
    app._right_pane_claude = None
    app._running = {
        "__new__-tok-1": _Running(
            key="__new__-tok-1",
            tmux_name="cx-new----tok-1",
            label="test-proj/(new)",
            project=proj,
            placeholder_path=proj.real_path,
            created_at=999.0,
            session_type="codex",
        )
    }
    app._codex_index = MagicMock()
    app._codex_index.sessions_for_cwd.return_value = [
        _codex_session(proj, unrelated, mtime=1001.0),
    ]
    app._correlate_codex_rollout = lambda r: set()  # procfs, but no fd open yet

    app._resolve_placeholders([proj])

    assert "__new__-tok-1" in app._running  # waited, not bound
    assert app._running["__new__-tok-1"].is_placeholder
    assert unrelated not in app._running  # heuristic did NOT fire


def test_correlate_codex_rollout_fails_closed_without_config(monkeypatch):
    """A helper failure on procfs must wait, never unlock the heuristic."""
    monkeypatch.setattr(tmux_ctl, "proc_fs_available", lambda: True)
    app = App.__new__(App)
    r = _Running(key="__new__-tok-1", tmux_name="cx-x", label="l", session_type="codex")
    assert app._correlate_codex_rollout(r) == set()


def test_msys_proc_projection_uses_fenced_placeholder_fallback(monkeypatch):
    """MSYS /proc cannot identify FDs opened by native Windows Codex."""
    monkeypatch.setattr(tmux_ctl.sys, "platform", "msys")
    monkeypatch.setattr(tmux_ctl.os.path, "isdir", lambda _path: True)
    app = App.__new__(App)
    running = _Running(
        key="__new__-tok-1",
        tmux_name="cx-x",
        label="p/(new)",
        session_type="codex",
    )

    assert app._correlate_codex_rollout(running) is None


def test_correlate_codex_rollout_follows_swap_displayed_real_pane(monkeypatch):
    """A swap leaves only an inert placeholder in the named home session."""
    session_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    sessions_dir = Path("/codex/sessions")
    app = App.__new__(App)
    app._codex_home_path = lambda: sessions_dir.parent
    manager = MagicMock()
    manager.displayed_real_pane.return_value = "%42"
    app._display_transport_manager = manager
    running = _Running(
        key="__new__-tok-1",
        tmux_name="cx-new----tok-1",
        label="test-proj/(new)",
        session_type="codex",
    )
    monkeypatch.setattr(
        tmux_ctl,
        "pane_identity",
        lambda pane: tmux_ctl.PaneIdentity(
            pane, 4242, "railmux", "$1", "@1", False, 100, 30
        ),
    )
    process_probe = MagicMock(return_value={session_id})
    monkeypatch.setattr(tmux_ctl, "process_tree_rollout_ids", process_probe)
    home_probe = MagicMock(
        side_effect=AssertionError(
            "the home session contains only the swap placeholder"
        )
    )
    monkeypatch.setattr(tmux_ctl, "session_rollout_ids", home_probe)

    assert app._correlate_codex_rollout(running) == {session_id}
    process_probe.assert_called_once_with(4242, sessions_dir)
    home_probe.assert_not_called()


def test_codex_identity_rejects_dead_displayed_provider(monkeypatch):
    app = App.__new__(App)
    running = _Running(
        key="session-a",
        tmux_name="cx-session-a",
        label="p/session",
        session_type="codex",
    )
    manager = MagicMock()
    manager.displayed_real_pane.return_value = "%42"
    app._display_transport_manager = manager
    dead = tmux_ctl.PaneIdentity("%42", 42, "railmux", "$1", "@1", True, 80, 24)
    monkeypatch.setattr(tmux_ctl, "pane_identity", lambda _pane: dead)
    topology = MagicMock()
    monkeypatch.setattr(tmux_ctl, "session_topology", topology)

    assert app._codex_real_pane_identity(running) is None
    topology.assert_not_called()


def test_launch_snapshots_pre_existing_ids(monkeypatch):
    """_launch captures the cwd's existing session ids into the placeholder's
    pre_launch_ids before starting the child (#12)."""
    proj = _project()
    existing = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    app = App.__new__(App)
    app._running = {}
    app._set_status = lambda *a, **k: None
    app._codex_index = MagicMock()
    app._codex_index.sessions_for_cwd.return_value = [
        _codex_session(proj, existing, mtime=5.0)
    ]
    app._shellify = lambda *a, **k: "SHELLCMD"
    app._ensure_detached_agent = lambda *a, **k: (True, None)
    app._attach_in_right_pane = lambda *a, **k: True
    app._session_name = lambda key: "cx-abc"
    app._restart_identity = OuterTmuxIdentity(
        server_digest="a" * 64,
        server_pid=123,
        pane_id="%1",
        session_id="$1",
        window_id="@1",
    )
    holder = tmux_ctl.PaneIdentity(
        pane_id="%9",
        pane_pid=999,
        session_name="cx-abc",
        session_id="$9",
        window_id="@9",
        dead=False,
        width=80,
        height=24,
    )
    monkeypatch.setattr(
        tmux_ctl, "create_detached_holder", lambda *a, **k: (holder, None)
    )
    monkeypatch.setattr(tmux_ctl, "start_detached_holder", lambda *a, **k: (True, None))
    app._write_orphan_marker = lambda marker: True

    assert app._launch(
        "__new__-tok-1",
        ["codex"],
        proj.real_path,
        "l",
        proj,
        placeholder_path=proj.real_path,
        session_type="codex",
    )
    entry = app._running["__new__-tok-1"]
    assert entry.pre_launch_ids == frozenset({existing})
    # Snapshot taken with a fresh scan of the cwd.
    app._codex_index.sessions_for_cwd.assert_called_once_with(
        proj.real_path, refresh=True
    )
