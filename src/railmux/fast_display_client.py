"""Interactive latest-state SSH client for the complete Railmux tmux window.

The remote helper attaches one real tmux client inside a private PTY and
coalesces its output before sending a compressed keyframe followed by changed
rows over ordinary SSH. All input except Ctrl-] is delivered to that tmux
client, so native tmux and Railmux bindings remain authoritative. Ctrl-] is
always consumed locally.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import select
import selectors
import shlex
import shutil
import subprocess
import sys
import termios
import time
import tty
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import BinaryIO, NoReturn, Optional, Sequence

from railmux import __version__, local_clipboard, local_open
from railmux.config import (
    ConfigError,
    SSH_HISTORY_MAX_LINES,
    SSH_HISTORY_MIN_LINES,
    load_config,
)
from railmux.fast_display_history import (
    HistoryAction,
    LocalHistoryView,
    PeriodicPrefetchGate,
    claim_batched_forwarded_wheel,
    input_may_change_routes,
)
from railmux.fast_display_input import (
    ClickTarget,
    LocalTextSelection,
    SelectionSegment,
    SgrMouseEvent,
    TermuxTouchKeyboard,
    TouchKeyboardAction,
    TerminalInputDecoder,
    is_termux_environment,
    page_key_direction,
    split_page_key_input,
)
from railmux.fast_display_protocol import (
    ClipboardCopy,
    ClaudeHistoryPolicyResult,
    DISPLAY_MAGIC,
    HistoryBatch,
    HistorySnapshot,
    PathKind,
    PathOpenResult,
    PathResult,
    PROTOCOL_VERSION,
    REMOTE_ATTACH_ACCEPTED,
    REMOTE_ATTACH_BUSY,
    REMOTE_HELLO_PREFIX,
    REMOTE_START,
    RemoteExit,
    ScreenUpdate,
    ServerMessageDecoder,
    TerminalMode,
    UpdateKind,
    encode_heartbeat,
    encode_claude_history_policy,
    encode_input,
    encode_keyframe_request,
    encode_path_request,
    encode_path_open_request,
    encode_resize,
)
from railmux.pane_surface import render_startup_surface
from railmux.ssh_compat import CompatibilityFacts, decide as decide_compatibility
from railmux.ssh_args import AppendSshArgument, ExtendSshArguments
from railmux.ssh_display_diagnostics import SshDisplayRecorder, SshDisplayStats

LOCAL_ESCAPE = b"\x1d"  # Ctrl-]
_SGR_STYLE_RE = re.compile(rb"\x1b\[[0-9;]*m")
_HISTORY_PREFETCH_INTERVAL = 3.0
_CLAUDE_HISTORY_SAVE_TIMEOUT = 5.0
_HISTORY_INFO_SECONDS = 2.0
_SELECTION_HIGHLIGHT_SECONDS = 2.0
_CLICK_FLASH_SECONDS = 0.18
_PATH_OPEN_TIMEOUT = 5.0
_TERMUX_TOUCH_HINT = "Tap the prompt again to open the keyboard"
_REMOTE_HELLO_TIMEOUT = 60.0
_REMOTE_HELLO_LIMIT = 16 * 1024
_REMOTE_ATTACH_TIMEOUT = 30.0
_FIRST_FRAME_TIMEOUT = 30.0
_REMOTE_ATTACH_RETRY_DELAY = 0.2
_HEARTBEAT_INTERVAL = 5.0
_DISPLAY_MAGIC_PREFIX = b"RMUXD"
_REMOTE_VENV = ".local/share/railmux/ssh-venv"
_MIN_TERMINAL_COLUMNS = 40
_MIN_TERMINAL_LINES = 12
_MAX_TERMINAL_COLUMNS = 1000
_MAX_TERMINAL_LINES = 500
_TERMINAL_SIZE_POLL_INTERVAL = 0.1
_RECONNECT_WINDOW = 60.0
_RECONNECT_ATTEMPT_TIMEOUT = 5.0
_RECONNECT_MAX_DELAY = 5.0
_KNOWN_REMOTE_EXITS = {
    int(RemoteExit.DETACHED): "detached; the Railmux session is still running",
    int(RemoteExit.SOFT_QUIT): "soft-quit; agent sessions were left running",
    int(RemoteExit.HARD_QUIT): "hard-quit; the managed Railmux session ended",
}


@dataclass(frozen=True)
class AppliedScreen:
    width: int
    height: int
    cursor_x: int
    cursor_y: int
    cursor_visible: bool
    terminal_modes: TerminalMode
    rows: tuple[bytes, ...]
    changed_rows: tuple[int, ...]
    clear: bool


class ScreenModel:
    """Apply sequenced updates and reject patches without a valid base."""

    def __init__(self) -> None:
        self.sequence: int | None = None
        self.width = 0
        self.height = 0
        self.rows: list[bytes] = []

    def apply(
        self,
        update: ScreenUpdate,
        expected_size: os.terminal_size,
    ) -> AppliedScreen | None:
        if (update.width, update.height) != (
            expected_size.columns,
            expected_size.lines,
        ):
            return None
        if update.kind is UpdateKind.KEYFRAME:
            rows = [b""] * update.height
            for index, row in update.rows:
                rows[index] = row
            self.rows = rows
            changed = tuple(range(update.height))
            clear = True
        else:
            expected_sequence = (
                None if self.sequence is None else (self.sequence + 1) & 0xFFFFFFFF
            )
            if (
                expected_sequence is None
                or update.sequence != expected_sequence
                or update.width != self.width
                or update.height != self.height
            ):
                return None
            for index, row in update.rows:
                self.rows[index] = row
            changed = tuple(index for index, _row in update.rows)
            clear = False
        self.sequence = update.sequence
        self.width = update.width
        self.height = update.height
        return AppliedScreen(
            width=update.width,
            height=update.height,
            cursor_x=update.cursor_x,
            cursor_y=update.cursor_y,
            cursor_visible=update.cursor_visible,
            terminal_modes=update.terminal_modes,
            rows=tuple(self.rows),
            changed_rows=changed,
            clear=clear,
        )


def full_repaint(screen: AppliedScreen) -> AppliedScreen:
    return AppliedScreen(
        width=screen.width,
        height=screen.height,
        cursor_x=screen.cursor_x,
        cursor_y=screen.cursor_y,
        cursor_visible=screen.cursor_visible,
        terminal_modes=screen.terminal_modes,
        rows=screen.rows,
        changed_rows=tuple(range(screen.height)),
        clear=True,
    )


def selection_changed_rows(
    before: tuple[SelectionSegment, ...],
    after: tuple[SelectionSegment, ...],
) -> set[int]:
    """Rows that must be restored when one local highlight changes."""
    return {row for row, _column, _text in (*before, *after)}


def focus_in_frame_for_screen(screen: AppliedScreen | None) -> bytes | None:
    """Reassert focus only while the remote application requested events."""
    if screen is None or not screen.terminal_modes & TerminalMode.FOCUS_EVENTS:
        return None
    return encode_input(b"\033[I")


def compact_status_row(screen: AppliedScreen) -> int | None:
    """Return the 1-based compact navigation row at either tmux bar edge."""
    prefixes = (
        b"[R][1][2] ",
        b"[Railmux][A1][A2] ",
        b"[Railmux][Agent 1][Agent 2] ",
    )
    for index in dict.fromkeys((0, screen.height - 1)):
        if not 0 <= index < len(screen.rows):
            continue
        plain = _SGR_STYLE_RE.sub(b"", screen.rows[index])
        if plain.startswith(prefixes):
            return index + 1
    return None


def termux_prompt_touch_action(
    touch_keyboard: TermuxTouchKeyboard,
    event: SgrMouseEvent,
    history: LocalHistoryView,
    screen: AppliedScreen | None,
    *,
    now: float | None = None,
) -> TouchKeyboardAction:
    """Classify one translated Termux tap against live screen route authority."""
    if screen is None:
        return TouchKeyboardAction()
    clicked_pane_id = history.pane_id_at_position(event.x - 1, event.y - 1)
    cursor_pane_id = history.pane_id_at_position(
        screen.cursor_x,
        screen.cursor_y,
    )
    return touch_keyboard.pointer_event(
        event,
        clicked_pane_id=clicked_pane_id,
        cursor_pane_id=cursor_pane_id,
        cursor_y=screen.cursor_y,
        pane_frozen=history.pane_is_frozen(clicked_pane_id),
        navigation_row=compact_status_row(screen),
        now=now,
    )


class ProbeError(RuntimeError):
    """A bounded, user-facing SSH display failure."""


class ReconnectCancelled(Exception):
    """A local Ctrl-]/Ctrl-C/EOF cancelled an in-progress reconnect."""

    def __init__(self, exit_code: int) -> None:
        super().__init__("automatic reconnect cancelled")
        self.exit_code = exit_code


@dataclass(frozen=True)
class RemoteHello:
    version: str
    protocol: int
    ready: bool
    tmux: bool = True
    config_status: str = "valid"
    tmux_configured: bool = False
    config_protocol: int = 0
    platform: str = "posix"


@dataclass(frozen=True)
class _PendingPathOpen:
    target: ClickTarget
    requested_at: float


class RemoteStartKind(Enum):
    HELLO = "hello"
    MISSING = "missing"
    FAILED = "failed"
    TIMEOUT = "timeout"


class RemoteLaunchMode(Enum):
    POSIX = "posix"
    DIRECT = "direct"


class RemoteAttachKind(Enum):
    ACCEPTED = "accepted"
    BUSY = "busy"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class RemoteStartup:
    kind: RemoteStartKind
    hello: RemoteHello | None = None
    returncode: int | None = None


def await_remote_attach_status(
    process: subprocess.Popen,
    timeout: float = _REMOTE_ATTACH_TIMEOUT,
    *,
    cancel_fd: int | None = None,
) -> RemoteAttachKind:
    """Read one post-start status without consuming the first display frame."""
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    received = bytearray()
    limit = max(len(REMOTE_ATTACH_ACCEPTED), len(REMOTE_ATTACH_BUSY)) + 2
    while len(received) < limit:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return RemoteAttachKind.TIMEOUT
        readers = [process.stdout.fileno()]
        if cancel_fd is not None:
            readers.append(cancel_fd)
        readable, _writable, _exceptional = select.select(readers, [], [], remaining)
        if not readable:
            return RemoteAttachKind.TIMEOUT
        if cancel_fd is not None and cancel_fd in readable:
            _consume_reconnect_input(cancel_fd)
            if process.stdout.fileno() not in readable:
                continue
        chunk = os.read(process.stdout.fileno(), 1)
        if not chunk:
            process.wait()
            return RemoteAttachKind.FAILED
        received.extend(chunk)
        if chunk != b"\n":
            continue
        line = bytes(received)
        if line == REMOTE_ATTACH_ACCEPTED:
            return RemoteAttachKind.ACCEPTED
        if line == REMOTE_ATTACH_BUSY:
            return RemoteAttachKind.BUSY
        return RemoteAttachKind.FAILED
    return RemoteAttachKind.FAILED


def parse_remote_hello(line: bytes) -> RemoteHello:
    """Parse one bounded, untrusted compatibility line from the remote."""
    if not line.startswith(REMOTE_HELLO_PREFIX):
        raise ValueError("not a Railmux remote hello")
    payload = line[len(REMOTE_HELLO_PREFIX) :].rstrip(b"\r\n")
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid Railmux remote hello") from exc
    if not isinstance(value, dict):
        raise ValueError("invalid Railmux remote hello")
    version = value.get("version")
    protocol = value.get("protocol")
    ready = value.get("ready")
    tmux = value.get("tmux")
    config_status = value.get("config_status", "valid")
    tmux_configured = value.get("tmux_configured", False)
    config_protocol = value.get("config_protocol", 0)
    platform = value.get("platform", "posix")
    if (
        not isinstance(version, str)
        or not version
        or len(version) > 128
        or not isinstance(protocol, int)
        or isinstance(protocol, bool)
        or not 1 <= protocol <= 65535
        or not isinstance(ready, bool)
        or not isinstance(tmux, bool)
        or config_status not in {"valid", "invalid"}
        or not isinstance(tmux_configured, bool)
        or not isinstance(config_protocol, int)
        or isinstance(config_protocol, bool)
        or not 0 <= config_protocol <= 65535
        or platform not in {"posix", "windows-msys2"}
    ):
        raise ValueError("invalid Railmux remote hello")
    try:
        version.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("invalid Railmux remote version") from exc
    return RemoteHello(
        version,
        protocol,
        ready,
        tmux,
        config_status,
        tmux_configured,
        config_protocol,
        platform,
    )


def await_remote_startup(
    process: subprocess.Popen,
    timeout: float = _REMOTE_HELLO_TIMEOUT,
    *,
    cancel_fd: int | None = None,
) -> RemoteStartup:
    """Wait before raw mode until the remote proves its compatibility state."""
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    received = bytearray()
    line_start = 0
    while len(received) < _REMOTE_HELLO_LIMIT:
        magic_start = received.find(_DISPLAY_MAGIC_PREFIX)
        if magic_start >= 0:
            magic_end = magic_start + len(DISPLAY_MAGIC)
            legacy_end = received.find(b"\0", magic_start)
            if len(received) >= magic_end or legacy_end >= 0:
                return RemoteStartup(RemoteStartKind.FAILED)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return RemoteStartup(RemoteStartKind.TIMEOUT)
        readers = [process.stdout.fileno()]
        if cancel_fd is not None:
            readers.append(cancel_fd)
        readable, _writable, _exceptional = select.select(readers, [], [], remaining)
        if not readable:
            return RemoteStartup(RemoteStartKind.TIMEOUT)
        if cancel_fd is not None and cancel_fd in readable:
            _consume_reconnect_input(cancel_fd)
            if process.stdout.fileno() not in readable:
                continue
        chunk = os.read(process.stdout.fileno(), 1)
        if not chunk:
            returncode = process.wait()
            kind = (
                RemoteStartKind.MISSING if returncode == 127 else RemoteStartKind.FAILED
            )
            return RemoteStartup(kind, returncode=returncode)
        received.extend(chunk)
        if chunk != b"\n":
            continue
        line = bytes(received[line_start:])
        line_start = len(received)
        marker = line.find(REMOTE_HELLO_PREFIX)
        if marker < 0:
            continue
        try:
            hello = parse_remote_hello(line[marker:])
        except ValueError:
            return RemoteStartup(RemoteStartKind.FAILED)
        return RemoteStartup(RemoteStartKind.HELLO, hello=hello)
    return RemoteStartup(RemoteStartKind.FAILED)


def _consume_reconnect_input(fd: int) -> None:
    """Discard unavailable-connection input, stopping on local escape/Ctrl-C."""
    data = os.read(fd, 4096)
    if b"\x03" in data:
        raise ReconnectCancelled(130)
    if not data or LOCAL_ESCAPE in data:
        raise ReconnectCancelled(0)


def claude_history_save_timed_out(
    started_at: float | None,
    now: float,
) -> bool:
    return started_at is not None and now - started_at >= _CLAUDE_HISTORY_SAVE_TIMEOUT


def first_frame_timed_out(deadline: float | None, now: float) -> bool:
    """Bound a newly attached transport without timing established frames."""
    return deadline is not None and now >= deadline


@dataclass(frozen=True)
class ClaudeHistoryPolicyAction:
    """Pure result of matching a helper acknowledgement to a pending choice."""

    update_runtime: bool
    runtime_choice: str | None
    prefetch: bool
    forwarded_input: bytes
    status_text: str | None


def apply_claude_history_policy_result(
    pending: tuple[str, bool, bytes] | None,
    result: ClaudeHistoryPolicyResult,
) -> ClaudeHistoryPolicyAction | None:
    """Resolve a four-way Claude history choice without select-loop coupling."""
    if (
        pending is None
        or pending[0] != result.policy
        or pending[1] != result.persistent
    ):
        return None
    if not result.applied:
        return ClaudeHistoryPolicyAction(
            update_runtime=False,
            runtime_choice=None,
            prefetch=False,
            forwarded_input=b"",
            status_text=("Could not save Claude history choice; setting remains Ask"),
        )
    runtime_choice = None if result.persistent else result.policy
    if result.policy == "local":
        return ClaudeHistoryPolicyAction(
            update_runtime=True,
            runtime_choice=runtime_choice,
            prefetch=True,
            forwarded_input=b"",
            status_text=("Smooth local Claude history enabled; scroll again"),
        )
    return ClaudeHistoryPolicyAction(
        update_runtime=True,
        runtime_choice=runtime_choice,
        prefetch=False,
        forwarded_input=pending[2],
        status_text=None,
    )


def claude_history_reconnect_frame(runtime_choice: str | None) -> bytes:
    """Resend only a non-persistent policy after transport replacement."""
    if runtime_choice is None:
        return b""
    return encode_claude_history_policy(runtime_choice, persistent=False)


def screen_input_may_change_routes(
    data: bytes,
    history: LocalHistoryView,
    screen: AppliedScreen | None,
) -> bool:
    """Fail closed when keyboard input originates outside an agent route."""
    cursor_in_agent = (
        screen is not None
        and history.pane_id_at_position(screen.cursor_x, screen.cursor_y) is not None
    )
    return input_may_change_routes(
        data,
        routes_visible=bool(history.visible_routes),
        cursor_in_agent=cursor_in_agent,
    )


def split_local_escape(data: bytes) -> tuple[bytes, bool]:
    """Return bytes before Ctrl-] and whether an emergency exit was found."""
    escape_at = data.find(LOCAL_ESCAPE)
    if escape_at < 0:
        return data, False
    return data[:escape_at], True


def _terminal_size_is_usable(size: os.terminal_size) -> bool:
    return size.columns >= _MIN_TERMINAL_COLUMNS and size.lines >= _MIN_TERMINAL_LINES


def _terminal_size_exceeds_limits(size: os.terminal_size) -> bool:
    return size.columns > _MAX_TERMINAL_COLUMNS or size.lines > _MAX_TERMINAL_LINES


def wait_for_usable_terminal_size(fd: int) -> os.terminal_size:
    """Wait in cooked mode for a soft-keyboard-sized terminal to recover."""
    reported: os.terminal_size | None = None
    while True:
        size = os.get_terminal_size(fd)
        if _terminal_size_exceeds_limits(size):
            raise ProbeError(
                "local terminal reports "
                f"{size.columns}x{size.lines}; SSH display limits are "
                f"{_MAX_TERMINAL_COLUMNS}x{_MAX_TERMINAL_LINES}"
            )
        if _terminal_size_is_usable(size):
            if reported is not None:
                print(
                    "railmux ssh: local terminal is now "
                    f"{size.columns}x{size.lines}; continuing",
                    file=sys.stderr,
                )
            return size
        if size.columns < _MIN_TERMINAL_COLUMNS:
            raise ProbeError(
                "local terminal reports "
                f"{size.columns}x{size.lines}; SSH display requires at least "
                f"{_MIN_TERMINAL_COLUMNS}x{_MIN_TERMINAL_LINES}"
            )
        if size != reported:
            print(
                "railmux ssh: local terminal reports "
                f"{size.columns}x{size.lines}; waiting for at least "
                f"{_MIN_TERMINAL_COLUMNS}x{_MIN_TERMINAL_LINES} "
                "(hide the soft keyboard; Ctrl-C cancels)",
                file=sys.stderr,
            )
            reported = size
        time.sleep(_TERMINAL_SIZE_POLL_INTERVAL)


def _is_soft_keyboard_projection(
    physical_size: os.terminal_size,
    logical_size: os.terminal_size,
) -> bool:
    """Recognize the same-width, short-height resize used by soft keyboards."""
    return (
        physical_size.columns == logical_size.columns
        and 0 < physical_size.lines < _MIN_TERMINAL_LINES
    )


class RawTerminal:
    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.saved: Optional[list[object]] = None

    def __enter__(self) -> "RawTerminal":
        self.saved = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            self.saved = None


class TerminalSurface:
    """Paint a server-rendered screen and unconditionally restore the TTY."""

    def __init__(
        self,
        stream: BinaryIO,
        *,
        mouse: bool = True,
        mouse_hover: bool = True,
    ) -> None:
        self.stream = stream
        self.mouse = mouse
        self.mouse_tracking_mode = 1003 if mouse_hover else 1002
        self.active = False
        self.interaction_active = False
        self.mouse_active = False
        self.mouse_suspended = False
        self.cursor_hidden = False
        # Track the physical emulator cursor separately from the remote model.
        # Re-sending DECSET/DECRST 25 on every 20 fps patch restarts the cursor
        # animation in Windows Terminal and presents as a rapid flash.
        self._painted_cursor_visible: bool | None = None
        self.terminal_modes = TerminalMode.NONE
        self.physical_size: os.terminal_size | None = None
        self._last_screen: AppliedScreen | None = None
        self._last_overlays: tuple[
            tuple[HistorySnapshot, tuple[bytes, ...]], ...
        ] = ()
        # A disconnected frame is never input authority, but retaining its
        # status row lets local reconnect feedback use Railmux's normal
        # status-right placement instead of replacing status-left.
        self._reconnect_status_screen: AppliedScreen | None = None
        self._awaiting_reconnect_frame = False
        self._startup_detail: str | None = None
        self._local_status_text: str | None = None
        self._local_status_level = "info"
        self._local_status_bounds: tuple[int, int, int] | None = None
        self._local_status_interruptible = False
        self._local_status_expires_at: float | None = None

    def _mouse_mode(self, enabled: bool) -> bytes:
        suffix = b"h" if enabled else b"l"
        return (
            b"\033[?"
            + str(self.mouse_tracking_mode).encode("ascii")
            + suffix
            + b"\033[?1006"
            + suffix
        )

    def set_physical_size(self, size: os.terminal_size) -> None:
        """Set the local viewport without changing the remote screen geometry."""
        self.physical_size = size

    def _projection(self, screen_height: int) -> tuple[int, int]:
        visible_height = screen_height
        if self.physical_size is not None:
            visible_height = min(visible_height, self.physical_size.lines)
        visible_height = max(0, visible_height)
        return screen_height - visible_height, visible_height

    def translate_mouse_event(
        self,
        event: SgrMouseEvent,
        *,
        logical_height: int,
    ) -> SgrMouseEvent:
        """Map an SGR report from the bottom-anchored viewport to tmux."""
        top, _visible_height = self._projection(logical_height)
        return event.translated_y(top)

    def start(self, *, interactive: bool = True) -> None:
        controls: list[bytes] = []
        if not self.active:
            controls.append(b"\033[?1049h\033[2J\033[H")
            self.active = True
        if interactive and not self.cursor_hidden:
            controls.append(b"\033[?25l")
            self.cursor_hidden = True
            self._painted_cursor_visible = False
        if (
            interactive
            and self.mouse
            and not self.mouse_active
            and not self.mouse_suspended
        ):
            # Any-event tracking adds desktop URL/path hover. Termux supports
            # button-event mode but does not implement DECSET 1003, so its
            # touch client uses DECSET 1002. SGR mode preserves coordinates
            # beyond the legacy X10 limit on both routes.
            controls.append(self._mouse_mode(True))
            self.mouse_active = True
        if controls:
            self.stream.write(b"".join(controls))
            self.stream.flush()

    def suspend_mouse(self) -> None:
        """Yield touch input to a local terminal until explicitly resumed."""
        if not self.mouse or self.mouse_suspended:
            return
        self.mouse_suspended = True
        if self.mouse_active:
            self.stream.write(self._mouse_mode(False))
            self.stream.flush()
            self.mouse_active = False

    def resume_mouse(self, *, reassert: bool = False) -> None:
        """Restore local mouse reports after a bounded touch-input handoff."""
        if not self.mouse:
            return
        self.mouse_suspended = False
        if not self.active or self.interaction_active:
            return
        if reassert and self.mouse_active:
            # Termux can retain its native touch/selection owner across a soft
            # keyboard close even though DECSET 1002 remains logically active.
            # Toggle tracking and SGR so the emulator returns gestures to
            # Railmux.
            self.stream.write(self._mouse_mode(False) + self._mouse_mode(True))
            self.stream.flush()
            return
        if self.mouse_active:
            return
        self.stream.write(self._mouse_mode(True))
        self.stream.flush()
        self.mouse_active = True

    def show_startup(
        self,
        size: os.terminal_size,
        detail: str = "Reconnecting sessions and panes…",
    ) -> None:
        """Paint startup feedback only on the recoverable alternate screen."""
        self.set_physical_size(size)
        self.clear_local_status()
        if (
            self.active
            and not self.interaction_active
            and detail == self._startup_detail
        ):
            return
        self.start(interactive=False)
        self.stream.write(
            render_startup_surface(
                size.columns,
                size.lines,
                detail,
            ).encode("utf-8")
        )
        self.stream.flush()
        self.interaction_active = False
        self._startup_detail = detail

    def begin_interaction(self) -> None:
        """Reveal cooked-mode setup prompts without leaving startup cleanup.

        Keeping prompts on the same alternate screen avoids a terminal race
        where restoring the primary screen can leave the startup curtain
        visible over ``input()``.  The complete setup transcript remains
        visible until attach succeeds, then ``show_startup`` replaces it; an
        error or cancellation still restores the untouched primary screen.
        """
        self.start(interactive=False)
        if self.interaction_active:
            return
        controls: list[bytes] = [b"\033[0m\033[?7h\033[?25h"]
        if self.terminal_modes & TerminalMode.BRACKETED_PASTE:
            controls.append(b"\033[?2004l")
        if self.terminal_modes & TerminalMode.FOCUS_EVENTS:
            controls.append(b"\033[?1004l")
        if self.mouse_active:
            controls.append(self._mouse_mode(False))
        controls.append(b"\033[2J\033[H")
        self.stream.write(b"".join(controls))
        self.stream.flush()
        self.terminal_modes = TerminalMode.NONE
        self.mouse_active = False
        self.mouse_suspended = False
        self.cursor_hidden = False
        self._painted_cursor_visible = True
        self.interaction_active = True

    def show_local_status(
        self,
        message: str,
        *,
        level: str = "info",
        interruptible: bool = False,
        expires_at: float | None = None,
    ) -> None:
        """Show local feedback without erasing Railmux's status-left brand."""
        self.start()
        safe = "".join(
            character if character.isprintable() and character != "\x1b" else " "
            for character in message
        )
        self._local_status_text = safe
        self._local_status_level = level
        self._local_status_interruptible = interruptible
        self._local_status_expires_at = expires_at
        restore_cursor = self._last_screen is not None
        rendered = [self._render_local_status(hide_cursor=not restore_cursor)]
        if self._last_screen is not None:
            projection_top, visible_height = self._projection(
                self._last_screen.height
            )
            self._append_cursor(
                rendered,
                self._last_screen,
                self._last_overlays,
                projection_top=projection_top,
                visible_height=visible_height,
            )
        self.stream.write(b"".join(rendered))
        self.stream.flush()

    def _render_local_status(self, *, hide_cursor: bool = True) -> bytes:
        """Render and locate the current local status-right replacement."""
        if self._local_status_text is None:
            self._local_status_bounds = None
            return b""
        safe = self._local_status_text
        level = self._local_status_level
        height = 1 if self.physical_size is None else self.physical_size.lines
        screen = self._last_screen or self._reconnect_status_screen
        if screen is None:
            if self.physical_size is not None:
                safe = safe[: self.physical_size.columns]
            self._local_status_bounds = (max(1, height), 1, max(1, len(safe)))
            rendered = (
                f"\033[?7l\033[0m\033[{max(1, height)};1H\033[2K{safe}"
                "\033[0m\033[?7h"
            ).encode("utf-8")
            if hide_cursor:
                rendered += b"\033[?25l"
                self._painted_cursor_visible = False
            return rendered
        width = screen.width
        if self.physical_size is not None:
            width = min(width, self.physical_size.columns)
        # The left half belongs to Railmux's persistent brand/mode/layout
        # controls. Local-only SSH feedback is a transient status-right
        # replacement and must never clear those controls.
        reserved = width // 2
        available = max(0, width - reserved)
        safe = safe[:available]
        column = max(reserved + 1, width - len(safe) + 1)
        self._local_status_bounds = (max(1, height), column, width)
        background = self._row_background_sgr(screen.rows[-1])
        foreground = {
            "success": b"\033[1;38;5;17m",
            "warning": b"\033[1;38;5;220m",
            "error": b"\033[1;38;5;196m",
        }.get(level, b"\033[38;5;231m")
        rendered = b"".join(
            [
                b"\033[?7l\033[0m",
                f"\033[{max(1, height)};{reserved + 1}H".encode(),
                background,
                foreground,
                b"\033[K",
                f"\033[{max(1, height)};{column}H".encode(),
                safe.encode("utf-8"),
                b"\033[0m",
            ]
        )
        if hide_cursor:
            rendered += b"\033[?25l"
            self._painted_cursor_visible = False
        return rendered

    def clear_local_status(self) -> None:
        """Forget local feedback before restoring the authoritative screen."""
        self._local_status_text = None
        self._local_status_bounds = None
        self._local_status_interruptible = False
        self._local_status_expires_at = None

    def expire_local_status(self, now: float) -> bool:
        """Clear one locally timed status and report whether repaint is due."""
        deadline = self._local_status_expires_at
        if deadline is None or now < deadline:
            return False
        self.clear_local_status()
        return True

    def dismiss_interruptible_local_status(self) -> bool:
        """Let the first user action reveal Railmux's authoritative status."""
        if not self._local_status_interruptible:
            return False
        self.clear_local_status()
        return True

    def local_status_at(self, event: SgrMouseEvent) -> str | None:
        """Return the exact local status source rendered under a mouse event."""
        bounds = self._local_status_bounds
        if self._local_status_text is None or bounds is None:
            return None
        row, first_column, last_column = bounds
        if event.y != row or not first_column <= event.x <= last_column:
            return None
        return self._local_status_text

    def copy_local_status_at(self, event: SgrMouseEvent) -> bool:
        """Copy the visible local status and replace it with acknowledgement."""
        text = self.local_status_at(event)
        if text is None:
            return False
        self.copy_to_clipboard(text.encode("utf-8"))
        self.show_local_status("Copied status message.", level="success")
        return True

    @staticmethod
    def _row_background_sgr(row: bytes) -> bytes:
        """Return the first explicit row background as a standalone SGR."""
        for match in _SGR_STYLE_RE.finditer(row):
            raw = match.group()[2:-1]
            try:
                codes = [int(value) if value else 0 for value in raw.split(b";")]
            except ValueError:
                continue
            for index, code in enumerate(codes):
                if 40 <= code <= 47 or 100 <= code <= 107:
                    return f"\033[{code}m".encode()
                if code != 48 or index + 1 >= len(codes):
                    continue
                mode = codes[index + 1]
                count = 3 if mode == 5 else 5 if mode == 2 else 0
                if count and index + count <= len(codes):
                    values = ";".join(
                        str(value) for value in codes[index : index + count]
                    )
                    return f"\033[{values}m".encode()
        return b"\033[49m"

    def copy_to_clipboard(self, data: bytes) -> None:
        """Copy one validated payload locally, with OSC 52 as fallback."""
        if local_clipboard.copy(data):
            return
        self.start()
        encoded = base64.b64encode(data)
        self.stream.write(b"\033]52;c;" + encoded + b"\007")
        self.stream.flush()

    def _claude_history_prompt_geometry(self) -> tuple[int, int, int]:
        size = self.physical_size or os.terminal_size((80, 24))
        width = min(52, max(38, size.columns - 2))
        left = max(1, (size.columns - width) // 2 + 1)
        top = max(1, (size.lines - 7) // 2 + 1)
        return left, top, width

    def show_claude_history_prompt(self) -> None:
        """Draw a local-only choice dialog over the mirrored screen."""
        self.start()
        left, top, width = self._claude_history_prompt_geometry()
        inner = width - 2

        border = "\033[38;5;70m"
        yellow = "\033[1;38;5;220m"
        normal = "\033[0m"

        def middle(shortcut: str, text: str) -> str:
            plain = f" {shortcut} {text}"[:inner]
            padding = " " * (inner - len(plain))
            return (
                f"{border}│{normal} {yellow}{shortcut}{normal} "
                f"{text[: inner - len(shortcut) - 2]}{padding}{border}│"
                f"{normal}"
            )

        title = " Claude Code history "
        rows = (
            f"{border}┌\033[1m{title}\033[22m{'─' * (inner - len(title))}┐{normal}",
            middle("[1]", "Always use smooth local history"),
            middle("[2]", "Use smooth local history this time"),
            middle("[3]", "Always use Claude native history"),
            middle("[4]", "Use Claude native history this time"),
            middle("[Esc]", "Decide later"),
            f"{border}└{'─' * inner}┘{normal}",
        )
        rendered = [b"\033[0m"]
        for offset, row in enumerate(rows):
            rendered.append(
                f"\033[{top + offset};{left}H{row}\033[?25l".encode("utf-8")
            )
        self.stream.write(b"".join(rendered))
        self.stream.flush()
        self._painted_cursor_visible = False

    def claude_history_prompt_choice(
        self,
        event: SgrMouseEvent,
    ) -> tuple[str, bool] | None:
        """Resolve a press inside the local Claude-history dialog."""
        left, top, width = self._claude_history_prompt_geometry()
        if not event.pressed or not left <= event.x < left + width:
            return None
        if event.y == top + 1:
            return "local", True
        if event.y == top + 2:
            return "local", False
        if event.y == top + 3:
            return "native", True
        if event.y == top + 4:
            return "native", False
        return None

    def _path_open_prompt_geometry(self) -> tuple[int, int, int]:
        size = self.physical_size or os.terminal_size((80, 24))
        width = min(54, max(40, size.columns - 2))
        left = max(1, (size.columns - width) // 2 + 1)
        top = max(1, (size.lines - 7) // 2 + 1)
        return left, top, width

    def show_path_open_prompt(self) -> None:
        """Draw the local destination choice for one clicked remote path."""
        self.start()
        left, top, width = self._path_open_prompt_geometry()
        inner = width - 2
        border = "\033[38;5;70m"
        yellow = "\033[1;38;5;220m"
        normal = "\033[0m"

        def middle(shortcut: str, text: str) -> str:
            content_width = max(0, inner - len(shortcut) - 2)
            shown = text[:content_width]
            plain_width = len(shortcut) + len(shown) + 2
            return (
                f"{border}│{normal} {yellow}{shortcut}{normal} {shown}"
                f"{' ' * max(0, inner - plain_width)}{border}│{normal}"
            )

        title = " Open remote path "
        rows = (
            f"{border}┌\033[1m{title}\033[22m{'─' * (inner - len(title))}┐{normal}",
            middle("[1]", "Always use Railmux managed Vim"),
            middle("[2]", "Use Railmux managed Vim this time"),
            middle("[3]", "Always open in a separate terminal"),
            middle("[4]", "Open in a separate terminal this time"),
            middle("[Esc]", "Cancel"),
            f"{border}└{'─' * inner}┘{normal}",
        )
        rendered = [b"\033[0m"]
        for offset, row in enumerate(rows):
            rendered.append(
                f"\033[{top + offset};{left}H{row}\033[?25l".encode("utf-8")
            )
        self.stream.write(b"".join(rendered))
        self.stream.flush()
        self._painted_cursor_visible = False

    def path_open_prompt_choice(
        self,
        event: SgrMouseEvent,
    ) -> tuple[str, bool] | None:
        left, top, width = self._path_open_prompt_geometry()
        if not event.pressed or not left <= event.x < left + width:
            return None
        if event.y == top + 1:
            return "internal", True
        if event.y == top + 2:
            return "internal", False
        if event.y == top + 3:
            return "external", True
        if event.y == top + 4:
            return "external", False
        return None

    def _reconcile_terminal_modes(
        self,
        requested: TerminalMode,
    ) -> TerminalMode:
        """Mirror only input-affecting modes explicitly carried by protocol v15."""
        disabled = self.terminal_modes & ~requested
        enabled = requested & ~self.terminal_modes
        controls: list[bytes] = []
        for mode, disable, enable in (
            (TerminalMode.BRACKETED_PASTE, b"\033[?2004l", b"\033[?2004h"),
            (TerminalMode.FOCUS_EVENTS, b"\033[?1004l", b"\033[?1004h"),
        ):
            if disabled & mode:
                controls.append(disable)
            if enabled & mode:
                controls.append(enable)
        if controls:
            self.stream.write(b"".join(controls))
            self.stream.flush()
        self.terminal_modes = requested
        return enabled

    def begin_reconnect(self) -> None:
        """Discard stale presentation state before replacing one helper."""
        if not self.active:
            return
        self._reconcile_terminal_modes(TerminalMode.NONE)
        self.clear_local_status()
        self._reconnect_status_screen = self._last_screen
        self._last_screen = None
        self._last_overlays = ()
        self._awaiting_reconnect_frame = True
        # A status message can end in the terminal's bottom-right cell.  Leave
        # the disconnected surface in a known wrap/cursor state so neither a
        # retry message nor the next keyframe can consume a pending wrap and
        # scroll the alternate screen.
        self.stream.write(b"\033[0m\033[?7h\033[?25l")
        self.stream.flush()
        self._painted_cursor_visible = False

    @staticmethod
    def _cursor_is_covered(
        screen: AppliedScreen,
        overlays: tuple[tuple[HistorySnapshot, tuple[bytes, ...]], ...],
    ) -> bool:
        return any(
            snapshot.x <= screen.cursor_x < snapshot.x + snapshot.width
            and snapshot.y <= screen.cursor_y < snapshot.y + snapshot.height
            for snapshot, _lines in overlays
        )

    @staticmethod
    def _append_overlay_rows(
        rendered: list[bytes],
        overlays: tuple[tuple[HistorySnapshot, tuple[bytes, ...]], ...],
        *,
        projection_top: int = 0,
        visible_height: int | None = None,
        changed_rows: frozenset[int] | None = None,
    ) -> None:
        for snapshot, lines in overlays:
            for index in range(snapshot.height):
                row = snapshot.y + index
                if (
                    visible_height is not None
                    and not projection_top <= row < projection_top + visible_height
                ):
                    continue
                if changed_rows is not None and row not in changed_rows:
                    continue
                line = lines[index] if index < len(lines) else b""
                rendered.extend(
                    (
                        (f"\033[{row - projection_top + 1};{snapshot.x + 1}H").encode(),
                        f"\033[{snapshot.width}X".encode(),
                        line,
                    )
                )

    @staticmethod
    def _append_selection_segments(
        rendered: list[bytes],
        selection: tuple[SelectionSegment, ...],
        *,
        projection_top: int = 0,
        visible_height: int | None = None,
    ) -> None:
        for row, column, text in selection:
            if (
                visible_height is not None
                and not projection_top <= row < projection_top + visible_height
            ):
                continue
            rendered.extend(
                (
                    f"\033[{row - projection_top + 1};{column + 1}H".encode(),
                    b"\033[0;7m",
                    text,
                    b"\033[0m",
                )
            )

    def _append_cursor(
        self,
        rendered: list[bytes],
        screen: AppliedScreen,
        overlays: tuple[tuple[HistorySnapshot, tuple[bytes, ...]], ...],
        *,
        projection_top: int = 0,
        visible_height: int | None = None,
        force_visibility: bool = False,
    ) -> None:
        cursor_in_projection = (
            visible_height is None
            or projection_top <= screen.cursor_y < projection_top + visible_height
        )
        visible = bool(
            screen.cursor_visible
            and cursor_in_projection
            and not self._cursor_is_covered(screen, overlays)
        )
        rendered.extend((
            b"\033[0m\033[?7h",
            (
                f"\033[{screen.cursor_y - projection_top + 1};"
                f"{screen.cursor_x + 1}H"
            ).encode()
            if cursor_in_projection
            else b"\033[1;1H",
        ))
        if force_visibility or self._painted_cursor_visible is not visible:
            rendered.append(b"\033[?25h" if visible else b"\033[?25l")
            self._painted_cursor_visible = visible

    def paint(
        self,
        screen: AppliedScreen,
        overlays: tuple[tuple[HistorySnapshot, tuple[bytes, ...]], ...] = (),
        selection: tuple[SelectionSegment, ...] = (),
    ) -> bool:
        """Paint a screen and report whether focus reporting was just enabled."""
        if self._awaiting_reconnect_frame:
            # "Waiting for a fresh screen" has served its purpose.  It must
            # not be redrawn after the authoritative keyframe or hide the
            # agent cursor that keyframe restores.
            self.clear_local_status()
            self._reconnect_status_screen = None
            self._awaiting_reconnect_frame = False
        self.start()
        self._last_screen = screen
        self._last_overlays = overlays
        enabled_modes = self._reconcile_terminal_modes(screen.terminal_modes)
        projection_top, visible_height = self._projection(screen.height)
        rendered = [b"\033[?7l"]
        if screen.clear:
            rendered.append(b"\033[0m\033[2J")
        for row_index in screen.changed_rows:
            if not (projection_top <= row_index < projection_top + visible_height):
                continue
            rendered.extend(
                (
                    f"\033[{row_index - projection_top + 1};1H".encode(),
                    b"\033[2K",
                    screen.rows[row_index],
                )
            )
        self._append_overlay_rows(
            rendered,
            overlays,
            projection_top=projection_top,
            visible_height=visible_height,
            changed_rows=frozenset(screen.changed_rows),
        )
        self._append_selection_segments(
            rendered,
            selection,
            projection_top=projection_top,
            visible_height=visible_height,
        )
        if self._local_status_text is not None:
            rendered.append(self._render_local_status(hide_cursor=False))
        # Status and selection painting move and may temporarily hide the
        # terminal cursor.  Restoring the authoritative cursor must therefore
        # be the final operation in every complete paint.
        self._append_cursor(
            rendered,
            screen,
            overlays,
            projection_top=projection_top,
            visible_height=visible_height,
        )
        self.stream.write(b"".join(rendered))
        self.stream.flush()
        return bool(enabled_modes & TerminalMode.FOCUS_EVENTS)

    def paint_overlays(
        self,
        screen: AppliedScreen,
        overlays: tuple[tuple[HistorySnapshot, tuple[bytes, ...]], ...],
        selection: tuple[SelectionSegment, ...] = (),
    ) -> None:
        self.start()
        self._last_screen = screen
        self._last_overlays = overlays
        projection_top, visible_height = self._projection(screen.height)
        rendered: list[bytes] = [b"\033[?7l"]
        self._append_overlay_rows(
            rendered,
            overlays,
            projection_top=projection_top,
            visible_height=visible_height,
        )
        self._append_selection_segments(
            rendered,
            selection,
            projection_top=projection_top,
            visible_height=visible_height,
        )
        if self._local_status_text is not None:
            rendered.append(self._render_local_status(hide_cursor=False))
        self._append_cursor(
            rendered,
            screen,
            overlays,
            projection_top=projection_top,
            visible_height=visible_height,
        )
        self.stream.write(b"".join(rendered))
        self.stream.flush()

    def paint_changed_rows(
        self,
        screen: AppliedScreen,
        overlays: tuple[tuple[HistorySnapshot, tuple[bytes, ...]], ...],
        selection: tuple[SelectionSegment, ...],
        changed_rows: set[int] | frozenset[int],
    ) -> None:
        """Restore and repaint only rows touched by a local highlight.

        Hover, drag selection, and click flash are local overlays. Clearing the
        whole alternate screen for them is both unnecessary and visibly flashes
        in Windows Terminal. Restore each affected row from the authoritative
        live frame, then layer frozen history and the new highlight back on it.
        """
        self.start()
        self._last_screen = screen
        self._last_overlays = overlays
        projection_top, visible_height = self._projection(screen.height)
        rows = frozenset(
            row for row in changed_rows
            if projection_top <= row < projection_top + visible_height
        )
        rendered: list[bytes] = [b"\033[?7l"]
        for row in sorted(rows):
            rendered.extend((
                f"\033[{row - projection_top + 1};1H".encode(),
                b"\033[2K",
                screen.rows[row],
            ))
        self._append_overlay_rows(
            rendered,
            overlays,
            projection_top=projection_top,
            visible_height=visible_height,
            changed_rows=rows,
        )
        self._append_selection_segments(
            rendered,
            selection,
            projection_top=projection_top,
            visible_height=visible_height,
        )
        if self._local_status_text is not None and screen.height - 1 in rows:
            rendered.append(self._render_local_status(hide_cursor=False))
        self._append_cursor(
            rendered,
            screen,
            overlays,
            projection_top=projection_top,
            visible_height=visible_height,
        )
        self.stream.write(b"".join(rendered))
        self.stream.flush()

    def reassert_cursor(self) -> None:
        """Restore the last authoritative cursor without repainting the frame."""
        if self._last_screen is None:
            return
        projection_top, visible_height = self._projection(self._last_screen.height)
        rendered: list[bytes] = []
        self._append_cursor(
            rendered,
            self._last_screen,
            self._last_overlays,
            projection_top=projection_top,
            visible_height=visible_height,
            force_visibility=True,
        )
        self.stream.write(b"".join(rendered))
        self.stream.flush()

    def close(self) -> None:
        if not self.active:
            return
        controls = [b"\033[0m\033[?7h\033[?25h"]
        if self.terminal_modes & TerminalMode.BRACKETED_PASTE:
            controls.append(b"\033[?2004l")
        if self.terminal_modes & TerminalMode.FOCUS_EVENTS:
            controls.append(b"\033[?1004l")
        if self.mouse_active:
            controls.append(self._mouse_mode(False))
        controls.append(b"\033[?1049l")
        self.stream.write(b"".join(controls))
        self.stream.flush()
        self.terminal_modes = TerminalMode.NONE
        self.mouse_active = False
        self.mouse_suspended = False
        self.cursor_hidden = False
        self._painted_cursor_visible = None
        self.clear_local_status()
        self._last_screen = None
        self._reconnect_status_screen = None
        self._last_overlays = ()
        self._awaiting_reconnect_frame = False
        self.active = False
        self.interaction_active = False


@dataclass
class _DeferredHistoryPaint:
    """Collapse one local-input batch into its final history viewport paint."""

    render_history: bool = False
    restore_live: bool = False

    def defer(self, action: HistoryAction) -> None:
        self.render_history = self.render_history or action.render_history
        self.restore_live = self.restore_live or action.restore_live

    def flush(
        self,
        surface: TerminalSurface,
        screen: AppliedScreen | None,
        overlays: tuple[tuple[HistorySnapshot, tuple[bytes, ...]], ...],
        selection: tuple[SelectionSegment, ...],
    ) -> None:
        if screen is not None:
            if self.restore_live:
                # A viewport may have reached the live bottom and another may
                # still be frozen. Restore the authoritative frame and compose
                # only the final overlays in one terminal write.
                surface.paint(full_repaint(screen), overlays, selection)
            elif self.render_history:
                surface.paint_overlays(screen, overlays, selection)
        self.render_history = False
        self.restore_live = False


class _LocalInputBatch:
    """Preserve local wheel distance while bounding forwarded wheel bursts."""

    def __init__(self) -> None:
        self._forwarded_wheel_directions: set[int] = set()
        self._history_paint = _DeferredHistoryPaint()

    def prepare(
        self,
        event: SgrMouseEvent,
        action: HistoryAction,
    ) -> tuple[HistoryAction | None, _DeferredHistoryPaint | None]:
        if event.wheel_direction == 0:
            return action, None
        if action.forwarded_input:
            if not claim_batched_forwarded_wheel(
                event,
                self._forwarded_wheel_directions,
            ):
                return None, None
            return action, None
        # Every locally owned history tick has already advanced the viewport.
        # Defer only its terminal paint so a Windows/RDP read containing
        # several SGR packets preserves the complete distance without issuing
        # one synchronous pane repaint per packet.
        return action, self._history_paint

    def flush(
        self,
        surface: TerminalSurface,
        screen: AppliedScreen | None,
        overlays: tuple[tuple[HistorySnapshot, tuple[bytes, ...]], ...],
        selection: tuple[SelectionSegment, ...],
    ) -> None:
        self._history_paint.flush(surface, screen, overlays, selection)


def _remote_server_args(
    *,
    session: str,
    width: int,
    height: int,
    fps: float,
    replace_existing_client: bool = False,
    existing_session_only: bool = False,
) -> list[str]:
    args = [
        "remote-server",
        "--protocol",
        str(PROTOCOL_VERSION),
        "--session",
        session,
        "--width",
        str(width),
        "--height",
        str(height),
        "--fps",
        str(fps),
    ]
    if replace_existing_client:
        args.append("--replace-existing-client")
    if existing_session_only:
        args.append("--existing-session-only")
    return args


def _remote_launch_command(server_args: Sequence[str]) -> str:
    direct = shlex.join(["railmux", *server_args])
    managed_python = f'"$HOME/{_REMOTE_VENV}/bin/python"'
    managed_args = shlex.join(["-m", "railmux", *server_args])
    branches = [
        f"if [ -x {managed_python} ] "
        f"&& {managed_python} -c 'import railmux' >/dev/null 2>&1; "
        f"then exec {managed_python} {managed_args}",
        f"elif command -v railmux >/dev/null 2>&1; then exec {direct}",
    ]
    for python in ("python3", "python"):
        probe = shlex.join([python, "-c", "import railmux"])
        launch = shlex.join([python, "-m", "railmux", *server_args])
        branches.append(
            f"elif command -v {python} >/dev/null 2>&1 "
            f"&& {probe} >/dev/null 2>&1; then exec {launch}"
        )
    branches.append("else exit 127; fi")
    return "; ".join(branches)


def _remote_direct_launch_command(server_args: Sequence[str]) -> str:
    """Build a shell-neutral command accepted by Windows OpenSSH PowerShell."""
    return shlex.join(["railmux", *server_args])


def build_remote_command_argv(
    destination: str,
    *,
    remote_args: Sequence[str],
    ssh_args: Sequence[str],
    force_tty: bool = False,
    launch_mode: RemoteLaunchMode = RemoteLaunchMode.POSIX,
) -> list[str]:
    """Build SSH argv through the shared remote Railmux discovery ladder."""
    return [
        "ssh",
        *ssh_args,
        # Railmux owns RequestTTY because the binary display protocol requires
        # no PTY while the remote config editor requires one. Keep this after
        # user arguments so a copied -t/-T flag cannot invert the transport.
        "-tt" if force_tty else "-T",
        destination,
        (
            _remote_direct_launch_command(remote_args)
            if launch_mode is RemoteLaunchMode.DIRECT
            else _remote_launch_command(remote_args)
        ),
    ]


def build_ssh_argv(
    destination: str,
    *,
    session: str,
    width: int,
    height: int,
    fps: float,
    ssh_args: Sequence[str],
    replace_existing_client: bool = False,
    existing_session_only: bool = False,
    launch_mode: RemoteLaunchMode = RemoteLaunchMode.POSIX,
) -> list[str]:
    server_args = _remote_server_args(
        session=session,
        width=width,
        height=height,
        fps=fps,
        replace_existing_client=replace_existing_client,
        existing_session_only=existing_session_only,
    )
    return build_remote_command_argv(
        destination,
        remote_args=server_args,
        # User arguments come first because OpenSSH keeps the first value for
        # scalar options.  These display-only defaults bound black-hole
        # detection without preventing an explicit user override.
        ssh_args=(
            *ssh_args,
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=3",
        ),
        launch_mode=launch_mode,
    )


def build_remote_install_argv(
    destination: str,
    *,
    version: str,
    remote_args: Sequence[str],
    ssh_args: Sequence[str],
) -> list[str]:
    """Install into the remote user environment, then exec Railmux args."""
    requirement = f"railmux[ssh]=={version}"
    managed_python = f'"$HOME/{_REMOTE_VENV}/bin/python"'
    managed_install = shlex.join(
        [
            "-m",
            "pip",
            "install",
            "--upgrade",
            requirement,
        ]
    )
    managed_launch = shlex.join(["-m", "railmux", *remote_args])
    branches = [
        f"if [ -x {managed_python} ] "
        f"&& {managed_python} -m pip --version >/dev/null 2>&1; "
        f"then {managed_python} {managed_install} 1>&2 "
        f"&& exec {managed_python} {managed_launch}; exit $?"
    ]
    candidates = (
        (("python3", "-m", "pip"), ("python3", "-m", "railmux")),
        (("python", "-m", "pip"), ("python", "-m", "railmux")),
        (("pip3",), ("python3", "-m", "railmux")),
        (("pip",), ("python", "-m", "railmux")),
    )
    for installer, runner in candidates:
        executable = installer[0]
        runner_executable = runner[0]
        pip_probe = shlex.join([*installer, "--version"])
        condition = (
            f"command -v {executable} >/dev/null 2>&1 && {pip_probe} >/dev/null 2>&1"
        )
        if runner_executable != executable:
            condition += f" && command -v {runner_executable} >/dev/null 2>&1"
        install = shlex.join(
            [
                *installer,
                "install",
                "--user",
                "--upgrade",
                requirement,
            ]
        )
        launch = shlex.join([*runner, *remote_args])
        branches.append(
            f"elif {condition}; then {install} 1>&2 && exec {launch}; exit $?"
        )
    branches.append(
        "else echo 'error: no usable python/pip, python3/pip3, or pip was found' "
        ">&2; exit 127; fi"
    )
    return ["ssh", *ssh_args, "-T", destination, "; ".join(branches)]


def build_ssh_install_argv(
    destination: str,
    *,
    version: str,
    session: str,
    width: int,
    height: int,
    fps: float,
    ssh_args: Sequence[str],
) -> list[str]:
    """Install into the remote user environment, then exec the same session."""
    server_args = _remote_server_args(
        session=session, width=width, height=height, fps=fps
    )
    return build_remote_install_argv(
        destination,
        version=version,
        remote_args=server_args,
        ssh_args=ssh_args,
    )


def build_remote_private_venv_install_argv(
    destination: str,
    *,
    version: str,
    remote_args: Sequence[str],
    ssh_args: Sequence[str],
) -> list[str]:
    """Install into Railmux's private remote venv, then exec Railmux args."""
    requirement = f"railmux[ssh]=={version}"
    managed_dir = f'"$HOME/{_REMOTE_VENV}"'
    managed_python = f"{managed_dir}/bin/python"
    install = shlex.join(
        [
            "-m",
            "pip",
            "install",
            "--upgrade",
            requirement,
        ]
    )
    launch = shlex.join(["-m", "railmux", *remote_args])
    branches = [
        f"if [ -x {managed_python} ] "
        f"&& {managed_python} -m pip --version >/dev/null 2>&1; "
        f"then {managed_python} {install} 1>&2 "
        f"&& exec {managed_python} {launch}; exit $?"
    ]
    for python in ("python3", "python"):
        branches.append(
            f"elif command -v {python} >/dev/null 2>&1 "
            f"&& {python} -m venv {managed_dir} 1>&2; then "
            f"{managed_python} {install} 1>&2 "
            f"&& exec {managed_python} {launch}; exit $?"
        )
    branches.append(
        "else echo 'error: no usable python3 or python was found to create "
        "the private Railmux environment' >&2; exit 127; fi"
    )
    return ["ssh", *ssh_args, "-T", destination, "; ".join(branches)]


def build_ssh_private_venv_install_argv(
    destination: str,
    *,
    version: str,
    session: str,
    width: int,
    height: int,
    fps: float,
    ssh_args: Sequence[str],
) -> list[str]:
    """Create Railmux's private remote venv, install, and start it."""
    server_args = _remote_server_args(
        session=session, width=width, height=height, fps=fps
    )
    return build_remote_private_venv_install_argv(
        destination,
        version=version,
        remote_args=server_args,
        ssh_args=ssh_args,
    )


def remote_install_help(destination: str, version: str) -> str:
    requirement = shlex.quote(f"railmux[ssh]=={version}")
    return (
        f"Install it manually on {destination}, then retry:\n"
        f"  python3 -m pip install --user --upgrade {requirement}\n"
        f"or:\n  pip3 install --user --upgrade {requirement}\n"
        "These commands use per-user site packages and do not modify the "
        "system Python. If site policy still rejects them, use a private "
        "Railmux environment:\n"
        f"  python3 -m venv ~/{_REMOTE_VENV}\n"
        f"  ~/{_REMOTE_VENV}/bin/python -m pip install --upgrade {requirement}\n"
        "If that version is not published, copy the matching wheel or source "
        "checkout to the remote host and install it with the same Python."
    )


def remote_windows_install_help(destination: str, version: str) -> str:
    return (
        f"Update the user-level Windows preview on {destination} from "
        "PowerShell, then retry:\n"
        f"  py -m pip install --upgrade railmux=={version}\n"
        "  railmux runtime install --yes\n"
        "The runtime command installs only the matching Railmux app layer and "
        "reuses a verified managed MSYS2 base when its pinned version is "
        "unchanged. It does not modify Codex or Claude session files. If 'py' "
        "is unavailable, use the same Windows Python executable that installed "
        "the Railmux command."
    )


def _remote_launch_mode(args: argparse.Namespace) -> RemoteLaunchMode:
    selected = getattr(args, "_selected_remote_launch_mode", None)
    if isinstance(selected, RemoteLaunchMode):
        return selected
    if getattr(args, "remote_platform", "auto") == "windows":
        return RemoteLaunchMode.DIRECT
    return RemoteLaunchMode.POSIX


def _remember_remote_launch_mode(
    args: argparse.Namespace, mode: RemoteLaunchMode
) -> None:
    args._selected_remote_launch_mode = mode


def _remote_uses_windows_runtime(
    args: argparse.Namespace, hello: RemoteHello | None = None
) -> bool:
    return _remote_launch_mode(args) is RemoteLaunchMode.DIRECT or (
        hello is not None and hello.platform == "windows-msys2"
    )


def remote_tmux_help(destination: str) -> str:
    return (
        f"tmux is not installed or not on PATH on {destination}. Install it "
        "with the remote operating system's package manager, then retry. "
        "Railmux will not run sudo or install system packages automatically."
    )


def remote_config_help(destination: str, *, tmux: bool = False) -> str:
    if tmux:
        problem = "The tmux executable selected by remote Railmux is unavailable."
    else:
        problem = "The remote Railmux configuration is invalid."
    return (
        f"{problem} Log in to {destination}, run 'railmux config', and "
        "repair or reset the affected setting before retrying."
    )


def _confirm(question: str) -> bool:
    try:
        answer = input(f"{question} [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return False
    return answer.strip().lower() in ("y", "yes")


def _stop_unstarted_remote(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _reap_remote(process: subprocess.Popen, *, terminate: bool = False) -> int:
    """Bound one SSH child's lifetime while preserving its natural status."""
    if terminate and process.poll() is None:
        process.terminate()
    try:
        return process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            return process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait()


def _local_upgrade_argv(version: str) -> list[str]:
    from railmux.self_update import upgrade_argv

    return upgrade_argv(version)


def _upgrade_local_and_restart(
    version: str,
    raw_args: Sequence[str],
    *,
    subcommand: str = "ssh",
) -> NoReturn:
    from railmux.self_update import installed_version_matches

    argv = _local_upgrade_argv(version)
    command_label = f"railmux {subcommand}"
    print(
        f"{command_label}: upgrading local Railmux to {version}...",
        file=sys.stderr,
    )
    try:
        result = subprocess.run(argv, check=False)
    except OSError as exc:
        raise ProbeError(
            f"could not start local pip: {exc}\nRun manually:\n  {shlex.join(argv)}"
        ) from exc
    if result.returncode:
        raise ProbeError(
            "local Railmux upgrade failed. Run manually, then retry:\n  "
            f"{shlex.join(argv)}"
        )
    if not installed_version_matches(version):
        raise ProbeError(
            "pip reported success, but a fresh Railmux process did not import "
            f"version {version}. Run manually, then retry:\n  {shlex.join(argv)}"
        )
    restart = [sys.executable, "-m", "railmux", subcommand, *raw_args]
    print(
        f"{command_label}: local upgrade succeeded; restarting...",
        file=sys.stderr,
    )
    try:
        os.execv(sys.executable, restart)
    except OSError as exc:
        raise ProbeError(
            "local upgrade succeeded but Railmux could not restart; run:\n  "
            f"{shlex.join(restart)}"
        ) from exc
    raise AssertionError("os.execv returned unexpectedly")


def _spawn_remote(
    argv: Sequence[str],
    *,
    suppress_stderr: bool = False,
) -> subprocess.Popen:
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if suppress_stderr else None,
        )
    except OSError as exc:
        raise ProbeError(f"could not start ssh: {exc}") from exc
    assert process.stdin is not None
    assert process.stdout is not None
    return process


def _install_remote_and_start(
    args: argparse.Namespace,
    current_size: os.terminal_size,
    version: str,
) -> tuple[subprocess.Popen, RemoteStartup]:
    install_argv = build_ssh_install_argv(
        args.destination,
        version=version,
        session=args.session,
        width=current_size.columns,
        height=current_size.lines,
        fps=args.fps,
        ssh_args=args.ssh_arg,
    )
    process = _spawn_remote(install_argv)
    startup = await_remote_startup(process)
    return process, startup


def _install_remote_private_venv_and_start(
    args: argparse.Namespace,
    current_size: os.terminal_size,
    version: str,
) -> tuple[subprocess.Popen, RemoteStartup]:
    install_argv = build_ssh_private_venv_install_argv(
        args.destination,
        version=version,
        session=args.session,
        width=current_size.columns,
        height=current_size.lines,
        fps=args.fps,
        ssh_args=args.ssh_arg,
    )
    process = _spawn_remote(install_argv)
    startup = await_remote_startup(process)
    return process, startup


def _confirm_remote_install(
    args: argparse.Namespace,
    reason: str,
    version: str,
    *,
    before_interaction: Callable[[], None] | None = None,
) -> bool:
    if before_interaction is not None:
        before_interaction()
    return _confirm(
        f"{reason} Install Railmux {version} with SSH support into "
        f"the remote user environment on {args.destination}?"
    )


def _confirm_remote_private_venv_install(
    args: argparse.Namespace,
    version: str,
    *,
    before_interaction: Callable[[], None] | None = None,
) -> bool:
    if before_interaction is not None:
        before_interaction()
    return _confirm(
        "Remote user-site installation failed or timed out. Create the isolated "
        f"~/{_REMOTE_VENV} environment and install Railmux {version} there? "
        "This does not use sudo or modify the system Python."
    )


def _send_start(process: subprocess.Popen) -> None:
    try:
        process.stdin.write(REMOTE_START)
        process.stdin.flush()
    except BrokenPipeError as exc:
        raise ProbeError("remote Railmux exited before accepting the display") from exc


def _reconnect_remote_attach(
    args: argparse.Namespace,
    current_size: os.terminal_size,
    *,
    replace_existing_client: bool,
    cancel_fd: int | None = None,
    timeout: float | None = None,
    noninteractive: bool = False,
    existing_session_only: bool = False,
) -> tuple[subprocess.Popen, RemoteAttachKind]:
    """Start one already-negotiated helper and return its attach status."""
    ssh_args = list(args.ssh_arg)
    if noninteractive:
        ssh_args = [
            "-o",
            "BatchMode=yes",
            *ssh_args,
            # OpenSSH keeps the first scalar -o value. BatchMode is a safety
            # invariant for raw-mode reconnects, while a user-supplied
            # ConnectTimeout should remain authoritative.
            "-o",
            f"ConnectTimeout={max(1, int(timeout or 5))}",
        ]
    argv = build_ssh_argv(
        args.destination,
        session=args.session,
        width=current_size.columns,
        height=current_size.lines,
        fps=args.fps,
        ssh_args=ssh_args,
        replace_existing_client=replace_existing_client,
        existing_session_only=existing_session_only,
        launch_mode=_remote_launch_mode(args),
    )
    process = (
        _spawn_remote(argv, suppress_stderr=True)
        if noninteractive
        else _spawn_remote(argv)
    )
    try:
        if cancel_fd is None and timeout is None:
            startup = await_remote_startup(process)
        else:
            startup = await_remote_startup(
                process,
                timeout=timeout or _REMOTE_HELLO_TIMEOUT,
                cancel_fd=cancel_fd,
            )
        hello = startup.hello
        if (
            startup.kind is not RemoteStartKind.HELLO
            or hello is None
            or hello.protocol != PROTOCOL_VERSION
            or not hello.ready
            or not hello.tmux
        ):
            raise ProbeError(
                "reconnect could not start a compatible remote display; "
                "the Railmux session and agents were left intact"
            )
        _send_start(process)
        if cancel_fd is None and timeout is None:
            status = await_remote_attach_status(process)
        else:
            status = await_remote_attach_status(
                process,
                timeout=timeout or _REMOTE_ATTACH_TIMEOUT,
                cancel_fd=cancel_fd,
            )
    except BaseException:
        _stop_unstarted_remote(process)
        raise
    return process, status


def _wait_reconnect_delay(delay: float, cancel_fd: int) -> None:
    """Wait without making a disconnected raw terminal feel trapped."""
    deadline = time.monotonic() + delay
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        readable, _writable, _exceptional = select.select(
            [cancel_fd], [], [], remaining
        )
        if readable:
            _consume_reconnect_input(cancel_fd)


def _automatic_reconnect(
    args: argparse.Namespace,
    current_size: os.terminal_size,
    surface: TerminalSurface,
    cancel_fd: int,
    reconnect_metrics: dict[str, int] | None = None,
) -> subprocess.Popen:
    """Reconnect one display without install, upgrade, takeover, or prompts."""
    surface.begin_reconnect()
    deadline = time.monotonic() + _RECONNECT_WINDOW
    delay = 0.0
    attempt = 0
    last_reason = "the remote display did not accept a new client"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeError(
                "automatic reconnect timed out; the Railmux session and agents "
                "were left intact. Rerun the command to reconnect."
            )
        if delay:
            wait_for = min(delay, remaining)
            surface.show_local_status(
                f"Connection lost; retrying in {wait_for:.1f}s (Ctrl-] or Ctrl-C stops)"
            )
            _wait_reconnect_delay(wait_for, cancel_fd)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            continue
        attempt += 1
        if reconnect_metrics is not None:
            reconnect_metrics["attempts"] = reconnect_metrics.get("attempts", 0) + 1
        surface.show_local_status(
            f"Reconnecting (attempt {attempt}; Ctrl-] or Ctrl-C stops)"
        )
        timeout = min(_RECONNECT_ATTEMPT_TIMEOUT, max(0.1, remaining / 2))
        try:
            process, status = _reconnect_remote_attach(
                args,
                current_size,
                replace_existing_client=False,
                cancel_fd=cancel_fd,
                timeout=timeout,
                noninteractive=True,
                existing_session_only=True,
            )
        except ReconnectCancelled:
            raise
        except ProbeError as exc:
            last_reason = str(exc)
        else:
            if status is RemoteAttachKind.ACCEPTED:
                return process
            _stop_unstarted_remote(process)
            if status is RemoteAttachKind.BUSY:
                last_reason = "the previous remote helper is still releasing its lease"
            elif status is RemoteAttachKind.TIMEOUT:
                last_reason = "the remote attach timed out"
            else:
                last_reason = "the remote display rejected the attach"
        delay = min(
            _RECONNECT_MAX_DELAY,
            0.5 if delay == 0 else delay * 2,
        )
        if time.monotonic() + delay >= deadline:
            raise ProbeError(
                f"automatic reconnect timed out ({last_reason}); the Railmux "
                "session and agents were left intact. Rerun the command to "
                "reconnect."
            )


def should_automatically_reconnect(
    *,
    enabled: bool,
    painted_frames: int,
    local_exit: bool,
    returncode: int | None,
) -> bool:
    """Classify only an established, unexpectedly ended display as retryable."""
    return (
        enabled
        and painted_frames > 0
        and not local_exit
        and returncode is not None
        and returncode not in _KNOWN_REMOTE_EXITS
    )


def _finish_remote_attach(
    args: argparse.Namespace,
    current_size: os.terminal_size,
    process: subprocess.Popen,
    *,
    before_interaction: Callable[[], None] | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> subprocess.Popen:
    """Complete the cooked-mode attach handshake and one consented takeover."""
    if on_stage is not None:
        on_stage("Attaching to workspace…")
    try:
        _send_start(process)
    except ProbeError:
        _stop_unstarted_remote(process)
        raise
    status = await_remote_attach_status(process)
    if status is RemoteAttachKind.ACCEPTED:
        return process
    if status is RemoteAttachKind.TIMEOUT:
        _stop_unstarted_remote(process)
        raise ProbeError("timed out waiting for the remote display to attach")
    if status is not RemoteAttachKind.BUSY:
        _stop_unstarted_remote(process)
        raise ProbeError("remote display helper failed before attaching")

    _stop_unstarted_remote(process)
    # A current v15 helper holds the mutex only while registering its exact tmux
    # child. Give that ordinary race one fresh SSH process before presenting
    # the explicit legacy-lock takeover choice.
    time.sleep(_REMOTE_ATTACH_RETRY_DELAY)
    retry, retry_status = _reconnect_remote_attach(
        args, current_size, replace_existing_client=False
    )
    if retry_status is RemoteAttachKind.ACCEPTED:
        return retry
    _stop_unstarted_remote(retry)
    if retry_status is RemoteAttachKind.TIMEOUT:
        raise ProbeError("timed out while retrying the remote display attach")
    if retry_status is not RemoteAttachKind.BUSY:
        raise ProbeError("remote display helper failed while retrying attach")

    if before_interaction is not None:
        before_interaction()
    if not _confirm(
        "Another display helper is persistently holding the attach lock. "
        "Replace "
        "it? This detaches every terminal currently attached to the same "
        "managed Railmux session, but keeps the session and agents alive."
    ):
        raise ProbeError(
            "remote Railmux is still owned by an older display client; "
            "retry after it exits, or reconnect and approve replacement"
        )

    replacement, replacement_status = _reconnect_remote_attach(
        args, current_size, replace_existing_client=True
    )
    if replacement_status is not RemoteAttachKind.ACCEPTED:
        _stop_unstarted_remote(replacement)
        raise ProbeError(
            "the old display client did not release in time; the Railmux "
            "session and agents remain intact, so retry shortly"
        )
    return replacement


def prepare_remote_process(
    args: argparse.Namespace,
    current_size: os.terminal_size,
    *,
    before_interaction: Callable[[], None] | None = None,
    before_local_restart: Callable[[], None] | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> subprocess.Popen:
    """Resolve compatibility and consent before the remote attaches to tmux."""

    def reveal_terminal() -> None:
        if before_interaction is not None:
            before_interaction()

    launch_mode = _remote_launch_mode(args)
    argv = build_ssh_argv(
        args.destination,
        session=args.session,
        width=current_size.columns,
        height=current_size.lines,
        fps=args.fps,
        ssh_args=args.ssh_arg,
        launch_mode=launch_mode,
    )
    if on_stage is not None:
        on_stage("Connecting to remote host…")
    process = _spawn_remote(argv)
    startup = await_remote_startup(process)
    if (
        getattr(args, "remote_platform", "auto") == "auto"
        and launch_mode is RemoteLaunchMode.POSIX
        and startup.kind is RemoteStartKind.FAILED
        and startup.returncode != 255
    ):
        _stop_unstarted_remote(process)
        direct_argv = build_ssh_argv(
            args.destination,
            session=args.session,
            width=current_size.columns,
            height=current_size.lines,
            fps=args.fps,
            ssh_args=args.ssh_arg,
            launch_mode=RemoteLaunchMode.DIRECT,
        )
        process = _spawn_remote(direct_argv)
        startup = await_remote_startup(process)
        if startup.kind is RemoteStartKind.HELLO:
            launch_mode = RemoteLaunchMode.DIRECT
            _remember_remote_launch_mode(args, launch_mode)
        else:
            _stop_unstarted_remote(process)
            if startup.returncode == 255:
                raise ProbeError("ssh could not connect to the remote host")
            if startup.kind is RemoteStartKind.TIMEOUT:
                raise ProbeError(
                    "timed out while checking the remote host's direct Railmux launcher"
                )
            raise ProbeError(
                "the remote shell did not accept the POSIX discovery command, "
                "and a direct Railmux launch did not complete. If this is a "
                "Windows preview host:\n"
                f"{remote_windows_install_help(args.destination, __version__)}"
            )
    elif startup.kind is RemoteStartKind.HELLO:
        _remember_remote_launch_mode(args, launch_mode)

    if (
        getattr(args, "remote_platform", "auto") == "windows"
        and startup.kind is not RemoteStartKind.HELLO
    ):
        _stop_unstarted_remote(process)
        if startup.returncode == 255:
            raise ProbeError("ssh could not connect to the remote host")
        if startup.kind is RemoteStartKind.TIMEOUT:
            raise ProbeError(
                "timed out waiting for the Windows Railmux compatibility handshake"
            )
        raise ProbeError(remote_windows_install_help(args.destination, __version__))
    install_version = __version__
    install_reason: str | None = None
    if startup.kind is RemoteStartKind.MISSING:
        install_reason = "Railmux is not installed or discoverable remotely."
    elif startup.kind is RemoteStartKind.TIMEOUT:
        _stop_unstarted_remote(process)
        raise ProbeError(
            "timed out waiting for the remote Railmux compatibility handshake"
        )
    elif startup.kind is RemoteStartKind.FAILED:
        _stop_unstarted_remote(process)
        if startup.returncode == 255:
            raise ProbeError("ssh could not connect to the remote host")
        install_reason = (
            "The remote Railmux does not support the compatibility handshake."
        )
    else:
        assert startup.hello is not None
        hello = startup.hello
        if hello.config_status != "valid":
            _stop_unstarted_remote(process)
            raise ProbeError(remote_config_help(args.destination))
        if not hello.tmux and hello.tmux_configured:
            _stop_unstarted_remote(process)
            raise ProbeError(remote_config_help(args.destination, tmux=True))
        if on_stage is not None:
            on_stage("Checking Railmux versions…")
        facts = CompatibilityFacts(
            local_version=__version__,
            local_protocol=PROTOCOL_VERSION,
            remote_version=hello.version,
            remote_protocol=hello.protocol,
            remote_ready=hello.ready,
            remote_tmux=hello.tmux,
        )
        consents: dict[str, bool] = {}
        while True:
            decision = decide_compatibility(facts, consents)
            if decision.action == "prompt" and decision.prompt == "local_upgrade":
                reveal_terminal()
                consents["local_upgrade"] = _confirm(
                    f"{decision.reason} Upgrade local Railmux to "
                    f"{hello.version}?"
                )
                continue
            if decision.action == "upgrade_local":
                _stop_unstarted_remote(process)
                assert decision.install_version is not None
                if before_local_restart is not None:
                    before_local_restart()
                _upgrade_local_and_restart(decision.install_version, args.raw_argv)
            if decision.action == "prompt" and decision.prompt == "remote_install":
                assert decision.reason is not None
                assert decision.install_version is not None
                if _remote_uses_windows_runtime(args, hello):
                    _stop_unstarted_remote(process)
                    raise ProbeError(
                        f"{decision.reason}\n"
                        f"{remote_windows_install_help(args.destination, decision.install_version)}"
                    )
                consents["remote_install"] = _confirm_remote_install(
                    args,
                    decision.reason,
                    decision.install_version,
                    before_interaction=before_interaction,
                )
                continue
            if decision.action == "tmux_missing":
                _stop_unstarted_remote(process)
                raise ProbeError(remote_tmux_help(args.destination))
            if decision.action == "error":
                _stop_unstarted_remote(process)
                message = decision.reason or "incompatible remote Railmux"
                if decision.install_version is not None:
                    install_help = (
                        remote_windows_install_help
                        if _remote_uses_windows_runtime(args, hello)
                        else remote_install_help
                    )
                    message = (
                        f"{message}\n"
                        f"{install_help(args.destination, decision.install_version)}"
                    )
                raise ProbeError(message)
            if decision.warning is not None:
                reveal_terminal()
                print(f"warning: {decision.warning}", file=sys.stderr)
            if decision.action == "attach":
                return _finish_remote_attach(
                    args,
                    current_size,
                    process,
                    before_interaction=before_interaction,
                    on_stage=on_stage,
                )
            if decision.action == "install_remote":
                assert decision.reason is not None
                assert decision.install_version is not None
                install_reason = decision.reason
                install_version = decision.install_version
                break
            raise AssertionError(f"unknown compatibility action: {decision.action}")

    assert install_reason is not None
    if _remote_uses_windows_runtime(
        args, startup.hello if startup.kind is RemoteStartKind.HELLO else None
    ):
        _stop_unstarted_remote(process)
        raise ProbeError(
            f"{install_reason}\n"
            f"{remote_windows_install_help(args.destination, install_version)}"
        )
    if startup.kind is not RemoteStartKind.HELLO and not _confirm_remote_install(
        args,
        install_reason,
        install_version,
        before_interaction=before_interaction,
    ):
        _stop_unstarted_remote(process)
        raise ProbeError(remote_install_help(args.destination, install_version))
    _stop_unstarted_remote(process)
    process, startup = _install_remote_and_start(args, current_size, install_version)
    if (
        startup.kind is RemoteStartKind.HELLO
        and startup.hello is not None
        and startup.hello.config_status != "valid"
    ):
        _stop_unstarted_remote(process)
        raise ProbeError(remote_config_help(args.destination))
    if (
        startup.kind is RemoteStartKind.HELLO
        and startup.hello is not None
        and not startup.hello.tmux
    ):
        _stop_unstarted_remote(process)
        raise ProbeError(
            remote_config_help(args.destination, tmux=True)
            if startup.hello.tmux_configured
            else remote_tmux_help(args.destination)
        )
    if startup.kind in (
        RemoteStartKind.MISSING,
        RemoteStartKind.FAILED,
        RemoteStartKind.TIMEOUT,
    ):
        _stop_unstarted_remote(process)
        if not _confirm_remote_private_venv_install(
            args,
            install_version,
            before_interaction=before_interaction,
        ):
            raise ProbeError(
                "remote user-site installation failed or timed out.\n"
                f"{remote_install_help(args.destination, install_version)}"
            )
        process, startup = _install_remote_private_venv_and_start(
            args, current_size, install_version
        )
        if (
            startup.kind is RemoteStartKind.HELLO
            and startup.hello is not None
            and startup.hello.config_status != "valid"
        ):
            _stop_unstarted_remote(process)
            raise ProbeError(remote_config_help(args.destination))
        if (
            startup.kind is RemoteStartKind.HELLO
            and startup.hello is not None
            and not startup.hello.tmux
        ):
            _stop_unstarted_remote(process)
            raise ProbeError(
                remote_config_help(args.destination, tmux=True)
                if startup.hello.tmux_configured
                else remote_tmux_help(args.destination)
            )
    if (
        startup.kind is not RemoteStartKind.HELLO
        or startup.hello is None
        or startup.hello.version != install_version
        or startup.hello.protocol != PROTOCOL_VERSION
        or not startup.hello.ready
        or not startup.hello.tmux
    ):
        _stop_unstarted_remote(process)
        raise ProbeError(
            "automatic remote installation did not produce a compatible "
            f"Railmux.\n{remote_install_help(args.destination, install_version)}"
        )
    return _finish_remote_attach(
        args,
        current_size,
        process,
        before_interaction=before_interaction,
        on_stage=on_stage,
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="railmux ssh",
        description=(
            "Connect to Railmux with a version-negotiated latest-state SSH display"
        ),
        epilog=(
            "Before attaching, missing or incompatible remote packages can "
            "be installed after confirmation; automatic setup never uses sudo."
        ),
    )
    parser.add_argument("destination", help="SSH destination or configured host alias")
    parser.add_argument(
        "--session",
        default="railmux",
        help="managed remote tmux session name (default: railmux)",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=20.0,
        help="maximum coalesced display update rate, 1-60 (default: 20)",
    )
    parser.add_argument(
        "--remote-platform",
        choices=("auto", "posix", "windows"),
        default="auto",
        help=(
            "remote shell family: auto tries POSIX discovery then direct "
            "Windows launch; windows avoids the failed POSIX probe and an "
            "extra password prompt (default: auto)"
        ),
    )
    parser.add_argument(
        "--no-mouse",
        action="store_true",
        help="do not capture mouse events (allows ordinary terminal selection)",
    )
    reconnect_group = parser.add_mutually_exclusive_group()
    reconnect_group.add_argument(
        "--reconnect",
        dest="reconnect",
        action="store_true",
        default=True,
        help=(
            "retry an established display for up to 60 seconds after an "
            "unexpected connection loss (default)"
        ),
    )
    reconnect_group.add_argument(
        "--no-reconnect",
        dest="reconnect",
        action="store_false",
        help="exit immediately after an established display loses its connection",
    )
    parser.add_argument(
        "--history-lines",
        type=int,
        default=None,
        metavar="LINES",
        help=(
            "local agent history limit "
            f"({SSH_HISTORY_MIN_LINES}-{SSH_HISTORY_MAX_LINES}; "
            "default from [ssh].history_lines)"
        ),
    )
    parser.add_argument(
        "--ssh-arg",
        action=AppendSshArgument,
        dest="ssh_arg",
        default=[],
        metavar="VALUE",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--ssh-args",
        action=ExtendSshArguments,
        dest="ssh_arg",
        metavar="ARGS",
        help=(
            "a quoted group of ssh arguments, split locally without running "
            "a shell"
        ),
    )
    args = parser.parse_args(raw_argv)
    args.raw_argv = tuple(raw_argv)
    if not 1.0 <= args.fps <= 60.0:
        parser.error("--fps must be between 1 and 60")
    if (
        args.history_lines is not None
        and not SSH_HISTORY_MIN_LINES <= args.history_lines <= SSH_HISTORY_MAX_LINES
    ):
        parser.error(
            "--history-lines must be between "
            f"{SSH_HISTORY_MIN_LINES} and {SSH_HISTORY_MAX_LINES}"
        )
    return args


def run(args: argparse.Namespace) -> int:
    diagnostic_started = time.monotonic()
    recorder = SshDisplayRecorder(__version__, PROTOCOL_VERSION)
    try:
        try:
            config = load_config()
        except ConfigError as exc:
            raise ProbeError(
                f"configuration error: {exc}; run 'railmux config' to repair "
                "or reset it"
            ) from exc
        history_limit = (
            config.ssh_history_lines
            if args.history_lines is None
            else args.history_lines
        )
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ProbeError("stdin and stdout must both be interactive terminals")
        if shutil.which("ssh") is None:
            raise ProbeError("ssh is not installed or not on PATH")
        current_size = wait_for_usable_terminal_size(sys.stdout.fileno())
    except KeyboardInterrupt:
        recorder.finish("startup_failed", SshDisplayStats())
        print(
            "\nrailmux ssh: cancelled while waiting for terminal size", file=sys.stderr
        )
        return 130
    except BaseException:
        recorder.finish("startup_failed", SshDisplayStats())
        raise
    termux_touch = is_termux_environment()
    surface = TerminalSurface(
        sys.stdout.buffer,
        mouse=not args.no_mouse,
        mouse_hover=not termux_touch,
    )
    surface.show_startup(current_size, "Connecting to remote host…")
    try:
        process = prepare_remote_process(
            args,
            current_size,
            before_interaction=surface.begin_interaction,
            before_local_restart=surface.close,
            on_stage=lambda detail: surface.show_startup(current_size, detail),
        )
    except KeyboardInterrupt:
        surface.close()
        recorder.finish("startup_failed", SshDisplayStats())
        print("\nrailmux ssh: cancelled during remote setup", file=sys.stderr)
        return 130
    except BaseException:
        surface.close()
        recorder.finish("startup_failed", SshDisplayStats())
        raise
    recorder.mark_attached()
    surface.show_startup(current_size, "Waiting for the first frame…")
    local_size = current_size
    decoder = ServerMessageDecoder()
    model = ScreenModel()
    terminal_input = TerminalInputDecoder()
    history = LocalHistoryView(history_limit)
    selection = LocalTextSelection()
    touch_keyboard = TermuxTouchKeyboard(
        enabled=not args.no_mouse and termux_touch
    )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout.fileno(), selectors.EVENT_READ, "remote")
    selector.register(sys.stdin.fileno(), selectors.EVENT_READ, "local")
    started = time.monotonic()
    first_frame_deadline: float | None = started + _FIRST_FRAME_TIMEOUT
    next_history_prefetch = started
    next_heartbeat = started + _HEARTBEAT_INTERVAL
    frames = 0
    frames_since_attach = 0
    painted_rows = 0
    wire_bytes = 0
    keyframes = 0
    patches = 0
    first_frame_ms: int | None = None
    reconnect_metrics = {"attempts": 0, "successes": 0}
    local_exit = False
    reconnect_cancel_code: int | None = None
    remote_closed = False
    awaiting_keyframe = False
    latest_screen: AppliedScreen | None = None
    route_refresh_needed = False
    history_info_until: float | None = None
    selection_clear_at: float | None = None
    claude_history_prompt_input: bytes | None = None
    claude_history_pending_choice: tuple[str, bool, bytes] | None = None
    claude_history_pending_since: float | None = None
    claude_history_prompt_mouse_button: int | None = None
    claude_history_runtime_choice: str | None = None
    path_request_id = 0
    pending_path_open: dict[int, _PendingPathOpen] = {}
    pending_path_actions: dict[
        int, tuple[_PendingPathOpen, PathResult, str]
    ] = {}
    path_open_prompt: tuple[
        int, _PendingPathOpen, PathResult
    ] | None = None
    path_open_prompt_mouse_button: int | None = None
    local_status_mouse_button: int | None = None
    periodic_prefetch = PeriodicPrefetchGate()

    def send_protocol_frame(frame: bytes) -> None:
        nonlocal remote_closed
        if remote_closed:
            return
        try:
            process.stdin.write(frame)
            process.stdin.flush()
        except BrokenPipeError:
            remote_closed = True

    def show_open_result(result: local_open.OpenResult) -> None:
        nonlocal history_info_until
        if result.copy_data is not None:
            surface.copy_to_clipboard(result.copy_data)
        surface.show_local_status(result.message, level=result.level)
        history_info_until = time.monotonic() + _HISTORY_INFO_SECONDS

    def begin_path_open(target: ClickTarget) -> None:
        nonlocal path_request_id, history_info_until
        path_request_id = (path_request_id + 1) & 0xFFFFFFFF
        if path_request_id == 0:
            path_request_id = 1
        pending_path_open.clear()
        pending_path_open[path_request_id] = _PendingPathOpen(
            target,
            time.monotonic(),
        )
        send_protocol_frame(
            encode_path_request(path_request_id, target.pane_id, target.value)
        )
        surface.show_local_status("Checking remote path…")
        history_info_until = None

    def choose_path_open(
        request_id: int,
        pending: _PendingPathOpen,
        resolved: PathResult,
        policy: str,
        persistent: bool,
    ) -> None:
        nonlocal history_info_until
        pending = _PendingPathOpen(pending.target, time.monotonic())
        pending_path_actions[request_id] = (pending, resolved, policy)
        send_protocol_frame(encode_path_open_request(
            request_id,
            pending.target.pane_id,
            pending.target.value,
            policy=policy,
            persistent=persistent,
            line=pending.target.line,
            column=pending.target.column,
        ))
        scope = "Saving" if persistent else "Using"
        destination = (
            "managed Vim" if policy == "internal" else "separate terminal"
        )
        surface.show_local_status(f"{scope} {destination}…")
        history_info_until = None

    def redraw_local_prompt() -> None:
        if path_open_prompt is not None:
            surface.show_path_open_prompt()
        elif claude_history_prompt_input is not None:
            surface.show_claude_history_prompt()

    def reassert_remote_focus() -> None:
        """Keep an agent focused across Termux's soft-keyboard view handoff."""
        frame = focus_in_frame_for_screen(latest_screen)
        if frame is not None:
            send_protocol_frame(frame)

    def apply_history_action(
        action: HistoryAction,
        deferred_paint: _DeferredHistoryPaint | None = None,
    ) -> None:
        nonlocal route_refresh_needed
        nonlocal history_info_until, claude_history_prompt_input
        overlays = history.overlays()
        if deferred_paint is not None:
            deferred_paint.defer(action)
        else:
            if action.restore_live and latest_screen is not None:
                surface.paint(
                    full_repaint(latest_screen),
                    overlays,
                    selection.segments(),
                )
            elif action.render_history and latest_screen is not None:
                surface.paint_overlays(
                    latest_screen,
                    overlays,
                    selection.segments(),
                )
        if action.protocol_frame:
            send_protocol_frame(action.protocol_frame)
        if action.forwarded_input:
            send_protocol_frame(encode_input(action.forwarded_input))
        if action.refresh_routes:
            route_refresh_needed = True
        if action.info_message:
            surface.show_local_status(action.info_message)
            history_info_until = time.monotonic() + _HISTORY_INFO_SECONDS
        if action.claude_history_prompt:
            claude_history_prompt_input = action.claude_history_prompt
            surface.show_claude_history_prompt()
            history_info_until = None

    def handle_terminal_part(
        part: bytes | SgrMouseEvent,
        input_batch: _LocalInputBatch,
    ) -> None:
        nonlocal route_refresh_needed
        nonlocal claude_history_prompt_input, claude_history_pending_choice
        nonlocal claude_history_pending_since
        nonlocal claude_history_prompt_mouse_button
        nonlocal claude_history_runtime_choice
        nonlocal history_info_until
        nonlocal selection_clear_at
        nonlocal path_open_prompt, path_open_prompt_mouse_button
        nonlocal local_status_mouse_button
        if surface.dismiss_interruptible_local_status():
            history_info_until = None
            if latest_screen is not None:
                surface.paint(
                    full_repaint(latest_screen),
                    history.overlays(),
                    selection.segments(),
                )
        if isinstance(part, SgrMouseEvent):
            if local_status_mouse_button is not None and not part.pressed:
                suppress = part.button & 3 == local_status_mouse_button
                local_status_mouse_button = None
                if suppress:
                    return
            if (
                part.pressed
                and not part.button & (32 | 64)
                and part.button & 3 in (0, 2)
            ):
                if surface.copy_local_status_at(part):
                    history_info_until = (
                        time.monotonic() + _HISTORY_INFO_SECONDS
                    )
                    local_status_mouse_button = part.button & 3
                    return
        if isinstance(part, SgrMouseEvent) and part.is_hover_motion:
            # DECSET 1003 reports local pointer movement even without a
            # pressed button. Consume it locally: hover must never become SSH
            # input or disturb a modal choice.
            if (
                path_open_prompt is not None
                or claude_history_prompt_input is not None
            ):
                return
            displayed_height = (
                latest_screen.height
                if latest_screen is not None
                else current_size.lines
            )
            hover_event = surface.translate_mouse_event(
                part,
                logical_height=displayed_height,
            )
            focused_pane_id = (
                None
                if latest_screen is None
                else history.pane_id_at_position(
                    latest_screen.cursor_x,
                    latest_screen.cursor_y,
                )
            )
            source = (
                None
                if latest_screen is None
                else history.selection_source(
                    hover_event,
                    latest_screen.rows,
                    focused_pane_id=focused_pane_id,
                )
            )
            before = selection.segments()
            if selection.hover(hover_event, source) and latest_screen is not None:
                after = selection.segments()
                surface.paint_changed_rows(
                    latest_screen,
                    history.overlays(),
                    after,
                    selection_changed_rows(before, after),
                )
            return
        if path_open_prompt is not None:
            request_id, pending, resolved = path_open_prompt
            if isinstance(part, SgrMouseEvent):
                selected = surface.path_open_prompt_choice(part)
                if selected is None:
                    return
                path_open_prompt_mouse_button = part.button & 3
                policy, persistent = selected
            else:
                selected = {
                    b"1": ("internal", True),
                    b"2": ("internal", False),
                    b"3": ("external", True),
                    b"4": ("external", False),
                }.get(part)
                if selected is None:
                    if part == b"\x1b":
                        path_open_prompt = None
                        pending_path_open.pop(request_id, None)
                        if latest_screen is not None:
                            surface.paint(
                                full_repaint(latest_screen),
                                history.overlays(),
                                selection.segments(),
                            )
                        return
                    surface.show_path_open_prompt()
                    return
                policy, persistent = selected
            path_open_prompt = None
            pending_path_open.pop(request_id, None)
            if latest_screen is not None:
                surface.paint(
                    full_repaint(latest_screen),
                    history.overlays(),
                    selection.segments(),
                )
            choose_path_open(
                request_id,
                pending,
                resolved,
                policy,
                persistent,
            )
            return
        if isinstance(part, SgrMouseEvent) and (
            path_open_prompt_mouse_button is not None
        ):
            suppress = (
                not part.pressed
                and part.button & 3 == path_open_prompt_mouse_button
            )
            path_open_prompt_mouse_button = None
            if suppress:
                return
        if claude_history_prompt_input is not None:
            if isinstance(part, SgrMouseEvent):
                selected = surface.claude_history_prompt_choice(part)
                if selected is None:
                    return
                claude_history_prompt_mouse_button = part.button & 3
                policy, persistent = selected
            else:
                choices = {
                    b"1": ("local", True),
                    b"2": ("local", False),
                    b"3": ("native", True),
                    b"4": ("native", False),
                }
                selected = choices.get(part)
                if selected is None:
                    if part == b"\x1b":
                        pending_wheel = claude_history_prompt_input
                        claude_history_prompt_input = None
                        if latest_screen is not None:
                            surface.paint(
                                full_repaint(latest_screen),
                                history.overlays(),
                            )
                        return
                    surface.show_claude_history_prompt()
                    return
                policy, persistent = selected
            if selected is not None:
                pending_wheel = claude_history_prompt_input
                claude_history_prompt_input = None
                if latest_screen is not None:
                    surface.paint(full_repaint(latest_screen), history.overlays())
                claude_history_pending_choice = (policy, persistent, pending_wheel)
                claude_history_pending_since = time.monotonic()
                send_protocol_frame(
                    encode_claude_history_policy(policy, persistent=persistent)
                )
                action = "Saving" if persistent else "Enabling"
                label = "smooth local" if policy == "local" else "Claude native"
                surface.show_local_status(f"{action} {label} history…")
                return
        if isinstance(part, SgrMouseEvent) and (
            claude_history_prompt_mouse_button is not None
        ):
            suppress = (
                not part.pressed
                and part.button & 3 == claude_history_prompt_mouse_button
            )
            claude_history_prompt_mouse_button = None
            if suppress:
                return
        if (
            claude_history_pending_choice is not None
            and isinstance(part, SgrMouseEvent)
            and part.wheel_direction != 0
        ):
            # The helper normally acknowledges in the next ordered packet.
            # Do not reopen the ask route or forward a second wheel while that
            # one persistent choice is still being confirmed.
            return
        if isinstance(part, SgrMouseEvent):
            selection_before = selection.segments()
            displayed_height = (
                latest_screen.height
                if latest_screen is not None
                else current_size.lines
            )
            part = surface.translate_mouse_event(part, logical_height=displayed_height)
            # Keep a frozen viewport stable across reported clicks and drags.
            # Terminal-native selection overrides never arrive here.
            focused_pane_id = (
                None
                if latest_screen is None
                else history.pane_id_at_position(
                    latest_screen.cursor_x, latest_screen.cursor_y
                )
            )
            status_row = (
                compact_status_row(latest_screen)
                if latest_screen is not None
                else None
            )
            touch_action = termux_prompt_touch_action(
                touch_keyboard,
                part,
                history,
                latest_screen,
                now=time.monotonic(),
            )
            if touch_action.suspend_mouse:
                selection.cancel()
                selection_clear_at = None
                history_info_until = None
                surface.suspend_mouse()
                if touch_action.show_hint:
                    now = time.monotonic()
                    surface.show_local_status(
                        _TERMUX_TOUCH_HINT,
                        interruptible=True,
                        expires_at=now + _HISTORY_INFO_SECONDS,
                    )
            if touch_action.handled:
                return
            selection_action = selection.pointer_event(
                part,
                (
                    None
                    if latest_screen is None
                    else history.selection_source(
                        part,
                        latest_screen.rows,
                        focused_pane_id=focused_pane_id,
                    )
                ),
            )
            if selection.capturing or not selection.active:
                selection_clear_at = None
            if selection_action.repaint and latest_screen is not None:
                selection_after = selection.segments()
                surface.paint_changed_rows(
                    latest_screen,
                    history.overlays(),
                    selection_after,
                    selection_changed_rows(selection_before, selection_after),
                )
            for replay_event in selection_action.replay_events:
                replay_action = history.pointer_event(
                    replay_event,
                    focused_pane_id,
                    status_row=status_row,
                    now=time.monotonic(),
                )
                replay_action, deferred_paint = input_batch.prepare(
                    replay_event,
                    replay_action,
                )
                if replay_action is not None:
                    apply_history_action(replay_action, deferred_paint)
            if selection_action.copy_data is not None:
                surface.copy_to_clipboard(selection_action.copy_data)
                character_count = len(
                    selection_action.copy_data.decode("utf-8", errors="replace")
                )
                surface.show_local_status(
                    f"Copied {character_count:,} chars.",
                    level="success",
                )
                history_info_until = time.monotonic() + _HISTORY_INFO_SECONDS
                selection_clear_at = time.monotonic() + _SELECTION_HIGHLIGHT_SECONDS
            if selection_action.open_target is not None:
                target = selection_action.open_target
                flash_before = selection.segments()
                if (
                    selection.flash(
                        target,
                        now=time.monotonic(),
                        duration=_CLICK_FLASH_SECONDS,
                    )
                    and latest_screen is not None
                ):
                    flash_after = selection.segments()
                    surface.paint_changed_rows(
                        latest_screen,
                        history.overlays(),
                        flash_after,
                        selection_changed_rows(flash_before, flash_after),
                    )
                if target.kind == "url":
                    show_open_result(local_open.open_url(target.value))
                else:
                    begin_path_open(target)
            if selection_action.handled:
                return
            action = history.pointer_event(
                part,
                focused_pane_id,
                status_row=status_row,
                now=time.monotonic(),
            )
            action, deferred_paint = input_batch.prepare(part, action)
            if action is not None:
                apply_history_action(action, deferred_paint)
            return
        if not part:
            return
        if touch_keyboard.consumes_focus_out(part):
            # Opening Android's input view can briefly remove focus from the
            # terminal View even though the user is still interacting with
            # this SSH client. Forwarding that synthetic focus-out makes
            # Codex/Claude hide their prompt cursor until Android happens to
            # emit another focus-in. The projection/input transition below
            # reasserts the authoritative focus-in instead.
            return
        if (
            part not in (b"\x1b[I", b"\x1b[O")
            and touch_keyboard.keyboard_input()
        ):
            surface.resume_mouse()
            reassert_remote_focus()
            if latest_screen is not None:
                surface.paint(
                    full_repaint(latest_screen),
                    history.overlays(),
                    selection.segments(),
                )
        selection_clear_at = None
        if selection.cancel() and latest_screen is not None:
            surface.paint(
                full_repaint(latest_screen),
                history.overlays(),
            )
        if (
            latest_screen is not None
            and page_key_direction(part)
            and history.pane_id_at_position(
                latest_screen.cursor_x,
                latest_screen.cursor_y,
            )
            is not None
        ):
            apply_history_action(
                history.page(
                    part,
                    latest_screen.cursor_x,
                    latest_screen.cursor_y,
                    now=time.monotonic(),
                )
            )
            return
        may_change_routes = screen_input_may_change_routes(
            part,
            history,
            latest_screen,
        )
        if history.active or history.pending:
            if part == b"\x1b":
                restore = history.cancel()
                apply_history_action(HistoryAction(restore_live=restore))
                return
            if part not in (b"\x1b[I", b"\x1b[O"):
                if may_change_routes:
                    restore = history.invalidate_routes()
                    route_refresh_needed = True
                elif latest_screen is not None:
                    restore = history.cancel_for_input(
                        latest_screen.cursor_x, latest_screen.cursor_y
                    )
                else:
                    restore = history.cancel()
                apply_history_action(
                    HistoryAction(
                        forwarded_input=part,
                        restore_live=restore,
                    )
                )
                return
        if may_change_routes:
            history.invalidate_routes()
            route_refresh_needed = True
        if termux_touch and part == b"\033[I":
            # Android may restore the terminal View with its cursor a few
            # cells away from the last painted application cursor. A remote
            # patch would correct it, but an idle provider can otherwise leave
            # the two cursor positions visibly alternating until the user
            # types. Reassert only the cached cursor on Termux focus-in; the
            # remote focus event remains authoritative and is still forwarded.
            surface.reassert_cursor()
        send_protocol_frame(encode_input(part))

    surface.show_local_status(
        "Ctrl-] disconnects · Ctrl-B d detaches · "
        f"mouse/copy {'off' if args.no_mouse else 'on'} · "
        f"reconnect {'on' if args.reconnect else 'off'}",
        interruptible=True,
    )
    history_info_until = time.monotonic() + _HISTORY_INFO_SECONDS
    try:
        with RawTerminal(sys.stdin.fileno()):
            while True:
                observed_size = os.get_terminal_size(sys.stdout.fileno())
                if observed_size != local_size:
                    selection.cancel()
                    selection_clear_at = None
                    if _terminal_size_exceeds_limits(observed_size):
                        raise ProbeError(
                            "resized terminal reports "
                            f"{observed_size.columns}x{observed_size.lines}; "
                            "SSH display limits are "
                            f"{_MAX_TERMINAL_COLUMNS}x{_MAX_TERMINAL_LINES}"
                        )
                    if _is_soft_keyboard_projection(observed_size, current_size):
                        if touch_keyboard.observe_projection(True):
                            surface.resume_mouse()
                            reassert_remote_focus()
                        surface.set_physical_size(observed_size)
                        local_size = observed_size
                        if latest_screen is not None:
                            surface.paint(
                                full_repaint(latest_screen),
                                history.overlays(),
                                selection.segments(),
                            )
                            redraw_local_prompt()
                            if (
                                path_open_prompt is None
                                and claude_history_prompt_input is None
                                and claude_history_pending_choice is not None
                            ):
                                label = (
                                    "smooth local"
                                    if claude_history_pending_choice[0] == "local"
                                    else "Claude native"
                                )
                                action = (
                                    "Saving"
                                    if claude_history_pending_choice[1]
                                    else "Enabling"
                                )
                                surface.show_local_status(f"{action} {label} history…")
                    elif not _terminal_size_is_usable(observed_size):
                        raise ProbeError(
                            "resized terminal reports "
                            f"{observed_size.columns}x{observed_size.lines}; "
                            "the minimum is "
                            f"{_MIN_TERMINAL_COLUMNS}x"
                            f"{_MIN_TERMINAL_LINES}"
                        )
                    elif observed_size == current_size:
                        if touch_keyboard.observe_projection(False):
                            surface.resume_mouse(reassert=True)
                            reassert_remote_focus()
                        # The soft keyboard closed. Restore the complete
                        # logical screen even if no remote patch is pending.
                        surface.set_physical_size(observed_size)
                        local_size = observed_size
                        if latest_screen is not None:
                            surface.paint(
                                full_repaint(latest_screen),
                                history.overlays(),
                                selection.segments(),
                            )
                            redraw_local_prompt()
                    else:
                        keyboard_projected = touch_keyboard.keyboard_projected
                        if touch_keyboard.observe_projection(False):
                            surface.resume_mouse(reassert=True)
                            reassert_remote_focus()
                        elif touch_keyboard.cancel():
                            surface.resume_mouse(reassert=keyboard_projected)
                            reassert_remote_focus()
                        surface.set_physical_size(observed_size)
                        local_size = observed_size
                        if history.active and latest_screen is not None:
                            surface.paint(
                                full_repaint(latest_screen),
                                selection=selection.segments(),
                            )
                        redraw_local_prompt()
                        history.clear_cache()
                        route_refresh_needed = True
                        send_protocol_frame(
                            encode_resize(observed_size.columns, observed_size.lines)
                        )
                        current_size = observed_size
                        awaiting_keyframe = True
                events = selector.select(timeout=terminal_input.next_timeout())
                for key, _mask in events:
                    if key.data == "remote":
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            remote_closed = True
                            break
                        wire_bytes += len(chunk)
                        saw_screen_update = False
                        for message in decoder.feed(chunk):
                            if isinstance(message, ClipboardCopy):
                                surface.copy_to_clipboard(message.data)
                                continue
                            if isinstance(message, PathResult):
                                pending = pending_path_open.pop(
                                    message.request_id,
                                    None,
                                )
                                if pending is None:
                                    continue
                                if message.kind is PathKind.UNAVAILABLE:
                                    show_open_result(local_open.OpenResult(
                                        False,
                                        "Path is not available in this remote workspace",
                                        "warning",
                                    ))
                                    continue
                                if message.policy == "ask":
                                    pending_path_open[message.request_id] = pending
                                    path_open_prompt = (
                                        message.request_id,
                                        pending,
                                        message,
                                    )
                                    surface.show_path_open_prompt()
                                    history_info_until = None
                                else:
                                    choose_path_open(
                                        message.request_id,
                                        pending,
                                        message,
                                        message.policy,
                                        False,
                                    )
                                continue
                            if isinstance(message, PathOpenResult):
                                action = pending_path_actions.pop(
                                    message.request_id,
                                    None,
                                )
                                if action is None:
                                    continue
                                pending, resolved, policy = action
                                if message.applied and policy == "external":
                                    show_open_result(local_open.open_remote_path(
                                        args.destination,
                                        ssh_args=args.ssh_arg,
                                        path=resolved.path,
                                        directory=(
                                            resolved.kind is PathKind.DIRECTORY
                                        ),
                                        regular_file=(
                                            resolved.kind is PathKind.FILE
                                        ),
                                        line=pending.target.line,
                                        column=pending.target.column,
                                    ))
                                else:
                                    surface.show_local_status(
                                        message.message,
                                        level=message.level,
                                    )
                                    history_info_until = (
                                        time.monotonic() + _HISTORY_INFO_SECONDS
                                    )
                                continue
                            if isinstance(message, HistoryBatch):
                                accepted_prefetch_id = history.prefetch_pending_id
                                action = history.accept_prefetch(message)
                                periodic_prefetch.accepted(
                                    message.request_id,
                                    accepted_prefetch_id,
                                )
                                selection_changed = selection.validate_routes(
                                    history.visible_routes
                                )
                                if selection_changed:
                                    selection_clear_at = None
                                apply_history_action(action)
                                if selection_changed and latest_screen is not None:
                                    surface.paint(
                                        full_repaint(latest_screen),
                                        history.overlays(),
                                    )
                                continue
                            if isinstance(message, HistorySnapshot):
                                apply_history_action(history.accept(message))
                                continue
                            if isinstance(message, ClaudeHistoryPolicyResult):
                                pending = claude_history_pending_choice
                                action = apply_claude_history_policy_result(
                                    pending, message
                                )
                                if action is None:
                                    continue
                                claude_history_pending_choice = None
                                claude_history_pending_since = None
                                if action.status_text is not None:
                                    surface.show_local_status(action.status_text)
                                if not action.update_runtime:
                                    history_info_until = (
                                        time.monotonic() + _HISTORY_INFO_SECONDS
                                    )
                                    continue
                                claude_history_runtime_choice = action.runtime_choice
                                if action.prefetch:
                                    prefetch = history.begin_prefetch(
                                        time.monotonic(), force=True
                                    )
                                    if prefetch:
                                        send_protocol_frame(prefetch)
                                        periodic_prefetch.sent(
                                            history.prefetch_pending_id
                                        )
                                route_refresh_needed = False
                                if action.forwarded_input:
                                    send_protocol_frame(
                                        encode_input(action.forwarded_input)
                                    )
                                    surface.show_local_status(
                                        "Claude native clickable history enabled"
                                    )
                                history_info_until = (
                                    time.monotonic() + _HISTORY_INFO_SECONDS
                                )
                                continue
                            update = message
                            applied = model.apply(update, current_size)
                            if applied is None:
                                if not awaiting_keyframe:
                                    send_protocol_frame(encode_keyframe_request())
                                    awaiting_keyframe = True
                                continue
                            saw_screen_update = True
                            periodic_prefetch.screen_updated()
                            if update.kind is UpdateKind.KEYFRAME:
                                awaiting_keyframe = False
                                keyframes += 1
                            else:
                                patches += 1
                            latest_screen = applied
                            focus_reporting_started = surface.paint(
                                applied,
                                history.overlays(),
                                selection.segments(),
                            )
                            redraw_local_prompt()
                            if focus_reporting_started:
                                # Enabling DECSET 1004 does not require a
                                # terminal to report its already-focused state.
                                # On SSH reconnect that can leave tmux and the
                                # active agent believing the client is still
                                # unfocused until the user changes windows.
                                send_protocol_frame(encode_input(b"\033[I"))
                            frames += 1
                            frames_since_attach += 1
                            first_frame_deadline = None
                            if first_frame_ms is None:
                                first_frame_ms = int(
                                    max(0.0, time.monotonic() - diagnostic_started)
                                    * 1000
                                )
                            painted_rows += len(applied.changed_rows)
                        if saw_screen_update and route_refresh_needed:
                            prefetch = history.begin_prefetch(time.monotonic())
                            if prefetch:
                                send_protocol_frame(prefetch)
                                periodic_prefetch.sent(history.prefetch_pending_id)
                            if history.prefetch_pending_id is not None:
                                route_refresh_needed = False
                                next_history_prefetch = (
                                    time.monotonic() + _HISTORY_PREFETCH_INTERVAL
                                )
                    else:
                        data = os.read(sys.stdin.fileno(), 4096)
                        if not data:
                            local_exit = True
                            break
                        data, emergency_exit = split_local_escape(data)
                        if emergency_exit:
                            local_exit = True
                        input_batch = _LocalInputBatch()
                        for part in terminal_input.feed(data):
                            if isinstance(part, bytes):
                                for key_part in split_page_key_input(part):
                                    handle_terminal_part(key_part, input_batch)
                            else:
                                handle_terminal_part(part, input_batch)
                        input_batch.flush(
                            surface,
                            latest_screen,
                            history.overlays(),
                            selection.segments(),
                        )
                        if local_exit:
                            break
                if not local_exit:
                    for part in terminal_input.flush_pending():
                        input_batch = _LocalInputBatch()
                        for key_part in split_page_key_input(part):
                            handle_terminal_part(key_part, input_batch)
                        input_batch.flush(
                            surface,
                            latest_screen,
                            history.overlays(),
                            selection.segments(),
                        )
                now = time.monotonic()
                keyboard_projected = touch_keyboard.keyboard_projected
                if touch_keyboard.expire(now):
                    surface.resume_mouse(reassert=keyboard_projected)
                    reassert_remote_focus()
                    if latest_screen is not None:
                        surface.paint(
                            full_repaint(latest_screen),
                            history.overlays(),
                            selection.segments(),
                        )
                        redraw_local_prompt()
                if touch_keyboard.post_close_reassert_due(now):
                    # Termux can finish transferring native touch ownership
                    # just after its close SIGWINCH. Reassert once after that
                    # boundary as well as immediately in the resize branch.
                    surface.resume_mouse(reassert=True)
                expired_paths = tuple(
                    request_id
                    for request_id, pending in pending_path_open.items()
                    if now - pending.requested_at >= _PATH_OPEN_TIMEOUT
                    and (
                        path_open_prompt is None
                        or path_open_prompt[0] != request_id
                    )
                )
                if expired_paths:
                    for request_id in expired_paths:
                        pending_path_open.pop(request_id, None)
                    show_open_result(local_open.OpenResult(
                        False,
                        "Remote path check timed out",
                        "warning",
                    ))
                expired_actions = tuple(
                    request_id
                    for request_id, (pending, _resolved, _policy)
                    in pending_path_actions.items()
                    if now - pending.requested_at >= _PATH_OPEN_TIMEOUT
                )
                if expired_actions:
                    for request_id in expired_actions:
                        pending_path_actions.pop(request_id, None)
                    show_open_result(local_open.OpenResult(
                        False,
                        "Remote path open timed out",
                        "warning",
                    ))
                restore_local_status = surface.expire_local_status(now) or (
                    history_info_until is not None and now >= history_info_until
                )
                timed_selection_before = selection.segments()
                clear_selection = (
                    selection_clear_at is not None and now >= selection_clear_at
                )
                clear_click_flash = selection.clear_expired_flash(now)
                if restore_local_status or clear_selection or clear_click_flash:
                    if clear_selection:
                        selection.cancel()
                    if restore_local_status:
                        surface.clear_local_status()
                    timed_selection_after = selection.segments()
                    if latest_screen is not None and restore_local_status:
                        surface.paint(
                            full_repaint(latest_screen),
                            history.overlays(),
                            timed_selection_after,
                        )
                        redraw_local_prompt()
                    elif latest_screen is not None:
                        surface.paint_changed_rows(
                            latest_screen,
                            history.overlays(),
                            timed_selection_after,
                            selection_changed_rows(
                                timed_selection_before,
                                timed_selection_after,
                            ),
                        )
                    if restore_local_status:
                        history_info_until = None
                    if clear_selection:
                        selection_clear_at = None
                if (
                    claude_history_pending_choice is not None
                    and claude_history_save_timed_out(claude_history_pending_since, now)
                ):
                    claude_history_pending_choice = None
                    claude_history_pending_since = None
                    prefetch = history.begin_prefetch(now, force=True)
                    if prefetch:
                        send_protocol_frame(prefetch)
                        periodic_prefetch.sent(history.prefetch_pending_id)
                    surface.show_local_status(
                        "Claude history save confirmation timed out; "
                        "refreshing remote policy"
                    )
                    history_info_until = now + _HISTORY_INFO_SECONDS
                if local_exit:
                    break
                connection_ended = remote_closed or (
                    process.poll() is not None and not events
                )
                if connection_ended:
                    if touch_keyboard.cancel():
                        surface.resume_mouse()
                    old_fd = process.stdout.fileno()
                    _reap_remote(process)
                    if should_automatically_reconnect(
                        enabled=args.reconnect,
                        painted_frames=frames_since_attach,
                        local_exit=local_exit,
                        returncode=process.returncode,
                    ):
                        selector.unregister(old_fd)
                        for stream in (process.stdin, process.stdout):
                            try:
                                stream.close()
                            except OSError:
                                pass
                        try:
                            process = _automatic_reconnect(
                                args,
                                current_size,
                                surface,
                                sys.stdin.fileno(),
                                reconnect_metrics,
                            )
                        except ReconnectCancelled as exc:
                            reconnect_cancel_code = exc.exit_code
                            local_exit = True
                            break
                        selector.register(
                            process.stdout.fileno(),
                            selectors.EVENT_READ,
                            "remote",
                        )
                        decoder = ServerMessageDecoder()
                        model = ScreenModel()
                        terminal_input = TerminalInputDecoder()
                        history.mark_reconnected()
                        # The retained pixels and history cache are useful as
                        # reconnect feedback, but neither is input authority
                        # for the replacement helper.  Do not route a wheel
                        # against the old cursor/screen or prefetch against
                        # stale geometry before the new keyframe arrives.
                        latest_screen = None
                        selection = LocalTextSelection()
                        history_info_until = None
                        selection_clear_at = None
                        pending_path_open.clear()
                        pending_path_actions.clear()
                        path_open_prompt = None
                        path_open_prompt_mouse_button = None
                        claude_history_prompt_input = None
                        claude_history_pending_choice = None
                        claude_history_pending_since = None
                        # Mouse press/release is one local terminal gesture.
                        # Preserve a consumed press across remote reconnect so
                        # its release cannot leak into the replacement PTY.
                        remote_closed = False
                        awaiting_keyframe = False
                        # The first accepted screen immediately refreshes
                        # visible pane routes instead of making the user spend
                        # and lose one wheel tick to discover them again.
                        route_refresh_needed = True
                        reconnect_policy = claude_history_reconnect_frame(
                            claude_history_runtime_choice
                        )
                        if reconnect_policy:
                            send_protocol_frame(reconnect_policy)
                        now = time.monotonic()
                        first_frame_deadline = now + _FIRST_FRAME_TIMEOUT
                        next_history_prefetch = now
                        next_heartbeat = now + _HEARTBEAT_INTERVAL
                        frames_since_attach = 0
                        reconnect_metrics["successes"] += 1
                        periodic_prefetch.reset()
                        surface.show_local_status(
                            "Reconnected; waiting for a fresh screen"
                        )
                        continue
                    break
                now = time.monotonic()
                if first_frame_timed_out(first_frame_deadline, now):
                    raise ProbeError(
                        "timed out waiting for the remote display's first frame; "
                        "the Railmux session and agents were left intact"
                    )
                if now >= next_heartbeat:
                    send_protocol_frame(encode_heartbeat())
                    next_heartbeat = now + _HEARTBEAT_INTERVAL
                if (
                    not args.no_mouse
                    and latest_screen is not None
                    and now >= next_history_prefetch
                ):
                    if periodic_prefetch.should_request():
                        prefetch = history.begin_prefetch(now)
                        if prefetch:
                            send_protocol_frame(prefetch)
                            route_refresh_needed = False
                            periodic_prefetch.sent(history.prefetch_pending_id)
                    next_history_prefetch = now + _HISTORY_PREFETCH_INTERVAL
    except KeyboardInterrupt:
        # Raw mode normally forwards Ctrl-C. This only handles an external
        # signal and follows the conventional shell exit status.
        return 130
    finally:
        selector.close()
        surface.close()
        _reap_remote(process, terminate=local_exit)
        history_metrics = history.metrics
        if local_exit:
            diagnostic_outcome = "local_disconnect"
        elif process.returncode == int(RemoteExit.DETACHED):
            diagnostic_outcome = "remote_detach"
        elif process.returncode == int(RemoteExit.SOFT_QUIT):
            diagnostic_outcome = "remote_soft_quit"
        elif process.returncode == int(RemoteExit.HARD_QUIT):
            diagnostic_outcome = "remote_hard_quit"
        elif process.returncode:
            diagnostic_outcome = "transport_failed"
        else:
            diagnostic_outcome = "connected"
        recorder.finish(
            diagnostic_outcome,
            SshDisplayStats(
                reached_first_frame=frames > 0,
                first_frame_ms=first_frame_ms,
                frames=frames,
                keyframes=keyframes,
                patches=patches,
                painted_rows=painted_rows,
                wire_bytes=wire_bytes,
                reconnect_attempts=reconnect_metrics["attempts"],
                reconnect_successes=reconnect_metrics["successes"],
                history_prefetch_requests=history_metrics.prefetch_requests,
                history_deep_requests=history_metrics.deep_requests,
                history_timeouts=history_metrics.timeouts,
                history_anchor_rejects=history_metrics.anchor_rejects,
            ),
        )

    elapsed = max(0.001, time.monotonic() - started)
    print(
        f"railmux ssh: painted {frames} coalesced updates / "
        f"{painted_rows} rows in {elapsed:.1f}s; "
        f"received {wire_bytes / 1024:.1f} KiB",
        file=sys.stderr,
    )
    if reconnect_cancel_code is not None:
        print("railmux ssh: automatic reconnect cancelled", file=sys.stderr)
        return reconnect_cancel_code
    if process.returncode in _KNOWN_REMOTE_EXITS:
        print(
            f"railmux ssh: {_KNOWN_REMOTE_EXITS[process.returncode]}",
            file=sys.stderr,
        )
        return 0
    if frames == 0 and not local_exit:
        raise ProbeError("remote display helper exited before its first frame")
    if not local_exit and process.returncode:
        print(
            "railmux ssh: remote display failed; run 'railmux doctor' on "
            "the remote host for tmux health and the last recorded incident",
            file=sys.stderr,
        )
        return process.returncode
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return run(parse_args(argv))
    except ProbeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
