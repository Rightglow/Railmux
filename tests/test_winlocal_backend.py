from pathlib import Path

import pytest

from railmux.ui.workspace import AgentWorkspace, WorkspaceLayout
from railmux.config import Config
from railmux.ui.app import App
from railmux.winlocal.backend import WinMuxBackend
from railmux.winlocal.session_store import SessionRecord, SessionStore


class _FakeProcess:
    _next_pid = 100

    def __init__(self, *_args, **_kwargs):
        self.pid = self._next_pid
        type(self)._next_pid += 1
        self.alive = True
        self.writes = []
        self.size = None

    def read(self, _size=65536):
        raise EOFError

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def resize(self, columns, rows):
        self.size = (columns, rows)

    def is_alive(self):
        return self.alive

    def terminate(self, force=False):
        self.alive = False
        return True


def _factory(*_args, **_kwargs):
    return _FakeProcess()


def test_windows_backend_starts_typed_provider_and_composes_display():
    backend = WinMuxBackend(process_factory=_factory)
    launch = backend.prepare_launch(["codex", "resume", "abc"], Path("C:/repo"))
    ok, error = backend.new_detached_session("codex-abc", launch)
    workspace = AgentWorkspace()
    transport = backend.create_display_transport(workspace, "swap")
    transport.create_primary()
    attached = transport.attach(workspace.primary, "codex-abc")

    assert ok and error is None
    assert attached.ok
    assert backend.session_exists("codex-abc")
    session = backend._sessions["codex-abc"]
    assert session.process.size == (83, 29)
    update = backend.screen_update()
    assert update.width == 120 and update.height == 30


def test_windows_snapshot_keeps_attached_display_pane_alive():
    backend = WinMuxBackend(process_factory=_factory)
    launch = backend.prepare_launch(["codex", "resume", "abc"], Path("C:/repo"))
    assert backend.new_detached_session("codex-abc", launch) == (True, None)
    workspace = AgentWorkspace()
    transport = backend.create_display_transport(workspace, "swap")

    assert transport.attach(workspace.primary, "codex-abc").ok
    assert workspace.primary.pane_id is not None
    snapshot = backend.server_snapshot()

    assert workspace.primary.pane_id in snapshot.panes
    assert backend._sessions["codex-abc"].pane_id in snapshot.panes
    assert transport.fallback_for_external_client(workspace.primary) is None


def test_shared_reconciliation_preserves_native_attached_agent(tmp_path):
    backend = WinMuxBackend(process_factory=_factory)
    app = App(tmp_path, Config(), mux_backend=backend)
    launch = backend.prepare_launch(["codex", "resume", "abc"], tmp_path)
    assert backend.new_detached_session("codex-abc", launch) == (True, None)
    session = backend._sessions["codex-abc"]
    assert session.terminal is not None
    session.terminal.feed(b"AGENT_PANEL_READY")
    slot = app._agent_workspace().primary
    assert app._display_transport().attach(slot, "codex-abc").ok

    snapshot = backend.server_snapshot()
    app._reconcile_display_slots(
        lambda name: name in snapshot.sessions,
        lambda pane_id: pane_id in snapshot.panes,
    )

    assert slot.pane_id is not None
    assert slot.agent_tmux_name == "codex-abc"
    assert "primary" in backend._display_regions()
    assert any(
        b"AGENT_PANEL_READY" in row
        for _index, row in backend.screen_update().rows
    )


def test_windows_backend_preserves_unsuperseded_resume_offers(tmp_path):
    store = SessionStore(tmp_path / "sessions.json", "new-daemon")
    offer = SessionRecord(
        record_id="old-session",
        provider="codex",
        cwd=r"C:\old",
        phase="resume_offer",
        daemon_id="old-daemon",
        provider_session_id="provider-old",
    )
    backend = WinMuxBackend(
        process_factory=_factory,
        daemon_id="new-daemon",
        session_store=store,
        resume_offers=(offer,),
    )

    launch = backend.prepare_launch(["claude"], Path("C:/new"))
    assert backend.new_detached_session("new-session", launch) == (True, None)

    records = {record.record_id: record for record in store.load()}
    assert records["old-session"].phase == "resume_offer"
    assert records["old-session"].provider_session_id == "provider-old"
    assert records["new-session"].phase == "resolved"


def test_windows_backend_resolves_npm_shim_before_conpty_spawn(monkeypatch):
    captured = []

    def factory(argv, **_kwargs):
        captured.append(tuple(argv))
        return _FakeProcess()

    monkeypatch.setattr(
        "railmux.winlocal.backend.shutil.which",
        lambda _name: r"C:\Program Files\nodejs\codex.cmd",
    )
    backend = WinMuxBackend(process_factory=factory)
    launch = backend.prepare_launch(
        ["codex", "resume", "session with spaces"], Path("C:/repo")
    )

    assert backend.new_detached_session("codex-abc", launch) == (True, None)
    assert captured[0][:4] == ("cmd.exe", "/d", "/s", "/c")
    assert '"C:\\Program Files\\nodejs\\codex.cmd"' in captured[0][4]


def test_windows_backend_dual_layout_does_not_duplicate_session():
    backend = WinMuxBackend(process_factory=_factory)
    workspace = AgentWorkspace()
    transport = backend.create_display_transport(workspace, "swap")
    assert transport.create_dual(WorkspaceLayout.SIDE_BY_SIDE)
    assert workspace.primary.pane_id != workspace.secondary.pane_id


def test_shared_app_constructs_and_runs_with_windows_backend(tmp_path, monkeypatch):
    backend = WinMuxBackend(process_factory=_factory)
    app = App(tmp_path, Config(), mux_backend=backend)
    monkeypatch.setattr("urwid.MainLoop.run", lambda _loop: None)

    app.run()

    assert app.mux is backend
    assert backend.create_ui_screen().get_cols_rows()[0] >= 20


def test_shared_app_launch_marker_is_recovered_by_next_native_ui(tmp_path):
    backend = WinMuxBackend(process_factory=_factory)
    first = App(tmp_path, Config(), mux_backend=backend)

    assert first._launch(
        "__new__-native-1",
        ["codex"],
        tmp_path,
        "new",
        None,
        placeholder_path=tmp_path,
        session_type="codex",
    )

    second = App(tmp_path, Config(), mux_backend=backend)
    restored = second._running["__new__-native-1"]
    assert restored.tmux_name == "cx-new---native-1"
    assert restored.orphan is not None
    assert restored.orphan.phase == "unresolved"


def test_native_ui_crash_fails_soft_and_preserves_provider(tmp_path, monkeypatch):
    backend = WinMuxBackend(process_factory=_factory)
    launch = backend.prepare_launch(["codex", "resume", "abc"], tmp_path)
    assert backend.new_detached_session("codex-abc", launch) == (True, None)
    app = App(tmp_path, Config(), mux_backend=backend)
    monkeypatch.setattr(
        "urwid.MainLoop.run",
        lambda _loop: (_ for _ in ()).throw(RuntimeError("UI failed")),
    )

    with pytest.raises(RuntimeError, match="UI failed"):
        app.run()

    assert backend.session_exists("codex-abc")


def test_native_app_honors_initial_project_path(tmp_path):
    claude_home = tmp_path / ".claude"
    project = tmp_path / "project"
    project.mkdir()
    app = App(
        claude_home,
        Config(),
        mux_backend=WinMuxBackend(process_factory=_factory),
        initial_project_path=project,
    )

    assert app._initial_project_path == project
