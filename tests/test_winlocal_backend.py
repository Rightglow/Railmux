from pathlib import Path
from unittest.mock import MagicMock

import pytest
import urwid

from railmux.ui.workspace import (
    AgentWorkspace,
    WorkspaceLayout,
    WorkspacePage,
    WorkspacePresentation,
)
from railmux.config import Config
from railmux.models import Project, SessionMeta
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


def test_native_topology_and_resize_keep_urwid_and_preview_geometry_in_sync(
    tmp_path,
):
    backend = WinMuxBackend(width=120, height=30, process_factory=_factory)
    workspace = AgentWorkspace()
    transport = backend.create_display_transport(workspace, "swap")
    screen = backend.create_ui_screen()

    assert screen.get_cols_rows() == (120, 29)
    transport.create_primary()
    assert screen.get_cols_rows() == (36, 29)

    transcript = tmp_path / "preview.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"content":"a long preview line"}}\n',
        encoding="utf-8",
    )
    assert backend.show_transcript(
        workspace.primary.pane_id, transcript, "claude"
    )

    backend.resize(160, 40)
    preview = backend._previews[workspace.primary.pane_id]
    assert screen.get_cols_rows() == (48, 39)
    assert (preview.width, preview.height) == (111, 39)

    workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    transport.create_secondary(workspace.layout)
    backend.screen_update()
    assert screen.get_cols_rows() == (32, 39)


def test_native_modal_uses_current_sidebar_viewport_after_agent_opens(tmp_path):
    backend = WinMuxBackend(width=120, height=30, process_factory=_factory)
    app = App(tmp_path, Config(), mux_backend=backend)
    app._display_transport().create_primary()
    app._loop = MagicMock()
    app._loop.screen = backend.create_ui_screen()
    app._frame = urwid.SolidFill(" ")

    app._show_overlay(
        urwid.SolidFill(" "),
        width=60,
        height=40,
        fixed_width=True,
        fixed_height=True,
    )

    assert app._loop.widget.width == 34
    assert app._loop.widget.height == 27


def test_native_bottom_chrome_contains_mode_layout_and_click_actions(tmp_path):
    backend = WinMuxBackend(width=100, height=30, process_factory=_factory)
    app = App(tmp_path, Config(), mux_backend=backend)
    app._display_transport().create_primary()
    app._apply_tmux_bar(False)
    backend.set_status_text("ready", "info")

    update = backend.screen_update()
    status_row = dict(update.rows)[29]

    assert b"Railmux" in status_row
    assert app._active_mode().label.encode() in status_row
    assert "▣".encode() in status_row
    assert b"ready" in status_row

    mode_hit = next(hit for hit in backend._status_hits if hit.action == "mode")
    screen = backend.create_ui_screen()
    screen.start()
    try:
        backend._route_mouse(type("Event", (), {
            "x": mode_hit.start + 1,
            "y": backend.height,
            "button": 0,
            "pressed": True,
        })())
        keys, _raw = screen.get_input(True)
    finally:
        screen.stop()

    assert "f5" in keys


def test_native_preview_then_open_replaces_history_with_live_agent(tmp_path):
    backend = WinMuxBackend(width=100, height=30, process_factory=_factory)
    workspace = AgentWorkspace()
    transport = backend.create_display_transport(workspace, "swap")
    launch = backend.prepare_launch(["codex", "resume", "abc"], tmp_path)
    assert backend.new_detached_session("codex-abc", launch) == (True, None)
    session = backend._sessions["codex-abc"]
    session.terminal.feed(b"LIVE_AGENT_SURFACE")
    assert transport.attach(workspace.primary, "codex-abc").ok

    transcript = tmp_path / "preview.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"content":"PREVIEW_SURFACE"}}\n',
        encoding="utf-8",
    )
    assert transport.prepare_preview(workspace.primary)
    assert backend.show_transcript(
        workspace.primary.pane_id, transcript, "claude"
    )
    assert workspace.primary.pane_id in backend._previews

    assert transport.attach(workspace.primary, "codex-abc").ok
    update = backend.screen_update()

    assert workspace.primary.pane_id not in backend._previews
    assert any(b"LIVE_AGENT_SURFACE" in row for _index, row in update.rows)


def test_shared_preview_open_gesture_contract_runs_through_native_backend(
    tmp_path,
):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"content":"preview body"}}\n',
        encoding="utf-8",
    )
    project = Project(
        real_path=tmp_path,
        encoded_name="native-project",
        claude_dir=tmp_path,
        session_count=1,
        last_activity_ts=1.0,
    )
    session = SessionMeta(
        project=project,
        session_id="11111111-1111-1111-1111-111111111111",
        jsonl_path=transcript,
        title="Native preview",
        message_count=1,
        token_total=1,
        last_mtime=1.0,
    )
    backend = WinMuxBackend(width=100, height=30, process_factory=_factory)
    app = App(tmp_path, Config(), mux_backend=backend)

    app._on_session_row_preview(session)
    slot = app._agent_workspace().primary

    assert slot.in_history_mode
    assert backend.active_pane_id() == "%controller"
    assert slot.pane_id in backend._previews

    app._on_session_select(session, steal_focus=False, from_double=True)

    assert not slot.in_history_mode
    assert slot.pane_id not in backend._previews
    assert slot.agent_tmux_name is not None
    assert backend.session_exists(slot.agent_tmux_name)


def test_native_compact_resize_projects_full_page_then_restores_sidebar(tmp_path):
    backend = WinMuxBackend(width=100, height=30, process_factory=_factory)
    app = App(tmp_path, Config(), mux_backend=backend)
    app._display_transport().create_primary()
    app._railmux_pane_id = "%controller"
    app._loop = MagicMock()
    app._loop.widget = app._frame

    backend.resize(70, 20)
    app._check_terminal_size()

    assert app._agent_workspace().presentation.value == "compact"
    assert backend.window_is_zoomed("%controller") is True
    assert backend.create_ui_screen().get_cols_rows() == (70, 19)

    backend.resize(100, 30)
    app._check_terminal_size()

    assert app._agent_workspace().presentation.value == "wide"
    assert backend.window_is_zoomed("%controller") is False
    assert backend.create_ui_screen().get_cols_rows() == (30, 29)


def test_native_compact_status_page_click_moves_zoom_to_live_agent(tmp_path):
    backend = WinMuxBackend(width=70, height=20, process_factory=_factory)
    workspace = AgentWorkspace()
    workspace.presentation = type(workspace.presentation).COMPACT
    transport = backend.create_display_transport(workspace, "swap")
    launch = backend.prepare_launch(["codex", "resume", "abc"], tmp_path)
    assert backend.new_detached_session("codex-abc", launch) == (True, None)
    assert transport.attach(workspace.primary, "codex-abc").ok
    assert backend.toggle_pane_zoom("%controller")
    backend.screen_update()
    hit = next(
        hit for hit in backend._status_hits
        if hit.action == f"page:{workspace.primary.pane_id}"
    )

    backend._route_mouse(type("Event", (), {
        "x": hit.start + 1,
        "y": backend.height,
        "button": 0,
        "pressed": True,
    })())
    update = backend.screen_update()

    assert backend.active_pane_id() == workspace.primary.pane_id
    assert backend.window_is_zoomed(workspace.primary.pane_id) is True
    assert (update.width, update.height) == (70, 20)


def test_native_layout_keeps_independent_dividers_across_axis_changes():
    backend = WinMuxBackend(width=160, height=40, process_factory=_factory)
    workspace = AgentWorkspace()
    workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    transport = backend.create_display_transport(workspace, "swap")
    assert transport.create_dual(workspace.layout)
    primary = workspace.primary.pane_id
    assert primary is not None

    assert backend.resize_pane_width(primary, 30)
    assert backend._display_regions()["primary"].width == 30

    workspace.layout = WorkspaceLayout.STACKED
    backend._sync_display_sizes()
    stacked = backend._display_regions()
    assert stacked["primary"].height == 19

    assert backend.resize_pane_height(primary, 14)
    workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    backend._sync_display_sizes()
    assert backend._display_regions()["primary"].width == 30


def test_native_zoom_keeps_logical_hidden_pane_sizes_available():
    backend = WinMuxBackend(width=160, height=40, process_factory=_factory)
    workspace = AgentWorkspace()
    workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    transport = backend.create_display_transport(workspace, "swap")
    assert transport.create_dual(workspace.layout)
    primary = workspace.primary.pane_id
    secondary = workspace.secondary.pane_id
    assert primary is not None and secondary is not None
    before = (backend.pane_size(primary), backend.pane_size(secondary))

    assert backend.toggle_pane_zoom(primary)

    assert backend.pane_size(primary) == before[0]
    assert backend.pane_size(secondary) == before[1]
    assert set(backend._display_regions()) == {"primary"}


def test_native_reset_slot_clears_stale_preview(tmp_path):
    backend = WinMuxBackend(width=100, height=30, process_factory=_factory)
    workspace = AgentWorkspace()
    transport = backend.create_display_transport(workspace, "swap")
    assert transport.create_primary()
    transcript = tmp_path / "preview.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"content":"stale"}}\n',
        encoding="utf-8",
    )
    assert backend.show_transcript(
        workspace.primary.pane_id, transcript, "claude"
    )

    assert transport.reset_slot(workspace.primary)

    assert not backend._previews
    assert backend._terminal_for_display(workspace.primary.pane_id) is None


def test_native_reselect_is_noop_for_last_pane_and_keyframes():
    backend = WinMuxBackend(width=100, height=30, process_factory=_factory)
    workspace = AgentWorkspace()
    transport = backend.create_display_transport(workspace, "swap")
    assert transport.create_primary()
    primary = workspace.primary.pane_id
    assert primary is not None
    assert backend.select_pane(primary)
    assert backend.toggle_pane_zoom(primary)
    backend.screen_update()
    sequence = backend._compositor.sequence

    assert backend.select_pane(primary)

    assert backend.active_pane_id() == primary
    assert backend.last_pane_id() == "%controller"
    assert backend._compositor.sequence == sequence
    assert backend._compositor._last_rows is not None


def test_native_mouse_route_preserves_double_click_event_stream():
    backend = WinMuxBackend(width=100, height=30, process_factory=_factory)
    screen = backend.create_ui_screen()
    screen.start()
    try:
        backend.route_input(
            b"\x1b[<0;5;4M\x1b[<0;5;4m"
            b"\x1b[<0;5;4M\x1b[<0;5;4m"
        )
        keys, _raw = screen.get_input(True)
    finally:
        screen.stop()

    assert keys == [
        ("mouse press", 1, 4, 3),
        ("mouse release", 1, 4, 3),
        ("mouse press", 1, 4, 3),
        ("mouse release", 1, 4, 3),
    ]


def test_native_empty_display_input_falls_back_to_sidebar():
    backend = WinMuxBackend(width=100, height=30, process_factory=_factory)
    workspace = AgentWorkspace()
    workspace.presentation = WorkspacePresentation.COMPACT
    workspace.compact_page = WorkspacePage.PRIMARY
    transport = backend.create_display_transport(workspace, "swap")
    assert transport.create_primary()
    primary = workspace.primary.pane_id
    assert primary is not None
    assert backend.select_pane(primary)
    screen = backend.create_ui_screen()
    screen.start()
    try:
        backend.route_input(b"x")
        keys, _raw = screen.get_input(True)
    finally:
        screen.stop()

    assert "x" in keys
    assert backend.active_pane_id() == "%controller"


def test_native_status_actions_only_accept_plain_left_press():
    backend = WinMuxBackend(width=100, height=30, process_factory=_factory)
    backend.screen_update()
    mode_hit = next(
        hit for hit in backend._status_hits if hit.action == "mode"
    )
    screen = backend.create_ui_screen()
    screen.start()
    try:
        column = mode_hit.start + 1
        backend.route_input(
            f"\x1b[<64;{column};30M".encode()  # wheel up
            + f"\x1b[<32;{column};30M".encode()  # left drag
            + f"\x1b[<0;{column};30M".encode()  # plain left press
        )
        keys, _raw = screen.get_input(True)
    finally:
        screen.stop()

    assert keys.count("f5") == 1
