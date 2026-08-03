"""Daemon-owned multiplexer backend for native Windows."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from railmux import tmux_ctl
from railmux.fast_display_input import SgrMouseEvent, TerminalInputDecoder
from railmux.mux.backend import Capabilities, LaunchSpec, StatusChrome
from railmux.platform.process import provider_argv
from railmux.restart_state import OuterTmuxIdentity
from railmux.ui.status_chrome import (
    ACTION_COPY,
    ACTION_LAYOUT,
    ACTION_MODE,
    StatusHit,
    project_status_chrome,
)
from railmux.ui.workspace import (
    DisplayTransportKind,
    WorkspaceLayout,
    WorkspacePage,
    WorkspacePresentation,
)
from railmux.winlocal.compositor import Compositor, Region, TerminalPane
from railmux.winlocal.conpty import ConPtyProcess, PyWinPtyProcess
from railmux.winlocal.history_viewer import HistoryViewer
from railmux.winlocal.session_store import SessionRecord, SessionStore
from railmux.winlocal.virtual_screen import VirtualScreen


@dataclass
class _Session:
    name: str
    session_id: str
    pane_id: str
    launch: LaunchSpec | None = None
    process: ConPtyProcess | None = None
    terminal: TerminalPane | None = None
    options: dict[str, str] = field(default_factory=dict)
    alive: bool = True
    created_at: int = field(default_factory=lambda: int(time.time()))
    process_size: tuple[int, int] | None = None


class WinMuxBackend:
    """Railmux semantics over daemon-owned ConPTY handles, without tmux."""

    RAILMUX_TARGET_OPTION = tmux_ctl.RAILMUX_TARGET_OPTION
    STATUS_ACTION_COPY = tmux_ctl.STATUS_ACTION_COPY

    def __init__(
        self,
        *,
        width: int = 120,
        height: int = 30,
        process_factory: Callable[..., ConPtyProcess] = PyWinPtyProcess.spawn,
        daemon_id: str | None = None,
        session_store: SessionStore | None = None,
        resume_offers: tuple[SessionRecord, ...] = (),
    ) -> None:
        self.width = width
        self.height = height
        self.daemon_id = daemon_id or uuid.uuid4().hex
        self._process_factory = process_factory
        self._session_store = session_store
        self._resume_offers = tuple(
            record for record in resume_offers if record.phase == "resume_offer"
        )
        self._sessions: dict[str, _Session] = {}
        self._panes: dict[str, _Session | None] = {"%controller": None}
        self._pane_options: dict[str, dict[str, str]] = {}
        self._window_options: dict[str, str] = {}
        self._displayed: dict[str, str] = {}
        self._previews: dict[str, HistoryViewer] = {}
        self._next_session = 1
        self._next_pane = 1
        self._active_pane = "%controller"
        self._last_pane = "%controller"
        self._zoomed_pane: str | None = None
        self._detach_callback: Callable[[object | None], None] | None = None
        self._last_input_source: object | None = None
        self._client_count: Callable[[], int] | None = None
        self._workspace = None
        self._sidebar_width: int | None = None
        self._primary_size: int | None = None
        # The sidebar starts as the only surface and therefore owns the full
        # viewport.  Opening the first display pane narrows it through the same
        # geometry synchronization path used by later layout changes.
        self._screen = VirtualScreen(width, max(1, height - 1))
        self._compositor = Compositor(width, height)
        self._status_text = ""
        self._status_level = "tip"
        self._status_chrome = StatusChrome("Claude Code", None)
        self._status_hits: tuple[StatusHit, ...] = ()
        self._lock = threading.RLock()
        self._input_decoder = TerminalInputDecoder()

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            pane_local_options=True,
            resize_window=True,
            binding_notes=False,
            border_indicators=True,
            status_ranges=True,
            grouped_sessions=False,
            process_correlation=True,
            external_binding_leases=False,
        )

    def prepare_launch(self, argv, cwd: Path, *, env=None, login_shell=False) -> LaunchSpec:
        safe_env = tuple(sorted((env or {}).items()))
        return LaunchSpec(tuple(argv), cwd, safe_env, login_shell)

    def capture_outer_identity(self) -> OuterTmuxIdentity:
        digest = hashlib.sha256(self.daemon_id.encode()).hexdigest()
        return OuterTmuxIdentity(
            digest, os.getpid(), "%controller", "$windows", "@windows"
        )

    def create_ui_screen(self) -> VirtualScreen:
        return self._screen

    def create_display_transport(self, workspace, preference: str, **_kwargs):
        with self._lock:
            self._workspace = workspace
            return WinDisplayTransport(self, workspace)

    def has_tmux(self) -> bool:
        return True

    def in_tmux(self) -> bool:
        return False

    def tmux_version(self) -> tuple[int, int]:
        return (99, 0)

    def current_pane_id(self) -> str:
        return self._active_pane

    def current_session_name(self) -> str:
        return "railmux"

    def current_session_id(self) -> str:
        return "$windows"

    def session_attached_count(self, _name: str) -> int:
        return self._client_count() if self._client_count is not None else 1

    def live_session_count(self) -> int:
        with self._lock:
            return sum(
                1 for session in self._sessions.values() if self.session_exists(session.name)
            )

    def use_smallest_window_size(self, _pane_id: str) -> bool:
        return True

    def enable_clipboard_passthrough(self) -> None:
        pass

    def set_status_text(self, text: str, level: str) -> None:
        prefix = {"error": "ERROR: ", "warn": "WARNING: "}.get(level, "")
        with self._lock:
            self._status_text = prefix + text
            self._status_level = level

    def set_status_chrome(self, chrome: StatusChrome) -> None:
        with self._lock:
            self._status_chrome = chrome

    def create_display_pane(self, key: str) -> str:
        with self._lock:
            pane_id = f"%display-{key}"
            self._panes[pane_id] = None
            return pane_id

    def prepare_launch_session(self, name: str, launch: LaunchSpec) -> _Session:
        with self._lock:
            session = self._sessions.get(name)
            if session is None:
                session = _Session(
                    name=name,
                    session_id=f"${self._next_session}",
                    pane_id=f"%{self._next_pane}",
                    launch=launch,
                    terminal=TerminalPane(80, 24),
                )
                self._next_session += 1
                self._next_pane += 1
                self._sessions[name] = session
                self._panes[session.pane_id] = session
            else:
                session.launch = launch
            return session

    def _start(self, session: _Session) -> tuple[bool, str | None]:
        with self._lock:
            launch = session.launch
            if launch is None:
                return False, "launch specification is missing"
            if session.process is not None and session.process.is_alive():
                return True, None
            executable = shutil.which(launch.argv[0]) or launch.argv[0]
            argv = provider_argv(executable, launch.argv[1:], windows=True)
            environment = dict(os.environ)
            environment.update(launch.env)
            try:
                session.process = self._process_factory(
                    argv,
                    cwd=launch.cwd,
                    env=environment,
                    columns=80,
                    rows=24,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                session.alive = False
                self._save_sessions()
                return False, str(exc)
            session.alive = True
            session.process_size = (80, 24)
            self._sync_display_sizes()
            self._save_sessions()
        threading.Thread(
            target=self._read_process,
            args=(session,),
            daemon=True,
            name=f"railmux-conpty-{session.name[:24]}",
        ).start()
        return True, None

    def _read_process(self, session: _Session) -> None:
        process = session.process
        assert process is not None and session.terminal is not None
        while process.is_alive():
            try:
                data = process.read(65536)
            except (EOFError, OSError):
                break
            if data:
                with self._lock:
                    session.terminal.feed(data)
        if not process.is_alive():
            with self._lock:
                session.alive = False
                self._save_sessions()

    def new_detached_session(self, name: str, launch: object, env=None):
        if not isinstance(launch, LaunchSpec):
            return False, "native Windows requires a typed launch specification"
        return self._start(self.prepare_launch_session(name, launch))

    def create_detached_holder(self, name: str, env=None):
        if name in self._sessions:
            return None, "session already exists"
        session = self.prepare_launch_session(
            name, LaunchSpec(("cmd.exe",), Path.cwd())
        )
        return self.pane_identity(session.pane_id), None

    def start_detached_holder(self, identity, launch: object):
        session = self._session_for_pane(identity.pane_id)
        if session is None or not isinstance(launch, LaunchSpec):
            return False, "holder identity or launch specification changed"
        session.launch = launch
        return self._start(session)

    def session_exists(self, name: str) -> bool:
        with self._lock:
            session = self._lookup_session(name)
            return bool(
                session
                and session.alive
                and (session.process is None or session.process.is_alive())
            )

    def pane_alive(self, pane_id: str) -> bool:
        if pane_id.startswith("%display-") or pane_id == "%controller":
            return pane_id in self._panes
        session = self._session_for_pane(pane_id)
        return bool(
            session
            and session.alive
            and (session.process is None or session.process.is_alive())
        )

    def _session_for_pane(self, pane_id: str) -> _Session | None:
        value = self._panes.get(pane_id)
        return value if isinstance(value, _Session) else None

    def pane_identity(self, pane_id: str):
        session = self._session_for_pane(pane_id)
        if session is None:
            if pane_id not in self._panes:
                return None
            return tmux_ctl.PaneIdentity(
                pane_id, os.getpid(), "railmux", "$windows", "@windows", False,
                self.width, max(1, self.height - 1),
            )
        process = session.process
        pane_width, pane_height = self.pane_size(pane_id) or (80, 24)
        return tmux_ctl.PaneIdentity(
            pane_id,
            process.pid if process is not None else os.getpid(),
            session.name,
            session.session_id,
            f"@{session.session_id[1:]}",
            not session.alive or bool(process and not process.is_alive()),
            pane_width,
            pane_height,
        )

    def session_topology(self, name: str):
        session = self._lookup_session(name)
        if session is None:
            return None
        identity = self.pane_identity(session.pane_id)
        return tmux_ctl.SessionTopology(
            name, session.session_id, 0, (f"@{session.session_id[1:]}",),
            (identity,) if identity is not None else (),
        )

    def server_snapshot(self):
        with self._lock:
            live = [
                session
                for session in self._sessions.values()
                if self.session_exists(session.name)
            ]
            panes = frozenset(
                pane_id
                for pane_id in self._panes
                if self.pane_alive(pane_id)
            )
            return tmux_ctl.ServerSnapshot(
                frozenset(session.name for session in live),
                panes,
                tuple(
                    (session.name, session.process.pid)
                    for session in live
                    if session.process
                ),
            )

    def select_pane(self, pane_id: str) -> bool:
        with self._lock:
            if not self.pane_alive(pane_id):
                return False
            self._last_pane, self._active_pane = self._active_pane, pane_id
            # tmux status-page selection inside a zoomed window displays the
            # newly selected pane.  Preserve that semantic in the native
            # compositor instead of leaving zoom ownership on an invisible
            # previous page.
            if self._zoomed_pane is not None:
                self._zoomed_pane = pane_id
                self._sync_display_sizes()
                self.request_keyframe()
            return True

    def active_pane_id(self, _target: str | None = None) -> str:
        return self._active_pane

    def last_pane_id(self, _target: str | None = None) -> str:
        return self._last_pane

    def pane_size(self, pane_id: str):
        with self._lock:
            regions = self._display_regions()
            if pane_id == "%controller":
                region = regions.get("sidebar")
                return (region.width, region.height) if region else None
            workspace = self._workspace
            if workspace is not None:
                for name, slot in (
                    ("primary", workspace.primary),
                    ("secondary", workspace.secondary),
                ):
                    if pane_id == slot.pane_id:
                        region = regions.get(name)
                        return (region.width, region.height) if region else None
            session = self._session_for_pane(pane_id)
            if session is not None and session.terminal is not None:
                return (
                    session.terminal.screen.columns,
                    session.terminal.screen.lines,
                )
            return None

    def window_size(self, _pane_id: str):
        return (self.width, self.height)

    def resize_pane_width(self, pane_id: str, width: int) -> bool:
        with self._lock:
            workspace = self._workspace
            if pane_id == "%controller":
                self._sidebar_width = width
            elif (
                workspace is not None
                and pane_id == workspace.primary.pane_id
                and workspace.layout is WorkspaceLayout.SIDE_BY_SIDE
            ):
                self._primary_size = width
            else:
                return False
            self._sync_display_sizes()
            self.request_keyframe()
            return True

    def resize_pane_height(self, pane_id: str, height: int) -> bool:
        with self._lock:
            workspace = self._workspace
            if (
                workspace is None
                or pane_id != workspace.primary.pane_id
                or workspace.layout is not WorkspaceLayout.STACKED
            ):
                return False
            self._primary_size = height
            self._sync_display_sizes()
            self.request_keyframe()
            return True

    def toggle_pane_zoom(self, _pane_id: str) -> bool:
        with self._lock:
            if not self.pane_alive(_pane_id):
                return False
            self._zoomed_pane = None if self._zoomed_pane == _pane_id else _pane_id
            self._sync_display_sizes()
            self.request_keyframe()
            return True

    def window_is_zoomed(self, pane_id: str) -> bool | None:
        return self._zoomed_pane == pane_id

    def configure_clients(
        self,
        *,
        detach: Callable[[object | None], None],
        count: Callable[[], int],
    ) -> None:
        self._detach_callback = detach
        self._client_count = count

    def detach_client(self) -> bool:
        if self._detach_callback is None:
            return False
        self._detach_callback(self._last_input_source)
        return True

    def split_window_h(self, _command: object, **_kwargs):
        return self.create_display_pane(f"dynamic-{len(self._panes)}")

    def respawn_pane(self, pane_id: str, _command: object) -> bool:
        return pane_id in self._panes

    def kill_pane(self, pane_id: str) -> bool:
        with self._lock:
            if pane_id.startswith("%display-"):
                self._panes.pop(pane_id, None)
                self._displayed.pop(pane_id, None)
                self._previews.pop(pane_id, None)
                return True
            session = self._session_for_pane(pane_id)
            return self.kill_session(session.name) if session else False

    def kill_session(self, name: str) -> bool:
        with self._lock:
            session = self._lookup_session(name)
            if session is None:
                return False
            if session.process and session.process.is_alive():
                session.process.terminate(force=True)
            session.alive = False
            self._sessions.pop(session.name, None)
            self._panes.pop(session.pane_id, None)
            self._save_sessions()
            return True

    def kill_session_identity(self, identity) -> bool:
        session = self._session_for_pane(identity.pane_id)
        return bool(session and session.session_id == identity.session_id and self.kill_session(session.name))

    def set_session_user_option(self, name: str, key: str, value: str | None) -> bool:
        session = self._lookup_session(name)
        if session is None:
            return False
        if value is None:
            session.options.pop(key, None)
        else:
            session.options[key] = value
        return True

    def show_session_user_option(self, name: str, key: str):
        session = self._lookup_session(name)
        return session.options.get(key) if session else None

    def detached_single_pane_start_command(self, *_args, **_kwargs):
        # Native sessions have never used the legacy pre-v2 tmux command form.
        return None

    def owned_session_rows(self) -> tuple[tuple[str, ...], ...]:
        """Return the bounded recovery fields consumed by shared discovery."""
        from railmux import orphan_marker

        rows = []
        for session in self._sessions.values():
            if not self.session_exists(session.name) or session.launch is None:
                continue
            rows.append((
                session.name,
                str(session.launch.cwd),
                str(session.created_at),
                session.session_id,
                session.pane_id,
                session.options.get(orphan_marker.OPTION_NAME, ""),
                session.options.get("@railmux_binding_v1", ""),
            ))
        return tuple(rows)

    def set_pane_user_option(self, pane_id: str, key: str, value: str | None) -> bool:
        options = self._pane_options.setdefault(pane_id, {})
        if value is None:
            options.pop(key, None)
        else:
            options[key] = value
        return pane_id in self._panes

    def set_window_user_option(self, _target: str, key: str, value: str | None) -> bool:
        if value is None:
            self._window_options.pop(key, None)
        else:
            self._window_options[key] = value
        return True

    def show_window_user_option(self, _target: str, key: str):
        return self._window_options.get(key)

    def unset_window_user_option_if_value(self, target: str, key: str, expected: str) -> bool:
        if self.show_window_user_option(target, key) != expected:
            return False
        return self.set_window_user_option(target, key, None)

    def set_window_option(self, _name: str, _value: str | None):
        return True

    def local_window_option(self, _name: str):
        return True, None

    def set_window_border_styles(self, _active: str, _inactive: str) -> bool:
        return True

    def window_border_styles(self):
        return True, ("green", "gray")

    def status_action_range(self, _action: str, content: str) -> str:
        return content

    def copy_to_clipboard(self, _text: str) -> bool:
        from railmux.local_clipboard import copy

        return copy(_text.encode("utf-8"))

    def show_transcript(self, pane_id: str, path: Path, fmt: str) -> bool:
        with self._lock:
            if pane_id not in self._panes:
                return False
            regions = self._display_regions()
            workspace = self._workspace
            name = (
                "primary"
                if workspace is not None and pane_id == workspace.primary.pane_id
                else "secondary"
            )
            region = regions.get(name)
            if region is None:
                region_width, region_height = 80, max(1, self.height - 1)
            else:
                region_width, region_height = region.width, region.height
            try:
                self._previews[pane_id] = HistoryViewer(
                    path, fmt, region_width, region_height
                )
            except OSError:
                return False
            self._displayed.pop(pane_id, None)
            return True

    def proc_fs_available(self) -> bool:
        return False

    def process_tree_rollout_ids(self, *_args):
        return None

    def session_rollout_ids(self, *_args):
        return None

    def session_process_has_exact_arg(self, *_args):
        return None

    def process_has_child(self, pid: int):
        return any(session.process and session.process.pid == pid and session.process.is_alive() for session in self._sessions.values())

    def session_has_child(self, name: str):
        return self.session_exists(name)

    def session_process_ids(self, name: str):
        session = self._sessions.get(name)
        return (session.process.pid,) if session and session.process else ()

    def wait_for_processes_exit(self, pids, timeout: float = 2.0) -> bool:
        return not any(session.process and session.process.pid in pids and session.process.is_alive() for session in self._sessions.values())

    def screen_update(self):
        with self._lock:
            self._sync_display_sizes()
            status = self._project_status()
            self._status_hits = status.hits
            workspace = self._workspace
            primary = secondary = None
            stacked = False
            if workspace is not None:
                primary = self._terminal_for_display(workspace.primary.pane_id)
                secondary = self._terminal_for_display(workspace.secondary.pane_id)
                stacked = workspace.layout is WorkspaceLayout.STACKED
            if self._zoomed_pane is not None:
                zoomed = (
                    self._screen.pane
                    if self._zoomed_pane == "%controller"
                    else self._terminal_for_display(self._zoomed_pane)
                )
                if zoomed is not None:
                    return self._compositor.compose_full(
                        zoomed,
                        status=status.text.encode("utf-8", errors="replace"),
                        status_error=status.error,
                    )
            focus = "sidebar"
            if workspace is not None and self._active_pane == workspace.primary.pane_id:
                focus = "primary"
            elif workspace is not None and self._active_pane == workspace.secondary.pane_id:
                focus = "secondary"
            return self._compositor.compose(
                self._screen.pane, primary, secondary, stacked=stacked,
                status=status.text.encode("utf-8", errors="replace"),
                status_error=status.error,
                focus=focus,
                show_primary=bool(
                    workspace is not None and workspace.primary.pane_id
                ),
                show_secondary=bool(
                    workspace is not None
                    and workspace.secondary.pane_id
                    and workspace.layout is not WorkspaceLayout.SINGLE
                ),
                layout=workspace.layout if workspace is not None else None,
                sidebar_width=self._sidebar_width,
                primary_size=self._primary_size,
            )

    def request_keyframe(self) -> None:
        with self._lock:
            self._compositor.invalidate()

    def resize(self, width: int, height: int) -> None:
        """Resize the composed display, Urwid sidebar, and visible ConPTYs."""
        if not 40 <= width <= 1000 or not 12 <= height <= 500:
            raise ValueError("invalid terminal geometry")
        with self._lock:
            self.width, self.height = width, height
            self._compositor.resize(width, height)
            self._sync_display_sizes()

    def route_input(self, data: bytes, *, source: object | None = None) -> None:
        """Route keyboard and SGR mouse reports to the focused surface."""
        with self._lock:
            self._last_input_source = source
            for part in self._input_decoder.feed(data):
                if isinstance(part, SgrMouseEvent):
                    self._route_mouse(part)
                elif part:
                    self._write_active(part)

    def flush_pending_input(self) -> None:
        with self._lock:
            for data in self._input_decoder.flush_pending():
                self._write_active(data)

    def _sync_display_sizes(self) -> None:
        """Apply compositor geometry to Urwid, previews, and every ConPTY.

        A topology change can narrow the sidebar without an outer terminal
        resize.  Urwid must receive that viewport change too; otherwise it
        keeps drawing its old wide canvas into a narrow terminal emulator and
        wrapped fragments appear as duplicated rows.
        """
        regions = self._display_regions()
        sidebar = regions.get("sidebar")
        if (
            sidebar is not None
            and self._screen.get_cols_rows() != (sidebar.width, sidebar.height)
        ):
            self._screen.resize(sidebar.width, sidebar.height)
        for name in ("primary", "secondary"):
            region = regions.get(name)
            pane_id = self._pane_for_region(name)
            preview = self._previews.get(pane_id or "")
            if region is not None and preview is not None:
                if (preview.width, preview.height) != (
                    region.width, region.height
                ):
                    preview.resize(region.width, region.height)
            session = self._session_for_region(name)
            if region is None or session is None:
                continue
            if session.terminal is not None:
                if (
                    session.terminal.screen.columns,
                    session.terminal.screen.lines,
                ) != (region.width, region.height):
                    session.terminal.resize(region.width, region.height)
            if (
                session.process is not None
                and session.process.is_alive()
                and session.process_size != (region.width, region.height)
            ):
                session.process.resize(region.width, region.height)
                session.process_size = (region.width, region.height)

    def _write_active(self, data: bytes) -> None:
        with self._lock:
            if self._active_pane == "%controller":
                self._screen.inject(data)
                return
            preview = self._previews.get(self._active_pane)
            if preview is not None:
                if not preview.input(data):
                    self.kill_pane(self._active_pane)
                return
            session = self._session_for_display(self._active_pane)
            if session is None:
                session = self._session_for_pane(self._active_pane)
            if session is not None and session.process is not None:
                session.process.write(data)

    def _route_mouse(self, event: SgrMouseEvent) -> None:
        x, y = event.x - 1, event.y - 1
        with self._lock:
            if y == self.height - 1:
                if event.pressed and event.button & 3 == 0:
                    self._route_status_click(x)
                return
            for name, region in self._display_regions().items():
                if not (
                    region.x <= x < region.x + region.width
                    and region.y <= y < region.y + region.height
                ):
                    continue
                local = _mouse_bytes(
                    event,
                    x=x - region.x + 1,
                    y=y - region.y + 1,
                )
                if name == "sidebar":
                    self.select_pane("%controller")
                    self._screen.inject(local)
                    return
                pane_id = self._pane_for_region(name)
                if pane_id is None:
                    return
                self.select_pane(pane_id)
                preview = self._previews.get(pane_id)
                if preview is not None:
                    if not preview.input(local):
                        self.kill_pane(pane_id)
                    return
                session = self._session_for_display(pane_id)
                if session is not None and session.process is not None:
                    session.process.write(local)
                return

    def _display_regions(self):
        if self._zoomed_pane is not None:
            name = "sidebar"
            workspace = self._workspace
            if (
                workspace is not None
                and self._zoomed_pane == workspace.primary.pane_id
            ):
                name = "primary"
            elif (
                workspace is not None
                and self._zoomed_pane == workspace.secondary.pane_id
            ):
                name = "secondary"
            return {
                name: Region(0, 0, self.width, max(1, self.height - 1))
            }
        workspace = self._workspace
        return self._compositor.regions(
            has_primary=bool(
                workspace is not None and workspace.primary.pane_id
            ),
            has_secondary=bool(
                workspace is not None
                and workspace.secondary.pane_id
                and workspace.layout is not WorkspaceLayout.SINGLE
            ),
            stacked=bool(
                workspace is not None
                and workspace.layout is WorkspaceLayout.STACKED
            ),
            layout=workspace.layout if workspace is not None else None,
            sidebar_width=self._sidebar_width,
            primary_size=self._primary_size,
        )

    def _project_status(self):
        workspace = self._workspace
        compact = bool(
            workspace is not None
            and workspace.presentation is WorkspacePresentation.COMPACT
        )
        page = (
            workspace.compact_page
            if workspace is not None else WorkspacePage.SIDEBAR
        )
        targets = (
            "%controller",
            workspace.primary.pane_id if workspace is not None else None,
            workspace.secondary.pane_id if workspace is not None else None,
        )
        return project_status_chrome(
            width=self.width,
            mode_label=self._status_chrome.mode_label,
            layout_indicator=self._status_chrome.layout_indicator,
            status_text=self._status_text,
            status_level=(
                "error" if self._status_chrome.error else self._status_level
            ),
            compact=compact,
            active_page=page,
            page_targets=targets,
        )

    def _route_status_click(self, column: int) -> None:
        hit = next(
            (candidate for candidate in self._status_hits
             if candidate.start <= column < candidate.end),
            None,
        )
        if hit is None:
            return
        if hit.action == ACTION_MODE:
            self._screen.inject(b"\x1b[15~")  # F5
        elif hit.action == ACTION_LAYOUT:
            self._screen.inject(b"\x1b[18~")  # F7
        elif hit.action == ACTION_COPY:
            self._screen.inject(b"\x1b[17~")  # F6
        elif hit.action.startswith("page:"):
            pane_id = hit.action[5:]
            if self.pane_alive(pane_id):
                self.select_pane(pane_id)

    def _pane_for_region(self, name: str) -> str | None:
        workspace = self._workspace
        if workspace is None:
            return None
        slot = workspace.primary if name == "primary" else workspace.secondary
        return slot.pane_id

    def _session_for_region(self, name: str) -> _Session | None:
        pane_id = self._pane_for_region(name)
        return self._session_for_display(pane_id) if pane_id else None

    def _session_for_display(self, pane_id: str) -> _Session | None:
        name = self._displayed.get(pane_id)
        return self._sessions.get(name) if name else None

    def _save_sessions(self) -> None:
        if self._session_store is None:
            return
        records = []
        for session in self._sessions.values():
            launch = session.launch
            if launch is None:
                continue
            executable = Path(launch.argv[0]).name.lower()
            provider = "claude" if "claude" in executable else "codex"
            process = session.process
            records.append(SessionRecord(
                record_id=session.name,
                provider=provider,
                cwd=str(launch.cwd),
                phase=(
                    "resolved"
                    if session.alive and process is not None and process.is_alive()
                    else "stopped"
                ),
                daemon_id=self.daemon_id,
                provider_session_id=_provider_session_id(launch.argv),
                pid=process.pid if process is not None and process.is_alive() else None,
            ))
        live_ids = {record.record_id for record in records}
        records.extend(
            offer for offer in self._resume_offers if offer.record_id not in live_ids
        )
        self._session_store.save(tuple(records))

    def _terminal_for_display(self, pane_id: str | None):
        preview = self._previews.get(pane_id or "")
        if preview is not None:
            return preview.terminal
        name = self._displayed.get(pane_id or "")
        session = self._sessions.get(name or "")
        return session.terminal if session else None

    def _lookup_session(self, reference: str) -> _Session | None:
        direct = self._sessions.get(reference)
        if direct is not None:
            return direct
        return next(
            (
                session
                for session in self._sessions.values()
                if session.session_id == reference
            ),
            None,
        )


def _mouse_bytes(event: SgrMouseEvent, *, x: int, y: int) -> bytes:
    terminator = b"M" if event.pressed else b"m"
    return b"\x1b[<" + f"{event.button};{x};{y}".encode() + terminator


def _provider_session_id(argv: tuple[str, ...]) -> str | None:
    for index, value in enumerate(argv[:-1]):
        if value in {"--resume", "resume", "--session-id"}:
            candidate = argv[index + 1]
            if re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", candidate):
                return candidate
    return None


class WinDisplayTransport:
    def __init__(self, backend: WinMuxBackend, workspace) -> None:
        self.backend = backend
        self.workspace = workspace

    def swap_capable(self):
        return True, None

    def create_primary(self, **_kwargs) -> bool:
        if self.workspace.primary.pane_id is None:
            self.workspace.primary.pane_id = self.backend.create_display_pane("primary")
            with self.backend._lock:
                self.backend._sync_display_sizes()
        return True

    def create_secondary(self, _layout) -> bool:
        self.create_primary()
        if self.workspace.secondary.pane_id is None:
            self.workspace.secondary.pane_id = self.backend.create_display_pane("secondary")
            with self.backend._lock:
                self.backend._sync_display_sizes()
        return True

    def create_dual(self, layout, **_kwargs) -> bool:
        return self.create_secondary(layout)

    def attach(self, slot, agent_tmux_name: str, **_kwargs):
        from railmux.display_transport import AttachOutcome

        if not self.backend.session_exists(agent_tmux_name):
            return AttachOutcome(False, DisplayTransportKind.SWAP, "agent is not live")
        if slot.pane_id is None:
            slot.pane_id = self.backend.create_display_pane(slot.key)
        with self.backend._lock:
            self.backend._previews.pop(slot.pane_id, None)
            self.backend._displayed[slot.pane_id] = agent_tmux_name
            slot.agent_tmux_name = agent_tmux_name
            slot.transport_kind = DisplayTransportKind.SWAP
            self.backend._sync_display_sizes()
        return AttachOutcome(True, DisplayTransportKind.SWAP)

    def park(self, slot) -> bool:
        slot.display_parked = True
        return True

    def resume(self, slot) -> bool:
        slot.display_parked = False
        return True

    def return_home(self, slot, **_kwargs) -> bool:
        with self.backend._lock:
            if slot.pane_id:
                self.backend._displayed.pop(slot.pane_id, None)
                self.backend._previews.pop(slot.pane_id, None)
            self.backend._sync_display_sizes()
        return True

    def prepare_preview(self, slot) -> bool:
        return self.return_home(slot)

    def reset_slot(self, slot) -> bool:
        with self.backend._lock:
            if slot.pane_id:
                self.backend._displayed.pop(slot.pane_id, None)
            slot.clear_content()
            self.backend._sync_display_sizes()
        return True

    def prepare_kill(self, _name):
        from railmux.display_transport import KillPreparation
        return KillPreparation(True)

    def close_slot(self, slot) -> bool:
        if slot.pane_id:
            self.backend.kill_pane(slot.pane_id)
        slot.clear_display()
        with self.backend._lock:
            self.backend._sync_display_sizes()
        return True

    def close_all(self) -> bool:
        return all(self.close_slot(slot) for slot in self.workspace.slots if slot.pane_id)

    def displayed_real_pane(self, name: str):
        session = self.backend._sessions.get(name)
        return session.pane_id if session else None

    def displayed_real_pid(self, name: str):
        session = self.backend._sessions.get(name)
        return session.process.pid if session and session.process else None

    def reap_dead_display(self, slot):
        name = slot.agent_tmux_name
        if name and not self.backend.session_exists(name):
            self.reset_slot(slot)
            return name
        return None

    def fallback_for_external_client(self, *_args, **_kwargs):
        # Native Windows has no external tmux client or nested-display
        # fallback. ``App`` treats ``None`` as not applicable and otherwise
        # requires an AttachOutcome-compatible object.
        return None

    def outer_session_lost(self) -> bool:
        return False
