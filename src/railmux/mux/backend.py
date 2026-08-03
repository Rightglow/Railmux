"""Railmux-level multiplexer contracts, deliberately free of tmux grammar."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from railmux.tmux_ctl import PaneIdentity, ServerSnapshot, SessionTopology


@dataclass(frozen=True)
class Capabilities:
    """Named behavior gates used instead of leaking backend versions."""

    pane_local_options: bool
    resize_window: bool
    binding_notes: bool
    border_indicators: bool
    status_ranges: bool
    grouped_sessions: bool
    process_correlation: bool
    external_binding_leases: bool


@dataclass(frozen=True)
class LaunchSpec:
    """Shell-free provider launch authority used by the Windows daemon."""

    argv: tuple[str, ...]
    cwd: Path
    env: tuple[tuple[str, str], ...] = ()
    login_shell: bool = False


@dataclass(frozen=True)
class StatusChrome:
    """Platform-neutral state for Railmux's persistent bottom chrome.

    POSIX projects this into tmux's status line; native Windows draws it in
    the daemon compositor.  The transient status message is intentionally a
    separate value so it cannot replace the Mode/Layout controls.
    """

    mode_label: str
    layout_indicator: str | None
    error: bool = False


class MuxBackend(Protocol):
    """Core topology/lifecycle surface consumed by the shared UI.

    Display transport and chrome verbs are added here as their existing tmux
    implementations are wrapped.  Windows implementations must expose
    Railmux operations, never tmux command strings or format expressions.
    """

    @property
    def capabilities(self) -> Capabilities: ...

    def server_snapshot(self) -> ServerSnapshot | None: ...
    def session_exists(self, name: str) -> bool: ...
    def session_topology(self, name: str) -> SessionTopology | None: ...
    def pane_alive(self, pane_id: str) -> bool: ...
    def pane_identity(self, pane_id: str) -> PaneIdentity | None: ...
    def active_pane_id(self, target: str | None = None) -> str | None: ...
    def select_pane(self, pane_id: str) -> bool: ...
    def pane_size(self, pane_id: str) -> tuple[int, int] | None: ...
    def window_size(self, pane_id: str) -> tuple[int, int] | None: ...
    def toggle_pane_zoom(self, pane_id: str) -> bool: ...
    def window_is_zoomed(self, pane_id: str) -> bool | None: ...
    def detach_client(self) -> bool: ...
    def kill_pane(self, pane_id: str) -> bool: ...
    def kill_session(self, name: str) -> bool: ...
    def set_status_text(self, text: str, level: str) -> None: ...
    def set_status_chrome(self, chrome: StatusChrome) -> None: ...
