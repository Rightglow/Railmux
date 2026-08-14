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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from railmux import restart_state, tmux_server
from railmux.fast_display_client import ScreenModel, TerminalSurface
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
_SYNCHRONIZED_UPDATE_MAX_HOLD = 0.25
_RELAY_NAME = re.compile(r"windows-attach-[0-9a-f]{16}\.sock\Z")


def _terminal_events_first(events):
    """Order one selector batch so input guards same-batch PTY output."""
    return sorted(events, key=lambda event: event[0].data != "terminal")


class _TerminalFdWriter:
    """Minimal binary stream adapter which never assumes ownership of stdout."""

    def __init__(self, fd: int) -> None:
        self.fd = fd

    def writable(self) -> bool:
        return True

    def write(self, payload: bytes) -> int:
        _write_terminal_output(self.fd, payload)
        return len(payload)

    def flush(self) -> None:
        return None


class _SemanticTerminalRenderer:
    """Render only final changed rows from one local tmux PTY stream."""

    def __init__(self, stdout_fd: int, width: int, height: int) -> None:
        self.producer = ScreenProducer(width, height)
        self.model = ScreenModel()
        self.surface = TerminalSurface(
            _TerminalFdWriter(stdout_fd),
            synchronized_output=True,
        )
        self.surface.set_physical_size(os.terminal_size((width, height)))
        self._next_frame = 0.0
        self._synchronized_since: float | None = None
        self._synchronized_bypassed = False

    def feed(self, payload: bytes) -> None:
        self.producer.feed(payload)
        for clipboard_data in self.producer.drain_clipboard():
            self.surface.copy_to_clipboard(clipboard_data)

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

    def next_timeout(self, maximum: float, now: float) -> float:
        if self._synchronized_update_blocked(now):
            assert self._synchronized_since is not None
            remaining = (
                self._synchronized_since
                + _SYNCHRONIZED_UPDATE_MAX_HOLD
                - now
            )
            return max(0.0, min(maximum, remaining))
        if not self.producer.dirty or now >= self._next_frame:
            return 0.0 if self.producer.dirty else maximum
        return max(0.0, min(maximum, self._next_frame - now))

    def paint_due(self, now: float, *, force: bool = False) -> None:
        if not force and now < self._next_frame:
            return
        if self._synchronized_update_blocked(now):
            return
        update = self.producer.take_update()
        if update is None:
            return
        screen = self.model.apply(
            update,
            os.terminal_size((self.producer.width, self.producer.height)),
        )
        if screen is None:
            raise WindowsAttachRelayError("local terminal screen lost its update base")
        self.surface.paint(screen)
        self._next_frame = now + _LOCAL_FRAME_INTERVAL

    def close(self) -> None:
        if self.producer.received_output:
            self.paint_due(time.monotonic(), force=True)
        self.surface.close()


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

    def close(self) -> None:
        if self._renderer is not None:
            try:
                self._renderer.close()
            except OSError:
                pass
        self._selector.close()
        self.connection.close()
        self.listener.close()
        _unlink_owned_endpoint(self.endpoint, self.identity)
        self._interrupts.close()


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
        self._interrupts = _ActiveWindowsInterruptForwarder(forward_interrupts)
        self._interrupts.start()
        self._closed = False

    def poll(self) -> int | None:
        return self.returncode

    def _resize_if_needed(self) -> None:
        size = _terminal_size(self.stdin_fd)
        if size == self._size:
            return
        _set_winsize(self.master_fd, *size)
        self._size = size
        self._renderer.resize(*size)
        try:
            os.killpg(self.pid, signal.SIGWINCH)
        except ProcessLookupError:
            pass

    def _forward_pending_interrupts(self) -> None:
        pending = self._interrupts.consume()
        if not pending:
            return
        _write_pty_input(self.master_fd, b"\x03" * pending)

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
        self._resize_if_needed()
        now = time.monotonic()
        self._forward_pending_interrupts()
        wait = self._renderer.next_timeout(max(0.0, timeout), now)
        events = _terminal_events_first(self._selector.select(timeout=wait))
        for key, _events in events:
            if key.data == "terminal":
                data = os.read(self.stdin_fd, 65536)
                if not data:
                    self.terminate()
                    return
                _write_pty_input(self.master_fd, data)
                continue
            try:
                data = os.read(self.master_fd, 65536)
            except BlockingIOError:
                continue
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                data = b""
            if data:
                self._renderer.feed(data)
        self._forward_pending_interrupts()
        self._renderer.paint_due(time.monotonic())
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

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.returncode is None:
            self.terminate()
        try:
            self._renderer.close()
        finally:
            self._selector.close()
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self._interrupts.close()


def start_local_pty_client(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str],
    stdin_fd: int,
    stdout_fd: int,
    suppress_stderr: bool = False,
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
        return LocalPtyClient(
            pid,
            master_fd,
            stdin_fd=stdin_fd,
            stdout_fd=stdout_fd,
            forward_interrupts=True,
        )
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
