"""POSIX backend that delegates to the established tmux implementation."""
from __future__ import annotations

from collections.abc import Callable
from types import ModuleType
from pathlib import Path

from railmux import tmux_ctl
from railmux.mux.backend import Capabilities


class TmuxBackend:
    """A behavior-preserving typed view over :mod:`railmux.tmux_ctl`."""

    def __init__(
        self,
        authority: Callable[[], ModuleType] | None = None,
    ) -> None:
        self._authority = authority or (lambda: tmux_ctl)

    @property
    def capabilities(self) -> Capabilities:
        authority = self._authority()
        version = authority.tmux_version()
        return Capabilities(
            pane_local_options=version >= (3, 0),
            resize_window=version >= (2, 9),
            binding_notes=version >= (3, 2),
            border_indicators=version >= (3, 3),
            status_ranges=version >= (3, 4),
            grouped_sessions=True,
            process_correlation=authority.proc_fs_available(),
            external_binding_leases=True,
        )

    def server_snapshot(self):
        return self._authority().server_snapshot()

    def session_exists(self, name: str) -> bool:
        return self._authority().session_exists(name)

    def session_topology(self, name: str):
        return self._authority().session_topology(name)

    def pane_alive(self, pane_id: str) -> bool:
        return self._authority().pane_alive(pane_id)

    def pane_identity(self, pane_id: str):
        return self._authority().pane_identity(pane_id)

    def active_pane_id(self, target: str | None = None):
        return self._authority().active_pane_id(target)

    def select_pane(self, pane_id: str) -> bool:
        return self._authority().select_pane(pane_id)

    def pane_size(self, pane_id: str):
        return self._authority().pane_size(pane_id)

    def window_size(self, pane_id: str):
        return self._authority().window_size(pane_id)

    def toggle_pane_zoom(self, pane_id: str) -> bool:
        return self._authority().toggle_pane_zoom(pane_id)

    def kill_pane(self, pane_id: str) -> bool:
        return self._authority().kill_pane(pane_id)

    def kill_session(self, name: str) -> bool:
        return self._authority().kill_session(name)

    def window_is_zoomed(self, pane_id: str) -> bool | None:
        import subprocess

        try:
            result = subprocess.run(
                ["tmux", "display-message", "-p", "-t", pane_id,
                 "-F", "#{window_zoomed_flag}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError:
            return None
        value = result.stdout.strip()
        return (
            value == "1"
            if result.returncode == 0 and value in {"0", "1"}
            else None
        )

    def detach_client(self) -> bool:
        import subprocess

        try:
            return subprocess.run(
                ["tmux", "detach-client"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode == 0
        except OSError:
            return False

    def create_display_transport(self, workspace, preference: str, **kwargs):
        """Keep tmux swap/nested mechanics private to the POSIX backend."""
        from railmux.display_transport import AgentDisplayTransport

        return AgentDisplayTransport(workspace, preference, **kwargs)

    def create_ui_screen(self):
        """Construct the established POSIX raw screen without changing flags."""
        import urwid

        return urwid.raw_display.Screen(
            focus_reporting=True,
            bracketed_paste_mode=True,
        )

    def capture_outer_identity(self):
        from railmux import restart_state

        return restart_state.capture_outer_identity()

    def set_status_text(self, _text: str, _level: str) -> None:
        # POSIX status rendering remains in App because it owns the established
        # tmux option formatting; native rendering uses this backend hook.
        pass

    def prepare_launch(
        self,
        argv: list[str],
        cwd: Path,
        *,
        env: dict[str, str] | None = None,
        login_shell: bool = False,
    ) -> str:
        """Preserve the established POSIX shell grammar byte-for-byte."""
        import shlex

        quoted = " ".join(shlex.quote(value) for value in argv)
        exports = ""
        if env:
            for key, value in env.items():
                exports += f"export {shlex.quote(key)}={shlex.quote(value)} && "
        if login_shell:
            return (
                f"cd {shlex.quote(str(cwd))} && exec $SHELL -li -c "
                f"{shlex.quote(exports + 'exec ' + quoted)}"
            )
        return f"{exports}cd {shlex.quote(str(cwd))} && exec {quoted}"

    def __getattr__(self, name: str):
        """Delegate POSIX-only compatibility operations to tmux authority.

        The shared cross-platform operations are explicit methods above.
        Existing tmux-specific diagnostics and capability probes stay on their
        long-standing implementation so the POSIX argv contract is unchanged.
        """
        return getattr(self._authority(), name)
