"""Native Windows frontend for the detached Railmux ConPTY daemon."""
from __future__ import annotations

import argparse
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import BinaryIO, Callable

from railmux.config import Config
from railmux.fast_display_client import (
    ScreenModel,
    TerminalSurface,
    split_local_escape,
)
from railmux.fast_display_protocol import (
    ScreenUpdate,
    ServerMessageDecoder,
    encode_heartbeat,
    encode_input,
    encode_keyframe_request,
    encode_resize,
)
from railmux.platform.console import RawConsole
from railmux.winlocal.daemon import AUTH_OK, AUTH_PREFIX, endpoint_path
from railmux.winlocal.ipc import Endpoint, read_endpoint, receive_message, send_message


_START_TIMEOUT = 12.0
_HEARTBEAT_INTERVAL = 5.0
_MAX_COLUMNS = 1000
_MAX_LINES = 500


def connect_endpoint(endpoint: Endpoint, *, timeout: float = 2.0) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", endpoint.port), timeout=timeout)
    try:
        send_message(sock, AUTH_PREFIX + endpoint.token.encode("ascii"))
        response = receive_message(sock)
        if response is None or not response.startswith(AUTH_OK + b" "):
            raise ConnectionError("native daemon authentication failed")
        if response[len(AUTH_OK) + 1 :].decode("ascii") != endpoint.daemon_id:
            raise ConnectionError("native daemon identity changed")
        sock.settimeout(None)
        return sock
    except BaseException:
        sock.close()
        raise


def _spawn_daemon(
    claude_home: Path | None = None,
    project: Path | None = None,
) -> None:
    creationflags = _daemon_creation_flags(breakaway=True)
    argv = [sys.executable, "-m", "railmux.winlocal.daemon"]
    if claude_home is not None:
        argv.extend(("--claude-home", str(claude_home)))
    if project is not None:
        argv.extend(("--project", str(project)))
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "creationflags": creationflags,
    }
    try:
        subprocess.Popen(argv, **kwargs)
    except OSError as exc:
        if os.name != "nt" or getattr(exc, "winerror", None) != 5:
            raise
        # Some enclosing job objects forbid BREAKAWAY_FROM_JOB. A normal
        # detached process still survives ordinary console/window closure.
        kwargs["creationflags"] = _daemon_creation_flags(breakaway=False)
        subprocess.Popen(argv, **kwargs)


def _daemon_creation_flags(*, breakaway: bool) -> int:
    if os.name != "nt":
        return 0
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    return flags | (0x01000000 if breakaway else 0)


def ensure_daemon(
    *,
    spawn: Callable[[], None] = _spawn_daemon,
    timeout: float = _START_TIMEOUT,
) -> socket.socket:
    deadline = time.monotonic() + timeout
    spawned = False
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        endpoint = read_endpoint(endpoint_path())
        if endpoint is not None:
            try:
                return connect_endpoint(endpoint)
            except (ConnectionError, OSError, UnicodeError) as exc:
                last_error = exc
        if not spawned:
            spawn()
            spawned = True
        time.sleep(0.1)
    detail = f": {last_error}" if last_error is not None else ""
    raise ConnectionError(f"native Railmux daemon did not start{detail}")


class NativeClient:
    """One disposable terminal attachment to the persistent native daemon."""

    def __init__(
        self,
        sock: socket.socket,
        *,
        input_fd: int = 0,
        output_fd: int = 1,
        output: BinaryIO | None = None,
    ) -> None:
        self.sock = sock
        self.input_fd = input_fd
        self.output_fd = output_fd
        self.output = output or sys.stdout.buffer
        self._events: queue.Queue[tuple[str, bytes | None]] = queue.Queue()
        self._stop = threading.Event()

    def run(self) -> int:
        size = shutil.get_terminal_size((120, 30))
        if size.columns < 40 or size.lines < 12:
            raise ValueError("native Railmux requires a terminal of at least 40x12")
        if size.columns > _MAX_COLUMNS or size.lines > _MAX_LINES:
            raise ValueError(
                "native Railmux supports terminals up to "
                f"{_MAX_COLUMNS}x{_MAX_LINES}"
            )
        model = ScreenModel()
        decoder = ServerMessageDecoder()
        surface = TerminalSurface(self.output)
        threads = (
            threading.Thread(target=self._read_socket, daemon=True),
            threading.Thread(target=self._read_input, daemon=True),
        )
        try:
            with RawConsole(self.input_fd, output_fd=self.output_fd):
                surface.set_physical_size(size)
                surface.show_startup(size, "Connecting to native Railmux…")
                send_message(self.sock, encode_resize(size.columns, size.lines))
                send_message(self.sock, encode_keyframe_request())
                for thread in threads:
                    thread.start()
                last_heartbeat = time.monotonic()
                while True:
                    try:
                        kind, payload = self._events.get(timeout=0.1)
                    except queue.Empty:
                        kind, payload = "tick", None
                    if kind == "closed":
                        return 2
                    if kind == "input":
                        assert payload is not None
                        forwarded, detach = split_local_escape(payload)
                        if forwarded:
                            send_message(self.sock, encode_input(forwarded))
                        if detach:
                            return 0
                    elif kind == "server":
                        assert payload is not None
                        for message in decoder.feed(payload):
                            if not isinstance(message, ScreenUpdate):
                                continue
                            current_size = shutil.get_terminal_size((120, 30))
                            applied = model.apply(message, current_size)
                            if applied is None:
                                send_message(self.sock, encode_keyframe_request())
                                continue
                            surface.set_physical_size(current_size)
                            surface.paint(applied)
                    new_size = shutil.get_terminal_size((120, 30))
                    if (
                        new_size.columns > _MAX_COLUMNS
                        or new_size.lines > _MAX_LINES
                    ):
                        raise ValueError(
                            "native Railmux supports terminals up to "
                            f"{_MAX_COLUMNS}x{_MAX_LINES}"
                        )
                    if new_size != size and new_size.columns >= 40 and new_size.lines >= 12:
                        size = new_size
                        model = ScreenModel()
                        surface.set_physical_size(size)
                        send_message(self.sock, encode_resize(size.columns, size.lines))
                        send_message(self.sock, encode_keyframe_request())
                    now = time.monotonic()
                    if now - last_heartbeat >= _HEARTBEAT_INTERVAL:
                        send_message(self.sock, encode_heartbeat())
                        last_heartbeat = now
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except (ConnectionError, OSError):
            return 2
        finally:
            self._stop.set()
            surface.close()
            try:
                self.sock.close()
            except OSError:
                pass

    def _read_socket(self) -> None:
        try:
            while not self._stop.is_set():
                packet = receive_message(self.sock)
                if packet is None:
                    break
                self._events.put(("server", packet))
        except (ConnectionError, OSError, ValueError):
            pass
        self._events.put(("closed", None))

    def _read_input(self) -> None:
        try:
            while not self._stop.is_set():
                data = os.read(self.input_fd, 65535)
                if not data:
                    break
                self._events.put(("input", data))
        except OSError:
            pass


def main(
    argv: list[str] | None = None,
    *,
    config: Config | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="railmux")
    parser.add_argument("--project", metavar="PATH")
    parser.add_argument("--claude-home", default=str(Path.home() / ".claude"), help=argparse.SUPPRESS)
    parser.add_argument("--inside-tmux", action="store_true", help=argparse.SUPPRESS)
    scroll = parser.add_mutually_exclusive_group()
    scroll.add_argument("--scroll-coalescing", action="store_true", help=argparse.SUPPRESS)
    scroll.add_argument("--no-scroll-coalescing", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    del config
    project = Path(args.project).expanduser().resolve() if args.project else None
    if project is not None and not project.is_dir():
        print(f"error: project directory does not exist: {project}", file=sys.stderr)
        return 2
    existing_daemon = read_endpoint(endpoint_path()) is not None
    try:
        sock = ensure_daemon(
            spawn=lambda: _spawn_daemon(Path(args.claude_home), project)
        )
    except (ConnectionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if project is not None and existing_daemon:
        print(
            "warning: --project applies when starting the native daemon; "
            "the existing workspace remains open",
            file=sys.stderr,
        )
    try:
        return NativeClient(sock).run()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sock.close()
        return 2
