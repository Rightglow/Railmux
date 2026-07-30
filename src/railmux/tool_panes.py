"""Managed shell and reusable Vim surfaces below Railmux agent slots.

Each slot owns at most one long-lived shell and one Vim viewer process.
Only one of those panes is joined to the managed Railmux window at a time; the
other remains alive in a private parking session on the same tmux server.
Layout rebuilds park the visible process before changing topology and restore
it below the slot afterwards.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from railmux import tmux_server


_SCHEMA = 1
_SLOTS = frozenset({"primary", "secondary"})
_STATE_OPTIONS = {
    "primary": "@railmux_tool_primary_v1",
    "secondary": "@railmux_tool_secondary_v1",
}
_OWNER_OPTIONS = {
    "primary": "@railmux_agent_primary_v1",
    "secondary": "@railmux_agent_secondary_v1",
}
_KEEPER_OPTION = "@railmux_tool_keeper_v1"
TOOL_PANE_OPTION = "@railmux_tool_surface_v1"
_DUMMY_COMMAND = "exec sleep 2147483647"


@dataclass(frozen=True)
class PaneRef:
    pane_id: str
    pane_pid: int
    session_id: str
    window_id: str

    def to_json(self) -> dict[str, object]:
        return {
            "pane_id": self.pane_id,
            "pane_pid": self.pane_pid,
            "session_id": self.session_id,
            "window_id": self.window_id,
        }


@dataclass(frozen=True)
class ToolState:
    slot: str
    outer_session_id: str
    owner: PaneRef
    shell: PaneRef | None
    viewer: PaneRef | None
    active: str
    parking_session: str | None
    parking_session_id: str | None
    placeholder: PaneRef | None

    def to_json(self) -> str:
        value = {
            "version": _SCHEMA,
            "slot": self.slot,
            "outer_session_id": self.outer_session_id,
            "owner": self.owner.to_json(),
            "shell": self.shell.to_json() if self.shell else None,
            "viewer": self.viewer.to_json() if self.viewer else None,
            "active": self.active,
            "parking_session": self.parking_session,
            "parking_session_id": self.parking_session_id,
            "placeholder": (
                self.placeholder.to_json() if self.placeholder else None
            ),
        }
        return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    message: str
    pane_id: str | None = None
    level: str = "info"


def _decode_ref(raw: object) -> PaneRef | None:
    if not isinstance(raw, dict) or set(raw) != {
        "pane_id", "pane_pid", "session_id", "window_id",
    }:
        return None
    pane_id = raw.get("pane_id")
    pane_pid = raw.get("pane_pid")
    session_id = raw.get("session_id")
    window_id = raw.get("window_id")
    if (
        not isinstance(pane_id, str)
        or not re.fullmatch(r"%[0-9]+", pane_id)
        or not isinstance(pane_pid, int)
        or isinstance(pane_pid, bool)
        or pane_pid <= 0
        or not isinstance(session_id, str)
        or not re.fullmatch(r"\$[0-9]+", session_id)
        or not isinstance(window_id, str)
        or not re.fullmatch(r"@[0-9]+", window_id)
    ):
        return None
    return PaneRef(pane_id, pane_pid, session_id, window_id)


def decode_state(raw: str, *, slot: str, outer_session_id: str) -> ToolState | None:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("version") != _SCHEMA
        or value.get("slot") != slot
        or value.get("outer_session_id") != outer_session_id
        or value.get("active") not in {"shell", "viewer"}
    ):
        return None
    owner = _decode_ref(value.get("owner"))
    shell = _decode_ref(value.get("shell")) if value.get("shell") else None
    viewer = _decode_ref(value.get("viewer")) if value.get("viewer") else None
    placeholder = (
        _decode_ref(value.get("placeholder"))
        if value.get("placeholder")
        else None
    )
    parking_session = value.get("parking_session")
    parking_session_id = value.get("parking_session_id")
    if owner is None or (shell is None and viewer is None):
        return None
    if (parking_session is None) != (parking_session_id is None):
        return None
    if parking_session is not None and (
        not isinstance(parking_session, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", parking_session)
        or not isinstance(parking_session_id, str)
        or not re.fullmatch(r"\$[0-9]+", parking_session_id)
    ):
        return None
    return ToolState(
        slot=slot,
        outer_session_id=outer_session_id,
        owner=owner,
        shell=shell,
        viewer=viewer,
        active=value["active"],
        parking_session=parking_session,
        parking_session_id=parking_session_id,
        placeholder=placeholder,
    )


def is_tool_pane_marker(raw: str, *, outer_session_id: str) -> bool:
    """Recognize one bounded tool-pane marker owned by this outer session."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(value, dict)
        and set(value) == {
            "version", "outer_session_id", "slot", "kind",
        }
        and value.get("version") == _SCHEMA
        and value.get("outer_session_id") == outer_session_id
        and value.get("slot") in _SLOTS
        and value.get("kind") in {"shell", "viewer"}
    )


class ToolPaneManager:
    """Identity-checked tmux operations for the two managed tool surfaces."""

    def __init__(
        self,
        outer_session_id: str,
        *,
        target: tmux_server.TmuxServerTarget | None = None,
    ) -> None:
        if not re.fullmatch(r"\$[0-9]+", outer_session_id):
            raise ValueError("invalid outer tmux session identity")
        if target is None:
            target = tmux_server.current_target()
        if target is None:
            target = tmux_server.discover_target(timeout=1.0)
        if target is None:
            raise RuntimeError("Railmux tmux server is unavailable")
        self.outer_session_id = outer_session_id
        self.target = target

    def _argv(self, *args: str) -> list[str]:
        return tmux_server.target_argv(self.target, *args)

    def _output(self, *args: str, timeout: float = 1.0) -> str | None:
        try:
            value = subprocess.check_output(
                self._argv(*args),
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=timeout,
            ).rstrip("\n")
            return value or None
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            UnicodeError,
        ):
            return None

    def _run(self, *args: str, timeout: float = 1.0) -> bool:
        try:
            result = subprocess.run(
                self._argv(*args),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _pane_ref(self, pane_id: str) -> PaneRef | None:
        if not re.fullmatch(r"%[0-9]+", pane_id):
            return None
        raw = self._output(
            "display-message",
            "-p",
            "-t",
            pane_id,
            "#{pane_id}\t#{pane_pid}\t#{session_id}\t#{window_id}\t#{pane_dead}",
        )
        if raw is None:
            return None
        fields = raw.split("\t")
        if len(fields) != 5 or fields[4] != "0":
            return None
        try:
            pane_pid = int(fields[1])
        except ValueError:
            return None
        return _decode_ref({
            "pane_id": fields[0],
            "pane_pid": pane_pid,
            "session_id": fields[2],
            "window_id": fields[3],
        })

    def _exact_ref(self, ref: PaneRef | None) -> PaneRef | None:
        if ref is None:
            return None
        current = self._pane_ref(ref.pane_id)
        if current is None or current.pane_pid != ref.pane_pid:
            return None
        return current

    def _show_option(self, name: str) -> str | None:
        return self._output(
            "show-options", "-v", "-t", self.outer_session_id, name
        )

    def _outer_window_ids(self) -> frozenset[str]:
        raw = self._output(
            "list-windows",
            "-t",
            self.outer_session_id,
            "-F",
            "#{window_id}",
        )
        if raw is None:
            return frozenset()
        return frozenset(
            value
            for value in raw.splitlines()
            if re.fullmatch(r"@[0-9]+", value)
        )

    def _set_option(self, name: str, value: str | None) -> bool:
        if value is None:
            return self._run(
                "set-option", "-u", "-t", self.outer_session_id, name
            )
        return self._run(
            "set-option", "-t", self.outer_session_id, name, value
        )

    def _set_option_if_changed(self, name: str, value: str | None) -> bool:
        current = self._show_option(name)
        if (current or None) == value:
            return True
        return self._set_option(name, value)

    def _tool_marker(self, slot: str, kind: str) -> str:
        return json.dumps(
            {
                "version": _SCHEMA,
                "outer_session_id": self.outer_session_id,
                "slot": slot,
                "kind": kind,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def _mark_tool(self, pane: PaneRef, slot: str, kind: str) -> bool:
        return self._run(
            "set-option",
            "-p",
            "-t",
            pane.pane_id,
            TOOL_PANE_OPTION,
            self._tool_marker(slot, kind),
        )

    def load(self, slot: str) -> ToolState | None:
        if slot not in _SLOTS:
            return None
        raw = self._show_option(_STATE_OPTIONS[slot])
        if not raw:
            return None
        state = decode_state(
            raw, slot=slot, outer_session_id=self.outer_session_id
        )
        if state is None:
            return None
        owner = self._exact_ref(state.owner) or state.owner
        shell = self._exact_ref(state.shell)
        viewer = self._exact_ref(state.viewer)
        placeholder = self._exact_ref(state.placeholder)
        if shell is None and viewer is None:
            self._set_option(_STATE_OPTIONS[slot], None)
            self._cleanup_parking(state)
            return None
        active = state.active
        if active == "shell" and shell is None:
            active = "viewer"
        elif active == "viewer" and viewer is None:
            active = "shell"
        return ToolState(
            slot,
            state.outer_session_id,
            owner,
            shell,
            viewer,
            active,
            state.parking_session,
            state.parking_session_id,
            placeholder,
        )

    def _save(self, state: ToolState) -> bool:
        return self._set_option(_STATE_OPTIONS[state.slot], state.to_json())

    def sync_owners(self, owners: Mapping[str, str | None]) -> None:
        """Publish exact agent slot identities for the SSH display helper."""
        for slot in _SLOTS:
            pane_id = owners.get(slot)
            ref = self._pane_ref(pane_id) if pane_id else None
            self._set_option_if_changed(
                _OWNER_OPTIONS[slot],
                json.dumps(ref.to_json(), sort_keys=True, separators=(",", ":"))
                if ref is not None
                else None,
            )

    def _cleanup_parking(self, state: ToolState) -> None:
        if (
            state.parking_session is None
            or state.parking_session_id is None
        ):
            return
        session_id = self._output(
            "display-message",
            "-p",
            "-t",
            state.parking_session,
            "#{session_id}",
        )
        marker = self._output(
            "show-options",
            "-v",
            "-t",
            state.parking_session,
            _KEEPER_OPTION,
        )
        expected = json.dumps(
            {
                "version": _SCHEMA,
                "outer_session_id": self.outer_session_id,
                "slot": state.slot,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        if session_id == state.parking_session_id and marker == expected:
            self._run("kill-session", "-t", state.parking_session_id)

    def slot_for_owner(self, pane_id: str) -> str | None:
        current = self._pane_ref(pane_id)
        if current is None:
            return None
        for slot in ("primary", "secondary"):
            raw = self._show_option(_OWNER_OPTIONS[slot])
            if not raw:
                continue
            try:
                ref = _decode_ref(json.loads(raw))
            except ValueError:
                ref = None
            if (
                ref is not None
                and ref.pane_id == current.pane_id
                and ref.pane_pid == current.pane_pid
            ):
                return slot
        return None

    def visible_tool_panes(self) -> frozenset[str]:
        result: set[str] = set()
        outer_windows = self._outer_window_ids()
        for slot in _SLOTS:
            state = self.load(slot)
            if state is None:
                continue
            for ref in (state.shell, state.viewer):
                current = self._exact_ref(ref)
                if current is not None and current.window_id in outer_windows:
                    result.add(current.pane_id)
        return frozenset(result)

    def _parking_name(self, slot: str) -> str:
        suffix = self.outer_session_id[1:]
        return f"railmux-tool-{suffix}-{slot}"

    def _ensure_parking(
        self, state: ToolState,
    ) -> tuple[ToolState, PaneRef] | None:
        placeholder = self._exact_ref(state.placeholder)
        if (
            placeholder is not None
            and state.parking_session_id is not None
            and placeholder.session_id == state.parking_session_id
        ):
            return state, placeholder
        name = state.parking_session or self._parking_name(state.slot)
        marker = json.dumps(
            {
                "version": _SCHEMA,
                "outer_session_id": self.outer_session_id,
                "slot": state.slot,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        session_id = self._output(
            "display-message", "-p", "-t", name, "#{session_id}"
        )
        placeholder_id: str | None = None
        if session_id is None:
            placeholder_id = self._output(
                "new-session",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-s",
                name,
                "-x",
                "80",
                "-y",
                "24",
                _DUMMY_COMMAND,
            )
            if placeholder_id is None:
                return None
            session_id = self._output(
                "display-message", "-p", "-t", name, "#{session_id}"
            )
            if (
                session_id is None
                or not self._run(
                    "set-option", "-t", name, _KEEPER_OPTION, marker
                )
            ):
                self._run("kill-session", "-t", name)
                return None
        elif self._output(
            "show-options", "-v", "-t", name, _KEEPER_OPTION
        ) != marker:
            return None
        if not re.fullmatch(r"\$[0-9]+", session_id):
            return None
        if placeholder_id is None:
            placeholder_id = self._output(
                "split-window",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                name,
                _DUMMY_COMMAND,
            )
        if placeholder_id is None:
            return None
        placeholder = self._pane_ref(placeholder_id)
        if placeholder is None:
            return None
        state = ToolState(
            state.slot,
            state.outer_session_id,
            state.owner,
            state.shell,
            state.viewer,
            state.active,
            name,
            session_id,
            placeholder,
        )
        if not self._save(state):
            self._run("kill-pane", "-t", placeholder.pane_id)
            return None
        return state, placeholder

    def _split_below(
        self, owner: PaneRef, command: str, *, percent: int = 35,
    ) -> PaneRef | None:
        pane_id = self._output(
            "split-window",
            "-v",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-l",
            f"{percent}%",
            "-t",
            owner.pane_id,
            command,
        )
        return self._pane_ref(pane_id) if pane_id else None

    @staticmethod
    def _shell_command(cwd: Path) -> str:
        shell = os.environ.get("SHELL", "/bin/sh")
        return (
            f"cd -- {shlex.quote(str(cwd))} && "
            f"exec {shlex.quote(shell)} -l"
        )

    @staticmethod
    def _viewer_command(
        path: str, *, line: int | None = None, column: int | None = None,
    ) -> str:
        destination = str(Path(path).parent)
        vim = ["vim"]
        if line is not None:
            if column is not None:
                vim.append(f"+call cursor({line}, {column})")
            else:
                vim.append(f"+{line}")
        vim.extend(("--", path))
        shell = os.environ.get("SHELL", "/bin/sh")
        notice = shlex.quote(
            "Railmux: vim is unavailable; opened the file's directory."
        )
        return (
            "if command -v vim >/dev/null 2>&1; "
            f"then exec {shlex.join(vim)}; "
            f"else printf '%s\\n' {notice}; "
            f"cd -- {shlex.quote(destination)} && "
            f"exec {shlex.quote(shell)} -l; fi"
        )

    def _pane_current_command(self, pane_id: str) -> str | None:
        command = self._output(
            "display-message", "-p", "-t", pane_id, "#{pane_current_command}"
        )
        return Path(command).name if command else None

    def _window_is_zoomed(self, pane_id: str) -> bool:
        return (
            self._output(
                "display-message",
                "-p",
                "-t",
                pane_id,
                "#{window_zoomed_flag}",
            )
            == "1"
        )

    def _select_tool(self, pane_id: str, *, preserve_zoom: bool) -> bool:
        if not self._run("select-pane", "-t", pane_id):
            return False
        return (
            not preserve_zoom
            or self._window_is_zoomed(pane_id)
            or self._run("resize-pane", "-Z", "-t", pane_id)
        )

    def _send_vim_tab(
        self,
        viewer: PaneRef,
        path: str,
        *,
        line: int | None,
        column: int | None,
    ) -> bool:
        if self._pane_current_command(viewer.pane_id) not in {
            "vim", "vim.basic", "vim.tiny", "nvim", "view",
        }:
            return False
        # Vim single-quoted strings escape a literal quote by doubling it.
        escaped = path.replace("'", "''")
        command = f":execute 'tab drop ' . fnameescape('{escaped}')"
        if line is not None:
            command += f" | call cursor({line}, {column or 1})"
        return (
            self._run("send-keys", "-t", viewer.pane_id, "Escape")
            and self._run(
                "send-keys", "-l", "-t", viewer.pane_id, "--", command
            )
            and self._run("send-keys", "-t", viewer.pane_id, "Enter")
        )

    def _switch(self, state: ToolState, kind: str) -> ToolState | None:
        desired = self._exact_ref(
            state.shell if kind == "shell" else state.viewer
        )
        owner = self._exact_ref(state.owner)
        if desired is None or owner is None:
            return None
        outer_windows = self._outer_window_ids()
        visible = next(
            (
                current
                for current in (
                    self._exact_ref(state.shell),
                    self._exact_ref(state.viewer),
                )
                if current is not None and current.window_id in outer_windows
            ),
            None,
        )
        if desired.window_id in outer_windows:
            updated = state
        elif visible is not None:
            if not self._run(
                "swap-pane", "-d", "-s", desired.pane_id, "-t", visible.pane_id
            ):
                return None
            updated = state
        else:
            temporary = self._split_below(owner, _DUMMY_COMMAND)
            if temporary is None:
                return None
            if not self._run(
                "swap-pane",
                "-d",
                "-s",
                desired.pane_id,
                "-t",
                temporary.pane_id,
            ):
                self._run("kill-pane", "-t", temporary.pane_id)
                return None
            self._run("kill-pane", "-t", temporary.pane_id)
            updated = state
        updated = ToolState(
            updated.slot,
            updated.outer_session_id,
            owner,
            self._exact_ref(updated.shell),
            self._exact_ref(updated.viewer),
            kind,
            updated.parking_session,
            updated.parking_session_id,
            self._exact_ref(updated.placeholder),
        )
        if updated == state:
            return updated
        return updated if self._save(updated) else None

    def open_shell(self, slot: str, owner_pane_id: str, cwd: Path) -> ToolResult:
        if slot not in _SLOTS:
            return ToolResult(False, "Unknown agent slot", level="error")
        owner = self._pane_ref(owner_pane_id)
        if owner is None or owner.session_id != self.outer_session_id:
            return ToolResult(False, "Agent pane is no longer available", level="warning")
        preserve_zoom = self._window_is_zoomed(owner.pane_id)
        state = self.load(slot)
        shell = self._exact_ref(state.shell) if state else None
        reused = shell is not None
        if state is None:
            shell = self._split_below(owner, self._shell_command(cwd))
            if shell is None:
                return ToolResult(False, "Could not create terminal pane", level="error")
            if not self._mark_tool(shell, slot, "shell"):
                self._run("kill-pane", "-t", shell.pane_id)
                return ToolResult(False, "Could not register terminal pane", level="error")
            state = ToolState(
                slot,
                self.outer_session_id,
                owner,
                shell,
                None,
                "shell",
                None,
                None,
                None,
            )
            if not self._save(state):
                self._run("kill-pane", "-t", shell.pane_id)
                return ToolResult(False, "Could not register terminal pane", level="error")
        else:
            state = ToolState(
                state.slot,
                state.outer_session_id,
                owner,
                shell,
                state.viewer,
                state.active,
                state.parking_session,
                state.parking_session_id,
                state.placeholder,
            )
            if shell is None:
                parked = self._ensure_parking(state)
                if parked is None:
                    return ToolResult(
                        False,
                        "Could not create the tool parking session",
                        level="error",
                    )
                state, _placeholder = parked
                shell_id = self._output(
                    "split-window",
                    "-d",
                    "-P",
                    "-F",
                    "#{pane_id}",
                    "-t",
                    state.parking_session_id or "",
                    self._shell_command(cwd),
                )
                shell = self._pane_ref(shell_id) if shell_id else None
                if shell is None or not self._mark_tool(shell, slot, "shell"):
                    if shell is not None:
                        self._run("kill-pane", "-t", shell.pane_id)
                    return ToolResult(
                        False, "Could not recreate terminal pane", level="error"
                    )
                state = ToolState(
                    state.slot,
                    state.outer_session_id,
                    owner,
                    shell,
                    state.viewer,
                    "shell",
                    state.parking_session,
                    state.parking_session_id,
                    state.placeholder,
                )
            state = self._switch(state, "shell")
            if state is None:
                return ToolResult(False, "Could not restore terminal pane", level="error")
            shell = self._exact_ref(state.shell)
        if shell is None or not self._select_tool(
            shell.pane_id,
            preserve_zoom=preserve_zoom,
        ):
            return ToolResult(False, "Could not focus terminal pane", level="error")
        message = (
            "Terminal already open; current directory preserved"
            if reused
            else f"Terminal: {cwd.name or cwd}"
        )
        return ToolResult(True, message, shell.pane_id)

    def open_viewer(
        self,
        slot: str,
        owner_pane_id: str,
        path: str,
        *,
        line: int | None = None,
        column: int | None = None,
    ) -> ToolResult:
        owner = self._pane_ref(owner_pane_id)
        if slot not in _SLOTS or owner is None:
            return ToolResult(False, "Agent pane is no longer available", level="warning")
        preserve_zoom = self._window_is_zoomed(owner.pane_id)
        state = self.load(slot)
        if state is None:
            shell_result = self.open_shell(slot, owner_pane_id, Path(path).parent)
            if not shell_result.ok:
                return shell_result
            state = self.load(slot)
        assert state is not None
        viewer = self._exact_ref(state.viewer)
        if viewer is not None:
            if not self._send_vim_tab(
                viewer,
                path,
                line=line,
                column=column,
            ):
                return ToolResult(
                    False,
                    "The managed viewer is not accepting Vim tabs",
                    viewer.pane_id,
                    "warning",
                )
            switched = self._switch(
                ToolState(
                    state.slot,
                    state.outer_session_id,
                    owner,
                    state.shell,
                    viewer,
                    state.active,
                    state.parking_session,
                    state.parking_session_id,
                    state.placeholder,
                ),
                "viewer",
            )
            if switched is None:
                return ToolResult(False, "Could not restore Vim viewer", level="error")
            if not self._select_tool(
                viewer.pane_id,
                preserve_zoom=preserve_zoom,
            ):
                return ToolResult(False, "Could not focus Vim viewer", level="error")
            return ToolResult(
                True,
                "Opened remote file in the managed Vim",
                viewer.pane_id,
            )
        parked = self._ensure_parking(
            ToolState(
                state.slot,
                state.outer_session_id,
                owner,
                state.shell,
                None,
                state.active,
                state.parking_session,
                state.parking_session_id,
                state.placeholder,
            )
        )
        if parked is None:
            return ToolResult(False, "Could not create the tool parking session", level="error")
        state, _placeholder = parked
        viewer_id = self._output(
            "split-window",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            state.parking_session_id or "",
            self._viewer_command(path, line=line, column=column),
        )
        viewer = self._pane_ref(viewer_id) if viewer_id else None
        if viewer is None:
            return ToolResult(False, "Could not start remote Vim", level="error")
        if not self._mark_tool(viewer, slot, "viewer"):
            self._run("kill-pane", "-t", viewer.pane_id)
            return ToolResult(False, "Could not register remote Vim", level="error")
        state = ToolState(
            state.slot,
            state.outer_session_id,
            owner,
            state.shell,
            viewer,
            "viewer",
            state.parking_session,
            state.parking_session_id,
            state.placeholder,
        )
        state = self._switch(state, "viewer")
        if state is None:
            self._run("kill-pane", "-t", viewer.pane_id)
            return ToolResult(False, "Could not display remote Vim", level="error")
        if not self._select_tool(
            viewer.pane_id,
            preserve_zoom=preserve_zoom,
        ):
            return ToolResult(False, "Could not focus Vim viewer", level="error")
        return ToolResult(True, "Opened remote file inside Railmux", viewer.pane_id)

    def focus_viewer(self, slot: str, owner_pane_id: str) -> ToolResult:
        state = self.load(slot)
        owner = self._pane_ref(owner_pane_id)
        if state is None or owner is None or self._exact_ref(state.viewer) is None:
            return ToolResult(
                False,
                "No Vim viewer is open for this agent",
                level="warning",
            )
        preserve_zoom = self._window_is_zoomed(owner.pane_id)
        state = ToolState(
            state.slot,
            state.outer_session_id,
            owner,
            state.shell,
            state.viewer,
            state.active,
            state.parking_session,
            state.parking_session_id,
            state.placeholder,
        )
        state = self._switch(state, "viewer")
        viewer = self._exact_ref(state.viewer) if state else None
        if viewer is None or not self._select_tool(
            viewer.pane_id,
            preserve_zoom=preserve_zoom,
        ):
            return ToolResult(False, "Could not restore Vim viewer", level="error")
        return ToolResult(True, "Vim viewer", viewer.pane_id)

    def suspend(self, slot: str) -> bool:
        state = self.load(slot)
        if state is None:
            return True
        outer_windows = self._outer_window_ids()
        visible = next(
            (
                current
                for current in (
                    self._exact_ref(state.shell),
                    self._exact_ref(state.viewer),
                )
                if current is not None
                and current.window_id in outer_windows
            ),
            None,
        )
        if visible is None:
            return True
        parked = self._ensure_parking(state)
        if parked is None:
            return False
        state, placeholder = parked
        if not self._run(
            "swap-pane", "-d", "-s", visible.pane_id, "-t", placeholder.pane_id
        ):
            return False
        replacement_id = self._output(
            "split-window",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            state.parking_session_id or "",
            _DUMMY_COMMAND,
        )
        replacement = self._pane_ref(replacement_id) if replacement_id else None
        if replacement is None:
            # The process is safely parked even if a reusable placeholder
            # could not be created. A later restore can create one directly.
            replacement = None
        self._run("kill-pane", "-t", placeholder.pane_id)
        state = ToolState(
            state.slot,
            state.outer_session_id,
            state.owner,
            self._exact_ref(state.shell),
            self._exact_ref(state.viewer),
            state.active,
            state.parking_session,
            state.parking_session_id,
            replacement,
        )
        return self._save(state)

    def reconcile(self, owners: Mapping[str, str | None]) -> None:
        """Repair dead viewers and restore live surfaces below current slots."""
        self.sync_owners(owners)
        for slot in ("primary", "secondary"):
            state = self.load(slot)
            if state is None:
                continue
            owner_id = owners.get(slot)
            owner = self._pane_ref(owner_id) if owner_id else None
            if owner is None:
                self.suspend(slot)
                continue
            if state.owner != owner:
                if not self.suspend(slot):
                    continue
                state = self.load(slot)
                if state is None:
                    continue
            state = ToolState(
                state.slot,
                state.outer_session_id,
                owner,
                self._exact_ref(state.shell),
                self._exact_ref(state.viewer),
                state.active,
                state.parking_session,
                state.parking_session_id,
                self._exact_ref(state.placeholder),
            )
            if state.shell is None and state.viewer is None:
                self._set_option(_STATE_OPTIONS[slot], None)
                continue
            if state.active == "viewer" and state.viewer is None:
                state = ToolState(
                    state.slot,
                    state.outer_session_id,
                    owner,
                    state.shell,
                    None,
                    "shell",
                    state.parking_session,
                    state.parking_session_id,
                    state.placeholder,
                )
            elif state.active == "shell" and state.shell is None:
                state = ToolState(
                    state.slot,
                    state.outer_session_id,
                    owner,
                    None,
                    state.viewer,
                    "viewer",
                    state.parking_session,
                    state.parking_session_id,
                    state.placeholder,
                )
            if self._switch(state, state.active) is None:
                self._save(state)


def manager_for_session(session_id: str) -> ToolPaneManager | None:
    try:
        return ToolPaneManager(session_id)
    except (RuntimeError, ValueError, tmux_server.TmuxServerError):
        return None
