"""Managed-Windows PTY clients and cross-session byte relay.

Windows OpenSSH and an interactive Windows desktop can run under different
Terminal Services sessions.  MSYS2 can keep tmux's AF_UNIX control socket
reachable across that boundary while failing to transfer the later client's
terminal handle.  This module asks the already-validated tmux server to spawn
one helper in its own Windows session.  That helper owns the real tmux PTY;
the helper forwards opaque bytes and resize messages. A supported Windows
Terminal entry consumes those bytes into Railmux's shared semantic screen
model before painting; it does not interpret Railmux panes or providers.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import hmac
import os
import re
import secrets
import select
import selectors
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import termios
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

from railmux import restart_state, tmux_server
from railmux.config import ConfigError, load_config
from railmux.fast_display_client import (
    ScreenModel,
    TerminalSurface,
    compact_status_row,
    screen_input_may_change_routes,
)
from railmux.fast_display_history import (
    HistoryAction,
    HistoryCaptureJob,
    HistoryCaptureWorker,
    LocalHistoryView,
    PeriodicPrefetchGate,
)
from railmux.setting_contracts import HISTORY_DEFAULT_LINES
from railmux.fast_display_input import (
    BracketedPasteInput,
    ClickTarget,
    LocalTextSelection,
    SelectionSource,
    SgrMouseEvent,
    TerminalInputDecoder,
    page_key_direction,
    split_page_key_input,
)
from railmux.fast_display_protocol import (
    HistoryBatch,
    HistorySnapshot,
    InputFrameDecoder,
    InputKind,
    PathKind,
    PathResult,
    decode_history_prefetch,
    decode_history_request,
)
from railmux import local_open
from railmux.provider_paths import running_in_managed_windows_wrapper
from railmux.terminal_screen import ScreenProducer


_PROTOCOL_MAGIC = b"RMUX-WPTY-1\0"
_TOKEN_BYTES = 16
_HEADER = struct.Struct(">BI")
_MAX_FRAME_BYTES = 1024 * 1024
_TYPE_INPUT = 1
_TYPE_OUTPUT = 2
_TYPE_RESIZE = 3
_TYPE_HEARTBEAT = 4
_TYPE_EXIT = 5
_TYPE_CLOSE = 6
_CONNECT_TIMEOUT = 5.0
_SEND_TIMEOUT = 5.0
_HEARTBEAT_INTERVAL = 5.0
_HEARTBEAT_TIMEOUT = 45.0
_DRAIN_TIMEOUT = 0.25
_CHILD_EXIT_GRACE = 0.2
_PTY_INPUT_TIMEOUT = 5.0
_STALE_ENDPOINT_AGE = 5 * 60
_MAX_STALE_ENDPOINTS = 64
_LOCAL_FRAME_INTERVAL = 1.0 / 30.0
_LOCAL_HISTORY_PREFETCH_INTERVAL = 3.0
_SYNCHRONIZED_UPDATE_MAX_HOLD = 0.25
# Consume a busy PTY into the latest semantic screen before painting it.  The
# time budget keeps terminal input responsive; the byte budget bounds one
# pump even when a producer can outrun the parser indefinitely.
_LOCAL_PTY_DRAIN_BUDGET = 0.01
_LOCAL_PTY_DRAIN_BYTES = 4 * 1024 * 1024
# A producer which never lets the PTY reach EAGAIN must still make visible
# progress.  This is deliberately slower than the normal 30 fps cadence so a
# restore burst remains heavily coalesced without freezing an endless stream.
_LOCAL_PTY_MAX_PAINT_STALENESS = 0.2
# A restore can briefly reach EAGAIN between complete application frames. Once
# output reaches roughly one physical screen, keep treating those short gaps as
# one catch-up transaction instead of painting a viewport-by-viewport replay.
# Normal small Working ticks never enter this gate, and any terminal input
# leaves it immediately.
_LOCAL_PTY_CATCHUP_WINDOW = 0.15
_LOCAL_PTY_CATCHUP_QUIET = 0.12
_LOCAL_PTY_CATCHUP_MAX_STALENESS = 0.75
_LOCAL_PTY_CATCHUP_MIN_BYTES = 1024
_TERMINAL_WRITER_POLL = 0.01
_TERMINAL_WRITER_CLOSE_TIMEOUT = 5.0
_TERMINAL_WRITER_MAX_QUEUE = 8
_RELAY_NAME = re.compile(r"windows-attach-[0-9a-f]{16}\.sock\Z")


def _terminal_events_first(events):
    """Order one selector batch so input guards same-batch PTY output."""
    return sorted(events, key=lambda event: event[0].data != "terminal")


class _TerminalFdWriter:
    """Single-flight terminal writer that keeps the client loop responsive.

    TerminalSurface can issue a small setup write followed by one synchronized
    screen frame.  A dedicated worker preserves that exact order while a slow
    ConPTY consumer cannot block tmux health probes, PTY draining, or input.
    The semantic renderer will not advance its diff base while any payload is
    queued or being written, so no later patch can depend on a skipped frame.
    """

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self._condition = threading.Condition()
        self._queue: list[bytes] = []
        self._writing = False
        self._closed = False
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="railmux-terminal-writer",
            # If close has to defer the restore payload behind one blocked OS
            # write, process shutdown must not discard that final reset.
            daemon=False,
        )
        self._thread.start()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._closed:
                    self._condition.wait()
                if self._closed and not self._queue:
                    return
                payload = self._queue.pop(0)
                self._writing = True
            try:
                _write_terminal_output(self.fd, payload)
            except BaseException as exc:
                with self._condition:
                    self._error = exc
                    self._queue.clear()
                    self._writing = False
                    self._closed = True
                    self._condition.notify_all()
                return
            with self._condition:
                self._writing = False
                self._condition.notify_all()

    def _raise_error_locked(self) -> None:
        if self._error is not None:
            raise WindowsAttachRelayError(
                "the physical terminal stopped accepting output"
            ) from self._error

    @property
    def idle(self) -> bool:
        with self._condition:
            self._raise_error_locked()
            return not self._queue and not self._writing

    def writable(self) -> bool:
        return True

    def write(self, payload: bytes) -> int:
        value = bytes(payload)
        with self._condition:
            self._raise_error_locked()
            if self._closed:
                raise WindowsAttachRelayError("the physical terminal writer is closed")
            if len(self._queue) >= _TERMINAL_WRITER_MAX_QUEUE:
                raise WindowsAttachRelayError(
                    "the physical terminal output queue exceeded its bound"
                )
            self._queue.append(value)
            self._condition.notify()
        return len(payload)

    def flush(self) -> None:
        return None

    def wait_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._queue or self._writing:
                self._raise_error_locked()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            self._raise_error_locked()
            return True

    def close(
        self,
        timeout: float = _TERMINAL_WRITER_CLOSE_TIMEOUT,
        *,
        final_payload: bytes | None = None,
    ) -> bool:
        """Stop writes and report a deferred ordered final payload.

        Cleanup must never mask the renderer error which initiated it or skip
        the launcher's terminal restoration. If an OS write remains blocked,
        discard every stale not-yet-started payload and retain only the reset
        supplied by the caller. The non-daemon worker then preserves ordering:
        the in-flight frame finishes before the reset, never after it.
        """
        try:
            idle = self.wait_idle(timeout)
        except (OSError, WindowsAttachRelayError):
            idle = False
        with self._condition:
            self._closed = True
            if not idle:
                self._queue.clear()
                if (
                    final_payload
                    and self._error is None
                    and self._thread.is_alive()
                ):
                    self._queue.append(bytes(final_payload))
            self._condition.notify_all()
        self._thread.join(timeout if idle else 0.0)
        return bool(final_payload and self._thread.is_alive())


class _SemanticTerminalRenderer:
    """Render only final changed rows from one local tmux PTY stream."""

    def __init__(self, stdout_fd: int, width: int, height: int) -> None:
        self.producer = ScreenProducer(width, height)
        self.model = ScreenModel()
        self.writer = _TerminalFdWriter(stdout_fd)
        self.surface = TerminalSurface(
            self.writer,
            synchronized_output=True,
        )
        self.surface.set_physical_size(os.terminal_size((width, height)))
        self._next_frame = 0.0
        self._synchronized_since: float | None = None
        self._synchronized_bypassed = False
        self._pending_clipboard: bytes | None = None
        self._pending_status: tuple[str, str] | None = None
        self._screen = None
        # ``ScreenModel`` advances when a frame is queued, but pointer hit
        # testing must advance only after that frame has actually reached the
        # physical terminal.  ConPTY can keep one write in flight long enough
        # for a pane split or resize to otherwise pair an old visible screen
        # with new tmux geometry.
        self._pending_screen = None
        self._presentation_serial = 0
        self._presentation_required_serial = 0
        self._selection: tuple[tuple[int, int, bytes], ...] = ()
        self._selection_dirty_rows: set[int] = set()
        self._history_overlays: (
            tuple[tuple[HistorySnapshot, tuple[bytes, ...]], ...]
        ) = ()
        self._path_prompt_visible = False
        self._path_prompt_needs_paint = False
        self._path_prompt_painted = False
        self._claude_history_prompt_visible = False
        self._claude_history_prompt_needs_paint = False
        self._claude_history_prompt_painted = False

    def _promote_presented_screen(self) -> None:
        if self._pending_screen is None or not self.writer.idle:
            return
        self._screen = self._pending_screen
        self._pending_screen = None
        self._presentation_serial += 1

    @property
    def screen(self):
        self._promote_presented_screen()
        return self._screen

    @property
    def presentation_serial(self) -> int:
        self._promote_presented_screen()
        return self._presentation_serial

    @property
    def presentation_stable(self) -> bool:
        self._promote_presented_screen()
        return bool(
            self._screen is not None
            and self._pending_screen is None
            and self.writer.idle
            and self._presentation_serial >= self._presentation_required_serial
        )

    def require_fresh_presentation(self) -> None:
        """Fail pointer input closed until one newer frame is displayed."""
        required = self._presentation_serial + 1
        if self._pending_screen is not None:
            required += 1
        self._presentation_required_serial = max(
            self._presentation_required_serial,
            required,
        )

    def cancel_fresh_presentation_requirement(self) -> None:
        """Restore pointer input when a requested topology change failed."""
        self._presentation_required_serial = self._presentation_serial + (
            1 if self._pending_screen is not None else 0
        )

    def set_selection(self, selection: tuple[tuple[int, int, bytes], ...]) -> None:
        if selection == self._selection:
            return
        self._selection_dirty_rows.update(row for row, _column, _text in self._selection)
        self._selection_dirty_rows.update(row for row, _column, _text in selection)
        self._selection = selection

    def set_history_overlays(
        self,
        overlays: tuple[tuple[HistorySnapshot, tuple[bytes, ...]], ...],
    ) -> None:
        if overlays == self._history_overlays:
            return
        for snapshot, _lines in self._history_overlays + overlays:
            self._selection_dirty_rows.update(
                range(snapshot.y, snapshot.y + snapshot.height)
            )
        self._history_overlays = overlays

    def copy_to_clipboard(self, data: bytes) -> None:
        self._pending_clipboard = data

    def show_status(self, message: str, *, level: str = "info") -> None:
        self._pending_status = (message, level)

    def show_path_prompt(self) -> None:
        self._path_prompt_visible = True
        self._path_prompt_needs_paint = True

    def clear_path_prompt(self) -> None:
        if not self._path_prompt_visible and not self._path_prompt_needs_paint:
            return
        painted = self._path_prompt_painted
        self._path_prompt_visible = False
        self._path_prompt_needs_paint = False
        self._path_prompt_painted = False
        if painted and self._screen is not None:
            self._selection_dirty_rows.update(range(self._screen.height))

    def show_claude_history_prompt(self) -> None:
        self._claude_history_prompt_visible = True
        self._claude_history_prompt_needs_paint = True

    def clear_claude_history_prompt(self) -> None:
        painted = self._claude_history_prompt_painted
        self._claude_history_prompt_visible = False
        self._claude_history_prompt_needs_paint = False
        self._claude_history_prompt_painted = False
        if painted:
            self.invalidate_local_overlay()

    def invalidate_local_overlay(self) -> None:
        """Repaint the authoritative screen after a one-shot local dialog."""
        if self._screen is not None:
            self._selection_dirty_rows.update(range(self._screen.height))

    def feed(self, payload: bytes) -> None:
        self.producer.feed(payload)
        clipboard = self.producer.drain_clipboard()
        if clipboard:
            # Clipboard ownership is latest-state semantics too. Holding one
            # validated request prevents a chatty source from building a
            # side-channel write queue behind a slow physical frame.
            self._pending_clipboard = clipboard[-1]

    def resize(self, width: int, height: int) -> None:
        self.producer.resize(width, height)
        self.surface.set_physical_size(os.terminal_size((width, height)))
        self._next_frame = 0.0

    def _synchronized_update_blocked(self, now: float) -> bool:
        """Avoid partial frames, but never let a malformed frame freeze input."""
        if not self.producer.synchronized_update_active:
            self._synchronized_since = None
            self._synchronized_bypassed = False
            return False
        if self._synchronized_bypassed:
            return False
        if self._synchronized_since is None:
            self._synchronized_since = now
        if now - self._synchronized_since >= _SYNCHRONIZED_UPDATE_MAX_HOLD:
            # Stay in semantic latest-state mode. This is not a raw-output
            # fallback: it merely stops trusting one unclosed source frame.
            self._synchronized_bypassed = True
            return False
        return True

    def _paint_pending_local_ui(self, now: float) -> bool:
        painted = False
        if self._pending_status is not None:
            message, level = self._pending_status
            self._pending_status = None
            self.surface.show_local_status(
                message,
                level=level,
                expires_at=now + 2.0,
            )
            painted = True
        if self._path_prompt_visible and self._path_prompt_needs_paint:
            self.surface.show_path_open_prompt()
            self._path_prompt_needs_paint = False
            self._path_prompt_painted = True
            painted = True
        if (
            self._claude_history_prompt_visible
            and self._claude_history_prompt_needs_paint
        ):
            self.surface.show_claude_history_prompt()
            self._claude_history_prompt_needs_paint = False
            self._claude_history_prompt_painted = True
            painted = True
        return painted

    def next_timeout(self, maximum: float, now: float) -> float:
        self._promote_presented_screen()
        if not self.writer.idle:
            return min(maximum, _TERMINAL_WRITER_POLL)
        if self._synchronized_update_blocked(now):
            assert self._synchronized_since is not None
            remaining = (
                self._synchronized_since
                + _SYNCHRONIZED_UPDATE_MAX_HOLD
                - now
            )
            return max(0.0, min(maximum, remaining))
        if self.producer.dirty:
            if now >= self._next_frame:
                return 0.0
            return max(0.0, min(maximum, self._next_frame - now))
        if self._pending_clipboard is not None:
            return max(0.0, min(maximum, self._next_frame - now))
        if self._selection_dirty_rows:
            return max(0.0, min(maximum, self._next_frame - now))
        if (
            self._pending_status is not None
            or self._path_prompt_needs_paint
            or self._claude_history_prompt_needs_paint
        ):
            return 0.0
        return maximum

    def paint_due(self, now: float, *, force: bool = False) -> bool:
        self._promote_presented_screen()
        if not self.writer.idle:
            return False
        local_ui_pending = bool(
            self._pending_status is not None
            or self._path_prompt_needs_paint
            or self._claude_history_prompt_needs_paint
        )
        if not force and not local_ui_pending and now < self._next_frame:
            return False
        if self._synchronized_update_blocked(now):
            return False
        if self.surface.expire_local_status(now) and self._screen is not None:
            self._selection_dirty_rows.add(self._screen.height - 1)
        if self._pending_clipboard is not None:
            clipboard = self._pending_clipboard
            self._pending_clipboard = None
            self.surface.copy_to_clipboard(clipboard)
        update = self.producer.take_update()
        if update is None:
            painted = False
            if self._screen is not None and self._selection_dirty_rows:
                rows = set(self._selection_dirty_rows)
                self._selection_dirty_rows.clear()
                self.surface.paint_changed_rows(
                    self._screen,
                    self._history_overlays,
                    self._selection,
                    rows,
                )
                painted = True
            painted = self._paint_pending_local_ui(now) or painted
            if painted:
                self._next_frame = now + _LOCAL_FRAME_INTERVAL
            return painted
        screen = self.model.apply(
            update,
            os.terminal_size((self.producer.width, self.producer.height)),
        )
        if screen is None:
            raise WindowsAttachRelayError("local terminal screen lost its update base")
        if self._selection_dirty_rows:
            screen = replace(
                screen,
                changed_rows=tuple(sorted(set(screen.changed_rows) | self._selection_dirty_rows)),
            )
            self._selection_dirty_rows.clear()
        self.surface.paint(
            screen,
            self._history_overlays,
            selection=self._selection,
        )
        self._pending_screen = screen
        if self._path_prompt_visible:
            # The authoritative screen frame overwrote the local modal.
            self._path_prompt_needs_paint = True
        if self._claude_history_prompt_visible:
            self._claude_history_prompt_needs_paint = True
        self._paint_pending_local_ui(now)
        self._next_frame = now + _LOCAL_FRAME_INTERVAL
        return True

    def close(self) -> bool:
        deadline = time.monotonic() + _TERMINAL_WRITER_CLOSE_TIMEOUT

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        writer_idle = False
        restore_payload = self.surface.close_payload()
        restore_deferred = False
        try:
            try:
                writer_idle = self.writer.wait_idle(remaining())
            except (OSError, WindowsAttachRelayError):
                writer_idle = False
            if writer_idle and self.producer.received_output:
                try:
                    self.paint_due(time.monotonic(), force=True)
                    writer_idle = self.writer.wait_idle(remaining())
                except (OSError, WindowsAttachRelayError):
                    writer_idle = False
            if writer_idle:
                try:
                    self.surface.close()
                    writer_idle = self.writer.wait_idle(remaining())
                except (OSError, WindowsAttachRelayError):
                    writer_idle = False
        finally:
            restore_deferred = self.writer.close(
                remaining(),
                final_payload=restore_payload if not writer_idle else None,
            )
        return restore_deferred


class _ActiveWindowsInterruptForwarder:
    """Turn a native Windows Ctrl-C signal into one PTY input byte."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._pending = 0
        self._previous: object | None = None

    def start(self) -> None:
        if not self.enabled or self._previous is not None:
            return
        try:
            self._previous = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, self._capture)
        except (OSError, RuntimeError, ValueError):
            self.enabled = False
            self._previous = None

    def _capture(self, _signum: int, _frame: object) -> None:
        self._pending = min(16, self._pending + 1)

    def consume(self) -> int:
        pending = self._pending
        self._pending = 0
        return pending

    def close(self) -> None:
        if self.enabled and self._previous is not None:
            signal.signal(signal.SIGINT, self._previous)
        self._previous = None


class WindowsAttachRelayError(RuntimeError):
    """A bounded relay setup or transport failure."""


@dataclass(frozen=True)
class _EndpointIdentity:
    dev: int
    ino: int


class _FrameDecoder:
    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buffer.extend(data)
        frames: list[tuple[int, bytes]] = []
        while len(self._buffer) >= _HEADER.size:
            kind, size = _HEADER.unpack(self._buffer[: _HEADER.size])
            if size > _MAX_FRAME_BYTES:
                raise WindowsAttachRelayError("terminal bridge frame is too large")
            end = _HEADER.size + size
            if len(self._buffer) < end:
                break
            frames.append((kind, bytes(self._buffer[_HEADER.size : end])))
            del self._buffer[:end]
        return frames


def _frame(kind: int, payload: bytes = b"") -> bytes:
    if len(payload) > _MAX_FRAME_BYTES:
        raise WindowsAttachRelayError("terminal bridge frame is too large")
    return _HEADER.pack(kind, len(payload)) + payload


def _terminal_capability(value: str | None, default: str, limit: int) -> str:
    candidate = value or default
    if not 1 <= len(candidate) <= limit or any(
        ord(char) < 0x20 or ord(char) > 0x7E for char in candidate
    ):
        return default
    return candidate


def _terminal_size(fd: int) -> tuple[int, int]:
    size = os.get_terminal_size(fd)
    return max(1, min(size.columns, 65535)), max(1, min(size.lines, 65535))


def _set_winsize(fd: int, width: int, height: int) -> None:
    fcntl.ioctl(
        fd,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", height, width, 0, 0),
    )


def _endpoint_identity(path: Path) -> _EndpointIdentity | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
    ):
        return None
    return _EndpointIdentity(info.st_dev, info.st_ino)


def _unlink_owned_endpoint(path: Path, identity: _EndpointIdentity | None) -> None:
    if identity is None or _endpoint_identity(path) != identity:
        return
    try:
        path.unlink()
    except OSError:
        pass


def _validate_endpoint(path: Path) -> bool:
    try:
        root = restart_state.runtime_state_dir()
        parent = path.parent.lstat()
    except OSError:
        return False
    return bool(
        path.parent == root
        and _RELAY_NAME.fullmatch(path.name)
        and stat.S_ISDIR(parent.st_mode)
        and parent.st_uid == os.getuid()
        and not parent.st_mode & 0o022
        and _endpoint_identity(path) is not None
    )


def _cleanup_stale_endpoints(root: Path) -> None:
    """Remove only old, same-owner relay sockets with no live listener."""
    try:
        entries = root.iterdir()
    except OSError:
        return
    now = time.time()
    matched = 0
    for path in entries:
        if _RELAY_NAME.fullmatch(path.name) is None:
            continue
        matched += 1
        if matched > _MAX_STALE_ENDPOINTS:
            break
        identity = _endpoint_identity(path)
        if identity is None:
            continue
        try:
            if now - path.lstat().st_mtime < _STALE_ENDPOINT_AGE:
                continue
        except OSError:
            continue
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.1)
        try:
            probe.connect(str(path))
        except OSError:
            _unlink_owned_endpoint(path, identity)
        finally:
            probe.close()


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        data = connection.recv(remaining)
        if not data:
            raise WindowsAttachRelayError("terminal bridge closed during handshake")
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def _peer_is_same_user(connection: socket.socket) -> bool:
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        # MSYS2 releases without SO_PEERCRED still retain a same-owner,
        # non-writable runtime directory plus an unguessable handshake token.
        return True
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, option, 12)
        _pid, uid, _gid = struct.unpack("3i", raw)
    except OSError as exc:
        if exc.errno in {
            errno.EINVAL,
            errno.ENOPROTOOPT,
            getattr(errno, "ENOTSUP", errno.EINVAL),
        }:
            return True
        return False
    except struct.error:
        return False
    return uid == os.getuid()


def _challenge_response(token: bytes, challenge: bytes) -> bytes:
    return hmac.new(token, challenge, hashlib.sha256).digest()


def _normalized_wait_status(status: int) -> int:
    result = os.waitstatus_to_exitcode(status)
    return 128 - result if result < 0 else result


def _spawn_tmux_client(
    target: tmux_server.TmuxServerTarget,
    session_id: str,
    *,
    tmux_path: str,
    width: int,
    height: int,
    term: str,
    colorterm: str | None,
    synchronized_output: bool,
) -> tuple[int, int]:
    master_fd, slave_fd = os.openpty()
    _set_winsize(slave_fd, width, height)
    pid = os.fork()
    if pid == 0:  # pragma: no cover - exercised by real MSYS2/PTY tests
        try:
            os.close(master_fd)
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            for target_fd in (0, 1, 2):
                os.dup2(slave_fd, target_fd)
            if slave_fd > 2:
                os.close(slave_fd)
            env = os.environ.copy()
            env.pop("TMUX", None)
            env.pop("TMUX_PANE", None)
            env["TERM"] = term
            if colorterm:
                env["COLORTERM"] = colorterm
            else:
                env.pop("COLORTERM", None)
            argv = tmux_server.target_argv(
                target,
                *tmux_server.client_feature_args(
                    ("sync",) if synchronized_output else ()
                ),
                "attach-session",
                "-t",
                session_id,
            )
            argv[0] = tmux_path
            os.execve(tmux_path, argv, env)
        except BaseException:
            os._exit(127)
    os.close(slave_fd)
    os.set_blocking(master_fd, False)
    return pid, master_fd


def _spawn_local_pty_process(
    argv: Sequence[str],
    environ: Mapping[str, str],
    *,
    width: int,
    height: int,
    suppress_stderr: bool = False,
) -> tuple[int, int]:
    """Run one ordinary tmux client behind a same-session private PTY."""
    if not argv or not argv[0]:
        raise WindowsAttachRelayError("tmux client command is unavailable")
    master_fd, slave_fd = os.openpty()
    _set_winsize(slave_fd, width, height)
    pid = os.fork()
    if pid == 0:  # pragma: no cover - exercised by real managed Windows tests
        try:
            os.close(master_fd)
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            for target_fd in (0, 1):
                os.dup2(slave_fd, target_fd)
            if suppress_stderr:
                null_fd = os.open(os.devnull, os.O_WRONLY)
                try:
                    os.dup2(null_fd, 2)
                finally:
                    if null_fd > 2:
                        os.close(null_fd)
            else:
                os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            os.execvpe(argv[0], list(argv), dict(environ))
        except BaseException:
            os._exit(127)
    os.close(slave_fd)
    os.set_blocking(master_fd, False)
    return pid, master_fd


def _child_status(pid: int) -> int | None:
    try:
        waited, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return 127
    if waited == 0:
        return None
    return _normalized_wait_status(status)


def _stop_child(pid: int) -> int:
    status = _child_status(pid)
    if status is not None:
        return status
    grace_deadline = time.monotonic() + _CHILD_EXIT_GRACE
    while time.monotonic() < grace_deadline:
        status = _child_status(pid)
        if status is not None:
            return status
        time.sleep(0.01)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        status = _child_status(pid)
        if status is not None:
            return status
        time.sleep(0.02)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        _waited, raw = os.waitpid(pid, 0)
    except ChildProcessError:
        return 127
    return _normalized_wait_status(raw)


def _write_pty_input(master_fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    deadline = time.monotonic() + _PTY_INPUT_TIMEOUT
    while view:
        try:
            written = os.write(master_fd, view)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise WindowsAttachRelayError("terminal bridge input remained blocked")
            time.sleep(0.005)
            continue
        if written <= 0:
            raise WindowsAttachRelayError("terminal bridge could not forward input")
        view = view[written:]


def _write_terminal_output(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise WindowsAttachRelayError("terminal proxy could not forward output")
        view = view[written:]


def _drain_pty_output(
    master_fd: int,
    connection: socket.socket,
) -> None:
    """Forward tmux's bounded terminal-restore tail after client exit."""
    deadline = time.monotonic() + _DRAIN_TIMEOUT
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        readable, _writable, _exceptional = select.select(
            [master_fd], [], [], remaining
        )
        if not readable:
            return
        try:
            data = os.read(master_fd, 65536)
        except BlockingIOError:
            continue
        except OSError as exc:
            if exc.errno == errno.EIO:
                return
            raise
        if not data:
            return
        connection.sendall(_frame(_TYPE_OUTPUT, data))


def _relay_server_loop(
    connection: socket.socket,
    *,
    target: tmux_server.TmuxServerTarget,
    session_id: str,
    tmux_path: str,
    width: int,
    height: int,
    term: str,
    colorterm: str | None,
    synchronized_output: bool,
) -> int:
    pid, master_fd = _spawn_tmux_client(
        target,
        session_id,
        tmux_path=tmux_path,
        width=width,
        height=height,
        term=term,
        colorterm=colorterm,
        synchronized_output=synchronized_output,
    )
    decoder = _FrameDecoder()
    selector = selectors.DefaultSelector()
    selector.register(connection, selectors.EVENT_READ, "client")
    selector.register(master_fd, selectors.EVENT_READ, "tmux")
    last_heartbeat = time.monotonic()
    status: int | None = None
    try:
        while status is None:
            now = time.monotonic()
            if now - last_heartbeat > _HEARTBEAT_TIMEOUT:
                break
            for key, _events in selector.select(timeout=0.25):
                if key.data == "tmux":
                    try:
                        data = os.read(master_fd, 65536)
                    except BlockingIOError:
                        continue
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        data = b""
                    if not data:
                        status = _child_status(pid)
                        break
                    connection.sendall(_frame(_TYPE_OUTPUT, data))
                    continue

                data = connection.recv(65536)
                if not data:
                    break
                for kind, payload in decoder.feed(data):
                    if kind == _TYPE_INPUT:
                        _write_pty_input(master_fd, payload)
                    elif kind == _TYPE_RESIZE and len(payload) == 4:
                        new_width, new_height = struct.unpack(">HH", payload)
                        if new_width and new_height:
                            _set_winsize(master_fd, new_width, new_height)
                            try:
                                os.killpg(pid, signal.SIGWINCH)
                            except ProcessLookupError:
                                pass
                    elif kind == _TYPE_HEARTBEAT:
                        last_heartbeat = time.monotonic()
                    elif kind == _TYPE_CLOSE:
                        status = _stop_child(pid)
                        break
                    else:
                        raise WindowsAttachRelayError(
                            "terminal bridge received an invalid client frame"
                        )
            else:
                if status is None:
                    status = _child_status(pid)
                continue
            # A socket EOF uses the loop-breaking path above.
            break
    finally:
        selector.close()
        if status is None:
            status = _stop_child(pid)
        try:
            _drain_pty_output(master_fd, connection)
        except OSError:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
    try:
        connection.sendall(_frame(_TYPE_EXIT, struct.pack(">i", status)))
    except OSError:
        pass
    return status


class _SilentArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _relay_server_main(argv: Sequence[str]) -> int:
    parser = _SilentArgumentParser(add_help=False)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--socket-path", required=True)
    parser.add_argument("--tmux-path", required=True)
    parser.add_argument("--server-pid", required=True, type=int)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--width", required=True, type=int)
    parser.add_argument("--height", required=True, type=int)
    parser.add_argument("--term", required=True)
    parser.add_argument("--colorterm", default="")
    parser.add_argument("--synchronized-output", action="store_true")
    try:
        args = parser.parse_args(list(argv))
    except (SystemExit, ValueError):
        return 2
    # A detached server can predate the current preview app layer. The helper
    # executable is absolute and belongs to the current layer; replace only
    # these two marker hints, then independently verify both on-disk markers.
    os.environ["RAILMUX_MSYS2_RUNTIME_ID"] = args.runtime_id
    os.environ["RAILMUX_MSYS2_APP_ID"] = args.app_id
    if not running_in_managed_windows_wrapper():
        return 2
    try:
        token = bytes.fromhex(args.token)
    except ValueError:
        return 2
    endpoint = Path(args.endpoint)
    if (
        len(token) != _TOKEN_BYTES
        or _validated_label(args.label) is None
        or args.server_pid <= 0
        or not os.path.isabs(args.socket_path)
        or not os.path.isabs(args.tmux_path)
        or not os.access(args.tmux_path, os.X_OK)
        or not args.session_id.startswith("$")
        or not args.session_id[1:].isdigit()
        or not 1 <= args.width <= 65535
        or not 1 <= args.height <= 65535
        or not 1 <= len(args.term) <= 128
        or any(ord(char) < 0x20 or ord(char) > 0x7E for char in args.term)
        or len(args.colorterm) > 64
        or not _validate_endpoint(endpoint)
    ):
        return 2
    os.environ[tmux_server.SOCKET_LABEL_ENV] = args.label
    target = tmux_server.TmuxServerTarget(args.socket_path, args.server_pid)
    if not tmux_server.target_is_live(
        target, timeout=1.0
    ) or not tmux_server.target_has_session(target, args.session_id, timeout=1.0):
        return 2
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(_CONNECT_TIMEOUT)
    try:
        connection.connect(str(endpoint))
        connection.sendall(_PROTOCOL_MAGIC)
        challenge = _recv_exact(connection, hashlib.sha256().digest_size)
        connection.sendall(_challenge_response(token, challenge))
        connection.settimeout(_SEND_TIMEOUT)
        return _relay_server_loop(
            connection,
            target=target,
            session_id=args.session_id,
            tmux_path=args.tmux_path,
            width=args.width,
            height=args.height,
            term=args.term,
            colorterm=args.colorterm or None,
            synchronized_output=args.synchronized_output,
        )
    except (OSError, WindowsAttachRelayError):
        return 2
    finally:
        connection.close()


def relay_server_main(argv: Sequence[str]) -> int:
    try:
        return _relay_server_main(argv)
    except BaseException:
        # The invoking run-shell job redirects output as a second boundary;
        # never let an internal traceback disturb the live Railmux pane.
        return 2


class RelayClient:
    """Process-like terminal bridge used by the existing launcher watchdog."""

    def __init__(
        self,
        connection: socket.socket,
        listener: socket.socket,
        endpoint: Path,
        identity: _EndpointIdentity,
        *,
        stdin_fd: int,
        stdout_fd: int,
        semantic_rendering: bool = False,
        forward_interrupts: bool = False,
    ) -> None:
        self.connection = connection
        self.listener = listener
        self.endpoint = endpoint
        self.identity = identity
        self.stdin_fd = stdin_fd
        self.stdout_fd = stdout_fd
        self.returncode: int | None = None
        self._decoder = _FrameDecoder()
        self._selector = selectors.DefaultSelector()
        self._selector.register(connection, selectors.EVENT_READ, "relay")
        self._selector.register(stdin_fd, selectors.EVENT_READ, "terminal")
        self._size = _terminal_size(stdin_fd)
        self._next_heartbeat = time.monotonic() + _HEARTBEAT_INTERVAL
        self._renderer = (
            _SemanticTerminalRenderer(stdout_fd, *self._size)
            if semantic_rendering
            else None
        )
        self._interrupts = _ActiveWindowsInterruptForwarder(forward_interrupts)
        self._interrupts.start()
        self._closed = False
        self._restore_deferred = False

    def _write_output(self, payload: bytes, now: float) -> None:
        if self._renderer is None:
            _write_terminal_output(self.stdout_fd, payload)
            return
        self._renderer.feed(payload)
        self._renderer.paint_due(now)

    def _forward_pending_interrupts(self) -> None:
        pending = self._interrupts.consume()
        if not pending:
            return
        payload = b"\x03" * pending
        self.connection.sendall(_frame(_TYPE_INPUT, payload))

    def poll(self) -> int | None:
        return self.returncode

    def pump(self, timeout: float) -> None:
        if self.returncode is not None:
            return
        now = time.monotonic()
        self._forward_pending_interrupts()
        size = _terminal_size(self.stdin_fd)
        if size != self._size:
            self.connection.sendall(_frame(_TYPE_RESIZE, struct.pack(">HH", *size)))
            self._size = size
            if self._renderer is not None:
                self._renderer.resize(*size)
        if now >= self._next_heartbeat:
            self.connection.sendall(_frame(_TYPE_HEARTBEAT))
            self._next_heartbeat = now + _HEARTBEAT_INTERVAL
        wait = max(0.0, timeout)
        if self._renderer is not None:
            wait = self._renderer.next_timeout(wait, now)
        events = _terminal_events_first(self._selector.select(timeout=wait))
        for key, _events in events:
            if key.data == "terminal":
                data = os.read(self.stdin_fd, 65536)
                if not data:
                    self._selector.unregister(self.stdin_fd)
                    self.connection.sendall(_frame(_TYPE_CLOSE))
                    continue
                self.connection.sendall(_frame(_TYPE_INPUT, data))
                continue
            data = self.connection.recv(65536)
            if not data:
                raise WindowsAttachRelayError(
                    "terminal bridge connection ended unexpectedly"
                )
            for kind, payload in self._decoder.feed(data):
                if kind == _TYPE_OUTPUT:
                    output_now = time.monotonic()
                    self._write_output(payload, output_now)
                elif kind == _TYPE_EXIT and len(payload) == 4:
                    self.returncode = struct.unpack(">i", payload)[0]
                else:
                    raise WindowsAttachRelayError(
                        "terminal bridge received an invalid relay frame"
                    )
        self._forward_pending_interrupts()
        if self._renderer is not None:
            self._renderer.paint_due(time.monotonic())

    def terminate(self) -> None:
        if self.returncode is not None:
            return
        try:
            self.connection.sendall(_frame(_TYPE_CLOSE))
        except OSError:
            pass
        self.returncode = 143

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.returncode is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("windows terminal bridge", timeout)
            self.pump(0.05)
        return self.returncode

    def close(self) -> bool:
        if self._closed:
            return self._restore_deferred
        self._closed = True
        self._interrupts.close()
        try:
            if self._renderer is not None:
                try:
                    self._restore_deferred = self._renderer.close()
                except (OSError, WindowsAttachRelayError):
                    pass
        finally:
            self._selector.close()
            self.connection.close()
            self.listener.close()
            _unlink_owned_endpoint(self.endpoint, self.identity)
        return self._restore_deferred


class LocalPtyClient:
    """Process-like local tmux proxy with a semantic latest-state renderer."""

    def __init__(
        self,
        pid: int,
        master_fd: int,
        *,
        stdin_fd: int,
        stdout_fd: int,
        forward_interrupts: bool = False,
        session_id: str | None = None,
        history_limit: int | None = None,
    ) -> None:
        self.pid = pid
        self.master_fd = master_fd
        self.stdin_fd = stdin_fd
        self.stdout_fd = stdout_fd
        self.returncode: int | None = None
        self._selector = selectors.DefaultSelector()
        self._selector.register(master_fd, selectors.EVENT_READ, "tmux")
        self._selector.register(stdin_fd, selectors.EVENT_READ, "terminal")
        self._size = _terminal_size(stdin_fd)
        self._renderer = _SemanticTerminalRenderer(stdout_fd, *self._size)
        self._session_id = session_id
        self._input_decoder = TerminalInputDecoder()
        self._selection = LocalTextSelection()
        self._passthrough_left_gesture = False
        self._suppressed_left_gesture = False
        self._routes: tuple[HistorySnapshot, ...] = ()
        self._routes_checked_at = 0.0
        self._routes_force_refresh = True
        if history_limit is None:
            try:
                history_limit = load_config().history_lines
            except ConfigError:
                # Normal launch validates config before this adapter starts.
                # If the file changes in that narrow interval, keep the
                # terminal usable with the documented bound instead of
                # escaping raw mode through an adapter-only traceback.
                history_limit = HISTORY_DEFAULT_LINES
        self._history = LocalHistoryView(history_limit, wheel_lines=3)
        self._history_request_decoder = InputFrameDecoder()
        self._history_worker: HistoryCaptureWorker | None = None
        self._history_prefetch = PeriodicPrefetchGate()
        self._next_history_prefetch = 0.0
        self._claude_history_override: str | None = None
        self._claude_history_prompt_input: bytes | None = None
        self._claude_history_prompt_mouse_button: int | None = None
        self._path_prompt: tuple[ClickTarget, PathResult] | None = None
        self._path_prompt_mouse_button: int | None = None
        # Path validation is optional. Avoid importing the server graph or
        # starting a thread on the ordinary attach/input path until a clean
        # semantic path click actually needs it.
        self._path_worker = None
        self._pending_path_open: (
            tuple[ClickTarget, PathResult, str] | None
        ) = None
        self._pending_path_resolve: tuple[int, ClickTarget] | None = None
        self._next_path_request_id = 1
        self._interrupts = _ActiveWindowsInterruptForwarder(forward_interrupts)
        self._interrupts.start()
        self._closed = False
        self._restore_deferred = False
        self._pty_backlog_pending = False
        started = time.monotonic()
        self._last_pty_paint = started
        self._last_pty_output = started
        self._pty_catchup_active = True
        self._pty_catchup_started = started
        self._pty_seen_output = False
        self._pty_burst_started = started
        self._pty_burst_bytes = 0
        if session_id is not None:
            import pyte

            from railmux.fast_display_server import (
                capture_history_batch,
                capture_history_snapshot,
            )

            self._history_worker = HistoryCaptureWorker(
                pyte,
                capture_snapshot=capture_history_snapshot,
                capture_batch=capture_history_batch,
            )
            self._selector.register(
                self._history_worker.read_fd,
                selectors.EVENT_READ,
                "history",
            )
            self._request_history_prefetch(started, force=True)

    def _ensure_path_worker(self):
        worker = getattr(self, "_path_worker", None)
        if worker is None:
            from railmux.fast_display_server import PathActionWorker

            worker = PathActionWorker()
            self._path_worker = worker
        return worker

    def _request_history_prefetch(
        self,
        now: float | None = None,
        *,
        force: bool = False,
    ) -> None:
        if self._history_worker is None:
            return
        checked_at = time.monotonic() if now is None else now
        frame = self._history.begin_prefetch(checked_at, force=force)
        if frame:
            self._dispatch_history_frame(frame)
            self._history_prefetch.sent(self._history.prefetch_pending_id)
        self._next_history_prefetch = checked_at + _LOCAL_HISTORY_PREFETCH_INTERVAL

    def _dispatch_history_frame(self, frame: bytes) -> None:
        if not frame or self._history_worker is None or self._session_id is None:
            return
        messages = self._history_request_decoder.feed(frame)
        if len(messages) != 1:
            return
        message = messages[0]
        if message.kind is InputKind.PREFETCH_HISTORY:
            request_id, max_lines = decode_history_prefetch(message.data)
            job = HistoryCaptureJob(
                "batch",
                self._session_id,
                (request_id, max_lines),
                self._claude_history_override,
            )
            if not self._history_worker.submit(job):
                self._accept_history_result(HistoryBatch(request_id, ()))
        elif message.kind is InputKind.REQUEST_HISTORY:
            request = decode_history_request(message.data)
            job = HistoryCaptureJob(
                "snapshot",
                self._session_id,
                request,
                self._claude_history_override,
            )
            if not self._history_worker.submit(job):
                self._accept_history_result(HistorySnapshot(request[0], None))

    def _accept_history_result(
        self,
        result: HistorySnapshot | HistoryBatch,
    ) -> None:
        if isinstance(result, HistoryBatch):
            pending_id = self._history.prefetch_pending_id
            action = self._history.accept_prefetch(result)
            self._history_prefetch.accepted(result.request_id, pending_id)
            routes = self._history.visible_routes
            self._routes_checked_at = time.monotonic()
            self._routes_force_refresh = False
        else:
            action = self._history.accept(result)
            routes = self._history.visible_routes
        if self._selection.validate_routes(routes):
            self._renderer.set_selection(self._selection.segments())
        self._routes = routes
        self._apply_history_action(action)

    def _drain_history_results(self) -> None:
        if self._history_worker is None:
            return
        for result in self._history_worker.drain():
            self._accept_history_result(result)

    def _refresh_routes(self, *, force: bool = False) -> None:
        if self._history_worker is None:
            return
        now = time.monotonic()
        force = force or self._routes_force_refresh
        if force or now - self._routes_checked_at >= _LOCAL_HISTORY_PREFETCH_INTERVAL:
            self._request_history_prefetch(now, force=force)

    def _apply_history_action(self, action: HistoryAction) -> None:
        if action.protocol_frame:
            self._dispatch_history_frame(action.protocol_frame)
        if action.forwarded_input:
            _write_pty_input(self.master_fd, action.forwarded_input)
        if action.restore_live or action.render_history:
            self._renderer.set_history_overlays(self._history.overlays())
        if action.refresh_routes:
            self._routes_force_refresh = True
            self._next_history_prefetch = 0.0
        if action.info_message:
            self._renderer.show_status(action.info_message)
        if action.claude_history_prompt:
            self._claude_history_prompt_input = action.claude_history_prompt
            self._renderer.show_claude_history_prompt()

    def _selection_source(self, event: SgrMouseEvent) -> SelectionSource | None:
        if not self._renderer.presentation_stable:
            return None
        screen = self._renderer.screen
        if screen is None:
            return None
        self._refresh_routes()
        focused = next(
            (
                candidate
                for candidate in self._routes
                if candidate.x <= screen.cursor_x < candidate.x + candidate.width
                and candidate.y <= screen.cursor_y < candidate.y + candidate.height
            ),
            None,
        )
        return self._history.selection_source(
            event,
            screen.rows,
            focused_pane_id=None if focused is None else focused.pane_id,
        )

    def _focused_history_pane_id(self) -> str | None:
        screen = self._renderer.screen
        if screen is None:
            return None
        return self._history.pane_id_at_position(screen.cursor_x, screen.cursor_y)

    def _status_row(self) -> int | None:
        screen = self._renderer.screen
        return None if screen is None else compact_status_row(screen)

    def _show_open_result(self, result: local_open.OpenResult) -> None:
        if result.copy_data:
            self._renderer.copy_to_clipboard(result.copy_data)
        self._renderer.show_status(result.message, level=result.level)

    def _apply_path_choice(
        self,
        target: ClickTarget,
        resolved: PathResult,
        policy: str,
        *,
        persistent: bool,
    ) -> None:
        if self._session_id is None:
            return
        worker = self._ensure_path_worker()
        if not worker.submit(
            self._session_id,
            resolved.request_id,
            target.pane_id,
            target.value,
            policy,
            persistent,
            target.line,
            target.column,
        ):
            self._renderer.show_status(
                "Another path open is still completing",
                level="warning",
            )
            return
        self._pending_path_open = (target, resolved, policy)
        if policy == "internal":
            # The worker is about to split/focus a tmux pane. Invalidate the
            # old hit map before that topology mutation can become observable.
            self._renderer.require_fresh_presentation()
        self._renderer.show_status("Opening path…")

    def _drain_path_action_results(self) -> None:
        worker = getattr(self, "_path_worker", None)
        if worker is None:
            return
        for result in worker.drain():
            if isinstance(result, PathResult):
                pending_resolve = getattr(
                    self, "_pending_path_resolve", None
                )
                if (
                    pending_resolve is None
                    or result.request_id != pending_resolve[0]
                ):
                    continue
                _request_id, target = pending_resolve
                self._pending_path_resolve = None
                if result.kind is PathKind.UNAVAILABLE:
                    self._renderer.show_status(
                        "Path is no longer available in this workspace",
                        level="warning",
                    )
                elif result.policy == "ask":
                    self._path_prompt = (target, result)
                    self._renderer.show_path_prompt()
                else:
                    self._apply_path_choice(
                        target,
                        result,
                        result.policy,
                        persistent=False,
                    )
                continue
            pending = getattr(self, "_pending_path_open", None)
            if pending is None or result.request_id != pending[1].request_id:
                continue
            _target, resolved, policy = pending
            self._pending_path_open = None
            self._selection.cancel()
            self._renderer.set_selection(())
            restore = self._history.invalidate_routes()
            self._apply_history_action(HistoryAction(restore_live=restore))
            self._routes = ()
            self._routes_checked_at = 0.0
            self._routes_force_refresh = True
            if not result.applied:
                if policy == "internal":
                    self._renderer.cancel_fresh_presentation_requirement()
                self._renderer.show_status(result.message, level=result.level)
            elif policy == "external":
                self._show_open_result(
                    local_open.open_windows_path(
                        resolved.path,
                        directory=resolved.kind is PathKind.DIRECTORY,
                    )
                )
            else:
                self._renderer.show_status(result.message, level=result.level)

    def _open_target(self, target: ClickTarget) -> None:
        if target.kind == "url":
            self._show_open_result(local_open.open_url(target.value))
            return
        if self._session_id is None:
            return
        request_id = getattr(self, "_next_path_request_id", 1)
        self._next_path_request_id = request_id + 1
        worker = self._ensure_path_worker()
        if not worker.submit_resolve(
            self._session_id,
            request_id,
            target.pane_id,
            target.value,
        ):
            self._renderer.show_status(
                "Another path open is still completing",
                level="warning",
            )
            return
        self._pending_path_resolve = (request_id, target)
        self._renderer.show_status("Checking path…")

    def _handle_claude_history_prompt(
        self,
        part: bytes | BracketedPasteInput | SgrMouseEvent,
    ) -> bool:
        pending = self._claude_history_prompt_input
        if pending is None:
            return False
        selected: tuple[str, bool] | None = None
        if isinstance(part, bytes):
            selected = {
                b"1": ("local", True),
                b"2": ("local", False),
                b"3": ("native", True),
                b"4": ("native", False),
            }.get(part)
        elif isinstance(part, SgrMouseEvent):
            selected = self._renderer.surface.claude_history_prompt_choice(part)
            if selected is not None:
                self._claude_history_prompt_mouse_button = part.button & 3
        if selected is None:
            if part == b"\x1b":
                self._claude_history_prompt_input = None
                self._renderer.clear_claude_history_prompt()
            else:
                self._renderer.show_claude_history_prompt()
            return True
        policy, persistent = selected
        if persistent:
            from railmux.settings import Settings

            if not Settings().set_claude_history_policy(policy):
                self._renderer.show_status(
                    "Could not save Claude history choice",
                    level="warning",
                )
                return True
            self._claude_history_override = None
        else:
            self._claude_history_override = policy
        self._claude_history_prompt_input = None
        self._renderer.clear_claude_history_prompt()
        restore = self._history.invalidate_routes()
        self._apply_history_action(HistoryAction(restore_live=restore))
        self._request_history_prefetch(force=True)
        if policy == "native":
            _write_pty_input(self.master_fd, pending)
            self._renderer.show_status("Claude native history enabled")
        else:
            self._renderer.show_status(
                "Railmux-managed Claude history enabled; scroll again",
                level="success",
            )
        return True

    def _forward_input_part(
        self,
        part: bytes | BracketedPasteInput | SgrMouseEvent,
    ) -> None:
        if (
            self._claude_history_prompt_mouse_button is not None
            and isinstance(part, SgrMouseEvent)
        ):
            suppress = (
                not part.pressed
                and part.button & 3 == self._claude_history_prompt_mouse_button
            )
            self._claude_history_prompt_mouse_button = None
            if suppress:
                return
        if self._handle_claude_history_prompt(part):
            return
        if (
            self._path_prompt_mouse_button is not None
            and isinstance(part, SgrMouseEvent)
        ):
            suppress = (
                not part.pressed
                and part.button & 3 == self._path_prompt_mouse_button
            )
            self._path_prompt_mouse_button = None
            if suppress:
                return
        if self._path_prompt is not None:
            target, resolved = self._path_prompt
            choice = None
            if isinstance(part, bytes):
                choice = {
                    b"1": ("internal", True),
                    b"2": ("internal", False),
                    b"3": ("external", True),
                    b"4": ("external", False),
                }.get(part)
            elif isinstance(part, SgrMouseEvent):
                choice = self._renderer.surface.path_open_prompt_choice(part)
                if choice is not None:
                    self._path_prompt_mouse_button = part.button & 3
            if choice is not None:
                self._path_prompt = None
                self._renderer.clear_path_prompt()
                self._apply_path_choice(
                    target,
                    resolved,
                    choice[0],
                    persistent=choice[1],
                )
                return
            if part == b"\x1b":
                self._path_prompt = None
                self._renderer.clear_path_prompt()
                self._renderer.show_status("Path opening cancelled")
                return
            # The prompt is local authority. Unknown keys, paste, hover, and
            # clicks outside it must not leak through to the provider pane.
            return
        if isinstance(part, BracketedPasteInput):
            screen = self._renderer.screen
            restore = (
                self._history.cancel_for_input(screen.cursor_x, screen.cursor_y)
                if screen is not None
                else self._history.cancel()
            )
            self._apply_history_action(HistoryAction(restore_live=restore))
            _write_pty_input(self.master_fd, part.raw)
            return
        if isinstance(part, bytes):
            if self._selection.cancel():
                self._renderer.set_selection(self._selection.segments())
            screen = self._renderer.screen
            if (
                screen is not None
                and page_key_direction(part)
                and self._history.pane_id_at_position(
                    screen.cursor_x,
                    screen.cursor_y,
                )
                is not None
            ):
                self._apply_history_action(
                    self._history.page(
                        part,
                        screen.cursor_x,
                        screen.cursor_y,
                        now=time.monotonic(),
                    )
                )
                return
            may_change_routes = screen_input_may_change_routes(
                part,
                self._history,
                screen,
            )
            if self._history.active or self._history.pending:
                if part == b"\x1b":
                    restore = self._history.cancel()
                    self._apply_history_action(HistoryAction(restore_live=restore))
                    return
                restore = (
                    self._history.invalidate_routes()
                    if may_change_routes
                    else self._history.cancel_for_input(
                        screen.cursor_x,
                        screen.cursor_y,
                    )
                    if screen is not None
                    else self._history.cancel()
                )
                self._apply_history_action(HistoryAction(restore_live=restore))
            elif may_change_routes:
                self._history.invalidate_routes()
            if may_change_routes:
                self._routes_force_refresh = True
            _write_pty_input(self.master_fd, part)
            return
        screen = self._renderer.screen
        logical_height = self._size[1] if screen is None else screen.height
        event = self._renderer.surface.translate_mouse_event(
            part,
            logical_height=logical_height,
        )
        if not self._renderer.presentation_stable:
            if not self._selection.capturing and self._selection.cancel():
                self._renderer.set_selection(self._selection.segments())
            # A queued frame can already represent a new tmux pane topology
            # while the user still sees the prior one.  Dropping only pointer
            # input during this bounded transition is safer than forwarding it
            # to Quit or stock tmux copy-mode. A capture already owns an
            # immutable visible snapshot and therefore keeps the whole gesture.
            if not self._selection.capturing:
                return
        if self._selection.capturing:
            # Once the newer frame is physically visible, revalidate the
            # captured route before consuming another event. During a queued
            # transition the old snapshot remains the visible authority.
            if self._renderer.presentation_stable:
                self._refresh_routes()
            source = None
        else:
            source = self._selection_source(event)
        if event.is_hover_motion:
            if self._selection.hover(event, source):
                self._renderer.set_selection(self._selection.segments())
            return
        action = self._selection.pointer_event(event, source)
        if action.repaint:
            self._renderer.set_selection(self._selection.segments())
        for replay in action.replay_events:
            self._apply_history_action(
                self._history.pointer_event(
                    replay,
                    self._focused_history_pane_id(),
                    status_row=self._status_row(),
                    now=time.monotonic(),
                )
            )
        if action.copy_data is not None:
            self._renderer.copy_to_clipboard(action.copy_data)
            count = len(action.copy_data.decode("utf-8", errors="replace"))
            self._renderer.show_status(f"Copied {count:,} chars.", level="success")
        if action.open_target is not None:
            self._open_target(action.open_target)
        if not action.handled:
            self._apply_history_action(
                self._history.pointer_event(
                    event,
                    self._focused_history_pane_id(),
                    status_row=self._status_row(),
                    now=time.monotonic(),
                )
            )

    def poll(self) -> int | None:
        return self.returncode

    def _resize_if_needed(self) -> None:
        size = _terminal_size(self.stdin_fd)
        if size == self._size:
            return
        _set_winsize(self.master_fd, *size)
        self._size = size
        self._renderer.resize(*size)
        self._selection.cancel()
        self._renderer.set_selection(())
        restore = self._history.invalidate_routes()
        self._apply_history_action(HistoryAction(restore_live=restore))
        self._routes = ()
        self._routes_checked_at = 0.0
        self._routes_force_refresh = True
        self._history_prefetch.reset()
        self._next_history_prefetch = time.monotonic()
        self._renderer.require_fresh_presentation()
        self._enter_pty_catchup(time.monotonic(), restart=True)
        try:
            os.killpg(self.pid, signal.SIGWINCH)
        except ProcessLookupError:
            pass

    def _enter_pty_catchup(self, now: float, *, restart: bool = False) -> None:
        if restart or not self._pty_catchup_active:
            self._pty_catchup_started = now
        self._pty_catchup_active = True
        self._last_pty_output = now

    def _leave_pty_catchup(self, now: float) -> None:
        self._pty_catchup_active = False
        self._pty_burst_started = now
        self._pty_burst_bytes = 0

    def _observe_pty_output(self, size: int, now: float) -> None:
        if not self._pty_seen_output:
            self._pty_seen_output = True
            self._pty_catchup_started = now
        self._last_pty_output = now
        if now - self._pty_burst_started > _LOCAL_PTY_CATCHUP_WINDOW:
            self._pty_burst_started = now
            self._pty_burst_bytes = 0
        self._pty_burst_bytes += size
        catchup_threshold = max(
            _LOCAL_PTY_CATCHUP_MIN_BYTES,
            (self._size[0] * self._size[1]) // 2,
        )
        if (
            self._pty_backlog_pending
            or self._pty_burst_bytes >= catchup_threshold
            or not self._renderer.writer.idle
        ):
            self._enter_pty_catchup(now)

    def _pty_catchup_deadline(self) -> float:
        return min(
            self._last_pty_output + _LOCAL_PTY_CATCHUP_QUIET,
            max(self._last_pty_paint, self._pty_catchup_started)
            + _LOCAL_PTY_CATCHUP_MAX_STALENESS,
        )

    def _paint_is_due(self, now: float) -> bool:
        if not self._pty_catchup_active:
            return not self._pty_backlog_pending or (
                now - self._last_pty_paint >= _LOCAL_PTY_MAX_PAINT_STALENESS
            )
        quiet = (
            not self._pty_backlog_pending
            and now - self._last_pty_output >= _LOCAL_PTY_CATCHUP_QUIET
        )
        stale = (
            now - max(self._last_pty_paint, self._pty_catchup_started)
            >= _LOCAL_PTY_CATCHUP_MAX_STALENESS
        )
        return quiet or stale

    def _forward_pending_interrupts(self) -> None:
        pending = self._interrupts.consume()
        if not pending:
            return
        _write_pty_input(self.master_fd, b"\x03" * pending)
        self._leave_pty_catchup(time.monotonic())

    def _drain_ready_pty(self) -> bool:
        """Feed a bounded PTY burst and report whether its backlog is drained.

        A tmux attach or provider restore can emit megabytes of intermediate
        full-screen states.  Painting after each 64 KiB read serializes that
        replay onto Windows Terminal and can fall minutes behind current
        input.  Keep consuming into ScreenProducer instead; only an EAGAIN/EOF
        proves that the current semantic state is eligible to paint.
        """
        deadline = time.monotonic() + _LOCAL_PTY_DRAIN_BUDGET
        consumed = 0
        while consumed < _LOCAL_PTY_DRAIN_BYTES:
            try:
                data = os.read(self.master_fd, 65536)
            except BlockingIOError:
                return True
            except OSError as exc:
                if exc.errno == errno.EIO:
                    return True
                raise
            if not data:
                return True
            self._renderer.feed(data)
            self._history_prefetch.screen_updated()
            consumed += len(data)
            self._observe_pty_output(len(data), time.monotonic())
            if time.monotonic() >= deadline:
                return False
        return False

    def _drain_after_exit(self) -> None:
        deadline = time.monotonic() + _DRAIN_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            readable, _writable, _exceptional = select.select(
                [self.master_fd], [], [], remaining
            )
            if not readable:
                return
            try:
                data = os.read(self.master_fd, 65536)
            except BlockingIOError:
                continue
            except OSError as exc:
                if exc.errno == errno.EIO:
                    return
                raise
            if not data:
                return
            self._renderer.feed(data)

    def pump(self, timeout: float) -> None:
        if self.returncode is not None:
            return
        self._drain_path_action_results()
        self._drain_history_results()
        self._resize_if_needed()
        now = time.monotonic()
        if (
            self._history_worker is not None
            and now >= self._next_history_prefetch
            and self._renderer.presentation_stable
            and self._history_prefetch.should_request()
        ):
            self._request_history_prefetch(now)
        self._forward_pending_interrupts()
        wait = self._renderer.next_timeout(max(0.0, timeout), now)
        if (
            self._pty_catchup_active
            and self._renderer.producer.dirty
            and self._renderer.writer.idle
        ):
            wait = min(
                max(0.0, timeout),
                max(0.0, self._pty_catchup_deadline() - now),
            )
        events = _terminal_events_first(self._selector.select(timeout=wait))
        drained_pty = False
        for key, _events in events:
            if key.data == "history":
                self._drain_history_results()
                continue
            if key.data == "terminal":
                data = os.read(self.stdin_fd, 65536)
                if not data:
                    self.terminate()
                    return
                for part in self._input_decoder.feed(data):
                    if isinstance(part, bytes):
                        for key_part in split_page_key_input(part):
                            self._forward_input_part(key_part)
                    else:
                        self._forward_input_part(part)
                # Interactive feedback outranks replay coalescing. A later
                # screen-sized burst can enter catch-up mode again.
                self._leave_pty_catchup(time.monotonic())
                continue
            self._pty_backlog_pending = not self._drain_ready_pty()
            drained_pty = True
        if self._pty_backlog_pending and not drained_pty:
            # A budget can end exactly as the kernel queue empties.  Probe it
            # again without waiting so an EAGAIN can publish the settled frame.
            self._pty_backlog_pending = not self._drain_ready_pty()
        self._forward_pending_interrupts()
        for part in self._input_decoder.flush_pending():
            if isinstance(part, bytes):
                for key_part in split_page_key_input(part):
                    self._forward_input_part(key_part)
            else:
                self._forward_input_part(part)
        self._drain_path_action_results()
        self._drain_history_results()
        paint_now = time.monotonic()
        catchup_was_active = self._pty_catchup_active
        catchup_settled = (
            catchup_was_active
            and not self._pty_backlog_pending
            and paint_now - self._last_pty_output >= _LOCAL_PTY_CATCHUP_QUIET
        )
        if self._paint_is_due(paint_now):
            if self._renderer.paint_due(paint_now):
                self._last_pty_paint = paint_now
            if (
                catchup_settled
                and self._renderer.writer.idle
                and not self._renderer.producer.dirty
            ):
                self._leave_pty_catchup(paint_now)
        self.returncode = _child_status(self.pid)
        if self.returncode is not None:
            self._drain_after_exit()
            self._renderer.paint_due(time.monotonic(), force=True)

    def terminate(self) -> None:
        if self.returncode is None:
            self.returncode = _stop_child(self.pid)
        try:
            self._drain_after_exit()
            self._renderer.paint_due(time.monotonic(), force=True)
        except (OSError, RuntimeError):
            # Termination and reaping remain authoritative even when the
            # presentation channel that triggered cleanup is already broken.
            pass

    def kill(self) -> None:
        if self.returncode is not None:
            return
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            _waited, status = os.waitpid(self.pid, 0)
        except ChildProcessError:
            self.returncode = 127
        else:
            self.returncode = _normalized_wait_status(status)
        try:
            self._drain_after_exit()
            self._renderer.paint_due(time.monotonic(), force=True)
        except (OSError, RuntimeError):
            pass

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.returncode is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("windows terminal proxy", timeout)
            self.pump(0.05)
        return self.returncode

    def close(self) -> bool:
        if self._closed:
            return self._restore_deferred
        self._closed = True
        self._interrupts.close()
        if self.returncode is None:
            self.terminate()
        path_worker = getattr(self, "_path_worker", None)
        if path_worker is not None:
            path_worker.close()
        if self._history_worker is not None:
            try:
                self._selector.unregister(self._history_worker.read_fd)
            except (KeyError, ValueError):
                pass
            self._history_worker.close()
        try:
            self._restore_deferred = self._renderer.close()
        finally:
            self._selector.close()
            try:
                os.close(self.master_fd)
            except OSError:
                pass
        return self._restore_deferred


def start_local_pty_client(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str],
    stdin_fd: int,
    stdout_fd: int,
    suppress_stderr: bool = False,
    session_id: str | None = None,
) -> LocalPtyClient:
    """Start the managed-Windows semantic PTY client without addressing a server."""
    if not running_in_managed_windows_wrapper(environ):
        raise WindowsAttachRelayError("terminal proxy is unavailable")
    width, height = _terminal_size(stdin_fd)
    pid, master_fd = _spawn_local_pty_process(
        argv,
        environ,
        width=width,
        height=height,
        suppress_stderr=suppress_stderr,
    )
    try:
        kwargs = {
            "stdin_fd": stdin_fd,
            "stdout_fd": stdout_fd,
            "forward_interrupts": True,
        }
        if session_id is not None:
            kwargs["session_id"] = session_id
        return LocalPtyClient(pid, master_fd, **kwargs)
    except BaseException:
        _stop_child(pid)
        try:
            os.close(master_fd)
        except OSError:
            pass
        raise


def start_relay_client(
    *,
    target: tmux_server.TmuxServerTarget,
    session_id: str,
    environ: Mapping[str, str],
    stdin_fd: int,
    stdout_fd: int,
) -> RelayClient:
    if not running_in_managed_windows_wrapper(environ):
        raise WindowsAttachRelayError("terminal bridge is unavailable")
    if not session_id.startswith("$") or not session_id[1:].isdigit():
        raise WindowsAttachRelayError("managed Railmux session is unavailable")
    token = secrets.token_bytes(_TOKEN_BYTES)
    token_hex = token.hex()
    root = restart_state.runtime_state_dir()
    _cleanup_stale_endpoints(root)
    endpoint = root / f"windows-attach-{secrets.token_hex(8)}.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    identity: _EndpointIdentity | None = None
    connection: socket.socket | None = None
    try:
        listener.bind(str(endpoint))
        os.chmod(endpoint, 0o600)
        identity = _endpoint_identity(endpoint)
        if identity is None:
            raise WindowsAttachRelayError("terminal bridge endpoint is not private")
        listener.listen(2)
        listener.settimeout(0.5)
        width, height = _terminal_size(stdin_fd)
        label = tmux_server.socket_label(environ)
        tmux_path = shutil.which("tmux", path=environ.get("PATH"))
        if tmux_path is None or not os.path.isabs(tmux_path):
            raise WindowsAttachRelayError("managed tmux executable is unavailable")
        term = _terminal_capability(environ.get("TERM"), "xterm-256color", 128)
        colorterm = _terminal_capability(environ.get("COLORTERM"), "", 64)
        helper = [
            sys.executable,
            "-I",
            "-m",
            "railmux",
            "_windows-attach-relay",
            "--endpoint",
            str(endpoint),
            "--token",
            token_hex,
            "--label",
            label,
            "--runtime-id",
            environ.get("RAILMUX_MSYS2_RUNTIME_ID", ""),
            "--app-id",
            environ.get("RAILMUX_MSYS2_APP_ID", ""),
            "--socket-path",
            target.socket_path,
            "--tmux-path",
            tmux_path,
            "--server-pid",
            str(target.server_pid),
            "--session-id",
            session_id,
            "--width",
            str(width),
            "--height",
            str(height),
            "--term",
            term,
            "--colorterm",
            colorterm,
        ]
        if environ.get("WT_SESSION"):
            # The helper runs in the tmux server's Terminal Services session,
            # so carry only this capability bit from the actual entry client;
            # never persist or transmit the opaque WT_SESSION identifier.
            helper.append("--synchronized-output")
        command = (
            "exec env -u PYTHONPATH "
            + " ".join(shlex.quote(argument) for argument in helper)
            + " >/dev/null 2>&1"
        )
        result = subprocess.run(
            tmux_server.target_argv(target, "run-shell", "-b", command),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            check=False,
            env=dict(environ),
        )
        if result.returncode != 0:
            raise WindowsAttachRelayError("tmux did not start the terminal bridge")
        deadline = time.monotonic() + _CONNECT_TIMEOUT
        while time.monotonic() < deadline:
            try:
                candidate, _address = listener.accept()
            except socket.timeout:
                continue
            candidate.settimeout(_CONNECT_TIMEOUT)
            try:
                hello = _recv_exact(candidate, len(_PROTOCOL_MAGIC))
                if hello != _PROTOCOL_MAGIC or not _peer_is_same_user(candidate):
                    candidate.close()
                    continue
                challenge = secrets.token_bytes(hashlib.sha256().digest_size)
                candidate.sendall(challenge)
                response = _recv_exact(candidate, hashlib.sha256().digest_size)
                if not hmac.compare_digest(
                    response, _challenge_response(token, challenge)
                ):
                    candidate.close()
                    continue
            except (OSError, WindowsAttachRelayError):
                candidate.close()
                continue
            connection = candidate
            break
        if connection is None:
            raise WindowsAttachRelayError("terminal bridge did not become ready")
        connection.settimeout(_SEND_TIMEOUT)
        connection.sendall(_frame(_TYPE_RESIZE, struct.pack(">HH", width, height)))
        return RelayClient(
            connection,
            listener,
            endpoint,
            identity,
            stdin_fd=stdin_fd,
            stdout_fd=stdout_fd,
            semantic_rendering=bool(environ.get("WT_SESSION")),
            forward_interrupts=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if connection is not None:
            connection.close()
        listener.close()
        _unlink_owned_endpoint(endpoint, identity)
        raise WindowsAttachRelayError("terminal bridge setup failed") from exc
    except BaseException:
        if connection is not None:
            connection.close()
        listener.close()
        _unlink_owned_endpoint(endpoint, identity)
        raise


def _validated_label(label: str) -> str | None:
    try:
        return tmux_server.socket_label({tmux_server.SOCKET_LABEL_ENV: label})
    except tmux_server.TmuxServerError:
        return None
