"""Detached per-user daemon which owns native Windows ConPTY sessions."""
from __future__ import annotations

import argparse
import os
import socket
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from railmux.config import Config, ConfigError, load_config
from railmux.fast_display_protocol import (
    InputFrameDecoder,
    InputKind,
    encode_update,
)
from railmux.platform.filelock import try_lock, unlock
from railmux.platform.runtime_paths import ensure_private_dir, runtime_base
from railmux.winlocal.backend import WinMuxBackend
from railmux.winlocal.ipc import (
    Endpoint,
    new_token,
    receive_message,
    send_message,
    write_endpoint,
)
from railmux.winlocal.session_store import SessionStore


AUTH_PREFIX = b"AUTH "
AUTH_OK = b"OK"
AUTH_FAILED = b"ERROR authentication failed"


def native_runtime_dir() -> Path:
    return runtime_base() / "native"


def endpoint_path() -> Path:
    return native_runtime_dir() / "endpoint.json"


def session_store_path() -> Path:
    return native_runtime_dir() / "sessions.json"


class DaemonServer:
    """Authenticated loopback server around one daemon-owned backend."""

    def __init__(
        self,
        backend: WinMuxBackend,
        *,
        endpoint_file: Path,
        token: str | None = None,
        daemon_id: str | None = None,
        fps: float = 20.0,
        idle_timeout: float = 300.0,
        app_factory: Callable[[], object] | None = None,
    ) -> None:
        self.backend = backend
        self.endpoint_file = endpoint_file
        self.token = token or new_token()
        self.daemon_id = daemon_id or backend.daemon_id
        self.fps = max(1.0, min(60.0, fps))
        self.idle_timeout = max(1.0, idle_timeout)
        self._last_nonidle = time.monotonic()
        self.app_factory = app_factory
        self._listener: socket.socket | None = None
        self._clients: set[socket.socket] = set()
        self._clients_lock = threading.RLock()
        self._stop = threading.Event()
        self._app_lock = threading.Lock()
        self._app_thread: threading.Thread | None = None
        self.backend.configure_clients(
            detach=self._detach_client,
            count=self._client_count,
        )

    def start(self) -> Endpoint:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.settimeout(0.5)
        self._listener = listener
        endpoint = Endpoint(
            port=listener.getsockname()[1],
            token=self.token,
            daemon_id=self.daemon_id,
            pid=os.getpid(),
        )
        write_endpoint(self.endpoint_file, endpoint)
        threading.Thread(
            target=self._render_loop,
            daemon=True,
            name="railmux-native-render",
        ).start()
        return endpoint

    def serve_forever(self) -> None:
        if self._listener is None:
            self.start()
        assert self._listener is not None
        try:
            while not self._stop.is_set():
                try:
                    client, _address = self._listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(
                    target=self._serve_client,
                    args=(client,),
                    daemon=True,
                    name="railmux-native-client",
                ).start()
        finally:
            self.close()

    def close(self) -> None:
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        with self._clients_lock:
            clients = tuple(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                client.close()
            except OSError:
                pass
        try:
            current = self.endpoint_file.read_text(encoding="utf-8")
            if self.daemon_id in current:
                self.endpoint_file.unlink()
        except OSError:
            pass

    def _serve_client(self, client: socket.socket) -> None:
        try:
            client.settimeout(10.0)
            auth = receive_message(client)
            expected = AUTH_PREFIX + self.token.encode("ascii")
            if auth != expected:
                send_message(client, AUTH_FAILED)
                return
            send_message(client, AUTH_OK + b" " + self.daemon_id.encode("ascii"))
            client.settimeout(None)
            with self._clients_lock:
                self._clients.add(client)
            self.backend.request_keyframe()
            self._ensure_app()
            decoder = InputFrameDecoder()
            while not self._stop.is_set():
                packet = receive_message(client)
                if packet is None:
                    return
                for message in decoder.feed(packet):
                    if message.kind is InputKind.BYTES:
                        self.backend.route_input(message.data, source=client)
                    elif message.kind is InputKind.RESIZE:
                        self.backend.resize(*struct.unpack(">HH", message.data))
                    elif message.kind is InputKind.REQUEST_KEYFRAME:
                        self.backend.request_keyframe()
                    elif message.kind is InputKind.HEARTBEAT:
                        continue
        except (ConnectionError, OSError, ValueError):
            return
        finally:
            with self._clients_lock:
                self._clients.discard(client)
            try:
                client.close()
            except OSError:
                pass

    def _ensure_app(self) -> None:
        if self.app_factory is None:
            return
        with self._app_lock:
            if self._app_thread is not None and self._app_thread.is_alive():
                return
            self._app_thread = threading.Thread(
                target=self._run_app,
                daemon=True,
                name="railmux-native-ui",
            )
            self._app_thread.start()

    def _run_app(self) -> None:
        try:
            assert self.app_factory is not None
            app = self.app_factory()
            run = getattr(app, "run")
            run()
        except Exception:
            # Keep the daemon/ConPTY authority alive so a later attachment can
            # retry the UI without converting a display fault into session loss.
            self.backend.set_status_text(
                "Native UI stopped unexpectedly; reconnect to retry",
                "error",
            )
        finally:
            # Urwid returning means hard quit, soft quit, or a failed UI. The
            # daemon and any surviving ConPTYs remain authoritative, but every
            # frontend must leave instead of displaying a frozen last frame.
            self._detach_all_clients()

    def _render_loop(self) -> None:
        interval = 1.0 / self.fps
        while not self._stop.wait(interval):
            self.backend.flush_pending_input()
            with self._clients_lock:
                clients = tuple(self._clients)
            if not clients:
                if self.backend.live_session_count():
                    self._last_nonidle = time.monotonic()
                elif time.monotonic() - self._last_nonidle >= self.idle_timeout:
                    self._stop.set()
                    listener = self._listener
                    if listener is not None:
                        listener.close()
                    return
                continue
            self._last_nonidle = time.monotonic()
            try:
                update = self.backend.screen_update()
                packet = encode_update(update)
            except (OSError, ValueError, RuntimeError):
                continue
            dead = []
            for client in clients:
                try:
                    send_message(client, packet)
                except (ConnectionError, OSError, ValueError):
                    dead.append(client)
            if dead:
                with self._clients_lock:
                    for client in dead:
                        self._clients.discard(client)

    def _client_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)

    def _detach_all_clients(self) -> None:
        with self._clients_lock:
            clients = tuple(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass

    def _detach_client(self, source: object | None) -> None:
        if not isinstance(source, socket.socket):
            return
        with self._clients_lock:
            if source not in self._clients:
                return
            self._clients.discard(source)
        try:
            source.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        source.close()


def _make_server(
    config: Config,
    *,
    fps: float = 20.0,
    claude_home: Path | None = None,
    initial_project_path: Path | None = None,
) -> DaemonServer:
    from railmux.ui.app import App

    directory = native_runtime_dir()
    ensure_private_dir(directory)
    daemon_id = uuid.uuid4().hex
    store = SessionStore(session_store_path(), daemon_id)
    stale_offers = tuple(row for row in store.load() if row.phase == "resume_offer")
    backend = WinMuxBackend(
        daemon_id=daemon_id,
        session_store=store,
        resume_offers=stale_offers,
    )
    if stale_offers:
        backend.set_status_text(
            f"{len(stale_offers)} previous session(s) available to restore",
            "warn",
        )
    return DaemonServer(
        backend,
        endpoint_file=endpoint_path(),
        daemon_id=daemon_id,
        fps=fps,
        app_factory=lambda: App(
            claude_home or Path.home() / ".claude",
            config,
            auto_launched=True,
            scroll_coalescing=False,
            mux_backend=backend,
            initial_project_path=initial_project_path,
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="railmux native-daemon")
    parser.add_argument("--fps", type=float, default=20.0)
    parser.add_argument(
        "--claude-home",
        default=str(Path.home() / ".claude"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--project", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if os.name != "nt" and not os.environ.get("RAILMUX_TEST_NATIVE_DAEMON"):
        parser.error("the native daemon is available only on Windows")
    directory = native_runtime_dir()
    ensure_private_dir(directory)
    lock_path = directory / "daemon.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    if not try_lock(lock_fd):
        os.close(lock_fd)
        return 0
    try:
        try:
            config = load_config()
        except ConfigError:
            return 2
        server = _make_server(
            config,
            fps=args.fps,
            claude_home=Path(args.claude_home),
            initial_project_path=Path(args.project) if args.project else None,
        )
        server.start()
        server.serve_forever()
        return 0
    finally:
        unlock(lock_fd)
        os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
